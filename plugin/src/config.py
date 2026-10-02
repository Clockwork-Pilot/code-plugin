"""Plugin configuration - hooks and ledger system."""

import os
from collections.abc import Mapping
from pathlib import Path

from . import (
    _expand_roots,
    _load_hook_cli_calls,
    _load_hook_cli_env,
    resolve_from_environment,
)


_HOOK_INPUT: dict[str, object] = {}


def set_hook_input(hook_input: Mapping[str, object] | None) -> None:
    """Register the current hook payload for resolver fallbacks.

    The hook host supplies the working directory in JSON stdin, while the normal
    environment-root variables remain authoritative. The dispatcher calls this before
    importing a handler so module-level configuration consumers see the same context
    as the handler itself.
    """
    global _HOOK_INPUT
    _HOOK_INPUT = dict(hook_input) if isinstance(hook_input, Mapping) else {}


def hook_session_id() -> str | None:
    """The host's session id from the hook payload, or None.

    Every host puts ``session_id`` in the hook's JSON stdin; only some also export it to
    the hook subprocess's environment (Claude Code does, Codex does not).
    """
    value = _HOOK_INPUT.get("session_id")
    return value.strip() or None if isinstance(value, str) else None


def _hook_cwd() -> Path | None:
    value = _HOOK_INPUT.get("cwd")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return Path(value) if value else None


def plugin_root() -> Path:
    """Resolve the plugin root when a caller needs it."""
    return resolve_from_environment(
        ["CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"],
        fallback=Path(__file__).resolve().parent.parent,
        description="plugin root",
    )


def project_root() -> Path:
    """Resolve the project root, using hook ``cwd`` only as the final fallback."""
    return resolve_from_environment(
        ["CWPILOT_PROJECT_ROOT", "CLAUDE_PROJECT_DIR", "PROJECT_ROOT"],
        fallback=_hook_cwd(),
        description="project root",
    )


def plugin_data() -> Path:
    """Resolve the plugin data directory when a caller needs it."""
    return resolve_from_environment(
        ["CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"],
        description="plugin data directory",
    )


def hooks_log_file() -> Path:
    """Resolve the plugin log path without requiring a project root."""
    return Path(
        os.getenv("CWPILOT_PLUGIN_LOG_FILE")
        or str(plugin_data() / "cwpilot-plugin-hooks.log")
    )


def hooks_log_level() -> str:
    """Return the plugin hook log level."""
    return "INFO"


def changes_tracker_dir() -> Path:
    """Resolve the project-owned changes tracker directory."""
    return Path(
        os.getenv("CHANGES_TRACKER_DIR", str(project_root() / ".changes-tracker"))
    )


def hook_cli_calls_file() -> Path:
    """Resolve the fixed hook-to-CLI wiring file in the plugin checkout."""
    return plugin_root() / "cw-plugin-cli-hooks.json"


def hook_cli_calls() -> dict:
    """Load hook-to-CLI bindings after both roots are available."""
    return _load_hook_cli_calls(
        hook_cli_calls_file(),
        plugin_root=plugin_root(),
        project_root=project_root(),
    )


def hook_cli_env() -> dict:
    """Load hook-to-CLI environment bindings after both roots are available."""
    return _load_hook_cli_env(
        hook_cli_calls_file(),
        plugin_root=plugin_root(),
        project_root=project_root(),
    )


def __getattr__(name: str):
    """Expose the legacy uppercase constants lazily.

    Importing ``src.config`` must not resolve PROJECT_ROOT just to obtain the
    plugin-owned logging values. A caller that asks for PROJECT_ROOT still gets the
    same strict exception, but unrelated values remain independently available.
    """
    lazy = {
        "PROJECT_ROOT": project_root,
        "PLUGIN_ROOT": plugin_root,
        "PLUGIN_DATA": plugin_data,
        "HOOKS_LOG_FILE": hooks_log_file,
        "HOOKS_LOG_LEVEL": hooks_log_level,
        "CHANGES_TRACKER_DIR": changes_tracker_dir,
        "HOOK_CLI_CALLS_FILE": hook_cli_calls_file,
        "HOOK_CLI_CALLS": hook_cli_calls,
        "HOOK_CLI_ENV": hook_cli_env,
    }
    resolver = lazy.get(name)
    if resolver is None:
        raise AttributeError(name)
    return resolver()

