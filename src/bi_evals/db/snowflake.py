"""Snowflake database client implementation using key pair authentication."""

from __future__ import annotations

from pathlib import Path

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from snowflake.connector.errors import ProgrammingError

from bi_evals.config import DatabaseConfig
from bi_evals.db.client import QueryResult


def _load_private_key(key_path: str, passphrase: str = "") -> bytes:
    """Load a PEM private key and return DER-encoded bytes for Snowflake."""
    key_bytes = Path(key_path).expanduser().resolve().read_bytes()
    pwd = passphrase.encode() if passphrase else None
    private_key = serialization.load_pem_private_key(
        key_bytes, password=pwd, backend=default_backend(),
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class SnowflakeClient:
    """DatabaseClient implementation for Snowflake using key pair auth."""

    def __init__(self, config: DatabaseConfig) -> None:
        conn = config.connection
        key_path = (conn.private_key_path or "").strip()
        if not key_path:
            raise ValueError(
                "Snowflake connection requires a non-empty connection.private_key_path "
                "(path to your PEM / PKCS8 key file). Check bi-evals.yaml and that "
                "${SNOWFLAKE_PRIVATE_KEY_PATH} resolves after loading your .env."
            )
        private_key_der = _load_private_key(
            key_path, conn.private_key_passphrase,
        )
        self._conn = snowflake.connector.connect(
            account=conn.account,
            user=conn.user,
            private_key=private_key_der,
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
