#!/usr/bin/env python3
"""Stop hook handler - invokes the configured Stop CLI and relays its decision.

Normally a pure relay: ``call_on_stop_hook``'s exit code IS this hook's decision
(2 = block, per the harness convention) and its stdout carries the
``{"decision": ...}`` JSON Claude Code's own Stop-hook protocol reads. See
cli_stubs/call_on_stop_hook for the documented interface.

ONE exception -- the keep-warm poll. A provider with no self-scheduling primitive
(anything but claude-code, which has ScheduleWakeup) gets a decision payload
carrying ``keepCacheWarm: {maxTimeoutSeconds: N}``. Rather than relay it straight
through -- which would let the session stop and never resume -- this hook BLOCKS,
polling session_main.json's ``running_agents`` map once a second, and re-emits the
payload (exit 2) the instant every dispatched sub-agent has a ``finished_at``, or
after N seconds, whichever comes first. The blocking keeps the session process --
and its prompt cache -- alive across the wait.

For Codex and Claude Code, the final JSON object is shallow-filtered against that
provider's Stop-hook output fields after the keep-warm directive has been consumed.
"""

import json
import os
import sys
import time


from src.config import LOG_ENVS
from src.hook_logging import redact_session_fields, setup_logger

logger = setup_logger("Stop", log_envs=LOG_ENVS)

# Keep the CLI seam available for tests without resolving project-root configuration at
# handler import time. The real callable is loaded only when the handler runs.
invoke_cli_call = None

_POLL_INTERVAL_SECONDS = 1.0
_CODEX_STOP_FIELDS = frozenset({
    "continue",
    "stopReason",
    "systemMessage",
    "suppressOutput",
    "decision",
    "reason",
})
_CLAUDE_CODE_STOP_FIELDS = frozenset({
    "continue",
    "stopReason",
    "suppressOutput",
    "systemMessage",
    "terminalSequence",
    "decision",
    "reason",
    "hookSpecificOutput",
})
_STOP_FIELDS_BY_PROVIDER = {
    "codex": _CODEX_STOP_FIELDS,
    "claude-code": _CLAUDE_CODE_STOP_FIELDS,
}


def _project_root():
    """Resolve the consuming project only when a relay record needs it."""
    from src.config import project_root

    return project_root()


def _keep_warm_timeout(payload_text: str):
    """maxTimeoutSeconds from a ``keepCacheWarm`` decision payload, or None when the
    payload is absent / not JSON / carries no such directive."""
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    directive = payload.get("keepCacheWarm")
    if not isinstance(directive, dict):
        return None
    timeout = directive.get("maxTimeoutSeconds")
    return timeout if isinstance(timeout, (int, float)) and timeout > 0 else None


def _poll_until_subagent_finishes(max_timeout_seconds: float) -> None:
    """Block until every dispatched sub-agent has stamped finished_at, or the timeout
    elapses. Reads a FRESH session ledger each tick -- SubagentStop writes finished_at
    from a separate process. Best-effort: any ledger error ends the wait (release)."""
    from src.ledger import session_ledger  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + max_timeout_seconds
    while time.monotonic() < deadline:
        try:
            still_running = session_ledger().unfinished_agent_ids()
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(f"keep-warm poll: ledger read failed, releasing: {exc}")
            return
        if not still_running:
            logger.info({'keep_warm': 'released_on_subagent_finish'})
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    logger.info({'keep_warm': 'released_on_timeout',
                 'max_timeout_seconds': max_timeout_seconds})


def _filter_stop_payload(payload_text: str, allowed_fields: frozenset[str]) -> str:
    """Drop top-level fields the current provider does not support for Stop."""
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError):
        return payload_text
    if not isinstance(payload, dict):
        return payload_text

    filtered = {key: value for key, value in payload.items()
                if key in allowed_fields}
    return json.dumps(filtered) if len(filtered) != len(payload) else payload_text


def relay_decision(stdout: str, exit_code: int) -> None:
    """Apply Stop's keep-warm delay, then filter provider-unsupported fields."""
    logger.info({
        'phase': 'relay_decision',
        'project_root': str(_project_root()),
        'exit_code': exit_code,
        'stdout': stdout,
    })
    max_timeout = _keep_warm_timeout(stdout)
    if max_timeout is not None:
        _poll_until_subagent_finishes(max_timeout)
    output = stdout
    allowed_fields = _STOP_FIELDS_BY_PROVIDER.get(
        os.environ.get("CWPILOT_AGENT_PROVIDER", "")
    )
    if allowed_fields is not None:
        output = _filter_stop_payload(stdout, allowed_fields)
    if output:
        sys.stdout.write(output)
    sys.exit(exit_code)


def main():
    global invoke_cli_call
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}
    logger.info({'hook_input': redact_session_fields(hook_input)})

    if invoke_cli_call is None:
        from src.hook_cli_calls import invoke_cli_call as configured_invoke

        invoke_cli_call = configured_invoke

    logger.info({'phase': 'invoke_cli'})
    try:
        execution = invoke_cli_call("call_on_stop_hook")
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(f"Error: {exc}")
        sys.exit(1)

    stdout = execution.result.stdout if execution.completed else ""
    exit_code = execution.result.returncode if execution.completed else 0
    if execution.completed and execution.result.stderr:
        sys.stderr.write(execution.result.stderr)
    # The configured CLI's decision is what reaches the harness -- verbatim stdout,
    # real exit code. The keep-warm poll only decides when.
    relay_decision(stdout, exit_code)


if __name__ == '__main__':
    main()
