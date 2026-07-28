"""
validate_batch.py — View validation: MSSQL vs Postgres (generic).

Dynamically discovers all views in both databases.
No hardcoded schema names, view names, or connection details.

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
Optional env vars: See config.py for full list.
"""
import os, sys, re, hashlib, decimal, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, BATCH_SIZE, OUTPUT_DIR,
                    VIEW_LOG_FILE, is_spg_system_schema, is_mssql_system_schema,
                    check_required)
import pymssql, psycopg2
import reporting as rpt

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Schema discovery ─────────────────────────────────────────────────────────

def discover_mssql_views():
    """Return all (schema, view_name) pairs from MSSQL, excluding system schemas."""
    conn = pymssql.connect(**MSSQL_CONF)
    cur = conn.cursor()
    cur.execute("""
        SELECT s.name, v.name
        FROM sys.views v
        JOIN sys.schemas s ON v.schema_id = s.schema_id
        ORDER BY s.name, v.name
    """)
    views = [(r[0], r[1]) for r in cur.fetchall()
             if not is_mssql_system_schema(r[0])]
    conn.close()
    return views

def discover_spg_views():
    """Return all (schema, view_name) pairs from Postgres, excluding system schemas."""
    conn = psycopg2.connect(**SPG_CONF)
    cur = conn.cursor()
    cur.execute("""
        SELECT schemaname, viewname
        FROM pg_views
        ORDER BY schemaname, viewname
    """)
    views = [(r[0], r[1]) for r in cur.fetchall()
             if not is_spg_system_schema(r[0])]
    conn.close()
    return views


# ── Per-view query ───────────────────────────────────────────────────────────

def _normalize_val(v):
    """Normalize a value for hashing — strips trailing zeros from Decimals."""
    if v is None:
        return ''
    if isinstance(v, decimal.Decimal):
        return str(v.normalize())
    return str(v)

def _row_hash(rows):
    canon = '\n'.join('|'.join(_normalize_val(v) for v in r)
                      for r in sorted(rows, key=lambda r: str(r)))
    return hashlib.md5(canon.encode()).hexdigest()

def query_mssql_view(schema, view_name):
    try:
        conn = pymssql.connect(**MSSQL_CONF)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{view_name}]")
        count = cur.fetchone()[0]
        cur.execute(f"SELECT TOP 0 * FROM [{schema}].[{view_name}]")
        cols = [d[0].lower() for d in cur.description]
        rows = []
        if count > 0:
            cur.execute(f"SELECT * FROM [{schema}].[{view_name}]")
            rows = cur.fetchall()
        conn.close()
        return {'count': count, 'cols': cols, 'rows': rows, 'error': None}
    except Exception as e:
        return {'count': None, 'cols': [], 'rows': [], 'error': str(e)[:150]}

def query_spg_view(schema, view_name):
    try:
        conn = psycopg2.connect(**SPG_CONF)
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{view_name}"')
        count = cur.fetchone()[0]
        cur.execute(f'SELECT * FROM "{schema}"."{view_name}" LIMIT 0')
        cols = [d[0].lower() for d in cur.description]
        rows = []
        if count > 0:
            cur.execute(f'SELECT * FROM "{schema}"."{view_name}"')
            rows = cur.fetchall()
        conn.close()
        return {'count': count, 'cols': cols, 'rows': rows, 'error': None}
    except Exception as e:
        return {'count': None, 'cols': [], 'rows': [], 'error': str(e)[:150]}


