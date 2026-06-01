# Request flow: what happens when you run an eval

This document walks through what bi-evals actually does when you run `bi-evals run` — from the moment you press enter to the moment the scorer produces a verdict. Written for integrators who want to understand what to expect, where their data goes, and how to debug failures.

If you only read one thing: **every test's JSON response is captured to `results/traces/*.json` before scoring.** When something goes wrong, that's where you look first.

---

## The high-level shape

A single test, in order:

1. You run `bi-evals run`.
2. bi-evals generates a `promptfooconfig.yaml` from your `bi-evals.yaml` + goldens, then shells out to `npx promptfoo eval`.
3. **Promptfoo (Node.js) forks a Python subprocess that runs the provider.** This is where your agent gets called.
4. The provider gets the agent's response and **writes it to `results/traces/{test}__{model}__{suffix}.json` on disk.**
5. Promptfoo then forks a *second* Python subprocess that runs the scorer.
6. The scorer reads the trace file back from disk, runs each scoring dimension, returns a pass/fail per dimension.
7. After every test completes, bi-evals auto-ingests the run's JSON output into DuckDB.
8. You open the viewer or generate a report.

The thing to internalize: **the provider and the scorer are different Python processes.** They communicate via files on disk, not in-memory. This is by design — Promptfoo's test harness forks workers independently and bi-evals couldn't pass objects between them even if it wanted to.

Disk is the bus.

---

## Step 1: bi-evals generates the Promptfoo config

`bi-evals run` does very little orchestration itself. It:

- Loads your `bi-evals.yaml` + every golden under `golden/`
- Generates a Promptfoo config: one provider entry per model in `agent.models`, one test entry per golden test, plus assertion configuration pointing at the bi-evals scorer
- Writes the config to `results/promptfooconfig_<timestamp>.yaml`
- Spawns `npx promptfoo eval --config <that file> --output results/eval_<timestamp>.json`

After this step, control passes to Promptfoo. bi-evals' own process is just waiting for Promptfoo to exit.

---

## Step 2: Promptfoo runs each test through the provider

For every (test, model, trial) combination, Promptfoo invokes the bi-evals provider. The provider lives at `src/bi_evals/provider/entry.py:call_api()` and is referenced from the Promptfoo config as a `file://` provider path.

**Built-in mode** (`agent.type: anthropic_tool_loop`): the provider runs the multi-turn Claude tool loop directly. Tool calls happen inside the same Python process; the model reads skill files via the `FileReaderTool`, writes SQL, the loop ends.

**BYO mode** (`agent.type: api_endpoint`): the provider calls `call_api_endpoint()` in `provider/api_endpoint.py`, which does exactly this:

```python
req = Request(endpoint_config.url, data=request_body, headers=headers, method="POST")
with urlopen(req, timeout=endpoint_config.timeout) as resp:
    response_data = json.loads(resp.read().decode("utf-8"))
```

Plain `urllib`. POST the question, wait for the complete response, parse it as JSON. There is no streaming, no connection reuse, no callbacks — one request, one response, connection closes. Errors (HTTP 4xx/5xx, connection refused, timeout) become structured error fields rather than exceptions; the request still produces a result the scorer can grade.

After parsing, the provider walks the response to extract:

- `sql` — from `response_sql_key` (defaults to `"sql"`, supports dot-notation like `"answer.query"`)
- `text` — from `response_text_key` (defaults to `"text"`); if absent, falls back to a fenced ` ```sql ` block search inside the text
- `files_read` — uses the top-level `files_read` array if present; otherwise derives it from `trace[].tool_input.path`
- `trace` — walks the array and builds `TraceStep` objects

The result of all this is an `AgentResult` dataclass held in memory. **Nothing is on disk yet.**

---

## Step 3: The provider writes the trace to disk

Still inside the provider's Python process, `provider/entry.py` builds a JSON dict and writes it:

```python
trace_data = {
    "test_id": test_id,
    "agent_type": agent_type,
    "model": effective_model,
    "rounds": result.rounds,
    "trace": result.trace_as_dicts(),
    "files_read": result.files_read,
    "generated_sql": result.extracted_sql,
    "prompt_tokens": result.prompt_tokens,
    "completion_tokens": result.completion_tokens,
    "total_tokens": result.total_tokens,
    "cost": result.cost,
    "latency_ms": result.latency_ms,
}

