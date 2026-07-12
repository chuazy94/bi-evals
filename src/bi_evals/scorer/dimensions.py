"""Nine-dimension binary evaluators for scoring agent outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bi_evals.config import ScoringConfig
from bi_evals.db.client import QueryResult
from bi_evals.golden.model import AntiPatterns, GoldenTest
from bi_evals.scorer.capability import DimensionStatus
from bi_evals.scorer.sql_utils import (
    extract_columns_with_tables,
    extract_filter_columns,
    extract_output_aliases,
    extract_select_columns,
    extract_tables,
)


@dataclass
class DimensionResult:
    """Result of a single dimension evaluation.

    ``status`` is the honest interpretation (Build Stage 2): PASS/FAIL count
    toward scores and gating; SKIPPED (golden declares nothing to check) and
    NOT_EVALUATED (submission lacks the data) are excluded from both. When not
    given explicitly it derives from ``passed``.
    """

    name: str
    passed: bool
    score: float  # 1.0 or 0.0
    reason: str
    status: DimensionStatus = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.status is None:
            self.status = DimensionStatus.PASS if self.passed else DimensionStatus.FAIL


def _skip(name: str, reason: str) -> DimensionResult:
    """A dimension the golden declares nothing for — vacuously true.

    ``passed=True`` is kept for back-compat display, but SKIPPED status means
    it no longer contributes its weight to the score (Stage 2 decision D2).
    """
    return DimensionResult(
        name=name,
        passed=True,
        score=1.0,
        reason=f"skipped: {reason}",
        status=DimensionStatus.SKIPPED,
    )


def not_evaluated(name: str, reason: str) -> DimensionResult:
    """A dimension the submission carries no data for — bi-evals cannot know.

    Excluded from the weighted score entirely; fails the test only when the
    dimension is critical (Stage 2 decision D1).
    """
    return DimensionResult(
        name=name,
        passed=False,
        score=0.0,
        reason=reason,
        status=DimensionStatus.NOT_EVALUATED,
    )


# ---------------------------------------------------------------------------
# Dimension 1: Execution
# ---------------------------------------------------------------------------


def check_execution(generated: QueryResult) -> DimensionResult:
    if generated.success:
        return DimensionResult(
            name="execution",
            passed=True,
            score=1.0,
            reason=f"SQL executed successfully, returned {generated.row_count} rows",
        )
    return DimensionResult(
        name="execution",
        passed=False,
        score=0.0,
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
            name="table_alignment",
            passed=False,
            score=0.0,
            reason=f"SQL parse error: {e}",
        )

    if not ref_tables:
        return _skip("table_alignment", "no tables found in reference SQL")

    missing = ref_tables - gen_tables
    if not missing:
        return DimensionResult(
            name="table_alignment",
            passed=True,
            score=1.0,
            reason=f"All reference tables present: {sorted(ref_tables)}",
        )
    return DimensionResult(
        name="table_alignment",
        passed=False,
        score=0.0,
        reason=f"Missing tables: {sorted(missing)}",
    )


# ---------------------------------------------------------------------------
# Dimension 3: Column Alignment
# ---------------------------------------------------------------------------


def check_column_alignment(generated_sql: str, golden: GoldenTest) -> DimensionResult:
    """Check that the generated SQL references the required source columns.

    Parses SELECT expressions to extract the underlying column names (ignoring
    aliases and aggregation wrappers) and compares against required_columns.
    """
    required = {c.upper() for c in golden.expected.required_columns}
    if not required:
        return _skip("column_alignment", "no required_columns defined")

    try:
        gen_cols = extract_select_columns(generated_sql)
    except Exception as e:
        return DimensionResult(
            name="column_alignment",
            passed=False,
            score=0.0,
            reason=f"SQL parse error: {e}",
        )

    missing = required - gen_cols
    if not missing:
        return DimensionResult(
            name="column_alignment",
            passed=True,
            score=1.0,
            reason=f"All required source columns present: {sorted(required)}",
        )
    # Common authoring mistake: required_columns lists *output aliases* (the
    # names in the question's "output columns: ...") rather than the *source*
    # columns this dimension checks. Detect it and say so, rather than emitting
    # a confusing "missing source column" for a query that's otherwise correct.
    alias_named = missing & extract_output_aliases(generated_sql)
    reason = f"Missing required source columns: {sorted(missing)}"
    if alias_named:
        reason += (
            f". Note: {sorted(alias_named)} appear to be output aliases, not "
            "source columns — column_alignment checks the source columns a query "
            "reads, not its result names. If these are output names, replace them "
            "in the golden's required_columns with the source columns they're "
            "computed from."
        )
    return DimensionResult(
        name="column_alignment",
        passed=False,
        score=0.0,
        reason=reason,
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
            name="filter_correctness",
            passed=False,
            score=0.0,
            reason=f"SQL parse error: {e}",
        )

    if not ref_filters and not gen_filters:
        return _skip("filter_correctness", "no WHERE clause in either SQL")

    if gen_filters == ref_filters:
        return DimensionResult(
            name="filter_correctness",
            passed=True,
            score=1.0,
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
        name="filter_correctness",
        passed=False,
        score=0.0,
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


def _align_generated_rows(
    reference: QueryResult, generated: QueryResult
) -> tuple[list[dict[str, Any]], bool]:
    """Return generated rows re-keyed to the *reference* column names.

    Row matching keys by column name, but a black-box agent (push / api_endpoint
    / otel) names its output columns however it likes — a correct answer with
    ``nation_name`` instead of the golden's ``NATION`` would otherwise key to
    nothing and falsely fail. SQL result-set order is deterministic and the
    golden's ``ORDER BY`` pins it, so when names don't line up we fall back to
    matching by ordinal position.

    All-or-nothing: if every reference column name is present in the generated
    result, keep the generated rows as-is (name matching, today's behaviour).
    Otherwise, if the column *counts* match, remap each generated row onto the
    reference column names positionally. If counts differ we can't safely pair,
    so return the generated rows unchanged (the dimensions will report the
    mismatch as a genuine failure).

    NOTE: positional remap assumes both queries list columns in the same logical
    order (col 0 ↔ col 0, …), not merely the same row order. Two queries
    answering the same question almost always agree on column order, but a query
    that reorders its projection relative to the reference would be silently
    mis-paired. Revisit if such a case surfaces.

    Returns ``(rows, was_remapped)`` — ``was_remapped`` is True only when
    positional remapping was applied, so callers can pick the right column names
    for downstream mapping without relying on object identity.
    """
    ref_cols = [c.upper() for c in reference.columns]
    gen_cols = [c.upper() for c in generated.columns]

    if set(ref_cols).issubset(set(gen_cols)):
        return generated.rows, False  # names align — nothing to do
    if len(ref_cols) != len(gen_cols):
        return generated.rows, False  # can't pair positionally — fail honestly

    remapped: list[dict[str, Any]] = []
    for row in generated.rows:
        # generated row values in column order, relabelled with reference names
        values = [row.get(gc) for gc in gen_cols]
        remapped.append(dict(zip(ref_cols, values)))
    return remapped, True


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
            name="row_completeness",
            passed=False,
            score=0.0,
            reason="Reference SQL failed — cannot compare rows",
        )

    key_cols = rc.key_columns or reference.columns
    tolerance = rc.value_tolerance
    threshold = rc.completeness_threshold

    gen_rows, _ = _align_generated_rows(reference, generated)
    ref_keys = {_row_key(r, key_cols, tolerance) for r in reference.rows}
    gen_keys = {_row_key(r, key_cols, tolerance) for r in gen_rows}

    if not ref_keys:
        return _skip("row_completeness", "reference returned 0 rows")

    found = len(ref_keys & gen_keys)
    ratio = found / len(ref_keys)
    passed = ratio >= threshold

    return DimensionResult(
        name="row_completeness",
        passed=passed,
        score=1.0 if passed else 0.0,
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
            name="row_precision",
            passed=False,
            score=0.0,
            reason="Reference SQL failed — cannot compare rows",
        )

    key_cols = rc.key_columns or reference.columns
    tolerance = rc.value_tolerance
    threshold = rc.precision_threshold

    gen_rows, _ = _align_generated_rows(reference, generated)
    ref_keys = {_row_key(r, key_cols, tolerance) for r in reference.rows}
    gen_keys = {_row_key(r, key_cols, tolerance) for r in gen_rows}

    if not gen_keys:
        return _skip("row_precision", "generated returned 0 rows")

    matched = len(gen_keys & ref_keys)
    ratio = matched / len(gen_keys)
    passed = ratio >= threshold

    return DimensionResult(
        name="row_precision",
        passed=passed,
        score=1.0 if passed else 0.0,
        reason=f"{matched}/{len(gen_keys)} generated rows match reference ({ratio:.1%}), threshold {threshold:.0%}",
    )


# ---------------------------------------------------------------------------
# Dimension 7: Value Accuracy
# ---------------------------------------------------------------------------


def _build_column_map(
    ref_columns: list[str],
    gen_columns: list[str],
    key_cols: list[str],
) -> list[tuple[str, str]]:
    """Map reference value columns to generated value columns by position.

    Strips key columns from both lists, then pairs the remaining columns
    by ordinal position. This handles alias differences (e.g. TOTAL_CASES
    in the reference vs TOTAL_CONFIRMED_CASES in the generated result).
    """
    key_set = {c.upper() for c in key_cols}
    ref_val = [c for c in ref_columns if c.upper() not in key_set]
    gen_val = [c for c in gen_columns if c.upper() not in key_set]
    return list(zip(ref_val, gen_val))


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
            name="value_accuracy",
            passed=False,
            score=0.0,
            reason="Reference SQL failed — cannot compare values",
        )

    key_cols = rc.key_columns or reference.columns
    tolerance = rc.value_tolerance

    # Re-key generated rows to reference column names (positionally, when the
    # agent named its columns differently) so name-based matching works.
    gen_rows, was_remapped = _align_generated_rows(reference, generated)

    if rc.value_columns:
        col_pairs = [(c.upper(), c.upper()) for c in rc.value_columns]
    else:
        # After a positional remap, generated rows carry reference column names.
        gen_cols_for_map = reference.columns if was_remapped else generated.columns
        col_pairs = _build_column_map(reference.columns, gen_cols_for_map, key_cols)

    if not col_pairs:
        return _skip("value_accuracy", "no value columns to compare")

    ref_by_key: dict[tuple, dict[str, Any]] = {}
    for row in reference.rows:
        k = _row_key(row, key_cols, tolerance)
        ref_by_key[k] = row

    mismatches: list[str] = []
    matched_count = 0

    for row in gen_rows:
        k = _row_key(row, key_cols, tolerance)
        ref_row = ref_by_key.get(k)
        if ref_row is None:
            continue
        matched_count += 1
        for ref_col, gen_col in col_pairs:
            gen_val = row.get(gen_col.upper())
            ref_val = ref_row.get(ref_col.upper())
            if gen_val is None and ref_val is None:
                continue
            if gen_val is None or ref_val is None:
                mismatches.append(f"{ref_col}: {gen_val} vs {ref_val}")
                continue
            if isinstance(gen_val, (int, float)) and isinstance(ref_val, (int, float)):
                denom = max(abs(ref_val), 1)
                if abs(gen_val - ref_val) / denom > tolerance:
                    mismatches.append(f"{ref_col}: {gen_val} vs {ref_val}")

    if matched_count == 0:
        return DimensionResult(
            name="value_accuracy",
            passed=False,
            score=0.0,
            reason="No matching rows found to compare values",
        )

    if not mismatches:
        return DimensionResult(
            name="value_accuracy",
            passed=True,
            score=1.0,
            reason=f"All values match within tolerance ({tolerance}) across {matched_count} matched rows",
        )
    return DimensionResult(
        name="value_accuracy",
        passed=False,
        score=0.0,
        reason=f"Value mismatches: {'; '.join(mismatches[:10])}",
    )


# ---------------------------------------------------------------------------
# Dimension 8: No Hallucinated Columns
# ---------------------------------------------------------------------------


def check_no_hallucinated_columns(
    generated_sql: str,
    reference_sql: str,
) -> DimensionResult:
    """Check the generated SQL doesn't reference source columns absent from the reference.

    Compares the underlying column names in SELECT expressions (ignoring aliases).
    """
    try:
        gen_cols = extract_select_columns(generated_sql)
        ref_cols = extract_select_columns(reference_sql)
    except Exception as e:
        return DimensionResult(
            name="no_hallucinated_columns",
            passed=False,
            score=0.0,
            reason=f"SQL parse error: {e}",
        )

    if not ref_cols:
        return _skip("no_hallucinated_columns", "reference SQL has no select columns")

    extra = gen_cols - ref_cols
    if not extra:
        return DimensionResult(
            name="no_hallucinated_columns",
            passed=True,
            score=1.0,
            reason="No hallucinated source columns beyond reference",
        )
    return DimensionResult(
        name="no_hallucinated_columns",
        passed=False,
        score=0.0,
        reason=f"Hallucinated source columns: {sorted(extra)}",
    )


# ---------------------------------------------------------------------------
# Dimension 10: Anti-Pattern Compliance (Phase 6c)
# ---------------------------------------------------------------------------


def _check_anti_patterns(sql: str, patterns: AntiPatterns) -> list[str]:
    """Return human-readable violation descriptions, empty list if compliant."""
    violations: list[str] = []

    if patterns.forbidden_tables:
        try:
            used_tables = extract_tables(sql)
        except Exception:
            used_tables = set()
        # Forbidden lists are written as users want to read them — match against
        # the *bare* uppercase name (extract_tables returns fully qualified, e.g.
        # "FINANCE.RAW_ORDERS"). Bare entries match any schema; qualified entries
        # match exactly.
        for forbidden in patterns.forbidden_tables:
            forbidden_upper = forbidden.upper()
            for used in used_tables:
                if used == forbidden_upper or used.endswith("." + forbidden_upper):
                    violations.append(f"forbidden table used: {forbidden_upper}")
                    break

    if patterns.forbidden_columns:
        try:
            used_pairs = extract_columns_with_tables(sql)
        except Exception:
            used_pairs = set()
        for spec in patterns.forbidden_columns:
            spec_upper = spec.upper()
            if "." in spec_upper:
                tbl, col = spec_upper.split(".", 1)
                # Match exact (TBL, COL); also match (None, COL) since
                # unresolved owners shouldn't get a free pass.
                if (tbl, col) in used_pairs or (None, col) in used_pairs:
                    violations.append(f"forbidden column used: {tbl}.{col}")
            else:
                if any(c == spec_upper for _, c in used_pairs):
                    violations.append(f"forbidden column used: {spec_upper}")

    return violations


def check_anti_pattern_compliance(
    generated_sql: str,
    golden: GoldenTest,
) -> DimensionResult:
    """Fail when the generated SQL uses any forbidden table or column.

    Vacuous-pass when no anti-patterns are defined on the golden — keeps the
    dimension safely opt-in for goldens that don't care about it.
    """
    patterns = golden.anti_patterns
    if patterns is None or (
        not patterns.forbidden_tables and not patterns.forbidden_columns
    ):
        return _skip("anti_pattern_compliance", "no anti-patterns defined")

    violations = _check_anti_patterns(generated_sql, patterns)
    if violations:
        return DimensionResult(
            name="anti_pattern_compliance",
            passed=False,
            score=0.0,
            reason="; ".join(violations),
        )
    return DimensionResult(
        name="anti_pattern_compliance",
        passed=True,
        score=1.0,
        reason="no forbidden tables/columns used",
    )


# ---------------------------------------------------------------------------
# Dimension 9: Skill Path Correctness
# ---------------------------------------------------------------------------


def check_skill_path_correctness(
    trace: list[dict],
    golden: GoldenTest,
) -> DimensionResult:
    esp = golden.expected_skill_path
    if not esp.required_skills:
        return _skip("skill_path_correctness", "no required_skills defined")

    # Extract tool-use steps from trace
    tool_steps = [s for s in trace if s.get("type") == "tool_use"]

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
            name="skill_path_correctness",
            passed=False,
            score=0.0,
            reason=f"Missing skill invocations: {', '.join(missing)}",
        )

    # Check sequence if required
    if esp.sequence_matters and matched_indices != sorted(matched_indices):
        return DimensionResult(
            name="skill_path_correctness",
            passed=False,
            score=0.0,
            reason="Skills invoked out of expected order",
        )

    return DimensionResult(
        name="skill_path_correctness",
        passed=True,
        score=1.0,
        reason=f"All {len(esp.required_skills)} required skills invoked correctly",
    )
