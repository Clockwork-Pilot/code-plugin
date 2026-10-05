#!/usr/bin/env python3
"""Hook CLI invoker - executes configured CLI commands from config.HOOK_CLI_CALLS.

Hooks invoke external CLI commands defined in config.HOOK_CLI_CALLS. The consuming
project provides these CLIs and makes them available via PATH or as absolute paths.

Design principles:

1. A hook NEVER breaks the tool call it observes. Every failure mode here -- missing
   run ID, CLI missing, non-zero exit, timeout, unparseable config -- degrades to "no
   call made" and returns False. A dropped call costs monitoring; a raising hook costs
   the user their command.
2. The throttle state stays in the hook's own ledger, not in the harness or graph.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


from src import binary_resolver
from src.config import (AGENT_LIVE_STAMP_INTERVAL_SECONDS, HOOK_CLI_CALLS,
                        HOOK_CLI_ENV, PLUGIN_ROOT, PROJECT_ROOT, hook_session_id)
from src.hook_logging import setup_logger

logger = setup_logger()

# How long a stamping call may take before the hook abandons it. Deliberately short: this
# runs in the critical path of the user's own tool call, and the fact being written is a
# heartbeat -- a late one is worthless, so waiting is strictly worse than skipping.
#
# The DEFAULT only. A binding in cw-plugin-cli-hooks.json declares its own `timeout` when
# that reasoning does not apply to it -- call_on_stop_hook is the case: abandoning a
# decision is not skipping a stale write, it is allowing a stop that was meant to be
# gated, and it happens silently (a timeout fails soft, like every non-blocking outcome).
CLI_CALL_TIMEOUT_SECONDS = 10


def plugin_version() -> str:
    """The installed plugin version (see binary_resolver.plugin_version). Public:
    handler_session_start.py's cwpilot compatibility gate reuses this exact read."""
    return binary_resolver.plugin_version()


def find_cwpilot() -> Optional[str]:
    """Where cwpilot actually is on this machine, or None if it cannot be found.

    Always binary_resolver.find_cwpilot(): the newest versioned install of this
    plugin's major.minor that install.sh placed under CWPILOT_VERSIONS_DIR. $PATH and
    $CWPILOT_BIN are deliberately never consulted -- a cwpilot found there was not
    placed and signed by the installer. Public so handler_session_start.py and the
    PreToolUse guard resolve exactly the same binary the {cwpilot_bin} placeholder does.
    """
    return binary_resolver.find_cwpilot()


# Lives in binary_resolver (which needs no project root) so the cwpilot-path handler can
# share it; re-exported for the guard and SessionStart.
path_cwpilot_matches = binary_resolver.path_cwpilot_matches


def resolve_cwpilot_bin() -> str:
    """The value {cwpilot_bin} in a configured command expands to.

    find_cwpilot(), or -- when it cannot be found -- an absolute, guaranteed-absent
    path under CWPILOT_VERSIONS_DIR. NEVER the bare name "cwpilot": passed to
    subprocess.run() unqualified (no "/"), that would let the OS's own exec search
    $PATH for a binary binary_resolver deliberately never consults, to keep this
    plugin from ever running. Any absolute, nonexistent path gets the exact same
    FileNotFoundError -> exit 127 "not installed" handling a bare-name miss always
    had, without smuggling a $PATH search back in through subprocess.run's own
    argv[0] resolution.
    """
    found = find_cwpilot()
    if found:
        return found
    versions_dir = os.environ.get("CWPILOT_VERSIONS_DIR") or str(
        Path.home() / ".local" / "share" / "cwpilot-versions"
    )
    return str(Path(versions_dir) / "cwpilot")


