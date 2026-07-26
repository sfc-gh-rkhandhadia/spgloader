#!/usr/bin/env python3
"""
convert_procedures.py — Convert T-SQL stored procedures to PL/pgSQL.

Two-phase approach:
  1. Regex pre-processing (keyword/function substitutions)
  2. Stack-based structural rewriter (IF/BEGIN/END → IF/THEN/END IF)

Usage:
  python convert_procedures.py --work-dir ~/.spgloader/20260101_120000
"""
import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Type mappings (T-SQL → PostgreSQL)
# ---------------------------------------------------------------------------
TYPE_MAP = {
    'nvarchar': 'text', 'varchar': 'text', 'nchar': 'text', 'char': 'text',
    'ntext': 'text', 'bit': 'boolean', 'tinyint': 'smallint',
    'int': 'integer', 'bigint': 'bigint', 'smallint': 'smallint',
    'decimal': 'numeric', 'money': 'numeric(19,4)', 'smallmoney': 'numeric(10,4)',
    'float': 'double precision', 'real': 'real',
    'datetime': 'timestamp', 'datetime2': 'timestamp',
    'smalldatetime': 'timestamp', 'date': 'date', 'time': 'time',
    'uniqueidentifier': 'uuid', 'varbinary': 'bytea', 'binary': 'bytea',
    'image': 'bytea', 'xml': 'xml', 'sql_variant': 'text',
}

PARAM_RE = re.compile(
    r'@(\w+)\s+((?:n?var)?(?:char|binary|decimal|numeric|float|real|int|bigint|'
    r'smallint|tinyint|bit|datetime[\w]*|date|time|money|smallmoney|'
    r'uniqueidentifier|image|text|xml|sql_variant|ntext|nchar|varbinary|binary)'
    r'(?:\s*\([^)]+\))?)'
    r'(?:\s*=\s*[^,\n)]+)?'
    r'(\s+OUTPUT)?',
    re.IGNORECASE
)


def map_type(tsql_type: str) -> str:
    base = re.sub(r'\s*\([^)]+\)', '', tsql_type.strip()).lower()
    pg = TYPE_MAP.get(base, tsql_type.lower())
    if base in ('decimal', 'numeric'):
        m = re.search(r'\(([^)]+)\)', tsql_type)
        if m:
            pg = f'numeric({m.group(1)})'
    return pg


def extract_params(param_block: str) -> list[dict]:
    params = []
    for m in PARAM_RE.finditer(param_block):
        name = m.group(1).lower()
        pg_type = map_type(m.group(2))
        is_output = bool(m.group(3))
        params.append({'name': name, 'type': pg_type, 'output': is_output})
    return params


