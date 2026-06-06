# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is the test runner; all custom logic (provider/adapters, tools, scoring, storage, reporting, viewer) is Python. The MVP (Pillar 1: Accuracy + Explainability per `docs/mvp-eval-platform.md`) is complete and exceeded. The project is now mid-way through a **response-evaluation architecture pivot** (`docs/bi-eval-integration-analysis.md`): reframing the agent layer as **one canonical contract `{generated_sql, trace}`, many adapters** — bi-evals scores the real agent's response and never rebuilds the agent. Steps 1 (contract + adapter registry, PR #28) and Phase 2a (adapter-nested schema, PR #29) are merged to `main`.

What works today:

- `bi-evals init api_endpoint` (default on-ramp) / `bi-evals init dev` (dev-only driving adapter) scaffold projects with the adapter-nested config shape; bare `bi-evals init` errors with a hint. The api_endpoint scaffold ships `adapter_example.py` (a FastAPI shim demonstrating the response contract).
- `bi-evals doctor` validates a project's runtime setup before a paid eval run. api_endpoint: synthetic POST + JSON Schema validation + scoring-coverage report. dev (anthropic_tool_loop): Anthropic API key reachable, system prompt + tool `base_dir`s exist, Snowflake `SELECT 1` succeeds, `npx` on PATH. Exits non-zero on required failures, warns on missing optional fields.
- `bi-evals run` runs the full eval end-to-end and auto-ingests into DuckDB; supports `--filter`, `--dry-run`, `--repeats N`, `--no-cache`, `--yes`, `--verbose`.
- `bi-evals ingest <path>` backfills existing eval JSON.
- `bi-evals report [--run-id ID]` writes a self-contained HTML report (filter strip, per-dimension failure reasons, category dashboard, weakest dimensions, weighted-score rule + per-test verdict sentence, model summary + cost-vs-quality scatter, stability, freshness, cost alerts, all-tests table).
- `bi-evals compare A B` writes HTML regression diff with tiered verdict (🟢/🟡/🔴) and prompt-drift annotations; supports `latest` / `prev`.
- `bi-evals ui` starts a local FastAPI + Jinja viewer on `localhost:8765`: runs list (project filter, meta refresh, compare shortcuts, triage filters + "regressed since" badge), single-run view, and per-test drilldown (generated SQL, reference SQL, per-dimension reasons, files-read, full trace JSON).
- `bi-evals cost [--last-n N]` and `bi-evals flakiness [--last-n N] [--limit N]`.
- `bi-evals view` opens the Promptfoo web UI.
- **Adapter architecture**: `agent.adapter` selects an adapter from a registry. `api_endpoint` POSTs to the user's agent and scores the response; `anthropic_tool_loop` (dev-only) runs Claude + skill files locally for golden authoring. Both normalise into the canonical `AgentResult` contract the scorer consumes.
- Multi-model evaluation via `agent.anthropic_tool_loop.models: [...]` (dev adapter); per-model summary + scatter chart; drilldown model picker.
- Repeat-run variance via `--repeats N` or `scoring.repeats: N`; per-test pass rates and stddev.
- 10-dimension scorer with tiered/weighted pass-fail; `FileReaderTool` + `DescribeTableTool`; `SnowflakeClient`; `GoldenTest` with `last_verified_at` and `anti_patterns`.
- **372 unit tests passing, 0 warnings.** Strict YAML loading; old flat `agent:` configs rejected at load with a migration hint.

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`src/bi_evals/config.py`** — Pydantic config from `bi-evals.yaml`, `${ENV_VAR}` resolution with strict fail-fast, relative path resolution, automatic `.env` loading. Adapter-nested `AgentConfig` (Phase 2a): `agent.adapter` is a `Literal`; `api_endpoint`/`anthropic_tool_loop` config nest under blocks named for them; back-compat property accessors keep readers adapter-agnostic; old flat schema rejected with a migration hint.
- **`src/bi_evals/cli.py`** — Click CLI; `init` with `api_endpoint` (default) / `dev` subcommands.
- **`tests/test_config.py`**, **`tests/test_cli_init.py`** — config loading, env vars, dotenv, defaults, legacy-flat rejection, both init scaffolds.

