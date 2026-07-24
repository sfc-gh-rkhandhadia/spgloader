#!/usr/bin/env python3
"""
convert_objects.py — LLM-style conversion of MSSQL DDL objects to PostgreSQL.

Applies type-mapping rules from references/type-mappings/mssql-to-pg.md and
annotates output with SPG-EWI codes per the EWI code catalog.

Writes wave-ordered output and a conversion manifest.
"""
import json
import re
import sys
from pathlib import Path

# Resolve lib path
SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))

from spgloader.conversion.ewi import annotate_sql
from spgloader.rules import get_loader

_rules = get_loader(SKILL_DIR)


# ---------------------------------------------------------------------------
# Type mapping substitutions (T-SQL → PostgreSQL)
# ---------------------------------------------------------------------------

def apply_type_mappings(ddl: str) -> tuple[str, list[str]]:
    """Apply type mappings and function substitutions from YAML rule files.

    Returns (converted_ddl, [ewiCodes]).
    """
    codes_emit: list[str] = []

    # ── Type mappings (pre-downcase context) ─────────────────────────────────
    any_type = False
    for rule in _rules.type_mappings("pre_downcase"):
        new_ddl, n = re.subn(rule["pattern"], rule["replacement"], ddl, flags=re.IGNORECASE)
        if n > 0:
            ddl = new_ddl
            any_type = True
    if any_type:
        codes_emit.append("SPG-EWI-0001")

    # ── Function / syntax substitutions ──────────────────────────────────────
    any_func = False
    for rule in _rules.function_substitutions():
        flags = _rules._build_flags(rule.get("flags", ["IGNORECASE"]))
        replacement = rule.get("replacement") or ""
        new_ddl, n = re.subn(rule["pattern"], replacement, ddl, flags=flags)
        if n > 0:
            ddl = new_ddl
            any_func = True
            ewi = rule.get("ewi_code")
            # ewi_code: ~ means suppress; explicit code overrides the default SPG-EWI-0002
            if ewi and ewi not in codes_emit:
                codes_emit.append(ewi)
    if any_func and "SPG-EWI-0002" not in codes_emit:
        codes_emit.append("SPG-EWI-0002")

    return ddl, list(dict.fromkeys(codes_emit))  # deduplicate preserving order


def strip_brackets(ddl: str) -> str:
    """Remove T-SQL square bracket quoting from identifiers."""
    return re.sub(r"\[([^\]]+)\]", r'"\1"', ddl)


def downcase_identifiers(ddl: str) -> str:
    """Downcase all quoted "Identifier" to unquoted identifier (like pgloader's downcase identifiers)."""
    return re.sub(r'"([^"]+)"', lambda m: m.group(1).lower(), ddl)


def fix_view_alias_syntax(ddl: str) -> str:
    """Fix T-SQL view-specific alias forms that are invalid in PostgreSQL.

    1. Single-quoted alias:  col AS 'Name'  →  col AS "Name"
    2. Column-equals alias:  alias = expr   →  expr AS alias
       (T-SQL allows `alias = expression` in SELECT lists; PG does not)
    """
    # 1. AS 'single_quoted' → AS "double_quoted"
    ddl = re.sub(r"\bAS\s+'([^']+)'", lambda m: f'AS "{m.group(1)}"', ddl, flags=re.IGNORECASE)

    # 2. alias = expr  →  expr AS alias
    #    Matches a bare identifier followed by a single = (not ==, <=, >=, !=, <>)
    #    at the beginning of a column expression (after comma/newline in SELECT list).
    #    Conservative: only triggers when the identifier starts the expression
    #    (i.e. is preceded only by optional whitespace after a comma or newline).
    def _swap_alias(m: re.Match) -> str:
        alias = m.group(1)
        expr = m.group(2).rstrip()
        return f"{expr} AS {alias}"

    ddl = re.sub(
        r"(?m)^(\s*)(\b[a-z_][a-z0-9_]*)\s*=\s*(?!=)([^,\n=<>!][^,\n]*)",
        lambda m: m.group(1) + _swap_alias(re.match(r"(\S+)\s*=\s*(.*)", m.group(0).strip())),
        ddl, flags=re.IGNORECASE,
    )
    return ddl


