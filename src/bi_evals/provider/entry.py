"""Promptfoo Python provider entry point.

Promptfoo calls `call_api(prompt, options, context)` for each test case.
This module loads the bi-evals config, resolves the adapter for the configured
``agent.type`` via the adapter registry, captures the canonical trace it
produces, and returns results in Promptfoo's expected format.

It does not branch on agent type itself — that lives in ``registry.py``. One
contract, many adapters.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.provider.registry import build_adapter
from bi_evals.trace_paths import make_test_id_slug, slugify_model


def call_api(
    prompt: str, options: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Promptfoo Python provider entry point.

    Resolves the adapter for the configured ``agent.type`` and runs it. Every
    adapter returns the same canonical :class:`AgentResult`, which is written to
    a trace file and surfaced in Promptfoo's expected result shape.

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

    # Push overrides: `bi-evals score` runs the push adapter without editing
    # bi-evals.yaml, so it threads the adapter + input file through the provider
    # block's config (the on-disk config the provider just loaded doesn't have
    # them). Apply them here, after load.
    if provider_config.get("adapter") == "push":
        config.agent.adapter = "push"
        config.agent.push.input_file = provider_config.get("push_input_file", "")

    agent_type = config.agent.type
    # Multi-model: each provider block carries its own `model` override so the
    # cartesian product of (test × model × repeat) runs correctly under Promptfoo.
    model_override = provider_config.get("model")

    try:
        adapter = build_adapter(config)
    except ValueError as e:
        return {"error": str(e)}

    vars_ = context.get("vars", {})
    result = adapter.produce(prompt, vars_, config, model_override)

    # Handle error strings
    if isinstance(result, str):
        return {"error": result}

    # Write trace to file for the scorer to read
    trace_dir = config.resolve_path(config.reporting.results_dir) / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    test_id_slug = make_test_id_slug(prompt, vars_)
    test_id = vars_.get("golden_file") or test_id_slug
    effective_model = model_override or config.agent.model
    model_slug = slugify_model(effective_model)
    # Unique suffix so N repeats against the same (test, model) don't overwrite.
    suffix = secrets.token_hex(4)

    trace_data = {
        "test_id": test_id,
        "agent_type": agent_type,
        "model": effective_model,
        "rounds": result.rounds,
        "trace": result.trace_as_dicts(),
        "files_read": result.files_read,
        "generated_sql": result.extracted_sql,
        "agent_error": result.agent_error,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cost": result.cost,
        "latency_ms": result.latency_ms,
    }

    trace_file = trace_dir / f"{test_id_slug}__{model_slug}__{suffix}.json"
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
            "model": effective_model,
            "rounds": result.rounds,
            "latency_ms": result.latency_ms,
        },
    }