class _LazyCwpilotBin:
    """A {cwpilot_bin} value that resolves only if a configured command's argv or
    env actually references it.

    _hook_values() builds one dict of placeholder values per call, for EVERY
    configured event -- most of which (the subagent lifecycle/facts calls) never
    mention {cwpilot_bin} at all. resolving it scans the versions
    directory (see find_cwpilot()), and this runs in the critical path of the
    user's own tool call -- paying that cost on every hook regardless of whether
    THIS call even names cwpilot would be exactly the "hot path" mistake
    CLI_CALL_TIMEOUT_SECONDS's docstring warns about. str.format only calls
    __format__ on a value if its placeholder actually appears in the template, so
    a call that never writes {cwpilot_bin} never constructs the resolved string.
    """

    def __format__(self, spec: str) -> str:
        return format(resolve_cwpilot_bin(), spec)

    def __str__(self) -> str:
        return resolve_cwpilot_bin()


def _hook_values(run_id: Optional[str] = None, agent_id: Optional[str] = None,
                 extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Values available to command and environment placeholders at call time."""
    values: Dict[str, str] = {
        "plugin_root": str(PLUGIN_ROOT),
        "project_root": str(PROJECT_ROOT),
        "cwpilot_bin": _LazyCwpilotBin(),
        "plugin_version": plugin_version(),
    }
    values.update({k: v for k, v in (("run_id", run_id), ("agent_id", agent_id)) if v})
    try:
        from src.ledger import session_ledger
        tracker = session_ledger()
        tracker.load()
        values["last_file_change_at"] = tracker._cache.get("last_file_change_at") or ""
    except Exception:  # pylint: disable=broad-except
        values["last_file_change_at"] = ""
    if extra:
        values.update(extra)
    return values


def _child_env():
    """The environment every configured call runs with, or None to inherit unchanged.

    A MERGE over os.environ, never a replacement: the CLI still needs PATH/HOME and the
    CLAUDE_* variables the harness exported into this hook process. None (not a copy of
    os.environ) when the config declares nothing, so the no-env case stays exactly the
    plain inheritance it was before this existed.

    Read per call rather than cached at import: nothing here is hot, and a snapshot
    taken at import would miss anything a handler sets on os.environ before invoking.
    The config's call-time ``{plugin_version}`` placeholder is resolved here, after the
    load-time root placeholders have already been expanded by ``src.config``.
    """
    session = _session_env()
    if not HOOK_CLI_ENV and not session:
        return None
    values = _hook_values()
    configured = {}
    for name, value in (HOOK_CLI_ENV or {}).items():
        try:
            configured[name] = value.format(**values)
        except (KeyError, IndexError):
            configured[name] = value
    return {**os.environ, **configured, **session}


# The variable each host's CLI reads its session id from (cwpilot's PROVIDER_SESSION_ENV).
_SESSION_ID_ENV = {"claude-code": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_SESSION_ID"}


def _session_env() -> dict:
    """The session-id variable the CLI expects, when the host did not export it to this
    hook process.

    cwpilot keys its per-session store (the project root recorded at SessionStart, which
    a later direct CLI run reads back) by the id in that variable. Claude Code exports it
    to hooks; Codex puts the id only in the hook's JSON stdin, so the SessionStart call
    saw no session, recorded nothing, and every later cwpilot command in the session
    reported "no CWPILOT_PROJECT_ROOT". Fills it from the payload; never overrides a value
    the host did set.
    """
    name = _SESSION_ID_ENV.get(os.environ.get("CWPILOT_AGENT_PROVIDER", ""))
    session_id = hook_session_id()
    if not name or os.environ.get(name) or not session_id:
        return {}
    return {name: session_id}


def _entry(event: str):
    """The (argv, timeout) a configured event runs with, or (None, _) when unconfigured.

    One reader for all three call paths (invoke, invoke_and_get_payload,
    run_and_get_exit_code): they differ in what they do with the RESULT, never in how the
    command is resolved, and a per-call timeout added to two of the three would be a
    silent difference between otherwise identical wiring.
    """
    entry = HOOK_CLI_CALLS.get(event) or {}
    timeout = entry.get("timeout")
    return entry.get("cmd"), (timeout if timeout else CLI_CALL_TIMEOUT_SECONDS)

# Ledger field holding the last time this agent stamped agent_live, per run. Lives in the
# hook's ledger, never on the graph: it is throttle bookkeeping, not run state.
LAST_LIVE_STAMP_FIELD = "synced_at"

def _log_output(event: str, command: List[str], returncode: int,
                stdout: str, stderr: str) -> None:
    """Record every configured CLI result in the configured plugin log.

    No separate allowlist: config.HOOK_CLI_CALLS (the dynamic plugin entrypoint JSON,
    e.g. cw-plugin-entrypoint.json) is the one source of truth for which interface
    calls exist -- _run_one only reaches this for an event that's actually configured
    there, so gating on a second, hand-maintained Python-side list just risks drifting
    out of sync with it (as it did: the subagent-lifecycle calls -- dispatch/heartbeat/
    finish -- were silently excluded, so a real staleness bug left no trace in the log
    to confirm whether those calls ran, were skipped, or failed).

    Logs every completed invocation, including empty output, so an operator can
    distinguish a successful no-op from a hook that never reached its CLI.
    """
    logger.info({
        "hook_cli_call": event,
        "phase": "cwpilot_result",
        "command": command,
        "invocation": shlex.join(command),
        "exit_code": returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
    })


def _log_stop_hook_diagnostics(event: str, command: List[str], raw_exit_code: int,
                               raw_payload: str, transformed_payload: dict,
                               final_exit_code: int, failure: Optional[str] = None,
                               failure_reason: Optional[str] = None) -> None:
    """Log comprehensive diagnostics for Stop hook decision capture.

    Captures the complete decision and relay path needed to diagnose stop blocking:
    - provider: the CLI command configuration
    - raw_exit_code: the CLI's actual exit code
    - raw_payload: stdout before parsing
    - transformed_payload: parsed JSON payload
    - final_exit_code: the exit code being relayed to the harness
    - failure: type of failure (timeout, missing_command, malformed_json, unexecuted)
    - failure_reason: actionable reason for the failure
    """
    diagnostics = {
        "hook_cli_call": event,
        "phase": "stop_hook_decision",
        "provider": shlex.join(command) if command else "",
        "raw_exit_code": raw_exit_code,
        "raw_payload": raw_payload or "",
        "transformed_payload": transformed_payload,
        "final_exit_code": final_exit_code,
    }
    if failure:
        diagnostics["failure"] = failure
    if failure_reason:
        diagnostics["failure_reason"] = failure_reason
    logger.info(diagnostics)


def _resolve_command(argv: List[str]) -> Optional[List[str]]:
    """Resolve command to executable.

    Tries to find the command in PATH or as an absolute path.
    The consuming project is responsible for providing the CLIs.
    """
    if not argv:
        return None
    # A bare `cwpilot` would make the OS search $PATH for it -- the one lookup this
    # plugin never allows (binary_resolver). {cwpilot_bin} always expands to an absolute
    # path, so a bare name can only come from a literal written into the config; refuse
    # it rather than run whatever $PATH happens to hold.
    if argv[0] == "cwpilot":
        logger.warning("refusing bare `cwpilot` in a configured command; use {cwpilot_bin}")
        return None
    return argv


def _fill(argv: List[str], values: Dict[str, str]) -> Optional[List[str]]:
    """Substitute call-time placeholders, or None if any is unavailable.

    Returning None (rather than leaving a literal ``{run_id}`` in the command) is what
    makes an unresolvable run a no-op instead of a bogus call against a run named
    ``{run_id}``.
    """
    filled: List[str] = []
    for arg in argv:
        try:
            filled.append(arg.format(**values))
        except (KeyError, IndexError):
            logger.debug(f"hook cli call skipped: no value for placeholder in {arg!r}")
            return None
    return filled


def _seconds_since(iso_value: Optional[str]) -> Optional[float]:
    """Age of an ISO timestamp in seconds, or None if absent/unparseable.

    None means "no usable previous stamp", which callers treat as due -- a corrupt
    throttle value must not wedge liveness stamping off forever.
    """
    if not iso_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _claim_live_stamp(tracker, agent_id: str) -> bool:
    """Claim this heartbeat's slot: read the throttle AND write the new stamp, here,
    before the caller spawns anything. True when the slot was claimed.

    Throttling is the whole reason a per-tool-use hook can afford to stamp at all: without
    it this would spawn a CLI subprocess on every bash and every edit.

    Claiming is one step on purpose. The stamp used to be written only after the CLI
    subprocess returned, so two overlapping hooks both read a not-yet-updated timestamp,
    both judged themselves due, and both stamped -- observed live as two agent_live calls
    10.9s apart under a 30s interval. Writing the claim first narrows that window from
    "read + whole subprocess + write" down to the ledger read/write itself.

    The cost is that a claim is spent even when the call turns out to be a no-op (an
    unresolvable placeholder, a missing CLI). That is the safe direction: one skipped
    heartbeat is already tolerated by design -- test_config_hook_cli_calls asserts the
    stale threshold is at least three intervals wide.
    """
    if tracker is None or not agent_id:
        return True
    try:
        tracker.load()
        entry = (tracker.get_running_agents() or {}).get(agent_id) or {}
        age = _seconds_since(entry.get(LAST_LIVE_STAMP_FIELD))
    except Exception:  # pylint: disable=broad-except
        # The throttle state is unreadable (e.g. a concurrent conflict on the ledger).
        # Do NOT stamp: this used to fail OPEN, which turned every burst of ledger
        # contention -- exactly when overlapping hooks are most likely -- into a burst of
        # unthrottled stamps, adding write pressure to the very contention that caused it.
        return False
    if age is not None and age < AGENT_LIVE_STAMP_INTERVAL_SECONDS:
        return False
    _record_live_stamp(tracker, agent_id)
    return True


def _record_live_stamp(tracker, agent_id: str) -> None:
    """Remember when we last stamped, so the next call can throttle against it."""
    if tracker is None or not agent_id:
        return
    try:
        tracker.update_timestamp(
            agent_id, LAST_LIVE_STAMP_FIELD, datetime.now(timezone.utc).isoformat(timespec="microseconds")
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug(f"could not record live-stamp time for {agent_id}: {exc}")


def invoke(event: str, *, run_id: Optional[str] = None, agent_id: Optional[str] = None,
           tracker=None, throttled: bool = False,
           extra: Optional[Dict[str, str]] = None) -> bool:
    """Run the CLI call configured for ``event``. Returns True only if it actually ran.

    Args:
        event: a key of config.HOOK_CLI_CALLS.
        run_id: the run this hook resolved (detected_run_id / playbook_run_id).
            A call needing {run_id} without one is skipped -- there is nothing to stamp.
        agent_id: the sub-agent this hook is observing, when there is one.
        tracker: the hook's ledger, used only for throttle bookkeeping.
        throttled: gate this call behind AGENT_LIVE_STAMP_INTERVAL_SECONDS (the bash/edit
            heartbeat). Dispatch and finish are one-shot events and are never throttled,
            but dispatch still stamps to throttle subsequent bash calls.
        extra: additional {placeholder: value} substitutions beyond run_id/agent_id (e.g.
            a finish call's own agent_finished_at/files_changed), merged into the same
            _fill() pass. A caller supplying this is responsible for defaulting every
            key to a real string ("" when there is nothing to say) -- same as run_id/
            agent_id, a missing key makes the WHOLE call a no-op rather than partially
            filled.
    """
    if not (HOOK_CLI_CALLS.get(event) or {}).get("cmd"):
        return False

    # Claim BEFORE running: the claim is the throttle, and it has to be written while
    # no subprocess is in flight or an overlapping hook reads a stale timestamp and
    # stamps too (see _claim_live_stamp).
    if throttled and not _claim_live_stamp(tracker, agent_id):
        return False

    execution = invoke_cli_call(event, run_id=run_id, agent_id=agent_id, extra=extra)
    ran = execution.completed and execution.result.returncode == 0

    if ran and not throttled and event == "subagent_call_on_run_id_resolved":
        # Dispatch is one-shot and never throttled, but it still seeds the timestamp so
        # the sub-agent's first bash call throttles against its dispatch.
        _record_live_stamp(tracker, agent_id)
    return ran


class _PayloadResult(dict):
    """A payload mapping that also carries the command's exit status internally."""

    def __init__(self, payload: dict, exit_code: int = 0):
        super().__init__(payload)
        self.exit_code = exit_code


@dataclass
class _CliExecution:
    """Raw outcome from the one shared configured-CLI executor."""

    result: Optional[subprocess.CompletedProcess] = None
    issue: Optional[str] = None
    issue_detail: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.result is not None


def invoke_cli_call(event: str, *, run_id: Optional[str] = None,
                    agent_id: Optional[str] = None,
                    extra: Optional[Dict[str, str]] = None) -> _CliExecution:
    """Invoke one named hook-CLI interface call and return its raw result.

    ``HOOK_CLI_CALLS`` is an installed binding for the interface: production loads
    cw-plugin-cli-hooks.json, while tests bind the same calls to cli_stubs.
    """
    argv, timeout = _entry(event)
    if not argv:
        return _CliExecution(issue="unconfigured", issue_detail="event not configured in HOOK_CLI_CALLS")
    filled = _fill(argv, _hook_values(run_id, agent_id, extra))
    if filled is None:
        return _CliExecution(issue="unresolved_placeholder", issue_detail="unable to resolve placeholder in command")
    command = _resolve_command(filled)
    if command is None:
        return _CliExecution(issue="unavailable", issue_detail="command not available")
    failure = None
    failure_details = {}
    issue_detail = None
    try:
        result = subprocess.run(  # noqa: S603 - argv list, never shell=True
            command, capture_output=True, text=True, timeout=timeout, check=False,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        failure = 'timeout'
        issue_detail = f"command did not complete within {timeout} seconds"
        failure_details = {
            'timeout': timeout,
            'stdout': exc.stdout or '',
            'stderr': exc.stderr or '',
        }
    except FileNotFoundError as exc:
        failure = 'not_found'
        issue_detail = f"command not found in PATH: {str(exc)}"
        failure_details = {'error': str(exc), 'exit_code': 127}
    except PermissionError as exc:
        failure = 'not_executable'
        issue_detail = f"command is not executable: {str(exc)}"
        failure_details = {'error': str(exc), 'exit_code': 126}
    except (OSError, subprocess.SubprocessError) as exc:
        failure = 'unavailable'
        issue_detail = f"failed to execute command: {str(exc)}"
        failure_details = {'error': str(exc)}
    if failure:
        logger.info({
            'hook_cli_call': event,
            'phase': 'cwpilot_result',
            'command': command,
            'invocation': shlex.join(command),
            'exit_code': failure_details.pop('exit_code', None),
            'stdout': failure_details.pop('stdout', ''),
            'stderr': failure_details.pop('stderr', ''),
            'issue': failure,
            'issue_detail': issue_detail,
            **failure_details,
        })
        return _CliExecution(issue=failure, issue_detail=issue_detail)
    _log_output(event, command, result.returncode, result.stdout, result.stderr)
    return _CliExecution(result=result)


def _parse_json_payload(stdout: str) -> dict:
    """Best-effort parse of a stub's stdout as a JSON object.

    Fail-soft, not fail-loud: a stub that prints a bare log line (the shipped default)
    or malformed JSON must degrade to "no payload" rather than break the caller. Only a
    JSON *object* is accepted -- a bare string/number/array has no fields to look up.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def invoke_and_get_payload(event: str, *, run_id: Optional[str] = None,
                           agent_id: Optional[str] = None,
                           extra: Optional[Dict[str, str]] = None) -> dict:
    """Run the CLI configured for ``event`` and return its stdout parsed as a JSON object.

    Unlike ``invoke()`` (flattens to True/False for side-effect stamps) or
    ``run_and_get_exit_code()`` (the exit code IS the decision, e.g. Stop's block/allow),
    this is for events where the DATA a stub prints feeds into what the calling hook
    does next -- e.g. call_on_session_start returning extra additionalContext/
    systemMessage text to splice into the SessionStart payload.

    An event configured with a `policy` literal instead of a `cmd` is answered from the
    config itself, verbatim and without a subprocess. Same return contract either way --
    the caller cannot tell which kind of entry served it, which is the point: where the
    payload comes from is the binding's business, not the hook's.

    Returns {} when unconfigured, the configured command fails to run, or its stdout
    isn't a JSON object -- callers must treat {} as "nothing to add", the same fail-soft
    contract every other path in this module follows. For a configured command, the
    returned dict also carries its subprocess status as the non-serialized ``exit_code``
    attribute, allowing SessionStart to distinguish a missing command (127) from a
    found-but-non-executable command (126) without changing the mapping contract.
    """
    literal = (HOOK_CLI_CALLS.get(event) or {}).get("policy")
    if isinstance(literal, dict):
        return literal

    execution = invoke_cli_call(event, run_id=run_id, agent_id=agent_id, extra=extra)
    if execution.issue == "not_found":
        return _PayloadResult({}, 127)
    if execution.issue == "not_executable":
        return _PayloadResult({}, 126)
    if not execution.completed:
        return {}
    if execution.result.returncode != 0:
        return _PayloadResult({}, execution.result.returncode)
    return _PayloadResult(_parse_json_payload(execution.result.stdout.strip()), 0)


def run_and_get_exit_code(event: str, *, run_id: Optional[str] = None,
                          agent_id: Optional[str] = None) -> int:
    """Run the CLI configured for ``event``, relay its stdout/stderr verbatim to this
    process's own, and return its ACTUAL exit code.

    Unlike ``invoke()`` -- which flattens every outcome to True/False and is meant for
    side-effect stamps a hook fires alongside its own decision (a dropped heartbeat
    costs monitoring, not correctness) -- this is for the hooks whose invoked CLI IS
    the decision: e.g. the Stop hook's ``call_on_stop_hook``, where the harness reads
    the hook's own stdout/exit code directly as what the configured CLI decided.
    Swallowing that into a generic 0/1 would silently turn a real block into a plain
    error, or a real error into a silent pass -- and dropping stdout (only exit code
    used to be forwarded) would silently throw away a decision payload like
    ``{"decision": "block", "reason": ...}`` / ``{"decision": "approve",
    "systemMessage": ...}`` that Claude Code's own hook JSON protocol expects on this
    process's stdout. The relay is verbatim and un-parsed on purpose: whatever extra
    fields a project's CLI adds (e.g. its own ``vars``) reach the harness untouched.

    Returns 0 when nothing is configured, or the configured command failed to even
    run (missing binary, timeout, unfillable placeholder) -- there is no CLI opinion to
    report, so "no opinion" degrades to success rather than blocking on infrastructure
    trouble. A command that actually executed reports its real exit code.

    Logs comprehensive diagnostics for every Stop event decision to enable diagnosis
    of stop blocking issues, capturing provider, raw/transformed payloads, and failures.
    """
    execution = invoke_cli_call(event, run_id=run_id, agent_id=agent_id)
    argv, _ = _entry(event)
    command = _resolve_command(argv) if argv else []
    raw_payload = ""
    transformed_payload = {}

    if not execution.completed:
        # Log failure diagnostic
        failure_reason = execution.issue_detail or f"failed to execute: {execution.issue}"
        _log_stop_hook_diagnostics(
            event, command, 0, "", {}, 0,
            failure=execution.issue, failure_reason=failure_reason
        )
        return 0

    # Capture raw payload before relaying
    raw_payload = execution.result.stdout or ""

    # Attempt to parse payload for diagnostics
    has_parse_error = False
    try:
        parsed = json.loads(raw_payload) if raw_payload.strip() else {}
        transformed_payload = parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        # Malformed JSON is logged but not fatal
        transformed_payload = {}
        has_parse_error = True

    # Log diagnostics
    if has_parse_error and raw_payload.strip():
        # Log malformed JSON as a failure
        _log_stop_hook_diagnostics(
            event, command, execution.result.returncode, raw_payload, {},
            execution.result.returncode, failure="malformed_json",
            failure_reason="stdout is not valid JSON"
        )
    else:
        # Log successful decision with both raw and transformed payloads
        _log_stop_hook_diagnostics(
            event, command, execution.result.returncode, raw_payload,
            transformed_payload, execution.result.returncode
        )

    # Relay output verbatim to harness
    if raw_payload:
        sys.stdout.write(raw_payload)
    if execution.result.stderr:
        sys.stderr.write(execution.result.stderr)
    return execution.result.returncode


def run_and_get_output(event: str, *, run_id: Optional[str] = None,
                       agent_id: Optional[str] = None) -> "tuple[str, int]":
    """Like ``run_and_get_exit_code`` but RETURNS ``(stdout, exit_code)`` instead of
    relaying stdout to this process and returning the code alone.

    For a hook that must INSPECT the configured CLI's decision payload before deciding
    what to emit -- the Stop hook's keep-warm poll reads ``keepCacheWarm`` out of the
    payload, then either relays it verbatim (no field) or blocks polling the ledger
    before re-emitting it. stderr is still relayed here (diagnostics, never the
    decision). ``("", 0)`` on nothing-configured / failed-to-run, same "no opinion
    degrades to success" contract.
    """
    execution = invoke_cli_call(event, run_id=run_id, agent_id=agent_id)
    if not execution.completed:
        return "", 0
    if execution.result.stderr:
        sys.stderr.write(execution.result.stderr)
    return execution.result.stdout or "", execution.result.returncode


def invoke_throttled_sync(event: str, run_id: str, agent_id: str) -> bool:
    """Common entry point for all throttled sync calls (bash, edit, write).

    Handles session_ledger access and throttle tracking in one place.
    All sync heartbeats use this to respect the throttle interval.
    """
    from src.ledger import session_ledger  # pylint: disable=import-outside-toplevel
    session_tracker = session_ledger()
    return invoke(event, run_id=run_id, agent_id=agent_id,
                  tracker=session_tracker, throttled=True)


def stamp_agent_live(agent_id: Optional[str], tracker) -> bool:
    """Heartbeat helper for the file-mutating hooks (Edit, Write).

    Unlike the Bash hook, these have no command to parse a run out of, so the run comes
    solely from what this agent's ledger already detected. No agent, or no detected run,
    means no call -- the main-window session has no sub-agent liveness to report.

    Shared by handler_edit and handler_write rather than copied into each: they ask the
    identical question, and letting the two drift is how a resolved-run detection
    ends up with two disagreeing implementations.
    """
    if not agent_id:
        return False
    try:
        tracker.load()
        run_id = tracker.get_detected_playbook_run_id()
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug(f"could not resolve run for {agent_id}: {exc}")
        return False
    if not run_id:
        return False
    return invoke_throttled_sync("subagent_sync_call_on_edit_write", run_id, agent_id)
