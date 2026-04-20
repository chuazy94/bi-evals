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


def build_report_html(conn: duckdb.DuckDBPyConnection, run_id: str) -> str:
    """Render the single-run HTML report."""
    run = q.get_run(conn, run_id)
    categories = q.aggregate_by_category(conn, run_id)
    dimensions = q.dimension_pass_rates(conn, run_id)
    models = q.cost_by_model(conn, run_id)

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
    )


def build_compare_html(
    conn: duckdb.DuckDBPyConnection, run_a_id: str, run_b_id: str
) -> str:
    """Render the compare HTML for two runs."""
    diff = q.test_diff(conn, run_a_id, run_b_id)
    critical = q.critical_dimensions(conn, run_b_id) or q.critical_dimensions(conn, run_a_id)

    classified = classify_pairs(diff.pairs, critical)
    verdict = compute_verdict(classified)
    meta = VERDICT_META[verdict]

    counts = bucket_counts(classified)
    cat_deltas = category_deltas(classified)
    dim_deltas = dimension_deltas(classified)

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

    env = _env()
    return env.get_template("compare.html.j2").render(
        run_a=diff.run_a,
        run_b=diff.run_b,
        verdict_emoji=meta["emoji"],
        verdict_class=meta["class"],
        verdict_headline=meta["headline"],
        verdict_sub=meta["sub"],
        counts=counts,
        transitions=transitions,
        category_deltas=cat_deltas,
        dimension_deltas=dim_deltas,
    )


def sanitize_for_filename(run_id: str) -> str:
    """Make a Promptfoo evalId safe to use in file paths."""
    return run_id.replace(":", "-").replace("/", "_")
