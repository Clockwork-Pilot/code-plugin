#!/usr/bin/env python3
"""Handler for SubagentStop event.

This hook writes NO node evidence: it is ledger-side work only -- stamp the agent's
stop and sweep stale ledger files. Reconciling a finished agent against any external
engine's own run state (if the consuming project has one) is that project's concern,
reached via config.HOOK_CLI_CALLS' "subagent_call_on_run_finished" CLI stamp
below, never by this plugin importing or patching that engine's documents directly.
"""

import json
import sys


from src.hook_logging import redact_session_fields, setup_logger
from src.ledger import sweep_stale_ledger_files, agent_ledger, session_ledger
from src.hook_cli_calls import invoke as invoke_hook_cli

logger = setup_logger("SubagentStop")


def _resolve_agent_tracker(agent_id):
    """Resolve the ledger this stop event's ``agent_id`` belongs to.

    Normally SubagentStart and SubagentStop carry the SAME agent_id, so the direct
    agent_ledger(agent_id) lookup below finds the file SubagentStart (or any
    Bash/Edit heartbeat in between) already wrote for it. But that lookup is a
    literal filename match (see agent_ledger_path) -- if the harness ever hands
    SubagentStop an agent_id that doesn't match any ledger filename (observed at
    least once in the wild), the direct lookup silently comes back empty and
    agent_finished/files_changed never post; completion then has to be INFERRED
    from silence instead of recorded as a fact.

    When the direct lookup finds nothing, this returns (None, None) and logs why.
    It deliberately does NOT guess at another agent's ledger.

    Returns (tracker_or_None, resolved_agent_id_or_None). The resolved id is what
    every subsequent write against this tracker must use (record_agent_finished
    checks it against the tracker's own self.agent_id), which is why it is
    returned alongside the tracker instead of the caller continuing to use the
    original, possibly-mismatched agent_id.
    """
    direct = agent_ledger(agent_id) if agent_id else None
    if direct is not None and direct.ledger_exists():
        return direct, agent_id

    if not agent_id:
        return None, None

    # NO FALLBACK to "whichever other agent is still open". That guess attributes one
    # agent's stop to a DIFFERENT agent's run: observed live as agent_finished landing on
    # a run 7 seconds after it started, while its agent went on working for another 90.
    # The original guard here refused to fabricate a ledger for an untracked agent_id;
    # borrowing a live agent's ledger is the same error with a worse blast radius, since
    # it marks a working run finished.
    #
    # Unresolvable means unresolvable: log it and post nothing. A missing fact is
    # recoverable; a false "finished" on a live run is not.
    logger.warning({
        'issue': 'agent_id_had_no_ledger',
        'stop_agent_id': agent_id,
    })
    return None, None


def _files_changed(agent_tracker) -> str:
    """Comma-joined, sorted list of every path this agent's own ledger recorded as
    created or modified -- the ledger's own file_stats.files_modified/files_created,
    not a fact the sub-agent has to remember to self-report (which it routinely
    skips)."""
    file_stats = agent_tracker.get_file_stats()
    paths = set(file_stats.get('files_created', {}) or {})
    paths.update(file_stats.get('files_modified', {}) or {})
    return ",".join(sorted(paths))


def main():
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    stop_agent_id = hook_input.get('agent_id')
    # Don't collapse these into one tracker: agent_tracker's own-ledger stop-stamp
    # and session_tracker's running_agents[agent_id].finished_at (read by the
    # cap-gate) are two different files with two different readers.
    agent_tracker, agent_id = _resolve_agent_tracker(stop_agent_id)
    session_tracker = session_ledger() if stop_agent_id else None

    # GC backstop: sweep ledger files older than the retention window (see
    # sweep_stale_ledger_files()), purely by mtime -- no running_agents liveness
    # check. Previously this only ran on SubagentStart, so a session with no
    # further sub-agent spawns after the last one never swept again; running it
    # here too closes that gap with a second trigger point.
    sweep_stale_ledger_files()

    # Mark agent_id's running_agents entry finished (does NOT remove it --
    # removal happens via the single time-based eviction check below, which
    # covers both this clean-stop case and an ungraceful death alike).
    finished_run_id = None
    if agent_tracker:
        agent_tracker.record_agent_finished(agent_id)
        # Resolve the run_id with fallback priority:
        # 1. Extract from subagent's last_assistant_message (most reliable context)
        # 2. Fall back to detected_run_id from agent ledger (if play-run was run)
        try:
            agent_tracker.load()
            finished_run_id = agent_tracker.get_detected_playbook_run_id()
        except Exception:  # pylint: disable=broad-except
            finished_run_id = None

    if session_tracker:
        session_tracker.record_agent_finished(agent_id)

    # The actual reconciliation hand-off: tell the consuming project's own engine
    # (via the configured CLI stub) that agent_id finished, so it can resolve
    # finished_run_id's run state. finished_at/files_changed ride along as `extra`
    # placeholders (unused by the default template, but available to a consuming
    # project's own hook_cli_calls.json) so completion is a recorded fact -- and
    # files_changed is read straight from THIS agent's own ledger file_stats --
    # rather than left to the sub-agent self-reporting a fact it routinely skips.
    if agent_id and finished_run_id:
        invoke_hook_cli(
            "subagent_call_on_run_finished",
            run_id=finished_run_id,
            agent_id=agent_id,
            tracker=session_tracker,
            extra={
                'agent_finished_at': (agent_tracker.load().get('subagent_finished_at') or '')
                                      if agent_tracker else '',
                'files_changed': _files_changed(agent_tracker) if agent_tracker else '',
            },
        )
    elif agent_id and not finished_run_id:
        # invoke_hook_cli would skip this silently (a call needing {run_id} without
        # one is a no-op by design) -- that silence is exactly why a resolvable
        # agent whose ledger never detected a run_id went unnoticed. Log it instead
        # of dropping it: this is diagnosable (no play-run/play-advance call was ever
        # observed by this agent), not a plugin bug on its own.
        logger.info({
            'issue': 'finish_call_skipped_no_run_id',
            'agent_id': agent_id,
        })

    # Hard-eviction check for running_agents (RUNNING_AGENT_GRACE_SECONDS since the
    # most recent of started_at/updated_at/finished_at) -- run on SubagentStop too,
    # alongside every main-window ledger write, so completed/orphaned entries don't
    # linger past their grace period.
    if session_tracker:
        session_tracker.evict_expired_running_agents()

    logger.info({
        'hook_input': redact_session_fields(hook_input)
    })
    sys.exit(0)


if __name__ == '__main__':
    main()
