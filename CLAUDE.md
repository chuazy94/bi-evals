# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Users provide their own skill/knowledge files, golden tests, and database credentials — the framework handles the LLM provider loop, 10-dimension accuracy scoring, HTML reporting, and regression detection. Promptfoo (Node.js) is used as the test runner engine; all custom logic is Python.

## Commands

```bash
# Install dependencies
uv sync --group dev

# Run all tests
uv run python -m pytest tests/ -v

# Run a single test file
uv run python -m pytest tests/test_config.py -v

# Run a specific test
uv run python -m pytest tests/test_config.py::TestBiEvalsConfig::test_load_basic -v

# Run only integration tests (require Snowflake/Anthropic API)
uv run python -m pytest tests/ -m integration -v

# Skip integration tests
uv run python -m pytest tests/ -m "not integration" -v

# CLI commands
uv run bi-evals init --dir /tmp/test-project
uv run bi-evals run                                     # runs Promptfoo + auto-ingests
uv run bi-evals ingest results/eval_<ts>.json           # backfill an old run into DuckDB
uv run bi-evals report [--run-id ID]                    # single-run HTML
uv run bi-evals compare <run_a> <run_b>                 # accepts evalId or `latest`/`prev`
uv run bi-evals cost                                    # cost-vs-median history
uv run bi-evals flakiness                               # tests that flip pass/fail across runs
uv run bi-evals view                                    # opens Promptfoo's per-test UI
```

## Architecture

### Configuration-driven design

Everything is driven by `bi-evals.yaml`. The `BiEvalsConfig` Pydantic model (`src/bi_evals/config.py`) is the central dependency — almost every module loads it. Config supports `${ENV_VAR}` substitution and resolves all paths relative to the config file location. On load, if `.env` exists in the same directory as `bi-evals.yaml`, it is applied automatically (`override=False`: already-exported shell variables win).

The framework does NOT own or scaffold skill/knowledge files. Users point `agent.tools[].config.base_dir` to their existing files.

### Provider dispatch (`src/bi_evals/provider/`)

The provider entry point (`entry.py`) dispatches based on `agent.type` in config:

- **`anthropic_tool_loop`** (`agent_loop.py`): Manages the full Claude multi-turn tool-calling loop. Promptfoo never sees individual tool calls — our code handles the entire loop (send question → Claude returns tool_use → execute tool locally → send tool_result → repeat until end_turn). This exists because Promptfoo's standard providers don't execute tool callbacks in a loop.
- **`api_endpoint`** (`api_endpoint.py`): HTTP POST to an existing agent API. Supports configurable response JSON keys with dot-notation.

Both produce the same `AgentResult` dataclass and write trace JSON to `results/traces/{test_id}.json`.

### Trace communication between provider and scorer

The provider and scorer may run in separate Python processes (Promptfoo may fork). They communicate via JSON files in `results/traces/`. The provider writes a trace file after each test; the scorer reads it. Test ID is derived from the golden file path.

### Tool abstraction (`src/bi_evals/tools/`)

Tools follow a `Tool` protocol (name, definition, execute). `FileReaderTool` reads files from a configured base directory with path traversal protection. The registry (`registry.py`) builds tools from config — adding new tool types means adding one `elif` branch in the registry.

### Database abstraction (`src/bi_evals/db/`)

`DatabaseClient` protocol with `SnowflakeClient` as the only current implementation. Adding new databases = new file + one line in `factory.py`.

### Scorer (`src/bi_evals/scorer/`)

10 binary pass/fail dimensions, each an independent evaluator (`dimensions/*.py`). Pass/fail rule: every `critical_dimensions` entry must pass AND the weighted score ≥ `pass_threshold`. The scorer entry point (`entry.py`) implements Promptfoo's `get_assert()` interface. A dimension whose golden has nothing to evaluate (e.g. `anti_pattern_compliance` when no `anti_patterns` are declared) skips with `passed=true` and a `"skipped: ..."` reason; vacuously-passing dims are dropped from the HTML report.

### Storage (`src/bi_evals/store/`)

Embedded DuckDB at `results/bi-evals.duckdb`. `bi-evals run` auto-ingests after Promptfoo finishes; ingest is idempotent (`DELETE` + re-insert per `run_id`). The JSON files in `results/` remain the replayable source of truth — DuckDB is the queryable view. Schema is in `schema.py`; query helpers return frozen dataclasses (`queries.py`). Each run snapshots `prompt_snapshot` (SHA256/size/mtime per file the agent read) into `runs.prompt_snapshot` for prompt-drift detection.

### Report & compare (`src/bi_evals/report/`, `src/bi_evals/compare/`)

Jinja2 templates extending `_base.html.j2`. **No CDN, no external URLs** (enforced by test). `report/builder.py` and `compare/builder.py` do all computation; templates only iterate. Compare uses a tiered verdict — 🔴 if any test regressed (overall pass T→F, or critical dim flipped pass→fail), 🟡 for non-regression deltas, 🟢 otherwise. `added`/`removed` tests never flip the verdict to red.

### Key patterns

- **Protocols over inheritance**: `Tool`, `DatabaseClient` use `typing.Protocol` for extensibility
- **Config as the root**: Modules receive `BiEvalsConfig` and resolve everything from it
- **No hardcoding**: All paths, model names, tool definitions, scoring thresholds come from config
- **Pydantic for validation**: Config and golden test schemas validate at load time


### Testing
When creating tests, there are 2 different types of tests. It is important to delineate between the 2 as the demo testing may consume loads of API credits.
1. Unit testing - these are tests that test individual functionality, and dont actually call any LLM API endpoints. For eg. tests/test_agent_loop.py. Naming convention would be test_<functionality>.py
2. Demo testing - These tests make actual LLM API endpoint calls and will consume credits. For eg. tests/test_demo_routing.py. Naming convention will be test_demo_<functionality>.py.

## Live test project at `tmp/my-evals/`

`tmp/my-evals/` is the user's active end-to-end project — real `bi-evals.yaml`, golden tests, knowledge files, and an ingested DuckDB. It's used to manually verify features against a working setup, so it must stay in sync with the framework.

**When adding or changing a feature, also update `tmp/my-evals/` if the change touches user-visible config or scaffolded files.** Specifically:

- New config field with a non-default behavior to demo → add it to `tmp/my-evals/bi-evals.yaml`
- New golden-test field (e.g. `last_verified_at`, `anti_patterns`) → add it to at least one golden in `tmp/my-evals/golden/cases/` so the feature actually exercises
- New CLI command or flag that warrants a smoke test → mention the exact command to run against `tmp/my-evals/` in the PR description
- Schema change that requires re-ingest → call it out so the user can rebuild `tmp/my-evals/results/bi-evals.duckdb`

If a feature has no surface in the user-facing config (pure internal refactor, new test fixture, etc.), no `tmp/my-evals/` change is needed. When in doubt, ask.

## Documentation

- `docs/feature_summary.md` — consolidated reference for every feature with the commands to invoke it
- `docs/golden-tests-guide.md` — golden-test schema and authoring guide
- `docs/duckdb-schema.md` — storage schema reference
- `docs/mvp-eval-platform.md` — original design doc and roadmap
- `STATUS.md` — implementation status snapshot, updated via the `/project-status` skill 