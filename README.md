# bi-evals

A configurable Python framework for evaluating SQL-generating BI agents. You provide golden tests, database credentials, and your agent's answers -- the framework handles SQL execution, 10-dimension accuracy scoring, HTML reporting, and regression detection.

[Promptfoo](https://promptfoo.dev/) (Node.js) is used as the test runner engine; all custom logic is Python.

## Where bi-evals fits in your eval process

> bi-evals is a framework for the offline evaluation suite behind self-service analytics.
> The default on-ramp is the **`bi_evals.Runner` SDK** — see
> [Getting started](#getting-started-with-the-sdk) below.

**bi-evals doesn't run your agent — you run your agent, and bi-evals grades its homework.** The
offline evaluation suite is the golden questions you run your agent against before launch, gate
releases on, and re-run to catch regressions. You author the evals; bi-evals is the shared engine
that scores them.

### Evaluate the response, don't rebuild the agent

To be faithful you must score the agent your users actually hit, and to plug into *any* stack
bi-evals can't assume how your agent is wired. So it makes **no assumption about how an answer was
produced** — it evaluates the *response*. For each golden question your agent hands over two things:

- **`generated_sql`** — the SQL it produced.
- **`trace`** — what it did to get there (files/skills read, tools invoked).

bi-evals **executes that SQL itself** against your warehouse, so you never send result sets — only
the query and what it touched. Everything else (provider, orchestration, MCP/LangChain/notebook)
stays yours and untouched.

### What you'll need

- **A BI agent that exposes its generated SQL and trace.** This is the real prerequisite — see
  [The real boundary](#the-real-boundary-and-scope) below. bi-evals doesn't care how it's built;
  [`docs/instrumenting-your-agent.md`](docs/instrumenting-your-agent.md) describes the output
  shapes that make scoring effortless.
- **Warehouse credentials** bi-evals can use to execute SQL for grading.
- **Golden questions with reference SQL** — the "right answer" to grade against. You author these.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/) to run the framework.

### The journey

1. **Install & scaffold** — `bi-evals init push` gives you a config and a `golden/` folder. You
   provide warehouse credentials and where your tests live.
2. **Author golden tests** — a question + reference SQL + what should be true of the result. This
   encodes what "correct" means for your data; it's the real work, and it's yours.
3. **Run your own agent over the questions** — the `bi_evals.Runner` SDK owns the loop: you write
   one `ask()` call against the agent you already have and `submit()` what it returns. Your
   production agent runs unchanged.
4. **Score** — bi-evals runs each generated SQL (and the reference) against your warehouse and
   compares across 10 dimensions, producing a pass/fail verdict and a report.
5. **Gate and regress** — in CI, every change re-runs the suite, gates on a threshold, and shows a
   per-dimension diff.

### The real boundary, and scope

What bi-evals can score depends on **whether your agent surfaces its own SQL and trace.** Most
text-to-SQL agents do (it's in their UI) — then connecting is light. If your agent only returns a
natural-language answer, you'll need to surface the SQL first, and that cost is the same however
you integrate: nothing can score a query the agent never emitted.

bi-evals targets the **offline** half of evaluation — the pre-launch gate, regression suite, and
"change one thing, compare pass rates" experiments. Online/production monitoring is a different
surface it doesn't aim to be; the seam is that every real correction is a candidate golden test.

## Adapters: how your agent's answers reach the scorer

`agent.adapter` in `bi-evals.yaml` selects how answers arrive. The scoring engine is identical for
all of them — pick the adapter that matches your situation **before** you scaffold a project.

| Adapter | How it works | Use when |
|---|---|---|
| **`push`** (default) | You run your agent, hand each answer to the **`bi_evals.Runner` SDK** (or write a JSONL and `bi-evals score --input`). No live agent during scoring. | Almost always — works for any stack (MCP, LangChain, a notebook, anything). |
| **`api_endpoint`** | bi-evals POSTs each question to your agent's HTTP endpoint and scores the response live. | Your agent is already a clean question→SQL HTTP service. Response shape: [`docs/byo-response-contract.md`](docs/byo-response-contract.md); validate with `bi-evals doctor`. |
| **`anthropic_tool_loop`** (dev-only) | bi-evals runs Claude with *your* skill files locally. Evaluates a rebuild, not your real agent. | Authoring goldens before a real agent exists. |

See [`docs/getting-started.md`](docs/getting-started.md) for the full walkthrough.

## How it works

1. **You define golden tests** in YAML -- each contains a natural-language question, reference SQL, expected skill path, and scoring criteria.
2. **Your agent answers each question** — you submit `{generated_sql, trace}` via the SDK or a JSONL file (push), or bi-evals fetches it live (`api_endpoint`).
3. **The scorer runs both the generated and reference SQL** against your database and compares results across 10 dimensions. See [Scoring](#scoring) for details.

## Scoring

A test produces 10 independent dimension results, then a single pass/fail verdict via tiered/weighted aggregation. A dimension whose golden has nothing to evaluate (e.g. `anti_pattern_compliance` with no `anti_patterns` declared) skips as a vacuous pass and is dropped from the report.

### Dimensions

| Dimension | Tier | Default weight | What it checks |
|---|---|---|---|
| `execution` | critical | 3.0 | Generated SQL runs without error |
| `row_completeness` | critical | 3.0 | Generated results contain the expected rows (executes both queries against the live DB and compares row keys) |
| `value_accuracy` | critical | 3.0 | Numeric values in matching rows are within `value_tolerance`; column matching falls back to position when aliases differ |
| `row_precision` | important | 2.0 | No spurious extra rows beyond the reference |
| `anti_pattern_compliance` | important | 2.0 | The SQL avoids the golden's declared `forbidden_tables`/`forbidden_columns` |
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

## Getting started with the SDK

The fastest path from zero to a scored eval run. Full walkthrough (including the JSONL and
`api_endpoint` alternatives): [`docs/getting-started.md`](docs/getting-started.md).

### Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Node.js (bi-evals runs Promptfoo under the hood via `npx`)
- A Snowflake account with key-pair authentication
- **Your own BI agent that exposes its generated SQL** — see
  [`docs/instrumenting-your-agent.md`](docs/instrumenting-your-agent.md) for the output shapes
  bi-evals picks up best

### 1. Install & scaffold

```bash
mkdir my-evals && cd my-evals
uv init && uv add bi-evals       # or from a checkout: uv sync --group dev
uv run bi-evals init push --dir .
```

This writes a push-shaped `bi-evals.yaml`, `.env` / `.env.example` (Snowflake-only), an example
golden test, and `golden/`, `results/`, `reports/` directories.

### 2. Configure your warehouse

Fill in `.env` (loaded automatically; see `.env.example` for the variable list). Then check the
setup before spending anything:

```bash
uv run bi-evals doctor
```

### 3. Write your first golden test

One YAML file per question in `golden/` — a question, the reference SQL for the correct answer,
and what should be true of the result:

```yaml
id: revenue-001
category: revenue
question: "What was total revenue by region last quarter?"
reference_sql: |
  SELECT region, SUM(revenue) AS total
  FROM analytics.fct_revenue
  WHERE quarter = '2026Q1'
  GROUP BY region
expected:
  required_columns: [region, revenue]   # SOURCE columns the query must read — not output aliases
  row_comparison:
    enabled: true
```

See [`docs/golden-tests-guide.md`](docs/golden-tests-guide.md) for the full schema.

### 4. Run your agent and score

The `bi_evals.Runner` SDK owns iteration, collection, and scoring — you write one `ask()` call
against the agent you already have:

```python
import bi_evals

runner = bi_evals.Runner("bi-evals.yaml", verbose=True)
for case in runner.golden_cases():
    try:
        answer = my_agent.ask(case.question)      # your agent, unchanged
        runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
        # or, if your agent returns prose: response_text=answer.text
    except Exception as e:
        runner.submit(case, error=str(e))         # flaky agent → recorded, run continues

report = runner.score()                           # executes SQL, scores, writes HTML report
print(f"{report.passed}/{report.total} passed → {report.report_path}")
assert report.pass_rate >= 0.9                    # gate CI on it
```

`submit()` takes `generated_sql` **or** `response_text` (raw answer — SQL is extracted) **or**
`error`. `trace` is optional; it's needed for `skill_path_correctness`.

### 5. View results

```bash
uv run bi-evals report                 # self-contained HTML scorecard
uv run bi-evals ui                     # interactive viewer with per-test drilldown
uv run bi-evals compare latest prev    # regression diff between two runs
```

## Project structure

```
src/bi_evals/
  sdk.py          # bi_evals.Runner — the public SDK (golden_cases / submit / score)
  runner_core.py  # Push-score pipeline core shared by the SDK and the CLI
  cli.py          # CLI entry point (init, score, run, doctor, report, compare, ui, ...)
  config.py       # Pydantic config model driven by bi-evals.yaml
  doctor.py       # Pre-run setup validation per adapter
  provider/       # Adapter contract + registry (push, api_endpoint, anthropic_tool_loop)
  scorer/         # 10-dimension evaluators + SQL parsing utilities
  tools/          # Tool protocol (file_reader, describe_table)
  db/             # Database client protocol (Snowflake implementation)
  golden/         # Golden test loader and Pydantic models
  promptfoo/      # Promptfoo config generation and runner bridge
  store/          # Embedded DuckDB run history (ingest + query helpers)
  report/         # HTML report generation
  compare/        # Run-vs-run regression diff
  ui/             # Local FastAPI viewer (runs list, per-test drilldown)
```

## Documentation

- [`docs/getting-started.md`](docs/getting-started.md) -- full setup walkthrough (SDK, JSONL, and api_endpoint paths)
- [`docs/golden-tests-guide.md`](docs/golden-tests-guide.md) -- how to write golden tests
- [`docs/instrumenting-your-agent.md`](docs/instrumenting-your-agent.md) -- for agent builders: what your agent should emit so bi-evals scores it with zero massaging
- [`docs/push-limitations.md`](docs/push-limitations.md) -- what the push adapter requires; the push-ready checklist
- [`docs/byo-response-contract.md`](docs/byo-response-contract.md) -- the `api_endpoint` response shape
- [`docs/eval-landscape.md`](docs/eval-landscape.md) -- where bi-evals sits in the eval-tool landscape and why the SDK is the on-ramp
- [`docs/pivot-phases.md`](docs/pivot-phases.md) -- the adapter architecture and where it's heading
- [`STATUS.md`](STATUS.md) -- what works today
- [`CLAUDE.md`](CLAUDE.md) -- architecture reference and development commands

## Development

```bash
uv run python -m pytest tests/ -v                    # all tests
uv run python -m pytest tests/ -m "not integration"  # unit tests only
```

See [`CLAUDE.md`](CLAUDE.md) for the full list of commands and architectural details.

## License

Private.
