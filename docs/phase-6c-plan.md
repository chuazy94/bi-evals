# Phase 6c: Anti-Patterns — Forbidden Tables and Columns

## Context

Phases 6a and 6b make the eval signal trustworthy and explainable, but they don't change *what* gets tested. Today a golden has one success definition: did the generated SQL produce the right rows? That misses an important class of failure — **queries that accidentally produce the right answer via the wrong mechanism.**

Example: the agent writes a query that pulls revenue from `RAW_ORDERS` instead of the canonical `V_UNIFIED_REVENUE` view. Result rows happen to match because the test question is simple enough. The test passes — but on a more complex question, or when the raw data diverges from the view, the agent will fail silently because it's learned the wrong pattern.

Phase 6c adds **anti-patterns** to the golden schema: constraints saying "a correct solution for this question must NOT use table/column X." The scorer checks the generated SQL against these constraints using the existing sqlglot parser. Pass/fail becomes interpretable: "used forbidden table RAW_ORDERS — the canonical source is V_UNIFIED_REVENUE."

Scope is deliberately tight: **forbidden tables and forbidden columns only**, via structural SQL analysis. No keyword matching, no required-filter checking — those add scope without meaningfully more signal. Anti-refusal tests and hallucination detection (previously discussed) are explicitly out of scope; the framing shift to "pattern constraints on existing goldens" made them a different kind of thing.

---

## Feature: `anti_patterns` on goldens

### Schema addition

Extend `GoldenTest` with an optional `anti_patterns` field:

```yaml
id: revenue-by-region
question: "What's Q3 2026 revenue by region?"
category: revenue

reference_sql: |
  SELECT region, SUM(amount) AS revenue
  FROM V_UNIFIED_REVENUE
  WHERE DATE_TRUNC('quarter', order_date) = '2026-07-01'
  GROUP BY region;

anti_patterns:
  forbidden_tables:
    - RAW_ORDERS           # Use V_UNIFIED_REVENUE instead
    - LEGACY_REVENUE_V2    # Deprecated; replaced by V_UNIFIED_REVENUE
  forbidden_columns:
    - ACCOUNT_INVOICES.amount     # Known gotcha: not in the right base currency
    - RAW_ORDERS.gross_revenue    # Doesn't include returns
```

Both fields optional; omitting either skips the corresponding check. An empty `anti_patterns` block is equivalent to not having the field at all.

### Pydantic model

```python
class AntiPatterns(BaseModel):
    forbidden_tables: list[str] = Field(default_factory=list)
    forbidden_columns: list[str] = Field(default_factory=list)  # "TABLE.COLUMN" or bare "COLUMN"

class GoldenTest(BaseModel):
    # ... existing fields
    anti_patterns: AntiPatterns | None = None
```

Column entries support two forms:
- `"TABLE.COLUMN"` — checks that this exact table+column pair doesn't appear together
- `"COLUMN"` — checks that any reference to this column name is absent (more aggressive; use sparingly)

Table/column names are case-insensitive (we uppercase both sides at check time, matching Snowflake's default identifier resolution — same as the existing scorer's behavior).

### Scorer changes

The 9-dimension scorer gets a **new dimension: `anti_pattern_compliance`**. Not a 10th tacked on unevenly — integrated into the existing scoring pipeline with standard weighting and criticality config.

```python
# src/bi_evals/scorer/dimensions.py

def score_anti_pattern_compliance(
    generated_sql: str,
    golden: GoldenTest,
) -> DimensionResult:
    """Fails if generated SQL uses any forbidden tables or columns."""
    if not golden.anti_patterns or (
        not golden.anti_patterns.forbidden_tables
        and not golden.anti_patterns.forbidden_columns
    ):
        # No constraints defined → dimension vacuously passes (weight 0 effectively)
        return DimensionResult(passed=True, score=1.0, reason="no anti-patterns defined")

    violations = _check_anti_patterns(generated_sql, golden.anti_patterns)
    if violations:
        return DimensionResult(
            passed=False,
            score=0.0,
            reason="; ".join(violations),
        )
    return DimensionResult(passed=True, score=1.0, reason="no forbidden tables/columns used")
```

