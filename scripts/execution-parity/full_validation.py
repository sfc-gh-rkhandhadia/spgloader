"""
Cross-schema structural validator (all schemas, auto-discovered)
=================================================================
Discovers every user schema on both MSSQL and SPG from the system catalog,
then checks existence, parameter parity (procedures/functions), and view row
count + column parity across all schemas.

Results print to stdout; nothing is written to the validation audit tables.

Use this script for:
  - Fast ad-hoc structural survey across all schemas during development
  - Checking whether objects were converted at all (MISSING vs SPG_ONLY)
  - Debugging a specific schema without running the full pipeline

Do NOT use this script for:
  - Behavioral execution testing   -> use run.py --procs
  - Producing stored/audited results -> use run.py (writes to validation.validation_result)
  - Generating reports              -> use generate_validation_markdown.py / generate_migration_report.py

Limitations:
  - Structural parity only: checks existence, parameter counts/names, and view
    row counts. Does not execute stored procedures or functions.
  - Results are not persisted to validation.validation_result
"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2, psycopg2.extras, concurrent.futures, sys, os
from config import MSSQL_CONF, SPG_CONF, is_mssql_system_schema, is_spg_system_schema, check_required
from source_adapter import build_adapter

_src_adapter = build_adapter()

check_required()

BATCH = 24  # matches parity thread parallelism
SEP   = "=" * 110

def ms_conn():  return _src_adapter.connect()
def spg_conn(): return psycopg2.connect(**SPG_CONF)


# ── Schema discovery ──────────────────────────────────────────────────────────

def discover_schemas_mssql():
    return _src_adapter.get_schemas()

def discover_schemas_spg():
    conn = spg_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT schemaname FROM pg_views
        UNION
        SELECT DISTINCT n.nspname FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        ORDER BY 1
    """)
    schemas = [r[0] for r in cur.fetchall() if not is_spg_system_schema(r[0])]
    conn.close()
    return schemas


# ── Object discovery per schema ───────────────────────────────────────────────

def discover_mssql_schema(schema, ms_schema_name=None):
    """Discover procedures, functions, and views in a source DB schema.
    schema       — lowercase key used for matching.
    ms_schema_name — original source casing (MSSQL is case-sensitive in sys.* queries).
    """
    src_name = ms_schema_name or schema
    result = {}
    try:
        for rinfo in _src_adapter.get_routines(src_name):
            obj_kind = 'FUNCTION' if rinfo['type'] in ('FN','TF','IF','FUNCTION') else 'PROCEDURE'
            result[rinfo['name'].lower()] = {'name': rinfo['name'], 'type': obj_kind}
    except Exception:
        pass
    # Views: use INFORMATION_SCHEMA for all source types
    try:
        conn = _src_adapter.connect()
        cur  = conn.cursor()
        if _src_adapter.source_type == 'mssql':
            cur.execute("""
                SELECT v.name AS obj_name FROM sys.views v
                JOIN sys.schemas s ON v.schema_id = s.schema_id
                WHERE s.name = %s
            """, (src_name,))
        else:
            cur.execute("""
                SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS
                WHERE TABLE_SCHEMA = %s
            """, (src_name,))
        for r in cur.fetchall():
            name = r[0]
            result[name.lower()] = {'name': name, 'type': 'VIEW'}
        conn.close()
    except Exception:
        pass
    return result

