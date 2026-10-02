"""Regression guard: file_stats (the top-level ledger field's own nested dict)
is a closed set of exactly {files_created, files_modified}. Nothing else may be
written into it -- a caught regression: record_file_change() used to stamp
last_file_change_at INTO file_stats instead of onto the ledger's top level,
which would have silently corrupted every file_stats dict on disk.
"""

from src.ledger import PersistentLedgerTracker

ALLOWED_FILE_STATS_FIELDS = {"files_created", "files_modified"}


class TestFileStatsFieldAllowlist:
    def test_fresh_file_stats_is_exactly_the_allowlist(self, tmp_path):
        tracker = PersistentLedgerTracker(path=tmp_path / "session.json")
        assert set(tracker.get_file_stats().keys()) == ALLOWED_FILE_STATS_FIELDS

    def test_full_lifecycle_only_writes_allowed_file_stats_fields(self, tmp_path, monkeypatch):
        """record_file_change (creation + modification) and reset_file_stats, the
        real production sequence, must never leave a key outside the allowlist on
        the ledger's file_stats dict."""
        import src.config as config
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
        created = tmp_path / "new.py"
        modified = tmp_path / "existing.py"
        created.write_text("x = 1\n")
        modified.write_text("y = 2\n")

        tracker = PersistentLedgerTracker(path=tmp_path / "session.json")
        tracker.record_file_change(str(created), is_creation=True)
        tracker.record_file_change(str(modified), [1, 2], is_creation=False)

        assert set(tracker.get_file_stats().keys()) <= ALLOWED_FILE_STATS_FIELDS

        tracker.reset_file_stats()
        assert set(tracker.get_file_stats().keys()) <= ALLOWED_FILE_STATS_FIELDS
