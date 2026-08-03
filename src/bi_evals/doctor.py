"""bi-evals doctor — validates a configured eval project against runtime expectations.

Both modes get a structured set of CheckResults that say what's working,
what's broken, and what's degraded. The CLI command in cli.py is a thin
formatter on top of these pure functions.

For BYO mode: validates the configured endpoint against the JSON Schema
in byo_response_schema.json, runs a synthetic POST, reports scoring
coverage based on which optional fields the endpoint emits.

For Built-in mode: confirms the Anthropic API key is reachable, the
system prompt and file_reader base_dirs exist, Snowflake is reachable
(SELECT 1), and Promptfoo (npx) is on PATH.

Snowflake reachability uses a real SELECT 1 because instantiating a
connector without round-tripping doesn't catch network / warehouse /
permissions issues — which are the actual common failure mode.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bi_evals.config import ApiEndpointConfig, BiEvalsConfig
from bi_evals.db.factory import create_db_client
from bi_evals.scorer.capability import (
    TRACE_DEPENDENT_DIMENSIONS,
    coverage_warning,
    trace_coverage,
)


# ──────────────────────────────────────────────────────────────────────────────
# CheckResult
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """One line in the doctor's report.

    severity:
      - "ok"   — required check passed, or optional check passed
      - "warn" — optional check failed; scoring degraded but eval will run
      - "fail" — required check failed; eval will not work

    The CLI exits 0 only if no `fail` checks exist; `warn` is informational.
    """

    name: str
    severity: str  # "ok" | "warn" | "fail"
    detail: str = ""


def is_failing(results: list[CheckResult]) -> bool:
    return any(r.severity == "fail" for r in results)


# ──────────────────────────────────────────────────────────────────────────────
# Schema loading
# ──────────────────────────────────────────────────────────────────────────────


def _load_byo_schema() -> dict[str, Any]:
    """Load the bundled BYO response JSON Schema."""
    text = (files("bi_evals") / "byo_response_schema.json").read_text()
    return json.loads(text)


# ──────────────────────────────────────────────────────────────────────────────
# Shared checks (both modes)
# ──────────────────────────────────────────────────────────────────────────────


def check_warehouse_select_one(config: BiEvalsConfig) -> CheckResult:
    """Real SELECT 1 against the configured warehouse. Catches auth + network.

    Warehouse-neutral: the label reflects ``database.type`` so the same check
    serves Snowflake, Databricks, and any future client behind the factory.
    """
    label = f"{config.database.type.title()} reachability"
    try:
        client = create_db_client(config.database)
        result = client.execute("SELECT 1")
        error = getattr(result, "error", None)
        if error:
            return CheckResult(label, "fail", f"SELECT 1 returned error: {error}")
        row_count = getattr(result, "row_count", 0)
        if row_count < 1:
            return CheckResult(label, "fail", "SELECT 1 returned no rows")
        return CheckResult(label, "ok", f"SELECT 1 returned {row_count} row(s)")
    except Exception as e:
        return CheckResult(label, "fail", f"{type(e).__name__}: {e}")


# Back-compat alias: existing callers/tests reference this name. The check is
# warehouse-neutral now.
check_snowflake_select_one = check_warehouse_select_one


def check_promptfoo_available() -> CheckResult:
    if shutil.which("npx") is None:
        return CheckResult(
            "Promptfoo (npx) on PATH",
            "fail",
            "npx not found — install Node.js, then `npm install -g promptfoo`",
        )
    return CheckResult("Promptfoo (npx) on PATH", "ok")


# ──────────────────────────────────────────────────────────────────────────────
# Built-in mode checks
# ──────────────────────────────────────────────────────────────────────────────


def check_anthropic_api_key() -> CheckResult:
    """Verify the Anthropic API key works without spending tokens.

    Uses client.models.list() — a cheap metadata call that exercises auth.
    """
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult(
            "Anthropic API key",
            "fail",
            "ANTHROPIC_API_KEY not set in environment",
        )
    try:
        from anthropic import Anthropic

        Anthropic().models.list(limit=1)
        return CheckResult("Anthropic API key", "ok", "auth verified")
    except Exception as e:
        return CheckResult(
            "Anthropic API key",
            "fail",
            f"{type(e).__name__}: {e}",
        )


def check_system_prompt(config: BiEvalsConfig) -> CheckResult:
    if not config.agent.system_prompt:
        return CheckResult(
            "System prompt configured",
            "fail",
            "agent.system_prompt is empty",
        )
    path = config.resolve_path(config.agent.system_prompt)
    if not Path(path).is_file():
        return CheckResult(
            "System prompt file exists",
            "fail",
            f"{config.agent.system_prompt} not found (resolved: {path})",
        )
    return CheckResult(
        "System prompt file exists",
        "ok",
        f"{config.agent.system_prompt}",
    )


def check_tool_base_dirs(config: BiEvalsConfig) -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool in config.agent.tools:
        base_dir = (tool.config or {}).get("base_dir") if tool.config else None
        if not base_dir:
            # Tools without a base_dir (e.g. describe_table) skip silently.
            continue
        resolved = config.resolve_path(base_dir)
        if not Path(resolved).is_dir():
            results.append(
                CheckResult(
                    f"Tool '{tool.name}' base_dir exists",
                    "fail",
                    f"{base_dir} is not a directory (resolved: {resolved})",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"Tool '{tool.name}' base_dir exists",
                    "ok",
                    f"{base_dir}",
                )
            )
    return results


def check_builtin_setup(config: BiEvalsConfig) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_anthropic_api_key())
    results.append(check_system_prompt(config))
    results.extend(check_tool_base_dirs(config))
    results.append(check_snowflake_select_one(config))
    results.append(check_promptfoo_available())
    return results


def check_push_setup(config: BiEvalsConfig) -> list[CheckResult]:
    """Pre-flight checks for the push adapter.

    There is no live agent to ping — the customer ran their agent ahead of time
    and submits a JSONL via `bi-evals score --input`. So doctor validates the
    things push *does* depend on: the warehouse the scorer executes SQL against,
    Promptfoo on PATH, and (when a submission file is configured) that it parses.
    """
    results: list[CheckResult] = [
        CheckResult(
            "push adapter",
            "ok",
            "No live agent to validate — submit results with `bi-evals score --input`.",
        ),
        check_snowflake_select_one(config),
        check_promptfoo_available(),
    ]

    input_file = config.agent.push.input_file
    if not input_file:
        results.append(
            CheckResult(
                "Submission file",
                "warn",
                "agent.push.input_file not set (normally passed via `score --input`).",
            )
        )
        return results

    path = config.resolve_path(input_file)
    if not path.exists():
        results.append(
            CheckResult("Submission file", "fail", f"not found: {input_file}")
        )
        return results
    try:
        rows = 0
        parsed_rows: list[dict[str, Any]] = []
        with path.open() as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if not row.get("golden_file"):
                    raise ValueError(f"line {lineno}: missing 'golden_file'")
                parsed_rows.append(row)
                rows += 1
        results.append(
            CheckResult("Submission file", "ok", f"{input_file} ({rows} row(s))")
        )
        # Build Stage 2: capability coverage — say up front which dimensions
        # won't be evaluable, before any warehouse spend.
        trace_dims = TRACE_DEPENDENT_DIMENSIONS & set(config.scoring.dimensions)
        if trace_dims and parsed_rows:
            usable, total = trace_coverage(parsed_rows)
            for dim in sorted(trace_dims):
                warning = coverage_warning(usable, total, dim)
                if warning:
                    results.append(CheckResult("Trace coverage", "warn", warning))
            if not any(coverage_warning(usable, total, d) for d in sorted(trace_dims)):
                results.append(
                    CheckResult(
                        "Trace coverage",
                        "ok",
                        f"usable trace in {usable}/{total} submission(s).",
                    )
                )
    except (json.JSONDecodeError, ValueError, OSError) as e:
        results.append(CheckResult("Submission file", "fail", f"{input_file}: {e}"))
    return results


# ──────────────────────────────────────────────────────────────────────────────
# BYO mode checks
# ──────────────────────────────────────────────────────────────────────────────


_SYNTHETIC_QUESTION = (
    "Health check: please respond with the SQL query "
    "`SELECT 1 AS health_check` inside a fenced ```sql block. "
    "Do not include any other SQL."
)


def _post_synthetic(
    endpoint: ApiEndpointConfig,
) -> tuple[dict[str, Any] | None, str, int, float]:
    """POST a synthetic question. Returns (parsed_json, raw_body, status, latency_ms).

    parsed_json is None on connection error or non-JSON body.
    """
    body = json.dumps({"question": _SYNTHETIC_QUESTION}).encode("utf-8")
    headers = {"Content-Type": "application/json", **endpoint.headers}
    req = Request(endpoint.url, data=body, headers=headers, method=endpoint.method)

    start = time.monotonic()
    try:
        with urlopen(req, timeout=endpoint.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed = (time.monotonic() - start) * 1000
            try:
                return json.loads(raw), raw, resp.status, elapsed
            except json.JSONDecodeError:
                return None, raw, resp.status, elapsed
    except HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return None, raw, e.code, elapsed
    except (URLError, TimeoutError) as e:
        elapsed = (time.monotonic() - start) * 1000
        return None, f"connection error: {e}", 0, elapsed


def _extract_sql_fence(text: str) -> str:
    """Same fence-extraction the provider uses; defined inline so doctor doesn't
    pull provider code (avoids importing anthropic SDK transitively)."""
    import re

    m = re.search(r"```sql\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _get_nested(data: Any, key: str) -> Any:
    parts = key.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def check_byo_endpoint(config: BiEvalsConfig) -> list[CheckResult]:
    results: list[CheckResult] = []

    if config.agent.adapter != "api_endpoint":
        results.append(
            CheckResult(
                "api_endpoint adapter",
                "fail",
                f"agent.adapter is {config.agent.adapter!r}, expected 'api_endpoint'",
            )
        )
        return results

    endpoint = config.agent.endpoint
    if not endpoint or not endpoint.url:
        results.append(
            CheckResult(
                "Endpoint URL configured",
                "fail",
                "agent.endpoint.url is empty",
            )
        )
        return results

    # 1. Connection + parse
    parsed, raw, status, latency_ms = _post_synthetic(endpoint)

    if status == 0:
        results.append(
            CheckResult(
                "Endpoint reachable",
                "fail",
                f"connection failed in {latency_ms:.0f}ms: {raw[:200]}",
            )
        )
        return results

    if status >= 400:
        results.append(
            CheckResult(
                "Endpoint reachable",
                "fail",
                f"HTTP {status} in {latency_ms:.0f}ms: {raw[:200]}",
            )
        )
        return results

    results.append(
        CheckResult(
            "Endpoint reachable",
            "ok",
            f"HTTP {status} in {latency_ms:.0f}ms",
        )
    )

    if parsed is None:
        results.append(
            CheckResult(
                "Response is valid JSON",
                "fail",
                f"could not parse body: {raw[:200]}",
            )
        )
        return results

    results.append(CheckResult("Response is valid JSON", "ok"))

    # 2. Schema validation against bundled JSON Schema
    try:
        from jsonschema import Draft202012Validator

        schema = _load_byo_schema()
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(parsed))
    except Exception as e:
        results.append(
            CheckResult(
                "Schema validation",
                "fail",
                f"could not validate ({type(e).__name__}): {e}",
            )
        )
        return results

    if errors:
        # Surface the first schema error verbatim — useful for missing-anyOf cases
        results.append(
            CheckResult(
                "Schema validation",
                "fail",
                "; ".join(e.message for e in errors[:3]),
            )
        )
    else:
        results.append(CheckResult("Schema validation", "ok"))

    # 3. Required-for-scoring: SQL is retrievable, either directly or via fence
    sql_key = endpoint.response_sql_key
    sql = _get_nested(parsed, sql_key)
    if not sql:
        # Try to extract from text
        text_key = endpoint.response_text_key
        text = _get_nested(parsed, text_key)
        if isinstance(text, str) and _extract_sql_fence(text):
            results.append(
                CheckResult(
                    f"SQL retrievable (via {text_key} fence)",
                    "ok",
                    "SQL extracted from fenced block",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"SQL retrievable (via {sql_key} or {text_key} fence)",
                    "fail",
                    "no SQL found in response — every scoring dimension will fail",
                )
            )
    else:
        results.append(
            CheckResult(
                f"SQL retrievable (key: {sql_key})",
                "ok",
            )
        )

    # 4. Optional fields → scoring coverage
    text_key = endpoint.response_text_key
    has_text = isinstance(_get_nested(parsed, text_key), str)
    has_files_read = isinstance(parsed.get("files_read"), list)
    has_trace = isinstance(parsed.get("trace"), list)

    if has_text:
        results.append(
            CheckResult(
                f"Text response (key: {text_key})",
                "ok",
                "viewer will display the agent's natural-language answer",
            )
        )
    else:
        results.append(
            CheckResult(
                f"Text response (key: {text_key})",
                "warn",
                "absent — viewer will display the raw response body",
            )
        )

    if has_files_read:
        results.append(
            CheckResult(
                "files_read array",
                "ok",
                "file-attribution checks in skill_path_correctness enabled",
            )
        )
    elif has_trace:
        results.append(
            CheckResult(
                "files_read derivable from trace",
                "ok",
                "no top-level files_read, but trace[].tool_input.path will be harvested",
            )
        )
    else:
        results.append(
            CheckResult(
                "files_read array",
                "warn",
                "absent — skill_path_correctness file checks will skip",
            )
        )

    if has_trace:
        results.append(
            CheckResult(
                "trace array",
                "ok",
                "sequence-sensitive skill_path_correctness checks enabled",
            )
        )
    else:
        results.append(
            CheckResult(
                "trace array",
                "warn",
                "absent — sequence-sensitive skill_path_correctness checks will skip",
            )
        )

    # 5. Snowflake (still needed — scorer runs both queries)
    results.append(check_snowflake_select_one(config))

    # 6. Promptfoo
    results.append(check_promptfoo_available())

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────


_GLYPHS = {"ok": "[ok]   ", "warn": "[warn] ", "fail": "[fail] "}


def format_report(results: list[CheckResult], *, mode: str) -> str:
    """Render a CheckResult list as terminal-friendly text."""
    lines = [f"Mode: {mode}", ""]
    for r in results:
        glyph = _GLYPHS.get(r.severity, "[?]    ")
        line = f"{glyph}{r.name}"
        if r.detail:
            line += f"  ({r.detail})"
        lines.append(line)

    n_ok = sum(1 for r in results if r.severity == "ok")
    n_warn = sum(1 for r in results if r.severity == "warn")
    n_fail = sum(1 for r in results if r.severity == "fail")
    lines.append("")
    lines.append(f"Summary: {n_ok} ok, {n_warn} warn, {n_fail} fail")
    if n_fail:
        lines.append(
            "Required checks failed — fix the [fail] items above before running."
        )
    elif n_warn:
        lines.append(
            "All required checks passed. Warnings indicate degraded scoring coverage."
        )
    else:
        lines.append("Ready to run: bi-evals run")
    return "\n".join(lines)
