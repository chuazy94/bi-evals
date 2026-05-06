"""Tests for HTML report/compare rendering."""

from __future__ import annotations

from pathlib import Path

from bi_evals.config import BiEvalsConfig
from bi_evals.report import build_compare_html, build_report_html
from bi_evals.report.builder import compute_verdict_sentence, sanitize_for_filename
from bi_evals.store import connect
from bi_evals.store.ingest import ingest_run

from tests.conftest import RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON


class _FakeDim:
    """Minimal stand-in for ``DimRow`` used by verdict-sentence tests."""
    def __init__(self, dimension: str, passed: bool) -> None:
        self.dimension = dimension
        self.passed = passed


def _seed(tmp_path: Path, config: BiEvalsConfig) -> Path:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_A_JSON, config)
        ingest_run(conn, RUN_B_JSON, config)
    return db


def test_report_renders_with_key_content(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(conn, RUN_B_ID)
    assert "<html" in html and "</html>" in html
    assert eval_sample_config.project.name in html
    assert RUN_B_ID in html
    # Categories from fixture goldens
    for cat in ("cases", "joins", "us-states"):
        assert cat in html
    # Dimension section present
    assert "row_completeness" in html
    assert "skill_path_correctness" in html


def test_report_is_self_contained_no_external_urls(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(conn, RUN_B_ID)
    assert "http://" not in html
    assert "https://" not in html


def test_compare_renders_red_verdict_for_known_regression(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Run A → Run B has one regression, so verdict should be red."""
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_compare_html(conn, RUN_A_ID, RUN_B_ID)
    assert "verdict red" in html
    assert "Regressions detected" in html


def test_compare_has_transitions_table(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_compare_html(conn, RUN_A_ID, RUN_B_ID)
    # Regressed test should appear in transitions, with its reason dims
    assert "daily-cases-filtered" in html
    assert "row_completeness" in html or "value_accuracy" in html


def test_compare_no_external_urls(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_compare_html(conn, RUN_A_ID, RUN_B_ID)
    assert "http://" not in html
    assert "https://" not in html


def test_sanitize_for_filename_handles_colons_and_slashes() -> None:
    assert sanitize_for_filename("eval-11c-2026-04-19T22:19:05") == "eval-11c-2026-04-19T22-19-05"
    assert sanitize_for_filename("a/b:c") == "a_b-c"


def test_report_renders_failure_reasons(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(conn, RUN_B_ID)
    # Failures section header is present (run B has 1 known failure)
    assert "Failures" in html
    # The failing test surfaces in the failures table
    assert "daily-cases-filtered" in html


def test_report_renders_scoring_rule_callout(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(
            conn, RUN_B_ID,
            pass_threshold=0.75,
            critical_dimensions=["execution", "row_completeness", "value_accuracy"],
        )
    assert "Scoring rule" in html
    assert "critical dimension" in html
    assert "0.75" in html
    for dim in ("execution", "row_completeness", "value_accuracy"):
        assert dim in html


def test_report_weighted_score_column_header_uses_threshold(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(conn, RUN_B_ID, pass_threshold=0.90)
    # The "All tests" + "Failures" tables both share the header text.
    assert "Weighted score (&ge; 0.90)" in html or "Weighted score (\u2265 0.90)" in html
    # Bare "Score" header (without the qualifier) should be gone from the
    # tables we updated. Other "Score" labels (e.g. dimension table column
    # "Score") may still exist, but the precise standalone "<th class=\"num\">Score</th>"
    # should not.
    assert '<th class="num">Score</th>' not in html


def test_report_pass_threshold_threading(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Custom threshold should propagate into the rendered HTML verbatim."""
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(
            conn, RUN_B_ID,
            pass_threshold=0.42,
            critical_dimensions=["execution"],
        )
    assert "0.42" in html
    # The default 0.75 should NOT appear in the scoring callout / column header
    # for this overridden run. (It can still appear elsewhere, e.g. as a
    # numeric score; assert against the formatted column header instead.)
    assert "Weighted score (&ge; 0.75)" not in html
    assert "Weighted score (\u2265 0.75)" not in html


def test_compute_verdict_sentence_pass_path() -> None:
    sentence = compute_verdict_sentence(
        passed=True,
        score=0.91,
        dimensions=[_FakeDim("execution", True), _FakeDim("value_accuracy", True)],
        pass_threshold=0.75,
        critical_dimensions=["execution", "value_accuracy"],
    )
    assert sentence.startswith("Passed:")
    assert "all critical dimensions green" in sentence
    assert "0.91" in sentence
    assert "0.75" in sentence


def test_compute_verdict_sentence_fail_critical_path() -> None:
    sentence = compute_verdict_sentence(
        passed=False,
        score=0.62,
        dimensions=[
            _FakeDim("execution", True),
            _FakeDim("value_accuracy", False),
        ],
        pass_threshold=0.75,
        critical_dimensions=["execution", "value_accuracy"],
    )
    assert sentence.startswith("Failed:")
    assert "value_accuracy" in sentence
    assert "critical dimension" in sentence


def test_compute_verdict_sentence_fail_threshold_path() -> None:
    sentence = compute_verdict_sentence(
        passed=False,
        score=0.62,
        dimensions=[
            _FakeDim("execution", True),
            _FakeDim("value_accuracy", True),
            _FakeDim("row_completeness", True),
        ],
        pass_threshold=0.75,
        critical_dimensions=["execution", "value_accuracy", "row_completeness"],
    )
    assert sentence.startswith("Failed:")
    assert "0.62" in sentence
    assert "0.75" in sentence
    assert "3/3 critical green" in sentence


def test_report_filter_by_category_excludes_other_categories(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed(tmp_path, eval_sample_config)
    with connect(db) as conn:
        html = build_report_html(conn, RUN_B_ID, category="cases")
    # The "All tests" table should only contain cases tests, not joins or us-states.
    # The category dropdown lists every category, so we can't just check absence
    # of the strings "joins"/"us-states" globally. Instead, check that the per-test
    # rows (mono test_id cells with .yaml suffix) only mention `cases`.
    import re
    test_id_rows = re.findall(r'href="/runs/[^"]+/tests/([^"?]+)', html)
    assert test_id_rows, "expected at least one test row"
    for tid in test_id_rows:
        assert "/cases/" in tid, f"unexpected non-cases row: {tid}"
