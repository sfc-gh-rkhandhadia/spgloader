#!/usr/bin/env python3
"""Load data from source DB (MSSQL/MySQL/MariaDB/Oracle) to SPG table-by-table."""
import json
import sys
import uuid
import re
import os
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from source_adapter import build_adapter
import psycopg2
import psycopg2.extras
psycopg2.extras.register_uuid()

SOURCE_TYPE = os.environ.get('SOURCE_TYPE', 'mssql').lower()
_adapter    = build_adapter()

# Read source DB credentials from SOURCE_* env vars (with MSSQL_* aliases)
SOURCE_HOST = os.environ.get('SOURCE_HOST', os.environ.get('MSSQL_HOST', 'localhost'))
SOURCE_PORT = int(os.environ.get('SOURCE_PORT', os.environ.get('MSSQL_PORT', '1433')))
SOURCE_USER = os.environ.get('SOURCE_USER', os.environ['MSSQL_USER']) if os.environ.get('MSSQL_USER') else os.environ['SOURCE_USER']
SOURCE_PASS = os.environ.get('SOURCE_PASSWORD', os.environ.get('MSSQL_PASSWORD', ''))
SOURCE_DB   = os.environ.get('SOURCE_DATABASE', os.environ.get('MSSQL_DATABASE', ''))

SPG_HOST   = os.environ["SPG_HOST"]
SPG_PORT   = int(os.environ.get("SPG_PORT", "5432"))
SPG_USER   = os.environ["SPG_USER"]
SPG_PASS   = os.environ["SPG_PASSWORD"]
SPG_DB     = os.environ["SPG_DATABASE"]

SHARED_DIR = Path(os.environ.get('SHARED_DIR', os.environ.get('MSSQL_SPG_SHARED_DIR', str(Path(__file__).parent))))

inv = json.loads((SHARED_DIR / "object_inventory.json").read_text())

def mc():
    return _adapter.connect()

def sc():
    return psycopg2.connect(host=SPG_HOST, port=SPG_PORT, user=SPG_USER,
                            password=SPG_PASS, dbname=SPG_DB, sslmode="require")

# Connect
mconn = mc()
sconn = sc()
sconn.autocommit = True
mcur = mconn.cursor()
scur = sconn.cursor()

# Discover user schemas dynamically from the SPG catalog — no hardcoded names.
# Excludes Postgres/Snowflake system schemas by convention.
_SPG_SCHEMA_FILTER = (
    "table_schema NOT IN ('pg_catalog','information_schema','pg_toast','cron','public')"
    " AND table_schema NOT LIKE 'pg~_%' ESCAPE '~'"
    " AND table_schema NOT LIKE 'snowflake~_%' ESCAPE '~'"
    " AND table_schema NOT LIKE '__pg~_%' ESCAPE '~'"
    " AND table_schema NOT LIKE '__lake~_%' ESCAPE '~'"
    " AND table_schema NOT LIKE 'extension~_%' ESCAPE '~'"
)

# Get all user tables in SPG
scur.execute(f"""
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_type='BASE TABLE' AND {_SPG_SCHEMA_FILTER}
""")
spg_set = {(r[0], r[1]) for r in scur.fetchall()}
print(f"SPG user tables: {len(spg_set)} in schemas: {sorted({s for s,_ in spg_set})}")

# Get identity and bool columns for all user tables
scur.execute(f"""
    SELECT table_schema, table_name, column_name, data_type, identity_generation
    FROM information_schema.columns
    WHERE {_SPG_SCHEMA_FILTER}
""")
spg_col_info = {}
for schema, tname, colname, dtype, iden in scur.fetchall():
    key = (schema, tname)
    if key not in spg_col_info:
        spg_col_info[key] = {"identity": set(), "bool": set(), "cols": []}
    if iden == 'ALWAYS':
        spg_col_info[key]["identity"].add(colname)
    if dtype == 'boolean':
        spg_col_info[key]["bool"].add(colname)
    spg_col_info[key]["cols"].append(colname)

# Drop FK constraints from all user schemas (dynamic)
scur.execute(f"""
    SELECT tc.table_schema, tc.table_name, tc.constraint_name
    FROM information_schema.table_constraints tc
    WHERE tc.constraint_type='FOREIGN KEY' AND {_SPG_SCHEMA_FILTER}
""")
fks = scur.fetchall()
fk_dropped = 0
for schema, tname, cname in fks:
    try:
        scur.execute(f'ALTER TABLE {schema}."{tname}" DROP CONSTRAINT IF EXISTS "{cname}"')
        fk_dropped += 1
    except Exception as e:
        sconn.rollback()
        sconn.autocommit = True
print(f"Dropped {fk_dropped} FK constraints")

# Truncate all tables
print("Truncating tables...")
trunc_done = 0
for schema, tname in sorted(spg_set):
    try:
        scur.execute(f'TRUNCATE TABLE {schema}."{tname}" CASCADE')
        trunc_done += 1
    except Exception as e:
        print(f"  Trunc fail {schema}.{tname}: {e}")

print(f"Truncated {trunc_done} tables")

