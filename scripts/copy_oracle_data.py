#!/usr/bin/env python3
"""
copy_oracle_data.py — Copy table data from an Oracle source to Snowflake Postgres.

pgloader has no Oracle support, so this script uses:
  - oracledb (Python Oracle driver) for the source
  - psycopg2 with pg_service for the target (SPG)

Usage:
  python copy_oracle_data.py \\
      --work-dir ~/.spgloader/20260101_120000 \\
      --spg-service pg_my_instance

  # Copy specific tables only
  python copy_oracle_data.py ... --tables HR.EMPLOYEES HR.DEPARTMENTS

  # Truncate target tables before copying (idempotent re-runs)
  python copy_oracle_data.py ... --truncate-first

  # Tune batch size (rows per INSERT batch, default 5000)
  python copy_oracle_data.py ... --batch-size 2000
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _load_source_env(work_dir: Path) -> dict:
    """Read source_conn.env from workspace."""
    env_file = work_dir / "source_conn.env"
    if not env_file.exists():
        raise FileNotFoundError(f"source_conn.env not found in {work_dir}")
    env = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"')
    return env


def _connect_oracle(env: dict):
    """Open an oracledb connection from source_conn.env settings."""
    import oracledb
    host = env.get("SOURCE_HOST", "localhost")
    port = int(env.get("SOURCE_PORT", 1521))
    database = env.get("SOURCE_DATABASE", "FREEPDB1")
    user = env.get("SOURCE_USER", "system")
    pass_env = env.get("SOURCE_PASSWORD_ENV", "ORACLE_PWD")
    password = os.environ.get(pass_env, "")
    if not password:
        raise RuntimeError(
            f"Password env var {pass_env!r} is not set. "
            f"Run: export {pass_env}='your_password'"
        )
    conn = oracledb.connect(
        user=user,
        password=password,
        dsn=f"{host}:{port}/{database}",
    )
    return conn, user.upper()  # Oracle schema = user (uppercase)


def _connect_spg(spg_service: str):
    """Open a psycopg2 connection via pg_service."""
    import psycopg2
    return psycopg2.connect(f"service={spg_service}")


# ---------------------------------------------------------------------------
# Type conversion helpers
# ---------------------------------------------------------------------------

def _pg_value(val):
    """Convert Oracle Python value to something psycopg2 can bind."""
    if val is None:
        return None
    # oracledb LOB objects (CLOB / BLOB)
    t = type(val).__name__
    if hasattr(val, 'read'):
        # oracledb LOB — read full content
        try:
            content = val.read()
            return content if content is not None else None
        except Exception:
            return None
    # Oracle DATE / TIMESTAMP come as datetime objects — pass through
    if isinstance(val, (datetime, date)):
        return val
    # Decimal → float for simplicity (numeric columns)
    if isinstance(val, Decimal):
        return float(val)
    return val


def _convert_row(row: tuple) -> tuple:
    """Convert an entire Oracle row to psycopg2-compatible values."""
    return tuple(_pg_value(v) for v in row)


# ---------------------------------------------------------------------------
# Per-table copy
# ---------------------------------------------------------------------------

def _copy_table(
    ora_conn,
    pg_conn,
    schema: str,
    table: str,
    batch_size: int,
    truncate_first: bool,
) -> dict:
    """Copy one table from Oracle to Postgres.

    Returns a result dict: {table, rows_copied, error, elapsed_s}
    """
    fqn = f"{schema}.{table}"
    pg_fqn = f"{schema.lower()}.{table.lower()}"
    t0 = time.time()

    try:
        # Truncate target if requested
        if truncate_first:
            with pg_conn.cursor() as pg_cur:
                pg_cur.execute(f"TRUNCATE TABLE {pg_fqn}")
            pg_conn.commit()

        # Fetch column names from Oracle
        ora_cur = ora_conn.cursor()
        ora_cur.execute(f"SELECT * FROM {fqn} WHERE 1=0")
        col_names = [d[0].lower() for d in ora_cur.description]
        cols_sql = ", ".join(col_names)
        placeholders = ", ".join(["%s"] * len(col_names))
        insert_sql = f"INSERT INTO {pg_fqn} ({cols_sql}) VALUES ({placeholders})"

        # Stream data from Oracle in batches
        ora_cur = ora_conn.cursor()
        ora_cur.arraysize = batch_size
        ora_cur.execute(f"SELECT * FROM {fqn}")

        total_rows = 0
        while True:
            batch = ora_cur.fetchmany(batch_size)
            if not batch:
                break
            converted = [_convert_row(row) for row in batch]
            with pg_conn.cursor() as pg_cur:
                pg_cur.executemany(insert_sql, converted)
            pg_conn.commit()
            total_rows += len(batch)

        elapsed = time.time() - t0
        print(f"  OK  {fqn:<40} {total_rows:>8} rows  {elapsed:.1f}s")
        return {"table": fqn, "rows_copied": total_rows, "error": None, "elapsed_s": round(elapsed, 2)}

    except Exception as e:
        pg_conn.rollback()
        elapsed = time.time() - t0
        err_msg = str(e).replace("\n", " ").strip()
        print(f"  ERR {fqn:<40} {err_msg[:80]}")
        return {"table": fqn, "rows_copied": 0, "error": err_msg, "elapsed_s": round(elapsed, 2)}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def copy_oracle_data(
    work_dir: Path,
    spg_service: str,
    tables: list[str] | None = None,
    batch_size: int = 5000,
    truncate_first: bool = False,
) -> dict:
    """Copy Oracle table data to SPG.

    Returns {results: [...], summary: {total, copied, failed}}
    """
    # Load workspace config
    env = _load_source_env(work_dir)
    if env.get("SOURCE_TYPE", "").lower() != "oracle":
        raise ValueError(
            f"source_conn.env SOURCE_TYPE={env.get('SOURCE_TYPE')!r} — "
            "copy_oracle_data.py only supports Oracle sources"
        )

    # Determine which tables to copy
    if tables:
        # User-specified: parse schema.table format
        table_list = []
        for t in tables:
            if '.' in t:
                sch, tbl = t.split('.', 1)
                table_list.append((sch.upper(), tbl.upper()))
            else:
                schema = env.get("SOURCE_USER", "system").upper()
                table_list.append((schema, t.upper()))
    else:
        # Read from ddl_objects.json
        ddl_path = work_dir / "ddl_objects.json"
        if not ddl_path.exists():
            raise FileNotFoundError(
                f"ddl_objects.json not found in {work_dir}. "
                "Run extract_ddl.py first."
            )
        objs = json.loads(ddl_path.read_text())
        table_list = [
            (o.get("schema", env.get("SOURCE_USER", "system")).upper(),
             o["name"].upper())
            for o in objs if o.get("type") == "table"
        ]

    if not table_list:
        print("No tables to copy.")
        return {"results": [], "summary": {"total": 0, "copied": 0, "failed": 0}}

    print(f"Connecting to Oracle @ {env.get('SOURCE_HOST')}:{env.get('SOURCE_PORT')}"
          f"/{env.get('SOURCE_DATABASE')} ...")
    ora_conn, _ = _connect_oracle(env)

    print(f"Connecting to SPG via service={spg_service!r} ...")
    pg_conn = _connect_spg(spg_service)
    pg_conn.autocommit = False

    print(f"\nCopying {len(table_list)} table(s)  [batch_size={batch_size}"
          f"{', truncate_first' if truncate_first else ''}]\n")

    results = []
    for schema, table in table_list:
        result = _copy_table(ora_conn, pg_conn, schema, table, batch_size, truncate_first)
        results.append(result)

    ora_conn.close()
    pg_conn.close()

    copied = sum(1 for r in results if r["error"] is None)
    failed = len(results) - copied
    total_rows = sum(r["rows_copied"] for r in results)

    summary = {
        "total": len(results),
        "copied": copied,
        "failed": failed,
        "total_rows": total_rows,
    }

    print(f"\n{'='*60}")
    print(f"Tables copied   : {copied}/{len(results)}")
    print(f"Total rows      : {total_rows:,}")
    if failed:
        print(f"Failed tables   : {failed}")

    # Write report
    report_path = work_dir / "copy_data_report.json"
    report_path.write_text(json.dumps({"results": results, "summary": summary}, indent=2))
    print(f"Report          : {report_path}")

    return {"results": results, "summary": summary}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy Oracle table data to Snowflake Postgres (no pgloader needed)"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory (contains source_conn.env)")
    parser.add_argument("--spg-service", required=True,
                        help="pg_service name from ~/.pg_service.conf")
    parser.add_argument("--tables", nargs="+", default=None,
                        help="Optional: copy only these tables (SCHEMA.TABLE format)")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per INSERT batch (default: 5000)")
    parser.add_argument("--truncate-first", action="store_true",
                        help="Truncate target table before copying (idempotent re-run)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    result = copy_oracle_data(
        work_dir=work_dir,
        spg_service=args.spg_service,
        tables=args.tables,
        batch_size=args.batch_size,
        truncate_first=args.truncate_first,
    )

    if result["summary"].get("failed", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
