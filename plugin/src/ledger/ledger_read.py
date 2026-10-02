"""Ledger READ API: path resolution, validation, and read-only operations.

Provides:
  1. PersistentLedgerReaderMixin: mixin class with all read methods
  2. Module-level functions: path resolution, validation, agent discovery
"""

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import src.config as config
from .utils import _sanitize_ledger_id

_LOGGER = logging.getLogger(__name__)

# Agent ledger filenames are <iso_timestamp>-<agent_id>.json. The timestamp is the
# same ISO 8601 form the ledger stores inside (utils._now_iso: UTC, microseconds,
# +00:00), stamped at ledger CREATION so files sort and age chronologically on disk
# and compare directly against in-ledger timestamps. The format is fixed-width so the
# agent_id can be recovered from a filename by POSITION, not by splitting on "-"
# (which the agent_id itself may also contain).
_AGENT_LEDGER_TS_LEN = len("2000-01-01T00:00:00.000000+00:00")
# Pre-ISO ledgers (<yyyymmdd_hhmmss>-<agent_id>.json) are still recognised so an
# agent's existing file is found rather than duplicated after an upgrade.
_LEGACY_AGENT_LEDGER_TS_LEN = len("20000101_000000")
MAIN_WINDOW_LEDGER_FILENAME = "session_main.json"


def agent_ledger_filename(agent_id: str, timestamp: Optional[datetime] = None) -> str:
    """The ledger filename for one sub-agent: ``<iso_timestamp>-<agent_id>.json``,
    stamped (ISO 8601, UTC, microseconds) at ledger CREATION time. ``timestamp`` defaults
    to now; a caller re-deriving the filename an existing file already used passes it
    explicitly (see ``agent_ledger_path``'s find-or-create resolution below).
    """
    ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="microseconds")
    return f"{ts}-{_sanitize_ledger_id(agent_id)}.json"


def _agent_id_from_ledger_filename(filename: str) -> Optional[str]:
    """Recover the agent_id an ``agent_ledger_filename()`` name was built for, or
    None if ``filename`` doesn't match the ``<fixed-width timestamp>-<agent_id>.json``
    shape (ISO, or the legacy ``yyyymmdd_hhmmss`` form). Position-based (not a ``.split("-")``): the agent_id itself may contain
    ``-`` (``_sanitize_ledger_id`` allows it), so only the fixed-width timestamp
    prefix is a reliable delimiter.
    """
    if not filename.endswith(".json"):
        return None
    stem = filename[:-len(".json")]
    for ts_len in (_AGENT_LEDGER_TS_LEN, _LEGACY_AGENT_LEDGER_TS_LEN):
        if len(stem) > ts_len and stem[ts_len] == "-" and stem[:1].isdigit():
            ts = stem[:ts_len]
            if ts_len == _AGENT_LEDGER_TS_LEN and ts[10] != "T":
                continue
            if ts_len == _LEGACY_AGENT_LEDGER_TS_LEN and ts[8] != "_":
                continue
            return stem[ts_len + 1:]
    return None


def agent_ledger_path(agent_id: str) -> Path:
    """The persistent ledger path for one sub-agent.

    An agent's ledger accumulates state (bash_invocations, file_stats,
    detected_run_id, ...) across many SEPARATE hook process invocations
    over its whole lifetime (SubagentStart, every Bash/Edit heartbeat,
    SubagentStop) -- each of which calls this function fresh. Since the filename
    embeds a creation timestamp, minting a new one on every call would fragment one
    agent's history across a new file each time. So this FINDS an existing file for
    agent_id first (by scanning agents/ and matching via
    ``_agent_id_from_ledger_filename``, not a wildcard glob suffix -- a suffix glob
    would false-match e.g. agent_id "A" against an existing "...-B-A.json") and only
    mints a new (now-timestamped) filename when no existing file matches.
    """
    agents_dir = config.CHANGES_TRACKER_DIR / "agents"
    sanitized = _sanitize_ledger_id(agent_id)
    if agents_dir.is_dir():
        try:
            for p in agents_dir.iterdir():
                if p.is_file() and _agent_id_from_ledger_filename(p.name) == sanitized:
                    return p
        except OSError:
            pass
    return agents_dir / agent_ledger_filename(agent_id)


def main_ledger_path() -> Path:
    """The persistent ledger path for the main window."""
    return config.CHANGES_TRACKER_DIR / MAIN_WINDOW_LEDGER_FILENAME


# --- Public API: Agent operations -----------------------------------------------

