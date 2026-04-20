"""Shared test fixtures for bi-evals."""

from __future__ import annotations

from pathlib import Path

import pytest

from bi_evals.config import BiEvalsConfig


FIXTURES_DIR = Path(__file__).parent / "fixtures"
EVAL_SAMPLE_DIR = FIXTURES_DIR / "eval_sample"

RUN_A_JSON = EVAL_SAMPLE_DIR / "results" / "eval_20260416_003723.json"  # earlier run
RUN_B_JSON = EVAL_SAMPLE_DIR / "results" / "eval_20260419_231903.json"  # later run (one regression vs A)
RUN_A_ID = "eval-Nbb-2026-04-15T23:37:25"
RUN_B_ID = "eval-11c-2026-04-19T22:19:05"


@pytest.fixture
def eval_sample_config(tmp_path: Path) -> BiEvalsConfig:
    """Load the fixture bi-evals.yaml with its db_path redirected to tmp."""
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg.storage.db_path = str(tmp_path / "test.duckdb")
    cfg.reporting.output_dir = str(tmp_path / "reports")
    return cfg
