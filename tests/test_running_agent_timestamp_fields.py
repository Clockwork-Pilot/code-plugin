"""Regression guard: running_agents entries must never grow ad-hoc timestamp
fields (e.g. the retired dispatched_at) beyond the four the ledger's
staleness/eviction logic actually understands: started_at, updated_at,
synced_at, finished_at.
"""

import ast

from src.config import PLUGIN_ROOT
from src.ledger import PersistentLedgerTracker
from src.hook_cli_calls import LAST_LIVE_STAMP_FIELD

ALLOWED_RUNNING_AGENT_TIMESTAMP_FIELDS = {"started_at", "updated_at", "synced_at", "finished_at"}


def _string_literal_arg(call, index):
    """The literal string value of a Call's positional arg at `index`, or None."""
    if index >= len(call.args):
        return None
    arg = call.args[index]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _timestamp_name_literals(py_file):
    """Every literal timestamp_name string passed to update_timestamp/clear_timestamp
    in one source file."""
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    literals = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("update_timestamp", "clear_timestamp")):
            literal = _string_literal_arg(node, 1)
            if literal is not None:
                literals.append(literal)
    return literals


class TestRunningAgentTimestampFieldAllowlist:
    def test_no_forbidden_timestamp_field_literals_in_source(self):
        """Every hardcoded timestamp_name in hooks/ and src/ must be one of the four
        fields the eviction/staleness logic reads -- a new one-off field (like the
        retired dispatched_at) must not reappear silently."""
        offenders = {}
        for base in (PLUGIN_ROOT / "hooks", PLUGIN_ROOT / "src"):
            for py_file in base.rglob("*.py"):
                bad = [l for l in _timestamp_name_literals(py_file)
                       if l not in ALLOWED_RUNNING_AGENT_TIMESTAMP_FIELDS]
                if bad:
                    offenders[str(py_file)] = bad
        assert offenders == {}

    def test_dispatched_at_literal_is_gone_from_source(self):
        """dispatched_at was retired outright -- it must not resurface anywhere in
        the hook/ledger source, not just in the timestamp-writing call sites."""
        for base in (PLUGIN_ROOT / "hooks", PLUGIN_ROOT / "src"):
            for py_file in base.rglob("*.py"):
                assert "dispatched_at" not in py_file.read_text(), py_file

    def test_full_agent_lifecycle_only_writes_allowed_fields(self, tmp_path):
        """record_agent_start -> dispatch/live stamp -> record_agent_finished, the
        real production sequence, must never leave a field outside the allowlist
        on the agent's running_agents entry."""
        tracker = PersistentLedgerTracker(path=tmp_path / "session.json")
        tracker.record_agent_start("agent-1")
        tracker.update_timestamp("agent-1", LAST_LIVE_STAMP_FIELD, "2026-08-19T00:00:00+00:00")
        tracker.record_agent_finished("agent-1")

        entry = tracker.get_running_agents()["agent-1"]
        assert set(entry.keys()) <= ALLOWED_RUNNING_AGENT_TIMESTAMP_FIELDS
