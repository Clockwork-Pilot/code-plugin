---
name: cwpilot:execute_work
description: Execute, verify, review, and close out an approved WorkNode run.
---

Load when a WorkNode is dispatched or an implement_work run is active.

## Work lifecycle

**Capture** → **Approve** → **Assign** → **Execute** → **Verify** → **Commit** → **Complete**

1. **Capture** — work item is proposed with operation, path, and goals
```bash
cwpilot work create --op edit --path path/to/file --goal "feature.flag=Description"
```
2. **Approve** — human reviews and approves (or declines) the work; mandatory gate
```bash
# see pending approvals, get token while rendering
cwpilot show-approval work <work_id|alias>
cwpilot work approve <work_id|alias> --token $TOK   # approve with token
```
3. **Assign** — approved work enters a queue lane, waits for that lane to be free
```bash
cwpilot queue assign <workid|alias> --advance    # assign and start if lane is free
```
4. **Execute** — Bind `implement_work` playbook to a specific work node
```bash
cwpilot play from-node <WORK_PATH> --playbook implement_work [--mode inline|subagent|fork|auto]

```
5. **Advance through steps**: understand, implement, verify, review, commit, etc.
Follow instructions from `cwpilot pull` or:
```bash
cwpilot play advance <run_id> --fact key=val  # perform step, record fact, advance
```

For a bound run, inspect the current step with `cwpilot play show <run_id> --step`, perform its instruction, record actual evidence with `--fact`, and advance once per performed step. Verify the real diff and test output before acceptance; close out the run through review and commit according to its rendered instructions.
