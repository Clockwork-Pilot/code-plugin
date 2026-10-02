#!/usr/bin/env python3
"""Regression tests for the per-agent changes-tracker ledger layout.

Design: dc_per_agent_changes_tracker_ledgers_replace_the_dd729be7 -- replaces the
single PROJECT_ROOT/changes-tracker.json file with PROJECT_ROOT/.changes-tracker/,
one ledger file per sub-agent (or one fixed file for the main window). The
internal JSON shape of each ledger file is unchanged; only the storage layout (one
file -> many, resolved per hook payload) changed.

Covers:
  - agent_ledger(agent_id)/session_ledger() routing (agent_id present/absent,
    fallback when hook_input is missing/empty).
  - Two concurrent agent_ids writing never comingle (acceptance criterion 4).
  - A main-window write and a sub-agent write land in two
    distinct files (acceptance criterion 5).
  - get_persistent_tracker()'s zero-arg default keeps resolving to a stable
    main-window fallback file (so the 9 out-of-scope call sites don't misroute).
  - Each resolved ledger's internal schema is unchanged from the old single-file
    shape (acceptance criterion 1).
"""

import re
from pathlib import Path

import pytest

import src.config as _config

# agent_ledger_filename() output: <iso_timestamp>-<agent_id>.json (ISO 8601
# UTC, timestamped at ledger creation) -- the timestamp is dynamic, so tests
# assert against this pattern rather than a literal filename.
_AGENT_LEDGER_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00-(?P<agent_id>.+)\.json$")


def _assert_agent_ledger_path(path: Path, ledger_dir: Path, agent_id: str) -> None:
    assert path.parent == ledger_dir / "agents"
    m = _AGENT_LEDGER_FILE_RE.match(path.name)
    assert m, f"{path.name!r} doesn't match <timestamp>-<agent_id>.json"
    assert m.group("agent_id") == agent_id
from src.ledger import (
    PersistentLedgerTracker,
    session_ledger,
    agent_ledger,
    tracker_for_hook,
)
from src.ledger import agent_ledger_path
from src.ledger.ledger_read import (
    agent_ledger_filename,
    main_ledger_path,
)
from src.config import CHANGES_TRACKER_DIR


class TestLedgerFilenames:
    """Pure filename-building helpers."""

    def test_agent_filename_without_run_id(self):
        m = _AGENT_LEDGER_FILE_RE.match(agent_ledger_filename("agent-123"))
        assert m and m.group("agent_id") == "agent-123"


class TestLedgerRouting:
    """agent_ledger(agent_id)/session_ledger() routing."""

    def test_agent_id_present_routes_to_agent_file(self, _isolated_ledger_dir):
        tracker = agent_ledger("agent-A")
        _assert_agent_ledger_path(tracker.path, _isolated_ledger_dir, "agent-A")

    def test_agent_id_absent_routes_to_main(self, _isolated_ledger_dir):
        """Acceptance criterion 4: no agent_id routes to main window."""
        tracker = session_ledger()
        assert tracker.path == _isolated_ledger_dir / "session_main.json"

    def test_main_window_converges_with_zero_arg_default(self, _isolated_ledger_dir):
        """Regression test: session_ledger() resolves to the same path
        as PersistentLedgerTracker()."""
        main_tracker = session_ledger()
        zero_arg_tracker = PersistentLedgerTracker()
        assert main_tracker.path == zero_arg_tracker.path
        assert main_tracker.path == _isolated_ledger_dir / "session_main.json"

    def test_agent_id_routes_to_agent_file(self, _isolated_ledger_dir):
        """Acceptance criterion 5: agent_id present routes to agent's own file."""
        tracker = agent_ledger("agent-X")
        _assert_agent_ledger_path(tracker.path, _isolated_ledger_dir, "agent-X")

    def test_zero_arg_falls_back_to_main_window(self, _isolated_ledger_dir):
        assert session_ledger().path == _isolated_ledger_dir / "session_main.json"

    def test_none_hook_input_falls_back_to_main_window(self, _isolated_ledger_dir):
        # session_ledger() routes to main window (same as zero-arg test)
        assert session_ledger().path == _isolated_ledger_dir / "session_main.json"


