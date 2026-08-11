# MariaDB → Snowflake Postgres Migration

> **⛔ DISABLED THIS RELEASE.** Do NOT execute this sub-skill. If routed here,
> stop and respond: "MariaDB support is planned for the next release. Currently
> supported: MSSQL and MySQL." Then return to the parent skill.

**Source entry-point for MariaDB 10.x / 11.x migrations.**
MariaDB is treated as a MySQL dialect — most rules, scripts, and prompts are shared.
This sub-skill documents the differences.

---

## Differences from MySQL

| Aspect | MySQL | MariaDB |
|---|---|---|
| Docker template | `mysql-compose.yml` | `mariadb-compose.yml` |
| Docker env var | `MYSQL_ROOT_PASSWORD` | `MARIADB_ROOT_PASSWORD` (same env var name used) |
| Image | `mysql:8.0` | `mariadb:11` |
| `SOURCE_TYPE` value | `mysql` | `mariadb` |
| Type mappings | `rules/mysql-to-pg/type-mappings.yaml` | same (shared) |
| Function substitutions | `rules/mysql-to-pg/function-substitutions.yaml` | same (shared) |
| LLM repair prompt | `procedure-repair-mysql-prompt.md` | same (auto-selected for `mariadb`) |
| Structural parity | `mysql_structural_parity.py` | same (shared) |

---

## Source-Specific Details

### Connection & Docker
- **Driver**: `mysql-connector-python` (compatible with MariaDB)
- **Docker template**: `references/docker-templates/mariadb-compose.yml`
- **Required env vars**: `MYSQL_ROOT_PASSWORD` (both MySQL and MariaDB use this env var name), `SOURCE_DATABASE`
- **Default port**: 3306

### MariaDB-Specific Features
- MariaDB `SEQUENCE` objects (native sequences, separate from `AUTO_INCREMENT`): currently extracted as-is; map `sequence_name.NEXTVAL` → `NEXTVAL('sequence_name')` in conversion
- MariaDB `JSON_TABLE()` (10.6+): not directly supported in SPG — EWI-0012 placeholder
- MariaDB `CONNECT` storage engine tables: cannot be migrated — treat as deprecated object
- `PERIOD` columns (temporal tables, MariaDB 10.4+): needs manual review

### Parity (Phase 6.6)
- Use `mysql_structural_parity.py` with `--source-type mariadb`

---

## Phase Routing for MariaDB

| Phase | Sub-skill | Notes |
|---|---|---|
| 1 | `source-setup` | Use `mariadb-compose.yml`, `MYSQL_ROOT_PASSWORD` |
| 2 | `target-setup` | Shared |
| 3 | `ddl-extract` | Pass `--source-type mariadb` |
| 3.5 | `assess` | Pass `--source-type mariadb` |
| 3.6 | `deprecated-review` | Check for CONNECT tables, PERIOD columns |
| 4 | `convert` | Pass `--source-type mariadb` (shares MySQL rules) |
| 5 | `deploy` | Shared |
| 6 | `validate` | Shared |
| 6.5–6.6 | `witness-validate` | Use `mysql_structural_parity.py --source-type mariadb` |
