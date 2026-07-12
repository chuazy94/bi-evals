"""Capability check: can each dimension be scored, given what was submitted?

Build Stage 2 (see docs/plans/build-stage-2-capability-check.md). The core
principle: when bi-evals cannot score a dimension it says "I can't know" —
never "I know it failed". This module is the single source of truth for

- the :class:`DimensionStatus` vocabulary,
- classifying a submitted trace as usable / unusable / absent,
- the user-facing "not evaluated" message strings (the NE-x catalogue),
- suite-level trace-coverage counts used by pre-flight warnings.

Everything here is pure — no DB, no I/O — so the scorer, push validation,
`doctor`, and the report all consume the same logic and can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class DimensionStatus(str, Enum):
    """How a dimension result should be interpreted (and aggregated).

    PASS / FAIL count toward the weighted score and critical gating. The
    other two are excluded from both numerator and denominator:

    - NOT_EVALUATED — the submission lacks the data; bi-evals cannot know.
    - SKIPPED — the golden declares nothing to check; vacuously true.

    An upstream-cascade failure (execution failed → row dims) is a FAIL:
    the agent caused it.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUATED = "not_evaluated"
    SKIPPED = "skipped"


# Reason-string prefixes, kept stable: ingest falls back to sniffing these
# when the explicit status key doesn't survive the Promptfoo round-trip,
# and the report builder keys off them for vacuous-dimension dropping.
NOT_EVALUATED_PREFIX = "not evaluated:"
SKIPPED_PREFIX = "skipped:"


def status_from_reason(reason: str | None, passed: bool) -> DimensionStatus:
    """Derive a status for rows whose explicit status didn't survive the wire."""
    r = (reason or "").lower()
    if r.startswith(NOT_EVALUATED_PREFIX):
        return DimensionStatus.NOT_EVALUATED
    if r.startswith(SKIPPED_PREFIX):
        return DimensionStatus.SKIPPED
    return DimensionStatus.PASS if passed else DimensionStatus.FAIL


class TraceUsability(str, Enum):
    ABSENT = "absent"  # no trace steps at all
    UNUSABLE = "unusable"  # entries present, none carries tool_name + tool_input
    USABLE = "usable"  # at least one structured tool step


@dataclass(frozen=True)
class TraceCapability:
    usability: TraceUsability
    usable_steps: int
    total_entries: int


def classify_trace(trace_steps: list[dict[str, Any]] | None) -> TraceCapability:
    """Classify a (already envelope-normalised) trace's usability.

    A step is usable when it carries a ``tool_name`` and a dict ``tool_input``
    — the fields ``skill_path_correctness`` matches required skills against.
    By this point the adapter has normalised entries into TraceStep dicts, so
    an entry that arrived without those keys shows up here with them empty.
    """
    steps = trace_steps or []
    usable = sum(
        1
        for s in steps
        if isinstance(s, dict)
        and s.get("tool_name")
        and isinstance(s.get("tool_input"), dict)
    )
    if not steps:
        usability = TraceUsability.ABSENT
    elif usable == 0:
        usability = TraceUsability.UNUSABLE
    else:
        usability = TraceUsability.USABLE
    return TraceCapability(
        usability=usability, usable_steps=usable, total_entries=len(steps)
    )


# ---------------------------------------------------------------------------
# The message catalogue (docs/plans/build-stage-2-capability-check.md).
# Three parts each: what happened → what it means → what to do.
# ---------------------------------------------------------------------------


def ne1_no_trace(dimension: str) -> str:
    return (
        f"{NOT_EVALUATED_PREFIX} the submission has no trace, so bi-evals cannot "
        f"know which tools or files the agent used. To enable: submit "
        f'trace.tool_calls as [{{"tool_name": ..., "tool_input": {{...}}}}] '
        f"(docs/instrumenting-your-agent.md). If your agent has no tools, "
        f"remove {dimension} from scoring.dimensions instead."
    )


def ne2_unusable_trace(dimension: str, total_entries: int) -> str:
    return (
        f"{NOT_EVALUATED_PREFIX} a trace was submitted but none of its "
        f"{total_entries} entr{'y was' if total_entries == 1 else 'ies were'} "
        f'usable — each step needs "tool_name" and a dict "tool_input". '
        f'See docs/instrumenting-your-agent.md ("The trace shape bi-evals '
        f'understands").'
    )


def cascade_failed_upstream(blocked_dimension: str) -> str:
    return (
        "failed upstream: the generated SQL did not execute, so result "
        "comparison was impossible. Fix the execution failure first — "
        f"{blocked_dimension} counts as failed because the agent's SQL never "
        "produced rows to compare."
    )


def critical_not_evaluated(dimensions: list[str]) -> str:
    dims = ", ".join(dimensions)
    return (
        f"critical dimension(s) could not be evaluated: {dims}. A critical "
        f"dimension must be verifiable to pass — submit the data it needs, or "
        f"remove it from scoring.critical_dimensions."
    )


def coverage_warning(usable: int, total: int, dimension: str) -> str | None:
    """Pre-flight / suite warning line; None when there is nothing to warn about."""
    if total == 0 or usable == total:
        return None
    if usable == 0:
        return (
            f"0 of {total} submission(s) contain a usable trace — {dimension} "
            f"will not be evaluated this run (structural and result dimensions "
            f"are unaffected). If this is unexpected, check your harness passes "
            f"the agent's trace through; if your agent has no tools, remove "
            f"{dimension} from scoring.dimensions to silence this."
        )
    return (
        f"trace usable in {usable}/{total} submission(s) — {dimension} will be "
        f"evaluated for {usable} case(s) only."
    )


# Which dimensions depend on the submitted trace (vs. the SQL itself).
TRACE_DEPENDENT_DIMENSIONS = frozenset({"skill_path_correctness"})


def trace_coverage(rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """(usable, total) trace coverage across raw push submission rows.

    Used by pre-flight (push validation / doctor) *before* the adapter runs,
    so it works on the raw open-envelope ``trace`` field of each row. Rows
    recording an agent ``error`` are excluded — they fail execution outright
    and a trace is not expected of them.
    """
    usable = 0
    total = 0
    for row in rows:
        if row.get("error"):
            continue
        total += 1
        if _raw_trace_has_usable_step(row.get("trace")):
            usable += 1
    return usable, total


def _raw_trace_has_usable_step(trace: Any) -> bool:
    """Mirror _trace_from_row's reading of the open envelope, boolean-only."""
    if isinstance(trace, list):
        steps = trace
    elif isinstance(trace, dict):
        steps = trace.get("tool_calls") or trace.get("trace") or []
    else:
        return False
    return any(
        isinstance(s, dict)
        and s.get("tool_name")
        and isinstance(s.get("tool_input"), dict)
        for s in steps
    )
