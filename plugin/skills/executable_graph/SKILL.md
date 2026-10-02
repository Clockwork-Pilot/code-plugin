---
name: cwpilot:patch
description: Understand the ExecutableGraph document and use cwpilot pull to navigate a graph-driven project.
---

Load when entering a cwpilot project or locating its graph.

# ExecutableGraph — what it is

A cwpilot-driven project keeps one **ExecutableGraph** document, by default
`executable_graph.k.json` at the project root. It holds the project's decisions and the work
that follows from them, so a session can pick up where the last one stopped.

It is a *document*, not a scratchpad: it is read and written through cwpilot's commands, never
by hand and never with a text editor. Document has simple read-only attribute which remove
before write and set just after write. Document itself getting atomic transactional updates,
and altogether with retries mechanism either deliver changes or fails transaction.
Following command acts like is a router for agent rendering direct instructions:

**Important**: No piping cwpilot pull, as its instructions are meaningfull, and any partial
instructions it renders - are useless and will cause incorrect operaton.

```bash
cwpilot pull                       # steer when stopped and the next action is unknown
```

Every `cwpilot` command operates on this document, and cli resolves in a certain ways:
`CWPILOT_PROJECT_ROOT` - environemnt variabe is a primary way of identifying document's location.
If env var is not set - it resolves it from a path saved in *semi-trusted shared session store*
a file on disk created by SessionStart hook. This method is less durable, though just works.
Outside of agentic session - it is required to either set env var or specifying a path using
`cwpilot --doc <path>` argument, which is widely available in CLI.

## What it holds

- **Work** — a persisted work assignment, includes description, goals and its target
- **Designs** — a decision that was made and the reasoning behind it
- **Insights** — what was learned along the way, kept so a later session can find it
- **Queues** - execution lanes, for works to be assigned and executed sequentially per queue
- **Features and Constraints** — behavioral contracts and their verification status
- **Settings** — document-level configuration

Items reference each other — a work item can depend on another, or trace back to the design
that motivated it.
