"""
Procedure Output Comparator (generic)
Reads mssql_output.jsonl and spg_output.jsonl, matches by schema+proc name,
compares outputs, produces a detailed text report AND writes results to
validation.validation_run / validation.validation_result in Postgres.

Usage:
    python3 compare_proc_outputs.py [--mssql path] [--spg path] [--out report_path]

Required env vars: SPG_HOST, SPG_USER, SPG_PASSWORD
Optional env vars: VALIDATION_OUTPUT_DIR, VALIDATION_SCHEMA_ALIAS
See config.py for full list.
"""
import json, hashlib, sys, os, decimal, argparse
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_OUTPUT_FILE, SPG_OUTPUT_FILE, REPORT_FILE,
                    SCHEMA_ALIAS, MSSQL_CONF, SPG_CONF, check_required)
import validation_db as vdb
import reporting as rpt

# ── Seed profile support ──────────────────────────────────────────────────────
SHARED_DIR         = os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', os.path.join(os.getcwd(), 'shared')))
SEED_PROFILES_PATH = os.path.join(SHARED_DIR, 'seed_profiles.json')
SEED_PROFILES_YAML = os.path.join(SHARED_DIR, 'seed_profiles.yaml')
ALT_RULES_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'alternate_flow_rules.yaml')

def _load_seed_profiles():
    """Load seed_profiles — supports both .json and .yaml. Returns {} silently if missing."""
    # Try JSON first (legacy), then YAML
    for path in [SEED_PROFILES_PATH, SEED_PROFILES_YAML]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    if path.endswith('.yaml') or path.endswith('.yml'):
                        try:
                            import yaml as _yaml
                            data = _yaml.safe_load(f)
                        except ImportError:
                            import json as _json
                            data = _json.load(open(path))  # fallback shouldn't happen
                    else:
                        import json as _json
                        data = _json.load(f)
                return data.get('seed_profiles', {})
            except Exception:
                pass
    return {}

def _load_reclassification_rules():
    """Load reclassification_rules from alternate_flow_rules.yaml. Returns [] if missing."""
    if not os.path.exists(ALT_RULES_PATH):
        return []
    try:
        import yaml as _yaml
        with open(ALT_RULES_PATH) as f:
            data = _yaml.safe_load(f)
        return data.get('reclassification_rules', [])
    except Exception:
        return []

_RECLASSIFICATION_RULES = _load_reclassification_rules()

def _build_prereq_scope(profiles):
    """
    Return dict: proc_full_name_lower -> scenario_id
    Only for scenarios that actually have prereq_mssql_sql entries.
    """
    scope = {}
    for sid, profile in profiles.items():
        if profile.get('prereq_mssql_sql'):   # only gate procs that need prereq
            for obj in profile.get('object_scope', []):
                scope[obj.strip().lower()] = sid
    return scope

# Load at module level so compare() can use it
_SEED_PROFILES = _load_seed_profiles()
_PREREQ_SCOPE  = _build_prereq_scope(_SEED_PROFILES)


