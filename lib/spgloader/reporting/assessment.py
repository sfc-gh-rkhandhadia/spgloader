"""
SPG Compatibility Assessment — scans extracted DDL objects against SPG rules.

This is the guardrail phase that runs before conversion. It detects:
  BLOCK  — hard stops; migration cannot proceed
  WARN   — risks that require user confirmation
  RESOLVE — advisory items with automatic resolution available
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from spgloader.conversion.ewi import (
    EWICode, EWISeverity, SPG_EWI_CODES, get_codes_by_severity
)


# SPG extensions catalog (from docs.snowflake.com/en/user-guide/snowflake-postgres/postgres-extensions)
# This is the list of extensions supported in SPG. Any extension NOT in this list is BLOCKED.
SPG_SUPPORTED_EXTENSIONS = {
    "address_standardizer", "address_standardizer_data_us", "amcheck", "age",
    "datasketches", "pgaudit", "autoinc", "bloom", "btree_gin", "btree_gist",
    "pg_buffercache", "citext", "pg_cron", "pgcrypto", "cube", "ddlx", "dict_int",
    "dict_xsyn", "earthdistance", "pg_freespacemap", "fuzzystrmatch", "h3",
    "h3_postgis", "pg_hint_plan", "hll", "hstore", "http", "hypopg",
    "pg_incremental", "insert_username", "intagg", "intarray", "isn",
    "pg_ivm", "lo", "ltree", "pglogical", "moddatetime", "orafce",
    "pageinspect", "pgrowlocks", "pg_partman", "pg_lake", "postgis",
    "postgis_raster", "postgis_sfcgal", "postgis_topology", "postgres_fdw",
    "pg_prewarm", "pg_proctab", "refint", "pg_repack", "pgrouting",
    "semver", "pg_surgery", "seg", "sslinfo", "pg_stat_statements",
    "pgstattuple", "pg_squeeze", "tablefunc", "tsm_system_rows",
    "tsm_system_time", "tcn", "pg_trgm", "unaccent", "pg_visibility",
    "vector", "pgx_ulid", "uuid-ossp", "pg_uuidv7", "pg_walinspect", "xml2",
}

# Non-PL/pgSQL procedural languages (BLOCK if detected)
UNSUPPORTED_PROC_LANGUAGES = {
    "plpython", "plpython3u", "plpython2u", "plperlu", "plperl",
    "pltcl", "pltclu", "plv8", "plcoffee", "plls", "plruby",
    "plr", "pljava", "plphp", "plscheme",
}

# Protected server config parameters
PROTECTED_CONFIG_PARAMS = {
    "max_connections", "shared_buffers", "effective_cache_size",
    "wal_level", "archive_mode", "wal_keep_size", "max_wal_senders",
    "max_replication_slots", "shared_preload_libraries",
}

# Filesystem access functions
FILESYSTEM_FUNCTIONS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_waldir",
    "pg_ls_logdir", "pg_stat_file", "lo_import", "lo_export",
    "pg_write_file",
}

# Spatial type patterns
SPATIAL_PATTERNS = re.compile(
    r"\b(geography|geometry|sdo_geometry|sdo_point_type|mdsys\.sdo_geometry)\b",
    re.IGNORECASE,
)

# UUID function patterns
UUID_PATTERNS = re.compile(
    r"\b(newid|sys_guid|uuid_generate|gen_random_uuid|uuid)\s*\(", re.IGNORECASE
)

# Crypto patterns
CRYPTO_PATTERNS = re.compile(
    r"\b(hashbytes|md5|sha1|sha2|encrypt|decrypt|pgcrypto)\b", re.IGNORECASE
)


@dataclass
class Finding:
    code: str
    severity: EWISeverity
    title: str
    object_fqn: str
    object_type: str
    detail: str
    auto_resolution: str | None = None
    extension_prereq: str | None = None


@dataclass
class AssessmentResult:
    source_type: str
    total_objects: int
    block_findings: list[Finding] = field(default_factory=list)
    warn_findings: list[Finding] = field(default_factory=list)
    resolve_findings: list[Finding] = field(default_factory=list)
    info_findings: list[Finding] = field(default_factory=list)
    extension_prereqs: list[str] = field(default_factory=list)
    catalog_eligible: list[str] = field(default_factory=list)    # fqns (tables for parallel_deploy.py)
    llm_required: list[str] = field(default_factory=list)        # fqns
    conversion_confidence: float = 1.0
    tinyint1_count: int = 0   # MySQL: TINYINT(1) occurrences — skill will ask BOOLEAN vs SMALLINT

    @property
    def is_blocked(self) -> bool:
        return bool(self.block_findings)

    @property
    def block_codes(self) -> list[str]:
        return [f.code for f in self.block_findings]

    @property
    def warn_codes(self) -> list[str]:
        return [f.code for f in self.warn_findings]

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "total_objects": self.total_objects,
            "is_blocked": self.is_blocked,
            "conversion_confidence": round(self.conversion_confidence, 2),
            "block_findings": [_finding_to_dict(f) for f in self.block_findings],
            "warn_findings": [_finding_to_dict(f) for f in self.warn_findings],
            "resolve_findings": [_finding_to_dict(f) for f in self.resolve_findings],
            "extension_prereqs": self.extension_prereqs,
            "catalog_eligible": self.catalog_eligible,
            "llm_required": self.llm_required,
            "tinyint1_count": self.tinyint1_count,
        }


def _finding_to_dict(f: Finding) -> dict:
    return {
        "code": f.code,
        "severity": f.severity.value,
        "title": f.title,
        "object_fqn": f.object_fqn,
        "object_type": f.object_type,
        "detail": f.detail,
        "auto_resolution": f.auto_resolution,
        "extension_prereq": f.extension_prereq,
    }


class SPGCompatibilityAssessment:
    """Scans DDL objects for SPG compatibility issues."""

    def scan(self, objects: list[dict], source_type: str) -> AssessmentResult:
        result = AssessmentResult(source_type=source_type, total_objects=len(objects))
        prereqs_added: set[str] = set()

        for obj in objects:
            ddl = obj.get("ddl", "")
            fqn = obj.get("fqn", obj.get("name", "?"))
            obj_type = obj.get("type", "")

            # --- BLOCK checks ---
            self._check_procedural_languages(ddl, fqn, obj_type, result)
            self._check_superuser_ops(ddl, fqn, obj_type, result)
            self._check_alter_system(ddl, fqn, obj_type, result)
            self._check_filesystem_access(ddl, fqn, obj_type, result)
            self._check_catalog_modification(ddl, fqn, obj_type, result)
            self._check_extension_dependencies(ddl, fqn, obj_type, result)

            # --- WARN checks ---
            self._check_spatial_types(ddl, fqn, obj_type, result, prereqs_added)
            self._check_oracle_packages(ddl, fqn, obj_type, source_type, result, prereqs_added)
            self._check_scheduled_jobs(ddl, fqn, obj_type, result, prereqs_added)
            self._check_foreign_data_wrappers(ddl, fqn, obj_type, result)
            self._check_cursor_usage(ddl, fqn, obj_type, result)
            self._check_dynamic_sql(ddl, fqn, obj_type, source_type, result)
            self._check_protected_config(ddl, fqn, obj_type, result)
            self._check_pivot_views(ddl, fqn, obj_type, result)
            self._check_udt_parameters(ddl, fqn, obj_type, result)
            self._check_cross_db_refs(ddl, fqn, obj_type, result, source_type)
            self._check_union_type_mismatch(ddl, fqn, obj_type, result)
            self._check_implicit_type_coercion(ddl, fqn, obj_type, result)

            # --- RESOLVE checks ---
            self._check_uuid_functions(ddl, fqn, obj_type, result, prereqs_added)
            self._check_crypto_functions(ddl, fqn, obj_type, result, prereqs_added)

            # Classify pgloader vs LLM
            if source_type in ("mssql", "mysql") and obj_type == "table":
                result.catalog_eligible.append(fqn)
            else:
                result.llm_required.append(fqn)

        # Oracle: always recommend orafce
        if source_type == "oracle" and "orafce" not in prereqs_added:
            result.resolve_findings.append(Finding(
                code="SPG-RESOLVE-002",
                severity=EWISeverity.RESOLVE,
                title="Extension prerequisite: orafce (Oracle emulation)",
                object_fqn="(all Oracle objects)",
                object_type="schema",
                detail="Oracle source detected. orafce provides NVL, DECODE, ADD_MONTHS, and other Oracle-compatible functions.",
                auto_resolution="CREATE EXTENSION IF NOT EXISTS orafce;",
                extension_prereq="orafce",
            ))
            result.extension_prereqs.append("orafce")
            prereqs_added.add("orafce")

        # --- Schema-level checks (run after per-object loop) ---
        self._check_tinyint1_mapping(objects, source_type, result)

        # Count-based BLOCK checks
        role_count = sum(1 for o in objects if "CREATE ROLE" in (o.get("ddl") or "").upper())
        if role_count > 64:
            result.block_findings.append(Finding(
                code="SPG-BLOCK-007",
                severity=EWISeverity.BLOCK,
                title="Exceeds 64-role limit",
                object_fqn="(schema)",
                object_type="schema",
                detail=f"Source schema has {role_count} roles. SPG enforces a maximum of 64 roles per instance.",
            ))

        db_count = sum(1 for o in objects if "CREATE DATABASE" in (o.get("ddl") or "").upper())
        if db_count > 32:
            result.block_findings.append(Finding(
                code="SPG-BLOCK-008",
                severity=EWISeverity.BLOCK,
                title="Exceeds 32-database limit",
                object_fqn="(schema)",
                object_type="schema",
                detail=f"Source schema creates {db_count} databases. SPG enforces a maximum of 32 databases.",
            ))

        # Confidence score: start at 1.0, reduce for each BLOCK/WARN
        deductions = len(result.block_findings) * 0.15 + len(result.warn_findings) * 0.05
        result.conversion_confidence = max(0.0, min(1.0, 1.0 - deductions))

        return result

    # ----------------------------------------------------------------
    # Detection helpers
    # ----------------------------------------------------------------

    def _check_procedural_languages(self, ddl, fqn, obj_type, result):
        upper = ddl.upper()
        for lang in UNSUPPORTED_PROC_LANGUAGES:
            if lang.upper() in upper or f"LANGUAGE {lang.upper()}" in upper:
                result.block_findings.append(Finding(
                    code="SPG-BLOCK-001",
                    severity=EWISeverity.BLOCK,
                    title="Non-PL/pgSQL procedural language",
                    object_fqn=fqn,
                    object_type=obj_type,
                    detail=f"Object uses procedural language '{lang}'. SPG only supports PL/pgSQL.",
                ))
                break

    def _check_superuser_ops(self, ddl, fqn, obj_type, result):
        patterns = [r"CREATE\s+ROLE\s+\w+.*SUPERUSER", r"SET\s+ROLE\s+(postgres|snowflake_superuser)",
                    r"ALTER\s+ROLE\s+\w+.*SUPERUSER"]
        for p in patterns:
            if re.search(p, ddl, re.IGNORECASE):
                result.block_findings.append(Finding(
                    code="SPG-BLOCK-002",
                    severity=EWISeverity.BLOCK,
                    title="Superuser creation or assumption",
                    object_fqn=fqn, object_type=obj_type,
                    detail="Creating or assuming superuser roles is blocked in SPG.",
                ))
                return

    def _check_alter_system(self, ddl, fqn, obj_type, result):
        if re.search(r"\bALTER\s+SYSTEM\b", ddl, re.IGNORECASE):
            result.block_findings.append(Finding(
                code="SPG-BLOCK-003",
                severity=EWISeverity.BLOCK,
                title="ALTER SYSTEM statement",
                object_fqn=fqn, object_type=obj_type,
                detail="ALTER SYSTEM is reserved for Snowflake-managed configuration in SPG.",
            ))

    def _check_filesystem_access(self, ddl, fqn, obj_type, result):
        upper = ddl.upper()
        for fn in FILESYSTEM_FUNCTIONS:
            if fn.upper() in upper:
                result.block_findings.append(Finding(
                    code="SPG-BLOCK-004",
                    severity=EWISeverity.BLOCK,
                    title="Filesystem access function",
                    object_fqn=fqn, object_type=obj_type,
                    detail=f"Object uses filesystem function '{fn}'. Filesystem access is blocked in SPG.",
                ))
                return
        if re.search(r"\bCOPY\b.+\bPROGRAM\b", ddl, re.IGNORECASE):
            result.block_findings.append(Finding(
                code="SPG-BLOCK-004",
                severity=EWISeverity.BLOCK,
                title="COPY TO/FROM PROGRAM",
                object_fqn=fqn, object_type=obj_type,
                detail="COPY ... PROGRAM requires filesystem access, which is blocked in SPG.",
            ))

    def _check_catalog_modification(self, ddl, fqn, obj_type, result):
        if re.search(r"\bpg_catalog\b|\bsystem\s+catalog\b", ddl, re.IGNORECASE):
            if re.search(r"\b(INSERT|UPDATE|DELETE|ALTER)\b", ddl, re.IGNORECASE):
                result.block_findings.append(Finding(
                    code="SPG-BLOCK-005",
                    severity=EWISeverity.BLOCK,
                    title="Direct system catalog modification",
                    object_fqn=fqn, object_type=obj_type,
                    detail="Modifying pg_catalog tables directly is restricted in SPG.",
                ))

    def _check_extension_dependencies(self, ddl, fqn, obj_type, result):
        m = re.search(r"\bCREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w[\w-]*)[\"']?",
                      ddl, re.IGNORECASE)
        if m:
            ext_name = m.group(1).lower().replace("-", "_")
            if ext_name not in {e.replace("-", "_") for e in SPG_SUPPORTED_EXTENSIONS}:
                result.block_findings.append(Finding(
                    code="SPG-BLOCK-006",
                    severity=EWISeverity.BLOCK,
                    title="Extension not in SPG catalog",
                    object_fqn=fqn, object_type=obj_type,
                    detail=f"Extension '{m.group(1)}' is not in the SPG extension catalog. "
                           f"Custom .so/.dll extensions cannot be installed.",
                ))

    def _check_spatial_types(self, ddl, fqn, obj_type, result, prereqs_added):
        if SPATIAL_PATTERNS.search(ddl):
            result.warn_findings.append(Finding(
                code="SPG-WARN-001",
                severity=EWISeverity.WARN,
                title="Spatial/geometry type detected",
                object_fqn=fqn, object_type=obj_type,
                detail="PostGIS IS available in SPG but must be enabled first.",
                auto_resolution="CREATE EXTENSION IF NOT EXISTS postgis;",
                extension_prereq="postgis",
            ))
            if "postgis" not in prereqs_added:
                result.extension_prereqs.append("postgis")
                result.resolve_findings.append(Finding(
                    code="SPG-RESOLVE-001",
                    severity=EWISeverity.RESOLVE,
                    title="Extension prerequisite: postgis",
                    object_fqn="(pre-deploy)", object_type="schema",
                    detail="Spatial types detected. Add to pre-deploy script.",
                    auto_resolution="CREATE EXTENSION IF NOT EXISTS postgis;",
                    extension_prereq="postgis",
                ))
                prereqs_added.add("postgis")

    def _check_oracle_packages(self, ddl, fqn, obj_type, source_type, result, prereqs_added):
        if source_type == "oracle" and re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PACKAGE\b", ddl, re.IGNORECASE):
            result.warn_findings.append(Finding(
                code="SPG-WARN-004",
                severity=EWISeverity.WARN,
                title="Oracle package or synonym",
                object_fqn=fqn, object_type=obj_type,
                detail="Oracle PACKAGE has no direct PostgreSQL equivalent. "
                       "orafce provides Oracle function emulation.",
                auto_resolution="CREATE EXTENSION IF NOT EXISTS orafce;",
                extension_prereq="orafce",
            ))
            if "orafce" not in prereqs_added:
                result.extension_prereqs.append("orafce")
                prereqs_added.add("orafce")

    def _check_scheduled_jobs(self, ddl, fqn, obj_type, result, prereqs_added):
        if re.search(r"\b(dbms_scheduler|pg_agent|pg_cron\.schedule)\b", ddl, re.IGNORECASE):
            result.warn_findings.append(Finding(
                code="SPG-WARN-005",
                severity=EWISeverity.WARN,
                title="Scheduled job / pg_agent reference",
                object_fqn=fqn, object_type=obj_type,
                detail="pg_cron IS available in SPG. Jobs must be migrated to pg_cron syntax.",
                auto_resolution="CREATE EXTENSION IF NOT EXISTS pg_cron;",
                extension_prereq="pg_cron",
            ))
            if "pg_cron" not in prereqs_added:
                result.extension_prereqs.append("pg_cron")
                prereqs_added.add("pg_cron")

    def _check_foreign_data_wrappers(self, ddl, fqn, obj_type, result):
        m = re.search(r"\bFOREIGN\s+DATA\s+WRAPPER\s+(\w+)", ddl, re.IGNORECASE)
        if m:
            fdw = m.group(1).lower()
            if fdw not in ("postgres_fdw", "file_fdw"):
                result.warn_findings.append(Finding(
                    code="SPG-WARN-006",
                    severity=EWISeverity.WARN,
                    title="Non-Postgres foreign data wrapper",
                    object_fqn=fqn, object_type=obj_type,
                    detail=f"FDW '{fdw}' is not available in SPG. Only postgres_fdw is supported.",
                ))

    def _check_cursor_usage(self, ddl, fqn, obj_type, result):
        if obj_type in ("procedure", "function") and re.search(
            r"\b(DECLARE\s+\w+\s+CURSOR|OPEN\s+\w+|FETCH\s+\w+|CURSOR\s+FOR)\b", ddl, re.IGNORECASE
        ):
            result.warn_findings.append(Finding(
                code="SPG-WARN-007",
                severity=EWISeverity.WARN,
                title="Cursor loop in procedure",
                object_fqn=fqn, object_type=obj_type,
                detail="Cursor loops are supported in PL/pgSQL but should be reviewed for performance.",
            ))

    def _check_dynamic_sql(self, ddl, fqn, obj_type, source_type, result):
        patterns = {"mssql": r"\bsp_executesql\b", "oracle": r"\bEXECUTE\s+IMMEDIATE\b",
                    "mysql": r"\bPREPARE\s+\w+\s+FROM\b"}
        pattern = patterns.get(source_type, r"\bEXECUTE\s+IMMEDIATE\b")
        if re.search(pattern, ddl, re.IGNORECASE):
            result.warn_findings.append(Finding(
                code="SPG-WARN-008",
                severity=EWISeverity.WARN,
                title="Dynamic SQL with dialect-specific syntax",
                object_fqn=fqn, object_type=obj_type,
                detail="Dynamic SQL is supported in PL/pgSQL via EXECUTE; bind variable syntax differs.",
            ))

    def _check_protected_config(self, ddl, fqn, obj_type, result):
        for param in PROTECTED_CONFIG_PARAMS:
            if re.search(rf"\bSET\s+{param}\b", ddl, re.IGNORECASE):
                result.warn_findings.append(Finding(
                    code="SPG-WARN-003",
                    severity=EWISeverity.WARN,
                    title="Protected server configuration reference",
                    object_fqn=fqn, object_type=obj_type,
                    detail=f"Parameter '{param}' is managed by Snowflake and cannot be changed.",
                ))
                return

    def _check_uuid_functions(self, ddl, fqn, obj_type, result, prereqs_added):
        if UUID_PATTERNS.search(ddl) and "uuid-ossp" not in prereqs_added:
            result.resolve_findings.append(Finding(
                code="SPG-RESOLVE-003",
                severity=EWISeverity.RESOLVE,
                title="Extension prerequisite: uuid-ossp",
                object_fqn=fqn, object_type=obj_type,
                detail="UUID generation functions detected.",
                auto_resolution='CREATE EXTENSION IF NOT EXISTS "uuid-ossp";',
                extension_prereq="uuid-ossp",
            ))
            result.extension_prereqs.append("uuid-ossp")
            prereqs_added.add("uuid-ossp")

    def _check_crypto_functions(self, ddl, fqn, obj_type, result, prereqs_added):
        if CRYPTO_PATTERNS.search(ddl) and "pgcrypto" not in prereqs_added:
            result.resolve_findings.append(Finding(
                code="SPG-RESOLVE-005",
                severity=EWISeverity.RESOLVE,
                title="Extension prerequisite: pgcrypto",
                object_fqn=fqn, object_type=obj_type,
                detail="Cryptographic functions detected.",
                auto_resolution="CREATE EXTENSION IF NOT EXISTS pgcrypto;",
                extension_prereq="pgcrypto",
            ))
            result.extension_prereqs.append("pgcrypto")
            prereqs_added.add("pgcrypto")

    # ----------------------------------------------------------------
    # New WARN checks (added from migration analysis)
    # ----------------------------------------------------------------

    def _check_tinyint1_mapping(self, objects: list[dict], source_type: str, result: "AssessmentResult"):
        """Schema-level: count TINYINT(1) columns in MySQL/MariaDB migrations.
        The skill will ask the user BOOLEAN vs SMALLINT based on this count."""
        if source_type not in ("mysql", "mariadb"):
            return
        import re as _re
        count = sum(
            len(_re.findall(r'\bTINYINT\s*\(\s*1\s*\)', obj.get("ddl", ""), _re.IGNORECASE))
            for obj in objects
            if obj.get("type") in ("table", "TABLE")
        )
        if count:
            result.tinyint1_count = count
            result.warn_findings.append(Finding(
                code="SPG-WARN-014",
                severity=EWISeverity.WARN,
                title=f"TINYINT(1) mapping choice required ({count} occurrence(s))",
                object_fqn="(schema-wide)",
                object_type="table",
                detail=(
                    f"{count} column(s) use TINYINT(1). "
                    "TINYINT(1) is MySQL's boolean convention but some schemas use it "
                    "for small numeric values. The skill will ask how to map these columns."
                ),
            ))

    def _check_pivot_views(self, ddl: str, fqn: str, obj_type: str, result: "AssessmentResult"):
        """MSSQL: PIVOT syntax requires CTE rewrite — warn if auto-conversion may fail."""
        if obj_type == "view" and re.search(r'\bPIVOT\s*\(', ddl, re.IGNORECASE):
            result.warn_findings.append(Finding(
                code="SPG-WARN-009",
                severity=EWISeverity.WARN,
                title="PIVOT expression in view",
                object_fqn=fqn,
                object_type=obj_type,
                detail=(
                    "PIVOT is auto-converted to conditional aggregation (CTE). "
                    "If conversion fails, the view is marked FIX-REQUIRED and skipped during deployment. "
                    "Verify output in wave_2_views_fixed/ after Phase 4 conversion."
                ),
            ))

    def _check_udt_parameters(self, ddl: str, fqn: str, obj_type: str, result: "AssessmentResult"):
        """MSSQL: User-Defined Table Type parameters cannot be auto-migrated."""
        if obj_type in ("procedure", "function") and re.search(
            r'\bREADONLY\b|\bTABLE\s+TYPE\b|\bAS\s+TABLE\s*\(', ddl, re.IGNORECASE
        ):
            result.warn_findings.append(Finding(
                code="SPG-WARN-010",
                severity=EWISeverity.WARN,
                title="User-Defined Table Type (UDTT) parameter",
                object_fqn=fqn,
                object_type=obj_type,
                detail=(
                    "Procedure/function uses a table-valued parameter (UDTT) which cannot be "
                    "directly migrated to PostgreSQL. This object will be excluded from execution "
                    "parity testing. Consider rewriting to use a temporary table or JSON parameter."
                ),
            ))

    def _check_cross_db_refs(self, ddl: str, fqn: str, obj_type: str, result: "AssessmentResult", source_type: str):
        """MySQL/MariaDB: cross-database references in views won't work in PG."""
        if source_type not in ("mysql", "mariadb"):
            return
        if obj_type == "view" and re.search(r'\b\w+\.\w+\.\w+\b', ddl):
            result.warn_findings.append(Finding(
                code="SPG-WARN-011",
                severity=EWISeverity.WARN,
                title="Cross-database reference in MySQL view",
                object_fqn=fqn,
                object_type=obj_type,
                detail=(
                    "View contains a three-part name referencing a table in another MySQL database. "
                    "PostgreSQL does not support cross-database queries. "
                    "Include the referenced database in the migration scope or use postgres_fdw."
                ),
            ))

    def _check_union_type_mismatch(self, ddl: str, fqn: str, obj_type: str, result: "AssessmentResult"):
        """MSSQL/MySQL: UNION with mixed date and text expressions — PG requires exact type compat."""
        if obj_type != "view":
            return
        if not re.search(r'\bUNION\b', ddl, re.IGNORECASE):
            return
        has_date = re.search(r'\bCURRENT_DATE\b|\bCURRENT_TIMESTAMP\b|\bGETDATE\s*\(\s*\)|\bNOW\s*\(\s*\)', ddl, re.IGNORECASE)
        has_text_cast = re.search(r"CAST\s*\([^)]+AS\s+(?:TEXT|VARCHAR|NVARCHAR)\s*\)", ddl, re.IGNORECASE)
        if has_date and has_text_cast:
            result.warn_findings.append(Finding(
                code="SPG-WARN-012",
                severity=EWISeverity.WARN,
                title="Potential UNION branch type mismatch (date vs text)",
                object_fqn=fqn,
                object_type=obj_type,
                detail=(
                    "View uses UNION with mixed date and text expressions. "
                    "PostgreSQL requires exact type compatibility across all UNION branches. "
                    "If deployment fails with 'UNION types cannot be matched', add an explicit CAST "
                    "to the mismatched branch (e.g., '::date' or '::text')."
                ),
            ))

    def _check_implicit_type_coercion(self, ddl: str, fqn: str, obj_type: str, result: "AssessmentResult"):
        """MSSQL: ObjectKey (varchar) joined to integer ID columns — PG requires explicit cast."""
        if obj_type != "view":
            return
        if (re.search(r'\bObjectKey\b', ddl, re.IGNORECASE)
                and re.search(r'\b(?:OrderID|ItemID|ClientID|VendorID|UserID|PersonID)\b', ddl, re.IGNORECASE)):
            result.warn_findings.append(Finding(
                code="SPG-WARN-013",
                severity=EWISeverity.WARN,
                title="Potential implicit integer/text coercion in JOIN",
                object_fqn=fqn,
                object_type=obj_type,
                detail=(
                    "View joins a varchar column (ObjectKey) to an integer ID column. "
                    "T-SQL coerces types implicitly; PostgreSQL requires an explicit cast. "
                    "If deployment fails with 'operator does not exist: integer = text', "
                    "add '::integer' or '::text' to the JOIN ON clause."
                ),
            ))


