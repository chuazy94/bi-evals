# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is the test runner; all custom logic (provider, tools, scoring, storage, reporting, viewer) is Python. The MVP (Pillar 1: Accuracy + Explainability per `docs/mvp-eval-platform.md`) is complete and exceeded — Phases 1–5, 6a–6d, 7, and 7.5 have all shipped.

What works today:
- `bi-evals init` scaffolds a new eval project (config + dirs only; users bring their own skill/knowledge files)
- `bi-evals run` runs the full eval end-to-end and auto-ingests into DuckDB; supports `--filter`, `--dry-run`, `--repeats N`, `--no-cache`, `--yes`, `--verbose`. Auto-ingest fires on any successful JSON output, even when Promptfoo exits non-zero from failed tests.
- `bi-evals ingest <path>` backfills existing eval JSON
- `bi-evals report [--run-id ID]` writes a self-contained HTML report (filter strip, failures with per-dimension reasons, category dashboard, weakest dimensions, model summary + cost-vs-quality scatter, stability, freshness, cost alerts, all-tests table)
- `bi-evals compare A B` writes HTML regression diff with tiered verdict (🟢/🟡/🔴) and prompt-drift annotations; supports `latest` / `prev`
- `bi-evals ui` starts a local FastAPI + Jinja viewer on `localhost:8765` with three pages: runs list (with project filter, 10s meta refresh, "Compare prev → latest" shortcut, multi-row compare via checkboxes), single-run view (with category/model filters), and per-test drilldown (`/runs/{id}/tests/{id}`) showing generated SQL, reference SQL, per-dimension reasons, files-read, and the full trace JSON
- `bi-evals cost [--last-n N]` lists recent runs with cost-vs-prior-median multiplier
- `bi-evals flakiness [--last-n N] [--limit N]` lists tests by cross-run flip count
- `bi-evals view` opens the Promptfoo web UI for per-test deep-dive (separate from `bi-evals ui`)
- Multi-model evaluation via `agent.models: [...]`; per-model summary + scatter chart in the report; drilldown auto-redirects multi-model tests to the first model with a model picker
- Repeat-run variance via `--repeats N` or `scoring.repeats: N`; per-test pass rates and stddev
- `anthropic_tool_loop` provider runs the multi-turn Claude tool-calling loop with trace capture, SQL extraction, cost tracking
- `api_endpoint` provider calls external agent APIs with configurable response parsing
- `FileReaderTool` + `DescribeTableTool` serve skill files and DB schema to the agent
- `SnowflakeClient` executes SQL with structured results
- `GoldenTest` model loads expected results from YAML with optional `last_verified_at` (Phase 6b) and `anti_patterns` (Phase 6c)
- 293 unit tests passing, 0 warnings

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`pyproject.toml`** — pip-installable via uv (click, pydantic, pyyaml, anthropic, snowflake-connector-python, sqlglot, jinja2, duckdb, python-dotenv, fastapi, uvicorn, python-multipart)
- **`src/bi_evals/config.py`** — Pydantic config from `bi-evals.yaml`, `${ENV_VAR}` resolution, relative path resolution, automatic `.env` loading
- **`src/bi_evals/cli.py`** — Click CLI with `bi-evals init` scaffolding eval infrastructure (config + dirs, no skill/knowledge files)
- **`tests/test_config.py`** — 11 tests covering config loading, env vars, dotenv, defaults

### Phase 2: Tools + Agent Loop + Provider

