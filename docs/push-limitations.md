# Push adapter — limitations & what the user must provide

The **push** adapter (`bi-evals score --input results.jsonl`) scores results your own
agent already produced. bi-evals never calls your agent, so it can only work with what
you put in the submission file. This doc lists exactly what push needs, the sharp edges,
and how to satisfy each — so you know up front whether your agent is push-ready.

> TL;DR: push works only as well as **what your agent surfaces about its own work**. If it
> exposes its generated SQL (and, optionally, its tool/file trace), push is light work. If
> it hides the SQL behind a chart or a prose-only answer, you must surface it first — and
> no adapter can work around that.

---

## What you submit

One JSON object per line in a `.jsonl` file, one row per golden test:

```jsonl
{"golden_file": "golden/cases/revenue.yaml", "generated_sql": "SELECT ...", "trace": {...}}
{"golden_file": "golden/cases/usage.yaml",   "response_text": "Here's the query:\n```sql\nSELECT ...\n```", "trace": {...}}
```

| Field | Required? | Purpose |
|---|---|---|
| `golden_file` | **yes** | Path (relative to the config dir) of the golden this row answers. The join key. |
| `generated_sql` **or** `response_text` | **one of** | The SQL to score. `generated_sql` = pre-extracted SQL; `response_text` = the agent's raw answer (SQL extracted from it). |
| `trace` | optional | What the agent did (tool calls / files read). Needed for `skill_path_correctness`. |

---

## Hard requirements (push fails without these)

### 1. Your agent must expose its generated SQL

This is the real precondition, and it's the same for every adapter — push, api_endpoint,
OTel. bi-evals scores SQL by **executing it**; it cannot grade a query the agent never
revealed. If your agent only returns a natural-language summary or a rendered chart and
never surfaces the SQL it ran, push cannot score it. **Fix:** instrument your agent to
return/log the SQL (most text-to-SQL agents already show it in their UI).

### 2. The SQL must be *extractable* from what you submit

If you submit `response_text` (the raw answer), bi-evals extracts the SQL using three
strategies, in order:

1. A fenced ` ```sql … ``` ` block.
2. Any fenced ` ``` … ``` ` block that contains `SELECT`.
3. A bare `SELECT … ;` (or `SELECT …` to end of string).

If none match, that row **fails** with: *"has a response_text but no SQL could be
extracted."* So your agent's answer must contain the SQL in one of those forms. If it
describes the query in prose without including it verbatim, extraction fails. **Fix:**
either submit `generated_sql` directly (pre-extracted), or ensure the agent fences its
SQL.

### 3. One submission per golden; both fields can't be missing

- Each `golden_file` may appear **at most once** — a duplicate fails validation (the
  second row would silently overwrite the first).
- A row with neither `generated_sql` nor `response_text` fails: *"missing both."*
- Every selected golden must have a matching submission row, or `score` stops before
  running with a clear "no submission for these goldens" list.

### 4. The warehouse must be reachable

bi-evals runs both your generated SQL and the golden's `reference_sql` against your
database to compare results. The `database:` block in `bi-evals.yaml` must have working
credentials. (`bi-evals doctor` checks this with a real `SELECT 1`.)

---

## Sharp edges (push *runs*, but results can surprise you)

### A. Column **order** must match the golden's reference — when names don't

