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
- Compare two runs for regressions: `bi-evals compare latest prev`.
- Add a golden for every production failure you find — the dataset grows from real misses.

---

## Common pitfalls

- **"No SQL found / could be extracted"** — your agent's answer didn't contain the SQL in a
  scorable form. Submit `generated_sql` directly, or ensure the SQL is fenced/bare-SELECT in
  `response_text`. (push)
- **`skill_path_correctness` fails though the agent read the files** — you didn't submit a
  `trace`, or it lacks `tool_name`/`tool_input`. Capture the trace, or drop the dimension if
  your agent has no tools. (push)
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
