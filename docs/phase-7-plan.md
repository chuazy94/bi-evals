# Phase 7: Polish the COVID-19 Example Project

## Context

A working example exists under `tmp/my-evals/`:
- Snowflake-backed (COVID-19 public dataset via Snowflake Marketplace)
- 5 goldens across 3 categories (`cases`, `joins`, `us-states`)
- 5 skill/knowledge files under `skills/covid-reporting/`
- 17 historical eval runs (~9.9MB) with traces, reports, and DuckDB history

The example is functional but lives in `tmp/` — a scratch directory that:
- Isn't shipped with the repo (`tmp/` is gitignored)
- Has local Snowflake credentials wired in as env vars (fine for dev, but no public contributor can run it)
- Carries 17 accumulated result files plus reports (noise for a first-time reader)
- Has uneven golden coverage — 5 tests across 3 categories isn't enough to showcase the framework's breadth

Phase 7 promotes it to a first-class `examples/covid-19/` directory that a new contributor can clone and run end-to-end, and that demonstrates the framework meaningfully (more goldens, at least one seeded regression, a README walkthrough).

This is deliberately a **polish phase**, not a feature phase. No new code in `src/bi_evals/`. Success is a newcomer running `bi-evals run` in `examples/covid-19/` on a fresh clone and seeing the full flow work.

---

## Goals

1. **Turn the existing scratch project into a documented, shipped example.**
2. **Demonstrate the framework's breadth** with better golden coverage (8–10 tests across more categories).
3. **Keep a known-regression run in history** so the `bi-evals compare` walkthrough has something to show.
4. **Lower the barrier to running the example** — minimal setup instructions, clear credential requirements, no hand-editing needed.

---

## Non-goals

- New framework features (those live in Phases 6 and 8)
- Postgres/BigQuery database support (Snowflake only, matches current MVP scope)
- Automated Snowflake dataset seeding — users bring their own marketplace share
- A second example project (keep focus; add more later if demand materializes)

---

## Work items

### 1. Move `tmp/my-evals/` → `examples/covid-19/`

Target layout:

```
examples/
  covid-19/
    README.md              # Walkthrough (new)
    .env.example           # Required env vars with placeholders (new)
    .gitignore             # Ignore .env, local duckdb, generated reports
    bi-evals.yaml          # Cleaned config
    system-prompt.md       # Existing
    skills/
      covid-reporting/
        SKILL.md
        knowledge/
          CASE_TRACKING.md
          MOBILITY_DATA.md
          TESTING_DATA.md
          US_STATE_DATA.md
    golden/
      cases/
      joins/
      us-states/
      time-series/         # NEW category
      aggregates/          # NEW category
    results/
      eval_<ts>_baseline.json     # ~3 representative runs (see below)
      eval_<ts>_regressed.json
      eval_<ts>_fixed.json
      traces/
        (corresponding traces only)
    reports/
      .gitkeep             # empty; filled by bi-evals run
```

Keep `tmp/my-evals/` intact for Zhi's personal dev use. Phase 7 **copies** to `examples/`, doesn't move-and-delete.

### 2. Credential hygiene

- **`bi-evals.yaml`**: already uses `${SNOWFLAKE_*}` substitution. No code changes needed — just verify.
- **`.env.example`** (new):
  ```
  SNOWFLAKE_ACCOUNT=xy12345.us-east-1
  SNOWFLAKE_USER=your_user
  SNOWFLAKE_PRIVATE_KEY_PATH=/absolute/path/to/rsa_key.p8
  SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=your_passphrase
  SNOWFLAKE_WAREHOUSE=COMPUTE_WH
  SNOWFLAKE_DATABASE=COVID19_EPIDEMIOLOGICAL_DATA
  SNOWFLAKE_SCHEMA=PUBLIC
  ANTHROPIC_API_KEY=sk-ant-...
  ```
- **`.gitignore`** (new): `.env`, `results/bi-evals.duckdb`, `reports/*.html`
- Verify no hardcoded creds leak into any skill/knowledge files (spot check).

### 3. Trim and curate results history

Current `results/` has 17 runs with accumulated noise. Keep 3 that tell a clear story:

