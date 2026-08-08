#!/usr/bin/env python3
"""
convert_objects.py — Rule-based conversion of source DDL objects to PostgreSQL.

Supports:
  --source-type mssql   (default) T-SQL → PL/pgSQL
  --source-type mysql   MySQL/MariaDB → PL/pgSQL (uses same MSSQL rule path)
  --source-type oracle  PL/SQL → PL/pgSQL

Applies type-mapping rules and annotates output with SPG-EWI codes per the EWI
code catalog.  Writes wave-ordered output and a conversion manifest.
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

# Per-source-type rule loader cache — resolved lazily in _get_rules()
_rule_cache: dict = {}


def _get_rules(source_type: str = "mssql"):
    """Return a source-type-specific RuleLoader, cached by source_type."""
    st = source_type.lower()
    if st not in _rule_cache:
        _rule_cache[st] = get_loader(SKILL_DIR, st)
    return _rule_cache[st]


# Default MSSQL loader for backward compatibility (module-level usage in helpers)
_rules = get_loader(SKILL_DIR, "mssql")


# ===========================================================================
# MSSQL / T-SQL conversion helpers  (unchanged from previous version)
# ===========================================================================

def apply_type_mappings(ddl: str, source_type: str = "mssql") -> tuple[str, list[str]]:
    """Apply source-type-specific type mappings and function substitutions.

    Returns (converted_ddl, [ewiCodes]).
    """
    rules = _get_rules(source_type)
    codes_emit: list[str] = []

    any_type = False
    for rule in rules.type_mappings("pre_downcase"):
        new_ddl, n = re.subn(rule["pattern"], rule["replacement"], ddl, flags=re.IGNORECASE)
        if n > 0:
            ddl = new_ddl
            any_type = True
    if any_type:
        codes_emit.append("SPG-EWI-0001")

    any_func = False
    for rule in rules.function_substitutions():
        flags = rules._build_flags(rule.get("flags", ["IGNORECASE"]))
        replacement = rule.get("replacement") or ""
        new_ddl, n = re.subn(rule["pattern"], replacement, ddl, flags=flags)
        if n > 0:
            ddl = new_ddl
            any_func = True
            ewi = rule.get("ewi_code")
            if ewi and ewi not in codes_emit:
                codes_emit.append(ewi)
    if any_func and "SPG-EWI-0002" not in codes_emit:
        codes_emit.append("SPG-EWI-0002")

    return ddl, list(dict.fromkeys(codes_emit))


def strip_brackets(ddl: str) -> str:
    """Remove T-SQL square bracket quoting from identifiers."""
    return re.sub(r"\[([^\]]+)\]", r'"\1"', ddl)


def downcase_identifiers(ddl: str) -> str:
    """Downcase quoted identifiers.  Identifiers with spaces must stay double-quoted
    because PostgreSQL rejects bare multi-word tokens as column aliases."""
    def _lower(m: re.Match) -> str:
        ident = m.group(1).lower()
        # Keep double-quotes when the lowered name still contains spaces —
        # e.g. [vendor id] → "vendor id" (valid PG alias)
        if " " in ident:
            return f'"{ident}"'
        return ident
    return re.sub(r'"([^"]+)"', _lower, ddl)


_LOWERCASE_TOKENS = re.compile(
    r"(/\*.*?\*/)"        # block comment — preserve verbatim
    r"|(--[^\n]*)"        # line comment — preserve verbatim
    r"|('(?:''|[^'])*')"  # single-quoted string literal — preserve verbatim
    r'|("(?:""|[^"])*")'  # double-quoted identifier — preserve verbatim
    r"|([A-Za-z_]\w*)",   # bare word token — lowercase
    re.DOTALL,
)


def lowercase_sql_identifiers(sql: str) -> str:
    """Lowercase all bare unquoted SQL identifiers in converted SQL.

    Called as the final pass in convert_view/procedure/function/trigger so
    object references like Products, Orders, CustomerID become products,
    orders, customerid — matching the lowercase names parallel_deploy.py
    creates in the target SPG schema.  String literals, double-quoted
    identifiers (including multi-word names like "order details"), line
    comments, and block comments are preserved verbatim.
    """
    def _sub(m: re.Match) -> str:
        if m.group(5) is not None:  # bare word token
            return m.group(5).lower()
        return m.group(0)           # strings / comments / quoted identifiers
    return _LOWERCASE_TOKENS.sub(_sub, sql)


def fix_view_alias_syntax(ddl: str) -> str:
    """Fix T-SQL view-specific alias forms that are invalid in PostgreSQL."""
    ddl = re.sub(r"\bAS\s+'([^']+)'", lambda m: f'AS "{m.group(1)}"', ddl, flags=re.IGNORECASE)

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


def convert_view(ddl: str, source_type: str = "mssql") -> tuple[str, list[str]]:
    """Convert a T-SQL / MySQL view to PostgreSQL CREATE OR REPLACE VIEW."""
    codes = []
    ddl = re.sub(r"WITH\s+SCHEMABINDING\s*", "", ddl, flags=re.IGNORECASE)
    # MySQL: strip ALGORITHM=, DEFINER=, SQL SECURITY clauses before VIEW keyword.
    # mysqldump exports: CREATE ALGORITHM=UNDEFINED DEFINER=`x`@`y` SQL SECURITY DEFINER VIEW
    # Without this, the bare CREATE\s+VIEW regex below never matches.
    ddl = re.sub(
        r'CREATE\s+'
        r'(?:ALGORITHM\s*=\s*\w+\s+)?'
        r'(?:DEFINER\s*=\s*`[^`]*`@`[^`]*`\s+)?'
        r'(?:SQL\s+SECURITY\s+\w+\s+)?'
        r'VIEW',
        'CREATE OR REPLACE VIEW',
        ddl, flags=re.IGNORECASE,
    )
    ddl = re.sub(r"CREATE\s+VIEW", "CREATE OR REPLACE VIEW", ddl, flags=re.IGNORECASE)
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)
    ddl, tc = apply_type_mappings(ddl, source_type)
    codes.extend(tc)
    ddl = fix_view_alias_syntax(ddl)
    ddl = lowercase_sql_identifiers(ddl)
    ddl = ddl.strip().rstrip(";").rstrip()
    return ddl + ";", codes


def convert_procedure(ddl: str, source_type: str = "mssql") -> tuple[str, list[str]]:
    """Convert a T-SQL / MySQL stored procedure to a PL/pgSQL function."""
    codes = ["SPG-EWI-0004"]
    # Strip MySQL-specific DEFINER clause (no-op for MSSQL DDL)
    ddl = re.sub(
        r'CREATE\s+DEFINER\s*=\s*`[^`]*`@`[^`]*`\s+',
        'CREATE ', ddl, flags=re.IGNORECASE,
    )
    # Strip MySQL backtick quoting from procedure/function names
    ddl = re.sub(r'`(\w+)`', r'\1', ddl)
    # Strip MySQL integer display widths: INT(11) -> INT, TINYINT(1) -> TINYINT
    ddl = re.sub(
        r'\b(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT)\s*\(\d+\)',
        r'\1', ddl, flags=re.IGNORECASE,
    )
    ddl = strip_brackets(ddl)
    ddl, tc = apply_type_mappings(ddl, source_type)
    codes.extend(tc)
    ddl = downcase_identifiers(ddl)

    m_name = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:"([^"]+)"|([^\s(]+))',
        ddl, re.IGNORECASE,
    )
    if not m_name:
        return f"-- T-SQL procedure (manual conversion required)\n{ddl}", codes
    raw_name = (m_name.group(1) or m_name.group(2)).strip('"').lower()
    proc_name = f'"{raw_name}"' if ' ' in raw_name else raw_name

    m_paren = re.search(r'PROCEDURE\s+(?:"[^"]+"|[^\s(]+)\s*\(', ddl, re.IGNORECASE)
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
        name_end = m_name.end()
        as_body = re.search(r"(?:^|\n)\s*AS\s*(?:\n|$)", ddl[name_end:], re.IGNORECASE)
        if as_body:
            params_section = ddl[name_end: name_end + as_body.start()]
            body_start = name_end + as_body.end() - 1
        else:
            begin_m = re.search(r"(?:^|\n)\s*BEGIN\b", ddl[name_end:], re.IGNORECASE)
            if begin_m:
                params_section = ddl[name_end: name_end + begin_m.start()]
                body_start = name_end + begin_m.start()
            else:
                params_section = ""
                body_start = name_end
        params_raw = ", ".join(
            line.strip().rstrip(",")
            for line in params_section.splitlines()
            if re.search(r"@\w+", line)
        )

    body_text = ddl[body_start:].strip() if body_start > 0 else ddl
    body_m = re.search(r"(?:^|\n)\s*BEGIN\b\s*(.+)", body_text, re.IGNORECASE | re.DOTALL)
    if body_m:
        body = body_m.group(1).strip()
    else:
        body = body_text

    params = []
    for param in re.split(r",\s*", params_raw):
        param = param.strip()
        if not param:
            continue
        # MySQL parameters already carry IN/OUT/INOUT; detect and strip before re-adding
        mysql_mode_m = re.match(r'^(IN|OUT|INOUT)\s+', param, re.IGNORECASE)
        if mysql_mode_m:
            mysql_mode_str = mysql_mode_m.group(1).upper()
            mode = "INOUT" if mysql_mode_str == "INOUT" else ("OUT" if mysql_mode_str == "OUT" else "IN")
            param = param[mysql_mode_m.end():].strip()
        else:
            is_output = bool(re.search(r"\bOUTPUT\b", param, re.IGNORECASE))
            mode = "INOUT" if is_output else "IN"
        param = re.sub(r"\s+OUTPUT\b", "", param, flags=re.IGNORECASE)
        param = re.sub(r"\s+=\s*.+$", "", param)
        param = re.sub(r"\bAS\b\s+", "", param, flags=re.IGNORECASE)
        param = re.sub(r"@(\w+)", r"\1", param)
        params.append(f"    {mode} {param.strip()}")

    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\bSET\s+XACT_ABORT\s+ON\s*;?", "", body, flags=re.IGNORECASE)
    # Strip MySQL session variables (no PG equivalent)
    body = re.sub(r"\bUNIQUE_CHECKS\s*:?=\s*\d+\s*;?", "-- UNIQUE_CHECKS (MySQL only)", body, flags=re.IGNORECASE)
    body = re.sub(r"\bFOREIGN_KEY_CHECKS\s*:?=\s*\d+\s*;?", "-- FOREIGN_KEY_CHECKS (MySQL only)", body, flags=re.IGNORECASE)
    body = re.sub(r"\bSQL_MODE\s*:?=\s*[^;]+;?", "-- SQL_MODE (MySQL only)", body, flags=re.IGNORECASE)
    # Convert MySQL EXIT HANDLER to PL/pgSQL EXCEPTION WHEN OTHERS
    body = re.sub(
        r"DECLARE\s+EXIT\s+HANDLER\s+FOR\s+SQLEXCEPTION\s*(?:BEGIN)?\s*(.*?)\s*(?:END\s*;?)?",
        r"-- EXIT HANDLER converted:\n        EXCEPTION WHEN OTHERS THEN\n        \1",
        body, flags=re.IGNORECASE | re.DOTALL,
    )
    # Remove MySQL CONTINUE HANDLER FOR NOT FOUND (PL/pgSQL cursor handles this automatically)
    body = re.sub(
        r"DECLARE\s+CONTINUE\s+HANDLER\s+FOR\s+NOT\s+FOUND\s+[^;]+;?",
        "-- CONTINUE HANDLER FOR NOT FOUND (removed; PL/pgSQL cursor exits automatically)",
        body, flags=re.IGNORECASE,
    )
    # Convert MySQL proc_label:begin to just begin
    body = re.sub(r"^\s*\w+\s*:\s*begin\b", "BEGIN", body, flags=re.IGNORECASE | re.MULTILINE)
    body = re.sub(r"\bBEGIN\s+TRANSACTION\s*;?", "-- BEGIN TRANSACTION", body, flags=re.IGNORECASE)
    body = re.sub(r"\bCOMMIT\s+TRANSACTION\s*;?", "-- COMMIT", body, flags=re.IGNORECASE)
    body = re.sub(r"\bROLLBACK\s+TRANSACTION\s*;?", "-- ROLLBACK", body, flags=re.IGNORECASE)
    body = re.sub(r"\bBEGIN\s+TRY\b", "BEGIN  -- TRY block", body, flags=re.IGNORECASE)
    body = re.sub(r"\bEND\s+TRY\s+BEGIN\s+CATCH\b", "EXCEPTION WHEN OTHERS THEN", body, flags=re.IGNORECASE)
    body = re.sub(r"\bEND\s+TRY\b", "-- END TRY", body, flags=re.IGNORECASE)
    body = re.sub(r"\bEND\s+CATCH\b", "-- END CATCH", body, flags=re.IGNORECASE)
    body = re.sub(r"\bSET\s+@?(\w+)\s*=\s*", r"\1 := ", body, flags=re.IGNORECASE)
    body = re.sub(r"\bRAISERROR\s*\(([^,)]+),\s*\d+,\s*\d+\s*\)",
                  r"RAISE EXCEPTION '%', \1", body, flags=re.IGNORECASE)
    body = re.sub(r"\bTHROW\s+\d+,\s*([^,]+),\s*\d+\s*;?",
                  r"RAISE EXCEPTION '%', \1;", body, flags=re.IGNORECASE)
    body = re.sub(r"\bPRINT\s+(.+?);", r"RAISE NOTICE '%', \1;", body, flags=re.IGNORECASE)
    body = re.sub(r"@@TRANCOUNT", "0 /*@@TRANCOUNT*/", body, flags=re.IGNORECASE)
    body = re.sub(r"@PROCID", "NULL /*@PROCID*/", body, flags=re.IGNORECASE)
    body = re.sub(r"\bOBJECT_NAME\s*\([^)]+\)", "NULL /*OBJECT_NAME*/", body, flags=re.IGNORECASE)
    body = re.sub(r"\bEXEC\s+sp_executesql\s+", "EXECUTE ", body, flags=re.IGNORECASE)
    body = re.sub(r"\bCREATE\s+PROC\b", "CREATE OR REPLACE PROCEDURE", body, flags=re.IGNORECASE)
    declare_lines = []

    def _parse_var_decl(var: str, rest: str) -> str:
        rest = rest.strip().rstrip(",").rstrip(";").strip()
        rest = re.sub(r"^AS\s+", "", rest, flags=re.IGNORECASE)
        if re.match(r"TABLE\s*\(", rest, re.IGNORECASE) or rest.upper().strip() == "TABLE":
            return f"    -- SPG-EWI-0012: {var} TABLE variable — convert to CREATE TEMP TABLE"
        assign_m = re.match(r"(.+?)\s*=\s*(.+)$", rest, re.DOTALL)
        if assign_m:
            typ = assign_m.group(1).strip()
            default = assign_m.group(2).strip()
            return f"    {var} {typ} := {default};"
        return f"    {var} {rest};"

    def _process_multi_declare(body_text: str) -> str:
        pattern = re.compile(r"\bDECLARE\s*\n((?:\s+@\w+[^\n]+\n?)+)", re.IGNORECASE)
        def _replace_block(m: re.Match) -> str:
            block = m.group(1)
            for var_m in re.finditer(r"@(\w+)\s+(.+?)(?:,\s*$|;|\Z)", block, re.MULTILINE | re.IGNORECASE):
                declare_lines.append(_parse_var_decl(var_m.group(1), var_m.group(2)))
            return ""
        return pattern.sub(_replace_block, body_text)

    body = _process_multi_declare(body)
    body = re.sub(r"\bDECLARE\s+@(\w+)\s+(.+?)(?:;|\n)",
                  lambda m: (declare_lines.append(_parse_var_decl(m.group(1), m.group(2))), "")[1],
                  body, flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)
    body = re.sub(r"\bGO\b", "", body, flags=re.IGNORECASE)
    body = re.sub(r"^\s*BEGIN\s*\n?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\n?\s*END\s*;?\s*$", "", body.strip(), flags=re.IGNORECASE)

    param_str = ",\n".join(params) if params else ""
    declare_str = "\nDECLARE\n" + "\n".join(declare_lines) if declare_lines else ""
    result = f"""CREATE OR REPLACE PROCEDURE {proc_name}(
{param_str}
) LANGUAGE plpgsql AS $${declare_str}
BEGIN
    {body.strip()}
