---
name: spgloader-deploy
description: "Deploy converted DDL objects to Snowflake Postgres in topological dependency order."
parent_skill: spgloader
---

# spgloader — Phase 5: Deploy

## When to Load

From `spgloader/SKILL.md` Phase 5. `dep_graph.json`, `converted_objects/`, and
`conversion_manifest.json` are in `$SPGLOADER_WORK_DIR`.

## Workflow

### Step 1: Build deployment plan

Read `dep_graph.json` to get ordered objects. Cross-reference with
`conversion_manifest.json` to identify what to deploy:

- Tables migrated by pgloader: already in SPG — mark as "pgloader (data already loaded)"
- LLM-converted objects: deploy from `converted_objects/*.sql` in dep order

Show the deployment plan to the user:

```
Deployment Plan
===============
Order  Type        FQN                       Source
-----  ----------  ------------------------  ----------------------
1      table       dbo.customers             pgloader (data loaded)
2      table       dbo.orders                pgloader (data loaded)
3      view        dbo.customer_orders       converted_objects/
4      procedure   dbo.get_customer_orders   converted_objects/
5      trigger     dbo.orders_audit_trig     converted_objects/

Total: 5 objects (2 via pgloader, 3 DDL deploy)
```

### Step 2: MANDATORY STOPPING POINT

Display:
```
I will deploy the DDL objects above to SPG instance '<TARGET_SPG_SERVICE>'.

This operation:
- Creates/replaces views, functions, procedures, and triggers in the target DB
- pgloader-migrated tables are NOT re-deployed (data already loaded)
- Any existing objects with the same name will be replaced

Proceed? (yes/no)
```

Wait for explicit "yes" before continuing.

### Step 3: Run deployment

Load `target_conn.env` to get `TARGET_SPG_SERVICE`.

The deploy scripts are selected based on what was converted:

**Tables** (rule-based DDL conversion):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_tables_spg.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```

**Views** (fix + deploy with rule-based corrections):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_views.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"

uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_views.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```

**Functions / Procedures** (fix + deploy with PL/pgSQL corrections):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_functions.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"

uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_functions.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```

**Legacy (combined deploy)** — use when only `deploy_to_spg.py` output is available:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_to_spg.py \
  --dep-graph "$SPGLOADER_WORK_DIR/dep_graph.json" \
  --converted-dir "$SPGLOADER_WORK_DIR/converted_objects" \
  --conversion-manifest "$SPGLOADER_WORK_DIR/conversion_manifest.json" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --output "$SPGLOADER_WORK_DIR/deployment_summary.json"
```

### Step 4: Display results

Parse `deployment_summary.json` and show a summary table:

```
Deployment Results
==================
Status     Count
---------  -----
SUCCESS    N
FAILED     N
SKIPPED    N

Failures:
  - dbo.some_procedure: ERROR: syntax error at or near "BEGIN"
```

If any failures occurred, list the object name and the PostgreSQL error message.
Offer to attempt re-conversion for failed objects.

**Common errors and remedies:**

| Error pattern | Likely cause | Action |
|---|---|---|
| `syntax error at or near ...` | Incomplete T-SQL→PL/pgSQL conversion | Re-run `fix_functions.py`; check EWI annotations |
| `relation "X" does not exist` | Missing table or schema prefix | Add the table to `schema_prefix.tables` in `view-fixes.yaml`, re-run `fix_views.py` |
| `type "X" does not exist` | UDTT or custom type not deployed | Create the type manually or use a substitute (array, JSONB) |
| `function X() does not exist` | UDF not deployed yet | Deploy dependencies first; check topological order |
| `column "X" does not exist` | Column name case mismatch or missing column | Check source table schema; add to schema fix rules |

## Output

- `$SPGLOADER_WORK_DIR/deployment_summary.json`
- Proceed to Phase 6 (validate)
