---
name: spgloader
description: "Migrate MSSQL, MySQL, or Oracle databases to Snowflake Postgres (SPG).
  Includes a mandatory SPG Compatibility Assessment guardrail that scans DDL
  against Snowflake Postgres-specific rules before conversion begins.
  Handles source environment setup (existing or Docker), SPG provisioning,
  DDL extraction, SPG compatibility assessment, pgloader-based data migration,
  LLM-based object conversion with EWI annotations, deployment, and validation.
  Triggers: migrate to snowflake postgres, spgloader, mssql to spg,
  mysql to spg, oracle to spg, database migration to postgres,
  pgloader migration, source ddl to snowflake postgres, convert mssql mysql oracle to postgres."
---

# spgloader — Multi-Source to Snowflake Postgres Migration

## Overview

spgloader migrates MSSQL, MySQL, or Oracle databases to Snowflake Postgres (SPG).
A **mandatory SPG Compatibility Assessment** (Phase 3.5) blocks migration on hard incompatibilities.
See `references/migration-overview.md` for conversion paths, source support matrix, and Phase 3.6 detail.

## Skill Directory

`<SKILL_DIR>` = absolute path to this skill: `~/sko-coco/spgloader`

## Phase 0 — Gather Environment Info

Ask the user with a single `ask_user_question` call (4 questions):

1. **Source DB type** (options): MSSQL | MySQL | Oracle
2. **Source DB version** (text): default per type: MSSQL → `2022`, MySQL → `8.0`, Oracle → `23c`
3. **Source environment** (options): "Use existing environment" | "Deploy in Docker"
4. **Target SPG** (options): "Use existing SPG instance" | "Provision new SPG"

Store answers as:
- `SOURCE_TYPE` — mssql | mysql | oracle
- `SOURCE_VERSION` — version string
- `SOURCE_ENV` — existing | docker
- `TARGET_SPG` — existing | new

## Shared Workspace

Initialize the workspace before starting phases:

```bash
SPGLOADER_WORK_DIR="${SPGLOADER_WORK_DIR:-$HOME/.spgloader/$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$SPGLOADER_WORK_DIR/.spgloader"
echo "Working directory: $SPGLOADER_WORK_DIR"
```

All phases read and write through files in this directory. See the workspace contract in
`lib/spgloader/workspace.py`.

## Phase Routing

Execute phases in order. Load each sub-skill, execute its full workflow, then continue.

| Phase | Sub-skill | Description |
|-------|-----------|-------------|
| 1 | `sub-skills/source-setup/SKILL.md` | Connect or deploy source DB |
| 2 | `sub-skills/target-setup/SKILL.md` | Connect or provision SPG |
| 3 | `sub-skills/ddl-extract/SKILL.md` | Extract DDL + build dep graph |
| **3.5** | **`sub-skills/assess/SKILL.md`** | **SPG Compatibility Assessment (GUARDRAIL)** |
| **3.6** | **`scripts/analyze_deprecated.py`** | **Deprecated Object Review (DECISION GATE)** |
| 4 | `sub-skills/convert/SKILL.md` | Convert DDL (pgloader + LLM, EWI-annotated) |
| 5 | `sub-skills/deploy/SKILL.md` | Deploy to SPG in dep order |
| 6 | `sub-skills/validate/SKILL.md` | Row counts + spot checks |

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
│   ├── pgloader/migration.load   — pgloader config
│   └── postgres/
│       ├── wave_1_tables/        — Oracle tables only
│       ├── wave_2_views/
│       ├── wave_3_functions/
│       └── wave_4_procedures_triggers/
│   └── _conversion_report.json
├── deployment/
│   └── deployment_summary.json
└── validation/
    └── validation_report.json
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
```

## Mandatory Stopping Points

| Action | Why | Phase |
|--------|-----|-------|
| BLOCK findings in assessment | Migration incompatible with SPG | 3.5 |
| Docker Oracle image pull | Requires `docker login container-registry.oracle.com` | 1 |
| SPG CREATE | Billable resource | 2 |
| Deploy DDL to SPG | Destructive on target | 5 |

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

SPG Assessment:   PASSED (<N> warnings acknowledged)
Extension prereqs installed: <list or none>

Objects migrated:
  Tables:              N  (via pgloader)
  Views:               N  (via LLM, wave 2)
  Functions:           N  (via LLM, wave 3)
  Stored procedures:   N  (via LLM, wave 4)
  Triggers:            N  (via LLM, wave 4)

Deployment:   N succeeded / N failed / N skipped
Validation:   N tables match / N mismatches

Working dir:  <SPGLOADER_WORK_DIR>
```
