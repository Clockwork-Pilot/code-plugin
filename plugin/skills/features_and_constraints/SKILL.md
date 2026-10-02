---
name: cwpilot:features_and_constraints
description: Design features and the ConstraintBash commands that guard them in spec.k.json / project.k.json, and run them with cwpilot check. A constraint is verified only by proving it BOTH ways (failed_once AND passed_once); verified means locked, unverified means it blocks all source edits. Owns the add_verified_constraint playbook end to end — when to reach for it, what you end up with, and the judgments the state machine cannot make — plus the dev loop a violation forces.
---

# Features and Constraints

A **Spec** (`spec.k.json`) holds the features and constraints for one area of the codebase. A
**Project** (`project.k.json`) is an optional top-level index referencing several specs, each
with its own environment; when present, one check covers every spec it lists.

A constraint is a shell command that guards a behaviour. Its lifecycle is the whole point of
this skill: it is **unverified** — and blocking — until it has **failed once AND passed once**.

| Phase | Tool |
|---|---|
| Design | `patch-knowledge-document spec.k.json '[...]'` — add features/constraints |
| Render | `patch-knowledge-document spec.k.json` — regenerate `spec.k.md` after any edit |
| Verify | `cwpilot check` — the only thing that records `failed_once`/`passed_once` |

`.k.md` files are generated from the JSON. Re-render after every spec edit; re-rendering a
`project.k.json` renders every spec it references.

**Be deliberate.** A poorly written constraint blocks all development: verified, it is
immutable; unverified, it blocks source edits until it fails. Add only constraints you are
confident fail now for the right reason and will pass once the feature is correct.

## Verification model (the part everything else follows from)

- **verified = `failed_once` AND `passed_once`** — proven both ways. Only the checker sets
  these flags; `Constraint.verified` is computed from them.
- **verified ⇒ locked**: `cmd` is immutable and the constraint cannot be removed. `description`
  stays editable.
- **not verified ⇒ editable**: a constraint that has only failed, or only passed, keeps its
  `cmd` editable. A buggy command can always be fixed — you cannot get trapped.
- **never failed ⇒ blocking**: any constraint with `failed_once=False` sets the spec's
  `contains_unverified_constraints`, which **blocks all source edits until it fails once**.
  Resolving that is TOP PRIORITY over any other work.

There is no `fails_count` — it is a legacy field, rejected at load under strict validation.
A constraint is not verified by failing twice; it is verified by failing once and passing once.

# Designing a constraint suite

Add the feature, then its constraints:

```bash
patch-knowledge-document spec.k.json '[{"op": "add", "path": "/features/my_feature", "value": {
  "type": "Feature", "model_version": 1, "id": "my_feature",
  "description": "Concise summary (max 100 chars)",
  "goals": "Detailed knowledge driving constraint design",
  "constraints": {}}}]'
```

Cover all four categories — a suite missing one has a blind spot:

| Category | What it checks |
|---|---|
| **Structural** | File presence, imports, naming, schema shape |
| **Behavioral** | Input A → Output B through the real CLI/API |
| **Environmental** | Side effect: file written, record created, port opened |
| **Negative/Security** | Bad or unauthorized input is strictly rejected |

A `ConstraintBash` is `id` (snake_case, unique in the feature), `cmd` (exit 0 = PASS), and
`description` (what it checks and why). Optional `graph` holds a `MermaidFlowchart` — for a
non-trivial check, author it with `opts.auto_tree_edges: false`, rhombus decision nodes and
labeled yes/no links (schema in `cwpilot:knowledge_document_tools`).

**Exit code reflects the test, not the echo.**
```bash
grep -q 'pattern' file && echo 'Found' || echo 'Not found'   # WRONG — always exits 0
test -f file || { echo "Error: missing"; exit 1; }            # CORRECT
```

**Zero-State Rule.** Every constraint MUST FAIL on an empty codebase. One that passes on an
empty repo is worthless — rewrite it.

