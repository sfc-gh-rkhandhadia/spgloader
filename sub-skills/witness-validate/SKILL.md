---
name: spgloader-witness-validate
description: "Witness validation and parity testing: seed synthetic data into source DB (Docker/SPCS only), confirm views/procs return rows on MSSQL, then compare results against SPG."
parent_skill: spgloader
---

# spgloader — Phase 6.5 & 6.6: Witness Validation + Parity Testing

## When to Load

From `spgloader/SKILL.md` after Phase 6 (schema validation) completes.
`source_conn.env` and `target_conn.env` are in `$SPGLOADER_WORK_DIR`.

## Purpose

| Phase | What | Where |
|---|---|---|
| **6.5** | Seed synthetic data (Docker/SPCS only) + confirm views/procs return rows | MSSQL source |
| **6.6** | Same queries on SPG, diff results, produce sign-off report | SPG target |

---

## Step 1 — Ask user

Load connection info first:

```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
# Provides: SOURCE_TYPE, SOURCE_HOST, SOURCE_PORT, SOURCE_DATABASE,
#           SOURCE_PASSWORD_ENV, SOURCE_ENV (docker | spcs | existing | none)

source "$SPGLOADER_WORK_DIR/target_conn.env"
# Provides: TARGET_SPG_SERVICE, TARGET_SNOWFLAKE_CONNECTION, TARGET_SNOWFLAKE_ROLE
```

Read SPG connection from `.pg_service.conf` for the target instance (`$TARGET_SPG_SERVICE`):
```bash
SPG_HOST=$(grep -A10 "\[$TARGET_SPG_SERVICE\]" ~/.pg_service.conf | grep "^host=" | cut -d= -f2)
SPG_USER=$(grep -A10 "\[$TARGET_SPG_SERVICE\]" ~/.pg_service.conf | grep "^user=" | cut -d= -f2)
SPG_DATABASE=$(grep -A10 "\[$TARGET_SPG_SERVICE\]" ~/.pg_service.conf | grep "^dbname=" | cut -d= -f2)
SPG_PASSWORD=$(awk "/\[$SPG_HOST\]/{found=1} found && /password/{print \$NF; exit}" ~/.pgpass 2>/dev/null || \
              grep "$SPG_HOST:*:$SPG_DATABASE:$SPG_USER:" ~/.pgpass | cut -d: -f5)
```

Then ask:

```
ask_user_question:
  header: "Witness Validation"
  question: "Phase 6 schema validation is complete. Do you want to run witness
             validation (confirm views/procs return rows) and parity testing?"
  options:
    - label: "Yes — full (seed + validate + parity)"
      description: "Generate 3-row synthetic dataset, confirm MSSQL views/procs
                    return rows, then run same queries on SPG and diff results.
                    [Only available when source is Docker or SPCS]"
      # Only show this option if SOURCE_ENV == "docker" or SOURCE_ENV == "spcs"

    - label: "Yes — validate + parity only (no seeding)"
      description: "Validate views/procs against existing data in source DB,
                    then parity-test on SPG. Safe for customer's own instance."

    - label: "No — skip"
      description: "Skip to final report"
```

**If SOURCE_ENV is `existing` or `none`:** only show "validate + parity only" and "skip".
**If SOURCE_ENV is `docker` or `spcs`:** show all three options.

If user chooses "No — skip": jump to Step 8 (update report, done).

Store choice as:
- `DO_SEED` = true | false
- `DO_WITNESS` = true | false

---

## Step 2 — Parse DDL → object_inventory.json

Parse spgloader's `source/` directory (written by Phase 3) into the inventory format
that all witness scripts consume.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/parse_ddl.py \
  --ddl-dir  "$SPGLOADER_WORK_DIR/source" \
  --output   "$SPGLOADER_WORK_DIR/witness/object_inventory.json"
```

**Note:** If `source/` is empty or contains no `.sql` files, fall back to the individual
DDL files inside `conversion/postgres/`:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/parse_ddl.py \
  --ddl-dir  "$SPGLOADER_WORK_DIR" \
  --output   "$SPGLOADER_WORK_DIR/witness/object_inventory.json"
```

Show progress: `Parsed N objects (T tables, V views, P procedures, F functions)`

---

## Step 3 — Discover SPG constraints → spg_column_constraints.json

