"""DuckDB schema for bi-evals runs, trials, test results, and dimension results.

Phase 6a introduced:
- ``trial_results``: one row per (run, test, model, trial_ix) — the atomic observation.
- ``test_results``: now an aggregate per (run, test, model) with pass_rate/stddev.
- ``dimension_results``: now per-trial — PK extended with (model, trial_ix).

Fresh databases are created in the Phase 6a shape directly. Pre-6a databases
(where ``trial_results`` did not exist) are migrated in place by ``ensure_schema``:
new columns are added with ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. The
primary keys on legacy tables are not re-created; existing rows all have
``model=''`` and ``trial_ix=0`` so the old PKs remain valid and unique.
"""

from __future__ import annotations

import duckdb

# Fresh-DB DDL. CREATE TABLE IF NOT EXISTS is safe to re-run; migration of
# pre-existing tables with the old shape is handled below in ``_migrate_legacy``.
# Indexes are kept separate (``_INDEXES_SQL``) so they run *after* legacy column
# adds — otherwise a pre-6a ``test_results`` (no ``category``) would break the
# fresh-DB index creation.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                  VARCHAR PRIMARY KEY,
    project_name            VARCHAR NOT NULL,
    timestamp               TIMESTAMP NOT NULL,
    config_snapshot         JSON NOT NULL,
    promptfoo_config        JSON,
    eval_json_path          VARCHAR NOT NULL,
    test_count              INTEGER NOT NULL,
    pass_count              INTEGER NOT NULL,
    fail_count              INTEGER NOT NULL,
    error_count             INTEGER NOT NULL,
    total_cost_usd          DOUBLE,
    total_latency_ms        BIGINT,
    total_prompt_tokens     BIGINT,
    total_completion_tokens BIGINT,
    prompt_snapshot         JSON,
    ingested_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_results (
    run_id              VARCHAR NOT NULL,
    test_id             VARCHAR NOT NULL,
    model               VARCHAR NOT NULL DEFAULT '',
    golden_id           VARCHAR,
    category            VARCHAR,
    difficulty          VARCHAR,
    tags                JSON,
    question            TEXT,
    description         TEXT,
    reference_sql       TEXT,
    generated_sql       TEXT,
    files_read          JSON,
    trace_file_path     VARCHAR,
    trace_json          JSON,
    passed              BOOLEAN NOT NULL,
    score               DOUBLE NOT NULL,
    fail_reason         TEXT,
    cost_usd            DOUBLE,
    latency_ms          BIGINT,
    prompt_tokens       BIGINT,
    completion_tokens   BIGINT,
    total_tokens        BIGINT,
    provider            VARCHAR,
    trial_count         INTEGER NOT NULL DEFAULT 1,
    pass_count          INTEGER NOT NULL DEFAULT 0,
    pass_rate           DOUBLE,
    score_mean          DOUBLE,
    score_stddev        DOUBLE,
    last_verified_at    DATE,
    PRIMARY KEY (run_id, test_id, model)
);

CREATE TABLE IF NOT EXISTS trial_results (
    run_id              VARCHAR NOT NULL,
    test_id             VARCHAR NOT NULL,
    model               VARCHAR NOT NULL,
    trial_ix            INTEGER NOT NULL,
    passed              BOOLEAN NOT NULL,
    score               DOUBLE NOT NULL,
    generated_sql       TEXT,
    fail_reason         TEXT,
    cost_usd            DOUBLE,
    latency_ms          BIGINT,
    prompt_tokens       BIGINT,
    completion_tokens   BIGINT,
    total_tokens        BIGINT,
    trace_file_path     VARCHAR,
    trace_json          JSON,
    PRIMARY KEY (run_id, test_id, model, trial_ix)
);

