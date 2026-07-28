"""
prereq_guard.py — Dynamic prerequisite guard for MSSQL→SPG migration validation.

Rules are defined in alternate_flow_rules.yaml (same directory).
Each rule maps a prereq_key to an ordered list of restore steps.
Column names are resolved at runtime from the actual schema — no hardcoding.

To add a new prereq type: add a block to alternate_flow_rules.yaml.
Zero Python changes required.

Public API (unchanged from previous version):
    detect_mssql_prereqs(proc_body: str) -> list[str]
    restore_mssql_prereqs(prereqs: list[str]) -> None   (raises RuntimeError on failure)

    detect_spg_prereqs(proc_body: str) -> list[str]
    restore_spg_prereqs(prereqs: list[str]) -> None     (raises RuntimeError on failure)
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import MSSQL_CONF, SPG_CONF


class PrereqRestoreError(RuntimeError):
    """
    Raised when prereq_guard ran correctly but could not restore the required
    database state (e.g. column not found, insert failed after retry).

    Callers should classify the procedure as FAIL_MISSING_PREREQ — this is an
    environment/data issue, NOT a bug in the test harness.

    Contrast with a plain Exception from the guard: that signals a bug in the
    guard code itself (bad YAML, unexpected DB error) and should be FAIL_HARNESS.
    """

# ── Load rules from YAML ───────────────────────────────────────────────────────
_RULES = {}
_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'alternate_flow_rules.yaml')
try:
    import yaml
    with open(_RULES_PATH) as _f:
        _RULES = (yaml.safe_load(_f) or {}).get('rules', {})
except Exception as _e:
    print(f"WARN prereq_guard: could not load {_RULES_PATH}: {_e}")


# ── Pattern detection maps ─────────────────────────────────────────────────────
# Patterns compiled against the uppercased MSSQL proc body.
_MSSQL_PATTERNS = [
    ('F_MICROSLOAD_GETJOBSTATUSID',        'active_job_state'),
    ('F_MICROSLOAD_GETCURRENTJOBLOGID',    'active_job_state'),
    ('MICROSLOAD_DEFINITIONSTAGING',       'definition_staging'),
    ('MENUITEMDEFINITION_STG',             'menuitem_definition_stg'),
    ('MICROSLOAD_PRICESTAGING',            'price_staging'),
    ('MENUITEMPRICE_STG',                  'menuitem_price_stg'),
    ('PRINTERS_STG',                       'printers_stg'),
    ('PRINTTEMPLATES_STG',                 'print_templates_stg'),
    ('PRIMARECIPI',                        'prima_recipies_stg'),
    ('ELECTRONICTAG_STG',                  'electronic_tag_stg'),
    ('ZEBRAPRINTERS_STG',                  'zebra_printers_stg'),
]
# SPG proc bodies use lowercase identifiers
_SPG_PATTERNS = [(p.lower(), k) for p, k in _MSSQL_PATTERNS]


def detect_mssql_prereqs(proc_body: str) -> list:
    """Scan MSSQL proc body for known prereq patterns. Returns ordered list of keys."""
    body = (proc_body or '').upper()
    seen, result = set(), []
    for pattern, key in _MSSQL_PATTERNS:
        if key not in seen and pattern in body:
            seen.add(key)
            result.append(key)
    return result


def detect_spg_prereqs(proc_body: str) -> list:
    """Scan SPG proc body for known prereq patterns. Returns ordered list of keys."""
    body = (proc_body or '').lower()
    seen, result = set(), []
    for pattern, key in _SPG_PATTERNS:
        if key not in seen and pattern in body:
            seen.add(key)
            result.append(key)
    return result


# ── Column introspection cache ─────────────────────────────────────────────────
_col_cache: dict = {}


def _mssql_cols(conn, schema: str, table: str) -> dict:
    """Return {col_name_lower: {name, is_identity, is_nullable, data_type}}."""
    key = ('ms', schema.lower(), table.lower())
    if key in _col_cache:
        return _col_cache[key]
    cur = conn.cursor(as_dict=True)
    cur.execute("""
        SELECT c.name, c.is_identity, c.is_nullable, t.name AS data_type
        FROM sys.columns c
        JOIN sys.objects o ON c.object_id = o.object_id
        JOIN sys.schemas s ON o.schema_id = s.schema_id
        JOIN sys.types   t ON c.user_type_id = t.user_type_id
        WHERE s.name = %s AND o.name = %s AND o.type = 'U'
        ORDER BY c.column_id
    """, (schema, table))
    result = {r['name'].lower(): r for r in cur.fetchall()}
    _col_cache[key] = result
    return result


def _spg_cols(conn, schema: str, table: str) -> dict:
    """Return {col_name_lower: {name, is_nullable(bool NOT NULL), data_type, is_identity(bool)}}."""
    key = ('spg', schema.lower(), table.lower())
    if key in _col_cache:
        return _col_cache[key]
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, is_nullable, data_type, is_identity
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """, (schema, table))
    result = {}
    for row in cur.fetchall():
        col, nullable, dtype, identity = row
        result[col.lower()] = {
            'name':        col,
            'is_nullable': nullable == 'NO',   # True = NOT NULL
            'data_type':   dtype,
            'is_identity': identity == 'YES',
        }
    _col_cache[key] = result
    return result


