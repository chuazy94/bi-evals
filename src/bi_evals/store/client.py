"""DuckDB connection helpers with schema initialization and lock retry."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from bi_evals.store.schema import ensure_schema


@contextmanager
def connect(
    db_path: Path | str,
    *,
    read_only: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a DuckDB connection, ensuring the schema exists.

    Retries briefly if the database file is locked by another process. When
    `read_only=True`, uses a shared lock so multiple readers (and the DuckDB
    CLI in read-only mode) can coexist — use this for `report`/`compare` so
    they don't collide with an open `duckdb` shell.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if read_only and not db_path.exists():
        raise RuntimeError(
            f"No DuckDB store at {db_path}. Run `bi-evals run` or "
            f"`bi-evals ingest <eval_json>` first."
        )

    last_err: Exception | None = None
    conn: duckdb.DuckDBPyConnection | None = None
    for _ in range(3):
        try:
            conn = duckdb.connect(str(db_path), read_only=read_only)
            break
        except duckdb.IOException as e:
            last_err = e
            time.sleep(0.2)
    if conn is None:
        raise RuntimeError(_format_lock_error(db_path, last_err, read_only))

    try:
        if not read_only:
            ensure_schema(conn)
        yield conn
    finally:
        conn.close()


def _format_lock_error(
    db_path: Path,
    err: Exception | None,
    read_only: bool,
) -> str:
    msg = str(err or "")
    if "Conflicting lock" in msg:
        hint = (
            "Another process is holding a write lock on this DuckDB file. "
            "Close any open `duckdb` CLI sessions or running `bi-evals` "
            "commands pointing at it."
        )
        if not read_only:
            hint += (
                " If you only need to read (e.g. for `report` or `compare`), "
                "read-only mode can coexist with other readers."
            )
        return f"Could not open DuckDB at {db_path}: {hint}\n\nUnderlying error: {msg}"
    return f"Could not open DuckDB at {db_path}: {msg}"
