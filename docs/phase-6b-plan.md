# Phase 6b: Context and Causation — Drift, Staleness, Cost Alerts

## Context

Phase 6a makes the eval signal trustworthy. Phase 6b makes regressions **explainable**. Today when `bi-evals compare` says a test regressed, the user has to manually git-log skill files, eyeball knowledge edits, and cross-reference with when the last run happened — often with no conclusive answer. Phase 6b closes that gap with three orthogonal signals:

1. **Prompt drift detection** — hash every skill file used in a run. Compare surfaces which files changed between runs.
2. **Dataset staleness** — track when each golden was last verified. Warn the user about goldens that haven't been touched in months — these are the ones likely to validate wrong answers.
3. **Cost alerts** — flag runs that cost significantly more than historical median. Catches runaway tool loops, context bloat, or accidental model upgrades.

None of these individually transform the product. Together they shift the failure-debugging UX from "guess and git-log" to "the system tells you what changed."

6b depends on 6a for the schema shape (`trial_results` exists; `test_results` has per-run-per-model aggregates) but is otherwise independent. Can be shipped right after 6a.

---

## Feature 1: Prompt drift detection

### What gets snapshotted

Every file the agent actually read during the run. The `anthropic_tool_loop` provider already records `files_read` in the trace. At ingest time we collect the union across all trials and hash each file's contents from disk.

### Data model

```sql
ALTER TABLE runs ADD COLUMN prompt_snapshot JSON;
-- Shape:
-- {
--   "<resolved_abs_path>": { "sha256": "...", "size": 1234, "mtime": 1713456789 },
--   ...
-- }
```

Single JSON column on `runs`. Files the run never touched aren't recorded — drift detection is scoped to what actually mattered for the eval.

### Hashing at ingest time

Ingest walks `files_read` from every trial, dedupes to a set of absolute paths, and computes `sha256` of each file's contents as it exists on disk at ingest time. Write time is trivial for reasonably-sized skill files (< 100KB typical).

If a file has been deleted between the agent run and ingest, record `{ "sha256": null, "size": null, "mtime": null }` — signals drift even more strongly than a changed hash.

Files larger than 1MB are hashed but a warning is logged (we're probably hashing something that isn't a skill file).

### Content normalization

Hash the raw bytes, no normalization. Whitespace-only changes will trigger drift, but that's arguably correct behavior — a skill file edit is a skill file edit.

If this produces too much false-positive noise in practice, we can normalize later (strip trailing whitespace, normalize line endings). Start strict.

### Query helper

```python
@dataclass(frozen=True)
class PromptDiff:
    added: list[str]       # files in B but not A
    removed: list[str]     # files in A but not B
    modified: list[str]    # files in both, different hashes
    unchanged: list[str]

def prompt_diff(conn, run_a_id: str, run_b_id: str) -> PromptDiff: ...
```

### Surfaces

**`bi-evals compare` HTML** — new "Prompt changes" section above the transitions table:

```
Skill files changed between runs:
  • skills/covid-reporting/knowledge/CASE_TRACKING.md   ✎ modified  (3f2a… → 9b1c…)
  • skills/covid-reporting/knowledge/NEW_KNOWLEDGE.md   + added
  • skills/covid-reporting/OBSOLETE.md                  − removed
```

**Regression annotations** — when a test appears in the "regressed" bucket AND a file it read changed, annotate the row: `⚠️ CASE_TRACKING.md changed`. This is the "caused by" signal.

**Report (single-run) unchanged.** Drift is a comparison concept; the single-run report doesn't need it.

### What drift does NOT do

- Never flips a verdict on its own. Informational only. Regressions stay the verdict signal.
- Does not auto-diff file contents — only reports "changed." Diff viewing lives in Phase 7 UI.
- Does not track git commit hashes of skill files. Content hashing handles uncommitted edits cleanly; git tracking would miss the "I edited this during debugging and forgot" case which is exactly the one we want to catch.

---

## Feature 2: Dataset staleness

### What it is

Goldens age. A reference SQL that returned the right answer 6 months ago might not today — underlying tables evolve, columns get deprecated, domain conventions shift. Stale goldens silently validate wrong answers.

Staleness tracking is a **process prompt**, not a correctness check. It tells the user "you should re-verify this golden," not "this golden is wrong."

### Schema additions

Optional field on `GoldenTest`:

```yaml
# golden/cases/daily-cases-filtered.yaml
id: daily-cases-filtered
category: cases
question: ...
reference_sql: ...
last_verified_at: 2026-02-10   # NEW: ISO date, optional
```

Ingest snapshots this into `test_results.last_verified_at` per run (same pattern as `reference_sql`):

