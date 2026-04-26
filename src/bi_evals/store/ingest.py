"""Parse Promptfoo eval JSON + traces + golden YAML into DuckDB rows.

Phase 6a changes: a Promptfoo "result" entry is one *trial*, not one test. We
group trials by (test_id, model) and:
- write one ``trial_results`` row per trial,
- aggregate each group into one ``test_results`` row (pass_rate, stddev, etc.),
- write ``dimension_results`` per trial (PK includes trial_ix).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from bi_evals.config import BiEvalsConfig
from bi_evals.golden.loader import load_golden_test


MAX_TRACE_BYTES = 1_000_000  # 1 MB guardrail per trial
MAX_HASH_BYTES = 1_000_000  # warn (still hash) anything larger
MAX_HASHED_FILES = 50  # cap hashing to keep ingest fast
LOG = logging.getLogger(__name__)


def ingest_run(
    conn: duckdb.DuckDBPyConnection,
    eval_json_path: Path | str,
    config: BiEvalsConfig,
) -> str:
    """Ingest a Promptfoo eval_*.json (plus sibling traces) into DuckDB.

    Idempotent: re-ingesting the same run_id overwrites prior rows in a single
    transaction.

    Returns the run_id (the Promptfoo evalId).
    """
    eval_json_path = Path(eval_json_path).resolve()
    raw = json.loads(eval_json_path.read_text())

    run_id = raw["evalId"]
    results_obj = raw["results"]
    per_trial = results_obj["results"]

    run_row = _build_run_row(raw, eval_json_path, config, per_trial)
    trial_rows, test_rows, dim_rows = _build_rows(per_trial, run_id, config)
    # Append prompt_snapshot to the run row (kept separate so _build_run_row
    # can stay focused on per-run metric extraction).
    run_row.append(_build_prompt_snapshot(per_trial, config))

    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM dimension_results WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM trial_results WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM test_results WHERE run_id = ?", [run_id])
        conn.execute("DELETE FROM runs WHERE run_id = ?", [run_id])

        conn.execute(
            """
            INSERT INTO runs (
                run_id, project_name, timestamp, config_snapshot, promptfoo_config,
                eval_json_path, test_count, pass_count, fail_count, error_count,
                total_cost_usd, total_latency_ms, total_prompt_tokens, total_completion_tokens,
                prompt_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_row,
        )

        for tr in trial_rows:
            conn.execute(
                """
                INSERT INTO trial_results (
                    run_id, test_id, model, trial_ix, passed, score, generated_sql,
                    fail_reason, cost_usd, latency_ms, prompt_tokens, completion_tokens,
                    total_tokens, trace_file_path, trace_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tr,
            )

        for tr in test_rows:
            conn.execute(
                """
                INSERT INTO test_results (
                    run_id, test_id, model, golden_id, category, difficulty, tags,
                    question, description, reference_sql, generated_sql, files_read,
                    trace_file_path, trace_json, passed, score, fail_reason,
                    cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                    provider, trial_count, pass_count, pass_rate, score_mean, score_stddev,
                    last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tr,
            )

        for dr in dim_rows:
            conn.execute(
                """
                INSERT INTO dimension_results (
                    run_id, test_id, model, trial_ix, dimension, passed, score,
                    reason, is_critical, weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    per_trial: list[dict[str, Any]],
) -> list[Any]:
    results_obj = raw["results"]
    run_id = raw["evalId"]
    timestamp = results_obj.get("timestamp")
    stats = results_obj.get("stats", {}) or {}
    prompt_metrics = {}
    prompts = results_obj.get("prompts") or []
    if prompts:
        prompt_metrics = prompts[0].get("metrics", {}) or {}

    pass_count = stats.get("successes", sum(1 for t in per_trial if t.get("success")))
    fail_count = stats.get("failures", sum(1 for t in per_trial if not t.get("success")))
    error_count = stats.get("errors", 0)

    total_cost = prompt_metrics.get("cost")
    if total_cost is None:
        total_cost = sum((t.get("cost") or 0.0) for t in per_trial)

    total_latency = prompt_metrics.get("totalLatencyMs")
    if total_latency is None:
        total_latency = sum((t.get("latencyMs") or 0) for t in per_trial)

    token_usage = prompt_metrics.get("tokenUsage") or stats.get("tokenUsage") or {}
    prompt_tokens = token_usage.get("prompt", 0)
    completion_tokens = token_usage.get("completion", 0)

    promptfoo_cfg = raw.get("config")
    config_snapshot = config.model_dump(mode="json", exclude={"_base_dir"})

    return [
        run_id,
        config.project.name,
        timestamp,
        json.dumps(config_snapshot),
        json.dumps(promptfoo_cfg) if promptfoo_cfg is not None else None,
        str(eval_json_path),
        len(per_trial),
        pass_count,
        fail_count,
        error_count,
        float(total_cost) if total_cost is not None else None,
        int(total_latency) if total_latency is not None else None,
        int(prompt_tokens),
        int(completion_tokens),
    ]


def _build_rows(
    per_trial: list[dict[str, Any]],
    run_id: str,
    config: BiEvalsConfig,
) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    """Walk every trial, bucket by (test_id, model), emit trial / test / dim rows."""
    critical = set(config.scoring.critical_dimensions)
    weights = config.scoring.dimension_weights

    # Group trials by (test_id, model). Preserve arrival order via list append.
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in per_trial:
        test_case = t.get("testCase") or {}
        test_vars = test_case.get("vars") or t.get("vars") or {}
        test_id = test_vars.get("golden_file") or test_case.get("description") or ""
        metadata = t.get("metadata") or {}
        model = metadata.get("model") or _model_from_provider(t.get("provider")) or ""
        buckets[(test_id, model)].append(t)

    trial_rows: list[list[Any]] = []
    test_rows: list[list[Any]] = []
    dim_rows: list[list[Any]] = []

    for (test_id, model), trials in buckets.items():
        # Snapshot golden metadata once per (test, model) — golden doesn't vary by trial.
        golden_snapshot = _load_golden_snapshot(test_id, config)

        scores: list[float] = []
        pass_count = 0
        total_cost = 0.0
        total_latency = 0
        last_trial = None

        for trial_ix, t in enumerate(trials):
            response = t.get("response") or {}
            token_usage = response.get("tokenUsage") or {}
            metadata = t.get("metadata") or {}

            trace_file_path = metadata.get("trace_file")
            trace_json_str = _load_trace(trace_file_path)

            grading = t.get("gradingResult") or {}
            fail_reason = None
            if not t.get("success"):
                fail_reason = t.get("error") or grading.get("reason")

            passed = bool(t.get("success"))
            score = float(t.get("score") or 0.0)
            scores.append(score)
            if passed:
                pass_count += 1
            trial_cost = float(t.get("cost") or 0.0) if t.get("cost") is not None else None
            if trial_cost is not None:
                total_cost += trial_cost
            trial_latency = int(t.get("latencyMs") or 0) if t.get("latencyMs") is not None else None
            if trial_latency is not None:
                total_latency += trial_latency

            trial_rows.append([
                run_id,
                test_id,
                model,
                trial_ix,
                passed,
                score,
                metadata.get("sql"),
                fail_reason,
                trial_cost,
                trial_latency,
                int(token_usage.get("prompt") or 0),
                int(token_usage.get("completion") or 0),
                int(token_usage.get("total") or 0),
                trace_file_path,
                trace_json_str,
            ])

            # Per-trial dimension rows (unwrap Promptfoo's nested componentResults).
            outer = (grading.get("componentResults") or [{}])[0]
            dims = outer.get("componentResults") or []
            for d in dims:
                dim_name = _dimension_name(d)
                if dim_name is None:
                    continue
                dim_rows.append([
                    run_id,
                    test_id,
                    model,
                    trial_ix,
                    dim_name,
                    bool(d.get("pass")),
                    float(d.get("score") or 0.0),
                    d.get("reason"),
                    dim_name in critical,
                    float(weights.get(dim_name, 1.0)),
                ])

            last_trial = t

        # Aggregate into a single test_results row per (run, test, model).
        trial_count = len(trials)
        pass_rate = pass_count / trial_count if trial_count else 0.0
        score_mean = sum(scores) / len(scores) if scores else 0.0
        score_stddev = _stddev(scores, score_mean)

        # Representative single-trial fields come from the last trial for backward
        # compatibility with code that still reads `passed`, `generated_sql`, etc.
        t_last = last_trial or {}
        response_last = t_last.get("response") or {}
        token_usage_last = response_last.get("tokenUsage") or {}
        metadata_last = t_last.get("metadata") or {}
        grading_last = t_last.get("gradingResult") or {}
        rep_fail_reason = None
        if not t_last.get("success"):
            rep_fail_reason = t_last.get("error") or grading_last.get("reason")
        provider_field = t_last.get("provider")
        if isinstance(provider_field, dict):
            provider_str = provider_field.get("label") or provider_field.get("id") or ""
        else:
            provider_str = provider_field or ""

        # A test "passes" overall if strictly more trials pass than fail. For a
        # single-trial run this collapses to `passed == True`. Ties count as fail.
        overall_passed = pass_count > (trial_count - pass_count)

        test_rows.append([
            run_id,
            test_id,
            model,
            golden_snapshot["golden_id"],
            golden_snapshot["category"],
            golden_snapshot["difficulty"],
            json.dumps(golden_snapshot["tags"]),
            (t_last.get("testCase") or {}).get("vars", {}).get("question")
                or (t_last.get("vars") or {}).get("question"),
            (t_last.get("testCase") or {}).get("description"),
            golden_snapshot["reference_sql"],
            metadata_last.get("sql"),
            json.dumps(metadata_last.get("files_read") or []),
            metadata_last.get("trace_file"),
            _load_trace(metadata_last.get("trace_file")),
            overall_passed,
            score_mean,
            rep_fail_reason,
            float(total_cost) if total_cost else None,
            int(total_latency) if total_latency else None,
            int(token_usage_last.get("prompt") or 0),
            int(token_usage_last.get("completion") or 0),
            int(token_usage_last.get("total") or 0),
            provider_str,
            trial_count,
            pass_count,
            pass_rate,
            score_mean,
            score_stddev,
            golden_snapshot["last_verified_at"],
        ])

    return trial_rows, test_rows, dim_rows


def _stddev(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def _model_from_provider(provider_field: Any) -> str | None:
    """Fallback: extract model from provider.label when metadata.model is empty."""
    if isinstance(provider_field, dict):
        label = provider_field.get("label") or ""
        if label.startswith("bi-evals:"):
            return label.split(":", 1)[1]
    return None


def _dimension_name(dim: dict[str, Any]) -> str | None:
    """Pull the dimension name out of a nested componentResult's namedScores."""
    named = dim.get("namedScores") or {}
    if len(named) == 1:
        return next(iter(named))
    if not named:
        return None
    return sorted(named.keys())[0]


def _load_golden_snapshot(test_id: str, config: BiEvalsConfig) -> dict[str, Any]:
    """Load golden YAML and snapshot its metadata; tolerate missing/malformed files."""
    empty = {
        "golden_id": None,
        "category": None,
        "difficulty": None,
        "tags": [],
        "reference_sql": None,
        "last_verified_at": None,
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
            "last_verified_at": golden.last_verified_at,
        }
    except (yaml.YAMLError, ValueError, OSError):
        return empty


def _build_prompt_snapshot(
    per_trial: list[dict[str, Any]],
    config: BiEvalsConfig,
) -> str | None:
    """Hash every file the agent read across all trials.

    Returns a JSON string mapping ``relative_path`` → ``{sha256, size, mtime}``
    or ``None`` if the run touched no files. Paths are normalized to be
    relative to the config's base_dir so moving the project on disk doesn't
    look like total drift across runs.

    A file deleted between run-time and ingest-time gets ``sha256: None`` so
    the diff still flags it (a deletion is itself a form of drift).
    """
    base = config._base_dir.resolve()
    # The ``file_reader`` tool resolves paths against its own ``base_dir``, so
    # ``files_read`` typically holds paths relative to that — not the project
    # root. Try each tool's base_dir first when locating a file, then fall back
    # to the project root.
    tool_bases: list[Path] = []
    for t in config.agent.tools:
        if t.type == "file_reader":
            tool_base = t.config.get("base_dir") if isinstance(t.config, dict) else None
            if tool_base:
                tool_bases.append(config.resolve_path(tool_base).resolve())

    seen: set[str] = set()
    for t in per_trial:
        metadata = t.get("metadata") or {}
        for f in metadata.get("files_read") or []:
            if f:
                seen.add(str(f))

    if not seen:
        return None

    if len(seen) > MAX_HASHED_FILES:
        LOG.warning(
            "prompt_snapshot: hashing %d files (cap %d) — truncating",
            len(seen), MAX_HASHED_FILES,
        )
        seen = set(sorted(seen)[:MAX_HASHED_FILES])

    snapshot: dict[str, dict[str, Any]] = {}
    for raw_path in sorted(seen):
        path = Path(raw_path)
        if path.is_absolute():
            resolved = path
        else:
            # Try each tool base_dir, then project root. First hit wins.
            resolved = None
            for tool_base in tool_bases:
                candidate = (tool_base / raw_path).resolve()
                if candidate.exists():
                    resolved = candidate
                    break
            if resolved is None:
                resolved = config.resolve_path(raw_path).resolve()
        path = resolved

        try:
            rel = path.resolve().relative_to(base)
            rel_str = str(rel)
        except ValueError:
            # File lives outside the project root — keep the absolute path so
            # we still detect drift, but it won't match across moved checkouts.
            rel_str = str(path.resolve())

        if not path.exists():
            snapshot[rel_str] = {"sha256": None, "size": None, "mtime": None}
            continue

        try:
            data = path.read_bytes()
        except OSError:
            snapshot[rel_str] = {"sha256": None, "size": None, "mtime": None}
            continue

        if len(data) > MAX_HASH_BYTES:
            LOG.warning(
                "prompt_snapshot: %s is %d bytes (>1MB); hashing anyway",
                rel_str, len(data),
            )

        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = None

        snapshot[rel_str] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mtime": mtime,
        }

    return json.dumps(snapshot)


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

    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return None
    return raw
