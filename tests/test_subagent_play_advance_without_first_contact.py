"""A sub-agent's denied commands are read PER BASH CALL, so the denial holds on the
very first call -- with no prior `play show` first contact, and with nothing about the
denylist authored by this plugin.

The policy is the project's, declared in its YAML:

    # playbooks/imports/subagent_handoff.yaml
    policy:
      subagent_denied_commands:
      - play advance
      - git

and the subagent_fetch_policy binding carries that block VERBATIM -- same key, same
values, no process spawned to go and get it. The plugin's only job is to read it, per
call, and enforce whatever it holds.

The regression this pins: that payload used to be fetched ONCE, at first contact,
when the sub-agent's own `play show <run_id>` triggered
subagent_call_on_run_id_resolved --emit-policy-payload -- then cached on the session
ledger under running_agents[agent_id].denied_commands and re-read from there. Every
link was load-bearing: no first contact, a synced_at already stamped by a retried
`play show`, a lost or cleared ledger write, and NOTHING was denied. The sub-agent
advanced its own run through verify, review and commit, and closed the work out with
no commit at all.

So the stub below answers ONLY subagent_fetch_policy -- the per-call seam. It returns
nothing for subagent_call_on_run_id_resolved, which is the point: a denial that still
needed first contact to arrive would leave these red.
"""

import io
import json

import pytest

from hooks import handler_bash

# What the project's YAML declares, carried verbatim on the subagent_fetch_policy
# binding -- the playbook's own `policy:` block, key for key. The plugin neither authors
# nor edits nor renames it, so this literal is readable straight off the YAML above.
PROJECT_POLICY = {
    "subagent_denied_commands": ["play advance", "git"]
}


def _run_pretooluse(monkeypatch, command, agent_id="agent-no-first-contact"):
    """Drive handler_bash.main() as the Bash PreToolUse hook for one sub-agent command.

    Only the per-call policy seam answers; every other CLI seam is silent. A stub that
    also answered subagent_call_on_run_id_resolved would test the first-contact delivery
    being removed, and would pass for the wrong reason.
    """
    def _fetch(event, *args, **kwargs):
        return PROJECT_POLICY if event == "subagent_fetch_policy" else None

    monkeypatch.setattr(handler_bash, "invoke_and_get_payload", _fetch)
    monkeypatch.setattr(handler_bash, "invoke_throttled_sync", lambda *a, **k: None)
    monkeypatch.setattr(handler_bash, "invoke_hook_cli", lambda *a, **k: None)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "agent_id": agent_id,
        "tool_input": {"command": command},
    })))
    with pytest.raises(SystemExit) as exit_info:
        handler_bash.main()
    return exit_info.value.code


class TestPolicyIsFetchedPerBashCall:
    def test_play_advance_is_blocked_on_the_first_bash_call(self, monkeypatch):
        """No `play show` ran, so first-contact delivery never happened."""
        code = _run_pretooluse(monkeypatch, "cwpilot play advance run_impl_abc123")
        assert code == 2, (
            "a dispatched sub-agent must never advance its own run; the project's "
            "policy declares 'play advance' and the per-call fetch must deliver it "
            "without depending on a prior first contact"
        )

    def test_git_is_blocked_on_the_first_bash_call(self, monkeypatch):
        """The project's policy declares 'git' too -- same list, same delivery."""
        code = _run_pretooluse(monkeypatch, "git commit -m wip")
        assert code == 2

    def test_a_command_the_policy_does_not_name_is_allowed(self, monkeypatch):
        """The gate must not fail closed: a sub-agent still has to be able to work."""
        code = _run_pretooluse(monkeypatch, "pytest -n4 tests/")
        assert code in (0, None), "a command the policy does not name must not be denied"

    def test_the_orchestrator_is_never_gated(self, monkeypatch):
        """agent_id absent = the main session, which owns advancing and committing."""
        code = _run_pretooluse(
            monkeypatch, "cwpilot play advance run_impl_abc123", agent_id=None)
        assert code in (0, None)

    def test_no_policy_is_persisted_anywhere(self, monkeypatch, _isolated_ledger_dir):
        """Fetched per call means never stored: nothing may write denied_commands.

        A cached copy is a copy anyone who can write the ledger can author -- either to
        forge a restriction that bricks the agent, or to clear one and remove it.
        """
        _run_pretooluse(monkeypatch, "cwpilot play advance run_impl_abc123")
        for path in _isolated_ledger_dir.rglob("*.json"):
            assert "denied_commands" not in path.read_text(), (
                f"{path.name} persisted the denylist; policy is fetched, never stored"
            )
