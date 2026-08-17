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
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))
from spgloader.rules import get_loader as _get_rules
_rules = _get_rules(SKILL_DIR, "mssql")  # default MSSQL; override via --source-type

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

    if cfg.get("double_quoted_alias"):
        # AS "MixedCase"  →  AS mixedcase  (double-quoted aliases create case-sensitive
        # PG identifiers; normalise to lowercase unquoted to prevent downstream failures
        # when outer queries reference the alias without quoting).
        def _lc_dq_alias(m: re.Match) -> str:
            alias = m.group(1)
            lower = alias.lower()
            if re.match(r"^[a-z_][a-z0-9_]*$", lower):
                return f"AS {lower}"
            return f'AS "{lower}"'

        new, n = re.subn(r'\bAS\s+"([^"]+)"', _lc_dq_alias, sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"double_quoted_alias: {n} occurrences")

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
        # CONVERT(Type(N[,M]), expr) → CAST(expr AS Type(N[,M]))
        # Uses _extract_balanced to handle nested parens in expr (e.g. NUMERIC(19,4)
        # with expressions like ("t".UnitPrice*Quantity*(1-Discount)/100)).
        _conv_re = re.compile(r"\bCONVERT\s*\(", re.IGNORECASE)
        _type_re = re.compile(
            r"\s*([A-Za-z]+)\s*\(\s*(\d+(?:\s*,\s*\d+)?)\s*\)\s*,\s*",
            re.IGNORECASE,
        )
        parts, pos, n = [], 0, 0
        while True:
            m = _conv_re.search(sql, pos)
            if not m:
                parts.append(sql[pos:])
                break
            # m.end()-1 is the position of the '(' that opens CONVERT's arg list
            paren_start = m.end() - 1
            if sql[paren_start] != "(":
                parts.append(sql[pos : m.end()])
                pos = m.end()
                continue
            try:
                full_args = _extract_balanced(sql, paren_start)
            except (ValueError, AssertionError):
                parts.append(sql[pos : m.end()])
                pos = m.end()
                continue
            type_m = _type_re.match(full_args)
            if not type_m:
                # Not a Type(N,M) CONVERT — leave it for other rules
                parts.append(sql[pos : m.end()])
                pos = m.end()
                continue
            type_name  = type_m.group(1)
            type_param = re.sub(r"\s+", "", type_m.group(2))  # strip spaces: "19 , 4" → "19,4"
            expr       = full_args[type_m.end():]
            # end_pos = one past the closing ')' of CONVERT
            end_pos = paren_start + len(full_args) + 2
            parts.append(sql[pos : m.start()])
            parts.append(f"CAST({expr} AS {type_name}({type_param}))")
            pos = end_pos
            n += 1
        if n:
            sql = "".join(parts)
            fixes.append(f"convert_typed: {n} CONVERT(Type(N[,M]),expr) fixed")

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
# XML Namespace / .value() / .nodes() → XMLTABLE conversion
# ---------------------------------------------------------------------------


