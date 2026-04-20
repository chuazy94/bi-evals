"""Tests for Promptfoo JSON → DuckDB ingest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bi_evals.config import BiEvalsConfig
from bi_evals.store import connect
from bi_evals.store.ingest import ingest_run

from tests.conftest import EVAL_SAMPLE_DIR, RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON


def test_ingest_populates_three_tables(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, RUN_B_JSON, eval_sample_config)
        assert run_id == RUN_B_ID

        (runs,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        (tests,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
        (dims,) = conn.execute("SELECT COUNT(*) FROM dimension_results").fetchone()

    assert runs == 1
    assert tests == 5
    assert dims == 45  # 9 dims × 5 tests


def test_ingest_unwraps_nested_componentresults(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    """Promptfoo wraps our assertion into one outer componentResult whose nested
    componentResults list is the real 9 per-dimension entries. Confirm we extract
    exactly those 9 per test, not any siblings."""
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        rows = conn.execute(
            "SELECT dimension, COUNT(*) FROM dimension_results GROUP BY dimension ORDER BY dimension"
        ).fetchall()
    # 9 expected dimensions
    expected = {
        "column_alignment", "execution", "filter_correctness",
        "no_hallucinated_columns", "row_completeness", "row_precision",
        "skill_path_correctness", "table_alignment", "value_accuracy",
    }
    got = {r[0] for r in rows}
    assert got == expected
    # Each dimension appears once per test (5 tests)
    for _, count in rows:
        assert count == 5


def test_ingest_snapshots_golden_metadata(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        rows = conn.execute(
            "SELECT test_id, category, reference_sql FROM test_results ORDER BY test_id"
        ).fetchall()
    for test_id, category, ref_sql in rows:
        assert category is not None and category != ""
        assert ref_sql, f"reference_sql missing for {test_id}"


def test_ingest_is_idempotent(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        (runs,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        (tests,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
        (dims,) = conn.execute("SELECT COUNT(*) FROM dimension_results").fetchone()
    assert runs == 1 and tests == 5 and dims == 45


def test_ingest_two_runs_coexist(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_A_JSON, eval_sample_config)
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        (runs,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        (tests,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
    assert runs == 2
    assert tests == 10  # 5 + 5


def test_ingest_marks_critical_dimensions(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        rows = conn.execute(
            "SELECT dimension, BOOL_OR(is_critical) FROM dimension_results GROUP BY dimension"
        ).fetchall()
    critical = {name for name, is_crit in rows if is_crit}
    # Matches DEFAULT_CRITICAL_DIMENSIONS in config.py
    assert critical == {"execution", "row_completeness", "value_accuracy"}


def test_ingest_missing_trace_file_still_succeeds(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Simulate a run JSON that points at a trace path that doesn't exist."""
    # Build a minimal eval JSON in-place with a bogus trace_file path.
    raw = json.loads(RUN_B_JSON.read_text())
    for t in raw["results"]["results"]:
        t["metadata"]["trace_file"] = str(tmp_path / "does-not-exist.json")
    patched = tmp_path / "patched.json"
    patched.write_text(json.dumps(raw))

    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, patched, eval_sample_config)
        nulls = conn.execute(
            "SELECT COUNT(*) FROM test_results WHERE trace_json IS NULL"
        ).fetchone()
    assert nulls[0] == 5
