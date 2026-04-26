# Phase 8: Web UI for Eval Exploration and Regression Analysis

## Context

Phases 1–5 built the eval pipeline: `bi-evals run` produces data, DuckDB stores it, HTML reports render it. In daily use, the HTML reports hit a ceiling: regenerating files and passing paths around for exploration is clumsy, and "how has this test performed over time" requires raw SQL. The CLI flow is fine for CI artifacts and shareable snapshots, but day-to-day debugging wants an interactive surface.

Phase 8 ships a **local-first web UI** that connects directly to the DuckDB store, rendering runs, tests, comparisons, and per-test history in a modern React app. Deployment stays local for v1 (`bi-evals ui` starts a FastAPI + Vite bundle on `localhost:8000`), but the architecture is explicitly designed for a future migration to a shared team instance backed by Postgres.

Out of scope for v1 (deferred to v2):
- Golden authoring (forms, YAML validation, preview)
- Triggering `bi-evals run` from the UI
- Multi-user / auth / deployment infrastructure
- Trend charts on the dashboard
- Advanced search, saved filters, tags

---

## Decisions (locked in)

| Area | Choice | Rationale |
|---|---|---|
| **Primary user** | Engineer debugging regressions | Immediate pain; analyst/stakeholder views deferred to v2+ |
| **Deployment** | Local-only, Postgres-ready | YAGNI on auth/hosting; design for migration |
| **Backend** | FastAPI | Same language as pipeline; reuses `store/queries.py` |
| **Frontend** | React + Vite + TypeScript | Interactivity needs (charts, diffs, traces) justify SPA |
| **Base UI** | shadcn/ui + Tailwind CSS | Good defaults, copy-paste components, theming support |
| **Tables** | TanStack Table | Sort/filter/search at client for small data, ergonomic API |
| **Charts** | Recharts | React-idiomatic, sufficient for v1 needs |
| **SQL diff** | Monaco editor | Readonly mode + built-in diff viewer |
| **Storage abstraction** | `ResultsStore` Protocol | 1-hour upfront cost; cheap insurance for Postgres migration |
| **API style** | REST / JSON | Simpler than GraphQL; FastAPI auto-generates OpenAPI |
| **Theme** | Dark default, light toggle | Matches engineering tooling conventions |
| **Scope** | 7 pages (see below) | Covers read-path exploration without scope creep |

### Future considerations
- **Trace viewer** — v1 renders traces as collapsible JSON; future phase may integrate Langfuse if external trace tooling becomes standard.
- **Postgres migration** — schema is 95% portable (JSON → JSONB); auth + deploy are future team decisions.

---

## Architecture

```
┌──────────────────────────────────────────────┐
│  Browser (localhost:8000)                    │
│   React SPA (Vite build, bundled into pkg)   │
└──────────────┬───────────────────────────────┘
               │ HTTP / JSON
┌──────────────▼───────────────────────────────┐
│  FastAPI (bi-evals ui)                       │
│   routes/  → REST endpoints                  │
│   deps/    → ResultsStore injection          │
└──────────────┬───────────────────────────────┘
               │
┌──────────────▼───────────────────────────────┐
│  ResultsStore (Protocol)                     │
│   ├── DuckDBStore  (v1)                      │
│   └── PostgresStore (future)                 │
└──────────────┬───────────────────────────────┘
               │
      existing store/queries.py
      (unchanged; wrapped by store)
```

**`bi-evals ui`** boots:
1. Loads `bi-evals.yaml` to find the DuckDB path
2. Instantiates `DuckDBStore(db_path)` in read-only mode
3. Starts `uvicorn` serving FastAPI
4. FastAPI serves the bundled React app under `/` and JSON under `/api/*`
5. Opens `http://localhost:8000` in the default browser

No separate dev server in production. For frontend development, run `pnpm dev` (Vite) with a proxy to `localhost:8000/api`.

---

## Pages (v1)

### Global shell

- **Top bar**: `[bi-evals]`, project name, `[Run eval]` (disabled, visible as v2 affordance), theme toggle
- **Left sidebar**: Runs, Tests, Goldens, Compare
- **Content area**: page-specific
- **Routing**: `/runs`, `/runs/<id>`, `/runs/<id>/tests/<test-id>`, `/tests`, `/tests/<test-id>`, `/compare`, `/compare/<a>/<b>`, `/goldens`, `/goldens/<id>`

### Page 1 — Runs list (landing, `/runs`)

