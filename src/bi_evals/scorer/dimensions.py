"""Nine-dimension binary evaluators for scoring agent outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bi_evals.config import ScoringConfig
from bi_evals.db.client import QueryResult
from bi_evals.golden.model import GoldenTest
from bi_evals.scorer.sql_utils import extract_filter_columns, extract_tables


@dataclass
class DimensionResult:
    """Result of a single dimension evaluation."""

    name: str
    passed: bool
    score: float  # 1.0 or 0.0
    reason: str


def _skip(name: str, reason: str) -> DimensionResult:
    """Auto-pass a dimension that doesn't apply."""
    return DimensionResult(name=name, passed=True, score=1.0, reason=f"skipped: {reason}")


# ---------------------------------------------------------------------------
# Dimension 1: Execution
# ---------------------------------------------------------------------------

def check_execution(generated: QueryResult) -> DimensionResult:
    if generated.success:
        return DimensionResult(
            name="execution", passed=True, score=1.0,
            reason=f"SQL executed successfully, returned {generated.row_count} rows",
        )
    return DimensionResult(
        name="execution", passed=False, score=0.0,
        reason=f"SQL execution failed: {generated.error}",
    )


# ---------------------------------------------------------------------------
# Dimension 2: Table Alignment
# ---------------------------------------------------------------------------

def check_table_alignment(generated_sql: str, reference_sql: str) -> DimensionResult:
    try:
        gen_tables = extract_tables(generated_sql)
        ref_tables = extract_tables(reference_sql)
    except Exception as e:
        return DimensionResult(
            name="table_alignment", passed=False, score=0.0,
            reason=f"SQL parse error: {e}",
        )

    if not ref_tables:
        return _skip("table_alignment", "no tables found in reference SQL")

    missing = ref_tables - gen_tables
    if not missing:
        return DimensionResult(
            name="table_alignment", passed=True, score=1.0,
            reason=f"All reference tables present: {sorted(ref_tables)}",
        )
    return DimensionResult(
        name="table_alignment", passed=False, score=0.0,
        reason=f"Missing tables: {sorted(missing)}",
    )


# ---------------------------------------------------------------------------
# Dimension 3: Column Alignment
# ---------------------------------------------------------------------------

def check_column_alignment(generated: QueryResult, golden: GoldenTest) -> DimensionResult:
    required = {c.upper() for c in golden.expected.required_columns}
    if not required:
        return _skip("column_alignment", "no required_columns defined")

    actual = {c.upper() for c in generated.columns}
    missing = required - actual
    if not missing:
        return DimensionResult(
            name="column_alignment", passed=True, score=1.0,
            reason=f"All required columns present: {sorted(required)}",
        )
    return DimensionResult(
        name="column_alignment", passed=False, score=0.0,
        reason=f"Missing required columns: {sorted(missing)}",
    )


# ---------------------------------------------------------------------------
# Dimension 4: Filter Correctness
# ---------------------------------------------------------------------------

def check_filter_correctness(generated_sql: str, reference_sql: str) -> DimensionResult:
    try:
        gen_filters = extract_filter_columns(generated_sql)
        ref_filters = extract_filter_columns(reference_sql)
    except Exception as e:
        return DimensionResult(
            name="filter_correctness", passed=False, score=0.0,
            reason=f"SQL parse error: {e}",
        )

    if not ref_filters and not gen_filters:
        return _skip("filter_correctness", "no WHERE clause in either SQL")

    if gen_filters == ref_filters:
        return DimensionResult(
            name="filter_correctness", passed=True, score=1.0,
            reason=f"Filter structure matches: {sorted(ref_filters)}",
        )

    missing = ref_filters - gen_filters
    extra = gen_filters - ref_filters
    parts = []
    if missing:
        parts.append(f"missing filters: {sorted(missing)}")
    if extra:
        parts.append(f"extra filters: {sorted(extra)}")
    return DimensionResult(
        name="filter_correctness", passed=False, score=0.0,
        reason="; ".join(parts),
    )


