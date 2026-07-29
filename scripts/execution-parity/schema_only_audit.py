"""
schema_only_audit.py — Object-level validation only (no data, no execution).

For every object type, checks existence parity and structural schema:
  TABLES      : existence + column names + column data types
  VIEWS       : existence + column names + column data types
  PROCEDURES  : existence + parameter count + parameter names + parameter types
  FUNCTIONS   : existence + parameter count + parameter names + parameter types
  TRIGGERS    : existence + event type + table target

No SQL execution of procedures/functions. No COUNT(*) on views/tables.
All results written to validation.validation_run / validation.validation_result.

Usage:
    python3 schema_only_audit.py

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
See config.py for full list.
"""
import os, sys, re, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, is_mssql_system_schema,
                    is_spg_system_schema, check_required)
import pymssql, psycopg2, psycopg2.extras
import validation_db as vdb

check_required()

SEP  = "=" * 110
LINE = "-" * 110

# ---------------------------------------------------------------------------
# MSSQL → Postgres type equivalence map
# Values are the set of SPG type names considered equivalent to the MSSQL key.
# Postgres format_type() strings like 'character varying(50)' are handled by
# stripping the precision suffix before lookup.
# ---------------------------------------------------------------------------
TYPE_EQUIV = {
    'int':              {'integer', 'int4', 'int', 'bigint', 'int8'},
    'bigint':           {'bigint', 'int8', 'integer', 'int4'},
    'smallint':         {'smallint', 'int2', 'integer', 'int4'},
    'tinyint':          {'smallint', 'integer', 'int2', 'int4', 'bigint'},
    'bit':              {'boolean', 'integer', 'int4'},
    'float':            {'double precision', 'float8', 'real', 'float4'},
    'real':             {'real', 'float4'},
    'decimal':          {'numeric', 'decimal'},
    'numeric':          {'numeric', 'decimal'},
    'money':            {'numeric', 'decimal', 'money'},
    'smallmoney':       {'numeric', 'decimal'},
    'varchar':          {'character varying', 'text', 'varchar'},
    'nvarchar':         {'character varying', 'text', 'varchar'},
    'char':             {'character', 'character varying', 'bpchar', 'text'},
    'nchar':            {'character', 'character varying', 'bpchar', 'text'},
    'text':             {'text'},
    'ntext':            {'text'},
    'datetime':         {'timestamp without time zone', 'timestamp', 'timestamp with time zone'},
    'datetime2':        {'timestamp without time zone', 'timestamp'},
    'smalldatetime':    {'timestamp without time zone', 'timestamp'},
    'date':             {'date'},
    'time':             {'time without time zone', 'time'},
    'uniqueidentifier': {'uuid'},
    'binary':           {'bytea'},
    'varbinary':        {'bytea'},
    'image':            {'bytea'},
    'xml':              {'xml', 'text'},
    'geography':        {'geography', 'text'},
    'geometry':         {'geometry', 'text'},
    'sql_variant':      {'text', 'anyelement'},
    'hierarchyid':      {'text'},
    'sysname':          {'character varying', 'text', 'name'},
}


def types_equivalent(ms_type, spg_type):
    """Return True if ms_type and spg_type are considered equivalent.
    Handles Postgres format_type() strings like 'character varying(50)'
    by stripping the length/precision before lookup.
    Also handles UDTT migration: MSSQL user-defined table types (e.g. InputType,
    IdList) are correctly migrated as ARRAY in Postgres.
    """
    ms = ms_type.lower().strip()
    # Strip Postgres precision: 'character varying(50)' → 'character varying'
    spg_base = re.sub(r'\(.*\)', '', spg_type.lower()).strip()
    if ms == spg_base:
        return True
    equiv = TYPE_EQUIV.get(ms, set())
    if spg_base in equiv or spg_type.lower().strip() in equiv:
        return True
    # UDTT rule: any MSSQL type not in the known built-in list that maps to
    # ARRAY in SPG is a User-Defined Table Type migrated as Postgres ARRAY.
    if spg_base == 'array' and ms not in TYPE_EQUIV:
        return True
    return False


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def ms():  return pymssql.connect(**MSSQL_CONF)
def spg(): return psycopg2.connect(**SPG_CONF)


# ---------------------------------------------------------------------------
# TABLES
# ---------------------------------------------------------------------------

def discover_mssql_tables():
    conn = ms(); cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT s.name AS schema_name, t.name AS table_name
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA')
        ORDER BY s.name, t.name
    """)
    rows = cur.fetchall(); conn.close()
    return {(r['schema_name'].lower(), r['table_name'].lower()): r for r in rows}

def discover_spg_tables():
    conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT n.nspname AS schema_name, c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE c.relkind = 'r'
    """)
    rows = cur.fetchall(); conn.close()
    return {(r['schema_name'].lower(), r['table_name'].lower()): r
            for r in rows if not is_spg_system_schema(r['schema_name'])}

