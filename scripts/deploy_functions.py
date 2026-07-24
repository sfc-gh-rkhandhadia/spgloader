#!/usr/bin/env python3
"""
deploy_functions.py — Deploy fixed function SQL files to SPG in dependency order.

Reads wave_3_functions_fixed/*.sql, detects inter-function dependencies,
deploys in topological order, and writes a deploy report.

Usage:
  python deploy_functions.py --work-dir ~/.spgloader/20260101_120000 \\
                             --spg-service <pg_service_name>
"""
import argparse
import json
import re
import sys
from pathlib import Path


def _extract_function_name(sql: str) -> str | None:
    """Extract the fully-qualified function name from CREATE OR REPLACE FUNCTION."""
    m = re.search(r'CREATE\s+OR\s+REPLACE\s+(?:FUNCTION|PROCEDURE)\s+(\S+)\s*\(',
                  sql, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower().rstrip('"').strip('"')


def _extract_called_functions(sql: str, all_names: set[str]) -> set[str]:
    """Find references to other functions in this function's body."""
    refs = set()
    # Match dbo.functionname( calls
    for m in re.finditer(r'\b((?:\w+\.)\w+)\s*\(', sql, re.IGNORECASE):
        candidate = m.group(1).lower()
        if candidate in all_names:
            refs.add(candidate)
    return refs


def topological_sort(nodes: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm — returns nodes in deployment order."""
    in_degree = {n: len(deps.get(n, set())) for n in nodes}
    queue = sorted([n for n in nodes if in_degree[n] == 0])
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for other in nodes:
            if node in deps.get(other, set()):
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)
                    queue.sort()

    if len(result) < len(nodes):
        # Cycle — append remainder
        result.extend(n for n in nodes if n not in result)

    return result


def deploy_functions(work_dir: Path, spg_service: str, dry_run: bool = False) -> dict:
    """Deploy functions from wave_3_functions_fixed/ in dependency order."""
    import psycopg2

    input_dir = work_dir / "conversion" / "postgres" / "wave_3_functions_fixed"
    if not input_dir.exists():
        # Fall back to wave_3_functions (unfixed)
        input_dir = work_dir / "conversion" / "postgres" / "wave_3_functions"

    func_files = sorted(input_dir.glob("*.sql"))
    if not func_files:
        print("No function files found.")
        return {}

    # ── 1. Parse function names ───────────────────────────────────────────
    functions: dict[str, dict] = {}  # name → {sql, file}
    for f in func_files:
        sql = f.read_text(encoding="utf-8")
        name = _extract_function_name(sql)
        if not name:
            print(f"  WARN: could not parse function name from {f.name}")
            continue
        # Allow multiple overloads (same name, different params) by using file as key
        functions[f.name] = {"sql": sql, "name": name, "file": f.name}

    all_names = {v["name"] for v in functions.values()}

    # ── 2. Build dependency graph ─────────────────────────────────────────
    deps: dict[str, set[str]] = {}
    for file_key, info in functions.items():
        refs = _extract_called_functions(info["sql"], all_names)
        refs.discard(info["name"])  # no self-reference
        deps[file_key] = {
            fk for fk, v in functions.items()
            if v["name"] in refs and fk != file_key
        }

    # ── 3. Topological sort ───────────────────────────────────────────────
    ordered = topological_sort(list(functions.keys()), deps)

    dep_info = [(k, deps[k]) for k in ordered if deps.get(k)]
    if dep_info:
        print("Function dependency order:")
        for k, d in dep_info:
            names = ", ".join(functions[dk]["name"] for dk in d)
            print(f"  {functions[k]['name']} depends on → {names}")
        print()

    # ── 4. Deploy ─────────────────────────────────────────────────────────
    if dry_run:
        print("DRY-RUN:")
        for k in ordered:
            print(f"  WOULD DEPLOY  {functions[k]['name']}")
        return {"dry_run": True}

    conn = psycopg2.connect(f"service={spg_service}")
    conn.autocommit = False
    results = {"succeeded": [], "failed": []}

    for file_key in ordered:
        info = functions[file_key]
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(info["sql"])
            results["succeeded"].append(info["name"])
            print(f"  OK    {info['name']}")
        except Exception as e:
            conn.rollback()
            err = str(e).replace("\n", " ").strip()
            results["failed"].append({"function": info["name"], "file": info["file"], "error": err})
            print(f"  FAIL  {info['name']}: {err}")

    conn.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="Deploy fixed functions to SPG in dependency order")
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--spg-service", required=True,
                        help="pg_service name from ~/.pg_service.conf")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and order but do not execute SQL")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    results = deploy_functions(work_dir, args.spg_service, dry_run=args.dry_run)

    if args.dry_run:
        return

    report_path = work_dir / "conversion" / "functions_deploy_report.json"
    report_path.write_text(json.dumps(results, indent=2))

    print(f"\n{'='*60}")
    print(f"Deployed OK : {len(results.get('succeeded', []))}")
    print(f"Failed      : {len(results.get('failed', []))}")
    print(f"Report      : {report_path}")

    if results.get("failed"):
        print("\nFailed functions:")
        for item in results["failed"]:
            print(f"  {item['function']}: {item['error'][:100]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
