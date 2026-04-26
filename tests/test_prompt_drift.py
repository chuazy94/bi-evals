"""Tests for Phase 6b prompt drift: hashing at ingest, prompt_diff query, compare annotations."""

from __future__ import annotations

import json
from pathlib import Path

from bi_evals.config import BiEvalsConfig
from bi_evals.store import connect
from bi_evals.store import queries as q
from bi_evals.store.ingest import _build_prompt_snapshot, ingest_run

from tests.conftest import EVAL_SAMPLE_DIR, RUN_B_JSON


def _make_skill_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("# v1 instructions")
    (skills / "KNOWLEDGE.md").write_text("# v1 knowledge")
    return skills


def _trial_with_files(files: list[str]) -> dict:
    return {"metadata": {"files_read": files}}


# --- _build_prompt_snapshot --------------------------------------------------


def test_snapshot_empty_when_no_files_read(eval_sample_config: BiEvalsConfig) -> None:
    assert _build_prompt_snapshot([], eval_sample_config) is None
    assert _build_prompt_snapshot([{"metadata": {}}], eval_sample_config) is None


def test_snapshot_dedupes_files_across_trials(tmp_path: Path) -> None:
    skills = _make_skill_dir(tmp_path)
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg._base_dir = tmp_path

    trials = [
        _trial_with_files([str(skills / "SKILL.md"), str(skills / "KNOWLEDGE.md")]),
        _trial_with_files([str(skills / "SKILL.md")]),  # duplicate
    ]
    snap = json.loads(_build_prompt_snapshot(trials, cfg))
    # Two unique files, both with hash + size + mtime.
    assert set(snap.keys()) == {"skills/SKILL.md", "skills/KNOWLEDGE.md"}
    for entry in snap.values():
        assert entry["sha256"] is not None
        assert entry["size"] > 0


def test_snapshot_records_deleted_file_with_null_hash(tmp_path: Path) -> None:
    skills = _make_skill_dir(tmp_path)
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg._base_dir = tmp_path

    missing = str(skills / "DELETED.md")
    trials = [_trial_with_files([missing])]
    snap = json.loads(_build_prompt_snapshot(trials, cfg))
    assert snap["skills/DELETED.md"]["sha256"] is None


def test_snapshot_resolves_files_via_file_reader_base_dir(tmp_path: Path) -> None:
    """file_reader base_dir is where the agent actually read from — try that first."""
    # Layout: tmp_path/skills/covid/SKILL.md. Tool's base_dir is "skills/covid".
    # Agent reports files_read=["SKILL.md"] (relative to tool base, not project root).
    skill_dir = tmp_path / "skills" / "covid"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# instructions")

    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg._base_dir = tmp_path
    # Replace tools so only one file_reader exists, pointing at the skill dir.
    from bi_evals.config import ToolConfig
    cfg.agent.tools = [ToolConfig(name="file_reader", type="file_reader", config={"base_dir": "skills/covid"})]

    trials = [_trial_with_files(["SKILL.md"])]  # bare relative path, like the real agent
    snap = json.loads(_build_prompt_snapshot(trials, cfg))

    # Hash should populate (not None), proving we found the file via the tool's base_dir.
    assert snap["skills/covid/SKILL.md"]["sha256"] is not None
    assert snap["skills/covid/SKILL.md"]["size"] > 0


def test_snapshot_uses_relative_paths(tmp_path: Path) -> None:
    """Moving the project on disk should not look like total drift."""
    skills = _make_skill_dir(tmp_path)
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg._base_dir = tmp_path

    trials = [_trial_with_files([str(skills / "SKILL.md")])]
    snap = json.loads(_build_prompt_snapshot(trials, cfg))
    # Path is relative to base_dir, not absolute.
    key = next(iter(snap.keys()))
    assert not Path(key).is_absolute()
    assert key.endswith("SKILL.md")


# --- prompt_diff -------------------------------------------------------------


def test_prompt_diff_empty_when_no_snapshots(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, RUN_B_JSON, eval_sample_config)
        d = q.prompt_diff(conn, run_id, run_id)
    # No prompt_snapshot in fixture → all empty lists.
    assert d.added == d.removed == d.modified == []


