# Phase 7: Minimal Local Viewer

## Context

The current loop — `bi-evals run` → `bi-evals report` → find file → `open file://...html` → `bi-evals compare A B` → open another file — is a friction tax paid every eval cycle. Phase 7 replaces it with a single command (`bi-evals ui`) that serves the same content over HTTP, so browsing runs is one click instead of three CLI invocations.

This is **deliberately the minimum to remove that friction**. No SPA, no build step, no React, no charts library, no editor component. If a richer UI ever becomes warranted (regression drilldowns, trend charts, golden authoring), it gets a separate phase based on actual usage data — not on guesses about what users will want.

An earlier draft of this phase planned a full SPA (FastAPI + Vite + React + TypeScript + shadcn + TanStack + Recharts + Monaco). That was designing v3 of the UI before v1 existed. This plan is v1.

---

## Goals

1. **One command:** `bi-evals ui` starts a local server, opens the browser. No more manual report generation.
2. **Three pages:** runs list, single-run view, compare view. That's it.
3. **Zero new dependencies beyond FastAPI + Uvicorn.** Reuse the existing Jinja templates verbatim.

Success: I never have to run `bi-evals report` or `bi-evals compare` from the CLI again. After a `bi-evals run`, I refresh the browser and see the new run.

---

## Non-goals

- Auth (it's localhost, single user — match Promptfoo's `view` UX)
- Trigger runs from the UI (`bi-evals run` stays in the CLI)
- Golden authoring forms
- Live progress streaming
- Trend charts, dashboards, search, filters
- Per-test history, prompt-drift visualizer, flakiness drilldown
- Postgres-ready storage abstraction (`ResultsStore` Protocol from the old plan) — YAGNI; revisit when there's a real reason to share state
- Any frontend framework, build step, or `node_modules`

If any of these become genuinely painful after v1 ships, they go in a separate phase.

---

## Stack

- **FastAPI** + **Uvicorn** — Python, no build step
- **Jinja2** — already used by `report/builder.py`; templates port over with minimal changes
- **Inline CSS** — already self-contained in the existing report templates
- **No JavaScript** for v1. If a single page needs partial updates, add HTMX (one `<script>` tag). Don't add it preemptively.

---

## Pages

### `GET /` — Runs list

A table of recent runs from the DuckDB store. Columns: timestamp, run-id, model(s), test count, pass rate, total cost, links.

- Sorted newest-first
- Each row links to `/runs/<run_id>`
- Two checkboxes per row + a "Compare selected" button → `/compare?a=<id>&b=<id>`
- "Latest vs prev" shortcut button at the top

Replaces: `ls results/eval_*.json`, `bi-evals cost`, `bi-evals flakiness` for the basic "what runs do I have" question.

### `GET /runs/<run_id>` — Single run

Renders exactly what `build_report_html(conn, run_id, ...)` produces today — the same template, same data, same CSS — but served from the DB instead of written to disk.

Reuses: `bi_evals.report.builder.build_report_html` directly. The function already takes a `duckdb` connection.

### `GET /compare?a=<id>&b=<id>` — Compare two runs

Renders exactly what `build_compare_html(conn, a, b, ...)` produces today.

Reuses: `bi_evals.report.builder.build_compare_html` directly.

---

## CLI

```bash
bi-evals ui
# Opening bi-evals viewer at http://localhost:8765 ...

bi-evals ui --port 9000 --no-open
```

- Default port: `8765` (avoids collision with Promptfoo's `15500`)
- Opens browser automatically (suppressible with `--no-open`)
- Reads `storage.db_path` from config like every other command
- `Ctrl+C` to stop

---

## Locked design decisions

These were open in an earlier draft. Defaults below; revisit only if v1 usage proves them wrong.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Template wrapping | **None.** Reuse `report.html.j2` and `compare.html.j2` as-is. | Templates already extend `_base.html.j2` and ship inline CSS — they render as standalone documents. Verified by inspection. |
| 2 | Runs list scope | **Last 50 runs, no pagination, no filters.** Sort by timestamp DESC, secondary by `run_id` for stable ordering when timestamps tie. | If you have >50 runs and need older ones, that's a real signal to add filtering — don't pre-build it. |
| 3 | "Compare selected" UX | **`<form>` wrapping the table; server validates exactly 2 boxes checked; bad count → render the runs list with an inline error banner.** No JS. | Crude but correct. Vanilla JS adds a script tag's worth of code we don't need yet. |
| 4 | Empty state (no runs in DB) | **Render the runs list with a centered message: "No runs yet. Run `bi-evals run` to create one."** Return 200, not 404. | First-run UX matters; 404 looks broken. |
| 5 | Config loading | **Load `BiEvalsConfig` once at server startup; store on `app.state.config` and `app.state.db_path`.** Handlers read from `app.state`. | Standard FastAPI pattern. Avoids reloading YAML per request. The config is immutable for the server's lifetime — restart `bi-evals ui` if you edit `bi-evals.yaml`. |
| 6 | Live refresh on new runs | **HTML meta refresh on the runs list only**, 10-second cadence: `<meta http-equiv="refresh" content="10">`. Single-run and compare views don't refresh (they're snapshots of a specific run). | Zero JS, zero new endpoints, one line in `runs_list.html.j2`. Localhost SQL is sub-100ms so the flash is barely perceptible. Upgrade to fetch+swap if/when the flash becomes annoying. |

