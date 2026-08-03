# Semantic-layer scoring (design / proposal)

> **Status: proposal, post-MVP.** `CLAUDE.md` lists "No semantic layer integration" as an explicit
> MVP non-goal, and the north star is first-run setup speed. This is a genuinely valuable capability
> but should be framed as **Build Stage 6, which builds on Build Stage 2 (open-envelope trace)** — see
> `STATUS.md`'s "Remaining — Build Stages" — not as an MVP item. This doc records the review + research
> from the discussion so the design is captured before any code.

## The gap

bi-evals is well set up for three things today:

- **SQL generation + result scoring** — 10 binary dimensions split into *structural*
  (`table_alignment`, `column_alignment`, `filter_correctness`, `no_hallucinated_columns`) and
  *execution-grounded* (`execution`, `row_completeness`, `row_precision`, `value_accuracy`). Generated
  and `reference_sql` both execute on **bi-evals' own** DB connection and the result sets are
  compared — execution accuracy, the gold standard, with the yardstick never routed through the
  agent's tools.
- **Knowledge / skill tracing** — `skill_path_correctness` checks the agent invoked the right tools
  with the right inputs, in order (the generic form of Promptfoo's `trajectory:` assertions).
- **Result scoring** — row completeness/precision and value accuracy against gold rows.

What's missing is **"right for the right reason."** The only notion of semantic correctness today is
indirect:

- `skill_path_correctness` checks the agent *read* the right knowledge file — not that it *applied*
  the definition correctly.
- `anti_patterns` is a negative structural guard (forbidden tables/columns).
- Result-matching can pass while the semantic logic is wrong: an agent can hit the right numbers
  using the wrong metric definition, the wrong grain, or a coincidentally-correct aggregation.
  Result accuracy alone can't separate "right for the right reason" from "right by accident," and it
  says nothing about *why* a wrong answer is wrong.

Semantic-layer scoring closes that gap.

## Research: semantic layers share one vocabulary, differ only in dialect

Checked dbt MetricFlow / Semantic Layer, Snowflake Semantic Views + Cortex Analyst, and Cube, plus
prior art on evaluating text-to-semantic-layer agents (notably dbt's own `dbt-llm-sl-bench`). The key
finding: the vendors share one conceptual vocabulary; only the **query surface** differs.

| Concept | dbt SL | Snowflake Semantic View | Cube |
|---|---|---|---|
| Aggregations / KPIs | **metrics** (built on measures) | **METRICS** (built on FACTS) | **measures** |
| Slice / group attributes | **dimensions** (`entity__dim`) | **DIMENSIONS** | **dimensions** |
| Join keys | **entities** | **RELATIONSHIPS** | **joins** |
| Time bucketing | **grain** (`metric_time__month`) | time dimension | `timeDimensions.granularity` |
| Filters | **where** (`Dimension(...)`) | WHERE | **filters** |
| Query shape | `query(metrics=[...], group_by=[...], where=...)` | `SELECT ... FROM SEMANTIC_VIEW(v METRICS ... DIMENSIONS ...)` | REST / GraphQL JSON |

Differences are *dialect*, not *concept*. dbt hands you a structured query object; Snowflake encodes
the selection inside SQL (`SEMANTIC_VIEW(...)`), or Cortex Analyst compiles straight to physical-table
SQL; Cube uses a JSON query. The literature converges on two evaluation layers:

1. **Execution accuracy** against gold results — bi-evals already has this.
2. **Selection accuracy** — did the agent pick the correct metrics / dimensions / grain / filters.

dbt's benchmark runs the same NL questions across `sql`, `semantic_layer`, and `mcp` strategies and
compares to gold — validation that this is the right framing.

Sources:
- dbt Semantic Layer / MetricFlow — <https://docs.getdbt.com/docs/build/about-metricflow>, JDBC query
  shape <https://docs.getdbt.com/docs/dbt-apis/sl-jdbc>
- Snowflake Semantic Views — <https://docs.snowflake.com/en/user-guide/views-semantic/overview>,
  Cortex Analyst <https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst>
- dbt LLM Semantic Layer benchmark — <https://github.com/dbt-labs/dbt-llm-sl-bench>

### OSI / Apache Ossie — an emerging open standard for the *model* (not the query)

Apache Ossie (incubating; formerly Open Semantic Interchange / OSI) is a vendor-neutral, Apache-2.0
YAML standard for semantic metadata, backed by 50+ orgs including Snowflake, Databricks, and dbt Labs.
Its self-described purpose is to kill *"Metric Drift"* (inconsistent KPIs across dashboards) and AI
*"Hallucinations"* from conflicting data logic — the same failure modes `metric_definition_integrity`
below targets. Worth folding into this plan, but with three precise boundaries, because the spec is
narrower than "a canonical semantic layer format" might suggest:

- **OSI is a MODEL format, not a QUERY format.** It defines `SemanticModel` → `datasets` → `fields` /
  `relationships` / `metrics`. It does **not** define what an agent *selected* for a given question.
  So OSI does **not** replace this plan's canonical `SemanticQuery` envelope, and the four *selection*
  dimensions (`metric_selection`, `dimension_selection`, `semantic_grain_correctness`,
  `semantic_filter_correctness`) still need the per-vendor query parsers as designed.
- **Where OSI *does* fit: `metric_definition_integrity`.** OSI's `metrics[].expression` is exactly the
  metric formula that dimension checks. If vendors ship OSI exports, the plan's N bespoke definition
  loaders (`semantic_manifest.json` for dbt, view DDL for Snowflake, `/meta` for Cube) collapse toward
  **one OSI loader** — a standards-aligned source for definition integrity / semantic-drift detection.
- **Grain is not in OSI.** OSI marks time only with a boolean `dimension.is_time` role flag — no
  explicit grain levels (month/quarter/day). `semantic_grain_correctness` must therefore keep sourcing
  grain from the query surface, *not* from an OSI model.

**Maturity flag:** Ossie is still incubating (*"has yet to be fully endorsed by the ASF"*). Consume it
as an *optional input* to definition loading now; do **not** harden bi-evals' canonical schema against
it until it graduates. Revisit whether OSI should become the canonical definition source once stable.

Sources:
- Apache Ossie project — <https://ossie.apache.org/>
- Core spec — <https://github.com/apache/ossie/blob/main/core-spec/spec.md>

## Design: normalize once, score once (same move as the contract pivot)

The unifying abstraction is a **canonical semantic query** — one normalized representation of the
semantic selection, with a thin per-vendor parser mapping each dialect into it. The scorer only ever
sees the canonical form, so it stays vendor-agnostic. This mirrors the "one canonical contract, many
adapters" architecture the pivot already established.

### 1. Canonical envelope

```python
@dataclass
class SemanticQuery:
    source: str | None              # semantic model / view name
    metrics: list[str]              # ["revenue", "order_count"]
    dimensions: list[str]           # group_by, normalized ["customer__region"]
    filters: list[SemanticFilter]   # (field, operator, value), normalized
    grain: dict[str, str]           # {"metric_time": "month"}
    order_by: list[str]
    limit: int | None
```

### 2. Vendor parsers behind a Protocol

Mirrors the existing `Tool` / `DatabaseClient` / `Adapter` protocols.

```python
class SemanticLayerParser(Protocol):
    def parse_query(self, raw: Any) -> SemanticQuery: ...
    def load_definitions(self) -> dict[str, MetricDef]: ...   # for definition integrity
```

- `DbtSemanticLayerParser` — parses the MetricFlow query object / MCP tool args; reads
  `semantic_manifest.json` for definitions.
- `SnowflakeSemanticViewParser` — parses the `SEMANTIC_VIEW(... METRICS ... DIMENSIONS ...)` clause
  out of the generated SQL (sqlglot / regex); reads the view DDL/YAML for definitions.
- `CubeParser` — parses the JSON query; `/meta` endpoint for definitions.

### 3. Where the semantic query comes from at scoring time

This is where **Build Stage 2 (open-envelope trace)** pays off: `semantic_query` is just another
optional key the customer over-captures; the scorer reads it if present. Three sources, increasing
effort:

- **Snowflake / Cortex** — parse it out of `generated_sql`. **Zero extra agent instrumentation** —
  the semantic selection is already inside the SQL bi-evals captures today.
- **dbt / Cube via push or BYO** — the customer includes the structured SL query their agent already
  constructs.
- **Definition integrity** — bi-evals loads the semantic model artifact via a new opt-in config block
  (manifest path / view name / API URL).

### 4. New golden fields (opt-in, vacuous-pass like `anti_patterns`)

```yaml
expected_semantic:
  source: orders
  metrics: [revenue]
  dimensions: [customer__region]
  grain: { metric_time: month }
  filters:
    - { field: customer__status, operator: "=", value: active }
  metric_definitions:                 # optional — definition integrity
    revenue: "sum(order_total)"
```

### 5. New scoring dimensions

The two readings of "the correct semantic logic is used" map cleanly:

- `metric_selection` — set-match the chosen metrics (candidate **critical** dimension).
- `dimension_selection` — set-match the group-by dimensions.
- `semantic_grain_correctness` — time grain matches.
- `semantic_filter_correctness` — normalized semantic filter set (distinct from raw SQL `WHERE`
  parsing, which breaks on SL-compiled SQL).
- `metric_definition_integrity` — the *definition* behind the selected metric matches expected. This
  catches what result-matching misses: wrong metric with a coincidentally-right number, or
  semantic-layer **drift** (someone changed `revenue`'s formula upstream). It is the deepest "correct
  semantic logic" signal and a natural complement to the existing `prompt_snapshot` drift detection —
  call it *semantic drift detection*.

Execution stays the yardstick: compile/run the gold semantic query (MetricFlow compile, or run the
`SEMANTIC_VIEW` query) to produce gold rows, feeding the existing `row_completeness` /
`value_accuracy`. The semantic dimensions explain *why* a result is right or wrong.

## How a dimension is added (current mechanics)

There is no dynamic dimension registry today; adding one touches a few files (this is the existing
pattern, recorded here so the work is scoped):

1. `scorer/dimensions.py` — `check_<name>(...) -> DimensionResult` with a stable string key; use
   `_skip()` when the golden has nothing to evaluate; return only `score` 0.0 / 1.0.
2. `config.py` — append to `ALL_DIMENSIONS`; optionally add to `DEFAULT_DIMENSION_WEIGHTS` and/or
   `DEFAULT_CRITICAL_DIMENSIONS`.
3. `scorer/entry.py` — import the checker; add an `if "<name>" in enabled:` block with any gating;
   pass the right inputs.
4. `golden/model.py` — add the `expected_semantic` Pydantic model + field.
5. Tests + `tmp/my-evals/` demo golden (per project conventions for user-visible fields).

## Recommended sequencing

- **Frame as Build Stage 6** that depends on Build Stage 2's open envelope — not MVP.
- **Start with Snowflake end-to-end.** Cheapest first target for this stack: already on Snowflake,
  and the semantic selection is parseable straight out of the generated SQL — no new agent capture.
  Prove the canonical schema + dimensions + one parser, then add dbt/Cube as pure adapters.

Suggested slices:

1. `SemanticQuery` + `expected_semantic` schema + `metric_selection` / `dimension_selection` /
   `semantic_grain_correctness` scoring a hand-supplied canonical query.
2. `SnowflakeSemanticViewParser` — parse the canonical query from `generated_sql`.
3. `metric_definition_integrity` + definition loading (semantic drift detection).
4. `DbtSemanticLayerParser` and `CubeParser` as additional adapters.

## Open questions

- **Critical vs. important tier** for `metric_selection` — fail the test outright on wrong metric, or
  let the weighted threshold decide? (Leaning critical — wrong metric is rarely acceptable.)
- **Definition matching strictness** — *leaning normalized AST (sqlglot).* OSI stores metric formulas
  as dialect-specific SQL in `metrics[].expression`, so exact-string comparison would thrash across
  dialects; normalized sqlglot AST (already a dependency) is the robust choice. Open sub-question: the
  per-dialect parse target when the expression's dialect differs from the execution warehouse.
- **Filter normalization** — how far to canonicalize operators/values across dialects before
  set-comparison.
- **Gold semantic query authoring** — hand-written in the golden vs. derived by running a known-good
  query through the same parser (the latter keeps gold and parser honest).
