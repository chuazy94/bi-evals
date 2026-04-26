# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is used as the test runner engine; all custom logic (provider, tools, scoring, storage, reporting) is Python. Phases 1–5 and Phase 6a are complete — the full pipeline from `bi-evals run` through DuckDB-backed reporting and regression compare is built, with repeat-run variance, multi-model evaluation, and cross-run stability tracking.

What works today:
- `bi-evals init` scaffolds a new eval project (config, directories, golden test stub)
- `bi-evals run` runs the full eval end-to-end (generates `promptfooconfig.yaml`, invokes Promptfoo, auto-ingests results into DuckDB)
- `bi-evals ingest <path>` backfills existing eval JSON into the store
- `bi-evals report [--run-id ID]` generates a self-contained HTML report (category dashboard, weakest dimensions, cost by model)
- `bi-evals compare <a> <b>` generates an HTML regression diff with tiered verdict (🟢/🟡/🔴); supports `latest` / `prev` shortcuts
- Config system loads `bi-evals.yaml` with env var substitution and path resolution
- `anthropic_tool_loop` provider runs the full multi-turn Claude tool-calling loop with trace capture, SQL extraction, and cost tracking
- `api_endpoint` provider calls external agent APIs with configurable response parsing
- `FileReaderTool` + `DescribeTableTool` serve skill files and DB schema to the agent
- `SnowflakeClient` executes SQL and returns structured results with error handling
- `GoldenTest` model loads expected results from YAML (reference SQL, required columns, skill path, row comparison config)
- Tiered/weighted 9-dimension scorer: critical dims (execution, row_completeness, value_accuracy) gate overall pass; remaining dims contribute a weighted score
- DuckDB store with idempotent ingest, 4-table schema (`runs`, `test_results`, `trial_results`, `dimension_results`), golden metadata snapshotted at ingest time
- `bi-evals run --repeats N` runs each golden N times; report shows pass rates with stddev
- Multi-model runs via `agent.models: [...]`; report includes per-model summary table and cost-vs-quality SVG scatter
- `bi-evals flakiness` lists tests ranked by cross-run flip count
- Rate-based regression compare with configurable `compare.regression_threshold` (default 0.2)
- 216 unit tests passing, 0 warnings

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`pyproject.toml`** — pip-installable package via uv, all deps declared (click, pydantic, pyyaml, anthropic, snowflake-connector-python, sqlglot, jinja2)
- **`src/bi_evals/config.py`** — Pydantic config model loading from `bi-evals.yaml`, `${ENV_VAR}` resolution, relative path resolution, validation
- **`src/bi_evals/cli.py`** — Click CLI with `bi-evals init` command that scaffolds eval infrastructure (config, golden test dir, results/reports dirs). Does NOT scaffold skill/knowledge files — users point to their own.
- **`.gitignore`**, **`.env.example`**
- **`tests/test_config.py`** — 11 tests covering config loading, env vars, defaults, validation

### Phase 2: Tools + Agent Loop + Provider

