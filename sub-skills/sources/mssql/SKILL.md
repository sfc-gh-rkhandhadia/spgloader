# MSSQL → Snowflake Postgres Migration
**Source entry-point for SQL Server migrations.**
This sub-skill documents every source-specific detail for MSSQL migrations.
All phases delegate to shared phase sub-skills; only MSSQL-specific behaviour is documented here.

---

## Source-Specific Details

### Connection & Docker
- **Driver**: `pymssql` / `pyodbc`
- **Docker template**: `references/docker-templates/mssql-compose.yml`
- **Required env vars**: `MSSQL_SA_PASSWORD`, `SOURCE_DATABASE`
- **Default port**: 1433

### DDL Extraction (Phase 3)
- Uses `sys.objects`, `sys.schemas`, `sys.sql_modules`, `sys.columns` (not INFORMATION_SCHEMA)
- Views, procs, and functions extracted via `sys.views`, `sys.procedures`, `sys.objects`
- Identity columns: `IDENTITY(1,1)` detected via `sys.columns.is_identity`
- FK constraints: `sys.foreign_keys` + `sys.foreign_key_columns`
- Known MSSQL-only objects detected: CLR assemblies, SQL Server Agent jobs, linked servers, temporal tables, UDTT

### Compatibility Assessment (Phase 3.5)
- Checks: CLR objects (`SPG-BLOCK-001`), FILESTREAM (`SPG-BLOCK-003`), linked servers
- MSSQL-specific deprecated patterns: `aspnet_membership`, `sql_server_agent`, `linked_servers`, `clr_objects`, `udtt`, `extended_procs`, `temporal_tables`

### Conversion (Phase 4)
- **Type mappings**: `references/rules/mssql-to-pg/type-mappings.yaml`
- **Function substitutions**: `references/rules/mssql-to-pg/function-substitutions.yaml`
  - Key: `ISNULL→COALESCE`, `GETDATE()→NOW()`, `DATEDIFF→EXTRACT`, `TOP N→LIMIT N`, `LEN→LENGTH`, `CHARINDEX→STRPOS`, `STUFF→OVERLAY`, `CONVERT→CAST`
- **DDL cleanup**: `references/rules/mssql-to-pg/ddl-cleanup.yaml` — removes `[brackets]`, `IDENTITY(1,1)→GENERATED ALWAYS AS IDENTITY`, `GO` splitter, `WITH(NOLOCK)`
- **Date units**: `references/rules/mssql-to-pg/date-units.yaml` — `DATEDIFF` / `DATEADD` unit name mapping
- **View fix pass** (`fix_views.py`): applied after initial deploy failures to handle `dbo.schema.table` 3-part references and lateral join patterns
- **LLM repair prompt**: `references/prompts/procedure-repair-prompt.md` (T-SQL → PL/pgSQL)
- **Unsupported**: XQuery views (`xml.nodes()`, `.value()`), `FOR XML` — these get `SPG-EWI-0004` and may need manual rewrite

### Data Copy (Phase 5.5, optional)
- Script: `scripts/copy_source_data.py`
- Uses BULK INSERT-style extraction (SELECT * via pymssql, INSERT batches to SPG)
- Identity values preserved via `OVERRIDING SYSTEM VALUE`

### Parity (Phase 6.6)
- Structural parity: `scripts/execution-parity/full_validation.py`
  - MSSQL schemas are mixed-case (e.g. `HumanResources`); SPG folds to lowercase — schema normalization handled automatically
- Execution parity: `scripts/execution-parity/run.py` with `--source-type mssql`

### EWI Codes most common in MSSQL migrations
| Code | Meaning | Action |
|---|---|---|
| `SPG-EWI-0004` | Dynamic SQL (`EXEC`, `sp_executesql`) | Review and rewrite if needed |
| `SPG-EWI-0007` | `OPENQUERY` / linked server reference | Replace with direct query or view |
| `SPG-EWI-0012` | Unconverted construct (TABLE var, cursor, UDTT) | **Manual rewrite required** |
| `SPG-BLOCK-001` | CLR object | Must remove before migration |

---

## Phase Routing for MSSQL

| Phase | Sub-skill | Notes |
|---|---|---|
| 1 | `source-setup` | Use `mssql-compose.yml`, `MSSQL_SA_PASSWORD` |
| 2 | `target-setup` | Shared — identical for all sources |
| 3 | `ddl-extract` | Pass `--source-type mssql` |
| 3.5 | `assess` | Pass `--source-type mssql` |
| 3.6 | `deprecated-review` | MSSQL-specific patterns (see above) |
| 4 | `convert` | Pass `--source-type mssql`, uses MSSQL YAML rules |
| 5 | `deploy` | Shared — identical for all sources |
| 6 | `validate` | Shared — identical for all sources |
| 6.5–6.6 | `witness-validate` | Pass `--source-type mssql` to full_validation.py |
