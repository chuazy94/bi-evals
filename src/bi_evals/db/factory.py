"""Factory for creating database clients from config."""

from __future__ import annotations

from bi_evals.config import DatabaseConfig
from bi_evals.db.client import DatabaseClient


def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    """Create a database client based on the config type."""
    if config.type == "snowflake":
        from bi_evals.db.snowflake import SnowflakeClient

        return SnowflakeClient(config)
    raise ValueError(f"Unknown database type: '{config.type}'")
