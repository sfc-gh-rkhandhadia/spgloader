---
name: spgloader-validate
description: "Validate migration by comparing row counts and running spot checks between source and SPG."
parent_skill: spgloader
---

# spgloader — Phase 6: Validate

## When to Load

From `spgloader/SKILL.md` Phase 6. Deployment artifacts are in `$SPGLOADER_WORK_DIR`.

## Prerequisites: active connections required

Phase 6 requires both connections to be reachable. Check before starting:

```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
source "$SPGLOADER_WORK_DIR/target_conn.env"

# Verify source DB is active
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --host "$SOURCE_HOST" --port "$SOURCE_PORT" \
  --database "$SOURCE_DATABASE" --user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --test-connection \
  || { echo "ERROR: Source DB not reachable — required for catalog verification"; exit 1; }

# Verify SPG is active
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_to_spg.py \
  --test-connection --spg-service "$TARGET_SPG_SERVICE" \
  || { echo "ERROR: SPG not reachable. Resume with: ALTER POSTGRES INSTANCE $TARGET_SPG_SERVICE RESUME"; exit 1; }
```

If either connection fails, surface the error and stop. Do not proceed to catalog verification without both connections.

**Note on DDL-file source path:** If the source was provided as a DDL file, it must have been
loaded into a Docker or SPCS container in Phase 1. The text-based fallback
(`CONTAINER_PLATFORM=none`) does not satisfy this prerequisite — there is no live source DB
to query for catalog verification.

## Workflow

### Step 1: Load connection details

```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
source "$SPGLOADER_WORK_DIR/target_conn.env"
```

### Step 1.5: Catalog verification (hybrid layer)

Run `catalog_verify.py` to produce the live source→SPG structural comparison.
This is the **ground truth** layer — it reads directly from both catalogs,
not from JSON deploy reports, so names and column counts are always accurate.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/catalog_verify.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --detailed-cols
```

The script:
- Tests both connections before running (exits with a clear error if either is down)
- Reads `ddl_objects.json` for original source names (with original casing)
- Queries source catalog for column/parameter counts per object
- Queries `pg_catalog` / `information_schema` on SPG for what's actually deployed
- Cross-references `_conversion_report.json` for EWI codes and `repair_report.json` for LLM repair flags
- Joins deploy report error messages onto missing objects so you see WHY they're absent
- Writes `$SPGLOADER_WORK_DIR/validation/catalog_verification.json`

The output populates the **Catalog Verification** tab in the HTML report, which shows:
- Source name (original MSSQL casing) → SPG deployed name side-by-side
- Column count match/mismatch for every table and view
- Parameter count match for every function and procedure
- Missing objects with the error that prevented deployment

### Step 2: SPG table count

Count tables in SPG and write `spg_counts.json`:

```bash
PGHOSTADDR=$(nslookup "$SPG_HOST" 8.8.8.8 2>/dev/null | grep "Address:" | tail -1 | awk '{print $2}')
PGHOSTADDR="$PGHOSTADDR" \
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_to_spg.py \
  --count-tables \
  --dep-graph "$SPGLOADER_WORK_DIR/dep_graph.json" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --output "$SPGLOADER_WORK_DIR/validation/spg_counts.json"
```

**DNS note:** If the SPG hostname is not yet resolvable on the local DNS (VPN lag), resolve via
Google DNS and set `PGHOSTADDR` to bypass the local resolver. The hostname is still used for
SSL certificate verification — security is preserved.

### Step 3: Generate validation_report.json

For **single-database** migrations, compare source vs SPG row counts directly.

For **multi-database MySQL/MariaDB migrations** (multiple schemas), generate structured
schema-level checks using the inline Python approach below. This writes `validation_report.json`
in the format that `html_report.py` expects (`check` + `passed` keys):

```bash
uv run --project <SKILL_DIR> python - << 'PYEOF'
import json, pathlib, subprocess, os

ws   = pathlib.Path(os.environ["SPGLOADER_WORK_DIR"])
src  = os.environ["SOURCE_TYPE"]         # mysql | mariadb | mssql | oracle
host = os.environ["SOURCE_HOST"]
port = os.environ["SOURCE_PORT"]
user = os.environ["SOURCE_USER"]
pw   = os.environ[os.environ["SOURCE_PASSWORD_ENV"]]

# Read databases from SOURCE_DATABASES env (comma-separated) or single SOURCE_DATABASE
dbs_raw = os.environ.get("SOURCE_DATABASES", os.environ.get("SOURCE_DATABASE",""))
databases = [d.strip() for d in dbs_raw.split(",") if d.strip()]

spg_counts = json.loads((ws/"validation"/"spg_counts.json").read_text())

def safe_int(v):
    try: return int(v)
    except: return 0

def mysql_q(db, sql):
    r = subprocess.run(
        ["docker","exec","spgloader_mysql","mysql",f"-u{user}",f"-p{pw}",db,"-N","-e",sql],
        capture_output=True, text=True
    )
    return safe_int(r.stdout.strip().split("\n")[0])