CREATE TABLE IF NOT EXISTS dimension_results (
    run_id       VARCHAR NOT NULL,
    test_id      VARCHAR NOT NULL,
    model        VARCHAR NOT NULL DEFAULT '',
    trial_ix     INTEGER NOT NULL DEFAULT 0,
    dimension    VARCHAR NOT NULL,
    passed       BOOLEAN NOT NULL,
    score        DOUBLE NOT NULL,
    reason       TEXT,
    is_critical  BOOLEAN NOT NULL,
    weight       DOUBLE,
    PRIMARY KEY (run_id, test_id, model, trial_ix, dimension)
);
"""

_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_tr_run    ON test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_tr_cat    ON test_results(run_id, category);
CREATE INDEX IF NOT EXISTS idx_tr_passed ON test_results(run_id, passed);
CREATE INDEX IF NOT EXISTS idx_tr_model  ON test_results(run_id, model);
CREATE INDEX IF NOT EXISTS idx_trial_run ON trial_results(run_id);
CREATE INDEX IF NOT EXISTS idx_trial_tm  ON trial_results(run_id, test_id, model);
CREATE INDEX IF NOT EXISTS idx_dr_run    ON dimension_results(run_id);
CREATE INDEX IF NOT EXISTS idx_dr_dim    ON dimension_results(run_id, dimension);
CREATE INDEX IF NOT EXISTS idx_dr_fail   ON dimension_results(run_id, dimension, passed);
"""


