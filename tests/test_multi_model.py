"""Tests for multi-model evaluation: config parsing, ingest, per-model aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bi_evals.config import AgentConfig, BiEvalsConfig
from bi_evals.promptfoo.bridge import generate_promptfoo_config
from bi_evals.store import connect
from bi_evals.store import queries as q
from bi_evals.store.ingest import ingest_run

from tests.conftest import EVAL_SAMPLE_DIR, RUN_B_JSON


# --- Config parsing ------------------------------------------------------


def test_singular_model_normalizes_to_models_list() -> None:
    agent = AgentConfig(model="claude-sonnet-4-5")
    assert agent.models == ["claude-sonnet-4-5"]


def test_plural_models_populates_singular_model() -> None:
    agent = AgentConfig(models=["sonnet-4-5", "haiku-4-5"])
    assert agent.model == "sonnet-4-5"
    assert agent.models == ["sonnet-4-5", "haiku-4-5"]


def test_both_model_and_models_with_different_values_fails() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(model="sonnet-4-5", models=["haiku-4-5"])


def test_both_set_but_consistent_is_idempotent() -> None:
    """Pydantic re-validates nested models; {model='x', models=['x']} is normalized state."""
    agent = AgentConfig(model="x", models=["x"])
    assert agent.model == "x" and agent.models == ["x"]


# --- Promptfoo bridge ----------------------------------------------------


def test_bridge_emits_one_provider_per_model(tmp_path: Path) -> None:
    """Generating promptfoo config with two models yields two provider blocks."""
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg.agent.model = ""
    cfg.agent.models = ["sonnet-4-5", "haiku-4-5"]

    pf = generate_promptfoo_config(cfg, str(EVAL_SAMPLE_DIR / "bi-evals.yaml"))
    providers = pf["providers"]
    assert len(providers) == 2
    labels = [p["label"] for p in providers]
    assert labels == ["bi-evals:sonnet-4-5", "bi-evals:haiku-4-5"]


def test_bridge_does_not_write_per_test_repeat_key(tmp_path: Path) -> None:
    """Promptfoo's per-test `repeat` yaml key is not supported; we pass --repeat at CLI instead."""
    cfg = BiEvalsConfig.load(EVAL_SAMPLE_DIR / "bi-evals.yaml")
    cfg.scoring.repeats = 3
    pf = generate_promptfoo_config(cfg, str(EVAL_SAMPLE_DIR / "bi-evals.yaml"))
    for t in pf["tests"]:
        assert "repeat" not in t


# --- Ingest: multi-model matrix ------------------------------------------


def _make_multi_model_eval_json(base: Path, out_path: Path, models: list[str]) -> Path:
    """Synthesize a multi-model eval JSON by duplicating each result entry per model."""
    raw = json.loads(base.read_text())
    originals = list(raw["results"]["results"])
    new_results = []
    for t in originals:
        for model in models:
            copy = json.loads(json.dumps(t))
            meta = copy.setdefault("metadata", {})
            meta["model"] = model
            copy.setdefault("provider", {})["label"] = f"bi-evals:{model}"
            new_results.append(copy)
    raw["results"]["results"] = new_results
    out_path.write_text(json.dumps(raw))
    return out_path


def test_ingest_produces_one_test_row_per_model(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_model_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", ["sonnet-4-5", "haiku-4-5"]
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, patched, eval_sample_config)
        (test_rows,) = conn.execute("SELECT COUNT(*) FROM test_results").fetchone()
        models = q.list_models_for_run(conn, run_id)

    # 5 tests × 2 models = 10 test_results rows
    assert test_rows == 10
    assert set(models) == {"sonnet-4-5", "haiku-4-5"}


def test_model_summary_has_entry_per_model(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_model_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", ["sonnet-4-5", "haiku-4-5"]
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, patched, eval_sample_config)
        summaries = q.model_summary(conn, run_id)

    assert {s.model for s in summaries} == {"sonnet-4-5", "haiku-4-5"}
    for s in summaries:
        assert s.test_count == 5
        assert 0.0 <= s.pass_rate <= 1.0


def test_test_results_by_model_keyed_correctly(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    patched = _make_multi_model_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", ["sonnet-4-5", "haiku-4-5"]
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, patched, eval_sample_config)
        # Pick an arbitrary test_id present in the fixture
        tests = q.list_tests(conn, run_id)
        test_id = tests[0].test_id
        per_model = q.test_results_by_model(conn, run_id, test_id)

    assert set(per_model.keys()) == {"sonnet-4-5", "haiku-4-5"}


def test_diff_keyed_by_test_and_model(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    """Test pairs with different models for same test_id should not collide."""
    patched = _make_multi_model_eval_json(
        RUN_B_JSON, tmp_path / "multi.json", ["sonnet-4-5", "haiku-4-5"]
    )
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        run_id = ingest_run(conn, patched, eval_sample_config)
        diff = q.test_diff(conn, run_id, run_id)

    # 10 pairs (5 tests × 2 models); model field is populated
    assert len(diff.pairs) == 10
    models_seen = {p.model for p in diff.pairs}
    assert models_seen == {"sonnet-4-5", "haiku-4-5"}
