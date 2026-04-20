"""Parse Promptfoo eval JSON + traces + golden YAML into DuckDB rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import yaml

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.loader import load_golden_test


MAX_TRACE_BYTES = 1_000_000  # 1 MB guardrail per test


def ingest_run(
    conn: duckdb.DuckDBPyConnection,
    eval_json_path: Path | str,
    config: BiEvalsConfig,
) -> str:
    """Ingest a Promptfoo eval_*.json (plus its sibling traces) into DuckDB.

    Idempotent: re-ingesting the same run_id overwrites prior rows in a single
    transaction.

    Returns the run_id (the Promptfoo evalId).
    """
    eval_json_path = Path(eval_json_path).resolve()
    raw = json.loads(eval_json_path.read_text())

    run_id = raw["evalId"]
    results_obj = raw["results"]
    per_test = results_obj["results"]

    run_row = _build_run_row(raw, eval_json_path, config, per_test)
    test_rows, dim_rows = _build_test_and_dim_rows(per_test, run_id, config)

    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM dimension_results WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM test_results WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM runs WHERE run_id = ?", [run_id])

        conn.execute(
            """
            INSERT INTO runs (
                run_id, project_name, timestamp, config_snapshot, promptfoo_config,
                eval_json_path, test_count, pass_count, fail_count, error_count,
                total_cost_usd, total_latency_ms, total_prompt_tokens, total_completion_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_row,
        )

        for tr in test_rows:
            conn.execute(
                """
                INSERT INTO test_results (
                    run_id, test_id, golden_id, category, difficulty, tags,
                    question, description, reference_sql, generated_sql, files_read,
                    trace_file_path, trace_json, passed, score, fail_reason,
                    cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                    provider, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tr,
            )

        for dr in dim_rows:
            conn.execute(
                """
                INSERT INTO dimension_results (
                    run_id, test_id, dimension, passed, score, reason, is_critical, weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                dr,
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return run_id


def _build_run_row(
    raw: dict[str, Any],
    eval_json_path: Path,
    config: BiEvalsConfig,
    per_test: list[dict[str, Any]],
) -> list[Any]:
    results_obj = raw["results"]
    run_id = raw["evalId"]
    timestamp = results_obj.get("timestamp")
    stats = results_obj.get("stats", {}) or {}
    prompt_metrics = {}
    prompts = results_obj.get("prompts") or []
    if prompts:
        prompt_metrics = prompts[0].get("metrics", {}) or {}

    pass_count = stats.get("successes", sum(1 for t in per_test if t.get("success")))
    fail_count = stats.get("failures", sum(1 for t in per_test if not t.get("success")))
    error_count = stats.get("errors", 0)

    total_cost = prompt_metrics.get("cost")
    if total_cost is None:
        total_cost = sum((t.get("cost") or 0.0) for t in per_test)

    total_latency = prompt_metrics.get("totalLatencyMs")
    if total_latency is None:
        total_latency = sum((t.get("latencyMs") or 0) for t in per_test)

    token_usage = prompt_metrics.get("tokenUsage") or stats.get("tokenUsage") or {}
    prompt_tokens = token_usage.get("prompt", 0)
    completion_tokens = token_usage.get("completion", 0)

    # Try to load the promptfoo config that generated this run (best-effort).
    promptfoo_cfg = raw.get("config")

    # Snapshot the bi-evals config that was in effect (best-effort: dump model).
    config_snapshot = config.model_dump(mode="json", exclude={"_base_dir"})

    return [
        run_id,
        config.project.name,
        timestamp,
        json.dumps(config_snapshot),
        json.dumps(promptfoo_cfg) if promptfoo_cfg is not None else None,
        str(eval_json_path),
        len(per_test),
        pass_count,
        fail_count,
        error_count,
        float(total_cost) if total_cost is not None else None,
        int(total_latency) if total_latency is not None else None,
        int(prompt_tokens),
        int(completion_tokens),
    ]


def _build_test_and_dim_rows(
    per_test: list[dict[str, Any]],
    run_id: str,
    config: BiEvalsConfig,
) -> tuple[list[list[Any]], list[list[Any]]]:
    test_rows: list[list[Any]] = []
    dim_rows: list[list[Any]] = []

    critical = set(config.scoring.critical_dimensions)
    weights = config.scoring.dimension_weights

    for t in per_test:
        test_case = t.get("testCase") or {}
        test_vars = test_case.get("vars") or t.get("vars") or {}
        test_id = test_vars.get("golden_file") or test_case.get("description") or ""

        metadata = t.get("metadata") or {}
        response = t.get("response") or {}
        token_usage = response.get("tokenUsage") or {}

        golden_snapshot = _load_golden_snapshot(test_id, config)

        trace_file_path = metadata.get("trace_file")
        trace_json_str = _load_trace(trace_file_path)

        provider_field = t.get("provider")
        if isinstance(provider_field, dict):
            provider_str = provider_field.get("id") or provider_field.get("label") or ""
        else:
            provider_str = provider_field or ""

        model = metadata.get("model") or ""

        grading = t.get("gradingResult") or {}
        outer = (grading.get("componentResults") or [{}])[0]
        dims = outer.get("componentResults") or []

        fail_reason = None
        if not t.get("success"):
            fail_reason = t.get("error") or grading.get("reason")

        test_rows.append([
            run_id,
            test_id,
            golden_snapshot["golden_id"],
            golden_snapshot["category"],
            golden_snapshot["difficulty"],
            json.dumps(golden_snapshot["tags"]),
            test_vars.get("question"),
            test_case.get("description"),
            golden_snapshot["reference_sql"],
            metadata.get("sql"),
            json.dumps(metadata.get("files_read") or []),
            trace_file_path,
            trace_json_str,
            bool(t.get("success")),
            float(t.get("score") or 0.0),
            fail_reason,
            float(t.get("cost") or 0.0) if t.get("cost") is not None else None,
            int(t.get("latencyMs") or 0) if t.get("latencyMs") is not None else None,
            int(token_usage.get("prompt") or 0),
            int(token_usage.get("completion") or 0),
            int(token_usage.get("total") or 0),
            provider_str,
            model,
        ])

        for d in dims:
            dim_name = _dimension_name(d)
            if dim_name is None:
                continue
            dim_rows.append([
                run_id,
                test_id,
                dim_name,
                bool(d.get("pass")),
                float(d.get("score") or 0.0),
                d.get("reason"),
                dim_name in critical,
                float(weights.get(dim_name, 1.0)),
            ])

    return test_rows, dim_rows


def _dimension_name(dim: dict[str, Any]) -> str | None:
    """Pull the dimension name out of a nested componentResult's namedScores."""
    named = dim.get("namedScores") or {}
    if len(named) == 1:
        return next(iter(named))
    if not named:
        return None
    # Multiple keys shouldn't happen; take the first deterministically.
    return sorted(named.keys())[0]


def _load_golden_snapshot(test_id: str, config: BiEvalsConfig) -> dict[str, Any]:
    """Load golden YAML and snapshot its metadata; tolerate missing/malformed files."""
    empty = {
        "golden_id": None,
        "category": None,
        "difficulty": None,
        "tags": [],
        "reference_sql": None,
    }
    if not test_id:
        return empty
    try:
        golden_path = config.resolve_path(test_id)
        if not golden_path.exists():
            return empty
        golden = load_golden_test(golden_path)
        return {
            "golden_id": golden.id,
            "category": golden.category,
            "difficulty": golden.difficulty,
            "tags": list(golden.tags),
            "reference_sql": golden.reference_sql,
        }
    except (yaml.YAMLError, ValueError, OSError):
        return empty


def _load_trace(trace_file_path: str | None) -> str | None:
    """Load and validate trace JSON, returning a string for DuckDB JSON column."""
    if not trace_file_path:
        return None
    path = Path(trace_file_path)
    if not path.exists():
        return None
    try:
        raw = path.read_text()
    except OSError:
        return None

    if len(raw) > MAX_TRACE_BYTES:
        # Store a truncation marker rather than the full oversized blob.
        try:
            data = json.loads(raw)
            steps = data.get("trace") or []
            keep = 20
            if len(steps) > keep * 2:
                truncated = {
                    **data,
                    "trace": steps[:keep] + [{"_truncated": len(steps) - keep * 2}] + steps[-keep:],
                }
                return json.dumps(truncated)
        except json.JSONDecodeError:
            return None

    # Validate it's parseable JSON before handing to DuckDB.
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return None
    return raw
