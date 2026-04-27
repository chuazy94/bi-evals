"""Read helpers over the DuckDB store.

All functions return frozen dataclasses so callers (report/compare) don't depend
on DuckDB row shapes.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
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
    trial_count: int = 1
    pass_count: int = 1
    pass_rate: float = 1.0
    score_mean: float = 0.0
    score_stddev: float = 0.0


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
    """One (test, model) compared across two runs. Either side may be None.

    Phase 6a adds ``model`` as part of the pair identity and ``a_pass_rate`` /
    ``b_pass_rate`` so rate-based compare can threshold on distribution shifts,
    not just boolean flips.
    """
    test_id: str
    category: str | None
    model: str | None
    a_passed: bool | None
    a_score: float | None
    a_pass_rate: float | None
    b_passed: bool | None
    b_score: float | None
    b_pass_rate: float | None
    a_dims: dict[str, float]  # dimension -> pass_rate (0.0–1.0)
    b_dims: dict[str, float]


@dataclass(frozen=True)
class ModelSummary:
    model: str
    test_count: int
    pass_count: int
    pass_rate: float
    avg_score: float
    total_cost_usd: float
    avg_latency_ms: float


@dataclass(frozen=True)
class TestStability:
    test_id: str
    runs_observed: int
    flip_count: int           # number of pass → fail or fail → pass transitions
    longest_pass_streak: int
    longest_fail_streak: int
    current_streak: int       # positive = pass, negative = fail (magnitude = length)
    pass_rate_overall: float


@dataclass(frozen=True)
class RunDiff:
    run_a: RunRow
    run_b: RunRow
    pairs: list[RunTestPair]


@dataclass(frozen=True)
class PromptDiff:
    """Files the agent read in run_a vs run_b, classified by drift bucket."""
    added: list[str]
    removed: list[str]
    modified: list[str]
    unchanged: list[str]


@dataclass(frozen=True)
class CostAlert:
    run_id: str
    actual_cost: float
    median_cost: float
    multiplier: float
    sample_size: int  # how many prior runs went into the median
    anomalous_tests: list[tuple[str, float, float]]  # (test_id, actual, median)


@dataclass(frozen=True)
class StaleGolden:
    """One row for the staleness warning. Either ``last_verified_at`` is None
    (unverified) or it predates the threshold (stale)."""
    test_id: str
    last_verified_at: date | None
    days_since_verified: int | None  # None if never verified


@dataclass(frozen=True)
class StaleKnowledgeFile:
    """Phase 6d: a knowledge file whose mtime is older than the threshold AND
    that was actually read in the relevant run.

    Missing files are filtered out before construction (the prompt_diff already
    surfaces deletions), so these fields are always populated.
    """
    path: str  # relative-to-project path as stored in prompt_snapshot
    mtime: date
    days_since_modified: int


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
               cost_usd, latency_ms, model,
               COALESCE(trial_count, 1),
               COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END),
               COALESCE(pass_rate, CASE WHEN passed THEN 1.0 ELSE 0.0 END),
               COALESCE(score_mean, score),
               COALESCE(score_stddev, 0.0)
        FROM test_results
        WHERE run_id = ?
        ORDER BY category, test_id, model
        """,
        [run_id],
    ).fetchall()
    return [TestRow(*r) for r in rows]


def list_dimensions(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    test_id: str,
    model: str | None = None,
) -> list[DimRow]:
    """Aggregate per-trial dimension results for a (run, test[, model]).

    Returns one ``DimRow`` per dimension with ``score`` being the mean across
    trials and ``passed`` being True iff every trial passed.
    """
    sql = """
        SELECT dimension,
               BOOL_AND(passed) AS all_passed,
               AVG(score) AS mean_score,
               ANY_VALUE(reason) AS reason,
               BOOL_OR(is_critical) AS is_critical,
               ANY_VALUE(weight) AS weight
        FROM dimension_results
        WHERE run_id = ? AND test_id = ?
    """
    params: list[Any] = [run_id, test_id]
    if model is not None:
        sql += " AND model = ?"
        params.append(model)
    sql += " GROUP BY dimension ORDER BY dimension"
    rows = conn.execute(sql, params).fetchall()
    return [DimRow(*r) for r in rows]


