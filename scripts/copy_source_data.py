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
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

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
    # UUID (uniqueidentifier) → string for psycopg2
    if isinstance(val, UUID):
        return str(val)
    # hierarchyid .ToString() returns '/1/2/3/' — convert to LTREE format '1.2.3'
    if isinstance(val, str) and val.startswith('/') and val.endswith('/'):
        ltree_val = val.strip('/').replace('/', '.')
        return ltree_val if ltree_val else None
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


def _convert_row_mysql(
    row: tuple,
    col_names: list[str],
    bool_cols: set[str],
    geometry_cols: set[str] = frozenset(),
    bytea_cols: set[str] = frozenset(),
) -> tuple:
    """Like _convert_row but handles MySQL-specific column types:
    - TINYINT(1) 0/1 → Python bool for boolean columns
    - GEOMETRY/POINT (MySQL WKB bytes) → WKT string for PostGIS columns
    - BLOB/TINYBLOB (binary bytes) → hex bytea string
    """
    result = []
    for val, col in zip(row, col_names):
        if val is None:
            result.append(None)
        elif col in bool_cols and isinstance(val, int):
            result.append(bool(val))
        elif col in geometry_cols and isinstance(val, (bytes, bytearray)):
            # MySQL stores geometry as 4-byte SRID prefix + WKB; strip prefix and convert
            result.append(_mysql_geometry_to_wkt(val))
        elif col in bytea_cols and isinstance(val, (bytes, bytearray)):
            # Binary BLOB — keep as bytes; the COPY path will hex-encode it
            result.append(bytes(val))
        else:
            result.append(_pg_value(val))
    return tuple(result)


def _mysql_geometry_to_wkt(wkb_with_srid: bytes) -> "str | bytes | None":
    """Convert MySQL geometry bytes (4-byte SRID + WKB) to WKT string.

    Returns:
      str  — WKT string when shapely is available (e.g. 'POINT (1.0 2.0)')
      bytes — raw WKB when shapely is not available (COPY path hex-encodes for PostGIS)
      None  — on any error (column stored as NULL rather than crashing the copy)
    """
    try:
        # MySQL prepends a 4-byte little-endian SRID to the standard WKB
        raw_wkb = wkb_with_srid[4:] if len(wkb_with_srid) > 4 else wkb_with_srid
        # Try shapely first (fast, no external process)
        try:
            from shapely.wkb import loads as wkb_loads
            geom = wkb_loads(raw_wkb)
            return geom.wkt  # PostGIS accepts WKT strings directly
        except ImportError:
            pass
        # Fallback: return raw WKB bytes — the COPY path will hex-encode as \x<hex>
        # which PostGIS can accept as bytea-encoded WKB
        return raw_wkb
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-table copy
# ---------------------------------------------------------------------------

