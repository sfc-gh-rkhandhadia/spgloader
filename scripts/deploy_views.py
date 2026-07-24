#!/usr/bin/env python3
"""
deploy_views.py — Deploy fixed view SQL files to SPG in dependency order.

Reads fixed view files from wave_2_views_fixed/, auto-detects view-to-view
dependencies, deploys in topological order, and writes a deploy report.

Usage:
  python deploy_views.py --work-dir ~/.spgloader/20260101_120000 \\
                         --spg-service <pg_service_name>
"""
import argparse
import json
import re
import sys
from pathlib import Path


def _extract_view_name(sql: str) -> str | None:
    """Extract the fully-qualified view name from a CREATE OR REPLACE VIEW statement."""
    m = re.search(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(\S+)", sql, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).lower().rstrip("(").strip('"')
    # Normalise: dbo.view_name → dbo.view_name
    return name


def _extract_view_refs(sql: str, all_names: set[str]) -> set[str]:
    """Find references to other views in this view's SQL body.

    Scans FROM and JOIN clauses for names that match known view names.
    """
    refs = set()
    # Match schema.name patterns in FROM / JOIN
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+((?:\w+\.)\w+)", sql, re.IGNORECASE):
        candidate = m.group(1).lower()
        if candidate in all_names:
            refs.add(candidate)
    return refs


def topological_sort(nodes: list[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm — returns nodes in deployment order (dependencies first)."""
    in_degree = {n: 0 for n in nodes}
    for n in nodes:
        for dep in edges.get(n, set()):
            in_degree[n] = in_degree.get(n, 0) + 1

    # Rebuild: in_degree[node] = number of dependencies it has
    in_degree = {n: len(edges.get(n, set())) for n in nodes}
    queue = sorted([n for n in nodes if in_degree[n] == 0])
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        # Find nodes that depended on this one
        for other in nodes:
            if node in edges.get(other, set()):
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)
                    queue.sort()

    if len(result) < len(nodes):
        # Cycle detected — append remaining nodes (will fail gracefully at deploy time)
        remaining = [n for n in nodes if n not in result]
        result.extend(remaining)

    return result


def deploy_views(work_dir: Path, spg_service: str, dry_run: bool = False) -> dict:
    """Deploy views from wave_2_views_fixed/ in dependency order."""
    import psycopg2

    input_dir = work_dir / "conversion" / "postgres" / "wave_2_views_fixed"
    if not input_dir.exists():
        print(f"ERROR: directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    view_files = sorted(input_dir.glob("*.sql"))
    if not view_files:
        print("No view files found.")
        return {}

    # ── 1. Parse view names and SQL ────────────────────────────────────────
    views: dict[str, dict] = {}  # view_name → {sql, file}
    skip_files = []

    for f in view_files:
        sql = f.read_text(encoding="utf-8")
        if sql.lstrip().startswith("-- FIX-REQUIRED:"):
            skip_files.append(f.name)
            continue
        name = _extract_view_name(sql)
        if not name:
            print(f"  WARN: could not parse view name from {f.name}, skipping")
            skip_files.append(f.name)
            continue
        views[name] = {"sql": sql, "file": f.name}

    print(f"Views to deploy : {len(views)}")
    if skip_files:
        print(f"Skipped (needs manual fix): {len(skip_files)}")
        for s in skip_files:
            print(f"  SKIP  {s}")
    print()

    # ── 2. Build dependency graph ──────────────────────────────────────────
    all_names = set(views.keys())
    # deps[view_name] = set of view names it depends on
    deps: dict[str, set[str]] = {}
    for name, info in views.items():
        refs = _extract_view_refs(info["sql"], all_names)
        refs.discard(name)  # no self-references
        deps[name] = refs

    # ── 3. Topological sort ────────────────────────────────────────────────
    ordered = topological_sort(list(views.keys()), deps)

    dep_info = [(n, deps.get(n, set())) for n in ordered if deps.get(n)]
    if dep_info:
        print("View dependency order (selected):")
        for n, d in dep_info:
            print(f"  {n} depends on → {', '.join(sorted(d))}")
        print()

    # ── 4. Deploy ──────────────────────────────────────────────────────────
    if dry_run:
        print("DRY-RUN mode — SQL will not be executed")
        for name in ordered:
            print(f"  WOULD DEPLOY  {name}")
        return {"dry_run": True, "ordered": ordered}

    conn = psycopg2.connect(f"service={spg_service}")
    conn.autocommit = False

    results = {"succeeded": [], "failed": [], "skipped": skip_files}

    for name in ordered:
        info = views[name]
        sql = info["sql"]
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
            results["succeeded"].append(name)
            print(f"  OK    {name}")
        except Exception as e:
            conn.rollback()
            err = str(e).replace("\n", " ").strip()
            results["failed"].append({"view": name, "file": info["file"], "error": err})
            print(f"  FAIL  {name}: {err}")

    conn.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="Deploy fixed views to SPG in dependency order")
    parser.add_argument(
        "--work-dir", required=True,
        help="spgloader workspace directory (e.g. ~/.spgloader/20260101_120000)",
    )
    parser.add_argument(
        "--spg-service", required=True,
        help="pg_service name from ~/.pg_service.conf",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and order views but do not execute any SQL",
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    results = deploy_views(work_dir, args.spg_service, dry_run=args.dry_run)

    if args.dry_run:
        return

    # Write deploy report
    report_path = work_dir / "conversion" / "deploy_report.json"
    report_path.write_text(json.dumps(results, indent=2))

    print(f"\n{'='*60}")
    print(f"Deployed OK     : {len(results.get('succeeded', []))}")
    print(f"Failed          : {len(results.get('failed', []))}")
    print(f"Skipped         : {len(results.get('skipped', []))}")
    print(f"Deploy report   : {report_path}")

    if results.get("failed"):
        print("\nFailed views:")
        for item in results["failed"]:
            print(f"  {item['view']}: {item['error'][:120]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
