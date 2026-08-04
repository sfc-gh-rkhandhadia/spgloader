"""
tests/test_convert.py — Unit tests for convert_objects.py conversion accuracy.

Tests:
  1. EWI-0012 marker appears for unconverted constructs (TABLE vars, cursors, etc.)
  2. Oracle function substitutions (NVL, SYSDATE, NEXTVAL, FROM DUAL, SYS_GUID)
  3. MSSQL-specific conversions (ISNULL, TOP, schema-qualified views)
  4. No SPG-EWI-0012 in clean conversions (false-positive check)

Run with:
    uv run --project /path/to/spgloader pytest tests/test_convert.py -v
"""
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_ROOT / "lib"))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))


@pytest.fixture(scope="module")
def convert():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "convert_objects",
        SKILL_ROOT / "scripts" / "convert_objects.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# SPG-EWI-0012 — unconverted construct markers
# ---------------------------------------------------------------------------

class TestEWI0012Markers:
    """SPG-EWI-0012 appears for constructs the converter cannot handle."""

    def test_table_variable_gets_ewi_0012(self, convert):
        """TABLE variable in MSSQL proc body → SPG-EWI-0012 placeholder."""
        ddl = """
        CREATE PROCEDURE dbo.test_proc AS
        BEGIN
            DECLARE @MyTable TABLE (id INT, name VARCHAR(100));
            SELECT * FROM @MyTable;
        END
        """
        result, codes = convert.convert_mssql_procedure(ddl)
        assert "SPG-EWI-0012" in result, (
            "Expected SPG-EWI-0012 marker for TABLE variable, got:\n" + result[:500]
        )

    def test_clean_proc_no_ewi_0012(self, convert):
        """Simple MSSQL proc with no unsupported constructs → no SPG-EWI-0012."""
        ddl = """
        CREATE PROCEDURE dbo.get_count
            @schema_name NVARCHAR(128)
        AS
        BEGIN
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = @schema_name;
        END
        """
        result, codes = convert.convert_mssql_procedure(ddl)
        assert "SPG-EWI-0012" not in result, (
            "No SPG-EWI-0012 expected for clean proc, but found it:\n"
            + result[:500]
        )

    def test_cursor_loop_gets_ewi_0012(self, convert):
        """CURSOR usage → SPG-EWI-0012 CURSOR placeholder."""
        ddl = """
        CREATE PROCEDURE dbo.proc_with_cursor AS
        BEGIN
            DECLARE cur CURSOR FOR SELECT id FROM orders;
            OPEN cur;
            FETCH NEXT FROM cur INTO @id;
            CLOSE cur;
            DEALLOCATE cur;
        END
        """
        result, codes = convert.convert_mssql_procedure(ddl)
        assert "SPG-EWI-0012" in result, (
            "Expected SPG-EWI-0012 for cursor construct"
        )


# ---------------------------------------------------------------------------
# Oracle function substitutions
# ---------------------------------------------------------------------------

