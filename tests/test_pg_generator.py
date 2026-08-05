"""
tests/test_pg_generator.py — Unit tests for pg_generator computed column handling.

Covers the cases that caused 6 Tipalti tables to fail deployment:
  - CONVERT(type, expr, style)  → no PG equivalent → plain nullable column
  - TRY_CONVERT(type, expr)     → no PG equivalent → plain nullable column
  - dbo.UDF(args)               → not yet deployed at CREATE TABLE time → plain nullable column
  - len(expr)                   → safe: converted to length(expr)
  - Simple arithmetic/string ops → safe: GENERATED ALWAYS AS (expr) STORED

Run with:
    uv run --project /path/to/spgloader pytest tests/test_pg_generator.py -v
"""
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_ROOT / "lib"))

from spgloader.conversion.pg_generator import (
    _mssql_expr_to_pg,
    _mssql_expr_is_convertible,
    _gen_column,
    MSSQL_TYPE_MAP,
)


# ---------------------------------------------------------------------------
# _mssql_expr_is_convertible — detection of unconvertible expressions
# ---------------------------------------------------------------------------

class TestComputedExprConvertibility:
    """Expressions that must NOT generate GENERATED ALWAYS AS."""

    # These are the exact expressions that caused the 6 Tipalti table failures.

    def test_try_convert_is_unconvertible(self):
        expr = _mssql_expr_to_pg(
            "(case when len([value])>(23) AND TRY_CONVERT([datetime2](7),[value],(127)) "
            "IS NOT NULL then CONVERT([datetime2](7),[value],(127)) end)"
        )
        assert not _mssql_expr_is_convertible(expr), (
            "TRY_CONVERT has no PG equivalent — must fall back to plain column"
        )

    def test_convert_date_cast_is_unconvertible(self):
        # CONVERT([date], [submittedDateUTC], (0)) → column "date" does not exist
        expr = _mssql_expr_to_pg("(CONVERT([date],[submittedDateUTC],(0)))")
        assert not _mssql_expr_is_convertible(expr), (
            "CONVERT with type name has no PG equivalent"
        )

    def test_udf_call_is_unconvertible(self):
        # UDFs are deployed after tables — cannot reference them at CREATE TABLE time
        expr = _mssql_expr_to_pg("([dbo].[GetAPTransactionType]([amount]))")
        assert not _mssql_expr_is_convertible(expr), (
            "schema-qualified UDF not yet deployed when table is created"
        )

    def test_convert_nvarchar_cast_is_unconvertible(self):
        expr = _mssql_expr_to_pg(
            "(case when [firstName] IS NULL OR [firstName]='' then NULL "
            "else CONVERT([nvarchar](450),left([firstName],(225))) end)"
        )
        assert not _mssql_expr_is_convertible(expr)

    def test_try_cast_is_unconvertible(self):
        expr = _mssql_expr_to_pg("(TRY_CAST([value] AS [datetime2](7)))")
        assert not _mssql_expr_is_convertible(expr)

    def test_mssql_string_concat_plus_is_unconvertible(self):
        # payee.displayName: isnull(x,'')+' ' is MSSQL string concat; PG needs ||
        expr = _mssql_expr_to_pg(
            "(case when ((isnull(ltrim(rtrim([firstName])),'')+' ')+isnull(ltrim(rtrim([lastName])),''))='' "
            "then [idAtPayer] end)"
        )
        assert not _mssql_expr_is_convertible(expr), (
            "MSSQL string concat with + must fall back to plain column (PG needs ||)"
        )


class TestComputedExprConvertibleCases:
    """Expressions that ARE safely convertible to PG GENERATED ALWAYS AS."""

    def test_simple_arithmetic_is_convertible(self):
        expr = _mssql_expr_to_pg("([price] * [qty])")
        assert _mssql_expr_is_convertible(expr)

    def test_subtraction_with_reserved_words_is_convertible(self):
        expr = _mssql_expr_to_pg("([in] - [out])")
        assert _mssql_expr_is_convertible(expr)

    def test_left_function_is_convertible(self):
        # left() is a native PG function
        expr = _mssql_expr_to_pg("(left([lastName],(400)))")
        assert _mssql_expr_is_convertible(expr)

    def test_string_concat_is_unconvertible(self):
        # MSSQL uses + for string concat; PG uses || — this must fall back to plain column
        expr = _mssql_expr_to_pg("([firstName] + ' ' + [lastName])")
        assert not _mssql_expr_is_convertible(expr), (
            "MSSQL string concat with + has no PG equivalent at column definition time"
        )