def test_prompt_diff_empty_when_one_side_predates_6b() -> None:
    """A pre-6b run (NULL snapshot) vs a 6b run shouldn't report every file as added."""
    import duckdb

    conn = duckdb.connect(":memory:")
    from bi_evals.store.schema import ensure_schema
    ensure_schema(conn)

    snap_b = json.dumps({"skills/SKILL.md": {"sha256": "aaa", "size": 10, "mtime": 1}})

    # Run A: pre-6b — no prompt_snapshot.
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count)
        VALUES ('run-a', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0)
        """
    )
    # Run B: 6b — has snapshot.
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count, prompt_snapshot)
        VALUES ('run-b', 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0, ?)
        """,
        [snap_b],
    )
    d = q.prompt_diff(conn, "run-a", "run-b")
    assert d.added == d.removed == d.modified == d.unchanged == []


def test_prompt_diff_buckets_changes_correctly() -> None:
    """Unit-test the bucketing on synthetic snapshots without an ingest cycle."""
    import duckdb

    conn = duckdb.connect(":memory:")
    from bi_evals.store.schema import ensure_schema
    ensure_schema(conn)

    snap_a = json.dumps({
        "skills/SKILL.md": {"sha256": "aaa", "size": 10, "mtime": 1},
        "skills/UNCHANGED.md": {"sha256": "ccc", "size": 5, "mtime": 1},
        "skills/REMOVED.md": {"sha256": "ddd", "size": 7, "mtime": 1},
    })
    snap_b = json.dumps({
        "skills/SKILL.md": {"sha256": "bbb", "size": 11, "mtime": 2},  # modified
        "skills/UNCHANGED.md": {"sha256": "ccc", "size": 5, "mtime": 1},
        "skills/ADDED.md": {"sha256": "eee", "size": 4, "mtime": 2},  # added
    })

    for rid, snap in (("run-a", snap_a), ("run-b", snap_b)):
        conn.execute(
            """
            INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
                eval_json_path, test_count, pass_count, fail_count, error_count, prompt_snapshot)
            VALUES (?, 'p', '2026-04-25', '{}', '/p', 0, 0, 0, 0, ?)
            """,
            [rid, snap],
        )

    d = q.prompt_diff(conn, "run-a", "run-b")
    assert d.added == ["skills/ADDED.md"]
    assert d.removed == ["skills/REMOVED.md"]
    assert d.modified == ["skills/SKILL.md"]
    assert d.unchanged == ["skills/UNCHANGED.md"]


# --- compare regression annotations -----------------------------------------


def test_compare_annotates_regressed_test_with_changed_file(tmp_path: Path) -> None:
    """A regressed test that read a modified file should be annotated."""
    import duckdb
    from bi_evals.report.builder import build_compare_html
    from bi_evals.store.schema import ensure_schema

    conn = duckdb.connect(":memory:")
    ensure_schema(conn)

    snap_a = json.dumps({"skills/SKILL.md": {"sha256": "aaa", "size": 10, "mtime": 1}})
    snap_b = json.dumps({"skills/SKILL.md": {"sha256": "bbb", "size": 11, "mtime": 2}})

    for rid, snap, ts in (
        ("a", snap_a, "2026-04-20T00:00:00"),
        ("b", snap_b, "2026-04-25T00:00:00"),
    ):
        conn.execute(
            """
            INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
                eval_json_path, test_count, pass_count, fail_count, error_count, prompt_snapshot)
            VALUES (?, 'p', ?, '{}', '/p', 1, 0, 1, 0, ?)
            """,
            [rid, ts, snap],
        )

    # Same test, regressed (passed in A, failed in B), reading SKILL.md.
    files_json = json.dumps(["skills/SKILL.md"])
    for rid, passed, score, rate in (("a", True, 1.0, 1.0), ("b", False, 0.0, 0.0)):
        conn.execute(
            """
            INSERT INTO test_results (
                run_id, test_id, model, golden_id, category, difficulty, tags,
                question, description, reference_sql, generated_sql, files_read,
                trace_file_path, trace_json, passed, score, fail_reason,
                cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                provider, trial_count, pass_count, pass_rate, score_mean, score_stddev
            ) VALUES (?, 't', '', 't', 'cat', 'easy', '[]',
                      'q', 'd', '', '', ?, NULL, NULL, ?, ?, NULL,
                      0.0, 0, 0, 0, 0, '', 1, ?, ?, ?, 0.0)
            """,
            [rid, files_json, passed, score, 1 if passed else 0, rate, score],
        )

    html = build_compare_html(conn, "a", "b")
    assert "Prompt changes" in html
    assert "skills/SKILL.md" in html
    # The regressed row should carry the changed-file annotation.
    assert "changed file(s)" in html