def _copy_table(
    env: dict,
    spg_service: str,
    schema: str,
    table: str,
    batch_size: int,
    truncate_first: bool,
    source_type: str,
) -> dict:
    """Copy one table from source to SPG.

    Opens its own source AND SPG connections (thread-safe: one connection per worker).
    Returns a result dict: {table, rows_copied, error, elapsed_s}
    """
    import psycopg2

    # Schema/table casing: MSSQL is case-insensitive, PG uses lowercase
    # Bracket-quote MSSQL identifiers to handle spaces/reserved words in names
    if source_type == "mssql":
        src_fqn = f"[{schema}].[{table}]"
    else:
        src_fqn = f"{schema}.{table}"
    pg_schema = schema.lower()
    pg_table = table.lower()
    pg_fqn = f'"{pg_schema}"."{pg_table}"'

    t0 = time.time()
    pg_conn = None
    source_conn = None

    try:
        source_conn, _ = _connect_source(env)
        pg_conn = psycopg2.connect(f"service={spg_service}")
        pg_conn.autocommit = False
        # Disable FK checks for this session (seed data may arrive out of FK order)
        with pg_conn.cursor() as pg_cur:
            pg_cur.execute("SET session_replication_role = 'replica'")

        # Truncate target if requested (CASCADE to handle FK constraints)
        if truncate_first:
            with pg_conn.cursor() as pg_cur:
                pg_cur.execute(f"TRUNCATE TABLE {pg_fqn} CASCADE")
            pg_conn.commit()

        # Query SPG for identity columns, boolean columns, and generated columns
        with pg_conn.cursor() as meta_cur:
            meta_cur.execute("""
                SELECT column_name,
                       data_type,
                       column_default,
                       is_identity,
                       is_generated
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (pg_schema, pg_table))
            spg_meta = {row[0]: {"data_type": row[1], "default": row[2], "is_identity": row[3], "is_generated": row[4]} for row in meta_cur.fetchall()}
        identity_cols = {c for c, m in spg_meta.items() if m["is_identity"] == "YES"}
        bool_cols = {c for c, m in spg_meta.items() if m["data_type"] == "boolean"}
        generated_cols = {c for c, m in spg_meta.items() if m["is_generated"] == "ALWAYS"}
        # MySQL geometry columns → WKT; MySQL BLOB columns → hex bytea
        geometry_cols = {c for c, m in spg_meta.items() if m["data_type"] in ("geometry", "geography")}
        bytea_cols = {c for c, m in spg_meta.items() if m["data_type"] == "bytea"}

        # Fetch column names from source (use buffered cursor for MySQL to avoid "Unread result" errors)
        if source_type == "mssql":
            src_cur = source_conn.cursor()
            src_cur.execute(f"SELECT TOP 0 * FROM {src_fqn}")
            all_col_names = [d[0].lower() for d in src_cur.description]
            src_cur.close()
        else:
            src_cur = source_conn.cursor(buffered=True)
            src_cur.execute(f"SELECT * FROM {src_fqn} LIMIT 0")
            all_col_names = [d[0].lower() for d in src_cur.description]
            src_cur.close()

        # Exclude generated columns from the column list
        col_names = [c for c in all_col_names if c not in generated_cols]
        col_indices = [i for i, c in enumerate(all_col_names) if c not in generated_cols]
        # Detect ltree columns (hierarchyid mapped to ltree — need .ToString() in source)
        ltree_cols = {c for c, m in spg_meta.items() if m["data_type"] == "USER-DEFINED" or "ltree" in m.get("data_type", "")}
        # Also check udt_name for ltree (information_schema may report USER-DEFINED)
        if not ltree_cols:
            with pg_conn.cursor() as ltree_cur:
                ltree_cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND udt_name = 'ltree'
                """, (pg_schema, pg_table))
                ltree_cols = {row[0] for row in ltree_cur.fetchall()}

        cols_sql = ", ".join(f'"{c}"' for c in col_names)
        # For COPY with identity columns, temporarily set to BY DEFAULT
        has_identity = bool(identity_cols and identity_cols.intersection(col_names))
        if has_identity:
            for id_col in identity_cols.intersection(col_names):
                with pg_conn.cursor() as pg_cur:
                    pg_cur.execute(f'ALTER TABLE {pg_fqn} ALTER COLUMN "{id_col}" SET GENERATED BY DEFAULT')
            pg_conn.commit()

        # Build source SELECT with only non-generated columns
        # Use .ToString() for hierarchyid/ltree columns (MSSQL returns binary otherwise)
        if source_type == "mssql":
            src_col_exprs = []
            for i in col_indices:
                col = all_col_names[i]
                if col in ltree_cols:
                    src_col_exprs.append(f"[{col}].ToString() AS [{col}]")
                else:
                    src_col_exprs.append(f"[{col}]")
            src_cols = ", ".join(src_col_exprs)
            select_sql = f"SELECT {src_cols} FROM {src_fqn}"
        else:
            src_cols = ", ".join(f"`{all_col_names[i]}`" for i in col_indices)
            select_sql = f"SELECT {src_cols} FROM {src_fqn}"

        # Stream data in batches using COPY protocol (much faster than executemany)
        copy_sql = f"COPY {pg_fqn} ({cols_sql}) FROM STDIN WITH (FORMAT csv, NULL '\\N', DELIMITER E'\\t')"
        src_cur = source_conn.cursor()
        src_cur.execute(select_sql)

        total_rows = 0
        while True:
            batch = src_cur.fetchmany(batch_size)
            if not batch:
                break
            converted = [
                _convert_row_mysql(row, col_names, bool_cols, geometry_cols, bytea_cols)
                if source_type != "mssql" else _convert_row(row)
                for row in batch
            ]
            # Build TSV buffer for COPY
            buf = io.StringIO()
            for row in converted:
                fields = []
                for val in row:
                    if val is None:
                        fields.append('\\N')
                    elif isinstance(val, bool):
                        fields.append('t' if val else 'f')
                    elif isinstance(val, (bytes, bytearray)):
                        # Hex-encode binary data for COPY
                        fields.append('\\\\x' + val.hex())
                    else:
                        # Escape tabs and newlines in string values
                        s = str(val).replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')
                        fields.append(s)
                buf.write('\t'.join(fields) + '\n')
            buf.seek(0)
            # Deadlock retry: up to 3 attempts with exponential back-off
            for _attempt in range(3):
                try:
                    buf.seek(0)
                    with pg_conn.cursor() as pg_cur:
                        pg_cur.copy_expert(copy_sql, buf)
                    pg_conn.commit()
                    break
                except Exception as _e:
                    import psycopg2.errors as _pgerr
                    if isinstance(_e, _pgerr.DeadlockDetected) and _attempt < 2:
                        pg_conn.rollback()
                        import time as _t; _t.sleep(0.5 * (2 ** _attempt))
                    else:
                        raise
            total_rows += len(batch)

        src_cur.close()
        source_conn.close()
        # Restore identity columns to ALWAYS
        if has_identity:
            for id_col in identity_cols.intersection(col_names):
                with pg_conn.cursor() as pg_cur:
                    pg_cur.execute(f'ALTER TABLE {pg_fqn} ALTER COLUMN "{id_col}" SET GENERATED ALWAYS')
            pg_conn.commit()
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
        if source_conn:
            try: source_conn.close()
            except Exception: pass
        if pg_conn:
            pg_conn.close()