# ---------------------------------------------------------------------------
# Phase 1: regex pre-processing
# ---------------------------------------------------------------------------
def preprocess(s: str) -> str:
    """Apply keyword-level T-SQL → PG substitutions."""
    # Strip T-SQL pragmas
    for pat in [r'\bSET\s+NOCOUNT\s+\w+\b\s*;?', r'\bSET\s+XACT_ABORT\s+\w+\b\s*;?',
                r'\bSET\s+ANSI_NULLS\s+\w+\b\s*;?', r'\bSET\s+QUOTED_IDENTIFIER\s+\w+\b\s*;?',
                r'\bWITH\s+RECOMPILE\b', r'\bGO\b\s*$']:
        s = re.sub(pat, '', s, flags=re.IGNORECASE | re.MULTILINE)

    # System tables
    s = re.sub(r'\bsysobjects\b', 'pg_class', s, flags=re.IGNORECASE)
    s = re.sub(r'\bsysindexes\b', 'pg_indexes', s, flags=re.IGNORECASE)
    # Functions
    s = re.sub(r'\bISNULL\s*\(', 'COALESCE(', s, flags=re.IGNORECASE)
    s = re.sub(r'\bGETDATE\s*\(\s*\)', 'NOW()', s, flags=re.IGNORECASE)
    s = re.sub(r'\bGETUTCDATE\s*\(\s*\)', "(NOW() AT TIME ZONE 'UTC')", s, flags=re.IGNORECASE)
    s = re.sub(r'\bSYSDATETIME\s*\(\s*\)', 'NOW()', s, flags=re.IGNORECASE)
    s = re.sub(r'\bNEWID\s*\(\s*\)', 'gen_random_uuid()', s, flags=re.IGNORECASE)
    s = re.sub(r'\bLEN\s*\(', 'LENGTH(', s, flags=re.IGNORECASE)
    s = re.sub(r'\bDATALENGTH\s*\(', 'OCTET_LENGTH(', s, flags=re.IGNORECASE)
    # DATEADD(unit, n, expr) → (expr + n * INTERVAL '1 unit')
    def dateadd_repl(m):
        unit, n, expr = m.group(1), m.group(2).strip(), m.group(3).strip()
        unit_map = {'year':'year','yy':'year','yyyy':'year','month':'month','mm':'month',
                    'day':'day','dd':'day','d':'day','hour':'hour','hh':'hour',
                    'minute':'minute','mi':'minute','n':'minute','second':'second','ss':'second'}
        pu = unit_map.get(unit.lower(), unit.lower())
        return f"({expr} + {n} * INTERVAL '1 {pu}')"
    s = re.sub(r'\bDATEADD\s*\(\s*(\w+)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
               dateadd_repl, s, flags=re.IGNORECASE)
    # DATEDIFF(unit, a, b) → EXTRACT(EPOCH FROM b-a) cast
    def datediff_repl(m):
        unit, a, b = m.group(1).lower(), m.group(2).strip(), m.group(3).strip()
        unit_map = {'year':'year','yy':'year','yyyy':'year','month':'month','mm':'month',
                    'day':'day','dd':'day','d':'day','hour':'hour','hh':'hour',
                    'second':'second','ss':'second','minute':'minute','mi':'minute'}
        pu = unit_map.get(unit, unit)
        if pu in ('day',):
            return f"EXTRACT(DAY FROM ({b}::timestamp - {a}::timestamp))"
        elif pu in ('second',):
            return f"EXTRACT(EPOCH FROM ({b}::timestamp - {a}::timestamp))"
        else:
            return f"(DATE_PART('{pu}', {b}::timestamp) - DATE_PART('{pu}', {a}::timestamp))"
    s = re.sub(r'\bDATEDIFF\s*\(\s*(\w+)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
               datediff_repl, s, flags=re.IGNORECASE)
    # NEWSEQUENTIALID → gen_random_uuid
    s = re.sub(r'\bNEWSEQUENTIALID\s*\(\s*\)', 'gen_random_uuid()', s, flags=re.IGNORECASE)
    # @@ROWCOUNT/@@ERROR/@@IDENTITY
    s = re.sub(r'\b@@ROWCOUNT\b', '_row_count', s, flags=re.IGNORECASE)
    s = re.sub(r'\b@@ERROR\b', '0', s, flags=re.IGNORECASE)
    s = re.sub(r'\b@@IDENTITY\b', 'lastval()', s, flags=re.IGNORECASE)
    s = re.sub(r'\bSCOPE_IDENTITY\s*\(\s*\)', 'lastval()', s, flags=re.IGNORECASE)
    # N'string' → 'string'
    s = re.sub(r"\bN'", "'", s)
    # String concat: ' + ' → ' || '  (text concat only)
    s = re.sub(r"('\s*)\+\s*(')", r"\1 || \2", s)
    # @@global_var handling
    s = re.sub(r'\b@@trancount\b', '0 /* @@trancount not available */', s, flags=re.IGNORECASE)
    s = re.sub(r'\b@@servername\b', 'current_setting(\'server_version_num\')', s, flags=re.IGNORECASE)
    s = re.sub(r'\b@@version\b', 'version()', s, flags=re.IGNORECASE)
    # @var → lowercase var name (must do BEFORE other transforms)
    s = re.sub(r'@(\w+)', lambda m: m.group(1).lower(), s)
    # SET var = expr → var := expr (standalone assignment only, NOT UPDATE...SET)
    # Step 1: temporarily protect UPDATE...SET clauses from the SET→:= conversion
    s = re.sub(r'(\bUPDATE\b[^\n]+\n\s*)SET\b', r'\1__UPDATE_SET__', s, flags=re.IGNORECASE)
    # Step 2: convert standalone SET var = expr → var := expr
    s = re.sub(r'(?<![A-Z\w])SET\s+(\w+)\s*=\s*(?!\s*SELECT\b)', r'\1 := ', s, flags=re.IGNORECASE)
    # Step 3: restore UPDATE SET
    s = s.replace('__UPDATE_SET__', 'SET')
    # SELECT TOP (1) → SELECT (remove TOP 1)
    s = re.sub(r'\bSELECT\s+TOP\s*\(?\s*1\s*\)?\s+', 'SELECT ', s, flags=re.IGNORECASE)
    s = re.sub(r'\bSELECT\s+TOP\s*\(?\s*(\d+)\s*\)?\s+', r'SELECT /* LIMIT \1 */ ', s, flags=re.IGNORECASE)
    # PRINT → RAISE NOTICE (don't add ; if line already ends with ;)
    s = re.sub(r'\bPRINT\s+(.+?)(\s*;?\s*)$', r"RAISE NOTICE '%', \1;", s, flags=re.IGNORECASE | re.MULTILINE)
    # Remove double semicolons from RAISE NOTICE
    s = re.sub(r"(RAISE\s+(?:NOTICE|EXCEPTION)\s+'[^']*'\s*;)\s*;", r'\1', s, flags=re.IGNORECASE)
    # RAISERROR → RAISE EXCEPTION
    def raiserror_repl(m):
        msg = m.group(1).strip().strip("'\"")
        return f"RAISE EXCEPTION '{msg}';"
    s = re.sub(r'\bRAISERROR\s*\(\s*([\'"][^,]+?[\'"])\s*,\s*\d+\s*,\s*\d+[^)]*\)',
               raiserror_repl, s, flags=re.IGNORECASE)
    # ELSE IF → ELSIF placeholder (will be fixed in structural pass)
    s = re.sub(r'\bELSE\s+IF\b', 'ELSEIF', s, flags=re.IGNORECASE)
    # EXEC [schema.]proc args → EWI comment + closed stub CALL
    # Does NOT hardcode a schema — the schema comes from the procedure's own
    # CREATE OR REPLACE PROCEDURE header, not from the EXEC call itself.
    # EXEC with OUTPUT params requires manual rewrite; mark as EWI.
    def exec_repl(m: re.Match) -> str:
        # Strip brackets and dots from schema/proc names using regex, not str.strip
        schema_part = re.sub(r'[\[\].]', '', m.group(1) or '').lower()
        proc = re.sub(r'[\[\]]', '', m.group(2) or '').lower()
        args = (m.group(3) or '').strip().rstrip(';')
        qualified = f"{schema_part}.{proc}" if schema_part else proc
        stub = f"CALL {qualified}(/* {args} */);"
        return (f"-- SPG-EWI: EXEC {qualified}({args})"
                f" — review OUTPUT params before using\n    {stub}")
    s = re.sub(
        r'\bEXEC(?:UTE)?\s+(\[?\w+\]?\.)?\[?(\w+)\]?\s+((?:[^;\n]|\n(?!\s*(?:END|IF|ELSE|BEGIN)))*);?',
        exec_repl, s, flags=re.IGNORECASE
    )
    # Remove double semicolons from generated stubs
    s = re.sub(r';(\s*);', r';\1', s)
    # SELECT var1 = expr1, var2 = expr2 FROM → SELECT expr1, expr2 INTO var1, var2 FROM
    def select_assign_to_into(m):
        indent_ws = re.match(r'^(\s*)', m.group(1)).group(1)  # leading whitespace only
        assigns_str = m.group(2)
        rest = m.group(3)
        # Parse assignments: var = expr, var2 = expr2
        parts = re.split(r',\s*(?=\w+\s*=\s*(?!\s*SELECT\b))', assigns_str)
        vars_list, exprs_list = [], []
        for part in parts:
            am = re.match(r'(\w+)\s*=\s*(.+)$', part.strip())
            if am:
                vars_list.append(am.group(1))
                exprs_list.append(am.group(2).strip())
            else:
                exprs_list.append(part.strip())
        return f"{indent_ws}SELECT {', '.join(exprs_list)} INTO {', '.join(vars_list)}{rest}"
    s = re.sub(
        r'(\s*SELECT\s+)((?:\w+\s*=\s*[^,\n]+(?:,\s*)?)+)((?:\s+FROM|\s*$))',
        select_assign_to_into,
        s, flags=re.IGNORECASE | re.MULTILINE
    )
    # Strip T-SQL table hints: WITH (HOLDLOCK, XLOCK, UPDLOCK, NOLOCK, etc.)
    s = re.sub(r'\bWITH\s*\([^)]*(?:HOLDLOCK|UPDLOCK|XLOCK|NOLOCK|ROWLOCK|TABLOCK|PAGLOCK|READPAST|READUNCOMMITTED|REPEATABLEREAD|SERIALIZABLE)[^)]*\)', '', s, flags=re.IGNORECASE)
    # Convert [bracket] identifiers to "quoted" identifiers
    s = re.sub(r'\[(\w+)\]', r'"\1"', s)
    # Table variable declarations: @var table (cols) → temp table (mark as EWI)
    s = re.sub(r'\bDECLARE\s+(\w+)\s+TABLE\s*\(([^)]+)\)\s*;?',
               r'-- SPG-EWI: TABLE variable \1 — create TEMP TABLE \1 (\2) instead\n    CREATE TEMP TABLE \1 (\2);',
               s, flags=re.IGNORECASE)
    # Fix RAISERROR double quotes
    s = re.sub(r"RAISE\s+EXCEPTION\s+'((?:[^']|'')*)'", r"RAISE EXCEPTION '\1'", s, flags=re.IGNORECASE)
    # DB_NAME() → current_database()
    s = re.sub(r'\bDB_NAME\s*\(\s*\)', 'current_database()', s, flags=re.IGNORECASE)
    s = re.sub(r'\bDB_NAME\s*\([^)]+\)', 'current_database()', s, flags=re.IGNORECASE)
    # OBJECT_ID() → NULL (MSSQL-specific)
    s = re.sub(r'\bOBJECT_ID\s*\([^)]+\)', 'NULL', s, flags=re.IGNORECASE)
    # sys.dm_db_index_physical_stats and other sys. views → comment
    s = re.sub(r'\bsys\.\w+', r'/* sys.view */', s, flags=re.IGNORECASE)
    # CURSOR patterns → mark as EWI
    s = re.sub(r'\bDECLARE\s+\w+\s+CURSOR\b.*', '-- SPG-EWI: CURSOR not converted', s, flags=re.IGNORECASE | re.DOTALL)
    # FETCH NEXT FROM → mark as EWI
    s = re.sub(r'\bFETCH\s+NEXT\s+FROM\b.*', '-- SPG-EWI: FETCH NEXT not converted', s, flags=re.IGNORECASE)
    # OPEN cursor → mark as EWI
    s = re.sub(r'\bOPEN\s+\w+\s*;?', '-- SPG-EWI: OPEN CURSOR not converted', s, flags=re.IGNORECASE)
    # DEALLOCATE → remove
    s = re.sub(r'\bDEALLOCATE\s+\w+\s*;?', '', s, flags=re.IGNORECASE)
    # CLOSE cursor → remove
    s = re.sub(r'\bCLOSE\s+\w+\s*;?', '', s, flags=re.IGNORECASE)
    # RETURN value in procedure → RETURN; (procedures can't return values, only functions can)
    s = re.sub(r'\bRETURN\s+(?!;|\s*$)\S[^\n;]*', 'RETURN /* SPG-EWI: return value removed */', s, flags=re.IGNORECASE)
    # INSERT table → INSERT INTO table
    s = re.sub(r'\bINSERT\s+(?!INTO\b)(\w)', r'INSERT INTO \1', s, flags=re.IGNORECASE)
    # COMMIT/ROLLBACK → comment
    s = re.sub(r'\b(COMMIT|ROLLBACK)\s*(?:TRANSACTION\s+\w+)?\s*;?', r'-- \1 (implicit in plpgsql)', s, flags=re.IGNORECASE)
    # Standalone result-set SELECT in procedure → PERFORM (discards result)
    # Only convert bare SELECT at start of statement (not SELECT INTO)
    # Note: result-set SELECTs in procedures require functions — mark as EWI
    return s