def validate_view(schema_view):
    schema, view_name = schema_view
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_ms  = ex.submit(query_mssql_view, schema, view_name)
        f_spg = ex.submit(query_spg_view,   schema, view_name)
        try:    ms  = f_ms.result(timeout=60)
        except Exception as e: ms  = {'count': None, 'cols': [], 'rows': [], 'error': f'TIMEOUT: {e}'}
        try:    spg = f_spg.result(timeout=60)
        except Exception as e: spg = {'count': None, 'cols': [], 'rows': [], 'error': f'TIMEOUT: {e}'}

    verdict, issues = 'PASS', []

    if ms['error']:  issues.append(f'MSSQL_ERR: {ms["error"]}');  verdict = 'ERROR'
    if spg['error']: issues.append(f'SPG_ERR: {spg["error"]}');   verdict = 'ERROR'

    if verdict != 'ERROR':
        # Row count
        if ms['count'] != spg['count']:
            issues.append(f'ROW_COUNT mismatch: MSSQL={ms["count"]} SPG={spg["count"]}')
            verdict = 'FAIL'

        # Column schema
        ms_cols, spg_cols = set(ms['cols']), set(spg['cols'])
        only_ms  = ms_cols - spg_cols
        only_spg = spg_cols - ms_cols
        if only_ms:
            issues.append(f'COLS_ONLY_IN_MSSQL: {sorted(only_ms)}')
            verdict = 'FAIL'
        if only_spg:
            issues.append(f'COLS_ONLY_IN_SPG: {sorted(only_spg)}')
            verdict = 'WARN' if verdict == 'PASS' else verdict

        # Data hash (only when rows exist and counts match)
        if verdict == 'PASS' and ms['count'] == spg['count'] and ms['count'] > 0:
            if _row_hash(ms['rows']) != _row_hash(spg['rows']):
                ms_set  = set(str(r) for r in ms['rows'])
                spg_set = set(str(r) for r in spg['rows'])
                issues.append(f'DATA_HASH_MISMATCH ({ms["count"]} rows compared)')
                diff_ms  = ms_set  - spg_set
                diff_spg = spg_set - ms_set
                if diff_ms:  issues.append(f'ROWS_IN_MSSQL_NOT_IN_SPG: {len(diff_ms)}')
                if diff_spg: issues.append(f'ROWS_IN_SPG_NOT_IN_MSSQL: {len(diff_spg)}')
                verdict = 'FAIL'

    return {
        'object': f'{schema}.{view_name}', 'type': 'VIEW', 'verdict': verdict,
        'mssql_rows': ms.get('count'), 'spg_rows': spg.get('count'), 'issues': issues,
        'schema': schema, 'name': view_name,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    import sys as _sys
    # Optionally write to log file
    log_path = VIEW_LOG_FILE
    log_fh = open(log_path, 'w', encoding='utf-8') if log_path else None

    def out(line=''):
        print(line)
        if log_fh: log_fh.write(line + '\n')

    out(f"Discovering views in MSSQL [{MSSQL_CONF['database']}] and Postgres [{SPG_CONF['host'][:40]}]...")
    ms_views  = set((s.lower(), v.lower()) for s, v in discover_mssql_views())
    spg_views = set((s.lower(), v.lower()) for s, v in discover_spg_views())

    matched   = sorted(ms_views & spg_views)
    ms_only   = sorted(ms_views - spg_views)
    spg_only  = sorted(spg_views - ms_views)

    out(f"  MSSQL views: {len(ms_views)}  |  SPG views (business): {len(spg_views)}")
    out(f"  Matched: {len(matched)}  |  MSSQL-only: {len(ms_only)}  |  SPG-only: {len(spg_only)}")

    # ── Execute all view validations, buffering results ───────────────────
    results = []

    for i in range(0, len(matched), BATCH_SIZE):
        batch = matched[i: i + BATCH_SIZE]
        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
            futs = {pool.submit(validate_view, sv): sv for sv in batch}
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

    # Add MSSQL-only and SPG-only as synthetic rows (for the summary)
    for s, v in ms_only:
        results.append({'object': f'{s}.{v}', 'type': 'VIEW', 'verdict': 'MSSQL_ONLY',
                        'mssql_rows': None, 'spg_rows': None, 'issues': [], 'schema': s, 'name': v})
    for s, v in spg_only:
        results.append({'object': f'{s}.{v}', 'type': 'VIEW', 'verdict': 'SPG_ONLY',
                        'mssql_rows': None, 'spg_rows': None, 'issues': [], 'schema': s, 'name': v})

    # ── Summary table FIRST ───────────────────────────────────────────────
    summary_rows = [{'schema': r['schema'], 'object_type': 'VIEW', 'verdict': r['verdict']}
                    for r in results]
    import io
    summary_buf = io.StringIO()
    rpt.print_summary_table(
        summary_rows,
        source_db=MSSQL_CONF['database'],
        target_db=SPG_CONF['host'].split('.')[0],
        object_type_label='VIEW',
        out=summary_buf,
    )
    out(summary_buf.getvalue())

    # ── Detail rows (sorted: failures first, then alpha) ──────────────────
    V_ORDER = {'FAIL':0,'ERROR':1,'WARN':2,'MSSQL_ONLY':3,'SPG_ONLY':4,'PASS':5}
    results_sorted = sorted(results, key=lambda r: (V_ORDER.get(r['verdict'], 9), r['object']))

    if ms_only:
        out("MSSQL-ONLY (in MSSQL but not found in SPG):")
        for s, v in sorted(ms_only): out(f"  {s}.{v}")
        out("")

    if spg_only:
        out("SPG-ONLY (in SPG but not in MSSQL):")
        for s, v in sorted(spg_only): out(f"  {s}.{v}")
        out("")

    out(f"{'OBJECT':<60} {'TYPE':<5} {'MSSQL':>10} {'SPG':>10}  VERDICT")
    out("-" * 100)

    totals = {}
    matched_results = [r for r in results_sorted if r['verdict'] not in ('MSSQL_ONLY', 'SPG_ONLY')]
    for r in matched_results:
        row_ms  = r['mssql_rows'] if r['mssql_rows'] is not None else 'ERR'
        row_spg = r['spg_rows']   if r['spg_rows']   is not None else 'ERR'
        out(f"{r['object']:<60} {r['type']:<5} {str(row_ms):>10} {str(row_spg):>10}  {r['verdict']}")
        for iss in r['issues']: out(f"  └─ {iss}")
        totals[r['verdict']] = totals.get(r['verdict'], 0) + 1

    totals['MSSQL_ONLY'] = len(ms_only)
    totals['SPG_ONLY']   = len(spg_only)

    out("\n" + "=" * 100)
    out("VIEWS SUMMARY — "
        f"PASS:{totals.get('PASS',0)}  "
        f"FAIL:{totals.get('FAIL',0)}  "
        f"WARN:{totals.get('WARN',0)}  "
        f"ERROR:{totals.get('ERROR',0)}  "
        f"MSSQL_ONLY:{totals.get('MSSQL_ONLY',0)}  "
        f"SPG_ONLY:{totals.get('SPG_ONLY',0)}")

    if log_fh:
        log_fh.close()
        print(f"Log written to: {log_path}")

    return results, totals

if __name__ == '__main__':
    main()