def discover_spg_schema(schema):
    conn = spg_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    result = {}
    try:
        cur.execute("""
            SELECT p.proname AS obj_name,
                   CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS obj_type,
                   p.pronargs AS param_count
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname = %s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': r['obj_type']}

        cur.execute("""
            SELECT c.relname AS obj_name, 'VIEW' AS obj_type
            FROM pg_class c
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relkind = 'v' AND n.nspname = %s
        """, (schema,))
        for r in cur.fetchall():
            result[r['obj_name'].lower()] = {'name': r['obj_name'], 'type': 'VIEW'}
    except Exception:
        pass
    conn.close()
    return result


# ── Validators ────────────────────────────────────────────────────────────────

def validate_view(schema, name):
    try:
        ms = _src_adapter.connect(); mc = ms.cursor()
        if _src_adapter.source_type == 'mssql':
            mc.execute("SELECT TOP 0 * FROM [%s].[%s]" % (schema, name))
        elif _src_adapter.source_type in ('mysql', 'mariadb'):
            mc.execute("SELECT * FROM `%s`.`%s` LIMIT 0" % (schema, name))
        else:
            mc.execute('SELECT * FROM "%s"."%s" WHERE 1=0' % (schema, name))
        ms_cols  = [d[0].lower() for d in mc.description]
        if _src_adapter.source_type == 'mssql':
            mc.execute("SELECT COUNT(*) FROM [%s].[%s]" % (schema, name))
        elif _src_adapter.source_type in ('mysql', 'mariadb'):
            mc.execute("SELECT COUNT(*) FROM `%s`.`%s`" % (schema, name))
        else:
            mc.execute('SELECT COUNT(*) FROM "%s"."%s"' % (schema, name))
        ms_count = mc.fetchone()[0]
        ms.close()
    except Exception as e:
        return {'verdict': 'ERROR', 'issues': ['MSSQL_ERR: %s' % str(e)[:100]],
                'ms_rows': None, 'spg_rows': None}

    try:
        sp = spg_conn(); sc = sp.cursor()
        sc.execute('SELECT * FROM "%s"."%s" LIMIT 0' % (schema, name))
        spg_cols  = [d[0].lower() for d in sc.description]
        sc.execute('SELECT COUNT(*) FROM "%s"."%s"' % (schema, name))
        spg_count = sc.fetchone()[0]
        sp.close()
    except Exception as e:
        return {'verdict': 'ERROR', 'issues': ['SPG_ERR: %s' % str(e)[:120]],
                'ms_rows': ms_count, 'spg_rows': None}

    issues, verdict = [], 'PASS'
    if ms_count != spg_count:
        issues.append('ROW_COUNT: MSSQL=%d SPG=%d' % (ms_count, spg_count))
        verdict = 'FAIL'
    only_ms  = sorted(set(ms_cols) - set(spg_cols))
    only_spg = sorted(set(spg_cols) - set(ms_cols))
    if only_ms:
        issues.append('COLS_ONLY_IN_MSSQL: %s' % only_ms)
        verdict = 'FAIL'
    if only_spg:
        issues.append('COLS_ONLY_IN_SPG: %s' % only_spg)
        verdict = 'FAIL'
    return {'verdict': verdict, 'issues': issues, 'ms_rows': ms_count, 'spg_rows': spg_count}

def get_ms_params(schema, name):
    try:
        params = _src_adapter.get_parameters(schema, name)
        if isinstance(params, str):
            return 'ERR:%s' % params[:80]
        return [{'name': p['name'], 'type': p['type_name']} for p in params]
    except Exception as e:
        return 'ERR:%s' % str(e)[:80]

def get_spg_params(schema, name):
    try:
        conn = spg_conn(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT pa.ordinal_position, pa.parameter_name, pa.data_type, pa.parameter_mode
            FROM information_schema.routines r
            JOIN information_schema.parameters pa ON r.specific_name = pa.specific_name
            WHERE r.routine_schema = %s AND LOWER(r.routine_name) = %s
            ORDER BY pa.ordinal_position
        """, (schema, name))
        rows = cur.fetchall(); conn.close()
        return [{'name': (r['parameter_name'] or '').lstrip('_').lower(), 'type': r['data_type'] or ''}
                for r in rows if (r['parameter_mode'] or 'IN') == 'IN']
    except Exception as e:
        return 'ERR:%s' % str(e)[:80]

def strip_p(n):
    if n.startswith('par_'): return n[4:]
    if n.startswith('p_'):   return n[2:]
    return n

