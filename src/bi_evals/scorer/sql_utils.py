"""SQL parsing utilities using sqlglot."""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def extract_tables(sql: str, dialect: str = "snowflake") -> set[str]:
    """Extract table names from SQL, normalized to uppercase.

    Returns fully qualified names where available (e.g. "DB.SCHEMA.TABLE").
    """
    parsed = sqlglot.parse(sql, dialect=dialect)
    tables: set[str] = set()
    for stmt in parsed:
        if stmt is None:
            continue
        for table in stmt.find_all(exp.Table):
            parts = [p for p in [table.catalog, table.db, table.name] if p]
            if parts:
                tables.add(".".join(p.upper() for p in parts))
    return tables


def extract_filter_columns(sql: str, dialect: str = "snowflake") -> set[tuple[str, str]]:
    """Extract (column_name, operator) pairs from WHERE clauses.

    Returns uppercase column names paired with operator class names.
    """
    parsed = sqlglot.parse(sql, dialect=dialect)
    filters: set[tuple[str, str]] = set()
    for stmt in parsed:
        if stmt is None:
            continue
        for where in stmt.find_all(exp.Where):
            for col in where.find_all(exp.Column):
                parent = col.parent
                if isinstance(
                    parent,
                    (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.In, exp.Is),
                ):
                    op_name = type(parent).__name__.upper()
                    filters.add((col.name.upper(), op_name))
    return filters
