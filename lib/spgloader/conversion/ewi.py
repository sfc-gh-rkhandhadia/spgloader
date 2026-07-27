"""
SPG EWI (Error, Warning, Issue) code system for Snowflake Postgres compatibility.

EWI codes are defined in references/rules/mssql-to-pg/ewi-codes.yaml.
This module loads that catalog at import time and exposes the same API as before.

Three assessment tiers:
  BLOCK   — hard stop; migration cannot proceed until resolved
  WARN    — proceed with user confirmation; risk acknowledged
  RESOLVE — advisory; automatic resolution available (extension prereq, etc.)

One annotation tier:
  INFO    — informational annotation on converted SQL files
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise ImportError("pyyaml is required: pip install pyyaml") from exc


class EWISeverity(Enum):
    BLOCK   = "BLOCK"
    WARN    = "WARN"
    RESOLVE = "RESOLVE"
    INFO    = "INFO"


@dataclass
class EWICode:
    code: str
    severity: EWISeverity
    title: str
    description: str
    spg_rule: str = ""
    auto_resolution: str | None = None
    extension_prereq: str | None = None


# ---------------------------------------------------------------------------
# Load catalog from YAML
# ---------------------------------------------------------------------------

def _load_catalog() -> dict[str, EWICode]:
    yaml_path = Path(__file__).parent.parent.parent.parent / "references" / "rules" / "mssql-to-pg" / "ewi-codes.yaml"
    if not yaml_path.exists():
        # Graceful fallback: return empty catalog (scripts still work, just no EWI metadata)
        return {}

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    catalog: dict[str, EWICode] = {}
    for code_str, entry in (data.get("codes") or {}).items():
        severity_str = entry.get("severity", "INFO").upper()
        try:
            severity = EWISeverity[severity_str]
        except KeyError:
            severity = EWISeverity.INFO

        catalog[code_str] = EWICode(
            code=code_str,
            severity=severity,
            title=entry.get("title", ""),
            description=entry.get("description", ""),
            spg_rule=entry.get("spg_rule", ""),
            auto_resolution=entry.get("auto_resolution"),
            extension_prereq=entry.get("extension_prereq"),
        )
    return catalog


SPG_EWI_CODES: dict[str, EWICode] = _load_catalog()


# ---------------------------------------------------------------------------
# Annotation helper
# ---------------------------------------------------------------------------

def annotate_sql(sql: str, codes: list[str]) -> str:
    """Prepend SPG EWI comment block to converted SQL."""
    if not codes:
        return sql
    lines = []
    for code in codes:
        ewi = SPG_EWI_CODES.get(code)
        if ewi:
            lines.append(f"-- ** {ewi.code} **")
        else:
            lines.append(f"-- ** {code} **")
    return "\n".join(lines) + "\n" + sql


def get_codes_by_severity(severity: EWISeverity) -> list[EWICode]:
    return [c for c in SPG_EWI_CODES.values() if c.severity == severity]
