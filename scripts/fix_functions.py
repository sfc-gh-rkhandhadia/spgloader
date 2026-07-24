#!/usr/bin/env python3
"""
fix_functions.py — Apply rule-based PL/pgSQL fixes to converted function SQL files.

Reads wave_3_functions/*.sql (output of convert_objects.py) and applies:
  Pass 1 — Pattern substitutions from plpgsql-fixes.yaml
  Pass 2 — Multi-variable SELECT INTO (SELECT a=c1, b=c2 FROM → SELECT c1,c2 INTO a,b FROM)
  Pass 3 — Add missing END IF; (structural depth counter)
  Pass 4 — Add missing END LOOP; (structural depth counter)
  Pass 5 — Read-only parameter copy (detects assigned params, adds local var)
  Pass 6 — Schema prefix for table refs inside function bodies

Input:   {work_dir}/conversion/postgres/wave_3_functions/*.sql
Output:  {work_dir}/conversion/postgres/wave_3_functions_fixed/*.sql
         {work_dir}/conversion/functions_fix_report.json

Usage:
  python fix_functions.py --work-dir ~/.spgloader/20260101_120000 \\
                          [--mapping  ~/sko-coco/spgloader/references/fix-mappings/view-fixes.yaml] \\
                          [--plpgsql  ~/sko-coco/spgloader/references/rules/mssql-to-pg/plpgsql-fixes.yaml]
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
# Pass 1 — Pattern substitutions from plpgsql-fixes.yaml
# ---------------------------------------------------------------------------

def apply_plpgsql_rules(sql: str, plpgsql_rules: list[dict]) -> tuple[str, list[str]]:
    """Apply plpgsql-fixes.yaml body_transform rules to the full function SQL."""
    fixes = []
    for rule in plpgsql_rules:
        if not rule.get("enabled", True):
            continue
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        flag_names = rule.get("flags", ["IGNORECASE"])
        flags = _rules._build_flags(flag_names)

        new_sql, n = re.subn(pattern, replacement, sql, flags=flags)
        if n:
            sql = new_sql
            fixes.append(f"{rule['name']}: {n}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 2 — Multi-variable SELECT INTO
# ---------------------------------------------------------------------------

def fix_multi_select_into(sql: str) -> tuple[str, list[str]]:
    """Convert SELECT a=c1, b=c2 FROM t → SELECT c1, c2 INTO a, b FROM t."""
    fixes = []

    def _rewrite_multi(m: re.Match) -> str:
        assignments_str = m.group(1)
        rest = m.group(2)  # FROM ...
        # Split on commas, parse each  var = col  pair
        pairs = [a.strip() for a in assignments_str.split(",") if a.strip()]
        vars_list, cols_list = [], []
        for pair in pairs:
            pm = re.match(r'(\w+)\s*=\s*(.+)', pair.strip(), re.IGNORECASE)
            if pm:
                vars_list.append(pm.group(1))
                cols_list.append(pm.group(2).strip())
            else:
                # Not a var=col pair — return unchanged
                return m.group(0)
        return f"SELECT {', '.join(cols_list)} INTO {', '.join(vars_list)} {rest}"

    # Match: SELECT word=expr, word=expr [, ...] FROM
    # Use greedy + so all assignment pairs are captured before FROM
    new, n = re.subn(
        r'\bSELECT\s+((?:\w+\s*=\s*[^,\n]+,?\s*)+)\s+(FROM\b.+)',
        _rewrite_multi, sql, flags=re.IGNORECASE,
    )
    if n:
        sql = new
        fixes.append(f"select_assign_multi: {n}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 3 — Add missing END IF;
# ---------------------------------------------------------------------------

def fix_missing_end_if(sql: str) -> tuple[str, list[str]]:
    """Insert END IF; wherever an IF block is missing its terminator.

    Strategy: split on lines, track IF/ELSIF/ELSE/END depth.
    When an IF opens and is closed by the next statement without END IF,
    insert it.
    """
    fixes = []

    # Locate the body (between $$ ... $$)
    body_m = re.search(r'\$\$\s*(.*?)\s*\$\$', sql, re.DOTALL)
    if not body_m:
        return sql, fixes

    body = body_m.group(1)
    fixed_body, count = _insert_missing_end_if(body)

    if count:
        sql = sql[:body_m.start(1)] + fixed_body + sql[body_m.end(1):]
        fixes.append(f"missing_end_if: {count} END IF; inserted")

    return sql, fixes


def _insert_missing_end_if(body: str) -> tuple[str, int]:
    """Insert missing END IF; statements using a simple line-based parser."""
    lines = body.split('\n')
    result = []
    # Stack of (keyword, indent) for open IF blocks
    if_stack: list[tuple[str, str]] = []
    count = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip().upper()
        indent = line[:len(line) - len(line.lstrip())]

        # Detect END IF (close current IF)
        if re.match(r'^END\s+IF\b', stripped):
            if if_stack:
                if_stack.pop()
            result.append(line)
            i += 1
            continue

        # Detect END LOOP (don't interact with IF stack)
        if re.match(r'^END\s+LOOP\b', stripped):
            result.append(line)
            i += 1
            continue

        # Detect closing END; of the function body — insert any missing END IFs first
        if re.match(r'^END\s*;?\s*$', stripped):
            # Close all open IF blocks
            while if_stack:
                _, open_indent = if_stack.pop()
                result.append(f"{open_indent}END IF;")
                count += 1
            result.append(line)
            i += 1
            continue

        # Detect RETURN — if inside an IF block that has no END IF before RETURN
        # (T-SQL single-statement IF body pattern)
        if re.match(r'^RETURN\b', stripped) and if_stack:
            # Check if the previous non-empty line was a statement body of an IF
            # (i.e. IF was opened, body was one line, now we see RETURN without END IF)
            last_kw, last_indent = if_stack[-1]
            if last_kw in ('THEN', 'ELSE', 'ELSIF') and indent <= last_indent:
                if_stack.pop()
                result.append(f"{last_indent}END IF;")
                count += 1

        # Detect IF THEN (open new IF block)
        if re.match(r'^IF\b', stripped) and re.search(r'\bTHEN\b', stripped):
            result.append(line)
            if_stack.append(('THEN', indent + '    '))
            i += 1
            continue

        # Detect ELSIF / ELSE — pop last THEN level, push new
        if re.match(r'^ELSIF\b', stripped) or re.match(r'^ELSE\b', stripped):
            if if_stack and if_stack[-1][0] in ('THEN', 'ELSIF', 'ELSE'):
                _, body_indent = if_stack.pop()
                if_stack.append((stripped.split()[0], body_indent))
            result.append(line)
            i += 1
            continue

        result.append(line)
        i += 1

    return '\n'.join(result), count


# ---------------------------------------------------------------------------
# Pass 4 — Add missing END LOOP;
# ---------------------------------------------------------------------------

def fix_missing_end_loop(sql: str) -> tuple[str, list[str]]:
    """Insert END LOOP; wherever a WHILE/LOOP block is missing its terminator."""
    fixes = []

    body_m = re.search(r'\$\$\s*(.*?)\s*\$\$', sql, re.DOTALL)
    if not body_m:
        return sql, fixes

    body = body_m.group(1)
    fixed_body, count = _insert_missing_end_loop(body)

    if count:
        sql = sql[:body_m.start(1)] + fixed_body + sql[body_m.end(1):]
        fixes.append(f"missing_end_loop: {count} END LOOP; inserted")

    return sql, fixes


def _insert_missing_end_loop(body: str) -> tuple[str, int]:
    lines = body.split('\n')
    result = []
    loop_stack: list[str] = []  # indent of open loops
    count = 0

    for line in lines:
        stripped = line.strip().upper()
        indent = line[:len(line) - len(line.lstrip())]

        if re.match(r'^END\s+LOOP\b', stripped):
            if loop_stack:
                loop_stack.pop()
            result.append(line)
            continue

        if re.match(r'^END\s*;?\s*$', stripped):
            while loop_stack:
                loop_indent = loop_stack.pop()
                result.append(f"{loop_indent}END LOOP;")
                count += 1
            result.append(line)
            continue

        if re.match(r'^WHILE\b', stripped) and re.search(r'\bLOOP\b', stripped):
            result.append(line)
            loop_stack.append(indent + '    ')
            continue

        result.append(line)

    return '\n'.join(result), count


# ---------------------------------------------------------------------------
# Pass 5 — Read-only parameter copy
# ---------------------------------------------------------------------------

def fix_readonly_params(sql: str) -> tuple[str, list[str]]:
    """Detect parameters that are assigned in the body and add local variable copies.

    PL/pgSQL function parameters are immutable. If the body assigns a param
    (e.g. LicenseNumber := ...), we:
      1. Declare a local _<param> variable with the same type.
      2. Add  _<param> := <param>;  at the start of the body.
      3. Replace all body references of the param with _<param>.
    """
    fixes = []

    # Extract parameter names and types from function signature
    sig_m = re.search(
        r'CREATE\s+OR\s+REPLACE\s+(?:FUNCTION|PROCEDURE)\s+\S+\s*\((.*?)\)\s+RETURNS',
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not sig_m:
        return sql, fixes

    param_defs = sig_m.group(1)
    params: dict[str, str] = {}  # name → type
    for param in re.split(r',', param_defs):
        # Match: IN/OUT/INOUT name type
        pm = re.match(r'\s*(?:IN|OUT|INOUT)?\s+(\w+)\s+(.+)', param.strip(), re.IGNORECASE)
        if pm:
            params[pm.group(1).lower()] = pm.group(2).strip()

    if not params:
        return sql, fixes

    # Find the body between $$...$$
    body_m = re.search(r'\$\$\s*(.*?)\s*\$\$', sql, re.DOTALL)
    if not body_m:
        return sql, fixes

    body = body_m.group(1)

    # Detect which params are actually ASSIGNED in the body (not just compared).
    # Only `:=` counts as an assignment in PL/pgSQL.
    # `SET var = expr` counts too (before it's been converted to var :=).
    # We deliberately exclude `WHERE var = value` and similar comparison patterns.
    assigned_params = {}
    for pname, ptype in params.items():
        if re.search(rf'\b{re.escape(pname)}\s*:=', body, re.IGNORECASE):
            assigned_params[pname] = ptype
        elif re.search(rf'\bSET\s+{re.escape(pname)}\s*=', body, re.IGNORECASE):
            assigned_params[pname] = ptype

    if not assigned_params:
        return sql, fixes

    # For each assigned param: add local var, copy at body start, replace references
    # Local variable name: _<param>
    new_declares = []
    copies = []
    for pname, ptype in assigned_params.items():
        local = f"_{pname}"
        new_declares.append(f"    {local} {ptype};")
        copies.append(f"    {local} := {pname};")
        # Replace body references of param with local var
        # Use word boundary but be careful not to replace the DECLARE or param name
        body = re.sub(rf'\b{re.escape(pname)}\b', local, body, flags=re.IGNORECASE)

    # Inject new DECLARE entries and copy statements
    # Find DECLARE section or insert one
    declare_m = re.search(r'(DECLARE\b.*?)(BEGIN\b)', body, re.IGNORECASE | re.DOTALL)
    if declare_m:
        new_declare_section = declare_m.group(1).rstrip() + "\n" + "\n".join(new_declares) + "\n"
        new_begin = declare_m.group(2) + "\n" + "\n".join(copies)
        body = body[:declare_m.start(1)] + new_declare_section + new_begin + body[declare_m.end(2):]
    else:
        begin_m = re.search(r'\bBEGIN\b', body, re.IGNORECASE)
        if begin_m:
            insert_point = begin_m.end()
            body = (body[:begin_m.start()] +
                    "DECLARE\n" + "\n".join(new_declares) + "\nBEGIN\n" +
                    "\n".join(copies) + "\n" +
                    body[insert_point:])

    sql = sql[:body_m.start(1)] + body + sql[body_m.end(1):]
    fixes.append(f"readonly_params: {', '.join(assigned_params)}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Pass 6 — Schema prefix for table references in function body
# ---------------------------------------------------------------------------

def fix_schema_prefix_in_body(sql: str, schema: str, tables: list[str]) -> tuple[str, list[str]]:
    """Add schema. prefix to unqualified table references inside function bodies."""
    fixes = []
    for table in tables:
        pattern = rf"(?<!\.)(?i:\b(?:FROM|JOIN)\s+)(?i:{re.escape(table)})\b"

        def _add_schema(m: re.Match, _t: str = table, _s: str = schema) -> str:
            kw = m.group(0).split()[0]
            ws = re.match(rf"(?i:{re.escape(kw)})\s*", m.group(0)).group(0)[len(kw):]
            return f"{kw}{ws}{_s}.{_t}"

        new, n = re.subn(pattern, _add_schema, sql, flags=re.IGNORECASE)
        if n:
            sql = new
            fixes.append(f"schema_prefix {schema}.{table}: {n}")
    return sql, fixes


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def fix_file(sql: str, plpgsql_rules: list[dict], mapping: dict) -> tuple[str, list[str]]:
    """Apply all fix passes to a single function SQL file."""
    fixes: list[str] = []

    # Pass 1 — plpgsql rule substitutions
    sql, f = apply_plpgsql_rules(sql, plpgsql_rules)
    fixes.extend(f)

    # Pass 2 — multi-variable SELECT INTO
    sql, f = fix_multi_select_into(sql)
    fixes.extend(f)

    # Pass 1b — re-apply plpgsql rules after Pass 2 so any SELECT INTO
    # statements created by Pass 2 also get their trailing semicolons.
    sql, f = apply_plpgsql_rules(sql, plpgsql_rules)
    fixes.extend(f)

    # Pass 3 — missing END IF;
    sql, f = fix_missing_end_if(sql)
    fixes.extend(f)

    # Pass 4 — missing END LOOP;
    sql, f = fix_missing_end_loop(sql)
    fixes.extend(f)

    # Pass 5 — read-only parameter copy
    sql, f = fix_readonly_params(sql)
    fixes.extend(f)

    # Pass 6 — schema prefix
    sp = mapping.get("schema_prefix", {})
    schema = sp.get("schema", "dbo")
    tables = sp.get("tables", [])
    if tables:
        sql, f = fix_schema_prefix_in_body(sql, schema, tables)
        fixes.extend(f)

    return sql, fixes


def main():
    parser = argparse.ArgumentParser(description="Apply PL/pgSQL fixes to converted function files")
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--mapping",
                        default=str(SKILL_DIR / "references" / "fix-mappings" / "view-fixes.yaml"),
                        help="Path to project view-fixes.yaml (for schema_prefix)")
    parser.add_argument("--plpgsql",
                        default=str(SKILL_DIR / "references" / "rules" / "mssql-to-pg" / "plpgsql-fixes.yaml"),
                        help="Path to plpgsql-fixes.yaml rule file")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    mapping_path = Path(args.mapping).expanduser()
    plpgsql_path = Path(args.plpgsql).expanduser()

    import yaml
    mapping = yaml.safe_load(mapping_path.read_text()) if mapping_path.exists() else {}
    plpgsql_doc = yaml.safe_load(plpgsql_path.read_text()) if plpgsql_path.exists() else {}
    plpgsql_rules = plpgsql_doc.get("body_transforms", [])

    input_dir  = work_dir / "conversion" / "postgres" / "wave_3_functions"
    output_dir = work_dir / "conversion" / "postgres" / "wave_3_functions_fixed"

    if not input_dir.exists():
        print(f"ERROR: input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    func_files = sorted(input_dir.glob("*.sql"))
    print(f"Processing {len(func_files)} function files\nOutput → {output_dir}\n")

    report = {"succeeded": [], "failed": [], "fix_details": {}}
    total_fixes = 0

    for f in func_files:
        sql = f.read_text(encoding="utf-8", errors="ignore")
        try:
            fixed, fixes = fix_file(sql, plpgsql_rules, mapping)
            out_path = output_dir / f.name
            out_path.write_text(fixed, encoding="utf-8")
            report["succeeded"].append(f.name)
            report["fix_details"][f.name] = fixes
            total_fixes += len(fixes)
            fix_summary = f"  {len(fixes)} fix(es)" if fixes else "  no fixes"
            print(f"  OK  {f.name}{fix_summary}")
        except Exception as e:
            report["failed"].append({"file": f.name, "error": str(e)})
            print(f"  ERR {f.name}: {e}")

    report_path = work_dir / "conversion" / "functions_fix_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(f"\n{'='*60}")
    print(f"Files processed : {len(func_files)}")
    print(f"Succeeded       : {len(report['succeeded'])}")
    print(f"Failed          : {len(report['failed'])}")
    print(f"Total fixes     : {total_fixes}")
    print(f"Fix report      : {report_path}")

    if report["failed"]:
        for item in report["failed"]:
            print(f"  {item['file']}: {item['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
