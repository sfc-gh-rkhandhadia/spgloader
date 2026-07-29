#!/usr/bin/env python3
"""
mysql_structural_parity.py — MySQL / MariaDB ↔ SPG structural equivalence check.

Compares table existence, column counts, routine (function/procedure) existence,
and view existence between the MySQL source and the migrated Snowflake Postgres
instance for one or more schemas.

Writes `parity_results.json` in the schema that html_report._build_equivalence_tab
expects so the Equivalence Test tab is fully populated.

Usage:
    uv run --project <SKILL_DIR> python scripts/parity/mysql_structural_parity.py \\
      --source-type  mysql \\
      --source-host  localhost \\
      --source-port  3306 \\
      --source-user  root \\
      --password-env MYSQL_ROOT_PASSWORD \\
      --databases    evdas,ms,ms_literature,sapphire,spotfire_reporting,udr \\
      --spg-service  pg_arisglobal_v2 \\
      --output       /path/to/workspace/parity/parity_results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from repo root
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from source_adapter import SourceAdapter, open_source_conn


# ---------------------------------------------------------------------------
# SPG connection
# ---------------------------------------------------------------------------

def _read_pg_service(svc: str) -> dict:
    """Parse ~/.pg_service.conf for connection params of a named service."""
    conf_path = Path.home() / ".pg_service.conf"
    if not conf_path.exists():
        return {}
    cfg: dict = {}
    in_block = False
    for line in conf_path.read_text().splitlines():
        line = line.strip()
        if line == f"[{svc}]":
            in_block = True
            continue
        if in_block:
            if line.startswith("["):
                break
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def _read_pgpass(host: str, db: str, user: str) -> str:
    pgpass = Path.home() / ".pgpass"
    if not pgpass.exists():
        return ""
    for row in pgpass.read_text().splitlines():
        parts = row.split(":")
        if len(parts) == 5 and (parts[0] in (host, "*")) and (parts[3] in (user, "*")):
            return parts[4]
    return ""


def _spg_cursor(spg_service: str):
    """Return (psycopg2 connection, cursor) for the named SPG service."""
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
    except ImportError:
        sys.exit("psycopg2 not installed. Run: uv add psycopg2-binary")
    cfg = _read_pg_service(spg_service)
    if not cfg:
        sys.exit(f"SPG service '{spg_service}' not found in ~/.pg_service.conf")
    host = cfg.get("host", "localhost")
    user = cfg.get("user", "postgres")
    db   = cfg.get("dbname", "postgres")
    port = int(cfg.get("port", 5432))
    pw   = _read_pgpass(host, db, user)
    conn = psycopg2.connect(host=host, port=port, user=user, password=pw, dbname=db)
    conn.autocommit = True
    return conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor)


# ---------------------------------------------------------------------------
# Per-schema comparison helpers
# ---------------------------------------------------------------------------

def _spg_tables(scur, schema: str) -> set[str]:
    scur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
    """, (schema,))
    return {r[0].lower() for r in scur.fetchall()}


def _spg_routines(scur, schema: str) -> dict[str, str]:
    scur.execute("""
        SELECT routine_name, routine_type FROM information_schema.routines
        WHERE routine_schema = %s ORDER BY routine_name
    """, (schema,))
    return {r[0].lower(): r[1] for r in scur.fetchall()}


def _spg_views(scur, schema: str) -> set[str]:
    scur.execute("""
        SELECT table_name FROM information_schema.views WHERE table_schema = %s
    """, (schema,))
    return {r[0].lower() for r in scur.fetchall()}