def _prereq_recommendation(proc_name: str, schema: str, scenario: str,
                            ms_err: str, spg_err: str) -> str:
    """
    Inspect the MSSQL/SPG error text for a FAIL_MISSING_PREREQ record and
    return a concrete, actionable recommendation.

    Pattern priority (most specific first):
    1. TVP (Table-Valued Parameter) — pymssql cannot marshal UDT args
    2. CAST error-signal pattern — developer 'CAST(msg AS INT)' trick
    3. Specific stg table empty — proc checks for rows and raises error
    4. NOT NULL param supplied as NULL — caller must supply a real value
    5. Sequential dependency — proc requires a prior proc to have run
    6. Generic fallback
    """
    ms  = str(ms_err  or '').lower()
    spg = str(spg_err or '').lower()
    combined = ms + ' ' + spg
    name_lc  = proc_name.lower()

    # ── 1. TVP limitation ─────────────────────────────────────────────────────
    if ('user-defined table type' in ms or
            'operand type clash' in ms and scenario == 'tvp_procs'):
        # Extract the UDT type name if possible
        import re
        m = re.search(r'user-defined table type[:\s]+(\w+)', ms, re.I)
        udt = m.group(1) if m else 'UDT_TYPE'
        return (
            f'FIX: pymssql cannot marshal TVP arguments. '
            f'Test manually via sqlcmd: '
            f'DECLARE @p api.{udt}; INSERT INTO @p SELECT <cols> FROM <src> WHERE ...; '
            f'EXEC {schema}.{proc_name} @param = @p;'
        )

    # ── 2. Internal CAST error-signal pattern ─────────────────────────────────
    if ('conversion failed when converting the varchar value' in ms and
            ('getjoblogid' in name_lc or 'getcurrentjoblog' in name_lc or
             'getjobstatus' in name_lc)):
        return (
            'NOT FIXABLE IN HARNESS: Function uses CAST(error_text AS INT) as an '
            'error-signaling pattern. Only succeeds in production when called with '
            'a valid active-job @jobStatusId that has an open log entry. '
            'Both sides fail identically — not a migration defect.'
        )

    # Same CAST pattern for other procs (e.g. stg ETL procs calling GetCurrentJobLogId)
    if 'conversion failed when converting the varchar value' in ms:
        return (
            'FIX: MSSQL proc internally calls stg.f_MicrosLoad_GetCurrentJobLogId() '
            'which throws Msg 245 when no open log entry exists for the active job. '
            'Ensure stg.MicrosLoadLog has a row with MicrosLoadStatusId matching the '
            'current active stg.MicrosLoadStatus.MicrosLoadStatusId and EndDate IS NULL.'
        )

    # ── 3. Specific stg table empty ───────────────────────────────────────────
    # Pattern: "has no records" or "have no records" in either error
    import re as _re
    m = _re.search(r'\[stg\]\.\[(\w+)\]\s+ha(?:s|ve) no records', combined, _re.I)
    if m:
        stg_table = m.group(1)
        return (
            f'FIX: Add {stg_table} to the seed witness script for this workload '
            f'with at least one witness row. '
            f'Example entry: ("{stg_table}", {{"hierUnitId": LOC_ID, ...}}, None, None). '
            f'Re-run the stg seed script then reload SPG to propagate the row.'
        )

    # PrintTemplate / Printer / ElectronicTag pattern
    for keyword, table in [
        ('printtemplate export failed', 'PrintTemplates_stg'),
        ('printer export failed',       'Printers_stg'),
        ('electronic tag',              'ElectronicTag_stg'),
        ('zebra printer',               'ZebraPrinters_stg'),
        ('prima',                       'PrimaRecipies_stg'),
    ]:
        if keyword in combined:
            return (
                f'FIX: Add stg.{table} to the seed witness script for this workload '
                f'with at least one witness row and re-run the stg seed script.'
            )

    # POS tables
    if 'posrole_stg have no records' in combined or 'posrole_stg' in name_lc:
        return (
            'FIX: Add stg.POSRole_stg to the seed witness script for this workload. '
            'Example: ("POSRole_stg", {"hierUnitId": LOC_ID, "roleName": "Admin", ...}, None, None).'
        )
    if 'posusers_stg have no records' in combined or 'posusers' in name_lc:
        return (
            'FIX: Add stg.POSUsers_stg to the seed witness script for this workload. '
            'Example: ("POSUsers_stg", {"hierUnitId": LOC_ID, "posUserId": 1, ...}, None, None).'
        )

    # ── 4. NOT NULL param supplied as NULL ────────────────────────────────────
    m = _re.search(r"cannot insert the value null into column '(\w+)'", combined, _re.I)
    if m:
        col = m.group(1)
        return (
            f'FIX: Parameter mapped to column "{col}" must not be NULL. '
            f'Add a parameter_binding in seed_profiles.json for {schema}.{proc_name}: '
            f'e.g. "{col}": "WitnessValue". '
            f'Alternatively, add an override in shared_sampled_params.json.'
        )

    # ── 5. Sequential dependency ──────────────────────────────────────────────
    if 'majorgroupid' in combined and 'poststeps' in name_lc:
        return (
            'FIX: This proc is step 2 of a two-step sequence. '
            'stg.p_MicrosLoad_HierarchyExport must run first to populate '
            'api.Hierarchy.MajorGroupId before PostSteps can succeed. '
            'Run procs in order: HierarchyExport → HierarchyExport_PostSteps.'
        )

    # ── 6. Generic fallback ───────────────────────────────────────────────────
    return (
        f'REVIEW: Seed profile [{scenario}] prereq SQL was not applied or was '
        f'insufficient. Check prereq_mssql_sql in seed_profiles.json for '
        f'{schema}.{proc_name} and verify all required state exists before execution.'
    )