# ---------------------------------------------------------------------------
# Phase 2: DECLARE block extraction
# ---------------------------------------------------------------------------
def extract_declares(body: str) -> tuple[str, list[str]]:
    """Extract DECLARE statements from body, return (cleaned_body, declare_lines)."""
    declare_lines = []
    output_body = []

    for line in body.split('\n'):
        stripped = line.strip()
        dm = re.match(r'DECLARE\s+(\w+)\s+(.*?)\s*(?::?=\s*(.+?))?;?\s*$', stripped, re.IGNORECASE)
        if dm:
            vname = dm.group(1).lower()
            vtype_raw = dm.group(2).strip().rstrip(',;')
            vdefault = dm.group(3).strip().rstrip(';,') if dm.group(3) else None
            pg_type = map_type(vtype_raw)
            if vdefault:
                declare_lines.append(f'    {vname} {pg_type} := {vdefault};')
            else:
                declare_lines.append(f'    {vname} {pg_type};')
        else:
            output_body.append(line)

    return '\n'.join(output_body), declare_lines


# ---------------------------------------------------------------------------
# Phase 3: structural rewriter (IF/BEGIN/END → IF/THEN/END IF)
# ---------------------------------------------------------------------------
def _join_continuation_lines(lines: list[str]) -> list[str]:
    """Join multi-line IF/WHILE conditions: lines with unclosed parens or ending AND/OR."""
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        upper = stripped.upper()
        # Start of IF or WHILE statement
        if re.match(r'^\s*(?:IF|ELSIF|ELSEIF|WHILE)\b', line, re.IGNORECASE):
            # Count parens and look for BEGIN terminator
            combined = line.rstrip()
            depth = combined.count('(') - combined.count(')')
            j = i + 1
            while j < len(lines) and depth > 0:
                combined = combined + ' ' + lines[j].strip()
                depth += lines[j].count('(') - lines[j].count(')')
                j += 1
            # Check if next meaningful line is BEGIN
            k = j
            while k < len(lines) and lines[k].strip() == '':
                k += 1
            if k < len(lines) and re.match(r'^\s*BEGIN\s*$', lines[k], re.IGNORECASE):
                combined = combined + ' BEGIN'
                result.append(combined)
                i = k + 1  # skip through BEGIN line
            else:
                result.append(combined)
                i = j
        else:
            result.append(line)
            i += 1
    return result


