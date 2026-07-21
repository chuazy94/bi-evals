# Response-evaluation pivot — phase plan

> The pivot reframes the agent layer as **one canonical contract `{generated_sql, trace}`,
> many adapters** — bi-evals scores the real agent's response and never rebuilds the agent.
> Full thesis: `docs/plans/pivot-phase-1-integration-analysis.md`.
>
> **Phase numbering.** The original MVP phases (Phase 1–7.8) are historical and shipped. This
> pivot's shipped work is numbered separately as **Pivot Phase 1, 2, …** so the two schemes never
> collide. The still-unbuilt phase (formerly Pivot Phase 6) is now tracked as **Build Stage 3** in
> `STATUS.md`'s "Remaining — Build Stages" — the single ordered backlog that also covers work
> outside this pivot (CI gating, semantic-layer scoring, etc). Pivot Phase 5 (model-as-request
> honesty marker) was dropped after review — see the note in its section below. This doc keeps
> calling shipped phases by their old Pivot Phase number in the narrative sections for continuity
> with the research; `STATUS.md` is the source of truth for current numbering and priority.

## At a glance

**This table covers only the pivot's own phases — it is not the project's overall priority
order.** `STATUS.md`'s "Remaining — Build Stages" interleaves these with unrelated work (e.g.
CI regression gating, sequenced *ahead* of the phases below because it has no dependency on them
and is fully designed) and is the doc that says what's actually next.

