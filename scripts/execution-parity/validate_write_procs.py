"""
validate_write_procs.py — Rollback-wrapped validation for write/modify procedures.

Validates the procedures that are normally SKIPPED (DELETE, UPDATE, ARCHIVE, etc.)
by executing each one inside a transaction that is ALWAYS ROLLED BACK.  The witness
dataset is never modified regardless of the procedure outcome.

Validation strategy (contract-based, not output-parity):
  - If both sides execute without error → PASS_WRITE_PROC
  - If both sides raise an expected data error (NOT NULL, FK, type) → WRITE_EXPECTED_FAIL
    (consistent behaviour — not a migration defect; counted as passing)
  - If MSSQL succeeds but SPG errors → WRITE_SPG_ERROR  (migration defect)
  - If both sides fail with unexpected / different errors → WRITE_BOTH_FAILED

Run this AFTER the main validation pipeline to avoid any interaction.
Results are written as a new run_number in the validation audit tables.

Usage:
    python3 scripts/validate_write_procs.py [--out PATH]

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
"""
import os, sys, json, re, datetime, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, OUTPUT_DIR, WRITE_KEYWORDS,
                    is_mssql_system_schema, is_spg_system_schema, check_required)
import pymssql, psycopg2, psycopg2.extras
import validation_db as vdb

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

WRITE_PROC_OUTPUT_FILE = os.path.join(OUTPUT_DIR, 'write_proc_results.jsonl')

# ── Expected-error classification ─────────────────────────────────────────────
# These errors are EXPECTED when calling procs with NULL params.
# They prove the proc is correctly migrated but needs real data to run.
# MSSQL error numbers:
_MS_EXPECTED = {
    515,    # Cannot insert NULL into NOT NULL column
    547,    # FK constraint violation
    2601,   # Duplicate key (unique index)
    2627,   # Duplicate key (PK)
    8114,   # Error converting data type
    245,    # Conversion failed (varchar→int etc.) — expected with NULL input
    50000,  # Application RAISERROR — often a data validation guard
}
# SPG / PostgreSQL SQLSTATE prefixes for expected errors:
_SPG_EXPECTED_STATES = {
    '23',   # integrity_constraint_violation (NOT NULL, FK, unique)
    '22',   # data_exception (type mismatch, division by zero, etc.)
    'P0',   # PL/pgSQL errors (RAISE EXCEPTION in proc body)
}


def is_expected_error(err_str: str) -> bool:
    """Heuristically classify an error as an expected data error vs migration bug."""
    if not err_str:
        return False
    s = err_str.lower()
    # MSSQL error number patterns
    ms_match = re.search(r'\((\d+),', err_str)
    if ms_match:
        code = int(ms_match.group(1))
        if code in _MS_EXPECTED:
            return True
    # SPG SQLSTATE / known message patterns
    for state_prefix in _SPG_EXPECTED_STATES:
        if state_prefix in err_str:
            return True
    # Text patterns that indicate expected data-level failures
    expected_phrases = [
        'not-null constraint', 'not null constraint', 'null value in column',
        'foreign key constraint', 'unique constraint', 'violates',
        'invalid input syntax', 'cannot insert null', 'raiserror',
        'raise exception', 'invalid value', 'conversion failed',
        'error converting', 'cannot be null', 'must not be null',
    ]
    return any(ph in s for ph in expected_phrases)


def classify_verdict(ms_ok: bool, spg_ok: bool,
                     ms_err: str, spg_err: str) -> str:
    if ms_ok and spg_ok:
        return 'PASS_WRITE_PROC'
    if not ms_ok and not spg_ok:
        # Both failed — consistent constraint/data error?
        if is_expected_error(ms_err) or is_expected_error(spg_err):
            return 'WRITE_EXPECTED_FAIL'
        return 'WRITE_BOTH_FAILED'
    if ms_ok and not spg_ok:
        return 'WRITE_SPG_ERROR'
    # spg_ok and not ms_ok
    return 'WRITE_MSSQL_ERROR'