# Hook diagnostic flag for SessionStart and Stop. setup_logger filters this output to
# environment names containing PROJECT, PLUGIN, or SESSION. Session values are redacted.
LOG_ENVS = True

# The hook -> CLI call map. Loaded from HOOK_CLI_CALLS_FILE, a fixed path inside the
# plugin checkout; an event the file omits makes no call. Not overridable by
# environment: the wiring is part of the installed plugin, not of a session's env.
#
# Format: event -> {"cmd": argv list, "timeout": seconds}, flat or under a top-level
# "hook_cli_calls" key. A sibling top-level "env" block (see _load_hook_cli_env) adds
# variables to the environment of every call. JSON only -- every hook subprocess re-imports this module, and
# the compiled hookrunner ships no third-party parser.
#
# `timeout` is optional and falls back to hook_cli_calls.CLI_CALL_TIMEOUT_SECONDS. It
# exists because one number cannot serve every call: that default was sized for a
# heartbeat stamp, where a late write is worthless so abandoning it is right, while
# call_on_stop_hook is a DECISION -- abandoning that one does not skip a stale write, it
# allows a stop that was meant to be gated.
#
# cmd[0] is used as-is (absolute path, or bare name for PATH lookup).
# {plugin_root}/{project_root} expand at LOAD time; {run_id}/{agent_id} are left for
# hook_cli_calls._fill at CALL time.
#
# An entry declares EITHER `cmd` (a process to run) or `policy` (a literal payload,
# carried here and answered without spawning anything). Never both.
#
# Stdout conventions (see the cli_stubs/ script of each for the fields):
#   call_on_session_start  JSON the calling hook re-emits (invoke_and_get_payload).
#   call_on_stop_hook      Claude Code's Stop decision protocol, relayed verbatim
#                          (run_and_get_exit_code).
#
# Literal conventions:
#   subagent_fetch_policy  the project's playbook `policy:` block VERBATIM, asked per
#                          bash call (invoke_and_get_payload). No process, no run_id,
#                          no --doc. Nothing configured means nothing denied.
#
# The names below are the vocabulary, not a schema: the loader does not check against
# them, and an entry the file omits simply makes no call.
HOOK_CLI_EVENTS = (
    "call_on_session_start",
    "call_on_compaction",
    "call_on_stop_hook",
    "subagent_sync_call_on_bash",
    "subagent_sync_call_on_edit_write",
    "subagent_fetch_policy",
    "subagent_call_on_run_id_resolved",
    "subagent_call_on_run_finished",
)

# NO plugin-side denylist. What a dispatched sub-agent may not run is the PROJECT's
# policy, declared in the project's own playbook YAML and served by the CLI wired to
# subagent_fetch_policy, which handler_bash.py asks on every bash call. The plugin
# enforces what it receives and nothing else: no policy received, no forbidden calls.
#
# A hardcoded set here was a second copy of that policy, and the two disagreed -- the
# YAML denied `play advance` and `git`, this held `git` alone. Worse, the copy made the
# denial depend on delivery: the fetched half arrived once, at first contact, and was
# cached on the session ledger, so any gap in that chain left only the hardcoded half
# and a sub-agent could advance its own run to completion with no commit.

# run_id extraction from bash invocations (handler_bash.py): command_name -> regex with
# one capture group. ANCHORED to the segment start so a mention inside a later argument
# (`grep "play show fake123" f.py`) never matches. Accepts an optional path prefix and
# an optional `cwpilot ` prefix. Tried in dict order; first match wins.
#
# `advance` is here so the gate can resolve a run from the very command it is judging:
# `play advance <run_id>` names its own run, which is what lets the policy fetch happen
# on a sub-agent's FIRST bash call, before its ledger has detected anything.
RUN_ID_EXTRACTION_PATTERNS = {
    "play": r"^\s*(?:\S*/)?(?:cwpilot\s+)?play\s+(?:show|advance|force-complete|fail|start)\s+(\S+)",
}

