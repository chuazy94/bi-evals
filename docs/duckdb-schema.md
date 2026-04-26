# DuckDB Schema Reference

bi-evals persists every eval run to a local DuckDB file so that reports, regression compares, and (eventually) a UI can query history without re-parsing JSON. This document describes the tables, how they relate, and every code path that writes to them.

**DB path** — default `results/bi-evals.duckdb`, configurable via `storage.db_path` in `bi-evals.yaml`. Path resolves relative to the config file.

**Schema module** — `src/bi_evals/store/schema.py` (DDL constants + `ensure_schema()`). Called automatically by `connect()` on every open — tables and indexes use `CREATE ... IF NOT EXISTS` so the call is idempotent.

---

## Tables

Four tables. Phase 6a introduced per-trial data, so PKs widened to include `model` and `trial_ix`:

- `runs` — one row per eval run
- `test_results` — one row per `(run_id, test_id, model)` — aggregate over trials
- `trial_results` — one row per `(run_id, test_id, model, trial_ix)` — atomic observation
- `dimension_results` — one row per `(run_id, test_id, model, trial_ix, dimension)`

All rows for a single run are written in one transaction; re-ingesting the same run deletes-then-inserts so the DB always reflects the latest JSON on disk.

### `runs` — one row per eval run

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR (PK) | Promptfoo `evalId`, e.g. `eval-11c-2026-04-19T22:19:05` |
| `project_name` | VARCHAR | From `config.project.name` at ingest time |
| `timestamp` | TIMESTAMP | Wall-clock time Promptfoo wrote the result (from `results.timestamp`) |
| `config_snapshot` | JSON | Full `BiEvalsConfig` model dump at ingest time — freezes scoring weights, critical dims, paths |
| `promptfoo_config` | JSON | The `config` block Promptfoo serialized into the eval JSON |
| `eval_json_path` | VARCHAR | Absolute path to the source `eval_<ts>.json` on disk |
| `test_count` | INTEGER | Total tests in the run |
| `pass_count` | INTEGER | From `stats.successes`, falls back to per-test count |
| `fail_count` | INTEGER | From `stats.failures`, falls back to per-test count |
| `error_count` | INTEGER | From `stats.errors` |
| `total_cost_usd` | DOUBLE | Prefers `results.prompts[0].metrics.cost`, falls back to per-test sum |
| `total_latency_ms` | BIGINT | Same preference order as cost |
| `total_prompt_tokens` | BIGINT | From `prompts[0].metrics.tokenUsage.prompt` |
| `total_completion_tokens` | BIGINT | From `prompts[0].metrics.tokenUsage.completion` |
| `prompt_snapshot` | JSON | Phase 6b — sha256 + size + mtime per skill/knowledge file the agent read this run, keyed by path relative to the project base. Powers prompt-drift detection in `bi-evals compare`. |
| `ingested_at` | TIMESTAMP | Auto-set by DuckDB on INSERT |

### `test_results` — one row per (run, test, model)

Per-trial detail lives in `trial_results`; this table holds the per-(run,test,model) aggregate. When `scoring.repeats == 1` the aggregate columns trivially mirror the single trial.

