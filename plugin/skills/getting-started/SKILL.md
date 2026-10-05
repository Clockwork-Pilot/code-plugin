---
name: cwpilot:getting-started
description: Finish setting up the plugin by installing its programs, and find the right cwpilot to run. Use right after installing the plugin, or when cwpilot is missing, outdated, or refused.
---

Load right after installing this plugin, or when `cwpilot` is not found, is refused by the
PreToolUse guard, or SessionStart reports a setup problem (missing, incompatible, or failing
verification).

# Getting started

Installing the plugin adds its skills and hooks. Its programs, including the `cwpilot`
command the skills rely on, come from one setup command that the user approves. Everything
below is about running that command once and then knowing which `cwpilot` to run.

## 1. Ask where cwpilot is

Run the setup helper, which lives in the plugin next to this skill. First get this skill's
directory:

- Claude Code: the "Base directory for this skill" line at the top of this skill's text.
- Codex: the skill's path in the skill catalog (the folder holding its `SKILL.md`).

Then run:

```bash
<skill directory>/../../dist/cwpilot-path.sh
```

Write the whole path out literally, with the skill directory pasted in and the `..` kept as
is. Do not put it in a shell variable or `$(...)`: the PreToolUse guard refuses a command it
cannot read literally.

- It prints one line, the command to type for cwpilot: either plain `cwpilot` (when `$PATH`
  already reaches the verified binary) or an absolute path. Setup is done: go to step 3.
- If it fails, stdout is empty and stderr says why and gives the exact setup command to run.
  Go to step 2. The exit code tells the two failures apart:
  - `1`: cwpilot is not installed.
  - `2`: cwpilot is installed but fails its stored signature (or has none).

If you cannot tell this skill's directory, use the helper path from this session's
SessionStart context ("Plugin setup helper: `…/dist/cwpilot-path.sh`") instead, and if that
is missing too, ask the user to restart the session. Do not hunt for the plugin directory.

Do not search for `cwpilot` yourself (`ls`, `which`, `find`, `$PATH`), and do not reuse a path
from memory or an earlier session. Only what the helper prints is valid.

## 2. Run setup, with the user's consent

The setup command downloads and runs a remote script, so ask the user first
(AskUserQuestion on Claude Code; Directly in the message on Codex), once per session. Do not run it silently, and do not ask
again after an answer.

Run the command exactly as the script's stderr or the SessionStart notice gives it. It
already has the right version in it: do not edit it, add arguments, or write your own.

## 3. Verify and use

Run the helper from step 1 again; it must now print a command. Use exactly that for every
cwpilot command this session: if it printed `cwpilot`, type plain `cwpilot ...`; if it
printed a path, run `<path> ...` instead. If a command is refused, re-run the helper and
use what it prints. Keep these as separate commands: never write
`cwpilot ... || <path> ...`.

No new session is needed. If the helper still fails after setup, report its stderr and the
setup command's output to the user rather than working around it, propose filing a report on GitHub issues.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Script exit 1, or `cwpilot: not installed` | Step 2. |
| `not compatible with this plugin` | Run the setup command again; it updates to the matching version. Do not run the old one. |
| Script exit 2, or `does not match its stored signature` | Run the setup command again; it re-verifies and repairs. Do not run that cwpilot meanwhile. |
| `found but not executable` | Check the file's execute permission; re-run setup if it persists. |
| SessionStart offers to install a faster hook runner | Optional. Offer it once; the plugin works without it. |

Once cwpilot runs, continue with the `cwpilot:cwpilot` skill;
