-- Validation audit tables for MSSQL → Snowflake Postgres migration validation
-- Run this once against your SPG instance before executing any validation scripts
-- psql -h $SPG_HOST -U snowflake_admin -d postgres -f setup_validation_tables.sql

CREATE SCHEMA IF NOT EXISTS validation;

CREATE TABLE IF NOT EXISTS validation.validation_run (
    run_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_number       SERIAL UNIQUE,
    run_started_at   TIMESTAMPTZ DEFAULT NOW(),
    run_completed_at TIMESTAMPTZ,
    source_database  TEXT,
    target_database  TEXT,
    schemas_tested   TEXT[],
    total_objects    INTEGER,
    pass_count       INTEGER DEFAULT 0,
    fail_count       INTEGER DEFAULT 0,
    error_count      INTEGER DEFAULT 0,
    skip_count       INTEGER DEFAULT 0,
    notes            TEXT,
    run_by           TEXT DEFAULT current_user
);

CREATE TABLE IF NOT EXISTS validation.validation_result (
    result_id          BIGSERIAL PRIMARY KEY,
    run_id             UUID REFERENCES validation.validation_run(run_id),
    run_number         INTEGER,
    object_name        TEXT,
    object_type        TEXT,      -- PROCEDURE | FUNCTION | VIEW
    source_schema      TEXT,
    target_schema      TEXT,
    source_call        TEXT,
    target_call        TEXT,
    params_used        JSONB,
    strategy_used      TEXT,
    source_call_output JSONB,
    target_call_output JSONB,
    source_row_count   INTEGER,
    target_row_count   INTEGER,
    test_verdict       TEXT,      -- PASS | FAIL | ERROR | SKIPPED | BOTH_FAILED | BOTH_EMPTY
                                  -- SPG_ERROR | SPG_NO_RESULTSET | SPG_ONLY | MSSQL_ONLY
    issues             TEXT[],
    error_message      TEXT,
    diff_sample        JSONB,
    validated_at       TIMESTAMPTZ DEFAULT NOW(),
    mssql_status       TEXT,
    spg_status         TEXT
);

-- Convenience summary view
CREATE OR REPLACE VIEW validation.v_run_summary AS
SELECT
    run_number,
    run_started_at,
    run_completed_at,
    source_database,
    target_database,
    schemas_tested,
    total_objects,
    pass_count,
    fail_count,
    error_count,
    skip_count,
    CASE
        WHEN run_completed_at IS NULL THEN 'IN_PROGRESS'
        WHEN fail_count = 0 AND error_count = 0 THEN 'ALL_PASS'
        WHEN pass_count > 0 THEN 'PARTIAL_PASS'
        ELSE 'FAIL'
    END AS run_status,
    notes,
    run_by
FROM validation.validation_run
ORDER BY run_number DESC;

-- Verdict breakdown per run
CREATE OR REPLACE VIEW validation.v_verdict_breakdown AS
SELECT
    run_number,
    object_type,
    test_verdict,
    COUNT(*) AS object_count
FROM validation.validation_result
GROUP BY run_number, object_type, test_verdict
ORDER BY run_number DESC, object_type, test_verdict;
