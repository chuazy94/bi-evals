# Refactor Step 1 — Contract + Adapter Registry

> Part of the response-evaluation pivot. See `docs/bi-eval-integration-analysis.md` for the
> full thesis and issue #27. This doc covers **only Step 1**: the architectural core.

## Thesis recap (one line)

bi-evals evaluates the *response* of the real agent — it never owns or rebuilds the agent's
loop. The shape is **one contract `{ generated_sql, trace }`, many adapters** — not "two modes."
The scorer is already agent-agnostic; Step 1 names the seam that physically exists and routes
adapters through a registry.

## Step 1 goal & guardrails

**Goal:** Name the contract, route adapter dispatch through a registry, reverse the import
direction so adapters depend on a neutral contract module.

**Hard guardrail: zero user-facing behavior change.**
- The flat YAML schema is untouched. `anthropic_tool_loop` and `api_endpoint` work exactly as today.
- The trace JSON written for the scorer is byte-for-byte the same shape (including `agent_type`,
  which `store/ingest.py` reads).
- All existing non-integration tests pass **without modification**, via re-export shims.

**Explicit non-goals (deferred to Step 2):** schema break, `push` adapter, capability check,
model honesty marker (`requested_model`/`actual_model`), `init` / `doctor` / `tmp/my-evals`
changes, open-envelope trace handling.

## Current state (verified)

- `provider/entry.py:91-98` — the two-armed `if agent_type == "anthropic_tool_loop" / elif
  "api_endpoint" / else` dispatch. Both arms already converge: each produces an `AgentResult` and
  writes the *same* trace dict (`entry.py:116-129`).
- `provider/agent_loop.py` — defines `TraceStep`, `AgentResult`, `extract_sql` **and** the driving
  loop `run_agent_loop`. It's doing double duty: shared contract types + the built-in driver.
- `provider/api_endpoint.py:13` — imports `AgentResult, TraceStep, extract_sql` **from
  agent_loop.py** (backwards: the response-eval adapter depends on the driver module).
- The scorer (`scorer/entry.py`, `dimensions.py`) never branches on `agent_type`. It reads only
  `generated_sql` and `trace` steps (`tool_use` / `tool_name` / `tool_input`). Already contract-pure.

**Import coupling to preserve (these tests import current paths):**
- `tests/test_agent_loop.py` → `AgentResult, TraceStep, extract_sql, run_agent_loop` from `agent_loop`
- `tests/test_api_endpoint.py` → `call_api_endpoint, _get_nested` from `api_endpoint`
- `tests/test_demo_routing.py`, `tests/test_demo_scorer_phase_3.py` → `run_agent_loop`, `AgentResult` from `agent_loop`
- `api_endpoint.py` → `AgentResult, TraceStep, extract_sql` from `agent_loop`

## Changes (file by file)

### 1. NEW `src/bi_evals/provider/contract.py` — the named contract
- Move `TraceStep`, `AgentResult`, `extract_sql` here (unchanged code).
- `AgentResult` *is* the canonical trace today (`generated_sql`, `trace`, `files_read`, + usage).
- Define an `Adapter` protocol:
  ```python
  class Adapter(Protocol):
      def produce(self, question: str, vars: dict[str, Any],
                  config: BiEvalsConfig, model: str | None) -> AgentResult | str: ...
  ```
  (`str` return = error message, matching today's convention in `entry.py`.)
- Docstring names this the canonical contract and notes the Step 2 open-envelope direction
  (no envelope handling built yet — just don't fight it).

### 2. `src/bi_evals/provider/agent_loop.py` — becomes the (dev-only) driving adapter
- Keep `run_agent_loop` (driving logic, unchanged).
- Replace the local definitions of `TraceStep`/`AgentResult`/`extract_sql` with a **re-export shim**:
  `from bi_evals.provider.contract import AgentResult, TraceStep, extract_sql` (re-exported at
  module level so existing importers keep working).
- Module docstring: mark driving as **dev/golden-authoring only — not a public product feature.**

### 3. `src/bi_evals/provider/api_endpoint.py`
- Change one import line: pull `AgentResult, TraceStep, extract_sql` from `contract` instead of
  `agent_loop`. Behavior identical. (`call_api_endpoint` / `_get_nested` stay here — its test imports them.)

### 4. NEW `src/bi_evals/provider/registry.py` — the adapter registry
- `build_adapter(config: BiEvalsConfig) -> Adapter`, mirroring `db/factory.py`'s `if type == ... / else raise`.
- Two adapters wrapping today's logic (moved out of `entry.py`):
  - `AnthropicToolLoopAdapter` — wraps current `_run_anthropic_tool_loop`. Comment: **dev-only.**
  - `ApiEndpointAdapter` — wraps current `_run_api_endpoint` / `call_api_endpoint`.
- Unknown type → `ValueError` with the same wording as today's `else` branch.

### 5. `src/bi_evals/provider/entry.py`
- Replace the `if/elif/else` (lines 91-98) with:
  `adapter = build_adapter(config); result = adapter.produce(prompt, vars_, config, model_override)`.
- Move `_run_anthropic_tool_loop` / `_run_api_endpoint` into the registry/adapters.
- Trace-writing + return dict (lines 104-151) **unchanged**, including `agent_type` in the trace.

### 6. NEW `tests/test_provider_registry.py`
- `build_adapter` returns the correct adapter per `agent.type`; raises on unknown.
- Both adapters satisfy the `Adapter` protocol.
- (Existing provider tests pass untouched via the shims.)

## Verification

```bash
uv run python -m pytest tests/ -m "not integration" -v
```
Must stay green — especially `test_agent_loop`, `test_api_endpoint`, `test_multi_model`,
`test_bridge`, `test_store_ingest`. No demo/API-cost tests required (pure refactor).

## Why low-risk / reversible

Both adapters already converge on one `AgentResult` + one trace dict; the scorer is already
agent-agnostic. This names a seam that exists and replaces one `if`. Re-export shims keep the
test surface still. Fully reversible. The transitional shim line in `agent_loop.py` is cleaned up
in Step 2 when driving is formally demoted and the schema breaks.

## What Step 2 will then own

Clean schema break (`AgentConfig` restructure, strip driving fields from the public surface),
`push` adapter + thin `submit()` SDK as the **default on-ramp**, capability check (graceful
degradation: absent fields → `unknown`, never silent-fail), model-as-request honesty marker,
and `init` / `doctor` / `tmp/my-evals` / docs updates.
