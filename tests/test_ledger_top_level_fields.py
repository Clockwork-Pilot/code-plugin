"""Regression guard: the ledger's top-level JSON schema is a closed set. A stray
new top-level field (typo, forgotten mixin, copy-paste of a running_agents-style
field one level too high) must fail loudly here rather than silently riding
along in every ledger file from then on.
"""

from src.ledger import PersistentLedgerTracker

ALLOWED_TOP_LEVEL_LEDGER_FIELDS = {
    "running_agents",
    "bash_invocations",
    "file_stats",
    "agent_id",
    "last_prompt_at",
    "last_command_at",
    "last_file_change_at",
    "last_check_result",
    "detected_run_id",
    "detected_run_id_at",
    # Stamped on an agent ledger at SubagentStop, so a sub-agent's own file says when it
    # stopped instead of leaving completion to be inferred from silence.
    "subagent_finished_at",
    "created_at",
    "updated_at",
}


class TestTopLevelLedgerFieldAllowlist:
    def test_fresh_ledger_schema_is_within_the_allowlist(self, tmp_path):
        """A brand-new ledger's _ensure_schema() defaults never exceed the
        allowed top-level keys."""
        tracker = PersistentLedgerTracker(path=tmp_path / "session.json")
        data = tracker.load()
        assert set(data.keys()) <= ALLOWED_TOP_LEVEL_LEDGER_FIELDS

    def test_full_lifecycle_only_writes_allowed_top_level_fields(self, tmp_path, monkeypatch):
        """Exercise the representative top-level writers together and confirm no
        top-level key outside the allowlist ever lands on disk."""
        import src.config as config
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
        target = tmp_path / "x.py"
        target.write_text("x = 1\n")

        tracker = PersistentLedgerTracker(path=tmp_path / "session.json")
        tracker.record_agent_start("agent-1")
        tracker.record_agent_finished("agent-1")
        tracker.record_own_agent_id("agent-1")
        tracker.record_detected_run("run_abc")
        tracker.update_last_prompt_at()
        tracker.update_last_command_at()
        tracker.record_check_result(0)
        tracker.record_bash_invocation(command="ls", exit_code=0)
        tracker.record_file_change(str(target), [1, 2])

        data = tracker.load()
        assert set(data.keys()) <= ALLOWED_TOP_LEVEL_LEDGER_FIELDS
