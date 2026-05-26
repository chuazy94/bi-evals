# Getting Started with bi-evals

This walks you through setting up a brand-new eval project that uses bi-evals as a dependency. By the end you'll have your own eval suite, separate from the bi-evals repo, that you can run, iterate on, and look at in the viewer.

> **Where this guide assumes bi-evals lives.** This guide refers to the bi-evals repo at `~/Documents/Repos/bi-evals`. If yours is somewhere else, substitute that path everywhere you see it.

---

## Step 0 — Pick your mode

bi-evals runs in one of two modes. **Pick now, before you scaffold** — the rest of this guide branches based on your choice.

### Built-in mode

bi-evals runs the agent itself: Claude + your skill/knowledge files + tools bi-evals provides. You bring an Anthropic API key, system prompt, and skill files. bi-evals handles the tool-calling loop.

**Pick this when:**
- You don't yet have a production BI agent and want to evaluate/build one with bi-evals' Claude harness.
- You want to compare models on a controlled, identical harness.

Config: `agent.type: anthropic_tool_loop`.

### Bring-your-own mode (BYO)

bi-evals calls your existing BI agent over HTTP and scores what it returns. You bring an endpoint URL (and any auth headers). Your agent does the routing, retrieval, prompting, model calling, and SQL generation — exactly as it does in production. **The skill/knowledge file config is unused in BYO mode** because your agent owns its own knowledge setup.

**Pick this when:**
- You already have a production BI agent reachable over HTTP (any model, any stack).
- You want to evaluate what actually ships, not a parallel harness.

Config: `agent.type: api_endpoint`.

### Decision rule

If you have a production BI agent reachable over HTTP → **BYO**. Otherwise → **Built-in**.

Once you've picked, follow the steps below. Where a step says **(Built-in only)** or **(BYO only)**, skip it if it doesn't apply to your mode.

---

## What you're going to build

### Built-in mode

```
~/work/my-test-evals/
├── bi-evals.yaml          # Eval project config (anthropic_tool_loop)
├── .env                   # ANTHROPIC_API_KEY + DB creds (gitignored)
├── .env.example           # Template (safe to commit)
├── system-prompt.md       # The system prompt the Claude harness uses
├── skills/                # Your knowledge files
│   ├── SKILL.md
│   └── knowledge/
│       └── ORDERS.md
├── golden/                # Your golden tests (YAML, one per file)
├── results/               # Run outputs (auto-created)
└── reports/               # Generated HTML reports (auto-created)
```

### BYO mode

```
~/work/my-test-evals/
├── bi-evals.yaml          # Eval project config (api_endpoint)
├── .env                   # BI_AGENT_URL, auth tokens + DB creds (gitignored)
├── .env.example           # Template (safe to commit)
├── golden/                # Your golden tests (YAML, one per file)
├── results/               # Run outputs (auto-created)
└── reports/               # Generated HTML reports (auto-created)
```

Note: no `system-prompt.md` or `skills/` directory — your production agent already has those wherever its own code lives. bi-evals never touches them.

You will run all `bi-evals` commands from inside `~/work/my-test-evals/`, not from the bi-evals repo.

---

## Prerequisites

