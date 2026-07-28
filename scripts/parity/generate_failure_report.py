"""
generate_failure_report.py — Generate a failure-only Markdown report
from the latest validation run results in SPG audit tables.
"""
import os, sys, datetime, psycopg2, psycopg2.extras
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SPG_CONF, OUTPUT_DIR, check_required

check_required()

OUT_DIR  = os.environ.get("VALIDATION_OUTPUT_DIR", os.path.expanduser("~/Downloads"))
CLIENT   = os.environ.get("CLIENT_NAME", os.environ.get("MSSQL_DATABASE", ""))
TODAY    = datetime.date.today().strftime("%Y%m%d")
OUT_FILE = os.path.join(OUT_DIR, f"{CLIENT}_Failure_Report_{TODAY}.md")

FAIL_VERDICTS = ('FAIL','SPG_ERROR','SPG_NO_RESULTSET','MSSQL_ERROR','BOTH_FAILED',
                 'BOTH_EMPTY_ERROR','MSSQL_ONLY','ERROR','WARN')

conn = psycopg2.connect(**SPG_CONF)
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# ── Find latest run per type ───────────────────────────────────────────────────
cur.execute("""
    SELECT notes, MAX(run_number) AS run_number
    FROM validation.validation_run
    GROUP BY notes
    ORDER BY MAX(run_number) DESC
""")
latest = {}
for row in cur.fetchall():
    notes = (row['notes'] or '').lower()
    n = row['run_number']
    if 'trigger' in notes and 'trigger' not in latest:
        latest['trigger'] = n
    elif 'view' in notes and 'view' not in latest:
        latest['view'] = n
    elif ('proc' in notes or 'function' in notes) and 'proc' not in latest:
        latest['proc'] = n

print(f"Using runs — triggers:{latest.get('trigger')}  views:{latest.get('view')}  procs:{latest.get('proc')}")

# ── Pull all failing rows across the three runs ────────────────────────────────
run_ids = [v for v in latest.values() if v is not None]
if not run_ids:
    print("No runs found — exiting"); sys.exit(1)

placeholders = ','.join(['%s'] * len(run_ids))
cur.execute(f"""
    SELECT
        r.run_number,
        vr.notes                            AS run_type,
        vr.source_database                  AS source_db,
        vr.target_database                  AS target_db,
        r.object_type,
        r.source_schema                     AS schema_name,
        r.object_name,
        r.test_verdict,
        r.source_row_count,
        r.target_row_count,
        r.error_message,
        r.issues
    FROM validation.validation_result r
    JOIN validation.validation_run vr ON r.run_id = vr.run_id
    WHERE r.run_number IN ({placeholders})
      AND r.test_verdict NOT IN ('PASS','SKIPPED','PASS_DML_PROC','SPG_ONLY')
    ORDER BY
        CASE r.object_type
            WHEN 'TRIGGER'   THEN 1
            WHEN 'VIEW'      THEN 2
            ELSE 3
        END,
        r.source_schema,
        r.object_name
""", run_ids)

rows = cur.fetchall()
conn.close()
print(f"Found {len(rows)} failing objects")

# ── Verdict → readable label ───────────────────────────────────────────────────
VERDICT_LABEL = {
    'FAIL':               'Data Mismatch',
    'SPG_ERROR':          'SPG Execution Error',
    'SPG_NO_RESULTSET':   'SPG PROCEDURE → needs FUNCTION conversion',
    'MSSQL_ERROR':        'MSSQL Execution Error',
    'BOTH_FAILED':        'Both Sides Failed',
    'MSSQL_ONLY':         'Not Migrated to SPG',
    'ERROR':              'Error',
    'WARN':               'Warning',
}

def fmt_verdict(v):
    return VERDICT_LABEL.get(v, v)

def fmt_reason(row):
    seen = set()
    parts = []

    def add(s):
        s = str(s).strip()[:200]
        key = s[:80]
        if key not in seen:
            seen.add(key)
            parts.append(s)

    # Row count delta first (clearest signal)
    if (row['source_row_count'] is not None and row['target_row_count'] is not None
            and row['source_row_count'] != row['target_row_count']):
        add(f"Row count: MSSQL={row['source_row_count']} SPG={row['target_row_count']}")

    # Issues list
    if row['issues']:
        iss = row['issues']
        for i in (iss if isinstance(iss, list) else [iss]):
            add(str(i)[:150])

    # Error message fallback
    if row['error_message'] and not parts:
        add(str(row['error_message']))

    return ' · '.join(parts) if parts else '—'

def clean_name(full_name, schema):
    """Strip schema prefix from object name if present."""
    prefix = (schema or '').lower() + '.'
    n = str(full_name or '')
    if n.lower().startswith(prefix):
        n = n[len(prefix):]
    return n

# ── Group by object type ───────────────────────────────────────────────────────
from collections import defaultdict
by_type = defaultdict(list)
for r in rows:
    by_type[r['object_type']].append(r)

# ── Build Markdown ─────────────────────────────────────────────────────────────
lines = []
lines.append(f"# {CLIENT} — Migration Failure Report")
lines.append(f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
lines.append(f"**Runs used:** triggers={latest.get('trigger')}  views={latest.get('view')}  procs={latest.get('proc')}  ")
lines.append(f"**Total failing objects:** {len(rows)}")
lines.append("")

# Summary table
lines.append("## Summary by Object Type")
lines.append("")
lines.append("| Object Type | Failing | Verdict Breakdown |")
lines.append("|---|---|---|")
for otype, obj_rows in sorted(by_type.items(), key=lambda x: {'TRIGGER':0,'VIEW':1}.get(x[0],2)):
    from collections import Counter
    vc = Counter(r['test_verdict'] for r in obj_rows)
    breakdown = ', '.join(f"{fmt_verdict(v)}={c}" for v,c in vc.most_common())
    lines.append(f"| {otype} | {len(obj_rows)} | {breakdown} |")
lines.append("")

# Detail sections per type
TYPE_ORDER = ['TRIGGER','VIEW','PROCEDURE','FUNCTION']
all_types = sorted(by_type.keys(), key=lambda x: TYPE_ORDER.index(x) if x in TYPE_ORDER else 99)

for otype in all_types:
    obj_rows = by_type[otype]
    lines.append(f"## {otype}S — {len(obj_rows)} failing")
    lines.append("")
    lines.append(f"| Schema | Object Name | Verdict | Failure Reason |")
    lines.append(f"|---|---|---|---|")
    for r in obj_rows:
        schema = r['schema_name'] or '—'
        name   = clean_name(r['object_name'], r['schema_name'])
        verdict = fmt_verdict(r['test_verdict'])
        reason  = fmt_reason(r).replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| `{schema}` | `{name}` | {verdict} | {reason} |")
    lines.append("")

# Verdict legend
lines.append("---")
lines.append("## Verdict Legend")
lines.append("")
lines.append("| Verdict | Meaning |")
lines.append("|---|---|")
for code, label in VERDICT_LABEL.items():
    lines.append(f"| `{code}` | {label} |")
lines.append("")
lines.append("---")
lines.append(f"*Report generated by MSSQL → Snowflake Postgres Migration Validator*")

# ── Write ──────────────────────────────────────────────────────────────────────
os.makedirs(OUT_DIR, exist_ok=True)
with open(OUT_FILE, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')

print(f"Written : {OUT_FILE}")
print(f"Lines   : {len(lines)}")
print(f"Size    : {os.path.getsize(OUT_FILE):,} bytes")
