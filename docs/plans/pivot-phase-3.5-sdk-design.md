# `bi_evals.Runner` — SDK design

The Runner SDK is the **default, lowest-effort on-ramp**: the customer writes one `ask()`
call against their own agent, and the framework owns iteration, collection, JSONL I/O, and
scoring. It is the ergonomic front-end to the **same push pipeline** that
`bi-evals score --input` uses — not a second pipeline.

Strategy context: `docs/plans/eval-landscape-strategy.md` (why the SDK is top priority — raw-file push is
the highest-effort build-it path; the SDK turns its painful parts into the framework's job).

## What the SDK does — and does NOT — solve

The SDK removes **plumbing**, not **visibility**. These are two independent kinds of work,
and conflating them over-promises the SDK:

- **Plumbing (the SDK removes this):** reading the goldens, looping over questions, tracking
  which golden each answer belongs to, reshaping the agent's output into the right fields,
  writing the JSONL, and kicking off scoring. This was the customer's hand-built glue with
  raw-file push; the framework now does it.
- **Visibility (the SDK cannot touch this):** whether the agent's **generated SQL and trace
  exist as data the customer can hand over at all.** The SDK gives a clean place to put them
  (`submit(case, generated_sql=..., trace=...)`) but it cannot *create* them. If
  `my_agent.ask()` returns only a chart or a prose sentence with no `.sql` / `.trace`, there
  is nothing to pass to `submit()`.

**The SDK does not reduce what the customer's agent must expose.** The same requirement holds
for every adapter — push, SDK, api_endpoint, OTel: bi-evals scores SQL by *executing* it, so
it can never grade a query the agent never surfaced (`push-limitations.md`).

> **"Can the SDK orchestrate capture for me?"** Not in the general case, and deliberately so.
> Auto-capturing the SQL/trace would mean the SDK reaching into the agent's internals (its DB
> calls, its tools) — which requires per-customer knowledge of the stack and only works for a
> locally-importable Python agent, not an MCP-fronted, another-language, or behind-an-API
> production agent. That coupling is exactly what the response-evaluation pivot rejected. The
> legitimate "capture automatically" path is **OTel** (Pivot Phase 6): there the *agent
> itself* emits structured spans and bi-evals reads them — the agent does the emitting, not
> the SDK doing the reaching-in.

The one way the SDK *helps* visibility: `submit()`'s named arguments (`generated_sql=`,
`trace=`) give the customer a clear, typed target for *what* to expose — clearer than
"reshape your output into this JSONL schema." It clarifies the requirement; it doesn't remove
it.

## The shape (what the customer writes)

```python
import bi_evals

runner = bi_evals.Runner("bi-evals.yaml")

for case in runner.golden_cases():
    try:
        answer = my_agent.ask(case.question)
        runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
    except Exception as e:
        runner.submit(case, error=str(e))      # flaky agent → record + keep going

report = runner.score()
assert report.pass_rate >= 0.9                  # CI-assertable
for f in report.failures:
    print(f.test_id, f.reason)
```

## Design decisions (settled)

| Decision | Choice | Why |
|---|---|---|
| Control | **Thin** — customer writes the `for` loop + `submit()` | Explicit, debuggable, matches the field (Braintrust `task`) |
| Scoring | **Reuse the CLI/Promptfoo path** | One scoring code path; SDK results are *identical* to `score --input` by construction — zero divergence risk |
| `score()` returns | **`RunReport` object + report/DuckDB** | CI users `assert report.pass_rate > x`; the HTML report + DuckDB ingest still happen |
| Mid-loop failure | **`submit(case, error=...)`** | Real agents are flaky; record a failing row and continue rather than crash |
| Import name | **`import bi_evals`** | Matches the package; no alias packaging surface |

## Public API

### `Runner(config_path="bi-evals.yaml", *, filter=None)`
Loads `BiEvalsConfig`. `filter` is the same substring filter `run`/`score` accept (id /
category / tag), so a CI job can scope a subset. **The filter is applied once, in
`golden_cases()`** — it yields only matching cases, and `score()` validates submissions
against that same filtered set. So a customer who iterates `golden_cases()` and submits each
case will never trip the "missing submission" pre-flight; the two stay in lockstep.

### `runner.golden_cases() -> Iterator[Case]`
Yields one `Case` per golden (via the existing `load_golden_tests_with_paths`), honoring the
`filter`.

```python
@dataclass(frozen=True)
class Case:
    id: str           # golden id
    question: str     # the question to ask your agent
    golden_file: str  # internal join key — don't construct it yourself, but safe to print
    category: str
```

### `runner.submit(case, *, generated_sql=None, response_text=None, trace=None, error=None)`
Records one result in memory, keyed by `case.golden_file`. Exactly one of
`generated_sql` / `response_text` / `error` must be provided. `trace` is the open-envelope
trace (optional). Submitting twice for the same case raises (mirrors the CLI's
duplicate-golden rejection). Missing or extra fields raise immediately with a clear message
(fail at the call site, not later at `score()`).

