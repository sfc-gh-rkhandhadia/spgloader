---
name: spgloader-assess
description: "SPG Compatibility Assessment — Phase 3.5 guardrail. Scans extracted DDL against Snowflake Postgres-specific compatibility rules before any conversion begins. BLOCK findings halt migration; WARN findings require user confirmation."
parent_skill: spgloader
---

# spgloader — Phase 3.5: SPG Compatibility Assessment (Guardrail)

## Purpose

This is the **migration guardrail** for Snowflake Postgres. Before any conversion
or deployment occurs, the assessment scans every extracted DDL object against
SPG-specific compatibility rules sourced from the official Snowflake Postgres
documentation.

Load `references/spg-compatibility.md` for the complete SPG rule set.

## When to Load

From `spgloader/SKILL.md` Phase 3.5 — after DDL extraction, before conversion.

## Stopping Behavior

| Finding Level | Action |
|--------------|--------|
| **BLOCK** | Hard stop — do NOT proceed to Phase 4. List findings and resolution steps. |
| **WARN** | Mandatory confirmation prompt — user must explicitly acknowledge before continuing. |
| **RESOLVE** | Automatic resolution generated (extension prereq script) — no stop required. |

## Workflow

### Step 1: Run the SPG compatibility scanner

Load `source_conn.env` and `target_conn.env` to build the `--source-desc` string.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/assess.py \
  --source-type "$SOURCE_TYPE" \
  --ddl-objects "$SPGLOADER_WORK_DIR/ddl_objects.json" \
  --output "$SPGLOADER_WORK_DIR/assessment/" \
  --source-desc "$SOURCE_TYPE $SOURCE_VERSION @ $SOURCE_HOST:$SOURCE_PORT/$SOURCE_DATABASE"
```

The script:
- Exits with code **0** if no BLOCK findings (may still have WARN)
- Exits with code **1** if any BLOCK findings exist
- Writes `assessment/assessment_summary.json`
- Writes `assessment/assessment_report.md`
- Writes `assessment/pre_deploy_extensions.sql` if extension prerequisites found

### Step 2: Display the assessment report

Show the full report in chat. The report includes:
- Object inventory (pgloader vs LLM conversion breakdown)
- Conversion confidence score
- BLOCKED findings with SPG rule citations
- WARN findings with resolution guidance
- Extension prerequisite recommendations

### Step 3: ⚠️ MANDATORY STOPPING POINT — BLOCK findings

If exit code = 1 (BLOCK findings): display:

```
MIGRATION BLOCKED

The following SPG incompatibilities must be resolved before migration can proceed:

[For each BLOCK finding, show:]
  Code:        SPG-BLOCK-XXX
  Object:      <fqn>
  Issue:       <detail>
  SPG Rule:    <spg_rule from ewi-codes.md>
  Resolution:  <guidance>

The migration cannot continue until these are resolved.
Options:
  1. Modify the source DDL to remove the incompatible feature
  2. Re-run DDL extraction after modifying the source
  3. Accept the object as out-of-scope (remove it from ddl_objects.json)
```

Do NOT load `sub-skills/convert/SKILL.md`. Return control to the user.

Record the assessment in the workspace manifest with BLOCK status:
```bash
# The assess.py script writes to assessment_summary.json
# The main orchestrator reads is_blocked from there to gate Phase 4
```

### Step 4: ⚠️ MANDATORY CONFIRMATION — WARN findings exist (no BLOCKs)

If there are WARN findings but no BLOCKs, display:

```
SPG Compatibility Warnings Found

The following items require your review. The migration can proceed,
but these objects may need manual adjustment after conversion:

[For each WARN finding:]
  [SPG-WARN-XXX] <object_fqn>: <detail>

Do you want to proceed with migration? (yes/no)
```

Use `ask_user_question` for this confirmation.

If the user says **no**: stop and return control.
If the user says **yes**: proceed to Phase 4.

### Step 4.5: ⚠️ TINYINT(1) mapping choice (MySQL / MariaDB only)

If `assessment_summary.json` has `tinyint1_count > 0`, ask the user how to map these columns.
This must be asked **before** Phase 4 conversion begins so the choice can be applied consistently.

```python
import json, pathlib
summary = json.loads(pathlib.Path(f"{WORK_DIR}/assessment/assessment_summary.json").read_text())
tinyint1_count = summary.get("tinyint1_count", 0)
```

If `tinyint1_count > 0`:

```
ask_user_question:
  header: "TINYINT(1) mapping"
  question: "<tinyint1_count> column(s) use TINYINT(1).

             In MySQL, TINYINT(1) is the conventional boolean flag (0/1 → false/true).
             Some schemas also use TINYINT(1) for small numeric values (counters, status codes).

             How should TINYINT(1) columns be mapped in Snowflake Postgres?"
  defaultAnswer: "BOOLEAN (MySQL convention — recommended)"
  options:
    - label: "BOOLEAN (MySQL convention — recommended)"
      description: "Map to PG BOOLEAN. Values 0→false, 1→true. Best choice for
                    flag columns (is_active, deleted, enabled, archived)."
    - label: "SMALLINT (preserve as numeric)"
      description: "Map to PG SMALLINT. Values remain 0/1 as integers. Choose
                    if TINYINT(1) is used for counters or small numeric values in your schema."
```

Write the user's choice back into `assessment_summary.json`:

```python
summary["tinyint1_mapping"] = "boolean"   # or "smallint"
pathlib.Path(f"{WORK_DIR}/assessment/assessment_summary.json").write_text(json.dumps(summary, indent=2))
```

Phase 4 `convert_objects.py` and `parallel_deploy.py` read `tinyint1_mapping` from this file
and apply the correct type consistently across all TINYINT(1) columns.

If `tinyint1_count == 0` (no TINYINT(1) found, or not a MySQL/MariaDB migration): skip this step.

### Step 5: Deploy extension prerequisites (if any)

If `assessment/pre_deploy_extensions.sql` was generated, display it to the user:

```
The following extensions will be enabled in SPG before deploying objects:

<contents of pre_deploy_extensions.sql>

This will be run automatically as the first step of Phase 5 (Deploy).
```

Record in `$SPGLOADER_WORK_DIR/.spgloader/manifest.json`:
- phase: "assess"
- status: "passed" | "blocked"
- block_codes: [list of SPG-BLOCK-XXX codes]
- warn_codes: [list of SPG-WARN-XXX codes]

### Step 6: Proceed to Phase 4

If assessment passed (no BLOCKs, or WARNs acknowledged):
- State: "Assessment passed. Proceeding to conversion."
- Load `sub-skills/convert/SKILL.md`

## Output

- `$SPGLOADER_WORK_DIR/assessment/assessment_summary.json`
- `$SPGLOADER_WORK_DIR/assessment/assessment_report.md`
- `$SPGLOADER_WORK_DIR/assessment/pre_deploy_extensions.sql` (if needed)
- Workspace manifest updated with assess phase result

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `ddl_objects.json` not found | Phase 3 not completed | Run ddl-extract phase first |
| `assess.py` exits with no output | Empty DDL input | Check that `ddl_objects.json` has objects (not an empty array) |
| BLOCK findings cannot be resolved | SPG hard incompatibility (CLR, linked servers) | Scope those objects out — add them to a manual remediation list |
| WARN for extension (e.g. uuid-ossp) | Extension not pre-installed on SPG | Run `pre_deploy_extensions.sql` before Phase 5; if script fails, install manually |
