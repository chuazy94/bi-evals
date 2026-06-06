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
| **Pivot Phase 3** | Push adapter (replay) + `submit()` SDK | ⬜ next |
| **Pivot Phase 4** | Capability check (open-envelope trace) | ⬜ |
| **Pivot Phase 5** | Model-as-request honesty marker | ⬜ |

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
