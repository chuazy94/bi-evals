"""Claude multi-turn tool-calling loop with trace capture."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from bi_evals.provider.cost import calculate_cost
from bi_evals.tools.base import Tool


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
    """Result of running the agent loop."""

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
    match = re.search(
        r"(SELECT\b.+?)(?:;|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    return None


def run_agent_loop(
    question: str,
    system_prompt: str,
    model: str,
    tools: list[Tool],
    tool_definitions: list[dict[str, Any]],
    max_rounds: int,
    api_key: str,
) -> AgentResult:
    """Run a multi-turn Claude tool-calling loop.

    Each iteration:
    1. Send messages to Claude with tool definitions
    2. If response has tool_use blocks, execute each tool, append results
    3. If response has no tool_use (end_turn), extract SQL and return
    4. Track trace, token usage, cost at each round
    """
    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    trace: list[TraceStep] = []
    files_read: list[str] = []
    total_prompt = 0
    total_completion = 0
    tool_map = {t.name: t for t in tools}

    start_time = time.monotonic()

    for round_num in range(1, max_rounds + 1):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            tools=tool_definitions,
            messages=messages,
        )

        total_prompt += response.usage.input_tokens
        total_completion += response.usage.output_tokens

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        # Record text blocks in trace
        for block in text_blocks:
            trace.append(
                TraceStep(
                    round=round_num,
                    type="text",
                    text=block.text,
                    timestamp_ms=int((time.monotonic() - start_time) * 1000),
                )
            )

        # If no tool calls, we're done
        if not tool_use_blocks:
            final_text = "\n".join(b.text for b in text_blocks)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return AgentResult(
                final_text=final_text,
                extracted_sql=extract_sql(final_text),
                trace=trace,
                files_read=files_read,
                rounds=round_num,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                total_tokens=total_prompt + total_completion,
                cost=calculate_cost(model, total_prompt, total_completion),
                latency_ms=elapsed_ms,
            )

        # Execute tools and build response
        assistant_content = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        for block in tool_use_blocks:
            tool = tool_map.get(block.name)
            if tool is None:
                result_text = f"Error: unknown tool '{block.name}'"
            else:
                result_text = tool.execute(block.input)

            # Track file reads
            if block.name in tool_map and hasattr(tool, '_name'):
                path_value = block.input.get("path", "")
                if path_value:
                    files_read.append(path_value)

            # Truncate result for trace
            preview = result_text[:500] + "..." if len(result_text) > 500 else result_text

            trace.append(
                TraceStep(
                    round=round_num,
                    type="tool_use",
                    tool_name=block.name,
                    tool_input=block.input,
                    tool_result_preview=preview,
                    timestamp_ms=int((time.monotonic() - start_time) * 1000),
                )
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Max rounds reached — return what we have
    all_text = []
    for step in trace:
        if step.type == "text" and step.text:
            all_text.append(step.text)
    final_text = "\n".join(all_text) if all_text else ""
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    return AgentResult(
        final_text=final_text,
        extracted_sql=extract_sql(final_text),
        trace=trace,
        files_read=files_read,
        rounds=max_rounds,
        prompt_tokens=total_prompt,
        completion_tokens=total_completion,
        total_tokens=total_prompt + total_completion,
        cost=calculate_cost(model, total_prompt, total_completion),
        latency_ms=elapsed_ms,
    )
