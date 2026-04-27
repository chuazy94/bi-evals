"""Tests for Phase 6b dataset staleness + Phase 6d knowledge-file staleness."""

from __future__ import annotations

import json
import os
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


# --- Phase 6d: knowledge-file staleness -------------------------------------


def _seed_run_with_snapshot(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    snapshot: dict[str, dict],
) -> None:
    """Insert a run row whose prompt_snapshot lists the given files."""
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count,
            prompt_snapshot)
        VALUES (?, 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0, ?)
        """,
        [run_id, json.dumps(snapshot)],
    )


def _set_mtime(path: Path, days_ago: int) -> None:
    """Backdate a file's mtime by ``days_ago`` days."""
    target_ts = (date.today() - timedelta(days=days_ago))
    epoch = (target_ts - date(1970, 1, 1)).total_seconds()
    os.utime(path, (epoch, epoch))


def test_stale_knowledge_disabled_when_threshold_zero(tmp_path: Path) -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    f = tmp_path / "SKILL.md"
    f.write_text("hi")
    _set_mtime(f, days_ago=365)
    _seed_run_with_snapshot(conn, "r", {"SKILL.md": {"sha256": "abc"}})
    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=0
    )
    assert out == []


def test_stale_knowledge_returns_empty_when_no_snapshot(tmp_path: Path) -> None:
    """A pre-6b run (NULL prompt_snapshot) yields no warnings — nothing to check."""
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count)
        VALUES ('r', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0)
        """
    )
    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90
    )
    assert out == []


def test_stale_knowledge_flags_old_file(tmp_path: Path) -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    f = tmp_path / "OLD.md"
    f.write_text("contents")
    _set_mtime(f, days_ago=200)
    _seed_run_with_snapshot(conn, "r", {"OLD.md": {"sha256": "abc"}})

    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90,
    )
    assert len(out) == 1
    assert out[0].path == "OLD.md"
    assert out[0].days_since_modified >= 200


def test_stale_knowledge_skips_fresh_file(tmp_path: Path) -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    f = tmp_path / "FRESH.md"
    f.write_text("contents")
    _set_mtime(f, days_ago=10)
    _seed_run_with_snapshot(conn, "r", {"FRESH.md": {"sha256": "abc"}})

    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90,
    )
    assert out == []


def test_stale_knowledge_only_includes_files_that_were_read(tmp_path: Path) -> None:
    """A stale file NOT in the run's prompt_snapshot must NOT be flagged."""
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    read = tmp_path / "READ.md"
    read.write_text("x")
    _set_mtime(read, days_ago=200)
    unread = tmp_path / "UNREAD.md"
    unread.write_text("x")
    _set_mtime(unread, days_ago=200)
    # Only READ.md is in the snapshot.
    _seed_run_with_snapshot(conn, "r", {"READ.md": {"sha256": "abc"}})

    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90,
    )
    paths = {f.path for f in out}
    assert "READ.md" in paths
    assert "UNREAD.md" not in paths


def test_stale_knowledge_skips_missing_file(tmp_path: Path) -> None:
    """A snapshot entry whose file no longer exists is silently skipped.

    The prompt_diff already surfaces deletions; this card is about *current*
    files that have gone stale.
    """
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    _seed_run_with_snapshot(conn, "r", {"GONE.md": {"sha256": "abc"}})

    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90,
    )
    assert out == []


def test_stale_knowledge_sorted_oldest_first(tmp_path: Path) -> None:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    older = tmp_path / "OLDER.md"
    older.write_text("x")
    _set_mtime(older, days_ago=300)
    old = tmp_path / "OLD.md"
    old.write_text("x")
    _set_mtime(old, days_ago=150)
    _seed_run_with_snapshot(conn, "r", {
        "OLD.md": {"sha256": "a"},
        "OLDER.md": {"sha256": "b"},
    })

    out = q.stale_knowledge_files(
        conn, "r", base_dir=tmp_path, stale_after_days=90,
    )
    # Both stale; OLDER.md must come first.
    assert [f.path for f in out] == ["OLDER.md", "OLD.md"]
