# Oracle → Snowflake Postgres Type Mapping

Use this reference when performing LLM-based conversion of Oracle objects. pgloader does NOT support Oracle — all objects require LLM conversion.

## Data Types

| Oracle Type | PostgreSQL Type | Notes |
|-------------|----------------|-------|
| `NUMBER` | `NUMERIC` | |
| `NUMBER(p)` | `NUMERIC(p)` | |
| `NUMBER(p,s)` | `NUMERIC(p,s)` | |
| `NUMBER(1)` | `BOOLEAN` | Oracle convention — consider CHECK |
| `INTEGER` | `INTEGER` | Alias for NUMBER(38) in Oracle |
| `FLOAT` | `DOUBLE PRECISION` | |
| `BINARY_FLOAT` | `REAL` | |
| `BINARY_DOUBLE` | `DOUBLE PRECISION` | |
| `CHAR(n)` | `CHAR(n)` | |
| `CHAR(n CHAR)` | `CHAR(n)` | Drop `CHAR` units qualifier |
| `VARCHAR2(n)` | `VARCHAR(n)` | |
| `VARCHAR2(n CHAR)` | `VARCHAR(n)` | Drop `CHAR` units qualifier |
| `NCHAR(n)` | `CHAR(n)` | |
| `NVARCHAR2(n)` | `VARCHAR(n)` | |
| `CLOB` | `TEXT` | |
| `NCLOB` | `TEXT` | |
| `BLOB` | `BYTEA` | |
| `BFILE` | `TEXT` | Store file path; no binary equivalent |
| `RAW(n)` | `BYTEA` | |
| `LONG RAW` | `BYTEA` | |
| `LONG` | `TEXT` | (deprecated in Oracle) |
| `DATE` | `TIMESTAMP` | **Critical**: Oracle DATE includes time component |
| `TIMESTAMP` | `TIMESTAMP` | |
| `TIMESTAMP WITH TIME ZONE` | `TIMESTAMPTZ` | |
| `TIMESTAMP WITH LOCAL TIME ZONE` | `TIMESTAMPTZ` | |
| `INTERVAL YEAR TO MONTH` | `INTERVAL` | |
| `INTERVAL DAY TO SECOND` | `INTERVAL` | |
| `XMLTYPE` | `XML` or `TEXT` | |
| `SDO_GEOMETRY` | `TEXT` | PostGIS for spatial |
| `ROWID` | `TEXT` | Internal Oracle rowid — usually not migrated |
| `UROWID` | `TEXT` | |
| `SYS.ANYDATA` | `TEXT` | No direct equivalent |

## Identity / Sequences

Oracle uses separate SEQUENCE objects; PostgreSQL prefers identity columns.

| Oracle | PostgreSQL |
|--------|-----------|
| `CREATE SEQUENCE seq START WITH 1 INCREMENT BY 1` | `CREATE SEQUENCE seq START 1 INCREMENT 1` |
| `seq.NEXTVAL` | `NEXTVAL('seq')` |
| `seq.CURRVAL` | `CURRVAL('seq')` |
| Column default: `DEFAULT seq.NEXTVAL` | `DEFAULT NEXTVAL('seq')` or `GENERATED ALWAYS AS IDENTITY` |

## SQL Functions

