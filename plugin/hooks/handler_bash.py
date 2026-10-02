#!/usr/bin/env python3
"""Handler for Bash tool execution."""

import sys
import json



from src.hook_logging import setup_logger, relevant_bash_fields
from src.config  import (
    RUN_ID_EXTRACTION_PATTERNS, SHELL_SEPARATOR_PATTERN,
    PLAY_RUN_SHOW_PATTERN,
)
from src.cwpilot_guard import check_command as check_cwpilot_command
from src.ledger import session_ledger, agent_ledger
from src.hook_cli_calls import (
    invoke as invoke_hook_cli, invoke_throttled_sync, invoke_and_get_payload,
)
import re

logger = setup_logger("Bash")


# Shell operators that separate distinct commands within one bash tool_input string.
# A non-anchored substring search (matching 'play-run'/'play-advance' anywhere in the
# command text) false-positives on commands that merely MENTION those tokens without
# invoking them -- e.g. `grep -n "play-run start <run_id>" hooks/handler_bash.py`, whose
# actual command is `grep`, or a `cat`/`echo` of a docstring containing the same literal
# example text. Splitting on these operators first and then requiring the tool name be
# the actual leading command token of a segment closes that false-positive.
_SHELL_SEPARATOR_RE = re.compile(SHELL_SEPARATOR_PATTERN)


# The one CLI's unified entry point: `cwpilot <subcommand> ...` invokes <subcommand>.
UNIFIED_CLI_NAME = "cwpilot"


def _segment_invokes(segment: str, tool_name: str) -> bool:
    """True if `segment` (one shell-operator-delimited piece of a command string)
    actually INVOKES tool_name as its own command -- bare (``play-run ...``) or
    path-prefixed (``$PLUGIN_ROOT/bin/play-run ...``, ``./bin/play-run ...``) -- not
    merely containing tool_name somewhere inside a quoted argument passed to some
    OTHER command (whose own leading token would be e.g. ``grep``/``cat``/``echo``).
    """
    stripped = segment.strip()
    if not stripped:
        return False
    tokens = [tok.strip('\'"') for tok in stripped.split()]
    if not tokens:
        return False

    def _is(tok: str, name: str) -> bool:
        return tok == name or tok.endswith('/' + name)

    # A denial may name a MULTI-TOKEN form (`play advance`), which is what a project
    # should declare: it says which call of its CLI is forbidden, rather than the tool
    # name alone. The `cwpilot` prefix is optional here for the same reason it is optional
    # for a single name below -- a project declares its own subcommand (`play advance`)
    # while the invocation a sub-agent actually issues is `cwpilot play advance ...`, or
    # the path-prefixed binary. Requiring the prefix to be part of the declaration let a
    # policy's own denial sail past the gate: `play advance` never matched
    # `cwpilot play advance run_x`, because only tokens[0] was ever tried.
    wanted = tool_name.split()
    if len(wanted) > 1:
        candidates = [tokens]
        if len(tokens) > 1 and _is(tokens[0], UNIFIED_CLI_NAME):
            candidates.append(tokens[1:])
        for toks in candidates:
            if (len(toks) >= len(wanted) and _is(toks[0], wanted[0])
                    and toks[1:len(wanted)] == wanted[1:]):
                return True
        return False

    if _is(tokens[0], tool_name):
        return True
    # Single-name denial still catches the canonical form: matching only the first token
    # would let `cwpilot play-advance ...` past a gate that denies `play-advance`, and
    # that is exactly the form the dispatch envelope hands sub-agents verbatim.
    if len(tokens) > 1 and _is(tokens[0], UNIFIED_CLI_NAME):
        return _is(tokens[1], tool_name)
    return False


def _extract_run_id_from_command(command: str) -> str | None:
    """Extract run_id from a REAL command invocation matching config.RUN_ID_EXTRACTION_PATTERNS.

    Each configured pattern is ANCHORED to the start of its shell segment (see the
    patterns themselves in config.py) so it matches only a genuine invocation -- never
    text that merely mentions the command inside a later argument (e.g. a grep pattern
    or a docstring being read/catted). This allows the plugin to be agnostic to specific
    commands -- new commands can be added to the config without modifying this code.

    Args:
        command: The bash command string.

    Returns:
        The extracted run_id, or None if no configured pattern matched.
    """
    if not command:
        return None

    for segment in _SHELL_SEPARATOR_RE.split(command):
        for pattern in RUN_ID_EXTRACTION_PATTERNS.values():
            match = re.match(pattern, segment)
            if match:
                return match.group(1)

    return None