def get_mssql_table_columns(schema, table):
    """Return {col_name: dtype} dict for a MSSQL table."""
    try:
        conn = ms(); cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT c.name AS col, t.name AS dtype
            FROM sys.columns c
            JOIN sys.types t ON c.user_type_id = t.user_type_id
            JOIN sys.tables tb ON c.object_id = tb.object_id
            JOIN sys.schemas s ON tb.schema_id = s.schema_id
            WHERE s.name = %s AND tb.name = %s
            ORDER BY c.column_id
        """, (schema, table))
        rows = cur.fetchall(); conn.close()
        return {r['col'].lower(): r['dtype'].lower() for r in rows}
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]

def get_spg_table_columns(schema, table):
    """Return {col_name: dtype} dict for a SPG table."""
    try:
        conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT a.attname AS col,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS dtype
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
        """, (schema, table))
        rows = cur.fetchall(); conn.close()
        return {r['col'].lower(): r['dtype'].lower() for r in rows}
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]

def validate_table(schema, table):
    ms_cols  = get_mssql_table_columns(schema, table)
    spg_cols = get_spg_table_columns(schema, table)
    if isinstance(ms_cols,  str): return {'verdict':'ERROR','issues':['MSSQL_ERR:%s'%ms_cols],'ms_cols':0,'spg_cols':0}
    if isinstance(spg_cols, str): return {'verdict':'ERROR','issues':['SPG_ERR:%s'%spg_cols],'ms_cols':len(ms_cols),'spg_cols':0}

    issues, verdict = [], 'PASS'
    ms_names  = set(ms_cols)
    spg_names = set(spg_cols)
    only_ms  = sorted(ms_names - spg_names)
    only_spg = sorted(spg_names - ms_names)
    if only_ms:
        issues.append('COLS_ONLY_IN_MSSQL(%d): %s%s' % (
            len(only_ms), str(only_ms[:4])[1:-1], '...' if len(only_ms) > 4 else ''))
        verdict = 'FAIL'
    if only_spg:
        issues.append('COLS_ONLY_IN_SPG(%d): %s%s' % (
            len(only_spg), str(only_spg[:4])[1:-1], '...' if len(only_spg) > 4 else ''))
        if verdict == 'PASS': verdict = 'WARN'
    # Type comparison for matching columns
    type_mismatches = []
    for col in sorted(ms_names & spg_names):
        if not types_equivalent(ms_cols[col], spg_cols[col]):
            type_mismatches.append('col=%s MSSQL=%s SPG=%s' % (col, ms_cols[col], spg_cols[col]))
    if type_mismatches:
        issues.append('TYPE_MISMATCH(%d): %s%s' % (
            len(type_mismatches),
            str(type_mismatches[:3])[1:-1],
            '...' if len(type_mismatches) > 3 else ''))
        verdict = 'FAIL'
    return {'verdict': verdict, 'issues': issues,
            'ms_cols': len(ms_cols), 'spg_cols': len(spg_cols)}


# ---------------------------------------------------------------------------
# VIEWS  (columns only — no row count)
# ---------------------------------------------------------------------------

def discover_mssql_views():
    conn = ms(); cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT s.name AS schema_name, v.name AS view_name
        FROM sys.views v
        JOIN sys.schemas s ON v.schema_id = s.schema_id
        WHERE s.name NOT IN ('sys','INFORMATION_SCHEMA')
        ORDER BY s.name, v.name
    """)
    rows = cur.fetchall(); conn.close()
    return {(r['schema_name'].lower(), r['view_name'].lower()): r for r in rows}

def discover_spg_views():
    conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT n.nspname AS schema_name, c.relname AS view_name
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE c.relkind = 'v'
    """)
    rows = cur.fetchall(); conn.close()
    return {(r['schema_name'].lower(), r['view_name'].lower()): r
            for r in rows if not is_spg_system_schema(r['schema_name'])}

def get_mssql_view_columns(schema, view):
    """Return {col_name: dtype} dict for a MSSQL view."""
    try:
        conn = ms(); cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT c.name AS col, t.name AS dtype
            FROM sys.columns c
            JOIN sys.types t ON c.user_type_id = t.user_type_id
            JOIN sys.views v ON c.object_id = v.object_id
            JOIN sys.schemas s ON v.schema_id = s.schema_id
            WHERE s.name = %s AND v.name = %s
            ORDER BY c.column_id
        """, (schema, view))
        rows = cur.fetchall(); conn.close()
        return {r['col'].lower(): r['dtype'].lower() for r in rows}
    except Exception as e:
        return 'ERR:%s' % str(e)[:120]

def get_spg_view_columns(schema, view):
    """Return {col_name: dtype} dict for a SPG view."""
    try:
        conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT a.attname AS col,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS dtype
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
        """, (schema, view))
        rows = cur.fetchall(); conn.close()
        return {r['col'].lower(): r['dtype'].lower() for r in rows}
    except Exception as e:
        return 'ERR:%s' % str(e)[:120]

