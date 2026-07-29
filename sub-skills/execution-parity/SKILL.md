---
name: spgloader-execution-parity
description: "Execution parity testing: executes every stored procedure and function on both the source database (MSSQL, MySQL, MariaDB, or Oracle) and the migrated Snowflake Postgres instance using identical parameters, compares result sets row-by-row with data hashing, writes verdicts to SPG audit tables, and generates a Snowflake-branded PowerPoint sign-off report."
parent_skill: spgloader
---

# spgloader — Execution Parity Sub-skill

## Purpose

**Structural parity** (`full_validation.py`, Phase 6.6 Step 8) checks parameter signatures,
column names, and view row counts — but does **not** execute code or compare result set contents.

**Execution parity** (this sub-skill) goes further:
- Executes every procedure and function on **both sides** with identical sampled parameters
- Compares result sets row-by-row via data hashing
- Compares views by column list and row count
- Classifies each object with a verdict (PASS / FAIL / SPG_ERROR / BOTH_FAILED / SKIPPED)
- Writes all verdicts to `validation.validation_result` in SPG for audit

Supports **all source types spgloader handles**: MSSQL · MySQL · MariaDB · Oracle.

---

## When to Load

From `witness-validate/SKILL.md` Step 8.5, after structural parity (Step 8) completes
and the user opts into behavioral execution testing.

All connection env vars are already set from Step 1 of `witness-validate/SKILL.md`.

---

## Environment Setup

Set `SOURCE_TYPE` from `source_conn.env` before running any scripts:

```bash
source "$SPGLOADER_WORK_DIR/source_conn.env"
# SOURCE_TYPE is set by source-setup sub-skill:
#   mssql | mysql | mariadb | oracle
export SOURCE_TYPE="${SOURCE_TYPE:-mssql}"

# Source credentials — use SOURCE_* vars (MSSQL_* aliases still work for mssql)
export SOURCE_HOST="$SOURCE_HOST"
export SOURCE_PORT="${SOURCE_PORT:-1433}"
export SOURCE_USER="sa"          # or appropriate admin user for SOURCE_TYPE
export SOURCE_PASSWORD="$MSSQL_SA_PASSWORD"   # from env set earlier
export SOURCE_DATABASE="$SOURCE_DATABASE"

# SPG credentials — already set from witness-validate Step 1
# export SPG_HOST SPG_USER SPG_PASSWORD SPG_DATABASE

# Output dirs
export SHARED_DIR="$SPGLOADER_WORK_DIR/validation_shared"
export VALIDATION_OUTPUT_DIR="$SPGLOADER_WORK_DIR/validation_exec"
mkdir -p "$SPGLOADER_WORK_DIR/validation_exec" "$SPGLOADER_WORK_DIR/validation_shared"

# Resolve script directory
EP_SCRIPTS="<SKILL_DIR>/scripts/execution-parity"
```

---

## Step 1 — Setup audit tables in SPG (once per SPG instance)

```bash
PGPASSWORD="$SPG_PASSWORD" psql \
  -h "$SPG_HOST" -U "$SPG_USER" -d "$SPG_DATABASE" \
  -f "$EP_SCRIPTS/setup_validation_tables.sql"
```

Creates: `validation.validation_result`, `validation.validation_run`, `validation.v_run_summary`

---

## Step 2 — Copy seed data from source DB into SPG

Copies the rows seeded in Phase 6.5 from the source DB into the corresponding SPG tables
so both sides have identical data when procedures execute.

```bash
python3 "$EP_SCRIPTS/load_source_to_spg.py"
```

**Env vars read:** `SOURCE_TYPE`, `SOURCE_HOST`, `SOURCE_PORT`, `SOURCE_USER`,
`SOURCE_PASSWORD`, `SOURCE_DATABASE`, `SPG_HOST`, `SPG_USER`, `SPG_PASSWORD`, `SPG_DATABASE`,
`SHARED_DIR` (for `object_inventory.json`).

