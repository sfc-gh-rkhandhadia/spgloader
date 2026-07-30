# spgloader — Architecture

Technical reference for contributors and advanced users. For end-user guidance, see [`README.md`](README.md).

---

## Repository layout

```
spgloader/
├── SKILL.md                                # Cortex Code skill entry point
├── sub-skills/
│   ├── source-setup/SKILL.md               # Phase 1: Docker / SPCS / existing source
│   ├── target-setup/SKILL.md               # Phase 2: SPG provisioning / connection
│   ├── ddl-extract/SKILL.md                # Phase 3: catalog extraction
│   ├── assess/SKILL.md                     # Phase 3.5: SPG compatibility guardrail
│   ├── convert/SKILL.md                    # Phase 4: object conversion routing
│   ├── deploy/SKILL.md                     # Phase 5: deployment pipeline
│   ├── validate/SKILL.md                   # Phase 6: row-count + schema validation
│   ├── witness-validate/SKILL.md           # Phase 6.5 / 6.6: witness + parity
│   └── execution-parity/SKILL.md          # Execution parity sub-skill
│
├── scripts/                                # CLI scripts (thin wrappers over lib/)
│   ├── extract_ddl.py                      # Phase 3: catalog extraction entry point
│   ├── parse_ddl.py                        # DDL text parser (fallback / witness use)
│   ├── assess.py                           # Phase 3.5: SPG compatibility scanner
│   ├── analyze_deprecated.py               # Phase 3.6: deprecated pattern detector
│   ├── build_dep_graph.py                  # Topological sort of DDL objects
│   ├── convert_objects.py                  # Phase 4B: view/proc/fn/trigger converter
│   ├── fix_views.py                        # View syntax corrections (YAML-driven)
│   ├── fix_functions.py                    # Function syntax corrections
│   ├── parallel_deploy.py                  # Phase 4A: catalog-based table deploy (8 workers)
│   ├── deploy_views.py                     # View deployment with dep ordering
│   ├── deploy_functions.py                 # Function deployment
│   ├── deploy_procedures.py                # Procedure deployment + legacy group filter
│   ├── repair_procedures.py                # LLM repair loop (rule + Cortex AI)
│   ├── copy_source_data.py                 # Data copy: MSSQL/MySQL → SPG
│   ├── copy_oracle_data.py                 # Data copy: Oracle → SPG
│   ├── load_source_ddl.py                  # DDL file → Docker source DB loader
│   ├── generate_report.py                  # HTML report generator alias
│   ├── update_deploy_reports.py            # Multi-DB deploy report merger
│   │
│   ├── witness/                            # Phase 6.5 witness scripts
│   │   ├── parse_ddl.py                    # DDL → object_inventory.json
│   │   ├── discover_spg_constraints.py     # SPG CHECK constraint reader
│   │   ├── build_dep_graph.py              # Dep graph + SPG constraints
│   │   ├── seed_data.py                    # Synthetic 3-row seed generator
│   │   ├── validate_chains.py              # Source-side view/proc execution
│   │   └── adaptive_seed.py                # MySQL adaptive parameter inference
│   │
│   ├── parity/                             # Phase 6.6 structural parity
│   │   ├── full_validation.py              # MSSQL/MSSQL+SPG structural parity
│   │   ├── mysql_structural_parity.py      # MySQL+SPG structural parity
│   │   ├── source_adapter.py               # Dialect-agnostic catalog adapter
│   │   └── generate_validation_markdown.py
│   │
│   └── execution-parity/                   # Phase 6.6 execution parity
│       ├── setup_validation_tables.sql     # SPG audit schema setup
│       ├── load_source_to_spg.py           # Seed data copy source→SPG
│       ├── source_proc_executor.py         # Source-side proc/fn execution
│       ├── spg_proc_executor.py            # SPG-side proc/fn execution
│       ├── compare_proc_outputs.py         # Result-set hash comparison + audit write
│       ├── validate_batch.py               # View count + column comparison
│       └── generate_migration_report.py    # PowerPoint sign-off generator
│
├── lib/spgloader/                          # Importable Python package
│   ├── connectors/
│   │   ├── base.py                         # Abstract connector + DDL parsing
│   │   ├── mssql.py                        # pymssql connector
│   │   ├── mysql.py                        # mysql-connector-python connector
│   │   ├── oracle.py                       # oracledb connector
│   │   └── __init__.py                     # get_connector() factory
│   ├── conversion/
│   │   ├── dep_graph.py                    # Kahn's topological sort
│   │   ├── ewi.py                          # 21 SPG EWI annotation codes
│   │   └── pg_generator.py                 # Source-agnostic PostgreSQL DDL generator
│   ├── deployment/
│   │   └── spg.py                          # psycopg2 deployment via pg_service.conf
│   ├── reporting/
│   │   ├── assessment.py                   # SPGCompatibilityAssessment.scan()
│   │   └── html_report.py                  # Self-contained HTML report builder
│   └── workspace.py                        # Per-project workspace contract
│
└── references/
    ├── rules/
    │   ├── mssql-to-pg/
    │   │   ├── plpgsql-fixes.yaml          # PL/pgSQL body transformation rules
    │   │   ├── ddl-cleanup.yaml            # T-SQL DDL artifact removal
    │   │   ├── type-mappings.yaml          # Source type → PG type maps
    │   │   └── function-substitutions.yaml # T-SQL function → PG function maps
    │   ├── mysql-to-pg/                    # MySQL-specific rules
    │   └── shared/                         # Source-agnostic rules
    ├── fix-mappings/
    │   └── view-fixes.yaml                 # Generic view conversion config
    ├── prompts/
    │   ├── procedure-repair-prompt.md      # MSSQL LLM repair prompt
    │   └── procedure-repair-oracle-prompt.md
    ├── docker-templates/
    │   ├── mssql-compose.yml
    │   ├── mysql-compose.yml
    │   └── oracle-compose.yml
    ├── spg-compatibility.md                # SPG rule set (BLOCK / WARN / RESOLVE)
    ├── ewi-codes.md                        # EWI annotation code catalog
    ├── llm-repair-config.yaml              # Cortex model + iteration config
    └── legacy-proc-rules.yaml             # Legacy procedure group definitions
```

