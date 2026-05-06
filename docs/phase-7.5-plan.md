# Phase 7.5: Viewer Enhancements — Failure Reasons, Drilldown, Filters, Multi-Project

## Context

Phase 7 shipped a minimal local viewer (`bi-evals ui`): runs list, single-run report, compare view, ~3 days of work. It removed the friction of generating HTML files manually.

Real usage immediately surfaced four gaps:

1. **No failure reasons.** A test shows "score = 0.5" with no indication of *why*. The `fail_reason` and per-dimension `reason` text is in the DB; the report just doesn't render it.
2. **No per-test drilldown.** Can't see the actual SQL the agent generated, the trace of which skill files it read, or the dimension-by-dimension breakdown for a single failing test.
3. **No filtering.** A run with 50 tests across 5 categories renders as one giant blob. To investigate the `cases` category, you scroll.
4. **No multi-project view.** The runs list mixes all projects together. Today this is fine because users have one DuckDB per project, but as soon as a user runs evals against two projects they'll want to switch between them.

This phase adds these four things while staying inside the same architectural envelope as Phase 7: server-rendered Jinja, no JS framework, no build step. The reason this stays small: **the data is already in the store.** Almost every gap is "the template doesn't render a field that's already in the DB."

This phase is explicitly **disposable scaffolding.** When golden authoring lands (Phase 8+), the entire `ui/` package gets rebuilt as a SPA against the same backend. Don't introduce abstractions, design tokens, or component primitives that try to outlive the rewrite.

---

## Goals

1. **Surface failure reasons** so users can debug without leaving the UI.
2. **Per-test drilldown** showing generated SQL, full trace, dimension-level reasons.
3. **Server-side filtering** by category and model on the single-run view.
4. **Multi-project switching** on the runs list.

Success: a user looking at a failed run can click into a single test, read why each dimension failed, see the generated SQL, and jump back to filter the run by category to see related failures — without opening a terminal or refreshing manually.

---

## Non-goals

- Charts / trend graphs over time. Useful eventually; not in this phase.
- JavaScript frameworks, build steps, design systems.
- Editing data in the UI (golden authoring, run triggering, deletion). Read-only.
- Authentication / multi-user. Localhost-only, single user.
- Aggregating across multiple DuckDB files. Multi-project means "switch between projects within one DB" — see Q1 below.
- A SQL diff viewer (Monaco, etc.). Plain `<pre>` blocks for v1; if reading SQL becomes painful, that's a separate phase.
- Refactoring `report/builder.py` to be more "API-like" preemptively. When the SPA rebuild needs JSON endpoints, we write JSON endpoints then.
- Per-test history charts. Out of scope; lives with the future trend-charts work.

---

## Design decisions to lock in upfront

### Q1: How does multi-project work given today's data model?

A DuckDB store is per-project today: each `bi-evals.yaml` points at its own `storage.db_path`, and `runs.project_name` is set from `project.name` in that config. Multiple projects in one DB is *technically* supported (same `project_name` column distinguishes them) but in practice it doesn't happen — every user we've imagined has one DB per project.

**Decision:** v1 of multi-project is a **`project_name` filter on the runs list**, not a multi-DB aggregator. If a user has two projects in one DB, the dropdown lets them filter. If they have two projects in two DBs, they restart the UI pointing at the other one. We document the second case clearly. This matches the "disposable scaffolding" framing — multi-DB aggregation belongs in the SPA.

### Q2: Where does filtering happen — runs list or single-run view?

**Both, but they mean different things:**
- Runs list: filter by `project_name`. Affects which rows render.
- Single-run view: filter by `category` and/or `model`. Affects which sections of the report render. Implemented as query string params on `/runs/{id}` (`?category=cases&model=sonnet`); links from filtered views preserve the filter.

### Q3: Drilldown URL

`/runs/{run_id}/tests/{test_id}` for single-model runs. For multi-model: `/runs/{run_id}/tests/{test_id}?model=<model>`. URL-quote `test_id` since it can contain slashes (e.g. `cases/daily-cases-filtered`).

### Q4: Failure reasons — where do they live in the report?

Two places, in order of importance:
- **Per-test rows in the existing tables**: add a "Reason" column or expandable row showing `fail_reason` (truncated to ~100 chars; full reason in the drilldown).
- **A new "Failures" section** at the top of the single-run view listing every failed test with its `fail_reason`. Sorted by category, then by test_id. This is the "what went wrong on this run?" answer at a glance.

### Q5: Drilldown content

