"""Build HTML for single-run reports and run-to-run compare pages."""

from __future__ import annotations

from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, select_autoescape

from bi_evals.compare.diff import (
    Verdict,
    bucket_counts,
    category_deltas,
    classify_pairs,
    compute_verdict,
    dimension_deltas,
)
from bi_evals.store import queries as q

TEMPLATES_DIR = Path(__file__).parent / "templates"

VERDICT_META = {
    Verdict.GREEN: {"emoji": "🟢", "class": "green",
                     "headline": "No regressions",
                     "sub": "All tests that passed before still pass."},
    Verdict.AMBER: {"emoji": "🟡", "class": "amber",
                     "headline": "Mixed changes",
                     "sub": "No regressions, but some scores, fixes, or test set drift to review."},
    Verdict.RED:   {"emoji": "🔴", "class": "red",
                     "headline": "Regressions detected",
                     "sub": "One or more tests (or critical dimensions) regressed."},
}


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["bar_class"] = _bar_class
    env.globals["pass_pill"] = _pass_pill
    return env


def _bar_class(pass_rate: float) -> str:
    if pass_rate >= 0.9:
        return ""
    if pass_rate >= 0.6:
        return "warn"
    return "fail"


def _pass_pill(passed: bool | None) -> str:
    if passed is True:
        return '<span class="pill pass">pass</span>'
    if passed is False:
        return '<span class="pill fail">fail</span>'
    return '<span class="pill neutral">—</span>'


def build_report_html(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    stale_after_days: int = 180,
    cost_alert_multiplier: float = 2.0,
    cost_alert_window: int = 10,
) -> str:
    """Render the single-run HTML report."""
    run = q.get_run(conn, run_id)
    categories = q.aggregate_by_category(conn, run_id)
    dimensions = q.dimension_pass_rates(conn, run_id)
    dimensions = _drop_vacuous_dimensions(conn, run_id, dimensions)
    models = q.cost_by_model(conn, run_id)
    model_list = q.list_models_for_run(conn, run_id)
    summaries = q.model_summary(conn, run_id) if len(model_list) > 1 else []
    scatter_svg = _quality_cost_scatter(summaries) if summaries else ""
    stability = q.flakiest_tests(conn, last_n_runs=10, limit=5)

    # Phase 6b: freshness + cost alert
    stale, unverified = q.stale_goldens(conn, run_id, stale_after_days=stale_after_days)
    fresh_vs_stale = _fresh_vs_stale_pass_rates(conn, run_id, {g.test_id for g in stale})
    cost_alert = q.cost_alerts(
        conn, run_id, multiplier=cost_alert_multiplier, window=cost_alert_window
    )

    overall_pass_rate = (run.pass_count / run.test_count) if run.test_count else 0.0
    total_latency_s = (
        f"{run.total_latency_ms / 1000.0:.1f}" if run.total_latency_ms else "0.0"
    )
    total_tokens = (run.total_prompt_tokens or 0) + (run.total_completion_tokens or 0)

    env = _env()
    return env.get_template("report.html.j2").render(
        run=run,
        categories=categories,
        dimensions=dimensions,
        models=models,
        overall_pass_rate_pct=round(overall_pass_rate * 100),
        total_latency_s=total_latency_s,
        total_tokens=f"{total_tokens:,}",
        model_summaries=summaries,
        scatter_svg=scatter_svg,
        stability=stability,
        stale_goldens=stale,
        unverified_goldens=unverified,
        fresh_vs_stale=fresh_vs_stale,
        cost_alert=cost_alert,
    )


def _drop_vacuous_dimensions(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    dimensions: list,
) -> list:
    """Drop dimensions whose every row is a vacuous pass — they add no signal.

    Currently this only fires for ``anti_pattern_compliance`` on runs where no
    golden defines anti-patterns: every dimension row passes for the boring
    reason "no anti-patterns defined", which would otherwise show as 100% in
    the report and dilute the user's attention.

    A dimension is vacuous when *every* row has ``passed=true`` AND a reason
    that starts with "skipped" (the marker emitted by ``_skip()``).
    """
    if not dimensions:
        return dimensions
    candidate_names = {d.dimension for d in dimensions if d.pass_count == d.total}
    if not candidate_names:
        return dimensions
    rows = conn.execute(
        """
        SELECT dimension,
               BOOL_AND(passed) AS all_pass,
               BOOL_AND(reason LIKE 'skipped:%') AS all_skipped
        FROM dimension_results
        WHERE run_id = ? AND dimension IN ({})
        GROUP BY dimension
        """.format(",".join(["?"] * len(candidate_names))),
        [run_id, *sorted(candidate_names)],
    ).fetchall()
    drop = {r[0] for r in rows if r[1] and r[2]}
    return [d for d in dimensions if d.dimension not in drop]


