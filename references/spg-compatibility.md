# Snowflake Postgres (SPG) Compatibility Reference

Source: https://docs.snowflake.com/en/user-guide/snowflake-postgres/

This file is the source of truth for the SPG Compatibility Assessment guardrail.
All BLOCK and WARN detection rules in `lib/spgloader/reporting/assessment.py` are
grounded in the rules documented here.

---

## 1. Regional Availability

SPG is available on **AWS and Azure only**. GCP is NOT supported.

If your Snowflake account is on GCP, Snowflake Postgres cannot be provisioned.

**Supported CSPs and regions:**
- AWS: us-east-1, us-east-2, us-west-2, eu-west-1, eu-central-1, ap-southeast-1, ap-northeast-1, and more
- Azure: eastus2, westus2, northeurope, westeurope, japaneast, and more

---

## 2. Supported PostgreSQL Versions

SPG supports PostgreSQL major versions: **16, 17, and 18** only.

Minor versions are automatically managed by Snowflake.

---

## 3. Procedural Languages (CRITICAL)

**Only PL/pgSQL is supported.**

The following are NOT available in SPG:
- `plpython3u` / `plpython2u` (PL/Python)
- `plperlu` / `plperl` (PL/Perl)
- `pltcl` / `pltclu` (PL/Tcl)
- `plv8` / `plcoffee` / `plls` (PL/V8 / JavaScript variants)
- `plr` (PL/R)
- `pljava` (PL/Java)
- Custom `.so` / `.dll` procedural languages

**Assessment code:** `SPG-BLOCK-001` — hard stop if detected.

---

## 4. Role Limitations

SPG automatically creates two managed roles:

### `snowflake_admin`
- High-privilege but NOT a full superuser
- Can: create/manage roles, databases, manage replication, bypass RLS
- Cannot: assume superuser roles (`postgres`, `snowflake_superuser`), run `ALTER SYSTEM`,
  access filesystem, modify system catalog tables, create other superusers

### `application`
- Non-superuser role for application use
- Default permissions to create objects in `postgres` database

### Hard limits
- **Maximum 64 roles** per instance → `SPG-BLOCK-007`
- **Maximum 32 databases** per instance → `SPG-BLOCK-008`

### Blocked operations (all roles including `snowflake_admin`)
- `SET ROLE postgres` or any superuser role
- `CREATE ROLE ... SUPERUSER`
- `ALTER SYSTEM` → `SPG-BLOCK-003`
- Changing protected server-level configuration parameters → `SPG-WARN-003`
- Accessing generic file access functions → `SPG-BLOCK-004`
- Directly modifying `pg_catalog.*` → `SPG-BLOCK-005`
- Accessing or altering Snowflake-managed system databases
- Accessing instance filesystem

---

## 5. Extensions

SPG has a curated extension catalog. **Only listed extensions can be installed.**
Custom `.so` extensions are not permitted.

### Full extension catalog (as of 2026-02)

```sql
-- See all available extensions:
SELECT * FROM pg_available_extensions;
```

Key extensions available in SPG:

| Extension | Description | Install |
|-----------|-------------|---------|
| `postgis` | Geospatial types and functions | `CREATE EXTENSION postgis;` |
| `postgis_raster` | PostGIS raster types | `CREATE EXTENSION postgis_raster;` |
| `pg_cron` | Scheduled tasks | `CREATE EXTENSION pg_cron;` |
| `orafce` | Oracle function emulation (NVL, DECODE, etc.) | `CREATE EXTENSION orafce;` |
| `vector` | pgvector for ML workloads | `CREATE EXTENSION vector;` |
| `pgcrypto` | Cryptographic functions | `CREATE EXTENSION pgcrypto;` |
| `pg_trgm` | Fuzzy string matching | `CREATE EXTENSION pg_trgm;` |
| `uuid-ossp` | UUID generation | `CREATE EXTENSION "uuid-ossp";` |
| `pg_uuidv7` | UUID v7 generation | `CREATE EXTENSION pg_uuidv7;` |
| `hstore` | Key-value data type | `CREATE EXTENSION hstore;` |
| `ltree` | Tree-like structure data type | `CREATE EXTENSION ltree;` |
| `citext` | Case-insensitive text | `CREATE EXTENSION citext;` |
| `pg_stat_statements` | Query statistics | `CREATE EXTENSION pg_stat_statements;` |
| `postgres_fdw` | Foreign data wrapper (PostgreSQL only) | `CREATE EXTENSION postgres_fdw;` |
| `pg_lake` | Iceberg/Parquet/ORC storage | See pg_lake docs |
| `amcheck` | Relation integrity check | `CREATE EXTENSION amcheck;` |
| `age` | Graph database (Apache AGE) | `CREATE EXTENSION age;` |
| `http` | HTTP client | `CREATE EXTENSION http;` |
| `pgrouting` | Routing functionality | `CREATE EXTENSION pgrouting;` |

