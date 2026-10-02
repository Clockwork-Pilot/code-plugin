"""The hook -> CLI invoker: placeholder filling, throttling, and fail-soft behavior.

The fail-soft tests are the load-bearing ones. This code runs inside the critical path of
the user's own tool call, so every failure mode must degrade to "no call made" rather than
propagate: a dropped liveness stamp costs one heartbeat, a raising hook costs the user
their command.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import hook_cli_calls
from src.config import AGENT_LIVE_STAMP_INTERVAL_SECONDS


#: Bindings the invoker tests run against, in the loader's own shape. THEIRS, not the
#: shipped file's: this module tests what the invoker DOES -- fills placeholders, blocks
#: on exit 2, relays stdout, throttles, gives up on time -- and none of that is a claim
#: about the command any particular project wired. Reading the shipped config instead made
#: every one of these tests hostage to it: a project adding {files_changed} to its own
#: finish call left _fill unable to complete the argv, so no subprocess ran and thirteen
#: assertions about blocking and relaying quietly never got their chance.
#:
#: The EVENT NAMES are kept -- they are the plugin's own vocabulary
#: (config.HOOK_CLI_EVENTS), not a project's choice. Only the argv is invented here, and
#: it carries exactly the placeholders these tests supply, so what fills is decided by the
#: test rather than by whatever a project happens to have wired.
#:
#: Small timeouts, stated rather than inherited: nothing below actually executes
#: (subprocess.run is mocked in every test but the stub-contract class), and a test that
#: would sit for the production timeout on a hang reports a hang as a long wait.
TEST_BINDINGS = {
    "call_on_session_start": {"cmd": ["mytool", "hook", "session-start"], "timeout": 5},
    "call_on_stop_hook": {"cmd": ["mytool", "hook", "stop"], "timeout": 5},
    "subagent_sync_call_on_bash": {"cmd": ["mytool", "facts", "{run_id}",
                                           "--fact", "agent_live"], "timeout": 5},
    "subagent_sync_call_on_edit_write": {"cmd": ["mytool", "facts", "{run_id}",
                                                 "--fact", "agent_live"], "timeout": 5},
    "subagent_call_on_run_id_resolved": {"cmd": ["mytool", "facts", "{run_id}",
                                             "--fact", "dispatched",
                                             "--fact", "agent_id={agent_id}"], "timeout": 5},
    "subagent_call_on_run_finished": {"cmd": ["mytool", "facts", "{run_id}",
                                              "--fact", "agent_finished"], "timeout": 5},
}


@pytest.fixture(autouse=True)
def _own_bindings(monkeypatch):
    """Every test in this module resolves events from TEST_BINDINGS."""
    monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", dict(TEST_BINDINGS))


def _tracker(last_stamp=None):
    """A ledger stub exposing only what the invoker actually touches."""
    tracker = MagicMock()
    tracker.get_running_agents.return_value = (
        {"agent-1": {hook_cli_calls.LAST_LIVE_STAMP_FIELD: last_stamp}} if last_stamp
        else {"agent-1": {}}
    )
    return tracker


def _iso_ago(seconds):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class TestPlaceholderFilling:
    def test_run_id_is_substituted(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            assert hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc") is True
            argv = run.call_args[0][0]
        assert "run_abc" in argv
        assert not any("{run_id}" in a for a in argv)

    def test_agent_id_is_substituted(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_id_resolved", run_id="run_abc", agent_id="agent-1")
            argv = run.call_args[0][0]
        assert "agent_id=agent-1" in argv

    def test_cwpilot_bin_ignores_the_environment_override(self, monkeypatch):
        """CWPILOT_BIN must NOT be honored -- only a signed, versioned install ever runs."""
        monkeypatch.setenv("CWPILOT_BIN", "/custom/bin/cwpilot")
        monkeypatch.setattr(hook_cli_calls.binary_resolver, "find_cwpilot", lambda: None)
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", {
            "call_on_stop_hook": {"cmd": ["{cwpilot_bin}", "hook"], "timeout": 5},
        })
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("call_on_stop_hook")
        assert run.call_args[0][0][0] != "/custom/bin/cwpilot"

    def test_cwpilot_bin_never_resolves_to_a_bare_path_searched_name(self, monkeypatch):
        """Unresolved must not fall back to the bare name "cwpilot": passed to
        subprocess.run() unqualified, the OS's own exec would search $PATH for it,
        smuggling back in exactly what the resolver refuses to consult."""
        monkeypatch.delenv("CWPILOT_BIN", raising=False)
        monkeypatch.setattr(hook_cli_calls.binary_resolver, "find_cwpilot", lambda: None)
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", {
            "call_on_stop_hook": {"cmd": ["{cwpilot_bin}", "hook"], "timeout": 5},
        })
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("call_on_stop_hook")
        resolved = run.call_args[0][0][0]
        assert resolved != "cwpilot"
        assert os.path.isabs(resolved)

    def test_cwpilot_bin_resolves_to_the_versioned_install(self, monkeypatch):
        """The versioned install under CWPILOT_VERSIONS_DIR (binary_resolver.find_cwpilot)
        is the only thing that makes this plugin usable at all -- neither
        CWPILOT_BIN nor $PATH are ever consulted for cwpilot."""
        monkeypatch.delenv("CWPILOT_BIN", raising=False)
        monkeypatch.setattr(
            hook_cli_calls.binary_resolver, "find_cwpilot",
            lambda: "/versions/v1.0.0/cwpilot",
        )
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", {
            "call_on_stop_hook": {"cmd": ["{cwpilot_bin}", "hook"], "timeout": 5},
        })
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("call_on_stop_hook")
        assert run.call_args[0][0][0] == "/versions/v1.0.0/cwpilot"

    def test_cwpilot_bin_resolution_is_skipped_when_unused(self, monkeypatch):
        """A configured call that never mentions {cwpilot_bin} must not pay for
        resolving it -- find_cwpilot() scans the versions directory, and
        that cost must not land on every hook of every tool call."""
        monkeypatch.delenv("CWPILOT_BIN", raising=False)

        def unexpected_find():
            raise AssertionError("binary_resolver.find_cwpilot must not run for an unused placeholder")

        monkeypatch.setattr(hook_cli_calls.binary_resolver, "find_cwpilot", unexpected_find)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("call_on_session_start")

    def test_missing_run_id_makes_no_call(self):
        """No run resolved means there is nothing to stamp -- not a call against a run
        literally named '{run_id}'."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            assert hook_cli_calls.invoke("subagent_call_on_run_finished") is False
            run.assert_not_called()

    def test_unconfigured_event_makes_no_call(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            assert hook_cli_calls.invoke("call_on_nothing", run_id="run_abc") is False
            run.assert_not_called()

    def test_empty_configured_call_makes_no_call(self):
        """An event configured as [] -- an explicit no-op."""
        with patch.dict(hook_cli_calls.HOOK_CLI_CALLS, {"call_on_empty": {"cmd": []}}):
            with patch.object(hook_cli_calls.subprocess, "run") as run:
                assert hook_cli_calls.invoke("call_on_empty", run_id="run_abc") is False
                run.assert_not_called()

    # "one argv list per event, never a shell string" is asserted about the SHIPPED
    # config in test_config_hook_cli_calls.test_calls_are_argv_lists_not_shell_strings,
    # which is where a claim about that file belongs. It lived here too, as the module's
    # last read of the shipped wiring; a second copy in the invoker's own tests only
    # re-coupled them to a file this module has no business knowing about.

    def test_never_uses_a_shell(self):
        """run_id/agent_id carry agent-controlled text; a shell would be injectable."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc; rm -rf /")
            assert run.call_args.kwargs.get("shell") in (None, False)
            assert isinstance(run.call_args[0][0], list)


class TestBlocking:
    def test_exit_two_blocks(self):
        """A side-effect caller fails soft; it does not reinterpret the CLI exit code."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, "", "not allowed")
            assert hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc") is False

    def test_every_other_nonzero_exit_still_fails_soft(self):
        """Blocking is narrow on purpose -- a hook must not break the tool call it
        observes just because a CLI errored."""
        for code in (1, 3, 127):
            with patch.object(hook_cli_calls.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], code, "", "boom")
                assert hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc") is False


