"""
workspace_validator.py — Validate workspace artifacts at runtime.

Called automatically by pipeline scripts after writing output files.
Raises WorkspaceContractError if any artifact violates its contract.

Usage from any spgloader script:
    from spgloader.workspace_validator import validate_after_deploy, validate_before_report
"""
import json
from pathlib import Path


class WorkspaceContractError(Exception):
    """Raised when a workspace artifact violates its data contract."""
    pass


def validate_deployment_summary(ws: Path) -> list[str]:
    """Validate deployment_summary.json after parallel_deploy.py writes it."""
    f = ws / "deployment" / "deployment_summary.json"
    if not f.exists():
        return [f"deployment_summary.json not written to {f}"]
    data = json.loads(f.read_text())
    errors = []
    if "phases" not in data:
        errors.append("deployment_summary.json: missing 'phases' key (will show 0/0 in report)")
    else:
        for phase in ("tables", "indexes", "foreign_keys"):
            if phase not in data["phases"]:
                errors.append(f"deployment_summary.json: phases.{phase} missing")
            elif "ok" not in data["phases"][phase]:
                errors.append(f"deployment_summary.json: phases.{phase}.ok missing")
    if "source_db" not in data:
        errors.append("deployment_summary.json: missing 'source_db'")
    if "failures" not in data or not isinstance(data.get("failures"), list):
        errors.append("deployment_summary.json: missing or invalid 'failures' list")
    return errors


def validate_migration_state_views(ws: Path) -> list[str]:
    """Validate migration_state.json views section after deploy/repair."""
    f = ws / ".spgloader" / "migration_state.json"
    if not f.exists():
        return ["migration_state.json not found"]
    data = json.loads(f.read_text())
    errors = []
    if "views" not in data:
        errors.append("migration_state.json: missing 'views' section")
    else:
        v = data["views"]
        if "succeeded" not in v:
            errors.append("migration_state.json: views.succeeded missing")
        elif not isinstance(v["succeeded"], list):
            errors.append("migration_state.json: views.succeeded must be a list")
        if "failed" not in v:
            errors.append("migration_state.json: views.failed missing")
    return errors


def validate_catalog_verification(ws: Path) -> list[str]:
    """Validate catalog_verification.json after catalog_verify.py writes it."""
    f = ws / "validation" / "catalog_verification.json"
    if not f.exists():
        return [f"catalog_verification.json not found at {f}"]
    data = json.loads(f.read_text())
    errors = []
    if "summary" not in data:
        errors.append("catalog_verification.json: missing 'summary'")
    if "objects" not in data or not data["objects"]:
        errors.append("catalog_verification.json: missing or empty 'objects' (catalog tab will be empty)")
    return errors


def validate_validation_report(ws: Path) -> list[str]:
    """Validate validation_report.json has checks for Schema Verification tab."""
    f = ws / "validation" / "validation_report.json"
    if not f.exists():
        return ["validation_report.json not found (Schema Verification tab will be empty)"]
    data = json.loads(f.read_text())
    errors = []
    if "checks" not in data:
        errors.append("validation_report.json: missing 'checks' key")
    elif not data["checks"]:
        errors.append("validation_report.json: 'checks' is empty (Schema Verification tab will be empty)")
    return errors


def validate_after_deploy(ws: Path) -> None:
    """Run after parallel_deploy.py. Raises on contract violation."""
    errors = validate_deployment_summary(ws)
    if errors:
        raise WorkspaceContractError(
            "Deployment output contract violation:\n  " + "\n  ".join(errors))


def validate_after_views(ws: Path) -> None:
    """Run after deploy_views.py + repair. Raises on contract violation."""
    errors = validate_migration_state_views(ws)
    if errors:
        raise WorkspaceContractError(
            "Views state contract violation:\n  " + "\n  ".join(errors))


def validate_after_catalog_verify(ws: Path) -> None:
    """Run after catalog_verify.py. Raises on contract violation."""
    errors = validate_catalog_verification(ws) + validate_validation_report(ws)
    if errors:
        raise WorkspaceContractError(
            "Catalog verification contract violation:\n  " + "\n  ".join(errors))


def validate_before_report(ws: Path) -> list[str]:
    """Run before generate_report.py. Returns warnings (does not raise).

    This is the soft gate — it warns but allows report generation to proceed
    so partial reports can still be generated for debugging.
    """
    warnings = []
    warnings.extend(validate_deployment_summary(ws))
    warnings.extend(validate_migration_state_views(ws))
    # These are optional — only warn if the files exist but are malformed
    cv = ws / "validation" / "catalog_verification.json"
    if cv.exists():
        warnings.extend(validate_catalog_verification(ws))
    vr = ws / "validation" / "validation_report.json"
    if vr.exists():
        warnings.extend(validate_validation_report(ws))
    return warnings
