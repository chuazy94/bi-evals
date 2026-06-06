# Pivot Phase 3 — Push adapter (design)

> Part of the response-evaluation pivot. See `docs/pivot-phases.md` for the phase map and
> `docs/bi-eval-integration-analysis.md` for the thesis.

## Goal

`bi-evals score --input results.jsonl` — the customer runs their own agent over the goldens,
submits `{generated_sql, trace}` per question, and bi-evals scores it. **The default on-ramp** of
the pivot, and the first slice with a runnable payoff that needs **no live agent and no API spend**.

## The decisive constraint (from the code)

Everything downstream — scorer, `store/ingest.py`, DuckDB, report, compare, viewer — consumes the
**Promptfoo `eval_*.json` shape** (`raw["evalId"]`, `raw["results"]["results"]`, per-trial
`metadata.trace_file`) and **sibling trace files** in `results/traces/`. Producing a separate push
result format would fork all of that.

So push **reuses the Promptfoo pipeline unchanged** and only swaps what the *provider* does: instead
of calling a live agent, it **replays the customer's submitted row**. Push becomes one more
registered adapter — the "one contract, many adapters" architecture paying off.

## Decisions (settled)

- **Input plumbing:** `score --input X` writes `agent.push.input_file: X` into the generated
  promptfoo config. The provider already receives `config_path`; the adapter reads the JSONL from
  the config like every other adapter reads its own block.
- **Keying:** submissions are keyed by `golden_file` path — the same identity
  `trace_paths.make_test_id_slug` already uses.
- **First cut:** CLI `score --input` + `PushReplayAdapter` only. Deferred: `submit()` SDK helper,
  making push the `init` default.

## Submission format (JSONL, one row per golden)

```jsonl
{"golden_file": "golden/cases/revenue-001.yaml", "generated_sql": "SELECT ...", "trace": {"tool_calls": [...], "files_read": [...]}}
```

- `golden_file` (required) — path matching a golden, relative to the config dir. The join key.
- `generated_sql` (required) — the SQL the customer's agent produced.
- `trace` (optional) — open envelope; whatever the agent emitted. Pivot Phase 4 turns absent trace
  fields into `unknown` dimensions rather than failures. For Phase 3, a missing trace just means the
  trace-dependent dimension (`skill_path_correctness`) has nothing to grade (already skips today).

## Flow

```
bi-evals score --input results.jsonl
  ├─ load + validate JSONL (golden_file + generated_sql required; clear error per bad row)
  ├─ generate promptfooconfig: adapter=push, agent.push.input_file=<abs path>
  ├─ run Promptfoo (shared with `run`)
  │     ├─ provider entry → PushReplayAdapter.produce()
  │     │     → look up row by golden_file → AgentResult{generated_sql, trace}
  │     │     → entry.py writes the trace file (unchanged path logic)
  │     └─ scorer entry → reads trace, executes SQL, scores (unchanged)
  ├─ emit eval_*.json → auto-ingest into DuckDB (unchanged)
  └─ report / compare / ui all work (unchanged)
```

## Changes (file by file)

1. **`config.py`** — new `PushConfig(BaseModel)` with `input_file: str = ""`; add `push:
   PushConfig` to `AgentConfig`; add `"push"` to the `adapter` Literal.
2. **`provider/registry.py`** — `PushReplayAdapter`; register `"push"` in `build_adapter`.
   `produce()` loads the JSONL (cached per process), finds the row whose `golden_file` matches the
   test's `golden_file` var, returns an `AgentResult`. Missing row → error string (matches the
   existing adapter error convention).
3. **`cli.py`** — new `score` command: `--input PATH` (required), plus the same `--filter`,
   `--repeats`, `--yes`, `--verbose` ergonomics as `run` where they apply. Validates the JSONL up
   front (every `golden_file` resolves to a real golden; required fields present), writes
   `agent.push.input_file`, then shares the run/ingest path with `run` (extract the common
   orchestration so `run` and `score` don't duplicate it).
4. **`provider/contract.py`** — no change (the contract already carries `generated_sql` + `trace`).
5. **Scorer / ingest / report / compare / ui** — **untouched.**

## Testing

- **Unit:** `PushReplayAdapter` returns the right row by `golden_file`; missing row → error;
  malformed JSONL → clear error; missing required field → clear error.
- **CLI:** `score --input` with a fixture JSONL + fixture goldens generates the expected
  promptfoo config (adapter=push, input_file set); validates bad input.
- **End-to-end (no API spend):** a hand-written `results.jsonl` + goldens + a stubbed DB client →
  `score` produces an `eval_*.json`, ingests, and a report renders. This is the "playable" proof.
- `tmp/my-evals` smoke: add a `results.jsonl` and run `score` against it.

## Reality check: submissions are messy

Real BI agents don't emit clean `{generated_sql}` — they return SQL inside markdown fences or
mixed into a prose answer, and traces in wildly varying shapes. So the customer's real task isn't
"my agent already speaks this JSONL"; it's **"write a small reshape script that maps my agent's
output into these fields."** The "contract" is just the *target shape* — the work is the mapping
to it (which richer adapters like OTel later shrink, but never eliminate).

The first cut is already somewhat forgiving: the adapter runs `extract_sql()` on the submitted
`generated_sql`, so a fenced/prose blob still yields the SQL (the `tmp/my-evals` example submits
one deliberately). But the contract should be honest about this.

## Explicitly deferred (fast follow-up)

- **Accept `response_text` as an alternative to `generated_sql`** — submit the agent's raw answer
  and let the adapter extract the SQL, mirroring `api_endpoint`'s `response_sql_key`/
  `response_text_key`. Plus realistic, messy-output docs/examples rather than the clean ideal.
- `submit()` SDK helper (a `Runner` that yields golden questions and collects submissions).
- Making push the `init` default + README "Two modes" rework.
- Capability check (Pivot Phase 4) and model-as-request marker (Pivot Phase 5).
