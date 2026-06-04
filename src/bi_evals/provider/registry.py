"""Adapter registry: maps ``agent.type`` to the adapter that produces the contract.

One contract, many adapters. Each ``agent.type`` resolves to a single adapter
whose ``produce`` returns the canonical :class:`AgentResult` (or an error string).
The provider entry point dispatches through :func:`build_adapter`; it never
branches on agent type itself, and the scorer stays entirely agent-agnostic.

This mirrors ``db/factory.py`` and ``tools/registry.py``: add a new adapter by
adding one branch here, with no change to the entry point or the scorer.
"""

from __future__ import annotations

import os
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.provider.api_endpoint import call_api_endpoint
from bi_evals.provider.contract import Adapter, AgentResult
from bi_evals.tools.registry import build_tools


class AnthropicToolLoopAdapter:
    """DEV-ONLY adapter — drives a local Claude tool-calling loop.

    Not a public product feature: this rebuilds the agent locally from skill
    files, which is useful for authoring goldens before a real agent exists but
    is *not* a faithful evaluation of a production agent. See
    ``agent_loop.py`` for the rationale.
    """

    def produce(
        self,
        question: str,
        prompt_vars: dict[str, Any],  # protocol-required; unused by this adapter
        config: BiEvalsConfig,
        model: str | None,
    ) -> AgentResult | str:
        # Imported lazily so the (heavy, anthropic-dependent) driving loop is
        # only pulled in when this dev adapter is actually used.
        from bi_evals.provider.agent_loop import run_agent_loop

        system_prompt_path = config.resolve_path(config.agent.system_prompt)
        if not system_prompt_path.exists():
            return f"System prompt not found: {config.agent.system_prompt}"

        system_prompt = system_prompt_path.read_text()

        tools = build_tools(config.agent.tools, config)
        tool_definitions = [t.definition() for t in tools]

        api_key = os.environ.get(config.agent.api_key_env, "")
        if not api_key:
            return f"Environment variable {config.agent.api_key_env} is not set."

        effective_model = model or config.agent.model
        if not effective_model:
            return "No model configured. Set agent.model or agent.models."

        return run_agent_loop(
            question=question,
            system_prompt=system_prompt,
            model=effective_model,
            tools=tools,
            tool_definitions=tool_definitions,
            max_rounds=config.agent.max_rounds,
            api_key=api_key,
        )


class ApiEndpointAdapter:
    """Calls an existing BI agent over HTTP and scores what it returns."""

    def produce(
        self,
        question: str,
        prompt_vars: dict[str, Any],  # protocol-required; unused by this adapter
        config: BiEvalsConfig,
        model: str | None,  # Step 2 will carry this to the customer's agent
    ) -> AgentResult | str:
        endpoint = config.agent.endpoint
        if not endpoint.url:
            return "agent.endpoint.url is not configured."

        return call_api_endpoint(question=question, endpoint_config=endpoint)


def build_adapter(config: BiEvalsConfig) -> Adapter:
    """Resolve the adapter for the configured ``agent.type``."""
    agent_type = config.agent.type
    if agent_type == "anthropic_tool_loop":
        return AnthropicToolLoopAdapter()
    if agent_type == "api_endpoint":
        return ApiEndpointAdapter()
    raise ValueError(
        f"Unknown agent type: '{agent_type}'. "
        "Use 'anthropic_tool_loop' or 'api_endpoint'."
    )