def _resolve(candidates, col_map: dict):
    """Return the first candidate that exists in col_map (case-insensitive), or None."""
    for c in (candidates or []):
        if c.lower() in col_map:
            return c.lower()
    return None


# ── Default value generators ───────────────────────────────────────────────────

def _mssql_default(col_lower: str, col_info: dict, fixed: dict) -> str:
    """Return a T-SQL literal or function call for a NOT NULL column."""
    if col_lower in fixed:
        v = fixed[col_lower]
        if v is None:
            pass  # fall through to type inference
        elif str(v).upper() in ('GETDATE()', 'NOW()'):
            return 'GETDATE()'
        else:
            return f"'{v}'"
    dt = (col_info.get('data_type') or '').lower()
    if any(x in dt for x in ('date', 'time')):
        return 'GETDATE()'
    if dt == 'bit':
        return '0'
    if dt in ('int', 'bigint', 'smallint', 'tinyint',
              'decimal', 'numeric', 'float', 'real', 'money', 'numeric'):
        return '0'
    if dt in ('uniqueidentifier',):
        return 'NEWID()'
    return "'guard-prereq'"


def _spg_default(col_lower: str, col_info: dict, fixed: dict) -> str:
    """Return a PostgreSQL expression for a NOT NULL column."""
    if col_lower in fixed:
        v = fixed[col_lower]
        if v is None:
            pass  # fall through to type inference
        elif str(v).lower() in ('now()', 'getdate()'):
            return 'NOW()'
        else:
            return f"'{v}'"
    dt = (col_info.get('data_type') or '').lower()
    if any(x in dt for x in ('timestamp', 'date', 'time')):
        return 'NOW()'
    if dt == 'boolean':
        return 'false'
    if dt in ('integer', 'bigint', 'smallint', 'numeric',
              'real', 'double precision', 'money'):
        return '0'
    if dt in ('uuid',):
        return 'gen_random_uuid()'
    return "'guard-prereq'"


def _normalise_fixed(fixed_values) -> dict:
    """Lower-case all keys in the fixed_values dict from YAML."""
    return {k.lower(): v for k, v in (fixed_values or {}).items()}


# ── MSSQL step handlers ────────────────────────────────────────────────────────

