"""
test_workspace_contract.py — Validates workspace artifacts match the data contracts
defined in CONTRACTS.md.

Run with:
    pytest tests/test_workspace_contract.py -v
    pytest tests/test_workspace_contract.py -v --workspace /path/to/workspace

These tests can run in two modes:
  1. Against a live workspace (pass --workspace flag)
  2. Against synthetic fixtures (default — tests contract schemas only)
"""
import json
import pytest
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Schema validators (reusable by other scripts)
# ---------------------------------------------------------------------------

def validate_deployment_summary(data: dict) -> list[str]:
    """Validate deployment_summary.json structure. Returns list of errors."""
    errors = []
    if not isinstance(data, dict):
        return ["deployment_summary.json is not a dict"]

    if "phases" not in data:
        errors.append("Missing 'phases' key")
    else:
        phases = data["phases"]
        for phase in ("tables", "indexes", "foreign_keys"):
            if phase not in phases:
                errors.append(f"phases.{phase} missing")
            elif "ok" not in phases[phase]:
                errors.append(f"phases.{phase}.ok missing")

    if "source_db" not in data:
        errors.append("Missing 'source_db' key")
    if "failures" not in data:
        errors.append("Missing 'failures' key")
    elif not isinstance(data["failures"], list):
        errors.append("'failures' must be a list")

    return errors


def validate_migration_state(data: dict) -> list[str]:
    """Validate migration_state.json structure."""
    errors = []
    if not isinstance(data, dict):
        return ["migration_state.json is not a dict"]

    for section in ("views", "functions", "procedures"):
        if section not in data:
            # Not all sections are required (e.g. functions may not exist for some sources)
            continue
        sec = data[section]
        if "succeeded" not in sec:
            errors.append(f"{section}.succeeded missing")
        elif not isinstance(sec["succeeded"], list):
            errors.append(f"{section}.succeeded must be a list")
        if "failed" not in sec:
            errors.append(f"{section}.failed missing")
        elif not isinstance(sec["failed"], list):
            errors.append(f"{section}.failed must be a list")

    return errors


def validate_catalog_verification(data: dict) -> list[str]:
    """Validate catalog_verification.json structure."""
    errors = []
    if not isinstance(data, dict):
        return ["catalog_verification.json is not a dict"]

    if "summary" not in data:
        errors.append("Missing 'summary' key")
    else:
        s = data["summary"]
        for obj_type in ("tables", "views", "functions", "procedures", "triggers"):
            if f"{obj_type}_total" not in s:
                errors.append(f"summary.{obj_type}_total missing")

    if "objects" not in data:
        errors.append("Missing 'objects' key")
    elif not isinstance(data["objects"], list):
        errors.append("'objects' must be a list")
    elif data["objects"]:
        obj = data["objects"][0]
        for key in ("source_fqn", "type", "status"):
            if key not in obj:
                errors.append(f"objects[0].{key} missing")

    return errors


def validate_validation_report(data: dict) -> list[str]:
    """Validate validation_report.json structure."""
    errors = []
    if not isinstance(data, dict):
        return ["validation_report.json is not a dict"]

    if "checks" not in data:
        errors.append("Missing 'checks' key")
    elif not isinstance(data["checks"], list):
        errors.append("'checks' must be a list")
    elif data["checks"]:
        check = data["checks"][0]
        for key in ("check", "passed", "source_count", "spg_count", "details"):
            if key not in check:
                errors.append(f"checks[0].{key} missing")

    return errors


def validate_deploy_report(data: dict) -> list[str]:
    """Validate deploy_report.json (views) structure."""
    errors = []
    if not isinstance(data, dict):
        return ["deploy_report.json is not a dict"]

    if "succeeded" not in data:
        errors.append("Missing 'succeeded' key")
    elif not isinstance(data["succeeded"], list):
        errors.append("'succeeded' must be a list")

    if "failed" not in data:
        errors.append("Missing 'failed' key")
    elif not isinstance(data["failed"], list):
        errors.append("'failed' must be a list")

    return errors


def validate_procedures_deploy_report(data: dict) -> list[str]:
    """Validate procedures_deploy_report.json structure."""
    errors = []
    if not isinstance(data, dict):
        return ["procedures_deploy_report.json is not a dict"]

    if "succeeded" not in data:
        errors.append("Missing 'succeeded' key")
    if "failed" not in data:
        errors.append("Missing 'failed' key")
    elif isinstance(data["failed"], list):
        for i, f in enumerate(data["failed"]):
            if isinstance(f, dict):
                if "procedure" not in f and "function" not in f:
                    errors.append(f"failed[{i}]: missing 'procedure' or 'function' key")
                if "error" not in f:
                    errors.append(f"failed[{i}]: missing 'error' key")

    return errors


# ---------------------------------------------------------------------------
# Tests against synthetic data (always run)
# ---------------------------------------------------------------------------

