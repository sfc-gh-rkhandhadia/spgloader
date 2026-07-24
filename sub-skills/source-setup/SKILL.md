---
name: spgloader-source-setup
description: "Set up the source database environment for migration. Handles Docker deployment and existing environment connectivity testing for MSSQL, MySQL, and Oracle."
parent_skill: spgloader
---

# spgloader — Phase 1: Source Setup

## When to Load

From `spgloader/SKILL.md` Phase 1. `SOURCE_TYPE`, `SOURCE_VERSION`, and `SOURCE_ENV` are already set.

## Workflow

### If SOURCE_ENV = docker

#### Step 1: Pre-flight checks

```bash
docker info > /dev/null 2>&1 && echo "Docker OK" || echo "Docker not running"
```

If Docker is not running, tell the user to start Docker Desktop and stop.

#### Step 2: Oracle-specific login warning

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

#### Step 3: Collect credentials for the source container

Ask the user for an admin password to use for the source database. Use `ask_user_question` with `type: text`:
- MSSQL: "SA password for SQL Server container" — default `"Migration2024!"`
- MySQL: "root password for MySQL container" — default `"migration2024"`
- Oracle: "ORACLE_PWD for Oracle Free container" — default `"Migration2024!"`

Store the password in an environment variable. Never echo it back.

#### Step 4: \u26a0\ufe0f MANDATORY STOPPING POINT: Start the container

**Note for MSSQL:** The compose file mounts `mssql-init.conf` which sets `forceencryption = 0`.
This is required because pgloader's internal TDS client does not negotiate SQL Server 2022 TLS.
The volume mount ensures this config persists across container restarts.

Set the password env var and start the compose service:

```bash
# Export password as env var for docker compose
export MSSQL_SA_PASSWORD="<password>"       # for MSSQL
# OR
export MYSQL_ROOT_PASSWORD="<password>"     # for MySQL
# OR
export ORACLE_PWD="<password>"              # for Oracle

# Start the container
docker compose -f <SKILL_DIR>/references/docker-templates/<db>-compose.yml up -d
```

Replace `<db>` with `mssql`, `mysql`, or `oracle`.

#### Step 5: Wait for health

Poll until healthy or timeout (90s for MSSQL/MySQL, 180s for Oracle):

```bash
timeout=90   # or 180 for oracle
elapsed=0
while [ $elapsed -lt $timeout ]; do
  status=$(docker compose -f <SKILL_DIR>/references/docker-templates/<db>-compose.yml ps --format json \
    2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['Health'] if d else 'unknown')" 2>/dev/null || echo "starting")
  echo "Status: $status (${elapsed}s)"
  [ "$status" = "healthy" ] && break
  sleep 10
  elapsed=$((elapsed + 10))
done
[ "$status" = "healthy" ] && echo "Container ready" || echo "Timeout — check docker logs"
```

#### Step 6: Write source connection env file

Write `$SPGLOADER_WORK_DIR/source_conn.env`:

For MSSQL:
```
SOURCE_TYPE=mssql
SOURCE_HOST=localhost
SOURCE_PORT=1433
SOURCE_DATABASE=master
SOURCE_USER=sa
SOURCE_PASSWORD_ENV=MSSQL_SA_PASSWORD
```

For MySQL:
```
SOURCE_TYPE=mysql
SOURCE_HOST=localhost
SOURCE_PORT=3306
SOURCE_DATABASE=mysql
SOURCE_USER=root
SOURCE_PASSWORD_ENV=MYSQL_ROOT_PASSWORD
```

For Oracle:
```
SOURCE_TYPE=oracle
SOURCE_HOST=localhost
SOURCE_PORT=1521
SOURCE_DATABASE=FREEPDB1
SOURCE_USER=system
SOURCE_PASSWORD_ENV=ORACLE_PWD
```

`SOURCE_PASSWORD_ENV` stores the name of the environment variable holding the password,
never the password itself.

#### Step 7: Test connectivity

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type <SOURCE_TYPE> \
  --host localhost \
  --port <port> \
  --database <db> \
  --user <user> \
  --password-env <PASS_ENV_VAR> \
  --test-connection
```

Show: "Source database connected successfully" or the error with suggested fix.

---

### If SOURCE_ENV = existing

#### Step 1: Collect connection details

Use `ask_user_question` with text inputs:
- Host (default: `localhost`)
- Port (default per type: MSSQL `1433`, MySQL `3306`, Oracle `1521`)
- Database name (default per type: MSSQL `master`, MySQL schema name, Oracle `ORCLPDB1`)
- Username (default per type: MSSQL `sa`, MySQL `root`, Oracle `system`)

Tell the user: "I'll need the password too. Please set it as an environment variable before I connect:
```bash
export SPGLOADER_SOURCE_PASSWORD='your_password_here'
```
Then confirm when ready."

Use `ask_user_question` to confirm the env var is set.

#### Step 2: Test connectivity

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type <SOURCE_TYPE> \
  --host <host> \
  --port <port> \
  --database <database> \
  --user <user> \
  --password-env SPGLOADER_SOURCE_PASSWORD \
  --test-connection
```

#### Step 3: Write source_conn.env

Write `$SPGLOADER_WORK_DIR/source_conn.env` with the confirmed connection details.
Use `SOURCE_PASSWORD_ENV=SPGLOADER_SOURCE_PASSWORD`.

---

## Output

- `$SPGLOADER_WORK_DIR/source_conn.env` written
- Connectivity confirmed: "Source: <TYPE> <VERSION> @ <host>:<port>/<database> — connected"
- Proceed to Phase 2 (target-setup)