The `_check_anti_patterns` helper uses sqlglot to extract tables and columns from the generated SQL (already imported in `scorer/sql_utils.py`) and sets-intersects against the forbidden lists:

```python
def _check_anti_patterns(sql: str, patterns: AntiPatterns) -> list[str]:
    violations = []

    used_tables = extract_tables(sql)  # existing helper
    forbidden_tables_upper = {t.upper() for t in patterns.forbidden_tables}
    table_violations = used_tables & forbidden_tables_upper
    for tbl in sorted(table_violations):
        violations.append(f"forbidden table used: {tbl}")

    used_columns = extract_columns_with_tables(sql)  # new helper; returns {(TABLE, COLUMN)}
    for spec in patterns.forbidden_columns:
        if "." in spec:
            tbl, col = spec.upper().split(".", 1)
            if (tbl, col) in used_columns:
                violations.append(f"forbidden column used: {tbl}.{col}")
        else:
            bare = spec.upper()
            if any(col == bare for _, col in used_columns):
                violations.append(f"forbidden column used: {bare}")

    return violations
```

The only new sqlglot helper needed is `extract_columns_with_tables`, which walks the AST and resolves column references to their owning table via alias lookup. Sqlglot has this ability built-in; we just need to wire it up.

### Config integration

`anti_pattern_compliance` joins the existing dimension list in `bi-evals.yaml`:

```yaml
scoring:
  dimensions:
    - execution
    - table_alignment
    - column_alignment
    - filter_correctness
    - row_completeness
    - row_precision
    - value_accuracy
    - no_hallucinated_columns
    - skill_path_correctness
    - anti_pattern_compliance      # NEW
  dimension_weights:
    # ... existing weights
    anti_pattern_compliance: 2.0   # Suggested default
  # Critical? Debatable. I lean NOT critical by default — an anti-pattern violation
  # on a test that otherwise produced correct output is a warning, not a hard failure.
  # Users who want it critical can opt in.
```

Backward compat: goldens without `anti_patterns` defined get a vacuous pass (score=1.0, weight applies but contributes nothing distinguishing). Existing configs don't need to change; adding the dimension to `dimensions` is opt-in.

### Scoring philosophy

The dimension is non-critical by default. Rationale:
- A test that got the right answer via a forbidden table is still a partial success — the agent can produce correct SQL, just not ideal SQL
- Making it critical would punish teams who retrofit anti-patterns onto existing goldens with strict expectations
- Teams that want it critical can add `anti_pattern_compliance` to `scoring.critical_dimensions` in their config

For teams that care about this strongly, setting the weight high (e.g., 3.0, same as critical dims) effectively makes it gate the weighted score without making it an absolute pass/fail gate.

### Reporting

In the single-run report, `anti_pattern_compliance` appears in the dimension pass-rate list like any other dimension. No new section needed. The per-test drilldown (in Phase 8 UI) shows which specific forbidden tables/columns were violated via the `reason` field.

For now, the existing report already shows `reason` inline for failed dimensions. That's enough for CLI-era debugging.

### Compare semantics

Anti-pattern regressions participate in compare like any other dimension. A test that newly violates an anti-pattern shows up in `regressed` (if `anti_pattern_compliance` becomes critical, or if overall score drops below threshold) or gets flagged in the dimension deltas table.

No special handling needed — Phase 6a's rate-based compare logic already handles per-dimension deltas.

---

## File changes

