---
name: cwpilot:create_design
description: Capture and promote design decisions through the design approval lifecycle.
---

Load when recording a design decision.

## Design lifecycle

Capture → Approve → Promote → Active

1. Capture — design bullets are drafted under /draft_designs
```bash
cwpilot design create "Feature name" \
  --description "Mechanism or rationale" \
  --key domain.concept
```
2. Approve — human decides the design should be promoted; mandatory gate
```bash
cwpilot show-approval next                        # see pending design approvals
cwpilot design approve <draft_id> --token $TOK    # approve for promotion
```
3. Promote — approved design moves to /designs and creates a thin MindMap insight for cross-reference
    (automatic with approval)
4. Active — promoted design becomes part of the project's decision record; immutable except description
cwpilot design update <design_id> --description "Updated rationale"