def aggregate_by_category(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> list[CategoryAgg]:
    """Category-level aggregates across trials (weighted by trial_count).

    Uses trial counts so multi-trial runs report the true pass rate rather than
    the per-(test, model) aggregate bit.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(category, '(uncategorized)') AS cat,
               SUM(COALESCE(trial_count, 1))                            AS total_trials,
               SUM(COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END)) AS passes,
               AVG(COALESCE(score_mean, score))                         AS avg_score
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
    """Build the full cross-run test comparison, keyed by (test_id, model)."""
    run_a = get_run(conn, run_a_id)
    run_b = get_run(conn, run_b_id)

    a_tests = {(t.test_id, t.model or ""): t for t in list_tests(conn, run_a_id)}
    b_tests = {(t.test_id, t.model or ""): t for t in list_tests(conn, run_b_id)}

    a_dims = _dims_by_test(conn, run_a_id)
    b_dims = _dims_by_test(conn, run_b_id)

    all_keys = sorted(set(a_tests) | set(b_tests))
    pairs: list[RunTestPair] = []
    for key in all_keys:
        a = a_tests.get(key)
        b = b_tests.get(key)
        category = (b.category if b else a.category) if (a or b) else None
        pairs.append(
            RunTestPair(
                test_id=key[0],
                category=category,
                model=key[1] or None,
                a_passed=a.passed if a else None,
                a_score=a.score if a else None,
                a_pass_rate=a.pass_rate if a else None,
                b_passed=b.passed if b else None,
                b_score=b.score if b else None,
                b_pass_rate=b.pass_rate if b else None,
                a_dims=a_dims.get(key, {}),
                b_dims=b_dims.get(key, {}),
            )
        )

    return RunDiff(run_a=run_a, run_b=run_b, pairs=pairs)


def _dims_by_test(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[tuple[str, str], dict[str, float]]:
    """Aggregate per-trial dimension pass rates, keyed by (test_id, model)."""
    rows = conn.execute(
        """
        SELECT test_id, model, dimension,
               AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END) AS pass_rate
        FROM dimension_results
        WHERE run_id = ?
        GROUP BY test_id, model, dimension
        """,
        [run_id],
    ).fetchall()
    out: dict[tuple[str, str], dict[str, float]] = {}
    for test_id, model, dim, pass_rate in rows:
        out.setdefault((test_id, model or ""), {})[dim] = float(pass_rate)
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


def list_models_for_run(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[str]:
    """Distinct non-empty models present in this run's test_results."""
    rows = conn.execute(
        """
        SELECT DISTINCT NULLIF(model, '') AS m
        FROM test_results
        WHERE run_id = ? AND NULLIF(model, '') IS NOT NULL
        ORDER BY m
        """,
        [run_id],
    ).fetchall()
    return [r[0] for r in rows]


def test_results_by_model(
    conn: duckdb.DuckDBPyConnection, run_id: str, test_id: str
) -> dict[str, TestRow]:
    """All (test_id) rows keyed by model for a given run."""
    rows = conn.execute(
        """
        SELECT test_id, category, difficulty, question, passed, score, fail_reason,
               cost_usd, latency_ms, model,
               COALESCE(trial_count, 1),
               COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END),
               COALESCE(pass_rate, CASE WHEN passed THEN 1.0 ELSE 0.0 END),
               COALESCE(score_mean, score),
               COALESCE(score_stddev, 0.0)
        FROM test_results
        WHERE run_id = ? AND test_id = ?
        """,
        [run_id, test_id],
    ).fetchall()
    out: dict[str, TestRow] = {}
    for r in rows:
        row = TestRow(*r)
        out[row.model or ""] = row
    return out