Two follow-on details from the above:

- **Kwargs to `build_report_html` / `build_compare_html`** come from `app.state.config` (`stale_after_days`, `cost_alert_multiplier`, `cost_alert_window`, `regression_threshold`). Same wiring as `cli.py:225-230` and `cli.py:261-264` — copy that pattern.
- **Bad run-id in URL** (e.g., `/runs/does-not-exist`) → 404 with a "run not found, here are the runs we have" link back to `/`. This is the one place 404 is right.

---

## Code

- New `src/bi_evals/ui/__init__.py`
- New `src/bi_evals/ui/server.py` — FastAPI app, ~100 lines, three route handlers
- New `src/bi_evals/ui/templates/runs_list.html.j2` — the only new template (the other two pages reuse `report.html.j2` and `compare.html.j2` from `bi_evals/report/templates/`)
- Modify `src/bi_evals/cli.py` — add `ui` command that imports lazily (so `fastapi` doesn't slow down unrelated commands)
- Modify `pyproject.toml` — add `fastapi`, `uvicorn[standard]` to deps
- `tests/test_ui.py` — use FastAPI's `TestClient`; assert each route returns 200, contains expected text from a seeded DB fixture. ~5 tests.

The handler functions are essentially:

```python
@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(run_id: str):
    with store_connect(db_path, read_only=True) as conn:
        return build_report_html(conn, run_id, ...)
```

---

## File changes

| Path | Action | Notes |
|---|---|---|
| `src/bi_evals/ui/__init__.py` | New | Empty |
| `src/bi_evals/ui/server.py` | New | FastAPI app, ~100 lines |
| `src/bi_evals/ui/templates/runs_list.html.j2` | New | Only new template |
| `src/bi_evals/cli.py` | Modify | Add `ui` command |
| `pyproject.toml` | Modify | Add `fastapi`, `uvicorn[standard]` |
| `tests/test_ui.py` | New | Route smoke tests against `TestClient` |
| `docs/feature_summary.md` | Modify | Document `bi-evals ui` |
| `STATUS.md` | Modify | Mark Phase 7 complete |

---

## Risks / gotchas

- **Read-only DB connection in handlers.** Multiple browser tabs + an in-progress `bi-evals run` writing to the same DuckDB file can lock. Open all UI connections with `read_only=True` (already supported by `store_connect`).
- **Auto-refresh + concurrent writes.** The runs list polls every 10s via meta refresh. Worth verifying once during implementation that read-only `store_connect` calls see new rows committed by an in-progress `bi-evals run` — the existing retry-on-lock code suggests they do, but confirm with a manual test (start `bi-evals ui`, run `bi-evals run` in another terminal, watch the list update). If reads are stuck on a stale snapshot, opening a fresh connection per request (which the context manager does anyway) should fix it.
- **Port collision.** Default to a less-common port (`8765`); document `--port`.
- **Template drift.** The two reused templates (`report.html.j2`, `compare.html.j2`) currently render as standalone HTML files. Confirm they work when served from a route — they should, since they include their own CSS and have no relative URLs. If they need a layout wrapper, keep it minimal.
- **Scope creep is the real risk.** The temptation to add "just one chart" or "just a search box" will be strong. Defer everything to a future phase. Ship the boring version, see what people actually use.

---

## Estimate

~3 days end-to-end. Most of the work is wiring, not building — the data layer and templates already exist.

---

## Verification

```bash
uv run bi-evals ui
# Browser opens to http://localhost:8765 showing the runs list
# Click a run → single-run view renders identically to bi-evals report
# Check two rows + Compare → compare view renders identically to bi-evals compare
# Ctrl+C exits cleanly
```

Success criteria:
- All three routes return 200 with expected content for a seeded DB
- Single-run and compare pages are visually identical to the CLI-generated HTML
- `bi-evals report` and `bi-evals compare` CLI commands continue to work (not removed; the UI is additive)
- ≥5 new tests in `tests/test_ui.py`
