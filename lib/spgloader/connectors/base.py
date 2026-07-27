"""Base connector interface and shared DDL parsing utilities."""
from __future__ import annotations

import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared object schema
# ---------------------------------------------------------------------------

def make_object(obj_type: str, schema: str, name: str, ddl: str, depends_on: list[str]) -> dict:
    return {
        "type": obj_type,
        "schema": schema,
        "name": name,
        "fqn": f"{schema}.{name}" if schema else name,
        "ddl": ddl.strip(),
        "depends_on": depends_on,
    }


# ---------------------------------------------------------------------------
# Abstract base connector
# ---------------------------------------------------------------------------

class Connector(ABC):
    def __init__(self, host: str, port: int, database: str, user: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    @abstractmethod
    def extract(self) -> list[dict]:
        """Extract all schema objects from the live source database."""
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        """Return True if a test query succeeds, False otherwise."""
        ...

    def extract_bit_columns(self) -> dict[str, list[str]]:
        """Return {schema.table: [col_name, ...]} for boolean-equivalent columns.

        Subclasses should override this to query the source catalog.
        Returns empty dict if not implemented (DDL-file mode falls back to this).
        """
        return {}


# ---------------------------------------------------------------------------
# DDL file parsing (source-type-aware, no live connection required)
# ---------------------------------------------------------------------------

def parse_ddl_file(path: str, source_type: str) -> list[dict]:
    """Parse a .sql DDL file and return list of object dicts."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    statements = _split_statements(text, source_type)
    objects = []
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        obj = _classify_statement(stmt, source_type)
        if obj:
            objects.append(obj)
    return objects


def _split_statements(text: str, source_type: str) -> list[str]:
    """Split DDL text into individual statements."""
    if source_type == "oracle":
        parts = re.split(r"\n\s*/\s*\n", text)
    elif source_type == "mssql":
        go_parts = re.split(r"^\s*GO\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
        parts = go_parts if len(go_parts) > 1 else re.split(r";", text)
    else:
        parts = re.split(r";", text)
    return [p.strip() for p in parts if p.strip()]


def _classify_statement(stmt: str, source_type: str) -> dict | None:
    """Classify a single DDL statement into an object dict."""
    upper = stmt.upper().lstrip()
    patterns = [
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+([^\s(]+)", "table"),
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([^\s(]+)", "view"),
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+([^\s(]+)", "procedure"),
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([^\s(]+)", "function"),
        (r"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+([^\s(]+)", "trigger"),
    ]
    for pattern, obj_type in patterns:
        m = re.search(pattern, upper)
        if m:
            raw_name = m.group(1).strip("[]`\"'")
            parts = raw_name.split(".")
            schema = parts[-2] if len(parts) >= 2 else ""
            name = parts[-1]
            deps = _extract_deps_from_sql(stmt, tables=[])
            return make_object(obj_type, schema, name, stmt, deps)
    return None


def _extract_deps_from_sql(sql: str, tables: list[str]) -> list[str]:
    """Heuristically extract table name dependencies from SQL text."""
    pattern = r"(?:FROM|JOIN)\s+([\w\[\]`\".]+)"
    matches = re.findall(pattern, sql, re.IGNORECASE)
    deps: set[str] = set()
    for m in matches:
        raw = m.strip("[]`\"")
        parts = raw.split(".")
        name = parts[-1]
        if tables:
            if name in tables:
                deps.add(name)
        else:
            deps.add(name)
    return sorted(deps)