# ---------------------------------------------------------------------------
# CSV-direct data loading (alternative to live-source streaming)
# ---------------------------------------------------------------------------

def _csv_weighted_order(work_dir: Path) -> list[tuple[str, str]]:
    """Return (schema, table) list ordered by FK dependency (parents first).

    Uses dep_graph.json ordered_objects (topo sorted) when present; otherwise
    falls back to the table list from ddl_objects.json.
    """
    dep_path = work_dir / "dep_graph.json"
    if dep_path.exists():
        try:
            dep = json.loads(dep_path.read_text())
            ordered = dep.get("ordered_objects") or []
            return [
                (o.get("schema", "dbo").lower(), o.get("name", "").lower())
                for o in ordered
                if o.get("type", "").lower() == "table" and o.get("name")
            ]
        except Exception:
            pass
    return []


def _table_order_from_ddl(work_dir: Path) -> list[tuple[str, str]]:
    """Table list from ddl_objects.json (used when no dep graph)."""
    ddl_path = work_dir / "ddl_objects.json"
    if not ddl_path.exists():
        raise FileNotFoundError(
            f"ddl_objects.json not found in {work_dir}. Run extract_ddl.py first."
        )
    objs = json.loads(ddl_path.read_text())
    if isinstance(objs, dict):
        objs = objs.get("objects", [])
    return [
        (o.get("schema", "dbo").lower(), o["name"].lower())
        for o in objs
        if o.get("type", "").lower() in ("table",)
    ]


