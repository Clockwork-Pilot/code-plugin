#!/usr/bin/env python3
"""Single entrypoint for every hook handler: ``python3 -m hooks <handler_name>``.

Lives in the package it routes into, so there is one hooks entrypoint and no
router floating at the repo root. ``hooks/__init__.py`` stays pure utilities --
every handler imports it, and an entrypoint there would run on each of those
imports (and could not be reached by ``-m`` anyway, which always executes this
file).

Dispatches by handler name so the separate Nuitka flow ships one binary
instead of one per hook.

stdin/stdout/env are passed through untouched -- handlers read their payload from
stdin and configuration from the environment, not from argv, so argv here only
ever carries the dispatch key.
"""

import importlib
import io
import json
import sys

_HANDLERS = (
    "handler_bash",
    "handler_compaction",
    "handler_cwpilot_path",
    "handler_edit",
    "handler_post_change",
    "handler_session_start",
    "handler_stop",
    "handler_subagent_start",
    "handler_subagent_stop",
    "handler_write",
)

_HANDLER_MODULES = {handler: f"hooks.{handler}" for handler in _HANDLERS}

_HANDLER_EVENTS = {
    "handler_bash": "Bash",
    "handler_compaction": "Compaction",
    "handler_cwpilot_path": "CwpilotPath",
    "handler_edit": "Edit",
    "handler_post_change": "PostChange",
    "handler_session_start": "SessionStart",
    "handler_stop": "Stop",
    "handler_subagent_start": "SubagentStart",
    "handler_subagent_stop": "SubagentStop",
    "handler_write": "Write",
}


def _prime_hook_context() -> None:
    """Make hook JSON available to config resolvers before handler import.

    Handlers still own parsing their stdin, so the original text is restored after the
    dispatcher registers the context. This matters because several handlers import the
    CLI seam at module load time, before their ``main()`` can inspect the payload.
    """
    input_data = sys.stdin.read()
    try:
        hook_input = json.loads(input_data) if input_data else {}
    except (TypeError, ValueError):
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}

    from src.config import set_hook_input

    set_hook_input(hook_input)
    sys.stdin = io.StringIO(input_data)

# The build explicitly includes every module in _HANDLER_MODULES. Import the selected
# handler and call main() directly, like cwpilot's compiled CLI dispatcher. Running a
# handler through runpy.run_module() asks Nuitka's loader for get_code(), which its
# compiled module loader does not provide.


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _HANDLERS:
        known = ", ".join(_HANDLERS)
        print(f"usage: python3 -m hooks <handler_name>\nknown handlers: {known}",
              file=sys.stderr)
        return 64  # EX_USAGE

    handler = sys.argv[1]
    sys.argv = [handler] + sys.argv[2:]
    event = _HANDLER_EVENTS[handler]

    # Set up logging before importing the handler. Some handlers import project-root
    # configuration at module load time; a missing root must remain a real error, but
    # it must not prevent the hook from recording why it failed.
    from src.hook_logging import setup_logger

    setup_logger(event).info({
        "phase": "hook_start",
        "handler": handler,
    })
    _prime_hook_context()
    try:
        module = importlib.import_module(_HANDLER_MODULES[handler])
        return module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)


if __name__ == "__main__":
    sys.exit(main())
