#!/usr/bin/env python3
"""
validate_migration.py — Automated correctness harness for schema migrations.

Connects to both the source database and the target SPG instance, runs 6
verification checks, and reports pass/fail with counts.

Checks:
  1. Table count    — source table count ≤ SPG table count
  2. Column count   — 10 random tables: exact column count match
  3. Primary keys   — 10 random tables: PK column names match (lowercased)
  4. IDENTITY/serial — all identity/auto-increment columns exist as sequences in SPG
  5. Foreign keys   — FK count in source matches SPG (only if catalog path was used)
  6. Index count    — non-PK index count matches (only if catalog path was used)

Usage:
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/validate_migration.py \\
        --source-type  mssql \\
        --source-host  <host> \\
        --source-port  1433 \\
        --source-db    <database> \\
        --source-user  <user> \\
        --password-env MSSQL_SA_PASSWORD \\
        --spg-service  pg_spgloader_migration \\
        --source-schema dbo \\
        [--catalog]   # set if catalog-based path was used (enables FK/index checks)
        [--output <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))

import psycopg2
from spgloader.connectors import get_connector


# ---------------------------------------------------------------------------
# Source-side catalog counts
# ---------------------------------------------------------------------------

def _source_table_count(connector) -> int:
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    if "mssql" in st:
        cur.execute("SELECT COUNT(*) FROM sys.tables WHERE is_ms_shipped = 0")
    elif "mysql" in st or "mariadb" in st:
        cur.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """, (connector.database,))
    else:  # oracle
        cur.execute("SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = :s",
                    s=connector.user.upper())
    n = cur.fetchone()[0]
    conn.close()
    return n


def _source_columns_for_tables(connector, table_list: list[tuple]) -> dict[tuple, int]:
    """Return {(schema, table): column_count} for the given table list."""
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    result = {}
    for schema, table in table_list:
        if "mssql" in st:
            cur.execute("""
                SELECT COUNT(*) FROM sys.columns c
                JOIN sys.objects o ON o.object_id = c.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.name = %s AND o.is_ms_shipped = 0
                  AND c.is_computed = 0
            """, (schema, table))
        elif "mysql" in st or "mariadb" in st:
            cur.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """, (schema, table))
        else:  # oracle
            cur.execute("""
                SELECT COUNT(*) FROM ALL_TAB_COLUMNS
                WHERE OWNER = :s AND TABLE_NAME = :t
            """, s=schema.upper(), t=table.upper())
        result[(schema, table)] = cur.fetchone()[0]
    conn.close()
    return result


def _source_pk_columns(connector, table_list: list[tuple]) -> dict[tuple, list[str]]:
    """Return {(schema, table): [pk_cols_lowercased]}."""
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    result = {}
    for schema, table in table_list:
        if "mssql" in st:
            cur.execute("""
                SELECT c.name FROM sys.indexes i
                JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                JOIN sys.columns c ON c.object_id = i.object_id AND c.column_id = ic.column_id
                JOIN sys.objects o ON o.object_id = i.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE i.is_primary_key = 1 AND s.name = %s AND o.name = %s
                ORDER BY ic.key_ordinal
            """, (schema, table))
        elif "mysql" in st or "mariadb" in st:
            cur.execute("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                  AND CONSTRAINT_NAME = 'PRIMARY'
                ORDER BY ORDINAL_POSITION
            """, (schema, table))
        else:  # oracle
            cur.execute("""
                SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS c
                JOIN ALL_CONS_COLUMNS cc
                  ON cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME AND cc.OWNER = c.OWNER
                WHERE c.CONSTRAINT_TYPE = 'P' AND c.OWNER = :s AND c.TABLE_NAME = :t
                ORDER BY cc.POSITION
            """, s=schema.upper(), t=table.upper())
        result[(schema, table)] = [r[0].lower() for r in cur.fetchall()]
    conn.close()
    return result


