---
name: spgloader-validate
description: "Validate migration by comparing row counts and running spot checks between source and SPG."
parent_skill: spgloader
---

# spgloader — Phase 6: Validate

## When to Load

From `spgloader/SKILL.md` Phase 6. `deployment_summary.json` is in `$SPGLOADER_WORK_DIR`.

## Workflow

### Step 1: Load connection details

```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
source "$SPGLOADER_WORK_DIR/target_conn.env"
```

### Step 2: Table row count comparison

For each table in `ddl_objects.json` (type = table), compare row counts:

**Source count:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --host "$SOURCE_HOST" --port "$SOURCE_PORT" \
  --database "$SOURCE_DATABASE" --user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --count-only --output "$SPGLOADER_WORK_DIR/source_counts.json"
```

**SPG count** (via psql or deploy_to_spg.py):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_to_spg.py \
  --count-tables \
  --dep-graph "$SPGLOADER_WORK_DIR/dep_graph.json" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --output "$SPGLOADER_WORK_DIR/spg_counts.json"
```

### Step 3: Display comparison

Show a table for all migrated tables:

```
Row Count Validation
====================
Table                  Source      SPG         Match
---------------------  ----------  ----------  ------
dbo.customers          10,000      10,000      OK
dbo.orders             250,000     250,000     OK
dbo.products           500         498         MISMATCH (-2)

Tables matched:   2 / 3
Tables mismatched: 1
```

Flag any mismatches with `(+N)` or `(-N)`.

### Step 4: Spot-check queries

For up to 3 tables (prioritize mismatched ones), offer spot-check queries.

Show first 5 rows from both source and SPG side by side if the user requests it:

**Ask:** "Would you like to run spot-check queries on any tables? (yes/no, or specify table names)"

If yes, for each requested table:

```sql
-- Source (via extract_ddl.py --query)
SELECT * FROM <table> LIMIT 5;

-- SPG
SELECT * FROM <table> LIMIT 5;
```

Display both result sets side by side for comparison.

### Step 5: Write validation report

Write `$SPGLOADER_WORK_DIR/validation_report.json`:

```json
{
  "tables_checked": N,
  "tables_matched": N,
  "tables_mismatched": N,
  "details": [
    {
      "fqn": "dbo.customers",
      "source_count": 10000,
      "spg_count": 10000,
      "match": true,
      "diff": 0
    }
  ]
}
```

## Output

- `$SPGLOADER_WORK_DIR/validation_report.json`
- Validation summary displayed in chat
- Return to main SKILL.md to display final migration summary

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `source_conn.env` missing | Phase 1 not completed | Reload source-setup sub-skill |
| Cannot connect to source DB | Container stopped or credentials changed | Re-run `docker compose up -d` or re-test source connectivity |
| `deployment_summary.json` missing | Phase 5 not completed | Run deploy phase first |
| `select count(*)` fails on SPG table | Table was not deployed (FAILED in deploy) | Check `deployment_summary.json` for failures; re-run deploy |
| Row count mismatch | Partial pgloader run or concurrent writes during migration | Re-run pgloader for the affected table; check pgloader logs |
