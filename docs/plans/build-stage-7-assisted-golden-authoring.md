# Assisted golden authoring (design / proposal)

> **Status: proposal, post-MVP. Sequenced after Build Stage 6** (its grounded tier depends on Stage 6's
> semantic-model loader). This directly serves the `CLAUDE.md` north star — *"author your first golden
> test with light assistance — quickly"* — so unlike most post-MVP items it is *on* the north star, not
> adjacent to it. It is still gated behind Stage 6 for the trustworthy tier; see "Sequencing" below.

## The gap

bi-evals is reference-based by design: a golden test carries `reference_sql` — the correct query for a
question — and the scorer executes it as ground truth. **This requirement is correct and non-negotiable.**
Every serious eval framework grades against ground truth; the golden bank *is* the product, and a broad
bank across domains is the asset, not a tax. bi-evals should not apologize for requiring it.

The real, narrower gap is **cold-start cost**: authoring the *first* domain's worth of goldens is
expensive because there is no tooling to accelerate it. Today a user hand-writes every `reference_sql`
from scratch — exact tables, joins, filters, dialect. For a 30-question suite that is 30 hand-authored,
hand-verified answer keys before the user sees a single scored result. Teams running a BI agent in
production generally *do* have queries they trust (increasingly compiled from a semantic model), but
turning that trust into a formal golden bank is manual, per-question work.

The fix is **not** to weaken ground truth (e.g. accepting an opaque expected-result-set instead of SQL —
explicitly rejected: it trades away the trusted, inspectable, semantic-model-backed SQL the method
depends on). The fix is to make building the golden bank **faster**: convert authoring from *writing*
`reference_sql` to *reviewing* a grounded draft. The golden SQL list stays; we just accelerate producing
it.

## Design: two tiers, grounding improves with the semantic model

A drafter takes a `question` (and optionally a schema / semantic model) and emits a **candidate golden
YAML** — `reference_sql` filled in, plus `id` / `category` / `expected` scaffolding — for a human to
approve, edit, or reject. **It never trusts a draft blindly; the human approves every one.** The output
is a normal golden test file, identical in shape to a hand-authored one.

The tiers differ only in *what grounds the draft*:

### Tier 1 — schema-only drafter (no Stage 6 dependency, ships first)

- **Input:** the question + table/column schema (from the warehouse `INFORMATION_SCHEMA` or a supplied
  schema file).
- **Draft:** an LLM proposes `reference_sql` from the raw schema.
- **Trust level:** *weaker* — the model guesses the correct join/metric logic from column names alone.
  The human review step is load-bearing here, not optional polish.
- **Why ship it anyway:** it depends on nothing already-unbuilt, and it turns a blank-page problem into
  an edit-this-draft problem even before a semantic model exists. It is the right on-ramp for a team
  that has a warehouse but no formal semantic layer yet.

### Tier 2 — semantic-model-grounded drafter (depends on Stage 6 loader)

- **Input:** the question + the customer's governed semantic model (loaded via the **Build Stage 6
  semantic-model loader** — see `docs/plans/build-stage-6-semantic-layer-scoring.md`).
- **Draft:** `reference_sql` compiled/derived from the model's actual metric and dimension definitions —
  e.g. `revenue → sum(order_total)` grouped by `customer__region` — not from column-name guesswork.
- **Trust level:** *high* — the draft is grounded in the customer's own governed logic, so it is far
  more likely to be correct on the first pass, and review is confirmation rather than repair.
- **Why it needs Stage 6:** the semantic-model loader (`SemanticLayerParser.load_definitions()` in the
  Stage 6 design) is exactly the artifact that makes grounded drafting possible. Reusing it keeps one
  loader doing double duty: it grounds the drafts *and* it is what Stage 6's `metric_definition_integrity`
  dimension scores. If Apache Ossie matures into the loader's input (see the OSI note in the Stage 6
  doc), Tier 2 inherits that standard source for free.

The two tiers share one review loop and one output format; Tier 2 is a grounding upgrade to Tier 1, not a
rewrite.

## Relationship to production-traffic golden import (Stage 9)

`STATUS.md` Build Stage 9 already parks **`production-traffic golden import`** — seeding goldens from past
agent answers you have confirmed correct (the "verified query repository" pattern Snowflake/Databricks
push, and that `docs/mvp-eval-platform.md` calls the highest-impact improvement available). That is a
**sibling accelerator to this one, not a duplicate**:

- **Production-traffic import** starts from *answers the agent already produced* — the agent's own emitted
  SQL becomes `reference_sql` after a human confirms it. Best when you have a log of real, correct runs.
- **Assisted authoring** (this doc) starts from *a question with no answer yet* — the drafter proposes
  `reference_sql`. Best when you are authoring net-new coverage for a domain the agent hasn't been run
  against.

They converge on the same output (a reviewed golden with trusted `reference_sql`) and should share the
review/approve UX. When this stage is scheduled, decide whether to pull the Stage 9 import item forward to
land alongside it so the two accelerators ship as one coherent authoring story.

## Rough surface (to be detailed when scheduled)

- A CLI entry point (working name `bi-evals author` / `bi-evals draft`) that takes a question or a file
  of questions and writes candidate goldens into `golden/` for review.
- Batch mode over a question list — the cold-start case is authoring *many* goldens, so one-at-a-time is
  the wrong default.
- Every draft written with a clear "unreviewed" marker (candidate `last_verified_at` unset / a status
  the report surfaces) so an un-reviewed draft can never masquerade as a verified golden.
- Tier 2 wired to the Stage 6 `SemanticLayerParser.load_definitions()` output; Tier 1 to a schema reader.

## Open questions

- **Draft granularity** — full golden YAML (id/category/expected/reference_sql) vs. just `reference_sql`
  for the human to drop into a stub? Leaning full YAML with everything but the human's judgment pre-filled.
- **Review UX** — CLI diff/approve, or write-then-edit-in-place? The report already has a freshness /
  unverified surface (`last_verified_at`) that could double as the review queue.
- **Tier 1 guardrails** — how loudly to warn that a schema-only draft is a weak guess. Possibly refuse to
  stamp `last_verified_at` on any drafted golden regardless of tier until a human signs off.
- **Merge with Stage 9 import** — ship the two accelerators together, or Tier 1 first and import later?
