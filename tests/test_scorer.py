"""Tests for bi_evals.scorer — dimensions, sql_utils, and get_assert."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from bi_evals.config import ScoringConfig, ScoringThresholds
from bi_evals.db.client import QueryResult
from bi_evals.golden.model import (
    ExpectedResults,
    ExpectedSkillPath,
    GoldenTest,
    RowComparison,
    SkillStep,
    ValueCheck,
)
from bi_evals.scorer.dimensions import (
    DimensionResult,
    check_column_alignment,
    check_execution,
    check_filter_correctness,
    check_no_hallucinated_columns,
    check_row_completeness,
    check_row_precision,
    check_skill_path_correctness,
    check_table_alignment,
    check_value_accuracy,
)
from bi_evals.scorer.sql_utils import extract_filter_columns, extract_select_columns, extract_tables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _golden(
    required_columns: list[str] | None = None,
    row_comparison: RowComparison | None = None,
    skill_path: ExpectedSkillPath | None = None,
) -> GoldenTest:
    return GoldenTest(
        id="test",
        question="Q",
        expected=ExpectedResults(
            required_columns=required_columns or [],
            row_comparison=row_comparison or RowComparison(),
        ),
        expected_skill_path=skill_path or ExpectedSkillPath(),
    )


def _scoring(
    completeness: float = 0.95,
    precision: float = 0.95,
    tolerance: float = 0.0001,
) -> ScoringConfig:
    return ScoringConfig(thresholds=ScoringThresholds(
        completeness=completeness,
        precision=precision,
        value_tolerance=tolerance,
    ))


def _qr(
    columns: list[str],
    rows: list[dict] | None = None,
    error: str | None = None,
) -> QueryResult:
    rows = rows or []
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), error=error)


# ---------------------------------------------------------------------------
# SQL Utils
# ---------------------------------------------------------------------------

class TestExtractTables:
    def test_simple_select(self) -> None:
        tables = extract_tables("SELECT a FROM my_table")
        assert "MY_TABLE" in tables

    def test_join(self) -> None:
        sql = "SELECT a FROM t1 JOIN t2 ON t1.id = t2.id"
        tables = extract_tables(sql)
        assert tables == {"T1", "T2"}

    def test_fully_qualified(self) -> None:
        sql = "SELECT a FROM db.schema.my_table"
        tables = extract_tables(sql)
        assert "DB.SCHEMA.MY_TABLE" in tables

    def test_cte(self) -> None:
        sql = "WITH cte AS (SELECT a FROM t1) SELECT * FROM cte JOIN t2 ON cte.id = t2.id"
        tables = extract_tables(sql)
        assert "T1" in tables
        assert "T2" in tables

    def test_cte_excluded_from_tables(self) -> None:
        """CTE names like MAX_ACROSS_REGIONS are not real tables."""
        sql = """WITH MAX_ACROSS_REGIONS AS (
            SELECT x FROM db.schema.real_table
        ) SELECT * FROM MAX_ACROSS_REGIONS"""
        tables = extract_tables(sql)
        assert tables == {"DB.SCHEMA.REAL_TABLE"}
        assert "MAX_ACROSS_REGIONS" not in tables

    def test_subquery(self) -> None:
        sql = "SELECT * FROM (SELECT a FROM inner_t) sub"
        tables = extract_tables(sql)
        assert "INNER_T" in tables


class TestExtractSelectColumns:
    def test_simple_column(self) -> None:
        cols = extract_select_columns("SELECT COUNTRY FROM t")
        assert cols == {"COUNTRY"}

    def test_aggregation_unwrapped(self) -> None:
        cols = extract_select_columns("SELECT SUM(DIFFERENCE) AS TOTAL FROM t")
        assert cols == {"DIFFERENCE"}

    def test_multiple_with_alias(self) -> None:
        cols = extract_select_columns("SELECT STATE, MAX(DEATHS) AS TOTAL_DEATHS FROM t GROUP BY STATE")
        assert cols == {"STATE", "DEATHS"}

    def test_cte_intermediate_aliases_excluded(self) -> None:
        """Intermediate aliases (s) defined in inner SELECT should be excluded."""
        sql = "WITH cte AS (SELECT a, SUM(b) AS s FROM t GROUP BY a) SELECT a, s FROM cte"
        cols = extract_select_columns(sql)
        assert "A" in cols
        assert "B" in cols
        assert "S" not in cols

    def test_window_function(self) -> None:
        sql = "SELECT CASES - LAG(CASES) OVER (PARTITION BY STATE ORDER BY DATE) AS DAILY FROM t"
        cols = extract_select_columns(sql)
        assert cols == {"CASES", "STATE", "DATE"}


class TestExtractFilterColumns:
    def test_simple_where(self) -> None:
        sql = "SELECT a FROM t WHERE id = 1"
        filters = extract_filter_columns(sql)
        assert ("ID", "EQ") in filters

    def test_multiple_conditions(self) -> None:
        sql = "SELECT a FROM t WHERE id = 1 AND status != 'active' AND dt >= '2024-01-01'"
        filters = extract_filter_columns(sql)
        assert ("ID", "EQ") in filters
        assert ("STATUS", "NEQ") in filters
        assert ("DT", "GTE") in filters

    def test_no_where(self) -> None:
        sql = "SELECT a FROM t"
        filters = extract_filter_columns(sql)
        assert filters == set()


# ---------------------------------------------------------------------------
# Dimension 1: Execution
# ---------------------------------------------------------------------------

class TestExecution:
    def test_pass(self) -> None:
        r = check_execution(_qr(["A"], [{"A": 1}]))
        assert r.passed
        assert r.name == "execution"

    def test_fail(self) -> None:
        r = check_execution(_qr([], error="syntax error near 'FROM'"))
        assert not r.passed
        assert "syntax error" in r.reason


# ---------------------------------------------------------------------------
# Dimension 2: Table Alignment
# ---------------------------------------------------------------------------

class TestTableAlignment:
    def test_pass(self) -> None:
        r = check_table_alignment(
            "SELECT a FROM t1 JOIN t2 ON t1.id = t2.id",
            "SELECT b FROM t1 JOIN t2 ON t1.id = t2.id",
        )
        assert r.passed

    def test_fail_missing_table(self) -> None:
        r = check_table_alignment(
            "SELECT a FROM t1",
            "SELECT b FROM t1 JOIN t2 ON t1.id = t2.id",
        )
        assert not r.passed
        assert "T2" in r.reason


# ---------------------------------------------------------------------------
# Dimension 3: Column Alignment
# ---------------------------------------------------------------------------

class TestColumnAlignment:
    def test_pass_source_columns(self) -> None:
        r = check_column_alignment(
            "SELECT NAME, SUM(VALUE) AS TOTAL FROM t GROUP BY NAME",
            _golden(required_columns=["NAME", "VALUE"]),
        )
        assert r.passed

    def test_fail_missing_source_column(self) -> None:
        r = check_column_alignment(
            "SELECT NAME FROM t",
            _golden(required_columns=["NAME", "VALUE"]),
        )
        assert not r.passed
        assert "VALUE" in r.reason

    def test_alias_ignored(self) -> None:
        """Alias TOTAL_CASES doesn't matter; source column DIFFERENCE is what counts."""
        r = check_column_alignment(
            "SELECT SUM(DIFFERENCE) AS TOTAL_CASES FROM t",
            _golden(required_columns=["DIFFERENCE"]),
        )
        assert r.passed

    def test_skip_no_required(self) -> None:
        r = check_column_alignment("SELECT A FROM t", _golden())
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Dimension 4: Filter Correctness
# ---------------------------------------------------------------------------

