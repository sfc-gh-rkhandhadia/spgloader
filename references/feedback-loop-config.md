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
