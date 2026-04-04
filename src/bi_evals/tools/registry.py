"""Build tools from config."""

from __future__ import annotations

from pathlib import Path

from bi_evals.config import BiEvalsConfig, ToolConfig
from bi_evals.tools.base import Tool
from bi_evals.tools.file_reader import FileReaderTool


def build_tools(tool_configs: list[ToolConfig], config: BiEvalsConfig) -> list[Tool]:
    """Create tool instances from config definitions."""
    tools: list[Tool] = []
    for tc in tool_configs:
        if tc.type == "file_reader":
            base_dir = config.resolve_path(tc.config.get("base_dir", "."))
            tools.append(FileReaderTool(tool_name=tc.name, base_dir=base_dir))
        else:
            raise ValueError(
                f"Unknown tool type '{tc.type}'. Available types: file_reader"
            )
    return tools
