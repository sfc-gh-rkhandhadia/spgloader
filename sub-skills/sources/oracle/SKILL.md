# Oracle → Snowflake Postgres Migration
**Source entry-point for Oracle Database 12c / 19c / 21c migrations.**
This sub-skill documents every source-specific detail for Oracle migrations.

---

## Source-Specific Details

### Connection & Docker
- **Driver**: `oracledb` (python-oracledb thin mode, no Oracle Client needed)
- **Docker template**: `references/docker-templates/oracle-compose.yml`
- **Required env vars**: `ORACLE_PASSWORD`, `SOURCE_DATABASE` (service name or SID)
- **Default port**: 1521
- **Thin mode**: set `ORACLE_THIN_MODE=true` — no Oracle Instant Client required

### DDL Extraction (Phase 3)
- Uses `ALL_OBJECTS`, `ALL_SOURCE`, `ALL_VIEWS`, `ALL_ARGUMENTS` (not INFORMATION_SCHEMA)
- Schema = Oracle user/schema (e.g. `HR`, `SCOTT`) — all-uppercase in Oracle catalog
- PL/SQL objects extracted from `ALL_SOURCE` concatenated by line number
- Sequences extracted from `ALL_SEQUENCES` — mapped to PostgreSQL sequences
- `CREATE OR REPLACE` syntax is Oracle-native — preserved in extraction
- Script: `scripts/extract_ddl.py --source-type oracle`
- Table data copy (optional): `scripts/copy_oracle_data.py` (dedicated script, not `copy_source_data.py`)

### Compatibility Assessment (Phase 3.5)
- Oracle-specific: `DBMS_` package calls, `UTL_FILE`, `XMLTYPE`, `SDO_GEOMETRY` (spatial)
- `DBMS_OUTPUT.PUT_LINE` → `RAISE NOTICE` (auto-converted in repair pass)
- Autonomous transactions (`PRAGMA AUTONOMOUS_TRANSACTION`) → `SPG-BLOCK` (not supported in SPG)
- `CONNECT BY` hierarchical queries → needs manual rewrite

### Conversion (Phase 4)
- **Type mappings**: `references/rules/oracle-to-pg/type-mappings.yaml`
  - Key: `NUMBER(p,s)→NUMERIC(p,s)`, `VARCHAR2→VARCHAR`, `DATE→TIMESTAMP`, `CLOB→TEXT`, `BLOB→BYTEA`, `RAW(n)→BYTEA`, `BINARY_INTEGER→INTEGER`, `PLS_INTEGER→INTEGER`
- **Function substitutions**: `references/rules/oracle-to-pg/function-substitutions.yaml`
  - Key: `NVL→COALESCE`, `SYSDATE→NOW()`, `SYSTIMESTAMP→NOW()`, `SYS_GUID()→gen_random_uuid()`, `seq.NEXTVAL→NEXTVAL('seq')`, `FROM DUAL` removal, `DECODE(col,v1,r1,v2,r2,def)→CASE WHEN`
- **PlpgSQL fixes**: `references/rules/oracle-to-pg/plpgsql-fixes.yaml`
  - Key: `EXCEPTION WHEN OTHERS→EXCEPTION WHEN OTHERS`, `IS NULL` syntax, `||` (string concat — already PostgreSQL-compatible), `OUT` parameter handling
- **No `fix_views.py` pass**: Oracle views go through conversion directly — no separate fix pass needed
- **LLM repair prompt**: `references/prompts/procedure-repair-oracle-prompt.md` — auto-selected when `SOURCE_TYPE=oracle`
- **Unsupported constructs** (get `SPG-EWI-0012`):
  - `CURSOR FOR LOOP` with complex logic
  - `BULK COLLECT INTO` / `FORALL`
  - `TYPE ... IS TABLE OF` / `VARRAY`
  - `DBMS_PIPE`, `UTL_HTTP`, `XMLTYPE` methods

### Data Copy (Phase 5.5, optional)
- Script: `scripts/copy_oracle_data.py` (not `copy_source_data.py`)
- Uses `oracledb` cursor with FETCH_ARRAYSIZE for large tables
- CLOB/BLOB columns handled with `read()` wrapper

### Parity (Phase 6.6)
- Structural parity: `scripts/execution-parity/full_validation.py` with `source_adapter` for Oracle
- Execution parity: `scripts/execution-parity/run.py` with `--source-type oracle`
- Oracle schemas are all-uppercase in catalog but `source_adapter` normalizes to lowercase for SPG comparison

### EWI Codes most common in Oracle migrations
| Code | Meaning | Action |
|---|---|---|
| `SPG-EWI-0004` | Dynamic SQL (`EXECUTE IMMEDIATE`) | Review and rewrite if needed |
| `SPG-EWI-0012` | BULK COLLECT, VARRAY, CURSOR FOR LOOP | **Manual rewrite required** |
| `SPG-BLOCK-001` | Autonomous transaction, Java stored proc | Must remove before migration |

---

## Phase Routing for Oracle

| Phase | Sub-skill | Notes |
|---|---|---|
| 1 | `source-setup` | Use `oracle-compose.yml`, `ORACLE_PASSWORD`, thin mode |
| 2 | `target-setup` | Shared |
| 3 | `ddl-extract` | Pass `--source-type oracle` |
| 3.5 | `assess` | Pass `--source-type oracle` |
| 3.6 | `deprecated-review` | Check for DBMS_ packages, XMLTYPE, SDO |
| 4 | `convert` | Pass `--source-type oracle`, uses Oracle YAML rules |
| 5 | `deploy` | Shared |
| 6 | `validate` | Shared |
| 6.5–6.6 | `witness-validate` | Pass `--source-type oracle` to full_validation.py |
