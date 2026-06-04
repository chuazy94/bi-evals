"""Tests for the adapter registry — dispatch and protocol conformance."""

from __future__ import annotations

import pytest

from bi_evals.config import (
    AgentConfig,
    ApiEndpointConfig,
    BiEvalsConfig,
    DatabaseConfig,
    ProjectConfig,
)
from bi_evals.provider.contract import Adapter, AgentResult
from bi_evals.provider.registry import (
    AnthropicToolLoopAdapter,
    ApiEndpointAdapter,
    build_adapter,
)


def _config(agent_type: str) -> BiEvalsConfig:
    """Minimal config with the given adapter."""
    agent_kwargs: dict = {"adapter": agent_type}
    if agent_type == "anthropic_tool_loop":
        agent_kwargs["anthropic_tool_loop"] = {"model": "claude-3-5-sonnet-20241022"}
    elif agent_type == "api_endpoint":
        agent_kwargs["api_endpoint"] = ApiEndpointConfig(url="http://example.test")
    return BiEvalsConfig(
        project=ProjectConfig(name="t"),
        agent=AgentConfig(**agent_kwargs),
        database=DatabaseConfig(type="snowflake"),
    )


class TestBuildAdapter:
    def test_anthropic_tool_loop(self) -> None:
        adapter = build_adapter(_config("anthropic_tool_loop"))
        assert isinstance(adapter, AnthropicToolLoopAdapter)

    def test_api_endpoint(self) -> None:
        adapter = build_adapter(_config("api_endpoint"))
        assert isinstance(adapter, ApiEndpointAdapter)

    def test_unknown_type_raises(self) -> None:
        config = _config("api_endpoint")
        config.agent.adapter = "does_not_exist"
        with pytest.raises(ValueError, match="Unknown agent type"):
            build_adapter(config)


class TestProtocolConformance:
    @pytest.mark.parametrize(
        "adapter", [AnthropicToolLoopAdapter(), ApiEndpointAdapter()]
    )
    def test_satisfies_adapter_protocol(self, adapter: object) -> None:
        assert isinstance(adapter, Adapter)


class TestAdapterErrorPaths:
    """Adapters return error strings (not raise) for missing config, matching
    the provider entry point's error convention."""

    def test_api_endpoint_missing_url(self) -> None:
        config = _config("api_endpoint")
        config.agent.api_endpoint = ApiEndpointConfig(url="")
        result = ApiEndpointAdapter().produce("q", {}, config, None)
        assert isinstance(result, str)
        assert "url is not configured" in result

    def test_anthropic_missing_system_prompt(self) -> None:
        config = _config("anthropic_tool_loop")
        config.agent.anthropic_tool_loop.system_prompt = "nonexistent-prompt.md"
        result = AnthropicToolLoopAdapter().produce("q", {}, config, None)
        assert isinstance(result, str)
        assert "System prompt not found" in result

    def test_anthropic_missing_api_key(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The system-prompt check runs first, so give it a real file; the next
        # guard is the unset api_key_env we want to exercise.
        prompt_file = tmp_path / "system.md"
        prompt_file.write_text("You are a SQL assistant.")
        config = _config("anthropic_tool_loop")
        config._base_dir = tmp_path
        config.agent.anthropic_tool_loop.system_prompt = "system.md"
        config.agent.anthropic_tool_loop.api_key_env = "DEFINITELY_NOT_SET_12345"
        monkeypatch.delenv("DEFINITELY_NOT_SET_12345", raising=False)

        result = AnthropicToolLoopAdapter().produce("q", {}, config, None)
        assert isinstance(result, str)
        assert "is not set" in result
