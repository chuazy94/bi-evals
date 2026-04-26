"""Tests for Phase 6b cost alerts: median, threshold, per-test anomalies, history view."""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from bi_evals.store import queries as q
from bi_evals.store.schema import ensure_schema


def _seed_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    timestamp: datetime,
    total_cost: float | None,
    *,
    project: str = "p",
    test_costs: dict[str, float] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, project_name, timestamp, config_snapshot,
            eval_json_path, test_count, pass_count, fail_count, error_count,
            total_cost_usd)
        VALUES (?, ?, ?, '{}', '/p', ?, 0, 0, 0, ?)
        """,
        [run_id, project, timestamp, len(test_costs or {}), total_cost],
    )
    for tid, cost in (test_costs or {}).items():
        conn.execute(
            """
            INSERT INTO test_results (
                run_id, test_id, model, golden_id, category, difficulty, tags,
                question, description, reference_sql, generated_sql, files_read,
                trace_file_path, trace_json, passed, score, fail_reason,
                cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                provider, trial_count, pass_count, pass_rate, score_mean, score_stddev
            ) VALUES (?, ?, '', ?, 'c', 'easy', '[]', 'q', 'd', '', '', '[]',
                      NULL, NULL, true, 1.0, NULL, ?, 0, 0, 0, 0, '', 1, 1, 1.0, 1.0, 0.0)
            """,
            [run_id, tid, tid, cost],
        )


def _conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    return conn


# --- _median + threshold ---------------------------------------------------


def test_no_alert_when_multiplier_zero() -> None:
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(5):
        _seed_run(conn, f"r{i}", base - timedelta(hours=i), 1.0)
    _seed_run(conn, "current", base + timedelta(hours=1), 100.0)
    assert q.cost_alerts(conn, "current", multiplier=0) is None


def test_no_alert_with_insufficient_history() -> None:
    """Fewer than 3 prior runs is too noisy — skip."""
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    _seed_run(conn, "r0", base - timedelta(hours=2), 1.0)
    _seed_run(conn, "r1", base - timedelta(hours=1), 1.0)
    _seed_run(conn, "current", base, 100.0)  # would be 100x but only 2 priors
    assert q.cost_alerts(conn, "current") is None


def test_alert_fires_when_cost_exceeds_threshold() -> None:
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(5):
        _seed_run(conn, f"r{i}", base - timedelta(hours=i + 1), 1.0)
    _seed_run(conn, "current", base, 3.0)  # 3x median (1.0)

    alert = q.cost_alerts(conn, "current", multiplier=2.0)
    assert alert is not None
    assert alert.actual_cost == 3.0
    assert alert.median_cost == 1.0
    assert alert.multiplier == 3.0
    assert alert.sample_size == 5


def test_no_alert_when_below_threshold() -> None:
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(5):
        _seed_run(conn, f"r{i}", base - timedelta(hours=i + 1), 1.0)
    _seed_run(conn, "current", base, 1.5)  # 1.5x — below 2.0 threshold
    assert q.cost_alerts(conn, "current", multiplier=2.0) is None


def test_alert_only_uses_same_project_history() -> None:
    """A different project's runs should not influence the median."""
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    # Other project: high costs.
    for i in range(5):
        _seed_run(conn, f"other{i}", base - timedelta(hours=i + 1), 100.0, project="other")
    # Our project: 3 cheap priors, then a 2.5x spike.
    for i in range(3):
        _seed_run(conn, f"r{i}", base - timedelta(hours=i + 1), 1.0)
    _seed_run(conn, "current", base, 2.5)
    alert = q.cost_alerts(conn, "current", multiplier=2.0)
    assert alert is not None
    assert alert.median_cost == 1.0  # not contaminated by 'other' project


def test_window_caps_history_size() -> None:
    """Only the most recent N prior runs feed the median."""
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    # 3 cheap recent + 5 expensive old. window=3 → median should be the cheap ones.
    for i, cost in enumerate([1.0, 1.0, 1.0]):
        _seed_run(conn, f"new{i}", base - timedelta(hours=i + 1), cost)
    for i, cost in enumerate([100.0] * 5):
        _seed_run(conn, f"old{i}", base - timedelta(days=i + 1), cost)
    _seed_run(conn, "current", base, 3.0)

    alert = q.cost_alerts(conn, "current", multiplier=2.0, window=3)
    assert alert is not None
    assert alert.median_cost == 1.0


# --- per-test anomalies ----------------------------------------------------


def test_per_test_anomaly_flagged() -> None:
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    # 5 priors with test "t1" costing $0.10, run-level total dominated by t1.
    for i in range(5):
        _seed_run(
            conn, f"r{i}", base - timedelta(hours=i + 1),
            total_cost=0.10, test_costs={"t1": 0.10},
        )
    _seed_run(
        conn, "current", base,
        total_cost=0.50, test_costs={"t1": 0.50},  # 5x median
    )
    alert = q.cost_alerts(conn, "current", multiplier=2.0)
    assert alert is not None
    assert any(t[0] == "t1" for t in alert.anomalous_tests)


# --- cost_history view ------------------------------------------------------


def test_cost_history_marks_recent_runs_with_multipliers() -> None:
    conn = _conn()
    base = datetime(2026, 4, 25, 12, 0, 0)
    for i in range(4):
        _seed_run(conn, f"r{i}", base - timedelta(hours=i + 1), 1.0)
    _seed_run(conn, "spike", base, 4.0)

    history = q.cost_history(conn, last_n=10)
    multipliers = {r.run_id: m for r, m in history}
    assert multipliers["spike"] == 4.0
    # Earliest runs lack history → multiplier 0.0.
    assert multipliers["r3"] == 0.0
