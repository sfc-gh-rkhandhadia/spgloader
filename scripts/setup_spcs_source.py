#!/usr/bin/env python3
"""
setup_spcs_source.py — Deploy a source database (MSSQL) on Snowpark Container Services.

Steps performed:
  1. Create an image repository (spgloader_source_repo)
  2. Pull the source DB image locally, retag for linux/amd64, push to the repo
  3. Create a compute pool (CPU_X64_XS, 1 node)
  4. Write a service spec and PUT it to a stage
  5. CREATE SERVICE and wait for RUNNING
  6. Retrieve the service DNS name
  7. Write source_conn.env with SOURCE_HOST + SPCS_SERVICE / SPCS_POOL keys

Usage:
    uv run python scripts/setup_spcs_source.py \\
        --source-type mssql \\
        --work-dir    $SPGLOADER_WORK_DIR \\
        --password-env MSSQL_SA_PASSWORD \\
        --database     migration_db

    # Custom names
    uv run python scripts/setup_spcs_source.py \\
        --source-type  mssql \\
        --work-dir     $SPGLOADER_WORK_DIR \\
        --password-env MSSQL_SA_PASSWORD \\
        --database     migration_db \\
        --pool-name    my_pool \\
        --service-name my_source_db \\
        --repo-name    my_repo \\
        --instance-family CPU_X64_S
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Source DB image config
# ---------------------------------------------------------------------------

SOURCE_IMAGES = {
    "mssql": {
        "image": "mcr.microsoft.com/mssql/server:2022-latest",
        "port": 1433,
        "env": {
            "ACCEPT_EULA": "Y",
            # password injected at deploy time from the named env var
        },
        "password_env": "MSSQL_SA_PASSWORD",
        "default_user": "sa",
        "default_db": "master",
    },
    "mysql": {
        "image": "mysql:8.0",
        "port": 3306,
        "env": {},
        "password_env": "MYSQL_ROOT_PASSWORD",
        "default_user": "root",
        "default_db": "mysql",
    },
}

# ---------------------------------------------------------------------------
# Helpers — snow CLI wrapper
# ---------------------------------------------------------------------------

def _snow_sql(sql: str, connection: str | None = None, format: str = "json") -> str:
    """Run a SQL statement via `snow sql` and return stdout."""
    cmd = ["snow", "sql", "-q", sql, f"--format={format}"]
    if connection:
        cmd += ["--connection", connection]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"snow sql failed (exit {result.returncode}):\n"
            f"  SQL : {sql[:120]}\n"
            f"  ERR : {(result.stdout + result.stderr)[:400]}"
        )
    return result.stdout.strip()


def _snow_sql_json(sql: str, connection: str | None = None) -> list[dict]:
    raw = _snow_sql(sql, connection=connection, format="json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
        print((result.stdout + result.stderr)[:600], file=sys.stderr)
        sys.exit(1)
    return result


def _check_snow_cli() -> None:
    r = subprocess.run(["snow", "--version"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: 'snow' CLI not found.", file=sys.stderr)
        print("  Install: pip install snowflake-cli-labs", file=sys.stderr)
        sys.exit(1)
    print(f"  snow CLI: OK ({r.stdout.strip()})")


def _check_docker() -> None:
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: Docker daemon is not running or docker is not installed.", file=sys.stderr)
        sys.exit(1)
    print("  docker: OK")


# ---------------------------------------------------------------------------
# Step 1 — Image repository
# ---------------------------------------------------------------------------

def create_image_repo(repo_name: str, connection: str | None) -> str:
    """Create the image repository (idempotent) and return its URL."""
    print(f"\n[1/6] Creating image repository '{repo_name}'...")
    _snow_sql(
        f"CREATE IMAGE REPOSITORY IF NOT EXISTS {repo_name}",
        connection=connection,
    )
    rows = _snow_sql_json(
        f"SHOW IMAGE REPOSITORIES LIKE '{repo_name}'",
        connection=connection,
    )
    if not rows:
        raise RuntimeError(f"Could not find image repository '{repo_name}' after creation.")
    # repository_url column
    repo_url = rows[0].get("repository_url") or rows[0].get("REPOSITORY_URL", "")
    if not repo_url:
        raise RuntimeError(f"Could not read repository_url from: {rows[0]}")
    print(f"  Repository URL: {repo_url}")
    return repo_url


# ---------------------------------------------------------------------------
# Step 2 — Pull, retag, push image
# ---------------------------------------------------------------------------

def push_source_image(
    source_type: str,
    repo_url: str,
    tag: str = "latest",
) -> str:
    """Pull the source DB image and push it to the Snowflake image registry."""
    cfg = SOURCE_IMAGES[source_type]
    src_image = cfg["image"]
    dest_image = f"{repo_url}/{source_type}-source:{tag}"

    print(f"\n[2/6] Pushing source image to registry...")
    print(f"  Source : {src_image}")
    print(f"  Target : {dest_image}")

    # Pull for linux/amd64 (SPCS runs on x86)
    print("  Pulling image (linux/amd64)...")
    _run([
        "docker", "pull", "--platform", "linux/amd64", src_image,
    ])

    # Tag for registry
    _run(["docker", "tag", src_image, dest_image])

    # Authenticate docker to Snowflake registry (snow cli handles token)
    registry_host = repo_url.split("/")[0]
    print(f"  Authenticating to registry {registry_host}...")
    _run(["snow", "spcs", "image-registry", "login"])

    # Push
    print("  Pushing image (this may take a few minutes)...")
    _run(["docker", "push", dest_image])
    print(f"  Pushed: {dest_image}")
    return dest_image


# ---------------------------------------------------------------------------
# Step 3 — Compute pool
# ---------------------------------------------------------------------------

def create_compute_pool(
    pool_name: str,
    instance_family: str,
    connection: str | None,
) -> None:
    print(f"\n[3/6] Creating compute pool '{pool_name}' ({instance_family})...")
    _snow_sql(
        f"""CREATE COMPUTE POOL IF NOT EXISTS {pool_name}
            MIN_NODES = 1
            MAX_NODES = 1
            INSTANCE_FAMILY = {instance_family}
            AUTO_RESUME = TRUE
            AUTO_SUSPEND_SECS = 3600""",
        connection=connection,
    )
    print(f"  Compute pool '{pool_name}' ready.")


# ---------------------------------------------------------------------------
# Step 4 — Service spec + stage
# ---------------------------------------------------------------------------

def _build_spec(
    source_type: str,
    dest_image: str,
    password_env: str,
    service_name: str,
) -> str:
    cfg = SOURCE_IMAGES[source_type]
    port = cfg["port"]
    env_block = "\n".join(
        f"      {k}: \"{v}\"" for k, v in cfg["env"].items()
    )
    # Password injected as a Snowflake secret reference
    secret_env = f"      MSSQL_SA_PASSWORD: \"${{MSSQL_SA_PASSWORD}}\""

    spec = f"""spec:
  containers:
  - name: source-db
    image: {dest_image}
    env:
{env_block}
      {password_env}: "${{{{ {password_env} }}}}"
    resources:
      requests:
        memory: 2G
        cpu: "1"
      limits:
        memory: 4G
  endpoints:
  - name: db
    port: {port}
    protocol: TCP
