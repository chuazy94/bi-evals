"""Promptfoo scorer entry point.

Promptfoo calls `get_assert(output, context)` for each test case.
This module loads the golden test, reads the provider trace, executes
SQL, runs enabled dimensions, and returns per-dimension results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.db.factory import create_db_client
from bi_evals.golden.loader import load_golden_test
from bi_evals.scorer.dimensions import (
    DimensionResult,
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


def _make_test_id_slug(prompt: str, vars_: dict[str, Any]) -> str:
    """Derive trace file slug matching provider/entry.py logic."""
    golden_file = vars_.get("golden_file", "")
    test_id = golden_file if golden_file else hashlib.md5(prompt.encode()).hexdigest()
    return test_id.replace("/", "_").replace(".", "_")


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """Promptfoo scorer entry point.

    Returns a GradingResult dict with componentResults (one per dimension).
    """
    provider_config = context.get("config", {})
    config_path = provider_config.get("config_path", "bi-evals.yaml")
    config = BiEvalsConfig.load(Path(config_path))

    vars_ = context.get("vars", {})
    prompt = context.get("prompt", output)

    # Load golden test
    golden_file = vars_.get("golden_file", "")
    if not golden_file:
        return {"pass": False, "score": 0.0, "reason": "No golden_file in test vars"}

    golden_path = config.resolve_path(golden_file)
    if not golden_path.exists():
        return {"pass": False, "score": 0.0, "reason": f"Golden test not found: {golden_file}"}

    golden = load_golden_test(golden_path)

    # Load trace
    test_id_slug = _make_test_id_slug(prompt, vars_)
    trace_dir = config.resolve_path(config.reporting.results_dir) / "traces"
    trace_path = trace_dir / f"{test_id_slug}.json"
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
