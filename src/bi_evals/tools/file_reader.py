"""FileReaderTool — reads files from a configured base directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FileReaderTool:
    """Reads files relative to a base directory with path traversal protection."""

    def __init__(self, tool_name: str, base_dir: Path) -> None:
        self._name = tool_name
        self._base_dir = base_dir.resolve()

    @property
    def name(self) -> str:
        return self._name

    def definition(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "description": (
                f"Read a file from the {self._base_dir.name}/ directory. "
                "Use this to read skill and knowledge files."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file to read",
                    }
                },
                "required": ["path"],
            },
        }

    def execute(self, input: dict[str, Any]) -> str:
        raw_path = input.get("path", "")
        resolved = (self._base_dir / raw_path).resolve()

        # Path traversal protection
        if not str(resolved).startswith(str(self._base_dir)):
            return f"Error: path '{raw_path}' is outside the allowed directory."

        if not resolved.exists():
            return f"Error: file '{raw_path}' not found."

        if not resolved.is_file():
            return f"Error: '{raw_path}' is not a file."

        return resolved.read_text()
