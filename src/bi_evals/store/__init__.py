"""DuckDB-backed storage for bi-evals runs."""

from bi_evals.store.client import connect
from bi_evals.store.schema import ensure_schema

__all__ = ["connect", "ensure_schema"]
