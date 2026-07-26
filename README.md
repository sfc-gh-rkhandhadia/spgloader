# spgloader

**Multi-source database migration skill for Snowflake Postgres (SPG)**

`spgloader` is a Cortex Code skill that migrates schemas from Microsoft SQL Server, MySQL, MariaDB, and Oracle into Snowflake Postgres. It extracts the full schema from the source database catalog, converts all objects to PostgreSQL DDL, deploys them in parallel, and validates the result. A two-phase LLM repair loop (rule-based + Snowflake Cortex AI) automatically fixes stored procedures that fail to compile.

---

## What it does

```
Source Database          spgloader              Snowflake Postgres
───────────────    ─────────────────────────    ─────────────────
MSSQL / MySQL   ─► Catalog extraction       ─►  Tables + Indexes
MariaDB         ─► DDL conversion           ─►  Views
Oracle          ─► Parallel deploy (8×)     ─►  Functions
(DDL file)      ─► Rule-based repair        ─►  Stored Procedures
                ─► LLM repair (Cortex AI)   ─►  Sequences
                ─► Validation (6 checks)    ─►  [PASS / FAIL report]
```

### Key capabilities

| Capability | Description |
|---|---|
| **Catalog-based extraction** | Reads `sys.columns`, `sys.indexes`, `sys.identity_columns` directly — no DDL file parsing |
| **Parallel deployment** | 8-worker parallel table/index/FK creation; 1,493 tables in ~3 minutes |
| **T-SQL → PL/pgSQL converter** | Converts stored procedures: IF/BEGIN/END structure, variable declarations, type mapping |
| **LLM repair loop** | Phase 1: rule-based fixes (plpgsql-fixes.yaml). Phase 2: Snowflake Cortex `llama3.3-70b` with original T-SQL + error message, up to 3 iterations |
| **Legacy group detection** | Identifies `aspnet_*`, `sp_fivetran_*` and other legacy procedure groups, prompts user before deploying |
| **Auto schema prefix** | `search_path` set automatically from workspace; unqualified table refs in views auto-retried with schema prefix |
| **6-check validation** | Table count, column counts, primary keys, IDENTITY columns, foreign keys, indexes |
| **Generic rules** | No project-specific names hardcoded; all fixes driven by YAML rule files |

---

## Architecture

```
spgloader/
├── SKILL.md                          # Skill entry point (Cortex Code)
├── scripts/
│   ├── parallel_deploy.py            # Phase 3: tables + indexes + FKs (8 workers)
│   ├── deploy_views.py               # Views with auto search_path + retry
│   ├── deploy_functions.py           # Functions in dependency order
│   ├── deploy_procedures.py          # Procedures with legacy-group detection
│   ├── convert_procedures.py         # T-SQL → PL/pgSQL converter
│   ├── fix_procedures.py             # Rule-based procedure fixer
│   ├── repair_procedures.py          # Two-phase LLM repair loop
│   ├── fix_views.py                  # View syntax corrections (YAML-driven)
│   ├── patch_views.py                # Generic T-SQL→PG view patches
│   ├── validate_migration.py         # 6-check validation harness
│   └── load_source_ddl.py            # DDL file → Docker SQL Server loader
├── lib/spgloader/
│   ├── connectors/                   # MSSQL / MySQL / MariaDB / Oracle connectors
│   └── conversion/
│       └── pg_generator.py           # Source-agnostic PostgreSQL DDL generator
├── references/
│   ├── rules/mssql-to-pg/
│   │   ├── plpgsql-fixes.yaml        # PL/pgSQL body transformation rules
│   │   ├── ddl-cleanup.yaml          # T-SQL DDL artifact removal rules
│   │   ├── type-mappings.yaml        # Source type → PG type maps
│   │   └── function-substitutions.yaml  # T-SQL function → PG function maps
│   ├── fix-mappings/
│   │   └── view-fixes.yaml           # Generic view conversion config (auto_detect: true)
│   ├── prompts/
│   │   └── procedure-repair-prompt.md  # LLM repair prompt template
│   ├── legacy-proc-rules.yaml        # Legacy procedure group definitions
│   └── llm-repair-config.yaml        # Cortex model + iteration config
└── sub-skills/
    ├── source-setup/SKILL.md         # Docker / SPCS source DB setup
    ├── ddl-extract/SKILL.md
    ├── assess/SKILL.md
    ├── convert/SKILL.md
    ├── deploy/SKILL.md
    └── validate/SKILL.md
```