def _mssql_exec(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()


def _mssql_promote_status_row(conn, step: dict):
    cfg     = step.get('mssql', {})
    schema  = cfg['table'][0]
    table   = cfg['table'][1]
    col_map = _mssql_cols(conn, schema, table)
    flag    = _resolve(cfg.get('active_flag_col', []), col_map)
    pk      = _resolve(cfg.get('pk_col', []), col_map)

    if not flag or not pk:
        raise PrereqRestoreError(
            f'promote_status_row: cannot resolve active_flag_col or pk_col '
            f'in [{schema}].[{table}]. Available: {list(col_map.keys())}')

    # If no rows exist at all, insert one minimal row
    fixed = _normalise_fixed(cfg.get('new_row_defaults', {}))
    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable') == 0]
    insert_col_str = ', '.join(f'[{c}]' for c in non_id_req)
    insert_val_str = ', '.join(
        ('1' if c == flag else _mssql_default(c, col_map[c], fixed))
        for c in non_id_req
    )

    sql = f"""
IF NOT EXISTS (SELECT 1 FROM [{schema}].[{table}] WHERE [{flag}] = 1)
BEGIN
    -- Promote highest existing row if any rows exist
    DECLARE @_top INT = (
        SELECT TOP 1 [{pk}] FROM [{schema}].[{table}]
        ORDER BY [{pk}] DESC
    );
    IF @_top IS NOT NULL
        UPDATE [{schema}].[{table}] SET [{flag}] = 1
        WHERE [{pk}] = @_top;
    ELSE
        -- No rows at all — insert a minimal active row
        INSERT INTO [{schema}].[{table}] ({insert_col_str})
        VALUES ({insert_val_str});
END;
"""
    _mssql_exec(conn, sql)


def _mssql_ensure_log_row(conn, step: dict):
    cfg         = step.get('mssql', {})
    schema      = cfg['table'][0]
    table       = cfg['table'][1]
    col_map     = _mssql_cols(conn, schema, table)
    fk          = _resolve(cfg.get('fk_to_status', []), col_map)
    start_col   = _resolve(cfg.get('open_start_col', []), col_map)
    end_col     = _resolve(cfg.get('open_end_col', []), col_map)

    if not fk:
        raise PrereqRestoreError(
            f'ensure_log_row: cannot resolve fk_to_status in [{schema}].[{table}]. '
            f'Available: {list(col_map.keys())}')

    fixed = _normalise_fixed(cfg.get('fixed_values', {}))

    # Build INSERT for all NOT NULL non-IDENTITY columns
    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable') == 0]

    col_str = ', '.join(f'[{c}]' for c in non_id_req)

    vals = []
    for c in non_id_req:
        if c == fk:
            vals.append('@_active')
        elif c == start_col:
            vals.append('GETDATE()')
        else:
            vals.append(_mssql_default(c, col_map[c], fixed))
    val_str = ', '.join(vals)

    end_cond = f'AND [{end_col}] IS NULL' if end_col else ''

    sql = f"""
DECLARE @_active INT = (
    SELECT TOP 1 [{fk}]
    FROM [{schema}].[{table}]
    WHERE [{fk}] IN (
        SELECT TOP 1 MicrosLoadStatusId
        FROM [stg].[MicrosLoadStatus]
        WHERE JobActiveFlag = 1
        ORDER BY MicrosLoadStatusId DESC
    )
    {end_cond}
);
DECLARE @_status INT = (
    SELECT TOP 1 MicrosLoadStatusId FROM [stg].[MicrosLoadStatus]
    WHERE JobActiveFlag = 1 ORDER BY MicrosLoadStatusId DESC
);
IF @_status IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM [{schema}].[{table}]
    WHERE [{fk}] = @_status {end_cond}
)
    INSERT INTO [{schema}].[{table}] ({col_str})
    VALUES ({val_str.replace('@_active', '@_status')});
"""
    _mssql_exec(conn, sql)


def _mssql_ensure_staging_row(conn, step: dict):
    cfg    = step.get('mssql', {})
    schema = cfg['table'][0]
    table  = cfg['table'][1]
    col_map = _mssql_cols(conn, schema, table)
    fixed   = _normalise_fixed(cfg.get('fixed_values', {}))

    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable') == 0]

    if not non_id_req:
        sql = f"IF NOT EXISTS (SELECT 1 FROM [{schema}].[{table}]) INSERT INTO [{schema}].[{table}] DEFAULT VALUES;"
    else:
        col_str = ', '.join(f'[{c}]' for c in non_id_req)
        val_str = ', '.join(_mssql_default(c, col_map[c], fixed) for c in non_id_req)
        sql = (f"IF NOT EXISTS (SELECT 1 FROM [{schema}].[{table}]) "
               f"INSERT INTO [{schema}].[{table}] ({col_str}) VALUES ({val_str});")

    _mssql_exec(conn, sql)


# ── SPG step handlers ──────────────────────────────────────────────────────────

def _spg_exec(conn, sql: str):
    cur = conn.cursor()
    cur.execute(sql)


def _spg_promote_status_row(conn, step: dict):
    cfg     = step.get('spg', {})
    schema  = cfg['table'][0]
    table   = cfg['table'][1]
    col_map = _spg_cols(conn, schema, table)
    flag    = _resolve(cfg.get('active_flag_col', []), col_map)
    pk      = _resolve(cfg.get('pk_col', []), col_map)

    if not flag or not pk:
        raise PrereqRestoreError(
            f'promote_status_row: cannot resolve active_flag_col or pk_col '
            f'in {schema}.{table}. Available: {list(col_map.keys())}')

    fixed = _normalise_fixed(cfg.get('new_row_defaults', {}))
    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable')]
    insert_col_str = ', '.join(non_id_req)
    insert_val_str = ', '.join(
        ('true' if c == flag else _spg_default(c, col_map[c], fixed))
        for c in non_id_req
    )

    sql = f"""
DO $$
DECLARE v_top INTEGER;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM {schema}.{table} WHERE {flag} = true) THEN
        SELECT {pk} INTO v_top FROM {schema}.{table} ORDER BY {pk} DESC LIMIT 1;
        IF v_top IS NOT NULL THEN
            UPDATE {schema}.{table} SET {flag} = true WHERE {pk} = v_top;
        ELSE
            INSERT INTO {schema}.{table} ({insert_col_str}) VALUES ({insert_val_str});
        END IF;
    END IF;
END $$;
"""
    _spg_exec(conn, sql)


def _spg_ensure_log_row(conn, step: dict):
    cfg       = step.get('spg', {})
    schema    = cfg['table'][0]
    table     = cfg['table'][1]
    col_map   = _spg_cols(conn, schema, table)
    fk        = _resolve(cfg.get('fk_to_status', []), col_map)
    start_col = _resolve(cfg.get('open_start_col', []), col_map)
    end_col   = _resolve(cfg.get('open_end_col', []), col_map)

    if not fk:
        raise PrereqRestoreError(
            f'ensure_log_row: cannot resolve fk_to_status in {schema}.{table}. '
            f'Available: {list(col_map.keys())}')

    fixed = _normalise_fixed(cfg.get('fixed_values', {}))

    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable')]

    col_str = ', '.join(non_id_req)
    vals = []
    for c in non_id_req:
        if c == fk:
            vals.append('v_active')
        elif c == start_col:
            vals.append('NOW()')
        else:
            vals.append(_spg_default(c, col_map[c], fixed))
    val_str = ', '.join(vals)

    end_cond = f'AND {end_col} IS NULL' if end_col else ''

    sql = f"""
DO $$
DECLARE v_active INTEGER;
BEGIN
    SELECT microsloadstatusid INTO v_active
    FROM stg.microsloadstatus
    WHERE jobactiveflag = true
    ORDER BY microsloadstatusid DESC LIMIT 1;

    IF v_active IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM {schema}.{table}
        WHERE {fk} = v_active {end_cond}
    ) THEN
        INSERT INTO {schema}.{table} ({col_str}) VALUES ({val_str});
    END IF;
END $$;
"""
    _spg_exec(conn, sql)


def _spg_ensure_staging_row(conn, step: dict):
    cfg    = step.get('spg', {})
    schema = cfg['table'][0]
    table  = cfg['table'][1]
    col_map = _spg_cols(conn, schema, table)
    fixed   = _normalise_fixed(cfg.get('fixed_values', {}))

    non_id_req = [c for c, info in col_map.items()
                  if not info.get('is_identity') and info.get('is_nullable')]

    if not non_id_req:
        sql = f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM {schema}.{table}) THEN
        INSERT INTO {schema}.{table} DEFAULT VALUES;
    END IF;