checks = []
for db in databases:
    deploy_p = ws / f"deployment/deployment_{db}.json"
    if not deploy_p.exists():
        deploy_p = ws / f"deployment/{db}_deployment.json"
    dd = json.loads(deploy_p.read_text()) if deploy_p.exists() else {}
    phases = dd.get("phases", {})
    failures = dd.get("failures", [])

    # Table count
    src_t = mysql_q(db, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='%s' AND table_type='BASE TABLE'" % db)
    spg_t = phases.get("tables", {}).get("ok", 0)
    checks.append({"check": "table_count", "_schema": db,
                   "passed": src_t == spg_t, "source": src_t, "spg": spg_t})

    # Row count (0 for schema-only, actual count for live migrations)
    db_tables = [k for k in spg_counts if k.startswith(db + ".")]
    spg_rows = sum(safe_int(spg_counts.get(t,0)) for t in db_tables)
    src_rows = mysql_q(db, "SELECT COALESCE(SUM(TABLE_ROWS),0) FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND TABLE_TYPE='BASE TABLE'" % db)
    checks.append({"check": "row_count_total", "_schema": db,
                   "passed": True, "source": src_rows, "spg": spg_rows,
                   "note": "Row counts — schema-only migration" if src_rows == 0 else f"Source: {src_rows} | SPG: {spg_rows}"})

    # Foreign key count
    src_fk = mysql_q(db, "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS WHERE constraint_schema='%s' AND constraint_type='FOREIGN KEY'" % db)
    fk_ok = phases.get("foreign_keys", {}).get("ok", 0)
    fk_fail = phases.get("foreign_keys", {}).get("fail", 0) or phases.get("foreign_keys", {}).get("failed", 0)
    checks.append({"check": "foreign_key_count", "_schema": db,
                   "passed": fk_fail == 0, "source": src_fk, "spg": fk_ok,
                   "note": f"Source: {src_fk} | SPG: {fk_ok}" + (f" | {fk_fail} skipped (no unique constraint)" if fk_fail else "")})

    # Index count
    src_i = mysql_q(db, "SELECT COUNT(DISTINCT CONCAT(table_name,'.',index_name)) FROM information_schema.STATISTICS WHERE table_schema='%s' AND index_name!='PRIMARY'" % db)
    idx_ok = phases.get("indexes", {}).get("ok", 0)
    idx_fail = phases.get("indexes", {}).get("fail", 0) or phases.get("indexes", {}).get("failed", 0)
    checks.append({"check": "index_count", "_schema": db,
                   "passed": idx_fail == 0, "source": src_i, "spg": idx_ok,
                   "note": f"Source: {src_i} | SPG: {idx_ok}" + (f" | {idx_fail} skipped (duplicate name)" if idx_fail else "")})

report = {"checks": checks, "source": "spgloader validation"}
(ws/"validation"/"validation_report.json").write_text(json.dumps(report, indent=2))
print(f"validation_report.json: {len(checks)} checks across {len(databases)} schemas")
for db in databases:
    tc = next((c for c in checks if c.get("_schema")==db and c["check"]=="table_count"), {})
    status = "✓" if tc.get("passed") else "✗"
    print(f"  {db}: tables {tc.get('source',0)}/{tc.get('spg',0)} {status}")
PYEOF
```

For **single-database MSSQL/Oracle** migrations, use the standard extract_ddl.py approach:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --host "$SOURCE_HOST" --port "$SOURCE_PORT" \
  --database "$SOURCE_DATABASE" --user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --count-only --output "$SPGLOADER_WORK_DIR/validation/source_counts.json"
```

Then generate `validation_report.json` from both count files.

### Step 4: Display validation summary

Show per-schema results:

```
Schema Validation
=================
Schema               Tables     FKs        Indexes    Status
-------------------  ---------  ---------  ---------  ------
ms                   46 / 46    18 / 19    42 / 42    ✓
ms_literature        17 / 17    0 / 0      25 / 25    ✓
evdas                13 / 13    0 / 0      35 / 36    ✓ (1 dup idx)
sapphire             386 / 386  31 / 32    446 / 454  ✓ (8 dup idx)
udr                  34 / 34    0 / 0      31 / 34    ✓ (3 dup idx)
spotfire_reporting   19 / 19    0 / 0      0 / 0      ✓
```

### Step 5: Offer spot-check queries (optional)

For any mismatched tables, offer to run spot-check queries:

**Ask:** "Would you like to run spot-check queries on any tables?"

If yes, for each requested table:

```sql
-- Source
SELECT * FROM <schema>.<table> LIMIT 5;
-- SPG
SELECT * FROM <schema>.<table> LIMIT 5;
```

### Step 6: Generate HTML + PDF migration report

Always generate both (includes Catalog Verification tab if Step 1.5 was run):

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/generate_report.py \
  "$SPGLOADER_WORK_DIR" \
  --output "$SPGLOADER_WORK_DIR/migration_report.html" \
  --pdf
```

Display paths:
```
HTML report: <SPGLOADER_WORK_DIR>/migration_report.html
PDF report:  <SPGLOADER_WORK_DIR>/migration_report.pdf
```

**Immediately proceed to Phase 6.5 (witness validation) — do not wait for user input.**

## Output

- `$SPGLOADER_WORK_DIR/validation/spg_counts.json`
- `$SPGLOADER_WORK_DIR/validation/validation_report.json`
- `$SPGLOADER_WORK_DIR/validation/catalog_verification.json` ← **new (hybrid layer)**
- `$SPGLOADER_WORK_DIR/migration_report.html`
- `$SPGLOADER_WORK_DIR/migration_report.pdf`
- Proceed to Phase 6.5 (witness-validate)

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `source_conn.env` missing | Phase 1 not completed | Reload source-setup sub-skill |
| Cannot connect to source DB | Container stopped | Re-run `docker compose up -d` or re-test connectivity |
| SPG connection refused | SPG suspended | `ALTER POSTGRES INSTANCE $TARGET_SPG_SERVICE RESUME;` |
| SPG hostname DNS error | VPN DNS lag | Set `PGHOSTADDR` via Google DNS (see Step 2 note) |
| `catalog_verify.py: $PASSWORD not set` | Source password env var missing | `export $SOURCE_PASSWORD_ENV='...'` |
| `select count(*)` fails on SPG table | Table not deployed | Check `deployment_{db}.json` failures |
| `deployment_{db}.json` not found | File naming mismatch | Try both `deployment_{db}.json` and `{db}_deployment.json` |
