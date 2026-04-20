"""Tests for HTML report/compare rendering."""

from __future__ import annotations

from pathlib import Path

from bi_evals.config import BiEvalsConfig
from bi_evals.report import build_compare_html, build_report_html
from bi_evals.report.builder import sanitize_for_filename
from bi_evals.store import connect
from bi_evals.store.ingest import ingest_run

from tests.conftest import RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON


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
