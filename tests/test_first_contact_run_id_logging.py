"""handler_bash logs the first-contact / run-id-resolution call.

When a sub-agent runs its first ``cwpilot play show <run_id> --source``, handler_bash
(PreToolUse) fires the ``subagent_call_on_run_id_resolved`` binding -- the SOLE writer
of dispatched_at + agent_id on the run. The run_id is parsed out of that command, so
it is always known at that moment; this log record makes that verifiable from
hooks_log rather than argued about.

RED until handler_bash emits the record at that call site.
"""

import io
import json
import logging

import pytest

from hooks import handler_bash

_RUN_ID = "run_impl_first_contact_abc123"
_AGENT_ID = "agent-first-contact-probe"


def _run_first_contact(monkeypatch, command):
    monkeypatch.setattr(handler_bash, "invoke_and_get_payload", lambda *a, **k: {})
    monkeypatch.setattr(handler_bash, "invoke_throttled_sync", lambda *a, **k: None)
    monkeypatch.setattr(handler_bash, "invoke_hook_cli", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "agent_id": _AGENT_ID,
        "tool_input": {"command": command},
    })))
    with pytest.raises(SystemExit):
        handler_bash.main()


def test_first_contact_record_carries_the_resolved_run_id(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="hooks.handler_bash"):
        _run_first_contact(monkeypatch, f"cwpilot play show {_RUN_ID} --source")

    text = caplog.text
    assert "subagent_run_id_resolved" in text, (
        "the first-contact call site must emit a log record so run_id presence is auditable"
    )
    assert _RUN_ID in text, "the record must carry the resolved run_id"
    assert _AGENT_ID in text


def test_no_first_contact_record_when_command_names_no_run(monkeypatch, caplog):
    """A bash command that is not a play-show invocation resolves no run_id, so the
    first-contact record must not fire."""
    with caplog.at_level(logging.INFO, logger="hooks.handler_bash"):
        _run_first_contact(monkeypatch, "pytest -q tests/")

    assert "subagent_run_id_resolved" not in caplog.text
