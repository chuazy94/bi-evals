"""Promptfoo scorer entry point.

Promptfoo calls `get_assert(output, context)` for each test case.
This module loads the golden test, reads the provider trace, executes
SQL, runs enabled dimensions, and returns per-dimension results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.db.factory import create_db_client

log = logging.getLogger("bi_evals.scorer")
from bi_evals.golden.loader import load_golden_test
from bi_evals.trace_paths import make_test_id_slug, slugify_model
from bi_evals.scorer.capability import (
    DimensionStatus,
    TraceUsability,
    cascade_failed_upstream,
    classify_trace,
    critical_not_evaluated,
    ne1_no_trace,
    ne2_unusable_trace,
)
from bi_evals.scorer.dimensions import (
    DimensionResult,
    check_anti_pattern_compliance,
    check_column_alignment,
    check_execution,
    check_filter_correctness,
    check_no_hallucinated_columns,
    check_row_completeness,
    check_row_precision,
    check_skill_path_correctness,
    check_table_alignment,
    check_value_accuracy,
    not_evaluated,
)
from bi_evals.db.client import QueryResult


def _load_trace(trace_path: Path) -> dict[str, Any]:
    """Load the trace JSON written by the provider."""
    if not trace_path.exists():
        return {}
    return json.loads(trace_path.read_text())


def _resolve_trace_path(
    trace_dir: Path,
    test_id_slug: str,
    model_slug: str | None,
) -> Path:
    """Pick the trace file the provider just wrote for this (test, model).

    The provider writes `{slug}__{model}__{suffix}.json` per invocation so
    that multi-model runs and repeat-N don't collide. The scorer used to
    read `{slug}.json` — a path the provider hasn't written to since
    multi-model support landed — which silently graded whatever stale
    trace happened to be at that path. This resolver fixes that by:

      1. Preferring the most recent `{slug}__{model_slug}__*.json` match
         when we know the model (the same `provider_config["model"]` the
         provider read at write-time).
      2. Falling back to the most recent `{slug}__*.json` if model is
         unknown (e.g. single-model legacy config).
      3. Falling back to the legacy `{slug}.json` path so manually-written
         test fixtures keep working.

    Picking by mtime handles repeat-N: each repeat writes a fresh trace
    and the scorer for that repeat sees the newest.
    """
    if model_slug:
        per_model = sorted(
            trace_dir.glob(f"{test_id_slug}__{model_slug}__*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if per_model:
            return per_model[-1]

    any_model = sorted(
        trace_dir.glob(f"{test_id_slug}__*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if any_model:
        return any_model[-1]

    return trace_dir / f"{test_id_slug}.json"


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """Promptfoo scorer entry point.

    Returns a GradingResult dict with componentResults (one per dimension).
    """
    vars_ = context.get("vars", {})
    provider_config = context.get("config", {}) or {}
    config_path = (
        provider_config.get("config_path")
        or vars_.get("config_path")
        or "bi-evals.yaml"
    )
    config = BiEvalsConfig.load(Path(config_path))

    prompt = context.get("prompt", output)

    # Load golden test
    golden_file = vars_.get("golden_file", "")
    if not golden_file:
        return {"pass": False, "score": 0.0, "reason": "No golden_file in test vars"}

    golden_path = config.resolve_path(golden_file)
    if not golden_path.exists():
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Golden test not found: {golden_file}",
        }

    golden = load_golden_test(golden_path)

    # Load trace. The model slug must match what the provider used at
    # write-time so we grade *this* run's trace, not a stale one from a
    # different model that happens to share the test slug.
    test_id_slug = make_test_id_slug(prompt, vars_)
    trace_dir = config.resolve_path(config.reporting.results_dir) / "traces"
    model_for_trace = provider_config.get("model") or config.agent.model
    model_slug = slugify_model(model_for_trace) if model_for_trace else None
    trace_path = _resolve_trace_path(trace_dir, test_id_slug, model_slug)
    trace_data = _load_trace(trace_path)

    generated_sql = trace_data.get("generated_sql", "")
    trace_steps = trace_data.get("trace", [])
    agent_error = trace_data.get("agent_error")

    # The agent failed to answer this golden (e.g. a push `error` row). Report it
    # as a failed `execution` dimension carrying the message — a visible failing
    # outcome, not a silent gap or a generic "no SQL" error.
    if agent_error:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"agent error: {agent_error}",
            "componentResults": [
                {
                    "pass": False,
                    "score": 0.0,
                    "reason": f"agent error: {agent_error}",
                    "namedScores": {"execution": 0.0},
                }
            ],
        }

    if not generated_sql:
        return {
            "pass": False,
            "score": 0.0,
            "reason": "No generated SQL found in trace",
        }

    reference_sql = golden.reference_sql
    test_label = vars_.get("golden_file") or test_id_slug

    # Execute SQL. This is the slow, failure-prone step (network + warehouse +
    # auth) — narrate it and surface execution errors at run time rather than
    # only into the trace file.
    log.info("%s: executing generated SQL against %s", test_label, config.database.type)
    db_client = create_db_client(config.database)
    try:
        generated_result = db_client.execute(generated_sql)
        if not generated_result.success:
            log.warning(
                "%s: generated SQL execution failed: %s",
                test_label,
                generated_result.error,
            )
        reference_result = (
            db_client.execute(reference_sql)
            if reference_sql
            else QueryResult(columns=[], rows=[], row_count=0, error="No reference SQL")
        )
        if reference_sql and not reference_result.success:
            log.warning(
                "%s: reference SQL execution failed: %s",
                test_label,
                reference_result.error,
            )
    finally:
        db_client.close()

    # Map dimension names to evaluator calls
    enabled = set(config.scoring.dimensions)
    results: list[DimensionResult] = []

    # The sqlglot dialect the scorer parses SQL with, derived from the configured
    # warehouse. Resolved once here and threaded into every structural dimension
    # so no evaluator silently falls back to a hardcoded dialect.
    dialect = config.database.dialect

    execution_passed = generated_result.success

    if "execution" in enabled:
        results.append(check_execution(generated_result))

    if "table_alignment" in enabled and reference_sql:
        results.append(check_table_alignment(generated_sql, reference_sql, dialect))

    if "column_alignment" in enabled:
        results.append(check_column_alignment(generated_sql, golden, dialect))

    if "filter_correctness" in enabled and reference_sql:
        results.append(check_filter_correctness(generated_sql, reference_sql, dialect))

    def _cascade(name: str) -> DimensionResult:
        # Upstream execution failure: a genuine FAIL (the agent's SQL never
        # produced rows to compare), with a reason that says so honestly
        # instead of the old misleading "skipped:" wording.
        return DimensionResult(
            name=name,
            passed=False,
            score=0.0,
            reason=cascade_failed_upstream(name),
        )

    if "row_completeness" in enabled:
        results.append(
            check_row_completeness(
                generated_result, reference_result, golden, config.scoring
            )
            if execution_passed
            else _cascade("row_completeness")
        )

    if "row_precision" in enabled:
        results.append(
            check_row_precision(
                generated_result, reference_result, golden, config.scoring
            )
            if execution_passed
            else _cascade("row_precision")
        )

    if "value_accuracy" in enabled:
        results.append(
            check_value_accuracy(
                generated_result, reference_result, golden, config.scoring
            )
            if execution_passed
            else _cascade("value_accuracy")
        )

    if "no_hallucinated_columns" in enabled and reference_sql:
        results.append(
            check_no_hallucinated_columns(generated_sql, reference_sql, dialect)
        )

    if "skill_path_correctness" in enabled:
        # Capability check first (Build Stage 2): distinguish "can't know"
        # (nothing usable submitted) from "know it failed" (usable trace,
        # wrong tools). Golden-side vacuous skip still wins — nothing to
        # check means the trace's presence is irrelevant.
        if not golden.expected_skill_path.required_skills:
            results.append(check_skill_path_correctness(trace_steps, golden))
        else:
            trace_cap = classify_trace(trace_steps)
            if trace_cap.usability is TraceUsability.ABSENT:
                results.append(
                    not_evaluated(
                        "skill_path_correctness",
                        ne1_no_trace("skill_path_correctness"),
                    )
                )
            elif trace_cap.usability is TraceUsability.UNUSABLE:
                results.append(
                    not_evaluated(
                        "skill_path_correctness",
                        ne2_unusable_trace(
                            "skill_path_correctness", trace_cap.total_entries
                        ),
                    )
                )
            else:
                results.append(check_skill_path_correctness(trace_steps, golden))

    if "anti_pattern_compliance" in enabled:
        results.append(check_anti_pattern_compliance(generated_sql, golden, dialect))

    # Convert to Promptfoo GradingResult with componentResults. The explicit
    # `status` key is the primary channel to ingest; the reason-string prefix
    # is the fallback if a Promptfoo version drops unknown keys.
    component_results = [
        {
            "pass": r.passed,
            "score": r.score,
            "reason": r.reason,
            "status": r.status.value,
            "namedScores": {r.name: r.score},
        }
        for r in results
    ]

    overall_pass, weighted_score, reason = aggregate_results(results, config.scoring)

    return {
        "pass": overall_pass,
        "score": weighted_score,
        "reason": reason,
        "componentResults": component_results,
    }


def aggregate_results(
    results: list[DimensionResult],
    scoring: Any,
) -> tuple[bool, float, str]:
    """Tiered scoring over honest dimension statuses (Build Stage 2).

    1. A critical dimension that could NOT be evaluated fails the test — a
       critical dimension must be *verifiable* to pass (decision D1).
    2. All evaluated critical dimensions must pass.
    3. Otherwise the weighted score over **evaluated** dimensions (PASS/FAIL
       only — `skipped` and `not_evaluated` are excluded from numerator and
       denominator, decision D2) must reach ``pass_threshold``.
    """
    weights = scoring.dimension_weights
    critical = set(scoring.critical_dimensions)

    countable = [
        r for r in results if r.status in (DimensionStatus.PASS, DimensionStatus.FAIL)
    ]
    ne_count = sum(1 for r in results if r.status is DimensionStatus.NOT_EVALUATED)
    ne_suffix = f", {ne_count} not evaluated" if ne_count else ""

    total_weight = sum(weights.get(r.name, 1.0) for r in countable)
    weighted_score = (
        sum(weights.get(r.name, 1.0) * r.score for r in countable) / total_weight
        if total_weight
        else 0.0
    )

    passed_dims = sum(1 for r in countable if r.passed)
    total_dims = len(countable)

    critical_ne = sorted(
        r.name
        for r in results
        if r.name in critical and r.status is DimensionStatus.NOT_EVALUATED
    )
    failed_critical = [r.name for r in countable if r.name in critical and not r.passed]

    if critical_ne:
        return (
            False,
            weighted_score,
            f"FAIL: {critical_not_evaluated(critical_ne)} "
            f"({passed_dims}/{total_dims} evaluated dimensions passed{ne_suffix})",
        )
    if failed_critical:
        return (
            False,
            weighted_score,
            f"Failed critical dimension(s): {failed_critical} "
            f"({passed_dims}/{total_dims} dimensions passed, "
            f"weighted score {weighted_score:.2f}{ne_suffix})",
        )
    if not countable:
        return (
            False,
            0.0,
            f"No dimension could be evaluated for this submission{ne_suffix} — "
            f"nothing to score.",
        )
    if weighted_score >= scoring.pass_threshold:
        return (
            True,
            weighted_score,
            f"Passed: {passed_dims}/{total_dims} dimensions, "
            f"weighted score {weighted_score:.2f} >= {scoring.pass_threshold:.2f}"
            f"{ne_suffix}",
        )
    return (
        False,
        weighted_score,
        f"Weighted score {weighted_score:.2f} below threshold "
        f"{scoring.pass_threshold:.2f} ({passed_dims}/{total_dims} dimensions "
        f"passed{ne_suffix})",
    )
