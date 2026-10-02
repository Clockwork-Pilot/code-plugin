---
name: cwpilot:bootstrap
description: Bootstrap a cwpilot project before any graph commands when no graph document exists.
---

Load when `executable_graph.k.json` does not exist.
If `executable_graph.k.json` is absent, run `cwpilot bootstrap` immediately, before `cwpilot pull`, `play`, or `query-graph`.
```bash
cwpilot bootstrap
```
