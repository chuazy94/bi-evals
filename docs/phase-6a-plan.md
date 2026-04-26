# Phase 6a: Signal Reliability — Variance, Multi-Model, Outcome Stability

## Context

Phases 1–5 produce a **single bit of information per test per run**: pass or fail. In practice that bit is noisy — LLMs are non-deterministic even at `temperature=0`, and a "regression" from the `compare` command might just be an unlucky draw. Today we have no way to distinguish "this test is flaky" from "this test broke," and no way to answer "would Haiku be 90% as accurate as Sonnet at 20% of the cost?"

Phase 6a fixes the quality of the signal itself. Everything downstream — Phase 6b's causation tooling, Phase 6c's anti-patterns, Phase 8's UI — gets sharper when the underlying data is trustworthy. Three features:

1. **Repeat-run variance** — run each golden N times, aggregate pass rate + stddev. Replace single-bit pass/fail with a distribution.
2. **Multi-model evaluation** — run the same goldens across multiple models in one eval. Surface quality-vs-cost tradeoffs.
3. **Cross-run outcome stability** — track per-test flip history across all runs, not just pairwise compares. Find flaky tests quickly.

This is the highest-leverage phase in the "improve eval performance" umbrella because it changes *what data exists*. 6b and 6c build on it.

---

## Architecture shift

Today's data model treats `(run_id, test_id)` as the atomic observation. Phase 6a makes the atomic observation `(run_id, test_id, model, trial_ix)` — the cartesian product of "how many times we tried" and "which model we used."

This is a schema change. The existing `test_results` table stops holding per-trial data and becomes a per-test aggregate. A new `trial_results` table holds the raw per-trial rows. Existing single-model, single-trial runs remain the default — they just produce one `trial_results` row per test.

```
Before:  one `test_results` row per (run, test), holds the outcome directly
After:   one `trial_results` row per (run, test, model, trial_ix), holds the outcome
         `test_results` becomes an aggregate over trials (pass_count, pass_rate, stddev)
```

---

## Feature 1: Repeat-run variance

### Data model changes

New table:

```sql
CREATE TABLE IF NOT EXISTS trial_results (
    run_id      VARCHAR NOT NULL,
    test_id     VARCHAR NOT NULL,
    model       VARCHAR NOT NULL,         -- see Feature 2
    trial_ix    INTEGER NOT NULL,
    passed      BOOLEAN NOT NULL,
    score       DOUBLE NOT NULL,
    generated_sql TEXT,
    fail_reason TEXT,
    latency_ms  BIGINT,
    cost_usd    DOUBLE,
    prompt_tokens BIGINT,
    completion_tokens BIGINT,
    trace_json  JSON,
    PRIMARY KEY (run_id, test_id, model, trial_ix)
);
```

`dimension_results` extends the same way — per-trial, not per-test:

```sql
ALTER TABLE dimension_results ADD COLUMN model VARCHAR NOT NULL DEFAULT '';
ALTER TABLE dimension_results ADD COLUMN trial_ix INTEGER NOT NULL DEFAULT 0;
-- New primary key: (run_id, test_id, model, trial_ix, dimension)
```

`test_results` becomes the aggregate over trials for a single model:

```sql
ALTER TABLE test_results ADD COLUMN trial_count  INTEGER DEFAULT 1;
ALTER TABLE test_results ADD COLUMN pass_count   INTEGER DEFAULT 0;
ALTER TABLE test_results ADD COLUMN pass_rate    DOUBLE;     -- pass_count / trial_count
ALTER TABLE test_results ADD COLUMN score_mean   DOUBLE;
ALTER TABLE test_results ADD COLUMN score_stddev DOUBLE;
-- `generated_sql`, `trace_json`, etc. move to trial_results;
-- test_results keeps the representative trial's values (e.g., the last one) for convenience
```

For a single-model, single-trial run: `trial_count = 1`, `pass_rate ∈ {0.0, 1.0}`, `score_stddev = 0.0`. Fully backward-compatible.

### Config

```yaml
scoring:
  repeats: 1            # default; raise for flakiness detection
```

CLI override: `bi-evals run --repeats 5`.

### Promptfoo integration

Promptfoo supports `repeat: N` natively on a test case. `runner/config_generator.py` reads `config.scoring.repeats` and writes `repeat: N` into each generated test. Each trial writes its trace to `results/traces/{test_id}_trial_{N}.json`.

### Compare semantics

Replace strict `a_passed != b_passed` with a pass-rate threshold check:

