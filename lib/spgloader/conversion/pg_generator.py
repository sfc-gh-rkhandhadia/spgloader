"""pg_generator.py — Generate clean PostgreSQL DDL from a source-agnostic schema model.

Takes the normalized dicts produced by connector.catalog_extract() and generates
valid PostgreSQL CREATE TABLE / CREATE INDEX / ALTER TABLE FOREIGN KEY statements
without touching any DDL text.  This is the catalog-based equivalent of pgloader's
schema generation and avoids all regex fragility.

Supported source types: mssql | mysql | mariadb | oracle
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Type mapping tables — source type name → PostgreSQL type string
# ---------------------------------------------------------------------------

# Called as: _pg_type("mssql", col) where col is a column dict from catalog_extract
# Each entry is either a string or callable(col) → str

MSSQL_TYPE_MAP: dict[str, Any] = {
    "uniqueidentifier": "uuid",
    "bit":              "boolean",
    "tinyint":          "smallint",
    "smallint":         "smallint",
    "int":              "integer",
    "bigint":           "bigint",
    "real":             "real",
    "float":            "double precision",
    "decimal":          lambda c: f"numeric({c['precision']},{c['scale'] or 0})" if c.get("precision") else "numeric",
    "numeric":          lambda c: f"numeric({c['precision']},{c['scale'] or 0})" if c.get("precision") else "numeric",
    "money":            "numeric(19,4)",
    "smallmoney":       "numeric(10,4)",
    "char":             "text",
    "nchar":            "text",
    "varchar":          "text",
    "nvarchar":         "text",
    "text":             "text",
    "ntext":            "text",
    "xml":              "text",
    "binary":           "bytea",
    "varbinary":        "bytea",
    "image":            "bytea",
    "datetime":         "timestamptz",
    "smalldatetime":    "timestamp",
    "datetime2":        "timestamptz",
    "datetimeoffset":   "timestamptz",
    "date":             "date",
    "time":             "time",
    "rowversion":       "bytea",
    "timestamp":        "bytea",     # MSSQL TIMESTAMP = rowversion, not a date type
    "hierarchyid":      "text",
    "geography":        "bytea",
    "geometry":         "bytea",
    "sql_variant":      "text",
    "sysname":          "text",
}

MYSQL_TYPE_MAP: dict[str, Any] = {
    "tinyint_bool":  "boolean",   # connector sets this for tinyint(1)
    "tinyint":       "smallint",
    "smallint":      "smallint",
    "mediumint":     "integer",
    "int":           "integer",
    "bigint":        "bigint",
    "decimal":       lambda c: f"numeric({c['precision']},{c['scale'] or 0})" if c.get("precision") else "numeric",
    "numeric":       lambda c: f"numeric({c['precision']},{c['scale'] or 0})" if c.get("precision") else "numeric",
    "float":         "real",
    "double":        "double precision",
    # MySQL BIT(n): preserve size from col_type e.g. "bit(1)" → "bit(1)"
    "bit":           lambda c: f"bit({c['precision']})" if c.get("precision") else "bit(1)",
    "char":          "text",
    "varchar":       "text",
    "tinytext":      "text",
    "text":          "text",
    "mediumtext":    "text",
    "longtext":      "text",
    "tinyblob":      "bytea",
    "blob":          "bytea",
    "mediumblob":    "bytea",
    "longblob":      "bytea",
    "binary":        "bytea",
    "varbinary":     "bytea",
    "date":          "date",
    "time":          "time",
    "datetime":      "timestamptz",
    "timestamp":     "timestamptz",
    "year":          "integer",
    "json":          "jsonb",
    "enum":          "text",
    "set":           "text",
    "geometry":      "bytea",
    "point":         "bytea",
    "linestring":    "bytea",
    "polygon":       "bytea",
    "multipoint":    "bytea",
    "multilinestring": "bytea",
    "multipolygon":  "bytea",
    "geometrycollection": "bytea",
}

# MariaDB shares MySQL type mapping
MARIADB_TYPE_MAP = MYSQL_TYPE_MAP

ORACLE_TYPE_MAP: dict[str, Any] = {
    "NUMBER":   lambda c: (
        "integer"          if c.get("scale") == 0 and (c.get("precision") or 0) <= 9  else
        "bigint"           if c.get("scale") == 0 and (c.get("precision") or 0) <= 18 else
        f"numeric({c['precision']},{c['scale'] or 0})" if c.get("precision") else "numeric"
    ),
    "FLOAT":             "double precision",
    "BINARY_FLOAT":      "real",
    "BINARY_DOUBLE":     "double precision",
    "CHAR":              "text",
    "NCHAR":             "text",
    "VARCHAR2":          "text",
    "NVARCHAR2":         "text",
    "CLOB":              "text",
    "NCLOB":             "text",
    "LONG":              "text",
    "BLOB":              "bytea",
    "RAW":               "bytea",
    "LONG RAW":          "bytea",
    "BFILE":             "text",
    "DATE":              "timestamptz",  # Oracle DATE includes time component
    "TIMESTAMP":         "timestamptz",
    "TIMESTAMP WITH TIME ZONE":              "timestamptz",
    "TIMESTAMP WITH LOCAL TIME ZONE":        "timestamptz",
    "INTERVAL YEAR TO MONTH":               "interval",
    "INTERVAL DAY TO SECOND":               "interval",
    "XMLTYPE":           "text",
    "SDO_GEOMETRY":      "bytea",
    "ROWID":             "text",
    "UROWID":            "text",
    "BOOLEAN":           "boolean",    # Oracle 23c
    "SMALLINT":          "smallint",
    "INTEGER":           "integer",
    "INT":               "integer",
    "REAL":              "real",
    "DOUBLE PRECISION":  "double precision",
}

_TYPE_MAPS = {
    "mssql":   MSSQL_TYPE_MAP,
    "mysql":   MYSQL_TYPE_MAP,
    "mariadb": MARIADB_TYPE_MAP,
    "oracle":  ORACLE_TYPE_MAP,
}

# ---------------------------------------------------------------------------
# Default expression normalisation
# ---------------------------------------------------------------------------

_DEFAULT_PATTERNS = [
    # Numeric defaults: ((42)) → 42  (must come BEFORE any boolean patterns
    # so ((1)) / ((0)) stay as 1 / 0 and are not promoted to TRUE / FALSE for
    # non-boolean columns; the boolean guard in _gen_column handles the rest)
    (re.compile(r"^\(\((-?\d+(?:\.\d+)?)\)\)$"), r"\1"),
    # String defaults: (N'value') or ('value')
    # String defaults: (N'value') or ('value')
    (re.compile(r"^\(N'(.*)'\)$", re.S), r"'\1'"),
    (re.compile(r"^\('(.*)'\)$", re.S), r"'\1'"),
    # Bare NULL
    (re.compile(r"^\(NULL\)$", re.I), "NULL"),
    # Function substitutions
    (re.compile(r"getdate\s*\(\s*\)", re.I), "now()"),
    (re.compile(r"getutcdate\s*\(\s*\)", re.I), "(now() at time zone 'UTC')"),
    (re.compile(r"sysutcdatetime\s*\(\s*\)", re.I), "now()"),
    (re.compile(r"sysdatetime\s*\(\s*\)", re.I), "now()"),
    (re.compile(r"newid\s*\(\s*\)", re.I), "gen_random_uuid()"),
    (re.compile(r"newsequentialid\s*\(\s*\)", re.I), "gen_random_uuid()"),
    # MSSQL audit functions — map to PostgreSQL equivalents
    (re.compile(r"suser_sname\s*\(\s*\)", re.I), "current_user"),
    (re.compile(r"app_name\s*\(\s*\)", re.I), "current_setting('application_name', true)"),
    # MSSQL ISNULL(x, y) → PostgreSQL COALESCE(x, y)
    (re.compile(r"ISNULL\s*\(", re.I), "COALESCE("),
    # MSSQL CONVERT(type, 'literal') — extract the string literal
    (re.compile(r"CONVERT\s*\(\s*\w+\s*,\s*'([^']*)'\s*\)", re.I), r"'\1'"),
    (re.compile(r"sysdate", re.I), "now()"),    # Oracle SYSDATE
    (re.compile(r"systimestamp", re.I), "now()"),
    (re.compile(r"current_timestamp", re.I), "CURRENT_TIMESTAMP"),
]

# PostgreSQL reserved keywords that must be double-quoted as column names
_PG_RESERVED = frozenset({
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "asymmetric", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "cross", "current_catalog", "current_date",
    "current_role", "current_schema", "current_time", "current_timestamp",
    "current_user", "default", "deferrable", "desc", "distinct", "do",
    "else", "end", "except", "false", "fetch", "for", "foreign", "from",
    "full", "grant", "group", "having", "ilike", "in", "initially",
    "inner", "intersect", "into", "is", "isnull", "join", "leading",
    "left", "like", "limit", "localtime", "localtimestamp", "natural",
    "not", "notnull", "null", "offset", "on", "only", "or", "order",
    "outer", "over", "overlaps", "placing", "primary", "references",
    "returning", "right", "select", "session_user", "similar", "some",
    "symmetric", "table", "then", "to", "trailing", "true", "union",
    "unique", "user", "using", "variadic", "verbose", "when", "where",
    "window", "with",
    # Common types that clash as identifiers
    "value", "values", "type", "index", "date", "time", "year", "month",
    "day", "hour", "minute", "second", "zone", "range", "function",
    "row", "rows", "key", "name", "position", "language", "comment",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_ddl(schema_model: dict, source_type: str) -> dict[str, list[str]]:
    """Generate all DDL statements from a catalog schema model.

    Returns a dict keyed by phase:
      schemas      — CREATE SCHEMA statements
      sequences    — CREATE SEQUENCE statements
      tables       — CREATE TABLE statements (no FKs)
      indexes      — CREATE INDEX statements
      foreign_keys — ALTER TABLE ADD CONSTRAINT FOREIGN KEY statements
    """
    st = source_type.lower()
    type_map = _TYPE_MAPS.get(st, MSSQL_TYPE_MAP)

    return {
        "schemas":      _gen_schemas(schema_model.get("schemas", [])),
        "sequences":    _gen_sequences(schema_model.get("sequences", [])),
        "tables":       _gen_tables(schema_model.get("tables", []), type_map),
        "indexes":      _gen_indexes(schema_model.get("indexes", [])),
        "foreign_keys": _gen_foreign_keys(schema_model.get("foreign_keys", [])),
    }


# ---------------------------------------------------------------------------
# Phase generators
# ---------------------------------------------------------------------------

def _gen_schemas(schemas: list[str]) -> list[str]:
    return [f'CREATE SCHEMA IF NOT EXISTS "{s.lower()}";' for s in schemas]


def _gen_sequences(sequences: list[dict]) -> list[str]:
    stmts = []
    for seq in sequences:
        schema = seq["schema"].lower()
        name   = seq["name"].lower()
        start  = seq.get("start", 1)
        inc    = seq.get("increment", 1)
        min_v  = seq.get("min_value", 1)
        max_v  = seq.get("max_value")
        cycle  = "CYCLE" if seq.get("is_cycling") else "NO CYCLE"

        parts = [
            f'CREATE SEQUENCE IF NOT EXISTS "{schema}"."{name}"',
            f"    START WITH {start}",
            f"    INCREMENT BY {inc}",
            f"    MINVALUE {min_v}",
        ]
        if max_v and max_v < 9_223_372_036_854_775_807:
            parts.append(f"    MAXVALUE {max_v}")
        parts.append(f"    {cycle};")
        stmts.append("\n".join(parts))
    return stmts


def _gen_tables(tables: list[dict], type_map: dict) -> list[str]:
    stmts = []
    for tbl in tables:
        schema = tbl["schema"].lower()
        name   = tbl["name"].lower()
        cols   = tbl.get("columns", [])
        pk     = [c.lower() for c in tbl.get("primary_key", [])]

        col_defs = []
        for col in cols:
            col_defs.append(_gen_column(col, type_map))

        if pk:
            pk_list = ", ".join(_quote_ident(c) for c in pk)
            col_defs.append(f"    PRIMARY KEY ({pk_list})")

        if not col_defs:
            col_defs = ["    -- no columns extracted"]

        body = ",\n".join(col_defs)
        stmts.append(
            f'CREATE TABLE IF NOT EXISTS "{schema}"."{name}" (\n{body}\n);'
        )
    return stmts


def _gen_column(col: dict, type_map: dict) -> str:
    raw_name     = col["name"]
    col_name     = _quote_ident(raw_name.lower())
    pg_type      = _resolve_type(col, type_map)
    nullable     = col.get("is_nullable", True)
    identity     = col.get("is_identity", False)
    computed     = col.get("is_computed", False)
    computed_expr = col.get("computed_expr")
    default      = _normalise_default(col.get("default_expr"))

    parts = [f"    {col_name} {pg_type}"]

    if computed and computed_expr:
        # MSSQL computed column → PG GENERATED ALWAYS AS (expr) STORED
        # First check if the expression is safely convertible; if it references
        # MSSQL-specific functions (CONVERT, TRY_CONVERT) or schema-qualified UDFs
        # that aren't yet deployed, fall through to a plain nullable column instead
        # of failing the entire table.
        pg_expr = _mssql_expr_to_pg(computed_expr)
        if _mssql_expr_is_convertible(pg_expr):
            parts.append(f"GENERATED ALWAYS AS ({pg_expr}) STORED")
            return " ".join(parts)
        # Unconvertible: emit as a plain nullable column (no GENERATED clause).
        # The expression is preserved in a comment for manual review.
        safe_comment = computed_expr.replace('*/', '* /')[:120]
        parts = [f"    {col_name} {pg_type} /* computed: {safe_comment} */"]
        return parts[0]

    if identity:
        parts.append("GENERATED ALWAYS AS IDENTITY")
    else:
        # Boolean default: convert integer literals 0/1 → false/true
        if pg_type == "boolean" and default is not None:
            if default.strip() in ("0", "false"):
                default = "false"
            elif default.strip() in ("1", "true"):
                default = "true"
            elif default.strip() not in ("null", "NULL", "false", "true",
                                         "FALSE", "TRUE", "false", "true"):
                default = None

        # Temporal columns: drop numeric/arithmetic defaults
        if default is not None and pg_type in ("timestamptz", "timestamp", "date", "time"):
            if re.match(r'^[\d()\s\-+*/\.]+$', default.strip()):
                default = None

        if default is not None:
            parts.append(f"DEFAULT {default}")

    if not nullable:
        parts.append("NOT NULL")

    return " ".join(parts)


def _gen_indexes(indexes: list[dict]) -> list[str]:
    stmts = []
    for idx in indexes:
        schema = idx["schema"].lower()
        table  = idx["table_name"].lower()
        # Qualify the index name with the table name so that reused index names
        # (legal in MySQL/MSSQL, illegal in PG where index names are unique per
        # schema) do not collide. Falls back to the bare name when it already
        # encodes the table, keeping names short for single-table cases.
        bare_name = idx["name"]
        if bare_name and table and not bare_name.lower().startswith(table + "_"):
            qualified = f"{table}_{bare_name}"
        else:
            qualified = bare_name
        safe_name = _safe_index_name(qualified)
        unique = "UNIQUE " if idx.get("is_unique") else ""
        cols   = ", ".join(_quote_ident(_strip_mssql_brackets(c.split()[0]).lower()) +
                           (" DESC" if c.upper().endswith(" DESC") else "")
                           for c in idx.get("columns", []))
        if not cols:
            continue

        stmt = f'CREATE {unique}INDEX IF NOT EXISTS "{safe_name}" ON "{schema}"."{table}" ({cols})'

        if idx.get("include_cols"):
            inc = ", ".join(_quote_ident(_strip_mssql_brackets(c).lower()) for c in idx["include_cols"])
            stmt += f" INCLUDE ({inc})"

        if idx.get("predicate"):
            # Strip MSSQL bracket identifiers from the filter expression
            predicate = re.sub(r'\[([^\]]+)\]', r'\1', idx['predicate'])
            stmt += f" WHERE {predicate.lower()}"

        stmt += ";"
        stmts.append(stmt)
    return stmts


def _gen_foreign_keys(foreign_keys: list[dict]) -> list[str]:
    stmts = []
    for fk in foreign_keys:
        from_schema = fk["from_schema"].lower()
        from_table  = fk["from_table"].lower()
        to_schema   = fk["to_schema"].lower()
        to_table    = fk["to_table"].lower()
        fk_name     = fk["name"].lower()
        from_cols   = ", ".join(_quote_ident(c.lower()) for c in fk["from_cols"])
        to_cols     = ", ".join(_quote_ident(c.lower()) for c in fk["to_cols"])
        on_delete   = fk.get("on_delete", "NO ACTION").upper()
        on_update   = fk.get("on_update", "NO ACTION").upper()

        stmt = (
            f'ALTER TABLE "{from_schema}"."{from_table}"\n'
            f'    ADD CONSTRAINT "{fk_name}"\n'
            f'    FOREIGN KEY ({from_cols})\n'
            f'    REFERENCES "{to_schema}"."{to_table}" ({to_cols})\n'
            f'    ON DELETE {on_delete}\n'
            f'    ON UPDATE {on_update};'
        )
        stmts.append(stmt)
    return stmts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_type(col: dict, type_map: dict) -> str:
    """Map a source column dict to a PostgreSQL type string."""
    type_key = col["type_name"]
    # Normalise case for lookup (Oracle types are upper, others are lower)
    # Try exact match first, then case-insensitive
    mapping = type_map.get(type_key) or type_map.get(type_key.upper()) or type_map.get(type_key.lower())
    if mapping is None:
        return "text"   # safe fallback
    if callable(mapping):
        return mapping(col)
    return mapping


def _normalise_default(expr: str | None) -> str | None:
    """Convert a source-DB default expression to PostgreSQL equivalent."""
    if expr is None:
        return None
    val = expr.strip()
    if not val:          # empty string default → no default clause
        return None

    # Strip MSSQL bracket identifiers first (e.g. ([dbo].[fn]()) → (dbo.fn()))
    # Recurse after stripping so patterns can match without brackets
    stripped = re.sub(r'\[([^\]]+)\]', r'\1', val)
    if stripped != val:
        return _normalise_default(stripped)

    # Apply all _DEFAULT_PATTERNS in sequence using sub() for each.
    # Anchored patterns (^...$) only match the full string; unanchored ones
    # substitute inline.  Multiple patterns can chain: each sees the result
    # of the previous transformation.
    for pattern, replacement in _DEFAULT_PATTERNS:
        new_val = pattern.sub(replacement, val)
        if new_val != val:
            val = new_val

    # Drop MSSQL string-concatenation defaults — they use + (not ||) and often
    # reference SQL Server-specific functions.  Cannot be translated portably.
    # Detect by: contains string literal AND contains + outside quotes.
    if "'" in val and '+' in val:
        return None

    # Drop MSSQL CONVERT(type, non-string) defaults — cannot translate portably.
    # e.g. CONVERT(datetime, (0)) or CONVERT(datetime, someExpr)
    if re.search(r'CONVERT\s*\(\s*\w+', val, re.I):
        return None

    # Column-reference defaults like "(other_col)" are not valid in PG
    if re.match(r'^\([a-z_][a-z0-9_]*\)$', val, re.I):
        return None

    # Bare unquoted string literals (MySQL INFORMATION_SCHEMA omits the quotes)
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_\- ]*$", val) and val.lower() not in (
        "null", "true", "false", "current_timestamp", "current_date", "current_time",
        "current_user", "now"
    ):
        return f"'{val}'"

    return val.lower()


def _strip_mssql_brackets(name: str) -> str:
    """Strip MSSQL T-SQL bracket identifiers: [col_name] → col_name."""
    name = name.strip()
    if name.startswith('[') and name.endswith(']'):
        return name[1:-1]
    return name


def _quote_ident(name: str) -> str:
    """Double-quote an identifier if it's a PG reserved word or contains special chars."""
    if name.lower() in _PG_RESERVED or not re.match(r'^[a-z_][a-z0-9_]*$', name.lower()):
        return f'"{name.lower()}"'
    return name.lower()


