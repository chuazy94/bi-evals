"""Tests for bi_evals.tools.describe_table — warehouse-agnostic DESCRIBE parsing.

Snowflake's ``DESCRIBE TABLE`` returns NAME/TYPE columns; Databricks' returns
COL_NAME/DATA_TYPE. The tool must render both, otherwise the agent is handed a
schema with blank column names and hallucinates its way through the SQL.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bi_evals.config import DatabaseConfig, DatabaseConnection
from bi_evals.db.client import QueryResult
from bi_evals.tools.describe_table import DescribeTableTool


@pytest.fixture()
def db_config() -> DatabaseConfig:
    return DatabaseConfig(
        type="databricks",
        connection=DatabaseConnection(
            server_hostname="dbc-abc.cloud.databricks.com",
            http_path="/sql/1.0/warehouses/abc123",
            access_token="dapi-token",
        ),
        query_timeout=30,
    )


def _run(db_config: DatabaseConfig, result: QueryResult) -> str:
    """Execute the tool with a stubbed database client."""
    client = MagicMock()
    client.execute.return_value = result
    with patch("bi_evals.tools.describe_table.create_db_client", return_value=client):
        tool = DescribeTableTool("describe_table", db_config)
        return tool.execute({"table_name": "samples.wanderbricks.bookings"})


class TestDescribeTableOutput:
    def test_databricks_column_keys(self, db_config: DatabaseConfig) -> None:
        """Databricks COL_NAME/DATA_TYPE rows must render names and types.

        Regression: the tool only read Snowflake's NAME/TYPE keys, so every
        Databricks column rendered as "- ()" with the type missing.
        """
        result = QueryResult(
            columns=["COL_NAME", "DATA_TYPE", "COMMENT"],
            rows=[
                {
                    "COL_NAME": "booking_id",
                    "DATA_TYPE": "bigint",
                    "COMMENT": "Unique identifier of the booking",
                },
                {"COL_NAME": "total_amount", "DATA_TYPE": "float", "COMMENT": ""},
            ],
            row_count=2,
        )
        out = _run(db_config, result)
        assert "- booking_id (bigint)  -- Unique identifier of the booking" in out
        assert "- total_amount (float)" in out
        assert "()" not in out

    def test_snowflake_column_keys(self, db_config: DatabaseConfig) -> None:
        """Snowflake NAME/TYPE rows keep working."""
        result = QueryResult(
            columns=["NAME", "TYPE"],
            rows=[{"NAME": "COUNTRY_REGION", "TYPE": "TEXT"}],
            row_count=1,
        )
        out = _run(db_config, result)
        assert "- COUNTRY_REGION (TEXT)" in out

    def test_lowercase_keys(self, db_config: DatabaseConfig) -> None:
        """Drivers returning lowercase keys are handled too."""
        result = QueryResult(
            columns=["col_name", "data_type"],
            rows=[{"col_name": "user_id", "data_type": "bigint"}],
            row_count=1,
        )
        out = _run(db_config, result)
        assert "- user_id (bigint)" in out

    def test_skips_databricks_partition_metadata(
        self, db_config: DatabaseConfig
    ) -> None:
        """Databricks appends non-column detail rows — they must be dropped."""
        result = QueryResult(
            columns=["COL_NAME", "DATA_TYPE"],
            rows=[
                {"COL_NAME": "booking_id", "DATA_TYPE": "bigint"},
                {"COL_NAME": "", "DATA_TYPE": ""},
                {"COL_NAME": "# Partition Information", "DATA_TYPE": ""},
                {"COL_NAME": "# col_name", "DATA_TYPE": "data_type"},
            ],
            row_count=4,
        )
        out = _run(db_config, result)
        assert "- booking_id (bigint)" in out
        assert "#" not in out
        assert out.count("\n") == 1  # header + exactly one column line

    def test_execution_error_surfaces(self, db_config: DatabaseConfig) -> None:
        result = QueryResult(
            columns=[], rows=[], row_count=0, error="TABLE_OR_VIEW_NOT_FOUND"
        )
        out = _run(db_config, result)
        assert out.startswith("Error:")
        assert "TABLE_OR_VIEW_NOT_FOUND" in out

    def test_rejects_invalid_table_name(self, db_config: DatabaseConfig) -> None:
        tool = DescribeTableTool("describe_table", db_config)
        out = tool.execute({"table_name": "bookings; DROP TABLE users"})
        assert out.startswith("Error: invalid table name")