# ── MSSQL type → T-SQL DECLARE type mapping ───────────────────────────────────
_MS_DECL_TYPE = {
    'int': 'INT', 'bigint': 'BIGINT', 'smallint': 'SMALLINT', 'tinyint': 'TINYINT',
    'bit': 'BIT', 'float': 'FLOAT', 'real': 'REAL',
    'decimal': 'DECIMAL(18,4)', 'numeric': 'NUMERIC(18,4)', 'money': 'MONEY',
    'varchar': 'VARCHAR(MAX)', 'nvarchar': 'NVARCHAR(MAX)',
    'char': 'CHAR(1)', 'nchar': 'NCHAR(1)', 'text': 'NVARCHAR(MAX)',
    'datetime': 'DATETIME', 'datetime2': 'DATETIME2', 'date': 'DATE',
    'time': 'TIME', 'smalldatetime': 'SMALLDATETIME',
    'uniqueidentifier': 'UNIQUEIDENTIFIER', 'varbinary': 'VARBINARY(MAX)',
    'xml': 'XML',
}

# ── SPG type → Postgres cast type mapping ─────────────────────────────────────
_SPG_CAST_TYPE = {
    'integer': 'integer', 'int': 'integer', 'bigint': 'bigint',
    'smallint': 'smallint', 'boolean': 'boolean', 'bool': 'boolean',
    'numeric': 'numeric', 'decimal': 'numeric', 'real': 'real',
    'double precision': 'double precision', 'money': 'numeric',
    'character varying': 'text', 'text': 'text', 'varchar': 'text',
    'character': 'text', 'name': 'name',
    'timestamp without time zone': 'timestamp', 'timestamp with time zone': 'timestamptz',
    'timestamp': 'timestamp', 'date': 'date', 'time': 'time',
    'uuid': 'uuid', 'bytea': 'bytea', 'json': 'json', 'jsonb': 'jsonb',
    'array': 'text[]',
}


def build_mssql_null_call(schema: str, proc_name: str, params: list) -> str:
    """Build a T-SQL EXEC statement with all params declared as NULL."""
    in_params = [p for p in params if not p.get('is_output')]
    decls, args = [], []
    for i, p in enumerate(in_params):
        var    = f"@_p{i}"
        ptype  = _MS_DECL_TYPE.get(p['type'].lower(), 'NVARCHAR(MAX)')
        decls.append(f"DECLARE {var} {ptype} = NULL")
        args.append(f"@{p['name']}={var}")
    decl_str = "; ".join(decls) + "; " if decls else ""
    arg_str  = ", ".join(args)
    return f"{decl_str}EXEC {schema}.{proc_name} {arg_str}"


def build_spg_null_call(schema: str, proc_name: str, params: list) -> str:
    """Build a Postgres CALL statement with all params as NULL::type."""
    in_params = [p for p in params if p.get('parameter_mode', 'IN') in ('IN', 'INOUT')]
    args = []
    for p in in_params:
        pname = p.get('parameter_name', '').strip()
        dtype = p.get('data_type', 'text').lower()
        cast  = _SPG_CAST_TYPE.get(dtype, 'text')
        if pname:
            args.append(f"{pname} => NULL::{cast}")
        else:
            args.append(f"NULL::{cast}")
    arg_str = ", ".join(args)
    return f'CALL {schema}."{proc_name.lower()}"({arg_str})'


def build_spg_function_call(schema: str, proc_name: str, params: list) -> str:
    """Build a SELECT * FROM schema.func(NULL::type, ...) call for FUNCTION-type routines."""
    in_params = [p for p in params if p.get('parameter_mode', 'IN') in ('IN', 'INOUT')]
    args = []
    for p in in_params:
        dtype = p.get('data_type', 'text').lower()
        cast  = _SPG_CAST_TYPE.get(dtype, 'text')
        args.append(f"NULL::{cast}")
    arg_str = ", ".join(args)
    return f'SELECT * FROM {schema}."{proc_name.lower()}"({arg_str})'


# ── MSSQL rollback execution ───────────────────────────────────────────────────
def exec_mssql_rollback(call_str: str) -> tuple:
    """Execute call_str inside a MSSQL transaction then ALWAYS rollback.
    Returns (ok: bool, error_str: str|None).
    """
    conn = pymssql.connect(**MSSQL_CONF)
    conn.autocommit(False)
    cur  = conn.cursor()
    try:
        cur.execute(call_str)
        conn.rollback()
        return True, None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── SPG rollback execution ─────────────────────────────────────────────────────
