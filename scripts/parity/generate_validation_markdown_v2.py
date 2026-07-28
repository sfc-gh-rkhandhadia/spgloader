"""
generate_validation_markdown_v2.py — Generate a clean client-ready Markdown report.

Layout for MSSQL → SPG migration validation report:
  1. Header (KPIs)
  2. Migration at a Glance
  3. Validation Runs table
  4. Object Count by Schema and Type
  5. Failure Categories
  6. Remediation Priorities (named)
  7. Appendix — Failed Object Details (by schema)

Usage:
    python3 generate_validation_markdown_v2.py [options]

Options:
    --out-dir   PATH    Output directory (default: ~/Downloads)
    --out-file  NAME    Output filename (default: auto-generated)
    --client    NAME    Client/project name
    --author    NAME    Author name (default: Rekha Khandhadia)
    --run-trig  N       Run number for trigger results (0 = auto)
    --run-view  N       Run number for view results   (0 = auto)
    --run-proc  N       Run number for proc results   (0 = auto)
    --schema-run N      Run number for schema audit   (0 = omit)

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MSSQL_CONF, SPG_CONF, check_required
import pymssql, psycopg2, psycopg2.extras
from datetime import datetime
from collections import defaultdict

check_required()

parser = argparse.ArgumentParser(description='Generate client-ready Markdown validation report')
parser.add_argument('--out-dir',   default=os.environ.get('VALIDATION_OUTPUT_DIR', os.path.expanduser('~/Downloads')))
parser.add_argument('--out-file',  default='')
parser.add_argument('--client',    default='')
parser.add_argument('--author',    default='Rekha Khandhadia')
parser.add_argument('--run-trig',  type=int, default=0)
parser.add_argument('--run-view',  type=int, default=0)
parser.add_argument('--run-proc',  type=int, default=0)
parser.add_argument('--schema-run',type=int, default=0)
args = parser.parse_args()

DATE_STR = datetime.now().strftime('%Y%m%d')
DATE_LONG = datetime.now().strftime('%B %d, %Y')

# ── Verdict helpers ───────────────────────────────────────────────────────────
PASS_V   = {'PASS', 'PASS_DML_PROC'}
FAIL_V   = {'FAIL', 'FAIL_DATA', 'FAIL_CONVERSION', 'SPG_ERROR', 'SPG_NO_RESULTSET', 'MSSQL_ERROR',
            'BOTH_FAILED', 'ERROR', 'WARN', 'MSSQL_ONLY'}
SKIP_V   = {'SKIPPED'}
EXTRA_V  = {'SPG_ONLY'}

def classify(v):
    if v in PASS_V:  return 'pass'
    if v in FAIL_V:  return 'fail'
    if v in SKIP_V:  return 'skip'
    return 'extra'

def pct(p, f, s=0):
    d = p + f + s
    return f'{p/d*100:.0f}%' if d else '—'

def clean(s, n=160):
    if not s: return ''
    s = str(s).replace('\n', ' ').replace('\\n', ' ')
    s = re.sub(r"b'[^']{0,3}|b\"", '', s)
    return s[:n].strip()

# ── Connect and load runs ─────────────────────────────────────────────────────
print('Connecting to SPG audit tables...')
conn = psycopg2.connect(**SPG_CONF)
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("SELECT * FROM validation.v_run_summary ORDER BY run_number")
all_runs = {r['run_number']: dict(r) for r in cur.fetchall()}

if not all_runs:
    print('ERROR: No validation runs found.'); sys.exit(1)

def latest_by_keyword(kw):
    matches = [r for r in all_runs.values() if kw.lower() in (r.get('notes') or '').lower()]
    return matches[-1]['run_number'] if matches else max(all_runs)

rn_trig   = args.run_trig   or latest_by_keyword('trigger')
rn_view   = args.run_view   or latest_by_keyword('view')
rn_proc   = args.run_proc   or latest_by_keyword('procedure')
rn_schema = args.schema_run or 0

print(f'Runs: triggers={rn_trig}  views={rn_view}  procs={rn_proc}  schema={rn_schema or "(none)"}')

# Load results for data validation runs
data_runs = tuple(sorted({rn_trig, rn_view, rn_proc}))
phs = ','.join(['%s'] * len(data_runs))
cur.execute(f'''
    SELECT * FROM validation.validation_result
    WHERE run_number IN ({phs})
    ORDER BY run_number, source_schema, object_type, test_verdict, object_name
''', data_runs)
all_results = [dict(r) for r in cur.fetchall()]
conn.close()

trig_rows = [r for r in all_results if r['run_number'] == rn_trig]
view_rows = [r for r in all_results if r['run_number'] == rn_view]
proc_rows = [r for r in all_results if r['run_number'] == rn_proc]

# ── Fetch MSSQL and SPG object counts ─────────────────────────────────────────
print('Fetching MSSQL and SPG object counts...')
mc  = pymssql.connect(**MSSQL_CONF)
msc = mc.cursor(as_dict=True)

# MSSQL counts by schema+type
msc.execute("""
    SELECT s.name AS sc,
           CASE o.type
             WHEN 'P'  THEN 'PROCEDURE'
             WHEN 'FN' THEN 'FUNCTION'
             WHEN 'IF' THEN 'FUNCTION'
             WHEN 'TF' THEN 'FUNCTION'
             WHEN 'V'  THEN 'VIEW'
             WHEN 'U'  THEN 'TABLE'
             WHEN 'TR' THEN 'TRIGGER'
             ELSE o.type
           END AS obj_type,
           COUNT(*) AS cnt
    FROM sys.objects o
    JOIN sys.schemas s ON o.schema_id = s.schema_id
    WHERE o.type IN ('P','FN','IF','TF','V','U','TR')
      AND s.name NOT IN ('sys','INFORMATION_SCHEMA')
    GROUP BY s.name, o.type
    ORDER BY s.name, o.type
