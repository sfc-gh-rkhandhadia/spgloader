#!/bin/bash
# Load .env if present (never commit .env to git)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source "$SCRIPT_DIR/.env" && set +a

: "${MSSQL_HOST:=localhost}"
: "${MSSQL_PORT:=1434}"

export MSSQL_HOST MSSQL_PORT MSSQL_USER MSSQL_PASSWORD MSSQL_DATABASE
export SPG_HOST SPG_USER SPG_PASSWORD SPG_DATABASE
export VALIDATION_OUTPUT_DIR REPORT_CLIENT REPORT_AUTHOR
export MSSQL_SPG_SHARED_DIR

# Fail early if required variables are missing
for var in MSSQL_USER MSSQL_PASSWORD MSSQL_DATABASE SPG_HOST SPG_USER SPG_PASSWORD SPG_DATABASE; do
  if [ -z "${!var}" ]; then
    echo "ERROR: \$$var is not set. Add it to .env or export it before running." >&2
    exit 1
  fi
done

OUTPUT_DIR="$VALIDATION_OUTPUT_DIR"
SCRIPT_SKILLS="/Users/rkhandhadia/.snowflake/cortex/skills/mssql_spg_migration_validation_testing/scripts"
TODAY=$(date +%Y%m%d)

cd "$SCRIPT_SKILLS"

echo "=== Step 1: Comparing procedure outputs ==="
python3 compare_proc_outputs.py 2>&1

echo ""
echo "=== Step 2: Generating Markdown report ==="
rm -f "${OUTPUT_DIR}/Migration_Validation_${TODAY}.md"
python3 generate_validation_markdown.py \
  --out-dir "${OUTPUT_DIR}" \
  --client "${REPORT_CLIENT}" 2>&1

echo ""
echo "=== Step 3: Generating PowerPoint report ==="
rm -f "${HOME}/Downloads/Migration_Validation_${TODAY}.pptx"
rm -f "${HOME}/Downloads/Migration_Validation_${TODAY}.md"
python3 generate_migration_report.py 2>&1

echo ""
echo "=== Step 4: Copying PPTX to output directory ==="
PPTX_NAME="Migration_Validation_$(echo "$REPORT_CLIENT" | tr ' ' '_')_${TODAY}.pptx"
cp "${HOME}/Downloads/Migration_Validation_${TODAY}.pptx" \
   "${OUTPUT_DIR}/${PPTX_NAME}" && \
   echo "Copied PPTX to: ${OUTPUT_DIR}/${PPTX_NAME}"

echo ""
echo "=== Final files in output directory ==="
ls -lh "${OUTPUT_DIR}"/Migration_Validation_*.{md,pptx} 2>/dev/null