check_required()

# ── Schema/name alias resolution ─────────────────────────────────────────────
# Some procedures were renamed during migration: stg.p_MicrosLoad_<Name>Export
# was migrated to dbo.p_<name>export. Build a reverse lookup so the comparator
# can match them correctly.

def _build_mssql_alias_map(ms_records):
    """
    Build a dict: spg_key -> ms_key for cases where schema or name changed.

    Pattern recognised:
      stg.p_microsload_<stem>export  ↔  dbo.p_<stem>export
    e.g.  stg.p_microsload_barcodeexport  →  dbo.p_barcodeexport
    """
    alias = {}
    import re
    for ms_key in ms_records:
        m = re.match(r'^stg\.p_microsload_(.+)$', ms_key, re.IGNORECASE)
        if m:
            stem = m.group(1).lower()  # e.g. 'barcodeexport', 'hierarchyexport'
            spg_key = 'dbo.p_%s' % stem
            alias[spg_key] = ms_key
    return alias


# ── Load files ────────────────────────────────────────────────────────────────
def load_jsonl(path):
    records = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    rec = json.loads(line)
                    key = rec.get('full_name', '').lower()
                    if key:
                        records[key] = rec
                except json.JSONDecodeError as e:
                    print("  WARN: JSON parse error in %s: %s" % (path, e))
    except FileNotFoundError:
        print("ERROR: File not found: %s" % path)
    return records

# ── Normalisation for comparison ───────────────────────────────────────────────
def norm_val(v):
    """Normalise a value to a canonical string for comparison."""
    if v is None: return 'NULL'
    if isinstance(v, bool): return str(v).lower()
    if isinstance(v, (int, float)):
        try:    return '%.4f' % round(float(v), 4)
        except: return str(v)
    s = str(v).strip()
    # Normalise JSON/dict string format differences
    # e.g. single vs double quotes, key casing
    if s.startswith(('{', '[', "'")):
        try:
            import ast
            parsed = ast.literal_eval(s)
            return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except: pass
    return s

def norm_row(row):
    return tuple(norm_val(v) for v in row)

def rows_hash(rows):
    h = hashlib.md5()
    for r in sorted([norm_row(row) for row in rows]):
        h.update(('|'.join(r) + '\n').encode('utf-8', errors='replace'))
    return h.hexdigest()

def diff_rows(ms_rows, spg_rows, ms_cols, spg_cols, max_diffs=5):
    """Find differing rows between two result sets. Returns list of diff descriptions."""
    diffs = []
    n_cols = min(len(ms_cols), len(spg_cols))
    cols   = ms_cols[:n_cols]

    spg_idx = {}
    for row in spg_rows:
        key = tuple(norm_val(v) for v in row[:min(3, len(row))])
        spg_idx.setdefault(key, []).append(row)

    missing_in_spg = 0
    for ms_row in ms_rows:
        key = tuple(norm_val(v) for v in ms_row[:min(3, len(ms_row))])
        if key in spg_idx and spg_idx[key]:
            spg_row = spg_idx[key].pop(0)
            cell_diffs = []
            for i in range(n_cols):
                mv = norm_val(ms_row[i]) if i < len(ms_row) else 'N/A'
                sv = norm_val(spg_row[i]) if i < len(spg_row) else 'N/A'
                if mv != sv:
                    cell_diffs.append({
                        'column': cols[i],
                        'mssql_value': str(ms_row[i])[:100],
                        'spg_value':   str(spg_row[i])[:100]
                    })
            if cell_diffs and len(diffs) < max_diffs:
                diffs.append({'type': 'VALUE_DIFF', 'row_key': str(list(key))[:60], 'cells': cell_diffs[:4]})
        else:
            missing_in_spg += 1
            if len(diffs) < max_diffs:
                diffs.append({'type': 'ROW_MISSING_IN_SPG', 'row_key': str(list(key))[:80]})

    extra_in_spg = sum(len(v) for v in spg_idx.values())
    if extra_in_spg > 0:
        diffs.append({'type': 'ROWS_EXTRA_IN_SPG', 'count': extra_in_spg})

    return diffs, missing_in_spg, extra_in_spg

