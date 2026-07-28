#!/usr/bin/env python3
"""parse_ddl.py - Parse MSSQL DDL files into a canonical object inventory (JSON)."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

RE_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+ALTER\s+)?"
    r"(?P<type>TABLE|VIEW|PROCEDURE|PROC|FUNCTION|TRIGGER|INDEX|SYNONYM|TYPE|SCHEMA)\s+"
    r"(?:\[?(?P<schema>\w+)\]?\.)?\[?(?P<name>\w+)\]?",
    re.IGNORECASE | re.MULTILINE,
)

# Captures: local_cols, ref_schema (opt), ref_table, ref_cols (opt)
# Matches inline FKs inside CREATE TABLE bodies
RE_FK = re.compile(
    r"FOREIGN\s+KEY\s*\((?P<local_cols>[^)]+)\)\s*REFERENCES\s+"
    r"(?:\[?(?P<ref_schema>\w+)\]?\.)?\[?(?P<ref_table>\w+)\]?"
    r"(?:\s*\((?P<ref_cols>[^)]+)\))?",
    re.IGNORECASE,
)

# Matches standalone ALTER TABLE ... FOREIGN KEY statements (SSMS export format)
RE_ALTER_FK = re.compile(
    r"ALTER\s+TABLE\s+(?:\[?(?P<schema>\w+)\]?\.)?\[?(?P<table>\w+)\]?"
    r".*?FOREIGN\s+KEY\s*\((?P<local_cols>[^)]+)\)\s*"
    r"REFERENCES\s+(?:\[?(?P<ref_schema>\w+)\]?\.)?\[?(?P<ref_table>\w+)\]?"
    r"(?:\s*\((?P<ref_cols>[^)]+)\))?",
    re.IGNORECASE | re.DOTALL,
)


def _parse_col_list(raw: str) -> list[str]:
    """Strip brackets and whitespace from a comma-separated column list."""
    return [c.strip().strip("[]") for c in raw.split(",") if c.strip()]

RE_PK = re.compile(
    r"(?:PRIMARY\s+KEY|CONSTRAINT\s+\w+\s+PRIMARY\s+KEY)"
    r"\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)

RE_DEPENDS_ON = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE|EXEC(?:UTE)?)\s+"
    r"(?:\[?(?P<schema>\w+)\]?\.)?\[?(?P<name>\w+)\]?",
    re.IGNORECASE,
)

DDL_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

_KEYWORDS = frozenset(
    "SELECT WHERE AND OR NULL NOT IN SET DECLARE BEGIN END AS WITH BY HAVING CASE "
    "WHEN THEN ELSE ON IS TOP DISTINCT ORDER GROUP INTO EXEC EXECUTE FROM JOIN".split()
)


def split_statements(text: str) -> list[str]:
    # Split ONLY on GO — never on semicolons.
    # T-SQL stored procedures/functions contain semicolons inside BEGIN...END
    # blocks; splitting on them would truncate multi-statement objects.
    parts = DDL_GO.split(text)
    out = []
    for part in parts:
        part = part.strip()
        if part:
            out.append(part)
    return out


def _read_file(path: "Path") -> str:
    """Read a file, auto-detecting UTF-16 LE/BE BOMs before falling back to UTF-8."""
    raw = path.read_bytes()
    if raw[:2] == b'\xff\xfe':
        return raw.decode("utf-16-le", errors="replace").lstrip('\ufeff')
    if raw[:2] == b'\xfe\xff':
        return raw.decode("utf-16-be", errors="replace").lstrip('\ufeff')
    if raw[:3] == b'\xef\xbb\xbf':
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


def parse_table_body(body: str) -> dict[str, Any]:
    columns: list[dict] = []
    fk_refs: list[dict] = []
    pk_cols: list[str] = []

    col_pat = re.compile(
        r"^[\t ]+\[?(?P<name>\w+)\]?\s+\[?(?P<dtype>[\w]+)\]?(?:\s*\([^)]*\))?"
        r"(?P<opts>[^,\n]*)",
        re.IGNORECASE | re.MULTILINE,
    )
    skip_name = frozenset(
        "CONSTRAINT PRIMARY FOREIGN UNIQUE INDEX CHECK GO WITH ON".split()
    )
    # Data-type keywords; anything else in the dtype position is a sort/option keyword
    valid_dtypes = frozenset(
        "INT BIGINT SMALLINT TINYINT BIT DECIMAL NUMERIC FLOAT REAL MONEY SMALLMONEY "
        "DATE DATETIME DATETIME2 SMALLDATETIME TIME DATETIMEOFFSET "
        "CHAR VARCHAR NCHAR NVARCHAR TEXT NTEXT "
        "BINARY VARBINARY IMAGE UNIQUEIDENTIFIER XML TIMESTAMP ROWVERSION "
        "GEOGRAPHY GEOMETRY HIERARCHYID SQL_VARIANT SYSNAME".split()
    )
    seen_names: set[str] = set()
    for m in col_pat.finditer(body):
        name = m.group("name")
        dtype = m.group("dtype").upper()
        if name.upper() in skip_name:
            continue
        if dtype not in valid_dtypes:
            continue  # sort order, WITH, PAD_INDEX, etc.
        if name.upper() in seen_names:
            continue  # deduplicate (PK constraint repeats column names)
        seen_names.add(name.upper())
        opts = m.group("opts") or ""
        columns.append(
            {
                "name": name,
                "data_type": m.group("dtype").strip(),
                "nullable": "NOT NULL" not in opts.upper(),
                "identity": "IDENTITY" in opts.upper(),
            }
        )

    for m in RE_PK.finditer(body):
        pk_cols = [c.strip().strip("[]") for c in m.group(1).split(",")]

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


def extract_deps(body: str, self_name: str) -> list[str]:
    deps: set[str] = set()
    for m in RE_DEPENDS_ON.finditer(body):
        schema = m.group("schema") or "dbo"
        name = m.group("name")
        if name.upper() in _KEYWORDS or name.upper() == self_name.upper():
            continue
        deps.add(f"{schema}.{name}")
    return sorted(deps)


def parse(source: str) -> dict[str, Any]:
    text = read_source(source)
    stmts = split_statements(text)
    objects: dict[str, Any] = {}
    duplicates: list[str] = []

    # ---- Pass 1: CREATE statements ----
    for stmt in stmts:
        m = RE_CREATE.search(stmt)
        if not m:
            continue
        obj_type = classify_type(m.group("type"))
        schema = m.group("schema") or "dbo"
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
            # Find table body by bracket counting (handles ') ON [PRIMARY]' suffix)
            first_paren = stmt.find('(')
            body = ""
            if first_paren != -1:
                depth = 0
                end = first_paren
                for i, ch in enumerate(stmt[first_paren:], start=first_paren):
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            body = stmt[first_paren + 1:i]
                            break
            obj.update(parse_table_body(body))
            obj["row_producing"] = False

        elif obj_type == "VIEW":
            obj["dependencies"] = extract_deps(stmt, name)
            obj["row_producing"] = True

        elif obj_type == "PROCEDURE":
            obj["dependencies"] = extract_deps(stmt, name)
            obj["row_producing"] = bool(
                re.search(r"^\s+SELECT\b", stmt, re.IGNORECASE | re.MULTILINE)
            )
            obj["has_dynamic_sql"] = bool(
                re.search(r"\bEXEC\s*\(\s*@", stmt, re.IGNORECASE)
            )

        elif obj_type == "FUNCTION":
            obj["dependencies"] = extract_deps(stmt, name)
            is_tvf = bool(re.search(r"RETURNS\s+TABLE", stmt, re.IGNORECASE))
            obj["row_producing"] = is_tvf
            obj["function_type"] = "TVF" if is_tvf else "SCALAR"

        else:
            obj["dependencies"] = []
            obj["row_producing"] = False

        # TABLE / TYPE FQN collision: SQL Server allows both to coexist.
        # Keep TABLE at the plain FQN key; stash TYPE under a qualified key so
        # both appear in inventory["objects"] and get deployed in their phases.
        if fqn in objects:
            existing = objects[fqn]["type"]
            if obj_type == "TYPE" and existing == "TABLE":
                objects[f"{fqn}::TYPE"] = obj  # don't overwrite TABLE
                continue
            if obj_type == "TABLE" and existing == "TYPE":
                objects[f"{fqn}::TYPE"] = objects[fqn]  # save TYPE, TABLE wins plain key

        objects[fqn] = obj

    # ---- Pass 2: ALTER TABLE … FOREIGN KEY statements (SSMS export format) ----
    for stmt in stmts:
        m = RE_ALTER_FK.search(stmt)
        if not m:
            continue
        table_schema = m.group("schema") or "dbo"
        table_name   = m.group("table")
        table_fqn    = f"{table_schema}.{table_name}"
        if table_fqn not in objects:
            continue
        local_cols = _parse_col_list(m.group("local_cols") or "")
        ref_schema = m.group("ref_schema") or "dbo"
        ref_table  = m.group("ref_table")
        ref_cols_raw = m.group("ref_cols") or ""
        ref_cols   = _parse_col_list(ref_cols_raw) if ref_cols_raw else []
        objects[table_fqn].setdefault("fk_references", []).append({
            "local_columns": local_cols,
            "ref_schema":    ref_schema,
            "ref_table":     ref_table,
            "ref_columns":   ref_cols,
        })

    # ---- Pass 3: CREATE TRIGGER statements ----
    # Triggers are often embedded at the end of table SQL files and are missed
    # by Pass 1 because the bracket-counter stops at the end of CREATE TABLE.
    # This pass explicitly scans every GO-separated statement for TRIGGER objects.
    for stmt in stmts:
        m = RE_CREATE.search(stmt)
        if not m:
            continue
        if classify_type(m.group("type")) != "TRIGGER":
            continue
        schema = m.group("schema") or "dbo"
        name   = m.group("name")
        fqn    = f"{schema}.{name}"
        if fqn in objects:
            continue  # already seen
        # Determine which table this trigger is ON
        on_match = re.search(
            r"\bON\b\s+(?:\[?(?P<s>\w+)\]?\.)?\[?(?P<t>\w+)\]?",
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
            "duplicates": duplicates,
            "row_producing_count": sum(
                1 for o in objects.values() if o.get("row_producing")
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse MSSQL DDL into a canonical object inventory"
    )
    parser.add_argument(
        "--source", required=True, help="DDL source: file or directory of .sql files"
    )
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    print(f"Parsing DDL from: {args.source}")
    inventory = parse(args.source)

    with open(args.output, "w") as f:
        json.dump(inventory, f, indent=2, default=str)

    s = inventory["summary"]
    print(f"Found {s['total']} objects:")
    for t, c in s["by_type"].items():
        print(f"  {t}: {c}")
    print(f"Row-producing targets: {s['row_producing_count']}")
    if s["duplicates"]:
        print(f"Duplicates: {s['duplicates']}")
    print(f"Written to: {args.output}")


if __name__ == "__main__":
    main()
