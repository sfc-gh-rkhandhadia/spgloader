# spgloader EWI Code Catalog

SPG-EWI codes annotate converted SQL files and appear in the assessment report.
Format in converted files: `-- ** SPG-EWI-XXXX SEVERITY: Title **`

All BLOCK and WARN codes map directly to rules in `references/spg-compatibility.md`.

---

## BLOCK Codes — Hard Stop (migration cannot proceed)

| Code | Title | SPG Rule | Detection Pattern | Resolution |
|------|-------|----------|-------------------|------------|
| `SPG-BLOCK-001` | Non-PL/pgSQL procedural language | SPG supports PL/pgSQL only | `LANGUAGE plpython3u/plperl/plv8/...` in DDL | Remove or rewrite using PL/pgSQL |
| `SPG-BLOCK-002` | Superuser creation or assumption | No superusers in SPG | `CREATE ROLE ... SUPERUSER`, `SET ROLE postgres` | Remove superuser grants; use `snowflake_admin` |
| `SPG-BLOCK-003` | ALTER SYSTEM statement | ALTER SYSTEM blocked | `ALTER SYSTEM` keyword | Remove; contact Snowflake support for config changes |
| `SPG-BLOCK-004` | Filesystem access function | No filesystem access | `pg_read_file`, `lo_import`, `COPY ... PROGRAM` | Remove filesystem operations; use cloud storage instead |
| `SPG-BLOCK-005` | Direct system catalog modification | Catalog modifications blocked | `UPDATE/INSERT/DELETE pg_catalog.*` | Remove catalog modifications |
| `SPG-BLOCK-006` | Extension not in SPG catalog | Custom extensions not allowed | `CREATE EXTENSION <unlisted_ext>` | Use an SPG-supported alternative or refactor |
| `SPG-BLOCK-007` | Exceeds 64-role limit | Max 64 roles per SPG instance | Role count > 64 in source | Consolidate roles before migration |
| `SPG-BLOCK-008` | Exceeds 32-database limit | Max 32 databases per SPG instance | Database count > 32 in source | Consolidate databases before migration |

---

## WARN Codes — Confirmation Required

| Code | Title | SPG Rule | Detection Pattern | Auto-Resolution |
|------|-------|----------|-------------------|-----------------|
| `SPG-WARN-001` | Spatial/geometry type detected | PostGIS must be enabled first | `GEOGRAPHY`, `GEOMETRY`, `SDO_GEOMETRY` column | `CREATE EXTENSION IF NOT EXISTS postgis;` |
| `SPG-WARN-002` | Superuser-level operation | `snowflake_admin` is not full superuser | Operations requiring superuser beyond creation | Review; test with `snowflake_admin` |
| `SPG-WARN-003` | Protected server config reference | Snowflake manages these params | `SET max_connections`, `SET shared_buffers`, etc. | Remove SET statements |
| `SPG-WARN-004` | Oracle package or synonym | No PG equivalent for packages | `CREATE PACKAGE` in Oracle DDL | Split into functions/procedures; use `orafce` |
| `SPG-WARN-005` | Scheduled job reference | Must use pg_cron in SPG | `DBMS_SCHEDULER`, `pg_agent` job | `CREATE EXTENSION IF NOT EXISTS pg_cron;` then migrate job |
| `SPG-WARN-006` | Non-Postgres FDW | Only `postgres_fdw` available | `FOREIGN DATA WRAPPER oracle_fdw/mysql_fdw/tds_fdw` | Use `postgres_fdw` or remove FDW dependency |
| `SPG-WARN-007` | Cursor loop in procedure | Cursors supported but review recommended | `DECLARE cursor`, `OPEN cursor`, `FETCH cursor` | Review converted PL/pgSQL; consider set-based rewrite |
| `SPG-WARN-008` | Dynamic SQL dialect syntax | PG EXECUTE syntax differs | `EXECUTE IMMEDIATE`, `sp_executesql`, `PREPARE ... FROM` | Review converted EXECUTE statement |
| `SPG-WARN-009` | PIVOT expression in view | Auto-converts to CTE; FIX-REQUIRED if parse fails | `PIVOT(` in view DDL | Verify `wave_2_views_fixed/` after Phase 4; manually rewrite if FIX-REQUIRED |
| `SPG-WARN-010` | UDTT parameter in procedure | Cannot auto-migrate table-valued params | `READONLY` or `TABLE TYPE` in proc params | Rewrite to temp table or JSON parameter; object excluded from execution parity |
| `SPG-WARN-011` | Cross-database reference in MySQL view | No cross-DB access in PostgreSQL | Three-part `db.schema.table` name in MySQL view DDL | Include the referenced DB in migration scope or use `postgres_fdw` |
| `SPG-WARN-012` | Potential UNION branch type mismatch | PG requires exact type compat across UNION branches | UNION with mixed date and text expressions | Add explicit `CAST` to the mismatched branch if deploy fails |
| `SPG-WARN-013` | Potential implicit integer/text coercion in JOIN | PG requires explicit cast | `ObjectKey` (varchar) joined to an integer ID column | Add `::integer` or `::text` to the JOIN ON clause |
| `SPG-WARN-014` | TINYINT(1) mapping choice required (MySQL) | TINYINT(1) may be boolean flag or small integer | `TINYINT(1)` columns in MySQL/MariaDB schema | Skill will ask: BOOLEAN (convention) or SMALLINT (numeric) |

