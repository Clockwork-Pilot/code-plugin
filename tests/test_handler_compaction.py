"""Compaction hook dispatch is fail-soft and registered like other handlers."""

import io

from hooks import __main__ as hook_main
from hooks import handler_compaction


def test_compaction_handler_invokes_configured_callback(monkeypatch):
    calls = []
    monkeypatch.setattr(handler_compaction, "invoke", lambda event: calls.append(event))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"trigger": "manual"}'))

    assert handler_compaction.main() == 0
    assert calls == ["call_on_compaction"]


def test_compaction_handler_fails_soft_when_callback_is_unavailable(monkeypatch):
    def unavailable(_event):
        raise RuntimeError("cwpilot unavailable")

    monkeypatch.setattr(handler_compaction, "invoke", unavailable)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    assert handler_compaction.main() == 0


def test_compaction_handler_is_registered_for_provider_event():
    assert "handler_compaction" in hook_main._HANDLERS
    assert hook_main._HANDLER_EVENTS["handler_compaction"] == "Compaction"