END;
$$;"""
    result = lowercase_sql_identifiers(result)
    return result, codes


def convert_function(ddl: str, source_type: str = "mssql") -> tuple[str, list[str]]:
    """Convert a T-SQL / MySQL scalar or table-valued function to PL/pgSQL."""
    codes = ["SPG-EWI-0004"]
    # Strip MySQL-specific DEFINER clause (no-op for MSSQL DDL)
    ddl = re.sub(
        r'CREATE\s+DEFINER\s*=\s*`[^`]*`@`[^`]*`\s+',
        'CREATE ', ddl, flags=re.IGNORECASE,
    )
    # Strip MySQL backtick quoting
    ddl = re.sub(r'`(\w+)`', r'\1', ddl)
    # Strip MySQL CHARSET/COLLATE from RETURNS clause
    ddl = re.sub(
        r'\bCHARSET\s+\w+(?:\s+COLLATE\s+\w+)?',
        '', ddl, flags=re.IGNORECASE,
    )
    # Strip MySQL integer/bool display widths: INT(6)->INT, SMALLINT(1)->SMALLINT, BOOLEAN(1)->BOOLEAN
    ddl = re.sub(
        r'\b(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|BOOLEAN)\s*\(\d+\)',
        r'\1', ddl, flags=re.IGNORECASE,
    )
    # Strip MySQL := assignment operator from RETURNS lines (use = in pg header)
    ddl = re.sub(r':=\s*last_insert_id\([^)]*\)', 'last_insert_id()', ddl)
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)
    ddl, tc = apply_type_mappings(ddl, source_type)
    codes.extend(tc)

    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([^\s(]+)", ddl, re.IGNORECASE)
    func_name = m.group(1).strip('"').lower() if m else "unknown_function"

    params_raw = ""
    m_start = re.search(r"FUNCTION\s+[^\s(]+\s*\(", ddl, re.IGNORECASE)
    end = 0
    if m_start:
        start = m_start.end()
        depth, end = 1, start
        while end < len(ddl) and depth > 0:
            if ddl[end] == '(':
                depth += 1
            elif ddl[end] == ')':
                depth -= 1
            end += 1
        params_raw = ddl[start:end-1].strip()

    params = []
    for param in re.split(r",\s*", params_raw, flags=re.DOTALL):
        param = param.strip()
        if not param:
            continue
        param = re.sub(r"=\s*.+$", "", param)
        param = re.sub(r"@(\w+)", r"\1", param)
        params.append(f"    IN {param.strip()}")

    ret_m = re.search(r"RETURNS\s+(\w[\w\s(),.]+?)(?:WITH\s+SCHEMABINDING|AS\b|BEGIN\b|\Z)",
                      ddl, re.IGNORECASE)
    return_type_raw = ret_m.group(1).strip() if ret_m else "void"
    is_stvf = return_type_raw.upper().startswith("TABLE")

    if is_stvf:
        return_body_m = re.search(r"\bRETURN\s+(.+)", ddl, re.IGNORECASE | re.DOTALL)
        return_body = return_body_m.group(1).strip().rstrip(";") if return_body_m else "SELECT NULL"
        return_body = re.sub(r"@(\w+)", r"\1", return_body)
        param_str = ",\n".join(params) if params else ""
        result = f"""-- ** SPG-EWI-0004: iTVF converted — specify return columns or use RETURNS TABLE(col type, ...) **