def structural_rewrite(body: str) -> str:
    """
    Token-based rewriter: convert T-SQL BEGIN/END blocks after IF/ELSE/WHILE
    into PL/pgSQL THEN/ELSE/LOOP ... END IF/END LOOP.
    """
    lines = _join_continuation_lines(body.split('\n'))
    output = []
    # Stack entries: ('if', 'while', 'proc', 'other')
    block_stack = []
    pending_if = False   # True if we just saw IF/ELSIF/ELSEIF
    pending_while = False

    # Simple line-by-line processing
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip().rstrip(';')
        upper = stripped.upper()

        # WHILE condition BEGIN → WHILE condition LOOP
        wm = re.match(r'^(WHILE\s+.+?)\s+BEGIN\s*$', stripped, re.IGNORECASE)
        if wm:
            output.append(line.rstrip().rstrip('BEGIN').rstrip() + '\n' +
                          re.sub(r'\bWHILE\b', 'WHILE', wm.group(1), flags=re.IGNORECASE) +
                          ' LOOP')
            block_stack.append('while')
            i += 1
            continue

        # IF/ELSIF condition BEGIN → IF/ELSIF condition THEN
        ifm = re.match(r'^((?:ELSIF|ELSEIF|IF)\s*\(.+?)\s+BEGIN\s*$', stripped, re.IGNORECASE)
        if not ifm:
            ifm = re.match(r'^((?:ELSIF|ELSEIF|IF)\s+.+?)\s+BEGIN\s*$', stripped, re.IGNORECASE)
        if ifm:
            cond = re.sub(r'\bELSEIF\b', 'ELSIF', ifm.group(1), flags=re.IGNORECASE)
            indent = re.match(r'^(\s*)', line).group(1)
            output.append(indent + cond + ' THEN')
            block_stack.append('if')
            i += 1
            continue

        # ELSE BEGIN → ELSE
        elif re.match(r'^ELSE\s+BEGIN\s*$', stripped, re.IGNORECASE):
            indent = re.match(r'^(\s*)', line).group(1)
            output.append(indent + 'ELSE')
            block_stack.append('else')
            i += 1
            continue

        # BEGIN TRANSACTION → comment (transactions are implicit in plpgsql)
        elif re.match(r'^BEGIN\s+TRANSACTION\b', stripped, re.IGNORECASE):
            indent = re.match(r'^(\s*)', line).group(1)
            output.append(indent + '-- BEGIN TRANSACTION (implicit in plpgsql)')
            i += 1
            continue

        # Standalone BEGIN (transaction or top-level)
        elif re.match(r'^BEGIN\s*(?:TRANSACTION\s+\w+)?\s*;?\s*$', stripped, re.IGNORECASE) and not upper.startswith('BEGIN TRY'):
            block_stack.append('block')
            i += 1
            continue

        # BEGIN TRY → BEGIN (exception handling simplified)
        elif re.match(r'^BEGIN\s+TRY\s*$', stripped, re.IGNORECASE):
            block_stack.append('try')
            i += 1
            continue
        elif re.match(r'^END\s+TRY\s*$', stripped, re.IGNORECASE):
            if block_stack and block_stack[-1] == 'try':
                block_stack.pop()
            i += 1
            continue
        elif re.match(r'^BEGIN\s+CATCH\s*$', stripped, re.IGNORECASE):
            indent = re.match(r'^(\s*)', line).group(1)
            output.append(indent + 'EXCEPTION WHEN OTHERS THEN')
            block_stack.append('catch')
            i += 1
            continue
        elif re.match(r'^END\s+CATCH\s*$', stripped, re.IGNORECASE):
            if block_stack and block_stack[-1] == 'catch':
                block_stack.pop()
            i += 1
            continue

        # END → close based on stack
        elif re.match(r'^END\s*;?\s*$', stripped, re.IGNORECASE):
            indent = re.match(r'^(\s*)', line).group(1)
            if block_stack:
                frame = block_stack.pop()
                if frame == 'if':
                    output.append(indent + 'END IF;')
                elif frame == 'else':
                    output.append(indent + 'END IF;')
                elif frame == 'while':
                    output.append(indent + 'END LOOP;')
                elif frame in ('block', 'catch'):
                    pass  # transparent
                else:
                    output.append(indent + 'END;')
            else:
                output.append(indent + '-- END (unmatched)')
            i += 1
            continue

        # ELSEIF/ELSIF without BEGIN (single-statement branch)
        # IF condition → just output with THEN (BEGIN will follow or single stmt)
        # Handle IF without BEGIN on same line (single stmt follows)
        ifm2 = re.match(r'^((?:ELSIF|ELSEIF|IF)\s+.+?)$', stripped, re.IGNORECASE)
        if ifm2 and not upper.endswith('BEGIN'):
            cond = re.sub(r'\bELSEIF\b', 'ELSIF', ifm2.group(1), flags=re.IGNORECASE)
            indent = re.match(r'^(\s*)', line).group(1)
            output.append(indent + cond + ' THEN')
            # Next line is the single statement; add END IF after it
            if i + 1 < len(lines):
                output.append(lines[i + 1])
                i += 2
            else:
                i += 1
            output.append(indent + 'END IF;')
            continue

        # SELECT var = expr → SELECT expr INTO var
        # Pattern: SELECT var = expr (assignment, no FROM yet)
        sm = re.match(r'^SELECT\s+(\w+)\s*:?=\s*(.+?)(?:\s+FROM\b|$)', line, re.IGNORECASE)
        if sm and ':=' not in line:
            vname = sm.group(1)
            expr = sm.group(2)
            rest = line[sm.end():]
            indent = re.match(r'^(\s*)', line).group(1)
            if 'FROM' in rest.upper():
                output.append(f'{indent}SELECT {expr} INTO {vname}{rest}')
            else:
                output.append(f'{indent}SELECT {expr} INTO {vname}')
            i += 1
            continue

        # ELSEIF → ELSIF
        line = re.sub(r'\bELSEIF\b', 'ELSIF', line, flags=re.IGNORECASE)
        output.append(line)
        i += 1

    return '\n'.join(output)