- **`src/bi_evals/tools/`** — `Tool` protocol, `FileReaderTool` (path-traversal protected), `DescribeTableTool`, registry factory
- **`src/bi_evals/provider/cost.py`** — pricing map for Claude models
- **`src/bi_evals/provider/agent_loop.py`** — multi-turn tool-calling loop with full trace capture, SQL extraction (3 strategies), token counting, cost calculation
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST provider with configurable response keys (dot-notation), custom headers, optional trace capture
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()` entry point dispatching by `agent.type`; trace JSON written to `results/traces/`
- **`tests/test_agent_loop.py`** (24), **`tests/test_api_endpoint.py`** (11), **`tests/test_demo_routing.py`** (live API)

### Phase 3: Database + Golden Tests + 9-Dimension Scorer

- **`src/bi_evals/db/`** — `DatabaseClient` protocol, `SnowflakeClient`, factory
- **`src/bi_evals/golden/`** — `GoldenTest` Pydantic model, YAML loaders (`load_golden_test`, `load_golden_tests_with_paths`)
- **`src/bi_evals/scorer/sql_utils.py`** — sqlglot helpers (`extract_tables`, `extract_filter_columns`, `extract_select_columns`, and Phase-6c `extract_columns_with_tables`)
- **`src/bi_evals/scorer/dimensions.py`** — dimension evaluator functions + `DimensionResult` dataclass with descriptive `reason` strings (e.g. "missing filters: [...]", "Value mismatches: ...")
- **`src/bi_evals/scorer/entry.py`** — `get_assert()` Promptfoo scorer entry point
- **`tests/test_db.py`** (9), **`tests/test_golden.py`** (7), **`tests/test_scorer.py`** (39), **`tests/test_demo_scorer_phase_3.py`** (end-to-end demo)

### Phase 4: Promptfoo Bridge + `bi-evals run`

- **`src/bi_evals/promptfoo/bridge.py`** — translates `bi-evals.yaml` + goldens into `promptfooconfig.yaml`; emits one provider per model; writes `repeat: N` per test
- **`src/bi_evals/promptfoo/runner.py`** — invokes `npx promptfoo eval`, streams output
- Tiered/weighted scoring: critical-dim gating (`execution`, `row_completeness`, `value_accuracy`) + `pass_threshold` on weighted score

### Phase 5: Storage + Reporting + Regression Compare

- **`src/bi_evals/store/`** — DuckDB layer
  - `schema.py` — tables (`runs`, `test_results`, `trial_results`, `dimension_results`) + indexes; idempotent migrations including legacy-PK rebuild via copy/drop/rename
  - `client.py` — `connect(db_path)` context manager with retry on lock contention; `read_only=True` mode for viewer + report/compare
  - `ingest.py` — idempotent ingest, golden metadata snapshotted, traces inlined
  - `queries.py` — frozen-dataclass read helpers
- **`src/bi_evals/compare/diff.py`** — pure regression classifier (regressed / fixed / unchanged / added / removed) + tiered verdict
- **`src/bi_evals/report/`** — Jinja2 templates with inline CSS, no external URLs; `builder.py` does all data prep
- `ingest`, `report`, `compare` CLI commands; auto-ingest at end of `run`
- **`tests/test_store_*.py`** (18), **`tests/test_compare_diff.py`** (13), **`tests/test_report_builder.py`** (8 after Phase 7.5), **`tests/test_cli_report.py`** (3)

### Phase 6a: Variance, Multi-Model, Outcome Stability

- **`config.py`** — `AgentConfig.models` list with mutual-exclusion validator; `ScoringConfig.repeats`; `CompareConfig.regression_threshold`
- **`store/schema.py`** — `trial_results` table; `test_results` extended with `model` in PK + aggregates (`pass_rate`, `score_mean`, `score_stddev`); `dimension_results` PK extended with `(model, trial_ix)`
- **`store/ingest.py`** — trials grouped by `(test_id, model)`; per-trial + aggregate rows
- **`store/queries.py`** — `list_models_for_run`, `model_summary`, `test_stability`, `flakiest_tests`, etc.
- **`promptfoo/bridge.py`** — provider per model, labeled `bi-evals:<model>`
- **`provider/entry.py`** — per-provider model override; trace filename includes model slug + random suffix
- **`compare/diff.py`** — rate-based classifier with configurable threshold; pairs by `(test_id, model)`
- **`report/`** — model comparison section, cost-vs-quality SVG scatter, stability section
- **`cli.py`** — `--repeats`, `--yes`, cost-multiplier confirmation; `bi-evals flakiness` command
- **`tests/test_variance.py`** (10), **`tests/test_multi_model.py`** (11), **`tests/test_stability.py`** (8)

### Phase 6b: Prompt Drift + Staleness + Cost Alerts

- **Prompt drift** — `runs.prompt_snapshot` SHA256 of every file the agent read (resolved via each `file_reader.base_dir`); `prompt_diff` returns added/removed/modified files between two runs; per-transition annotation showing which changed files each test actually read
- **Dataset staleness** — `GoldenTest.last_verified_at` (optional); `_warn_stale_goldens()` pre-run warning when older than `scoring.stale_after_days` (default 180); report includes "Dataset freshness" card with stale/unverified counts and fresh-vs-stale pass-rate split; worst-offenders table
- **Cost alerts** — post-run multiplier check (`storage.cost_alert_multiplier`, `cost_alert_window`) prints alert when run > N× median of prior W runs; `bi-evals cost` command surfaces history
- **`compare/diff.py`** — prompt-drift annotations on transitions
- **`tests/test_prompt_drift.py`**, **`tests/test_staleness.py`**, **`tests/test_cost_alerts.py`** — ~24 tests across drift/staleness/cost

### Phase 6c: Anti-Patterns

- **`golden/model.py`** — `AntiPatterns` model with `forbidden_tables` and `forbidden_columns` (latter accepts `"TABLE.COL"` or bare `"COL"`); `GoldenTest.anti_patterns: AntiPatterns | None`
- **`scorer/sql_utils.py`** — new `extract_columns_with_tables()` with per-SELECT scope analysis, alias resolution, CTE-launder collapsing (CTE refs map to `None` so bare-name forbidden entries still flag)
- **`scorer/dimensions.py`** — `_check_anti_patterns()` and `check_anti_pattern_compliance()`. Bare table entries match schema-qualified forms; qualified column entries match exact (table, col) and CTE-laundered `(None, col)` references
- **`config.py`** — `anti_pattern_compliance` added to `ALL_DIMENSIONS` (10th dim) with default weight 2.0; non-critical by default
- **`report/builder.py`** — `_drop_vacuous_dimensions()` removes dimensions where every row has `passed=true` and reason starts with `"skipped:"` (avoids 100% rows that are vacuous passes)
- **`tests/test_anti_patterns.py`** — 24 tests covering config wiring, YAML round-trip, column-table extraction, anti-pattern checks, vacuous-pass dropping

### Phase 6d: Knowledge-File Staleness

- **`config.py`** — new `ScoringConfig.knowledge_stale_after_days` (default 90, 0 disables)
- **`store/queries.py`** — new `StaleKnowledgeFile` dataclass and `stale_knowledge_files()` helper. Reads the run's `prompt_snapshot`, re-stats each file (current disk state), returns ones older than the threshold sorted oldest-first
- **`cli.py`** — new `_warn_stale_knowledge()` called pre-run after `_warn_stale_goldens`. Uses the latest ingested run's snapshot as the read-set source; silent when no history exists
- **`report/builder.py`** + **`templates/report.html.j2`** — "Knowledge freshness" card next to "Dataset freshness", listing worst offenders by mtime
- Warning only — no scoring penalty, by design (a stale knowledge file is a nudge, not a fail)
- **`tests/test_staleness.py`** — 7 new cases (threshold disabled, no snapshot, stale file flagged, fresh file skipped, only-read-files included, missing files skipped, sorted oldest-first)

### Phase 7: Minimal local viewer

Replaced the `run → report → open file://...html` loop with a single command. Deliberately minimal — no SPA, no React, no build step. See `docs/phase-7-plan.md`.

