"""Promptfoo scorer entry point.

Promptfoo calls `get_assert(output, context)` for each test case.
This module loads the golden test, reads the provider trace, executes
SQL, runs enabled dimensions, and returns per-dimension results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.db.factory import create_db_client
from bi_evals.golden.loader import load_golden_test
from bi_evals.trace_paths import make_test_id_slug, slugify_model
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
        return {"pass": False, "score": 0.0, "reason": f"Golden test not found: {golden_file}"}

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

    if not generated_sql:
        return {"pass": False, "score": 0.0, "reason": "No generated SQL found in trace"}

    reference_sql = golden.reference_sql

    # Execute SQL
    db_client = create_db_client(config.database)
    try:
        generated_result = db_client.execute(generated_sql)
        reference_result = (
            db_client.execute(reference_sql) if reference_sql
            else QueryResult(columns=[], rows=[], row_count=0, error="No reference SQL")
        )
    finally:
        db_client.close()

    # Map dimension names to evaluator calls
    enabled = set(config.scoring.dimensions)
    results: list[DimensionResult] = []

    execution_passed = generated_result.success

    if "execution" in enabled:
        results.append(check_execution(generated_result))

    if "table_alignment" in enabled and reference_sql:
        results.append(check_table_alignment(generated_sql, reference_sql))

    if "column_alignment" in enabled:
        results.append(check_column_alignment(generated_sql, golden))

    if "filter_correctness" in enabled and reference_sql:
        results.append(check_filter_correctness(generated_sql, reference_sql))

    if "row_completeness" in enabled:
        if execution_passed:
            results.append(check_row_completeness(generated_result, reference_result, golden, config.scoring))
        else:
            results.append(DimensionResult(
                name="row_completeness", passed=False, score=0.0,
                reason="skipped: SQL execution failed",
            ))

    if "row_precision" in enabled:
        if execution_passed:
            results.append(check_row_precision(generated_result, reference_result, golden, config.scoring))
        else:
            results.append(DimensionResult(
                name="row_precision", passed=False, score=0.0,
                reason="skipped: SQL execution failed",
            ))

    if "value_accuracy" in enabled:
        if execution_passed:
            results.append(check_value_accuracy(generated_result, reference_result, golden, config.scoring))
        else:
            results.append(DimensionResult(
                name="value_accuracy", passed=False, score=0.0,
                reason="skipped: SQL execution failed",
            ))

    if "no_hallucinated_columns" in enabled and reference_sql:
        results.append(check_no_hallucinated_columns(generated_sql, reference_sql))

    if "skill_path_correctness" in enabled:
        results.append(check_skill_path_correctness(trace_steps, golden))

    if "anti_pattern_compliance" in enabled:
        results.append(check_anti_pattern_compliance(generated_sql, golden))

    # Convert to Promptfoo GradingResult with componentResults
    component_results = [
        {
            "pass": r.passed,
            "score": r.score,
            "reason": r.reason,
            "namedScores": {r.name: r.score},
        }
        for r in results
    ]

    # Tiered scoring:
    #   1. All critical dimensions must pass (e.g. execution, row_completeness,
    #      value_accuracy). If any critical dimension fails, the test fails.
    #   2. Otherwise, compute a weighted score across all dimensions and require
    #      it to be >= pass_threshold.
    weights = config.scoring.dimension_weights
    critical = set(config.scoring.critical_dimensions)

    total_weight = sum(weights.get(r.name, 1.0) for r in results)
    weighted_score = (
        sum(weights.get(r.name, 1.0) * r.score for r in results) / total_weight
        if total_weight else 0.0
    )

    failed_critical = [
        r.name for r in results if r.name in critical and not r.passed
    ]
    passed_dims = sum(1 for r in results if r.passed)
    total_dims = len(results)

    if failed_critical:
        overall_pass = False
        reason = (
            f"Failed critical dimension(s): {failed_critical} "
            f"({passed_dims}/{total_dims} dimensions passed, "
            f"weighted score {weighted_score:.2f})"
        )
    elif weighted_score >= config.scoring.pass_threshold:
        overall_pass = True
        reason = (
            f"Passed: {passed_dims}/{total_dims} dimensions, "
            f"weighted score {weighted_score:.2f} >= {config.scoring.pass_threshold:.2f}"
        )
    else:
        overall_pass = False
        reason = (
            f"Weighted score {weighted_score:.2f} below threshold "
            f"{config.scoring.pass_threshold:.2f} ({passed_dims}/{total_dims} dimensions passed)"
        )

    return {
        "pass": overall_pass,
        "score": weighted_score,
        "reason": reason,
        "componentResults": component_results,
    }