class TestFilterCorrectness:
    def test_pass_matching_filters(self) -> None:
        r = check_filter_correctness(
            "SELECT a FROM t WHERE id = 1 AND dt >= '2024-01-01'",
            "SELECT b FROM t WHERE id = 1 AND dt >= '2024-01-01'",
        )
        assert r.passed

    def test_fail_different_filters(self) -> None:
        r = check_filter_correctness(
            "SELECT a FROM t WHERE id = 1",
            "SELECT b FROM t WHERE id = 1 AND status = 'active'",
        )
        assert not r.passed

    def test_skip_no_where(self) -> None:
        r = check_filter_correctness("SELECT a FROM t", "SELECT b FROM t")
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Dimension 5: Row Completeness
# ---------------------------------------------------------------------------

class TestRowCompleteness:
    def test_pass_all_rows_present(self) -> None:
        ref = _qr(["ID"], [{"ID": 1}, {"ID": 2}, {"ID": 3}])
        gen = _qr(["ID"], [{"ID": 1}, {"ID": 2}, {"ID": 3}])
        rc = RowComparison(enabled=True, key_columns=["ID"])
        r = check_row_completeness(gen, ref, _golden(row_comparison=rc), _scoring())
        assert r.passed

    def test_fail_missing_rows(self) -> None:
        ref = _qr(["ID"], [{"ID": i} for i in range(20)])
        gen = _qr(["ID"], [{"ID": i} for i in range(10)])
        rc = RowComparison(enabled=True, key_columns=["ID"], completeness_threshold=0.95)
        r = check_row_completeness(gen, ref, _golden(row_comparison=rc), _scoring())
        assert not r.passed

    def test_skip_disabled(self) -> None:
        r = check_row_completeness(_qr([]), _qr([]), _golden(), _scoring())
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Dimension 6: Row Precision
# ---------------------------------------------------------------------------