| File | Action | Purpose |
|---|---|---|
| `src/bi_evals/golden/model.py` | Modify | Add `AntiPatterns` and `anti_patterns` field on `GoldenTest` |
| `src/bi_evals/scorer/sql_utils.py` | Modify | Add `extract_columns_with_tables()` helper |
| `src/bi_evals/scorer/dimensions.py` | Modify | Add `score_anti_pattern_compliance()` dimension function |
| `src/bi_evals/scorer/entry.py` | Modify | Wire new dimension into scorer pipeline |
| `src/bi_evals/config.py` | Modify | `anti_pattern_compliance` in default dimension list + weight |
| `tests/test_anti_patterns.py` | New | SQL parsing, violation detection, dimension integration |
| `tests/test_golden.py` | Modify | Test loading of `anti_patterns` YAML field |
| `tests/fixtures/eval_sample/golden/...` | Modify | Add one fixture golden with anti-patterns for end-to-end test |

No DB schema changes. `anti_pattern_compliance` is one more dimension row in the existing `dimension_results` table.

---

## Example walkthrough

Golden:
```yaml
id: revenue-by-region
question: "What's Q3 revenue by region?"
reference_sql: "SELECT region, SUM(amount) FROM V_UNIFIED_REVENUE ..."
anti_patterns:
  forbidden_tables: [RAW_ORDERS]
```

Agent generates:
```sql
SELECT region, SUM(amount) FROM RAW_ORDERS WHERE quarter = 'Q3' GROUP BY region;
```

Scorer output:
- `execution`: pass (SQL runs)
- `row_completeness`: pass (rows match)
- `value_accuracy`: pass (values happen to match)
- `table_alignment`: fail (used `RAW_ORDERS`, expected `V_UNIFIED_REVENUE`)
- `anti_pattern_compliance`: fail — reason: "forbidden table used: RAW_ORDERS"

Note the overlap with `table_alignment`: when the expected table is known and the used table is forbidden, both dimensions may fail on the same test. That's fine — they capture related-but-different signals. `table_alignment` is about matching the reference SQL; `anti_pattern_compliance` is about violating an explicit ban that may apply regardless of the reference SQL.

---

## Risks / Gotchas

- **sqlglot column-to-table resolution is imperfect.** Complex queries with CTEs, subqueries, and aliasing can confuse the resolver. For cases where resolution is ambiguous, fall back to bare column name matching and log a warning. Tests should cover CTE and subquery cases explicitly.
- **Case sensitivity.** Snowflake normalizes unquoted identifiers to uppercase; we already do this in the existing scorer. Follow the same convention. Document that quoted identifiers are matched literally.
- **False negatives via aliasing.** `FROM RAW_ORDERS AS o` vs `FROM RAW_ORDERS` — sqlglot handles this correctly; write a test to confirm.
- **Dynamic SQL / EXECUTE IMMEDIATE.** Not worrying about it. If the agent generates dynamic SQL, it's broken in other ways first.
- **Cross-schema collisions.** `FINANCE.RAW_ORDERS` vs `LEGACY.RAW_ORDERS` — sqlglot gives us the fully-qualified name when available. Forbidden lists should specify schema-qualified names when needed; bare names match any schema. Document this.
- **Retrofit pain.** Teams with existing golden suites won't have `anti_patterns` defined and will see `anti_pattern_compliance` show 100% pass rate (vacuous). That's correct behavior but might be confusing in reports — consider excluding vacuous dimensions from the pass-rate display when they contribute no signal.

---

## Verification

```bash
# Unit tests
uv run python -m pytest tests/test_anti_patterns.py -v

# Full suite regressions
uv run python -m pytest tests/ -m "not integration" -v

# Manual: golden with anti-patterns
# Add anti_patterns to one golden in tmp/my-evals/, then:
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter <that-category>
# Expect: if agent uses the forbidden table, dimension fails with a clear reason

# Manual: existing goldens unaffected
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run
# Expect: no behavioral change for goldens without anti_patterns defined
```

Success criteria:
- Golden YAML schema accepts `anti_patterns` with `forbidden_tables` and `forbidden_columns`
- New `anti_pattern_compliance` dimension appears in all runs
- Goldens without `anti_patterns` defined get vacuous pass (no behavior change)
- Goldens with `anti_patterns` correctly flag violations with sqlglot-based structural analysis
- Existing 187 tests still pass
- Integration test demonstrates an end-to-end violation → failed dimension → visible reason in report
