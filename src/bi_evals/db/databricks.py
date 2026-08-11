"""Databricks SQL warehouse client (Build Stage 4, Part 2).

Mirrors :class:`~bi_evals.db.snowflake.SnowflakeClient`: same ``DatabaseClient``
protocol (``execute`` -> ``QueryResult``, ``close``), errors captured into
``QueryResult.error`` rather than raised, results normalised to uppercase column
names so downstream row-matching is warehouse-agnostic.

``databricks-sql-connector`` is an optional dependency (the ``databricks`` extra),
imported lazily so Snowflake-only users never need it.

One deliberate divergence from ``SnowflakeClient``: ``query_timeout`` is applied
at **connect** time via the driver's ``_socket_timeout``, because Databricks'
``cursor.execute()`` accepts no ``timeout`` kwarg. See ``__init__``.
"""

from __future__ import annotations

from bi_evals.config import DatabaseConfig
from bi_evals.db.client import QueryResult


class DatabricksClient:
    """DatabaseClient implementation for a Databricks SQL warehouse."""

    def __init__(self, config: DatabaseConfig) -> None:
        conn = config.connection
        hostname = (conn.server_hostname or "").strip()
        http_path = (conn.http_path or "").strip()
        token = (conn.access_token or "").strip()

        missing = [
            name
            for name, value in (
                ("connection.server_hostname", hostname),
                ("connection.http_path", http_path),
                ("connection.access_token", token),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Databricks connection requires "
                + ", ".join(missing)
                + ". Check bi-evals.yaml and that the matching ${DATABRICKS_*} "
                "variables resolve after loading your .env."
            )

        try:
            from databricks import sql as databricks_sql
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "database.type is 'databricks' but the Databricks driver is not "
                'installed. Install the extra:  uv add "bi-evals[databricks]"'
            ) from e

        # Optional session defaults — only passed when set, so an empty catalog
        # or schema doesn't override the warehouse's own defaults.
        session_kwargs: dict[str, str] = {}
        if conn.catalog:
            session_kwargs["catalog"] = conn.catalog
        if conn.schema_:
            session_kwargs["schema"] = conn.schema_

        # `query_timeout` is applied at connect time, not per-execute: unlike
        # snowflake-connector-python, databricks-sql-connector's cursor.execute()
        # takes no `timeout` kwarg. `_socket_timeout` is the driver's documented
        # equivalent — "the timeout in seconds for socket send, recv and connect
        # operations" — and does reach the HTTP transport (verified against a live
        # warehouse: it lands on the Thrift transport's socket timeout).
        #
        # Caveat, also verified live: this bounds each socket *operation*, not the
        # query end-to-end. A query whose server-side work outlasts the value can
        # still succeed, because the driver keeps polling within the timeout. It
        # protects against a wedged connection, not a slow query. Databricks has
        # no client-side statement timeout; use a server-side one
        # (`SET STATEMENT_TIMEOUT`) if you need a hard cap.
        self._conn = databricks_sql.connect(
            server_hostname=hostname,
            http_path=http_path,
            access_token=token,
            _socket_timeout=config.query_timeout,
            **session_kwargs,
        )

    def execute(self, sql: str) -> QueryResult:
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            description = cursor.description or []
            columns = [desc[0].upper() for desc in description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return QueryResult(columns=columns, rows=rows, row_count=len(rows))
        except Exception as e:
            # Contract: set error, never raise — a bad query is a failed
            # `execution` dimension, not a crashed run.
            return QueryResult(columns=[], rows=[], row_count=0, error=str(e))
        finally:
            cursor.close()

    def close(self) -> None:
        self._conn.close()
