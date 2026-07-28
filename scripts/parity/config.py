"""
config.py — Connection and settings configuration for MSSQL → Postgres Migration Validator.

ALL values come from environment variables. No defaults for credentials.
Set required vars before running any script:

    export MSSQL_HOST="localhost"
    export MSSQL_PORT="1433"          # optional, default 1433
    export MSSQL_USER="sa"
    export MSSQL_PASSWORD="<mssql-sa-password>"
    export MSSQL_DATABASE="YourDatabase"

    export SPG_HOST="yourhost.aws.postgres.snowflake.app"
    export SPG_USER="snowflake_admin"
    export SPG_PASSWORD="<spg-admin-password>"
    export SPG_DATABASE="postgres"    # optional, default postgres

    # Optional settings
    export VALIDATION_OUTPUT_DIR="/tmp/validation_output"
    export VALIDATION_BATCH_SIZE="10"
    export VALIDATION_SKIP_WRITES="true"
    export VALIDATION_WRITE_KEYWORDS="update,delete,insert,archive,cancel,upsert"
    export VALIDATION_SCHEMA_ALIAS="dbo=public"   # optional: src_schema=tgt_schema pairs
"""

import os, sys

_MISSING = []

def _require(name, description):
    """Return env var value or record it as missing."""
    v = os.environ.get(name, '').strip()
    if not v:
        _MISSING.append((name, description))
        return ''
    return v

def _optional(name, default=''):
    return os.environ.get(name, default).strip()

def check_required():
    """Call this at script start to abort if any required var is unset."""
    if _MISSING:
        print("ERROR: The following required environment variables are not set:\n")
        for name, desc in _MISSING:
            print(f"  export {name}=\"{desc}\"")
        print("\nSee scripts/README.md for full setup instructions.")
        sys.exit(1)


# ── MSSQL (source) ────────────────────────────────────────────────────────────
MSSQL_CONF = dict(
    server   = _require('MSSQL_HOST',     'your-sql-server-host-or-ip'),
    port     = int(_optional('MSSQL_PORT', '1433')),
    user     = _require('MSSQL_USER',     'sa'),
    password = _require('MSSQL_PASSWORD', 'your-mssql-password'),
    database = _require('MSSQL_DATABASE', 'YourDatabaseName'),
    timeout  = int(_optional('MSSQL_TIMEOUT', '30')),
)

# ── Snowflake Postgres / any Postgres (target) ────────────────────────────────
SPG_CONF = dict(
    host     = _require('SPG_HOST',     'yourhost.aws.postgres.snowflake.app'),
    port     = int(_optional('SPG_PORT', '5432')),
    user     = _require('SPG_USER',     'snowflake_admin'),
    password = _require('SPG_PASSWORD', 'your-spg-password'),
    dbname   = _optional('SPG_DATABASE', 'postgres'),
    sslmode  = _optional('SPG_SSLMODE',  'require'),
    connect_timeout = int(_optional('SPG_CONNECT_TIMEOUT', '15')),
    options  = _optional('SPG_OPTIONS', '-c statement_timeout=30000'),
    keepalives          = 1,
    keepalives_idle     = 20,
    keepalives_interval = 5,
    keepalives_count    = 3,
)

# ── Output paths ──────────────────────────────────────────────────────────────
OUTPUT_DIR         = _optional('VALIDATION_OUTPUT_DIR', '/tmp/validation_output')
MSSQL_OUTPUT_FILE  = os.path.join(OUTPUT_DIR, 'mssql_output.jsonl')
SPG_OUTPUT_FILE    = os.path.join(OUTPUT_DIR, 'spg_output.jsonl')
SHARED_PARAMS_FILE = os.path.join(OUTPUT_DIR, 'shared_sampled_params.json')
REPORT_FILE        = os.path.join(OUTPUT_DIR, 'comparison_report.txt')
VIEW_LOG_FILE      = os.path.join(OUTPUT_DIR, 'view_validation.log')
TRIGGER_LOG_FILE   = os.path.join(OUTPUT_DIR, 'trigger_validation.log')

# ── Behaviour ─────────────────────────────────────────────────────────────────
BATCH_SIZE       = int(_optional('VALIDATION_BATCH_SIZE', '10'))
SKIP_WRITE_PROCS = _optional('VALIDATION_SKIP_WRITES', 'true').lower() == 'true'