| Oracle Function | PostgreSQL Equivalent |
|----------------|----------------------|
| `SYSDATE` | `NOW()` or `CURRENT_TIMESTAMP` |
| `SYSTIMESTAMP` | `NOW()` |
| `CURRENT_DATE` | `CURRENT_DATE` — same |
| `CURRENT_TIMESTAMP` | `CURRENT_TIMESTAMP` — same |
| `NVL(a, b)` | `COALESCE(a, b)` |
| `NVL2(a, b, c)` | `CASE WHEN a IS NOT NULL THEN b ELSE c END` |
| `NULLIF(a, b)` | `NULLIF(a, b)` — same |
| `DECODE(expr, v1, r1, v2, r2, default)` | `CASE expr WHEN v1 THEN r1 WHEN v2 THEN r2 ELSE default END` |
| `GREATEST(a, b, ...)` | `GREATEST(a, b, ...)` — same |
| `LEAST(a, b, ...)` | `LEAST(a, b, ...)` — same |
| `TRUNC(date, fmt)` | `DATE_TRUNC(fmt, date)` |
| `TRUNC(n, d)` | `TRUNC(n, d)` — same |
| `ROUND(date, fmt)` | `DATE_TRUNC(fmt, date + interval)` — varies |
| `ADD_MONTHS(date, n)` | `date + (n * INTERVAL '1 month')` |
| `MONTHS_BETWEEN(d1, d2)` | `EXTRACT(year FROM age(d1,d2))*12 + EXTRACT(month FROM age(d1,d2))` |
| `LAST_DAY(date)` | `DATE_TRUNC('month', date) + INTERVAL '1 month' - INTERVAL '1 day'` |
| `NEXT_DAY(date, day)` | No direct equivalent; use date arithmetic |
| `TO_DATE(s, fmt)` | `TO_DATE(s, fmt)` — same (format codes differ slightly) |
| `TO_TIMESTAMP(s, fmt)` | `TO_TIMESTAMP(s, fmt)` — same |
| `TO_CHAR(date, fmt)` | `TO_CHAR(date, fmt)` — same |
| `TO_CHAR(n, fmt)` | `TO_CHAR(n, fmt)` — same |
| `TO_NUMBER(s)` | `s::NUMERIC` or `CAST(s AS NUMERIC)` |
| `SUBSTR(s, start, len)` | `SUBSTRING(s FROM start FOR len)` |
| `INSTR(s, sub)` | `POSITION(sub IN s)` |
| `INSTR(s, sub, start)` | `POSITION(sub IN SUBSTRING(s FROM start)) + start - 1` |
| `LENGTH(s)` | `LENGTH(s)` — same |
| `UPPER(s)` | `UPPER(s)` — same |
| `LOWER(s)` | `LOWER(s)` — same |
| `TRIM(s)` | `TRIM(s)` — same |
| `LTRIM(s, chars)` | `LTRIM(s, chars)` — same |
| `RTRIM(s, chars)` | `RTRIM(s, chars)` — same |
| `LPAD(s, n, fill)` | `LPAD(s, n, fill)` — same |
| `RPAD(s, n, fill)` | `RPAD(s, n, fill)` — same |
| `REPLACE(s, old, new)` | `REPLACE(s, old, new)` — same |
| `REGEXP_REPLACE(s, pat, repl)` | `REGEXP_REPLACE(s, pat, repl)` — same |
| `REGEXP_LIKE(s, pat)` | `s ~ pat` |
| `LISTAGG(col, sep) WITHIN GROUP (ORDER BY ...)` | `STRING_AGG(col, sep ORDER BY ...)` |
| `SYS_GUID()` | `gen_random_uuid()` |
| `ROWNUM` | Use `ROW_NUMBER() OVER ()` in subquery, or `LIMIT`/`OFFSET` |
| `LEVEL` (CONNECT BY) | Use recursive CTE: `WITH RECURSIVE ...` |
| `CONNECT BY PRIOR` | Replace with `WITH RECURSIVE ...` |
| `DUAL` (FROM DUAL) | Remove `FROM DUAL` — PG `SELECT` needs no FROM |
| `ROWID` reference | Avoid; use primary key instead |

## ROWNUM → LIMIT

```sql
-- Oracle
SELECT * FROM orders WHERE ROWNUM <= 10;

-- PostgreSQL
SELECT * FROM orders LIMIT 10;
```

## Hierarchical Queries (CONNECT BY)

```sql
-- Oracle
SELECT id, parent_id, name, LEVEL
FROM categories
START WITH parent_id IS NULL
CONNECT BY PRIOR id = parent_id;

-- PostgreSQL
WITH RECURSIVE category_tree AS (
  SELECT id, parent_id, name, 1 AS level
  FROM categories WHERE parent_id IS NULL
  UNION ALL
  SELECT c.id, c.parent_id, c.name, ct.level + 1
  FROM categories c
  JOIN category_tree ct ON ct.id = c.parent_id
)
SELECT * FROM category_tree;
```

