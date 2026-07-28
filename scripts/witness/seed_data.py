#!/usr/bin/env python3
"""seed_data.py - Generate and insert referentially coherent synthetic data.

Referential coherence strategy:
  1. Build col_fk_map: for every table, map each FK column → (parent_fqn, parent_col)
     sourced from ALTER TABLE … FOREIGN KEY … REFERENCES DDL captured in inventory.
  2. Seed tables in wave order (parents always before children).
  3. After inserting each table, READ BACK the actual committed rows from the DB.
     This captures IDENTITY values automatically and handles NULL-filtered columns.
  4. When generating a value for an FK column, sample from the parent table's already-
     committed values using round-robin so all parent rows get referenced.
"""

import argparse
import json
import os
import random
import sys
import uuid
from datetime import date, timedelta
from typing import Any

try:
    import pymssql
except ImportError:
    print("ERROR: pymssql not installed.", file=sys.stderr)
    sys.exit(1)

try:
    from faker import Faker
    _fake = Faker()
    Faker.seed(42)
except ImportError:
    _fake = None

random.seed(42)

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(server: str, port: int, user: str, password: str, database: str) -> "pymssql.Connection":
    return pymssql.connect(
        server=server, port=port, user=user, password=password,
        database=database, timeout=10, login_timeout=10,
    )


# ---------------------------------------------------------------------------
# Column length metadata (fetched from live DB, not DDL text)
# ---------------------------------------------------------------------------

# Cache: (schema, table) -> {col_name_lower: max_char_length | None}
# None means the column is a MAX-length type (varchar(max), etc.) — no clamping.
_COL_LENGTH_CACHE: dict[tuple, dict[str, int | None]] = {}


def fetch_col_lengths(conn: "pymssql.Connection", schema: str, table: str) -> dict[str, int | None]:
    """Return {col_name_lower: max_char_length | None} for every column in the table.

    Derives the maximum *character* length from sys.columns.max_length:
      - char / varchar          : max_length bytes == max_length chars
      - nchar / nvarchar        : max_length bytes == max_length // 2 chars
      - any type with max_length = -1 (MAX types): None (unlimited)
      - all other types         : None (not a string — clamping irrelevant)
    Results are cached so we never hit sys.columns twice for the same table.
    """
    key = (schema.lower(), table.lower())
    if key in _COL_LENGTH_CACHE:
        return _COL_LENGTH_CACHE[key]

    cur = conn.cursor()
    cur.execute("""
        SELECT c.name, c.max_length, tp.name
        FROM sys.columns c
        JOIN sys.types tp ON c.user_type_id = tp.user_type_id
        JOIN sys.tables t  ON c.object_id    = t.object_id
        JOIN sys.schemas s ON t.schema_id    = s.schema_id
        WHERE s.name = %s AND t.name = %s
    """, (schema, table))
    result: dict[str, int | None] = {}
    for col_name, max_len, type_name in cur.fetchall():
        tn = type_name.lower()
        if max_len == -1:
            result[col_name.lower()] = None          # MAX type, no limit
        elif tn in ("nchar", "nvarchar", "ntext"):
            result[col_name.lower()] = max_len // 2  # bytes → chars
        elif tn in ("char", "varchar", "text"):
            result[col_name.lower()] = max_len       # bytes == chars for single-byte types
        else:
            result[col_name.lower()] = None          # non-string type, clamping irrelevant
    cur.close()
    _COL_LENGTH_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# Column-level FK map
# ---------------------------------------------------------------------------

def build_col_fk_map(objects: dict) -> dict[str, dict[str, tuple]]:
    """
    Returns col_fk_map[table_fqn][local_col_lower] = (ref_table_fqn, ref_col_name).

    For each FK constraint on a table, maps every local column to the corresponding
    referenced column in the parent table.  When the DDL omits the referenced column
    list we fall back to the first PK column of the parent table.
    """
    col_fk_map: dict[str, dict[str, tuple]] = {}
    for fqn, obj in objects.items():
        if obj["type"] != "TABLE":
            continue
        mapping: dict[str, tuple] = {}
        for fk in obj.get("fk_references", []):
            local_cols = fk.get("local_columns", [])
            ref_cols   = fk.get("ref_columns", [])
            ref_fqn    = f"{fk['ref_schema']}.{fk['ref_table']}"
            for i, lc in enumerate(local_cols):
                # Use the explicit ref column if available, else None (resolved at seed time)
                rc = ref_cols[i] if i < len(ref_cols) else None
                mapping[lc.lower()] = (ref_fqn, rc)
        col_fk_map[fqn] = mapping
    return col_fk_map