# ---------------------------------------------------------------------------
# Dimension 5: Row Completeness
# ---------------------------------------------------------------------------

def _normalize_value(v: Any, tolerance: float) -> Any:
    """Normalize a value for comparison."""
    if v is None:
        return None
    if isinstance(v, float):
        # Round to tolerance precision for hashing
        if tolerance > 0:
            digits = max(0, -int(f"{tolerance:e}".split("e")[1]) + 1)
            return round(v, digits)
        return v
    if isinstance(v, str):
        return v.strip().upper()
    return v


def _row_key(row: dict[str, Any], columns: list[str], tolerance: float) -> tuple:
    """Create a hashable key from a row using specified columns."""
    return tuple(_normalize_value(row.get(c.upper()), tolerance) for c in columns)


def check_row_completeness(
    generated: QueryResult,
    reference: QueryResult,
    golden: GoldenTest,
    config: ScoringConfig,
) -> DimensionResult:
    rc = golden.expected.row_comparison
    if not rc.enabled:
        return _skip("row_completeness", "row_comparison not enabled")

    if not reference.success:
        return DimensionResult(
            name="row_completeness", passed=False, score=0.0,
            reason="Reference SQL failed — cannot compare rows",
        )

    key_cols = rc.key_columns or reference.columns
    tolerance = rc.value_tolerance
    threshold = rc.completeness_threshold

    ref_keys = {_row_key(r, key_cols, tolerance) for r in reference.rows}
    gen_keys = {_row_key(r, key_cols, tolerance) for r in generated.rows}

    if not ref_keys:
        return _skip("row_completeness", "reference returned 0 rows")

    found = len(ref_keys & gen_keys)
    ratio = found / len(ref_keys)
    passed = ratio >= threshold

    return DimensionResult(
        name="row_completeness", passed=passed, score=1.0 if passed else 0.0,
        reason=f"{found}/{len(ref_keys)} reference rows found ({ratio:.1%}), threshold {threshold:.0%}",
    )


# ---------------------------------------------------------------------------
# Dimension 6: Row Precision
# ---------------------------------------------------------------------------

def check_row_precision(
    generated: QueryResult,
    reference: QueryResult,
    golden: GoldenTest,
    config: ScoringConfig,
) -> DimensionResult:
    rc = golden.expected.row_comparison
    if not rc.enabled:
        return _skip("row_precision", "row_comparison not enabled")

    if not reference.success:
        return DimensionResult(
            name="row_precision", passed=False, score=0.0,
            reason="Reference SQL failed — cannot compare rows",
        )

    key_cols = rc.key_columns or reference.columns
    tolerance = rc.value_tolerance
    threshold = rc.precision_threshold

    ref_keys = {_row_key(r, key_cols, tolerance) for r in reference.rows}
    gen_keys = {_row_key(r, key_cols, tolerance) for r in generated.rows}

    if not gen_keys:
        return _skip("row_precision", "generated returned 0 rows")

    matched = len(gen_keys & ref_keys)
    ratio = matched / len(gen_keys)
    passed = ratio >= threshold

    return DimensionResult(
        name="row_precision", passed=passed, score=1.0 if passed else 0.0,
        reason=f"{matched}/{len(gen_keys)} generated rows match reference ({ratio:.1%}), threshold {threshold:.0%}",
    )


# ---------------------------------------------------------------------------
# Dimension 7: Value Accuracy
# ---------------------------------------------------------------------------