class TestRowPrecision:
    def test_pass_no_extra_rows(self) -> None:
        ref = _qr(["ID"], [{"ID": 1}, {"ID": 2}])
        gen = _qr(["ID"], [{"ID": 1}, {"ID": 2}])
        rc = RowComparison(enabled=True, key_columns=["ID"])
        r = check_row_precision(gen, ref, _golden(row_comparison=rc), _scoring())
        assert r.passed

    def test_fail_extra_rows(self) -> None:
        ref = _qr(["ID"], [{"ID": 1}])
        gen = _qr(["ID"], [{"ID": i} for i in range(20)])
        rc = RowComparison(enabled=True, key_columns=["ID"], precision_threshold=0.95)
        r = check_row_precision(gen, ref, _golden(row_comparison=rc), _scoring())
        assert not r.passed

    def test_skip_disabled(self) -> None:
        r = check_row_precision(_qr([]), _qr([]), _golden(), _scoring())
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Dimension 7: Value Accuracy
# ---------------------------------------------------------------------------

class TestValueAccuracy:
    def test_pass_exact_match(self) -> None:
        ref = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 100.0}])
        gen = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 100.0}])
        rc = RowComparison(enabled=True, key_columns=["ID"], value_columns=["VAL"])
        r = check_value_accuracy(gen, ref, _golden(row_comparison=rc), _scoring())
        assert r.passed

    def test_fail_value_mismatch(self) -> None:
        ref = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 100.0}])
        gen = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 200.0}])
        rc = RowComparison(enabled=True, key_columns=["ID"], value_columns=["VAL"])
        r = check_value_accuracy(gen, ref, _golden(row_comparison=rc), _scoring())
        assert not r.passed

    def test_pass_within_tolerance(self) -> None:
        ref = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 100.0}])
        gen = _qr(["ID", "VAL"], [{"ID": 1, "VAL": 100.005}])
        rc = RowComparison(
            enabled=True, key_columns=["ID"], value_columns=["VAL"],
            value_tolerance=0.001,
        )
        r = check_value_accuracy(gen, ref, _golden(row_comparison=rc), _scoring())
        assert r.passed

    def test_pass_positional_column_mapping(self) -> None:
        """Different column aliases should still match by position."""
        ref = _qr(
            ["COUNTRY_REGION", "TOTAL_CASES"],
            [{"COUNTRY_REGION": "US", "TOTAL_CASES": 100.0}],
        )
        gen = _qr(
            ["COUNTRY_REGION", "TOTAL_CONFIRMED_CASES"],
            [{"COUNTRY_REGION": "US", "TOTAL_CONFIRMED_CASES": 100.0}],
        )
        rc = RowComparison(enabled=True, key_columns=["COUNTRY_REGION"])
        r = check_value_accuracy(gen, ref, _golden(row_comparison=rc), _scoring())
        assert r.passed

    def test_fail_positional_column_value_differs(self) -> None:
        """Positional mapping should still catch real value mismatches."""
        ref = _qr(
            ["COUNTRY_REGION", "TOTAL_CASES"],
            [{"COUNTRY_REGION": "US", "TOTAL_CASES": 100.0}],
        )
        gen = _qr(
            ["COUNTRY_REGION", "TOTAL_CONFIRMED_CASES"],
            [{"COUNTRY_REGION": "US", "TOTAL_CONFIRMED_CASES": 999.0}],
        )
        rc = RowComparison(enabled=True, key_columns=["COUNTRY_REGION"])
        r = check_value_accuracy(gen, ref, _golden(row_comparison=rc), _scoring())
        assert not r.passed

    def test_skip_disabled(self) -> None:
        r = check_value_accuracy(_qr([]), _qr([]), _golden(), _scoring())
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Dimension 8: No Hallucinated Columns
# ---------------------------------------------------------------------------

