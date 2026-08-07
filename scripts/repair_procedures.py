#!/usr/bin/env python3
"""
repair_procedures.py — Two-phase repair pipeline for failed stored procedures.

Phase 1 — Rule-based:
  Applies plpgsql-fixes.yaml patterns (including procedure_only rules) to all
  failed procedures, then retries deployment.

Phase 2 — LLM repair (Snowflake Cortex):
  For procedures that still fail after rule-based fixes, calls
  SNOWFLAKE.CORTEX.COMPLETE with the original T-SQL + current PL/pgSQL +
  PostgreSQL error.  Retries up to max_iterations times per procedure.

Usage:
  python repair_procedures.py \\
      --work-dir ~/.spgloader/20260101_120000 \\
      --spg-service pg_my_instance

  # Rules only, no LLM (fast, offline)
  python repair_procedures.py ... --rules-only

  # Override model / iterations from config
  python repair_procedures.py ... --model mistral-large2 --max-iterations 5

  # Debug: save every LLM attempt
  python repair_procedures.py ... --debug-llm
"""
import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))


# ---------------------------------------------------------------------------
# 1-minute status reporter
# ---------------------------------------------------------------------------

class _StatusReporter:
    """Background thread that prints repair progress every `interval` seconds (default 20s)."""

    def __init__(self, total: int, interval: int = 60):
        self.total = total
        self.interval = interval
        self._fixed = 0
        self._failed = 0
        self._lock = threading.Lock()
        self._start = time.time()
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._thread.join()

    def record_fixed(self) -> None:
        with self._lock:
            self._fixed += 1

    def record_failed(self) -> None:
        with self._lock:
            self._failed += 1

    def _run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            elapsed = int(time.time() - self._start)
            m, s = divmod(elapsed, 60)
            with self._lock:
                done = self._fixed + self._failed
                queued = self.total - done
            print(
                f"\n[Status {m}:{s:02d}]  Fixed: {self._fixed}  "
                f"Failed so far: {self._failed}  "
                f"Remaining: {queued}/{self.total}",
                flush=True,
            )



# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "model": "llama3.3-70b",
    "max_iterations": 3,
    "temperature": 0.1,
    "max_tokens": 4096,
    "workers": 4,
    "progress_interval": 20,
    "snowflake_connection": "",
    "warehouse": "COMPUTE_WH",
    "prompt_template": "procedure-repair-prompt.md",
    "mysql_prompt_template": "procedure-repair-mysql-prompt.md",
    "oracle_prompt_template": "procedure-repair-oracle-prompt.md",
    "debug_llm_output": False,
}


def _load_config(skill_dir: Path) -> dict:
    cfg_path = skill_dir / "references" / "llm-repair-config.yaml"
    if not cfg_path.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return {**_DEFAULT_CONFIG, **(data or {})}
    except Exception as e:
        print(f"  WARN: could not load llm-repair-config.yaml: {e} — using defaults",
              file=sys.stderr)
        return dict(_DEFAULT_CONFIG)