def _play_run_show_run_id(command: str) -> "str | None":
    """Return the run_id of a real ``play-run show <run_id>`` invocation in this command,
    else None. The sub-agent's first action per its dispatch prompt — the delivery trigger."""
    for segment in _SHELL_SEPARATOR_RE.split(command):
        match = re.match(PLAY_RUN_SHOW_PATTERN, segment)
        if match:
            return match.group(1)
    return None


def should_block_subagent_command(command: str, agent_id, denied=None) -> bool | tuple[bool, str]:
    """Block a denylisted command from a spawned sub-agent.

    The denylist is DATA, supplied by the caller: the project authors it in its own
    playbook YAML and the CLI wired to subagent_fetch_policy serves it, fetched per bash
    call. The enforcement RULE is what lives here, never the list itself -- this module
    holds no denials of its own, so an empty or absent policy denies nothing.

    Gated on ``agent_id`` PRESENCE (the caller is a sub-agent), not its value. Returns
    False to allow, or ``(True, message)`` to block. Checks each shell segment for a real
    invocation (not a mere mention in a quoted arg)."""
    if not agent_id:
        return False  # orchestrator/main session — never gated
    denied = denied or ()
    for segment in _SHELL_SEPARATOR_RE.split(command):
        for tok in denied:
            if _segment_invokes(segment, tok):
                return (True, f"Sub-agent cannot run '{tok}': a dispatched sub-agent performs "
                              f"its single step and returns — that's the orchestrator's job.")
    return False


# Back-compat alias: the private name predates the injectable signature.
_should_block_subagent_command = should_block_subagent_command


