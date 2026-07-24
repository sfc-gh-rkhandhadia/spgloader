"""
Workspace contract for spgloader — tracks config and phase manifest per project.

Each migration project has a .spgloader/ directory containing:
  config.yaml   — source/target connection details, source type, work dir
  manifest.json — which phases have run, their status, timestamps, artifacts
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


META_DIR = ".spgloader"
CONFIG_FILE = "config.yaml"
MANIFEST_FILE = "manifest.json"

SUBDIRS = ["source", "assessment", "conversion", "conversion/postgres",
           "conversion/pgloader", "deployment", "validation"]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    """Handle to a project's .spgloader/ workspace."""

    def __init__(self, project_dir: str | Path):
        self.root = Path(project_dir).resolve()
        self.meta_dir = self.root / META_DIR

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def init(
        cls,
        project_dir: str | Path,
        source_type: str,
        source_version: str = "",
        source_host: str = "localhost",
        source_port: int | None = None,
        source_database: str = "",
        source_user: str = "",
        source_password_env: str = "",
        target_spg_service: str = "",
    ) -> "Workspace":
        """Create a new workspace in project_dir."""
        ws = cls(project_dir)
        ws.meta_dir.mkdir(parents=True, exist_ok=True)

        default_ports = {"mssql": 1433, "mysql": 3306, "oracle": 1521}
        port = source_port or default_ports.get(source_type, 5432)

        config = {
            "source_type": source_type,
            "source_version": source_version,
            "source_host": source_host,
            "source_port": port,
            "source_database": source_database,
            "source_user": source_user,
            "source_password_env": source_password_env,
            "target_spg_service": target_spg_service,
            "created_at": _utcnow(),
        }
        (ws.meta_dir / CONFIG_FILE).write_text(yaml.dump(config, sort_keys=False))

        # Create output subdirectories
        for subdir in SUBDIRS:
            (ws.root / subdir).mkdir(parents=True, exist_ok=True)

        # Initialize empty manifest
        if not (ws.meta_dir / MANIFEST_FILE).exists():
            (ws.meta_dir / MANIFEST_FILE).write_text(json.dumps({"phases": {}}, indent=2))

        return ws

    @classmethod
    def find(cls, start: str | Path = ".") -> "Workspace":
        """Walk up from start looking for a .spgloader/ directory."""
        cur = Path(start).resolve()
        for candidate in [cur, *cur.parents]:
            if (candidate / META_DIR).is_dir():
                return cls(candidate)
        raise WorkspaceError(
            f"No {META_DIR}/ workspace found at or above {cur}. "
            f"Run: python scripts/assess.py --init --source-type mssql ..."
        )

    # ------------------------------------------------------------------
    # Config access
    # ------------------------------------------------------------------

    def read_config(self) -> dict:
        cfg_file = self.meta_dir / CONFIG_FILE
        if not cfg_file.exists():
            raise WorkspaceError(f"Config file not found: {cfg_file}")
        return yaml.safe_load(cfg_file.read_text()) or {}

    def update_config(self, updates: dict) -> None:
        cfg = self.read_config()
        cfg.update(updates)
        (self.meta_dir / CONFIG_FILE).write_text(yaml.dump(cfg, sort_keys=False))

    # ------------------------------------------------------------------
    # Manifest / phase tracking
    # ------------------------------------------------------------------

    def _read_manifest(self) -> dict:
        mfile = self.meta_dir / MANIFEST_FILE
        if not mfile.exists():
            return {"phases": {}}
        return json.loads(mfile.read_text())

    def _write_manifest(self, manifest: dict) -> None:
        (self.meta_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))

    def record_phase(
        self,
        phase: str,
        status: str,
        artifacts: dict[str, Any] | None = None,
        block_codes: list[str] | None = None,
        warn_codes: list[str] | None = None,
    ) -> None:
        """Record a phase run in the manifest."""
        manifest = self._read_manifest()
        manifest["phases"][phase] = {
            "status": status,
            "completed_at": _utcnow(),
            "artifacts": artifacts or {},
            "block_codes": block_codes or [],
            "warn_codes": warn_codes or [],
        }
        self._write_manifest(manifest)

    def has_run(self, phase: str) -> bool:
        return phase in self._read_manifest().get("phases", {})

    def phase_status(self, phase: str) -> str | None:
        phases = self._read_manifest().get("phases", {})
        return phases.get(phase, {}).get("status")

    def blocked_by(self, phase: str) -> list[str]:
        """
        Return unresolved BLOCK codes from the given phase.

        This is the guardrail gate — call this at the start of Phase 4 (convert)
        to ensure Phase 3.5 (assess) passed without any BLOCK findings.

        Returns [] if phase has not run or has no block codes.
        """
        phases = self._read_manifest().get("phases", {})
        return phases.get(phase, {}).get("block_codes", [])

    def is_assessment_blocked(self) -> bool:
        """Return True if the assessment phase recorded any BLOCK codes."""
        return bool(self.blocked_by("assess"))

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def path(self, subdir: str) -> Path:
        """Resolve a canonical subdirectory path, creating it if needed."""
        p = self.root / subdir
        p.mkdir(parents=True, exist_ok=True)
        return p

    def __repr__(self) -> str:
        return f"Workspace({self.root})"
