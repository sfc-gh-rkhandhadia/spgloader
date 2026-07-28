---
name: spgloader
description: "Migrate MSSQL, MySQL, MariaDB, or Oracle databases to Snowflake Postgres (SPG).
  Includes a mandatory SPG Compatibility Assessment guardrail that scans DDL
  against Snowflake Postgres-specific rules before conversion begins.
  Handles source environment setup (existing, Docker, or SPCS), SPG provisioning,
  catalog-based schema extraction, SPG compatibility assessment,
  catalog-driven parallel table deployment, LLM-based object conversion with EWI
  annotations, deployment, and validation.
  Triggers: migrate to snowflake postgres, spgloader, mssql to spg,
  mysql to spg, mariadb to spg, oracle to spg, database migration to postgres,
  source ddl to snowflake postgres, convert mssql mysql mariadb oracle to postgres."
---

# spgloader — Multi-Source to Snowflake Postgres Migration

## Overview

spgloader migrates **MSSQL, MySQL, MariaDB, or Oracle** databases to Snowflake Postgres (SPG).

For **tables and schemas** the skill uses a **catalog-based extractor** — reading `sys.*`,
`INFORMATION_SCHEMA.*`, or `ALL_*` views directly from the live source database.
This gives accurate type mapping, IDENTITY/AUTO_INCREMENT detection, foreign keys, indexes,
and sequences without fragile DDL text parsing.

For **views, procedures, functions, and triggers** the skill extracts the raw source SQL and
applies a rule-based + LLM conversion pipeline.

A **mandatory SPG Compatibility Assessment** (Phase 3.5) blocks migration on hard incompatibilities.
See `references/migration-overview.md` for conversion paths, source support matrix, and Phase 3.6 detail.

## Skill Directory

`<SKILL_DIR>` = absolute path to this skill: `~/sko-coco/spgloader`

## Phase 0 — Gather Environment Info

Ask the user with a single `ask_user_question` call (4 questions):

1. **Source DB type** (options): MSSQL | MySQL | MariaDB | Oracle
2. **Source DB version** (text): default per type: MSSQL → `2022`, MySQL → `8.0`, MariaDB → `10.11`, Oracle → `23c`
3. **Source input** (options): "Connect to live source database" | "Provide DDL file" | "Paste DDL directly"
4. **Target SPG** (options): "Use existing SPG instance" | "Provision new SPG"

Store answers as:
- `SOURCE_TYPE` — mssql | mysql | mariadb | oracle
- `SOURCE_VERSION` — version string
- `SOURCE_INPUT` — live | file | paste
- `TARGET_SPG` — existing | new

### Phase 0B — Source environment (if SOURCE_INPUT = live)

Ask one more question:
- **Source environment** (options): "Use existing environment" | "Deploy in Docker"

Store as `SOURCE_ENV` — existing | docker

### Phase 0C — Container platform choice (if SOURCE_INPUT = file or paste)

When the source is a DDL file (not a live connection), we need to load it into a running
database to enable catalog-based extraction (FK, indexes, IDENTITY columns, etc.).

**Note on SSMS exports:** SQL Server Management Studio "Scripts and Tables" exports produce
a **directory** of individual `.sql` files (one per object) encoded as **UTF-16 LE**
(BOM `\xff\xfe`). `load_source_ddl.py --ddl-dir` handles this automatically:
it detects the encoding, strips `USE [dbname]` statements, sorts files in
dependency order (Schema → Table → Function → View → Procedure), combines them,
and runs a second FK pass to resolve forward-reference failures.

Ask the user:

```
A DDL file provides schema structure only. For the most accurate migration
(foreign keys, indexes, IDENTITY columns), we can load it into a source
database container and extract directly from the catalog.

Which container platform is available?
  Docker  — deploy a local container on this machine (recommended)
  SPCS    — deploy on Snowpark Container Services (Snowflake-hosted)
  Neither — use Python text-based conversion (known gaps: FKs and indexes
            will not be migrated; IDENTITY detection via regex only)
```

Store as `CONTAINER_PLATFORM` — docker | spcs | none