CREATE OR REPLACE FUNCTION {func_name}(
{param_str}
) RETURNS SETOF record LANGUAGE sql AS $$
    {return_body};
$$;"""
        result = lowercase_sql_identifiers(result)
        return result, codes

    fragment = ddl[end:] if m_start and end > 0 else ddl
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
                    pos += tm.start()
        body = fragment[body_inner_start:pos].strip()
    else:
        am = re.search(r'\bAS\b\s*(.+)', fragment, re.IGNORECASE | re.DOTALL)
        body = am.group(1).strip() if am else "-- SPG-EWI-0012: convert function body"
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)

    declare_lines = []
    def _extract_declare(m):
        var_name = m.group(1).lower()
        var_type = m.group(2).strip()
        for rule in _get_rules(source_type).type_mappings("declare"):
            var_type = re.sub(rule["pattern"], rule["replacement"], var_type, flags=re.IGNORECASE)
        declare_lines.append(f"    {var_name} {var_type};")
        return ""
    body = re.sub(r"DECLARE\s+@(\w+)\s+([^\n;]+);?", _extract_declare, body, flags=re.IGNORECASE)

    body = re.sub(r"\bSET\s+@(\w+)\s*=", r"\1 :=", body, flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)
    body = re.sub(r"\bSELECT\s+(\w+)\s*=\s*", r"SELECT \1 = ", body, flags=re.IGNORECASE)
    body = re.sub(r"\bIF\s+\(([^)]+)\)\n(\s+)", r"IF (\1) THEN\n\2", body, flags=re.IGNORECASE)
    body = re.sub(r"\bIF\s+([^\n(][^\n]+)\n(\s+)(?!THEN|--)", r"IF \1 THEN\n\2", body, flags=re.IGNORECASE)
    body = re.sub(r"\bELSE\s+IF\b", "ELSIF", body, flags=re.IGNORECASE)
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
    result = lowercase_sql_identifiers(result)
    return result, codes


def convert_trigger(ddl: str, source_type: str = "mssql") -> tuple[str, list[str]]:
    """Convert a T-SQL / MySQL trigger to a PL/pgSQL trigger function + CREATE TRIGGER."""
    codes = ["SPG-EWI-0005"]
    # Strip MySQL DEFINER clause and backtick quoting
    ddl = re.sub(
        r'CREATE\s+DEFINER\s*=\s*`[^`]*`@`[^`]*`\s+',
        'CREATE ', ddl, flags=re.IGNORECASE,
    )
    ddl = re.sub(r'`(\w+)`', r'\1', ddl)
    # MySQL trigger syntax: CREATE TRIGGER name BEFORE/AFTER event ON table FOR EACH ROW
    # Normalise to MSSQL-like: CREATE TRIGGER name ON table BEFORE/AFTER event
    ddl = re.sub(
        r'CREATE\s+TRIGGER\s+(\w+)\s+(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\S+)\s+FOR\s+EACH\s+ROW',
        r'CREATE TRIGGER \1 ON \4 \2 \3',
        ddl, flags=re.IGNORECASE,
    )
    # Fallback: triggers without explicit timing (default to AFTER)
    ddl = re.sub(
        r'CREATE\s+TRIGGER\s+(\w+)\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\S+)\s+FOR\s+EACH\s+ROW',
        r'CREATE TRIGGER \1 ON \3 AFTER \2',
        ddl, flags=re.IGNORECASE,
    )
    ddl = strip_brackets(ddl)
    ddl = downcase_identifiers(ddl)
    ddl, tc = apply_type_mappings(ddl, source_type)
    codes.extend(tc)

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
    timing = m.group(3).upper().replace("_", " ")
    event = m.group(4).upper()
    extra_event = m.group(5)
    events = event + (f" OR {extra_event.upper()}" if extra_event else "")

    body_m = re.search(r"(?:^AS|\bBEGIN\b)(.+)", ddl, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    body = body_m.group(1).strip() if body_m else "-- SPG-EWI-0012: convert trigger body"
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\bGO\b", "", body, flags=re.IGNORECASE)
    body = re.sub(r"^\s*BEGIN\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*END\s*;?\s*$", "", body.strip(), flags=re.IGNORECASE)
    body = re.sub(r"@(\w+)", r"\1", body)
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
    result = lowercase_sql_identifiers(result)
    return result, codes


# ===========================================================================
# Oracle / PL/SQL → PL/pgSQL conversion helpers
# ===========================================================================

# Common Oracle → PG function/syntax substitutions
_ORACLE_FUNC_SUBS: list[tuple[re.Pattern, str]] = [
    # Remove optimizer hints /*+ ... */
    (re.compile(r'/\*\+.*?\*/', re.DOTALL),                               ''),
    # SYSDATE / SYSTIMESTAMP
    (re.compile(r'\bSYSDATE\b', re.IGNORECASE),                           'NOW()'),
    (re.compile(r'\bSYSTIMESTAMP\b', re.IGNORECASE),                      'NOW()'),
    # SYS_GUID() → gen_random_uuid()
    (re.compile(r'\bSYS_GUID\s*\(\s*\)', re.IGNORECASE),                  'gen_random_uuid()'),
    # FROM DUAL / FROM SYS.DUAL  (leading whitespace absorbed to avoid double spaces)
    (re.compile(r'\s+FROM\s+(?:SYS\.)?DUAL\b', re.IGNORECASE),            ''),
    # NVL → COALESCE (same 2-arg signature)
    (re.compile(r'\bNVL\s*\(', re.IGNORECASE),                            'COALESCE('),
    # seq.NEXTVAL → NEXTVAL('seq')
    (re.compile(r'\b(\w+)\.NEXTVAL\b', re.IGNORECASE),                    r"NEXTVAL('\1')"),
    # seq.CURRVAL → CURRVAL('seq')
    (re.compile(r'\b(\w+)\.CURRVAL\b', re.IGNORECASE),                    r"CURRVAL('\1')"),
    # DBMS_OUTPUT.PUT_LINE(x); → RAISE NOTICE '%', x;
    (re.compile(r'\bDBMS_OUTPUT\.PUT_LINE\s*\(([^)]+)\)\s*;', re.IGNORECASE),
                                                                            r"RAISE NOTICE '%', \1;"),
    # RAISE_APPLICATION_ERROR(-20xxx, 'msg') → RAISE EXCEPTION ...
    (re.compile(r'\bRAISE_APPLICATION_ERROR\s*\(\s*-?\d+\s*,\s*([^)]+)\)\s*;?', re.IGNORECASE),
                                                                            r"RAISE EXCEPTION '%', \1;"),
    # EXECUTE IMMEDIATE → EXECUTE
    (re.compile(r'\bEXECUTE\s+IMMEDIATE\b', re.IGNORECASE),               'EXECUTE'),
    # NOVALIDATE (constraint hint) → remove
    (re.compile(r'\bNOVALIDATE\b', re.IGNORECASE),                        ''),
    # NOLOGGING → remove
    (re.compile(r'\s+NOLOGGING\b', re.IGNORECASE),                        ''),
    # END proc_name; → END; (must come last in subs to avoid premature strip)
    (re.compile(r'\bEND\s+\w+\s*;', re.IGNORECASE),                       'END;'),
]

# Oracle type → PG type substitutions for local variable declarations and params
_ORACLE_DECL_TYPE_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bNUMBER\b', re.IGNORECASE),                            'NUMERIC'),
    (re.compile(r'\bPLS_INTEGER\b', re.IGNORECASE),                       'INTEGER'),
    (re.compile(r'\bBINARY_INTEGER\b', re.IGNORECASE),                    'INTEGER'),
    (re.compile(r'\bSIMPLE_INTEGER\b', re.IGNORECASE),                    'INTEGER'),
    (re.compile(r'\bPOSITIVE\b', re.IGNORECASE),                          'INTEGER'),
    (re.compile(r'\bNATURAL\b', re.IGNORECASE),                           'INTEGER'),
    (re.compile(r'\bVARCHAR2\b', re.IGNORECASE),                          'TEXT'),
    (re.compile(r'\bNVARCHAR2\b', re.IGNORECASE),                         'TEXT'),
    (re.compile(r'\bCLOB\b', re.IGNORECASE),                              'TEXT'),
    (re.compile(r'\bNCLOB\b', re.IGNORECASE),                             'TEXT'),
    (re.compile(r'\bLONG\s+RAW\b', re.IGNORECASE),                        'BYTEA'),
    (re.compile(r'\bBLOB\b', re.IGNORECASE),                              'BYTEA'),
    (re.compile(r'\bRAW\b', re.IGNORECASE),                               'BYTEA'),
    (re.compile(r'\bLONG\b', re.IGNORECASE),                              'TEXT'),
    (re.compile(r'\bROWID\b', re.IGNORECASE),                             'TEXT'),
    (re.compile(r'\bUROWID\b', re.IGNORECASE),                            'TEXT'),
    (re.compile(r'\bXMLTYPE\b', re.IGNORECASE),                           'TEXT'),
    (re.compile(r'\bSYS_REFCURSOR\b', re.IGNORECASE),                     'REFCURSOR'),
    # TIMESTAMP WITH [LOCAL] TIME ZONE → TIMESTAMPTZ  (must precede bare TIMESTAMP)
    (re.compile(r'\bTIMESTAMP\s+WITH\s+(?:LOCAL\s+)?TIME\s+ZONE\b', re.IGNORECASE), 'TIMESTAMPTZ'),
    (re.compile(r'\bTIMESTAMP\b', re.IGNORECASE),                         'TIMESTAMPTZ'),
    # Oracle DATE includes time component
    (re.compile(r'\bDATE\b', re.IGNORECASE),                              'TIMESTAMPTZ'),
    (re.compile(r'\bINTERVAL\s+YEAR\s+TO\s+MONTH\b', re.IGNORECASE),     'INTERVAL'),
    (re.compile(r'\bINTERVAL\s+DAY\s+TO\s+SECOND\b', re.IGNORECASE),     'INTERVAL'),
]


def _apply_oracle_func_subs(ddl: str) -> tuple[str, list[str]]:
    """Apply Oracle-specific function/syntax substitutions. Returns (ddl, ewi_codes)."""
    codes: list[str] = []
    for pat, repl in _ORACLE_FUNC_SUBS:
        new_ddl, n = pat.subn(repl, ddl)
        if n > 0:
            ddl = new_ddl
            if "SPG-EWI-0002" not in codes:
                codes.append("SPG-EWI-0002")
    return ddl, codes


def _extract_balanced_parens(text: str, start: int) -> tuple[str, int]:
    """Extract content of balanced parens.

    start = position just after the opening '('.
    Returns (content_between_parens, pos_after_closing_paren).
    """
    depth, pos = 1, start
    while pos < len(text) and depth > 0:
        if text[pos] == '(':
            depth += 1
        elif text[pos] == ')':
            depth -= 1
        pos += 1
    return text[start:pos - 1], pos


def _convert_oracle_param(param: str) -> str:
    """Convert a single Oracle parameter declaration to PG format.

    Oracle: 'p_name IN VARCHAR2', 'p_out OUT NUMBER', 'p_io IN OUT DATE'
    PG:     'p_name TEXT', 'OUT p_out NUMERIC', 'INOUT p_io TIMESTAMPTZ'
    """
    param = param.strip().rstrip(',')
    if not param:
        return ""

    m_inout = re.match(r'^(\w+)\s+IN\s+OUT\s+(.+)$', param, re.IGNORECASE)
    m_out   = re.match(r'^(\w+)\s+OUT\s+(.+)$', param, re.IGNORECASE)
    m_in    = re.match(r'^(\w+)\s+IN\s+(.+)$', param, re.IGNORECASE)
    m_plain = re.match(r'^(\w+)\s+(.+)$', param, re.IGNORECASE)

    if m_inout:
        pname, ptype, mode = m_inout.group(1), m_inout.group(2).strip(), "INOUT"
    elif m_out:
        pname, ptype, mode = m_out.group(1), m_out.group(2).strip(), "OUT"
    elif m_in:
        pname, ptype, mode = m_in.group(1), m_in.group(2).strip(), "IN"
    elif m_plain:
        pname, ptype, mode = m_plain.group(1), m_plain.group(2).strip(), "IN"
    else:
        return f"    -- SPG-EWI-0012 param: {param}"

    # Strip DEFAULT/':=' and NOCOPY
    ptype = re.sub(r'\s+DEFAULT\s+.+$', '', ptype, flags=re.IGNORECASE)
    ptype = re.sub(r'\s*:=\s*.+$', '', ptype)
    ptype = re.sub(r'\bNOCOPY\s+', '', ptype, flags=re.IGNORECASE)
    # Strip CHAR unit qualifier: VARCHAR2(50 CHAR) → (50)
    ptype = re.sub(r'\((\d+)\s+CHAR\)', r'(\1)', ptype, flags=re.IGNORECASE)

    for pat, repl in _ORACLE_DECL_TYPE_SUBS:
        ptype = pat.sub(repl, ptype)
    ptype = ptype.strip().lower()

    if mode == "IN":
        return f"    {pname.lower()} {ptype}"
    return f"    {mode} {pname.lower()} {ptype}"


def _convert_oracle_local_decls(decls_text: str) -> list[str]:
    """Convert Oracle IS/AS declaration section to PL/pgSQL DECLARE lines."""
    lines: list[str] = []
    # Split on semicolons at end of line to get individual declarations
    stmts = re.split(r';\s*(?=\s*\n|\s*$)', decls_text.strip())

    for stmt in stmts:
        stmt = stmt.strip()
        if not stmt or stmt.startswith('--'):
            if stmt:
                lines.append(f"    {stmt}")
            continue

        stmt_norm = ' '.join(stmt.split())  # collapse whitespace

        if re.match(r'CURSOR\b', stmt_norm, re.IGNORECASE):
            lines.append(f"    -- SPG-EWI-0012 CURSOR: {stmt_norm};  -- convert to FOR loop or REFCURSOR")
            continue
        if re.match(r'TYPE\b', stmt_norm, re.IGNORECASE):
            lines.append(f"    -- SPG-EWI-0012 TYPE: {stmt_norm};  -- convert collection/record type")
            continue
        if re.search(r'\bEXCEPTION\s*$', stmt_norm, re.IGNORECASE):
            lines.append(f"    -- SPG-EWI-0012 EXCEPTION_VAR: {stmt_norm};  -- define as needed")
            continue
        if re.match(r'PRAGMA\b', stmt_norm, re.IGNORECASE):
            lines.append(f"    -- PRAGMA: {stmt_norm};  -- review Oracle-specific pragma")
            continue

        # Extract DEFAULT / := value
        default_m = re.search(r'\s+DEFAULT\s+(.+)$|\s*:=\s*(.+)$', stmt_norm, re.IGNORECASE)
        if default_m:
            default_val = (default_m.group(1) or default_m.group(2)).strip()
            base_decl = stmt_norm[:default_m.start()].strip()
        else:
            default_val = None
            base_decl = stmt_norm

        parts = base_decl.split(None, 1)
        if len(parts) < 2:
            lines.append(f"    -- SPG-EWI-0012: {stmt_norm};")
            continue

        vname = parts[0].lower()
        vtype = parts[1].strip()

        # Apply type substitutions to the type portion only
        for pat, repl in _ORACLE_DECL_TYPE_SUBS:
            vtype = pat.sub(repl, vtype)
        vtype = re.sub(r'\((\d+)\s+CHAR\)', r'(\1)', vtype, flags=re.IGNORECASE)
        vtype = vtype.lower()

        if default_val:
            lines.append(f"    {vname} {vtype} := {default_val};")
        else:
            lines.append(f"    {vname} {vtype};")

    return lines


def _split_oracle_body_exception(body_and_rest: str) -> tuple[str, str]:
    """Split Oracle body text at the top-level EXCEPTION keyword.

    Returns (body_text, exception_block) where exception_block starts with EXCEPTION.
    """
    exc_m = re.search(r'\bEXCEPTION\b', body_and_rest, re.IGNORECASE)
    end_m = re.search(r'\bEND\s*(?:\w+\s*)?;\s*$', body_and_rest, re.IGNORECASE | re.MULTILINE)

    if exc_m:
        body = body_and_rest[:exc_m.start()].strip()
        exc_end = end_m.start() if end_m else len(body_and_rest)
        exception_block = body_and_rest[exc_m.start():exc_end].strip()
    elif end_m:
        body = body_and_rest[:end_m.start()].strip()
        exception_block = ''
    else:
        body = body_and_rest.strip()
        exception_block = ''

    return body, exception_block


def _oracle_check_complex(ddl: str, codes: list[str]) -> None:
    """Append EWI codes for Oracle patterns that need LLM review."""
    checks = [
        (r'\bBULK\s+COLLECT\b', "SPG-EWI-0008"),
        (r'\bFORALL\b',          "SPG-EWI-0008"),
        (r'\bCONNECT\s+BY\b',   "SPG-EWI-0007"),
        (r'\bROWNUM\b',          "SPG-EWI-0006"),
        (r'%TYPE\b',             "SPG-EWI-0009"),
        (r'%ROWTYPE\b',          "SPG-EWI-0009"),
    ]
    for pattern, ewi in checks:
        if re.search(pattern, ddl, re.IGNORECASE) and ewi not in codes:
            codes.append(ewi)


def convert_oracle_view(ddl: str) -> tuple[str, list[str]]:
    """Convert an Oracle view to PostgreSQL CREATE OR REPLACE VIEW."""
    codes: list[str] = []
    ddl = re.sub(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b',
                 'CREATE OR REPLACE VIEW', ddl, flags=re.IGNORECASE)
    # Remove WITH READ ONLY constraint
    ddl = re.sub(r'\bWITH\s+READ\s+ONLY\b', '', ddl, flags=re.IGNORECASE)
    ddl, fc = _apply_oracle_func_subs(ddl)
    codes.extend(fc)
    _oracle_check_complex(ddl, codes)
    ddl = ddl.strip().rstrip(';').rstrip() + ';'
    return ddl, codes


def convert_oracle_procedure(ddl: str) -> tuple[str, list[str]]:
    """Convert an Oracle PL/SQL procedure to a PL/pgSQL procedure."""
    codes = ["SPG-EWI-0004"]
    ddl, fc = _apply_oracle_func_subs(ddl)
    codes.extend(fc)
    _oracle_check_complex(ddl, codes)

    name_m = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:"([^"]+)"|(\S+))',
        ddl, re.IGNORECASE,
    )
    if not name_m:
        return f"-- Oracle procedure (manual conversion required)\n{ddl}", codes
        _raw_oracle_name = re.sub(r"""["'(]""", '', (name_m.group(1) or name_m.group(2))).lower()
    proc_name = f'"{_raw_oracle_name}"' if ' ' in _raw_oracle_name else _raw_oracle_name

    # Extract parameters (balanced parens)
    paren_m = re.search(r'PROCEDURE\s+(?:"[^"]+"|[^\s(]+)\s*\(', ddl, re.IGNORECASE)
    if paren_m:
        params_raw, after_paren_pos = _extract_balanced_parens(ddl, paren_m.end())
        post_params = ddl[after_paren_pos:]
    else:
        params_raw = ""
        post_params = ddl[name_m.end():]

    # Find IS/AS separator
    is_as_m = re.search(r'\b(IS|AS)\b', post_params, re.IGNORECASE)
    after_is_as = post_params[is_as_m.end():] if is_as_m else post_params

    # Find BEGIN
    begin_m = re.search(r'\bBEGIN\b', after_is_as, re.IGNORECASE)
    if begin_m:
        local_decls_text = after_is_as[:begin_m.start()].strip()
        body_and_rest = after_is_as[begin_m.end():]
    else:
        local_decls_text = ''
        body_and_rest = after_is_as

    body, exception_block = _split_oracle_body_exception(body_and_rest)

    # Convert parameters — split on commas not inside parens
    param_list = re.split(r',\s*(?=[A-Za-z_])', params_raw)
    params = [_convert_oracle_param(p) for p in param_list if p.strip()]
    params = [p for p in params if p]

    decl_lines = _convert_oracle_local_decls(local_decls_text) if local_decls_text else []

    param_str = ",\n".join(params)
    declare_str = "\nDECLARE\n" + "\n".join(decl_lines) if decl_lines else ""
    exc_str = f"\n{exception_block}\n" if exception_block else ""

    result = (
        f"CREATE OR REPLACE PROCEDURE {proc_name}(\n"
        f"{param_str}\n"
        f") LANGUAGE plpgsql AS $${declare_str}\n"
        f"BEGIN\n"
        f"    {body.strip()}\n"
        f"{exc_str}"
        f"END;\n"
        f"$$;"
    )
    return result, codes