def format_report(result: AssessmentResult, source_desc: str = "") -> str:
    """Format a human-readable SPG Compatibility Assessment Report."""
    from datetime import datetime, timezone
    lines = [
        "=" * 63,
        "spgloader — SPG Compatibility Assessment",
        "=" * 63,
    ]
    if source_desc:
        lines.append(f"Source:      {source_desc}")
    lines += [
        f"Objects:     {result.total_objects}",
        f"Generated:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "OBJECT INVENTORY",
        "=" * 40,
        f"{'Type':<22} {'Count':>5}  {'catalog':>10}  {'LLM conversion':>15}",
        "-" * 60,
    ]
    from collections import Counter
    # We don't have full object list here, just summarize from result
    pgloader_count = len(result.catalog_eligible)
    llm_count = len(result.llm_required)
    lines.append(f"  Total            {result.total_objects:>5}  {pgloader_count:>10}  {llm_count:>15}")
    lines += [
        "",
        f"CONVERSION CONFIDENCE: {result.conversion_confidence:.0%}",
        "",
    ]

    if result.block_findings:
        lines += [
            "BLOCKED — Migration cannot proceed",
            "=" * 40,
        ]
        for f in result.block_findings:
            lines.append(f"  [{f.code}] {f.object_fqn}: {f.detail}")
        lines.append("")

    if result.warn_findings:
        lines += [
            "WARNINGS — Review required before proceeding",
            "=" * 40,
        ]
        for f in result.warn_findings:
            lines.append(f"  [{f.code}] {f.object_fqn}: {f.title}")
        lines.append("")

    if result.resolve_findings:
        lines += [
            "EXTENSION PREREQUISITES (auto-generated)",
            "=" * 40,
        ]
        for f in result.resolve_findings:
            lines.append(f"  {f.auto_resolution}")
        lines.append("")

    status = "BLOCKED" if result.is_blocked else "PASSED"
    lines += [f"Assessment status: {status}", "=" * 63]
    return "\n".join(lines)