class TestTrackerForHookOrphanGuard:
    """tracker_for_hook() routes to an agent ledger ONLY for a known sub-agent.

    Regression: an Edit/Write hook that received a transient agent_id the harness
    never announced (no SubagentStart, no running_agents entry) used to mint a
    fresh agents/<ts>-<id>.json whose body carried agent_id: null -- an orphan
    that duplicated an entry already recorded in the real agent's ledger.
    """

    def test_unknown_agent_id_routes_to_session_and_creates_no_agent_file(
        self, _isolated_ledger_dir
    ):
        tracker = tracker_for_hook("phantom-never-announced")
        tracker.record_file_change("/tmp/x.py", [1, 2], is_creation=False)

        assert tracker.path == _isolated_ledger_dir / "session_main.json"
        assert not list(_isolated_ledger_dir.glob("agents/*.json"))

    def test_agent_id_with_running_agents_entry_routes_to_agent_file(
        self, _isolated_ledger_dir
    ):
        session_ledger().record_agent_start("agent-known")

        tracker = tracker_for_hook("agent-known")

        _assert_agent_ledger_path(tracker.path, _isolated_ledger_dir, "agent-known")

    def test_agent_id_with_existing_ledger_file_routes_to_agent_file(
        self, _isolated_ledger_dir
    ):
        # A prior hook already created this agent's ledger file.
        agent_ledger("agent-seen").record_own_agent_id("agent-seen")

        tracker = tracker_for_hook("agent-seen")

        _assert_agent_ledger_path(tracker.path, _isolated_ledger_dir, "agent-seen")

    def test_absent_agent_id_routes_to_session(self, _isolated_ledger_dir):
        assert tracker_for_hook(None).path == _isolated_ledger_dir / "session_main.json"
        assert tracker_for_hook("").path == _isolated_ledger_dir / "session_main.json"


class TestConcurrentAgentLedgers:
    """Acceptance criteria 4 & 5: distinct writers never comingle files."""

    def test_two_concurrent_agent_ids_produce_two_distinct_files(self, _isolated_ledger_dir):
        """Two sub-agents in the SAME session write to two separate ledger files."""
        tracker_a = agent_ledger("agent-A")
        tracker_b = agent_ledger("agent-B")

        assert tracker_a.path != tracker_b.path

        tracker_a.record_bash_invocation(command="cmd-from-a", exit_code=1)
        tracker_b.record_bash_invocation(command="cmd-from-b", exit_code=1)

        assert tracker_a.path.exists()
        assert tracker_b.path.exists()

        data_a = tracker_a.load()
        data_b = tracker_b.load()

        # Each ledger only sees its own agent's write -- never comingled.
        commands_a = [v["command"] for v in data_a["bash_invocations"].values()]
        commands_b = [v["command"] for v in data_b["bash_invocations"].values()]
        assert commands_a == ["cmd-from-a"]
        assert commands_b == ["cmd-from-b"]

        # Exactly two ledger files exist on disk for this session (in agents/ subdirectory).
        ledger_files = sorted(p.name for p in _isolated_ledger_dir.glob("agents/*.json"))
        assert len(ledger_files) == 2
        agent_ids = {_AGENT_LEDGER_FILE_RE.match(f).group("agent_id") for f in ledger_files}
        assert agent_ids == {"agent-A", "agent-B"}

    def test_main_window_and_subagent_write_land_in_distinct_files(self, _isolated_ledger_dir):
        """A main-window (no agent_id) write and a sub-agent write in the same session
        never land in the same file. Main-window always uses 'session_main.json',
        while sub-agent uses its own file in agents/ subdirectory."""
        main_tracker = session_ledger()
        agent_tracker = agent_ledger("agent-Z")

        assert main_tracker.path != agent_tracker.path

        main_tracker.record_bash_invocation(command="main-cmd", exit_code=1)
        agent_tracker.record_bash_invocation(command="agent-cmd", exit_code=1)

        main_data = main_tracker.load()
        agent_data = agent_tracker.load()

        assert [v["command"] for v in main_data["bash_invocations"].values()] == ["main-cmd"]
        assert [v["command"] for v in agent_data["bash_invocations"].values()] == ["agent-cmd"]

        # Check that main window file exists at root level and agent file in agents/ subdirectory
        assert (_isolated_ledger_dir / "session_main.json").exists()
        assert agent_tracker.path.exists()
        _assert_agent_ledger_path(agent_tracker.path, _isolated_ledger_dir, "agent-Z")


