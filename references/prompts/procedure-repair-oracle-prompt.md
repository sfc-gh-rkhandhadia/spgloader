# Oracle PL/SQL → PL/pgSQL Procedure Repair Prompt
# ---------------------------------------------------------------------------
# Template used by repair_procedures.py when the source type is Oracle.
# Placeholders (replaced at runtime):
#   {original_plsql}   — Original Oracle PL/SQL CREATE PROCEDURE/FUNCTION DDL
#   {current_plpgsql}  — Current (failed) PL/pgSQL attempt
#   {pg_error}         — Exact PostgreSQL error message from the deploy attempt
#   {iteration}        — Current iteration number (1-N)
# ---------------------------------------------------------------------------

You are a PostgreSQL 18 expert.  Your task is to fix a stored procedure or function
that was automatically converted from Oracle PL/SQL but fails to compile.

## Conversion Rules

Apply these rules when rewriting the code:

**Procedure signature**
- `CREATE OR REPLACE PROCEDURE name(p IN type, p2 OUT type)` →
  `CREATE OR REPLACE PROCEDURE name(p type, OUT p2 type) LANGUAGE plpgsql AS $$`
- `IN` mode: move param before name → `name type` (IN is default, no keyword needed)
- `OUT` mode: prepend `OUT` → `OUT name type`
- `IN OUT` mode: → `INOUT name type`
- Remove `NOCOPY` hint

**Function signature**
- `CREATE OR REPLACE FUNCTION name RETURN type IS` →
  `CREATE OR REPLACE FUNCTION name() RETURNS type LANGUAGE plpgsql AS $$`
- `RETURN type` → `RETURNS type` (at signature level only)
- `RETURN expr;` inside body → stays as `RETURN expr;` (valid in PG functions)

**IS / AS separator**
- Everything between `IS`/`AS` and `BEGIN` is the DECLARE section
- Output as: `LANGUAGE plpgsql AS $$\nDECLARE\n   var type;\nBEGIN`
- If no local variables: omit DECLARE section entirely

**Oracle type → PostgreSQL type**
- `NUMBER` → `NUMERIC`; `NUMBER(p,0)` with p≤9 → `INTEGER`; p≤18 → `BIGINT`
- `NUMBER(p,s)` → `NUMERIC(p,s)`
- `PLS_INTEGER`, `BINARY_INTEGER`, `SIMPLE_INTEGER`, `POSITIVE`, `NATURAL` → `INTEGER`
- `VARCHAR2(n)` → `TEXT` (or `VARCHAR(n)` if length is significant)
- `VARCHAR2(n CHAR)` → `TEXT`
- `NVARCHAR2`, `CLOB`, `NCLOB`, `LONG` → `TEXT`
- `BLOB`, `RAW(n)`, `LONG RAW` → `BYTEA`
- `DATE` → `TIMESTAMPTZ`  (**critical**: Oracle DATE includes time-of-day)
- `TIMESTAMP` → `TIMESTAMPTZ`
- `TIMESTAMP WITH TIME ZONE` / `TIMESTAMP WITH LOCAL TIME ZONE` → `TIMESTAMPTZ`
- `INTERVAL YEAR TO MONTH` / `INTERVAL DAY TO SECOND` → `INTERVAL`
- `BOOLEAN` → `BOOLEAN` (same)
- `SYS_REFCURSOR` → `REFCURSOR`
- `%TYPE` → look up the column/variable type and use it explicitly
- `%ROWTYPE` → use the table's row type or an explicit record

**Local variable declarations**
- Oracle: `v_count NUMBER := 0;` → PL/pgSQL: `v_count NUMERIC := 0;`
- Oracle: `v_name VARCHAR2(100);` → PL/pgSQL: `v_name TEXT;`
- CURSOR: `CURSOR c IS SELECT ...` → convert to `FOR rec IN SELECT ... LOOP` pattern in body
- Exception variable: `e_custom EXCEPTION;` → remove (use `RAISE EXCEPTION` directly)
- PRAGMA: remove (not needed in PG)
- Collection type: `TYPE t IS TABLE OF NUMBER;` → use `NUMERIC[]` array or a temp table

**Control flow** (largely same as PL/pgSQL, verify these are correct)
- `IF cond THEN ... ELSIF cond THEN ... ELSE ... END IF;` — same in PG ✓
- `LOOP ... EXIT WHEN cond; ... END LOOP;` — same in PG ✓
- `FOR i IN 1..10 LOOP ... END LOOP;` — same in PG ✓
- `WHILE cond LOOP ... END LOOP;` — same in PG ✓
- Cursor FOR loop: `FOR rec IN (SELECT ...) LOOP ... END LOOP;` — same in PG ✓

