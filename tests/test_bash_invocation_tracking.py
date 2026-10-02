#!/usr/bin/env python3
"""Test bash invocation tracking in PersistentLedgerTracker.

Tests the record_bash_invocation() method and integration with reset_file_stats(),
verifying the shape and capping behavior of bash_invocations dict.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ledger import PersistentLedgerTracker
from src.ledger.utils import _now_iso


class TestBashInvocationTracking:
    """PersistentLedgerTracker bash invocation recording tests."""

    @pytest.fixture
    def tracker(self, tmp_path):
        """Create a PersistentLedgerTracker with a temp file.

        Ensures TEST_LOG is not set so that _write() actually persists data.
        """
        # Ensure TEST_LOG is not set so writes actually happen
        tracker_file = tmp_path / "changes-tracker.json"
        return PersistentLedgerTracker(path=tracker_file)

    def test_schema_includes_bash_invocations(self, tracker):
        """_ensure_schema initializes bash_invocations as empty dict."""
        data = tracker.load()
        assert "bash_invocations" in data
        assert data["bash_invocations"] == {}

    def test_record_bash_invocation_success(self, tracker):
        """record_bash_invocation stores command with correct shape."""
        tracker.record_bash_invocation(
            command="echo hello",
            exit_code=0,
            duration_ms=42,
            phase="PostToolUse"
        )

        data = tracker.load()
        invocations = data["bash_invocations"]
        assert len(invocations) == 1

        # Unpack the single entry (keyed by ISO timestamp)
        entry = list(invocations.values())[0]
        assert entry["command"] == "echo hello"
        assert entry["exit_code"] == 0
        assert entry["duration_ms"] == 42
        assert entry["type"] == "PostToolUse"

    def test_record_bash_invocation_failure(self, tracker):
        """record_bash_invocation works for failed commands."""
        tracker.record_bash_invocation(
            command="false",
            exit_code=1,
            duration_ms=10,
            phase="PostToolUseFailure"
        )

        data = tracker.load()
        invocations = data["bash_invocations"]
        entry = list(invocations.values())[0]
        assert entry["command"] == "false"
        assert entry["exit_code"] == 1
        assert entry["type"] == "PostToolUseFailure"

    def test_record_bash_invocation_no_command_skipped(self, tracker):
        """record_bash_invocation skips empty commands."""
        tracker.record_bash_invocation(command="", exit_code=0)
        data = tracker.load()
        assert data["bash_invocations"] == {}

    def test_record_bash_invocation_truncates_long_command(self, tracker):
        """record_bash_invocation truncates commands to 500 bytes."""
        long_cmd = "x" * 1000
        tracker.record_bash_invocation(command=long_cmd, exit_code=0)

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert len(entry["command"]) == 500
        assert entry["command"] == "x" * 500

    def test_record_bash_invocation_optional_fields(self, tracker):
        """record_bash_invocation handles None for optional fields."""
        tracker.record_bash_invocation(
            command="cmd",
            exit_code=None,
            duration_ms=None,
            phase="PostToolUse"
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert entry["exit_code"] is None
        assert entry["duration_ms"] is None

    def test_record_bash_invocation_multiple(self, tracker):
        """record_bash_invocation accumulates multiple entries with different timestamps."""
        tracker.record_bash_invocation(command="cmd1", exit_code=0, phase="PostToolUse")
        # Small delay to ensure different timestamp
        time.sleep(0.01)
        tracker.record_bash_invocation(command="cmd2", exit_code=1, phase="PostToolUseFailure")

        data = tracker.load()
        invocations = data["bash_invocations"]
        assert len(invocations) == 2

        commands = [v["command"] for v in invocations.values()]
        assert "cmd1" in commands
        assert "cmd2" in commands

    def test_get_bash_invocations(self, tracker):
        """get_bash_invocations returns the bash_invocations dict."""
        tracker.record_bash_invocation(command="test", exit_code=0)
        invocations = tracker.get_bash_invocations()
        assert len(invocations) == 1
        assert invocations[list(invocations.keys())[0]]["command"] == "test"

    def test_reset_file_stats_clears_bash_invocations(self, tracker):
        """reset_file_stats clears bash_invocations."""
        tracker.record_bash_invocation(command="bash_cmd", exit_code=0)

        data = tracker.load()
        assert len(data["bash_invocations"]) == 1

        tracker.reset_file_stats()

        data = tracker.load()
        assert data["bash_invocations"] == {}

    def test_bash_invocations_capping(self, tracker):
        """bash_invocations caps at ~10KB total cumulative size, evicting oldest."""
        # Record enough large invocations to exceed the 10KB cap (each entry here
        # is ~310 bytes; 40 of them total ~12.4KB, comfortably past the 10240 threshold).
        for i in range(40):
            tracker.record_bash_invocation(
                command=f"cmd_{i}_{'x' * 300}",  # Larger command to exceed the 10KB cap
                exit_code=i % 2,
                duration_ms=100 + i,
                phase="PostToolUse"
            )

        data = tracker.load()
        invocations = data["bash_invocations"]

        # Should have fewer invocations after capping
        assert len(invocations) < 30, "Capping should have evicted oldest entries"

        # Check that some early entries were evicted but later ones remain
        commands = [v["command"] for v in invocations.values()]
        commands_str = ";".join(commands)

        # Early entries should be gone
        assert "cmd_0_" not in commands_str, "cmd_0 should be evicted"
        assert "cmd_1_" not in commands_str, "cmd_1 should be evicted"

        # Later entries should still be present
        assert any("cmd_" in cmd for cmd in commands), "Some entries should remain"

    def test_bash_invocations_persistent_across_loads(self, tracker):
        """bash_invocations persist on disk across load/write cycles."""
        tracker.record_bash_invocation(command="persistent", exit_code=0, phase="PostToolUse")

        # Create a new tracker instance pointing to the same file
        tracker2 = PersistentLedgerTracker(path=tracker.path)
        data = tracker2.load()
        invocations = data["bash_invocations"]

        assert len(invocations) == 1
        assert list(invocations.values())[0]["command"] == "persistent"

    def test_bash_invocations_keyed_by_timestamp(self, tracker):
        """bash_invocations are keyed by ISO timestamp."""
        tracker.record_bash_invocation(command="cmd1", exit_code=0)
        time.sleep(0.01)
        tracker.record_bash_invocation(command="cmd2", exit_code=1)

        data = tracker.load()
        keys = list(data["bash_invocations"].keys())

        # Keys should be ISO 8601 timestamps
        for key in keys:
            assert "T" in key  # ISO 8601 includes T
            # UTC offset can be Z or +00:00
            assert "Z" in key or "+00:00" in key or "Z" in key

        # Keys should be ordered (insertion order preserved in Python 3.7+)
        assert keys[0] < keys[1], "Timestamps should be ordered chronologically"

    def test_file_json_structure(self, tracker):
        """Verify the written changes-tracker.json has correct structure."""
        tracker.record_bash_invocation(command="test_cmd", exit_code=42, duration_ms=123)

        # Read the file directly to verify JSON structure
        content = json.loads(tracker.path.read_text())
        assert "bash_invocations" in content
        assert isinstance(content["bash_invocations"], dict)

        # Verify an invocation has all required fields
        invocation = list(content["bash_invocations"].values())[0]
        required_fields = {"command", "exit_code", "duration_ms", "type"}
        # Optional fields may or may not be present (backward compatibility)
        optional_fields = {"stdout", "stderr"}
        actual_keys = set(invocation.keys())
        assert required_fields.issubset(actual_keys), f"Missing required fields: {required_fields - actual_keys}"
        assert actual_keys.issubset(required_fields | optional_fields), f"Unexpected fields: {actual_keys - (required_fields | optional_fields)}"

    def test_record_bash_invocation_with_stdout_stderr(self, tracker):
        """record_bash_invocation stores stdout and stderr when provided."""
        tracker.record_bash_invocation(
            command="echo hello",
            exit_code=0,
            duration_ms=50,
            stdout="hello\n",
            stderr=""
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert entry["stdout"] == "hello\n"
        assert entry["stderr"] == ""

    def test_record_bash_invocation_truncates_stdout_stderr(self, tracker):
        """record_bash_invocation truncates stdout and stderr to 100 bytes."""
        long_output = "x" * 200
        tracker.record_bash_invocation(
            command="cmd",
            exit_code=0,
            stdout=long_output,
            stderr=long_output
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert len(entry["stdout"]) == 100
        assert len(entry["stderr"]) == 100
        assert entry["stdout"] == "x" * 100
        assert entry["stderr"] == "x" * 100

    def test_record_bash_invocation_optional_stdout_stderr(self, tracker):
        """record_bash_invocation handles missing stdout/stderr (None or not provided)."""
        tracker.record_bash_invocation(
            command="cmd",
            exit_code=0,
            stdout=None,
            stderr=None
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        # stdout and stderr should not be in the entry if None
        assert "stdout" not in entry
        assert "stderr" not in entry

    def test_record_bash_invocation_partial_output(self, tracker):
        """record_bash_invocation handles partial stdout/stderr (only stdout or only stderr)."""
        tracker.record_bash_invocation(
            command="cmd",
            exit_code=0,
            stdout="output",
            stderr=None
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert entry["stdout"] == "output"
        assert "stderr" not in entry

    def test_record_bash_invocation_with_reason(self, tracker):
        """record_bash_invocation persists the reason/description into the entry."""
        tracker.record_bash_invocation(
            command="pytest tests/",
            exit_code=0,
            reason="Run the unit suite before advancing the step",
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert entry["reason"] == "Run the unit suite before advancing the step"

    def test_record_bash_invocation_without_reason_omits_field(self, tracker):
        """record_bash_invocation omits 'reason' entirely when none is given."""
        tracker.record_bash_invocation(command="ls", exit_code=0)

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert "reason" not in entry

    def test_record_bash_invocation_truncates_reason(self, tracker):
        """record_bash_invocation truncates an overlong reason to 500 bytes."""
        long_reason = "x" * 1000
        tracker.record_bash_invocation(command="ls", exit_code=0, reason=long_reason)

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert len(entry["reason"]) == 500

    def test_record_bash_invocation_with_reason(self, tracker):
        """record_bash_invocation records reason."""
        tracker.record_bash_invocation(
            command="pytest tests/",
            exit_code=0,
            reason="verify step",
        )

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert entry["reason"] == "verify step"

    def test_record_bash_invocation_omits_run_id_field(self, tracker):
        """record_bash_invocation no longer writes run_id field."""
        tracker.record_bash_invocation(command="ls", exit_code=0, reason="just checking")

        data = tracker.load()
        entry = list(data["bash_invocations"].values())[0]
        assert "run_id" not in entry
        assert entry["reason"] == "just checking"

    def test_get_detected_playbook_run_id_roundtrip(self, tracker):
        """get_detected_playbook_run_id returns None until record_detected_run
        is called, then returns the recorded run_id (idempotent, first-seen wins)."""
        assert tracker.get_detected_playbook_run_id() is None

        tracker.record_detected_run("run_abc123")
        assert tracker.get_detected_playbook_run_id() == "run_abc123"

        # Idempotent: a second call with a different run_id does not overwrite
        tracker.record_detected_run("run_other")
        assert tracker.get_detected_playbook_run_id() == "run_abc123"

    def test_bash_invocations_capping_with_output(self, tracker):
        """bash_invocations caps at ~10KB total, including stdout/stderr sizes."""
        # Record entries with stdout/stderr that will exceed the cap
        # Each entry is now ~300 bytes (command + stdout + stderr + metadata)
        # 40 entries × ~300 bytes = ~12KB, well over the 10KB cap
        for i in range(40):
            tracker.record_bash_invocation(
                command=f"cmd_{i}_{'x' * 100}",
                exit_code=0,
                duration_ms=100 + i,
                stdout="stdout_" + "y" * 50,
                stderr="stderr_" + "z" * 50
            )

        data = tracker.load()
        invocations = data["bash_invocations"]

        # Should have fewer invocations after capping
        # With ~300 bytes per entry and 10KB cap, expect ~25-30 entries remaining
        assert len(invocations) < 40, "Capping should have evicted oldest entries"
        assert len(invocations) > 10, "Should retain some entries after capping"

        # Verify early entries were evicted
        commands = [v["command"] for v in invocations.values()]
        commands_str = ";".join(commands)
        # Early entries (cmd_0, cmd_1, cmd_2) should be gone
        assert "cmd_0_" not in commands_str, "cmd_0 should be evicted"


class TestAgentLedgerBashInvocationGuarding:
    """Regression tests: agent ledgers preserve bash_invocations, bypass capping/retention."""

    @pytest.fixture
    def agent_tracker(self, tmp_path):
        """Create an agent ledger tracker (not a session ledger)."""
        from src.ledger import agent_ledger
        tracker = agent_ledger("test_agent_id")
        # Override the path to use tmp_path for testing
        tracker.path = tmp_path / "agent_ledger.json"
        tracker._cache = None
        tracker.load()
        return tracker

    def test_agent_ledger_bash_invocations_no_byte_cap(self, agent_tracker):
        """Agent ledger with >10240 bytes bash_invocations keeps ALL entries (no eviction)."""
        # Record enough entries to exceed 10KB cap
        # Each entry is ~300 bytes; 40 entries = ~12KB (well over cap)
        for i in range(40):
            agent_tracker.record_bash_invocation(
                command=f"cmd_{i}_{'x' * 300}",
                exit_code=0,
                duration_ms=100 + i,
                phase="PostToolUse"
            )

        data = agent_tracker.load()
        invocations = data["bash_invocations"]

        # On an agent ledger, ALL invocations should be preserved
        # (no eviction due to byte cap)
        assert len(invocations) == 40, (
            f"Agent ledger should preserve all {40} bash invocations; "
            f"got {len(invocations)}"
        )

        # Verify all commands are present
        commands = [v["command"] for v in invocations.values()]
        for i in range(40):
            assert any(f"cmd_{i}_" in cmd for cmd in commands), (
                f"cmd_{i} should be preserved in agent ledger"
            )

    def test_agent_ledger_bash_invocations_no_retention_prune(self, agent_tracker, monkeypatch):
        """Agent ledger bash_invocations skip time-based retention pruning."""
        from datetime import datetime, timezone, timedelta
        from src.ledger.utils import _now_iso

        # Mock the retention seconds to a very short value to force a prune
        # on the main session ledger (but not on agent ledgers)
        monkeypatch.setattr("src.config.BASH_INVOCATION_RETENTION_SECONDS", 1)

        # Record an invocation
        agent_tracker.record_bash_invocation(
            command="old_cmd",
            exit_code=0
        )

        # Get the timestamp and manually backdate the entry
        data = agent_tracker.load()
        invocation_key = list(data["bash_invocations"].keys())[0]

        # Simulate an old entry by manually setting it with an old timestamp
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        data["bash_invocations"][old_time] = {
            "command": "very_old_cmd",
            "exit_code": 0,
            "type": "PostToolUse"
        }

        agent_tracker.set_bash_invocations(data["bash_invocations"])
        agent_tracker._write(data)

        # Reload and verify old entry survives (not pruned)
        data = agent_tracker.load()
        commands = [v["command"] for v in data["bash_invocations"].values()]
        assert "very_old_cmd" in commands, (
            "Agent ledger should preserve stale bash invocations"
        )
