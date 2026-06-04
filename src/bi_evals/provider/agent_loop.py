"""Claude multi-turn tool-calling loop with trace capture.

DEV-ONLY ADAPTER — not a public product feature. bi-evals' product stance is to
evaluate the *response* of the real production agent, never to drive or rebuild
its loop. This driving loop is kept only as an internal convenience for authoring
golden tests and sanity-checking before a real agent exists. Do not position it
as a way to "evaluate the agent" — it evaluates a local rebuild.

The canonical contract types (``AgentResult``, ``TraceStep``, ``extract_sql``)
now live in ``bi_evals.provider.contract``. They are re-exported here so existing
importers keep working; new code should import from ``contract`` directly.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from bi_evals.provider.contract import AgentResult, TraceStep, extract_sql
from bi_evals.provider.cost import calculate_cost
from bi_evals.tools.base import Tool

__all__ = ["AgentResult", "TraceStep", "extract_sql", "run_agent_loop"]


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
            if block.name in tool_map and hasattr(tool, "_name"):
                path_value = block.input.get("path", "")
                if path_value:
                    files_read.append(path_value)

            # Truncate result for trace
            preview = (
                result_text[:500] + "..." if len(result_text) > 500 else result_text
            )

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
