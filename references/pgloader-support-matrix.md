# pgloader Support Matrix — spgloader

pgloader 3.6.x is installed at `/opt/homebrew/bin/pgloader`.

## Object Support by Source Database

| Object Type              | MSSQL | MySQL | Oracle |
|--------------------------|-------|-------|--------|
| Tables (schema + data)   | YES   | YES   | NO     |
| Indexes                  | YES   | YES   | NO     |
| Primary keys             | YES   | YES   | NO     |
| Foreign keys             | YES   | YES   | NO     |
| Default values           | YES   | YES   | NO     |
| NOT NULL constraints     | YES   | YES   | NO     |
| CHECK constraints        | NO    | NO    | NO     |
| Views                    | NO    | NO    | NO     |
| Stored procedures        | NO    | NO    | NO     |
| Functions                | NO    | NO    | NO     |
| Triggers                 | NO    | NO    | NO     |
| Sequences / auto-inc     | YES (reset) | YES (reset) | NO |
| Schemas (namespaces)     | YES (mapped) | N/A (DB = schema) | NO |

**Oracle**: pgloader has no Oracle driver. All Oracle objects are converted by LLM
using `references/type-mappings/oracle-to-pg.md`.

## What pgloader Does NOT Migrate (requires LLM conversion)

For MSSQL and MySQL, the following require LLM-based conversion:

- **Views** — SQL dialect differences (T-SQL TOP/NOLOCK, MySQL backtick syntax)
- **Stored procedures** — T-SQL / MySQL SQL → PL/pgSQL
- **Functions** — same as procedures
- **Triggers** — trigger body code; also need a trigger function wrapper in PG
- **Computed columns** — MSSQL only; convert to regular columns or generated columns
- **Temp tables** (`##temp`, `#local`) — not migrated; document for manual review
- **Linked server references** — not applicable to SPG
- **MSSQL-specific types** — `xml`, `hierarchyid`, `geography`, `geometry`
  (pgloader casts xml→text but geography/geometry need PostGIS or TEXT)

## pgloader Cast Rules Applied

### MSSQL → PostgreSQL

| MSSQL type        | PostgreSQL type  | Notes |
|-------------------|-----------------|-------|
| `uniqueidentifier`| `uuid`          | |
| `bit`             | `boolean`       | 0/1 → false/true |
| `tinyint`         | `smallint`      | |
| `money`           | `numeric`       | |
| `smallmoney`      | `numeric`       | |
| `datetime`        | `timestamptz`   | |
| `datetime2`       | `timestamptz`   | |
| `datetimeoffset`  | `timestamptz`   | |
| `smalldatetime`   | `timestamp`     | |
| `nvarchar`        | `text`          | |
| `nchar`           | `text`          | |
| `ntext`           | `text`          | |
| `xml`             | `text`          | |
| `image`           | `bytea`         | |
| `varbinary`       | `bytea`         | |
| `float`           | `float`         | |

### MySQL → PostgreSQL

| MySQL type        | PostgreSQL type  | Notes |
|-------------------|-----------------|-------|
| `tinyint(1)`      | `boolean`       | Drops typemod |
| `bit`             | `boolean`       | |
| `datetime`        | `timestamptz`   | |
| `timestamp`       | `timestamptz`   | |
| `year`            | `integer`       | |
| `enum`            | `text`          | Consider CHECK constraint |
| `set`             | `text`          | |
| `json`            | `jsonb`         | |
| `longtext`        | `text`          | |
| `mediumtext`      | `text`          | |
| `tinytext`        | `text`          | |
| `longblob`        | `bytea`         | |
| `mediumblob`      | `bytea`         | |
| `tinyblob`        | `bytea`         | |
| `AUTO_INCREMENT`  | `serial`/`GENERATED ALWAYS AS IDENTITY` | pgloader resets sequence |

## pgloader .load File Format Quick Reference

```
LOAD DATABASE
     FROM  <source-dsn>
     INTO  <target-dsn>

WITH include drop,
     create tables,
     create indexes,
     reset sequences,
     foreign keys,
     downcase identifiers

CAST ...

SET work_mem to '256MB';
```

`downcase identifiers` — converts MSSQL/MySQL mixed-case names to lowercase in PG.
Remove this if the source schema uses case-sensitive names intentionally.

## Known pgloader Issues and Workarounds

| Issue | Symptom | Fix |
|-------|---------|-----|
| SSL handshake error | `SSL SYSCALL error` on local Docker | Add `?sslmode=disable` to source DSN |
| ODBC driver missing (MSSQL) | `ODBC driver not found` | Install `ODBC Driver 18 for SQL Server` |
| Large tables OOM | pgloader crashes mid-load | Add `WITH rows per range = 50000` |
| Identity column conflict | PK violation on insert | pgloader handles with `reset sequences` — verify post-load |
| MySQL `utf8mb3` warning | Type cast warning | Harmless; utf8mb3 maps to text |
