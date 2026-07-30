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

## Step 1 — Choose seeding mode

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

**Witness validation and parity testing run automatically as the next step in the migration flow.**
Present the seeding mode prompt — do NOT add any "are you done?" gate between Phase 6 and this phase.

```
ask_user_question:
  header: "Witness Validation"
  question: "Proceeding to witness validation (Phase 6.5) and parity testing (Phase 6.6).

             How should the witness phase run?"
  defaultAnswer: <see routing below>
  options:
    - label: "Full — seed synthetic data + validate + parity"        [Docker/SPCS only]
      description: "Generate 3-row synthetic dataset, confirm source views/procs
                    return rows, then compare same queries on SPG and diff results.
                    Recommended for Docker and SPCS source environments."
      # Show only if SOURCE_ENV == "docker" or SOURCE_ENV == "spcs"

    - label: "Validate + parity only (no seeding)"
      description: "Run chain validation against existing data in the source DB,
                    then parity-test on SPG. Correct for customer instances and
                    existing environments where seeding is not safe."

    - label: "Skip — go straight to final report"
      description: "Skip witness validation and parity testing. The Witness and
                    Equivalence Test tabs will show 'Not Run' in the migration
                    report. Use this only if you intend to run them separately."
```

**Routing rules (automatic, no user input needed):**
- `SOURCE_ENV = docker` or `SOURCE_ENV = spcs` → offer all three options; default to "Full"
- `SOURCE_ENV = existing` or `SOURCE_ENV = none` → offer "Validate + parity only" and "Skip"; default to "Validate + parity only"

If user chooses **Skip**: jump directly to Step 10 (regenerate HTML report + display final summary).

Store choice as:
- `DO_SEED` = true | false

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
SOURCE_PASSWORD="${!SOURCE_PASSWORD_ENV}"
# Dereferences whatever env var name SOURCE_PASSWORD_ENV points to (works for both MSSQL and MySQL)
```

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/witness/seed_data.py \
  --source-type   "${SOURCE_TYPE:-mssql}" \
  --inventory     "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --dep-graph     "$SPGLOADER_WORK_DIR/witness/dep_graph.json" \
  --deploy-report "$SPGLOADER_WORK_DIR/witness/mssql_deploy_report.json" \
  --server        "$SOURCE_HOST" \
  --port          "$SOURCE_PORT" \
  --user          "${SOURCE_USER:-sa}" \
  --password      "$SOURCE_PASSWORD" \
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
  --source-type   "${SOURCE_TYPE:-mssql}" \
  --inventory     "$SPGLOADER_WORK_DIR/witness/object_inventory.json" \
  --seed-report   "$SPGLOADER_WORK_DIR/witness/seed_report.json" \
  --deploy-report "$SPGLOADER_WORK_DIR/witness/mssql_deploy_report.json" \
  --dep-graph     "$SPGLOADER_WORK_DIR/witness/dep_graph.json" \
  --server        "$SOURCE_HOST" \
  --port          "$SOURCE_PORT" \
  --user          "${SOURCE_USER:-sa}" \
  --password      "$SOURCE_PASSWORD" \
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

**Multi-database MySQL note:** For MySQL migrations with multiple source databases, run
`validate_chains.py` once per database, writing to `chains_report_{db}.json`, then merge
all results into the canonical `validation_chains.json` the report expects:

```bash
uv run --project <SKILL_DIR> python - << 'PYEOF'
import json, os, pathlib

ws = pathlib.Path(os.environ["SPGLOADER_WORK_DIR"])
merged = {"validation_results": {}, "summary": {}}
for f in sorted((ws / "witness").glob("chains_report_*.json")):
    d = json.loads(f.read_text())
    merged["validation_results"].update(d.get("validation_results", {}))
    for k, v in d.get("summary", {}).items():
        merged["summary"][k] = merged["summary"].get(k, 0) + int(v or 0)
out = ws / "witness" / "validation_chains.json"
out.write_text(json.dumps(merged, indent=2))
n = len(list((ws / "witness").glob("chains_report_*.json")))
print(f"Merged {n} db chain reports → {out}")
PYEOF
```

For single-database migrations, `validate_chains.py` writes directly to
`validation_chains.json` — no merge needed.

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

Runs the same queries that succeeded on source side against SPG and diffs the results.

**Route by SOURCE_TYPE:**

**MSSQL / T-SQL:**
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

**MySQL / MariaDB:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parity/mysql_structural_parity.py \
  --source-type  "${SOURCE_TYPE:-mysql}" \
  --source-host  "$SOURCE_HOST" \
  --source-port  "${SOURCE_PORT:-3306}" \
  --source-user  "${SOURCE_USER:-root}" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --databases    "$SOURCE_DATABASE" \
  --spg-service  "$TARGET_SPG_SERVICE" \
  --output       "$SPGLOADER_WORK_DIR/parity/parity_results.json"
```

For **multi-database MySQL** (multiple schemas in one instance), pass all database names
comma-separated: `--databases "evdas,ms,ms_literature,sapphire,spotfire_reporting,udr"`.

Show summary:
```
Parity Testing
==============
✅ Matched:   49 objects (identical result sets on MSSQL + SPG)
⚠️  Partial:   4 objects (column-level differences)
❌ Mismatched: 1 object (different row counts)
```

---

## Step 8.5 — ⚠️ MANDATORY STOPPING POINT: Execution parity

**Never auto-continue to Step 9 without presenting this choice to the user.**
This prompt must always be shown immediately after Step 8 (structural parity) completes.

The structural parity test (`full_validation.py`) checks parameter signatures, column
names, and row counts. It does **not** execute functions/procedures or compare result
set contents.

Execution parity goes further — it actually runs every object on both sides and hashes
the result sets. This is the difference between "the function exists with the right signature"
and "the function returns the same data on both MSSQL and SPG."

Ask the user:

```
ask_user_question:
  header: "Execution Parity"
  question: "Structural equivalence test is complete (signatures + row counts checked).

             Execution parity testing goes further: it executes every procedure and
             function on both the source DB and SPG with identical parameters, compares
             result sets row-by-row via data hashing, writes verdicts to SPG audit tables,
             and generates a Snowflake-branded PowerPoint sign-off deck.

             Do you want to run execution parity testing?"
  defaultAnswer: "Yes — run execution parity testing"
  options:
    - label: "Yes — run execution parity testing"
      description: "Executes all procedures/functions on both sides, compares outputs
                    with data hashing, writes verdicts to SPG audit tables, and
                    generates a Snowflake-branded PowerPoint sign-off.
                    Works with MSSQL, MySQL, MariaDB, and Oracle sources."
    - label: "No — structural check is sufficient"
      description: "Skip behavioral execution. The HTML report and parity_report.md
                    are the sign-off artifacts. You can always run execution parity
                    separately by re-loading the execution-parity sub-skill."
```

**If user chooses Yes:**

Load `<SKILL_DIR>/sub-skills/execution-parity/SKILL.md` and execute its full workflow.

The sub-skill reads `SOURCE_TYPE` from the env (already set via `source_conn.env`)
and handles all source DB catalog differences internally via `source_adapter.py`.

All connection env vars are already set from Step 1 of this sub-skill — pass them through.

**If user chooses No:**

Continue to Step 9 (markdown/parity report + HTML regeneration).

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
