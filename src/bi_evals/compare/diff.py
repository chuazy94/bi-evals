"""Pure regression-detection logic over run pairs.

A test regresses if either:
  - The overall pass flipped True → False, OR
  - A critical dimension flipped pass → fail (even if the overall pass is still True,
    which shouldn't happen under normal tiered scoring but we stay defensive).

Added/removed tests are reported separately and never flip the verdict red.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from bi_evals.store.queries import RunTestPair


class Verdict(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class ClassifiedPair:
    pair: RunTestPair
    bucket: str              # regressed | fixed | unchanged_pass | unchanged_fail | added | removed
    regressed_dims: list[str]  # critical dims that flipped pass->fail (for regressed bucket)
    score_delta: float | None  # b - a, None if either side missing


@dataclass(frozen=True)
class CategoryDelta:
    category: str
    a_pass_count: int
    a_total: int
    b_pass_count: int
    b_total: int
    a_pass_rate: float
    b_pass_rate: float
    pass_rate_delta: float


@dataclass(frozen=True)
class DimensionDelta:
    dimension: str
    a_pass_count: int
    a_total: int
    b_pass_count: int
    b_total: int
    a_pass_rate: float
    b_pass_rate: float
    pass_rate_delta: float


def classify_pairs(
    pairs: Iterable[RunTestPair],
    critical_dimensions: set[str],
) -> list[ClassifiedPair]:
    """Bucket each test pair and capture which critical dims regressed."""
    out: list[ClassifiedPair] = []
    for p in pairs:
        if p.a_passed is None and p.b_passed is not None:
            bucket = "added"
            regressed_dims: list[str] = []
        elif p.a_passed is not None and p.b_passed is None:
            bucket = "removed"
            regressed_dims = []
        else:
            regressed_dims = _regressed_critical_dims(p, critical_dimensions)
            overall_regressed = p.a_passed is True and p.b_passed is False
            if overall_regressed or regressed_dims:
                bucket = "regressed"
            elif p.a_passed is False and p.b_passed is True:
                bucket = "fixed"
            elif p.a_passed and p.b_passed:
                bucket = "unchanged_pass"
            else:
                bucket = "unchanged_fail"

        score_delta = (
            (p.b_score - p.a_score)
            if (p.a_score is not None and p.b_score is not None)
            else None
        )
        out.append(
            ClassifiedPair(
                pair=p,
                bucket=bucket,
                regressed_dims=regressed_dims,
                score_delta=score_delta,
            )
        )
    return out


def _regressed_critical_dims(pair: RunTestPair, critical: set[str]) -> list[str]:
    regressed: list[str] = []
    for dim in critical:
        a = pair.a_dims.get(dim)
        b = pair.b_dims.get(dim)
        if a is True and b is False:
            regressed.append(dim)
    return sorted(regressed)


def compute_verdict(classified: list[ClassifiedPair]) -> Verdict:
    """Red if anything regressed. Amber if any soft deltas. Green otherwise."""
    has_regression = any(c.bucket == "regressed" for c in classified)
    if has_regression:
        return Verdict.RED

    has_soft_delta = any(
        c.bucket in {"fixed", "added", "removed"}
        or (c.score_delta is not None and abs(c.score_delta) > 1e-6)
        for c in classified
    )
    return Verdict.AMBER if has_soft_delta else Verdict.GREEN


def category_deltas(classified: list[ClassifiedPair]) -> list[CategoryDelta]:
    """Per-category pass-rate deltas. Uses 'bucket' to determine a/b presence."""
    agg: dict[str, dict[str, int]] = {}
    for c in classified:
        cat = c.pair.category or "(uncategorized)"
        d = agg.setdefault(cat, {"a_total": 0, "a_pass": 0, "b_total": 0, "b_pass": 0})
        if c.pair.a_passed is not None:
            d["a_total"] += 1
            if c.pair.a_passed:
                d["a_pass"] += 1
        if c.pair.b_passed is not None:
            d["b_total"] += 1
            if c.pair.b_passed:
                d["b_pass"] += 1

    deltas: list[CategoryDelta] = []
    for cat, d in sorted(agg.items()):
        a_rate = d["a_pass"] / d["a_total"] if d["a_total"] else 0.0
        b_rate = d["b_pass"] / d["b_total"] if d["b_total"] else 0.0
        deltas.append(
            CategoryDelta(
                category=cat,
                a_pass_count=d["a_pass"],
                a_total=d["a_total"],
                b_pass_count=d["b_pass"],
                b_total=d["b_total"],
                a_pass_rate=a_rate,
                b_pass_rate=b_rate,
                pass_rate_delta=b_rate - a_rate,
            )
        )
    return deltas


def dimension_deltas(classified: list[ClassifiedPair]) -> list[DimensionDelta]:
    """Per-dimension pass-rate deltas across both runs."""
    agg: dict[str, dict[str, int]] = {}
    for c in classified:
        for dim, passed in c.pair.a_dims.items():
            d = agg.setdefault(dim, {"a_total": 0, "a_pass": 0, "b_total": 0, "b_pass": 0})
            d["a_total"] += 1
            if passed:
                d["a_pass"] += 1
        for dim, passed in c.pair.b_dims.items():
            d = agg.setdefault(dim, {"a_total": 0, "a_pass": 0, "b_total": 0, "b_pass": 0})
            d["b_total"] += 1
            if passed:
                d["b_pass"] += 1

    deltas: list[DimensionDelta] = []
    for dim, d in sorted(agg.items(), key=lambda kv: kv[0]):
        a_rate = d["a_pass"] / d["a_total"] if d["a_total"] else 0.0
        b_rate = d["b_pass"] / d["b_total"] if d["b_total"] else 0.0
        deltas.append(
            DimensionDelta(
                dimension=dim,
                a_pass_count=d["a_pass"],
                a_total=d["a_total"],
                b_pass_count=d["b_pass"],
                b_total=d["b_total"],
                a_pass_rate=a_rate,
                b_pass_rate=b_rate,
                pass_rate_delta=b_rate - a_rate,
            )
        )
    # Sort by biggest regressions first (most negative delta).
    deltas.sort(key=lambda x: x.pass_rate_delta)
    return deltas


def bucket_counts(classified: list[ClassifiedPair]) -> dict[str, int]:
    """Summary counts by bucket, including zero-count buckets for stable templates."""
    buckets = ["regressed", "fixed", "unchanged_pass", "unchanged_fail", "added", "removed"]
    counts = {b: 0 for b in buckets}
    for c in classified:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    return counts
