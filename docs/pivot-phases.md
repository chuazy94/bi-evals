# Response-evaluation pivot — phase plan

> The pivot reframes the agent layer as **one canonical contract `{generated_sql, trace}`,
> many adapters** — bi-evals scores the real agent's response and never rebuilds the agent.
> Full thesis: `docs/bi-eval-integration-analysis.md`.
>
> **Phase numbering.** The original MVP phases (Phase 1–7.8) are historical and shipped. This
> pivot is numbered separately as **Pivot Phase 1, 2, …** so the two schemes never collide.

## At a glance

| Phase | What | Status |
|-------|------|--------|
| **Pivot Phase 1** | Contract + adapter registry | ✅ merged (PR #28) — `docs/refactor-step1.md` |
| **Pivot Phase 2** | Adapter-nested config schema (clean break) | ✅ merged (PR #29) — `docs/migration-adapter-schema.md` |
| **Pivot Phase 3** | Push adapter (replay) + `score --input` | ✅ merged (PR #34) — `docs/pivot-phase-3-design.md` |
| **Pivot Phase 4** | Capability check (open-envelope trace) | ⬜ |
| **Pivot Phase 5** | Model-as-request honesty marker | ⬜ |
| **Pivot Phase 6** | OTel adapter — ingest spans the real agent emits | ⬜ (see "Why response-evaluation is the right approach" below) |

**Settled architectural decisions:**
- Push **reuses Promptfoo** — it does not bypass the runner. The replay adapter feeds submitted
  traces into the existing scorer path, so scoring/ingest/report stay single-path.
- Driving (`anthropic_tool_loop`) stays **dev-only** — kept functional but demoted off the public
  config surface.

---

## Pivot Phase 1 — Contract + adapter registry ✅

Extracted the canonical `{generated_sql, trace}` contract into `provider/contract.py`, introduced
an `Adapter` protocol + registry (`provider/registry.py`), and reversed the import direction so
adapters depend on the contract rather than each other. The two-mode `if/elif` dispatch became a
registry lookup. Behavior-neutral. Detail: `docs/refactor-step1.md`.

## Pivot Phase 2 — Adapter-nested config schema ✅

Clean break: `agent.type` → `agent.adapter`; each adapter's config nests under a block named for it;
the dev-only driving adapter is demoted off the public surface; `init built-in`/`byo` → `init
dev`/`api_endpoint`; old flat configs are rejected at load with a migration hint. Detail +
conversion guide: `docs/migration-adapter-schema.md`.

Before / after:
```yaml
# before (flat, two-mode)
agent:
  type: api_endpoint
  endpoint: { url: ... }

# after (adapter-nested)
agent:
  adapter: api_endpoint
  api_endpoint: { url: ... }
```

---

## Pivot Phase 3 — Push adapter (replay) + `submit()` SDK ⬜

The headline of the pivot and the first slice with a tangible "run it, see a report" payoff that
needs no live agent or API spend.

- New `bi-evals score --input results.jsonl` CLI command + a thin `submit()` SDK helper.
- A `PushReplayAdapter` registered like any other adapter: instead of *producing* a result, it
  *replays* the customer's submitted `{generated_sql, trace}` for each test (keyed by test id),
  reusing the existing Promptfoo → scorer → ingest pipeline unchanged.
- Bridge/runner change so a push run feeds submitted traces through the scorer without calling a
  live provider.
- Make push the `init` default; rework README "Two modes" framing.

## Pivot Phase 4 — Capability check (open-envelope trace) ⬜

- Treat `trace` as an open envelope: the customer over-captures whatever their agent emits; the
  scorer reads the keys it understands and stores the rest, so new dimensions light up
  retroactively on existing submissions.
- At score time, report which dimensions can be scored given what the submission contains; absent
  fields → `unknown`/skipped, surfaced explicitly, never silently failed. Uses the existing
  critical/important/diagnostic tiers. Doubles as the adoption ladder.

## Pivot Phase 5 — Model-as-request honesty marker ⬜

- `requested_model` / `actual_model` on the contract; report flags honored / violated /
  **unverifiable** so model A/B comparisons are never silently assumed faithful. A special case of
  the Pivot Phase 4 capability check.
- Fold in the latent bridge cleanup: model fan-out in `bridge.py` should be adapter-aware, matching
  the `run` fix from Pivot Phase 2 (which only fans out models for the driving adapter).

## Pivot Phase 6 — OTel adapter (ingest spans the real agent emits) ⬜

The lowest-customer-effort, highest-fidelity adapter, and — per the research below — the one the
ecosystem has standardised on. The customer's **real, independently-running** agent emits
OpenTelemetry GenAI spans (SQL + tool calls); bi-evals consumes them and maps them onto the
canonical contract. No orchestration, no reconstruction, and the trace is clean by construction
(structured spans, not scraped prose).

- Likely leans heavily on capability the **runner we already use exposes**: Promptfoo is itself an
  OTLP receiver with a `trajectory:` assertion family (see below), so a chunk of this may be
  configuration rather than net-new infrastructure.
- The reference SQL must still execute on bi-evals' own connection (the independence caveat in
  `docs/bi-eval-integration-analysis.md` — never route the yardstick through the agent's tools).

---

## Why response-evaluation is the right approach (Promptfoo research, June 2026)

This section records external research into how Promptfoo — the test runner bi-evals is built on —
expects agentic systems to be evaluated. It directly validates the pivot, and it answers a recurring
question: *"should bi-evals orchestrate the customer's LLM calls in an 'eval mode' so it can inject
an instruction to emit a clean trace?"* The short answer the research supports is **no — that is the
pre-pivot `anthropic_tool_loop` path, and the runner now offers a higher-fidelity route (ingesting
spans from the real agent) that removes the only reason to drive the loop ourselves.**

### Finding 1 — Promptfoo's provider API is built to call a real agent, not reconstruct it

A Promptfoo custom provider needs only an `id` and a `callApi(prompt, context, options)` that
returns a `ProviderResponse` — i.e. it hands you the question and expects the final output back,
with no opinion on how you produce it. `callApi` is the natural place to call your *own* deployed
agent and let it run its full loop internally. For agents that build multi-turn conversations, the
response may include a `prompt` field to report the actual prompt sent. ([custom provider
docs][p-custom])

> **Sourcing caveat.** The page documents this *capability* (call out to whatever you like from
> `callApi`); it does **not** contain an explicit "wrap the real agent, don't reconstruct"
> recommendation in those words. An earlier draft of this section quoted such a recommendation —
> that phrasing was a fetch-summariser paraphrase and has been corrected. The architectural
> conclusion rests on Findings 2 and 3 below, which are directly sourced; Finding 1 is supporting
> context about what the provider API makes natural, not a Promptfoo policy statement.

### Finding 2 — Promptfoo added first-class trajectory (reasoning-path) assertions

A `trajectory:` assertion family now grades *what the agent did*, not just the final answer:
`trajectory:tool-used`, `trajectory:tool-sequence`, `trajectory:tool-args-match`,
`trajectory:step-count`, and an LLM-judged `trajectory:goal-success`. This is the generic form of
bi-evals' own `skill_path_correctness` dimension — confirming that grading the trace is a
recognised, first-class concern. ([assertions][p-assert])

### Finding 3 (decisive) — Promptfoo is an OpenTelemetry receiver, and external agent loops can feed it

Verbatim from the tracing docs: *"Promptfoo acts as an **OpenTelemetry receiver**, collecting
traces from your providers"*, exposing an OTLP endpoint at `http://localhost:4318/v1/traces` with
*"Standard OpenTelemetry support: Use any OpenTelemetry SDK in any language"*. Crucially:
*"External providers that wrap their own agent loops can adopt the same convention: emit one
OpenTelemetry span per LLM round."* So a customer's own agent loop can emit structured spans that
Promptfoo ingests and the `trajectory:` assertions grade. ([tracing docs][p-tracing])

This is the decisive fact for adapter strategy: a clean, structured, gradable trace can come from
the **real agent's own run** via OTel — no need for bi-evals to drive the loop to manufacture one.

### Why this makes orchestration the wrong move

When bi-evals was first built, *driving* the loop (`anthropic_tool_loop`) was a defensible way to
obtain a structured, gradable trace — at the time, the runner had no trace ingestion. Two things
have since invalidated that rationale:

1. **The mirror is only ever approximate.** To drive the loop, bi-evals must supply the system
   prompt, model, and routing — the *reasoning layer*, which is exactly the part that differs most
   per company and most determines answer quality. Fidelity was always capped, and a hypothetical
   "eval-mode orchestrator that injects a trace-format instruction" hits the same wall: to inject
   anything, it must own the loop, and owning the loop *is* reconstructing the agent.
2. **The one upside of driving — a clean, structured trace — is now available without driving.**
   OTel span emission gives a clean-by-construction trace from the agent's *real* run. The clean
   trace was the goal; orchestration was a means that costs fidelity; OTel achieves the goal without
   paying that cost.

So the "orchestrate in eval mode" idea is not a synthesis of before-and-after — it is a return to
*before*, which is lower-fidelity and which the ecosystem (and Promptfoo specifically) has moved
past. The legitimate instinct inside it — *clean traces are valuable* — is real, and the right owner
of "emit a clean trace" is the **customer's own agent** (via OTel spans, or an eval-mode emit
convention they add to their own loop), never a bi-evals orchestrator.

### Implication for adapter priority

The research promotes the **OTel adapter (Pivot Phase 6)** from "someday" toward strategically
central: it is the lowest-customer-effort, highest-fidelity path, it is how the field gets clean
traces, and the runner bi-evals already uses supports it natively (OTLP receiver + trajectory
assertions) — so building it may be substantially configuration rather than new infrastructure.
**push** remains the universal floor (works for any stack, including those emitting no telemetry);
**OTel** is the premium path for already-instrumented stacks.

### Sources

- [Promptfoo — Custom / JavaScript Provider][p-custom]
- [Promptfoo — Assertions / expected outputs (trajectory family)][p-assert]
- [Promptfoo — Tracing (OTLP receiver for external agents)][p-tracing]
- [Promptfoo — Python Provider][p-python]

[p-custom]: https://www.promptfoo.dev/docs/providers/custom-api/
[p-assert]: https://www.promptfoo.dev/docs/configuration/expected-outputs/
[p-tracing]: https://www.promptfoo.dev/docs/tracing/
[p-python]: https://www.promptfoo.dev/docs/providers/python/
