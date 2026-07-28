"""
spg_proc_executor.py — Execute Postgres procedures and functions (generic).

Dynamically discovers all business schemas. Reads shared params from the
MSSQL executor so both sides use identical input values for comparison.

Required env vars: SPG_HOST, SPG_USER, SPG_PASSWORD
Optional env vars: SPG_DATABASE, SPG_PORT, VALIDATION_OUTPUT_DIR,
                   VALIDATION_SKIP_WRITES, VALIDATION_WRITE_KEYWORDS
See config.py for full list.
"""
import os, sys, json, datetime, decimal, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (SPG_CONF, SPG_OUTPUT_FILE, SHARED_PARAMS_FILE,
                    SKIP_WRITE_PROCS, WRITE_KEYWORDS, OUTPUT_DIR,
                    is_spg_system_schema, check_required)
import psycopg2, psycopg2.extras, psycopg2.extensions
from param_discovery import sample_spg_params

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Seed profile prereq support ───────────────────────────────────────────────
SHARED_DIR         = os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', os.path.join(os.getcwd(), 'shared')))
SEED_PROFILES_PATH = os.path.join(SHARED_DIR, 'seed_profiles.json')

def _load_seed_profiles():
    if os.path.exists(SEED_PROFILES_PATH):
        try:
            with open(SEED_PROFILES_PATH) as f:
                d = json.load(f)
            # Handle both {scenario: ...} and {seed_profiles: {scenario: ...}} layouts
            return d.get('seed_profiles', d)
        except Exception as e:
            print("  WARN: could not load seed_profiles: %s" % e)
    return {}

def _build_spg_scope_map(profiles):
    """Return {schema.proc_name (lower) -> scenario_id}."""
    m = {}
    for scenario_id, profile in profiles.items():
        for obj in profile.get('object_scope', []):
            m[obj.lower()] = scenario_id
    return m

_SEED_PROFILES = _load_seed_profiles()
_SPG_SCOPE_MAP  = _build_spg_scope_map(_SEED_PROFILES)

def apply_spg_prereq(prereq_sqls, scenario_id):
    """Run prereq_spg_sql statements on a fresh SPG connection. Returns (ok, err)."""
    if not prereq_sqls:
        return True, None
    conn = psycopg2.connect(**SPG_CONF)
    conn.autocommit = True
    cur  = conn.cursor()
    for sql in prereq_sqls:
        sql = sql.strip()
        if not sql:
            continue
        try:
            cur.execute(sql)
        except Exception as e:
            try: conn.close()
            except: pass
            return False, 'spg_prereq[%s]: %s' % (scenario_id, str(e)[:150])
    conn.close()
    return True, None

# Load shared sampled params from MSSQL executor (same inputs on both sides)
if os.path.exists(SHARED_PARAMS_FILE):
    with open(SHARED_PARAMS_FILE) as _f:
        KNOWN_PARAMS = json.load(_f)
    print("Loaded %d shared param sets from MSSQL executor" % len(KNOWN_PARAMS))
else:
    KNOWN_PARAMS = {}
    print("No shared params file found — using typed NULLs (run MSSQL executor first)")

# ── Type mapping ──────────────────────────────────────────────────────────────
PG_TYPE_CAST = {
    'integer': 'integer', 'bigint': 'bigint', 'smallint': 'smallint',
    'character varying': 'text', 'text': 'text', 'character': 'text',
    'varchar': 'text', 'name': 'name',
    'timestamp without time zone': 'timestamp', 'timestamp with time zone': 'timestamptz',
    'timestamp': 'timestamp', 'date': 'date',
    'time without time zone': 'time', 'time': 'time',
    'numeric': 'numeric', 'decimal': 'numeric', 'real': 'real',
    'double precision': 'double precision', 'money': 'numeric',
    'boolean': 'boolean', 'bit': 'boolean',
    'bytea': 'bytea', 'json': 'json', 'jsonb': 'jsonb', 'uuid': 'uuid',
    'array': 'text[]', 'ARRAY': 'text[]',
}

MSSQL_TO_PG = {
    'int': 'integer', 'bigint': 'bigint', 'smallint': 'smallint', 'tinyint': 'smallint',
    'varchar': 'text', 'nvarchar': 'text', 'char': 'text', 'nchar': 'text', 'text': 'text',
    'datetime': 'timestamp', 'datetime2': 'timestamp', 'smalldatetime': 'timestamp',
    'date': 'date', 'bit': 'boolean', 'decimal': 'numeric', 'numeric': 'numeric',
    'float': 'double precision', 'real': 'real', 'money': 'numeric',
    'uniqueidentifier': 'uuid', 'varbinary': 'bytea', 'xml': 'text',
}