---

## RESOLVE Codes — Advisory (auto-resolution generated)

| Code | Title | When Triggered | Auto-Resolution |
|------|-------|---------------|-----------------|
| `SPG-RESOLVE-001` | Extension prereq: postgis | Spatial types detected | `CREATE EXTENSION IF NOT EXISTS postgis;` |
| `SPG-RESOLVE-002` | Extension prereq: orafce | Oracle source database | `CREATE EXTENSION IF NOT EXISTS orafce;` |
| `SPG-RESOLVE-003` | Extension prereq: uuid-ossp | UUID generation functions | `CREATE EXTENSION IF NOT EXISTS "uuid-ossp";` |
| `SPG-RESOLVE-004` | Extension prereq: pg_trgm | Fuzzy string matching | `CREATE EXTENSION IF NOT EXISTS pg_trgm;` |
| `SPG-RESOLVE-005` | Extension prereq: pgcrypto | Cryptographic functions | `CREATE EXTENSION IF NOT EXISTS pgcrypto;` |

---

## INFO Codes — Informational Annotations

These appear as inline comments in converted SQL files only (not in the assessment report).

| Code | Title | Example original | Example annotation |
|------|-------|-----------------|-------------------|
| `SPG-EWI-0001` | Type mapping applied | `NVARCHAR(100)` → `TEXT` | `-- ** SPG-EWI-0001 INFO: NVARCHAR → TEXT **` |
| `SPG-EWI-0002` | Function replaced with PG equivalent | `ISNULL(x, y)` → `COALESCE(x, y)` | `-- ** SPG-EWI-0002 INFO: ISNULL → COALESCE **` |
| `SPG-EWI-0004` | Procedure converted to PL/pgSQL | T-SQL `BEGIN ... END` | `-- ** SPG-EWI-0004 WARN: verify business logic **` |
| `SPG-EWI-0005` | Trigger restructured | T-SQL trigger → PG trigger function | `-- ** SPG-EWI-0005 WARN: verify trigger behavior **` |
| `SPG-EWI-0006` | ROWNUM/TOP → LIMIT | `TOP 10` or `WHERE ROWNUM <= 10` | `-- ** SPG-EWI-0006 INFO: TOP/ROWNUM → LIMIT **` |
| `SPG-EWI-0007` | CONNECT BY → recursive CTE | Oracle `CONNECT BY PRIOR` | `-- ** SPG-EWI-0007 WARN: verify CTE hierarchy **` |
| `SPG-EWI-0008` | Cursor → set-based | Cursor loop | `-- ** SPG-EWI-0008 WARN: cursor converted; verify equivalence **` |
| `SPG-EWI-0009` | No PG equivalent → TEXT | Oracle `XMLTYPE` or `HIERARCHYID` | `-- ** SPG-EWI-0009 WARN: type mapped to TEXT; verify data **` |
| `SPG-EWI-0010` | Spatial type needs PostGIS | `GEOGRAPHY` column | `-- ** SPG-EWI-0010 WARN: PostGIS extension required **` |
| `SPG-EWI-0011` | Dialect hint removed | `WITH (NOLOCK)`, `USE INDEX(...)` | `-- ** SPG-EWI-0011 INFO: hint removed **` |
| `SPG-EWI-0012` | Oracle DUAL removed | `SELECT 1 FROM DUAL` | `-- ** SPG-EWI-0012 INFO: FROM DUAL removed **` |
| `SPG-EWI-0013` | Package/synonym/linked server | Oracle PACKAGE | `-- ** SPG-EWI-0013 ERROR: manual migration required **` |

---

## How EWI Codes Appear in Converted Files

```sql
-- ** SPG-EWI-0004 WARN: Stored procedure converted to PL/pgSQL — verify business logic **
-- ** SPG-EWI-0002 INFO: ISNULL → COALESCE **
-- ** SPG-EWI-0011 INFO: NOLOCK hint removed **
CREATE OR REPLACE FUNCTION get_orders(p_status TEXT)
RETURNS TABLE(id INTEGER, status TEXT, total NUMERIC) AS $$
BEGIN
    RETURN QUERY
    SELECT o.id, o.status, o.total
    FROM orders o  -- (NOLOCK removed)
    WHERE o.status = COALESCE(p_status, o.status);  -- ISNULL → COALESCE
END;
$$ LANGUAGE plpgsql;
```

---

## Severity Summary

| Severity | Count | Migration impact |
|----------|-------|-----------------|
| BLOCK | 8 codes | Hard stop — must resolve before migration |
| WARN | 14 codes | Confirmation required — acknowledge before proceeding |
| RESOLVE | 5 codes | Advisory — pre-deploy extensions auto-generated |
| INFO | 13+ codes | Annotation only — no stop |
