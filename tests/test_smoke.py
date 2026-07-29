"""
tests/test_smoke.py — Smoke tests for the spgloader conversion pipeline.

Covers:
  1. RuleLoader — all 7 rule files load without error
  2. Type mapping — spot-check critical type conversions
  3. Function substitutions — ISNULL→COALESCE, GETDATE()→NOW() etc.
  4. deprecated-patterns.yaml — structure valid, all 7 patterns present
  5. analyze_deprecated — scan function handles empty DDL list
  6. ddl conversion — basic table DDL round-trip via RuleLoader

Run with:
    uv run --project /path/to/spgloader pytest tests/test_smoke.py -v
"""

import json
import sys
from pathlib import Path

import pytest

# Allow importing lib/ and scripts/ without install
SKILL_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_ROOT / "lib"))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# RuleLoader smoke tests
# ---------------------------------------------------------------------------

class TestRuleLoader:
    """All 7 rule YAML files load without error (MSSQL default)."""

    @pytest.fixture(autouse=True)
    def loader(self):
        from spgloader.rules import RuleLoader
        self.loader = RuleLoader(SKILL_ROOT)  # default source_type="mssql"

    def test_type_mappings_loads(self):
        rules = self.loader.type_mappings()
        assert isinstance(rules, list)
        assert len(rules) > 0

    def test_function_substitutions_loads(self):
        rules = self.loader.function_substitutions()
        assert isinstance(rules, list)
        assert len(rules) > 0

    def test_ddl_cleanup_loads(self):
        rules = self.loader.ddl_cleanup()
        assert isinstance(rules, list)
        assert len(rules) > 0
        # Spot-check a known cleanup pattern exists
        names = [r.get("name", "") for r in rules]
        assert any("filegroup" in n or "identity" in n for n in names)

    def test_date_units_loads(self):
        rules = self.loader.date_units()
        assert isinstance(rules, dict)
        assert "datepart_units" in rules

    def test_pg_reserved_keywords_loads(self):
        kw = self.loader.pg_reserved_keywords()
        assert isinstance(kw, list)
        assert "user" in kw        # must quote 'user' in PG
        assert "order" in kw

    def test_ewi_codes_loads(self):
        codes = self.loader.ewi_codes()
        assert isinstance(codes, dict)

    def test_plpgsql_fixes_loads(self):
        fixes = self.loader.load("plpgsql-fixes")
        assert isinstance(fixes, (dict, list))


class TestRuleLoaderMultiSource:
    """Per-source-type rule isolation: MySQL rules don't bleed into MSSQL."""

    def test_mysql_type_mappings_load(self):
        from spgloader.rules import get_loader
        mysql = get_loader(SKILL_ROOT, "mysql")
        rules = mysql.type_mappings()
        assert isinstance(rules, list)
        assert len(rules) > 0
        # MySQL-specific types must be present
        patterns = [r["pattern"] for r in rules]
        assert any("LONGTEXT" in p for p in patterns), "MySQL LONGTEXT mapping missing"

    def test_mssql_isolation_no_mysql_types(self):
        from spgloader.rules import get_loader
        mssql = get_loader(SKILL_ROOT, "mssql")
        patterns = [r["pattern"] for r in mssql.type_mappings()]
        assert not any("LONGTEXT" in p for p in patterns), \
            "MySQL LONGTEXT must NOT appear in MSSQL type-mappings"

    def test_shared_ewi_accessible_from_mysql(self):
        from spgloader.rules import get_loader
        mysql = get_loader(SKILL_ROOT, "mysql")
        codes = mysql.ewi_codes()
        assert isinstance(codes, dict)
        assert len(codes) > 0, "EWI codes not accessible from MySQL loader"

    def test_mariadb_maps_to_mysql_rules(self):
        from spgloader.rules import get_loader
        mariadb = get_loader(SKILL_ROOT, "mariadb")
        mysql = get_loader(SKILL_ROOT, "mysql")
        # MariaDB should load same type rules as MySQL
        assert mariadb.type_mappings() == mysql.type_mappings()

    def test_mysql_function_substitutions(self):
        from spgloader.rules import get_loader
        mysql = get_loader(SKILL_ROOT, "mysql")
        rules = mysql.function_substitutions()
        assert isinstance(rules, list)
        assert len(rules) > 0
        patterns = [r["pattern"] for r in rules]
        assert any("IFNULL" in p for p in patterns), "MySQL IFNULL→COALESCE missing"

    def test_oracle_stub_loads(self):
        from spgloader.rules import get_loader
        oracle = get_loader(SKILL_ROOT, "oracle")
        rules = oracle.type_mappings()
        assert isinstance(rules, list)
        assert len(rules) > 0
        patterns = [r["pattern"] for r in rules]
        assert any("NUMBER" in p for p in patterns), "Oracle NUMBER type mapping missing"


# ---------------------------------------------------------------------------
# Type-mapping spot checks
# ---------------------------------------------------------------------------

