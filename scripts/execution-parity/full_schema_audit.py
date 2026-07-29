"""
Cross-schema DDL audit (structural only)
=========================================
Discovers every user schema on both MSSQL and SPG and checks existence +
parameter/column parity for procedures, functions, and views across all schemas.
Results print to stdout; nothing is written to the validation audit tables.

Use this script for:
  - A wide-angle structural survey when you want to see every schema at once
  - Early-stage migration assessment before the full behavioral pipeline is run
  - Checking whether a schema was converted at all (MISSING vs SPG_ONLY)

Do NOT use this script for:
  - Behavioral execution testing → use run.py --procs
  - Storing results for report generation → use run.py (writes to validation.validation_result)
  - Trigger or table validation → use validate_triggers.py / run.py

Limitations:
  - Structural parity only: checks existence and parameter counts, does NOT execute objects
  - Results are not persisted to validation.validation_result
  - Schema list is computed eagerly at import time (two DB connections on startup)

Alternative for persistent results: python3 run.py --all
Alternative for schema-only parity:  python3 validate_funcs_procs_separate.py
"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
print("[NOTICE] full_schema_audit.py is a structural spot-check tool. "
      "Results are not saved to audit tables. Use run.py for the full pipeline.")
import pymssql, psycopg2, psycopg2.extras, concurrent.futures, sys
from config import MSSQL_CONF, SPG_CONF, is_mssql_system_schema, is_spg_system_schema, check_required

check_required()

def ms_conn():  return pymssql.connect(**MSSQL_CONF)
def spg_conn(): return psycopg2.connect(**SPG_CONF)

def discover_schemas_mssql():
    conn = ms_conn(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT s.name FROM sys.objects o JOIN sys.schemas s ON o.schema_id=s.schema_id WHERE o.type IN ('P','FN','TF','IF','V') ORDER BY s.name")
    schemas = [r[0] for r in cur.fetchall() if not is_mssql_system_schema(r[0])]
    conn.close()
    return schemas

def discover_schemas_spg():
    conn = spg_conn(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT schemaname FROM pg_views UNION SELECT DISTINCT n.nspname FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid ORDER BY 1")
    schemas = [r[0] for r in cur.fetchall() if not is_spg_system_schema(r[0])]
    conn.close()
    return schemas

SCHEMAS_TO_AUDIT = list(set(discover_schemas_mssql() + discover_schemas_spg()))

# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_mssql_schema(schema):
    conn = ms_conn(); cur = conn.cursor(as_dict=True)
    result = {}
    try:
        cur.execute("""
            SELECT p.name AS obj_name, 'PROCEDURE' AS obj_type
            FROM sys.procedures p JOIN sys.schemas s ON p.schema_id=s.schema_id
            WHERE s.name=%s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': 'PROCEDURE'}

        cur.execute("""
            SELECT v.name AS obj_name, 'VIEW' AS obj_type
            FROM sys.views v JOIN sys.schemas s ON v.schema_id=s.schema_id
            WHERE s.name=%s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': 'VIEW'}
    except: pass
    conn.close()
    return result

def discover_spg_schema(schema):
    conn = spg_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    result = {}
    try:
        cur.execute("""
            SELECT p.proname AS obj_name,
                   CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS obj_type
            FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid
            WHERE n.nspname=%s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': r['obj_type']}

        cur.execute("""
            SELECT c.relname AS obj_name, 'VIEW' AS obj_type
            FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid
            WHERE c.relkind='v' AND n.nspname=%s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': 'VIEW'}
    except: pass
    conn.close()
    return result

# ── Validators ────────────────────────────────────────────────────────────────

def validate_view(schema, name):
    try:
        ms = ms_conn(); mc = ms.cursor()
        mc.execute("SELECT TOP 0 * FROM %s.%s" % (schema, name))
        ms_cols = [d[0].lower() for d in mc.description]
        mc.execute("SELECT COUNT(*) FROM %s.%s" % (schema, name))
        ms_cnt = mc.fetchone()[0]; ms.close()
    except Exception as e:
        return {'verdict':'ERROR','issues':['MSSQL_ERR:%s'%str(e)[:100]],'ms_rows':None,'spg_rows':None}

    try:
        sp = spg_conn(); sc = sp.cursor()
        sc.execute('SELECT * FROM %s."%s" LIMIT 0' % (schema, name))
        spg_cols = [d[0].lower() for d in sc.description]
        sc.execute('SELECT COUNT(*) FROM %s."%s"' % (schema, name))
        spg_cnt = sc.fetchone()[0]; sp.close()
    except Exception as e:
        return {'verdict':'ERROR','issues':['SPG_ERR:%s'%str(e)[:120]],'ms_rows':ms_cnt,'spg_rows':None}

    issues, verdict = [], 'PASS'
    if ms_cnt != spg_cnt:
        issues.append('ROW_COUNT: MSSQL=%d SPG=%d' % (ms_cnt, spg_cnt)); verdict = 'FAIL'
    only_ms  = sorted(set(ms_cols) - set(spg_cols))
    only_spg = sorted(set(spg_cols) - set(ms_cols))
    if only_ms:  issues.append('COLS_ONLY_IN_MSSQL: %s' % only_ms); verdict = 'FAIL'
    if only_spg: issues.append('COLS_ONLY_IN_SPG: %s'  % only_spg); verdict = 'FAIL' if verdict!='PASS' else 'WARN'
    return {'verdict':verdict,'issues':issues,'ms_rows':ms_cnt,'spg_rows':spg_cnt,
            'ms_cols':len(ms_cols),'spg_cols':len(spg_cols)}

def get_ms_params(schema, name):
    try:
        conn = ms_conn(); cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT p.parameter_id, p.name AS pname, t.name AS tname
            FROM sys.procedures pr
            JOIN sys.schemas s ON pr.schema_id=s.schema_id
            JOIN sys.parameters p ON pr.object_id=p.object_id
            JOIN sys.types t ON p.user_type_id=t.user_type_id
            WHERE s.name=%s AND LOWER(pr.name)=%s ORDER BY p.parameter_id
        """, (schema, name))
        rows = cur.fetchall(); conn.close()
        return [{'name':r['pname'].lstrip('@').lower(),'type':r['tname']} for r in rows]
    except Exception as e:
        return 'ERR:%s' % str(e)[:80]

def get_spg_params(schema, name):
    try:
        conn = spg_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT pa.ordinal_position, pa.parameter_name, pa.data_type
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name=pa.specific_name
            WHERE r.routine_schema=%s AND LOWER(r.routine_name)=%s
            ORDER BY pa.ordinal_position
        """, (schema, name))
        rows = cur.fetchall(); conn.close()
        return [{'name':(r['parameter_name'] or '').lstrip('_').lower(),'type':r['data_type'] or ''} for r in rows]
    except Exception as e:
        return 'ERR:%s' % str(e)[:80]

def strip_p(n): return n[2:] if n.startswith('p_') else n

def validate_proc(schema, name):
    ms_p  = get_ms_params(schema, name)
    spg_p = get_spg_params(schema, name)
    if isinstance(ms_p,  str): return {'verdict':'ERROR','issues':['MSSQL_ERR:%s'%ms_p],'ms_p':'?','spg_p':'?'}
    if isinstance(spg_p, str): return {'verdict':'ERROR','issues':['SPG_ERR:%s'%spg_p],'ms_p':len(ms_p),'spg_p':'?'}

    issues, verdict = [], 'PASS'
    if len(ms_p) != len(spg_p):
        issues.append('PARAM_COUNT: MSSQL=%d SPG=%d' % (len(ms_p), len(spg_p))); verdict = 'FAIL'
    else:
        all_ms  = [p['name'] for p in ms_p]
        all_spg = [strip_p(p['name']) for p in spg_p]
        if set(all_ms) == set(all_spg) and all_ms != all_spg:
            mis = ['pos%d: MSSQL=%s SPG=%s'%(i+1,a,b) for i,(a,b) in enumerate(zip(all_ms,all_spg)) if a!=b]
            issues.append('PARAM_ORDER_SWAPPED (%d): %s%s'%(len(mis),str(mis[:2])[1:-1],'...' if len(mis)>2 else ''))
            verdict = 'FAIL'
        elif set(all_ms) != set(all_spg):
            diff = ['pos%d MSSQL=%s SPG=%s'%(i+1,a,b) for i,(a,b) in enumerate(zip(all_ms,all_spg)) if a!=b]
            issues.append('PARAM_NAMES_DIFFER: %s%s'%(str(diff[:2])[1:-1],'...' if len(diff)>2 else ''))
            verdict = 'FAIL'
    return {'verdict':verdict,'issues':issues,'ms_p':len(ms_p),'spg_p':len(spg_p)}

# ── Grand totals ──────────────────────────────────────────────────────────────
grand = {'pass':0,'fail':0,'error':0,'warn':0,'missing':0,'spg_only':0}
SEP = "=" * 110

print(SEP)
print("FULL CROSS-SCHEMA VALIDATION REPORT")
print("Schemas: %s" % ', '.join(SCHEMAS_TO_AUDIT))
print(SEP)

for SCHEMA in SCHEMAS_TO_AUDIT:
    ms_obj  = discover_mssql_schema(SCHEMA)
    spg_obj = discover_spg_schema(SCHEMA)

    ms_names  = set(ms_obj.keys())
    spg_names = set(spg_obj.keys())
    matched   = ms_names & spg_names
    only_ms   = ms_names - spg_names
    only_spg  = spg_names - ms_names

    total = len(ms_obj) + len(only_spg)
    print("\n%s" % SEP)
    print("SCHEMA: %s  |  MSSQL=%d  SPG=%d  Matched=%d  Missing-in-SPG=%d  SPG-only=%d" % (
        SCHEMA.upper(), len(ms_obj), len(spg_obj), len(matched), len(only_ms), len(only_spg)))
    print(SEP)

    if not ms_obj and not spg_obj:
        print("  (empty schema — no objects on either side)")
        continue

    def run_one(name):
        ms_info  = ms_obj[name]
        spg_info = spg_obj[name]
        if ms_info['type'] == 'VIEW':
            r = validate_view(SCHEMA, name)
        else:
            r = validate_proc(SCHEMA, name)
        r.update({'name': name, 'type': ms_info['type']})
        return r

    # Run in batches
    BATCH = 10
    all_names   = sorted(matched)
    all_results = []
    for i in range(0, len(all_names), BATCH):
        batch = all_names[i:i+BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(run_one, n): n for n in batch}
            for fut in concurrent.futures.as_completed(futs):
                all_results.append(fut.result())
        sys.stdout.flush()

    order = {'FAIL':0,'ERROR':1,'WARN':2,'PASS':3}
    all_results.sort(key=lambda r: (order.get(r['verdict'],9), r['type'], r['name']))

    # Views
    view_res = [r for r in all_results if r['type']=='VIEW']
    proc_res = [r for r in all_results if r['type'] in ('PROCEDURE','FUNCTION')]

    if view_res:
        print("\n  VIEWS (%d)" % len(view_res))
        print("  %-55s %10s %10s  %-7s  ISSUE" % ("VIEW","MSSQL_ROWS","SPG_ROWS","VERDICT"))
        print("  " + "-"*100)
        for r in view_res:
            ms_r  = r.get('ms_rows'); spg_r = r.get('spg_rows')
            print("  %-55s %10s %10s  %-7s" % (
                '%s.%s'%(SCHEMA,r['name']),
                str(ms_r) if ms_r is not None else 'ERR',
                str(spg_r) if spg_r is not None else 'ERR',
                r['verdict']))
            for iss in r['issues']: print("    └─ %s" % iss)

    if proc_res:
        print("\n  PROCEDURES / FUNCTIONS (%d)" % len(proc_res))
        print("  %-58s %-10s %6s %6s  %-7s  ISSUE" % ("OBJECT","TYPE","MS_P","SPG_P","VERDICT"))
        print("  " + "-"*105)
        for r in proc_res:
            print("  %-58s %-10s %6s %6s  %-7s" % (
                '%s.%s'%(SCHEMA,r['name']), r['type'],
                str(r.get('ms_p','?')), str(r.get('spg_p','?')), r['verdict']))
            for iss in r['issues']: print("    └─ %s" % iss)

    # Missing in SPG
    if only_ms:
        print("\n  MISSING IN SPG (%d):" % len(only_ms))
        for n in sorted(only_ms):
            print("    MISSING  %s.%-50s  %s" % (SCHEMA, ms_obj[n]['name'], ms_obj[n]['type']))

    # SPG only
    if only_spg:
        print("\n  NEW IN SPG ONLY (%d):" % len(only_spg))
        for n in sorted(only_spg):
            print("    SPG_ONLY  %s.%-50s  %s" % (SCHEMA, spg_obj[n]['name'], spg_obj[n]['type']))

    # Schema totals
    v_pass = sum(1 for r in view_res if r['verdict']=='PASS')
    v_fail = sum(1 for r in view_res if r['verdict'] in ('FAIL','ERROR','WARN'))
    p_pass = sum(1 for r in proc_res if r['verdict']=='PASS')
    p_fail = sum(1 for r in proc_res if r['verdict'] in ('FAIL','ERROR'))
    print("\n  SCHEMA SUMMARY: Views PASS=%d FAIL=%d | Procs PASS=%d FAIL=%d | Missing=%d | SPG-only=%d" % (
        v_pass, v_fail, p_pass, p_fail, len(only_ms), len(only_spg)))

    grand['pass']    += v_pass + p_pass
    grand['fail']    += v_fail + p_fail
    grand['missing'] += len(only_ms)
    grand['spg_only']+= len(only_spg)

print("\n" + SEP)
print("GRAND TOTAL ACROSS ALL SCHEMAS")
print(SEP)
print("  PASS: %d  |  FAIL/ERROR: %d  |  Missing-in-SPG: %d  |  SPG-only: %d" % (
    grand['pass'], grand['fail'], grand['missing'], grand['spg_only']))
print(SEP)
