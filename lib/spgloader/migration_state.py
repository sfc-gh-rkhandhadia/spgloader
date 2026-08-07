"""
migration_state.py — Single source of truth for migration phase results.

All deploy scripts (deploy_views, deploy_functions, deploy_procedures,
parallel_deploy) write their results here in a canonical schema.
html_report.py reads from this file first; if absent it falls back to the
legacy per-script JSON files.

Layout: .spgloader/migration_state.json (beside manifest.json)

Schema (schema_version=1):
  {
    "schema_version": 1,
    "updated_at": "...",
    "views":      DeployPhaseState,
    "functions":  DeployPhaseState,
    "procedures": DeployPhaseState,
    "tables": {
      "<db>": { "ok": N, "fail": M, "indexes_ok": K, "fk_ok": J, "fk_fail": L },
      ...
    },
    "parity": {
      "source_type": "mysql",
      "schemas": { "<schema>": {...} },
      "grand": { "pass": N, "fail": M, "missing": K, "spg_only": 0 },
      "_is_structural": true,
      "timestamp": "..."
    }
  }

  DeployPhaseState:
  {
    "succeeded": [fqn, ...],
    "failed":    [{"fqn": ..., "error": ...}, ...],
    "skipped":   [fqn, ...],
    "input_file_count": N,    # files found in wave dir
    "accounted_for":    N,    # must equal input_file_count
    "timestamp": "..."
  }
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_FILE = ".spgloader/migration_state.json"
SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostconditionError(RuntimeError):
    """Raised when a deploy phase output does not account for all input files."""


# ---------------------------------------------------------------------------
# MigrationState
# ---------------------------------------------------------------------------

class MigrationState:
    """Read/write the canonical migration_state.json file."""

    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir).resolve()
        self._path = self.work_dir / STATE_FILE
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self._path.exists():
            try:
                d = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except (json.JSONDecodeError, OSError):
                pass
        return {"schema_version": SCHEMA_VERSION}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data["schema_version"] = SCHEMA_VERSION
        self._data["updated_at"] = _utcnow()
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Postcondition check
    # ------------------------------------------------------------------

    @staticmethod
    def postcondition_check(
        phase: str,
        wave_dir: Path | None,
        succeeded: list,
        failed: list,
        skipped: list,
        *,
        strict: bool = True,
    ) -> tuple[int, int]:
        """Assert that every file in wave_dir is accounted for in the results.

        Returns (input_file_count, accounted_for).
        Raises PostconditionError when strict=True and they diverge.

        A file is "accounted for" if it appears in succeeded, failed, or skipped.
        This check prevents silent zeros in the report caused by files that were
        neither deployed nor logged as failures.
        """
        if wave_dir is None or not Path(wave_dir).exists():
            return 0, len(succeeded) + len(failed) + len(skipped)

        sql_files = list(Path(wave_dir).glob("*.sql"))
        input_count = len(sql_files)
        accounted = len(succeeded) + len(failed) + len(skipped)

        if input_count != accounted:
            gap = input_count - accounted
            msg = (
                f"[{phase}] Postcondition FAILED — {gap} file(s) unaccounted for.\n"
                f"  Input files  : {input_count}  ({wave_dir})\n"
                f"  Succeeded    : {len(succeeded)}\n"
                f"  Failed       : {len(failed)}\n"
                f"  Skipped      : {len(skipped)}\n"
                f"  Accounted    : {accounted}\n"
                f"Every .sql file in wave_dir must appear in exactly one bucket.\n"
                f"Silent skips cause phantom zeros in the migration report."
            )
            if strict:
                raise PostconditionError(msg)
            print(f"WARNING: {msg}")

        return input_count, accounted

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record_deploy_phase(
        self,
        phase: str,
        succeeded: list[str],
        failed: list[dict],
        skipped: list[str],
        wave_dir: Path | None = None,
        *,
        strict_postcondition: bool = True,
    ) -> None:
        """Record the result of a single deploy phase (views/functions/procedures).

        Runs postcondition_check before writing — raises PostconditionError if
        any wave_dir files are unaccounted for.

        Args:
            phase:      "views", "functions", "procedures", or "triggers"
            succeeded:  list of FQN strings that deployed successfully
            failed:     list of {"fqn": ..., "error": ...} dicts
            skipped:    list of FQN strings excluded by user (legacy, FIX-REQUIRED)
            wave_dir:   path to the wave_N_*/  converted SQL directory
            strict_postcondition: if True, raise on unaccounted files; else warn
        """
        input_count, accounted = self.postcondition_check(
            phase, wave_dir, succeeded, failed, skipped,
            strict=strict_postcondition,
        )
        self._data[phase] = {
            "succeeded":        succeeded,
            "failed":           failed,
            "skipped":          skipped,
            "input_file_count": input_count,
            "accounted_for":    accounted,
            "timestamp":        _utcnow(),
        }
        self._save()

    def record_tables(
        self,
        schema: str,
        tables_ok: int,
        tables_fail: int,
        indexes_ok: int = 0,
        indexes_fail: int = 0,
        fk_ok: int = 0,
        fk_fail: int = 0,
        elapsed_s: float = 0.0,
    ) -> None:
        """Record table/index/FK deploy results for one source database/schema.

        Called once per database by parallel_deploy.py. Accumulates across schemas.
        """
        if "tables" not in self._data:
            self._data["tables"] = {}
        self._data["tables"][schema] = {
            "ok":          tables_ok,
            "fail":        tables_fail,
            "indexes_ok":  indexes_ok,
            "indexes_fail":indexes_fail,
            "fk_ok":       fk_ok,
            "fk_fail":     fk_fail,
            "elapsed_s":   elapsed_s,
        }
        self._save()

    def record_parity(
        self,
        source_type: str,
        schemas: dict,
        grand: dict,
        *,
        is_structural: bool = True,
    ) -> None:
        """Record structural parity results in the canonical format.

        Both mysql_structural_parity.py and full_validation.py must call this
        so html_report.py always reads the same schema regardless of source type.

        grand must have: {"pass": N, "fail": M, "missing": K, "spg_only": J}
        schemas keys must match the parity_structural.json schema used by html_report.
        """
        self._data["parity"] = {
            "source_type":    source_type,
            "schemas":        schemas,
            "grand":          grand,
            "_is_structural": is_structural,
            "timestamp":      _utcnow(),
        }
        self._save()

    # ------------------------------------------------------------------
    # Read API for html_report.py
    # ------------------------------------------------------------------

    def get_phase(self, phase: str) -> dict | None:
        """Return the recorded result for a deploy phase, or None if not run."""
        return self._data.get(phase)

    def get_parity(self) -> dict | None:
        """Return parity results, or None if not run."""
        return self._data.get("parity")

    def get_tables(self) -> dict:
        """Return table deploy results keyed by schema. Empty dict if not run."""
        return self._data.get("tables", {})

    def has_phase(self, phase: str) -> bool:
        return phase in self._data

    @classmethod
    def load(cls, work_dir: str | Path) -> "MigrationState":
        """Load (or create empty) MigrationState for a workspace directory."""
        return cls(work_dir)

    @classmethod
    def exists(cls, work_dir: str | Path) -> bool:
        """Return True if migration_state.json exists for this workspace."""
        return (Path(work_dir) / STATE_FILE).exists()

    def to_report_context(self) -> dict | None:
        """Return a dict for html_report.py's collect_context() to merge.

        Returns None if migration_state.json has not been written yet
        (pre-Phase-5 workspaces) so html_report falls back to legacy files.

        The returned dict has exactly the keys collect_context() normally
        derives from its 16 individual files, so the merge is drop-in.
        """
        has_any = any(k in self._data for k in ("views", "functions", "procedures", "tables", "parity"))
        if not has_any:
            return None

        views_phase    = self._data.get("views",      {})
        funcs_phase    = self._data.get("functions",   {})
        procs_phase    = self._data.get("procedures",  {})
        tables_data    = self._data.get("tables",      {})
        parity_data    = self._data.get("parity",      None)

        # ── views ──────────────────────────────────────────────────────────
        views_ok   = views_phase.get("succeeded", [])
        views_fail = [
            {"view": f.get("fqn", f) if isinstance(f, dict) else f,
             "error": f.get("error", "") if isinstance(f, dict) else ""}
            for f in views_phase.get("failed", [])
        ]
        views_skip = views_phase.get("skipped", [])

        # ── functions ──────────────────────────────────────────────────────
        funcs_ok   = funcs_phase.get("succeeded", [])
        funcs_fail = funcs_phase.get("failed", [])

        # ── procedures / triggers ──────────────────────────────────────────
        procs_ok       = procs_phase.get("succeeded", [])
        procs_fail     = procs_phase.get("failed", [])
        procs_legacy   = procs_phase.get("skipped", [])

        # ── tables (aggregate) ─────────────────────────────────────────────
        total_tables  = sum(v.get("ok", 0)         for v in tables_data.values())
        total_indexes = sum(v.get("indexes_ok", 0) for v in tables_data.values())

        return {
            "_from_migration_state": True,   # sentinel so html_report can tell
            # views
            "views_ok":    views_ok,
            "views_fail":  views_fail,
            "views_skip":  views_skip,
            # functions
            "funcs_ok":    funcs_ok,
            "funcs_fail":  funcs_fail,
            # procedures
            "procs_ok":    procs_ok,
            "procs_fail":  procs_fail,
            "procs_legacy":procs_legacy,
            # tables
            "total_tables":  total_tables,
            "total_indexes": total_indexes,
            "tables_by_schema": tables_data,
            # parity (canonical — no fallback logic needed)
            "parity_results":    parity_data,
            "parity_structured": bool(parity_data),
            "parity_ran":        bool(parity_data),
        }
