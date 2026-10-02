"""File-change tracking logic mixin for both ephemeral and persistent trackers.

Provides the core record/deduplicate/timestamp logic for file create/modify events.
Used by both FilesChangesTracker (ephemeral) and PersistentLedgerTracker (persistent).
"""

from typing import Any, Dict, List, Optional

import src.config as config
from .utils import _now_iso, prune_older_than


class FileChangesRecordingMixin:
    """Mixin providing file-change recording logic (create/modify dedup + timestamping).

    Expects the class using this mixin to provide:
      - self.load() -> dict: returns {files_created, files_modified}
      - self._write(data) -> None: persists the data (to temp file or ledger)
    """

    def record_file_change(self, file_path: str, line_range: list | None = None,
                           is_creation: bool = False) -> None:
        """Record a file create/modify with optional line range.

        Dedups within the tracker: a created file is listed once; edits to a file the
        tracker itself created stay in files_created and do not leak into files_modified.

        Each edit becomes its own timestamped entry -- {file_path: {iso_timestamp: value}}
        -- rather than merging ranges at write time, so a reader can later window entries
        by time (e.g. only those after a given PlaybookRun's start) instead of attributing
        a file's entire accumulated history unconditionally.

        Args:
            file_path: The file written or edited.
            line_range: Optional [start, end] (1-based, inclusive) for modifications.
            is_creation: True if this is a new file, False if modification.
        """
        data = self.load()
        created = data.get("files_created") or {}
        modified = data.get("files_modified") or {}

        if is_creation:
            per_file = created.setdefault(file_path, {})
            per_file[_now_iso()] = []
            created[file_path] = prune_older_than(per_file, config.FILE_STATS_RETENTION_SECONDS)
        elif file_path not in created:
            # Only record in modified if not already in files_created
            per_file = modified.setdefault(file_path, {})
            per_file[_now_iso()] = line_range or []
            modified[file_path] = prune_older_than(per_file, config.FILE_STATS_RETENTION_SECONDS)

        data["files_created"] = created
        data["files_modified"] = modified
        self._write(data)

    def record_file_created(self, file_path: str) -> None:
        """Record a file creation."""
        self.record_file_change(file_path, line_range=None, is_creation=True)

    def record_file_modified(self, file_path: str, line_range: list | None = None) -> None:
        """Record a file modification."""
        self.record_file_change(file_path, line_range=line_range, is_creation=False)

    def get_file_stats(self) -> dict:
        """Return the current accumulated file_stats {files_created, files_modified}."""
        data = self.load()
        return {
            "files_created": data.get("files_created") or {},
            "files_modified": data.get("files_modified") or {},
        }

    def reset_file_stats(self) -> None:
        """Clear all recorded file changes."""
        data = self.load()
        data["files_created"] = {}
        data["files_modified"] = {}
        self._write(data)

    def _ensure_file_stats_schema(self, data: dict) -> dict:
        """Ensure file_stats keys exist with defaults."""
        if "files_created" not in data:
            data["files_created"] = {}
        if "files_modified" not in data:
            data["files_modified"] = {}
        return data


def _fold_line_ranges(ranges: Optional[List[List[int]]]) -> Optional[str]:
    """Fold overlapping/adjacent line ranges into a single "start - end" string.

    [[20, 71], [50, 120]] -> "20 - 120"; [[20, 30], [31, 40]] -> "20 - 40";
    [] / None -> None. Returns None if the list is empty/None or has no valid ranges.
    """
    if not ranges or not isinstance(ranges, list):
        return None
    if len(ranges) == 0:
        return None

    valid_ranges = [r for r in ranges if isinstance(r, (list, tuple)) and len(r) >= 2]
    if not valid_ranges:
        return None

    sorted_ranges = sorted(valid_ranges, key=lambda r: r[0])
    merged = [sorted_ranges[0]]
    for current in sorted_ranges[1:]:
        last = merged[-1]
        if current[0] <= last[1] + 1:
            merged[-1] = [last[0], max(last[1], current[1])]
        else:
            merged.append(current)

    start = merged[0][0]
    end = merged[-1][1]
    return f"{start} - {end}"


def _fold_file_stats(file_stats: Dict[str, Any], since_timestamp: Optional[str] = None) -> Dict[str, Any]:
    """Convert file_stats with nested {timestamp: line_range} entries into folded string format.

    Input: {"files_created": {path: {iso_ts: []}}, "files_modified": {path: {iso_ts: [s,e]}}}
    Output: {"files_created": {path: "s - e"}, "files_modified": {path: "s - e"}}

    When since_timestamp is given, only entries with timestamp >= since_timestamp are
    gathered (ISO 8601 strings sort lexicographically). A plain list value (pre-migration
    ledger) is accepted as-is. Empty created-file ranges become None and are omitted.
    """
    folded: Dict[str, Any] = {}

    for kind in ("files_created", "files_modified"):
        original = file_stats.get(kind) or {}
        folded_paths: Dict[str, str] = {}

        for path, entries in original.items():
            if isinstance(entries, dict):
                ranges = [
                    r for ts, r in entries.items()
                    if since_timestamp is None or ts >= since_timestamp
                ]
            elif isinstance(entries, list):
                ranges = entries
            elif isinstance(entries, str):
                folded_paths[path] = entries
                continue
            else:
                continue

            folded_range = _fold_line_ranges(ranges)
            if folded_range is not None:
                folded_paths[path] = folded_range

        folded[kind] = folded_paths

    return folded
