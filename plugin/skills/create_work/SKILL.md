---
name: cwpilot:create_work
description: Author and capture a concrete WorkNode with paths, structural spec, and verifiable goals.
---

Load when the user explicitly asks to capture or create work.

## Design vs. Work

**DESIGN = knowledge/why** — an architectural decision, pattern, or principle.
**WORK = execution/how** — an implementation task.

- ✅ Design: "Sub-agents spawn fresh with zero context to enforce isolation"
- ✅ Work: "Add `--approve`/`--decline` flags to interactions drain CLI"
- ❌ Design that is really work: "Add `--approve` flag to drain CLI"
- ❌ Work that is really design: "Preserve user agency by gating queue decisions"

The prose/user-story description of a feature belongs on the **WorkNode**'s `--description`,
never in a design bullet.

**The rule - Work is authored, never generated.** Nothing expands an intention into work items
on its own; each one is created deliberately, and each one is the user's to approve before it runs.

## Content limits: 200-char cap on --description and --goal values

Both `--description` and `--goal` values are capped at **200 characters** (soft limit with hard cap at 220 chars). If you have a longer description, split it:
- Use `--description` for a short summary (≤200 chars)
- Move additional context into multiple `--goal` entries instead

**Example: splitting an oversized description**

Instead of cramming everything into one long --description:
```bash
# ❌ This fails — description exceeds 200 chars
cwpilot work create "fix_timeout_edge_case" --op edit --path src/api/client.py \
  --description "Handle timeout when requests exceed 30s during network degradation with exponential backoff retry logic and detailed error reporting including request id and duration metrics so operators can debug production issues"
```

Split it into a short description plus multiple goals:
```bash
# ✅ Correct — description is short, details go in --goal entries
cwpilot work create "fix_timeout_edge_case" --op edit --path src/api/client.py \
  --description "Handle timeout when requests exceed 30s during network degradation" \
  --goal "timeout.backoff=Implement exponential backoff retry logic (max 3 retries)" \
  --goal "timeout.logging=Log request id, duration, and error details for debugging" \
  --goal "timeout.metrics=Emit metrics so operators can detect degradation patterns"
```

## Work lifecycle

**Capture** → **Approve** → **Assign** → **Execute** → **Verify** → **Commit** → **Complete**

1. **Capture** — work item is proposed with operation, path, and goals
```bash
cwpilot work create --op edit --path path/to/file --goal "feature.flag=Description"
```

Choose a specific primary `--path`, list all known additional source files, state the outcome and why in `--description`, include the structural spec, and write goals whose results can be verified. Capturing a WorkNode records the proposal; assignment approval is a separate gate.

Before capturing, read `/settings/enable_works_approval/value`:

- **Approvals enabled** (`enable_works_approval=true`) — capture every sufficiently scoped
  WorkNode immediately, including explicit user work and system-generated repair work. Do
  not ask a pre-capture question. After capture, render `show-approval`; the user then
  approves or declines the captured item. This approval card is the gate, not a second
  capture question.
- **Approvals disabled** (`enable_works_approval=false`) — for inferred or proactive work,
  show the complete proposal and wait for the user's confirmation before running
  `work create`. A direct, explicit user request to "create work" or "capture this"
  authorizes capture because the request itself is the user's confirmation; do not add a
  duplicate question.

Verify both branches: with approvals enabled, explicit and system-generated scoped work
produce an immediate `cwpilot work create` followed by a rendered `show-approval` card;
with approvals disabled, inferred work stops at a confirmation question before any
`work create` call.

When approvals are disabled, present inferred scope and criteria to the user for review
before creating the WorkNode. Do not silently turn an inferred idea into proposed work.
The user's review authorizes capture; the resulting proposed WorkNode does not need a
second approval gate before assignment.

### Constraint checks are an acceptance gate

When a WorkNode must check every configured feature constraint, capture that requirement
with the dedicated `--check-features-constraints all` option:

```bash
cwpilot work create "repair constraint guidance" --op edit --path path/to/file \
  --check-features-constraints all --goal "scope.change=Describe the verifiable outcome"
```

`check_features_constraints` is an acceptance-time gate, not a `constraints.checks` goal.
Do not encode `all` as an ordinary `--goal`; goals describe the work outcome, while the
dedicated field controls which constraints `cwpilot check` evaluates during verification.
Before approval or assignment, run both `cwpilot show-approval work <id>` and
`cwpilot work explain <id>` and confirm the rendered acceptance gate is non-empty and the
gate is shown separately from the goals. A focused regression check should create or
inspect a WorkNode with `--check-features-constraints all` and prove that
`check_features_constraints == "all"` while no goal key is `constraints.checks`.

## Write it for a dumb agent with zero memory of this conversation

A WorkNode is what survives when this session's context is gone. Whoever executes it later —
a fresh sub-agent, a different session, you after a `/clear` — gets ONLY what is written on
the node. If the investigation that justified the work happened in conversation and isn't
captured here, it is lost. Write every goal as if briefing someone who was not in the room:

