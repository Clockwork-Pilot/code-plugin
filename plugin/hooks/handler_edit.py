#!/usr/bin/env python3
"""Handler for Edit tool execution.

Blocks edits to registered knowledge files. If a file is registered in
protected_files.txt, exits with code 2 to prevent direct modification.
Knowledge files should only be updated via patch_knowledge_document.

When a task is running, updates task stats with edited file and line ranges.
"""

import sys
import json
import os

from datetime import datetime


from src.hook_logging import setup_logger, relevant_edit_write_fields
from src.config import GUIDE_MESSAGE_UNVERIFIED_BLOCKING_CONSTRAINTS
from hooks import is_knowledge_file, is_edit_blocked_by_unverified_constraints, send_error
from src.ledger import tracker_for_hook
from src.hook_cli_calls import stamp_agent_live as _stamp_agent_live

logger = setup_logger("Edit")


def _get_line_range_for_edit(file_path: str, old_string: str, new_string: str) -> list | None:
    """Find the line range where the edit occurred."""
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Find where old_string was replaced
    old_lines = old_string.split("\n")
    start_line = None

    file_lines = content.split("\n")
    for i in range(len(file_lines) - len(old_lines) + 1):
        if file_lines[i:i+len(old_lines)] == old_lines:
            start_line = i + 1  # 1-based line numbers
            break

    if start_line is None:
        return None

    return [start_line, start_line + len(old_lines) - 1]


def main():
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    phase = hook_input.get('hook_event_name', '')
    file_path = hook_input.get('tool_input', {}).get('file_path')
    agent_id = hook_input.get('agent_id')
    tracker = tracker_for_hook(agent_id)

    logger.info({
        'phase': phase,
        'file_path': file_path,
        'data': relevant_edit_write_fields(hook_input),
    })

    # Check if editing is blocked due to unverified constraints
    if is_edit_blocked_by_unverified_constraints(file_path):
        error_msg = "Cannot edit: one or more specs in this project contain unverified constraints.\n\nUnverified constraints must be removed or fixed before modifications are allowed.\n\n" + GUIDE_MESSAGE_UNVERIFIED_BLOCKING_CONSTRAINTS
        send_error(error_msg, file_path)
        logger.error({
            'phase': phase,
            'status': 'blocked',
            'reason': 'Unverified constraints',
            'file_path': file_path,
            'data': relevant_edit_write_fields(hook_input),
        })
        sys.exit(2)

    # Check if the file being edited is a registered knowledge file
    # Hook input has file_path in tool_input (actual structure from hook data)
    if file_path and is_knowledge_file(file_path):
        error_msg = f"Cannot edit knowledge document: {file_path}\n\nKnowledge documents should only be modified using patch_knowledge_document.py to ensure proper validation and automatic markdown rendering."
        send_error(error_msg, file_path)
        logger.error({
            'phase': phase,
            'status': 'blocked',
            'reason': 'Knowledge file protected',
            'file_path': file_path,
            'data': relevant_edit_write_fields(hook_input),
        })
        sys.exit(2)

    # Record the change in PreToolUse only (old_string still in file → accurate
    # line range). Always is_creation=False: Edit always modifies, never creates.
    if file_path and phase == 'PreToolUse':
        old_string = hook_input.get('tool_input', {}).get('old_string')
        new_string = hook_input.get('tool_input', {}).get('new_string')
        line_range = _get_line_range_for_edit(file_path, old_string or "", new_string or "")
        tracker.record_file_change(file_path, line_range, is_creation=False)
        _stamp_agent_live(agent_id, tracker)

    # Re-stamp the agent liveness heartbeat in PostToolUse phase.
    # The PreToolUse stamp proved the agent started work. An edit that runs longer
    # than STALE_THRESHOLD stamps once at start then silent for the whole operation
    # -> STALE while alive. The Post-phase re-stamp bounds the heartbeat window at
    # command_duration + throttle instead of unbounded -- still proof the agent was
    # working.
    if agent_id and phase == 'PostToolUse':
        _stamp_agent_live(agent_id, tracker)

    logger.info({
        'phase': phase,
        'data': relevant_edit_write_fields(hook_input),
    })
    sys.exit(0)


if __name__ == '__main__':
    main()
