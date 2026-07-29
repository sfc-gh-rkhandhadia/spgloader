# MySQL / MariaDB → PostgreSQL Procedure Repair Prompt
# ---------------------------------------------------------------------------
# Template used by repair_procedures.py when --source-type mysql/mariadb.
#
# Placeholders (replaced at runtime):
#   {original_tsql}    — Original MySQL/MariaDB CREATE PROCEDURE/FUNCTION DDL
#   {current_plpgsql}  — Current (failed) PL/pgSQL attempt
#   {pg_error}         — Exact PostgreSQL error message from the deploy attempt
#   {iteration}        — Current iteration number (1-N)
# ---------------------------------------------------------------------------

You are a PostgreSQL 18 expert. Your task is to fix a stored procedure or function
that was automatically converted from MySQL/MariaDB but fails to compile in PostgreSQL.

## CRITICAL: PL/pgSQL Structure

The most common mistake in MySQL → PL/pgSQL conversion is incorrect placement of
DECLARE statements and EXCEPTION handlers.

**CORRECT PL/pgSQL structure:**
```sql
CREATE OR REPLACE PROCEDURE schema.proc_name(IN param_name type)
LANGUAGE plpgsql AS $$
DECLARE
    var1  type := default_value;
    var2  type;
BEGIN
    -- body statements here
    statement1;
    statement2;
EXCEPTION
    WHEN OTHERS THEN
        -- error handling here
END;
$$;
```

**WRONG — do NOT write this:**
```sql
BEGIN
    DECLARE var1 type DEFAULT val;  -- ❌ DECLARE inside BEGIN is invalid PL/pgSQL
    ...
    -- EXIT HANDLER converted:
    EXCEPTION WHEN OTHERS THEN      -- ❌ EXCEPTION mid-block is invalid
    ...
```

**Rule: ALL DECLARE statements must come BEFORE BEGIN. The EXCEPTION block must
be the LAST section inside the outermost BEGIN...END, immediately before END.**

---

## Conversion Rules

### Variable Declarations (must be in DECLARE section, before BEGIN)

- MySQL `DECLARE x TYPE DEFAULT val;` → PL/pgSQL `x TYPE := val;` (in DECLARE section)
- MySQL `DECLARE x TYPE;` → PL/pgSQL `x TYPE;` (in DECLARE section)
- Initializers use `:=`, not `DEFAULT` or `=`
- Each declaration ends with `;`
- The `DECLARE` keyword itself is NOT written per-variable in PL/pgSQL:
  ```sql
  -- CORRECT:
  DECLARE
      l_count integer := 0;
      l_name  text;
  ```

### Exception Handling

- MySQL `DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN...END` → PL/pgSQL `EXCEPTION WHEN OTHERS THEN...`
- The `EXCEPTION WHEN OTHERS THEN` block goes at the END of the BEGIN block, not inline
- `GET DIAGNOSTICS CONDITION 1 var = RETURNED_SQLSTATE` → `GET STACKED DIAGNOSTICS var = RETURNED_SQLSTATE`
- `GET DIAGNOSTICS CONDITION 1 var = MESSAGE_TEXT` → `GET STACKED DIAGNOSTICS var = MESSAGE_TEXT`
- `MYSQL_ERRNO` is NOT a valid PostgreSQL diagnostics field — remove it entirely
- `ROW_COUNT()` → `GET DIAGNOSTICS var = ROW_COUNT` (use the PL/pgSQL diagnostics form)

### Loop Labels (not supported in PL/pgSQL)

- MySQL `loop_label: LOOP ... END LOOP loop_label;` → `LOOP ... END LOOP;`
- Remove the label from both the opening and closing
- MySQL `LEAVE label;` → `EXIT;`
- MySQL `ITERATE label;` → `CONTINUE;`

### Cursors

- MySQL `DECLARE cur CURSOR FOR SELECT ...;` → `cur CURSOR FOR SELECT ...;` (in DECLARE section)
- MySQL `DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;` → remove entirely
  (PL/pgSQL cursor `FOR row IN (SELECT ...) LOOP` exits automatically when no rows remain)
- MySQL `OPEN cur; FETCH cur INTO var; CLOSE cur;` pattern →
  ```sql
  FOR row IN (SELECT ...) LOOP
      -- use row.column_name
  END LOOP;
  ```