- `regressed` — `b.pass_rate < a.pass_rate - regression_threshold` OR any critical dim's pass rate drops by the threshold
- `fixed` — `b.pass_rate > a.pass_rate + regression_threshold`
- `unchanged` — within threshold
- `added` / `removed` — unchanged semantics

Threshold is config-driven (`compare.regression_threshold`, default `0.2`). For single-trial runs the rates are 0.0/1.0 and a flip always clears 0.2 — so existing behavior is preserved.

### Report changes

- Category dashboard shows average pass rate ± stddev as a bar-with-error-range
- Per-test rows show `4/5 (80%)` instead of ✓/✗ when `trial_count > 1`

### Cost warning

When `repeats > 1` is set via CLI, the `run` command prints the estimated cost multiplier and a confirmation prompt (bypassed with `--yes`).

---

## Feature 2: Multi-model evaluation

### Config changes

Today `agent.model` is a single string. Extend it to accept a list:

```yaml
agent:
  type: "anthropic_tool_loop"
  models:                                  # NEW: list form
    - claude-sonnet-4-6
    - claude-haiku-4-5
  # `model:` still accepted as a single-element shorthand for backward compat
  system_prompt: "system-prompt.md"
  ...
```

Validation: `model` and `models` are mutually exclusive; exactly one must be set. `BiEvalsConfig.agent.models` is always a `list[str]` internally (single `model` → single-element list at load time).

### Runner changes

Generating `promptfooconfig.yaml` with multi-model is a cartesian product: each test case runs once per model per trial. Promptfoo supports multiple providers natively — the config generator emits one provider block per model, and Promptfoo executes the full matrix.

Trace file naming: `results/traces/{test_id}_{model_slug}_trial_{N}.json`. Model slug strips provider prefixes and normalizes hyphens for filesystem safety.

### Ingest changes

`ingest_run()` walks every trial in the Promptfoo JSON, extracts the model from the provider field, and writes one `trial_results` row per `(test, model, trial_ix)`. Aggregation into `test_results` happens **per model** — one `test_results` row per `(run, test, model)`. That means for a 5-test run with 2 models, `test_results` has 10 rows, not 5.

### New query helpers

```python
def list_models_for_run(conn, run_id: str) -> list[str]: ...
def test_results_by_model(conn, run_id: str, test_id: str) -> dict[str, TestRow]: ...
def model_summary(conn, run_id: str) -> list[ModelSummary]:
    # pass_rate, avg_score, total_cost, avg_latency per model
```

### Report changes

A new section: **Model comparison**. For a multi-model run, show:

- Summary table: model, pass_rate, avg score, total cost, avg latency
- Per-test matrix: rows are tests, columns are models, cells are ✓/✗ (or pass rate if repeats > 1)
- "Quality vs cost" scatter: x = total cost, y = pass rate, one point per model. Renders as an HTML `<svg>` (no JS) to keep the report self-contained.

Single-model runs skip this section entirely.

### Compare semantics for multi-model

Comparing two single-model runs is unchanged. Comparing two multi-model runs:
- Match by `(test_id, model)` — same model across runs is the right comparison
- Verdict aggregates across all (test, model) pairs
- A model present in run A but not run B → treated like added/removed tests (never flips verdict red)

### Cost implications

2 models × 5 goldens × 3 repeats = 30 trials. Document prominently. The confirmation prompt covers this too.

---

## Feature 3: Cross-run outcome stability

### What it is

For any given test, track the full pass/fail history across all runs in the DB. "This test has flipped 4 times in the last 10 runs" is a flakiness signal that pairwise compares can't surface.

### No new tables

This feature is pure query over existing data. Once `trial_results` exists (Feature 1), stability queries follow:

```python
@dataclass(frozen=True)
class TestStability:
    test_id: str
    runs_observed: int
    flip_count: int           # number of pass → fail or fail → pass transitions
    longest_pass_streak: int
    longest_fail_streak: int
    current_streak: int       # positive = pass, negative = fail
    pass_rate_overall: float

def test_stability(conn, test_id: str, *, last_n_runs: int = 10) -> TestStability: ...
def flakiest_tests(conn, *, last_n_runs: int = 10, limit: int = 20) -> list[TestStability]: ...
```

### Surfaces

- **CLI**: new command `bi-evals flakiness [--last-n 10]` lists tests sorted by flip count. Text output; no new HTML.
- **Report**: add a "Stability" section listing the top 5 flakiest tests with their flip counts and current streak.
- **Compare**: unchanged. Stability is about history, not pair-wise diff.
- **Phase 8 (UI)** later: per-test history view gets a prominent "flipped N times" badge.

### When it's useful

Only meaningful with 5+ runs in history. For a fresh project, the output is all zeros until history accumulates. Document this.

