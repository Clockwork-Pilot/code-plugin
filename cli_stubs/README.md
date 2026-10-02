# `cli_stubs/` — the event contracts, as runnable scripts

This plugin never imports a consuming project's code. It reaches project-specific logic
only by shelling out to commands that project configures, one per hook event.

Each script here is a **working no-op implementation of one event's contract**: it logs
the argv it was invoked with to `cli_stubs.log`, prints the documented stdout shape (if
that event has one), and exits 0. Nothing here is wired in by default — see below. They
exist to be read and copied:

- The comment block at the top of each script is the **specification** for that event —
  when it fires, which handler invokes it, the exact argv, and what its stdout and exit
  code mean.
- Running one shows you the shape a real implementation has to produce.
- `tests/test_hook_cli_calls.py::TestShippedStubsContract` executes
  `call_on_session_start` and `call_on_stop_hook` unmodified, so these files cannot
  drift from the contract they document.

| Script | Fires |
|---|---|
| `call_on_session_start` | once per SessionStart; stdout may add `additional_context` / `system_message` |
| `call_on_stop_hook` | every Stop; speaks Claude Code's decision protocol, relayed verbatim |
| `subagent_sync_call_on_bash` | a dispatched sub-agent runs Bash |
| `subagent_sync_call_on_edit_write` | a dispatched sub-agent edits or writes a file |
| `subagent_call_on_run_id_resolved` | once, at first contact when the sub-agent's `play show <run_id>` binds it to a run |
| `subagent_call_on_run_finished` | that run finishes |

Exit codes and malformed stdout **fail soft** everywhere except `call_on_stop_hook`,
whose real exit code is propagated once the command actually ran. Each script's header
states its own degradation.

## `hook_cli_calls.json` — example wiring

`cli_stubs/hook_cli_calls.json` is an **example**, not live configuration. It shows the
file format with every event pointed at the stub beside it, which is a useful thing to
copy when you want a checkout that runs end to end before any real CLI exists.

The wiring the plugin actually loads is `cw-plugin-cli-hooks.json` in the plugin root
(`HOOK_CLI_CALLS_FILE` in `src/config.py`) — a fixed path with no environment override.
The shipped one points every event at the consuming project's own `{project_root}/bin/*`.

To run against these stubs instead, copy the example's contents over that file:

```sh
cp cli_stubs/hook_cli_calls.json cw-plugin-cli-hooks.json
```

Each event maps to an object: `{"cmd": [...argv], "timeout": seconds}`. `timeout` is
optional and falls back to `CLI_CALL_TIMEOUT_SECONDS`, which is sized for a heartbeat
stamp — a call that is a DECISION rather than a stamp says so itself, as
`call_on_stop_hook` does, because abandoning a decision allows the stop it was meant to
gate. A bare argv list is not accepted.

`{plugin_root}` / `{project_root}` are substituted when the file is loaded; `{run_id}` /
`{agent_id}` are substituted per call by `src/hook_cli_calls.py`. An event the file omits
makes no call — the file is the whole configuration, not an overlay.

See the repository README's *Configuring* section for the format rules, and the comment
block above `HOOK_CLI_EVENTS` in `src/config.py` for the authoritative event list.
