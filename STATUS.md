# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is the test runner; all custom logic (provider/adapters, tools, scoring, storage, reporting, viewer) is Python. The MVP (Pillar 1: Accuracy + Explainability per `docs/mvp-eval-platform.md`) is complete and exceeded. The project has completed the **response-evaluation architecture pivot** (`docs/plans/pivot-phase-1-integration-analysis.md`, `docs/plans/pivot-phases-overview.md`): the agent layer is **one canonical contract `{generated_sql, trace}`, many adapters** — bi-evals scores the real agent's response and never rebuilds the agent. The framework tagged its first release, **v0.1.0** (2026-06-16), the README leads with the SDK on-ramp, **Build Stage 1 (CI regression gating) is merged (PR #46)** — `compare` and the SDK can now fail a build on regressions or a pass-rate floor — and **Build Stage 2 (capability check) is merged (PR #47)** — the scorer now distinguishes "couldn't know" from "know it failed" for dimensions the submission lacks data for, with pre-flight warnings and a report Capability panel. A follow-on fix (**PR #48**) generalized the CTE-mangling bug Stage 2's live testing surfaced: SQL extraction from prose now validates candidates with sqlglot's real parser instead of shape-specific regexes.

> **Phase numbering.** The original MVP phases (Phase 1–7.8) are historical and shipped. The response-evaluation architecture work is numbered separately as **Pivot Phase 1, 2, …** to avoid colliding with them.

What works today:

- **`bi_evals.Runner` SDK** (`import bi_evals`) — the primary on-ramp. `runner.golden_cases()` yields questions, `runner.submit(case, generated_sql=..., trace=...)` records answers, `runner.score()` runs the push pipeline and returns an assertable `RunReport` (`pass_rate`, `failures`, truthy iff `failed == 0`). `Runner(verbose=True)` streams progress (loaded/submitted/scored heartbeats) to stderr — no logging setup required.
- **`bi-evals score --input results.jsonl`** — score a submission the customer's own agent produced (push adapter), no live agent and no API spend. Each row is `{golden_file, generated_sql | response_text, trace?}`; `response_text` (the agent's raw prose answer) is accepted and the SQL extracted from it. Validates the JSONL up front (per-row errors, missing/duplicate-golden detection). The SDK's `score()` is a thin wrapper over the same `runner_core.run_push_score` the CLI uses — one pipeline, two front doors.
- `bi-evals init push` (the default on-ramp; Snowflake-only `.env`, next steps point at the SDK) / `init api_endpoint` (ships `adapter_example.py`, a FastAPI shim demonstrating the response contract) / `init dev` (dev-only driving adapter) scaffold projects with the adapter-nested config shape; bare `bi-evals init` errors with a hint listing push first.
- `bi-evals doctor` validates a project's runtime setup before a paid eval run. **api_endpoint**: synthetic POST + JSON Schema validation + scoring-coverage report. **push**: warehouse reachable + Promptfoo on PATH + submission file parses. **dev (anthropic_tool_loop)**: Anthropic API key reachable, system prompt + tool `base_dir`s exist, Snowflake `SELECT 1`, `npx` on PATH.
- `bi-evals run` runs the full eval end-to-end (live adapters) and auto-ingests into DuckDB; `--filter`, `--dry-run`, `--repeats N`, `--no-cache`, `--yes`, `--verbose`.
- `bi-evals ingest <path>` backfills existing eval JSON.
- `bi-evals report [--run-id ID]` writes a self-contained HTML report (filter strip, per-dimension failure reasons, category dashboard, weakest dimensions, weighted-score rule + per-test verdict, model summary + cost-vs-quality scatter, stability, freshness, cost alerts, all-tests table).
- `bi-evals compare BASELINE CANDIDATE` writes an HTML regression diff with tiered verdict (🟢/🟡/🔴) and prompt-drift annotations; supports `prev` / `latest` (baseline first: `compare prev latest`).
- **CI regression gating** (PR #46, merged) — `compare --fail-on [red|amber|never]` exits 1 on gate failure; SDK `report.passed_gate` (absolute `min_pass_rate` floor, no baseline needed) and `report.compare_to("prev") → GateResult` for CI assertions. Knobs on `compare:` config (`min_pass_rate`, `max_regressions_allowed` flaky budget, `fail_on`; unset = gating off, defaults are no-ops). When gating is enabled, the compare page (CLI artifact + ui view) renders a **gate strip**: passed/FAILED, the policy level, the floor/budget reasons, and the regressed tests by name. One shared engine (`compare/gate.py: evaluate_gate`) behind both surfaces.
- `bi-evals ui` starts a local FastAPI + Jinja viewer: runs list (project filter, meta refresh, compare shortcuts, triage filters + "regressed since" badge), single-run view, per-test drilldown (generated SQL, reference SQL, per-dimension reasons, files-read, full trace JSON).
- `bi-evals cost [--last-n N]`, `bi-evals flakiness [--last-n N] [--limit N]`, `bi-evals view` (Promptfoo web UI).
- **Adapter architecture**: `agent.adapter` selects an adapter from a registry — `push` (replay submitted results), `api_endpoint` (POST to the live agent), `anthropic_tool_loop` (dev-only; runs Claude + skill files locally). All normalise into the canonical `AgentResult` the scorer consumes; the scorer is adapter-agnostic.
- **10-dimension scorer** with tiered/weighted pass-fail. Row matching is **position-tolerant**: when the agent names output columns differently from the golden's reference, rows match by ordinal position rather than falsely failing (the values are what matter, not the labels).
- Multi-model evaluation (dev adapter), repeat-run variance, `FileReaderTool` + `DescribeTableTool`, `SnowflakeClient`, `GoldenTest` with `last_verified_at` and `anti_patterns`.
- **493 unit tests passing, 0 warnings.** Strict YAML loading; old flat `agent:` configs rejected at load with a migration hint.

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`src/bi_evals/config.py`** — Pydantic config; `${ENV_VAR}` resolution with strict fail-fast; auto `.env` loading. Adapter-nested `AgentConfig` (Pivot Phase 2): `agent.adapter` is a `Literal` (`api_endpoint` | `anthropic_tool_loop` | `push`); each adapter's config nests under a block named for it; `push: PushConfig(input_file)`; back-compat property accessors keep readers adapter-agnostic; old flat schema rejected with a migration hint.
- **`src/bi_evals/cli.py`** — Click CLI; `init` (`api_endpoint` default / `dev`); `score`, `run`, `doctor`, `ingest`, `report`, `compare`, `ui`, `cost`, `flakiness`, `view`. `run`/`score` share the write-config → run → ingest tail via `_execute_eval`, which now delegates through `runner_core` (shared with the SDK).
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

### Pivot Phase 3.5 — SDK on-ramp (merged)

- **`src/bi_evals/runner_core.py`** — extracted the write-config → run Promptfoo → ingest tail shared by the CLI (`_execute_eval`) and the SDK, so the two front doors can never diverge (`PushScoreError`, `run_push_score`).
- **`src/bi_evals/sdk.py`** — `bi_evals.Runner`: `golden_cases()` yields `Case`s honoring `filter`; `submit()` records exactly one of `generated_sql`/`response_text`/`error` per case (duplicate-submission guarded); `score()` writes a kept `results/sdk_<ts>.jsonl`, runs the push pipeline, and returns `RunReport` (`pass_rate`, `failures: list[TestResult]`, `__bool__` true iff no failures). `Runner(verbose=True)` attaches an idempotent stderr handler for progress heartbeats (loaded/submitted/done) independent of the caller's own logging config.
- README restructured SDK-first: "Getting started with the SDK" now leads onboarding; the old adapter-only "Two modes" framing became "Adapters: how your agent's answers reach the scorer" (push/api_endpoint/dev all documented as adapters, not top-level modes).
- **`tests/test_sdk.py`**, **`tests/test_runner_core.py`**.
- PR #40 review fixes folded in directly (no separate follow-up PR needed).

### Build Stage 1 — CI regression gating (PR #46, merged)

Implements `docs/plans/build-stage-1-regression-gating.md` — one shared gate engine behind both surfaces, so CLI and SDK verdicts cannot diverge (the `runner_core` principle applied to gating).

- **`src/bi_evals/compare/gate.py`** — `evaluate_gate` (pure): absolute floor (`min_pass_rate`, works with no baseline — safe on a first-ever run), baseline regression with a `max_regressions_allowed` flaky-suite budget, `fail_on` level (`red`/`amber`/`never` report-only). `GateResult` records the policy it was evaluated under (`fail_on` field); truthy iff passed. `classify_runs` shares one `classify_pairs` computation between the HTML report and the gate.
- **CLI** — `compare BASELINE CANDIDATE --fail-on [...]` prints verdict + reasons, exits 1 on gate failure; flag overrides `compare.fail_on` config; no gating configured → informational exit-0 (strict no-op defaults — deviation from the plan's config block, per its own Decisions section). Run-ref resolution moved to `store/queries.resolve_run_ref` (shared with the SDK).
- **SDK** — `report.passed_gate` (floor only) + `report.compare_to("prev") → GateResult` (rejects a baseline resolving to the run itself). `GateResult` exported from `bi_evals`.
- **Gate strip on the compare page** — when gating is enabled, the CLI artifact and the `bi-evals ui` `/compare` view render the gate outcome under the verdict banner (passed/FAILED, `fail_on` level, floor/budget reasons, regressed tests by name). Decision "HTML stays report-verdict only" amended in the plan doc after the first live demo (an amber banner next to a floor-failed build was unreadable). Non-gating compares render byte-identical. The artifact freezes what CI saw; the ui recomputes from current config; gate outcomes are not persisted in the store.
- **Docs** — `getting-started.md` Step 8 (CI recipe + the multi-trial `scoring.repeats` flakiness recipe); fixed the inverted `compare latest prev` examples (the diff engine treats run A as baseline — the old order reported improvements as regressions).
- **Verified live** — `tmp/my-evals` (floor breach → exit 1) and `demo-bi-evals-snowflake` (real regression pair → red gate; healthy pair → green), both with `compare:` blocks demoing the knobs.
- **`tests/test_gate.py`** (floor/budget/fail_on matrix + SDK gate surface) + CLI exit-code and ui gate-strip tests.

### Build Stage 2 — Capability check (PR #47, merged)

Implements `docs/plans/build-stage-2-capability-check.md`. Core principle: when bi-evals cannot score a dimension it says "I can't know" — never "I know it failed."

- **`src/bi_evals/scorer/capability.py`** (new) — `DimensionStatus` (`pass`/`fail`/`skipped`/`not_evaluated`); a trace-usability classifier distinguishing *absent* / *present-but-unusable* / *usable*; the NE-1/NE-2/CASCADE-1 message catalogue (each names the exact unlock action); `trace_coverage`/`coverage_warning` for pre-flight. Pure — single source of truth for the scorer, pre-flight, `doctor`, and the report.
- **Scorer** — `skill_path_correctness` with no usable trace is `NOT_EVALUATED` (not a failure); a usable trace with the wrong tools still genuinely fails. `aggregate_results` (extracted pure from `get_assert`) excludes `not_evaluated`/`skipped` from the weighted score entirely (**decision D2** — vacuous skips no longer pad the score, a behavior change); a **critical** dimension that cannot be evaluated fails the test with a distinct reason (**decision D1**). Upstream cascade failures (execution failed → row dims) now say so honestly ("failed upstream: …") instead of the old misleading "skipped: SQL execution failed".
- **Store** — `dimension_results.status` column (auto-migrated, nullable, no backfill per **decision D3**: historical rows keep boolean-only semantics). `not_evaluated` **and** `skipped` rows are excluded from both `dimension_pass_rates()` and `_dims_by_test()` (fixed post-review — the first pass only excluded `not_evaluated`, leaving vacuous skips still padding the run-level dimension table and compare's regression detection; `tests/test_capability.py::test_skipped_rows_excluded_from_run_level_aggregates` covers the partial-skip case). Unknown dims are *absent* from the compare diff, not zero — adding/dropping a trace never misreads as a fix/regression.
- **Report** — a **Capability panel** (only rendered when something wasn't evaluable) lists per-dimension evaluated/not-evaluated counts and the unlock hint; NE dims no longer appear in the failures section.
- **Pre-flight** — `runner_core.preflight_capability_warnings`, shared by the `score` CLI and the SDK; `doctor`'s push checks gained a Trace-coverage line. All before any warehouse spend.
- **Live-testing found a pre-existing bug**: `extract_sql`'s bare-SELECT fallback stripped the `WITH` prefix off CTEs, sending broken SQL to the warehouse whenever `generated_sql` was a CTE. First patched narrowly (`provider/contract.py`, `provider/registry.py`), then generalized in PR #48 (below) after a second live-test pass found the narrow regex still missed `WITH RECURSIVE`.
- **`tests/test_capability.py`** (28 tests: classifier, D1/D2 aggregation, ingest status via explicit key and prefix fallback, run-level aggregate exclusion incl. partial-skip, capability panel render, pre-flight matrix, end-to-end `get_assert` NE-1/NE-2/genuine-fail).

### SQL extraction generalization (PR #48, merged)

Follow-on to Stage 2's CTE bugfix. The first fix special-cased `WITH <name> AS (` — a second live-test pass found it still missed `WITH RECURSIVE` (two tokens between `WITH` and `AS`, not one). Rather than patch the regex again, `extract_sql` (`provider/contract.py`) was rewritten to separate concerns: find candidate start positions with a plain `WITH`/`SELECT` keyword search, then ask **sqlglot** (already a dependency, `error_level=RAISE`) whether the text from there parses as one clean statement — whichever candidate parses first, wins. This is a strict behavioral superset of the regex approach: `WITH RECURSIVE` and multi-CTE queries parse with no special-casing, and SQL subtly broken by prose glued onto a real clause (e.g. `"SELECT x FROM t because that's the total"`) is now correctly rejected rather than silently mangled and sent to the warehouse. Confirmed empirically that sqlglot's lenient `ErrorLevel.IGNORE` mode is unsafe for this (it silently misparses prose as SQL rather than rejecting it) — `RAISE` + trim-and-retry is the only safe combination, documented in-code. `registry.py`'s verbatim-SQL bypass (a workaround for the original bug) was deleted since clean SQL now round-trips through `extract_sql` on its own; one side effect, a trailing `;` on already-clean `generated_sql` is now stripped rather than preserved, noted in `resolve_sql`'s docstring (harmless — every supported warehouse accepts SQL either way).

- **`tests/test_push_adapter.py`** — `TestSqlglotValidatedExtraction` (7 tests: `WITH RECURSIVE`, multi-CTE, trailing-prose trimming, prose-glued-without-semicolon rejection, prose-before-query, two-statements-only-first-extracted) alongside the 5 CTE regression tests from PR #47's fix (all still pass unchanged — confirms the rewrite is a superset, not a replacement).

### Housekeeping

- **v0.1.0 tagged** (2026-06-16) — first versioned baseline; `CHANGELOG.md` added (Keep a Changelog format); `pyproject.toml` version now dynamic via `hatch.version`.
- **Promptfoo research** (PRs #30/#33/#35) — `docs/plans/pivot-phase-1-integration-analysis.md` (the thesis) + verbatim-sourced findings in `docs/plans/pivot-phases-overview.md` validating response-evaluation and settling the "should bi-evals orchestrate the agent?" question (no — OTel/observe the real agent).

**Validated end-to-end:** the push path was run against `mock-bi-agent` (a FastAPI TPCH agent) + real Snowflake `SNOWFLAKE_SAMPLE_DATA.TPCH_SF10`; that run is what surfaced the PR #37 scorer bug. All three real goldens now pass. Build Stage 2's capability check was validated the same way against `tmp/my-evals` (see plan doc + PR #47 for the scenario matrix), and the same CTE golden was re-verified end-to-end against Snowflake after PR #48's generalized extraction fix.

**Total: 493 unit tests passing, 0 warnings.**

---

## Remaining — Build Stages

One ordered backlog. Earlier stages are prerequisites for later ones only where noted; otherwise the order reflects priority against the MVP north star, not a hard dependency chain.

### Build Stage 1: CI regression gating — ✅ implemented (PR #46, merged)

- Done; see the "Build Stage 1" entry under **Completed**. Deferred from its plan doc (still open): `stddev`-based significance testing, per-test `repeats`, a shared remote store for CI baselines, and a committed GitHub Actions workflow example.

### Build Stage 2: Capability check (open-envelope trace) — ✅ implemented (PR #47, merged)

- Done; see the "Build Stage 2" entry under **Completed**. Unblocks Build Stage 3.

### Build Stage 3: OTel — SDK trace correlation + batch-ingest adapter

- `docs/plans/build-stage-3-otel.md` (new, design-complete) — split into two independently-shippable parts after review found the original one-line framing overpromised ("zero customer changes") what an *offline* eval tool can actually deliver, since bi-evals never calls the agent and so can't tag a request's trace after the fact.
  - **Part 1 — `Runner.traced_call()`**: a small SDK context manager tagging the customer's own OTel span with `bi_evals.golden_id`, for customers who already run OTel and want a failing bi-evals row to correlate back into their own trace dashboard. Reuses `submit(trace=...)` (already exists) for actually getting trace data to bi-evals — no scoring-path change at all.
  - **Part 2 — file-based OTLP batch-ingest adapter** (`agent.adapter: otel`): for the narrower case of an agent that ran independently of any bi-evals-authored loop. Mirrors `PushReplayAdapter` exactly — new parser + new adapter, same canonical contract, scorer/report/compare untouched.
  - A live-receiver design (bi-evals standing up an OTLP endpoint mid-run, mirroring Promptfoo's own tracing feature) was considered and rejected — it requires bi-evals to be in the agent's request path at call time, which breaks the "offline eval tool, not live traffic" decision below.
- Not started (design only). Build Stage 2 (its dependency) is merged — unblocked, next in line. Ship Part 1 first; Part 2 only once real usage confirms the file-ingestion case is needed.

### Build Stage 4: Onboarding polish

- ~~`init push` scaffold~~ — already shipped (Pivot Phase 3.5); `init push` is the listed default on-ramp today.
- Promote `demo-bi-evals-snowflake` (now a proven, working end-to-end demo — including the CI gate) into a committed `examples/` reference project.
- CI recipes doc + committed GitHub Actions workflow example (baseline-via-cache pattern; surfaced by user questions after Build Stage 1 shipped).
- Not started.

### Build Stage 5: Semantic-layer scoring

- `docs/plans/build-stage-5-semantic-layer-scoring.md` (new, unreleased) — closes the "right for the right reason" gap: today's dimensions can pass on a coincidentally-correct result even when the wrong metric/dimension/grain was selected. Proposes a canonical `SemanticQuery` envelope + per-vendor `SemanticLayerParser` (dbt Semantic Layer / Snowflake Semantic Views / Cube), new opt-in golden field `expected_semantic`, and four new dimensions (`metric_selection`, `dimension_selection`, `semantic_grain_correctness`, `semantic_filter_correctness`) plus `metric_definition_integrity` for semantic-drift detection. Sequencing starts with Snowflake (semantic selection parseable straight out of `generated_sql`, zero new agent instrumentation).
- Design only — no code yet. Builds on Build Stage 2's open envelope. Explicitly out of MVP scope per `CLAUDE.md` — lowest priority of the numbered stages.

### Build Stage 6: Small fixes and cleanups

- ~~"9 dimensions" → "10 dimensions" inconsistency in `README.md`~~ — resolved in the SDK-first README rewrite (verified: README says 10, lists 10).
- `generated_sql` (trace JSON key) vs `extracted_sql` (Python field) naming inconsistency (touches the scorer/ingest contract).
- Migrate `api_endpoint.py`'s manual `_get_nested` parsing to schema-based validation.
- `push-limitations.md` §D slightly overstates cost handling ("unless your submission carries them" — the push adapter zeroes cost/tokens regardless; one-word fix).

### Build Stage 7: Deferred / unscheduled

No committed order yet within this stage; pull items forward into Stages 1–6 as they become priorities:

- DuckDB as a built-in `database.type` (zero-cred demo target)
- `bi-evals init --from <dir>`
- Snowflake SSO
- additional warehouses (Postgres/BigQuery/Redshift/Databricks)
- `mcp-server` adapter
- SPA viewer rebuild
- production-traffic golden import
- OpenAI tool-loop adapter
- in-process Python wrap adapter (for importable agents, à la Promptfoo's ADK pattern)

### Build Stage 8: Pillars 2 & 3 (post-MVP — see `docs/mvp-eval-platform.md`)

Pillar 1 (Accuracy + Explainability) is fully shipped; the next two pillars are out of MVP scope and not yet planned. Lowest priority overall — sequenced last deliberately.

- **Pillar 2 Faithfulness** — LLM-as-judge decomposing NL responses into atomic claims and verifying each against the data. Trace capture (shipped) is the prerequisite.
- **Pillar 3 Confidence** — multi-trial pass@k/pass^k (groundwork from 6a), composite reliability score, graduation model, trust dashboard.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **bi-evals is an offline eval tool** | It scores a curated golden suite in CI to gate regressions before ship — not live production traffic. Online eval (scoring real traffic) needs a reference-free scorer (Pillar 2 Faithfulness) we haven't built, and live SQL questions have no ground truth. The buying wedge is the CI gate, not production monitoring. See `docs/plans/eval-landscape-strategy.md` |
| **Golden ground truth is hand-authored by SMEs** | The `reference_sql` that makes a golden trustworthy must be written and verified by a human — you can't harvest a verified-correct answer from production, because production is the agent's *output* (the thing under suspicion). Production can suggest *which questions* are worth testing; it can never hand you the answer key. So the friction to attack is **fast hand-authoring** (scaffolders, reference-SQL validation, clear errors), not auto-generating goldens. Production-trace harvesting (scrape candidate questions from logs) is a *future convenience*, not a way to remove the human |
| **Response-evaluation, not reconstruction** | bi-evals scores the real agent's *response*; never rebuilds or drives the agent. Fidelity ("score the agent users hit") and "plug into any stack" are the same constraint. `docs/plans/pivot-phase-1-integration-analysis.md` |
| **One contract, many adapters** | The contract is `{generated_sql, trace}`; the scorer executes the SQL itself, so customers never send result sets. `push`/`api_endpoint`/`anthropic_tool_loop` are adapters, not top-level "modes" |
| **Push reuses Promptfoo, doesn't bypass it** | `PushReplayAdapter` replays submitted rows through the existing provider→scorer→ingest path, so the whole downstream pipeline (scorer, report, ui) is unchanged |
| **Accept `response_text`, not just clean SQL** | Real agents emit SQL fenced/in prose; the contract is a *target shape* and the customer's mapping to it is the work. `generated_sql` wins on conflict; `response_text` is extracted; no-SQL fails the row clearly |
| **Position-tolerant row matching** | A correct answer with differently-named output columns must not fail. The scorer matches by name when columns align, else by ordinal position — scoring substance (values, in order) not surface (labels). The thing the agent controls (naming) can't be a false-failure source |
| **Driving (`anthropic_tool_loop`) is dev-only** | Kept for authoring goldens before a real agent exists; not a public feature — it evaluates a local rebuild. Verbatim Promptfoo research confirms the field's pattern is observe-the-real-agent, never reconstruct |
| **The `submit()` SDK is the default on-ramp** | Sorted by *adoption friction*, raw-file push is actually the highest-effort build-it path (hand-build the loop + reshape + JSONL). `bi_evals.Runner` makes that plumbing the framework's job — the customer writes one `ask()` call — so it's the front door (shipped in Pivot Phase 3.5). Raw-file `score --input` is the logs-only/non-Python fallback; `api_endpoint` suits agents already exposed as a clean HTTP service; OTel (Build Stage 3) is the high-fidelity future path. See `docs/plans/eval-landscape-strategy.md` |
| **One shared core behind CLI and SDK** | `runner_core.py` extracts the write-config → run → ingest tail so `bi-evals score`/`run` and `Runner.score()` can never diverge — the same principle `compare/gate.py` applies to gating |
| **Gate ≠ verdict** | The verdict is *descriptive* (what changed — same for everyone) and unconfigurable; the gate is *policy* (should this fail the build — floor, budget, `fail_on`) and per-team. They can legitimately disagree (amber verdict + floor-failed gate; red verdict + budget-passed gate). The compare page shows both, visually distinct, only when gating is enabled |
| **Gating defaults are strict no-ops** | `fail_on` unset = gating disabled; `min_pass_rate` null; budget 0. A `red` default would have made every already-red `compare` start failing builds. Opting in is one config line or one CLI flag |
| **Gate outcomes are computed, not persisted** | The gate derives from (run diff + config at evaluation time); the CLI artifact freezes what a CI invocation saw, the ui recomputes from current config. Persisting per-run gate history becomes relevant only if a shared/hosted store materializes |
| **Clean schema break over back-compat shim** | Old flat `agent:` configs fail loudly with a migration hint rather than silently mis-parsing |
| **`agent.adapter` is a `Literal`** | A typo'd adapter fails at config-load with a clear pydantic error, not later at dispatch |
| **Back-compat property accessors on `AgentConfig`** | `.type`/`.model`/`.endpoint`/`.tools` delegate into nested blocks so reader modules stay adapter-agnostic through the schema break |
| **Semantic-layer scoring is a canonical-query problem** | dbt Semantic Layer / Snowflake Semantic Views / Cube share one vocabulary (metrics/dimensions/grain/filters) and differ only in query surface dialect — same "normalize once, score once" move as the adapter contract pivot. Proposal only; see `docs/plans/build-stage-5-semantic-layer-scoring.md` |
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
