import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Smart parameter discovery: samples real values from the procedure's primary table
instead of using NULL for all parameters.

Strategy:
1. Parse proc body to find the main FROM table
2. Match parameter names → column names (strip @/p_ prefix, case-insensitive)
3. Run SELECT TOP 1 on that table on MSSQL to get real values
4. Use those values for both MSSQL and SPG calls
"""
import re

# ── Proc body parser ──────────────────────────────────────────────────────────

def _clean_body(definition):
    """Strip comments, get the proc body after AS/BEGIN."""
    d = re.sub(r'--[^\n]*', '', definition)
    d = re.sub(r'/\*.*?\*/', '', d, flags=re.DOTALL)
    # Get body after AS
    m = re.search(r'\bAS\b\s*(BEGIN\s*)?', d, re.IGNORECASE)
    return d[m.end():] if m else d

def find_tables_in_body(body):
    """Extract real table references from FROM and JOIN clauses."""
    tables = []
    for m in re.finditer(
        r'\b(?:FROM|JOIN)\s+((?:\[?\w+\]?\.)*\[?\w+\]?)(?:\s+(?:AS\s+)?\w+)?',
        body, re.IGNORECASE
    ):
        t = m.group(1).replace('[', '').replace(']', '')
        # Skip temp tables, table variables, subqueries
        if t.startswith('#') or t.startswith('@') or '(' in t:
            continue
        # Skip system tables
        if t.lower() in ('sys', 'information_schema'):
            continue
        tables.append(t)
    return tables

def find_param_column_mappings(body, param_names):
    """
    Find WHERE col = @param patterns. Returns {param_name: column_name}.
    param_names is a list of lowercase names (without @).
    """
    mappings = {}
    # Patterns: [schema.]col = @param  OR  @param = [schema.]col
    for m in re.finditer(
        r'(?:[\w\.]+\.)?(\w+)\s*=\s*@(\w+)|@(\w+)\s*=\s*(?:[\w\.]+\.)?(\w+)',
        body, re.IGNORECASE
    ):
        if m.group(1) and m.group(2):
            col, param = m.group(1).lower(), m.group(2).lower()
        else:
            param, col = m.group(3).lower(), m.group(4).lower()
        if param in param_names:
            mappings[param] = col
    return mappings

def strip_prefix(name):
    """Strip @, p_, p from parameter names to get the likely column name."""
    n = name.lstrip('@').lower()
    if n.startswith('p_'): n = n[2:]
    return n

# ── MSSQL real value sampler ──────────────────────────────────────────────────

def sample_mssql_params(conn_factory, schema, proc_name, proc_def, params):
    """
    Try to find real values for procedure parameters by:
    1. Parsing the proc body for table references and WHERE col=@param patterns
    2. Querying the identified table with matching column names

    Returns: list of values matching params order, or None if sampling fails.
    """
    if not params:
        return [], None, None

    param_names = [p['name'].lstrip('@').lower() for p in params if not p.get('is_output', False)]
    if not param_names:
        return [], None, None

    body = _clean_body(proc_def)
    tables = find_tables_in_body(body)
    col_mappings = find_param_column_mappings(body, param_names)

    # Build column list to SELECT — prefer mapped names, fall back to param name
    select_cols = []
    for pname in param_names:
        col = col_mappings.get(pname) or strip_prefix(pname)
        select_cols.append((pname, col))

    # Try each table until we get a non-empty row
    for table in tables:
        if '.' in table:
            full_table = table
        else:
            full_table = '%s.%s' % (schema, table)

        try:
            conn = conn_factory()
            cur  = conn.cursor(as_dict=True)

            # First check which columns actually exist in this table
            cur.execute("""
                SELECT LOWER(c.name) AS col_name
                FROM sys.columns c
                JOIN sys.objects o ON c.object_id = o.object_id
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                WHERE LOWER(s.name + '.' + o.name) = LOWER(%s)
                   OR LOWER(o.name) = LOWER(%s)
            """, (full_table, table.split('.')[-1]))
            existing_cols = {r['col_name'] for r in cur.fetchall()}

            # Map params to available columns
            final_cols = []
            param_to_col = {}
            for pname, col in select_cols:
                if col.lower() in existing_cols:
                    final_cols.append(col)
                    param_to_col[pname] = col
                elif pname in existing_cols:
                    final_cols.append(pname)
                    param_to_col[pname] = pname

            if not final_cols:
                conn.close()
                continue

            # Sample one real row with non-null values
            not_null = ' AND '.join(['%s IS NOT NULL' % c for c in final_cols[:3]])
            sample_sql = "SELECT TOP 1 %s FROM %s WHERE %s ORDER BY NEWID()" % (
                ', '.join(final_cols), full_table, not_null or '1=1'
            )
            cur.execute(sample_sql)
            row = cur.fetchone()
            conn.close()

            if row:
                # Build full param list (None for output params and unmapped params)
                result = []
                for p in params:
                    if p.get('is_output', False):
                        continue  # skip output params
                    pname = p['name'].lstrip('@').lower()
                    col   = param_to_col.get(pname)
                    result.append(row.get(col) if col else None)
                return result, full_table, sample_sql

        except Exception as e:
            try: conn.close()
            except: pass
            continue

    return None, None, None

# ── SPG real value sampler ────────────────────────────────────────────────────

def sample_spg_params(conn_factory, schema, proc_name, proc_def, params):
    """
    Same approach for SPG: parse proc body, find tables, sample real values.
    """
    if not params:
        return [], None, None

    in_params = [p for p in params if p.get('mode', 'IN') in ('IN', 'INOUT')]
    if not in_params:
        return None, None, None

    param_names = [p['name'].lstrip('_').lower() for p in in_params]
    # Strip p_ prefix
    stripped = [strip_prefix(n) for n in param_names]

    body = _clean_body(proc_def)
    tables = find_tables_in_body(body)
    col_mappings = find_param_column_mappings(body, stripped + param_names)

    for table in tables:
        if '.' in table:
            full_table = table
        else:
            full_table = '%s."%s"' % (schema, table)

        try:
            conn = conn_factory()
            cur  = conn.cursor()

            # Check which columns exist
            tname = table.split('.')[-1].strip('"')
            sname = table.split('.')[0].strip('"') if '.' in table else schema
            cur.execute("""
                SELECT LOWER(column_name) AS col_name
                FROM information_schema.columns
                WHERE LOWER(table_schema) = LOWER(%s) AND LOWER(table_name) = LOWER(%s)
            """, (sname, tname))
            existing_cols = {r[0] for r in cur.fetchall()}

            final_cols = []
            param_to_col = {}
            for i, p in enumerate(in_params):
                pname    = p['name'].lstrip('_').lower()
                stripped_n = strip_prefix(pname)
                for candidate in [stripped_n, pname]:
                    if candidate in existing_cols:
                        final_cols.append(candidate)
                        param_to_col[pname] = candidate
                        break

            if not final_cols:
                conn.close()
                continue

            not_null = ' AND '.join(['"%s" IS NOT NULL' % c for c in final_cols[:3]])
            sample_sql = 'SELECT %s FROM %s WHERE %s LIMIT 1' % (
                ', '.join('"%s"' % c for c in final_cols),
                full_table, not_null or 'TRUE'
            )
            cur.execute(sample_sql)
            row = cur.fetchone()
            col_names = [d[0] for d in cur.description] if cur.description else []
            conn.close()

            if row:
                row_dict = dict(zip(col_names, row))
                result = []
                for p in in_params:
                    pname = p['name'].lstrip('_').lower()
                    col   = param_to_col.get(pname)
                    result.append(row_dict.get(col) if col else None)
                return result, full_table, sample_sql

        except Exception as e:
            try: conn.close()
            except: pass
            continue

    return None, None, None
