"""
mssql_proc_executor.py — Execute MSSQL stored procedures (generic).

Discovers all business schemas dynamically. Samples real parameter values
from primary tables where possible; falls back to typed NULLs.
Saves a shared params file for the SPG executor to reuse.

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE
Optional env vars: MSSQL_PORT, VALIDATION_OUTPUT_DIR, VALIDATION_SKIP_WRITES,
                   VALIDATION_WRITE_KEYWORDS
See config.py for full list.
"""
import os, sys, json, datetime, decimal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, MSSQL_OUTPUT_FILE, SHARED_PARAMS_FILE,
                    SKIP_WRITE_PROCS, WRITE_KEYWORDS, OUTPUT_DIR,
                    is_mssql_system_schema, check_required)
import pymssql
from param_discovery import sample_mssql_params

# ── Seed profile support ──────────────────────────────────────────────────────
SHARED_DIR          = os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', os.path.join(os.getcwd(), 'shared')))
SEED_PROFILES_PATH  = os.path.join(SHARED_DIR, 'seed_profiles.json')

def load_seed_profiles():
    """Load seed_profiles.json. Returns {} if not found or malformed."""
    for path in (SEED_PROFILES_PATH,
                 os.path.join(SHARED_DIR, 'seed_profiles.yaml')):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return data.get('seed_profiles', {})
            except Exception:
                pass
    return {}

def build_scope_map(profiles):
    """Return dict: proc_full_name_lower -> scenario_id."""
    scope_map = {}
    for scenario_id, profile in profiles.items():
        for obj in profile.get('object_scope', []):
            scope_map[obj.strip().lower()] = scenario_id
    return scope_map

def apply_prereq_sql(prereq_sqls, scenario_id):
    """Run prereq_mssql_sql statements on a fresh connection. Returns (ok, err)."""
    if not prereq_sqls:
        return True, None
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor()
    for sql in prereq_sqls:
        sql = sql.strip()
        if not sql:
            continue
        try:
            cur.execute(sql)
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return False, 'prereq[%s]: %s' % (scenario_id, str(e)[:150])
    conn.close()
    return True, None

def check_readiness(checks, scenario_id):
    """Verify readiness_checks_mssql all return count > 0. Returns (ok, failed)."""
    if not checks:
        return True, None
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor()
    for sql in checks:
        sql = sql.strip()
        if not sql:
            continue
        try:
            cur.execute(sql)
            row   = cur.fetchone()
            count = row[0] if row else 0
            if int(count) == 0:
                conn.close()
                return False, sql
        except Exception as e:
            conn.close()
            return False, '%s -> %s' % (sql[:60], str(e)[:60])
    conn.close()
    return True, None

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

# User-defined table types — cannot be passed as NULL, procedure is untestable
UDTT_TYPES = {'inputtype', 'idlist', 'idlistdb', 'guidlist', 'stringlist',
              'intlist', 'bigintlist', 'tinyintlist'}

# ── Type mapping ──────────────────────────────────────────────────────────────
def mssql_type_str(type_name):
    """Return SQL Server type declaration string for DECLARE @var <type>."""
    t = type_name.lower()
    if t in UDTT_TYPES:
        return None   # signal: untestable
    TYPE_DECL = {
        'int': 'INT', 'bigint': 'BIGINT', 'smallint': 'SMALLINT', 'tinyint': 'TINYINT',
        'varchar': 'VARCHAR(MAX)', 'nvarchar': 'NVARCHAR(MAX)',
        'char': 'CHAR', 'nchar': 'NCHAR(10)', 'text': 'VARCHAR(MAX)', 'ntext': 'NVARCHAR(MAX)',
        'datetime': 'DATETIME', 'datetime2': 'DATETIME2', 'smalldatetime': 'SMALLDATETIME',
        'date': 'DATE', 'time': 'TIME', 'datetimeoffset': 'DATETIMEOFFSET',
        'decimal': 'DECIMAL(18,6)', 'numeric': 'NUMERIC(18,6)',
        'float': 'FLOAT', 'real': 'REAL', 'money': 'MONEY', 'smallmoney': 'SMALLMONEY',
        'bit': 'BIT', 'varbinary': 'VARBINARY(MAX)', 'binary': 'BINARY(8)',
        'uniqueidentifier': 'UNIQUEIDENTIFIER', 'xml': 'XML', 'sql_variant': 'SQL_VARIANT',
    }
    return TYPE_DECL.get(t, 'NVARCHAR(MAX)')

