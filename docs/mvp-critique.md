# bi-evals MVP — External Critique

**Reviewer perspective:** Data engineer evaluating bi-evals as a candidate evaluation framework for an in-house SQL-generating BI agent.
**Date:** 2026-05-06
**Surfaces reviewed:** `tmp/my-evals/` demo project, `bi-evals.yaml`, the local viewer at `http://127.0.0.1:8765`, the run report for `eval-Egd-2026-04-27T22:54:47`, the per-test drilldown for `golden/cases/daily-cases-filtered.yaml`, and the relevant scorer / config / Snowflake-client source.

---

## Verdict up front

The bones are good, but the demo run actually surfaces a real correctness bug that would block me from trusting the scores today — see [Bug 1](#1-real-scoring-bug-the-demo-marks-an-obviously-wrong-answer-as-100-pass) below. That's separately why a framework like this matters: I caught it in five minutes from the drilldown, which is exactly the explainability story being sold.

If the dimension-logic bug were fixed, DuckDB-as-target landed for cold-start, and weighted scores were exposed in the report, this would clear my "would I depend on it" bar for an internal BI eval suite. The architecture is the right shape and the trace + drilldown UX is meaningfully better than what I'd build myself in the same time. The friction is concentrated on day 0 (Snowflake setup) and on trust (the demo run scoring an obviously wrong answer as 100%), not on the long-term shape of the tool.

---

## What's genuinely useful

### 1. The 10-dimension breakdown is the right level of resolution
A single pass/fail bit on "did the SQL run and return rows" is what most homegrown eval scripts give you, and it's useless for diagnosis. Splitting into `execution / table_alignment / column_alignment / filter_correctness / row_completeness / row_precision / value_accuracy / no_hallucinated_columns / skill_path_correctness / anti_pattern_compliance`, with critical-vs-diagnostic tiering and per-dimension *reason strings* (e.g. "missing filters: ['STATE']"), is exactly what's needed when triaging a failed regression. The drilldown shows reasons inline — no need to dig.

### 2. The trace is the killer feature
The full multi-turn tool-calling trace (rounds, tool inputs, tool result previews, generated SQL, tokens, latency, cost) being persisted per test and rendered on a single page is the thing most eval tools botch. Promptfoo alone doesn't give this for tool-loop agents; rolling it from scratch would be a week of work.

### 3. Anti-patterns as a first-class dimension is smart
`forbidden_columns: JHU_COVID_19.CASES` for the cumulative-vs-daily trap is the right shape. Bad SQL patterns that *happen to return plausible numbers* are the failure mode that bites in production, and structural assertions catch them where row-comparison won't.

### 4. Persistence + flakiness + cost-vs-median alerts are differentiated
The runs list at `/` shows 27 historical runs with the pass-rate trajectory clearly visible (40% → 0% → 100% climb across April 12–27). That's something I'd actually use. Flakiness tracking across runs (e.g. `golden/joins/us-test-positivity.yaml: 10 runs, 0 flips, 0% pass`) tells me which tests are stable-broken vs. genuinely flaky — a class of insight Promptfoo on its own doesn't give.

### 5. DuckDB as the local store is the right call
Replayable JSON as source-of-truth + DuckDB as the queryable view, ingest is idempotent, no infra. Exactly what an MVP should do.

### 6. Scoping decisions are sane
Framework-not-project (users bring their own skill files), Pydantic for config, `${ENV_VAR}` substitution, auto-load `.env`. Snowflake-only with a `DatabaseClient` protocol so Postgres/BigQuery slot in cleanly later.

---

## What needs more refinement

### 1. Real scoring bug — the demo marks an obviously wrong answer as 100% pass

**Test:** `golden/cases/daily-cases-filtered.yaml`
**Run:** `eval-Egd-2026-04-27T22:54:47`
**Drilldown:** `/runs/eval-Egd-2026-04-27T22:54:47/tests/golden/cases/daily-cases-filtered.yaml`
**Reported result:** pass, score 1.00, every dimension 1.00.

#### Question and what was expected

The golden asks:

> "How many new confirmed COVID-19 cases were there in the United States on 2023-01-15?"

It declares two things the agent must do:

**(a) `expected_skill_path.required_skills`:**
```yaml
- tool: read_skill_file,  input_contains: "SKILL.md"
- tool: read_skill_file,  input_contains: "CASE_TRACKING.md"
- tool: describe_table,   input_contains: "JHU_COVID_19"
```

**(b) `reference_sql`:**
```sql
SELECT sum(DIFFERENCE) AS DAILY_NEW_CASES
FROM COVID19_EPIDEMIOLOGICAL_DATA.PUBLIC.JHU_COVID_19
WHERE CASE_TYPE = 'Confirmed'
  AND COUNTRY_REGION = 'United States'
  AND DATE = '2023-01-15'
```

So: read `CASE_TRACKING.md`, describe `JHU_COVID_19`, query `JHU_COVID_19`.

#### What the agent actually did (from the persisted trace)

- Read `SKILL.md` ✓
- Read **`knowledge/US_STATE_DATA.md`** (not `CASE_TRACKING.md`)
- Described **`NYT_US_COVID19`** (not `JHU_COVID_19`)
- Generated SQL against **`NYT_US_COVID19`**:
  ```sql
  SELECT DATE, SUM(CASES_SINCE_PREV_DAY) AS total_new_confirmed_cases
  FROM COVID19_EPIDEMIOLOGICAL_DATA.PUBLIC.NYT_US_COVID19
  WHERE DATE = '2023-01-15'
  GROUP BY DATE;
  ```

Wrong knowledge file, wrong table, wrong column. The agent answered a related question ("US new cases on that date from NYT data, summed across all states") instead of the asked question ("US new confirmed cases from JHU data filtered by `CASE_TYPE = 'Confirmed'` and `COUNTRY_REGION = 'United States'`"). It also dropped the `CASE_TYPE = 'Confirmed'` filter that the golden's anti-pattern hint flags as load-bearing.

#### What the scorer reported

Every dimension passes with score 1.00. The two dimensions that should have caught this both produced wrong verdicts, with reason strings that look like they were computed from the *reference* SQL rather than the *generated* SQL:

| Dimension | Reported reason | Reality |
|---|---|---|
| `skill_path_correctness` | "All 3 required skills invoked correctly" | `CASE_TRACKING.md` and `describe_table(JHU_COVID_19)` were never invoked |
| `table_alignment` | "All reference tables present: `['…JHU_COVID_19']`" | Generated SQL uses `NYT_US_COVID19`, not `JHU_COVID_19` |
| `column_alignment` | "All required source columns present: `['DIFFERENCE']`" | Generated SQL doesn't reference `DIFFERENCE` at all (uses `CASES_SINCE_PREV_DAY`) |
| `filter_correctness` | "Filter structure matches: `[('CASE_TYPE', 'EQ'), ('COUNTRY_REGION', 'EQ'), ('DATE', 'EQ')]`" | Generated SQL only filters on `DATE`; missing `CASE_TYPE` and `COUNTRY_REGION` |

Each reason string describes a property of the **reference SQL**, not the **generated SQL** — so each dimension passes regardless of what the agent produced.

The remaining dimensions' passes are explainable but symptomatic of the same root issue: row-comparison can't tell that the agent used the wrong data source. NYT and JHU are different sources for the same underlying pandemic, so a sum-by-date from NYT and a `SUM(DIFFERENCE)` from JHU land in the same ballpark for a given day — `row_completeness` / `row_precision` / `value_accuracy` all pass with "1/1 rows match" because the numbers are close enough.

#### Why this matters

Three to four independent dimensions appear to be analyzing the reference SQL rather than the generated SQL. Whichever the actual code bug is (looking at `src/bi_evals/scorer/dimensions.py` would pin it down — likely an argument-order swap or a copy-paste between `generated_sql` and `reference_sql`), the user-visible effect is the same: **the framework greenlit a query that hit the wrong table, dropped two filters, and skipped the required skill path.** That's the exact failure mode the 10-dimension scorer is supposed to prevent.

The fix is probably small but the trust impact is large: every "100% pass" run in the runs list — and there are many — is now suspect until those dimensions are re-verified against their drilldown traces.

**Recommended fix:** A regression test that loads this exact golden + this exact trace and asserts the four dimensions return `passed=False` with sensible reasons. Lock the fix in before shipping anything else.

---

### 2. Snowflake setup is the biggest adoption blocker

The current path requires: a Snowflake account, a service user, key-pair auth (PEM key generated, public half registered to the user, passphrase managed), warehouse + database + schema names, and seven env vars. That's table-stakes for a real eval suite, but it's a 30-minute ceremony before the first green checkmark.

Three asks:

- **A DuckDB or SQLite `database.type` for first-run.** Already on the Deferred list — promote it. Ship the COVID parquet/CSVs in `examples/covid-19/` so `bi-evals init --example covid` → `bi-evals run` works zero-cred. The first run shouldn't require a warehouse.
- **Snowflake SSO / `externalbrowser` auth.** Also on the deferred list. Most data engineers have SSO, not a service account with a key pair lying around. Currently `SnowflakeClient.__init__` (`src/bi_evals/db/snowflake.py:36-41`) hard-fails without `private_key_path` — there's no fallback path.
- **A `bi-evals doctor` command** (also deferred) to validate `${SNOWFLAKE_*}` vars resolve, key path readable, key passphrase decrypts, account reachable, warehouse usable, `ANTHROPIC_API_KEY` set — *before* burning API credits on a run that fails on test 1. Highest-ROI command not yet built.

### 3. `bi-evals.yaml` has duplicate `scoring:` keys in the demo

In `tmp/my-evals/bi-evals.yaml`, both line 33 and line 70 define `scoring:`. YAML silently lets the second one win, which would drop `dimensions`, `thresholds`, `critical_dimensions`, `dimension_weights`, and `pass_threshold` on the floor. Either Pydantic is rescuing this with defaults or the config the run actually used is not what the file appears to say. Either way: a config linter (or just `yaml.safe_load` with strict-duplicate-key detection) would catch it.

Combined with `${ENV_VAR}` silently resolving missing vars to empty string (`src/bi_evals/config.py:17-21`), the config layer is too forgiving. A missing env var produces an empty string that propagates into the Snowflake connector and yields a confusing error several layers down.

### 4. The scoring model is opaque from the report

The report shows per-dimension pass rates but not the **weighted score → pass_threshold** computation that actually drove the pass/fail. With weights `execution=3, value_accuracy=3, row_completeness=3, …` and `pass_threshold=0.75`, a test can fail two diagnostic dims and still pass; a non-data-engineer reading the report can't reconstruct that.

Either show the weighted score per test, or add a tooltip on the pass pill explaining e.g. "passed: all critical dims green AND weighted=0.91 ≥ 0.75".

### 5. The runs list has no triage affordances

27 rows of `eval-xru-2026-04-12T21:40:14`-style mono-spaced IDs, no filter by pass-rate band, no "show only regressions vs. previous", no diff column. The "Compare prev → latest" shortcut is good but I want to A/B any two runs without checkboxing through 27 rows. A `?since=7d` filter and a "regressed since" badge would carry a lot of weight.

### 6. Token count shows 0 on the run summary

Run summary stat block shows **Tokens: 0**, but the "Cost by model" table shows **23,175 tokens** and the trace for a single test shows **7,668 tokens**. The aggregation in `src/bi_evals/report/builder.py` is summing the wrong field somewhere. Easy fix, but it undermines trust in the surrounding numbers.

### 7. Authoring goldens is still the long tail

YAML by hand is fine for 3 tests, painful at 50. The deferred SPA with golden authoring + production-traffic import (PostHog/Langfuse) is the right roadmap but is the gap between "demo" and "platform I'd commit a team to." A `bi-evals golden new --question "..." --reference-sql @file.sql` scaffolder would bridge it before then.

### 8. Missing a Pillar 2 placeholder hurts the pitch

The MVP doc explicitly scopes out Faithfulness (does the natural-language summary match the rows?) and Confidence (pass^k). For a SQL-generating BI agent, **Faithfulness is the bigger trust gap than SQL accuracy** — the hallucination happens in the prose, not the WHERE clause. The framework already captures the prose in the trace; not even a stub LLM-rubric dimension feels like a missed near-term win.

---

## Suggested priority order

1. Fix the dimension-logic bug ([1](#1-real-scoring-bug-the-demo-marks-an-obviously-wrong-answer-as-100-pass)) and add a regression test that pins the four dimensions against the existing `eval-Egd-…` trace.
2. Re-verify every "100% pass" historical run on the runs list against its drilldown — these scores are suspect until the four dimensions are corrected.
3. DuckDB-as-target + a bundled COVID example with no creds required ([2](#2-snowflake-setup-is-the-biggest-adoption-blocker)).
4. `bi-evals doctor` for pre-run validation ([2](#2-snowflake-setup-is-the-biggest-adoption-blocker)).
5. Strict-duplicate-key YAML loading + fail-fast on unresolved `${ENV_VAR}` ([3](#3-bi-evalsyaml-has-duplicate-scoring-keys-in-the-demo)).
6. Show weighted score per test in the report ([4](#4-the-scoring-model-is-opaque-from-the-report)).
7. Token aggregation fix ([6](#6-token-count-shows-0-on-the-run-summary)).
8. Stub Faithfulness dimension (LLM-as-judge over the prose response in the trace) ([8](#8-missing-a-pillar-2-placeholder-hurts-the-pitch)).