**Use `$PROJECT_ROOT`, never an absolute path.** Constraints test the project, and the checker
substitutes `$PROJECT_ROOT`/`${PROJECT_ROOT}` in the command string (`PROJECT_ROOT` and
`PLUGIN_ROOT` are both in the constraint's environment).
```bash
test -f $PROJECT_ROOT/path/to/feature    # Correct
test -f /project/path/to/feature          # Wrong
```

**Assert the payload, not a success string.** `grep -q '"status": "active"' output.json`, never
`grep -q "success" output.log`.

**Author `cmd` as multi-line shell**, one step per line, so it is readable in `spec.k.json` and
in failure output (embed real `\n` in the JSON string when patching).

# The `add_verified_constraint` playbook

A constraint you just wrote is a claim, not a guard. It might pass because the behaviour
holds — or because the command is wrong and would pass on an empty repository. Nothing tells
those apart except making it fail on purpose, which is why verification takes both halves and
why the sequence is worth not improvising. It is encoded as the `add_verified_constraint`
playbook.

Reach for it whenever you add a constraint — above all when the guarded behaviour **already
works**, because then the deliberate failure is the step most likely to be quietly skipped,
and when you expect to iterate on the `cmd`, which its loop-back edges exist for.

It is about no node — its subject is its own params — so it starts with `cwpilot play create`, which
binds no source. `cwpilot play from-node` is for a playbook that acts ON something, and making this
one borrow an unrelated node writes a second, unrelated run onto that node:

```bash
cwpilot play create --playbook add_verified_constraint --mode inline \
  --param feature_id=<feature> --param constraint_id=<id> \
  --param behavior="<what it guards>"
cwpilot play show <run_id>                              # what this step wants
cwpilot play advance <run_id> --fact <key>=<value>      # perform it, then advance
```

**The playbook itself is the instructions.** `cwpilot play show` prints the current step's own text
and the facts it wants; `cwpilot play playbooks add_verified_constraint` prints every step and
transition. Neither is reproduced here: a copy of the YAML drifts the moment the YAML
changes, and this one had — it told readers to bind a run to a WorkNode long after that
stopped being how you start it. (`cwpilot play debug` steps the same playbook statelessly, recording
no run at all — fine for reading the steps, but nothing afterwards can tell the work was
done.)

## What you end up with

When the run reaches `done`, the constraint sits in `spec.k.json` carrying both
`failed_once` and `passed_once`. That is the whole result, and everything follows from it:

- its `cmd` is locked and it can no longer be removed — `cwpilot check` failing on it from
  now on means the source changed, never that the check drifted;
- the spec's `contains_unverified_constraints` clears, so source edits are unblocked;
- `cwpilot check` is green, including the deliberate breakage you introduced and restored;
- the run itself is gone from the document — it was operational, so completing it disposed of
  it. Nothing lingers except the constraint, which is the point.

Finishing with only one of the two flags set means the playbook did not do its job: you
proved half of what a guard has to prove.

## What the playbook cannot judge for you

The state machine records *that* a failure happened, never that the failure meant anything,
and both halves of the proof can be satisfied by accident.

A `cmd` with a typo fails, and that failure records `failed_once` just as convincingly as a
real one — so a constraint can reach "verified" having never once observed the behaviour it
claims to guard. That is what the loop back to `draft` is for, and why the deliberate failure
is worth reading rather than glancing at.

The mirror case is a check that cannot fail: a grep over a path that no longer exists, or one
that would pass on an empty repository. It sails through `pass_once`, never earns
`failed_once`, and leaves the spec permanently blocking edits — a constraint that can only
pass is worse than no constraint at all.

When the behaviour already works, the honest failure comes from breaking the implementation —
move the file, revert the code — then restoring it. Breaking the `cmd` instead proves only
that a broken command fails.

*Worked example:* `constraint_pyrightconfig_exists` on `pyright_config` — the file already
existed, so `fail_once` moved it aside (FAIL → `failed_once`), `pass_once` restored it (PASS →
`passed_once` → verified), final check green.

# Running the checker

**Always `cwpilot check`.** Never run a constraint's `cmd` by hand for validation and never
write a custom validation script: manual execution bypasses exit-code validation, error
filtering, history recording and ChecksResults generation — and it is the only thing that
records `failed_once`/`passed_once`.

```bash
cwpilot check [<project.k.json>|<spec.k.json>] [--features f1,f2] \
              [--output-checks-path spec-checks.k.json] [--dry-run] [--full-report]
```

With no positional argument it resolves `$PROJECT_ROOT/project.k.json` (checking every spec it
lists) and falls back to `$PROJECT_ROOT/spec.k.json`. `--dry-run` re-renders the previous run's
report from `spec-checks.k.json` without executing anything — use it to review results after
context loss instead of re-running the suite. Without `--full-report`, success prints just
`PASS`.

**When to run:** while verifying a new constraint (the fail-once/pass-once dance above), or when
the user asks for it by name. **Do NOT run it as a generic completion gate** — "after finishing
implementation", "before marking complete". The Stop hook already runs the same check when it is
needed; a blanket re-run buys no information. Narrow, feature-scoped runs while iterating on
that feature stay fine.

# When a constraint fails

Read its `description`, run its `cmd` manually to debug, fix the **source**, re-run
`cwpilot check`, repeat. For an unverified constraint you may also fix the `cmd` itself
(`patch-knowledge-document` a `replace` on `.../constraints/<id>/cmd`) — that option disappears
the moment it is verified.

These escapes do not work: removing or rewriting a verified constraint (`cmd` is locked),
hand-editing `spec-checks.k.json`, or reintroducing `fails_count`. Once verified, the only way
out of a dev loop is changing the source so the constraint passes.

# Absolute rules

- **Never edit a `.k.json` directly** — always `patch-knowledge-document`.
- **Never delete a `spec.k.json`.** They are immutable system artifacts defining feature
  contracts; restructure in place. Refuse any request to remove one.
- **Never run constraint commands manually for validation** — `cwpilot check` only.
- **Never write a fake or trivially-passing check.** Unconditional `exit 1` can never pass and
  traps you; a trivially-passing constraint never verifies and blocks every source edit.