### `runner.score(*, verbose=False) -> RunReport`
1. Writes the collected submissions to `results/sdk_<ts>.jsonl` under the project's
   `results/` dir. **This file is kept**, not cleaned up — it's a replayable artifact (same
   principle as the `results/eval_<ts>.json` the CLI keeps: the JSON/JSONL files remain the
   replayable source of truth, DuckDB is the queryable view). A customer can re-run it later
   with `bi-evals score --input results/sdk_<ts>.jsonl`.
2. Invokes the **existing** push score path (`generate_promptfoo_config` with
   `adapter=push`, `_validate_push_submissions`, `_execute_eval`) — same code as
   `bi-evals score --input`.
3. Queries the just-ingested run from DuckDB (`store/queries.py`) and returns a `RunReport`.

Raises if a selected golden has no submission (same pre-flight as the CLI).

```python
@dataclass(frozen=True)
class TestResult:
    test_id: str            # the golden's id
    passed: bool
    score: float            # weighted overall score
    fail_reason: str        # e.g. "Failed critical dimension(s): ['value_accuracy']"
    # (mirrors the per-test row already stored in DuckDB / store/queries.py)

@dataclass(frozen=True)
class RunReport:
    run_id: str
    total: int
    passed: int
    failed: int
    pass_rate: float            # passed / total
    report_path: str            # the HTML report
    failures: list[TestResult]  # the failing tests only (passed ones omitted)

    def __bool__(self) -> bool:
        return self.failed == 0  # truthy when all passed — `if not runner.score(): ...`
```

## The `error` row (push schema extension)

To support `submit(case, error=...)` symmetrically, the push row schema gains an optional
`error` field. A raw-file push user can write it too:

```jsonl
{"golden_file": "golden/q3.yaml", "error": "agent timed out after 60s"}
```

Scoring an error row: the `execution` dimension fails with `agent error: <error>`; the other
dimensions skip (nothing to grade). This makes "the agent failed to answer" a first-class,
visible, *failing* outcome — not a silent gap. Implementation: `resolve_sql` (or the
adapter) recognises `error` and short-circuits to a failed `AgentResult` carrying the error
text; `check_execution` reports it. **This touches the push adapter + scorer, not just the
SDK** — flagged because it's a (small) contract change, and `push-limitations.md` /
`byo-response-contract` symmetry should be updated.

## Internal flow

```
runner.submit(...)        → append to in-memory list (validated)
runner.score()
  → write results.jsonl   (results/sdk_<ts>.jsonl)
  → config.agent.adapter = push; push.input_file = that file
  → generate_promptfoo_config + _validate_push_submissions + _execute_eval   (CLI path)
  → ingest happens inside _execute_eval (unchanged)
  → query DuckDB for the new run_id → build RunReport
```

The SDK is a **collector + dispatcher**, not a scorer. It cannot produce results that differ
from `score --input`, because it calls the same code.

## What's exported (`src/bi_evals/__init__.py`)

Currently empty. The SDK is the project's first real public surface:

```python
from bi_evals.sdk import Runner, Case, RunReport
__all__ = ["Runner", "Case", "RunReport", "__version__"]
```

## Refactor needed to reuse the CLI path

`score`, `_validate_push_submissions`, and `_execute_eval` currently live in `cli.py` and are
Click-flavoured (raise `ClickException`, `click.echo`). To call them from the SDK cleanly,
extract the non-Click core into a reusable function (e.g. `bi_evals.runner_core.run_push(
config, submissions_or_path) -> run_id`) that both `cli.score` and `Runner.score` call. This
keeps one code path and avoids the SDK importing Click error types. (Small, mechanical; the
CLI command becomes a thin wrapper.)

## Test plan

- `golden_cases()` yields a `Case` per golden, with the right `question`/`golden_file`.
- `submit()` validates: exactly-one-of generated_sql/response_text/error; duplicate raises;
  missing-all raises.
- `score()` writes a well-formed JSONL and dispatches to the push path (mock the DB
  execute / `_execute_eval`); returns a `RunReport` with correct counts.
- `error` row: scores `execution` fail, other dims skip (scorer-level test).
- End-to-end (stubbed DB): a tiny fake agent + 2 goldens → `score()` → `RunReport`.

## Docs re-messaging (part of this work, per eval-landscape.md)

- `getting-started.md`: lead Step 5 with the SDK; raw-file `score --input` becomes the
  "logs-only / non-Python" alternative.
- `push-limitations.md`: note the SDK removes the "build the loop + JSONL" effort; the
  *agent-must-expose-SQL* boundary is unchanged. Add the `error` field.
- `README` "Where bi-evals fits": SDK is the default on-ramp.
- Commit `eval-landscape.md` as the recorded strategy.

## Explicitly out of scope (this slice)

- Thick `run(fn)` convenience (decided against — thin only).
- In-process scoring (decided against — reuse CLI path).
- Async / parallel agent calls (the loop is the customer's; they can parallelise).
- `golden new` scaffolder + zero-cred example (separate friction items from eval-landscape).