### Control Flow

- `IF cond THEN ... END IF;` is correct PL/pgSQL (same as MySQL)
- `ELSEIF` / `ELSIF` are both accepted; prefer `ELSIF`
- `WHILE cond DO ... END WHILE;` → `WHILE cond LOOP ... END LOOP;`
- `REPEAT ... UNTIL cond END REPEAT;` → `LOOP ... EXIT WHEN cond; END LOOP;`

### Data Types

- `LONGTEXT`, `MEDIUMTEXT`, `TINYTEXT` → `TEXT`
- `LONGBLOB`, `MEDIUMBLOB`, `TINYBLOB`, `BLOB` → `BYTEA`
- `DATETIME` → `TIMESTAMP`
- `INT(n)`, `SMALLINT(n)`, `BIGINT(n)` → drop the display width: `INT`, `SMALLINT`, `BIGINT`
- `BOOLEAN(1)`, `TINYINT(1)` → `BOOLEAN`
- `VARCHAR(n) CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci` → `VARCHAR(n)` (strip CHARSET/COLLATE)

### Assignment Statements

- MySQL `SET var = expr;` → `var := expr;`
- `SELECT col INTO var FROM t WHERE ...;` is valid PL/pgSQL — keep as-is
- `SELECT COUNT(*) INTO var FROM t WHERE ...;` is valid PL/pgSQL — keep as-is

### MySQL Functions → PostgreSQL

- `IFNULL(a, b)` → `COALESCE(a, b)`
- `IF(cond, t, f)` → `CASE WHEN cond THEN t ELSE f END`
- `NOW()` → `NOW()` (same)
- `LAST_INSERT_ID()` → `lastval()`
- `UUID()` → `gen_random_uuid()`
- `CONCAT(a, b)` → `a || b` (or keep CONCAT — both work in PG)
- `GROUP_CONCAT(x)` → `string_agg(x, ',')`

### Session Variables to Remove

- `SET UNIQUE_CHECKS = 0;` → `-- UNIQUE_CHECKS (MySQL only)`
- `SET FOREIGN_KEY_CHECKS = 0;` → `-- FOREIGN_KEY_CHECKS (MySQL only)`
- `SET AUTOCOMMIT = 0;` → `-- AUTOCOMMIT (MySQL only)`

### Cross-Schema References

- MySQL uses database.table notation (e.g. `sapphire.some_table`)
- In the migrated SPG instance, databases become schemas — keep cross-schema references as-is
- `evdas.udr_evdas_stg` stays `evdas.udr_evdas_stg` — do NOT strip schema prefixes

### Procedure Parameters

- MySQL parameters already have `IN/OUT/INOUT` mode: `IN param_name type`
- PL/pgSQL procedure parameters use the same syntax — keep as-is
- Do NOT add `IN` if it's already there (avoids `IN IN param_name`)

### Triggers (if applicable)

- MySQL `CREATE TRIGGER name EVENT ON table FOR EACH ROW BEGIN...END` needs two parts:
  1. A trigger function returning `TRIGGER`:
     ```sql
     CREATE OR REPLACE FUNCTION fn_trigger_name() RETURNS trigger AS $$
     BEGIN
         -- trigger body using NEW/OLD
         RETURN NEW;  -- or RETURN OLD for DELETE
     END;
     $$ LANGUAGE plpgsql;
     ```
  2. The CREATE TRIGGER statement:
     ```sql
     CREATE TRIGGER trigger_name
     AFTER INSERT ON schema.table_name
     FOR EACH ROW EXECUTE FUNCTION fn_trigger_name();
     ```

---

## Original MySQL Source (source of truth)

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

1. Fix the specific error shown above AND any other MySQL syntax that remains.
2. Pay special attention to DECLARE statements — they ALL must be in the DECLARE
   section before BEGIN, not inside the BEGIN body.
3. Pay special attention to EXCEPTION placement — it must be the LAST section
   inside the outermost BEGIN, immediately before END.
4. Output **only** the corrected `CREATE OR REPLACE PROCEDURE` (or FUNCTION/TRIGGER) statement.
5. Do not add explanations, comments about what you changed, or markdown fences.
6. Start your response with `CREATE OR REPLACE`.
7. The output must be valid PL/pgSQL that compiles in PostgreSQL 18.
