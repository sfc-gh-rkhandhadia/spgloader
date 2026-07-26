#!/usr/bin/env python3
"""
run_pgloader_docker.py — Run pgloader via Docker instead of the host binary.

This script replaces direct `pgloader` binary calls.  It:
  1. Reads source_conn.env to get source connection details.
  2. Generates the migration.load config via gen_pgloader_config, patching the
     source host to the Docker container hostname (so pgloader can resolve it on
     the Docker network).
  3. Detects the source container's Docker network.
  4. Builds the spgloader-pgloader Docker image if not already built.
  5. Runs pgloader via docker compose, tee-ing output to pgloader.log.
  6. Parses and prints the pgloader summary table.

Usage:
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/run_pgloader_docker.py \\
        --work-dir     $SPGLOADER_WORK_DIR \\
        --spg-service  pg_spgloader_migration \\
        --source-type  mssql \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

COMPOSE_FILE = SKILL_DIR / "references" / "docker-templates" / "pgloader-compose.yml"

# Container hostname to use inside the Docker network (pgloader resolves this
# via Docker DNS when both containers are on the same network).
CONTAINER_HOSTS = {
    "mssql":  "spgloader_mssql",
    "mysql":  "spgloader_mysql",
    "oracle": None,  # pgloader does not support Oracle sources
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: list[str], check: bool = True, capture: bool = True,
            env: dict | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(cmd, capture_output=capture, text=True,
                          env=merged_env, check=check)


def load_source_conn(work_dir: Path) -> dict[str, str]:
    env_file = work_dir / "source_conn.env"
    if not env_file.exists():
        print(f"Error: {env_file} not found. Run Phase 1 (source-setup) first.",
              file=sys.stderr)
        sys.exit(1)
    result = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def detect_source_network(container_name: str) -> str:
    """Return the Docker network name that the source container is attached to."""
    r = run_cmd(
        ["docker", "inspect", "--format",
         "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}",
         container_name],
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"Warning: could not detect network for '{container_name}'. "
              f"Defaulting to 'docker-templates_default'.", file=sys.stderr)
        return "docker-templates_default"
    # Take the first network if multiple exist
    return r.stdout.strip().split()[0]


def get_container_ip(container_name: str, network_name: str) -> str:
    """Return the container's IP address on the given network.

    pgloader's LISP URI parser does not accept underscores in hostnames (RFC 3986
    technically forbids them).  Using the IP address avoids the parse failure that
    would otherwise occur with container names like 'spgloader_mssql'.
    """
    # Use jq-style JSON extraction via docker inspect --format json to avoid
    # Go template syntax issues with hyphens in network names.
    r = run_cmd(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_name],
        check=False,
    )
    if r.returncode == 0 and r.stdout.strip():
        import json as _json
        try:
            networks = _json.loads(r.stdout.strip())
            # Try the specific network first, then any network
            if network_name in networks:
                ip = networks[network_name].get("IPAddress", "")
                if ip:
                    return ip
            # Fallback: first network with a non-empty IP
            for net_data in networks.values():
                ip = net_data.get("IPAddress", "")
                if ip:
                    return ip
        except Exception:
            pass

    print(f"Warning: could not get IP for '{container_name}'; will use hostname.", file=sys.stderr)
    return container_name


def build_image_if_needed() -> None:
    """Build spgloader-pgloader:local if the image does not already exist."""
    r = run_cmd(
        ["docker", "image", "inspect", "spgloader-pgloader:local"],
        check=False,
    )
    if r.returncode == 0:
        print("  pgloader Docker image already built (spgloader-pgloader:local)")
        return

    print("  Building pgloader Docker image (first run — takes ~30s)...")
    dockerfile = SKILL_DIR / "references" / "docker-templates" / "pgloader.Dockerfile"
    context_dir = SKILL_DIR / "references" / "docker-templates"
    result = run_cmd(
        [
            "docker", "build",
            "--platform", "linux/amd64",
            "-t", "spgloader-pgloader:local",
            "-f", str(dockerfile),
            str(context_dir),
        ],
        capture=False,  # stream build output to terminal
    )
    if result.returncode != 0:
        print("Error: failed to build pgloader Docker image.", file=sys.stderr)
        sys.exit(1)
    print("  Image built: spgloader-pgloader:local")


def generate_load_config(
    work_dir: Path,
    source_type: str,
    source_host: str,  # container hostname, not localhost
    source_port: int,
    source_db: str,
    source_user: str,
    password_env: str,
    spg_service: str,
) -> Path:
    """Call gen_pgloader_config.py, substitute the password, return path to migration.load."""
    load_file = work_dir / "migration.load"
    cmd = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "gen_pgloader_config.py"),
        "--source-type", source_type,
        "--source-host", source_host,
        "--source-port", str(source_port),
        "--source-db", source_db,
        "--source-user", source_user,
        "--source-password-env", password_env,
        "--target-service", spg_service,
        "--output", str(load_file),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)
    if r.returncode != 0:
        print("Error generating pgloader config:", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)

    # pgloader does not support shell variable substitution in .load files.
    # Substitute ${SOURCE_PASSWORD} with the actual password in the generated file.
    password = os.environ.get(password_env, "")
    if not password:
        print(f"Error: env var '{password_env}' is not set.", file=sys.stderr)
        sys.exit(1)

    content = load_file.read_text()
    if "${SOURCE_PASSWORD}" in content:
        # Use the raw password — do NOT URL-encode special characters like '!'.
        # pgloader passes the DSN password directly to dbsetlpwd (via CFFI/libsybdb)
        # without URL-decoding it, so %21 would be sent literally instead of '!'.
        content = content.replace("${SOURCE_PASSWORD}", password)
        load_file.write_text(content)

    print(f"  Config written: {load_file}")
    return load_file


def parse_pgloader_summary(log_text: str) -> None:
    """Extract and print the pgloader summary table from log output."""
    lines = log_text.splitlines()
    # Find the summary separator lines (all dashes)
    sep_indices = [i for i, l in enumerate(lines) if re.match(r"^-{5,}", l.strip())]

    if len(sep_indices) >= 2:
        # Print from the header row before first separator to second separator
        start = max(0, sep_indices[0] - 1)
        end = sep_indices[-1] + 1
        print("\npgloader Summary:")
        print("-" * 60)
        for line in lines[start:end]:
            print(line)
        print("-" * 60)
    else:
        # Fallback: print last 30 lines
        print("\npgloader Output (last 30 lines):")
        for line in lines[-30:]:
            print(line)

    # Highlight any fatal errors
    fatal_lines = [l for l in lines if "FATAL" in l or "ERROR" in l.upper()]
    if fatal_lines:
        print("\nErrors / Warnings:")
        for line in fatal_lines[:10]:
            print(f"  {line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run pgloader via Docker for MSSQL/MySQL → SPG migration"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--spg-service", required=True,
                        help="Service name in ~/.pg_service.conf")
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql"],
                        help="Source database type")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate connections only, do not migrate data")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()

    # ------------------------------------------------------------------
    # 1. Load source connection details
    # ------------------------------------------------------------------
    print("\n[1/5] Reading source connection...")
    conn = load_source_conn(work_dir)
    source_type = conn.get("SOURCE_TYPE", args.source_type)
    source_db = conn.get("SOURCE_DATABASE", "master")
    source_user = conn.get("SOURCE_USER", "sa")
    source_port = int(conn.get("SOURCE_PORT", "1433" if source_type == "mssql" else "3306"))
    password_env = conn.get("SOURCE_PASSWORD_ENV", "MSSQL_SA_PASSWORD")

    # Use the Docker container IP address (not hostname) so pgloader can connect.
    # pgloader's LISP URI parser rejects underscores in hostnames (RFC 3986 compliant),
    # so container names like 'spgloader_mssql' cause a parse failure.
    container_name = CONTAINER_HOSTS.get(source_type)
    if not container_name:
        print(f"Error: pgloader does not support source type '{source_type}'.",
              file=sys.stderr)
        sys.exit(1)

    # Detect network first so we can get the right IP
    print("\n[2/5] Detecting source container network...")
    source_network = detect_source_network(container_name)
    print(f"  Network: {source_network}")

    source_host = get_container_ip(container_name, source_network)
    print(f"  Source: {source_type} @ {source_host}:{source_port}/{source_db} (container: {container_name})")
    print(f"  Target: SPG service '{args.spg_service}'")

    # ------------------------------------------------------------------
    # 3. Generate migration.load (container IP as source host)
    # ------------------------------------------------------------------
    print("\n[3/5] Generating pgloader config...")
    load_file = generate_load_config(
        work_dir, source_type, source_host, source_port,
        source_db, source_user, password_env, args.spg_service,
    )

    # ------------------------------------------------------------------
    # 4. Build Docker image if needed
    # ------------------------------------------------------------------
    print("\n[4/5] Checking pgloader Docker image...")
    build_image_if_needed()

    # ------------------------------------------------------------------
    # 5. Run pgloader via Docker
    # ------------------------------------------------------------------
    pgpass_file = Path.home() / ".pgpass"
    spg_certs_dir = Path.home() / ".spgloader" / "certs"

    extra_args = ["--dry-run"] if args.dry_run else []
    mode_label = "(dry run)" if args.dry_run else ""

    print(f"\n[5/5] Running pgloader via Docker {mode_label}...")
    print(f"  LOAD_FILE:      {load_file}")
    print(f"  SOURCE_NETWORK: {source_network}")

    # Prepare SPG cert files for the container trust store.
    # The wrapper entrypoint in the Docker image installs any *.crt files
    # mounted at /spg-certs/ before running pgloader, solving the self-signed
    # cert chain issue without relying on --no-ssl-cert-verification (which
    # has a known bug in pgloader 3.6.7 for CL-POSTGRES target connections).
    crt_files = list(spg_certs_dir.glob("*.pem")) if spg_certs_dir.exists() else []
    if crt_files:
        crt_dir = str(spg_certs_dir)
        # Rename .pem → .crt so update-ca-certificates picks them up
        for pem in crt_files:
            crt = spg_certs_dir / (pem.stem + ".crt")
            if not crt.exists():
                import shutil
                shutil.copy(pem, crt)
        print(f"  SPG certs:      {len(crt_files)} cert(s) from {spg_certs_dir}")
    else:
        crt_dir = None
        print("  SPG certs:      none found — extracting from SPG now...")
        # Extract certs on-demand using the trust_spg_cert logic
        extract_result = subprocess.run(
            [sys.executable,
             str(SKILL_DIR / "scripts" / "trust_spg_cert.py"),
             "--spg-service", args.spg_service,
             "--cert-dir", str(spg_certs_dir)],
            capture_output=True, text=True,
        )
        if extract_result.returncode == 0:
            crt_files = list(spg_certs_dir.glob("*.pem"))
            for pem in crt_files:
                crt = spg_certs_dir / (pem.stem + ".crt")
                if not crt.exists():
                    import shutil
                    shutil.copy(pem, crt)
            crt_dir = str(spg_certs_dir) if crt_files else None
            if crt_dir:
                print(f"  SPG certs:      {len(crt_files)} cert(s) extracted")

    log_file = work_dir / "pgloader.log"

    # Use `docker run` directly (not compose) to avoid compose env-var
    # interpolation issues and to have full control over all flags.
    docker_cmd = [
        "docker", "run", "--rm",
        "--platform", "linux/amd64",
        "--network", source_network,
        "-v", f"{load_file}:/migration.load:ro",
    ]
    if pgpass_file.exists():
        docker_cmd += ["-v", f"{pgpass_file}:/root/.pgpass:ro"]
    if crt_dir:
        docker_cmd += ["-v", f"{crt_dir}:/spg-certs:ro"]

    docker_cmd += [
        "spgloader-pgloader:local",
        "--no-ssl-cert-verification",   # belt-and-suspenders: also set SBCL flag
        *extra_args,
        "/migration.load",
    ]

    result = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
    )

    combined_output = result.stdout + result.stderr

    # Write full log
    log_file.write_text(combined_output)
    print(f"  Log written: {log_file}")

    # Parse and display summary
    parse_pgloader_summary(combined_output)

    if result.returncode != 0:
        print(f"\npgloader exited with code {result.returncode}.", file=sys.stderr)
        if args.dry_run:
            print("Dry run failed — check source and target connectivity.", file=sys.stderr)
        sys.exit(result.returncode)

    if args.dry_run:
        print("\nDry run passed — both connections are valid.")
        print("Run without --dry-run to start the migration.")
    else:
        print("\npgloader migration complete.")


if __name__ == "__main__":
    main()
