"""
manifest.py — Canonical Object Manifest for spgloader.

Single source of truth tracking every migration object through its lifecycle:
  extracted → converted → repaired → deployed → validated

Each object has explicit state tiers with status, timestamp, error, and artifact path.
The manifest is persisted as .spgloader/object_manifest.json and updated incrementally.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_FILENAME = "object_manifest.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class TierState:
    status: str = "pending"       # pending | completed | failed | skipped
    timestamp: str = ""           # ISO 8601 when last updated
    error: str = ""               # empty if no error
    artifact: str = ""            # relative path to output file
    attempts: int = 0             # for repair: LLM iterations attempted

    def mark(self, status: str, error: str = "", artifact: str = "", attempts: int = 0) -> None:
        self.status = status
        self.timestamp = _utcnow()
        self.error = error
        if artifact:
            self.artifact = artifact
        if attempts:
            self.attempts = attempts


@dataclass
class ObjectState:
    fqn: str                              # e.g. "sapphire.proc_import_products"
    obj_type: str = ""                    # TABLE | VIEW | FUNCTION | PROCEDURE | TRIGGER
    schema: str = ""                      # source schema/database name
    source_ddl_hash: str = ""             # short SHA-256 of extracted DDL

    extraction: TierState = field(default_factory=TierState)
    conversion: TierState = field(default_factory=TierState)
    repair: TierState = field(default_factory=TierState)
    deployment: TierState = field(default_factory=TierState)
    validation: TierState = field(default_factory=TierState)

    ewi_codes: list[str] = field(default_factory=list)
    deprecated_disposition: str = ""      # skip | migrate | modernize
    is_excluded: bool = False
    exclusion_reason: str = ""

    def current_tier(self) -> str:
        """Return the highest tier that has been completed."""
        for tier in ("validation", "deployment", "repair", "conversion", "extraction"):
            if getattr(self, tier).status == "completed":
                return tier
        return "pending"

    def is_deployable(self) -> bool:
        """True if the object has passed conversion or repair and is ready to deploy."""
        if self.is_excluded:
            return False
        if self.conversion.status == "completed":
            return True
        if self.repair.status == "completed":
            return True
        return False

    def needs_repair(self) -> bool:
        """True if conversion failed and repair hasn't succeeded yet."""
        return (
            self.conversion.status == "failed"
            and self.repair.status not in ("completed", "skipped")
            and not self.is_excluded
        )

    def needs_deployment(self) -> bool:
        """True if deployable but not yet deployed."""
        return self.is_deployable() and self.deployment.status != "completed"

    def needs_validation(self) -> bool:
        """True if deployed but not yet validated."""
        return (
            self.deployment.status == "completed"
            and self.validation.status not in ("completed", "skipped")
        )