def validate_proc(schema, name):
    ms_p  = get_ms_params(schema, name)
    spg_p = get_spg_params(schema, name)
    if isinstance(ms_p,  str): return {'verdict': 'ERROR', 'issues': ['MSSQL_ERR:%s' % ms_p], 'ms_p': '?', 'spg_p': '?'}
    if isinstance(spg_p, str): return {'verdict': 'ERROR', 'issues': ['SPG_ERR:%s'  % spg_p], 'ms_p': len(ms_p), 'spg_p': '?'}

    issues, verdict = [], 'PASS'
    if len(ms_p) != len(spg_p):
        issues.append('PARAM_COUNT: MSSQL=%d SPG=%d' % (len(ms_p), len(spg_p)))
        verdict = 'FAIL'
    else:
        all_ms  = [p['name'].lower() for p in ms_p]
        all_spg = [strip_p(p['name']) for p in spg_p]
        if set(all_ms) == set(all_spg) and all_ms != all_spg:
            mis = ['pos%d: MSSQL=%s SPG=%s' % (i+1, a, b) for i,(a,b) in enumerate(zip(all_ms, all_spg)) if a != b]
            issues.append('PARAM_ORDER_SWAPPED (%d): %s%s' % (len(mis), str(mis[:2])[1:-1], '...' if len(mis) > 2 else ''))
            verdict = 'FAIL'
        elif set(all_ms) != set(all_spg):
            diff = ['pos%d MSSQL=%s SPG=%s' % (i+1, a, b) for i,(a,b) in enumerate(zip(all_ms, all_spg)) if a != b]
            issues.append('PARAM_NAMES_DIFFER: %s%s' % (str(diff[:2])[1:-1], '...' if len(diff) > 2 else ''))
            verdict = 'FAIL'
    return {'verdict': verdict, 'issues': issues, 'ms_p': len(ms_p), 'spg_p': len(spg_p)}


# ── Per-schema runner ─────────────────────────────────────────────────────────

