# bi-evals

A configurable Python framework for evaluating SQL-generating BI agents. You provide skill/knowledge files, golden tests, and database credentials -- the framework handles the LLM provider loop, multi-dimension accuracy scoring, and regression detection.

[Promptfoo](https://promptfoo.dev/) (Node.js) is used as the test runner engine; all custom logic is Python.

## How it works

1. **You define golden tests** in YAML -- each contains a natural-language question, reference SQL, expected skill path, and scoring criteria.
2. **The framework sends each question to your agent** (either via a built-in Claude tool-calling loop or an HTTP endpoint).
3. **The agent generates SQL** using tools you configure (file reader for skill/knowledge files, `describe_table` for schema discovery).
4. **The scorer runs both the generated and reference SQL** against your database and compares results across 9 dimensions.

### Scoring dimensions

| Dimension | What it checks |
|---|---|
| execution | Generated SQL runs without error |
| table_alignment | Correct physical tables referenced |
| column_alignment | Correct source columns used (alias-agnostic) |
| filter_correctness | WHERE clause matches reference |
| row_completeness | Generated results contain the expected rows |
| row_precision | No spurious extra rows |
| value_accuracy | Numeric values match within tolerance |
| no_hallucinated_columns | No fabricated source columns in the SQL |
| skill_path_correctness | Agent read the right files / called the right tools |

## Quick start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Node.js (for Promptfoo)
- A Snowflake account with key-pair authentication

### Install

```bash
uv sync --group dev
```

### Scaffold a project

```bash
uv run bi-evals init --dir /path/to/my-evals
```

This creates a `bi-evals.yaml` config, a `.env` file, and starter directories for golden tests, skills, and results.

### Configure

Edit `bi-evals.yaml` to point at your skill files, database, and model. Set credentials in `.env` (loaded automatically). See `.env.example` for required variables.

### Run an eval

```bash
cd /path/to/my-evals
uv run bi-evals run              # run all golden tests
uv run bi-evals run -v           # verbose Promptfoo output
uv run bi-evals run --no-cache   # force fresh API calls
uv run bi-evals run -f cases     # filter tests by id/category/tag
```

### View results

```bash
uv run bi-evals view             # open Promptfoo web UI
```

## Project structure

```
src/bi_evals/
  cli.py          # CLI entry point (init, run, view, report, compare)
  config.py       # Pydantic config model driven by bi-evals.yaml
  provider/       # Agent dispatch (Claude tool loop, HTTP endpoint)
  scorer/         # 9-dimension evaluators + SQL parsing utilities
  tools/          # Tool protocol (file_reader, describe_table)
  db/             # Database client protocol (Snowflake implementation)
  golden/         # Golden test loader and Pydantic models
  promptfoo/      # Promptfoo config generation and runner bridge
  report/         # HTML report generation
```

## Documentation

- [`docs/bi-eval-framework.md`](docs/bi-eval-framework.md) -- design rationale and architecture
- [`docs/golden-tests-guide.md`](docs/golden-tests-guide.md) -- how to write golden tests
- [`docs/mvp-eval-platform.md`](docs/mvp-eval-platform.md) -- MVP implementation plan
- [`CLAUDE.md`](CLAUDE.md) -- architecture reference and development commands

## Development

```bash
uv run python -m pytest tests/ -v                    # all tests
uv run python -m pytest tests/ -m "not integration"  # unit tests only
```

See [`CLAUDE.md`](CLAUDE.md) for the full list of commands and architectural details.

## License

Private.