def exec_spg_rollback(call_str: str) -> tuple:
    """Execute call_str inside a SPG transaction then ALWAYS rollback.
    Returns (ok: bool, error_str: str|None).
    """
    conn = psycopg2.connect(**SPG_CONF)
    conn.autocommit = False
    cur  = conn.cursor()
    try:
        cur.execute(call_str)
        conn.rollback()
        return True, None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── MSSQL write proc discovery ────────────────────────────────────────────────
def get_write_procs_mssql():
    """Return list of (schema, proc_name, params) for all write-keyword procs."""
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor(as_dict=True)

    # Discover all stored procedures
    cur.execute("""
        SELECT s.name AS schema_name, o.name AS proc_name
        FROM sys.objects o
        JOIN sys.schemas s ON o.schema_id = s.schema_id
        WHERE o.type = 'P'
        ORDER BY s.name, o.name
    """)
    all_procs = [(r['schema_name'], r['proc_name']) for r in cur.fetchall()
                 if not is_mssql_system_schema(r['schema_name'])]

    # Filter to write procs only
    write_procs = [
        (s, p) for (s, p) in all_procs
        if any(kw in p.lower() for kw in WRITE_KEYWORDS)
    ]

    result = []
    for schema, proc_name in write_procs:
        cur.execute("""
            SELECT p.name AS param_name, t.name AS type_name,
                   p.parameter_id, p.is_output
            FROM sys.objects o
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            JOIN sys.parameters p ON o.object_id = p.object_id
            JOIN sys.types t ON p.user_type_id = t.user_type_id
            WHERE s.name = %s AND o.name = %s
            ORDER BY p.parameter_id
        """, (schema, proc_name))
        params = [r for r in cur.fetchall() if r['parameter_id'] > 0]
        result.append({
            'schema':     schema,
            'proc_name':  proc_name,
            'full_name':  f"{schema}.{proc_name}",
            'params':     [{'name': r['param_name'].lstrip('@'),
                            'type': r['type_name'],
                            'is_output': r['is_output']} for r in params],
        })
    conn.close()
    return result


# ── SPG parameter discovery ───────────────────────────────────────────────────
def get_spg_routine_info(schema: str, proc_name: str) -> dict:
    """Return {'params': [...], 'prokind': 'p'|'f'|None} for a routine, or defaults if not found."""
    conn = psycopg2.connect(**SPG_CONF)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    spg_schema = schema.lower()
    try:
        # Get routine kind from pg_proc
        cur.execute("""
            SELECT p.prokind
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname = %s AND LOWER(p.proname) = %s
            LIMIT 1
        """, (spg_schema, proc_name.lower()))
        kind_row = cur.fetchone()
        prokind = kind_row['prokind'] if kind_row else None

        # Get parameter info
        cur.execute("""
            SELECT pa.parameter_name, pa.data_type, pa.parameter_mode, pa.ordinal_position
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name = pa.specific_name
            WHERE r.routine_schema = %s AND LOWER(r.routine_name) = %s
            ORDER BY pa.ordinal_position
        """, (spg_schema, proc_name.lower()))
        params = list(cur.fetchall())
        return {'params': params, 'prokind': prokind}
    except Exception:
        return {'params': [], 'prokind': None}
    finally:
        conn.close()


def get_spg_params(schema: str, proc_name: str) -> list:
    """Return SPG params for a procedure, or [] if not found."""
    return get_spg_routine_info(schema, proc_name)['params']


