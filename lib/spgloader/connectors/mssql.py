"""MSSQL connector — extracts schema objects from SQL Server.

Prefers pymssql (pure Python, no ODBC driver required).
Falls back to pyodbc with ODBC Driver 18 for SQL Server if pymssql is unavailable.
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
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'PROCEDURE'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
        """)
        for schema, name, defn in cur.fetchall():
            deps = _extract_deps_from_sql(defn or "", tables=table_names)
            objects.append(make_object("procedure", schema, name, defn or "", deps))

        # Functions
        cur.execute("""
            SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_DEFINITION
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_TYPE = 'FUNCTION'
            ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME
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