def _pg_type_info(pg_conn, schema: str, table: str) -> dict:
    """Fetch per-column metadata from SPG keyed by column name."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, column_default, is_identity,
                   is_generated, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        rows = cur.fetchall()
    meta = {}
    for name, dtype, default, is_id, is_gen, udt in rows:
        meta[name] = {
            "data_type": dtype,
            "is_identity": is_id == "YES",
            "is_generated": is_gen == "ALWAYS",
            "udt_name": udt,
        }
    return meta


def _parse_csv_field(value: str, col_type: str, udt_name: str):
    """Convert a raw CSV (TAB-delimited) field to a value for psycopg2 COPY.

    MSSQL exports are TAB-delimited with no header; empty fields mean empty
    string ('') for text but NULL for typed columns.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
    from spgloader.geometry import mssql_spatial_to_wkt_or_null

    v = value.strip()
    geo_udts = (udt_name or "").lower()

    if geo_udts in ("geography", "geometry"):
        if v == "":
            return None
        return mssql_spatial_to_wkt_or_null(v)

    if v == "":
        if col_type in ("text", "character varying", "character", "citext"):
            return ""
        return None

    # bytea (MSSQL varbinary/image) — the CSV stores the blob as a hex string.
    if col_type in ("bytea", "binary") and (udt_name or "").lower() == "bytea":
        try:
            return bytes.fromhex(v)
        except ValueError:
            # Not hex — let COPY raise a clear error rather than guess.
            return v

    if geo_udts == "ltree" or (v.startswith("/") and v.endswith("/")
                               and "/" in v[1:-1]):
        ltree_val = v.strip("/").replace("/", ".")
        return ltree_val if ltree_val else None

    if col_type == "boolean":
        return v.lower() in ("1", "true", "t", "yes")

    return v


def _csv_field_sep(csv_file: Path) -> "tuple[str, str]":
    """Auto-detect the (field_term, row_term) for an MSSQL CSV export.

    AdventureWorks-style exports are inconsistent: most files are
    TAB-delimited, but some use SSMS's '+|' field terminator with a '&|'
    record terminator (which is why BULK INSERT with '\t' failed on them).
    Detection samples enough bytes to cover large first records (binary
    blobs) that push the record terminator far from the file start.
    """
    field_term = "\t"
    row_term = "\n"
    with open(csv_file, "rb") as fh:
        sample = fh.read(1 << 20)  # up to 1 MB
    # SSMS pipe format: '+|' separates fields and '&|' ends a record.
    if b"&|" in sample and b"+|" in sample.split(b"&|")[0]:
        return "+|", "&|"
    # Large single-record files: the '+' terminator appears but '&|' may sit
    # beyond the sample window.  Fall back on the '+' marker heuristically.
    if b"+|" in sample and b"&|" not in sample and b"|" in sample:
        return "+|", "&|"
    return field_term, row_term


def _probe_csv_width(csv_file: Path, field_term: str, row_term: str,
                     max_width: int) -> int:
    """Return the number of CSV fields in a complete record.

    Pipe files: width of the first record.  Tab files: the maximum field
    count across all physical lines — a fully-intact record line reveals
    the true width even when a text column (e.g. comments) spans newlines.
    """
    if row_term == "&|":
        for fields in _iter_csv_records(csv_file, field_term, row_term,
                                        max_width):
            return len(fields)
        return 0
    # Tab: max fields over all physical lines.
    best = 0
    with open(csv_file, encoding="utf-8-sig", newline="") as fh:
        for raw in fh:
            line = raw.strip("\r\n")
            if not line:
                continue
            n = len(line.split("\t"))
            if n > best:
                best = n
    return best


