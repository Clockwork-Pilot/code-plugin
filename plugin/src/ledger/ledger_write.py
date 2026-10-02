"""Ledger WRITE API: mixin providing mutation/write operations for ledger state.

Provides:
  1. PersistentLedgerWriterMixin: mixin class with all write/mutation methods
  2. Ledger maintenance functions: GC, stale file sweeping, etc.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import src.config as config
from .utils import (_sanitize_ledger_id, _now_iso, parse_iso_timestamp,
                     prune_older_than, dumps_indented)

_LOGGER = logging.getLogger(__name__)

# Mirrors knowledge_tool.knowledge_tool.knowledge_config's retry constants -- duplicated
# locally (not imported) since src/ledgers may not depend on knowledge_tool.
DEFAULT_RETRY_ATTEMPTS = 5
MINIMAL_RETRY_DELAY_MS = 100


class PersistentLedgerWriterMixin:
    """Mixin providing write/mutation operations for persistent ledger state.

    Expects the class using this mixin to provide:
      - self.path: Path to the ledger file
      - self._cache: in-memory cache dict
      - self.load(): method to load ledger
      - self.get_running_agents(): read getter
    """

    def set_running_agents(self, running_agents: dict) -> None:
        """Update the running_agents dict.

        Session ledger only. Agent ledgers must not manipulate running_agents.
        """
        if hasattr(self, 'agent_id') and self.agent_id:
            raise RuntimeError(f"Agent ledger {self.agent_id} cannot manipulate running_agents (session-only)")
        self._cache["running_agents"] = running_agents

    def set_bash_invocations(self, bash_invocations: dict) -> None:
        """Update the bash_invocations dict."""
        self._cache["bash_invocations"] = bash_invocations

    def set_file_stats(self, file_stats: dict) -> None:
        """Update the file_stats dict."""
        self._cache["file_stats"] = file_stats

    def _ensure_schema(self, data: dict) -> dict:
        """Fill in missing fields with defaults; return the normalized data."""
        if "running_agents" not in data:
            data["running_agents"] = {}
        if "bash_invocations" not in data:
            data["bash_invocations"] = {}
        if "file_stats" not in data:
            data["file_stats"] = {"files_created": {}, "files_modified": {}}
        if "agent_id" not in data:
            data["agent_id"] = None
        if "last_prompt_at" not in data:
            data["last_prompt_at"] = None
        if "last_command_at" not in data:
            data["last_command_at"] = None
        if "last_file_change_at" not in data:
            data["last_file_change_at"] = None
        if "last_check_result" not in data:
            data["last_check_result"] = None
        if "detected_run_id" not in data:
            data["detected_run_id"] = None
        if "detected_run_id_at" not in data:
            data["detected_run_id_at"] = None
        if "subagent_finished_at" not in data:
            data["subagent_finished_at"] = None
        if "created_at" not in data:
            data["created_at"] = _now_iso()
        if "updated_at" not in data:
            data["updated_at"] = _now_iso()
        return data

    def _is_main_window_ledger(self) -> bool:
        """Check if this is the main session ledger."""
        from .ledger_read import main_ledger_path
        return self.path == main_ledger_path()

    def _is_agent_ledger(self) -> bool:
        """Check if this is an agent ledger (has agent_id)."""
        return bool(getattr(self, 'agent_id', None))

    def _prune_stale_file_stats(self, data: dict) -> None:
        """Prune file_stats entries older than FILE_STATS_RETENTION_SECONDS, across
        EVERY tracked path, in place on ``data``.

        record_file_change/record_file_created/record_file_modified only prune the
        single path they're currently recording -- a path with no further activity
        would otherwise keep its stale entries forever. This is the backstop sweep
        that covers every path on every write, mirroring how bash_invocations'
        retention is enforced. A path whose entries all age out is dropped entirely
        rather than left as an empty dict.
        """
        file_stats = data.get("file_stats")
        if not isinstance(file_stats, dict):
            return
        for kind in ("files_created", "files_modified"):
            paths = file_stats.get(kind)
            if not isinstance(paths, dict):
                continue
            for path in list(paths.keys()):
                entries = paths[path]
                if not isinstance(entries, dict):
                    continue
                pruned = prune_older_than(entries, config.FILE_STATS_RETENTION_SECONDS)
                if pruned:
                    paths[path] = pruned
                else:
                    paths.pop(path, None)

    def _write(self, data: dict) -> None:
        """Atomically write the tracker and update cache, with retry on transient OSError.

        Retries the tmp-write + os.replace sequence up to DEFAULT_RETRY_ATTEMPTS times
        with exponential backoff on OSError (e.g. ENOENT, EACCES) to handle transient
        filesystem issues. Permanent failures (all retries exhausted) re-raise OSError.
        """
        data["updated_at"] = _now_iso()

        # Sweep EVERY tracked path's file_stats entries, not just the one path a
        # record_file_change/record_file_created/record_file_modified call is
        # currently touching -- a file edited once and never touched again would
        # otherwise keep its stale entries forever. Every ledger write (bash, edit,
        # write -- this method is the single write chokepoint for all of them)
        # already re-reads/rewrites the whole ledger, so it's a free hook point for
        # this sweep. Only run on the main session ledger; agent ledgers preserve
        # all file_stats as they are whole-run evidence records.
        if not self._is_agent_ledger():
            try:
                self._prune_stale_file_stats(data)
            except Exception:
                # Never block a write on this backstop sweep; log failures and continue
                pass


        tmp = Path(str(self.path) + ".tmp")
        attempt = 0
        last_error = None

        while attempt < DEFAULT_RETRY_ATTEMPTS:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(dumps_indented(data))
                os.replace(tmp, self.path)

                # Success - update cache and return
                self._cache = data
                self._ledger_present = True

                if attempt > 0:
                    _LOGGER.debug(f"Ledger write succeeded after {attempt} retry attempt(s)")
                break

            except OSError as e:
                last_error = e
                attempt += 1

                # Clean up tmp file if it exists
                try:
                    tmp.unlink()
                except OSError:
                    pass

                # If this was the last attempt, raise the error
                if attempt >= DEFAULT_RETRY_ATTEMPTS:
                    raise

                # Exponential backoff: delay_ms, delay_ms * 1.5, delay_ms * 2.25, etc.
                delay_seconds = min(
                    MINIMAL_RETRY_DELAY_MS * (1.5 ** (attempt - 1)) / 1000,
                    2.0  # Cap at 2 seconds
                )
                _LOGGER.debug(f"Ledger write transient error (attempt {attempt}/{DEFAULT_RETRY_ATTEMPTS}). "
                             f"Retrying in {delay_seconds*1000:.0f}ms...")
                time.sleep(delay_seconds)

        # Evict expired running_agents entries on every main-window write, so grace
        # expiry doesn't need a separate trigger.
        if self._is_main_window_ledger():
            self._evict_expired_running_agents()

    def update_timestamp(self, agent_id: str, timestamp_name: str, value: str) -> None:
        """Update a timestamp field for an agent in running_agents.

        When setting synced_at (or any timestamp), also maintains created_at (first set)
        and updated_at (last update time) for lifecycle tracking.

        Session ledger only.
        """
        if hasattr(self, 'agent_id') and self.agent_id:
            _LOGGER.debug(f"Agent ledger {self.agent_id} ignoring running_agents update (session-only)")
            return
        running_agents = self.get_running_agents()

        if agent_id not in running_agents:
            running_agents[agent_id] = {}

        if not isinstance(running_agents[agent_id], dict):
            running_agents[agent_id] = {}

        entry = running_agents[agent_id]
        # Always update the target timestamp
        entry[timestamp_name] = value
        # Always refresh updated_at to track when entry was last touched
        entry["updated_at"] = value

        self.set_running_agents(running_agents)
        self._write(self._cache)

    # NO set_denied_commands. A sub-agent's denylist is the project's policy, fetched
    # from it per bash call (handler_bash.py's subagent_fetch_policy) and never written
    # here. Storing it made a security decision into ledger DATA: anything able to write
    # this file could author a restriction -- forge one broad enough to refuse the
    # agent's every command, or clear one to lift it -- and the enforcement point trusted
    # the file rather than the playbook that declared it. A write path with no caller is
    # still that surface, so it is gone rather than merely unused.

    def clear_timestamp(self, agent_id: str, timestamp_name: str) -> None:
        """Remove a timestamp field from an agent in running_agents.

        The counterpart to :meth:`update_timestamp` -- whatever stamps a field must have a
        way to un-stamp it in the SAME place. Unlike update_timestamp this never creates an
        agent entry: clearing a field on an agent that isn't there is a no-op, not a reason
        to invent one.
        """
        running_agents = self.get_running_agents()
        entry = running_agents.get(agent_id)
        if not isinstance(entry, dict) or timestamp_name not in entry:
            return
        entry.pop(timestamp_name)
        self.set_running_agents(running_agents)
        self._write(self._cache)

    def record_bash_invocation(self, command: str, exit_code: int | None = None,
                              duration_ms: int | None = None, phase: str = "PostToolUse",
                              stdout: str | None = None, stderr: str | None = None,
                              reason: str | None = None) -> None:
        """Record a bash tool invocation."""
        bash_invocations = self.get_bash_invocations()

        cmd_val = command[:500] if command else ""
        if not cmd_val:
            return

        key = _now_iso()
        entry = {
            "command": cmd_val,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "type": phase
        }

        if stdout is not None:
            entry["stdout"] = stdout[:100]
        if stderr is not None:
            entry["stderr"] = stderr[:100]
        if reason:
            entry["reason"] = reason[:500]

        bash_invocations[key] = entry

        # Only prune retention on main session ledger; agent ledgers preserve all
        # bash invocations as whole-run evidence records.
        if not self._is_agent_ledger():
            bash_invocations = prune_older_than(
                bash_invocations, config.BASH_INVOCATION_RETENTION_SECONDS
            )

        def size_of(k: str, v: dict) -> int:
            s = len(k) + len(v.get("command", ""))
            s += len(str(v.get("exit_code", ""))) + len(str(v.get("duration_ms", "")))
            s += len(v.get("type", "")) + len(v.get("stdout", "")) + len(v.get("stderr", ""))
            s += len(v.get("reason", ""))
            return s

        # Only cap bash invocations at 10KB on main session ledger; agent ledgers
        # preserve all bash invocations as whole-run evidence records.
        if not self._is_agent_ledger():
            cumulative = sum(size_of(k, v) for k, v in bash_invocations.items() if isinstance(v, dict))
            while cumulative > 10240 and bash_invocations:
                first_key = next(iter(bash_invocations))
                cumulative -= size_of(first_key, bash_invocations.pop(first_key, {}))

        self.set_bash_invocations(bash_invocations)
        self._write(self._cache)

    def record_file_created(self, file_path: str, timestamp: str | None = None) -> None:
        """Record that a file was created.

        Retention (FILE_STATS_RETENTION_SECONDS) is enforced by _write()'s
        _prune_stale_file_stats() sweep, not here -- that sweep covers every
        tracked path on every write, not just this one.
        """
        if not file_path:
            return
        timestamp = timestamp or _now_iso()
        file_stats = self.get_file_stats()
        created = file_stats.get("files_created", {})
        created.setdefault(file_path, {})[timestamp] = []
        self.set_file_stats(file_stats)
        self._write(self._cache)

    def record_file_modified(self, file_path: str, line_range: list | None = None,
                            timestamp: str | None = None) -> None:
        """Record that a file was modified.

        Retention (FILE_STATS_RETENTION_SECONDS) is enforced by _write()'s
        _prune_stale_file_stats() sweep, not here -- that sweep covers every
        tracked path on every write, not just this one.
        """
        if not file_path:
            return
        timestamp = timestamp or _now_iso()
        file_stats = self.get_file_stats()
        modified = file_stats.get("files_modified", {})
        modified.setdefault(file_path, {})[timestamp] = line_range or []
        self.set_file_stats(file_stats)
        self._write(self._cache)

    def record_agent_start(self, agent_id: str, timestamp: str | None = None,
                           model: str | None = None) -> None:
        """Record a SubagentStart event: agent_id is now running.

        ``model`` is the sub-agent's model as reported in the SubagentStart hook
        payload (e.g. "claude-haiku-4-5") -- recorded so the model a sub-agent was
        actually spawned on is observable, not merely inferable from a transcript.

        Session ledger only.
        """
        if hasattr(self, 'agent_id') and self.agent_id:
            _LOGGER.debug(f"Agent ledger {self.agent_id} ignoring running_agents update (session-only)")
            return
        if not agent_id:
            return

        timestamp = timestamp or _now_iso()
        running_agents = self.get_running_agents()
        entry = {"started_at": timestamp, "updated_at": timestamp}
        if model:
            entry["model"] = model
        running_agents[agent_id] = entry
        self.set_running_agents(running_agents)
        self._write(self._cache)

    def record_agent_finished(self, agent_id: str) -> None:
        """Record a SubagentStop event.

        On the SESSION ledger this marks agent_id's ``running_agents`` entry finished.
        On an AGENT ledger there is no running_agents map to update -- that stays
        session-only -- but the ledger stamps its own ``subagent_finished_at`` so the
        agent's file says when it stopped, the same way it already says when it started
        (created_at) and when it last acted (updated_at). Without it an agent ledger
        recorded every fact about a sub-agent's life except its end, and completion had
        to be inferred from silence.
        """
        if hasattr(self, 'agent_id') and self.agent_id:
            if agent_id and agent_id == self.agent_id:
                self._cache["subagent_finished_at"] = _now_iso()
                self._write(self._cache)
                return
            _LOGGER.debug(f"Agent ledger {self.agent_id} ignoring running_agents update (session-only)")
            return
        if not agent_id:
            return

        running_agents = self.get_running_agents()
        if agent_id in running_agents and isinstance(running_agents[agent_id], dict):
            running_agents[agent_id]["finished_at"] = _now_iso()
        self.set_running_agents(running_agents)

        self._write(self._cache)

    def _evict_expired_running_agents(self) -> list:
        """Hard-evict running_agents entries whose grace period has expired.

        An entry is evicted once RUNNING_AGENT_GRACE_SECONDS has elapsed since the
        most recent of its started_at/updated_at/finished_at timestamps (or
        immediately, if none of those are present/parseable). This is the ONLY
        staleness/eviction mechanism for running_agents -- no file-mtime reads, no
        live-agent-id cross-referencing, no other stale detection.
        """
        running_agents = self.get_running_agents()
        if not running_agents:
            return []

        now_epoch = time.time()
        evicted: list = []

        for agent_id, entry in list(running_agents.items()):
            if not isinstance(entry, dict):
                evicted.append(agent_id)
                running_agents.pop(agent_id, None)
                continue

            timestamps = [
                parse_iso_timestamp(entry.get(field))
                for field in ("started_at", "updated_at", "finished_at")
                if entry.get(field)
            ]
            timestamps = [ts for ts in timestamps if ts is not None]

            if not timestamps or now_epoch - max(timestamps) >= config.RUNNING_AGENT_GRACE_SECONDS:
                evicted.append(agent_id)
                running_agents.pop(agent_id, None)

        if evicted:
            self.set_running_agents(running_agents)
        return evicted

    def evict_expired_running_agents(self) -> list:
        """Evict expired running_agents entries and persist the result.

        Public entry point for :meth:`_evict_expired_running_agents`; ``_write()``
        already runs the private check inline on every main-window write, so this is
        for callers (e.g. handler_subagent_stop.py) that want an explicit, immediately
        persisted eviction pass.
        """
        evicted = self._evict_expired_running_agents()
        if evicted:
            self._write(self._cache)
        return evicted

    def record_own_agent_id(self, agent_id: str, model: str | None = None) -> None:
        """Tag this ledger's own ``agent_id`` field with the sub-agent it belongs to.

        ``model`` -- when the SubagentStart payload carried one -- is recorded
        alongside as ``agent_model`` so the sub-agent's own ledger file names the
        model it ran on.
        """
        if not agent_id:
            return
        data = self.load()
        data["agent_id"] = agent_id
        if model:
            data["agent_model"] = model
        self._write(data)

    def record_detected_run(self, run_id: str) -> None:
        """Record an in-flight detected run_id in this ledger."""
        if not run_id:
            return

        data = self.load()
        if not data.get("detected_run_id"):
            data["detected_run_id"] = run_id
            data["detected_run_id_at"] = _now_iso()
            self._write(data)

    def record_file_change(self, file_path: str, line_range: list | None = None,
                          is_creation: bool | None = None) -> None:
        """Record a file creation or modification in file_stats.

        Only records files within PROJECT_ROOT to keep evidence log scoped to actual project work.

        Retention (FILE_STATS_RETENTION_SECONDS) is enforced by _write()'s
        _prune_stale_file_stats() sweep, not here -- that sweep covers every tracked
        path on every write (bash/edit/write alike), not just this one.

        Args:
            file_path: The file written or edited.
            line_range: Optional [start, end] for modifications.
            is_creation: Force created (True) / modified (False); None auto-detects.
        """
        from pathlib import Path  # pylint: disable=import-outside-toplevel
        import src.config as cfg  # pylint: disable=import-outside-toplevel

        try:
            proj_root = Path(cfg.PROJECT_ROOT).resolve()
            if not Path(file_path).resolve().is_relative_to(proj_root):
                return
        except (ValueError, OSError):
            return

        is_new = not Path(file_path).exists() if is_creation is None else is_creation
        file_stats = self.get_file_stats()
        created = file_stats.get("files_created", {})
        modified = file_stats.get("files_modified", {})

        if is_new:
            created.setdefault(file_path, {})[_now_iso()] = []
        elif file_path not in created:
            modified.setdefault(file_path, {})[_now_iso()] = line_range or []

        self.set_file_stats(file_stats)
        self._cache["last_file_change_at"] = _now_iso()
        self._write(self._cache)

    def update_last_prompt_at(self) -> None:
        """Update last_prompt_at to now."""
        self._cache["last_prompt_at"] = _now_iso()
        self._write(self._cache)

    def update_last_command_at(self) -> None:
        """Update last_command_at to now."""
        self._cache["last_command_at"] = _now_iso()
        self._write(self._cache)

    def reset_file_stats(self) -> None:
        """Reset file_stats and bash_invocations to empty (called by Stop hook on constraint success).

        Agent ledgers preserve all file_stats and bash_invocations as whole-run evidence,
        so this is a no-op for them.
        """
        # Only reset on main session ledger; agent ledgers are whole-run evidence records
        if self._is_agent_ledger():
            return
        self.set_file_stats({"files_created": {}, "files_modified": {}})
        self.set_bash_invocations({})
        self._write(self._cache)

    def record_check_result(self, exit_code: int) -> None:
        """Persist the outcome of the most recent check_constraints() run.

        The Stop hook's has_changes gate needs this: a known-FAILING result must not
        be silently waved through just because no further edits happened since.
        """
        self._cache["last_check_result"] = {
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "checked_at": _now_iso(),
        }
        self._write(self._cache)


_RETENTION_README_TEXT = """# Ledger Retention Policy