class TestZeroArgDefaultStaysStable:
    """PersistentLedgerTracker() (no hook payload) resolves to a fixed main-window fallback file."""

    def test_zero_arg_resolves_to_main_window_fallback(self, _isolated_ledger_dir):
        tracker = PersistentLedgerTracker()
        assert tracker.path == _isolated_ledger_dir / "session_main.json"

    def test_zero_arg_matches_hook_routed_none(self, _isolated_ledger_dir):
        """Zero-arg and session_ledger() resolve to the SAME file."""
        zero_arg_tracker = PersistentLedgerTracker()
        hook_routed_tracker = session_ledger()
        assert zero_arg_tracker.path == hook_routed_tracker.path

    def test_zero_arg_returns_fresh_instance_each_call(self, _isolated_ledger_dir):
        """Deliberately NOT a singleton (no module-level global) -- each call
        constructs a fresh PersistentLedgerTracker(). Safe because the class holds
        no in-memory state beyond self.path/self._cache (self.path is cheap and
        deterministic; self._cache is per-instance and every read/write ultimately
        goes through the same underlying ledger file), so two instances pointed at
        the same path are interchangeable."""
        a, b = PersistentLedgerTracker(), PersistentLedgerTracker()
        assert a is not b
        assert a.path == b.path


class TestLedgerSchemaUnchanged:
    """Acceptance criterion 1: internal JSON shape of each ledger file is unchanged
    from today's single changes-tracker.json structure -- only storage layout moved."""

    def test_resolved_agent_ledger_has_full_schema(self, _isolated_ledger_dir):
        tracker = agent_ledger("agent-A")
        tracker.record_bash_invocation(command="cmd", exit_code=1)  # force a write
        data = tracker.load()

        for key in (
            "file_stats", "bash_invocations", "running_agents",
            "agent_id", "last_prompt_at", "last_command_at", "last_file_change_at",
            "last_check_result", "created_at", "updated_at",
        ):
            assert key in data

        assert data["file_stats"] == {"files_created": {}, "files_modified": {}}

    def test_agent_id_defaults_to_none(self, _isolated_ledger_dir):
        """Acceptance criterion 1: agent_id defaults to None until explicitly tagged."""
        tracker = session_ledger()
        tracker.record_bash_invocation(command="cmd", exit_code=1)  # force a write
        data = tracker.load()
        assert data["agent_id"] is None

    def test_ledger_directory_created_on_first_write(self, _isolated_ledger_dir):
        # _isolated_ledger_dir itself is pre-created by tmp_path_factory.mktemp() (every
        # other test in this suite relies on that), so this test points config at a
        # not-yet-created child path instead -- direct save/restore, matching this
        # codebase's established convention for scoped config overrides.
        fresh_dir = _isolated_ledger_dir / "not_yet_created"
        assert not fresh_dir.exists()
        original = _config.CHANGES_TRACKER_DIR
        _config.CHANGES_TRACKER_DIR = fresh_dir
        try:
            tracker = agent_ledger("agent-A")
            tracker.record_bash_invocation(command="cmd", exit_code=1)
            assert fresh_dir.is_dir()
            # Agent files are in agents/ subdirectory, so parent is agents/
            assert tracker.path.parent == fresh_dir / "agents"
        finally:
            _config.CHANGES_TRACKER_DIR = original