Reads live CHECK constraints from the already-deployed SPG instance.
These feed into the dep graph so seed data respects SPG enum/range constraints.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/discover_spg_constraints.py \
  --host     "$SPG_HOST" \
  --user     "$SPG_USER" \
  --password "$SPG_PASSWORD" \
  --database "$SPG_DATABASE" \
  --output   "$SPGLOADER_WORK_DIR/witness/spg_column_constraints.json"
```

If connection fails: print a warning and continue — constraints file will be empty,
seeding will fall back to type-based generation only.

---

## Step 4 — Build dependency graph → dep_graph.json

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/build_dep_graph.py \
  --inventory       "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --constraints-file "$SPGLOADER_WORK_DIR/witness/spg_column_constraints.json" \
  --output          "$SPGLOADER_WORK_DIR/witness/dep_graph.json"
```

---

## Step 5 — Build MSSQL deploy report bridge

spg_migration's `seed_data.py` and `validate_chains.py` expect a `deploy_report.json`
that lists which objects were successfully deployed. Build this from spgloader's
existing artifacts:

```python
# Bridge: build mssql_deploy_report.json from spgloader workspace
import json, pathlib

ws = pathlib.Path(SPGLOADER_WORK_DIR)

# Tables — all tables in deployment_summary.json phases.tables.ok_list
dep_sum = json.loads((ws / "deployment/deployment_summary.json").read_text())
tables_ok = dep_sum.get("phases", {}).get("tables", {}).get("ok_list", [])
# Fallback: build from object_inventory
if not tables_ok:
    inv = json.loads((ws / "witness/object_inventory.json").read_text())
    tables_ok = [o["fqn"] for o in inv["objects"] if o["type"] == "TABLE"]

# Views / functions / procedures from conversion reports
view_report = json.loads((ws / "conversion/deploy_report.json").read_text()) if (ws / "conversion/deploy_report.json").exists() else {}
fn_report   = json.loads((ws / "conversion/functions_deploy_report.json").read_text()) if (ws / "conversion/functions_deploy_report.json").exists() else {}
proc_report = json.loads((ws / "conversion/procedures_deploy_report.json").read_text()) if (ws / "conversion/procedures_deploy_report.json").exists() else {}

succeeded = (tables_ok
             + view_report.get("succeeded", [])
             + fn_report.get("succeeded", [])
             + proc_report.get("succeeded", []))

bridge = {
    "succeeded": succeeded,
    "failed": {},
    "database": SOURCE_DATABASE,
    "server": SOURCE_HOST,
    "summary": {"deployed": len(succeeded), "failed": 0},
    "wave_results": []
}
(ws / "witness/mssql_deploy_report.json").write_text(json.dumps(bridge, indent=2))
```

Run this as a quick inline Python script:

```bash
uv run --project <SKILL_DIR> python - <<'PYEOF'
import json, pathlib, os, sys

ws  = pathlib.Path(os.environ["SPGLOADER_WORK_DIR"])
src_host = os.environ.get("SOURCE_HOST","localhost")
src_db   = os.environ.get("SOURCE_DATABASE","migration_db")

dep_sum_path = ws / "deployment" / "deployment_summary.json"
dep_sum = json.loads(dep_sum_path.read_text()) if dep_sum_path.exists() else {}
tables_ok = dep_sum.get("phases",{}).get("tables",{}).get("ok_list",[])

if not tables_ok:
    inv_path = ws / "witness" / "object_inventory.json"
    inv = json.loads(inv_path.read_text()) if inv_path.exists() else {"objects":[]}
    tables_ok = [o["fqn"] for o in inv.get("objects",[]) if o.get("type") == "TABLE"]

def load_report(p):
    return json.loads(p.read_text()) if p.exists() else {}

view_ok = load_report(ws/"conversion"/"deploy_report.json").get("succeeded",[])
fn_ok   = load_report(ws/"conversion"/"functions_deploy_report.json").get("succeeded",[])
proc_ok = load_report(ws/"conversion"/"procedures_deploy_report.json").get("succeeded",[])

succeeded = tables_ok + view_ok + fn_ok + proc_ok
bridge = {"succeeded": succeeded, "failed": {}, "database": src_db,
          "server": src_host, "summary": {"deployed": len(succeeded), "failed": 0},
          "wave_results": []}

out = ws / "witness" / "mssql_deploy_report.json"
out.write_text(json.dumps(bridge, indent=2))
print(f"Bridge deploy report: {len(succeeded)} objects → {out}")
PYEOF
```