class TestOracleFuncSubs:
    """Oracle-specific SQL functions are replaced with PostgreSQL equivalents."""

    def test_nvl_to_coalesce(self, convert):
        result, _ = convert._apply_oracle_func_subs("SELECT NVL(col, 0) FROM t")
        assert "COALESCE" in result
        assert "NVL" not in result

    def test_sysdate_to_now(self, convert):
        result, _ = convert._apply_oracle_func_subs("WHERE created_at < SYSDATE")
        assert "NOW()" in result
        assert "SYSDATE" not in result

    def test_systimestamp_to_now(self, convert):
        result, _ = convert._apply_oracle_func_subs(
            "INSERT INTO t VALUES (SYSTIMESTAMP)"
        )
        assert "NOW()" in result

    def test_from_dual_removed(self, convert):
        result, _ = convert._apply_oracle_func_subs(
            "SELECT 1+1 FROM DUAL"
        )
        assert "DUAL" not in result
        assert "SELECT 1+1" in result

    def test_sys_dual_removed(self, convert):
        result, _ = convert._apply_oracle_func_subs(
            "SELECT SYSDATE FROM SYS.DUAL"
        )
        assert "DUAL" not in result

    def test_nextval_syntax(self, convert):
        result, _ = convert._apply_oracle_func_subs(
            "INSERT INTO t(id) VALUES (my_seq.NEXTVAL)"
        )
        assert "NEXTVAL('my_seq')" in result
        assert ".NEXTVAL" not in result

    def test_sys_guid_to_gen_random_uuid(self, convert):
        result, _ = convert._apply_oracle_func_subs(
            "SELECT SYS_GUID() FROM DUAL"
        )
        assert "gen_random_uuid()" in result
        assert "SYS_GUID" not in result

    def test_nvl_case_insensitive(self, convert):
        result, _ = convert._apply_oracle_func_subs("SELECT nvl(col, 'default')")
        assert "COALESCE" in result


# ---------------------------------------------------------------------------
# View conversion — MSSQL
# ---------------------------------------------------------------------------

class TestMSSQLViewConversion:
    """MSSQL view DDL converts cleanly to PostgreSQL CREATE VIEW."""

    def test_simple_view_converts(self, convert):
        ddl = """
        CREATE VIEW dbo.v_orders AS
        SELECT o.id, o.amount, c.name
        FROM dbo.orders o
        JOIN dbo.customers c ON o.customer_id = c.id
        """
        result, codes = convert.convert_mssql_view(ddl)
        assert "CREATE OR REPLACE VIEW" in result
        assert "SPG-EWI-0012" not in result

    def test_view_top_clause_handled(self, convert):
        """TOP without ORDER BY in a view should not crash the converter."""
        ddl = """
        CREATE VIEW dbo.v_top_orders AS
        SELECT TOP 100 id, amount FROM dbo.orders ORDER BY amount DESC
        """
        # Should not raise
        result, codes = convert.convert_mssql_view(ddl)
        assert "CREATE OR REPLACE VIEW" in result

    def test_xml_view_gets_ewi(self, convert):
        """Views using FOR XML / xml.nodes() get EWI annotation."""
        ddl = """
        CREATE VIEW HumanResources.vJobCandidate AS
        SELECT jc.JobCandidateID, jc.BusinessEntityID,
               jc.Resume.value('(/n:Resume/n:Name/n:Name.First)[1]', 'nvarchar(30)') AS [Name.First]
        FROM HumanResources.JobCandidate jc
        """
        result, codes = convert.convert_mssql_view(ddl)
        # XQuery views should have an EWI warning (0004 or 0012)
        has_ewi = any(c.startswith("SPG-EWI") or c.startswith("SPG-BLOCK") for c in codes)
        assert has_ewi or "SPG-EWI" in result, (
            "Expected EWI annotation for XML/XQuery view"
        )


# ---------------------------------------------------------------------------
# Oracle view conversion
# ---------------------------------------------------------------------------

class TestOracleViewConversion:
    """Oracle view DDL applies func subs and converts syntax."""

    def test_oracle_view_nvl_converted(self, convert):
        ddl = """
        CREATE OR REPLACE VIEW hr.v_employees AS
        SELECT emp_id, NVL(salary, 0) AS salary, SYSDATE AS snapshot
        FROM hr.employees
        """
        result, codes = convert.convert_oracle_view(ddl)
        assert "COALESCE" in result
        assert "NVL" not in result
        assert "NOW()" in result
        assert "SYSDATE" not in result

    def test_oracle_dual_removed_in_view(self, convert):
        ddl = """
        CREATE OR REPLACE VIEW hr.v_date AS
        SELECT SYSDATE AS today FROM DUAL
        """
        result, codes = convert.convert_oracle_view(ddl)
        assert "DUAL" not in result
