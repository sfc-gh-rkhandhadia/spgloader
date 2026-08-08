"""MSSQL connector — extracts schema objects from SQL Server.

Prefers pymssql (pure Python, no ODBC driver required).
Falls back to pyodbc with ODBC Driver 18 for SQL Server if pymssql is unavailable.

Two extraction modes:
  extract()          — DDL-text extraction (legacy, used when no live catalog is available)
  catalog_extract()  — Catalog-based extraction (accurate FKs, indexes, sequences, IDENTITY)
"""
from __future__ import annotations

from .base import Connector, make_object, _extract_deps_from_sql


class MSSQLConnector(Connector):
    def _connect(self):
        try:
            import pymssql
            return pymssql.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                timeout=30,
                as_dict=False,
            )
        except ImportError:
            import pyodbc
            conn_str = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={self.host},{self.port};"
                f"DATABASE={self.database};"
                f"UID={self.user};PWD={self.password};"
                f"TrustServerCertificate=yes"
            )
            return pyodbc.connect(conn_str, timeout=30)

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            import sys
            print(f"MSSQL connection failed: {e}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # catalog_extract — reads sys.* directly, returns a normalized schema
    # model suitable for pg_generator.py
    # ------------------------------------------------------------------

    def catalog_extract(self) -> dict:
        """Return a structured schema model from the MSSQL system catalog.

        Returns a dict with keys:
          schemas      — list of schema name strings
          tables       — list of TableDef dicts
          foreign_keys — list of ForeignKeyDef dicts
          indexes      — list of IndexDef dicts
          sequences    — list of SequenceDef dicts
        """
        conn = self._connect()
        cur = conn.cursor()

        result = {
            "schemas":      _mssql_schemas(cur),
            "tables":       _mssql_tables(cur),
            "foreign_keys": _mssql_foreign_keys(cur),
            "indexes":      _mssql_indexes(cur),
            "sequences":    _mssql_sequences(cur),
        }

        conn.close()
        return result

    # ------------------------------------------------------------------
    # extract — DDL-text extraction (legacy path, used when catalog
    # is unavailable or for non-table objects)
    # ------------------------------------------------------------------

    def extract(self) -> list[dict]:
        conn = self._connect()
        objects = []
        cur = conn.cursor()

        # Tables
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        tables = cur.fetchall()
        table_names = [r[1] for r in tables]
        for schema, name in tables:
            ddl = _mssql_table_ddl(conn, schema, name)
            objects.append(make_object("table", schema, name, ddl, []))

        # Views
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION
            FROM INFORMATION_SCHEMA.VIEWS
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        for schema, name, defn in cur.fetchall():
            deps = _extract_deps_from_sql(defn or "", tables=table_names)
            objects.append(make_object("view", schema, name, defn or "", deps))

        # Stored procedures
        # Use sys.sql_modules for reliable full-body retrieval (INFORMATION_SCHEMA
        # truncates bodies longer than 4000 chars in some SQL Server versions).
        cur.execute("""
            SELECT s.name AS schema_name, o.name AS obj_name,
                   m.definition
            FROM sys.sql_modules m
            JOIN sys.objects o ON o.object_id = m.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.type = 'P' AND o.is_ms_shipped = 0
            ORDER BY s.name, o.name
        """)
        for schema, name, defn in cur.fetchall():
            deps = _extract_deps_from_sql(defn or "", tables=table_names)
            objects.append(make_object("procedure", schema, name, defn or "", deps))

        # Functions
        cur.execute("""
            SELECT s.name, o.name, m.definition
            FROM sys.sql_modules m
            JOIN sys.objects o ON o.object_id = m.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.type IN ('FN','IF','TF') AND o.is_ms_shipped = 0
            ORDER BY s.name, o.name
        """)
        for schema, name, defn in cur.fetchall():
            deps = _extract_deps_from_sql(defn or "", tables=table_names)
            objects.append(make_object("function", schema, name, defn or "", deps))

        # Triggers
        cur.execute("""
            SELECT s.name, t.name, OBJECT_NAME(t.parent_id), OBJECT_DEFINITION(t.object_id)
            FROM sys.triggers t
            JOIN sys.objects o ON t.parent_id = o.object_id
            JOIN sys.schemas s ON o.schema_id = s.schema_id
            WHERE t.type = 'TR'
            ORDER BY s.name, t.name
        """)
        for schema, name, parent, defn in cur.fetchall():
            objects.append(make_object("trigger", schema, name, defn or "", [parent] if parent else []))

        conn.close()
        return objects

    def extract_bit_columns(self) -> dict[str, list[str]]:
        """Return {schema.table: [col_name, ...]} for all BIT-type columns.

        Queries sys.columns with tp.name = 'bit' to get the definitive source
        of truth for which columns will become boolean in PostgreSQL.
        Used by fix_views.py to convert `alias.col = 0/1` correctly.
        """
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                s.name  AS schema_name,
                t.name  AS table_name,
                c.name  AS col_name
            FROM sys.columns c
            JOIN sys.types   tp ON tp.user_type_id = c.user_type_id
            JOIN sys.tables  t  ON t.object_id = c.object_id
            JOIN sys.schemas s  ON s.schema_id = t.schema_id
            WHERE tp.name = 'bit'
              AND t.is_ms_shipped = 0
            ORDER BY s.name, t.name, c.name
        """)
        result: dict[str, list[str]] = {}
        for schema, table, col in cur.fetchall():
            key = f"{schema.lower()}.{table.lower()}"
            result.setdefault(key, []).append(col.lower())
        conn.close()
        return result

def _mssql_schemas(cur) -> list[str]:
    cur.execute("""
        SELECT s.name
        FROM sys.schemas s
        JOIN sys.database_principals p ON p.principal_id = s.principal_id
        WHERE s.name NOT IN (
            'sys','INFORMATION_SCHEMA','db_owner','db_accessadmin',
            'db_securityadmin','db_ddladmin','db_backupoperator',
            'db_datareader','db_datawriter','db_denydatareader',
            'db_denydatawriter','guest'
        )
        ORDER BY s.name
    """)
    return [r[0] for r in cur.fetchall()]


def _mssql_tables(cur) -> list[dict]:
    """Return one TableDef dict per user table, including columns with catalog metadata."""
    # Columns with identity, computed, and default information.
    # seed_value/increment_value live in sys.identity_columns, not sys.columns.
    cur.execute("""
        SELECT
            SCHEMA_NAME(tab.schema_id)  AS schema_name,
            tab.name                    AS table_name,
            c.name                      AS col_name,
            tp.name                     AS type_name,
            CASE WHEN tp.is_user_defined = 1
                 THEN (SELECT base.name FROM sys.types base WHERE base.user_type_id = tp.system_type_id)
                 ELSE tp.name
            END                         AS base_type_name,
            c.max_length,
            c.precision,
            c.scale,
            c.is_nullable,
            c.is_identity,
            c.is_computed,
            ic.seed_value,
            ic.increment_value,
            dc.definition               AS default_expr,
            cc.definition               AS computed_expr
        FROM sys.columns c
        JOIN sys.objects tab ON tab.object_id = c.object_id
        JOIN sys.types tp ON tp.user_type_id = c.user_type_id
        LEFT JOIN sys.identity_columns ic
            ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc
            ON dc.parent_column_id = c.column_id
            AND dc.parent_object_id = c.object_id
        LEFT JOIN sys.computed_columns cc
            ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        WHERE tab.type = 'U' AND tab.is_ms_shipped = 0
        ORDER BY SCHEMA_NAME(tab.schema_id), tab.name, c.column_id
    """)
    rows = cur.fetchall()

    # Group by (schema, table)
    tables: dict[tuple, dict] = {}
    for (schema, table, col, type_name, base_type_name, max_len, prec, scale,
         is_nullable, is_identity, is_computed, seed, increment,
         default_expr, computed_expr) in rows:
        key = (schema, table)
        if key not in tables:
            tables[key] = {
                "schema": schema,
                "name": table,
                "columns": [],
                "primary_key": [],
            }
        tables[key]["columns"].append({
            "name":          col,
            "type_name":     base_type_name if base_type_name else type_name,
            "max_length":    max_len,
            "precision":     prec,
            "scale":         scale,
            "is_nullable":   bool(is_nullable),
            "is_identity":   bool(is_identity),
            "is_computed":   bool(is_computed),
            "computed_expr": computed_expr,   # None for regular columns
            "seed":          seed,
            "increment":     increment,
            "default_expr":  default_expr,
        })

    # Primary keys
    cur.execute("""
        SELECT
            SCHEMA_NAME(tab.schema_id) AS schema_name,
            tab.name                   AS table_name,
            c.name                     AS col_name
        FROM sys.indexes i
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = i.object_id AND c.column_id = ic.column_id
        JOIN sys.objects tab ON tab.object_id = i.object_id
        WHERE i.is_primary_key = 1 AND tab.is_ms_shipped = 0
        ORDER BY SCHEMA_NAME(tab.schema_id), tab.name, ic.key_ordinal
    """)
    for schema, table, col in cur.fetchall():
        key = (schema, table)
        if key in tables:
            tables[key]["primary_key"].append(col)

    return list(tables.values())


def _mssql_foreign_keys(cur) -> list[dict]:
    """Return one ForeignKeyDef dict per FK constraint column."""
    cur.execute("""
        SELECT
            fk.name                             AS fk_name,
            SCHEMA_NAME(tp.schema_id)           AS from_schema,
            tp.name                             AS from_table,
            fc.name                             AS from_col,
            SCHEMA_NAME(tr.schema_id)           AS to_schema,
            tr.name                             AS to_table,
            rc.name                             AS to_col,
            fk.delete_referential_action_desc   AS on_delete,
            fk.update_referential_action_desc   AS on_update,
            fkc.constraint_column_id            AS col_ordinal
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc
            ON fkc.constraint_object_id = fk.object_id
        JOIN sys.objects tp ON tp.object_id = fk.parent_object_id
        JOIN sys.objects tr ON tr.object_id = fk.referenced_object_id
        JOIN sys.columns fc
            ON fc.object_id = fk.parent_object_id
            AND fc.column_id = fkc.parent_column_id
        JOIN sys.columns rc
            ON rc.object_id = fk.referenced_object_id
            AND rc.column_id = fkc.referenced_column_id
        WHERE fk.is_ms_shipped = 0
        ORDER BY fk.name, fkc.constraint_column_id
    """)
    # Group multi-column FKs
    fks: dict[str, dict] = {}
    for (fk_name, from_schema, from_table, from_col,
         to_schema, to_table, to_col, on_delete, on_update, _ordinal) in cur.fetchall():
        if fk_name not in fks:
            fks[fk_name] = {
                "name": fk_name,
                "from_schema": from_schema,
                "from_table":  from_table,
                "from_cols":   [],
                "to_schema":   to_schema,
                "to_table":    to_table,
                "to_cols":     [],
                "on_delete":   _ref_action(on_delete),
                "on_update":   _ref_action(on_update),
            }
        fks[fk_name]["from_cols"].append(from_col)
        fks[fk_name]["to_cols"].append(to_col)
    return list(fks.values())


def _mssql_indexes(cur) -> list[dict]:
    """Return one IndexDef dict per non-PK index."""
    cur.execute("""
        SELECT
            SCHEMA_NAME(o.schema_id)    AS schema_name,
            o.name                      AS table_name,
            i.name                      AS index_name,
            i.is_unique,
            i.filter_definition,
            c.name                      AS col_name,
            ic.is_descending_key,
            ic.is_included_column,
            ic.key_ordinal
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id
        JOIN sys.index_columns ic
            ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c
            ON c.object_id = i.object_id AND c.column_id = ic.column_id
        WHERE i.is_primary_key = 0
          AND i.type > 0
          AND o.is_ms_shipped = 0
          AND o.type = 'U'
        ORDER BY SCHEMA_NAME(o.schema_id), o.name, i.name, ic.key_ordinal, ic.index_column_id
    """)
    idxs: dict[tuple, dict] = {}
    for (schema, table, idx_name, is_unique, filter_def,
         col_name, is_desc, is_included, _ordinal) in cur.fetchall():
        key = (schema, table, idx_name)
        if key not in idxs:
            idxs[key] = {
                "schema":      schema,
                "table_name":  table,
                "name":        idx_name,
                "is_unique":   bool(is_unique),
                "predicate":   filter_def,
                "columns":     [],
                "include_cols": [],
            }
        if is_included:
            idxs[key]["include_cols"].append(col_name)
        else:
            idxs[key]["columns"].append(
                f"{col_name} DESC" if is_desc else col_name
            )
    return list(idxs.values())


def _mssql_sequences(cur) -> list[dict]:
    cur.execute("""
        SELECT
            SCHEMA_NAME(schema_id)  AS schema_name,
            name,
            CAST(start_value AS BIGINT)   AS start_value,
            CAST(increment AS BIGINT)     AS increment,
            CAST(minimum_value AS BIGINT) AS minimum_value,
            CAST(maximum_value AS BIGINT) AS maximum_value,
            is_cycling
        FROM sys.sequences
        ORDER BY SCHEMA_NAME(schema_id), name
    """)
    return [
        {
            "schema":    r[0],
            "name":      r[1],
            "start":     r[2],
            "increment": r[3],
            "min_value": r[4],
            "max_value": r[5],
            "is_cycling": bool(r[6]),
        }
        for r in cur.fetchall()
    ]


def _ref_action(action: str | None) -> str:
    """Normalise MSSQL referential action desc to SQL keyword."""
    mapping = {
        "NO_ACTION":   "NO ACTION",
        "CASCADE":     "CASCADE",
        "SET_NULL":    "SET NULL",
        "SET_DEFAULT": "SET DEFAULT",
    }
    return mapping.get((action or "").upper(), "NO ACTION")


# ---------------------------------------------------------------------------
# DDL-text helpers (legacy path)
# ---------------------------------------------------------------------------

def _mssql_table_ddl(conn, schema: str, name: str) -> str:
    cur = conn.cursor()
    cur.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """, (schema, name))
    col_defs = []
    for col_name, dtype, max_len, num_prec, num_scale, nullable, default in cur.fetchall():
        type_str = dtype.upper()
        if max_len and max_len > 0:
            type_str += f"({max_len})"
        elif max_len == -1:
            type_str += "(MAX)"
        elif num_prec and dtype.lower() in ("decimal", "numeric"):
            type_str += f"({num_prec},{num_scale})" if num_scale else f"({num_prec})"
        null_str = "NULL" if nullable == "YES" else "NOT NULL"
        default_str = f" DEFAULT {default}" if default else ""
        col_defs.append(f"    [{col_name}] {type_str}{default_str} {null_str}")

    cur.execute("""
        SELECT kc.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc
          ON tc.CONSTRAINT_NAME = kc.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = kc.TABLE_SCHEMA
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
        ORDER BY kc.ORDINAL_POSITION
    """, (schema, name))
    pk_cols = [r[0] for r in cur.fetchall()]
    if pk_cols:
        col_defs.append(f"    PRIMARY KEY ({', '.join(f'[{c}]' for c in pk_cols)})")

    return f"CREATE TABLE [{schema}].[{name}] (\n" + ",\n".join(col_defs) + "\n);"