### Phase 2: Tools + Adapters + Provider (response-evaluation architecture)

- **`src/bi_evals/provider/contract.py`** (PR #28) — the canonical contract: `AgentResult`, `TraceStep`, `extract_sql`, and the `Adapter` protocol. Neutral module that depends on no adapter; the scorer consumes only this shape.
- **`src/bi_evals/provider/registry.py`** (PR #28) — `build_adapter(config)` mirroring `db/factory.py`. `ApiEndpointAdapter` and `AnthropicToolLoopAdapter` (dev-only).
- **`src/bi_evals/provider/agent_loop.py`** — multi-turn Claude tool-calling loop with trace capture, SQL extraction, cost; re-exports contract types (shim). Marked dev-only.
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST adapter with configurable response keys (dot-notation), headers, optional trace capture.
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()`; resolves the adapter via the registry (no longer an `if/elif` on agent type) and writes trace JSON via `trace_paths`.
- **`src/bi_evals/tools/`** — `Tool` protocol, `FileReaderTool` (path-traversal protected), `DescribeTableTool`, registry.
- **`tests/test_provider_registry.py`**, **`test_agent_loop.py`**, **`test_api_endpoint.py`**, **`test_demo_routing.py`** (live API).

### Phase 3: Database + Golden Tests + 10-Dimension Scorer

- **`src/bi_evals/db/`** — `DatabaseClient` protocol, `SnowflakeClient`, factory.
- **`src/bi_evals/golden/`** — `GoldenTest` Pydantic model, YAML loaders.
- **`src/bi_evals/scorer/`** — sqlglot helpers (`sql_utils.py`), 10 dimension evaluators (`dimensions.py`), `get_assert()` entry point grading the per-(test, model) trace.
- **`tests/test_db.py`**, **`test_golden.py`**, **`test_scorer.py`**, **`test_anti_patterns.py`**, **`test_demo_scorer_phase_3.py`**.

### Phase 4: Promptfoo Bridge + `bi-evals run`

- **`src/bi_evals/promptfoo/bridge.py`** — translates config + goldens into `promptfooconfig.yaml`; one provider per model; package-root resolution works under editable + wheel installs.
- Tiered/weighted scoring: critical-dim gating + `pass_threshold`.
- **`tests/test_bridge.py`** (incl. wheel-install path regression test).

### Phase 5: Storage + Reporting + Regression Compare

- **`src/bi_evals/store/`** — DuckDB layer (schema, client with read-only + lock retry, idempotent ingest, frozen-dataclass queries).
- **`src/bi_evals/compare/diff.py`** — regression classifier + tiered verdict + prompt-drift annotations.
- **`src/bi_evals/report/`** — Jinja2 templates, inline CSS, no external URLs; `builder.py` does all data prep.
- `ingest`, `report`, `compare` CLI commands; auto-ingest at end of `run`.
- **`tests/test_store_*.py`**, **`test_compare_diff.py`**, **`test_report_builder.py`**, **`test_cli_report.py`**.

### Phase 6a–6d: Variance, Multi-Model, Drift, Staleness, Cost, Anti-Patterns

- Multi-model (`models` list), repeat-run variance (`repeats`), outcome stability, flakiness.
- Prompt drift (`runs.prompt_snapshot` SHA256, `prompt_diff`), dataset staleness (`last_verified_at`), cost alerts.
- Anti-patterns (`forbidden_tables`/`forbidden_columns`) as the 10th dimension; vacuous-dimension dropping in reports.
- Polish: strict YAML loading (PR #14), weighted-score rule in report (PR #15), bridge package-root fix (PR #19).
- **`tests/test_variance.py`**, **`test_multi_model.py`**, **`test_stability.py`**, **`test_prompt_drift.py`**, **`test_staleness.py`**, **`test_cost_alerts.py`**.

### Phase 7–7.5: Viewer (FastAPI + Jinja, intentionally throwaway)

- **`src/bi_evals/ui/`** — runs list, single-run view, per-test drilldown; project filter; meta-refresh; multi-row compare; triage filters + "regressed since" badge (PR #16).
- **`tests/test_ui.py`**.

### Phase 7.7: Plug-and-play infrastructure

- **PR #23** — `docs/byo-response-contract.md` + `src/bi_evals/byo_response_schema.json` (JSON Schema 2020-12) bundled in the package, loaded at runtime by `doctor`.
- **PR #24 / #26** — `bi-evals doctor` (synthetic health check now elicits SQL). **`tests/test_doctor.py`**.

### Phase 7.8: Harness engineering

- **PR #17** — CLAUDE.md "Current phase: MVP"; Claude Code hooks (ruff format, block edits under `results/`/`*.duckdb`, sync-`tmp/my-evals` reminder).
- **PR #25** — `docs/request-flow.md` end-to-end walkthrough + doc-honesty hook watching the nine files that drive the request flow (now incl. `provider/registry.py`, `provider/contract.py`).

### Response-evaluation pivot (in progress)

- **PR #28 (Step 1, merged)** — contract + adapter registry. One canonical `{generated_sql, trace}` contract; `Adapter` protocol + registry; import direction reversed so adapters depend on the contract, not each other. Behavior-neutral; the two-mode `if/elif` became a registry lookup.
- **PR #29 (Phase 2a, merged)** — adapter-nested config schema (clean break). `agent.type` → `agent.adapter`; nested adapter config blocks; driving adapter demoted off the public surface; `init` subcommands renamed `dev` / `api_endpoint`; `docs/migration-adapter-schema.md` + a load-time rejection of the old flat schema. Review fixes: `Literal` adapter type, model-required validator for the driving adapter, adapter-aware model fan-out in `run`.
- **PR #30** — archived superseded phase docs; added `docs/bi-eval-integration-analysis.md` (the response-evaluation thesis).

**Total: 372 unit tests passing, 0 warnings.**

---

## Remaining

### Phase 2b: Push adapter + `submit()` SDK (the default on-ramp)

The headline of the pivot and the first slice with a tangible "run it, see a report" payoff that needs no live agent or API spend.

- `bi-evals score --input results.jsonl` + a thin `submit()` SDK helper.
- A `PushReplayAdapter` registered like any other adapter: instead of producing a result, it replays the customer's submitted `{generated_sql, trace}` for each test, reusing the existing Promptfoo → scorer → ingest pipeline.
- Make push the `init` default; update README "Two modes" framing.

### Phase 2c: Capability check (open-envelope trace)

- Treat `trace` as an open envelope (customer over-captures; scorer reads what it understands).
- At score time, report which dimensions can be scored given what the submission contains; absent fields → `unknown`/skipped, surfaced explicitly, never silently failed. Doubles as the adoption ladder.

### Phase 2d: Model-as-request honesty marker

- `requested_model` / `actual_model` on the contract; report flags honored / violated / **unverifiable** so model A/B comparisons are never silently assumed faithful. A special case of 2c.
- Fold in the latent bridge cleanup (model fan-out should be adapter-aware in `bridge.py`, matching the `run` fix from 2a).

### Phase 8: COVID-19 Example Project

- Promote `tmp/my-evals/` → `examples/covid-19/` (no creds, `.env.example`, trimmed results, README walkthrough, 8–10 goldens, verified on a fresh clone).

### Follow-ups surfaced from recent reviews

- "9 dimensions" → "10 dimensions" inconsistency in `README.md` (pre-existing).
- `generated_sql` (trace JSON key) vs `extracted_sql` (Python field) naming inconsistency (deferred from PR #28 — touches the scorer/ingest contract).
- README "Two modes" section still describes the pre-pivot world; rework when 2b lands.
- Migrate `api_endpoint.py`'s manual `_get_nested` parsing to schema-based validation.

### Deferred (no committed phase yet)

- DuckDB as a built-in `database.type` (zero-cred demo target); `bi-evals init --from <dir>`; Snowflake SSO; additional warehouses (Postgres/BigQuery/Redshift/Databricks); `mcp-server` and `otel` adapters; SPA viewer rebuild; production-traffic golden import; OpenAI tool-loop adapter.

### Pillars 2 & 3 (post-MVP — see `docs/mvp-eval-platform.md`)

Pillar 1 (Accuracy + Explainability) is fully shipped. The next two pillars are out of MVP scope and not yet planned.

- **Pillar 2 Faithfulness** — LLM-as-judge decomposing NL responses into atomic claims and verifying each against the data. Trace capture (shipped) is the prerequisite.
- **Pillar 3 Confidence** — multi-trial pass@k/pass^k (groundwork from 6a), composite reliability score, graduation model, trust dashboard.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Response-evaluation, not reconstruction** | bi-evals scores the real agent's *response*; it never rebuilds or drives the customer's agent. Fidelity ("score the agent users hit") and "plug into any stack" turn out to be the same constraint. See `docs/bi-eval-integration-analysis.md` |
| **One contract, many adapters** | The contract is `{generated_sql, trace}`; the scorer executes the SQL itself, so customers never send result sets. `anthropic_tool_loop` / `api_endpoint` are adapters, not top-level "modes" |
| **Driving (`anthropic_tool_loop`) is dev-only** | Kept for authoring goldens before a real agent exists, but not a public product feature — it evaluates a local rebuild, not the production agent |
| **Push is the default on-ramp (planned)** | For a plug-in platform, asking a customer to run a script and submit results (network out, no infra) beats asking them to host an HTTP service. `api_endpoint` stays for agents that already are a clean HTTP service |
| **Clean schema break over back-compat shim** | Old flat `agent:` configs fail loudly at load with a migration hint rather than silently mis-parsing into adapter defaults |
| **`agent.adapter` is a `Literal`** | A typo'd adapter fails at config-load with a clear pydantic error, not later at dispatch |
| **Back-compat property accessors on `AgentConfig`** | `.type`/`.model`/`.endpoint`/`.tools` delegate into nested blocks so the ~8 reader modules stay adapter-agnostic through the schema break |
| Framework, not hardcoded project | Users bring their own skill files, golden tests, DB credentials |
| Python over original JS design | MVP doc described JS; Python chosen for consistency with data tooling |
| Provider owns the full tool loop (dev adapter) | Promptfoo's standard providers don't execute tool callbacks in a loop |
| File-based trace communication | Adapter writes JSON, scorer reads it — handles Promptfoo process isolation |
| Trace filenames per `(test, model, invocation)` | Avoids multi-model + multi-trial collisions; writer and reader compute the same name via `trace_paths.py` |
| Protocols over inheritance | `Tool`, `DatabaseClient`, `Adapter` use `typing.Protocol` for extensibility |
| Snowflake only for MVP | `DatabaseClient` protocol designed for adding Postgres/BigQuery later |
| sqlglot for SQL parsing | Handles Snowflake dialect, aliases, CTEs without regex |
| DuckDB for local store | Embedded, file-backed, zero infra; same SQL ports to Postgres |
| Golden metadata snapshotted at ingest | Editing a golden YAML never mutates historical runs |
| Tiered regression semantics | Critical dims can flip verdict red even if overall score masks failure |
| Auto-ingest whenever JSON exists, not just on exit-0 | The failure case is exactly when users want the report |
| Atomic observation is `(run, test, model, trial_ix)` | Pass rate + stddev rather than single-bit pass/fail |
| Anti-patterns non-critical by default | A violation that still produced correct rows is a warning, not a hard fail |
| Vacuously-passing dimensions dropped from report | A 100% pass from `"skipped:"` would dilute the scorecard |
| Viewer is intentionally throwaway | Jinja + FastAPI for now; the data layer (`store/queries.py`, `report/builder.py`) is the durable asset |
| Harness hooks block edits to `results/` and `*.duckdb` | Agent hand-edits there almost always indicate a fake-the-result attempt |