Table: `Run ID`, `Time`, `Tests`, `Pass%`, `Cost`, `Δ vs prev`.

- Latest 50 rows, "Load more" button to extend
- Filter: date range, verdict (🟢/🟡/🔴 via existing compare logic against previous run)
- Search: free-text against run_id
- Sort: any column (default: timestamp desc)
- Row click → `/runs/<id>`
- No sparklines in v1 (text only)

### Page 2 — Run detail (`/runs/<id>`)

Top: run metadata + `[Compare to prev]` + `[Download JSON]`

Sections (top to bottom):
1. **Summary tiles**: pass rate, avg score, total cost
2. **Category pass rates**: bars per category
3. **Dimension pass rates**: 9 bars, worst first, critical dims highlighted
4. **Cost by model**: collapsed by default (click to expand)
5. **Tests table**:
   - Columns: ✓/✗, test_id, category, score, failed dimensions (inline comma list)
   - Default sort: failures first, then category
   - Filter: status (pass/fail), category, search
   - Row click → `/runs/<id>/tests/<test-id>`

### Page 3 — Test drilldown (`/runs/<id>/tests/<test-id>`)

Dense debug view. This is where engineers spend real time.

- Top: test_id, category, pass/fail, score, `[← prev test] [next test →]`, `[View history across runs]`
- **Question** block
- **Dimensions (9)**: grid with pass/fail per dim, expand to see reason inline. Critical dims starred.
- **SQL tabs**: Generated / Reference / **Diff** (Monaco diff viewer)
- **Row comparison** (when available): side-by-side data tables showing first 20 rows each of expected vs actual
- **Agent trace**: collapsible tree of tool calls, pretty-printed args/results, step-by-step
- **Files read**: flat list of skill file paths
- **Metadata footer**: cost, latency, tokens, model

### Page 4 — Compare view (`/compare`, `/compare/<a>/<b>`)

- Two run pickers at top (searchable dropdowns, sorted by recency)
- **Verdict banner** (🟢/🟡/🔴) with pass-rate delta
- **Transitions**: 6 collapsible sections (regressed, fixed, unchanged_pass, unchanged_fail, added, removed) with counts
  - Failures/changes expanded by default; unchanged collapsed
  - Row in "regressed" click → test drilldown in **compare mode** (split screen A/B for SQL and data, sequential for metadata)
- **Category deltas** table
- **Dimension deltas** table, worst first

### Page 5 — Tests list (`/tests`)

All goldens seen across all runs. The "find flaky tests" page.

- Columns: test_id, category, runs count, pass rate, last-10-runs sparkline (pass/fail strip)
- Filter: category, last-run status
- Row click → `/tests/<test-id>` (per-test history)

### Page 6 — Per-test history (`/tests/<test-id>`)

- **Score trend chart**: line chart of score across runs (Recharts)
- **Pass/fail strip**: one cell per run, colored
- **Dimension trends**: 9 small-multiples mini-charts, one per dimension
- **Runs table**: every run this test appeared in (columns: run_id, time, passed, score, failed dims)
  - Row click → test drilldown for that (run, test)
- **Golden definition** block: question, reference SQL, YAML link

### Page 7 — Goldens list (`/goldens`, `/goldens/<id>`)

- Table: golden_id, category, difficulty, tags
- Row click → `/goldens/<id>` showing YAML content + latest run result preview
- **v1 is read-only.** v2 adds authoring (form, validation, preview).

---

## API surface

All endpoints return JSON. Base: `/api/v1`.

```
GET  /runs?limit=50&offset=0&verdict=red|amber|green&after=<ts>
         → [{ run_id, timestamp, test_count, pass_count, total_cost_usd, verdict_vs_prev }]

GET  /runs/<run_id>
         → full RunRow + category_aggregates + dimension_pass_rates + cost_by_model

GET  /runs/<run_id>/tests
         → [{ test_id, category, passed, score, failed_dimensions[] }]

GET  /runs/<run_id>/tests/<test_id>
         → full TestRow + dimensions[] + trace + row_comparison

GET  /tests
         → [{ test_id, category, runs_count, pass_rate, last_10_runs[] }]

GET  /tests/<test_id>/history
         → [{ run_id, timestamp, passed, score, failed_dimensions[] }]
         + dimension_trends (9 series)

GET  /compare?a=<run_a>&b=<run_b>
         → { verdict, transitions, category_deltas, dimension_deltas }

GET  /goldens
         → [{ golden_id, category, difficulty, tags }]

GET  /goldens/<golden_id>
         → { golden YAML content, latest_run_result }

GET  /projects
         → [{ name, db_path }]  (v1: returns just the current one)
```

