# Feedback Loop Configuration

## Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `SPGLOADER_FEEDBACK_DIR` | Shared drive path where testers upload feedback JSONL | `/Volumes/GoogleDrive/Shared drives/SPGLoader Testing/feedback` |
| `SPGLOADER_SKILL_DIR` | Path to the spgloader skill installation | `/Users/you/.snowflake/cortex/skills/spgloader` |

## Setup for Testers

1. **Mount the shared drive** (Google Drive, OneDrive, or network share)
2. **Set the environment variable** in your shell profile:
   ```bash
   export SPGLOADER_FEEDBACK_DIR="/Volumes/GoogleDrive/Shared drives/SPGLoader Testing/feedback"
   ```
3. **Run migrations normally** with `/spgloader`
4. After migration completes, failures are automatically collected and uploaded

If the shared drive is not available, the feedback file is saved locally at
`$SPGLOADER_WORK_DIR/feedback_export.jsonl`. You can manually share this file
with the skill owner.

## Setup for Skill Owner

1. **Ensure access to the shared drive** (same path as testers)
2. **Run the aggregator** when ready to apply fixes:
   ```bash
   python scripts/aggregate_feedback.py "$SPGLOADER_FEEDBACK_DIR"
   ```
3. **Invoke the auto-fix sub-skill** in CoCo:
   ```
   "Aggregate feedback and fix the top patterns"
   ```

## Shared Drive Structure

```
<Shared Drive>/SPGLoader Testing/
├── feedback/                      ← testers drop JSONL files here
│   ├── rekha_northwind_mssql_20260810.jsonl
│   ├── john_sakila_mysql_20260811.jsonl
│   └── sarah_hr_oracle_20260812.jsonl
├── aggregated/                    ← owner writes aggregated output here
│   └── aggregated_20260812.json
└── resolved/                      ← owner moves processed files here
    └── (moved after fixes are committed)
```

## JSONL Entry Schema

Each line in a feedback JSONL file:

```json
{
  "id": "a1b2c3d4",
  "timestamp": "2026-08-10T19:00:00Z",
  "tester": "rekha.khandhadia",
  "scenario": "northwind",
  "source_type": "mssql",
  "source_version": "2022",
  "phase": "deploy",
  "object_type": "procedure",
  "object_name": "dbo.CustOrderHist",
  "error": "ERROR: column \"customerid\" does not exist",
  "error_class": "column_does_not_exist",
  "source_sql_snippet": "SELECT CustomerID FROM ...",
  "converted_sql_snippet": "SELECT customerid FROM ...",
  "repair_attempted": true,
  "repair_succeeded": false,
  "spgloader_version": "commit:6ed4d3d"
}
```

## Workflow Summary

```
Tester runs /spgloader  →  failures collected  →  JSONL uploaded to shared drive
                                                         │
                    ┌────────────────────────────────────┘
                    ▼
Owner runs aggregator  →  sees ranked patterns  →  tells CoCo "fix top 3"
                                                         │
                    ┌────────────────────────────────────┘
                    ▼
CoCo applies fixes  →  verifies (replay + pytest + parity)  →  owner approves commit
```

## Known Error Classes and Fixes Applied

| error_class | Root cause | Fix location | Fixed in |
|---|---|---|---|
| `syntax_error` (reads sql data) | `READS SQL DATA` leaking into RETURNS or body | `convert_objects.py` RETURNS regex + body strip | 1.1.0 |
| `syntax_error` (GROUP_CONCAT SEPARATOR) | `SEPARATOR` keyword not valid in PG; broken `([^)]+)` regex in YAML | `_convert_group_concat()` in `convert_objects.py`; removed YAML rule | 1.1.0 |
| `syntax_error` (SET ROWCOUNT) | T-SQL `SET ROWCOUNT N` not converted | Body substitution in `convert_procedure` | 1.1.0 |
| `syntax_error` (beginning_date) | `^\s*BEGIN\s*` without `\b` matched `begin` in `@beginning_date` param name | `^\s*BEGIN\b\s*` fix + extended `as_body` regex | 1.1.0 |
| `type_does_not_exist` (_utf8mb4) | MySQL charset introducer `_utf8mb4'str'` leaking into view SQL | Stripped in `convert_view()` for mysql/mariadb | 1.1.0 |
| `relation_does_not_exist` | Trigger functions deploy before schema search_path includes their schema | `deploy_procedures.py` injects `SET search_path` | existing |
| `column_does_not_exist` (name as alias) | Single-quoted T-SQL aliases: `AS 'Name'` invalid in PG | `AS 'x'` → `AS "x"` in body + `single_quoted_alias` rule | 1.0.1 |
| `rowcount_conversion_bug` | `@@ROWCOUNT` → `@ROWCOUNT` after `@(\w+)` stripper ate one `@` | Explicit `@@ROWCOUNT` → `GET DIAGNOSTICS` before generic stripper | 1.0.1 |
| `cannot_change_output_params` | Proc re-created with changed OUT params without DROP first | `procedure-repair-prompt.md` hint: emit DROP PROCEDURE IF EXISTS first | 1.1.0 |
| `invalid_geometry` | MySQL POINT/GEOMETRY WKB binary not converted for PostGIS | `_mysql_geometry_to_wkt()` in `copy_source_data.py` | 1.1.0 |
| `unterminated_csv_quoted_field` | MySQL BLOB/TINYBLOB binary data breaks COPY text format | `bytea_cols` detection + bytes passthrough in `_convert_row_mysql` | 1.1.0 |
| `deadlock_detected` | Concurrent table copy workers hit SPG lock contention | 3-attempt retry with exponential back-off in `_copy_table` | 1.1.0 |
