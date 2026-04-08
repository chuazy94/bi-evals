# bi-evals — Implementation Status

## Summary

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Promptfoo is used as the test runner engine; all custom logic (provider, tools, scoring) is Python. Phases 1-3 are complete — the config system, CLI scaffolding, tool abstraction, both provider types, database client, golden test model, and 9-dimension scorer are built and tested.

What works today:
- `bi-evals init` scaffolds a new eval project (config, directories, golden test stub)
- Config system loads `bi-evals.yaml` with env var substitution and path resolution
- `anthropic_tool_loop` provider runs the full multi-turn Claude tool-calling loop with trace capture, SQL extraction, and cost tracking
- `api_endpoint` provider calls external agent APIs with configurable response parsing
- `FileReaderTool` serves skill/knowledge files to Claude with path traversal protection
- Provider entry point dispatches based on `agent.type` and writes trace JSON for the scorer
- `SnowflakeClient` executes SQL and returns structured results with error handling
- `GoldenTest` model loads expected results from YAML (reference SQL, required columns, skill path, row comparison config)
- 9-dimension binary scorer evaluates: execution, table alignment, column alignment, filter correctness, row completeness, row precision, value accuracy, no hallucinated columns, skill path correctness
- Scorer entry point `get_assert()` integrates with Promptfoo's assertion interface
- 101 unit tests, all passing (+ 1 integration demo test)

---

## Completed

### Phase 1: Project Skeleton + Config System

- **`pyproject.toml`** — pip-installable package via uv, all deps declared
- **`src/bi_evals/config.py`** — Pydantic config model, `${ENV_VAR}` resolution, relative path resolution, validation
- **`src/bi_evals/cli.py`** — Click CLI with `bi-evals init` scaffolding command
- **`tests/test_config.py`** — 11 tests

### Phase 2: Tools + Agent Loop + Provider

- **`src/bi_evals/tools/base.py`** — `Tool` protocol (name, definition, execute)
- **`src/bi_evals/tools/file_reader.py`** — `FileReaderTool` with path traversal protection
- **`src/bi_evals/tools/registry.py`** — Factory building tools from config
- **`src/bi_evals/provider/cost.py`** — Pricing map for Claude models
- **`src/bi_evals/provider/agent_loop.py`** — Multi-turn Claude tool-calling loop (trace capture, SQL extraction via 3 strategies, token counting, cost calculation, max rounds safety limit)
- **`src/bi_evals/provider/api_endpoint.py`** — HTTP POST provider (configurable response keys with dot-notation, custom headers, optional trace capture, fallback SQL extraction)
- **`src/bi_evals/provider/entry.py`** — Promptfoo `call_api()` entry point dispatching on `agent.type`, writes trace JSON to `results/traces/`
- **`tests/test_agent_loop.py`** — 24 tests
- **`tests/test_api_endpoint.py`** — 11 tests
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

**Total: 101 unit tests, all passing. 2 demo/integration tests.**

---

## Remaining

### Phase 4: Promptfoo Bridge + `bi-evals run`
- Generate `promptfooconfig.yaml` from `bi-evals.yaml`
- Wire up CLI `run` command (end-to-end: config → promptfoo → results)

### Phase 5: Reporting + Regression
- HTML report generator (single-file, self-contained) (`src/bi_evals/report/` — empty stub)
- Regression comparison (`bi-evals compare`)
- CLI `report` and `compare` commands

### Phase 6: Example Project
- Complete working example in `examples/`
- Skill/knowledge files for a sample dataset
- 5-8 golden tests across categories

### Phase 7: CI/CD (optional)
- GitHub Actions for PR gating and nightly runs

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
