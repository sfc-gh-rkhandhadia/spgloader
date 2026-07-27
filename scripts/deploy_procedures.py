#!/usr/bin/env python3
"""
deploy_procedures.py — Deploy stored procedure SQL files to SPG.

Reads wave_4_procedures_triggers/*.sql, deploys each one, and writes a report.

Legacy procedure detection:
  Before deploying, the script groups procedures that match configurable
  legacy-prefix rules (e.g. aspnet_*, sp_fivetran_*) and prompts the user
  whether to deploy each group.  Use --include-legacy to skip the prompts
  and deploy everything, or --exclude-legacy to skip all legacy groups.

  Legacy rules live in references/legacy-proc-rules.yaml in the skill
  directory (next to this script's parent).  They are intentionally kept
  separate from the workspace so one rule set applies to all projects.

Usage:
  # Interactive (default): prompts for each legacy group found
  python deploy_procedures.py --work-dir ~/.spgloader/... --spg-service ...

  # Non-interactive: deploy everything including legacy
  python deploy_procedures.py ... --include-legacy

  # Non-interactive: skip all legacy groups
  python deploy_procedures.py ... --exclude-legacy
"""
import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Legacy detection
# ---------------------------------------------------------------------------

# Default rules used when no external YAML is found.
# Each rule: {label, description, patterns: [regex, ...]}
# A procedure matches a rule when its short name (without schema) matches
# ANY of the patterns (case-insensitive prefix or full regex).
_DEFAULT_LEGACY_RULES = [
    {
        "label": "aspnet",
        "description": (
            "ASP.NET Membership/Role/Profile provider procedures "
            "(Microsoft legacy framework — typically not required in new deployments)"
        ),
        "patterns": [r"^aspnet_"],
    },
    {
        "label": "sp_fivetran",
        "description": (
            "Fivetran CDC/replication procedures "
            "(SQL Server Change Data Capture API — not portable to PostgreSQL)"
        ),
        "patterns": [r"^sp_fivetran_"],
    },
    {
        "label": "sp_system",
        "description": (
            "SQL Server system-style procedures (sp_ prefix) that call "
            "SQL Server-specific system objects or replication APIs"
        ),
        "patterns": [r"^sp_repldone$", r"^sp_replflush$", r"^sp_repltrans$"],
    },
]


def _load_legacy_rules(skill_dir: Path | None) -> list[dict]:
    """Load legacy rules from YAML if available, otherwise use defaults."""
    if skill_dir is None:
        return _DEFAULT_LEGACY_RULES
    yaml_path = skill_dir / "references" / "legacy-proc-rules.yaml"
    if not yaml_path.exists():
        return _DEFAULT_LEGACY_RULES
    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        return data.get("rules", _DEFAULT_LEGACY_RULES)
    except Exception as e:
        print(f"  WARN: could not load {yaml_path}: {e} — using defaults",
              file=sys.stderr)
        return _DEFAULT_LEGACY_RULES


def _classify_procedure(short_name: str, rules: list[dict]) -> str | None:
    """Return the rule label if short_name matches a legacy rule, else None."""
    for rule in rules:
        for pattern in rule.get("patterns", []):
            if re.search(pattern, short_name, re.IGNORECASE):
                return rule["label"]
    return None


def _prompt_deploy_group(label: str, description: str,
                          names: list[str], interactive: bool) -> bool:
    """Ask the user whether to deploy a legacy group.

    Returns True if the group should be deployed.
    Always returns True when not interactive (handled by caller).
    """
    print(f"\n  ┌─ Legacy group detected: [{label}]")
    print(f"  │  {description}")
    print(f"  │  Procedures in this group ({len(names)}):")
    for n in sorted(names):
        print(f"  │    • {n}")
    print(f"  └─ Deploy this group? [y/N]: ", end="", flush=True)
    answer = input().strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Core deploy
# ---------------------------------------------------------------------------

def _extract_proc_name(sql: str) -> str | None:
    m = re.search(
        r'CREATE\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION)\s+(\S+)\s*\(',
        sql, re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1).lower().rstrip('"').strip('"')


def _short_name(fq_name: str) -> str:
    """Return the unqualified procedure name (strip schema prefix)."""
    return fq_name.split(".")[-1]


