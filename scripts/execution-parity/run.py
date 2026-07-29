"""
run.py — Single entry point for MSSQL → Postgres Migration Validator.

Runs the full validation pipeline or individual components.
After validation completes, automatically generates the Markdown and
PowerPoint reports so every run produces complete, consistent output.

Usage:
    python3 run.py [--views] [--procs] [--triggers] [--all]
    python3 run.py --all                  # run everything (default)
    python3 run.py --views                # views only
    python3 run.py --procs                # procedures/functions only
    python3 run.py --triggers             # triggers only

Required environment variables (must be set before running):
    MSSQL_HOST        SQL Server host (e.g. localhost or 192.168.1.10)
    MSSQL_PORT        SQL Server port (default: 1433)
    MSSQL_USER        SQL Server login (e.g. sa)
    MSSQL_PASSWORD    SQL Server password
    MSSQL_DATABASE    Database name (e.g. MyDatabase)

    SPG_HOST          Postgres host (set via environment variable)
    SPG_USER          Postgres user
    SPG_PASSWORD      Postgres password

Optional environment variables:
    SPG_DATABASE              Target database name (default: postgres)
    SPG_PORT                  Target port (default: 5432)
    SPG_SSLMODE               SSL mode (default: require)
    VALIDATION_OUTPUT_DIR     Directory for output files (default: /tmp/validation_output)
    VALIDATION_BATCH_SIZE     Parallel batch size (default: 10)
    VALIDATION_SKIP_WRITES    Skip write procedures: true|false (default: true)
    VALIDATION_WRITE_KEYWORDS Comma-separated list of keywords that identify write procs
    VALIDATION_SCHEMA_ALIAS   Schema remapping: src=tgt,src2=tgt2 (e.g. dbo=public)
    SHARED_DIR                Shared handoff directory (default: ./shared, override with MSSQL_SPG_SHARED_DIR)
    REPORT_CLIENT             Client name for report title (default: MSSQL_DATABASE value)
    REPORT_AUTHOR             Author name for report cover (default: empty)

Example:
    export MSSQL_HOST=localhost
    export MSSQL_PORT=1433
    export MSSQL_USER=sa
    export MSSQL_PASSWORD=YourPassword
    export MSSQL_DATABASE=MyDatabase
    export SPG_HOST=<your-spg-host>
    export SPG_USER=<your-spg-user>
    export SPG_PASSWORD=YourPassword

    python3 run.py --all
"""
import argparse, os, sys, time, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (MSSQL_CONF, SPG_CONF, OUTPUT_DIR,
                    MSSQL_OUTPUT_FILE, SPG_OUTPUT_FILE, check_required)

check_required()
os.makedirs(OUTPUT_DIR, exist_ok=True)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


# ── Step runners ──────────────────────────────────────────────────────────────

def run_views():
    print("\n" + "=" * 70)
    print("STEP: VIEW VALIDATION")
    print("=" * 70)
    from validate_batch import main as view_main
    results, totals = view_main()
    _write_view_results(results, totals)
    return totals

def run_triggers():
    print("\n" + "=" * 70)
    print("STEP: TRIGGER VALIDATION")
    print("=" * 70)
    from validate_triggers import main as trig_main
    return trig_main()

def run_procs():
    print("\n" + "=" * 70)
    print("STEP: PROCEDURE / FUNCTION VALIDATION — MSSQL side")
    print("=" * 70)
    from mssql_proc_executor import main as mssql_main
    mssql_main()

    print("\n" + "=" * 70)
    print("STEP: PROCEDURE / FUNCTION VALIDATION — Postgres side")
    print("=" * 70)
    from spg_proc_executor import main as spg_main
    spg_main()

    print("\n" + "=" * 70)
    print("STEP: COMPARING OUTPUTS")
    print("=" * 70)
    from compare_proc_outputs import main as compare_main
    compare_main([])


def run_write_procs():
    """Rollback-wrapped validation for write/modify procedures (Cart, Update, Delete, Archive).
    Each procedure is executed inside a transaction that is ALWAYS rolled back —
    the witness dataset is never modified regardless of outcome.
    """
    print("\n" + "=" * 70)
    print("STEP: WRITE PROCEDURE VALIDATION (rollback-wrapped)")
    print("=" * 70)
    # Temporarily clear VALIDATION_SKIP_WRITES so the write executor can see all procs
    _orig = os.environ.get('VALIDATION_SKIP_WRITES', 'true')
    os.environ['VALIDATION_SKIP_WRITES'] = 'false'
    try:
        from validate_write_procs import main as write_main
        write_main([])
    finally:
        os.environ['VALIDATION_SKIP_WRITES'] = _orig