def list_live_agents(age_threshold_seconds: float = 600.0) -> List[str]:
    """List agent_ids with recent file-write activity (mtime <= age_threshold).

    Args:
        age_threshold_seconds: Maximum age in seconds (default 600 = 10 min)

    Returns:
        list[str]: agent_ids whose ledger files are younger than the threshold
    """
    live: List[str] = []
    # Look up changes_tracker_dir from src.ledgers to allow monkeypatching
    ledgers_mod = sys.modules.get('src.ledgers', None)
    if ledgers_mod and hasattr(ledgers_mod, 'changes_tracker_dir'):
        get_dir = getattr(ledgers_mod, 'changes_tracker_dir')
    else:
        get_dir = changes_tracker_dir
    agents_dir = get_dir() / "agents"
    if not agents_dir.exists():
        return live

    try:
        now_epoch = time.time()
        cutoff = now_epoch - age_threshold_seconds
        for path in agents_dir.glob("*.json"):
            try:
                if path.stat().st_mtime > cutoff:
                    # Extract agent_id from filename for simplicity
                    # (could also read the file's own agent_id field for validation)
                    agent_id = _agent_id_from_ledger_filename(path.name)
                    if agent_id is not None:
                        live.append(agent_id)
            except OSError:
                continue
    except OSError:
        return live

    return live


def changes_tracker_dir() -> Path:
    """Path to the persistent ledger directory (from config), e.g. PROJECT_ROOT/.changes-tracker/."""
    return config.CHANGES_TRACKER_DIR


class PersistentLedgerReaderMixin:
    """Mixin providing read-only operations for persistent ledger state.

    Expects the class using this mixin to provide:
      - self.path: Path to the ledger file
      - self._cache: in-memory cache dict
      - self.load(): method to load ledger
    """

    def get_running_agents(self) -> dict:
        """Return the current running_agents dict (keyed by agent_id)."""
        return self._cache.get("running_agents") or {}

    def unfinished_agent_ids(self) -> List[str]:
        """agent_ids in running_agents that have NOT been marked finished_at.

        The Stop hook's keep-warm poll condition: an empty list means every
        dispatched sub-agent has returned (SubagentStop stamps finished_at on the
        SESSION ledger unconditionally -- see handler_subagent_stop). An entry with
        a non-dict body is treated as unfinished (unknown state, do not release on it).
        """
        out: List[str] = []
        for agent_id, entry in (self.get_running_agents() or {}).items():
            if not isinstance(entry, dict) or not entry.get("finished_at"):
                out.append(agent_id)
        return out

    def get_bash_invocations(self) -> dict:
        """Return the current bash_invocations dict."""
        return self._cache.get("bash_invocations") or {}

    def ledger_exists(self) -> bool:
        """Whether this ledger was actually read from disk, vs schema-filled from nothing.

        False means "no information recorded" -- NOT "nothing happened". A caller
        deciding whether work has occurred must not read an absent ledger as an
        empty one.

        Checks both the cached flag (from initialization) and whether the file
        still exists on disk, so deletions after session start are detected.
        """
        return self.path.exists() and getattr(self, "_ledger_present", False)

    def get_file_stats(self) -> dict:
        """Return the current file_stats dict."""
        return self._cache.get("file_stats") or {"files_created": {}, "files_modified": {}}

    def get_windowed_file_stats(self, ended_at: str | None) -> dict:
        """Return this ledger's file_stats, pre-filtered to entries with timestamp
        <= ended_at -- shared by any caller windowing evidence for a completed run,
        so this windowing logic is never duplicated.

        Only the upper bound is applied here: a caller folding the result (e.g. via
        knowledge_tool.playbook_run._fold_file_stats) applies its own since_timestamp
        for the lower bound (ts >= started_at) at fold time, so combining this with
        that gets the full two-sided [started_at, ended_at] window.
        """
        file_stats = self.get_file_stats()
        windowed: dict = {}
        for kind in ("files_created", "files_modified"):
            paths = file_stats.get(kind) or {}
            windowed_paths: dict = {}
            for path, entries in paths.items():
                if isinstance(entries, dict) and ended_at:
                    entries = {ts: v for ts, v in entries.items() if ts <= ended_at}
                windowed_paths[path] = entries
            windowed[kind] = windowed_paths
        return windowed

    def agent_exists_in_running_agents(self, agent_id: str) -> bool:
        """Check if an agent exists in running_agents.

        Args:
            agent_id: The agent ID to check.

        Returns:
            True if the agent is present in running_agents, False otherwise.
        """
        running_agents = self.get_running_agents()
        return agent_id in running_agents

    def get_updated_at(self) -> str | None:
        """Return this ledger's own top-level updated_at timestamp (stamped by
        every _write() call), or None if it has never been written."""
        return self._cache.get("updated_at")

    def get_last_check_result(self) -> dict | None:
        """Return the persisted result of the most recent check_constraints() run, or None."""
        return self._cache.get("last_check_result")

    def get_detected_playbook_run_id(self) -> str | None:
        """Return this ledger's in-flight detected playbook run_id, or None.

        The read-side counterpart of ``record_detected_run`` -- used to
        bind a bash-invocation record to the active run/step context already
        detected earlier in this ledger's lifetime (e.g. a subagent's own ledger,
        tagged once a play-run/play-advance invocation was observed).
        """
        return self._cache.get("detected_run_id")


__all__ = [
    # Mixin
    "PersistentLedgerReaderMixin",
    # Path resolution
    "main_window_ledger_filename",
    "agent_ledger_filename",
    "main_ledger_path",
    "agent_ledger_path",
    "changes_tracker_dir",
    # Input validation
    "is_valid_run_id",
    # Agent operations
    "list_live_agents",
]
