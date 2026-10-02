---
name: cwpilot:patch
description: write / patch knowledge documents (.k.json) in transactional way. Discover document schema.
---

This skill drives the knowledge document patching and schema workflow:
- **Patching**: Apply **RFC 6902 JSON Patch** operations with **transactional semantics** (atomic writes, conflict detection, pluggable triggers, Markdown rendering)
- **Schema Discovery**: Query document structure (paths, field types, available models)

# Knowledge documents
Knowledge documents are structured JSON files that mutated exclusiely using `cwpilot patch` based commands in separate transactions.

We claim that all the files matched by following patterns are protected knowledge documents:

- **`xxxxxxx.k.json`** — is the source of truth canonical knowledge document in JSON format.
- **`xxxxxxx.k.md`** — auto-generated Markdown representation of `xxxxxxx.k.json`.

> ⚠️ Both files are protected (read-only). Do not edit them directly.
> Sometimes user omits `.k` sub-extension in knowledge files. We support `*.json`, `*.md` natively as well.

* Prevent direct updates / Direct updates enforcement

## Crete a new knowledge document

`cwpilot patch --create-document`

## Schema Discovery (before patching)

Always inspect the schema first — **don't blind-patch**:

```bash
# List available model types
cwpilot patch --list-models

# See JSON Pointer paths and field types — accepts a bare model name OR a document.
cwpilot patch --schema-paths Doc
cwpilot patch --schema-paths <doc.k.json>
```

For each field shown:
- Each line is the complete JSON Pointer (copy-pasteable as a patch `path`)
- Field types with clean syntax:
  - `string?` for nullable types (suffix `?`)
  - `dict[str, Type]` for dictionaries (path gains a `{key}` segment)
  - `Type[]` for arrays (path gains a `{i}` index segment)
  - Type names for referenced models (e.g., `Metadata`, `Doc`)

## Patching

Apply patches with one of these patterns:

**Canonical (stdin) — for multi-op patches:**
```bash
cat patch.json | cwpilot patch --stdin <doc.k.json>
```
Avoids shell escaping issues. This is the safest pattern.

**Inline — for simple single-op patches:**
```bash
cwpilot patch <doc.k.json> '[{"op": "replace", "path": "/label", "value": "New"}]'
```

## Rendering and flags

Render and skip patching (no changes to JSON)
`cwpilot patch --render <doc.k.json> ['<json-patch>']`

### Best practices

**Prefer stdin for multi-op patches** — Avoids shell escaping headaches. Use `cat patch.json | cwpilot patch --stdin doc.k.json`.

**Use focused patches over monolithic ones** — A patch list is atomic (all ops succeed or fail together). Use that for ops that genuinely belong together. Don't bundle unrelated changes:
```bash
# Good — related ops together (adding a node and its state)
cwpilot patch doc.k.json '[
  {"op": "add", "path": "/children/foo", "value": {...}},
  {"op": "add", "path": "/children/foo/children/design_ref", "value": {...}}
]'

# Avoid — unrelated nodes bundled into one patch
cwpilot patch doc.k.json '[...ops for foo, bar, baz, qux...]'
```

# Transactional Model

Every patch operation is executed as an **atomic transaction** with the following guarantees:

1. **Atomicity**: Either all changes are applied or none. Temp-file + atomic rename prevents partial writes.
2. **Conflict detection**: Optimistic locking via `updated_at` timestamp (ISO 8601 with microseconds). If another process modifies the file between read and write, the transaction is aborted.
3. **Validation**: Document is validated against the Pydantic schema before write.
4. **Pluggable triggers**: Pre-triggers (in-memory, can modify or abort) and post-triggers (side effects, after commit).
5. **Bounded execution**: The trigger cascade has a wall-clock budget (`KNOWLEDGE_TOOL_TX_TIMEOUT`, default `5` seconds). If handlers keep emitting new ops past the deadline, the transaction is aborted before any write — no partial state is committed.

### Transaction Failure Modes

