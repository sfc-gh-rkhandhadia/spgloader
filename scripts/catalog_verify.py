"""
catalog_verify.py — Hybrid catalog verification for spgloader.

Produces catalog_verification.json by joining:
  1. ddl_objects.json            — original source names (original casing)
  2. _conversion_report.json     — source_fqn → ewi_codes bridge
  3. repair_report.json          — which objects were LLM-repaired
  4. *_deploy_report.json files  — error messages for failed objects
  5. Live SOURCE catalog         — column / parameter counts from source DB
  6. Live SPG pg_catalog         — ground truth: what's actually deployed

Output: $WORK_DIR/validation/catalog_verification.json

Usage:
    uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/catalog_verify.py \\
      --work-dir /path/to/spgloader/workspace      \\
      [--detailed-cols]   # include per-column name diff, not just count      \\
      [--output /path/to/catalog_verification.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))

import psycopg2
import psycopg2.extras
from spgloader.connectors import get_connector

# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

_BRACKET_RE = re.compile(r'[\[\]"]')


def _norm(fqn: str) -> str:
    """Normalise a source FQN to the SPG lowercase equivalent.

    Examples:
      "HumanResources.vEmployee"  →  "humanresources.vemployee"
      "[dbo].[MyTable]"           →  "dbo.mytable"
      "dbo.MyProc"                →  "dbo.myproc"
    """
    return _BRACKET_RE.sub("", fqn).lower()


def _base(fqn: str) -> str:
    """Return just the object name (no schema)."""
    return fqn.split(".")[-1]


# ---------------------------------------------------------------------------
# Build artifacts lookup maps from workspace JSON files
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _build_error_map(ws: Path) -> dict[str, str]:
    """Return {normalized_fqn: error_string} from all *_deploy_report.json files."""
    error_map: dict[str, str] = {}
    for report_name in (
        "deploy_report.json",
        "functions_deploy_report.json",
        "procedures_deploy_report.json",
    ):
        r = _load_json(ws / "conversion" / report_name)
        for item in r.get("failed", []):
            if not isinstance(item, dict):
                continue
            raw = item.get("view") or item.get("function") or item.get("procedure") or ""
            err = item.get("error", "")
            if raw:
                error_map[_norm(raw)] = err
    # deployment_summary.json failures (tables / indexes / fks)
    ds = _load_json(ws / "deployment" / "deployment_summary.json")
    for item in ds.get("failures", []):
        label = item.get("label", "")
        err   = item.get("error", "")
        if label:
            error_map[label.lower()] = err
    return error_map


def _build_repaired_set(ws: Path) -> set[str]:
    """Return set of normalized FQNs that were LLM or rule repaired."""
    r = _load_json(ws / "conversion" / "repair_report.json")
    repaired: set[str] = set()
    for name in r.get("fixed_llm", []) + r.get("fixed_rules", []):
        repaired.add(_norm(name) if isinstance(name, str) else "")
    return repaired


def _build_ewi_map(ws: Path) -> dict[str, list[str]]:
    """Return {normalized_source_fqn: [ewi_codes]} from _conversion_report.json."""
    cr = _load_json(ws / "conversion" / "_conversion_report.json")
    ewi_map: dict[str, list[str]] = {}
    for obj in cr.get("converted_objects", []):
        fqn = obj.get("fqn", "")
        if fqn:
            ewi_map[_norm(fqn)] = obj.get("ewi_codes", [])
    return ewi_map


def _load_source_objects(ws: Path) -> list[dict]:
    """Load ddl_objects.json — the canonical source inventory with original casing."""
    raw = _load_json(ws / "ddl_objects.json")
    if isinstance(raw, list):
        return raw
    return raw.get("objects", [])


# ---------------------------------------------------------------------------
# Source catalog queries
# ---------------------------------------------------------------------------

def _connector_type(connector) -> str:
    return connector.__class__.__name__.lower().replace("connector", "")


def _source_columns(connector, schema: str, name: str) -> list[str]:
    """Return lowercased column names for a table or view from the source DB."""
    conn = connector._connect()
    cur  = conn.cursor()
    ct   = _connector_type(connector)
    try:
        if "mssql" in ct:
            cur.execute(
                """
                SELECT c.name
                FROM sys.columns c
                JOIN sys.objects o ON o.object_id = c.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.name = %s
                  AND o.type IN ('U','V')
                  AND c.is_computed = 0
                ORDER BY c.column_id
                """,
                (schema, name),
            )
        elif "mysql" in ct or "mariadb" in ct:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (schema, name),
            )
        else:  # oracle
            cur.execute(
                "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
                "WHERE OWNER = :s AND TABLE_NAME = :t ORDER BY COLUMN_ID",
                s=schema.upper(), t=name.upper(),
            )
        return [r[0].lower() for r in cur.fetchall()]
    finally:
        conn.close()


def _source_params(connector, schema: str, name: str) -> list[str]:
    """Return parameter names for a function or procedure from the source DB."""
    conn = connector._connect()
    cur  = conn.cursor()
    ct   = _connector_type(connector)
    try:
        if "mssql" in ct:
            cur.execute(
                """
                SELECT p.name
                FROM sys.parameters p
                JOIN sys.objects o ON o.object_id = p.object_id
                JOIN sys.schemas s ON s.schema_id = o.schema_id
                WHERE s.name = %s AND o.name = %s AND p.parameter_id > 0
                ORDER BY p.parameter_id
                """,
                (schema, name),
            )
        elif "mysql" in ct or "mariadb" in ct:
            cur.execute(
                """
                SELECT PARAMETER_NAME
                FROM INFORMATION_SCHEMA.PARAMETERS
                WHERE SPECIFIC_SCHEMA = %s AND SPECIFIC_NAME = %s
                  AND PARAMETER_MODE IS NOT NULL
                ORDER BY ORDINAL_POSITION
                """,
                (schema, name),
            )
        else:  # oracle
            cur.execute(
                "SELECT ARGUMENT_NAME FROM ALL_ARGUMENTS "
                "WHERE OWNER = :s AND OBJECT_NAME = :n "
                "AND ARGUMENT_NAME IS NOT NULL ORDER BY POSITION",
                s=schema.upper(), n=name.upper(),
            )
        return [r[0].lower() if r[0] else "" for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SPG catalog queries (pg_catalog / information_schema)
# ---------------------------------------------------------------------------

def _spg_connect(spg_service: str) -> "psycopg2.connection":
    """Open a psycopg2 connection using the pg_service.conf entry."""
    return psycopg2.connect(f"service={spg_service}")


def _spg_columns(cur, schema: str, name: str) -> list[str]:
    """Return lowercased column names for a table or view from SPG."""
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, name),
    )
    return [r[0].lower() for r in cur.fetchall()]


def _spg_params(cur, schema: str, name: str) -> list[str]:
    """Return parameter names for a function or procedure in SPG."""
    cur.execute(
        """
        SELECT p.parameter_name
        FROM information_schema.routines r
        JOIN information_schema.parameters p
             ON p.specific_name = r.specific_name
            AND p.specific_schema = r.specific_schema
        WHERE r.routine_schema = %s AND r.routine_name = %s
          AND p.parameter_mode IS NOT NULL
        ORDER BY p.ordinal_position
        """,
        (schema, name),
    )
    return [r[0].lower() if r[0] else "" for r in cur.fetchall()]


def _spg_all_objects(cur) -> dict[str, set[str]]:
    """
    Return a dict keyed by object type with sets of 'schema.name' strings for
    everything deployed in SPG (excluding system schemas).

    Types returned: 'table', 'view', 'function', 'procedure'
    """
    system_schemas = ("pg_catalog", "information_schema", "pg_toast",
                      "pg_temp_1", "pg_toast_temp_1")

    result: dict[str, set[str]] = {
        "table":     set(),
        "view":      set(),
        "function":  set(),
        "procedure": set(),
        "trigger":   set(),
    }

    # Tables
    cur.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema NOT IN %s
        """,
        (system_schemas,),
    )
    for schema, name in cur.fetchall():
        result["table"].add(f"{schema}.{name}")

    # Views
    cur.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.views
        WHERE table_schema NOT IN %s
        """,
        (system_schemas,),
    )
    for schema, name in cur.fetchall():
        result["view"].add(f"{schema}.{name}")

    # Functions and procedures (pg_proc)
    cur.execute(
        """
        SELECT n.nspname, p.proname,
               CASE p.prokind WHEN 'f' THEN 'function'
                              WHEN 'p' THEN 'procedure'
                              WHEN 't' THEN 'trigger'
                              ELSE 'function' END AS kind
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN %s
        """,
        (system_schemas,),
    )
    for schema, name, kind in cur.fetchall():
        result.get(kind, result["function"]).add(f"{schema}.{name}")

    return result


# ---------------------------------------------------------------------------
# Core verification logic
# ---------------------------------------------------------------------------

def _col_diff(src_cols: list[str], tgt_cols: list[str]) -> dict:
    src_set, tgt_set = set(src_cols), set(tgt_cols)
    return {
        "only_in_source": sorted(src_set - tgt_set),
        "only_in_target": sorted(tgt_set - src_set),
    }


def _param_diff(src_params: list[str], tgt_params: list[str]) -> dict:
    src_set, tgt_set = set(src_params), set(tgt_params)
    return {
        "only_in_source": sorted(src_set - tgt_set),
        "only_in_target": sorted(tgt_set - src_set),
    }


def _determine_status(
    in_spg: bool,
    source_count: int,
    target_count: int,
    diff_only_src: list,
    diff_only_tgt: list,
) -> str:
    if not in_spg:
        return "missing"
    if diff_only_src or diff_only_tgt or source_count != target_count:
        return "col_mismatch"
    return "match"


def verify(
    ws: Path,
    connector,
    spg_service: str,
    detailed_cols: bool = False,
) -> dict:
    """
    Run full hybrid verification and return the catalog_verification dict.

    Parameters
    ----------
    ws           : workspace directory (contains ddl_objects.json etc.)
    connector    : source DB connector (already instantiated)
    spg_service  : SPG service name for pg_service.conf lookup
    detailed_cols: if True, include per-column name diff; if False, counts only
    """
    source_objects = _load_source_objects(ws)
    error_map      = _build_error_map(ws)
    repaired_set   = _build_repaired_set(ws)
    ewi_map        = _build_ewi_map(ws)

    # Identify source_type for connector detection
    source_type = _connector_type(connector)

    # Connect to SPG and get full object inventory + open cursor for detail queries
    spg_conn = _spg_connect(spg_service)
    spg_cur  = spg_conn.cursor()
    spg_objects = _spg_all_objects(spg_cur)

    # Flatten SPG object sets into a lookup: normalized_fqn → type
    spg_deployed: dict[str, str] = {}
    for obj_type, fqns in spg_objects.items():
        for fqn in fqns:
            spg_deployed[fqn] = obj_type

    objects: list[dict] = []
    summary: dict[str, int] = {}

    for src_obj in source_objects:
        src_fqn = src_obj.get("fqn") or src_obj.get("name") or ""
        if not src_fqn:
            continue
        obj_type = src_obj.get("type", "").lower()

        # Normalise to get the expected SPG name
        target_fqn_norm = _norm(src_fqn)
        parts = target_fqn_norm.split(".", 1)
        tgt_schema = parts[0] if len(parts) == 2 else "public"
        tgt_name   = parts[-1]
        src_schema = src_obj.get("schema", tgt_schema)
        src_name   = src_obj.get("name", tgt_name)

        in_spg = target_fqn_norm in spg_deployed

        # Look up error from deploy reports
        error = error_map.get(target_fqn_norm) or error_map.get(tgt_name)

        # EWI codes from conversion
        ewi_codes = ewi_map.get(target_fqn_norm, [])

        # Was this object repaired by LLM?
        llm_repaired = target_fqn_norm in repaired_set

        entry: dict = {
            "source_fqn":   src_fqn,
            "target_fqn":   target_fqn_norm if in_spg else None,
            "type":         obj_type,
            "llm_repaired": llm_repaired,
            "ewi_codes":    ewi_codes,
            "error":        error,
        }

        # Structural comparison: column counts for tables/views; params for funcs/procs
        if obj_type in ("table", "view"):
            src_cols = _source_columns(connector, src_schema, src_name)
            if in_spg:
                tgt_cols = _spg_columns(spg_cur, tgt_schema, tgt_name)
            else:
                tgt_cols = []

            diff = _col_diff(src_cols, tgt_cols) if in_spg else {
                "only_in_source": src_cols if detailed_cols else [],
                "only_in_target": [],
            }
            status = _determine_status(in_spg, len(src_cols), len(tgt_cols),
                                       diff["only_in_source"], diff["only_in_target"])
            entry.update({
                "status":           status,
                "source_col_count": len(src_cols),
                "target_col_count": len(tgt_cols),
                "col_diff": diff if detailed_cols else {
                    "only_in_source": diff["only_in_source"][:10],  # cap at 10 for brevity
                    "only_in_target": diff["only_in_target"][:10],
                },
            })

        elif obj_type in ("function", "procedure"):
            src_params = _source_params(connector, src_schema, src_name)
            if in_spg:
                tgt_params = _spg_params(spg_cur, tgt_schema, tgt_name)
            else:
                tgt_params = []

            diff = _param_diff(src_params, tgt_params)
            if not in_spg:
                status = "missing"
            elif diff["only_in_source"] or diff["only_in_target"]:
                status = "param_mismatch"
            else:
                status = "match"

            entry.update({
                "status":             status,
                "source_param_count": len(src_params),
                "target_param_count": len(tgt_params),
                "param_diff":         diff,
            })

        elif obj_type == "trigger":
            # Triggers deploy as PG trigger functions (name + _fn suffix)
            trig_fn_fqn = f"{tgt_schema}.{tgt_name}_fn"
            in_spg_as_fn = trig_fn_fqn in spg_deployed
            actual_in_spg = in_spg or in_spg_as_fn
            entry.update({
                "status":     "match" if actual_in_spg else "missing",
                "target_fqn": trig_fn_fqn if in_spg_as_fn and not in_spg else entry["target_fqn"],
            })
        else:
            entry["status"] = "match" if in_spg else "missing"

        objects.append(entry)

        # Accumulate summary counts
        k = f"{obj_type}s_{entry['status']}"
        summary[k] = summary.get(k, 0) + 1
        # Also count totals per type
        summary[f"{obj_type}s_total"] = summary.get(f"{obj_type}s_total", 0) + 1

    spg_cur.close()
    spg_conn.close()

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":       f"{source_type} @ {connector.host}:{connector.port}/{connector.database}",
        "target":       spg_service,
        "detailed_cols": detailed_cols,
        "summary":      summary,
        "objects":      objects,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hybrid catalog verification: compare source catalog vs SPG deployment."
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory (contains source_conn.env, target_conn.env)")
    parser.add_argument("--detailed-cols", action="store_true",
                        help="Include full per-column name diff (not just counts)")
    parser.add_argument("--output",
                        help="Output JSON path (default: <work-dir>/validation/catalog_verification.json)")
    args = parser.parse_args()

    ws = Path(args.work_dir).expanduser().resolve()

    # ── Load connection details from workspace env files ──────────────────
    def load_env(path: Path) -> dict[str, str]:
        env: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        return env

    src_env = load_env(ws / "source_conn.env")
    tgt_env = load_env(ws / "target_conn.env")

    source_type     = src_env.get("SOURCE_TYPE", "mssql")
    source_host     = src_env.get("SOURCE_HOST", "localhost")
    source_port     = int(src_env.get("SOURCE_PORT", 1433))
    source_db       = src_env.get("SOURCE_DATABASE", "")
    source_user     = src_env.get("SOURCE_USER", "sa")
    password_env    = src_env.get("SOURCE_PASSWORD_ENV", "MSSQL_SA_PASSWORD")
    source_password = os.environ.get(password_env, "")
    spg_service     = tgt_env.get("TARGET_SPG_SERVICE", "")

    if not source_password:
        print(
            f"ERROR: ${password_env} is not set.\n"
            f"  export {password_env}='your-password'  then retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not spg_service:
        print("ERROR: TARGET_SPG_SERVICE not found in target_conn.env", file=sys.stderr)
        sys.exit(1)

    # ── Test connections before running ──────────────────────────────────
    print(f"Testing source connection ({source_type} @ {source_host}:{source_port}/{source_db}) ...")
    connector = get_connector(source_type, source_host, source_port, source_db,
                              source_user, source_password)
    try:
        conn = connector._connect()
        conn.close()
        print("  Source: OK")
    except Exception as e:
        print(f"  Source: FAILED — {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Testing SPG connection ({spg_service}) ...")
    try:
        c = psycopg2.connect(f"service={spg_service}")
        c.close()
        print("  SPG: OK")
    except Exception as e:
        print(f"  SPG: FAILED — {e}", file=sys.stderr)
        print("  Is the SPG instance running? Resume it with:", file=sys.stderr)
        print(f"    snow sql -c <connection> -q \"ALTER POSTGRES INSTANCE {spg_service} RESUME;\"",
              file=sys.stderr)
        sys.exit(1)

    # ── Run verification ──────────────────────────────────────────────────
    print(f"\nRunning catalog verification ({ws.name}) ...")
    result = verify(ws, connector, spg_service, detailed_cols=args.detailed_cols)

    # ── Print summary ─────────────────────────────────────────────────────
    s = result["summary"]
    print("\nCatalog Verification Complete")
    print("=" * 40)
    for obj_t in ("table", "view", "function", "procedure", "trigger"):
        total   = s.get(f"{obj_t}s_total", 0)
        missing = s.get(f"{obj_t}s_missing", 0)
        mismatch= s.get(f"{obj_t}s_col_mismatch", s.get(f"{obj_t}s_param_mismatch", 0))
        matched = total - missing - mismatch
        if total:
            status_str = (f"{matched}/{total} match"
                          + (f"  |  {mismatch} mismatch" if mismatch else "")
                          + (f"  |  {missing} missing"   if missing   else ""))
            print(f"  {obj_t.capitalize()+'s':<12}: {status_str}")

    # ── Write output ──────────────────────────────────────────────────────
    out_path = Path(args.output) if args.output else ws / "validation" / "catalog_verification.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nCatalog verification: {out_path}")

    # ── Also write validation_report.json with checks for the Schema Verification tab ──
    _write_validation_checks(ws, result)

    # ── Contract validation — raises on violation ──
    from spgloader.workspace_validator import validate_after_catalog_verify
    validate_after_catalog_verify(ws)


def _write_validation_checks(ws: Path, catalog_result: dict) -> None:
    """Generate validation_report.json checks from catalog verification results.

    The Schema Verification tab in the migration report reads 'checks' from
    validation_report.json. Emits per-schema checks (with _schema tag) so the
    tab shows one row per schema rather than collapsing everything into the first
    schema name via the html_report.py fallback.
    """
    objects = catalog_result.get("objects", [])

    # Group objects by schema (lower-cased for consistency)
    by_schema: dict = {}
    for obj in objects:
        fqn = obj.get("source_fqn", "")
        parts = fqn.split(".")
        schema = parts[0].lower() if len(parts) >= 2 else "dbo"
        by_schema.setdefault(schema, []).append(obj)

    # Also read deployment_summary for global FK / index counts
    dep_summary_path = ws / "deployment" / "deployment_summary.json"
    fk_ok = fk_fail = idx_ok = idx_fail = 0
    if dep_summary_path.exists():
        dep = json.loads(dep_summary_path.read_text())
        phases = dep.get("phases", {})
        fk_ok   = phases.get("foreign_keys", {}).get("ok", 0)
        fk_fail = phases.get("foreign_keys", {}).get("failed", 0)
        idx_ok  = phases.get("indexes", {}).get("ok", 0)
        idx_fail = phases.get("indexes", {}).get("failed", 0)

    checks = []

    for schema_name in sorted(by_schema.keys()):
        objs = by_schema[schema_name]
        tables   = [o for o in objs if o.get("type") == "table"]
        views    = [o for o in objs if o.get("type") == "view"]
        procs    = [o for o in objs if o.get("type") == "procedure"]
        fns      = [o for o in objs if o.get("type") == "function"]
        triggers = [o for o in objs if o.get("type") == "trigger"]

        ok_statuses = ("match", "col_mismatch")
        t_ok   = sum(1 for o in tables   if o.get("status") in ok_statuses)
        t_miss = sum(1 for o in tables   if o.get("status") == "missing")
        v_ok   = sum(1 for o in views    if o.get("status") in ok_statuses)
        v_miss = sum(1 for o in views    if o.get("status") == "missing")
        p_ok   = sum(1 for o in procs    if o.get("status") in ("match", "param_mismatch"))
        f_ok   = sum(1 for o in fns      if o.get("status") in ("match", "param_mismatch"))
        tr_ok  = sum(1 for o in triggers if o.get("status") == "match")
        tr_miss = sum(1 for o in triggers if o.get("status") == "missing")

        if tables:
            checks.append({
                "check": "table_count",
                "_schema": schema_name,
                "passed": t_miss == 0,
                "source": len(tables),
                "spg": t_ok,
                "note": f"{t_ok}/{len(tables)} tables in SPG"
                        + (f" ({t_miss} missing)" if t_miss else ""),
            })

        if views:
            checks.append({
                "check": "view_count",
                "_schema": schema_name,
                "passed": v_miss == 0,
                "source": len(views),
                "spg": v_ok,
                "note": f"{v_ok}/{len(views)} views in SPG"
                        + (f" ({v_miss} missing)" if v_miss else ""),
            })

        if procs or fns:
            total = len(procs) + len(fns)
            ok    = p_ok + f_ok
            checks.append({
                "check": "proc_fn_count",
                "_schema": schema_name,
                "passed": ok == total,
                "source": total,
                "spg": ok,
                "note": f"{ok}/{total} procedures/functions in SPG",
            })

        if triggers:
            checks.append({
                "check": "trigger_count",
                "_schema": schema_name,
                "passed": tr_miss == 0,
                "source": len(triggers),
                "spg": tr_ok,
                "note": f"{tr_ok}/{len(triggers)} triggers in SPG"
                        + (f" ({tr_miss} missing)" if tr_miss else ""),
            })

    # Global FK / index checks (cross-schema — tag as 'all schemas')
    if fk_ok + fk_fail:
        checks.append({
            "check": "foreign_key_count",
            "_schema": "all schemas",
            "passed": fk_fail == 0,
            "source": fk_ok + fk_fail,
            "spg": fk_ok,
            "note": f"{fk_ok} FKs deployed" + (f" | {fk_fail} failed" if fk_fail else ""),
        })
    if idx_ok + idx_fail:
        checks.append({
            "check": "index_count",
            "_schema": "all schemas",
            "passed": idx_fail == 0,
            "source": idx_ok + idx_fail,
            "spg": idx_ok,
            "note": f"{idx_ok} indexes deployed" + (f" | {idx_fail} failed" if idx_fail else ""),
        })

    val_report = {
        "source": catalog_result.get("source", ""),
        "target": catalog_result.get("target", ""),
        "generated_at": catalog_result.get("generated_at", ""),
        "checks": checks,
    }
    val_path = ws / "validation" / "validation_report.json"
    val_path.write_text(json.dumps(val_report, indent=2), encoding="utf-8")
    print(f"  Schema checks written: {val_path} ({len(checks)} checks across {len(by_schema)} schemas)")


if __name__ == "__main__":
    main()