- Name exact file paths and line numbers/anchors ("around line N", "next to `<existing
  function name>`"), not "the relevant file".
- Name exact function/variable/key names to reuse or match ("mirror `<helper>` in
  `<file>`", "key format from `<module.function>`"), not "the existing mechanism".
- Spell out literal values, formats, and thresholds involved, not "seed the value
  appropriately".
- State each expected outcome as a concrete, checkable assertion ("command X must print
  Y" / "field Z must equal W"), not "should work correctly".
- State the decision and its rationale, not just the outcome ("use X because Y", not just
  "do X") — the executor should not have to reconstruct why a choice was made.
- Name every dependency this work assumes: another WorkNode it must follow (`--depends-on`),
  prerequisite state, or a file that must already exist.
- One concept per goal. `--goal` content is capped at 200 chars — if a goal is trying to say
  two things, that's the sign to split it into two dotted keys under the same domain prefix
  rather than compress the prose. A rejected long goal is a decomposition problem, not a
  wording problem.

A WorkNode that only a person who remembers this conversation could execute has already
failed at its one job. The implementer receives ONLY the WorkNode assignment — nothing
else from this conversation. Missing context is a capture failure to fix before approval,
never something the implementer is expected to ask you to clarify.

Assume the executor is a cheap, low-judgment model, not a peer who will fill gaps with
inference. That raises the bar past "no missing context" to "no interpretation required":

- Where the change is a text/code edit, quote the exact current line(s) and the exact
  replacement, not a description of the change — a diff the executor can apply, not a
  summary it has to derive one.
- For a discovered bug, give an exact repro/verify pair as its own goal: a command that
  demonstrates the bug failing BEFORE the fix, and a command with the concrete expected
  output or exit code that must hold true AFTER it — so the executor can confirm it fixed
  the right thing without re-deriving the investigation.
- Name every known source, test, skill, and convention file relevant to the area, and any
  linked historical decision (`design` capture) that bears on it, before requesting
  approval — not left for the executor to rediscover.
- If a related skill, convention file, or docstring governs the area (an existing sibling
  implementation to mirror, a project's own CLAUDE.md), name it and say to read it first,
  rather than restating its contents from memory.

A WorkNode a cheap model could only complete by guessing has failed the same way a
WorkNode missing context has.

## New findings before implementation: amend, don't leave them in chat

If new scope, findings, exact files, decisions, or acceptance criteria emerge after a
WorkNode is captured, update that existing WorkNode in place — conversation context is
never an implementation dependency, per "zero memory" above. Do not create a duplicate
WorkNode for the same outcome, whether the original is pending approval, approved, queued,
or already running.

- Use `cwpilot work update <node_id> --description ... / --goal domain.concept=...` to fold
  the finding into the existing pending WorkNode in place, then re-render its approval card
  (`show-approval`). Do not leave it only in chat, and do not create a second WorkNode for
  the same target.
- This applies whether the node is still pending approval or already approved and not yet
  assigned — amend it in place either way rather than letting the run start under-specified.

Checkable example: a pending WorkNode targeting `src/foo.py` is missing the exact function
name to modify, discovered later in chat. Run `cwpilot work update <node_id> --goal
"foo.anchor=Modify handle_request in src/foo.py near the retry loop"`, confirm the new goal
appears in `cwpilot work explain <node_id>`, and confirm `show-approval` re-renders the card
with it included.

## `--spec` is a payload field, not a narrative field

`--spec JSON` (`target.spec`) holds an **operation-specific structured payload**: argv for
`--op run`, a file-contents skeleton for `--op create`, an interface-def or useful-samples
dict — something the executor consumes as data, not prose it reads. It is not an overflow
bucket for investigation writeups.

All narrative context — confirmed bug, root cause, what was verified live, the exact fix
location, repro/verify commands, how it was discovered — goes in `--description` and
repeated `--goal domain.concept=...` entries (one concept per goal; split rather than
compress when a goal would exceed 200 chars), per "Write it for a dumb agent" above. If the
context genuinely needs more structure than goals give it, that is a sign to reconsider
scope, not a reason to reach for `--spec`.

Stuffing long free text into `--spec` also causes real, repeated failures, not just a
misfiled field: `--spec` is passed as a single shell-quoted JSON argument, and long prose is
far more likely to contain an apostrophe (`"_activate's"`, `"/v1/validate's"`, "user's",
contractions) that closes the shell's quoting early and breaks the whole command — often with
a confusing downstream error (e.g. `bash: eval: ... syntax error`) that has nothing to do with
the actual `work create`/`work update` logic. Short, targeted `--goal` values are both the
correct field AND far less likely to contain a quote-breaking apostrophe in the first place.

## Dependencies between works

`--depends-on NAME` (repeatable, at create or via `work update`) declares that this WorkNode
must not run before another named work item completes — use it when this work's goals assume
the other's outcome already exists (e.g. a client-side change that reads a constant a separate
WorkNode is adding). Without it, two works assigned to different lanes execute in parallel with
no ordering between them at all, even when one logically needs the other to land first.

Check the prerequisite's current state before adding the relation. A WorkNode already in
`completed` state has no future gate to provide, so do NOT add it to `depends_on`; completed
work is historical context, not a pending prerequisite. Mention it in the description or use
`--link-work` only when that context is useful. Add `--depends-on` only for an unfinished
WorkNode whose future completion is required.

`--link-work WORK_ID` (repeatable, **create only** — wiring it into `work update` is a known gap,
see the `--link-work` bug tracked separately) records a **non-blocking** relationship: two works
worth reading together, neither gating the other's execution. Use it for related-but-independent
halves of the same change (e.g. a client-side and server-side piece of one protocol) so a reader
finds the sibling without either one blocking the other's assignment.

Completely unrelated work needs neither — assign each to its own lane and they run in parallel
with no coordination required. See `cwpilot:enqueue_work` for what a lane assignment does and
does not guarantee about ordering.