- **`src/bi_evals/tools/base.py`** — `Tool` protocol (name, definition, execute)
- **`src/bi_evals/tools/file_reader.py`** — `FileReaderTool` with path traversal protection
- **`src/bi_evals/tools/registry.py`** — factory building tools from config
- **`src/bi_evals/provider/cost.py`** — pricing map for Claude models (Sonnet, Opus, Haiku)
- **`src/bi_evals/provider/agent_loop.py`** — multi-turn Claude tool-calling loop with:
  - Full trace capture (every tool call, every text block, per-round timestamps)
  - SQL extraction (3 strategies: ```sql fence → generic fence → bare SELECT)
  - Token counting and cost calculation
  - Max rounds safety limit
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST provider for teams with existing agent APIs:
  - Configurable response keys (dot-notation for nested JSON)
  - Custom headers (auth tokens)
  - Optional trace/files_read capture if the API returns them
  - Fallback SQL extraction from text
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()` entry point that dispatches based on `agent.type`:
  - `anthropic_tool_loop` — runs Claude with skill files
  - `api_endpoint` — calls external agent API
  - Both write trace JSON to `results/traces/` for the scorer
- **`tests/test_agent_loop.py`** — 24 tests (SQL extraction, cost, trace, mocked agent loop, file reader)
- **`tests/test_api_endpoint.py`** — 11 tests (response parsing, nested keys, trace capture, error handling, real HTTP mock server)
- **`tests/test_demo_routing.py`** — demo test (requires live API)

### Phase 3: Database + Golden Tests + 9-Dimension Scorer

- **`src/bi_evals/db/client.py`** — `DatabaseClient` protocol + `QueryResult` dataclass
- **`src/bi_evals/db/snowflake.py`** — `SnowflakeClient` (executes SQL, catches errors, uppercases column names)
- **`src/bi_evals/db/factory.py`** — `create_db_client()` factory
- **`src/bi_evals/golden/model.py`** — `GoldenTest` Pydantic model + nested types (`ExpectedSkillPath`, `SkillStep`, `ValueCheck`, `RowComparison`, `ExpectedResults`)
- **`src/bi_evals/golden/loader.py`** — `load_golden_test()` and `load_golden_tests()` YAML loaders
- **`src/bi_evals/scorer/sql_utils.py`** — sqlglot helpers (`extract_tables`, `extract_filter_columns`)
- **`src/bi_evals/scorer/dimensions.py`** — 9 dimension evaluator functions + `DimensionResult` dataclass
- **`src/bi_evals/scorer/entry.py`** — `get_assert()` Promptfoo scorer entry point (loads trace, golden test, executes SQL, runs dimensions, returns results)
- **`tests/test_db.py`** — 9 tests
- **`tests/test_golden.py`** — 7 tests
- **`tests/test_scorer.py`** — 39 tests
- **`tests/test_demo_scorer_phase_3.py`** — end-to-end demo (real API call + mock DB + all 9 dimensions)

### Phase 4: Promptfoo Bridge + `bi-evals run`

- **`src/bi_evals/runner/config_generator.py`** — translates `bi-evals.yaml` + goldens into `promptfooconfig.yaml`
- **`src/bi_evals/runner/executor.py`** — invokes `npx promptfoo eval`, streams output, returns path to results JSON
- **`src/bi_evals/cli.py`** — `bi-evals run` wired end-to-end (filters by category, writes `results/eval_<ts>.json` + `results/traces/*.json`)
- **`src/bi_evals/tools/describe_table.py`** — new tool that exposes DB schema to the agent
- Scorer refinements: tiered/weighted pass threshold, critical-dim gating

### Phase 5: Storage + Reporting + Regression Compare

- **`src/bi_evals/store/`** — DuckDB layer
  - `schema.py` — 3 tables (`runs`, `test_results`, `dimension_results`) + indexes
  - `client.py` — `connect(db_path)` context manager with retry on lock contention
  - `ingest.py` — idempotent ingest (DELETE-then-INSERT per run_id), snapshots golden metadata, inlines traces
  - `queries.py` — frozen-dataclass read helpers (`latest_run_id`, `get_run`, `aggregate_by_category`, `dimension_pass_rates`, `cost_by_model`, `test_diff`, …)
- **`src/bi_evals/compare/diff.py`** — pure regression classifier (buckets: regressed / fixed / unchanged / added / removed) + tiered verdict (🟢/🟡/🔴)
- **`src/bi_evals/report/`** — Jinja2 templates (`_base.html.j2`, `report.html.j2`, `compare.html.j2`) rendered via `builder.py`. Inline CSS, no external URLs.
- **`src/bi_evals/cli.py`** — `ingest`, `report`, `compare` commands; auto-ingest on successful `run`
- **`tests/fixtures/eval_sample/`** — real Promptfoo output with known regression
- **`tests/test_store_schema.py`** — 3 tests
- **`tests/test_store_ingest.py`** — 7 tests (3-table population, nested componentResult unwrap, golden snapshot, idempotent re-ingest, missing-trace tolerance)
- **`tests/test_store_queries.py`** — 8 tests
- **`tests/test_compare_diff.py`** — 13 tests (all bucket transitions, verdict red/amber/green, category + dimension deltas)
- **`tests/test_report_builder.py`** — 6 tests (content presence, self-contained HTML, red verdict for seeded regression)
- **`tests/test_cli_report.py`** — 3 end-to-end CLI smoke tests

**Total (through Phase 5): 187 unit tests passing, 0 warnings.**

### Phase 6a: Signal Reliability — Variance, Multi-Model, Outcome Stability

- **`src/bi_evals/config.py`** — `AgentConfig.models` list form with `_normalize_models` validator (mutually exclusive with singular `model`, backward compat); `ScoringConfig.repeats`; new `CompareConfig.regression_threshold` (default 0.2)
- **`src/bi_evals/store/schema.py`** — new `trial_results` table (PK `(run_id, test_id, model, trial_ix)`); `test_results` extended with `model` in PK + `trial_count`, `pass_count`, `pass_rate`, `score_mean`, `score_stddev`; `dimension_results` PK extended with `(model, trial_ix)`. Legacy Phase-5 DBs migrate idempotently via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` + backfill.
- **`src/bi_evals/store/ingest.py`** — trials grouped by `(test_id, model)`; writes per-trial `trial_results` rows and per-(run,test,model) aggregate `test_results` rows with stddev; dimension rows now per-trial
- **`src/bi_evals/store/queries.py`** — `list_models_for_run`, `test_results_by_model`, `model_summary`, `test_stability`, `flakiest_tests`, `_compute_stability` (pure); updated `list_tests` / `test_diff` / `_dims_by_test` to carry pass_rate and be keyed by `(test_id, model)`
- **`src/bi_evals/promptfoo/bridge.py`** — emits one provider block per model in `agent.models` (labeled `bi-evals:<model>`); writes `repeat: N` on each test when `scoring.repeats > 1`
- **`src/bi_evals/provider/entry.py`** — reads per-provider `model` override; trace filename includes model slug + 4-byte random suffix so trials don't overwrite each other
- **`src/bi_evals/compare/diff.py`** — rate-based classifier with configurable `regression_threshold`; matches pairs by `(test_id, model)`
- **`src/bi_evals/report/builder.py`** + **`templates/report.html.j2`** + **`_base.html.j2`** — Model Comparison section with summary table + inline SVG cost-vs-quality scatter; Stability section (top-5 flakiest)
- **`src/bi_evals/cli.py`** — `--repeats N` and `--yes` flags on `run`; cost-multiplier confirmation prompt for high-trial runs; new `bi-evals flakiness [--last-n N] [--limit N]` command
- **`tests/test_variance.py`** — 10 tests (stddev math, single-trial baseline, multi-trial ingest aggregation, fractional pass rates, rate-based threshold semantics)
- **`tests/test_multi_model.py`** — 11 tests (config parsing edge cases, bridge provider-per-model emission, repeat key handling, multi-model ingest matrix, per-model queries, diff keyed by (test, model))
- **`tests/test_stability.py`** — 8 tests (pure flip-counting: empty, all-pass, all-fail, alternating, streaks, pass-rate fractions)

**Total (through Phase 6a): 216 unit tests passing, 0 warnings.**

---

## Remaining

### Phase 6: Improve Eval Performance

Umbrella for three focused sub-phases. 6a shipped; 6b and 6c remain.

#### Phase 6b — Context and causation (`docs/phase-6b-plan.md`)

Explains *why* things changed. Depends on 6a schema; otherwise independent.

- **Prompt drift detection** — sha256 hash skill files per run into `runs.prompt_snapshot`. Compare annotates regressed tests with which of their files changed.
- **Dataset staleness** — optional `last_verified_at` on goldens; CLI warns on run, report includes freshness section.
- **Cost alerts** — post-hoc flag for runs > 2× median historical cost. `bi-evals cost` command surfaces anomalies.

#### Phase 6c — Anti-patterns (`docs/phase-6c-plan.md`)

Extends what gets tested. No schema changes; independent of 6a/6b.

- **`anti_patterns.forbidden_tables`** and **`anti_patterns.forbidden_columns`** on goldens.
- New `anti_pattern_compliance` scoring dimension using sqlglot structural analysis. Non-critical by default (opt-in to make critical); vacuous pass when no constraints defined.

### Phase 7: Polish the COVID-19 example project

A working COVID-19 example already exists under `tmp/my-evals/` (config + skill files + 3 golden categories + 16+ prior runs ingested). Phase 7 promotes it from a scratch directory into a first-class, repo-shipped example and closes the gaps needed for end-to-end usability:

- Move `tmp/my-evals/` → `examples/covid-19/` with cleaned-up config (no local creds, `.env.example` instead)
- Trim results history to 2–3 representative runs (keep one with a seeded regression for the compare walkthrough)
- Write an `examples/covid-19/README.md` with a full walkthrough: setup, `bi-evals run`, `report`, `compare`
- Fill obvious golden-coverage gaps (aim for 8–10 tests across cases / joins / us-states / time-series / aggregations)
- Verify the example runs cleanly on a fresh clone (new Snowflake trial or DuckDB-backed mock)

### Phase 8: UI for authoring, running, and reviewing evals

CLI + HTML reports cover the engineering loop. A UI is what unlocks non-developer contributors (analysts, PMs, domain experts) who should be writing goldens and reviewing regressions. Scope:

- **Golden authoring** — web form to create/edit goldens: question, reference SQL, category, skill-path expectations, row comparison config. Validates against DB schema (via `DescribeTableTool`), previews row output, writes YAML to `goldens/`.
- **Run triggering** — start a `bi-evals run` from the UI with filters (category, tags) and repeats; live progress via streaming logs
- **Report/compare browsing** — render the existing HTML reports inline; let users pick any two runs for a compare diff without shelling out
- **Regression drilldown** — click a regressed test → see full trace, SQL diff, skill files read, dimension failures side-by-side
- **Auth/access** — minimal (local single-user at first); design leaves room for multi-user when the DB moves to Postgres

Open design questions: embedded Flask/FastAPI + HTMX vs. a proper SPA; ship bundled with the CLI or a separate package; reuse Jinja templates or rebuild in React. Resolve before implementation starts.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Framework, not hardcoded project | Users bring their own skill files, golden tests, and DB credentials |
| Python over original JS design | MVP doc described JS; implemented in Python for consistency with data tooling ecosystem |
| `bi-evals init` scaffolds eval infra only | No opinion on skill/knowledge file structure — users point to theirs |
| Two provider types | `anthropic_tool_loop` for Claude-native agents, `api_endpoint` for existing APIs |
| Provider owns the full tool loop | Promptfoo's standard providers don't execute tool callbacks in a loop — our code handles send/tool_use/tool_result cycles |
| File-based trace communication | Provider writes JSON, scorer reads it — handles Promptfoo process isolation |
| Protocols over inheritance | `Tool`, `DatabaseClient` use `typing.Protocol` for extensibility |
| Snowflake only for MVP | `DatabaseClient` protocol designed for adding Postgres/BigQuery later |
| sqlglot for SQL parsing | Handles Snowflake dialect, aliases, CTEs without regex |
| QueryResult.error not raised | Execution failures stored in result, not thrown — lets execution dimension report cleanly and other dimensions cascade |
| Row comparison opt-in | `row_comparison.enabled` gates dimensions 5-7 — golden tests without expected result sets skip row comparison automatically |
| DuckDB for local store | Embedded, file-backed, zero infra; same SQL/schema ports cleanly to Postgres later. JSON results remain source of truth; DB is the queryable view |
| Golden metadata snapshotted at ingest | Editing a golden YAML never mutates historical runs — compare remains accurate over time |
| Tiered regression semantics | Critical dims (execution, row_completeness, value_accuracy) can flip verdict red even if overall score masks the failure |
| Auto-ingest at end of `run` | Single-command workflow; ingest failure warns but doesn't fail the run (JSON still on disk for retry) |
| Atomic observation is `(run, test, model, trial_ix)` | Lets us report pass rate + stddev rather than single-bit pass/fail; supports multi-model runs without collision |
| `test_results` is an aggregate, not a raw outcome | Per-trial detail lives in `trial_results`; aggregates are computed at ingest so queries stay cheap |
| Rate-based regression threshold (default 0.2) | Single-trial runs collapse to `{0, 1}` so a flip always clears 0.2 — legacy behavior preserved; multi-trial runs resist noise |
| `agent.model` and `agent.models` normalized internally | Users can write either; code always reads `.models` list |
