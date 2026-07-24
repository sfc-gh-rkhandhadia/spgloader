"""Oracle connector — extracts schema objects from Oracle via python-oracledb."""
from __future__ import annotations

import sys
from .base import Connector, make_object, _extract_deps_from_sql


class OracleConnector(Connector):
    def _connect(self):
        import oracledb
        return oracledb.connect(
            user=self.user,
            password=self.password,
            dsn=f"{self.host}:{self.port}/{self.database}",
        )

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            conn.cursor().execute("SELECT 1 FROM DUAL")
            conn.close()
            return True
        except Exception as e:
            print(f"Oracle connection failed: {e}", file=sys.stderr)
            return False

    def extract(self) -> list[dict]:
        conn = self._connect()
        cur = conn.cursor()
        objects = []
        schema = self.user.upper()

        cur.execute("SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = :s ORDER BY TABLE_NAME",
                    s=schema)
        table_names = [r[0] for r in cur.fetchall()]
        for tname in table_names:
            objects.append(make_object("table", schema, tname,
                                       _oracle_table_ddl(cur, schema, tname), []))

        cur.execute("SELECT VIEW_NAME, TEXT FROM ALL_VIEWS WHERE OWNER = :s ORDER BY VIEW_NAME",
                    s=schema)
        for vname, text in cur.fetchall():
            ddl = f"CREATE OR REPLACE VIEW {schema}.{vname} AS\n{text}"
            objects.append(make_object("view", schema, vname, ddl,
                                       _extract_deps_from_sql(text or "", table_names)))

        cur.execute("""
            SELECT OBJECT_NAME, OBJECT_TYPE FROM ALL_OBJECTS
            WHERE OWNER = :s AND OBJECT_TYPE IN ('PROCEDURE','FUNCTION')
            ORDER BY OBJECT_NAME
        """, s=schema)
        for pname, ptype in cur.fetchall():
            cur.execute("""
                SELECT TEXT FROM ALL_SOURCE
                WHERE OWNER = :s AND NAME = :n AND TYPE = :t ORDER BY LINE
            """, s=schema, n=pname, t=ptype)
            src = "".join(r[0] for r in cur.fetchall())
            ddl = f"CREATE OR REPLACE {src}"
            obj_type = "procedure" if ptype == "PROCEDURE" else "function"
            objects.append(make_object(obj_type, schema, pname, ddl,
                                       _extract_deps_from_sql(src, table_names)))

        cur.execute("""
            SELECT TRIGGER_NAME, TABLE_NAME, TRIGGER_BODY FROM ALL_TRIGGERS
            WHERE OWNER = :s ORDER BY TRIGGER_NAME
        """, s=schema)
        for tname, parent_table, body in cur.fetchall():
            ddl = f"CREATE OR REPLACE TRIGGER {schema}.{tname}\n{body}"
            objects.append(make_object("trigger", schema, tname, ddl,
                                       [parent_table] if parent_table else []))

        conn.close()
        return objects


def _oracle_table_ddl(cur, schema: str, name: str) -> str:
    cur.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, DATA_DEFAULT
        FROM ALL_TAB_COLUMNS WHERE OWNER = :s AND TABLE_NAME = :n ORDER BY COLUMN_ID
    """, s=schema, n=name)
    col_defs = []
    for col_name, dtype, length, prec, scale, nullable, default in cur.fetchall():
        type_str = dtype
        if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"):
            type_str += f"({length})"
        elif dtype == "NUMBER" and prec:
            type_str += f"({prec},{scale or 0})"
        null_str = "" if nullable == "Y" else " NOT NULL"
        default_str = f" DEFAULT {default.strip()}" if default else ""
        col_defs.append(f"    {col_name} {type_str}{default_str}{null_str}")
    return f"CREATE TABLE {schema}.{name} (\n" + ",\n".join(col_defs) + "\n);"
