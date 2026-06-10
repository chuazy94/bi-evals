"""Tests for the bi_evals.Runner SDK."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

import bi_evals
from bi_evals.sdk import Case, RunReport, Runner, TestResult


def _project(tmp_path: Path, n_goldens: int = 2) -> Path:
    (tmp_path / "golden").mkdir(exist_ok=True)
    (tmp_path / "bi-evals.yaml").write_text(
        dedent("""\
        project: {name: SDK Test}
        agent: {adapter: push, push: {input_file: results.jsonl}}
        database: {type: snowflake}
        golden_tests: {dir: golden/}
        reporting: {results_dir: results/, output_dir: reports/}
        """)
    )
    for i in range(1, n_goldens + 1):
        (tmp_path / "golden" / f"q{i}.yaml").write_text(
            f"id: q{i}\nquestion: 'question {i}?'\nreference_sql: 'SELECT {i}'\n"
        )
    return tmp_path / "bi-evals.yaml"


class TestPublicAPI:
    def test_exports(self) -> None:
        assert bi_evals.Runner is Runner
        assert set(bi_evals.__all__) >= {"Runner", "Case", "RunReport", "TestResult"}


class TestGoldenCases:
    def test_yields_a_case_per_golden(self, tmp_path: Path) -> None:
        runner = Runner(str(_project(tmp_path, 3)))
        cases = list(runner.golden_cases())
        assert {c.id for c in cases} == {"q1", "q2", "q3"}
        assert all(c.golden_file.startswith("golden/") for c in cases)
        assert all(c.question for c in cases)

    def test_filter_applied_in_golden_cases(self, tmp_path: Path) -> None:
        runner = Runner(str(_project(tmp_path, 3)), filter="q2")
        cases = list(runner.golden_cases())
        assert [c.id for c in cases] == ["q2"]


class TestSubmit:
    def _runner_with_case(self, tmp_path: Path) -> tuple[Runner, Case]:
        runner = Runner(str(_project(tmp_path, 1)))
        case = next(iter(runner.golden_cases()))
        return runner, case

    def test_records_generated_sql(self, tmp_path: Path) -> None:
        runner, case = self._runner_with_case(tmp_path)
        runner.submit(case, generated_sql="SELECT 1", trace={"files_read": ["A.md"]})
        row = runner._submissions[case.golden_file]
        assert row["generated_sql"] == "SELECT 1"
        assert row["trace"] == {"files_read": ["A.md"]}

    def test_records_response_text(self, tmp_path: Path) -> None:
        runner, case = self._runner_with_case(tmp_path)
        runner.submit(case, response_text="```sql\nSELECT 1\n```")
        assert "response_text" in runner._submissions[case.golden_file]

    def test_records_error(self, tmp_path: Path) -> None:
        runner, case = self._runner_with_case(tmp_path)
        runner.submit(case, error="timeout")
        assert runner._submissions[case.golden_file]["error"] == "timeout"

    def test_requires_exactly_one_field(self, tmp_path: Path) -> None:
        runner, case = self._runner_with_case(tmp_path)
        with pytest.raises(ValueError, match="exactly one"):
            runner.submit(case)  # none
        with pytest.raises(ValueError, match="exactly one"):
            runner.submit(case, generated_sql="x", error="y")  # two

    def test_duplicate_submit_raises(self, tmp_path: Path) -> None:
        runner, case = self._runner_with_case(tmp_path)
        runner.submit(case, generated_sql="SELECT 1")
        with pytest.raises(ValueError, match="already submitted"):
            runner.submit(case, generated_sql="SELECT 2")


class TestScore:
    def test_score_writes_jsonl_and_builds_report(self, tmp_path: Path) -> None:
        """score() writes a kept JSONL, dispatches to run_push_score, and builds
        a RunReport from the ingested run. run_push_score + DB are stubbed so the
        test is offline and fast."""
        runner = Runner(str(_project(tmp_path, 2)))
        cases = list(runner.golden_cases())
        runner.submit(cases[0], generated_sql="SELECT 1")
        runner.submit(cases[1], error="agent crashed")

        # Fake the scored rows the report would read back from DuckDB.
        from bi_evals.store.queries import TestRow

        fake_tests = [
            TestRow(
                test_id="golden/q1.yaml",
                category=None,
                difficulty=None,
                question=None,
                passed=True,
                score=1.0,
                fail_reason=None,
                cost_usd=None,
                latency_ms=None,
                model=None,
            ),
            TestRow(
                test_id="golden/q2.yaml",
                category=None,
                difficulty=None,
                question=None,
                passed=False,
                score=0.0,
                fail_reason="agent error: agent crashed",
                cost_usd=None,
                latency_ms=None,
                model=None,
            ),
        ]

        from bi_evals.runner_core import PushScoreResult

        captured_jsonl = {}

        def fake_run_push_score(config, config_path, input_file, **kw):
            # Capture what the SDK wrote, so we can assert the JSONL shape.
            captured_jsonl["path"] = input_file
            captured_jsonl["rows"] = [
                json.loads(line)
                for line in Path(input_file).read_text().splitlines()
                if line.strip()
            ]
            return PushScoreResult(
                run_id="run-xyz",
                results_json=Path(input_file),
                promptfoo_config=Path(input_file),
                exit_code=0,
                ingested=True,
            )

        # Only run_push_score (the Promptfoo pipeline) and the DB read are
        # patched; the SDK's collection, JSONL write, and report-building run for
        # real.
        with (
            patch("bi_evals.sdk.run_push_score", side_effect=fake_run_push_score),
            patch("bi_evals.sdk.store_connect"),
            patch("bi_evals.sdk.store_queries.list_tests", return_value=fake_tests),
            patch("bi_evals.report.build_report_html", return_value="<html>"),
        ):
            report = runner.score()

        # The SDK wrote a kept sdk_<ts>.jsonl with both rows.
        assert "sdk_" in captured_jsonl["path"]
        keys = {r["golden_file"] for r in captured_jsonl["rows"]}
        assert keys == {"golden/q1.yaml", "golden/q2.yaml"}
        error_row = next(
            r for r in captured_jsonl["rows"] if r["golden_file"] == "golden/q2.yaml"
        )
        assert error_row["error"] == "agent crashed"

        # The RunReport reflects the (faked) scored results.
        assert isinstance(report, RunReport)
        assert report.run_id == "run-xyz"
        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        assert report.pass_rate == 0.5
        assert not report  # __bool__ False when any failed
        assert [f.test_id for f in report.failures] == ["golden/q2.yaml"]
        assert isinstance(report.failures[0], TestResult)
        assert "agent crashed" in report.failures[0].fail_reason

    def test_score_without_submissions_raises(self, tmp_path: Path) -> None:
        from bi_evals.runner_core import PushScoreError

        runner = Runner(str(_project(tmp_path, 1)))
        with pytest.raises(PushScoreError, match="No submissions recorded"):
            runner.score()

    def test_run_push_score_does_not_mutate_caller_config(self, tmp_path: Path) -> None:
        """run_push_score works on a deep copy — a long-lived config (e.g. the
        Runner's) must not be flipped to push/input_file as a side effect of
        scoring. Calls the REAL run_push_score with only the Promptfoo
        subprocess mocked, so the deepcopy itself is exercised."""
        import json as _json

        from bi_evals.config import BiEvalsConfig
        from bi_evals.runner_core import run_push_score

        config_file = _project(tmp_path, 1)
        config = BiEvalsConfig.load(config_file)
        # Caller's config is configured for a DIFFERENT adapter.
        config.agent.adapter = "api_endpoint"
        results = tmp_path / "r.jsonl"
        results.write_text(
            _json.dumps({"golden_file": "golden/q1.yaml", "generated_sql": "SELECT 1"})
            + "\n"
        )

        with patch("bi_evals.runner_core.run_promptfoo", return_value=0):
            run_push_score(config, str(config_file), str(results))

        assert config.agent.adapter == "api_endpoint"  # not flipped to push
        assert config.agent.push.input_file == "results.jsonl"  # untouched

    def test_report_bool_true_when_all_pass(self, tmp_path: Path) -> None:
        r = RunReport(
            run_id="r",
            total=2,
            passed=2,
            failed=0,
            pass_rate=1.0,
            report_path="x.html",
            failures=[],
        )
        assert bool(r) is True


class TestLogging:
    def test_milestones_logged(self, tmp_path: Path, caplog) -> None:
        runner = Runner(str(_project(tmp_path, 2)))
        with caplog.at_level(logging.INFO, logger="bi_evals"):
            cases = list(runner.golden_cases())
            runner.submit(cases[0], generated_sql="SELECT 1")
            runner.submit(cases[1], error="timeout")
        msgs = "\n".join(r.message for r in caplog.records)
        assert "Loaded 2 golden(s)" in msgs
        assert "Submitted q1" in msgs and "via generated_sql" in msgs
        assert "Submitted q2" in msgs and "via error" in msgs

    def test_filter_no_match_warns(self, tmp_path: Path, caplog) -> None:
        runner = Runner(str(_project(tmp_path, 2)), filter="nomatch")
        with caplog.at_level(logging.WARNING, logger="bi_evals"):
            list(runner.golden_cases())
        assert any("matched no goldens" in r.message for r in caplog.records)

    def test_silent_by_default_no_console_handler(self, tmp_path: Path) -> None:
        """Without verbose, the SDK attaches no stderr handler — only the
        package NullHandler is present, so output is silent unless the consumer
        configures logging."""
        before = list(logging.getLogger("bi_evals").handlers)
        Runner(str(_project(tmp_path, 1)))  # not verbose
        after = logging.getLogger("bi_evals").handlers
        assert not any(getattr(h, "_bi_evals_console", False) for h in after)
        assert len(after) == len(before)

    def test_verbose_attaches_console_handler_idempotently(
        self, tmp_path: Path
    ) -> None:
        root = logging.getLogger("bi_evals")
        try:
            Runner(str(_project(tmp_path, 1)), verbose=True)
            Runner(str(_project(tmp_path, 1)), verbose=True)  # again — must not dup
            console = [
                h for h in root.handlers if getattr(h, "_bi_evals_console", False)
            ]
            assert len(console) == 1
        finally:
            # Always remove the console handler(s) so a failed assertion can't
            # leak them into other logging tests.
            for h in list(root.handlers):
                if getattr(h, "_bi_evals_console", False):
                    root.removeHandler(h)
