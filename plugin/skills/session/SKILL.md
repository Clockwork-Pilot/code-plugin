---
name: cwpilot:session
description: Set or clear session-scoped creative/architect mode, and see what it restricts.
---

Use this skill when the user asks to enable, check, or disable creative or architect
mode. These modes provide a planning-oriented session context; clear the mode to
return to the normal default context.

## Commands

```bash
cwpilot session set creative    # or: architect
cwpilot session show            # prints the active mode, or "default"
cwpilot session clear           # returns to default -- works even from inside creative/architect mode
```

Verbs are `set`/`show`/`clear` directly, with no `mode` subcommand prefix.