---

## Step 6 — Seed synthetic data (Docker/SPCS only)

**Skip this step if `DO_SEED == false` or `SOURCE_ENV == "existing"` or `SOURCE_ENV == "none"`.**

Read the source password:
```bash
MSSQL_SA_PASSWORD="${!SOURCE_PASSWORD_ENV}"
# e.g. if SOURCE_PASSWORD_ENV=MSSQL_SA_PASSWORD, then: ${MSSQL_SA_PASSWORD}
```

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/seed_data.py \
  --inventory     "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --dep-graph     "$SPGLOADER_WORK_DIR/witness/dep_graph.json" \
  --deploy-report "$SPGLOADER_WORK_DIR/witness/mssql_deploy_report.json" \
  --server        "$SOURCE_HOST" \
  --port          "${SOURCE_PORT:-1433}" \
  --user          sa \
  --password      "$MSSQL_SA_PASSWORD" \
  --database      "$SOURCE_DATABASE" \
  --row-volume    3 \
  --output        "$SPGLOADER_WORK_DIR/witness/seed_report.json"
```

Show summary:
```
Seeding complete:
  Tables with rows: 1,482
  Tables zero rows: 0
  Tables skipped:   0
```

If seeding was skipped, write a minimal stub:
```bash
uv run --project <SKILL_DIR> python - <<'PYEOF'
import json, pathlib, os
ws = pathlib.Path(os.environ["SPGLOADER_WORK_DIR"])
stub = {"seed_results": {}, "row_volume": 0,
        "summary": {"tables_seeded": 0, "tables_zero_rows": 0, "tables_skipped": 0},
        "note": "Seeding skipped — existing customer instance"}
(ws / "witness" / "seed_report.json").write_text(json.dumps(stub, indent=2))
PYEOF
```

---

## Step 7 — validate_chains (source-side: confirm views/procs return rows)

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/validate_chains.py \
  --inventory     "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --seed-report   "$SPGLOADER_WORK_DIR/witness/seed_report.json" \
  --deploy-report "$SPGLOADER_WORK_DIR/witness/mssql_deploy_report.json" \
  --dep-graph     "$SPGLOADER_WORK_DIR/witness/dep_graph.json" \
  --server        "$SOURCE_HOST" \
  --port          "${SOURCE_PORT:-1433}" \
  --user          sa \
  --password      "$MSSQL_SA_PASSWORD" \
  --database      "$SOURCE_DATABASE" \
  --output        "$SPGLOADER_WORK_DIR/witness/validation_chains.json"
```

Show summary table:

```
Source-Side Witness Validation
==============================
✅ Validated:           48 views, 54 procedures, 20 functions
⚠️  Partially validated: 3 (TVP params, DML-only procs)
❌ Failed:              2 (UDTT-dependent procs)
⏭️  Skipped:            9 (legacy/excluded)
```

For any ❌ FAILED objects, show the error message inline.

---

## Step 7.5 — Legacy group filter for equivalence test

Before running parity, ask the user whether to **include or exclude** each legacy group
that was **migrated** (disposition = "migrate" in `deprecated_review.json`).

Legacy framework objects (e.g. `aspnet_*`, `sp_fivetran_*`) may fail the structural
check even if correctly deployed, so the user should decide whether they want them tested.

**Read migrated groups:**
```python
import json, pathlib
review = json.loads(pathlib.Path(f"{WORK_DIR}/deprecated/deprecated_review.json").read_text())
migrated_groups = {
    key: grp for key, grp in review.get("groups", {}).items()
    if grp.get("disposition") == "migrate"
}
```

**If migrated_groups is empty — skip this step entirely (no prompt needed).**

**For each migrated group, call `ask_user_question`:**
```
header:   "<group_label>"
question: "The '<group_label>' group (<N> objects) was migrated to SPG.
           Include these in the equivalence test, or skip them?"
options:
  - label: "Include — test them in equivalence test"
    description: "Objects will be compared MSSQL vs SPG. Any missing or
                  structurally different objects appear as FAIL."
  - label: "Skip — exclude from equivalence test"
    description: "Objects are deployed but won't be tested. They won't
                  appear as missing or failed in the report."
```

**Write decisions to `$SPGLOADER_WORK_DIR/parity/equivalence_filter.json`:**
```json
{
  "excluded_groups": ["aspnet_membership"],
  "excluded_fqns": ["dbo.aspnet_Applications", "dbo.aspnet_Membership"]
}
```

