"""Click-free core of the push score path, shared by the CLI and the SDK.

`cli.score` and `bi_evals.Runner.score` must run the *same* scoring pipeline so
their results can never diverge. The orchestration used to live inside the Click
command (raising ``ClickException``, calling ``click.echo``); this module holds
the plain-Python version. The CLI command is now a thin wrapper that translates
``PushScoreError`` into a ``ClickException`` and prints progress; the SDK calls
``run_push_score`` directly.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bi_evals.config import BiEvalsConfig
from bi_evals.promptfoo.bridge import (
    generate_promptfoo_config,
    run_promptfoo,
    write_promptfoo_config,
)
from bi_evals.store import connect as store_connect
from bi_evals.store import queries as store_queries
from bi_evals.store.ingest import ingest_run

log = logging.getLogger("bi_evals.runner")


class PushScoreError(Exception):
    """A user-actionable problem with a push score run (bad/missing submissions,
    no matching goldens). Carriers a clear message; callers surface it however
    they like (ClickException in the CLI, raised as-is in the SDK)."""


@dataclass
class PushScoreResult:
    """Outcome of a push score run (the file/ids; not the scored numbers — those
    are read back from DuckDB by the caller via ``store_queries``)."""

    run_id: str | None
    results_json: Path
    promptfoo_config: Path
    exit_code: int
    ingested: bool
    extra_submissions: list[str] = field(default_factory=list)


# A no-op progress sink; the CLI passes click.echo, the SDK passes nothing.
def _noop(_msg: str, *, err: bool = False) -> None:  # pragma: no cover - trivial
    pass


def validate_push_submissions(
    config: BiEvalsConfig, pf_config: dict[str, Any], input_file: str
) -> list[str]:
    """Fail fast before launching Promptfoo. Returns the list of *extra*
    submissions (rows that match no selected golden — ignored, not an error).
    Raises ``PushScoreError`` on any blocking problem."""
    from bi_evals.provider.registry import resolve_sql

    submitted: dict[str, dict] = {}
    with Path(input_file).open() as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise PushScoreError(f"{input_file}:{lineno}: invalid JSON ({e})")
            gf = row.get("golden_file")
            if not gf:
                raise PushScoreError(
                    f"{input_file}:{lineno}: row is missing required 'golden_file'."
                )
            # Same SQL resolution the adapter uses at run time, so validation and
            # scoring never disagree on what's acceptable.
            _sql, _text, sql_err = resolve_sql(row, gf)
            if sql_err:
                raise PushScoreError(f"{input_file}:{lineno}: {sql_err}")
            if gf in submitted:
                raise PushScoreError(
                    f"{input_file}:{lineno}: duplicate golden_file '{gf}' — "
                    "each golden may appear at most once."
                )
            submitted[gf] = row

    selected = {t["vars"]["golden_file"] for t in pf_config.get("tests", [])}
    missing = sorted(selected - set(submitted))
    if missing:
        raise PushScoreError(
            "No submission for these goldens:\n  "
            + "\n  ".join(missing)
            + f"\nAdd a line per golden to {input_file}."
        )
    return sorted(set(submitted) - selected)


def preflight_capability_warnings(
    config: BiEvalsConfig, pf_config: dict[str, Any], input_file: str
) -> list[str]:
    """Capability warnings computable before scoring (Build Stage 2).

    Currently: trace coverage over the selected submissions, when a
    trace-dependent dimension (e.g. ``skill_path_correctness``) is enabled.
    Returns human-readable warning lines; empty list = nothing to warn about.
    """
    from bi_evals.scorer.capability import (
        TRACE_DEPENDENT_DIMENSIONS,
        coverage_warning,
        trace_coverage,
    )

    enabled_trace_dims = TRACE_DEPENDENT_DIMENSIONS & set(config.scoring.dimensions)
    if not enabled_trace_dims:
        return []

    selected = {t["vars"]["golden_file"] for t in pf_config.get("tests", [])}
    rows: list[dict[str, Any]] = []
    with Path(input_file).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)  # validate_push_submissions already ran
            if row.get("golden_file") in selected:
                rows.append(row)

    usable, total = trace_coverage(rows)
    warnings: list[str] = []
    for dim in sorted(enabled_trace_dims):
        line_ = coverage_warning(usable, total, dim)
        if line_:
            warnings.append(line_)
    return warnings


def run_push_score(
    config: BiEvalsConfig,
    config_path: str,
    input_file: str,
    *,
    filter_pattern: str | None = None,
    verbose: bool = False,
    echo: Callable[..., None] = _noop,
) -> PushScoreResult:
    """Run the push score pipeline end to end (validate → Promptfoo → ingest).

    Forces the push adapter and points it at ``input_file``. Returns a
    :class:`PushScoreResult`; raises :class:`PushScoreError` on user-actionable
    problems. ``echo`` receives progress lines (the CLI passes ``click.echo``).

    The caller's ``config`` is never mutated: the push overrides are applied to
    a deep copy, so a long-lived config (e.g. ``Runner._config``, or one
    configured for another adapter) isn't silently flipped to push as a side
    effect of scoring.
    """
    config = copy.deepcopy(config)  # private attrs (_base_dir) survive deepcopy
    config.agent.adapter = "push"
    config.agent.push.input_file = str(Path(input_file).resolve())

    pf_config = generate_promptfoo_config(config, config_path, filter_pattern)
    if not pf_config.get("tests"):
        if filter_pattern:
            raise PushScoreError(f"No tests match filter '{filter_pattern}'.")
        raise PushScoreError(
            "No golden tests found. Add tests to the golden/ directory."
        )

    extra = validate_push_submissions(config, pf_config, input_file)

    # Pre-flight capability check (Build Stage 2): warn about dimensions that
    # will not be evaluable *before* any warehouse spend. Shared by the CLI
    # and the SDK, so the warning can't diverge between front doors.
    for warning in preflight_capability_warnings(config, pf_config, input_file):
        log.warning(warning)
        echo(f"warning: {warning}")

    results_dir = config.resolve_path(config.reporting.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pf_config_path = results_dir / f"promptfooconfig_{ts}.yaml"
    results_output = results_dir / f"eval_{ts}.json"
    write_promptfoo_config(pf_config, pf_config_path)
    echo(f"Config:  {pf_config_path}")
    echo(f"Results: {results_output}")

    # Push is a pure replay — no live agent calls to cache, so always uncached.
    exit_code = run_promptfoo(
        pf_config_path, results_output, verbose=verbose, no_cache=True, repeat=1
    )

    run_id: str | None = None
    ingested = False
    if config.storage.auto_ingest and results_output.exists():
        db_path = config.resolve_path(config.storage.db_path)
        with store_connect(db_path) as conn:
            run_id = ingest_run(conn, results_output, config)
        ingested = True
        echo(f"Ingested: {run_id}")

    return PushScoreResult(
        run_id=run_id,
        results_json=results_output,
        promptfoo_config=pf_config_path,
        exit_code=exit_code,
        ingested=ingested,
        extra_submissions=extra,
    )
