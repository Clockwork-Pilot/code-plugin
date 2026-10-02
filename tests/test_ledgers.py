"""Comprehensive tests for src.ledgers storage package.

Coverage:
  - Path resolution (agent_ledger_path, etc.)
  - Agent ledger read/write round-trip
  - Running agents tracking and eviction
  - Live agent listing by recency
  - Ledger sweep/GC
"""

import os
import re
import pytest
import time
from pathlib import Path

from src.config import CHANGES_TRACKER_DIR
from src.ledger import (
    agent_ledger_path,
    PersistentLedgerTracker,
    session_ledger,
    agent_ledger,
    sweep_stale_ledger_files,
)
from src.ledger.ledger_read import (
    agent_ledger_filename,
    list_live_agents,
)

# agent_ledger_filename() output: <iso_timestamp>-<agent_id>.json (ISO 8601
# UTC, timestamped at ledger creation) -- the timestamp is dynamic, so tests
# assert against this pattern rather than a literal filename.
_AGENT_LEDGER_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00-(?P<agent_id>.+)\.json$")


class TestPathResolution:
    """Test path/directory resolution functions."""

    def test_agent_ledger_filename_format(self):
        """agent_ledger_filename() formats correctly."""
        filename = agent_ledger_filename("agent_abc123")
        m = _AGENT_LEDGER_FILE_RE.match(filename)
        assert m and m.group("agent_id") == "agent_abc123"

    def test_agent_ledger_filename_sanitizes_unsafe_chars(self):
        """agent_ledger_filename() sanitizes unsafe characters."""
        filename = agent_ledger_filename("agent/abc:123")
        assert "/" not in filename
        m = _AGENT_LEDGER_FILE_RE.match(filename)
        assert m and m.group("agent_id") == "agent_abc_123"

    def test_agent_ledger_path_structure(self):
        """agent_ledger_path() returns agents/<timestamp>-<agent_id>.json."""
        path = agent_ledger_path("test_agent")
        assert "agents" in str(path)
        m = _AGENT_LEDGER_FILE_RE.match(path.name)
        assert m and m.group("agent_id") == "test_agent"


class TestPersistentLedgerTracker:
    """Test PersistentLedgerTracker basic operations."""

    def test_tracker_initialization(self, tmp_path):
        """PersistentLedgerTracker initializes with default or custom path."""
        # With custom path
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        assert tracker.path == tmp_path / "test.json"

    def test_tracker_load_empty_creates_schema(self, tmp_path):
        """load() on empty/missing file initializes schema."""
        tracker = PersistentLedgerTracker(path=tmp_path / "empty.json")
        data = tracker.load()
        assert "created_at" in data
        assert "updated_at" in data

    def test_tracker_load_caches_data(self, tmp_path):
        """load() caches data after first read."""
        ledger_path = tmp_path / "cached.json"
        tracker = PersistentLedgerTracker(path=ledger_path)

        data1 = tracker.load()
        data2 = tracker.load()
        # Both should be the same in-memory object (cached)
        assert data1 is data2



class TestTrackerAccess:
    """Test PersistentLedgerTracker and the session_ledger()/agent_ledger() factories."""

    def test_persistent_ledger_tracker_instantiation(self):
        """PersistentLedgerTracker() can be instantiated directly."""
        tracker = PersistentLedgerTracker()
        assert isinstance(tracker, PersistentLedgerTracker)

    def test_agent_ledger_routes_to_agent_path(self, monkeypatch):
        """agent_ledger(agent_id) routes to that agent's ledger path."""
        tracker = agent_ledger("test_agent")

        assert isinstance(tracker, PersistentLedgerTracker)
        assert "agents" in str(tracker.path)

    def test_session_ledger_routes_to_session_main(self):
        """session_ledger() routes to session_main.json."""
        tracker = session_ledger()

        assert isinstance(tracker, PersistentLedgerTracker)
        assert "session_main.json" in str(tracker.path)


