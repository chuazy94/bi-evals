# Phase 2 — Push on-ramp + clean schema (plan)

> Continues the response-evaluation pivot. Step 1 (contract + adapter registry) landed in PR #28.
> Full thesis: `docs/bi-eval-integration-analysis.md`. Step 1 detail: `docs/refactor-step1.md`.

## Phase 2 at a glance

Four slices, each its own PR, in dependency order:

- **2a — Clean schema break.** Restructure `AgentConfig` to adapter-nested; strip driving fields
  from the public surface. *Riskiest migration; do first so nothing stacks on a config shape that's
  about to change.*
- **2b — Push adapter (replay).** `bi-evals score --input results.jsonl`. A **replay adapter**
  registered like any other: instead of producing a result, it returns the customer's submitted
  `{generated_sql, trace}` for that test. Reuses the entire Promptfoo → scorer → ingest pipeline.
  Push becomes the default on-ramp.
- **2c — Capability check.** Open-envelope trace; at score time, report which dimensions can be
  scored given what the submission contains. Absent fields → `unknown`, surfaced, never silent-fail.
- **2d — Model-as-request honesty marker.** `requested_model` / `actual_model` in the contract;
  report flags honored / violated / unverifiable. (A special case of 2c.)

**Key architectural decisions (settled):**
- Push **reuses Promptfoo** — it does not bypass the runner. The replay adapter feeds submitted
  traces into the existing scorer path, so scoring/ingest/report stay single-path.
- Driving (`anthropic_tool_loop`) stays **dev-only** — kept functional but demoted off the public
  config surface.

---

## Slice 2a — Clean schema break (this PR)

### Goal
Replace the flat, two-mode `agent:` block with an adapter-nested shape, and remove driving fields
from the public surface. Clean break (no back-compat shim) with a migration note.

### Current shape (the problem)
```yaml
agent:
  type: anthropic_tool_loop      # two-mode worldview
  model: ...                     # driving field
  models: [...]                  # driving field (multi-model fan-out)
  system_prompt: ...             # driving field
  tools: [...]                   # driving field
  max_rounds: 10                 # driving field
  api_key_env: ...               # driving field
  endpoint: { url: ... }         # api_endpoint field, flattened as a peer
```

### Target shape (proposed — confirm before building)
```yaml
agent:
  adapter: api_endpoint          # which adapter (was `type`)
  api_endpoint:
    url: ...
    headers: { ... }
    response_sql_key: sql
    response_text_key: text

# dev-only driving adapter — kept but off the documented happy path:
agent:
  adapter: anthropic_tool_loop
  anthropic_tool_loop:
    model: ...        # or models: [...]
    system_prompt: ...
    tools: [...]
    max_rounds: 10
    api_key_env: ANTHROPIC_API_KEY
```

Rename `type` → `adapter` to match the registry vocabulary, and nest each adapter's config under a
key named for it. Driving fields move *into* the `anthropic_tool_loop:` block — still loadable, no
longer top-level peers.

### Blast radius (verified — every `agent.*` reader)
- `config.py` — `AgentConfig` restructure + `_normalize_models` (currently hard-codes
  `type == "anthropic_tool_loop"`).
- `provider/registry.py` — `build_adapter` reads `config.agent.type` → `config.agent.adapter`;
  adapters read their nested config.
- `provider/entry.py` — `config.agent.type`, `config.agent.model`.
- `scorer/entry.py:119` — `config.agent.model` (for trace-model resolution).
- `promptfoo/bridge.py:88-94` — multi-model fan-out reads `config.agent.models/model`.
- `cli.py` — mode labels (`141-150`), multi-model echo (`213-216`), init scaffolds.
- `doctor.py` — `system_prompt`/`tools` checks (`152-174`), `api_endpoint` checks (`269-285`).
- `store/ingest.py:423` — `config.agent.tools` (prompt-snapshot of read files).

### Migration tasks
- Update `tmp/my-evals/bi-evals.yaml` to the new shape (CLAUDE.md requires keeping it in sync).
- Update `init` scaffolds. **Open question:** `init` currently has `built-in` and `byo`
  subcommands — under the new world, the default on-ramp is *push* (2b) and driving is dev-only.
  Likely `init` leads with `api_endpoint` now (push lands in 2b), and the `built-in` scaffold
  becomes a hidden/dev option. Decide when we build 2b; for 2a, minimally update existing scaffolds
  to the new shape.
- Update `doctor` to validate the nested shape.
- Migration note in README / a `docs/migration-*.md`: how to convert an old flat config.

### Testing
- `test_config.py` — update fixtures to the new shape; add a test that an old flat config now
  fails to load with a clear, actionable error (clean break = explicit failure, not silent
  mis-parse).
- All existing tests that build a config (e.g. `test_provider_registry.py`'s `_config` helper,
  `test_multi_model.py`, `test_doctor.py`) must move to the new shape.
- `uv run python -m pytest tests/ -m "not integration"` green.

### Risk notes
- This is the one slice that **breaks existing user configs** — the migration note is mandatory.
- `_normalize_models` and the multi-model fan-out are the fiddly bits: models now live under
  `anthropic_tool_loop:` (driving) — but Phase 2d makes "model" a *request* for response-eval
  adapters too. Keep 2a's model handling scoped to the driving adapter; 2d generalizes it.

---

## Slices 2b–2d (sketch — plan in detail when 2a lands)

- **2b push:** new `score` CLI command; a `PushReplayAdapter` in the registry that reads submissions
  keyed by test id; bridge/runner change so a push run feeds submitted traces through the scorer
  without calling a live provider; `submit()` SDK helper; `init` default → push.
- **2c capability check:** treat `trace` as an open envelope; a connect-time/score-time report of
  scorable vs `unknown` dimensions, using the existing critical/important/diagnostic tiers.
- **2d model honesty marker:** `requested_model`/`actual_model` on the contract; report flags
  honored / violated / unverifiable; never assume honored when `actual_model` is absent.
