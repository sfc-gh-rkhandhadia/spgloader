"""
rules.py — Centralised YAML rule-file loader for spgloader scripts.

All conversion patterns (type mappings, function substitutions, DDL cleanup,
EWI codes, etc.) live in YAML rule files under:
  references/rules/mssql-to-pg/

Scripts call RuleLoader.get(name) to retrieve a parsed rule document.
Loaded documents are cached for the lifetime of the process.

Usage example:
    from spgloader.rules import RuleLoader
    from pathlib import Path

    loader = RuleLoader(Path(__file__).parent.parent.parent)  # skill root
    type_rules = loader.type_mappings()      # list of dicts
    func_rules  = loader.function_substitutions()
    cleanup     = loader.ddl_cleanup()
    date_units  = loader.date_units()
    keywords    = loader.pg_reserved_keywords()
    ewi         = loader.ewi_codes()
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise ImportError("pyyaml is required: pip install pyyaml") from exc


class RuleLoaderError(Exception):
    """Raised when a rule file is missing or malformed."""


class RuleLoader:
    """Load and cache YAML rule files from references/rules/mssql-to-pg/."""

    def __init__(self, skill_root: Path):
        self._rules_dir = skill_root / "references" / "rules" / "mssql-to-pg"
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Core loader
    # ------------------------------------------------------------------

    def load(self, name: str) -> Any:
        """Load {name}.yaml from the rules directory. Results are cached.

        Args:
            name: filename without extension (e.g. 'type-mappings').

        Returns:
            Parsed YAML document (usually a dict).
        """
        if name in self._cache:
            return self._cache[name]

        path = self._rules_dir / f"{name}.yaml"
        if not path.exists():
            raise RuleLoaderError(
                f"Rule file not found: {path}\n"
                f"Expected at: {self._rules_dir / name}.yaml"
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
        rules = self.load("function-substitutions").get("substitutions", [])
        if context is None:
            return rules
        return [r for r in rules if r.get("context", "both") in (context, "both")]

    def ddl_cleanup(self, phase: str | None = None) -> list[dict]:
        """Return SSMS DDL artifact cleanup patterns.

        Args:
            phase: 'pre_bracket', 'post_bracket', or None (all).
        """
        patterns = self.load("ddl-cleanup").get("patterns", [])
        if phase is None:
            return patterns
        return [p for p in patterns if p.get("phase", "pre_bracket") == phase]

    def date_units(self) -> dict:
        """Return the full date-units document (all three maps)."""
        return self.load("date-units")

    def datepart_units(self) -> dict[str, str]:
        """T-SQL DATEPART unit abbreviation → PG EXTRACT keyword."""
        return self.load("date-units").get("datepart_units", {})

    def dateadd_units(self) -> dict[str, str]:
        """T-SQL DATEADD unit abbreviation → PG INTERVAL unit word."""
        return self.load("date-units").get("dateadd_units", {})

    def datediff_divisors(self) -> dict[str, str]:
        """T-SQL DATEDIFF unit → epoch-seconds divisor expression."""
        return self.load("date-units").get("datediff_divisors", {})

    def pg_reserved_keywords(self) -> list[str]:
        """PostgreSQL reserved keywords that need double-quoting."""
        return self.load("pg-reserved-keywords").get("reserved_keywords", [])

    def pg_type_names(self) -> list[str]:
        """PG built-in type names that conflict with column names."""
        return self.load("pg-reserved-keywords").get("type_names", [])

    def ewi_codes(self) -> dict[str, dict]:
        """Full EWI code catalog as {code: {severity, title, description, ...}}."""
        return self.load("ewi-codes").get("codes", {})

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

_loaders: dict[Path, RuleLoader] = {}


def get_loader(skill_root: Path | None = None) -> RuleLoader:
    """Get (or create) a cached RuleLoader for the given skill root.

    If skill_root is None, auto-detects from this file's location
    (assumes: lib/spgloader/rules.py → skill_root = lib/../..)
    """
    if skill_root is None:
        skill_root = Path(__file__).parent.parent.parent

    skill_root = skill_root.resolve()
    if skill_root not in _loaders:
        _loaders[skill_root] = RuleLoader(skill_root)
    return _loaders[skill_root]
