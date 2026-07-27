#!/usr/bin/env python3
"""
copy_source_data.py — Copy table data from an MSSQL, MySQL, or MariaDB source to Snowflake Postgres.

Replaces pgloader for data migration.  Uses:
  - pymssql (pure Python, no ODBC driver required) for MSSQL source
  - mysql-connector-python for MySQL/MariaDB source
  - psycopg2 with pg_service for the target (SPG)

Streams data in configurable batches — no heap limits.
Copies tables in parallel for throughput.

Usage:
  python copy_source_data.py \\
      --work-dir ~/.spgloader/20260101_120000 \\
      --spg-service pg_my_instance

  # Copy specific tables only
  python copy_source_data.py ... --tables dbo.orders dbo.customers

  # Truncate target tables before copying (idempotent re-runs)
  python copy_source_data.py ... --truncate-first

  # Tune batch size (rows per batch, default 5000)
  python copy_source_data.py ... --batch-size 2000

  # Parallel table workers (default 4)
  python copy_source_data.py ... --workers 8
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

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
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"')
    return env


def _connect_mssql(env: dict):
    """Open a pymssql connection from source_conn.env settings."""
    import pymssql
    host = env.get("SOURCE_HOST", "localhost")
    port = int(env.get("SOURCE_PORT", 1433))
    database = env.get("SOURCE_DATABASE", "master")
    user = env.get("SOURCE_USER", "sa")
    pass_env = env.get("SOURCE_PASSWORD_ENV", "MSSQL_SA_PASSWORD")
    password = os.environ.get(pass_env, "")
    if not password:
        raise RuntimeError(
            f"Password env var {pass_env!r} is not set. "
            f"Run: export {pass_env}='your_password'"
        )
    return pymssql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        as_dict=False,
    )


def _connect_mysql(env: dict):
    """Open a mysql-connector connection from source_conn.env settings."""
    import mysql.connector
    host = env.get("SOURCE_HOST", "localhost")
    port = int(env.get("SOURCE_PORT", 3306))
    database = env.get("SOURCE_DATABASE", "mysql")
    user = env.get("SOURCE_USER", "root")
    pass_env = env.get("SOURCE_PASSWORD_ENV", "MYSQL_ROOT_PASSWORD")
    password = os.environ.get(pass_env, "")
    if not password:
        raise RuntimeError(
            f"Password env var {pass_env!r} is not set. "
            f"Run: export {pass_env}='your_password'"
        )
    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        use_pure=True,
    )


def _connect_source(env: dict):
    """Open a source DB connection based on SOURCE_TYPE."""
    source_type = env.get("SOURCE_TYPE", "mssql").lower()
    if source_type == "mssql":
        return _connect_mssql(env), source_type
    elif source_type in ("mysql", "mariadb"):
        return _connect_mysql(env), source_type
    else:
        raise ValueError(
            f"copy_source_data.py supports mssql/mysql/mariadb sources, "
            f"got SOURCE_TYPE={source_type!r}"
        )


def _connect_spg(spg_service: str):
    """Open a psycopg2 connection via pg_service."""
    import psycopg2
    return psycopg2.connect(f"service={spg_service}")


# ---------------------------------------------------------------------------
# Type conversion helpers
# ---------------------------------------------------------------------------

def _pg_value(val):
    """Convert MSSQL/MySQL Python value to something psycopg2 can bind."""
    if val is None:
        return None
    import psycopg2.extras
    t = type(val)
    # bytes / bytearray (BINARY, IMAGE, VARBINARY) → psycopg2 Binary
    if isinstance(val, (bytes, bytearray)):
        import psycopg2
        return psycopg2.Binary(val)
    # datetime / date — pass through (psycopg2 handles these natively)
    if isinstance(val, (datetime, date)):
        return val
    # Decimal (MONEY, NUMERIC, DECIMAL) — pass through
    if isinstance(val, Decimal):
        return val
    # bool (BIT) — pass through
    if isinstance(val, bool):
        return val
    return val


def _convert_row(row: tuple) -> tuple:
    """Convert an entire source row to psycopg2-compatible values."""
    return tuple(_pg_value(v) for v in row)


# ---------------------------------------------------------------------------
# Per-table copy
# ---------------------------------------------------------------------------

def _copy_table(
    source_conn,
    spg_service: str,
    schema: str,
    table: str,
    batch_size: int,
    truncate_first: bool,
    source_type: str,
) -> dict:
    """Copy one table from source to SPG.

    Opens its own SPG connection (thread-safe: one connection per worker).
    Returns a result dict: {table, rows_copied, error, elapsed_s}
    """
    import psycopg2

    # Schema/table casing: MSSQL is case-insensitive, PG uses lowercase
    src_fqn = f"{schema}.{table}"
    pg_schema = schema.lower()
    pg_table = table.lower()
    pg_fqn = f'"{pg_schema}"."{pg_table}"'

    t0 = time.time()
    pg_conn = None

    try:
        pg_conn = psycopg2.connect(f"service={spg_service}")
        pg_conn.autocommit = False

        # Truncate target if requested
        if truncate_first:
            with pg_conn.cursor() as pg_cur:
                pg_cur.execute(f"TRUNCATE TABLE {pg_fqn}")
            pg_conn.commit()

        # Fetch column names from source
        src_cur = source_conn.cursor()
        if source_type == "mssql":
            src_cur.execute(f"SELECT TOP 0 * FROM {src_fqn}")
        else:
            src_cur.execute(f"SELECT * FROM {src_fqn} LIMIT 0")
        col_names = [d[0].lower() for d in src_cur.description]
        cols_sql = ", ".join(f'"{c}"' for c in col_names)
        placeholders = ", ".join(["%s"] * len(col_names))
        insert_sql = f"INSERT INTO {pg_fqn} ({cols_sql}) VALUES ({placeholders})"

        # Stream data in batches
        src_cur = source_conn.cursor()
        src_cur.execute(f"SELECT * FROM {src_fqn}")

        total_rows = 0
        while True:
            batch = src_cur.fetchmany(batch_size)
            if not batch:
                break
            converted = [_convert_row(row) for row in batch]
            with pg_conn.cursor() as pg_cur:
                pg_cur.executemany(insert_sql, converted)
            pg_conn.commit()
            total_rows += len(batch)

        elapsed = time.time() - t0
        print(f"  OK  {src_fqn:<45} {total_rows:>8,} rows  {elapsed:.1f}s")
        return {
            "table": src_fqn,
            "rows_copied": total_rows,
            "error": None,
            "elapsed_s": round(elapsed, 2),
        }

    except Exception as e:
        if pg_conn:
            pg_conn.rollback()
        elapsed = time.time() - t0
        err_msg = str(e).replace("\n", " ").strip()
        print(f"  ERR {src_fqn:<45} {err_msg[:80]}")
        return {
            "table": src_fqn,
            "rows_copied": 0,
            "error": err_msg,
            "elapsed_s": round(elapsed, 2),
        }
    finally:
        if pg_conn:
            pg_conn.close()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def copy_source_data(
    work_dir: Path,
    spg_service: str,
    tables: list[str] | None = None,
    batch_size: int = 5000,
    truncate_first: bool = False,
    workers: int = 4,
) -> dict:
    """Copy MSSQL/MySQL table data to SPG in parallel.

    Returns {results: [...], summary: {total, copied, failed, total_rows}}
    """
    env = _load_source_env(work_dir)
    source_conn, source_type = _connect_source(env)

    # Determine which tables to copy
    if tables:
        table_list = []
        for t in tables:
            if "." in t:
                sch, tbl = t.split(".", 1)
                table_list.append((sch, tbl))
            else:
                table_list.append(("dbo", t))
    else:
        ddl_path = work_dir / "ddl_objects.json"
        if not ddl_path.exists():
            raise FileNotFoundError(
                f"ddl_objects.json not found in {work_dir}. "
                "Run extract_ddl.py first."
            )
        objs = json.loads(ddl_path.read_text())
        table_list = [
            (o.get("schema", "dbo"), o["name"])
            for o in objs
            if o.get("type") == "table"
        ]

    if not table_list:
        print("No tables to copy.")
        return {"results": [], "summary": {"total": 0, "copied": 0, "failed": 0, "total_rows": 0}}

    host = env.get("SOURCE_HOST", "localhost")
    port = env.get("SOURCE_PORT", "1433")
    database = env.get("SOURCE_DATABASE", "")
    print(f"Source: {source_type} @ {host}:{port}/{database}")
    print(f"Target: SPG service={spg_service!r}")
    print(f"\nCopying {len(table_list)} table(s)  "
          f"[batch={batch_size}, workers={workers}"
          f"{', truncate_first' if truncate_first else ''}]\n")

    results = []

    def _worker(args):
        schema, table = args
        return _copy_table(
            source_conn, spg_service, schema, table,
            batch_size, truncate_first, source_type
        )

    if workers == 1:
        # Sequential — simpler for debugging
        for schema, table in table_list:
            results.append(_worker((schema, table)))
    else:
        # Parallel — one SPG connection per worker thread
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker, args): args for args in table_list}
            for future in as_completed(futures):
                results.append(future.result())

    source_conn.close()

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
    print(f"Tables copied : {copied}/{len(results)}")
    print(f"Total rows    : {total_rows:,}")
    if failed:
        print(f"Failed tables : {failed}")
        for r in results:
            if r["error"]:
                print(f"  - {r['table']}: {r['error'][:80]}")

    report_path = work_dir / "copy_data_report.json"
    report_path.write_text(
        json.dumps({"results": results, "summary": summary}, indent=2)
    )
    print(f"Report        : {report_path}")

    return {"results": results, "summary": summary}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy MSSQL/MySQL table data to Snowflake Postgres (no pgloader needed)"
    )
    parser.add_argument(
        "--work-dir", required=True,
        help="spgloader workspace directory (contains source_conn.env and ddl_objects.json)",
    )
    parser.add_argument(
        "--spg-service", required=True,
        help="pg_service name from ~/.pg_service.conf",
    )
    parser.add_argument(
        "--tables", nargs="+", default=None,
        help="Optional: copy only these tables (SCHEMA.TABLE format, e.g. dbo.orders)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=5000,
        help="Rows per INSERT batch (default: 5000)",
    )
    parser.add_argument(
        "--truncate-first", action="store_true",
        help="TRUNCATE each target table before copying (enables idempotent reruns)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Parallel table copy workers (default: 4)",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()
    copy_source_data(
        work_dir=work_dir,
        spg_service=args.spg_service,
        tables=args.tables,
        batch_size=args.batch_size,
        truncate_first=args.truncate_first,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
