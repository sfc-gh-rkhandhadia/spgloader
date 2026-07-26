---
name: spgloader-target-setup
description: "Connect to an existing Snowflake Postgres instance or provision a new one for migration target."
parent_skill: spgloader
---

# spgloader — Phase 2: Target Setup

## When to Load

From `spgloader/SKILL.md` Phase 2. `TARGET_SPG` is already set (existing | new).

The snowflake-postgres skill scripts live at:
`<SNOWFLAKE_POSTGRES_SKILL_DIR>/scripts/pg_connect.py`

If `SNOWFLAKE_POSTGRES_SKILL_DIR` is not already set, resolve it from the skill catalog
(typically `~/sko-coco/snowflake-postgres` for internal installs) or ask the user.

Always expand `~` to the actual home directory in script paths.

## Workflow

### If TARGET_SPG = existing

#### Step 1: List saved SPG connections

```bash
uv run --project <SNOWFLAKE_POSTGRES_SKILL_DIR> \
  python <SNOWFLAKE_POSTGRES_SKILL_DIR>/scripts/pg_connect.py --list
```

Display the list of saved connection names to the user.

#### Step 2: Identify target instance

If the target instance appears in the saved connections, ask the user to confirm which one to use.

If the target instance is NOT in the saved connections:

1. List existing SPG instances in Snowflake:
   ```sql
   SHOW POSTGRES INSTANCES;
   ```
2. Ask the user which instance to use.
3. Describe it to get the host:
   ```sql
   DESCRIBE POSTGRES INSTANCE <instance_name>;
   ```
   ⚠️ Do NOT display raw DESCRIBE output — it contains `access_roles` with credentials.
   Extract only the `host` field.

4. Add the connection:
   ```bash
   uv run --project <SNOWFLAKE_POSTGRES_SKILL_DIR> \
     python <SNOWFLAKE_POSTGRES_SKILL_DIR>/scripts/pg_connect.py \
     --reset \
     --instance-name <instance_name> \
     --host <host_from_describe>
   ```
   This saves the connection and password to `~/.pg_service.conf` and `~/.pgpass`
   without displaying credentials.

#### Step 3: Test connectivity

```bash
psql "service=<instance_name>" -c "SELECT version();" 2>&1
```

If psql is not available:
```bash
uv run --project ~/sko-coco/spgloader \
  python ~/sko-coco/spgloader/scripts/deploy_to_spg.py \
  --test-connection \
  --spg-service <instance_name>
```

#### Step 5: Write target connection env

Capture the active Snowflake connection name:
```bash
SF_CONN=$(snow connection list --format json | python3 -c \
  "import json,sys; conns=json.load(sys.stdin); \
   print(next((c['name'] for c in conns if c.get('is_default')), 'default'))")
```

Write `$SPGLOADER_WORK_DIR/target_conn.env`:
```
TARGET_SPG_SERVICE=<instance_name>
TARGET_SNOWFLAKE_CONNECTION=<sf_conn_name>
TARGET_SNOWFLAKE_ROLE=ACCOUNTADMIN
```

The `TARGET_SNOWFLAKE_CONNECTION` and `TARGET_SNOWFLAKE_ROLE` are required for
teardown so the right account and role are used to drop the SPG instance.

---

### If TARGET_SPG = new

#### ⚠️ MANDATORY STOPPING POINT: SPG instance creation is billable

Before proceeding, confirm with the user:

```
Creating a new Snowflake Postgres instance will incur Snowflake credits.
Do you want to proceed with provisioning a new SPG instance? (yes/no)
```

Use `ask_user_question` for this confirmation. Only continue on explicit "yes".

#### Step 1: Hand off to snowflake-postgres skill

State: "I'll provision a new Snowflake Postgres instance. Loading the provisioning workflow now."

Load `<SNOWFLAKE_POSTGRES_SKILL_DIR>/SKILL.md` and execute the **MANAGE → Create Instance** workflow in full.

The pg_connect.py `--create` script saves the connection automatically after creation.
Do NOT run `--reset` after `--create` — the password is already saved.

#### Step 2: Confirm the instance name

After creation, ask the user to confirm the instance name that was just created
(it appears in the CREATE output).

#### Step 3: Network policy — REQUIRED before connectivity test

SPG instances block all inbound Postgres connections until a network policy with
`POSTGRES_INGRESS` mode is attached.  Do NOT skip this step.

Ask the user (via `ask_user_question`):

```
To allow psql connections, a POSTGRES_INGRESS network policy must be attached.

Do you have an existing Snowflake network policy for Postgres access, or should I create one?
```

**Path A — existing policy (user provides a name):**

Run exactly:
```sql
ALTER POSTGRES INSTANCE <instance_name>
  SET NETWORK_POLICY = '<existing_policy_name>';
```

No network rule or `CREATE NETWORK POLICY` needed.  This is the correct and
simplest path when the user already has a working policy.

**Path B — create a new policy:**

Load `<SNOWFLAKE_POSTGRES_SKILL_DIR>/connect/SKILL.md` and execute the
**Setup Network Policy** workflow in full.  That workflow generates the correct
three-statement sequence:

```sql
CREATE NETWORK RULE ...  TYPE = IPV4  MODE = POSTGRES_INGRESS ...;
CREATE NETWORK POLICY ... ALLOWED_NETWORK_RULE_LIST = (...);
ALTER POSTGRES INSTANCE <instance_name> SET NETWORK_POLICY = '<policy_name>';
```

⚠️ **NEVER generate network policy SQL inline** — always use the connect
sub-skill.  Inline SQL has historically used the wrong `MODE = INGRESS` instead
of `MODE = POSTGRES_INGRESS`, which silently creates an unusable policy.

#### Step 4: Write target connection env

Capture the active Snowflake connection name:
```bash
SF_CONN=$(snow connection list --format json | python3 -c \
  "import json,sys; conns=json.load(sys.stdin); \
   print(next((c['name'] for c in conns if c.get('is_default')), 'default'))")
```

Write `$SPGLOADER_WORK_DIR/target_conn.env`:
```
TARGET_SPG_SERVICE=<instance_name>
TARGET_SNOWFLAKE_CONNECTION=<sf_conn_name>
TARGET_SNOWFLAKE_ROLE=ACCOUNTADMIN
```

The `TARGET_SNOWFLAKE_CONNECTION` and `TARGET_SNOWFLAKE_ROLE` are required for
teardown so the right account and role are used to drop the SPG instance.

---

## Output

- `$SPGLOADER_WORK_DIR/target_conn.env` written
- Connectivity confirmed: "Target: SPG instance <name> — connected"
- Proceed to Phase 3 (ddl-extract)

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `psql: could not connect to server` | Wrong host or SPG not running | Re-run `DESCRIBE POSTGRES INSTANCE` to get current host |
| `psql: connection refused` after instance READY | No network policy attached | Run Step 3 (network policy) — do NOT generate SQL inline; use connect/SKILL.md |
| Network rule creation fails with "invalid mode" | Wrong MODE value | Only `POSTGRES_INGRESS` is valid for SPG; standard `INGRESS` is rejected |
| `FATAL: password authentication failed` | Wrong password saved | Re-run `pg_connect.py --reset` to update credentials |
| `CREATE POSTGRES INSTANCE` fails | Quota exceeded or unsupported region | Check Snowflake account limits; try a different region |
| `~/.pgpass` not picked up by psql | File permissions not 0600 | Run `chmod 0600 ~/.pgpass` |