---

## Pipeline overview

```
Phase 0  Gather inputs (source type, connection method, SPG target)
   │
Phase 1  Deploy source DB in Docker (mssql/mysql/oracle-compose.yml)
         OR connect to existing instance
   │
Phase 2  Provision SPG (pg_connect.py --create) or connect to existing
         Attach POSTGRES_INGRESS network policy
   │
Phase 3  extract_ddl.py
         ├── MSSQL: sys.tables + sys.columns + sys.indexes + sys.foreign_keys
         ├── MySQL: INFORMATION_SCHEMA.TABLES/COLUMNS/KEY_COLUMN_USAGE
         ├── Oracle: ALL_TABLES/COLUMNS/CONSTRAINTS/INDEXES
         └── DDL file: parse_ddl.py text parser (fallback)
         → ddl_objects.json  + dep_graph.json
   │
Phase 3.5  assess.py
           Scans ddl_objects.json against spg-compatibility.md rules
           → BLOCK (hard stop) / WARN (confirm) / RESOLVE (extension prereq)
   │
Phase 3.6  analyze_deprecated.py --non-interactive
           Detects legacy groups (aspnet_*, sp_fivetran_*, etc.)
           → ask_user_question for each group: skip | migrate | modernize
           → deprecated_review.json
   │
Phase 4A  parallel_deploy.py (8 workers)
           MSSQL/MySQL/Oracle catalog → PostgreSQL DDL via pg_generator.py
           Deploy order: schemas → sequences → tables → indexes → FKs
   │
Phase 4B  convert_objects.py (views, functions, procedures, triggers)
           Rule-based conversion: type-mappings.yaml, function-substitutions.yaml
           fix_views.py (6 YAML-driven passes)
           fix_functions.py
   │
Phase 5   deploy_views.py    (dependency-ordered)
          deploy_functions.py
          deploy_procedures.py  (legacy-group filter from deprecated_review.json)
          ↓ failures → repair_procedures.py (rule pass → Cortex LLM pass)
   │
Phase 5.5  FIX-REQUIRED check (Step 4.5 in deploy SKILL.md)
           Views pre-marked by fix_views.py → LLM repair attempt
   │
Phase 6   extract_ddl.py --count-only  (source row counts)
          deploy_to_spg.py --count-tables (SPG row counts)
          → validation_report.json
   │
Phase 6.5  Witness validation
           seed_data.py → 3 synthetic rows per table (respects FK order)
           validate_chains.py → executes every view/proc on source
           → validation_chains.json
   │
Phase 6.6  Structural parity
           full_validation.py (MSSQL) or mysql_structural_parity.py (MySQL)
           → parity_results.json
           ↓
           ask_user_question: execution parity? (MANDATORY STOPPING POINT)
           ↓ yes
           Execution parity sub-skill:
             load_source_to_spg.py → source_proc_executor.py → spg_proc_executor.py
             → compare_proc_outputs.py → validation.validation_result (audit table)
             → generate_migration_report.py → Migration_Validation_Report.pptx
   │
html_report.py → migration_report.html (7-tab self-contained report)
```

