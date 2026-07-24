#!/usr/bin/env python3
"""
assess.py — SPG Compatibility Assessment CLI.

Scans ddl_objects.json against Snowflake Postgres compatibility rules
and produces assessment_summary.json + assessment_report.md.
Exits with code 1 if any BLOCK-level findings are detected.

Usage:
  python scripts/assess.py --source-type mssql \\
    --ddl-objects $WORK_DIR/ddl_objects.json \\
    --output $WORK_DIR/assessment/
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from spgloader.connectors import get_connector, parse_ddl_file, DEFAULT_PORTS
from spgloader.reporting.assessment import SPGCompatibilityAssessment, format_report


def main():
    parser = argparse.ArgumentParser(
        description="SPG Compatibility Assessment — scans DDL for Snowflake Postgres compatibility"
    )
    parser.add_argument("--source-type", required=True, choices=["mssql", "mysql", "oracle"])
    parser.add_argument("--ddl-objects", default=None,
                        help="Path to ddl_objects.json (from extract_ddl.py)")
    parser.add_argument("--ddl-file", default=None,
                        help="Alternative: parse a raw .sql DDL file directly")
    parser.add_argument("--output", default="assessment/",
                        help="Output directory for assessment_summary.json and assessment_report.md")
    parser.add_argument("--source-desc", default="",
                        help="Human-readable source description (e.g. 'MSSQL 2022 @ localhost/mydb')")
    args = parser.parse_args()

    # Load objects
    if args.ddl_objects:
        objects = json.loads(Path(args.ddl_objects).read_text())
    elif args.ddl_file:
        objects = parse_ddl_file(args.ddl_file, args.source_type)
    else:
        print("Error: provide --ddl-objects or --ddl-file", file=sys.stderr)
        sys.exit(1)

    # Run assessment
    scanner = SPGCompatibilityAssessment()
    result = scanner.scan(objects, args.source_type)

    # Write outputs
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "assessment_summary.json"
    summary_path.write_text(json.dumps(result.to_dict(), indent=2))

    report_text = format_report(result, args.source_desc)
    report_path = out_dir / "assessment_report.md"
    report_path.write_text(report_text)

    # Generate pre-deploy extensions script if needed
    if result.extension_prereqs:
        ext_lines = [
            "-- spgloader: pre-deploy extensions",
            "-- Run this BEFORE deploying converted objects to SPG",
            "",
        ]
        seen = set()
        for f in result.resolve_findings:
            if f.auto_resolution and f.auto_resolution not in seen:
                ext_lines.append(f.auto_resolution)
                seen.add(f.auto_resolution)
        ext_path = out_dir / "pre_deploy_extensions.sql"
        ext_path.write_text("\n".join(ext_lines))
        print(f"Extension prereqs script: {ext_path}")

    # Print report to stdout
    print(report_text)

    # Summary line
    print(f"\nAssessment summary: {summary_path}")
    print(f"Report:             {report_path}")

    # Exit 1 if blocked
    if result.is_blocked:
        print(f"\nBLOCKED: {len(result.block_findings)} SPG incompatibilities must be resolved before migration.",
              file=sys.stderr)
        sys.exit(1)

    if result.warn_findings:
        print(f"\n{len(result.warn_findings)} warning(s) found — review before proceeding.")

    sys.exit(0)


if __name__ == "__main__":
    main()