# ── Helpers ───────────────────────────────────────────────────────────────────
def serialize_val(v):
    if v is None: return None
    if isinstance(v, bool): return bool(v)
    if isinstance(v, int): return int(v)
    if isinstance(v, (float, decimal.Decimal)):
        try:    return round(float(v), 6)
        except: return str(v)
    if isinstance(v, datetime.datetime): return v.strftime('%Y-%m-%dT%H:%M:%S')
    if isinstance(v, datetime.date):     return v.strftime('%Y-%m-%d')
    if isinstance(v, datetime.time):     return v.strftime('%H:%M:%S')
    if isinstance(v, (bytes, bytearray)): return v.hex()
    return str(v)

def serialize_row(row):
    return [serialize_val(v) for v in row]

def ms_conn():
    return pymssql.connect(**MSSQL_CONF)

def should_skip(name):
    if not SKIP_WRITE_PROCS:
        return False
    n = name.lower()
    return any(kw in n for kw in WRITE_KEYWORDS)

# ── Schema discovery ──────────────────────────────────────────────────────────
def discover_schemas():
    """Return all business schemas that contain stored procedures OR functions."""
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor()
    cur.execute("""
        SELECT DISTINCT s.name
        FROM sys.objects o
        JOIN sys.schemas s ON o.schema_id = s.schema_id
        WHERE o.type IN ('P', 'FN', 'IF', 'TF')
        ORDER BY s.name
    """)
    schemas = [r[0] for r in cur.fetchall()
               if not is_mssql_system_schema(r[0])]
    conn.close()
    return schemas

# ── Procedure + Function discovery ───────────────────────────────────────────
def get_all_procs(schema):
    """Return dict of name_lower -> {name, proc_def, params, obj_kind} for a schema.
    Discovers stored procedures (P) AND functions (FN=scalar, IF=inline TVF, TF=multi-stmt TVF).
    """
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor(as_dict=True)

    # Discover procedures AND functions
    cur.execute("""
        SELECT o.name AS proc_name, o.type AS obj_type, o.type_desc AS obj_type_desc
        FROM sys.objects o
        JOIN sys.schemas s ON o.schema_id = s.schema_id
        WHERE s.name = %s AND o.type IN ('P', 'FN', 'IF', 'TF')
        ORDER BY o.name
    """, (schema,))
    procs = [(r['proc_name'], r['obj_type'].strip(), r['obj_type_desc']) for r in cur.fetchall()]

    result = {}
    for proc_name_raw, obj_type, obj_type_desc in procs:
        # Parameters work the same for procs and functions (via sys.parameters)
        cur.execute("""
            SELECT p.name AS param_name, t.name AS type_name,
                   p.parameter_id, p.is_output
            FROM sys.objects o
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            JOIN sys.parameters p ON o.object_id = p.object_id
            JOIN sys.types t ON p.user_type_id = t.user_type_id
            WHERE s.name = %s AND o.name = %s
            ORDER BY p.parameter_id
        """, (schema, proc_name_raw))
        params = cur.fetchall()
        # Filter out return-value pseudo-parameter (parameter_id=0 for functions)
        params = [r for r in params if r['parameter_id'] > 0]

        cur.execute("""
            SELECT sm.definition
            FROM sys.objects o
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            JOIN sys.sql_modules sm ON o.object_id = sm.object_id
            WHERE s.name = %s AND o.name = %s
        """, (schema, proc_name_raw))
        def_row = cur.fetchone()

        # Map SQL Server type code to a friendly kind label
        kind_map = {'P': 'PROCEDURE', 'FN': 'FUNCTION',
                    'IF': 'FUNCTION', 'TF': 'FUNCTION'}
        obj_kind = kind_map.get(obj_type, 'PROCEDURE')

        result[proc_name_raw.lower()] = {
            'name':     proc_name_raw,
            'obj_kind': obj_kind,
            'obj_type': obj_type,          # raw SQL Server type code
            'proc_def': def_row['definition'] if def_row else '',
            'params':   [{'name':      r['param_name'].lstrip('@'),
                          'type':      r['type_name'],
                          'is_output': r['is_output']} for r in params],
        }
    conn.close()
    return result