class TestDocumentIsolation:
    def test_placeholder_filling(self):
        """Placeholders are filled correctly."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
            argv = run.call_args[0][0]
        assert "run_abc" in argv
        assert not any("{" in a for a in argv)


class TestThrottling:
    def test_first_stamp_is_due(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=_tracker(),
                                        throttled=True)
        assert ran is True

    def test_recent_stamp_is_skipped(self):
        recent = _iso_ago(AGENT_LIVE_STAMP_INTERVAL_SECONDS / 3)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=_tracker(recent),
                                        throttled=True)
            run.assert_not_called()
        assert ran is False

    def test_old_stamp_is_due_again(self):
        old = _iso_ago(AGENT_LIVE_STAMP_INTERVAL_SECONDS * 2)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=_tracker(old),
                                        throttled=True)
        assert ran is True

    def test_corrupt_stamp_does_not_wedge_stamping_off(self):
        """An unparseable throttle value must read as due, not as 'never again'."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1",
                                        tracker=_tracker("not-a-timestamp"),
                                        throttled=True)
        assert ran is True

    def test_unthrottled_events_ignore_the_interval(self):
        """Dispatch and finish are one-shot; a recent heartbeat must not suppress them."""
        recent = _iso_ago(1)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc",
                                        agent_id="agent-1", tracker=_tracker(recent))
        assert ran is True

    def test_stamp_is_claimed_before_the_call_not_after_it(self):
        """The claim is the throttle, so it is written while no subprocess is in flight.

        Recording it afterwards let two overlapping hooks both read a not-yet-updated
        timestamp and both stamp -- seen live as two agent_live calls 10.9s apart under
        a 30s interval.
        """
        tracker = _tracker()
        calls = []
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.side_effect = lambda *a, **k: (
                calls.append("subprocess"),
                subprocess.CompletedProcess([], 0, "", ""),
            )[1]
            tracker.update_timestamp.side_effect = lambda *a, **k: calls.append("stamp")
            hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                  agent_id="agent-1", tracker=tracker, throttled=True)
        assert calls == ["stamp", "subprocess"]

    def test_a_failed_call_still_spends_its_claim(self):
        """Releasing the claim on failure would re-open the race it exists to close.

        One skipped heartbeat is already tolerated: test_config_hook_cli_calls asserts
        the stale threshold is at least three intervals wide.
        """
        tracker = _tracker()
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=tracker, throttled=True)
        assert ran is False
        tracker.update_timestamp.assert_called_once()

    def test_unreadable_throttle_state_does_not_stamp(self):
        """A ledger that cannot be read fails CLOSED.

        It used to fail open, which turned a burst of ledger contention -- exactly when
        overlapping hooks are most likely -- into a burst of unthrottled stamps.
        """
        tracker = _tracker()
        tracker.load.side_effect = RuntimeError("ledger conflict")
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=tracker, throttled=True)
            run.assert_not_called()
        assert ran is False

    def test_dispatch_records_a_stamp(self):
        """Dispatch runs immediately but stamps to throttle subsequent bash calls."""
        tracker = _tracker()
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_call_on_run_id_resolved", run_id="run_abc",
                                        agent_id="agent-1", tracker=tracker)
        assert ran is True
        tracker.update_timestamp.assert_called_once()

    def test_bash_throttled_by_dispatch_stamp(self):
        """A recent dispatch stamp throttles the next bash heartbeat."""
        recent_dispatch = _iso_ago(1)
        tracker = _tracker(recent_dispatch)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=tracker, throttled=True)
            run.assert_not_called()
        assert ran is False

    def test_dispatch_without_tracker_no_op(self):
        """Dispatch without tracker cannot record stamp (but still runs)."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            ran = hook_cli_calls.invoke("subagent_call_on_run_id_resolved", run_id="run_abc",
                                        agent_id="agent-1")
        assert ran is True
        # No tracker means no stamp recorded

    def test_dispatch_with_tracker_and_then_bash(self):
        """Dispatch with tracker records stamp; bash within interval is throttled."""
        tracker = _tracker()
        # First invoke dispatch (records timestamp)
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_id_resolved", run_id="run_abc",
                                  agent_id="agent-1", tracker=tracker)
        assert tracker.update_timestamp.called
        # Simulate recent dispatch (1 second ago, within interval)
        recent = _iso_ago(1)
        tracker.get_running_agents.return_value = {"agent-1": {hook_cli_calls.LAST_LIVE_STAMP_FIELD: recent}}
        # Now bash should be throttled
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                        agent_id="agent-1", tracker=tracker, throttled=True)
            run.assert_not_called()
        assert ran is False

    def test_regression_dispatch_throttles_bash_immediately_after(self):
        """Regression: dispatch must record synced_at so bash doesn't run immediately after.

        Bug: dispatch was being called with agent_ledger instead of session_ledger,
        so synced_at was not recorded on the session's running_agents, leaving bash
        unthrottled and allowing it to run 0-6ms after dispatch.
        """
        tracker = _tracker()
        # Simulate real scenario: dispatch runs and records synced_at
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            dispatch_ran = hook_cli_calls.invoke("subagent_call_on_run_id_resolved",
                                                 run_id="run_abc", agent_id="agent-1",
                                                 tracker=tracker)
        assert dispatch_ran is True
        assert tracker.update_timestamp.called
        # Extract the synced_at timestamp that was just written
        call_args = tracker.update_timestamp.call_args
        synced_at_value = call_args[0][2]  # third positional arg is the value
        # Immediately after, bash should see the recent timestamp and throttle
        tracker.get_running_agents.return_value = {
            "agent-1": {hook_cli_calls.LAST_LIVE_STAMP_FIELD: synced_at_value}
        }
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            bash_ran = hook_cli_calls.invoke("subagent_sync_call_on_bash", run_id="run_abc",
                                             agent_id="agent-1", tracker=tracker, throttled=True)
            run.assert_not_called()
        assert bash_ran is False  # bash should be throttled, not run immediately


class TestFailSoft:
    @pytest.mark.parametrize("boom", [
        OSError("no such binary"),
        subprocess.TimeoutExpired(cmd="play-run", timeout=10),
        subprocess.SubprocessError("generic"),
    ])
    def test_subprocess_failures_never_propagate(self, boom):
        """A hook must not break the tool call it is observing."""
        with patch.object(hook_cli_calls.subprocess, "run", side_effect=boom):
            assert hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc") is False

    def test_nonzero_exit_is_reported_as_not_run(self):
        """Exit 1, not 2 -- 2 now means block and is asserted separately."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "refused")
            assert hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc") is False

    def test_call_is_bounded_by_its_bindings_timeout(self):
        """A heartbeat that arrives late is worthless, so waiting beats nothing -- and
        each binding says how long its own call may take."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
            declared = TEST_BINDINGS["subagent_call_on_run_finished"]["timeout"]
            assert run.call_args.kwargs["timeout"] == declared

    def test_a_binding_without_a_timeout_uses_the_default(self, monkeypatch):
        """Only a call whose reasoning differs from the heartbeat's declares its own."""
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", {
            "subagent_call_on_run_finished": {"cmd": ["mytool", "facts", "{run_id}"],
                                              "timeout": None},
        })
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
            assert run.call_args.kwargs["timeout"] == hook_cli_calls.CLI_CALL_TIMEOUT_SECONDS

    def test_a_timed_out_call_is_logged_not_silent(self, caplog):
        """The one failure that looks like nothing happened: the call is abandoned and
        every path fails soft, so without a log line the only trace is a missing fact."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["mytool"], timeout=5)
            with caplog.at_level("INFO"):
                assert hook_cli_calls.invoke("subagent_call_on_run_finished",
                                             run_id="run_abc") is False
        assert any("'timeout': 5" in r.getMessage() for r in caplog.records)

    def test_completed_call_logs_status_and_empty_output(self, caplog):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            with caplog.at_level("INFO"):
                assert hook_cli_calls.invoke("subagent_call_on_run_finished",
                                             run_id="run_abc") is True
        record = next(r.getMessage() for r in caplog.records if "cwpilot_result" in r.getMessage())
        assert "subagent_call_on_run_finished" in record
        assert "'exit_code': 0" in record
        assert "'stdout': ''" in record


class TestRunAndGetExitCode:
    """run_and_get_exit_code is for hooks whose invoked CLI IS the decision (e.g. the
    Stop hook), so unlike invoke() it must hand back the real exit code, not a bool."""

    def test_unconfigured_event_reports_success(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            assert hook_cli_calls.run_and_get_exit_code("call_on_nothing") == 0
            run.assert_not_called()

    def test_successful_command_reports_zero(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            assert hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r") == 0

    def test_block_exit_code_is_propagated_verbatim(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, "", "not allowed")
            assert hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r") == 2

    def test_arbitrary_nonzero_exit_code_is_propagated_verbatim(self):
        """Unlike invoke(), this must not flatten non-2 failures to False/0."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 7, "", "boom")
            assert hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r") == 7

    def test_subprocess_failure_reports_success(self):
        """A missing binary/timeout is infrastructure trouble, not a CLI opinion."""
        with patch.object(hook_cli_calls.subprocess, "run", side_effect=OSError("no binary")):
            assert hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r") == 0

    def test_stdout_is_relayed_verbatim(self, capsys):
        """call_on_stop_hook's stdout IS the Stop hook decision JSON Claude Code reads
        (e.g. {"decision": "approve", "systemMessage": ...}) -- it must reach this
        process's own stdout unparsed, not just get logged internally."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, '{"decision": "approve", "systemMessage": "ok"}', "")
            hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r")
        assert capsys.readouterr().out == '{"decision": "approve", "systemMessage": "ok"}'

    def test_stderr_is_relayed_verbatim_alongside_stdout(self, capsys):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 2, '{"decision": "block", "reason": "nope"}', "diagnostic")
            hook_cli_calls.run_and_get_exit_code("subagent_call_on_run_finished", run_id="r")
        captured = capsys.readouterr()
        assert captured.out == '{"decision": "block", "reason": "nope"}'
        assert captured.err == "diagnostic"


class TestStopHookDiagnostics:
    """Stop hook diagnostics capture complete decision and relay path for diagnosis."""

    def test_block_decision_is_logged_with_diagnostics(self, caplog):
        """Every Stop hook decision is logged with provider, exit code, and payload info."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            payload = '{"decision": "block", "reason": "test block"}'
            run.return_value = subprocess.CompletedProcess([], 2, payload, "")
            with caplog.at_level("INFO"):
                exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 2
        # Verify diagnostics were logged
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "raw_exit_code" in diagnostics
        assert "2" in diagnostics
        assert "raw_payload" in diagnostics
        assert "decision" in diagnostics

    def test_allow_decision_is_logged_with_full_path(self, caplog):
        """Allow decisions preserve Codex protocol semantics: all fields relayed."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            payload = '{"decision": "approve", "systemMessage": "proceed"}'
            run.return_value = subprocess.CompletedProcess([], 0, payload, "")
            with caplog.at_level("INFO"):
                exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 0
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "transformed_payload" in diagnostics
        assert "approve" in diagnostics
        assert "systemMessage" in diagnostics

    def test_malformed_json_payload_logs_failure(self, caplog):
        """Malformed JSON is logged as a failure with actionable reason."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            payload = '{"decision": "block" missing closing brace'
            run.return_value = subprocess.CompletedProcess([], 1, payload, "")
            with caplog.at_level("INFO"):
                hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "failure" in diagnostics
        assert "malformed_json" in diagnostics
        assert "failure_reason" in diagnostics
        assert "not valid JSON" in diagnostics

    def test_timeout_failure_logs_actionable_reason(self, caplog):
        """Timeout failures are logged with actionable reason."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["stop-cli"], timeout=5)
            with caplog.at_level("INFO"):
                exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 0
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "failure" in diagnostics
        assert "timeout" in diagnostics
        assert "5 seconds" in diagnostics

    def test_missing_command_logs_actionable_reason(self, caplog):
        """Missing command failures are logged with actionable reason."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.side_effect = FileNotFoundError("stop-cli not found")
            with caplog.at_level("INFO"):
                exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 0
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "failure" in diagnostics
        assert "not_found" in diagnostics
        assert "failure_reason" in diagnostics
        assert "PATH" in diagnostics

    def test_raw_and_transformed_payloads_both_captured(self, caplog):
        """Both raw (string) and transformed (parsed) payloads are captured."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            raw = '{"decision": "block", "reason": "policy", "extra": "field"}'
            run.return_value = subprocess.CompletedProcess([], 2, raw, "")
            with caplog.at_level("INFO"):
                hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "raw_payload" in diagnostics
        assert raw in diagnostics
        assert "transformed_payload" in diagnostics
        # The transformed payload should be the parsed dict
        assert "decision" in diagnostics
        assert "policy" in diagnostics

    def test_provider_cli_command_is_captured(self, caplog):
        """Provider (CLI command) is captured for diagnostics."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            with caplog.at_level("INFO"):
                hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "provider" in diagnostics
        # Provider should contain the mytool hook stop command
        assert "mytool" in diagnostics
        assert "stop" in diagnostics

    def test_final_relayed_exit_code_is_captured(self, caplog):
        """Final relayed exit code is captured, even when different from raw."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 7, "{}", "")
            with caplog.at_level("INFO"):
                exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 7
        diagnostics = next(
            (r.getMessage() for r in caplog.records if "stop_hook_decision" in r.getMessage()),
            None
        )
        assert diagnostics is not None
        assert "final_exit_code" in diagnostics
        assert "7" in diagnostics


class TestInvokeAndGetPayload:
    """invoke_and_get_payload is for events whose stub DATA feeds into the calling
    hook (e.g. call_on_session_start's additional_context/system_message), as opposed
    to invoke()'s bool or run_and_get_exit_code()'s raw exit code."""

    def test_unconfigured_event_returns_empty_dict(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            assert hook_cli_calls.invoke_and_get_payload("call_on_nothing") == {}
            run.assert_not_called()

    def test_json_object_stdout_is_parsed(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, '{"additional_context": "ctx", "system_message": "msg"}', "")
            payload = hook_cli_calls.invoke_and_get_payload("subagent_call_on_run_finished",
                                                             run_id="run_abc")
        assert payload == {"additional_context": "ctx", "system_message": "msg"}

    def test_non_json_stdout_degrades_to_empty_dict(self):
        """The shipped default stub prints a bare log line, not JSON -- must not raise."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "not json", "")
            assert hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc") == {}

    def test_empty_stdout_degrades_to_empty_dict(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            assert hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc") == {}

    def test_json_array_is_not_a_payload(self):
        """Only a JSON object has fields to look up -- an array/string/number is
        treated the same as no payload at all."""
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '["a", "b"]', "")
            assert hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc") == {}

    def test_nonzero_exit_yields_no_payload(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, '{"a": "b"}', "boom")
            payload = hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc")
        assert payload == {}
        assert payload.exit_code == 1

    def test_missing_or_non_executable_payload_command_exposes_shell_status(self):
        with patch.object(
            hook_cli_calls.subprocess, "run", side_effect=FileNotFoundError
        ):
            missing = hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc"
            )
        with patch.object(
            hook_cli_calls.subprocess, "run", side_effect=PermissionError
        ):
            non_executable = hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc"
            )

        assert missing == {} and missing.exit_code == 127
        assert non_executable == {} and non_executable.exit_code == 126

    def test_block_exit_code_is_returned_to_the_payload_caller(self):
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, "", "not allowed")
            result = hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc"
            )
        assert result == {} and result.exit_code == 2

    def test_subprocess_failure_fails_soft(self):
        with patch.object(hook_cli_calls.subprocess, "run", side_effect=OSError("no binary")):
            assert hook_cli_calls.invoke_and_get_payload(
                "subagent_call_on_run_finished", run_id="run_abc") == {}