- **Baseline** (all passing) — demonstrates a healthy run
- **Regressed** (1+ critical-dim regression vs baseline) — enables the `bi-evals compare` walkthrough
- **Fixed** (regression resolved) — demonstrates the full lifecycle

Pick from existing files (prefer recent runs that already show this pattern). Rename with suffixes to make the story obvious: `eval_baseline.json`, `eval_regressed.json`, `eval_fixed.json`. Also trim `results/traces/` to only trace files referenced by these three runs.

Re-ingest into a fresh `results/bi-evals.duckdb` so the ingested DB matches what's on disk. Don't ship the `.duckdb` file — `bi-evals ingest` or a fresh `bi-evals run` produces it.

### 4. Fill golden coverage gaps

Target 8–10 goldens across 5 categories. Current state: 5 goldens / 3 categories.

Existing (keep):
- `cases/total-cases-by-country.yaml`
- `cases/daily-cases-filtered.yaml`
- `joins/us-test-positivity.yaml`
- `joins/cases-vs-mobility.yaml`
- `us-states/state-level-deaths.yaml`

Add (propose):
- **`time-series/weekly-cases-rolling-avg.yaml`** — 7-day rolling average; exercises window functions
- **`time-series/cases-peak-date.yaml`** — find the date of peak cases per country; exercises argmax patterns
- **`aggregates/top-10-countries-by-deaths.yaml`** — ranked aggregation with ORDER BY + LIMIT
- **`aggregates/mobility-categories-summary.yaml`** — multi-column GROUP BY; exercises MOBILITY_DATA knowledge
- **`us-states/state-testing-trends.yaml`** — state-level test positivity over time; combines TESTING + US_STATE knowledge

Each new golden: reference SQL verified against the actual Snowflake marketplace COVID dataset, row_comparison enabled where deterministic, expected skill-path documented.

Mix difficulties so the suite isn't all trivial: include at least two "hard" (multi-table joins + aggregation), four "medium", a couple "easy" smoke tests.

### 5. Write `examples/covid-19/README.md`

Structure:

```markdown
# COVID-19 BI Evals Example

A complete, working bi-evals project demonstrating the framework against
the public COVID-19 Epidemiology dataset on Snowflake Marketplace.

## Prerequisites
- Python 3.11+, uv installed
- Node.js + npm (for Promptfoo)
- Anthropic API key
- Snowflake account with access to COVID19_EPIDEMIOLOGICAL_DATA marketplace share

## One-time setup
1. Get the Snowflake marketplace share (link)
2. Create Snowflake key-pair auth (link to Snowflake docs)
3. Copy env: `cp .env.example .env`; fill in values
4. Install: `uv sync`

## Run your first eval
```bash
cd examples/covid-19
bi-evals run
bi-evals report
open reports/report_*.html
```

## Run the regression-compare walkthrough
We've included three pre-ingested runs telling a story:
- `eval_baseline` — all passing
- `eval_regressed` — one regression (row_completeness on daily-cases-filtered)
- `eval_fixed` — regression resolved

```bash
bi-evals ingest results/eval_baseline.json
bi-evals ingest results/eval_regressed.json
bi-evals compare prev latest
open reports/compare_*.html
```

## Project structure
[brief tour]

## Adding your own goldens
[link to docs/golden-tests-guide.md]

## Troubleshooting
- `Snowflake connection failed` → [common fixes]
- `Promptfoo not found` → `npm install -g promptfoo`
- `DuckDB locked` → close any open `duckdb` CLI sessions
```

Keep it under ~150 lines. Link to the main repo README and `docs/` for deep detail; this is an onramp, not the reference.

### 6. Update the top-level README

The repo's `README.md` should link to `examples/covid-19/` as the canonical example. One-paragraph addition: "To see bi-evals in action, see `examples/covid-19/`." Don't rewrite the whole thing.

### 7. Verification on a fresh clone

After the files are in place, verify on a clean checkout (worktree or fresh clone):
- `cd examples/covid-19 && bi-evals run` succeeds end-to-end (requires real credentials — document this caveat)
- `bi-evals report` and `bi-evals compare prev latest` both produce HTML output
- Ingest of the 3 shipped eval JSONs works without errors
- All shipped goldens execute successfully against Snowflake

### 8. Update STATUS.md

Mark Phase 7 complete once merged. Move the "polish example" item from Remaining to Completed.