def check_value_accuracy(
    generated: QueryResult,
    reference: QueryResult,
    golden: GoldenTest,
    config: ScoringConfig,
) -> DimensionResult:
    rc = golden.expected.row_comparison
    if not rc.enabled:
        return _skip("value_accuracy", "row_comparison not enabled")

    if not reference.success:
        return DimensionResult(
            name="value_accuracy", passed=False, score=0.0,
            reason="Reference SQL failed — cannot compare values",
        )

    key_cols = rc.key_columns or reference.columns
    val_cols = [c.upper() for c in rc.value_columns] if rc.value_columns else reference.columns
    tolerance = rc.value_tolerance

    # Build lookup: key -> row for reference
    ref_by_key: dict[tuple, dict[str, Any]] = {}
    for row in reference.rows:
        k = _row_key(row, key_cols, tolerance)
        ref_by_key[k] = row

    mismatches: list[str] = []
    matched_count = 0

    for row in generated.rows:
        k = _row_key(row, key_cols, tolerance)
        ref_row = ref_by_key.get(k)
        if ref_row is None:
            continue
        matched_count += 1
        for col in val_cols:
            gen_val = row.get(col.upper())
            ref_val = ref_row.get(col.upper())
            if gen_val is None and ref_val is None:
                continue
            if gen_val is None or ref_val is None:
                mismatches.append(f"{col}: {gen_val} vs {ref_val}")
                continue
            if isinstance(gen_val, (int, float)) and isinstance(ref_val, (int, float)):
                denom = max(abs(ref_val), 1)
                if abs(gen_val - ref_val) / denom > tolerance:
                    mismatches.append(f"{col}: {gen_val} vs {ref_val}")

    if matched_count == 0:
        return DimensionResult(
            name="value_accuracy", passed=False, score=0.0,
            reason="No matching rows found to compare values",
        )

    if not mismatches:
        return DimensionResult(
            name="value_accuracy", passed=True, score=1.0,
            reason=f"All values match within tolerance ({tolerance}) across {matched_count} matched rows",
        )
    return DimensionResult(
        name="value_accuracy", passed=False, score=0.0,
        reason=f"Value mismatches: {'; '.join(mismatches[:10])}",
    )


# ---------------------------------------------------------------------------
# Dimension 8: No Hallucinated Columns
# ---------------------------------------------------------------------------

def check_no_hallucinated_columns(
    generated: QueryResult, reference: QueryResult,
) -> DimensionResult:
    ref_cols = {c.upper() for c in reference.columns}
    gen_cols = {c.upper() for c in generated.columns}

    if not ref_cols:
        return _skip("no_hallucinated_columns", "reference has no columns")

    extra = gen_cols - ref_cols
    if not extra:
        return DimensionResult(
            name="no_hallucinated_columns", passed=True, score=1.0,
            reason="No extra columns beyond reference",
        )
    return DimensionResult(
        name="no_hallucinated_columns", passed=False, score=0.0,
        reason=f"Hallucinated columns: {sorted(extra)}",
    )


# ---------------------------------------------------------------------------
# Dimension 9: Skill Path Correctness
# ---------------------------------------------------------------------------

def check_skill_path_correctness(
    trace: list[dict], golden: GoldenTest,
) -> DimensionResult:
    esp = golden.expected_skill_path
    if not esp.required_skills:
        return _skip("skill_path_correctness", "no required_skills defined")

    # Extract tool-use steps from trace
    tool_steps = [
        s for s in trace if s.get("type") == "tool_use"
    ]

    matched_indices: list[int] = []
    missing: list[str] = []

    for skill in esp.required_skills:
        found = False
        for i, step in enumerate(tool_steps):
            if step.get("tool_name") != skill.tool:
                continue
            # Check if input_contains appears in any input value
            tool_input = step.get("tool_input", {}) or {}
            input_str = " ".join(str(v) for v in tool_input.values())
            if skill.input_contains in input_str:
                matched_indices.append(i)
                found = True
                break
        if not found:
            missing.append(f"{skill.tool}({skill.input_contains})")

    if missing:
        return DimensionResult(
            name="skill_path_correctness", passed=False, score=0.0,
            reason=f"Missing skill invocations: {', '.join(missing)}",
        )

    # Check sequence if required
    if esp.sequence_matters and matched_indices != sorted(matched_indices):
        return DimensionResult(
            name="skill_path_correctness", passed=False, score=0.0,
            reason="Skills invoked out of expected order",
        )

    return DimensionResult(
        name="skill_path_correctness", passed=True, score=1.0,
        reason=f"All {len(esp.required_skills)} required skills invoked correctly",
    )
