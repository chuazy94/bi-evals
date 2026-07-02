# Instrumenting your agent for bi-evals

This guide is for the person who **builds or owns the BI agent** — not the person authoring
golden tests. It answers one question: *what should my agent emit so bi-evals can score it
with zero massaging?*

bi-evals never runs your agent. Per question, it needs two things from you:

- **`generated_sql`** — the SQL your agent produced. bi-evals grades by executing it.
- **`trace`** (optional) — what the agent did: tools invoked, files/skills read. Needed only
  for the `skill_path_correctness` dimension.

How easily those two things fall out of your agent depends entirely on how it emits its
work. There's a clear quality ladder.

---

## The emission ladder

### Tier 1 — structured return value (best)

Your agent's API/function returns the SQL and trace as **distinct fields**, separate from
any prose answer:

```json
{
  "answer": "Total revenue was $4.2M, led by EMEA.",
  "sql": "SELECT region, SUM(revenue) ...",
  "trace": {
    "tool_calls": [
      {"tool_name": "read_skill_file", "tool_input": {"path": "REVENUE.md"}},
      {"tool_name": "execute_sql", "tool_input": {"query": "SELECT ..."}}
    ],
    "files_read": ["REVENUE.md"]
  }
}
```

Then the SDK loop is one line per field — nothing is parsed or guessed:

```python
answer = my_agent.ask(case.question)
runner.submit(case, generated_sql=answer.sql, trace=answer.trace)
```

This is also exactly what the `api_endpoint` adapter expects
([`byo-response-contract.md`](./byo-response-contract.md)), so a Tier-1 agent can switch
adapters without re-instrumenting.

### Tier 2 — structured audit log (good)

Your agent writes **one JSON record per question** to a log file as it runs:

```jsonl
{"question": "What was total revenue...", "sql": "SELECT ...", "tool_calls": [...], "files_read": ["REVENUE.md"], "ts": "..."}
```

You can then harvest the log into push rows with a small join script — no re-running the
agent, no live agent during scoring. This is the richest *retroactive* source: yesterday's
production answers become today's eval run.

### Tier 3 — prose with fenced SQL (acceptable)

If your agent only returns a prose answer, make sure the SQL appears **verbatim** in it.
Submit the raw answer as `response_text` and bi-evals extracts the SQL using three
strategies, in order:

1. A fenced ` ```sql … ``` ` block ← *make your agent always do this one*
2. Any fenced ` ``` … ``` ` block containing `SELECT`
3. A bare `SELECT … ;` (or `SELECT …` to end of string)

If none match, the row **fails** ("no SQL could be extracted"). The failure mode to design
against: an answer that *describes* the query ("I summed revenue by region from the fact
table") without including it. Prose-mode agents usually also return no trace, so
`skill_path_correctness` can't be scored — drop the dimension or move up a tier.

### Tier 4 — chart-only / prose-only (not scorable)

If the agent never surfaces the SQL it ran — only a rendered chart or a natural-language
summary — **no adapter can score it**. Nothing can grade a query the agent never revealed.
The fix is always the same: instrument the agent to return or log its SQL (most text-to-SQL
agents already show it in their UI, so the data exists — it just needs an exit path).

---

## The trace shape bi-evals understands

The `trace` field is an **open envelope**: submit whatever your agent emits; bi-evals reads
the keys it understands and ignores the rest. Two accepted shapes:

```json
{"trace": [ {"tool_name": "...", "tool_input": {...}}, ... ]}
{"trace": {"tool_calls": [ ... ], "files_read": ["REVENUE.md"]}}
```

Per step, bi-evals reads: `tool_name`, `tool_input` (a dict), and optionally `type`,
`tool_result_preview`, `text`. For `skill_path_correctness` to pass, the golden's
`expected_skill_path.required_skills` are matched against `tool_name` +
`input_contains` on `tool_input` — so **emit the tool name and its input arguments**, not
just a free-text narration of what happened.

`files_read` can be explicit (a top-level list) or implicit: when absent, bi-evals collects
every `tool_input.path` from the steps. If your file-reading tool's argument isn't called
`path`, emit `files_read` explicitly.

---

## What *not* to bother emitting (today)

- **Token counts / cost.** The push adapter does not consume them — push runs report zero
  cost regardless of what the row carries. Log them for your own analysis if you like, but
  don't build mapping code for bi-evals' sake. (Cost reporting is populated only when
  bi-evals runs the model itself, i.e. the dev adapter.)
- **Result sets.** Never send query results — bi-evals executes the SQL itself on its own
  warehouse connection. Only the query and what it touched.

---

## Why structured emission pays twice

The planned OTel adapter (Build Stage 4, [`plans/pivot-phases-overview.md`](./plans/pivot-phases-overview.md)) will
consume OpenTelemetry GenAI spans straight off the agent — the lowest-effort, highest-fidelity
integration. An agent that already emits structured per-question records (Tier 1/2) is one
small mapping away from that; a prose-only agent will face the same instrumentation work
then. Structuring your emission now is the durable investment.

---

## Related

- [`push-limitations.md`](./push-limitations.md) — the failure modes and sharp edges, from
  the eval author's side; includes the push-ready checklist.
- [`byo-response-contract.md`](./byo-response-contract.md) — the exact HTTP response shape
  for the `api_endpoint` adapter (the Tier-1 shape, formalised).
- [`getting-started.md`](./getting-started.md) — the end-to-end setup walkthrough.