Show progress: `Tables loaded: N  |  Total rows: M  |  Skipped: K`

---

## Step 3 — Execute source DB procedures/functions (capture outputs + sample params)

Discovers every procedure and function in the source DB, samples real parameter
values from the seed data, executes each one, and saves outputs + sampled params
to a shared file for SPG reuse.

```bash
python3 "$EP_SCRIPTS/source_proc_executor.py"
```

**Routing by `SOURCE_TYPE`:**
- `mssql` → uses `sys.objects` + `sys.parameters` catalog via `SourceAdapter`
- `mysql` / `mariadb` → uses `INFORMATION_SCHEMA.ROUTINES` + `INFORMATION_SCHEMA.PARAMETERS`
- `oracle` → uses `ALL_PROCEDURES` + `ALL_ARGUMENTS`

Writes to:
- `$VALIDATION_OUTPUT_DIR/source_output.jsonl` (aka `mssql_output.jsonl`)
- `$VALIDATION_OUTPUT_DIR/shared_sampled_params.json`

Show progress: `SOURCE DONE [MSSQL]  Success=N  Errors=M  Skipped=K`

---

## Step 4 — Execute same procedures/functions on SPG (using shared params)

Re-executes every procedure and function on SPG using the **exact same sampled parameter
values** captured in Step 3. This ensures a true apples-to-apples comparison.

```bash
python3 "$EP_SCRIPTS/spg_proc_executor.py"
```

Writes to: `$VALIDATION_OUTPUT_DIR/spg_output.jsonl`

Show progress: `SPG DONE  Success=N  Errors=M  Skipped=K`

---

## Step 5 — Diff outputs and write audit records

Compares source DB vs SPG result sets row-by-row, classifies each object using the
verdict taxonomy below, and writes all results to `validation.validation_result` in SPG.

```bash
python3 "$EP_SCRIPTS/compare_proc_outputs.py"
```

Show summary:
```
SUMMARY
=======
  PASS        : N   (exact output match)
  FAIL        : N   (both ran, outputs differ)
  SPG_ERROR   : N   (source OK → SPG threw runtime error)
  BOTH_FAILED : N   (both failed — missing prereq state, not a conversion defect)
  SKIPPED     : N   (write/DML procs — use validate_write_procs.py)

Results stored: validation.validation_result (run_number=N)
```

---

## Step 6 — Execute and compare views on both sides

Discovers every view in both source DB and SPG, executes `SELECT COUNT(*)`
and column-level comparison on each, and writes results to the view log.

```bash
python3 "$EP_SCRIPTS/validate_batch.py"
```

**Routing by `SOURCE_TYPE`:**
- `mssql` → `sys.views` + `SELECT COUNT(*) FROM [schema].[view]`
- `mysql` / `mariadb` → `INFORMATION_SCHEMA.VIEWS` + `SELECT COUNT(*) FROM schema.view`
- `oracle` → `ALL_VIEWS` + `SELECT COUNT(*) FROM schema.view`

Show summary: `VIEWS SUMMARY — PASS:N  FAIL:M  WARN:K  MSSQL_ONLY:J  SPG_ONLY:L`

---

## Step 7 — Generate PowerPoint sign-off report (optional)

Ask:
```
ask_user_question:
  header: "PowerPoint Report"
  question: "Generate a Snowflake-branded client-delivery PowerPoint from the live execution results?"
  options:
    - label: "Yes — generate .pptx"
      description: "27-slide deck: KPIs, pass rate chart, failure breakdown, remediation priorities, failed object appendix."
    - label: "No — skip"
```

If yes:
```bash
python3 "$EP_SCRIPTS/generate_migration_report.py" \
  --client "Migration Report" \
  --spg-host "$SPG_HOST" --spg-password "$SPG_PASSWORD" \
  --mssql-host "$SOURCE_HOST" --mssql-port "$SOURCE_PORT" \
  --mssql-user "$SOURCE_USER" --mssql-password "$SOURCE_PASSWORD" \
  --mssql-db "$SOURCE_DATABASE"
```