class TestNoHallucinatedColumns:
    def test_pass_same_source_columns(self) -> None:
        r = check_no_hallucinated_columns(
            "SELECT A, SUM(B) AS TOTAL FROM t GROUP BY A",
            "SELECT A, SUM(B) AS DIFFERENT_ALIAS FROM t GROUP BY A",
        )
        assert r.passed

    def test_pass_subset(self) -> None:
        r = check_no_hallucinated_columns(
            "SELECT A FROM t",
            "SELECT A, B FROM t",
        )
        assert r.passed

    def test_fail_extra_source_column(self) -> None:
        r = check_no_hallucinated_columns(
            "SELECT A, B, PHANTOM FROM t",
            "SELECT A, B FROM t",
        )
        assert not r.passed
        assert "PHANTOM" in r.reason

    def test_alias_difference_passes(self) -> None:
        """Different aliases for the same source column should pass."""
        r = check_no_hallucinated_columns(
            "SELECT SUM(DIFFERENCE) AS NEW_CONFIRMED_CASES FROM t",
            "SELECT SUM(DIFFERENCE) AS DAILY_NEW_CASES FROM t",
        )
        assert r.passed


# ---------------------------------------------------------------------------
# Dimension 9: Skill Path Correctness
# ---------------------------------------------------------------------------

class TestSkillPathCorrectness:
    def test_pass_correct_sequence(self) -> None:
        trace = [
            {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}},
            {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "REVENUE.md"}},
        ]
        esp = ExpectedSkillPath(
            required_skills=[
                SkillStep(tool="read_skill_file", input_contains="SKILL.md"),
                SkillStep(tool="read_skill_file", input_contains="REVENUE.md"),
            ],
            sequence_matters=True,
        )
        r = check_skill_path_correctness(trace, _golden(skill_path=esp))
        assert r.passed

    def test_fail_missing_skill(self) -> None:
        trace = [
            {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}},
        ]
        esp = ExpectedSkillPath(
            required_skills=[
                SkillStep(tool="read_skill_file", input_contains="SKILL.md"),
                SkillStep(tool="read_skill_file", input_contains="REVENUE.md"),
            ],
        )
        r = check_skill_path_correctness(trace, _golden(skill_path=esp))
        assert not r.passed
        assert "REVENUE.md" in r.reason

    def test_fail_wrong_order(self) -> None:
        trace = [
            {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "REVENUE.md"}},
            {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}},
        ]
        esp = ExpectedSkillPath(
            required_skills=[
                SkillStep(tool="read_skill_file", input_contains="SKILL.md"),
                SkillStep(tool="read_skill_file", input_contains="REVENUE.md"),
            ],
            sequence_matters=True,
        )
        r = check_skill_path_correctness(trace, _golden(skill_path=esp))
        assert not r.passed
        assert "order" in r.reason.lower()

    def test_skip_no_required_skills(self) -> None:
        r = check_skill_path_correctness([], _golden())
        assert r.passed
        assert "skipped" in r.reason


# ---------------------------------------------------------------------------
# Scorer entry point: get_assert
# ---------------------------------------------------------------------------

