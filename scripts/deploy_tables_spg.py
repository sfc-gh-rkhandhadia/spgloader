#!/usr/bin/env python3
"""Deploy tables to SPG with comprehensive T-SQL→PG conversion."""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))
import psycopg2

from spgloader.rules import get_loader as _get_rules
_rules = _get_rules(SKILL_DIR)


def full_convert_table(ddl: str) -> str:
    """Apply all SSMS DDL cleanup + T-SQL→PG conversions using YAML rule files."""

    # ── Phase 1: Pre-bracket cleanup (SSMS artifacts) ────────────────────────
    for rule in _rules.ddl_cleanup("pre_bracket"):
        flags = _rules._build_flags(rule.get("flags", ["IGNORECASE"]))
        replacement = rule.get("replacement") or ""
        if rule.get("name", "").startswith("graph_"):
            # Graph column rules capture a trailing comma to preserve it
            ddl = re.sub(rule["pattern"], lambda m, _r=replacement: m.group(1), ddl, flags=flags)
        else:
            ddl = re.sub(rule["pattern"], replacement, ddl, flags=flags)
    # Fix missing commas after computed-column removal
    ddl = re.sub(r"(NOT NULL|NULL)\n(\s+\[)", r"\1,\n\2", ddl)

    # ── Phase 2: Strip T-SQL brackets → double-quote identifiers ─────────────
    ddl = re.sub(r"\[([^\]]+)\]", r'"\1"', ddl)

    # ── Phase 3: Downcase all quoted identifiers ──────────────────────────────
    def _downcase(m: re.Match) -> str:
        name = m.group(1)
        if re.search(r"[^a-zA-Z0-9_]", name):
            return f'"{name.lower()}"'
        return name.lower()
    ddl = re.sub(r'"([^"]+)"', _downcase, ddl)

    # ── Phase 4: Post-bracket type mappings (all lowercase now) ──────────────
    for rule in _rules.type_mappings("post_downcase"):
        ddl = re.sub(rule["pattern"], rule["replacement"], ddl, flags=re.IGNORECASE)

    # ── Phase 5: Function / default substitutions (post-downcase) ────────────
    func_rules = [
        (r"sysutcdatetime\s*\(\s*\)", "NOW()"),
        (r"getdate\s*\(\s*\)", "NOW()"),
        (r"getutcdate\s*\(\s*\)", "NOW()"),
        (r"suser_sname\s*\(\s*\)", "CURRENT_USER"),
        (r"newsequentialid\s*\(\s*\)", "gen_random_uuid()"),
        (r"newid\s*\(\s*\)", "gen_random_uuid()"),
    ]
    for pattern, replacement in func_rules:
        ddl = re.sub(pattern, replacement, ddl, flags=re.IGNORECASE)

    # ── Phase 6: BOOLEAN defaults ─────────────────────────────────────────────
    ddl = re.sub(r"(boolean[^,\n]*default\s+)\(\(1\)\)", r"\1TRUE",  ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"(boolean[^,\n]*default\s+)\(\(0\)\)", r"\1FALSE", ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"\bdefault\s+\(\(1\)\)", "DEFAULT TRUE",  ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"\bdefault\s+\(\(0\)\)", "DEFAULT FALSE", ddl, flags=re.IGNORECASE)

    # ── Phase 7: Post-bracket DDL cleanup ─────────────────────────────────────
    for rule in _rules.ddl_cleanup("post_bracket"):
        flags = _rules._build_flags(rule.get("flags", ["IGNORECASE"]))
        replacement = rule.get("replacement") or ""
        ddl = re.sub(rule["pattern"], replacement, ddl, flags=flags)

    # ── Phase 8: Second downcase pass (catches remaining quoted identifiers) ──
    ddl = re.sub(r'"([a-zA-Z][a-zA-Z0-9_]*)"', lambda m: m.group(1).lower(), ddl)

    # ── Phase 9: Requote PG reserved keywords used as column names ───────────
    # MUST run AFTER the second downcase pass so requoted identifiers survive.
    pg_types = "|".join(re.escape(t) for t in _rules.pg_type_names())
    for kw in _rules.pg_reserved_keywords():
        ddl = re.sub(
            rf'\b({re.escape(kw)})\s+({pg_types})',
            rf'"{kw}" \2',
            ddl, flags=re.IGNORECASE,
        )

    # ── Final cleanup ─────────────────────────────────────────────────────────
    ddl = re.sub(r"\n{3,}", "\n\n", ddl)
    return ddl.strip()


