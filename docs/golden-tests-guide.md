# Golden Tests Guide

This guide walks you through setting up golden tests so you can run `bi-evals run` end-to-end.

## Quick Start

```bash
# 1. Scaffold a project (if you haven't already)
uv run bi-evals init --dir /tmp/my-evals

# 2. cd into the project
cd /tmp/my-evals

# 3. Edit bi-evals.yaml (see "Configure bi-evals.yaml" below)

# 4. Create golden tests in golden/ (see "Writing Golden Tests" below)

# 5. Preview the generated Promptfoo config
uv run bi-evals run --dry-run

# 6. Run the eval suite
uv run bi-evals run
```

---

## Configure bi-evals.yaml

After scaffolding, edit `bi-evals.yaml` to point to your actual resources:

```yaml
project:
  name: "My BI Agent Evals"

agent:
  type: "anthropic_tool_loop"
  model: "claude-sonnet-4-5-20250929"
  system_prompt: "path/to/your/system-prompt.md"   # your agent's system prompt
  tools:
    - name: read_skill_file
      type: file_reader
      config:
        base_dir: "path/to/your/skill/"             # your existing skill/knowledge files
  max_rounds: 10

database:
  type: snowflake
  connection:
    account: "${SNOWFLAKE_ACCOUNT}"
    user: "${SNOWFLAKE_USER}"
    private_key_path: "${SNOWFLAKE_PRIVATE_KEY_PATH}"
    private_key_passphrase: "${SNOWFLAKE_PRIVATE_KEY_PASSPHRASE}"  # optional
    warehouse: "${SNOWFLAKE_WAREHOUSE}"
    database: "${SNOWFLAKE_DATABASE}"
    schema: "${SNOWFLAKE_SCHEMA}"
  query_timeout: 30

golden_tests:
  dir: "golden/"                                    # where your golden test YAMLs live

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
    - anti_pattern_compliance       # Phase 6c — opt-in via per-golden anti_patterns
  thresholds:
    completeness: 0.95
    precision: 0.95
    value_tolerance: 0.0001
  repeats: 1                         # Phase 6a — N>1 runs each golden N times
  stale_after_days: 180              # Phase 6b — warn when last_verified_at older than this
```

### Multi-model evaluation (Phase 6a)

Replace `agent.model` with `agent.models` (a list) to run every golden against multiple models in one pass:

```yaml
agent:
  type: "anthropic_tool_loop"
  models:
    - claude-sonnet-4-5-20250929
    - claude-opus-4-5-20251001
  # ... rest unchanged
```

`model` and `models` are mutually exclusive — set exactly one.

Set your environment variables (or create a `.env` file):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export SNOWFLAKE_ACCOUNT=...
export SNOWFLAKE_USER=...
export SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
export SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=      # leave empty if key is unencrypted
export SNOWFLAKE_WAREHOUSE=...
export SNOWFLAKE_DATABASE=...
export SNOWFLAKE_SCHEMA=...
```

---

## Writing Golden Tests

Each golden test is a YAML file in the `golden/` directory. You can organize them in subdirectories by category.

### Directory structure

```
golden/
  revenue/
    enterprise-revenue.yaml
    monthly-trend.yaml
  orders/
    order-count.yaml
    order-by-status.yaml
  accounts/
    active-accounts.yaml
```

### Minimal golden test

The simplest golden test only needs an `id`, `question`, and `reference_sql`:

```yaml
# golden/orders/order-count.yaml
id: orders-001
category: orders
question: "How many orders are there in total?"

reference_sql: |
  SELECT COUNT(*) AS ORDER_COUNT
  FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
```

This will evaluate:
- **execution** — does the agent's SQL run?
- **table_alignment** — does it query the right table?
- **filter_correctness** — does the WHERE clause match?

### Golden test with column checks

Add `expected.required_columns` to check that specific columns appear in the result:

```yaml
# golden/revenue/enterprise-revenue.yaml
id: rev-001
category: revenue
difficulty: medium
question: "Show me the last 6 months of revenue for account 48821"

reference_sql: |
  SELECT ABLY_ACCOUNT_ID, DT_INVOICE_MONTH, INVOICE_VALUE
  FROM MY_DB.MY_SCHEMA.V_UNIFIED_REVENUE
  WHERE ABLY_ACCOUNT_ID = 48821
    AND DT_INVOICE_MONTH >= DATEADD(MONTH, -6, DATE_TRUNC('MONTH', CURRENT_DATE()))
  ORDER BY DT_INVOICE_MONTH DESC