def convert_xml_to_xmltable(sql: str) -> tuple[str, bool]:
    """Convert MSSQL XML .value()/.nodes() with namespace declarations to
    PostgreSQL XMLTABLE with XMLNAMESPACES.

    Detects the pattern produced by convert_objects.py:
      - col.ref.value(N'declare ... namespace "uri"; (xpath)[1]', 'TYPE') AS alias
      - CROSS APPLY col.nodes(N'declare ... namespace "uri"; /path') AS alias(ref)

    Returns (converted_sql, was_converted).
    """
    # Quick check: does this SQL use the declare namespace pattern?
    if not re.search(r"declare\s+(?:default\s+element\s+)?namespace", sql, re.IGNORECASE):
        return sql, False

    # ── 1. Extract the CREATE VIEW header ────────────────────────────────────
    view_hdr_m = re.match(
        r"(.*?create\s+or\s+replace\s+view\s+(\S+)\s+as\s*)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not view_hdr_m:
        return sql, False

    view_header = view_hdr_m.group(1)
    view_name = view_hdr_m.group(2)

    # ── 2. Extract CROSS APPLY / OUTER APPLY ... .nodes() clause ───────────────
    # Pattern: (CROSS|OUTER) APPLY table.col.nodes(N'declare ... namespace ...; /XPath') AS alias(ref)
    nodes_m = re.search(
        r"(?:cross|outer)\s+apply\s+(\w+)\.(\w+)\.nodes\(\s*n?'(.*?)'\s*\)\s+as\s+(\w+)\((\w+)\)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    # Also try: .nodes() directly on unqualified column (no table alias prefix)
    if not nodes_m:
        nodes_m = re.search(
            r"(?:cross|outer)\s+apply\s+(\w+)\.nodes\(\s*n?'(.*?)'\s*\)\s+as\s+(\w+)\((\w+)\)",
            sql, re.IGNORECASE | re.DOTALL,
        )
        if nodes_m:
            # In this pattern the column IS the first group; we need to infer table alias from FROM
            xml_column = nodes_m.group(1)
            nodes_xquery = nodes_m.group(2)
            _nodes_alias = nodes_m.group(3)
            _nodes_ref = nodes_m.group(4)
            # Find table alias from the FROM clause preceding this
            from_pre = re.search(r"\bfrom\s+[\w.]+\s+(\w+)", sql[:nodes_m.start()], re.IGNORECASE)
            table_alias = from_pre.group(1) if from_pre else "t"
        else:
            # No .nodes() found — try direct .value() pattern (no row generation)
            # Pattern: col.value(N'declare ... ; xpath', 'TYPE') without any CROSS APPLY
            if re.search(r"\w+\.value\(\s*n?'declare\s+", sql, re.IGNORECASE):
                return _convert_direct_value_xml(sql, view_name)
            return sql, False
    else:
        table_alias = nodes_m.group(1)
        xml_column = nodes_m.group(2)
        nodes_xquery = nodes_m.group(3)
        _nodes_alias = nodes_m.group(4)
        _nodes_ref = nodes_m.group(5)

    # ── 3. Parse namespace declarations from the nodes() XQuery ──────────────
    namespaces = _parse_xml_namespaces(nodes_xquery)
    # Extract the actual XPath (after the last semicolon)
    xpath_root = nodes_xquery.rsplit(";", 1)[-1].strip().lstrip("/")

    # ── 4. Parse .value() column extractions ─────────────────────────────────
    # Pattern A: bare  alias.ref.value(N'declare ... ; (xpath)[1]', 'TYPE') AS col_alias
    value_pattern = re.compile(
        r",?\s*\w+\.\w+\.value\(\s*n?'(.*?)'\s*,\s*'(\w+(?:\(\d+(?:,\s*\d+)?\))?)'[^)]*\)\s+as\s+(\S+)",
        re.IGNORECASE | re.DOTALL,
    )
    # Pattern B: cast(replace(alias.ref.value(N'...'; xpath, 'TYPE') ,'Z',''), 101) AS col_alias
    # This is the intermediate form produced by convert_objects.py when MSSQL uses
    # CONVERT(datetime, REPLACE(.value(...), 'Z', ''), 101) to strip ISO timezone suffix.
    # The outer cast/101 is invalid PG syntax — extract the XPath and emit as DATE XMLTABLE column.
    convert_value_pattern = re.compile(
        r",?\s*cast\s*\(\s*replace\s*\(\s*\w+\.\w+\.value\s*\(\s*n?'(.*?)'\s*,\s*'[^']+'\s*\)\s*(?:\s*,\s*'[^']*')*\s*\)\s*,\s*\d+\s*\)\s+as\s+(\S+)",
        re.IGNORECASE | re.DOTALL,
    )

    def _mssql_type_to_pg(pg_type: str) -> str:
        type_map = {"NVARCHAR": "VARCHAR", "TEXT": "TEXT", "VARCHAR": "VARCHAR",
                    "INT": "INTEGER", "BIGINT": "BIGINT", "NUMERIC": "NUMERIC"}
        base_type = re.match(r"(\w+)", pg_type).group(1)
        return pg_type.replace(base_type, type_map.get(base_type, base_type))

    def _extract_xpath(xquery_expr: str) -> str:
        col_xpath = xquery_expr.rsplit(";", 1)[-1].strip()
        col_xpath = re.sub(r"^\((.+)\)\s*\d*$", r"\1", col_xpath)  # strip outer parens + [1]
        # Also strip a bare trailing digit that came from [1]→1 conversion when there
        # are no outer parens (e.g. 'DateFirstPurchase1' → 'DateFirstPurchase').
        # The regex matches any word-based XPath ending in digits with no preceding slash.
        col_xpath = re.sub(r"^([\w.]+)\d+$", r"\1", col_xpath)
        return col_xpath.lstrip("/")

    # Collect all matches with their start position so we preserve source column order.
    #
    # Problem: value_pattern uses a lazy (.*?) for the XQuery, which can bridge across
    # the cast(replace(..., 'Z', ''), 101) wrappers that convert_objects.py generates for
    # MSSQL CONVERT(datetime, REPLACE(.value(...), ...)) columns.  When a cast/replace
    # column is followed by a bare .value() column, value_pattern starts inside the
    # cast/replace wrapper and (.*?) stretches all the way to the next .value() expression,
    # producing one cross-expression "match" and consuming the bare column's position in
    # the string, so finditer skips the real bare match entirely.
    #
    # Fix: mask the cast/replace wrappers in the SQL before running value_pattern, so
    # value_pattern only sees clean bare .value() calls.  convert_value_pattern is run on
    # the original SQL.
    raw_cols: list[tuple[int, str, str, str]] = []  # (start, alias, pg_type, xpath)

    # Step 1: collect convert-wrapped date columns (on original SQL)
    convert_spans: list[tuple[int, int]] = []
    for m in convert_value_pattern.finditer(sql):
        alias = m.group(2).strip().rstrip(",")
        xquery = m.group(1)
        xpath = _extract_xpath(xquery)
        xpath = re.sub(r"\s+AS\s+\w+$", "", xpath, flags=re.IGNORECASE).strip()
        raw_cols.append((m.start(), alias, "DATE", xpath))
        convert_spans.append((m.start(), m.end()))

    # Step 2: replace each cast/replace block with whitespace of the same length so that
    # character positions for all subsequent bare .value() calls remain intact.
    sql_for_value = sql
    for span_start, span_end in sorted(convert_spans, reverse=True):
        placeholder = " " * (span_end - span_start)
        sql_for_value = sql_for_value[:span_start] + placeholder + sql_for_value[span_end:]

    # Step 3: run value_pattern on the masked SQL — only bare .value() calls remain
    for m in value_pattern.finditer(sql_for_value):
        xquery_expr = m.group(1)
        pg_type = _mssql_type_to_pg(m.group(2).upper())
        alias = m.group(3).strip().rstrip(",")
        raw_cols.append((m.start(), alias, pg_type, _extract_xpath(xquery_expr)))

    # Sort by source position so XMLTABLE COLUMNS and SELECT list match source order
    raw_cols.sort(key=lambda t: t[0])

    columns = []
    for _pos, alias, pg_type, col_xpath in raw_cols:
        pg_alias = f'"{alias}"' if "." in alias else alias
        columns.append((pg_alias, pg_type, col_xpath))

    if not columns:
        return sql, False

    # ── 5. Find non-XML columns in the SELECT (before .value() columns) ──────
    # Extract simple columns like: jc.jobcandidateid, jc.businessentityid
    select_start = view_hdr_m.end()
    first_value = sql.find(".ref.value(", select_start)
    if first_value < 0:
        first_value = sql.find(".value(", select_start)
    if first_value < 0:
        return sql, False

    # Find the SELECT keyword
    select_m = re.search(r"\bselect\b", sql[select_start:], re.IGNORECASE)
    if not select_m:
        return sql, False
    select_pos = select_start + select_m.end()

    # Get text between SELECT and first .value() call — these are the simple cols
    pre_value_text = sql[select_pos:first_value]
    # Find last comma before the .value() fragment
    last_comma = pre_value_text.rfind(",")
    if last_comma >= 0:
        pre_value_text = pre_value_text[:last_comma]

    simple_cols = []
    for line in pre_value_text.splitlines():
        col = line.strip().strip(",").strip()
        if col and not col.startswith("--"):
            simple_cols.append(col)

    # ── 6. Find trailing columns after CROSS APPLY (e.g. modifieddate) ───────
    # Look for columns between the last .value() and CROSS APPLY
    nodes_start = nodes_m.start()
    after_last_value = sql[:nodes_start].rstrip()
    trailing_cols = []
    # Check for simple columns after the last .value() AS alias
    last_value_end = 0
    for m in value_pattern.finditer(sql):
        last_value_end = m.end()
    between_text = sql[last_value_end:nodes_start].strip()
    if between_text:
        for line in between_text.splitlines():
            col = line.strip().strip(",").strip()
            if col and not col.startswith("--") and not re.match(r"^(from|cross)\b", col, re.IGNORECASE):
                trailing_cols.append(col)

    # ── 7. Extract the FROM clause (table source) ────────────────────────────
    from_m = re.search(
        r"\bfrom\s+([\w.]+)\s+(\w+)",
        sql[last_value_end:nodes_start],
        re.IGNORECASE,
    )
    if not from_m:
        # Try in the full SQL before CROSS APPLY
        from_m = re.search(r"\bfrom\s+([\w.]+)\s+(\w+)", sql[:nodes_start], re.IGNORECASE)
    if not from_m:
        return sql, False

    source_table = from_m.group(1)
    source_alias = from_m.group(2)

    # ── 8. Build XMLNAMESPACES clause ────────────────────────────────────────
    ns_parts = []
    for ns_prefix, ns_uri in namespaces:
        if ns_prefix is None:  # default namespace
            ns_parts.append(f"DEFAULT '{ns_uri}'")
        else:
            ns_parts.append(f"'{ns_uri}' AS {ns_prefix}")
    ns_clause = ", ".join(ns_parts)

    # ── 9. Build COLUMNS clause ──────────────────────────────────────────────
    col_lines = []
    for pg_alias, pg_type, xpath in columns:
        col_lines.append(f"        {pg_alias} {pg_type} PATH '{xpath}'")

    # ── 10. Assemble the final view ──────────────────────────────────────────
    # EWI comments
    comments = [l for l in sql.splitlines() if l.strip().startswith("--")]
    comment_block = "\n".join(comments) + "\n" if comments else ""

    # SELECT columns: simple_cols + x.col_alias for each XMLTABLE col + trailing
    select_parts = list(simple_cols)
    for pg_alias, _, _ in columns:
        select_parts.append(f"    x.{pg_alias}")
    for tc in trailing_cols:
        select_parts.append(f"    {tc}")

    select_list = ",\n".join(select_parts)
    columns_block = ",\n".join(col_lines)

    result = (
        f"{comment_block}"
        f"CREATE OR REPLACE VIEW {view_name} AS\n"
        f"SELECT\n{select_list}\n"
        f"FROM {source_table} {source_alias}\n"
        f"CROSS JOIN LATERAL XMLTABLE(\n"
        f"    XMLNAMESPACES({ns_clause}),\n"
        f"    '/{xpath_root}' PASSING CAST({source_alias}.{xml_column} AS xml)\n"
        f"    COLUMNS\n"
        f"{columns_block}\n"
        f") AS x;\n"
    )

    return result, True


def _parse_xml_namespaces(xquery: str) -> list[tuple[str | None, str]]:
    """Parse namespace declarations from an XQuery preamble.

    Returns list of (prefix_or_None, uri) tuples.
    """
    namespaces = []
    # Default namespace: declare default element namespace "uri"
    for m in re.finditer(
        r'declare\s+default\s+element\s+namespace\s+"?([^";]+)"?',
        xquery, re.IGNORECASE,
    ):
        namespaces.append((None, m.group(1).strip()))

    # Named namespace: declare namespace prefix="uri" or declare namespace prefix=uri
    for m in re.finditer(
        r'declare\s+namespace\s+(\w+)\s*=\s*"?([^";]+)"?',
        xquery, re.IGNORECASE,
    ):
        namespaces.append((m.group(1), m.group(2).strip()))

    return namespaces


def _convert_direct_value_xml(sql: str, view_name: str) -> tuple[str, bool]:
    """Convert views that use col.value() directly (no .nodes() row expansion).

    Pattern: SELECT col.value(N'declare namespace ...; xpath', 'TYPE') AS alias
    FROM table

    These become: SELECT (xpath('/path', col::xml, ns_array))[1]::TYPE AS alias
    Or use XMLTABLE with a single '/root' path.
    """
    # Extract the view header
    view_hdr_m = re.match(
        r"(.*?create\s+or\s+replace\s+view\s+\S+\s+as\s*)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not view_hdr_m:
        return sql, False

    # Parse all .value() extractions
    # Pattern: col.value(N'declare ns...; (xpath)[1]', 'TYPE') AS alias
    value_pattern = re.compile(
        r",?\s*(\w+)\.value\(\s*n?'(.*?)'\s*,\s*'(\w+(?:\(\d+(?:,\s*\d+)?\))?)'[^)]*\)\s+as\s+(\S+)",
        re.IGNORECASE | re.DOTALL,
    )

    matches = list(value_pattern.finditer(sql))
    if not matches:
        return sql, False

    # Get xml column name from first match
    xml_column = matches[0].group(1)

    # Parse namespaces from first match's XQuery (all matches use the same ns)
    first_xquery = matches[0].group(2)
    namespaces = _parse_xml_namespaces(first_xquery)

    # Build columns list
    columns = []
    for m in matches:
        xquery_expr = m.group(2)
        pg_type = m.group(3).upper()
        alias = m.group(4).strip().rstrip(",")

        # Extract xpath (after last semicolon, strip parens and [1])
        col_xpath = xquery_expr.rsplit(";", 1)[-1].strip()
        col_xpath = re.sub(r"^\((.+)\)\s*\d*$", r"\1", col_xpath)
        col_xpath = col_xpath.lstrip("/")

        # Map types
        type_map = {"NVARCHAR": "VARCHAR", "TEXT": "TEXT", "VARCHAR": "VARCHAR",
                    "INT": "INTEGER", "BIGINT": "BIGINT", "NUMERIC": "NUMERIC"}
        base_type = re.match(r"(\w+)", pg_type).group(1)
        pg_type_mapped = pg_type.replace(base_type, type_map.get(base_type, base_type))

        pg_alias = f'"{alias}"' if "." in alias else alias
        columns.append((pg_alias, pg_type_mapped, col_xpath))

    # Find simple columns (before first .value())
    select_m = re.search(r"\bselect\b", sql, re.IGNORECASE)
    if not select_m:
        return sql, False
    pre_text = sql[select_m.end():matches[0].start()]
    simple_cols = []
    for line in pre_text.splitlines():
        col = line.strip().strip(",").strip()
        if col and not col.startswith("--"):
            simple_cols.append(col)

    # Find FROM clause (after all .value() calls)
    from_m = re.search(r"\bfrom\s+([\w.]+)\s*(\w*)", sql[matches[-1].end():], re.IGNORECASE)
    if not from_m:
        return sql, False
    source_table = from_m.group(1)
    source_alias = from_m.group(2) if from_m.group(2) else ""

    # Check for trailing columns after FROM (e.g. ,rowguid ,modifieddate)
    # These might be between the table name and WHERE/ORDER/;
    after_from = sql[matches[-1].end() + from_m.end():]
    trailing_cols = []
    for line in after_from.splitlines():
        col = line.strip().strip(",").strip().rstrip(";")
        if col and not col.startswith("--") and not re.match(r"^(where|order|group|having|;)", col, re.IGNORECASE):
            if re.match(r"^,?\s*\w+$", col):
                trailing_cols.append(col.lstrip(",").strip())
        else:
            break

    # Build XMLNAMESPACES clause
    ns_parts = []
    for ns_prefix, ns_uri in namespaces:
        if ns_prefix is None:
            ns_parts.append(f"DEFAULT '{ns_uri}'")
        else:
            ns_parts.append(f"'{ns_uri}' AS {ns_prefix}")
    ns_clause = ", ".join(ns_parts)

    # Build XMLTABLE COLUMNS
    col_lines = []
    for pg_alias, pg_type, xpath in columns:
        col_lines.append(f"        {pg_alias} {pg_type} PATH '{xpath}'")

    # Determine xpath root — for direct .value() it's typically the document root
    # Look at first column xpath to find common prefix
    first_path = columns[0][2] if columns else ""
    # Use '/' as root for direct extractions
    xpath_root = "/"

    # Assemble
    comments = [l for l in sql.splitlines() if l.strip().startswith("--")]
    comment_block = "\n".join(comments) + "\n" if comments else ""

    select_parts = list(simple_cols)
    for pg_alias, _, _ in columns:
        select_parts.append(f"    x.{pg_alias}")
    for tc in trailing_cols:
        select_parts.append(f"    {tc}")

    select_list = ",\n".join(select_parts)
    columns_block = ",\n".join(col_lines)

    tbl_ref = f"{source_table} {source_alias}" if source_alias else source_table
    result = (
        f"{comment_block}"
        f"CREATE OR REPLACE VIEW {view_name} AS\n"
        f"SELECT\n{select_list}\n"
        f"FROM {tbl_ref}\n"
        f"CROSS JOIN LATERAL XMLTABLE(\n"
        f"    XMLNAMESPACES({ns_clause}),\n"
        f"    '{xpath_root}' PASSING CAST({source_alias or source_table}.{xml_column} AS xml)\n"
        f"    COLUMNS\n"
        f"{columns_block}\n"
        f") AS x\n"
        f"WHERE {source_alias or source_table}.{xml_column} IS NOT NULL;\n"
    )

    return result, True


# ---------------------------------------------------------------------------
# Workspace catalog helpers
# ---------------------------------------------------------------------------

def _load_bit_columns(work_dir: Path) -> dict[str, list[str]]:
    """Load {schema.table: [col, ...]} from bit_columns.json if it exists.

    Written by extract_ddl.py during Phase 3 when a live source connection is
    available.  Returns empty dict if the file is absent (DDL-file migrations).
    """
    path = work_dir / "bit_columns.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_alias_map(sql: str) -> dict[str, str]:
    """Parse FROM/JOIN clauses and return {alias → schema.table} (lowercase).

    Handles:
      FROM api.Foo f              → {'f': 'api.foo'}
      JOIN dbo.Bar AS b           → {'b': 'dbo.bar'}
      FROM SomeTable              → {'sometable': 'sometable'}
      FROM api.Definition AS def  → {'def': 'api.definition'}
    """
    _SQL_KEYWORDS = {
        'on', 'where', 'inner', 'left', 'right', 'outer', 'cross',
        'join', 'set', 'and', 'or', 'not', 'in', 'is', 'null',
        'select', 'from', 'as', 'with', 'group', 'order', 'by',
        'having', 'union', 'all', 'distinct', 'case', 'when', 'then',
        'else', 'end', 'into', 'values', 'insert', 'update', 'delete',
    }
    alias_map: dict[str, str] = {}
    # Match: FROM/JOIN [schema.]table [AS] alias  (not followed by '(' which is a subquery)
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+'
        r'(?:(\w+)\.)?(\w+)'           # optional schema.table
        r'(?:\s+AS\s+|\s+)(\w+)'       # optional AS + alias
        r'(?!\s*\()',                    # not a subquery
        re.IGNORECASE,
    )
    for m in pattern.finditer(sql):
        schema = (m.group(1) or '').lower()
        table  = m.group(2).lower()
        alias  = m.group(3).lower()
        if alias in _SQL_KEYWORDS:
            continue
        fqn = f"{schema}.{table}" if schema else table
        alias_map[alias] = fqn
    # Also register the bare table name as an alias for itself (unqualified usage)
    for alias, fqn in list(alias_map.items()):
        alias_map.setdefault(fqn.split('.')[-1], fqn)
    return alias_map


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


def _fix_additional(sql: str, bit_columns: dict[str, list[str]] | None = None) -> tuple[str, list[str]]:
    """Apply extra T-SQL→PG fixes not covered by the YAML-driven passes."""
    fixes = []
    original = sql

    # OUTER APPLY (subquery) alias → LEFT JOIN LATERAL (subquery) alias ON true
    sql = re.sub(r"\bOUTER\s+APPLY\s*\(", "LEFT JOIN LATERAL (", sql, flags=re.IGNORECASE)
    # Add ON true after ) alias — only when the view actually has a LATERAL join.
    # Without this guard, the regex fires on ordinary derived-table aliases in FROM clauses
    # (e.g. "FROM (SELECT ...) a\nINNER JOIN ...") and inserts a spurious ON true.
    if "LEFT JOIN LATERAL" in sql.upper():
        # Case 1: ) alias\n  followed by another JOIN, WHERE, ORDER BY, etc.
        sql = re.sub(
            r'\)\s+(\w+)\s*\n(\s*(?:LEFT JOIN LATERAL|INNER JOIN|LEFT JOIN|RIGHT JOIN|WHERE|ORDER BY|GROUP BY|HAVING|\Z))',
            lambda m: f') {m.group(1)} ON true\n{m.group(2)}',
            sql, flags=re.IGNORECASE
        )
        # Case 2: ) alias; (alias then semicolon — end of statement)
        sql = re.sub(r'\)\s+(\w+)\s*;', lambda m: f') {m.group(1)} ON true;', sql)

    # CAST(expr AS NVARCHAR) → CAST(expr AS TEXT)
    sql = re.sub(r"\bCAST\s*\(([^)]+)\s+AS\s+NVARCHAR\s*\)",
                 r"CAST(\1 AS TEXT)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCAST\s*\(([^)]+)\s+AS\s+NVARCHAR\s*\((\d+)\)\s*\)",
                 r"CAST(\1 AS VARCHAR(\2))", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bNVARCHAR\b", "TEXT", sql, flags=re.IGNORECASE)

    # FOR JSON PATH → EWI comment then attempt json_agg rewrite
    # Step 1: For subqueries of form: (SELECT col1, col2 FROM ... WHERE ... FOR JSON PATH) AS alias
    # Convert to: (SELECT json_agg(json_build_object('col1', col1, 'col2', col2)) FROM ... WHERE ...) AS alias
    def _rewrite_for_json_path(m: re.Match) -> str:
        subq = m.group(1)
        # Extract SELECT column list (before FROM)
        select_m = re.match(r'\s*SELECT\s+(.*?)\s+FROM\s+', subq, re.IGNORECASE | re.DOTALL)
        if not select_m:
            return f"(SELECT json_agg(row_to_json(t))::text FROM ({subq}) t)"
        cols_raw = select_m.group(1)
        # Parse individual columns (simple comma split, ignore nested parens)
        cols = []
        depth = 0
        cur = []
        for ch in cols_raw:
            if ch == '(': depth += 1
            elif ch == ')': depth -= 1
            elif ch == ',' and depth == 0:
                cols.append(''.join(cur).strip())
                cur = []
                continue
            cur.append(ch)
        if cur:
            cols.append(''.join(cur).strip())
        # Build json_build_object pairs: 'ColName', col_expr
        pairs = []
        for col in cols:
            # Handle col AS alias or just col
            alias_m = re.match(r'(.+?)\s+AS\s+(\w+)\s*$', col.strip(), re.IGNORECASE)
            if alias_m:
                key = alias_m.group(2)
                expr = alias_m.group(1).strip()
            else:
                key = col.strip().split('.')[-1]  # use last part of dotted name
                expr = col.strip()
            pairs.append(f"'{key}', {expr}")
        rest = re.sub(r'^\s*SELECT\s+.*?\s+FROM\s+', 'FROM ', subq, flags=re.IGNORECASE | re.DOTALL)
        rest = re.sub(r'\s*FOR\s+JSON\s+PATH.*$', '', rest, flags=re.IGNORECASE | re.DOTALL).strip()
        return f"(SELECT json_agg(json_build_object({', '.join(pairs)}))::text {rest})"

    sql = re.sub(
        r'\(\s*(SELECT\s+(?:(?!\bSELECT\b).)+?)\s*FOR\s+JSON\s+PATH\s*\)',
        _rewrite_for_json_path, sql, flags=re.IGNORECASE | re.DOTALL
    )

    # Remaining FOR JSON PATH (not in subquery) → EWI comment
    sql = re.sub(r"\s*FOR\s+JSON\s+PATH(?:\s*,\s*ROOT\s*\('[^']*'\))?\s*",
                 "\n    -- EWI: FOR JSON PATH not supported — manual rewrite needed",
                 sql, flags=re.IGNORECASE)

    # SELECT TOP (100) PERCENT — T-SQL "select all rows in order" → just remove it
    sql = re.sub(r"\bTOP\s*\(\s*100\s*\)\s*PERCENT\s+", "", sql, flags=re.IGNORECASE)

    # STRING_AGG(expr, sep)\n  WITHIN GROUP (ORDER BY ...) → string_agg(expr, sep ORDER BY ...)
    # T-SQL style: STRING_AGG(expr, sep) WITHIN GROUP (ORDER BY cols) AS alias
    def _fix_string_agg(m: re.Match) -> str:
        inner = m.group(1)      # everything inside STRING_AGG(...)
        order_by = m.group(2)   # everything inside WITHIN GROUP (ORDER BY ...)
        # Find the last comma to split expr from sep
        depth = 0
        last_comma = -1
        for i, ch in enumerate(inner):
            if ch == '(': depth += 1
            elif ch == ')': depth -= 1
            elif ch == ',' and depth == 0: last_comma = i
        if last_comma == -1:
            return m.group(0)  # can't parse — leave as-is
        expr = inner[:last_comma].strip()
        sep = inner[last_comma+1:].strip()
        return f"string_agg({expr}, {sep} ORDER BY {order_by})"

    sql = re.sub(
        r"\bSTRING_AGG\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+([^)]+)\)",
        _fix_string_agg, sql, flags=re.IGNORECASE
    )

    # IIF(condition, true_val, false_val) → CASE WHEN condition THEN true_val ELSE false_val END
    # Uses balanced-paren extraction to handle CAST(), nested functions etc.
    def _iif_to_case(m: re.Match) -> str:
        # Find the balanced content of IIF(...)
        start_pos = m.start()
        iif_open = sql.find('(', start_pos + len('IIF'))
        if iif_open == -1:
            return m.group(0)
        content = _extract_balanced(sql, iif_open)
        # Split into 3 args respecting paren depth
        args = []
        depth = 0
        cur = []
        for ch in content:
            if ch == '(':
                depth += 1; cur.append(ch)
            elif ch == ')':
                depth -= 1; cur.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            args.append(''.join(cur).strip())
        if len(args) != 3:
            return m.group(0)
        return f"CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END"

    # Replace IIF( ... ) using a non-greedy approach with balanced parens via string scan
    result_parts = []
    i = 0
    while i < len(sql):
        m = re.search(r'\bIIF\s*\(', sql[i:], re.IGNORECASE)
        if not m:
            result_parts.append(sql[i:])
            break
        abs_start = i + m.start()
        abs_open = i + m.end() - 1  # position of the opening '('
        result_parts.append(sql[i:abs_start])
        # Extract balanced content
        content = _extract_balanced(sql, abs_open)
        abs_end = abs_open + len(content) + 2  # +2 for the parens
        # Parse args
        args = []
        depth = 0
        cur = []
        for ch in content:
            if ch == '(':
                depth += 1; cur.append(ch)
            elif ch == ')':
                depth -= 1; cur.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            args.append(''.join(cur).strip())
        if len(args) == 3:
            result_parts.append(f"CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END")
        else:
            result_parts.append(sql[abs_start:abs_end])
        i = abs_end
    sql = ''.join(result_parts)

    # Boolean column comparisons: schema-aware using bit_columns.json from Phase 3.
    # When bit_columns is provided, we resolve `alias.col` to (schema.table, col) and
    # check the source catalog — fixing only columns that were genuinely BIT.
    # Falls back to a static list when bit_columns.json was not available (DDL-file mode).
    _FALLBACK_BOOL_COLS = {
        "issubmissionfailed", "isprocessing", "ispublished",
        "iscpgatrevenuecenter", "isslb", "isactive", "isvalid",
        "isanonymous", "isonmenu", "isdefault", "isenabled",
        "isstartschedule", "iscartbulkscheduler", "isvisible", "isdeleted",
        "discontinued",  # Northwind Products.Discontinued BIT
    }

    _alias_map = _build_alias_map(sql) if bit_columns else {}

    def _fix_bool_comparison(m: re.Match) -> str:
        prefix = m.group(1)         # table alias or bare table name
        col    = m.group(2).lower()
        op     = m.group(3)         # =, !=, or <>
        val    = m.group(4).strip() # "0" or "1"

        is_bool = False
        if bit_columns:
            # Schema-aware path: resolve alias → fully-qualified table name
            fqn = _alias_map.get(prefix.lower(), prefix.lower())
            table_cols = bit_columns.get(fqn, [])
            # If fqn is unqualified, try matching just the table part
            if not table_cols:
                for key, cols in bit_columns.items():
                    if key.split('.')[-1] == fqn and col in cols:
                        table_cols = cols
                        break
            is_bool = col in table_cols
        else:
            # Fallback: match by column name only
            is_bool = col in _FALLBACK_BOOL_COLS

        if is_bool:
            bool_val = "false" if val == "0" else "true"
            return f"{prefix}.{col} {op} {bool_val}"
        return m.group(0)

    # Match `table.col = 0` and the MSSQL generated `(table.col)=0` form
    # (Northwind wizards emit triple-nested parens like `(((col)=0))`).
    # Two-pass approach:
    #   Pass 1 — `(table.col)=0` pattern: the surrounding parens are part of
    #             the column grouping only; consuming both ( and ) preserves the
    #             outer parenthesis balance (e.g. `((( col = false ))`).
    #   Pass 2 — `table.col = 0` plain form: catches anything pass 1 missed.
    sql = re.sub(
        r'\(\s*(\w+)\.(\w+)\s*\)\s*(=|!=|<>)\s*(0|1)\b',
        _fix_bool_comparison,
        sql, flags=re.IGNORECASE
    )
    sql = re.sub(
        r'(\w+)\.(\w+)\s*(=|!=|<>)\s*(0|1)\b',
        _fix_bool_comparison,
        sql, flags=re.IGNORECASE
    )

    # Epoch-based timestamp arithmetic: '1970-01-01' + x * INTERVAL
    # PG requires an explicit ::timestamp cast on the date literal
    sql = re.sub(
        r"'1970-01-01'\s*\+",
        "'1970-01-01'::timestamp +",
        sql, flags=re.IGNORECASE
    )

    if sql != original:
        fixes.append("additional_fixes: OUTER APPLY/NVARCHAR/FOR JSON PATH/boolean comparisons/epoch-timestamp")
    return sql, fixes


def _apply_view_plpgsql_rules(sql: str) -> tuple[str, list[str]]:
    """Apply view_only: true rules from plpgsql-fixes.yaml to view SQL.

    These are generalizable SQL-level patterns (not PL/pgSQL) that belong in
    the shared rule file so they apply to both views and functions/procedures.
    """
    import yaml
    fixes = []
    rules_path = SKILL_DIR / "references" / "rules" / "mssql-to-pg" / "plpgsql-fixes.yaml"
    if not rules_path.exists():
        return sql, fixes

    try:
        doc = yaml.safe_load(rules_path.read_text())
    except Exception:
        return sql, fixes

    for rule in doc.get("body_transforms", []):
        if not rule.get("view_only"):
            continue
        if not rule.get("enabled", True):
            continue
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        flag_names = rule.get("flags", ["IGNORECASE"])
        flags = 0
        for fn in flag_names:
            flags |= getattr(re, fn, 0)
        try:
            new_sql, n = re.subn(pattern, replacement, sql, flags=flags)
            if n:
                sql = new_sql
                fixes.append(f"{rule['name']}: {n}")
        except re.error:
            pass

    return sql, fixes


def _fix_mysql_view(sql: str) -> tuple[str, list[str]]:
    """Apply MySQL-specific cleanup to a SnowConvert-generated PostgreSQL view.

    MySQL (MariaDB) views converted for PostgreSQL commonly retain MySQL-isms
    that break deployment on strict servers:

      1. Backtick identifiers/aliases  ``dma_id`` -> dma_id  (backticks are
         invalid PostgreSQL syntax; PG folds unquoted idents to lower-case).
      2. Bare numeric / date-string pivot aliases such as  `AS 20171013` ->
         quoted  `AS "20171013"`  (a purely-numeric alias is not a valid PG
         column identifier).  Anything that is not a simple PG identifier gets
         double-quoted.
      3. Non-aggregated SELECT columns missing from GROUP BY (MySQL tolerates
         this with ONLY_FULL_GROUP_BY=OFF; PostgreSQL does not) — those
         columns are appended to GROUP BY.
    """
    fixes = []

    # 1. Strip backtick-quoted identifiers: ``name`` -> unquoted lower-case,
    #    or double-quoted when the inner name is not a simple PG identifier.
    def _unquote_backtick(m: re.Match) -> str:
        name = m.group(1)
        if re.match(r"^[a-z_][a-z0-9_]*$", name):
            return name
        return f'"{name}"'

    new, n = re.subn(r"`([^`]+)`", _unquote_backtick, sql)
    if n:
        sql = new
        fixes.append(f"mysql_backticks: {n} backtick identifiers unquoted")

    # 2. Quote bare non-identifier aliases:  AS <numeric-or-mixed> -> AS "<...>".
    #    Leaves plain `AS lower_ident` untouched.
    def _quote_alias(m: re.Match) -> str:
        alias = m.group(1)
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", alias):
            return f"AS {alias}"
        return f'AS "{alias}"'

    new, n = re.subn(
        r"\bAS\s+([0-9][^\s,;]+|[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+)",
        _quote_alias, sql,
    )
    if n:
        sql = new
        fixes.append(f"mysql_quote_aliases: {n} non-identifier aliases quoted")

    # 3. Complete GROUP BY with non-aggregated SELECT columns (PostgreSQL
    #    requires every non-aggregated output column to appear in GROUP BY).
    #    Use a greedy match up to the statement terminator ';' (or end of text)
    #    so additions are inserted before the ';' — never mid-token.
    sel_m = re.search(r"\bSELECT\s+(.*?)\s+FROM\b", sql, re.IGNORECASE | re.DOTALL)
    gby_m = re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE)
    if sel_m and gby_m:
        rest = sql[gby_m.end():]
        semi = rest.find(";")
        list_end = gby_m.end() + (semi if semi != -1 else len(rest))
        existing_list = rest[:semi] if semi != -1 else rest
        existing = {g.strip().strip('"').lower()
                    for g in existing_list.split(",") if g.strip()}

        def _add(key: str) -> None:
            if key and key not in existing:
                existing.add(key)
                additions.append(f'"{key}"' if not re.match(r"^[a-z_][a-z0-9_]*$", key) else key)

        additions = []
        for part in re.split(r"\s*,\s*", sel_m.group(1)):
            part = part.strip()
            if not part or part.lower().startswith(("sum(", "max(", "min(", "count(", "avg(")):
                continue  # skip aggregate / function-call expressions
            alias_m = re.search(r"\bAS\s+(\S+)$", part, re.IGNORECASE)
            if alias_m:
                # Pivot columns like `stat_name AS "20171013"` — group by the
                # source column, not the aliased output.
                _add(part.split(" AS ", 1)[0].split(".")[-1].strip('"').lower())
            elif "(" not in part:
                _add(part.split(".")[-1].strip('"').lower())
        if additions:
            sql = sql[:list_end] + ", " + ", ".join(additions) + sql[list_end:]
            fixes.append(f"mysql_group_by: added {len(additions)} non-aggregated col(s) to GROUP BY")

    return sql, fixes


def fix_file(
    sql: str,
    filename: str,
    mapping: dict,
    schema: str = "",
    known_tables: set[str] | None = None,
    bit_columns: dict[str, list[str]] | None = None,
    source_type: str = "mssql",
) -> tuple[str, list[str], bool]:
    """Apply all fixes to a single view file.

    For MySQL/MariaDB sources this runs the MySQL-specific cleanup
    (_fix_mysql_view) and skips the T-SQL-specific passes that are only valid
    for SQL Server converted output.

    Returns (fixed_sql, list_of_fix_descriptions, was_pivot_converted).
    """
    if source_type in ("mysql", "mariadb"):
        sql, fixes = _fix_mysql_view(sql)
        return sql, fixes, False

    fixes = []
    was_pivot = False

    # Pass 0 — XML namespace (.value/.nodes) → XMLTABLE conversion runs FIRST.
    # These views have embedded XQuery namespace declarations that no other pass
    # can handle. Short-circuit if detected.
    if re.search(r"declare\s+(?:default\s+element\s+)?namespace", sql, re.IGNORECASE):
        converted, ok = convert_xml_to_xmltable(sql)
        if ok:
            sql = converted
            fixes.append("xml_to_xmltable: converted .value()/.nodes() to XMLTABLE")
            return sql, fixes, False

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

    # Pass 4 — additional T-SQL→PG fixes not covered by the above
    sql, f = _fix_additional(sql, bit_columns=bit_columns)
    fixes.extend(f)

    # Pass 5 — view_only rules from plpgsql-fixes.yaml
    sql, f = _apply_view_plpgsql_rules(sql)
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
    parser.add_argument(
        "--source-type",
        default=os.environ.get("SOURCE_TYPE", "mssql"),
        help="Source type: mssql | mysql | mariadb | oracle (default: $SOURCE_TYPE or mssql)",
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
    bit_columns = _load_bit_columns(work_dir)
    print(f"Source schema   : {schema}")
    print(f"Known tables    : {len(known_tables)} loaded from workspace")
    if bit_columns:
        total_bc = sum(len(v) for v in bit_columns.values())
        print(f"BIT columns     : {total_bc} from {len(bit_columns)} tables (bit_columns.json)")
    else:
        print("BIT columns     : bit_columns.json not found — using fallback list")

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
                sql, f.name, mapping, schema=schema, known_tables=known_tables,
                bit_columns=bit_columns, source_type=args.source_type,
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
