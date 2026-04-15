"""SQL parsing utilities using sqlglot."""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def extract_tables(sql: str, dialect: str = "snowflake") -> set[str]:
    """Extract physical table names from SQL, normalized to uppercase.

    Returns fully qualified names where available (e.g. "DB.SCHEMA.TABLE").
    CTE-defined names are excluded since they aren't real tables.
    """
    parsed = sqlglot.parse(sql, dialect=dialect)
    tables: set[str] = set()
    for stmt in parsed:
        if stmt is None:
            continue
        cte_names = {cte.alias.upper() for cte in stmt.find_all(exp.CTE) if cte.alias}
        for table in stmt.find_all(exp.Table):
            parts = [p for p in [table.catalog, table.db, table.name] if p]
            if parts:
                name = ".".join(p.upper() for p in parts)
                if name not in cte_names:
                    tables.add(name)
    return tables


def extract_select_columns(sql: str, dialect: str = "snowflake") -> set[str]:
    """Extract source column names referenced in SELECT expressions.

    Walks every SELECT expression and collects the underlying Column nodes,
    stripping aliases, aggregations, window functions, and arithmetic.
    Intermediate aliases (names created by AS in inner SELECTs / CTEs) are
    excluded so that only real table columns remain.

    Example: ``SELECT SUM(DIFFERENCE) AS TOTAL`` → ``{'DIFFERENCE'}``

    Example with CTE::

        WITH cte AS (SELECT MAX(CASES) AS MAX_C FROM t)
        SELECT SUM(MAX_C) AS TOTAL FROM cte
        → ``{'CASES'}``  (MAX_C is an intermediate alias, excluded)
    """
    parsed = sqlglot.parse(sql, dialect=dialect)
    columns: set[str] = set()
    aliases: set[str] = set()
    for stmt in parsed:
        if stmt is None:
            continue
        # First pass: collect all alias names defined in SELECT clauses
        for select in stmt.find_all(exp.Select):
            for expr in select.expressions:
                if isinstance(expr, exp.Alias) and expr.alias:
                    aliases.add(expr.alias.upper())
        # Second pass: collect Column names, excluding intermediate aliases
        for select in stmt.find_all(exp.Select):
            for expr in select.expressions:
                for col in expr.find_all(exp.Column):
                    name = col.name.upper()
                    if name not in aliases:
                        columns.add(name)
    return columns


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
