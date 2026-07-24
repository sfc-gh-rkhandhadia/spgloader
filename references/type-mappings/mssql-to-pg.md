# MSSQL → Snowflake Postgres Type Mapping

Use this reference when performing LLM-based conversion of MSSQL objects (views, procedures, functions, triggers).

## Data Types

| MSSQL Type | PostgreSQL Type | Notes |
|------------|----------------|-------|
| `INT` | `INTEGER` | |
| `BIGINT` | `BIGINT` | |
| `SMALLINT` | `SMALLINT` | |
| `TINYINT` | `SMALLINT` | PG has no TINYINT |
| `BIT` | `BOOLEAN` | |
| `DECIMAL(p,s)` | `NUMERIC(p,s)` | |
| `NUMERIC(p,s)` | `NUMERIC(p,s)` | |
| `FLOAT` | `DOUBLE PRECISION` | |
| `REAL` | `REAL` | |
| `MONEY` | `NUMERIC(19,4)` | |
| `SMALLMONEY` | `NUMERIC(10,4)` | |
| `CHAR(n)` | `CHAR(n)` | |
| `VARCHAR(n)` | `VARCHAR(n)` | |
| `VARCHAR(MAX)` | `TEXT` | |
| `NCHAR(n)` | `CHAR(n)` | Strip N prefix |
| `NVARCHAR(n)` | `VARCHAR(n)` | Strip N prefix |
| `NVARCHAR(MAX)` | `TEXT` | |
| `TEXT` | `TEXT` | (deprecated in MSSQL) |
| `NTEXT` | `TEXT` | (deprecated in MSSQL) |
| `BINARY(n)` | `BYTEA` | |
| `VARBINARY(n)` | `BYTEA` | |
| `VARBINARY(MAX)` | `BYTEA` | |
| `IMAGE` | `BYTEA` | (deprecated in MSSQL) |
| `UNIQUEIDENTIFIER` | `UUID` | |
| `DATETIME` | `TIMESTAMP` | |
| `DATETIME2` | `TIMESTAMP` | |
| `DATETIMEOFFSET` | `TIMESTAMPTZ` | |
| `SMALLDATETIME` | `TIMESTAMP` | |
| `DATE` | `DATE` | |
| `TIME` | `TIME` | |
| `XML` | `TEXT` or `XML` | SPG supports XML type |
| `ROWVERSION` / `TIMESTAMP` | `BYTEA` | Not a real timestamp |
| `SQL_VARIANT` | `TEXT` | No direct equivalent |
| `HIERARCHYID` | `TEXT` | No direct equivalent |
| `GEOGRAPHY` | `TEXT` | PostGIS needed for spatial |
| `GEOMETRY` | `TEXT` | PostGIS needed for spatial |

## Identity / Auto-Increment