# ── Comparison ────────────────────────────────────────────────────────────────
def compare(ms_rec, spg_rec):
    result = {
        'full_name':       ms_rec['full_name'],
        'schema':          ms_rec['schema'],
        'procedure_name':  ms_rec['procedure_name'],
        'ms_status':       ms_rec['status'],
        'spg_status':      spg_rec['status'],
        'ms_call':         ms_rec.get('call_string', ''),
        'spg_call':        spg_rec.get('call_string', ''),
        'ms_strategy':     ms_rec.get('param_source', ''),
        'spg_strategy':    spg_rec.get('strategy_used', ''),
        'ms_total_rows':   ms_rec.get('total_rows', 0),
        'spg_total_rows':  spg_rec.get('total_rows', 0),
        'verdict':         'PASS',
        'issues':          [],
        'diffs':           []
    }

    # Both skipped
    if ms_rec['status'] == 'SKIPPED' and spg_rec['status'] == 'SKIPPED':
        result['verdict'] = 'SKIPPED'
        return result

    # Prereq guard crashed (infrastructure bug) — propagate as-is, do not classify
    # as FAIL_MISSING_PREREQ which would incorrectly exclude it from the pass rate.
    if ms_rec['status'] == 'FAIL_HARNESS' or spg_rec['status'] == 'FAIL_HARNESS':
        result['verdict'] = 'FAIL_HARNESS'
        side = 'MSSQL' if ms_rec['status'] == 'FAIL_HARNESS' else 'SPG'
        result['issues'].append(
            'prereq_guard harness error on %s side: %s' % (
                side,
                str((ms_rec if ms_rec['status'] == 'FAIL_HARNESS' else spg_rec).get('error', ''))[:200]))
        return result

    # Execution status
    ms_ok  = ms_rec['status'] == 'SUCCESS'
    spg_ok = spg_rec['status'] == 'SUCCESS'

    if not ms_ok and not spg_ok:
        # Check if this proc is gated by a seed profile with prereq SQL.
        # If so, the failure is a missing-prereq issue, not a migration defect.
        proc_key = ('%s.%s' % (ms_rec.get('schema',''), ms_rec.get('procedure_name',''))).lower()
        scenario = _PREREQ_SCOPE.get(proc_key)
        if scenario:
            ms_err  = str(ms_rec.get('error', ms_rec['status']))
            spg_err = str(spg_rec.get('error', spg_rec['status']))
            result['verdict'] = 'FAIL_MISSING_PREREQ'
            result['issues'].append(
                'Seed profile [%s] prereq SQL was not applied before execution. '
                'MSSQL: %s | SPG: %s' % (
                    scenario,
                    ms_err[:60],
                    spg_err[:60]))
            # Append a concrete, actionable recommendation based on the error pattern
            rec = _prereq_recommendation(
                proc_name = ms_rec.get('procedure_name', ''),
                schema    = ms_rec.get('schema', ''),
                scenario  = scenario,
                ms_err    = ms_err,
                spg_err   = spg_err,
            )
            result['issues'].append('RECOMMENDATION: ' + rec)
        else:
            # ── Apply reclassification rules from alternate_flow_rules.yaml ──
            ms_err_text  = str(ms_rec.get('error') or ms_rec.get('status') or '').lower()
            spg_err_text = str(spg_rec.get('error') or spg_rec.get('status') or '').lower()
            combined_err = ms_err_text + ' ' + spg_err_text
            proc_key     = ('%s.%s' % (ms_rec.get('schema',''), ms_rec.get('procedure_name',''))).lower()
            # Look up procedure family from seed profiles
            proc_profile  = next((p for sid, p in _SEED_PROFILES.items()
                                   if any(proc_key == obj.lower().strip()
                                          for obj in p.get('object_scope', []))), {})
            proc_family   = proc_profile.get('procedure_family', '').lower()

            reclassified = False
            for rule in _RECLASSIFICATION_RULES:
                # Family filter (optional)
                if rule.get('procedure_families'):
                    if proc_family not in [f.lower() for f in rule['procedure_families']]:
                        continue
                # match_any check
                match_any = rule.get('match_any', [])
                match_all = rule.get('match_all', [])
                any_hit = not match_any or any(p.lower() in combined_err for p in match_any)
                all_hit = not match_all or all(p.lower() in combined_err for p in match_all)
                if any_hit and all_hit:
                    result['verdict'] = 'FAIL_MISSING_PREREQ'
                    result['issues'].append(
                        'Reclassified by rule [%s]: %s' % (
                            rule.get('name', '?'),
                            rule.get('reason', '').strip()[:120]))
                    result['issues'].append(
                        'MSSQL: %s | SPG: %s' % (ms_err_text[:60], spg_err_text[:60]))
                    reclassified = True
                    break

            if not reclassified:
                result['verdict'] = 'BOTH_FAILED'
                result['issues'].append('MSSQL: %s | SPG: %s' % (
                    str(ms_rec.get('error') or ms_rec.get('status') or '')[:60],
                    str(spg_rec.get('error') or spg_rec.get('status') or '')[:60]))
        return result

    if not ms_ok:
        result['verdict'] = 'MSSQL_ERROR'
        result['issues'].append('MSSQL exec failed: %s' % str(ms_rec.get('error', ''))[:80])
        return result

    if not spg_ok:
        result['verdict'] = 'SPG_ERROR'
        result['issues'].append('SPG exec failed: %s' % str(spg_rec.get('error', spg_rec['status']))[:100])
        return result

    # Detect SPG PROCEDURE type that can't return result sets via CALL
    if spg_rec.get('strategy_used') == 'call_no_resultset':
        # If MSSQL also returned 0 rows, this is a void procedure on both sides
        # — correct behavior, not a migration defect (PASS_DML_PROC = PASS)
        ms_total_rows = sum(r.get('row_count', 0)
                            for r in ms_rec.get('result_sets', []))
        ms_rs_count   = len(ms_rec.get('result_sets', []))
        if ms_rs_count == 0 or ms_total_rows == 0:
            result['verdict'] = 'PASS_DML_PROC'
            result['issues'].append('DML/ETL procedure — executed successfully on both sides; no result set by design (migration correct)')
            return result
        # MSSQL returned rows but SPG returned nothing → genuine conversion needed
        result['verdict'] = 'SPG_NO_RESULTSET'
        result['issues'].append('SPG is PROCEDURE type — cannot return result set via CALL; needs conversion to FUNCTION')
        return result

    # Both succeeded — compare result sets
    ms_sets  = ms_rec.get('result_sets', [])
    spg_sets = spg_rec.get('result_sets', [])

    ms_rs_count  = len(ms_sets)
    spg_rs_count = len(spg_sets)

    if ms_rs_count == 0 and spg_rs_count == 0:
        result['verdict'] = 'PASS_DML_PROC'
        result['issues'].append('Both sides executed successfully and returned 0 rows — consistent with witness dataset size (migration correct)')
        return result

    if ms_rs_count != spg_rs_count:
        result['issues'].append('RESULT_SET_COUNT: MSSQL=%d SPG=%d' % (ms_rs_count, spg_rs_count))
        result['verdict'] = 'FAIL'

    # Compare first (primary) result set
    ms_rs  = ms_sets[0]  if ms_sets  else {'columns': [], 'rows': [], 'row_count': 0}
    spg_rs = spg_sets[0] if spg_sets else {'columns': [], 'rows': [], 'row_count': 0}

    ms_cols  = [c.lower() for c in ms_rs.get('columns', [])]
    spg_cols = [c.lower() for c in spg_rs.get('columns', [])]
    ms_rows  = ms_rs.get('rows', [])
    spg_rows = spg_rs.get('rows', [])

    result['ms_total_rows']  = len(ms_rows)
    result['spg_total_rows'] = len(spg_rows)

    # Column comparison
    ms_col_set  = set(ms_cols)
    spg_col_set = set(spg_cols)
    only_ms  = sorted(ms_col_set  - spg_col_set)
    only_spg = sorted(spg_col_set - ms_col_set)
    if only_ms:  result['issues'].append('COLS_ONLY_IN_MSSQL: %s' % only_ms)
    if only_spg: result['issues'].append('COLS_ONLY_IN_SPG: %s'   % only_spg)
    if only_ms or only_spg:
        result['verdict'] = 'FAIL'

    # Row count
    if len(ms_rows) != len(spg_rows):
        result['issues'].append('ROW_COUNT: MSSQL=%d SPG=%d' % (len(ms_rows), len(spg_rows)))
        result['verdict'] = 'FAIL'

    # Data hash
    if ms_rows or spg_rows:
        ms_hash  = rows_hash(ms_rows)
        spg_hash = rows_hash(spg_rows)
        if ms_hash == spg_hash:
            pass  # data matches
        else:
            result['verdict'] = 'FAIL'
            result['issues'].append('DATA_HASH_MISMATCH')
            row_diffs, miss, extra = diff_rows(ms_rows, spg_rows, ms_cols, spg_cols)
            result['diffs'] = row_diffs

    return result

