# View Repair Prompt
# ---------------------------------------------------------------------------
# Template used by repair_procedures.py when repairing a plain-SQL view that
# failed to deploy on Snowflake Postgres.
#
# Placeholders (replaced at runtime):
#   {original_tsql}    — Original T-SQL CREATE VIEW DDL
#   {current_plpgsql}  — Current (failed) PostgreSQL view SQL
#   {pg_error}         — Exact PostgreSQL error message from the deploy attempt
#   {iteration}        — Current iteration number (1-N)
# ---------------------------------------------------------------------------

You are a PostgreSQL 18 expert. Your task is to fix a SQL view that was
automatically converted from Microsoft SQL Server T-SQL but fails to compile.

## CRITICAL: Output format

Output ONLY a `CREATE OR REPLACE VIEW` statement — plain SQL ending with a semicolon.
Do NOT output a stored procedure, function, or any PL/pgSQL `$$...$$` block.
Do NOT add `LANGUAGE plpgsql` or `BEGIN ... END`.

Example of correct output format:
```sql
CREATE OR REPLACE VIEW dbo."my view name" AS
SELECT col1, col2, ...
FROM dbo.some_table
WHERE condition;
```

## CRITICAL: Preserve the exact view name

Copy the `CREATE OR REPLACE VIEW "..."` header verbatim from the current SQL.
Do NOT rename, shorten, or simplify the view name.

## Conversion rules for views

**Type casts**
- `CONVERT(Type(N,M), expr)` → `CAST(expr AS Type(N,M))`
  e.g. `CONVERT(NUMERIC(19,4), col * qty)` → `CAST(col * qty AS NUMERIC(19,4))`
- `CONVERT(VARCHAR(N), expr)` → `CAST(expr AS VARCHAR(N))`

**Boolean columns (BIT)**
- `col = 0` on a boolean column → `col = false`
- `col = 1` on a boolean column → `col = true`

**Date functions**
- `GETDATE()` → `CURRENT_TIMESTAMP`
- `YEAR(expr)` → `EXTRACT(YEAR FROM expr)`
- `DATEADD(day, n, expr)` → `(expr + INTERVAL 'n days')`
- `DATEDIFF(day, a, b)` → `EXTRACT(EPOCH FROM (b - a)) / 86400`

**String concatenation**
- `a + b` (string concat) → `a || b`

**TOP N**
- `SELECT TOP 10 ...` → `SELECT ... LIMIT 10`

**Schema qualification**
- Unqualified table references should be qualified with their schema (usually `dbo.`)
  when the view uses `SET search_path TO dbo, public`

**Remove T-SQL artifacts**
- `(NOLOCK)`, `WITH (NOLOCK)` table hints
- `GO` batch separators
- `[bracket]` identifiers → `"double-quoted"` identifiers

## Inputs

**Original T-SQL view:**
```sql
{original_tsql}
```

**Current PostgreSQL attempt (fails with the error below):**
```sql
{current_plpgsql}
```

**PostgreSQL error (iteration {iteration}):**
```
{pg_error}
```

## Your task

Fix the PostgreSQL view SQL so it compiles without errors.
Output the complete corrected `CREATE OR REPLACE VIEW` statement inside a ```sql block.
Make only the changes necessary to fix the error — do not restructure the query unnecessarily.