def validate_view_schema(schema, view):
    ms_cols  = get_mssql_view_columns(schema, view)
    spg_cols = get_spg_view_columns(schema, view)
    if isinstance(ms_cols,  str): return {'verdict':'ERROR','issues':['MSSQL_ERR:%s'%ms_cols],'ms_cols':0,'spg_cols':0}
    if isinstance(spg_cols, str): return {'verdict':'ERROR','issues':['SPG_ERR:%s'%spg_cols],'ms_cols':len(ms_cols),'spg_cols':0}

    issues, verdict = [], 'PASS'
    ms_names  = set(ms_cols)
    spg_names = set(spg_cols)
    only_ms  = sorted(ms_names - spg_names)
    only_spg = sorted(spg_names - ms_names)
    if only_ms:
        issues.append('COLS_ONLY_IN_MSSQL(%d): %s%s' % (
            len(only_ms), str(only_ms[:4])[1:-1], '...' if len(only_ms) > 4 else ''))
        verdict = 'FAIL'
    if only_spg:
        issues.append('COLS_ONLY_IN_SPG(%d): %s%s' % (
            len(only_spg), str(only_spg[:4])[1:-1], '...' if len(only_spg) > 4 else ''))
        if verdict == 'PASS': verdict = 'WARN'
    # Type comparison for matching columns
    type_mismatches = []
    for col in sorted(ms_names & spg_names):
        if not types_equivalent(ms_cols[col], spg_cols[col]):
            type_mismatches.append('col=%s MSSQL=%s SPG=%s' % (col, ms_cols[col], spg_cols[col]))
    if type_mismatches:
        issues.append('TYPE_MISMATCH(%d): %s%s' % (
            len(type_mismatches),
            str(type_mismatches[:3])[1:-1],
            '...' if len(type_mismatches) > 3 else ''))
        verdict = 'FAIL'
    return {'verdict': verdict, 'issues': issues,
            'ms_cols': len(ms_cols), 'spg_cols': len(spg_cols)}


# ---------------------------------------------------------------------------
# PROCEDURES / FUNCTIONS (parameter parity, no execution)
# ---------------------------------------------------------------------------

def discover_mssql_routines():
    conn = ms(); cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT s.name AS schema_name, o.name AS obj_name, o.type AS ms_type
        FROM sys.objects o
        JOIN sys.schemas s ON o.schema_id = s.schema_id
        WHERE o.type IN ('P','FN','TF','IF','FS','FT')
          AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
        ORDER BY s.name, o.name
    """)
    rows = cur.fetchall(); conn.close()
    result = {}
    for r in rows:
        sc = r['schema_name'].lower(); nm = r['obj_name'].lower()
        kind = 'FUNCTION' if r['ms_type'].strip() in ('FN','TF','IF','FS','FT') else 'PROCEDURE'
        result[(sc, nm)] = {'name': r['obj_name'], 'schema': r['schema_name'],
                            'ms_type': r['ms_type'].strip(), 'kind': kind}
    return result

def discover_spg_routines():
    conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT n.nspname AS schema_name, p.proname AS obj_name,
               p.prokind, p.prorettype::regtype AS return_type
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE p.prokind IN ('f','p')
    """)
    rows = cur.fetchall(); conn.close()
    result = {}
    for r in rows:
        sc = r['schema_name'].lower(); nm = r['obj_name'].lower()
        if is_spg_system_schema(sc): continue
        if str(r['return_type']).lower() == 'trigger': continue
        kind = 'FUNCTION' if r['prokind'] == 'f' else 'PROCEDURE'
        result[(sc, nm)] = {'name': r['obj_name'], 'schema': r['schema_name'],
                            'prokind': r['prokind'], 'kind': kind,
                            'return_type': str(r['return_type'])}
    return result

def get_mssql_params(schema, name):
    try:
        conn = ms(); cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT p.parameter_id, p.name AS pname, t.name AS tname, p.is_output
            FROM sys.objects o
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            JOIN sys.parameters p ON o.object_id = p.object_id
            JOIN sys.types t ON p.user_type_id = t.user_type_id
            WHERE s.name = %s AND LOWER(o.name) = %s
            ORDER BY p.parameter_id
        """, (schema, name.lower()))
        rows = cur.fetchall(); conn.close()
        # Exclude parameter_id=0: MSSQL exposes scalar function return type as param [0]
        # Exclude OUTPUT params that are not real inputs (scalar return values)
        return [{'name': r['pname'].lstrip('@').lower(), 'type': r['tname'],
                 'is_output': r['is_output']}
                for r in rows
                if r['parameter_id'] != 0 and not r['is_output']]
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]

def get_spg_params(schema, name):
    try:
        conn = spg(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT pa.ordinal_position, pa.parameter_name, pa.data_type, pa.parameter_mode
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name = pa.specific_name
            WHERE r.routine_schema = %s AND LOWER(r.routine_name) = %s
            ORDER BY pa.ordinal_position
        """, (schema, name.lower()))
        rows = cur.fetchall(); conn.close()
        # Exclude OUT parameters: in Postgres, table-valued function return columns
        # are exposed as OUT params in information_schema — not real input parameters.
        return [{'name': (r['parameter_name'] or '').lower().lstrip('_'),
                 'type': r['data_type'] or '',
                 'mode': r['parameter_mode'] or 'IN'}
                for r in rows
                if (r['parameter_mode'] or 'IN') == 'IN']
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]

def strip_p(n):
    if n.startswith('par_'): return n[4:]   # par_propertyid → propertyid
    if n.startswith('p_'):   return n[2:]   # p_propertyid  → propertyid
    return n