expected:
  min_rows: 1
  required_columns:
    - ABLY_ACCOUNT_ID
    - DT_INVOICE_MONTH
    - INVOICE_VALUE

tags: [revenue, enterprise]
notes: "Must use V_UNIFIED_REVENUE, not ACCOUNT_INVOICES"
```

### Golden test with row comparison

Enable `row_comparison` to check that the agent's result set matches the reference:

```yaml
# golden/orders/order-by-status.yaml
id: orders-002
category: orders
difficulty: easy
question: "How many orders are there per status?"

reference_sql: |
  SELECT O_ORDERSTATUS, COUNT(*) AS ORDER_COUNT
  FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
  GROUP BY O_ORDERSTATUS

expected:
  min_rows: 1
  required_columns:
    - O_ORDERSTATUS
    - ORDER_COUNT
  row_comparison:
    enabled: true
    key_columns: [O_ORDERSTATUS]        # join reference and generated results on this
    value_columns: [ORDER_COUNT]         # compare these values between matched rows
    completeness_threshold: 0.95         # 95% of reference rows must be in generated
    precision_threshold: 0.95            # 95% of generated rows must be in reference
    value_tolerance: 0.0001              # numeric tolerance for value comparison
    ignore_order: true

tags: [orders, aggregation]
```

### Golden test with skill path validation

Add `expected_skill_path` to verify the agent reads the right files in the right order:

```yaml
# golden/revenue/monthly-trend.yaml
id: rev-002
category: revenue
difficulty: hard
question: "Show monthly revenue trend for the last 12 months"

expected_skill_path:
  required_skills:
    - tool: read_skill_file
      input_contains: "SKILL.md"
    - tool: read_skill_file
      input_contains: "REVENUE.md"
  sequence_matters: true       # SKILL.md must be read before REVENUE.md
  allow_extra_skills: true     # agent can read other files too

reference_sql: |
  SELECT DATE_TRUNC('MONTH', ORDER_DATE) AS MONTH, SUM(REVENUE) AS TOTAL
  FROM MY_DB.MY_SCHEMA.REVENUE_TABLE
  GROUP BY MONTH
  ORDER BY MONTH DESC
  LIMIT 12

expected:
  min_rows: 1
  required_columns:
    - MONTH
    - TOTAL

tags: [revenue, trend]
```

### Golden test with anti-patterns (Phase 6c)

Use `anti_patterns` to ban specific tables or columns the agent must NOT use. Useful when the right shape can be reached the wrong way (e.g. summing a cumulative column instead of a daily delta):

```yaml
# golden/cases/daily-cases.yaml
id: cases-002
category: cases
question: "How many new confirmed COVID-19 cases were there in the US on 2023-01-15?"

reference_sql: |
  SELECT SUM(DIFFERENCE) AS DAILY_NEW_CASES
  FROM COVID19_DATA.PUBLIC.JHU_COVID_19
  WHERE CASE_TYPE = 'Confirmed'
    AND COUNTRY_REGION = 'United States'
    AND DATE = '2023-01-15'

anti_patterns:
  forbidden_tables:
    - LEGACY_REVENUE                 # bare name → matches any schema-qualified form
  forbidden_columns:
    - JHU_COVID_19.CASES             # qualified → only flags this table.column
    - gross_revenue                  # bare → flags any column named gross_revenue
```

Matching rules:
- `forbidden_tables: [RAW_ORDERS]` flags `RAW_ORDERS`, `FINANCE.RAW_ORDERS`, and `RAW_ORDERS o` (alias).
- `forbidden_columns: [TBL.COL]` flags only references resolvable to that table (alias-aware). CTE-laundered references still flag.
- `forbidden_columns: [COL]` flags any reference to a column with that name.

The `anti_pattern_compliance` dimension is non-critical by default — a violation lowers the weighted score but doesn't hard-fail. Add it to `scoring.critical_dimensions` if you want it to gate.

### Golden test with last_verified_at (Phase 6b)

Add `last_verified_at` to mark when you last hand-verified the reference SQL still matches reality. Goldens older than `scoring.stale_after_days` (default 180) trigger a warning at run time and appear in the report's freshness section:

```yaml
id: rev-001
question: "..."
reference_sql: |
  SELECT ...
