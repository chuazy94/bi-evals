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


def extract_columns_with_tables(
    sql: str, dialect: str = "snowflake"
) -> set[tuple[str | None, str]]:
    """Extract every ``(table, column)`` pair referenced in the SQL.

    The table component is the column's owning table (resolved via alias
    lookup) — uppercased and unqualified (no schema/db prefix), matching the
    forms users write in ``forbidden_columns``. ``None`` is returned when the
    column has no explicit table prefix and no alias resolution succeeds; the
    caller should treat that as "owner unknown" and fall back to bare-name
    matching.

    CTE-defined names are *not* treated as tables here — a column referenced
    against a CTE alias collapses to ``None`` so it can still match a bare
    forbidden name. This is intentional: a forbidden column laundered through
    a CTE is still a forbidden column.
    """
    parsed = sqlglot.parse(sql, dialect=dialect)
    pairs: set[tuple[str | None, str]] = set()
    for stmt in parsed:
        if stmt is None:
            continue

        # Names defined as CTEs anywhere in the statement. Columns against
        # these are laundered — owner unknown.
        cte_names = {
            cte.alias.upper() for cte in stmt.find_all(exp.CTE) if cte.alias
        }

        # Process each SELECT in its own alias scope. A scope's tables are the
        # ones in its own FROM/JOIN clauses, not the whole statement — that
        # way an inner CTE definition doesn't see the outer reference and a
        # column inside the CTE resolves to the CTE's source table.
        for select in stmt.find_all(exp.Select):
            alias_to_table: dict[str, str | None] = {}
            tables_in_scope: list[exp.Table] = []
            # sqlglot exposes the FROM clause under either ``from`` or ``from_``
            # depending on version — try both.
            from_clause = select.args.get("from") or select.args.get("from_")
            if from_clause is not None:
                tables_in_scope.extend(from_clause.find_all(exp.Table))
            for join in select.args.get("joins") or []:
                tables_in_scope.extend(join.find_all(exp.Table))

            for tbl in tables_in_scope:
                name = tbl.name.upper() if tbl.name else None
                # If this "table" is actually a CTE reference, mark its alias
                # as unknown rather than as itself.
                phys = None if (name and name in cte_names) else name
                alias = tbl.alias_or_name
                if alias:
                    alias_to_table[alias.upper()] = phys

            # Columns belonging to *this* SELECT's own clauses — projections,
            # WHERE/HAVING/GROUP BY, JOIN ON conditions. Subselects nested
            # inside expressions are processed separately when the outer
            # find_all reaches them.
            scope_nodes: list[exp.Expression] = list(select.expressions or [])
            for key in ("where", "having", "group", "qualify"):
                node = select.args.get(key)
                if node is not None:
                    scope_nodes.append(node)
            for join in select.args.get("joins") or []:
                on = join.args.get("on")
                if on is not None:
                    scope_nodes.append(on)

            for node in scope_nodes:
                for col in node.find_all(exp.Column):
                    # Skip columns that belong to a subselect rooted at `node`.
                    inner = col.find_ancestor(exp.Select)
                    if inner is not None and inner is not select:
                        continue
                    col_name = col.name.upper() if col.name else None
                    if not col_name:
                        continue
                    tbl_ref = col.table.upper() if col.table else None
                    if tbl_ref is None:
                        physical_tables = {
                            v for v in alias_to_table.values() if v is not None
                        }
                        owner: str | None = (
                            next(iter(physical_tables))
                            if len(physical_tables) == 1
                            else None
                        )
                    else:
                        owner = alias_to_table.get(tbl_ref, tbl_ref)
                        # If the qualifier is itself a CTE name (no alias
                        # rebinding), treat it as unknown.
                        if owner is not None and owner.upper() in cte_names:
                            owner = None
                    pairs.add((owner, col_name))
    return pairs


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
