"""
validate_triggers.py — Trigger validation: MSSQL vs Postgres (generic).

Dynamically discovers all triggers in both databases and compares
existence, event type, and table target. No hardcoded schema or trigger names.

Required env vars: MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE,
                   SPG_HOST, SPG_USER, SPG_PASSWORD
See config.py for full list.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, OUTPUT_DIR, check_required,
                    is_mssql_system_schema, is_spg_system_schema)
import pymssql, psycopg2
import validation_db as vdb
import reporting as rpt

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Discovery ────────────────────────────────────────────────────────────────

def get_mssql_triggers():
    """Return all DML triggers from MSSQL with schema, table, events, enabled state."""
    conn = pymssql.connect(**MSSQL_CONF)
    cur  = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT
            s.name                          AS schema_name,
            t.name                          AS table_name,
            tr.name                         AS trigger_name,
            tr.is_disabled                  AS is_disabled,
            tr.is_instead_of_trigger        AS is_instead_of,
            OBJECT_DEFINITION(tr.object_id) AS trigger_def,
            STUFF((
                SELECT ',' + te.type_desc
                FROM sys.trigger_events te
                WHERE te.object_id = tr.object_id
                FOR XML PATH('')
            ), 1, 1, '') AS events
        FROM sys.triggers tr
        JOIN sys.tables  t  ON tr.parent_id = t.object_id
        JOIN sys.schemas s  ON t.schema_id  = s.schema_id
        WHERE tr.parent_class = 1
          AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
        ORDER BY s.name, t.name, tr.name
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def get_spg_triggers():
    """Return all triggers from Postgres, grouped by (schema, table, trigger_name)."""
    conn = psycopg2.connect(**SPG_CONF)
    cur  = conn.cursor()
    cur.execute("""
        SELECT trigger_schema, event_object_table, trigger_name,
               event_manipulation, action_timing
        FROM information_schema.triggers
        ORDER BY trigger_schema, event_object_table, trigger_name
    """)
    rows = cur.fetchall()
    conn.close()

    # Group by (schema, table, trigger_name) consolidating events/timings
    grouped = {}
    for schema, table, trig_name, event, timing in rows:
        if is_spg_system_schema(schema):
            continue
        key = (schema.lower(), table.lower(), trig_name.lower())
        if key not in grouped:
            grouped[key] = {'schema': schema, 'table': table, 'name': trig_name,
                            'events': [], 'timings': []}
        grouped[key]['events'].append(event)
        grouped[key]['timings'].append(timing)
    return list(grouped.values())

# ── Normalisation ─────────────────────────────────────────────────────────────

def normalize_trigger_name(name):
    """Strip common schema-prefix and suffix conventions for matching.
    e.g. 'stg_microsloadlog_audit_update' → 'microsloadlog_audit_update'
    e.g. 'microsloadlog_audit_update_trigger' → 'microsloadlog_audit_update'
    """
    n = name.lower()
    # Strip leading schema_name_ prefix (e.g. stg_) if present
    for prefix in ['stg_', 'dbo_', 'api_', 'pub_']:
        if n.startswith(prefix):
            n = n[len(prefix):]
    # Strip trailing _trigger suffix if present (added by some PG migration conventions)
    if n.endswith('_trigger'):
        n = n[:-len('_trigger')]
    return n

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ms_triggers  = get_mssql_triggers()
    spg_triggers = get_spg_triggers()

    # Build SPG lookup by normalized name
    spg_by_norm = {normalize_trigger_name(t['name']): t for t in spg_triggers}

    records = []
    pass_count = fail_count = 0

    for ms in ms_triggers:
        tr_name   = ms['trigger_name']
        full_name = f"{ms['schema_name']}.{ms['table_name']}.{tr_name}"
        norm      = normalize_trigger_name(tr_name)
        spg       = spg_by_norm.get(norm)

        issues, verdict = [], 'PASS'

        if spg is None:
            verdict = 'FAIL'
            issues.append(f'MISSING_IN_SPG: trigger {tr_name} not found in SPG')
            fail_count += 1
        else:
            if ms['table_name'].lower() != spg['table'].lower():
                issues.append(f"TABLE_MISMATCH: MSSQL={ms['table_name']} SPG={spg['table']}")
                verdict = 'FAIL'

            ms_events  = set(e.strip().upper() for e in (ms['events'] or '').split(',') if e.strip())
            spg_events = set(e.strip().upper() for e in spg['events'])
            if ms_events and ms_events != spg_events:
                issues.append(f"EVENT_MISMATCH: MSSQL={sorted(ms_events)} SPG={sorted(spg_events)}")
                verdict = 'FAIL'

            if ms['is_disabled'] and spg.get('enabled', True):
                issues.append('STATE_MISMATCH: MSSQL disabled but SPG enabled')
                verdict = 'FAIL'

            if verdict == 'PASS':
                pass_count += 1
            else:
                fail_count += 1

        records.append({
            'object_name':   full_name,
            'object_type':   'TRIGGER',
            'source_schema': ms['schema_name'],
            'target_schema': spg['schema'] if spg else ms['schema_name'],
            'source_call':   f"ON {ms['table_name']} {ms['events']}",
            'target_call':   f"ON {spg['table']} {','.join(spg['events'])}" if spg else None,
            'test_verdict':  verdict,
            'issues':        issues,
            'error_message': '; '.join(issues) if issues else None,
            'mssql_status':  'FOUND',
            'spg_status':    'FOUND' if spg else 'MISSING',
            'params_used': None, 'strategy_used': None,
            'source_call_output': None, 'target_call_output': None, 'diff_sample': None,
        })

    # SPG-only triggers
    ms_norms = {normalize_trigger_name(t['trigger_name']) for t in ms_triggers}
    spg_only = [t for t in spg_triggers if normalize_trigger_name(t['name']) not in ms_norms]
    for t in spg_only:
        full = f"{t['schema']}.{t['table']}.{t['name']}"
        records.append({
            'object_name': full, 'object_type': 'TRIGGER',
            'source_schema': t['schema'], 'target_schema': t['schema'],
            'source_call': None,
            'target_call': f"ON {t['table']} {','.join(t['events'])}",
            'test_verdict': 'SPG_ONLY', 'issues': ['No matching MSSQL trigger'],
            'error_message': None, 'mssql_status': 'MISSING', 'spg_status': 'FOUND',
            'params_used': None, 'strategy_used': None,
            'source_call_output': None, 'target_call_output': None, 'diff_sample': None,
        })

    # ── Summary table FIRST ──────────────────────────────────────────────
    summary_rows = [{'schema': r['source_schema'], 'object_type': 'TRIGGER',
                     'verdict': r['test_verdict']} for r in records]
    rpt.print_summary_table(
        summary_rows,
        source_db=MSSQL_CONF.get('database', 'source'),
        target_db=SPG_CONF.get('dbname', 'postgres'),
        object_type_label='TRIGGER',
    )

    # ── Detail per trigger ───────────────────────────────────────────────────
    print(f"MSSQL: {len(ms_triggers)} triggers")
    for t in ms_triggers:
        print(f"  {t['schema_name']}.{t['table_name']} -> {t['trigger_name']}  "
              f"events={t['events']}  disabled={t['is_disabled']}")

    print(f"\nSPG:   {len(spg_triggers)} triggers")
    for t in spg_triggers:
        print(f"  {t['schema']}.{t['table']} -> {t['name']}  "
              f"events={t['events']}  timings={t['timings']}")

    for r in records:
        v = r['test_verdict']
        print(f"  {v:<10}  {r['object_name']}")
        for iss in r.get('issues', []):
            print(f"    -> {iss}")

    print(f"\nTRIGGER SUMMARY — PASS:{pass_count}  FAIL:{fail_count}  "
          f"SPG_ONLY:{len(spg_only)}")

    # Write to validation tables
    source_db = MSSQL_CONF.get('database', 'source')
    run_id, run_number = vdb.create_run(
        source_db, SPG_CONF.get('dbname', 'postgres'), ['triggers'],
        notes='Trigger validation — existence, event type, table target, enabled state'
    )
    vdb.insert_results(run_id, run_number, records)
    vdb.complete_run(run_id, len(records), pass_count, fail_count, 0, 0)
    print(f"Run number: {run_number}")
    return records

if __name__ == '__main__':
    main()