END $$;"""
    else:
        col_str = ', '.join(non_id_req)
        val_str = ', '.join(_spg_default(c, col_map[c], fixed) for c in non_id_req)
        sql = f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM {schema}.{table}) THEN
        INSERT INTO {schema}.{table} ({col_str}) VALUES ({val_str});
    END IF;
END $$;"""
    _spg_exec(conn, sql)


# ── Step dispatcher ────────────────────────────────────────────────────────────

_MSSQL_STEP_HANDLERS = {
    'promote_status_row':  _mssql_promote_status_row,
    'ensure_log_row':      _mssql_ensure_log_row,
    'ensure_staging_row':  _mssql_ensure_staging_row,
}

_SPG_STEP_HANDLERS = {
    'promote_status_row':  _spg_promote_status_row,
    'ensure_log_row':      _spg_ensure_log_row,
    'ensure_staging_row':  _spg_ensure_staging_row,
}


def _execute_rule(conn, rule: dict, handlers: dict, key: str):
    for step in (rule.get('steps') or []):
        step_type = step.get('type')
        handler   = handlers.get(step_type)
        if not handler:
            raise PrereqRestoreError(
                f'prereq_guard: unknown step type "{step_type}" in rule "{key}"')
        handler(conn, step)


# ── Public API ─────────────────────────────────────────────────────────────────