def _iter_csv_records(csv_file: Path, field_term: str, row_term: str,
                      expected_cols: int):
    """Yield one field-list per logical record, handling embedded newlines.

    Pipe files: records are terminated by the '&|' marker (not by newline),
    so XML/text fields that contain newlines are kept whole.
    Tab files: a physical newline ends a record normally, but a text field
    may itself contain a newline.  We rebuild records by stitching lines
    until the accumulated field count reaches ``expected_cols``.
    """
    if row_term == "&|":
        content = csv_file.read_text(encoding="utf-8-sig")
        # Normalize CRLF to LF inside records for stable field splitting.
        for rec in content.split("&|"):
            rec = rec.strip("\r\n").strip()
            if not rec:
                continue
            if rec.endswith("+|"):
                rec = rec[:-2]
            yield rec.split("+|")
        return

    # Tab-delimited: records start with an integer first column.  Split raw
    # text on a newline immediately followed by <digits><tab> — the start of
    # the next record.  A continuation of a multi-line text field (e.g. a
    # comment line like "3-day ride") does not match '<digits><tab>' since the
    # digits are not followed by a tab, so embedded newlines stay inside the
    # field.  Each yielded record has exactly expected_cols fields.
    import re as _re
    rec_pat = _re.compile(r"\n(?=[0-9]+\t)")
    text = csv_file.read_text(encoding="utf-8-sig")
    for piece in rec_pat.split(text):
        piece = piece.lstrip("\r\n")
        if not piece or not piece[0].isdigit():
            continue
        yield piece.rstrip("\r\n").split("\t")


def _copy_table_from_csv(
    spg_service: str,
    schema: str,
    table: str,
    csv_file: Path,
    batch_size: int,
    disable_fk: bool = False,
) -> dict:
    """Load one CSV file into a single SPG table.

    Identity values are preserved via GENERATED BY DEFAULT during the load
    and restored to ALWAYS afterwards.  The CSV delimiter is auto-detected
    (TAB or SSMS '+|' / '&|') and records may contain embedded newlines.
    """
    import psycopg2

    pg_fqn = f'"{schema}"."{table}"'
    t0 = time.time()
    pg_conn = None
    try:
        pg_conn = psycopg2.connect(f"service={spg_service}")
        pg_conn.autocommit = False

        if disable_fk:
            with pg_conn.cursor() as cur:
                cur.execute("SET session_replication_role = 'replica'")

        meta = _pg_type_info(pg_conn, schema, table)
        col_order = list(meta.keys())
        copy_cols = [c for c in col_order if not meta[c]["is_generated"]]
        if not copy_cols:
            return {"table": f"{schema}.{table}", "rows_copied": 0,
                    "error": None, "elapsed_s": round(time.time() - t0, 2),
                    "fk_forced": disable_fk}

        identity_cols = [c for c in copy_cols if meta[c]["is_identity"]]
        if identity_cols:
            for id_col in identity_cols:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        f'ALTER TABLE {pg_fqn} ALTER COLUMN "{id_col}" '
                        f"SET GENERATED BY DEFAULT"
                    )
            pg_conn.commit()

        cols_sql = ", ".join(f'"{c}"' for c in copy_cols)
        copy_sql = (f"COPY {pg_fqn} ({cols_sql}) FROM STDIN "
                    f"WITH (FORMAT csv, NULL '\\N', DELIMITER E'\\t')")

        field_term, row_term = _csv_field_sep(csv_file)

        # The CSV may omit trailing audit columns (e.g. ModifiedDate) that the
        # target provides with a DEFAULT.  Detect the true CSV width and trim
        # the COPY column list so omitted trailing columns fire their default.
        csv_width = _probe_csv_width(csv_file, field_term, row_term,
                                     len(copy_cols))
        if 0 < csv_width < len(copy_cols):
            copy_cols = copy_cols[:csv_width]
            identity_cols = [c for c in copy_cols if meta[c]["is_identity"]]
            cols_sql = ", ".join(f'"{c}"' for c in copy_cols)
            copy_sql = (f"COPY {pg_fqn} ({cols_sql}) FROM STDIN "
                        f"WITH (FORMAT csv, NULL '\\N', DELIMITER E'\\t')")

        total_rows = 0
        buf = io.StringIO()
        pending = 0
        for fields in _iter_csv_records(csv_file, field_term, row_term,
                                        len(copy_cols)):
            values = []
            for col in copy_cols:
                idx = col_order.index(col)
                raw = fields[idx] if idx < len(fields) else ""
                m = meta[col]
                values.append(_parse_csv_field(raw, m["data_type"],
                                               m["udt_name"]))
            out_fields = []
            for val in values:
                if val is None:
                    out_fields.append("\\N")
                elif isinstance(val, bool):
                    out_fields.append("t" if val else "f")
                elif isinstance(val, (bytes, bytearray)):
                    out_fields.append("\\\\x" + val.hex())
                else:
                    s = str(val).replace("\\", "\\\\").replace("\t", "\\t")
                    s = s.replace("\n", "\\n").replace("\r", "\\r")
                    out_fields.append(s)
            buf.write("\t".join(out_fields) + "\n")
            pending += 1

            if pending >= batch_size:
                buf.seek(0)
                with pg_conn.cursor() as cur:
                    cur.copy_expert(copy_sql, buf)
                pg_conn.commit()
                total_rows += pending
                buf = io.StringIO()
                pending = 0

        if pending:
            buf.seek(0)
            with pg_conn.cursor() as cur:
                cur.copy_expert(copy_sql, buf)
            pg_conn.commit()
            total_rows += pending

        if identity_cols:
            for id_col in identity_cols:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        f'ALTER TABLE {pg_fqn} ALTER COLUMN "{id_col}" '
                        f"SET GENERATED ALWAYS"
                    )
            pg_conn.commit()

        elapsed = time.time() - t0
        status = "OK (FK_FORCED)" if disable_fk else "OK"
        print(f"  {status} {schema}.{table:<42} {total_rows:>8,} rows  {elapsed:.1f}s")
        return {"table": f"{schema}.{table}", "rows_copied": total_rows,
                "error": None, "elapsed_s": round(elapsed, 2),
                "fk_forced": disable_fk}

    except Exception as e:
        if pg_conn:
            pg_conn.rollback()
        err = str(e).replace("\n", " ").strip()
        print(f"  ERR {schema}.{table:<42} {err[:90]}")
        return {"table": f"{schema}.{table}", "rows_copied": 0,
                "error": err, "elapsed_s": round(time.time() - t0, 2),
                "fk_forced": disable_fk}
    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception:
                pass


