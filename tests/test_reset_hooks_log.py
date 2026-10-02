"""SessionStart resets the shared hook log before the new session begins."""

import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"


def test_reset_hooks_log_truncates_before_the_next_session(tmp_path):
    log_path = tmp_path / "test-plugin.log"
    env = {
        **os.environ,
        "PYTHONPATH": str(PLUGIN_ROOT),
        "CWPILOT_PLUGIN_LOG_FILE": str(log_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.hook_logging import setup_logger; "
                "logger = setup_logger('Stop'); logger.info('old'); "
                "setup_logger('SessionStart', reset_log=True).info('new')"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0].split(" - ", 3)[3]) == {
        "message": "new", "event": "SessionStart"
    }
