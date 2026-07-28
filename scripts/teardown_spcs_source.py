#!/usr/bin/env python3
"""
teardown_spcs_source.py — Drop the SPCS service and compute pool created by setup_spcs_source.py.

Reads SPCS_SERVICE and SPCS_POOL from source_conn.env in the workspace directory,
then issues DROP SERVICE and DROP COMPUTE POOL.

Usage:
    uv run python scripts/teardown_spcs_source.py \\
        --work-dir $SPGLOADER_WORK_DIR

    # Confirm automatically (no prompt)
    uv run python scripts/teardown_spcs_source.py \\
        --work-dir $SPGLOADER_WORK_DIR --yes

    # Override service/pool names explicitly
    uv run python scripts/teardown_spcs_source.py \\
        --work-dir $SPGLOADER_WORK_DIR \\
        --service-name spgloader_source_db \\
        --pool-name    spgloader_source_pool
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# snow CLI helper
# ---------------------------------------------------------------------------

def _snow_sql(sql: str, connection: str | None = None) -> list[dict]:
    cmd = ["snow", "sql", "-q", sql, "--format=json"]
    if connection:
        cmd += ["--connection", connection]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"snow sql failed:\n  SQL: {sql[:100]}\n"
            f"  ERR: {(result.stdout + result.stderr)[:400]}"
        )
    raw = result.stdout.strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# source_conn.env reader
# ---------------------------------------------------------------------------

def _read_source_conn_env(work_dir: Path) -> dict[str, str]:
    env_file = work_dir / "source_conn.env"
    if not env_file.exists():
        return {}
    env: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"')
    return env


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def teardown(
    service_name: str,
    pool_name: str,
    connection: str | None,
    yes: bool,
) -> None:
    print("=== SPCS Teardown ===")
    print(f"  Service      : {service_name}")
    print(f"  Compute pool : {pool_name}")

    if not yes:
        answer = input("\nDrop these SPCS resources? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Drop service
    print(f"\nDropping service '{service_name}'...")
    try:
        _snow_sql(f"DROP SERVICE IF EXISTS {service_name}", connection=connection)
        print(f"  Service '{service_name}' dropped.")
    except RuntimeError as e:
        print(f"  WARNING: could not drop service: {e}", file=sys.stderr)

    # Drop compute pool
    print(f"Dropping compute pool '{pool_name}'...")
    try:
        _snow_sql(f"DROP COMPUTE POOL IF EXISTS {pool_name}", connection=connection)
        print(f"  Compute pool '{pool_name}' dropped.")
    except RuntimeError as e:
        print(f"  WARNING: could not drop compute pool: {e}", file=sys.stderr)

    print("\nSPCS resources cleaned up.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drop the SPCS service and compute pool created by setup_spcs_source.py"
    )
    parser.add_argument(
        "--work-dir", required=True,
        help="spgloader workspace directory (reads SPCS_SERVICE / SPCS_POOL from source_conn.env)",
    )
    parser.add_argument(
        "--service-name",
        help="Override SPCS service name (default: read from source_conn.env)",
    )
    parser.add_argument(
        "--pool-name",
        help="Override compute pool name (default: read from source_conn.env)",
    )
    parser.add_argument(
        "--connection",
        help="Snowflake connection name (passed to snow --connection)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()
    env = _read_source_conn_env(work_dir)

    service_name = args.service_name or env.get("SPCS_SERVICE", "")
    pool_name = args.pool_name or env.get("SPCS_POOL", "")

    if not service_name:
        print(
            "ERROR: SPCS_SERVICE not found in source_conn.env and --service-name not provided.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not pool_name:
        print(
            "ERROR: SPCS_POOL not found in source_conn.env and --pool-name not provided.",
            file=sys.stderr,
        )
        sys.exit(1)

    teardown(service_name, pool_name, args.connection, args.yes)


if __name__ == "__main__":
    main()
