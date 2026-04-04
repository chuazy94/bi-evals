# bi-evals — Implementation Status

## Overview

Configurable Python framework for evaluating SQL-generating BI agents.
Promptfoo as test runner, 9-dimension binary accuracy scoring.

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
- **`test_live.py`** — manual test script for running the Anthropic loop against real Claude API

**Total tests: 46, all passing.**

----------

## Remaining

### Phase 3: Database + Scorer — 9 Dimensions
- `DatabaseClient` protocol + `SnowflakeClient`
- `GoldenTest` Pydantic model + YAML loader
- SQL parser (sqlglot) for table/column extraction
- Row-level result comparator with tolerance
- 9 dimension evaluators (execution, table alignment, column alignment, filter correctness, row completeness, row precision, value accuracy, no hallucinated columns, skill path correctness)
- Promptfoo scorer entry point (`get_assert`)

### Phase 4: Promptfoo Bridge + `bi-evals run`
- Generate `promptfooconfig.yaml` from `bi-evals.yaml`
- Wire up CLI `run` command (end-to-end: config → promptfoo → results)

### Phase 5: Reporting + Regression
- HTML report generator (single-file, self-contained)
- Regression comparison (`bi-evals compare`)
- CLI `report` and `compare` commands

### Phase 6: Example Project — COVID-19
- Complete working example in `examples/covid-19/`
- Skill/knowledge files for COVID-19 Snowflake dataset
- 5-8 golden tests across categories

### Phase 7: CI/CD (optional)
- GitHub Actions for PR gating and nightly runs

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Framework, not hardcoded project | Users bring their own skill files, golden tests, and DB credentials |
| `bi-evals init` scaffolds eval infra only | No opinion on skill/knowledge file structure — users point to theirs |
| Two provider types | `anthropic_tool_loop` for Claude-native agents, `api_endpoint` for existing APIs |
| File-based trace communication | Provider writes JSON, scorer reads it — handles Promptfoo process isolation |
| Snowflake only for MVP | `DatabaseClient` protocol designed for adding Postgres/BigQuery later |
| sqlglot for SQL parsing | Handles Snowflake dialect, aliases, CTEs without regex |