**DML and assignments**
- `:=` assignment — same in PG ✓
- `SELECT col INTO var FROM t WHERE ...;` — same in PG ✓
- `SELECT col1, col2 INTO v1, v2 FROM t WHERE ...;` — same in PG ✓
- `INSERT ... RETURNING col INTO var;` — same in PG ✓

**Oracle built-ins → PostgreSQL equivalents**
- `SYSDATE` → `NOW()`
- `SYSTIMESTAMP` → `NOW()`
- `SYS_GUID()` → `gen_random_uuid()`
- `NVL(a, b)` → `COALESCE(a, b)`
- `NVL2(a, b, c)` → `CASE WHEN a IS NOT NULL THEN b ELSE c END`
- `DECODE(x, v1, r1, v2, r2, def)` → `CASE x WHEN v1 THEN r1 WHEN v2 THEN r2 ELSE def END`
- `SUBSTR(s, pos, len)` → `SUBSTRING(s FROM pos FOR len)` (or keep SUBSTR — PG accepts it)
- `INSTR(s, sub)` → `POSITION(sub IN s)`
- `TO_NUMBER(s)` → `s::NUMERIC`
- `TO_DATE(s, fmt)` → `TO_DATE(s, fmt)` (same, but format codes may differ)
- `ADD_MONTHS(d, n)` → `d + (n * INTERVAL '1 month')`
- `MONTHS_BETWEEN(d1, d2)` → `EXTRACT(YEAR FROM AGE(d1,d2))*12 + EXTRACT(MONTH FROM AGE(d1,d2))`
- `TRUNC(date)` → `DATE_TRUNC('day', date)`
- `TRUNC(n, d)` → `TRUNC(n, d)` (same)
- `LISTAGG(col, sep) WITHIN GROUP (ORDER BY ...)` → `STRING_AGG(col, sep ORDER BY ...)`
- `seq.NEXTVAL` → `NEXTVAL('seq')`
- `seq.CURRVAL` → `CURRVAL('seq')`
- `FROM DUAL` → remove entirely
- `ROWNUM <= n` → use `LIMIT n` subquery or `ROW_NUMBER() OVER ()`
- `DBMS_OUTPUT.PUT_LINE(x)` → `RAISE NOTICE '%', x;`
- `RAISE_APPLICATION_ERROR(-20xxx, 'msg')` → `RAISE EXCEPTION 'msg';`
- `EXECUTE IMMEDIATE sql` → `EXECUTE sql;`
- `EXECUTE IMMEDIATE sql INTO var` → `EXECUTE sql INTO var;`

**EXCEPTION block** (largely same structure)
- Oracle EXCEPTION section maps directly to PG EXCEPTION section
- Exception names are case-insensitive in PG: `NO_DATA_FOUND` works
- `RAISE;` (re-raise) — same in PG ✓
- `SQLCODE` → `SQLSTATE`
- `SQLERRM` → `SQLERRM` (same) or `SQLERRM || ' [' || SQLSTATE || ']'`

**Bulk operations** (complex — simplify if possible)
- `BULK COLLECT INTO arr` → convert to loop with `ARRAY_AGG` or explicit cursor
- `FORALL i IN arr.FIRST..arr.LAST INSERT INTO t VALUES arr(i)` →
  `FOREACH val IN ARRAY arr LOOP INSERT INTO t VALUES val; END LOOP;`

**Remove entirely**
- `PRAGMA EXCEPTION_INIT(e, -n)` → remove
- `PRAGMA AUTONOMOUS_TRANSACTION` → flag with comment, requires redesign
- Optimizer hints: `/*+ FULL(t) */`, `/*+ INDEX(t idx) */` etc.
- `NOLOGGING` on INSERT
- `NOVALIDATE` on constraints

**Sequence body terminator**
- `END proc_name;` → `END;`

---

## Original Oracle PL/SQL (source of truth)

```sql
{original_plsql}
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
2. Output **only** the corrected `CREATE OR REPLACE PROCEDURE` or `CREATE OR REPLACE FUNCTION` statement.
3. Do not add explanations, comments about what you changed, or markdown fences.
4. Start your response with `CREATE OR REPLACE`.
5. The output must be valid PL/pgSQL that compiles in PostgreSQL 18.
6. If the procedure uses `BULK COLLECT`, `FORALL`, `%TYPE`, `%ROWTYPE`, or `CONNECT BY` that you cannot convert, add a `-- TODO:` comment explaining what needs manual attention but still produce compilable PL/pgSQL.
