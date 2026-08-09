# Workspace Data Contracts

This document defines the **data contract** between spgloader scripts. Every file listed
here has an authoritative writer and one or more readers. If a writer changes its output
format, the contract test (`tests/test_workspace_contract.py`) will fail.

---

## Authoritative Files (Report Sources)

The migration report (`generate_report.py`) reads ONLY from these files. Every other
JSON in the workspace is intermediate and must NOT be read directly by the report.

### `deployment/deployment_summary.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `parallel_deploy.py` | |
| **Read by** | `html_report.py` (line ~172) | |

```json
{
  "source_db": "AdventureWorks",
  "phases": {
    "schemas":      {"ok": 6,   "failed": 0},
    "sequences":    {"ok": 0,   "failed": 0},
    "tables":       {"ok": 71,  "failed": 0},
    "indexes":      {"ok": 102, "failed": 0},
    "foreign_keys": {"ok": 90,  "failed": 0}
  },
  "failures": [],
  "extensions_installed": ["ltree", "uuid-ossp", "postgis"],
  "elapsed_s": 45.0
}
```

**Required keys:** `phases.tables.ok`, `phases.indexes.ok`, `phases.foreign_keys.ok`, `source_db`, `failures[]`

---

### `.spgloader/migration_state.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `deploy_views.py`, `deploy_functions.py`, `deploy_procedures.py`, `repair_procedures.py` | |
| **Read by** | `html_report.py` (line ~210) — **primary source for view/func/proc counts** | |

```json
{
  "schema_version": 1,
  "views": {
    "succeeded": ["schema.viewname", ...],
    "failed": [{"fqn": "schema.name", "error": "..."}],
    "skipped": ["schema.viewname"]
  },
  "functions": {
    "succeeded": ["schema.funcname", ...],
    "failed": [{"fqn": "schema.name", "error": "..."}]
  },
  "procedures": {
    "succeeded": ["schema.procname", ...],
    "failed": [{"fqn": "schema.name", "error": "...", "resolution": "platform_limitation"}]
  }
}
```

**Required keys:** `views.succeeded[]`, `views.failed[]`, `functions.succeeded[]`, `procedures.succeeded[]`

**Rule:** Any script that deploys or repairs objects MUST update this file before exiting.

---

### `validation/catalog_verification.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `catalog_verify.py` | |
| **Read by** | `html_report.py` → `_render_catalog_tab()` | |

```json
{
  "generated_at": "2026-08-09T18:10:00",
  "source": "mssql @ localhost:1433/AdventureWorks",
  "target": "adventureworks_pg",
  "summary": {
    "tables_total": 71,
    "tables_match": 63,
    "tables_col_mismatch": 8,
    "views_total": 20,
    "views_match": 13,
    "views_missing": 2,
    "views_col_mismatch": 5,
    "functions_total": 11,
    "functions_match": 2,
    "functions_param_mismatch": 9,
    "procedures_total": 9,
    "procedures_match": 1,
    "procedures_param_mismatch": 8,
    "triggers_total": 10,
    "triggers_match": 9,
    "triggers_missing": 1
  },
  "objects": [
    {
      "source_fqn": "HumanResources.Employee",
      "target_fqn": "humanresources.employee",
      "type": "table",
      "status": "match|col_mismatch|param_mismatch|missing",
      "source_col_count": 15,
      "target_col_count": 16,
      "llm_repaired": false,
      "error": null
    }
  ]
}
```

**Required keys:** `summary` (with `*_total` and `*_match` for each type), `objects[]`

---

### `validation/validation_report.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `catalog_verify.py` → `_write_validation_checks()` | |
| **Read by** | `html_report.py` → Schema Verification tab (`val_checks`) | |

```json
{
  "source": "mssql @ localhost:1433/AdventureWorks",
  "target": "adventureworks_pg",
  "generated_at": "2026-08-09T18:10:00",
  "checks": [
    {
      "check": "Tables in SPG",
      "passed": true,
      "source_count": 71,
      "spg_count": 71,
      "details": "71/71 tables deployed"
    }
  ]
}
```

**Required keys:** `checks[]` (each: `check`, `passed`, `source_count`, `spg_count`, `details`)

---

### `conversion/deploy_report.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `deploy_views.py`, updated by `repair_procedures.py` | |
| **Read by** | `html_report.py` (fallback when `migration_state.json` views section is empty) | |

```json
{
  "succeeded": ["schema.viewname", ...],
  "failed": [{"view": "schema.name", "file": "path.sql", "error": "..."}],
  "skipped": ["schema.viewname"]
}
```

---

### `conversion/procedures_deploy_report.json`

| Field | Writer | Reader |
|-------|--------|--------|
| **Written by** | `deploy_procedures.py`, updated by `repair_procedures.py` | |
| **Read by** | `html_report.py` (via `migration_state.json` sync) | |

```json
{
  "succeeded": ["schema.procname", ...],
  "failed": [{"procedure": "schema.name", "file": "path.sql", "error": "...", "resolution": "platform_limitation"}]
}
```

---

### `conversion/functions_deploy_report.json`

Same structure as procedures_deploy_report.json with `"function"` key instead of `"procedure"`.

---

## Intermediate Files (NOT read by report)

These are working files used between phases but NOT directly by the report generator:

| File | Purpose |
|------|---------|
| `ddl_objects.json` | Raw extraction output (Phase 3) |
| `dep_graph.json` | Topological sort for deployment order |
| `conversion/_conversion_report.json` | Conversion metrics (informational) |
| `conversion/fix_report.json` | fix_views.py transformation log |
| `conversion/repair_report.json` | LLM repair accumulator |
| `witness/seed_report.json` | Seeding results |
| `witness/validation_chains.json` | Chain validation results |

---

## Rules

1. **Single writer per file.** If two scripts need to update the same file, one must call the other or use a shared helper function.
2. **Report reads only authoritative files.** Never add a `_load_json()` call to the report for an intermediate file.
3. **Format changes require contract test updates.** If you change a schema here, update `tests/test_workspace_contract.py`.
4. **Fail loud.** Every writer must assert its output file exists and has the required keys before exiting.