# MSSQL function substitutions safe to apply inside computed expressions
_COMPUTED_FUNC_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\blen\s*\(', re.IGNORECASE),    'length('),
    (re.compile(r'\bisnull\s*\(', re.IGNORECASE), 'coalesce('),
    (re.compile(r'\bgetdate\s*\(\s*\)', re.IGNORECASE), 'now()'),
]

# Patterns that indicate an expression is NOT safely convertible at table-create
# time (MSSQL-specific functions / cross-schema UDF calls that either have no PG
# equivalent or depend on objects not yet deployed).
_UNCONVERTIBLE_EXPR_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bCONVERT\s*\(',      re.IGNORECASE),  # CONVERT(type, expr, style)
    re.compile(r'\bTRY_CONVERT\s*\(', re.IGNORECASE),  # TRY_CONVERT(type, expr)
    re.compile(r'\bTRY_CAST\s*\(',    re.IGNORECASE),  # TRY_CAST
    # UDF calls (schema.func) — not deployed yet when table is created
    re.compile(r'\bdbo\.\w+\s*\(',   re.IGNORECASE),
    # MSSQL string concatenation with +  ('str' + expr or expr + 'str')
    # PG requires || for string concat; + only works for numeric types.
    re.compile(r"[)\w]\s*\+\s*'"),   # word/paren + 'string'
    re.compile(r"'\s*\+\s*[(\w]"),   # 'string' + word/paren
]


def _mssql_expr_is_convertible(expr: str) -> bool:
    """Return False if the (post-bracket-substitution) expr contains MSSQL-specific
    constructs that cannot be executed inside a PG GENERATED column at table-create
    time — either because they have no PG equivalent (CONVERT/TRY_CONVERT) or because
    they reference UDFs that are deployed after tables."""
    for pat in _UNCONVERTIBLE_EXPR_PATTERNS:
        if pat.search(expr):
            return False
    return True


def _mssql_expr_to_pg(expr: str) -> str:
    """Convert an MSSQL computed-column expression to PostgreSQL.

    Applies transformations in order:
      1. Strip outer parentheses SQL Server adds:  ([in]-[out])  →  [in]-[out]
      2. Replace every [bracket_identifier] with a properly double-quoted PG
         identifier, quoting reserved words automatically.
         e.g. [in] → "in", [out] → out, [Amount] → amount
      3. Apply safe function substitutions (len→length, isnull→coalesce, etc.)

    Callers should check _mssql_expr_is_convertible() first; if that returns
    False the expression contains MSSQL-specific constructs (CONVERT, UDFs)
    that cannot be represented as a PG GENERATED column.
    """
    expr = expr.strip()
    # Strip a single wrapping pair of parentheses SQL Server adds
    if expr.startswith('(') and expr.endswith(')'):
        expr = expr[1:-1].strip()
    # Replace every [identifier] with a properly quoted PG identifier
    expr = re.sub(
        r'\[([^\]]+)\]',
        lambda m: _quote_ident(m.group(1)),
        expr,
    )
    # Apply safe function substitutions
    for pat, replacement in _COMPUTED_FUNC_SUBS:
        expr = pat.sub(replacement, expr)
    return expr


def _safe_index_name(original: str, max_len: int = 63) -> str:
    """Return a PostgreSQL-safe index name within the 63-char limit."""
    lowered = original.lower()
    if len(lowered) <= max_len:
        return lowered
    # Truncate and append a short hash to avoid collisions
    import hashlib
    suffix = hashlib.md5(original.encode()).hexdigest()[:6]
    return lowered[:max_len - 7] + "_" + suffix