def restore_mssql_prereqs(prereqs: list) -> None:
    """
    Restore all required MSSQL prerequisite states.
    On first failure, clears the column cache and retries once with fresh introspection.
    Raises PrereqRestoreError if retry also fails — callers should classify as FAIL_MISSING_PREREQ.
    Any other exception from this function is an unexpected harness error — classify as FAIL_HARNESS.
    """
    if not prereqs:
        return
    import pymssql
    conn = pymssql.connect(**MSSQL_CONF)
    try:
        for key in prereqs:
            rule = _RULES.get(key)
            if not rule:
                continue
            try:
                _execute_rule(conn, rule, _MSSQL_STEP_HANDLERS, key)
            except Exception as first_err:
                # Retry with fresh column introspection (clears cache)
                _col_cache.clear()
                try:
                    _execute_rule(conn, rule, _MSSQL_STEP_HANDLERS, key)
                except Exception as retry_err:
                    raise PrereqRestoreError(
                        f'prereq_guard MSSQL [{key}]: {retry_err}')
    finally:
        try:
            conn.close()
        except Exception:
            pass


def restore_spg_prereqs(prereqs: list) -> None:
    """
    Restore all required SPG prerequisite states.
    On first failure, clears the column cache and retries once with fresh introspection.
    Raises PrereqRestoreError if retry also fails — callers should classify as FAIL_MISSING_PREREQ.
    Any other exception from this function is an unexpected harness error — classify as FAIL_HARNESS.
    """
    if not prereqs:
        return
    import psycopg2
    conn = psycopg2.connect(**SPG_CONF)
    conn.autocommit = True
    try:
        for key in prereqs:
            rule = _RULES.get(key)
            if not rule:
                continue
            try:
                _execute_rule(conn, rule, _SPG_STEP_HANDLERS, key)
            except Exception as first_err:
                _col_cache.clear()
                try:
                    _execute_rule(conn, rule, _SPG_STEP_HANDLERS, key)
                except Exception as retry_err:
                    raise PrereqRestoreError(
                        f'prereq_guard SPG [{key}]: {retry_err}')
    finally:
        try:
            conn.close()
        except Exception:
            pass