---

## File changes

| Path | Action | Notes |
|---|---|---|
| `examples/covid-19/README.md` | New | Walkthrough |
| `examples/covid-19/.env.example` | New | Env var placeholders |
| `examples/covid-19/.gitignore` | New | Ignore .env, .duckdb, reports |
| `examples/covid-19/bi-evals.yaml` | Copy+verify | From tmp/my-evals/ |
| `examples/covid-19/system-prompt.md` | Copy | From tmp/my-evals/ |
| `examples/covid-19/skills/covid-reporting/**` | Copy | From tmp/my-evals/ |
| `examples/covid-19/golden/cases/*.yaml` | Copy | 2 existing |
| `examples/covid-19/golden/joins/*.yaml` | Copy | 2 existing |
| `examples/covid-19/golden/us-states/state-level-deaths.yaml` | Copy | 1 existing |
| `examples/covid-19/golden/us-states/state-testing-trends.yaml` | New | |
| `examples/covid-19/golden/time-series/weekly-cases-rolling-avg.yaml` | New | |
| `examples/covid-19/golden/time-series/cases-peak-date.yaml` | New | |
| `examples/covid-19/golden/aggregates/top-10-countries-by-deaths.yaml` | New | |
| `examples/covid-19/golden/aggregates/mobility-categories-summary.yaml` | New | |
| `examples/covid-19/results/eval_baseline.json` | Curated copy | From tmp/my-evals/results/ |
| `examples/covid-19/results/eval_regressed.json` | Curated copy | |
| `examples/covid-19/results/eval_fixed.json` | Curated copy | |
| `examples/covid-19/results/traces/*.json` | Curated copy | Only those referenced |
| `examples/covid-19/reports/.gitkeep` | New | Keep empty dir |
| `README.md` | Modify | Add link to example |
| `STATUS.md` | Modify | Mark Phase 7 complete |

---

## Risks / Gotchas

- **Snowflake marketplace access** is a real prerequisite — many contributors won't have it. Document clearly; don't pretend the example is zero-setup.
- **Reference SQL drift** — the COVID marketplace dataset may be updated; golden reference SQL must be re-verified before shipping. Run every golden once against live Snowflake to confirm.
- **Regression seeding** — finding (or producing) a run with a clean, understandable regression takes care. Worst case: intentionally perturb a skill file to produce a regression, then revert.
- **Bundle size** — 17 result JSONs + traces + reports is ~10MB today. Trimmed to 3 runs it should be well under 2MB. Don't accidentally commit the full history.
- **Credential leakage** — one stray `grep -r` of `account:` or real API keys before committing. Add a pre-push safety check to the verification step.
- **`.duckdb` file** — easy to forget in `.gitignore`. Confirm it's not committed.

---

## Verification

```bash
# Structure check
ls examples/covid-19/
find examples/covid-19/golden -name "*.yaml" | wc -l  # Expect 8-10
find examples/covid-19/results -name "*.json" | wc -l # Expect 3 (+ traces)

# No credential leaks
grep -rE "(sk-ant|p8|password|\.snowflakecomputing\.com)" examples/covid-19/ \
    --include="*.yaml" --include="*.md" --include="*.json"
# Expect: no matches (only .env.example placeholders)

# Fresh-clone simulation
git worktree add /tmp/bi-evals-fresh main
cd /tmp/bi-evals-fresh/examples/covid-19
cp .env.example .env  # then fill in creds
uv run bi-evals run --filter cases
# Expect: promptfooconfig generated, cases tests run, auto-ingest succeeds

uv run bi-evals ingest results/eval_baseline.json
uv run bi-evals ingest results/eval_regressed.json
uv run bi-evals compare prev latest
# Expect: compare_*.html with red verdict

# Cleanup
cd - && git worktree remove /tmp/bi-evals-fresh
```

Success criteria:
- A developer with Anthropic + Snowflake credentials can go from `git clone` to a passing `bi-evals run` in under 10 minutes of setup
- The shipped `results/` tells the baseline → regression → fixed story on first `bi-evals compare`
- 8–10 goldens covering 5 categories run cleanly
- No leaked credentials, no stale `.duckdb`, no orphan traces
- STATUS.md reflects Phase 7 complete