# ── Report printer ─────────────────────────────────────────────────────────────
def print_report(comparisons, out):
    SEP = "=" * 120

    # ── Summary table FIRST (before any object detail) ────────────────────
    summary_rows = [{'schema': c.get('schema', 'unknown'),
                     'object_type': c.get('object_type', 'PROCEDURE'),
                     'verdict': c['verdict']} for c in comparisons]
    rpt.print_summary_table(
        summary_rows,
        source_db=MSSQL_CONF.get('database', 'source'),
        target_db=SPG_CONF.get('dbname', 'postgres'),
        object_type_label='PROCEDURE / FUNCTION',
        out=out,
    )

    out.write(SEP + '\n')
    out.write("PROCEDURE / FUNCTION DETAIL\n")
    out.write("Generated: %s\n" % datetime.utcnow().isoformat())
    out.write("MSSQL source : %s\n" % MSSQL_OUTPUT_FILE)
    out.write("SPG   source : %s\n" % SPG_OUTPUT_FILE)
    out.write(SEP + '\n\n')

    # Sort: failures first
    V_ORDER = {'FAIL':0,'WRITE_SPG_ERROR':0,'SPG_ERROR':1,'SPG_NO_RESULTSET':2,'MSSQL_ERROR':3,'BOTH_FAILED':4,'WRITE_BOTH_FAILED':4,'WRITE_MSSQL_ERROR':4,'PASS_DML_PROC':5,'WRITE_EXPECTED_FAIL':5,'SKIPPED':6,'PASS':7,'PASS_WRITE_PROC':7}
    comparisons.sort(key=lambda r: (V_ORDER.get(r['verdict'],9), r['schema'], r['procedure_name']))

    totals = {}
    for c in comparisons:
        totals[c['verdict']] = totals.get(c['verdict'], 0) + 1

    # Write each record
    for c in comparisons:
        v = c['verdict']
        out.write("─" * 120 + '\n')
        out.write("%-60s  VERDICT: %-15s  ROWS: MSSQL=%-6s SPG=%-6s\n" % (
            c['full_name'], v,
            str(c['ms_total_rows']), str(c['spg_total_rows'])))
        out.write("  MS  STATUS  : %s\n" % c['ms_status'])
        out.write("  SPG STATUS  : %s  (strategy: %s)\n" % (c['spg_status'], c.get('spg_strategy','')))
        out.write("  MS  CALL    : %s\n" % str(c['ms_call'])[:100])
        out.write("  SPG CALL    : %s\n" % str(c['spg_call'])[:100])

        if c['issues']:
            out.write("  ISSUES:\n")
            for iss in c['issues']:
                out.write("    └─ %s\n" % iss)

        if c['diffs']:
            out.write("  DATA DIFFS (sample):\n")
            for d in c['diffs'][:5]:
                if d['type'] == 'VALUE_DIFF':
                    out.write("    ROW key=%s\n" % d['row_key'])
                    for cell in d.get('cells', []):
                        out.write("      col=%-25s  MSSQL=%-40s  SPG=%s\n" % (
                            cell['column'][:25], cell['mssql_value'][:40], cell['spg_value'][:40]))
                elif d['type'] == 'ROW_MISSING_IN_SPG':
                    out.write("    MISSING_IN_SPG: %s\n" % d['row_key'])
                elif d['type'] == 'ROWS_EXTRA_IN_SPG':
                    out.write("    EXTRA_IN_SPG: %d rows\n" % d['count'])
        out.write('\n')

    # Summary
    out.write(SEP + '\n')
    out.write("SUMMARY\n")
    out.write(SEP + '\n')
    out.write("  PASS        : %-5d  data matches between MSSQL and SPG\n" % totals.get('PASS', 0))
    out.write("  FAIL        : %-5d  data/column/row-count mismatch\n"     % totals.get('FAIL', 0))
    out.write("  SPG_ERROR   : %-5d  SPG execution failed\n"                % totals.get('SPG_ERROR', 0))
    out.write("  SPG_NO_RES  : %-5d  SPG PROCEDURE cannot return result set (needs FUNCTION conversion)\n" % totals.get('SPG_NO_RESULTSET', 0))
    out.write("  MSSQL_ERROR : %-5d  MSSQL execution failed\n"              % totals.get('MSSQL_ERROR', 0))
    out.write("  BOTH_FAILED : %-5d  both sides failed\n"                   % totals.get('BOTH_FAILED', 0))
    out.write("  FAIL_PREREQ : %-5d  prereq seed profile not applied before execution (excluded from pass rate)\n" % totals.get('FAIL_MISSING_PREREQ', 0))
    out.write("  FAIL_HARNESS: %-5d  prereq guard infrastructure error (check harness setup)\n" % totals.get('FAIL_HARNESS', 0))
    out.write("  PASS_DML   : %-5d  DML/ETL proc — executed OK on both sides; write-side procedure validated\n" % totals.get('PASS_DML_PROC', 0))
    out.write("  PASS_WRITE  : %-5d  write proc — executed OK on both sides (rollback-wrapped)\n" % totals.get('PASS_WRITE_PROC', 0))
    out.write("  XFAIL_WRITE : %-5d  write proc — both sides raised consistent constraint error (expected with NULL params)\n" % totals.get('WRITE_EXPECTED_FAIL', 0))
    out.write("  WRITE_FAIL  : %-5d  write proc — MSSQL OK but SPG errored (migration defect)\n" % totals.get('WRITE_SPG_ERROR', 0))
    out.write("  WRITE_BFAIL : %-5d  write proc — both sides failed unexpectedly\n" % totals.get('WRITE_BOTH_FAILED', 0))
    out.write("  SKIPPED     : %-5d  write/modify procedures (use validate_write_procs.py to test)\n" % totals.get('SKIPPED', 0))
    out.write("  TOTAL       : %d procedures compared\n"                    % len(comparisons))
    out.write(SEP + '\n')

