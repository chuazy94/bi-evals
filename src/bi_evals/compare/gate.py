"""CI gate: pass/fail policy layered on top of the compare verdict.

`compute_verdict` stays the pure *report* verdict (what a human sees in the
HTML); this module decides whether that outcome should fail a build. The two
are deliberately separate — the gate's budget/floor/fail_on knobs never change
what the report shows.

One engine, two surfaces: `bi-evals compare` (exit code) and the SDK's
`RunReport.passed_gate` / `RunReport.compare_to` all call `evaluate_gate`, so
CLI and SDK verdicts cannot diverge.

The gate answers two independent questions, each opt-in via `compare:` config:

- **Absolute** — is this run good enough on its own? (`min_pass_rate`; needs no
  baseline, so it works on a very first run.)
- **Relative** — did this run get worse than the baseline? (the existing
  rate-based `Verdict` machinery, with `max_regressions_allowed` as the flaky-
  suite release valve.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import duckdb

from bi_evals.compare.diff import (
    ClassifiedPair,
    Verdict,
    classify_pairs,
    compute_verdict,
)
from bi_evals.store import queries as q
from bi_evals.store.queries import RunDiff

FailOn = Literal["red", "amber", "never"]


@dataclass(frozen=True)
class ComparedRuns:
    """One classification of a (baseline, candidate) run pair, computed once
    and shared by the HTML report and the gate."""

    diff: RunDiff
    classified: list[ClassifiedPair]


def classify_runs(
    conn: duckdb.DuckDBPyConnection,
    baseline_run_id: str,
    candidate_run_id: str,
    *,
    regression_threshold: float = 0.2,
) -> ComparedRuns:
    """Fetch and classify the pair diff for baseline (run A) vs candidate (run B)."""
    diff = q.test_diff(conn, baseline_run_id, candidate_run_id)
    critical = q.critical_dimensions(conn, candidate_run_id) or q.critical_dimensions(
        conn, baseline_run_id
    )
    classified = classify_pairs(
        diff.pairs, critical, regression_threshold=regression_threshold
    )
    return ComparedRuns(diff=diff, classified=classified)


@dataclass(frozen=True)
class GateResult:
    verdict: Verdict  # the raw report verdict (unaffected by budget/floor)
    passed: bool  # did the gate pass, given the config?
    reasons: list[str]  # human-readable why, for CLI output and assert messages
    regression_count: int
    suite_pass_rate: float | None  # the candidate run's absolute pass rate
    fail_on: FailOn = "red"  # the policy level this result was evaluated under

    def __bool__(self) -> bool:
        return self.passed


def evaluate_gate(
    classified: list[ClassifiedPair],
    *,
    suite_pass_rate: float | None,
    min_pass_rate: float | None = None,
    max_regressions_allowed: int = 0,
    fail_on: FailOn = "red",
) -> GateResult:
    """Decide whether a run should fail the build.

    Pass ``classified=[]`` to evaluate only the absolute floor (no baseline —
    fine on a first-ever run). ``fail_on="never"`` reports breaches in
    ``reasons`` but never flips ``passed`` — report-only mode.
    """
    verdict = compute_verdict(classified)
    regression_count = sum(1 for c in classified if c.bucket == "regressed")

    reasons: list[str] = []
    failures: list[bool] = []

    # Relative gate: regressions vs the budget.
    if regression_count > max_regressions_allowed:
        failures.append(True)
        reasons.append(
            f"{regression_count} test(s) regressed (budget {max_regressions_allowed})"
        )
    elif regression_count:
        reasons.append(
            f"{regression_count} regression(s) absorbed by budget "
            f"({max_regressions_allowed} allowed)"
        )
    elif classified:
        reasons.append("no regressions")

    # fail_on="amber": any delta at all (soft or hard) fails the gate.
    if fail_on == "amber" and verdict is not Verdict.GREEN and not failures:
        failures.append(True)
        reasons.append(f"deltas present (verdict {verdict.value}, fail_on amber)")

    # Absolute gate: the floor needs no baseline and overrides a clean diff.
    if min_pass_rate is not None:
        if suite_pass_rate is None:
            reasons.append(f"floor {min_pass_rate:.2f} not evaluated (no pass rate)")
        elif suite_pass_rate < min_pass_rate:
            failures.append(True)
            reasons.append(
                f"suite pass rate {suite_pass_rate:.2f} < floor {min_pass_rate:.2f}"
            )
        else:
            reasons.append(
                f"suite pass rate {suite_pass_rate:.2f} >= floor {min_pass_rate:.2f}"
            )

    if not reasons:
        reasons.append("nothing to gate")

    if fail_on == "never":
        if failures:
            reasons.append("(report-only: fail_on=never, build not failed)")
        passed = True
    else:
        passed = not failures

    return GateResult(
        verdict=verdict,
        passed=passed,
        reasons=reasons,
        regression_count=regression_count,
        suite_pass_rate=suite_pass_rate,
        fail_on=fail_on,
    )