class TestGetAssert:
    @pytest.fixture()
    def setup_env(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Set up config, golden test, and trace files in tmp_path."""
        # Config
        config_content = dedent("""\
            project:
              name: "Test"
            agent:
              model: "claude-sonnet-4-5-20250929"
              system_prompt: "prompts/system.md"
            database:
              type: snowflake
            scoring:
              dimensions:
                - execution
                - table_alignment
                - skill_path_correctness
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        # Golden test
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        golden_content = dedent("""\
            id: test-001
            question: "What is total revenue?"
            reference_sql: "SELECT SUM(val) FROM revenue"
            expected:
              required_columns:
                - TOTAL
        """)
        golden_file = golden_dir / "test-001.yaml"
        golden_file.write_text(golden_content)

        # Trace
        trace_dir = tmp_path / "results" / "traces"
        trace_dir.mkdir(parents=True)
        trace_data = {
            "test_id": "golden/test-001.yaml",
            "generated_sql": "SELECT SUM(val) FROM revenue",
            "trace": [
                {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}},
            ],
            "files_read": ["SKILL.md"],
        }
        trace_file = trace_dir / "golden_test-001_yaml.json"
        trace_file.write_text(json.dumps(trace_data))

        return config_file, golden_file, trace_file

    @patch("bi_evals.scorer.entry.create_db_client")
    def test_returns_per_dimension_results(
        self, mock_create: MagicMock, setup_env: tuple[Path, Path, Path],
    ) -> None:
        config_file, golden_file, _ = setup_env

        mock_client = MagicMock()
        mock_client.execute.return_value = QueryResult(
            columns=["TOTAL"], rows=[{"TOTAL": 100}], row_count=1,
        )
        mock_create.return_value = mock_client

        from bi_evals.scorer.entry import get_assert

        results = get_assert("output text", {
            "config": {"config_path": str(config_file)},
            "vars": {"golden_file": "golden/test-001.yaml"},
            "prompt": "What is total revenue?",
        })

        assert isinstance(results, dict)
        assert "componentResults" in results
        assert len(results["componentResults"]) == 3
        assert results["pass"] is True
        assert results["score"] == 1.0
        mock_client.close.assert_called_once()

    @patch("bi_evals.scorer.entry.create_db_client")
    def test_execution_failure_cascades(
        self, mock_create: MagicMock, setup_env: tuple[Path, Path, Path],
    ) -> None:
        config_file, _, _ = setup_env

        mock_client = MagicMock()
        mock_client.execute.return_value = QueryResult(
            columns=[], rows=[], row_count=0, error="SQL error",
        )
        mock_create.return_value = mock_client

        # Use all dimensions to test cascading
        from bi_evals.config import BiEvalsConfig
        config = BiEvalsConfig.load(config_file)

        from bi_evals.scorer.entry import get_assert

        results = get_assert("output", {
            "config": {"config_path": str(config_file)},
            "vars": {"golden_file": "golden/test-001.yaml"},
            "prompt": "Q",
        })

        assert isinstance(results, dict)
        assert results["pass"] is False
        execution_component = results["componentResults"][0]
        assert execution_component["pass"] is False

    def test_missing_golden_file(self, tmp_path: Path) -> None:
        config_content = dedent("""\
            project:
              name: "Test"
            agent:
              model: "test"
              system_prompt: "p.md"
            database:
              type: snowflake
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        from bi_evals.scorer.entry import get_assert

        results = get_assert("output", {
            "config": {"config_path": str(config_file)},
            "vars": {"golden_file": "nonexistent.yaml"},
        })
        assert isinstance(results, dict)
        assert results["pass"] is False
        assert "not found" in results["reason"]

    def test_no_golden_file_var(self, tmp_path: Path) -> None:
        config_content = dedent("""\
            project:
              name: "Test"
            agent:
              model: "test"
              system_prompt: "p.md"
            database:
              type: snowflake
        """)
        config_file = tmp_path / "bi-evals.yaml"
        config_file.write_text(config_content)

        from bi_evals.scorer.entry import get_assert

        results = get_assert("output", {
            "config": {"config_path": str(config_file)},
            "vars": {},
        })
        assert isinstance(results, dict)
        assert results["pass"] is False
        assert "golden_file" in results["reason"]
