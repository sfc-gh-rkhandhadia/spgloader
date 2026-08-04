# MySQL → Snowflake Postgres Migration
**Source entry-point for MySQL 5.7 / 8.x migrations.**
This sub-skill documents every source-specific detail for MySQL migrations.

---

## Source-Specific Details

### Connection & Docker
- **Driver**: `mysql-connector-python`
- **Docker template**: `references/docker-templates/mysql-compose.yml`
- **Required env vars**: `MYSQL_ROOT_PASSWORD`, `SOURCE_DATABASE`
- **Default port**: 3306

### DDL Extraction (Phase 3)
- Uses `INFORMATION_SCHEMA.TABLES`, `INFORMATION_SCHEMA.COLUMNS`, `INFORMATION_SCHEMA.ROUTINES`
- Backtick-quoted identifiers stripped during extraction
- `AUTO_INCREMENT` → detected as identity column
- `ENGINE=InnoDB`, `CHARSET=utf8mb4`, `COLLATE` clauses stripped in DDL cleanup
- `DEFINER=...` clause stripped from views and procedures

### Compatibility Assessment (Phase 3.5)
- MySQL-specific patterns: `ENUM` types, full-text indexes, MySQL JSON functions
- No CLR/Agent-style BLOCK codes — MySQL migrations are typically lower risk

### Conversion (Phase 4)
- **Type mappings**: `references/rules/mysql-to-pg/type-mappings.yaml`
  - Key: `TINYINT(1)→BOOLEAN`, `BIGINT UNSIGNED→NUMERIC(20)`, `DATETIME→TIMESTAMP`, `LONGTEXT→TEXT`, `MEDIUMTEXT→TEXT`, `TINYBLOB→BYTEA`, `ENUM(...)→VARCHAR`
- **Function substitutions**: `references/rules/mysql-to-pg/function-substitutions.yaml`
  - Key: `IFNULL→COALESCE`, `UNIX_TIMESTAMP()→EXTRACT(EPOCH FROM NOW())`, `DATE_FORMAT→TO_CHAR`, `GROUP_CONCAT→STRING_AGG`, `IF(cond,a,b)→CASE WHEN cond THEN a ELSE b END`, `STR_TO_DATE→TO_DATE`
- **PlpgSQL fixes**: `references/rules/mysql-to-pg/plpgsql-fixes.yaml`
- **No `ddl-cleanup.yaml`** for MySQL (bracket removal and GO-splitter are MSSQL-only)
- **Structural parity script**: `scripts/execution-parity/mysql_structural_parity.py`
- **LLM repair prompt**: `references/prompts/procedure-repair-mysql-prompt.md` — auto-selected when `SOURCE_TYPE=mysql`

### Data Copy (Phase 5.5, optional)
- Script: `scripts/copy_source_data.py`
- Uses `SELECT * FROM schema.table` via mysql-connector, INSERT batches to SPG

### Parity (Phase 6.6)
- Structural parity: `scripts/execution-parity/mysql_structural_parity.py` (MySQL-specific, uses INFORMATION_SCHEMA)
- Execution parity: `scripts/execution-parity/run.py` with `--source-type mysql`

### Notes / Common Issues
- MySQL `DELIMITER $$` / `END$$` must be stripped before SPG deployment — handled automatically by `extract_ddl.py`
- MySQL `CALL proc()` syntax works in SPG PostgreSQL natively
- `AUTO_INCREMENT` reset: SPG SERIAL/IDENTITY sequences start from 1 unless reseeded after data copy
- JSON path syntax differs: MySQL `->>'$.field'` → PostgreSQL `->'field'->>'text'`

---

## Phase Routing for MySQL

| Phase | Sub-skill | Notes |
|---|---|---|
| 1 | `source-setup` | Use `mysql-compose.yml`, `MYSQL_ROOT_PASSWORD` |
| 2 | `target-setup` | Shared |
| 3 | `ddl-extract` | Pass `--source-type mysql` |
| 3.5 | `assess` | Pass `--source-type mysql` |
| 3.6 | `deprecated-review` | Fewer patterns than MSSQL |
| 4 | `convert` | Pass `--source-type mysql`, uses MySQL YAML rules |
| 5 | `deploy` | Shared |
| 6 | `validate` | Shared |
| 6.5–6.6 | `witness-validate` | Use `mysql_structural_parity.py` for Phase 6.6 |