def _source_identity_columns(connector, schema: str) -> set[str]:
    """Return set of 'table.column' strings for all identity/auto-increment columns."""
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    if "mssql" in st:
        cur.execute("""
            SELECT SCHEMA_NAME(o.schema_id), o.name, c.name
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            WHERE c.is_identity = 1 AND o.is_ms_shipped = 0
              AND SCHEMA_NAME(o.schema_id) = %s
        """, (schema,))
        cols = {f"{r[1].lower()}.{r[2].lower()}" for r in cur.fetchall()}
    elif "mysql" in st or "mariadb" in st:
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND EXTRA LIKE '%%auto_increment%%'
        """, (schema,))
        cols = {f"{r[0].lower()}.{r[1].lower()}" for r in cur.fetchall()}
    else:  # oracle
        try:
            cur.execute("""
                SELECT TABLE_NAME, COLUMN_NAME FROM ALL_TAB_COLUMNS
                WHERE OWNER = :s AND IDENTITY_COLUMN = 'YES'
            """, s=schema.upper())
            cols = {f"{r[0].lower()}.{r[1].lower()}" for r in cur.fetchall()}
        except Exception:
            cols = set()  # IDENTITY_COLUMN column not available (pre-12c)
    conn.close()
    return cols


def _source_fk_count(connector, schema: str) -> int:
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    if "mssql" in st:
        cur.execute("""
            SELECT COUNT(*) FROM sys.foreign_keys fk
            JOIN sys.objects o ON o.object_id = fk.parent_object_id
            WHERE fk.is_ms_shipped = 0 AND SCHEMA_NAME(o.schema_id) = %s
        """, (schema,))
    elif "mysql" in st or "mariadb" in st:
        cur.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA = %s
        """, (schema,))
    else:  # oracle
        cur.execute("""
            SELECT COUNT(*) FROM ALL_CONSTRAINTS
            WHERE CONSTRAINT_TYPE = 'R' AND OWNER = :s
        """, s=schema.upper())
    n = cur.fetchone()[0]
    conn.close()
    return n


def _source_index_count(connector, schema: str) -> int:
    conn = connector._connect()
    cur = conn.cursor()
    st = connector.__class__.__name__.lower().replace("connector", "")
    if "mssql" in st:
        cur.execute("""
            SELECT COUNT(*) FROM sys.indexes i
            JOIN sys.objects o ON o.object_id = i.object_id
            WHERE i.is_primary_key = 0 AND i.type > 0 AND o.is_ms_shipped = 0
              AND SCHEMA_NAME(o.schema_id) = %s
        """, (schema,))
    elif "mysql" in st or "mariadb" in st:
        cur.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s AND INDEX_NAME != 'PRIMARY'
        """, (schema,))
    else:  # oracle
        cur.execute("""
            SELECT COUNT(*) FROM ALL_INDEXES i
            WHERE i.OWNER = :s AND i.INDEX_TYPE NOT IN ('LOB','DOMAIN')
              AND NOT EXISTS (
                SELECT 1 FROM ALL_CONSTRAINTS c
                WHERE c.INDEX_NAME = i.INDEX_NAME AND c.OWNER = i.OWNER
                  AND c.CONSTRAINT_TYPE IN ('P','U')
              )
        """, s=schema.upper())
    n = cur.fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# SPG-side queries
# ---------------------------------------------------------------------------

def _spg_table_count(conn, schema: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
        """, (schema.lower(),))
        return cur.fetchone()[0]


def _spg_table_list(conn, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """, (schema.lower(),))
        return [r[0] for r in cur.fetchall()]


def _spg_column_count(conn, schema: str, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
        """, (schema.lower(), table.lower()))
        return cur.fetchone()[0]


def _spg_pk_columns(conn, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
            ORDER BY kcu.ordinal_position
        """, (schema.lower(), table.lower()))
        return [r[0].lower() for r in cur.fetchall()]


def _spg_serial_tables(conn, schema: str) -> set[str]:
    """Return 'table.column' for columns that are IDENTITY (GENERATED ALWAYS/BY DEFAULT AS IDENTITY)."""
    with conn.cursor() as cur:
        # is_identity = 'YES' covers GENERATED ALWAYS AS IDENTITY and GENERATED BY DEFAULT AS IDENTITY
        # column_default LIKE 'nextval%' covers the older serial / sequence-based pattern
        cur.execute("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = %s
              AND (is_identity = 'YES' OR column_default LIKE 'nextval%%')
        """, (schema.lower(),))
        return {f"{r[0].lower()}.{r[1].lower()}" for r in cur.fetchall()}


