"""
collect_failures.py — Extract failures from migration artifacts into a JSONL ledger.

Reads deployment reports, repair reports, and parity results from a workspace,
then produces a feedback_export.jsonl file with standardized failure entries.

Usage:
    python scripts/collect_failures.py <work_dir> [--output <path>] [--tester <email>] [--scenario <name>]

Called automatically after Phase 6.6 by the witness-validate sub-skill.
Also callable manually for ad-hoc analysis.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Error classification (shared with feedback.py)
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (r"column .+ does not exist",          "column_does_not_exist"),
    (r"relation .+ does not exist",        "relation_does_not_exist"),
    (r"function .+ does not exist",        "function_does_not_exist"),
    (r"type .+ does not exist",            "type_does_not_exist"),
    (r"syntax error at or near",           "syntax_error"),
    (r"unterminated .* quoted",            "unterminated_quoted_identifier"),
    (r"division by zero",                  "division_by_zero"),
    (r"cursor",                            "cursor_loop"),
    (r"undeclared variable",               "undeclared_variable"),
    (r"operator does not exist",           "operator_type_mismatch"),
    (r"permission denied",                 "permission_denied"),
    (r"already exists",                    "already_exists"),
    (r"cannot cast",                       "cast_error"),
    (r"ambiguous column",                  "ambiguous_column"),
]


def classify_error(error: str) -> str:
    err_lower = error.lower()
    for pattern, label in _ERROR_PATTERNS:
        if re.search(pattern, err_lower):
            return label
    if error:
        return "other:" + error[:60].strip().replace("\n", " ")
    return "unknown"


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _get_spgloader_version(work_dir: Path) -> str:
    """Try to determine the spgloader version from git or state file."""
    # Check migration_state.json for version
    state_file = work_dir / "migration_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            return state.get("spgloader_version", "unknown")
        except Exception:
            pass
    # Try git
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.environ.get("SPGLOADER_SKILL_DIR", ".")
        )
        if result.returncode == 0:
            return f"commit:{result.stdout.strip()}"
    except Exception:
        pass
    return "unknown"


def _get_source_info(work_dir: Path) -> tuple[str, str]:
    """Get source_type and source_version from migration state."""
    state_file = work_dir / "migration_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            return (
                state.get("source_type", "unknown"),
                state.get("source_version", "unknown"),
            )
        except Exception:
            pass
    return ("unknown", "unknown")


# ---------------------------------------------------------------------------
# Failure extraction
# ---------------------------------------------------------------------------

def collect_failures(work_dir: str | Path, tester: str = "", scenario: str = "") -> list[dict]:
    """Extract all failures from workspace artifacts into standardized entries."""
    work_dir = Path(work_dir).expanduser()
    entries: list[dict] = []

    source_type, source_version = _get_source_info(work_dir)
    spgloader_version = _get_spgloader_version(work_dir)
    timestamp = datetime.now(timezone.utc).isoformat()

    if not tester:
        tester = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    if not scenario:
        # Derive from work_dir name
        scenario = work_dir.name if work_dir.name != "." else "unknown"

    base = {
        "timestamp": timestamp,
        "tester": tester,
        "scenario": scenario,
        "source_type": source_type,
        "source_version": source_version,
        "spgloader_version": spgloader_version,
    }

    # --- Deploy failures (Phase 5) ---
    deploy_summary = _read_json(work_dir / "deployment" / "deployment_summary.json")
    if isinstance(deploy_summary, dict):
        for phase_name, phase_data in deploy_summary.get("phases", {}).items():
            if not isinstance(phase_data, dict):
                continue
            for failure in phase_data.get("failures", []):
                if not isinstance(failure, dict):
                    continue
                entries.append({
                    **base,
                    "id": str(uuid.uuid4())[:8],
                    "phase": "deploy",
                    "object_type": failure.get("type", phase_name.rstrip("s")),
                    "object_name": failure.get("name", "unknown"),
                    "error": failure.get("error", ""),
                    "error_class": classify_error(failure.get("error", "")),
                    "source_sql_snippet": failure.get("source_sql", "")[:500],
                    "converted_sql_snippet": failure.get("converted_sql", "")[:500],
                    "repair_attempted": False,
                    "repair_succeeded": False,
                })

    # --- Conversion/repair failures (Phase 4) ---
    for report_name in ("procedures_deploy_report.json", "functions_deploy_report.json"):
        report = _read_json(work_dir / "conversion" / report_name)
        if not isinstance(report, dict):
            continue
        obj_type = "procedure" if "procedure" in report_name else "function"
        for failure in report.get("failed", []):
            if not isinstance(failure, dict):
                continue
            entries.append({
                **base,
                "id": str(uuid.uuid4())[:8],
                "phase": "convert",
                "object_type": obj_type,
                "object_name": failure.get("procedure") or failure.get("function") or "unknown",
                "error": failure.get("error", ""),
                "error_class": classify_error(failure.get("error", "")),
                "source_sql_snippet": failure.get("source_sql", "")[:500],
                "converted_sql_snippet": failure.get("converted_sql", "")[:500],
                "repair_attempted": False,
                "repair_succeeded": False,
            })

    # --- Repair failures (LLM repair that still failed) ---
    repair_report = _read_json(work_dir / "conversion" / "repair_report.json")
    if isinstance(repair_report, dict):
        for failure in repair_report.get("still_failed", []):
            if not isinstance(failure, dict):
                continue
            obj_name = failure.get("procedure") or failure.get("function") or failure.get("name", "unknown")
            entries.append({
                **base,
                "id": str(uuid.uuid4())[:8],
                "phase": "repair",
                "object_type": failure.get("type", "procedure"),
                "object_name": obj_name,
                "error": failure.get("error", ""),
                "error_class": classify_error(failure.get("error", "")),
                "source_sql_snippet": failure.get("source_sql", "")[:500],
                "converted_sql_snippet": failure.get("converted_sql", "")[:500],
                "repair_attempted": True,
                "repair_succeeded": False,
            })

    # --- Parity failures (Phase 6.6) ---
    parity = _read_json(work_dir / "parity" / "parity_results.json")
    if isinstance(parity, dict):
        for obj in parity.get("missing_objects", []):
            name = obj if isinstance(obj, str) else obj.get("name", "unknown") if isinstance(obj, dict) else "unknown"
            entries.append({
                **base,
                "id": str(uuid.uuid4())[:8],
                "phase": "parity",
                "object_type": "unknown",
                "object_name": name,
                "error": "Object missing in SPG (not deployed or dropped)",
                "error_class": "missing_in_spg",
                "source_sql_snippet": "",
                "converted_sql_snippet": "",
                "repair_attempted": False,
                "repair_succeeded": False,
            })

        for obj in parity.get("row_count_results", []):
            if not isinstance(obj, dict):
                continue
            diff_pct = abs(obj.get("diff_pct", 0))
            if diff_pct > 5:
                entries.append({
                    **base,
                    "id": str(uuid.uuid4())[:8],
                    "phase": "parity",
                    "object_type": "table",
                    "object_name": obj.get("table", "unknown"),
                    "error": f"Row count delta: {obj.get('diff_pct', 0):.1f}% "
                             f"(source={obj.get('source_count', '?')}, spg={obj.get('spg_count', '?')})",
                    "error_class": "row_count_mismatch",
                    "source_sql_snippet": "",
                    "converted_sql_snippet": "",
                    "repair_attempted": False,
                    "repair_succeeded": False,
                })

    return entries


# ---------------------------------------------------------------------------
# JSONL export
# ---------------------------------------------------------------------------

def export_jsonl(entries: list[dict], output_path: str | Path) -> None:
    """Write entries as JSONL (one JSON object per line, append-safe)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Collect migration failures into a JSONL feedback file."
    )
    parser.add_argument("work_dir", help="Path to the migration workspace")
    parser.add_argument("--output", "-o", help="Output JSONL path (default: <work_dir>/feedback_export.jsonl)")
    parser.add_argument("--tester", default="", help="Tester email/name")
    parser.add_argument("--scenario", default="", help="Scenario name (e.g., northwind)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    if not work_dir.exists():
        print(f"ERROR: work_dir does not exist: {work_dir}")
        raise SystemExit(1)

    output = Path(args.output) if args.output else work_dir / "feedback_export.jsonl"

    entries = collect_failures(work_dir, tester=args.tester, scenario=args.scenario)

    export_jsonl(entries, output)
    if not entries:
        print(f"No failures found — empty feedback_export.jsonl written → {output}")
    else:
        print(f"Collected {len(entries)} failures → {output}")


if __name__ == "__main__":
    main()
