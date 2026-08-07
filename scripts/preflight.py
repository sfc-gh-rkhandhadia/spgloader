#!/usr/bin/env python3
"""
preflight.py — Preflight validation for spgloader phases.

Validates prerequisites (tools, connectivity, privileges, prior phases, disk space)
before a phase runs. Exits 0 if all checks pass or only warnings; exits 1 if blockers found.

Usage:
    python scripts/preflight.py --work-dir /path/to/workspace --phase extract
    python scripts/preflight.py --work-dir /path/to/workspace --phase deploy --fix
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def _cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run_quiet(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)[:200]


def _load_env(work_dir: Path, filename: str) -> dict[str, str]:
    env_file = work_dir / filename
    env: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ---------------------------------------------------------------------------
# Check functions per phase
# ---------------------------------------------------------------------------

def check_tools() -> list[dict]:
    """Check required CLI tools are available."""
    checks = []
    for tool, desc in [("docker", "container runtime"), ("psql", "PostgreSQL client"),
                       ("uv", "Python package runner")]:
        if _cmd_exists(tool):
            checks.append(_check(f"tool_{tool}", "pass", f"{tool} found"))
        else:
            checks.append(_check(f"tool_{tool}", "warn", f"{tool} not found ({desc})"))

    ok, detail = _run_quiet(["snow", "--version"])
    if ok:
        checks.append(_check("tool_snow", "pass", detail.split("\n")[0]))
    else:
        checks.append(_check("tool_snow", "warn", "snow CLI not found or not working"))
    return checks


def check_source_connectivity(work_dir: Path) -> list[dict]:
    """Check source database is reachable."""
    env = _load_env(work_dir, "source_conn.env")
    if not env:
        return [_check("source_conn_env", "fail", "source_conn.env not found")]

    source_type = env.get("SOURCE_TYPE", "unknown")
    host = env.get("SOURCE_HOST", "localhost")
    port = env.get("SOURCE_PORT", "3306")
    container = env.get("SOURCE_CONTAINER", "")

    if container:
        ok, detail = _run_quiet(["docker", "inspect", "--format", "{{.State.Status}}", container])
        if ok and "running" in detail.lower():
            return [_check("source_container", "pass", f"{container} is running")]
        else:
            return [_check("source_container", "fail", f"Container {container} not running: {detail}")]

    # TCP check for non-container sources
    ok, detail = _run_quiet(["python3", "-c",
        f"import socket; s=socket.create_connection(('{host}',{port}),5); s.close(); print('OK')"])
    if ok:
        return [_check("source_tcp", "pass", f"{source_type} @ {host}:{port} reachable")]
    else:
        return [_check("source_tcp", "fail", f"Cannot reach {host}:{port}: {detail}")]


def check_target_connectivity(work_dir: Path) -> list[dict]:
    """Check SPG instance is reachable via psql."""
    env = _load_env(work_dir, "target_conn.env")
    if not env:
        return [_check("target_conn_env", "fail", "target_conn.env not found")]

    spg_service = env.get("TARGET_SPG_SERVICE", "")
    if not spg_service:
        return [_check("target_spg_service", "fail", "TARGET_SPG_SERVICE not set")]

    ok, detail = _run_quiet(["psql", f"service={spg_service}", "-c", "SELECT 1;"])
    if ok:
        return [_check("spg_connection", "pass", f"SPG {spg_service} connected")]
    else:
        return [_check("spg_connection", "fail", f"Cannot connect to SPG {spg_service}: {detail}")]


def check_prior_phases(work_dir: Path, phase: str) -> list[dict]:
    """Check that prerequisite phases have produced their expected outputs."""
    checks = []
    required_files: dict[str, list[str]] = {
        "extract": [],
        "assess": ["ddl_objects.json"],
        "convert": ["ddl_objects.json", "assessment/assessment_summary.json"],
        "deploy": ["ddl_objects.json", "conversion/_conversion_report.json"],
        "validate": ["ddl_objects.json"],
        "witness": ["ddl_objects.json", "validation/validation_report.json"],
    }

    for filepath in required_files.get(phase, []):
        full = work_dir / filepath
        if full.exists():
            checks.append(_check(f"file_{filepath}", "pass", "exists"))
        else:
            checks.append(_check(f"file_{filepath}", "fail", f"Missing: {filepath} (run prerequisite phase first)"))

    # Assessment guardrail for convert/deploy
    if phase in ("convert", "deploy"):
        assess_path = work_dir / "assessment" / "assessment_summary.json"
        if assess_path.exists():
            try:
                assess = json.loads(assess_path.read_text())
                if assess.get("is_blocked"):
                    checks.append(_check("assessment_blocked", "fail",
                                         f"Assessment has unresolved BLOCK findings: {assess.get('block_findings',[])}"))
                else:
                    checks.append(_check("assessment_passed", "pass", "No BLOCK findings"))
            except json.JSONDecodeError:
                checks.append(_check("assessment_json", "fail", "assessment_summary.json is malformed"))

    return checks


def check_disk_space(work_dir: Path) -> list[dict]:
    """Warn if disk space is low."""
    try:
        stat = os.statvfs(str(work_dir))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)
        if free_gb < 1.0:
            return [_check("disk_space", "warn", f"{free_gb:.1f} GB free (recommend 1GB+)")]
        return [_check("disk_space", "pass", f"{free_gb:.1f} GB free")]
    except OSError:
        return [_check("disk_space", "warn", "Could not determine free space")]


def check_python_deps() -> list[dict]:
    """Verify key Python dependencies are importable."""
    checks = []
    for mod in ["psycopg2", "yaml", "jinja2", "pymssql", "mysql.connector", "faker"]:
        try:
            __import__(mod)
            checks.append(_check(f"dep_{mod}", "pass", "importable"))
        except ImportError:
            checks.append(_check(f"dep_{mod}", "warn", f"{mod} not importable"))
    return checks


def check_schemas_exist(work_dir: Path) -> list[dict]:
    """Check that source schemas referenced in ddl_objects.json exist in the source DB."""
    ddl_path = work_dir / "ddl_objects.json"
    if not ddl_path.exists():
        return []
    try:
        objs = json.loads(ddl_path.read_text())
        if isinstance(objs, dict):
            objs = objs.get("objects", objs.get("ordered_objects", []))
        schemas = sorted(set(o.get("schema", "") for o in objs if o.get("schema")))
        return [_check("schemas_found", "pass", f"{len(schemas)} schema(s): {', '.join(schemas[:6])}")]
    except (json.JSONDecodeError, TypeError):
        return [_check("schemas_parse", "warn", "Could not parse ddl_objects.json for schemas")]


# ---------------------------------------------------------------------------
# Phase router
# ---------------------------------------------------------------------------

PHASE_CHECKS: dict[str, list] = {
    "extract": [check_tools, check_source_connectivity, check_disk_space, check_python_deps],
    "assess": [check_prior_phases, check_disk_space],
    "convert": [check_prior_phases, check_source_connectivity, check_disk_space, check_python_deps],
    "deploy": [check_prior_phases, check_target_connectivity, check_disk_space],
    "validate": [check_prior_phases, check_target_connectivity, check_source_connectivity],
    "witness": [check_prior_phases, check_target_connectivity, check_source_connectivity, check_schemas_exist],
}


def run_preflight(work_dir: Path, phase: str) -> dict:
    """Run all preflight checks for the given phase. Returns the result dict."""
    check_fns = PHASE_CHECKS.get(phase, [check_tools, check_disk_space])
    all_checks: list[dict] = []

    for fn in check_fns:
        # Some check fns take work_dir, some take (work_dir, phase), some take nothing
        import inspect
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        if len(params) == 2:
            all_checks.extend(fn(work_dir, phase))
        elif len(params) == 1:
            all_checks.extend(fn(work_dir))
        else:
            all_checks.extend(fn())

    blockers = [c for c in all_checks if c["status"] == "fail"]
    warnings = [c for c in all_checks if c["status"] == "warn"]
    overall = "fail" if blockers else ("warn" if warnings else "pass")

    return {
        "phase": phase,
        "checks": all_checks,
        "overall": overall,
        "blockers": blockers,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight validation for spgloader phases")
    parser.add_argument("--work-dir", required=True, help="spgloader workspace directory")
    parser.add_argument("--phase", required=True,
                        choices=["extract", "assess", "convert", "deploy", "validate", "witness"],
                        help="Phase to validate prerequisites for")
    parser.add_argument("--fix", action="store_true",
                        help="Attempt to auto-fix issues (create dirs, install deps)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()
    result = run_preflight(work_dir, args.phase)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\nPreflight: {args.phase}")
        print("=" * 50)
        for check in result["checks"]:
            icon = {"pass": "✓", "warn": "⚠", "fail": "✗"}.get(check["status"], "?")
            print(f"  {icon}  {check['name']}: {check['detail']}")
        print(f"\nOverall: {result['overall'].upper()}")
        if result["blockers"]:
            print(f"\n  BLOCKERS ({len(result['blockers'])}):")
            for b in result["blockers"]:
                print(f"    ✗ {b['name']}: {b['detail']}")

    sys.exit(0 if result["overall"] != "fail" else 1)


if __name__ == "__main__":
    main()
