---
name: spgloader-deploy
description: "Deploy converted DDL objects to Snowflake Postgres: tables via parallel_deploy.py, views/functions/procedures via dedicated deploy scripts, then LLM repair for any failures."
parent_skill: spgloader
---

# spgloader — Phase 5: Deploy

## When to Load

From `spgloader/SKILL.md` Phase 5. `dep_graph.json`, converted SQL files, and
`conversion_manifest.json` are in `$SPGLOADER_WORK_DIR`.

---

## Workflow

### Step 1: Display deployment plan

Show the user what will be deployed:

```
Deployment Plan
===============
Tables (via parallel_deploy.py):   1494  → schemas, sequences, tables, indexes, FKs
Views (deploy_views.py):              55  → wave_2_views_fixed/
Functions (deploy_functions.py):      20  → wave_3_functions_fixed/
Procedures (deploy_procedures.py):    45  → wave_4_procedures_triggers/
```

### Step 2: MANDATORY STOPPING POINT

```
I will deploy all objects to SPG instance '<TARGET_SPG_SERVICE>'.
- Tables/indexes/FKs are created via catalog (parallel_deploy.py)
- Views, functions, procedures are deployed from converted SQL files
- Any existing objects with the same name will be replaced

Proceed? (yes/no)
```

Wait for explicit "yes" before continuing.

### Step 3: Deploy tables (parallel_deploy.py)

Tables are always deployed first since views/functions/procedures depend on them.

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

Expected output: `→ tables: N OK, 0 failed`

If there are failures, check `deployment_summary.json` for the error per table.
Most common causes are fixed DDL generator bugs — ensure `pg_generator.py` is
current (commit 8160bc4+).

---

### Step 4: Deploy views

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_views.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"

uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_views.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```

`fix_views.py` applies 6 passes including Pass 5 which loads `view_only` rules
from `plpgsql-fixes.yaml` (generalizable SQL fixes: string concat `+`→`||`,
`boolean = 1`→`= true`, timestamp `<> ''`→`IS NOT NULL`, LATERAL join `ON true`).

---

### Step 5: Deploy functions

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/fix_functions.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --mapping "<SKILL_DIR>/references/fix-mappings/view-fixes.yaml"

uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_functions.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
```

---

### Step 6: Deploy procedures

The script **defaults to non-interactive mode** — it never prompts via stdin.
All decisions come from `deprecated_review.json` (Phase 3.6).

**Normal run (non-interactive, default):**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_procedures.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE"
# --no-interactive is the default; explicit flag is also accepted
```
If undecided legacy groups exist, they are skipped silently and logged.

**Interactive run — use this when you want the user to decide:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/deploy_procedures.py \
  --work-dir "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --interactive
```

**Handling exit code 2 (undecided legacy groups in interactive mode):**

If `deploy_procedures.py` exits with code 2, it found legacy procedure groups not yet decided in
Phase 3.6. It will have written them to `$SPGLOADER_WORK_DIR/deprecated/legacy_groups_pending.json`.

The skill MUST:

1. Read `legacy_groups_pending.json` to get the undecided groups.
2. Use `ask_user_question` to present each group to the user with **migrate** or **skip** options.
   Show the group label, description, and procedure count.
3. Write the user's decisions back into `deprecated_review.json` under `groups[label].disposition`.
4. Re-run `deploy_procedures.py --interactive` — it will now read the updated decisions and proceed.

```python
# Example: reading pending groups and prompting user
import json, pathlib
pending = json.loads(pathlib.Path(f"{WORK_DIR}/deprecated/legacy_groups_pending.json").read_text())
# For each group in pending["pending_groups"]:
#   ask_user_question with options: ["Migrate to PostgreSQL", "Skip — exclude from migration"]
#   write decision to deprecated_review.json
```

**Never skip this step or default silently — the user must decide.**

---

### Step 7: LLM repair for any failures

If any views, functions, or procedures failed to deploy, run the repair pipeline.
Uses `claude-sonnet-4-5` with 6 parallel workers for fast repair (~3-5 minutes
for 60+ objects).

