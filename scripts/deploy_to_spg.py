#!/usr/bin/env python3
"""CLI wrapper for spgloader.deployment.spg — deploys DDL to Snowflake Postgres."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from spgloader.deployment import spg


def main():
    parser = argparse.ArgumentParser(description="Deploy converted DDL to Snowflake Postgres")
    parser.add_argument("--spg-service", required=True, help="Service name in ~/.pg_service.conf")
    parser.add_argument("--test-connection", action="store_true")
    parser.add_argument("--count-tables", action="store_true")
    parser.add_argument("--dep-graph", default=None)
    parser.add_argument("--converted-dir", default=None)
    parser.add_argument("--conversion-manifest", default=None)
    parser.add_argument("--output", default="deployment_summary.json")
    args = parser.parse_args()

    if args.test_connection:
        spg.test_connection(args.spg_service)
        sys.exit(0)

    if args.count_tables:
        if not args.dep_graph:
            print("Error: --dep-graph required for --count-tables", file=sys.stderr)
            sys.exit(1)
        counts = spg.count_tables(args.dep_graph, args.spg_service)
        Path(args.output).write_text(json.dumps(counts, indent=2))
        print(f"Counted {len(counts)} tables — written to {args.output}")
        sys.exit(0)

    required = [("dep_graph", "--dep-graph"), ("converted_dir", "--converted-dir"),
                ("conversion_manifest", "--conversion-manifest")]
    missing = [flag for attr, flag in required if not getattr(args, attr)]
    if missing:
        print(f"Error: missing required arguments: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    summary = spg.deploy(args.dep_graph, args.converted_dir,
                         args.conversion_manifest, args.spg_service)
    Path(args.output).write_text(json.dumps(summary, indent=2))
    print(f"\nDeployment: {summary['succeeded']} succeeded, "
          f"{summary['failed']} failed, {summary['skipped']} skipped")
    print(f"Summary written to {args.output}")


if __name__ == "__main__":
    main()