- **`bi-evals ui`** — FastAPI + Jinja server on `localhost:8765`, auto-opens browser, 10s meta-refresh on the runs list
- **Three pages** — runs list, single-run view, compare view (latter two reuse existing `report/builder.py` templates verbatim)
- **`src/bi_evals/ui/server.py`** — ~100 lines wrapping `store/queries.py` + `report/builder.py` over HTTP; read-only DB connections; six locked design decisions documented in the plan
- **`src/bi_evals/ui/templates/runs_list.html.j2`** — checkboxes + "Compare selected" form; "Compare prev → latest" shortcut; empty state; error banner
- **`src/bi_evals/ui/templates/not_found.html.j2`** — friendly 404 with link back to runs list
- **CLI bug fix in `cli.py:144-167`** — auto-ingest now runs whenever `results_output` exists, not just when Promptfoo exits 0 (failures used to skip ingest entirely; npm update notice can also override exit code with 100 — both now harmless)
- **`tests/test_ui.py`** — 8 tests via FastAPI's `TestClient` covering all routes + happy/sad paths
- New deps: `fastapi`, `uvicorn[standard]`, `python-multipart`, `httpx` (dev)

### Phase 7.5: Viewer enhancements

Real usage of Phase 7 surfaced four gaps. Same architectural envelope (Jinja + FastAPI, no JS framework) — explicitly disposable scaffolding before the SPA rebuild. See `docs/phase-7.5-plan.md`.