last_verified_at: 2026-04-15
```

Goldens with no `last_verified_at` show in the "unverified" list — useful for retroactively triaging older tests.

### Golden test with value checks

Use `checks` to validate specific constraints on the result:

```yaml
# golden/accounts/active-accounts.yaml
id: acct-001
category: accounts
question: "How many active accounts do we have?"

reference_sql: |
  SELECT COUNT(*) AS ACTIVE_COUNT
  FROM MY_DB.MY_SCHEMA.ACCOUNTS
  WHERE STATUS = 'active'

expected:
  min_rows: 1
  required_columns:
    - ACTIVE_COUNT
  checks:
    - column: ACTIVE_COUNT
      condition: type
      value: positive_number

tags: [accounts]
```

---

## Full Schema Reference

```yaml
# Required
id: string                    # unique test identifier
question: string              # the question to ask the agent

# Optional metadata
category: string              # grouping (revenue, orders, etc.)
difficulty: string             # easy, medium, hard
tags: [string, ...]            # for filtering with --filter
notes: string                  # human notes about this test

# Reference SQL — the "correct" query
reference_sql: |
  SELECT ...

# Phase 6b: ISO date the reference_sql was last hand-verified.
# Optional. Older than scoring.stale_after_days → warning at run time.
last_verified_at: 2026-04-15

# Phase 6c: structural ban-list checked by anti_pattern_compliance dimension.
# Optional. Omit entirely → dimension passes vacuously.
anti_patterns:
  forbidden_tables: [string, ...]    # bare name matches schema-qualified forms
  forbidden_columns: [string, ...]   # "TABLE.COL" or bare "COL"

# Expected skill path — verify agent reasoning
expected_skill_path:
  required_skills:
    - tool: string             # tool name (e.g., "read_skill_file")
      input_contains: string   # substring that must appear in tool input
  sequence_matters: bool       # default: true — enforce order
  allow_extra_skills: bool     # default: true — allow extra tool calls

# Expected results — what the output should look like
expected:
  min_rows: int                # minimum row count (default: 0)
  required_columns:            # columns that must appear in result
    - COLUMN_NAME
  checks:                      # per-column value constraints
    - column: string
      condition: string        # "type", "equals", "contains"
      value: any               # e.g., "positive_number", 42, "active"
  row_comparison:              # full row-level comparison
    enabled: bool              # default: false — must opt in
    key_columns: [string]      # columns to join on
    value_columns: [string]    # columns to compare values
    completeness_threshold: float  # default: 0.95
    precision_threshold: float     # default: 0.95
    value_tolerance: float         # default: 0.0001
    ignore_order: bool             # default: true
```

---

## Running Tests

```bash
# Run all golden tests
uv run bi-evals run

# Preview generated Promptfoo config without running
uv run bi-evals run --dry-run

# Run only tests matching a pattern (matches id, category, or tags)
uv run bi-evals run --filter revenue
uv run bi-evals run --filter rev-001
uv run bi-evals run --filter enterprise

# Run each golden N times (overrides scoring.repeats; useful for variance checks)
uv run bi-evals run --repeats 3

# Disable Promptfoo's provider cache (force fresh API calls)
uv run bi-evals run --no-cache

# Skip the cost-estimate confirmation prompt
uv run bi-evals run --yes

# Use a different config file
uv run bi-evals run -c path/to/bi-evals.yaml
```

For the full set of CLI commands (report, compare, cost, flakiness, ingest), see [feature_summary.md](./feature_summary.md).

---

## Tips

1. **Start small** — begin with 3-5 simple golden tests (just `id`, `question`, `reference_sql`). Add complexity as you gain confidence.

2. **Use categories** — organize tests by domain (revenue, orders, accounts). This lets you run subsets with `--filter`.

3. **Add skill paths for routing tests** — `expected_skill_path` is the best way to catch routing regressions (agent reads wrong knowledge file).

4. **Enable row_comparison selectively** — only for queries where you need to validate actual data values. Simple structural tests (does it query the right table?) don't need it.

5. **Every production failure becomes a golden test** — when the agent gets a question wrong in production, add it as a golden test. This builds your regression suite over time.

6. **Use `--dry-run` to debug** — if tests aren't being picked up, check the generated config to verify your golden tests are being loaded correctly.
