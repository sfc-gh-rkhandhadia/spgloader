---
name: spgloader-convert-pgloader
description: "Generate pgloader configuration and run tables+data migration from MSSQL or MySQL to Snowflake Postgres via Docker."
parent_skill: spgloader
---

# spgloader — Phase 4a: pgloader Migration (Docker)

## When to Load

From `spgloader/convert/SKILL.md` when `pgloader_eligible` is not empty and
`SOURCE_TYPE` is `mssql` or `mysql`.

pgloader always runs inside Docker (`spgloader-pgloader:local`) rather than the host
binary.  This ensures consistent FreeTDS/TLS behaviour across all host platforms
and avoids the SQL Server 2022 TLS negotiation issue in the macOS Homebrew pgloader.

## Prerequisites

- Docker is running (confirmed in Phase 1)
- Source container (`spgloader_mssql` / `spgloader_mysql`) is healthy (Phase 1)
- Target SPG connection is saved in `~/.pg_service.conf` (Phase 2)
- Source DB password is in env var `$SOURCE_PASSWORD_ENV`
- **No separate cert trust step needed** — the `--no-ssl-cert-verification` flag is
  passed inside Docker automatically by `run_pgloader_docker.py`

---

## Step 0 (DDL-file sources only): Load DDL into source container

Skip this step if `SOURCE_ENV = existing` (live source database already has data).

If the DDL was extracted from a **file** (Phase 3 Option B or C), the Docker MSSQL/MySQL
container is still empty.  pgloader needs a live catalog to read from, so load the DDL
into the container first:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --ddl-file     "/path/to/source_schema.sql" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

This creates a dedicated database (`migration_db`) in the container, copies and runs
the DDL file inside it, and updates `source_conn.env` with `SOURCE_DATABASE=migration_db`
so that subsequent steps use the loaded database.

Expected output:
```
Loading DDL into mssql container...
  Container : spgloader_mssql
  Database  : migration_db
  DDL file  : /path/to/source_schema.sql
  Creating database 'migration_db' (if not exists)...
  Copying DDL into container → /tmp/spgloader_ddl_source_schema.sql
  Loading DDL into [migration_db]...
Source DB ready: mssql @ spgloader_mssql/migration_db
  Updated source_conn.env: SOURCE_DATABASE=migration_db
```

---

## Step 1: Dry run (connection validation)

Always run a dry run first to confirm both source and target are reachable from Docker:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/run_pgloader_docker.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --source-type "$SOURCE_TYPE" \
  --dry-run
```

What happens:
1. Reads `source_conn.env` for connection details
2. Generates `migration.load` using the **container hostname** (`spgloader_mssql`,
   `spgloader_mysql`) instead of `localhost` — Docker DNS resolves these on the shared network
3. Detects the source container's Docker network
4. Builds `spgloader-pgloader:local` image if not already built (first run ~30s)
5. Runs pgloader with `--no-ssl-cert-verification` and `--dry-run`
6. Displays connection result

**Expected dry run output:**
```
[1/5] Reading source connection...
  Source: mssql @ spgloader_mssql:1433/migration_db
  Target: SPG service 'pg_spgloader_migration'
[2/5] Detecting source container network...
  Network: docker-templates_default
[3/5] Generating pgloader config...
  Config written: /path/to/.spgloader/.../migration.load
[4/5] Checking pgloader Docker image...
  Image built: spgloader-pgloader:local
[5/5] Running pgloader via Docker (dry run)...

Dry run passed — both connections are valid.
Run without --dry-run to start the migration.
```

If the dry run fails:
- **"Connection refused" on source** — container not on correct network; verify
  `docker inspect spgloader_mssql` shows the expected network
- **"password authentication failed"** — verify `$SOURCE_PASSWORD_ENV` is exported
- **"could not connect to server" on SPG** — check SPG network policy allows the
  Docker host's outbound IP

---

## Step 2: Run pgloader

After dry run passes, ask the user for confirmation to start the full migration:

```
pgloader dry run passed.
I will now migrate tables from <SOURCE_TYPE> @ <source_db> → SPG '<TARGET_SPG_SERVICE>'.
This will CREATE tables and LOAD DATA in SPG.

Proceed? (yes/no)
```

Use `ask_user_question` for this confirmation.

After confirmation:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/run_pgloader_docker.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --source-type "$SOURCE_TYPE"
```

Output is teed to `$SPGLOADER_WORK_DIR/pgloader.log`.

---

## Step 3: Display results

`run_pgloader_docker.py` parses and prints the pgloader summary table automatically.
Relay it to the user:

```
pgloader Migration Results
==========================
Table                    Rows      Errors   Time
-----------------------  --------  -------  ------
dbo.customers            10,000    0        1.2s
dbo.orders               250,000   0        8.4s

Total:                   260,000   0
```

If there are table errors, show the affected table names and error snippets from
`pgloader.log`.

---

## Notes

- pgloader reads schema metadata from the **live source catalog** (not DDL text),
  so MSSQL-specific artifacts like `IDENTITY`, `NOT FOR REPLICATION`, temporal columns,
  and filegroup specs are automatically handled — no manual rule fixes needed for tables.
- The `spgloader-pgloader:local` image is built once and reused for all subsequent runs.
- To rebuild the image (e.g. after updating `pgloader.Dockerfile`):
  ```bash
  docker compose -f <SKILL_DIR>/references/docker-templates/pgloader-compose.yml build --no-cache
  ```
- Oracle sources do not go through pgloader — all Oracle objects use the LLM/script
  conversion path (wave_1_tables through wave_4_procedures_triggers).