```sql
ALTER TABLE test_results ADD COLUMN last_verified_at DATE;
```

Rationale for snapshotting into `test_results`: if someone updates `last_verified_at` in the YAML later, historical runs still reflect what was true at run time.

### Config

```yaml
scoring:
  stale_after_days: 180   # default; 0 disables the check
```

### CLI warning

`bi-evals run` prints a warning header if any golden in the selected set has:
- `last_verified_at is None` → "unverified" (not technically stale, but flagged once as a nudge to backfill)
- `today - last_verified_at > stale_after_days` → "stale"

Example output:

```
⚠  4 goldens are stale (last verified > 180 days ago):
   - cases/daily-cases-filtered.yaml        verified 2025-09-15 (220 days ago)
   - joins/us-test-positivity.yaml          verified 2025-10-02 (203 days ago)
   ...
⚠  2 goldens have no last_verified_at set:
   - time-series/weekly-rolling-avg.yaml
   - aggregates/top-10-countries.yaml

Proceeding with eval (goldens still run; warning only).
```

Exit code unaffected. This is a nudge, not a block.

### Report additions

New "Dataset freshness" section in the single-run report:

- Count of stale/unverified goldens
- Worst offenders listed with age
- Pass rate on stale vs. fresh goldens — if stale goldens pass at a suspiciously high rate, that's a sign they're no longer meaningfully challenging

### What staleness does NOT do

- Does not auto-regenerate goldens. Re-verifying requires human judgment; the system's job is to surface the need, not to guess at answers.
- Does not fail the run. Stale goldens still execute normally.
- Doesn't apply to anti-patterns (Phase 6c) — those are about structural constraints, less sensitive to data drift.

### Future: `bi-evals verify <golden>`

Out of scope for 6b, but worth flagging. A future command could run a golden once against the current database, show the user the output, and (on user confirmation) bump `last_verified_at` to today. Closes the loop between warning and action.

---

## Feature 3: Cost alerts

### Scope: post-hoc, not pre-flight

Two variants of cost checking exist:

- **Pre-flight** — estimate cost before running, abort if over budget. Requires accurate token estimation, which is hard to get right.
- **Post-hoc** — after a run completes, flag if cost is anomalous vs. history.

6b ships the post-hoc variant. It's cheaper to build (all data already in DuckDB) and catches the same real problem: runaway costs from tool loops, context bloat, accidental model upgrades, or someone bumping `max_rounds`.

Pre-flight cost limits can come later if a real need appears.

### What counts as anomalous

A run is flagged if **total cost > 2× the median of the last 10 runs** for the same project. Threshold is configurable:

```yaml
storage:
  cost_alert_multiplier: 2.0   # default; 0 disables
  cost_alert_window: 10        # runs to compute median over
```

Per-test cost anomalies get the same treatment — any single test costing > 2× the median of that test's historical cost is flagged.

### Data model

No schema changes. Cost history is already in `runs.total_cost_usd` and `test_results.cost_usd`. Alerts are computed at ingest time (or on-demand in the report).

Storing the "anomaly detected" bit inline on `runs` is tempting but wrong — thresholds change, history grows, yesterday's anomaly might be today's normal. Compute on read.

### Query helper

```python
@dataclass(frozen=True)
class CostAlert:
    run_id: str
    actual_cost: float
    median_cost: float
    multiplier: float
    anomalous_tests: list[tuple[str, float, float]]  # (test_id, actual, median)

def cost_alerts(conn, run_id: str, *, multiplier: float = 2.0, window: int = 10) -> CostAlert | None: ...
```

### Surfaces

**`bi-evals run` output** — after ingest, if the just-completed run triggers an alert, print a warning:

```
⚠  This run cost $2.47, 3.2× the median ($0.78) of the last 10 runs.
   Anomalous tests:
   - daily-cases-filtered: $0.42 vs median $0.11 (3.8×)
   - cases-vs-mobility:    $0.38 vs median $0.14 (2.7×)
```

**Report** — a "Cost" section (collapsed by default if nothing anomalous) shows the alert inline.

**CLI query** — new command `bi-evals cost` lists runs with anomalies:

```bash
$ bi-evals cost --last-n 20
RUN                         COST    MEDIAN   MULT   TESTS_FLAGGED
eval-11c-...22:19           $2.47   $0.78    3.2×   2
eval-xJa-...09:03           $1.14   $0.78    1.5×   0  (below threshold)
...
```

### What cost alerts do NOT do

- Don't pre-empt expensive runs. The money's already spent by the time the alert fires.
- Don't auto-kill runaway runs. Would require a streaming integration with Promptfoo we don't have.
- Don't replace explicit budgets for teams with real cost ceilings. That's a later phase.