def convert_oracle_function(ddl: str) -> tuple[str, list[str]]:
    """Convert an Oracle PL/SQL function to a PL/pgSQL function."""
    codes = ["SPG-EWI-0004"]
    ddl, fc = _apply_oracle_func_subs(ddl)
    codes.extend(fc)
    _oracle_check_complex(ddl, codes)

    name_m = re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\S+)', ddl, re.IGNORECASE)
    if not name_m:
        return f"-- Oracle function (manual conversion required)\n{ddl}", codes
    func_name = re.sub(r'["\'\(]', '', name_m.group(1)).lower()

    paren_m = re.search(r'FUNCTION\s+\S+\s*\(', ddl, re.IGNORECASE)
    if paren_m:
        params_raw, after_paren_pos = _extract_balanced_parens(ddl, paren_m.end())
        post_params = ddl[after_paren_pos:]
    else:
        params_raw = ""
        post_params = ddl[name_m.end():]

    # Extract RETURN type (before IS/AS)
    ret_m = re.search(r'\bRETURN\s+(\S+)', post_params, re.IGNORECASE)
    return_type_raw = ret_m.group(1).strip().rstrip(';') if ret_m else "void"
    # Map return type through Oracle type table
    rt = return_type_raw
    for pat, repl in _ORACLE_DECL_TYPE_SUBS:
        rt = pat.sub(repl, rt)
    return_type = rt.lower()

    # Find IS/AS
    is_as_m = re.search(r'\b(IS|AS)\b', post_params, re.IGNORECASE)
    after_is_as = post_params[is_as_m.end():] if is_as_m else post_params

    begin_m = re.search(r'\bBEGIN\b', after_is_as, re.IGNORECASE)
    if begin_m:
        local_decls_text = after_is_as[:begin_m.start()].strip()
        body_and_rest = after_is_as[begin_m.end():]
    else:
        local_decls_text = ''
        body_and_rest = after_is_as

    body, exception_block = _split_oracle_body_exception(body_and_rest)

    param_list = re.split(r',\s*(?=[A-Za-z_])', params_raw)
    params = [_convert_oracle_param(p) for p in param_list if p.strip()]
    params = [p for p in params if p]

    decl_lines = _convert_oracle_local_decls(local_decls_text) if local_decls_text else []

    param_str = ",\n".join(params)
    declare_str = "\nDECLARE\n" + "\n".join(decl_lines) if decl_lines else ""
    exc_str = f"\n{exception_block}\n" if exception_block else ""

    result = (
        f"CREATE OR REPLACE FUNCTION {func_name}(\n"
        f"{param_str}\n"
        f") RETURNS {return_type} LANGUAGE plpgsql AS $${declare_str}\n"
        f"BEGIN\n"
        f"    {body.strip()}\n"
        f"{exc_str}"
        f"END;\n"
        f"$$;"
    )
    return result, codes


