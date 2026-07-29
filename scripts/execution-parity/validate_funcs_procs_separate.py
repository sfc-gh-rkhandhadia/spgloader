"""
validate_funcs_procs_separate.py — Separate validation runs for FUNCTIONS and PROCEDURES.

Discovers all routines in MSSQL and SPG, splits them into two categories:
  - FUNCTION  : MSSQL types FN, TF, IF, FS; SPG prokind='f' (excluding trigger functions)
  - PROCEDURE : MSSQL type P; SPG prokind='p'

Creates TWO separate validation run entries in validation.validation_run:
  - run_number N   : FUNCTIONS only
  - run_number N+1 : PROCEDURES only

Validates: existence, parameter count, parameter names.

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
"""
import os, sys, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, is_mssql_system_schema,
                    is_spg_system_schema, check_required)
import pymssql, psycopg2, psycopg2.extras
import validation_db as vdb

check_required()

SEP = "=" * 110
LINE = "-" * 110

# ── MSSQL discovery ───────────────────────────────────────────────────────────

MSSQL_FUNC_TYPES  = ('FN', 'TF', 'IF', 'FS', 'FT')   # scalar + table-valued
MSSQL_PROC_TYPES  = ('P',)

def _ms_conn():
    return pymssql.connect(**MSSQL_CONF)

def discover_mssql_routines():
    """Return dict keyed by (schema, name) → {type, kind='FUNCTION'|'PROCEDURE'}"""
    conn = _ms_conn(); cur = conn.cursor(as_dict=True)
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
        schema = r['schema_name'].lower()
        name   = r['obj_name'].lower()
        ms_type = r['ms_type'].strip()
        kind = 'FUNCTION' if ms_type in MSSQL_FUNC_TYPES else 'PROCEDURE'
        result[(schema, name)] = {
            'name':    r['obj_name'],
            'schema':  r['schema_name'],
            'ms_type': ms_type,
            'kind':    kind,
        }
    return result


def discover_spg_routines():
    """Return dict keyed by (schema, name) → {kind='FUNCTION'|'PROCEDURE', prokind, return_type}"""
    conn = psycopg2.connect(**SPG_CONF)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT n.nspname AS schema_name, p.proname AS obj_name,
               p.prokind,
               p.prorettype::regtype AS return_type
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE p.prokind IN ('f','p')
    """)
    rows = cur.fetchall(); conn.close()
    result = {}
    for r in rows:
        schema = r['schema_name'].lower()
        name   = r['obj_name'].lower()
        if is_spg_system_schema(schema):
            continue
        # Exclude trigger functions (they have no direct MSSQL counterpart)
        if str(r['return_type']).lower() == 'trigger':
            continue
        kind = 'FUNCTION' if r['prokind'] == 'f' else 'PROCEDURE'
        result[(schema, name)] = {
            'name':        r['obj_name'],
            'schema':      r['schema_name'],
            'prokind':     r['prokind'],
            'kind':        kind,
            'return_type': str(r['return_type']),
        }
    return result


# ── Parameter comparison ──────────────────────────────────────────────────────

def get_mssql_params(schema, name):
    try:
        conn = _ms_conn(); cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT p.parameter_id, p.name AS pname, t.name AS tname, p.is_output
            FROM sys.objects o
            JOIN sys.schemas s  ON o.schema_id = s.schema_id
            JOIN sys.parameters p ON o.object_id = p.object_id
            JOIN sys.types t ON p.user_type_id = t.user_type_id
            WHERE s.name = %s AND LOWER(o.name) = %s
            ORDER BY p.parameter_id
        """, (schema, name.lower()))
        rows = cur.fetchall(); conn.close()
        return [{'name': r['pname'].lstrip('@').lower(), 'type': r['tname'],
                 'is_output': r['is_output']} for r in rows]
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]