class TestTypeMappings:
    """Critical type conversions are correct and ordering is respected."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from spgloader.rules import RuleLoader
        self.loader = RuleLoader(SKILL_ROOT)

    def _apply(self, text, context="pre_downcase"):
        rules = self.loader.type_mappings(context)
        result, _ = self.loader.apply_substitutions(text, rules)
        return result

    def test_smalldatetime_not_mapped_to_smalltimestamp(self):
        # Regression: smalldatetime was being matched by datetime pattern first
        result = self._apply("col SMALLDATETIME")
        assert "timestamp" in result.lower()
        assert "smalltimestamp" not in result.lower()

    def test_datetime_mapped(self):
        result = self._apply("col DATETIME")
        assert "timestamp" in result.lower()

    def test_nvarchar_mapped(self):
        result = self._apply("col NVARCHAR(100)")
        assert "varchar" in result.lower() or "text" in result.lower()

    def test_bit_mapped(self):
        result = self._apply("col BIT")
        assert "boolean" in result.lower()

    def test_int_identity_mapped(self):
        # IDENTITY(1,1) is handled by ddl-cleanup (pre_bracket phase), not type-mappings
        rules = self.loader.ddl_cleanup("pre_bracket")
        result, _ = self.loader.apply_substitutions("col INT IDENTITY(1,1)", rules)
        assert "GENERATED ALWAYS AS IDENTITY" in result


# ---------------------------------------------------------------------------
# Function substitution spot checks
# ---------------------------------------------------------------------------

class TestFunctionSubstitutions:
    """Key T-SQL function replacements are applied correctly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from spgloader.rules import RuleLoader
        self.loader = RuleLoader(SKILL_ROOT)

    def _apply(self, text):
        rules = self.loader.function_substitutions()
        result, _ = self.loader.apply_substitutions(text, rules)
        return result

    def test_isnull_to_coalesce(self):
        result = self._apply("SELECT ISNULL(col, 0)")
        assert "COALESCE" in result

    def test_getdate_to_now(self):
        result = self._apply("DEFAULT GETDATE()")
        assert "NOW()" in result or "now()" in result.lower()

    def test_nolock_removed(self):
        result = self._apply("FROM tbl WITH (NOLOCK)")
        assert "NOLOCK" not in result

    def test_len_to_length(self):
        result = self._apply("LEN(col)")
        assert "LENGTH" in result.upper()


# ---------------------------------------------------------------------------
# Deprecated patterns structure
# ---------------------------------------------------------------------------

class TestDeprecatedPatterns:
    """deprecated-patterns.yaml has the expected 7 patterns with required fields."""

    @pytest.fixture(autouse=True)
    def patterns(self):
        import yaml
        path = SKILL_ROOT / "references" / "rules" / "deprecated-patterns.yaml"
        with open(path) as f:
            doc = yaml.safe_load(f)
        self.patterns = doc.get("patterns", doc) if isinstance(doc, dict) else doc

    def test_seven_patterns_exist(self):
        assert len(self.patterns) == 7

    def test_all_have_required_fields(self):
        required = {"id", "name", "source_db", "severity", "detect"}
        for p in self.patterns:
            missing = required - set(p.keys())
            assert not missing, f"Pattern {p.get('id')} missing fields: {missing}"

    def test_known_pattern_ids(self):
        ids = {p["id"] for p in self.patterns}
        expected = {
            "aspnet_membership", "sql_server_agent", "linked_servers",
            "clr_objects", "udtt", "extended_procs", "temporal_tables",
        }
        assert ids == expected


# ---------------------------------------------------------------------------
# analyze_deprecated — unit test for scan_objects with empty input
# ---------------------------------------------------------------------------

class TestAnalyzeDeprecated:
    """analyze_deprecated.scan_objects handles edge cases."""

    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "analyze_deprecated",
            SKILL_ROOT / "scripts" / "analyze_deprecated.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    @pytest.fixture(autouse=True)
    def setup(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "analyze_deprecated",
            SKILL_ROOT / "scripts" / "analyze_deprecated.py",
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        # Load patterns list from YAML for passing to scan_objects
        import yaml
        catalog_path = SKILL_ROOT / "references" / "rules" / "deprecated-patterns.yaml"
        with open(catalog_path) as f:
            doc = yaml.safe_load(f)
        self.patterns = doc.get("patterns", doc) if isinstance(doc, dict) else doc

    def test_empty_object_list_returns_empty(self):
        result = self.mod.scan_objects([], self.patterns)
        assert result == {}

    def test_no_match_on_clean_ddl(self):
        objects = [
            {
                "type": "TABLE",
                "schema": "dbo",
                "name": "orders",
                "ddl": "CREATE TABLE dbo.orders (id INT PRIMARY KEY, amount DECIMAL(10,2))",
            }
        ]
        result = self.mod.scan_objects(objects, self.patterns)
        assert result == {}, f"Unexpected matches: {result}"

    def test_aspnet_membership_detected(self):
        objects = [
            {
                "type": "PROCEDURE",
                "schema": "dbo",
                "name": "aspnet_Membership_GetUserByName",
                "ddl": "CREATE PROCEDURE dbo.aspnet_Membership_GetUserByName AS BEGIN SELECT 1 END",
            }
        ]
        result = self.mod.scan_objects(objects, self.patterns)
        assert "aspnet_membership" in result, f"Expected aspnet_membership match, got: {list(result.keys())}"
