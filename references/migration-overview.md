# spgloader — Migration Reference

Full reference material for the spgloader migration skill.
See `SKILL.md` for the orchestration workflow.

## Source Database Support Matrix

| Source | Tables + Data | Views | Functions | Procedures | Triggers | Notes |
|---|---|---|---|---|---|---|
| **MSSQL** | ✅ pgloader | ✅ rule-based | ✅ rule-based | ⚠️ best-effort | ⚠️ best-effort | Validated with SQL Server 2022 |
| **MySQL** | ✅ pgloader | ✅ rule-based | ⚠️ best-effort | ⚠️ best-effort | ⚠️ best-effort | Experimental — test before production use |
| **Oracle** | ⚠️ LLM only | ⚠️ LLM only | ⚠️ LLM only | ⚠️ LLM only | ⚠️ LLM only | Experimental — orafce extension recommended |

- ✅ = validated in production use
- ⚠️ = best-effort; results require review before deploying to production

## Conversion Paths

| Path | Objects | Sources | Script |
|---|---|---|---|
| **pgloader** | Tables + data | MSSQL, MySQL | `gen_pgloader_config.py` |
| **Rule-based** | Views | MSSQL, MySQL | `convert_objects.py` → `fix_views.py` |
| **Rule-based** | Functions | MSSQL, MySQL | `convert_objects.py` → `fix_functions.py` |
| **LLM** | All objects | Oracle | `convert_objects.py` (LLM path) |
| **LLM** | Complex procs/triggers | Any source | Manual + `convert_objects.py` |

Converted files are annotated with `SPG-EWI-XXXX` codes identifying what was changed
and what requires human review — following SnowConvert conventions.

## Phase 3.6 — Deprecated Object Review

**Script:** `scripts/analyze_deprecated.py`

**Rule catalog:** `references/rules/deprecated-patterns.yaml`

Runs immediately after Phase 3.5. Scans all DDL objects against the deprecated
patterns catalog and groups matches by technology. For each group detected, shows
the user:
- What the deprecated technology is and why it is flagged
- How many objects were matched and their names
- A recommendation for what to do
- Options: `skip | migrate | modernize`

```bash
python scripts/analyze_deprecated.py \
  --work-dir $SPGLOADER_WORK_DIR \
  [--catalog  references/rules/deprecated-patterns.yaml] \
  [--non-interactive]   # auto-apply recommended option; use in CI
```

| Scenario | Result |
|---|---|
| No deprecated patterns detected | Completes silently; all objects go to Phase 4 |
| Patterns detected, user chooses `skip` | Objects excluded from conversion |
| Patterns detected, user chooses `migrate` | Objects converted normally |
| Patterns detected, user chooses `modernize` | Objects annotated; excluded from auto-conversion |

### Currently detected patterns

| Pattern ID | Technology | Source |
|---|---|---|
| `aspnet_membership` | ASP.NET Membership Provider (deprecated 2013) | MSSQL |
| `sql_server_agent` | SQL Server Agent jobs (xp_cmdshell, sp_add_job) | MSSQL |
| `linked_servers` | Linked server four-part names | MSSQL |
| `clr_objects` | CLR / .NET assemblies in SQL | MSSQL |
| `udtt` | User-Defined Table Types (TVPs) | MSSQL |
| `extended_procs` | Extended stored procedures (xp_*) | MSSQL |
| `temporal_tables` | System-versioned temporal tables | MSSQL |

To add a new pattern: edit `references/rules/deprecated-patterns.yaml` — no code changes needed.
See the file header for the full field schema.
