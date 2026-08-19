---
name: spgloader-source-setup
description: "Set up the source database environment for migration. Handles Docker deployment, SPCS deployment, and existing environment connectivity testing for MSSQL, MySQL, MariaDB, and Oracle."
parent_skill: spgloader
---

# spgloader — Phase 1: Source Setup

## When to Load

From `spgloader/SKILL.md` Phase 1.
`SOURCE_TYPE`, `SOURCE_VERSION`, `SOURCE_ENV`, and (if applicable) `CONTAINER_PLATFORM` are already set.

---

## Routing

| SOURCE_ENV | Workflow |
|------------|----------|
| `docker`   | [Docker section](#docker) — deploy local container |
| `spcs`     | **⛔ DISABLED this release** — respond: "SPCS is planned for the next release. Use Docker instead." |
| `existing` | [Existing section](#existing) — connect to running DB |
| `none`     | Skip this phase — no container; text-based fallback in Phase 3 |

> **Guard:** If `SOURCE_TYPE` is `oracle`, stop immediately and respond
> with the appropriate disabled message from the Feature Flags table in the parent SKILL.md.

---

## Docker {#docker}

### Step 1: Pre-flight checks

```bash
docker info > /dev/null 2>&1 && echo "Docker OK" || echo "Docker not running"
```

If Docker is not running, tell the user to start Docker Desktop and stop.

### Step 2: Oracle-specific login warning

If `SOURCE_TYPE = oracle`, display this warning before pulling:

```
Oracle Free requires authentication with Oracle Container Registry.
Before proceeding, run in your terminal:

  docker login container-registry.oracle.com

Use your Oracle account credentials. Register free at https://container-registry.oracle.com
if you don't have one.

Once logged in, confirm to continue.
```

Use `ask_user_question` to confirm they have logged in before proceeding.

### Step 3: Collect credentials for the source container

Ask the user for an admin password. Use `ask_user_question` with `type: text`:
- MSSQL:   "SA password for SQL Server container"
- MySQL:   "root password for MySQL container"
- MariaDB: "root password for MariaDB container"
- Oracle:  "ORACLE_PWD for Oracle Free container"

Store the password in an environment variable. Never echo it back.

### Step 4: ⚠️ MANDATORY STOPPING POINT: Start the container

Set the password env var and start the compose service:

```bash
export MSSQL_SA_PASSWORD="<password>"       # MSSQL
export MYSQL_ROOT_PASSWORD="<password>"     # MySQL / MariaDB
export ORACLE_PWD="<password>"              # Oracle

docker compose -f <SKILL_DIR>/references/docker-templates/<db>-compose.yml up -d
```

Replace `<db>` with `mssql`, `mysql`, `mariadb`, or `oracle`.

### Step 5: Wait for health

Poll until healthy or timeout (90s for MSSQL/MySQL/MariaDB, 180s for Oracle):

```bash
timeout_s=90  # or 180 for oracle
elapsed=0
container_health="unknown"
while [ $elapsed -lt $timeout_s ]; do
  container_health=$(docker compose -f <SKILL_DIR>/references/docker-templates/<db>-compose.yml \
    ps --format json 2>/dev/null | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d[0]['Health'] if d else 'unknown')" \
    2>/dev/null || echo "starting")
  echo "Health: $container_health (${elapsed}s)"
  [ "$container_health" = "healthy" ] && break
  sleep 10
  elapsed=$((elapsed + 10))
done
[ "$container_health" = "healthy" ] && echo "Container ready" || echo "Timeout — check docker logs"
```

### Step 6: Write source_conn.env and test

Write `$SPGLOADER_WORK_DIR/source_conn.env` per the templates below, then test connectivity:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type <SOURCE_TYPE> --host localhost --port <port> \
  --database <db> --user <user> --password-env <PASS_ENV_VAR> \
  --test-connection
```

**source_conn.env templates:**

MSSQL:
```
SOURCE_TYPE=mssql
SOURCE_HOST=localhost
SOURCE_PORT=1433
SOURCE_DATABASE=master
SOURCE_USER=sa
SOURCE_PASSWORD_ENV=MSSQL_SA_PASSWORD
```

### Step 6b: Load DDL into the Docker container (if SOURCE_INPUT = file)

If the user provided a DDL file or directory (not a live source), load it now.

**Single combined `.sql` file:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --ddl-file     "/path/to/schema.sql" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

The script automatically preserves `SET QUOTED_IDENTIFIER ON` and other session
SET statements from the preamble so that DDL files using `"double-quoted"` table
identifiers (common in Northwind, AdventureWorks, etc.) load correctly.

**If CSV data files are in the same directory**, add `--csv-dir` to also load data:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --ddl-file     "/path/to/schema.sql" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR" \
  --csv-dir      "/path/to/csv_data_files"
```

The `--csv-dir` option:
- Copies all `.csv` files into the Docker container at `/tmp/csvdata/`
- Maps filenames to tables (e.g. `Address.csv` → `[Person].[Address]`)
- Runs BULK INSERT with KEEPIDENTITY, tab-delimited, constraints disabled
- Re-enables constraints after all tables are loaded

**SSMS "Scripts and Tables" export directory** (one `.sql` file per object, common
UTF-16 LE encoding — use `--ddl-dir` and the script handles everything automatically):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --ddl-dir      "/path/to/ssms_export_directory" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

After loading, verify objects were created:
```bash
docker exec spgloader_mssql /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -No -d migration_db \
  -Q "SELECT 'Tables' t, COUNT(*) FROM sys.tables UNION ALL \
      SELECT 'Views', COUNT(*) FROM sys.views UNION ALL \
      SELECT 'Functions', COUNT(*) FROM sys.objects WHERE type IN ('FN','TF','IF') \
      ORDER BY t"
```

Update `source_conn.env` SOURCE_DATABASE to the loaded database name.

MySQL / MariaDB:
```
SOURCE_TYPE=mysql        # or mariadb
SOURCE_HOST=localhost
SOURCE_PORT=3306
SOURCE_DATABASE=mysql
SOURCE_USER=root
SOURCE_PASSWORD_ENV=MYSQL_ROOT_PASSWORD
```

Oracle:
```
SOURCE_TYPE=oracle
SOURCE_HOST=localhost
SOURCE_PORT=1521
SOURCE_DATABASE=FREEPDB1
SOURCE_USER=system
SOURCE_PASSWORD_ENV=ORACLE_PWD
```

---

## SPCS (Snowpark Container Services) {#spcs}

Use SPCS when Docker is not available on the local machine. The source DB runs as
a Snowflake-managed service and is accessible via a service endpoint DNS name.

**Data copy uses `copy_source_data.py` (pymssql over TCP) — pgloader is not used.**

### Step 1: ⚠️ MANDATORY STOPPING POINT — SPCS compute pool creation is billable

Confirm with the user before creating any SPCS resources. The compute pool incurs
credit charges while running. Remind them to tear it down after migration completes.

### Step 2: Prerequisites

```bash
snow --version          # must be installed
snow connection test    # must succeed
docker --version        # needed to push the source DB image
```

Ensure the active Snowflake role has `CREATE COMPUTE POOL`, `CREATE SERVICE`, and
`CREATE IMAGE REPOSITORY` privileges.

### Step 3: Collect source DB password

Ask the user for the source DB admin password and store it as an environment variable:

```bash
export MSSQL_SA_PASSWORD="<password>"      # MSSQL
export MYSQL_ROOT_PASSWORD="<password>"    # MySQL
```

### Step 4: Deploy source DB on SPCS

Run `setup_spcs_source.py` — this handles the image repo, docker pull+push, compute
pool, service spec, service deployment, RUNNING wait, DNS retrieval, and writes
`source_conn.env` in one step:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/setup_spcs_source.py \
  --source-type  "$SOURCE_TYPE" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

Optional flags:
- `--pool-name NAME` — custom compute pool name (default: `spgloader_source_pool`)
- `--service-name NAME` — custom service name (default: `spgloader_source_db`)
- `--instance-family CPU_X64_S` — larger instance if needed (default: `CPU_X64_XS`)
- `--connection my_conn` — Snowflake connection name
- `--skip-push` — skip docker pull+push if image is already in the repo

The script prints the service DNS name and writes `source_conn.env` including
`SOURCE_HOST`, `SPCS_SERVICE`, and `SPCS_POOL` keys.

Read the DNS name from the written file:
```bash
SERVICE_HOST=$(grep '^SOURCE_HOST=' "$SPGLOADER_WORK_DIR/source_conn.env" | cut -d= -f2)
echo "Source DB endpoint: $SERVICE_HOST"
```

### Step 5: Load DDL file into the SPCS source DB (if SOURCE_INPUT = file)

Pass `--host` to route through the TCP/pymssql path instead of Docker exec:

**Single combined `.sql` file:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --host         "$SERVICE_HOST" \
  --ddl-file     "/path/to/schema.sql" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

**SSMS export directory** (use `--ddl-dir`; encoding and ordering handled automatically):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --host         "$SERVICE_HOST" \
  --ddl-dir      "/path/to/ssms_export_directory" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR"
```

### Step 6: Test connectivity

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --test-connection
```

(`extract_ddl.py` reads `source_conn.env` automatically when `--work-dir` is given.)

### Teardown (after migration completes)

Run this at the end of Phase 6 to drop the SPCS service and compute pool:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/teardown_spcs_source.py \
  --work-dir "$SPGLOADER_WORK_DIR"
```

This reads `SPCS_SERVICE` and `SPCS_POOL` from `source_conn.env` and prompts for
confirmation before dropping. Use `--yes` to skip the prompt in automated flows.

---

## Existing environment {#existing}

### Step 1: Collect connection details

Use `ask_user_question` with text inputs:
- Host (default: `localhost`)
- Port (default per type: MSSQL `1433`, MySQL/MariaDB `3306`, Oracle `1521`)
- Database name (default per type: MSSQL `master`, MySQL schema name, Oracle `ORCLPDB1`)
- Username (default per type: MSSQL `sa`, MySQL/MariaDB `root`, Oracle `system`)

Tell the user: "I'll need the password too. Please set it as an environment variable:
```bash
export SPGLOADER_SOURCE_PASSWORD='your_password_here'
```
Then confirm when ready."

Use `ask_user_question` to confirm the env var is set.

### Step 2: Test connectivity and write source_conn.env

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type <SOURCE_TYPE> --host <host> --port <port> \
  --database <database> --user <user> \
  --password-env SPGLOADER_SOURCE_PASSWORD \
  --test-connection
```

Write `$SPGLOADER_WORK_DIR/source_conn.env` with confirmed details.
Use `SOURCE_PASSWORD_ENV=SPGLOADER_SOURCE_PASSWORD`.

---

## Output

- `$SPGLOADER_WORK_DIR/source_conn.env` written
- Connectivity confirmed: `"Source: <TYPE> <VERSION> @ <host>:<port>/<database> — connected"`
- If SPCS: service endpoint recorded; cleanup info in `SPCS_SERVICE` / `SPCS_POOL`
- Proceed to Phase 2 (target-setup)
