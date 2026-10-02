---
name: cwpilot:playbooks
description: Basic principles of playbooks — a Playbook is a YAML step graph, a PlaybookRun is one execution of it. How to list playbooks and read one (cwpilot play playbooks), start one (play create for a nodeless playbook, play from-node for one bound to a node), step it (play show --step / play advance --fact), where playbooks are loaded from (overridable_playbooks in knowledge_config.yaml), and how to author your own. Running a WorkNode is not here — queue, approval, dispatch and close-out live in cwpilot:execute_work.
---

# Playbooks

A **Playbook** is a step graph defined in YAML. A **PlaybookRun** is one execution of it,
persisted in the graph document. You reference a playbook by its `playbook_id`.

> **If your source node is a WorkNode, use `cwpilot:execute_work`.** Queue entries, approval,
> dispatch and close-out all live there and win on any disagreement.

## Principles

- **The CLI owns state.** Transitions, gates and facts are its job — never hand-patch a run.
- **The step's `instruct` is the instruction.** Read it from the CLI, not from the YAML.
- **One `advance` per step actually performed.** Each call asserts "I did that step" and
  carries that step's facts. Never loop it.
- **Facts drive transitions.** `--fact K=V` persists; predicates evaluate against those facts.
- **A `predicate` is a hard gate** — false means the advance is rejected, and it is right to be.
  A `when:` alone is a soft gate you judge in-session.
- **Discover, don't memorize.** Playbook ids and params come from the CLI, not from docs.

## Listing and reading playbooks

```bash
cwpilot play playbooks                  # every resolvable playbook: id, label, description
cwpilot play playbooks <playbook_id>    # one playbook: params, start step, every step
```

The first is the live registry — the source of truth over any list written down. The second
self-describes a playbook end to end, including which params are required. `--json` on either.

## Running one

**Two entry points, and which one is right depends on whether the playbook is *about* a node.**

```bash
# A playbook parameterized entirely by its own params (a guidance or utility playbook):
cwpilot play create --playbook <id> [--param K=V ...]

# A playbook about a node — normally a WorkNode:
cwpilot play from-node <node_path> --playbook <id> [--param K=V ...] [--mode subagent|inline|fork]
```

Don't make a nodeless playbook borrow an unrelated node: `from-node` binds the run to it and
its evidence trail then points at work it never touched. `cwpilot play create` exists for exactly that
case — it is nodeless, always inline, and skips the queue entirely.

`from-node` branches on the source node's `kind`:

- **a WorkNode** → it creates a **QueueEntry**, not a run. The run is materialized only when
  the entry starts, and only if its lane is free — a busy lane leaves it `pending`. Unapproved
  work is refused outright: the approval gate sits on the enqueue primitive, beneath the CLI.
  **This is `cwpilot:execute_work`' territory; don't drive it from here.**
- **any other node** → an operational PlaybookRun directly in `running`, no queue, no lane.

Required params are enforced at start — omit one and it fails before the first step renders.

Then step it:

```bash
cwpilot play show <run_id>            # where the run stands, and what facts advance it
cwpilot play show <run_id> --step     # the current step's instruct — show alone does NOT print it
cwpilot play advance <run_id> --fact K=V ...
```

`advance` picks the single valid transition and errors on none or several. The final `advance`
completes the run and records its evidence; there is no separate "complete" verb.

The run id is printed when the run is created; its short 8-hex tail works everywhere the full
id does. Don't derive it — read it back with `cwpilot play show`.

`cwpilot play debug <playbook_id>` is the odd one out: it steps a playbook DEFINITION, with no
run and no writes — see *Authoring one*.

Other verbs (`facts`, `fail`, `resolve`, `park`, `ping`, `liveness`, `restore`,
`force-complete`) are lifecycle and recovery, governed by `cwpilot:execute_work`.
`cwpilot play --help` is the authoritative list.

## Preinstalled playbooks

| id | what it does |
|---|---|
| `implement_work` | Implements a WorkNode. Run it through `cwpilot:execute_work`, never from here. |
| `add_verified_constraint` | Walks a constraint into `spec.k.json` until verified — draft the `cmd`, add it, make it fail once, make it pass. Nodeless: `cwpilot play create --playbook add_verified_constraint --param feature_id=… --param constraint_id=… --param behavior=…` |

`cwpilot play playbooks` also lists `subagent_handoff`. It is a defaults base a step names in
`import:`, not a runnable playbook — it declares no steps and starting it is refused.

## Where playbooks come from

`overridable_playbooks:` in `knowledge_config.yaml` — the project's file if there is one, else
the plugin's. It lists **folders**; every `.yaml` in each is a playbook.
File name should correspond to the playbook's `id`.

```yaml
overridable_playbooks:
  - playbooks
```

- A file's **name is its id**: `playbooks/standard/implement_work.yaml` → `implement_work`.
- Relative paths resolve against `knowledge_config.yaml` itself, not the cwd.
- **Order is precedence and the last entry wins** — a later folder declaring the same id
  overrides the earlier one. That is what *overridable* means; there is no `standard` tier.
- Adding a playbook is dropping a file into a listed folder. No config edit, no registration.

## Authoring one: audit_dependencies.yaml

```yaml
id: audit_dependencies          # == filename
label: Audit third-party dependencies
description: One or two sentences on what this is for.
version: 1.0.0
visibility: user_facing
params:                         # required at bind time; node_path is always bound
- node_path
start: scan
steps:
  scan:
    step_id: scan
    label: Scan the manifests
    instruct: |-
      Scan the manifests under {node_path} and list every outdated dependency.

      Set facts: `findings_count`.
    fact_schema:
      - name: findings_count
        type: int
        description: How many outdated dependencies were found.
    next:
      finish:
        when: the scan finished
        predicate: exists(.facts.findings_count)
  finish:
    step_id: finish
    label: Report
    instruct: 'Summarise the findings.'
    next: {}                    # no transitions == terminal
```

Then check it two ways, neither of which creates a run:

```bash
cwpilot play playbooks audit_dependencies          # loads it the way the system does — the validity check
cwpilot play debug audit_dependencies --at scan \
  --param node_path=/graph --fact findings_count=3 # render one step and evaluate its gates
```

`cwpilot play debug` steps the DEFINITION: it substitutes `{param}` into the `instruct`, and
computes each transition's predicate against the `--fact`/`--node` you supply, so you can see
a gate say `✓ TRUE` before you author against it. `--at` picks the step (default: the start
step). It writes nothing unless you pass `--run-id`, which persists step history to a run.

- **`Playbook` is a strict model.** An undeclared top-level key stops the file loading
  *entirely*. Per-playbook or per-step settings go in the `policy` bag, which exists for that.
- **Name the terminal step `finish`** — it closes out in one fewer `advance` than any other name.
- **`handoff: true`** on a step makes the run spawn a fresh sub-agent for that one step. Use it
  where fresh context matters — an entry step, or verification that must not be self-certified.
- **`close_out_eligible: true`** lets a walk leave that step without a fresh call. Set it only
  where the gating fact is genuinely verified, never merely asserted.