trace_file = trace_dir / f"{test_id_slug}__{model_slug}__{suffix}.json"
trace_file.write_text(json.dumps(trace_data, indent=2))
```

A few things to note about the filename:

- `test_id_slug` — derived from the golden file path (e.g. `golden/revenue/top-elephants-lions-1996.yaml` → `golden_revenue_top-elephants-lions-1996_yaml`). Stable across runs.
- `model_slug` — filesystem-safe form of the model ID (`claude-sonnet-4-6`). When you run multiple models in one eval, each gets its own trace files.
- `suffix` — 4 random hex bytes per invocation. Why? `--repeats N` runs the same `(test, model)` N times; without a unique suffix, they'd overwrite each other and only the last trial would survive.

The provider then returns a small metadata dict to Promptfoo containing `output`, token counts, cost, and a `metadata.trace_file` pointer. Promptfoo doesn't care about the trace itself — that's between the provider and the scorer.

**This is the moment your agent's response becomes captured.** From here on, the scorer sees the trace file on disk, not the live HTTP response.

---

## Step 4: Promptfoo invokes the scorer (a separate process)

After the provider returns, Promptfoo invokes the scorer for the same test. The scorer is `scorer/entry.py:get_assert()`, also referenced as a `file://` path in the Promptfoo config.

**This is a different Python process from the provider.** Promptfoo forks it independently. The provider's `AgentResult` doesn't exist anymore — that process has exited. The scorer starts cold and rebuilds what it needs from disk.

---

## Step 5: The scorer reads the trace back

The scorer doesn't know the random `suffix` the provider chose. It only knows the test and the model — same as the provider. So it globs the traces directory:

```python
per_model = sorted(
    trace_dir.glob(f"{test_id_slug}__{model_slug}__*.json"),
    key=lambda p: p.stat().st_mtime,
)
if per_model:
    return per_model[-1]   # most recent — handles --repeats correctly
```

The glob-and-pick-newest approach handles `--repeats N` correctly: each repeat writes a fresh trace, and the scorer for that repeat picks up the newest one because Promptfoo runs the provider and scorer in order per trial.

The fallback chain (defined in `scorer/entry.py:_resolve_trace_path()`):

1. `{slug}__{model_slug}__*.json` — most recent for this (test, model). Normal case.
2. `{slug}__*.json` — most recent for this test, any model. Single-model legacy configs.
3. `{slug}.json` — legacy flat name. Manually-written test fixtures.

The scoring entry point loads the matched JSON and hands it to the 10 dimension evaluators.

---

## Step 6: The scorer runs the dimensions and queries Snowflake

The scorer's job is to grade the agent's output. For most dimensions this means running both the agent's SQL and the golden's reference SQL against Snowflake, then comparing results:

- `execution` — does the agent's SQL run without error?
- `row_completeness`, `row_precision`, `value_accuracy` — execute both, compare row sets
- `table_alignment`, `column_alignment`, `filter_correctness`, `no_hallucinated_columns` — sqlglot parse of both, structural diff
- `skill_path_correctness` — reads the `trace` and `files_read` from the loaded trace file
- `anti_pattern_compliance` — checks the agent's SQL against the golden's declared anti-patterns

The agent's SQL never gets executed by the provider. **Snowflake only gets touched here, in the scorer.** That's why your BYO Snowflake credentials in `.env` matter even if your agent doesn't use them — the scorer needs its own connection to run the comparison queries.

Each dimension returns a `DimensionResult` with `passed: bool` and a descriptive `reason` string. The scorer aggregates them into the tiered pass/fail verdict per `scoring.critical_dimensions` and `scoring.pass_threshold`.

---

## Step 7: Auto-ingest and the database snapshot

After Promptfoo finishes all tests, control returns to bi-evals' CLI. It checks for `results/eval_<timestamp>.json` (which Promptfoo wrote) and ingests it into `results/bi-evals.duckdb`:

- One row per `(run, test, model, trial_ix)` in `trial_results`
- One aggregated row per `(run, test, model)` in `test_results` with pass-rate and stddev
- One row per dimension per trial in `dimension_results`
- Golden metadata (text, tags, anti-patterns, last_verified_at) snapshotted into the run so editing the YAML later never mutates history

Auto-ingest fires **whenever `eval_<timestamp>.json` exists**, even if Promptfoo exited non-zero (which it does when any test fails). The failing case is exactly when you want the report; bailing out on exit code would be the wrong default.

---

## What's actually on disk after a run

```
results/
├── eval_20260601_103045.json                 ← Promptfoo's overall output (the source of truth for ingest)
├── promptfooconfig_20260601_103045.yaml      ← What bi-evals fed to Promptfoo
├── bi-evals.duckdb                           ← Queryable history (ingested from eval_*.json)
└── traces/
    ├── golden_revenue_top-elephants-lions-1996_yaml__claude-sonnet-4-6__a3f12c8e.json
    ├── golden_revenue_top-elephants-lions-1996_yaml__claude-sonnet-4-6__b91d4e02.json   ← repeat trial 2
    └── golden_inventory_top-5-very-big-nations_yaml__claude-sonnet-4-6__f7a8b231.json
```

Each `traces/*.json` is one trial's captured response plus bi-evals' parsed fields. Pretty-printed with `indent=2`.

The relationship between these files:

- `eval_*.json` is the **portable source of truth** — re-ingestable, no DB dependency. You can move it between machines.
- `bi-evals.duckdb` is the **queryable view** built from `eval_*.json` files.
- `traces/*.json` are **per-trial debugging artifacts** — they don't get re-ingested but they're what the viewer's drilldown reads for the "trace" tab and what `bi-evals doctor` would (eventually) introspect.

---

## Debugging through the request flow

Because every step persists its output, every step is independently inspectable. Here's where to look when something goes wrong:

**"Every test failed with HTTP errors."**
Your endpoint isn't reachable from where you ran `bi-evals run`. Run `bi-evals doctor` first — it does exactly the POST bi-evals would have done, with the same headers, and reports HTTP status and parsed JSON. The doctor's failure message is the actual response body of your endpoint.

**"My SQL looks right but a dimension is failing in a way that makes no sense."**
Open the trace file: `cat results/traces/<test_slug>__<model_slug>__<suffix>.json | jq`. You'll see:
- The exact `generated_sql` bi-evals extracted from your response. Compare to what your endpoint actually returned. Common gap: bi-evals extracted the *first* fenced block; your model emitted two.
- The `trace` array as parsed. If `skill_path_correctness` is failing, this is what the scorer is evaluating.
- The `files_read` list. If it's empty when you expected entries, your endpoint either didn't return `files_read` or didn't return `trace[].tool_input.path`.

**"The viewer shows zero pass-rate even though my endpoint returns SQL."**
Either the trace file is empty (provider crashed) or the SQL parsing extracted nothing. Check the trace file's `generated_sql` field — if it's `null`, the provider didn't find SQL where it looked. Likely either your `response_sql_key` config is wrong or your endpoint returned the SQL in a fenced block under a different field than `text`.

**"Auto-ingest didn't run."**
Look for `results/eval_<timestamp>.json`. If it doesn't exist, Promptfoo crashed before writing it (likely a config or env issue — re-run with `-v`). If it exists, you can ingest manually: `bi-evals ingest results/eval_<timestamp>.json`.

**"Scoring graded a stale trace."**
This used to be a real bug (fixed in PR #12). The scorer reads the *most recent* trace matching `{slug}__{model_slug}__*.json`. If you're somehow seeing it grade an old one, check `ls -lt results/traces/` to verify the newest file's mtime matches the run you expect. If your filesystem has weird mtime behavior (some network mounts do), that's the failure mode.

---

## Why this architecture (brief aside)

You might wonder: why not pass the response in memory from provider to scorer? The answer is that **they're different processes by Promptfoo's design.** Promptfoo's test harness forks workers per test for isolation and parallelism. bi-evals can't share memory between them.

The disk-as-bus design was chosen because:

1. **It survives the process boundary.** No serialization protocol needed; just files.
2. **It's debuggable.** You can `cat` a trace file. You can edit one to test the scorer with a synthetic response.
3. **It composes with `--repeats N` and multi-model.** Per-trial files mean trials don't race over a shared in-memory buffer.
4. **It's the right granularity for the viewer.** Per-trial files map 1:1 to viewer drilldown pages.

The cost is that you need a consistent naming scheme (`{slug}__{model}__{suffix}.json`) so the scorer can find what the provider wrote. That contract lives in one place — `src/bi_evals/trace_paths.py` — specifically so writer and reader stay in lockstep. (When they didn't, before that module existed, the result was silent miscoring — see PR #12.)

---

## Keeping this doc honest

A Claude Code PostToolUse hook in `.claude/settings.json` watches the seven files that drive this request flow: `cli.py`, `provider/api_endpoint.py`, `provider/entry.py`, `scorer/entry.py`, `trace_paths.py`, `promptfoo/bridge.py`, and `store/ingest.py`. When any of them is edited, the hook emits a system reminder asking the agent to update this doc if the change altered the documented flow.

The wording is deliberately permissive ("if this change altered the request flow, also update request-flow.md — otherwise no action needed") because the hook can't tell intent from a diff. False positives (reminder fires for a comment-only edit) cost nothing; false negatives (a real flow change with no doc update) cost doc rot. The list is intentionally narrow — adding files that don't shape this flow would dilute the signal.

If you're modifying one of those seven files and the doc *should* change, the relevant sections to edit are usually Steps 2–6 (the parts that describe runtime behavior) and the "What's actually on disk" inventory.

---

## Related reading

- [`docs/byo-response-contract.md`](./byo-response-contract.md) — the JSON shape your endpoint must return
- [`docs/getting-started.md`](./getting-started.md) — per-mode setup walkthrough
- [`docs/duckdb-schema.md`](./duckdb-schema.md) — what gets stored after ingest
- `src/bi_evals/provider/api_endpoint.py` — the actual HTTP code
- `src/bi_evals/provider/entry.py` — the trace-write code
- `src/bi_evals/scorer/entry.py` — the trace-read code
- `src/bi_evals/trace_paths.py` — the naming contract
