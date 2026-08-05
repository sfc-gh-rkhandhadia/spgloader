"""
collect_artifacts.py — Bundle migration work dir artifacts for tester feedback.

Collects the relevant JSON files from a completed migration run, strips
sensitive fields (hostnames, passwords, service names), adds metadata,
and writes a zip for submission via GitHub Issues.

Usage:
  uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/collect_artifacts.py \\
    --work-dir ~/.spgloader/20260805_120000 \\
    --output ~/Desktop/spgloader_feedback.zip

What is included:
  conversion/deploy_report.json
  conversion/functions_deploy_report.json
  conversion/procedures_deploy_report.json
  conversion/repair_report.json
  conversion/_conversion_metrics.json
  deployment/deployment_summary.json
  parity/parity_results.json
  assessment/assessment_summary.json
  metadata.json  (added by this script)

What is NOT included (may contain customer data):
  ddl_objects.json
  Data files, CSV exports
  pg_service.conf, connections.toml
"""
import argparse
import json
import platform
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


# Fields to redact in any JSON file
_REDACT_KEYS = {
    "spg_service", "source_db", "host", "hostname", "password",
    "connection_string", "dsn", "spg_host", "account",
}

# Files to include (relative to work_dir)
_ARTIFACT_FILES = [
    "conversion/deploy_report.json",
    "conversion/functions_deploy_report.json",
    "conversion/procedures_deploy_report.json",
    "conversion/repair_report.json",
    "conversion/_conversion_metrics.json",
    "deployment/deployment_summary.json",
    "parity/parity_results.json",
    "assessment/assessment_summary.json",
]


def _redact(obj, depth: int = 0):
    """Recursively redact sensitive fields in a JSON-compatible object."""
    if depth > 20:
        return obj
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k.lower() in _REDACT_KEYS else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj


def _get_skill_version(skill_dir: Path) -> str:
    """Get current git SHA from the skill repo."""
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=skill_dir, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _detect_source_type(work_dir: Path) -> str:
    """Best-effort detection of source type from work dir artifacts."""
    summary_path = work_dir / "deployment" / "deployment_summary.json"
    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text())
            return data.get("source_type", "unknown")
        except Exception:
            pass
    return "unknown"


def collect(work_dir: Path, output: Path, skill_dir: Path) -> None:
    work_dir = work_dir.expanduser().resolve()
    output   = output.expanduser().resolve()

    if not work_dir.exists():
        print(f"ERROR: work dir not found: {work_dir}")
        raise SystemExit(1)

    included = []
    skipped  = []

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Write artifact files
        for rel in _ARTIFACT_FILES:
            src = work_dir / rel
            if not src.exists():
                skipped.append(rel)
                continue
            try:
                raw = json.loads(src.read_text())
                clean = _redact(raw)
                zf.writestr(rel, json.dumps(clean, indent=2))
                included.append(rel)
            except Exception as e:
                print(f"  WARNING: could not process {rel}: {e}")
                skipped.append(rel)

        # Write metadata
        metadata = {
            "spgloader_version": _get_skill_version(skill_dir),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_type": _detect_source_type(work_dir),
            "platform": platform.system().lower(),
            "python_version": platform.python_version(),
            "included_files": included,
            "skipped_files": skipped,
        }
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

    size_kb = output.stat().st_size / 1024
    print(f"\nArtifact bundle written: {output}  ({size_kb:.1f} KB)")
    print(f"  Included : {len(included)} files")
    if skipped:
        print(f"  Skipped  : {len(skipped)} files not found: {', '.join(skipped)}")
    print(f"\nSubmit this zip at:")
    print(f"  https://github.com/sfc-gh-rkhandhadia/spgloader/issues/new"
          f"?template=migration-feedback.yml")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bundle migration artifacts for spgloader feedback submission.",
    )
    parser.add_argument(
        "--work-dir", required=True, metavar="PATH",
        help="Path to the spgloader work directory (e.g. ~/.spgloader/20260805_120000)",
    )
    parser.add_argument(
        "--output", metavar="PATH",
        help="Output zip path (default: <work-dir>/spgloader_feedback_<timestamp>.zip)",
    )
    args = parser.parse_args()

    work_dir  = Path(args.work_dir)
    skill_dir = Path(__file__).parent.parent

    if args.output:
        output = Path(args.output)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = work_dir / f"spgloader_feedback_{ts}.zip"

    collect(work_dir, output, skill_dir)


if __name__ == "__main__":
    main()
