#!/usr/bin/env python3
"""source_adapter.py — Unified catalog + execution adapter for MSSQL, MySQL, MariaDB.

Provides SourceAdapter: a thin dialect-aware wrapper that lets parity scripts call
one API regardless of the source DB type.  All dialect branches are contained here;
callers never import pymssql or mysql.connector directly.

Usage:
    from source_adapter import SourceAdapter, open_source_conn

    conn = open_source_conn(source_type, host, port, user, password, database)
    adapter = SourceAdapter(source_type, conn)

    routines = adapter.list_routines("my_schema")
    params   = adapter.get_routine_params("my_schema", "my_proc")
    rows     = adapter.call_proc("my_schema", "my_proc", [1, "foo"])
    view_rows= adapter.query_view("my_schema", "my_view", limit=100)
"""

from __future__ import annotations

import os
import sys
from typing import Any


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def open_source_conn(source_type: str, host: str, port: int,
                     user: str, password: str, database: str):
    """Open and return a raw DB-API 2.0 connection to the source database."""
    st = source_type.lower()
    if st in ("mysql", "mariadb"):
        try:
            import mysql.connector  # type: ignore
        except ImportError:
            sys.exit("mysql-connector-python not installed. Run: uv add mysql-connector-python")
        return mysql.connector.connect(
            host=host, port=port, user=user, password=password,
            database=database, connection_timeout=30,
        )
    elif st == "mssql":
        try:
            import pymssql  # type: ignore
        except ImportError:
            sys.exit("pymssql not installed. Run: uv add pymssql")
        return pymssql.connect(
            server=host, port=port, user=user, password=password,
            database=database, login_timeout=30,
        )
    else:
        raise ValueError(f"Unsupported source_type: {st!r}. Supported: mssql, mysql, mariadb")


# ---------------------------------------------------------------------------
# SourceAdapter
# ---------------------------------------------------------------------------

