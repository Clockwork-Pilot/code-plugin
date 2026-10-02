#!/usr/bin/env python3
"""PersistentLedgerTracker records each edit as its own timestamped entry --
{path: {iso_timestamp: value}} -- instead of merging ranges at write time,
so a reader can later window entries by time (e.g. only those after a given
PlaybookRun's start) rather than attributing a file's whole accumulated history
unconditionally.
"""

import json

import pytest

from src.ledger import PersistentLedgerTracker


def _seed_ledger_file(path, file_stats):
    """Write file_stats straight to disk, bypassing PersistentLedgerTracker._write()'s
    pruning sweep entirely -- used to seed an already-stale entry so a later,
    real write can be observed pruning it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "running_agents": {}, "bash_invocations": {},
        "file_stats": file_stats,
    }))


class TestPersistentLedgerTrackerTimestampedEntries:
    @pytest.fixture
    def tracker(self, tmp_path):
        # PersistentLedgerTracker.record_file_change() filters files outside PROJECT_ROOT.
        # Use record_file_modified/record_file_created directly to test timestamped entries.
        return PersistentLedgerTracker(path=tmp_path / "changes-tracker.json")

    def test_modification_recorded_as_timestamp_keyed_range(self, tracker, tmp_path):
        # Test the core logic using record_file_modified() directly
        # (bypasses PROJECT_ROOT filter)
        target = "existing.py"
        tracker.record_file_modified(target, [3, 8])

        stats = tracker.get_file_stats()
        entries = stats["files_modified"][target]
        assert isinstance(entries, dict)
        (timestamp, value), = entries.items()
        assert value == [3, 8]

    def test_multiple_edits_produce_multiple_timestamped_entries(self, tracker, tmp_path):
        # Test the core logic using record_file_modified() directly
        target = "f.py"
        tracker.record_file_modified(target, [1, 5])
        tracker.record_file_modified(target, [10, 15])

        stats = tracker.get_file_stats()
        entries = stats["files_modified"][target]
        assert len(entries) == 2

    def test_modified_entries_past_retention_are_pruned_on_next_write(self, tmp_path):
        """ANY subsequent write prunes stale entries -- not just a write touching
        this same path -- since _write()'s _prune_stale_file_stats() sweeps every
        tracked path on every write (bash/edit/write alike), not only the path the
        current call happens to be recording.

        The stale entry is seeded straight to disk (bypassing the tracker's own
        _write(), which would prune it on the spot) so a later, real write can be
        observed pruning it.
        """
        from datetime import datetime, timezone, timedelta
        import src.config as config

        ledger_path = tmp_path / "changes-tracker.json"
        target = "f.py"
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=config.FILE_STATS_RETENTION_SECONDS + 60)).isoformat()
        _seed_ledger_file(ledger_path, {"files_created": {}, "files_modified": {target: {old_ts: [1, 5]}}})

        tracker = PersistentLedgerTracker(path=ledger_path)
        assert old_ts in tracker.get_file_stats()["files_modified"][target]

        # A write touching a DIFFERENT path still sweeps and prunes the stale one.
        tracker.record_file_modified("other.py", [1, 2])

        entries = tracker.get_file_stats()["files_modified"]
        assert target not in entries

    def test_created_entries_past_retention_are_pruned_on_next_write(self, tmp_path):
        from datetime import datetime, timezone, timedelta
        import src.config as config

        ledger_path = tmp_path / "changes-tracker.json"
        target = "new.py"
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=config.FILE_STATS_RETENTION_SECONDS + 60)).isoformat()
        _seed_ledger_file(ledger_path, {"files_created": {target: {old_ts: []}}, "files_modified": {}})

        tracker = PersistentLedgerTracker(path=ledger_path)
        assert old_ts in tracker.get_file_stats()["files_created"][target]

        tracker.record_file_created("other.py")

        entries = tracker.get_file_stats()["files_created"]
        assert target not in entries

    def test_record_file_change_hot_path_prunes_past_retention(self, tmp_path, monkeypatch):
        """record_file_change() -- the actual method hooks/handler_edit.py and
        hooks/handler_write.py call on every real Edit/Write -- goes through the
        same _write() sweep, so it also prunes stale entries on ANY write, not
        just one touching the same path."""
        import src.config as config
        from datetime import datetime, timezone, timedelta

        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

        stale_target = tmp_path / "existing.py"
        stale_target.write_text("x = 1\n")
        other_target = tmp_path / "other.py"
        other_target.write_text("y = 2\n")

        ledger_path = tmp_path / "changes-tracker.json"
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=config.FILE_STATS_RETENTION_SECONDS + 60)).isoformat()
        _seed_ledger_file(ledger_path, {"files_created": {}, "files_modified": {str(stale_target): {old_ts: [1, 2]}}})

        tracker = PersistentLedgerTracker(path=ledger_path)
        tracker.record_file_change(str(other_target), [3, 4], is_creation=False)

        entries = tracker.get_file_stats()["files_modified"]
        assert str(stale_target) not in entries
