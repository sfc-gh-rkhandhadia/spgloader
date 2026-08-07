#!/usr/bin/env python3
"""
extract_ddl.py — CLI wrapper for spgloader.connectors.

Extracts schema DDL from MSSQL, MySQL, or Oracle and writes ddl_objects.json.
All extraction logic lives in lib/spgloader/connectors/.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from spgloader.connectors import get_connector, parse_ddl_file, DEFAULT_PORTS


def main():
    parser = argparse.ArgumentParser(
        description="Extract DDL from MSSQL, MySQL, or Oracle → ddl_objects.json"
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql", "oracle"])
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--database", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--password-env", default=None,
                        help="Name of env var holding the password")
    parser.add_argument("--ddl-file", default=None,
                        help="Parse a .sql file instead of connecting to a live DB")
    parser.add_argument("--output", default="ddl_objects.json")
    parser.add_argument("--test-connection", action="store_true")
    args = parser.parse_args()

    port = args.port or DEFAULT_PORTS[args.source_type]
    password = ""
    if args.password_env:
        password = os.environ.get(args.password_env, "")
        if not password and not args.ddl_file and not args.test_connection:
            print(f"Error: env var {args.password_env!r} is not set", file=sys.stderr)
            sys.exit(1)

    if args.test_connection:
        connector = get_connector(args.source_type, args.host, port,
                                  args.database, args.user, password)
        ok = connector.test_connection()
        if ok:
            print(f"Connection OK: {args.source_type} @ {args.host}:{port}/{args.database}")
            sys.exit(0)
        sys.exit(1)

    if args.ddl_file:
        objects = parse_ddl_file(args.ddl_file, args.source_type)
    else:
        connector = get_connector(args.source_type, args.host, port,
                                  args.database, args.user, password)
        objects = connector.extract()

        # Write bit_columns.json alongside ddl_objects.json for fix_views.py
        bit_cols = connector.extract_bit_columns()
        if bit_cols:
            bit_path = Path(args.output).parent / "bit_columns.json"
            bit_path.write_text(json.dumps(bit_cols, indent=2))
            total_bit = sum(len(v) for v in bit_cols.values())
            print(f"  bit_columns      {total_bit} BIT columns in {len(bit_cols)} tables "
                  f"→ {bit_path.name}")

    Path(args.output).write_text(json.dumps(objects, indent=2))
    counts = Counter(o["type"] for o in objects)
    print(f"Extracted {len(objects)} objects to {args.output}")
    for obj_type, count in sorted(counts.items()):
        print(f"  {obj_type:<20} {count}")

    # Seed the object manifest (if workspace exists)
    output_dir = Path(args.output).parent
    meta_dir = output_dir / ".spgloader"
    if meta_dir.exists() or (output_dir / "source_conn.env").exists():
        try:
            from spgloader.manifest import ObjectManifest
            manifest = ObjectManifest(output_dir)
            added = manifest.seed_from_ddl_objects(objects)
            manifest.save()
            if added:
                print(f"  manifest         {added} objects seeded → .spgloader/object_manifest.json")
        except Exception as e:
            print(f"  manifest         (skipped: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