Golden metadata is snapshotted here at ingest time. Editing a golden YAML later never rewrites history — this is deliberate, so regression compares stay valid across golden edits.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR (PK) | FK to `runs.run_id` |
| `test_id` | VARCHAR (PK) | The golden file's relative path (stable across runs); primary join key |
| `model` | VARCHAR (PK) | e.g. `claude-sonnet-4-6`. Empty string `''` for legacy pre-6a rows. |
| `golden_id` | VARCHAR | `id` field from the golden YAML (may differ from filename) |
| `category` | VARCHAR | Snapshotted from golden YAML |
| `difficulty` | VARCHAR | Snapshotted from golden YAML |
| `tags` | JSON | List of strings, snapshotted from golden YAML |
| `question` | TEXT | From test case `vars.question` |
| `description` | TEXT | From test case `description` |
| `reference_sql` | TEXT | Snapshotted from golden YAML (the "correct" SQL) |
| `generated_sql` | TEXT | What the agent produced (from a representative trial — typically the last one) |
| `files_read` | JSON | List of skill/knowledge files the agent read |
| `trace_file_path` | VARCHAR | Absolute path to the per-test trace file (representative trial) |
| `trace_json` | JSON | Inlined trace content (null if missing; 1MB guardrail truncates oversized traces) |
| `passed` | BOOLEAN | Overall pass/fail (the aggregate verdict — typically `pass_count >= ceil(trial_count * threshold)`) |
| `score` | DOUBLE | Weighted composite score from the 9 dimensions (representative trial) |
| `fail_reason` | TEXT | Error message or grading reason if failed |
| `cost_usd` | DOUBLE | Sum across trials |
| `latency_ms` | BIGINT | Sum across trials |
| `prompt_tokens` | BIGINT | Sum across trials |
| `completion_tokens` | BIGINT | Sum across trials |
| `total_tokens` | BIGINT | Sum across trials |
| `provider` | VARCHAR | e.g. `anthropic_tool_loop` |
| `trial_count` | INTEGER | Phase 6a — number of trials executed (default 1) |
| `pass_count` | INTEGER | Phase 6a — number of trials that passed |
| `pass_rate` | DOUBLE | `pass_count / trial_count` |
| `score_mean` | DOUBLE | Mean of per-trial scores |
| `score_stddev` | DOUBLE | Population stddev of per-trial scores |
| `last_verified_at` | DATE | Phase 6b — snapshotted from golden YAML; powers staleness warnings |

### `trial_results` — one row per (run, test, model, trial_ix)

Phase 6a. The atomic observation. Without this, repeat-run variance metrics (stddev, flake rate) couldn't be reconstructed from `test_results` alone.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR (PK) | |
| `test_id` | VARCHAR (PK) | |
| `model` | VARCHAR (PK) | |
| `trial_ix` | INTEGER (PK) | 0-based trial index within the (run, test, model) group |
| `passed` | BOOLEAN | This trial's verdict |
| `score` | DOUBLE | Weighted composite for this trial |
| `generated_sql` | TEXT | The SQL this specific trial produced |
| `fail_reason` | TEXT | If this trial failed, why |
| `cost_usd` | DOUBLE | Per-trial cost |
| `latency_ms` | BIGINT | Per-trial latency |
| `prompt_tokens` | BIGINT | |
| `completion_tokens` | BIGINT | |
| `total_tokens` | BIGINT | |
| `trace_file_path` | VARCHAR | This trial's trace file |
| `trace_json` | JSON | Inlined trace |

### `dimension_results` — one row per (run, test, model, trial_ix, dimension)

9 rows per trial (one per scoring dimension). Ingest unwraps Promptfoo's double-nested `gradingResult.componentResults[0].componentResults[]` — each inner entry is one dimension.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR (PK) | |
| `test_id` | VARCHAR (PK) | |
| `model` | VARCHAR (PK) | Phase 6a addition. Empty string `''` for legacy rows. |
| `trial_ix` | INTEGER (PK) | Phase 6a addition. `0` for legacy rows. |
| `dimension` | VARCHAR (PK) | One of: `execution`, `table_alignment`, `column_alignment`, `filter_correctness`, `row_completeness`, `row_precision`, `value_accuracy`, `no_hallucinated_columns`, `skill_path_correctness` |
| `passed` | BOOLEAN | Binary pass/fail for this dimension |
| `score` | DOUBLE | Always 0.0 or 1.0 today (dimensions are binary) |
| `reason` | TEXT | Human-readable explanation from the scorer |
| `is_critical` | BOOLEAN | True if this dim is in `config.scoring.critical_dimensions` — affects overall pass gating |
| `weight` | DOUBLE | From `config.scoring.dimension_weights`, defaults to 1.0 |

### Indexes

