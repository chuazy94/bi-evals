# Plan: Build Stage 2 — Capability check (open-envelope trace)

> **Status: proposed — not yet implemented.** Design under discussion with the
> user; the outcome classification and user-facing messages below are the core
> of the stage and were specified first, before any code.

## Goal

When bi-evals cannot score a dimension, it must say **"I can't know"** — never
report it as **"I know it failed."** At score time, classify every dimension
outcome honestly, tell the user exactly *why* something wasn't evaluated and
*what to submit to change that*, and surface it at three moments: pre-flight
(before warehouse spend), per test, and per suite.

Motivating incident (firsthand, twice): a `skill_path_correctness` failure in
the demo project was really "no trace submitted" — the harness sent
`trace={"tool_calls": []}` while the agent's audit log had the real tool calls
all along. The score said *the agent read the wrong files*; the truth was
*bi-evals had no way to know what the agent read*.

This stage unblocks Build Stages 3 (model honesty), 4 (OTel), and 6
(semantic-layer scoring) — all three add richer submission fields and need the
same "absent field → explicit unknown, never silent failure" machinery.

## Where things stand today (verified 2026-07-13, not assumed)

Three different "couldn't score this" situations already exist, conflated into
booleans and string prefixes that can't tell them apart:

| Situation | Today's behavior | Verdict |
|---|---|---|
| Golden declares no `anti_patterns` / no `expected_skill_path` — nothing to check | `_skip()` in `scorer/dimensions.py`: **`passed=True, score=1.0`**, reason `"skipped: …"`; vacuous dims dropped from the HTML report | Wrong-ish: a vacuous dim silently **inflates** the weighted score with a free 1.0 |
| Generated SQL failed to execute — row dims can't run | `scorer/entry.py`: `passed=False, score=0.0`, reason `"skipped: SQL execution failed"` | Correct outcome (agent's fault), but the word "skipped" lies — it's a cascade **failure** |
| No usable trace submitted — `skill_path_correctness` | `check_skill_path_correctness([])` → **`passed=False`**, indistinguishable from "agent invoked the wrong tools" | **The bug this stage fixes**: "can't know" reported as "know it failed" |

- `dimension_results.passed` is `BOOLEAN NOT NULL` (`store/schema.py`) — the
  store cannot represent a third state; skips survive only as a reason-string
  prefix.
- `_trace_from_row` (`provider/registry.py`) already implements the *parsing*
  half of the open envelope: accepts a list of steps or a
  `{tool_calls, files_read}` dict, reads the keys it understands, ignores the
  rest, returns `([], [])` when nothing usable is present. This stage adds the
  *consequences* half.
- `doctor` already prints a scoring-coverage report for `api_endpoint` —
  precedent for pre-flight capability warnings; push/SDK have nothing.
- `compare/diff.py::_regressed_critical_dims` already skips dims whose rate is
  `None` on either side — if `not_evaluated` ingests as NULL, the gate treats
  unknown as absent (not zero) with no diff-engine change.

## Why a trace goes missing (cause taxonomy)

The causes group by what the right response is — the messages must not treat
them as one thing:

