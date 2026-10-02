"""Ledgers storage package: centralized ledger IO for changes-tracker.

PUBLIC API (re-exported from ledger and ledger_read modules; consumed by hooks/):
  Path/directory functions:
    - agent_ledger_path(agent_id) -> Path
    - changes_tracker_dir() -> Path

  High-level tracker access:
    - PersistentLedgerTracker() -> direct instantiation for main window ledger
    - session_ledger() / agent_ledger(agent_id) -> PersistentLedgerTracker

  Ledger maintenance (GC):
    - sweep_stale_ledger_files(max_age_seconds, doc) -> list[str]

This package owns ONLY storage/IO primitives and the thin PersistentLedgerTracker
class. Higher-level logic (stale detection, liveness classification, orphan
detection) stays in other modules (src.runinfo, etc.).

Other read-only helpers (agent_ledger_filename, main_ledger_path,
list_live_agents, the mixins, _sanitize_ledger_id, _now_iso, etc.) are not
consumed by hooks/ and are imported directly from their submodules
(ledger_read, ledger_write, utils) where still needed (mainly tests).

IMPLEMENTATION SPLIT:
  - ledger_read.py: PersistentLedgerReaderMixin (read-only operations)
  - ledger_write.py: PersistentLedgerWriterMixin (write/mutation operations + GC)
  - ledger.py: PersistentLedgerTracker (inherits both mixins), path resolution, validation
  - __init__.py: thin re-export for backward compatibility
"""

# Re-export from ledger_read (path resolution used by hooks/)
from .ledger_read import agent_ledger_path, changes_tracker_dir

# Re-export from ledger (tracker + factory functions)
from .ledger import (
    PersistentLedgerTracker,
    session_ledger,
    agent_ledger,
    tracker_for_hook,
)

# Re-export from ledger_write (GC and maintenance)
from .ledger_write import (
    sweep_stale_ledger_files,
)

__all__ = [
    # Path resolution
    "agent_ledger_path",
    "changes_tracker_dir",
    # Tracker base
    "PersistentLedgerTracker",
    # Factory functions
    "session_ledger",
    "agent_ledger",
    # Ledger maintenance
    "sweep_stale_ledger_files",
]