**If CONTAINER_PLATFORM = docker or spcs:**
- Set `SOURCE_ENV = docker` or `SOURCE_ENV = spcs`
- Phase 1 will deploy the source DB container and load the DDL file **or directory**
- Use `--ddl-dir` when the source is an SSMS export directory; `--ddl-file` for a single combined file
- Phase 3 will extract from the live catalog

**If CONTAINER_PLATFORM = none:**
- Set `SOURCE_ENV = none` (text-based fallback)
- Phase 3 will use the DDL file directly with regex text conversion
- Annotate the output with EWI-WARN-NO-CATALOG warnings

## Shared Workspace

**Prompt the user for their preferred workspace directory** before initializing.
Use `ask_user_question` with `type: text` and a sensible default:

```
header:       "Workspace directory"
question:     "Where should spgloader store migration workspace files
               (DDL extracts, converted SQL, logs, reports)?"
defaultValue: "~/.spgloader/<timestamp>"
```

If the user accepts the default or provides a path, initialize:

```bash
# Use user-provided path, or generate a timestamped default
SPGLOADER_WORK_DIR="${USER_WORK_DIR:-$HOME/.spgloader/$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$SPGLOADER_WORK_DIR"
echo "Working directory: $SPGLOADER_WORK_DIR"
```

All phases read and write through files in this directory. See the workspace contract in
`lib/spgloader/workspace.py`.

## Phase Routing

Execute phases in order. Load each sub-skill, execute its full workflow, then continue.

| Phase | Sub-skill | Description |
|-------|-----------|-------------|
| 1 | `sub-skills/source-setup/SKILL.md` | Connect or deploy source DB (Docker or SPCS) |
| 2 | `sub-skills/target-setup/SKILL.md` | Connect or provision SPG |
| 3 | `sub-skills/ddl-extract/SKILL.md` | Extract schema (catalog or file) + build dep graph |
| **3.5** | **`sub-skills/assess/SKILL.md`** | **SPG Compatibility Assessment (GUARDRAIL)** |
| **3.6** | **`scripts/analyze_deprecated.py`** | **Deprecated Object Review (DECISION GATE)** |
| 4 | `sub-skills/convert/SKILL.md` | Convert non-table objects (LLM, EWI-annotated) |
| 5 | `sub-skills/deploy/SKILL.md` | Deploy to SPG (catalog-based parallel for tables; DDL for rest) |
| 6 | `sub-skills/validate/SKILL.md` | Row counts + schema spot checks |
| **6.5** | **`sub-skills/witness-validate/SKILL.md`** | **Witness validation: seed synthetic data (Docker/SPCS) + confirm views/procs return rows on source** |
| **6.6** | **`sub-skills/witness-validate/SKILL.md`** | **Parity testing: same queries on SPG, diff results, sign-off report** |

**Phase 6.5 and 6.6 are optional** — the sub-skill asks the user at the start of Phase 6.5 whether to proceed.
If source is an existing customer instance (`SOURCE_ENV=existing`), synthetic seeding is automatically
skipped; only `validate_chains` + parity run against existing data.

**Phase 3.5 is mandatory and cannot be skipped.** If it returns BLOCKED, the migration
halts until the user resolves all BLOCK findings. Phase 4 always checks
`assessment_summary.json` for `is_blocked` before proceeding.

**Phase 3.6 is automatic** — runs immediately after 3.5. If no deprecated patterns are
detected, it completes silently. If patterns are detected, the user is shown each group
and chooses a disposition (skip | migrate | modernize). Phase 4 reads
`deprecated/deprecated_review.json` and excludes objects marked `skip`.

## Phase 3.6 — Deprecated Object Review

See `references/migration-overview.md` for full detail.

```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/analyze_deprecated.py \
  --work-dir "$SPGLOADER_WORK_DIR" [--non-interactive]
```

If patterns are detected the user chooses skip | migrate | modernize per group.
Phase 4 (`convert_objects.py`) reads `deprecated/deprecated_review.json` and skips excluded objects.

## Workspace Output Structure