def _spg_col_count(scur, schema: str, table: str) -> int:
    scur.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
    """, (schema, table))
    return int(scur.fetchone()[0] or 0)


# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------

def compare_schema(adapter: SourceAdapter, scur, schema: str,
                   sample_col_tables: int = 30) -> dict:
    """Compare one schema and return a dict in the parity_results schemas format."""
    # ── Tables ──────────────────────────────────────────────────────────
    src_tables = {t.lower() for t in adapter.list_tables(schema)}
    spg_tables = _spg_tables(scur, schema)
    only_src   = sorted(src_tables - spg_tables)
    only_spg   = sorted(spg_tables - src_tables)
    matched    = src_tables & spg_tables

    # Column count spot-check (skip CREATE-TABLE-AS-SELECT tables where src=0)
    col_mismatches = []
    for tbl in sorted(matched)[:sample_col_tables]:
        src_cc = adapter.column_count(schema, tbl)
        spg_cc = _spg_col_count(scur, schema, tbl)
        if src_cc != spg_cc and src_cc != 0:
            col_mismatches.append({"table": tbl, "src": src_cc, "spg": spg_cc})

    # ── Routines ────────────────────────────────────────────────────────
    src_rout = {r["name"].lower(): r for r in adapter.list_routines(schema)}
    spg_rout = _spg_routines(scur, schema)
    rout_only_src = sorted(k for k in src_rout if k not in spg_rout)
    rout_matched  = len(src_rout) - len(rout_only_src)

    # ── Views ───────────────────────────────────────────────────────────
    src_views = {v.lower() for v in adapter.list_views(schema)}
    spg_views = _spg_views(scur, schema)
    view_only_src = sorted(src_views - spg_views)
    view_only_spg = sorted(spg_views - src_views)

    # ── Counts for grand totals ──────────────────────────────────────────
    pass_count = len(matched) + rout_matched + len(src_views & spg_views)
    fail_count = (len(only_src)         # tables missing in SPG
                  + len(rout_only_src)  # routines missing in SPG
                  + len(view_only_src)  # views missing in SPG
                  + len(col_mismatches))  # real column count differences

    # Build parity "objects" list for the MSSQL-compat renderer (empty for structural)
    missing_objects = (
        [{"fqn": f"{schema}.{t}", "type": "TABLE"}    for t in only_src]
        + [{"fqn": f"{schema}.{r}", "type": "ROUTINE"} for r in rout_only_src]
        + [{"fqn": f"{schema}.{v}", "type": "VIEW"}    for v in view_only_src]
    )

    return {
        "pass":    pass_count,
        "fail":    fail_count,
        "missing": len(only_src) + len(rout_only_src) + len(view_only_src),
        "spg_only": len(only_spg) + len(view_only_spg),
        # Structural detail (used by the MySQL renderer branch)
        "tables_src":       len(src_tables),
        "tables_spg":       len(spg_tables),
        "tables_match":     len(matched),
        "routines_src":     len(src_rout),
        "routines_spg":     len(spg_rout),
        "routines_match":   rout_matched,
        "routines_missing": rout_only_src,
        "col_mismatches":   col_mismatches,
        "views_src":        len(src_views),
        "views_spg":        len(spg_views),
        # MSSQL-compat fields (empty for structural path)
        "missing_objects":  missing_objects,
        "results":          [],
        "excluded_objects": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MySQL / MariaDB ↔ SPG structural equivalence parity check"
    )
    parser.add_argument("--source-type", default="mysql",
                        choices=["mysql", "mariadb"],
                        help="Source DB dialect (default: mysql)")
    parser.add_argument("--source-host", default="localhost")
    parser.add_argument("--source-port", type=int, default=3306)
    parser.add_argument("--source-user", default="root")
    parser.add_argument("--password-env", required=True,
                        help="Name of the env var that holds the source DB password")
    parser.add_argument("--databases", required=True,
                        help="Comma-separated list of source database/schema names")
    parser.add_argument("--spg-service", required=True,
                        help="Name of the SPG service in ~/.pg_service.conf")
    parser.add_argument("--output", required=True,
                        help="Path to write parity_results.json")
    parser.add_argument("--sample-col-tables", type=int, default=30,
                        help="Number of tables to spot-check column counts (default: 30)")
    args = parser.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        sys.exit(f"Environment variable {args.password_env!r} is not set or empty")

    databases = [d.strip() for d in args.databases.split(",") if d.strip()]
    if not databases:
        sys.exit("--databases must contain at least one database name")

    print(f"Connecting to {args.source_type} on {args.source_host}:{args.source_port} …")
    src_conn = open_source_conn(
        args.source_type, args.source_host, args.source_port,
        args.source_user, password, databases[0],
    )

    print(f"Connecting to SPG service {args.spg_service!r} …")
    spg_conn, scur = _spg_cursor(args.spg_service)

    adapter = SourceAdapter(args.source_type, src_conn)

    grand_pass = grand_fail = grand_missing = grand_spg_only = 0
    schema_results: dict = {}

    for db in databases:
        print(f"\n── {db} ──")
        result = compare_schema(adapter, scur, db, args.sample_col_tables)
        schema_results[db] = result

        grand_pass    += result["pass"]
        grand_fail    += result["fail"]
        grand_missing += result["missing"]
        grand_spg_only += result["spg_only"]

        print(f"  Tables:   {result['tables_match']:>4} / {result['tables_src']}")
        print(f"  Routines: {result['routines_match']:>4} / {result['routines_src']}"
              + (f"  ({len(result['routines_missing'])} missing: "
                 f"{result['routines_missing'][:5]})" if result["routines_missing"] else ""))
        print(f"  Views:    {result['views_spg']:>4} / {result['views_src']}")
        if result["col_mismatches"]:
            print(f"  Col Δ:    {len(result['col_mismatches'])} mismatches "
                  f"(sample ≤{args.sample_col_tables} tables)")

    total_tested = grand_pass + grand_fail
    pass_pct = round(grand_pass / total_tested * 100) if total_tested else 0

    parity_results = {
        "source":        args.source_type,
        "databases":     databases,
        "sample_col_tables": args.sample_col_tables,
        "grand": {
            "pass":     grand_pass,
            "fail":     grand_fail,
            "missing":  grand_missing,
            "spg_only": grand_spg_only,
        },
        "schemas": schema_results,
        "_is_structural": True,   # flag: tells html_report to use structural renderer
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(parity_results, indent=2))

    print(f"\n{'='*60}")
    print(f"PARITY SUMMARY")
    print(f"{'='*60}")
    print(f"  Pass:     {grand_pass:>6}  (tables + routines + views matched)")
    print(f"  Fail:     {grand_fail:>6}  (missing objects + real col mismatches)")
    print(f"  Missing:  {grand_missing:>6}  (in source, not in SPG)")
    print(f"  SPG-only: {grand_spg_only:>6}  (in SPG, not in source)")
    print(f"  Pass rate: {pass_pct}%")
    print(f"\nReport: {out}")

    scur.close()
    spg_conn.close()
    src_conn.close()


if __name__ == "__main__":
    main()