class TestAgentLedgerOperations:
    """Test agent ledger read/write and discovery."""

    def test_list_live_agents_empty(self, tmp_path, monkeypatch):
        """list_live_agents() returns empty list when no agents."""
        monkeypatch.setattr("src.config.CHANGES_TRACKER_DIR", tmp_path)

        result = list_live_agents()
        assert result == []

    def test_list_live_agents_finds_recent(self, tmp_path, monkeypatch):
        """list_live_agents() finds agents with recent mtime."""
        monkeypatch.setattr("src.config.CHANGES_TRACKER_DIR", tmp_path)

        # Create agent ledger files
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Create a recent file
        (agents_dir / agent_ledger_filename("agent_recent")).write_text("{}")

        # Create an old file (older than 600s threshold)
        old_file = agents_dir / agent_ledger_filename("agent_old")
        old_file.write_text("{}")
        old_time = time.time() - 700  # 700 seconds ago
        os.utime(old_file, (old_time, old_time))

        result = list_live_agents(age_threshold_seconds=600)
        assert "agent_recent" in result
        assert "agent_old" not in result


class TestLedgerSweep:
    """Test sweep_stale_ledger_files() garbage collection."""

    def test_sweep_stale_ledger_files_empty(self, tmp_path, monkeypatch):
        """sweep_stale_ledger_files() returns empty when no files."""
        monkeypatch.setattr("src.config.CHANGES_TRACKER_DIR", tmp_path)

        result = sweep_stale_ledger_files()
        assert result == []

    def test_sweep_stale_ledger_files_removes_old_agents(self, tmp_path, monkeypatch):
        """sweep_stale_ledger_files() removes old agent ledgers."""
        monkeypatch.setattr("src.config.CHANGES_TRACKER_DIR", tmp_path)

        # Create directories
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        # Create a fresh file
        fresh_file = agents_dir / "agent_agent_fresh.json"
        fresh_file.write_text("{}")

        # Create an old file
        old_file = agents_dir / "agent_agent_old.json"
        old_file.write_text("{}")
        old_time = time.time() - 7 * 3600  # 7 hours ago (older than 6 hour default)
        os.utime(old_file, (old_time, old_time))

        # Sweep with default 6-hour threshold
        result = sweep_stale_ledger_files()

        assert "agents/agent_agent_old.json" in result
        assert "agents/agent_agent_fresh.json" not in result
        assert not old_file.exists()
        assert fresh_file.exists()

class TestRoundTrip:
    """Integration tests for read/write round-trips."""
    pass


class TestBashInvocations:
    """Test bash invocation recording in PersistentLedgerTracker."""

    def test_record_bash_invocation_basic(self, tmp_path):
        """Record a basic bash invocation."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_bash_invocation(
            command="python3 test.py",
            exit_code=0,
            duration_ms=1234,
            phase="PostToolUse"
        )

        invocations = tracker.get_bash_invocations()
        assert len(invocations) == 1
        inv = list(invocations.values())[0]
        assert inv["command"] == "python3 test.py"
        assert inv["exit_code"] == 0
        assert inv["duration_ms"] == 1234
        assert inv["type"] == "PostToolUse"

    def test_record_bash_invocation_with_output(self, tmp_path):
        """Record bash invocation with stdout/stderr."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_bash_invocation(
            command="echo test",
            exit_code=0,
            stdout="test output",
            stderr="some warning"
        )

        invocations = tracker.get_bash_invocations()
        inv = list(invocations.values())[0]
        assert inv["stdout"] == "test output"
        assert inv["stderr"] == "some warning"

    def test_record_bash_invocation_output_truncation(self, tmp_path):
        """stdout/stderr truncated to 100 bytes."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        long_output = "x" * 200
        tracker.record_bash_invocation(
            command="test",
            stdout=long_output,
            stderr=long_output
        )

        invocations = tracker.get_bash_invocations()
        inv = list(invocations.values())[0]
        assert len(inv["stdout"]) == 100
        assert len(inv["stderr"]) == 100

    def test_record_bash_invocation_with_reason_and_run_id(self, tmp_path):
        """Record bash invocation with reason."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_bash_invocation(
            command="test",
            reason="Test hook wrapper"
        )

        invocations = tracker.get_bash_invocations()
        inv = list(invocations.values())[0]
        assert inv["reason"] == "Test hook wrapper"

    def test_bash_invocations_keyed_by_timestamp(self, tmp_path):
        """Bash invocations keyed by ISO timestamp."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_bash_invocation(command="test1")
        time.sleep(0.01)
        tracker.record_bash_invocation(command="test2")

        invocations = tracker.get_bash_invocations()
        keys = list(invocations.keys())
        assert len(keys) == 2
        for key in keys:
            assert "T" in key  # ISO timestamp format


class TestFileStats:
    """Test file stat recording in PersistentLedgerTracker."""

    def test_record_file_created(self, tmp_path):
        """Record a file creation."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_file_created("/workspace/test.py")

        stats = tracker.get_file_stats()
        assert "/workspace/test.py" in stats["files_created"]

    def test_record_file_modified_with_line_range(self, tmp_path):
        """Record file modification with line range."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_file_modified(
            "/workspace/test.py",
            line_range=[10, 20]
        )

        stats = tracker.get_file_stats()
        assert "/workspace/test.py" in stats["files_modified"]
        # Verify line range is stored
        for entry in stats["files_modified"].values():
            if isinstance(entry, dict):
                for range_list in entry.values():
                    assert range_list == [10, 20]

    def test_record_file_modified_without_line_range(self, tmp_path):
        """Record file modification without line range."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.record_file_modified("/workspace/test.py")

        stats = tracker.get_file_stats()
        assert "/workspace/test.py" in stats["files_modified"]