```
$SPGLOADER_WORK_DIR/
├── .spgloader/
│   ├── config.yaml          — source/target connection details
│   └── manifest.json        — phase completion tracking
├── source/                  — original DDL organized by type
├── assessment/
│   ├── assessment_summary.json   — SPG compatibility findings
│   ├── assessment_report.md      — human-readable report
│   └── pre_deploy_extensions.sql — extension prereqs (if any)
├── deprecated/                   ← Phase 3.6 output
│   ├── deprecated_review.json    — per-group user disposition decisions
│   └── deprecated_report.md      — human-readable report of detected patterns
├── conversion/
│   └── postgres/
│       ├── wave_2_views/
│       ├── wave_2_views_fixed/
│       ├── wave_3_functions/
│       ├── wave_3_functions_fixed/
│       └── wave_4_procedures_triggers/
│   ├── _conversion_report.json
│   ├── deploy_report.json
│   ├── functions_deploy_report.json
│   ├── procedures_deploy_report.json
│   └── repair_report.json
├── deployment/
│   └── deployment_summary.json
├── validation/
│   ├── validation_report.json
│   ├── migration_report.html
│   └── migration_report.pdf  (optional)
├── witness/                         ← Phase 6.5 output
│   ├── object_inventory.json        — parse_ddl output
│   ├── dep_graph.json               — dependency graph + SPG constraints
│   ├── spg_column_constraints.json  — SPG CHECK constraints
│   ├── mssql_deploy_report.json     — bridge from spgloader deploy artifacts
│   ├── seed_report.json             — seeding results (stub if skipped)
│   └── validation_chains.json       — view/proc/fn confirmation results
└── parity/                          ← Phase 6.6 output
    ├── parity_report.md             — sign-off report
    └── migration_signoff.pptx       — PowerPoint sign-off (if requested)
```

## Progress Tracking

Use `system_todo_write` to track phases:
```
Phase 0: Gather info             ← pending / in_progress / completed
Phase 1: Source setup            ← pending / in_progress / completed
Phase 2: Target setup            ← pending / in_progress / completed
Phase 3: DDL extraction          ← pending / in_progress / completed
Phase 3.5: SPG Assessment        ← pending / in_progress / BLOCKED / completed
Phase 3.6: Deprecated Review     ← pending / in_progress / skipped / completed
Phase 4: Conversion              ← pending / in_progress / completed
Phase 5: Deploy                  ← pending / in_progress / completed
Phase 6: Validate                ← pending / in_progress / completed
Phase 6.5: Witness Validation    ← pending / in_progress / skipped / completed
Phase 6.6: Parity Testing        ← pending / in_progress / skipped / completed
```

## Mandatory Stopping Points

| Action | Why | Phase |
|--------|-----|-------|
| BLOCK findings in assessment | Migration incompatible with SPG | 3.5 |
| Docker Oracle image pull | Requires `docker login container-registry.oracle.com` | 1 |
| SPCS compute pool creation | Billable resource | 1 |
| SPG CREATE | Billable resource | 2 |
| Deploy DDL to SPG | Destructive on target | 5 |

## EWI-WARN-NO-CATALOG (text-based fallback)

When `CONTAINER_PLATFORM = none`, the schema is extracted from DDL text using regex
conversions. The following gaps apply and are annotated in the output:

```
[EWI-WARN-NO-CATALOG] Schema extracted from DDL text. Catalog unavailable.
  Known gaps in this migration:
  - Foreign keys: NOT migrated (cannot be reliably parsed from DDL text)
  - Secondary indexes: NOT migrated (not present in DDL text exports)
  - IDENTITY detection: regex-based only (may miss edge cases)
  Recommendation: load the DDL file into a Docker or SPCS container
  and re-run to get full structural fidelity.
  Data copy uses copy_source_data.py (pymssql/mysql-connector over TCP) — pgloader is not used.
```

## Global Safety Rules

- Never ask for or display passwords in chat
- Passwords are passed via environment variables to scripts and Docker Compose
- Never display raw `DESCRIBE POSTGRES INSTANCE` output — contains `access_roles`
- Use `pg_connect.py` from `<SNOWFLAKE_POSTGRES_SKILL_DIR>/scripts/` for all SPG connections
  (resolve `SNOWFLAKE_POSTGRES_SKILL_DIR` at runtime or ask the user if unknown)
- Approval required for all billable and destructive operations

## Final Migration Summary

After all phases complete, display:

```
Migration Complete
=================
Source:       <SOURCE_TYPE> <SOURCE_VERSION> @ <host>:<port>/<database>
Target:       SPG instance <name>
Extraction:   <catalog | text-based (no-catalog)>

SPG Assessment:   PASSED (<N> warnings acknowledged)
Extension prereqs installed: <list or none>

Objects migrated:
  Schemas:             N
  Tables:              N  (catalog-based, <M> workers)
  Foreign keys:        N  (or: NOT migrated — no catalog)
  Indexes:             N  (or: NOT migrated — no catalog)
  Views:               N  (rule conversion + LLM repair)
  Functions:           N  (rule conversion + LLM repair)
  Stored procedures:   N  (rule conversion + LLM repair)

LLM Repair (claude-sonnet-4-5):
  Fixed by rules   : N
  Fixed by LLM     : N
  Still failing    : N  (UDTT types or truncated DDL)

Deployment:   N succeeded / N failed / N skipped
Validation:   N tables match / N FKs match / N indexes match

Report:       <SPGLOADER_WORK_DIR>/migration_report.html
Working dir:  <SPGLOADER_WORK_DIR>
```

Then offer to generate the HTML report:
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/generate_report.py \
  "$SPGLOADER_WORK_DIR" \
  --output "$SPGLOADER_WORK_DIR/migration_report.html"
```

---

## Teardown

When a user asks to tear down the environment (drop SPG + remove Docker source container):

### Step 1 — Read workspace connection info

```bash
source "$SPGLOADER_WORK_DIR/target_conn.env"
# Provides: TARGET_SPG_SERVICE, TARGET_SNOWFLAKE_CONNECTION, TARGET_SNOWFLAKE_ROLE
source "$SPGLOADER_WORK_DIR/source_conn.env"
# Provides: SOURCE_CONTAINER (if Docker was used)
```

### Step 2 — Drop the source environment (Docker or SPCS)

**Docker:**
```bash
if [ -n "$SOURCE_CONTAINER" ]; then
  docker rm -f "$SOURCE_CONTAINER"
fi
```

**SPCS** (if `SPCS_SERVICE` is set in `source_conn.env`):
```bash
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/teardown_spcs_source.py \
  --work-dir "$SPGLOADER_WORK_DIR" --yes
```

This reads `SPCS_SERVICE` and `SPCS_POOL` from `source_conn.env` and drops both resources.

### Step 3 — Drop the SPG instance

**CRITICAL: Use the Snowflake account and role stored at provisioning time.**
The Cortex Code session may be connected to a different account than the one
that owns the SPG instance.  Always use `snow sql -c $TARGET_SNOWFLAKE_CONNECTION`
with `USE ROLE $TARGET_SNOWFLAKE_ROLE` — never assume the current session's
account is correct.

```bash
snow sql \
  -c "$TARGET_SNOWFLAKE_CONNECTION" \
  -q "USE ROLE ${TARGET_SNOWFLAKE_ROLE:-ACCOUNTADMIN};
      DROP POSTGRES INSTANCE IF EXISTS ${TARGET_SPG_SERVICE};"
```

Verify it is gone:

```bash
snow sql \
  -c "$TARGET_SNOWFLAKE_CONNECTION" \
  -q "USE ROLE ${TARGET_SNOWFLAKE_ROLE:-ACCOUNTADMIN}; SHOW POSTGRES INSTANCES;"
```

The instance name must no longer appear in the output.

### Step 4 — Drop the network rule (if one was created)

```bash
snow sql \
  -c "$TARGET_SNOWFLAKE_CONNECTION" \
  -q "USE ROLE SECURITYADMIN;
      DROP NETWORK RULE IF EXISTS <NETWORK_RULE_NAME>;"
```

If the network rule name is not known, skip this step — it is not billable.

### Why the account matters

SPG instances are provisioned in a specific Snowflake account.  The Cortex
Code IDE session runs against whatever account is configured in `connections.toml`
as default, which may be a **different** account (e.g. a data analytics account
vs the account that holds the SPG).

`TARGET_SNOWFLAKE_CONNECTION` is written to `target_conn.env` by target-setup
at provisioning time so teardown always targets the right account, regardless of
which account the current IDE session uses.