- **`report/builder.py`** — `build_report_html` accepts `category` / `model` filters; pre-computes per-test list and a failure view (each failed test with its sorted failed dimensions, critical first)
- **`report/templates/report.html.j2`** — filter strip (category + model dropdowns, plain `<select onchange=this.form.submit()>`); "Failures" section listing failed tests with **per-dimension reasons inline** (e.g. "missing filters: ['STATE']"); "All tests" table with absolute-path drilldown links
- **`store/queries.py`** — `list_projects`, `get_test`, `get_test_extras` (generated_sql, reference_sql, files_read, pretty-printed trace_json); `list_runs` accepts `project_name` filter
- **`ui/server.py`** — new `GET /runs/{run_id}/tests/{test_id:path}` route; multi-model auto-redirects to first model with picker; runs list `?project=` filter; meta-refresh URL preserves the project filter
- **`ui/templates/test_detail.html.j2`** — drilldown page: status/score/cost stats, question, failure summary, per-dimension table with full reasons, generated SQL, reference SQL, files-read list, collapsed full trace, model picker for multi-model runs, breadcrumbs
- **`ui/templates/runs_list.html.j2`** — project dropdown shown only when ≥2 projects exist; refresh URL preserves filter
- **Bug fix** — drilldown links in `report.html.j2` are now absolute (`/runs/{run_id}/tests/{test_id}`); previous relative `tests/{id}` hrefs lost the run-id segment when the browser at `/runs/<id>` (no trailing slash) resolved them per RFC 3986
- **`tests/test_ui.py`** (+7), **`tests/test_report_builder.py`** (+2), **`tests/test_store_queries.py`** (+5) — 14 new tests covering drilldown, filters, project scoping
- No new dependencies, no new top-level CLI commands

**Total: 293 unit tests passing, 0 warnings.**

---

## Remaining

### Phase 8: COVID-19 Example Project

A working COVID-19 example exists under `tmp/my-evals/` (config + skill files + 3 golden categories + 16+ prior runs). Promote it to a first-class repo example. See `docs/phase-8-plan.md`.

- Move `tmp/my-evals/` → `examples/covid-19/` with cleaned-up config (no creds, `.env.example` instead)
- Trim results history to 2–3 representative runs (keep one with seeded regression)
- `examples/covid-19/README.md` walkthrough: setup, `run`, viewer
- Fill golden-coverage gaps (target 8–10 tests across categories)
- Verify on a fresh clone

### Deferred (no committed phase yet)

Sized once Phase 8 ships and we have real users:
- `bi-evals doctor` — pre-run validation of config, env vars, DB connectivity, API keys
- DuckDB as a built-in `database.type` — zero-cred eval target for demos
- `bi-evals init --from <dir>` — scaffold from existing artifacts
- Snowflake SSO (`authenticator: externalbrowser`)
- Additional warehouses (Postgres, BigQuery, Redshift, Databricks) — add when ≥2 users ask for the same one
- SPA rebuild of the viewer (golden authoring, run triggering, trend charts, per-test history, regression drilldown with SQL diff) — committed for whenever golden authoring is needed; will throw away the Jinja templates and reuse the existing data layer
- Production-traffic golden import (PostHog / Langfuse / CSV)