def run_reports(report_dir, client_name, author):
    """Generate Markdown and PowerPoint reports after all validation steps."""
    date_str = datetime.datetime.now().strftime('%Y%m%d')
    md_path  = os.path.join(report_dir, f'Migration_Validation_{date_str}.md')
    pptx_path = os.path.join(report_dir, f'Migration_Validation_{date_str}.pptx')

    print("\n" + "=" * 70)
    print("STEP: GENERATING REPORTS")
    print("=" * 70)

    # ── Markdown ────────────────────────────────────────────────────────────
    print(f"\nGenerating Markdown report → {md_path}")
    md_script = os.path.join(SCRIPTS_DIR, 'generate_validation_markdown.py')
    md_env = {**os.environ, 'SHARED_DIR': os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', os.path.join(os.getcwd(), 'shared')))}
    md_result = subprocess.run(
        [sys.executable, md_script, '--out-dir', report_dir, '--client', client_name],
        env=md_env, capture_output=True, text=True
    )
    if md_result.returncode == 0:
        for line in md_result.stdout.splitlines():
            print(f"  {line}")
    else:
        print(f"  WARN: Markdown generation failed — {md_result.stderr[:200]}")

    # ── PowerPoint ─────────────────────────────────────────────────────────
    print(f"\nGenerating PowerPoint report → {pptx_path}")
    pptx_script = os.path.join(SCRIPTS_DIR, 'generate_migration_report.py')
    pptx_result = subprocess.run(
        [sys.executable, pptx_script,
         '--client',        client_name,
         '--author',        author,
         '--spg-host',      SPG_CONF['host'],
         '--spg-password',  os.environ.get('SPG_PASSWORD', ''),
         '--mssql-host',    MSSQL_CONF['server'],
         '--mssql-port',    str(MSSQL_CONF.get('port', 1433)),
         '--mssql-user',    MSSQL_CONF['user'],
         '--mssql-password', os.environ.get('MSSQL_PASSWORD', ''),
         '--mssql-db',      MSSQL_CONF['database']],
        env={**os.environ,
             'REPORT_OUTPUT': pptx_path,
             'SHARED_DIR':    os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', os.path.join(os.getcwd(), 'shared')))},
        capture_output=True, text=True
    )
    if pptx_result.returncode == 0:
        for line in pptx_result.stdout.splitlines():
            print(f"  {line}")
    else:
        print(f"  WARN: PowerPoint generation failed — {pptx_result.stderr[:200]}")

    print(f"\nReports written to: {report_dir}")
    print(f"  {os.path.basename(md_path)}")
    print(f"  {os.path.basename(pptx_path)}")


def _write_view_results(results, totals):
    """Write view validation results to the audit tables."""
    import validation_db as vdb
    if not results:
        return

    source_db = MSSQL_CONF.get('database', 'source')
    target_db = SPG_CONF.get('dbname', 'postgres')

    # Determine schemas covered
    schemas = sorted({r['schema'] for r in results})

    pass_c  = totals.get('PASS', 0)
    fail_c  = totals.get('FAIL', 0) + totals.get('WARN', 0)
    err_c   = totals.get('ERROR', 0)
    skip_c  = totals.get('MSSQL_ONLY', 0) + totals.get('SPG_ONLY', 0)

    run_id, run_number = vdb.create_run(
        source_db, target_db, schemas,
        notes='View data validation (row count + column schema + data hash)'
    )

    records = []
    for r in results:
        records.append({
            'object_name':    r['object'],
            'object_type':    'VIEW',
            'source_schema':  r['schema'],
            'target_schema':  r['schema'],
            'source_row_count': r.get('mssql_rows'),
            'target_row_count': r.get('spg_rows'),
            'test_verdict':   r['verdict'],
            'issues':         r.get('issues', [])[:5],
            'error_message':  r['issues'][0] if r.get('issues') else None,
            'mssql_status':   None, 'spg_status': None,
            'source_call':    None, 'target_call': None,
            'params_used':    None, 'strategy_used': None,
            'source_call_output': None, 'target_call_output': None,
            'diff_sample':    None,
        })

    # Also add MSSQL_ONLY and SPG_ONLY as records
    # (these are not in results — they were printed but not returned)
    # They are already counted in totals

    vdb.insert_results(run_id, run_number, records)
    vdb.complete_run(run_id, len(records), pass_c, fail_c, err_c, skip_c)
    print(f"View results saved: run_number={run_number}  "
          f"PASS={pass_c}  FAIL={fail_c}  ERROR={err_c}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='MSSQL → Postgres Migration Validator',
        epilog='If no flags are given, --all is assumed.'
    )
    parser.add_argument('--views',      action='store_true', help='Run view validation')
    parser.add_argument('--procs',      action='store_true', help='Run procedure/function validation')
    parser.add_argument('--triggers',   action='store_true', help='Run trigger validation')
    parser.add_argument('--all',        action='store_true', help='Run all validations (default)')
    parser.add_argument('--report-dir', default=OUTPUT_DIR,
                        help='Directory to write Markdown and PPTX reports (default: VALIDATION_OUTPUT_DIR)')
    parser.add_argument('--client',     default=os.environ.get('REPORT_CLIENT', MSSQL_CONF.get('database', 'Client')),
                        help='Client name for report title (default: MSSQL_DATABASE)')
    parser.add_argument('--author',     default=os.environ.get('REPORT_AUTHOR', ''),
                        help='Author name for PPTX cover slide')
    parser.add_argument('--no-reports', action='store_true',
                        help='Skip report generation after validation')
    args = parser.parse_args()

    run_all = args.all or not any([args.views, args.procs, args.triggers])

    print("=" * 70)
    print("MSSQL → Postgres Migration Validator")
    print(f"  Source: {MSSQL_CONF['database']} @ {MSSQL_CONF['server']}")
    print(f"  Target: {SPG_CONF['dbname']} @ {SPG_CONF['host'][:50]}")
    print(f"  Output: {OUTPUT_DIR}")
    print(f"  Reports: {args.report_dir}")
    print("=" * 70)

    start = time.time()

    if run_all or args.triggers:
        run_triggers()

    if run_all or args.views:
        run_views()

    if run_all or args.procs:
        run_procs()
        run_write_procs()   # always follows procs — rollback-wrapped, witness-safe

    elapsed = time.time() - start
    print(f"\nValidation done in {elapsed:.0f}s. Results in: {OUTPUT_DIR}")
    print("Query audit tables:")
    print("  SELECT * FROM validation.v_run_summary ORDER BY run_number DESC;")

    # ── Always generate reports after a complete validation run ───────────
    if not args.no_reports:
        os.makedirs(args.report_dir, exist_ok=True)
        run_reports(args.report_dir, args.client, args.author)

if __name__ == '__main__':
    main()
