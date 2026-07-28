"""
validation_db.py — Shared helper: write validation results to Postgres audit tables.

All connection details come from config.py (environment variables).
Tables are auto-created on first use via ensure_schema_exists().

Required env vars: SPG_HOST, SPG_USER, SPG_PASSWORD
See config.py for full list.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SPG_CONF, check_required
import psycopg2, psycopg2.extras, json, datetime

check_required()

# DDL file is in the same directory as this script
_DDL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'setup_validation_tables.sql')


def _spg_conn():
    return psycopg2.connect(**SPG_CONF)


def ensure_schema_exists():
    """
    Check if validation.validation_run exists in Postgres.
    If not, read setup_validation_tables.sql from the same directory
    and execute it to create the schema, tables, and views.

    Called automatically at the start of create_run() so scripts
    work against a freshly provisioned Postgres instance without manual setup.
    """
    conn = _spg_conn()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'validation'
              AND table_name   = 'validation_run'
            LIMIT 1
        """)
        exists = cur.fetchone() is not None
    except Exception:
        exists = False
    finally:
        conn.close()

    if exists:
        return

    if not os.path.exists(_DDL_FILE):
        raise FileNotFoundError(
            'setup_validation_tables.sql not found at %s. '
            'Cannot auto-create validation schema.' % _DDL_FILE
        )

    ddl = open(_DDL_FILE, 'r').read()
    conn = _spg_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(ddl)
        print('Validation schema deployed from %s' % _DDL_FILE)
    finally:
        conn.close()


def json_safe(obj):
    if obj is None:
        return None
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)[:2000]


def create_run(source_database, target_database, schemas_tested, notes=None):
    """
    Insert a new validation_run row and return (run_id, run_number).
    Auto-creates the validation schema if it does not yet exist.
    """
    ensure_schema_exists()
    conn = _spg_conn()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO validation.validation_run
            (source_database, target_database, schemas_tested, notes)
        VALUES (%s, %s, %s, %s)
        RETURNING run_id, run_number
    """, (source_database, target_database, schemas_tested, notes))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    run_id, run_number = row
    print("Validation run created: run_number=%d  run_id=%s" % (run_number, run_id))
    return str(run_id), run_number


def complete_run(run_id, total_objects, pass_count, fail_count, error_count, skip_count):
    """Update the validation_run row with final counts and completion timestamp."""
    conn = _spg_conn()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE validation.validation_run SET
            run_completed_at = NOW(),
            total_objects    = %s,
            pass_count       = %s,
            fail_count       = %s,
            error_count      = %s,
            skip_count       = %s
        WHERE run_id = %s
    """, (total_objects, pass_count, fail_count, error_count, skip_count, run_id))
    conn.commit()
    conn.close()


def insert_results(run_id, run_number, records, batch_size=50):
    """
    Bulk-insert a list of result dicts into validation.validation_result.

    Required keys per dict:
        object_name, object_type, source_schema, target_schema,
        source_call, target_call, params_used, strategy_used,
        source_call_output, target_call_output,
        source_row_count, target_row_count,
        test_verdict, issues, error_message, diff_sample,
        mssql_status, spg_status
    """
    if not records:
        return 0

    conn = _spg_conn()
    cur  = conn.cursor()

    sql = """
        INSERT INTO validation.validation_result (
            run_id, run_number,
            object_name, object_type, source_schema, target_schema,
            source_call, target_call, params_used, strategy_used,
            source_call_output, target_call_output,
            source_row_count, target_row_count,
            test_verdict, issues, error_message, diff_sample,
            mssql_status, spg_status
        ) VALUES (
            %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s
        )
    """

    inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i: i + batch_size]
        rows  = []
        for r in batch:
            rows.append((
                run_id, run_number,
                r.get('object_name', ''),
                r.get('object_type', ''),
                r.get('source_schema', ''),
                r.get('target_schema', r.get('source_schema', '')),
                r.get('source_call'),
                r.get('target_call'),
                json.dumps(json_safe(r.get('params_used')))        if r.get('params_used')        is not None else None,
                r.get('strategy_used'),
                json.dumps(json_safe(r.get('source_call_output'))) if r.get('source_call_output') is not None else None,
                json.dumps(json_safe(r.get('target_call_output'))) if r.get('target_call_output') is not None else None,
                r.get('source_row_count'),
                r.get('target_row_count'),
                r.get('test_verdict', 'ERROR'),
                r.get('issues') or [],
                r.get('error_message'),
                json.dumps(json_safe(r.get('diff_sample')))        if r.get('diff_sample')        is not None else None,
                r.get('mssql_status'),
                r.get('spg_status'),
            ))
        psycopg2.extras.execute_batch(cur, sql, rows)
        conn.commit()
        inserted += len(rows)

    conn.close()
    print("  Inserted %d validation result rows (run_number=%d)" % (inserted, run_number))
    return inserted
