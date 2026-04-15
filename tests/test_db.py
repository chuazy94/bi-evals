"""Tests for bi_evals.db — DatabaseClient, SnowflakeClient, factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bi_evals.config import DatabaseConfig, DatabaseConnection
from bi_evals.db.client import DatabaseClient, QueryResult
from bi_evals.db.factory import create_db_client
from bi_evals.db.snowflake import SnowflakeClient


@pytest.fixture()
def db_config() -> DatabaseConfig:
    return DatabaseConfig(
        type="snowflake",
        connection=DatabaseConnection(
            account="test-account",
            user="test-user",
            private_key_path="/path/to/key.p8",
            warehouse="test-wh",
            database="test-db",
            schema_="test-schema",
        ),
        query_timeout=30,
    )


class TestQueryResult:
    def test_success(self) -> None:
        qr = QueryResult(columns=["A", "B"], rows=[{"A": 1, "B": 2}], row_count=1)
        assert qr.success is True
        assert qr.error is None

    def test_error(self) -> None:
        qr = QueryResult(columns=[], rows=[], row_count=0, error="syntax error")
        assert qr.success is False
        assert qr.error == "syntax error"


class TestSnowflakeClient:
    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_execute_success(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        mock_cursor = MagicMock()
        mock_cursor.description = [("name",), ("value",)]
        mock_cursor.fetchall.return_value = [("alice", 100), ("bob", 200)]
        mock_connect.return_value.cursor.return_value = mock_cursor

        client = SnowflakeClient(db_config)
        result = client.execute("SELECT name, value FROM t")

        assert result.success
        assert result.columns == ["NAME", "VALUE"]
        assert result.row_count == 2
        assert result.rows[0] == {"NAME": "alice", "VALUE": 100}
        assert result.rows[1] == {"NAME": "bob", "VALUE": 200}

    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_execute_sql_error(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        from snowflake.connector.errors import ProgrammingError

        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = ProgrammingError("bad SQL")
        mock_connect.return_value.cursor.return_value = mock_cursor

        client = SnowflakeClient(db_config)
        result = client.execute("SELECT bad")

        assert not result.success
        assert "bad SQL" in result.error

    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_close(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        client = SnowflakeClient(db_config)
        client.close()
        mock_connect.return_value.close.assert_called_once()

    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_connects_with_key_pair(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        SnowflakeClient(db_config)
        mock_key.assert_called_once_with("/path/to/key.p8", "")
        mock_connect.assert_called_once_with(
            account="test-account",
            user="test-user",
            private_key=b"fake-der-key",
            warehouse="test-wh",
            database="test-db",
            schema="test-schema",
        )

    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_satisfies_protocol(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        client = SnowflakeClient(db_config)
        assert isinstance(client, DatabaseClient)

    def test_requires_private_key_path(self) -> None:
        cfg = DatabaseConfig(
            type="snowflake",
            connection=DatabaseConnection(
                account="a",
                user="u",
                private_key_path="",
                warehouse="w",
                database="d",
                schema_="s",
            ),
        )
        with pytest.raises(ValueError, match="non-empty connection.private_key_path"):
            SnowflakeClient(cfg)


class TestFactory:
    @patch("bi_evals.db.snowflake._load_private_key", return_value=b"fake-der-key")
    @patch("bi_evals.db.snowflake.snowflake.connector.connect")
    def test_create_snowflake(self, mock_connect: MagicMock, mock_key: MagicMock, db_config: DatabaseConfig) -> None:
        client = create_db_client(db_config)
        assert isinstance(client, SnowflakeClient)

    def test_unknown_type(self) -> None:
        config = DatabaseConfig(type="postgres")
        with pytest.raises(ValueError, match="Unknown database type"):
            create_db_client(config)
