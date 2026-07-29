"""
rules.py — Centralised YAML rule-file loader for spgloader scripts.

Rule files live under per-source-type directories:
  references/rules/mssql-to-pg/    ← T-SQL → PostgreSQL rules
  references/rules/mysql-to-pg/    ← MySQL/MariaDB → PostgreSQL rules
  references/rules/oracle-to-pg/   ← PL/SQL → PostgreSQL rules
  references/rules/shared/         ← dialect-agnostic rules (ewi-codes, pg-keywords)

RuleLoader.load(name) looks in the source-type directory first, then falls back
to shared/ so shared rules are accessible from any source type without duplication.

Loaded documents are cached for the lifetime of the process.

Usage:
    from spgloader.rules import get_loader
    from pathlib import Path

    loader = get_loader(skill_root, source_type="mysql")
    type_rules = loader.type_mappings()
    func_rules  = loader.function_substitutions()
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise ImportError("pyyaml is required: pip install pyyaml") from exc


# Map source_type strings → rule sub-directory names
_SOURCE_DIRS: dict[str, str] = {
    "mssql":    "mssql-to-pg",
    "mysql":    "mysql-to-pg",
    "mariadb":  "mysql-to-pg",
    "oracle":   "oracle-to-pg",
}
_SHARED_DIR = "shared"


class RuleLoaderError(Exception):
    """Raised when a rule file is missing or malformed."""


class RuleLoader:
    """Load and cache YAML rule files from the per-source-type rules directory.

    Resolution order for load(name):
      1. references/rules/<source_type_dir>/<name>.yaml
      2. references/rules/shared/<name>.yaml
      3. RuleLoaderError if neither exists
    """

    def __init__(self, skill_root: Path, source_type: str = "mssql"):
        src_dir = _SOURCE_DIRS.get(source_type.lower(), "mssql-to-pg")
        rules_base = skill_root / "references" / "rules"
        self._rules_dir = rules_base / src_dir
        self._shared_dir = rules_base / _SHARED_DIR
        self._source_type = source_type.lower()
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Core loader
    # ------------------------------------------------------------------

    def load(self, name: str) -> Any:
        """Load {name}.yaml — source-type dir first, shared/ fallback.

        Args:
            name: filename without extension (e.g. 'type-mappings').

        Returns:
            Parsed YAML document (usually a dict).
        """
        if name in self._cache:
            return self._cache[name]

        # Search order: source-type dir → shared/
        candidates = [
            self._rules_dir / f"{name}.yaml",
            self._shared_dir / f"{name}.yaml",
        ]
        path: Path | None = next((p for p in candidates if p.exists()), None)
        if path is None:
            searched = "\n  ".join(str(p) for p in candidates)
            raise RuleLoaderError(
                f"Rule file not found: {name}.yaml\n"
                f"  Searched:\n  {searched}"
            )

        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RuleLoaderError(f"YAML parse error in {path}: {exc}") from exc

        if doc is None:
            raise RuleLoaderError(f"Rule file is empty: {path}")

        self._cache[name] = doc
        return doc

    # ------------------------------------------------------------------
    # Typed accessors
    # ------------------------------------------------------------------

    def type_mappings(self, context: str | None = None) -> list[dict]:
        """Return type-mapping substitution rules.

        Args:
            context: 'pre_downcase', 'post_downcase', 'declare', or None (all).
        """
        rules = self.load("type-mappings").get("substitutions", [])
        if context is None:
            return rules
        return [r for r in rules if r.get("context", "pre_downcase") == context]

    def function_substitutions(self, context: str | None = None) -> list[dict]:
        """Return function/syntax substitution rules."""
        try:
            rules = self.load("function-substitutions").get("substitutions", [])
        except RuleLoaderError:
            return []  # not all source types define function substitutions
        if context is None:
            return rules
        return [r for r in rules if r.get("context", "both") in (context, "both")]

    def ddl_cleanup(self, phase: str | None = None) -> list[dict]:
        """Return SSMS DDL artifact cleanup patterns (MSSQL only)."""
        try:
            patterns = self.load("ddl-cleanup").get("patterns", [])
        except RuleLoaderError:
            return []
        if phase is None:
            return patterns
        return [p for p in patterns if p.get("phase", "pre_bracket") == phase]

    def date_units(self) -> dict:
        """Return the full date-units document (MSSQL only)."""
        try:
            return self.load("date-units")
        except RuleLoaderError:
            return {}

    def datepart_units(self) -> dict[str, str]:
        """T-SQL DATEPART unit abbreviation → PG EXTRACT keyword."""
        return self.date_units().get("datepart_units", {})

    def dateadd_units(self) -> dict[str, str]:
        """T-SQL DATEADD unit abbreviation → PG INTERVAL unit word."""
        return self.date_units().get("dateadd_units", {})

    def datediff_divisors(self) -> dict[str, str]:
        """T-SQL DATEDIFF unit → epoch-seconds divisor expression."""
        return self.date_units().get("datediff_divisors", {})

    def pg_reserved_keywords(self) -> list[str]:
        """PostgreSQL reserved keywords that need double-quoting."""
        return self.load("pg-reserved-keywords").get("reserved_keywords", [])

    def pg_type_names(self) -> list[str]:
        """PG built-in type names that conflict with column names."""
        return self.load("pg-reserved-keywords").get("type_names", [])

    def ewi_codes(self) -> dict[str, dict]:
        """Full EWI code catalog as {code: {severity, title, description, ...}}."""
        return self.load("ewi-codes").get("codes", {})

    def plpgsql_fixes(self) -> list[dict]:
        """Source-type-specific PL/pgSQL body transformation rules."""
        try:
            return self.load("plpgsql-fixes").get("body_transforms", [])
        except RuleLoaderError:
            return []

    # ------------------------------------------------------------------
    # Regex application helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_flags(flag_list: list[str]) -> int:
        """Convert a list of flag name strings to a combined re.* integer."""
        flag_map = {
            "IGNORECASE": re.IGNORECASE,
            "DOTALL": re.DOTALL,
            "MULTILINE": re.MULTILINE,
            "VERBOSE": re.VERBOSE,
        }
        result = 0
        for name in flag_list:
            result |= flag_map.get(name.upper(), 0)
        return result

    def apply_substitutions(
        self,
        text: str,
        rules: list[dict],
        default_flags: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Apply a list of substitution rules to text.

        Each rule dict must have 'pattern' and 'replacement'. Optional fields:
        'flags' (list of str), 'ewi_code' (str or null).

        Returns:
            (converted_text, list_of_matched_ewi_codes)  — codes deduplicated.
        """
        if default_flags is None:
            default_flags = ["IGNORECASE"]

        codes: list[str] = []
        for rule in rules:
            pattern = rule["pattern"]
            replacement = rule["replacement"] or ""
            flag_names = rule.get("flags", default_flags)
            flags = self._build_flags(flag_names)

            new_text, n = re.subn(pattern, replacement, text, flags=flags)
            if n > 0:
                text = new_text
                ewi = rule.get("ewi_code")
                if ewi:
                    codes.append(ewi)

        # deduplicate while preserving order
        seen: set[str] = set()
        unique_codes = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                unique_codes.append(c)

        return text, unique_codes


# ------------------------------------------------------------------
# Module-level singleton factory
# ------------------------------------------------------------------

_loaders: dict[tuple, RuleLoader] = {}


def get_loader(skill_root: Path | None = None, source_type: str = "mssql") -> RuleLoader:
    """Get (or create) a cached RuleLoader for the given skill root and source type.

    Args:
        skill_root:   Path to the spgloader skill directory. Auto-detected if None.
        source_type:  'mssql' | 'mysql' | 'mariadb' | 'oracle' (default: 'mssql').
    """
    if skill_root is None:
        skill_root = Path(__file__).parent.parent.parent

    skill_root = skill_root.resolve()
    key = (skill_root, source_type.lower())
    if key not in _loaders:
        _loaders[key] = RuleLoader(skill_root, source_type)
    return _loaders[key]