def main():
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    phase = hook_input.get('hook_event_name', '')
    command = hook_input.get('tool_input', {}).get('command', '')
    description = hook_input.get('tool_input', {}).get('description', '')
    timeout_ms = hook_input.get('tool_input', {}).get('timeout')  # Optional timeout from Bash tool
    agent_id = hook_input.get('agent_id')
    tracker = agent_ledger(agent_id) if agent_id else session_ledger()

    # Extract run_id from command once, used by both PreToolUse and PostToolUse phases.
    # Resolve from the command itself (a real play-run/play-advance invocation) if present,
    # else fall back to this ledger's already-detected in-flight run_id (tagged earlier by
    # record_detected_run). If neither resolves, safely no-op.
    detected_run_id = _extract_run_id_from_command(command) if command else None
    if not detected_run_id and agent_id:
        # For sub-agents, try to load previously detected run_id from agent ledger
        agent_tracker = agent_ledger(agent_id)
        agent_tracker.load()
        detected_run_id = agent_tracker.get_detected_playbook_run_id()

    # Handle PreToolUse: update activity timestamps for staleness detection
    if phase == 'PreToolUse':
        if command:
            # Every agent, sub-agent or not: only the plugin's resolved, verified cwpilot
            # may run -- never a $PATH one (see src/cwpilot_guard.py).
            denial = check_cwpilot_command(command)
            if denial:
                logger.warning({'phase': 'cwpilot_guard_denied', 'command': command})
                sys.stderr.write(f"Bash tool rejected: {denial}\n")
                sys.exit(2)

            # Sub-agent command denylist (agent_id present = the caller is a sub-agent).
            # ENFORCE the universal prohibitions on every call. A sub-agent does its ONE
            # dispatched step and returns — it never advances its run or commits.
            if agent_id:
                # Loaded once per bash call (this hook runs as a fresh process per call
                # anyway); the first-contact dispatch below needs it for its synced_at
                # guard. The denylist is NOT read from here -- see the fetch further down.
                session_tracker = session_ledger()
                session_tracker.load()
                show_run_id = _play_run_show_run_id(command)
                if show_run_id:
                    # FIRST CONTACT is the only reliable moment to bind this agent to
                    # this run. SubagentStart knows the agent but not the run (the
                    # transcript scan that once tried to recover it was retired as
                    # unreliable), and the orchestrator knows the run but not yet the
                    # agent. The sub-agent's own first `play-run show <run_id>` is where
                    # both are finally in the same place.
                    #
                    # Guarded on running_agents[agent_id].synced_at: a sub-agent that
                    # re-runs the identical `play-run show` (e.g. retrying after the
                    # command itself failed) must not re-fire the dispatch CLI call a
                    # second time -- it's a one-shot first-contact signal, not a
                    # per-invocation heartbeat. invoke() itself stamps synced_at as a
                    # side effect once the call actually runs, so there is no separate
                    # "dispatched" field to maintain.
                    already_dispatched = bool(
                        (session_tracker.get_running_agents().get(agent_id) or {}).get("synced_at")
                    )
                    if not already_dispatched:
                        # FIRST CONTACT: the run_id is parsed from THIS command
                        # (`play show <run_id>`), so it is always known here -- logged
                        # so hooks_log makes that auditable rather than assumed. This
                        # call is the SOLE writer of the run's dispatch marker + agent
                        # binding (there is no orchestrator-side stamp).
                        logger.info({
                            'phase': 'subagent_run_id_resolved',
                            'run_id': show_run_id,
                            'agent_id': agent_id,
                            'source': 'play_show_command',
                        })
                        # A pure side-effect call now: it binds this agent to this run and
                        # seeds the throttle stamp (invoke() does that for this event), and
                        # carries no policy. Delivering the denylist here was the defect --
                        # one-shot delivery, cached on the ledger, read back forever after.
                        invoke_hook_cli(
                            "subagent_call_on_run_id_resolved",
                            run_id=show_run_id,
                            agent_id=agent_id,
                            tracker=session_tracker,
                        )
                # THE DENYLIST IS READ, NEVER STORED. Ask for this run's policy on every
                # bash call: the answer is the project's own playbook `policy:` block,
                # carried verbatim on the subagent_fetch_policy binding (a Playbook is
                # never parsed here -- and no process is spawned either; the binding IS
                # the payload). Read with the key the YAML declares, because verbatim
                # means no translation layer: one name, from the project's file to here.
                #
                # Per call, because a cached copy is a copy someone can author: anything
                # able to write the ledger could forge a restriction that bricks the agent,
                # or clear one to remove it. Re-read each time, there is nothing at rest to
                # forge, and no first-contact chain the denial depends on arriving.
                #
                # Fail OPEN: no policy received means no forbidden calls. The project owns
                # what is denied; a plugin-side default would be a second copy of a policy
                # it does not own, and the two would disagree.
                policy = invoke_and_get_payload(
                    "subagent_fetch_policy",
                    run_id=detected_run_id,
                    agent_id=agent_id,
                ) or {}
                denied = set(policy.get("subagent_denied_commands") or [])
                block_result = _should_block_subagent_command(
                    command, agent_id, denied=denied,
                )
                if isinstance(block_result, tuple):
                    blocked, error_msg = block_result
                    if blocked:
                        sys.stderr.write(f"Bash tool rejected: {error_msg}\n")
                        sys.exit(2)

            # Update agent ledger's updated_at to show activity during long commands
            # Activity tracking removed - playbook_runs was removed from ledger

            # Heartbeat onto the RUN itself: a working agent proves liveness by working.
            # Throttled, so this is not a CLI subprocess per bash invocation. The ledger
            # writes above remain the authority for now -- flipping that over is a
            # separate step, and stamping first means nothing reads a fact that no run
            # has yet.
            if agent_id and detected_run_id:
                invoke_throttled_sync(
                    "subagent_sync_call_on_bash",
                    detected_run_id,
                    agent_id,
                )

    # Handle PostToolUse and PostToolUseFailure: log exit code
    if phase in ('PostToolUse', 'PostToolUseFailure'):
        # Activity tracking removed - playbook_runs was removed from ledger

        # Re-stamp the agent liveness heartbeat on the way out of a Post phase.
        # The PreToolUse stamp (line 248-253) proved the agent started work.
        # A command running longer than STALE_THRESHOLD (e.g. e2e test suite, ~5 min)
        # stamps once at start then silent for the whole run -> STALE while alive.
        # The Post-phase re-stamp bounds the heartbeat window at command_duration +
        # throttle instead of unbounded -- still proof the agent was working.
        # Throttled, so not a CLI subprocess per invocation.
        if agent_id and detected_run_id:
            invoke_throttled_sync(
                "subagent_sync_call_on_bash",
                detected_run_id,
                agent_id,
            )

        exit_code = 0 if phase == 'PostToolUse' else None
        error_msg = hook_input.get('error', '')
        is_interrupt = hook_input.get('is_interrupt', False)
        duration_ms = hook_input.get('duration_ms')

        # Extract stdout/stderr from tool_output (available in PostToolUse phase)
        # tool_output structure for Bash: {"content": [{"type": "text", "text": "output"}], ...}
        stdout = ''
        stderr = ''
        tool_output = hook_input.get('tool_output')
        if tool_output and isinstance(tool_output, dict):
            # Extract text content from tool_output.content array
            content_list = tool_output.get('content', [])
            if isinstance(content_list, list) and content_list:
                # For Bash tool, output is typically in the first content block
                for content_item in content_list:
                    if isinstance(content_item, dict) and content_item.get('type') == 'text':
                        stdout = content_item.get('text', '')
                        break

        # Extract exit code from error message: "Exit code N" or "Command exited with non-zero status code N"
        if error_msg:
            import re
            match = re.search(r'(?:Exit code|status code) (\d+)', error_msg)
            if match:
                exit_code = int(match.group(1))

        # Log bash execution with exit code
        log_entry = {
            'phase': phase,
            'command': command,
            'description': description,
            'exit_code': exit_code,
            'data': relevant_bash_fields(hook_input),
        }
        if error_msg and phase != 'PostToolUseFailure':
            log_entry['error'] = error_msg
        if is_interrupt:
            log_entry['interrupted'] = True
        if duration_ms is not None:
            log_entry['duration_ms'] = duration_ms

        logger.info(log_entry)

        # Record bash invocations in persistent tracker (errors fold into this same
        # record's stderr field rather than a separate error_stats structure -- a
        # failed command already gets one bash_invocations entry below, so a second
        # top-level dict recording the identical command/exit_code was pure
        # duplication).
        # Skip self-referential commands (e.g., writes into the .changes-tracker/
        # ledger directory) -- was a 'changes-tracker.json' substring check before
        # the single-file -> per-agent-directory layout change; matches the
        # directory name now so it keeps suppressing noisy self-writes.
        is_self_write = '.changes-tracker' in command

        if phase == 'PostToolUseFailure' and exit_code is not None:
            stderr = error_msg or f"Command failed with exit code {exit_code}"
        elif phase == 'PostToolUse' and exit_code == 0:
            # Update timestamp on success, skip self-writes
            if not is_self_write:
                tracker.update_last_command_at()

        # Record bash invocation (both success and failure), skip self-writes.
        # Bind the reason-of-record (the agent-authored tool_input.description)
        # and the active run/step context: prefer a run_id THIS command itself
        # invokes (a real play-run/play-advance call -- the command group's own
        # declared context), falling back to this ledger's already-detected
        # in-flight run_id (tagged earlier by record_detected_run). When
        # neither resolves, the record carries the free-text reason alone.
        if not is_self_write:
            tracker.record_bash_invocation(command, exit_code=exit_code, duration_ms=duration_ms,
                                           phase=phase, stdout=stdout, stderr=stderr,
                                           reason=description or None)

        # Detect in-flight playbook run from play-run/play-advance commands --
        # SUBAGENT mode only (agent_id present): tag the agent's OWN ledger with
        # the detected run_id, consumed by handler_subagent_stop.py's run_id
        # resolution for killed subagents. Detection is anchored to a REAL
        # invocation (see _segment_invokes) -- never a bare substring match
        # anywhere in the command text (that false-positived on e.g. a grep of
        # this file's own docstrings, which contain the same literal example
        # command text).
        #
        # Record regardless of exit_code so that subagent_call_on_run_finished
        # can retrieve the run_id even if the play-run command itself failed. This
        # ensures the finished hook fires even when the first contact fails (e.g.,
        # play-run command not found, permissions error, etc.).
        #
        # There is no agent_id-absent (inline/main-session) counterpart here:
        # session_main.json is NEVER linked to any particular run_id -- it's a
        # plain, continuous activity log (see session_ledger()/agent_ledger() and
        # HookRuntimeGate.is_suspected_stale()). A prior design stamped a
        # "current_run_id" pointer here for inline runs, diverting subsequent
        # writes to that run's own runs/run_<id>.json file -- but nothing ever
        # cleared the stamp on completion, so session_main.json's own
        # file_stats/bash_invocations froze forever the first time an inline
        # run completed. That stamping was removed; only session_main.json's
        # own continuous history is used for inline runs going forward.
        if agent_id and detected_run_id:
            tracker.record_detected_run(detected_run_id)

        sys.exit(0)

    logger.info({
        'phase': phase,
        'command': command,
        'description': description,
        'data': relevant_bash_fields(hook_input),
    })
    sys.exit(0)


if __name__ == '__main__':
    main()
