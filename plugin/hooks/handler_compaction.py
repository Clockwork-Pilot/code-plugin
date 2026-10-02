#!/usr/bin/env python3
"""Handler for provider compaction events.

Compaction bookkeeping belongs to the consuming cwpilot CLI.  This handler is a
fail-soft bridge, just like the other plugin hook handlers.
"""

import json
import sys

from src.hook_cli_calls import invoke
from src.hook_logging import redact_session_fields, setup_logger

logger = setup_logger("Compaction")


class CompactionHandler:
    """Dispatch the configured compaction callback without affecting the host."""

    def handle(self, hook_input=None) -> int:
        logger.info({"hook_input": redact_session_fields(hook_input or {})})
        try:
            invoke("call_on_compaction")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("compaction callback failed: %s", exc)
        return 0


def main() -> int:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        hook_input = {}
    return CompactionHandler().handle(hook_input)


if __name__ == "__main__":
    sys.exit(main())
