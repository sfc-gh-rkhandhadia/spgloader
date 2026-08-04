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

### Step 0: Choose Snowflake connection — REQUIRED FIRST

**Do this before any SPG SQL.** The default IDE session may point to a different
Snowflake account (e.g. SNOWHOUSE) that does not have SPG enabled. Using the wrong
connection causes `SHOW POSTGRES INSTANCES` to hang or error silently.

#### Step 0a: Read connections from connections.toml

Read `~/.snowflake/connections.toml` directly — this is instant and works without
network access. `tomllib` is built into Python 3.11+.

```bash
python3 -c "
import tomllib, pathlib, sys

p = pathlib.Path.home() / '.snowflake' / 'connections.toml'
if not p.exists():
    print('NOT_FOUND'); sys.exit(0)

data = tomllib.loads(p.read_text())
default_name = data.get('default_connection_name', '')

for name, cfg in data.items():
    if not isinstance(cfg, dict):
        continue
    marker = ' [default]' if name == default_name else ''
    acct   = cfg.get('account', '')
    role   = cfg.get('role', '')
    user   = cfg.get('user', '')
    # Never print password / token / private_key fields
    print(f'{name}{marker} | account={acct} | role={role} | user={user}')
"
```

Parse this output to build the list of connection names with their account/role.
Identify the default connection (marked `[default]`) to use as `defaultValue`.

#### Step 0b: Ask the user which connection to use

Use `ask_user_question` with `type: options`, one option per connection read from
the TOML. Each option's `label` is the connection name and `description` is
`account=<acct> | role=<role>`.  Pre-select (`defaultAnswer`) whichever entry
is marked `[default]`, or the first connection whose account/role suggests
ACCOUNTADMIN on an SE/trial account if the default is obviously wrong (e.g. SNOWHOUSE).

```
header:       "Snowflake connection"
question:     "Which Snowflake connection should be used for SPG provisioning?
               (Must point to the account where the SPG instance will live.)"
options:      <one per connection from Step 0a>
defaultAnswer: <default connection name or best candidate>
```

Store the answer as `SF_SNOWFLAKE_CONNECTION`.

#### Step 0c: Verify the connection is reachable

```bash
snow connection test -c "$SF_SNOWFLAKE_CONNECTION" 2>&1
```

Show the user the Account, Role, and Status from the output. Only continue if Status = OK.

#### Step 0d: Test SPG feature availability on this connection

```bash
snow sql -c "$SF_SNOWFLAKE_CONNECTION" -q "SHOW POSTGRES INSTANCES;" --format json 2>&1 | head -5
```

- If it **errors** with "unsupported feature" or "syntax error near POSTGRES" → SPG is not
  enabled on this account. Tell the user to verify regional availability and pick a different
  connection. Do NOT proceed.
- If it **succeeds** (even with zero rows) → feature is available. Continue.

⚠️ **Never use `snowflake_sql_execute` for SPG SQL.** That tool uses the default IDE session
which may point to a different account. All SPG commands (`SHOW/DESCRIBE/CREATE/ALTER/DROP
POSTGRES INSTANCE`, network rules, network policies) must go through:
```bash
snow sql -c "$SF_SNOWFLAKE_CONNECTION" -q "<SQL>"
```

---

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

Use `SF_SNOWFLAKE_CONNECTION` set in Step 0 (do not re-discover the default).

Write `$SPGLOADER_WORK_DIR/target_conn.env`:
```
TARGET_SPG_SERVICE=<instance_name>
TARGET_SNOWFLAKE_CONNECTION=$SF_SNOWFLAKE_CONNECTION
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

#### Step 1: Collect instance name and compute size

Ask the user two questions in one `ask_user_question` call:

1. **Instance name** (text, default: `<source_db>_pg`)
2. **Compute size** (options — use EXACT names below, these are the only valid values):

| Label | Compute Family | Use for |
|---|---|---|
| `STANDARD_L` | Standard L | Most migrations (default) |
| `STANDARD_XL` | Standard XL | Larger databases or parallel workloads |
| `STANDARD_2XL` | Standard 2XL | Heavy workloads |

Use these EXACT option labels in ask_user_question — do NOT invent or abbreviate (e.g. STANDARD_1 is NOT valid):

```
ask_user_question:
  questions:
    - header: "Instance name"
      question: "Name for the new SPG instance?"
      type: text
      defaultValue: "<source_db>_pg"
    - header: "Compute size"
      question: "What compute size for the SPG instance?"
      type: options
      defaultAnswer: "STANDARD_L"
      options:
        - label: STANDARD_L
          description: "Standard L — recommended for most migrations"
        - label: STANDARD_XL
          description: "Standard XL — larger databases, more parallelism"
        - label: STANDARD_2XL
          description: "Standard 2XL — heavy workloads"
