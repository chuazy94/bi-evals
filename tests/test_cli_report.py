"""End-to-end smoke tests for the ingest/report/compare CLI commands."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from click.testing import CliRunner

from bi_evals.cli import cli

from tests.conftest import EVAL_SAMPLE_DIR, RUN_A_JSON, RUN_B_JSON


def _prepare_workspace(tmp_path: Path) -> Path:
    """Copy the fixture project (config + goldens + results) into an isolated dir."""
    workdir = tmp_path / "project"
    shutil.copytree(EVAL_SAMPLE_DIR, workdir)
    # Make sure reports dir exists; CLI should create it but keep test deterministic.
    (workdir / "reports").mkdir(exist_ok=True)
    return workdir


def test_ingest_report_compare_flow(tmp_path: Path) -> None:
    workdir = _prepare_workspace(tmp_path)
    runner = CliRunner()

    # Ingest both runs
    for src in (RUN_A_JSON, RUN_B_JSON):
        dest_rel = Path("results") / src.name
        result = runner.invoke(
            cli,
            ["--config", str(workdir / "bi-evals.yaml"), "ingest", str(workdir / dest_rel)],
        )
        assert result.exit_code == 0, result.output
        assert "Ingested:" in result.output

    # Report (defaults to latest)
    result = runner.invoke(cli, ["--config", str(workdir / "bi-evals.yaml"), "report"])
    assert result.exit_code == 0, result.output
    assert "Report:" in result.output
    reports = list((workdir / "reports").glob("report_*.html"))
    assert len(reports) == 1
    assert "<html" in reports[0].read_text()

    # Compare prev -> latest
    result = runner.invoke(
        cli,
        ["--config", str(workdir / "bi-evals.yaml"), "compare", "prev", "latest"],
    )
    assert result.exit_code == 0, result.output
    assert "Compare:" in result.output
    compares = list((workdir / "reports").glob("compare_*.html"))
    assert len(compares) == 1
    html = compares[0].read_text()
    assert "verdict red" in html  # known regression in the fixture


def test_report_without_db_errors_helpfully(tmp_path: Path) -> None:
    workdir = _prepare_workspace(tmp_path)
    runner = CliRunner()

    # No ingest -> DB is empty
    result = runner.invoke(cli, ["--config", str(workdir / "bi-evals.yaml"), "report"])
    assert result.exit_code != 0
    assert "ingest" in result.output.lower()


def test_compare_rejects_unknown_run_id(tmp_path: Path) -> None:
    workdir = _prepare_workspace(tmp_path)
    runner = CliRunner()
    # Ingest only one run, then try to compare against a bogus id
    result = runner.invoke(
        cli,
        ["--config", str(workdir / "bi-evals.yaml"), "ingest", str(workdir / "results" / RUN_B_JSON.name)],
    )
    assert result.exit_code == 0

    result = runner.invoke(
        cli,
        ["--config", str(workdir / "bi-evals.yaml"), "compare", "nonexistent", "latest"],
    )
    assert result.exit_code != 0
