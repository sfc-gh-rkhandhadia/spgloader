"""SPG deployment logic — psycopg2-based DDL deploy via ~/.pg_service.conf."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def get_connection(service_name: str):
    import psycopg2
    try:
        conn = psycopg2.connect(f"service={service_name}")
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"SPG connection failed for service '{service_name}': {e}", file=sys.stderr)
        sys.exit(1)


def test_connection(service_name: str) -> None:
    conn = get_connection(service_name)
    cur = conn.cursor()
    cur.execute("SELECT version();")
    version = cur.fetchone()[0]
    conn.close()
    print(f"Connected to SPG ({service_name}): {version}")


def count_tables(dep_graph_path: str, service_name: str) -> dict[str, int | str]:
    conn = get_connection(service_name)
    cur = conn.cursor()
    dep_graph = json.loads(Path(dep_graph_path).read_text())
    tables = [o for o in dep_graph["ordered_objects"] if o["type"] == "table"]
    counts: dict[str, int | str] = {}
    for obj in tables:
        schema = obj.get("schema", "")
        name = obj["name"]
        try:
            q = f'SELECT COUNT(*) FROM "{schema}"."{name}"' if schema else f'SELECT COUNT(*) FROM "{name}"'
            cur.execute(q)
            counts[obj["fqn"]] = cur.fetchone()[0]
        except Exception as e:
            counts[obj["fqn"]] = f"ERROR: {e}"
            conn.rollback()
    conn.close()
    return counts


def deploy(
    dep_graph_path: str,
    converted_dir: str,
    conversion_manifest_path: str,
    service_name: str,
) -> dict:
    dep_graph = json.loads(Path(dep_graph_path).read_text())
    manifest = json.loads(Path(conversion_manifest_path).read_text())
    conv_dir = Path(converted_dir)
    catalog_tables = set(manifest.get("catalog_tables", manifest.get("pgloader_tables", [])))
    fqn_to_file = {e["fqn"]: e["output_file"] for e in manifest.get("converted_objects", [])}

    conn = get_connection(service_name)
    results = []

    for obj in dep_graph["ordered_objects"]:
        fqn = obj["fqn"]
        obj_type = obj["type"]

        if fqn in catalog_tables:
            results.append({"fqn": fqn, "type": obj_type, "status": "SKIPPED",
                            "reason": "catalog (parallel_deploy — schema already deployed)"})
            print(f"  SKIPPED  {fqn} (catalog)")
            continue

        rel_path = fqn_to_file.get(fqn)
        if not rel_path:
            schema = obj.get("schema", "")
            name = obj["name"]
            candidate = conv_dir / f"{schema}__{name}__{obj_type}.sql"
            rel_path = str(candidate) if candidate.exists() else None

        if not rel_path:
            results.append({"fqn": fqn, "type": obj_type, "status": "SKIPPED",
                            "reason": "no converted DDL file found"})
            print(f"  SKIPPED  {fqn} (no DDL file)")
            continue

        ddl_path = Path(rel_path) if Path(rel_path).is_absolute() else conv_dir / rel_path
        if not ddl_path.exists():
            results.append({"fqn": fqn, "type": obj_type, "status": "SKIPPED",
                            "reason": f"file not found: {ddl_path}"})
            continue

        cur = conn.cursor()
        try:
            cur.execute(ddl_path.read_text(encoding="utf-8"))
            conn.commit()
            results.append({"fqn": fqn, "type": obj_type, "status": "SUCCESS"})
            print(f"  SUCCESS  {fqn}")
        except Exception as e:
            conn.rollback()
            results.append({"fqn": fqn, "type": obj_type, "status": "FAILED",
                            "error": str(e).strip()})
            print(f"  FAILED   {fqn}: {e}")

    conn.close()
    return {
        "total": len(results),
        "succeeded": sum(1 for r in results if r["status"] == "SUCCESS"),
        "failed": sum(1 for r in results if r["status"] == "FAILED"),
        "skipped": sum(1 for r in results if r["status"] == "SKIPPED"),
        "objects": results,
    }