If all groups are included, write `{ "excluded_groups": [], "excluded_fqns": [] }`.

**Pass `--exclude-fqns-file` to Step 8:**
```bash
EQUIV_FILTER="$SPGLOADER_WORK_DIR/parity/equivalence_filter.json"
# Add --exclude-fqns-file "$EQUIV_FILTER" to the full_validation.py call below
```

---

## Step 8 — Parity testing (SPG-side)

Runs the same queries that succeeded on MSSQL side against SPG and diffs the results.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parity/full_validation.py \
  --inventory    "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --mssql-server "$SOURCE_HOST" \
  --mssql-port   "${SOURCE_PORT:-1433}" \
  --mssql-user   sa \
  --mssql-password "$MSSQL_SA_PASSWORD" \
  --mssql-db     "$SOURCE_DATABASE" \
  --spg-host     "$SPG_HOST" \
  --spg-user     "$SPG_USER" \
  --spg-password "$SPG_PASSWORD" \
  --spg-db       "$SPG_DATABASE" \
  --output-dir   "$SPGLOADER_WORK_DIR/parity/" \
  --exclude-fqns-file "$SPGLOADER_WORK_DIR/parity/equivalence_filter.json"
```

Show summary:
```
Parity Testing
==============
✅ Matched:   49 objects (identical result sets on MSSQL + SPG)
⚠️  Partial:   4 objects (column-level differences)
❌ Mismatched: 1 object (different row counts)
```

---

## Step 9 — Generate sign-off reports

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parity/generate_validation_markdown.py \
  --output "$SPGLOADER_WORK_DIR/parity/parity_report.md"
```

Then ask:
```
ask_user_question:
  header: "PowerPoint Report"
  question: "Do you want to generate a Snowflake-branded PowerPoint sign-off deck?"
  options:
    - label: "Yes"
      description: "Generate .pptx sign-off report (requires python-pptx)"
    - label: "No"
      description: "Skip PowerPoint, HTML/PDF report already includes results"
```

If yes:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parity/generate_migration_report.py \
  --output "$SPGLOADER_WORK_DIR/parity/migration_signoff.pptx"
```

---

## Step 10 — Regenerate HTML report

After witness/parity data is written, regenerate the migration report so the new
Witness tab picks up the results:

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/generate_report.py \
  "$SPGLOADER_WORK_DIR" \
  --output "$SPGLOADER_WORK_DIR/migration_report.html"
```

Display final paths:
```
Witness validation:  <SPGLOADER_WORK_DIR>/witness/validation_chains.json
Parity report:       <SPGLOADER_WORK_DIR>/parity/parity_report.md
Migration report:    <SPGLOADER_WORK_DIR>/migration_report.html
```

---

## Output Artifacts

```
$SPGLOADER_WORK_DIR/
├── witness/
│   ├── object_inventory.json       ← parse_ddl output
│   ├── dep_graph.json              ← dependency graph + SPG constraints
│   ├── spg_column_constraints.json ← SPG CHECK constraints
│   ├── mssql_deploy_report.json    ← bridge file from spgloader artifacts
│   ├── seed_report.json            ← seeding results (stub if skipped)
│   └── validation_chains.json      ← view/proc/fn confirmation results
└── parity/
    ├── parity_report.md            ← sign-off report
    └── migration_signoff.pptx      ← PowerPoint (if requested)
```

---

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `parse_ddl.py: no .sql files found` | `source/` dir empty | Use `conversion/postgres/` fallback |
| `pymssql: Login failed` | Container stopped or wrong password | Re-check `SOURCE_PASSWORD_ENV`; run `docker ps` |
| `seed_data.py: FK violation` | Parent table missing | Check dep_graph waves; rerun with `--row-volume 1` |
| `validate_chains.py: 0 rows — seed data does not satisfy join` | Complex view joins | Mark as `partial` — manually verify |
| `discover_spg_constraints.py: connection refused` | SPG instance suspended | Resume SPG first: `ALTER POSTGRES INSTANCE $TARGET_SPG_SERVICE RESUME` |
| `full_validation.py: module not found` | Missing parity script | Check `scripts/parity/` directory exists |
| `SOURCE_ENV = existing` + seed attempted | Bug in routing | Never seed existing instances — `DO_SEED` must be `false` when `SOURCE_ENV=existing` |
