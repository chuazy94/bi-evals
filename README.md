# bi-evals

A configurable Python framework for evaluating SQL-generating BI agents. You provide skill/knowledge files, golden tests, and database credentials -- the framework handles the LLM provider loop, multi-dimension accuracy scoring, and regression detection.

[Promptfoo](https://promptfoo.dev/) (Node.js) is used as the test runner engine; all custom logic is Python.

## How it works

1. **You define golden tests** in YAML -- each contains a natural-language question, reference SQL, expected skill path, and scoring criteria.
2. **The framework sends each question to your agent** (either via a built-in Claude tool-calling loop or an HTTP endpoint).
3. **The agent generates SQL** using tools you configure (file reader for skill/knowledge files, `describe_table` for schema discovery).
4. **The scorer runs both the generated and reference SQL** against your database and compares results across 9 dimensions. See [Scoring](#scoring) for details.

## Scoring

A test produces 9 independent dimension results, then a single pass/fail verdict via tiered/weighted aggregation.

### Dimensions

| Dimension | Tier | Default weight | What it checks |
|---|---|---|---|
| `execution` | critical | 3.0 | Generated SQL runs without error |
| `row_completeness` | critical | 3.0 | Generated results contain the expected rows (executes both queries against the live DB and compares row keys) |
| `value_accuracy` | critical | 3.0 | Numeric values in matching rows are within `value_tolerance`; column matching falls back to position when aliases differ |
| `row_precision` | important | 2.0 | No spurious extra rows beyond the reference |
| `column_alignment` | important | 2.0 | The SQL references the source columns listed in the golden test's `required_columns` (aliases ignored) |
| `table_alignment` | diagnostic | 1.0 | Correct physical tables referenced (CTE names excluded) |
| `filter_correctness` | diagnostic | 1.0 | WHERE-clause column/operator structure matches the reference |
| `no_hallucinated_columns` | diagnostic | 1.0 | No fabricated source columns in the SQL beyond what the reference uses |
| `skill_path_correctness` | diagnostic | 1.0 | Agent read the right files and invoked the expected tools |

### Pass/fail rule

A test passes when **both** conditions hold:

1. Every dimension listed in `scoring.critical_dimensions` passes (default: `execution`, `row_completeness`, `value_accuracy`).
2. The weighted score across all dimensions is at least `scoring.pass_threshold` (default: `0.75`).

If any critical dimension fails, the test fails regardless of the weighted score. This means the result-based correctness checks are gating, while structural checks (table/column/filter alignment) act as diagnostic signals that influence the score but don't independently fail the test.

### Tuning

All values are configurable in `bi-evals.yaml` under `scoring`:

```yaml
scoring:
  dimensions: [...]              # which dimensions to run
  critical_dimensions: [...]     # which must pass; others are advisory
  dimension_weights: { ... }     # per-dimension weight in the overall score
  pass_threshold: 0.75           # minimum weighted score to pass
  thresholds:
    completeness: 0.95           # row_completeness ratio threshold
    precision: 0.95              # row_precision ratio threshold
    value_tolerance: 0.0001      # numeric tolerance for value_accuracy
```

Common adjustments:

- **Stricter eval**: raise `pass_threshold` (e.g. `0.9`), or add diagnostic dimensions to `critical_dimensions`.
- **Looser eval**: lower `pass_threshold`, drop noisy dimensions (e.g. remove `filter_correctness` from `dimensions`), or set `value_tolerance` higher.
- **Result-only mode**: enable just `execution`, `row_completeness`, `row_precision`, `value_accuracy` and ignore structural checks entirely.

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
