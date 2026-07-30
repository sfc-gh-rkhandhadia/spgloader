# spgloader

**Migrate your database to Snowflake Postgres — fully automated, guided, and AI-assisted.**

`spgloader` is a Cortex Code skill that migrates SQL Server, MySQL, MariaDB, and Oracle databases into Snowflake Postgres (SPG). It handles every step of the migration: extracting your schema, converting SQL dialects, deploying objects in parallel, and validating results. An AI-powered repair loop automatically fixes procedures and views that fail to compile.

---

## What it does

spgloader takes your source database and produces a fully migrated, validated Snowflake Postgres instance:

| Input | → | Output |
|---|---|---|
| SQL Server / MySQL / MariaDB / Oracle | | Tables with types, indexes, and foreign keys |
| Live connection or DDL file | | Views converted to PostgreSQL SQL |
| Docker or SPCS source container | | Functions and stored procedures in PL/pgSQL |
| | | Parity and equivalence test reports |
| | | HTML migration report with KPIs |

---

## Supported sources

| Source | Versions | Schema extraction | Data copy |
|---|---|---|---|
| **Microsoft SQL Server** | 2017, 2019, 2022 | Catalog (`sys.*`) | Yes |
| **MySQL** | 5.7, 8.0 | Catalog (`INFORMATION_SCHEMA`) | Yes |
| **MariaDB** | 10.6, 10.11 | Catalog (`INFORMATION_SCHEMA`) | Yes |
| **Oracle** | 19c, 21c, 23c | Catalog (`ALL_*`) | Yes |
| **DDL file** (SSMS export, any source) | — | Text parsing | No (schema-only) |

---

## Migration phases

spgloader runs as a guided conversation. You answer a few setup questions, then it executes these phases automatically:

| Phase | What happens |
|---|---|
| **Phase 0** | Gather source type, version, connection method, and SPG target |
| **Phase 1** | Deploy source database in Docker or connect to an existing instance |
| **Phase 2** | Provision or connect to a Snowflake Postgres instance |
| **Phase 3** | Extract the full schema from the source catalog |
| **Phase 3.5** | SPG compatibility assessment — blocks on hard incompatibilities |
| **Phase 3.6** | Review any deprecated legacy objects (ASP.NET, Fivetran, etc.) |
| **Phase 4** | Convert views, functions, stored procedures, and triggers |
| **Phase 5** | Deploy to SPG: tables first, then views, functions, and procedures |
| **Phase 6** | Validate: row counts and schema spot-checks |
| **Phase 6.5** | Witness validation: seed data + confirm objects return rows on source |
| **Phase 6.6** | Parity testing: run same queries on SPG, compare results |

---

## What you get

After a completed migration:

```
$WORKSPACE/
├── migration_report.html          ← Full HTML report with all tabs
├── deployment/deployment_summary.json
├── conversion/deploy_report.json
├── witness/validation_chains.json
├── parity/parity_results.json
└── validation_exec/
    ├── comparison_report.txt
    └── Acuity_Migration_Validation_Report.pptx   ← PowerPoint sign-off
```

**Migration report tabs:**
- **Overview** — KPI cards: tables, views, functions deployed; pass rate
- **Deployment** — Per-object deploy results with pass/fail/skip breakdown
- **Migration Summary** — Full table-by-table results split by type
- **Validation** — Schema validation checks
- **Witness** — Source-side execution results (views/procedures returning rows)
- **Equivalence** — Structural comparison between source and SPG
- **Parity** — Execution parity: result-set hashing on both sides

---

## How to invoke it

Type the following in Cortex Code:

```
/spgloader
```

spgloader will ask you:
1. Source database type (SQL Server / MySQL / MariaDB / Oracle)
2. Source version
3. How to connect (live connection / DDL file)
4. Container platform (Docker / SPCS / none)
5. SPG target (new instance or existing)

### Example prompts

**Start a fresh migration:**
```
/spgloader fresh migrate
```

**Describe your source:**
```
I have a SQL Server 2022 database at db.internal:1433, database ProductionDB.
Migrate it to a new STANDARD_M Snowflake Postgres instance.
```

```
Migrate our MySQL 8.0 database to SPG using a DDL file at ~/exports/schema.sql
```

**Run repair on failed procedures after a migration:**
```
run repair on failed procedures
```

**Check what's deployed:**
```
what objects are currently deployed in SPG?
```

**Run validation:**
```
run the validation
```

**Tear down after migration:**
```
teardown docker and drop SPG
```

---

## Requirements

- **Cortex Code** desktop (CoCo)
- **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/)
- **Docker Desktop** (for source database container — required for DDL file migrations)
  - Or **SPCS** (Snowpark Container Services) if Docker is not available locally
- **Snowflake account** with Snowflake Postgres enabled
- **Cortex AI** enabled on your Snowflake account (for LLM-powered repair)

---

## Migration scale

spgloader has been tested on production-scale schemas:

- **1,400+ tables** with IDENTITY columns, composite PKs, 1,200+ foreign keys
- **70+ views** including OUTER APPLY, PIVOT, and complex JOINs
- **20+ scalar functions** with inter-function dependencies
- **100+ stored procedures** including ASP.NET Membership and business logic

**Typical performance at 8 parallel workers:** ~3 minutes for 1,400 tables.
**LLM repair success rate:** resolves ~80% of initially-failing stored procedures automatically.

---

## Key capabilities

| Capability | Detail |
|---|---|
| **Catalog-based extraction** | Reads directly from `sys.columns`, `INFORMATION_SCHEMA`, or `ALL_*` — not DDL text parsing — for accurate type mapping, FK detection, and IDENTITY columns |
| **Parallel deployment** | 8-worker concurrent table/index/FK creation |
| **Dialect conversion** | T-SQL, MySQL, PL/SQL → PostgreSQL DDL and PL/pgSQL |
| **AI-powered repair** | Rule-based fix pass followed by Snowflake Cortex LLM repair for remaining failures |
| **Deprecated object detection** | Identifies legacy frameworks (ASP.NET, Fivetran, etc.) and prompts you before skipping or migrating |
| **Witness validation** | Seeds synthetic data into the source, confirms every view and procedure returns rows, then replicates the test on SPG |
| **Execution parity** | Executes objects on both sides with identical parameters, hashes result sets, and generates a PowerPoint sign-off deck |
| **Full HTML report** | Self-contained report with 7 tabs covering deployment, validation, witness, and parity results |

---

## Learn more

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Internal design, file layout, rule configuration, and extension guide for developers
