"""dist/run-hook.sh: runs the installed hookrunner for THIS plugin's version, else the source."""

import json
import os
import subprocess
from pathlib import Path

import pytest

RUN_HOOK = Path(__file__).resolve().parent.parent / "plugin" / "dist" / "run-hook.sh"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.2.0"}))
    # Source fallback: `python3 -m hooks <handler>` run from the plugin root.
    (root / "hooks").mkdir()
    (root / "hooks" / "__init__.py").write_text("")
    (root / "hooks" / "__main__.py").write_text("import sys; print('source', *sys.argv[1:])\n")
    versions = tmp_path / "versions"
    versions.mkdir()
    return {
        "root": root,
        "versions": versions,
        "env": {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PLUGIN_ROOT": str(root),
            "CWPILOT_VERSIONS_DIR": str(versions),
        },
    }


def _fake_hookrunner(directory: Path, label: str) -> Path:
    binary = directory / "hookrunner"
    directory.mkdir(parents=True)
    binary.write_text(f"#!/bin/sh\necho {label} \"$@\"\n")
    binary.chmod(0o755)
    return binary


def _run(env, *args, extra=None):
    full = {**env["env"], **(extra or {})}
    return subprocess.run(["bash", str(RUN_HOOK), *args], env=full, capture_output=True, text=True)


def test_runs_the_hookrunner_installed_for_the_plugin_version(env):
    _fake_hookrunner(env["versions"] / "hookrunner-v1.2.0", "binary")
    result = _run(env, "handler_bash", "--x")
    assert result.stdout.strip() == "binary handler_bash --x"


def test_a_hookrunner_of_another_version_is_not_used(env):
    _fake_hookrunner(env["versions"] / "hookrunner-v1.1.0", "binary")
    result = _run(env, "handler_bash")
    assert result.stdout.strip() == "source handler_bash"


def test_falls_back_to_source_when_nothing_is_installed(env):
    assert _run(env, "handler_stop").stdout.strip() == "source handler_stop"


def test_a_non_executable_hookrunner_falls_back_to_source(env):
    _fake_hookrunner(env["versions"] / "hookrunner-v1.2.0", "binary").chmod(0o644)
    assert _run(env, "handler_stop").stdout.strip() == "source handler_stop"


def test_path_and_override_are_never_consulted(env, tmp_path):
    decoy = _fake_hookrunner(tmp_path / "decoy", "decoy")
    result = _run(env, "handler_stop", extra={
        "PATH": f"{decoy.parent}:{os.environ['PATH']}", "HOOKRUNNER_BIN": str(decoy)})
    assert result.stdout.strip() == "source handler_stop"


def test_codex_reads_its_own_manifest_first(env):
    (env["root"] / ".codex-plugin").mkdir()
    (env["root"] / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": "1.2.1"}))
    _fake_hookrunner(env["versions"] / "hookrunner-v1.2.1", "codex-binary")
    result = _run(env, "handler_stop", extra={"CWPILOT_AGENT_PROVIDER": "codex"})
    assert result.stdout.strip() == "codex-binary handler_stop"


def test_unset_plugin_root_is_an_error(env):
    full = {k: v for k, v in env["env"].items() if k != "CLAUDE_PLUGIN_ROOT"}
    result = subprocess.run(["bash", str(RUN_HOOK), "handler_stop"], env=full, capture_output=True, text=True)
    assert result.returncode == 1 and "unset" in result.stderr
