# Getting Started with bi-evals

This walks you through setting up a brand-new eval project that uses bi-evals as a
dependency. By the end you'll have your own eval suite — golden tests, a config pointed at
your warehouse — that you can run, iterate on, and view in the browser.

bi-evals **scores the response your agent produces; it never rebuilds or drives your agent.**
You run your agent however it already runs, hand bi-evals `{generated_sql, trace}` per
question, and it executes the SQL against your warehouse and scores it across ~10 dimensions.

---

## Step 0 — Pick how bi-evals gets your agent's answers (the adapter)

`agent.adapter` in `bi-evals.yaml` selects how the answers arrive. The scorer is identical
for all of them.

| Adapter | How it works | Use when |
|---|---|---|
| **`push`** (recommended on-ramp) | You run your agent offline, write its results to a JSONL, `bi-evals score --input` grades them. No live agent, no API spend during scoring. | Almost always — works for any stack (MCP, LangChain, a notebook, anything). |
| **`api_endpoint`** | bi-evals POSTs each question to your agent's HTTP endpoint and scores the response live. | Your agent is already a clean question→SQL HTTP service. |
| **`anthropic_tool_loop`** (dev-only) | bi-evals runs Claude with *your* skill files locally. Evaluates a rebuild, not your real agent. | Authoring goldens before a real agent exists. Not a production-fidelity setup. |

> **The real prerequisite, for every adapter:** your agent must expose the **SQL it
> generated**. bi-evals grades by executing that SQL — it can't score a query the agent
> never revealed. If your agent only returns a chart or a prose summary, surface the SQL
> first. See [`instrumenting-your-agent.md`](./instrumenting-your-agent.md) for the output
> shapes that work best, and [`push-limitations.md`](./push-limitations.md) for the full
> checklist.

This guide leads with **push** (the recommended path) and notes the `api_endpoint`
alternative where it differs.

---