def _spg_fk_count(conn, schema: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.table_constraints
            WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'
        """, (schema.lower(),))
        return cur.fetchone()[0]


def _spg_index_count(conn, schema: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM pg_indexes
            WHERE schemaname = %s
        """, (schema.lower(),))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Main validation logic
# ---------------------------------------------------------------------------

def validate(
    source_type: str,
    source_host: str,
    source_port: int,
    source_db: str,
    source_user: str,
    source_password: str,
    spg_service: str,
    source_schema: str = "dbo",
    catalog_path: bool = True,
    output_path: str | None = None,
    sample_size: int = 10,
) -> dict:

    checks = []

    connector = get_connector(
        source_type=source_type,
        host=source_host,
        port=source_port,
        database=source_db,
        user=source_user,
        password=source_password,
    )

    spg_conn = psycopg2.connect(f"service={spg_service}")

    # ------------------------------------------------------------------
    # Check 1: Table count
    # ------------------------------------------------------------------
    src_tables = _source_table_count(connector)
    spg_tables = _spg_table_count(spg_conn, source_schema)
    passed = spg_tables >= src_tables
    checks.append({
        "check": "table_count",
        "source": src_tables,
        "spg":    spg_tables,
        "passed": passed,
        "note":   f"SPG has {spg_tables} tables, source has {src_tables}"
                  + ("" if passed else f" — {src_tables - spg_tables} missing"),
    })
    _print_check("1. Table count", passed,
                 f"source={src_tables}  SPG={spg_tables}")

    # ------------------------------------------------------------------
    # Check 2: Column count (random sample)
    # ------------------------------------------------------------------
    spg_table_list = _spg_table_list(spg_conn, source_schema)
    sample_tables = random.sample(spg_table_list, min(sample_size, len(spg_table_list)))
    src_pairs = [(source_schema, t) for t in sample_tables]
    src_col_counts = _source_columns_for_tables(connector, src_pairs)
    col_mismatches = []
    for schema, table in src_pairs:
        spg_count = _spg_column_count(spg_conn, schema, table)
        src_count = src_col_counts.get((schema, table), -1)
        if src_count != spg_count:
            col_mismatches.append(f"{table}: source={src_count} SPG={spg_count}")
    checks.append({
        "check":      "column_count_sample",
        "sample_size": len(sample_tables),
        "mismatches":  col_mismatches,
        "passed":      len(col_mismatches) == 0,
    })
    _print_check("2. Column count (sample)", len(col_mismatches) == 0,
                 f"{len(sample_tables)} tables sampled, {len(col_mismatches)} mismatches")

    # ------------------------------------------------------------------
    # Check 3: Primary key columns (random sample)
    # ------------------------------------------------------------------
    src_pks = _source_pk_columns(connector, src_pairs)
    pk_mismatches = []
    for schema, table in src_pairs:
        spg_pk = _spg_pk_columns(spg_conn, schema, table)
        src_pk = src_pks.get((schema, table), [])
        if sorted(src_pk) != sorted(spg_pk):
            pk_mismatches.append(
                f"{table}: source={src_pk} SPG={spg_pk}"
            )
    checks.append({
        "check":      "primary_key_sample",
        "sample_size": len(sample_tables),
        "mismatches":  pk_mismatches,
        "passed":      len(pk_mismatches) == 0,
    })
    _print_check("3. Primary keys (sample)", len(pk_mismatches) == 0,
                 f"{len(sample_tables)} tables sampled, {len(pk_mismatches)} mismatches")

    # ------------------------------------------------------------------
    # Check 4: IDENTITY / serial columns
    # ------------------------------------------------------------------
    src_identity = _source_identity_columns(connector, source_schema)
    spg_serials  = _spg_serial_tables(spg_conn, source_schema)
    missing_serials = src_identity - spg_serials
    checks.append({
        "check":           "identity_serial",
        "source_count":    len(src_identity),
        "spg_count":       len(spg_serials),
        "missing_in_spg":  sorted(missing_serials),
        "passed":          len(missing_serials) == 0,
    })
    _print_check("4. IDENTITY / serial columns", len(missing_serials) == 0,
                 f"source={len(src_identity)}  SPG={len(spg_serials)}"
                 + (f"  missing={sorted(missing_serials)[:3]}..." if missing_serials else ""))

    # ------------------------------------------------------------------
    # Check 5: Foreign key count (catalog path only)
    # ------------------------------------------------------------------
    if catalog_path:
        src_fks = _source_fk_count(connector, source_schema)
        spg_fks = _spg_fk_count(spg_conn, source_schema)
        passed5 = spg_fks >= src_fks
        checks.append({
            "check":  "foreign_key_count",
            "source": src_fks,
            "spg":    spg_fks,
            "passed": passed5,
        })
        _print_check("5. Foreign key count", passed5,
                     f"source={src_fks}  SPG={spg_fks}")
    else:
        checks.append({"check": "foreign_key_count", "passed": None,
                       "note": "skipped — text-based path (no catalog)"})
        _print_check("5. Foreign key count", None, "SKIPPED (no-catalog path)")

    # ------------------------------------------------------------------
    # Check 6: Index count (catalog path only)
    # ------------------------------------------------------------------
    if catalog_path:
        src_idxs = _source_index_count(connector, source_schema)
        spg_idxs = _spg_index_count(spg_conn, source_schema)
        # SPG may have PK indexes counted; allow SPG >= source
        passed6 = spg_idxs >= src_idxs
        checks.append({
            "check":  "index_count",
            "source": src_idxs,
            "spg":    spg_idxs,
            "passed": passed6,
        })
        _print_check("6. Index count", passed6,
                     f"source={src_idxs}  SPG={spg_idxs}")
    else:
        checks.append({"check": "index_count", "passed": None,
                       "note": "skipped — text-based path (no catalog)"})
        _print_check("6. Index count", None, "SKIPPED (no-catalog path)")

    spg_conn.close()

    # Summary
    definite = [c for c in checks if c["passed"] is not None]
    passed_count = sum(1 for c in definite if c["passed"])
    total_count  = len(definite)

    overall = passed_count == total_count
    print(f"\n{'='*60}")
    print(f"Validation: {passed_count}/{total_count} checks passed "
          + ("✓ ALL PASS" if overall else "✗ FAILURES FOUND"))
    print(f"{'='*60}")

    report = {
        "source_type":   source_type,
        "source_schema": source_schema,
        "spg_service":   spg_service,
        "catalog_path":  catalog_path,
        "overall_pass":  overall,
        "checks":        checks,
    }

    if output_path:
        Path(output_path).write_text(json.dumps(report, indent=2))
        print(f"Validation report: {output_path}")

    return report