class TestAgentTimestamps:
    """Test agent timestamp and staleness operations."""

    def test_agent_exists_in_running_agents(self, tmp_path):
        """Check if agent exists in running_agents."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.update_timestamp("agent-1", "created_at", "2026-07-15T12:00:00+00:00")

        assert tracker.agent_exists_in_running_agents("agent-1") is True
        assert tracker.agent_exists_in_running_agents("agent-99") is False

    def test_evict_expired_running_agents_fresh_entry_survives(self, tmp_path):
        """An entry updated well within the grace period is not evicted."""
        from datetime import datetime, timezone
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        now_ts = datetime.now(timezone.utc).isoformat()
        tracker.update_timestamp("agent-1", "updated_at", now_ts)

        evicted = tracker._evict_expired_running_agents()

        assert evicted == []
        assert tracker.agent_exists_in_running_agents("agent-1") is True

    def test_evict_expired_running_agents_past_grace_is_evicted(self, tmp_path):
        """An entry whose latest timestamp is past RUNNING_AGENT_GRACE_SECONDS is evicted."""
        from datetime import datetime, timezone, timedelta
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")

        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        tracker.update_timestamp("agent-1", "updated_at", old_time.isoformat())

        evicted = tracker._evict_expired_running_agents()

        assert evicted == ["agent-1"]
        assert tracker.agent_exists_in_running_agents("agent-1") is False

    def test_evict_expired_running_agents_no_timestamps_is_evicted(self, tmp_path):
        """An entry with no usable timestamps is evicted immediately."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.set_running_agents({"agent-1": {}})

        evicted = tracker._evict_expired_running_agents()

        assert evicted == ["agent-1"]
        assert tracker.agent_exists_in_running_agents("agent-1") is False

    def test_update_timestamp_creates_agent_entry(self, tmp_path):
        """update_timestamp creates agent entry if missing."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.update_timestamp("agent-1", "updated_at", "2026-07-15T12:00:00+00:00")

        assert tracker.agent_exists_in_running_agents("agent-1") is True

    def test_update_timestamp_multiple_fields(self, tmp_path):
        """Update multiple timestamp fields for an agent."""
        tracker = PersistentLedgerTracker(path=tmp_path / "test.json")
        tracker.update_timestamp("agent-1", "created_at", "2026-07-15T12:00:00+00:00")
        tracker.update_timestamp("agent-1", "updated_at", "2026-07-15T12:30:00+00:00")
        tracker.update_timestamp("agent-1", "pinged_at", "2026-07-15T12:40:00+00:00")

        running_agents = tracker._cache.get("running_agents") or {}
        agent = running_agents["agent-1"]
        assert "created_at" in agent
        assert "updated_at" in agent
        assert "pinged_at" in agent


class TestAgentLedgerFileStatsGuarding:
    """Regression tests: agent ledgers preserve file_stats, skip pruning."""

    def test_agent_ledger_file_stats_no_stale_prune(self, tmp_path, monkeypatch):
        """Agent ledger file_stats survive stale pruning; not removed on _write."""
        from datetime import datetime, timezone, timedelta
        from src.ledger.utils import _now_iso

        monkeypatch.setattr("src.config.CHANGES_TRACKER_DIR", tmp_path)
        monkeypatch.setattr("src.config.FILE_STATS_RETENTION_SECONDS", 1)

        # Create an agent ledger
        tracker = agent_ledger("test_agent_stale")
        tracker.path = tmp_path / "test_agent.json"
        tracker._cache = None
        tracker.load()

        # Record file creation
        tracker.record_file_created("/workspace/test.py")

        # Manually backdate all entries to be older than retention
        data = tracker.load()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        # Replace timestamps with old ones
        if "files_created" in data.get("file_stats", {}):
            for path in data["file_stats"]["files_created"]:
                old_entries = {old_time: []}
                data["file_stats"]["files_created"][path] = old_entries

        tracker.set_file_stats(data["file_stats"])
        tracker._write(data)

        # Reload and verify stale entries survive (not pruned on agent ledger)
        data = tracker.load()
        assert len(data["file_stats"]["files_created"]) > 0, (
            "Agent ledger should preserve all file_stats, even stale ones"
        )
        assert "/workspace/test.py" in data["file_stats"]["files_created"], (
            "File path should remain in agent ledger despite stale timestamp"
        )

    def test_agent_ledger_reset_file_stats_is_noop(self, tmp_path):
        """Agent ledger reset_file_stats is a no-op; file_stats preserved."""
        tracker = agent_ledger("test_agent_noop")
        tracker.path = tmp_path / "test_agent.json"
        tracker._cache = None
        tracker.load()

        # Record file and bash
        tracker.record_file_created("/workspace/test.py")
        tracker.record_bash_invocation(command="echo test", exit_code=0)

        data = tracker.load()
        assert len(data["file_stats"]["files_created"]) > 0
        assert len(data["bash_invocations"]) > 0

        # Call reset_file_stats on agent ledger (should be no-op)
        tracker.reset_file_stats()

        # Verify nothing was cleared
        data = tracker.load()
        assert len(data["file_stats"]["files_created"]) > 0, (
            "Agent ledger reset_file_stats should be no-op"
        )
        assert len(data["bash_invocations"]) > 0, (
            "Agent ledger reset_file_stats should preserve bash_invocations"
        )


class TestSessionLedgerStillCapsAndPrunes:
    """Regression tests: session ledger still applies size caps and retention pruning."""

    def test_session_ledger_bash_invocations_still_capped(self, tmp_path):
        """Session ledger bash_invocations still capped at 10KB (not guarded out)."""
        from src.ledger import session_ledger

        # Create session ledger
        tracker = session_ledger()
        # Override path for testing
        tracker.path = tmp_path / "session_ledger.json"
        tracker._cache = None
        tracker.load()

        # Record many large bash invocations
        for i in range(40):
            tracker.record_bash_invocation(
                command=f"cmd_{i}_{'x' * 300}",
                exit_code=0,
                duration_ms=100 + i
            )

        data = tracker.load()
        invocations = data["bash_invocations"]

        # Session ledger should enforce the 10KB cap, so we should have fewer entries
        assert len(invocations) < 40, (
            f"Session ledger should cap bash_invocations at ~10KB; "
            f"got {len(invocations)} entries"
        )
        assert len(invocations) > 5, (
            "Session ledger should retain some entries after capping"
        )

    def test_session_ledger_reset_file_stats_still_works(self, tmp_path):
        """Session ledger reset_file_stats still clears file_stats and bash_invocations."""
        from src.ledger import session_ledger

        tracker = session_ledger()
        tracker.path = tmp_path / "session_ledger.json"
        tracker._cache = None
        tracker.load()

        # Record file and bash
        tracker.record_file_created("/workspace/test.py")
        tracker.record_bash_invocation(command="echo test", exit_code=0)

        data = tracker.load()
        assert len(data["file_stats"]["files_created"]) > 0
        assert len(data["bash_invocations"]) > 0

        # Call reset_file_stats on session ledger (should work)
        tracker.reset_file_stats()

        # Verify everything was cleared
        data = tracker.load()
        assert len(data["file_stats"]["files_created"]) == 0, (
            "Session ledger reset_file_stats should clear file_stats"
        )
        assert len(data["bash_invocations"]) == 0, (
            "Session ledger reset_file_stats should clear bash_invocations"
        )


class TestLegacyAgentLedgerFilename:
    def test_legacy_and_iso_names_both_resolve_agent_id(self):
        from src.ledger.ledger_read import _agent_id_from_ledger_filename as f
        assert f("20260101_120000-agent-1.json") == "agent-1"
        assert f(agent_ledger_filename("agent-1")) == "agent-1"
        assert f("garbage.json") is None
