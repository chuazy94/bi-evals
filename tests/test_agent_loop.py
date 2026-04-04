"""Tests for the agent loop and related utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bi_evals.provider.agent_loop import AgentResult, TraceStep, extract_sql, run_agent_loop
from bi_evals.provider.cost import calculate_cost
from bi_evals.tools.file_reader import FileReaderTool


class TestExtractSql:
    def test_sql_code_fence(self) -> None:
        text = "Here's the query:\n```sql\nSELECT * FROM users\nWHERE id = 1\n```"
        assert extract_sql(text) == "SELECT * FROM users\nWHERE id = 1"

    def test_generic_code_fence_with_select(self) -> None:
        text = "```\nSELECT name FROM accounts\n```"
        assert extract_sql(text) == "SELECT name FROM accounts"

    def test_generic_code_fence_without_select_skipped(self) -> None:
        text = "```\nCREATE TABLE foo (id INT)\n```"
        assert extract_sql(text) is None

    def test_bare_select(self) -> None:
        text = "The SQL is: SELECT COUNT(*) FROM orders WHERE status = 'active';"
        result = extract_sql(text)
        assert result is not None
        assert result.startswith("SELECT COUNT(*)")

    def test_multiline_bare_select(self) -> None:
        text = "I generated:\nSELECT a, b\nFROM table1\nJOIN table2 ON a = c;"
        result = extract_sql(text)
        assert result is not None
        assert "SELECT a, b" in result
        assert "JOIN table2" in result

    def test_no_sql(self) -> None:
        text = "I don't have enough information to answer this question."
        assert extract_sql(text) is None

    def test_prefers_sql_fence_over_bare(self) -> None:
        text = (
            "Some preamble with SELECT noise\n"
            "```sql\nSELECT id FROM real_query\n```"
        )
        assert extract_sql(text) == "SELECT id FROM real_query"

    def test_case_insensitive(self) -> None:
        text = "```SQL\nselect * from TaBLe\n```"
        assert extract_sql(text) == "select * from TaBLe"


class TestCalculateCost:
    def test_known_model(self) -> None:
        cost = calculate_cost("claude-sonnet-4-5-20250929", 1000, 500)
        expected = 1000 * (3.0 / 1e6) + 500 * (15.0 / 1e6)
        assert abs(cost - expected) < 1e-10

    def test_opus_model(self) -> None:
        cost = calculate_cost("claude-opus-4-6", 1000, 500)
        expected = 1000 * (15.0 / 1e6) + 500 * (75.0 / 1e6)
        assert abs(cost - expected) < 1e-10

    def test_unknown_model_uses_default(self) -> None:
        cost = calculate_cost("unknown-model", 1000, 500)
        expected = 1000 * (3.0 / 1e6) + 500 * (15.0 / 1e6)
        assert abs(cost - expected) < 1e-10

    def test_zero_tokens(self) -> None:
        assert calculate_cost("claude-sonnet-4-5-20250929", 0, 0) == 0.0


class TestTraceStep:
    def test_tool_use_step(self) -> None:
        step = TraceStep(
            round=1,
            type="tool_use",
            tool_name="read_skill_file",
            tool_input={"path": "SKILL.md"},
            tool_result_preview="# Skill Routing...",
        )
        assert step.tool_name == "read_skill_file"
        assert step.type == "tool_use"

    def test_text_step(self) -> None:
        step = TraceStep(round=2, type="text", text="I'll query the revenue table.")
        assert step.text == "I'll query the revenue table."


class TestAgentResult:
    def test_trace_as_dicts(self) -> None:
        result = AgentResult(
            final_text="done",
            extracted_sql="SELECT 1",
            trace=[
                TraceStep(round=1, type="tool_use", tool_name="read_skill_file",
                          tool_input={"path": "SKILL.md"}, tool_result_preview="content"),
                TraceStep(round=2, type="text", text="generating SQL"),
            ],
        )
        dicts = result.trace_as_dicts()
        assert len(dicts) == 2
        assert dicts[0]["tool_name"] == "read_skill_file"
        assert dicts[1]["text"] == "generating SQL"


# --- Agent loop tests with mocked Anthropic client ---


def _make_text_block(text: str) -> Any:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(id: str, name: str, input: dict) -> Any:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = id
    block.name = name
    block.input = input
    return block


def _make_response(content: list, input_tokens: int = 100, output_tokens: int = 50) -> Any:
    """Create a mock Anthropic response."""
    response = MagicMock()
    response.content = content
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.stop_reason = "end_turn" if not any(b.type == "tool_use" for b in content) else "tool_use"
    return response


class TestRunAgentLoop:
    def _make_tool(self, tmp_path) -> FileReaderTool:
        """Create a FileReaderTool with a test file."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Routing table\nUse REVENUE.md for revenue questions.")
        (skill_dir / "REVENUE.md").write_text("# Revenue\nUse V_UNIFIED_REVENUE table.")
        return FileReaderTool(tool_name="read_skill_file", base_dir=skill_dir)

    @patch("bi_evals.provider.agent_loop.anthropic.Anthropic")
    def test_simple_text_response(self, mock_anthropic_cls, tmp_path) -> None:
        """Agent returns text immediately without tool calls."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        mock_client.messages.create.return_value = _make_response(
            [_make_text_block("```sql\nSELECT 1\n```")],
        )

        tool = self._make_tool(tmp_path)
        result = run_agent_loop(
            question="test question",
            system_prompt="You are a BI agent.",
            model="claude-sonnet-4-5-20250929",
            tools=[tool],
            tool_definitions=[tool.definition()],
            max_rounds=5,
            api_key="test-key",
        )

        assert result.extracted_sql == "SELECT 1"
        assert result.rounds == 1
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.cost > 0
        assert len(result.trace) == 1
        assert result.trace[0].type == "text"

    @patch("bi_evals.provider.agent_loop.anthropic.Anthropic")
    def test_tool_calling_loop(self, mock_anthropic_cls, tmp_path) -> None:
        """Agent calls tools then returns final text."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Round 1: agent calls read_skill_file("SKILL.md")
        round1_response = _make_response(
            [
                _make_text_block("Let me read the skill file."),
                _make_tool_use_block("call_1", "read_skill_file", {"path": "SKILL.md"}),
            ],
        )
        # Round 2: agent calls read_skill_file("REVENUE.md")
        round2_response = _make_response(
            [
                _make_text_block("Now reading revenue knowledge."),
                _make_tool_use_block("call_2", "read_skill_file", {"path": "REVENUE.md"}),
            ],
        )
        # Round 3: agent returns final SQL
        round3_response = _make_response(
            [_make_text_block("```sql\nSELECT * FROM V_UNIFIED_REVENUE\n```")],
        )

        mock_client.messages.create.side_effect = [
            round1_response,
            round2_response,
            round3_response,
        ]

        tool = self._make_tool(tmp_path)
        result = run_agent_loop(
            question="Show me revenue",
            system_prompt="You are a BI agent.",
            model="claude-sonnet-4-5-20250929",
            tools=[tool],
            tool_definitions=[tool.definition()],
            max_rounds=10,
            api_key="test-key",
        )

        assert result.extracted_sql == "SELECT * FROM V_UNIFIED_REVENUE"
        assert result.rounds == 3
        assert result.files_read == ["SKILL.md", "REVENUE.md"]
        assert result.prompt_tokens == 300  # 100 * 3 rounds
        assert result.completion_tokens == 150  # 50 * 3 rounds

        # Check trace structure
        tool_steps = [s for s in result.trace if s.type == "tool_use"]
        text_steps = [s for s in result.trace if s.type == "text"]
        assert len(tool_steps) == 2
        assert tool_steps[0].tool_name == "read_skill_file"
        assert tool_steps[0].tool_input == {"path": "SKILL.md"}
        assert "Routing table" in (tool_steps[0].tool_result_preview or "")
        assert len(text_steps) == 3

    @patch("bi_evals.provider.agent_loop.anthropic.Anthropic")
    def test_max_rounds_reached(self, mock_anthropic_cls, tmp_path) -> None:
        """Agent keeps calling tools until max rounds is hit."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        # Always return a tool call — never end
        infinite_response = _make_response(
            [_make_tool_use_block("call_n", "read_skill_file", {"path": "SKILL.md"})],
        )
        mock_client.messages.create.return_value = infinite_response

        tool = self._make_tool(tmp_path)
        result = run_agent_loop(
            question="loop forever",
            system_prompt="You are a BI agent.",
            model="claude-sonnet-4-5-20250929",
            tools=[tool],
            tool_definitions=[tool.definition()],
            max_rounds=3,
            api_key="test-key",
        )

        assert result.rounds == 3
        assert result.extracted_sql is None

    @patch("bi_evals.provider.agent_loop.anthropic.Anthropic")
    def test_unknown_tool_handled(self, mock_anthropic_cls, tmp_path) -> None:
        """Agent calls a tool that doesn't exist."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        round1 = _make_response(
            [_make_tool_use_block("call_1", "nonexistent_tool", {"foo": "bar"})],
        )
        round2 = _make_response(
            [_make_text_block("```sql\nSELECT 1\n```")],
        )
        mock_client.messages.create.side_effect = [round1, round2]

        tool = self._make_tool(tmp_path)
        result = run_agent_loop(
            question="test",
            system_prompt="test",
            model="claude-sonnet-4-5-20250929",
            tools=[tool],
            tool_definitions=[tool.definition()],
            max_rounds=5,
            api_key="test-key",
        )

        assert result.extracted_sql == "SELECT 1"
        # The unknown tool should still appear in trace
        tool_steps = [s for s in result.trace if s.type == "tool_use"]
        assert tool_steps[0].tool_name == "nonexistent_tool"
        assert "Error: unknown tool" in (tool_steps[0].tool_result_preview or "")