class SourceAdapter:
    """Dialect-aware catalog + execution adapter.

    All methods return plain Python lists / dicts so callers stay agnostic.
    """

    def __init__(self, source_type: str, conn):
        self._type = source_type.lower()
        self._conn = conn

    # ------------------------------------------------------------------
    # Catalog queries
    # ------------------------------------------------------------------

    def list_schemas(self) -> list[str]:
        """Return user-defined schema names (databases for MySQL)."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT SCHEMA_NAME FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME NOT IN
                  ('information_schema','mysql','performance_schema','sys')
                ORDER BY SCHEMA_NAME
            """)
        else:  # mssql
            cur.execute("""
                SELECT name FROM sys.schemas
                WHERE name NOT IN ('sys','guest','INFORMATION_SCHEMA',
                  'db_owner','db_accessadmin','db_securityadmin',
                  'db_ddladmin','db_backupoperator','db_datareader',
                  'db_datawriter','db_denydatareader','db_denydatawriter')
                ORDER BY name
            """)
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows

    def list_tables(self, schema: str) -> list[str]:
        """Return base table names in schema."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT TABLE_NAME FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
            """, (schema,))
        else:
            cur.execute("""
                SELECT t.name FROM sys.tables t
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE s.name = %s ORDER BY t.name
            """, (schema,))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows

    def list_routines(self, schema: str) -> list[dict]:
        """Return [{name, routine_type, param_count}] for all routines in schema."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT r.ROUTINE_NAME, r.ROUTINE_TYPE,
                       COUNT(p.ORDINAL_POSITION) AS param_count
                FROM information_schema.ROUTINES r
                LEFT JOIN information_schema.PARAMETERS p
                  ON p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA
                 AND p.SPECIFIC_NAME   = r.ROUTINE_NAME
                 AND p.PARAMETER_MODE IS NOT NULL
                WHERE r.ROUTINE_SCHEMA = %s
                GROUP BY r.ROUTINE_NAME, r.ROUTINE_TYPE
                ORDER BY r.ROUTINE_NAME
            """, (schema,))
        else:
            cur.execute("""
                SELECT o.name, o.type_desc,
                       COUNT(p.object_id) AS param_count
                FROM sys.objects o
                LEFT JOIN sys.parameters p ON p.object_id = o.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s
                  AND o.type IN ('P','FN','TF','IF')
                GROUP BY o.name, o.type_desc
                ORDER BY o.name
            """, (schema,))
        rows = [
            {"name": r[0], "routine_type": r[1], "param_count": int(r[2] or 0)}
            for r in cur.fetchall()
        ]
        cur.close()
        return rows

    def list_views(self, schema: str) -> list[str]:
        """Return view names in schema."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT TABLE_NAME FROM information_schema.VIEWS
                WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME
            """, (schema,))
        else:
            cur.execute("""
                SELECT v.name FROM sys.views v
                JOIN sys.schemas s ON s.schema_id = v.schema_id
                WHERE s.name = %s ORDER BY v.name
            """, (schema,))
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows

    def get_routine_params(self, schema: str, name: str) -> list[dict]:
        """Return [{name, ordinal, data_type, mode}] for a routine's parameters."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT PARAMETER_NAME, ORDINAL_POSITION, DATA_TYPE,
                       PARAMETER_MODE
                FROM information_schema.PARAMETERS
                WHERE SPECIFIC_SCHEMA = %s AND SPECIFIC_NAME = %s
                  AND PARAMETER_MODE IS NOT NULL
                ORDER BY ORDINAL_POSITION
            """, (schema, name))
        else:
            cur.execute("""
                SELECT p.name, p.parameter_id, t.name, ''
                FROM sys.parameters p
                JOIN sys.types t ON t.user_type_id = p.user_type_id
                JOIN sys.objects o ON o.object_id = p.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.name = %s
                  AND p.is_output = 0
                ORDER BY p.parameter_id
            """, (schema, name))
        rows = [
            {"name": r[0], "ordinal": r[1], "data_type": r[2], "mode": r[3]}
            for r in cur.fetchall()
        ]
        cur.close()
        return rows

    # ------------------------------------------------------------------
    # Row count helpers
    # ------------------------------------------------------------------

    def table_row_count(self, schema: str, table: str) -> int:
        """Return the exact row count for a table."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute(f"SELECT COUNT(*) FROM `{schema}`.`{table}`")
        else:
            cur.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]")
        count = cur.fetchone()[0]
        cur.close()
        return int(count or 0)

    def view_row_count(self, schema: str, view: str, limit: int = 100) -> int:
        """Return the row count (up to limit) for a view — safe for large views."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM `{schema}`.`{view}` LIMIT %s) _sub",
                (limit,)
            )
        else:
            cur.execute(
                f"SELECT COUNT(*) FROM (SELECT TOP {limit} 1 FROM [{schema}].[{view}]) _sub"
            )
        count = cur.fetchone()[0]
        cur.close()
        return int(count or 0)

    def query_view(self, schema: str, view: str, limit: int = 100) -> list[dict]:
        """Return up to `limit` rows from a view as a list of dicts."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            if hasattr(cur, "fetchall"):
                import mysql.connector
                cur2 = self._conn.cursor(dictionary=True)
                cur2.execute(f"SELECT * FROM `{schema}`.`{view}` LIMIT %s", (limit,))
                rows = cur2.fetchall()
                cur2.close()
                return rows
        else:
            cur.execute(f"SELECT TOP {limit} * FROM [{schema}].[{view}]")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows
        cur.close()
        return []

    # ------------------------------------------------------------------
    # Column count (for structural parity)
    # ------------------------------------------------------------------

    def column_count(self, schema: str, table: str) -> int:
        """Return the number of columns in a table from the catalog."""
        cur = self._conn.cursor()
        if self._type in ("mysql", "mariadb"):
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """, (schema, table))
        else:
            cur.execute("""
                SELECT COUNT(*) FROM sys.columns c
                JOIN sys.objects o ON o.object_id = c.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.name = %s
            """, (schema, table))
        count = cur.fetchone()[0]
        cur.close()
        return int(count or 0)
