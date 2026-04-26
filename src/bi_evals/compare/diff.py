"""Rate-based regression detection over run pairs.

Phase 6a changes the atom of comparison from a boolean to a pass *rate*. A test
regresses when its pass_rate drops by at least ``regression_threshold`` (default
0.2) from run A to run B, or when a critical dimension's pass_rate drops by the
same threshold. For single-trial runs the rates collapse to {0.0, 1.0} so any
flip clears a 0.2 threshold — legacy semantics are preserved.

Added/removed tests (either direction, or a model present on only one side) are
reported separately and never flip the verdict red.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from bi_evals.store.queries import RunTestPair


DEFAULT_REGRESSION_THRESHOLD = 0.2


class Verdict(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class ClassifiedPair:
    pair: RunTestPair
    bucket: str              # regressed | fixed | unchanged_pass | unchanged_fail | added | removed
    regressed_dims: list[str]  # critical dims whose pass_rate dropped by >= threshold
    score_delta: float | None  # b_score - a_score, None if either side missing
    pass_rate_delta: float | None  # b_pass_rate - a_pass_rate


def classify_pairs(
    pairs: Iterable[RunTestPair],
    critical_dimensions: set[str],
    *,
    regression_threshold: float = DEFAULT_REGRESSION_THRESHOLD,
) -> list[ClassifiedPair]:
    """Bucket each test pair and capture which critical dims regressed by rate."""
    out: list[ClassifiedPair] = []
    for p in pairs:
        a_rate = p.a_pass_rate
        b_rate = p.b_pass_rate

        if a_rate is None and b_rate is not None:
            bucket = "added"
            regressed_dims: list[str] = []
        elif a_rate is not None and b_rate is None:
            bucket = "removed"
            regressed_dims = []
        else:
            regressed_dims = _regressed_critical_dims(
                p, critical_dimensions, regression_threshold
            )
            overall_regressed = (
                a_rate is not None
                and b_rate is not None
                and (a_rate - b_rate) >= regression_threshold
            )
            overall_fixed = (
                a_rate is not None
                and b_rate is not None
                and (b_rate - a_rate) >= regression_threshold
            )
            if overall_regressed or regressed_dims:
                bucket = "regressed"
            elif overall_fixed:
                bucket = "fixed"
            elif (a_rate or 0.0) >= 0.5 and (b_rate or 0.0) >= 0.5:
                bucket = "unchanged_pass"
            else:
                bucket = "unchanged_fail"

        score_delta = (
            (p.b_score - p.a_score)
            if (p.a_score is not None and p.b_score is not None)
            else None
        )
        pass_rate_delta = (
            (b_rate - a_rate)
            if (a_rate is not None and b_rate is not None)
            else None
        )
        out.append(
            ClassifiedPair(
                pair=p,
                bucket=bucket,
                regressed_dims=regressed_dims,
                score_delta=score_delta,
                pass_rate_delta=pass_rate_delta,
            )
        )
    return out


def _regressed_critical_dims(
    pair: RunTestPair, critical: set[str], threshold: float
) -> list[str]:
    regressed: list[str] = []
    for dim in critical:
        a = pair.a_dims.get(dim)
        b = pair.b_dims.get(dim)
        if a is None or b is None:
            continue
        if (a - b) >= threshold:
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


def category_deltas(classified: list[ClassifiedPair]) -> list[CategoryDelta]:
    """Per-category pass-rate deltas. Weights each (test, model) equally."""
    agg: dict[str, dict[str, float]] = {}
    for c in classified:
        cat = c.pair.category or "(uncategorized)"
        d = agg.setdefault(cat, {"a_total": 0.0, "a_pass": 0.0, "b_total": 0.0, "b_pass": 0.0})
        if c.pair.a_pass_rate is not None:
            d["a_total"] += 1
            d["a_pass"] += c.pair.a_pass_rate
        if c.pair.b_pass_rate is not None:
            d["b_total"] += 1
            d["b_pass"] += c.pair.b_pass_rate

    deltas: list[CategoryDelta] = []
    for cat, d in sorted(agg.items()):
        a_rate = d["a_pass"] / d["a_total"] if d["a_total"] else 0.0
        b_rate = d["b_pass"] / d["b_total"] if d["b_total"] else 0.0
        deltas.append(
            CategoryDelta(
                category=cat,
                a_pass_count=int(round(d["a_pass"])),
                a_total=int(d["a_total"]),
                b_pass_count=int(round(d["b_pass"])),
                b_total=int(d["b_total"]),
                a_pass_rate=a_rate,
                b_pass_rate=b_rate,
                pass_rate_delta=b_rate - a_rate,
            )
        )
    return deltas


def dimension_deltas(classified: list[ClassifiedPair]) -> list[DimensionDelta]:
    """Per-dimension pass-rate deltas across both runs."""
    agg: dict[str, dict[str, float]] = {}
    for c in classified:
        for dim, rate in c.pair.a_dims.items():
            d = agg.setdefault(dim, {"a_total": 0.0, "a_pass": 0.0, "b_total": 0.0, "b_pass": 0.0})
            d["a_total"] += 1
            d["a_pass"] += rate
        for dim, rate in c.pair.b_dims.items():
            d = agg.setdefault(dim, {"a_total": 0.0, "a_pass": 0.0, "b_total": 0.0, "b_pass": 0.0})
            d["b_total"] += 1
            d["b_pass"] += rate

    deltas: list[DimensionDelta] = []
    for dim, d in sorted(agg.items(), key=lambda kv: kv[0]):
        a_rate = d["a_pass"] / d["a_total"] if d["a_total"] else 0.0
        b_rate = d["b_pass"] / d["b_total"] if d["b_total"] else 0.0
        deltas.append(
            DimensionDelta(
                dimension=dim,
                a_pass_count=int(round(d["a_pass"])),
                a_total=int(d["a_total"]),
                b_pass_count=int(round(d["b_pass"])),
                b_total=int(d["b_total"]),
                a_pass_rate=a_rate,
                b_pass_rate=b_rate,
                pass_rate_delta=b_rate - a_rate,
            )
        )
    deltas.sort(key=lambda x: x.pass_rate_delta)
    return deltas


def bucket_counts(classified: list[ClassifiedPair]) -> dict[str, int]:
    """Summary counts by bucket, including zero-count buckets for stable templates."""
    buckets = ["regressed", "fixed", "unchanged_pass", "unchanged_fail", "added", "removed"]
    counts = {b: 0 for b in buckets}
    for c in classified:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    return counts
