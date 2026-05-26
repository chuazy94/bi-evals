# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is the test runner; all custom logic (provider, tools, scoring, storage, reporting, viewer) is Python. The MVP (Pillar 1: Accuracy + Explainability per `docs/mvp-eval-platform.md`) is complete and exceeded — Phases 1–5, 6a–6d, 7, and 7.5 have all shipped, plus a "Two modes" UX rebuild (Built-in / BYO) and `bi-evals doctor` for pre-flight validation.

What works today:

- `bi-evals init built-in` / `bi-evals init byo` scaffold mode-specific projects; bare `bi-evals init` errors with a hint. BYO scaffold ships `adapter_example.py` (a FastAPI shim demonstrating the response contract).
- `bi-evals doctor` validates a project's runtime setup before a paid eval run. Built-in: Anthropic API key reachable, system prompt + tool `base_dir`s exist, Snowflake `SELECT 1` succeeds, `npx` on PATH. BYO: synthetic POST + JSON Schema validation + scoring-coverage report. Exits non-zero on required failures, warns on missing optional fields.
- `bi-evals run` runs the full eval end-to-end and auto-ingests into DuckDB; supports `--filter`, `--dry-run`, `--repeats N`, `--no-cache`, `--yes`, `--verbose`. Auto-ingest fires on any successful JSON output, even when Promptfoo exits non-zero from failed tests.
- `bi-evals ingest <path>` backfills existing eval JSON.
- `bi-evals report [--run-id ID]` writes a self-contained HTML report (filter strip, per-dimension failure reasons, category dashboard, weakest dimensions, weighted-score rule + per-test verdict sentence, model summary + cost-vs-quality scatter, stability, freshness, cost alerts, all-tests table).
- `bi-evals compare A B` writes HTML regression diff with tiered verdict (🟢/🟡/🔴) and prompt-drift annotations; supports `latest` / `prev`.
- `bi-evals ui` starts a local FastAPI + Jinja viewer on `localhost:8765` with three pages: runs list (project filter, 10s meta refresh, "Compare prev → latest" shortcut, multi-row compare, triage filters + "regressed since" badge), single-run view (category/model filters), and per-test drilldown showing generated SQL, reference SQL, per-dimension reasons, files-read, and the full trace JSON.
- `bi-evals cost [--last-n N]` lists recent runs with cost-vs-prior-median multiplier.
- `bi-evals flakiness [--last-n N] [--limit N]` lists tests by cross-run flip count.
- `bi-evals view` opens the Promptfoo web UI for per-test deep-dive (separate from `bi-evals ui`).
- Two run modes — **Built-in** (`agent.type: anthropic_tool_loop`) runs Claude + skill files inside bi-evals; **BYO** (`agent.type: api_endpoint`) POSTs to the user's agent. Named throughout README, CLAUDE.md, getting-started, and the CLI scaffolds.
- BYO response contract documented at `docs/byo-response-contract.md` with a machine-readable JSON Schema at `src/bi_evals/byo_response_schema.json` (bundled in the wheel, loaded at runtime by `bi-evals doctor`).
- Multi-model evaluation via `agent.models: [...]`; per-model summary + scatter chart in the report; drilldown auto-redirects multi-model tests to the first model with a model picker.
- Repeat-run variance via `--repeats N` or `scoring.repeats: N`; per-test pass rates and stddev.
- `anthropic_tool_loop` provider runs the multi-turn Claude tool-calling loop with trace capture, SQL extraction, cost tracking.
- `api_endpoint` provider calls external agent APIs with configurable response parsing (`response_sql_key` / `response_text_key`, dot-notation).
- `FileReaderTool` + `DescribeTableTool` serve skill files and DB schema to the agent.
- `SnowflakeClient` executes SQL with structured results.
- `GoldenTest` model loads expected results from YAML with optional `last_verified_at` (Phase 6b) and `anti_patterns` (Phase 6c).
- 359 unit tests passing, 0 warnings. Strict YAML loading with fail-fast on unresolved `${ENV_VAR}` references.

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`pyproject.toml`** — pip-installable via uv (click, pydantic, pyyaml, anthropic, snowflake-connector-python, sqlglot, jinja2, duckdb, python-dotenv, fastapi, uvicorn, python-multipart, jsonschema)
- **`src/bi_evals/config.py`** — Pydantic config from `bi-evals.yaml`, `${ENV_VAR}` resolution with strict fail-fast on missing vars, relative path resolution, automatic `.env` loading
- **`src/bi_evals/cli.py`** — Click CLI with mode-aware `init` (built-in / byo subcommands)
- **`tests/test_config.py`** — covers config loading, env vars, dotenv, defaults, strict-mode failures
- **`tests/test_cli_init.py`** — 12 tests covering both `init` subcommands