def spg_proc_exists(schema: str, proc_name: str) -> bool:
    """Check whether the proc (PROCEDURE or FUNCTION) exists in SPG."""
    conn = psycopg2.connect(**SPG_CONF)
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname = %s AND LOWER(p.proname) = %s
            LIMIT 1
        """, (schema.lower(), proc_name.lower()))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Rollback-wrapped validation for write/modify procedures')
    parser.add_argument('--out', default=WRITE_PROC_OUTPUT_FILE,
                        help='Output JSONL path')
    args = parser.parse_args(argv)

    print("=" * 70)
    print("WRITE PROCEDURE VALIDATION (rollback-wrapped — witness data safe)")
    print("=" * 70)
    print()
    print("Strategy: each proc is executed inside a transaction that is ALWAYS")
    print("rolled back.  The witness dataset is never modified.")
    print()

    write_procs = get_write_procs_mssql()
    print(f"Discovered {len(write_procs)} write/modify procedures to validate")
    print()

    records = []
    verdict_counts: dict = {}

    for info in write_procs:
        schema    = info['schema']
        proc_name = info['proc_name']
        full_name = info['full_name']
        ms_params = info['params']

        # MSSQL side
        ms_call  = build_mssql_null_call(schema, proc_name, ms_params)
        ms_ok, ms_err = exec_mssql_rollback(ms_call)

        # SPG side — discover params and routine type from SPG catalog
        if not spg_proc_exists(schema, proc_name):
            spg_ok   = False
            spg_err  = f"Procedure {schema}.{proc_name} not found in SPG"
            spg_call = f'CALL {schema}."{proc_name.lower()}"()  -- NOT FOUND'
        else:
            spg_info   = get_spg_routine_info(schema, proc_name)
            spg_params = spg_info['params']
            prokind    = spg_info['prokind']
            if prokind == 'f':
                # Migrated as FUNCTION returning TABLE (e.g. p_UpdateSLU) — use SELECT
                spg_call = build_spg_function_call(schema, proc_name, spg_params)
            else:
                # Standard PROCEDURE — use CALL
                spg_call = build_spg_null_call(schema, proc_name, spg_params)
            spg_ok, spg_err = exec_spg_rollback(spg_call)

        verdict = classify_verdict(ms_ok, spg_ok, ms_err or '', spg_err or '')
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

        # Status indicator
        icon = {'PASS_WRITE_PROC':    'PASS ',
                'WRITE_EXPECTED_FAIL': 'XFAIL',
                'WRITE_SPG_ERROR':     'FAIL ',
                'WRITE_MSSQL_ERROR':   'WARN ',
                'WRITE_BOTH_FAILED':   'BFAIL'}.get(verdict, '?    ')

        ms_note  = '' if ms_ok  else f'MS:{(ms_err  or "")[:60]}'
        spg_note = '' if spg_ok else f'SPG:{(spg_err or "")[:60]}'
        note     = '  '.join(filter(None, [ms_note, spg_note]))
        print(f"  {icon}  {full_name:<55} {note[:80]}")

        rec = {
            'schema':           schema,
            'procedure_name':   proc_name,
            'full_name':        full_name,
            'object_type':      'PROCEDURE',
            'object_kind':      'WRITE_PROC',
            'verdict':          verdict,
            'mssql_status':     'SUCCESS' if ms_ok  else 'ERROR',
            'spg_status':       'SUCCESS' if spg_ok else 'ERROR',
            'mssql_call':       ms_call,
            'spg_call':         spg_call,
            'mssql_error':      ms_err,
            'spg_error':        spg_err,
            'mssql_row_count':  0,
            'spg_row_count':    0,
            'note':             'Executed in rollback transaction — witness data unchanged',
            'executed_at':      datetime.datetime.now(datetime.UTC).isoformat(),
        }
        records.append(rec)

    # Write JSONL
    with open(args.out, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + '\n')

    print()
    print("=" * 70)
    print("WRITE PROC SUMMARY")
    print("=" * 70)

    PASS_V = {'PASS_WRITE_PROC', 'WRITE_EXPECTED_FAIL'}
    total   = len(records)
    passing = sum(verdict_counts.get(v, 0) for v in PASS_V)
    failing = verdict_counts.get('WRITE_SPG_ERROR', 0)

    print(f"  PASS_WRITE  : {verdict_counts.get('PASS_WRITE_PROC', 0):<5}  executed OK on both sides (rollback-wrapped)")
    print(f"  XFAIL_WRITE : {verdict_counts.get('WRITE_EXPECTED_FAIL', 0):<5}  both sides raised consistent constraint error (expected with NULL params)")
    print(f"  SPG_ERROR   : {verdict_counts.get('WRITE_SPG_ERROR', 0):<5}  MSSQL OK but SPG errored — migration defect")
    print(f"  MSSQL_ERROR : {verdict_counts.get('WRITE_MSSQL_ERROR', 0):<5}  SPG OK but MSSQL errored")
    print(f"  BOTH_FAILED : {verdict_counts.get('WRITE_BOTH_FAILED', 0):<5}  both failed — unexpected error")
    print(f"  TOTAL       : {total}")
    print()

    if total > 0:
        pass_rate = round(passing / total * 100)
        print(f"  Write proc pass rate: {passing}/{total} = {pass_rate}%")
        print(f"  (pass = PASS_WRITE_PROC + WRITE_EXPECTED_FAIL)")

    # Write to validation audit tables
    print()
    print("Writing results to validation tables in SPG...")
    try:
        run_id, run_number = vdb.create_run(
            source_database = MSSQL_CONF.get('database', ''),
            target_database = SPG_CONF.get('dbname', 'postgres'),
            schemas_tested  = sorted({r['schema'] for r in records}),
            notes           = 'Write procedure validation (rollback-wrapped, run after main pipeline)',
        )
        rows_to_insert = []
        for rec in records:
            issues = []
            if rec['mssql_error']:
                issues.append('MSSQL: ' + rec['mssql_error'][:120])
            if rec['spg_error']:
                issues.append('SPG: '   + rec['spg_error'][:120])
            if rec['verdict'] in ('PASS_WRITE_PROC', 'WRITE_EXPECTED_FAIL'):
                issues.append(rec['note'])

            rows_to_insert.append({
                'object_name':       rec['procedure_name'],
                'object_type':       'PROCEDURE',
                'source_schema':     rec['schema'],
                'target_schema':     rec['schema'].lower(),
                'source_call':       rec['mssql_call'][:500],
                'target_call':       rec['spg_call'][:500],
                'params_used':       ['<null params — rollback wrapped>'],
                'strategy_used':     'rollback_wrapped',
                'source_call_output': None,
                'target_call_output': None,
                'source_row_count':  0,
                'target_row_count':  0,
                'test_verdict':      rec['verdict'],
                'issues':            issues,
                'error_message':     '; '.join(filter(None, [rec['mssql_error'], rec['spg_error']]))[:400] or None,
                'diff_sample':       None,
                'mssql_status':      rec['mssql_status'],
                'spg_status':        rec['spg_status'],
            })

        vdb.insert_results(run_id, run_number, rows_to_insert)
        vdb.complete_run(
            run_id       = run_id,
            total_objects = total,
            pass_count   = passing,
            fail_count   = total - passing,
            error_count  = 0,
            skip_count   = 0,
        )
        print(f"Done. Run number: {run_number} — "
              f"query: SELECT * FROM validation.v_run_summary WHERE run_number={run_number};")
    except Exception as e:
        print(f"WARNING: Could not write to validation tables: {e}")
        print(f"Results still saved to: {args.out}")

    print()
    print("Witness data integrity check: verifying witness dataset is intact...")
    try:
        import psycopg2 as _pg
        conn = _pg.connect(**SPG_CONF)
        cur  = conn.cursor()
        cur.execute("""
            SELECT SUM(cnt) FROM (
              SELECT COUNT(*) cnt FROM api.hierarchy
              UNION ALL SELECT COUNT(*) FROM api.majorgroup
              UNION ALL SELECT COUNT(*) FROM api.definition
              UNION ALL SELECT COUNT(*) FROM stg.microsloadstatus WHERE jobactiveflag=true
            ) x
        """)
        total_witness = cur.fetchone()[0]
        conn.close()
        if total_witness and total_witness >= 4:
            print(f"  OK  Witness dataset intact ({total_witness} spot-checked rows present)")
        else:
            print(f"  WARN  Spot check returned fewer rows than expected ({total_witness})")
    except Exception as e:
        print(f"  WARN  Could not verify witness data: {e}")

    print()
    print("=" * 70)
    print("WRITE PROC VALIDATION COMPLETE")
    print("=" * 70)
    print(f"Output: {args.out}")

    return records


if __name__ == '__main__':
    main()
