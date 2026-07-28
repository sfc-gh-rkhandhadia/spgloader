"""
reporting.py — Shared validation summary table printer.

Prints a structured summary table (by schema + object type) showing
pass, fail, skip, missing counts and pass % BEFORE any detail output.
Imported by validate_batch.py, validate_triggers.py, compare_proc_outputs.py.
"""
from datetime import datetime
from collections import defaultdict

# Verdict classification — used consistently across all object types
PASS_VERDICTS    = {'PASS', 'PASS_DML_PROC'}
FAIL_VERDICTS    = {'FAIL', 'SPG_ERROR', 'SPG_NO_RESULTSET', 'MSSQL_ERROR',
                    'BOTH_FAILED', 'ERROR', 'WARN'}
SKIP_VERDICTS    = {'SKIPPED'}
MISSING_VERDICTS = {'MSSQL_ONLY', 'SPG_ONLY'}


def _pct(pass_n, fail_n):
    """Pass % = pass / (pass + fail), N/A when no testable objects."""
    denom = pass_n + fail_n
    return '%.1f%%' % (pass_n / denom * 100) if denom > 0 else 'N/A'


def print_summary_table(results, source_db, target_db, object_type_label=None, out=None):
    """
    Print the structured validation summary table.

    Parameters
    ----------
    results : list of dicts, each with keys:
        - 'schema'       : str
        - 'object_type'  : str  (e.g. 'VIEW', 'PROCEDURE', 'FUNCTION', 'TRIGGER')
        - 'verdict'      : str  (PASS, FAIL, SPG_ERROR, SKIPPED, MSSQL_ONLY, etc.)
    source_db : str   (e.g. 'YourDatabase')
    target_db : str   (e.g. 'postgres')
    object_type_label : str  optional override for the section title
    out : file-like   if None, prints to stdout
    """
    import sys
    _out = out or sys.stdout

    # ── Aggregate by (schema, object_type) ──────────────────────────────────
    groups = defaultdict(lambda: {'pass': 0, 'fail': 0, 'skip': 0, 'missing': 0})

    for r in results:
        schema  = (r.get('schema') or 'unknown').lower()
        otype   = (r.get('object_type') or 'OBJECT').upper()
        verdict = r.get('verdict', '')
        key     = (schema, otype)
        if verdict in PASS_VERDICTS:
            groups[key]['pass'] += 1
        elif verdict in FAIL_VERDICTS:
            groups[key]['fail'] += 1
        elif verdict in SKIP_VERDICTS:
            groups[key]['skip'] += 1
        elif verdict in MISSING_VERDICTS:
            groups[key]['missing'] += 1
        else:
            # Unknown verdict — count as fail to be safe
            groups[key]['fail'] += 1

    if not groups:
        return

    # ── Layout ──────────────────────────────────────────────────────────────
    W = 99
    SEP_OUTER = '=' * W
    SEP_INNER = '─' * W

    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    title_label = object_type_label or 'VALIDATION'
    header_line = f'  {title_label} SUMMARY  —  {source_db} → {target_db}  |  {now}'

    def _write(line=''):
        _out.write(line + '\n')

    _write()
    _write(SEP_OUTER)
    _write(header_line)
    _write(SEP_OUTER)

    col_w = {'schema': 12, 'type': 14, 'total': 7, 'pass': 7,
             'fail': 7, 'skip': 7, 'missing': 9, 'pct': 9}

    hdr = (f"  {'Schema':<{col_w['schema']}} {'Object Type':<{col_w['type']}}"
           f" {'Total':>{col_w['total']}} {'Pass':>{col_w['pass']}}"
           f" {'Fail':>{col_w['fail']}} {'Skip':>{col_w['skip']}}"
           f" {'Missing':>{col_w['missing']}} {'Pass %':>{col_w['pct']}}")
    _write(hdr)
    _write(SEP_INNER)

    total_pass = total_fail = total_skip = total_missing = 0

    for (schema, otype) in sorted(groups.keys()):
        g    = groups[(schema, otype)]
        p, f, s, m = g['pass'], g['fail'], g['skip'], g['missing']
        total = p + f + s + m
        total_pass    += p
        total_fail    += f
        total_skip    += s
        total_missing += m
        pct = _pct(p, f)
        _write(f"  {schema:<{col_w['schema']}} {otype:<{col_w['type']}}"
               f" {total:>{col_w['total']}} {p:>{col_w['pass']}}"
               f" {f:>{col_w['fail']}} {s:>{col_w['skip']}}"
               f" {m:>{col_w['missing']}} {pct:>{col_w['pct']}}")

    _write(SEP_INNER)
    grand_total = total_pass + total_fail + total_skip + total_missing
    grand_pct   = _pct(total_pass, total_fail)
    _write(f"  {'TOTAL':<{col_w['schema']}} {'':<{col_w['type']}}"
           f" {grand_total:>{col_w['total']}} {total_pass:>{col_w['pass']}}"
           f" {total_fail:>{col_w['fail']}} {total_skip:>{col_w['skip']}}"
           f" {total_missing:>{col_w['missing']}} {grand_pct:>{col_w['pct']}}")
    _write(SEP_OUTER)
    _write()

    # Legend
    _write('  Pass %  = Pass / (Pass + Fail)  [excludes Skipped and Missing objects]')
    _write('  Pass    = Exact match (PASS) or DML/ETL proc executed OK on both sides (PASS_DML_PROC)')
    _write('  Fail    = FAIL | SPG_ERROR | SPG_NO_RESULTSET | BOTH_FAILED | MSSQL_ERROR | ERROR | WARN')
    _write('  Skip    = Write/modify procedures intentionally excluded')
    _write('  Missing = Object not migrated (MSSQL_ONLY) or extra in SPG (SPG_ONLY)')
    _write(SEP_OUTER)
    _write()
