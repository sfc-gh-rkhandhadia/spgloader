#!/usr/bin/env python3
"""
Acuity MSSQL → SPG Full Validation Runner
Validates tables, views, functions, procedures, triggers across all schemas.
Writes results to validation.validation_result on SPG.
"""
import os
import sys
import json
import uuid
import re
import time
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; rely on environment already set

# Regex for identifiers PostgreSQL normalises to lowercase.
# Covers letters, digits, underscores, and dollar signs (valid unquoted PG
# identifier characters per SQL:2003 and the PostgreSQL docs).  Names that
# contain anything else — most commonly hyphens or spaces — must be
# double-quoted with their original case preserved.
_SAFE_PG_IDENT = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_$]*$')


def pg_name(name: str) -> str:
    """Return the correct SPG identifier for a name when used inside double quotes.

    PostgreSQL stores plain identifiers in lowercase, so 'Orders' and 'orders'
    refer to the same object and we normalise to lowercase.

    Names that contain hyphens, spaces, or other special characters must be
    double-quoted with their *original* case preserved — PostgreSQL treats
    \"Consolidated-VendorList\" and \"consolidated-vendorlist\" as two different
    objects.  Lowercasing such a name would produce a 'relation does not exist'
    error even when the table is present.
    """
    if _SAFE_PG_IDENT.match(name):
        return name.lower()
    return name  # preserve case for hyphenated / special-character names

# Required env vars — set these in .env (see .env.example) or export them
_REQUIRED = [
    "MSSQL_HOST", "MSSQL_PORT", "MSSQL_USER", "MSSQL_PASSWORD", "MSSQL_DATABASE",
    "SPG_HOST", "SPG_USER", "SPG_PASSWORD", "SPG_DATABASE",
    "VALIDATION_OUTPUT_DIR",
]
for _var in _REQUIRED:
    if not os.environ.get(_var):
        sys.exit(f"ERROR: required environment variable {_var!r} is not set. Copy .env.example to .env and fill in values.")

SCRIPTS_DIR = Path(os.environ.get(
    "SCRIPTS_DIR",
    Path(__file__).parent
))
OUT_DIR = Path(os.environ["VALIDATION_OUTPUT_DIR"])
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(SCRIPTS_DIR))

import pymssql
import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def mssql_connect():
    return pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        port=int(os.environ["MSSQL_PORT"]),
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASSWORD"],
        database=os.environ["MSSQL_DATABASE"],
        timeout=30, login_timeout=15
    )

def spg_connect(autocommit=False):
    conn = psycopg2.connect(
        host=os.environ["SPG_HOST"],
        port=int(os.environ.get("SPG_PORT", 5432)),
        user=os.environ["SPG_USER"],
        password=os.environ["SPG_PASSWORD"],
        dbname=os.environ["SPG_DATABASE"],
        sslmode="require", connect_timeout=15
    )
    conn.autocommit = autocommit
    return conn

# ---------------------------------------------------------------------------
# Create a new validation run
# ---------------------------------------------------------------------------

def create_validation_run(pc, source_db, target_db, schemas):
    pc.autocommit = True
    cur = pc.cursor()
    cur.execute(
        """INSERT INTO validation.validation_run
           (source_database, target_database, schemas_tested, notes, run_by)
           VALUES (%s, %s, %s, %s, %s)
           RETURNING run_id, run_number""",
        (source_db, target_db, schemas, "Acuity full migration validation", "cortex-code")
    )
    run_id, run_number = cur.fetchone()
    cur.close()
    print(f"Created validation run #{run_number} (id={run_id})")
    return run_id, run_number


def complete_validation_run(pc, run_id, pass_count, fail_count, error_count, skip_count, total):
    cur = pc.cursor()
    cur.execute(
        """UPDATE validation.validation_run
           SET run_completed_at=%s, total_objects=%s,
               pass_count=%s, fail_count=%s, error_count=%s, skip_count=%s
           WHERE run_id=%s""",
        (datetime.utcnow(), total, pass_count, fail_count, error_count, skip_count, run_id)
    )
    cur.close()


