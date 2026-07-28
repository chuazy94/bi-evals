"""`bi_evals.Runner` — the SDK on-ramp.

The customer writes one ``ask()`` call against their own agent; the Runner owns
iteration, collection, JSONL I/O, and scoring. It is the ergonomic front-end to
the **same push pipeline** ``bi-evals score --input`` uses (``runner_core``), so
its results can never diverge from the CLI.

    import bi_evals

    runner = bi_evals.Runner("bi-evals.yaml")
    for case in runner.golden_cases():
        try:
            answer = my_agent.ask(case.question)
            runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
        except Exception as e:
            runner.submit(case, error=str(e))
    report = runner.score()
    assert report.pass_rate >= 0.9

What the SDK does NOT do: it removes *plumbing*, not *visibility*. The agent must
still expose its generated SQL (and, for skill-path scoring, its trace) — the SDK
gives a clean place to put them but cannot create them. See
``docs/push-limitations.md``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Iterator

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

from bi_evals.compare.gate import GateResult, classify_runs, evaluate_gate
from bi_evals.config import BiEvalsConfig
from bi_evals.golden.loader import load_golden_tests_with_paths
from bi_evals.promptfoo.bridge import filter_tests
from bi_evals.runner_core import PushScoreError, run_push_score
from bi_evals.store import connect as store_connect
from bi_evals.store import queries as store_queries

log = logging.getLogger("bi_evals.sdk")


def _enable_console_logging() -> None:
    """Attach a stderr handler at INFO to the ``bi_evals`` logger (idempotent).

    For ``Runner(verbose=True)`` — gives casual users readable progress without
    needing to configure ``logging`` themselves. No-op if already attached."""
    root = logging.getLogger("bi_evals")
    if any(getattr(h, "_bi_evals_console", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._bi_evals_console = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


@dataclass(frozen=True)
class Case:
    """One golden test handed to the customer's loop."""

    id: str  # golden id
    question: str  # the question to ask your agent
    golden_file: str  # internal join key — don't construct it, but safe to print
    category: str = ""


@dataclass(frozen=True)
class TestResult:
    """One scored test (mirrors the per-test row in DuckDB)."""

    # Not a pytest test class despite the name — opt out of collection.
    __test__: ClassVar[bool] = False

    test_id: str
    passed: bool
    score: float
    fail_reason: str


@dataclass(frozen=True)
class RunReport:
    """Result of ``Runner.score()`` — assertable in CI."""

    run_id: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    report_path: str
    failures: list[TestResult] = field(default_factory=list)
    # Set by Runner.score() so the gate methods can read `compare:` config and
    # open the store. Not part of the report's data surface.
    _config: BiEvalsConfig | None = field(default=None, repr=False, compare=False)

    def __bool__(self) -> bool:
        return self.failed == 0  # truthy when all passed: `if not runner.score(): ...`

    def _require_config(self) -> BiEvalsConfig:
        if self._config is None:
            raise PushScoreError(
                "This RunReport was constructed without config — gate methods "
                "are only available on reports returned by Runner.score()."
            )
        return self._config

    @property
    def passed_gate(self) -> bool:
        """Absolute-floor gate: does this run clear ``compare.min_pass_rate``?

        Needs no baseline, so it is safe on a very first run. With no
        ``min_pass_rate`` configured (or ``fail_on: never``) it is always True.
        For the baseline-relative gate, use :meth:`compare_to`.
        """
        cfg = self._require_config().compare
        gate = evaluate_gate(
            [],  # no baseline: evaluate only the absolute floor
            suite_pass_rate=self.pass_rate,
            min_pass_rate=cfg.min_pass_rate,
            max_regressions_allowed=cfg.max_regressions_allowed,
            fail_on=cfg.fail_on or "red",
        )
        return gate.passed

    def compare_to(self, ref: str = "prev") -> GateResult:
        """Gate this run against a baseline run: ``"prev"``, ``"latest"``, or a
        pinned run_id. Returns a :class:`GateResult` (truthy when the gate
        passed) instead of writing HTML or exiting — assert it in CI:

            gate = report.compare_to("prev")
            assert gate.passed, gate.reasons
        """
        config = self._require_config()
        db_path = config.resolve_path(config.storage.db_path)
        with store_connect(db_path, read_only=True) as conn:
            baseline = store_queries.resolve_run_ref(conn, ref)
            if baseline is None:
                raise PushScoreError(f"No baseline run in the store for {ref!r}.")
            if baseline == self.run_id:
                raise PushScoreError(
                    f"Baseline {ref!r} resolves to this run itself "
                    f"({self.run_id}) — use 'prev' or a pinned run_id."
                )
            compared = classify_runs(
                conn,
                baseline,
                self.run_id,
                regression_threshold=config.compare.regression_threshold,
            )
        gate = evaluate_gate(
            compared.classified,
            suite_pass_rate=self.pass_rate,
            min_pass_rate=config.compare.min_pass_rate,
            max_regressions_allowed=config.compare.max_regressions_allowed,
            fail_on=config.compare.fail_on or "red",
        )
        log.info(
            "Gate vs %s (%s): %s — %s",
            ref,
            baseline,
            "passed" if gate.passed else "FAILED",
            "; ".join(gate.reasons),
        )
        return gate