def _truncate_csv_targets(spg_service: str, ordered: list[tuple[str, str]]) -> None:
    """TRUNCATE all target tables before a CSV load (idempotent rerun)."""
    import psycopg2
    conn = psycopg2.connect(f"service={spg_service}")
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = 'replica'")
            for schema, table in ordered:
                cur.execute(f'TRUNCATE TABLE "{schema}"."{table}"')
            cur.execute("SET session_replication_role = 'origin'")
            print(f"  TRUNCATED {len(ordered)} target table(s)")
    finally:
        conn.close()


def copy_source_data_from_csv(
    work_dir: Path,
    spg_service: str,
    csv_dir: Path,
    tables: list[str] | None = None,
    batch_size: int = 5000,
    truncate_first: bool = False,
) -> dict:
    """Load table data from CSV files into SPG, respecting FK dependency order.

    Strategy (FK handling):
      1. Load in parent->child order (dep_graph.json) with FKs enabled.
      2. On FK violation, record + continue.
      3. Retry the failed set once more.
      4. Last resort: disable FKs (session_replication_role=replica) for any
         table still failing purely on FK.

    Returns {results: [...], summary: {total, copied, failed, total_rows}}.
    """
    if not csv_dir.exists() or not csv_dir.is_dir():
        raise FileNotFoundError(f"--csv-dir path not found: {csv_dir}")

    if tables:
        # Load exactly the requested tables, ordered by FK dependency
        # (parents first) so FK constraints validate on load.
        want = set()
        for t in tables:
            if "." in t:
                sch, tbl = t.split(".", 1)
                want.add((sch.lower(), tbl.lower()))
            else:
                want.add(("dbo", t.lower()))
        ordered = [p for p in _csv_weighted_order(work_dir) if p in want]
        # Preserve requested order for any table not in the dep graph.
        for schema, table in list(want):
            if (schema, table) not in ordered:
                ordered.append((schema, table))
    else:
        ordered = _csv_weighted_order(work_dir) or _table_order_from_ddl(work_dir)

    csv_files = {f.stem.lower(): f for f in csv_dir.glob("*.csv")}

    if truncate_first:
        _truncate_csv_targets(spg_service, ordered)

    results = []
    done_names = set()
    failed_names = set()

    def run_pass(targets, disable):
        for schema, table in targets:
            fqn = f"{schema}.{table}"
            csv_f = csv_files.get(table.lower())
            if csv_f is None:
                print(f"  SKIP {schema}.{table:<42} no CSV file")
                continue
            res = _copy_table_from_csv(spg_service, schema, table, csv_f,
                                       batch_size, disable_fk=disable)
            results.append(res)
            done_names.add(fqn)
            if res["error"]:
                failed_names.add(fqn)
            else:
                failed_names.discard(fqn)

    def _is_fk_error(msg: str) -> bool:
        if not msg:
            return False
        low = msg.lower()
        return "violates foreign key" in low or "foreign key constraint" in low

    def _pending_fk_targets():
        out = []
        for schema, table in ordered:
            fqn = f"{schema}.{table}"
            if fqn in failed_names and _is_fk_error(
                next((r["error"] for r in results
                      if r["table"].lower() == fqn and r["error"]), "")
            ):
                out.append((schema, table))
        return out

    # Pass 1: FK order, FKs ON.
    run_pass(ordered, disable=False)

    # Pass 2: retry any table still failing on FK (parents now loaded).
    if _pending_fk_targets():
        print(f"\nRetry pass: {len(_pending_fk_targets())} table(s) failed on FK — retrying")
        # Remove retried FQNs from failed set so pass 3 only sees still-bad ones.
        run_pass(_pending_fk_targets(), disable=False)

    # Pass 3 (last resort): disable FKs for any table still failing on FK.
    still_fk = _pending_fk_targets()
    if still_fk:
        print(f"\nLast resort: disabling FKs for {len(still_fk)} table(s) (FK_FORCED)")
        run_pass(still_fk, disable=True)

    # Keep the latest result per table (later passes override earlier ones).
    by_name = {}
    for r in results:
        by_name[r["table"].lower()] = r
    results = list(by_name.values())

    copied = sum(1 for r in results if r["error"] is None)
    failed = len(results) - copied
    total_rows = sum(r["rows_copied"] for r in results)
    forced = sum(1 for r in results if r.get("fk_forced"))
    summary = {
        "total": len(results), "copied": copied, "failed": failed,
        "total_rows": total_rows, "fk_forced": forced,
    }
    print(f"\n{'='*60}")
    print(f"Tables copied : {copied}/{len(results)}  (CSV mode)")
    print(f"Total rows    : {total_rows:,}")
    if forced:
        print(f"FK_FORCED     : {forced}")
    if failed:
        print(f"Failed tables : {failed}")
        for r in results:
            if r["error"]:
                print(f"  - {r['table']}: {r['error'][:80]}")
    report_path = work_dir / "copy_data_report.json"
    report_path.write_text(json.dumps({"results": results, "summary": summary,
                                       "mode": "csv"}, indent=2))
    print(f"Report        : {report_path}")
    return {"results": results, "summary": summary}


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
    _, source_type = _connect_source(env)  # validate credentials; workers open their own conns

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
        if isinstance(objs, dict):
            objs = objs.get("objects", [])
        table_list = [
            (o.get("schema", "dbo"), o["name"])
            for o in objs
            if o.get("type") in ("table", "TABLE")
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
            env, spg_service, schema, table,
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
    parser.add_argument(
        "--csv-dir", default=None,
        help="Optional: load from TAB-delimited CSV data files in this "
             "directory instead of streaming from the live source DB. "
             "CSV filename stem = table name (e.g. Address.csv -> person.address). "
             "Tables are loaded parent-first using the FK dependency graph.",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()

    if args.csv_dir:
        copy_source_data_from_csv(
            work_dir=work_dir,
            spg_service=args.spg_service,
            csv_dir=Path(args.csv_dir).expanduser().resolve(),
            tables=args.tables,
            batch_size=args.batch_size,
            truncate_first=args.truncate_first,
        )
    else:
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
