"""Tests for Phase 6c: anti-pattern goldens and the anti_pattern_compliance dimension."""

from __future__ import annotations

from pathlib import Path

import duckdb

from bi_evals.config import (
    ALL_DIMENSIONS,
    DEFAULT_CRITICAL_DIMENSIONS,
    DEFAULT_DIMENSION_WEIGHTS,
)
from bi_evals.golden.loader import load_golden_test
from bi_evals.golden.model import AntiPatterns, GoldenTest
from bi_evals.report.builder import _drop_vacuous_dimensions
from bi_evals.scorer.dimensions import (
    _check_anti_patterns,
    check_anti_pattern_compliance,
)
from bi_evals.scorer.sql_utils import extract_columns_with_tables
from bi_evals.store import queries as q
from bi_evals.store.schema import ensure_schema


# --- Config wiring ----------------------------------------------------------


def test_anti_pattern_dim_in_default_dimensions() -> None:
    assert "anti_pattern_compliance" in ALL_DIMENSIONS


def test_anti_pattern_dim_not_critical_by_default() -> None:
    """Plan: non-critical by default — a violation that still produced correct
    rows is a warning, not a hard fail."""
    assert "anti_pattern_compliance" not in DEFAULT_CRITICAL_DIMENSIONS


def test_anti_pattern_dim_has_default_weight() -> None:
    assert DEFAULT_DIMENSION_WEIGHTS["anti_pattern_compliance"] == 2.0


# --- Golden YAML round-trip -------------------------------------------------


