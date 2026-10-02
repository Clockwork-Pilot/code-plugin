---
name: cwpilot:cwpilot
description: Route Clockwork-Pilot graph work to its focused skill and get live instructions with cwpilot pull.
---

# cwpilot

Clockwork-Pilot persists project state in an ExecutableGraph. Use `cwpilot pull` as
steering when work has stopped and the next action is unknown; never pipe its output or
act on a partial rendering. If an active assignment is already known, continue it
directly—an identical Stop-hook prompt does not require another `pull`.

## Direct user requests take precedence

The user's explicit request is authoritative over a router-rendered next action. If the user
asks for a concrete action that differs from `cwpilot pull`, perform the requested action and
surface the conflict if useful; do not substitute the router proposal for the user's request.

This precedence does not bypass safety or approval boundaries: a direct request to create work
authorizes capture, but approval is still required before assignment or execution. Likewise,
do not claim completion without real verification evidence.

Load the focused skill for the action:

- `executable_graph` for graph identity, resolution, and pull; `bootstrap` when the graph is absent.
- `create_work` for capture, `approve_work` for the human approval gate, `enqueue_work` for lanes, and `execute_work` for an active WorkNode.
- `create_design` for design decisions; `insights` for project learning.
- `settings` for configuration and `query_graph` for read-only graph queries.
- `patch` plus `knowledge_document_tools` for direct document writes; `check_constraints` plus `features_and_constraints` for constraints.
- `playbooks` for playbook mechanics and `interactions` for pending prompts.

```bash
cwpilot pull
```

Capture follows `/settings/enable_works_approval`: explicit, sufficiently specified user
requests are captured immediately; inferred work is presented for review before capture
when approvals are disabled. New scope or findings amend the existing WorkNode with
`cwpilot work update`; they do not create duplicate work items.