def _load_prompt_template(skill_dir: Path, template_name: str) -> str:
    path = skill_dir / "references" / "prompts" / template_name
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_proc_name(sql: str) -> str | None:
    m = re.search(
        r'CREATE\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION)\s+(["\w]+(?:\.["\w]+)?)\s*\(',
        sql, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower().rstrip('"').strip('"')


def _extract_plpgsql_from_response(response: str) -> str | None:
    """Extract the CREATE OR REPLACE PROCEDURE/FUNCTION block from an LLM response."""
    # Try to find code fenced SQL
    fence_m = re.search(
        r'```(?:sql|plpgsql)?\s*(CREATE\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION).*?)```',
        response, re.IGNORECASE | re.DOTALL)
    if fence_m:
        return fence_m.group(1).strip()
    # Try bare CREATE OR REPLACE PROCEDURE/FUNCTION
    bare_m = re.search(r'(CREATE\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION)\b.*)',
                       response, re.IGNORECASE | re.DOTALL)
    if bare_m:
        return bare_m.group(1).strip()
    return None


def _inject_search_path(sql: str, file_name: str) -> str:
    """Prepend SET search_path when the filename encodes a target schema.

    evdas__proc_name.sql  →  SET search_path TO "evdas", public;\n<sql>
    proc_name.sql         →  <sql> unchanged
    """
    stem = Path(file_name).stem
    if "__" in stem:
        prefix = stem.split("__")[0].strip()
        if prefix:
            return f'SET search_path TO "{prefix}", public;\n{sql}'
    return sql


def _try_deploy(conn, sql: str, name: str) -> str | None:
    """Try to execute SQL on the SPG connection.  Returns error string or None."""
    import psycopg2
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        return None  # success
    except Exception as e:
        conn.rollback()
        return str(e).replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Cortex LLM call
# ---------------------------------------------------------------------------

def _call_cortex(sf_conn, prompt: str, model: str,
                 temperature: float, max_tokens: int) -> str:
    """Call SNOWFLAKE.CORTEX.COMPLETE and return the text response."""
    # Build the messages JSON as a Python string, bind model/options separately
    messages_json = json.dumps([{"role": "user", "content": prompt}])
    options_json = json.dumps({"temperature": temperature, "max_tokens": max_tokens})
    # Use PARSE_JSON to convert string literals to VARIANT for Cortex
    sql = (
        "SELECT SNOWFLAKE.CORTEX.COMPLETE("
        "  %s, PARSE_JSON(%s), PARSE_JSON(%s))"
    )
    try:
        with sf_conn.cursor() as cur:
            cur.execute(sql, (model, messages_json, options_json))
            row = cur.fetchone()
            if row:
                result = row[0]
                if isinstance(result, str) and result.strip().startswith('{'):
                    data = json.loads(result)
                    choices = data.get('choices', [])
                    if choices:
                        msg = choices[0]
                        return (msg.get('messages') or
                                msg.get('message', {}).get('content', '') or
                                str(msg))
                return str(result) if result else ""
    except Exception as e:
        raise RuntimeError(f"Cortex call failed: {e}") from e
    return ""


def _open_snowflake_conn(connection_name: str, warehouse: str):
    """Open a Snowflake connection using connections.toml."""
    import snowflake.connector
    # Try PAT-based connection first (no MFA prompt), then fall back
    # Add your PAT connection name from ~/.snowflake/connections.toml if needed
    pat_conn_name = ""  # set to your PAT connection name, e.g. "my-account-pat"
    for name in [connection_name, pat_conn_name, "default", ""]:
        if name is None:
            continue
        try:
            kwargs = {"connection_name": name} if name else {}
            conn = snowflake.connector.connect(**kwargs)
            if warehouse:
                conn.cursor().execute(f"USE WAREHOUSE {warehouse}")
            print(f"  Snowflake connected via '{name or 'env'}' connection")
            return conn
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All Snowflake connection attempts failed: {last_err}")


# ---------------------------------------------------------------------------
# Phase 1: Rule-based repair
# ---------------------------------------------------------------------------

def _phase1_rules(work_dir: Path, failed_names: set[str], wave_dir: Path | None = None) -> dict[str, str]:
    """Apply rule-based fixes to failed procedures.

    Returns {proc_name: error_after_fix} for procedures still failing, or
    {} entries for those that were fixed (caller checks deploy separately).
    """
    # Import fix_procedures logic inline (same module location)
    scripts_dir = Path(__file__).parent
    fix_procs_path = scripts_dir / "fix_procedures.py"
    if not fix_procs_path.exists():
        print("  WARN: fix_procedures.py not found — skipping rule phase",
              file=sys.stderr)
        return {}

    # Use the public API from fix_procedures
    import importlib.util
    spec = importlib.util.spec_from_file_location("fix_procedures", fix_procs_path)
    fp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fp)

    rules = fp._load_plpgsql_rules(SKILL_DIR)
    proc_dir = wave_dir or (work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers")

    changed = 0
    for f in sorted(proc_dir.glob("*.sql")):
        name = _extract_proc_name(f.read_text(encoding="utf-8", errors="replace"))
        if name not in failed_names:
            continue
        was_changed, fixes = fp.fix_procedure_file(f, rules)
        if was_changed:
            changed += 1
    print(f"  Rule-phase: {changed} procedure files updated")
    return {}


# ---------------------------------------------------------------------------
# Phase 2: LLM repair loop
# ---------------------------------------------------------------------------

def _phase2_llm(
    work_dir: Path,
    spg_conn,
    sf_conn,
    failed_items: list[dict],
    original_tsql: dict[str, str],
    prompt_template: str,
    config: dict,
    debug: bool = False,
    source_type: str = "mssql",
    workers: int = 1,
    wave_dir: Path | None = None,
) -> dict:
    """LLM repair loop.

    failed_items: list of {procedure, file, error} from deploy report
    original_tsql: {proc_name_lower: ddl_string}
    source_type: 'mssql' | 'oracle' — selects placeholder key in prompt
    workers: number of parallel repair threads (each opens its own connections)
    Returns {fixed: [...], still_failed: [...]}
    """
    max_iter = config["max_iterations"]
    model = config["model"]
    temperature = config["temperature"]
    max_tokens = config["max_tokens"]

    _wd = wave_dir or (work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers")
    review_dir = _wd.parent / (_wd.name + "_llm_review")
    review_dir.mkdir(parents=True, exist_ok=True)

    proc_dir = _wd

    fixed = []
    still_failed = []

    def _repair_one(item: dict) -> dict:
        """Repair a single procedure. Opens its own DB connections (thread-safe)."""
        import psycopg2
        proc_name = item["procedure"]
        file_name = item["file"]
        current_error = item["error"]
        short_name = proc_name.split(".")[-1]

        proc_file = proc_dir / file_name
        if not proc_file.exists():
            print(f"  SKIP  {proc_name} — file not found")
            return {"status": "skip", "item": {**item, "reason": "file_not_found"}}

        current_sql = proc_file.read_text(encoding="utf-8", errors="replace")
        tsql = original_tsql.get(short_name, original_tsql.get(proc_name, "-- T-SQL not found"))

        print(f"\n  LLM repair: {proc_name}")

        # Each worker opens its own connections
        _spg = psycopg2.connect(f"service={spg_service}")
        _spg.autocommit = False
        _sf = _open_snowflake_conn(
            config.get("snowflake_connection", ""),
            config.get("warehouse", ""),
        )

        try:
            for iteration in range(1, max_iter + 1):
                print(f"    [{proc_name}] Iteration {iteration}/{max_iter} ...", end=" ", flush=True)

                source_key = "original_plsql" if source_type == "oracle" else "original_tsql"
                prompt = (prompt_template
                          .replace(f"{{{source_key}}}", tsql)
                          .replace("{current_plpgsql}", current_sql)
                          .replace("{pg_error}", current_error)
                          .replace("{iteration}", str(iteration)))

                try:
                    response = _call_cortex(_sf, prompt, model, temperature, max_tokens)
                except Exception as e:
                    print(f"Cortex error: {e}")
                    break

                repaired_sql = _extract_plpgsql_from_response(response)
                if not repaired_sql:
                    print(f"could not extract PL/pgSQL from response")
                    if debug:
                        (review_dir / f"{file_name}.iter{iteration}.llm_raw.txt").write_text(
                            response, encoding="utf-8")
                    continue

                if debug:
                    (review_dir / f"{file_name}.iter{iteration}.sql").write_text(
                        repaired_sql, encoding="utf-8")

                deploy_err = _try_deploy(_spg, _inject_search_path(repaired_sql, file_name), proc_name)
                if deploy_err is None:
                    proc_file.write_text(repaired_sql, encoding="utf-8")
                    print(f"FIXED on iteration {iteration}")
                    return {"status": "fixed", "item": {"procedure": proc_name, "iteration": iteration}}
                else:
                    print(f"still fails: {deploy_err[:80]}")
                    current_sql = repaired_sql
                    current_error = deploy_err
                    time.sleep(0.2)

            # All iterations exhausted
            (review_dir / file_name).write_text(current_sql, encoding="utf-8")
            print(f"    [{proc_name}] → written to llm_review/ for manual inspection")
            return {"status": "failed", "item": {
                "procedure": proc_name,
                "file": file_name,
                "error": current_error,
                "iterations_tried": max_iter,
            }}
        finally:
            _spg.close()
            _sf.close()

    # Capture spg_service for use in _repair_one closures
    spg_service = config.get("_spg_service", "")

    # Start status reporter — interval from config, fallback 20s
    _progress_interval = config.get("progress_interval", 20)
    reporter = _StatusReporter(total=len(failed_items), interval=_progress_interval)
    reporter.start()

    try:
        if workers <= 1:
            for item in failed_items:
                result = _repair_one(item)
                if result["status"] == "fixed":
                    fixed.append(result["item"])
                    reporter.record_fixed()
                else:
                    still_failed.append(result["item"])
                    reporter.record_failed()
        else:
            print(f"\n  Running {workers} parallel repair workers (status every 60s)...")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_repair_one, item): item for item in failed_items}
                for future in as_completed(futures):
                    result = future.result()
                    if result["status"] == "fixed":
                        fixed.append(result["item"])
                        reporter.record_fixed()
                    else:
                        still_failed.append(result["item"])
                        reporter.record_failed()
    finally:
        reporter.stop()

    return {"fixed": fixed, "still_failed": still_failed}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def repair_procedures(
    work_dir: Path,
    spg_service: str,
    rules_only: bool = False,
    model_override: str | None = None,
    max_iterations_override: int | None = None,
    debug: bool = False,
    skill_dir: Path | None = None,
    source_type: str = "mssql",
    workers: int = 1,
    report_file: str | None = None,
    wave_dir: str | None = None,
) -> dict:
    """Run the two-phase repair pipeline.  Returns the repair report dict."""
    import psycopg2

    if skill_dir is None:
        skill_dir = SKILL_DIR
    config = _load_config(skill_dir)
    if model_override:
        config["model"] = model_override
    if max_iterations_override:
        config["max_iterations"] = max_iterations_override
    # Resolve workers: CLI override > config file > _DEFAULT_CONFIG (4)
    resolved_workers = workers if workers is not None else config.get("workers", 4)

    # Resolve the source directory for converted SQL files
    _wave_dir = Path(wave_dir).expanduser() if wave_dir else (
        work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers"
    )

    # Load deploy report — supports both procedures and functions reports
    if report_file:
        report_path = Path(report_file).expanduser()
    else:
        report_path = work_dir / "conversion" / "procedures_deploy_report.json"
    if not report_path.exists():
        print(f"ERROR: deploy report not found: {report_path}",
              file=sys.stderr)
        return {}

    deploy_report = json.loads(report_path.read_text())
    raw_failed = deploy_report.get("failed", [])
    # Normalise: functions report uses 'function' key; procedures use 'procedure'
    failed_items = [
        {**item, "procedure": item.get("procedure") or item.get("function", "unknown")}
        for item in raw_failed
    ]
    if not failed_items:
        print("No failed items in deploy report — nothing to repair.")
        return {"fixed_rules": [], "fixed_llm": [], "still_failed": []}

    print(f"\nObjects to repair: {len(failed_items)}")

    # Load original source DDL from ddl_objects.json (T-SQL or PL/SQL depending on source)
    ddl_path = work_dir / "ddl_objects.json"
    original_tsql: dict[str, str] = {}
    if ddl_path.exists():
        data = json.loads(ddl_path.read_text())
        if isinstance(data, dict):
            data = data.get("objects", [])
        for obj in data:
            if obj.get("type") == "procedure":
                name = obj["name"].strip('["').rstrip(']"]').lower()
                original_tsql[name] = obj.get("ddl", "")
    source_label = "PL/SQL" if source_type == "oracle" else "T-SQL"
    print(f"Original {source_label} loaded: {len(original_tsql)} procedures")

    # ── Phase 1: Rule-based repair ─────────────────────────────────────────
    print("\n" + "="*60)
    print("PHASE 1: Rule-based repair")
    print("="*60)

    failed_names = {item["procedure"] for item in failed_items}
    _phase1_rules(work_dir, failed_names, wave_dir=_wave_dir)

    # Re-run deployment to see which ones rule-fixes resolved
    spg_conn = psycopg2.connect(f"service={spg_service}")
    spg_conn.autocommit = False

    proc_dir = _wave_dir
    fixed_rules = []
    still_failed_after_rules = []

    for item in failed_items:
        f = proc_dir / item["file"]
        if not f.exists():
            still_failed_after_rules.append(item)
            continue
        sql = f.read_text(encoding="utf-8", errors="replace")
        sql = _inject_search_path(sql, item["file"])
        err = _try_deploy(spg_conn, sql, item["procedure"])
        if err is None:
            fixed_rules.append(item["procedure"])
            print(f"  FIXED-RULES  {item['procedure']}")
        else:
            updated_item = {**item, "error": err}
            still_failed_after_rules.append(updated_item)
            print(f"  STILL-FAIL   {item['procedure']}: {err[:80]}")

    print(f"\nPhase 1 result: {len(fixed_rules)} fixed by rules, "
          f"{len(still_failed_after_rules)} still failing")

    if rules_only or not still_failed_after_rules:
        spg_conn.close()
        repair_report = {
            "fixed_rules": fixed_rules,
            "fixed_llm": [],
            "still_failed": still_failed_after_rules,
        }
        _update_deploy_report(report_path, fixed_rules)
        _write_report(work_dir, repair_report)
        return repair_report

    # ── Phase 2: LLM repair ────────────────────────────────────────────────
    print("\n" + "="*60)
    print(f"PHASE 2: LLM repair ({config['model']}, up to {config['max_iterations']} iterations)")
    print("="*60)

    # Select prompt template based on source type
    if source_type == "oracle":
        tpl_name = config.get("oracle_prompt_template", "procedure-repair-oracle-prompt.md")
    elif source_type in ("mysql", "mariadb"):
        tpl_name = config.get("mysql_prompt_template", "procedure-repair-mysql-prompt.md")
    else:
        tpl_name = config.get("prompt_template", "procedure-repair-prompt.md")
    prompt_template = _load_prompt_template(skill_dir, tpl_name)
    print(f"  Prompt template : {tpl_name}")

    try:
        sf_conn = _open_snowflake_conn(
            config.get("snowflake_connection", ""),
            config.get("warehouse", ""),
        )
    except Exception as e:
        print(f"ERROR: could not connect to Snowflake for Cortex: {e}", file=sys.stderr)
        print("Hint: check your ~/.snowflake/connections.toml or set SNOWFLAKE_* env vars")
        spg_conn.close()
        repair_report = {
            "fixed_rules": fixed_rules,
            "fixed_llm": [],
            "still_failed": still_failed_after_rules,
            "llm_error": str(e),
        }
        _update_deploy_report(report_path, fixed_rules)
        _write_report(work_dir, repair_report)
        return repair_report

    llm_result = _phase2_llm(
        work_dir=work_dir,
        spg_conn=spg_conn,
        sf_conn=sf_conn,
        failed_items=still_failed_after_rules,
        original_tsql=original_tsql,
        prompt_template=prompt_template,
        config={**config, "_spg_service": spg_service},
        debug=debug or config.get("debug_llm_output", False),
        source_type=source_type,
        workers=resolved_workers,
        wave_dir=_wave_dir,
    )

    sf_conn.close()
    spg_conn.close()

    repair_report = {
        "fixed_rules": fixed_rules,
        "fixed_llm": [item["procedure"] for item in llm_result["fixed"]],
        "still_failed": llm_result["still_failed"],
        "llm_iterations": llm_result["fixed"],
    }
    all_fixed = fixed_rules + repair_report["fixed_llm"]
    _update_deploy_report(report_path, all_fixed)
    _write_report(work_dir, repair_report)

    # Update object manifest with repair results
    try:
        from spgloader.manifest import ObjectManifest
        manifest = ObjectManifest(work_dir)
        for name in all_fixed:
            manifest.set_repaired(name, "completed")
        for item in llm_result.get("still_failed", []):
            name = item.get("procedure", item) if isinstance(item, dict) else str(item)
            manifest.set_repaired(name, "failed", error="LLM repair exhausted")
        manifest.save()
    except Exception:
        pass  # manifest is optional — don't break repair flow

    return repair_report