""")
ms_counts_raw = msc.fetchall()
mc.close()

ms_counts = defaultdict(int)
for r in ms_counts_raw:
    ms_counts[(r['sc'].lower(), r['obj_type'])] += r['cnt']

# SPG counts by schema+type
sc2 = psycopg2.connect(**SPG_CONF)
sc2c = sc2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
sc2c.execute("""
    SELECT n.nspname AS sc,
           CASE p.prokind
             WHEN 'f' THEN 'FUNCTION'
             WHEN 'p' THEN 'PROCEDURE'
             ELSE 'FUNCTION'
           END AS obj_type,
           COUNT(*) AS cnt
    FROM pg_proc p
    JOIN pg_namespace n ON p.pronamespace = n.oid
    WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast',
                             'validation','cron','public')
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname NOT LIKE 'snowflake%'
      AND n.nspname NOT LIKE 'lake%'
      AND n.nspname NOT LIKE '__lake%'
    GROUP BY n.nspname, p.prokind
""")
spg_procs_raw = sc2c.fetchall()
sc2c.execute("""
    SELECT schemaname AS sc, 'VIEW' AS obj_type, COUNT(*) AS cnt
    FROM pg_views
    WHERE schemaname NOT IN ('pg_catalog','information_schema','pg_toast',
                              'validation','cron','public')
      AND schemaname NOT LIKE 'pg_%'
      AND schemaname NOT LIKE 'snowflake%'
    GROUP BY schemaname
""")
spg_views_raw = sc2c.fetchall()
sc2c.execute("""
    SELECT n.nspname AS sc, 'TABLE' AS obj_type, COUNT(*) AS cnt
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast',
                             'validation','cron','public')
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname NOT LIKE 'snowflake%'
    GROUP BY n.nspname
""")
spg_tables_raw = sc2c.fetchall()
sc2c.execute("""
    SELECT trigger_schema AS sc, 'TRIGGER' AS obj_type, COUNT(*) AS cnt
    FROM information_schema.triggers
    WHERE trigger_schema NOT IN ('pg_catalog','information_schema','pg_toast',
                                  'validation','cron','public')
    GROUP BY trigger_schema
