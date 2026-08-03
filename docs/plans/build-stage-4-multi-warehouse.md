# Multi-warehouse support (design / proposal)

> **Status: proposal. Sequenced as a near-term priority — a conscious override of the MVP north star.**
> `CLAUDE.md` deprioritizes "new provider integrations" for MVP, and warehouse breadth does *not* make
> the current Snowflake user's first run faster. It makes bi-evals **usable at all** for the
> Databricks/Genie (and later BigQuery/Looker) audience — adoption *breadth*, not first-run *ergonomics*.
> The project owner has explicitly chosen breadth as the strategic bet (reaching those users outweighs
> polishing the Snowflake first-run), so this is recorded as a deliberate override, not a default yes.
> This **replaces** the vague Stage 9 (Deferred) bullet *"additional warehouses (Postgres/BigQuery/Redshift/Databricks)"*,
> which hid the dialect-plumbing prerequisite below.

## The gap, precisely

bi-evals executes both `reference_sql` and the agent's `generated_sql` on its **own** DB connection and
compares results — execution accuracy is the yardstick. Today that connection can only be **Snowflake**:
`db/factory.py` raises `ValueError` for any other `database.type`. The three *critical* dimensions
(`execution`, `row_completeness`, `value_accuracy`) all run through the DB client, so a Databricks/Genie
shop cannot run bi-evals at all — not "with reduced fidelity," but not at all.

Naively this looks like "add a DB client." It is actually **two coupled pieces**, and the second is the
one the old Stage 9 (Deferred) one-liner hid:

## Part 1 — Thread `dialect` from config into the scorer (the real work)

The scorer parses SQL with sqlglot at a **hardcoded Snowflake dialect**. Every function in
`scorer/sql_utils.py` — `extract_tables`, `extract_select_columns`, `extract_output_aliases`,
`extract_where_*`, and `provider/contract.py`'s `extract_sql` — defaults `dialect: str = "snowflake"`.
Worse, the **call sites in `scorer/dimensions.py` pass no dialect at all** (e.g.
`extract_tables(generated_sql)` at `dimensions.py:100`), so they silently ride that default. They don't
currently even have a dialect value in scope to pass.

Consequence: the moment a Databricks agent emits Spark SQL — or a golden's `reference_sql` is written in
Spark SQL — sqlglot parses it against the wrong grammar. Table/column/filter extraction degrades or
throws, and the **structural dimensions** (`table_alignment`, `column_alignment`, `filter_correctness`,
`no_hallucinated_columns`, `anti_pattern_compliance`) misfire. `execution`/`row_completeness`/
`value_accuracy` still work (they run SQL, they don't parse it), but the diagnostic layer goes quietly
wrong — the worst failure mode: green where it should be red.

**Scope of Part 1 (warehouse-neutral; benefits every future warehouse, not just Databricks):**

1. Map `database.type` → sqlglot dialect (`snowflake` → `snowflake`, `databricks` → `spark`/`databricks`,
   `bigquery` → `bigquery`, …). A small dict, one place. Confirm each target's actual sqlglot dialect
   name (sqlglot has `databricks` and `spark` — decide which; likely `databricks`).
2. Plumb that dialect from config **to the scorer call sites**. The scorer already loads `BiEvalsConfig`;
   the dialect needs to reach `dimensions.py` and be passed into every `sql_utils` call. Decide the
   mechanism: explicit argument threading vs. resolving once in the scorer entry and passing down. Prefer
   explicit — no module-level global.
3. Keep `"snowflake"` as the default **only** at the config-resolution boundary (back-compat for existing
   projects), never buried in `sql_utils` signatures. Remove the per-function hardcoded defaults, or make
   them assert-only, so a missing dialect fails loudly instead of silently parsing as Snowflake.
4. Tests: parse a Spark-dialect query through each `sql_utils` function and assert extraction is correct
   where the Snowflake dialect would have mangled it.

**Part 1 is a prerequisite for every warehouse**, and it delivers value the instant it lands: even before
a Databricks *client* exists, a user could write Spark-dialect goldens and have them scored correctly if
they execute elsewhere. This is the piece worth doing carefully.

## Part 2 — `DatabricksClient` (first consumer of Part 1)

Mechanically small, given the `DatabaseClient` protocol is tiny (`execute(sql) -> QueryResult`, `close()`):

1. `db/databricks.py` — `DatabricksClient` implementing the protocol via `databricks-sql-connector`
   (server hostname / HTTP path / access token, or OAuth). `execute` returns the same `QueryResult`
   (columns/rows/row_count/error); **catch and set `.error`, never raise** (protocol contract).
2. `db/factory.py` — one `elif config.type == "databricks":` branch.
3. `config.py` — `DatabaseConnection` already holds a connection dict; confirm it carries the Databricks
   fields (host, http_path, token) or extend it. Keep `${ENV_VAR}` substitution working.
4. `doctor.py` — a Databricks connectivity check mirroring the Snowflake one (the whole point of `doctor`
   is "catch setup errors before spending anything," and a new warehouse is exactly where setup breaks).
5. Dependency hygiene: `databricks-sql-connector` as an **optional/extra** dependency (like the
   `anthropic_tool_loop` dev deps), so a Snowflake-only user doesn't pull it in.
6. Docs: `.env.example`, getting-started, and a golden or note in `tmp/my-evals/` per CLAUDE.md's
   live-project rule — though a Databricks demo needs a real Databricks workspace, so the smoke test is
   gated on that. Flag in the PR which commands to run.

## Sequencing

1. **Part 1 first, standalone.** It is the prerequisite, it's warehouse-neutral, and it can merge and be
   tested with zero Databricks credentials (Spark-dialect parse tests). Do not fold it into Part 2.
2. **Part 2 second.** Small once Part 1 exists. Its end-to-end smoke test needs a real Databricks
   workspace — surface that as the demand/verification gate.
3. Later warehouses (BigQuery/Postgres/Redshift) become **pure Part-2-shaped adds** once Part 1 is done —
   new client + factory `elif` + dialect-map entry. That's the payoff of scoping Part 1 separately.

## Open questions

- **sqlglot dialect for Databricks** — `databricks` vs `spark`; confirm which handles the SQL Genie
  actually emits (Genie compiles to Databricks SQL / Spark SQL). Test against real Genie output.
- **Dialect of the *reference* vs the *generated* SQL** — normally the same (both target the configured
  warehouse), but if a golden's `reference_sql` is authored in one dialect and the agent emits another,
  the scorer needs one canonical parse dialect. Assume "the configured warehouse dialect for both" and
  document it; revisit only if a cross-dialect case appears.
- **Auth surface** — Databricks PAT vs OAuth M2M; start with token (simplest for `doctor`), add OAuth on
  demand — same staged approach Snowflake key-pair took.
- **Ties to the semantic-layer plan** — Databricks is a Stage 6 target too (Unity Catalog metric views).
  Part 1's dialect threading is a shared prerequisite; note the overlap so the two plans stay aligned.
