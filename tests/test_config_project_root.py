"""Project-root precedence for the hook configuration."""

import os
import json
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"


def _load_project_root(tmp_path, **roots):
    """Import config in a clean process with only the requested root variables."""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PLUGIN_ROOT),
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        }
    )
    for name in ("CWPILOT_PROJECT_ROOT", "CLAUDE_PROJECT_DIR", "PROJECT_ROOT"):
        env.pop(name, None)
    env.update({name: str(value) for name, value in roots.items() if value is not None})

    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.config import PROJECT_ROOT; print(PROJECT_ROOT)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cwpilot_project_root_has_highest_precedence(tmp_path):
    cwpilot_root = tmp_path / "cwpilot"
    claude_root = tmp_path / "claude"
    generic_root = tmp_path / "generic"

    result = _load_project_root(
        tmp_path,
        CWPILOT_PROJECT_ROOT=cwpilot_root,
        CLAUDE_PROJECT_DIR=claude_root,
        PROJECT_ROOT=generic_root,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == cwpilot_root.resolve()


def test_claude_project_dir_is_next_fallback(tmp_path):
    claude_root = tmp_path / "claude"
    result = _load_project_root(
        tmp_path,
        CLAUDE_PROJECT_DIR=claude_root,
        PROJECT_ROOT=tmp_path / "generic",
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == claude_root.resolve()


def test_project_root_is_last_environment_fallback(tmp_path):
    generic_root = tmp_path / "generic"
    result = _load_project_root(tmp_path, PROJECT_ROOT=generic_root)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == generic_root.resolve()


def test_missing_project_root_fails_explicitly(tmp_path):
    result = _load_project_root(tmp_path)

    assert result.returncode != 0
    assert "Cannot resolve project root" in result.stderr


def test_hook_input_provides_project_fallback(tmp_path):
    project_root = tmp_path / "hook-project"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
    })
    for name in (
        "CWPILOT_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "PROJECT_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.config import set_hook_input, project_root; "
                f"set_hook_input({{'cwd': {str(project_root)!r}}}); "
                "print(project_root())"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(project_root.resolve())]


def test_environment_root_beats_hook_input(tmp_path):
    env_project = tmp_path / "env-project"
    hook_project = tmp_path / "hook-project"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CWPILOT_PROJECT_ROOT": str(env_project),
    })
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("PROJECT_ROOT", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.config import set_hook_input, project_root; "
                f"set_hook_input({{'cwd': {str(hook_project)!r}}}); "
                "print(project_root())"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(env_project.resolve())]


def test_hook_dispatcher_uses_cwd_for_project_owned_ledger(tmp_path):
    """The real hook entrypoint primes cwd before importing a project-root consumer."""
    project_root = tmp_path / "hook-project"
    plugin_data = tmp_path / "plugin-data"
    log_path = tmp_path / "hooks.log"
    project_root.mkdir()
    plugin_data.mkdir()

    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_DATA": str(plugin_data),
        "CWPILOT_PLUGIN_LOG_FILE": str(log_path),
    })
    for name in (
        "CWPILOT_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "PROJECT_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-m", "hooks", "handler_subagent_start"],
        cwd=PLUGIN_ROOT,
        input=json.dumps({
            "cwd": str(project_root),
            "agent_id": "dispatcher-project-root-test",
            "session_id": "secret-session-id",
        }),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (project_root / ".changes-tracker" / "session_main.json").is_file()
    records = [
        json.loads(line.split(" - ", 3)[3])
        for line in log_path.read_text().splitlines()
    ]
    hook_input = next(record["hook_input"] for record in records if "hook_input" in record)
    assert hook_input["cwd"] == str(project_root)
    assert hook_input["session_id"] == "<reducted>"


def test_missing_plugin_data_fails_explicitly(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PLUGIN_ROOT),
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CWPILOT_PROJECT_ROOT": str(tmp_path / "project"),
        }
    )
    for name in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA"):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.config import PLUGIN_DATA; print(PLUGIN_DATA)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Cannot resolve plugin data directory" in result.stderr


def test_log_is_created_when_project_root_is_missing(tmp_path):
    """Logging must survive strict project-root resolution failures."""
    plugin_data = tmp_path / "plugin-data"
    log_path = plugin_data / "cwpilot-plugin-hooks.log"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_DATA": str(plugin_data),
    })
    env.pop("CWPILOT_PLUGIN_LOG_FILE", None)
    for name in (
        "CWPILOT_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "PROJECT_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import hooks; "
                "from src.hook_logging import setup_logger; "
                "setup_logger('Test').info({'probe': 'created'})"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log_path.is_file()
    assert '"probe": "created"' in log_path.read_text()


def test_log_requires_configured_plugin_data(tmp_path):
    """Logging must fail explicitly when no configured plugin-data path exists."""
    log_path = tmp_path / "cwpilot-plugin-hooks.log"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "CWPILOT_PROJECT_ROOT": str(tmp_path / "project"),
        "TMPDIR": str(tmp_path),
    })
    for name in ("CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "CWPILOT_PLUGIN_LOG_FILE"):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import hooks; "
                "from src.hook_logging import setup_logger; "
                "setup_logger('Test').info({'probe': 'missing-config'})"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Cannot resolve plugin data directory" in result.stderr
    assert not log_path.exists()


def test_failed_hook_logs_project_root_resolution_error(tmp_path):
    """A hook import failure must still leave a diagnostic in the plugin log."""
    plugin_data = tmp_path / "plugin-data"
    log_path = plugin_data / "cwpilot-plugin-hooks.log"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PLUGIN_ROOT),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PLUGIN_DATA": str(plugin_data),
    })
    env.pop("CWPILOT_PLUGIN_LOG_FILE", None)
    for name in (
        "CWPILOT_PROJECT_ROOT",
        "CLAUDE_PROJECT_DIR",
        "PROJECT_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-m", "hooks", "handler_stop"],
        cwd=PLUGIN_ROOT,
        input="{}\n",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert log_path.is_file()
    log_text = log_path.read_text()
    assert '"phase": "hook_start"' in log_text
    assert "Cannot resolve project root" in result.stderr
