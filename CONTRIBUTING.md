# Contributing to spgloader

## Security & data hygiene rules (enforced automatically)

**These rules are non-negotiable and enforced by pre-commit hooks on every `git commit`.**

### 1. No hardcoded credentials

Never commit passwords, tokens, API keys, or secrets as literals.

```python
# ❌ BLOCKED — will fail pre-commit
password = "MySuperSecret123!"
token = "eyJraWQiOiIxMjQ..."
api_key = "sk-abc123xyz"

# ✅ CORRECT — use environment variables
password = os.environ["MY_PASSWORD"]
token = os.environ.get("API_TOKEN")
```

### 2. No server / host / endpoint information

Hostnames, IP addresses, and cloud service endpoints must not be embedded as string literals.

```python
# ❌ BLOCKED
host = "abc123.us-east-1.aws.postgres.snowflake.app"
ip   = "10.0.1.42"

# ✅ CORRECT
host = os.environ["SPG_HOST"]
# or: read from pg_service.conf / connections.toml
```

### 3. No client or project-specific names

Database names, account names, or any names that identify a customer, project, or person must not appear in source code, YAML configs, README, or documentation.

```python
# ❌ BLOCKED
--database client_production_db
connection_name = "my-account-pat"

# ✅ CORRECT
--database migration_db          # generic placeholder
connection_name = ""             # let user configure in connections.toml
```

### 4. No Snowflake account locators

```yaml
# ❌ BLOCKED
account: ORGNAME-ACCOUNTNAME

# ✅ CORRECT — read from ~/.snowflake/connections.toml
```

### 5. No personal information

Usernames, email addresses, and real names must not appear in any code or config file.

```python
# ❌ BLOCKED
user = "jsmith@company.com"
owner = "John Smith"
```

---

## How the guardrail works

The pre-commit hook runs [`scripts/check_secrets.py`](scripts/check_secrets.py) automatically before every `git commit`. If any violation is found the commit is **blocked** with a clear error message explaining what to fix.

### Set up the hooks (one-time, per developer)

```bash
pip install pre-commit
pre-commit install
```

### Run the scan manually

```bash
# Scan staged files only (same as pre-commit)
pre-commit run check-secrets

# Scan all files in the repo
python scripts/check_secrets.py --all

# Scan specific files
python scripts/check_secrets.py path/to/file.py
```

### What the scanner catches

| Rule | Examples caught |
|---|---|
| `hardcoded_password` | `password="abc123"`, `secret="token-xyz"` |
| `connection_string_with_password` | `mssql://user:pass@host/db` |
| `jwt_token` | `"eyJraWQiO..."` (JWT / Snowflake PAT) |
| `snowflake_account_locator` | `"abc.us-east-1.snowflakecomputing.com"` |
| `spg_hostname` | `"xyz.aws.postgres.snowflake.app"` |
| `ip_address` | `"203.0.113.5"` in a string literal |
| `snowflake_account_name` | `account="ORGNAME-ACCOUNTNAME"` |
| `personal_username` | Known personal usernames |
| `email_address` | `"user@company.com"` as a literal |

### What is NOT flagged (safe patterns)

```python
password = os.environ["MY_PASSWORD"]     # env var reference ✅
host = "${DB_HOST}"                       # shell substitution ✅
conn = f"postgres://{user}:{pw}@{host}"  # template variables ✅
# export PASSWORD="<your-password>"       # doc placeholder ✅
```

### Emergency bypass (use sparingly)

If a violation is a known false positive, bypass the hook **once** with:

```bash
git commit --no-verify -m "your message"
```

Then immediately add the pattern to the `SKIP_PATHS` or refine the regex in `scripts/check_secrets.py`.

---

## General contribution guidelines

### Generic skill — no project specifics

`spgloader` is a generic Cortex Code skill. All examples, documentation, and defaults must use neutral placeholder values:

| Instead of | Use |
|---|---|
| A real database name | `migration_db` |
| A real hostname | `<your-host>` or an env var |
| A real account name | `<your-account>` or blank |
| A real company name | `<your-company>` |
| Actual results from a migration | Generic performance benchmarks |

### Adding new rules

To add a new sensitive-data pattern:

1. Add a tuple to `CREDENTIAL_PATTERNS` or `PERSONAL_INFO_PATTERNS` in `scripts/check_secrets.py`
2. Test it: `python scripts/check_secrets.py --all`
3. Make sure the clean codebase still passes with 0 violations
4. Commit

### Running the full test suite

```bash
pre-commit run --all-files
```
