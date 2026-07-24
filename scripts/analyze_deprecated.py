#!/usr/bin/env python3
"""
analyze_deprecated.py — Phase 3.6: Deprecated Object Review.

Scans extracted DDL objects against references/rules/deprecated-patterns.yaml,
groups matching objects by deprecated technology, prompts the user for a
disposition (skip | migrate | modernize) per group, and writes the decision
to {work_dir}/deprecated/deprecated_review.json.

Phase 4 (convert_objects.py) reads deprecated_review.json and excludes
objects whose FQN is in skip_objects.

Usage:
  python analyze_deprecated.py --work-dir ~/.spgloader/20260101_120000 \\
                               [--catalog  <path-to-deprecated-patterns.yaml>]
                               [--non-interactive]   # auto-apply recommended option
"""
import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _match_pattern(obj: dict, pattern: dict) -> bool:
    """Return True if the DDL object matches any detect rule in the pattern."""
    detect = pattern.get("detect", {})
    obj_name   = obj.get("name", "").lower()
    obj_schema = obj.get("schema", "").lower()
    obj_type   = obj.get("type", "").lower()
    obj_ddl    = obj.get("ddl", "")

    # Check object type allow-list (if specified)
    allowed_types = [t.lower() for t in pattern.get("detect", {}).get("object_types", [])]
    if allowed_types and obj_type not in allowed_types:
        # Still check other detect rules even if type doesn't match (ddl_patterns etc.)
        # But skip if ONLY object_types is set
        has_other_rules = bool(
            detect.get("object_name_patterns") or
            detect.get("schema_patterns") or
            detect.get("ddl_patterns")
        )
        if not has_other_rules:
            return False

    # Schema name patterns
    for sp in detect.get("schema_patterns", []):
        if re.search(sp, obj_schema, re.IGNORECASE):
            return True

    # Object name patterns
    for np in detect.get("object_name_patterns", []):
        if re.search(np, obj_name, re.IGNORECASE):
            return True

    # Object type exact match
    for ot in detect.get("object_types", []):
        if obj_type == ot.lower():
            return True

    # DDL content patterns
    for dp in detect.get("ddl_patterns", []):
        if re.search(dp, obj_ddl, re.IGNORECASE):
            return True

    return False


def scan_objects(ddl_objects: list[dict], patterns: list[dict]) -> dict[str, list[dict]]:
    """Return {pattern_id: [matching_ddl_objects]} for all patterns with matches."""
    results: dict[str, list[dict]] = {}
    for obj in ddl_objects:
        for pattern in patterns:
            if _match_pattern(obj, pattern):
                pid = pattern["id"]
                results.setdefault(pid, [])
                # Avoid duplicates (object might match multiple detect rules)
                if obj not in results[pid]:
                    results[pid].append(obj)
    return results


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------