class ObjectManifest:
    """Persistent manifest tracking all objects from extraction through validation."""

    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir).resolve()
        self._meta_dir = self.work_dir / ".spgloader"
        self._path = self._meta_dir / MANIFEST_FILENAME
        self._objects: dict[str, ObjectState] = {}
        self._dirty = False
        self.reload()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Load manifest from disk. If it doesn't exist, start empty."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for fqn, raw in data.get("objects", {}).items():
                    self._objects[fqn] = self._deserialize(fqn, raw)
            except (json.JSONDecodeError, KeyError):
                self._objects = {}
        self._dirty = False

    def save(self) -> None:
        """Write manifest to disk (only if modified)."""
        if not self._dirty:
            return
        self._meta_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": _utcnow(),
            "object_count": len(self._objects),
            "objects": {fqn: self._serialize(obj) for fqn, obj in self._objects.items()},
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._dirty = False

    def _serialize(self, obj: ObjectState) -> dict:
        return asdict(obj)

    def _deserialize(self, fqn: str, raw: dict) -> ObjectState:
        tiers = {}
        for tier_name in ("extraction", "conversion", "repair", "deployment", "validation"):
            tier_raw = raw.get(tier_name, {})
            tiers[tier_name] = TierState(
                status=tier_raw.get("status", "pending"),
                timestamp=tier_raw.get("timestamp", ""),
                error=tier_raw.get("error", ""),
                artifact=tier_raw.get("artifact", ""),
                attempts=tier_raw.get("attempts", 0),
            )
        return ObjectState(
            fqn=fqn,
            obj_type=raw.get("obj_type", ""),
            schema=raw.get("schema", ""),
            source_ddl_hash=raw.get("source_ddl_hash", ""),
            ewi_codes=raw.get("ewi_codes", []),
            deprecated_disposition=raw.get("deprecated_disposition", ""),
            is_excluded=raw.get("is_excluded", False),
            exclusion_reason=raw.get("exclusion_reason", ""),
            **tiers,
        )

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, fqn: str) -> ObjectState | None:
        return self._objects.get(fqn)

    def all_objects(self) -> list[ObjectState]:
        return list(self._objects.values())

    def objects_in_state(self, tier: str, status: str) -> list[ObjectState]:
        """Return objects where the given tier has the given status."""
        return [o for o in self._objects.values() if getattr(o, tier).status == status]

    def objects_needing(self, tier: str) -> list[ObjectState]:
        """Return objects that need processing for the given tier.

        This respects the state machine gating:
          - conversion: needs extraction completed, not excluded
          - repair: needs conversion failed, repair not completed/skipped
          - deployment: needs conversion or repair completed, not yet deployed
          - validation: needs deployment completed, not yet validated
        """
        results = []
        for obj in self._objects.values():
            if obj.is_excluded:
                continue
            if tier == "conversion":
                if obj.extraction.status == "completed" and obj.conversion.status in ("pending", "failed"):
                    # Tables skip conversion (they go straight to deployment via catalog)
                    if obj.obj_type.upper() != "TABLE":
                        results.append(obj)
            elif tier == "repair":
                if obj.needs_repair():
                    results.append(obj)
            elif tier == "deployment":
                if obj.needs_deployment():
                    results.append(obj)
            elif tier == "validation":
                if obj.needs_validation():
                    results.append(obj)
        return results

    def __len__(self) -> int:
        return len(self._objects)

    def __contains__(self, fqn: str) -> bool:
        return fqn in self._objects

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def set_extracted(self, fqn: str, obj_type: str, schema: str,
                     ddl_hash: str = "", artifact: str = "") -> ObjectState:
        """Register an object as extracted from the source."""
        obj = self._objects.get(fqn)
        if obj is None:
            obj = ObjectState(fqn=fqn, obj_type=obj_type, schema=schema)
            self._objects[fqn] = obj
        obj.obj_type = obj_type
        obj.schema = schema
        obj.source_ddl_hash = ddl_hash
        obj.extraction.mark("completed", artifact=artifact)
        self._dirty = True
        return obj

    def set_converted(self, fqn: str, status: str, artifact: str = "",
                      error: str = "", ewi_codes: list[str] | None = None) -> None:
        """Mark an object's conversion tier."""
        obj = self._objects.get(fqn)
        if obj is None:
            return
        obj.conversion.mark(status, error=error, artifact=artifact)
        if ewi_codes is not None:
            obj.ewi_codes = ewi_codes
        self._dirty = True

    def set_repaired(self, fqn: str, status: str, artifact: str = "",
                     error: str = "", attempts: int = 0) -> None:
        """Mark an object's repair tier."""
        obj = self._objects.get(fqn)
        if obj is None:
            return
        obj.repair.mark(status, error=error, artifact=artifact, attempts=attempts)
        self._dirty = True

    def set_deployed(self, fqn: str, status: str, error: str = "") -> None:
        """Mark an object's deployment tier."""
        obj = self._objects.get(fqn)
        if obj is None:
            return
        obj.deployment.mark(status, error=error)
        self._dirty = True

    def set_validated(self, fqn: str, status: str, error: str = "") -> None:
        """Mark an object's validation tier."""
        obj = self._objects.get(fqn)
        if obj is None:
            return
        obj.validation.mark(status, error=error)
        self._dirty = True

    def set_excluded(self, fqn: str, reason: str, disposition: str = "skip") -> None:
        """Mark an object as excluded from migration."""
        obj = self._objects.get(fqn)
        if obj is None:
            return
        obj.is_excluded = True
        obj.exclusion_reason = reason
        obj.deprecated_disposition = disposition
        self._dirty = True

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def seed_from_ddl_objects(self, ddl_objects: list[dict]) -> int:
        """Populate the manifest from the output of extract_ddl.py (ddl_objects.json).

        Returns the number of objects added.
        """
        added = 0
        for obj in ddl_objects:
            fqn = obj.get("fqn") or f"{obj.get('schema', '')}.{obj.get('name', '')}"
            if not fqn or fqn in self._objects:
                continue
            obj_type = (obj.get("type") or "").upper()
            schema = obj.get("schema", "")
            ddl = obj.get("ddl", "")
            ddl_hash = _sha256(ddl) if ddl else ""
            self.set_extracted(fqn, obj_type, schema, ddl_hash=ddl_hash)
            added += 1
        return added

    def backfill_from_deploy_reports(self, work_dir: Path | None = None) -> dict[str, int]:
        """Backfill deployment state from legacy deploy report JSON files.

        Reads procedures_deploy_report.json, functions_deploy_report.json,
        deploy_report.json (views), and per-db deployment_*.json files.
        Returns counts of objects updated per tier.
        """
        ws = work_dir or self.work_dir
        counts: dict[str, int] = {"deployed": 0, "deploy_failed": 0, "converted": 0}

        # Tables from per-db deployment files
        deploy_dir = ws / "deployment"
        if deploy_dir.exists():
            for dp in deploy_dir.glob("deployment_*.json"):
                try:
                    dd = json.loads(dp.read_text())
                    phases = dd.get("phases", {})
                    for result in phases.get("tables", {}).get("results", []):
                        fqn_raw = result.get("fqn", "")
                        if fqn_raw and fqn_raw in self._objects:
                            status = "completed" if result.get("status") in ("OK", "ok", True) else "failed"
                            self.set_deployed(fqn_raw, status, error=result.get("error", ""))
                            counts["deployed" if status == "completed" else "deploy_failed"] += 1
                except (json.JSONDecodeError, KeyError):
                    continue

        # Views
        vr_path = ws / "conversion" / "deploy_report.json"
        if vr_path.exists():
            vr = json.loads(vr_path.read_text())
            for name in vr.get("succeeded", []):
                if name in self._objects:
                    self.set_deployed(name, "completed")
                    counts["deployed"] += 1

        # Functions
        fr_path = ws / "conversion" / "functions_deploy_report.json"
        if fr_path.exists():
            fr = json.loads(fr_path.read_text())
            for name in fr.get("succeeded", []):
                if name in self._objects:
                    self.set_deployed(name, "completed")
                    counts["deployed"] += 1

        # Procedures
        pr_path = ws / "conversion" / "procedures_deploy_report.json"
        if pr_path.exists():
            pr = json.loads(pr_path.read_text())
            for name in pr.get("succeeded", []):
                if name in self._objects:
                    self.set_deployed(name, "completed")
                    counts["deployed"] += 1

        # Conversion report
        cr_path = ws / "conversion" / "_conversion_report.json"
        if cr_path.exists():
            cr = json.loads(cr_path.read_text())
            for entry in cr.get("converted_objects", []):
                fqn = entry.get("fqn", "")
                if fqn and fqn in self._objects:
                    self.set_converted(fqn, "completed",
                                       artifact=entry.get("output_file", ""),
                                       ewi_codes=entry.get("ewi_codes", []))
                    counts["converted"] += 1

        self._dirty = True
        return counts

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return a structured summary of all objects by tier and status."""
        result: dict[str, dict[str, int]] = {}
        for tier in ("extraction", "conversion", "repair", "deployment", "validation"):
            counts: dict[str, int] = {}
            for obj in self._objects.values():
                st = getattr(obj, tier).status
                counts[st] = counts.get(st, 0) + 1
            result[tier] = counts

        type_counts: dict[str, int] = {}
        for obj in self._objects.values():
            t = obj.obj_type.upper()
            type_counts[t] = type_counts.get(t, 0) + 1

        excluded = sum(1 for o in self._objects.values() if o.is_excluded)

        return {
            "total_objects": len(self._objects),
            "excluded": excluded,
            "by_type": type_counts,
            "tiers": result,
        }

    def summary_text(self) -> str:
        """Return a human-readable summary string."""
        s = self.summary()
        lines = [f"Object Manifest: {s['total_objects']} objects ({s['excluded']} excluded)"]
        lines.append(f"  Types: {s['by_type']}")
        for tier, counts in s["tiers"].items():
            parts = [f"{status}={count}" for status, count in sorted(counts.items()) if count > 0]
            lines.append(f"  {tier:<12}: {', '.join(parts)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Resume / rerun helpers
    # ------------------------------------------------------------------

    def filter_for_run(self, tier: str, mode: str = "resume") -> list[ObjectState]:
        """Return the list of objects to process based on the run mode.

        Modes:
          resume          — only objects not yet completed (default, idempotent)
          force_rerun     — all objects (reprocess everything)
          force_failed    — only objects that previously failed this tier
        """
        if mode == "force_rerun":
            return [o for o in self._objects.values()
                    if not o.is_excluded and o.obj_type.upper() != "TABLE"]
        elif mode == "force_failed":
            return [o for o in self._objects.values()
                    if getattr(o, tier).status == "failed" and not o.is_excluded]
        else:  # resume
            return self.objects_needing(tier)

    def mark_tier_bulk(self, tier: str, fqns: list[str], status: str,
                       error: str = "", artifact: str = "") -> int:
        """Set the same status on a tier for multiple objects. Returns count updated."""
        count = 0
        for fqn in fqns:
            obj = self._objects.get(fqn)
            if obj:
                getattr(obj, tier).mark(status, error=error, artifact=artifact)
                count += 1
        if count:
            self._dirty = True
        return count