# Keywords that identify write/modify procedures (only used when SKIP_WRITE_PROCS=true).
# Override via env var as a comma-separated list.
_default_write_kw = (
    'update,delete,insert,archive,cancel,upsert,precartdelete,'
    'moveprecart,posusercartarchive,cartdeleteall,closecartprocessing,'
    'deleteschedulecart,deleteuserhierarchyaccessbyuser,deleteuserroles,'
    'deletlocationcatalogaccessbylocation,cancelpriceschedule,'
    'locationcatalogdelete,menuitemlocationshare,processlogexport,audit_update'
)
WRITE_KEYWORDS = [
    k.strip() for k in _optional('VALIDATION_WRITE_KEYWORDS', _default_write_kw).split(',')
    if k.strip()
]

# ── Schema alias map (source_schema=target_schema) ────────────────────────────
# Use when procs were migrated to a different schema in the target.
# Example: export VALIDATION_SCHEMA_ALIAS="dbo=public,staging=stg"
SCHEMA_ALIAS = {}
_alias_str = _optional('VALIDATION_SCHEMA_ALIAS', '')
for _pair in _alias_str.split(','):
    if '=' in _pair:
        _src, _tgt = _pair.split('=', 1)
        SCHEMA_ALIAS[_src.strip().lower()] = _tgt.strip().lower()

# ── Schema exclusion list ─────────────────────────────────────────────────────
# Schemas to exclude from validation entirely (in addition to system schemas).
# Default: 'public' — the Postgres public schema typically contains extensions
# (btree_gist, pgcrypto, uuid, pg_stat_statements) not related to the migration.
# Override: export VALIDATION_EXCLUDE_SCHEMAS="" to include public
#           export VALIDATION_EXCLUDE_SCHEMAS="public,reporting" to exclude more
_default_exclude = 'public'
EXCLUDE_SCHEMAS = {
    s.strip().lower()
    for s in _optional('VALIDATION_EXCLUDE_SCHEMAS', _default_exclude).split(',')
    if s.strip()
}

# ── System schema detection ───────────────────────────────────────────────────
# Postgres / Snowflake internal schemas excluded from validation.
#
# _SYSTEM_PREFIXES: schemas whose names START WITH these strings are excluded.
#   Only add a prefix here when the entire prefix namespace is Snowflake/Postgres
#   internal — never add short words that could collide with customer schemas.
#
# _SYSTEM_EXACT: exact schema names that are always system/infrastructure.
#   Prefer exact matches over prefix matches for short/common names (e.g. 'cron')
#   to avoid accidentally excluding customer schemas like 'cron_billing'.
#
# VALIDATION_EXCLUDE_SCHEMAS: customer-controlled override (comma-separated).
#   Default 'public' — Postgres public schema holds extensions, not migrated objects.
_SYSTEM_PREFIXES = (
    'pg_',              # all Postgres catalog schemas (pg_catalog, pg_toast, pg_temp_*, ...)
    'information_schema',  # SQL standard information schema
    'snowflake_',       # Snowflake internal schemas
    'snowflake_cdc',    # Snowflake CDC replication schema
    '__pg_',            # Postgres internal double-underscore schemas
    '__lake__',         # Snowflake lake internal double-underscore schemas
    'extension_',       # Postgres extension namespaces
)
# Exact names that are always system/infrastructure — short names stay here,
# not in _SYSTEM_PREFIXES, to avoid blocking customer schemas with similar names.
_SYSTEM_EXACT = {
    'information_schema',  # also covered by prefix, belt-and-suspenders
    'pg_catalog',
    'pg_toast',
    'cron',             # pg_cron extension schema (exact only — not a prefix)
    'lake',             # Snowflake lake schema (exact only — 'lake_data' is customer)
    'incremental',      # Snowflake incremental-refresh internal schema
    'map_type',         # Snowflake type-mapping internal schema
}
# MSSQL system schemas. sys.objects only returns user-created objects so these
# never appear in practice, but kept as a defensive filter.
_MSSQL_SYSTEM = {'sys', 'information_schema'}  # normalised to lowercase

def is_spg_system_schema(name):
    """Return True if this schema should be excluded from validation.
    Covers Postgres/Snowflake infrastructure schemas AND any schemas
    explicitly listed in VALIDATION_EXCLUDE_SCHEMAS.
    """
    n = name.lower()
    if n in _SYSTEM_EXACT or n in EXCLUDE_SCHEMAS:
        return True
    return any(n.startswith(p) for p in _SYSTEM_PREFIXES)

def is_mssql_system_schema(name):
    """Return True if this is a MSSQL system schema (case-insensitive)."""
    return name.lower() in _MSSQL_SYSTEM