**Repair functions:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/repair_procedures.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --report-file "$SPGLOADER_WORK_DIR/conversion/functions_deploy_report.json" \
  --wave-dir    "$SPGLOADER_WORK_DIR/conversion/postgres/wave_3_functions_fixed" \
  --workers 6
```

**Repair procedures:**
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/repair_procedures.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --workers 6
```

**Repair views:**
```bash
# 1. Build a deduplicated report from the views deploy report
python3 - << 'EOF'
import json
r = json.load(open(f"{WORK_DIR}/conversion/deploy_report.json"))
seen, unique = set(), []
for f in r.get("failed", []):
    name = f.get("view", f.get("procedure", ""))
    if name not in seen:
        seen.add(name)
        unique.append({"procedure": name, "file": f["file"], "error": f["error"]})
json.dump({"failed": unique}, open("/tmp/views_repair_report.json", "w"), indent=2)
EOF

# 2. Run LLM repair
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/repair_procedures.py \
  --work-dir    "$SPGLOADER_WORK_DIR" \
  --spg-service "$TARGET_SPG_SERVICE" \
  --report-file "/tmp/views_repair_report.json" \
  --wave-dir    "$SPGLOADER_WORK_DIR/conversion/postgres/wave_2_views_fixed" \
  --workers 6
```

**Key options for repair_procedures.py:**

| Flag | Default | Purpose |
|---|---|---|
| `--workers N` | 1 | Parallel LLM repair workers (use 4-8) |
| `--model MODEL` | from config | Override Cortex model |
| `--max-iterations N` | from config (2) | Max LLM attempts per object |
| `--report-file PATH` | procedures_deploy_report.json | Point at functions or views report |
| `--wave-dir PATH` | wave_4_procedures_triggers | Source SQL directory |
| `--rules-only` | false | Rule-based fixes only, skip LLM |

The config file `references/llm-repair-config.yaml` sets:
- `model: claude-sonnet-4-5`
- `max_iterations: 2`

The repair pipeline prints a 1-minute status update: `[Status 1:00] Fixed: N  Failed so far: N  Remaining: N/total`

---

### Step 8: Display results

```
Deployment Results
==================
Tables:     N deployed / 0 failed
Views:      N deployed / M failed (M need manual SQL fixes)
Functions:  N deployed / M fixed by LLM / P still failing
Procedures: N deployed / M fixed by LLM / P still failing (UDTT dependency)
```

If any objects are still failing after LLM repair, list them and their errors.
Objects that use custom UDTT array types (`RecordType[]`, etc.) cannot be
auto-repaired — they need manual type substitution.

---

## Output

- `$SPGLOADER_WORK_DIR/deployment/deployment_summary.json` — table deploy results
- `$SPGLOADER_WORK_DIR/conversion/deploy_report.json` — view deploy results
- `$SPGLOADER_WORK_DIR/conversion/functions_deploy_report.json` — function deploy results
- `$SPGLOADER_WORK_DIR/conversion/procedures_deploy_report.json` — procedure deploy results
- `$SPGLOADER_WORK_DIR/conversion/repair_report.json` — LLM repair results
- Proceed to Phase 6 (validate)

## Error Handling

| Error pattern | Likely cause | Action |
|---|---|---|
| `column X default expression is of type boolean` | BIT default in DDL | Ensure pg_generator.py has MSSQL default fixes (commit 8160bc4+) |
| `syntax error at or near "["` | Bracket identifiers in defaults | Same — pg_generator.py bracket-stripping fix |
| `syntax error at or near ...` | Incomplete T-SQL→PL/pgSQL conversion | Re-run `fix_functions.py`; then LLM repair |
| `relation "X" does not exist` | Missing table or schema prefix | Deploy tables first; add to `schema_prefix.tables` in `view-fixes.yaml` |
| `type "X[]" does not exist` | UDTT array type (deprecated) | Mark as out-of-scope — cannot auto-repair |
| `function X() does not exist` | UDF not deployed yet | Deploy functions first; check wave order |
| `column "X" does not exist` | Computed column (doesn't exist in PG) | Remove the column from the SELECT list |
