---
name: feedback-analysis
description: "Analyze migration artifacts from a work dir or submitted zip to produce skill-improvement recommendations (read-only, no auto-fix)."
---

# Feedback Analysis

Analyze migration artifacts and produce skill-improvement recommendations.
Called automatically after Phase 6.6 via `witness-validate/SKILL.md` Step 11.
Also invoked manually to analyze artifacts submitted by external testers.

**Hard constraint**: this sub-skill is read-only. It outputs recommendations only.
It does NOT modify any skill files, scripts, or rules.

---

## When called automatically (post-Phase 6.6)

The analysis runs on `$SPGLOADER_WORK_DIR` — already in scope. No user input needed.

```python
import sys
sys.path.insert(0, "<SKILL_DIR>/lib")
from spgloader.reporting.feedback import analyze_artifacts
analyze_artifacts("<SPGLOADER_WORK_DIR>")
```

---

## When called manually (external tester artifacts)

The user provides a path to an unzipped artifact bundle or a zip file.

**Step 1 — Get the artifact path**

Ask the user:
```
Where is the artifact bundle?
  a) A directory path (already unzipped)
  b) A zip file path
```

**Step 2 — Unzip if needed**

```python
import zipfile, tempfile, os
from pathlib import Path

artifact_path = Path("<USER_PROVIDED_PATH>").expanduser()
if artifact_path.suffix == ".zip":
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(artifact_path) as zf:
        zf.extractall(tmp)
    work_dir = tmp
else:
    work_dir = artifact_path
```

**Step 3 — Run analysis**

```python
import sys
sys.path.insert(0, "<SKILL_DIR>/lib")
from spgloader.reporting.feedback import analyze_artifacts
analyze_artifacts(work_dir)
```

**Step 4 — Show recommendations**

The analysis output appears in chat. After printing:

1. For each HIGH recommendation: ask the user "Do you want to track this as a
   GitHub issue?" — if yes, help them file it at
   `https://github.com/sfc-gh-rkhandhadia/spgloader/issues/new?template=migration-feedback.yml`

2. For each MEDIUM recommendation: note it but do not action it without explicit
   user instruction.

3. Do NOT open or modify any skill files. Present findings only.

---

## Output interpretation guide

| Level | Meaning | Action |
|---|---|---|
| HIGH | Pattern affects 5+ objects or repair rate < 60% | Investigate this session |
| MEDIUM | Pattern affects 2-4 objects or parity gaps | Log for next sprint |
| LOW | Single occurrence or EWI density < 10% | Informational only |

---

## What is NOT in scope

- Applying any recommended fix (user must do this manually)
- Re-running the migration
- Modifying `plpgsql-fixes.yaml`, `convert_objects.py`, or any prompt file
- Committing or pushing changes
