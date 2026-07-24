# spgloader

Migrate MSSQL, MySQL, or Oracle databases to Snowflake Postgres (SPG).

spgloader is a Cortex Code skill that handles the full migration lifecycle:
source environment setup, DDL extraction, **SPG compatibility assessment**,
pgloader-based data migration, LLM-based DDL conversion with EWI annotations,
deployment, and validation.

---

## SPG Compatibility Assessment Guardrail

Before any conversion begins, spgloader runs a mandatory **SPG Compatibility
Assessment** that scans every extracted DDL object against Snowflake Postgres-
specific rules. Migration halts on BLOCK-level findings (e.g., non-PL/pgSQL
procedural languages, filesystem access, ALTER SYSTEM). WARN-level findings
require explicit user acknowledgment. This ensures objects deployed to SPG
will work as expected.

See `references/spg-compatibility.md` for the complete rule set and
`references/ewi-codes.md` for the full EWI code catalog.

---

## Prerequisites

| Requirement | Install |
|-------------|---------|
| Python 3.11+ | https://python.org |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| pgloader | `brew install pgloader` (macOS) or `apt install pgloader` (Linux) |
| Docker | https://docs.docker.com/get-docker/ (needed for Docker source option) |
| Snowflake account | AWS or Azure only (SPG not available on GCP) |
| Cortex Code | https://docs.snowflake.com/en/user-guide/cortex-code |

---

## Quick Start

### 1. Set up the skill

```bash
git clone <repo-url> ~/sko-coco/spgloader
cd ~/sko-coco/spgloader
bash setup.sh
```

### 2. Install into Cortex Code

```bash
# Option A: symlink (changes auto-reflect)
ln -s ~/sko-coco/spgloader ~/.snowflake/cortex/skills/spgloader

# Option B: copy
cp -r ~/sko-coco/spgloader ~/.snowflake/cortex/skills/spgloader
```

### 3. Start a migration in Cortex Code

Type any of:
```
migrate mssql to snowflake postgres
migrate mysql to snowflake postgres
migrate oracle to snowflake postgres
spgloader
```

The skill will walk you through all phases interactively.

---

## Supported Sources

| Source | Tables + Data | Views | Procedures | Functions | Triggers |
|--------|--------------|-------|------------|-----------|---------|
| MSSQL (SQL Server 2017+) | pgloader | LLM | LLM | LLM | LLM |
| MySQL 8.0+ | pgloader | LLM | LLM | LLM | LLM |
| Oracle (12c+, 19c, 21c, 23c) | LLM | LLM | LLM | LLM | LLM |

pgloader handles tables and data automatically. LLM converts all other objects
using type-mapping references as grounding.

---

## Migration Phases

```
Phase 0:   Gather environment info (source type, version, env, target SPG)
Phase 1:   Source setup (connect to existing DB or deploy in Docker)
Phase 2:   Target setup (connect to or provision Snowflake Postgres)
Phase 3:   DDL extraction (live DB query or file/paste)
Phase 3.5: SPG Compatibility Assessment ← GUARDRAIL
Phase 4:   Conversion (pgloader tables + LLM views/procs/funcs/triggers)
Phase 5:   Deploy to SPG in dependency order
Phase 6:   Validate (row counts + spot checks)
```

---

## Output Structure

Each migration run creates a timestamped working directory:

```
~/.spgloader/20250115_103000/
├── .spgloader/
│   ├── config.yaml              # connection details
│   └── manifest.json            # phase completion tracking
├── assessment/
│   ├── assessment_summary.json  # SPG compatibility findings
│   ├── assessment_report.md     # human-readable report
│   └── pre_deploy_extensions.sql
├── conversion/
│   ├── pgloader/migration.load
│   └── postgres/
│       ├── wave_2_views/        # EWI-annotated converted SQL
│       ├── wave_3_functions/
│       └── wave_4_procedures_triggers/
├── deployment/
│   └── deployment_summary.json
└── validation/
    └── validation_report.json
```

---

## Project Structure

```
spgloader/
├── SKILL.md                      ← Main orchestrator
├── setup.sh                      ← One-command setup
├── pyproject.toml                ← Dependencies (uv)
├── lib/spgloader/                ← Importable Python library
│   ├── connectors/               ← MSSQL, MySQL, Oracle extractors
│   ├── conversion/               ← dep_graph, ewi, pgloader_config
│   ├── deployment/               ← SPG deployment via psycopg2
│   ├── reporting/                ← SPG compatibility assessment
│   └── workspace.py              ← Per-project workspace contract
├── scripts/                      ← CLI entry points (thin wrappers)
│   ├── assess.py                 ← SPG compatibility assessment
│   ├── extract_ddl.py
│   ├── build_dep_graph.py
│   ├── gen_pgloader_config.py
│   └── deploy_to_spg.py
├── sub-skills/                   ← Phase sub-skills
│   ├── source-setup/
│   ├── target-setup/
│   ├── ddl-extract/
│   ├── assess/                   ← Phase 3.5 SPG guardrail
│   ├── convert/pgloader/
│   ├── deploy/
│   └── validate/
└── references/
    ├── spg-compatibility.md      ← SPG rule source of truth
    ├── ewi-codes.md              ← EWI code catalog
    ├── pgloader-support-matrix.md
    ├── type-mappings/            ← mssql, mysql, oracle → PG
    └── docker-templates/         ← mssql, mysql, oracle compose files
```

---

## Using the Python Library Directly

The `lib/spgloader/` package is designed to be imported by other tools:

```python
import sys
sys.path.insert(0, "/path/to/spgloader/lib")

# Extract DDL from a SQL Server database
from spgloader.connectors import get_connector
connector = get_connector("mssql", host="localhost", port=1433,
                          database="mydb", user="sa", password="...")
objects = connector.extract()

# Run SPG compatibility assessment
from spgloader.reporting.assessment import SPGCompatibilityAssessment
result = SPGCompatibilityAssessment().scan(objects, "mssql")
print(f"Blocked: {result.is_blocked}")
print(f"Extension prereqs: {result.extension_prereqs}")

# Build dependency graph
from spgloader.conversion.dep_graph import build_dep_graph_result
graph = build_dep_graph_result(objects)
```

---

## Frequently Asked Questions

**Q: My Snowflake account is on GCP. Can I use this?**
A: No. Snowflake Postgres is only available on AWS and Azure. The assessment will
block migration if GCP is detected as the target region.

**Q: Does spgloader support PostgreSQL-to-SPG migrations?**
A: Not currently. The supported sources are MSSQL, MySQL, and Oracle.
For Postgres-to-Snowflake (Standard) migrations, see the snowbound-migration plugin.

**Q: Why is my migration BLOCKED?**
A: The SPG assessment found features that cannot work in Snowflake Postgres.
Check `assessment/assessment_report.md` for the list of BLOCK findings and
resolution steps. See `references/spg-compatibility.md` for the full rule set.

**Q: Can I run the assessment without running the full migration?**
A: Yes. Use the CLI directly:
```bash
cd ~/sko-coco/spgloader
uv run python scripts/assess.py \
  --source-type mssql \
  --ddl-file /path/to/schema.sql \
  --output ./assessment/
```

**Q: What about Oracle packages?**
A: Oracle packages have no direct PostgreSQL equivalent. spgloader recommends
the `orafce` extension for Oracle function emulation (NVL, DECODE, etc.) and
converts packages to individual PL/pgSQL functions/procedures.
