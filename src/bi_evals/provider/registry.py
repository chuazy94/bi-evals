"""Adapter registry: maps ``agent.type`` to the adapter that produces the contract.

One contract, many adapters. Each ``agent.type`` resolves to a single adapter
whose ``produce`` returns the canonical :class:`AgentResult` (or an error string).
The provider entry point dispatches through :func:`build_adapter`; it never
branches on agent type itself, and the scorer stays entirely agent-agnostic.

This mirrors ``db/factory.py`` and ``tools/registry.py``: add a new adapter by
adding one branch here, with no change to the entry point or the scorer.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from bi_evals.config import BiEvalsConfig
from bi_evals.provider.api_endpoint import call_api_endpoint
from bi_evals.provider.contract import Adapter, AgentResult, TraceStep, extract_sql

log = logging.getLogger("bi_evals.provider")
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


@lru_cache(maxsize=None)
def _load_submissions(input_file: str) -> dict[str, dict[str, Any]]:
    """Parse a push JSONL submission file into a ``{golden_file: row}`` map.

    Cached per process (a single ``score`` run reads one file, but the adapter
    is invoked once per test) so the file is parsed once. Raises ``ValueError``
    with a row-specific message on malformed JSON, a missing ``golden_file``, or
    a duplicate ``golden_file`` (which would otherwise silently overwrite an
    earlier row and grade the wrong submission).
    """
    submissions: dict[str, dict[str, Any]] = {}
    path = Path(input_file)
    with path.open() as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{input_file}:{lineno}: invalid JSON ({e})") from e
            golden_file = row.get("golden_file")
            if not golden_file:
                raise ValueError(
                    f"{input_file}:{lineno}: row is missing required 'golden_file'."
                )
            if golden_file in submissions:
                raise ValueError(
                    f"{input_file}:{lineno}: duplicate golden_file '{golden_file}'."
                )
            submissions[golden_file] = row
    return submissions


def resolve_sql(row: dict[str, Any], golden_file: str) -> tuple[str, str, str | None]:
    """Resolve the SQL to score from a submission row.

    Real agents rarely emit clean SQL — they return it fenced or buried in prose.
    So a row may carry either a pre-extracted ``generated_sql`` or the agent's
    raw ``response_text`` (mirroring the ``api_endpoint`` adapter's
    sql-key/text-key split). Precedence:

      1. an ``error`` row is valid — the agent failed to answer; nothing to
         extract, and the adapter handles it as a failed `execution` outcome.
      2. ``generated_sql`` if present — trust the customer's explicit extraction
         (still run ``extract_sql`` so a fenced/prose value is unwrapped).
      3. else ``response_text`` — extract the SQL from the raw answer.
      4. else error — nothing to score.

    Returns ``(sql, final_text, error)``. ``error`` is non-None when no usable
    SQL could be determined, in which case ``sql``/``final_text`` are empty.
    For an ``error`` row, returns ``("", "", None)`` — valid, no SQL.
    """
    if row.get("error"):
        return "", "", None  # valid: the agent-error path is handled in produce()

    generated_sql = row.get("generated_sql")
    response_text = row.get("response_text")

    if generated_sql:
        raw = str(generated_sql)
        if re.match(r"^\s*(WITH|SELECT)\b", raw, re.IGNORECASE):
            # Already clean SQL — trust the customer's explicit extraction
            # verbatim. Running extract_sql here mangled CTEs (the bare-SELECT
            # fallback dropped the WITH prefix).
            sql = raw.strip()
        else:
            extracted = extract_sql(raw)
            sql = extracted or raw
            if extracted is None:
                # No fence/bare-SELECT found — the value was used verbatim.
                log.debug(
                    "%s: generated_sql used verbatim (no fence found)", golden_file
                )
        # Prefer the raw answer as final_text when the agent supplied one.
        final_text = str(response_text) if response_text else raw
        return sql, final_text, None

    if response_text:
        raw = str(response_text)
        sql = extract_sql(raw)
        if not sql:
            log.warning(
                "%s: no SQL extractable from response_text (%d chars)",
                golden_file,
                len(raw),
            )
            return (
                "",
                "",
                f"Push submission for '{golden_file}' has a response_text but no "
                "SQL could be extracted from it (no fenced ```sql block or bare "
                "SELECT found).",
            )
        log.debug("%s: extracted SQL from response_text: %s", golden_file, sql)
        return sql, raw, None

    return (
        "",
        "",
        f"Push submission for '{golden_file}' is missing both 'generated_sql' "
        "and 'response_text'.",
    )


def _trace_from_row(row: dict[str, Any]) -> tuple[list[TraceStep], list[str]]:
    """Normalise a submitted ``trace`` envelope into TraceSteps + files_read.

    Open-envelope: ``trace`` may be a list of step dicts, or a dict carrying a
    ``tool_calls``/``trace`` list and/or a ``files_read`` list. Whatever isn't
    understood is ignored (Pivot Phase 4 turns absent fields into ``unknown``
    dimensions). Returns ([], []) when nothing usable is present.
    """
    trace = row.get("trace")
    steps_raw: list[Any] = []
    files_read: list[str] = []

    if isinstance(trace, list):
        steps_raw = trace
    elif isinstance(trace, dict):
        steps_raw = trace.get("tool_calls") or trace.get("trace") or []
        if isinstance(trace.get("files_read"), list):
            files_read = list(trace["files_read"])

    # A top-level files_read on the row wins if present.
    if isinstance(row.get("files_read"), list):
        files_read = list(row["files_read"])

    steps: list[TraceStep] = []
    implicit_paths: list[str] = []
    for i, step in enumerate(steps_raw):
        if not isinstance(step, dict):
            continue
        steps.append(
            TraceStep(
                round=i + 1,
                type=step.get("type", "tool_use"),
                tool_name=step.get("tool_name"),
                tool_input=step.get("tool_input"),
                tool_result_preview=step.get("tool_result_preview"),
                text=step.get("text"),
            )
        )
        path_val = (step.get("tool_input") or {}).get("path")
        if path_val:
            implicit_paths.append(path_val)

    # Only fall back to paths derived from steps when no explicit files_read was
    # supplied — and capture *all* of them, not just the first.
    if not files_read and implicit_paths:
        files_read = implicit_paths

    return steps, files_read


class PushReplayAdapter:
    """Replays a customer-submitted ``{generated_sql, trace}`` row.

    The customer runs their own agent over the goldens and submits a JSONL file
    (one row per golden, keyed by ``golden_file``). This adapter calls nothing —
    it looks up the row for the test being run and returns it as the canonical
    contract. ``bi-evals score --input`` populates ``agent.push.input_file``.
    """

    def produce(
        self,
        question: str,  # protocol-required; the submitted SQL is authoritative
        prompt_vars: dict[str, Any],
        config: BiEvalsConfig,
        model: str | None,  # protocol-required; not used by push
    ) -> AgentResult | str:
        input_file = config.agent.push.input_file
        if not input_file:
            return "agent.push.input_file is not set (use `bi-evals score --input`)."

        abs_input = str(config.resolve_path(input_file))
        try:
            submissions = _load_submissions(abs_input)
        except (OSError, ValueError) as e:
            return f"Could not read push submissions: {e}"

        golden_file = prompt_vars.get("golden_file", "")
        row = submissions.get(golden_file)
        if row is None:
            return f"No push submission found for golden_file '{golden_file}'."

        steps, files_read = _trace_from_row(row)

        # An `error` row records that the agent failed to answer this golden
        # (timeout, crash, no SQL produced). It's a first-class *failing*
        # outcome, not a missing submission — the scorer fails `execution` with
        # this message rather than executing SQL.
        if row.get("error"):
            return AgentResult(
                final_text=str(row["error"]),
                extracted_sql=None,
                trace=steps,
                files_read=files_read,
                rounds=len(steps),
                agent_error=str(row["error"]),
            )

        sql, final_text, err = resolve_sql(row, golden_file)
        if err:
            return err

        return AgentResult(
            final_text=final_text,
            extracted_sql=sql,
            trace=steps,
            files_read=files_read,
            rounds=len(steps),
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost=0.0,
            latency_ms=0,
        )


def build_adapter(config: BiEvalsConfig) -> Adapter:
    """Resolve the adapter for the configured ``agent.type``."""
    agent_type = config.agent.type
    if agent_type == "anthropic_tool_loop":
        return AnthropicToolLoopAdapter()
    if agent_type == "api_endpoint":
        return ApiEndpointAdapter()
    if agent_type == "push":
        return PushReplayAdapter()
    raise ValueError(
        f"Unknown agent type: '{agent_type}'. "
        "Use 'api_endpoint', 'push', or 'anthropic_tool_loop'."
    )
