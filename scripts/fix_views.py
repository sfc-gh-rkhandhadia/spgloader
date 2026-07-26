#!/usr/bin/env python3
"""
fix_views.py — Apply mapping-document-driven corrections to converted view SQL files.

Reads  view-fixes.yaml  and applies:
  1. Pattern fixes  (single-quoted aliases, column= alias, bare LIMIT, etc.)
  2. Schema prefix  (adds dbo. to unqualified table references)
  3. Cross-db remaps  (e.g. dame.dbo. → dbo.)
  4. PIVOT → CTE + FILTER conversion

Input:   {work_dir}/conversion/postgres/wave_2_views/*.sql
Output:  {work_dir}/conversion/postgres/wave_2_views_fixed/*.sql
         {work_dir}/conversion/fix_report.json

Usage:
  python fix_views.py --work-dir ~/.spgloader/20260101_120000 \\
                      [--mapping ~/sko-coco/spgloader/references/fix-mappings/view-fixes.yaml]
"""
import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))
from spgloader.rules import get_loader as _get_rules
_rules = _get_rules(SKILL_DIR)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_balanced(text: str, start: int) -> str:
    """Extract content of parenthesised block starting at text[start]='('.
    Returns the content inside the outermost parens (exclusive)."""
    assert text[start] == "(", f"Expected '(' at pos {start}, got {text[start]!r}"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    raise ValueError("Unbalanced parentheses")


def _pg_identifier(name: str) -> str:
    """Return a safe PostgreSQL identifier.  Quotes names containing non-word
    characters (e.g. 'file_number.1' → '"file_number.1"')."""
    if re.match(r"^[a-z_][a-z0-9_]*$", name):
        return name
    return f'"{name}"'


# ---------------------------------------------------------------------------
# Pass 1 — Pattern fixes
# ---------------------------------------------------------------------------

