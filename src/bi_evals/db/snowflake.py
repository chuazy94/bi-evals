"""Snowflake database client implementation."""

from __future__ import annotations

import snowflake.connector
from snowflake.connector.errors import ProgrammingError

from bi_evals.config import DatabaseConfig
from bi_evals.db.client import QueryResult


class SnowflakeClient:
    """DatabaseClient implementation for Snowflake."""

    def __init__(self, config: DatabaseConfig) -> None:
        conn = config.connection
        self._conn = snowflake.connector.connect(
            account=conn.account,
            user=conn.user,
            password=conn.password,
            warehouse=conn.warehouse,
            database=conn.database,
            schema=conn.schema_,
        )
        self._timeout = config.query_timeout

    def execute(self, sql: str) -> QueryResult:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, timeout=self._timeout)
            columns = [desc[0].upper() for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return QueryResult(columns=columns, rows=rows, row_count=len(rows))
        except ProgrammingError as e:
            return QueryResult(columns=[], rows=[], row_count=0, error=str(e))
        finally:
            cursor.close()

    def close(self) -> None:
        self._conn.close()
