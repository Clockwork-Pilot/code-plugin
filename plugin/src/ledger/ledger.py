"""Ledger API: factory functions for different ledger types.

Uses PersistentLedgerReaderMixin and PersistentLedgerWriterMixin for operations.
Path resolution, validation, and agent discovery in ledger_read.py.
"""

import json
from pathlib import Path

from .ledger_read import (
    PersistentLedgerReaderMixin,
    main_ledger_path,
    agent_ledger_path,
)
from .ledger_write import PersistentLedgerWriterMixin


# --- Base Tracker ---------------------------------------------------------------

class PersistentLedgerTracker(PersistentLedgerReaderMixin, PersistentLedgerWriterMixin):
    """Base tracker for all persistent ledger operations.

    Maintains one persistent ledger (file_stats/bash_invocations/running_agents/...)
    for evidence capture.

    Data is loaded once at initialization and cached for the lifetime of the
    tracker instance. All read operations use the cached data. Write operations
    invalidate the cache (which is reloaded on the next load() call, ensuring
    any external writes are visible).

    Inherits from both PersistentLedgerReaderMixin (read operations) and
    PersistentLedgerWriterMixin (write operations, schema management).
    """

    def __init__(self, path: Path | None = None) -> None:
        """Initialize tracker for a specific ledger path.

        Loads the ledger data immediately and caches it for subsequent operations.

        Args:
            path: The full path to the ledger JSON file. Defaults to main_ledger_path() if None.
        """
        self.path = path if path is not None else main_ledger_path()
        self._cache: dict | None = None
        self._ledger_present: bool = False
        self.load()

    def load(self) -> dict:
        """Return the cached ledger data (loaded once at __init__).

        Cache is always populated since __init__ calls load(). Returns the
        in-memory cache directly without any disk I/O after initialization.

        A ledger that could not be read (absent/corrupt) is schema-filled to the
        same shape as an empty one, so ``_ledger_present`` records which of the two
        it was: callers that must not read "no ledger" as "nothing happened" (the
        Stop hook's constraint gate) need that distinction back.
        """
        if self._cache is None:
            try:
                data = json.loads(self.path.read_text())
                self._ledger_present = True
            except (OSError, ValueError):
                data = {}
                self._ledger_present = False
            self._cache = self._ensure_schema(data)
        return self._cache


# --- Factory Functions ----------------------------------------------------------

def session_ledger() -> PersistentLedgerTracker:
    """Create a tracker for the main window session ledger (session_main.json).

    Maintains session-wide evidence: running_agents, bash_invocations, file_stats, etc.
    Used by the main orchestrator to track all active runs and agents.

    Returns:
        PersistentLedgerTracker for the session ledger.
    """
    return PersistentLedgerTracker(path=main_ledger_path())


def agent_ledger(agent_id: str) -> PersistentLedgerTracker:
    """Create a tracker for a sub-agent ledger (agents/agent_<id>.json).

    Maintains per-agent evidence: file changes, bash commands, error logs.

    Args:
        agent_id: The unique identifier for the sub-agent.

    Returns:
        PersistentLedgerTracker for the agent ledger with agent_id attribute.
    """
    tracker = PersistentLedgerTracker(path=agent_ledger_path(agent_id))
    tracker.agent_id = agent_id
    return tracker


def _agent_is_known(agent_id: str) -> bool:
    """True when ``agent_id`` names a sub-agent this session has actually seen start.

    "Seen" means either an existing ledger file for it (any prior hook -- most
    reliably its SubagentStart, which calls ``record_own_agent_id``) or a
    ``running_agents`` entry in the session ledger. An id matching neither is a
    transient value the harness handed a single Edit/Write hook and never
    announced; minting a fresh ``agents/<ts>-<id>.json`` for it is what produced
    orphan ledgers with ``agent_id: null`` bodies.
    """
    try:
        if agent_ledger_path(agent_id).exists():
            return True
    except OSError:
        pass
    try:
        sess = session_ledger()
        sess.load()
        return agent_id in (sess.get_running_agents() or {})
    except Exception:  # pylint: disable=broad-except
        return False


def tracker_for_hook(agent_id):
    """The ledger a file/bash hook should record into.

    An agent ledger only for a KNOWN sub-agent (see ``_agent_is_known``); the
    session ledger for an absent or unrecognised ``agent_id``. This is the guard
    that keeps a transient id from spawning an orphan agent ledger.
    """
    if agent_id and _agent_is_known(agent_id):
        return agent_ledger(agent_id)
    return session_ledger()


__all__ = [
    # Base tracker
    "PersistentLedgerTracker",
    # Factory functions
    "session_ledger",
    "agent_ledger",
    "tracker_for_hook",
]
