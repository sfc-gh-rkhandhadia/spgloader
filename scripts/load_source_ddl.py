#!/usr/bin/env python3
"""
load_source_ddl.py — Load DDL into the source database Docker container.

Accepts either:
  --ddl-file PATH   A single combined .sql file (original behaviour)
  --ddl-dir  PATH   A directory of individual .sql files (SSMS "Scripts and
                    Tables" export format — one file per object)

When --ddl-dir is given the script:
  1. Auto-detects per-file encoding (UTF-16 LE BOM \xff\xfe is common for SSMS
     exports; falls back to UTF-8).
  2. Strips "USE [dbname]" lines so the correct target database is used.
  3. Combines files in dependency order:
       Schema → UserDefinedTableType → Table → Function → View →
       StoredProcedure/Trigger → everything else
  4. Loads the combined DDL into the container.
  5. Runs a second FK-only pass to resolve FK ordering failures that occur when
     tables are sorted alphabetically and reference later-named tables.

After loading, updates source_conn.env with the new SOURCE_DATABASE value.

Supported sources:
    mssql  — uses sqlcmd inside spgloader_mssql container
    mysql  — uses mysql client inside spgloader_mysql container

Usage (single file — original):
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \\
        --source-type  mssql \\
        --ddl-file     /path/to/schema.sql \\
        --database     migration_db \\
        --password-env MSSQL_SA_PASSWORD \\
        --work-dir     $SPGLOADER_WORK_DIR

Usage (SSMS export directory):
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \\
        --source-type  mssql \\
        --ddl-dir      "/path/to/Acuity Objects Scripts and Tables report" \\
        --database     migration_db \\
        --password-env MSSQL_SA_PASSWORD \\
        --work-dir     $SPGLOADER_WORK_DIR
"""
from __future__ import annotations

import argparse
import os
import re
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

