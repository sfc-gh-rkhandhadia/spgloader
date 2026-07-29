#!/usr/bin/env python3
"""parse_ddl.py — Parse MSSQL/MySQL/MariaDB DDL files into a canonical object inventory (JSON)."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Regex patterns — shared, handle both bracket ([]) and backtick (`) quoting
# ---------------------------------------------------------------------------

RE_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+ALTER\s+)?"
    r"(?P<type>TABLE|VIEW|PROCEDURE|PROC|FUNCTION|TRIGGER|INDEX|SYNONYM|TYPE|SCHEMA)\s+"
    r"(?:[`\[]?(?P<schema>\w+)[`\]]?\.)?"
    r"[`\[]?(?P<name>\w+)[`\]]?",
    re.IGNORECASE | re.MULTILINE,
)

# Matches inline FK constraints inside CREATE TABLE bodies (both dialects)
RE_FK = re.compile(
    r"FOREIGN\s+KEY\s*\([`\[]?(?P<local_cols>[^)\]`]+)[`\]]?\)\s*REFERENCES\s+"
    r"(?:[`\[]?(?P<ref_schema>\w+)[`\]]?\.)?[`\[]?(?P<ref_table>\w+)[`\]]?"
    r"(?:\s*\((?P<ref_cols>[^)]+)\))?",
    re.IGNORECASE,
)

# Standalone ALTER TABLE … FOREIGN KEY (SSMS export format)
RE_ALTER_FK = re.compile(
    r"ALTER\s+TABLE\s+(?:[`\[]?(?P<schema>\w+)[`\]]?\.)?[`\[]?(?P<table>\w+)[`\]]?"
    r".*?FOREIGN\s+KEY\s*\((?P<local_cols>[^)]+)\)\s*"
    r"REFERENCES\s+(?:[`\[]?(?P<ref_schema>\w+)[`\]]?\.)?[`\[]?(?P<ref_table>\w+)[`\]]?"
    r"(?:\s*\((?P<ref_cols>[^)]+)\))?",
    re.IGNORECASE | re.DOTALL,
)

RE_PK = re.compile(
    r"(?:PRIMARY\s+KEY|CONSTRAINT\s+\w+\s+PRIMARY\s+KEY)"
    r"\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)

RE_DEPENDS_ON = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE|EXEC(?:UTE)?)\s+"
    r"(?:[`\[]?(?P<schema>\w+)[`\]]?\.)?"
    r"[`\[]?(?P<name>\w+)[`\]]?",
    re.IGNORECASE,
)

DDL_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

# MySQL: strip DEFINER=`user`@`host` prefix before CREATE
_DEFINER_RE = re.compile(
    r"CREATE\s+DEFINER\s*=\s*`[^`]*`@`[^`]*`\s+",
    re.IGNORECASE,
)

_KEYWORDS = frozenset(
    "SELECT WHERE AND OR NULL NOT IN SET DECLARE BEGIN END AS WITH BY HAVING CASE "
    "WHEN THEN ELSE ON IS TOP DISTINCT ORDER GROUP INTO EXEC EXECUTE FROM JOIN".split()
)

# ---------------------------------------------------------------------------
# Column type vocabularies
# ---------------------------------------------------------------------------

_MSSQL_TYPES = frozenset(
    "INT BIGINT SMALLINT TINYINT BIT DECIMAL NUMERIC FLOAT REAL MONEY SMALLMONEY "
    "DATE DATETIME DATETIME2 SMALLDATETIME TIME DATETIMEOFFSET "
    "CHAR VARCHAR NCHAR NVARCHAR TEXT NTEXT "
    "BINARY VARBINARY IMAGE UNIQUEIDENTIFIER XML TIMESTAMP ROWVERSION "
    "GEOGRAPHY GEOMETRY HIERARCHYID SQL_VARIANT SYSNAME".split()
)

_MYSQL_TYPES = frozenset(
    "INT TINYINT SMALLINT MEDIUMINT BIGINT INTEGER DECIMAL NUMERIC FLOAT DOUBLE "
    "BIT BOOL BOOLEAN DATE DATETIME TIMESTAMP TIME YEAR "
    "CHAR VARCHAR TINYTEXT TEXT MEDIUMTEXT LONGTEXT "
    "BINARY VARBINARY TINYBLOB BLOB MEDIUMBLOB LONGBLOB "
    "ENUM SET JSON".split()
)


# ---------------------------------------------------------------------------
# MSSQL helpers
# ---------------------------------------------------------------------------

def split_statements_mssql(text: str) -> list[str]:
    """Split on GO — T-SQL stored procedures contain semicolons inside BEGIN…END."""
    parts = DDL_GO.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# MySQL helpers
# ---------------------------------------------------------------------------

def split_statements_mysql(text: str) -> list[str]:
    """Split MySQL DDL on statement boundaries.

    Handles:
    - DELIMITER $$ … $$ blocks (stored procedures / functions / triggers)
    - Regular semicolon-terminated statements
    """
    # Normalise line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    stmts: list[str] = []
    current: list[str] = []
    delimiter = ";"
    delim_re = re.compile(r"^\s*DELIMITER\s+(\S+)\s*$", re.IGNORECASE)

    for line in text.splitlines():
        m = delim_re.match(line)
        if m:
            # Flush current buffer before changing delimiter
            block = "\n".join(current).strip()
            if block:
                stmts.append(block)
            current = []
            new_delim = m.group(1)
            delimiter = ";" if new_delim == ";" else new_delim
            continue

        current.append(line)

        # Check if line ends with the current delimiter
        stripped = line.rstrip()
        if stripped.endswith(delimiter):
            block = "\n".join(current).strip()
            # Remove trailing delimiter
            if block.endswith(delimiter):
                block = block[: -len(delimiter)].rstrip()
            if block:
                stmts.append(block)
            current = []

    # Flush remaining
    leftover = "\n".join(current).strip()
    if leftover:
        stmts.append(leftover)

    return stmts


def strip_mysql_header(stmt: str) -> str:
    """Remove DEFINER=`u`@`h` and backtick quoting from a DDL statement."""
    stmt = _DEFINER_RE.sub("CREATE ", stmt)
    stmt = re.sub(r"`(\w+)`", r"\1", stmt)  # backtick → bare identifier
    return stmt


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_col_list(raw: str) -> list[str]:
    return [c.strip().strip("[]`") for c in raw.split(",") if c.strip()]


def _read_file(path: "Path") -> str:
    """Read a file, auto-detecting UTF-16 LE/BE BOMs before falling back to UTF-8."""
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xfe":
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if raw[:2] == b"\xfe\xff":
        return raw.decode("utf-16-be", errors="replace").lstrip("\ufeff")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_source(source: str) -> str:
    p = Path(source)
    if p.is_file():
        return _read_file(p)
    if p.is_dir():
        chunks = []
        for f in sorted(p.rglob("*.sql")):
            chunks.append(f"-- SOURCE: {f}\nGO\n")
            chunks.append(_read_file(f))
            chunks.append("\nGO\n")
        return "\n".join(chunks)
    print(f"ERROR: {source} is not a file or directory", file=sys.stderr)
    sys.exit(1)


def classify_type(raw: str) -> str:
    t = raw.upper()
    return "PROCEDURE" if t in ("PROC", "PROCEDURE") else t


# ---------------------------------------------------------------------------
# Table body parsers — one per dialect
# ---------------------------------------------------------------------------

def _extract_table_body(stmt: str) -> str:
    """Return the text between the first matching outer ( … ) of a CREATE TABLE."""
    first_paren = stmt.find("(")
    if first_paren == -1:
        return ""
    depth = 0
    for i, ch in enumerate(stmt[first_paren:], start=first_paren):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return stmt[first_paren + 1 : i]
    return ""


def parse_table_body_mssql(body: str) -> dict[str, Any]:
    columns: list[dict] = []
    fk_refs: list[dict] = []
    pk_cols: list[str] = []

    col_pat = re.compile(
        r"^[\t ]+\[?(?P<name>\w+)\]?\s+\[?(?P<dtype>[\w]+)\]?(?:\s*\([^)]*\))?"
        r"(?P<opts>[^,\n]*)",
        re.IGNORECASE | re.MULTILINE,
    )
    skip_name = frozenset("CONSTRAINT PRIMARY FOREIGN UNIQUE INDEX CHECK GO WITH ON".split())
    seen_names: set[str] = set()
    for m in col_pat.finditer(body):
        name = m.group("name")
        dtype = m.group("dtype").upper()
        if name.upper() in skip_name:
            continue
        if dtype not in _MSSQL_TYPES:
            continue
        if name.upper() in seen_names:
            continue
        seen_names.add(name.upper())
        opts = m.group("opts") or ""
        columns.append(
            {
                "name": name,
                "data_type": m.group("dtype").strip(),
                "nullable": "NOT NULL" not in opts.upper(),
                "identity": "IDENTITY" in opts.upper(),
                "is_pk": False,
            }
        )

    for m in RE_PK.finditer(body):
        pk_cols = [c.strip().strip("[]") for c in m.group(1).split(",")]

    # Mark PK columns
    pk_set = {c.upper() for c in pk_cols}
    for col in columns:
        col["is_pk"] = col["name"].upper() in pk_set

    for m in RE_FK.finditer(body):
        local_cols = _parse_col_list(m.group("local_cols") or "")
        ref_cols_raw = m.group("ref_cols") or ""
        ref_cols = _parse_col_list(ref_cols_raw) if ref_cols_raw else []
        fk_refs.append(
            {
                "local_columns": local_cols,
                "ref_schema": m.group("ref_schema") or "dbo",
                "ref_table": m.group("ref_table"),
                "ref_columns": ref_cols,
            }
        )

    return {"columns": columns, "pk_columns": pk_cols, "fk_references": fk_refs}


def parse_table_body_mysql(body: str, default_schema: str = "") -> dict[str, Any]:
    """Parse a MySQL CREATE TABLE body into columns, pk_columns, fk_references."""
    # Remove backticks from body for easier parsing
    body_clean = re.sub(r"`(\w+)`", r"\1", body)

    columns: list[dict] = []
    fk_refs: list[dict] = []
    pk_cols: list[str] = []

    # MySQL column line: `name` TYPE[(size)] [NOT NULL] [AUTO_INCREMENT] [DEFAULT x] [COMMENT '...']
    col_pat = re.compile(
        r"^\s+(?P<name>\w+)\s+(?P<dtype>\w+)(?:\s*\([^)]*\))?"
        r"(?P<opts>[^,\n]*)",
        re.IGNORECASE | re.MULTILINE,
    )
    skip_name = frozenset(
        "CONSTRAINT PRIMARY FOREIGN UNIQUE INDEX KEY CHECK ENGINE CHARSET "
        "COLLATE DEFAULT COMMENT ROW_FORMAT".split()
    )
    seen_names: set[str] = set()

    for m in col_pat.finditer(body_clean):
        name = m.group("name")
        dtype = m.group("dtype").upper()
        if name.upper() in skip_name:
            continue
        if dtype not in _MYSQL_TYPES:
            continue
        if name.upper() in seen_names:
            continue
        seen_names.add(name.upper())
        opts = m.group("opts") or ""
        columns.append(
            {
                "name": name,
                "data_type": m.group("dtype").strip(),
                "nullable": "NOT NULL" not in opts.upper(),
                "identity": "AUTO_INCREMENT" in opts.upper(),
                "is_pk": False,
            }
        )

    # PRIMARY KEY (`col1`, `col2`)
    pk_m = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", body_clean, re.IGNORECASE)
    if pk_m:
        pk_cols = [c.strip().strip("`") for c in pk_m.group(1).split(",")]

    pk_set = {c.upper() for c in pk_cols}
    for col in columns:
        col["is_pk"] = col["name"].upper() in pk_set

    for m in RE_FK.finditer(body_clean):
        local_cols = _parse_col_list(m.group("local_cols") or "")
        ref_cols_raw = m.group("ref_cols") or ""
        ref_cols = _parse_col_list(ref_cols_raw) if ref_cols_raw else []
        fk_refs.append(
            {
                "local_columns": local_cols,
                "ref_schema": m.group("ref_schema") or default_schema,
                "ref_table": m.group("ref_table"),
                "ref_columns": ref_cols,
            }
        )

    return {"columns": columns, "pk_columns": pk_cols, "fk_references": fk_refs}


# ---------------------------------------------------------------------------
# Dependency extractor (dialect-agnostic)
# ---------------------------------------------------------------------------

def extract_deps(body: str, self_name: str) -> list[str]:
    deps: set[str] = set()
    for m in RE_DEPENDS_ON.finditer(body):
        schema = m.group("schema") or ""
        name = m.group("name")
        if name.upper() in _KEYWORDS or name.upper() == self_name.upper():
            continue
        if schema:
            deps.add(f"{schema}.{name}")
    return sorted(deps)


# ---------------------------------------------------------------------------
# Core parser — dispatches by source_type
# ---------------------------------------------------------------------------

def parse(source: str, source_type: str = "mssql",
          default_schema: str = "dbo") -> dict[str, Any]:
    text = read_source(source)

    if source_type in ("mysql", "mariadb"):
        # Strip DEFINER before splitting so regex doesn't fail
        text = _DEFINER_RE.sub("CREATE ", text)
        stmts = split_statements_mysql(text)
    else:
        stmts = split_statements_mssql(text)

    objects: dict[str, Any] = {}

    # ---- Pass 1: CREATE statements ----
    for raw_stmt in stmts:
        stmt = strip_mysql_header(raw_stmt) if source_type in ("mysql", "mariadb") else raw_stmt
        m = RE_CREATE.search(stmt)
        if not m:
            continue
        obj_type = classify_type(m.group("type"))
        schema = m.group("schema") or default_schema
        name = m.group("name")
        fqn = f"{schema}.{name}"

        obj: dict[str, Any] = {
            "fqn": fqn,
            "schema": schema,
            "name": name,
            "type": obj_type,
            "ddl": stmt,
        }

        if obj_type == "TABLE":
            body = _extract_table_body(stmt)
            if source_type in ("mysql", "mariadb"):
                obj.update(parse_table_body_mysql(body, default_schema))
            else:
                obj.update(parse_table_body_mssql(body))
            obj["row_producing"] = False

        elif obj_type == "VIEW":
            obj["dependencies"] = extract_deps(stmt, name)
            obj["row_producing"] = True

        elif obj_type == "PROCEDURE":
            obj["dependencies"] = extract_deps(stmt, name)
            if source_type in ("mysql", "mariadb"):
                obj["row_producing"] = bool(
                    re.search(r"^\s+SELECT\b", stmt, re.IGNORECASE | re.MULTILINE)
                )
                obj["has_dynamic_sql"] = bool(
                    re.search(r"\bPREPARE\b|\bEXECUTE\b", stmt, re.IGNORECASE)
                )
            else:
                obj["row_producing"] = bool(
                    re.search(r"^\s+SELECT\b", stmt, re.IGNORECASE | re.MULTILINE)
                )
                obj["has_dynamic_sql"] = bool(
                    re.search(r"\bEXEC\s*\(\s*@", stmt, re.IGNORECASE)
                )

        elif obj_type == "FUNCTION":
            obj["dependencies"] = extract_deps(stmt, name)
            if source_type in ("mysql", "mariadb"):
                # MySQL functions are always SCALAR (no TVFs)
                obj["row_producing"] = False
                obj["function_type"] = "SCALAR"
            else:
                is_tvf = bool(re.search(r"RETURNS\s+TABLE", stmt, re.IGNORECASE))
                obj["row_producing"] = is_tvf
                obj["function_type"] = "TVF" if is_tvf else "SCALAR"

        else:
            obj["dependencies"] = []
            obj["row_producing"] = False

        # TABLE / TYPE collision (MSSQL only, but harmless for MySQL)
        if fqn in objects:
            existing = objects[fqn]["type"]
            if obj_type == "TYPE" and existing == "TABLE":
                objects[f"{fqn}::TYPE"] = obj
                continue
            if obj_type == "TABLE" and existing == "TYPE":
                objects[f"{fqn}::TYPE"] = objects[fqn]

        objects[fqn] = obj

    # ---- Pass 2: ALTER TABLE … FOREIGN KEY (MSSQL SSMS export only) ----
    if source_type not in ("mysql", "mariadb"):
        for raw_stmt in stmts:
            m = RE_ALTER_FK.search(raw_stmt)
            if not m:
                continue
            table_schema = m.group("schema") or default_schema
            table_name   = m.group("table")
            table_fqn    = f"{table_schema}.{table_name}"
            if table_fqn not in objects:
                continue
            local_cols = _parse_col_list(m.group("local_cols") or "")
            ref_schema = m.group("ref_schema") or default_schema
            ref_table  = m.group("ref_table")
            ref_cols_raw = m.group("ref_cols") or ""
            ref_cols   = _parse_col_list(ref_cols_raw) if ref_cols_raw else []
            objects[table_fqn].setdefault("fk_references", []).append(
                {
                    "local_columns": local_cols,
                    "ref_schema":    ref_schema,
                    "ref_table":     ref_table,
                    "ref_columns":   ref_cols,
                }
            )

    # ---- Pass 3: TRIGGER statements ----
    for raw_stmt in stmts:
        stmt = strip_mysql_header(raw_stmt) if source_type in ("mysql", "mariadb") else raw_stmt
        m = RE_CREATE.search(stmt)
        if not m:
            continue
        if classify_type(m.group("type")) != "TRIGGER":
            continue
        schema = m.group("schema") or default_schema
        name   = m.group("name")
        fqn    = f"{schema}.{name}"
        if fqn in objects:
            continue
        on_match = re.search(
            r"\bON\b\s+(?:[`\[]?(?P<s>\w+)[`\]]?\.)?[`\[]?(?P<t>\w+)[`\]]?",
            stmt, re.IGNORECASE,
        )
        target_table = ""
        if on_match:
            ts = on_match.group("s") or schema
            tt = on_match.group("t")
            target_table = f"{ts}.{tt}"
        objects[fqn] = {
            "fqn":          fqn,
            "schema":       schema,
            "name":         name,
            "type":         "TRIGGER",
            "ddl":          stmt,
            "target_table": target_table,
            "dependencies": [target_table] if target_table else [],
            "row_producing": False,
        }

    by_type: dict[str, int] = {}
    for o in objects.values():
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1

    return {
        "objects": list(objects.values()),
        "summary": {
            "total": len(objects),
            "by_type": by_type,
            "duplicates": [],
            "row_producing_count": sum(1 for o in objects.values() if o.get("row_producing")),
        },
    }


# ---------------------------------------------------------------------------
# --ddl-objects passthrough — primary MySQL path
# ---------------------------------------------------------------------------

def enrich_from_ddl_objects(path: str, source_type: str,
                             default_schema: str) -> dict[str, Any]:
    """Load a pre-parsed ddl_objects.json produced by extract_ddl.py and
    enrich each TABLE object with column metadata parsed from its .ddl field.

    This is the recommended MySQL path because extract_ddl.py already catalogues
    all 6 database schemas into a single ddl_objects.json — no need to re-read
    raw .sql files.
    """
    raw = json.loads(Path(path).read_text())
    # Accept both flat list and {"objects": [...]} wrapper
    objs: list[dict] = raw if isinstance(raw, list) else raw.get("objects", [])

    enriched: list[dict] = []
    for o in objs:
        o["type"] = o.get("type", "").upper()

        # Ensure schema / fqn are set
        if not o.get("schema"):
            o["schema"] = default_schema
        if not o.get("fqn"):
            o["fqn"] = f"{o['schema']}.{o['name']}"

        ddl = o.get("ddl", "")

        if o["type"] == "TABLE":
            body = _extract_table_body(ddl)
            if source_type in ("mysql", "mariadb"):
                tbl_data = parse_table_body_mysql(body, o["schema"])
            else:
                tbl_data = parse_table_body_mssql(body)
            # Only update keys that aren't already present (don't overwrite
            # columns already enriched by an earlier step)
            if not o.get("columns"):
                o.update(tbl_data)
            o["row_producing"] = False

        elif o["type"] == "VIEW":
            o.setdefault("row_producing", True)
            o.setdefault("dependencies", [])

        elif o["type"] == "FUNCTION":
            if source_type in ("mysql", "mariadb"):
                o.setdefault("row_producing", False)
                o.setdefault("function_type", "SCALAR")
            else:
                is_tvf = bool(re.search(r"RETURNS\s+TABLE", ddl, re.IGNORECASE))
                o.setdefault("row_producing", is_tvf)
                o.setdefault("function_type", "TVF" if is_tvf else "SCALAR")

        elif o["type"] == "PROCEDURE":
            o.setdefault("row_producing", bool(
                re.search(r"^\s+SELECT\b", ddl, re.IGNORECASE | re.MULTILINE)
            ))
            o.setdefault("dependencies", [])

        else:
            o.setdefault("row_producing", False)
            o.setdefault("dependencies", [])

        enriched.append(o)

    by_type: dict[str, int] = {}
    for o in enriched:
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1

    return {
        "objects": enriched,
        "summary": {
            "total": len(enriched),
            "by_type": by_type,
            "duplicates": [],
            "row_producing_count": sum(1 for o in enriched if o.get("row_producing")),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse SQL DDL (MSSQL/MySQL/MariaDB) into a canonical object inventory"
    )
    parser.add_argument(
        "--source", default=None,
        help="DDL source: file or directory of .sql files",
    )
    parser.add_argument(
        "--ddl-objects", default=None,
        help="Path to pre-parsed ddl_objects.json (MySQL shortcut — output of extract_ddl.py)",
    )
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument(
        "--source-type", default="mssql",
        choices=["mssql", "mysql", "mariadb"],
        help="Source DB dialect (default: mssql)",
    )
    parser.add_argument(
        "--database", default=None,
        help="Default schema name for MySQL/MariaDB (replaces 'dbo'); "
             "e.g. evdas, sapphire",
    )
    args = parser.parse_args()

    if not args.source and not args.ddl_objects:
        parser.error("one of --source or --ddl-objects is required")

    # Determine default schema
    source_type = args.source_type.lower()
    if args.database:
        default_schema = args.database
    elif source_type in ("mysql", "mariadb"):
        # Fall back to directory/file stem when --database not given
        src = args.source or args.ddl_objects
        default_schema = Path(src).stem if src else "public"
    else:
        default_schema = "dbo"

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    if args.ddl_objects:
        print(f"Loading pre-parsed ddl_objects from: {args.ddl_objects}")
        inventory = enrich_from_ddl_objects(args.ddl_objects, source_type, default_schema)
    else:
        print(f"Parsing DDL from: {args.source}  [source-type={source_type}]")
        inventory = parse(args.source, source_type, default_schema)

    with open(args.output, "w") as f:
        json.dump(inventory, f, indent=2, default=str)

    s = inventory["summary"]
    print(f"Found {s['total']} objects:")
    for t, c in s["by_type"].items():
        print(f"  {t}: {c}")
    print(f"Row-producing targets: {s['row_producing_count']}")
    print(f"Written to: {args.output}")


if __name__ == "__main__":
    main()
