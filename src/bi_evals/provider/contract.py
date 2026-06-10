"""The canonical agent contract shared by every adapter.

bi-evals evaluates the *response* of an agent — it never assumes how the answer
was produced. Every adapter (the dev-only Claude tool loop, the HTTP endpoint,
and future push / mcp-server / otel adapters) normalises into the one shape
defined here, and the scorer only ever sees this shape.

The contract is, at minimum, ``{ generated_sql, trace }`` per question (plus
file-reads and usage metadata). The scorer executes the SQL itself, so adapters
never carry result sets — only the query and what it touched.

This module is the *neutral* home for those types: adapters depend on it, and it
depends on no adapter. Anything agent-specific belongs in an adapter, not here.

Forward note (Step 2): ``AgentResult.trace`` is heading toward an open-envelope
model — adapters over-capture whatever their agent emits and the scorer reads the
keys it understands. Don't fight that here; keep this shape additive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class TraceStep:
    """A single step in the agent's reasoning trace."""

    round: int
    type: str  # "tool_use" or "text"
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result_preview: str | None = None  # truncated output
    text: str | None = None
    timestamp_ms: int = 0


@dataclass
class AgentResult:
    """The canonical contract: one agent's answer to one question.

    Produced by every adapter, consumed by the scorer. ``generated_sql`` and
    ``trace`` are the load-bearing fields; the rest is usage/diagnostic metadata
    that adapters fill in when their source exposes it.
    """

    final_text: str
    extracted_sql: str | None
    trace: list[TraceStep] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    # Set when the agent failed to produce an answer at all (e.g. a push
    # submission with an `error` field). The scorer treats this as a failed
    # `execution` dimension carrying this message, rather than trying to run SQL.
    agent_error: str | None = None

    def trace_as_dicts(self) -> list[dict[str, Any]]:
        """Serialize trace steps for JSON output."""
        return [
            {
                "round": s.round,
                "type": s.type,
                "tool_name": s.tool_name,
                "tool_input": s.tool_input,
                "tool_result_preview": s.tool_result_preview,
                "text": s.text,
                "timestamp_ms": s.timestamp_ms,
            }
            for s in self.trace
        ]


def extract_sql(text: str) -> str | None:
    """Extract SQL from the agent's response.

    Tries in order:
    1. ```sql code fences
    2. ``` generic code fences containing SELECT
    3. Bare SELECT ... ; pattern
    """
    # Strategy 1: ```sql fences
    match = re.search(r"```sql\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Strategy 2: ``` fences containing SELECT
    for match in re.finditer(r"```\s*\n(.*?)```", text, re.DOTALL):
        block = match.group(1).strip()
        if re.search(r"\bSELECT\b", block, re.IGNORECASE):
            return block

    # Strategy 3: bare SELECT statement
    match = re.search(r"(SELECT\b.+?)(?:;|\Z)", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return None


@runtime_checkable
class Adapter(Protocol):
    """How one delivery shape turns a question into the canonical contract.

    Each ``agent.type`` maps to one adapter. ``produce`` returns an
    :class:`AgentResult` on success or an error string (matching the provider
    entry point's existing error convention). ``model`` is the optional model
    override the provider passes through for that run; adapters that don't drive
    a model may ignore it (Step 2 will turn it into a model *request* carried to
    the customer's agent).
    """

    def produce(
        self,
        question: str,
        prompt_vars: dict[str, Any],
        config: Any,
        model: str | None,
    ) -> AgentResult | str: ...