def _deploy_table(task: dict) -> dict:
    """Deploy one table in its own connection. Called from a worker thread."""
    spg_service = task["spg_service"]
    fqn = task["fqn"]
    ddl = task["ddl"]
    try:
        conn = psycopg2.connect(f"service={spg_service}")
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        conn.close()
        return {"fqn": fqn, "ok": True}
    except Exception as e:
        return {"fqn": fqn, "ok": False, "error": str(e).strip().split("\n")[0]}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Deploy MSSQL tables to SPG")
    parser.add_argument("--work-dir", required=True, help="spgloader workspace directory (e.g. ~/.spgloader/20260101_120000)")
    parser.add_argument("--ddl-objects", default=None, help="Path to ddl_objects.json (default: <work-dir>/ddl_objects.json)")
    parser.add_argument("--dep-graph", default=None, help="Path to dep_graph.json (default: <work-dir>/dep_graph.json)")
    parser.add_argument("--spg-service", required=True, help="pg_service name from ~/.pg_service.conf (e.g. my_project_spg)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel connections for table deployment (default: 4)")
    args = parser.parse_args()

    work = Path(args.work_dir)
    ddl_path = Path(args.ddl_objects) if args.ddl_objects else work / "ddl_objects.json"
    dep_path = Path(args.dep_graph) if args.dep_graph else work / "dep_graph.json"

    objs = json.loads(ddl_path.read_text())
    dep_graph = json.loads(dep_path.read_text())
    tables = {o["fqn"]: o for o in objs if o["type"] == "table"}

    # Create sequences found in DDL (sequential — sequences must exist before tables)
    all_seqs = set()
    for o in objs:
        ddl = o.get("ddl", "")
        found = re.findall(r'NEXT\s+VALUE\s+FOR\s+\[([^\]]+)\]\.\[([^\]]+)\]', ddl, re.IGNORECASE)
        for schema, seq in found:
            all_seqs.add((schema.lower(), seq.lower()))

    print(f"Creating {len(all_seqs)} sequence(s)...")
    seq_conn = psycopg2.connect(f"service={args.spg_service}")
    for schema, seq in sorted(all_seqs):
        try:
            with seq_conn:
                with seq_conn.cursor() as cur:
                    cur.execute(f'CREATE SEQUENCE IF NOT EXISTS "{schema}"."{seq}" START 1')
            print(f"  SEQ {schema}.{seq}: OK")
        except Exception as e:
            print(f"  SEQ {schema}.{seq}: {e}")
    seq_conn.close()

    # Build the deployment task list in dep order
    tasks = []
    for entry in dep_graph["ordered_objects"]:
        fqn = entry["fqn"]
        if fqn not in tables:
            continue
        converted = full_convert_table(tables[fqn]["ddl"])
        tasks.append({"spg_service": args.spg_service, "fqn": fqn, "ddl": converted})

    # Deploy tables in parallel using a thread pool.
    # Each worker gets its own psycopg2 connection (connections are not thread-safe).
    workers = max(1, args.workers)
    print(f"\nDeploying {len(tasks)} tables with {workers} parallel worker(s)...")
    results = {"succeeded": [], "failed": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_deploy_table, task): task["fqn"] for task in tasks}
        done = 0
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if result["ok"]:
                results["succeeded"].append(result["fqn"])
                if done % 100 == 0 or done == len(tasks):
                    print(f"  [{done}/{len(tasks)}] {result['fqn']}: OK")
            else:
                results["failed"].append({"fqn": result["fqn"], "error": result["error"]})
                print(f"  FAIL  {result['fqn']}: {result['error']}")
    print(f"\nTables: {len(results['succeeded'])} OK, {len(results['failed'])} failed")
    if results["failed"]:
        print("Remaining failures:")
        for f in results["failed"]:
            print(f"  - {f['fqn']}: {f['error']}")

    return len(results["failed"]) == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
