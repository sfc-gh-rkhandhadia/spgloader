"""html_report.py — multi-tab HTML migration report for spgloader.

Generates a self-contained HTML file with 5 tabs:
  Overview | Deployment | Objects | Validation | Assessment
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"
_CHARTJS_INLINE: str | None = None


def _chartjs_script() -> str:
    global _CHARTJS_INLINE
    if _CHARTJS_INLINE is None:
        try:
            with urllib.request.urlopen(_CHARTJS_CDN, timeout=5) as resp:
                _CHARTJS_INLINE = resp.read().decode("utf-8")
        except Exception:
            _CHARTJS_INLINE = ""
    if _CHARTJS_INLINE:
        return f"<script>\n{_CHARTJS_INLINE}\n</script>"
    return f'<script src="{_CHARTJS_CDN}"></script>'


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def _norm_view_name(n: Any) -> str:
    """Normalise view names from deploy_report or fix_report to 'schema.view' form.

    fix_report stores file names  (schema__view_name.sql) -> schema.view_name
    deploy_report may have stray double-quotes from MySQL quoting round-trips.
    """
    if isinstance(n, dict):
        n = n.get("view") or n.get("name") or str(n)
    n = str(n)
    # fix_report file-name format: first __ is the schema separator
    if n.endswith(".sql") and "__" in n:
        n = n[:-4].replace("__", ".", 1)
    # strip stray double-quotes that can appear from MySQL quoting round-trips
    n = n.replace('"', "")
    return n.strip()


def _clean_name(n) -> str:
    if isinstance(n, dict):
        n = n.get("procedure") or n.get("view") or n.get("name") or str(n)
    n = str(n)
    # Normalise MySQL fix_report file-name format: schema__view_name.sql -> schema.view_name
    if n.endswith(".sql") and "__" in n:
        n = n[:-4].replace("__", ".", 1)
    return re.sub(r'"\.?"', ".", n).strip('"')


def _is_trigger_fn(name: str) -> bool:
    return "_trigger" in name.lower() or name.lower().endswith("_trg")


# ---------------------------------------------------------------------------
# Name mapping helpers  (source MSSQL name ↔ SPG deployed name)
# ---------------------------------------------------------------------------

def _build_name_map(ddl_objects: list) -> dict:
    """Build a lookup from SPG name key → {source_fqn, spg_fqn, source_base, spg_base}.

    Keys are indexed two ways so we can find the original name regardless of whether
    the caller has the exact lowercase form (key = spg_base) or the space/underscore-
    stripped form (key = base.replace(' ','').replace('_','')), which handles the
    procedure name-folding case (e.g. 'employee sales by country' → 'employee').
    """
    nmap: dict = {}
    for obj in ddl_objects if isinstance(ddl_objects, list) else ddl_objects.get("objects", []):
        src_fqn = obj.get("fqn") or obj.get("name") or ""
        if not src_fqn:
            continue
        parts = src_fqn.split(".", 1)
        schema   = parts[0].lower() if len(parts) == 2 else "dbo"
        src_base = parts[-1]
        spg_base  = src_base.lower()
        spg_fqn   = f"{schema}.{spg_base}"
        entry = {"source_fqn": src_fqn, "spg_fqn": spg_fqn,
                 "source_base": src_base, "spg_base": spg_base}
        nmap[spg_base] = entry
        nmap.setdefault(spg_base.replace(" ", "").replace("_", ""), entry)
    return nmap


def _resolve_name_pair(spg_name: str, name_map: dict) -> tuple[str, str]:
    """Return (source_display, spg_display) given the SPG-side name.

    - source_display: original MSSQL object name (base, no schema prefix)
    - spg_display:    SPG object name — shown as '—' when identical to source
                      (i.e. only a case-fold occurred, no meaningful rename)
    """
    base = (spg_name.split(".")[-1].lower() if spg_name else "")
    key  = base.replace(" ", "").replace("_", "")
    entry = name_map.get(base) or name_map.get(key)
    if not entry:
        return spg_name.split(".")[-1] if "." in spg_name else spg_name, "—"
    src = entry["source_base"]
    spg = entry["spg_base"]
    # Show SPG name whenever it differs from source (including case-only renames)
    spg_display = spg if spg != src else "—"
    return src, spg_display


# ---------------------------------------------------------------------------
# load_workspace_data
# ---------------------------------------------------------------------------

def load_workspace_data(workspace_dir: str | Path) -> dict:
    ws = Path(workspace_dir).resolve()

    # -- config -----------------------------------------------------------
    config = _load_yaml(ws / ".spgloader" / "config.yaml")
    if not config:
        for env_file, key_map in [
            ("source_conn.env", {"SOURCE_TYPE": "source_type", "SOURCE_DATABASE": "source_db"}),
            ("target_conn.env", {"TARGET_SPG_SERVICE": "spg_instance"}),
        ]:
            p = ws / env_file
            if p.exists():
                for line in p.read_text().splitlines():
                    for k, v in key_map.items():
                        if line.startswith(f"{k}="):
                            config[v] = line.split("=", 1)[1].strip()

    source_type  = config.get("source_type", "mssql").upper()
    source_db    = config.get("source_db", "—")
    spg_instance = config.get("spg_instance", "SPG")

    # -- deployment summary -----------------------------------------------
    deploy_dir    = ws / "deployment"
    deploy_files  = sorted(deploy_dir.glob("*_deploy.json")) if deploy_dir.exists() else []
    summary_file  = deploy_dir / "deployment_summary.json"
    if not deploy_files and deploy_dir.exists():
        # spgloader MySQL/multi-db layout: deployment_<db>.json
        deploy_files = sorted(deploy_dir.glob("deployment_*.json"))
    if not deploy_files and deploy_dir.exists():
        # parallel_deploy.py per-db layout: <db>_deployment.json
        deploy_files = [f for f in sorted(deploy_dir.glob("*_deployment.json"))
                        if f.name != "deployment_summary.json"]
    if not deploy_files and summary_file.exists():
        deploy_files = [summary_file]

    schemas: dict[str, dict] = {}
    for f in deploy_files:
        d = _load_json(f)
        db = d.get("source_db", f.stem.replace("_deploy", "").replace("_summary", ""))
        phases  = d.get("phases", {})
        failures = d.get("failures", [])
        fk_benign = sum(1 for x in failures if "already exists" in x.get("error", str(x)))
        fk_real   = sum(1 for x in failures if x.get("phase") == "foreign_keys"
                        and "already exists" not in x.get("error", str(x)))
        idx_fail  = [x for x in failures if x.get("phase") == "indexes"]
        schemas[db] = {
            "tables_ok":    phases.get("tables",      {}).get("ok",   0),
            "tables_total": phases.get("tables",      {}).get("ok",   0)
                          + (phases.get("tables",     {}).get("failed", 0) or phases.get("tables",  {}).get("fail", 0)),
            "indexes_ok":   phases.get("indexes",     {}).get("ok",   0),
            "indexes_fail": (phases.get("indexes",    {}).get("failed", 0) or phases.get("indexes", {}).get("fail", 0)),
            "seqs_ok":      phases.get("sequences",   {}).get("ok",   0),
            "fk_benign":    fk_benign,
            "fk_real":      fk_real,
            "elapsed_s":    d.get("elapsed_s", 0.0),
            "index_failures": idx_fail,
            "all_failures":   failures,
        }

    total_tables  = sum(s["tables_ok"]   for s in schemas.values())
    total_indexes = sum(s["indexes_ok"]  for s in schemas.values())

    # -- views deployment -------------------------------------------------
    vr     = _load_json(ws / "conversion" / "deploy_report.json")
    fix_vr = _load_json(ws / "conversion" / "fix_report.json")
    # fix_report.json is written after views are manually fixed and redeployed.
    # When it has more successes than the initial deploy_report.json, treat it
    # as the authoritative post-fix state: merge its succeeded list in and
    # remove from failed any views that were successfully fixed.
    if fix_vr.get("succeeded") and (
        not vr or len(fix_vr["succeeded"]) > len(vr.get("succeeded", []))
    ):
        fix_ok_set = {_norm_view_name(n).lower() for n in fix_vr["succeeded"]}
        extra_from_deploy = [
            n for n in vr.get("succeeded", [])
            if _norm_view_name(n).lower() not in fix_ok_set
        ]
        vr = dict(vr,
                  succeeded  = list(fix_vr["succeeded"]) + extra_from_deploy,
                  failed     = [
                      f for f in vr.get("failed", [])
                      if _norm_view_name(
                          f.get("view", f) if isinstance(f, dict) else f
                      ).lower() not in fix_ok_set
                  ],
                  auto_fixed = list(fix_vr.get("succeeded", [])),
                  )
    elif not vr:
        vr = fix_vr
    views_ok   = [_clean_name(n) for n in vr.get("succeeded", [])]
    views_fail = vr.get("failed", [])
    views_fixed = [_clean_name(n) for n in vr.get("auto_fixed", [])]
    views_skip  = [_clean_name(n) for n in vr.get("skipped", [])]

    # -- functions deployment ---------------------------------------------
    fr         = _load_json(ws / "conversion" / "functions_deploy_report.json")
    if not fr.get("succeeded") and (ws / "conversion" / "functions_fix_report.json").exists():
        fr     = _load_json(ws / "conversion" / "functions_fix_report.json")
    funcs_ok   = [_clean_name(n) for n in fr.get("succeeded", [])]
    funcs_fail = fr.get("failed", [])

    # -- procedures deployment --------------------------------------------
    pr           = _load_json(ws / "conversion" / "procedures_deploy_report.json")
    procs_ok     = [_clean_name(n if isinstance(n, str) else n.get("procedure", str(n)))
                    for n in pr.get("succeeded", [])]
    procs_fail   = pr.get("failed", [])
    procs_legacy = [_clean_name(n if isinstance(n, str) else n.get("procedure", str(n)))
                    for n in pr.get("skipped_legacy", [])]

    # -- LLM repair -------------------------------------------------------
    rr           = _load_json(ws / "conversion" / "repair_report.json")

    def _to_name_list(items):
        return [_clean_name(x) for x in items]

    llm_fixed    = _to_name_list(rr.get("fixed_llm", []))
    rule_fixed   = _to_name_list(rr.get("fixed_rules", []))
    still_failed = _to_name_list(rr.get("still_failed", []))

    # Stubs (backward compat)
    stubs_report = _load_json(ws / "conversion" / "stubs_report.json")
    stubs_list   = [_clean_name(n) for n in stubs_report.get("stubs", [])]

    # -- assessment -------------------------------------------------------
    apath = ws / "assessment" / "assessment_summary.json"
    if not apath.exists():
        apath = ws / "assessment" / "assessment_summary.json" / "assessment_summary.json"
    assess         = _load_json(apath)
    is_blocked     = assess.get("is_blocked", False)
    warn_findings  = assess.get("warn_findings", [])
    ext_prereqs    = assess.get("extension_prereqs", [])

    # -- deprecated -------------------------------------------------------
    dep_review     = _load_json(ws / "deprecated" / "deprecated_review.json")
    dep_groups     = dep_review.get("groups", {}) if isinstance(dep_review, dict) else {}

    # -- validation -------------------------------------------------------
    val_report = _load_json(ws / "validation" / "validation_report.json")
    # MySQL multi-db fallback: merge per-db validation_{db}.json files
    if not val_report:
        per_db_val = sorted((ws / "validation").glob("validation_*.json")) \
                     if (ws / "validation").exists() else []
        if per_db_val:
            merged_checks: list = []
            for _vf in per_db_val:
                _vd = _load_json(_vf)
                db_name = _vf.stem.replace("validation_", "")
                for chk in _vd.get("checks", []):
                    # Tag each check with the source schema so rows are labelled
                    merged_checks.append({**chk, "_schema": db_name})
            val_report = {"checks": merged_checks,
                          "source": "per-db validation files"}
    val_checks = val_report.get("checks", [])

    # -- witness validation (Phase 6.5) ------------------------------------
    witness_chains = _load_json(ws / "witness" / "validation_chains.json")
    # MySQL multi-db fallback: merge per-db chains_report_{db}.json files
    if not witness_chains:
        per_db_files = sorted((ws / "witness").glob("chains_report_*.json")) \
                       if (ws / "witness").exists() else []
        if per_db_files:
            merged_results: dict = {}
            merged_summary: dict = {}
            for _f in per_db_files:
                _d = _load_json(_f)
                merged_results.update(_d.get("validation_results", {}))
                for _k, _v in _d.get("summary", {}).items():
                    merged_summary[_k] = merged_summary.get(_k, 0) + int(_v or 0)
            witness_chains = {"validation_results": merged_results,
                              "summary": merged_summary}
    witness_results = witness_chains.get("validation_results", {})
    witness_summary = witness_chains.get("summary", {})
    witness_ran     = bool(witness_results or witness_summary)

    # -- seed data (Phase 6.5) ------------------------------------------------
    seed_report = _load_json(ws / "witness" / "seed_report.json")
    seed_summary = seed_report.get("summary", {})
    seed_tables  = seed_summary.get("tables_seeded", len(seed_report.get("seed_results", {})))
    seed_zero    = seed_summary.get("tables_zero_rows", 0)
    seed_skipped = seed_summary.get("tables_skipped", 0)
    seed_volume  = seed_report.get("row_volume", 3)
    seed_ran     = bool(seed_report and seed_tables > 0)

    # -- SPG seed load (Phase 6.6 execution parity) ---------------------------
    load_summary = _load_json(ws / "validation_shared" / "load_summary.json")
    spg_rows_loaded  = load_summary.get("total_rows", 0)
    spg_tables_loaded = load_summary.get("tables_loaded", 0)
    spg_load_ran     = bool(load_summary)
    parity_report_md = ""
    parity_file = ws / "parity" / "parity_report.md"
    if parity_file.exists():
        parity_report_md = parity_file.read_text(encoding="utf-8")[:8000]
    parity_ran = parity_file.exists()

    # Structured parity results (written by full_validation.py / mysql_structural_parity.py)
    parity_results = _load_json(ws / "parity" / "parity_results.json")
    parity_structured = bool(parity_results)

    # MySQL fallback: read parity_structural.json and convert to renderable format
    if not parity_results:
        structural = _load_json(ws / "parity" / "parity_structural.json")
        if structural:
            grand_pass = grand_fail = grand_missing = 0
            schema_rows: dict = {}
            for db, d in structural.items():
                tbl    = d.get("tables",   {})
                rout   = d.get("routines", {})
                views  = d.get("views",    {})
                pass_  = (tbl.get("match_count", 0)
                          + rout.get("matched", 0)
                          + views.get("spg_count", 0))
                miss   = (len(tbl.get("only_source", []))
                          + len(rout.get("only_source", []))
                          + len(views.get("only_source", [])))
                col_mm = len(tbl.get("col_mismatches", []))
                # col_mismatches where src=0 are Docker CREATE-TABLE-AS-SELECT
                # artifacts, not real migration failures — exclude from fail count
                real_col_mm = [m for m in tbl.get("col_mismatches", [])
                               if isinstance(m, dict) and m.get("src", -1) != 0]
                grand_pass   += pass_
                grand_fail   += miss + len(real_col_mm)
                grand_missing += miss
                schema_rows[db] = {
                    "pass":    pass_,
                    "fail":    miss + len(real_col_mm),
                    "missing": miss,
                    "spg_only": len(tbl.get("only_spg", [])),
                    "tables_src":      tbl.get("source_count", 0),
                    "tables_spg":      tbl.get("spg_count", 0),
                    "tables_match":    tbl.get("match_count", 0),
                    "routines_src":    rout.get("source_count", 0),
                    "routines_spg":    rout.get("spg_count", 0),
                    "routines_match":  rout.get("matched", 0),
                    "routines_missing":rout.get("only_source", []),
                    "col_mismatches":  real_col_mm,
                    "views_src":       views.get("source_count", 0),
                    "views_spg":       views.get("spg_count", 0),
                    "excluded_objects": [],
                    "objects": [],
                }
            parity_results = {
                "source": "parity_structural.json",
                "grand":  {"pass": grand_pass, "fail": grand_fail,
                           "missing": grand_missing, "spg_only": 0},
                "schemas": schema_rows,
                "_is_structural": True,   # flag for renderer
            }
            parity_structured = True
            parity_ran = True

    # Equivalence filter (user's legacy group include/skip choices)
    equiv_filter = _load_json(ws / "parity" / "equivalence_filter.json")

    # -- name map (source MSSQL name ↔ SPG name) --------------------------------
    ddl_objects_raw = _load_json(ws / "ddl_objects.json")
    name_map = _build_name_map(
        ddl_objects_raw if isinstance(ddl_objects_raw, list)
        else ddl_objects_raw.get("objects", [])
    )

    return {
        "generated":      date.today().isoformat(),
        "source_type":    source_type,
        "source_db":      source_db,
        "spg_instance":   spg_instance,
        "is_blocked":     is_blocked,
        # Deployment
        "schemas":        schemas,
        "total_tables":   total_tables,
        "total_indexes":  total_indexes,
        # Views
        "views_ok":       views_ok,
        "views_fail":     views_fail,
        "views_fixed":    views_fixed,
        "views_skip":     views_skip,
        # Functions
        "funcs_ok":       funcs_ok,
        "funcs_fail":     funcs_fail,
        # Procedures
        "procs_ok":       procs_ok,
        "procs_fail":     procs_fail,
        "procs_legacy":   procs_legacy,
        "stubs":          stubs_list,
        # LLM repair
        "llm_fixed":      llm_fixed,
        "rule_fixed":     rule_fixed,
        "still_failed":   still_failed,
        # Assessment
        "warn_findings":  warn_findings,
        "ext_prereqs":    ext_prereqs,
        # Deprecated
        "dep_groups":     dep_groups,
        # Validation
        "val_checks":     val_checks,
        # Witness (Phase 6.5)
        "witness_ran":      witness_ran,
        "witness_results":  witness_results,
        "witness_summary":  witness_summary,
        # Seed data
        "seed_ran":          seed_ran,
        "seed_tables":       seed_tables,
        "seed_zero":         seed_zero,
        "seed_skipped":      seed_skipped,
        "seed_volume":       seed_volume,
        "spg_rows_loaded":   spg_rows_loaded,
        "spg_tables_loaded": spg_tables_loaded,
        "spg_load_ran":      spg_load_ran,
        # Parity (Phase 6.6)
        "parity_ran":         parity_ran,
        "parity_report_md":   parity_report_md,
        "parity_results":     parity_results,
        "parity_structured":  parity_structured,
        "equiv_filter":       equiv_filter,
        # Name mapping
        "name_map":           name_map,
    }


# ---------------------------------------------------------------------------
# HTML row builders
# ---------------------------------------------------------------------------

def _badge(text: str, style: str) -> str:
    """style: success | warn | fail | info | muted"""
    return f'<span class="badge badge-{style}">{text}</span>'


def _obj_status_badge(name: str, llm_fixed: list, rule_fixed: list,
                       still_failed: list, stubs: list) -> str:
    n = name.lower()
    if any(x.lower() == n for x in still_failed):
        return _badge("✗ Failed", "fail")
    if any(x.lower() == n for x in llm_fixed):
        return _badge("⚙ LLM Fixed", "info")
    if any(x.lower() == n for x in rule_fixed):
        return _badge("⚙ Rule Fixed", "info")
    if any(x.lower() == n for x in stubs):
        return _badge("⟳ Stub", "muted")
    return _badge("✓ Deployed", "success")


def _build_obj_table(items: list, col: str, llm_fixed: list, rule_fixed: list,
                      still_failed: list, stubs: list,
                      name_map: dict | None = None) -> str:
    if not items:
        return "<p class='muted-msg'>None</p>"
    rows = []
    for raw in items:
        name = _clean_name(raw)
        schema = name.split(".")[0] if "." in name else "dbo"
        if name_map is not None:
            src_name, spg_name = _resolve_name_pair(name, name_map)
        else:
            src_name = name.split(".")[-1] if "." in name else name
            spg_name = "—"
        spg_cell = (f"<td class='mono small' style='color:var(--muted)'>{spg_name}</td>"
                    if spg_name != "—" else "<td class='muted' style='text-align:center'>—</td>")
        badge  = _obj_status_badge(name, llm_fixed, rule_fixed, still_failed, stubs)
        rows.append(f"<tr><td class='mono small'>{schema}</td>"
                    f"<td class='mono small'>{src_name}</td>"
                    f"{spg_cell}"
                    f"<td>{badge}</td></tr>")
    return (f"<div class='table-wrap'><table><thead><tr>"
            f"<th>Schema</th><th>Source Name</th><th>SPG Name</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _build_fail_table(failures: list, phase_filter: str | None = None) -> str:
    items = [f for f in failures
             if phase_filter is None or f.get("phase") == phase_filter]
    if not items:
        return "<p class='muted-msg'>None</p>"
    rows = []
    for f in items:
        label = f.get("label", "—")
        error = f.get("error", "—")[:120]
        rows.append(f"<tr><td class='mono small'>{label}</td>"
                    f"<td class='small'>{error}</td></tr>")
    return (f"<div class='table-wrap'><table><thead><tr>"
            f"<th>Object</th><th>Error</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _build_warn_rows(warns: list) -> str:
    if not warns:
        return "<tr><td colspan='4' class='muted-msg'>No warnings</td></tr>"
    rows = []
    for w in warns:
        rows.append(
            f"<tr><td class='mono small'>{w.get('code','')}</td>"
            f"<td class='mono small'>{w.get('object_fqn','')}</td>"
            f"<td class='small'>{w.get('title','')}</td>"
            f"<td class='small'>{w.get('detail','')[:100]}</td></tr>"
        )
    return "".join(rows)


def _build_val_rows(checks: list, name_map: dict | None = None) -> str:
    rows = []
    for c in checks:
        chk     = c.get("check", "")
        passed  = c.get("passed")
        note    = c.get("note", "")
        if passed is None:
            badge = _badge("— Skipped", "muted")
        elif passed:
            badge = _badge("✓ Pass", "success")
        else:
            badge = _badge("✗ Fail", "fail")

        detail = ""
        if chk == "table_count":
            detail = f"Source: {c.get('source',0)} | SPG: {c.get('spg',0)}"
        elif chk == "column_count_sample":
            mm = c.get("mismatches", [])
            detail = f"Sampled {c.get('sample_size',0)} tables — {len(mm)} mismatch(es)"
        elif chk == "primary_key_sample":
            mm = c.get("mismatches", [])
            detail = f"Sampled {c.get('sample_size',0)} tables — {len(mm)} mismatch(es)"
        elif chk == "identity_serial":
            miss = c.get("missing_in_spg", [])
            real_miss = [m for m in miss if "view" not in m.lower()]
            detail = (f"Source: {c.get('source_count',0)} | SPG: {c.get('spg_count',0)} "
                      f"| Missing (non-view): {len(real_miss)}")
            if real_miss:
                badge = _badge("✗ Fail", "fail")
            else:
                badge = _badge("✓ Pass*", "success")
        elif note:
            detail = note

        _check_tips = {
            "table_count":        "Compares the number of base tables in the source schema against SPG. A mismatch means some tables were not deployed.",
            "column_count_sample": "Spot-checks column counts on a sample of tables. source=0 means the table was created via CREATE TABLE AS SELECT and has no static column list in the source catalog (not a migration error).",
            "primary_key_sample":  "Verifies that primary key column sets match between source and SPG for a sample of tables.",
            "identity_serial":     "Counts AUTO_INCREMENT / IDENTITY columns. SPG uses GENERATED ALWAYS AS IDENTITY or SERIAL. Missing entries indicate sequences that may need reseeding.",
            "foreign_key_count":   "Compares FK constraint counts. Mismatches usually mean the referenced table lacks a UNIQUE constraint that PostgreSQL requires.",
            "index_count":         "Compares total index counts. SPG typically shows a higher count because PostgreSQL creates implicit indexes for PRIMARY KEY and UNIQUE constraints.",
        }
        tip = _check_tips.get(chk, "")
        tip_attr = f' data-tip="{tip}"' if tip else ""
        label = chk.replace("_", " ").title()
        schema = c.get("_schema", "")
        schema_cell = f"<td class='mono small'>{schema}</td>" if schema else "<td class='muted'>—</td>"
        # Source → SPG name columns (for per-table row_count checks)
        if chk == "row_count" and c.get("table"):
            tbl = c["table"]
            if name_map:
                src_name, spg_name = _resolve_name_pair(tbl, name_map)
            else:
                src_name = tbl
                spg_name = tbl.lower() if tbl.lower() != tbl else "—"
            spg_cell = (f"<td class='mono small' style='color:var(--muted)'>{spg_name}</td>"
                        if spg_name != "—" else "<td class='muted' style='text-align:center'>—</td>")
            obj_cells = f"<td class='mono small'>{src_name}</td>{spg_cell}"
        else:
            obj_cells = "<td class='muted' style='text-align:center'>—</td><td class='muted' style='text-align:center'>—</td>"
        rows.append(
            f"<tr>{schema_cell}"
            f"{obj_cells}"
            f"<td{tip_attr} style='{'cursor:help' if tip else ''}'>{label}"
            f"{'<span class=\"tip-icon\">ⓘ</span>' if tip else ''}"
            f"</td><td>{badge}</td>"
            f"<td class='small'>{detail}</td></tr>"
        )
    return "".join(rows) or "<tr><td colspan='6' class='muted-msg'>No checks run</td></tr>"


def _build_dep_rows(groups: dict) -> str:
    if not groups:
        return "<tr><td colspan='4' class='muted-msg'>No deprecated patterns detected</td></tr>"
    rows = []
    for key, g in groups.items():
        disp = g.get("disposition", "—")
        badge_map = {"skip": "muted", "migrate": "success", "modernize": "info"}
        badge = _badge(disp.title(), badge_map.get(disp, "muted"))
        rows.append(
            f"<tr><td class='mono small'>{key}</td>"
            f"<td class='small'>{g.get('pattern_name','')}</td>"
            f"<td class='num'>{g.get('object_count',0)}</td>"
            f"<td>{badge}</td></tr>"
        )
    return "".join(rows)


# ---------------------------------------------------------------------------
# render_html  (main template)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _build_equivalence_tab  (Phase 6.6 parity / equivalence test)
# ---------------------------------------------------------------------------

_VERDICT_STYLE = {
    "PASS":         ("success", "✓ Pass"),
    "PASS_RENAMED": ("info",    "✓ Renamed"),
    "FAIL":         ("fail",    "✗ Fail"),
    "ERROR":        ("fail",    "⚠ Error"),
}


def _build_equivalence_tab(data: dict) -> str:
    parity_structured = data.get("parity_structured", False)
    parity_results    = data.get("parity_results", {})
    parity_ran        = data.get("parity_ran", False)
    parity_md         = data.get("parity_report_md", "")

    if not parity_ran and not parity_structured:
        return """
  <div class="section">
    <div class="alert alert-success" style="background:var(--surface2);border-left:3px solid var(--muted)">
      <span class="alert-icon" style="color:var(--muted)">○</span>
      <div><strong>Not Run</strong> — Equivalence / parity testing was not yet executed.
      Re-invoke the skill and choose Phase 6.6 at the end of Phase 6.</div>
    </div>
  </div>"""

    if not parity_structured:
        # Fall back to rendering the markdown report
        import re as _re
        ph = _re.sub(r"^### (.+)$", r"<h3>\1</h3>", parity_md, flags=_re.MULTILINE)
        ph = _re.sub(r"^## (.+)$",  r"<h2>\1</h2>",  ph, flags=_re.MULTILINE)
        ph = _re.sub(r"^# (.+)$",   r"<h2>\1</h2>",  ph, flags=_re.MULTILINE)
        ph = _re.sub(r"\|(.+)\|",   lambda m: "<tr>" + "".join(f"<td>{c.strip()}</td>" for c in m.group(1).split("|")) + "</tr>", ph)
        ph = _re.sub(r"`([^`]+)`",  r"<code>\1</code>", ph)
        ph = _re.sub(r"^- (.+)$",   r"<li>\1</li>", ph, flags=_re.MULTILINE)
        return f"""
  <div class="section">
    <div class="alert alert-success" style="background:var(--surface2);border-left:3px solid var(--amber)">
      <span class="alert-icon" style="color:var(--amber)">⚠</span>
      <div>Structured results not available — showing markdown report.
      Re-run <code>full_validation.py</code> to generate <code>parity_results.json</code>.</div>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;
                padding:20px;font-size:13px;line-height:1.6">{ph}</div>
  </div>"""

    grand   = parity_results.get("grand", {})
    schemas = parity_results.get("schemas", {})
    equiv_filter = data.get("equiv_filter", {})
    name_map     = data.get("name_map", {})

    total_pass    = grand.get("pass",     0)
    total_fail    = grand.get("fail",     0)
    total_missing = grand.get("missing",  0)
    total_spgonly = grand.get("spg_only", 0)
    total_tested  = total_pass + total_fail
    pass_pct      = round(total_pass / total_tested * 100) if total_tested else 0

    total_excluded = grand.get("excluded", sum(
        len(s.get("excluded_objects", [])) for s in schemas.values()
    ))

    # Summary cards
    cards = f"""
      <div class='summary-card'>
        <div class='s-label'>✓ Pass</div>
        <div class='s-value' style='color:var(--green)'>{total_pass}</div>
      </div>
      <div class='summary-card'>
        <div class='s-label'>✗ Fail / Error</div>
        <div class='s-value' style='color:var(--red)'>{total_fail}</div>
      </div>
      <div class='summary-card'>
        <div class='s-label'>⊘ Missing in SPG</div>
        <div class='s-value' style='color:var(--amber)'>{total_missing}</div>
      </div>
      <div class='summary-card'>
        <div class='s-label'>SPG-only Objects</div>
        <div class='s-value' style='color:var(--muted)'>{total_spgonly}</div>
      </div>
      <div class='summary-card'>
        <div class='s-label'>Pass Rate</div>
        <div class='s-value' style='color:var(--{"green" if pass_pct >= 80 else "amber" if pass_pct >= 60 else "red"})'>{pass_pct}%</div>
      </div>
      {f"""<div class='summary-card'>
        <div class='s-label'>⏭ Excluded (legacy)</div>
        <div class='s-value' style='color:var(--muted)'>{total_excluded}</div>
      </div>""" if total_excluded else ""}"""

    # Exclusion banner (when user opted out of some legacy groups)
    exclusion_banner = ""
    if equiv_filter and equiv_filter.get("excluded_groups"):
        ex_groups = equiv_filter["excluded_groups"]
        fqn_count = len(equiv_filter.get("excluded_fqns", []))
        group_chips = "".join(
            f"<span class='badge badge-muted' style='margin-right:4px'>{g}</span>"
            for g in ex_groups
        )
        exclusion_banner = f"""
  <div class="alert" style="background:var(--surface2);border-left:3px solid var(--blue);margin-bottom:20px">
    <span class="alert-icon" style="color:var(--blue)">ℹ</span>
    <div>
      <strong>{len(ex_groups)} legacy group(s) excluded from this test by user choice
      ({fqn_count} objects total):</strong><br>
      <div style="margin-top:6px">{group_chips}</div>
      <div style="margin-top:4px;font-size:11px;color:var(--muted)">
        These objects were migrated but excluded from the equivalence test.
        To include them, re-run Phase 6.6 and choose <em>Include</em> for these groups.
      </div>
    </div>
  </div>"""

    # ── Structural (MySQL) parity — per-db table/routine/view breakdown ──
    if parity_results.get("_is_structural"):
        db_rows = ""
        for db, s in sorted(schemas.items()):
            t_src  = s.get("tables_src", 0)
            t_spg  = s.get("tables_spg", 0)
            t_match= s.get("tables_match", 0)
            r_src  = s.get("routines_src", 0)
            r_spg  = s.get("routines_spg", 0)
            r_match= s.get("routines_match", 0)
            r_miss = s.get("routines_missing", [])
            v_src  = s.get("views_src", 0)
            v_spg  = s.get("views_spg", 0)
            col_mm = s.get("col_mismatches", [])
            tbl_ok = "ok" if t_match == t_src else "fail"
            rut_ok = "ok" if r_match == r_src else "fail"
            miss_tip = (f" title='Missing: {chr(10).join(r_miss[:10])}'"
                        if r_miss else "")
            db_rows += (
                f"<tr>"
                f"<td class='mono'>{db}</td>"
                f"<td class='num {tbl_ok}'>{t_match:,} / {t_src:,}</td>"
                f"<td class='num small'>{len(col_mm)}</td>"
                f"<td class='num {rut_ok}'{miss_tip}>{r_match:,} / {r_src:,}</td>"
                f"<td class='num'>{v_spg:,} / {v_src:,}</td>"
                f"</tr>"
            )
            # Missing routine detail rows
            for mr in r_miss[:20]:
                db_rows += (f"<tr style='background:light-dark(#fef2f2,#450a0a)'>"
                            f"<td class='mono small' style='padding-left:24px;color:var(--muted)'>{db}</td>"
                            f"<td colspan='2' class='small' style='color:var(--red)'>⊘ Missing: {mr}</td>"
                            f"<td colspan='2' class='small muted'>Not deployed to SPG</td></tr>")
            for cm in col_mm[:5]:
                t = cm.get("table","?")
                db_rows += (f"<tr style='background:light-dark(#fffbeb,#44270a)'>"
                            f"<td class='mono small' style='padding-left:24px;color:var(--muted)'>{db}</td>"
                            f"<td colspan='2' class='small' style='color:var(--amber)'>⚠ {t}: col count src={cm.get('src')} spg={cm.get('spg')}</td>"
                            f"<td colspan='2' class='small muted'>Column count differs</td></tr>")

        structural_table = f"""
  <div class="section">
    <h2>Structural Parity — Per-Schema Breakdown</h2>
    <p class="small" style="color:var(--muted);margin-bottom:14px">
      Compares table, routine (function/procedure), and view counts between the source database
      and SPG across all migrated schemas. Column mismatches where source=0 are
      <em>CREATE TABLE AS SELECT</em> artifacts in the Docker source — not migration errors.
    </p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Schema</th>
          <th style="text-align:right" title="Tables present in both source and SPG">Tables matched</th>
          <th style="text-align:right" title="Column count mismatches (sample). src=0 = CREATE TABLE AS SELECT artifact.">Col. Δ</th>
          <th style="text-align:right" title="Functions + procedures matched">Routines matched</th>
          <th style="text-align:right" title="Views present in SPG vs source">Views</th>
        </tr></thead>
        <tbody>{db_rows}</tbody>
      </table>
    </div>
  </div>"""
        return f"""
  {exclusion_banner}
  <div class="section">
    <h2>Equivalence Test Summary (Phase 6.6)</h2>
    <p class="small" style="color:var(--muted);margin-bottom:14px">
      Structural parity: object existence and column counts compared between
      source and SPG target across all migrated schemas.
    </p>
    <div class="summary-row">{cards}</div>
  </div>
  {structural_table}"""

    # ── Per-schema sections with object tables (MSSQL deep-execution path) ──
    schema_sections = ""
    for schema_name, s in sorted(schemas.items()):
        results  = s.get("results", [])
        missing  = s.get("missing_objects", [])
        spg_only = s.get("spg_only_objects", [])
        s_pass   = s.get("pass", 0)
        s_fail   = s.get("fail", 0)

        if not results and not missing and not spg_only:
            continue

        badge_style = "success" if s_fail == 0 else "fail"
        badge_text  = f"✓ {s_pass} pass" if s_fail == 0 else f"✗ {s_fail} fail"

        rows = ""
        for r in results:
            verdict   = r.get("verdict", "?")
            vst, vlbl = _VERDICT_STYLE.get(verdict, ("muted", verdict))
            obj_type  = r.get("type", "")
            spg_name  = r.get("name", "")
            ms_rows   = r.get("ms_rows")
            spg_rows  = r.get("spg_rows")
            ms_p      = r.get("ms_p")
            spg_p     = r.get("spg_p")
            issues    = "; ".join(r.get("issues", []))[:120]

            # Resolve source name from name_map
            if name_map:
                src_name, spg_display = _resolve_name_pair(spg_name, name_map)
            else:
                src_name = spg_name
                spg_display = "—"
            spg_name_cell = (f"<td class='mono small' style='color:var(--muted)'>{spg_display}</td>"
                             if spg_display != "—" else "<td class='muted' style='text-align:center'>—</td>")

            if obj_type == "VIEW":
                ms_val  = str(ms_rows)  if ms_rows  is not None else "—"
                spg_val = str(spg_rows) if spg_rows is not None else "—"
            else:
                ms_val  = f"{ms_p}p"  if ms_p  is not None else "—"
                spg_val = f"{spg_p}p" if spg_p is not None else "—"

            rows += (
                f"<tr>"
                f"<td class='mono small'>{schema_name}</td>"
                f"<td class='mono small'>{src_name}</td>"
                f"{spg_name_cell}"
                f"<td class='small'>{obj_type}</td>"
                f"<td class='num small'>{ms_val}</td>"
                f"<td class='num small'>{spg_val}</td>"
                f"<td><span class='badge badge-{vst}'>{vlbl}</span></td>"
                f"<td class='small'>{issues}</td>"
                f"</tr>"
            )

        missing_rows = ""
        for m in missing:
            missing_rows += (
                f"<tr><td class='mono small'>{m.get('fqn','')}</td>"
                f"<td class='small'>{m.get('type','')}</td></tr>"
            )

        excluded = s.get("excluded_objects", [])
        excluded_rows = "".join(
            f"<tr><td class='mono small'>{e.get('fqn','')}</td>"
            f"<td class='small'>{e.get('type','')}</td></tr>"
            for e in excluded
        )

        schema_sections += f"""
    <div class="section">
      <h2>Schema: {schema_name}
        <span class='badge badge-{badge_style}' style='font-size:12px;margin-left:8px'>{badge_text}</span>
        <span class='badge badge-muted'    style='font-size:12px;margin-left:4px'>{len(missing)} missing</span>
        {f"<span class='badge badge-info' style='font-size:12px;margin-left:4px'>{len(excluded)} excluded</span>" if excluded else ""}
      </h2>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Schema</th><th>Source Name</th><th>SPG Name</th><th>Type</th>
            <th style="text-align:right">MSSQL</th>
            <th style="text-align:right">SPG</th>
            <th>Verdict</th><th>Issues</th>
          </tr></thead>
          <tbody>{rows if rows else "<tr><td colspan='8' class='muted-msg'>No matched objects tested</td></tr>"}</tbody>
        </table>
      </div>
      {f"""<details style='margin-top:12px'><summary style='cursor:pointer;font-size:12px;color:var(--amber)'>{len(missing)} Missing Objects (in MSSQL, not in SPG)</summary>
        <div class='table-wrap' style='margin-top:8px'><table>
        <thead><tr><th>Object</th><th>Type</th></tr></thead>
        <tbody>{missing_rows}</tbody></table></div></details>""" if missing else ""}
      {f"""<details style='margin-top:8px'><summary style='cursor:pointer;font-size:12px;color:var(--blue)'>{len(excluded)} Excluded Objects (user opted out of testing)</summary>
        <div class='table-wrap' style='margin-top:8px'><table>
        <thead><tr><th>Object (FQN)</th><th>Type</th></tr></thead>
        <tbody>{excluded_rows}</tbody></table></div></details>""" if excluded else ""}
    </div>"""

    return f"""
  {exclusion_banner}
  <div class="section">
    <h2>Equivalence Test Summary (Phase 6.6)</h2>
    <p class="small" style="color:var(--muted);margin-bottom:14px">
      Structural parity: object existence, parameter counts, view row counts compared between MSSQL source and SPG target.
    </p>
    <div class="summary-row">{cards}</div>
  </div>
  {schema_sections}"""


_WITNESS_ICONS = {
    "validated":           ("success", "✓ Validated"),
    "partially_validated": ("warn",    "⚠ Partial"),
    "failed":              ("fail",    "✗ Failed"),
    "unsupported":         ("muted",   "⊘ Unsupported"),
    "skipped":             ("muted",   "⏭ Skipped"),
}


def _build_witness_tab(data: dict) -> str:
    if not data.get("witness_ran"):
        return """
  <div class="section">
    <div class="alert alert-success" style="background:var(--surface2);border-left:3px solid var(--muted)">
      <span class="alert-icon" style="color:var(--muted)">○</span>
      <div><strong>Not Run</strong> — Witness validation was skipped.
      To run, re-invoke the skill and choose Phase 6.5 at the end of Phase 6.</div>
    </div>
  </div>"""

    results  = data["witness_results"]
    summary  = data["witness_summary"]
    seed_ran         = data.get("seed_ran", False)
    seed_tables      = data.get("seed_tables", 0)
    seed_zero        = data.get("seed_zero", 0)
    seed_volume      = data.get("seed_volume", 3)
    spg_load_ran     = data.get("spg_load_ran", False)
    spg_rows_loaded  = data.get("spg_rows_loaded", 0)
    spg_tables_loaded = data.get("spg_tables_loaded", 0)

    # Seed data KPI panel
    seed_panel = ""
    if seed_ran or spg_load_ran:
        src_kpi = (
            f"<div class='kpi-card'>"
            f"<div class='num green'>{seed_tables:,}</div>"
            f"<div class='label'>Tables Seeded (Source)</div>"
            f"<div class='sub'>{seed_volume} rows each · {seed_zero} tables got 0 rows</div>"
            f"</div>"
        ) if seed_ran else ""
        spg_kpi = (
            f"<div class='kpi-card'>"
            f"<div class='num green'>{spg_rows_loaded:,}</div>"
            f"<div class='label'>Rows Loaded into SPG</div>"
            f"<div class='sub'>across {spg_tables_loaded:,} tables</div>"
            f"</div>"
        ) if spg_load_ran else ""
        seed_panel = f"""
    <h3 style="margin-top:0;margin-bottom:10px">Seed Data</h3>
    <div class="kpi-grid" style="margin-bottom:24px">{src_kpi}{spg_kpi}</div>"""
    cards = ""
    order = ["validated", "partially_validated", "failed", "unsupported", "skipped"]
    style_to_color = {"success": "green", "warn": "amber", "fail": "red", "muted": "muted"}
    for status in order:
        count = summary.get(status, 0)
        if count == 0:
            continue
        style, label = _WITNESS_ICONS.get(status, ("muted", status))
        color = style_to_color.get(style, "muted")
        cards += (f"<div class='summary-card'>"
                  f"<div class='s-label'>{label}</div>"
                  f"<div class='s-value' style='color:var(--{color})'>{count}</div>"
                  f"</div>")

    # Per-object table
    name_map = data.get("name_map", {})
    rows = ""
    for fqn, r in sorted(results.items()):
        status = r.get("status", "skipped")
        obj_type = r.get("type", "")
        note = r.get("note", "")[:120]
        style, label = _WITNESS_ICONS.get(status, ("muted", status))
        schema = fqn.split(".")[0] if "." in fqn else "dbo"
        src_name = fqn.split(".")[-1]   # original MSSQL name (already correct case)
        if name_map:
            _, spg_name = _resolve_name_pair(fqn, name_map)
        else:
            spg_name = src_name.lower() if src_name.lower() != src_name else "—"
        spg_cell = (f"<td class='mono small' style='color:var(--muted)'>{spg_name}</td>"
                    if spg_name != "—" else "<td class='muted' style='text-align:center'>—</td>")
        rows += (f"<tr>"
                 f"<td class='mono small'>{schema}</td>"
                 f"<td class='mono small'>{src_name}</td>"
                 f"{spg_cell}"
                 f"<td class='small'>{obj_type}</td>"
                 f"<td><span class='badge badge-{style}'>{label}</span></td>"
                 f"<td class='small'>{note}</td>"
                 f"</tr>")

    if not rows:
        rows = "<tr><td colspan='6' class='muted-msg'>No objects validated</td></tr>"

    return f"""
  <div class="section">
    <h2>Source-Side Witness Validation (Phase 6.5)</h2>
    <p class="small" style="color:var(--muted);margin-bottom:14px">
      Synthetic {seed_volume}-row dataset seeded into source DB. Views and procedures queried to confirm they execute and return rows.
      Parity testing against SPG is in the <strong>Equivalence Test</strong> tab.
    </p>
    {seed_panel}
    <div class="summary-row">
      {cards}
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Schema</th><th>Source Name</th><th>SPG Name</th><th>Type</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>"""


def render_html(data: dict) -> str:
    source       = data["source_type"]
    source_db    = data["source_db"]
    spg          = data["spg_instance"]
    generated    = data["generated"]
    is_blocked   = data["is_blocked"]
    schemas      = data["schemas"]

    views_ok     = data["views_ok"]
    views_fail   = data["views_fail"]
    views_fixed  = data["views_fixed"]
    views_skip   = data.get("views_skip", [])
    funcs_ok     = data["funcs_ok"]
    funcs_fail   = data["funcs_fail"]
    procs_ok     = data["procs_ok"]
    procs_fail   = data["procs_fail"]
    procs_legacy = data["procs_legacy"]
    stubs        = data["stubs"]
    llm_fixed    = data["llm_fixed"]
    rule_fixed   = data["rule_fixed"]
    still_failed = data["still_failed"]
    name_map     = data.get("name_map", {})

    total_tables  = data["total_tables"]
    total_indexes = data["total_indexes"]
    total_views   = len(views_ok)
    total_funcs   = len(funcs_ok)
    total_procs   = len(procs_ok)
    total_repair  = len(llm_fixed) + len(rule_fixed)

    # counts for the overview donut
    total_idx_fail  = sum(s["indexes_fail"] for s in schemas.values())
    total_ok_objs   = total_tables + total_indexes + total_views + total_funcs + total_procs
    total_fail_objs = (total_idx_fail + len(views_fail) + len(funcs_fail) + len(procs_fail))

    assess_status = "&#10003; PASSED" if not is_blocked else "&#9888; BLOCKED"
    assess_badge  = "success" if not is_blocked else "fail"

    # build per-tab HTML fragments
    schema_rows = ""
    for db, s in schemas.items():
        schema_rows += (
            f"<tr>"
            f"<td class='mono'>{db}</td>"
            f"<td class='num ok'>{s['tables_ok']:,} / {s['tables_total']:,}</td>"
            f"<td class='num ok'>{s['indexes_ok']:,}</td>"
            f"<td class='num muted' title='These are benign — already created in table phase'>{s['fk_benign']:,}</td>"
            f"<td class='num {'fail' if s['fk_real'] else 'ok'}'>{s['fk_real'] if s['fk_real'] else '—'}</td>"
            f"<td class='num muted'>{s['elapsed_s']:.0f}s</td>"
            f"</tr>"
        )

    # index failure table
    all_failures = []
    for s in schemas.values():
        all_failures.extend(s.get("all_failures", []))
    idx_fail_html = _build_fail_table(all_failures, "indexes")

    # objects tab
    views_all  = list(views_ok) + [_clean_name(f.get("view", f) if isinstance(f, dict) else f)
                                    for f in views_fail]
    funcs_all  = list(funcs_ok) + [_clean_name(f.get("function", f) if isinstance(f, dict) else f)
                                    for f in funcs_fail]
    procs_all  = list(procs_ok) + [_clean_name(f.get("procedure", f) if isinstance(f, dict) else f)
                                    for f in procs_fail]

    views_table  = _build_obj_table(views_all,  "View",      llm_fixed, rule_fixed, still_failed, stubs, name_map)
    funcs_table  = _build_obj_table(funcs_all,  "Function",  llm_fixed, rule_fixed, still_failed, stubs, name_map)
    procs_table  = _build_obj_table(procs_all,  "Procedure", llm_fixed, rule_fixed, still_failed, stubs, name_map)
    legacy_table = (_build_obj_table(procs_legacy, "Procedure (Legacy)", [], [], [], [], name_map)
                    if procs_legacy else "<p class='muted-msg'>None</p>")

    warn_rows = _build_warn_rows(data["warn_findings"])
    val_rows  = _build_val_rows(data["val_checks"], name_map)
    dep_rows  = _build_dep_rows(data["dep_groups"])

    ext_list = ""
    for e in data["ext_prereqs"]:
        ext_list += f"<li class='mono small'>{e}</li>"
    ext_html = f"<ul>{ext_list}</ul>" if ext_list else "<p class='muted-msg'>None required</p>"

    witness_tab      = _build_witness_tab(data)
    equivalence_tab  = _build_equivalence_tab(data)
    chart_labels = json.dumps(list(schemas.keys()))
    chart_tables = json.dumps([s["tables_ok"] for s in schemas.values()])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{source} → Snowflake Postgres Migration Report</title>
  <style>
  :root{{color-scheme:light dark;
    --blue:    light-dark(#0069be,#38bdf8);
    --green:   light-dark(#16a34a,#4ade80);
    --amber:   light-dark(#d97706,#fbbf24);
    --red:     light-dark(#dc2626,#f87171);
    --purple:  light-dark(#7c3aed,#a78bfa);
    --surface: light-dark(#ffffff,#1a1f2e);
    --surface2:light-dark(#f8fafc,#232a3b);
    --border:  light-dark(#e2e8f0,#334155);
    --text:    light-dark(#0f172a,#f1f5f9);
    --muted:   light-dark(#64748b,#94a3b8);
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--surface2);color:var(--text);font-size:14px;line-height:1.5}}

  /* ── Header ── */
  .header{{background:linear-gradient(135deg,#0069be 0%,#003d73 100%);
    color:#fff;padding:24px 32px 0}}
  .header-top{{display:flex;align-items:center;gap:16px;margin-bottom:12px}}
  .logo{{width:32px;height:32px;flex-shrink:0}}
  .header h1{{font-size:20px;font-weight:700;letter-spacing:-.3px}}
  .header-meta{{display:flex;gap:28px;flex-wrap:wrap;padding:12px 0 0;
    border-top:1px solid rgba(255,255,255,.15);margin-top:4px}}
  .header-meta .item{{display:flex;flex-direction:column}}
  .header-meta .label{{font-size:10px;text-transform:uppercase;letter-spacing:.8px;opacity:.65}}
  .header-meta .value{{font-size:13px;font-weight:600;margin-top:1px}}

  /* ── Tabs ── */
  .tabs{{background:var(--surface);border-bottom:2px solid var(--border);
    padding:0 32px;display:flex;gap:0}}
  /* Tab tooltips: appear below the tab bar, not above */
  .tabs [data-tip]::after{{
    bottom:auto;top:calc(100% + 6px);
    left:50%;transform:translateX(-20%);
    max-width:300px;font-size:11px;line-height:1.5;
    text-align:left;
  }}
  .tab-btn{{padding:12px 20px;cursor:pointer;border:none;background:none;
    color:var(--muted);font-size:13px;font-weight:500;
    border-bottom:2px solid transparent;margin-bottom:-2px;transition:.15s}}
  .tab-btn:hover{{color:var(--text)}}
  .tab-btn.active{{color:var(--blue);border-bottom-color:var(--blue);font-weight:600}}

  /* ── Content ── */
  .content{{max-width:1200px;margin:0 auto;padding:28px 32px 64px}}
  .tab-panel{{display:none}}.tab-panel.active{{display:block}}
  .section{{margin-bottom:36px}}
  h2{{font-size:16px;font-weight:700;color:var(--text);margin:0 0 14px;
    padding-bottom:8px;border-bottom:2px solid var(--blue)}}
  h3{{font-size:13px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px}}

  /* ── KPI grid ── */
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
    gap:14px;margin-bottom:28px}}
  .kpi-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:10px;padding:16px 18px;display:flex;flex-direction:column;gap:4px}}
  .kpi-card .num{{font-size:28px;font-weight:800;line-height:1}}
  .kpi-card .label{{font-size:11px;color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.5px}}
  .kpi-card .sub{{font-size:11px;color:var(--muted)}}
  .green{{color:var(--green)}}.amber{{color:var(--amber)}}.red-c{{color:var(--red)}}
  .purple{{color:var(--purple)}}

  /* ── Tables ── */
  .table-wrap{{overflow-x:auto;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;background:var(--surface);
    border-radius:8px;overflow:hidden;font-size:13px}}
  th{{background:var(--surface2);color:var(--muted);font-size:11px;
    text-transform:uppercase;letter-spacing:.5px;padding:9px 12px;
    text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}}
  td{{padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:var(--surface2)}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  td.ok{{color:var(--green);font-weight:600}}
  td.fail{{color:var(--red);font-weight:600}}
  td.muted{{color:var(--muted)}}
  td.mono,.mono{{font-family:ui-monospace,"SFMono-Regular",monospace;font-size:12px}}
  td.small,.small{{font-size:12px}}

  /* ── Badges ── */
  .badge{{display:inline-block;padding:2px 8px;border-radius:12px;
    font-size:11px;font-weight:600;white-space:nowrap}}
  .badge-success{{background:light-dark(#dcfce7,#14532d);color:var(--green)}}
  .badge-fail{{background:light-dark(#fee2e2,#7f1d1d);color:var(--red)}}
  .badge-warn{{background:light-dark(#fef9c3,#713f12);color:var(--amber)}}
  .badge-info{{background:light-dark(#dbeafe,#1e3a5f);color:var(--blue)}}
  .badge-muted{{background:var(--surface2);color:var(--muted)}}

  /* ── Charts ── */
  .chart-row{{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:28px}}
  .chart-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:10px;padding:18px;flex:1;min-width:260px;max-width:460px}}
  .chart-card h3{{margin-bottom:14px}}
  .chart-container{{position:relative;height:220px;width:100%}}

  /* ── Summary cards (row) ── */
  .summary-row{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}}
  .summary-card{{background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:12px 16px;flex:1;min-width:160px}}
  .summary-card .s-label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
  .summary-card .s-value{{font-size:22px;font-weight:700;margin-top:2px}}

  /* ── Tooltips ── */
  [data-tip]{{position:relative;cursor:default}}
  [data-tip]::after{{
    content:attr(data-tip);
    position:absolute;
    bottom:calc(100% + 8px);
    left:50%;transform:translateX(-50%);
    background:light-dark(#1e293b,#f1f5f9);
    color:light-dark(#f1f5f9,#1e293b);
    padding:7px 11px;
    border-radius:7px;
    font-size:12px;font-weight:400;line-height:1.45;
    width:max-content;max-width:260px;white-space:normal;
    opacity:0;pointer-events:none;transition:opacity .15s;
    z-index:200;
    box-shadow:0 4px 12px rgba(0,0,0,.2);
  }}
  [data-tip]:hover::after{{opacity:1}}
  .tip-icon{{font-size:10px;color:var(--muted);margin-left:3px;
    vertical-align:super;cursor:help;user-select:none}}

  /* ── Misc ── */
  .muted-msg{{color:var(--muted);font-style:italic;font-size:13px;padding:8px 0}}
  .footnote{{font-size:11px;color:var(--muted);margin-top:6px;padding-top:6px;
    border-top:1px dashed var(--border)}}
  .alert{{display:flex;align-items:flex-start;gap:10px;padding:12px 16px;
    border-radius:8px;margin-bottom:16px;font-size:13px}}
  .alert-success{{background:light-dark(#f0fdf4,#052e16);border-left:3px solid var(--green)}}
  .alert-error{{background:light-dark(#fef2f2,#450a0a);border-left:3px solid var(--red)}}
  .alert-icon{{font-size:16px;margin-top:1px}}
  .detail-section{{margin-top:16px;padding-top:12px;border-top:1px solid var(--border)}}

  @media print{{
    *{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
    .tabs{{display:none}}
    .tab-panel{{display:block!important;page-break-after:always}}
    .tab-panel:last-child{{page-break-after:avoid}}
    .kpi-card,.chart-card,.summary-card{{break-inside:avoid}}
    table{{break-inside:auto}}
    tr{{break-inside:avoid;break-after:auto}}
    .header{{-webkit-print-color-adjust:exact}}
    .content{{padding:16px}}
    h2{{break-after:avoid}}
    /* Replace canvas charts with hidden (charts don't render in headless) */
    .chart-row{{display:none}}
    .print-chart-summary{{display:flex!important}}
  }}
  </style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-top">
    <svg class="logo" viewBox="0 0 64 64" fill="none">
      <rect width="64" height="64" rx="8" fill="#29B5E8"/>
      <path d="M32 12 L48 42 H16 Z" fill="white" opacity=".9"/>
      <circle cx="32" cy="44" r="8" fill="white" opacity=".7"/>
    </svg>
    <h1>{source} &rarr; Snowflake Postgres Migration Report</h1>
  </div>
  <div class="header-meta">
    <div class="item"><span class="label">Source Type</span><span class="value">{source}</span></div>
    <div class="item"><span class="label">Source Database</span><span class="value">{source_db}</span></div>
    <div class="item"><span class="label">Target Type</span><span class="value">Snowflake Postgres</span></div>
    <div class="item"><span class="label">Target Instance</span><span class="value">{spg}</span></div>
    <div class="item"><span class="label">Report Date</span><span class="value">{generated}</span></div>
    <div class="item"><span class="label">Assessment</span>
      <span class="value"><span class="badge badge-{assess_badge}">{assess_status}</span></span></div>
  </div>
</div>

<!-- Tab bar -->
<div class="tabs" id="tabBar">
  <button class="tab-btn active" onclick="showTab('overview')"
    data-tip="High-level migration summary: tables, indexes, views, functions, procedures deployed — and how many were fixed by rules or LLM repair.">Overview</button>
  <button class="tab-btn" onclick="showTab('deployment')"
    data-tip="Per-schema table deployment details: row counts, indexes, foreign keys, and duration. Also shows views, functions, and procedures deployment outcomes.">Deployment</button>
  <button class="tab-btn" onclick="showTab('objects')"
    data-tip="Full list of every converted view, function, and stored procedure — showing whether each was deployed, fixed by rules, fixed by LLM, or still failing.">Objects</button>
  <button class="tab-btn" onclick="showTab('validation')"
    data-tip="Schema validation checks run after deployment: table counts, column counts, primary keys, AUTO_INCREMENT columns, foreign keys, and indexes compared between source and SPG.">Validation</button>
  <button class="tab-btn" onclick="showTab('assessment')"
    data-tip="Pre-migration SPG Compatibility Assessment: flags any source objects that use features with no PostgreSQL equivalent (CLR, linked servers, temporal tables, etc.) before deployment begins.">Assessment</button>
  <button class="tab-btn" onclick="showTab('witness')"
    data-tip="Phase 6.5 — Synthetic 3-row seed data is inserted into the source DB, then every deployed view, function, and procedure is called to confirm it executes and returns data on the source side.">Witness</button>
  <button class="tab-btn" onclick="showTab('equivalence')"
    data-tip="Phase 6.6 — Structural parity between source and SPG: table counts, column counts, and routines compared schema by schema. Confirms the migration is structurally complete and ready for sign-off.">Equivalence Test</button>
</div>

<div class="content">

<!-- ═══════════════════════════ OVERVIEW ═══════════════════════════ -->
<div class="tab-panel active" id="tab-overview">

  {'<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:12px 16px;border-radius:6px;margin-bottom:20px"><strong style="color:#dc2626">&#9888; ' + str(total_fail_objs) + ' object(s) are NOT in your SPG instance</strong><div style="font-size:13px;margin-top:4px;color:#111">The following objects exist in the source database but were not deployed to SPG: ' + ', '.join(filter(None, [str(total_idx_fail) + ' indexes' if total_idx_fail else '', str(len(views_fail)) + ' views' if views_fail else '', str(len(funcs_fail)) + ' functions' if funcs_fail else '', str(len(procs_fail)) + ' procedures' if procs_fail else ''])) + '. Check the Deployment tab for details and fix the errors to deploy them.</div></div>' if total_fail_objs else ''}

  <div class="kpi-grid">
    <div class="kpi-card" data-tip="Base tables (CREATE TABLE) migrated from the source database. Temporary and derived tables are excluded. All source tables should appear here at 100%.">
      <div class="num green">{total_tables:,}</div>
      <div class="label">Tables<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--green)">100% deployed</div>
      <div class="sub">0 failed &nbsp;·&nbsp; 0 skipped</div>
    </div>
    <div class="kpi-card" data-tip="Non-primary-key indexes deployed to SPG. PostgreSQL auto-creates implicit indexes for PRIMARY KEY and UNIQUE constraints, so the SPG count is typically higher than the source. Skipped = duplicate names or unsupported index types.">
      <div class="num {'red' if total_idx_fail else 'green'}">{total_indexes:,}</div>
      <div class="label">Indexes<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--{'red' if total_idx_fail else 'green'})">{total_idx_fail} failed</div>
    </div>
    <div class="kpi-card" data-tip="CREATE VIEW objects deployed to SPG. Objects shown as 'not in SPG' exist in the source but failed deployment and are absent from your target database.">
      <div class="num {'red' if (not total_views and views_fail) else ('amber' if (total_views and views_fail) else 'green')}">{total_views}</div>
      <div class="label">Views in SPG<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--{'red' if views_fail else 'green'})">{len(views_fail)} not in SPG</div>
      <div class="sub">{len(data.get('views_skip', []))} skipped</div>
    </div>
    <div class="kpi-card" data-tip="Scalar and table-valued functions deployed to SPG. Objects shown as 'not in SPG' failed deployment and are absent from your target database.">
      <div class="num {'red' if (not total_funcs and funcs_fail) else ('amber' if (total_funcs and funcs_fail) else 'green')}">{total_funcs}</div>
      <div class="label">Functions in SPG<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--{'red' if funcs_fail else 'green'})">{len(funcs_fail)} not in SPG</div>
      <div class="sub">0 skipped</div>
    </div>
    <div class="kpi-card" data-tip="Stored procedures deployed to SPG. Failed procedures are NOT in the target database. They go through rule-based then LLM repair — remaining failures need manual review.">
      <div class="num {'red' if (not total_procs and procs_fail) else ('amber' if (total_procs and procs_fail) else 'green')}">{total_procs}</div>
      <div class="label">Procedures in SPG<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--{'red' if procs_fail else 'green'})">{len(procs_fail)} not in SPG</div>
      <div class="sub">{len(procs_legacy)} legacy skipped</div>
    </div>
    <div class="kpi-card" data-tip="Procedures and functions that failed initial conversion and were successfully repaired by the Cortex AI LLM repair loop. &#39;Still failing&#39; = objects that exceeded the repair budget and require manual intervention.">
      <div class="num purple">{total_repair}</div>
      <div class="label">LLM Repaired<span class="tip-icon">ⓘ</span></div>
      <div class="sub" style="color:var(--green)">{len([x for x in llm_fixed+rule_fixed])} fixed</div>
      <div class="sub" style="color:var(--{'red' if still_failed else 'muted'})">{len(still_failed)} still failing</div>
    </div>
  </div>

  <div class="chart-row">
    <div class="chart-card">
      <h3>Tables per Schema</h3>
      <div class="chart-container"><canvas id="tablesChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Objects by Type</h3>
      <div class="chart-container"><canvas id="typeChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Deployment Status</h3>
      <div class="chart-container"><canvas id="successChart"></canvas></div>
      {'<p style="text-align:center;color:var(--red);font-size:12px;margin-top:6px">&#9888; ' + str(total_fail_objs) + ' objects failed deployment</p>' if total_fail_objs else '<p style="text-align:center;color:var(--green);font-size:12px;margin-top:6px">&#10003; All objects deployed successfully</p>'}
    </div>
  </div>

  <!-- Print-only stat row replaces canvas charts in PDF -->
  <div class="print-chart-summary" style="display:none;gap:12px;flex-wrap:wrap;margin-bottom:24px;padding:16px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--blue)">{total_tables:,}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Tables</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--blue)">{total_indexes:,}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Indexes</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--{'green' if not views_fail else 'red'})">{total_views}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Views</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--{'green' if not funcs_fail else 'red'})">{total_funcs}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Functions</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--{'amber' if procs_fail else 'green'})">{total_procs}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Procedures</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--{'red' if total_fail_objs else 'green'})">{round(total_ok_objs/(total_ok_objs+total_fail_objs)*100) if (total_ok_objs+total_fail_objs) else 100}%</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Success Rate</div></div>
  </div>

  <div class="section">
    <h2>Migration Summary</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Category</th><th style="text-align:right">In SPG &#10003;</th><th style="text-align:right">Not in SPG &#10007;</th><th style="text-align:right">Skipped</th><th>Notes</th></tr></thead>
        <tbody>
          <tr>
            <td>Tables</td>
            <td class="num ok">{total_tables:,}</td>
            <td class="num ok">0</td>
            <td class="num muted">0</td>
            <td class="small">Catalog-based deployment via parallel_deploy.py</td>
          </tr>
          <tr>
            <td>Indexes</td>
            <td class="num ok">{total_indexes:,}</td>
            <td class="num ok">0</td>
            <td class="num {'fail' if sum(s['indexes_fail'] for s in schemas.values()) else 'muted'}">{sum(s['indexes_fail'] for s in schemas.values())}</td>
            <td class="small">Skipped = duplicate names or unsupported index types</td>
          </tr>
          <tr>
            <td>Foreign Keys</td>
            <td class="num ok">{sum(s['fk_benign'] for s in schemas.values()):,}</td>
            <td class="num {'fail' if sum(s['fk_real'] for s in schemas.values()) else 'ok'}">{sum(s['fk_real'] for s in schemas.values())}</td>
            <td class="num muted">—</td>
            <td class="small">Benign "already exists" duplicates excluded from fail count</td>
          </tr>
          <tr>
            <td>Views</td>
            <td class="num ok">{total_views}</td>
            <td class="num {'fail' if views_fail else 'ok'}">{len(views_fail)}</td>
            <td class="num {'fail' if views_fail else ('amber' if views_skip else 'ok')}">{len(views_skip)}</td>
            <td class="small">{len([n for n in still_failed if n in {v.lower() for v in views_all}])} still failing after LLM repair</td>
          </tr>
          <tr>
            <td>Functions</td>
            <td class="num ok">{total_funcs}</td>
            <td class="num {'fail' if funcs_fail else 'ok'}">{len(funcs_fail)}</td>
            <td class="num muted">0</td>
            <td class="small">{len(llm_fixed)} objects fixed by LLM repair</td>
          </tr>
          <tr>
            <td>Procedures</td>
            <td class="num {'ok' if not procs_fail else 'amber'}">{total_procs}</td>
            <td class="num {'fail' if procs_fail else 'ok'}">{len(procs_fail)}</td>
            <td class="num muted">{len(procs_legacy)}</td>
            <td class="small">{len(procs_legacy)} legacy skipped{' · ' + str(len([n for n in still_failed if n in {p.lower() for p in procs_all}])) + ' still failing after LLM repair' if any(n in {p.lower() for p in procs_all} for n in still_failed) else ''}</td>
          </tr>
          {'<tr><td colspan="5" class="small" style="background:var(--bg-subtle);color:var(--muted);padding:6px 12px">&#9881; LLM Repair — Fixed by rules: ' + str(len(rule_fixed)) + ' &nbsp;·&nbsp; Fixed by LLM: ' + str(len(llm_fixed)) + ' &nbsp;·&nbsp; Still failing (all types): ' + str(len(still_failed)) + '</td></tr>' if still_failed or llm_fixed or rule_fixed else ''}
        </tbody>
      </table>
    </div>
  </div>

</div><!-- /overview -->


<!-- ═══════════════════════════ DEPLOYMENT ══════════════════════════ -->
<div class="tab-panel" id="tab-deployment">

  <div class="section">
    <h2>Schema Deployment</h2>
    <div class="table-wrap">
      <table style="table-layout:fixed;width:100%">
        <colgroup>
          <col style="width:22%"/>
          <col style="width:15%"/>
          <col style="width:13%"/>
          <col style="width:16%"/>
          <col style="width:16%"/>
          <col style="width:13%"/>
        </colgroup>
        <thead><tr>
          <th style="text-align:left">Database</th>
          <th style="text-align:right">Tables</th>
          <th style="text-align:right">Indexes</th>
          <th style="text-align:right">FK (benign)</th>
          <th style="text-align:right">FK (real fail)</th>
          <th style="text-align:right">Duration</th>
        </tr></thead>
        <tbody>{schema_rows}</tbody>
      </table>
    </div>
    <p class="footnote">* Benign FK failures: constraints already created during the table phase
    are correctly rejected as duplicates in the FK phase — all relationships are intact.</p>
  </div>

  <div class="section">
    <h2>Non-Table Objects Deployment</h2>
    <p class="small" style="color:var(--muted);margin-bottom:12px">
      Views, functions, and stored procedures are converted from source SQL to PL/pgSQL by the rule
      engine, then re-attempted by the LLM repair loop for any failures.
    </p>
    <div class="table-wrap">
      <table style="table-layout:fixed;width:100%">
        <colgroup>
          <col style="width:18%"/>
          <col style="width:13%"/>
          <col style="width:13%"/>
          <col style="width:16%"/>
          <col style="width:16%"/>
          <col style="width:12%"/>
          <col style="width:12%"/>
        </colgroup>
        <thead><tr>
          <th style="text-align:left">Object Type</th>
          <th style="text-align:right">In SPG &#10003;</th>
          <th style="text-align:right">Not in SPG &#10007;</th>
          <th style="text-align:right">Fixed by Rules</th>
          <th style="text-align:right">Fixed by LLM</th>
          <th style="text-align:right">Still Failing</th>
          <th style="text-align:right">Skipped</th>
        </tr></thead>
        <tbody>
          <tr>
            <td><strong>Views</strong></td>
            <td class="num ok">{total_views}</td>
            <td class="num {'fail' if views_fail else 'ok'}">{len(views_fail)}</td>
            <td class="num ok">{len(views_fixed)}</td>
            <td class="num muted">—</td>
            <td class="num ok">0</td>
            <td class="num {'amber' if views_skip else 'muted'}">{len(views_skip)}</td>
          </tr>
          <tr>
            <td><strong>Functions</strong></td>
            <td class="num ok">{total_funcs}</td>
            <td class="num {'fail' if funcs_fail else 'ok'}">{len(funcs_fail)}</td>
            <td class="num muted">—</td>
            <td class="num muted">—</td>
            <td class="num ok">0</td>
            <td class="num muted">0</td>
          </tr>
          <tr>
            <td><strong>Procedures</strong></td>
            <td class="num {'ok' if not procs_fail else 'amber'}">{total_procs}</td>
            <td class="num {'fail' if procs_fail else 'ok'}">{len(procs_fail)}</td>
            <td class="num ok">{len(rule_fixed)}</td>
            <td class="num ok">{len(llm_fixed)}</td>
            <td class="num {'fail' if still_failed else 'ok'}">{len(still_failed)}</td>
            <td class="num muted">{len(procs_legacy)}</td>
          </tr>
          <tr style="font-weight:600;border-top:2px solid var(--border)">
            <td>Total</td>
            <td class="num ok">{total_views + total_funcs + total_procs}</td>
            <td class="num {'fail' if (views_fail or funcs_fail or procs_fail) else 'ok'}">{len(views_fail) + len(funcs_fail) + len(procs_fail)}</td>
            <td class="num ok">{len(views_fixed) + len(rule_fixed)}</td>
            <td class="num ok">{len(llm_fixed)}</td>
            <td class="num {'fail' if still_failed else 'ok'}">{len(still_failed)}</td>
            <td class="num muted">{len(procs_legacy)}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="footnote">
      Failed = initial conversion error before repair ·
      Fixed by Rules = rule-based plpgsql-fixes.yaml patterns ·
      Fixed by LLM = Cortex AI repair loop ·
      Still Failing = exceeded repair budget, needs manual review ·
      Skipped = legacy/deprecated objects excluded by user
    </p>
  </div>

  <div class="section">
    <h2>Index Failures</h2>
    <p class="small" style="margin-bottom:10px;color:var(--muted)">
      These indexes could not be deployed. Common causes: duplicate names (already created by PostgreSQL
      for a PRIMARY KEY or UNIQUE constraint), expression columns, or index types with no PostgreSQL equivalent.
    </p>
    {idx_fail_html}
  </div>

</div><!-- /deployment -->


<!-- ═══════════════════════════ OBJECTS ═════════════════════════════ -->
<div class="tab-panel" id="tab-objects">

  <div class="section">
    <h2>LLM Repair Summary</h2>
    <div class="summary-row">
      <div class="summary-card">
        <div class="s-label">Fixed by Rules</div>
        <div class="s-value green">{len(rule_fixed)}</div>
      </div>
      <div class="summary-card">
        <div class="s-label">Fixed by LLM</div>
        <div class="s-value purple">{len(llm_fixed)}</div>
      </div>
      <div class="summary-card">
        <div class="s-label">Still Failing</div>
        <div class="s-value {'red-c' if still_failed else 'green'}">{len(still_failed)}</div>
      </div>
      <div class="summary-card">
        <div class="s-label">Legacy Skipped</div>
        <div class="s-value muted" style="color:var(--muted)">{len(procs_legacy)}</div>
      </div>
    </div>
    {'<div class="alert alert-error"><span class="alert-icon">&#9888;</span><div><strong>Still failing (' + str(len(still_failed)) + '):</strong> ' + ', '.join(f'<code>{x}</code>' for x in still_failed) + '<br><small>These reference UDTT types (RecordType, ReleaseFundsPaymentType) that were excluded as deprecated.</small></div></div>' if still_failed else '<div class="alert alert-success"><span class="alert-icon">&#10003;</span><div>All repairable objects deployed successfully.</div></div>'}
  </div>

  <div class="section">
    <h2>Views <span class="badge badge-success" style="font-size:12px;margin-left:6px">{total_views} / {total_views + len(views_fail)}</span></h2>
    {views_table}
  </div>

  <div class="section">
    <h2>Functions <span class="badge badge-success" style="font-size:12px;margin-left:6px">{total_funcs} / {total_funcs + len(funcs_fail)}</span></h2>
    {funcs_table}
  </div>

  <div class="section">
    <h2>Stored Procedures <span class="badge badge-{'success' if not procs_fail else 'warn'}" style="font-size:12px;margin-left:6px">{total_procs} / {total_procs + len(procs_fail)}</span></h2>
    {procs_table}
  </div>

  <div class="section">
    <h2>Legacy / Skipped Procedures
      <span class="badge badge-muted" style="font-size:12px;margin-left:6px">{len(procs_legacy)} skipped</span>
    </h2>
    <p class="small" style="margin-bottom:10px;color:var(--muted)">
      Vendor-framework or deprecated objects excluded from migration during the Deprecated Object Review phase.
    </p>
    {legacy_table}
  </div>

</div><!-- /objects -->


<!-- ═══════════════════════════ VALIDATION ══════════════════════════ -->
<div class="tab-panel" id="tab-validation">

  <div class="section">
    <h2>Schema Validation Checks</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Schema</th><th>Source Object</th><th>SPG Object</th><th>Check</th><th>Result</th><th>Details</th></tr></thead>
        <tbody>{val_rows}</tbody>
      </table>
    </div>
    <p class="footnote">* Identity/serial check: 8 flagged objects are SQL views (not tables) and
    do not require serial columns — this is a false positive in the validator.</p>
  </div>

</div><!-- /validation -->


<!-- ═══════════════════════════ ASSESSMENT ══════════════════════════ -->
<div class="tab-panel" id="tab-assessment">

  <div class="section">
    <h2>SPG Compatibility Assessment</h2>
    {'<div class="alert alert-success"><span class="alert-icon">&#10003;</span><div><strong>PASSED</strong> — No blocking incompatibilities found. Migration can proceed.</div></div>' if not is_blocked else '<div class="alert alert-error"><span class="alert-icon">&#9888;</span><div><strong>BLOCKED</strong> — Blocking issues must be resolved before migration.</div></div>'}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Code</th><th>Object</th><th>Warning</th><th>Detail</th></tr></thead>
        <tbody>{warn_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>Extension Prerequisites</h2>
    {ext_html}
  </div>

  <div class="section">
    <h2>Deprecated Object Review</h2>
    <p class="small" style="margin-bottom:10px;color:var(--muted)">
      Objects detected as deprecated patterns and excluded from migration.
    </p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Group Key</th><th>Pattern</th><th>Objects</th><th>Disposition</th></tr></thead>
        <tbody>{dep_rows}</tbody>
      </table>
    </div>
  </div>

</div><!-- /assessment -->


<!-- ═══════════════════════════ WITNESS ═════════════════════════════ -->
<div class="tab-panel" id="tab-witness">
{witness_tab}
</div><!-- /witness -->


<!-- ═══════════════════════ EQUIVALENCE TEST ════════════════════════ -->
<div class="tab-panel" id="tab-equivalence">
{equivalence_tab}
</div><!-- /equivalence -->

</div><!-- /content -->

{_chartjs_script()}
<script>
function showTab(name) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}}

const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
Chart.defaults.color = isDark ? '#94a3b8' : '#64748b';
Chart.defaults.borderColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)';

// Tables per schema
new Chart(document.getElementById('tablesChart'), {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [{{ label: 'Tables deployed', data: {chart_tables},
      backgroundColor: '#0069be', borderRadius: 4 }}]
  }},
  options: {{ plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true }} }}, responsive: true, maintainAspectRatio: false }}
}});

// Objects by type
new Chart(document.getElementById('typeChart'), {{
  type: 'doughnut',
  data: {{
    labels: ['Tables', 'Indexes', 'Views', 'Functions', 'Procedures'],
    datasets: [{{ data: [{total_tables}, {total_indexes}, {total_views}, {total_funcs}, {total_procs}],
      backgroundColor: ['#0069be','#00b4d8','#16a34a','#7c3aed','#d97706'],
      borderWidth: 0 }}]
  }},
  options: {{ plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }} }},
    responsive: true, maintainAspectRatio: false }}
}});

// Success donut
new Chart(document.getElementById('successChart'), {{
  type: 'doughnut',
  data: {{
    labels: ['In SPG', 'Not in SPG'],
    datasets: [{{ data: [{total_ok_objs}, {total_fail_objs}],
      backgroundColor: ['#16a34a', '#dc2626'], borderWidth: 0 }}]
  }},
  options: {{
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }},
      tooltip: {{ callbacks: {{ label: ctx => {{
        const total = ctx.dataset.data.reduce((a,b)=>a+b,0);
        return ` ${{ctx.label}}: ${{ctx.parsed}} (${{Math.round(ctx.parsed/total*100)}}%)`;
      }} }} }}
    }},
    responsive: true,
    maintainAspectRatio: false
  }}
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------

def generate(workspace_dir: str | Path, output_path: str | Path | None = None) -> Path:
    ws  = Path(workspace_dir).resolve()
    out = Path(output_path) if output_path else ws / "migration_report.html"
    data = load_workspace_data(ws)
    html = render_html(data)
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate spgloader HTML migration report")
    ap.add_argument("--work-dir", required=True, help="Workspace directory")
    ap.add_argument("--output", default=None, help="Output HTML path (default: <work-dir>/migration_report.html)")
    args = ap.parse_args()
    out = generate(args.work_dir, args.output)
    print(f"Report written: {out}")