def fix_patterns(sql: str, cfg: dict) -> tuple[str, list[str]]:
    """Apply pattern-based fixes. Returns (fixed_sql, [descriptions])."""
    fixes = []

    if cfg.get("single_quoted_alias"):
        # AS 'Name'  →  AS name  (lowercase, no quotes for simple identifiers)
        # Using lowercase avoids PG case-sensitivity issues when outer queries
        # reference the alias without quoting (PG folds unquoted idents to lower).
        def _lc_alias(m: re.Match) -> str:
            alias = m.group(1)
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", alias):
                return f"AS {alias.lower()}"
            return f'AS "{alias.lower()}"'

        new, n = re.subn(r"\bAS\s+'([^']+)'", _lc_alias, sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"single_quoted_alias: {n} occurrences")

    if cfg.get("column_equals_alias"):
        # T-SQL: alias = expression  →  expression AS alias  (line-start form)
        def _swap(m: re.Match) -> str:
            indent = m.group(1)
            alias = m.group(2)
            expr = m.group(3).rstrip()
            return f"{indent}{expr} AS {alias}"

        new, n = re.subn(
            r"(?m)^([ \t]*)(\b[a-zA-Z_][a-zA-Z0-9_]*)\s*=(?!=|>|<)\s*([^\n,=<>!][^\n,]*)",
            _swap, sql,
        )
        if n:
            sql = new
            fixes.append(f"column_equals_alias: {n} occurrences")

    if cfg.get("embedded_column_equals_alias"):
        # T-SQL alias = expr embedded after a comma (not at line start)
        # e.g.  pathid, datasize=DATALENGTH(...)  →  pathid, DATALENGTH(...) AS datasize
        def _swap_embedded(m: re.Match) -> str:
            alias = m.group(1)
            expr = m.group(2).rstrip()
            return f"{expr} AS {alias}"

        new, n = re.subn(
            r"(?<=,\s)(\b[a-zA-Z_][a-zA-Z0-9_]*)\s*=(?!=|>|<)\s*([^\n,=<>!][^\n,]*)",
            _swap_embedded, sql,
        )
        if n:
            sql = new
            fixes.append(f"embedded_column_equals_alias: {n} occurrences")

    if cfg.get("multi_word_alias"):
        # Uses an explicit list of known multi-word aliases from the yaml (known_multi_word_aliases).
        # Safer than regex-based detection which risks false positives on SQL keywords.
        known = cfg.get("known_multi_word_aliases", [])
        total = 0
        for alias in known:
            alias_lower = alias.lower()
            # Replace bare alias (case-insensitive) with quoted lowercase form
            new, n = re.subn(re.escape(alias), f'"{alias_lower}"', sql, flags=re.IGNORECASE)
            if n:
                sql = new
                total += n
        if total:
            fixes.append(f"multi_word_alias: {total} occurrences")

    if cfg.get("bare_limit"):
        new, n = re.subn(r"\bLIMIT\s*;", ";", sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"bare_limit: {n} trailing LIMIT removed")
        new, n = re.subn(r"\bLIMIT\s*$", "", sql, flags=re.IGNORECASE | re.MULTILINE)
        if n:
            sql = new
            fixes.append(f"bare_limit_eol: {n} removed")

    if cfg.get("tsql_string_concat"):
        new, n = re.subn(r"\)\s*\+\s*'", ") || '", sql)
        if n:
            sql = new
            fixes.append(f"tsql_string_concat )+'str': {n}")
        new, n = re.subn(r"'\s*\+\s*\(", "' || (", sql)
        if n:
            sql = new
            fixes.append(f"tsql_string_concat 'str'+(: {n}")
        new, n = re.subn(r"'\s*\+\s*'", "' || '", sql)
        if n:
            sql = new
            fixes.append(f"tsql_string_concat 'a'+'b': {n}")

    if cfg.get("now_date_cast"):
        new, n = re.subn(
            r"CAST\s*\(\s*convert\s*\(\s*date\s*,\s*NOW\s*\(\s*AS\s+TEXT\s*\)\s*\)\s*\)",
            "CURRENT_DATE", sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"now_date_cast: {n} occurrences")
        new, n = re.subn(
            r"\bConvert\s*\(\s*varchar\s*\(\d+\)\s*,\s*'([^']*)'\s*\)",
            r"'\1'", sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"convert_varchar_literal: {n} occurrences")

    if cfg.get("datepart"):
        # DATEPART(unit, expr) → EXTRACT(unit FROM expr)
        # Unit map loaded from date-units.yaml instead of being hardcoded
        _dp_unit_map = _rules.datepart_units()  # {abbrev: PG_KEYWORD}

        def _datepart(m: re.Match) -> str:
            unit = m.group(1).upper()
            expr = m.group(2).strip()
            pg_unit = _dp_unit_map.get(unit, unit)
            return f"EXTRACT({pg_unit} FROM {expr})"

        new, n = re.subn(
            r"\bDATEPART\s*\(\s*(\w+)\s*,\s*([^)]+)\)",
            _datepart, sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"datepart: {n} occurrences")

    if cfg.get("now_to_current_timestamp"):
        # Swap NOW() → CURRENT_TIMESTAMP before DATEADD conversion so the
        # DATEADD regex ([^)]+) isn't tripped up by NOW()'s parentheses.
        new, n = re.subn(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"now_to_current_timestamp: {n}")

    if cfg.get("current_timestamp_minus_int"):
        # CURRENT_TIMESTAMP - N  →  CURRENT_TIMESTAMP - INTERVAL 'N days'
        new, n = re.subn(
            r"\bCURRENT_TIMESTAMP\s*-\s*(\d+)\b",
            lambda m: f"CURRENT_TIMESTAMP - INTERVAL '{m.group(1)} days'",
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"current_timestamp_minus_int: {n}")

    if cfg.get("datetrunc_idiom"):
        # DATEADD(unit, DATEDIFF(unit, 0, expr) [+/- offset], 0)  →  DATE_TRUNC + offset
        # Handles offset variants like: DATEDIFF(month,0,expr)-1, DATEDIFF(m,0,expr)-13

        def _datetrunc_with_offset(m: re.Match) -> str:
            unit = m.group(1).lower()
            # Reuse the dateadd_units map from date-units.yaml (lowercase lookup)
            _da_map = {k.lower(): v for k, v in _rules.dateadd_units().items()}
            pg_unit = _da_map.get(unit, unit)
            expr = m.group(2).strip()
            offset_str = (m.group(3) or "").replace(" ", "")
            base = f"DATE_TRUNC('{pg_unit}', {expr})"
            if not offset_str:
                return base
            # offset_str like "-1", "+5", "-13"
            try:
                offset_val = int(offset_str)
                if offset_val < 0:
                    return f"({base} - {abs(offset_val)} * INTERVAL '1 {pg_unit}')"
                elif offset_val > 0:
                    return f"({base} + {offset_val} * INTERVAL '1 {pg_unit}')"
                return base
            except ValueError:
                return base

        # Main idiom: DATEADD(unit, DATEDIFF(unit, 0, expr) [+/- N], 0)
        new, n = re.subn(
            r"\bDATEADD\s*\(\s*(\w+)\s*,\s*DATEDIFF\s*\(\s*\1\s*,\s*0\s*,\s*([^)]+)\)"
            r"(\s*[+-]\s*\d+)?\s*,\s*0\s*\)",
            _datetrunc_with_offset, sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"datetrunc_idiom: {n}")
        # Prior-period variant: DATEADD(unit, DATEDIFF(unit, -1, expr)-1, -1)
        new, n = re.subn(
            r"\bDATEADD\s*\(\s*(\w+)\s*,\s*DATEDIFF\s*\(\s*\1\s*,\s*-1\s*,\s*([^)]+)\)"
            r"\s*-\s*1\s*,\s*-1\s*\)",
            lambda m: (
                f"(DATE_TRUNC('{m.group(1).lower()}', {m.group(2).strip()}) "
                f"- INTERVAL '1 {m.group(1).lower()}')"
            ),
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"datetrunc_idiom (prior period): {n}")

    if cfg.get("eomonth"):
        # EOMONTH(expr) → last day of the month for expr
        # PG: (DATE_TRUNC('month', expr) + INTERVAL '1 month' - INTERVAL '1 day')::DATE
        new, n = re.subn(
            r"\bEOMONTH\s*\(\s*([^)]+)\s*\)",
            lambda m: f"(DATE_TRUNC('month', {m.group(1).strip()}) + INTERVAL '1 month' - INTERVAL '1 day')::DATE",
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"eomonth: {n}")

    if cfg.get("cast_interval_to_numeric"):
        # CAST(col1 - col2 AS REAL/FLOAT/NUMERIC) → EXTRACT(EPOCH FROM (...))/86400
        # In PG, date - date returns interval; can't cast directly to real.
        # Matches qualified column refs: alias.col - alias.col
        new, n = re.subn(
            r"\bCAST\s*\(\s*(\w+(?:\.\w+)*)\s*-\s*(\w+(?:\.\w+)*)\s+AS\s+(?:REAL|FLOAT8?|NUMERIC)\s*\)",
            lambda m: f"(EXTRACT(EPOCH FROM ({m.group(1).strip()} - {m.group(2).strip()})) / 86400)",
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"cast_interval_to_numeric: {n}")

    if cfg.get("datalength"):
        # DATALENGTH(expr) → OCTET_LENGTH(expr)
        # SQL Server DATALENGTH counts bytes; PostgreSQL OCTET_LENGTH is the equivalent.
        new, n = re.subn(r"\bDATALENGTH\s*\(", "OCTET_LENGTH(", sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"datalength: {n}")

    if cfg.get("dateadd"):
        # DATEADD(unit, n, expr) → (expr + n * INTERVAL '1 unit')
        # Unit map loaded from date-units.yaml
        _unit_map = _rules.dateadd_units()  # {abbrev: pg_interval_unit}

        def _dateadd(m: re.Match) -> str:
            unit = m.group(1).upper()
            n_str = m.group(2).strip()
            expr = m.group(3).strip()
            pg_unit = _unit_map.get(unit, unit.lower())
            # If n is negative: expr - |n| * INTERVAL
            try:
                n_val = int(n_str)
                if n_val < 0:
                    return f"({expr} - {abs(n_val)} * INTERVAL '1 {pg_unit}')"
                return f"({expr} + {n_val} * INTERVAL '1 {pg_unit}')"
            except ValueError:
                return f"({expr} + ({n_str}) * INTERVAL '1 {pg_unit}')"

        new, n = re.subn(
            r"\bDATEADD\s*\(\s*(\w+)\s*,\s*(-?\s*\d+|\w+(?:\.\w+)*)\s*,\s*([^)]+)\)",
            _dateadd, sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"dateadd: {n} occurrences")

    if cfg.get("convert_typed"):
        # CONVERT(VarChar(N), expr) → CAST(expr AS VARCHAR(N))
        # The basic CONVERT pattern missed types with parentheses
        new, n = re.subn(
            r"\bCONVERT\s*\(\s*([A-Za-z]+)\s*\(\s*(\d+)\s*\)\s*,\s*([^)]+)\)",
            r"CAST(\3 AS \1(\2))", sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"convert_typed: {n} CONVERT(Type(N),expr) fixed")

    if cfg.get("cast_format_code"):
        # CAST(expr,NNN AS TEXT) → CAST(expr AS DATE)
        # SQL Server CONVERT(datetime, expr, 101) was converted to CAST(expr,101 AS TEXT)
        new, n = re.subn(
            r"\bCAST\s*\(\s*([^,\n]+?)\s*,\s*\d+\s+AS\s+\w+\s*\)",
            r"CAST(\1 AS DATE)", sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"cast_format_code: {n} format codes stripped")

    if cfg.get("datediff_in_cast"):
        # Fix: CAST(DateDiff(unit, a, b AS TEXT)) →  CAST(... b) AS TEXT)
        # Earlier CONVERT→CAST put "AS TEXT" inside the last arg of DATEDIFF.
        # Convert DATEDIFF(HH, a, b) to EXTRACT(EPOCH FROM (b-a))/3600
        # Divisor map loaded from date-units.yaml
        _datediff_unit = _rules.datediff_divisors()  # {abbrev: divisor_expr}

        def _fix_datediff_in_cast(m: re.Match) -> str:
            unit = m.group(1).upper()
            col_a = m.group(2).strip()
            col_b = m.group(3).strip()
            divisor = _datediff_unit.get(unit, "")
            epoch_expr = f"EXTRACT(EPOCH FROM ({col_b} - {col_a})){(' ' + divisor) if divisor else ''}"
            # Return NUMERIC (not TEXT) so arithmetic on the result still works
            return f"({epoch_expr}::NUMERIC)"

        new, n = re.subn(
            # Matches CAST(DATEDIFF(unit, a, b AS TEXT)) -- note two closing parens:
            # the first ) closes DATEDIFF, the second ) closes CAST
            r"\bCAST\s*\(\s*(?:Date)?Diff\s*\(\s*(\w+)\s*,\s*([^,\n]+?)\s*,\s*([^)\n]+?)\s+AS\s+\w+\s*\)\s*\)",
            _fix_datediff_in_cast, sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"datediff_in_cast: {n} fixed")

    if cfg.get("objectkey_cast"):
        # addresses.objectkey is VARCHAR; integer ID columns → type error
        # id_cols list loaded from view-fixes.yaml (objectkey_cast_id_cols) — project-specific
        id_col_list = cfg.get("objectkey_cast_id_cols", ["orderid", "orderitemid", "clientid", "vendorid", "vendorfirmid"])
        id_cols = "(?:" + "|".join(re.escape(c) for c in id_col_list) + ")"
        # Direction 1: objectkey = alias.int_col
        new, n = re.subn(
            rf"\b((?:\w+\.)?objectkey)\s*=\s*(\w+\.{id_cols})\b",
            r"\1 = CAST(\2 AS TEXT)",
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"objectkey_cast (objectkey=id): {n}")
        # Direction 2: alias.int_col = objectkey
        new, n = re.subn(
            rf"\b(\w+\.{id_cols})\s*=\s*((?:\w+\.)?objectkey)\b",
            r"CAST(\1 AS TEXT) = \2",
            sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"objectkey_cast (id=objectkey): {n}")

    if cfg.get("now_minus_int"):
        # NOW()-N  →  NOW() - INTERVAL 'N days'
        # CURRENT_DATE - N stays as-is (PG DATE - integer is valid)
        def _now_minus(m: re.Match) -> str:
            n_val = m.group(1).strip()
            return f"NOW() - INTERVAL '{n_val} days'"

        new, n = re.subn(
            r"\bNOW\s*\(\s*\)\s*-\s*(\d+)\b",
            _now_minus, sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"now_minus_int: {n} occurrences")

    if cfg.get("vendorfirm_contact_cols"):
        # v.ContactFirstName / v.ContactLastName → vf.ContactFirstName / vf.ContactLastName
        # These columns exist in VendorFirms (aliased vf), not Vendors (aliased v)
        new, n = re.subn(
            r"\bv\.(contactfirstname|contactlastname)\b",
            r"vf.\1", sql, flags=re.IGNORECASE,
        )
        if n:
            sql = new
            fixes.append(f"vendorfirm_contact_cols: {n} occurrences")

    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 2 — Schema prefix
