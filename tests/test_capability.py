"""Build Stage 2 tests: capability classification, honest aggregation (D1/D2),
status ingestion/exclusion, the report capability panel, and pre-flight
coverage warnings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bi_evals.config import BiEvalsConfig
from bi_evals.report.builder import build_report_html
from bi_evals.runner_core import preflight_capability_warnings
from bi_evals.scorer.capability import (
    DimensionStatus,
    TraceUsability,
    classify_trace,
    coverage_warning,
    ne1_no_trace,
    ne2_unusable_trace,
    status_from_reason,
    trace_coverage,
)
from bi_evals.scorer.dimensions import DimensionResult, _skip, not_evaluated
from bi_evals.scorer.entry import aggregate_results
from bi_evals.store import connect as store_connect
from bi_evals.store import queries as store_queries
from bi_evals.store.ingest import ingest_run
from bi_evals.store.queries import _dims_by_test

from tests.conftest import RUN_A_JSON

USABLE_STEP = {"tool_name": "read_skill_file", "tool_input": {"path": "REVENUE.md"}}
UNUSABLE_STEP = {"type": "tool_use", "text": "I read the revenue docs"}


class TestClassifyTrace:
    def test_absent(self) -> None:
        assert classify_trace(None).usability is TraceUsability.ABSENT
        assert classify_trace([]).usability is TraceUsability.ABSENT

    def test_unusable(self) -> None:
        cap = classify_trace([UNUSABLE_STEP, {"tool_name": None, "tool_input": {}}])
        assert cap.usability is TraceUsability.UNUSABLE
        assert cap.total_entries == 2
        assert cap.usable_steps == 0

    def test_usable(self) -> None:
        cap = classify_trace([UNUSABLE_STEP, USABLE_STEP])
        assert cap.usability is TraceUsability.USABLE
        assert cap.usable_steps == 1

    def test_tool_input_must_be_dict(self) -> None:
        cap = classify_trace([{"tool_name": "t", "tool_input": "not-a-dict"}])
        assert cap.usability is TraceUsability.UNUSABLE


class TestStatusFromReason:
    def test_sniffs_prefixes(self) -> None:
        assert (
            status_from_reason("not evaluated: no trace", False)
            is DimensionStatus.NOT_EVALUATED
        )
        assert status_from_reason("skipped: nothing", True) is DimensionStatus.SKIPPED

    def test_plain_pass_fail(self) -> None:
        assert status_from_reason("all good", True) is DimensionStatus.PASS
        assert status_from_reason("wrong tables", False) is DimensionStatus.FAIL


class TestDimensionResultStatus:
    def test_derives_from_passed(self) -> None:
        assert DimensionResult("x", True, 1.0, "ok").status is DimensionStatus.PASS
        assert DimensionResult("x", False, 0.0, "bad").status is DimensionStatus.FAIL

    def test_skip_and_not_evaluated_constructors(self) -> None:
        assert _skip("x", "nothing declared").status is DimensionStatus.SKIPPED
        ne = not_evaluated("x", ne1_no_trace("x"))
        assert ne.status is DimensionStatus.NOT_EVALUATED
        assert not ne.passed


def _scoring(critical: list[str] | None = None, threshold: float = 0.75):
    from bi_evals.config import ScoringConfig

    cfg = ScoringConfig()
    cfg.pass_threshold = threshold
    if critical is not None:
        cfg.critical_dimensions = critical
    return cfg


def _pass(name: str) -> DimensionResult:
    return DimensionResult(name, True, 1.0, "ok")


def _fail(name: str) -> DimensionResult:
    return DimensionResult(name, False, 0.0, "bad")


class TestAggregateResults:
    def test_not_evaluated_excluded_from_score(self) -> None:
        # skill_path NE must not drag the score down nor count as a pass.
        results = [
            _pass("execution"),
            not_evaluated("skill_path_correctness", "not evaluated: x"),
        ]
        ok, score, reason = aggregate_results(results, _scoring(critical=["execution"]))
        assert ok
        assert score == 1.0
        assert "1 not evaluated" in reason

    def test_d1_critical_not_evaluated_fails(self) -> None:
        results = [
            _pass("execution"),
            not_evaluated("skill_path_correctness", "not evaluated: x"),
        ]
        ok, _, reason = aggregate_results(
            results, _scoring(critical=["execution", "skill_path_correctness"])
        )
        assert not ok
        assert "could not be evaluated" in reason
        assert "skill_path_correctness" in reason

    def test_d2_skipped_excluded_from_denominator(self) -> None:
        # pass(w3) + fail(w1) + skipped(w2): old math (3+2)/(3+1+2)=0.83;
        # honest math 3/(3+1)=0.75. Weights: execution 3, filter 1, anti 2.
        results = [
            _pass("execution"),
            _fail("filter_correctness"),
            _skip("anti_pattern_compliance", "no anti-patterns defined"),
        ]
        ok, score, _ = aggregate_results(
            results, _scoring(critical=["execution"], threshold=0.8)
        )
        assert score == pytest.approx(0.75)
        assert not ok  # 0.75 < 0.8; the vacuous skip no longer pads the score

    def test_nothing_evaluable_fails_clearly(self) -> None:
        results = [not_evaluated("skill_path_correctness", "not evaluated: x")]
        ok, score, reason = aggregate_results(results, _scoring(critical=[]))
        assert not ok
        assert score == 0.0
        assert "No dimension could be evaluated" in reason

    def test_failed_critical_still_fails(self) -> None:
        results = [_fail("execution"), _pass("table_alignment")]
        ok, _, reason = aggregate_results(results, _scoring(critical=["execution"]))
        assert not ok
        assert "Failed critical dimension(s)" in reason


class TestCoverage:
    def test_trace_coverage_shapes_and_error_rows(self) -> None:
        rows = [
            {"golden_file": "a", "generated_sql": "SELECT 1", "trace": [USABLE_STEP]},
            {
                "golden_file": "b",
                "generated_sql": "SELECT 1",
                "trace": {"tool_calls": [USABLE_STEP]},
            },
            {
                "golden_file": "c",
                "generated_sql": "SELECT 1",
                "trace": {"tool_calls": [UNUSABLE_STEP]},
            },
            {"golden_file": "d", "generated_sql": "SELECT 1"},
            {"golden_file": "e", "error": "timeout"},  # excluded from the ratio
        ]
        assert trace_coverage(rows) == (2, 4)

    def test_coverage_warning_texts(self) -> None:
        assert coverage_warning(4, 4, "skill_path_correctness") is None
        assert coverage_warning(0, 0, "skill_path_correctness") is None
        zero = coverage_warning(0, 3, "skill_path_correctness")
        assert zero is not None and "will not be evaluated" in zero
        partial = coverage_warning(2, 3, "skill_path_correctness")
        assert partial is not None and "2/3" in partial

    def test_messages_contain_unlock_instructions(self) -> None:
        assert "scoring.dimensions" in ne1_no_trace("skill_path_correctness")
        assert "tool_name" in ne2_unusable_trace("skill_path_correctness", 3)


def _mutated_run(tmp_path: Path, *, status_key: bool) -> Path:
    """RUN_A with its skill_path dimension turned into a not_evaluated row —
    via the explicit status key, or via the reason prefix only."""
    data = json.loads(RUN_A_JSON.read_text())
    for res in data["results"]["results"]:
        outer = res["gradingResult"]["componentResults"][0]
        for d in outer["componentResults"]:
            if "skill_path_correctness" in (d.get("namedScores") or {}):
                d["pass"] = False
                d["score"] = 0.0
                d["reason"] = ne1_no_trace("skill_path_correctness")
                if status_key:
                    d["status"] = "not_evaluated"
                else:
                    d.pop("status", None)
    out = tmp_path / "eval_ne.json"
    out.write_text(json.dumps(data))
    return out


def _partially_skipped_run(tmp_path: Path) -> Path:
    """RUN_A with table_alignment turned into a vacuous skip on ONE test
    only; the other 4 tests keep their genuine pass/fail on that dimension
    untouched. The partial-skip case _drop_vacuous_dimensions never covered
    (it only drops a dimension when *every* row is skipped) — exercises
    whether the run-level aggregates (dimension_pass_rates/_dims_by_test)
    exclude just the skipped row rather than folding it in as a free pass."""
    data = json.loads(RUN_A_JSON.read_text())
    results = data["results"]["results"]
    mutated = False
    for res in results:
        outer = res["gradingResult"]["componentResults"][0]
        for d in outer["componentResults"]:
            if "table_alignment" in (d.get("namedScores") or {}) and not mutated:
                d["pass"] = True
                d["score"] = 1.0
                d["reason"] = "skipped: no reference_sql to compare tables against"
                d["status"] = "skipped"
                mutated = True
    assert mutated, "fixture must contain a table_alignment row"
    out = tmp_path / "eval_partial_skip.json"
    out.write_text(json.dumps(data))
    return out


class TestStoreStatus:
    @pytest.mark.parametrize("status_key", [True, False])
    def test_ingest_stores_status(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path, status_key: bool
    ) -> None:
        run_json = _mutated_run(tmp_path, status_key=status_key)
        db = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db) as conn:
            run_id = ingest_run(conn, run_json, eval_sample_config)
            statuses = dict(
                conn.execute(
                    "SELECT dimension, ANY_VALUE(status) FROM dimension_results "
                    "WHERE run_id = ? GROUP BY dimension",
                    [run_id],
                ).fetchall()
            )
        assert statuses["skill_path_correctness"] == "not_evaluated"
        assert statuses["execution"] in ("pass", "fail")

    def test_not_evaluated_excluded_from_aggregates_and_compare(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        run_json = _mutated_run(tmp_path, status_key=True)
        db = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db) as conn:
            run_id = ingest_run(conn, run_json, eval_sample_config)
            aggs = store_queries.dimension_pass_rates(conn, run_id)
            assert "skill_path_correctness" not in {a.dimension for a in aggs}
            dims_by_test = _dims_by_test(conn, run_id)
            for dims in dims_by_test.values():
                assert "skill_path_correctness" not in dims

    def test_skipped_rows_excluded_from_run_level_aggregates(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        """D2 applies to skipped, not just not_evaluated, in the run-level
        aggregates the report and compare/gate read (not just the per-test
        aggregate_results score)."""
        run_json = _partially_skipped_run(tmp_path)
        db = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db) as conn:
            run_id = ingest_run(conn, run_json, eval_sample_config)

            aggs = {
                a.dimension: a for a in store_queries.dimension_pass_rates(conn, run_id)
            }
            ta = aggs["table_alignment"]
            # 5 tests total, 1 skipped: total/passes must reflect only the 4
            # genuinely-evaluated rows, not the free vacuous pass folded in.
            assert ta.total == 4
            assert ta.pass_count == 4

            dims_by_test = _dims_by_test(conn, run_id)
            skipped_key = next(
                k for k in dims_by_test if "daily-cases-filtered" in k[0]
            )
            assert "table_alignment" not in dims_by_test[skipped_key]
            other_key = next(
                k for k in dims_by_test if "daily-cases-filtered" not in k[0]
            )
            assert "table_alignment" in dims_by_test[other_key]

    def test_report_renders_capability_panel(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        run_json = _mutated_run(tmp_path, status_key=True)
        db = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db) as conn:
            run_id = ingest_run(conn, run_json, eval_sample_config)
            html = build_report_html(conn, run_id)
        assert "Capability" in html
        assert "not evaluated" in html
        # NE dims must not appear in the failures section as failed dimensions.
        assert "excluded from scores" in html

    def test_report_has_no_panel_without_ne_rows(
        self, eval_sample_config: BiEvalsConfig
    ) -> None:
        db = eval_sample_config.resolve_path(eval_sample_config.storage.db_path)
        with store_connect(db) as conn:
            run_id = ingest_run(conn, RUN_A_JSON, eval_sample_config)
            html = build_report_html(conn, run_id)
        assert "Capability" not in html


class TestPreflight:
    def _pf_config(self, golden_files: list[str]) -> dict:
        return {"tests": [{"vars": {"golden_file": g}} for g in golden_files]}

    def _jsonl(self, tmp_path: Path, rows: list[dict]) -> str:
        p = tmp_path / "results.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return str(p)

    def test_warns_when_no_traces(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        eval_sample_config.scoring.dimensions = ["execution", "skill_path_correctness"]
        input_file = self._jsonl(
            tmp_path, [{"golden_file": "g1.yaml", "generated_sql": "SELECT 1"}]
        )
        warnings = preflight_capability_warnings(
            eval_sample_config, self._pf_config(["g1.yaml"]), input_file
        )
        assert len(warnings) == 1
        assert "skill_path_correctness" in warnings[0]
        assert "will not be evaluated" in warnings[0]

    def test_silent_with_full_coverage(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        eval_sample_config.scoring.dimensions = ["execution", "skill_path_correctness"]
        input_file = self._jsonl(
            tmp_path,
            [
                {
                    "golden_file": "g1.yaml",
                    "generated_sql": "SELECT 1",
                    "trace": [USABLE_STEP],
                }
            ],
        )
        assert (
            preflight_capability_warnings(
                eval_sample_config, self._pf_config(["g1.yaml"]), input_file
            )
            == []
        )

    def test_silent_when_dimension_disabled(
        self, eval_sample_config: BiEvalsConfig, tmp_path: Path
    ) -> None:
        eval_sample_config.scoring.dimensions = ["execution"]
        input_file = self._jsonl(
            tmp_path, [{"golden_file": "g1.yaml", "generated_sql": "SELECT 1"}]
        )
        assert (
            preflight_capability_warnings(
                eval_sample_config, self._pf_config(["g1.yaml"]), input_file
            )
            == []
        )


class TestGetAssertSkillPathCapability:
    """End-to-end scorer wiring: NE-1/NE-2 short-circuit vs genuine failure."""

    def _setup(self, tmp_path: Path, trace_steps: list[dict] | None) -> Path:
        from textwrap import dedent

        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(
            dedent("""\
                project:
                  name: "Test"
                agent:
                  adapter: anthropic_tool_loop
                  anthropic_tool_loop:
                    model: "m"
                    system_prompt: "p.md"
                database:
                  type: snowflake
                scoring:
                  dimensions:
                    - execution
                    - skill_path_correctness
                  critical_dimensions:
                    - execution
            """)
        )
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        (golden_dir / "test-001.yaml").write_text(
            dedent("""\
                id: test-001
                question: "What is total revenue?"
                reference_sql: "SELECT SUM(val) FROM revenue"
                expected_skill_path:
                  required_skills:
                    - tool: read_skill_file
                      input_contains: "REVENUE.md"
            """)
        )
        trace_dir = tmp_path / "results" / "traces"
        trace_dir.mkdir(parents=True)
        (trace_dir / "golden_test-001_yaml.json").write_text(
            json.dumps(
                {
                    "test_id": "golden/test-001.yaml",
                    "generated_sql": "SELECT SUM(val) FROM revenue",
                    "trace": trace_steps if trace_steps is not None else [],
                }
            )
        )
        return config_file

    def _run(self, config_file: Path) -> dict:
        from unittest.mock import MagicMock, patch

        from bi_evals.db.client import QueryResult
        from bi_evals.scorer.entry import get_assert

        mock_client = MagicMock()
        mock_client.execute.return_value = QueryResult(
            columns=["c"], rows=[[1]], row_count=1
        )
        with patch("bi_evals.scorer.entry.create_db_client", return_value=mock_client):
            return get_assert(
                "output",
                {
                    "config": {"config_path": str(config_file)},
                    "vars": {"golden_file": "golden/test-001.yaml"},
                    "prompt": "Q",
                },
            )

    def _skill_component(self, result: dict) -> dict:
        [comp] = [
            c
            for c in result["componentResults"]
            if "skill_path_correctness" in c["namedScores"]
        ]
        return comp

    def test_absent_trace_is_not_evaluated(self, tmp_path: Path) -> None:
        result = self._run(self._setup(tmp_path, trace_steps=[]))
        comp = self._skill_component(result)
        assert comp["status"] == "not_evaluated"
        assert comp["reason"].startswith("not evaluated:")
        assert "no trace" in comp["reason"]
        # Diagnostic NE dim: the test still passes on the evaluated dims.
        assert result["pass"] is True
        assert "1 not evaluated" in result["reason"]

    def test_unusable_trace_is_not_evaluated(self, tmp_path: Path) -> None:
        result = self._run(
            self._setup(tmp_path, trace_steps=[{"type": "tool_use", "text": "hi"}])
        )
        comp = self._skill_component(result)
        assert comp["status"] == "not_evaluated"
        assert "usable" in comp["reason"]

    def test_usable_trace_wrong_tool_genuinely_fails(self, tmp_path: Path) -> None:
        result = self._run(
            self._setup(
                tmp_path,
                trace_steps=[
                    {
                        "type": "tool_use",
                        "tool_name": "describe_table",
                        "tool_input": {"table": "REVENUE"},
                    }
                ],
            )
        )
        comp = self._skill_component(result)
        assert comp["status"] == "fail"
        assert "Missing skill invocations" in comp["reason"]
