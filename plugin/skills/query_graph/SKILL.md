---
name: cwpilot:query_graph
description: Discover query-graph methods and inspect graph settings read-only.
---

Load when querying graph data directly.

## Querying and discovery

**`overridable_playbooks`** (list of paths)
- Folders containing `.yaml` playbook definitions
- Later entries override earlier ones (last wins)
- Projects can override bundled playbooks by adding their own folder

### Discovering methods
Query graph supports plenty of predefined query methods:
```bash
cwpilot query-graph --methods                # one line per method + its describe:
cwpilot query-graph <method> --help          # that method's params, scope root, an example
```

For instance check particular settings:

```bash
# select and project entire settings value
cwpilot query-graph executable_graph.k.json '{"select":{"type":"ExecutableGraph"},"project":["settings"]}'
# select and project particular settings value
cwpilot query-graph executable_graph.k.json '{"select":{"type":"ExecutableGraph"},"project":["settings.enable_works_approval.value"]}'
```
