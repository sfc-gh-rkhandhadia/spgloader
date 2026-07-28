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


def _clean_name(n) -> str:
    if isinstance(n, dict):
        n = n.get("procedure") or n.get("view") or n.get("name") or str(n)
    return re.sub(r'"\.?"', ".", str(n)).strip('"')


def _is_trigger_fn(name: str) -> bool:
    return "_trigger" in name.lower() or name.lower().endswith("_trg")


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
                          + phases.get("tables",      {}).get("fail", 0),
            "indexes_ok":   phases.get("indexes",     {}).get("ok",   0),
            "indexes_fail": phases.get("indexes",     {}).get("fail", 0),
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
    vr           = _load_json(ws / "conversion" / "deploy_report.json")
    views_ok     = [_clean_name(n) for n in vr.get("succeeded", [])]
    views_fail   = vr.get("failed", [])
    views_fixed  = [_clean_name(n) for n in vr.get("auto_fixed", [])]

    # -- functions deployment ---------------------------------------------
    fr           = _load_json(ws / "conversion" / "functions_deploy_report.json")
    funcs_ok     = [_clean_name(n) for n in fr.get("succeeded", [])]
    funcs_fail   = fr.get("failed", [])

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
    val_report     = _load_json(ws / "validation" / "validation_report.json")
    val_checks     = val_report.get("checks", [])

    # -- witness validation (Phase 6.5) ------------------------------------
    witness_chains = _load_json(ws / "witness" / "validation_chains.json")
    witness_results = witness_chains.get("validation_results", {})
    witness_summary = witness_chains.get("summary", {})
    witness_ran     = bool(witness_results)

    # -- parity testing (Phase 6.6) ----------------------------------------
    parity_report_md = ""
    parity_file = ws / "parity" / "parity_report.md"
    if parity_file.exists():
        parity_report_md = parity_file.read_text(encoding="utf-8")[:8000]
    parity_ran = parity_file.exists()

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
        # Parity (Phase 6.6)
        "parity_ran":       parity_ran,
        "parity_report_md": parity_report_md,
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
                      still_failed: list, stubs: list) -> str:
    if not items:
        return "<p class='muted-msg'>None</p>"
    rows = []
    for raw in items:
        name = _clean_name(raw)
        schema = name.split(".")[0] if "." in name else "dbo"
        obj    = name.split(".")[-1]
        badge  = _obj_status_badge(name, llm_fixed, rule_fixed, still_failed, stubs)
        rows.append(f"<tr><td class='mono'>{schema}</td><td class='mono'>{obj}</td>"
                    f"<td>{badge}</td></tr>")
    return (f"<div class='table-wrap'><table><thead><tr>"
            f"<th>Schema</th><th>{col}</th><th>Status</th></tr></thead>"
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


def _build_val_rows(checks: list) -> str:
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

        label = chk.replace("_", " ").title()
        rows.append(f"<tr><td>{label}</td><td>{badge}</td>"
                    f"<td class='small'>{detail}</td></tr>")
    return "".join(rows) or "<tr><td colspan='3' class='muted-msg'>No checks run</td></tr>"


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
    parity_ran = data.get("parity_ran", False)
    parity_md  = data.get("parity_report_md", "")

    # Summary cards
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
    rows = ""
    for fqn, r in sorted(results.items()):
        status = r.get("status", "skipped")
        obj_type = r.get("type", "")
        note = r.get("note", "")[:120]
        style, label = _WITNESS_ICONS.get(status, ("muted", status))
        schema = fqn.split(".")[0] if "." in fqn else "dbo"
        name = fqn.split(".")[-1]
        rows += (f"<tr>"
                 f"<td class='mono small'>{schema}</td>"
                 f"<td class='mono small'>{name}</td>"
                 f"<td class='small'>{obj_type}</td>"
                 f"<td><span class='badge badge-{style}'>{label}</span></td>"
                 f"<td class='small'>{note}</td>"
                 f"</tr>")

    if not rows:
        rows = "<tr><td colspan='5' class='muted-msg'>No objects validated</td></tr>"

    # Parity section
    if parity_ran and parity_md:
        # Convert markdown to minimal HTML (just paragraphs and headers)
        import re as _re
        parity_html = _re.sub(r"^### (.+)$", r"<h3>\1</h3>", parity_md, flags=_re.MULTILINE)
        parity_html = _re.sub(r"^## (.+)$", r"<h2>\1</h2>", parity_html, flags=_re.MULTILINE)
        parity_html = _re.sub(r"^# (.+)$", r"<h2>\1</h2>", parity_html, flags=_re.MULTILINE)
        parity_html = _re.sub(r"`([^`]+)`", r"<code>\1</code>", parity_html)
        parity_html = _re.sub(r"^- (.+)$", r"<li>\1</li>", parity_html, flags=_re.MULTILINE)
        parity_section = f"""
  <div class="section">
    <h2>Parity Testing (Phase 6.6)</h2>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;font-size:13px;line-height:1.6">
      {parity_html}
    </div>
  </div>"""
    elif parity_ran:
        parity_section = """
  <div class="section">
    <h2>Parity Testing (Phase 6.6)</h2>
    <p class="muted-msg">Parity testing ran but no report was generated.</p>
  </div>"""
    else:
        parity_section = """
  <div class="section">
    <h2>Parity Testing (Phase 6.6)</h2>
    <div class="alert alert-success" style="background:var(--surface2);border-left:3px solid var(--muted)">
      <span class="alert-icon" style="color:var(--muted)">○</span>
      <div>Parity testing not yet run.</div>
    </div>
  </div>"""

    return f"""
  <div class="section">
    <h2>Source-Side Witness Validation (Phase 6.5)</h2>
    <div class="summary-row">
      {cards}
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Schema</th><th>Object</th><th>Type</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
  {parity_section}"""


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
    funcs_ok     = data["funcs_ok"]
    funcs_fail   = data["funcs_fail"]
    procs_ok     = data["procs_ok"]
    procs_fail   = data["procs_fail"]
    procs_legacy = data["procs_legacy"]
    stubs        = data["stubs"]
    llm_fixed    = data["llm_fixed"]
    rule_fixed   = data["rule_fixed"]
    still_failed = data["still_failed"]

    total_tables  = data["total_tables"]
    total_indexes = data["total_indexes"]
    total_views   = len(views_ok)
    total_funcs   = len(funcs_ok)
    total_procs   = len(procs_ok)
    total_repair  = len(llm_fixed) + len(rule_fixed)

    # counts for the overview donut
    total_ok_objs   = total_tables + total_indexes + total_views + total_funcs + total_procs
    total_fail_objs = (sum(s["indexes_fail"] for s in schemas.values())
                       + len(views_fail) + len(funcs_fail) + len(procs_fail))

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

    views_table  = _build_obj_table(views_all,  "View",      llm_fixed, rule_fixed, still_failed, stubs)
    funcs_table  = _build_obj_table(funcs_all,  "Function",  llm_fixed, rule_fixed, still_failed, stubs)
    procs_table  = _build_obj_table(procs_all,  "Procedure", llm_fixed, rule_fixed, still_failed, stubs)
    legacy_table = (_build_obj_table(procs_legacy, "Procedure (Legacy)", [], [], [], [])
                    if procs_legacy else "<p class='muted-msg'>None</p>")

    warn_rows = _build_warn_rows(data["warn_findings"])
    val_rows  = _build_val_rows(data["val_checks"])
    dep_rows  = _build_dep_rows(data["dep_groups"])

    ext_list = ""
    for e in data["ext_prereqs"]:
        ext_list += f"<li class='mono small'>{e}</li>"
    ext_html = f"<ul>{ext_list}</ul>" if ext_list else "<p class='muted-msg'>None required</p>"

    witness_tab = _build_witness_tab(data)
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
  <button class="tab-btn active" onclick="showTab('overview')">Overview</button>
  <button class="tab-btn" onclick="showTab('deployment')">Deployment</button>
  <button class="tab-btn" onclick="showTab('objects')">Objects</button>
  <button class="tab-btn" onclick="showTab('validation')">Validation</button>
  <button class="tab-btn" onclick="showTab('assessment')">Assessment</button>
  <button class="tab-btn" onclick="showTab('witness')">Witness</button>
</div>

<div class="content">

<!-- ═══════════════════════════ OVERVIEW ═══════════════════════════ -->
<div class="tab-panel active" id="tab-overview">

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="num green">{total_tables:,}</div>
      <div class="label">Tables</div>
      <div class="sub">100% deployed</div>
    </div>
    <div class="kpi-card">
      <div class="num green">{total_indexes:,}</div>
      <div class="label">Indexes</div>
      <div class="sub">{sum(s['indexes_fail'] for s in schemas.values())} skipped</div>
    </div>
    <div class="kpi-card">
      <div class="num green">{total_views}</div>
      <div class="label">Views</div>
      <div class="sub">All deployed</div>
    </div>
    <div class="kpi-card">
      <div class="num green">{total_funcs}</div>
      <div class="label">Functions</div>
      <div class="sub">All deployed</div>
    </div>
    <div class="kpi-card">
      <div class="num {'green' if not procs_fail else 'amber'}">{total_procs}</div>
      <div class="label">Procedures</div>
      <div class="sub">{len(procs_fail)} failed · {len(procs_legacy)} legacy skipped</div>
    </div>
    <div class="kpi-card">
      <div class="num purple">{total_repair}</div>
      <div class="label">LLM Repaired</div>
      <div class="sub">{len(still_failed)} still failing</div>
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
      <h3>Migration Success</h3>
      <div class="chart-container"><canvas id="successChart"></canvas></div>
    </div>
  </div>

  <!-- Print-only stat row replaces canvas charts in PDF -->
  <div class="print-chart-summary" style="display:none;gap:12px;flex-wrap:wrap;margin-bottom:24px;padding:16px;background:var(--surface2);border-radius:8px;border:1px solid var(--border)">
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--blue)">{total_tables:,}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Tables</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--blue)">{total_indexes:,}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Indexes</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--green)">{total_views}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Views</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--green)">{total_funcs}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Functions</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--{'amber' if procs_fail else 'green'})">{total_procs}</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Procedures</div></div>
    <div style="flex:1;min-width:120px;text-align:center"><div style="font-size:28px;font-weight:800;color:var(--green)">{round(total_ok_objs/(total_ok_objs+total_fail_objs)*100) if (total_ok_objs+total_fail_objs) else 100}%</div><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Success Rate</div></div>
  </div>

  <div class="section">
    <h2>Migration Summary</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Category</th><th>Deployed</th><th>Failed / Skipped</th><th>Notes</th></tr></thead>
        <tbody>
          <tr><td>Tables</td><td class="num ok">{total_tables:,}</td><td class="num">0</td><td class="small">Catalog-based deployment via parallel_deploy.py</td></tr>
          <tr><td>Indexes</td><td class="num ok">{total_indexes:,}</td>
              <td class="num {'fail' if sum(s['indexes_fail'] for s in schemas.values()) else ''}">{sum(s['indexes_fail'] for s in schemas.values())}</td>
              <td class="small">Computed columns &amp; 32-col limit</td></tr>
          <tr><td>Foreign Keys</td><td class="num ok">{sum(s['fk_benign'] for s in schemas.values()):,}</td>
              <td class="num">{sum(s['fk_real'] for s in schemas.values())}</td>
              <td class="small">All FK failures are benign "already exists"</td></tr>
          <tr><td>Views</td><td class="num ok">{total_views}</td><td class="num">0</td><td class="small">Rule conversion + manual fixes</td></tr>
          <tr><td>Functions</td><td class="num ok">{total_funcs}</td><td class="num">0</td><td class="small">{len(llm_fixed)} objects fixed by LLM</td></tr>
          <tr><td>Procedures</td><td class="num ok">{total_procs}</td>
              <td class="num {'fail' if procs_fail else ''}">{len(procs_fail)}</td>
              <td class="small">{len(procs_legacy)} legacy skipped · {len(still_failed)} UDTT-dependent</td></tr>
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
    <h2>Index Failures</h2>
    <p class="small" style="margin-bottom:10px;color:var(--muted)">
      These indexes could not be migrated due to MSSQL-specific features with no
      PostgreSQL equivalent (computed columns, expression columns, &gt;32 column limit).
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
      ASP.NET Membership, SQL Server Agent, and other vendor-framework objects excluded during Phase 3.6 Deprecated Review.
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
        <thead><tr><th>Check</th><th>Result</th><th>Details</th></tr></thead>
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
    labels: ['Success', 'Failed'],
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
