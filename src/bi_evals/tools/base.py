"""Tool protocol for BI agent evaluation."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Tool(Protocol):
    """Interface for tools the agent can call during evaluation."""

    @property
    def name(self) -> str:
        """Tool name as seen by the LLM."""
        ...

    def definition(self) -> dict[str, Any]:
        """Anthropic tool schema definition."""
        ...

    def execute(self, input: dict[str, Any]) -> str:
        """Execute the tool and return result as string."""
        ...
