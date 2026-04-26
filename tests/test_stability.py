"""Tests for cross-run outcome stability (flakiness detection)."""

from __future__ import annotations

from bi_evals.store.queries import _compute_stability


def test_empty_history_is_zero() -> None:
    s = _compute_stability("t", [])
    assert s.runs_observed == 0
    assert s.flip_count == 0
    assert s.current_streak == 0
    assert s.pass_rate_overall == 0.0


def test_all_pass_no_flips() -> None:
    s = _compute_stability("t", [True, True, True, True])
    assert s.flip_count == 0
    assert s.longest_pass_streak == 4
    assert s.longest_fail_streak == 0
    assert s.current_streak == 4
    assert s.pass_rate_overall == 1.0


def test_all_fail_no_flips() -> None:
    s = _compute_stability("t", [False, False, False])
    assert s.flip_count == 0
    assert s.longest_fail_streak == 3
    assert s.current_streak == -3
    assert s.pass_rate_overall == 0.0


def test_alternating_pattern_max_flips() -> None:
    # 5 outcomes → 4 possible transitions, all different
    s = _compute_stability("t", [True, False, True, False, True])
    assert s.flip_count == 4
    assert s.longest_pass_streak == 1
    assert s.longest_fail_streak == 1
    assert s.current_streak == 1  # ends on pass
    assert s.pass_rate_overall == 0.6


def test_single_flip_at_end() -> None:
    s = _compute_stability("t", [True, True, True, False])
    assert s.flip_count == 1
    assert s.longest_pass_streak == 3
    assert s.current_streak == -1  # most recent is one fail


def test_long_streak_tracking() -> None:
    # pass pass fail fail fail pass pass pass pass
    outcomes = [True, True, False, False, False, True, True, True, True]
    s = _compute_stability("t", outcomes)
    assert s.longest_pass_streak == 4
    assert s.longest_fail_streak == 3
    assert s.flip_count == 2
    assert s.current_streak == 4


def test_pass_rate_partial() -> None:
    s = _compute_stability("t", [True, False, True, True])
    assert s.pass_rate_overall == 0.75


def test_single_observation_is_not_flaky() -> None:
    s = _compute_stability("t", [True])
    assert s.runs_observed == 1
    assert s.flip_count == 0
    assert s.current_streak == 1