def pg_null_expr(type_name):
    t = (type_name or '').lower().strip()
    cast = PG_TYPE_CAST.get(t) or PG_TYPE_CAST.get(type_name or '')
    if not cast:
        cast = MSSQL_TO_PG.get(t)
    if not cast:
        for k, v in PG_TYPE_CAST.items():
            if t.startswith(k.lower()):
                cast = v; break
    return ('NULL::%s' % cast) if cast else 'NULL'

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
    if isinstance(v, (list, dict)): return v
    return str(v)

def serialize_row(row): return [serialize_val(v) for v in row]
def spg_conn():         return psycopg2.connect(**SPG_CONF)

def should_skip(name):
    if not SKIP_WRITE_PROCS:
        return False
    return any(kw in name.lower() for kw in WRITE_KEYWORDS)

def real_vals_display(params, sampled_table):
    return ['<sampled from %s>' % sampled_table] if sampled_table else ['<typed-nulls>']

# ── Schema discovery ──────────────────────────────────────────────────────────
def discover_schemas():
    """Return all business schemas that contain procedures or functions."""
    conn = spg_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT DISTINCT n.nspname
        FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
        ORDER BY n.nspname
    """)
    schemas = [r[0] for r in cur.fetchall()
               if not is_spg_system_schema(r[0])]
    conn.close()
    return schemas

# ── Proc discovery ────────────────────────────────────────────────────────────
def get_all_procs(schema):
    conn = spg_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT p.proname AS proc_name,
               CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS kind,
               pg_get_functiondef(p.oid) AS proc_def
        FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid
        WHERE n.nspname=%s ORDER BY p.proname
    """, (schema,))
    procs = cur.fetchall()

    result = {}
    for proc in procs:
        cur.execute("""
            SELECT pa.parameter_name, pa.data_type, pa.parameter_mode, pa.ordinal_position
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name=pa.specific_name
            WHERE r.routine_schema=%s AND LOWER(r.routine_name)=%s
            ORDER BY pa.ordinal_position
        """, (schema, proc['proc_name'].lower()))
        params = cur.fetchall()
        result[proc['proc_name'].lower()] = {
            'name':     proc['proc_name'],
            'kind':     proc['kind'],
            'proc_def': proc['proc_def'] or '',
            'params':   [{'name': r['parameter_name'] or ('p%d' % i),
                          'type': r['data_type'] or 'text',
                          'mode': r['parameter_mode'] or 'IN'}
                         for i, r in enumerate(params)]
        }
    conn.close()
    return result

# ── Execution strategies ──────────────────────────────────────────────────────
def build_typed_null_args(params):
    exprs = []
    for p in params:
        if p.get('mode', 'IN') in ('IN', 'INOUT'):
            pname = (p['name'] or '').strip()
            exprs.append(('%s => %s' % (pname, pg_null_expr(p['type']))) if pname
                         else pg_null_expr(p['type']))
    return ', '.join(exprs) if exprs else ''

def exec_as_function(schema, name, params, real_vals=None):
    conn = spg_conn()
    conn.autocommit = True
    cur  = conn.cursor()
    sql  = ''
    try:
        in_p = [p for p in params if p.get('mode','IN') in ('IN','INOUT')]
        if real_vals is not None:
            named = ', '.join(['%s => %%s' % p['name'] for p in in_p])
            sql = 'SELECT * FROM %s."%s"(%s)' % (schema, name, named)
            cur.execute(sql, real_vals)
        else:
            sql = 'SELECT * FROM %s."%s"(%s)' % (schema, name, build_typed_null_args(in_p))
            cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [serialize_row(r) for r in cur.fetchall()]
        conn.close()
        return 'SUCCESS', sql, cols, rows, None
    except Exception as e:
        try: conn.close()
        except: pass
        return 'ERROR', sql, [], [], str(e)

def exec_as_call(schema, name, params, real_vals=None):
    in_p = [p for p in params if p.get('mode','IN') in ('IN','INOUT')]
    if real_vals is not None:
        sql  = 'CALL %s."%s"(%s)' % (schema, name, ', '.join(['%s => %%s' % p['name'] for p in in_p]))
        bind = real_vals
    else:
        sql  = 'CALL %s."%s"(%s)' % (schema, name, build_typed_null_args(in_p))
        bind = None
    try:
        conn = spg_conn()
        conn.autocommit = True   # allows COMMIT/ROLLBACK inside proc
        cur  = conn.cursor()
        cur.execute(sql, bind) if bind is not None else cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [serialize_row(r) for r in cur.fetchall()] if cols else []
        conn.close()
        return 'SUCCESS', sql, cols, rows, None
    except Exception as e:
        try: conn.close()
        except: pass
        return 'ERROR', sql, [], [], str(e)

