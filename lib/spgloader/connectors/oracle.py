"""Oracle connector — extracts schema objects from Oracle via python-oracledb.

Two extraction modes:
  extract()          — DDL-text extraction (ALL_SOURCE / ALL_VIEWS, legacy path)
  catalog_extract()  — Catalog-based extraction via ALL_* views
                       (accurate FKs, indexes, sequences, IDENTITY columns)
"""
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

    # ------------------------------------------------------------------
    # catalog_extract
    # ------------------------------------------------------------------

    def catalog_extract(self) -> dict:
        """Return a normalized schema model from Oracle ALL_* catalog views."""
        conn = self._connect()
        cur = conn.cursor()
        schema = self.user.upper()

        result = {
            "schemas":      [schema],
            "tables":       _oracle_tables(cur, schema),
            "foreign_keys": _oracle_foreign_keys(cur, schema),
            "indexes":      _oracle_indexes(cur, schema),
            "sequences":    _oracle_sequences(cur, schema),
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
        schema = self.user.upper()

        cur.execute("SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = :s ORDER BY TABLE_NAME",
                    s=schema)
        table_names = [r[0] for r in cur.fetchall()]
        for tname in table_names:
            objects.append(make_object("table", schema, tname,
                                       _oracle_table_ddl_text(cur, schema, tname), []))

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


# ---------------------------------------------------------------------------
# Catalog query helpers
# ---------------------------------------------------------------------------

def _oracle_tables(cur, schema: str) -> list[dict]:
    """Return TableDef dicts from ALL_TAB_COLUMNS."""
    # Check if IDENTITY_COLUMN column exists (Oracle 12c+)
    cur.execute("""
        SELECT COUNT(*) FROM ALL_TAB_COLUMNS
        WHERE COLUMN_NAME = 'IDENTITY_COLUMN'
          AND TABLE_NAME = 'ALL_TAB_COLUMNS'
          AND OWNER = 'SYS'
    """)
    has_identity = cur.fetchone()[0] > 0

    identity_col = "IDENTITY_COLUMN" if has_identity else "'NO'"
    cur.execute(f"""
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
               DATA_LENGTH, DATA_PRECISION, DATA_SCALE,
               NULLABLE, DATA_DEFAULT, {identity_col} AS IS_IDENTITY
        FROM ALL_TAB_COLUMNS
        WHERE OWNER = :s
        ORDER BY TABLE_NAME, COLUMN_ID
    """, s=schema)
    rows = cur.fetchall()

    tables: dict[str, dict] = {}
    for (table, col, dtype, length, prec, scale,
         nullable, default, is_identity) in rows:
        if table not in tables:
            tables[table] = {
                "schema": schema,
                "name":   table,
                "columns": [],
                "primary_key": [],
            }
        tables[table]["columns"].append({
            "name":        col,
            "type_name":   dtype,
            "max_length":  length,
            "precision":   prec,
            "scale":       scale,
            "is_nullable": nullable == "Y",
            "is_identity": (is_identity or "NO") == "YES",
            "seed":        1,
            "increment":   1,
            "default_expr": default.strip() if default else None,
        })

    # Primary keys
    cur.execute("""
        SELECT cc.TABLE_NAME, cc.COLUMN_NAME
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc
          ON cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME AND cc.OWNER = c.OWNER
        WHERE c.CONSTRAINT_TYPE = 'P' AND c.OWNER = :s
        ORDER BY cc.TABLE_NAME, cc.POSITION
    """, s=schema)
    for table, col in cur.fetchall():
        if table in tables:
            tables[table]["primary_key"].append(col)

    return list(tables.values())


def _oracle_foreign_keys(cur, schema: str) -> list[dict]:
    cur.execute("""
        SELECT
            c.CONSTRAINT_NAME,
            c.TABLE_NAME        AS from_table,
            cc.COLUMN_NAME      AS from_col,
            r.TABLE_NAME        AS to_table,
            rc.COLUMN_NAME      AS to_col,
            c.DELETE_RULE,
            cc.POSITION
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS cc
          ON cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME AND cc.OWNER = c.OWNER
        JOIN ALL_CONSTRAINTS r
          ON r.CONSTRAINT_NAME = c.R_CONSTRAINT_NAME AND r.OWNER = c.R_OWNER
        JOIN ALL_CONS_COLUMNS rc
          ON rc.CONSTRAINT_NAME = r.CONSTRAINT_NAME
         AND rc.OWNER = r.OWNER
         AND rc.POSITION = cc.POSITION
        WHERE c.CONSTRAINT_TYPE = 'R' AND c.OWNER = :s
        ORDER BY c.CONSTRAINT_NAME, cc.POSITION
    """, s=schema)

    fks: dict[str, dict] = {}
    for (fk_name, from_table, from_col, to_table, to_col,
         delete_rule, _pos) in cur.fetchall():
        if fk_name not in fks:
            fks[fk_name] = {
                "name":        fk_name,
                "from_schema": schema,
                "from_table":  from_table,
                "from_cols":   [],
                "to_schema":   schema,
                "to_table":    to_table,
                "to_cols":     [],
                "on_delete":   delete_rule or "NO ACTION",
                "on_update":   "NO ACTION",   # Oracle has no ON UPDATE CASCADE
            }
        fks[fk_name]["from_cols"].append(from_col)
        fks[fk_name]["to_cols"].append(to_col)
    return list(fks.values())


def _oracle_indexes(cur, schema: str) -> list[dict]:
    """Return non-PK, non-unique-constraint indexes."""
    # First get constraint-backed index names to exclude them
    cur.execute("""
        SELECT INDEX_NAME FROM ALL_CONSTRAINTS
        WHERE OWNER = :s AND CONSTRAINT_TYPE IN ('P','U')
    """, s=schema)
    constraint_idxs = {r[0] for r in cur.fetchall()}

    cur.execute("""
        SELECT i.INDEX_NAME, i.TABLE_NAME, i.UNIQUENESS,
               ic.COLUMN_NAME, ic.COLUMN_POSITION, ic.DESCEND
        FROM ALL_INDEXES i
        JOIN ALL_IND_COLUMNS ic
          ON ic.INDEX_NAME = i.INDEX_NAME AND ic.INDEX_OWNER = i.OWNER
        WHERE i.OWNER = :s
          AND i.INDEX_TYPE NOT IN ('LOB', 'DOMAIN')
        ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
    """, s=schema)

    idxs: dict[str, dict] = {}
    for (idx_name, table, uniqueness, col, _pos, descend) in cur.fetchall():
        if idx_name in constraint_idxs:
            continue
        if idx_name not in idxs:
            idxs[idx_name] = {
                "schema":       schema,
                "table_name":   table,
                "name":         idx_name,
                "is_unique":    uniqueness == "UNIQUE",
                "predicate":    None,
                "columns":      [],
                "include_cols": [],
            }
        col_entry = f"{col} DESC" if descend == "DESC" else col
        idxs[idx_name]["columns"].append(col_entry)
    return list(idxs.values())


def _oracle_sequences(cur, schema: str) -> list[dict]:
    cur.execute("""
        SELECT SEQUENCE_NAME, MIN_VALUE, MAX_VALUE,
               INCREMENT_BY, CYCLE_FLAG, LAST_NUMBER
        FROM ALL_SEQUENCES
        WHERE SEQUENCE_OWNER = :s
        ORDER BY SEQUENCE_NAME
    """, s=schema)
    return [
        {
            "schema":    schema,
            "name":      r[0],
            "start":     r[5] if r[5] else 1,   # LAST_NUMBER as start
            "increment": r[3],
            "min_value": r[1],
            "max_value": r[2],
            "is_cycling": r[4] == "Y",
        }
        for r in cur.fetchall()
    ]


def _oracle_table_ddl_text(cur, schema: str, name: str) -> str:
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
