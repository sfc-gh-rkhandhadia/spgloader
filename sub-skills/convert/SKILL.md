---
name: spgloader-convert
description: "Phase 4: Classify extracted DDL objects and route to pgloader (tables+data) or LLM-based conversion with SPG EWI annotations and wave-ordered output."
parent_skill: spgloader
---

# spgloader — Phase 4: Conversion

## When to Load

From `spgloader/SKILL.md` Phase 4.

**GUARDRAIL CHECK — run first:**

Read `$SPGLOADER_WORK_DIR/assessment/assessment_summary.json`:
```python
import json
summary = json.load(open(f"{SPGLOADER_WORK_DIR}/assessment/assessment_summary.json"))
if summary["is_blocked"]:
    print("ERROR: Assessment phase has unresolved BLOCK findings. Cannot convert.")
    print("Blocked by:", summary["block_codes"])
    # STOP — do not proceed
```

If `is_blocked = true`: refuse to continue. Tell the user to resolve BLOCK findings first.

## Object Classification

Read `ddl_objects.json`. Classify each object:

```
pgloader_eligible:   tables from MSSQL or MySQL source
llm_required:        all other objects (views, procedures, functions, triggers)
                     + all Oracle objects regardless of type
```

For reference, the `assessment_summary.json` already has `pgloader_eligible` and `llm_required` lists.

Show classification summary before proceeding.

## Phase 4A — pgloader (MSSQL/MySQL tables + data)

If `pgloader_eligible` is not empty, load `sub-skills/convert/pgloader/SKILL.md` and execute it.

pgloader handles: table schema, data loading, indexes, foreign keys, type casting.
Output: data loaded directly into SPG. No SQL files generated for tables.

## Phase 4B — LLM Conversion (views, procedures, functions, triggers + all Oracle)

### Script-based conversion (MSSQL/MySQL)

The conversion pipeline uses rule-based scripts rather than raw LLM conversion
for MSSQL/MySQL sources. Run in this order:

**1. Convert all non-table objects:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/convert_objects.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --ddl-objects "$SPGLOADER_WORK_DIR/ddl_objects.json"
```
Output: `conversion/postgres/wave_2_views/`, `wave_3_functions/`, `wave_4_procedures_triggers/`

**2. Apply view fixes (schema prefix, T-SQL syntax corrections, PIVOT→CTE):**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_views.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"
```
Output: `conversion/postgres/wave_2_views_fixed/`

**3. Apply function/procedure fixes (PL/pgSQL corrections, END IF/LOOP, SELECT INTO):**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_functions.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"
```
Output: `conversion/postgres/wave_3_functions_fixed/`

### LLM conversion (Oracle, or complex objects needing manual guidance)

For Oracle sources or objects that the rule-based scripts cannot handle, load the relevant type-mapping reference:
- `references/type-mappings/mssql-to-pg.md` for MSSQL
- `references/type-mappings/mysql-to-pg.md` for MySQL
- `references/type-mappings/oracle-to-pg.md` for Oracle

Load `references/ewi-codes.md` for EWI annotation codes.

### Output Directory (wave-ordered)

Write converted files to wave-ordered directories under `conversion/postgres/`:

| Wave | Directory | Object types |
|------|-----------|--------------|
| 1 | `wave_1_tables/` | Tables (Oracle only — MSSQL/MySQL use pgloader) |
| 2 | `wave_2_views/` | Views |
| 3 | `wave_3_functions/` | Functions |
| 4 | `wave_4_procedures_triggers/` | Stored procedures + triggers |

### Conversion process (per object, in dependency order from dep_graph.json)

For each object in `llm_required`, in topological order:

1. Read the object's DDL from `ddl_objects.json`
2. Identify any EWI codes from `assessment_summary.json` for this object
3. Convert the DDL to PostgreSQL using the type-mapping reference
4. Annotate the output with EWI comments:

```sql
-- ** SPG-EWI-0004 WARN: Procedure converted to PL/pgSQL — verify business logic **
-- ** SPG-EWI-0002 INFO: ISNULL → COALESCE **
CREATE OR REPLACE FUNCTION ...
```

5. Write to the appropriate wave directory:
   `$SPGLOADER_WORK_DIR/conversion/postgres/wave_N_<type>/<schema>__<name>.sql`

### EWI annotation rules

When converting, add EWI inline comments for each transformation:

| Transformation | EWI to add |
|----------------|-----------|
| Procedure/function body | SPG-EWI-0004 |
| Trigger restructured as trigger function | SPG-EWI-0005 |
| ROWNUM/TOP → LIMIT | SPG-EWI-0006 |
| CONNECT BY → recursive CTE | SPG-EWI-0007 |
| Cursor → set-based | SPG-EWI-0008 |
| Type with no direct equivalent → TEXT | SPG-EWI-0009 |
| Dialect hint removed (NOLOCK, etc.) | SPG-EWI-0011 |
| DUAL removed | SPG-EWI-0012 |
| Oracle function replaced with PG equiv | SPG-EWI-0002 |

### Conversion manifest

After all LLM conversions complete, write:
`$SPGLOADER_WORK_DIR/conversion/_conversion_report.json`

```json
{
  "pgloader_tables": ["dbo.customers", "dbo.orders"],
  "converted_objects": [
    {
      "fqn": "dbo.get_orders",
      "type": "procedure",
      "output_file": "conversion/postgres/wave_4_procedures_triggers/dbo__get_orders.sql",
      "ewi_codes": ["SPG-EWI-0004"]
    }
  ],
  "failed": []
}
```

## Output

- `$SPGLOADER_WORK_DIR/conversion/postgres/wave_N_*/` — EWI-annotated converted .sql files
- `$SPGLOADER_WORK_DIR/conversion/pgloader/migration.load` — pgloader config (if applicable)
- `$SPGLOADER_WORK_DIR/conversion/_conversion_report.json`
- Proceed to Phase 5 (deploy)

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `is_blocked = true` in `assessment_summary.json` | Phase 3.5 unresolved BLOCKs | Resolve BLOCK findings before proceeding |
| `convert_objects.py` fails on a specific object | Unsupported DDL pattern | Check the EWI annotation — manually rewrite that object |
| `fix_views.py` — `PIVOT` not converted | Missing `pivot_rules` in `view-fixes.yaml` | Add a pivot rule entry for that view's PIVOT syntax |
| `fix_functions.py` — `END IF` missing | Complex nested IF structure | Check the function body; add a manual `END IF` at the correct position |
| pgloader data load fails | Type mismatch or constraint violation | Check pgloader log; add a `CAST` or `USING` clause in the pgloader config |