def _add_missing_semicolons(body: str) -> str:
    """Add missing semicolons after SQL statements that end with WHERE/FROM clauses."""
    lines = body.split('\n')
    result = []
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        # Check if this line ends a statement (WHERE/FROM clause or value clause)
        # and the next non-empty line starts a new statement
        if stripped and not stripped.rstrip().endswith(';') and not stripped.rstrip().endswith(','):
            next_non_empty = ''
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    next_non_empty = lines[j].strip().upper()
                    break
            # Add semicolon if this line ends a logical statement
            stmt_end_words = ('WHERE', 'FROM', 'SET', 'INTO', 'ON', 'ELSE', 'LIMIT')
            stmt_start_words = ('SELECT ', 'INSERT ', 'UPDATE ', 'DELETE ', 'IF ', 'IF(',
                               'ELSIF ', 'ELSE', 'END IF', 'END;', 'RETURN', 'CALL ',
                               'RAISE ', 'PERFORM ', '--', '/*', 'CREATE ')
            in_stmt_end = any(re.search(rf'\b{w}\b', stripped, re.IGNORECASE) for w in stmt_end_words)
            next_is_stmt = any(next_non_empty.startswith(w.upper()) for w in stmt_start_words)
            if in_stmt_end and next_is_stmt and not stripped.endswith(';'):
                result.append(stripped + ';')
                continue
        result.append(line)
    return '\n'.join(result)


