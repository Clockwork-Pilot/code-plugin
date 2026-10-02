"""Stop hook keep-warm poll: block on a live sub-agent, release the instant it finishes.

When cwpilot's stop decision returns a payload carrying ``keepCacheWarm:
{maxTimeoutSeconds: N}`` (the fallback for a provider with no ScheduleWakeup),
handler_stop must NOT relay-and-exit. It blocks synchronously, polling
session_main.json's ``running_agents`` map once a second, and releases -- re-emitting
the payload, exit 2 -- the instant any agent gains a ``finished_at``, or after N
seconds, whichever comes first.

A payload WITHOUT that field is relayed verbatim, exactly as today.

RED until:
  - src.hook_cli_calls grows run_and_get_output() -> (stdout, exit_code)
  - handler_stop.main() parses the payload and runs the poll loop
"""

import json
import logging
import threading
import time

import pytest

from hooks import handler_stop
from src.config import project_root
from src.ledger import session_ledger

_KEEP_WARM = json.dumps({
    "decision": "block",
    "reason": "1 work item(s) running -- a sub-agent is working.",
    "keepCacheWarm": {"maxTimeoutSeconds": 0.25},
})
_PLAIN_BLOCK = json.dumps({"decision": "block", "reason": "next move: approvals"})


def _seed_running_agent(agent_id="agent-poll-probe", finished=False):
    t = session_ledger()
    t.record_agent_start(agent_id)
    if finished:
        session_ledger().record_agent_finished(agent_id)


def _mark_finished_later(agent_id, delay):
    timer = threading.Timer(delay, lambda: session_ledger().record_agent_finished(agent_id))
    timer.daemon = True
    timer.start()
    return timer


@pytest.fixture(autouse=True)
def _fast_poll_interval(monkeypatch):
    """Keep timing behavior covered without making the suite wait seconds."""
    monkeypatch.setattr(handler_stop, "_POLL_INTERVAL_SECONDS", 0.01)


def _drive_stop(capsys, payload, exit_code=2):
    with pytest.raises(SystemExit) as exc:
        handler_stop.relay_decision(payload, exit_code)
    return capsys.readouterr().out, exc.value.code


class TestKeepCacheWarmPoll:
    def test_relay_event_logs_project_root_exactly_once(self, caplog):
        caplog.set_level(logging.INFO, logger=handler_stop.logger.name)

        with pytest.raises(SystemExit) as exc:
            handler_stop.relay_decision(_PLAIN_BLOCK, exit_code=2)

        assert exc.value.code == 2
        stop_records = [
            record.msg for record in caplog.records
            if record.name == handler_stop.logger.name
            and isinstance(record.msg, dict)
        ]
        relay_records = [
            record for record in stop_records
            if record.get('phase') == 'relay_decision'
        ]

        assert len(relay_records) == 1
        assert relay_records[0]['project_root'] == str(project_root())
        assert sum('project_root' in record for record in stop_records) == 1

    def test_releases_early_when_agent_finishes_mid_loop(self, capsys):
        _seed_running_agent(finished=False)
        _mark_finished_later("agent-poll-probe", delay=0.05)

        start = time.monotonic()
        out, code = _drive_stop(capsys, _KEEP_WARM)
        elapsed = time.monotonic() - start

        assert elapsed < 0.2, "must release when finished_at appears, not wait out maxTimeoutSeconds"
        assert code == 2
        assert "decision" in out

    def test_releases_at_deadline_when_agent_never_finishes(self, capsys):
        _seed_running_agent(finished=False)

        start = time.monotonic()
        _out, code = _drive_stop(capsys, _KEEP_WARM)
        elapsed = time.monotonic() - start

        assert 0.2 <= elapsed < 1.0, "must exit at maxTimeoutSeconds with an agent still unfinished"
        assert code == 2

    def test_passthrough_when_no_keep_cache_warm_field(self, capsys):
        _seed_running_agent(finished=False)

        start = time.monotonic()
        out, code = _drive_stop(capsys, _PLAIN_BLOCK, exit_code=2)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, "a payload with no keepCacheWarm field must not enter the poll loop"
        assert code == 2
        assert json.loads(out)["reason"] == "next move: approvals"

    def test_codex_keeps_supported_fields_and_drops_unsupported_fields(
        self, capsys, monkeypatch
    ):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "codex")
        supported = {
            "continue": True,
            "stopReason": "recorded stop reason",
            "systemMessage": "visible message",
            "suppressOutput": False,
            "decision": "block",
            "reason": "continue prompt",
        }
        payload = json.dumps({
            **supported,
            "terminalSequence": "\u0007",
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "Claude Code only",
            },
            "extra": "drop",
        })

        out, code = _drive_stop(capsys, payload, exit_code=0)

        assert code == 0
        assert json.loads(out) == supported

    @pytest.mark.parametrize("provider", ["codex", "claude-code"])
    def test_provider_polls_keep_warm_then_filters_handler_fields(
        self, capsys, monkeypatch, provider
    ):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", provider)
        polled = []
        monkeypatch.setattr(
            handler_stop, "_poll_until_subagent_finishes", polled.append
        )
        payload = json.dumps({
            "decision": "block",
            "reason": "wait for sub-agent",
            "keepCacheWarm": {"maxTimeoutSeconds": 0.25},
            "extra": "drop",
        })

        out, code = _drive_stop(capsys, payload, exit_code=0)

        assert code == 0
        assert polled == [0.25]
        assert json.loads(out) == {
            "decision": "block",
            "reason": "wait for sub-agent",
        }

    def test_claude_code_keeps_its_supported_fields_and_drops_unknown_fields(
        self, capsys, monkeypatch
    ):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "claude-code")
        supported = {
            "continue": True,
            "stopReason": "recorded stop reason",
            "suppressOutput": False,
            "systemMessage": "visible message",
            "terminalSequence": "\u0007",
            "decision": "block",
            "reason": "continue prompt",
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "continue with this feedback",
            },
        }
        payload = json.dumps({**supported, "extra": "drop"})

        out, code = _drive_stop(capsys, payload, exit_code=2)

        assert code == 2
        assert json.loads(out) == supported

    def test_unknown_provider_relay_preserves_unknown_fields(self, capsys, monkeypatch):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "other-provider")
        payload = '{"decision":"approve","systemMessage":"ok","extra":"keep"}'

        out, code = _drive_stop(capsys, payload, exit_code=0)

        assert code == 0
        assert out == payload

    @pytest.mark.parametrize("provider", ["codex", "claude-code"])
    @pytest.mark.parametrize("payload", ["not-json", '["not", "an", "object"]'])
    def test_provider_leaves_malformed_or_non_object_output_unchanged(
        self, capsys, monkeypatch, payload, provider
    ):
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", provider)

        out, code = _drive_stop(capsys, payload, exit_code=0)

        assert code == 0
        assert out == payload