The `/runs/{id}/tests/{test_id}` page shows, in order:
1. Test metadata: id, category, difficulty, question, model
2. Pass/fail verdict + score
3. `fail_reason` (if any)
4. Per-dimension table: dimension, passed, score, weight, `reason`
5. Generated SQL (`<pre>` block)
6. Reference SQL (`<pre>` block)
7. Files the agent read (`files_read` list)
8. Full trace (collapsed `<details>` block with the raw `trace_json` pretty-printed)

No per-test history chart. No SQL diff. Both are SPA-era features.

### Q6: How do filters interact with the auto-refresh?

The runs list auto-refreshes every 10s (Phase 7). Single-run views and drilldown views **do not** — they're snapshots. Filters survive the runs-list refresh because they're in the URL.

---

## Work items

### 1. Failure reasons in the single-run report (~half day)

Modify the existing single-run view to surface `fail_reason` and dimension `reason` text.

**Code:**
- `src/bi_evals/report/templates/report.html.j2` — add a "Failures" section at the top listing failed tests with their `fail_reason`. Skip the section entirely if no failures. Add a "Reason" column to the per-test table (or render the reason inline below each failed row).
- `src/bi_evals/report/builder.py` — `build_report_html` currently doesn't pass per-test failure detail to the template. Add a query helper or use existing `list_tests` + filter by `passed=False`, and pass to template.
- `src/bi_evals/store/queries.py` — likely no change; `list_tests` already returns `fail_reason`. Verify.

**Tests:**
- `tests/test_report_builder.py` — add a case asserting that `fail_reason` text appears in the rendered HTML when a test in the fixture failed.

### 2. Per-test drilldown page (~1 day)

New route on the FastAPI app. New Jinja template. Reuses existing `store.queries` helpers — no new query logic except a single fetch of one row from `test_results` joined with its `dimension_results`.

**Code:**
- `src/bi_evals/store/queries.py` — add `get_test(conn, run_id, test_id, model=None) -> TestRow` (single-row variant of `list_tests`); already have `list_dimensions` for the per-dim breakdown.
- `src/bi_evals/ui/server.py` — add `GET /runs/{run_id}/tests/{test_id}` route. Reads optional `?model=` query param. Returns 404 if not found.
- `src/bi_evals/ui/templates/test_detail.html.j2` — new template per Q5 above.
- The single-run report and runs list link test-id cells to the drilldown URL.

**Tests:**
- `tests/test_ui.py` — add cases:
  - 200 + correct content for an existing test
  - 404 for unknown test_id
  - Multi-model run: model query-param disambiguation works
  - Drilldown shows generated SQL + dimension reasons

### 3. Filtering on single-run view (`?category=...&model=...`) (~1 day)

The existing `build_report_html` produces one monolithic blob. Refactor it to accept optional `category` and `model` filters that prune `categories`, `dimensions`, `models`, and the per-test list before rendering.

