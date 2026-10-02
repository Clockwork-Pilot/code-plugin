---
name: cwpilot:approve_work
description: Handle the human approval boundary, tokens, decline, and postponement for work.
---

Load when a captured work item is awaiting user approval.

# Approvals

Approval is a boundary for captured work: it separates captured work from executable work
and is always human-controlled. When `enable_works_approval=false`, proposed work does not
enter this approval gate; inferred work still requires user review before capture.

What approval means:
- Work: "I have reviewed this and authorize it to run"
- Design: "I have reviewed this decision and authorize it to be recorded"
- Never self-approve — approving your own work/design is forbidden

Approval vs. execution:
Approval is separate from execution. An approved work item waits in a queue until its lane is free; approval does not mean "run immediately." Settings control whether
approvals trigger auto-execution (auto_enqueue) or require manual assignment.

Approval workflow:

# Approve (requires token from show-approval)
```bash
cwpilot work approve <work_id> --token $TOK
cwpilot design approve <design_id> --token $TOK
```

# Decline (removes the item)
```bash
cwpilot work delete <work_id>
cwpilot design delete <draft_id>
```

# Postpone - stops surfacing approval for 1h/1d
```bash
cwpilot work postpone <work_id|alias> 1h
```

Once approved, the item is no longer re-litigated — approval is durable.

If scope, findings, or criteria change after capture, update the existing WorkNode in place
and re-render its approval card when it is still pending approval. Do not create a second
WorkNode for the same outcome.

2. **Approve** — human reviews and approves (or declines) the work; mandatory gate
```bash
# see pending approvals, get token while rendering
cwpilot show-approval work <work_id|alias>
cwpilot work approve <work_id|alias> --token $TOK   # approve with token
```