def _prompt_disposition(pattern: dict, matches: list[dict], non_interactive: bool) -> str:
    """Ask the user which disposition to apply to this group of objects.

    In non-interactive mode, auto-applies the first option (usually 'skip').
    Returns the chosen option id.
    """
    options = pattern.get("options", [])
    if not options:
        return "skip"

    if non_interactive:
        chosen = options[0]["id"]
        print(f"  [auto] {options[0]['label']}")
        return chosen

    # Rich interactive prompt (CoCo ask_user_question is not available from scripts;
    # fall back to a numbered CLI menu)
    print()
    print(f"  Options:")
    for i, opt in enumerate(options, 1):
        print(f"    {i}. {opt['label']}")
        print(f"       {opt['detail']}")

    while True:
        try:
            raw = input(f"\n  Choose [1-{len(options)}]: ").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]["id"]
        except (ValueError, EOFError):
            pass
        print(f"  Please enter a number between 1 and {len(options)}.")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _render_report(
    patterns: list[dict],
    matches_by_id: dict[str, list[dict]],
    review: dict,
) -> str:
    """Generate a human-readable markdown report."""
    lines = [
        "# Phase 3.6 — Deprecated Object Review Report",
        "",
    ]

    if not matches_by_id:
        lines.append("No deprecated patterns detected. All objects will proceed to conversion.")
        return "\n".join(lines)

    lines += [
        f"Detected **{len(matches_by_id)} deprecated technology group(s)**.",
        "",
    ]

    pattern_map = {p["id"]: p for p in patterns}
    for pid, objs in matches_by_id.items():
        p = pattern_map[pid]
        disposition = review["groups"][pid]["disposition"]
        severity_tag = {"advisory": "ℹ", "warning": "⚠", "critical": "🔴"}.get(p["severity"], "•")
        lines += [
            f"## {severity_tag} {p['name']}",
            "",
            f"**Severity:** {p['severity']}  |  **Disposition:** `{disposition}`  |  **Objects detected:** {len(objs)}",
            "",
            f"**Description:** {p['description'].strip()}",
            "",
            f"**Recommendation:** {p['recommendation'].strip()}",
            "",
            "**Matched objects:**",
        ]
        for obj in objs[:20]:  # cap at 20 for readability
            lines.append(f"  - `{obj.get('fqn', obj.get('name', '?'))}` ({obj.get('type','?')})")
        if len(objs) > 20:
            lines.append(f"  - *(and {len(objs)-20} more)*")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 3.6: scan DDL objects for deprecated patterns and record dispositions"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--catalog",
                        default=str(SKILL_DIR / "references" / "rules" / "deprecated-patterns.yaml"),
                        help="Path to deprecated-patterns.yaml")
    parser.add_argument("--ddl-objects", default=None,
                        help="Path to ddl_objects.json (default: <work-dir>/ddl_objects.json)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Auto-apply the first (recommended) option for every group")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    catalog_path = Path(args.catalog).expanduser()
    ddl_path = Path(args.ddl_objects) if args.ddl_objects else work_dir / "ddl_objects.json"

    # ── Load inputs ────────────────────────────────────────────────────────
    import yaml
    if not catalog_path.exists():
        print(f"ERROR: catalog not found: {catalog_path}", file=sys.stderr)
        sys.exit(1)
    catalog = yaml.safe_load(catalog_path.read_text())
    patterns = catalog.get("patterns", [])

    if not ddl_path.exists():
        print(f"ERROR: ddl_objects.json not found: {ddl_path}", file=sys.stderr)
        sys.exit(1)
    ddl_objects = json.loads(ddl_path.read_text())

    output_dir = work_dir / "deprecated"
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Scan ───────────────────────────────────────────────────────────────
    print(f"\nPhase 3.6 — Deprecated Object Review")
    print("=" * 50)
    print(f"Scanning {len(ddl_objects)} objects against {len(patterns)} deprecated patterns...\n")

    matches_by_id = scan_objects(ddl_objects, patterns)

    if not matches_by_id:
        print("No deprecated patterns detected. All objects proceed to conversion.\n")
        result = {
            "groups": {},
            "skip_objects": [],
            "migrate_objects": [o["fqn"] for o in ddl_objects if "fqn" in o],
            "modernize_objects": [],
        }
        (output_dir / "deprecated_review.json").write_text(json.dumps(result, indent=2))
        (output_dir / "deprecated_report.md").write_text(
            _render_report(patterns, {}, result)
        )
        print(f"Review written: {output_dir / 'deprecated_review.json'}")
        return

    # ── User review ────────────────────────────────────────────────────────
    pattern_map = {p["id"]: p for p in patterns}
    groups: dict[str, dict] = {}

    for pid, matches in matches_by_id.items():
        p = pattern_map[pid]
        severity_tag = {"advisory": "ℹ", "warning": "⚠", "critical": "🔴"}.get(p["severity"], "•")

        print(f"{severity_tag}  Deprecated pattern: {p['name']}")
        print(f"   {len(matches)} object(s) detected")
        # Show first 5 names
        for obj in matches[:5]:
            print(f"   • {obj.get('fqn', obj.get('name', '?'))} ({obj.get('type', '?')})")
        if len(matches) > 5:
            print(f"   • ... and {len(matches)-5} more")
        print()
        print(f"   Description: {p['description'].strip()[:200]}")
        print()
        print(f"   Recommendation: {p['recommendation'].strip()[:200]}")
        print()

        disposition = _prompt_disposition(p, matches, args.non_interactive)
        groups[pid] = {
            "pattern_name": p["name"],
            "severity": p["severity"],
            "disposition": disposition,
            "object_count": len(matches),
            "object_fqns": [o.get("fqn", o.get("name", "?")) for o in matches],
        }
        print(f"   → Disposition recorded: {disposition}\n")

    # ── Build disposition lists ────────────────────────────────────────────
    skip_fqns:      set[str] = set()
    migrate_fqns:   set[str] = set()
    modernize_fqns: set[str] = set()

    for pid, gdata in groups.items():
        fqns = set(gdata["object_fqns"])
        d = gdata["disposition"]
        if d == "skip":
            skip_fqns.update(fqns)
        elif d == "modernize":
            modernize_fqns.update(fqns)
        else:
            migrate_fqns.update(fqns)

    # Objects NOT matched by any deprecated pattern always go to migrate
    matched_fqns = {fqn for g in groups.values() for fqn in g["object_fqns"]}
    for obj in ddl_objects:
        fqn = obj.get("fqn", obj.get("name", ""))
        if fqn and fqn not in matched_fqns:
            migrate_fqns.add(fqn)

    review = {
        "groups": groups,
        "skip_objects":     sorted(skip_fqns),
        "migrate_objects":  sorted(migrate_fqns),
        "modernize_objects": sorted(modernize_fqns),
    }

    # ── Write outputs ──────────────────────────────────────────────────────
    review_path = output_dir / "deprecated_review.json"
    report_path = output_dir / "deprecated_report.md"

    review_path.write_text(json.dumps(review, indent=2))
    report_path.write_text(_render_report(patterns, matches_by_id, review))

    # ── Summary ────────────────────────────────────────────────────────────
    print("=" * 50)
    print(f"Deprecated groups reviewed : {len(groups)}")
    print(f"Objects to SKIP            : {len(skip_fqns)}")
    print(f"Objects to MIGRATE         : {len(migrate_fqns)}")
    print(f"Objects to MODERNIZE       : {len(modernize_fqns)}")
    print(f"\nReview saved   : {review_path}")
    print(f"Report saved   : {report_path}")

    if skip_fqns:
        print(f"\nPhase 4 will skip {len(skip_fqns)} deprecated object(s).")


if __name__ == "__main__":
    main()