# SSMS object type priority — lower number = deployed first.
# Derived from the file extension segment: dbo.TableName.<TypeKey>.sql
_SSMS_TYPE_PRIORITY = {
    "Schema": 0,
    "UserDefinedTableType": 1,
    "Table": 2,
    "UserDefinedFunction": 3,
    "View": 4,
    "StoredProcedure": 5,
    "Trigger": 6,
    "UnresolvedEntity": 9,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, optionally capturing output."""
    result = subprocess.run(cmd, capture_output=capture, text=True)
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


def _detect_and_read(path: Path) -> str:
    """Read a SQL file, auto-detecting UTF-16 LE (SSMS BOM \xff\xfe) or UTF-8."""
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xfe":
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace")
    return raw.decode("utf-8", errors="replace")


def combine_ddl_directory(ddl_dir: Path) -> str:
    """
    Combine a directory of individual SSMS-generated .sql files into a single
    ordered DDL string suitable for sqlcmd / mysql execution.

    - Detects per-file encoding (UTF-16 LE or UTF-8).
    - Strips USE [dbname] lines so the correct target database context is used.
    - Orders files by object type (Schema first, Views last) to minimise
      forward-reference failures.
    """
    files = sorted(
        ddl_dir.glob("*.sql"),
        key=lambda f: (
            _SSMS_TYPE_PRIORITY.get(f.stem.split(".")[-1], 8),
            f.name,
        ),
    )
    if not files:
        print(f"  Warning: no .sql files found in {ddl_dir}", file=sys.stderr)
        return ""

    counts: dict[str, int] = {}
    parts: list[str] = []
    for f in files:
        obj_type = f.stem.split(".")[-1]
        counts[obj_type] = counts.get(obj_type, 0) + 1
        content = _detect_and_read(f)
        # Strip USE [any_database_name] so the -d flag controls context
        content = re.sub(
            r"^\s*USE\s+\[[^\]]*\]\s*\r?\n?", "", content, flags=re.MULTILINE | re.IGNORECASE
        )
        parts.append(f"-- === {f.name} ===\n{content.strip()}\nGO\n")

    combined = "\n".join(parts)
    type_summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"  Combined {len(files)} files ({type_summary})")
    return combined


def _extract_fk_statements(ddl: str) -> str:
    """
    Extract ALTER TABLE ... FOREIGN KEY ... statements for a second-pass load.
    Used to resolve FK failures caused by alphabetical table ordering.
    """
    fk_stmts = re.findall(
        r"ALTER TABLE[^;G]*FOREIGN KEY[^G]*GO",
        ddl,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return "\n".join(fk_stmts)


# ---------------------------------------------------------------------------
# MSSQL loader
# ---------------------------------------------------------------------------

def load_mssql(ddl_file: Path, database: str, password: str, container: str,
               sqlcmd: str, run_fk_pass: bool = False) -> None:
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
        error_lines = [
            line for line in (result.stdout + result.stderr).splitlines()
            if "error" in line.lower() and "warning" not in line.lower()
        ]
        if error_lines:
            print("  Errors during DDL load (FK ordering errors are expected and "
                  "will be retried):", file=sys.stderr)
            for line in error_lines[:5]:
                print(f"    {line}", file=sys.stderr)
            if len(error_lines) > 5:
                print(f"    ... ({len(error_lines) - 5} more)", file=sys.stderr)

    print(f"  DDL loaded into {container}/{database}")

    # 4. Optional second FK pass — fixes ordering failures
    if run_fk_pass:
        print("  Running second FK pass to resolve ordering failures...")
        combined_ddl = ddl_file.read_text(encoding="utf-8", errors="replace")
        fk_sql = _extract_fk_statements(combined_ddl)
        if fk_sql:
            fk_path = ddl_file.parent / "_fk_pass2.sql"
            fk_path.write_text(fk_sql, encoding="utf-8")
            fk_container_path = "/tmp/spgloader_fk_pass2.sql"
            run(["docker", "cp", str(fk_path), f"{container}:{fk_container_path}"])
            run([
                "docker", "exec", container,
                sqlcmd, "-S", "localhost", "-U", "sa", "-P", password,
                "-No", "-d", database, "-i", fk_container_path,
            ], check=False)
            # Count FKs now present
            fk_count_result = run([
                "docker", "exec", container,
                sqlcmd, "-S", "localhost", "-U", "sa", "-P", password,
                "-No", "-d", database,
                "-Q", "SELECT COUNT(*) FROM sys.foreign_keys",
            ], check=False)
            fk_count = fk_count_result.stdout.strip().splitlines()
            print(f"  FK pass 2 complete. FKs in DB: {fk_count[2].strip() if len(fk_count) > 2 else '?'}")
        else:
            print("  No FK statements found in combined DDL — skipping second pass.")


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
        error_lines = [
            line for line in (result.stdout + result.stderr).splitlines()
            if "error" in line.lower()
        ]
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
        description=(
            "Load a DDL file (or directory of SSMS-exported .sql files) into "
            "the source DB Docker container for catalog-based extraction."
        )
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql"],
                        help="Source database type")

    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--ddl-file",
                           help="Path to a single combined .sql file to load")
    src_group.add_argument("--ddl-dir",
                           help=(
                               "Path to a directory of individual .sql files "
                               "(SSMS 'Scripts and Tables' export format). "
                               "Files are combined in dependency order and "
                               "loaded as a single batch."
                           ))

    parser.add_argument("--database", required=True,
                        help="Name of the database to create and load into")
    parser.add_argument("--password-env", required=True,
                        help="Name of env var holding the source DB admin password")
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory (to update source_conn.env)")
    args = parser.parse_args()

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

    # ── Resolve the DDL to a single file ────────────────────────────────────
    run_fk_pass = False
    if args.ddl_file:
        ddl_file = Path(args.ddl_file).expanduser().resolve()
        if not ddl_file.exists():
            print(f"Error: DDL file not found: {ddl_file}", file=sys.stderr)
            sys.exit(1)
    else:
        ddl_dir = Path(args.ddl_dir).expanduser().resolve()
        if not ddl_dir.is_dir():
            print(f"Error: DDL directory not found: {ddl_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"\nCombining DDL directory: {ddl_dir}")
        combined = combine_ddl_directory(ddl_dir)
        if not combined:
            print("Error: no SQL content found in directory.", file=sys.stderr)
            sys.exit(1)
        # Write combined file to workspace
        ddl_file = work_dir / "combined_ddl.sql"
        ddl_file.write_text(combined, encoding="utf-8")
        print(f"  Written: {ddl_file}  ({ddl_file.stat().st_size / 1024:.0f} KB)")
        run_fk_pass = True   # directory loads benefit from a second FK pass

    # ── Load ────────────────────────────────────────────────────────────────
    print(f"\nLoading DDL into {args.source_type} container...")

    if args.source_type == "mssql":
        load_mssql(ddl_file, args.database, password, container, info["sqlcmd"],
                   run_fk_pass=run_fk_pass)
    elif args.source_type == "mysql":
        load_mysql(ddl_file, args.database, password, container)

    update_source_conn_env(work_dir, args.database)

    print(f"\nSource DB ready: {args.source_type} @ {container}/{args.database}")
    print("Catalog-based extraction can now connect to the live source database.")


if __name__ == "__main__":
    main()
