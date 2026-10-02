"""Bug: the agent_live heartbeat is stamped ONLY in the PreToolUse phase.

handler_bash stamps `subagent_sync_call_on_bash` just before a command runs (and
handler_edit stamps `subagent_sync_call_on_edit_write` just before an edit), then
nothing re-stamps on the way out. A sub-agent that runs ONE command longer than
STALE_THRESHOLD_SECONDS -- e.g. `e2e/run_variant_probes.py`, ~5 min -- therefore
stamps once at the start and stays silent for the whole command, so classify()
reads it as STALE while it is demonstrably alive and working. cwpilot pull + the
Stop hook then render recover_stale on a live agent, every turn.

Desired: the Post phase re-stamps the heartbeat too (throttled, so this is not a
CLI subprocess per invocation). The heartbeat window then bounds at
command_duration + throttle instead of being unbounded.

These assert the DESIRED behaviour and FAIL on current code (no Post-phase stamp);
they turn green once the fix lands. `test_bash_pre_tool_use_still_stamps` is the
paired always-green pin: it proves the PreToolUse stamp is unchanged, so a red Post
test means "the stamp is missing from Post", not "the stamp moved".
"""

import io
import json

import pytest

from hooks import handler_bash, handler_edit

# A real `cwpilot play advance <run_id>` invocation -- _extract_run_id_from_command
# resolves run_impl_abc123 out of it (same form the sibling policy tests use).
_CMD_WITH_RUN = "cwpilot play advance run_impl_abc123"
_AGENT_ID = "agent-heartbeat-probe"


def _run_bash(monkeypatch, phase, command=_CMD_WITH_RUN, agent_id=_AGENT_ID):
    """Drive handler_bash.main() for one hook phase, capturing every throttled-sync
    event it fires. Returns the list of event names."""
    events = []
    monkeypatch.setattr(
        handler_bash, "invoke_throttled_sync",
        lambda event, *a, **k: events.append(event) or True,
    )
    monkeypatch.setattr(handler_bash, "invoke_hook_cli", lambda *a, **k: None)
    monkeypatch.setattr(handler_bash, "invoke_and_get_payload", lambda *a, **k: {})

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": phase,
        "agent_id": agent_id,
        "tool_input": {"command": command},
    })))
    with pytest.raises(SystemExit):
        handler_bash.main()
    return events


def _run_edit(monkeypatch, phase, tmp_path, agent_id=_AGENT_ID):
    """Drive handler_edit.main() for one hook phase, capturing agent-live stamps."""
    target = tmp_path / "some_source.py"
    target.write_text("x = 1\n")

    stamped = []
    monkeypatch.setattr(
        handler_edit, "_stamp_agent_live",
        lambda a_id, tracker: stamped.append(a_id) or True,
    )

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": phase,
        "agent_id": agent_id,
        "tool_input": {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
    })))
    with pytest.raises(SystemExit):
        handler_edit.main()
    return stamped


class TestBashHeartbeatOnPostPhase:
    def test_bash_post_tool_use_restamps_heartbeat(self, monkeypatch):
        events = _run_bash(monkeypatch, "PostToolUse")
        assert "subagent_sync_call_on_bash" in events, (
            "a long bash command stamps liveness only at PreToolUse today; PostToolUse "
            "must re-stamp so the heartbeat window is command_duration + throttle, "
            "not unbounded"
        )

    def test_bash_post_tool_use_failure_restamps_heartbeat(self, monkeypatch):
        events = _run_bash(monkeypatch, "PostToolUseFailure")
        assert "subagent_sync_call_on_bash" in events, (
            "a command that failed after running longer than the stale threshold is "
            "still proof the agent was alive; PostToolUseFailure must re-stamp too"
        )

    def test_bash_pre_tool_use_still_stamps(self, monkeypatch):
        """Always-green pin: the existing PreToolUse stamp is untouched by the fix."""
        events = _run_bash(monkeypatch, "PreToolUse")
        assert "subagent_sync_call_on_bash" in events

    def test_bash_post_tool_use_no_stamp_without_agent(self, monkeypatch):
        """Always-green pin: the main-window session has no sub-agent liveness to
        report, so an agent_id-less Post call must not stamp."""
        events = _run_bash(monkeypatch, "PostToolUse", agent_id=None)
        assert "subagent_sync_call_on_bash" not in events


class TestEditHeartbeatOnPostPhase:
    def test_edit_post_tool_use_restamps_heartbeat(self, monkeypatch, tmp_path):
        stamped = _run_edit(monkeypatch, "PostToolUse", tmp_path)
        assert stamped == [_AGENT_ID], (
            "handler_edit stamps agent_live only at PreToolUse today; an edit that "
            "took longer than the stale threshold must re-stamp on the way out"
        )

    def test_edit_pre_tool_use_still_stamps(self, monkeypatch, tmp_path):
        """Always-green pin: the existing PreToolUse stamp is untouched by the fix."""
        stamped = _run_edit(monkeypatch, "PreToolUse", tmp_path)
        assert stamped == [_AGENT_ID]
