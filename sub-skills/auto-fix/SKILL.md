---
name: auto-fix
description: "Apply fixes to spgloader based on aggregated feedback. Reads aggregated_feedback.json, applies changes to rules/prompts/scripts, and verifies each fix through a 4-layer verification pyramid."
---

# Auto-Fix Sub-Skill

Apply fixes to spgloader based on aggregated failure feedback from testers.

**Access control**: Only the skill owner invokes this. Testers cannot trigger fixes.

---

## Prerequisites

Before invoking this sub-skill, ensure:
1. `aggregate_feedback.py` has been run and produced `aggregated_feedback.json`
2. A SPG instance is available for verification (Layer 1 + Layer 3)
3. The source database container is running (for parity checks)
4. You are in the spgloader repo with write access

---

## Step 1 — Load Aggregated Feedback

```python
import json
from pathlib import Path

# User provides the path, or default to shared drive
feedback_path = Path("<AGGREGATED_FEEDBACK_JSON>")
patterns = json.loads(feedback_path.read_text())

print(f"Loaded {len(patterns)} patterns to fix")
for p in patterns[:10]:
    print(f"  #{p['rank']} [{p['fix_type']}] {p['pattern']} ({p['count']} objects)")
```

Show the user the ranked list and ask: "Which patterns should I fix? (e.g., 'top 3', 'all', or specific numbers)"

---

## Step 2 — Fix Each Pattern (Sequential)

For each selected pattern, apply the fix based on `fix_type`:

### fix_type: "rule"

Add a regex-based fix entry to the target YAML file.

1. Read `target_file` (e.g., `references/rules/mssql-to-pg/plpgsql-fixes.yaml`)
2. Use the `representative_source_sql` and `representative_error` to understand the pattern
3. Write a new YAML entry with:
   - `pattern`: regex matching the source SQL construct
   - `replacement`: the PostgreSQL-compatible equivalent
   - `description`: what the rule does
4. Append to the end of the YAML file

**Example:**
```yaml
# Fix: case-sensitive column identifiers (from feedback pattern: column_does_not_exist)
- pattern: '(?i)\b(CustomerID|OrderID|ProductID)\b'
  replacement: '"\1"'
  description: "Preserve case for mixed-case identifiers from MSSQL"
  added_by: "auto-fix"
  feedback_pattern: "column_does_not_exist"
```

### fix_type: "function-sub"

Add a function name mapping to `function-substitutions.yaml`:

```yaml
# Fix: function not found (from feedback pattern: function_does_not_exist)
- source: "ISNULL"
  target: "COALESCE"
  args: "pass-through"
  description: "MSSQL ISNULL → PostgreSQL COALESCE"
  added_by: "auto-fix"
```

### fix_type: "type-mapping"

Add a type mapping to `type-mappings.yaml`:

```yaml
# Fix: type not found (from feedback pattern: type_does_not_exist)
- source: "HIERARCHYID"
  target: "LTREE"
  notes: "Requires ltree extension enabled on SPG"
  added_by: "auto-fix"
```

### fix_type: "prompt"

Add an example to the LLM repair prompt (`procedure-repair-{source}-prompt.md`):

1. Read the existing prompt file
2. Find the "## Examples" section (or create one)
3. Add a new example with:
   - The error message
   - The failing SQL
   - The corrected SQL (CoCo writes the correction based on the error)
4. Format it clearly for the LLM to learn from

### fix_type: "script"

Edit the relevant Python script. This is the most complex fix type:

1. Read the target script
2. Identify the relevant function/section
3. Apply the minimal change to fix the pattern
4. This may involve:
   - Adding a special-case handler
   - Fixing ordering logic
   - Adding a filter or transformation step

**Important**: For `script` fixes, always show the diff to the user before applying.

---

## Step 3 — Verification Pyramid (After Each Fix)

After applying each fix, run the verification layers in order:

### Layer 1: Targeted Replay (~10 sec)

Re-convert and re-deploy the specific failing objects:

