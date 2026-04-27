# bi-evals Feature Summary

A consolidated reference for everything `bi-evals` can do today, organized by what you're trying to accomplish. Every feature includes the command(s) to invoke it.

For golden-test authoring, see [golden-tests-guide.md](./golden-tests-guide.md). For storage internals, see [duckdb-schema.md](./duckdb-schema.md).

---

## Table of contents

1. [CLI commands at a glance](#cli-commands-at-a-glance)
2. [Setting up a project](#setting-up-a-project)
3. [Running evals](#running-evals)
4. [Reporting and history](#reporting-and-history)
5. [Comparing runs (regression detection)](#comparing-runs-regression-detection)
6. [Operational signals](#operational-signals)
7. [Quality signals (scoring dimensions)](#quality-signals-scoring-dimensions)
8. [Storage and replay](#storage-and-replay)

---

## CLI commands at a glance

| Command | Purpose |
|---|---|
| `bi-evals init` | Scaffold a new eval project |
| `bi-evals run` | Run the eval suite (Promptfoo + auto-ingest) |
| `bi-evals view` | Open the Promptfoo web UI for per-test deep-dive |
| `bi-evals ingest <eval.json>` | Backfill an old `eval_*.json` into DuckDB |
| `bi-evals report` | Generate single-run HTML report |
| `bi-evals compare A B` | Generate run-vs-run regression report |
| `bi-evals cost` | List recent runs with cost-vs-median multiplier |
| `bi-evals flakiness` | List tests that flip pass/fail across runs |

All commands accept `-c <config>` to point at a non-default `bi-evals.yaml`.

---

## Setting up a project

### Scaffold a new project

```bash
uv run bi-evals init --dir /tmp/my-evals
cd /tmp/my-evals
```

This creates `bi-evals.yaml`, `.env`, `.env.example`, `golden/`, `results/`, `reports/`, and an example golden test.

### Configure your agent and goldens

Edit `bi-evals.yaml` — see [golden-tests-guide.md](./golden-tests-guide.md) for the full schema. At minimum, set:
- `agent.system_prompt` — path to your system prompt
- `agent.tools[].config.base_dir` — path to your skill/knowledge files
- `database.connection.*` — Snowflake credentials (via `${ENV_VAR}` substitution)

Set credentials in `.env` next to `bi-evals.yaml` — they're loaded automatically (`override=False`, so shell vars win).

---

## Running evals

### Single run, single model

```bash
uv run bi-evals run
```

After Promptfoo finishes, results auto-ingest into `results/bi-evals.duckdb` and the next-step report command is printed.

### Run a subset

```bash
# Filter by id, category, or tag (substring match)
uv run bi-evals run --filter revenue
uv run bi-evals run --filter rev-001
uv run bi-evals run --filter enterprise
```

### Preview without executing

```bash
uv run bi-evals run --dry-run
```

Prints the generated `promptfooconfig.yaml`. Useful for debugging filter/test discovery.

### Multi-model evaluation (Phase 6a)

Run every golden against several models in one pass. In `bi-evals.yaml`:

```yaml
agent:
  type: "anthropic_tool_loop"
  models:
    - claude-sonnet-4-5-20250929
    - claude-opus-4-5-20251001
```

Then:

```bash
uv run bi-evals run
```

The CLI prints the trial multiplier (tests × models × repeats) before launching, with a confirmation prompt unless `--yes` is passed. Each trial is stored as its own row keyed by `(run_id, test_id, model, trial_ix)`.

### Repeat trials for variance (Phase 6a)

To detect non-determinism, run each golden N times:

```bash
# Override config for one run
uv run bi-evals run --repeats 3

# Or set permanently in bi-evals.yaml under scoring.repeats
```

The report shows per-test pass-rate (e.g. `2/3 trials passed`) instead of a binary pass/fail when `repeats > 1`.

### Skip the confirmation prompt

```bash
uv run bi-evals run --yes
```

By default, large multi-trial runs prompt before launching to avoid surprise API spend.

### Force fresh API calls

```bash
uv run bi-evals run --no-cache
```

Disables Promptfoo's provider cache.

---

## Reporting and history

### Generate the single-run HTML report

```bash
# Latest run
uv run bi-evals report

# Specific run
uv run bi-evals report --run-id eval-2026-04-25T14:32:01

# Custom output path
uv run bi-evals report --out /tmp/report.html
```

The report includes:
- Run header (timestamp, totals, cost, latency)
- Category dashboard (pass-rate per category)
- Weakest dimensions (sorted worst-first)
- Cost-by-model table
- Per-model summary + quality-vs-cost scatter (when multiple models)
- Outcome stability (flakiest tests across last 10 runs)
- Freshness (stale + unverified goldens)
- Cost alert banner (if this run exceeded the threshold)

### Open the Promptfoo web UI

```bash
uv run bi-evals view
```

Per-test deep-dive (full tool traces, raw model output) is best viewed in Promptfoo's own UI; the bi-evals report is a thin complement, not a replacement.

---

## Comparing runs (regression detection)

### Compare two runs

```bash
# By run-id
uv run bi-evals compare eval-2026-04-15T00:06:54 eval-2026-04-19T22:19:05

# Using the latest/prev shortcuts
uv run bi-evals compare prev latest
uv run bi-evals compare latest prev      # reverse direction
```

The compare HTML shows:
- **Verdict banner**: 🟢 (no regressions) / 🟡 (mixed) / 🔴 (regressions detected)
- **Bucket counts**: regressed / fixed / unchanged / added / removed
- **Transitions table**: tests whose pass-state or score changed, sorted regression-first
- **Category and dimension deltas**
- **Prompt drift** (Phase 6b): files added / removed / modified between the two runs' snapshots, plus per-test "files I read that changed" annotations

### Verdict semantics

- 🔴 **Red** — overall pass flipped T→F, or a critical dimension flipped pass→fail
- 🟡 **Amber** — no regressions but score drops, dim flips, or test set drift to review
- 🟢 **Green** — no regressions, no deltas

Added/removed tests never flip the verdict to red — they're shown in their own bucket.

---

## Operational signals

### Cost anomaly alerts (Phase 6b)

```bash
# View recent runs with cost vs prior median
uv run bi-evals cost

# Last 50 runs
uv run bi-evals cost --last-n 50
```

Configure in `bi-evals.yaml`:

```yaml
storage:
  cost_alert_multiplier: 2.0      # flag runs ≥ 2× the median
  cost_alert_window: 10           # window of prior runs for the median
```

When `bi-evals run` finishes, an alert prints if the run is anomalous, listing the top per-test offenders.

### Outcome stability / flakiness (Phase 6a)

```bash
# Top 20 flakiest tests over last 10 runs
uv run bi-evals flakiness

# Customize window
uv run bi-evals flakiness --last-n 20 --limit 50
```

Lists tests by `flip_count` (how often they cross the pass/fail boundary) with current streak (`+3 pass` / `-2 fail`).

The same data appears in the HTML report's "Outcome stability" section.

### Dataset staleness warnings (Phase 6b)

Set `last_verified_at` per golden (see [golden-tests-guide.md](./golden-tests-guide.md)). On every `bi-evals run`, a warning header lists goldens past `scoring.stale_after_days`:

```yaml
scoring:
  stale_after_days: 180     # 0 disables the check
```

Stale and unverified goldens also appear in the HTML report's freshness panel, with side-by-side pass-rates for fresh vs stale buckets.

### Knowledge-file staleness warnings (Phase 6d)

A complementary check on the *knowledge* files (skill / system prompt / lookup tables) the agent reads. A file is flagged when its mtime is older than `scoring.knowledge_stale_after_days` AND it appears in the most recent run's `prompt_snapshot` — so only files actually read get warned about.

```yaml
scoring:
  knowledge_stale_after_days: 90    # 0 disables the check
```

- **Pre-run warning** prints the same `⚠` header listing stale files (silent on first run; needs at least one ingested run as the read-set source).
- **HTML report** includes a "Knowledge freshness" card listing the worst offenders (path + mtime + days ago).

No scoring impact — purely a nudge to re-verify aging knowledge.

### Prompt drift detection (Phase 6b)

Whenever a run is ingested, the SHA256 of every file the agent read (resolved against each `file_reader` tool's `base_dir`) is snapshotted into `runs.prompt_snapshot`. The compare command diffs two snapshots and shows:

- Files added / removed / modified between the two runs
- Per-transition test, the subset of changed files that test actually read

This makes the question "what knowledge file changed since the last run" answerable from the report, not git-blame.

No flag needed — automatic for every run since Phase 6b.

---

## Quality signals (scoring dimensions)

Every test is scored on 10 binary dimensions. A test passes when all `critical_dimensions` pass AND the weighted score ≥ `pass_threshold`.

| Dimension | What it checks | Default critical | Default weight |
|---|---|---|---|
| `execution` | Generated SQL runs without error | ✅ | 3.0 |
| `row_completeness` | All reference rows appear in generated | ✅ | 3.0 |
| `value_accuracy` | Matched-row values agree within tolerance | ✅ | 3.0 |
| `row_precision` | No spurious extra rows in generated | | 2.0 |
| `column_alignment` | All `required_columns` present | | 2.0 |
| `table_alignment` | Generated SQL queries the reference's tables | | 1.0 |
| `filter_correctness` | WHERE-clause column/operator pairs match | | 1.0 |
| `no_hallucinated_columns` | All generated columns exist in source tables | | 1.0 |
| `skill_path_correctness` | `expected_skill_path` was followed | | 1.0 |
| `anti_pattern_compliance` | None of `forbidden_tables`/`forbidden_columns` used (Phase 6c) | | 2.0 |

Override any of these in `bi-evals.yaml`:

```yaml
scoring:
  dimensions: [...]                  # which dims to evaluate
  critical_dimensions: [...]         # gating dims
  dimension_weights: {execution: 5.0, ...}
  pass_threshold: 0.75
  thresholds:
    completeness: 0.95
    precision: 0.95
    value_tolerance: 0.0001
```

A dimension that has nothing to evaluate (e.g. `anti_pattern_compliance` when no golden defines `anti_patterns`) skips with `passed=true` and a `"skipped: ..."` reason. Vacuously-passing dimensions are dropped from the HTML report so they don't dilute the scorecard.

---

## Storage and replay

### Where data lives

```
results/
  eval_<timestamp>.json         ← Promptfoo output (source of truth, replayable)
  promptfooconfig_<ts>.yaml     ← exact config for that run
  traces/<test_id>.json         ← per-test agent traces
  bi-evals.duckdb               ← queryable store (auto-populated)
reports/
  report_<run_id>.html          ← bi-evals report output
  compare_<a>__vs__<b>.html     ← bi-evals compare output
```

JSON files are the replayable source of truth. DuckDB is the queryable view.

### Re-ingest an existing run

```bash
uv run bi-evals ingest results/eval_20260425_143201.json
```

Idempotent — re-ingesting overwrites cleanly. Useful after a schema migration or if the DB was deleted.

### Replay a run via Promptfoo's UI

```bash
uv run bi-evals view
```

Promptfoo reads the raw `eval_*.json` files directly, so the UI works even if DuckDB is wiped.

### Disable auto-ingest

```yaml
storage:
  auto_ingest: false
```

Useful for dry-run workflows where you don't want to clutter the history.

### Schema and direct queries

DuckDB is just a file — point any tool at it:

```bash
duckdb results/bi-evals.duckdb -c "SELECT run_id, test_count, pass_count FROM runs ORDER BY timestamp DESC LIMIT 5"
```

For the schema reference, see [duckdb-schema.md](./duckdb-schema.md).

---

## See also

- [golden-tests-guide.md](./golden-tests-guide.md) — authoring golden tests
- [duckdb-schema.md](./duckdb-schema.md) — storage schema reference
- [mvp-eval-platform.md](./mvp-eval-platform.md) — original design doc and roadmap
