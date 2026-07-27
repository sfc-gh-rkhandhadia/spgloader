---
name: spgloader-convert
description: "Phase 4: Classify extracted DDL objects and route to catalog deploy (all sources: parallel_deploy.py + copy_source_data.py / copy_oracle_data.py), or rule-based conversion with SPG EWI annotations and wave-ordered output."
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

Read `ddl_objects.json`. Classify each object by source type:

| Source | Tables (schema + data) | Views / Procs / Functions / Triggers |
|--------|------------------------|--------------------------------------|
| MSSQL / MySQL | parallel_deploy.py (schema) + copy_source_data.py (data) | Rule-based convert_objects.py (Phase 4B) |
| Oracle | parallel_deploy.py (schema) + copy_oracle_data.py (data) | Rule-based convert_objects.py --source-type oracle (Phase 4B) |

For reference, the `assessment_summary.json` has `catalog_eligible` and `llm_required` lists.

Show classification summary before proceeding.

---

## Phase 4A — MSSQL / MySQL: catalog deploy + data copy

All MSSQL and MySQL migrations use the catalog-based path. **pgloader is no longer the default** — it has memory heap issues on large schemas and requires a Docker image. The catalog approach is faster, parallel, and more reliable.

### Step 1: Deploy table schema via catalog path

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parallel_deploy.py \
  --source-type "$SOURCE_TYPE" \
  --source-host "$SOURCE_HOST" --source-port "$SOURCE_PORT" \
  --source-db   "$SOURCE_DATABASE" --source-user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --workers 8 \
  --output "$SPGLOADER_WORK_DIR/deployment/deployment_summary.json"
```

This deploys in 5 parallel phases: schemas → sequences → tables → indexes → foreign keys.

### Step 2: Copy data from source to SPG

Skip this step for **schema-only migrations** (DDL file source with no live data).

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/copy_source_data.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --workers 4
```

Optional flags:
- `--truncate-first` — TRUNCATE each target table before copying (idempotent reruns)
- `--batch-size 2000` — tune rows per INSERT batch (default 5000)
- `--tables dbo.orders dbo.customers` — copy a subset of tables only

Output: `$SPGLOADER_WORK_DIR/copy_data_report.json`

### Why not pgloader?

pgloader is available as a **legacy fallback** via `sub-skills/convert/pgloader/SKILL.md`
but should only be used if the user **explicitly requests it** after acknowledging the
performance warnings. Known pgloader issues:
- OOM on tables > a few million rows (loads full result set into JVM heap)
- Requires building a Docker image (`spgloader-pgloader:local`)
- FreeTDS / TLS negotiation failures on macOS arm64
- Single-threaded — slow for wide schemas (1000+ tables)

**Never silently fall back to pgloader.** If the catalog path fails, surface the error
to the user and fix it — do NOT automatically retry with pgloader.

---

## Phase 4A-Oracle — Oracle: catalog table deploy + data copy

For Oracle sources, the path is identical in structure to MSSQL/MySQL above.

**Step 1: Deploy table DDL (schema only) via catalog path:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/parallel_deploy.py \
  --source-type oracle \
  --source-host "$SOURCE_HOST" --source-port "$SOURCE_PORT" \
  --source-db   "$SOURCE_DATABASE" --source-user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --output "$SPGLOADER_WORK_DIR/deployment/deployment_summary.json"
```
This deploys: tables (with column types mapped), sequences, indexes, foreign keys.

**Step 2: Copy data from Oracle to SPG:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/copy_oracle_data.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```
Optional flags: `--truncate-first` (idempotent re-run), `--batch-size 2000` (tune for memory).
Output: `$SPGLOADER_WORK_DIR/copy_data_report.json`

---

## Phase 4B — DDL Object Conversion (views, procedures, functions, triggers)

### MSSQL / MySQL — rule-based conversion

**1. Convert all non-table objects:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/convert_objects.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --source-type mssql   # or mysql / mariadb
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

### Oracle — rule-based conversion

Oracle views, procedures, functions, and triggers are converted by the same `convert_objects.py`
script with `--source-type oracle`. No separate `fix_views.py` pass is needed.

