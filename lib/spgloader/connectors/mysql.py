"""MySQL connector — extracts schema objects from MySQL via mysql-connector-python."""
from __future__ import annotations

import sys
from .base import Connector, make_object, _extract_deps_from_sql


class MySQLConnector(Connector):
    def _connect(self):
        import mysql.connector
        return mysql.connector.connect(
            host=self.host, port=self.port, database=self.database,
            user=self.user, password=self.password, connection_timeout=10,
        )

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            print(f"MySQL connection failed: {e}", file=sys.stderr)
            return False

    def extract(self) -> list[dict]:
        conn = self._connect()
        cur = conn.cursor()
        objects = []

        cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        table_names = [r[0] for r in cur.fetchall()]
        for tname in table_names:
            cur.execute(f"SHOW CREATE TABLE `{tname}`")
            row = cur.fetchone()
            objects.append(make_object("table", self.database, tname, row[1] if row else "", []))

        cur.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
        view_names = [r[0] for r in cur.fetchall()]
        for vname in view_names:
            cur.execute(f"SHOW CREATE VIEW `{vname}`")
            row = cur.fetchone()
            ddl = row[1] if row else ""
            objects.append(make_object("view", self.database, vname, ddl,
                                       _extract_deps_from_sql(ddl, table_names)))

        cur.execute("SHOW PROCEDURE STATUS WHERE Db = %s", (self.database,))
        for pname in [r[1] for r in cur.fetchall()]:
            cur.execute(f"SHOW CREATE PROCEDURE `{pname}`")
            row = cur.fetchone()
            ddl = row[2] if row else ""
            objects.append(make_object("procedure", self.database, pname, ddl,
                                       _extract_deps_from_sql(ddl, table_names)))

        cur.execute("SHOW FUNCTION STATUS WHERE Db = %s", (self.database,))
        for fname in [r[1] for r in cur.fetchall()]:
            cur.execute(f"SHOW CREATE FUNCTION `{fname}`")
            row = cur.fetchone()
            ddl = row[2] if row else ""
            objects.append(make_object("function", self.database, fname, ddl,
                                       _extract_deps_from_sql(ddl, table_names)))

        cur.execute("""
            SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_STATEMENT
            FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA = %s
            ORDER BY TRIGGER_NAME
        """, (self.database,))
        for trig_name, event, parent_table, body in cur.fetchall():
            ddl = f"CREATE TRIGGER `{trig_name}` {event} ON `{parent_table}`\nFOR EACH ROW\n{body}"
            objects.append(make_object("trigger", self.database, trig_name, ddl, [parent_table]))

        conn.close()
        return objects