Response shapes mirror existing `store/queries.py` dataclasses as JSON. FastAPI's Pydantic integration handles serialization; `response_model` parameter generates OpenAPI docs.

---

## Storage abstraction

Refactor `src/bi_evals/store/queries.py` behind a Protocol so the FastAPI layer doesn't depend on DuckDB directly.

```python
# src/bi_evals/store/base.py
class ResultsStore(Protocol):
    def list_runs(self, limit: int, offset: int, ...) -> list[RunRow]: ...
    def get_run(self, run_id: str) -> RunRow: ...
    def list_tests_for_run(self, run_id: str) -> list[TestRow]: ...
    def get_test(self, run_id: str, test_id: str) -> TestDetail: ...
    def test_history(self, test_id: str) -> TestHistory: ...
    def compare(self, run_a: str, run_b: str) -> CompareResult: ...
    # ...

# src/bi_evals/store/duckdb_store.py
class DuckDBStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    def list_runs(self, ...):
        with connect(self._db_path, read_only=True) as conn:
            return queries.list_runs(conn, ...)
    # ...
```

Existing `queries.py` functions stay as-is — `DuckDBStore` wraps them. When Postgres lands, `PostgresStore` implements the same Protocol. FastAPI routes take `store: ResultsStore = Depends(get_store)` and don't care which backend is active.

**Cost**: ~2 hours of wrapping. Cheap insurance.

**SQL portability**: all existing queries already use standard SQL. Keep it that way — no DuckDB-only functions.

---

## File structure

```
src/bi_evals/
  ui/
    __init__.py
    app.py              # FastAPI app factory
    deps.py             # Dependency injection (get_store, get_config)
    routes/
      runs.py
      tests.py
      compare.py
      goldens.py
      projects.py
    models.py           # Pydantic response models
    static/             # Bundled React build output (gitignored; produced by `pnpm build`)
      index.html
      assets/…

  store/
    base.py             # ResultsStore Protocol (NEW)
    duckdb_store.py     # DuckDB implementation (NEW)
    __init__.py         # Re-exports connect, ResultsStore, DuckDBStore
    client.py           # (unchanged)
    schema.py           # (unchanged)
    ingest.py           # (unchanged)
    queries.py          # (unchanged — wrapped by DuckDBStore)

  cli.py                # Add `ui` command

ui/                     # React app source (NEW, at repo root)
  package.json
  vite.config.ts
  tsconfig.json
  tailwind.config.ts
  src/
    main.tsx
    App.tsx
    routes/
      RunsList.tsx
      RunDetail.tsx
      TestDrilldown.tsx
      Compare.tsx
      TestsList.tsx
      TestHistory.tsx
      GoldensList.tsx
      GoldenDetail.tsx
    components/
      Shell.tsx         # Top bar + sidebar layout
      DataTable.tsx     # TanStack Table wrapper
      SqlDiff.tsx       # Monaco wrapper
      TraceViewer.tsx   # Collapsible JSON tree
      DimensionGrid.tsx
      VerdictBanner.tsx
      ThemeToggle.tsx
    lib/
      api.ts            # Typed fetch wrappers
      types.ts          # Mirrored from Pydantic response models (codegen or manual)
      theme.ts
    styles/
      globals.css

tests/
  test_ui_routes.py     # FastAPI TestClient-based smoke tests (NEW)
  test_store_abstract.py  # Test DuckDBStore Protocol conformance (NEW)
```

### Build pipeline
- `pnpm build` in `ui/` outputs to `src/bi_evals/ui/static/`
- Package data manifest (`pyproject.toml`) includes `ui/static/**` so the bundle ships with the Python package
- CI builds frontend → runs Python tests → publishes

### Development
- Terminal 1: `uv run bi-evals ui --dev` (starts FastAPI on :8000 with reload)
- Terminal 2: `cd ui && pnpm dev` (Vite on :5173 with API proxy to :8000)
- Production: single `bi-evals ui` serves bundled assets from Python

---

## `bi-evals ui` command

```bash
bi-evals ui [--port 8000] [--no-open] [--dev]
```

- Loads config, resolves DB path, verifies DB exists
- Starts uvicorn with FastAPI app
- Opens browser to `localhost:<port>` unless `--no-open`
- `--dev` mode: enables reload, expects Vite dev server separately

