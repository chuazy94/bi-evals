"""Read helpers over the DuckDB store.

All functions return frozen dataclasses so callers (report/compare) don't depend
on DuckDB row shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb


@dataclass(frozen=True)
class RunRow:
    run_id: str
    project_name: str
    timestamp: datetime | None
    test_count: int
    pass_count: int
    fail_count: int
    error_count: int
    total_cost_usd: float | None
    total_latency_ms: int | None
    total_prompt_tokens: int | None
    total_completion_tokens: int | None


@dataclass(frozen=True)
class TestRow:
    test_id: str
    category: str | None
    difficulty: str | None
    question: str | None
    passed: bool
    score: float
    fail_reason: str | None
    cost_usd: float | None
    latency_ms: int | None
    model: str | None


@dataclass(frozen=True)
class DimRow:
    dimension: str
    passed: bool
    score: float
    reason: str | None
    is_critical: bool
    weight: float | None


@dataclass(frozen=True)
class CategoryAgg:
    category: str
    test_count: int
    pass_count: int
    pass_rate: float
    avg_score: float


@dataclass(frozen=True)
class DimAgg:
    dimension: str
    pass_count: int
    total: int
    pass_rate: float
    is_critical: bool


@dataclass(frozen=True)
class ModelCostAgg:
    model: str
    test_count: int
    total_cost_usd: float
    total_tokens: int


@dataclass(frozen=True)
class RunTestPair:
    """One test compared across two runs. Either side may be None."""
    test_id: str
    category: str | None
    a_passed: bool | None
    a_score: float | None
    b_passed: bool | None
    b_score: float | None
    a_dims: dict[str, bool]  # dimension -> passed
    b_dims: dict[str, bool]


@dataclass(frozen=True)
class RunDiff:
    run_a: RunRow
    run_b: RunRow
    pairs: list[RunTestPair]


def latest_run_id(conn: duckdb.DuckDBPyConnection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM runs ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def previous_run_id(conn: duckdb.DuckDBPyConnection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM runs ORDER BY timestamp DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    return row[0] if row else None


def get_run(conn: duckdb.DuckDBPyConnection, run_id: str) -> RunRow:
    row = conn.execute(
        """
        SELECT run_id, project_name, timestamp, test_count, pass_count, fail_count,
               error_count, total_cost_usd, total_latency_ms,
               total_prompt_tokens, total_completion_tokens
        FROM runs WHERE run_id = ?
        """,
        [run_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"Run not found: {run_id}")
    return RunRow(*row)


def list_tests(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[TestRow]:
    rows = conn.execute(
        """
        SELECT test_id, category, difficulty, question, passed, score, fail_reason,
               cost_usd, latency_ms, model
        FROM test_results
        WHERE run_id = ?
        ORDER BY category, test_id
        """,
        [run_id],
    ).fetchall()
    return [TestRow(*r) for r in rows]


def list_dimensions(
    conn: duckdb.DuckDBPyConnection, run_id: str, test_id: str
) -> list[DimRow]:
    rows = conn.execute(
        """
        SELECT dimension, passed, score, reason, is_critical, weight
        FROM dimension_results
        WHERE run_id = ? AND test_id = ?
        ORDER BY dimension
        """,
        [run_id, test_id],
    ).fetchall()
    return [DimRow(*r) for r in rows]


def aggregate_by_category(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> list[CategoryAgg]:
    rows = conn.execute(
        """
        SELECT COALESCE(category, '(uncategorized)') AS cat,
               COUNT(*) AS total,
               SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS passes,
               AVG(score) AS avg_score
        FROM test_results
        WHERE run_id = ?
        GROUP BY cat
        ORDER BY cat
        """,
        [run_id],
    ).fetchall()
    return [
        CategoryAgg(
            category=r[0],
            test_count=int(r[1]),
            pass_count=int(r[2]),
            pass_rate=(r[2] / r[1]) if r[1] else 0.0,
            avg_score=float(r[3] or 0.0),
        )
        for r in rows
    ]


def dimension_pass_rates(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> list[DimAgg]:
    rows = conn.execute(
        """
        SELECT dimension,
               SUM(CASE WHEN passed THEN 1 ELSE 0 END) AS passes,
               COUNT(*) AS total,
               BOOL_OR(is_critical) AS is_critical
        FROM dimension_results
        WHERE run_id = ?
        GROUP BY dimension
        ORDER BY (SUM(CASE WHEN passed THEN 1 ELSE 0 END)::DOUBLE / COUNT(*)) ASC,
                 dimension ASC
        """,
        [run_id],
    ).fetchall()
    return [
        DimAgg(
            dimension=r[0],
            pass_count=int(r[1]),
            total=int(r[2]),
            pass_rate=(r[1] / r[2]) if r[2] else 0.0,
            is_critical=bool(r[3]),
        )
        for r in rows
    ]


def cost_by_model(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> list[ModelCostAgg]:
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(model, ''), '(unknown)') AS m,
               COUNT(*) AS tests,
               COALESCE(SUM(cost_usd), 0.0) AS cost,
               COALESCE(SUM(total_tokens), 0) AS tokens
        FROM test_results
        WHERE run_id = ?
        GROUP BY m
        ORDER BY cost DESC
        """,
        [run_id],
    ).fetchall()
    return [
        ModelCostAgg(
            model=r[0],
            test_count=int(r[1]),
            total_cost_usd=float(r[2] or 0.0),
            total_tokens=int(r[3] or 0),
        )
        for r in rows
    ]