class Runner:
    """Collects submissions for the configured goldens and scores them via the
    push pipeline."""

    def __init__(
        self,
        config_path: str = "bi-evals.yaml",
        *,
        filter: str | None = None,
        verbose: bool = False,
    ):
        """``verbose=True`` attaches a stderr handler at INFO to the ``bi_evals``
        logger, so casual users see progress without touching the logging module.
        Library consumers who manage their own logging should leave it False and
        configure ``logging`` themselves (DEBUG surfaces payloads like the
        extracted SQL)."""
        self._config_path = str(config_path)
        self._config = BiEvalsConfig.load(Path(config_path))
        self._filter = filter
        self._submissions: dict[str, dict[str, Any]] = {}
        self._total_cases_cache: int | None = None
        if verbose:
            _enable_console_logging()

    def _load_selected(self) -> list[tuple[Any, str]]:
        """Load goldens honoring the filter; caches the count for heartbeats."""
        pairs = load_golden_tests_with_paths(self._config)
        if self._filter:
            pairs = filter_tests(pairs, self._filter)
        self._total_cases_cache = len(pairs)
        return pairs

    def _total_cases(self) -> int:
        """Count of selected goldens (submit heartbeat denominator), cached so
        the heartbeat doesn't re-parse every golden YAML on each submit()."""
        if self._total_cases_cache is None:
            self._load_selected()
        return self._total_cases_cache or 0

    def golden_cases(self) -> Iterator[Case]:
        """Yield one :class:`Case` per golden, honoring the ``filter``."""
        pairs = self._load_selected()
        cases = [
            Case(
                id=golden.id,
                question=golden.question,
                golden_file=rel_path,
                category=golden.category or "",
            )
            for golden, rel_path in pairs
        ]
        filt = f" (filter: {self._filter})" if self._filter else ""
        log.info("Loaded %d golden(s)%s", len(cases), filt)
        if not cases and self._filter:
            log.warning("Filter %r matched no goldens", self._filter)
        yield from cases

    @contextmanager
    def traced_call(self, case: Case, tracer: "Tracer") -> Iterator["Span"]:
        """Open a span tagged ``bi_evals.golden_id`` so this request correlates
        in the caller's own OTel backend (Datadog/Langfuse/whatever you already
        use) — so a failing bi-evals report row can be traced back to your own
        full trace for that exact request.

        Purely a courtesy to your tracing setup: it has no effect on how
        bi-evals scores the submission. Use ``submit(trace=...)`` for that, same
        as always — this does not carry ``generated_sql`` or ``trace`` data to
        bi-evals, only the correlation tag.

            import bi_evals
            from opentelemetry import trace

            runner = bi_evals.Runner("bi-evals.yaml")
            tracer = trace.get_tracer("my-agent")      # your own OTel setup, untouched

            for case in runner.golden_cases():
                with runner.traced_call(case, tracer):
                    answer = my_agent.ask(case.question)
                    runner.submit(case, generated_sql=answer.sql, trace=answer.trace)

        Requires ``opentelemetry-api`` (``uv add "bi-evals[otel]"``) — not a
        base dependency, since most first-time users won't touch this.
        """
        with tracer.start_as_current_span(f"bi_evals.golden:{case.id}") as span:
            span.set_attribute("bi_evals.golden_id", case.id)
            yield span

    def submit(
        self,
        case: Case,
        *,
        generated_sql: str | None = None,
        response_text: str | None = None,
        trace: Any = None,
        error: str | None = None,
    ) -> None:
        """Record one result, keyed by the case. Exactly one of
        ``generated_sql`` / ``response_text`` / ``error`` must be given."""
        provided = [
            name
            for name, val in (
                ("generated_sql", generated_sql),
                ("response_text", response_text),
                ("error", error),
            )
            if val
        ]
        if len(provided) != 1:
            raise ValueError(
                f"submit({case.id}): provide exactly one of generated_sql / "
                f"response_text / error (got: {provided or 'none'})."
            )
        if case.golden_file in self._submissions:
            raise ValueError(
                f"submit({case.id}): already submitted for this case "
                "(each golden may be submitted at most once)."
            )

        row: dict[str, Any] = {"golden_file": case.golden_file}
        if generated_sql:
            row["generated_sql"] = generated_sql
        elif response_text:
            row["response_text"] = response_text
        else:
            row["error"] = error
        if trace is not None:
            row["trace"] = trace
        self._submissions[case.golden_file] = row
        # Heartbeat: doubles as a progress bar, since submit() is called right
        # after each (slow) agent answer.
        log.info(
            "Submitted %s [%d/%d] via %s%s",
            case.id,
            len(self._submissions),
            self._total_cases(),
            provided[0],
            " (+trace)" if trace is not None else "",
        )

    def score(self, *, verbose: bool = False) -> RunReport:
        """Write the collected submissions to a kept ``results/sdk_<ts>.jsonl``
        artifact, run the push score pipeline, and return a :class:`RunReport`.

        ``verbose`` here streams the Promptfoo *subprocess* output — it is not
        SDK progress logging (for that, use ``Runner(verbose=True)``).

        Raises :class:`PushScoreError` (e.g. a selected golden has no
        submission, or nothing was submitted) — the same pre-flight the CLI
        applies.
        """
        if not self._submissions:
            raise PushScoreError(
                "No submissions recorded — call submit() for each golden before "
                "score()."
            )
        results_dir = self._config.resolve_path(self._config.reporting.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_file = results_dir / f"sdk_{ts}.jsonl"
        input_file.write_text(
            "\n".join(json.dumps(r) for r in self._submissions.values()) + "\n"
        )
        log.info("Wrote %d submission(s) → %s", len(self._submissions), input_file)
        # Phase boundary: narrate the slow part (Promptfoo + per-case Snowflake
        # execution) up front, so there's no dead air during the wait.
        log.info(
            "Scoring %d case(s) — executing SQL against %s …",
            len(self._submissions),
            self._config.database.type,
        )

        result = run_push_score(
            self._config,
            self._config_path,
            str(input_file),
            filter_pattern=self._filter,
            verbose=verbose,
        )
        return self._build_report(result.run_id)

    def _build_report(self, run_id: str | None) -> RunReport:
        if run_id is None:
            raise PushScoreError(
                "Scoring produced no ingested run (Promptfoo emitted no results)."
            )
        db_path = self._config.resolve_path(self._config.storage.db_path)
        from bi_evals.report import build_report_html
        from bi_evals.report.builder import sanitize_for_filename

        out_dir = self._config.resolve_path(self._config.reporting.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_file = out_dir / f"report_{sanitize_for_filename(run_id)}.html"

        with store_connect(db_path, read_only=True) as conn:
            tests = store_queries.list_tests(conn, run_id)
            html = build_report_html(
                conn,
                run_id,
                stale_after_days=self._config.scoring.stale_after_days,
                cost_alert_multiplier=self._config.storage.cost_alert_multiplier,
                cost_alert_window=self._config.storage.cost_alert_window,
                knowledge_stale_after_days=self._config.scoring.knowledge_stale_after_days,
                base_dir=self._config._base_dir,
                pass_threshold=self._config.scoring.pass_threshold,
                critical_dimensions=list(self._config.scoring.critical_dimensions),
            )
        report_file.write_text(html)

        total = len(tests)
        passed = sum(1 for t in tests if t.passed)
        failed = total - passed
        report_path = str(report_file)

        # Post-hoc per-case summary, read back from the ingested run. (The slow
        # per-case extraction/execution happens in Promptfoo subprocesses; this
        # surfaces a clean roll-up in the SDK's own process once they finish.)
        for t in tests:
            if t.passed:
                log.info("  ✓ %s (score %.2f)", t.test_id, t.score)
            else:
                log.info(
                    "  ✗ %s — %s", t.test_id, (t.fail_reason or "failed").split("\n")[0]
                )
        log.info(
            "Done: %d/%d passed (%.0f%%) → %s",
            passed,
            total,
            (100 * passed / total if total else 0.0),
            report_path,
        )

        return RunReport(
            run_id=run_id,
            total=total,
            passed=passed,
            failed=failed,
            pass_rate=(passed / total if total else 0.0),
            report_path=report_path,
            failures=[
                TestResult(
                    test_id=t.test_id,
                    passed=t.passed,
                    score=t.score,
                    fail_reason=t.fail_reason or "",
                )
                for t in tests
                if not t.passed
            ],
            _config=self._config,
        )