def convert_oracle_trigger(ddl: str) -> tuple[str, list[str]]:
    """Convert an Oracle trigger to a PL/pgSQL trigger function + CREATE TRIGGER."""
    codes = ["SPG-EWI-0005"]

    # :NEW.col → NEW.col, :OLD.col → OLD.col
    ddl = re.sub(r':NEW\.', 'NEW.', ddl, flags=re.IGNORECASE)
    ddl = re.sub(r':OLD\.', 'OLD.', ddl, flags=re.IGNORECASE)

    ddl, fc = _apply_oracle_func_subs(ddl)
    codes.extend(fc)

    # Oracle trigger signature: CREATE [OR REPLACE] TRIGGER name timing event ON table [FOR EACH ROW]
    m = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER\s+([^\s]+)\s*\n?\s*'
        r'(BEFORE|AFTER|INSTEAD\s+OF)\s+'
        r'(INSERT|UPDATE|DELETE)(?:\s+OR\s+(INSERT|UPDATE|DELETE))?(?:\s+OR\s+(INSERT|UPDATE|DELETE))?\s+'
        r'ON\s+([^\s\n]+)',
        ddl, re.IGNORECASE,
    )
    if not m:
        return f"-- Oracle trigger (manual conversion required)\n-- SPG-EWI-0005: review required\n{ddl}", codes

    trig_name = re.sub(r'["\']', '', m.group(1)).lower()
    timing = m.group(2).upper()
    events = m.group(3).upper()
    if m.group(4):
        events += f" OR {m.group(4).upper()}"
    if m.group(5):
        events += f" OR {m.group(5).upper()}"
    table_name = re.sub(r'["\']', '', m.group(6)).lower()

    body_m = re.search(r'\bBEGIN\b(.+)', ddl, re.IGNORECASE | re.DOTALL)
    if body_m:
        body, exception_block = _split_oracle_body_exception(body_m.group(1))
    else:
        body = "-- SPG-EWI-0012: trigger body"
        exception_block = ""

    fn_name = f"{trig_name.split('.')[-1]}_fn"
    exc_str = f"\n{exception_block}" if exception_block else ""

    result = (
        f"CREATE OR REPLACE FUNCTION {fn_name}()\n"
        f"RETURNS TRIGGER LANGUAGE plpgsql AS $$\n"
        f"BEGIN\n"
        f"    {body.strip()}\n"
        f"    RETURN NEW;\n"
        f"{exc_str}"
        f"END;\n"
        f"$$;\n\n"
        f"CREATE TRIGGER {trig_name.split('.')[-1]}\n"
        f"{timing} {events} ON {table_name}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {fn_name}();"
    )
    return result, codes