---

## File changes

| File | Action | Purpose |
|---|---|---|
| `src/bi_evals/config.py` | Modify | `scoring.stale_after_days`, `storage.cost_alert_*` |
| `src/bi_evals/golden/model.py` | Modify | `last_verified_at: date \| None = None` on `GoldenTest` |
| `src/bi_evals/store/schema.py` | Modify | `runs.prompt_snapshot`, `test_results.last_verified_at` columns |
| `src/bi_evals/store/ingest.py` | Modify | Hash files → `prompt_snapshot`; snapshot `last_verified_at` |
| `src/bi_evals/store/queries.py` | Modify | `prompt_diff()`, `cost_alerts()` |
| `src/bi_evals/runner/executor.py` | Modify | Staleness warning header before Promptfoo starts |
| `src/bi_evals/cli.py` | Modify | `cost` command; post-ingest cost alert output in `run` |
| `src/bi_evals/report/builder.py` | Modify | Freshness section, cost-alert inclusion |
| `src/bi_evals/compare/builder.py` | Modify | Prompt changes section, regression annotations |
| `src/bi_evals/report/templates/report.html.j2` | Modify | Freshness block |
| `src/bi_evals/report/templates/compare.html.j2` | Modify | Prompt diff block |
| `tests/test_prompt_drift.py` | New | Snapshot, hashing, diff helper, compare annotations |
| `tests/test_staleness.py` | New | Date math, warning triggers, report section |
| `tests/test_cost_alerts.py` | New | Median calc, threshold, CLI output |

---

## Migration

All three features are additive. Schema changes are `ALTER TABLE ... ADD COLUMN` (nullable), so existing DuckDBs from 6a work unchanged — they just have `NULL` for `prompt_snapshot` and `last_verified_at` on historical runs.

No backfill required. Users who want historical drift detection can re-ingest old eval JSONs (`bi-evals ingest <path>`), which will snapshot file hashes as they exist on disk *now* — useful going forward but won't reconstruct historical hashes.

---

## Risks / Gotchas

- **File path resolution.** Skill paths in traces are absolute paths produced during the run. If a user moves their project directory between runs, paths differ and every file looks "removed + added" even though contents are unchanged. Mitigate by hashing content-only and matching on filename relative to the project root. Store relative paths in `prompt_snapshot`, not absolute.
- **Staleness thresholds feel arbitrary.** 180 days is a guess. Expose the threshold clearly in config and docs; revisit once we have real usage data.
- **Cost median is unstable with few runs.** For projects with < 10 runs, median of the available runs; for < 3, skip the alert. Document this.
- **Cost anomaly false positives.** A legitimate repeats=5 run will trigger the alert vs. a baseline of repeats=1 runs. Users will notice and understand; worth mentioning in docs.
- **Hashing cost.** Reading and hashing every skill file adds ingest latency. For 10 skill files × 50KB each, negligible. For someone with hundreds of files, could add seconds. Cap hashing at 50 files with a warning if over.
- **Staleness snapshot at ingest time locks in the old value.** If a golden was stale at run time and someone later updates `last_verified_at`, old runs still show stale. This is the correct behavior (history should reflect what was true then) but worth documenting.

---

## Verification

```bash
# Unit tests
uv run python -m pytest tests/test_prompt_drift.py tests/test_staleness.py tests/test_cost_alerts.py -v

# Full suite regressions
uv run python -m pytest tests/ -m "not integration" -v

# Manual: drift detection
# (edit a skill file between two runs)
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases
# edit skills/covid-reporting/knowledge/CASE_TRACKING.md
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases
uv run bi-evals --config tmp/my-evals/bi-evals.yaml compare prev latest
# Expect: compare HTML shows "CASE_TRACKING.md modified" in Prompt changes section

# Manual: staleness
# (set last_verified_at: 2025-01-01 on a golden)
uv run bi-evals --config tmp/my-evals/bi-evals.yaml run --filter cases
# Expect: warning banner listing stale goldens before Promptfoo starts
# Expect: Freshness section in report shows counts

# Manual: cost alert
uv run bi-evals --config tmp/my-evals/bi-evals.yaml cost --last-n 20
# Expect: table of runs with multiplier; flagged runs highlighted
```

Success criteria:
- Drift detection surfaces every file change between two real runs in `tmp/my-evals/`
- Regressed tests in compare view get annotated with which of their files changed
- Staleness warnings fire at the right threshold, don't fire on fresh goldens
- Cost alerts catch an intentional runaway run (high `max_rounds` or repeats) without false-positive on normal variation
- All three features are opt-in / thresholds are config-driven — no forced behavior changes for existing projects
