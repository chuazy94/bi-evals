"""Tests for Phase 6b dataset staleness: golden field, ingest snapshot, query helper."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.model import GoldenTest
from bi_evals.golden.loader import load_golden_test
from bi_evals.store import connect
from bi_evals.store import queries as q
from bi_evals.store.schema import ensure_schema

from tests.conftest import EVAL_SAMPLE_DIR, RUN_B_JSON


# --- Golden model -----------------------------------------------------------


def test_golden_model_default_last_verified_is_none() -> None:
    g = GoldenTest(id="x", question="q?")
    assert g.last_verified_at is None


def test_golden_yaml_round_trip_with_date(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(
        "id: x\ncategory: c\nquestion: q?\nreference_sql: SELECT 1\n"
        "last_verified_at: 2025-12-01\n"
    )
    g = load_golden_test(p)
    assert g.last_verified_at == date(2025, 12, 1)


# --- Query helper: stale_goldens --------------------------------------------


def _seed_run_with_dates(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    rows: list[tuple[str, date | None]],
) -> None:
    """Insert a run with one test_result per (test_id, last_verified_at)."""
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count)
        VALUES (?, 'p', '2026-04-25', '{}', '/p', ?, 0, 0, 0)
        """,
        [run_id, len(rows)],
    )
    for tid, last in rows:
        conn.execute(
            """
            INSERT INTO test_results (
                run_id, test_id, model, golden_id, category, difficulty, tags,
                question, description, reference_sql, generated_sql, files_read,
                trace_file_path, trace_json, passed, score, fail_reason,
                cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                provider, trial_count, pass_count, pass_rate, score_mean, score_stddev,
                last_verified_at
            ) VALUES (?, ?, '', ?, 'c', 'easy', '[]', 'q', 'd', '', '', '[]',
                      NULL, NULL, true, 1.0, NULL, 0.0, 0, 0, 0, 0, '', 1, 1, 1.0, 1.0, 0.0, ?)
            """,
            [run_id, tid, tid, last],
        )


def test_stale_goldens_disabled_when_threshold_zero() -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    _seed_run_with_dates(conn, "r", [("t1", date(2020, 1, 1))])
    stale, unverified = q.stale_goldens(conn, "r", stale_after_days=0)
    assert stale == [] and unverified == []


def test_stale_goldens_finds_old_dates() -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    today = date(2026, 4, 25)
    _seed_run_with_dates(conn, "r", [
        ("t-fresh", today - timedelta(days=10)),
        ("t-borderline", today - timedelta(days=180)),
        ("t-stale", today - timedelta(days=365)),
    ])
    stale, unverified = q.stale_goldens(conn, "r", stale_after_days=180, today=today)
    assert {g.test_id for g in stale} == {"t-stale"}
    assert unverified == []


def test_stale_goldens_returns_unverified_separately() -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    today = date(2026, 4, 25)
    _seed_run_with_dates(conn, "r", [
        ("t-no-date", None),
        ("t-fresh", today - timedelta(days=5)),
    ])
    stale, unverified = q.stale_goldens(conn, "r", stale_after_days=180, today=today)
    assert stale == []
    assert [g.test_id for g in unverified] == ["t-no-date"]
    assert unverified[0].days_since_verified is None


def test_stale_goldens_sorted_oldest_first() -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    today = date(2026, 4, 25)
    _seed_run_with_dates(conn, "r", [
        ("t-200d", today - timedelta(days=200)),
        ("t-365d", today - timedelta(days=365)),
        ("t-300d", today - timedelta(days=300)),
    ])
    stale, _ = q.stale_goldens(conn, "r", stale_after_days=180, today=today)
    # Oldest first.
    assert [g.test_id for g in stale] == ["t-365d", "t-300d", "t-200d"]


# --- Ingest: last_verified_at survives the round trip -----------------------


def test_ingest_snapshots_last_verified_at(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Add last_verified_at to one fixture golden, ingest, confirm it lands in DB."""
    # Patch the fixture YAML in-place via a temp copy: re-load the goldens
    # path-resolved by the eval JSON, write a date into one of them.
    target = EVAL_SAMPLE_DIR / "golden" / "cases" / "daily-cases-filtered.yaml"
    original = target.read_text()
    try:
        target.write_text(original.rstrip() + "\nlast_verified_at: 2025-01-15\n")

        from bi_evals.store.ingest import ingest_run
        with connect(tmp_path / "x.duckdb") as conn:
            run_id = ingest_run(conn, RUN_B_JSON, eval_sample_config)
            row = conn.execute(
                """
                SELECT test_id, last_verified_at FROM test_results
                WHERE run_id = ? AND test_id LIKE '%daily-cases-filtered%'
                """,
                [run_id],
            ).fetchone()
        assert row is not None
        assert row[1] == date(2025, 1, 15)
    finally:
        target.write_text(original)