def convert_view(ddl: str) -> tuple[str, list[str]]:
    """Convert a T-SQL view to PostgreSQL CREATE OR REPLACE VIEW."""
    codes = []
    # Remove WITH SCHEMABINDING (not supported in PostgreSQL)
    ddl = re.sub(r"WITH\s+SCHEMABINDING\s*", "", ddl, flags=re.IGNORECASE)
    # Normalise CREATE VIEW — handle CREATE VIEW with SSMS format
    ddl = re.sub(r"CREATE\s+VIEW", "CREATE OR REPLACE VIEW", ddl, flags=re.IGNORECASE)
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)  # match pgloader's downcase identifiers
    ddl, tc = apply_type_mappings(ddl)
    codes.extend(tc)
    ddl = fix_view_alias_syntax(ddl)
    # Strip trailing ; and GO (SSMS export artifacts)
    ddl = ddl.strip().rstrip(";").rstrip()
    return ddl + ";", codes


def convert_procedure(ddl: str) -> tuple[str, list[str]]:
    """Convert a T-SQL stored procedure to a PL/pgSQL function."""
    codes = ["SPG-EWI-0004"]
    ddl = strip_brackets(ddl)
    ddl, tc = apply_type_mappings(ddl)
    codes.extend(tc)
    ddl = downcase_identifiers(ddl)

    # Extract procedure name
    m_name = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+([^\s(]+)", ddl, re.IGNORECASE)
    if not m_name:
        return f"-- T-SQL procedure (manual conversion required)\n{ddl}", codes
    proc_name = m_name.group(1).strip('"').lower()

    # Extract parameter list using balanced-paren extraction (handles CHAR(1), NVARCHAR(50) etc.)
    params_raw = ""
    m_paren = re.search(r"PROCEDURE\s+[^\s(]+\s*\(", ddl, re.IGNORECASE)
    if m_paren:
        start = m_paren.end()
        depth, end = 1, start
        while end < len(ddl) and depth > 0:
            if ddl[end] == '(':
                depth += 1
            elif ddl[end] == ')':
                depth -= 1
            end += 1
        params_raw = ddl[start:end-1].strip()
        body_start = ddl.find('\n', end)
    else:
        body_start = -1

    # Extract body
    body_m = re.search(r"(?:AS|BEGIN)\s*(.+)", ddl[body_start:] if body_start > 0 else ddl,
                       re.IGNORECASE | re.DOTALL)
    body = body_m.group(1).strip() if body_m else "-- TODO: convert body"

    # Convert parameters: @param TYPE → p_param TYPE
    params = []
    for param in re.split(r",\s*", params_raw):
        param = param.strip()
        if not param:
            continue
        # Handle OUTPUT params
        is_output = bool(re.search(r"\bOUTPUT\b", param, re.IGNORECASE))
        param = re.sub(r"\s+OUTPUT\b", "", param, flags=re.IGNORECASE)
        param = re.sub(r"\s+=\s*.+$", "", param)  # remove defaults
        param = re.sub(r"@(\w+)", r"\1", param)  # strip @ prefix
        mode = "INOUT" if is_output else "IN"
        params.append(f"    {mode} {param.strip()}")

    # Convert SET NOCOUNT ON, BEGIN/END, DECLARE, etc.
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\bDECLARE\s+@(\w+)\s+", r"    \1 ", body, flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)
    body = re.sub(r"\bGO\b", "", body, flags=re.IGNORECASE)
    # Remove outer BEGIN/END if present
    body = re.sub(r"^\s*BEGIN\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*END\s*$", "", body.strip(), flags=re.IGNORECASE)

    param_str = ",\n".join(params) if params else ""
    result = f"""CREATE OR REPLACE PROCEDURE {proc_name}(
{param_str}
) LANGUAGE plpgsql AS $$
BEGIN
    {body.strip()}
END;
$$;"""
    return result, codes


def convert_function(ddl: str) -> tuple[str, list[str]]:
    """Convert a T-SQL scalar or table-valued function to PL/pgSQL."""
    codes = ["SPG-EWI-0004"]
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)
    ddl, tc = apply_type_mappings(ddl)
    codes.extend(tc)

    # Extract function name
    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([^\s(]+)", ddl, re.IGNORECASE)
    func_name = m.group(1).strip('"').lower() if m else "unknown_function"

    # Extract parameter list — use balanced-paren extraction to handle CHAR(1) etc.
    params_raw = ""
    m_start = re.search(r"FUNCTION\s+[^\s(]+\s*\(", ddl, re.IGNORECASE)
    if m_start:
        start = m_start.end()
        depth, end = 1, start
        while end < len(ddl) and depth > 0:
            if ddl[end] == '(':
                depth += 1
            elif ddl[end] == ')':
                depth -= 1
            end += 1
        params_raw = ddl[start:end-1].strip()  # content between outer ( )

    # Convert parameters: @param TYPE → param TYPE (no prefix — consistent with body references)
    params = []
    for param in re.split(r",\s*", params_raw, flags=re.DOTALL):
        param = param.strip()
        if not param:
            continue
        param = re.sub(r"=\s*.+$", "", param)  # remove defaults
        param = re.sub(r"@(\w+)", r"\1", param)  # strip @ without adding p_ prefix
        params.append(f"    IN {param.strip()}")

    # Extract return type — be careful with RETURNS TABLE (iTVF)
    ret_m = re.search(r"RETURNS\s+(\w[\w\s(),.]+?)(?:WITH\s+SCHEMABINDING|AS\b|BEGIN\b|\Z)",
                      ddl, re.IGNORECASE)
    return_type_raw = ret_m.group(1).strip() if ret_m else "void"
    is_stvf = return_type_raw.upper().startswith("TABLE")  # inline table-valued function

    # For iTVF (RETURNS TABLE ... AS RETURN SELECT ...), use SQL language
    if is_stvf:
        # Extract the RETURN SELECT body
        return_body_m = re.search(r"\bRETURN\s+(.+)", ddl, re.IGNORECASE | re.DOTALL)
        return_body = return_body_m.group(1).strip().rstrip(";") if return_body_m else "SELECT NULL"
        return_body = re.sub(r"@(\w+)", r"\1", return_body)
        param_str = ",\n".join(params) if params else ""
        # RETURNS SETOF record is safe when column types are unknown
        result = f"""-- ** SPG-EWI-0004: iTVF converted — specify return columns or use RETURNS TABLE(col type, ...) **
CREATE OR REPLACE FUNCTION {func_name}(
{param_str}
) RETURNS SETOF record LANGUAGE sql AS $$
    {return_body};
$$;"""
        return result, codes

    # Extract body — search ONLY after the closing ) of the params list to avoid
    # matching AS or BEGIN that appear earlier in the DDL (function name, RETURNS clause).
    fragment = ddl[end:] if m_start and end > 0 else ddl

    # Locate outermost BEGIN...END block using a depth counter
    bm = re.search(r'\bBEGIN\b', fragment, re.IGNORECASE)
    if bm:
        body_inner_start = bm.end()
        depth = 1
        pos = body_inner_start
        while pos < len(fragment) and depth > 0:
            tm = re.search(r'\b(BEGIN|END)\b', fragment[pos:], re.IGNORECASE)
            if not tm:
                break
            tok = tm.group(1).upper()
            if tok == 'BEGIN':
                depth += 1
                pos += tm.end()
            else:
                depth -= 1
                if depth > 0:
                    pos += tm.end()
                else:
                    pos += tm.start()  # stop BEFORE final END
        body = fragment[body_inner_start:pos].strip()
    else:
        # Fallback: take everything after AS
        am = re.search(r'\bAS\b\s*(.+)', fragment, re.IGNORECASE | re.DOTALL)
        body = am.group(1).strip() if am else "-- TODO: convert function body"
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)

    # Extract DECLARE block — move to PL/pgSQL DECLARE section before BEGIN
    declare_lines = []
    def _extract_declare(m):
        var_name = m.group(1).lower()
        var_type = m.group(2).strip()
        # Apply type mappings to the variable type using 'declare' context rules
        for rule in _rules.type_mappings("declare"):
            var_type = re.sub(rule["pattern"], rule["replacement"], var_type, flags=re.IGNORECASE)
        declare_lines.append(f"    {var_name} {var_type};")
        return ""  # remove from body
    body = re.sub(r"DECLARE\s+@(\w+)\s+([^\n;]+);?", _extract_declare, body, flags=re.IGNORECASE)

    # Convert remaining T-SQL body patterns to PL/pgSQL
    body = re.sub(r"\bSET\s+@(\w+)\s*=", r"\1 :=", body, flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)  # strip remaining @ prefixes
    body = re.sub(r"\bSELECT\s+(\w+)\s*=\s*", r"SELECT \1 = ", body, flags=re.IGNORECASE)
    # IF without THEN → IF condition THEN
    body = re.sub(r"\bIF\s+\(([^)]+)\)\n(\s+)", r"IF (\1) THEN\n\2", body, flags=re.IGNORECASE)
    body = re.sub(r"\bIF\s+([^\n(][^\n]+)\n(\s+)(?!THEN|--)", r"IF \1 THEN\n\2", body, flags=re.IGNORECASE)
    body = re.sub(r"\bELSE\s+IF\b", "ELSIF", body, flags=re.IGNORECASE)
    # WHILE without LOOP → WHILE condition LOOP
    body = re.sub(r"\bWHILE\s+([^\n]+)\n(\s+)(?!LOOP)", r"WHILE \1 LOOP\n\2", body, flags=re.IGNORECASE)
    body = re.sub(r"\bGO\b", "", body, flags=re.IGNORECASE)
    body = re.sub(r"^\s*BEGIN\s*$", "", body, flags=re.IGNORECASE | re.MULTILINE)
    body = re.sub(r"\s*END\s*;?\s*$", "", body.strip(), flags=re.IGNORECASE)

    declare_section = "\nDECLARE\n" + "\n".join(declare_lines) if declare_lines else ""
    param_str = ",\n".join(params) if params else ""
    result = f"""CREATE OR REPLACE FUNCTION {func_name}(
{param_str}
) RETURNS {return_type_raw} LANGUAGE plpgsql AS $${declare_section}
BEGIN
    {body.strip()}
END;
$$;"""
    return result, codes