---

## Key design decisions

### Catalog-based extraction (not DDL text parsing)

For live source connections, schema is extracted directly from the source catalog
(`sys.columns`, `INFORMATION_SCHEMA.COLUMNS`, `ALL_COLUMNS`). This gives:
- Accurate base types (INT vs SMALLINT vs TINYINT)
- IDENTITY/AUTO_INCREMENT column detection without regex
- Foreign key relationships with exact column mappings
- Computed column detection

DDL text parsing (`parse_ddl.py`) is used only when a live source is unavailable
(DDL file migrations) or for the witness sub-skill's object inventory.

### pg_generator.py — source-agnostic DDL generator

All source types share a single PostgreSQL DDL generator (`lib/spgloader/conversion/pg_generator.py`).
Each connector (`mssql.py`, `mysql.py`, `oracle.py`) normalizes catalog data into a
common intermediate format; `pg_generator.py` emits PostgreSQL DDL from that format.

Type mapping is driven by `references/rules/*/type-mappings.yaml` and loaded at runtime
so it can be updated without code changes.

### Two-phase LLM repair

`repair_procedures.py` runs two passes on each failed object:

1. **Rule pass** — applies `plpgsql-fixes.yaml` transformations (regex substitutions, keyword
   replacements, structural rewrites). Fast and deterministic.
2. **LLM pass** — if the rule pass doesn't fix the compile error, sends the original source SQL
   + the PG compile error to Snowflake Cortex (`claude-sonnet-4-5` by default) with a
   structured repair prompt. Up to `max_iterations` attempts.

Configuration: `references/llm-repair-config.yaml`

```yaml
model: claude-sonnet-4-5
max_iterations: 2
temperature: 0.1
snowflake_connection: <your-connection-name>
```

### EWI annotation system

Conversion scripts annotate output SQL with inline `-- ** SPG-EWI-XXXX **` comments:

| Code | Level | Meaning |
|---|---|---|
| SPG-BLOCK-001…008 | BLOCK | Hard incompatibility — migration cannot proceed |
| SPG-WARN-001…008 | WARN | Requires user acknowledgement before continuing |
| SPG-EWI-0001 | INFO | T-SQL-specific syntax replaced |
| SPG-EWI-0004 | INFO | Procedure/function body converted |
| SPG-EWI-0005 | INFO | Trigger restructured as trigger function |
| SPG-EWI-0011 | INFO | Complex pattern (PIVOT, APPLY) — may need review |

Full catalog: `references/ewi-codes.md`

### Workspace contract

Each migration runs in an isolated workspace directory written by `lib/spgloader/workspace.py`.
Phase outputs are files; downstream phases read those files. This makes phases individually
restartable and the workspace inspectable.

Key files:
```
$WORKSPACE/
├── source_conn.env          # Source connection details (no passwords)
├── target_conn.env          # SPG connection details
├── ddl_objects.json         # All extracted objects with DDL text
├── dep_graph.json           # Topologically sorted object list
├── assessment/assessment_summary.json
├── deprecated/deprecated_review.json
├── deployment/deployment_summary.json
├── conversion/deploy_report.json
├── witness/validation_chains.json
└── parity/parity_results.json
```