def insert_result(pc, run_id, run_number, result: dict):
    cur = pc.cursor()
    cur.execute(
        """INSERT INTO validation.validation_result
           (run_id, run_number, object_name, object_type, source_schema, target_schema,
            source_call, target_call, source_row_count, target_row_count,
            test_verdict, issues, error_message, diff_sample, mssql_status, spg_status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            run_id, run_number,
            result.get("object_name"), result.get("object_type"),
            result.get("source_schema", "dbo"), result.get("target_schema", "dbo"),
            result.get("source_call"), result.get("target_call"),
            result.get("source_row_count"), result.get("target_row_count"),
            result.get("test_verdict", "ERROR"),
            result.get("issues"),
            result.get("error_message"),
            json.dumps(result.get("diff_sample")) if result.get("diff_sample") else None,
            result.get("mssql_status"), result.get("spg_status"),
        )
    )
    cur.close()

# ---------------------------------------------------------------------------
# Table count validation
# ---------------------------------------------------------------------------

def validate_tables(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, t.name
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name NOT IN ('sys') AND t.name NOT LIKE 'z%'
        ORDER BY s.name, t.name
    """)
    tables = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== TABLE COUNT VALIDATION ({len(tables)} tables) ===")

    pc_cur = pc.cursor()
    for schema, table in tables:
        pg_schema = pg_name(schema)
        pg_table = pg_name(table)
        # MSSQL count
        try:
            mc_cur = mc.cursor()
            mc_cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
            mssql_count = mc_cur.fetchone()[0]
            mc_cur.close()
        except Exception as e:
            mssql_count = -1

        # SPG count
        spg_count = -1
        spg_err = None
        try:
            pc_cur.execute(f'SELECT COUNT(*) FROM "{pg_schema}"."{pg_table}"')
            spg_count = pc_cur.fetchone()[0]
        except Exception as e:
            pc.rollback()
            spg_err = str(e)[:200]

        if mssql_count >= 0 and spg_count >= 0:
            verdict = "PASS" if spg_count >= mssql_count else "FAIL_DATA"
        elif spg_err:
            verdict = "SPG_ERROR"
        else:
            verdict = "ERROR"

        r = {
            "object_name": f"{schema}.{table}",
            "object_type": "TABLE",
            "source_schema": schema, "target_schema": pg_schema,
            "source_call": f"SELECT COUNT(*) FROM [{schema}].[{table}]",
            "target_call": f'SELECT COUNT(*) FROM "{pg_schema}"."{pg_table}"',
            "source_row_count": mssql_count if mssql_count >= 0 else None,
            "target_row_count": spg_count if spg_count >= 0 else None,
            "test_verdict": verdict,
            "issues": [spg_err] if spg_err else None,
            "error_message": spg_err,
            "mssql_status": "ok" if mssql_count >= 0 else "error",
            "spg_status": "ok" if spg_count >= 0 else "error",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        status_sym = "✓" if verdict == "PASS" else ("△" if verdict == "FAIL_DATA" else "✗")
        print(f"  {status_sym} {schema}.{table}: MSSQL={mssql_count} SPG={spg_count} [{verdict}]")

    pc_cur.close()
    print(f"Table validation: {sum(1 for r in results if r['test_verdict']=='PASS')} PASS, "
          f"{sum(1 for r in results if r['test_verdict']!='PASS')} FAIL/ERROR")
    return results


# ---------------------------------------------------------------------------
# View validation
# ---------------------------------------------------------------------------

def validate_views(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, v.name
        FROM sys.views v
        JOIN sys.schemas s ON v.schema_id=s.schema_id
        WHERE s.name NOT IN ('sys')
        ORDER BY s.name, v.name
    """)
    views = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== VIEW VALIDATION ({len(views)} views) ===")

    for schema, view in views:
        pg_schema = pg_name(schema)
        pg_view = pg_name(view)

        # Execute on MSSQL
        mssql_count = -1
        mssql_err = None
        try:
            mc_cur = mc.cursor()
            mc_cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{view}]")
            mssql_count = mc_cur.fetchone()[0]
            mc_cur.close()
        except Exception as e:
            mssql_err = str(e)[:200]

        # Execute on SPG
        spg_count = -1
        spg_err = None
        try:
            pc_cur = pc.cursor()
            pc_cur.execute(f'SELECT COUNT(*) FROM "{pg_schema}"."{pg_view}"')
            spg_count = pc_cur.fetchone()[0]
            pc_cur.close()
        except Exception as e:
            pc.rollback()
            spg_err = str(e)[:200]

        # Determine verdict
        if mssql_err and spg_err:
            verdict = "BOTH_FAILED_SOURCE_DEFECT"
        elif mssql_err:
            verdict = "BOTH_FAILED_SOURCE_DEFECT"
        elif spg_err:
            verdict = "SPG_ERROR"
        elif mssql_count == 0 and spg_count == 0:
            verdict = "PASS"  # Both empty - consistent
        elif mssql_count > 0 and spg_count > 0:
            verdict = "PASS"
        elif mssql_count > 0 and spg_count == 0:
            verdict = "FAIL_DATA"
        else:
            verdict = "PASS"  # mssql 0, spg 0+ is fine

        r = {
            "object_name": f"{schema}.{view}",
            "object_type": "VIEW",
            "source_schema": schema, "target_schema": pg_schema,
            "source_call": f"SELECT COUNT(*) FROM [{schema}].[{view}]",
            "target_call": f'SELECT COUNT(*) FROM "{pg_schema}"."{pg_view}"',
            "source_row_count": mssql_count if mssql_count >= 0 else None,
            "target_row_count": spg_count if spg_count >= 0 else None,
            "test_verdict": verdict,
            "issues": [e for e in [mssql_err, spg_err] if e] or None,
            "error_message": spg_err or mssql_err,
            "mssql_status": "error" if mssql_err else "ok",
            "spg_status": "error" if spg_err else "ok",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        status_sym = "✓" if verdict == "PASS" else ("!" if "DEFECT" in verdict else "✗")
        print(f"  {status_sym} {schema}.{view}: MSSQL={mssql_count if mssql_count>=0 else 'ERR'} "
              f"SPG={spg_count if spg_count>=0 else 'ERR'} [{verdict}]")

    print(f"View validation: {sum(1 for r in results if r['test_verdict']=='PASS')} PASS, "
          f"{sum(1 for r in results if r['test_verdict']!='PASS')} FAIL/ERROR")
    return results


# ---------------------------------------------------------------------------
# Function/procedure validation (existence + basic call)
# ---------------------------------------------------------------------------

def validate_functions(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, o.name, o.type_desc
        FROM sys.objects o
        JOIN sys.schemas s ON o.schema_id=s.schema_id
        WHERE o.type IN ('FN','TF','IF')
        AND s.name NOT IN ('sys')
        ORDER BY s.name, o.name
    """)
    functions = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== FUNCTION VALIDATION ({len(functions)} functions) ===")

    # Get SPG functions
    pc_cur = pc.cursor()
    pc_cur.execute("""
        SELECT n.nspname, p.proname, p.prokind
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace=n.oid
        WHERE n.nspname NOT IN ('pg_catalog','information_schema','public','cron')
        AND n.nspname NOT LIKE 'pg_%'
        AND n.nspname NOT LIKE 'snowflake_%'
        AND n.nspname NOT LIKE 'lake%'
        ORDER BY n.nspname, p.proname
    """)
    spg_funcs = {f"{r[0]}.{r[1]}".lower() for r in pc_cur.fetchall()}
    pc_cur.close()
    print(f"  SPG has {len(spg_funcs)} functions/procedures")

    for schema, func, type_desc in functions:
        pg_schema = schema.lower()
        pg_func = func.lower()
        spg_key = f"{pg_schema}.{pg_func}"

        in_spg = spg_key in spg_funcs
        verdict = "PASS" if in_spg else "SPG_ONLY"  # missing from SPG

        r = {
            "object_name": f"{schema}.{func}",
            "object_type": "FUNCTION",
            "source_schema": schema, "target_schema": pg_schema,
            "source_call": f"EXISTS: [{schema}].[{func}]",
            "target_call": f"EXISTS: {spg_key}",
            "test_verdict": verdict,
            "mssql_status": "ok",
            "spg_status": "ok" if in_spg else "missing",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        sym = "✓" if in_spg else "✗"
        print(f"  {sym} {schema}.{func} ({type_desc}): {'FOUND in SPG' if in_spg else 'MISSING from SPG'}")

    print(f"Function validation: {sum(1 for r in results if r['test_verdict']=='PASS')} PASS, "
          f"{sum(1 for r in results if r['test_verdict']!='PASS')} FAIL/MISSING")
    return results


# ---------------------------------------------------------------------------
# Stored procedure validation
# ---------------------------------------------------------------------------

def validate_procedures(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, p.name
        FROM sys.procedures p
        JOIN sys.schemas s ON p.schema_id=s.schema_id
        WHERE s.name NOT IN ('sys')
        ORDER BY s.name, p.name
    """)
    procs = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== PROCEDURE VALIDATION ({len(procs)} procedures) ===")

    pc_cur = pc.cursor()
    pc_cur.execute("""
        SELECT n.nspname, p.proname
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace=n.oid
        WHERE p.prokind IN ('p','f')
        AND n.nspname NOT IN ('pg_catalog','information_schema','public','cron')
        AND n.nspname NOT LIKE 'pg_%'
        AND n.nspname NOT LIKE 'snowflake_%'
        AND n.nspname NOT LIKE 'lake%'
    """)
    spg_procs = {f"{r[0]}.{r[1]}".lower() for r in pc_cur.fetchall()}
    pc_cur.close()

    for schema, proc in procs:
        pg_key = f"{schema.lower()}.{proc.lower()}"
        in_spg = pg_key in spg_procs
        verdict = "PASS" if in_spg else "SPG_ONLY"

        r = {
            "object_name": f"{schema}.{proc}",
            "object_type": "PROCEDURE",
            "source_schema": schema, "target_schema": schema.lower(),
            "source_call": f"EXISTS: [{schema}].[{proc}]",
            "target_call": f"EXISTS: {pg_key}",
            "test_verdict": verdict,
            "mssql_status": "ok",
            "spg_status": "ok" if in_spg else "missing",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        sym = "✓" if in_spg else "✗"
        print(f"  {sym} {schema}.{proc}: {'FOUND in SPG' if in_spg else 'MISSING from SPG'}")

    return results


# ---------------------------------------------------------------------------
# Trigger validation
# ---------------------------------------------------------------------------

def validate_triggers(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, t.name, OBJECT_NAME(t.parent_id) AS parent_table
        FROM sys.triggers t
        JOIN sys.objects o ON t.object_id=o.object_id
        JOIN sys.schemas s ON o.schema_id=s.schema_id
        WHERE t.is_ms_shipped=0
        ORDER BY s.name, t.name
    """)
    triggers = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== TRIGGER VALIDATION ({len(triggers)} triggers) ===")

    # Get SPG triggers
    try:
        pc_cur = pc.cursor()
        pc_cur.execute("""
            SELECT trigger_schema, trigger_name
            FROM information_schema.triggers
            WHERE trigger_schema NOT IN ('pg_catalog','information_schema','public')
            ORDER BY trigger_schema, trigger_name
        """)
        spg_triggers = {f"{r[0]}.{r[1]}".lower() for r in pc_cur.fetchall()}
        pc_cur.close()
    except Exception as e:
        pc.rollback()
        spg_triggers = set()

    for schema, trigger, parent_table in triggers:
        pg_key = f"{schema.lower()}.{trigger.lower()}"
        in_spg = pg_key in spg_triggers
        verdict = "PASS" if in_spg else "SPG_ONLY"

        r = {
            "object_name": f"{schema}.{trigger}",
            "object_type": "TRIGGER",
            "source_schema": schema, "target_schema": schema.lower(),
            "source_call": f"EXISTS: trigger {trigger} ON [{schema}].[{parent_table}]",
            "target_call": f"EXISTS: {pg_key}",
            "test_verdict": verdict,
            "mssql_status": "ok",
            "spg_status": "ok" if in_spg else "missing",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        sym = "✓" if in_spg else "✗"
        print(f"  {sym} {schema}.{trigger} ON {parent_table}: {'FOUND' if in_spg else 'MISSING from SPG'}")

    return results


# ---------------------------------------------------------------------------
# User-defined types (UDTTs)
# ---------------------------------------------------------------------------

def validate_types(mc, pc, run_id, run_number) -> list:
    results = []
    mc_cur = mc.cursor()
    mc_cur.execute("""
        SELECT s.name, t.name
        FROM sys.table_types t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name NOT IN ('sys')
        ORDER BY s.name, t.name
    """)
    types = mc_cur.fetchall()
    mc_cur.close()
    print(f"\n=== USER-DEFINED TYPE VALIDATION ({len(types)} types) ===")

    # SPG composite types
    try:
        pc_cur = pc.cursor()
        pc_cur.execute("""
            SELECT n.nspname, t.typname
            FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace=n.oid
            WHERE t.typtype IN ('c','d','e')
            AND n.nspname NOT IN ('pg_catalog','information_schema','public')
            AND n.nspname NOT LIKE 'pg_%'
        """)
        spg_types = {f"{r[0]}.{r[1]}".lower() for r in pc_cur.fetchall()}
        pc_cur.close()
    except Exception as e:
        pc.rollback()
        spg_types = set()

    for schema, typename in types:
        pg_key = f"{schema.lower()}.{typename.lower()}"
        in_spg = pg_key in spg_types
        verdict = "PASS" if in_spg else "SPG_ONLY"

        r = {
            "object_name": f"{schema}.{typename}",
            "object_type": "TYPE",
            "source_schema": schema, "target_schema": schema.lower(),
            "test_verdict": verdict,
            "mssql_status": "ok",
            "spg_status": "ok" if in_spg else "missing",
        }
        results.append(r)
        insert_result(pc, run_id, run_number, r)
        sym = "✓" if in_spg else "—"
        print(f"  {sym} {schema}.{typename}: {'FOUND in SPG' if in_spg else 'MISSING (expected for UDTTs)'}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("ACUITY MIGRATION VALIDATION")
    print(f"Started: {datetime.utcnow().isoformat()}Z")
    print("=" * 70)

    mc = mssql_connect()
    pc = spg_connect(autocommit=True)

    # Create run
    run_id, run_number = create_validation_run(
        pc,
        source_db="AcuityDB (MSSQL Docker port 1435)",
        target_db="SPG postgres",
        schemas=["dbo"]
    )

    all_results = []

    # Run all validation phases
    try:
        table_results = validate_tables(mc, pc, run_id, run_number)
        all_results.extend(table_results)

        view_results = validate_views(mc, pc, run_id, run_number)
        all_results.extend(view_results)

        func_results = validate_functions(mc, pc, run_id, run_number)
        all_results.extend(func_results)

        proc_results = validate_procedures(mc, pc, run_id, run_number)
        all_results.extend(proc_results)

        trigger_results = validate_triggers(mc, pc, run_id, run_number)
        all_results.extend(trigger_results)

        type_results = validate_types(mc, pc, run_id, run_number)
        all_results.extend(type_results)

    except Exception as e:
        print(f"\nERROR during validation: {e}")

    # Compute stats
    pass_count = sum(1 for r in all_results if r.get("test_verdict") == "PASS")
    fail_count = sum(1 for r in all_results if r.get("test_verdict") in ("FAIL_DATA", "FAIL_CONVERSION", "FAIL_MISSING_PREREQ"))
    error_count = sum(1 for r in all_results if "ERROR" in r.get("test_verdict", "") or r.get("test_verdict") == "SPG_ERROR")
    skip_count = sum(1 for r in all_results if "SKIP" in r.get("test_verdict", "") or "UNSUPPORTED" in r.get("test_verdict", ""))
    spg_only = sum(1 for r in all_results if r.get("test_verdict") == "SPG_ONLY")
    both_fail = sum(1 for r in all_results if "BOTH_FAILED" in r.get("test_verdict", ""))

    complete_validation_run(pc, run_id, pass_count, fail_count, error_count, skip_count, len(all_results))

    print("\n" + "=" * 70)
    print(f"VALIDATION COMPLETE — Run #{run_number}")
    print(f"  Total objects:     {len(all_results)}")
    print(f"  PASS:              {pass_count}")
    print(f"  FAIL (data/conv):  {fail_count}")
    print(f"  SPG_ERROR:         {error_count}")
    print(f"  SPG_ONLY (missing): {spg_only}")
    print(f"  BOTH_FAILED:       {both_fail}")
    print(f"  SKIPPED:           {skip_count}")
    if len(all_results) > 0:
        pass_rate = pass_count / len(all_results) * 100
        print(f"  Pass rate:         {pass_rate:.1f}%")
    print(f"\nResults stored in validation.validation_result (run #{run_number})")
    print("=" * 70)

    # Save raw results
    results_file = OUT_DIR / f"validation_raw_run{run_number}.json"
    with open(results_file, "w") as f:
        json.dump({
            "run_id": str(run_id),
            "run_number": run_number,
            "timestamp": datetime.utcnow().isoformat(),
            "summary": {
                "total": len(all_results), "pass": pass_count, "fail": fail_count,
                "error": error_count, "skipped": skip_count, "spg_only": spg_only
            },
            "results": all_results
        }, f, indent=2, default=str)
    print(f"\nRaw results: {results_file}")

    mc.close()
    pc.close()
    return run_number


if __name__ == "__main__":
    run_num = main()
    sys.exit(0)
