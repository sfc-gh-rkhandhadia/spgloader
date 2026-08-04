"""
tests/test_report_regression.py — Regression tests for html_report.py count logic.

These tests use a minimal fixture workspace to verify that report counts
(views IN SPG, migration %, procedure LLM-fixed, etc.) are computed correctly.
They catch the class of silent counting bugs found in the AdventureWorks session
(views_ok=11 vs 13, procs Fixed by LLM=19 vs 9, etc.).

Run with:
    uv run --project /path/to/spgloader pytest tests/test_report_regression.py -v
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SKILL_ROOT / "lib"))


# ---------------------------------------------------------------------------
# Minimal fixture workspace builder
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path, deploy_report=None, fix_report=None,
                    repair_report=None, procedures_deploy=None,
                    functions_deploy=None, ddl_objects=None,
                    deployment_summary=None):
    """Create a minimal workspace directory with required JSON files."""
    ws = tmp_path / "workspace"
    (ws / "conversion" / "postgres").mkdir(parents=True)
    (ws / "deployment").mkdir()
    (ws / "assessment").mkdir()
    (ws / "validation").mkdir()

    # defaults
    _deploy = deploy_report or {"succeeded": [], "failed": [], "skipped": [], "auto_fixed": []}
    _fix    = fix_report    or {"succeeded": [], "failed": [], "fix_details": {}}
    _repair = repair_report or {"fixed_llm": [], "fixed_rules": [], "still_failed": []}
    _procs  = procedures_deploy or {"succeeded": [], "failed": [], "skipped_legacy": []}
    _funcs  = functions_deploy or {"succeeded": [], "failed": []}
    _ddl    = ddl_objects   or []
    _summary = deployment_summary or {
        "source_type": "mssql", "source_db": "testdb", "spg_service": "test_spg",
        "elapsed_s": 1.0,
        "phases": {
            "schemas":      {"ok": 1, "fail": 0},
            "tables":       {"ok": 3, "fail": 0},
            "indexes":      {"ok": 5, "fail": 0},
            "foreign_keys": {"ok": 2, "fail": 0},
            "sequences":    {"ok": 0, "fail": 0},
        },
        "total_ok": 10, "total_fail": 0, "failures": [],
    }

    (ws / "conversion" / "deploy_report.json").write_text(json.dumps(_deploy))
    (ws / "conversion" / "fix_report.json").write_text(json.dumps(_fix))
    (ws / "conversion" / "repair_report.json").write_text(json.dumps(_repair))
    (ws / "conversion" / "procedures_deploy_report.json").write_text(json.dumps(_procs))
    (ws / "conversion" / "functions_deploy_report.json").write_text(json.dumps(_funcs))
    (ws / "conversion" / "_conversion_report.json").write_text(json.dumps({}))
    (ws / "ddl_objects.json").write_text(json.dumps(_ddl))
    (ws / "deployment" / "deployment_summary.json").write_text(json.dumps(_summary))
    (ws / "assessment" / "assessment_summary.json").write_text(json.dumps(
        {"is_blocked": False, "warn_findings": [], "ext_prereqs": [], "dep_groups": {}}
    ))
    return ws


@pytest.fixture
def report():
    from spgloader.reporting.html_report import load_workspace_data
    return load_workspace_data


# ---------------------------------------------------------------------------
# Views: deploy_report is authoritative; fix_report does not inflate views_ok
# ---------------------------------------------------------------------------

class TestViewCounts:

    def test_views_ok_from_deploy_report_only(self, report, tmp_path):
        """views_ok must come from deploy_report.succeeded only, not fix_report."""
        ws = _make_workspace(
            tmp_path,
            deploy_report={
                "succeeded": ["dbo.v_orders", "dbo.v_customers"],
                "failed":    [],
                "skipped":   [],
                "auto_fixed": [],
            },
            fix_report={
                # fix_report has 5 files but they're transformations, not deployments
                "succeeded": ["dbo__v_orders.sql", "dbo__v_customers.sql",
                              "dbo__v_xml1.sql", "dbo__v_xml2.sql", "dbo__v_xml3.sql"],
                "failed": [],
            },
        )
        data = report(ws)
        assert len(data["views_ok"]) == 2, (
            f"views_ok should be 2 (from deploy_report.succeeded), got {len(data['views_ok'])}"
        )
        assert len(data["views_fixed"]) == 0, (
            f"views_fixed (Fixed by Rules) should be 0, got {len(data['views_fixed'])}"
        )

    def test_views_fail_deduplicates(self, report, tmp_path):
        """deploy_report.failed may have duplicate entries — they must be deduplicated."""
        ws = _make_workspace(
            tmp_path,
            deploy_report={
                "succeeded": [],
                "failed": [
                    # same view appears twice (two deploy attempts)
                    {"view": "dbo.v_xml1", "error": "xquery not supported"},
                    {"view": "dbo.v_xml1", "error": "xquery not supported"},
                    {"view": "dbo.v_xml2", "error": "xquery not supported"},
                ],
                "skipped": [],
                "auto_fixed": [],
            },
        )
        data = report(ws)
        assert len(data["views_fail"]) == 2, (
            f"views_fail should be 2 unique, got {len(data['views_fail'])}"
        )

    def test_skipped_views_counted_separately(self, report, tmp_path):
        """deploy_report.skipped must appear in views_skip (not views_fail)."""
        ws = _make_workspace(
            tmp_path,
            deploy_report={
                "succeeded": ["dbo.v_ok"],
                "failed":    [{"view": "dbo.v_xml", "error": "xquery"}],
                "skipped":   ["dbo__v_pivot.sql"],
                "auto_fixed": [],
            },
        )
        data = report(ws)
        assert len(data["views_ok"])   == 1
        assert len(data["views_fail"]) == 1
        assert len(data["views_skip"]) == 1


# ---------------------------------------------------------------------------
# Procedures: trigger functions must not inflate proc counts
# ---------------------------------------------------------------------------

class TestProcedureCounts:

    def test_trigger_fns_separated_from_procs(self, report, tmp_path):
        """Trigger functions (iuperson_fn, etc.) must NOT appear in procs_ok."""
        ddl_objects = [
            {"type": "trigger", "schema": "dbo", "name": "iuperson",
             "fqn": "dbo.iuperson", "ddl": ""},
            {"type": "trigger", "schema": "dbo", "name": "dorder",
             "fqn": "dbo.dorder", "ddl": ""},
        ]
        ws = _make_workspace(
            tmp_path,
            ddl_objects=ddl_objects,
            procedures_deploy={
                "succeeded": [
                    "dbo.usp_get_customer",    # real proc
                    "dbo.usp_update_order",    # real proc
                    "iuperson_fn",             # trigger function — must be separated
                    "dorder_fn",               # trigger function — must be separated
                ],
                "failed":          [],
                "skipped_legacy":  [],
            },
        )
        data = report(ws)
        assert len(data["procs_ok"]) == 2, (
            f"procs_ok should be 2 real procs, got {len(data['procs_ok'])}: {data['procs_ok']}"
        )
        assert len(data["triggers_ok"]) == 2, (
            f"triggers_ok should be 2 trigger fns, got {len(data['triggers_ok'])}"
        )

    def test_demployee_fn_classified_as_trigger_fail(self, report, tmp_path):
        """A trigger function in procedures_deploy.failed → triggers_fail, not procs_fail."""
        ddl_objects = [
            {"type": "trigger", "schema": "humanresources", "name": "demployee",
             "fqn": "humanresources.demployee", "ddl": ""},
        ]
        ws = _make_workspace(
            tmp_path,
            ddl_objects=ddl_objects,
            procedures_deploy={
                "succeeded": [],
                "failed": [
                    {"procedure": "demployee_fn",
                     "file": "humanresources__demployee.sql",
                     "error": "relation already exists"},
                ],
                "skipped_legacy": [],
            },
        )
        data = report(ws)
        assert len(data["procs_fail"])    == 0, "demployee_fn should be in triggers_fail, not procs_fail"
        assert len(data["triggers_fail"]) == 1, "demployee_fn should be in triggers_fail"


# ---------------------------------------------------------------------------
# LLM repair counts split by type
# ---------------------------------------------------------------------------

class TestRepairCounts:

    def test_repair_split_proc_vs_trigger(self, report, tmp_path):
        """repair_report.fixed_llm splits into proc_llm_fixed and trig_llm_fixed."""
        ddl_objects = [
            {"type": "trigger", "schema": "dbo", "name": "iuperson",
             "fqn": "dbo.iuperson", "ddl": ""},
        ]
        ws = _make_workspace(
            tmp_path,
            ddl_objects=ddl_objects,
            repair_report={
                "fixed_llm": [
                    "dbo.usp_get_data",      # real proc
                    "dbo.usp_update",        # real proc
                    "iuperson_fn",           # trigger fn
                ],
                "fixed_rules":  [],
                "still_failed": [],
            },
        )
        data = report(ws)
        assert len(data["proc_llm_fixed"]) == 2, (
            f"proc_llm_fixed should be 2, got {len(data['proc_llm_fixed'])}"
        )
        assert len(data["trig_llm_fixed"]) == 1, (
            f"trig_llm_fixed should be 1, got {len(data['trig_llm_fixed'])}"
        )

    def test_repair_report_19_splits_correctly(self, report, tmp_path):
        """The AdventureWorks case: 9 procs + 10 trigger fns → must not show 19 in proc row."""
        ddl_objects = [
            {"type": "trigger", "schema": "dbo", "name": f"trig{i}",
             "fqn": f"dbo.trig{i}", "ddl": ""}
            for i in range(10)
        ]
        ws = _make_workspace(
            tmp_path,
            ddl_objects=ddl_objects,
            repair_report={
                "fixed_llm": (
                    [f"dbo.usp_proc{i}" for i in range(9)] +
                    [f"trig{i}_fn" for i in range(10)]
                ),
                "fixed_rules":  [],
                "still_failed": [],
            },
        )
        data = report(ws)
        assert len(data["proc_llm_fixed"]) == 9
        assert len(data["trig_llm_fixed"]) == 10


# ---------------------------------------------------------------------------
# Migration % calculation
# ---------------------------------------------------------------------------

class TestMigrationPct:

    def test_migration_pct_excludes_indexes(self, report, tmp_path):
        """Migration % uses application objects only — indexes must not inflate it."""
        ws = _make_workspace(
            tmp_path,
            deploy_report={
                "succeeded": [f"dbo.v_view{i}" for i in range(5)],
                "failed":    [{"view": f"dbo.v_fail{i}", "error": "x"} for i in range(5)],
                "skipped":   [],
                "auto_fixed": [],
            },
            deployment_summary={
                "source_type": "mssql", "source_db": "testdb",
                "spg_service": "test_spg", "elapsed_s": 1.0,
                "phases": {
                    "schemas":      {"ok": 1, "fail": 0},
                    "tables":       {"ok": 10, "fail": 0},
                    "indexes":      {"ok": 100, "fail": 50},  # 150 indexes — must NOT count
                    "foreign_keys": {"ok": 10, "fail": 0},
                    "sequences":    {"ok": 0,  "fail": 0},
                },
                "total_ok": 121, "total_fail": 50, "failures": [],
            },
        )
        data = report(ws)
        # mig_ok = 10 tables + 5 views = 15
        # mig_total = 10 tables + (5+5) views = 20
        # pct = 15/20 = 75% — indexes (100/150) must NOT change this
        total_views = len(data["views_ok"])
        total_tables = data["total_tables"]
        mig_ok = total_tables + total_views
        mig_src_views = total_views + len(data["views_fail"]) + len(data["views_skip"])
        mig_total = total_tables + mig_src_views
        expected_pct = round(mig_ok / mig_total * 100) if mig_total else 100
        assert expected_pct == 75, f"Expected 75% migration, got {expected_pct}%"
        assert total_tables == 10


# ---------------------------------------------------------------------------
# Schema count
# ---------------------------------------------------------------------------

class TestSchemaCount:

    def test_schema_count_from_deployment_summary(self, report, tmp_path):
        """total_schema_count reads phases.schemas.ok from deployment_summary.json."""
        ws = _make_workspace(
            tmp_path,
            deployment_summary={
                "source_type": "mssql", "source_db": "testdb",
                "spg_service": "test_spg", "elapsed_s": 1.0,
                "phases": {
                    "schemas":  {"ok": 6, "fail": 0},
                    "tables":   {"ok": 71, "fail": 0},
                    "indexes":  {"ok": 98, "fail": 4},
                    "foreign_keys": {"ok": 90, "fail": 0},
                    "sequences": {"ok": 0, "fail": 0},
                },
                "total_ok": 265, "total_fail": 4, "failures": [],
            },
        )
        data = report(ws)
        assert data["total_schema_count"] == 6, (
            f"Expected 6 schemas from phases.schemas.ok, got {data['total_schema_count']}"
        )