class TestShippedStubsContract:
    """Run every shipped CLI-stub interface through its public generic caller.

    The real config points at consuming-project binaries, so this fixture copies each
    shipped stub into a temporary directory and wires its exact argv shape. No mocked
    subprocess is involved: every configured command interface must execute, receive
    its placeholder-expanded arguments, and preserve its documented return contract.
    """

    @pytest.fixture(autouse=True)
    def wire_stubs_in(self, monkeypatch, tmp_path):
        source_stubs = Path(hook_cli_calls.__file__).resolve().parents[2] / "cli_stubs"
        plugin_root = tmp_path / "plugin"
        stubs = plugin_root / "cli_stubs"
        plugin_root.mkdir()
        stubs.mkdir()
        for source in source_stubs.iterdir():
            if source.is_file() and source.name != "cli_stubs.log":
                target = stubs / source.name
                shutil.copy2(source, target)
                # 0o755, not source.stat().st_mode: the executable bit on a git
                # checkout depends on core.fileMode and how the clone happened, so a
                # machine where the source lost its +x (silently -- nothing here
                # would fail until a subprocess later gets EACCES) would copy that
                # loss forward. This fixture's only requirement is that ITS OWN
                # copies run; it should not inherit an ambient permission problem
                # from whatever checked out the source.
                target.chmod(0o755)

        self.stub_log = stubs / "cli_stubs.log"
        stub_binding = json.loads((source_stubs / "hook_cli_calls.json").read_text(
            encoding="utf-8"
        ))["hook_cli_calls"]
        for entry in stub_binding.values():
            entry["cmd"] = [arg.replace("{plugin_root}", str(plugin_root))
                            for arg in entry["cmd"]]
            entry.setdefault("timeout", 5)
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", stub_binding)

    def test_default_session_start_stub_emits_the_documented_fields(self):
        payload = hook_cli_calls.invoke_and_get_payload("call_on_session_start")
        assert set(payload) == {"additional_context", "system_message"}
        assert all(isinstance(v, str) and v for v in payload.values())

    def test_default_stop_hook_stub_approves_with_the_documented_decision_shape(self, capsys):
        """call_on_stop_hook speaks Claude Code's own decision protocol directly, relayed
        verbatim by run_and_get_exit_code() -- so read it back the same way the harness
        would: parse whatever landed on this process's real stdout."""
        exit_code = hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "approve"
        assert isinstance(payload.get("systemMessage"), str) and payload["systemMessage"]

    @pytest.mark.parametrize("event, expected_args", [
        ("subagent_sync_call_on_bash", "run_stub --fact agent_live"),
        ("subagent_sync_call_on_edit_write", "run_stub --fact agent_live"),
        ("subagent_call_on_run_id_resolved", "run_stub --fact dispatched --fact agent_id=agent_stub"),
        ("subagent_call_on_run_finished", "run_stub --fact agent_finished"),
    ])
    def test_every_fact_stub_executes_with_its_declared_arguments(self, event, expected_args):
        assert hook_cli_calls.invoke(event, run_id="run_stub", agent_id="agent_stub") is True
        record = self.stub_log.read_text(encoding="utf-8")
        assert f"cli_stubs/{event} {expected_args}" in record