# ---------------------------------------------------------------------------
# Value generation
# ---------------------------------------------------------------------------

_NAME_HINTS: dict[str, str] = {
    "email": "email", "phone": "phone",
    "last_name": "last_name", "firstname": "first_name", "first_name": "first_name",
    "lastname": "last_name", "address": "address", "street": "address",
    "city": "city", "state": "state", "zip": "zipcode", "postal": "zipcode",
    "country": "country", "company": "company", "name": "name",
}


def _hint(col_name: str) -> str | None:
    lower = col_name.lower()
    for token, hint in _NAME_HINTS.items():
        if token in lower:
            return hint
    return None


def _clamp_str(value: str, max_len: int | None) -> str:
    """Clamp a string to max_len characters. No-op when max_len is None."""
    if max_len is not None and len(value) > max_len:
        return value[:max_len]
    return value


def gen_value(
    col: dict,
    row_idx: int,
    col_fk: dict[str, tuple],          # col_fk_map[this_table_fqn]
    col_value_store: dict[str, dict],   # {table_fqn: {col_name: [values]}}
    parent_pk: dict[str, list],         # {table_fqn: [pk_values]} — fallback
    exclude_realistic: set[str],
) -> Any:
    """Generate one value for `col` at position `row_idx`.

    FK columns get a real value from the parent table's committed rows.
    All other columns get type-appropriate synthetic data.
    String values are clamped to the column's actual max_length (set by
    seed_table before calling gen_value) so char(1) columns never receive
    multi-character values that would raise error 2628.
    """
    col_lower = col["name"].lower()
    dtype = col.get("data_type", "NVARCHAR").upper()

    if col.get("identity"):
        return None  # SQL Server auto-assigns

    # ------------------------------------------------------------------
    # FK resolution: use actual committed parent values
    # ------------------------------------------------------------------
    if col_lower in col_fk:
        ref_fqn, ref_col = col_fk[col_lower]

        # Resolve ref_col: use explicit mapping, or fall back to same-name col,
        # then to first PK col of parent, then to any non-null column
        resolved_col = ref_col or col["name"]
        parent_vals = (
            col_value_store.get(ref_fqn, {}).get(resolved_col)
            or col_value_store.get(ref_fqn, {}).get(resolved_col.lower())
        )
        if not parent_vals:
            # Try first PK of parent (case-insensitive)
            parent_store = col_value_store.get(ref_fqn, {})
            for _, vals in parent_store.items():
                if vals:
                    parent_vals = vals
                    break

        if parent_vals:
            # Round-robin so every parent row gets at least one child referencing it
            return parent_vals[row_idx % len(parent_vals)]

    # ------------------------------------------------------------------
    # Constraint-based generation (rules from SPG schema via dep_graph)
    # Populated when build_dep_graph.py is run with --constraints-file.
    # When value_constraint is None (standalone or unconstrained column),
    # falls through to the type-based generation below.
    # ------------------------------------------------------------------
    constraint = col.get("value_constraint")
    if constraint:
        ctype = constraint["type"]
        if ctype == "enum":
            vals = constraint["values"]
            return vals[row_idx % len(vals)]
        if ctype == "range":
            lo  = int(constraint["min"])
            hi  = int(constraint["max"])
            span = max(hi - lo + 1, 1)
            return lo + (row_idx % span)

    # ------------------------------------------------------------------
    # Type-based synthetic generation
    # ------------------------------------------------------------------
    if col_lower in exclude_realistic:
        return _placeholder(dtype, row_idx)

    if any(t in dtype for t in ("BIGINT", "INT", "SMALLINT", "TINYINT")):
        return row_idx + 1
    if any(t in dtype for t in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY", "SMALLMONEY")):
        return round(random.uniform(1.0, 999.99), 2)
    if "BIT" in dtype:
        return row_idx % 2
    if "TIME" == dtype.strip():
        return f"{row_idx % 24:02d}:00:00"
    if "DATE" in dtype and "TIME" not in dtype:
        return str(date(2024, 1, 1) + timedelta(days=row_idx % 365))
    if "DATETIME" in dtype or "TIMESTAMP" in dtype:
        return f"2024-{(row_idx % 12) + 1:02d}-{(row_idx % 28) + 1:02d} 10:00:00"
    if "UNIQUEIDENTIFIER" in dtype:
        return str(uuid.uuid4())
    if any(t in dtype for t in ("CHAR", "VARCHAR", "TEXT", "NCHAR", "NVARCHAR", "NTEXT")):
        # col["max_length"] is set by seed_table from sys.columns (None = unlimited).
        max_len: int | None = col.get("max_length")

        if _fake:
            hint = _hint(col["name"])
            if hint == "email":
                return _clamp_str(_fake.email(), max_len or 100)
            if hint in ("first_name", "name"):
                return _clamp_str(_fake.first_name(), max_len or 50)
            if hint == "last_name":
                return _clamp_str(_fake.last_name(), max_len or 50)
            if hint == "phone":
                return _clamp_str(_fake.phone_number(), max_len or 20)
            if hint == "address":
                return _clamp_str(_fake.street_address(), max_len or 80)
            if hint == "city":
                return _clamp_str(_fake.city(), max_len or 50)
            if hint == "state":
                return _clamp_str(_fake.state_abbr(), max_len or 2)
            if hint == "zipcode":
                return _clamp_str(_fake.zipcode(), max_len or 10)
            if hint == "country":
                return _clamp_str("US", max_len or 2)
            if hint == "company":
                return _clamp_str(_fake.company(), max_len or 50)
        return _clamp_str(f"v{row_idx + 1}", max_len)
    return row_idx + 1


def _placeholder(dtype: str, idx: int) -> Any:
    if any(t in dtype for t in ("INT", "FLOAT", "DECIMAL", "NUMERIC", "MONEY", "BIT")):
        return idx + 1
    if "DATE" in dtype:
        return "2024-01-01"
    if "TIME" == dtype.strip():
        return "10:00:00"
    return f"p{idx + 1}"


# ---------------------------------------------------------------------------
# Insert + read-back
# ---------------------------------------------------------------------------

def insert_and_readback(
    conn: "pymssql.Connection",
    schema: str,
    table: str,
    columns: list[dict],
    rows: list[dict],
) -> dict[str, list]:
    """Insert `rows` into the table and read back ALL column values.

    Returns {col_name: [val, val, …]} for every non-IDENTITY column.
    Reading back from the DB captures IDENTITY values automatically and gives
    us the exact values that FK children must reference.
    """
    non_identity = [c for c in columns if not c.get("identity")]
    if not non_identity or not rows:
        return {}

    col_clause   = ", ".join(f"[{c['name']}]" for c in non_identity)
    placeholders = ", ".join("%s" for _ in non_identity)
    sql_ins = f"INSERT INTO [{schema}].[{table}] ({col_clause}) VALUES ({placeholders})"

    cur = conn.cursor()
    inserted = 0
    for row in rows:
        vals = tuple(row.get(c["name"]) for c in non_identity)
        try:
            cur.execute(sql_ins, vals)
            inserted += 1
        except Exception as exc:
            # Log all failures; truncation (2628) errors now indicate a genuine
            # gap in the length-clamping logic and should be visible, not hidden.
            err = str(exc)[:120]
            if "2627" not in err:  # still suppress unique-constraint spam on re-runs
                print(f"    Insert warn [{schema}].[{table}]: {err}")
    conn.commit()
    cur.close()

    if inserted == 0:
        return {}

    # Read back every column (including IDENTITY) so FK children can use real values
    all_col_clause = ", ".join(f"[{c['name']}]" for c in columns)
    sql_sel = f"SELECT TOP 200 {all_col_clause} FROM [{schema}].[{table}]"
    cur = conn.cursor()
    cur.execute(sql_sel)
    db_rows = cur.fetchall()
    cur.close()

    result: dict[str, list] = {c["name"]: [] for c in columns}
    for db_row in db_rows:
        for i, col in enumerate(columns):
            v = db_row[i]
            if v is not None:
                result[col["name"]].append(v)
    return result


# ---------------------------------------------------------------------------
# Per-table seeding
# ---------------------------------------------------------------------------

def seed_table(
    conn: "pymssql.Connection",
    obj: dict,
    col_fk: dict[str, tuple],          # col_fk_map[this_fqn]
    col_value_store: dict[str, dict],   # populated parent values
    parent_pk: dict[str, list],         # legacy fallback
    row_volume: int,
    exclude_realistic: set[str],
    column_value_constraints: dict | None = None,  # from dep_graph, keyed by fqn
) -> dict[str, list]:
    """Seed `row_volume` rows, then return the read-back column value map.

    Three correctness rules applied here:

    1. String-length clamping
       Fetches actual max_length from sys.columns and sets col["max_length"]
       on every column dict before calling gen_value.  gen_value uses this
       to clamp generated strings, preventing error 2628 (truncation) that
       silently causes all rows in a table to be skipped.

    2. SPG check constraint rules
       When column_value_constraints contains entries for this table (populated
       from spg_column_constraints.json via build_dep_graph.py), gen_value uses
       them to produce semantically correct values before falling through to
       type-based generation.  When absent (standalone realization), gen_value
       uses type-based generation only.  Fully rules-driven: rules come from
       pg_constraint introspection, not hardcoded column names or values.

    3. Self-referential FK bootstrap
       When a FK column references the same table (e.g. Users.CreatedBy →
       Users.UserName), the parent rows don't exist yet at seed time.  We
       detect these columns purely from the FK map (no column-name hardcoding)
       and build each row in two passes:
         Pass 1 — generate all non-self-FK columns (including PK columns).
         Pass 2 — for every self-FK column, look up the referenced column's
                  value from pass-1 output and copy it.  This ensures the
                  self-referencing value is always valid in the same row.
    """
    schema, name = obj["schema"], obj["name"]
    this_fqn = obj["fqn"]
    columns  = obj.get("columns", [])

    if not columns:
        return {}

    # --- Rules 1 & 2: enrich column dicts with max_length + value_constraint ---
    col_lengths = fetch_col_lengths(conn, schema, name)
    # Constraint rules from SPG (empty dict when running standalone)
    constraints_for_table = (column_value_constraints or {}).get(this_fqn.lower(), {})
    enriched_columns = [
        {
            **c,
            "max_length":       col_lengths.get(c["name"].lower()),
            "value_constraint": constraints_for_table.get(c["name"].lower()),
        }
        for c in columns
    ]

    # --- Rule 2: identify self-referential FK columns (purely from FK map) ---
    self_fk_cols: dict[str, str] = {}   # col_name_lower -> referenced_col_name
    for col_lower, (ref_fqn, ref_col) in col_fk.items():
        if ref_fqn == this_fqn:
            # ref_col is the column in THIS table the FK points to
            self_fk_cols[col_lower] = ref_col or col_lower  # fallback: same name

    rows = []
    for i in range(row_volume):
        # Pass 1: generate every non-identity, non-self-FK column
        row: dict[str, Any] = {}
        for col in enriched_columns:
            if col.get("identity"):
                continue
            if col["name"].lower() in self_fk_cols:
                continue  # handled in pass 2
            row[col["name"]] = gen_value(
                col, i, col_fk, col_value_store, parent_pk, exclude_realistic
            )

        # Pass 2: fill self-FK columns using the same row's already-generated value
        for col in enriched_columns:
            if col.get("identity"):
                continue
            col_lower = col["name"].lower()
            if col_lower not in self_fk_cols:
                continue
            ref_col_name = self_fk_cols[col_lower]
            # Find the referenced column's value from pass-1 output (case-insensitive)
            ref_val = row.get(ref_col_name) or next(
                (v for k, v in row.items() if k.lower() == ref_col_name.lower()), None
            )
            if ref_val is not None:
                row[col["name"]] = ref_val
            else:
                # Referenced column wasn't generated in pass 1 (e.g. it is itself a
                # self-FK or IDENTITY); fall back to normal generation so the row
                # can still be inserted.
                row[col["name"]] = gen_value(
                    col, i, col_fk, col_value_store, parent_pk, exclude_realistic
                )
        rows.append(row)

    return insert_and_readback(conn, schema, name, enriched_columns, rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Seed referentially coherent synthetic data")
    p.add_argument("--inventory",     required=True)
    p.add_argument("--dep-graph",     required=True)
    p.add_argument("--deploy-report", required=True)
    p.add_argument("--server",  default="localhost")
    p.add_argument("--port",    type=int, default=1433)
    p.add_argument("--user",    default="sa")
    p.add_argument("--password", required=True)
    p.add_argument("--database", default="RealizationDB")
    p.add_argument("--output",   required=True)
    p.add_argument("--row-volume", type=int, default=3)
    p.add_argument("--exclude-realistic-columns", default="")
    p.add_argument("--debug-object", default=None)
    args = p.parse_args()

    exclude_realistic = {
        c.strip().lower() for c in args.exclude_realistic_columns.split(",") if c.strip()
    }

    with open(args.inventory) as f:
        inventory = json.load(f)
    with open(args.dep_graph) as f:
        dep_graph = json.load(f)
    with open(args.deploy_report) as f:
        deploy_report = json.load(f)

    deployed = set(deploy_report.get("succeeded", []))
    objects  = {o["fqn"]: o for o in inventory["objects"]}
    waves: list[list[str]] = dep_graph["waves"]

    # Column value constraints from SPG (populated by build_dep_graph.py when
    # --constraints-file is supplied; empty dict for standalone realization).
    col_value_constraints: dict = dep_graph.get("column_value_constraints", {})
    if col_value_constraints:
        total_rules = sum(len(v) for v in col_value_constraints.values())
        print(f"Column value constraints loaded: {total_rules} rule(s) across "
              f"{len(col_value_constraints)} table(s)")

    # Build FK map from enriched inventory
    col_fk_map = build_col_fk_map(objects)
    fk_total   = sum(len(v) for v in col_fk_map.values())
    print(f"FK column mappings: {fk_total} across {sum(1 for v in col_fk_map.values() if v)} tables")

    conn = connect(args.server, args.port, args.user, args.password, args.database)

    # ------------------------------------------------------------------
    # Clear existing data and reset IDENTITY counters to 0.
    # IDENTITY counters don't reset on DELETE — if we re-seed after a previous
    # run, IDENTITY PKs would start where they left off (e.g. 101+), breaking
    # accidental JOIN alignment for tables without FK constraints.
    # ------------------------------------------------------------------
    print("Clearing existing data and resetting IDENTITY counters…")
    cur = conn.cursor()
    cur.execute("EXEC sp_msforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT ALL'")
    conn.commit()
    cur.execute("EXEC sp_msforeachtable 'DELETE FROM ?'")
    conn.commit()
    # Reset every IDENTITY column to 0 so first insert gets ID = 1
    cur.execute("""
        SELECT s.name + '.' + t.name
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE OBJECTPROPERTY(t.object_id, 'TableHasIdentity') = 1
    """)
    identity_tables = [r[0] for r in cur.fetchall()]
    for tbl in identity_tables:
        try:
            cur.execute(f"DBCC CHECKIDENT ('{tbl}', RESEED, 0)")
        except Exception:
            pass
    conn.commit()
    cur.execute("EXEC sp_msforeachtable 'ALTER TABLE ? WITH CHECK CHECK CONSTRAINT ALL'")
    conn.commit()
    cur.close()
    print(f"  Cleared all rows; reset {len(identity_tables)} IDENTITY counters to 0")

    # col_value_store[fqn][col_name] = [committed values]
    # Built incrementally as we seed each table — children query parent values here
    col_value_store: dict[str, dict[str, list]] = {}
    parent_pk: dict[str, list] = {}  # legacy fallback (first PK col values)

    seed_results: dict[str, Any] = {}

    print(f"Seeding {args.row_volume} rows per table (wave order, FK-coherent)…")

    for wave in waves:
        for fqn in wave:
            if fqn not in objects:
                continue
            obj = objects[fqn]
            if obj["type"] != "TABLE":
                continue
            if fqn not in deployed:
                seed_results[fqn] = "skipped — not deployed"
                continue

            col_vals = seed_table(
                conn, obj,
                col_fk_map.get(fqn, {}),
                col_value_store,
                parent_pk,
                args.row_volume,
                exclude_realistic,
                col_value_constraints,  # rules from dep_graph; {} when standalone
            )
            col_value_store[fqn] = col_vals

            # Legacy pk_store: first PK column values, for any fallback paths
            pk_cols = obj.get("pk_columns", [])
            if pk_cols and pk_cols[0] in col_vals:
                parent_pk[fqn] = col_vals[pk_cols[0]]

            row_count = len(next(iter(col_vals.values()), []))
            seed_results[fqn] = {
                "rows_seeded": row_count,
                "col_sample": {k: v[:3] for k, v in col_vals.items()},
            }
            print(f"  Seeded {row_count} rows → {fqn}")

    conn.close()

    report: dict[str, Any] = {
        "seed_results": seed_results,
        "row_volume": args.row_volume,
        "fk_mappings_total": fk_total,
        "summary": {
            "tables_seeded":  sum(1 for v in seed_results.values() if isinstance(v, dict) and v.get("rows_seeded", 0) > 0),
            "tables_zero_rows": sum(1 for v in seed_results.values() if isinstance(v, dict) and v.get("rows_seeded", 0) == 0),
            "tables_skipped": sum(1 for v in seed_results.values() if isinstance(v, str)),
        },
    }

    if args.debug_object and args.debug_object in dep_graph.get("witness_paths", {}):
        report["debug_witness_path"] = {
            args.debug_object: dep_graph["witness_paths"][args.debug_object]
        }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2, default=str)

    s = report["summary"]
    print(f"\nSeeding complete:")
    print(f"  Tables with rows:  {s['tables_seeded']}")
    print(f"  Tables zero rows:  {s['tables_zero_rows']}")
    print(f"  Tables skipped:    {s['tables_skipped']}")
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