## Prerequisites

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/).
- **Node.js** (bi-evals runs [Promptfoo](https://promptfoo.dev/) under the hood via `npx`).
- **Warehouse credentials** (Snowflake for now) — bi-evals executes SQL to grade it.
- **Your own BI agent** that exposes its generated SQL (and, ideally, its tool/file trace).

---

## Step 1 — Install bi-evals

### Option A: Add as a dependency in a fresh project (recommended)

```bash
mkdir my-evals && cd my-evals
uv init
uv add bi-evals            # or: uv add bi-evals @ file:///path/to/bi-evals for a local checkout
```

### Option B: Run straight from the bi-evals repo

```bash
cd /path/to/bi-evals
uv sync --group dev
uv run bi-evals --help
```

The rest of this guide assumes `bi-evals` resolves on your PATH (via `uv run` or an
activated venv).

---

## Step 2 — Scaffold the project

```bash
bi-evals init push --dir .
```

This writes a push-shaped `bi-evals.yaml` (`agent.adapter: push`), `.env` / `.env.example`
(Snowflake-only — push needs no agent URL), an example golden, and `golden/`, `results/`,
`reports/` directories.

(For a live HTTP agent use `bi-evals init api_endpoint` — it additionally ships
`adapter_example.py`, a FastAPI shim demonstrating the response contract; see Step 5b. For
the dev-only driving adapter, `bi-evals init dev`.)

---

## Step 3 — Configure your warehouse

Edit the `database:` block in `bi-evals.yaml`. It uses `${ENV_VAR}` placeholders resolved
from `.env`:

```yaml
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
```

Fill the values in `.env` (next to `bi-evals.yaml`; loaded automatically):

```
SNOWFLAKE_ACCOUNT=...
SNOWFLAKE_USER=...
SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_WAREHOUSE=...
SNOWFLAKE_DATABASE=...
SNOWFLAKE_SCHEMA=...
```

### Databricks

To score against a Databricks SQL warehouse instead, pass `--warehouse databricks` to
any `init` scaffold and install the extra:

```bash
bi-evals init push --warehouse databricks    # or api_endpoint / dev
uv add "bi-evals[databricks]"                # pulls in databricks-sql-connector
```

That scaffolds the `database:` block and `DATABRICKS_*` env vars below for you; fill in
`.env` and run `bi-evals doctor` to confirm the warehouse is reachable.

```yaml
database:
  type: databricks
  connection:
    server_hostname: "${DATABRICKS_SERVER_HOSTNAME}"   # dbc-xxxx.cloud.databricks.com
    http_path: "${DATABRICKS_HTTP_PATH}"               # /sql/1.0/warehouses/xxxxxxxx
    access_token: "${DATABRICKS_TOKEN}"                # a personal access token (PAT)
    catalog: "${DATABRICKS_CATALOG}"                   # optional (Unity Catalog)
    schema: "${DATABRICKS_SCHEMA}"                      # optional
  query_timeout: 30
```

```
DATABRICKS_SERVER_HOSTNAME=dbc-xxxxxxxx-xxxx.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/xxxxxxxxxxxxxxxx
DATABRICKS_TOKEN=dapi...
DATABRICKS_CATALOG=main
DATABRICKS_SCHEMA=default
```

The scorer parses your generated and reference SQL using the Databricks (Spark) dialect
automatically — it's derived from `database.type`, so backtick-quoted identifiers and
Spark-specific syntax are handled correctly. Verify connectivity before spending anything:
`bi-evals doctor` runs a real `SELECT 1` against the configured warehouse.

---

## Step 4 — Write your first golden test

A golden is a question + the **reference SQL** for the correct answer + what should be true
of the result. One YAML file per question in `golden/`:

```yaml
id: revenue-001
category: revenue
question: "What was total revenue by region last quarter?"
reference_sql: |
  SELECT region, SUM(revenue) AS total
  FROM analytics.fct_revenue
  WHERE quarter = '2026Q1'
  GROUP BY region
  ORDER BY total DESC
expected:
  required_columns: [region, revenue]   # SOURCE columns the query must read — NOT output aliases
  row_comparison:
    enabled: true
    completeness_threshold: 0.95
    precision_threshold: 0.95
# Optional — only if you want to score the agent's reasoning path:
# expected_skill_path:
#   required_skills:
#     - tool: read_skill_file
#       input_contains: "REVENUE.md"
```

Two gotchas worth internalising now (both cause confusing failures otherwise):

- **`required_columns` are SOURCE columns, not output names.** List the underlying table
  columns the answer is computed from (e.g. `revenue`), not aliases like `TOTAL`. bi-evals
  prints a hint if you get this wrong.
- **Column order/count.** bi-evals matches result rows by column name, and falls back to
  ordinal position when your agent names columns differently — but only if the column
  *count* matches and the *order* lines up. Author `reference_sql` to output columns in the
  order/count your agent produces, or set `row_comparison.key_columns` explicitly.

See [`golden-tests-guide.md`](./golden-tests-guide.md) for the full schema.

---

## Step 5a — Score with the SDK (recommended)

The `bi_evals.Runner` SDK owns the loop, collection, file I/O, and scoring — you write
one `ask()` call against your own agent:

```python
import bi_evals

runner = bi_evals.Runner("bi-evals.yaml")
for case in runner.golden_cases():
    try:
        answer = my_agent.ask(case.question)
        runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
        # or, if your agent returns prose: response_text=answer.text
    except Exception as e:
        runner.submit(case, error=str(e))     # flaky agent → record + keep going

report = runner.score()                        # executes SQL, scores, writes report
print(f"{report.passed}/{report.total} passed → {report.report_path}")
assert report.pass_rate >= 0.9                 # gate CI on it
```

`submit()` takes `generated_sql` **or** `response_text` (raw answer — the SQL is extracted
from a ```sql fence or bare SELECT) **or** `error` (the agent failed this golden — scored as
a failing `execution`). `trace` is optional; it's needed for `skill_path_correctness`.

> The SDK removes the *plumbing*, not the requirement that your agent **expose** its SQL and
> trace — see [`push-limitations.md`](./push-limitations.md).

## Step 5a-alt — Score a JSONL file you produced (logs-only / non-Python)

If you can't call your agent from Python (e.g. you only have a log dump), write the same
rows to a `.jsonl` yourself and score the file:

```jsonl
{"golden_file": "golden/revenue-001.yaml", "response_text": "Here's the query:\n```sql\nSELECT ...\n```", "trace": {"files_read": ["REVENUE.md"]}}
{"golden_file": "golden/usage-002.yaml", "error": "agent timed out"}
```

```bash
bi-evals doctor                       # warehouse reachable? submission parses?
bi-evals score --input results.jsonl  # extract SQL → execute → score → ingest
```

(See [`push-limitations.md`](./push-limitations.md) for ways to produce the file and the
full requirements.)

## Step 5b — Alternative: point bi-evals at a live endpoint (api_endpoint)

If your agent is a clean HTTP service, set `adapter: api_endpoint` and:

```yaml
agent:
  adapter: api_endpoint
  api_endpoint:
    url: "${BI_AGENT_URL}"            # e.g. http://localhost:8000/ask
    headers:
      Authorization: "Bearer ${BI_AGENT_TOKEN}"   # optional
```

Your endpoint must return the response shape in
[`byo-response-contract.md`](./byo-response-contract.md). Validate it with `bi-evals doctor`,
then run live:

```bash
bi-evals run
```

---

## Step 6 — Look at the results

```bash
bi-evals report                 # self-contained HTML scorecard in reports/
bi-evals ui                     # interactive viewer: runs list + per-test drilldown
```

The drilldown shows, per test: the generated SQL, the reference SQL, each dimension's
pass/fail and reason, files read, and the full trace.

> **A failing test is often a *successful* test of the eval.** If your agent's SQL is wrong,
> bi-evals should fail it — that's the point. Read the per-dimension reasons to see *what*
> kind of failure (routing, filter, value, missing rows).

---

## Step 7 — Iterate

- **push:** re-run your agent, regenerate `results.jsonl`, `bi-evals score --input` again.
  Idempotent — re-scoring overwrites that run's rows.
- **api_endpoint:** change your agent, `bi-evals run` again.
- Compare two runs for regressions: `bi-evals compare prev latest` (baseline
  first, candidate second — "did `latest` get worse than `prev`?").
- Add a golden for every production failure you find — the dataset grows from real misses.

---

## Step 8 — Gate CI on it

The gate answers two independent questions, each opt-in in `bi-evals.yaml`:

```yaml
compare:
  min_pass_rate: 0.85 # absolute: is this run good enough on its own?
  fail_on: red # relative: fail when tests regressed vs the baseline
  max_regressions_allowed: 0 # tolerate N regressed tests (flaky-suite valve)
```

**CLI** — score the new run, then gate it against the previous one; a failed
gate exits 1, which fails the CI job:

```bash
bi-evals score --input results.jsonl
bi-evals compare prev latest --fail-on red    # exit 1 on regression or floor breach
```

(`--fail-on` also works without config; without either, `compare` prints the
verdict but always exits 0.)

When gating is enabled, the compare page — the HTML artifact and the
`bi-evals ui` compare view — shows a **gate strip** under the verdict banner:
passed/FAILED, the reasons (floor/budget arithmetic), and the regressed tests
by name. That's the page to attach to CI failures.

**SDK** — the same gate, as assertions:

```python
report = runner.score()
assert report.passed_gate                    # absolute floor — works on a first run
gate = report.compare_to("prev")             # regression gate vs the previous run
assert gate.passed, gate.reasons
```

**Flaky agents:** a single-trial run turns one unlucky answer into a full
pass→fail flip, which reads as a regression. Two remedies, best combined:

```yaml
scoring:
  repeats: 5 # run each golden 5×; scores become pass *rates*
compare:
  regression_threshold: 0.2 # now a 20-point rate drop, not one flipped trial
  max_regressions_allowed: 1 # budget for what still leaks through
```

With `repeats: 5`, one unlucky trial moves a test's rate by 0.2 instead of 1.0,
so real regressions still trip the gate but noise doesn't. Mind the cost:
`repeats: N` multiplies agent + warehouse spend for that run by N.

---

## Common pitfalls

- **"No SQL found / could be extracted"** — your agent's answer didn't contain the SQL in a
  scorable form. Submit `generated_sql` directly, or ensure the SQL is fenced/bare-SELECT in
  `response_text`. (push)
- **`skill_path_correctness` shows "not evaluated"** — you didn't submit a `trace`, or it
  lacks `tool_name`/`tool_input`. bi-evals doesn't fail the dimension (it can't know what the
  agent did) — it excludes it from the score, warns before scoring ("0 of N rows contain a
  usable trace"), and the report's Capability panel says what to submit to unlock it. Capture
  the trace, or drop the dimension if your agent has no tools. (push)
- **A correct answer fails `value_accuracy`/`row_completeness`** — usually column
  order/count or `required_columns` listing output aliases. See Step 4's gotchas.
- **Old flat `agent:` config rejected at load** — configs from before the adapter pivot must
  be migrated. See [`migration-adapter-schema.md`](./migration-adapter-schema.md).
- **`${VAR}` unresolved** — the variable isn't set in `.env` or the shell. bi-evals fails
  loudly at load rather than substituting empty.

---

## What to read next

- [`push-limitations.md`](./push-limitations.md) — what push requires; the push-ready checklist.
- [`golden-tests-guide.md`](./golden-tests-guide.md) — golden-test schema and authoring.
- [`plans/pivot-phases-overview.md`](./plans/pivot-phases-overview.md) — the adapter architecture and where it's heading.
- [`byo-response-contract.md`](./byo-response-contract.md) — the `api_endpoint` response shape.
- [`STATUS.md`](../STATUS.md) — what works today.
