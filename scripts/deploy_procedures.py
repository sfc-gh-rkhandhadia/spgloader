#!/usr/bin/env python3
"""
deploy_procedures.py — Deploy stored procedure SQL files to SPG.

Reads wave_4_procedures_triggers/*.sql, deploys each one, and writes a report.

Schema routing:
  Files named with the schema__procname.sql convention (produced by convert_objects.py
  for MySQL/MariaDB/Oracle) carry the target PostgreSQL schema in the filename prefix.
  The script injects  SET search_path TO <schema>, public;  before each procedure so
  it lands in the correct schema instead of public.

  Override with --schema-mode:
    auto   (default) — infer from filename; fall back to public if no prefix
    source — force source-schema routing even for files without a prefix (uses
             source_conn.env + ddl_objects.json to map procedure → schema)
    public — force all procedures into public (old behaviour)

Legacy procedure detection:
  This script NEVER prompts for user input via stdin.
  All legacy/deprecated decisions must be made BEFORE calling this script:

  1. Phase 3.6 (analyze_deprecated.py) captures user decisions in
     deprecated/deprecated_review.json via the skill's ask_user_question.

  2. If legacy groups are found that are NOT in deprecated_review.json,
     the script writes them to deprecated/legacy_groups_pending.json
     and exits with code 2.  The calling SKILL must then prompt the user
     via ask_user_question, update deprecated_review.json, and re-run.

  3. On re-run, all groups are resolved in deprecated_review.json and
     the deploy proceeds non-interactively.

Usage:
  python deploy_procedures.py --work-dir ~/.spgloader/... --spg-service ...
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


# ---------------------------------------------------------------------------
# Core deploy
# ---------------------------------------------------------------------------

def _extract_proc_name(sql: str) -> str | None:
    m = re.search(
        r'CREATE\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION)\s+(["\w]+(?:\.["\w]+)?)\s*\(',
        sql, re.IGNORECASE,
    )
    if not m:
        return None
    return m.group(1).lower().rstrip('"').strip('"')


def _short_name(fq_name: str) -> str:
    """Return the unqualified procedure name (strip schema prefix)."""
    return fq_name.split(".")[-1]


def _load_deprecated_skip_set(work_dir: Path) -> set[str]:
    """Read deprecated_review.json (Phase 3.6) and return FQNs marked 'skip'.

    Returns an empty set if the file does not exist (pre-3.6 workspace).
    """
    review_path = work_dir / "deprecated" / "deprecated_review.json"
    if not review_path.exists():
        return set()
    try:
        import json
        data = json.loads(review_path.read_text(encoding="utf-8"))
        skip_fqns: set[str] = set()
        for group in data.get("groups", {}).values():
            if group.get("disposition") == "skip":
                for fqn in group.get("object_fqns", []):
                    skip_fqns.add(fqn.lower())
        return skip_fqns
    except Exception as e:
        print(f"  WARN: could not load deprecated_review.json: {e}", file=sys.stderr)
        return set()


# Exit code 2 = pending user decisions; skill must prompt then re-run.
EXIT_PENDING_DECISIONS = 2


# ---------------------------------------------------------------------------
# Schema routing helpers
# ---------------------------------------------------------------------------

def _schema_from_filename(f: "Path") -> str | None:
    """Return the target PostgreSQL schema inferred from the filename.

    Convention: files created by convert_objects.py for multi-schema sources
    (MySQL, MariaDB, Oracle) use  schema__procname.sql  (double-underscore).
    Returns the schema prefix, or None when the convention is not present.
    """
    stem = f.stem  # e.g. 'evdas__proc_evdas_de_populate_stg'
    if "__" in stem:
        prefix = stem.split("__")[0].strip()
        if prefix:  # guard against leading __ files
            return prefix
    return None


def _build_fqn_schema_map(work_dir: "Path") -> dict[str, str]:
    """Build {proc_name_lower: schema} from ddl_objects.json for fallback lookup."""
    try:
        ddl_path = work_dir / "ddl_objects.json"
        if not ddl_path.exists():
            return {}
        import json
        objects = json.loads(ddl_path.read_text(encoding="utf-8"))
        if isinstance(objects, dict):
            objects = objects.get("objects", [])
        result = {}
        for obj in objects:
            if obj.get("type") in ("procedure", "function", "PROCEDURE", "FUNCTION"):
                schema = obj.get("schema", "")
                name = obj.get("name", "")
                if schema and name:
                    result[name.lower()] = schema.lower()
        return result
    except Exception:
        return {}


def deploy_procedures(
    work_dir: Path,
    spg_service: str,
    dry_run: bool = False,
    include_legacy: bool = False,
    exclude_legacy: bool = False,
    interactive: bool = False,
    skill_dir: Path | None = None,
    schema_mode: str = "auto",  # "auto" | "source" | "public"
) -> dict:
    """Deploy procedures from wave_4_procedures_triggers/ to SPG.

    NEVER prompts via stdin.  All legacy/deprecated decisions come from
    deprecated/deprecated_review.json (written by Phase 3.6 or by the
    skill after calling ask_user_question).

    interactive=False (default / --no-interactive):
      Undecided legacy groups are skipped silently.  Safe for pipelines.

    interactive=True (skill omits --no-interactive):
      Undecided legacy groups trigger exit code 2 + legacy_groups_pending.json
      so the skill can prompt via ask_user_question and re-run.

    schema_mode="auto" (default):
      Infer target schema from filename prefix (schema__name.sql).  Files
      without the double-underscore convention land in public unchanged.

    schema_mode="public":
      Force all procedures into public (legacy behaviour).
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

    # Load Phase 3.6 deprecated decisions — these take priority over everything
    deprecated_skip = _load_deprecated_skip_set(work_dir)
    if deprecated_skip:
        print(f"  Phase 3.6 review: {len(deprecated_skip)} object(s) pre-marked 'skip'")

    # Load legacy rules (for objects NOT covered by deprecated_review.json)
    rules = _load_legacy_rules(skill_dir)

    # ── 1. Parse all procedures and classify them ─────────────────────────
    # Build schema fallback map from ddl_objects.json (used when filename has no prefix)
    fqn_schema_map = _build_fqn_schema_map(work_dir) if schema_mode != "public" else {}

    procs: list[dict] = []   # {file, sql, name, label, target_schema}
    for f in proc_files:
        sql = f.read_text(encoding="utf-8", errors="replace").strip()
        name = _extract_proc_name(sql) or f.stem
        sname = _short_name(name)

        # Determine target schema for this procedure
        if schema_mode == "public":
            target_schema = None
        else:
            # Try filename prefix first (most reliable)
            target_schema = _schema_from_filename(f)
            if target_schema is None:
                # Fall back to ddl_objects.json lookup
                target_schema = fqn_schema_map.get(sname.lower())

        # Check Phase 3.6 deprecated decision first
        fqn_lower = name.lower()
        short_lower = sname.lower()
        deprecated = any(
            fqn_lower == d or short_lower == d.split(".")[-1]
            for d in deprecated_skip
        )
        label = None if deprecated else _classify_procedure(sname, rules)
        procs.append({"file": f, "sql": sql, "name": name, "label": label,
                      "deprecated_skip": deprecated, "target_schema": target_schema})

    # ── 2. Handle legacy groups ───────────────────────────────────────────
    # Collect unique legacy labels for procedures NOT already decided by Phase 3.6
    legacy_labels: dict[str, list[str]] = {}  # label → [proc_names]
    for p in procs:
        if p["label"] and not p["deprecated_skip"]:
            legacy_labels.setdefault(p["label"], []).append(p["name"])

    skip_labels: set[str] = set()  # labels skipped

    # Report Phase 3.6 pre-decided objects
    already_deprecated = [p["name"] for p in procs if p["deprecated_skip"]]
    if already_deprecated:
        print(f"  Phase 3.6 decisions: skipping {len(already_deprecated)} pre-reviewed object(s)")

    if legacy_labels:
        # Load decisions from deprecated_review.json
        review_path = work_dir / "deprecated" / "deprecated_review.json"
        # Map label → disposition ("skip" | "migrate" | None)
        recorded_disposition: dict[str, str] = {}
        # Also map every FQN in object_fqns → disposition so we can look up
        # by procedure name when the rule label doesn't match the group key
        fqn_disposition: dict[str, str] = {}
        if review_path.exists():
            try:
                rdata = json.loads(review_path.read_text(encoding="utf-8"))
                for gkey, gval in rdata.get("groups", {}).items():
                    disp = gval.get("disposition")
                    if disp in ("skip", "migrate"):
                        # Index by group key, pattern_name, and any prefix of key
                        recorded_disposition[gkey.lower()] = disp
                        pname = gval.get("pattern_name", "")
                        if pname:
                            recorded_disposition[pname.lower()] = disp
                        # Index every known FQN in this group by short name
                        for fqn in gval.get("object_fqns", []):
                            fqn_disposition[fqn.lower()] = disp
                            fqn_disposition[fqn.split(".")[-1].lower()] = disp
            except Exception:
                pass

        def _lookup_disposition(lbl: str, proc_names: list[str]) -> str | None:
            """Look up disposition for a legacy label.
            Falls back to FQN lookup when label doesn't match group key
            (e.g. rule label 'aspnet' vs group key 'aspnet_membership').
            """
            # 1. Exact label / pattern_name match
            d = recorded_disposition.get(lbl.lower())
            if d:
                return d
            # 2. Prefix match: rule label is a prefix of a group key
            for key, disp in recorded_disposition.items():
                if key.startswith(lbl.lower()) or lbl.lower().startswith(key):
                    return disp
            # 3. FQN match: look up any procedure in this group by name
            for name in proc_names:
                short = name.split(".")[-1].lower()
                d = fqn_disposition.get(name.lower()) or fqn_disposition.get(short)
                if d:
                    return d
            return None

        # Separate already-decided from undecided
        undecided: dict[str, list[str]] = {}
        for lbl, names in legacy_labels.items():
            disp = _lookup_disposition(lbl, names)
            if disp == "skip":
                skip_labels.add(lbl)
                print(f"  Skipping legacy group [{lbl}] ({len(names)} procs) — user chose skip")
            elif disp == "migrate":
                print(f"  Including legacy group [{lbl}] ({len(names)} procs) — user chose migrate")
                # Not in skip_labels → will be deployed
            else:
                undecided[lbl] = names

        if undecided:
            if not interactive:
                # --no-interactive (default): skip undecided groups, never block
                for lbl, names in undecided.items():
                    skip_labels.add(lbl)
                    rule = next((r for r in rules if r["label"] == lbl), {})
                    print(
                        f"  Skipping legacy group [{lbl}] ({len(names)} procs) "
                        f"— no decision recorded, --no-interactive mode"
                    )
            else:
                # Interactive mode: write pending file and exit so the skill
                # can prompt the user via ask_user_question, then re-run.
                pending: list[dict] = []
                for lbl, names in undecided.items():
                    rule = next((r for r in rules if r["label"] == lbl), {})
                    pending.append({
                        "label": lbl,
                        "description": rule.get("description", ""),
                        "procedures": sorted(names),
                    })
                pending_path = work_dir / "deprecated" / "legacy_groups_pending.json"
                pending_path.parent.mkdir(parents=True, exist_ok=True)
                pending_path.write_text(
                    json.dumps({"pending_groups": pending}, indent=2), encoding="utf-8"
                )
                print(
                    f"\n{len(undecided)} legacy group(s) need user decisions."
                    f"\nWritten to: {pending_path}"
                    f"\nThe skill will prompt the user via ask_user_question,"
                    f" update deprecated_review.json, then re-run this script.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_PENDING_DECISIONS)

    # ── 3. Build the final ordered deploy list ────────────────────────────
    to_deploy = []
    skipped_legacy = []
    for p in procs:
        if p["deprecated_skip"] or p["label"] in skip_labels:
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
    # Log schema routing summary
    schemas_used = {p["target_schema"] for p in to_deploy if p.get("target_schema")}
    if schemas_used:
        print(f"  Schema routing: {sorted(schemas_used)} (via search_path injection)")
    else:
        print("  Schema routing: public (no schema prefix detected)")

    print(f"\nDeploying {len(to_deploy)} procedures...")
    conn = psycopg2.connect(f"service={spg_service}")
    conn.autocommit = False
    results = {"succeeded": [], "failed": [], "skipped_legacy": skipped_legacy}

    for p in to_deploy:
        # Build the SQL to execute: optionally prepend search_path so the
        # procedure lands in the correct schema instead of public.
        target_schema = p.get("target_schema")
        if target_schema:
            deploy_sql = f'SET search_path TO "{target_schema}", public;\n{p["sql"]}'
        else:
            deploy_sql = p["sql"]

        fqn = f"{target_schema}.{p['name']}" if target_schema else p["name"]
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(deploy_sql)
            results["succeeded"].append(fqn)
            schema_tag = f" [{target_schema}]" if target_schema else ""
            print(f"  OK    {p['name']}{schema_tag}")
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
                    # Use the schema-qualified table name for the DROP
                    tbl_ref = (f'"{target_schema}".{tbl_name}'
                               if target_schema else f'dbo.{tbl_name}')
                    try:
                        with conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    f'DROP TRIGGER IF EXISTS {trig_name} '
                                    f'ON {tbl_ref} CASCADE'
                                )
                                cur.execute(deploy_sql)
                        results["succeeded"].append(fqn)
                        print(f"  OK    {p['name']}  (dropped + recreated trigger)")
                        continue
                    except Exception as e2:
                        conn.rollback()
                        err = str(e2).replace("\n", " ").strip()

            results["failed"].append({
                "procedure": fqn, "file": p["file"].name, "error": err
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
    parser.add_argument("--no-interactive", action="store_true", default=True,
                        help="Non-interactive mode: skip undecided legacy groups silently (DEFAULT)")
    parser.add_argument("--interactive", action="store_true", default=False,
                        help="Interactive mode: pause for user decision on undecided legacy groups (skill use only)")
    parser.add_argument("--repair", action="store_true",
                        help="After deploy, run repair_procedures.py on any failures")
    parser.add_argument("--rules-only", action="store_true",
                        help="With --repair: apply rule-based fixes only, skip LLM")
    parser.add_argument("--model", default=None,
                        help="With --repair: Cortex model override (e.g. mistral-large2)")
    parser.add_argument("--repair-iterations", type=int, default=None,
                        help="With --repair: max LLM iterations per procedure")
    parser.add_argument(
        "--schema-mode",
        default="auto",
        choices=["auto", "source", "public"],
        help=(
            "Schema routing mode. "
            "auto (default): infer from filename schema__name.sql prefix. "
            "public: force all procedures into public (legacy). "
            "source: same as auto but required for backward compat."
        ),
    )
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
        interactive=args.interactive,  # False by default; True only when skill passes --interactive
        skill_dir=skill_dir,
        schema_mode=args.schema_mode,
    )

    if args.dry_run:
        return

    report_path = work_dir / "conversion" / "procedures_deploy_report.json"
    report_path.write_text(json.dumps(results, indent=2))

    # Update object manifest with deployment results
    try:
        from spgloader.manifest import ObjectManifest
        manifest = ObjectManifest(work_dir)
        for name in results.get("succeeded", []):
            manifest.set_deployed(name, "completed")
        for item in results.get("failed", []):
            fqn = item.get("procedure", "") if isinstance(item, dict) else str(item)
            err = item.get("error", "") if isinstance(item, dict) else ""
            manifest.set_deployed(fqn, "failed", error=err[:200])
        manifest.save()
    except Exception:
        pass  # manifest is optional

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