```bash
# Re-run conversion for the specific object
python <SKILL_DIR>/scripts/convert_objects.py \
    --work-dir <WORK_DIR> \
    --objects "<object_name>" \
    --source-type <source_type>

# Re-deploy just that object to SPG
python <SKILL_DIR>/scripts/deploy_to_spg.py \
    --work-dir <WORK_DIR> \
    --objects "<object_name>"
```

**Pass criteria**: Object deploys without error.
**On failure**: Revert the fix, report "Fix for pattern X does not resolve the error", try alternative approach.

### Layer 2: Regression Tests (~30 sec)

```bash
cd <SKILL_DIR>
pytest tests/ -v --tb=short
```

**Pass criteria**: All existing tests pass.
**On failure**: Revert the fix, report "Fix conflicts with existing rule: <test_name>".

### Layer 3: Parity Check (~2 min)

Run the same query on both source and SPG:

```bash
python <SKILL_DIR>/scripts/execution-parity/run_parity_single.py \
    --work-dir <WORK_DIR> \
    --object "<object_name>" \
    --source-type <source_type>
```

**Pass criteria**: Result-set hashes match between source and SPG.
**On failure**: Report "Fix deploys successfully but produces different results. Diff: <details>". Flag for manual review — do NOT revert (deploy-success is still an improvement).

### Layer 4: Full Scenario Re-run (On-demand only)

Only run when the user explicitly asks:

```bash
python <SKILL_DIR>/scripts/run_test_suite.py \
    --scenarios <affected_scenarios> \
    --collect
```

---

## Step 4 — Report Results

After all selected patterns are processed, present:

```
═══════════════════════════════════════════════════════════════════════
AUTO-FIX RESULTS
═══════════════════════════════════════════════════════════════════════

Pattern                         Fix Applied    Layer 1    Layer 2    Layer 3
───────────────────────────────────────────────────────────────────────
column_does_not_exist           ✓ rule         10/12 ✓    ✓          8/10 ✓
function_does_not_exist         ✓ func-sub     8/8 ✓      ✓          7/8 ✓
cursor_loop                     ✓ prompt       3/5 ✓      ✓          3/3 ✓
───────────────────────────────────────────────────────────────────────

Summary:
  Patterns attempted: 3
  Objects fixed: 21/25 (84%)
  Parity verified: 18/21 (86%)
  Still failing: 4 objects (different root cause)
  Manual review needed: 3 objects (parity diff)

Files modified:
  references/rules/mssql-to-pg/plpgsql-fixes.yaml (+2 rules)
  references/rules/mssql-to-pg/function-substitutions.yaml (+1 entry)
  references/prompts/procedure-repair-mssql-prompt.md (+1 example)

Commit these changes? [yes / show diffs / re-run full scenario / reject]
═══════════════════════════════════════════════════════════════════════
```

---

## Step 5 — Commit (Only on User Approval)

If the user says "yes" or "commit":

```bash
cd <SKILL_DIR>
git add -A
git commit -m "fix: auto-fix N patterns from aggregated feedback

Patterns resolved:
- column_does_not_exist: added case-preserving quote rule
- function_does_not_exist: added ISNULL→COALESCE mapping
- cursor_loop: added CURSOR example to repair prompt

Verified: pytest ✓, 21/25 objects deploy, 18/21 parity confirmed.

Generated with [Cortex Code](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code)

Co-Authored-By: Cortex Code <noreply@snowflake.com>"
```

---

## Handling Edge Cases

| Situation | Action |
|-----------|--------|
| No SPG instance available | Skip Layers 1 and 3, run Layer 2 only, warn user |
| No source container running | Skip Layer 3, warn user "parity not verified" |
| Pattern has no clear fix | Report "Cannot auto-fix: <reason>", suggest manual investigation |
| Fix helps some objects but not all | Apply the partial fix, report remaining objects separately |
| Two fixes conflict with each other | Apply them sequentially, detect conflict at Layer 2 |
| User rejects a fix | Revert that specific change, keep other fixes |

---

## What This Sub-Skill Does NOT Do

- Does NOT run without explicit user invocation
- Does NOT auto-push to remote (only local commits)
- Does NOT modify the feedback ledger (that's the collector's job)
- Does NOT delete or archive JSONL files (that's manual)
- Does NOT skip the human approval gate