Output: `~/Downloads/Migration_Validation_<date>.pptx` or `~/Google Drive/My Drive/`

---

## Verdict Taxonomy

| Verdict | Meaning | Counts toward pass rate? |
|---|---|---|
| `PASS` | Exact output match on both sides | Yes |
| `FAIL` | Both ran, outputs differ | Yes (as failure) |
| `SPG_ERROR` | Source OK → SPG threw runtime error | Yes (as failure) |
| `SPG_NO_RESULTSET` | Procedure can't return result set (needs FUNCTION conversion) | Yes (as failure) |
| `BOTH_FAILED` | Both sides failed — missing prereq state, not conversion defect | **No** |
| `FAIL_MISSING_PREREQ` | Missing seed data / prerequisite state | **No** |
| `FAIL_HARNESS` | Harness/parameter error | **No** |
| `SKIPPED` | Write/DML procedure (run validate_write_procs.py separately) | **No** |

---

## Multi-DB Source Catalog Translation

| Operation | MSSQL | MySQL / MariaDB | Oracle |
|---|---|---|---|
| List routines | `sys.objects WHERE type IN ('P','FN','IF','TF')` | `INFORMATION_SCHEMA.ROUTINES` | `ALL_PROCEDURES` |
| Get parameters | `sys.parameters + sys.types` | `INFORMATION_SCHEMA.PARAMETERS` | `ALL_ARGUMENTS` |
| Get routine body | `sys.sql_modules` | `ROUTINES.ROUTINE_DEFINITION` | `DBMS_METADATA.GET_DDL` |
| SELECT top N | `SELECT TOP N` | `SELECT ... LIMIT N` | `WHERE ROWNUM <= N` |
| List views | `sys.views` | `INFORMATION_SCHEMA.VIEWS` | `ALL_VIEWS` |

All translation is handled by `source_adapter.py` — scripts call `SourceAdapter` methods
and are source-type agnostic.

---

## Backward Compatibility

MSSQL_* env vars (MSSQL_HOST, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DATABASE, MSSQL_PORT)
are still accepted as aliases when `SOURCE_TYPE=mssql`. Existing spgloader workspaces
that already have MSSQL_* variables set will work without modification.

---

## Output Artifacts

```
$SPGLOADER_WORK_DIR/
└── validation_exec/
    ├── source_output.jsonl           ← source DB execution results
    ├── spg_output.jsonl              ← SPG execution results
    ├── shared_sampled_params.json    ← shared params for source→SPG reuse
    ├── comparison_report.txt         ← diff summary
    └── view_validation.log           ← view comparison log

$SPGLOADER_WORK_DIR/validation_shared/
    └── load_summary.json             ← seed data copy summary

SPG audit tables:
    validation.validation_result      ← per-object verdicts (run_number=N)
    validation.validation_run         ← run metadata
    validation.v_run_summary          ← aggregated pass/fail per run
```

---

## Error Handling

| Error | Likely cause | Action |
|---|---|---|
| `SOURCE_TYPE not set` | source_conn.env not sourced | `source "$SPGLOADER_WORK_DIR/source_conn.env"` |
| `mysql.connector not found` | MySQL driver missing | `pip install mysql-connector-python` |
| `cx_Oracle not found` | Oracle driver missing | `pip install cx_Oracle` |
| `relation "fundstracking" does not exist` | Table not deployed to SPG | Normal — FAIL_MISSING_PREREQ |
| `operator does not exist: boolean = integer` | BIT→INT type mismatch in migrated proc | Conversion defect — repair needed |
| `SPG_NO_RESULTSET` | Procedure returns results but was migrated as PROCEDURE instead of FUNCTION | Recreate as `CREATE FUNCTION ... RETURNS TABLE(...)` |