Files under this directory (`agents/`) are ephemeral bookkeeping state,
not a permanent record. `sweep_stale_ledger_files()` deletes ledger files whose
mtime exceeds the retention threshold (default 6 hours). Deletion is safe: these
ledgers are derived/transient state, never source of truth.
"""

def sweep_stale_ledger_files(max_age_seconds: float = 6 * 3600.0,
                             doc: Optional[dict] = None) -> list[str]:
    """Delete files under ``CHANGES_TRACKER_DIR/agents/`` whose mtime
    is older than ``max_age_seconds`` (default 6 hours).

    A GC backstop against orphaned per-agent ledger files whose owning run never
    signaled completion cleanly. mtime age is the ONLY signal here.

    ``doc`` is accepted but unused: it existed only to gate deletion on an
    undrained 'commit' interaction, a mechanism retired alongside the 'commit'
    InteractionKind. Kept as a parameter so existing callers (hooks/handler_subagent_start.py,
    hooks/handler_subagent_stop.py) don't need updating.

    Args:
        max_age_seconds: The ledger age threshold in seconds (default 6 hours).
        doc: Unused; kept for caller compatibility.

    Returns:
        list[str]: filenames actually removed (for logging/diagnostics).
    """
    removed: list[str] = []
    try:
        base_dir = config.CHANGES_TRACKER_DIR
        if not base_dir.exists():
            return removed
        cutoff = time.time() - max_age_seconds
        for subdir_name in ("agents",):
            subdir = base_dir / subdir_name
            if not subdir.exists():
                continue
            for path in subdir.iterdir():
                try:
                    if not path.is_file():
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed.append(f"{subdir_name}/{path.name}")
                except OSError:
                    continue
    except OSError:
        return removed

    return removed


__all__ = [
    # Ledger maintenance (GC)
    "sweep_stale_ledger_files",
]