### Phase 2: Tools + Agent Loop + Provider

- **`src/bi_evals/tools/`** — `Tool` protocol, `FileReaderTool` (path-traversal protected), `DescribeTableTool`, registry factory
- **`src/bi_evals/provider/cost.py`** — pricing map for Claude models
- **`src/bi_evals/provider/agent_loop.py`** — multi-turn tool-calling loop with full trace capture, SQL extraction (3 strategies), token counting, cost calculation
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST provider with configurable response keys (dot-notation), custom headers, optional trace capture
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()` entry point dispatching by `agent.type`; trace JSON written via `trace_paths` to `results/traces/`
- **`src/bi_evals/trace_paths.py`** — per-(test, model, invocation) trace filename helpers shared by provider (writer) and scorer (reader); prevents multi-model collisions
- **`tests/test_agent_loop.py`** (24), **`tests/test_api_endpoint.py`** (11), **`tests/test_demo_routing.py`** (live API)

### Phase 3: Database + Golden Tests + 10-Dimension Scorer

- **`src/bi_evals/db/`** — `DatabaseClient` protocol, `SnowflakeClient`, factory
- **`src/bi_evals/golden/`** — `GoldenTest` Pydantic model, YAML loaders
- **`src/bi_evals/scorer/sql_utils.py`** — sqlglot helpers (`extract_tables`, `extract_filter_columns`, `extract_select_columns`, `extract_columns_with_tables`)
- **`src/bi_evals/scorer/dimensions.py`** — 10 dimension evaluators + `DimensionResult` with descriptive `reason` strings
- **`src/bi_evals/scorer/entry.py`** — `get_assert()` Promptfoo scorer entry point; grades the per-(test, model) trace via `trace_paths`
- **`tests/test_db.py`** (9), **`tests/test_golden.py`** (7), **`tests/test_scorer.py`** (39), **`tests/test_anti_patterns.py`** (24), **`tests/test_demo_scorer_phase_3.py`** (end-to-end demo)

### Phase 4: Promptfoo Bridge + `bi-evals run`

- **`src/bi_evals/promptfoo/bridge.py`** — translates `bi-evals.yaml` + goldens into `promptfooconfig.yaml`; emits one provider per model; resolves package root via `parent.parent` so paths work under both editable and wheel installs
- **`src/bi_evals/promptfoo/runner.py`** — invokes `npx promptfoo eval`, streams output
- Tiered/weighted scoring: critical-dim gating (`execution`, `row_completeness`, `value_accuracy`) + `pass_threshold` on weighted score
- **`tests/test_bridge.py`** — includes parametrized regression test for the wheel-install path bug (issue #18)

### Phase 5: Storage + Reporting + Regression Compare

- **`src/bi_evals/store/`** — DuckDB layer (`schema.py`, `client.py` with read-only mode + retry on lock contention, `ingest.py` idempotent, `queries.py` frozen dataclasses)
- **`src/bi_evals/compare/diff.py`** — pure regression classifier + tiered verdict + prompt-drift annotations
- **`src/bi_evals/report/`** — Jinja2 templates with inline CSS, no external URLs; `builder.py` does all data prep; surfaces the weighted-score rule and a one-sentence verdict per test
- `ingest`, `report`, `compare` CLI commands; auto-ingest at end of `run`
- **`tests/test_store_*.py`** (18), **`tests/test_compare_diff.py`** (13), **`tests/test_report_builder.py`** (8), **`tests/test_cli_report.py`** (3)

### Phase 6a: Variance, Multi-Model, Outcome Stability

- **`config.py`** — `AgentConfig.models` list with mutual-exclusion validator; `ScoringConfig.repeats`; `CompareConfig.regression_threshold`
- **`store/schema.py`** — `trial_results` table; `test_results` extended with `model` in PK + aggregates; `dimension_results` PK extended with `(model, trial_ix)`
- **`store/ingest.py`** — trials grouped by `(test_id, model)`; per-trial + aggregate rows
- **`store/queries.py`** — `list_models_for_run`, `model_summary`, `test_stability`, `flakiest_tests`
- **`promptfoo/bridge.py`** — provider per model, labeled `bi-evals:<model>`
- **`compare/diff.py`** — rate-based classifier with configurable threshold; pairs by `(test_id, model)`
- **`report/`** — model comparison section, cost-vs-quality SVG scatter, stability section
- **`cli.py`** — `--repeats`, `--yes`, cost-multiplier confirmation; `bi-evals flakiness` command
- **`tests/test_variance.py`** (10), **`tests/test_multi_model.py`** (11), **`tests/test_stability.py`** (8)

### Phase 6b: Prompt Drift + Staleness + Cost Alerts

- **Prompt drift** — `runs.prompt_snapshot` SHA256 of every file the agent read; `prompt_diff` returns added/removed/modified files between two runs; per-transition annotation showing which changed files each test actually read
- **Dataset staleness** — `GoldenTest.last_verified_at`; pre-run warning when older than `scoring.stale_after_days` (default 180); report "Dataset freshness" card with stale/unverified counts and fresh-vs-stale pass-rate split
- **Cost alerts** — post-run multiplier check (`storage.cost_alert_multiplier`, `cost_alert_window`); `bi-evals cost` command
- **`tests/test_prompt_drift.py`**, **`tests/test_staleness.py`**, **`tests/test_cost_alerts.py`** — ~24 tests across drift/staleness/cost

### Phase 6c: Anti-Patterns

- **`golden/model.py`** — `AntiPatterns` model with `forbidden_tables` and `forbidden_columns` (qualified `"TABLE.COL"` or bare `"COL"`); `GoldenTest.anti_patterns: AntiPatterns | None`
- **`scorer/sql_utils.py`** — `extract_columns_with_tables()` with per-SELECT scope, alias resolution, CTE-launder collapsing
- **`scorer/dimensions.py`** — `_check_anti_patterns()` and `check_anti_pattern_compliance()`. Bare table entries match schema-qualified forms; qualified column entries match exact and CTE-laundered references
- **`config.py`** — `anti_pattern_compliance` added to `ALL_DIMENSIONS` (10th dim) with default weight 2.0; non-critical by default
- **`report/builder.py`** — `_drop_vacuous_dimensions()` removes dims where every row passed with a `"skipped:"` reason

### Phase 6d: Polish (post-MVP review fixes)

- **PR #14** — strict YAML loading; unresolved `${ENV_VAR}` references now fail at load time instead of silently empty
- **PR #15** — report surfaces the weighted-score rule and a per-test verdict sentence ("Failed critical dim: value_accuracy" rather than just a red badge)
- **PR #19** — `_get_package_root()` in `promptfoo/bridge.py` now walks two parents instead of four (issue #18); fixes BYO-eval-from-wheel-install crash and adds parametrized regression test

### Phase 7: Viewer (FastAPI + Jinja, intentionally throwaway)

- **`src/bi_evals/ui/server.py`** — runs list, single-run view, per-test drilldown (`/runs/{run_id}/tests/{test_id:path}`); project filter; meta-refresh; multi-row compare
- **`src/bi_evals/ui/templates/`** — runs_list, run_view, test_detail, base
- **`tests/test_ui.py`** — 7+ tests

### Phase 7.5: Viewer enhancements

- Filter strip (category + model); "Failures" section with per-dimension reasons inline; "All tests" table with absolute-path drilldown links
- Drilldown page: status/score/cost stats, question, failure summary, per-dimension table, generated SQL, reference SQL, files-read, collapsed full trace, model picker, breadcrumbs
- Project dropdown shown only when ≥2 projects exist; refresh URL preserves filter
- Bug fix — absolute drilldown links per RFC 3986
- **PR #16** — runs-list triage filters and "regressed since" badge

### Phase 7.6: Two-modes UX

The framing emerged from real BYO testing and is now load-bearing across docs and the CLI surface.

- **PR #20** — Named the two run modes in README (a "Two modes" section before install) and CLAUDE.md.
- **PR #21** — `docs/getting-started.md` rewritten as a per-mode walkthrough: branches at Step 0, tags subsequent steps "(Built-in only)" / "(BYO only)" where they diverge, shared sections (golden tests, run, view) stay shared.
- **PR #22** — `bi-evals init` split into `init built-in` and `init byo` subcommands. Bare `init` errors. Each mode writes a different `bi-evals.yaml`, different `.env.example`, and BYO additionally ships `adapter_example.py` (a ~70-line FastAPI reference shim).
- **`tests/test_cli_init.py`** — 12 tests covering both subcommands

### Phase 7.7: Plug-and-play infrastructure

Two PRs that make BYO actually usable for new customers without reading provider source.

- **PR #23** (`docs/byo-response-contract`, merged) — `docs/byo-response-contract.md` with three canonical response examples; `src/bi_evals/byo_response_schema.json` (JSON Schema 2020-12) bundled inside the package and loaded at runtime via `importlib.resources`. Automated review surfaced five items (missing `anti_pattern_compliance` row in the coverage table, schema gap on `tool_use` required fields, stale forward reference in `getting-started.md`, markdown rendering nit, internal PR cross-link that would rot) — all addressed in commit `e0c56c3` before merge. The schema now uses an `if/then` constraint to enforce `tool_name`/`tool_input` on `tool_use` steps.
- **PR #24** (`feat/cli-doctor`, merged) — `bi-evals doctor` command. **Built-in mode:** Anthropic API key reachable (no tokens spent), system prompt file exists, every `file_reader` `base_dir` exists, Snowflake `SELECT 1` succeeds, `npx` on PATH. **BYO mode:** POSTs a synthetic question, validates response against the bundled JSON Schema, reports scoring coverage (which optional fields are present and what each unlocks). Required failures exit 1; optional failures warn. New runtime dep: `jsonschema>=4.20`.
- **`tests/test_doctor.py`** — 23 tests; BYO tests use a real localhost `HTTPServer` (matching `test_api_endpoint.py`) to exercise the POST/parse path end-to-end; Anthropic + Snowflake mocked.

### Phase 7.8: Harness engineering

- **PR #17** — `CLAUDE.md` gains a "Current phase: MVP" section naming the north star and what to deprioritize. Project gains three Claude Code hooks in `.claude/settings.json`: PostToolUse `ruff format` on `.py` edits; PreToolUse block on edits under `results/` or `*.duckdb`; PostToolUse reminder on edits to user-facing surfaces (`config.py`, `cli.py`, `scorer/dimensions.py`, `scorer/entry.py`, `golden/*.yaml`) to also sync `tmp/my-evals/`. Format hook uses `git rev-parse --show-toplevel` for portability.

### Bug fixes

- **PR #12** — Scorer was grading a stale legacy trace path; now uses `src/bi_evals/trace_paths.py` to compute the per-(test, model, invocation) filename both writer and reader agree on. Eliminates a class of silent-fail-then-wrong-score bugs in multi-model + multi-trial runs.
- **PR #13** — Removed auto Claude Code Review workflow (unused).

**Total: 359 unit tests passing, 0 warnings.**

---

## Remaining

### Phase 8: COVID-19 Example Project

A working COVID-19 example exists under `tmp/my-evals/` (config + skill files + 3 golden categories + 16+ prior runs). Promote it to a first-class repo example. Still pending — see `docs/phase-8-plan.md`.

- Move `tmp/my-evals/` → `examples/covid-19/` with cleaned-up config (no creds, `.env.example` instead)
- Trim results history to 2–3 representative runs (keep one with seeded regression)
- `examples/covid-19/README.md` walkthrough: setup, `run`, viewer
- Fill golden-coverage gaps (target 8–10 tests across categories)
- Verify on a fresh clone

### Follow-ups surfaced from recent reviews

- "9 dimensions" → "10 dimensions" inconsistency in `README.md` (caught during PR #21 review; pre-existing, not yet fixed)
- Migrate `api_endpoint.py`'s manual `_get_nested` response parsing to use the JSON Schema for validation rather than relying on `bi-evals doctor` as an opt-in check
- Pydantic discriminated union on `AgentConfig` — schema-level enforcement that BYO configs require `endpoint` and Built-in configs require `system_prompt`/`tools`. Currently only enforced at runtime

### Deferred (no committed phase yet)

Sized once Phase 8 ships and we have real users:

- DuckDB as a built-in `database.type` — zero-cred eval target for demos
- `bi-evals init --from <dir>` — scaffold from existing artifacts
- Snowflake SSO (`authenticator: externalbrowser`)
- Additional warehouses (Postgres, BigQuery, Redshift, Databricks) — add when ≥2 users ask for the same one
- SPA rebuild of the viewer (golden authoring, run triggering, trend charts, per-test history, regression drilldown with SQL diff)
- Production-traffic golden import (PostHog / Langfuse / CSV)
- OpenAI tool-loop provider — only build when a real customer asks; `api_endpoint` is the universal escape hatch

### Pillars 2 & 3 (post-MVP — see `docs/mvp-eval-platform.md`)

The MVP plan's Pillar 1 (Accuracy + Explainability) is fully shipped. The next two pillars are explicitly out of MVP scope and not yet planned.

- **Pillar 2 Faithfulness** — LLM-as-judge layer that decomposes natural-language responses into atomic claims and verifies each against the data. Phase 1–2 trace capture is the prerequisite (already shipped).
- **Pillar 3 Confidence** — multi-trial pass@k/pass^k (groundwork from 6a `repeats`), composite reliability score per category, graduation model (eval → regression gate), trust dashboard for non-technical stakeholders.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Framework, not hardcoded project | Users bring their own skill files, golden tests, DB credentials |
| Python over original JS design | MVP doc described JS; Python chosen for consistency with data tooling |
| Two run modes (Built-in / BYO) are first-class | Production teams already have agents; "use ours" doesn't fit. BYO via `api_endpoint` is the realistic path; Built-in is for greenfield. Named throughout docs and CLI |
| `bi-evals init` is mode-required | Single scaffold confused BYO users — they had to delete half the files. Hard split means the scaffold matches the mode you picked |
| BYO response contract bundled in package, not docs | Schema is parsed at runtime by `bi-evals doctor`; loading from `docs/` would couple the runtime to install layout. `importlib.resources` works cleanly on bundled package data |
| `bi-evals doctor` is one command, two modes | Pre-flight validation is the same concern in both modes; dispatching on `agent.type` keeps the user-facing surface flat |
| `doctor` Snowflake check is a real `SELECT 1` | Client instantiation alone misses network / warehouse / role issues — the actual common failure mode. The credit cost is negligible |
| Two provider types | `anthropic_tool_loop` for Claude-native, `api_endpoint` for existing APIs |
| Provider owns the full tool loop | Promptfoo's standard providers don't execute tool callbacks in a loop |
| File-based trace communication | Provider writes JSON, scorer reads it — handles Promptfoo process isolation |
| Trace filenames per `(test, model, invocation)` | Avoids multi-model + multi-trial collisions; both writer and reader compute the same name via `trace_paths.py` |
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
| Strict YAML loading (PR #14) | Unresolved `${ENV_VAR}` references now fail at load instead of silently emptying — caught a class of "why is my eval all-fail" bugs |
| Bridge walks two parents (PR #19) | Original walked four and re-appended `src/bi_evals/...`, which only worked for editable installs from src-layout repo. Wheel installs crashed every test |
| Viewer is intentionally throwaway | Jinja + FastAPI for v1 and v1.5; SPA rebuild reserved for when golden authoring lands; data layer (`store/queries.py`, `report/builder.py`) is the durable asset |
| Viewer auto-refresh via meta refresh | One line of HTML; no JS, no SSE; runs list only |
| Drilldown links use absolute paths | Relative `tests/{id}` lost the run-id segment per RFC 3986 |
| Per-dimension failure reasons render inline in failures | Aggregate `fail_reason` is just a verdict; per-dim reason text is what's actually actionable |
| Harness hooks block edits to `results/` and `*.duckdb` | These are produced by `run`/`ingest`. Agent hand-edits almost always indicate a fake-the-result attempt |
| `tmp/my-evals` reminder hook | CLAUDE.md says config/CLI/scoring/golden changes need a `tmp/my-evals/` sync; agents forget — the hook nudges |