""")
spg_trig_raw = sc2c.fetchall()
sc2.close()

spg_counts = defaultdict(int)
for r in spg_procs_raw + spg_views_raw + spg_tables_raw + spg_trig_raw:
    spg_counts[(r['sc'].lower(), r['obj_type'])] += r['cnt']

# ── Build schema×type validation results ─────────────────────────────────────
# Group validation results by (schema, type)
val_by_key = defaultdict(lambda: {'pass': 0, 'fail': 0, 'skip': 0, 'extra': 0, 'rows': []})
for r in all_results:
    sc = (r.get('source_schema') or 'unknown').lower()
    ot = (r.get('object_type')   or 'OBJECT').upper()
    # Normalise PROC_TO_FUNC → FUNCTION for schema×type table
    if ot == 'PROC_TO_FUNC':
        ot = 'FUNCTION'
    v  = r.get('test_verdict', '')
    c  = classify(v)
    val_by_key[(sc, ot)][c] += 1
    if c in ('fail', 'skip'):
        val_by_key[(sc, ot)]['rows'].append(r)

# All business schemas (union of MSSQL and validation schemas)
ms_schemas = {sc for sc, _ in ms_counts}
all_schemas = sorted(ms_schemas)

# All object types present
all_types = sorted({ot for _, ot in ms_counts} | {ot for _, ot in spg_counts})

# ── Global KPIs ───────────────────────────────────────────────────────────────
total_ms  = sum(ms_counts.values())
total_spg = sum(spg_counts.values())
total_pass = sum(v['pass'] for v in val_by_key.values())
total_fail = sum(v['fail'] for v in val_by_key.values())
total_skip = sum(v['skip'] for v in val_by_key.values())
total_extra= sum(v['extra'] for v in val_by_key.values())
pass_rate  = pct(total_pass, total_fail)

source_db  = MSSQL_CONF.get('database', 'source')
target_host= SPG_CONF.get('host', '')
client_name= args.client or source_db

# ── Failure category breakdown ────────────────────────────────────────────────
verdict_counts = defaultdict(int)
for r in all_results:
    v = r.get('test_verdict', '')
    if v not in PASS_V:
        verdict_counts[v] += 1

VERDICT_DESC = {
    'BOTH_FAILED':      'Both sides failed — environment/data prerequisite (parity confirmed)',
    'SPG_ERROR':        'SPG execution failed — migration defect requiring fix',
    'SPG_NO_RESULTSET': 'SPG proc cannot return result set — needs FUNCTION conversion',
    'FAIL':             'Execution succeeded but results differ (data or column mismatch)',
    'FAIL_DATA':        'Row count mismatch — object has rows in MSSQL but 0 in SPG (data load gap)',
    'FAIL_CONVERSION':  'Type conversion error during data copy',
    'MSSQL_ERROR':      'MSSQL execution failed',
    'MSSQL_ONLY':       'Object in MSSQL but not migrated to SPG',
    'SPG_ONLY':         'Object in SPG but not in MSSQL source',
    'WARN':             'Warning — minor structural difference',
    'ERROR':            'Execution error on one or both sides',
    'PASS_DML_PROC':    'Both returned 0 rows (correct for void procedures)',
}

# ── Remediation priorities — named ───────────────────────────────────────────
# Detect which issues are actually present
def has_verdict(v): return verdict_counts.get(v, 0) > 0

# Look for specific SPG error patterns
spg_err_rows = [r for r in all_results if r.get('test_verdict') == 'SPG_ERROR']
both_failed   = [r for r in all_results if r.get('test_verdict') == 'BOTH_FAILED']
no_rs_rows    = [r for r in all_results if r.get('test_verdict') == 'SPG_NO_RESULTSET']
fail_rows     = [r for r in all_results if r.get('test_verdict') in {'FAIL', 'FAIL_DATA', 'FAIL_CONVERSION'}]

# Detect specific error patterns
def has_pattern(rows, pattern):
    return any(pattern.lower() in str(r.get('error_message') or '').lower() for r in rows)

missing_col_errors = [r for r in spg_err_rows if 'does not exist' in str(r.get('error_message') or '').lower() and 'column' in str(r.get('error_message') or '').lower()]
bool_int_errors    = [r for r in fail_rows if 'bool' in str(r.get('error_message') or '').lower()]
list_index_errors  = [r for r in spg_err_rows if 'list index out of range' in str(r.get('error_message') or '').lower()]
job_prereq_rows    = [r for r in both_failed if 'job' in str(r.get('error_message') or '').lower() or 'microsloadstatus' in str(r.get('error_message') or '').lower()]
other_spg_errors   = [r for r in spg_err_rows if r not in missing_col_errors and r not in list_index_errors]

remediation = []
n = 1

if missing_col_errors:
    cols = set()
    for r in missing_col_errors:
        m = re.search(r'column ([\w.]+) does not exist', str(r.get('error_message') or ''), re.I)
        if m: cols.add(m.group(1))
    sample = ', '.join(sorted(cols)[:3])
    remediation.append((n, 'Missing columns in SPG staging tables', 'MISSING_COLUMN',
        f'Columns exist in MSSQL but were not migrated to SPG staging tables '
        f'(e.g. {sample}). Re-run migration for affected staging tables or add columns manually.',
        len(missing_col_errors)))
    n += 1

if list_index_errors:
    remediation.append((n, 'SPG executor parameter mapping error', 'PARAM_INDEX_ERROR',
        'SPG procedure executor hit "list index out of range" — likely a mismatch between '
        'expected parameter positions and the migrated signature. Review parameter order in affected procs.',
        len(list_index_errors)))
    n += 1

if no_rs_rows:
    remediation.append((n, 'PG PROCEDURE cannot return rows', 'PROC_NO_RESULTSET',
        'Convert `CREATE PROCEDURE` to `CREATE FUNCTION … RETURNS TABLE(…)` for procedures '
        'that return result sets. Postgres PROCEDUREs cannot return data via CALL.',
        len(no_rs_rows)))
    n += 1

if other_spg_errors:
    remediation.append((n, 'Other SPG execution errors', 'SPG_EXEC_ERROR',
        'Review individual errors in §Appendix. Fix migration defects in procedure body logic.',
        len(other_spg_errors)))
    n += 1

if bool_int_errors:
    remediation.append((n, 'BIT columns compared as boolean', 'BOOL_INT_MISMATCH',
        'Replace `WHERE col = true/false` with `WHERE col = 1/0` in migrated procs/views.',
        len(bool_int_errors)))
    n += 1

if len(job_prereq_rows) > 0:
    remediation.append((n, 'stg.* procs need active MicrosLoad job', 'STG_JOB_INFRASTRUCTURE',
        'Populate `stg.MicrosLoadStatus` with a test job record before validating stg export procs. '
        'These procs require an active job to exist — both MSSQL and SPG fail identically (parity confirmed).',
        len(job_prereq_rows)))
    n += 1

fail_data_rows = [r for r in all_results if r.get('test_verdict') == 'FAIL_DATA']
if fail_data_rows:
    remediation.append((n, 'Tables/views with 0 rows in SPG (data load gap)', 'FAIL_DATA',
        f'{len(fail_data_rows)} object(s) have rows in MSSQL but 0 rows in SPG. '
        'Root causes: unique constraint collision during load, missing cascade dependency, '
        'or FK-ordered insert failure. Retry load for affected tables after resolving constraints.',
        len(fail_data_rows)))
    n += 1

if fail_rows:
    hash_fails = [r for r in fail_rows if 'data_hash' in str(r.get('error_message') or '').lower()
                  or 'hash' in str('; '.join(r.get('issues') or [])).lower()]
    rowcount_fails = [r for r in fail_rows if 'row_count' in str(r.get('error_message') or '').lower()
                      or 'row_count' in str('; '.join(r.get('issues') or [])).lower()]
    if rowcount_fails or hash_fails:
        total_data = len(hash_fails) + len(rowcount_fails)
        remediation.append((n, 'Data differences between MSSQL and SPG', 'DATA_MISMATCH',
            'Row count or data hash mismatch. Verify that data was loaded consistently '
            'to both systems. Some differences may reflect different data loaded to each side.',
            total_data))
        n += 1

# ── Build Markdown ─────────────────────────────────────────────────────────────
L = []
A = L.append

A(f'# Migration Validation Report — {client_name}')
A('')
A(f'**Author:** {args.author}  ')
A(f'**Date:** {DATE_STR}  ')
A(f'**Source:** MSSQL {source_db}  ')
A(f'**Target:** Snowflake Postgres  ')
A(f'**Validation Runs:** {rn_trig}, {rn_view}, {rn_proc}' + (f', {rn_schema} (schema audit)' if rn_schema else ''))
A('')
A('---')
A('')

# ── Migration at a Glance ──────────────────────────────────────────────────────
A('## Migration at a Glance')
A('')
A('| KPI | Value |')
A('|-----|-------|')
A(f'| Total MSSQL Objects | {total_ms} |')
A(f'| Total SPG Objects | {total_spg} |')
A(f'| Pass Rate | **{pass_rate}** |')
A(f'| Schemas Tested | {len(all_schemas)} ({", ".join(all_schemas)}) |')
A(f'| Total PASS | {total_pass} |')
A(f'| Total FAIL | {total_fail} |')
if total_skip:
    A(f'| Skipped (write-modify) | {total_skip} |')
A('')

# ── Validation Runs ────────────────────────────────────────────────────────────
A('### Validation Runs')
A('')
A('| Run # | Started | Object Type | Objects | Pass | Fail | Skip | Status |')
A('|-------|---------|-------------|--------:|-----:|-----:|-----:|--------|')

def run_row(rn, label):
    r = all_runs.get(rn)
    if not r: return
    started = r['run_started_at'].strftime('%Y-%m-%d') if r.get('run_started_at') else '—'
    A(f"| {rn} | {started} | {label} | {r['total_objects']} | {r['pass_count']} | {r['fail_count']} | {r.get('skip_count',0)} | {r['run_status']} |")

run_row(rn_trig,   'TRIGGER')
run_row(rn_view,   'VIEW')
run_row(rn_proc,   'PROCEDURE/FUNCTION')
if rn_schema:
    run_row(rn_schema, 'SCHEMA AUDIT')
A('')
A('---')
A('')

# ── Object Count by Schema and Type ───────────────────────────────────────────
A('## Object Count by Schema and Type')
A('')
A('| Schema | Type | # MSSQL | # SPG | Passed | Failed | Skipped | Pass % |')
A('|--------|------|--------:|------:|-------:|-------:|--------:|-------:|')

for sc in all_schemas:
    for ot in all_types:
        ms_c  = ms_counts.get((sc, ot), 0)
        spg_c = spg_counts.get((sc, ot), 0)
        if ms_c == 0 and spg_c == 0:
            continue
        vk = val_by_key.get((sc, ot), {})
        p = vk.get('pass', 0)
        f = vk.get('fail', 0)
        s = vk.get('skip', 0)
        e = vk.get('extra', 0)
        if ms_c == 0 and spg_c > 0:
            A(f'| {sc} | {ot} | — | {spg_c} | — | — | — | SPG-only |')
        elif p == 0 and f == 0 and s == 0:
            # TABLE or not tested — show counts only
            A(f'| {sc} | {ot} | {ms_c} | {spg_c} | — | — | — | — |')
        else:
            A(f'| {sc} | {ot} | {ms_c} | {spg_c} | {p} | {f} | {s if s else "—"} | {pct(p, f, s)} |')

A('')
A('---')
A('')

# ── Failure Categories ─────────────────────────────────────────────────────────
A('## Failure Categories')
A('')
A('| Verdict | Count | Description |')
A('|---------|------:|-------------|')
for v, cnt in sorted(verdict_counts.items(), key=lambda x: -x[1]):
    if cnt == 0: continue
    desc = VERDICT_DESC.get(v, v)
    A(f'| {v} | {cnt} | {desc} |')
A('')
A('---')
A('')

# ── Remediation Priorities ────────────────────────────────────────────────────
A('## Remediation Priorities')
A('')
for num, title, code, fix, count in remediation:
    A(f'### {num}. {title} ({count} object{"s" if count != 1 else ""})')
    A('')
    A(f'**Code:** `{code}`  ')
    A(f'**Fix:** {fix}')
    A('')
A('---')
A('')

# ── Appendix — Failed Object Details ──────────────────────────────────────────
A('## Appendix — Failed Object Details')
A('')

fail_results = [r for r in all_results if r.get('test_verdict') not in PASS_V | {'PASS_DML_PROC', 'SKIPPED'}]

schemas_with_fails = sorted({(r.get('source_schema') or 'unknown').lower() for r in fail_results})

for sc in schemas_with_fails:
    sc_rows = [r for r in fail_results if (r.get('source_schema') or 'unknown').lower() == sc]
    if not sc_rows: continue

    A(f'### Schema: `{sc}`')
    A('')
    A('| Object | Type | Verdict | Issue |')
    A('|--------|------|---------|-------|')

    sc_rows_sorted = sorted(sc_rows, key=lambda r: (r.get('test_verdict',''), r.get('object_name','')))
    for r in sc_rows_sorted:
        obj   = r.get('object_name') or ''
        otype = r.get('object_type') or ''
        v     = r.get('test_verdict') or ''
        # Build issue string
        issues = r.get('issues') or []
        err    = r.get('error_message') or ''
        if issues:
            iss = clean('; '.join(str(i) for i in issues[:2]), 120)
        elif err:
            iss = clean(err, 120)
        elif v == 'FAIL_DATA':
            src = r.get('source_row_count', '?')
            tgt = r.get('target_row_count', '?')
            iss = f'ROW_COUNT: MSSQL={src} SPG={tgt}'
        else:
            iss = ''
        # Escape pipe chars in issue
        iss = iss.replace('|', '\\|')
        A(f'| `{obj}` | {otype} | {v} | {iss} |')

    A('')

A('---')
A('')
A(f'_Generated by Cortex Code Migration Validator  |  {args.author}  |  {DATE_STR}_')

# ── Write ─────────────────────────────────────────────────────────────────────
os.makedirs(args.out_dir, exist_ok=True)
if args.out_file:
    filename = args.out_file
else:
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', client_name)
    filename = f'Migration_Validation_{safe}_{DATE_STR}.md'

out_path = os.path.join(args.out_dir, filename)
with open(out_path, 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(L))

print(f'Written : {out_path}')
print(f'Lines   : {len(L)}')
print(f'Size    : {os.path.getsize(out_path):,} bytes')