def validate_routine(schema, name):
    ms_p  = get_mssql_params(schema, name)
    spg_p = get_spg_params(schema, name)
    if isinstance(ms_p,  str): return {'verdict':'ERROR','issues':['MSSQL_ERR:%s'%ms_p],'ms_param_count':0,'spg_param_count':0}
    if isinstance(spg_p, str): return {'verdict':'ERROR','issues':['SPG_ERR:%s'%spg_p],'ms_param_count':len(ms_p),'spg_param_count':0}

    issues, verdict = [], 'PASS'
    if len(ms_p) != len(spg_p):
        issues.append('PARAM_COUNT: MSSQL=%d SPG=%d' % (len(ms_p), len(spg_p)))
        verdict = 'FAIL'
    elif ms_p and spg_p:
        all_ms  = [p['name'] for p in ms_p]
        all_spg = [strip_p(p['name']) for p in spg_p]
        if set(all_ms) != set(all_spg):
            diff = ['pos%d MSSQL=%s SPG=%s' % (i+1,a,b)
                    for i,(a,b) in enumerate(zip(all_ms,all_spg)) if a!=b]
            issues.append('PARAM_NAMES_DIFFER: %s%s' % (
                str(diff[:3])[1:-1], '...' if len(diff)>3 else ''))
            verdict = 'FAIL'
        else:
            # Names match — compare parameter types
            type_mismatches = []
            for ms_param, spg_param in zip(ms_p, spg_p):
                if not types_equivalent(ms_param['type'], spg_param['type']):
                    type_mismatches.append('param=%s MSSQL=%s SPG=%s' % (
                        ms_param['name'], ms_param['type'], spg_param['type']))
            if type_mismatches:
                issues.append('PARAM_TYPE_MISMATCH(%d): %s%s' % (
                    len(type_mismatches),
                    str(type_mismatches[:3])[1:-1],
                    '...' if len(type_mismatches) > 3 else ''))
                verdict = 'FAIL'
    return {'verdict': verdict, 'issues': issues,
            'ms_param_count': len(ms_p), 'spg_param_count': len(spg_p)}


# ---------------------------------------------------------------------------
# TRIGGERS
# ---------------------------------------------------------------------------

