#!/usr/bin/env python3
"""
audit_conversion.py — Conversion completeness and truncation detection.

Scans converted SQL files to detect:
  - Truncated output (incomplete BEGIN/END, missing $$, mid-statement EOF)
  - Empty procedure/function bodies
  - Missing output files (listed in manifest but no .sql exists)
  - EWI-annotated sections with no content after the annotation

Usage:
    python scripts/audit_conversion.py --work-dir /path/to/workspace [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


@staticmethod
def _count_pattern(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.IGNORECASE))


def check_truncation(sql: str, obj_type: str) -> str | None:
    """Return a truncation reason string, or None if the file appears complete."""
    stripped = sql.strip()
    if not stripped:
        return "empty file"

    # Check for PL/pgSQL $$ delimiter balance
    dollar_count = stripped.count("$$")
    if dollar_count % 2 != 0:
        return f"unbalanced $$ delimiters ({dollar_count} found, expected even)"

    # For procedures/functions: check BEGIN/END balance
    if obj_type.upper() in ("PROCEDURE", "FUNCTION", "TRIGGER"):
        # Only check within $$ blocks (PL/pgSQL body)
        body_match = re.search(r'\$\$(.*?)\$\$', stripped, re.DOTALL)
        if body_match:
            body = body_match.group(1)
            begins = len(re.findall(r'\bBEGIN\b', body, re.IGNORECASE))
            ends = len(re.findall(r'\bEND\b', body, re.IGNORECASE))
            if begins > 0 and ends < begins:
                return f"unbalanced BEGIN/END ({begins} BEGIN vs {ends} END)"

    # Check for mid-keyword truncation at EOF
    last_line = stripped.rstrip().split("\n")[-1].strip()
    # Common truncation indicators: line ends with a keyword that expects continuation
    truncation_keywords = ["THEN", "ELSE", "BEGIN", "AS", "SET", "WHERE", "AND", "OR",
                           "FROM", "JOIN", "ON", "INTO", "VALUES", "SELECT", "INSERT",
                           "UPDATE", "DECLARE", "WHEN", "CASE"]
    if last_line.upper().rstrip(";").rstrip() in truncation_keywords:
        return f"file ends with incomplete keyword: '{last_line}'"

    # Check for abrupt string/identifier truncation (line ends with unclosed quote)
    if last_line.count("'") % 2 != 0:
        return f"unclosed string literal on last line"
    if last_line.count('"') % 2 != 0:
        return f"unclosed identifier quote on last line"

    return None


def check_completeness(sql: str, obj_type: str) -> str | None:
    """Return an incompleteness reason, or None if the file looks complete."""
    stripped = sql.strip()

    if obj_type.upper() in ("PROCEDURE", "FUNCTION"):
        # Should have CREATE ... FUNCTION/PROCEDURE and at minimum a body
        if "$$" not in stripped and "LANGUAGE" not in stripped.upper():
            # Might be raw MySQL that wasn't converted
            if "DEFINER" in stripped.upper() or "ALGORITHM" in stripped.upper():
                return "unconverted MySQL syntax (DEFINER/ALGORITHM still present)"
            if "DELIMITER" in stripped.upper():
                return "unconverted MySQL syntax (DELIMITER still present)"

        # Check for empty body
        body_match = re.search(r'\$\$(.*?)\$\$', stripped, re.DOTALL)
        if body_match:
            body = body_match.group(1).strip()
            if not body or body in ("BEGIN\nEND", "BEGIN END", "NULL"):
                return "empty procedure/function body"

    if obj_type.upper() == "VIEW":
        # Views should have CREATE ... VIEW ... AS SELECT
        if "SELECT" not in stripped.upper():
            return "view missing SELECT statement"
        if "DEFINER" in stripped.upper() or "ALGORITHM" in stripped.upper():
            return "unconverted MySQL view syntax"

    return None


def check_ewi_content(sql: str) -> str | None:
    """Check that EWI annotations have actual content after them (not just comments then EOF)."""
    lines = sql.strip().split("\n")
    ewi_lines = [i for i, l in enumerate(lines) if "SPG-EWI-" in l]
    if not ewi_lines:
        return None

    # Check the last EWI annotation — is there meaningful content after it?
    last_ewi = ewi_lines[-1]
    remaining = "\n".join(lines[last_ewi + 1:]).strip()
    # Strip comments
    remaining_code = re.sub(r'--.*$', '', remaining, flags=re.MULTILINE).strip()
    if not remaining_code:
        return "EWI annotation at end of file with no code after it"
    return None


def audit_workspace(work_dir: Path) -> dict:
    """Run the full conversion audit and return results."""
    # Find conversion report
    conv_report_path = work_dir / "conversion" / "_conversion_report.json"
    if not conv_report_path.exists():
        return {"error": "No _conversion_report.json found — run convert_objects.py first"}

    conv_report = json.loads(conv_report_path.read_text())
    converted = conv_report.get("converted_objects", [])

    results = {
        "total_objects": len(converted),
        "complete": 0,
        "truncated": 0,
        "incomplete": 0,
        "missing_output": 0,
        "empty_body": 0,
        "unconverted_syntax": 0,
        "ewi_no_content": 0,
        "details": [],
    }

    for entry in converted:
        fqn = entry.get("fqn", "unknown")
        output_file = entry.get("output_file", "")
        obj_type = entry.get("type", "PROCEDURE")

        # Resolve the output file path
        full_path = work_dir / output_file if output_file else None
        if not full_path or not full_path.exists():
            results["missing_output"] += 1
            results["details"].append({"fqn": fqn, "issue": "missing_output",
                                        "detail": f"Expected: {output_file}"})
            continue

        sql = full_path.read_text(encoding="utf-8", errors="replace")

        # Check truncation
        trunc = check_truncation(sql, obj_type)
        if trunc:
            results["truncated"] += 1
            results["details"].append({"fqn": fqn, "issue": "truncated", "detail": trunc})
            continue

        # Check completeness
        incomp = check_completeness(sql, obj_type)
        if incomp:
            if "empty" in incomp:
                results["empty_body"] += 1
            elif "unconverted" in incomp:
                results["unconverted_syntax"] += 1
            else:
                results["incomplete"] += 1
            results["details"].append({"fqn": fqn, "issue": "incomplete", "detail": incomp})
            continue

        # Check EWI annotations have content
        ewi_issue = check_ewi_content(sql)
        if ewi_issue:
            results["ewi_no_content"] += 1
            results["details"].append({"fqn": fqn, "issue": "ewi_no_content", "detail": ewi_issue})
            continue

        results["complete"] += 1

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit conversion completeness and truncation")
    parser.add_argument("--work-dir", required=True, help="spgloader workspace directory")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser().resolve()
    result = audit_workspace(work_dir)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if "error" in result:
            print(f"ERROR: {result['error']}")
            sys.exit(1)

        print(f"\nConversion Audit")
        print("=" * 50)
        print(f"  Total objects:      {result['total_objects']}")
        print(f"  Complete:           {result['complete']}")
        print(f"  Truncated:          {result['truncated']}")
        print(f"  Incomplete:         {result['incomplete']}")
        print(f"  Missing output:     {result['missing_output']}")
        print(f"  Empty body:         {result['empty_body']}")
        print(f"  Unconverted syntax: {result['unconverted_syntax']}")
        print(f"  EWI w/o content:    {result['ewi_no_content']}")

        if result["details"]:
            print(f"\n  Issues ({len(result['details'])}):")
            for d in result["details"][:20]:
                print(f"    [{d['issue']}] {d['fqn']}: {d['detail']}")
            if len(result["details"]) > 20:
                print(f"    ... and {len(result['details']) - 20} more")

        # Exit 1 if any blocking issues
        blocking = result["truncated"] + result["missing_output"] + result["empty_body"]
        sys.exit(1 if blocking > 0 else 0)


if __name__ == "__main__":
    main()
