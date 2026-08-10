"""
replay_single.py — Re-convert and re-deploy a single object to verify a fix.

Used by the auto-fix sub-skill (Layer 1 verification) to confirm that a
rule/prompt/script change actually resolves the failure for a specific object.

Usage:
    python scripts/replay_single.py <work_dir> --object <name> --source-type <type> [--spg-conn <conn>]

Returns exit code 0 if deploy succeeds, 1 if it fails.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def find_source_sql(work_dir: Path, object_name: str) -> str | None:
    """Find the original source SQL for the object in the workspace."""
    # Check conversion directory for the source file
    conv_dir = work_dir / "conversion"
    if not conv_dir.exists():
        return None

    # Look in extracted DDL
    for sql_file in conv_dir.rglob("*.sql"):
        if object_name.lower().replace("dbo.", "") in sql_file.stem.lower():
            return sql_file.read_text()

    # Check object inventory
    inventory_file = work_dir / "object_inventory.json"
    if inventory_file.exists():
        try:
            inventory = json.loads(inventory_file.read_text())
            for obj in inventory.get("objects", []):
                if isinstance(obj, dict) and obj.get("name", "").lower() == object_name.lower():
                    return obj.get("source_sql", "")
        except Exception:
            pass

    return None


def reconvert_object(work_dir: Path, object_name: str, source_type: str, skill_dir: Path) -> tuple[bool, str]:
    """Re-run conversion for a single object. Returns (success, converted_sql_or_error)."""
    convert_script = skill_dir / "scripts" / "convert_objects.py"
    if not convert_script.exists():
        return False, f"convert_objects.py not found at {convert_script}"

    result = subprocess.run(
        [sys.executable, str(convert_script),
         "--work-dir", str(work_dir),
         "--objects", object_name,
         "--source-type", source_type,
         "--single-object"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "SPGLOADER_WORK_DIR": str(work_dir)},
    )

    if result.returncode != 0:
        return False, result.stderr or result.stdout or "Unknown conversion error"

    # Find the converted output
    conv_dir = work_dir / "conversion" / "output"
    for sql_file in conv_dir.rglob("*.sql"):
        if object_name.lower().replace("dbo.", "") in sql_file.stem.lower():
            return True, sql_file.read_text()

    return True, result.stdout


def deploy_to_spg(work_dir: Path, object_name: str, skill_dir: Path, spg_conn: str = "") -> tuple[bool, str]:
    """Deploy a single converted object to SPG. Returns (success, output_or_error)."""
    deploy_script = skill_dir / "scripts" / "deploy_to_spg.py"
    if not deploy_script.exists():
        return False, f"deploy_to_spg.py not found at {deploy_script}"

    cmd = [
        sys.executable, str(deploy_script),
        "--work-dir", str(work_dir),
        "--objects", object_name,
        "--single-object",
    ]
    if spg_conn:
        cmd.extend(["--connection", spg_conn])

    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "SPGLOADER_WORK_DIR": str(work_dir)},
    )

    if result.returncode != 0:
        return False, result.stderr or result.stdout or "Unknown deploy error"

    return True, result.stdout


def replay_single(work_dir: str | Path, object_name: str, source_type: str,
                  spg_conn: str = "", skill_dir: str | Path = "") -> dict:
    """Re-convert and re-deploy a single object. Returns result dict."""
    work_dir = Path(work_dir).expanduser()
    if not skill_dir:
        skill_dir = Path(os.environ.get("SPGLOADER_SKILL_DIR", "."))
    else:
        skill_dir = Path(skill_dir).expanduser()

    result = {
        "object_name": object_name,
        "source_type": source_type,
        "conversion_success": False,
        "deploy_success": False,
        "error": "",
        "converted_sql": "",
    }

    # Step 1: Re-convert
    print(f"  Re-converting: {object_name} ({source_type})")
    conv_ok, conv_output = reconvert_object(work_dir, object_name, source_type, skill_dir)
    result["conversion_success"] = conv_ok
    if not conv_ok:
        result["error"] = f"Conversion failed: {conv_output}"
        print(f"    FAIL (conversion): {conv_output[:100]}")
        return result
    result["converted_sql"] = conv_output[:1000]
    print(f"    Conversion OK")

    # Step 2: Deploy to SPG
    print(f"  Deploying: {object_name}")
    deploy_ok, deploy_output = deploy_to_spg(work_dir, object_name, skill_dir, spg_conn)
    result["deploy_success"] = deploy_ok
    if not deploy_ok:
        result["error"] = f"Deploy failed: {deploy_output}"
        print(f"    FAIL (deploy): {deploy_output[:100]}")
        return result

    print(f"    Deploy OK ✓")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Re-convert and re-deploy a single object to verify a fix."
    )
    parser.add_argument("work_dir", help="Path to the migration workspace")
    parser.add_argument("--object", required=True, help="Object name to replay")
    parser.add_argument("--source-type", required=True, help="Source DB type (mssql/mysql/oracle)")
    parser.add_argument("--spg-conn", default="", help="SPG connection name")
    parser.add_argument("--skill-dir", default="", help="Path to spgloader skill directory")
    args = parser.parse_args()

    result = replay_single(
        args.work_dir, args.object, args.source_type,
        spg_conn=args.spg_conn, skill_dir=args.skill_dir
    )

    # Print JSON result
    print(json.dumps(result, indent=2))

    # Exit code based on deploy success
    sys.exit(0 if result["deploy_success"] else 1)


if __name__ == "__main__":
    main()
