# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is the test runner; all custom logic (provider/adapters, tools, scoring, storage, reporting, viewer) is Python. The MVP (Pillar 1: Accuracy + Explainability per `docs/mvp-eval-platform.md`) is complete and exceeded. The project is mid-way through a **response-evaluation architecture pivot** (`docs/bi-eval-integration-analysis.md`, `docs/pivot-phases.md`): the agent layer is now **one canonical contract `{generated_sql, trace}`, many adapters** — bi-evals scores the real agent's response and never rebuilds the agent. **Pivot Phases 1–3 are merged**; the push on-ramp has been validated end-to-end against a real agent + Snowflake.

> **Phase numbering.** The original MVP phases (Phase 1–7.8) are historical and shipped. The new architecture work is numbered separately as **Pivot Phase 1, 2, …** to avoid colliding with them.

What works today:

- **`bi-evals score --input results.jsonl`** — score a submission the customer's own agent produced (push adapter), no live agent and no API spend. Each row is `{golden_file, generated_sql | response_text, trace?}`; `response_text` (the agent's raw prose answer) is accepted and the SQL extracted from it. Validates the JSONL up front (per-row errors, missing/duplicate-golden detection).
- `bi-evals init api_endpoint` (default on-ramp) / `bi-evals init dev` (dev-only driving adapter) scaffold projects with the adapter-nested config shape; bare `bi-evals init` errors with a hint. The api_endpoint scaffold ships `adapter_example.py` (a FastAPI shim demonstrating the response contract).
- `bi-evals doctor` validates a project's runtime setup before a paid eval run. **api_endpoint**: synthetic POST + JSON Schema validation + scoring-coverage report. **push**: warehouse reachable + Promptfoo on PATH + submission file parses. **dev (anthropic_tool_loop)**: Anthropic API key reachable, system prompt + tool `base_dir`s exist, Snowflake `SELECT 1`, `npx` on PATH.
- `bi-evals run` runs the full eval end-to-end (live adapters) and auto-ingests into DuckDB; `--filter`, `--dry-run`, `--repeats N`, `--no-cache`, `--yes`, `--verbose`.
- `bi-evals ingest <path>` backfills existing eval JSON.
- `bi-evals report [--run-id ID]` writes a self-contained HTML report (filter strip, per-dimension failure reasons, category dashboard, weakest dimensions, weighted-score rule + per-test verdict, model summary + cost-vs-quality scatter, stability, freshness, cost alerts, all-tests table).
- `bi-evals compare A B` writes an HTML regression diff with tiered verdict (🟢/🟡/🔴) and prompt-drift annotations; supports `latest` / `prev`.
- `bi-evals ui` starts a local FastAPI + Jinja viewer: runs list (project filter, meta refresh, compare shortcuts, triage filters + "regressed since" badge), single-run view, per-test drilldown (generated SQL, reference SQL, per-dimension reasons, files-read, full trace JSON).
- `bi-evals cost [--last-n N]`, `bi-evals flakiness [--last-n N] [--limit N]`, `bi-evals view` (Promptfoo web UI).
- **Adapter architecture**: `agent.adapter` selects an adapter from a registry — `push` (replay submitted results), `api_endpoint` (POST to the live agent), `anthropic_tool_loop` (dev-only; runs Claude + skill files locally). All normalise into the canonical `AgentResult` the scorer consumes; the scorer is adapter-agnostic.
- **10-dimension scorer** with tiered/weighted pass-fail. Row matching is **position-tolerant**: when the agent names output columns differently from the golden's reference, rows match by ordinal position rather than falsely failing (the values are what matter, not the labels).
- Multi-model evaluation (dev adapter), repeat-run variance, `FileReaderTool` + `DescribeTableTool`, `SnowflakeClient`, `GoldenTest` with `last_verified_at` and `anti_patterns`.
- **410 unit tests passing, 0 warnings.** Strict YAML loading; old flat `agent:` configs rejected at load with a migration hint.

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`src/bi_evals/config.py`** — Pydantic config; `${ENV_VAR}` resolution with strict fail-fast; auto `.env` loading. Adapter-nested `AgentConfig` (Pivot Phase 2): `agent.adapter` is a `Literal` (`api_endpoint` | `anthropic_tool_loop` | `push`); each adapter's config nests under a block named for it; `push: PushConfig(input_file)`; back-compat property accessors keep readers adapter-agnostic; old flat schema rejected with a migration hint.
- **`src/bi_evals/cli.py`** — Click CLI; `init` (`api_endpoint` default / `dev`); `score`, `run`, `doctor`, `ingest`, `report`, `compare`, `ui`, `cost`, `flakiness`, `view`. `run`/`score` share the write-config → run → ingest tail via `_execute_eval`.
- **`tests/test_config.py`**, **`tests/test_cli_init.py`** — config loading, env vars, dotenv, defaults, legacy-flat rejection, both init scaffolds.

