"""
feedback.py — Automatic skill-improvement analysis after a migration run.

Reads migration artifacts from a work dir, diagnoses failure patterns,
and prints actionable recommendations to stdout.

Called automatically after Phase 6.6 (parity testing) completes.
Also callable from the feedback-analysis sub-skill for async review.

No files are written. No skill changes are applied.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_artifacts(work_dir: str | Path) -> None:
    """Read migration artifacts, print skill-improvement recommendations."""
    work_dir = Path(work_dir).expanduser()
    data = _load_artifacts(work_dir)
    if not data:
        return
    recs = []
    recs += _check_repair_effectiveness(data)
    recs += _check_error_patterns(data)
    recs += _check_parity_gaps(data)
    recs += _check_ewi_density(data)
    _print_report(recs, data)


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def _load_artifacts(work_dir: Path) -> dict:
    """Load all available artifact JSON files from work_dir."""
    conv   = work_dir / "conversion"
    deploy = work_dir / "deployment"
    parity = work_dir / "parity"

    def _read(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                return {}
        return {}

    data = {
        "proc_report":     _read(conv   / "procedures_deploy_report.json"),
        "func_report":     _read(conv   / "functions_deploy_report.json"),
        "repair_report":   _read(conv   / "repair_report.json"),
        "deploy_report":   _read(conv   / "deploy_report.json"),
        "deploy_summary":  _read(deploy / "deployment_summary.json"),
        "parity_results":  _read(parity / "parity_results.json"),
        "conv_metrics":    _read(conv   / "_conversion_metrics.json"),
    }
    # Only proceed if we have at least one meaningful artifact
    has_data = any(v for v in data.values())
    return data if has_data else {}


# ---------------------------------------------------------------------------
# Analysis checks
# ---------------------------------------------------------------------------

def _check_repair_effectiveness(data: dict) -> list[dict]:
    recs = []
    repair = data.get("repair_report", {})
    if not repair:
        return recs

    fixed_llm   = repair.get("fixed_llm",   [])
    fixed_rules = repair.get("fixed_rules",  [])
    still_failed = repair.get("still_failed", [])

    attempted = len(fixed_llm) + len(still_failed)
    if attempted == 0:
        return recs

    success_rate = len(fixed_llm) / attempted * 100

    if success_rate < 60 and still_failed:
        # Classify still-failed by error pattern
        err_groups: Counter = Counter()
        for item in still_failed:
            err = item.get("error", "") if isinstance(item, dict) else ""
            key = _classify_error(err)
            err_groups[key] += 1
        top_pattern, top_count = err_groups.most_common(1)[0] if err_groups else ("unknown", 0)
        recs.append({
            "level": "HIGH",
            "id": "REC-LLM-01",
            "title": "LLM repair success rate below 60%",
            "symptom": f"LLM repaired {len(fixed_llm)}/{attempted} objects ({success_rate:.0f}%)",
            "cause": f"Top unresolved pattern: \"{top_pattern}\" ({top_count} objects)",
            "suggestion": "Review procedure-repair-prompt.md — add examples covering this error pattern",
            "files": ["references/prompts/procedure-repair-prompt.md",
                      "references/prompts/procedure-repair-mysql-prompt.md"],
        })

    if fixed_rules and not fixed_llm:
        recs.append({
            "level": "LOW",
            "id": "REC-LLM-02",
            "title": "All objects fixed by rules alone (LLM not needed)",
            "symptom": f"{len(fixed_rules)} objects fixed by rule-based pass only",
            "cause": "Rules covered all failures — LLM pass was wasted time",
            "suggestion": "Consider running --rules-only first to skip LLM for this source type",
            "files": ["references/llm-repair-config.yaml"],
        })
    return recs


def _check_error_patterns(data: dict) -> list[dict]:
    recs = []
    # Collect all still-failed items across proc + func reports
    all_failed: list[dict] = []
    for key in ("proc_report", "func_report"):
        report = data.get(key, {})
        all_failed += [
            item for item in report.get("failed", [])
            if isinstance(item, dict)
        ]
    # Also include repair still_failed
    repair_sf = data.get("repair_report", {}).get("still_failed", [])
    all_failed += [item for item in repair_sf if isinstance(item, dict)]

    if not all_failed:
        return recs

    # Group by error class
    groups: defaultdict[str, list[str]] = defaultdict(list)
    for item in all_failed:
        err   = item.get("error", "")
        name  = item.get("procedure") or item.get("function") or "unknown"
        label = _classify_error(err)
        groups[label].append(name)

    for pattern, names in sorted(groups.items(), key=lambda x: -len(x[1])):
        if len(names) < 2:
            continue
        rec = _error_pattern_rec(pattern, names)
        if rec:
            recs.append(rec)
    return recs


def _check_parity_gaps(data: dict) -> list[dict]:
    recs = []
    parity = data.get("parity_results", {})
    if not parity:
        return recs

    missing = parity.get("missing_objects", [])
    row_diffs = [
        obj for obj in parity.get("row_count_results", [])
        if isinstance(obj, dict) and abs(obj.get("diff_pct", 0)) > 5
    ]

    if missing:
        # Separate FIX-REQUIRED (likely intentional) from unexpected
        fix_req  = [n for n in missing if "fix" in str(n).lower()]
        unexpected = [n for n in missing if n not in fix_req]
        if unexpected:
            recs.append({
                "level": "MEDIUM",
                "id": "REC-PARITY-01",
                "title": f"{len(unexpected)} objects missing in SPG (not FIX-REQUIRED)",
                "symptom": f"parity_results.missing_objects: {', '.join(str(n) for n in unexpected[:5])}{'...' if len(unexpected) > 5 else ''}",
                "cause": "Objects may have failed deploy silently or been excluded by wave filter",
                "suggestion": "Check deploy_report.json and procedures_deploy_report.json for these names",
                "files": ["sub-skills/deploy/SKILL.md"],
            })

    if row_diffs:
        names = [obj.get("table", "?") for obj in row_diffs[:5]]
        recs.append({
            "level": "MEDIUM",
            "id": "REC-PARITY-02",
            "title": f"{len(row_diffs)} tables have row count delta > 5%",
            "symptom": f"Tables: {', '.join(names)}",
            "cause": "Data copy may have missed rows (pgloader filter, FK constraint, or NULL mismatch)",
            "suggestion": "Review copy_source_data.py output logs for these tables",
            "files": ["scripts/copy_source_data.py"],
        })
    return recs


def _check_ewi_density(data: dict) -> list[dict]:
    recs = []
    metrics = data.get("conv_metrics", {})
    if not metrics:
        return recs

    ewi_counts = metrics.get("ewi_counts", {})
    total_objects = metrics.get("total_objects", 0) or 1

    # EWI-0012 = TODO markers (unconverted fragments)
    todo_count = ewi_counts.get("SPG-EWI-0012", 0)
    if todo_count > 0:
        density = todo_count / total_objects
        level = "HIGH" if density > 0.3 else "MEDIUM" if density > 0.1 else "LOW"
        recs.append({
            "level": level,
            "id": "REC-EWI-01",
            "title": f"{todo_count} unconverted fragments (SPG-EWI-0012 markers)",
            "symptom": f"{todo_count} TODO markers across {total_objects} converted objects ({density:.0%} density)",
            "cause": "convert_objects.py hit patterns not covered by its conversion rules",
            "suggestion": "Add patterns to references/rules/mssql-to-pg/plpgsql-fixes.yaml or fix convert_objects.py",
            "files": [
                "scripts/convert_objects.py",
                "references/rules/mssql-to-pg/plpgsql-fixes.yaml",
            ],
        })

    # Check other high-frequency EWI codes
    for code, count in sorted(ewi_counts.items(), key=lambda x: -x[1]):
        if code == "SPG-EWI-0012":
            continue
        if count >= 5:
            recs.append({
                "level": "LOW",
                "id": f"REC-EWI-{code}",
                "title": f"EWI code {code} triggered {count} times",
                "symptom": f"{count} objects flagged with {code}",
                "cause": "Recurring pattern that may benefit from an automated rule",
                "suggestion": f"Review references/rules/ for a {code} fix rule",
                "files": ["references/ewi-codes.md"],
            })
    return recs


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[str, str]] = [
    (r"column .+ does not exist",          "column does not exist"),
    (r"relation .+ does not exist",        "relation does not exist"),
    (r"function .+ does not exist",        "function does not exist"),
    (r"type .+ does not exist",            "type does not exist"),
    (r"syntax error at or near",           "syntax error"),
    (r"unterminated .* quoted",            "unterminated quoted identifier"),
    (r"division by zero",                  "division by zero"),
    (r"cursor",                            "CURSOR loop"),
    (r"undeclared variable",               "undeclared variable"),
    (r"operator does not exist",           "operator type mismatch"),
]

def _classify_error(error: str) -> str:
    err_lower = error.lower()
    for pattern, label in _ERROR_PATTERNS:
        if re.search(pattern, err_lower):
            return label
    if error:
        # Use first 60 chars of error as label
        return error[:60].strip()
    return "unknown error"


def _error_pattern_rec(pattern: str, names: list[str]) -> dict | None:
    """Map a common error pattern to a recommendation."""
    suggestions = {
        "column does not exist": (
            "HIGH",
            "Add column-alias or computed-column rule to plpgsql-fixes.yaml",
            ["references/rules/mssql-to-pg/plpgsql-fixes.yaml", "scripts/convert_objects.py"],
        ),
        "relation does not exist": (
            "HIGH",
            "Check schema_prefix.tables in view-fixes.yaml; deploy tables before views",
            ["references/fix-mappings/view-fixes.yaml", "sub-skills/deploy/SKILL.md"],
        ),
        "function does not exist": (
            "HIGH",
            "Deploy functions before procedures; check wave order in convert sub-skill",
            ["sub-skills/deploy/SKILL.md"],
        ),
        "type does not exist": (
            "HIGH",
            "UDTT array types cannot be auto-repaired — mark out-of-scope or convert manually",
            ["references/udtt-migration-guide.md"],
        ),
        "syntax error": (
            "MEDIUM",
            "Add syntax fix rule to plpgsql-fixes.yaml; check convert_objects.py output",
            ["references/rules/mssql-to-pg/plpgsql-fixes.yaml"],
        ),
        "CURSOR loop": (
            "MEDIUM",
            "Add CURSOR loop example to procedure-repair-prompt.md",
            ["references/prompts/procedure-repair-prompt.md"],
        ),
    }
    if pattern not in suggestions:
        return None
    level, suggestion, files = suggestions[pattern]
    return {
        "level": level,
        "id": f"REC-ERR-{pattern.replace(' ', '-').upper()[:20]}",
        "title": f"{len(names)} objects fail: \"{pattern}\"",
        "symptom": f"Affected: {', '.join(names[:5])}{'...' if len(names) > 5 else ''}",
        "cause": f"Recurring error pattern not covered by conversion rules",
        "suggestion": suggestion,
        "files": files,
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

_SEP = "=" * 62

def _print_report(recs: list[dict], data: dict) -> None:
    repair  = data.get("repair_report", {})
    parity  = data.get("parity_results", {})

    fixed_llm    = repair.get("fixed_llm",    [])
    fixed_rules  = repair.get("fixed_rules",   [])
    still_failed = repair.get("still_failed",  [])
    attempted    = len(fixed_llm) + len(still_failed)
    repair_rate  = f"{len(fixed_llm)/attempted*100:.0f}%" if attempted else "n/a"

    missing_count = len(parity.get("missing_objects", []))
    parity_total  = parity.get("total_objects", 0)
    parity_ok     = parity_total - missing_count if parity_total else None

    print()
    print(_SEP)
    print("SKILL IMPROVEMENT ANALYSIS")
    print(_SEP)

    # Summary
    print("\nSummary")
    if attempted:
        print(f"  LLM repair success rate : {repair_rate}  "
              f"({len(fixed_llm)} fixed / {attempted} attempted)")
        print(f"  Fixed by rules          : {len(fixed_rules)}")
        print(f"  Still-failed objects    : {len(still_failed)}")
    if parity_total:
        pct = f"{parity_ok/parity_total*100:.0f}%" if parity_ok is not None else "n/a"
        print(f"  Parity pass rate        : {pct}  "
              f"({missing_count} missing / {parity_total} total)")

    if not recs:
        print("\nNo improvement recommendations — all checks passed.")
        _print_footer()
        return

    # Sort: HIGH first, then MEDIUM, then LOW
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    recs_sorted = sorted(recs, key=lambda r: order.get(r["level"], 3))

    high_med = [r for r in recs_sorted if r["level"] in ("HIGH", "MEDIUM")]
    low      = [r for r in recs_sorted if r["level"] == "LOW"]

    if high_med:
        print("\nRecommendations")
        for rec in high_med:
            _print_rec(rec)

    if low:
        print("\nLow-priority notes")
        for rec in low:
            _print_rec(rec)

    _print_footer()


def _print_rec(rec: dict) -> None:
    print(f"\n  [{rec['level']}]  {rec['id']} · {rec['title']}")
    print(f"    Symptom  : {rec['symptom']}")
    print(f"    Cause    : {rec['cause']}")
    print(f"    Suggested: {rec['suggestion']}")
    for f in rec.get("files", []):
        print(f"    File     : {f}")


def _print_footer() -> None:
    print()
    print("> Suggestions only. No changes were made to the skill.")
    print("> To apply a fix: edit the suggested file, run  pytest tests/ -v,")
    print("> then  git commit.")
    print(_SEP)
    print()
