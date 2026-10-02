#!/usr/bin/env python3
"""Handler for SubagentStart event."""

import sys
import json


from datetime import datetime


from src.hook_logging import redact_session_fields, setup_logger
from src.ledger import sweep_stale_ledger_files, session_ledger, agent_ledger

logger = setup_logger("SubagentStart")


def _sweep_stale_ledgers() -> None:
    """GC backstop: delete .changes-tracker/ ledger files older than 6 hours.

    Called on every SubagentStart, BEFORE record_agent_start() (below, in main())
    creates/touches this sub-agent's own ledger file -- ordering it first means the
    plain mtime-age check is sufficient to never sweep the file about to be
    created. Catches ledgers left behind by a run that never signaled completion
    cleanly (crashed, was abandoned, or never fired SubagentStop). mtime is the
    only signal -- no cross-referencing against running_agents -- so a file
    younger than 6 hours survives even for an agent_id no longer active anywhere.

    Known open question (not solved here, flagged as a follow-up): this trigger
    alone never fires in a long session with no further sub-agent spawns, so a
    stale file created near the end of such a session has nothing left to sweep it.
    """
    removed = sweep_stale_ledger_files()
    if removed:
        logger.info(f"Swept {len(removed)} stale ledger file(s): {removed}")


def main():
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    _sweep_stale_ledgers()

    # Record agent_id -> running_agents when a sub-agent starts. (The run_id this once
    # tried to resolve from the orchestrator's transcript was never reliably recoverable
    # that way -- see the retired transcript-scan mechanism -- so no run_id is passed
    # here; running_agents[agent_id] carries just the start timestamp.)
    agent_id = hook_input.get('agent_id')
    # The SubagentStart payload names the model the sub-agent is running on
    # (e.g. "claude-haiku-4-5"); record it so the spawn model is observable rather
    # than only recoverable from a transcript.
    model = hook_input.get('model')
    if agent_id:
        session_ledger().record_agent_start(agent_id, model=model)

        # Tag the NEW sub-agent's OWN ledger file with its agent_id -- a separate write,
        # to a separate file, from the main-window record_agent_start() above.
        # agent_ledger(agent_id) routes to that sub-agent's own file.
        agent_ledger(agent_id).record_own_agent_id(agent_id, model=model)

    logger.info({
        'hook_input': redact_session_fields(hook_input)
    })
    sys.exit(0)


if __name__ == '__main__':
    main()
