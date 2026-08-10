#!/usr/bin/env python3
"""discover_spg_constraints.py - Discover SPG check constraints and emit them as
a shared artifact for upstream seed generation.

Owned by: mssql-spg-load skill (the SPG bridge skill).

Usage:
    python3 discover_spg_constraints.py \
        --spg-host  yourhost.aws.postgres.snowflake.app \
        --spg-user  snowflake_admin \
        --spg-password ... \
        --spg-database acuity \
        --output /path/to/shared/spg_column_constraints.json

Output file is consumed by build_dep_graph.py via --constraints-file.
When that file is absent, build_dep_graph.py emits an empty constraints dict
and realization works standalone without any SPG knowledge.

Why this lives here, not in mssql-ddl-realization:
    mssql-ddl-realization has no required dependency on any other skill.
    SPG constraint discovery requires a live SPG connection, which only
    mssql-spg-load can mandate.  The constraints file is an optional
    accelerator consumed by realization when present.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2-binary not installed — pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constraint parser
# ---------------------------------------------------------------------------

def parse_constraint_def(constraint_def: str) -> dict | None:
    """Parse a PostgreSQL CHECK constraint definition into a semantic rule.

    Handles two patterns observed from pg_get_constraintdef():

    Pattern 1 — OR-equality enum:
        CHECK (((col = 'N'::bpchar) OR (col = 'Y'::bpchar)))
        CHECK (((col = 'N'::bpchar) OR (col = 'Y'::bpchar) OR (col IS NULL)))
        → {"type": "enum", "values": ["N", "Y"]}

    Pattern 2 — AND-comparison range:
        CHECK (((shiftendtime >= 0) AND (shiftendtime <= 25)))
        → {"type": "range", "min": 0.0, "max": 25.0}

    Returns None when the definition matches neither pattern — in that case
    no constraint is applied to the column and the seeder falls through to
    its normal type-based generation.

    No column names, table names, or domain values are hardcoded here.
    The parser operates purely on the structural shape of the constraint text.
    """
    # Pattern 1: extract all quoted literals that appear after a cast operator
    # e.g.  = 'N'::bpchar  or  = 'Y'::character varying
    enum_vals = re.findall(r"= '([^']+)'::", constraint_def)
    if enum_vals:
        # Deduplicate while preserving first-occurrence order
        seen: dict[str, None] = {}
        unique = [seen.setdefault(v, v) for v in enum_vals if v not in seen]
        return {"type": "enum", "values": unique}

    # Pattern 2: numeric range expressed as >= lower AND <= upper
    # Handles optional parentheses around the number: >= (0) or >= 0
    min_m = re.search(r">= \(?(-?\d+(?:\.\d+)?)\)?", constraint_def)
    max_m = re.search(r"<= \(?(-?\d+(?:\.\d+)?)\)?", constraint_def)
    if min_m and max_m:
        return {
            "type": "range",
            "min": float(min_m.group(1)),
            "max": float(max_m.group(1)),
        }

    # Unparseable — no constraint applied
    return None


# ---------------------------------------------------------------------------
# SPG discovery
# ---------------------------------------------------------------------------

def fetch_check_constraints(conn, schemas) -> dict[str, dict[str, dict]]:
    """Query SPG for all CHECK constraints in the given schema(s).

    Arguments may be a single schema name or an iterable of schema names.
    System schemas (pg_%, information_schema, snowflake%, lake%) are always
    excluded so multi-DB MySQL/MariaDB migrations can be inspected by passing
    every source schema.

    Returns:
        {mssql_table_fqn: {col_name_lower: rule}}

    where rule is:
        {"type": "enum",  "values": ["N", "Y"]}
      | {"type": "range", "min": 0.0, "max": 25.0}

    The returned FQN uses lowercase to match the lookup key used in seed_data.py
    (obj["fqn"].lower()).
    """
    if isinstance(schemas, str):
        schemas = [schemas]
    schemas = [s for s in schemas if s.strip()]
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            n.nspname                    AS schema_name,
            t.relname                    AS table_name,
            a.attname                    AS col_name,
            pg_get_constraintdef(c.oid)  AS constraint_def
        FROM pg_constraint c
        JOIN pg_class     t  ON c.conrelid    = t.oid
        JOIN pg_namespace n  ON t.relnamespace = n.oid
        JOIN pg_attribute a  ON a.attrelid    = t.oid
                             AND a.attnum      = ANY(c.conkey)
        WHERE c.contype = 'c'
          AND (%(schema_filter)s)
          AND n.nspname NOT LIKE 'pg~_%%' ESCAPE '~'
          AND n.nspname NOT LIKE 'information_schema'
          AND n.nspname NOT LIKE 'snowflake~_%%' ESCAPE '~'
          AND n.nspname NOT LIKE 'lake~_%%' ESCAPE '~'
        ORDER BY t.relname, a.attname
        """.replace(
            "%(schema_filter)s",
            "n.nspname = ANY(%s)" if schemas else "TRUE",
        ),
        (schemas,) if schemas else (),
    )
    rows = cur.fetchall()
    cur.close()

    result: dict[str, dict[str, dict]] = {}
    unparseable = []

    for schema_name, table_name, col_name, constraint_def in rows:
        rule = parse_constraint_def(constraint_def)
        if rule is None:
            unparseable.append((f"{schema_name}.{table_name}", col_name, constraint_def))
            continue
        # Prefix with the schema actually inspected (NOT hardcoded dbo) and
        # lowercase so seed_data.py lookups are case-insensitive.
        fqn = f"{schema_name}.{table_name.lower()}"
        result.setdefault(fqn, {})[col_name.lower()] = rule

    if unparseable:
        print(f"  Note: {len(unparseable)} constraint(s) not parsed (no rule applied):")
        for tbl, col, cdef in unparseable:
            print(f"    {tbl}.{col}: {cdef[:80]}")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Discover SPG check constraints for constraint-driven seed generation"
    )
    p.add_argument("--spg-host",     required=True)
    p.add_argument("--spg-port",     type=int, default=5432)
    p.add_argument("--spg-user",     required=True)
    p.add_argument("--spg-password", required=True)
    p.add_argument("--spg-database", required=True)
    p.add_argument("--schema",       default="",
                   help="Comma-separated SPG schemas to inspect. Empty = all "
                        "non-system schemas. (Default: all)")
    p.add_argument("--output",
                   default="spg_column_constraints.json",
                   help="Path to write spg_column_constraints.json "
                        "(default: ./spg_column_constraints.json)")
    args = p.parse_args()

    schemas = [s.strip() for s in args.schema.split(",") if s.strip()]
    print(f"Connecting to SPG {args.spg_host}/{args.spg_database}…")
    conn = psycopg2.connect(
        host=args.spg_host,
        port=args.spg_port,
        user=args.spg_user,
        password=args.spg_password,
        dbname=args.spg_database,
        sslmode="require",
        connect_timeout=30,
    )

    print(f"  Discovering CHECK constraints in schema(s) '{', '.join(schemas) or 'ALL'}'…")
    constraints = fetch_check_constraints(conn, schemas)
    conn.close()

    total_rules = sum(len(v) for v in constraints.values())
    tables_with_constraints = len(constraints)
    print(f"  Found {total_rules} column constraint rule(s) across "
          f"{tables_with_constraints} table(s)")

    for tbl_fqn, cols in sorted(constraints.items()):
        for col, rule in sorted(cols.items()):
            if rule["type"] == "enum":
                print(f"    {tbl_fqn}.{col}: enum {rule['values']}")
            elif rule["type"] == "range":
                print(f"    {tbl_fqn}.{col}: range [{rule['min']}, {rule['max']}]")

    output = {
        "schema":       schemas or ["all"],
        "spg_host":     args.spg_host,
        "spg_database": args.spg_database,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_rules":  total_rules,
        "constraints":  constraints,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Written to: {args.output}")


if __name__ == "__main__":
    main()
