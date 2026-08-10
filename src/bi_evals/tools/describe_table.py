"""DescribeTableTool — runs DESCRIBE TABLE against the configured database."""

from __future__ import annotations

from typing import Any

from bi_evals.db.factory import create_db_client
from bi_evals.config import DatabaseConfig


def _first(row: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty value among `keys`, case-insensitively."""
    for key in keys:
        value = row.get(key, row.get(key.lower()))
        if value:
            return str(value)
    return ""


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
                "Describe a table to get its column names and data types. "
                "Pass a fully qualified table name like DATABASE.SCHEMA.TABLE "
                "(Snowflake) or CATALOG.SCHEMA.TABLE (Databricks)."
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

        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
        )
        if not all(c in allowed for c in table_name):
            return f"Error: invalid table name '{table_name}'."

        client = create_db_client(self._db_config)
        try:
            result = client.execute(f"DESCRIBE TABLE {table_name}")
        finally:
            client.close()

        if not result.success:
            return f"Error: {result.error}"

        # Column-metadata keys differ by warehouse: Snowflake's DESCRIBE TABLE
        # returns NAME/TYPE, Databricks' returns COL_NAME/DATA_TYPE. Check both
        # so the tool stays warehouse-agnostic.
        lines = []
        for row in result.rows:
            name = _first(row, "NAME", "COL_NAME")
            dtype = _first(row, "TYPE", "DATA_TYPE")
            comment = _first(row, "COMMENT")
            # Databricks appends partition/detail sections whose rows have a
            # blank or "# ..." col_name. Those aren't columns — drop them.
            if not name or name.startswith("#"):
                continue
            line = f"- {name} ({dtype})"
            if comment:
                line += f"  -- {comment}"
            lines.append(line)

        return f"Columns for {table_name}:\n" + "\n".join(lines)