def model_summary(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[ModelSummary]:
    """Per-model aggregates: pass rate, avg score, total cost, avg latency."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(model, ''), '(unknown)') AS m,
               COUNT(*) AS tests,
               SUM(COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END)) AS passes,
               SUM(COALESCE(trial_count, 1)) AS trials,
               AVG(COALESCE(score_mean, score)) AS mean_score,
               COALESCE(SUM(cost_usd), 0.0) AS cost,
               AVG(COALESCE(latency_ms, 0)) AS avg_latency
        FROM test_results
        WHERE run_id = ?
        GROUP BY m
        ORDER BY mean_score DESC, cost ASC
        """,
        [run_id],
    ).fetchall()
    out: list[ModelSummary] = []
    for r in rows:
        trials = int(r[3] or 0)
        passes = int(r[2] or 0)
        out.append(
            ModelSummary(
                model=r[0],
                test_count=int(r[1]),
                pass_count=passes,
                pass_rate=(passes / trials) if trials else 0.0,
                avg_score=float(r[4] or 0.0),
                total_cost_usd=float(r[5] or 0.0),
                avg_latency_ms=float(r[6] or 0.0),
            )
        )
    return out


def test_stability(
    conn: duckdb.DuckDBPyConnection, test_id: str, *, last_n_runs: int = 10
) -> TestStability:
    """Pass/fail history for a single test across the most recent N runs."""
    rows = conn.execute(
        """
        WITH ordered AS (
            SELECT r.run_id, r.timestamp,
                   AVG(COALESCE(tr.pass_rate, CASE WHEN tr.passed THEN 1.0 ELSE 0.0 END)) AS pass_rate
            FROM runs r
            JOIN test_results tr USING (run_id)
            WHERE tr.test_id = ?
            GROUP BY r.run_id, r.timestamp
            ORDER BY r.timestamp DESC
            LIMIT ?
        )
        SELECT pass_rate FROM ordered ORDER BY timestamp ASC
        """,
        [test_id, last_n_runs],
    ).fetchall()

    # Treat pass_rate >= 0.5 as a pass for stability-transition purposes.
    outcomes = [float(r[0]) >= 0.5 for r in rows]
    return _compute_stability(test_id, outcomes)


def flakiest_tests(
    conn: duckdb.DuckDBPyConnection, *, last_n_runs: int = 10, limit: int = 20
) -> list[TestStability]:
    """Rank tests by flip count across the most recent N runs. Ties break by lower pass rate."""
    test_ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT test_id FROM test_results
            WHERE test_id IS NOT NULL AND test_id != ''
            """
        ).fetchall()
    ]
    all_stab = [
        test_stability(conn, tid, last_n_runs=last_n_runs) for tid in test_ids
    ]
    # Only surface tests we've actually observed more than once.
    all_stab = [s for s in all_stab if s.runs_observed > 1]
    all_stab.sort(key=lambda s: (-s.flip_count, s.pass_rate_overall, s.test_id))
    return all_stab[:limit]


def _compute_stability(test_id: str, outcomes: list[bool]) -> TestStability:
    """Core counting logic, kept pure for unit-testability."""
    if not outcomes:
        return TestStability(
            test_id=test_id,
            runs_observed=0,
            flip_count=0,
            longest_pass_streak=0,
            longest_fail_streak=0,
            current_streak=0,
            pass_rate_overall=0.0,
        )

    flips = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])
    longest_pass = longest_fail = 0
    run_pass = run_fail = 0
    for o in outcomes:
        if o:
            run_pass += 1
            run_fail = 0
            longest_pass = max(longest_pass, run_pass)
        else:
            run_fail += 1
            run_pass = 0
            longest_fail = max(longest_fail, run_fail)

    # Current streak: sign follows last outcome; magnitude is trailing run length.
    last = outcomes[-1]
    trailing = 0
    for o in reversed(outcomes):
        if o == last:
            trailing += 1
        else:
            break
    current = trailing if last else -trailing

    passes = sum(1 for o in outcomes if o)
    return TestStability(
        test_id=test_id,
        runs_observed=len(outcomes),
        flip_count=flips,
        longest_pass_streak=longest_pass,
        longest_fail_streak=longest_fail,
        current_streak=current,
        pass_rate_overall=passes / len(outcomes),
    )


# --- Phase 6b: drift, staleness, cost alerts ----------------------------


def _load_prompt_snapshot(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[str, dict[str, Any]] | None:
    """Return the run's prompt_snapshot as a dict.

    Returns ``None`` when no snapshot was recorded (pre-6b runs, or runs where
    the agent read no files at all). Distinct from ``{}``, which would be a
    snapshot that exists but is empty — currently unreachable, but keeping the
    distinction lets ``prompt_diff`` suppress noise when one side is pre-6b.
    """
    row = conn.execute(
        "SELECT prompt_snapshot FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    if not row or row[0] is None:
        return None
    raw = row[0]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def prompt_diff(
    conn: duckdb.DuckDBPyConnection, run_a_id: str, run_b_id: str
) -> PromptDiff:
    """Compare the file-hash snapshots of two runs.

    If either run lacks a prompt_snapshot (pre-6b), returns an empty diff
    rather than reporting every file as added/removed — the absence of a
    snapshot is "we didn't track," not "the file wasn't there."
    """
    a = _load_prompt_snapshot(conn, run_a_id)
    b = _load_prompt_snapshot(conn, run_b_id)

    if a is None or b is None:
        return PromptDiff(added=[], removed=[], modified=[], unchanged=[])

    a_keys = set(a.keys())
    b_keys = set(b.keys())

    added = sorted(b_keys - a_keys)
    removed = sorted(a_keys - b_keys)
    modified: list[str] = []
    unchanged: list[str] = []
    for key in sorted(a_keys & b_keys):
        if (a[key] or {}).get("sha256") != (b[key] or {}).get("sha256"):
            modified.append(key)
        else:
            unchanged.append(key)

    return PromptDiff(added=added, removed=removed, modified=modified, unchanged=unchanged)


def files_read_for_run(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> dict[tuple[str, str], list[str]]:
    """Map (test_id, model) → list of files the agent read in that test.

    Used by the compare view to annotate regressed rows with which of their
    files changed. Returns relative paths matching the prompt_snapshot keys.
    """
    rows = conn.execute(
        """
        SELECT test_id, COALESCE(model, ''), files_read
        FROM test_results
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchall()
    out: dict[tuple[str, str], list[str]] = {}
    for test_id, model, files_json in rows:
        if not files_json:
            out[(test_id, model)] = []
            continue
        try:
            files = json.loads(files_json) if isinstance(files_json, str) else files_json
        except json.JSONDecodeError:
            files = []
        out[(test_id, model)] = list(files or [])
    return out