class TestStampAgentLive:
    def test_no_agent_means_no_call(self):
        """The main window has no sub-agent liveness to report."""
        assert hook_cli_calls.stamp_agent_live(None, _tracker()) is False

    def test_no_detected_run_means_no_call(self):
        tracker = _tracker()
        tracker.get_detected_playbook_run_id.return_value = None
        assert hook_cli_calls.stamp_agent_live("agent-1", tracker) is False

    def test_detected_run_is_stamped(self):
        tracker = _tracker()
        tracker.get_detected_playbook_run_id.return_value = "run_abc"
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            assert hook_cli_calls.stamp_agent_live("agent-1", tracker) is True
            assert "run_abc" in run.call_args[0][0]

    def test_ledger_failure_never_propagates(self):
        tracker = _tracker()
        tracker.load.side_effect = OSError("ledger unreadable")
        assert hook_cli_calls.stamp_agent_live("agent-1", tracker) is False


class TestChildEnvironment:
    """The env block reaches the CLI as a MERGE over the hook's own environment."""

    def test_no_env_configured_inherits_unchanged(self, monkeypatch):
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_ENV", {})
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
        assert run.call_args.kwargs["env"] is None

    def test_configured_env_is_merged_over_os_environ(self, monkeypatch):
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_ENV", {"CWPILOT_PROJECT_ROOT": "/p"})
        monkeypatch.setenv("PATH", "/usr/bin")
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
        env = run.call_args.kwargs["env"]
        assert env["CWPILOT_PROJECT_ROOT"] == "/p"
        # PATH survives: the CLI still needs it to be found and to run.
        assert env["PATH"] == "/usr/bin"

    def test_payload_and_exit_code_paths_get_it_too(self, monkeypatch):
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_ENV", {"CWPILOT_PROJECT_ROOT": "/p"})
        for call in (lambda: hook_cli_calls.invoke_and_get_payload("call_on_session_start"),
                     lambda: hook_cli_calls.run_and_get_exit_code("call_on_stop_hook")):
            with patch.object(hook_cli_calls.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
                call()
            assert run.call_args.kwargs["env"]["CWPILOT_PROJECT_ROOT"] == "/p"

    # -- the session id the CLI keys its per-session store by -----------------------------

    def _env_for_call(self, monkeypatch, hook_input, env_block=None):
        from src import config
        # monkeypatch restores the module global; set_hook_input() would leak it.
        monkeypatch.setattr(config, "_HOOK_INPUT", dict(hook_input))
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_ENV", env_block or {})
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("subagent_call_on_run_finished", run_id="run_abc")
        return run.call_args.kwargs["env"]

    def test_codex_gets_its_session_id_from_the_hook_payload(self, monkeypatch):
        """Codex puts the id only in the hook's JSON stdin. Without it in the CLI's env,
        cwpilot's SessionStart records no project root for the session."""
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "codex")
        monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
        env = self._env_for_call(monkeypatch, {"session_id": "01a0fd06-sess"})
        assert env["CODEX_SESSION_ID"] == "01a0fd06-sess"

    def test_claude_code_gets_its_variable_when_the_host_did_not_export_it(self, monkeypatch):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "claude-code")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        env = self._env_for_call(monkeypatch, {"session_id": "abc"}, {"CWPILOT_PROJECT_ROOT": "/p"})
        assert env["CLAUDE_CODE_SESSION_ID"] == "abc" and env["CWPILOT_PROJECT_ROOT"] == "/p"

    def test_a_session_id_the_host_exported_is_never_overridden(self, monkeypatch):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "codex")
        monkeypatch.setenv("CODEX_SESSION_ID", "from-host")
        env = self._env_for_call(monkeypatch, {"session_id": "from-payload"}, {"X": "1"})
        assert env["CODEX_SESSION_ID"] == "from-host"

    def test_no_payload_session_or_unknown_provider_adds_nothing(self, monkeypatch):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "codex")
        monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
        assert self._env_for_call(monkeypatch, {}) is None
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "something-else")
        assert self._env_for_call(monkeypatch, {"session_id": "x"}) is None

    def test_plugin_version_is_filled_from_plugin_json(self, monkeypatch):
        monkeypatch.setattr(hook_cli_calls, "HOOK_CLI_CALLS", {
            "call_on_stop_hook": {"cmd": ["mytool"], "timeout": 5},
        })
        with patch.object(hook_cli_calls.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            hook_cli_calls.invoke("call_on_stop_hook")

        metadata = json.loads(
            (hook_cli_calls.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        assert run.call_args.kwargs["env"]["CWPILOT_PLUGIN_VERSION"] == metadata["version"]


def test_bare_cwpilot_command_is_refused():
    from src import hook_cli_calls
    assert hook_cli_calls._resolve_command(["cwpilot", "hook", "stop"]) is None
    assert hook_cli_calls._resolve_command(["/abs/cwpilot", "hook", "stop"]) == ["/abs/cwpilot", "hook", "stop"]


def test_shipped_cli_hooks_config_never_names_a_bare_cwpilot():
    """Every cmd in cw-plugin-cli-hooks.json must go through {cwpilot_bin}."""
    import json
    from pathlib import Path
    config = json.loads(
        (Path(__file__).resolve().parent.parent / "plugin" / "cw-plugin-cli-hooks.json").read_text()
    )
    cmds = [e["cmd"] for e in config["hook_cli_calls"].values() if "cmd" in e]
    assert cmds
    for cmd in cmds:
        assert cmd[0] == "{cwpilot_bin}", cmd
        assert "cwpilot" not in cmd[1:]
