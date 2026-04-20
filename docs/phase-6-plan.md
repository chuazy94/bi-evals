# Phase 6: Evaluation Quality — Variance, Drift, Freshness

## Context

Phases 1–5 built the core eval pipeline: run → score → store → report → compare. What's still missing is **signal quality**. Today's compare uses strict `pass → fail` flips on a single run of each test. That's noisy (LLM non-determinism flips tests randomly), blind to causation (a regression appears with no indication of *what changed*), and blind to rot (goldens written six months ago may no longer reflect the domain).

The Criteo engineering team's write-up on agentic evaluation calls out exactly these three failure modes, and they match patterns we're already seeing in `tmp/my-evals/` output. Phase 6 adds three features that make the existing pipeline's signal trustworthy.

Phase 6 ships:
1. **Repeat-run variance** — run each golden N times, track pass rate ± stddev, move compare from flip-based to rate-based regression detection
2. **Prompt-drift detection** — hash skill/knowledge files read during a run, surface file-level changes alongside regressions in the compare page
3. **Dataset staleness** — optional `last_verified_at` on goldens, CLI warnings and report section for stale tests

These are additive — existing single-run workflows keep working (repeats default to 1, drift and staleness are opt-in surfaces).

---

## Architecture

```
bi-evals run --repeats 3
  → Promptfoo runs each golden 3× (N trials parameter threaded through config generator)
  → each trial writes trace as usual; test_id unchanged (shared golden_file)
  → ingest aggregates: pass_count, fail_count, trial_count per (run_id, test_id)
  → prompt_snapshot captures {skill_file_path: sha256} read during the run
  → goldens carry optional last_verified_at → ingest snapshots it

report page → shows variance bars, flags stale tests
compare page → "Regressions" table includes pass-rate delta + which skill files changed
```

Storage model: extend `test_results` with trial aggregates; add a new `trial_results` table for per-trial rows (enables drilldown); add `prompt_snapshot` JSON column on `runs`.

---

## Feature 1: Repeat-Run Variance

### Data model

New table for per-trial results; existing `test_results` becomes the aggregate:

```sql
CREATE TABLE IF NOT EXISTS trial_results (
    run_id      VARCHAR NOT NULL,
    test_id     VARCHAR NOT NULL,
    trial_ix    INTEGER NOT NULL,       -- 0-based trial index
    passed      BOOLEAN NOT NULL,
    score       DOUBLE NOT NULL,
    latency_ms  BIGINT,
    cost_usd    DOUBLE,
    trace_json  JSON,
    PRIMARY KEY (run_id, test_id, trial_ix)
);

-- Extend test_results (aggregate across trials):
ALTER TABLE test_results ADD COLUMN trial_count    INTEGER DEFAULT 1;
ALTER TABLE test_results ADD COLUMN pass_count     INTEGER DEFAULT 0;  -- how many trials passed
ALTER TABLE test_results ADD COLUMN pass_rate      DOUBLE;              -- pass_count / trial_count
ALTER TABLE test_results ADD COLUMN score_stddev   DOUBLE;
```

For single-trial runs (the common case) `trial_count = 1`, `pass_rate ∈ {0.0, 1.0}`, `score_stddev = 0.0` — fully backward-compatible.

### Config

```yaml
scoring:
  repeats: 1  # default; set higher for flaky suites
```

CLI override: `bi-evals run --repeats 3`.

### Promptfoo integration

Promptfoo supports `repeat: N` on a test case natively. `config_generator.py` reads `config.scoring.repeats` and sets it per test. Each trial gets a unique trace file: `results/traces/{test_id}_trial_{N}.json`.

### Compare semantics

Replace strict `a_passed != b_passed` with a pass-rate threshold:

- `regressed`: `b.pass_rate < a.pass_rate - regression_threshold` (default 0.2) OR a critical dim's pass rate drops by the threshold
- `fixed`: `b.pass_rate > a.pass_rate + regression_threshold`
- `unchanged`: within threshold

Threshold is configurable (`compare.regression_threshold`, default 0.2). Single-trial behavior falls out naturally: rates are 0.0 or 1.0, any flip clears a 0.2 threshold.

### Report changes

- Category dashboard shows average pass rate with stddev bars
- Per-test rows show `4/5 (80%)` instead of just ✓/✗

### Cost implications

Running with `repeats: 3` triples eval cost and runtime. Documented prominently; CLI shows a warning confirmation when `repeats > 1` unless `--yes` passed.

---

## Feature 2: Prompt-Drift Detection

### What to snapshot

Every file the agent actually read during the run. The `anthropic_tool_loop` provider already logs `files_read` in the trace. We collect the union across all tests and hash file contents at ingest time.

### Data model

```sql
-- Extend runs:
ALTER TABLE runs ADD COLUMN prompt_snapshot JSON;
-- Shape: {"<resolved_abs_path>": {"sha256": "…", "size": 1234}, …}
```

Hashing is done at ingest time against the file on disk (the files the run actually used). If a file has already been deleted/renamed by ingest time, record `null` for that path — signals drift even more strongly.

### Compare surface

`compare.html.j2` adds a "Prompt changes" section above the transitions table:

```
Skill files changed between runs:
  • skill/knowledge/REVENUE.md       ✎ modified  (3f2a… → 9b1c…)
  • skill/knowledge/SIGNALS.md       + added
  • skill/knowledge/OLD_CONTEXT.md   − removed
```

