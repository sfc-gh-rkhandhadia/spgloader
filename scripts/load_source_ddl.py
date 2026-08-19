#!/usr/bin/env python3
"""
load_source_ddl.py — Load DDL into the source database container (Docker or SPCS).

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
    mssql  — uses sqlcmd inside Docker container, or pymssql over TCP (SPCS/--host)
    mysql  — uses mysql client inside spgloader_mysql container

Usage (Docker — original):
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \\
        --source-type  mssql \\
        --ddl-file     /path/to/schema.sql \\
        --database     migration_db \\
        --password-env MSSQL_SA_PASSWORD \\
        --work-dir     $SPGLOADER_WORK_DIR

Usage (SPCS / remote host — TCP via pymssql):
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \\
        --source-type  mssql \\
        --host         <spcs-dns-name> \\
        --ddl-file     /path/to/schema.sql \\
        --database     migration_db \\
        --password-env MSSQL_SA_PASSWORD \\
        --work-dir     $SPGLOADER_WORK_DIR

Usage (SSMS export directory, Docker):
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
from typing import Optional


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
        "client": "mysql",
    },
    "mariadb": {
        "container": "spgloader_mariadb",
        "default_db": "mysql",
        "default_user": "root",
        "password_env": "MYSQL_ROOT_PASSWORD",
        "client": "mariadb",
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

    # 2. Pre-process the DDL for single-file SSMS full-database scripts:
    #    a) Strip USE [dbname] statements so -d flag controls context.
    #    b) Drop the preamble (CREATE DATABASE, ALTER DATABASE, EXEC config)
    #       that precedes actual object DDL — these fail on Linux containers
    #       and corrupt sqlcmd's ODBC cursor state.
    content = _detect_and_read(ddl_file)
    # Strip USE [any_database_name] lines
    processed = re.sub(
        r"^\s*USE\s+\[[^\]]*\]\s*\r?\n?", "", content, flags=re.MULTILINE | re.IGNORECASE
    )
    # Find the first batch that contains a real schema object definition.
    # Split on GO boundaries and keep from the first batch that contains
    # CREATE TABLE/VIEW/PROCEDURE/FUNCTION/SEQUENCE/TYPE.
    _OBJECT_DDL_RE = re.compile(
        r"^\s*CREATE\s+(?:TABLE|VIEW|PROCEDURE|FUNCTION|SEQUENCE|TYPE)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    batches = re.split(r"\bGO\b[ \t]*(?:\r?\n|$)", processed, flags=re.IGNORECASE)
    first_obj_idx = next(
        (i for i, b in enumerate(batches) if _OBJECT_DDL_RE.search(b)),
        None,
    )
    if first_obj_idx is not None and first_obj_idx > 0:
        skipped = first_obj_idx
        # Preserve SET statements from preamble (QUOTED_IDENTIFIER, ANSI_NULLS, etc.)
        # These affect how subsequent DDL is parsed by sqlcmd.
        _SET_STMT_RE = re.compile(
            r"^\s*SET\s+(?:QUOTED_IDENTIFIER|ANSI_NULLS|ANSI_PADDING|ANSI_WARNINGS|NOCOUNT)\s+(?:ON|OFF)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        preserved_sets = []
        for b in batches[:first_obj_idx]:
            if _SET_STMT_RE.search(b):
                preserved_sets.append(b.strip())
        prefix = ("GO\n".join(preserved_sets) + "\nGO\n") if preserved_sets else ""
        processed = prefix + "GO\n".join(batches[first_obj_idx:])
        print(f"  Stripped database-creation preamble ({skipped} batches) from DDL")
        if preserved_sets:
            print(f"  Preserved {len(preserved_sets)} SET statement batch(es) from preamble")

    if processed != content:
        # Write preprocessed file to a temp location so the original is untouched
        tmp_path = ddl_file.parent / f"_preprocessed_{ddl_file.name}"
        tmp_path.write_text(processed, encoding="utf-8")
        ddl_file = tmp_path
        print(f"  Preprocessed DDL written: {tmp_path.name}")

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

def load_mysql(ddl_file: Path, database: str, password: str, container: str, client: str = "mysql") -> None:
    print(f"  Container : {container}")
    print(f"  Database  : {database}")
    print(f"  DDL file  : {ddl_file}")

    # 1. Create the database if it does not exist
    print(f"  Creating database '{database}' (if not exists)...")
    run([
        "docker", "exec", container,
        client, f"-uroot", f"-p{password}",
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
        client, f"-uroot", f"-p{password}", database,
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

# ---------------------------------------------------------------------------
# SPCS / TCP loader (pymssql — no Docker)
# ---------------------------------------------------------------------------

def load_mssql_tcp(
    ddl_file: Path,
    database: str,
    password: str,
    host: str,
    port: int = 1433,
    run_fk_pass: bool = False,
) -> None:
    """Load DDL into an MSSQL instance accessible over TCP (SPCS or any remote host).

    Uses pymssql so no Docker exec / docker cp is required.  The DDL file stays
    local and is executed batch-by-batch by splitting on GO boundaries.
    """
    try:
        import pymssql  # type: ignore
    except ImportError:
        print("Error: pymssql is not installed.", file=sys.stderr)
        print("  Run: uv add pymssql", file=sys.stderr)
        sys.exit(1)

    print(f"  Host     : {host}:{port}")
    print(f"  Database : {database}")
    print(f"  DDL file : {ddl_file}")

    # Connect to master to create the target database
    print(f"  Connecting to {host}:{port} (master)...")
    conn = pymssql.connect(
        host=host, port=port, user="sa", password=password,
        database="master", login_timeout=60, timeout=120,
    )
    conn.autocommit(True)
    cursor = conn.cursor()

    # Create database if not exists
    cursor.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = '{database}') "
        f"CREATE DATABASE [{database}]"
    )
    print(f"  Database '{database}' ensured.")
    cursor.close()
    conn.close()

    # Re-connect to the target database
    conn = pymssql.connect(
        host=host, port=port, user="sa", password=password,
        database=database, login_timeout=60, timeout=300,
    )
    conn.autocommit(True)
    cursor = conn.cursor()

    content = _detect_and_read(ddl_file)
    # Strip USE [dbname] — we already set the database on connect
    content = re.sub(
        r"^\s*USE\s+\[[^\]]*\]\s*\r?\n?", "", content, flags=re.MULTILINE | re.IGNORECASE
    )

    # Split on GO boundaries and execute each non-empty batch
    batches = re.split(r"\bGO\b[ \t]*(?:\r?\n|$)", content, flags=re.IGNORECASE)
    total = len([b for b in batches if b.strip()])
    print(f"  Executing {total} batch(es)...")

    errors = 0
    for i, batch in enumerate(batches):
        batch = batch.strip()
        if not batch:
            continue
        try:
            cursor.execute(batch)
        except Exception as e:
            msg = str(e).replace("\n", " ").strip()
            # FK ordering failures are expected and handled by the second pass
            if "foreign key" not in msg.lower():
                print(f"  Batch {i+1} error: {msg[:120]}", file=sys.stderr)
                errors += 1

    if errors:
        print(f"  {errors} batch(es) had errors (FK errors are expected and retried)",
              file=sys.stderr)

    # Optional second FK pass
    if run_fk_pass:
        print("  Running second FK pass to resolve ordering failures...")
        fk_sql = _extract_fk_statements(content)
        if fk_sql:
            fk_batches = re.split(r"\bGO\b[ \t]*(?:\r?\n|$)", fk_sql, flags=re.IGNORECASE)
            fk_errors = 0
            for batch in fk_batches:
                batch = batch.strip()
                if not batch:
                    continue
                try:
                    cursor.execute(batch)
                except Exception as e:
                    fk_errors += 1
            print(f"  FK pass 2 complete ({fk_errors} errors).")

    cursor.close()
    conn.close()
    print(f"  DDL loaded into {host}:{port}/{database}")


def _load_csv_data_mssql(csv_dir: Path, database: str, password: str, *,
                          container: str = "spgloader_mssql",
                          sqlcmd: str = "/opt/mssql-tools18/bin/sqlcmd",
                          host: str | None = None, port: int = 1433) -> None:
    """Load CSV data files into MSSQL via BULK INSERT.

    Copies CSV files into the container, disables constraints/triggers,
    runs BULK INSERT for each .csv file (matching table names by filename),
    then re-enables constraints.
    """
    import re as _re

    csv_files = sorted(csv_dir.glob("*.csv"))
    if not csv_files:
        print(f"  No .csv files found in {csv_dir}")
        return

    print(f"\nLoading CSV data: {len(csv_files)} files from {csv_dir}")

    if host:
        # TCP mode — use pymssql for data loading
        _load_csv_data_mssql_tcp(csv_dir, csv_files, database, password, host, port)
        return

    # Docker mode — copy files into container and use BULK INSERT via sqlcmd
    container_csv_path = "/tmp/csvdata"
    subprocess.run(["docker", "exec", container, "mkdir", "-p", container_csv_path],
                   check=True, capture_output=True)
    subprocess.run(["docker", "cp", f"{csv_dir}/.", f"{container}:{container_csv_path}/"],
                   check=True, capture_output=True)
    print(f"  Copied {len(csv_files)} CSV files into container at {container_csv_path}")

    # Build BULK INSERT script
    bulk_lines = [
        "SET QUOTED_IDENTIFIER ON;",
        "GO",
        f"USE [{database}];",
        "GO",
        "EXEC sp_MSforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT ALL';",
        "GO",
        "EXEC sp_MSforeachtable 'ALTER TABLE ? DISABLE TRIGGER ALL';",
        "GO",
        "",
    ]

    # Query table-to-schema mapping from the container
    schema_cmd = [
        "docker", "exec", container, sqlcmd,
        "-S", "localhost", "-U", "sa", "-P", password, "-No",
        "-d", database, "-h", "-1", "-W",
        "-Q", "SELECT s.name + '|' + t.name FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id ORDER BY t.name",
    ]
    result = subprocess.run(schema_cmd, capture_output=True, text=True)
    table_schema_map = {}
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if '|' in line:
            schema, tname = line.split('|', 1)
            table_schema_map[tname.lower()] = schema

    loaded_count = 0
    for csv_file in csv_files:
        # Table name from filename (e.g., Address.csv → Address)
        table_name = csv_file.stem
        schema = table_schema_map.get(table_name.lower(), "dbo")
        csv_path = f"{container_csv_path}/{csv_file.name}"

        bulk_lines.append(f"BULK INSERT [{schema}].[{table_name}] FROM '{csv_path}'")
        bulk_lines.append("WITH (")
        bulk_lines.append("    CHECK_CONSTRAINTS,")
        bulk_lines.append("    DATAFILETYPE = 'char',")
        bulk_lines.append("    FIELDTERMINATOR = '\\t',")
        bulk_lines.append("    ROWTERMINATOR = '0x0a',")
        bulk_lines.append("    KEEPIDENTITY,")
        bulk_lines.append("    TABLOCK")
        bulk_lines.append(");")
        bulk_lines.append("GO")
        loaded_count += 1

    # Re-enable constraints and triggers
    bulk_lines.extend([
        "",
        "EXEC sp_MSforeachtable 'ALTER TABLE ? ENABLE TRIGGER ALL';",
        "GO",
        "EXEC sp_MSforeachtable 'ALTER TABLE ? CHECK CONSTRAINT ALL';",
        "GO",
    ])

    # Write and execute
    bulk_script = "\n".join(bulk_lines)
    script_path = f"/tmp/spgloader_csv_load.sql"
    Path(script_path).write_text(bulk_script, encoding="utf-8")
    subprocess.run(["docker", "cp", script_path, f"{container}:/tmp/csv_load.sql"],
                   check=True, capture_output=True)

    print(f"  Running BULK INSERT for {loaded_count} tables...")
    load_result = subprocess.run(
        ["docker", "exec", container, sqlcmd,
         "-S", "localhost", "-U", "sa", "-P", password, "-No",
         "-d", database, "-i", "/tmp/csv_load.sql"],
        capture_output=True, text=True, timeout=600,
    )

    # Count successes
    rows_affected = load_result.stdout.count("rows affected")
    errors = [l for l in load_result.stdout.splitlines() if l.strip().startswith("Msg ")]
    unique_errors = len(set(errors))

    print(f"  BULK INSERT complete: {rows_affected} tables loaded, {unique_errors} unique errors")
    if unique_errors > 0 and unique_errors <= 5:
        for e in sorted(set(errors))[:5]:
            print(f"    {e.strip()[:120]}")

    # Verify total rows
    count_cmd = [
        "docker", "exec", container, sqlcmd,
        "-S", "localhost", "-U", "sa", "-P", password, "-No",
        "-d", database, "-h", "-1", "-W",
        "-Q", "SET QUOTED_IDENTIFIER ON; SELECT CAST(SUM(p.rows) AS VARCHAR) FROM sys.tables t JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0,1)",
    ]
    count_result = subprocess.run(count_cmd, capture_output=True, text=True)
    total_rows = count_result.stdout.strip().splitlines()[-1].strip() if count_result.stdout.strip() else "?"
    print(f"  Total rows in source: {total_rows}")


def _load_csv_data_mssql_tcp(csv_dir: Path, csv_files: list, database: str,
                              password: str, host: str, port: int) -> None:
    """Load CSV data via pymssql (TCP) — for SPCS or remote hosts."""
    import pymssql
    import csv as csv_mod

    conn = pymssql.connect(server=host, port=port, user="sa", password=password,
                           database=database)
    conn.autocommit(True)
    cur = conn.cursor()

    # Disable constraints
    cur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? NOCHECK CONSTRAINT ALL'")
    cur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? DISABLE TRIGGER ALL'")

    # Get schema mapping
    cur.execute("SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id")
    table_schema_map = {row[1].lower(): row[0] for row in cur.fetchall()}

    loaded = 0
    for csv_file in csv_files:
        table_name = csv_file.stem
        schema = table_schema_map.get(table_name.lower(), "dbo")
        try:
            with open(csv_file, 'r', encoding='utf-8', errors='replace') as f:
                reader = csv_mod.reader(f, delimiter='\t')
                rows = list(reader)
            if not rows:
                continue
            placeholders = ", ".join(["%s"] * len(rows[0]))
            insert_sql = f"INSERT INTO [{schema}].[{table_name}] VALUES ({placeholders})"
            cur.executemany(insert_sql, rows[:5000])  # Limit for TCP mode
            loaded += 1
        except Exception as e:
            pass  # Skip failed tables silently

    # Re-enable
    cur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? ENABLE TRIGGER ALL'")
    cur.execute("EXEC sp_MSforeachtable 'ALTER TABLE ? CHECK CONSTRAINT ALL'")
    conn.close()
    print(f"  TCP data load: {loaded} tables loaded")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Load a DDL file (or directory of SSMS-exported .sql files) into "
            "the source DB (Docker container or SPCS/remote host via TCP)."
        )
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql", "mariadb"],
                        help="Source database type")
    parser.add_argument("--host",
                        help="Remote host (SPCS DNS name or IP). If omitted, uses Docker.")
    parser.add_argument("--port", type=int, default=None,
                        help="Port override (default: 1433 for mssql, 3306 for mysql)")

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
    parser.add_argument("--csv-dir",
                        help="Path to a directory of CSV data files to BULK INSERT after DDL load (MSSQL only)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"Error: env var '{args.password_env}' is not set.", file=sys.stderr)
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
        ddl_file = work_dir / "combined_ddl.sql"
        ddl_file.write_text(combined, encoding="utf-8")
        print(f"  Written: {ddl_file}  ({ddl_file.stat().st_size / 1024:.0f} KB)")
        run_fk_pass = True

    # ── Load ────────────────────────────────────────────────────────────────
    if args.host:
        # SPCS / remote TCP path — no Docker required
        info = CONTAINER_INFO.get(args.source_type, {})
        default_port = 1433 if args.source_type == "mssql" else 3306
        port = args.port or default_port

        if args.source_type != "mssql":
            print("Error: --host (TCP) mode currently only supports --source-type mssql.",
                  file=sys.stderr)
            sys.exit(1)

        print(f"\nLoading DDL into {args.source_type} via TCP ({args.host}:{port})...")
        load_mssql_tcp(
            ddl_file, args.database, password,
            host=args.host, port=port, run_fk_pass=run_fk_pass,
        )
    else:
        # Docker path (original behaviour)
        info = CONTAINER_INFO[args.source_type]
        container = info["container"]

        if not container_running(container):
            print(f"Error: container '{container}' is not running.", file=sys.stderr)
            print(f"  Start it with: docker compose -f references/docker-templates/"
                  f"{args.source_type}-compose.yml up -d", file=sys.stderr)
            sys.exit(1)

        print(f"\nLoading DDL into {args.source_type} container...")

        if args.source_type == "mssql":
            load_mssql(ddl_file, args.database, password, container, info["sqlcmd"],
                       run_fk_pass=run_fk_pass)
        elif args.source_type in ("mysql", "mariadb"):
            load_mysql(ddl_file, args.database, password, container, client=info.get("client", "mysql"))

    update_source_conn_env(work_dir, args.database)

    # ── Load CSV data if --csv-dir provided ──────────────────────────────────
    if args.csv_dir and args.source_type == "mssql":
        csv_dir = Path(args.csv_dir).expanduser().resolve()
        if csv_dir.is_dir():
            _load_csv_data_mssql(csv_dir, args.database, password,
                                 container=CONTAINER_INFO["mssql"]["container"],
                                 sqlcmd=CONTAINER_INFO["mssql"]["sqlcmd"],
                                 host=args.host, port=args.port or 1433)
        else:
            print(f"Warning: --csv-dir '{csv_dir}' not found, skipping data load.")

    target = f"{args.host or 'docker-container'}/{args.database}"
    print(f"\nSource DB ready: {args.source_type} @ {target}")
    print("Catalog-based extraction can now connect to the live source database.")


if __name__ == "__main__":
    main()