class TestDeploymentSummaryContract:
    def test_valid(self):
        data = {
            "source_db": "TestDB",
            "phases": {
                "schemas": {"ok": 2, "failed": 0},
                "sequences": {"ok": 0, "failed": 0},
                "tables": {"ok": 10, "failed": 0},
                "indexes": {"ok": 5, "failed": 0},
                "foreign_keys": {"ok": 3, "failed": 0},
            },
            "failures": [],
            "elapsed_s": 10.0,
        }
        assert validate_deployment_summary(data) == []

    def test_missing_phases(self):
        data = {"source_db": "TestDB", "failures": []}
        errors = validate_deployment_summary(data)
        assert "Missing 'phases' key" in errors

    def test_flat_structure_rejected(self):
        """The old flat format that caused the 0/0 bug must be rejected."""
        data = {"schemas": {"ok": 6}, "tables": {"ok": 71}, "failures": []}
        errors = validate_deployment_summary(data)
        assert "Missing 'phases' key" in errors


class TestMigrationStateContract:
    def test_valid(self):
        data = {
            "schema_version": 1,
            "views": {"succeeded": ["a.v1"], "failed": [], "skipped": []},
            "functions": {"succeeded": ["a.f1"], "failed": []},
            "procedures": {"succeeded": ["a.p1"], "failed": []},
        }
        assert validate_migration_state(data) == []

    def test_views_missing_succeeded(self):
        data = {"views": {"failed": []}}
        errors = validate_migration_state(data)
        assert "views.succeeded missing" in errors


class TestCatalogVerificationContract:
    def test_valid(self):
        data = {
            "summary": {
                "tables_total": 10, "tables_match": 10,
                "views_total": 5, "views_match": 5,
                "functions_total": 3, "functions_match": 3,
                "procedures_total": 2, "procedures_match": 2,
                "triggers_total": 1, "triggers_match": 1,
            },
            "objects": [{"source_fqn": "dbo.t1", "type": "table", "status": "match"}],
        }
        assert validate_catalog_verification(data) == []

    def test_missing_objects(self):
        data = {"summary": {"tables_total": 1}}
        errors = validate_catalog_verification(data)
        assert "Missing 'objects' key" in errors


class TestValidationReportContract:
    def test_valid(self):
        data = {
            "checks": [{"check": "Tables", "passed": True,
                        "source_count": 10, "spg_count": 10,
                        "details": "10/10 deployed"}]
        }
        assert validate_validation_report(data) == []

    def test_empty_checks_passes(self):
        """Empty checks list is valid (just means no checks ran)."""
        data = {"checks": []}
        assert validate_validation_report(data) == []

    def test_missing_checks_key(self):
        data = {"objects": []}
        errors = validate_validation_report(data)
        assert "Missing 'checks' key" in errors


# ---------------------------------------------------------------------------
# Tests against live workspace (only run when --workspace is provided)
# ---------------------------------------------------------------------------

class TestLiveWorkspace:
    def test_deployment_summary(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / "deployment" / "deployment_summary.json"
        if not f.exists():
            pytest.skip("deployment_summary.json not found")
        data = json.loads(f.read_text())
        errors = validate_deployment_summary(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_migration_state(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / ".spgloader" / "migration_state.json"
        if not f.exists():
            pytest.skip("migration_state.json not found")
        data = json.loads(f.read_text())
        errors = validate_migration_state(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_catalog_verification(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / "validation" / "catalog_verification.json"
        if not f.exists():
            pytest.skip("catalog_verification.json not found")
        data = json.loads(f.read_text())
        errors = validate_catalog_verification(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_validation_report(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / "validation" / "validation_report.json"
        if not f.exists():
            pytest.skip("validation_report.json not found")
        data = json.loads(f.read_text())
        errors = validate_validation_report(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_deploy_report(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / "conversion" / "deploy_report.json"
        if not f.exists():
            pytest.skip("deploy_report.json not found")
        data = json.loads(f.read_text())
        errors = validate_deploy_report(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_procedures_deploy_report(self, workspace):
        if not workspace:
            pytest.skip("No --workspace provided")
        f = workspace / "conversion" / "procedures_deploy_report.json"
        if not f.exists():
            pytest.skip("procedures_deploy_report.json not found")
        data = json.loads(f.read_text())
        errors = validate_procedures_deploy_report(data)
        assert errors == [], f"Contract violations: {errors}"

    def test_views_count_consistency(self, workspace):
        """migration_state views count must match what's actually in SPG."""
        if not workspace:
            pytest.skip("No --workspace provided")
        state_f = workspace / ".spgloader" / "migration_state.json"
        deploy_f = workspace / "conversion" / "deploy_report.json"
        if not state_f.exists() or not deploy_f.exists():
            pytest.skip("Required files not found")
        state = json.loads(state_f.read_text())
        deploy = json.loads(deploy_f.read_text())
        state_count = len(state.get("views", {}).get("succeeded", []))
        deploy_count = len(deploy.get("succeeded", []))
        assert state_count == deploy_count, (
            f"migration_state shows {state_count} views but deploy_report shows {deploy_count}")