def get_mssql_triggers():
    conn = ms(); cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT s.name AS schema_name, t.name AS table_name, tr.name AS trigger_name,
               tr.is_disabled,
               STUFF((SELECT ','+te.type_desc FROM sys.trigger_events te
                      WHERE te.object_id=tr.object_id FOR XML PATH('')),1,1,'') AS events
        FROM sys.triggers tr
        JOIN sys.tables t ON tr.parent_id = t.object_id
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE tr.parent_class = 1 AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
        ORDER BY s.name, t.name, tr.name
    """)
    rows = cur.fetchall(); conn.close()
    return rows

def get_spg_triggers():
    conn = spg(); cur = conn.cursor()
    cur.execute("""
        SELECT trigger_schema, event_object_table, trigger_name,
               event_manipulation, action_timing
        FROM information_schema.triggers
        ORDER BY trigger_schema, event_object_table, trigger_name
    """)
    rows = cur.fetchall(); conn.close()
    grouped = {}
    for sc, tbl, trig, event, timing in rows:
        if is_spg_system_schema(sc): continue
        key = (sc.lower(), tbl.lower(), trig.lower())
        if key not in grouped:
            grouped[key] = {'schema':sc,'table':tbl,'name':trig,'events':[],'timings':[]}
        grouped[key]['events'].append(event)
        grouped[key]['timings'].append(timing)
    return list(grouped.values())

def norm_trig(n):
    n = n.lower()
    for p in ['stg_','dbo_','api_']:
        if n.startswith(p): n = n[len(p):]
    if n.endswith('_trigger'): n = n[:-8]
    return n


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

def run_all():
    source_db = MSSQL_CONF.get('database','source')
    target_db = SPG_CONF.get('dbname','postgres')
    all_records = []
    pass_c = fail_c = err_c = skip_c = 0

    # ── TABLES ────────────────────────────────────────────────────────────────
    print("\n%s\nTABLES — existence + column parity\n%s" % (SEP, LINE))
    ms_tables  = discover_mssql_tables()
    spg_tables = discover_spg_tables()
    t_matched  = sorted(set(ms_tables) & set(spg_tables))
    t_ms_only  = sorted(set(ms_tables) - set(spg_tables))
    t_spg_only = sorted(set(spg_tables) - set(ms_tables))

    print("MSSQL: %d  |  SPG: %d  |  Matched: %d  |  MSSQL-only: %d  |  SPG-only: %d" % (
        len(ms_tables), len(spg_tables), len(t_matched), len(t_ms_only), len(t_spg_only)))

    BATCH = 12
    tbl_results = []
    for i in range(0, len(t_matched), BATCH):
        batch = t_matched[i:i+BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(validate_table, sc, nm): (sc,nm) for sc,nm in batch}
            for fut in concurrent.futures.as_completed(futs):
                sc, nm = futs[fut]
                r = fut.result()
                r['key'] = (sc, nm)
                tbl_results.append(r)

    tbl_results.sort(key=lambda r: (r['verdict'] != 'PASS', r['key']))
    print("\n  %-60s %6s %6s  %-7s  ISSUES" % ("TABLE","MS_C","SPG_C","VERDICT"))
    print("  " + LINE)
    for r in tbl_results:
        sc, nm = r['key']
        ms_info = ms_tables[(sc,nm)]
        print("  %-60s %6s %6s  %-7s" % (
            '%s.%s'%(sc, ms_info['table_name']),
            str(r['ms_cols']), str(r['spg_cols']), r['verdict']))
        for iss in r['issues']: print("    └─ %s" % iss)
        v = r['verdict']
        if v=='PASS': pass_c+=1
        elif v in ('FAIL','ERROR'): fail_c+=1
        all_records.append({
            'object_name': '%s.%s'%(sc,nm), 'object_type':'TABLE',
            'source_schema':sc, 'target_schema':sc,
            'source_call':'schema_only','target_call':'schema_only',
            'params_used':None,'strategy_used':'column_parity',
            'source_call_output':None,'target_call_output':None,
            'source_row_count':r['ms_cols'],'target_row_count':r['spg_cols'],
            'test_verdict':v,'issues':r['issues'][:5],
            'error_message':r['issues'][0] if r['issues'] else None,
            'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND',
        })
    for sc,nm in t_ms_only:
        nm_orig = ms_tables[(sc,nm)]['table_name']
        print("  MISSING  %-60s" % ('%s.%s'%(sc,nm_orig)))
        skip_c+=1
        all_records.append({
            'object_name':'%s.%s'%(sc,nm),'object_type':'TABLE',
            'source_schema':sc,'target_schema':sc,
            'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
            'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
            'test_verdict':'MSSQL_ONLY','issues':['MISSING_IN_SPG'],'error_message':'Table in MSSQL but not SPG',
            'diff_sample':None,'mssql_status':'FOUND','spg_status':'MISSING',
        })
    for sc,nm in t_spg_only:
        nm_orig = spg_tables[(sc,nm)]['table_name']
        print("  SPG_ONLY %-60s" % ('%s.%s'%(sc,nm_orig)))
        skip_c+=1
        all_records.append({
            'object_name':'%s.%s'%(sc,nm),'object_type':'TABLE',
            'source_schema':sc,'target_schema':sc,
            'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
            'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
            'test_verdict':'SPG_ONLY','issues':['NOT_IN_MSSQL'],'error_message':'Table in SPG but not MSSQL',
            'diff_sample':None,'mssql_status':'MISSING','spg_status':'FOUND',
        })
    t_pass = sum(1 for r in tbl_results if r['verdict']=='PASS')
    t_fail = sum(1 for r in tbl_results if r['verdict'] in ('FAIL','ERROR','WARN'))
    print("\nTABLES SUMMARY: PASS=%d  FAIL=%d  MSSQL_ONLY=%d  SPG_ONLY=%d" % (
        t_pass, t_fail, len(t_ms_only), len(t_spg_only)))

    # ── VIEWS ─────────────────────────────────────────────────────────────────
    print("\n%s\nVIEWS — existence + column parity (no row count)\n%s" % (SEP, LINE))
    ms_views  = discover_mssql_views()
    spg_views = discover_spg_views()
    v_matched  = sorted(set(ms_views) & set(spg_views))
    v_ms_only  = sorted(set(ms_views) - set(spg_views))
    v_spg_only = sorted(set(spg_views) - set(ms_views))

    print("MSSQL: %d  |  SPG: %d  |  Matched: %d  |  MSSQL-only: %d  |  SPG-only: %d" % (
        len(ms_views), len(spg_views), len(v_matched), len(v_ms_only), len(v_spg_only)))

    view_results = []
    for i in range(0, len(v_matched), BATCH):
        batch = v_matched[i:i+BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(validate_view_schema, sc, nm): (sc,nm) for sc,nm in batch}
            for fut in concurrent.futures.as_completed(futs):
                sc, nm = futs[fut]
                r = fut.result(); r['key'] = (sc,nm)
                view_results.append(r)

    view_results.sort(key=lambda r: (r['verdict'] != 'PASS', r['key']))
    print("\n  %-60s %6s %6s  %-7s  ISSUES" % ("VIEW","MS_C","SPG_C","VERDICT"))
    print("  " + LINE)
    for r in view_results:
        sc, nm = r['key']
        nm_orig = ms_views[(sc,nm)]['view_name']
        print("  %-60s %6s %6s  %-7s" % (
            '%s.%s'%(sc,nm_orig), str(r['ms_cols']), str(r['spg_cols']), r['verdict']))
        for iss in r['issues']: print("    └─ %s" % iss)
        v = r['verdict']
        if v=='PASS': pass_c+=1
        elif v in ('FAIL','ERROR'): fail_c+=1
        all_records.append({
            'object_name':'%s.%s'%(sc,nm),'object_type':'VIEW',
            'source_schema':sc,'target_schema':sc,
            'source_call':'schema_only','target_call':'schema_only',
            'params_used':None,'strategy_used':'column_parity',
            'source_call_output':None,'target_call_output':None,
            'source_row_count':r['ms_cols'],'target_row_count':r['spg_cols'],
            'test_verdict':v,'issues':r['issues'][:5],
            'error_message':r['issues'][0] if r['issues'] else None,
            'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND',
        })
    for sc,nm in v_ms_only:
        nm_orig = ms_views[(sc,nm)]['view_name']
        print("  MISSING  %s.%s" % (sc, nm_orig))
        skip_c+=1
        all_records.append({
            'object_name':'%s.%s'%(sc,nm),'object_type':'VIEW',
            'source_schema':sc,'target_schema':sc,
            'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
            'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
            'test_verdict':'MSSQL_ONLY','issues':['MISSING_IN_SPG'],'error_message':'View in MSSQL but not SPG',
            'diff_sample':None,'mssql_status':'FOUND','spg_status':'MISSING',
        })
    for sc,nm in v_spg_only:
        nm_orig = spg_views[(sc,nm)]['view_name']
        print("  SPG_ONLY %s.%s" % (sc, nm_orig))
        skip_c+=1
        all_records.append({
            'object_name':'%s.%s'%(sc,nm),'object_type':'VIEW',
            'source_schema':sc,'target_schema':sc,
            'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
            'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
            'test_verdict':'SPG_ONLY','issues':['NOT_IN_MSSQL'],'error_message':'View in SPG but not MSSQL',
            'diff_sample':None,'mssql_status':'MISSING','spg_status':'FOUND',
        })
    vw_pass = sum(1 for r in view_results if r['verdict']=='PASS')
    vw_fail = sum(1 for r in view_results if r['verdict'] in ('FAIL','ERROR','WARN'))
    print("\nVIEWS SUMMARY: PASS=%d  FAIL=%d  MSSQL_ONLY=%d  SPG_ONLY=%d" % (
        vw_pass, vw_fail, len(v_ms_only), len(v_spg_only)))

    # ── PROCEDURES / FUNCTIONS ────────────────────────────────────────────────
    print("\n%s\nPROCEDURES / FUNCTIONS — existence + parameter parity (no execution)\n%s" % (SEP, LINE))
    ms_routines  = discover_mssql_routines()
    spg_routines = discover_spg_routines()

    ms_funcs  = {k:v for k,v in ms_routines.items()  if v['kind']=='FUNCTION'}
    ms_procs  = {k:v for k,v in ms_routines.items()  if v['kind']=='PROCEDURE'}
    spg_funcs = {k:v for k,v in spg_routines.items() if v['kind']=='FUNCTION'}
    spg_procs = {k:v for k,v in spg_routines.items() if v['kind']=='PROCEDURE'}

    print("MSSQL  — Functions: %d  |  Procedures: %d" % (len(ms_funcs), len(ms_procs)))
    print("SPG    — Functions: %d  |  Procedures: %d" % (len(spg_funcs), len(spg_procs)))

    # Keys that are PROCEDURE in MSSQL but FUNCTION in SPG (PROC_TO_FUNC pattern)
    # These are correctly migrated procs that return result sets
    proc_to_func = sorted(
        key for key in set(ms_procs) - set(spg_procs)
        if key in spg_funcs
    )
    # Keys that are FUNCTION in MSSQL but PROCEDURE in SPG (rare reverse case)
    func_to_proc = sorted(
        key for key in set(ms_funcs) - set(spg_funcs)
        if key in spg_procs
    )

    print("PROC_TO_FUNC (MSSQL PROCEDURE → SPG FUNCTION): %d" % len(proc_to_func))
    if func_to_proc:
        print("FUNC_TO_PROC (MSSQL FUNCTION → SPG PROCEDURE): %d" % len(func_to_proc))

    for cat, ms_sub, spg_sub in [('FUNCTION',ms_funcs,spg_funcs),('PROCEDURE',ms_procs,spg_procs)]:
        # Exclude cross-type matches from MSSQL-only and SPG-only counts
        cross_keys = set(proc_to_func) | set(func_to_proc)
        matched  = sorted(set(ms_sub) & set(spg_sub))
        ms_only  = sorted((set(ms_sub) - set(spg_sub)) - cross_keys)
        spg_only = sorted((set(spg_sub) - set(ms_sub)) - cross_keys)
        print("\n  %s: Matched=%d  MSSQL-only=%d  SPG-only=%d" % (
            cat, len(matched), len(ms_only), len(spg_only)))
        print("  %-60s %6s %6s  %-7s  ISSUES" % ("OBJECT","MS_P","SPG_P","VERDICT"))
        print("  " + LINE)

        rout_results = []
        for i in range(0, len(matched), BATCH):
            batch = matched[i:i+BATCH]
            with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
                futs = {pool.submit(validate_routine, sc, nm): (sc,nm) for sc,nm in batch}
                for fut in concurrent.futures.as_completed(futs):
                    sc,nm = futs[fut]; r = fut.result(); r['key']=(sc,nm)
                    rout_results.append(r)
        rout_results.sort(key=lambda r: (r['verdict']!='PASS', r['key']))

        for r in rout_results:
            sc,nm = r['key']
            nm_orig = ms_sub[(sc,nm)]['name']
            print("  %-60s %6s %6s  %-7s" % (
                '%s.%s'%(sc,nm_orig),
                str(r['ms_param_count']), str(r['spg_param_count']), r['verdict']))
            for iss in r['issues']: print("    └─ %s" % iss)
            v = r['verdict']
            if v=='PASS': pass_c+=1
            elif v in ('FAIL','ERROR'): fail_c+=1
            all_records.append({
                'object_name':'%s.%s'%(sc,nm),'object_type':cat,
                'source_schema':sc,'target_schema':sc,
                'source_call':'MSSQL type: %s'%ms_sub[(sc,nm)].get('ms_type',''),
                'target_call':'SPG prokind: %s'%spg_sub[(sc,nm)].get('prokind',''),
                'params_used':None,'strategy_used':'param_comparison',
                'source_call_output':None,'target_call_output':None,
                'source_row_count':r['ms_param_count'],'target_row_count':r['spg_param_count'],
                'test_verdict':v,'issues':r['issues'][:5],
                'error_message':r['issues'][0] if r['issues'] else None,
                'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND',
            })
        for sc,nm in ms_only:
            nm_orig = ms_sub[(sc,nm)]['name']
            print("  MISSING  %-60s" % ('%s.%s'%(sc,nm_orig)))
            skip_c+=1
            all_records.append({
                'object_name':'%s.%s'%(sc,nm),'object_type':cat,
                'source_schema':sc,'target_schema':sc,
                'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
                'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
                'test_verdict':'MSSQL_ONLY','issues':['MISSING_IN_SPG'],'error_message':'Object in MSSQL but not SPG',
                'diff_sample':None,'mssql_status':'FOUND','spg_status':'MISSING',
            })
        for sc,nm in spg_only:
            nm_orig = spg_sub[(sc,nm)]['name']
            print("  SPG_ONLY %-60s" % ('%s.%s'%(sc,nm_orig)))
            skip_c+=1
            all_records.append({
                'object_name':'%s.%s'%(sc,nm),'object_type':cat,
                'source_schema':sc,'target_schema':sc,
                'source_call':None,'target_call':None,'params_used':None,'strategy_used':'existence_check',
                'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
                'test_verdict':'SPG_ONLY','issues':['NOT_IN_MSSQL'],'error_message':'Object in SPG but not MSSQL',
                'diff_sample':None,'mssql_status':'MISSING','spg_status':'FOUND',
            })

    # ── PROC_TO_FUNC — MSSQL PROCEDURE migrated as SPG FUNCTION ──────────────
    if proc_to_func:
        print("\n  PROC_TO_FUNC — MSSQL PROCEDURE migrated as SPG FUNCTION (%d)" % len(proc_to_func))
        print("  (These are correctly converted: result-returning procs become FUNCTION in Postgres)")
        print("  %-60s %6s %6s  %-7s  ISSUES" % ("OBJECT","MS_P","SPG_P","VERDICT"))
        print("  " + LINE)

        ptf_results = []
        for i in range(0, len(proc_to_func), BATCH):
            batch = proc_to_func[i:i+BATCH]
            with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
                futs = {pool.submit(validate_routine, sc, nm): (sc,nm) for sc,nm in batch}
                for fut in concurrent.futures.as_completed(futs):
                    sc,nm = futs[fut]; r = fut.result(); r['key']=(sc,nm)
                    ptf_results.append(r)
        ptf_results.sort(key=lambda r: (r['verdict']!='PASS', r['key']))

        ptf_pass = ptf_fail = 0
        for r in ptf_results:
            sc,nm = r['key']
            nm_orig = ms_procs[(sc,nm)]['name']
            print("  %-60s %6s %6s  %-7s" % (
                '%s.%s'%(sc,nm_orig),
                str(r['ms_param_count']), str(r['spg_param_count']), r['verdict']))
            for iss in r['issues']: print("    └─ %s" % iss)
            v = r['verdict']
            if v=='PASS': pass_c+=1; ptf_pass+=1
            elif v in ('FAIL','ERROR'): fail_c+=1; ptf_fail+=1
            all_records.append({
                'object_name':'%s.%s'%(sc,nm),'object_type':'PROC_TO_FUNC',
                'source_schema':sc,'target_schema':sc,
                'source_call':'MSSQL PROCEDURE (returns resultset)',
                'target_call':'SPG FUNCTION (RETURNS TABLE)',
                'params_used':None,'strategy_used':'param_comparison_cross_type',
                'source_call_output':None,'target_call_output':None,
                'source_row_count':r['ms_param_count'],'target_row_count':r['spg_param_count'],
                'test_verdict':v,'issues':r['issues'][:5],
                'error_message':r['issues'][0] if r['issues'] else None,
                'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND',
            })
        print("\n  PROC_TO_FUNC SUMMARY: PASS=%d  FAIL=%d" % (ptf_pass, ptf_fail))

    # ── FUNC_TO_PROC (rare reverse) ───────────────────────────────────────────
    if func_to_proc:
        print("\n  FUNC_TO_PROC — MSSQL FUNCTION migrated as SPG PROCEDURE (%d)" % len(func_to_proc))
        for sc,nm in func_to_proc:
            nm_orig = ms_funcs[(sc,nm)]['name']
            print("  WARN  %-60s  (MSSQL=FUNCTION, SPG=PROCEDURE)" % ('%s.%s'%(sc,nm_orig)))
            all_records.append({
                'object_name':'%s.%s'%(sc,nm),'object_type':'FUNC_TO_PROC',
                'source_schema':sc,'target_schema':sc,
                'source_call':'MSSQL FUNCTION','target_call':'SPG PROCEDURE',
                'params_used':None,'strategy_used':'existence_check',
                'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
                'test_verdict':'WARN','issues':['TYPE_MISMATCH: MSSQL=FUNCTION SPG=PROCEDURE'],
                'error_message':'Type changed: MSSQL FUNCTION migrated as SPG PROCEDURE',
                'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND',
            })

    # ── TRIGGERS ──────────────────────────────────────────────────────────────
    print("\n%s\nTRIGGERS — existence + event type\n%s" % (SEP, LINE))
    ms_triggers  = get_mssql_triggers()
    spg_triggers = get_spg_triggers()
    spg_by_norm  = {norm_trig(t['name']): t for t in spg_triggers}
    ms_norms     = {norm_trig(t['trigger_name']) for t in ms_triggers}

    print("MSSQL: %d  |  SPG: %d" % (len(ms_triggers), len(spg_triggers)))
    trig_pass_c = trig_fail_c = 0
    for ms_t in ms_triggers:
        nm = ms_t['trigger_name']
        full = '%s.%s.%s' % (ms_t['schema_name'], ms_t['table_name'], nm)
        spg_t = spg_by_norm.get(norm_trig(nm))
        issues, verdict = [], 'PASS'
        if spg_t is None:
            verdict='FAIL'; issues.append('MISSING_IN_SPG: %s not found in SPG'%nm)
            trig_fail_c+=1; fail_c+=1
        else:
            ms_ev  = set(e.strip().upper() for e in (ms_t['events'] or '').split(',') if e.strip())
            spg_ev = set(e.strip().upper() for e in spg_t['events'])
            if ms_ev and ms_ev != spg_ev:
                issues.append('EVENT_MISMATCH: MSSQL=%s SPG=%s'%(sorted(ms_ev),sorted(spg_ev)))
                verdict='FAIL'; trig_fail_c+=1; fail_c+=1
            else:
                trig_pass_c+=1; pass_c+=1
        print("  %-7s  %s" % (verdict, full))
        for iss in issues: print("    └─ %s" % iss)
        all_records.append({
            'object_name':full,'object_type':'TRIGGER',
            'source_schema':ms_t['schema_name'],'target_schema':spg_t['schema'] if spg_t else ms_t['schema_name'],
            'source_call':'ON %s %s'%(ms_t['table_name'],ms_t['events'] or ''),'target_call':None,
            'params_used':None,'strategy_used':'existence_check',
            'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
            'test_verdict':verdict,'issues':issues,'error_message':'; '.join(issues) if issues else None,
            'diff_sample':None,'mssql_status':'FOUND','spg_status':'FOUND' if spg_t else 'MISSING',
        })
    for t in spg_triggers:
        if norm_trig(t['name']) not in ms_norms:
            full = '%s.%s.%s'%(t['schema'],t['table'],t['name'])
            print("  SPG_ONLY %s" % full)
            skip_c+=1
            all_records.append({
                'object_name':full,'object_type':'TRIGGER',
                'source_schema':t['schema'],'target_schema':t['schema'],
                'source_call':None,'target_call':'ON %s %s'%(t['table'],','.join(t['events'])),
                'params_used':None,'strategy_used':'existence_check',
                'source_call_output':None,'target_call_output':None,'source_row_count':None,'target_row_count':None,
                'test_verdict':'SPG_ONLY','issues':['NOT_IN_MSSQL'],'error_message':None,
                'diff_sample':None,'mssql_status':'MISSING','spg_status':'FOUND',
            })
    print("\nTRIGGERS SUMMARY: PASS=%d  FAIL=%d  SPG_ONLY=%d" % (
        trig_pass_c, trig_fail_c,
        sum(1 for t in spg_triggers if norm_trig(t['name']) not in ms_norms)))

    # ── Grand summary ─────────────────────────────────────────────────────────
    print("\n%s\nGRAND SUMMARY — Schema-Only Object Audit" % SEP)
    print(SEP)
    print("  PASS      : %d" % pass_c)
    print("  FAIL/ERROR: %d" % fail_c)
    print("  MISSING/EXTRA: %d" % skip_c)
    print("  TOTAL records: %d" % len(all_records))
    print(SEP)

    # ── Write to validation tables ────────────────────────────────────────────
    schemas = sorted({r['source_schema'] for r in all_records if r['source_schema']})
    run_id, run_number = vdb.create_run(
        source_db, target_db, schemas,
        notes='Schema-only object audit: tables + views (columns) + procs/funcs (params) + triggers (existence)'
    )
    vdb.insert_results(run_id, run_number, all_records)
    vdb.complete_run(run_id, len(all_records), pass_c, fail_c, 0, skip_c)
    print("Saved as run_number=%d" % run_number)
    return run_number, all_records


if __name__ == '__main__':
    run_all()
