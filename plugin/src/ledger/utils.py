"""Ledger utilities: shared helpers for read/write operations."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

# Run-id validation lives here (a leaf) so ledgers never imports src.runinfo for it.
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_.][a-zA-Z0-9_.-]{0,255}$")


def dumps_indented(data: Any, indent: int = 2) -> str:
    """json.dumps(data, indent=indent), except a list with no dict/list elements of
    its own (e.g. a [start, end] line range) renders on one line instead of one
    element per line."""
    if isinstance(data, dict):
        if not data:
            return "{}"
        pad = " " * indent
        body = ",\n".join(
            f"{pad}{json.dumps(k)}: {_indent_body(dumps_indented(v, indent), indent)}"
            for k, v in data.items()
        )
        return "{\n" + body + "\n}"
    if isinstance(data, list):
        if any(isinstance(x, (dict, list)) for x in data):
            pad = " " * indent
            body = ",\n".join(f"{pad}{_indent_body(dumps_indented(x, indent), indent)}"
                              for x in data)
            return "[\n" + body + "\n]"
        return json.dumps(data)
    return json.dumps(data)


def _indent_body(text: str, indent: int) -> str:
    """Indent every line of ``text`` after the first by ``indent`` spaces, so a
    nested dumps_indented() call's multi-line output lines up under its key."""
    pad = " " * indent
    return text.replace("\n", "\n" + pad)


def _sanitize_ledger_id(value: str | None) -> str:
    """Make an id safe to use in a filename."""
    if not value:
        return ""
    return _UNSAFE_ID_CHARS.sub("_", value)


def _now_iso() -> str:
    """Current time in ISO 8601 format (UTC, always microsecond precision so the
    string is fixed-width and sorts/compares lexicographically)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_iso_timestamp(ts: str) -> Optional[float]:
    """Parse ISO 8601 timestamp to Unix epoch seconds. Fail-soft: return None on error."""
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def prune_older_than(mapping: dict, retention_seconds: float) -> dict:
    """Drop ``{iso_timestamp: value}`` entries older than ``retention_seconds``.

    Shared by record_bash_invocation (flat {timestamp: entry}) and
    record_file_change (nested {path: {timestamp: value}}, called per-path) so both
    time-based retention windows use one aging rule. A key that fails to parse is
    kept rather than dropped -- an unparseable timestamp is not evidence of age.
    """
    if not mapping:
        return mapping
    cutoff = datetime.now(timezone.utc).timestamp() - retention_seconds
    return {
        k: v for k, v in mapping.items()
        if (ts := parse_iso_timestamp(k)) is None or ts >= cutoff
    }


def is_valid_run_id(run_id: str) -> bool:
    """Non-bogus run id: non-empty, no leading hyphen (rejects CLI flags),
    only alphanumerics/underscore/dot/hyphen, capped at 256 chars."""
    return isinstance(run_id, str) and bool(_RUN_ID_RE.match(run_id))


def run_ids_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two run ids name the SAME run.

    A run is referable two ways: its full id (``run_implement_work_<label>_<alias>``)
    and the short ``<alias>`` suffix that dispatch prompts and
    ``detected_playbook_run_id`` carry. Comparing the two forms with ``==`` silently
    fails, which is how a working sub-agent came to be classified stale: the ledger
    stored the alias while every lookup passed the full id.

    Equal ids match; otherwise the shorter must be the ``_``-delimited suffix of the
    longer, so ``54b142c7`` matches ``run_..._54b142c7`` but not a bare substring.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    long_id, short_id = (a, b) if len(a) > len(b) else (b, a)
    return long_id.endswith(f"_{short_id}")