def deploy_procedures(
    work_dir: Path,
    spg_service: str,
    dry_run: bool = False,
    include_legacy: bool = False,
    exclude_legacy: bool = False,
    interactive: bool = True,
    skill_dir: Path | None = None,
) -> dict:
    """Deploy procedures from wave_4_procedures_triggers/ to SPG.

    Parameters
    ----------
    include_legacy  : deploy all legacy groups without prompting
    exclude_legacy  : skip all legacy groups without prompting
    interactive     : when True, prompt for each legacy group found
    """
    import psycopg2

    input_dir = work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers"
    if not input_dir.exists():
        print(f"ERROR: directory not found: {input_dir}", file=sys.stderr)
        return {}

    proc_files = sorted(input_dir.glob("*.sql"))
    if not proc_files:
        print("No procedure files found.")
        return {}

    # Load legacy rules
    rules = _load_legacy_rules(skill_dir)

    # ── 1. Parse all procedures and classify them ─────────────────────────
    procs: list[dict] = []   # {file, sql, name, label}
    for f in proc_files:
        sql = f.read_text(encoding="utf-8", errors="replace").strip()
        name = _extract_proc_name(sql) or f.stem
        sname = _short_name(name)
        label = _classify_procedure(sname, rules)
        procs.append({"file": f, "sql": sql, "name": name, "label": label})

    # ── 2. Handle legacy groups ───────────────────────────────────────────
    # Collect unique legacy labels present in this workspace
    legacy_labels: dict[str, list[str]] = {}  # label → [proc_names]
    for p in procs:
        if p["label"]:
            legacy_labels.setdefault(p["label"], []).append(p["name"])

    deploy_labels: set[str] = set()   # labels approved for deployment
    skip_labels: set[str] = set()     # labels skipped

    if legacy_labels:
        if include_legacy:
            deploy_labels = set(legacy_labels)
            print(f"\n  --include-legacy: deploying all {len(legacy_labels)} legacy group(s)")
        elif exclude_legacy:
            skip_labels = set(legacy_labels)
            print(f"\n  --exclude-legacy: skipping all {len(legacy_labels)} legacy group(s):")
            for lbl, names in legacy_labels.items():
                rule = next((r for r in rules if r["label"] == lbl), {})
                print(f"    [{lbl}] {len(names)} procs — {rule.get('description','')[:60]}")
        elif interactive:
            print(f"\nLegacy procedure groups found: {len(legacy_labels)}")
            for lbl, names in legacy_labels.items():
                rule = next((r for r in rules if r["label"] == lbl), {})
                if _prompt_deploy_group(lbl, rule.get("description", ""), names, interactive):
                    deploy_labels.add(lbl)
                    print(f"  → [{lbl}]: will be deployed")
                else:
                    skip_labels.add(lbl)
                    print(f"  → [{lbl}]: will be skipped")
        else:
            # Non-interactive with no flag: default to skipping legacy
            skip_labels = set(legacy_labels)
            print(f"\n  Non-interactive mode: skipping {len(skip_labels)} legacy group(s) "
                  f"(use --include-legacy to deploy them)")

    # ── 3. Build the final ordered deploy list ────────────────────────────
    to_deploy = []
    skipped_legacy = []
    for p in procs:
        if p["label"] in skip_labels:
            skipped_legacy.append(p["name"])
        else:
            to_deploy.append(p)

    if skipped_legacy:
        print(f"\nSkipping {len(skipped_legacy)} legacy procedure(s).")

    if dry_run:
        print(f"\nDRY-RUN — {len(to_deploy)} procedures would be deployed:")
        for p in to_deploy:
            print(f"  WOULD DEPLOY  {p['name']}")
        return {"dry_run": True, "skipped_legacy": skipped_legacy}

    if not to_deploy:
        print("Nothing to deploy.")
        return {"succeeded": [], "failed": [], "skipped_legacy": skipped_legacy}

    # ── 4. Deploy ─────────────────────────────────────────────────────────
    print(f"\nDeploying {len(to_deploy)} procedures...")
    conn = psycopg2.connect(f"service={spg_service}")
    conn.autocommit = False
    results = {"succeeded": [], "failed": [], "skipped_legacy": skipped_legacy}

    for p in to_deploy:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(p["sql"])
            results["succeeded"].append(p["name"])
            print(f"  OK    {p['name']}")
        except Exception as e:
            conn.rollback()
            err = str(e).replace("\n", " ").strip()

            # Auto-retry: if a trigger "already exists", DROP it and retry once.
            # This happens when deploy_procedures.py is re-run after a partial deploy —
            # PostgreSQL has no CREATE OR REPLACE TRIGGER syntax.
            if "already exists" in err and "trigger" in err.lower():
                m = re.search(
                    r'trigger "(\w+)" for relation "(\w+)"', err, re.IGNORECASE
                )
                if m:
                    trig_name, tbl_name = m.group(1), m.group(2)
                    try:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    f'DROP TRIGGER IF EXISTS {trig_name} '
                                    f'ON dbo.{tbl_name} CASCADE'
                                )
                                cur.execute(p["sql"])
                        results["succeeded"].append(p["name"])
                        print(f"  OK    {p['name']}  (dropped + recreated trigger)")
                        continue
                    except Exception as e2:
                        conn.rollback()
                        err = str(e2).replace("\n", " ").strip()

            results["failed"].append({
                "procedure": p["name"], "file": p["file"].name, "error": err
            })
            print(f"  FAIL  {p['name']}: {err[:120]}")

    conn.close()
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy stored procedures to SPG with legacy-group detection"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--spg-service", required=True,
                        help="pg_service name from ~/.pg_service.conf")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and classify but do not execute SQL")
    parser.add_argument("--include-legacy", action="store_true",
                        help="Deploy all legacy groups without prompting")
    parser.add_argument("--exclude-legacy", action="store_true",
                        help="Skip all legacy groups without prompting")
    parser.add_argument("--no-interactive", action="store_true",
                        help="Non-interactive mode (defaults to --exclude-legacy behaviour)")
    parser.add_argument("--repair", action="store_true",
                        help="After deploy, run repair_procedures.py on any failures")
    parser.add_argument("--rules-only", action="store_true",
                        help="With --repair: apply rule-based fixes only, skip LLM")
    parser.add_argument("--model", default=None,
                        help="With --repair: Cortex model override (e.g. mistral-large2)")
    parser.add_argument("--repair-iterations", type=int, default=None,
                        help="With --repair: max LLM iterations per procedure")
    args = parser.parse_args()

    if args.include_legacy and args.exclude_legacy:
        parser.error("--include-legacy and --exclude-legacy are mutually exclusive")

    work_dir = Path(args.work_dir).expanduser()
    # Skill dir: two levels up from this script (scripts/ → skill root)
    skill_dir = Path(__file__).parent.parent

    results = deploy_procedures(
        work_dir=work_dir,
        spg_service=args.spg_service,
        dry_run=args.dry_run,
        include_legacy=args.include_legacy,
        exclude_legacy=args.exclude_legacy,
        interactive=not args.no_interactive,
        skill_dir=skill_dir,
    )

    if args.dry_run:
        return

    report_path = work_dir / "conversion" / "procedures_deploy_report.json"
    report_path.write_text(json.dumps(results, indent=2))

    print(f"\n{'='*60}")
    print(f"Deployed OK     : {len(results.get('succeeded', []))}")
    print(f"Failed          : {len(results.get('failed', []))}")
    print(f"Skipped (legacy): {len(results.get('skipped_legacy', []))}")
    print(f"Report          : {report_path}")

    # ── Optional repair phase ──────────────────────────────────────────────
    if args.repair and results.get("failed"):
        print(f"\n{'='*60}")
        print(f"Starting repair phase for {len(results['failed'])} failed procedure(s)...")
        import importlib.util
        repair_path = Path(__file__).parent / "repair_procedures.py"
        if not repair_path.exists():
            print(f"ERROR: repair_procedures.py not found at {repair_path}",
                  file=sys.stderr)
        else:
            spec = importlib.util.spec_from_file_location(
                "repair_procedures", repair_path)
            rp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(rp)
            repair_result = rp.repair_procedures(
                work_dir=work_dir,
                spg_service=args.spg_service,
                rules_only=args.rules_only,
                model_override=args.model,
                max_iterations_override=args.repair_iterations,
            )
            total_fixed = (len(repair_result.get("fixed_rules", []))
                           + len(repair_result.get("fixed_llm", [])))
            print(f"\nRepair complete: {total_fixed} additional procedures fixed")


if __name__ == "__main__":
    main()
