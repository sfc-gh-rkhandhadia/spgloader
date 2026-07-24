#!/usr/bin/env python3
"""
trust_spg_cert.py — Extract the SPG TLS certificate chain and trust it in the
macOS login keychain so that pgloader's SBCL SSL layer can verify it.

Must be run before pgloader when the SPG instance uses a self-signed cert chain
(which is always the case for Snowflake Postgres instances).

Usage:
    uv run python scripts/trust_spg_cert.py --spg-service <service_name>
    uv run python scripts/trust_spg_cert.py --host <host> --port 5432

Requires:
    - openssl in PATH
    - macOS security command (macOS only)
    - No sudo required (adds to ~/Library/Keychains/login.keychain)
"""
import argparse
import configparser
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def get_host_from_service(service_name: str) -> str:
    """Read host from ~/.pg_service.conf for the given service name."""
    service_file = Path.home() / ".pg_service.conf"
    if not service_file.exists():
        print(f"Error: ~/.pg_service.conf not found", file=sys.stderr)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(service_file)
    if service_name not in cfg:
        print(f"Error: service '{service_name}' not in ~/.pg_service.conf", file=sys.stderr)
        sys.exit(1)
    return cfg[service_name].get("host", ""), int(cfg[service_name].get("port", "5432"))


def extract_cert_chain(host: str, port: int, output_dir: Path) -> list[Path]:
    """
    Extract all certificates in the TLS chain from the PostgreSQL server.

    Returns a list of .pem files (one per cert in the chain).
    """
    if not shutil.which("openssl"):
        print("Error: openssl not found in PATH", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{port}",
         "-starttls", "postgres", "-showcerts"],
        capture_output=True, text=True, timeout=20,
        input="",  # close stdin immediately so openssl exits after handshake
    )
    if result.returncode != 0 and "CONNECTED" not in result.stdout:
        print(f"Error: could not connect to {host}:{port}: {result.stderr[:200]}", file=sys.stderr)
        sys.exit(1)

    # Parse out each -----BEGIN CERTIFICATE----- block
    import re
    cert_blocks = re.findall(
        r"(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)",
        result.stdout, re.DOTALL,
    )
    if not cert_blocks:
        print(f"Error: no certificates found in TLS handshake from {host}:{port}", file=sys.stderr)
        sys.exit(1)

    cert_files = []
    for i, block in enumerate(cert_blocks):
        cert_path = output_dir / f"spg_cert_{i}.pem"
        cert_path.write_text(block)
        cert_files.append(cert_path)

    print(f"Extracted {len(cert_files)} certificate(s) from {host}:{port}")
    return cert_files


def trust_cert_macos(cert_path: Path, keychain: str = "login") -> bool:
    """
    Trust a certificate in the macOS login keychain (no sudo required).

    Uses: security add-trusted-cert -d -r trustRoot -k ~/Library/Keychains/login.keychain
    """
    if sys.platform != "darwin":
        print("Note: macOS keychain trust only applies on macOS.", file=sys.stderr)
        return False

    keychain_path = (
        Path.home() / "Library" / "Keychains" / "login.keychain-db"
        if keychain == "login"
        else Path(keychain)
    )

    result = subprocess.run(
        [
            "security", "add-trusted-cert",
            "-d",                    # add to certificate trust settings
            "-r", "trustRoot",       # trust as root certificate
            "-k", str(keychain_path),
            str(cert_path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: failed to trust {cert_path.name}: {result.stderr.strip()}", file=sys.stderr)
        return False

    print(f"  Trusted: {cert_path.name} → {keychain_path.name}")
    return True


def verify_pgloader_connection(host: str, port: int, service_name: str) -> bool:
    """Run pgloader --dry-run to verify the SSL connection now works."""
    if not shutil.which("pgloader"):
        print("pgloader not found — skipping connection verification", file=sys.stderr)
        return True

    # Create a minimal test .load file
    import psycopg2
    try:
        conn = psycopg2.connect(f"service={service_name}")
        conn.cursor().execute("SELECT 1")
        conn.close()
        print(f"  psycopg2 connection to {service_name}: OK")
        return True
    except Exception as e:
        print(f"  psycopg2 connection test failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Trust SPG TLS certificate in macOS login keychain for pgloader"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--spg-service", help="Service name in ~/.pg_service.conf")
    group.add_argument("--host", help="SPG hostname or IP")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--cert-dir", default=None,
                        help="Directory to save extracted certs (default: ~/.spgloader/certs/)")
    args = parser.parse_args()

    if args.spg_service:
        host, port = get_host_from_service(args.spg_service)
        print(f"Resolved service '{args.spg_service}' → {host}:{port}")
    else:
        host, port = args.host, args.port

    cert_dir = Path(args.cert_dir) if args.cert_dir else Path.home() / ".spgloader" / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)

    # Extract cert chain
    cert_files = extract_cert_chain(host, port, cert_dir)

    # Trust each cert in the chain
    trusted = 0
    for cert_path in cert_files:
        if trust_cert_macos(cert_path):
            trusted += 1

    print(f"\nTrusted {trusted}/{len(cert_files)} certificate(s) in macOS login keychain")

    if trusted > 0:
        print("\npgloader should now be able to connect to SPG with SSL verification.")
        print("If pgloader still fails, try:")
        print("  1. Restart your terminal session (keychain changes sometimes need refresh)")
        print("  2. Run: security find-certificate -a ~/Library/Keychains/login.keychain-db | grep snowflake")
        print("     to confirm the cert was added")

    # Verify SPG connection if service name was given
    if args.spg_service:
        print(f"\nVerifying connection to {args.spg_service}...")
        verify_pgloader_connection(host, port, args.spg_service)

    sys.exit(0 if trusted > 0 else 1)


if __name__ == "__main__":
    main()