def convert_val(v, colname, bool_cols):
    if v is None:
        return None
    if colname in bool_cols:
        return bool(int(v)) if isinstance(v, (int, float)) else bool(v)
    if isinstance(v, (bytes, bytearray)):
        return v
    if isinstance(v, uuid.UUID):
        return v
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except (ValueError, AttributeError):
            pass
    return v

mssql_tables = [
    (o["schema"], o["name"])
    for o in inv["objects"]
    if o["type"] == "TABLE"
]

rows_loaded = {}
skipped = []
count_fails = []
total = 0

print(f"\nLoading {len(mssql_tables)} tables...")
for i, (schema, name) in enumerate(mssql_tables):
    name_lc = name.lower()
    if (schema, name_lc) not in spg_set:
        skipped.append(f"{schema}.{name}")
        continue

    spg_key = (schema, name_lc)
    info = spg_col_info.get(spg_key, {"identity": set(), "bool": set(), "cols": []})
    bool_cols = info["bool"]
    identity_cols = info["identity"]
    spg_cols = info["cols"]

    # Fetch source rows — syntax varies by DB type
    try:
        if SOURCE_TYPE == 'mssql':
            mcur.execute(f"SELECT TOP 5000 * FROM [{schema}].[{name}]")
        elif SOURCE_TYPE in ('mysql', 'mariadb'):
            mcur.execute(f"SELECT * FROM `{schema}`.`{name}` LIMIT 5000")
        elif SOURCE_TYPE == 'oracle':
            mcur.execute(f"SELECT * FROM {schema}.{name} WHERE ROWNUM <= 5000")
        else:
            mcur.execute(f"SELECT * FROM \"{schema}\".\"{name}\" LIMIT 5000")
        rows = mcur.fetchall()
        src_count = len(rows)
    except Exception as e:
        skipped.append(f"{schema}.{name} (fetch: {e})")
        continue

    if src_count == 0:
        rows_loaded[f"{schema}.{name}"] = 0
        continue

    src_cols = [d[0] for d in mcur.description]

    # Match src cols to spg cols (case-insensitive)
    spg_cols_lower_map = {c.lower(): c for c in spg_cols}
    matched = []
    for idx, scol in enumerate(src_cols):
        spg_col = spg_cols_lower_map.get(scol.lower())
        if spg_col:
            matched.append((idx, spg_col))

    if not matched:
        skipped.append(f"{schema}.{name} (no col match)")
        continue

    col_indices = [x[0] for x in matched]
    col_names = [x[1] for x in matched]
    has_identity = any(c in identity_cols for c in col_names)

    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    # execute_values appends VALUES %s itself — SQL must end before VALUES
    if has_identity:
        insert_sql = f'INSERT INTO {schema}."{name_lc}" ({quoted_cols}) OVERRIDING SYSTEM VALUE VALUES %s'
    else:
        insert_sql = f'INSERT INTO {schema}."{name_lc}" ({quoted_cols}) VALUES %s'
    row_template = "(" + ", ".join(["%s"] * len(col_names)) + ")"

    # Batch insert using execute_values — orders of magnitude faster than row-by-row
    batch = [
        tuple(
            convert_val(row[idx], col_names[j].lower(), bool_cols)
            for j, idx in enumerate(col_indices)
        )
        for row in rows
    ]
    loaded = 0
    try:
        psycopg2.extras.execute_values(
            scur, insert_sql, batch,
            template=row_template,
            page_size=500,
        )
        loaded = len(batch)
    except Exception as e:
        sconn.rollback()
        sconn.autocommit = True
        print(f"  FAIL {schema}.{name}: {str(e)[:120]}")

    rows_loaded[f"{schema}.{name}"] = loaded
    total += loaded

    if (i+1) % 50 == 0:
        print(f"  Progress: {i+1}/{len(mssql_tables)} tables, {total:,} rows so far")

print(f"\n{'='*60}")
print(f"Tables loaded: {len(rows_loaded)}")
print(f"Total rows: {total:,}")
print(f"Skipped: {len(skipped)}")
if skipped[:5]:
    for s in skipped[:5]:
        print(f"  {s}")

summary = {
    "workload": SOURCE_DB,
    "spg_target": SPG_HOST,
    "tables_loaded": len(rows_loaded),
    "tables_skipped": len(skipped),
    "total_rows": total,
    "skipped_tables": skipped,
    "count_fails": count_fails,
    "rows_per_table": rows_loaded,
    "completed_at": datetime.utcnow().isoformat() + "Z"
}
(SHARED_DIR / "load_summary.json").write_text(json.dumps(summary, indent=2))
(SHARED_DIR / "load_manifest.json").write_text(json.dumps({
    "workload": SOURCE_DB, "source_host": f"{SOURCE_HOST}:{SOURCE_PORT}", "source_db": SOURCE_DB,
    "spg_host": SPG_HOST, "spg_db": SPG_DB,
    "tables_loaded": len(rows_loaded), "total_rows": total,
    "fks_dropped": fk_dropped,
    "completed_at": datetime.utcnow().isoformat() + "Z"
}, indent=2))
print("load_summary.json and load_manifest.json written")
mconn.close()
sconn.close()