def test_golden_loads_anti_patterns(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(
        "id: x\nquestion: q?\nreference_sql: SELECT 1\n"
        "anti_patterns:\n"
        "  forbidden_tables: [RAW_ORDERS, LEGACY_REVENUE]\n"
        "  forbidden_columns: [ACCOUNT_INVOICES.amount, gross_revenue]\n"
    )
    g = load_golden_test(p)
    assert g.anti_patterns is not None
    assert g.anti_patterns.forbidden_tables == ["RAW_ORDERS", "LEGACY_REVENUE"]
    assert g.anti_patterns.forbidden_columns == [
        "ACCOUNT_INVOICES.amount", "gross_revenue",
    ]


def test_golden_default_anti_patterns_is_none() -> None:
    g = GoldenTest(id="x", question="q?")
    assert g.anti_patterns is None


# --- extract_columns_with_tables -------------------------------------------


def test_extract_columns_resolves_alias_to_physical_table() -> None:
    pairs = extract_columns_with_tables(
        "SELECT o.amount FROM raw_orders AS o WHERE o.region = 'EU'"
    )
    assert ("RAW_ORDERS", "AMOUNT") in pairs
    assert ("RAW_ORDERS", "REGION") in pairs


def test_extract_columns_unresolved_when_multiple_tables_unqualified() -> None:
    """Bare ``amount`` in a multi-table query → owner unknown (None)."""
    pairs = extract_columns_with_tables(
        "SELECT amount FROM orders, customers"
    )
    # Owner cannot be resolved without a qualifier; should be None.
    assert (None, "AMOUNT") in pairs


def test_extract_columns_resolves_when_single_table() -> None:
    pairs = extract_columns_with_tables(
        "SELECT amount FROM raw_orders"
    )
    assert ("RAW_ORDERS", "AMOUNT") in pairs


def test_extract_columns_cte_alias_collapses_to_none() -> None:
    """A column aliased through a CTE should still be matchable by bare name."""
    sql = """
        WITH bad AS (SELECT amount FROM raw_orders)
        SELECT b.amount FROM bad b
    """
    pairs = extract_columns_with_tables(sql)
    # Inside the CTE, amount resolves to RAW_ORDERS.
    assert ("RAW_ORDERS", "AMOUNT") in pairs
    # The outer reference to ``bad`` collapses to None — laundered, but the
    # bare name AMOUNT is still in the set so a bare forbidden_columns entry
    # would still flag it.
    assert any(c == "AMOUNT" for _, c in pairs)


# --- _check_anti_patterns ---------------------------------------------------


def test_no_violations_when_sql_is_clean() -> None:
    patterns = AntiPatterns(forbidden_tables=["RAW_ORDERS"])
    assert _check_anti_patterns("SELECT * FROM V_UNIFIED_REVENUE", patterns) == []


def test_forbidden_table_flagged() -> None:
    patterns = AntiPatterns(forbidden_tables=["RAW_ORDERS"])
    violations = _check_anti_patterns(
        "SELECT amount FROM raw_orders", patterns
    )
    assert len(violations) == 1
    assert "RAW_ORDERS" in violations[0]


def test_forbidden_table_flagged_via_alias() -> None:
    patterns = AntiPatterns(forbidden_tables=["RAW_ORDERS"])
    violations = _check_anti_patterns(
        "SELECT o.amount FROM raw_orders AS o", patterns
    )
    assert len(violations) == 1


def test_bare_forbidden_table_matches_schema_qualified() -> None:
    """A bare ``RAW_ORDERS`` entry should match ``FINANCE.RAW_ORDERS``."""
    patterns = AntiPatterns(forbidden_tables=["RAW_ORDERS"])
    violations = _check_anti_patterns(
        "SELECT * FROM FINANCE.RAW_ORDERS", patterns
    )
    assert len(violations) == 1


def test_qualified_forbidden_column() -> None:
    patterns = AntiPatterns(
        forbidden_columns=["ACCOUNT_INVOICES.amount"]
    )
    violations = _check_anti_patterns(
        "SELECT a.amount FROM ACCOUNT_INVOICES a", patterns
    )
    assert len(violations) == 1
    assert "ACCOUNT_INVOICES.AMOUNT" in violations[0]


def test_qualified_forbidden_column_misses_other_table() -> None:
    """forbidden_columns: ['FOO.bar'] must NOT flag 'OTHER.bar'."""
    patterns = AntiPatterns(forbidden_columns=["RAW_ORDERS.amount"])
    violations = _check_anti_patterns(
        "SELECT amount FROM V_UNIFIED_REVENUE", patterns
    )
    assert violations == []


def test_bare_forbidden_column_matches_anywhere() -> None:
    patterns = AntiPatterns(forbidden_columns=["gross_revenue"])
    violations = _check_anti_patterns(
        "SELECT gross_revenue FROM revenue_view", patterns
    )
    assert len(violations) == 1


def test_violation_via_cte_launder() -> None:
    """A forbidden column referenced inside a CTE should still be flagged."""
    patterns = AntiPatterns(forbidden_columns=["JHU_COVID_19.cases"])
    sql = """
        WITH wrong AS (SELECT cases FROM JHU_COVID_19)
        SELECT SUM(cases) FROM wrong
    """
    violations = _check_anti_patterns(sql, patterns)
    assert len(violations) == 1


# --- check_anti_pattern_compliance dimension --------------------------------


def test_dim_vacuous_pass_when_no_anti_patterns() -> None:
    g = GoldenTest(id="x", question="q?")
    r = check_anti_pattern_compliance("SELECT * FROM raw_orders", g)
    assert r.passed is True
    assert r.score == 1.0
    assert r.reason.startswith("skipped:")


def test_dim_vacuous_pass_when_lists_empty() -> None:
    g = GoldenTest(
        id="x", question="q?",
        anti_patterns=AntiPatterns(forbidden_tables=[], forbidden_columns=[]),
    )
    r = check_anti_pattern_compliance("SELECT * FROM raw_orders", g)
    assert r.passed is True
    assert r.reason.startswith("skipped:")


def test_dim_fails_with_clear_reason() -> None:
    g = GoldenTest(
        id="x", question="q?",
        anti_patterns=AntiPatterns(forbidden_tables=["RAW_ORDERS"]),
    )
    r = check_anti_pattern_compliance("SELECT amount FROM raw_orders", g)
    assert r.passed is False
    assert r.score == 0.0
    assert "RAW_ORDERS" in r.reason


def test_dim_passes_when_sql_is_clean() -> None:
    g = GoldenTest(
        id="x", question="q?",
        anti_patterns=AntiPatterns(forbidden_tables=["RAW_ORDERS"]),
    )
    r = check_anti_pattern_compliance(
        "SELECT amount FROM V_UNIFIED_REVENUE", g
    )
    assert r.passed is True


# --- Report: option (b) — drop vacuously-passing dimensions -----------------


def _seed_dim_row(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    test_id: str,
    dimension: str,
    *,
    passed: bool,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO dimension_results (
            run_id, test_id, model, trial_ix, dimension,
            passed, score, reason, is_critical, weight
        ) VALUES (?, ?, '', 0, ?, ?, ?, ?, false, 1.0)
        """,
        [run_id, test_id, dimension, passed, 1.0 if passed else 0.0, reason],
    )


def test_report_drops_anti_pattern_dim_when_all_skipped() -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO runs (run_id, project_name, timestamp, config_snapshot, "
        "eval_json_path, test_count, pass_count, fail_count, error_count) "
        "VALUES ('r', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0)"
    )
    # Two tests, both vacuous on anti_pattern_compliance.
    for tid in ("t1", "t2"):
        _seed_dim_row(
            conn, "r", tid, "anti_pattern_compliance",
            passed=True, reason="skipped: no anti-patterns defined",
        )
        _seed_dim_row(
            conn, "r", tid, "execution",
            passed=True, reason="ok",
        )
    dims = q.dimension_pass_rates(conn, "r")
    assert {d.dimension for d in dims} == {"anti_pattern_compliance", "execution"}
    filtered = _drop_vacuous_dimensions(conn, "r", dims)
    assert {d.dimension for d in filtered} == {"execution"}


def test_report_keeps_anti_pattern_dim_when_any_real_run() -> None:
    """If at least one test has a real (non-skipped) run, keep the dimension."""
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO runs (run_id, project_name, timestamp, config_snapshot, "
        "eval_json_path, test_count, pass_count, fail_count, error_count) "
        "VALUES ('r', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0)"
    )
    _seed_dim_row(
        conn, "r", "t1", "anti_pattern_compliance",
        passed=True, reason="skipped: no anti-patterns defined",
    )
    _seed_dim_row(
        conn, "r", "t2", "anti_pattern_compliance",
        passed=True, reason="no forbidden tables/columns used",
    )
    dims = q.dimension_pass_rates(conn, "r")
    filtered = _drop_vacuous_dimensions(conn, "r", dims)
    assert "anti_pattern_compliance" in {d.dimension for d in filtered}


def test_report_keeps_dimension_with_failures_even_if_pass_rate_full() -> None:
    """Sanity: a dim with any non-skipped failure must never be dropped."""
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO runs (run_id, project_name, timestamp, config_snapshot, "
        "eval_json_path, test_count, pass_count, fail_count, error_count) "
        "VALUES ('r', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0)"
    )
    _seed_dim_row(
        conn, "r", "t1", "anti_pattern_compliance",
        passed=False, reason="forbidden table used: RAW_ORDERS",
    )
    dims = q.dimension_pass_rates(conn, "r")
    filtered = _drop_vacuous_dimensions(conn, "r", dims)
    assert "anti_pattern_compliance" in {d.dimension for d in filtered}
