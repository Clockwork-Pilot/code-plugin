"""dist/cwpilot-path.sh -> handler_cwpilot_path: prints the cwpilot the guard would accept."""

import json
import os
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
SCRIPT = PLUGIN / "dist" / "cwpilot-path.sh"
VERSION = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
FAMILY = ".".join(VERSION.split(".")[:2])


@pytest.fixture
def env(tmp_path):
    """The real plugin (so the source fallback has hooks/ and src/), a scratch versions dir."""
    versions = tmp_path / "versions"
    versions.mkdir()
    return {"versions": versions, "env": {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "CWPILOT_VERSIONS_DIR": str(versions),
        "CWPILOT_PYTHON": sys.executable,
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
    }}


def _install(env, version, executable=True):
    binary = env["versions"] / f"v{version}" / "cwpilot"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755 if executable else 0o644)
    return binary


def _sign(env, binary):
    """Sign `binary` with a throwaway key, laid out the way install.sh stores it."""
    key = env["versions"] / "signing.key"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out",
                    str(env["versions"] / "signing.pub")], check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), "-out",
                    str(binary) + ".sig", str(binary)], check=True, capture_output=True)


def _run(env, extra=None):
    return subprocess.run(["bash", str(SCRIPT)], env={**env["env"], **(extra or {})},
                          capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_prints_the_newest_patch_of_the_plugins_major_minor(env):
    _install(env, f"{FAMILY}.3")
    newest = _install(env, f"{FAMILY}.10")
    _install(env, "9.3.0")
    _install(env, "9.0.0")
    _sign(env, newest)
    result = _run(env)
    assert result.returncode == 0
    assert result.stdout == f"{newest}\n"


def test_nothing_installed_exits_1_with_nothing_on_stdout(env):
    result = _run(env)
    assert result.returncode == 1
    assert result.stdout == ""


def test_another_family_or_non_executable_is_not_a_match(env):
    _install(env, "9.3.0")
    _install(env, f"{FAMILY}.0", executable=False)
    assert _run(env).returncode == 1


def test_path_and_cwpilot_bin_are_never_consulted(env, tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "cwpilot").write_text("#!/bin/sh\n")
    (decoy / "cwpilot").chmod(0o755)
    result = _run(env, {"PATH": f"{decoy}:{os.environ['PATH']}",
                        "CWPILOT_BIN": str(decoy / "cwpilot")})
    assert result.returncode == 1


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_valid_signature_passes(env):
    binary = _install(env, f"{FAMILY}.0")
    _sign(env, binary)
    assert _run(env).stdout == f"{binary}\n"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_missing_signature_exits_2(env):
    _install(env, f"{FAMILY}.0")
    result = _run(env)
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_tampered_binary_exits_2(env):
    binary = _install(env, f"{FAMILY}.0")
    _sign(env, binary)
    binary.write_text("#!/bin/sh\necho tampered\n")
    assert _run(env).returncode == 2


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_prints_plain_cwpilot_when_path_reaches_the_trusted_binary(env, tmp_path):
    binary = _install(env, f"{FAMILY}.0")
    _sign(env, binary)
    on_path = tmp_path / "bin"
    on_path.mkdir()
    (on_path / "cwpilot").symlink_to(binary)
    result = _run(env, {"PATH": f"{on_path}:{os.environ['PATH']}"})
    assert (result.returncode, result.stdout) == (0, "cwpilot\n")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_prints_the_full_path_when_path_has_a_different_cwpilot(env, tmp_path):
    binary = _install(env, f"{FAMILY}.0")
    _sign(env, binary)
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    (elsewhere / "cwpilot").write_text("#!/bin/sh\n")
    (elsewhere / "cwpilot").chmod(0o755)
    result = _run(env, {"PATH": f"{elsewhere}:{os.environ['PATH']}"})
    assert result.stdout == f"{binary}\n"


def test_failure_names_the_install_command_for_the_plugin_version(env):
    result = _run(env)
    assert result.returncode == 1
    assert f"curl -fsSL install.clockwork-pilot.com | sh -s -- {VERSION}" in result.stderr


def test_works_without_the_harness_environment(env):
    """The agent's shell has no CLAUDE_PLUGIN_*: the wrapper derives them from its location."""
    for name in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"):
        env["env"].pop(name)
    assert _run(env).returncode == 1  # a clean "not installed", not a config crash

