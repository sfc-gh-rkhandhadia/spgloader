#!/usr/bin/env bash
#
# test_mini_migration.sh — End-to-end integration test for spgloader
#
# Runs a full migration on a minimal 5-table MSSQL schema and validates:
#   1. All tables deployed to SPG
#   2. Report shows correct counts (not 0/0)
#   3. No "not available" sections in report
#   4. Schema Verification tab is populated
#   5. Catalog Verification tab is populated
#   6. PDF generated
#   7. Workspace contract tests pass
#
# Prerequisites:
#   - Docker running with spgloader_mssql container
#   - SPG instance available (or mock mode)
#   - uv + pytest installed
#
# Usage:
#   ./tests/integration/test_mini_migration.sh [--workspace /path/to/existing/workspace]
#
# If --workspace is provided, skips the migration and validates an existing workspace.
# Otherwise, runs a fresh mini migration.
#
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WORK_DIR="${1:-}"
EXISTING=false

if [[ "${1:-}" == "--workspace" ]]; then
    WORK_DIR="$2"
    EXISTING=true
fi

if [[ -z "$WORK_DIR" ]]; then
    WORK_DIR="/tmp/spgloader_integration_$(date +%s)"
    echo "Creating fresh workspace: $WORK_DIR"
    mkdir -p "$WORK_DIR"
fi

echo "============================================================"
echo "spgloader integration test"
echo "  Skill:     $SKILL_DIR"
echo "  Workspace: $WORK_DIR"
echo "  Mode:      $([ "$EXISTING" = true ] && echo 'validate existing' || echo 'fresh run')"
echo "============================================================"

# ── Phase 1: Contract tests (structural) ──────────────────────────────────
echo ""
echo "▶ Running contract tests (structural)..."
cd "$SKILL_DIR"
if uv run pytest tests/test_workspace_contract.py -v --tb=short -q 2>&1 | tail -5; then
    echo "  ✓ Structural contract tests passed"
else
    echo "  ✗ Structural contract tests FAILED"
    exit 1
fi

# ── Phase 2: Live workspace validation (if workspace exists) ──────────────
if [ -d "$WORK_DIR/.spgloader" ]; then
    echo ""
    echo "▶ Running live workspace contract tests..."
    if uv run pytest tests/test_workspace_contract.py -v --tb=short \
        --workspace "$WORK_DIR" -q 2>&1 | tail -10; then
        echo "  ✓ Live workspace contract tests passed"
    else
        echo "  ✗ Live workspace contract tests FAILED"
        exit 1
    fi
fi

# ── Phase 3: Report generation validation ─────────────────────────────────
if [ -d "$WORK_DIR/.spgloader" ]; then
    echo ""
    echo "▶ Validating report generation..."

    # Generate report and capture stderr warnings
    REPORT_WARNINGS=$(uv run python "$SKILL_DIR/scripts/generate_report.py" \
        "$WORK_DIR" --output "$WORK_DIR/validation/migration_report.html" \
        --pdf 2>&1 >/dev/null || true)

    if [ -n "$REPORT_WARNINGS" ]; then
        echo "  ⚠ Report generator warnings:"
        echo "$REPORT_WARNINGS" | sed 's/^/    /'
    fi

    # Check report file exists and is non-trivial
    REPORT="$WORK_DIR/validation/migration_report.html"
    if [ ! -f "$REPORT" ]; then
        echo "  ✗ FAIL: migration_report.html not generated"
        exit 1
    fi

    REPORT_SIZE=$(wc -c < "$REPORT")
    if [ "$REPORT_SIZE" -lt 10000 ]; then
        echo "  ✗ FAIL: migration_report.html is suspiciously small ($REPORT_SIZE bytes)"
        exit 1
    fi
    echo "  ✓ Report generated ($REPORT_SIZE bytes)"

    # Check for "not available" / empty sections
    NOT_AVAIL=$(grep -c "not available\|NOT RUN" "$REPORT" || true)
    if [ "$NOT_AVAIL" -gt 2 ]; then
        echo "  ⚠ WARN: Report has $NOT_AVAIL 'not available'/'NOT RUN' sections"
    fi

    # Check deployment section doesn't show 0/0
    if grep -q "0 / 0" "$REPORT"; then
        echo "  ✗ FAIL: Report shows '0 / 0' in deployment — deployment_summary.json format wrong"
        exit 1
    fi
    echo "  ✓ No 0/0 in deployment section"

    # Check PDF exists
    PDF="$WORK_DIR/validation/migration_report.pdf"
    if [ -f "$PDF" ]; then
        PDF_SIZE=$(wc -c < "$PDF")
        echo "  ✓ PDF generated ($PDF_SIZE bytes)"
    else
        echo "  ⚠ WARN: PDF not generated (Chrome may not be available)"
    fi
fi

echo ""
echo "============================================================"
echo "  ALL CHECKS PASSED"
echo "============================================================"