# ── Execution ─────────────────────────────────────────────────────────────────
def exec_func(schema, func_name, param_info, obj_type='IF', override_params=None, proc_def=''):
    """Execute a MSSQL scalar or table-valued function and return a result dict."""
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor()
    in_params = [p for p in param_info if not p.get('is_output')]
    sampled_table = sample_sql_used = None

    # Use override_params when provided (from seed profile parameter_bindings),
    # otherwise try real data sampling, then fall back to NULL literals.
    if override_params and len(override_params) == len(in_params):
        resolved = []
        for v in override_params:
            # Values prefixed with "sql:" are evaluated as live SQL queries.
            if isinstance(v, str) and v.strip().upper().startswith('SELECT'):
                try:
                    cur.execute(v.strip())
                    row = cur.fetchone()
                    resolved.append(str(row[0]) if row and row[0] is not None else 'NULL')
                except Exception:
                    resolved.append('NULL')
            else:
                resolved.append(str(v) if v is not None else 'NULL')
        arg_str = ', '.join(resolved)
        param_source = 'seed_profile_bindings'
    elif in_params and proc_def:
        # Try real data sampling (same as exec_proc does)
        real_vals, sampled_table, sample_sql_used = sample_mssql_params(
            ms_conn, schema, func_name, proc_def, in_params
        )
        if real_vals is not None:
            arg_str = ', '.join(str(v) if v is not None else 'NULL' for v in real_vals)
            param_source = 'sampled_from:%s' % (sampled_table or 'unknown')
        else:
            arg_str = ', '.join(['NULL'] * len(in_params))
            param_source = 'null_literals'
    else:
        arg_str = ', '.join(['NULL'] * len(in_params))
        param_source = 'null_literals'

    # Build call string
    # For scalar functions: SELECT schema.fn(args) AS result
    # For TVFs: SELECT * FROM schema.fn(args)
    if obj_type == 'FN':
        call_str = "SELECT %s.%s(%s) AS result" % (schema, func_name, arg_str)
    else:
        call_str = "SELECT * FROM %s.%s(%s)" % (schema, func_name, arg_str)

    try:
        cur.execute(call_str)
        result_sets = []
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [serialize_row(r) for r in cur.fetchall()]
            result_sets.append({'columns': cols, 'rows': rows, 'row_count': len(rows)})
        conn.close()
        total_rows = sum(rs['row_count'] for rs in result_sets)
        return {'status': 'SUCCESS', 'call_string': call_str,
                'params_used': [arg_str], 'param_source': param_source,
                'sampled_table': sampled_table, 'sample_query': sample_sql_used,
                'result_sets': result_sets,
                'total_result_sets': len(result_sets),
                'total_rows': total_rows, 'error': None}
    except Exception as e:
        conn.close()
        return {'status': 'ERROR', 'call_string': call_str,
                'params_used': [], 'param_source': param_source,
                'sampled_table': sampled_table, 'sample_query': sample_sql_used,
                'result_sets': [], 'total_result_sets': 0, 'total_rows': 0, 'error': str(e)}