---

## Example prompts

### Start a migration

```
/spgloader
```

The skill walks through an interactive setup:
- Source type (MSSQL / MySQL / MariaDB / Oracle)
- Source version
- Container platform (Docker / SPCS / DDL file only)
- Target SPG instance (new or existing)

---

### Migrate a specific SQL Server database

```
I have a SQL Server 2022 database and want to migrate it to Snowflake Postgres.
The DDL file is at ~/Documents/MyDB/schema.sql
```

```
Migrate our MSSQL 2019 database to SPG. Source host is db.internal:1433,
database name is ProductionDB. Deploy a new STANDARD_XL SPG instance.
```

---

### Migrate views and stored procedures

```
migrate views and functions, stored procedures and rest of the objects also check constraints
```

```
deploy aspnet and sp_fivetran legacy procedures
```

---

### Run the LLM repair loop on failed procedures

```
run repair_procedure
```

```
run repair_procedures with model mistral-large2 and 5 iterations
```

---

### Fresh migration (drop and rebuild)

```
drop all the objects in SPG and run a fresh migration
```

```
drop all the objects in spg and run the conversion including legacy code asp and fivetran
```

---

### Check what's deployed

```
what objects are currently deployed in SPG?
```

```
run the validation
```

---

### Fix specific failure patterns

```
for procedures add a rule if you find objects that are legacy prompt user if they would like to deploy them
```

```
we should have rules to address complex procedures, add llm-iteration repair loop
```

---

## Configuration files

### `references/llm-repair-config.yaml`
```yaml
model: llama3.3-70b       # Cortex model for procedure repair
max_iterations: 3          # Repair attempts per failed procedure
temperature: 0.1           # Low temp for deterministic code
warehouse: COMPUTE_WH
```

### `references/legacy-proc-rules.yaml`
```yaml
rules:
  - label: aspnet
    description: ASP.NET Membership provider procedures
    patterns: ['^aspnet_']
  - label: sp_fivetran
    description: Fivetran CDC/replication procedures
    patterns: ['^sp_fivetran_']
  - label: index_maintenance
    description: Index rebuild/reorganize procedures
    patterns: ['^rebuildindexes$', '^reorgindexes$']
```

### `references/fix-mappings/view-fixes.yaml`
```yaml
schema_prefix:
  auto_detect: true   # Auto-detects unqualified table refs from ddl_objects.json
  extra_tables: []    # Optional per-project overrides

pattern_fixes:
  tsql_string_concat: true    # + → ||
  dateadd: true               # DATEADD → interval arithmetic
  outer_apply: true           # handled by patch_views.py
  ...
```

---

## Example migration scale

spgloader has been tested against schemas with:
- **1,400+ tables** with complex constraints (IDENTITY, composite PKs, temporal tables)
- **70+ views** including OUTER APPLY, PIVOT, and TOP N PERCENT patterns
- **20 scalar functions** with inter-function dependencies
- **100+ stored procedures** including ASP.NET Membership, CDC, and business logic

Typical deployment times at 8 workers: ~3 minutes for 1,400 tables.
LLM repair loop (Cortex `llama3.3-70b`) resolves ~80% of initially-failing procedures.

**Validation checks:** table count, column counts, primary keys, IDENTITY columns, foreign keys, indexes.

---

## Requirements

- Python 3.10+
- `uv` (Python package manager)
- Docker (for source database container) or SPCS
- Snowflake Postgres instance with `POSTGRES_INGRESS` network policy
- For LLM repair: Snowflake account with Cortex enabled

---

## Quick start (DDL file workflow)

```bash
# 1. Start migration skill
/spgloader

# 2. Answer: MSSQL → Deploy in Docker → Provision new SPG → Provide DDL as file

# 3. After migration, run repair on failed procedures
python scripts/repair_procedures.py \
  --work-dir ~/.spgloader/<timestamp> \
  --spg-service <your-spg-service>

# 4. Validate
python scripts/validate_migration.py \
  --source-type mssql --source-host localhost --source-db mydb \
  --source-user sa --password-env SAPASSWORD \
  --spg-service <your-spg-service> --source-schema dbo --catalog
```
