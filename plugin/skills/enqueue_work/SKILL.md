---
name: cwpilot:enqueue_work
description: Assign approved work to a lane and advance the queue.
---

Load when assigning, advancing, pausing, resuming, or listing a queue.

## Queues and execution

A **queue** organizes work into **lanes**. Each lane maintains a single-running invariant: one
item executes at a time. Related work shares a lane to prevent conflicts; unrelated work takes
separate lanes and executes in parallel.

**Queue ordering is per-lane, not global.** A lane is a FIFO of its own queued entries — "this
runs before that" is only guaranteed for two items sitting in the SAME lane at the SAME time.
Move an item to a different lane (or assign a fresh one there) and whatever ordering it had
relative to its old lane-mates is gone — a lane has no memory of where an item came from, and
carries no guarantee about what ran in some other lane. Lane placement is a parallelism/conflict
tool, never an ordering guarantee on its own. If two works must run in a specific order
regardless of which lane(s) they end up in, declare it explicitly as `--depends-on` on the
WorkNode itself (see `cwpilot:create_work`) — that is the only durable ordering guarantee.

**Important** - cwpilot dispatches no background tasks or subagents directly, it has no daemon.
Instead it renders instructions for agent, and maintaining *advancing* concept.

*Advancing* has two meanings:
1. Advancing a queue/lane — moving the lane from one queued item to the next:
`cwpilot queue advance --lane implementation`
This starts the next work item waiting in that lane. The previous work must be complete before
the lane can advance to the next item. A lane with an active run cannot advance until that run
finishes.

2. Advancing a work/run — moving from one step to the next within a playbook execution:
`cwpilot play advance <run_id> --fact changes_implemented=true --fact tests_pass=true`
This evaluates the current step's gates against the facts you provide. If gates are satisfied,
the work moves to the next step. Facts record evidence: changes_implemented, verified, etc.
We use `implement_work` playbook for advancing run, playbook binds to a particular worknode,
as it carries state, in most of the cases command with exact instruction how to bind to a node
is rendered by either `cwpilot pull` or `cwpilot queue assign <workid> --advance`.
```bash
cwpilot play from-node <WORK_PATH> --playbook implement_work
```

**Queue commands:**

```bash
cwpilot queue assign <work_path>              # assign work to a lane
cwpilot queue assign <work_path> --advance    # assign and start advancing
cwpilot queue advance [--lane <name>]         # start the next item in a lane
cwpilot queue list                            # view all lanes and entries
cwpilot queue pause <lane> --reason "..."     # pause intake (user control)
cwpilot queue resume <lane>                   # resume a paused lane
```

The flow:
- cwpilot queue advance → starts work (lane moves to next queued item)
- play advance → steps work forward (work moves through playbook steps)
- Work completes → lane becomes free → queue advance can start the next item

So advancing at the queue level is about queue progression (which work runs next); advancing
at the work level is about playbook progression (which step runs next).

3. **Assign** — approved work enters a queue lane, waits for that lane to be free
```bash
cwpilot queue assign <workid|alias> --advance    # assign and start if lane is free
```