**Assessment code:** `SPG-BLOCK-006` if a CREATE EXTENSION uses an unlisted extension.

### NOT available
- Oracle FDW (`oracle_fdw`)
- MySQL FDW (`mysql_fdw`)
- TDS FDW (`tds_fdw`) for SQL Server
- Any custom C extension
- Custom procedural language extensions (other than PL/pgSQL)

---

## 6. Foreign Data Wrappers

Only `postgres_fdw` (and `file_fdw`) are available.

`oracle_fdw`, `mysql_fdw`, `tds_fdw`, and similar third-party FDWs are **not available**.

**Assessment code:** `SPG-WARN-006` if a non-postgres FDW is detected.

---

## 7. Spatial Types

PostGIS IS available in SPG. However, it must be enabled before deploying spatial objects:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;  -- if raster types used
```

**Assessment code:** `SPG-WARN-001` + `SPG-RESOLVE-001` (auto-generates extension prereq).

---

## 8. Scheduled Jobs

`pg_cron` IS available in SPG. Oracle `DBMS_SCHEDULER` or `pg_agent` jobs must be
migrated to `pg_cron` syntax:

```sql
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Example migration from Oracle DBMS_SCHEDULER:
-- Oracle: DBMS_SCHEDULER.CREATE_JOB(job_name=>'MY_JOB', schedule_type=>'CALENDAR', ...)
-- SPG:
SELECT cron.schedule('0 * * * *', 'SELECT my_procedure()');
```

**Assessment code:** `SPG-WARN-005` + `SPG-RESOLVE-005` (auto-generates `pg_cron` prereq).

---

## 9. Connection and Network

- Connections require a **network policy with `MODE = POSTGRES_INGRESS`** (not standard Snowflake mode)
- Private Link is available on AWS and Azure
- Built-in PgBouncer for connection pooling
- SSL required (`sslmode=require`)

---

## 10. File System Access

No filesystem access is permitted. Specifically blocked:
- `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `pg_stat_file`
- `lo_import`, `lo_export`
- `COPY TO PROGRAM` / `COPY FROM PROGRAM`

**Assessment code:** `SPG-BLOCK-004`.

---

## 11. Oracle Migration Helpers

When migrating from Oracle, the following SPG extensions ease conversion:

- **`orafce`** — provides `NVL`, `DECODE`, `ADD_MONTHS`, `MONTHS_BETWEEN`, `LAST_DAY`,
  `NEXT_DAY`, `TRUNC(date)`, and many other Oracle-compatible functions
- **`postgis`** — for Oracle Spatial / SDO_GEOMETRY migration
- **`pg_cron`** — for Oracle DBMS_SCHEDULER migration

Always run `CREATE EXTENSION IF NOT EXISTS orafce;` before deploying Oracle-converted objects.

---

## 12. Protected Configuration Parameters

These parameters are managed by Snowflake and cannot be changed by customers:
- `max_connections`
- `shared_buffers`
- `effective_cache_size`
- `wal_level`
- `archive_mode`
- `wal_keep_size`
- `max_wal_senders`
- `max_replication_slots`
- `shared_preload_libraries`

**Assessment code:** `SPG-WARN-003` if `SET <param>` is detected.
