# Plan: Regression gating for CI (CLI + SDK)

> **Status:** proposed — not yet implemented. This is the design we agreed on
> before coding. Scope decided with the user: **CLI first**, capability =
> **absolute floor + baseline regression**, with the SDK surface designed now so
> the CLI work doesn't paint us into a corner.

## Goal

Let a user gate CI on eval quality — fail the build when the agent regresses —
through **both** entry points:

- **CLI** (`bi-evals compare …`) — for users running the framework as a command
  in GitHub Actions / GitLab CI.
- **SDK** (`bi_evals.Runner`) — the primary on-ramp per `CLAUDE.md`; today it can
  only assert an absolute floor on a single run (`assert report.pass_rate >= 0.9`)
  and has **no** baseline/regression concept at all.

The gate must answer two independent questions, both opt-in via config:

1. **Absolute** — "is this run good enough on its own?" (`min_pass_rate`).
   Matches Promptfoo's default gate; needs no baseline, so it works on a very
   first run.
2. **Relative** — "did this run get worse than the baseline?" (the existing
   `Verdict`/`regression_threshold` machinery). Matches Braintrust's per-test
   regression gate.

## Where things stand today (verified, not assumed)

- Regression logic lives in `src/bi_evals/compare/diff.py`: `classify_pairs`,
  `compute_verdict`, the `Verdict` enum (`green`/`amber`/`red`). It is **per
  (test, model) pair**, rate-based, threshold `0.2` by default. **Red if any one
  test regressed** — not an aggregate score. `added`/`removed` never gate red.
- It is consumed only by `report/builder.py:build_compare_html` (HTML) and the
  `bi-evals compare` CLI command. **`compare` writes HTML and never sets a
  nonzero exit code.** So there is no CI gate today on either surface.
- `CompareConfig` (`config.py`) exposes exactly one knob: `regression_threshold`.
- The SDK (`sdk.py`) `Runner.score()` ingests into the **same DuckDB** the CLI
  reads, and `_build_report` already opens a store connection. `RunReport` carries
  `pass_rate`, `passed/failed/total`, `failures`; `__bool__` is true when
  `failed == 0`. No import from `compare/` anywhere in `sdk.py`.

The single most important consequence: because the SDK already ingests into the
shared store, **the SDK can run the exact same `compare/diff.py` against a prior
run** — we do not need a second implementation. One gate engine, two surfaces.

## Design: one shared gate engine

Introduce a single pure function that both surfaces call, so the CLI and SDK can
never diverge (the same principle `runner_core` already applies to scoring).

```
# compare/gate.py  (new)

@dataclass(frozen=True)
class GateResult:
    verdict: Verdict            # red | amber | green (the existing enum)
    passed: bool                # did the gate pass, given the config?
    reasons: list[str]          # human-readable why (for CLI output + SDK repr)
    regression_count: int
    suite_pass_rate: float | None   # the newer run's absolute pass rate

def evaluate_gate(
    classified: list[ClassifiedPair],
    *,
    suite_pass_rate: float | None,   # newer run's absolute pass rate
    min_pass_rate: float | None,
    max_regressions_allowed: int,
    fail_on: Literal["red", "amber", "never"],
) -> GateResult: ...
```

Logic:

1. Compute `verdict = compute_verdict(classified)` (unchanged existing call).
2. Count regressions; if `regression_count <= max_regressions_allowed`, the
   regression dimension does **not** fail the gate (but the HTML verdict is left
   untouched — the budget is a *gate* concept, not a *report* concept).
3. If `min_pass_rate` is set and `suite_pass_rate < min_pass_rate`, the gate
   fails on the absolute floor regardless of the baseline (and even with no
   baseline at all).
