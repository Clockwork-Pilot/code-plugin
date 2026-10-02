"""Small logging helpers used by hook subprocesses."""

import json
import logging
import os


# Hooks are separate subprocesses, so one process-wide value is enough. setup_logger
# assigns it once for the hook that owns the process; every record uses that same value.
CURRENT_EVENT = "unknown"
_LOGGER = logging.getLogger("cwpilot")
_REDACTED_SESSION_VALUE = "<reducted>"
# Only actual session identifiers -- not transcript_path/cwd/permission_mode, which
# are useful, non-sensitive log context (see test_hook_logging_contract.py).
_REDACTED_HOOK_FIELDS = frozenset({"session_id", "sessionid"})
_LOGGED_ENV_NAME_FRAGMENTS = ("PROJECT", "PLUGIN", "SESSION",)
# Deliberately narrower than _LOGGED_ENV_NAME_FRAGMENTS: PROJECT-/PLUGIN-named vars
# are logged AND kept visible (that's the point of logging them); only SESSION-named
# ones hide their value. Redacting everything that gets logged would make logging it
# pointless.
_REDACTED_ENV_NAME_FRAGMENTS = ("SESSION",)


class _EventFormatter(logging.Formatter):
    """Keep the old timestamped format while adding the current event field."""

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict):
            message = dict(record.msg)
        else:
            message = {"message": record.getMessage()}
        message["event"] = CURRENT_EVENT
        if record.exc_info:
            message.setdefault("exception", self.formatException(record.exc_info))
        rendered = json.dumps(message, ensure_ascii=False, default=str)
        return (
            f"{self.formatTime(record)} - {record.name} - "
            f"{record.levelname} - {rendered}"
        )


def setup_logger(event: str = None, use_json: bool = True,
                 log_envs: bool = False, reset_log: bool = False):
    """Set up a configured logger instance for hooks.

    Args:
        event: Event label for this hook. Only an explicit value changes CURRENT_EVENT.
        use_json: Whether to use JSON formatting (not used in stub)
        log_envs: Emit environment variables whose names contain one of
            _LOGGED_ENV_NAME_FRAGMENTS. Values whose names contain one of
            _REDACTED_ENV_NAME_FRAGMENTS are redacted.
        reset_log: Start a fresh log before configuring this logger.

    Returns:
        Configured logger instance
    """
    global CURRENT_EVENT
    if event is not None:
        CURRENT_EVENT = event

    logger = _LOGGER
    if reset_log:
        _reset_hooks_log()
    if not logger.handlers:
        from src.config import HOOKS_LOG_FILE, HOOKS_LOG_LEVEL
        try:
            # Create parent directory if it doesn't exist
            HOOKS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(HOOKS_LOG_FILE)
        except (OSError, AttributeError):
            # Fall back to stderr if file creation fails
            handler = logging.StreamHandler()
        handler.setFormatter(
            _EventFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, HOOKS_LOG_LEVEL, logging.INFO))
    if log_envs:
        environment = {
            name: (
                _REDACTED_SESSION_VALUE
                if any(fragment in name.upper()
                       for fragment in _REDACTED_ENV_NAME_FRAGMENTS)
                else value
            )
            for name, value in os.environ.items()
            if any(fragment in name.upper()
                   for fragment in _LOGGED_ENV_NAME_FRAGMENTS)
        }
        logger.info({"environment": environment})
    return logger


def setup_task_logger(use_json: bool = True):
    """Set up a logger that writes to the configured plugin log."""
    return setup_logger(use_json=use_json)


def _reset_hooks_log() -> None:
    """Start a fresh hook log, closing inherited handlers before truncating it."""
    from src.config import HOOKS_LOG_FILE

    for handler in list(_LOGGER.handlers):
        handler.flush()
        handler.close()
        _LOGGER.removeHandler(handler)
    HOOKS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_LOG_FILE.write_text("", encoding="utf-8")


def log_to_both_logs(message_dict: dict, use_json: bool = True) -> None:
    """Log an event to the configured plugin log."""
    logger = setup_logger(use_json=use_json)
    logger.info(json.dumps(message_dict) if use_json else str(message_dict))


def redact_session_fields(value):
    """Return a log-safe copy with explicitly listed hook fields redacted."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED_SESSION_VALUE
                  if str(key).casefold() in _REDACTED_HOOK_FIELDS
                  else redact_session_fields(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_session_fields(item) for item in value]
    return value


def relevant_edit_write_fields(hook_input: dict) -> dict:
    """Extract the useful subset of an Edit/Write hook payload for logging.

    The raw payload carries a lot of low-signal or duplicate data (cwd, permission_mode,
    transcript_path, and — worst — the full file/diff content in tool_input.old_string/
    new_string/content and tool_response.originalFile/structuredPatch, which can be many KB
    per call). Keep only what's actually useful for ad-hoc pattern analysis: which agent
    made the change, what file, how long it took, and whether the user
    hand-modified the result afterward. event/phase are already recorded as separate log
    fields by the caller, so hook_event_name/tool_name are dropped here to avoid duplication.

    Args:
        hook_input: The raw parsed hook stdin payload.

    Returns:
        A compact dict with only non-None useful fields.
    """
    tool_input = hook_input.get('tool_input') or {}
    tool_response = hook_input.get('tool_response') or {}
    fields = {
        'prompt_id': hook_input.get('prompt_id'),
        'agent_id': hook_input.get('agent_id'),
        'agent_type': hook_input.get('agent_type'),
        'tool_use_id': hook_input.get('tool_use_id'),
        'file_path': tool_input.get('file_path'),
        'duration_ms': hook_input.get('duration_ms'),
    }
    if isinstance(tool_response, dict) and 'userModified' in tool_response:
        fields['user_modified'] = tool_response['userModified']
    return {k: v for k, v in fields.items() if v is not None}


def relevant_bash_fields(hook_input: dict) -> dict:
    """Extract the useful subset of a Bash hook payload for logging.

    Uses the same correlation fields as relevant_edit_write_fields (prompt_id, agent_id,
    agent_type, tool_use_id, duration_ms) so Bash log lines can be correlated with
    Edit/Write logs. No file_path/user_modified (not applicable to Bash).

    Args:
        hook_input: The raw parsed hook stdin payload.

    Returns:
        A compact dict with only non-None useful fields.
    """
    fields = {
        'prompt_id': hook_input.get('prompt_id'),
        'agent_id': hook_input.get('agent_id'),
        'agent_type': hook_input.get('agent_type'),
        'tool_use_id': hook_input.get('tool_use_id'),
        'duration_ms': hook_input.get('duration_ms'),
    }
    return {k: v for k, v in fields.items() if v is not None}


def prepare_log_entry(base_entry: dict) -> dict:
    """Return a shallow copy of a log entry for callers that mutate it."""
    return base_entry.copy()

__all__ = [
    "CURRENT_EVENT",
    "setup_logger",
    "setup_task_logger",
    "log_to_both_logs",
    "redact_session_fields",
    "prepare_log_entry",
    "relevant_edit_write_fields",
    "relevant_bash_fields",
]
