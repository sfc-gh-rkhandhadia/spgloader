#!/bin/bash
# spgloader setup — verifies prerequisites and installs Python dependencies
set -e
cd "$(dirname "$0")"

echo "=== spgloader setup ==="
echo ""

# ---- Prerequisite checks ----

echo "Checking prerequisites..."
ERRORS=0

if ! command -v uv >/dev/null 2>&1; then
    echo "  ERROR: uv not found. Install with:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    ERRORS=$((ERRORS + 1))
else
    echo "  uv: OK ($(uv --version))"
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "  ERROR: python3 not found. Install Python 3.11+ from https://python.org"
    ERRORS=$((ERRORS + 1))
else
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo "  python3: OK (${PY_VERSION})"
fi

if command -v pgloader >/dev/null 2>&1; then
    echo "  pgloader: OK ($(pgloader --version 2>&1 | head -1))"
else
    echo "  WARNING: pgloader not found — tables/data migration disabled"
    echo "    Install: brew install pgloader  (macOS)"
    echo "             apt install pgloader   (Debian/Ubuntu)"
fi

if command -v docker >/dev/null 2>&1; then
    echo "  docker: OK ($(docker --version))"
else
    echo "  WARNING: docker not found — Docker-based source setup disabled"
    echo "    Install: https://docs.docker.com/get-docker/"
fi

if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "Setup failed: $ERRORS required prerequisite(s) missing. Fix them and re-run."
    exit 1
fi

echo ""

# ---- Install Python dependencies ----

echo "Installing Python dependencies..."
uv sync
echo "  Dependencies: OK"
echo ""

# ---- Verify lib is importable ----

echo "Verifying spgloader library..."
PYTHONPATH=lib uv run python -c "
from spgloader.connectors import get_connector
from spgloader.conversion.dep_graph import build_dep_graph_result
from spgloader.conversion.ewi import SPG_EWI_CODES, EWISeverity
from spgloader.deployment import spg
from spgloader.reporting.assessment import SPGCompatibilityAssessment
from spgloader.workspace import Workspace

blocks = [c for c in SPG_EWI_CODES.values() if c.severity == EWISeverity.BLOCK]
warns = [c for c in SPG_EWI_CODES.values() if c.severity == EWISeverity.WARN]
print(f'  EWI codes loaded: {len(SPG_EWI_CODES)} total ({len(blocks)} BLOCK, {len(warns)} WARN)')
print('  Library imports: OK')
"

echo ""

# ---- Verify CLI scripts ----

echo "Verifying CLI scripts..."
for script in extract_ddl build_dep_graph gen_pgloader_config deploy_to_spg assess; do
    PYTHONPATH=lib uv run python "scripts/${script}.py" --help > /dev/null 2>&1 \
        && echo "  ${script}.py: OK" \
        || echo "  ${script}.py: FAILED"
done

echo ""

# ---- Summary ----

echo "==========================="
echo "spgloader setup complete!"
echo ""
echo "Quick start:"
echo "  1. Install into Cortex Code:"
echo "     ln -s $(pwd) ~/.snowflake/cortex/skills/spgloader"
echo ""
echo "  2. Start a migration in Cortex Code:"
echo "     'migrate mssql to snowflake postgres'"
echo "     'migrate mysql to snowflake postgres'"
echo "     'migrate oracle to snowflake postgres'"
echo ""
echo "  3. Run SPG assessment standalone:"
echo "     uv run python scripts/assess.py --source-type mssql --ddl-file schema.sql --output ./assessment/"
echo "==========================="