### Phase 2: Tools + Adapters + Provider (response-evaluation architecture)

- **`src/bi_evals/provider/contract.py`** — the canonical contract: `AgentResult`, `TraceStep`, `extract_sql`, the `Adapter` protocol. Neutral module; the scorer consumes only this shape.
- **`src/bi_evals/provider/registry.py`** — `build_adapter(config)` mirroring `db/factory.py`. `ApiEndpointAdapter`, `AnthropicToolLoopAdapter` (dev-only), and `PushReplayAdapter` (replays submitted rows; `_resolve_sql` handles `generated_sql`/`response_text` precedence; `_trace_from_row` normalises the open-envelope trace).
- **`src/bi_evals/provider/agent_loop.py`** — dev-only Claude tool loop; re-exports contract types (shim).
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST adapter with configurable response keys (dot-notation).
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()`; resolves the adapter via the registry; applies push overrides threaded through the provider block; writes trace JSON via `trace_paths`.
- **`src/bi_evals/tools/`** — `Tool` protocol, `FileReaderTool`, `DescribeTableTool`, registry.
- **`tests/test_provider_registry.py`**, **`test_push_adapter.py`**, **`test_agent_loop.py`**, **`test_api_endpoint.py`**, **`test_demo_routing.py`**.

### Phase 3: Database + Golden Tests + 10-Dimension Scorer

- **`src/bi_evals/db/`** — `DatabaseClient` protocol, `SnowflakeClient`, factory.
- **`src/bi_evals/golden/`** — `GoldenTest` Pydantic model, YAML loaders.
- **`src/bi_evals/scorer/`** — sqlglot helpers (`sql_utils.py`, incl. `extract_output_aliases`), 10 dimension evaluators (`dimensions.py`), `get_assert()` entry point. **Row matching is position-tolerant** (`_align_generated_rows`): name-match when columns align, ordinal-position fallback when the agent renamed them, honest failure on column-count mismatch.
- **`tests/test_db.py`**, **`test_golden.py`**, **`test_scorer.py`** (incl. `TestPositionFallbackMatching`), **`test_anti_patterns.py`**, **`test_demo_scorer_phase_3.py`**.

### Phase 4: Promptfoo Bridge + `bi-evals run`

- **`src/bi_evals/promptfoo/bridge.py`** — translates config + goldens into `promptfooconfig.yaml`; one provider per model; threads push overrides through the provider block; package-root resolution works under editable + wheel installs.
- Tiered/weighted scoring: critical-dim gating + `pass_threshold`.
- **`tests/test_bridge.py`** (incl. wheel-install path regression test).

### Phase 5: Storage + Reporting + Regression Compare

- **`src/bi_evals/store/`** — DuckDB layer (schema, client with read-only + lock retry, idempotent ingest, frozen-dataclass queries).
- **`src/bi_evals/compare/diff.py`** — regression classifier + tiered verdict + prompt-drift annotations.
- **`src/bi_evals/report/`** — Jinja2 templates, inline CSS, no external URLs.
- `ingest`, `report`, `compare` CLI; auto-ingest at end of `run`/`score`.
- **`tests/test_store_*.py`**, **`test_compare_diff.py`**, **`test_report_builder.py`**, **`test_cli_report.py`**.

### Phase 6a–6d: Variance, Multi-Model, Drift, Staleness, Cost, Anti-Patterns

- Multi-model, repeat-run variance, outcome stability, flakiness.
- Prompt drift (`runs.prompt_snapshot`), dataset staleness (`last_verified_at`), cost alerts.
- Anti-patterns (`forbidden_tables`/`forbidden_columns`) as the 10th dimension; vacuous-dimension dropping.
- Polish: strict YAML loading (PR #14), weighted-score rule (PR #15), bridge package-root fix (PR #19).
- **`tests/test_variance.py`**, **`test_multi_model.py`**, **`test_stability.py`**, **`test_prompt_drift.py`**, **`test_staleness.py`**, **`test_cost_alerts.py`**.

### Phase 7–7.5: Viewer (FastAPI + Jinja, intentionally throwaway)

- **`src/bi_evals/ui/`** — runs list, single-run view, per-test drilldown; project filter; meta-refresh; multi-row compare; triage filters + "regressed since" badge (PR #16). **`tests/test_ui.py`**.

### Phase 7.7–7.8: Plug-and-play + Harness

- **PR #23** — `docs/byo-response-contract.md` + bundled JSON Schema, loaded at runtime by `doctor`.
- **PR #24 / #26** — `bi-evals doctor`. **`tests/test_doctor.py`**.
- **PR #17** — CLAUDE.md "Current phase: MVP"; Claude Code hooks (ruff format, block edits under `results/`/`*.duckdb`, sync-`tmp/my-evals` reminder).
- **PR #25** — `docs/request-flow.md` + doc-honesty hook watching the files that drive the request flow.

### Response-evaluation pivot — Phases 1–3 (merged)

- **Pivot Phase 1 — contract + adapter registry** (PR #28). One canonical `{generated_sql, trace}` contract; `Adapter` protocol + registry; import direction reversed; the two-mode `if/elif` became a registry lookup. Behavior-neutral.
- **Pivot Phase 2 — adapter-nested config schema** (PR #29; clean break). `agent.type` → `agent.adapter`; nested adapter blocks; driving demoted off the public surface; `init` renamed `dev`/`api_endpoint`; `docs/migration-adapter-schema.md`; load-time rejection of the old flat schema. Review fixes: `Literal` adapter, model-required validator, adapter-aware fan-out.
- **Pivot Phase 3 — push adapter** (PR #34). `bi-evals score --input`; `PushReplayAdapter` replays submitted rows through the existing scorer/ingest pipeline (untouched); push overrides threaded across the Promptfoo fork boundary. **`tests/test_push_adapter.py`**.
  - **`response_text` refinement** (PR #36) — accept the agent's raw prose answer and extract the SQL (mirrors `api_endpoint`'s sql-key/text-key split); `_resolve_sql` shared by validation and runtime.
  - **Scorer position-tolerant matching** (PR #37) — found by running the demo against a real Snowflake agent: row matching keyed by column *name*, so a correct answer with renamed output columns falsely failed. Now falls back to ordinal position; `column_alignment` gained an alias-vs-source authoring hint.
- **Promptfoo research** (PRs #30/#33/#35) — `docs/bi-eval-integration-analysis.md` (the thesis) + verbatim-sourced findings in `docs/pivot-phases.md` validating response-evaluation and settling the "should bi-evals orchestrate the agent?" question (no — OTel/observe the real agent).

**Validated end-to-end:** the push path was run against `mock-bi-agent` (a FastAPI TPCH agent) + real Snowflake `SNOWFLAKE_SAMPLE_DATA.TPCH_SF10`; that run is what surfaced the PR #37 scorer bug. All three real goldens now pass.

**Total: 410 unit tests passing, 0 warnings.**

---

## Remaining

### Pivot Phase 4: Capability check (open-envelope trace)

- Treat `trace` as an open envelope (customer over-captures; scorer reads what it understands).
- At score time, report which dimensions can be scored given what the submission contains; absent fields → `unknown`/skipped, surfaced explicitly, never silently failed. (Motivated firsthand: a `skill_path_correctness` failure in the demo was really "no trace submitted" — Phase 4 would say so up front.) Doubles as the adoption ladder.

### Pivot Phase 5: Model-as-request honesty marker

- `requested_model` / `actual_model` on the contract; report flags honored / violated / **unverifiable** so model A/B comparisons are never silently assumed faithful. A special case of Pivot Phase 4.
- Fold in the latent bridge cleanup (model fan-out should be adapter-aware in `bridge.py`, matching the `run` fix from Pivot Phase 2).

### Pivot Phase 6: OTel adapter (ingest spans the real agent emits)

- Lowest-customer-effort, highest-fidelity adapter; the path the research flagged as ecosystem-aligned. The real agent emits OTel GenAI spans; bi-evals consumes them onto the canonical contract. May lean on Promptfoo's existing OTLP receiver + `trajectory:` assertions rather than net-new infra. Reference SQL still executes on bi-evals' own connection.

### Onboarding polish (deferred from Pivot Phase 3)

- `init push` scaffold (today push uses an `api_endpoint`/hand-written config; `score` forces push regardless).
- Make push the documented default on-ramp; rework the README "Two modes" section (still describes the pre-pivot world).
- `submit()` SDK helper (a `Runner` that yields golden questions and collects submissions).
- Promote `demo-bi-evals-snowflake` (now a proven, working end-to-end demo) into a committed `examples/` reference project.

### Follow-ups surfaced from recent reviews

- "9 dimensions" → "10 dimensions" inconsistency in `README.md` (pre-existing).
- `generated_sql` (trace JSON key) vs `extracted_sql` (Python field) naming inconsistency (deferred from PR #28 — touches the scorer/ingest contract).
- Migrate `api_endpoint.py`'s manual `_get_nested` parsing to schema-based validation.

### Deferred (no committed phase yet)

- DuckDB as a built-in `database.type` (zero-cred demo target); `bi-evals init --from <dir>`; Snowflake SSO; additional warehouses (Postgres/BigQuery/Redshift/Databricks); `mcp-server` adapter; SPA viewer rebuild; production-traffic golden import; OpenAI tool-loop adapter; in-process Python wrap adapter (for importable agents, à la Promptfoo's ADK pattern).

### Pillars 2 & 3 (post-MVP — see `docs/mvp-eval-platform.md`)

Pillar 1 (Accuracy + Explainability) is fully shipped; the next two pillars are out of MVP scope and not yet planned.

- **Pillar 2 Faithfulness** — LLM-as-judge decomposing NL responses into atomic claims and verifying each against the data. Trace capture (shipped) is the prerequisite.
- **Pillar 3 Confidence** — multi-trial pass@k/pass^k (groundwork from 6a), composite reliability score, graduation model, trust dashboard.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **bi-evals is an offline eval tool** | It scores a curated golden suite in CI to gate regressions before ship — not live production traffic. Online eval (scoring real traffic) needs a reference-free scorer (Pillar 2 Faithfulness) we haven't built, and live SQL questions have no ground truth. The buying wedge is the CI gate, not production monitoring. See `docs/eval-landscape.md` |
| **Golden ground truth is hand-authored by SMEs** | The `reference_sql` that makes a golden trustworthy must be written and verified by a human — you can't harvest a verified-correct answer from production, because production is the agent's *output* (the thing under suspicion). Production can suggest *which questions* are worth testing; it can never hand you the answer key. So the friction to attack is **fast hand-authoring** (scaffolders, reference-SQL validation, clear errors), not auto-generating goldens. Production-trace harvesting (scrape candidate questions from logs) is a *future convenience*, not a way to remove the human |
| **Response-evaluation, not reconstruction** | bi-evals scores the real agent's *response*; never rebuilds or drives the agent. Fidelity ("score the agent users hit") and "plug into any stack" are the same constraint. `docs/bi-eval-integration-analysis.md` |
| **One contract, many adapters** | The contract is `{generated_sql, trace}`; the scorer executes the SQL itself, so customers never send result sets. `push`/`api_endpoint`/`anthropic_tool_loop` are adapters, not top-level "modes" |
| **Push reuses Promptfoo, doesn't bypass it** | `PushReplayAdapter` replays submitted rows through the existing provider→scorer→ingest path, so the whole downstream pipeline (scorer, report, ui) is unchanged |
| **Accept `response_text`, not just clean SQL** | Real agents emit SQL fenced/in prose; the contract is a *target shape* and the customer's mapping to it is the work. `generated_sql` wins on conflict; `response_text` is extracted; no-SQL fails the row clearly |
| **Position-tolerant row matching** | A correct answer with differently-named output columns must not fail. The scorer matches by name when columns align, else by ordinal position — scoring substance (values, in order) not surface (labels). The thing the agent controls (naming) can't be a false-failure source |
| **Driving (`anthropic_tool_loop`) is dev-only** | Kept for authoring goldens before a real agent exists; not a public feature — it evaluates a local rebuild. Verbatim Promptfoo research confirms the field's pattern is observe-the-real-agent, never reconstruct |
| **The `submit()` SDK is the default on-ramp (planned)** | Sorted by *adoption friction*, raw-file push is actually the highest-effort build-it path (hand-build the loop + reshape + JSONL). The `bi_evals.Runner` SDK makes that plumbing the framework's job — the customer writes one `ask()` call — so it's the front door. Raw-file `score --input` is the logs-only/non-Python fallback; `api_endpoint` suits agents already exposed as a clean HTTP service; OTel is the high-fidelity future path. See `docs/eval-landscape.md` |
| **Clean schema break over back-compat shim** | Old flat `agent:` configs fail loudly with a migration hint rather than silently mis-parsing |
| **`agent.adapter` is a `Literal`** | A typo'd adapter fails at config-load with a clear pydantic error, not later at dispatch |
| **Back-compat property accessors on `AgentConfig`** | `.type`/`.model`/`.endpoint`/`.tools` delegate into nested blocks so reader modules stay adapter-agnostic through the schema break |
| Framework, not hardcoded project | Users bring their own skill files, golden tests, DB credentials |
| Python over original JS design | MVP doc described JS; Python chosen for consistency with data tooling |
| File-based trace communication | Adapter writes JSON, scorer reads it — handles Promptfoo process isolation |
| Trace filenames per `(test, model, invocation)` | Avoids multi-model + multi-trial collisions; writer/reader compute the same name via `trace_paths.py` |
| Protocols over inheritance | `Tool`, `DatabaseClient`, `Adapter` use `typing.Protocol` |
| sqlglot for SQL parsing | Handles Snowflake dialect, aliases, CTEs without regex |
| DuckDB for local store | Embedded, file-backed, zero infra; SQL ports to Postgres |
| Golden metadata snapshotted at ingest | Editing a golden never mutates historical runs |
| Tiered regression semantics | Critical dims can flip verdict red even if overall score masks failure |
| Auto-ingest whenever JSON exists | The failure case is exactly when users want the report |
| Anti-patterns non-critical by default | A violation that still produced correct rows is a warning, not a hard fail |
| Viewer is intentionally throwaway | Jinja + FastAPI now; the data layer (`store/queries.py`, `report/builder.py`) is the durable asset |
| Verify quotes before citing | WebFetch/WebSearch summaries are interpretation; pull verbatim source text before quoting in a durable doc (corrected twice in the Promptfoo research) |
