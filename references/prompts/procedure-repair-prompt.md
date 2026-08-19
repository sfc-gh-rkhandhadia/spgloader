# Procedure Repair Prompt
# ---------------------------------------------------------------------------
# Template used by repair_procedures.py when calling Snowflake Cortex to fix
# failed PL/pgSQL procedure conversions.
#
# Placeholders (replaced at runtime):
#   {original_tsql}    — Original T-SQL CREATE PROCEDURE DDL
#   {current_plpgsql}  — Current (failed) PL/pgSQL attempt
#   {pg_error}         — Exact PostgreSQL error message from the deploy attempt
#   {iteration}        — Current iteration number (1-N)
# ---------------------------------------------------------------------------

You are a PostgreSQL 18 expert.  Your task is to fix a stored procedure that was
automatically converted from Microsoft SQL Server T-SQL but fails to compile.

## CRITICAL: Preserve the exact procedure name

The `CREATE OR REPLACE PROCEDURE` header must be copied verbatim from `{current_plpgsql}`.
- Do NOT rename, shorten, or simplify the procedure name under any circumstances.
- If the name contains spaces or mixed case, keep the double-quoted form exactly as-is.
  Example: `CREATE OR REPLACE PROCEDURE "Employee Sales by Country"(...)`
- The only permitted changes are the parameter types, body logic, and PL/pgSQL syntax.
- A response that renames the procedure will be rejected and the repair will fail.

## Conversion Rules

Apply these rules when rewriting the procedure:

**Variable declarations (DECLARE block only)**
- T-SQL `DECLARE @var TYPE [= val]` → PL/pgSQL `var TYPE [:= val];`
- Initializers use `:=`, not `=`
- Each DECLARE is on its own line, ends with `;`

**Assignment statements (inside BEGIN…END)**
- `SET @var = expr` → `var := expr;`
- `SELECT @var = expr` (no FROM) → `var := expr;`
- `SELECT @a = col1, @b = col2 FROM t WHERE ...` → `SELECT col1, col2 INTO a, b FROM t WHERE ...;`
- `SELECT @var = col FROM t WHERE ...` → `SELECT col INTO var FROM t WHERE ...;`

**Control flow**
- `IF (cond) BEGIN ... END` → `IF (cond) THEN ... END IF;`
- `ELSE IF` → `ELSIF`
- `WHILE (cond) BEGIN ... END` → `WHILE (cond) LOOP ... END LOOP;`
- Every `IF` block requires `THEN` and must close with `END IF;`
- Every `WHILE` block requires `LOOP` and must close with `END LOOP;`
- Every statement inside BEGIN…END must end with `;`

**RETURN**
- Procedures cannot return values in PostgreSQL: `RETURN expr` → `RETURN;`
- `RETURN(expr)` → `RETURN;`
- **`RETURN QUERY` requires a `SETOF` return type.** If you introduce `RETURN QUERY SELECT ...` you MUST also change the function header from `RETURNS void` (or any scalar type) to `RETURNS SETOF record`. Failing to do so causes: `cannot use RETURN QUERY in a non-SETOF function`.
  - Example fix — change header: `RETURNS void LANGUAGE plpgsql` → `RETURNS SETOF record LANGUAGE plpgsql`
  - Then use: `RETURN QUERY SELECT col1, col2, ... FROM ...;`
  - Add `RETURN;` at the end of the function body (required after the last `RETURN QUERY`).
- **`cannot change whether a procedure has output parameters`** — This means the procedure already exists in SPG with a different OUT parameter signature. The fix is to emit a `DROP PROCEDURE` before the `CREATE OR REPLACE`:
  - Emit: `DROP PROCEDURE IF EXISTS <proc_name>(<param_types>);` immediately before the `CREATE OR REPLACE PROCEDURE` statement.
  - Use the exact parameter types from the new signature in the DROP statement.
  - Example: error says `Use DROP PROCEDURE salesbycategory(character varying,character varying) first` → add `DROP PROCEDURE IF EXISTS salesbycategory(character varying, character varying);` before the CREATE.

**Data types**
- `nvarchar(n)`, `varchar(n)`, `nchar(n)`, `char(n)`, `ntext` → `text`
- `bit` → `boolean`  (comparisons: `= 1` → `= true`, `= 0` → `= false`)
- `int`, `integer` → `integer`
- `bigint` → `bigint`
- `smallint`, `tinyint` → `smallint`
- `decimal(p,s)`, `numeric(p,s)` → `numeric(p,s)`
- `money` → `numeric(19,4)`
- `datetime`, `datetime2` → `timestamp`
- `date` → `date`
- `uniqueidentifier` → `uuid`
- `sysname` → `text`

**Remove entirely**
- `(NOLOCK)`, `(UPDLOCK)`, `(HOLDLOCK)`, `(FORCESEEK)` table hints
- `WITH RECOMPILE`
- `SET NOCOUNT ON/OFF`
- `GO` batch separator

**Functions**
- `ISNULL(a, b)` → `COALESCE(a, b)`
- `GETDATE()` → `NOW()`
- `GETUTCDATE()` → `(NOW() AT TIME ZONE 'UTC')`
- `NEWID()` → `gen_random_uuid()`
- `LEN(x)` → `LENGTH(x)`
- `N'string'` → `'string'`
- String concatenation: `a + b` → `a || b`

**EXEC calls**
- `EXEC dbo.proc arg1, @arg2 OUTPUT` → `-- SPG-EWI: EXEC needs manual CALL conversion\n    CALL dbo.proc(/* arg1, arg2 */);`

---

## Original T-SQL (source of truth)

```sql
{original_tsql}
```

---

## Current PL/pgSQL — Iteration {iteration} (has errors)

```sql
{current_plpgsql}
```

---

## PostgreSQL Error

```
{pg_error}
```

---

## Instructions

1. Fix the specific error shown above and any other issues you spot.
2. Output **only** the corrected `CREATE OR REPLACE PROCEDURE` statement.
3. Do not add explanations, comments about what you changed, or markdown fences.
4. Start your response with `CREATE OR REPLACE PROCEDURE`.
5. The output must be valid PL/pgSQL that compiles in PostgreSQL 18.