**Convert all Oracle non-table objects (views, procedures, functions, triggers):**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/convert_objects.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --source-type oracle
```
Output: `conversion/postgres/wave_2_views/`, `wave_3_functions/`, `wave_4_procedures_triggers/`

The Oracle converter applies these transformations automatically:
- Parameter mode normalisation (`IN type` → `type`, `OUT type` → `OUT type`, `IN OUT` → `INOUT`)
- IS/AS separator → `AS $$ DECLARE ... BEGIN ... END; $$`
- Type substitutions: `NUMBER→NUMERIC`, `VARCHAR2→TEXT`, `DATE→TIMESTAMPTZ`, etc.
- Function substitutions: `NVL→COALESCE`, `SYSDATE→NOW()`, `SYS_GUID()→gen_random_uuid()`, `FROM DUAL` removed, `seq.NEXTVAL→NEXTVAL('seq')`, etc.
- `:NEW.col`/`:OLD.col` → `NEW.col`/`OLD.col` in triggers
- EWI annotations for patterns needing LLM review (CONNECT BY, ROWNUM, BULK COLLECT, %TYPE)

For objects flagged `SPG-EWI-0004`, `SPG-EWI-0007`, `SPG-EWI-0008` run the LLM repair loop:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/repair_procedures.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --source-type oracle
```
The repair loop auto-selects `references/prompts/procedure-repair-oracle-prompt.md`
(PL/SQL→PL/pgSQL rules) when `--source-type oracle` is specified.

For manual guidance, load:
- `references/type-mappings/oracle-to-pg.md` — Oracle type and function mapping reference
- `references/ewi-codes.md` — EWI annotation code catalog

### Output Directory (wave-ordered)

Write converted files to wave-ordered directories under `conversion/postgres/`:

| Wave | Directory | Object types |
|------|-----------|-------------|
| 2 | `wave_2_views/` | Views |
| 3 | `wave_3_functions/` | Functions |
| 4 | `wave_4_procedures_triggers/` | Stored procedures + triggers |

Note: Tables for all sources are deployed by `parallel_deploy.py` — no wave_1_tables directory.

### EWI annotation rules

convert_objects.py automatically annotates output with inline EWI comments:

| Transformation | EWI code |
|----------------|----------|
| Procedure/function body converted | SPG-EWI-0004 |
| Trigger restructured as trigger function | SPG-EWI-0005 |
| ROWNUM/TOP → LIMIT (needs review) | SPG-EWI-0006 |
| CONNECT BY / hierarchical query (needs review) | SPG-EWI-0007 |
| BULK COLLECT / FORALL → cursor loop | SPG-EWI-0008 |
| %TYPE / %ROWTYPE (type needs expanding) | SPG-EWI-0009 |
| Oracle function replaced with PG equiv | SPG-EWI-0002 |

### Conversion manifest

convert_objects.py writes `$SPGLOADER_WORK_DIR/conversion/_conversion_report.json`:

```json
{
  "source_type": "mssql",
  "catalog_tables": ["dbo.orders", "dbo.customers"],
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

- `$SPGLOADER_WORK_DIR/deployment/deployment_summary.json` — table/index/FK deploy results
- `$SPGLOADER_WORK_DIR/copy_data_report.json` — data copy results (if applicable)
- `$SPGLOADER_WORK_DIR/conversion/postgres/wave_N_*/` — EWI-annotated converted .sql files
- `$SPGLOADER_WORK_DIR/conversion/_conversion_report.json`
- Proceed to Phase 5 (deploy)

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `is_blocked = true` in `assessment_summary.json` | Phase 3.5 unresolved BLOCKs | Resolve BLOCK findings before proceeding |
| `parallel_deploy.py` — `column X default expression is of type boolean` | pg_generator.py bug | Ensure pg_generator.py has the MSSQL default fixes (commit 8160bc4+) |
| `parallel_deploy.py` — `syntax error at or near "["` | Bracket identifiers in defaults | Same fix — pg_generator.py bracket-stripping handles this |
| `convert_objects.py` fails on a specific object | Unsupported DDL pattern | Check the EWI annotation — manually rewrite that object |
| `fix_views.py` — `PIVOT` not converted | Missing `pivot_rules` in `view-fixes.yaml` | Add a pivot rule entry for that view's PIVOT syntax |
| `fix_functions.py` — `END IF` missing | Complex nested IF structure | Check the function body; add a manual `END IF` at the correct position |
| `copy_source_data.py` — `env var not set` | Password not exported | Run `export MSSQL_SA_PASSWORD='...'` then retry |
| `copy_source_data.py` — row type mismatch | MSSQL type not mapped cleanly | Add `--truncate-first`; check column types in SPG match source |
| `copy_oracle_data.py` — `env var not set` | Password not exported | Run `export ORACLE_PWD='...'` then retry |
| Oracle procedure — `END name;` in output | `_apply_oracle_func_subs` missed it | Check that the pattern `END \w+;` matches; add to `_ORACLE_FUNC_SUBS` if not |
