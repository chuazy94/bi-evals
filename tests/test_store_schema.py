"""Tests for DuckDB schema creation."""

from pathlib import Path

from bi_evals.store import connect
from bi_evals.store.schema import ensure_schema


def test_schema_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
        names = [r[0] for r in rows]
    assert names == ["dimension_results", "runs", "test_results"]


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ensure_schema(conn)
        ensure_schema(conn)
        (count,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert count == 0


def test_reopen_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_name, timestamp, config_snapshot, "
            "eval_json_path, test_count, pass_count, fail_count, error_count) "
            "VALUES ('r1', 'p', '2026-01-01', '{}', '/p', 0, 0, 0, 0)"
        )
    with connect(db) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert count == 1