# ===========================================================================
# Main
# ===========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Rule-based conversion of source DDL objects to PostgreSQL"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--ddl-objects", default=None,
                        help="Path to ddl_objects.json (default: <work-dir>/ddl_objects.json)")
    parser.add_argument("--source-type", default="mssql",
                        choices=["mssql", "mysql", "mariadb", "oracle"],
                        help="Source database type (default: mssql)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    ddl_path = Path(args.ddl_objects) if args.ddl_objects else work_dir / "ddl_objects.json"
    source_type = args.source_type
    objs = json.loads(ddl_path.read_text())
    # Support both flat list and {"objects": [...]} wrapper
    if isinstance(objs, dict):
        objs = objs.get("objects", [])
    # Normalize type to lowercase so all scripts are consistent
    for o in objs:
        if "type" in o:
            o["type"] = o["type"].lower()
    base = work_dir / "conversion" / "postgres"

    # ── Load deprecated review dispositions (Phase 3.6) ──────────────────
    # IMPORTANT: always derive skip set from groups[].disposition, which
    # reflects the user's ask_user_question decisions.  Do NOT read
    # skip_objects directly — that flat list is written at scan time and
    # is not updated when the user changes a group from "skip" to "migrate".

    # ── Read assessment summary for user-prompted choices (Phase 3.5) ────
    # tinyint1_mapping: "boolean" | "smallint" — set by skill based on user answer
    tinyint1_as_boolean: bool = True  # default: MySQL convention
    assessment_path = work_dir / "assessment" / "assessment_summary.json"
    if assessment_path.exists():
        try:
            assessment_summary = json.loads(assessment_path.read_text())
            mapping = assessment_summary.get("tinyint1_mapping", "boolean")
            tinyint1_as_boolean = (mapping == "boolean")
        except Exception:
            pass
    skip_fqns: set[str] = set()
    review_path = work_dir / "deprecated" / "deprecated_review.json"
    if review_path.exists():
        review = json.loads(review_path.read_text())
        for group in review.get("groups", {}).values():
            if group.get("disposition") == "skip":
                for fqn in group.get("object_fqns", []):
                    skip_fqns.add(fqn.lower())
        # Fallback: include skip_objects only for groups NOT listed in groups{}
        # (pre-3.6 workspaces that wrote skip_objects without groups structure)
        group_fqns = {
            fqn.lower()
            for g in review.get("groups", {}).values()
            for fqn in g.get("object_fqns", [])
        }
        for fqn in review.get("skip_objects", []):
            if fqn.lower() not in group_fqns:
                skip_fqns.add(fqn.lower())
        if skip_fqns:
            print(f"Deprecated review loaded: {len(skip_fqns)} object(s) will be skipped (disposition=skip).")

    wave_map = {
        "view":      "wave_2_views",
        "function":  "wave_3_functions",
        "procedure": "wave_4_procedures_triggers",
        "trigger":   "wave_4_procedures_triggers",
        "table":     "wave_1_tables",
    }
    known_types = set(wave_map)

    # For MSSQL/MySQL: tables go to pgloader
    # For Oracle: tables go to parallel_deploy.py (catalog path) — skip here
    catalog_tables: list[str] = []
    oracle_catalog_tables: list[str] = []
    manifest_entries: list[dict] = []

    if source_type in ("mssql", "mysql", "mariadb"):
        catalog_tables = [o["fqn"] for o in objs if o["type"] == "table"]

    if source_type == "oracle":
        oracle_catalog_tables = [o["fqn"] for o in objs if o["type"] == "table"]
        print(f"Source: oracle — {len(oracle_catalog_tables)} table(s) handled by parallel_deploy.py (catalog path)")

    non_table = [o for o in objs if o["type"] != "table"]
    print(f"Converting {len(non_table)} objects to PostgreSQL (source: {source_type})...")

    for o in non_table:
        schema = o.get("schema", "").lower()
        name = o["name"].lower()
        ddl = o["ddl"]
        obj_type = o["type"]

        if obj_type not in known_types:
            print(f"  SKIP (unknown type '{obj_type}')  {o.get('fqn', name)}")
            continue
        obj_fqn = o.get("fqn", name)
        if obj_fqn.lower() in skip_fqns:
            print(f"  SKIP (deprecated/excluded)  {obj_fqn}")
            continue

        wave = wave_map[obj_type]

        # ── MySQL/MariaDB: apply TINYINT(1) mapping before conversion ─────
        # User's choice (from Phase 3.5 assessment prompt) determines whether
        # TINYINT(1) maps to BOOLEAN or stays as TINYINT (→ SMALLINT via YAML).
        if source_type in ("mysql", "mariadb") and tinyint1_as_boolean:
            ddl = re.sub(
                r'\bTINYINT\s*\(\s*1\s*\)', 'BOOLEAN', ddl, flags=re.IGNORECASE
            )

        # ── Dispatch by source type ───────────────────────────────────────
        if source_type == "oracle":
            if obj_type == "view":
                converted, codes = convert_oracle_view(ddl)
            elif obj_type == "procedure":
                converted, codes = convert_oracle_procedure(ddl)
            elif obj_type == "function":
                converted, codes = convert_oracle_function(ddl)
            elif obj_type == "trigger":
                converted, codes = convert_oracle_trigger(ddl)
            else:
                converted, codes = ddl, []
        else:
            # MSSQL / MySQL / MariaDB — existing T-SQL path
            if obj_type == "view":
                converted, codes = convert_view(ddl, source_type)
            elif obj_type == "procedure":
                converted, codes = convert_procedure(ddl, source_type)
            elif obj_type == "function":
                converted, codes = convert_function(ddl, source_type)
            elif obj_type == "trigger":
                converted, codes = convert_trigger(ddl, source_type)
            else:
                converted, codes = ddl, []

        annotated = annotate_sql(converted, codes) if codes else converted
        outfile = base / wave / f"{schema}__{name}.sql"
        outfile.parent.mkdir(parents=True, exist_ok=True)
        outfile.write_text(annotated, encoding="utf-8")
        manifest_entries.append({
            "fqn": o["fqn"],
            "type": obj_type,
            "output_file": str(outfile.relative_to(work_dir)),
            "ewi_codes": codes,
        })
        print(f"  {obj_type:<12} {o['fqn']} [{', '.join(codes)}]")

    manifest = {
        "source_type": source_type,
        "catalog_tables": catalog_tables,
        "oracle_catalog_tables": oracle_catalog_tables,
        "converted_objects": manifest_entries,
        "failed": [],
    }
    manifest_path = work_dir / "conversion" / "_conversion_report.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Update the object manifest with conversion state per object
    try:
        from spgloader.manifest import ObjectManifest
        obj_manifest = ObjectManifest(work_dir)
        _NEEDS_REPAIR_CODES = {"SPG-EWI-0004", "SPG-EWI-0007", "SPG-EWI-0008"}
        for entry in manifest_entries:
            fqn = entry["fqn"]
            codes = set(entry.get("ewi_codes", []))
            status = "failed" if codes & _NEEDS_REPAIR_CODES else "completed"
            obj_manifest.set_converted(fqn, status,
                                       artifact=entry.get("output_file", ""),
                                       ewi_codes=entry.get("ewi_codes", []))
        # Mark tables as extraction-completed + deployment-pending (tables skip conversion)
        for fqn in catalog_tables + oracle_catalog_tables:
            obj_manifest.set_converted(fqn, "skipped")
        obj_manifest.save()
        n_clean = sum(1 for e in manifest_entries if not (set(e.get("ewi_codes",[])) & _NEEDS_REPAIR_CODES))
        print(f"  object_manifest  {len(manifest_entries)} updated (clean={n_clean}, needs_repair={len(manifest_entries)-n_clean})")
    except Exception as e:
        print(f"  object_manifest  (skipped: {e})", file=sys.stderr)

    # Write _conversion_metrics.json — accuracy tracking for consistent quality monitoring
    _NEEDS_REPAIR = {"SPG-EWI-0004", "SPG-EWI-0007", "SPG-EWI-0008"}
    _NEEDS_MANUAL = {"SPG-EWI-0012"}
    metrics: dict = {"source_type": source_type, "by_type": {}, "totals": {}}
    total_first_pass = total_needs_repair = total_needs_manual = 0
    for entry in manifest_entries:
        otype = entry["type"]
        codes_set = set(entry.get("ewi_codes", []))
        is_manual  = bool(codes_set & _NEEDS_MANUAL)
        is_repair  = bool(codes_set & _NEEDS_REPAIR) and not is_manual
        is_clean   = not is_manual and not is_repair
        bt = metrics["by_type"].setdefault(otype, {"total": 0, "first_pass_clean": 0, "needs_llm_repair": 0, "needs_manual": 0})
        bt["total"] += 1
        if is_clean:   bt["first_pass_clean"]  += 1; total_first_pass   += 1
        if is_repair:  bt["needs_llm_repair"]  += 1; total_needs_repair += 1
        if is_manual:  bt["needs_manual"]       += 1; total_needs_manual += 1
    total = len(manifest_entries)
    metrics["totals"] = {
        "objects": total,
        "first_pass_clean":  total_first_pass,
        "needs_llm_repair":  total_needs_repair,
        "needs_manual":      total_needs_manual,
        "first_pass_rate":   round(total_first_pass  / total * 100) if total else 100,
        "manual_rate":       round(total_needs_manual / total * 100) if total else 0,
    }
    metrics_path = work_dir / "conversion" / "_conversion_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(f"Conversion metrics:  {metrics_path}")
    print(f"  First-pass clean: {total_first_pass}/{total} ({metrics['totals']['first_pass_rate']}%)")
    if total_needs_repair:
        print(f"  Needs LLM repair: {total_needs_repair}")
    if total_needs_manual:
        print(f"  Needs manual:     {total_needs_manual} (SPG-EWI-0012 — check before deploying)")

    print(f"\nConversion complete: {len(manifest_entries)} objects")
    print(f"Conversion manifest: {manifest_path}")


if __name__ == "__main__":
    main()
