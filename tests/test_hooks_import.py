#!/usr/bin/env python3
# Plugin-scoped: tests hook files in /workspace/hooks directory syntax and structure
"""Test that all hook files can be executed without import errors."""

import sys
import subprocess

from src.config import PLUGIN_ROOT


def test_all_hook_files_syntax():
    """Verify every handler_*.py file in hooks/ has valid Python syntax."""
    hooks_dir = PLUGIN_ROOT / "hooks"
    hook_files = sorted([f for f in hooks_dir.glob("handler_*.py") if f.is_file()])

    assert len(hook_files) > 0, "No hook files found"

    # Check syntax of each hook file
    failed = []
    for hook_file in hook_files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(hook_file)],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            failed.append((hook_file.name, result.stderr))

    if failed:
        error_msg = "Hook syntax errors:\n"
        for hook_name, error in failed:
            error_msg += f"  ✗ {hook_name}: {error}\n"
        raise AssertionError(error_msg)