# ---------------------------------------------------------------------------

def fix_schema_prefix(sql: str, schema: str, tables: list[str]) -> tuple[str, list[str]]:
    """Add schema. prefix to unqualified table references in FROM/JOIN clauses."""
    fixes = []
    for table in tables:
        # Match table name after FROM or JOIN, not already preceded by a schema qualifier
        # Negative look-behind: not preceded by dot (which would mean schema.table)
        pattern = rf"(?<!\.)(?i:\b(?:FROM|JOIN)\s+)(?i:{re.escape(table)})\b"

        def _add_schema(m: re.Match, _t: str = table, _s: str = schema) -> str:
            # Preserve the FROM/JOIN keyword and original casing
            kw = m.group(0).split()[0]  # FROM or JOIN
            # Preserve spacing
            ws = re.match(rf"(?i:{re.escape(kw)})\s*", m.group(0)).group(0)[len(kw):]
            return f"{kw}{ws}{_s}.{_t}"

        new, n = re.subn(pattern, _add_schema, sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"schema_prefix {schema}.{table}: {n}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 3 — Cross-database remaps
# ---------------------------------------------------------------------------

def fix_cross_db(sql: str, remaps: list[dict]) -> tuple[str, list[str]]:
    fixes = []
    for remap in remaps:
        frm = remap["from_prefix"]
        to = remap["to_prefix"]
        new, n = re.subn(re.escape(frm), to, sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"cross_db_remap {frm!r}→{to!r}: {n}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 4 — PIVOT → CTE + FILTER
# ---------------------------------------------------------------------------

def _parse_inner_select_cols(inner_sql: str, value_col: str, key_col: str) -> list[str]:
    """Extract non-pivot columns from the inner SELECT list."""
    # Find the column list between SELECT and FROM
    m = re.search(r"SELECT\s+(.*?)\s+FROM\s", inner_sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    col_list = m.group(1)
    raw_cols = [c.strip() for c in col_list.split(",") if c.strip()]
    result = []
    for col in raw_cols:
        # Get the bare column name (last word, handling alias.col notation)
        bare = col.split(".")[-1].strip()
        bare_lower = bare.lower()
        if bare_lower not in (value_col.lower(), key_col.lower()):
            result.append(bare)
    return result


def convert_pivot_to_cte(sql: str, pivot_rules: dict) -> tuple[str, bool]:
    """Convert a MSSQL PIVOT clause to a PostgreSQL CTE + FILTER aggregate.

    Returns (converted_sql, was_converted).
    """
    agg_func = pivot_rules.get("agg_function", "MAX")
    value_col = pivot_rules.get("value_column", "Value")
    key_col = pivot_rules.get("key_column", "FieldName")
    inner_tables: dict = pivot_rules.get("inner_tables", {})

    # ── 1. Find the PIVOT keyword ──────────────────────────────────────────
    pivot_m = re.search(r"\bPIVOT\s*\(", sql, re.IGNORECASE)
    if not pivot_m:
        return sql, False

    # ── 2. Extract the outer SELECT * FROM (...) x block ──────────────────
    # The structure is always:  SELECT * FROM ( ... ) x   PIVOT (...) x ;
    outer_from_m = re.search(r"SELECT\s+\*\s+FROM\s*\(", sql, re.IGNORECASE | re.DOTALL)
    if not outer_from_m:
        return sql, False

    open_paren = sql.index("(", outer_from_m.start())
    try:
        inner_body = _extract_balanced(sql, open_paren)
    except ValueError:
        return sql, False  # unbalanced parens; skip

    # ── 3. Parse the PIVOT clause ──────────────────────────────────────────
    pivot_open = sql.index("(", pivot_m.start())
    try:
        pivot_body = _extract_balanced(sql, pivot_open)
    except ValueError:
        return sql, False

    # pivot_body = "MAX(Value) FOR FieldName IN (col1, col2, ...)"
    pivot_parse = re.match(
        r"\s*(\w+)\s*\(\s*(\w+)\s*\)\s*FOR\s+(\w+)\s+IN\s*\((.+)\)",
        pivot_body, re.IGNORECASE | re.DOTALL,
    )
    if not pivot_parse:
        return sql, False

    _agg_func = pivot_parse.group(1)
    _agg_col = pivot_parse.group(2)
    _key_col = pivot_parse.group(3)
    col_list_raw = pivot_parse.group(4)

    # ── 4. Parse pivot column names ────────────────────────────────────────
    pivot_cols = [c.strip().strip('"\'') for c in col_list_raw.split(",") if c.strip()]

    # ── 5. Fix the inner query ─────────────────────────────────────────────
    inner_fixed = inner_body
    # Replace unqualified table names with their qualified forms.
    # Negative lookbehind for '.' prevents double-qualifying a table that
    # the schema-prefix pass already qualified (e.g. avoids dbo.dbo.orderitems).
    for unqualified, qualified in inner_tables.items():
        inner_fixed = re.sub(
            rf"(?<!\.)\b{re.escape(unqualified)}\b",
            qualified, inner_fixed, flags=re.IGNORECASE,
        )
    # Fix T-SQL string concat: ) + '.'  →  ) || '.'
    inner_fixed = re.sub(r"\)\s*\+\s*'", ") || '", inner_fixed)
    inner_fixed = re.sub(r"'\s*\+\s*\(", "' || (", inner_fixed)

    # ── 6. Detect grouping columns ─────────────────────────────────────────
    group_cols = _parse_inner_select_cols(inner_fixed, _agg_col, _key_col)
    if not group_cols:
        # Fallback to documented defaults
        group_cols = [c for c in pivot_rules.get("group_by_cols", [_key_col])]

    # ── 7. Extract the CREATE OR REPLACE VIEW header ───────────────────────
    view_hdr_m = re.match(
        r"(.*?CREATE\s+OR\s+REPLACE\s+VIEW\s+\S+\s+(?:AS\s+)?)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    view_header = view_hdr_m.group(1) if view_hdr_m else ""
    # Strip EWI comment lines from header (they'll be re-prepended later)
    comments = "\n".join(l for l in view_header.splitlines() if l.startswith("--"))
    view_header_clean = re.sub(r"^--.*\n", "", view_header, flags=re.MULTILINE)

    # ── 8. Build FILTER aggregates ─────────────────────────────────────────
    filter_lines = []
    for col in pivot_cols:
        pg_col = _pg_identifier(col)
        filter_lines.append(
            f"    {_agg_func}({_agg_col}) FILTER (WHERE {_key_col} = '{col}') AS {pg_col}"
        )

    group_by_clause = ",\n    ".join(group_cols)
    select_cols = ",\n    ".join(group_cols) + (",\n" if filter_lines else "")

    cte_sql = (
        f"{comments}\n{view_header_clean}"
        f"WITH src AS ({inner_fixed}\n)\n"
        f"SELECT\n    {select_cols}"
        + ",\n".join(filter_lines)
        + f"\nFROM src\n"
        f"GROUP BY\n    {group_by_clause};\n"
    )

    return cte_sql, True


# ---------------------------------------------------------------------------
# Workspace catalog helpers
# ---------------------------------------------------------------------------

def _detect_source_schema(work_dir: Path) -> str:
    """Infer the source schema from ddl_objects.json.  Returns 'public' as fallback."""
    ddl_path = work_dir / "ddl_objects.json"
    if not ddl_path.exists():
        return "public"
    try:
        data = json.loads(ddl_path.read_text(encoding="utf-8"))
    except Exception:
        return "public"
    skip = {"", "sys", "information_schema", "guest", "public"}
    for obj in data:
        schema = obj.get("schema", "").strip("[").rstrip("]").lower()
        if schema and schema not in skip:
            return schema
    return "public"


def _auto_detect_unqualified_tables(sql: str, known_tables: set[str]) -> list[str]:
    """Return table names that appear unqualified in FROM/JOIN but exist in known_tables."""
    found: set[str] = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+(\w+)\b", sql, re.IGNORECASE):
        name = m.group(1).lower()
        # Skip if the match is already preceded by a dot (schema.table)
        start = m.start(1)
        if start > 0 and sql[start - 1] == ".":
            continue
        if name in known_tables:
            found.add(name)
    return list(found)


def _load_known_tables(work_dir: Path) -> set[str]:
    """Return a set of lowercase table names from ddl_objects.json."""
    ddl_path = work_dir / "ddl_objects.json"
    if not ddl_path.exists():
        return set()
    try:
        data = json.loads(ddl_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {
        obj["name"].strip("[").rstrip("]").lower()
        for obj in data
        if obj.get("type") in ("table", "view")
    }


def _resolve_schema_prefix_tables(
    sp_cfg: dict, sql: str, known_tables: set[str]
) -> list[str]:
    """Return the list of tables to prefix, combining static config and auto-detection.

    When sp_cfg['auto_detect'] is true (or no static 'tables' list is given),
    unqualified table references in *sql* that exist in *known_tables* are
    detected automatically.  Project-specific 'extra_tables' are appended.
    """
    static = sp_cfg.get("tables", [])
    if static:
        # Legacy: explicit list provided — use it as-is (backwards compatible)
        return static

    if not sp_cfg.get("auto_detect", True):
        return []

    # Auto-detect: find unqualified FROM/JOIN references that are known tables
    found: set[str] = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+(\w+)\b", sql, re.IGNORECASE):
        name = m.group(1).lower()
        start = m.start(1)
        if start > 0 and sql[start - 1] == ".":
            continue  # already qualified
        if name in known_tables:
            found.add(name)

    extra = [t.lower() for t in sp_cfg.get("extra_tables", [])]
    return sorted(found | set(extra))


def fix_file(
    sql: str,
    filename: str,
    mapping: dict,
    schema: str = "",
    known_tables: set[str] | None = None,
) -> tuple[str, list[str], bool]:
    """Apply all fixes to a single view file.

    Returns (fixed_sql, list_of_fix_descriptions, was_pivot_converted).
    """
    fixes = []
    was_pivot = False

    # Pass 4 — PIVOT conversion runs FIRST.
    # The PIVOT inner-query structure contains FROM/JOIN keywords that
    # the multi_word_alias and column_equals_alias patterns would garble.
    # PIVOT conversion extracts and rewrites the inner query itself.
    if re.search(r"\bPIVOT\b", sql, re.IGNORECASE):
        pivot_rules = mapping.get("pivot_rules", {})
        converted, ok = convert_pivot_to_cte(sql, pivot_rules)
        if ok:
            sql = converted
            was_pivot = True
            fixes.append("pivot_to_cte: PIVOT converted to CTE + FILTER")
        else:
            sql = f"-- FIX-REQUIRED: PIVOT — could not auto-parse, needs manual rewrite\n{sql}"
            fixes.append("pivot_to_cte: FAILED — manual rewrite needed")
        # For PIVOT views skip multi_word_alias / column_equals_alias
        # (PIVOT views have no multi-word aliases; converting them risks garbling CTE structure).
        # Still apply schema prefix and cross-db remaps for any non-CTE parts.
        sp = mapping.get("schema_prefix", {})
        # Resolve schema: prefer runtime-detected schema over mapping value
        eff_schema = schema or sp.get("schema", "public")
        tables = _resolve_schema_prefix_tables(sp, sql, known_tables or set())
        if tables:
            sql, f = fix_schema_prefix(sql, eff_schema, tables)
            fixes.extend(f)
        remaps = mapping.get("cross_db_remaps", [])
        if remaps:
            sql, f = fix_cross_db(sql, remaps)
            fixes.extend(f)
        return sql, fixes, was_pivot

    # For non-PIVOT views: apply all pattern fixes, then schema/cross-db.

    # Pass 1 — pattern fixes
    # Merge top-level known_multi_word_aliases into the pattern_fixes cfg
    # so fix_patterns() can access them without needing the full mapping.
    pattern_cfg = dict(mapping.get("pattern_fixes", {}))
    pattern_cfg["known_multi_word_aliases"] = mapping.get("known_multi_word_aliases", [])
    sql, f = fix_patterns(sql, pattern_cfg)
    fixes.extend(f)

    # Pass 2 — schema prefix
    sp = mapping.get("schema_prefix", {})
    eff_schema = schema or sp.get("schema", "public")
    tables = _resolve_schema_prefix_tables(sp, sql, known_tables or set())
    if tables:
        sql, f = fix_schema_prefix(sql, eff_schema, tables)
        fixes.extend(f)

    # Pass 3 — cross-db remaps
    remaps = mapping.get("cross_db_remaps", [])
    if remaps:
        sql, f = fix_cross_db(sql, remaps)
        fixes.extend(f)

    return sql, fixes, was_pivot


def main():
    parser = argparse.ArgumentParser(
        description="Apply mapping-document fixes to converted view SQL files"
    )
    parser.add_argument(
        "--work-dir", required=True,
        help="spgloader workspace directory (e.g. ~/.spgloader/20260101_120000)",
    )
    parser.add_argument(
        "--mapping",
        default=str(Path(__file__).parent.parent / "references" / "fix-mappings" / "view-fixes.yaml"),
        help="Path to view-fixes.yaml mapping document",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    mapping_path = Path(args.mapping).expanduser()

    if not mapping_path.exists():
        print(f"ERROR: mapping file not found: {mapping_path}", file=sys.stderr)
        sys.exit(1)

    import yaml  # pyyaml is a project dependency
    mapping = yaml.safe_load(mapping_path.read_text())

    # Detect source schema and known table names from the workspace.
    # These are passed into fix_file so schema_prefix.auto_detect works
    # without any project-specific names in view-fixes.yaml.
    schema = _detect_source_schema(work_dir)
    known_tables = _load_known_tables(work_dir)
    print(f"Source schema   : {schema}")
    print(f"Known tables    : {len(known_tables)} loaded from workspace")

    input_dir = work_dir / "conversion" / "postgres" / "wave_2_views"
    output_dir = work_dir / "conversion" / "postgres" / "wave_2_views_fixed"

    if not input_dir.exists():
        print(f"ERROR: input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    view_files = sorted(input_dir.glob("*.sql"))
    print(f"Processing {len(view_files)} view files from {input_dir}")
    print(f"Output → {output_dir}\n")

    report = {"succeeded": [], "pivots_converted": [], "failed": [], "fix_details": {}}
    total_fixes = 0

    for f in view_files:
        sql = f.read_text(encoding="utf-8", errors="ignore")
        try:
            fixed_sql, fixes, was_pivot = fix_file(
                sql, f.name, mapping, schema=schema, known_tables=known_tables
            )
            out_path = output_dir / f.name
            out_path.write_text(fixed_sql, encoding="utf-8")
            report["fix_details"][f.name] = {"fixes": fixes, "pivot": was_pivot}
            if was_pivot:
                report["pivots_converted"].append(f.name)
            report["succeeded"].append(f.name)
            total_fixes += len(fixes)
            pivot_tag = " [PIVOT→CTE]" if was_pivot else ""
            fix_summary = f"  {len(fixes)} fix(es)" if fixes else "  no fixes applied"
            print(f"  OK{pivot_tag}  {f.name}{fix_summary}")
        except Exception as e:
            report["failed"].append({"file": f.name, "error": str(e)})
            print(f"  FAIL {f.name}: {e}")

    # Write report
    report_path = work_dir / "conversion" / "fix_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(f"\n{'='*60}")
    print(f"Files processed : {len(view_files)}")
    print(f"Succeeded       : {len(report['succeeded'])}")
    print(f"PIVOT→CTE       : {len(report['pivots_converted'])}")
    print(f"Failed          : {len(report['failed'])}")
    print(f"Total fixes     : {total_fixes}")
    print(f"Fix report      : {report_path}")

    if report["failed"]:
        print("\nFailed files:")
        for item in report["failed"]:
            print(f"  {item['file']}: {item['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
