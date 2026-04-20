"""Pure-logic tests for regression classification."""

from __future__ import annotations

from bi_evals.compare.diff import (
    Verdict,
    bucket_counts,
    category_deltas,
    classify_pairs,
    compute_verdict,
    dimension_deltas,
)
from bi_evals.store.queries import RunTestPair


CRITICAL = {"execution", "row_completeness", "value_accuracy"}


def _pair(
    test_id: str,
    *,
    a_pass: bool | None,
    b_pass: bool | None,
    a_score: float | None = None,
    b_score: float | None = None,
    a_dims: dict[str, bool] | None = None,
    b_dims: dict[str, bool] | None = None,
    category: str = "cat",
) -> RunTestPair:
    return RunTestPair(
        test_id=test_id,
        category=category,
        a_passed=a_pass,
        b_passed=b_pass,
        a_score=a_score,
        b_score=b_score,
        a_dims=a_dims or {},
        b_dims=b_dims or {},
    )


def test_overall_pass_to_fail_is_regression() -> None:
    pair = _pair("t", a_pass=True, b_pass=False, a_score=1.0, b_score=0.4)
    [c] = classify_pairs([pair], CRITICAL)
    assert c.bucket == "regressed"
    assert c.score_delta == -0.6


def test_overall_fail_to_pass_is_fixed() -> None:
    pair = _pair("t", a_pass=False, b_pass=True, a_score=0.3, b_score=1.0)
    [c] = classify_pairs([pair], CRITICAL)
    assert c.bucket == "fixed"


def test_critical_dim_flip_with_overall_pass_counts_as_regressed() -> None:
    """Defensive path — tiered scoring usually prevents this, but we stay safe."""
    pair = _pair(
        "t",
        a_pass=True,
        b_pass=True,
        a_score=1.0,
        b_score=1.0,
        a_dims={"row_completeness": True, "execution": True},
        b_dims={"row_completeness": False, "execution": True},
    )
    [c] = classify_pairs([pair], CRITICAL)
    assert c.bucket == "regressed"
    assert "row_completeness" in c.regressed_dims


def test_non_critical_dim_flip_is_not_regression() -> None:
    pair = _pair(
        "t",
        a_pass=True,
        b_pass=True,
        a_score=1.0,
        b_score=1.0,
        a_dims={"table_alignment": True},
        b_dims={"table_alignment": False},
    )
    [c] = classify_pairs([pair], CRITICAL)
    assert c.bucket == "unchanged_pass"
    assert c.regressed_dims == []


def test_added_and_removed_buckets() -> None:
    added = _pair("new", a_pass=None, b_pass=True, b_score=1.0)
    removed = _pair("old", a_pass=True, b_pass=None, a_score=0.9)
    [c_add, c_rem] = classify_pairs([added, removed], CRITICAL)
    assert c_add.bucket == "added"
    assert c_rem.bucket == "removed"


def test_verdict_red_when_any_regression() -> None:
    pairs = [
        _pair("t1", a_pass=True, b_pass=False, a_score=1.0, b_score=0.2),
        _pair("t2", a_pass=True, b_pass=True, a_score=1.0, b_score=1.0),
    ]
    cls = classify_pairs(pairs, CRITICAL)
    assert compute_verdict(cls) == Verdict.RED


def test_verdict_amber_when_fixes_but_no_regressions() -> None:
    pairs = [
        _pair("t1", a_pass=False, b_pass=True, a_score=0.3, b_score=1.0),
        _pair("t2", a_pass=True, b_pass=True, a_score=1.0, b_score=1.0),
    ]
    assert compute_verdict(classify_pairs(pairs, CRITICAL)) == Verdict.AMBER


def test_verdict_green_when_fully_stable() -> None:
    pairs = [
        _pair("t1", a_pass=True, b_pass=True, a_score=1.0, b_score=1.0),
        _pair("t2", a_pass=False, b_pass=False, a_score=0.3, b_score=0.3),
    ]
    assert compute_verdict(classify_pairs(pairs, CRITICAL)) == Verdict.GREEN


def test_verdict_amber_for_added_test_alone() -> None:
    pairs = [_pair("new", a_pass=None, b_pass=True, b_score=1.0)]
    assert compute_verdict(classify_pairs(pairs, CRITICAL)) == Verdict.AMBER


def test_bucket_counts_covers_all_buckets() -> None:
    pairs = [_pair("t", a_pass=True, b_pass=True, a_score=1.0, b_score=1.0)]
    counts = bucket_counts(classify_pairs(pairs, CRITICAL))
    assert set(counts.keys()) == {
        "regressed", "fixed", "unchanged_pass", "unchanged_fail", "added", "removed"
    }
    assert counts["unchanged_pass"] == 1


def test_category_deltas_per_category() -> None:
    pairs = [
        _pair("a1", a_pass=True, b_pass=True, a_score=1.0, b_score=1.0, category="x"),
        _pair("a2", a_pass=True, b_pass=False, a_score=1.0, b_score=0.0, category="x"),
        _pair("b1", a_pass=False, b_pass=True, a_score=0.0, b_score=1.0, category="y"),
    ]
    cls = classify_pairs(pairs, CRITICAL)
    deltas = {d.category: d for d in category_deltas(cls)}
    assert deltas["x"].pass_rate_delta == -0.5  # 1.0 -> 0.5
    assert deltas["y"].pass_rate_delta == 1.0


def test_dimension_deltas_sorted_worst_first() -> None:
    pairs = [
        _pair(
            "t1",
            a_pass=True, b_pass=True, a_score=1.0, b_score=1.0,
            a_dims={"d1": True, "d2": True},
            b_dims={"d1": False, "d2": True},
        ),
    ]
    cls = classify_pairs(pairs, CRITICAL)
    dims = dimension_deltas(cls)
    assert dims[0].dimension == "d1"  # biggest regression first
    assert dims[0].pass_rate_delta == -1.0
