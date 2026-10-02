---
name: cwpilot:settings
description: Understand document settings for approval, execution, lanes, and constraints.
---

Load when interpreting or changing ExecutableGraph settings.

# Settings

Document-level settings control how approvals, execution, and queueing behave. They are stored in `/settings` of the ExecutableGraph and apply globally.

## Approval and execution flow

**`enable_dialogs`** (default: `true`)
- When `true`, approval cards are rendered as `AskUserQuestion` dialogs
- When `false`, approvals are shown as plain text
- The content and options are identical; only the presentation differs

**`auto_enqueue`** (default: `true`)
- When `true`, approved work automatically enters a queue lane
- When `false`, approval and queueing are separate steps (manual `cwpilot queue assign`)
- Approval alone does not execute work; queueing is the gate to execution

**`auto_commit_on_completion`** (default: `false`)
- When `true`, work is automatically committed to git after verification passes
- When `false`, commitment is manual (work reaches `commit` step and waits)
- This is the **only** authorization to commit — when set, agents commit without asking

**`enable_works_approval`**
- When `true` - sufficiently specified work requested by the user is captured immediately
  with state:captured and rendered for human approval before assignment.
- When `false` - explicitly requested work may be captured with state:proposed without an
  approval gate; inferred work must be shown to the user for review before it is captured.

## Concurrency control

**`max_concurrent_subagent_lanes`** (default: varies by plan)
- Maximum number of work items executing as subagents in parallel
- Lanes beyond the cap wait; inline and fork mode are unaffected
- Prevents overwhelming the system with too many concurrent agents

## Constraint verification

**`contains_unverified_constraints`** (auto-managed)
- Blocks all source edits if any constraint has never failed once
- Clears automatically when all constraints are verified (failed + passed)
- Not user-configurable; a constraint lifecycle management flag