| Phase | What | Status |
|-------|------|--------|
| **Pivot Phase 1** | Contract + adapter registry | ✅ merged (PR #28) — `docs/plans/pivot-phase-1-refactor-step1.md` |
| **Pivot Phase 2** | Adapter-nested config schema (clean break) | ✅ merged (PR #29) — `docs/migration-adapter-schema.md` |
| **Pivot Phase 3** | Push adapter (replay) + `score --input` | ✅ merged (PR #34) — `docs/plans/pivot-phase-3-push-adapter-design.md` |
| **Pivot Phase 4** (Build Stage 2) | Capability check (open-envelope trace) | ✅ merged (PR #47) |
| **Pivot Phase 5** | Model-as-request honesty marker | ❌ dropped — see note below |
| **Pivot Phase 6** (Build Stage 3) | OTel adapter — ingest spans the real agent emits | ⬜ not started — see `STATUS.md` for priority (see "Why response-evaluation is the right approach" below) |

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
registry lookup. Behavior-neutral. Detail: `docs/plans/pivot-phase-1-refactor-step1.md`.

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

## Pivot Phase 4 (Build Stage 2) — Capability check (open-envelope trace) ⬜

- Treat `trace` as an open envelope: the customer over-captures whatever their agent emits; the
  scorer reads the keys it understands and stores the rest, so new dimensions light up
  retroactively on existing submissions.
- At score time, report which dimensions can be scored given what the submission contains; absent
  fields → `unknown`/skipped, surfaced explicitly, never silently failed. Uses the existing
  critical/important/diagnostic tiers. Doubles as the adoption ladder.

## Pivot Phase 5 — Model-as-request honesty marker ❌ dropped

Proposed `requested_model` / `actual_model` on the contract, with the report flagging honored /
violated / **unverifiable** so model A/B comparisons couldn't be silently assumed faithful.

**Dropped on review (2026-07-21) rather than built.** bi-evals never issues a model request for
the two adapters that matter — `push` and `api_endpoint` call *your* agent; bi-evals only receives
`{generated_sql, trace}}`, so there's no "requested" side to compare against. The only adapter
where a real request/response model pair exists is `anthropic_tool_loop`, which is dev-only and
trivially honored by construction (bi-evals can't lie to itself about which model it just called).
Building this would have meant inventing a new "model" config/contract concept for `push`/
`api_endpoint` that doesn't exist today, to check something that's either unverifiable (agent
doesn't report it — the common case) or moot (it does, but you already know what you configured).
Model fidelity for a live agent is that agent's own observability problem, not a golden-SQL
scoring concern.

## Pivot Phase 6 (Build Stage 3) — OTel adapter (ingest spans the real agent emits) ⬜

The lowest-customer-effort, highest-fidelity adapter, and — per the research below — the one the
ecosystem has standardised on. The customer's **real, independently-running** agent emits
OpenTelemetry GenAI spans (SQL + tool calls); bi-evals consumes them and maps them onto the
canonical contract. No orchestration, no reconstruction, and the trace is clean by construction
(structured spans, not scraped prose).

- Likely leans heavily on capability the **runner we already use exposes**: Promptfoo is itself an
  OTLP receiver with a `trajectory:` assertion family (see below), so a chunk of this may be
  configuration rather than net-new infrastructure.
- The reference SQL must still execute on bi-evals' own connection (the independence caveat in
  `docs/plans/pivot-phase-1-integration-analysis.md` — never route the yardstick through the agent's tools).

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

### Finding 4 — Promptfoo's actual agent pattern is "wrap the real agent in-process," not reconstruct it

Promptfoo's agent guides demonstrate evaluating a *real* agent by wrapping it in a Python provider
that keeps the agent's runtime in process — not by rebuilding its loop. From the Google ADK guide,
the rationale verbatim: wrapping the app as a Python provider *"keeps the ADK runtime in process, so
Promptfoo can inspect the same sessions, artifacts, and native OpenTelemetry spans that the agent
produced."* ([Evaluate Google ADK Agents][p-adk])

Note the mechanism and its limit: this works because the agent is an importable SDK object, so the
provider runs the *customer's real agent*. It is neither "reconstruct the loop" (`anthropic_tool_loop`)
nor "call an HTTP endpoint" (`api_endpoint`) — it is "run the real agent and observe its native
spans." Where the agent isn't a local importable object (the common case across arbitrary customer
stacks), the same principle is served by the agent emitting OTel spans (Finding 3) or submitting its
output (push) — i.e. response-evaluation.

### Finding 5 — Promptfoo's own text-to-SQL guide is static-only; bi-evals extends past where it stops

Promptfoo's [text-to-SQL guide][p-sql] configures **plain model providers** (verbatim:
`- openai:gpt-5-mini` / `- openai:gpt-5`) and grades with **static validation** — `is-sql` /
`contains-sql`. It does **not** run a tool loop, does **not** call a deployed agent, and does
**not** execute the generated SQL against a database (the guide focuses "exclusively on testing raw
LLM outputs converted to SQL"). So Promptfoo treats the agent layer and execution-based accuracy as
out of scope for its base SQL story — which is exactly the gap bi-evals fills (execution-based,
multi-dimension scoring of the *real agent's* response). We are extending past Promptfoo's guidance,
not contradicting it.

### What Promptfoo recommends for `anthropic_tool_loop` vs `api_endpoint` (the original question)

**Nothing directly — and the absence is itself informative.** A search of the docs found no
recommendation favouring reconstruct-the-loop over call-the-endpoint for BI/SQL evals. What exists:

- Promptfoo's SQL guide does *neither* — it tests raw models statically (Finding 5).
- Promptfoo's agent pattern is *wrap the real agent in-process* + observe native spans (Finding 4),
  never "reconstruct the loop."
- There is **no** Promptfoo pattern resembling `anthropic_tool_loop` (bi-evals reconstructing the
  agent). That approach is a bi-evals invention, consistent with the pivot demoting it to dev-only.

In short: Promptfoo neither endorses nor provides the reconstruct-the-loop path; every agent example
it ships is a wrap/observe-the-real-agent pattern. That supports the pivot without needing to claim
Promptfoo issued a "don't reconstruct" instruction (it didn't, in those words).

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

The research promotes the **OTel adapter (Pivot Phase 6 / Build Stage 3)** from "someday" toward strategically
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
- [Promptfoo — Evaluate Google ADK Agents (wrap-real-agent-in-process)][p-adk]
- [Promptfoo — Evaluating LLM text-to-SQL performance (static-only)][p-sql]

[p-custom]: https://www.promptfoo.dev/docs/providers/custom-api/
[p-assert]: https://www.promptfoo.dev/docs/configuration/expected-outputs/
[p-tracing]: https://www.promptfoo.dev/docs/tracing/
[p-python]: https://www.promptfoo.dev/docs/providers/python/
[p-adk]: https://www.promptfoo.dev/docs/guides/evaluate-google-adk/
[p-sql]: https://www.promptfoo.dev/docs/guides/text-to-sql-evaluation/

> **Verification note.** Every quote in this section was pulled verbatim from the linked Promptfoo
> docs (June 2026). An earlier draft contained two fetch-summariser paraphrases quoted as if literal
> (a "wrap the real agent" recommendation, and "glass-box testing" / "agents running independently
> can emit spans"); those were removed once re-checked against the source. Treat fetched summaries as
> interpretation — quote only after verifying against the page.