# Shell-operator delimiters (handler_bash.py) splitting a compound command into
# segments, so each piece can be checked for whether it genuinely INVOKES a tracked
# command rather than merely mentioning one inside a later segment.
SHELL_SEPARATOR_PATTERN = r'&&|\|\||\||;'

# The sub-agent's first fetch of its dispatch instructions -- the delivery trigger
# (handler_bash._play_run_show_run_id). Narrower than the "play" pattern above (show
# only), because it marks a different event, not just another id-bearing invocation.
PLAY_RUN_SHOW_PATTERN = r"^\s*(?:\S*/)?(?:cwpilot\s+)?play\s+show\s+(\S+)"

# Liveness heartbeat interval (seconds).
#
# INVARIANT: must stay well below the project's 180s stale threshold, so a healthy agent
# misses several consecutive stamps before anything presumes it dead (three at 60s).
# 60 rather than 30 because each stamp is a real write (~1.5s): four concurrent agents at
# 30s cost ~26s of CPU per minute on liveness alone.
AGENT_LIVE_STAMP_INTERVAL_SECONDS = 60.0

# How long a ledger keeps bash_invocations / file_stats entries before they age out,
# on top of the size-based trim. Only recent activity feeds staleness checks and
# evidence review; older entries are unbounded growth.
BASH_INVOCATION_RETENTION_SECONDS = 3600.0
FILE_STATS_RETENTION_SECONDS = 3600.0

# Eviction grace for running_agents: an entry is removed once this has elapsed since the
# latest of its started_at/updated_at/finished_at. The ONLY eviction mechanism for them.
RUNNING_AGENT_GRACE_SECONDS = 60 * 60


# Appended to the block message when handler_edit.py/handler_write.py refuse an edit
# because hooks.have_unverified_constraints() found a spec with unverified constraints.
GUIDE_MESSAGE_UNVERIFIED_BLOCKING_CONSTRAINTS = (
    "Resolve or remove the unverified constraint(s) before editing further."
)

# Escape hatch for the unverified-constraints edit block above -- set True to disable
# the check entirely (e.g. a project with no spec.k.json/project.k.json convention).
TEMPORARY_BYPASS_UNVERIFIED_CONSTRAINTS_BLOCK = False

__all__ = [
    "set_hook_input",
    "project_root",
    "plugin_root",
    "plugin_data",
    "hooks_log_file",
    "hooks_log_level",
    "changes_tracker_dir",
    "hook_cli_calls_file",
    "hook_cli_calls",
    "hook_cli_env",
    "PROJECT_ROOT",
    "PLUGIN_ROOT",
    "PLUGIN_DATA",
    "HOOKS_LOG_FILE",
    "HOOKS_LOG_LEVEL",
    "LOG_ENVS",
    "CHANGES_TRACKER_DIR",
    "HOOK_CLI_EVENTS",
    "HOOK_CLI_CALLS_FILE",
    "HOOK_CLI_CALLS",
    "RUN_ID_EXTRACTION_PATTERNS",
    "SHELL_SEPARATOR_PATTERN",
    "PLAY_RUN_SHOW_PATTERN",
    "AGENT_LIVE_STAMP_INTERVAL_SECONDS",
    "BASH_INVOCATION_RETENTION_SECONDS",
    "FILE_STATS_RETENTION_SECONDS",
    "RUNNING_AGENT_GRACE_SECONDS",
    "GUIDE_MESSAGE_UNVERIFIED_BLOCKING_CONSTRAINTS",
    "TEMPORARY_BYPASS_UNVERIFIED_CONSTRAINTS_BLOCK",
]