# ---------------------------------------------------------------------------
# _mssql_expr_to_pg — function substitutions
# ---------------------------------------------------------------------------

class TestComputedFunctionSubstitutions:
    """MSSQL function names are rewritten to PG equivalents where possible."""

    def test_len_becomes_length(self):
        result = _mssql_expr_to_pg("(len([value]) > 23)")
        assert "length(" in result
        assert "len(" not in result

    def test_len_case_insensitive(self):
        result = _mssql_expr_to_pg("(LEN([col]) > 0)")
        assert "length(" in result

    def test_isnull_becomes_coalesce(self):
        result = _mssql_expr_to_pg("(isnull([firstName], ''))")
        assert "coalesce(" in result
        assert "isnull(" not in result

    def test_getdate_becomes_now(self):
        result = _mssql_expr_to_pg("(getdate())")
        assert "now()" in result

    def test_bracket_identifiers_lowercased(self):
        result = _mssql_expr_to_pg("([FirstName] + ' ' + [LastName])")
        assert "firstname" in result
        assert "lastname" in result
        assert "[" not in result

    def test_reserved_word_identifier_quoted(self):
        # 'in' is a PG reserved word → must be double-quoted; 'out' is not
        result = _mssql_expr_to_pg("([in] - [out])")
        assert '"in"' in result
        assert 'out' in result  # 'out' is not reserved, stays unquoted

    def test_outer_parens_stripped(self):
        result = _mssql_expr_to_pg("([price] * [qty])")
        assert not result.startswith("(")


# ---------------------------------------------------------------------------
# _gen_column — end-to-end column DDL generation
# ---------------------------------------------------------------------------

class TestGenColumnComputedFallback:
    """_gen_column falls back to plain nullable column for unconvertible exprs."""

    def _col(self, name, computed_expr, type_name="nvarchar"):
        """Build a minimal column dict as catalog_extract() would produce."""
        return {
            "name": name,
            "type_name": type_name,
            "is_computed": True,
            "computed_expr": computed_expr,
            "is_nullable": True,
            "is_identity": False,
            "default_expr": None,
            "precision": None,
            "scale": None,
            "max_length": -1,
        }

    def test_convert_expr_produces_plain_column(self):
        col = self._col("submitteddateonlydate",
                        "(CONVERT([date],[submittedDateUTC],(0)))",
                        type_name="date")
        ddl = _gen_column(col, MSSQL_TYPE_MAP)
        assert "GENERATED ALWAYS AS" not in ddl
        assert "submitteddateonlydate" in ddl.lower()
        # Original expression preserved as comment
        assert "computed:" in ddl

    def test_udf_expr_produces_plain_column(self):
        col = self._col("transactiontype",
                        "([dbo].[GetAPTransactionType]([amount]))",
                        type_name="smallint")
        ddl = _gen_column(col, MSSQL_TYPE_MAP)
        assert "GENERATED ALWAYS AS" not in ddl
        assert "computed:" in ddl

    def test_try_convert_produces_plain_column(self):
        col = self._col("datevalue",
                        "(case when len([value])>(23) AND TRY_CONVERT([datetime2](7),[value],(127)) "
                        "IS NOT NULL then CONVERT([datetime2](7),[value],(127)) end)",
                        type_name="datetime2")
        ddl = _gen_column(col, MSSQL_TYPE_MAP)
        assert "GENERATED ALWAYS AS" not in ddl

    def test_simple_arithmetic_produces_generated_column(self):
        col = self._col("total", "([price] * [qty])", type_name="numeric")
        ddl = _gen_column(col, MSSQL_TYPE_MAP)
        assert "GENERATED ALWAYS AS" in ddl
        assert "STORED" in ddl
        assert "price * qty" in ddl

    def test_len_expr_produces_generated_column(self):
        """len() expressions ARE convertible (len→length) so they get GENERATED."""
        col = self._col("namelength", "(len([name]))", type_name="int")
        ddl = _gen_column(col, MSSQL_TYPE_MAP)
        assert "GENERATED ALWAYS AS" in ddl
        assert "length(" in ddl
        assert "len(" not in ddl