# ---------------------------------------------------------------------------
# Full procedure converter
# ---------------------------------------------------------------------------
def convert_procedure(proc_name: str, tsql: str) -> str:
    # Extract parameter block
    pm = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:\[?\w+\]?\.)?\[?(\w+)\]?\s*(.*?)\s*(?:WITH[^AS]*?)?AS\b',
        tsql, re.IGNORECASE | re.DOTALL
    )
    if not pm:
        return f"-- SPG-EWI: Could not parse: {proc_name}\n-- {tsql[:200]}"

    param_block_raw = pm.group(2).strip()
    params = extract_params(param_block_raw)

    # Extract body
    body_m = re.search(r'\bAS\b\s*BEGIN\s*(.*?)\s*END\s*$', tsql, re.IGNORECASE | re.DOTALL)
    if not body_m:
        body_m = re.search(r'\bAS\b\s*BEGIN\s*(.*)', tsql, re.IGNORECASE | re.DOTALL)
    raw_body = body_m.group(1) if body_m else '-- SPG-EWI: body not found'

    # Phase 1: preprocess
    body = preprocess(raw_body)

    # Phase 2: extract inline DECLAREs
    body, extra_declares = extract_declares(body)

    # Phase 3: structural rewrite
    body = structural_rewrite(body)

    # Phase 4: add missing semicolons after multi-line statements
    # NOTE: disabled - too aggressive, add manually for specific cases
    # body = _add_missing_semicolons(body)

    # Indent body
    body_lines = body.split('\n')
    body_indented = '\n'.join('    ' + l if l.strip() else l for l in body_lines)

    # Build param string
    pg_params = []
    for p in params:
        mode = 'INOUT' if p['output'] else 'IN'
        pg_params.append(f"    {mode} {p['name']} {p['type']}")
    param_str = ',\n'.join(pg_params) if pg_params else ''

    # Declare section
    declare_block = '\n'.join(['    _row_count integer := 0;'] + extra_declares)

    return f"""-- Converted from T-SQL by spgloader convert_procedures.py
-- SPG-EWI: Body conversion is approximate — review before use in production
CREATE OR REPLACE PROCEDURE dbo.{proc_name.lower()}(
{param_str if param_str else '    -- no parameters'}
) LANGUAGE plpgsql AS $$
DECLARE
{declare_block}
BEGIN
{body_indented}
END;
$$;
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    ddl_path = work_dir / 'ddl_objects.json'
    if not ddl_path.exists():
        print(f'ERROR: {ddl_path} not found', file=sys.stderr)
        sys.exit(1)

    data = json.loads(ddl_path.read_text())
    procs = [o for o in data if o.get('type') == 'procedure']
    print(f'Found {len(procs)} procedures')

    out_dir = work_dir / 'conversion' / 'postgres' / 'wave_4_procedures_triggers'
    out_dir.mkdir(parents=True, exist_ok=True)

    # Optional overrides directory — manually-written PL/pgSQL files that should
    # not be auto-converted.  Place files here with the same filename convention.
    overrides_dir = work_dir / 'conversion' / 'postgres' / 'wave_4_procedures_overrides'

    converted = 0
    skipped_overrides = 0
    for proc in procs:
        raw_name = proc['name'].strip('[').rstrip(']')
        schema = proc.get('schema', 'DBO]').strip('[').rstrip(']').lower()
        fname = f"{schema}]__[{raw_name.lower()}.sql"
        out_path = out_dir / fname

        # If an override file exists, copy it rather than auto-converting
        override_path = overrides_dir / fname
        if override_path.exists():
            out_path.write_text(override_path.read_text(encoding='utf-8'), encoding='utf-8')
            skipped_overrides += 1
            continue

        tsql = proc.get('ddl', '')
        out_path.write_text(convert_procedure(raw_name, tsql), encoding='utf-8')
        converted += 1

    print(f'Converted {converted} procedures, {skipped_overrides} from overrides → {out_dir}')


if __name__ == '__main__':
    main()
