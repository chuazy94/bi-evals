"""API endpoint provider — sends questions to an existing BI agent API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json

from bi_evals.config import ApiEndpointConfig
from bi_evals.provider.agent_loop import AgentResult, TraceStep, extract_sql


def _get_nested(data: Any, key: str) -> Any:
    """Get a value from nested dict using dot-separated key.

    e.g., _get_nested({"response": {"sql": "SELECT 1"}}, "response.sql") -> "SELECT 1"
    """
    parts = key.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def call_api_endpoint(
    question: str,
    endpoint_config: ApiEndpointConfig,
) -> AgentResult:
    """Send a question to an external API endpoint and capture the response.

    The endpoint should accept a JSON body with a "question" field and return
    JSON with at least a text response (and optionally a SQL field).

    Args:
        question: The user question to send.
        endpoint_config: API endpoint configuration.

    Returns:
        AgentResult with the response text, extracted SQL, and basic trace.
    """
    start_time = time.monotonic()

    request_body = json.dumps({"question": question}).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        **endpoint_config.headers,
    }

    req = Request(
        endpoint_config.url,
        data=request_body,
        headers=headers,
        method=endpoint_config.method,
    )

    try:
        with urlopen(req, timeout=endpoint_config.timeout) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return AgentResult(
            final_text=f"HTTP {e.code}: {error_body}",
            extracted_sql=None,
            trace=[
                TraceStep(
                    round=1,
                    type="text",
                    text=f"API error: HTTP {e.code}",
                    timestamp_ms=elapsed_ms,
                )
            ],
            latency_ms=elapsed_ms,
        )
    except (URLError, TimeoutError) as e:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return AgentResult(
            final_text=f"Connection error: {e}",
            extracted_sql=None,
            trace=[
                TraceStep(
                    round=1,
                    type="text",
                    text=f"Connection error: {e}",
                    timestamp_ms=elapsed_ms,
                )
            ],
            latency_ms=elapsed_ms,
        )

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Extract text and SQL from response
    response_text = _get_nested(response_data, endpoint_config.response_text_key)
    response_sql = _get_nested(response_data, endpoint_config.response_sql_key)

    if response_text is None:
        response_text = json.dumps(response_data)

    response_text = str(response_text)

    # If the API returns SQL explicitly, use it; otherwise try to extract from text
    if response_sql:
        sql = str(response_sql)
    else:
        sql = extract_sql(response_text)

    # Build a minimal trace — we don't see the agent's internal reasoning
    trace = [
        TraceStep(
            round=1,
            type="text",
            text=f"API endpoint called: {endpoint_config.url}",
            timestamp_ms=0,
        ),
        TraceStep(
            round=1,
            type="text",
            text=response_text,
            timestamp_ms=elapsed_ms,
        ),
    ]

    # Check if the API returned trace/metadata we can capture
    api_trace = _get_nested(response_data, "trace")
    files_read: list[str] = []
    if isinstance(api_trace, list):
        for i, step in enumerate(api_trace):
            if isinstance(step, dict):
                trace.append(
                    TraceStep(
                        round=i + 1,
                        type=step.get("type", "text"),
                        tool_name=step.get("tool_name"),
                        tool_input=step.get("tool_input"),
                        tool_result_preview=step.get("tool_result_preview"),
                        text=step.get("text"),
                    )
                )
                if step.get("tool_input", {}).get("path"):
                    files_read.append(step["tool_input"]["path"])

    api_files = _get_nested(response_data, "files_read")
    if isinstance(api_files, list):
        files_read = api_files

    return AgentResult(
        final_text=response_text,
        extracted_sql=sql,
        trace=trace,
        files_read=files_read,
        rounds=1,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost=0.0,
        latency_ms=elapsed_ms,
    )