bi-evals matches result rows by column name. But a black-box agent names its output
columns however it likes (`nation_name` vs the golden's `NATION`). To avoid failing a
correct answer over cosmetic naming, the scorer falls back to matching **by ordinal
position** when names don't line up. This rescues the common case — but it introduces an
assumption:

- **Position fallback only triggers when the column *counts* match.** Same number of
  output columns in the generated and reference result → paired by position. Different
  counts → no remap, and the comparison fails honestly (we can't safely guess the pairing).
- **It assumes both queries list columns in the same logical order.** If your agent emits
  `SELECT count, nation` while the golden reference is `SELECT nation, count`, position
  matching pairs them wrong and you get silently incorrect comparisons. Two queries
  answering the same question usually agree on column order, but it isn't guaranteed.

**What the user should do:** make your golden's `reference_sql` output columns in the
order your agent naturally produces them, and keep the **same number** of output columns.
If you can, set `row_comparison.key_columns` explicitly in the golden so matching doesn't
depend on guessing.

### B. `skill_path_correctness` needs a real trace in the submission

This dimension checks that the agent invoked the expected tools/skills. It reads the
submitted `trace` for steps shaped like:

```json
{"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}}
```

and matches each required skill by `input_contains`. **If you don't submit a `trace`, or
your trace doesn't carry these fields, this dimension fails** with "missing skill
invocations" — even though the agent may well have read the files. This is the most common
surprise: an agent answering in prose-only mode returns no trace over HTTP, so the
submission has none.

**What the user should do:** capture the agent's tool/file trace and put it in the `trace`
field. The trace is an *open envelope* — submit whatever your agent emits; bi-evals reads
the keys it understands. Accepted shapes:

- `"trace": [ {step}, {step} ]` — a list of step dicts, or
- `"trace": {"tool_calls": [ {step}, … ], "files_read": ["SKILL.md", …]}` — a dict with a
  `tool_calls`/`trace` list and/or an explicit `files_read`.

If your agent has no notion of tools/files, drop `skill_path_correctness` from
`scoring.dimensions` (it's diagnostic/non-critical, so it won't gate the test, but it will
show as a failure otherwise).

### C. `required_columns` means **source** columns, not output names

The `column_alignment` dimension checks the *source* columns a query reads, not its result
names. A golden whose `required_columns` lists output aliases (e.g. `GROSS_REVENUE`,
`VERY_BIG_BUCKET`) will fail even on a correct query. (bi-evals now detects this and prints
a hint.) **What the user should do:** list the underlying table columns the answer is
computed from (e.g. `L_EXTENDEDPRICE`, `L_DISCOUNT`), not the alias names.

### D. Cost / token / model metadata is mostly absent in push

Because bi-evals didn't run the model, it has no token counts or cost unless your
submission carries them. Cost reports and cost alerts will be empty/zero for push runs.

---

## How to produce the submission file

push doesn't care *how* `results.jsonl` is created — only that the rows are well-formed.
Three realistic ways, in rough order of effort:

1. **Harvest an existing audit log.** If your agent already logs each call (SQL + trace) to
   a file, write a small script that joins those records to the golden questions and emits
   push rows. This is the richest source and re-runs nothing. (The `mock-bi-agent` demo
   does exactly this — it writes a structured record per `/ask` to a JSONL log, and a
   capture script reshapes it.)
2. **Loop over the goldens and call your agent.** Read each golden's `question`, send it to
   your agent, collect the SQL + trace, write a row. A `for`-loop around the agent you
   already have.
3. **Hand-write it.** For a handful of goldens, paste your agent's answers into
   `response_text` rows by hand. Fine for a first smoke test.

Whatever the method, the customer's real work is the **mapping**: taking your agent's
output (which is shaped however it's shaped) and emitting `{golden_file, generated_sql |
response_text, trace?}`. The "contract" is just that target shape — richer adapters (OTel)
shrink the mapping but never remove it.

---

## Checklist — is my agent push-ready?

- [ ] Agent exposes the **SQL** it generated (return value, log, or fenced in its answer).
- [ ] SQL is **extractable**: submitted as `generated_sql`, or fenced/bare-SELECT in `response_text`.
- [ ] (For `skill_path_correctness`) agent exposes its **tool/file trace**, submitted in `trace`.
- [ ] Warehouse credentials in `bi-evals.yaml` work (`bi-evals doctor` passes).
- [ ] Goldens' `reference_sql` output columns are in an order (and count) the agent matches,
      or `key_columns` is set.
- [ ] Goldens' `required_columns` list **source** columns, not output aliases.
- [ ] One submission row per golden; every selected golden has a row.

---

## Related

- `docs/pivot-phase-3-design.md` — the push adapter design.
- `docs/pivot-phases.md` — where push sits among the adapters; why OTel is the
  lower-effort, higher-fidelity future path.
- `docs/golden-tests-guide.md` — golden authoring (`required_columns`, `key_columns`,
  `expected_skill_path`).