```

#### Step 2: Create the instance using pg_connect.py

Run exactly this command with the user's answers substituted:

```bash
PG_SKILL_DIR=$(find ~/.snowflake/cortex/skills -name "pg_connect.py" -path "*/snowflake-postgres/*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
PG_SKILL_PARENT=$(dirname "$PG_SKILL_DIR")

uv run --project "$PG_SKILL_PARENT" python "$PG_SKILL_DIR/pg_connect.py" \
  --snowflake-connection "$SF_SNOWFLAKE_CONNECTION" \
  --create \
  --instance-name "<INSTANCE_NAME>" \
  --compute-pool <COMPUTE_FAMILY> \
  --storage 50 \
  --postgres-version 18 2>&1
```

The script saves the connection to `~/.pg_service.conf` and `~/.pgpass` automatically.
Do NOT run `--reset` after `--create` — the password is already saved.

#### Step 2: Wait for instance to be READY

The instance starts in CREATING → STARTING → FINALIZING → READY state.
Run `--ensure-ready` to wait (polls every 10s, up to 5 minutes):

```bash
uv run --project "$PG_SKILL_PARENT" python "$PG_SKILL_DIR/pg_connect.py" \
  --snowflake-connection "$SF_SNOWFLAKE_CONNECTION" \
  --instance-name "<INSTANCE_NAME>" \
  --ensure-ready 2>&1
```

If DNS has not propagated yet, the script will print `dns_error` and advise waiting 30-60s then retrying. Retry once before continuing.

After READY, add `hostaddr` to bypass local DNS (the SPG hostname may not resolve on the local machine's DNS):

```bash
# Get the IP
SPG_HOST=$(grep "^host=" ~/.pg_service.conf | grep -A5 "\[<INSTANCE_NAME>\]" | head -1 | cut -d= -f2)
SPG_IP=$(nslookup "$SPG_HOST" 8.8.8.8 2>/dev/null | grep "Address:" | tail -1 | awk '{print $2}')
echo "SPG IP: $SPG_IP"

# Add hostaddr to the pg_service.conf entry using Python (sed is unreliable cross-platform)
python3 - << PYEOF
import re, pathlib
conf = pathlib.Path("~/.pg_service.conf").expanduser()
text = conf.read_text()
# Insert hostaddr= after the host= line for this instance
section = "<INSTANCE_NAME>"
lines = text.split("\n")
result = []
in_section = False
hostaddr_added = False
for line in lines:
    result.append(line)
    if line.strip() == f"[{section}]":
        in_section = True; hostaddr_added = False
    elif in_section and not hostaddr_added and line.strip().startswith("host="):
        result.append(f"hostaddr=$SPG_IP")
        hostaddr_added = True
    elif line.strip().startswith("[") and line.strip() != f"[{section}]":
        in_section = False
conf.write_text("\n".join(result))
print("hostaddr added")
PYEOF
```

#### Step 3: Network policy — REQUIRED before connectivity test

SPG blocks all Postgres connections until a `POSTGRES_INGRESS` network policy is attached.

Ask via `ask_user_question`:

```yaml
header: "Network policy"
question: "A POSTGRES_INGRESS network policy must be attached to allow psql connections.
           Do you have an existing Snowflake network policy for Postgres access?"
options:
  - label: "Yes — use existing policy"
    description: "Attach an existing network policy (you'll provide the name)"
  - label: "No — create a new policy"
    description: "I'll create a network rule for your current IP and attach it"
```

**Path A — use existing policy:**

Ask for the policy name (text input), then run:
```bash
snow sql -c "$SF_SNOWFLAKE_CONNECTION" -q "
USE ROLE ACCOUNTADMIN;
ALTER POSTGRES INSTANCE <INSTANCE_NAME>
  SET NETWORK_POLICY = '<EXISTING_POLICY_NAME>';"
```

**Path B — create a new policy:**

Get the user's current public IP first:
```bash
curl -s https://checkip.amazonaws.com 2>/dev/null || curl -s https://api.ipify.org
```

Then run these three statements using `snow sql` — do NOT use `snowflake_sql_execute` (wrong account):
```bash
snow sql -c "$SF_SNOWFLAKE_CONNECTION" -q "
USE ROLE ACCOUNTADMIN;

CREATE NETWORK RULE IF NOT EXISTS spgloader_migration_rule
  TYPE = IPV4
  MODE = POSTGRES_INGRESS
  VALUE_LIST = ('<YOUR_IP>/32');

CREATE NETWORK POLICY IF NOT EXISTS spgloader_migration_policy
  ALLOWED_NETWORK_RULE_LIST = (spgloader_migration_rule);

ALTER POSTGRES INSTANCE <INSTANCE_NAME>
  SET NETWORK_POLICY = 'spgloader_migration_policy';"
```

⚠️ The MODE must be exactly `POSTGRES_INGRESS` — `INGRESS` is a different, incompatible mode that will silently create a non-working policy. Never substitute or abbreviate this value.

#### Step 4: Write target connection env

Use `SF_SNOWFLAKE_CONNECTION` set in Step 0 (do not re-discover the default).

Write `$SPGLOADER_WORK_DIR/target_conn.env`:
```
TARGET_SPG_SERVICE=<instance_name>
TARGET_SNOWFLAKE_CONNECTION=$SF_SNOWFLAKE_CONNECTION
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
