"""
aggregate_feedback.py — Read all feedback JSONL files and produce a ranked pattern report.

Reads *.jsonl from the feedback directory, deduplicates by object+error,
groups by error_class, ranks by frequency, and outputs aggregated_feedback.json.

Usage:
    python scripts/aggregate_feedback.py <feedback_dir> [--output <path>] [--min-count 2]

The aggregated output is what the auto-fix sub-skill reads to apply fixes.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Fix-type routing: maps error_class to the type of fix and target file
# ---------------------------------------------------------------------------

FIX_ROUTING: dict[str, dict] = {
    "column_does_not_exist": {
        "fix_type": "rule",
        "target_file": "references/rules/{source}-to-pg/plpgsql-fixes.yaml",
        "fix_hint": "Add case-preserving identifier quoting or column alias rule",
    },
    "relation_does_not_exist": {
        "fix_type": "rule",
        "target_file": "references/fix-mappings/view-fixes.yaml",
        "fix_hint": "Check schema prefix or deploy ordering (tables before views)",
    },
    "function_does_not_exist": {
        "fix_type": "function-sub",
        "target_file": "references/rules/{source}-to-pg/function-substitutions.yaml",
        "fix_hint": "Add function name mapping or deploy functions before procedures",
    },
    "type_does_not_exist": {
        "fix_type": "type-mapping",
        "target_file": "references/rules/{source}-to-pg/type-mappings.yaml",
        "fix_hint": "Add type mapping for custom/UDT type or mark as out-of-scope",
    },
    "rowcount_conversion_bug": {
        "fix_type": "script",
        "target_file": "scripts/convert_objects.py",
        "fix_hint": "Add @@ROWCOUNT → GET DIAGNOSTICS var = ROW_COUNT; before generic @var stripping in convert_procedure and convert_trigger",
    },
    "code_fence_leak": {
        "fix_type": "script",
        "target_file": "scripts/repair_procedures.py",
        "fix_hint": "Strip trailing ``` fences from bare-path extraction in _extract_plpgsql_from_response",
    },
    "non_setof_return_query": {
        "fix_type": "prompt",
        "target_file": "references/prompts/procedure-repair-prompt.md",
        "fix_hint": "LLM must change RETURNS void → RETURNS SETOF record when introducing RETURN QUERY; add explicit rule to prompt",
    },
    "syntax_error": {
        "fix_type": "rule",
        "target_file": "references/rules/{source}-to-pg/plpgsql-fixes.yaml",
        "fix_hint": "Add syntax transformation rule for the failing construct",
    },
    "cursor_loop": {
        "fix_type": "prompt",
        "target_file": "references/prompts/procedure-repair-{source}-prompt.md",
        "fix_hint": "Add CURSOR loop conversion example to the LLM repair prompt",
    },
    "undeclared_variable": {
        "fix_type": "rule",
        "target_file": "references/rules/{source}-to-pg/plpgsql-fixes.yaml",
        "fix_hint": "Add DECLARE block generation or variable scoping rule",
    },
    "operator_type_mismatch": {
        "fix_type": "rule",
        "target_file": "references/rules/{source}-to-pg/plpgsql-fixes.yaml",
        "fix_hint": "Add explicit CAST or operator substitution rule",
    },
    "cast_error": {
        "fix_type": "type-mapping",
        "target_file": "references/rules/{source}-to-pg/type-mappings.yaml",
        "fix_hint": "Add explicit type cast or conversion function mapping",
    },
    "row_count_mismatch": {
        "fix_type": "script",
        "target_file": "scripts/copy_source_data.py",
        "fix_hint": "Check data copy logic for filtering, FK constraints, or NULL handling",
    },
    "missing_in_spg": {
        "fix_type": "script",
        "target_file": "scripts/parallel_deploy.py",
        "fix_hint": "Check deploy ordering or wave filtering that may skip objects",
    },
}

DEFAULT_ROUTING = {
    "fix_type": "prompt",
    "target_file": "references/prompts/procedure-repair-prompt.md",
    "fix_hint": "Add example covering this error pattern to the LLM repair prompt",
}


# ---------------------------------------------------------------------------
# Aggregation logic
# ---------------------------------------------------------------------------

def load_all_jsonl(feedback_dir: Path) -> list[dict]:
    """Load all entries from *.jsonl files in feedback_dir."""
    entries = []
    for jsonl_file in sorted(feedback_dir.glob("*.jsonl")):
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception as e:
            print(f"Warning: Could not read {jsonl_file}: {e}")
    return entries


def deduplicate(entries: list[dict]) -> list[dict]:
    """Remove duplicate entries (same object + same error class)."""
    seen = set()
    deduped = []
    for entry in entries:
        key = (entry.get("object_name", ""), entry.get("error_class", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def aggregate(entries: list[dict], min_count: int = 1) -> list[dict]:
    """Group entries by error_class and produce ranked aggregated patterns."""
    groups: defaultdict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        groups[entry.get("error_class", "unknown")].append(entry)

    patterns = []
    for error_class, group_entries in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(group_entries) < min_count:
            continue

        sources = sorted(set(e.get("source_type", "?") for e in group_entries))
        affected = [e.get("object_name", "?") for e in group_entries]
        representative = group_entries[0]

        # Resolve target file with source type
        routing = FIX_ROUTING.get(error_class, DEFAULT_ROUTING).copy()
        primary_source = sources[0] if sources else "mssql"
        routing["target_file"] = routing["target_file"].format(source=primary_source)

        patterns.append({
            "rank": 0,  # filled below
            "pattern": error_class,
            "count": len(group_entries),
            "sources": sources,
            "affected_objects": affected[:20],  # cap at 20 for readability
            "representative_error": representative.get("error", ""),
            "representative_source_sql": representative.get("source_sql_snippet", ""),
            "representative_converted_sql": representative.get("converted_sql_snippet", ""),
            "fix_type": routing["fix_type"],
            "target_file": routing["target_file"],
            "fix_hint": routing["fix_hint"],
            "scenarios": sorted(set(e.get("scenario", "?") for e in group_entries)),
            "testers": sorted(set(e.get("tester", "?") for e in group_entries)),
        })

    # Assign ranks
    for i, p in enumerate(patterns, 1):
        p["rank"] = i

    return patterns


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_summary(patterns: list[dict], total_entries: int) -> None:
    """Print a human-readable summary to stdout."""
    print()
    print("=" * 72)
    print("AGGREGATED FEEDBACK SUMMARY")
    print("=" * 72)
    print(f"\nTotal failure entries: {total_entries}")
    print(f"Unique patterns: {len(patterns)}")
    print()

    if not patterns:
        print("No failure patterns found.")
        return

    # Header
    print(f"{'Rank':<5} {'Pattern':<35} {'Count':<6} {'Sources':<15} {'Fix Type':<12}")
    print("-" * 72)
    for p in patterns[:20]:  # show top 20
        sources_str = ",".join(p["sources"])[:14]
        print(f"{p['rank']:<5} {p['pattern']:<35} {p['count']:<6} {sources_str:<15} {p['fix_type']:<12}")

    if len(patterns) > 20:
        print(f"  ... and {len(patterns) - 20} more patterns")

    print()
    print("To apply fixes: tell CoCo 'Fix the top N patterns'")
    print("=" * 72)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Aggregate feedback JSONL files into a ranked pattern report."
    )
    parser.add_argument("feedback_dir", help="Directory containing *.jsonl feedback files")
    parser.add_argument("--output", "-o", help="Output JSON path (default: <feedback_dir>/../aggregated/aggregated_<date>.json)")
    parser.add_argument("--min-count", type=int, default=1, help="Minimum occurrences to include a pattern (default: 1)")
    args = parser.parse_args()

    feedback_dir = Path(args.feedback_dir).expanduser()
    if not feedback_dir.exists():
        print(f"ERROR: feedback directory does not exist: {feedback_dir}")
        raise SystemExit(1)

    # Load and process
    entries = load_all_jsonl(feedback_dir)
    if not entries:
        print(f"No JSONL entries found in: {feedback_dir}")
        return

    deduped = deduplicate(entries)
    patterns = aggregate(deduped, min_count=args.min_count)

    # Output path
    if args.output:
        output_path = Path(args.output)
    else:
        from datetime import datetime
        date_str = datetime.now().strftime("%Y%m%d")
        output_dir = feedback_dir.parent / "aggregated"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"aggregated_{date_str}.json"

    # Write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(patterns, indent=2, ensure_ascii=False))
    print(f"Aggregated {len(entries)} entries ({len(deduped)} unique) → {len(patterns)} patterns")
    print(f"Written to: {output_path}")

    # Print summary
    print_summary(patterns, len(entries))


if __name__ == "__main__":
    main()