def run_schema(schema, grand, schema_results=None, exclude_fqns=None, ms_schema_name=None):
    # schema is the lowercase key; ms_schema_name is the original source casing (MSSQL case-sensitive)
    ms_obj  = discover_mssql_schema(schema, ms_schema_name=ms_schema_name)
    spg_obj = discover_spg_schema(schema)

    ms_names  = set(ms_obj.keys())
    spg_names = set(spg_obj.keys())

    # Apply FQN exclusions (user opted out of testing legacy objects)
    excluded_names: set = set()
    if exclude_fqns:
        for name in list(ms_names):
            fqn = ('%s.%s' % (schema, name)).lower()
            if fqn in exclude_fqns or name.lower() in exclude_fqns:
                excluded_names.add(name)
        ms_names  = ms_names  - excluded_names
        spg_names = spg_names - excluded_names

    matched   = ms_names & spg_names
    only_ms   = ms_names - spg_names
    only_spg  = spg_names - ms_names

    print("\n" + SEP)
    print("SCHEMA: %s  |  MSSQL=%d  SPG=%d  Matched=%d  Missing-in-SPG=%d  SPG-only=%d" % (
        schema.upper(), len(ms_obj), len(spg_obj), len(matched), len(only_ms), len(only_spg)))
    print(SEP)

    if not ms_obj and not spg_obj:
        print("  (empty schema — no objects on either side)")
        return

    # Initialise schema_results entry for JSON output
    if schema_results is not None:
        schema_results[schema] = {
            'results': [],
            'missing_objects': [{'fqn': '%s.%s' % (schema, n), 'name': n, 'type': ms_obj[n]['type']} for n in sorted(only_ms)],
            'excluded_objects': [{'fqn': '%s.%s' % (schema, n), 'name': n, 'type': ms_obj[n]['type']} for n in sorted(excluded_names)],
        }

    def run_one(name):
        obj_type = ms_obj[name]['type']
        if obj_type == 'VIEW':
            r = validate_view(schema, name)
        else:
            r = validate_proc(schema, name)
        r.update({'name': name, 'type': obj_type, 'schema': schema, 'fqn': '%s.%s' % (schema, name)})
        return r

    all_results = []
    for i in range(0, len(matched), BATCH):
        batch = sorted(matched)[i:i+BATCH]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(run_one, n): n for n in batch}
            for fut in concurrent.futures.as_completed(futs):
                all_results.append(fut.result())
        sys.stdout.flush()

    order = {'FAIL': 0, 'ERROR': 1, 'PASS': 2}
    all_results.sort(key=lambda r: (order.get(r['verdict'], 9), r['type'], r['name']))

    # Store in collector for JSON output
    if schema_results is not None and schema in schema_results:
        schema_results[schema]['results'] = all_results
        schema_results[schema]['pass']    = sum(1 for r in all_results if r['verdict'] == 'PASS')
        schema_results[schema]['fail']    = sum(1 for r in all_results if r['verdict'] in ('FAIL', 'ERROR'))
        schema_results[schema]['missing'] = len(only_ms)
        schema_results[schema]['spg_only'] = len(only_spg)

    view_res = [r for r in all_results if r['type'] == 'VIEW']
    proc_res = [r for r in all_results if r['type'] in ('PROCEDURE', 'FUNCTION')]

    if view_res:
        print("\n  VIEWS (%d)" % len(view_res))
        print("  %-55s %10s %10s  %-7s  ISSUES" % ("VIEW", "MSSQL_ROWS", "SPG_ROWS", "VERDICT"))
        print("  " + "-" * 100)
        for r in view_res:
            ms_r = r.get('ms_rows'); spg_r = r.get('spg_rows')
            print("  %-55s %10s %10s  %-7s" % (
                '%s.%s' % (schema, r['name']),
                str(ms_r) if ms_r is not None else 'ERR',
                str(spg_r) if spg_r is not None else 'ERR',
                r['verdict']))
            for iss in r.get('issues', []):
                print("    └─ %s" % iss)

    if proc_res:
        print("\n  PROCEDURES / FUNCTIONS (%d)" % len(proc_res))
        print("  %-58s %-10s %6s %6s  %-7s  ISSUES" % ("OBJECT", "TYPE", "MS_P", "SPG_P", "VERDICT"))
        print("  " + "-" * 105)
        for r in proc_res:
            print("  %-58s %-10s %6s %6s  %-7s" % (
                '%s.%s' % (schema, r['name']), r['type'],
                str(r.get('ms_p', '?')), str(r.get('spg_p', '?')), r['verdict']))
            for iss in r.get('issues', []):
                print("    └─ %s" % iss)

    if only_ms:
        print("\n  MISSING IN SPG (%d):" % len(only_ms))
        for n in sorted(only_ms):
            print("    MISSING  %s.%-50s  %s" % (schema, ms_obj[n]['name'], ms_obj[n]['type']))

    if only_spg:
        print("\n  NEW IN SPG ONLY (%d):" % len(only_spg))
        for n in sorted(only_spg):
            print("    SPG_ONLY  %s.%-50s  %s" % (schema, spg_obj[n]['name'], spg_obj[n]['type']))

    v_pass = sum(1 for r in view_res if r['verdict'] == 'PASS')
    v_fail = sum(1 for r in view_res if r['verdict'] in ('FAIL', 'ERROR'))
    p_pass = sum(1 for r in proc_res if r['verdict'] == 'PASS')
    p_fail = sum(1 for r in proc_res if r['verdict'] in ('FAIL', 'ERROR'))
    print("\n  SCHEMA SUMMARY: Views PASS=%d FAIL=%d | Procs PASS=%d FAIL=%d | Missing=%d | SPG-only=%d" % (
        v_pass, v_fail, p_pass, p_fail, len(only_ms), len(only_spg)))

    grand['pass']     += v_pass + p_pass
    grand['fail']     += v_fail + p_fail
    grand['missing']  += len(only_ms)
    grand['spg_only'] += len(only_spg)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse, json as _json
    parser = argparse.ArgumentParser(description="Cross-schema structural validator")
    parser.add_argument("--inventory", help="Path to object_inventory.json (unused, accepted for compat)")
    parser.add_argument("--output-dir", help="Directory to write parity_results.json")
    parser.add_argument("--exclude-fqns-file",
        help="JSON file with {excluded_fqns: [...]} — skip these FQNs from the equivalence test")
    args, _ = parser.parse_known_args()

    # Load FQN exclusions
    exclude_fqns: set = set()
    if args.exclude_fqns_file and os.path.exists(args.exclude_fqns_file):
        with open(args.exclude_fqns_file) as ef:
            edata = _json.load(ef)
        for fqn in edata.get('excluded_fqns', []):
            exclude_fqns.add(fqn.lower())
            exclude_fqns.add(fqn.split('.')[-1].lower())
        if exclude_fqns:
            print("[FILTER] Excluding %d FQN(s) from equivalence test" % len(edata.get('excluded_fqns', [])))

    print("[NOTICE] full_validation.py is a structural spot-check tool. "
          "Results are not saved to audit tables. Use run.py for the full pipeline.")

    print("\nDiscovering schemas from system catalog...")
    ms_schemas  = discover_schemas_mssql()
    spg_schemas = discover_schemas_spg()
    # Normalize: source schemas may be mixed-case (e.g. MSSQL HumanResources),
    # SPG always folds to lowercase. Build lowercase → original mapping.
    ms_lower_map = {s.lower(): s for s in ms_schemas}
    spg_lower_set = {s.lower() for s in spg_schemas}
    all_schemas = sorted(set(list(ms_lower_map.keys()) + list(spg_lower_set)))
    print("Source schemas: %s" % ms_schemas)
    print("SPG    schemas: %s" % spg_schemas)
    print("Union (normalised): %s" % all_schemas)

    grand = {'pass': 0, 'fail': 0, 'missing': 0, 'spg_only': 0}
    schema_results = {}  # collector for JSON output

    print("\n" + SEP)
    print("FULL CROSS-SCHEMA VALIDATION REPORT")
    print("Schemas: %s" % ', '.join(all_schemas))
    print(SEP)

    for schema in all_schemas:
        ms_orig = ms_lower_map.get(schema, schema)  # original source casing
        run_schema(schema, grand, schema_results,
                   exclude_fqns=exclude_fqns if exclude_fqns else None,
                   ms_schema_name=ms_orig)

    print("\n" + SEP)
    print("GRAND TOTAL ACROSS ALL SCHEMAS")
    print(SEP)
    print("  PASS: %d  |  FAIL/ERROR: %d  |  Missing-in-SPG: %d  |  SPG-only: %d" % (
        grand['pass'], grand['fail'], grand['missing'], grand['spg_only']))
    print(SEP)

    grand['excluded'] = sum(len(s.get('excluded_objects', [])) for s in schema_results.values())
    if grand['excluded']:
        print("  Excluded from test (user choice): %d" % grand['excluded'])

    # Write structured JSON output (parity_results.json consumed by html_report.py)
    out_dir = args.output_dir or os.path.join(os.getcwd(), 'parity')
    os.makedirs(out_dir, exist_ok=True)
    results_data = {
        'grand': grand,
        'schemas': {
            sch: {
                'pass':             s.get('pass', 0),
                'fail':             s.get('fail', 0),
                'missing':          s.get('missing', 0),
                'spg_only':         s.get('spg_only', 0),
                'results':          s.get('results', []),
                'missing_objects':  s.get('missing_objects', []),
                'excluded_objects': s.get('excluded_objects', []),
            }
            for sch, s in schema_results.items()
        }
    }
    json_path = os.path.join(out_dir, 'parity_results.json')
    with open(json_path, 'w') as f:
        _json.dump(results_data, f, indent=2)
    print("\nParity results written: %s" % json_path)


if __name__ == "__main__":
    main()
