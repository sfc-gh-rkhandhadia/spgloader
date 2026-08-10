"""
upload_feedback.py — Copy feedback JSONL to the shared drive for aggregation.

Copies the feedback_export.jsonl from a workspace to the configured shared
drive location (SPGLOADER_FEEDBACK_DIR) with a standardized filename.

Usage:
    python scripts/upload_feedback.py <work_dir> [--feedback-dir <path>]

Filename convention: {tester}_{scenario}_{source}_{YYYYMMDD}.jsonl

If SPGLOADER_FEEDBACK_DIR is not set and --feedback-dir is not provided,
the file stays local and a message tells the user to share manually.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path


def get_feedback_dir(cli_override: str | None = None) -> Path | None:
    """Resolve the shared feedback directory."""
    if cli_override:
        return Path(cli_override).expanduser()
    env_dir = os.environ.get("SPGLOADER_FEEDBACK_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return None


def derive_filename(jsonl_path: Path) -> str:
    """Derive upload filename from the JSONL contents."""
    tester = "unknown"
    scenario = "unknown"
    source = "unknown"

    try:
        with open(jsonl_path, "r") as f:
            first_line = f.readline().strip()
            if first_line:
                entry = json.loads(first_line)
                tester = entry.get("tester", "unknown").split("@")[0].replace(".", "_")
                scenario = entry.get("scenario", "unknown")
                source = entry.get("source_type", "unknown")
    except Exception:
        pass

    date_str = datetime.now().strftime("%Y%m%d")
    # Sanitize for filesystem
    safe = lambda s: "".join(c if c.isalnum() or c in "_-" else "_" for c in s)
    return f"{safe(tester)}_{safe(scenario)}_{safe(source)}_{date_str}.jsonl"


def upload_feedback(work_dir: str | Path, feedback_dir: str | None = None) -> str | None:
    """Copy feedback_export.jsonl to shared drive. Returns destination path or None."""
    work_dir = Path(work_dir).expanduser()
    source_file = work_dir / "feedback_export.jsonl"

    if not source_file.exists():
        print(f"No feedback file found at: {source_file}")
        print("Run collect_failures.py first to generate it.")
        return None

    dest_dir = get_feedback_dir(feedback_dir)
    if dest_dir is None:
        print(f"Feedback exported locally: {source_file}")
        print("To share with the skill owner, set SPGLOADER_FEEDBACK_DIR or")
        print("manually send this file.")
        return str(source_file)

    if not dest_dir.exists():
        print(f"Shared feedback directory does not exist: {dest_dir}")
        print(f"Feedback saved locally: {source_file}")
        print("Check that the shared drive is mounted.")
        return str(source_file)

    filename = derive_filename(source_file)
    dest_path = dest_dir / filename

    # Avoid overwriting — append counter if file exists
    if dest_path.exists():
        stem = dest_path.stem
        for i in range(2, 100):
            candidate = dest_dir / f"{stem}_{i}.jsonl"
            if not candidate.exists():
                dest_path = candidate
                break

    shutil.copy2(source_file, dest_path)
    print(f"Feedback uploaded: {dest_path}")
    return str(dest_path)


def main():
    parser = argparse.ArgumentParser(
        description="Upload feedback JSONL to shared drive."
    )
    parser.add_argument("work_dir", help="Path to the migration workspace")
    parser.add_argument("--feedback-dir", help="Override shared feedback directory")
    args = parser.parse_args()

    upload_feedback(args.work_dir, args.feedback_dir)


if __name__ == "__main__":
    main()
