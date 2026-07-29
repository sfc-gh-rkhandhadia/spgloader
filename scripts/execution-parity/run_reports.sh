#!/bin/bash
# run_reports.sh — Generate markdown and PowerPoint validation reports.
#
# Required env vars (set before running):
#   MSSQL_HOST, MSSQL_PORT, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE
#   SPG_HOST, SPG_USER, SPG_PASSWORD, SPG_DATABASE
#   VALIDATION_OUTPUT_DIR   — directory for output files
#   CLIENT_NAME             — client name on report cover
#   AUTHOR                  — author name on report cover
#   MSSQL_SPG_SHARED_DIR    — shared handoff directory (optional)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 "${SCRIPT_DIR}/generate_validation_markdown.py" \
  --out-dir "${VALIDATION_OUTPUT_DIR:-/tmp/validation_output}" \
  --client  "${CLIENT_NAME:-${MSSQL_DATABASE}}" 2>&1

python3 "${SCRIPT_DIR}/generate_migration_report.py" \
  --client "${CLIENT_NAME:-${MSSQL_DATABASE}}" \
  --author "${AUTHOR:-}" 2>&1