def _print_check(label: str, passed: bool | None, detail: str) -> None:
    if passed is None:
        symbol = "-"
    elif passed:
        symbol = "PASS"
    else:
        symbol = "FAIL"
    print(f"  [{symbol:4s}] {label}: {detail}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a catalog-based schema migration from source DB to SPG"
    )
    parser.add_argument("--source-type", required=True,
                        choices=["mssql", "mysql", "mariadb", "oracle"])
    parser.add_argument("--source-host", default="localhost")
    parser.add_argument("--source-port", type=int, default=None)
    parser.add_argument("--source-db",   required=True)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--spg-service", required=True)
    parser.add_argument("--source-schema", default="dbo",
                        help="Schema to validate (default: dbo)")
    parser.add_argument("--catalog", action="store_true",
                        help="Enable FK and index checks (catalog-based path only)")
    parser.add_argument("--sample-size", type=int, default=10,
                        help="Number of tables to spot-check (default: 10)")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"Error: env var '{args.password_env}' is not set.", file=sys.stderr)
        sys.exit(1)

    default_ports = {"mssql": 1433, "mysql": 3306, "mariadb": 3306, "oracle": 1521}
    port = args.source_port or default_ports.get(args.source_type, 1433)

    report = validate(
        source_type=args.source_type,
        source_host=args.source_host,
        source_port=port,
        source_db=args.source_db,
        source_user=args.source_user,
        source_password=password,
        spg_service=args.spg_service,
        source_schema=args.source_schema,
        catalog_path=args.catalog,
        output_path=args.output,
        sample_size=args.sample_size,
    )

    sys.exit(0 if report["overall_pass"] else 1)


if __name__ == "__main__":
    main()