def get_spg_params(schema, name):
    try:
        conn = psycopg2.connect(**SPG_CONF)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT pa.ordinal_position, pa.parameter_name, pa.data_type, pa.parameter_mode
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name = pa.specific_name
            WHERE r.routine_schema = %s AND LOWER(r.routine_name) = %s
            ORDER BY pa.ordinal_position
        """, (schema, name.lower()))
        rows = cur.fetchall(); conn.close()
        # Strip leading underscore only; p_ prefix removal handled by strip_p_prefix()
        result = []
        for r in rows:
            n = (r['parameter_name'] or '').lower().lstrip('_')
            result.append({'name': n, 'type': r['data_type'] or '',
                           'mode': r['parameter_mode'] or 'IN'})
        return result
    except Exception as e:
        return 'ERR:%s' % str(e)[:100]


def strip_p_prefix(n):
    return n[2:] if n.startswith('p_') else n


def validate_routine(schema, name, kind):
    ms_p  = get_mssql_params(schema, name)
    spg_p = get_spg_params(schema, name)

    if isinstance(ms_p, str):
        return {'verdict': 'ERROR', 'issues': ['MSSQL_ERR: %s' % ms_p],
                'ms_params': '?', 'spg_params': '?', 'ms_param_count': 0, 'spg_param_count': 0}
    if isinstance(spg_p, str):
        return {'verdict': 'ERROR', 'issues': ['SPG_ERR: %s' % spg_p],
                'ms_params': len(ms_p), 'spg_params': '?',
                'ms_param_count': len(ms_p), 'spg_param_count': 0}

    issues, verdict = [], 'PASS'

    if len(ms_p) != len(spg_p):
        issues.append('PARAM_COUNT: MSSQL=%d SPG=%d' % (len(ms_p), len(spg_p)))
        verdict = 'FAIL'
    elif ms_p and spg_p:
        all_ms  = [p['name'] for p in ms_p]
        all_spg = [strip_p_prefix(p['name']) for p in spg_p]
        if set(all_ms) != set(all_spg):
            diff = ['pos%d MSSQL=%s SPG=%s' % (i + 1, a, b)
                    for i, (a, b) in enumerate(zip(all_ms, all_spg)) if a != b]
            issues.append('PARAM_NAMES_DIFFER: %s%s' % (
                str(diff[:3])[1:-1], '...' if len(diff) > 3 else ''))
            verdict = 'FAIL'

    return {
        'verdict': verdict,
        'issues':  issues,
        'ms_param_count':  len(ms_p),
        'spg_param_count': len(spg_p),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run_category(category, ms_subset, spg_subset, ms_full, spg_full):
    """
    Run validation for one category (FUNCTION or PROCEDURE).
    Returns (records, pass_count, fail_count, error_count, skip_count).
    """
    ms_keys  = set(ms_subset.keys())
    spg_keys = set(spg_subset.keys())
    matched  = sorted(ms_keys & spg_keys)
    ms_only  = sorted(ms_keys - spg_keys)
    spg_only = sorted(spg_keys - ms_keys)

    print("\n%s" % SEP)
    print("%s VALIDATION" % category)
    print("  MSSQL: %d  |  SPG: %d  |  Matched: %d  |  MSSQL-only: %d  |  SPG-only: %d" % (
        len(ms_keys), len(spg_keys), len(matched), len(ms_only), len(spg_only)))
    print(SEP)

    records = []
    pass_c = fail_c = err_c = 0

    # ── Matched: run validation
    BATCH = 12
    all_results = []

    def _run_one(key):
        schema, name = key
        r = validate_routine(schema, name, category)
        r['key'] = key
        return r

    for i in range(0, len(matched), BATCH):
        batch = matched[i:i + BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(_run_one, k): k for k in batch}
            for fut in concurrent.futures.as_completed(futs):
                all_results.append(fut.result())

    order = {'FAIL': 0, 'ERROR': 1, 'PASS': 2}
    all_results.sort(key=lambda r: (order.get(r['verdict'], 9), r['key']))

    # Print matched results
    if matched:
        print("\n  %-60s %6s %6s  %-7s  ISSUES" % ("OBJECT", "MS_P", "SPG_P", "VERDICT"))
        print("  " + LINE)
        for r in all_results:
            schema, name = r['key']
            ms_info  = ms_subset[r['key']]
            full_obj = '%s.%s' % (schema, ms_info['name'])
            print("  %-60s %6s %6s  %-7s" % (
                full_obj,
                str(r['ms_param_count']),
                str(r['spg_param_count']),
                r['verdict']))
            for iss in r['issues']:
                print("    └─ %s" % iss)

            v = r['verdict']
            if v == 'PASS':    pass_c += 1
            elif v == 'ERROR': err_c  += 1
            else:              fail_c += 1

            records.append({
                'object_name':        '%s.%s' % r['key'],
                'object_type':        category,
                'source_schema':      schema,
                'target_schema':      schema,
                'source_call':        'MSSQL type: %s' % ms_subset[r['key']].get('ms_type', ''),
                'target_call':        'SPG prokind: %s' % spg_subset[r['key']].get('prokind', ''),
                'params_used':        None,
                'strategy_used':      'param_comparison',
                'source_call_output': None,
                'target_call_output': None,
                'source_row_count':   r['ms_param_count'],
                'target_row_count':   r['spg_param_count'],
                'test_verdict':       r['verdict'],
                'issues':             r.get('issues', [])[:5],
                'error_message':      r['issues'][0] if r.get('issues') else None,
                'diff_sample':        None,
                'mssql_status':       'FOUND',
                'spg_status':         'FOUND',
            })

    # ── MSSQL-only
    if ms_only:
        print("\n  MISSING IN SPG (%d):" % len(ms_only))
        for key in ms_only:
            schema, name = key
            info = ms_subset[key]
            ms_t = info.get('ms_type', '')
            print("  MISSING  %-60s  ms_type=%s" % ('%s.%s' % (schema, info['name']), ms_t))
            records.append({
                'object_name':   '%s.%s' % key,
                'object_type':   category,
                'source_schema': schema,
                'target_schema': schema,
                'source_call':   'MSSQL type: %s' % ms_t,
                'target_call':   None,
                'params_used': None, 'strategy_used': 'existence_check',
                'source_call_output': None, 'target_call_output': None,
                'source_row_count': None, 'target_row_count': None,
                'test_verdict':  'MSSQL_ONLY',
                'issues':        ['MISSING_IN_SPG'],
                'error_message': 'Object exists in MSSQL but not in SPG',
                'diff_sample':   None,
                'mssql_status':  'FOUND', 'spg_status': 'MISSING',
            })

    # ── SPG-only
    if spg_only:
        print("\n  NEW IN SPG ONLY (%d):" % len(spg_only))
        for key in spg_only:
            schema, name = key
            info = spg_subset[key]
            rt = info.get('return_type', '')
            print("  SPG_ONLY  %-60s  return_type=%s" % ('%s.%s' % (schema, info['name']), rt))
            records.append({
                'object_name':   '%s.%s' % key,
                'object_type':   category,
                'source_schema': schema,
                'target_schema': schema,
                'source_call':   None,
                'target_call':   'SPG prokind: %s' % info.get('prokind', ''),
                'params_used': None, 'strategy_used': 'existence_check',
                'source_call_output': None, 'target_call_output': None,
                'source_row_count': None, 'target_row_count': None,
                'test_verdict':  'SPG_ONLY',
                'issues':        ['NOT_IN_MSSQL'],
                'error_message': 'Object exists in SPG but not in MSSQL',
                'diff_sample':   None,
                'mssql_status':  'MISSING', 'spg_status': 'FOUND',
            })

    # ── Summary line
    skip_c = len(ms_only) + len(spg_only)
    print("\n  %s SUMMARY — PASS:%d  FAIL:%d  ERROR:%d  MSSQL_ONLY:%d  SPG_ONLY:%d" % (
        category, pass_c, fail_c, err_c, len(ms_only), len(spg_only)))

    return records, pass_c, fail_c, err_c, skip_c


def main():
    print(SEP)
    print("FUNCTIONS vs PROCEDURES — Separate Validation")
    print("  Source: %s @ %s:%d" % (MSSQL_CONF['database'], MSSQL_CONF['server'], MSSQL_CONF['port']))
    print("  Target: %s @ %s" % (SPG_CONF.get('dbname', 'postgres'), SPG_CONF['host'][:60]))
    print(SEP)

    print("\nDiscovering routines...")
    ms_all  = discover_mssql_routines()
    spg_all = discover_spg_routines()

    # Split by kind
    ms_funcs  = {k: v for k, v in ms_all.items()  if v['kind'] == 'FUNCTION'}
    ms_procs  = {k: v for k, v in ms_all.items()  if v['kind'] == 'PROCEDURE'}
    spg_funcs = {k: v for k, v in spg_all.items() if v['kind'] == 'FUNCTION'}
    spg_procs = {k: v for k, v in spg_all.items() if v['kind'] == 'PROCEDURE'}

    print("  MSSQL  — Functions: %d  |  Procedures: %d" % (len(ms_funcs), len(ms_procs)))
    print("  SPG    — Functions: %d  |  Procedures: %d" % (len(spg_funcs), len(spg_procs)))

    source_db = MSSQL_CONF.get('database', 'source')
    target_db = SPG_CONF.get('dbname', 'postgres')

    # ── Run FUNCTIONS
    func_records, fp, ff, fe, fs = run_category(
        'FUNCTION', ms_funcs, spg_funcs, ms_all, spg_all)

    fn_run_id, fn_run_num = vdb.create_run(
        source_db, target_db,
        sorted({r['source_schema'] for r in func_records}) or ['all'],
        notes='Function-only validation (existence + parameter parity)'
    )
    vdb.insert_results(fn_run_id, fn_run_num, func_records)
    vdb.complete_run(fn_run_id, len(func_records), fp, ff, fe, fs)
    print("  → Saved as run_number=%d" % fn_run_num)

    # ── Run PROCEDURES
    proc_records, pp, pf, pe, ps = run_category(
        'PROCEDURE', ms_procs, spg_procs, ms_all, spg_all)

    pr_run_id, pr_run_num = vdb.create_run(
        source_db, target_db,
        sorted({r['source_schema'] for r in proc_records}) or ['all'],
        notes='Procedure-only validation (existence + parameter parity)'
    )
    vdb.insert_results(pr_run_id, pr_run_num, proc_records)
    vdb.complete_run(pr_run_id, len(proc_records), pp, pf, pe, ps)
    print("  → Saved as run_number=%d" % pr_run_num)

    # ── Grand summary
    print("\n%s" % SEP)
    print("GRAND SUMMARY")
    print(SEP)
    print("  FUNCTIONS  (run_number=%-3d) : PASS=%-3d FAIL=%-3d ERROR=%-3d MSSQL_ONLY=%-3d SPG_ONLY=%-3d" % (
        fn_run_num, fp, ff, fe,
        sum(1 for r in func_records if r['test_verdict'] == 'MSSQL_ONLY'),
        sum(1 for r in func_records if r['test_verdict'] == 'SPG_ONLY')))
    print("  PROCEDURES (run_number=%-3d) : PASS=%-3d FAIL=%-3d ERROR=%-3d MSSQL_ONLY=%-3d SPG_ONLY=%-3d" % (
        pr_run_num, pp, pf, pe,
        sum(1 for r in proc_records if r['test_verdict'] == 'MSSQL_ONLY'),
        sum(1 for r in proc_records if r['test_verdict'] == 'SPG_ONLY')))
    print(SEP)
    print("\nQuery results:")
    print("  SELECT object_type, test_verdict, COUNT(*) FROM validation.validation_result")
    print("  WHERE run_number IN (%d, %d) GROUP BY 1,2 ORDER BY 1,2;" % (fn_run_num, pr_run_num))


if __name__ == '__main__':
    main()
