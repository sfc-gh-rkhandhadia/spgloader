# User-Defined Table Types (UDTT) Migration Guide

## What Are UDTTs?

SQL Server supports **User-Defined Table Types** — named table schemas that can be
passed as parameters to stored procedures via **Table-Valued Parameters (TVPs)**:

```sql
-- SQL Server: define a named table shape
CREATE TYPE [dbo].[IDList] AS TABLE (
    [ID] INT NOT NULL
)
GO

-- Use it as a stored procedure parameter
CREATE PROCEDURE [dbo].[GetItems] (@IDs dbo.IDList READONLY)
AS BEGIN
    SELECT * FROM Items WHERE ItemID IN (SELECT ID FROM @IDs)
END
```

**PostgreSQL has no direct UDTT equivalent.** Choose a replacement based on the
UDTT's structure and how procedures use it.

---

## PostgreSQL Equivalents

| UDTT pattern | PostgreSQL replacement | When to use |
|---|---|---|
| Single-column INT list | `INTEGER[]` array parameter | Simple ID filtering |
| Single-column TEXT list | `TEXT[]` array parameter | String set operations |
| Key→value pairs | `JSONB` parameter | Dict-style lookups |
| Structured rows (2–5 cols) | `CREATE TYPE t AS (col1 TYPE, ...)` + array | Row-at-a-time processing |
| Temporary tabular input | `CREATE TEMP TABLE t (...)` per session | Complex multi-column sets |

---

## Migration Patterns

### 1 — Simple list → Array parameter

```sql
-- SQL Server: UDTT with one column
CREATE TYPE dbo.IDList AS TABLE (ID INT NOT NULL)

-- Stored procedure parameter:
CREATE PROCEDURE dbo.GetItems (@IDs dbo.IDList READONLY)

-- PostgreSQL: replace with INTEGER[]
CREATE OR REPLACE FUNCTION dbo.get_items(ids INTEGER[])
RETURNS TABLE (...)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
        SELECT * FROM items WHERE item_id = ANY(ids);
END;
$$;
```

### 2 — Key/value UDTT → JSONB

```sql
-- SQL Server: dictionary UDTT
CREATE TYPE dbo.StringMap AS TABLE (Key NVARCHAR(255), Value NVARCHAR(255))

-- PostgreSQL: JSONB parameter
CREATE OR REPLACE FUNCTION dbo.process_map(dict JSONB)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    v_key TEXT;
    v_val TEXT;
BEGIN
    FOR v_key, v_val IN SELECT key, value FROM jsonb_each_text(dict)
    LOOP
        -- process each pair
    END LOOP;
END;
$$;
```

### 3 — Structured UDTT → Composite type

```sql
-- SQL Server: multi-column UDTT
CREATE TYPE dbo.ContactInfo AS TABLE (
    FirstName NVARCHAR(50),
    LastName  NVARCHAR(50),
    Email     NVARCHAR(255)
)

-- PostgreSQL: composite type + array
CREATE TYPE dbo.contact_info AS (
    first_name TEXT,
    last_name  TEXT,
    email      TEXT
);

-- Procedure parameter becomes:
CREATE OR REPLACE FUNCTION dbo.import_contacts(contacts dbo.contact_info[])
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    c dbo.contact_info;
BEGIN
    FOREACH c IN ARRAY contacts LOOP
        INSERT INTO contacts_table VALUES (c.first_name, c.last_name, c.email);
    END LOOP;
END;
$$;
```

---

## SPG EWI Code

UDTTs found in the DDL are annotated with **SPG-WARN-007** (TVP / UDTT detected):

```
-- [SPG-WARN-007] Table-valued parameter / UDTT — no direct PG equivalent.
-- Resolution: replace with array, JSONB, or composite type parameter.
```

This is a WARN (not BLOCK) because the table structure can still be migrated;
only the procedures that *use* the type need manual attention.

---

## Assessment Checklist

Before migrating procedures that reference UDTTs:

1. Run `assess.py` — it flags all UDTT references with SPG-WARN-007
2. For each flagged UDTT, decide on replacement strategy (array / JSONB / composite)
3. Create the PG types in the target schema before deploying procedures
4. Update procedure signatures to use the new parameter types
5. Update procedure bodies to use array/JSON access patterns instead of TVP syntax

---

## Detecting UDTTs in a Source Database

```sql
-- SQL Server: list all UDTTs in the database
SELECT
    s.name            AS schema_name,
    t.name            AS type_name,
    c.name            AS column_name,
    tp.name           AS column_type,
    c.max_length,
    c.is_nullable
FROM sys.table_types t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.columns c ON c.object_id = t.type_table_object_id
JOIN sys.types  tp ON tp.user_type_id = c.user_type_id
ORDER BY s.name, t.name, c.column_id;
```
