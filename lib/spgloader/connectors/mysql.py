"""MySQL / MariaDB connector — extracts schema objects.

Two extraction modes:
  extract()          — DDL-text extraction (SHOW CREATE TABLE, legacy path)
  catalog_extract()  — Catalog-based extraction via INFORMATION_SCHEMA
                       (accurate FKs, indexes, AUTO_INCREMENT detection)
"""
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

    # ------------------------------------------------------------------
    # catalog_extract
    # ------------------------------------------------------------------

    def catalog_extract(self) -> dict:
        """Return a normalized schema model from INFORMATION_SCHEMA."""
        conn = self._connect()
        cur = conn.cursor()
        db = self.database

        result = {
            "schemas":      [db],   # MySQL: one DB = one schema
            "tables":       _mysql_tables(cur, db),
            "foreign_keys": _mysql_foreign_keys(cur, db),
            "indexes":      _mysql_indexes(cur, db),
            "sequences":    [],     # MySQL has no native CREATE SEQUENCE
        }

        conn.close()
        return result

    # ------------------------------------------------------------------
    # extract — legacy DDL-text path
    # ------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Catalog query helpers
# ---------------------------------------------------------------------------

def _mysql_tables(cur, db: str) -> list[dict]:
    """Return TableDef dicts from INFORMATION_SCHEMA.COLUMNS."""
    cur.execute("""
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE,
               CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, COLUMN_DEFAULT, EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """, (db,))
    rows = cur.fetchall()

    tables: dict[str, dict] = {}
    for (table, col, dtype, col_type, max_len, prec, scale,
         is_nullable, default, extra) in rows:
        if table not in tables:
            tables[table] = {
                "schema": db,
                "name":   table,
                "columns": [],
                "primary_key": [],
            }
        # tinyint(1) is conventionally boolean in MySQL/MariaDB
        effective_type = dtype
        if dtype.lower() == "tinyint" and "(1)" in (col_type or ""):
            effective_type = "tinyint_bool"  # pg_generator maps this to boolean

        tables[table]["columns"].append({
            "name":           col,
            "type_name":      effective_type,
            "col_type":       col_type,   # preserves enum/set values
            "max_length":     max_len,
            "precision":      prec,
            "scale":          scale,
            "is_nullable":    is_nullable == "YES",
            "is_identity":    "auto_increment" in (extra or "").lower(),
            "seed":           1,
            "increment":      1,
            "default_expr":   default,
        })

    # Primary keys
    cur.execute("""
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """, (db,))
    for table, col in cur.fetchall():
        if table in tables:
            tables[table]["primary_key"].append(col)

    return list(tables.values())


def _mysql_foreign_keys(cur, db: str) -> list[dict]:
    cur.execute("""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.TABLE_NAME       AS from_table,
            kcu.COLUMN_NAME      AS from_col,
            kcu.REFERENCED_TABLE_NAME   AS to_table,
            kcu.REFERENCED_COLUMN_NAME  AS to_col,
            rc.DELETE_RULE,
            rc.UPDATE_RULE,
            kcu.ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
          ON rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
         AND rc.CONSTRAINT_SCHEMA = kcu.TABLE_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
    """, (db,))

    fks: dict[str, dict] = {}
    for (fk_name, from_table, from_col, to_table, to_col,
         on_delete, on_update, _ordinal) in cur.fetchall():
        if fk_name not in fks:
            fks[fk_name] = {
                "name":        fk_name,
                "from_schema": db,
                "from_table":  from_table,
                "from_cols":   [],
                "to_schema":   db,
                "to_table":    to_table,
                "to_cols":     [],
                "on_delete":   on_delete or "NO ACTION",
                "on_update":   on_update or "NO ACTION",
            }
        fks[fk_name]["from_cols"].append(from_col)
        fks[fk_name]["to_cols"].append(to_col)
    return list(fks.values())


def _mysql_indexes(cur, db: str) -> list[dict]:
    cur.execute("""
        SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE,
               COLUMN_NAME, SEQ_IN_INDEX, COLLATION
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = %s AND INDEX_NAME != 'PRIMARY'
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
    """, (db,))

    idxs: dict[tuple, dict] = {}
    for (table, idx_name, non_unique, col, _seq, collation) in cur.fetchall():
        key = (table, idx_name)
        if key not in idxs:
            idxs[key] = {
                "schema":      db,
                "table_name":  table,
                "name":        idx_name,
                "is_unique":   non_unique == 0,
                "predicate":   None,
                "columns":     [],
                "include_cols": [],
            }
        col_entry = f"{col} DESC" if collation == "D" else col
        idxs[key]["columns"].append(col_entry)
    return list(idxs.values())
