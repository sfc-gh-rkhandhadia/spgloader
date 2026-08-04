"""
source_adapter.py — Multi-database source abstraction for execution-parity scripts.

Routes on SOURCE_TYPE env var: mssql | mysql | mariadb | oracle.
Provides a uniform interface for connection, schema/routine discovery,
parameter introspection, routine body retrieval, and row sampling.

MSSQL   → pymssql  +  sys.objects / sys.parameters / sys.sql_modules
MySQL   → mysql-connector-python  +  INFORMATION_SCHEMA
MariaDB → mysql-connector-python  +  INFORMATION_SCHEMA (same as MySQL)
Oracle  → cx_Oracle  +  ALL_PROCEDURES / ALL_ARGUMENTS / DBMS_METADATA
"""
from __future__ import annotations

import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# System schema exclusion sets per source type
# ---------------------------------------------------------------------------
_MSSQL_SYSTEM_SCHEMAS = {"sys", "information_schema"}
_MYSQL_SYSTEM_SCHEMAS = {"information_schema", "performance_schema", "mysql", "sys"}
_ORACLE_SYSTEM_SCHEMAS = {
    "sys", "system", "outln", "dbsnmp", "appqossys", "dbsfwuser",
    "ggsys", "ctxsys", "xdb", "wmsys", "gsmadmin_internal", "gsmuser",
    "dvsys", "lbacsys", "ojvmsys", "dvf", "sysbackup", "sysrac",
    "remote_scheduler_agent", "audsys",
}


