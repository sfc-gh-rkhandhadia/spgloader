#!/usr/bin/env python3
"""CLI wrapper for spgloader.conversion.pgloader_config — generates .load files."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from spgloader.conversion.pgloader_config import generate_config
from spgloader.connectors import DEFAULT_PORTS


def main():
    parser = argparse.ArgumentParser(
        description="Generate a pgloader .load config file for MSSQL or MySQL → SPG"
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql"])
    parser.add_argument("--source-host", default="localhost")
    parser.add_argument("--source-port", type=int, default=None)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--source-password-env", required=True,
                        help="Name of env var holding source DB password")
    parser.add_argument("--target-service", required=True,
                        help="Service name in ~/.pg_service.conf")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    port = args.source_port or DEFAULT_PORTS[args.source_type]

    source_password = os.environ.get(args.source_password_env)
    if not source_password:
        print(f"Error: env var {args.source_password_env!r} is not set.", file=sys.stderr)
        sys.exit(1)
    os.environ["SOURCE_PASSWORD"] = source_password

    content = generate_config(
        args.source_type, args.source_host, port,
        args.source_db, args.source_user, args.target_service,
    )
    Path(args.output).write_text(content)
    print(f"pgloader config written to {args.output}")
    print(f"  Source: {args.source_type} @ {args.source_host}:{port}/{args.source_db}")
    print(f"  Target: SPG service '{args.target_service}'")
    print(f"  Run with: pgloader {args.output}")


if __name__ == "__main__":
    main()