def _update_deploy_report(report_path: Path, all_fixed: list[str]) -> None:
    """Move successfully-repaired objects from failed → succeeded in the deploy report."""
    if not report_path.exists() or not all_fixed:
        return
    report = json.loads(report_path.read_text())
    fixed_bases = {n.split(".")[-1].lower() for n in all_fixed}
    fixed_fqns  = {n.lower() for n in all_fixed}
    still_fail: list = []
    newly_ok:   list[str] = []
    for item in report.get("failed", []):
        raw = item.get("procedure") or item.get("function") or ""
        name = raw if isinstance(raw, str) else str(raw)
        if name.lower() in fixed_fqns or name.split(".")[-1].lower() in fixed_bases:
            newly_ok.append(name)
        else:
            still_fail.append(item)
    report["succeeded"] = list(report.get("succeeded", [])) + newly_ok
    report["failed"]    = still_fail
    report_path.write_text(json.dumps(report, indent=2))
    print(f"  Deploy report updated: {len(newly_ok)} moved to succeeded → {report_path.name}")


def _write_report(work_dir: Path, report: dict) -> None:
    """Write repair_report.json, accumulating results across multiple repair runs."""
    path = work_dir / "conversion" / "repair_report.json"
    # Merge with results from any previous repair run (e.g. procs then funcs)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            def _merge_dedup(prev: list[str], curr: list[str]) -> list[str]:
                seen: set[str] = set()
                merged: list[str] = []
                for n in prev + curr:
                    key = n.lower()
                    if key not in seen:
                        seen.add(key)
                        merged.append(n)
                return merged
            report = {
                **report,
                "fixed_llm":   _merge_dedup(existing.get("fixed_llm",   []), report.get("fixed_llm",   [])),
                "fixed_rules": _merge_dedup(existing.get("fixed_rules",  []), report.get("fixed_rules",  [])),
            }
        except Exception:
            pass  # corrupt existing file — just overwrite
    path.write_text(json.dumps(report, indent=2))
    print(f"\nRepair report   : {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-phase LLM repair loop for failed stored procedures"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--spg-service", required=True,
                        help="pg_service name from ~/.pg_service.conf")
    parser.add_argument("--rules-only", action="store_true",
                        help="Apply rule-based fixes only; skip LLM phase")
    parser.add_argument("--model", default=None,
                        help="Override Cortex model (e.g. mistral-large2)")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Override max LLM iterations per procedure")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel repair workers (default: from llm-repair-config.yaml, fallback 4)")
    parser.add_argument("--debug-llm", action="store_true",
                        help="Save every LLM attempt to llm_review/")
    parser.add_argument("--source-type", default=None,
                        choices=["mssql", "mysql", "mariadb", "oracle"],
                        help="Source DB type (default: read from source_conn.env)")
    parser.add_argument("--report-file", default=None,
                        help="Path to a deploy report JSON (default: procedures_deploy_report.json). "
                             "Use to repair functions: --report-file .../functions_deploy_report.json")
    parser.add_argument("--wave-dir", default=None,
                        help="Directory containing converted SQL files "
                             "(default: wave_4_procedures_triggers). "
                             "Use for functions: --wave-dir .../wave_3_functions_fixed")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()

    # Resolve source type: CLI flag > source_conn.env > default mssql
    source_type = args.source_type
    if not source_type:
        env_file = work_dir / "source_conn.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("SOURCE_TYPE="):
                    source_type = line.split("=", 1)[1].strip().lower()
                    break
    source_type = source_type or "mssql"

    result = repair_procedures(
        work_dir=work_dir,
        spg_service=args.spg_service,
        rules_only=args.rules_only,
        model_override=args.model,
        max_iterations_override=args.max_iterations,
        debug=args.debug_llm,
        source_type=source_type,
        workers=args.workers,
        report_file=args.report_file,
        wave_dir=args.wave_dir,
    )

    total_fixed = len(result.get("fixed_rules", [])) + len(result.get("fixed_llm", []))
    print(f"\n{'='*60}")
    print(f"Fixed by rules  : {len(result.get('fixed_rules', []))}")
    print(f"Fixed by LLM    : {len(result.get('fixed_llm', []))}")
    print(f"Total fixed     : {total_fixed}")
    print(f"Still failing   : {len(result.get('still_failed', []))}")
    if result.get("still_failed"):
        _wd = Path(args.wave_dir).expanduser() if args.wave_dir else (
            work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers"
        )
        review_dir = _wd.parent / (_wd.name + "_llm_review")
        print(f"Review dir      : {review_dir}")


if __name__ == "__main__":
    main()