| Group | Cause | Right response |
|---|---|---|
| **1. Plumbing** | Harness drops it (`trace={"tool_calls": []}` while the agent's audit log has the real calls); agent's API doesn't expose it; log harvest never captured tool calls | Unlock hint — the adoption ladder |
| **2. Unusable shape** | Trace submitted but zero usable steps: entries missing `tool_name`/`tool_input`, paths under a key other than `path`, free-text narration instead of structured steps | Shape diagnostic — telling this user "no trace submitted" would be false |
| **3. Nothing to trace** | Agent is a single prompt→SQL call: no tools, no retrieval. The dimension is permanently inapplicable | "Remove the dimension from `scoring.dimensions`" — an unlock hint here is nagging toward something that can't exist |
| **4. Partial coverage** | Some rows have traces, others don't (error/timeout paths return none; mixed harvest sources) | Per-case status + suite ratio ("7/12"); a ratio *drop* between runs is itself a signal |
| **5. Deliberate withholding** | Security/privacy strips traces before submission | State the consequence, no moralizing |

bi-evals cannot distinguish Group 1 from Group 3 (both look like "no trace"),
so that message must carry both branches. It **can** distinguish Group 2
(trace key present, zero usable steps) and must say so explicitly.

## The outcome classification

Every dimension result gets a first-class status (new enum on
`DimensionResult`, persisted to the store — replaces string-prefix encoding):

| Status | Meaning | Whose "fault" | Weighted score | Critical-dim gating |
|---|---|---|---|---|
| `pass` | Evaluated; correct | — | counts | satisfies |
| `fail` | Evaluated; wrong | agent | counts (0) | fails the test |
| `fail` (upstream) | Could not evaluate **because the agent failed earlier** (execution failed → row dims) | agent | counts (0) | fails the test |
| `not_evaluated` | Submission lacks the data — **bi-evals cannot know** | submitter / integration | **excluded** (numerator *and* denominator) | **open decision D1** |
| `skipped` | Golden declares nothing to check — vacuously true | golden author (by design) | **excluded** (open decision D2 — today it's a free 1.0) | n/a |

`fail (upstream)` stays a failure — it is stored as `fail` with a
`cascade_from: execution` marker in the reason; the agent produced SQL that
didn't run, and everything downstream of that is legitimately its fault.

## User-facing messages (exact strings)

The strings below are the contract of this stage. Placeholders in `{braces}`.
Every `not_evaluated` message has three parts: **what happened → what it means
→ what to do** (with the no-tools escape hatch where bi-evals can't tell
Groups 1 and 3 apart).

### Per-dimension reasons (report drilldown, `dimension_results.reason`, ui)

**NE-1 — trace absent entirely** (Group 1/3/5):

> `not evaluated: the submission has no trace, so bi-evals cannot know which
> tools or files the agent used. To enable: submit trace.tool_calls as
> [{"tool_name": ..., "tool_input": {...}}] (docs/instrumenting-your-agent.md).
> If your agent has no tools, remove skill_path_correctness from
> scoring.dimensions instead.`

**NE-2 — trace present but no usable steps** (Group 2):

> `not evaluated: a trace was submitted but none of its {n} entries were
> usable — each step needs "tool_name" and "tool_input" keys. Got keys:
> {observed_keys}. See docs/instrumenting-your-agent.md ("The trace shape
> bi-evals understands").`

**NE-3 — files_read needed but not derivable** (variant of NE-2, when the
golden's `expected_skill_path` matches on file paths and steps carry no
`tool_input.path`):

> `not evaluated: trace steps carry no file paths ("path" key absent from
> tool_input) and no top-level files_read list was submitted, so file-read
> checks cannot run. Emit files_read explicitly if your file tool's argument
> isn't called "path".`

**SKIP-1 — golden declares nothing** (exists today; wording aligned):

> `skipped: this golden declares no expected_skill_path — nothing to check.`
> *(same pattern for anti_patterns)*

**CASCADE-1 — upstream execution failure** (replaces today's misleading
`"skipped: SQL execution failed"`):

> `failed upstream: the generated SQL did not execute, so result comparison
> was impossible. Fix the execution failure first — this dimension counts as
> failed because the agent's SQL never produced rows to compare.`

**Genuine failures keep their current specific reasons** (wrong tables, value
mismatch, missing rows, `no SQL could be extracted from response_text`, agent
`error` rows) — this stage does not touch them; they are true failures.

### Suite-level capability panel (report HTML, top of page)

Rendered only when at least one dimension has `not_evaluated` rows:

> `⚠ Capability: trace usable in {k}/{n} submissions → skill_path_correctness
> evaluated for {k} case(s), not evaluated for {n−k}. Not-evaluated dimensions
> are excluded from scores — they are neither passes nor failures.`

With `k = 0`, one extra line:

> `No submission carried a usable trace. If this is unexpected, check the
> harness passes the agent's trace through to submit(); if your agent has no
> tools, remove skill_path_correctness from scoring.dimensions to silence
> this.`

### Pre-flight (`bi-evals doctor` / push validation / SDK `score()` start)

Before any warehouse spend, from the same capability map:

> `warning: 0 of 12 rows contain a usable trace — skill_path_correctness will
> not be evaluated this run (structural and result dimensions are unaffected).`

SDK equivalent (via the existing `bi_evals.sdk` logger, INFO):

> `Capability: trace usable in 7/12 submissions; skill_path_correctness will
> be scored for 7 case(s) only.`

### Critical-dimension conflict (open decision D1 — proposed default text)

If a dimension listed in `scoring.critical_dimensions` is `not_evaluated`:

> `FAIL: critical dimension skill_path_correctness could not be evaluated (no
> usable trace submitted). A critical dimension must be verifiable to pass —
> submit the data it needs, or remove it from scoring.critical_dimensions.`

## The capability map

One declarative table, single source of truth for the scorer, pre-flight, and
messages (new module `scorer/capability.py`):

| Dimension | Requires from submission | Missing → |
|---|---|---|
| `execution` | extractable SQL | genuine **fail** (Tier-4 agent: nothing to score — today's behavior, unchanged) |
| `row_completeness`, `row_precision`, `value_accuracy` | `execution` passed | **fail (upstream)** — CASCADE-1 |
| `table_alignment`, `column_alignment`, `filter_correctness`, `no_hallucinated_columns`, `anti_pattern_compliance` | parseable SQL | follows `execution` (unparseable SQL fails execution anyway) |
| `skill_path_correctness` | usable trace steps (`tool_name` + `tool_input`); file-path matching additionally needs `tool_input.path` or `files_read` | **not_evaluated** — NE-1/NE-2/NE-3 |
| *(golden-side, all dims)* | golden declares something to check | **skipped** — SKIP-1 |

Detection is two-stage, reusing `_trace_from_row`'s parse result: *absent* →
NE-1; *present but zero usable steps* → NE-2 (with observed keys); *usable* →
evaluate normally (a genuine wrong-files answer still fails, as it should).

## Decisions (resolved with the user, 2026-07-13)

- **D1 — critical dimension that cannot be evaluated: FAIL.** "Critical" means
  *must be verified*; absence of evidence cannot satisfy it. Distinct message
  (see above) so it never reads as agent misbehavior. Default config keeps
  `skill_path_correctness` diagnostic, so the common case is unaffected.
- **D2 — fix the vacuous-skip score inflation: YES.** Unify on "excluded from
  numerator and denominator" for both `skipped` and `not_evaluated`. This
  *shifts existing weighted scores* (a golden with no anti_patterns loses a
  free weight from its denominator) — changelog **Changed** entry + re-baseline
  note; pass/fail flips possible near `pass_threshold`.
- **D3 — leave history as-is.** No backfill of old DuckDB rows; only new
  ingests carry `status`. Historical rows keep boolean-only semantics; the
  status column is nullable / defaulted so old and new rows coexist, and
  queries treat NULL status as "pre-Stage-2 row".

## Surfacing summary (three moments)

1. **Pre-flight** (`doctor`, push validation, SDK `score()` banner) — coverage
   ratios from the capability map, before warehouse spend. Catches Group-1
   plumbing mistakes at the cheapest moment.
2. **Per test** (report drilldown, ui) — the NE-x reason strings verbatim.
3. **Per suite** (report capability panel; SDK INFO log) — ratios + the k=0
   escape-hatch line.

Compare/gate: `not_evaluated` ingests as NULL rates → the diff engine already
excludes NULL-rate dims from regression math, so adding a trace later doesn't
read as a "fix" and dropping one doesn't read as a regression. The capability
*ratio* itself surfacing in compare (trace coverage dropped 12/12 → 3/12) is
a nice-to-have, listed out of scope.

## Out of scope (explicitly deferred)

- Capability-ratio deltas in `compare`/gate (coverage-drop detection).
- `requested_model`/`actual_model` honesty (Build Stage 3 — same machinery,
  new field).
- OTel ingestion (Build Stage 4 — new adapter feeding the same envelope).
- Persisting gate outcomes / capability history as first-class store rows.
- Any change to how genuine failures are scored.

## Implementation order

1. `scorer/capability.py` — the capability map + trace-usability classifier
   (pure; unit-test the NE-1/NE-2/NE-3/usable matrix first).
2. `DimensionResult.status` enum + message strings; rewrite `_skip` and the
   cascade branches in `scorer/entry.py` to use them.
3. Scoring math: exclusion rule (D2), critical-dim policy (D1).
4. `store/schema.py` + `ingest.py`: `status` column, NULL rates for
   `not_evaluated`, migration/backfill (D3).
5. Report: per-dim status rendering + suite capability panel; ui drilldown.
6. Pre-flight: extend push validation + `doctor` + SDK `score()` banner from
   the same map.
7. `tmp/my-evals` + demo: exercise NE-1 (drop a trace) and NE-2 (malformed
   steps) against the live project.
8. Docs: `instrumenting-your-agent.md` cross-links ("the tool now tells you
   your tier"), `golden-tests-guide.md`, changelog (incl. D2's score shift).

All tests non-LLM (fixture submissions; no `test_demo_` needed).
