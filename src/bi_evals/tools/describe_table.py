"""DescribeTableTool — runs DESCRIBE TABLE against the configured database."""

from __future__ import annotations

from typing import Any

from bi_evals.db.factory import create_db_client
from bi_evals.config import DatabaseConfig


class DescribeTableTool:
    """Executes DESCRIBE TABLE and returns column names/types."""

    def __init__(self, tool_name: str, db_config: DatabaseConfig) -> None:
        self._name = tool_name
        self._db_config = db_config

    @property
    def name(self) -> str:
        return self._name

    def definition(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "description": (
                "Describe a Snowflake table to get its column names and data types. "
                "Pass a fully qualified table name like DATABASE.SCHEMA.TABLE."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": (
                            "Fully qualified table name (e.g. DATABASE.SCHEMA.TABLE)"
                        ),
                    }
                },
                "required": ["table_name"],
            },
        }

    def execute(self, input: dict[str, Any]) -> str:
        table_name = input.get("table_name", "").strip()
        if not table_name:
            return "Error: table_name is required."

        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")
        if not all(c in allowed for c in table_name):
            return f"Error: invalid table name '{table_name}'."

        client = create_db_client(self._db_config)
        try:
            result = client.execute(f"DESCRIBE TABLE {table_name}")
        finally:
            client.close()

        if not result.success:
            return f"Error: {result.error}"

        lines = []
        for row in result.rows:
            name = row.get("NAME", row.get("name", ""))
            dtype = row.get("TYPE", row.get("type", ""))
            comment = row.get("COMMENT", row.get("comment", ""))
            line = f"- {name} ({dtype})"
            if comment:
                line += f"  -- {comment}"
            lines.append(line)

        return f"Columns for {table_name}:\n" + "\n".join(lines)