| Mode | When | Outcome |
|---|---|---|
| **Handler exception** | An `after_batch` trigger raises an exception | Cascade aborted, document untouched |
| **Validation failure** | Final document fails Pydantic schema validation | Transaction aborted, document untouched |
| **Conflict detected** | File modified by another process between read and write | Transaction aborted, document untouched |
| **Cascade overflow** | Cascade did not settle within `KNOWLEDGE_TOOL_TX_TIMEOUT` (default 5s) — defensive guard against runaway emissions | Transaction aborted before write, document untouched |
| **Render failure** | Rendering failed after JSON already committed | JSON write stands. Warning logged. Operation reports success |
| **Trigger veto** | An `after_batch` trigger returned `Veto(reason)` | Engine raises `CascadeError`, aborts entire transaction — nothing written |

# JSON Patch Op Cheatsheet

RFC 6902 has six operations. The three you'll use 95% of the time:

| Goal | Op | Path pattern | Example |
|---|---|---|---|
| **Create new field** | `add` | `/path/to/new_field` | `{"op": "add", "path": "/metadata/version", "value": "1.0"}` |
| **Update existing field** | `replace` | `/path/to/existing_field` | `{"op": "replace", "path": "/label", "value": "Updated"}` |
| **Delete field** | `remove` | `/path/to/field_to_delete` | `{"op": "remove", "path": "/children/old_id"}` |
| **Append to array** | `add` | `/array_path/-` (dash means "end") | `{"op": "add", "path": "/tags/-", "value": "new_tag"}` |
| **Insert at array index** | `add` | `/array_path/N` | `{"op": "add", "path": "/tags/0", "value": "first"}` |
| **Replace array element** | `replace` | `/array_path/N` | `{"op": "replace", "path": "/tags/0", "value": "changed"}` |
| **Remove array element** | `remove` | `/array_path/N` | `{"op": "remove", "path": "/tags/0"}` |

Also available but rarely needed: `move`, `copy`, `test`.

# Common Pitfalls and Recovery

| Symptom | Cause | Fix |
|---|---|---|
| `Path not found: ... 'foo'` with hint `Did you mean: 'fooo'?` | Typo in path | Use the suggested key. Run `--schema-paths` to see all valid paths. |
| `Path not found` + hint `use 'add' instead of 'replace'` | Used `replace` on a path that doesn't exist yet | Switch to `add` |
| `Path not found` on `add` to existing path | Used `add` where the value already exists | Switch to `replace` (or `remove` then `add`) |
| `Invalid JSON Patch syntax` | Patch JSON is malformed, or it's not an array of operations | Validate the JSON; remember it must be a JSON **array** even for a single op |
| Shell escaping a complex patch goes wrong | Embedded quotes get mangled | Use `--stdin` and pipe the patch JSON in |
| `Document validation failed` | Patched result missing required fields or wrong types | Run `--schema-paths MODEL_TYPE` to see valid paths, required vs optional fields, and types |
| `Document was modified by another process` | Concurrent write between your read and write | Re-read the file, recompute your patch against current state, retry once |
| `cascade exceeded Ns timeout (runaway emissions?)` | Triggers kept emitting ops past the transaction deadline | A handler is in a loop, or a legitimate cascade needs more headroom. Inspect handlers; if the work is genuine, raise `KNOWLEDGE_TOOL_TX_TIMEOUT` (seconds). The transaction was aborted before write — file is unchanged. |
| Rendered markdown looks wrong | Stale renderer output | Re-run the command with no patch (re-renders by default) |

# Patches are applied sequentially

A JSON Patch list is an ordered sequence. Each op is applied **after** the previous one. This means:
- A path that exists after op #1 might not have existed before op #1
- An `add` followed by a `replace` to the same path is valid; the reverse is not
- Order your ops so each path is valid at the moment of application

# Tool for creating knowledge documents — `${PLUGIN_ROOT}/bin/cwpilot patch`
Creates a new knowledge document of a specified model type and initializes it with default values.
