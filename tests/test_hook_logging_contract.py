"""Regression coverage for the hook log line contract."""

import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"


def _run_isolated(script: str, tmp_path: Path) -> list[dict]:
    log_path = tmp_path / "cwpilot-plugin-hooks.log"
    env = {
        **os.environ,
        "CWPILOT_PROJECT_ROOT": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "CLAUDE_PLUGIN_DATA": str(tmp_path),
        "PLUGIN_DATA": str(tmp_path),
        "CWPILOT_PLUGIN_LOG_FILE": str(log_path),
        "PYTHONPATH": str(PLUGIN_ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        json.loads(line.split(" - ", 3)[3])
        for line in log_path.read_text().splitlines()
    ]


def test_stop_cli_result_keeps_outer_event_and_full_invocation(tmp_path):
    records = _run_isolated(
        """
from hooks import handler_stop
from src import hook_cli_calls
import sys

handler_stop.logger.error("diagnostic")
hook_cli_calls.HOOK_CLI_CALLS = {
    "call_on_stop_hook": {
        "cmd": [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        "timeout": 5,
    },
}
hook_cli_calls.invoke_cli_call("call_on_stop_hook")
        """,
        tmp_path,
    )

    assert all(record.get("event") for record in records)
    diagnostic = next(record for record in records if record.get("message") == "diagnostic")
    assert diagnostic["event"] == "Stop"
    result = next(record for record in records if record.get("phase") == "cwpilot_result")
    assert result["event"] == "Stop"
    assert result["hook_cli_call"] == "call_on_stop_hook"
    assert result["exit_code"] == 3
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"
    assert result["invocation"]


def test_setup_logger_assigns_the_event_used_by_every_record(tmp_path):
    expected = {
        "bash": "Bash",
        "edit": "Edit",
        "post_change": "PostChange",
        "session_start": "SessionStart",
        "stop": "Stop",
        "subagent_start": "SubagentStart",
        "subagent_stop": "SubagentStop",
        "write": "Write",
    }
    for handler, event in expected.items():
        records = _run_isolated(
            f"""
from src.hook_logging import setup_logger

setup_logger("{event}").info({{"probe": "{handler}"}})
            """,
            tmp_path / handler,
        )
        assert records == [{"probe": handler, "event": event}]


def test_write_cli_result_uses_write_event_before_first_handler_log(tmp_path):
    records = _run_isolated(
        """
from src.hook_logging import setup_logger
from src import hook_cli_calls
import sys

setup_logger("Write")
hook_cli_calls.HOOK_CLI_CALLS = {
    "subagent_sync_call_on_edit_write": {
        "cmd": [sys.executable, "-c", "print('heartbeat')"],
        "timeout": 5,
    },
}
hook_cli_calls.invoke_cli_call("subagent_sync_call_on_edit_write")
        """,
        tmp_path,
    )

    result = next(record for record in records if record.get("phase") == "cwpilot_result")
    assert result["event"] == "Write"
    assert result["hook_cli_call"] == "subagent_sync_call_on_edit_write"


def test_setup_logger_does_not_dump_the_environment(tmp_path):
    records = _run_isolated(
        """
import os
from src.hook_logging import setup_logger

os.environ["CWPILOT_TEST_LOG_ENVS"] = "visible"
setup_logger("Bash").info("explicit")
        """,
        tmp_path,
    )

    assert records == [{"message": "explicit", "event": "Bash"}]


def test_setup_logger_logs_and_redacts_session_environment(tmp_path):
    records = _run_isolated(
        """
import os
from src.hook_logging import setup_logger

os.environ["CWPILOT_TEST_PROJECT_ROOT"] = "/project"
os.environ["CWPILOT_TEST_PLUGIN_ROOT"] = "/plugin"
os.environ["CWPILOT_TEST_SESSION_ID"] = "secret-session-id"
os.environ["CWPILOT_TEST_PLUGIN_SESSION_TOKEN"] = "secret-token"
os.environ["CWPILOT_TEST_UNRELATED"] = "hidden"
setup_logger("SessionStart", log_envs=True)
        """,
        tmp_path,
    )

    environment = records[0]["environment"]
    assert environment["CWPILOT_TEST_PROJECT_ROOT"] == "/project"
    assert environment["CWPILOT_TEST_PLUGIN_ROOT"] == "/plugin"
    assert environment["CWPILOT_TEST_SESSION_ID"] == "<reducted>"
    assert environment["CWPILOT_TEST_PLUGIN_SESSION_TOKEN"] == "<reducted>"
    assert "CWPILOT_TEST_UNRELATED" not in environment


def test_hook_input_log_item_redacts_session_id(tmp_path):
    records = _run_isolated(
        """
from src.hook_logging import redact_session_fields, setup_logger

logger = setup_logger("SessionStart", log_envs=True)
logger.info({"hook_input": redact_session_fields({
    "session_id": "secret-hook-session",
    "cwd": "/project",
})})
        """,
        tmp_path,
    )

    assert records[0]["environment"]
    assert records[1]["hook_input"] == {
        "session_id": "<reducted>",
        "cwd": "/project",
    }


def test_redact_session_fields_preserves_other_hook_data():
    from src.hook_logging import redact_session_fields

    assert redact_session_fields({
        "session_id": "secret",
        "sessionId": "secret-camel-case",
        "cwd": "/project",
        "nested": [{"SESSION_TOKEN": "secret-token", "value": 1}],
    }) == {
        "session_id": "<reducted>",
        "sessionId": "<reducted>",
        "cwd": "/project",
        "nested": [{"SESSION_TOKEN": "secret-token", "value": 1}],
    }
