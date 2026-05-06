"""Tests for DuckDB query helpers."""

from __future__ import annotations

from pathlib import Path

from bi_evals.config import BiEvalsConfig
from bi_evals.store import connect
from bi_evals.store import queries as q
from bi_evals.store.ingest import ingest_run

from tests.conftest import RUN_A_ID, RUN_A_JSON, RUN_B_ID, RUN_B_JSON


def _seed_both_runs(tmp_path: Path, config: BiEvalsConfig) -> Path:
    db = tmp_path / "x.duckdb"
    with connect(db) as conn:
        ingest_run(conn, RUN_A_JSON, config)
        ingest_run(conn, RUN_B_JSON, config)
    return db


def test_latest_and_previous(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        assert q.latest_run_id(conn) == RUN_B_ID
        assert q.previous_run_id(conn) == RUN_A_ID


def test_get_run(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        run = q.get_run(conn, RUN_B_ID)
    assert run.run_id == RUN_B_ID
    assert run.test_count == 5
    assert run.pass_count + run.fail_count + run.error_count == 5
    assert run.total_cost_usd and run.total_cost_usd > 0


def test_aggregate_by_category(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        cats = q.aggregate_by_category(conn, RUN_B_ID)
    names = {c.category for c in cats}
    assert names == {"cases", "joins", "us-states"}
    # Pass rate is bounded
    for c in cats:
        assert 0.0 <= c.pass_rate <= 1.0
        assert c.test_count > 0


def test_dimension_pass_rates_sorted_worst_first(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        dims = q.dimension_pass_rates(conn, RUN_B_ID)
    assert len(dims) == 9
    rates = [d.pass_rate for d in dims]
    assert rates == sorted(rates)  # ascending (worst first)


def test_cost_by_model(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        models = q.cost_by_model(conn, RUN_B_ID)
    assert len(models) >= 1
    total = sum(m.total_cost_usd for m in models)
    with connect(db) as conn:
        run = q.get_run(conn, RUN_B_ID)
    # Should roughly match (per-test sum may differ slightly from prompt-level aggregate)
    assert abs(total - (run.total_cost_usd or 0)) < 0.01


def test_test_diff_returns_all_tests(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        diff = q.test_diff(conn, RUN_A_ID, RUN_B_ID)
    assert diff.run_a.run_id == RUN_A_ID
    assert diff.run_b.run_id == RUN_B_ID
    assert len(diff.pairs) == 5
    # Every pair has both sides populated (same test set)
    for p in diff.pairs:
        assert p.a_passed is not None
        assert p.b_passed is not None
        assert len(p.a_dims) == 9 and len(p.b_dims) == 9


def test_critical_dimensions(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        crit = q.critical_dimensions(conn, RUN_B_ID)
    assert crit == {"execution", "row_completeness", "value_accuracy"}


def test_list_runs_ordering(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        runs = q.list_runs(conn)
    assert [r.run_id for r in runs] == [RUN_B_ID, RUN_A_ID]


def test_list_projects(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        projects = q.list_projects(conn)
    assert projects == [eval_sample_config.project.name]


def test_list_runs_filtered_by_project(tmp_path: Path, eval_sample_config: BiEvalsConfig) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        match = q.list_runs(conn, project_name=eval_sample_config.project.name)
        miss = q.list_runs(conn, project_name="does-not-exist")
    assert len(match) == 2
    assert len(miss) == 0


def test_get_test_returns_known_failure(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        t = q.get_test(conn, RUN_B_ID, "golden/cases/daily-cases-filtered.yaml")
    assert t.test_id == "golden/cases/daily-cases-filtered.yaml"
    assert t.passed is False
    assert t.fail_reason  # something populated


def test_get_test_unknown_raises(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        try:
            q.get_test(conn, RUN_B_ID, "does-not-exist")
        except KeyError:
            return
    raise AssertionError("expected KeyError")


def test_get_test_extras_has_sql_and_trace(
    tmp_path: Path, eval_sample_config: BiEvalsConfig
) -> None:
    db = _seed_both_runs(tmp_path, eval_sample_config)
    with connect(db) as conn:
        extras = q.get_test_extras(conn, RUN_B_ID, "golden/cases/total-cases-by-country.yaml")
    assert extras["generated_sql"]  # non-empty SQL
    assert isinstance(extras["files_read"], list)
    assert isinstance(extras["trace_json"], str)