When a test has regressed AND a file it read changed, the regression row is annotated: `⚠️ REVENUE.md changed`.

### Query helper

```python
def prompt_diff(conn, run_a_id: str, run_b_id: str) -> PromptDiff:
    """Returns {added, removed, modified, unchanged} lists of paths."""
```

No change to compare verdict semantics — drift is informational, never flips a verdict on its own.

---

## Feature 3: Dataset Staleness

### Golden schema addition

```yaml
# golden/revenue/weekly-revenue.yaml
id: weekly-revenue
category: revenue
last_verified_at: 2026-02-10    # optional; ISO date
question: …
```

`GoldenTest` model gains `last_verified_at: date | None = None`.

### Config

```yaml
scoring:
  stale_after_days: 180  # default; 0 disables the check
```

### CLI warnings

`bi-evals run` prints a warning header if any golden in the selected set has:
- `last_verified_at is None` → "unverified" (not stale, but flagged once)
- `today - last_verified_at > stale_after_days` → "stale"

Exit code unaffected.

### Ingest + report

- Snapshot `last_verified_at` into `test_results.last_verified_at` at ingest time (same pattern as `reference_sql`)
- Report adds a "Dataset freshness" section: count of stale/unverified goldens, worst offenders with age

### What this is NOT

No auto-regeneration of goldens. The warning tells the user to manually re-run and re-verify the reference SQL. Automated reverification is a future phase.

---

## File Changes

| File | Action | Purpose |
|------|--------|---------|
| `src/bi_evals/config.py` | Modify | Add `scoring.repeats`, `scoring.stale_after_days`, `compare.regression_threshold` |
| `src/bi_evals/golden/model.py` | Modify | Add `last_verified_at: date \| None` |
| `src/bi_evals/runner/config_generator.py` | Modify | Thread `repeats` into `promptfooconfig.yaml` per test |
| `src/bi_evals/runner/executor.py` | Modify | Surface staleness warnings before invoking Promptfoo |
| `src/bi_evals/store/schema.py` | Modify | New `trial_results` table; `ALTER` columns on `test_results` and `runs` |
| `src/bi_evals/store/ingest.py` | Modify | Aggregate trials → `test_results`; hash files into `prompt_snapshot`; snapshot `last_verified_at` |
| `src/bi_evals/store/queries.py` | Modify | New `prompt_diff()`; `test_diff` returns pass rates not booleans |
| `src/bi_evals/compare/diff.py` | Modify | Rate-based regression classifier with configurable threshold |
| `src/bi_evals/report/builder.py` | Modify | Staleness section; variance bars; prompt-change annotations |
| `src/bi_evals/report/templates/report.html.j2` | Modify | Render new sections |
| `src/bi_evals/report/templates/compare.html.j2` | Modify | Prompt-diff section |
| `src/bi_evals/cli.py` | Modify | `--repeats` flag on `run`; staleness warnings; confirm-on-high-repeat |
| `tests/fixtures/eval_sample/` | Modify | Add a multi-trial sample run |
| `tests/test_variance.py` | New | Trial aggregation, rate-based compare |
| `tests/test_prompt_drift.py` | New | Snapshot, diff helper, compare annotations |
| `tests/test_staleness.py` | New | Date math, warning triggers, report section |

---

## Migration

The ALTER TABLE statements run idempotently on first open via `ensure_schema()`. DuckDB supports `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (checked). Existing databases from Phase 5 upgrade cleanly without data loss; new columns get defaults.

Re-ingesting an old eval JSON (no trials, no prompt snapshot) produces `trial_count=1`, `prompt_snapshot=null` — graceful degradation.

---

## Risks / Gotchas

- **Cost explosion with high repeats**: mitigate with confirmation prompt and `--yes` bypass.
- **Trace file volume with N trials**: `trial_results.trace_json` can blow up storage. Apply existing 1MB guardrail per trial.
- **Hashing cost at ingest**: skill files are small (< 50KB typical). Negligible. Skip files larger than 1MB with a warning.
- **Stale data in `last_verified_at`**: users will forget to update this. Document clearly; consider a `bi-evals verify <golden>` command in a later phase that updates it after a successful re-run.
- **Drift false positives from whitespace/formatter changes**: hash by normalized content (strip trailing whitespace, consistent line endings). Or accept noise — the signal is coarse by design.
- **Regression threshold tuning**: 0.2 is a guess. Plan to revisit after first real-world use.

---

## Verification

```bash
# Unit tests
uv run python -m pytest tests/test_variance.py tests/test_prompt_drift.py tests/test_staleness.py -v

# Full suite regressions
uv run python -m pytest tests/ -m "not integration" -v

# Manual: repeat run
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases --repeats 3
# Expect: 3 trials per test, report shows pass rates

# Manual: drift detection
# (edit a skill file between two runs)
uv run bi-evals --config tmp/my-evals/bi-evals.yaml compare prev latest
# Expect: compare page shows which files changed

# Manual: staleness
# (set last_verified_at to a date > 180d ago on a golden)
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run
# Expect: warning banner listing stale goldens before Promptfoo starts
```

Success criteria:
- Single-trial workflows unchanged (`repeats: 1` is the default and produces identical behavior to Phase 5)
- Multi-trial pass rates visible in both report and compare
- Compare annotates regressed tests with the skill files that changed between runs
- Stale goldens surface clearly at `run` time and in the report