def extract_select(proc_def):
    body_m = re.search(r'\$\$(.*?)\$\$', proc_def, re.DOTALL)
    if not body_m: return None
    body = body_m.group(1)
    for sel in re.findall(r'(SELECT\s+(?!INTO\s+\w).*?);', body, re.DOTALL | re.IGNORECASE):
        sel = sel.strip()
        if re.search(r'SELECT\s+@\w+\s*=', sel, re.IGNORECASE): continue
        if len(sel) > 20: return sel
    return None

def exec_extracted_select(schema, name, proc_def):
    sel = extract_select(proc_def)
    if not sel:
        return 'NOT_EXTRACTABLE', '', [], [], 'No extractable SELECT in proc body'
    conn = spg_conn(); cur = conn.cursor()
    try:
        cur.execute(sel)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [serialize_row(r) for r in cur.fetchall()]
        conn.close()
        return 'SUCCESS', '-- extracted:\n' + sel[:300], cols, rows, None
    except Exception as e:
        try: conn.rollback(); conn.close()
        except: pass
        return 'ERROR_EXTRACTED', sel[:100], [], [], str(e)

def exec_proc(schema, name, kind, params, proc_def, override=None):
    in_params = [p for p in params if p.get('mode', 'IN') in ('IN', 'INOUT')]

    real_vals = sampled_table = sample_sql = None
    if override is not None:
        real_vals    = override
        param_source = 'configured'
    else:
        real_vals, sampled_table, sample_sql = sample_spg_params(
            spg_conn, schema, name, proc_def, in_params)
        param_source = ('sampled_from:%s' % sampled_table) if real_vals is not None else 'typed_nulls_fallback'

    if kind == 'FUNCTION':
        # Use MSSQL-sampled override values when available and arity matches.
        # Fall back to typed NULLs only when there are no override params
        # (SPG-local sampling can return fewer values than the function expects,
        # causing list-index-out-of-range).
        use_vals = (real_vals if override is not None
                    and len(real_vals or []) == len(in_params) else None)
        s, sql, cols, rows, err = exec_as_function(schema, name, in_params, real_vals=use_vals)
        return s, sql, cols, rows, err, 'function_select', param_source, sampled_table

    s, sql, cols, rows, err = exec_as_call(schema, name, in_params, real_vals)
    if s == 'SUCCESS' and cols:
        return s, sql, cols, rows, err, 'call_real_data' if real_vals else 'call_typed_nulls', param_source, sampled_table
    if s == 'SUCCESS' and not cols:
        s2, sql2, cols2, rows2, err2 = exec_as_function(schema, name, in_params, real_vals)
        if s2 == 'SUCCESS':
            return s2, sql2, cols2, rows2, err2, 'select_from_function', param_source, sampled_table
        s3, sql3, cols3, rows3, err3 = exec_extracted_select(schema, name, proc_def)
        if s3 == 'SUCCESS':
            return 'SUCCESS', sql3, cols3, rows3, None, 'extracted_select', param_source, sampled_table
        return s, sql, cols, rows, err, 'call_no_resultset', param_source, sampled_table

    if err and 'no destination for result data' in str(err):
        s2, sql2, cols2, rows2, err2 = exec_as_function(schema, name, in_params, real_vals)
        if s2 == 'SUCCESS':
            return s2, sql2, cols2, rows2, err2, 'select_from_function', param_source, sampled_table
        s3, sql3, cols3, rows3, err3 = exec_extracted_select(schema, name, proc_def)
        if s3 == 'SUCCESS':
            return 'SUCCESS', sql3, cols3, rows3, None, 'extracted_select', param_source, sampled_table
        return 'NOT_TESTABLE', sql, [], [], \
               'PROC_CANNOT_RETURN_RESULTSET: %s' % str(err)[:100], 'all_failed', param_source, sampled_table

    return s, sql, cols, rows, err, 'call_failed', param_source, sampled_table

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    records = []
    success = errors = skipped = not_test = 0

    schemas = discover_schemas()
    print("Schemas found: %s" % schemas)

    for schema in schemas:
        print("Discovering %s ..." % schema)
        try:
            proc_info = get_all_procs(schema)
        except Exception as e:
            print("  ERROR: %s" % e); continue
        print("  %d objects" % len(proc_info))

        for key, info in sorted(proc_info.items()):
            name     = info['name']
            full_key = '%s.%s' % (schema, key)

            if should_skip(key):
                records.append({'schema': schema, 'procedure_name': name,
                                 'full_name': full_key, 'object_kind': info['kind'],
                                 'params_schema': [], 'params_used': [],
                                 'status': 'SKIPPED', 'skip_reason': 'Write/modify',
                                 'strategy_used': None, 'call_string': 'SKIPPED',
                                 'result_sets': [], 'total_result_sets': 0,
                                 'total_rows': 0, 'error': None,
                                 'executed_at': datetime.datetime.now(datetime.UTC).isoformat()})
                skipped += 1
                print("  SKIP  %s" % full_key); continue

            in_params = [p for p in info['params'] if p['mode'] in ('IN', 'INOUT')]
            override  = KNOWN_PARAMS.get(full_key)
            # Discard the sentinel — treat it as no real values available
            if override == ['<typed-null-vars>']:
                override = None

            # Apply prereq_spg_sql if this proc is in a seed profile scope
            seed_scenario = _SPG_SCOPE_MAP.get(full_key.lower())
            if seed_scenario:
                profile     = _SEED_PROFILES.get(seed_scenario, {})
                prereq_sqls = profile.get('prereq_spg_sql', [])
                if prereq_sqls:
                    prereq_ok, prereq_err = apply_spg_prereq(prereq_sqls, seed_scenario)
                    if not prereq_ok:
                        print("  WARN  %-52s spg prereq failed: %s" % (full_key, str(prereq_err)[:80]))

            # ── Dynamic prereq guard ─────────────────────────────────────
            # Scan SPG proc body for known patterns and restore state before
            # every call. Idempotent — safe to run on every invocation.
            # If restore fails after retry, classify as FAIL_MISSING_PREREQ
            # and skip execution (per skill spec — not a converter defect).
            try:
                from prereq_guard import detect_spg_prereqs, restore_spg_prereqs, PrereqRestoreError
                prereqs = detect_spg_prereqs(info.get('proc_def', ''))
                if prereqs:
                    restore_spg_prereqs(prereqs)
                    print("  [GUARD] %-50s %s" % (full_key, ','.join(prereqs)))
            except PrereqRestoreError as _guard_err:
                # Guard ran but couldn't restore required state — environment issue
                print("  FAIL_PREREQ prereq_guard SPG [%s]: %s" % (full_key, str(_guard_err)[:150]))
                records.append({
                    'schema': schema, 'procedure_name': name,
                    'full_name': full_key, 'object_kind': info.get('kind', 'PROCEDURE'),
                    'params_schema': [], 'params_used': [], 'result_sets': [],
                    'row_count': 0, 'status': 'FAIL_MISSING_PREREQ',
                    'error': f'prereq_guard failed: {str(_guard_err)[:300]}',
                    'strategy': 'prereq_guard',
                    'prereq_guard_error_type': 'missing_prereq',
                })
                continue  # do not execute the procedure
            except Exception as _guard_err:
                # Guard itself crashed — bug in test infrastructure
                print("  FAIL_HARNESS prereq_guard SPG [%s]: %s" % (full_key, str(_guard_err)[:150]))
                records.append({
                    'schema': schema, 'procedure_name': name,
                    'full_name': full_key, 'object_kind': info.get('kind', 'PROCEDURE'),
                    'params_schema': [], 'params_used': [], 'result_sets': [],
                    'row_count': 0, 'status': 'FAIL_HARNESS',
                    'error': f'prereq_guard harness error: {str(_guard_err)[:300]}',
                    'strategy': 'prereq_guard',
                    'prereq_guard_error_type': 'harness_error',
                })
                continue  # do not execute the procedure

            s, sql, cols, rows, err, strategy, param_source, sampled_table = exec_proc(
                schema, name, info['kind'], in_params, info['proc_def'], override)

            rs = [{'columns': cols, 'rows': rows, 'row_count': len(rows)}] if cols else []
            records.append({'schema': schema, 'procedure_name': name,
                             'full_name': full_key, 'object_kind': info['kind'],
                             'params_schema': ['%s %s (%s)' % (
                                 p['name'], p['type'], p['mode']) for p in info['params']],
                             'params_used':   override or real_vals_display(in_params, sampled_table),
                             'param_source':  param_source,
                             'sampled_table': sampled_table,
                             'status': s, 'skip_reason': None,
                             'strategy_used': strategy, 'call_string': sql,
                             'result_sets': rs, 'total_result_sets': len(rs),
                             'total_rows': sum(r['row_count'] for r in rs),
                             'error': err,
                             'executed_at': datetime.datetime.now(datetime.UTC).isoformat()})

            if s == 'SUCCESS':
                success += 1
                print("  OK    %-52s strategy=%-22s rows=%d" % (full_key, strategy, sum(r['row_count'] for r in rs)))
            elif s == 'NOT_TESTABLE':
                not_test += 1
                print("  SKIP  %-52s %s" % (full_key, str(err)[:60]))
            else:
                errors += 1
                print("  ERR   %-52s %s" % (full_key, str(err)[:70]))

    with open(SPG_OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')

    print("\n" + "=" * 65)
    print("SPG DONE  Success=%d  Errors=%d  NotTestable=%d  Skipped=%d" % (
          success, errors, not_test, skipped))
    print("Output: %s" % SPG_OUTPUT_FILE)
    print("=" * 65)
    return records

if __name__ == '__main__':
    main()