### Pillars 2 & 3 (post-MVP — see `docs/mvp-eval-platform.md`)

The MVP plan's Pillar 1 (Accuracy + Explainability) is fully shipped. The next two pillars are explicitly out of MVP scope and not yet planned.

- **Pillar 2 Faithfulness** — LLM-as-judge layer that decomposes natural-language responses into atomic claims and verifies each against the data. Phase 1–2 trace capture is the prerequisite (already shipped).
- **Pillar 3 Confidence** — multi-trial pass@k/pass^k (groundwork laid by 6a `repeats`), composite reliability score per category, graduation model (eval → regression gate), trust dashboard for non-technical stakeholders.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Framework, not hardcoded project | Users bring their own skill files, golden tests, DB credentials |
| Python over original JS design | MVP doc described JS; Python chosen for consistency with data tooling |
| `bi-evals init` scaffolds eval infra only | No opinion on skill/knowledge file structure — users point to theirs |
| Two provider types | `anthropic_tool_loop` for Claude-native, `api_endpoint` for existing APIs |
| Provider owns the full tool loop | Promptfoo's standard providers don't execute tool callbacks in a loop |
| File-based trace communication | Provider writes JSON, scorer reads it — handles Promptfoo process isolation |
| Protocols over inheritance | `Tool`, `DatabaseClient` use `typing.Protocol` for extensibility |
| Snowflake only for MVP | `DatabaseClient` protocol designed for adding Postgres/BigQuery later |
| sqlglot for SQL parsing | Handles Snowflake dialect, aliases, CTEs without regex |
| Row comparison opt-in | `row_comparison.enabled` gates dimensions 5–7 |
| DuckDB for local store | Embedded, file-backed, zero infra; same SQL ports cleanly to Postgres |
| Golden metadata snapshotted at ingest | Editing a golden YAML never mutates historical runs |
| Tiered regression semantics | Critical dims can flip verdict red even if overall score masks failure |
| Auto-ingest whenever JSON exists, not just on exit-0 | Failed tests, npm update notices, and other non-zero exits no longer skip ingest; the failure case is exactly when users want the report |
| Atomic observation is `(run, test, model, trial_ix)` | Pass rate + stddev rather than single-bit pass/fail; multi-model without collision |
| `test_results` is an aggregate | Per-trial detail in `trial_results`; aggregates pre-computed at ingest |
| Rate-based regression threshold (default 0.2) | Single-trial collapses to {0,1} so any flip clears 0.2 — legacy preserved; multi-trial resists noise |
| `agent.model` and `agent.models` normalized | Users write either; code reads `.models` list |
| Prompt snapshot resolves via `file_reader.base_dir` | `files_read` paths are tool-relative, not project-relative |
| Pre-6b runs return empty `prompt_diff` | NULL `prompt_snapshot` short-circuits to no-diff rather than reporting every file as added |
| Anti-patterns non-critical by default (Phase 6c) | A violation that still produced correct rows is a warning, not a hard fail; teams can opt in to gating |
| Vacuously-passing dimensions dropped from report | A 100% pass rate from `"skipped: no anti-patterns defined"` would dilute the scorecard |
| Viewer is intentionally throwaway | Jinja + FastAPI for v1 (Phase 7) and v1.5 (Phase 7.5); SPA rebuild reserved for when golden authoring lands; data layer (`store/queries.py`, `report/builder.py`) is the durable asset |
| Viewer auto-refresh via meta refresh | One line of HTML; no JS, no SSE; runs list only (drilldown/compare are snapshots) |
| Drilldown links use absolute paths | Relative `tests/{id}` lost the run-id segment per RFC 3986 when browser was at `/runs/<id>` (no trailing slash) |
| Per-dimension failure reasons render inline in the failures section | Aggregate `fail_reason` is just a verdict ("Failed critical dim(s): ['value_accuracy']"); the scorer's per-dimension reason text (e.g. "missing filters: ['STATE']") is what's actually actionable |