### Multi-database MySQL migrations

MySQL migrations where the source has multiple databases (schemas in PG terms) are handled
by running each phase once per database and merging the results:

- `ddl_objects.json` — flat list across all databases; `schema` field = database name
- `parallel_deploy.py` — creates one PG schema per MySQL database
- `validate_chains.py` — run once per database, writing `chains_report_{db}.json`;
  merged into `validation_chains.json` by the skill
- `mysql_structural_parity.py` — takes `--databases "db1,db2,..."` comma-separated list

---

## Configuration reference

### `references/llm-repair-config.yaml`

```yaml
model: claude-sonnet-4-5      # Snowflake Cortex model
max_iterations: 2              # LLM repair attempts per object
temperature: 0.1               # Low temperature for code generation
snowflake_connection: ""       # Snowflake connection name (reads from connections.toml)
oracle_prompt_template: references/prompts/procedure-repair-oracle-prompt.md
```

### `references/legacy-proc-rules.yaml`

Defines deprecated/legacy procedure groups detected in Phase 3.6:

```yaml
rules:
  - label: aspnet_membership
    description: "ASP.NET Membership Provider — deprecated since 2013"
    patterns: ['^aspnet_', '^vw_aspnet_']
    severity: advisory
    recommendation: "Skip stored procedures and schemas; migrate underlying data tables."

  - label: sp_fivetran
    description: "Fivetran CDC/replication procedures"
    patterns: ['^sp_fivetran_']
    severity: advisory

  - label: index_maintenance
    description: "Index rebuild/reorganize maintenance procedures"
    patterns: ['^rebuildindexes$', '^reorgindexes$']
    severity: advisory
```

To add a new deprecated group, add an entry to this file. No code changes required.

### `references/fix-mappings/view-fixes.yaml`

Controls the 6 passes of `fix_views.py`:

```yaml
schema_prefix:
  auto_detect: true            # Infer unqualified table refs from ddl_objects.json
  extra_tables: []             # Override: force-qualify specific tables

pivot_rules: []                # PIVOT column lists for auto-conversion (optional)

pattern_fixes:
  tsql_string_concat: true     # + → || for string concat
  dateadd: true                # DATEADD(part, n, col) → interval arithmetic
  outer_apply: true            # OUTER APPLY → LEFT JOIN LATERAL
  isnull: true                 # ISNULL(a, b) → COALESCE(a, b)
  boolean_int: true            # col = 1 → col = true (for BIT columns)
  nolock_hints: true           # WITH (NOLOCK) removal
  go_separators: true          # GO batch separator removal
```

---

## Adding a new source type

1. Add a connector in `lib/spgloader/connectors/<source>.py` implementing the `BaseConnector`
   interface (see `base.py`).
2. Register it in `lib/spgloader/connectors/__init__.py`'s `get_connector()` factory.
3. Add rule files under `references/rules/<source>-to-pg/`.
4. Update `convert_objects.py` to route `--source-type <source>` to the new converter.
5. Add a Docker Compose template in `references/docker-templates/<source>-compose.yml`.
6. Add source type to the Phase 0 `ask_user_question` options in `SKILL.md`.

---

## SPG compatibility guardrail (Phase 3.5)

`assess.py` scans `ddl_objects.json` against rules in `references/spg-compatibility.md`
before any conversion or deployment occurs.

**BLOCK findings** halt the migration. Common causes:
- CLR assemblies (no equivalent in SPG)
- Linked server references (`OPENQUERY`, `OPENDATASOURCE`)
- `ALTER SYSTEM` / `pg_catalog` modification
- Language other than PL/pgSQL (`PL/Python`, `PL/Perl`)

**WARN findings** require user confirmation but do not halt migration.

**RESOLVE findings** generate `assessment/pre_deploy_extensions.sql` automatically
(e.g., `CREATE EXTENSION IF NOT EXISTS "uuid-ossp"`).
