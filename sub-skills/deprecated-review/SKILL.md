# Phase 3.6 — Deprecated Object Review
**Decision gate: review deprecated patterns before conversion.**

This sub-skill handles Phase 3.6 of the spgloader migration pipeline. It runs detection,
presents each deprecated pattern group to the user via `ask_user_question`, records
their dispositions, and writes `deprecated/deprecated_review.json`. Phase 4 reads
this file to exclude objects marked `skip`.

**When to invoke**: Immediately after Phase 3.5 (assess). Always run — exits silently
if no patterns are detected.

---

## Step 1 — Run Detection

```bash
# ALWAYS use --non-interactive here — detection only, no stdin blocking
uv run --project <SKILL_DIR> python <SKILL_DIR>/scripts/analyze_deprecated.py \
  --work-dir "$SPGLOADER_WORK_DIR" --non-interactive
```

The `--non-interactive` flag auto-applies the default `skip` disposition.
**Never treat this as the final disposition.** Always proceed to Step 2.

---

## Step 2 — Present Groups to User

Read the results. If no patterns were detected, proceed to Phase 4.
If patterns were detected, **ask the user about EACH group** — even if the auto-disposition looks correct.

```python
import json, pathlib

review_path = pathlib.Path(f"{WORK_DIR}/deprecated/deprecated_review.json")
review      = json.loads(review_path.read_text())
groups      = review.get("groups", {})

if not groups:
    print("Phase 3.6: No deprecated patterns detected — continuing to Phase 4.")
else:
    for group_key, group_data in groups.items():
        # Present group to user — see ask_user_question template below
        # Record their choice and update review["groups"][group_key]["disposition"]
        pass
    review_path.write_text(json.dumps(review, indent=2))
```

Use this `ask_user_question` template for **each** detected group:

```yaml
ask_user_question:
  header: "<pattern_name>"
  question: |
    Deprecated pattern detected: '<pattern_name>'

    <N> objects matched:
    <list first 5 FQNs, then "...and N more" if >5>

    <pattern description>
    Recommendation: <pattern recommendation>

    What should happen with these objects?
  defaultAnswer: "Skip — exclude from migration (recommended)"
  options:
    - label: "Skip — exclude from migration (recommended)"
      description: >
        Objects will NOT be converted or deployed to SPG.
        Views/procedures that only reference these objects will also be excluded.
    - label: "Migrate — convert and deploy anyway"
      description: >
        Objects will go through the full conversion pipeline.
        Results may be incomplete for deprecated frameworks.
    - label: "Modernize — flag for manual replacement"
      description: >
        Objects are marked for replacement with a modern alternative.
        See deprecated_report.md for guidance.
```

Map answers to dispositions:
- "Skip" → `"skip"`
- "Migrate" → `"migrate"`
- "Modernize" → `"modernize"`

---

## Supported Deprecated Patterns

| ID | Name | Default | Objects affected |
|---|---|---|---|
| `aspnet_membership` | ASP.NET Membership | skip | `aspnet_*` procs/tables |
| `sql_server_agent` | SQL Server Agent | skip | `msdb.*`, agent job procs |
| `linked_servers` | Linked Servers | skip | `OPENQUERY`, 4-part names |
| `clr_objects` | CLR Objects | skip | `EXTERNAL_NAME` procs |
| `udtt` | User-Defined Table Types | skip | `TYPE ... AS TABLE` |
| `extended_procs` | Extended Stored Procs | skip | `sp_OA*`, `xp_*` |
| `temporal_tables` | Temporal Tables | skip | `WITH (SYSTEM_VERSIONING = ON)` |

---

## Output Files

| File | Description |
|---|---|
| `deprecated/deprecated_review.json` | Per-group dispositions — read by Phase 4 |
| `deprecated/deprecated_report.md` | Human-readable summary of what was found |

---

## Phase 4 Integration

`convert_objects.py` (Phase 4) reads `deprecated/deprecated_review.json` and:
- Skips all objects in groups with `"disposition": "skip"`
- Converts objects in groups with `"disposition": "migrate"` (may produce EWI warnings)
- Tags objects in groups with `"disposition": "modernize"` with `SPG-EWI-0012`

---

## Re-running Phase 3.6

To re-run if the user changes their mind:
```bash
rm "$SPGLOADER_WORK_DIR/deprecated/deprecated_review.json"
# Then re-run Step 1 and Step 2
```
