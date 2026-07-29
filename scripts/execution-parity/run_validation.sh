#!/bin/bash
# Load .env if present (never commit .env to git)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source "$SCRIPT_DIR/.env" && set +a

: "${MSSQL_HOST:=localhost}"
: "${MSSQL_PORT:=1434}"

export MSSQL_HOST MSSQL_PORT MSSQL_USER MSSQL_PASSWORD MSSQL_DATABASE
export SPG_HOST SPG_USER SPG_PASSWORD SPG_DATABASE
export VALIDATION_OUTPUT_DIR VALIDATION_SKIP_WRITES
export REPORT_CLIENT REPORT_AUTHOR
export MSSQL_SPG_SHARED_DIR

# Fail early if required variables are missing
for var in MSSQL_USER MSSQL_PASSWORD MSSQL_DATABASE SPG_HOST SPG_USER SPG_PASSWORD SPG_DATABASE; do
  if [ -z "${!var}" ]; then
    echo "ERROR: \$$var is not set. Add it to .env or export it before running." >&2
    exit 1
  fi
done

SKILL_SCRIPTS="/Users/rkhandhadia/.snowflake/cortex/skills/mssql_spg_migration_validation_testing/scripts"
cd "$SKILL_SCRIPTS"
python3 run.py --all 2>&1