# Pre-6a columns missing from legacy databases. DuckDB's ALTER TABLE does not
# support inline NOT NULL / DEFAULT, so we add the column as nullable and let
# `_backfill_aggregates` populate values.
_LEGACY_MIGRATIONS = [
    # test_results
    ("test_results", "model", "VARCHAR"),
    ("test_results", "trial_count", "INTEGER"),
    ("test_results", "pass_count", "INTEGER"),
    ("test_results", "pass_rate", "DOUBLE"),
    ("test_results", "score_mean", "DOUBLE"),
    ("test_results", "score_stddev", "DOUBLE"),
    # dimension_results
    ("dimension_results", "model", "VARCHAR"),
    ("dimension_results", "trial_ix", "INTEGER"),
    # 6b
    ("runs", "prompt_snapshot", "JSON"),
    ("test_results", "last_verified_at", "DATE"),
]


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables/indexes (and apply legacy migrations) if needed.

    Idempotent — safe to run on every connection open. The order matters:
    create tables → add missing 6a/6b columns to legacy tables → backfill
    those columns → rebuild legacy PKs → finally create indexes (which now
    reference columns guaranteed to exist).
    """
    conn.execute(SCHEMA_SQL)
    _migrate_legacy(conn)
    _backfill_aggregates(conn)
    _rebuild_legacy_pks(conn)
    conn.execute(_INDEXES_SQL)


def _migrate_legacy(conn: duckdb.DuckDBPyConnection) -> None:
    """Add 6a columns to tables that predate this schema.

    Only catches ``CatalogException`` (table doesn't exist yet on a first-time
    fresh-DB path). Other errors propagate so we don't silently lose migrations.
    """
    for table, column, coltype in _LEGACY_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
        except duckdb.CatalogException:
            pass


def _rebuild_legacy_pks(conn: duckdb.DuckDBPyConnection) -> None:
    """Replace narrow pre-6a primary keys with the (model, trial_ix)-extended ones.

    DuckDB cannot drop or alter a PRIMARY KEY in place, so we copy each affected
    table to a fresh one with the correct PK and rename it back. Detection is by
    the legacy PK constraint name; once rebuilt the new constraint name is
    different and this is a no-op.
    """
    legacy_pks = {
        row[0]
        for row in conn.execute(
            """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE constraint_type = 'PRIMARY KEY'
              AND constraint_name IN (
                'test_results_run_id_test_id_pkey',
                'dimension_results_run_id_test_id_dimension_pkey'
              )
            """
        ).fetchall()
    }

    if "test_results_run_id_test_id_pkey" in legacy_pks:
        conn.execute(
            """
            CREATE TABLE test_results_new (
                run_id              VARCHAR NOT NULL,
                test_id             VARCHAR NOT NULL,
                model               VARCHAR NOT NULL DEFAULT '',
                golden_id           VARCHAR,
                category            VARCHAR,
                difficulty          VARCHAR,
                tags                JSON,
                question            TEXT,
                description         TEXT,
                reference_sql       TEXT,
                generated_sql       TEXT,
                files_read          JSON,
                trace_file_path     VARCHAR,
                trace_json          JSON,
                passed              BOOLEAN NOT NULL,
                score               DOUBLE NOT NULL,
                fail_reason         TEXT,
                cost_usd            DOUBLE,
                latency_ms          BIGINT,
                prompt_tokens       BIGINT,
                completion_tokens   BIGINT,
                total_tokens        BIGINT,
                provider            VARCHAR,
                trial_count         INTEGER NOT NULL DEFAULT 1,
                pass_count          INTEGER NOT NULL DEFAULT 0,
                pass_rate           DOUBLE,
                score_mean          DOUBLE,
                score_stddev        DOUBLE,
                last_verified_at    DATE,
                PRIMARY KEY (run_id, test_id, model)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO test_results_new (
                run_id, test_id, model, golden_id, category, difficulty, tags,
                question, description, reference_sql, generated_sql, files_read,
                trace_file_path, trace_json, passed, score, fail_reason,
                cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                provider, trial_count, pass_count, pass_rate, score_mean, score_stddev,
                last_verified_at
            )
            SELECT
                run_id, test_id, COALESCE(model, ''), golden_id, category, difficulty, tags,
                question, description, reference_sql, generated_sql, files_read,
                trace_file_path, trace_json, passed, score, fail_reason,
                cost_usd, latency_ms, prompt_tokens, completion_tokens, total_tokens,
                provider,
                COALESCE(trial_count, 1),
                COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END),
                pass_rate, score_mean, score_stddev, last_verified_at
            FROM test_results
            """
        )
        conn.execute("DROP TABLE test_results")
        conn.execute("ALTER TABLE test_results_new RENAME TO test_results")

    if "dimension_results_run_id_test_id_dimension_pkey" in legacy_pks:
        conn.execute(
            """
            CREATE TABLE dimension_results_new (
                run_id       VARCHAR NOT NULL,
                test_id      VARCHAR NOT NULL,
                model        VARCHAR NOT NULL DEFAULT '',
                trial_ix     INTEGER NOT NULL DEFAULT 0,
                dimension    VARCHAR NOT NULL,
                passed       BOOLEAN NOT NULL,
                score        DOUBLE NOT NULL,
                reason       TEXT,
                is_critical  BOOLEAN NOT NULL,
                weight       DOUBLE,
                PRIMARY KEY (run_id, test_id, model, trial_ix, dimension)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dimension_results_new (
                run_id, test_id, model, trial_ix, dimension,
                passed, score, reason, is_critical, weight
            )
            SELECT
                run_id, test_id, COALESCE(model, ''), COALESCE(trial_ix, 0), dimension,
                passed, score, reason, is_critical, weight
            FROM dimension_results
            """
        )
        conn.execute("DROP TABLE dimension_results")
        conn.execute("ALTER TABLE dimension_results_new RENAME TO dimension_results")


def _backfill_aggregates(conn: duckdb.DuckDBPyConnection) -> None:
    """Populate 6a columns for rows migrated from the pre-6a shape.

    Pre-6a rows had ``passed BOOLEAN`` only. After ALTER adds the columns as
    nullable, set pass_count/pass_rate/trial_count from ``passed`` and default
    model/trial_ix so downstream queries don't see NULLs on historical data.
    """
    conn.execute(
        """
        UPDATE test_results
        SET trial_count  = COALESCE(trial_count, 1),
            pass_count   = COALESCE(pass_count, CASE WHEN passed THEN 1 ELSE 0 END),
            pass_rate    = COALESCE(pass_rate, CASE WHEN passed THEN 1.0 ELSE 0.0 END),
            score_mean   = COALESCE(score_mean, score),
            score_stddev = COALESCE(score_stddev, 0.0),
            model        = COALESCE(model, '')
        WHERE trial_count IS NULL
           OR pass_count IS NULL
           OR pass_rate IS NULL
           OR score_mean IS NULL
           OR score_stddev IS NULL
           OR model IS NULL
        """
    )
    conn.execute(
        """
        UPDATE dimension_results
        SET model    = COALESCE(model, ''),
            trial_ix = COALESCE(trial_ix, 0)
        WHERE model IS NULL OR trial_ix IS NULL
        """
    )
