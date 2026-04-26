"""Tests for DuckDB schema creation."""

from pathlib import Path

import duckdb

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
    assert names == ["dimension_results", "runs", "test_results", "trial_results"]


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


def test_legacy_pk_is_rebuilt_to_include_model_and_trial_ix(tmp_path: Path) -> None:
    """Pre-6a DBs had narrower PKs that broke ingest of multi-model/multi-trial runs."""
    db = tmp_path / "legacy.duckdb"
    legacy = duckdb.connect(str(db))
    # Mirror the real pre-6a shape: all original columns present.
    legacy.execute(
        """
        CREATE TABLE runs (
            run_id VARCHAR PRIMARY KEY, project_name VARCHAR NOT NULL,
            timestamp TIMESTAMP NOT NULL, config_snapshot JSON NOT NULL,
            eval_json_path VARCHAR NOT NULL, test_count INTEGER NOT NULL,
            pass_count INTEGER NOT NULL, fail_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL
        );
        CREATE TABLE test_results (
            run_id VARCHAR NOT NULL, test_id VARCHAR NOT NULL,
            golden_id VARCHAR, category VARCHAR, difficulty VARCHAR, tags JSON,
            question TEXT, description TEXT,
            reference_sql TEXT, generated_sql TEXT, files_read JSON,
            trace_file_path VARCHAR, trace_json JSON,
            passed BOOLEAN NOT NULL, score DOUBLE NOT NULL,
            fail_reason TEXT, cost_usd DOUBLE, latency_ms BIGINT,
            prompt_tokens BIGINT, completion_tokens BIGINT, total_tokens BIGINT,
            provider VARCHAR,
            PRIMARY KEY (run_id, test_id)
        );
        CREATE TABLE dimension_results (
            run_id VARCHAR NOT NULL, test_id VARCHAR NOT NULL,
            dimension VARCHAR NOT NULL, passed BOOLEAN NOT NULL,
            score DOUBLE NOT NULL, reason TEXT,
            is_critical BOOLEAN NOT NULL, weight DOUBLE,
            PRIMARY KEY (run_id, test_id, dimension)
        );
        """
    )
    legacy.execute(
        "INSERT INTO test_results (run_id, test_id, passed, score) "
        "VALUES ('r1', 't1', true, 1.0)"
    )
    legacy.execute(
        "INSERT INTO dimension_results "
        "(run_id, test_id, dimension, passed, score, is_critical) "
        "VALUES ('r1', 't1', 'execution', true, 1.0, true)"
    )
    legacy.close()

    with connect(db) as conn:
        # The same (run, test, dim) is now allowed for distinct (model, trial_ix).
        conn.execute(
            "INSERT INTO dimension_results "
            "(run_id, test_id, model, trial_ix, dimension, passed, score, is_critical) "
            "VALUES ('r1', 't1', 'claude', 0, 'execution', true, 1.0, true)"
        )
        conn.execute(
            "INSERT INTO dimension_results "
            "(run_id, test_id, model, trial_ix, dimension, passed, score, is_critical) "
            "VALUES ('r1', 't1', 'claude', 1, 'execution', false, 0.0, true)"
        )
        (n_dim,) = conn.execute("SELECT COUNT(*) FROM dimension_results").fetchone()
        # Legacy row preserved + 2 new rows.
        assert n_dim == 3
        # Legacy row preserved in test_results too.
        (n_tr,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
        assert n_tr == 1