- **Python 3.11+** with [`uv`](https://docs.astral.sh/uv/) installed
- **Node.js + npm** (Promptfoo is invoked via `npx`)
- **Anthropic API key** *(Built-in only)*
- **A reachable BI agent endpoint** *(BYO only)* — local during dev (e.g. `http://localhost:8000/ask`) or a real internal URL
- **A warehouse the scorer can query.** Today bi-evals only supports Snowflake. The scorer needs to execute *both* the generated SQL (or whichever SQL your endpoint returns) and the reference SQL to compare results. You need:
  - A Snowflake account
  - A user with key-pair authentication ([Snowflake docs](https://docs.snowflake.com/en/user-guide/key-pair-auth))
  - At least one schema/table the SQL targets

If you don't have Snowflake yet, you can't currently run an end-to-end eval. (DuckDB-as-eval-target is on the roadmap.)

---

## Step 1 — Install bi-evals (both modes)

Since bi-evals isn't on PyPI yet, install from the local repo. Two options:

### Option A: Add as a dependency in a fresh project (recommended)

From the directory where you want your eval project to live:

```bash
mkdir ~/work/my-test-evals
cd ~/work/my-test-evals
uv init
uv add ~/Documents/Repos/bi-evals    # path to the bi-evals repo on your machine
```

`uv add <path>` installs bi-evals into your project's venv. If you change something in the bi-evals repo, your project picks it up automatically — useful while bi-evals is still evolving.

Verify the CLI is available:

```bash
uv run bi-evals --help
```

You should see a Click help screen listing `init`, `run`, `ui`, `report`, `compare`, etc.

### Option B: Run bi-evals straight from the repo

If you don't want a separate project shell, stay in the bi-evals repo and pass `--config` everywhere:

```bash
cd ~/Documents/Repos/bi-evals
uv run bi-evals --config ~/work/my-test-evals/bi-evals.yaml run
```

This is fine for one-off testing. Option A is nicer because you can `cd` into your eval project and forget about where bi-evals lives.

---

## Step 2 — Scaffold the project

Pick the command that matches your mode. Running `bi-evals init` on its own (without a mode) intentionally errors — the scaffold output differs between modes, so you have to pick.

### Built-in mode

```bash
cd ~/work/my-test-evals
uv run bi-evals init built-in
```

This drops:

- `bi-evals.yaml` — pre-configured for `anthropic_tool_loop` with placeholder paths for your system prompt and skill files
- `.env` and `.env.example` — keyed on `ANTHROPIC_API_KEY` + Snowflake creds
- `golden/example-query.yaml` — a stub golden test (mode-agnostic)
- `results/` and `reports/` — empty dirs for outputs

### BYO mode

```bash
cd ~/work/my-test-evals
uv run bi-evals init byo
```

This drops:

- `bi-evals.yaml` — pre-configured for `api_endpoint` with `${BI_AGENT_URL}` and `${BI_AGENT_TOKEN}` placeholders; **no** `system_prompt:` or `tools:` fields (unused in BYO)
- `.env` and `.env.example` — keyed on `BI_AGENT_URL`, `BI_AGENT_TOKEN`, and Snowflake creds (no `ANTHROPIC_API_KEY`)
- `adapter_example.py` — a ~70-line FastAPI reference shim showing the response shape bi-evals expects from your endpoint
- `golden/example-query.yaml` — a stub golden test (mode-agnostic)
- `results/` and `reports/` — empty dirs for outputs

---

## Step 3 — Configure your agent

This is where the two modes diverge.

### Step 3 (Built-in only) — Provide skill / knowledge files

bi-evals doesn't ship with skill files. You provide them.

**Q: What is a "skill file"?** It's a markdown document the LLM agent reads (via the `file_reader` tool) to learn about your warehouse — what tables exist, what columns mean, what business rules apply. Conceptually it's a lightweight semantic layer expressed in natural language.

**Q: Where do I put them?** Anywhere. The config has an `agent.tools[].config.base_dir` field — point it at whatever directory contains your files.

For testing, the simplest thing is to put them inside your eval project:

```bash
mkdir -p ~/work/my-test-evals/skills/knowledge
```

Then create at least:

**`skills/SKILL.md`** — the entry point:

```markdown
# Skill: Acme Warehouse Queries

You answer questions about Acme's data warehouse using SQL.

## How to use this skill
1. Identify the topic of the question (orders, customers, products, etc.)
2. Read the matching knowledge file in `knowledge/<topic>.md`
3. Generate a SQL query using the schema described there

## Knowledge files
- `knowledge/ORDERS.md` — order, line item, fulfillment data
- `knowledge/CUSTOMERS.md` — customer profile and lifecycle data
```

**`skills/knowledge/ORDERS.md`** — describe one specific area:

```markdown
# Orders

## Tables
- `RAW.ORDERS` — one row per order
  - `ORDER_ID` (PK)
  - `CUSTOMER_ID` (FK → CUSTOMERS)
  - `ORDER_DATE` (DATE)
  - `STATUS` ('pending' | 'shipped' | 'cancelled' | 'refunded')
  - `TOTAL_AMOUNT` (NUMERIC; in USD)

## Common patterns
- "Revenue" = `SUM(TOTAL_AMOUNT) WHERE STATUS = 'shipped'`. Cancelled and refunded orders should be excluded.
- "New customers this month" = first-ever ORDER_DATE within the month.
```

**`system-prompt.md`** at the project root (referenced by `agent.system_prompt` in the config):

```markdown
You are an analytics assistant for Acme.

When the user asks a question, use the `read_skill_file` tool to read SKILL.md first, then read whichever knowledge file is most relevant.

Output a single SQL query in a ```sql code fence. The query should run against Snowflake. Do not explain the query — just produce it.
```

These are starter examples. The shape and depth of your skill files is up to you.

> **If your knowledge files already live somewhere else** (a dbt project, an internal docs repo, a Notion export), just point `base_dir` at that directory in the config. Don't duplicate them.

Then edit `bi-evals.yaml`:

```yaml
project:
  name: "My Test Evals"

agent:
  type: "anthropic_tool_loop"
  model: "claude-sonnet-4-6"               # example; any valid Anthropic model ID works
  system_prompt: "system-prompt.md"
  tools:
    - name: read_skill_file
      type: file_reader
      config:
        base_dir: "skills/"
    - name: describe_table                   # optional but recommended
      type: describe_table
  max_rounds: 10

database:
  type: snowflake
  connection:
    account: "${SNOWFLAKE_ACCOUNT}"
    user: "${SNOWFLAKE_USER}"
    private_key_path: "${SNOWFLAKE_PRIVATE_KEY_PATH}"
    private_key_passphrase: "${SNOWFLAKE_PRIVATE_KEY_PASSPHRASE}"
    warehouse: "${SNOWFLAKE_WAREHOUSE}"
    database: "${SNOWFLAKE_DATABASE}"
    schema: "${SNOWFLAKE_SCHEMA}"
```

Defaults under `scoring`, `reporting`, and `storage` are fine — leave them alone for now. Skip to **Step 4**.

### Step 3 (BYO only) — Point bi-evals at your agent endpoint

In BYO mode you don't configure skills or a system prompt — those live with your production agent. You configure where to call it.

Replace the `agent` section of the scaffolded `bi-evals.yaml`:

```yaml
project:
  name: "My Test Evals"

agent:
  type: "api_endpoint"
  endpoint:
    url: "${BI_AGENT_URL}"                   # e.g. http://localhost:8000/ask
    method: "POST"                           # default; omit if unchanged
    timeout: 60                              # seconds; default
    headers:
      Authorization: "Bearer ${BI_AGENT_TOKEN}"   # optional; omit if your endpoint doesn't need auth
    # response_text_key defaults to "text", response_sql_key defaults to "sql".
    # Set them explicitly if your endpoint uses different field names or nests its response
    # (dot-notation supported, e.g. "response.sql").

database:
  type: snowflake
  connection:
    account: "${SNOWFLAKE_ACCOUNT}"
    user: "${SNOWFLAKE_USER}"
    private_key_path: "${SNOWFLAKE_PRIVATE_KEY_PATH}"
    private_key_passphrase: "${SNOWFLAKE_PRIVATE_KEY_PASSPHRASE}"
    warehouse: "${SNOWFLAKE_WAREHOUSE}"
    database: "${SNOWFLAKE_DATABASE}"
    schema: "${SNOWFLAKE_SCHEMA}"
```

**You can delete the `system_prompt:` and `tools:` fields entirely** — they're Built-in-only. Likewise, you don't need a `skills/` directory or `system-prompt.md` file.

**What your endpoint needs to do:**

bi-evals will `POST` to your URL with a JSON body containing the question:

```json
{ "question": "What was total shipped revenue in 2024?" }
```

Your endpoint should return JSON. The minimum useful shape is:

```json
{
  "text": "Revenue was $4.2M",
  "sql": "SELECT SUM(TOTAL_AMOUNT) FROM RAW.ORDERS WHERE STATUS = 'shipped' ..."
}
```

(The field names `text` and `sql` match the defaults for `response_text_key` and `response_sql_key`. If your endpoint already returns different field names, override them in `endpoint:` config rather than changing your endpoint.)

That's enough to score the SQL+results dimensions. To unlock the full scoring (including "did the agent read the right knowledge files?"), your endpoint can also return `trace` and `files_read` fields. The full schema with three canonical examples lives in [`docs/byo-response-contract.md`](byo-response-contract.md); validate your endpoint against it with `bi-evals doctor`.

**Don't have an HTTP endpoint yet?** Wrap your existing agent in a thin FastAPI shim (~30 lines) that imports it and exposes a `POST /ask` route. Run it locally during evals and point bi-evals at `http://localhost:8000/ask`. The wrapper isn't a production service — just a test harness.

Skip to **Step 4**.

---

## Step 4 — Fill in `.env`

bi-evals auto-loads `.env` from the same directory as `bi-evals.yaml`. You don't need to `source` it.

### Built-in mode

```
ANTHROPIC_API_KEY=sk-ant-...

SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_PRIVATE_KEY_PATH=/Users/you/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=                   # leave blank if your key isn't encrypted
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=YOUR_DB
SNOWFLAKE_SCHEMA=PUBLIC
```

### BYO mode

```
BI_AGENT_URL=http://localhost:8000/ask
BI_AGENT_TOKEN=                                      # only if your endpoint needs auth

SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_PRIVATE_KEY_PATH=/Users/you/.ssh/snowflake_rsa_key.p8
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=YOUR_DB
SNOWFLAKE_SCHEMA=PUBLIC
```

If you don't have key-pair auth on Snowflake yet, [follow Snowflake's guide](https://docs.snowflake.com/en/user-guide/key-pair-auth). One-time setup, ~10 minutes.

---

## Step 5 — Write your first golden test (both modes)

Golden tests are mode-agnostic — same schema, same scoring logic. Delete the scaffolded `golden/example-query.yaml` and create one that matches your actual data.

`golden/revenue/total-revenue.yaml`:

```yaml
id: revenue-001
category: revenue
difficulty: easy
question: "What was total shipped revenue in 2024?"

reference_sql: |
  SELECT SUM(TOTAL_AMOUNT) AS TOTAL_REVENUE
  FROM RAW.ORDERS
  WHERE STATUS = 'shipped'
    AND ORDER_DATE BETWEEN '2024-01-01' AND '2024-12-31'

expected:
  min_rows: 1
  required_columns:
    - TOTAL_REVENUE
  row_comparison:
    enabled: true
    completeness_threshold: 0.95
    precision_threshold: 0.95
    value_tolerance: 0.0001
    key_columns: []                # no grouping; single-row aggregate
    value_columns: [TOTAL_REVENUE]
    ignore_order: true

tags: [revenue, smoke]
```

Two important things:

- **The reference SQL has to actually run** against your warehouse. The scorer executes both the agent's SQL and the reference SQL, then compares row sets. Make sure the reference returns the right answer first.
- **`row_comparison.enabled: true` is what activates result-set scoring** (`row_completeness`, `row_precision`, `value_accuracy`). If it's left off, those dimensions skip and your test scores almost entirely on structural checks — usually not what you want.

For the full golden-test schema, see [`docs/golden-tests-guide.md`](golden-tests-guide.md).

---

## Step 6 — Run your eval (both modes)

```bash
cd ~/work/my-test-evals
uv run bi-evals run
```

What happens:

1. bi-evals loads your config and scans `golden/` for tests.
2. It generates a Promptfoo config and invokes `npx promptfoo eval` to run each golden.
3. For each test:
   - **Built-in:** the Claude tool-loop sends the question, executes tool calls (file reads), and produces SQL.
   - **BYO:** bi-evals POSTs the question to your endpoint and captures the response.
4. The scorer runs both the generated and reference SQL against Snowflake and compares results across the 10 dimensions.
5. Results are written to `results/eval_<timestamp>.json` and ingested into `results/bi-evals.duckdb`.

You'll see a Promptfoo progress bar, a per-test results table, and finally `Ingested: <run-id>`.

> **If you see `Error: Promptfoo exited with code 100`** at the end, that's a cosmetic `npm` warning, not a real failure. Your run is fine — auto-ingest still happens. Run `npm install -g npm@latest` to silence it.

### Useful flags

```bash
uv run bi-evals run -f revenue        # run only tests in the "revenue" category
uv run bi-evals run --dry-run         # preview Promptfoo config without running
uv run bi-evals run --no-cache        # force fresh API calls (bypass Promptfoo cache)
uv run bi-evals run --repeats 3       # run each test 3 times for variance/stability
uv run bi-evals run -v                # verbose Promptfoo output
```

---

## Step 7 — Look at the results (both modes)

### Option A: One-off HTML report

```bash
uv run bi-evals report
```

Writes `reports/report_<run-id>.html`. Open it in a browser.

### Option B (recommended): Interactive viewer

```bash
uv run bi-evals ui
```

Starts a local server on `http://localhost:8765` and opens your browser. You get:

- **Runs list** — all your runs, sorted newest first, with project filter and "Compare prev → latest" shortcut.
- **Single-run view** — same content as the HTML report, plus filter dropdowns for category and model.
- **Failures section** — at the top of every run with failures, listing each failed test with per-dimension reasons (e.g. `value_accuracy: Value mismatches: TOTAL_REVENUE: 1234567 vs 9876543`).
- **Per-test drilldown** — click any test_id to see the generated SQL, the reference SQL, the full agent trace, the dimension-by-dimension breakdown.
- **Compare two runs** — checkboxes on the runs list, then "Compare selected".

The viewer auto-refreshes every 10 seconds, so you can leave it open and run new evals in another terminal.

> **Important:** run `bi-evals ui` from your eval project directory (`~/work/my-test-evals`), not from the bi-evals repo. Otherwise it can't find your `bi-evals.yaml` and reads the wrong DuckDB file (or none).

---

## Step 8 — Iterate

### Built-in mode

1. Run an eval, see what fails (`bi-evals run`, then check the viewer).
2. Click into a failing test — see the generated SQL vs reference SQL and the agent's trace.
3. Fix something — usually one of:
   - Your skill files (the agent doesn't know about a column or rule).
   - The system prompt (the agent isn't following the right pattern).
   - The golden's reference SQL (your reference was wrong, not the agent).
4. Re-run only the affected category: `bi-evals run -f revenue`.
5. Compare runs to confirm: `bi-evals compare prev latest`.

### BYO mode

In BYO, bi-evals is a measurement tool — it tells you *what* fails. Fixing it almost always means changing your production agent (its prompt, its retrieval, its routing), then re-running the evals.

1. Run an eval, see what fails.
2. Click into a failing test — see the generated SQL, the reference SQL, and (if your endpoint returns it) the agent's trace.
3. Diagnose in your agent's codebase. Common categories:
   - Retrieval missed the relevant knowledge file → your retrieval index/chunking.
   - SQL is structurally off → your agent's prompt or post-processing.
   - SQL is right but values wrong → likely a knowledge-source issue on your agent's side.
4. Ship a fix to your agent. Re-run the evals against the updated endpoint.
5. Compare runs to confirm: `bi-evals compare prev latest`.

The viewer's compare view is especially useful in BYO mode for catching regressions when your prod agent changes underneath you.

---

## Common pitfalls

- **The agent generates SQL that runs but returns wrong values.** Usually a knowledge issue. In Built-in mode, check your skill files. In BYO mode, check what your agent's own retrieval surfaced — the `value_accuracy` reason in the viewer shows the mismatched values.

- **A test fails on `filter_correctness` even though results are right.** This dimension compares WHERE-clause structure to the reference. Equivalent-but-different filters fail it (`WHERE YEAR = 2024` vs `WHERE ORDER_DATE BETWEEN ...`). It's diagnostic — lowers the score but doesn't fail the test on its own. If noisy for you, drop it from `scoring.dimensions`.

- **`bi-evals ui` shows "Not Found" when you click a test.** Almost always means you ran the UI from the wrong directory. Restart from your eval project dir.

- **Auto-ingest didn't happen.** If Promptfoo crashed before writing JSON, there's nothing to ingest. Run `bi-evals ingest results/eval_<timestamp>.json` manually.

- **"Snowflake connection failed" with no other detail.** Almost always a key-pair issue. Check `SNOWFLAKE_PRIVATE_KEY_PATH` resolves to an absolute path that exists. If your key has a passphrase, set `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`.

- **(BYO only) Every test fails with HTTP errors.** Check `BI_AGENT_URL` is reachable from where you're running `bi-evals run`. If your endpoint is on a different host, check headers/auth. `bi-evals run -v` will surface the HTTP error body.

- **(BYO only) SQL dimension scores look fine but `skill_path_correctness` always fails.** Your endpoint isn't returning `files_read` or `trace`, so the scorer can't tell which knowledge files your agent touched. Either add those fields to your endpoint response or drop `skill_path_correctness` from `scoring.dimensions`.

---

## What to read next

- [`docs/golden-tests-guide.md`](golden-tests-guide.md) — full golden test YAML schema, including `last_verified_at`, `anti_patterns`, multi-model setup.
- [`docs/feature_summary.md`](feature_summary.md) — every CLI command and every config field with examples.
- [`README.md`](../README.md) — the 9 scoring dimensions and how to tune them; also a short overview of the two modes.
- [`docs/bi-eval-framework.md`](bi-eval-framework.md) — design rationale.

---

## Roadmap notes

Known onboarding gaps not yet fixed:

- **Install path** — eventually `pip install bi-evals` from PyPI; for now you install from the local repo.
- **Sample DuckDB dataset in the scaffold** — so you can run a real eval before setting up Snowflake; not built yet.
- **`bi-evals doctor`** — a one-shot validation command (will also check that a BYO endpoint returns a parseable response shape); not built yet.

When these land, the relevant steps in this guide get shorter. For now, expect ~30–60 minutes of setup with Snowflake creds in hand.