**Code:**
- `src/bi_evals/report/builder.py` — add `category: str | None = None` and `model: str | None = None` kwargs to `build_report_html`. Filter the data structures it builds before rendering. Stale-goldens / cost-alert / freshness sections render unfiltered (they're run-level, not category-level).
- `src/bi_evals/report/templates/report.html.j2` — add a filter strip at the top: "Category: [All ▼]  Model: [All ▼]  [Clear filters]". The dropdowns are plain `<select>` elements wrapped in a `<form method="get">` that submits to `/runs/{id}` — server-side, no JS. The currently-active filter is reflected in the dropdown's selected option.
- `src/bi_evals/ui/server.py` — the `/runs/{id}` handler picks up `category` and `model` from query params and passes them through.

**Tests:**
- `tests/test_report_builder.py` — assert that filtering by category produces a report that excludes other categories.
- `tests/test_ui.py` — `/runs/{id}?category=cases` returns 200 with only the `cases` category visible.

### 4. Project filter on the runs list (~half day)

Add a project-name filter to the runs list. Ships with a dropdown of distinct `project_name` values from the DB.

**Code:**
- `src/bi_evals/store/queries.py` — add `list_projects(conn) -> list[str]` (distinct `project_name` from `runs`); extend `list_runs(conn, limit=50, project_name: str | None = None)` to filter.
- `src/bi_evals/ui/server.py` — `/` handler reads optional `?project=` query param.
- `src/bi_evals/ui/templates/runs_list.html.j2` — add a project dropdown above the table. Empty value = "All projects" (current behavior). When a project is selected, the meta-refresh URL preserves the filter.

**Tests:**
- `tests/test_ui.py` — runs list with `?project=...` only shows matching runs. Empty project filter behaves like today.

### 5. Documentation

- `docs/feature_summary.md` — note the new drilldown URL and filter query params.
- `STATUS.md` — mark Phase 7.5 complete; brief description.

---

## File changes

| Path | Action | Notes |
|---|---|---|
| `src/bi_evals/report/templates/report.html.j2` | Modify | Failures section, filter strip, reason column |
| `src/bi_evals/report/builder.py` | Modify | Filter kwargs, pass failure detail to template |
| `src/bi_evals/store/queries.py` | Modify | `list_projects`, `get_test`, `list_runs` project filter |
| `src/bi_evals/ui/server.py` | Modify | New drilldown route, filter query params |
| `src/bi_evals/ui/templates/runs_list.html.j2` | Modify | Project dropdown |
| `src/bi_evals/ui/templates/test_detail.html.j2` | New | Drilldown page |
| `tests/test_report_builder.py` | Modify | Failure-reason rendering, filter behavior |
| `tests/test_ui.py` | Modify | Drilldown routes, filter query params, project filter |
| `tests/test_store_queries.py` | Modify | `list_projects`, `get_test`, project-filtered `list_runs` |
| `docs/feature_summary.md` | Modify | Document new URLs |
| `STATUS.md` | Modify | Mark Phase 7.5 complete |

No new dependencies. No new top-level CLI commands. No template inheritance changes.

---

## Risks / gotchas

- **The "Failures" section can swamp a bad run.** A run with 30 failing tests shouldn't render 30 long reason blocks at the top. Cap the section at 10 failures with "show all" linking to the existing per-test table or filtered view. The drilldown is where the full detail lives.
- **Filter combinatorics.** `?category=X&model=Y` should compose. Avoid building a fancy filter-state class — pass the two strings through as kwargs. If the third filter ever shows up, we revisit.
- **Multi-model runs in the drilldown.** A test_id can have multiple rows (one per model). The drilldown URL needs `?model=` to disambiguate, and we should redirect bare `/tests/{test_id}` to the only model when there's one, or show a model-picker when there's more than one. Pick: redirect when one, render a small "this test has results for: [m1] [m2] [m3]" picker when multiple.
- **Auto-refresh + filter URL.** The meta-refresh tag must include the current query string, or filters reset every 10 seconds. Easy to miss; add a test.
- **Trace JSON size.** `trace_json` for a multi-turn agent run can be hundreds of KB. Render it inside `<details>` (collapsed by default) so it doesn't blow up the page on load. If even that becomes painful, render the first N turns and link to the raw file.
- **Scope creep.** Every one of these features will tempt us to add "just one more thing" (search, sort, persistent filter state, etc.). Stop at the four work items. SPA era handles the rest.

---

## Estimate

~3 days end-to-end, possibly 4 with the drilldown polish. Specifically:
- Day 1: failure reasons in report + drilldown route + template
- Day 2: filtering in single-run view
- Day 3: project filter on runs list, tests, docs

---

## Verification

```bash
uv run bi-evals ui
# 1. Runs list shows project filter dropdown; selecting a project narrows the list.
#    The 10s refresh preserves the filter (URL has ?project=...).
# 2. Click a run: see "Failures" section at top listing failed tests with reasons.
#    Per-test table shows the reason inline.
# 3. Use category dropdown: page reloads with only that category's data.
# 4. Click a failed test_id: drilldown page loads with generated SQL,
#    per-dimension reasons, files_read, and a collapsible trace.
# 5. Multi-model run: drilldown shows model picker if test_id has >1 model.
```

Success criteria:
- A failing test's reason is visible without leaving the UI
- The drilldown page contains generated SQL, dimension-level reasons, and trace
- Filters compose (`?category=X&model=Y`) and survive the meta-refresh
- Project filter narrows the runs list and survives refresh
- All Phase 7 tests still pass; ≥10 new tests added in this phase
- No new dependencies in `pyproject.toml`

---

## What this phase deliberately leaves for the SPA rebuild

- Trend charts (pass rate over time, cost over time)
- Real-time updates without page reloads
- Multi-DB / multi-workspace aggregation
- Golden authoring forms with schema autocomplete
- SQL editor with syntax highlighting and live preview
- Triggering runs from the UI
- Per-test history with stability charts
- Saved filter presets, search, tags
- Anything that needs client-side state

When Phase 8+ commits to the SPA, `report/builder.py` gets carved into JSON endpoints, the Jinja templates are deleted, and the front-end is rebuilt in React/Svelte against the same `store/queries.py` data layer. The work in this phase is intentionally throwaway. Don't try to make any of it reusable.