| Index | Target | Used by |
|---|---|---|
| `idx_tr_run` | `test_results(run_id)` | Fetch all tests for a run |
| `idx_tr_cat` | `test_results(run_id, category)` | Category aggregates in report |
| `idx_tr_passed` | `test_results(run_id, passed)` | Pass/fail filtering |
| `idx_tr_model` | `test_results(run_id, model)` | Model comparison queries |
| `idx_trial_run` | `trial_results(run_id)` | Fetch all trials for a run |
| `idx_trial_tm` | `trial_results(run_id, test_id, model)` | Per-(test, model) trial fan-out |
| `idx_dr_run` | `dimension_results(run_id)` | Fetch all dims for a run |
| `idx_dr_dim` | `dimension_results(run_id, dimension)` | Dimension pass-rate report query |
| `idx_dr_fail` | `dimension_results(run_id, dimension, passed)` | Finding which dims flipped in compare |

---

## What writes to these tables

Exactly one function writes: **`ingest_run()`** in `src/bi_evals/store/ingest.py`. Every code path that produces DB rows goes through it. Each call is a single transaction that DELETE-then-INSERTs all three tables for the given `run_id`.

There are **three entry points** that invoke `ingest_run`:

### 1. `bi-evals run` (auto-ingest)

The primary path. When the `run` command's `run_promptfoo()` exits successfully, the CLI calls `ingest_run(conn, results_output, config)` if `config.storage.auto_ingest` is true (the default).

- **Trigger**: Every successful `bi-evals run`
- **Source file**: `src/bi_evals/cli.py` (`run` command body)
- **Failure mode**: Warning on stderr; the run is *not* marked failed because the JSON file is still on disk and can be re-ingested manually
- **Disable**: `storage.auto_ingest: false` in `bi-evals.yaml`

### 2. `bi-evals ingest <eval_json_path>`

Manual/backfill path. Ingests any existing `eval_<ts>.json` file into the DB.

- **Trigger**: Explicit user command
- **Source file**: `src/bi_evals/cli.py` (`ingest` command body)
- **Use cases**:
  - Backfilling runs that predate Phase 5 (no auto-ingest)
  - Re-ingesting after a schema change
  - Importing a JSON produced outside `bi-evals run` (e.g. another machine)
- **`--force` flag**: Documented but no-op — ingest is already idempotent

### 3. Tests / programmatic use

The tests (e.g. `tests/test_store_ingest.py`) call `ingest_run()` directly against fixture JSONs. External tooling (a UI, a notebook, a one-off script) can do the same:

```python
from bi_evals.store import connect
from bi_evals.store.ingest import ingest_run
from bi_evals.config import BiEvalsConfig

config = BiEvalsConfig.load("bi-evals.yaml")
with connect(config.resolve_path(config.storage.db_path)) as conn:
    run_id = ingest_run(conn, "results/eval_20260420_120000.json", config)
```

---

## Idempotency and ordering

**Idempotency** — `ingest_run` begins with:

```sql
DELETE FROM dimension_results WHERE run_id = ?;
DELETE FROM test_results      WHERE run_id = ?;
DELETE FROM runs              WHERE run_id = ?;
```

…then re-INSERTs all rows. Re-running on the same JSON always produces the same final DB state. This means:
- Re-ingesting the same file is safe
- Editing an eval JSON by hand and re-ingesting will overwrite cleanly
- But: editing a *golden YAML* then re-ingesting an old run **does** overwrite the snapshotted `reference_sql`/`category`/etc. Don't do this if you care about preserving historical metadata — the snapshotting only protects you from unintentional drift, not intentional re-ingest.

**Transaction ordering** — all three tables are written inside one `BEGIN` ... `COMMIT`. Partial ingests never appear; a crash mid-ingest rolls back cleanly via the `except` branch.

**Single-writer constraint** — DuckDB is a single-writer store. Two `bi-evals run` invocations targeting the same DB will serialize; `store/client.py` retries 3× at 200ms on `IOException` before surfacing the lock error. Acceptable for local CLI use; revisit when/if moving to Postgres for multi-user.

---

## What *doesn't* get written