def _fresh_vs_stale_pass_rates(
    conn: duckdb.DuckDBPyConnection, run_id: str, stale_test_ids: set[str]
) -> dict[str, dict[str, float]]:
    """Compute pass-rate buckets for fresh vs. stale goldens in a run."""
    if not stale_test_ids:
        return {}
    rows = conn.execute(
        """
        SELECT test_id,
               COALESCE(pass_rate, CASE WHEN passed THEN 1.0 ELSE 0.0 END) AS rate
        FROM test_results
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchall()
    fresh = [float(r[1]) for r in rows if r[0] not in stale_test_ids]
    stale = [float(r[1]) for r in rows if r[0] in stale_test_ids]

    def _avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "fresh": {"count": len(fresh), "pass_rate": _avg(fresh)},
        "stale": {"count": len(stale), "pass_rate": _avg(stale)},
    }


def _quality_cost_scatter(summaries: list) -> str:
    """Inline SVG: pass_rate (y) vs total cost (x), one dot per model."""
    if not summaries:
        return ""
    width, height, pad = 560, 240, 40
    costs = [s.total_cost_usd for s in summaries]
    max_cost = max(costs) if max(costs) > 0 else 1.0
    # y-axis is 0..1 pass_rate
    def x(cost: float) -> float:
        return pad + (cost / max_cost) * (width - 2 * pad)
    def y(rate: float) -> float:
        return height - pad - rate * (height - 2 * pad)

    points = []
    labels = []
    for s in summaries:
        cx, cy = x(s.total_cost_usd), y(s.pass_rate)
        points.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" class="scatter-dot" />')
        labels.append(
            f'<text x="{cx + 8:.1f}" y="{cy - 6:.1f}" class="scatter-label">{s.model}</text>'
        )

    axes = (
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" class="axis" />'
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" class="axis" />'
    )
    x_label = f'<text x="{width / 2:.0f}" y="{height - 8}" class="axis-label" text-anchor="middle">cost (USD)</text>'
    y_label = f'<text x="14" y="{height / 2:.0f}" class="axis-label" text-anchor="middle" transform="rotate(-90 14 {height / 2:.0f})">pass rate</text>'

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" class="scatter">'
        f"{axes}{x_label}{y_label}{''.join(points)}{''.join(labels)}"
        "</svg>"
    )


def build_compare_html(
    conn: duckdb.DuckDBPyConnection,
    run_a_id: str,
    run_b_id: str,
    *,
    regression_threshold: float = 0.2,
) -> str:
    """Render the compare HTML for two runs."""
    diff = q.test_diff(conn, run_a_id, run_b_id)
    critical = q.critical_dimensions(conn, run_b_id) or q.critical_dimensions(conn, run_a_id)

    classified = classify_pairs(
        diff.pairs, critical, regression_threshold=regression_threshold
    )
    verdict = compute_verdict(classified)
    meta = VERDICT_META[verdict]

    counts = bucket_counts(classified)
    cat_deltas = category_deltas(classified)
    dim_deltas = dimension_deltas(classified)

    # Phase 6b: prompt drift. Compute the per-test "files I read that changed"
    # annotation by intersecting each test's files_read (from run B) with the
    # set of modified/added/removed files between runs.
    prompt_changes = q.prompt_diff(conn, run_a_id, run_b_id)
    changed_set = set(prompt_changes.added) | set(prompt_changes.removed) | set(prompt_changes.modified)
    files_read_b = q.files_read_for_run(conn, run_b_id)

    # Show only tests whose state or score changed meaningfully.
    def _is_transition(c) -> bool:
        if c.bucket in {"regressed", "fixed", "added", "removed"}:
            return True
        if c.score_delta is not None and abs(c.score_delta) > 1e-6:
            return True
        return False

    transitions = [c for c in classified if _is_transition(c)]
    # Sort: regressions first, then fixed, then others; within each, largest |delta| first.
    bucket_order = {"regressed": 0, "fixed": 1, "added": 2, "removed": 3}
    transitions.sort(
        key=lambda c: (
            bucket_order.get(c.bucket, 4),
            -(abs(c.score_delta) if c.score_delta is not None else 0.0),
            c.pair.test_id,
        )
    )

    # Annotate each transition with the changed files it actually read.
    transition_views = []
    for c in transitions:
        key = (c.pair.test_id, c.pair.model or "")
        files = files_read_b.get(key, [])
        culprits = sorted(f for f in files if f in changed_set)
        transition_views.append({"c": c, "culprits": culprits})

    env = _env()
    return env.get_template("compare.html.j2").render(
        run_a=diff.run_a,
        run_b=diff.run_b,
        verdict_emoji=meta["emoji"],
        verdict_class=meta["class"],
        verdict_headline=meta["headline"],
        verdict_sub=meta["sub"],
        counts=counts,
        transitions=transition_views,
        category_deltas=cat_deltas,
        dimension_deltas=dim_deltas,
        prompt_changes=prompt_changes,
    )


def sanitize_for_filename(run_id: str) -> str:
    """Make a Promptfoo evalId safe to use in file paths."""
    return run_id.replace(":", "-").replace("/", "_")
