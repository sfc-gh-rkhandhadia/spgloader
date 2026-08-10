#!/usr/bin/env python3
"""
parallel_deploy.py — Deploy a catalog-generated schema to SPG in 5 parallel phases.

Phases (in order):
  1. CREATE SCHEMA          — sequential, usually < 10 schemas
  2. CREATE SEQUENCE        — sequential, few objects
  3. CREATE TABLE           — parallel (N workers), no FKs yet
  4. CREATE INDEX           — parallel (N workers), after tables exist
  5. ALTER TABLE ADD FK     — parallel (N workers), after all tables exist

Usage:
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parallel_deploy.py \\
        --source-type  mssql \\
        --source-host  <host> \\
        --source-port  1433 \\
        --source-db    <database> \\
        --source-user  <user> \\
        --password-env MSSQL_SA_PASSWORD \\
        --spg-service  pg_spgloader_migration \\
        --workers      8 \\
        [--schema-only]   # skip data migration (default for schema phase)
        [--output <path>] # deployment_summary.json path
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))

import psycopg2
from spgloader.connectors import get_connector
from spgloader.conversion.pg_generator import generate_ddl


# ---------------------------------------------------------------------------
# Worker function (one connection per thread)
# ---------------------------------------------------------------------------

def _exec_statement(task: dict) -> dict:
    """Execute a single DDL statement in its own connection.  Thread-safe."""
    spg_service = task["spg_service"]
    phase       = task["phase"]
    label       = task["label"]
    sql         = task["sql"]

    start = time.monotonic()
    try:
        conn = psycopg2.connect(f"service={spg_service}")
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        conn.close()
        elapsed = time.monotonic() - start
        return {"phase": phase, "label": label, "ok": True, "elapsed": elapsed}
    except Exception as e:
        elapsed = time.monotonic() - start
        err = str(e).strip().split("\n")[0]
        return {"phase": phase, "label": label, "ok": False, "error": err, "elapsed": elapsed}


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _run_sequential(phase: str, stmts: list[str], spg_service: str,
                    labels: list[str] | None = None) -> list[dict]:
    """Run statements sequentially on a single connection."""
    results = []
    conn = psycopg2.connect(f"service={spg_service}")
    for i, sql in enumerate(stmts):
        label = (labels[i] if labels and i < len(labels) else f"{phase}[{i}]")
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
            results.append({"phase": phase, "label": label, "ok": True})
        except Exception as e:
            err = str(e).strip().split("\n")[0]
            results.append({"phase": phase, "label": label, "ok": False, "error": err})
    conn.close()
    return results


def _run_parallel(phase: str, tasks: list[dict], workers: int) -> list[dict]:
    """Run tasks in parallel using a thread pool."""
    results = []
    done = 0
    total = len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_exec_statement, t): t["label"] for t in tasks}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            results.append(result)
            if result["ok"]:
                if done % 100 == 0 or done == total:
                    print(f"  [{done}/{total}] {result['label']}: OK ({result['elapsed']:.2f}s)")
            else:
                print(f"  FAIL  {result['label']}: {result['error']}")
    return results


# ---------------------------------------------------------------------------
# Main deploy orchestrator
# ---------------------------------------------------------------------------

def _install_extensions(spg_service: str, ext_sql_path: Path) -> None:
    """Run pre_deploy_extensions.sql against SPG if the file exists and is non-empty."""
    if not ext_sql_path.exists():
        return
    sql_text = ext_sql_path.read_text().strip()
    if not sql_text:
        return
    # Strip comment lines BEFORE splitting by semicolon.
    # If we filter after splitting, the first "statement" includes all leading
    # comments PLUS the first real statement joined together — and the startswith("--")
    # check drops the whole chunk, silently skipping the first extension (e.g. ltree).
    clean_lines = [l for l in sql_text.splitlines() if not l.strip().startswith("--")]
    clean_sql = "\n".join(clean_lines)
    stmts = [s.strip() for s in clean_sql.split(";") if s.strip()]
    if not stmts:
        return
    print(f"\nInstalling extension prerequisites ({len(stmts)} statement(s)) ...")
    conn = psycopg2.connect(f"service={spg_service}")
    with conn:
        with conn.cursor() as cur:
            for stmt in stmts:
                try:
                    cur.execute(stmt)
                    print(f"  OK  {stmt[:60]}")
                except Exception as e:
                    print(f"  WARN  {stmt[:60]}: {e}")
    conn.close()


def deploy(
    source_type: str,
    source_host: str,
    source_port: int,
    source_db: str,
    source_user: str,
    source_password: str,
    spg_service: str,
    workers: int = 8,
    output_path: str | None = None,
    work_dir: str | None = None,
) -> dict:
    """
    Run all 5 deployment phases.  Returns a summary dict.
    """
    total_start = time.monotonic()
    all_results: list[dict] = []

    # ------------------------------------------------------------------
    # Step 1: Extract schema model from source catalog
    # ------------------------------------------------------------------
    print(f"\nExtracting catalog from {source_type} @ {source_host}:{source_port}/{source_db} ...")
    connector = get_connector(
        source_type=source_type,
        host=source_host,
        port=source_port,
        database=source_db,
        user=source_user,
        password=source_password,
    )
    schema_model = connector.catalog_extract()

    n_schemas  = len(schema_model.get("schemas", []))
    n_seqs     = len(schema_model.get("sequences", []))
    n_tables   = len(schema_model.get("tables", []))
    n_indexes  = len(schema_model.get("indexes", []))
    n_fks      = len(schema_model.get("foreign_keys", []))

    print(f"  Schemas: {n_schemas}  Sequences: {n_seqs}  Tables: {n_tables}"
          f"  Indexes: {n_indexes}  FKs: {n_fks}")

    # ------------------------------------------------------------------
    # Step 2: Generate PostgreSQL DDL
    # ------------------------------------------------------------------
    print("Generating PostgreSQL DDL ...")
    ddl = generate_ddl(schema_model, source_type)

    # ------------------------------------------------------------------
    # Pre-deploy: install extension prerequisites (ltree, postgis, etc.)
    # Auto-detected from assessment/pre_deploy_extensions.sql when
    # --work-dir is provided or derivable from --output path.
    # ------------------------------------------------------------------
    _wd = work_dir or (str(Path(output_path).parent.parent) if output_path else None)
    if _wd:
        _ext_path = Path(_wd) / "assessment" / "pre_deploy_extensions.sql"
        _install_extensions(spg_service, _ext_path)

    # ------------------------------------------------------------------
    # Phase 1: Schemas (sequential)
    # ------------------------------------------------------------------
    print(f"\n[Phase 1/5] Creating {n_schemas} schema(s) ...")
    r1 = _run_sequential("schemas", ddl["schemas"], spg_service)
    all_results.extend(r1)
    _print_phase_summary("schemas", r1)

    # ------------------------------------------------------------------
    # Phase 2: Sequences (sequential)
    # ------------------------------------------------------------------
    print(f"\n[Phase 2/5] Creating {n_seqs} sequence(s) ...")
    seq_labels = [f"{s['schema']}.{s['name']}" for s in schema_model.get("sequences", [])]
    r2 = _run_sequential("sequences", ddl["sequences"], spg_service, labels=seq_labels)
    all_results.extend(r2)
    _print_phase_summary("sequences", r2)

    # ------------------------------------------------------------------
    # Phase 3: Tables (parallel)
    # ------------------------------------------------------------------
    print(f"\n[Phase 3/5] Creating {n_tables} table(s) with {workers} workers ...")
    table_tasks = [
        {
            "spg_service": spg_service,
            "phase": "tables",
            "label": f"{t['schema'].lower()}.{t['name'].lower()}",
            "sql":   sql,
        }
        for t, sql in zip(schema_model.get("tables", []), ddl["tables"])
    ]
    r3 = _run_parallel("tables", table_tasks, workers)
    all_results.extend(r3)
    _print_phase_summary("tables", r3)

    # ------------------------------------------------------------------
    # Phase 4: Indexes (parallel)
    # ------------------------------------------------------------------
    print(f"\n[Phase 4/5] Creating {n_indexes} index(es) with {workers} workers ...")
    index_tasks = [
        {
            "spg_service": spg_service,
            "phase": "indexes",
            "label": f"{ix['schema'].lower()}.{ix['name'].lower()}",
            "sql":   sql,
        }
        for ix, sql in zip(schema_model.get("indexes", []), ddl["indexes"])
    ]
    r4 = _run_parallel("indexes", index_tasks, workers)
    all_results.extend(r4)
    _print_phase_summary("indexes", r4)

    # ------------------------------------------------------------------
    # Phase 5: Foreign keys (parallel, all tables guaranteed to exist)
    # ------------------------------------------------------------------
    print(f"\n[Phase 5/5] Creating {n_fks} foreign key(s) with {workers} workers ...")
    fk_tasks = [
        {
            "spg_service": spg_service,
            "phase": "foreign_keys",
            "label": (f"{fk.get('from_schema', '').lower()}.{fk['name'].lower()}"
                      if fk.get("from_schema") else fk["name"].lower()),
            "sql":   sql,
        }
        for fk, sql in zip(schema_model.get("foreign_keys", []), ddl["foreign_keys"])
    ]
    r5 = _run_parallel("foreign_keys", fk_tasks, workers)
    all_results.extend(r5)
    _print_phase_summary("foreign_keys", r5)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total_elapsed = time.monotonic() - total_start
    succeeded = [r for r in all_results if r["ok"]]
    failed    = [r for r in all_results if not r["ok"]]

    summary = {
        "source_type": source_type,
        "source_db":   source_db,
        "spg_service": spg_service,
        "workers":     workers,
        "elapsed_s":   round(total_elapsed, 2),
        "phases": {
            "schemas":      _phase_counts(r1),
            "sequences":    _phase_counts(r2),
            "tables":       _phase_counts(r3),
            "indexes":      _phase_counts(r4),
            "foreign_keys": _phase_counts(r5),
        },
        "total_ok":   len(succeeded),
        "total_fail": len(failed),
        "failures":   [
            {"label": r["label"], "phase": r["phase"], "error": r.get("error", "")}
            for r in failed
        ],
    }

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json.dumps(summary, indent=2))
        _merge_canonical_summary(summary, Path(output_path), work_dir=work_dir)
        # Contract validation — runs automatically, raises on violation
        from spgloader.workspace_validator import validate_after_deploy
        validate_after_deploy(Path(output_path).parent.parent)
        print(f"\nDeployment summary: {output_path}")

    print(f"\n{'='*60}")
    print(f"Catalog-based schema deployment complete in {total_elapsed:.1f}s")
    print(f"  OK:     {len(succeeded)}")
    print(f"  FAILED: {len(failed)}")
    if failed:
        print("\nFailures:")
        for r in failed:
            print(f"  [{r['phase']}] {r['label']}: {r.get('error','')}")
    print(f"{'='*60}")

    # Update object manifest with table deployment results
    try:
        _lib_dir = str(Path(__file__).parent.parent / "lib")
        if _lib_dir not in sys.path:
            sys.path.insert(0, _lib_dir)
        from spgloader.manifest import ObjectManifest
        # Determine workspace from output_path or source_db
        ws_dir = Path(output_path).parent.parent if output_path else None
        if ws_dir and (ws_dir / ".spgloader").exists():
            manifest = ObjectManifest(ws_dir)
            for r in all_results:
                if r.get("phase") == "tables":
                    fqn = r.get("label", "")
                    if fqn:
                        status = "completed" if r["ok"] else "failed"
                        manifest.set_deployed(fqn, status, error=r.get("error", "")[:200])
            manifest.save()
    except Exception:
        pass

    # ── Write to canonical migration_state.json ────────────────────────────
    try:
        _lib_dir2 = str(Path(__file__).parent.parent / "lib")
        if _lib_dir2 not in sys.path:
            sys.path.insert(0, _lib_dir2)
        from spgloader.migration_state import MigrationState
        ws_dir2 = Path(output_path).parent.parent if output_path else None
        if ws_dir2:
            state = MigrationState(ws_dir2)
            p = summary.get("phases", {})
            state.record_tables(
                schema=source_db,
                tables_ok=p.get("tables", {}).get("ok", 0),
                tables_fail=p.get("tables", {}).get("fail", 0),
                indexes_ok=p.get("indexes", {}).get("ok", 0),
                indexes_fail=p.get("indexes", {}).get("fail", 0),
                fk_ok=p.get("foreign_keys", {}).get("ok", 0),
                fk_fail=p.get("foreign_keys", {}).get("fail", 0),
                elapsed_s=summary.get("elapsed_s", 0.0),
            )
    except Exception:
        pass
    # ──────────────────────────────────────────────────────────────────────

    return summary


def _print_phase_summary(phase: str, results: list[dict]) -> None:
    ok   = sum(1 for r in results if r["ok"])
    fail = sum(1 for r in results if not r["ok"])
    print(f"  → {phase}: {ok} OK, {fail} failed")


def _phase_counts(results: list[dict]) -> dict:
    return {
        "ok":   sum(1 for r in results if r["ok"]),
        "fail": sum(1 for r in results if not r["ok"]),
    }


def _merge_canonical_summary(summary: dict, output_path: Path, work_dir: str | None = None) -> None:
    """Merge this run's phases into the canonical deployment_summary.json.

    parallel_deploy.py is invoked once per source database in a multi-DB
    migration, each time with its own --output (e.g.
    deployment/deployment_summary_<db>.json).  Without aggregation the canonical
    deployment/deployment_summary.json is never written, so the workspace
    contract (validate_after_deploy -> phases.{tables,indexes,foreign_keys}.ok)
    fails.  This merges per-run phase ok/fail totals into the canonical file so
    the aggregate always reflects every database.

    Canonical target resolution:
      1) $WORK_DIR/deployment/deployment_summary.json  (preferred, when work_dir is set)
      2) output_path.parent.parent / deployment / deployment_summary.json
    """
    candidates: list[Path] = []
    if work_dir:
        candidates.append(Path(work_dir) / "deployment" / "deployment_summary.json")
    # output_path convention: <ws>/deployment/deployment_summary_<db>.json
    candidates.append(output_path.parent.parent / "deployment" / "deployment_summary.json")
    # If the per-run output is itself the canonical name, do not merge over it.
    candidates = [p for p in candidates if p.resolve() != output_path.resolve()]
    if not candidates:
        return
    canonical = candidates[0]
    if not canonical.parent.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)

    agg: dict
    if canonical.exists():
        agg = json.loads(canonical.read_text())
    else:
        agg = {
            "source_type": summary.get("source_type"),
            "phases": {
                "schemas":      {"ok": 0, "fail": 0},
                "sequences":    {"ok": 0, "fail": 0},
                "tables":       {"ok": 0, "fail": 0},
                "indexes":      {"ok": 0, "fail": 0},
                "foreign_keys": {"ok": 0, "fail": 0},
            },
            "failures": [],
            "per_db": {},
        }
    agg.setdefault("phases", {})
    agg.setdefault("failures", [])
    agg.setdefault("per_db", {})

    # Sum phase ok/fail counts across all databases.
    for phase, counts in summary.get("phases", {}).items():
        agg_ph = agg["phases"].setdefault(phase, {"ok": 0, "fail": 0})
        agg_ph["ok"] = int(agg_ph.get("ok", 0)) + int(counts.get("ok", 0))
        agg_ph["fail"] = int(agg_ph.get("fail", 0)) + int(counts.get("fail", 0))

    # Track this DB's failures with its source_db tag for traceability.
    db = summary.get("source_db") or "unknown"
    for f in summary.get("failures", []):
        agg["failures"].append({**f, "source_db": db})

    # Keep per-db phase breakdown for reporting.
    agg["per_db"][db] = summary.get("phases", {})

    agg["source_type"] = agg.get("source_type") or summary.get("source_type")
    agg["total_ok"] = int(agg.get("total_ok", 0)) + int(summary.get("total_ok", 0))
    agg["total_fail"] = int(agg.get("total_fail", 0)) + int(summary.get("total_fail", 0))

    canonical.write_text(json.dumps(agg, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Catalog-based parallel schema deployment from MSSQL/MySQL/MariaDB/Oracle to SPG"
    )
    parser.add_argument("--source-type", required=True,
                        choices=["mssql", "mysql", "mariadb", "oracle"])
    parser.add_argument("--source-host", default="localhost")
    parser.add_argument("--source-port", type=int, default=None)
    parser.add_argument("--source-db",   required=True)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--password-env", required=True,
                        help="Name of env var holding the source DB password")
    parser.add_argument("--spg-service", required=True,
                        help="Service name in ~/.pg_service.conf")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel workers for tables/indexes/FKs (default: 8)")
    parser.add_argument("--output", default=None,
                        help="Path to write deployment_summary.json")
    parser.add_argument("--work-dir", default=None,
                        help="spgloader workspace directory (used to auto-install "
                             "assessment/pre_deploy_extensions.sql before table creation)")
    args = parser.parse_args()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"Error: env var '{args.password_env}' is not set.", file=sys.stderr)
        sys.exit(1)

    # Default ports
    default_ports = {"mssql": 1433, "mysql": 3306, "mariadb": 3306, "oracle": 1521}
    port = args.source_port or default_ports.get(args.source_type, 1433)

    summary = deploy(
        source_type=args.source_type,
        source_host=args.source_host,
        source_port=port,
        source_db=args.source_db,
        source_user=args.source_user,
        source_password=password,
        spg_service=args.spg_service,
        workers=args.workers,
        output_path=args.output,
        work_dir=args.work_dir,
    )

    sys.exit(0 if summary["total_fail"] == 0 else 1)


if __name__ == "__main__":
    main()