---

## File changes

| File | Action | Purpose |
|---|---|---|
| `src/bi_evals/config.py` | Modify | `scoring.repeats`, `compare.regression_threshold`, `agent.models` (list) |
| `src/bi_evals/runner/config_generator.py` | Modify | Thread `repeats` + multi-model into promptfoo config |
| `src/bi_evals/runner/executor.py` | Modify | Cost estimation + confirmation prompt for high-cost runs |
| `src/bi_evals/store/schema.py` | Modify | New `trial_results` table; ALTER `test_results`, `dimension_results` |
| `src/bi_evals/store/ingest.py` | Modify | Walk trials × models; aggregate into test_results per model |
| `src/bi_evals/store/queries.py` | Modify | New helpers for multi-model + stability |
| `src/bi_evals/store/duckdb_store.py` | Modify | Expose new queries (if ResultsStore exists by then) |
| `src/bi_evals/compare/diff.py` | Modify | Rate-based regression classifier with threshold |
| `src/bi_evals/report/builder.py` | Modify | Variance bars, model comparison section, stability section |
| `src/bi_evals/report/templates/report.html.j2` | Modify | Render new sections |
| `src/bi_evals/report/templates/compare.html.j2` | Modify | Show pass rates not booleans |
| `src/bi_evals/cli.py` | Modify | `--repeats` flag; `flakiness` command; multi-model status line |
| `tests/fixtures/eval_sample/` | Modify | Add a multi-trial, multi-model sample |
| `tests/test_variance.py` | New | Trial aggregation, rate-based compare, stddev |
| `tests/test_multi_model.py` | New | Config parsing, ingest matrix, per-model aggregation |
| `tests/test_stability.py` | New | Flip counting, streak calculation, flakiness ranking |

---

## Migration

`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` runs idempotently via `ensure_schema()`. DuckDB supports this. Existing Phase 5 databases upgrade cleanly:
- Existing `test_results` rows get default values (`trial_count=1`, `pass_count=1 if passed else 0`, etc.)
- Existing `dimension_results` rows get `model=''`, `trial_ix=0` (they were single-trial, single-model by construction)
- `trial_results` is newly empty for pre-existing runs

Backfill strategy: re-running `bi-evals ingest <path>` on any historical eval JSON repopulates `trial_results` from scratch. Idempotent by design.

---

## Risks / Gotchas

- **Trial explosion on cost.** 5 tests × 3 models × 5 repeats = 75 trials per run. Mitigate with confirmation prompt and clear cost estimation in CLI.
- **Schema change is the biggest in the project's history.** Test the migration carefully against existing DBs in `tmp/my-evals/`. Consider a version bump in `runs` to track schema version if we end up needing multiple forward migrations.
- **Promptfoo multi-provider behavior.** Need to verify that Promptfoo correctly reports per-provider costs/tokens when multiple providers are configured. If it doesn't, we'll need to compute these per-trial at ingest time.
- **Model slug collisions.** Two providers with the same underlying model ID (e.g., `anthropic/claude-sonnet-4-6` vs `bedrock/claude-sonnet-4-6`) would collide on slug. Include provider prefix in the stored `model` field to avoid this.
- **Stability signal is noisy for new tests.** A test with 3 runs that happens to have flipped twice looks flaky but isn't statistically meaningful. Report should note sample size alongside flip count.
- **Regression threshold default (0.2) is a guess.** Revisit after first real-world use. Low threshold = false regressions from noise; high threshold = real regressions missed.
- **Backward compat for `model:` singular.** Keep the old field accepting a string. Fail loudly if both `model` and `models` are set.

---

## Verification

```bash
# Unit tests
uv run python -m pytest tests/test_variance.py tests/test_multi_model.py tests/test_stability.py -v

# Full suite regressions
uv run python -m pytest tests/ -m "not integration" -v

# Manual: single-model repeated run
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases --repeats 3
# Expect: 3 trials per test, report shows pass rates + variance bars

# Manual: multi-model run
# (edit bi-evals.yaml to use models: [sonnet-4-6, haiku-4-5])
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases
# Expect: model comparison section in report, per-model pass rates

# Manual: stability
uv run bi-evals --config tmp/my-evals/bi-evals.yaml flakiness --last-n 10
# Expect: ranked list of tests by flip count
```

Success criteria:
- Single-trial, single-model workflows unchanged — 187 existing tests still pass
- Multi-trial pass rates visible in report with stddev bars
- Multi-model runs produce quality-vs-cost scatter in the report
- `bi-evals flakiness` command surfaces historically flaky tests
- DB schema migrates cleanly from Phase 5 shape
