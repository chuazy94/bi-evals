"""Database client protocol and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class QueryResult:
    """Result of executing a SQL query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


@runtime_checkable
class DatabaseClient(Protocol):
    """Interface for database connections used during evaluation."""

    def execute(self, sql: str) -> QueryResult:
        """Execute SQL and return results. Sets error instead of raising."""
        ...

    def close(self) -> None:
        """Release connection resources."""
        ...
