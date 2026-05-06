"""FastAPI app serving runs list, single-run, and compare views."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from bi_evals.config import BiEvalsConfig
from bi_evals.report.builder import build_compare_html, build_report_html
from bi_evals.store import connect as store_connect
from bi_evals.store import queries as q

UI_TEMPLATES_DIR = Path(__file__).parent / "templates"
RUNS_LIST_REFRESH_SECONDS = 10

# Pass-rate band thresholds. Kept in sync with the pill classes computed in
# runs_list.html.j2 so server-side filtering matches the rendered colour.
_BAND_PASS_MIN = 0.9
_BAND_WARN_MIN = 0.6
_VALID_BANDS = {"all", "pass", "warn", "fail"}
_SINCE_PATTERN = re.compile(r"^(\d+)d$")


def create_app(config: BiEvalsConfig) -> FastAPI:
    """Build the FastAPI app bound to a loaded config.

    Config is read once at startup. Restart the server after editing
    ``bi-evals.yaml`` for changes to take effect.
    """
    app = FastAPI(title="bi-evals viewer", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db_path = config.resolve_path(config.storage.db_path)
    app.state.env = _build_jinja_env()

    @app.get("/", response_class=HTMLResponse)
    def runs_list(
        request: Request,
        error: str | None = Query(default=None),
        project: str | None = Query(default=None),
        since: str | None = Query(default=None),
        band: str | None = Query(default=None),
    ) -> str:
        cfg: BiEvalsConfig = app.state.config
        return _render_runs_list(
            app,
            error=error,
            project=project,
            since=since,
            band=band,
            regression_threshold=cfg.compare.regression_threshold,
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_view(
        run_id: str,
        category: str | None = Query(default=None),
        model: str | None = Query(default=None),
    ) -> str:
        cfg: BiEvalsConfig = app.state.config
        try:
            with store_connect(app.state.db_path, read_only=True) as conn:
                return build_report_html(
                    conn, run_id,
                    stale_after_days=cfg.scoring.stale_after_days,
                    cost_alert_multiplier=cfg.storage.cost_alert_multiplier,
                    cost_alert_window=cfg.storage.cost_alert_window,
                    category=category,
                    model=model,
                )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Run not found: {run_id}",
            )

    @app.get("/runs/{run_id}/tests/{test_id:path}", response_class=HTMLResponse)
    def test_detail(
        run_id: str,
        test_id: str,
        model: str | None = Query(default=None),
    ) -> Any:
        cfg: BiEvalsConfig = app.state.config  # noqa: F841 (reserved for future kwargs)
        try:
            with store_connect(app.state.db_path, read_only=True) as conn:
                run = q.get_run(conn, run_id)
                available_models = sorted({
                    t.model or ""
                    for t in q.list_tests(conn, run_id)
                    if t.test_id == test_id
                })
                # If the test exists for >1 model and the user didn't pick one,
                # redirect to the first so the page always shows a single result.
                if model is None and len(available_models) > 1:
                    return RedirectResponse(
                        url=f"/runs/{run_id}/tests/{_quote(test_id)}?model={_quote(available_models[0])}",
                        status_code=303,
                    )
                effective_model = model if model is not None else (available_models[0] if available_models else None)
                test = q.get_test(conn, run_id, test_id, model=effective_model)
                dimensions = q.list_dimensions(conn, run_id, test_id, model=effective_model)
                extras = q.get_test_extras(conn, run_id, test_id, model=effective_model)
                return app.state.env.get_template("test_detail.html.j2").render(
                    run=run,
                    test=test,
                    dimensions=dimensions,
                    extras=extras,
                    available_models=available_models,
                )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/compare", response_class=HTMLResponse)
    def compare_view(a: str = Query(...), b: str = Query(...)) -> Any:
        cfg: BiEvalsConfig = app.state.config
        try:
            with store_connect(app.state.db_path, read_only=True) as conn:
                return build_compare_html(
                    conn, a, b,
                    regression_threshold=cfg.compare.regression_threshold,
                )
        except KeyError as e:
            return RedirectResponse(
                url=f"/?error={_quote(str(e))}",
                status_code=303,
            )

    @app.post("/compare-selected", response_class=HTMLResponse)
    async def compare_selected(request: Request) -> Any:
        """Form target for the runs-list checkboxes. Validates exactly 2 picks."""
        form = await request.form()
        picks = form.getlist("run_ids")
        if len(picks) != 2:
            msg = (
                f"Pick exactly 2 runs to compare (got {len(picks)})."
            )
            return RedirectResponse(
                url=f"/?error={_quote(msg)}",
                status_code=303,
            )
        a, b = picks
        return RedirectResponse(url=f"/compare?a={a}&b={b}", status_code=303)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: HTTPException) -> HTMLResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Not found"
        html = app.state.env.get_template("not_found.html.j2").render(message=message)
        return HTMLResponse(content=html, status_code=404)

    return app


def _render_runs_list(
    app: FastAPI,
    *,
    error: str | None = None,
    project: str | None = None,
    since: str | None = None,
    band: str | None = None,
    regression_threshold: float = 0.2,
) -> str:
    db_path: Path = app.state.db_path
    env: Environment = app.state.env

    since_value = (since or "").strip()
    since_dt = _parse_since(since_value)
    if since_value and since_dt is None:
        # Invalid input → ignore the filter, surface the value back, banner.
        bad_since = since_value
        active_since = ""
        since_error = f"Ignored invalid `since` value: {bad_since!r}. Use e.g. `7d` or `30d`."
    else:
        active_since = since_value
        since_error = None

    band_value = (band or "all").strip().lower()
    if band_value not in _VALID_BANDS:
        band_error = f"Ignored invalid `band` value: {band!r}. Use pass, warn, fail, or all."
        band_value = "all"
    else:
        band_error = None

    composed_error = error
    extra_msgs = [m for m in (since_error, band_error) if m]
    if extra_msgs:
        composed_error = " ".join(([error] if error else []) + extra_msgs)

    if not db_path.exists():
        return env.get_template("runs_list.html.j2").render(
            runs=[],
            empty=True,
            error=composed_error,
            refresh_seconds=RUNS_LIST_REFRESH_SECONDS,
            available_projects=[],
            active_project=project or "",
            active_since=active_since,
            active_band=band_value,
            regressed_run_ids=set(),
            refresh_qs="",
        )

    with store_connect(db_path, read_only=True) as conn:
        runs = q.list_runs(
            conn,
            limit=50,
            project_name=project or None,
            since=since_dt,
        )
        runs = _filter_by_band(runs, band_value)
        available_projects = q.list_projects(conn)
        latest = runs[0].run_id if runs else None
        prev = runs[1].run_id if len(runs) > 1 else None
        regressed_ids = q.runs_with_regressions(
            conn,
            [r.run_id for r in runs],
            regression_threshold=regression_threshold,
        )

    refresh_qs = _build_refresh_qs(project=project, since=active_since, band=band_value)

    return env.get_template("runs_list.html.j2").render(
        runs=runs,
        empty=False,
        error=composed_error,
        latest_id=latest,
        prev_id=prev,
        refresh_seconds=RUNS_LIST_REFRESH_SECONDS,
        available_projects=available_projects,
        active_project=project or "",
        active_since=active_since,
        active_band=band_value,
        regressed_run_ids=regressed_ids,
        refresh_qs=refresh_qs,
    )


def _parse_since(raw: str) -> datetime | None:
    """Parse an `Nd` window into a UTC threshold. Returns None on empty/bad input."""
    if not raw:
        return None
    m = _SINCE_PATTERN.match(raw)
    if not m:
        return None
    days = int(m.group(1))
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _filter_by_band(runs: list[q.RunRow], band: str) -> list[q.RunRow]:
    if band == "all":
        return runs
    out: list[q.RunRow] = []
    for r in runs:
        rate = (r.pass_count / r.test_count) if r.test_count else 0.0
        if band == "pass" and rate >= _BAND_PASS_MIN:
            out.append(r)
        elif band == "warn" and _BAND_WARN_MIN <= rate < _BAND_PASS_MIN:
            out.append(r)
        elif band == "fail" and rate < _BAND_WARN_MIN:
            out.append(r)
    return out


def _build_refresh_qs(
    *, project: str | None, since: str | None, band: str | None
) -> str:
    parts: list[str] = []
    if project:
        parts.append(f"project={_quote(project)}")
    if since:
        parts.append(f"since={_quote(since)}")
    if band and band != "all":
        parts.append(f"band={_quote(band)}")
    return ("?" + "&".join(parts)) if parts else ""


def _build_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(UI_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = _pct_filter
    env.filters["money"] = _money_filter
    return env


def _pct_filter(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def _money_filter(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.4f}"


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
