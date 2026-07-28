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
| `spcs`     | [SPCS section](#spcs) — deploy Snowflake-hosted container |
| `existing` | [Existing section](#existing) — connect to running DB |
| `none`     | Skip this phase — no container; text-based fallback in Phase 3 |

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
a Snowflake-managed service and is accessible via a service endpoint.

### Step 1: ⚠️ MANDATORY STOPPING POINT — SPCS compute pool creation is billable

Confirm with the user before creating any SPCS resources.

### Step 2: Prerequisites

```bash
# Verify snow CLI and Snowflake connection
snow --version
snow connection test
```

Ensure the active Snowflake role has `CREATE COMPUTE POOL`, `CREATE SERVICE`, and
`CREATE IMAGE REPOSITORY` privileges.

### Step 3: Create image repository and push source DB image

```bash
REPO_URL=$(snow sql -q "CREATE IMAGE REPOSITORY IF NOT EXISTS spgloader_source_repo" \
  --format=json | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['repository_url'])")

# Build and push the source image
docker buildx build --platform linux/amd64 \
  -t "$REPO_URL/source-db:latest" \
  -f <SKILL_DIR>/references/docker-templates/<db>-compose.yml \
  --push .
```

### Step 4: Create compute pool and SPCS service

```sql
-- Compute pool (XS is sufficient for source DB during migration)
CREATE COMPUTE POOL IF NOT EXISTS spgloader_source_pool
  MIN_NODES = 1 MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_XS;

-- Service spec written to stage
CREATE STAGE IF NOT EXISTS spgloader_specs;
```

Write a service spec to `/tmp/source_db_spec.yaml` then deploy:

```bash
cat > /tmp/source_db_spec.yaml << 'EOF'
spec:
  containers:
  - name: source-db
    image: <REPO_URL>/source-db:latest
    env:
      MSSQL_SA_PASSWORD: $MSSQL_SA_PASSWORD     # or MySQL/Oracle equivalents
      ACCEPT_EULA: "Y"
    ports:
    - containerPort: 1433     # adjust per source type
  endpoints:
  - name: db
    port: 1433
    protocol: TCP
EOF

snow sql -q "PUT file:///tmp/source_db_spec.yaml @spgloader_specs AUTO_COMPRESS=FALSE OVERWRITE=TRUE"

snow sql -q "
CREATE SERVICE IF NOT EXISTS spgloader_source_db
  IN COMPUTE POOL spgloader_source_pool
  FROM @spgloader_specs
  SPEC = 'source_db_spec.yaml'
"
```

### Step 5: Wait for service to be READY

```bash
while true; do
  svc_status=$(snow sql -q "SHOW SERVICES LIKE 'SPGLOADER_SOURCE_DB'" --format=json \
    | python3 -c "import sys,json; rows=json.load(sys.stdin); print(rows[0]['status'] if rows else 'UNKNOWN')")
  echo "Service status: $svc_status"
  [ "$svc_status" = "RUNNING" ] && break
  sleep 15
done
```

### Step 6: Retrieve service endpoint

```bash
SERVICE_HOST=$(snow sql \
  -q "SELECT system\$get_service_dns_name('spgloader_source_db')" \
  --format=json | python3 -c "import sys,json; print(json.load(sys.stdin)[0][0])")
echo "Source DB endpoint: $SERVICE_HOST"
```

### Step 7: Load DDL file into the SPCS source DB (if SOURCE_INPUT = file)

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/load_source_ddl.py \
  --source-type  "$SOURCE_TYPE" \
  --ddl-file     "/path/to/schema.sql" \
  --database     "migration_db" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --work-dir     "$SPGLOADER_WORK_DIR" \
  --host         "$SERVICE_HOST"
```

### Step 8: Write source_conn.env

Write `$SPGLOADER_WORK_DIR/source_conn.env` using the service endpoint as host:

```
SOURCE_TYPE=<source_type>
SOURCE_HOST=<SERVICE_HOST>
SOURCE_PORT=<port>
SOURCE_DATABASE=migration_db
SOURCE_USER=<user>
SOURCE_PASSWORD_ENV=<PASS_ENV_VAR>
SPCS_SERVICE=spgloader_source_db
SPCS_POOL=spgloader_source_pool
```

The `SPCS_SERVICE` and `SPCS_POOL` entries are used by Phase 6 (validate) to clean up
SPCS resources after migration completes.

### Step 9: Test connectivity

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type <SOURCE_TYPE> --host "$SERVICE_HOST" --port <port> \
  --database migration_db --user <user> --password-env <PASS_ENV_VAR> \
  --test-connection
```

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