# ---------------------------------------------------------------------------
# SourceAdapter
# ---------------------------------------------------------------------------
class SourceAdapter:
    """Uniform source-DB interface for execution-parity validation scripts.

    Usage:
        adapter = build_adapter()
        conn    = adapter.connect()
        schemas = adapter.get_schemas()
        routines = adapter.get_routines("dbo")
        params   = adapter.get_parameters("dbo", "my_proc")
        body     = adapter.get_routine_body("dbo", "my_proc")
        row      = adapter.sample_row("dbo", "my_table", ["id", "name"])
    """

    def __init__(self, source_type: str, conf: dict):
        self.source_type = source_type.lower()
        self.conf = conf

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self):
        """Return an open connection to the source database."""
        if self.source_type == "mssql":
            import pymssql
            return pymssql.connect(
                server=self.conf["host"],
                port=self.conf["port"],
                user=self.conf["user"],
                password=self.conf["password"],
                database=self.conf["database"],
                timeout=self.conf.get("timeout", 30),
            )
        elif self.source_type in ("mysql", "mariadb"):
            import mysql.connector
            return mysql.connector.connect(
                host=self.conf["host"],
                port=self.conf["port"],
                user=self.conf["user"],
                password=self.conf["password"],
                database=self.conf["database"],
                connection_timeout=self.conf.get("timeout", 30),
            )
        elif self.source_type == "oracle":
            import oracledb
            host = self.conf["host"]
            port = self.conf["port"]
            svc  = self.conf.get("service_name") or self.conf["database"]
            dsn  = f"{host}:{port}/{svc}"
            return oracledb.connect(
                user=self.conf["user"],
                password=self.conf["password"],
                dsn=dsn,
            )
        else:
            raise ValueError(f"Unsupported SOURCE_TYPE: {self.source_type!r}")

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------
    def get_schemas(self) -> list[str]:
        """Return non-system schemas that contain stored procedures or functions."""
        conn = self.connect()
        try:
            if self.source_type == "mssql":
                cur = conn.cursor()
                cur.execute("""
                    SELECT DISTINCT s.name
                    FROM sys.objects o
                    JOIN sys.schemas s ON o.schema_id = s.schema_id
                    WHERE o.type IN ('P', 'FN', 'IF', 'TF')
                    ORDER BY s.name
                """)
                return [r[0] for r in cur.fetchall()
                        if r[0].lower() not in _MSSQL_SYSTEM_SCHEMAS]
            elif self.source_type in ("mysql", "mariadb"):
                cur = conn.cursor()
                cur.execute("""
                    SELECT DISTINCT ROUTINE_SCHEMA
                    FROM INFORMATION_SCHEMA.ROUTINES
                    WHERE ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')
                    ORDER BY ROUTINE_SCHEMA
                """)
                return [r[0] for r in cur.fetchall()
                        if r[0].lower() not in _MYSQL_SYSTEM_SCHEMAS]
            elif self.source_type == "oracle":
                cur = conn.cursor()
                cur.execute("""
                    SELECT DISTINCT OWNER FROM ALL_PROCEDURES
                    WHERE OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION')
                    ORDER BY OWNER
                """)
                return [r[0] for r in cur.fetchall()
                        if r[0].lower() not in _ORACLE_SYSTEM_SCHEMAS]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Routine discovery
    # ------------------------------------------------------------------
    def get_routines(self, schema: str) -> list[dict]:
        """Return [{name, type, type_desc}] for procs + functions in schema."""
        conn = self.connect()
        try:
            if self.source_type == "mssql":
                cur = conn.cursor(as_dict=True)
                cur.execute("""
                    SELECT o.name AS proc_name, o.type AS obj_type, o.type_desc AS obj_type_desc
                    FROM sys.objects o
                    JOIN sys.schemas s ON o.schema_id = s.schema_id
                    WHERE s.name = %s AND o.type IN ('P', 'FN', 'IF', 'TF')
                    ORDER BY o.name
                """, (schema,))
                return [{"name": r["proc_name"],
                         "type": r["obj_type"].strip(),
                         "type_desc": r["obj_type_desc"]} for r in cur.fetchall()]
            elif self.source_type in ("mysql", "mariadb"):
                cur = conn.cursor()
                cur.execute("""
                    SELECT ROUTINE_NAME, ROUTINE_TYPE
                    FROM INFORMATION_SCHEMA.ROUTINES
                    WHERE ROUTINE_SCHEMA = %s
                    ORDER BY ROUTINE_NAME
                """, (schema,))
                return [{"name": r[0],
                         "type": "P" if r[1] == "PROCEDURE" else "FN",
                         "type_desc": r[1]} for r in cur.fetchall()]
            elif self.source_type == "oracle":
                cur = conn.cursor()
                cur.execute("""
                    SELECT OBJECT_NAME, OBJECT_TYPE
                    FROM ALL_PROCEDURES
                    WHERE OWNER = :owner
                      AND OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION')
                    ORDER BY OBJECT_NAME
                """, {"owner": schema.upper()})
                return [{"name": r[0],
                         "type": "P" if r[1] == "PROCEDURE" else "FN",
                         "type_desc": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Parameter introspection
    # ------------------------------------------------------------------
    def get_parameters(self, schema: str, routine_name: str) -> list[dict]:
        """Return [{name, type_name, is_output, parameter_id}], ordered by position."""
        conn = self.connect()
        try:
            if self.source_type == "mssql":
                cur = conn.cursor(as_dict=True)
                cur.execute("""
                    SELECT p.parameter_id, p.name AS param_name,
                           t.name AS type_name, p.is_output
                    FROM sys.objects o
                    JOIN sys.schemas s ON o.schema_id = s.schema_id
                    JOIN sys.parameters p ON o.object_id = p.object_id
                    JOIN sys.types t ON p.user_type_id = t.user_type_id
                    WHERE o.type IN ('P', 'FN', 'TF', 'IF')
                      AND s.name = %s AND LOWER(o.name) = %s
                      AND p.parameter_id > 0
                    ORDER BY p.parameter_id
                """, (schema, routine_name.lower()))
                return [{"parameter_id": r["parameter_id"],
                         "name": r["param_name"].lstrip("@"),
                         "type_name": r["type_name"],
                         "is_output": bool(r["is_output"])} for r in cur.fetchall()]
            elif self.source_type in ("mysql", "mariadb"):
                cur = conn.cursor()
                cur.execute("""
                    SELECT ORDINAL_POSITION, PARAMETER_NAME,
                           DATA_TYPE, PARAMETER_MODE
                    FROM INFORMATION_SCHEMA.PARAMETERS
                    WHERE SPECIFIC_SCHEMA = %s
                      AND LOWER(SPECIFIC_NAME) = %s
                      AND PARAMETER_MODE IS NOT NULL
                    ORDER BY ORDINAL_POSITION
                """, (schema, routine_name.lower()))
                return [{"parameter_id": r[0],
                         "name": (r[1] or "").lstrip("@").lower(),
                         "type_name": r[2],
                         "is_output": r[3] in ("OUT", "INOUT")} for r in cur.fetchall()]
            elif self.source_type == "oracle":
                cur = conn.cursor()
                cur.execute("""
                    SELECT POSITION, ARGUMENT_NAME,
                           DATA_TYPE, IN_OUT
                    FROM ALL_ARGUMENTS
                    WHERE OWNER = :owner
                      AND LOWER(OBJECT_NAME) = :name
                      AND ARGUMENT_NAME IS NOT NULL
                    ORDER BY POSITION
                """, {"owner": schema.upper(), "name": routine_name.lower()})
                return [{"parameter_id": r[0],
                         "name": (r[1] or "").lower(),
                         "type_name": r[2],
                         "is_output": r[3] in ("OUT", "IN/OUT")} for r in cur.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Routine body (source SQL definition)
    # ------------------------------------------------------------------
    def get_routine_body(self, schema: str, routine_name: str) -> str | None:
        """Return the source SQL definition text, or None if not available."""
        conn = self.connect()
        try:
            if self.source_type == "mssql":
                cur = conn.cursor(as_dict=True)
                cur.execute("""
                    SELECT sm.definition
                    FROM sys.objects o
                    JOIN sys.schemas s ON o.schema_id = s.schema_id
                    JOIN sys.sql_modules sm ON o.object_id = sm.object_id
                    WHERE o.type IN ('P', 'FN', 'TF', 'IF')
                      AND s.name = %s AND LOWER(o.name) = %s
                """, (schema, routine_name.lower()))
                row = cur.fetchone()
                return row["definition"] if row else None
            elif self.source_type in ("mysql", "mariadb"):
                cur = conn.cursor()
                cur.execute("""
                    SELECT ROUTINE_DEFINITION
                    FROM INFORMATION_SCHEMA.ROUTINES
                    WHERE ROUTINE_SCHEMA = %s
                      AND LOWER(ROUTINE_NAME) = %s
                """, (schema, routine_name.lower()))
                row = cur.fetchone()
                return row[0] if row else None
            elif self.source_type == "oracle":
                # oracledb DBMS_METADATA returns LOB — read as string
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT DBMS_METADATA.GET_DDL('PROCEDURE', :name, :owner) FROM DUAL",
                        {"name": routine_name.upper(), "owner": schema.upper()}
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0].read() if hasattr(row[0], "read") else str(row[0])
                except Exception:
                    pass
                # Fallback: try FUNCTION
                try:
                    cur.execute(
                        "SELECT DBMS_METADATA.GET_DDL('FUNCTION', :name, :owner) FROM DUAL",
                        {"name": routine_name.upper(), "owner": schema.upper()}
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0].read() if hasattr(row[0], "read") else str(row[0])
                except Exception:
                    pass
                return None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Row sampling (for smart parameter discovery)
    # ------------------------------------------------------------------
    def sample_row(self, schema: str, table: str, columns: list[str]) -> dict[str, Any] | None:
        """Return one real row {col: value} from schema.table, or None."""
        conn = self.connect()
        try:
            col_list = ", ".join(f'"{c}"' for c in columns) if columns else "*"
            if self.source_type == "mssql":
                cur = conn.cursor(as_dict=True)
                cur.execute(f"SELECT TOP 1 {col_list} FROM [{schema}].[{table}]")
                return cur.fetchone()
            elif self.source_type in ("mysql", "mariadb"):
                cur = conn.cursor(dictionary=True)
                cur.execute(f"SELECT {col_list} FROM `{schema}`.`{table}` LIMIT 1")
                return cur.fetchone()
            elif self.source_type == "oracle":
                cur = conn.cursor()
                cur.execute(
                    f"SELECT {col_list} FROM {schema}.{table} WHERE ROWNUM = 1"
                )
                row = cur.fetchone()
                if row and cur.description:
                    return {d[0].lower(): v for d, v in zip(cur.description, row)}
                return None
        except Exception:
            return None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # System schema check
    # ------------------------------------------------------------------
    def is_system_schema(self, name: str) -> bool:
        if self.source_type == "mssql":
            return name.lower() in _MSSQL_SYSTEM_SCHEMAS
        elif self.source_type in ("mysql", "mariadb"):
            return name.lower() in _MYSQL_SYSTEM_SCHEMAS
        elif self.source_type == "oracle":
            return name.lower() in _ORACLE_SYSTEM_SCHEMAS
        return False


# ---------------------------------------------------------------------------
# Factory — reads SOURCE_TYPE + connection config from environment
# ---------------------------------------------------------------------------
def build_adapter() -> SourceAdapter:
    """Build a SourceAdapter from env vars.

    Reads SOURCE_TYPE (default: mssql).
    Connection values: SOURCE_HOST / SOURCE_USER / SOURCE_PASSWORD / SOURCE_DATABASE
    MSSQL_HOST / MSSQL_USER / MSSQL_PASSWORD / MSSQL_DATABASE accepted as aliases
    when SOURCE_TYPE=mssql (backward compat with existing scripts).
    """
    source_type = os.environ.get("SOURCE_TYPE", "mssql").lower()

    def _get(primary_key: str, *fallbacks: str, default: str = "") -> str:
        for key in (primary_key, *fallbacks):
            v = os.environ.get(key, "")
            if v:
                return v
        return default

    if source_type == "mssql":
        conf = dict(
            host=_get("SOURCE_HOST", "MSSQL_HOST"),
            port=int(_get("SOURCE_PORT", "MSSQL_PORT", default="1433")),
            user=_get("SOURCE_USER", "MSSQL_USER"),
            password=_get("SOURCE_PASSWORD", "MSSQL_PASSWORD"),
            database=_get("SOURCE_DATABASE", "MSSQL_DATABASE"),
            timeout=int(_get("SOURCE_TIMEOUT", "MSSQL_TIMEOUT", default="30")),
        )
    elif source_type in ("mysql", "mariadb"):
        conf = dict(
            host=_get("SOURCE_HOST", "MYSQL_HOST"),
            port=int(_get("SOURCE_PORT", "MYSQL_PORT", default="3306")),
            user=_get("SOURCE_USER", "MYSQL_USER"),
            password=_get("SOURCE_PASSWORD", "MYSQL_PASSWORD"),
            database=_get("SOURCE_DATABASE", "MYSQL_DATABASE"),
            timeout=int(_get("SOURCE_TIMEOUT", default="30")),
        )
    elif source_type == "oracle":
        conf = dict(
            host=_get("SOURCE_HOST", "ORACLE_HOST"),
            port=int(_get("SOURCE_PORT", "ORACLE_PORT", default="1521")),
            user=_get("SOURCE_USER", "ORACLE_USER"),
            password=_get("SOURCE_PASSWORD", "ORACLE_PASSWORD"),
            database=_get("SOURCE_DATABASE", "ORACLE_DATABASE", "ORACLE_SERVICE"),
            service_name=_get("ORACLE_SERVICE", "SOURCE_DATABASE"),
            timeout=int(_get("SOURCE_TIMEOUT", default="30")),
        )
    else:
        raise ValueError(
            f"Unsupported SOURCE_TYPE={source_type!r}. "
            "Supported: mssql | mysql | mariadb | oracle"
        )

    return SourceAdapter(source_type, conf)


def open_source_conn(source_type: str, host: str, port, user: str,
                     password: str, database: str, **extra) -> dict:
    """Build a source-connection conf dict from explicit parameters.

    Returns a dict (not a live connection) so callers can hand it to
    ``SourceAdapter(source_type, conf)`` which opens its own connection
    via ``connect()``. This mirrors the conf shape produced by ``build_adapter``.
    """
    source_type = (source_type or "mssql").lower()
    conf = {
        "host": host,
        "port": int(port) if port else (3306 if source_type in ("mysql", "mariadb") else 1433),
        "user": user,
        "password": password or "",
        "database": database,
        "timeout": int(extra.get("timeout", 30)),
    }
    if source_type == "oracle":
        conf["service_name"] = extra.get("service_name", database)
    return conf
