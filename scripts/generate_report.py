#!/usr/bin/env python3
"""
Generate a migration report for an spgloader workspace.

Usage:
    python scripts/generate_report.py /path/to/workspace
    python scripts/generate_report.py /path/to/workspace --output /tmp/report.html
    python scripts/generate_report.py /path/to/workspace --pdf   # also opens Chrome headless
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from spgloader.reporting.html_report import generate


def _export_pdf(html_path: Path) -> Path:
    pdf_path = html_path.with_suffix(".pdf")
    chrome = _find_chrome()
    if not chrome:
        print("  [warn] Chrome not found — skipping PDF export", file=sys.stderr)
        return pdf_path
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--run-all-compositor-stages-before-draw",
            f"--print-to-pdf={pdf_path}",
            "--print-to-pdf-no-header",
            "--no-pdf-header-footer",
            f"file://{html_path}",
        ],
        capture_output=True,
        timeout=60,
    )
    return pdf_path


def _find_chrome() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("google-chrome") or shutil.which("chromium")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an HTML (and optionally PDF) migration report."
    )
    parser.add_argument("workspace", help="Path to the spgloader workspace directory")
    parser.add_argument(
        "--output", "-o",
        help="Output HTML file path (default: {workspace}/migration_report.html)",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Also export a PDF via Chrome headless",
    )
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        sys.exit(f"Error: workspace directory not found: {ws}")

    # ── Pre-report workspace validation ──────────────────────────────────────
    warnings = _validate_workspace(ws)
    if warnings:
        print("Workspace validation warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)
        print("", file=sys.stderr)

    out = generate(ws, args.output)
    print(f"Report written: {out}")

    if args.pdf:
        pdf = _export_pdf(out)
        if pdf.exists():
            print(f"PDF written:    {pdf}")
        else:
            print("PDF export failed (no output file)", file=sys.stderr)


def _validate_workspace(ws: Path) -> list[str]:
    """Check workspace artifacts before generating report. Returns warnings."""
    import json as _json
    warnings = []

    # deployment_summary.json
    ds = ws / "deployment" / "deployment_summary.json"
    if ds.exists():
        try:
            d = _json.loads(ds.read_text())
            if "phases" not in d:
                warnings.append(f"{ds.name}: missing 'phases' key — deployment tab will show 0/0")
            elif "tables" not in d["phases"]:
                warnings.append(f"{ds.name}: phases.tables missing")
        except Exception as e:
            warnings.append(f"{ds.name}: parse error — {e}")
    else:
        warnings.append("deployment/deployment_summary.json not found — deployment tab will be empty")

    # migration_state.json
    ms = ws / ".spgloader" / "migration_state.json"
    if ms.exists():
        try:
            d = _json.loads(ms.read_text())
            if "views" in d and not d["views"].get("succeeded"):
                warnings.append(f"migration_state.json: views.succeeded is empty — report may show 0 views deployed")
        except Exception as e:
            warnings.append(f"migration_state.json: parse error — {e}")

    # validation_report.json
    vr = ws / "validation" / "validation_report.json"
    if vr.exists():
        try:
            d = _json.loads(vr.read_text())
            if "checks" not in d or not d["checks"]:
                warnings.append("validation_report.json: no 'checks' — Schema Verification tab will be empty")
        except Exception as e:
            warnings.append(f"validation_report.json: parse error — {e}")
    else:
        warnings.append("validation/validation_report.json not found — Schema Verification tab will be empty")

    # catalog_verification.json
    cv = ws / "validation" / "catalog_verification.json"
    if cv.exists():
        try:
            d = _json.loads(cv.read_text())
            if not d.get("objects"):
                warnings.append("catalog_verification.json: no 'objects' — Catalog tab will show 'not available'")
        except Exception as e:
            warnings.append(f"catalog_verification.json: parse error — {e}")

    return warnings


if __name__ == "__main__":
    main()