class TestFileReaderTool:
    def test_read_file(self, tmp_path) -> None:
        (tmp_path / "test.md").write_text("hello world")
        tool = FileReaderTool(tool_name="read_file", base_dir=tmp_path)
        assert tool.execute({"path": "test.md"}) == "hello world"

    def test_file_not_found(self, tmp_path) -> None:
        tool = FileReaderTool(tool_name="read_file", base_dir=tmp_path)
        result = tool.execute({"path": "missing.md"})
        assert "not found" in result

    def test_path_traversal_blocked(self, tmp_path) -> None:
        tool = FileReaderTool(tool_name="read_file", base_dir=tmp_path)
        result = tool.execute({"path": "../../etc/passwd"})
        assert "outside" in result

    def test_definition(self, tmp_path) -> None:
        tool = FileReaderTool(tool_name="read_skill_file", base_dir=tmp_path)
        defn = tool.definition()
        assert defn["name"] == "read_skill_file"
        assert "input_schema" in defn
        assert "path" in defn["input_schema"]["properties"]

    def test_nested_path(self, tmp_path) -> None:
        (tmp_path / "knowledge").mkdir()
        (tmp_path / "knowledge" / "REVENUE.md").write_text("revenue data")
        tool = FileReaderTool(tool_name="read_file", base_dir=tmp_path)
        assert tool.execute({"path": "knowledge/REVENUE.md"}) == "revenue data"
