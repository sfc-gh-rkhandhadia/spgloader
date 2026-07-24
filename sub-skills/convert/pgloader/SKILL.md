---
name: spgloader-convert-pgloader
description: "Generate pgloader configuration and run tables+data migration from MSSQL or MySQL to Snowflake Postgres."
parent_skill: spgloader-convert
---

# spgloader — Phase 4a: pgloader Migration

## When to Load

From `spgloader/convert/SKILL.md` when `pgloader_eligible` is not empty and
`SOURCE_TYPE` is `mssql` or `mysql`.

## Prerequisites

- pgloader must be installed: `which pgloader` or `pgloader --version`
- Source database is reachable (connectivity confirmed in Phase 1)
- Target SPG connection is saved in `~/.pg_service.conf` (confirmed in Phase 2)
- Source database password is in env var `$SOURCE_PASSWORD_ENV`
- **SPG TLS certificate is trusted** in the Homebrew OpenSSL CA bundle. If not done yet:
  ```bash
  uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/trust_spg_cert.py \
    --spg-service $TARGET_SPG_SERVICE
  ```
- **MSSQL source (Docker):** The compose file mounts `mssql-init.conf` with
  `forceencryption = 0`, which allows pgloader's TDS client to connect without TLS.
  For existing MSSQL servers: ensure `forceencryption = 0` is set in `mssql.conf`
  or that the server accepts unencrypted connections on the migration port.

## Workflow

### Step 1: Get target SPG DSN

Load `$SPGLOADER_WORK_DIR/target_conn.env` to get `TARGET_SPG_SERVICE`.

Build the target DSN from the PostgreSQL service file:
```bash
# Read host from ~/.pg_service.conf for the service name
python3 -c "
import configparser, pathlib, sys
cfg = configparser.ConfigParser()
cfg.read(pathlib.Path.home() / '.pg_service.conf')
svc = '$TARGET_SPG_SERVICE'
if svc in cfg:
    s = cfg[svc]
    print(f\"postgresql://{s.get('user','snowflake_admin')}:PASSWORD@{s.get('host')}:{s.get('port','5432')}/{s.get('dbname','postgres')}?sslmode={s.get('sslmode','require')}\")
else:
    print('SERVICE_NOT_FOUND', file=sys.stderr)
    sys.exit(1)
"
```

The actual password is injected at runtime via the pgloader config — never hardcoded.

### Step 2: Generate pgloader configuration

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/gen_pgloader_config.py \
  --source-type "$SOURCE_TYPE" \
  --source-host "$SOURCE_HOST" \
  --source-port "$SOURCE_PORT" \
  --source-db "$SOURCE_DATABASE" \
  --source-user "$SOURCE_USER" \
  --source-password-env "$SOURCE_PASSWORD_ENV" \
  --target-service "$TARGET_SPG_SERVICE" \
  --output "$SPGLOADER_WORK_DIR/migration.load"
```

### Step 3: Dry run (validation)

Always run a dry run first to catch connection and config errors:

```bash
pgloader --dry-run "$SPGLOADER_WORK_DIR/migration.load" 2>&1
```

If the dry run fails, show the error and stop. Common issues:
- **"Connection refused"** — source DB not running or port blocked
- **"password authentication failed"** — wrong password env var
- **"SSL SYSCALL error"** — add `sslmode=disable` to source DSN for local Docker

Show the user the dry-run output and ask for confirmation to proceed with the real migration.

### Step 4: Run pgloader

After dry-run success and user confirmation:

```bash
pgloader "$SPGLOADER_WORK_DIR/migration.load" 2>&1 | tee "$SPGLOADER_WORK_DIR/pgloader.log"
```

### Step 5: Parse and display results

Parse the pgloader summary table from `pgloader.log`:

```
pgloader Migration Results
==========================
Table                    Rows      Errors   Time
-----------------------  --------  -------  ------
dbo.customers            10,000    0        1.2s
dbo.orders               250,000   0        8.4s

Total:                   260,000   0
```

If there are errors, show the affected tables and the first few error lines from the log.

## Notes on pgloader DSN format

For MSSQL:
```
mssql://user:password@host:1433/database
```
For MySQL:
```
mysql://user:password@host:3306/database
```

pgloader uses `LOAD DATABASE FROM <source> INTO <target>` syntax.
SSL can be disabled per-connection in the load file for local Docker testing.
