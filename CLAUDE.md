# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

bi-evals is a configurable Python framework for evaluating SQL-generating BI agents. Users provide their own skill/knowledge files, golden tests, and database credentials — the framework handles the LLM provider loop, 9-dimension accuracy scoring, HTML reporting, and regression detection. Promptfoo (Node.js) is used as the test runner engine; all custom logic is Python.

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
uv run bi-evals run
uv run bi-evals report
uv run bi-evals compare <run1.json> <run2.json>
```

## Architecture

### Configuration-driven design

Everything is driven by `bi-evals.yaml`. The `BiEvalsConfig` Pydantic model (`src/bi_evals/config.py`) is the central dependency — almost every module loads it. Config supports `${ENV_VAR}` substitution and resolves all paths relative to the config file location.

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

`DatabaseClient` protocol with `SnowflakeClient` implementation. Adding new databases = new file + one line in factory. Not yet implemented.

### Scorer (`src/bi_evals/scorer/`)

9 binary pass/fail dimensions. Each dimension is an independent evaluator function. The scorer entry point (`entry.py`) provides Promptfoo's `get_assert()` interface. Not yet implemented.

### Key patterns

- **Protocols over inheritance**: `Tool`, `DatabaseClient` use `typing.Protocol` for extensibility
- **Config as the root**: Modules receive `BiEvalsConfig` and resolve everything from it
- **No hardcoding**: All paths, model names, tool definitions, scoring thresholds come from config
- **Pydantic for validation**: Config and golden test schemas validate at load time


### Testing
When creating tests, there are 2 different types of tests. It is important to delineate between the 2 as the demo testing may consume loads of API credits.
1. Unit testing - these are tests that test individual functionality, and dont actually call any LLM API endpoints. For eg. tests/test_agent_loop.py. Naming convention would be test_<functionality>.py
2. Demo testing - These tests make actual LLM API endpoint calls and will consume credits. For eg. tests/test_demo_routing.py. Naming convention will be test_demo_<functionality>.py. 