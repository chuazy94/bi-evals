"""Tests for repeat-run variance: trial aggregation, stddev, rate-based compare."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bi_evals.compare.diff import classify_pairs, compute_verdict, Verdict
from bi_evals.config import BiEvalsConfig
from bi_evals.store import connect
from bi_evals.store import queries as q
from bi_evals.store.ingest import _stddev, ingest_run

from tests.conftest import RUN_B_JSON


def test_stddev_zero_for_single_value() -> None:
    assert _stddev([0.8], 0.8) == 0.0


def test_stddev_zero_for_empty_list() -> None:
    assert _stddev([], 0.0) == 0.0


def test_stddev_nonzero_for_mixed_values() -> None:
    values = [1.0, 0.0, 1.0, 0.0]
    mean = sum(values) / len(values)
    result = _stddev(values, mean)
    assert result == pytest.approx(0.5)


def _make_multi_trial_eval_json(base_json_path: Path, out_path: Path, repeats: int) -> Path:
    """Synthesize a multi-trial eval JSON by duplicating each result entry.

    The first duplicate keeps the original success value; later duplicates
    alternate pass/fail so at least one test has a non-zero stddev.
    """
    raw = json.loads(base_json_path.read_text())
    originals = list(raw["results"]["results"])
    new_results = []
    for idx, t in enumerate(originals):
        for trial in range(repeats):
            copy = json.loads(json.dumps(t))  # deep copy
            # Make trial 0 match original, later trials flip for first test only
            if idx == 0 and trial > 0:
                copy["success"] = (trial % 2 == 0)
                copy["score"] = 1.0 if copy["success"] else 0.0
            new_results.append(copy)
    raw["results"]["results"] = new_results
    out_path.write_text(json.dumps(raw))
    return out_path


def test_single_trial_run_has_zero_variance(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Baseline: pre-6a-shape single-trial ingest yields pass_rate ∈ {0,1} and stddev=0."""
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_B_JSON, eval_sample_config)
        rows = conn.execute(
            "SELECT pass_rate, score_stddev, trial_count FROM test_results"
        ).fetchall()

    assert rows  # non-empty
    for pass_rate, stddev, trial_count in rows:
        assert trial_count == 1
        assert pass_rate in (0.0, 1.0)
        assert stddev == 0.0


def test_multi_trial_ingest_aggregates_across_trials(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_trial_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", repeats=3
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, patched, eval_sample_config)
        (trial_count,) = conn.execute("SELECT COUNT(*) FROM trial_results").fetchone()
        (test_count,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()

    # 5 original tests × 3 trials = 15 trial rows; 5 aggregate rows
    assert trial_count == 15
    assert test_count == 5


def test_multi_trial_produces_fractional_pass_rate(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_trial_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", repeats=3
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, patched, eval_sample_config)
        fractional = conn.execute(
            "SELECT pass_rate FROM test_results WHERE pass_rate NOT IN (0.0, 1.0)"
        ).fetchall()

    # At least the first test should have a mixed outcome across trials.
    assert len(fractional) >= 1
    for (rate,) in fractional:
        assert 0.0 < rate < 1.0


def test_rate_based_regression_with_small_drop_under_threshold(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """A 0.1 drop (e.g., 5/5 → 4.5/5) should NOT regress at default threshold 0.2."""
    from bi_evals.store.queries import RunTestPair

    pair = RunTestPair(
        test_id="t",
        category="cat",
        model="m",
        a_passed=True,
        a_score=1.0,
        a_pass_rate=1.0,
        b_passed=True,
        b_score=0.9,
        b_pass_rate=0.9,
        a_dims={},
        b_dims={},
    )
    [c] = classify_pairs([pair], set(), regression_threshold=0.2)
    assert c.bucket != "regressed"


def test_rate_based_regression_with_large_drop(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """A 0.4 drop clears the 0.2 threshold and is regressed."""
    from bi_evals.store.queries import RunTestPair

    pair = RunTestPair(
        test_id="t",
        category="cat",
        model="m",
        a_passed=True,
        a_score=1.0,
        a_pass_rate=1.0,
        b_passed=False,
        b_score=0.6,
        b_pass_rate=0.6,
        a_dims={},
        b_dims={},
    )
    [c] = classify_pairs([pair], set(), regression_threshold=0.2)
    assert c.bucket == "regressed"
    assert compute_verdict([c]) == Verdict.RED


def test_legacy_boolean_flip_still_regresses_under_default_threshold() -> None:
    """Single-trial runs have rates ∈ {0, 1}; a flip should still clear 0.2."""
    from bi_evals.store.queries import RunTestPair

    pair = RunTestPair(
        test_id="t",
        category="cat",
        model=None,
        a_passed=True,
        a_score=1.0,
        a_pass_rate=1.0,
        b_passed=False,
        b_score=0.0,
        b_pass_rate=0.0,
        a_dims={},
        b_dims={},
    )
    [c] = classify_pairs([pair], set())
    assert c.bucket == "regressed"


def test_multi_trial_test_row_score_mean(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_trial_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", repeats=3
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, patched, eval_sample_config)
        tests = q.list_tests(conn, _run_id_of(patched))

    assert tests
    for t in tests:
        assert 0.0 <= t.score_mean <= 1.0
        assert t.score_stddev >= 0.0


def _run_id_of(eval_json_path: Path) -> str:
    return json.loads(eval_json_path.read_text())["evalId"]
