---
name: spgloader-ddl-extract
description: "Extract DDL from the source database and build an object dependency graph for ordered migration."
parent_skill: spgloader
---

# spgloader — Phase 3: DDL Extraction

## When to Load

From `spgloader/SKILL.md` Phase 3. `source_conn.env` is in `$SPGLOADER_WORK_DIR`.

## Overview

This phase extracts schema DDL from the source database and produces:
- `ddl_objects.json` — all objects with type, name, DDL text, and raw dependency hints
- `dep_graph.json` — objects sorted in topological order for deployment

## Workflow

### Step 1: Determine DDL source

Ask the user with `ask_user_question` (options):
1. **Extract from connected source database** — runs extract_ddl.py against the live source
2. **Provide DDL as a file** — user supplies an existing .sql file
3. **Paste DDL directly** — user pastes SQL text into chat

---

### Option A: Extract from live database

Load `source_conn.env`:
```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
```

Run extraction:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --host "$SOURCE_HOST" \
  --port "$SOURCE_PORT" \
  --database "$SOURCE_DATABASE" \
  --user "$SOURCE_USER" \
  --password-env "$SOURCE_PASSWORD_ENV" \
  --output "$SPGLOADER_WORK_DIR/ddl_objects.json"
```

---

### Option B: DDL file

Ask for the file path (text input, default: `~/source_schema.sql`).

Run parsing:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --ddl-file "<path>" \
  --output "$SPGLOADER_WORK_DIR/ddl_objects.json"
```

---

### Option C: Paste DDL

Tell the user to paste their DDL. After receiving it, write it to:
`$SPGLOADER_WORK_DIR/pasted_ddl.sql`

Then run:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/extract_ddl.py \
  --source-type "$SOURCE_TYPE" \
  --ddl-file "$SPGLOADER_WORK_DIR/pasted_ddl.sql" \
  --output "$SPGLOADER_WORK_DIR/ddl_objects.json"
```

---

### Step 2: Build dependency graph

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/build_dep_graph.py \
  --input "$SPGLOADER_WORK_DIR/ddl_objects.json" \
  --output "$SPGLOADER_WORK_DIR/dep_graph.json"
```

### Step 3: Show extraction summary

Parse `ddl_objects.json` and display a summary table:

```
DDL Extraction Complete
=======================
Object type          Count
-------------------  -----
Tables               N
Views                N
Stored procedures    N
Functions            N
Triggers             N
-------------------  -----
Total                N

Working dir: <SPGLOADER_WORK_DIR>
```

If any objects failed to parse, list them with the parse error.

## Output

- `$SPGLOADER_WORK_DIR/ddl_objects.json` — all extracted objects
- `$SPGLOADER_WORK_DIR/dep_graph.json` — topologically sorted object list
- Proceed to Phase 3.5 (assess)

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `Login failed for user 'sa'` | Wrong password or SA not enabled | Verify SA password; for Docker, check the `MSSQL_SA_PASSWORD` env var |
| `Connection refused` | Database not ready or wrong port | Wait for container health check to complete; verify port mapping |
| `Cannot open database` | Wrong database name | Re-run with correct `--database` value |
| `extract_ddl.py` exits with no objects | Schema name filter too narrow | Check `--schema` argument or omit it to extract all schemas |
| `build_dep_graph.py` circular dependency error | Self-referencing or mutual view dependencies | Note the cycle; those objects may need manual ordering in deployment |
| DDL file parse errors | Non-standard or mixed SQL dialects | Check the parse error message; try `--source-type` override |