def stale_goldens(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    stale_after_days: int,
    today: date | None = None,
) -> tuple[list[StaleGolden], list[StaleGolden]]:
    """Return (stale, unverified) lists for goldens in a given run.

    ``stale``: have a ``last_verified_at`` older than the threshold.
    ``unverified``: never set ``last_verified_at``.
    Both lists are sorted by age descending (longest-stale first).
    """
    if stale_after_days <= 0:
        return [], []
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT DISTINCT test_id, last_verified_at
        FROM test_results
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchall()

    stale: list[StaleGolden] = []
    unverified: list[StaleGolden] = []
    for test_id, last in rows:
        if last is None:
            unverified.append(StaleGolden(test_id=test_id, last_verified_at=None, days_since_verified=None))
            continue
        days = (today - last).days
        if days > stale_after_days:
            stale.append(StaleGolden(test_id=test_id, last_verified_at=last, days_since_verified=days))
    stale.sort(key=lambda g: -(g.days_since_verified or 0))
    unverified.sort(key=lambda g: g.test_id)
    return stale, unverified


def stale_knowledge_files(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    base_dir: Path,
    stale_after_days: int,
    today: date | None = None,
) -> list[StaleKnowledgeFile]:
    """Phase 6d: knowledge files read in ``run_id`` whose mtime is older than the threshold.

    Re-stats each file at call time (current disk state, not the snapshot's
    captured mtime). A missing file is skipped silently — the file may have
    been moved or renamed since the run, which the prompt_diff already
    surfaces. Sorted oldest-first.

    ``stale_after_days = 0`` disables the check.
    """
    if stale_after_days <= 0:
        return []
    snapshot = _load_prompt_snapshot(conn, run_id)
    if not snapshot:
        return []

    today = today or date.today()
    out: list[StaleKnowledgeFile] = []
    for rel_path in snapshot.keys():
        path = Path(rel_path)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.exists():
            continue
        try:
            mtime_ts = path.stat().st_mtime
        except OSError:
            continue
        mtime_date = date.fromtimestamp(mtime_ts)
        days = (today - mtime_date).days
        if days > stale_after_days:
            out.append(StaleKnowledgeFile(
                path=rel_path,
                mtime=mtime_date,
                days_since_modified=days,
            ))
    out.sort(key=lambda f: -f.days_since_modified)
    return out


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def cost_alerts(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    multiplier: float = 2.0,
    window: int = 10,
    min_history: int = 3,
) -> CostAlert | None:
    """Flag a run if its cost exceeds ``multiplier`` × median of prior runs.

    Returns ``None`` when:
      - the multiplier is ``0`` (feature disabled)
      - there are fewer than ``min_history`` prior runs (median unstable)
      - the run's cost is at or below the threshold

    Per-test anomalies use the same rule against the test's prior history.
    """
    if multiplier <= 0:
        return None

    row = conn.execute(
        "SELECT total_cost_usd, project_name, timestamp FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if not row or row[0] is None:
        return None
    actual = float(row[0])
    project_name = row[1]
    run_ts = row[2]

    # Prior runs (same project, strictly older), most-recent first, up to window.
    prior_rows = conn.execute(
        """
        SELECT total_cost_usd FROM runs
        WHERE project_name = ?
          AND timestamp < ?
          AND total_cost_usd IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        [project_name, run_ts, window],
    ).fetchall()
    prior = [float(r[0]) for r in prior_rows if r[0] is not None]
    if len(prior) < min_history:
        return None

    median_cost = _median(prior)
    if median_cost <= 0 or actual <= median_cost * multiplier:
        return None

    # Per-test anomalies: compare each test in this run vs its own history.
    test_costs = conn.execute(
        "SELECT test_id, cost_usd FROM test_results WHERE run_id = ? AND cost_usd IS NOT NULL",
        [run_id],
    ).fetchall()
    anomalous: list[tuple[str, float, float]] = []
    for test_id, cost in test_costs:
        hist_rows = conn.execute(
            """
            SELECT tr.cost_usd FROM test_results tr
            JOIN runs r USING (run_id)
            WHERE tr.test_id = ?
              AND r.project_name = ?
              AND r.timestamp < ?
              AND tr.cost_usd IS NOT NULL
            ORDER BY r.timestamp DESC
            LIMIT ?
            """,
            [test_id, project_name, run_ts, window],
        ).fetchall()
        hist = [float(h[0]) for h in hist_rows if h[0] is not None]
        if len(hist) < min_history:
            continue
        med = _median(hist)
        if med > 0 and float(cost) > med * multiplier:
            anomalous.append((test_id, float(cost), med))
    anomalous.sort(key=lambda x: -(x[1] / x[2] if x[2] else 0.0))

    return CostAlert(
        run_id=run_id,
        actual_cost=actual,
        median_cost=median_cost,
        multiplier=actual / median_cost if median_cost else 0.0,
        sample_size=len(prior),
        anomalous_tests=anomalous,
    )


def cost_history(
    conn: duckdb.DuckDBPyConnection, *, last_n: int = 20
) -> list[tuple[RunRow, float]]:
    """Recent runs paired with their multiplier vs. their own prior-window median.

    For each run, the multiplier is computed against the (up-to-window) runs
    that preceded it. Runs without enough history get multiplier=0.0.
    """
    runs = list_runs(conn, limit=last_n)
    out: list[tuple[RunRow, float]] = []
    for r in runs:
        if r.total_cost_usd is None:
            out.append((r, 0.0))
            continue
        prior = conn.execute(
            """
            SELECT total_cost_usd FROM runs
            WHERE project_name = ?
              AND timestamp < ?
              AND total_cost_usd IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 10
            """,
            [r.project_name, r.timestamp],
        ).fetchall()
        prior_vals = [float(p[0]) for p in prior if p[0] is not None]
        if len(prior_vals) < 3:
            out.append((r, 0.0))
            continue
        med = _median(prior_vals)
        out.append((r, (r.total_cost_usd / med) if med > 0 else 0.0))
    return out
