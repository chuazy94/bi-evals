"""DuckDB schema for bi-evals runs, test results, and dimension results."""

from __future__ import annotations

import duckdb

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
    ingested_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_results (
    run_id              VARCHAR NOT NULL,
    test_id             VARCHAR NOT NULL,
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
    model               VARCHAR,
    PRIMARY KEY (run_id, test_id)
);

CREATE TABLE IF NOT EXISTS dimension_results (
    run_id       VARCHAR NOT NULL,
    test_id      VARCHAR NOT NULL,
    dimension    VARCHAR NOT NULL,
    passed       BOOLEAN NOT NULL,
    score        DOUBLE NOT NULL,
    reason       TEXT,
    is_critical  BOOLEAN NOT NULL,
    weight       DOUBLE,
    PRIMARY KEY (run_id, test_id, dimension)
);

CREATE INDEX IF NOT EXISTS idx_tr_run    ON test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_tr_cat    ON test_results(run_id, category);
CREATE INDEX IF NOT EXISTS idx_tr_passed ON test_results(run_id, passed);
CREATE INDEX IF NOT EXISTS idx_dr_run    ON dimension_results(run_id);
CREATE INDEX IF NOT EXISTS idx_dr_dim    ON dimension_results(run_id, dimension);
CREATE INDEX IF NOT EXISTS idx_dr_fail   ON dimension_results(run_id, dimension, passed);
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables and indexes if they don't already exist."""
    conn.execute(SCHEMA_SQL)