The following are intentionally **not** persisted to DuckDB:
- **Individual tool calls** beyond what's inside `trace_json`. If you need to query tool invocations, parse `trace_json` in SQL (`SELECT trace_json->>'$.trace[0].tool_name'`).
- **Skill/knowledge file contents.** Only `files_read` (the list of paths) and `runs.prompt_snapshot` (sha256 + size + mtime per file) are stored — enough to detect drift, not enough to reconstruct prompts.
- **Evaluator decisions separate from dimensions.** The scorer's intermediate state (SQL parsing, row comparison details) isn't stored — only the final pass/fail/reason per dimension.

---

## Querying the DB

Three equivalent ways:

**DuckDB CLI** (`brew install duckdb`):
```bash
duckdb results/bi-evals.duckdb
```
```sql
SELECT run_id, timestamp, pass_count, fail_count, total_cost_usd
FROM runs ORDER BY timestamp DESC LIMIT 10;
```

**Python ad-hoc**:
```python
from bi_evals.store import connect
with connect("results/bi-evals.duckdb") as conn:
    rows = conn.execute("SELECT * FROM runs LIMIT 5").fetchall()
```

**Query helpers** (stable API, used by `report` and `compare`):
```python
from bi_evals.store import connect
from bi_evals.store import queries as q
with connect("results/bi-evals.duckdb") as conn:
    latest = q.latest_run_id(conn)
    categories = q.aggregate_by_category(conn, latest)
    weakest_dims = q.dimension_pass_rates(conn, latest)
    diff = q.test_diff(conn, q.previous_run_id(conn), latest)
```

All helpers return frozen dataclasses; see `src/bi_evals/store/queries.py` for the full list.

---

## Schema evolution

`ensure_schema()` runs on every `connect()`. It executes in this order:

1. **`SCHEMA_SQL`** — `CREATE TABLE IF NOT EXISTS` for all four current tables. New tables added in future phases appear on the first `connect()` after upgrading.
2. **`_migrate_legacy()`** — adds 6a/6b columns to pre-existing tables via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. DuckDB's `ALTER` does not accept inline `NOT NULL DEFAULT`, so columns are added nullable and the next step backfills them.
3. **`_backfill_aggregates()`** — fills nullable 6a columns for legacy rows (`trial_count = 1`, `pass_count = passed ? 1 : 0`, `model = ''`, `trial_ix = 0`, etc.).
4. **`_rebuild_legacy_pks()`** — pre-6a primary keys were narrower (`(run_id, test_id)` and `(run_id, test_id, dimension)`). DuckDB cannot drop or alter a `PRIMARY KEY` in place, so this step detects the legacy PK constraint name, copies rows into a fresh table with the correct wider PK, drops the old table, and renames. Without this step, ingesting a multi-trial or multi-model run on a pre-6a DB would hit a duplicate-key error on `dimension_results`.
5. **`_INDEXES_SQL`** — index creation runs *last*, after the legacy column adds, because indexes reference columns (e.g. `category`) that pre-6a tables already have but that the index DDL would otherwise see before the migration ran. (Order doesn't matter on fresh DBs; it matters on legacy ones.)

### Past migrations (applied automatically on open)

- **Phase 6a — repeat-run support.** Added `model`, `trial_ix` (where applicable), `trial_count`, `pass_count`, `pass_rate`, `score_mean`, `score_stddev`. Created `trial_results`. Widened `test_results` PK to `(run_id, test_id, model)` and `dimension_results` PK to `(run_id, test_id, model, trial_ix, dimension)`.
- **Phase 6b — drift, staleness, cost alerts.** Added `runs.prompt_snapshot` and `test_results.last_verified_at`.

### Adding a column going forward

1. Add it to `SCHEMA_SQL` (so fresh DBs get it).
2. Append it to `_LEGACY_MIGRATIONS` in `schema.py` (so existing DBs get it via `ALTER TABLE ADD COLUMN IF NOT EXISTS`).
3. If the column has a non-NULL default, also add a backfill `UPDATE` to `_backfill_aggregates()` — DuckDB `ALTER` won't accept inline defaults.
4. If the change widens a primary key, add a rebuild branch to `_rebuild_legacy_pks()` keyed on the old constraint name.