| MSSQL | PostgreSQL |
|-------|-----------|
| `INT IDENTITY(1,1)` | `SERIAL` or `INTEGER GENERATED ALWAYS AS IDENTITY` |
| `BIGINT IDENTITY(1,1)` | `BIGSERIAL` or `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `IDENTITY(seed,increment)` | `GENERATED ALWAYS AS IDENTITY (START WITH seed INCREMENT BY increment)` |

## SQL Functions

| MSSQL Function | PostgreSQL Equivalent |
|---------------|----------------------|
| `ISNULL(a, b)` | `COALESCE(a, b)` |
| `GETDATE()` | `NOW()` or `CURRENT_TIMESTAMP` |
| `GETUTCDATE()` | `NOW() AT TIME ZONE 'UTC'` |
| `SYSDATE` | `NOW()` |
| `TOP N` | `LIMIT N` (move to end of SELECT) |
| `TOP N WITH TIES` | `FETCH FIRST N ROWS WITH TIES` |
| `LEN(s)` | `LENGTH(s)` |
| `CHARINDEX(sub, str)` | `POSITION(sub IN str)` |
| `CHARINDEX(sub, str, pos)` | `POSITION(sub IN SUBSTRING(str FROM pos)) + pos - 1` |
| `SUBSTRING(s, start, len)` | `SUBSTRING(s FROM start FOR len)` |
| `STUFF(s, start, len, repl)` | `OVERLAY(s PLACING repl FROM start FOR len)` |
| `CONVERT(type, expr)` | `CAST(expr AS type)` or `expr::type` |
| `CAST(expr AS type)` | `CAST(expr AS type)` — same |
| `DATEDIFF(part, start, end)` | `EXTRACT(epoch FROM end - start)` (varies by part) |
| `DATEADD(part, n, date)` | `date + (n * INTERVAL '1 part')` |
| `DATENAME(part, date)` | `TO_CHAR(date, 'format')` |
| `DATEPART(part, date)` | `EXTRACT(part FROM date)` |
| `FORMAT(val, fmt)` | `TO_CHAR(val, fmt)` |
| `STRING_AGG(col, sep)` | `STRING_AGG(col, sep)` — same in PG 9.0+ |
| `NEWID()` | `gen_random_uuid()` |
| `NEWSEQUENTIALID()` | `gen_random_uuid()` (sequential not available) |
| `@@ROWCOUNT` | `GET DIAGNOSTICS n = ROW_COUNT` (in PL/pgSQL) |
| `@@IDENTITY` | `lastval()` or `RETURNING id` |
| `SCOPE_IDENTITY()` | `lastval()` or `RETURNING id` |
| `OBJECT_ID('table')` | `'table'::regclass::oid` |
| `DB_NAME()` | `current_database()` |
| `SCHEMA_NAME()` | `current_schema()` |
| `USER_NAME()` | `current_user` |
| `PRINT 'msg'` | `RAISE NOTICE 'msg'` (PL/pgSQL) |

## Control Flow (T-SQL → PL/pgSQL)

| T-SQL | PL/pgSQL |
|-------|---------|
| `IF condition BEGIN ... END` | `IF condition THEN ... END IF;` |
| `IF ... ELSE BEGIN ... END` | `IF ... THEN ... ELSE ... END IF;` |
| `WHILE condition BEGIN ... END` | `WHILE condition LOOP ... END LOOP;` |
| `DECLARE @var TYPE` | `DECLARE var TYPE;` |
| `SET @var = value` | `var := value;` |
| `SELECT @var = col FROM ...` | `SELECT col INTO var FROM ...` |
| `RETURN value` | `RETURN value;` |
| `BEGIN ... END` (block) | `BEGIN ... END;` |
| `BEGIN TRY ... END TRY BEGIN CATCH ... END CATCH` | `BEGIN ... EXCEPTION WHEN OTHERS THEN ... END;` |
| `RAISERROR('msg', 16, 1)` | `RAISE EXCEPTION 'msg';` |
| `THROW 50000, 'msg', 1` | `RAISE EXCEPTION 'msg';` |
| `EXEC stored_proc` | `CALL stored_proc()` or `PERFORM stored_proc()` |
| `INSERT INTO ... VALUES; SELECT SCOPE_IDENTITY()` | `INSERT INTO ... RETURNING id` |

## Procedure / Function Template

```sql
-- MSSQL stored procedure
CREATE PROCEDURE dbo.GetCustomers @status NVARCHAR(50)
AS
BEGIN
    SELECT * FROM dbo.customers WHERE status = @status
END

-- PostgreSQL equivalent
CREATE OR REPLACE FUNCTION get_customers(p_status TEXT)
RETURNS TABLE(id INTEGER, name TEXT, status TEXT) AS $$
BEGIN
    RETURN QUERY
    SELECT c.id, c.name, c.status
    FROM customers c
    WHERE c.status = p_status;
END;
$$ LANGUAGE plpgsql;
```

## Object Naming

- Strip square bracket delimiters: `[dbo].[orders]` → `"dbo"."orders"` or `dbo.orders`
- `dbo` schema maps directly to `public` schema in many cases, but preserve schema names
- Downcase identifiers unless the source explicitly uses mixed-case quoting

## Notes

- `##global_temp` and `#local_temp` tables: document and migrate to regular tables or CTEs
- Computed columns (`AS expr PERSISTED`): convert to `GENERATED ALWAYS AS (expr) STORED`
- Cursors: convert to set-based SQL or use PL/pgSQL `FOR rec IN SELECT ... LOOP`
- `NOLOCK` hint: simply remove — SPG uses MVCC; no equivalent or need
- `WITH (INDEX(...))` hints: remove — SPG planner handles indexing
- `LINKED SERVER` references: flag for manual replacement with `postgres_fdw` or application-level queries