---

## Deployment migration path (future shared instance)

For when the team decides to move off local-only. Documented here so the v1 design doesn't paint us into a corner.

1. **New `PostgresStore`** implementing `ResultsStore`. Port DDL from DuckDB → Postgres (JSON → JSONB, minor type swaps). Migration script to backfill from one or more DuckDB files.
2. **Ingest** writes to Postgres instead of DuckDB. `bi-evals run` in CI publishes results to the shared DB.
3. **Auth layer** in FastAPI (middleware; can use FastAPI's built-in `OAuth2PasswordBearer` + team SSO via Auth0/Clerk/Okta).
4. **Frontend config** — point at hosted URL instead of localhost. Zero code change.
5. **Deploy** — Docker image bundling Python backend + React build + PG migration. Reverse proxy (nginx/Caddy) for TLS.

None of this blocks v1; none of it needs to be written speculatively.

---

## File changes summary

| Path | Action | Purpose |
|---|---|---|
| `pyproject.toml` | Modify | Add `fastapi`, `uvicorn[standard]`, `httpx` (tests) |
| `src/bi_evals/store/base.py` | New | `ResultsStore` Protocol |
| `src/bi_evals/store/duckdb_store.py` | New | DuckDB-backed implementation |
| `src/bi_evals/store/__init__.py` | Modify | Export new types |
| `src/bi_evals/ui/app.py` | New | FastAPI app factory |
| `src/bi_evals/ui/deps.py` | New | DI for store, config |
| `src/bi_evals/ui/routes/` | New | REST endpoints |
| `src/bi_evals/ui/models.py` | New | Pydantic response models |
| `src/bi_evals/ui/static/` | New (build artifact) | React bundle |
| `src/bi_evals/cli.py` | Modify | Add `ui` command |
| `ui/` | New | React app source |
| `tests/test_ui_routes.py` | New | FastAPI smoke tests |
| `tests/test_store_abstract.py` | New | Protocol conformance |
| `docs/duckdb-schema.md` | Modify | Link to `ResultsStore` abstraction |

---

## Risks / Gotchas

- **React build complexity** — first time the repo has a Node toolchain. Mitigate: use Vite (simplest modern setup), commit lockfile, document `pnpm install` as part of contributor setup. CI builds the bundle.
- **Bundle size** — Monaco editor is ~2MB. Lazy-load on test drilldown page only; don't include in initial bundle.
- **Trace size** — 1MB traces will crash naive rendering. Virtualize via `react-window` if it becomes a problem. v1 assumes modest traces.
- **DuckDB read-only concurrency** — solved in Phase 5 (`read_only=True`); UI uses same pattern. Multiple UI sessions + a `duckdb -readonly` CLI coexist cleanly.
- **Stale data** — UI fetches fresh from DB on every navigation. No client-side caching layer in v1. Consider React Query later if perf warrants.
- **Dark mode flash on load** — apply theme class from localStorage in inline `<head>` script before React mounts. Standard pattern.
- **Proxy config mismatch** — dev proxy (5173 → 8000) vs prod serving. Document both; test both before shipping.
- **Type drift between backend and frontend** — manual `types.ts` mirrors Pydantic models. Consider `datamodel-code-generator` or OpenAPI-based codegen in v2 if drift becomes a maintenance burden.

---

## Verification

```bash
# Backend tests
uv run python -m pytest tests/test_ui_routes.py tests/test_store_abstract.py -v

# Full suite regression
uv run python -m pytest tests/ -m "not integration" -v

# Frontend build
cd ui && pnpm install && pnpm build
# Expect: src/bi_evals/ui/static/ populated

# End-to-end smoke
uv run bi-evals --config tmp/my-evals/bi-evals.yaml ui --no-open &
curl -s http://localhost:8000/api/v1/runs | head -c 500
# Expect: JSON array of runs

# Manual: navigate every page with real data
open http://localhost:8000
# Test: runs list, run detail, test drilldown (with trace + SQL diff + row comparison),
#       compare, test history, goldens list
```

Success criteria:
- Every page works end-to-end against `tmp/my-evals/` data (16 runs, 5 tests each)
- Dark mode default, light toggle persists via localStorage
- Multiple browser tabs coexist without locking DuckDB
- Can navigate from a regression in the compare view → test drilldown → per-test history without using the URL bar
- Bundle size under 1MB initial load (Monaco lazy-loaded)
- Full test suite stays green
