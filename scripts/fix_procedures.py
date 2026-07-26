#!/usr/bin/env python3
"""
fix_procedures.py — Apply rule-based PL/pgSQL fixes to converted procedure files.

Two-pass approach:
  Pass 1 — Pattern substitutions from plpgsql-fixes.yaml (procedure_only rules
            are included when --procedure mode is active, which is the default)
  Pass 2 — Multi-variable SELECT INTO structural rewrite
  Pass 3 — Add missing semicolons before END IF / END LOOP / RETURN
  Pass 4 — Remove duplicate semicolons

Input/Output: wave_4_procedures_triggers/*.sql  (overwritten in place)

Usage:
  python fix_procedures.py --work-dir ~/.spgloader/20260101_120000
  python fix_procedures.py --work-dir ... --only-failed  # only fix those in
                             deploy_report.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_DIR / "lib"))


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def _load_plpgsql_rules(skill_dir: Path) -> list[dict]:
    """Load body_transform rules from plpgsql-fixes.yaml."""
    yaml_path = (skill_dir / "references" / "rules" / "mssql-to-pg"
                 / "plpgsql-fixes.yaml")
    if not yaml_path.exists():
        print(f"  WARN: {yaml_path} not found — skipping rule-based fixes",
              file=sys.stderr)
        return []
    try:
        import yaml
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        return data.get("body_transforms", [])
    except Exception as e:
        print(f"  WARN: could not load plpgsql-fixes.yaml: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Pass 1 — Pattern substitution rules
# ---------------------------------------------------------------------------

def _build_flags(flag_names: list[str]) -> int:
    result = 0
    for name in flag_names:
        flag = getattr(re, name.upper(), None)
        if flag is not None:
            result |= flag
    return result


def apply_rules(sql: str, rules: list[dict], procedure_mode: bool = True
                ) -> tuple[str, list[str]]:
    """Apply plpgsql-fixes.yaml rules.  procedure_only rules are included when
    procedure_mode is True."""
    applied = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if rule.get("procedure_only", False) and not procedure_mode:
            continue
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        flags = _build_flags(rule.get("flags", ["IGNORECASE"]))
        new_sql, n = re.subn(pattern, replacement, sql, flags=flags)
        if n:
            sql = new_sql
            applied.append(f"{rule['name']}: {n}")
    return sql, applied


# ---------------------------------------------------------------------------
# Pass 2 — Multi-variable SELECT INTO
# ---------------------------------------------------------------------------

def fix_multi_select_into(sql: str) -> tuple[str, list[str]]:
    """SELECT a=c1, b=c2 FROM t → SELECT c1, c2 INTO a, b FROM t."""
    applied = []

    def _rewrite(m: re.Match) -> str:
        assigns_str = m.group(1)
        rest = m.group(2)
        pairs = [a.strip() for a in assigns_str.split(",") if a.strip()]
        vars_list, cols_list = [], []
        for pair in pairs:
            pm = re.match(r'(\w+)\s*=\s*(.+)', pair.strip(), re.IGNORECASE)
            if pm:
                vars_list.append(pm.group(1))
                cols_list.append(pm.group(2).strip())
            else:
                return m.group(0)
        return f"SELECT {', '.join(cols_list)} INTO {', '.join(vars_list)} {rest}"

    pattern = (
        r'\bSELECT\s+'
        r'((?:\w+\s*=\s*[^,\n]+(?:,\s*\w+\s*=\s*[^,\n]+)*?))'
        r'\s+(FROM\b[^\n]*)'
    )
    new_sql, n = re.subn(pattern, _rewrite, sql, flags=re.IGNORECASE)
    if n:
        applied.append(f"multi_select_into: {n}")
    return new_sql, applied


# ---------------------------------------------------------------------------
# Pass 3 — Add missing semicolons
# ---------------------------------------------------------------------------

def fix_missing_semicolons(sql: str) -> tuple[str, list[str]]:
    """Add missing ';' before structural keywords where PL/pgSQL requires them."""
    applied = []

    # Before END IF; / END LOOP; / RETURN / ELSIF / ELSE
    keywords = ['END\\s+IF', 'END\\s+LOOP', 'RETURN\\b', 'ELSIF\\b', 'ELSE\\b']
    for kw in keywords:
        # If the previous non-whitespace char is not ; or ( add a ;
        pattern = rf'([^;(\n])([ \t]*\n[ \t]*)({kw})'
        new_sql, n = re.subn(pattern, r'\1;\2\3', sql, flags=re.IGNORECASE)
        if n:
            sql = new_sql
            applied.append(f"semicolon_before_{kw.split(chr(92))[0].lower()}: {n}")

    return sql, applied


# ---------------------------------------------------------------------------
# Pass 4 — Remove double semicolons
# ---------------------------------------------------------------------------

def fix_double_semicolons(sql: str) -> tuple[str, list[str]]:
    new_sql, n = re.subn(r';(\s*);', r';\1', sql)
    return new_sql, ([f"double_semicolon: {n}"] if n else [])


# ---------------------------------------------------------------------------
# Per-file fixer
# ---------------------------------------------------------------------------

def fix_procedure_file(path: Path, rules: list[dict]
                        ) -> tuple[bool, list[str]]:
    """Apply all passes to one procedure file.  Overwrites in place.
    Returns (changed, [fix_descriptions])."""
    sql = path.read_text(encoding="utf-8", errors="replace")
    original = sql
    all_fixes: list[str] = []

    # Pass 1: rule substitutions — clean double semicolons after each pass
    for _ in range(3):
        new_sql, fixes = apply_rules(sql, rules, procedure_mode=True)
        all_fixes.extend(fixes)
        # Clean double semicolons immediately so rules don't cascade them
        new_sql, ds_fixes = fix_double_semicolons(new_sql)
        all_fixes.extend(ds_fixes)
        if new_sql == sql:
            break
        sql = new_sql

    # Pass 2: multi-variable SELECT INTO (up to 5 passes)
    for _ in range(5):
        new_sql, fixes = fix_multi_select_into(sql)
        all_fixes.extend(fixes)
        if new_sql == sql:
            break
        sql = new_sql

    # Pass 3: missing semicolons
    sql, fixes = fix_missing_semicolons(sql)
    all_fixes.extend(fixes)

    # Pass 4: double semicolons
    sql, fixes = fix_double_semicolons(sql)
    all_fixes.extend(fixes)

    changed = sql != original
    if changed:
        path.write_text(sql, encoding="utf-8")
    return changed, all_fixes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply rule-based PL/pgSQL fixes to converted procedure files"
    )
    parser.add_argument("--work-dir", required=True,
                        help="spgloader workspace directory")
    parser.add_argument("--only-failed", action="store_true",
                        help="Only process procedures listed in procedures_deploy_report.json")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).expanduser()
    skill_dir = Path(__file__).parent.parent
    rules = _load_plpgsql_rules(skill_dir)
    print(f"Rules loaded    : {len(rules)} (including procedure_only rules)")

    proc_dir = work_dir / "conversion" / "postgres" / "wave_4_procedures_triggers"
    if not proc_dir.exists():
        print(f"ERROR: {proc_dir} not found", file=sys.stderr)
        sys.exit(1)

    # Optionally restrict to only failed procedures
    target_files: set[str] | None = None
    if args.only_failed:
        report_path = work_dir / "conversion" / "procedures_deploy_report.json"
        if report_path.exists():
            data = json.loads(report_path.read_text())
            target_files = {item["file"] for item in data.get("failed", [])}
            print(f"Only-failed mode: {len(target_files)} procedures to fix")
        else:
            print("WARN: no procedures_deploy_report.json found — processing all",
                  file=sys.stderr)

    files = sorted(proc_dir.glob("*.sql"))
    if target_files is not None:
        files = [f for f in files if f.name in target_files]

    changed_count = 0
    report: dict[str, list[str]] = {}

    for f in files:
        changed, fixes = fix_procedure_file(f, rules)
        if changed:
            changed_count += 1
            report[f.name] = fixes
            print(f"  FIXED  {f.stem}: {', '.join(fixes[:3])}"
                  f"{'...' if len(fixes) > 3 else ''}")

    # Write fix report
    fix_report_path = work_dir / "conversion" / "procedures_fix_report.json"
    fix_report_path.write_text(json.dumps(report, indent=2))

    print(f"\n{'='*60}")
    print(f"Files processed : {len(files)}")
    print(f"Files changed   : {changed_count}")
    print(f"Fix report      : {fix_report_path}")


if __name__ == "__main__":
    main()