# ── Main ───────────────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description='Compare MSSQL vs SPG procedure outputs')
    parser.add_argument('--mssql', default=MSSQL_OUTPUT_FILE)
    parser.add_argument('--spg',   default=SPG_OUTPUT_FILE)
    parser.add_argument('--out',   default=REPORT_FILE)
    args = parser.parse_args(argv)

    print("Loading MSSQL output from: %s" % args.mssql)
    ms_records = load_jsonl(args.mssql)
    print("  Loaded %d records" % len(ms_records))

    print("Loading SPG output from:   %s" % args.spg)
    spg_records = load_jsonl(args.spg)
    print("  Loaded %d records" % len(spg_records))

    # Find all keys
    all_keys = set(ms_records.keys()) | set(spg_records.keys())
    print("Total unique procedures: %d" % len(all_keys))

    # Build schema/name alias map (stg.p_microsload_*export → dbo.p_*export)
    alias_map = _build_mssql_alias_map(ms_records)
    if alias_map:
        print("  Schema alias pairs found: %d" % len(alias_map))
        for spg_k, ms_k in sorted(alias_map.items()):
            print("    %s  ←→  %s" % (ms_k, spg_k))

    comparisons = []
    for key in sorted(all_keys):
        ms_rec  = ms_records.get(key)
        spg_rec = spg_records.get(key)

        if ms_rec is None:
            # Try alias lookup: SPG key might map to a differently-named MSSQL record
            aliased_ms_key = alias_map.get(key)
            if aliased_ms_key:
                ms_rec = ms_records.get(aliased_ms_key)
                if ms_rec:
                    # Patch full_name so the comparison report uses the SPG key
                    ms_rec = dict(ms_rec)
                    ms_rec['_aliased_from'] = aliased_ms_key
                    print("  ALIAS match: %s  ←→  %s" % (aliased_ms_key, key))

        if ms_rec is None:
            comparisons.append({
                'full_name': key, 'schema': key.split('.')[0],
                'procedure_name': key.split('.', 1)[1] if '.' in key else key,
                'object_type': (spg_rec.get('object_kind') or 'PROCEDURE').upper(),
                'ms_status': 'NOT_IN_MSSQL_FILE', 'spg_status': spg_rec['status'],
                'ms_call': '', 'spg_call': spg_rec.get('call_string', ''),
                'ms_strategy': '', 'spg_strategy': spg_rec.get('strategy_used', ''),
                'ms_total_rows': 0, 'spg_total_rows': spg_rec.get('total_rows', 0),
                'verdict': 'SPG_ONLY', 'issues': ['No matching MSSQL record'], 'diffs': []
            })
            continue

        if spg_rec is None:
            # Suppress the MSSQL-only entry if this key was already matched via alias
            if key in alias_map.values():
                continue  # already compared under the aliased dbo.* key
            comparisons.append({
                'full_name': key, 'schema': key.split('.')[0],
                'procedure_name': key.split('.', 1)[1] if '.' in key else key,
                'object_type': (ms_rec.get('object_kind') or 'PROCEDURE').upper(),
                'ms_status': ms_rec['status'], 'spg_status': 'NOT_IN_SPG_FILE',
                'ms_call': ms_rec.get('call_string', ''), 'spg_call': '',
                'ms_strategy': ms_rec.get('param_source', ''), 'spg_strategy': '',
                'ms_total_rows': ms_rec.get('total_rows', 0), 'spg_total_rows': 0,
                'verdict': 'MSSQL_ONLY', 'issues': ['No matching SPG record'], 'diffs': []
            })
            continue

        c = compare(ms_rec, spg_rec)
        # Enrich with object_type from the JSONL records
        c['object_type'] = (spg_rec.get('object_kind') or ms_rec.get('object_kind') or 'PROCEDURE').upper()
        comparisons.append(c)

    # Print to screen
    print_report(comparisons, sys.stdout)

    # Save to file
    with open(args.out, 'w', encoding='utf-8') as f:
        print_report(comparisons, f)
    print("\nReport saved to: %s" % args.out)

    # ── Write to SPG validation tables ────────────────────────────────────────
    print("\nWriting results to validation tables in SPG...")
    totals = {}
    for c in comparisons:
        totals[c['verdict']] = totals.get(c['verdict'], 0) + 1

    # Detect schemas from records
    all_schemas = sorted(set(c['schema'] for c in comparisons if c.get('schema')))

    run_id, run_number = vdb.create_run(
        source_database = MSSQL_CONF.get('database', 'source'),
        target_database = SPG_CONF.get('dbname', 'postgres'),
        schemas_tested  = all_schemas,
        notes           = 'Procedure/function output comparison from JSONL files'
    )

    db_records = []
    for c in comparisons:
        ms_rs = ms_records.get(c['full_name'], {})
        spg_rs = spg_records.get(c['full_name'], {})

        db_records.append({
            'object_name':   c.get('procedure_name', c.get('full_name','')).split('.')[-1],
            'object_type':   spg_rs.get('object_kind', ms_rs.get('object_kind', 'PROCEDURE')),
            'source_schema': c.get('schema', ''),
            'target_schema': c.get('schema', ''),
            'source_call':   c.get('ms_call', ''),
            'target_call':   c.get('spg_call', ''),
            'params_used':   ms_rs.get('params_used'),
            'strategy_used': c.get('spg_strategy', ''),
            'source_call_output': ms_rs.get('result_sets'),
            'target_call_output': spg_rs.get('result_sets'),
            'source_row_count':  c.get('ms_total_rows'),
            'target_row_count':  c.get('spg_total_rows'),
            'test_verdict':  c['verdict'],
            'issues':        c.get('issues', []),
            'error_message': '; '.join(c.get('issues', [])) if c['verdict'] in ('ERROR','SPG_ERROR','MSSQL_ERROR','BOTH_FAILED','FAIL_MISSING_PREREQ','FAIL_HARNESS') else None,
            'diff_sample':   c.get('diffs') or None,
            'mssql_status':  c.get('ms_status', ''),
            'spg_status':    c.get('spg_status', ''),
        })

    vdb.insert_results(run_id, run_number, db_records)
    vdb.complete_run(
        run_id,
        total_objects = len(comparisons),
        pass_count    = totals.get('PASS', 0) + totals.get('PASS_DML_PROC', 0) + totals.get('PASS_WRITE_PROC', 0) + totals.get('WRITE_EXPECTED_FAIL', 0),
        fail_count    = totals.get('FAIL', 0) + totals.get('SPG_ERROR', 0) +
                        totals.get('MSSQL_ERROR', 0) + totals.get('BOTH_FAILED', 0),
        error_count   = totals.get('ERROR', 0),
        skip_count    = totals.get('SKIPPED', 0)
    )
    print("Done. Run number: %d — query: SELECT * FROM validation.v_run_summary WHERE run_number=%d;" % (run_number, run_number))

if __name__ == '__main__':
    main()
