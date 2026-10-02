#!/usr/bin/env python3
"""PostToolUse handler for Edit/Write — logs the main agent's file change.

Runs AFTER the edit applies (so content greps see the new content).
"""

import sys
import json



from src.hook_logging import setup_logger, relevant_edit_write_fields

logger = setup_logger("PostChange")


def main():
    data = sys.stdin.read()
    hook_input = json.loads(data) if data else {}

    logger.info({
        'tool_name': hook_input.get('tool_name', 'PostChange'),
        'phase': hook_input.get('hook_event_name', ''),
        'data': relevant_edit_write_fields(hook_input),
    })

    sys.exit(0)


if __name__ == "__main__":
    main()