4. `fail_on` decides which verdict levels count: `red` → fail on red only;
   `amber` → fail on amber+red; `never` → report-only, `passed=True` always
   (floor breach still reported in `reasons`, but doesn't flip `passed`).
5. `reasons` always explains the outcome ("2 tests regressed (budget 0)",
   "suite pass rate 0.78 < floor 0.85", "no regressions").

Keeping this **out of `diff.py`** preserves `compute_verdict` as the pure
report-verdict; the gate layers CI policy on top.

## Config surface (`CompareConfig`)

Add three fields (kept minimal; defaults preserve today's behavior = no gating):

```yaml
compare:
  regression_threshold: 0.2        # existing — per-test rate-drop sensitivity
  min_pass_rate: null              # NEW absolute floor on newer run; null = off
  max_regressions_allowed: 0       # NEW tolerate N regressions before gating
  fail_on: red                     # NEW red | amber | never
```

`fail_on` uses `typing.Literal` so Pydantic validates it at load time (consistent
with the codebase's "Pydantic validates at load" pattern).

## CLI surface

`bi-evals compare <a> <b>`:

- Add `--fail-on [red|amber|never]` overriding `CompareConfig.fail_on` for that
  invocation (CLI flag wins over config, config wins over default).
- After building HTML, compute `evaluate_gate(...)` from the same `classified`
  list. To avoid recomputing, `build_compare_html` returns the verdict + the data
  the gate needs (small refactor: return a result object, or expose a sibling
  `compute_compare_gate(conn, a, b, cfg)` so the HTML path and the gate path share
  one `classify_pairs` call).
- Print the gate reasons. If `not GateResult.passed`, `raise SystemExit(1)` (or
  `click` nonzero) so CI fails. `--fail-on never` always exits 0.

Result — the documented CI recipe becomes:

```bash
bi-evals run                                  # nightly / on-merge
bi-evals compare latest prev --fail-on red    # nonzero exit gates the build
```

## SDK surface (designed now, built after CLI)

The SDK keeps single-run `score()` and adds an explicit, Python-assertable gate:

```python
report = runner.score()                  # unchanged single-run report
# Absolute floor (works with no baseline — first run is fine):
assert report.passed_gate                 # uses CompareConfig.min_pass_rate / fail_on

# Baseline regression (opt-in; needs a prior run in the store):
gate = report.compare_to("prev")          # or a specific run_id / "latest"
assert gate.passed, gate.reasons
```

Mechanics (reuses the shared engine, no new logic):

- `RunReport.passed_gate` (property): evaluates the **absolute** part of the gate
  only — `min_pass_rate` + `fail_on` — from data already on the report. No store
  access needed. Cheap; safe on a first-ever run.
- `RunReport.compare_to(ref)`: opens the same DuckDB read-only, resolves `ref`
  (`"prev"`/`"latest"`/run_id) via the existing `_resolve_run_ref` helper, calls
  `test_diff` + `classify_pairs` + `evaluate_gate`, returns the `GateResult`.
  This is the SDK equivalent of `bi-evals compare`, returning a value instead of
  writing HTML / exiting.
- `RunReport` needs the `run_id` (already has it) and a handle to the config (pass
  it through from `_build_report`, or have `compare_to` take a `Runner`/config
  arg). Decide at implementation: simplest is for `Runner.score()` to stash the
  config on the report, or expose `Runner.compare(report, ref)`.

This gives SDK users the same two gates as the CLI, expressed as assertions.

## Why this shape (vs. alternatives)

- **Shared `evaluate_gate`** mirrors `runner_core` — CLI and SDK provably can't
  diverge, which is the property `sdk.py`'s own docstring already sells.
- **Absolute floor separate from verdict** matches the field: Promptfoo only does
  absolute; Braintrust does relative; production teams want **both**, with the
  relative one as the default and the absolute one as a floor.
- **`max_regressions_allowed` budget** is the standard release valve for flaky
  suites — without it, one flaky test (single-trial flip = full regression, per
  `diff.py`) turns every PR red and teams disable the gate entirely. It pairs with
  **multi-trial aggregation** (see below): aggregation cuts flake noise at the
  source, the budget backstops the remainder.
- **Defaults are no-ops** (`min_pass_rate: null`, `fail_on: red` but the command
  didn't gate before, `max_regressions_allowed: 0`): existing users see no
  behavior change until they opt in.

## Comparison model: single-baseline pairwise (same as Braintrust)

The regression verdict is **pairwise**: this run vs. **one** chosen baseline run,
diffed per `(test, model)`. It is *not* a trend over N runs, nor an average of
many baselines. This matches Braintrust, whose baseline is defined as "a specific
experiment/run used as the reference point" (singular). The only freedom is *which*
single run is the baseline (`prev` / `latest` / a pinned `run_id`).

Two enhancements Braintrust layers on the same pairwise core — both deferred here,
both already partly present in our codebase:

1. **git-aware baseline selection** (PR → last good `main` run) vs. our
   timestamp-only `prev`/`latest`. Additive; ties into the "where does the
   baseline live in CI" question below.
2. **multi-trial statistical aggregation** to tame flakiness *before* the pairwise
   diff — covered next.

## Multi-trial aggregation (mostly already built — exposure, not new machinery)

The Braintrust-style flakiness fix is: run each eval **N times in a single run**,
aggregate to a **pass rate** (not a boolean), and let the gate compare **rates**.
A test that's truly ~90% reliable randomly fails ~1 run in 10; if the gate reacts
to a single pass→fail flip it goes red on noise. Measuring a rate over N trials
makes one unlucky trial a small delta instead of a full 1.0 swing, so real
regressions still show but variance doesn't trip the gate.

**What already exists (verified end-to-end), defaulted off via `repeats: 1`:**

- `ScoringConfig.repeats: int = 1` (`config.py`) — trials per golden.
- The Promptfoo bridge honors it: `--repeat N` when `repeat > 1` (`bridge.py`), so
  N trials per golden actually execute in one run.
- `ingest.py` groups trials and computes the aggregates per test
  (`pass_rate = pass_count / trial_count`, `score_mean`, `score_stddev`,
  `overall_passed = pass_count > trial_count - pass_count` majority vote);
  per-trial rows kept in `trial_results`, aggregate in `test_results`.
- `compare/diff.py` is **already rate-based** — it thresholds `a_pass_rate` vs
  `b_pass_rate`. Its docstring: single-trial runs collapse rates to {0,1} so any
  flip clears `0.2` (legacy semantics preserved). With `repeats > 1` it becomes
  true rate-vs-rate comparison with no code change.

So enabling it is config only:

```yaml
scoring:
  repeats: 5                  # run each golden 5×, aggregate to a pass_rate
compare:
  regression_threshold: 0.2   # now a *rate* drop of 20 pts, not a boolean flip
```

`repeats: 5` → one unlucky trial is a 0.2 move (4/5→3/5); `repeats: 10` → 0.1,
comfortably under threshold.

**The genuine gaps (this is what's *not* free):**

1. **`stddev` is computed but unused by the gate.** True "is this delta within
   noise?" (confidence-interval style) would read `score_stddev` to decide whether
   a rate drop is significant vs. variance. Today the gate only thresholds the raw
   rate delta. This is the real conceptual gap vs. full statistical aggregation.
2. **No per-test trial override.** `repeats` is global; you usually want
   `repeats: 1` for cheap deterministic tests and a higher count only for known-
   flaky ones. No golden-level `repeats:` field exists yet.
3. **Cost.** `repeats: N` = N× LLM + Snowflake spend for that run — the reason it
   defaults to 1 (see `CLAUDE.md` on credit consumption), and why gap #2 matters.

**Relationship to `max_regressions_allowed`:** complementary, not redundant.
Aggregation reduces flake noise *at the source* (measure each test precisely);
the budget catches whatever still leaks through. Aggregation is the more
principled fix; the budget is the cheap backstop.

**Scope decision:** treat multi-trial as **document-and-expose now, enhance
later.** The pipeline works today via `repeats`; the plan should (a) document the
`repeats` + rate-compare CI recipe, and defer (b) `stddev`-based significance and
(c) per-test trial counts until a user hits flakiness the budget can't absorb.

## Out of scope (explicitly deferred)

- **`stddev`-based significance testing** (confidence intervals on rate deltas) and
  **per-test `repeats`** — the two real gaps from the multi-trial section above.
  The N-trials pipeline itself already works; these are refinements.
- **Postgres / shared remote store** — the "where does the baseline live in CI"
  question. Tracked separately; not required for this gate (artifact the DuckDB
  or commit a baseline JSON for now).
- **A GitHub Actions workflow file** — write it once the gate exits nonzero; it's
  a thin wrapper around the two commands above.

## Implementation order

1. `compare/gate.py`: `GateResult` + `evaluate_gate` (pure, unit-tested first).
2. `config.py`: add `min_pass_rate`, `max_regressions_allowed`, `fail_on` to
   `CompareConfig`.
3. `report/builder.py`: expose the verdict + gate inputs from the compare path
   (small refactor so the gate doesn't recompute `classify_pairs`).
4. `cli.py`: `--fail-on` flag on `compare`; compute gate; exit nonzero.
5. `sdk.py`: `RunReport.passed_gate` + `RunReport.compare_to` (reuse engine).
6. Tests: unit tests for `evaluate_gate` (floor, budget, fail_on matrix); a CLI
   exit-code test; an SDK gate test. All non-LLM (no `test_demo_` needed).
7. `tmp/my-evals/bi-evals.yaml`: add a `compare:` block demoing the new knobs so
   the feature is exercised against the live project.
8. Docs: document the multi-trial CI recipe (`scoring.repeats` + rate-based
   `regression_threshold`) — no code, since the pipeline already works; this is
   the "expose what exists" half of the multi-trial section.

## Open questions for the user

- **Default for `min_pass_rate`** — keep `null` (off; opt-in) as planned, or ship
  a non-null default so new projects get an absolute floor out of the box?
- **`compare_to` config plumbing** — acceptable for `RunReport` to hold a
  reference to the config/Runner so `compare_to` can open the store, or prefer
  `Runner.compare(report, ref)` to keep `RunReport` a pure data object?
- **Verdict vs. gate in HTML** — should the compare HTML show the *gate* outcome
  (with the budget/floor applied) too, or stay purely the report verdict and let
  the gate live only in the exit code / SDK return? (Plan currently: HTML stays
  report-verdict; gate is CI-only.)
```
