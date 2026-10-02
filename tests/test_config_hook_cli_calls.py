"""The hook -> CLI call map.

A hook is its own subprocess, so it reaches the engine through the plugin's CLIs rather
than by importing engine code. HOOK_CLI_CALLS is that seam: every call a hook may make,
in one reviewable place.
"""

import pytest

from src.config import (
    AGENT_LIVE_STAMP_INTERVAL_SECONDS,
    HOOK_CLI_CALLS,
    HOOK_CLI_EVENTS,
)

# An entry declares EITHER `cmd` (a process to run) or `policy` (a literal payload
# answered without spawning anything). That key IS the split, so it is read off the
# bindings rather than re-listed here -- a second copy of the event names would only
# go stale against config.HOOK_CLI_EVENTS, which is the one place they are declared.
COMMAND_EVENTS = {event for event, entry in HOOK_CLI_CALLS.items() if "cmd" in entry}
LITERAL_EVENTS = {event for event, entry in HOOK_CLI_CALLS.items() if "policy" in entry}


def _cmd(event):
    """The argv of a command event. Each such entry is {"cmd": argv, "timeout": …}, so a
    test asking about the COMMAND says so rather than indexing the binding itself.

    A literal event has no argv at all; KeyError here is the right outcome, since a test
    reaching for the command of a binding that spawns nothing is asking the wrong thing."""
    return HOOK_CLI_CALLS[event]["cmd"]


class TestHookCliCalls:
    def test_every_hook_event_is_configured(self):
        """Each event the plugin names has an entry, so a hook never falls back to a
        hardcoded command. config.HOOK_CLI_EVENTS is the vocabulary; this asserts the
        shipped file covers it, rather than re-typing the names to compare against."""
        assert set(HOOK_CLI_CALLS) == set(HOOK_CLI_EVENTS)

    @pytest.mark.parametrize("event", sorted(HOOK_CLI_EVENTS))
    def test_each_binding_declares_exactly_one_shape(self, event):
        """cmd or policy, never both and never neither -- an entry with both would run a
        process AND claim to answer from itself, and the loader honours only one."""
        entry = HOOK_CLI_CALLS[event]
        assert ("cmd" in entry) != ("policy" in entry)

    @pytest.mark.parametrize("event", sorted(COMMAND_EVENTS))
    def test_calls_are_argv_lists_not_shell_strings(self, event):
        """A single argv list per event, and never a shell string -- a run_id or
        agent_id must not pass through a shell."""
        argv = _cmd(event)
        assert isinstance(argv, list)
        assert all(isinstance(arg, str) for arg in argv)

    @pytest.mark.parametrize("event", sorted(COMMAND_EVENTS))
    def test_timeout_is_a_number_or_absent(self, event):
        """A binding may declare its own timeout; anything unusable loads as None, which
        means the default rather than a crash in a module every hook subprocess imports."""
        timeout = HOOK_CLI_CALLS[event]["timeout"]
        assert timeout is None or isinstance(timeout, (int, float))

    def test_the_stop_decision_does_not_share_the_stamping_timeout(self):
        """A heartbeat abandoned late is worthless, so the short default is right for it.
        A Stop decision abandoned is a stop ALLOWED that was meant to be gated -- the one
        call that must not be cut off on a stamp's schedule."""
        assert HOOK_CLI_CALLS["call_on_stop_hook"]["timeout"] is not None

    def test_finish_reports_what_the_agent_changed(self):
        """Close-out needs the file list, which only the {files_changed} placeholder
        can carry -- again the placeholder, not the fact it is spelled into."""
        argv = _cmd("subagent_call_on_run_finished")
        assert any("{files_changed}" in arg for arg in argv)

    def test_dispatch_binds_agent_to_run(self):
        """Dispatch carries the agent id, so correlation is direct.

        What must hold is that the call-time {agent_id} placeholder survives into the
        argv -- how the binding spells the fact it wraps that placeholder in is the
        binding's own data, free to change, and not this test's business."""
        argv = _cmd("subagent_call_on_run_id_resolved")
        assert any("{agent_id}" in arg for arg in argv)


    def test_session_start_carries_no_run_id(self):
        """No run is resolvable at the session boundary: the configured call may exist
        (the shipped hook_cli_calls.json wires every event), but it
        must not depend on a {run_id} that can never be filled in."""
        assert "{run_id}" not in _cmd("call_on_session_start")

    def test_session_start_carries_model_placeholder(self):
        assert "{model}" in _cmd("call_on_session_start")


class TestLiteralBindings:
    """A literal binding carries its payload instead of a command to go and fetch it."""

    @pytest.mark.parametrize("event", sorted(LITERAL_EVENTS))
    def test_a_literal_binding_carries_a_payload(self, event):
        """No cmd means no subprocess, so the payload has to be here and has to be a
        mapping -- a string or a list would reach the handler's .get() as a crash."""
        assert isinstance(HOOK_CLI_CALLS[event]["policy"], dict)

    def test_the_policy_block_is_carried_whole(self):
        """The playbook's `policy:` block, not a hand-picked subset of it: a binding that
        forwarded only the field today's caller reads would silently drop the next one."""
        policy = HOOK_CLI_CALLS["subagent_fetch_policy"]["policy"]
        assert "subagent_denied_commands" in policy


class TestAgentLiveStampInterval:
    def test_interval_is_positive(self):
        """Verify the liveness stamp interval is configured."""
        assert AGENT_LIVE_STAMP_INTERVAL_SECONDS > 0
