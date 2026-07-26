#!/usr/bin/env python3
"""
load_source_ddl.py — Load a DDL file into the source database Docker container.

When the DDL came from a file (not a live existing database), pgloader still needs
a live connection to read schema metadata.  This script creates a dedicated migration
database in the running source container and loads the DDL there, giving pgloader a
real catalog to query.

After loading, it updates source_conn.env with the new SOURCE_DATABASE value.

Supported sources:
    mssql  — uses sqlcmd inside spgloader_mssql container
    mysql  — uses mysql client inside spgloader_mysql container

Usage:
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \\
        --source-type  mssql \\
        --ddl-file     /path/to/schema.sql \\
        --database     migration_db \\
        --password-env MSSQL_SA_PASSWORD \\
        --work-dir     $SPGLOADER_WORK_DIR
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Container names and tooling per source type
# ---------------------------------------------------------------------------

CONTAINER_INFO = {
    "mssql": {
        "container": "spgloader_mssql",
        "default_db": "master",
        "default_user": "sa",
        "sqlcmd": "/opt/mssql-tools18/bin/sqlcmd",
        "password_env": "MSSQL_SA_PASSWORD",
    },
    "mysql": {
        "container": "spgloader_mysql",
        "default_db": "mysql",
        "default_user": "root",
        "password_env": "MYSQL_ROOT_PASSWORD",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, optionally capturing output."""
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
        if result.stdout:
            print(result.stdout[:500], file=sys.stderr)
        if result.stderr:
            print(result.stderr[:500], file=sys.stderr)
        sys.exit(1)
    return result


def container_running(name: str) -> bool:
    r = run(["docker", "inspect", "--format", "{{.State.Running}}", name], check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# MSSQL loader
# ---------------------------------------------------------------------------

def load_mssql(ddl_file: Path, database: str, password: str, container: str, sqlcmd: str) -> None:
    print(f"  Container : {container}")
    print(f"  Database  : {database}")
    print(f"  DDL file  : {ddl_file}")

    # 1. Create the database if it does not exist
    create_sql = (
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = '{database}') "
        f"CREATE DATABASE [{database}]"
    )
    print(f"  Creating database '{database}' (if not exists)...")
    run([
        "docker", "exec", container,
        sqlcmd, "-S", "localhost", "-U", "sa", "-P", password,
        "-No", "-Q", create_sql,
    ])

    # 2. Copy the DDL file into the container
    container_path = f"/tmp/spgloader_ddl_{ddl_file.name}"
    print(f"  Copying DDL into container → {container_path}")
    run(["docker", "cp", str(ddl_file), f"{container}:{container_path}"])

    # 3. Run the DDL file inside the container against the target database
    print(f"  Loading DDL into [{database}]...")
    result = run([
        "docker", "exec", container,
        sqlcmd, "-S", "localhost", "-U", "sa", "-P", password,
        "-No", "-d", database, "-i", container_path,
    ], check=False)

    if result.returncode != 0:
        # sqlcmd exits non-zero on warnings — filter for real ERROR lines
        error_lines = [l for l in (result.stdout + result.stderr).splitlines()
                       if "error" in l.lower() and "warning" not in l.lower()]
        if error_lines:
            print("  Errors during DDL load (warnings are expected for some objects):",
                  file=sys.stderr)
            for line in error_lines[:10]:
                print(f"    {line}", file=sys.stderr)

    print(f"  DDL loaded into {container}/{database}")


# ---------------------------------------------------------------------------
# MySQL loader
# ---------------------------------------------------------------------------

def load_mysql(ddl_file: Path, database: str, password: str, container: str) -> None:
    print(f"  Container : {container}")
    print(f"  Database  : {database}")
    print(f"  DDL file  : {ddl_file}")

    # 1. Create the database if it does not exist
    print(f"  Creating database '{database}' (if not exists)...")
    run([
        "docker", "exec", container,
        "mysql", f"-uroot", f"-p{password}",
        "-e", f"CREATE DATABASE IF NOT EXISTS `{database}`",
    ])

    # 2. Copy the DDL file into the container
    container_path = f"/tmp/spgloader_ddl_{ddl_file.name}"
    print(f"  Copying DDL into container → {container_path}")
    run(["docker", "cp", str(ddl_file), f"{container}:{container_path}"])

    # 3. Run the DDL file inside the container against the target database
    print(f"  Loading DDL into `{database}`...")
    result = run([
        "docker", "exec", "-i", container,
        "mysql", f"-uroot", f"-p{password}", database,
        "-e", f"source {container_path}",
    ], check=False)

    if result.returncode != 0:
        error_lines = [l for l in (result.stdout + result.stderr).splitlines()
                       if "error" in l.lower()]
        if error_lines:
            print("  Errors during DDL load:", file=sys.stderr)
            for line in error_lines[:10]:
                print(f"    {line}", file=sys.stderr)

    print(f"  DDL loaded into {container}/{database}")


# ---------------------------------------------------------------------------
# source_conn.env updater
# ---------------------------------------------------------------------------

def update_source_conn_env(work_dir: Path, database: str) -> None:
    env_file = work_dir / "source_conn.env"
    if not env_file.exists():
        print(f"Warning: {env_file} not found — cannot update SOURCE_DATABASE",
              file=sys.stderr)
        return

    lines = env_file.read_text().splitlines()
    updated = []
    found = False
    for line in lines:
        if line.startswith("SOURCE_DATABASE="):
            updated.append(f"SOURCE_DATABASE={database}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"SOURCE_DATABASE={database}")

    env_file.write_text("\n".join(updated) + "\n")
    print(f"  Updated {env_file}: SOURCE_DATABASE={database}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a DDL file into the source DB Docker container for pgloader"
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql"],
                        help="Source database type")
    parser.add_argument("--ddl-file", required=True,
                        help="Path to the DDL .sql file to load")
    parser.add_argument("--database", required=True,
                        help="Name of the database to create and load into")
    parser.add_argument("--password-env", required=True,
                        help="Name of env var holding the source DB admin password")
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory (to update source_conn.env)")
    args = parser.parse_args()

    ddl_file = Path(args.ddl_file).expanduser().resolve()
    if not ddl_file.exists():
        print(f"Error: DDL file not found: {ddl_file}", file=sys.stderr)
        sys.exit(1)

    work_dir = Path(args.work_dir).expanduser().resolve()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"Error: env var '{args.password_env}' is not set.", file=sys.stderr)
        sys.exit(1)

    info = CONTAINER_INFO[args.source_type]
    container = info["container"]

    if not container_running(container):
        print(f"Error: container '{container}' is not running.", file=sys.stderr)
        print(f"  Start it with: docker compose -f references/docker-templates/"
              f"{args.source_type}-compose.yml up -d", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoading DDL into {args.source_type} container...")

    if args.source_type == "mssql":
        load_mssql(ddl_file, args.database, password, container, info["sqlcmd"])
    elif args.source_type == "mysql":
        load_mysql(ddl_file, args.database, password, container)

    update_source_conn_env(work_dir, args.database)

    print(f"\nSource DB ready: {args.source_type} @ {container}/{args.database}")
    print("pgloader can now connect to the live source database.")


if __name__ == "__main__":
    main()
