"""Promptfoo Python provider entry point.

Promptfoo calls `call_api(prompt, options, context)` for each test case.
This module loads the bi-evals config, dispatches to the configured provider
type (anthropic_tool_loop or api_endpoint), captures the trace, and returns
results in Promptfoo's expected format.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.provider.agent_loop import AgentResult, run_agent_loop
from bi_evals.provider.api_endpoint import call_api_endpoint
from bi_evals.tools.registry import build_tools


def _run_anthropic_tool_loop(prompt: str, config: BiEvalsConfig) -> AgentResult | str:
    """Run the Anthropic tool-calling loop. Returns AgentResult or error string."""
    system_prompt_path = config.resolve_path(config.agent.system_prompt)
    if not system_prompt_path.exists():
        return f"System prompt not found: {config.agent.system_prompt}"

    system_prompt = system_prompt_path.read_text()

    tools = build_tools(config.agent.tools, config)
    tool_definitions = [t.definition() for t in tools]

    api_key = os.environ.get(config.agent.api_key_env, "")
    if not api_key:
        return f"Environment variable {config.agent.api_key_env} is not set."

    return run_agent_loop(
        question=prompt,
        system_prompt=system_prompt,
        model=config.agent.model,
        tools=tools,
        tool_definitions=tool_definitions,
        max_rounds=config.agent.max_rounds,
        api_key=api_key,
    )


def _run_api_endpoint(prompt: str, config: BiEvalsConfig) -> AgentResult | str:
    """Call an external API endpoint. Returns AgentResult or error string."""
    endpoint = config.agent.endpoint
    if not endpoint.url:
        return "agent.endpoint.url is not configured."

    return call_api_endpoint(question=prompt, endpoint_config=endpoint)


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Promptfoo Python provider entry point.

    Dispatches to the configured agent type:
    - anthropic_tool_loop: Runs Claude with tool-calling against skill files
    - api_endpoint: Sends question to an external API and captures the response

    Args:
        prompt: The user question (rendered from template).
        options: Provider config from promptfooconfig.yaml.
        context: Test context including vars.

    Returns:
        Dict with output, tokenUsage, cost, and metadata.
    """
    provider_config = options.get("config", {})
    config_path = provider_config.get("config_path", "bi-evals.yaml")
    config = BiEvalsConfig.load(Path(config_path))

    agent_type = config.agent.type

    if agent_type == "anthropic_tool_loop":
        result = _run_anthropic_tool_loop(prompt, config)
    elif agent_type == "api_endpoint":
        result = _run_api_endpoint(prompt, config)
    else:
        return {"error": f"Unknown agent type: '{agent_type}'. Use 'anthropic_tool_loop' or 'api_endpoint'."}

    # Handle error strings
    if isinstance(result, str):
        return {"error": result}

    # Write trace to file for the scorer to read
    trace_dir = config.resolve_path(config.reporting.results_dir) / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    vars_ = context.get("vars", {})
    golden_file = vars_.get("golden_file", "")
    test_id = golden_file if golden_file else hashlib.md5(prompt.encode()).hexdigest()
    test_id_slug = test_id.replace("/", "_").replace(".", "_")

    trace_data = {
        "test_id": test_id,
        "agent_type": agent_type,
        "model": config.agent.model,
        "rounds": result.rounds,
        "trace": result.trace_as_dicts(),
        "files_read": result.files_read,
        "generated_sql": result.extracted_sql,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cost": result.cost,
        "latency_ms": result.latency_ms,
    }

    trace_file = trace_dir / f"{test_id_slug}.json"
    trace_file.write_text(json.dumps(trace_data, indent=2))

    return {
        "output": result.final_text,
        "tokenUsage": {
            "total": result.total_tokens,
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
        },
        "cost": result.cost,
        "metadata": {
            "trace_file": str(trace_file),
            "agent_type": agent_type,
            "files_read": result.files_read,
            "sql": result.extracted_sql,
            "model": config.agent.model,
            "rounds": result.rounds,
            "latency_ms": result.latency_ms,
        },
    }
