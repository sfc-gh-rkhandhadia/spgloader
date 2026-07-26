#!/usr/bin/env python3
"""
patch_views.py — Apply generic T-SQL→PG fixes to converted view files.

All fixes are source-agnostic.  No project-specific names or column lists
are hardcoded here.

Handles:
  1. TOP (N) PERCENT + ORDER BY  → remove both
  2. OUTER APPLY  → LEFT JOIN LATERAL
  3. CROSS APPLY  → CROSS JOIN LATERAL
  4. Boolean = integer  → Boolean = true/false  (catalog-driven: queries SPG)
  5. Arithmetic split across AS alias  → proper arithmetic expression
  6. IIF(cond, a, b)  → CASE WHEN cond THEN a ELSE b END

Usage:
  # Without SPG connection: fix everything except bool-int comparisons
  python patch_views.py --work-dir ~/.spgloader/20260101_120000

  # With SPG connection: also fixes bool-int using live catalog
  python patch_views.py --work-dir ~/.spgloader/20260101_120000 \\
                        --spg-service pg_my_instance
"""

import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Catalog helper
# ---------------------------------------------------------------------------

def _fetch_bool_columns(spg_service: str) -> list[str]:
    """Return every boolean column name from the SPG catalog (any schema).

    Called once and cached.  Falls back to an empty list on connection error.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(f"service={spg_service}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT column_name "
                "FROM information_schema.columns "
                "WHERE data_type = 'boolean'"
            )
            cols = [row[0].lower() for row in cur.fetchall()]
        conn.close()
        return cols
    except Exception as e:
        print(f"  WARN: could not fetch boolean columns from SPG: {e}",
              file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Fix functions
# ---------------------------------------------------------------------------

def fix_top_percent(sql: str) -> str:
    """Remove TOP (N) PERCENT from SELECT; views cannot have ORDER BY without LIMIT."""
    sql = re.sub(r'SELECT\s+TOP\s*\(\d+\)\s+PERCENT\s+', 'SELECT ',
                 sql, flags=re.IGNORECASE)
    sql = re.sub(r'SELECT\s+TOP\s+\d+\s+PERCENT\s+', 'SELECT ',
                 sql, flags=re.IGNORECASE)
    sql = re.sub(r'\nORDER\s+BY\s+\w+[^;]*$', '', sql,
                 flags=re.IGNORECASE | re.DOTALL)
    return sql


def fix_outer_apply(sql: str) -> str:
    """OUTER APPLY → LEFT JOIN LATERAL, CROSS APPLY → CROSS JOIN LATERAL."""
    sql = re.sub(r'\bOUTER\s+APPLY\b', 'LEFT JOIN LATERAL', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bCROSS\s+APPLY\b', 'CROSS JOIN LATERAL', sql, flags=re.IGNORECASE)
    return sql


def fix_bool_int_comparison(sql: str, bool_cols: list[str]) -> str:
    """Replace col = 0 / col = 1 for columns that are boolean in the target.

    bool_cols comes from the live SPG catalog — no hardcoded list here.
    """
    for col in bool_cols:
        sql = re.sub(
            rf'(\b{re.escape(col)}\b)\s*=\s*0\b', r'\1 = false',
            sql, flags=re.IGNORECASE)
        sql = re.sub(
            rf'(\b{re.escape(col)}\b)\s*=\s*1\b', r'\1 = true',
            sql, flags=re.IGNORECASE)
    return sql


def fix_arithmetic_after_alias(sql: str) -> str:
    """Fix arithmetic continuation split across an AS alias.

    Pattern:  FUNC(a) AS alias
              + FUNC(b)
              + FUNC(c)
    becomes:  FUNC(a) + FUNC(b) + FUNC(c) AS alias

    This is a generic T-SQL export artifact — SSMS sometimes places the alias
    mid-expression when the expression spans multiple lines.
    """
    pattern = (
        r'(OCTET_LENGTH\([^)]+\))\s+AS\s+(\w+)'
        r'(\s*\+\s*OCTET_LENGTH\([^)]+\))'
        r'(\s*\+\s*OCTET_LENGTH\([^)]+\))'
    )
    return re.sub(pattern, r'\1\3\4 AS \2', sql, flags=re.IGNORECASE)


def fix_iif(sql: str) -> str:
    """IIF(cond, true_val, false_val) → CASE WHEN cond THEN true_val ELSE false_val END."""
    def iif_replacer(m: re.Match) -> str:
        args_str = m.group(1)
        args, depth, current = [], 0, []
        for ch in args_str:
            if ch == '(':
                depth += 1; current.append(ch)
            elif ch == ')':
                depth -= 1; current.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(current).strip()); current = []
            else:
                current.append(ch)
        if current:
            args.append(''.join(current).strip())
        if len(args) == 3:
            return f'CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END'
        return m.group(0)

    for _ in range(5):  # up to 5 nesting levels
        new_sql = re.sub(r'\bIIF\s*\(', lambda _m: '__IIF__(', sql, flags=re.IGNORECASE)
        new_sql = re.sub(
            r'__IIF__\(([^()]*(?:\([^()]*\)[^()]*)*)\)', iif_replacer, new_sql)
        if new_sql == sql:
            break
        sql = new_sql
    return sql


# ---------------------------------------------------------------------------
# Per-file patcher
# ---------------------------------------------------------------------------

def patch_file(path: Path, bool_cols: list[str]) -> tuple[bool, list[str]]:
    """Apply all generic fixes to one view file.

    Returns (changed: bool, [applied_fix_names]).
    """
    sql = path.read_text(encoding='utf-8', errors='replace')
    applied = []

    for fix_name, fn, kwargs in [
        ('top_percent',       fix_top_percent,           {}),
        ('outer_apply',       fix_outer_apply,            {}),
        ('bool_int',          fix_bool_int_comparison,    {'bool_cols': bool_cols}),
        ('arithmetic_alias',  fix_arithmetic_after_alias, {}),
        ('iif',               fix_iif,                    {}),
    ]:
        new_sql = fn(sql, **kwargs)
        if new_sql != sql:
            applied.append(fix_name)
            sql = new_sql

    if applied:
        path.write_text(sql, encoding='utf-8')
    return bool(applied), applied


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description='Apply generic T-SQL→PG patches to converted view files'
    )
    p.add_argument('--work-dir', required=True,
                   help='spgloader workspace directory')
    p.add_argument('--spg-service', default=None,
                   help='pg_service name — enables catalog-driven boolean fix')
    args = p.parse_args()

    views_dir = (Path(args.work_dir) / 'conversion' / 'postgres'
                 / 'wave_2_views_fixed')
    if not views_dir.exists():
        print(f'ERROR: directory not found: {views_dir}', file=sys.stderr)
        sys.exit(1)

    # Fetch boolean column names from SPG if a service is provided
    bool_cols: list[str] = []
    if args.spg_service:
        bool_cols = _fetch_bool_columns(args.spg_service)
        print(f'Boolean columns : {len(bool_cols)} fetched from SPG catalog')
    else:
        print('Boolean columns : skipped (pass --spg-service to enable)')

    files = sorted(views_dir.glob('*.sql'))
    changed = 0
    for f in files:
        modified, applied = patch_file(f, bool_cols)
        if modified:
            changed += 1
            print(f'  PATCHED  {f.name}: {", ".join(applied)}')

    print(f'\n{changed} files patched out of {len(files)} total.')


if __name__ == '__main__':
    main()
