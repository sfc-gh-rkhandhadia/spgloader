#!/usr/bin/env python3
"""
run_validation_realization.py
Full validation runner for an MSSQL -> SPG migration.

All credentials MUST be supplied via environment variables or a .env file.
Never hardcode credentials in this file.

Usage:
    # Set env vars then run:
    export MSSQL_HOST=127.0.0.1
    export MSSQL_PORT=1433
    export MSSQL_USER=sa
    export MSSQL_PASSWORD=<your-password>
    export MSSQL_DATABASE=<your-db>
    export SPG_HOST=<your-spg-host>
    export SPG_USER=snowflake_admin
    export SPG_PASSWORD=<your-password>
    export SPG_DATABASE=<your-db>
    python3 run_validation_realization.py

    # Or copy scripts/.env.example to scripts/.env, fill in values, then run.
"""
import os, sys, time

# ── Load .env file if present (never commit .env) ────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; rely on environment already set

# ── Validate required credentials are present — no defaults for secrets ───────
_REQUIRED = [
    'MSSQL_HOST', 'MSSQL_USER', 'MSSQL_PASSWORD', 'MSSQL_DATABASE',
    'SPG_HOST',   'SPG_USER',   'SPG_PASSWORD',   'SPG_DATABASE',
]
_missing = [v for v in _REQUIRED if not os.environ.get(v)]
if _missing:
    print("ERROR: The following required environment variables are not set:\n")
    for v in _missing:
        print(f"  export {v}=\"...\"")
    print("\nCopy scripts/.env.example to scripts/.env and fill in values.")
    print("Never commit .env or hardcode credentials in source files.")
    sys.exit(1)

os.environ.setdefault('MSSQL_PORT', '1433')
os.environ.setdefault('VALIDATION_OUTPUT_DIR', os.path.join(os.getcwd(), 'validation_output'))
os.environ.setdefault('VALIDATION_SKIP_WRITES', 'false')

SCRIPTS_DIR = os.path.expanduser('~/.snowflake/cortex/skills/mssql_spg_migration_validation_testing/scripts')
sys.path.insert(0, SCRIPTS_DIR)

os.makedirs(os.environ['VALIDATION_OUTPUT_DIR'], exist_ok=True)

# ── Import config after env vars are set ────────────────────────────────────
from config import MSSQL_CONF, SPG_CONF, OUTPUT_DIR, check_required
check_required()

import validation_db as vdb

print("=" * 70)
print("MSSQL -> Snowflake Postgres — Migration Validation")
print(f"  Source : {MSSQL_CONF['database']} @ {MSSQL_CONF['server']}:{MSSQL_CONF['port']}")
print(f"  Target : {SPG_CONF['dbname']} @ {SPG_CONF['host'][:55]}")
print(f"  Output : {OUTPUT_DIR}")
print("=" * 70)

# ── Step 0: Ensure validation schema ─────────────────────────────────────────
print("\nEnsuring validation schema in SPG...")
vdb.ensure_schema_exists()
print("Validation schema OK")

start = time.time()

# ── Step 1: Triggers ──────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 1: TRIGGER VALIDATION")
print("=" * 70)
from validate_triggers import main as run_triggers
run_triggers()

# ── Step 2: Views ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 2: VIEW VALIDATION")
print("=" * 70)
from validate_batch import main as run_views
from run import _write_view_results
results, totals = run_views()
_write_view_results(results, totals)

# ── Step 3: Procedures/Functions ─────────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 3: PROCEDURE / FUNCTION VALIDATION — MSSQL side")
print("=" * 70)
from mssql_proc_executor import main as run_mssql_procs
run_mssql_procs()

print("\n" + "=" * 70)
print("STEP 3b: PROCEDURE / FUNCTION VALIDATION — SPG side")
print("=" * 70)
from spg_proc_executor import main as run_spg_procs
run_spg_procs()

print("\n" + "=" * 70)
print("STEP 3c: COMPARING OUTPUTS")
print("=" * 70)
from compare_proc_outputs import main as compare_outputs
compare_outputs()

elapsed = time.time() - start
print(f"\n{'=' * 70}")
print(f"VALIDATION COMPLETE in {elapsed:.0f}s")
print(f"Results at: {OUTPUT_DIR}")
print("Query audit tables:")
print("  SELECT * FROM validation.v_run_summary ORDER BY run_number DESC;")
print("=" * 70)