## PL/SQL → PL/pgSQL

| PL/SQL | PL/pgSQL |
|--------|---------|
| `CREATE OR REPLACE PROCEDURE name(p IN type)` | `CREATE OR REPLACE PROCEDURE name(p type)` |
| `CREATE OR REPLACE FUNCTION name RETURN type` | `CREATE OR REPLACE FUNCTION name() RETURNS type` |
| `IS` / `AS` keyword | `AS $$` ... `$$ LANGUAGE plpgsql` |
| `DECLARE ... BEGIN ... EXCEPTION ... END;` | Same structure |
| `var type := value;` or `var type DEFAULT value;` | `var type := value;` |
| `var := expr;` | Same |
| `IF cond THEN ... ELSIF cond THEN ... ELSE ... END IF;` | Same |
| `LOOP ... EXIT WHEN cond; ... END LOOP;` | Same |
| `FOR i IN 1..10 LOOP ... END LOOP;` | Same |
| `FOR rec IN (SELECT ...) LOOP ... END LOOP;` | Same |
| `OPEN cur FOR SELECT ...; FETCH cur INTO var; CLOSE cur;` | Use `FOR rec IN SELECT ... LOOP` instead |
| `RAISE_APPLICATION_ERROR(-20001, 'msg')` | `RAISE EXCEPTION 'msg';` |
| `DBMS_OUTPUT.PUT_LINE('msg')` | `RAISE NOTICE 'msg';` |
| `EXECUTE IMMEDIATE sql_str` | `EXECUTE sql_str;` |
| `EXECUTE IMMEDIATE sql_str INTO var` | `EXECUTE sql_str INTO var;` |
| `COMMIT;` | `COMMIT;` — same (or remove if in function) |
| `ROLLBACK;` | `ROLLBACK;` — same |
| `%TYPE` attribute | Use explicit type |
| `%ROWTYPE` attribute | Use `TABLE_NAME%ROWTYPE` or explicit record type |
| `IN OUT` parameter mode | `INOUT` in PL/pgSQL |
| `RETURNING INTO var` (DML) | `RETURNING col INTO var` |
| Bulk `COLLECT INTO` | `ARRAY_AGG(...)` or cursor loop |

## Procedure Template

```sql
-- Oracle procedure
CREATE OR REPLACE PROCEDURE get_employees(p_dept_id IN NUMBER) AS
BEGIN
    FOR rec IN (SELECT * FROM employees WHERE dept_id = p_dept_id) LOOP
        DBMS_OUTPUT.PUT_LINE(rec.name);
    END LOOP;
END;

-- PostgreSQL equivalent
CREATE OR REPLACE PROCEDURE get_employees(p_dept_id INTEGER) AS $$
BEGIN
    FOR rec IN SELECT * FROM employees WHERE dept_id = p_dept_id LOOP
        RAISE NOTICE '%', rec.name;
    END LOOP;
END;
$$ LANGUAGE plpgsql;
```

## Notes

- `DUAL` table: remove `FROM DUAL` entirely — PostgreSQL `SELECT` works without a table
- Oracle packages: no direct equivalent — split into schema-qualified functions/procedures
- Oracle types (`CREATE TYPE ... AS OBJECT`): convert to PostgreSQL composite types or tables
- `PRAGMA AUTONOMOUS_TRANSACTION`: no equivalent — refactor to use `dblink` or redesign
- `PRAGMA EXCEPTION_INIT`: remove; use standard exception names
- Grants on packages: migrate as grants on individual functions/procedures
- Oracle synonyms: create PostgreSQL views or use `search_path` instead
- `NOLOGGING` / `APPEND` hints on INSERT: remove
- `NOVALIDATE` constraints: add constraints WITHOUT VALID, then validate separately