def test_diff(
    conn: duckdb.DuckDBPyConnection, run_a_id: str, run_b_id: str
) -> RunDiff:
    """Build the full cross-run test comparison. Loads both runs' tests + dims."""
    run_a = get_run(conn, run_a_id)
    run_b = get_run(conn, run_b_id)

    a_tests = {t.test_id: t for t in list_tests(conn, run_a_id)}
    b_tests = {t.test_id: t for t in list_tests(conn, run_b_id)}

    a_dims = _dims_by_test(conn, run_a_id)
    b_dims = _dims_by_test(conn, run_b_id)

    all_ids = sorted(set(a_tests) | set(b_tests))
    pairs: list[RunTestPair] = []
    for tid in all_ids:
        a = a_tests.get(tid)
        b = b_tests.get(tid)
        category = (b.category if b else a.category) if (a or b) else None
        pairs.append(
            RunTestPair(
                test_id=tid,
                category=category,
                a_passed=a.passed if a else None,
                a_score=a.score if a else None,
                b_passed=b.passed if b else None,
                b_score=b.score if b else None,
                a_dims=a_dims.get(tid, {}),
                b_dims=b_dims.get(tid, {}),
            )
        )

    return RunDiff(run_a=run_a, run_b=run_b, pairs=pairs)


def _dims_by_test(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[str, dict[str, bool]]:
    rows = conn.execute(
        """
        SELECT test_id, dimension, passed
        FROM dimension_results
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchall()
    out: dict[str, dict[str, bool]] = {}
    for test_id, dim, passed in rows:
        out.setdefault(test_id, {})[dim] = bool(passed)
    return out


def critical_dimensions(conn: duckdb.DuckDBPyConnection, run_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT dimension
        FROM dimension_results
        WHERE run_id = ? AND is_critical = TRUE
        """,
        [run_id],
    ).fetchall()
    return {r[0] for r in rows}


def list_runs(conn: duckdb.DuckDBPyConnection, limit: int = 50) -> list[RunRow]:
    rows = conn.execute(
        """
        SELECT run_id, project_name, timestamp, test_count, pass_count, fail_count,
               error_count, total_cost_usd, total_latency_ms,
               total_prompt_tokens, total_completion_tokens
        FROM runs
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [RunRow(*r) for r in rows]


def _to_dict(obj: Any) -> dict[str, Any]:
    """Small helper for templates that expect dicts."""
    return obj.__dict__ if hasattr(obj, "__dict__") else dict(obj)