def convert_trigger(ddl: str) -> tuple[str, list[str]]:
    """Convert a T-SQL trigger to a PL/pgSQL trigger function + CREATE TRIGGER."""
    codes = ["SPG-EWI-0005"]
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)
    ddl, tc = apply_type_mappings(ddl)
    codes.extend(tc)

    # MSSQL trigger syntax: CREATE TRIGGER name ON table AFTER|INSTEAD OF|BEFORE event
    # (note: ON table comes BEFORE the event, unlike the previous incorrect regex)
    m = re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+([^\s]+)\s+"
        r"ON\s+([^\s]+)\s+"
        r"(AFTER|INSTEAD\s+OF|BEFORE)\s+(INSERT|UPDATE|DELETE)(?:\s+OR\s+(INSERT|UPDATE|DELETE))?",
        ddl, re.IGNORECASE,
    )
    if not m:
        return f"-- T-SQL trigger (manual conversion required)\n-- SPG-EWI-0005: review required\n{ddl}", codes

    trig_name = m.group(1).strip('"').lower()
    table_name = m.group(2).strip('"')
    timing = m.group(3).upper().replace("_", " ")  # INSTEAD OF (two words), not INSTEAD_OF
    event = m.group(4).upper()
    extra_event = m.group(5)
    events = event + (f" OR {extra_event.upper()}" if extra_event else "")

    # Extract body
    body_m = re.search(r"(?:^AS|\bBEGIN\b)(.+)", ddl, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    body = body_m.group(1).strip() if body_m else "-- TODO: convert trigger body"
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\bGO\b", "", body, flags=re.IGNORECASE)
    body = re.sub(r"^\s*BEGIN\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*END\s*;?\s*$", "", body.strip(), flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)
    # Replace INSERTED/DELETED pseudo-tables with NEW/OLD
    body = re.sub(r"\bINSERTED\b", "new_table", body, flags=re.IGNORECASE)
    body = re.sub(r"\bDELETED\b", "old_table", body, flags=re.IGNORECASE)

    fn_name = f"{trig_name.split('.')[-1]}_fn"
    result = f"""CREATE OR REPLACE FUNCTION {fn_name}()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    {body.strip()}
    RETURN NEW;
END;
$$;

CREATE TRIGGER {trig_name.split('.')[-1]}
{timing} {events} ON {table_name}
FOR EACH ROW
EXECUTE FUNCTION {fn_name}();"""
    return result, codes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="LLM-style conversion of MSSQL objects to PostgreSQL")
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory (e.g. ~/.spgloader/20260101_120000)")
    parser.add_argument("--ddl-objects", default=None,
                        help="Path to ddl_objects.json (default: <work-dir>/ddl_objects.json)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    ddl_path = Path(args.ddl_objects) if args.ddl_objects else work_dir / "ddl_objects.json"
    objs = json.loads(ddl_path.read_text())
    base = work_dir / "conversion" / "postgres"

    # ── Load deprecated review dispositions (Phase 3.6) ──────────────────
    # If analyze_deprecated.py ran, skip_fqns contains objects the user chose
    # not to migrate (disposition = "skip").
    skip_fqns: set[str] = set()
    review_path = work_dir / "deprecated" / "deprecated_review.json"
    if review_path.exists():
        review = json.loads(review_path.read_text())
        skip_fqns = {fqn.lower() for fqn in review.get("skip_objects", [])}
        if skip_fqns:
            print(f"Deprecated review loaded: {len(skip_fqns)} object(s) will be skipped.")

    wave_map = {
        "view": "wave_2_views",
        "function": "wave_3_functions",
        "procedure": "wave_4_procedures_triggers",
        "trigger": "wave_4_procedures_triggers",
        "table": "wave_1_tables",
    }

    # Skip unrecognised object types (e.g. 'unresolved' from DDL extraction)
    known_types = set(wave_map)

    pgloader_tables = [o["fqn"] for o in objs if o["type"] == "table"]
    manifest_entries = []

    non_table = [o for o in objs if o["type"] != "table"]
    print(f"Converting {len(non_table)} objects to PostgreSQL...")

    for o in non_table:
        schema = o.get("schema", "").lower()
        name = o["name"].lower()
        ddl = o["ddl"]
        obj_type = o["type"]
        if obj_type not in known_types:
            print(f"  SKIP (unknown type '{obj_type}')  {o.get('fqn', name)}")
            continue
        # Phase 3.6: skip objects the user chose not to migrate
        obj_fqn = o.get("fqn", name)
        if obj_fqn.lower() in skip_fqns:
            print(f"  SKIP (deprecated/excluded)  {obj_fqn}")
            continue
        wave = wave_map[obj_type]

        if obj_type == "view":
            converted, codes = convert_view(ddl)
        elif obj_type == "procedure":
            converted, codes = convert_procedure(ddl)
        elif obj_type == "function":
            converted, codes = convert_function(ddl)
        elif obj_type == "trigger":
            converted, codes = convert_trigger(ddl)
        else:
            converted, codes = ddl, []

        annotated = annotate_sql(converted, codes) if codes else converted
        outfile = base / wave / f"{schema}__{name}.sql"
        outfile.write_text(annotated, encoding="utf-8")
        manifest_entries.append({
            "fqn": o["fqn"],
            "type": obj_type,
            "output_file": str(outfile.relative_to(work_dir)),
            "ewi_codes": codes,
        })
        print(f"  {obj_type:<12} {o['fqn']} [{', '.join(codes)}]")

    manifest = {
        "pgloader_tables": pgloader_tables,
        "converted_objects": manifest_entries,
        "failed": [],
    }
    manifest_path = work_dir / "conversion" / "_conversion_report.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"\nConversion complete: {len(manifest_entries)} objects")
    print(f"Conversion manifest: {manifest_path}")


if __name__ == "__main__":
    main()
