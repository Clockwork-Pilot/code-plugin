#!/usr/bin/env python3
"""Handler for Write tool execution.

Blocks writes to registered knowledge files. If a file is registered in
protected_files.txt, exits with code 2 to prevent direct modification.
Knowledge files should only be updated via patch_knowledge_document.

When a task is running, updates task stats with written file.
"""

import sys
import json
import os

from datetime import datetime


from src.hook_logging import setup_logger, relevant_edit_write_fields
from hooks import is_knowledge_file, is_edit_blocked_by_unverified_constraints, send_error
from src.ledger import tracker_for_hook
from src.config import GUIDE_MESSAGE_UNVERIFIED_BLOCKING_CONSTRAINTS
from src.hook_cli_calls import stamp_agent_live as _stamp_agent_live

logger = setup_logger("Write")


def main():
    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    # Check if writing is blocked due to unverified constraints
    file_path = hook_input.get('tool_input', {}).get('file_path')
    agent_id = hook_input.get('agent_id')
    tracker = tracker_for_hook(agent_id)

    if is_edit_blocked_by_unverified_constraints(file_path):
        error_msg = "Cannot write: one or more specs in this project contain unverified constraints.\n\nUnverified constraints must be removed or fixed before modifications are allowed."
        send_error(error_msg, file_path)
        logger.error(f"Write blocked due to unverified constraints: {file_path}")
        sys.exit(2)

    # Check if the file being written is a registered knowledge file
    # Hook input has file_path in tool_input (actual structure from hook data)
    if file_path and is_knowledge_file(file_path):
        error_msg = f"Cannot write to knowledge document: {file_path}\n\nKnowledge documents should only be modified using patch_knowledge_document.py to ensure proper validation and automatic markdown rendering."
        send_error(error_msg, file_path)
        logger.error(f"Attempted to write to registered knowledge file: {file_path}")
        sys.exit(2)

    # Record the change in PreToolUse only (file existence check must run before
    # the write determines created-vs-modified).
    phase = hook_input.get('hook_event_name', '')
    if file_path and phase == 'PreToolUse':
        tracker.record_file_change(file_path)
        _stamp_agent_live(agent_id, tracker)

    logger.info({
        'phase': hook_input.get('hook_event_name', ''),
        'data': relevant_edit_write_fields(hook_input),
    })
    sys.exit(0)


if __name__ == '__main__':
    main()