"""
    return spec


def deploy_service_spec(
    source_type: str,
    dest_image: str,
    password_env: str,
    service_name: str,
    pool_name: str,
    stage_name: str,
    connection: str | None,
) -> None:
    print(f"\n[4/6] Deploying service '{service_name}'...")

    # Create stage for specs
    _snow_sql(
        f"CREATE STAGE IF NOT EXISTS {stage_name} ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')",
        connection=connection,
    )

    spec_yaml = _build_spec(source_type, dest_image, password_env, service_name)
    spec_filename = f"{service_name}_spec.yaml"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix=f"{service_name}_"
    ) as f:
        f.write(spec_yaml)
        spec_local_path = f.name

    try:
        # PUT spec to stage
        _snow_sql(
            f"PUT file://{spec_local_path} @{stage_name}/{spec_filename} "
            f"OVERWRITE = TRUE AUTO_COMPRESS = FALSE",
            connection=connection,
        )

        # CREATE SERVICE
        _snow_sql(
            f"""CREATE SERVICE IF NOT EXISTS {service_name}
                IN COMPUTE POOL {pool_name}
                FROM @{stage_name}
                SPEC = '{spec_filename}'""",
            connection=connection,
        )
        print(f"  Service '{service_name}' created.")
    finally:
        Path(spec_local_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 5 — Wait for RUNNING
# ---------------------------------------------------------------------------

def wait_for_running(
    service_name: str,
    connection: str | None,
    timeout_seconds: int = 600,
    poll_interval: int = 15,
) -> None:
    print(f"\n[5/6] Waiting for service '{service_name}' to reach RUNNING status...")
    deadline = time.time() + timeout_seconds
    last_status = ""
    while time.time() < deadline:
        rows = _snow_sql_json(
            f"SHOW SERVICES LIKE '{service_name.upper()}'",
            connection=connection,
        )
        status = ""
        if rows:
            status = (
                rows[0].get("status")
                or rows[0].get("STATUS")
                or ""
            ).upper()
        if status != last_status:
            print(f"  Status: {status or 'UNKNOWN'}")
            last_status = status
        if status == "RUNNING":
            print("  Service is RUNNING.")
            return
        if status in ("FAILED", "SUSPENDED"):
            raise RuntimeError(
                f"Service '{service_name}' reached terminal status: {status}. "
                "Check Snowflake event table or SHOW SERVICE LOGS."
            )
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Service '{service_name}' did not reach RUNNING within {timeout_seconds}s."
    )


# ---------------------------------------------------------------------------
# Step 6 — Get DNS name
# ---------------------------------------------------------------------------

def get_service_dns(service_name: str, connection: str | None) -> str:
    print(f"\n[6/6] Retrieving service DNS name...")
    rows = _snow_sql_json(
        f"SELECT SYSTEM$GET_SERVICE_DNS_NAME('{service_name}') AS dns",
        connection=connection,
    )
    if not rows:
        raise RuntimeError("SYSTEM$GET_SERVICE_DNS_NAME returned no rows.")
    dns = rows[0].get("DNS") or rows[0].get("dns", "")
    if not dns:
        raise RuntimeError(f"Could not parse DNS from: {rows[0]}")
    print(f"  Service DNS: {dns}")
    return dns


# ---------------------------------------------------------------------------
# source_conn.env writer
# ---------------------------------------------------------------------------

def write_source_conn_env(
    work_dir: Path,
    source_type: str,
    host: str,
    database: str,
    password_env: str,
    service_name: str,
    pool_name: str,
) -> None:
    cfg = SOURCE_IMAGES[source_type]
    env_file = work_dir / "source_conn.env"

    lines = [
        f"SOURCE_TYPE={source_type}",
        f"SOURCE_HOST={host}",
        f"SOURCE_PORT={cfg['port']}",
        f"SOURCE_DATABASE={database}",
        f"SOURCE_USER={cfg['default_user']}",
        f"SOURCE_PASSWORD_ENV={password_env}",
        f"SPCS_SERVICE={service_name}",
        f"SPCS_POOL={pool_name}",
    ]
    env_file.write_text("\n".join(lines) + "\n")
    print(f"\n  Written: {env_file}")
    for line in lines:
        print(f"    {line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy a source database on Snowpark Container Services for spgloader migration"
    )
    parser.add_argument(
        "--source-type", required=True, choices=list(SOURCE_IMAGES),
        help="Source database type (mssql, mysql)",
    )
    parser.add_argument(
        "--work-dir", required=True,
        help="spgloader workspace directory (source_conn.env written here)",
    )
    parser.add_argument(
        "--password-env", required=True,
        help="Name of the env var holding the source DB admin password",
    )
    parser.add_argument(
        "--database", required=True,
        help="Database name that will be created and loaded inside the service",
    )
    parser.add_argument(
        "--pool-name", default="spgloader_source_pool",
        help="Compute pool name (default: spgloader_source_pool)",
    )
    parser.add_argument(
        "--service-name", default="spgloader_source_db",
        help="SPCS service name (default: spgloader_source_db)",
    )
    parser.add_argument(
        "--repo-name", default="spgloader_source_repo",
        help="Image repository name (default: spgloader_source_repo)",
    )
    parser.add_argument(
        "--stage-name", default="spgloader_specs",
        help="Stage for service specs (default: spgloader_specs)",
    )
    parser.add_argument(
        "--instance-family", default="CPU_X64_XS",
        help="Compute pool instance family (default: CPU_X64_XS)",
    )
    parser.add_argument(
        "--connection",
        help="Snowflake connection name (passed to snow --connection)",
    )
    parser.add_argument(
        "--skip-push", action="store_true",
        help="Skip docker pull+push (image already in repo)",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Validate password env var is set
    password = os.environ.get(args.password_env)
    if not password:
        print(f"ERROR: env var '{args.password_env}' is not set.", file=sys.stderr)
        print(f"  Run: export {args.password_env}='your_password'", file=sys.stderr)
        sys.exit(1)

    print("=== SPCS Source Setup ===")
    print(f"  Source type    : {args.source_type}")
    print(f"  Compute pool   : {args.pool_name} ({args.instance_family})")
    print(f"  Service        : {args.service_name}")
    print(f"  Image repo     : {args.repo_name}")
    print(f"  Work dir       : {work_dir}")

    _check_snow_cli()
    if not args.skip_push:
        _check_docker()

    # Step 1
    repo_url = create_image_repo(args.repo_name, args.connection)

    # Step 2
    cfg = SOURCE_IMAGES[args.source_type]
    if args.skip_push:
        dest_image = f"{repo_url}/{args.source_type}-source:latest"
        print(f"\n[2/6] Skipping image push (--skip-push). Assuming: {dest_image}")
    else:
        dest_image = push_source_image(args.source_type, repo_url)

    # Step 3
    create_compute_pool(args.pool_name, args.instance_family, args.connection)

    # Step 4
    deploy_service_spec(
        source_type=args.source_type,
        dest_image=dest_image,
        password_env=args.password_env,
        service_name=args.service_name,
        pool_name=args.pool_name,
        stage_name=args.stage_name,
        connection=args.connection,
    )

    # Step 5
    wait_for_running(args.service_name, args.connection)

    # Step 6
    dns = get_service_dns(args.service_name, args.connection)

    # Write source_conn.env
    write_source_conn_env(
        work_dir=work_dir,
        source_type=args.source_type,
        host=dns,
        database=args.database,
        password_env=args.password_env,
        service_name=args.service_name,
        pool_name=args.pool_name,
    )

    print("\n=== SPCS source setup complete ===")
    print(f"  Source DB endpoint : {dns}:{cfg['port']}")
    print(f"  source_conn.env    : {work_dir}/source_conn.env")
    print("\nNext steps:")
    print(f"  1. Load DDL:  uv run python scripts/load_source_ddl.py \\")
    print(f"       --source-type {args.source_type} --database {args.database} \\")
    print(f"       --ddl-file /path/to/schema.sql \\")
    print(f"       --password-env {args.password_env} \\")
    print(f"       --work-dir {work_dir}")
    print(f"  2. Extract:   uv run python scripts/extract_ddl.py \\")
    print(f"       --source-type {args.source_type} --work-dir {work_dir}")


if __name__ == "__main__":
    main()
