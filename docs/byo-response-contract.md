# BYO Response Contract

This document is the authoritative spec for the JSON response that a BYO endpoint must return to bi-evals. If you're setting `agent.type: api_endpoint` in `bi-evals.yaml`, your endpoint needs to conform to this contract for scoring to work as expected.

Machine-readable version: [`src/bi_evals/byo_response_schema.json`](../src/bi_evals/byo_response_schema.json) (JSON Schema 2020-12). The schema is bundled inside the package so `bi-evals doctor` can load it via `importlib.resources` at runtime — it validates against this exact file.

Validate your endpoint against it: `bi-evals doctor`.

---

## What bi-evals sends

bi-evals POSTs to the URL configured at `agent.endpoint.url`:

```http
POST {agent.endpoint.url}
Content-Type: application/json
{any headers from agent.endpoint.headers, including bearer auth}

{"question": "What was total shipped revenue in 2024?"}
```

The HTTP `method` (defaults to `POST`) and `timeout` (defaults to 60s) are configurable.

---

## What your endpoint must return

JSON, HTTP 200. Two kinds of fields:

- **Required** — bi-evals can't score without it.
- **Optional** — enables additional scoring dimensions. Absence is silent; the corresponding dimension is skipped, not failed.

### Required: SQL must be retrievable

Either set the `sql` field directly:

```json
{"sql": "SELECT SUM(REVENUE) FROM ..."}
```

…or include a fenced SQL block inside `text`. bi-evals will extract it:

```json
{"text": "Revenue was $4.2M.\n```sql\nSELECT SUM(REVENUE) FROM ...\n```"}
```

If neither produces SQL, **every scoring dimension fails** — bi-evals has nothing to execute against your warehouse.

### Optional: structured fields that unlock more scoring

| Field | Type | Unlocks | Notes |
|---|---|---|---|
| `text` | `string` | answer-text display in the viewer | If absent, bi-evals uses the raw response body as the text. |
| `files_read` | `string[]` | file-attribution checks in `skill_path_correctness` | If absent but `trace` is present, bi-evals derives it from `trace[].tool_input.path`. |
| `trace` | `TraceStep[]` | `skill_path_correctness` scoring (sequence of tool calls) | Each step represents one tool call or one text emission. |

#### `TraceStep`

```json
{
  "type": "tool_use" | "text",
  "tool_name": "read_skill_file",
  "tool_input": {"path": "knowledge/REVENUE.md"},
  "tool_result_preview": "...",
  "text": "...optional reasoning text..."
}
```

`type` is the only required field. `tool_name` and `tool_input` are required when `type` is `"tool_use"`. For file-reading tools, include `tool_input.path` — that's what bi-evals harvests into `files_read`.

---

## Three canonical examples

### Minimum — `sql` only

```json
{
  "sql": "SELECT SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT)) AS GROSS_REVENUE FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM JOIN ... WHERE YEAR(O_ORDERDATE) = 1996"
}
```

Scores: 8/9 dimensions. `skill_path_correctness` skips (no trace).

### Medium — adds `text` and `files_read`

```json
{
  "text": "Gross revenue in 1996 was $5.6B.",
  "sql": "SELECT ...",
  "files_read": ["SKILL.md", "knowledge/REVENUE.md"]
}
```

Scores: 9/9 dimensions when `expected_skill_path` exists in the golden (file-attribution check works; sequence check skips without trace).

### Full — adds the full `trace`

```json
{
  "text": "Gross revenue in 1996 was $5.6B.",
  "sql": "SELECT ...",
  "files_read": ["SKILL.md", "knowledge/REVENUE.md"],
  "trace": [
    {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "SKILL.md"}, "tool_result_preview": "# TPCH SF10 Reporting..."},
    {"type": "tool_use", "tool_name": "read_skill_file", "tool_input": {"path": "knowledge/REVENUE.md"}, "tool_result_preview": "# Revenue — TPCH..."},
    {"type": "tool_use", "tool_name": "describe_table", "tool_input": {"table": "SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM"}, "tool_result_preview": "{\"columns\": [...]}"},
    {"type": "text", "text": "I have the schemas I need. Here is the SQL:"}
  ]
}
```

Scores: 9/9 dimensions, including full sequence-sensitive `skill_path_correctness`.

---

## Renaming the SQL / text fields

If your existing endpoint already returns these values under different field names, **don't change your endpoint** — override the keys in `bi-evals.yaml`:

```yaml
agent:
  type: api_endpoint
  endpoint:
    url: ...
    response_sql_key: "answer.query"       # dot-notation supported for nested responses
    response_text_key: "answer.summary"
```

`response_sql_key` and `response_text_key` accept dot-notation paths, so a response like `{"answer": {"query": "SELECT ...", "summary": "..."}}` works without restructuring.

**Note:** the `trace` and `files_read` paths are *not* configurable. If you want skill-path scoring, your endpoint must place those arrays at the top level of the response under those exact names.

---

## Scoring coverage

| Dimension | Needs from response | Default critical? |
|---|---|---|
| `execution` | `sql` | yes |
| `row_completeness` | `sql` | yes |
| `value_accuracy` | `sql` | yes |
| `row_precision` | `sql` | no |
| `column_alignment` | `sql` | no |
| `table_alignment` | `sql` | no |
| `filter_correctness` | `sql` | no |
| `no_hallucinated_columns` | `sql` | no |
| `skill_path_correctness` | `trace` (or `files_read`, depending on the golden's `expected_skill_path` shape) | no |

If your golden's `expected_skill_path` only specifies `required_skills` by file name, `files_read` alone is sufficient. If it specifies an ordered sequence (`sequence_matters: true`), you need the full `trace`.

---

## Common mistakes

- **Wrong field name for SQL or text.** Defaults are `"sql"` and `"text"`. If you return `{"answer": "...", "query": "..."}`, override via `response_sql_key` / `response_text_key` — don't expect bi-evals to guess. The PR #21 review caught a doc example using `"answer"` against the default `"text"`; the same bug in production silently empties the text field in the viewer.

- **Returning HTML on errors.** A 500 page from your reverse proxy is not JSON. `bi-evals run` will fail every test with a JSON parse error. Return 200 with an `{"error": "..."}` body if your agent fails internally, or let the HTTP error surface and rely on the eval to flag the run.

- **No SQL anywhere in the response.** Even with `text` populated, if there's no fenced ```sql block bi-evals can't extract anything. Every dimension fails. Run `bi-evals doctor` to catch this before a paid eval run.

- **`trace` items missing `type`.** The schema requires it. Steps without `type` are ignored silently, which often makes a skill-path test fail when you thought you'd provided the trace.

- **`response_sql_key` pointing at a missing nested field.** `_get_nested` returns `None` silently — your `sql` becomes empty, every dimension fails. Pre-check with `bi-evals doctor`.

---

## Verifying your endpoint

```bash
bi-evals doctor
```

POSTs a synthetic question to your configured endpoint and reports which required and optional fields are present, plus the resulting scoring coverage. Run it before every fresh eval against a new endpoint shape.