def exec_proc(schema, proc_name, override_params, param_info, proc_def=''):
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor()

    in_params    = [p for p in param_info if not p['is_output']]
    param_source = 'typed_nulls'
    sampled_table = sample_sql_used = None

    if override_params is not None:
        ph = ', '.join(['%s'] * len(override_params))
        call_str = "EXEC %s.%s %s" % (schema, proc_name, ph)
        param_source = 'configured'
        try:
            cur.execute(call_str, override_params)
        except Exception as e:
            conn.close()
            return {'status': 'ERROR', 'call_string': call_str,
                    'params_used': override_params, 'param_source': param_source,
                    'result_sets': [], 'total_result_sets': 0, 'total_rows': 0, 'error': str(e)}
        params_used = override_params

    else:
        # Check for UDTT params first
        udtt = [p for p in in_params if p['type'].lower() in UDTT_TYPES]
        if udtt:
            conn.close()
            return {'status': 'UNTESTABLE',
                    'call_string': 'EXEC %s.%s <udtt>' % (schema, proc_name),
                    'params_used': [], 'param_source': 'udtt',
                    'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                    'error': 'User-defined table type: %s' % ', '.join(p['name'] for p in udtt)}

        # Try real data sampling
        real_vals, sampled_table, sample_sql_used = sample_mssql_params(
            ms_conn, schema, proc_name, proc_def, in_params
        )

        if real_vals is not None:
            exec_parts = ['@%s=%%s' % p['name'] for p in in_params]
            call_str   = "EXEC %s.%s %s" % (schema, proc_name, ', '.join(exec_parts))
            param_source  = 'sampled_from:%s' % (sampled_table or 'unknown')
            params_used   = real_vals
            try:
                cur.execute(call_str, real_vals)
            except Exception:
                real_vals = None  # fall through to typed nulls

        if real_vals is None:
            # Typed DECLARE fallback
            declare_parts, exec_parts, untestable = [], [], []
            for i, p in enumerate(in_params):
                type_str = mssql_type_str(p['type'])
                if type_str is None:
                    untestable.append('%s (%s)' % (p['name'], p['type']))
                else:
                    var = '@_p%d' % i
                    declare_parts.append('%s %s = NULL' % (var, type_str))
                    exec_parts.append('@%s=%s' % (p['name'], var))

            if untestable:
                conn.close()
                return {'status': 'UNTESTABLE',
                        'call_string': 'EXEC %s.%s <udtt>' % (schema, proc_name),
                        'params_used': [], 'param_source': 'udtt',
                        'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                        'error': 'UDTT params: %s' % ', '.join(untestable)}

            call_str = ''
            if declare_parts:
                call_str = 'DECLARE ' + ', '.join(declare_parts) + '; '
            call_str += 'EXEC %s.%s' % (schema, proc_name)
            if exec_parts:
                call_str += ' ' + ', '.join(exec_parts)
            param_source = 'typed_nulls_fallback'
            params_used  = ['<typed-null-vars>']
            try:
                cur.execute(call_str)
            except Exception as e:
                conn.close()
                return {'status': 'ERROR', 'call_string': call_str,
                        'params_used': params_used, 'param_source': param_source,
                        'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                        'error': str(e)}

    # Collect result sets
    result_sets = []
    fetch_error = None
    while True:
        if cur.description:
            cols = [d[0] for d in cur.description]
            try:
                rows = [serialize_row(r) for r in cur.fetchall()]
            except Exception as fe:
                fetch_error = str(fe)
                break
            result_sets.append({'columns': cols, 'rows': rows, 'row_count': len(rows)})
        try:
            if not cur.nextset(): break
        except: break

    conn.close()
    if fetch_error:
        return {'status': 'ERROR', 'call_string': call_str,
                'params_used': params_used, 'param_source': param_source,
                'result_sets': result_sets, 'total_result_sets': len(result_sets),
                'total_rows': sum(r['row_count'] for r in result_sets),
                'error': fetch_error}
    return {'status': 'SUCCESS', 'call_string': call_str,
            'params_used': params_used, 'param_source': param_source,
            'sampled_table': sampled_table, 'sample_query': sample_sql_used,
            'result_sets': result_sets,
            'total_result_sets': len(result_sets),
            'total_rows': sum(r['row_count'] for r in result_sets),
            'error': None}

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    records = []
    success = errors = skipped = untestable = 0

    # Load seed profiles for prerequisite gating
    profiles  = load_seed_profiles()
    scope_map = build_scope_map(profiles)
    if scope_map:
        print("Seed profiles loaded: %d scenarios, %d in-scope objects" % (
              len(profiles), len(scope_map)))
    else:
        print("No seed profiles found at %s — running without prereq gating" % SEED_PROFILES_PATH)

    schemas = discover_schemas()
    print("Schemas found: %s" % schemas)

    for schema in schemas:
        print("Discovering %s ..." % schema)
        try:
            proc_info = get_all_procs(schema)
        except Exception as e:
            print("  ERROR: %s" % e)
            continue

        print("  %d procedures/functions" % len(proc_info))
        for key, info in sorted(proc_info.items()):
            name     = info['name']
            obj_kind = info.get('obj_kind', 'PROCEDURE')
            obj_type = info.get('obj_type', 'P')
            full_key = '%s.%s' % (schema, key)

            if should_skip(key):
                records.append({'schema': schema, 'procedure_name': name,
                                 'full_name': full_key,
                                 'obj_kind': obj_kind,
                                 'params_schema': [p['name'] + ' ' + p['type'] for p in info['params']],
                                 'params_used': [], 'status': 'SKIPPED',
                                 'skip_reason': 'Write/modify procedure',
                                 'call_string': 'SKIPPED', 'result_sets': [],
                                 'total_result_sets': 0, 'total_rows': 0, 'error': None,
                                 'executed_at': datetime.datetime.now(datetime.UTC).isoformat()})
                skipped += 1
                print("  SKIP  %s" % full_key)
                continue

            # ── Seed profile prereq gate ─────────────────────────────────
            seed_scenario = scope_map.get(full_key.lower())
            override_params = None
            if seed_scenario:
                profile = profiles[seed_scenario]
                prereq_sqls = profile.get('prereq_mssql_sql', [])
                if prereq_sqls:
                    prereq_ok, prereq_err = apply_prereq_sql(prereq_sqls, seed_scenario)
                    if not prereq_ok:
                        result = {'status': 'ERROR', 'call_string': 'PREREQ_SETUP_FAILED',
                                  'params_used': [], 'param_source': 'prereq_failed',
                                  'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                                  'error': prereq_err}
                        errors += 1
                        print("  PREREQ_FAIL %-45s %s" % (full_key, prereq_err[:60]))
                        records.append({'schema': schema, 'procedure_name': name,
                                         'full_name': full_key, 'obj_kind': obj_kind,
                                         'params_schema': [], 'params_used': [],
                                         'param_source': 'prereq_failed', 'status': 'ERROR',
                                         'skip_reason': None, 'call_string': 'PREREQ_SETUP_FAILED',
                                         'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                                         'error': prereq_err,
                                         'executed_at': datetime.datetime.now(datetime.UTC).isoformat()})
                        continue
                    # Readiness check
                    ready_ok, ready_fail = check_readiness(
                        profile.get('readiness_checks_mssql', []), seed_scenario)
                    if not ready_ok:
                        err_msg = 'Readiness check failed [%s]: %s' % (seed_scenario, ready_fail)
                        result = {'status': 'ERROR', 'call_string': 'READINESS_CHECK_FAILED',
                                  'params_used': [], 'param_source': 'readiness_failed',
                                  'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                                  'error': err_msg}
                        errors += 1
                        print("  READY_FAIL  %-45s %s" % (full_key, err_msg[:60]))
                        records.append({'schema': schema, 'procedure_name': name,
                                         'full_name': full_key, 'obj_kind': obj_kind,
                                         'params_schema': [], 'params_used': [],
                                         'param_source': 'readiness_failed', 'status': 'ERROR',
                                         'skip_reason': None, 'call_string': 'READINESS_CHECK_FAILED',
                                         'result_sets': [], 'total_result_sets': 0, 'total_rows': 0,
                                         'error': err_msg,
                                         'executed_at': datetime.datetime.now(datetime.UTC).isoformat()})
                        continue
                # Use parameter_bindings from profile if present
                bindings = profile.get('parameter_bindings', {})
                if bindings and info['params']:
                    bound = [bindings.get(p['name']) or bindings.get('@' + p['name'])
                             for p in info['params'] if not p.get('is_output')]
                    if all(v is not None for v in bound):
                        override_params = bound

            # ── Dynamic prereq guard ─────────────────────────────────────
            # Scan the proc body for known patterns and restore required state
            # before every execution. Idempotent — safe to run on every call.
            # If restore fails after retry, classify as FAIL_MISSING_PREREQ
            # and skip execution (per skill spec — not a converter defect).
            _guard_failed = False
            try:
                from prereq_guard import detect_mssql_prereqs, restore_mssql_prereqs, PrereqRestoreError
                prereqs = detect_mssql_prereqs(info.get('proc_def', ''))
                if prereqs:
                    restore_mssql_prereqs(prereqs)
                    print("  [GUARD] %-50s %s" % (full_key, ','.join(prereqs)))
            except PrereqRestoreError as _guard_err:
                # Guard ran but couldn't restore required state — environment issue
                _guard_failed = True
                print("  FAIL_PREREQ prereq_guard MSSQL [%s]: %s" % (full_key, str(_guard_err)[:150]))
                records.append({
                    'schema': schema, 'procedure_name': name,
                    'full_name': full_key, 'obj_kind': obj_kind,
                    'params_schema': [], 'params_used': [],
                    'param_source': '', 'result_sets': [],
                    'row_count': 0, 'status': 'FAIL_MISSING_PREREQ',
                    'error': f'prereq_guard failed: {str(_guard_err)[:300]}',
                    'strategy': 'prereq_guard',
                    'prereq_guard_error_type': 'missing_prereq',
                })
                continue  # do not execute the procedure
            except Exception as _guard_err:
                # Guard itself crashed — bug in test infrastructure
                _guard_failed = True
                print("  FAIL_HARNESS prereq_guard MSSQL [%s]: %s" % (full_key, str(_guard_err)[:150]))
                records.append({
                    'schema': schema, 'procedure_name': name,
                    'full_name': full_key, 'obj_kind': obj_kind,
                    'params_schema': [], 'params_used': [],
                    'param_source': '', 'result_sets': [],
                    'row_count': 0, 'status': 'FAIL_HARNESS',
                    'error': f'prereq_guard harness error: {str(_guard_err)[:300]}',
                    'strategy': 'prereq_guard',
                    'prereq_guard_error_type': 'harness_error',
                })
                continue  # do not execute the procedure

            # Functions use SELECT-based execution; procedures use EXEC
            # Both now support override_params from seed profile parameter_bindings.
            if obj_kind == 'FUNCTION':
                result = exec_func(schema, name, info['params'], obj_type=obj_type,
                                   override_params=override_params, proc_def=info.get('proc_def', ''))
            else:
                result = exec_proc(schema, name, override_params, info['params'], info.get('proc_def', ''))

            records.append({'schema': schema, 'procedure_name': name,
                             'full_name': full_key,
                             'obj_kind': obj_kind,
                             'params_schema': ['%s %s%s' % (
                                 p['name'], p['type'], ' OUTPUT' if p['is_output'] else '')
                                 for p in info['params']],
                             'params_used':   result['params_used'],
                             'param_source':  result.get('param_source', ''),
                             'sampled_table': result.get('sampled_table'),
                             'sample_query':  result.get('sample_query'),
                             'status':        result['status'],
                             'skip_reason':   None,
                             'call_string':   result['call_string'],
                             'result_sets':   result['result_sets'],
                             'total_result_sets': result['total_result_sets'],
                             'total_rows':    result['total_rows'],
                             'error':         result['error'],
                             'executed_at':   datetime.datetime.now(datetime.UTC).isoformat()})

            if result['status'] == 'SUCCESS':
                success += 1
                print("  OK    %-55s rs=%d rows=%d" % (full_key, result['total_result_sets'], result['total_rows']))
            elif result['status'] == 'UNTESTABLE':
                untestable += 1
                print("  SKIP  %-55s (udtt params)" % full_key)
            else:
                errors += 1
                print("  ERR   %-55s %s" % (full_key, str(result['error'])[:80]))

    # Write JSONL output
    def _default_serializer(obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        return str(obj)

    with open(MSSQL_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=_default_serializer) + '\n')

    # Save shared params (lowercase keys) for SPG executor to reuse.
    # Only save records where real parameter values were sampled —
    # skip the '<typed-null-vars>' sentinel (no real values to share).
    shared = {}
    for rec in records:
        if (rec.get('params_used')
                and rec.get('status') == 'SUCCESS'
                and rec['params_used'] != ['<typed-null-vars>']):
            key = '%s.%s' % (rec['schema'].lower(), rec['procedure_name'].lower())
            shared[key] = rec['params_used']
    with open(SHARED_PARAMS_FILE, 'w') as f:
        json.dump(shared, f, indent=2, default=str)
    print("Shared params saved: %s (%d entries)" % (SHARED_PARAMS_FILE, len(shared)))

    print("\n" + "=" * 65)
    print("MSSQL DONE  Success=%d  Errors=%d  Untestable=%d  Skipped=%d" % (
          success, errors, untestable, skipped))
    print("Output: %s" % MSSQL_OUTPUT_FILE)
    print("=" * 65)
    return records

if __name__ == '__main__':
    main()
