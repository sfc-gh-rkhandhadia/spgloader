#!/usr/bin/env python3
"""
deploy_views.py — Deploy fixed view SQL files to SPG in dependency order.

Reads fixed view files from wave_2_views_fixed/, auto-detects view-to-view
dependencies, deploys in topological order, and writes a deploy report.

Generic behaviour (no project-specific hardcoding):
  - Target schema is auto-detected from ddl_objects.json in the workspace.
  - search_path is set automatically in SPG so unqualified table refs resolve.
  - When a view fails with "relation X does not exist", the script adds the
    schema prefix to X and retries automatically.

Usage:
  python deploy_views.py --work-dir ~/.spgloader/20260101_120000 \\
                         --spg-service <pg_service_name>
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
try:
    from spgloader.migration_state import MigrationState, PostconditionError
except ImportError:
    MigrationState = None
    PostconditionError = RuntimeError


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _detect_source_schema(work_dir: Path) -> str:
    """Infer the source schema name from ddl_objects.json.

    Reads the first non-system schema encountered in the workspace's extracted
    object list.  Returns 'public' if the file is absent or no schema is found.
    """
    ddl_path = work_dir / "ddl_objects.json"
    if not ddl_path.exists():
        return "public"
    try:
        data = json.loads(ddl_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("objects", [])
    except Exception:
        return "public"
    skip = {"", "sys", "information_schema", "guest", "public"}
    for obj in data:
        raw = obj.get("schema", "")
        schema = raw.strip("[").rstrip("]").lower()
        if schema and schema not in skip:
            return schema
    return "public"


def _get_spg_tables(conn, schema: str) -> set[str]:
    """Return the set of table and view names in *schema* from SPG."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s",
            (schema,),
        )
        return {row[0].lower() for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# View SQL helpers
# ---------------------------------------------------------------------------

def _extract_view_name(sql: str, filename: str = "") -> str | None:
    """Extract the fully-qualified view name from a CREATE [OR REPLACE] VIEW.

    Handles quoted identifiers with spaces, e.g.:
      CREATE OR REPLACE VIEW dbo."order details extended" AS
      CREATE OR REPLACE VIEW "products above average price" AS

    Falls back to parsing schema.name from the filename when the SQL header
    cannot be matched — e.g. unconverted MySQL ALGORITHM=.../DEFINER=... views.
    Filename convention: schema__viewname.sql → schema.viewname
    """
    # Primary: match CREATE [OR REPLACE] VIEW (handles both PG and residual MySQL headers)
    m = re.search(
        r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+((?:\w+\.)?(?:"[^"]+"|[\w]+))',
        sql, re.IGNORECASE
    )
    if m:
        name = m.group(1).lower()
        if '.' in name:
            schema, obj = name.split('.', 1)
            obj = obj.strip('"')
            return f"{schema}.{obj}"
        return name.strip('"')
    # Fallback: derive from filename (schema__viewname.sql → schema.viewname)
    if filename:
        stem = Path(filename).stem  # e.g. "udr__stats_temp_view"
        if "__" in stem:
            schema, obj = stem.split("__", 1)
            return f"{schema}.{obj}"
        return stem or None
    return None


def _extract_view_refs(sql: str, all_names: set[str]) -> set[str]:
    """Find references to other views in this view's SQL body."""
    refs = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+((?:\w+\.)\w+)", sql, re.IGNORECASE):
        candidate = m.group(1).lower()
        if candidate in all_names:
            refs.add(candidate)
    return refs


def _add_schema_prefix(sql: str, table: str, schema: str) -> str:
    """Add schema. prefix to every unqualified FROM/JOIN reference to *table*."""
    pattern = rf"(?<!\.)(\b(?:FROM|JOIN)\s+)(?i:{re.escape(table)})\b"
    def _repl(m: re.Match) -> str:
        kw = m.group(1)
        return f"{kw}{schema}.{table}"
    return re.sub(pattern, _repl, sql, flags=re.IGNORECASE)


def _try_schema_prefix_fix(sql: str, err_msg: str, schema: str,
                            spg_tables: set[str]) -> str | None:
    """If error is 'relation X does not exist' and X is in SPG, return sql with prefix added."""
    m = re.search(r'relation "?(\w+)"? does not exist', err_msg, re.IGNORECASE)
    if not m:
        return None
    tbl = m.group(1).lower()
    if tbl not in spg_tables:
        return None
    fixed = _add_schema_prefix(sql, tbl, schema)
    return fixed if fixed != sql else None


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

def topological_sort(nodes: list[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm — returns nodes in deployment order (dependencies first)."""
    in_degree = {n: len(edges.get(n, set())) for n in nodes}
    queue = sorted([n for n in nodes if in_degree[n] == 0])
    result = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for other in nodes:
            if node in edges.get(other, set()):
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)
                    queue.sort()
    if len(result) < len(nodes):
        result.extend(n for n in nodes if n not in result)
    return result


# ---------------------------------------------------------------------------
# Main deploy
# ---------------------------------------------------------------------------

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

    # Detect source schema from workspace (generic — works for any source DB)
    schema = _detect_source_schema(work_dir)
    print(f"Source schema   : {schema}")

    # ── 1. Parse view names and SQL ────────────────────────────────────────
    views: dict[str, dict] = {}
    skip_files = []
    failed_files: list[dict] = []

    for f in view_files:
        sql = f.read_text(encoding="utf-8")
        if sql.lstrip().startswith("-- FIX-REQUIRED:"):
            skip_files.append(f.name)
            continue
        name = _extract_view_name(sql, filename=f.name)
        if not name:
            print(f"  ERROR: could not parse view name from {f.name} — adding to failed list")
            failed_files.append({"view": f.stem, "file": str(f),
                                  "error": "Could not parse view name (unconverted DDL header)"})
            continue
        views[name] = {"sql": sql, "file": f.name}

    print(f"Views to deploy : {len(views)}")
    if skip_files:
        print(f"Skipped         : {len(skip_files)}")
    print()

    # ── 2. Build dependency graph ──────────────────────────────────────────
    all_names = set(views.keys())
    deps: dict[str, set[str]] = {}
    for name, info in views.items():
        refs = _extract_view_refs(info["sql"], all_names)
        refs.discard(name)
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

    # For multi-database migrations (schema__name.sql), each view may belong to
    # a different schema.  Collect all unique schemas from the view FQNs so we
    # can set a broad search_path that covers all of them.
    view_schemas = sorted({v.split('.')[0] for v in views if '.' in v})
    if not view_schemas:
        view_schemas = [schema]
    search_path_str = ", ".join(view_schemas) + ", public"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('search_path', %s, false)",
            (search_path_str,),
        )
    conn.commit()
    print(f"search_path set : {search_path_str}\n")

    # Fetch the full list of tables/views in the target schema for auto-prefix retry
    spg_tables = _get_spg_tables(conn, schema)

    results = {"succeeded": [], "failed": failed_files, "skipped": skip_files,
               "auto_fixed": []}

    for name in ordered:
        info = views[name]
        sql = info["sql"]

        for attempt in range(2):
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                # Always store as schema-qualified FQN so html_report shows the schema column correctly
                fqn = name if "." in name else f"{schema}.{name}"
                results["succeeded"].append(fqn)
                if attempt > 0:
                    results["auto_fixed"].append(fqn)
                    print(f"  OK    {name}  [auto-prefixed]")
                else:
                    print(f"  OK    {name}")
                break
            except Exception as e:
                conn.rollback()
                err = str(e).replace("\n", " ").strip()
                if attempt == 0:
                    # Try auto-fixing by adding schema prefix to the missing relation
                    fixed = _try_schema_prefix_fix(sql, err, schema, spg_tables)
                    if fixed:
                        sql = fixed
                        continue  # retry with prefixed SQL
                results["failed"].append({
                    "view": name, "file": info["file"], "error": err
                })
                print(f"  FAIL  {name}: {err}")

    conn.close()
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Deploy fixed views to SPG in dependency order"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--spg-service", required=True,
                        help="pg_service name from ~/.pg_service.conf")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and order views but do not execute any SQL")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    results = deploy_views(work_dir, args.spg_service, dry_run=args.dry_run)

    if args.dry_run:
        return

    report_path = work_dir / "conversion" / "deploy_report.json"
    report_path.write_text(json.dumps(results, indent=2))

    # ── Write to canonical migration_state.json ────────────────────────────
    if MigrationState is not None:
        try:
            wave_dir = work_dir / "conversion" / "postgres" / "wave_2_views_fixed"
            state = MigrationState(work_dir)
            # Normalise failed list to {fqn, error} dicts
            failed_norm = [
                {"fqn": f["view"] if isinstance(f, dict) else f,
                 "error": f.get("error", "") if isinstance(f, dict) else ""}
                for f in results.get("failed", [])
            ]
            state.record_deploy_phase(
                "views",
                succeeded=results.get("succeeded", []),
                failed=failed_norm,
                skipped=results.get("skipped", []),
                wave_dir=wave_dir,
                strict_postcondition=True,
            )
        except PostconditionError as exc:
            print(f"\n{'!'*60}")
            print(f"POSTCONDITION FAILURE: {exc}")
            print(f"{'!'*60}")
            print("The deploy report has been written but the migration_state.json")
            print("was NOT updated. Fix the unaccounted files before regenerating")
            print("the migration report.")
            # Do not sys.exit — the deploy itself may have succeeded
    # ──────────────────────────────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"Deployed OK     : {len(results.get('succeeded', []))}")
    if results.get("auto_fixed"):
        print(f"  (auto-prefixed): {len(results['auto_fixed'])}")
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
