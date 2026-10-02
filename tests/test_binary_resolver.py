"""binary_resolver: where install.sh's binaries are, and whether their stored signature holds."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import binary_resolver


@pytest.fixture
def versions(tmp_path, monkeypatch):
    """An empty, private versions dir; never the machine's real one."""
    d = tmp_path / "versions"
    d.mkdir()
    monkeypatch.setenv("CWPILOT_VERSIONS_DIR", str(d))
    return d


@pytest.fixture
def plugin_root(tmp_path, monkeypatch):
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    monkeypatch.setattr(binary_resolver, "PLUGIN_ROOT", root)
    monkeypatch.delenv("CWPILOT_AGENT_PROVIDER", raising=False)
    return root


def _manifest(root: Path, directory: str, version: str) -> None:
    (root / directory).mkdir(exist_ok=True)
    (root / directory / "plugin.json").write_text(json.dumps({"version": version}))


def _install(versions: Path, dirname: str, name: str, executable: bool = True) -> Path:
    binary = versions / dirname / name
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755 if executable else 0o644)
    return binary


class TestPluginVersion:
    def test_reads_the_claude_manifest(self, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.0.0")
        assert binary_resolver.plugin_version() == "1.0.0"

    def test_codex_prefers_its_own_manifest_and_falls_back_to_the_other(self, plugin_root, monkeypatch):
        _manifest(plugin_root, ".claude-plugin", "1.0.0")
        _manifest(plugin_root, ".codex-plugin", "1.0.1")
        monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "codex")
        assert binary_resolver.plugin_version() == "1.0.1"
        shutil.rmtree(plugin_root / ".codex-plugin")
        assert binary_resolver.plugin_version() == "1.0.0"

    def test_unreadable_is_empty_not_an_error(self, plugin_root):
        assert binary_resolver.plugin_version() == ""


class TestFindCwpilot:
    def test_newest_patch_of_the_plugins_own_minor_wins(self, versions, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.2.0")
        _install(versions, "v1.2.3", "cwpilot")
        newest = _install(versions, "v1.2.10", "cwpilot")  # numeric, not lexicographic
        assert binary_resolver.find_cwpilot() == str(newest)

    def test_the_plugins_patch_does_not_affect_which_cwpilot_matches(self, versions, plugin_root):
        """Only major.minor decides compatibility: a plugin 1.2.9 uses a cwpilot v1.2.0."""
        _manifest(plugin_root, ".claude-plugin", "1.2.9")
        installed = _install(versions, "v1.2.0", "cwpilot")
        assert binary_resolver.find_cwpilot() == str(installed)

    def test_another_family_never_wins_even_when_newer(self, versions, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.2")
        mine = _install(versions, "v1.2.3", "cwpilot")
        _install(versions, "v2.0.0", "cwpilot")
        _install(versions, "v1.3.9", "cwpilot")
        assert binary_resolver.find_cwpilot() == str(mine)

    def test_unknown_plugin_version_takes_the_newest_of_any_family(self, versions, plugin_root):
        _install(versions, "v1.2.3", "cwpilot")
        newest = _install(versions, "v2.0.0", "cwpilot")
        assert binary_resolver.find_cwpilot() == str(newest)

    def test_a_non_executable_file_is_not_a_binary(self, versions, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.2")
        _install(versions, "v1.2.3", "cwpilot", executable=False)
        assert binary_resolver.find_cwpilot() is None

    def test_other_entries_in_the_versions_dir_are_ignored(self, versions, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.2")
        _install(versions, "hookrunner-v1.2", "hookrunner")
        (versions / "signing.pub").write_text("key")
        assert binary_resolver.find_cwpilot() is None

    def test_missing_versions_dir_is_none(self, tmp_path, monkeypatch, plugin_root):
        monkeypatch.setenv("CWPILOT_VERSIONS_DIR", str(tmp_path / "absent"))
        assert binary_resolver.find_cwpilot() is None

    def test_path_and_cwpilot_bin_are_never_consulted(self, versions, plugin_root, tmp_path, monkeypatch):
        decoy = tmp_path / "bin" / "cwpilot"
        decoy.parent.mkdir()
        decoy.write_text("#!/bin/sh\n")
        decoy.chmod(0o755)
        monkeypatch.setenv("PATH", str(decoy.parent))
        monkeypatch.setenv("CWPILOT_BIN", str(decoy))
        assert binary_resolver.find_cwpilot() is None


class TestFindHookrunner:
    def test_is_keyed_by_the_plugin_version(self, versions, plugin_root):
        _manifest(plugin_root, ".claude-plugin", "1.2.0")
        _install(versions, "hookrunner-v1.1.0", "hookrunner")
        mine = _install(versions, "hookrunner-v1.2.0", "hookrunner")
        assert binary_resolver.find_hookrunner() == str(mine)

    def test_missing_or_unknown_version_is_none(self, versions, plugin_root):
        assert binary_resolver.find_hookrunner() is None  # no plugin version
        _manifest(plugin_root, ".claude-plugin", "1.2")
        assert binary_resolver.find_hookrunner() is None  # not installed

    def test_path_and_override_are_never_consulted(self, versions, plugin_root, tmp_path, monkeypatch):
        _manifest(plugin_root, ".claude-plugin", "1.2")
        decoy = tmp_path / "bin" / "hookrunner"
        decoy.parent.mkdir()
        decoy.write_text("#!/bin/sh\n")
        decoy.chmod(0o755)
        monkeypatch.setenv("PATH", str(decoy.parent))
        monkeypatch.setenv("HOOKRUNNER_BIN", str(decoy))
        assert binary_resolver.find_hookrunner() is None


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture
def signed(versions, tmp_path):
    """A binary signed the way the release workflow does, with its key and sig stored
    where install.sh stores them."""
    priv = tmp_path / "priv.pem"
    _openssl("genrsa", "-out", str(priv), "2048")
    _openssl("rsa", "-in", str(priv), "-pubout", "-out", str(versions / "signing.pub"))
    binary = _install(versions, "v1.2.3", "cwpilot")
    _openssl("dgst", "-sha256", "-sign", str(priv), "-out", str(binary.with_name("cwpilot.sig")), str(binary))
    return binary


@needs_openssl
class TestVerifySignature:
    def test_a_genuine_binary_verifies(self, signed):
        assert binary_resolver.verify_signature(signed) is True

    def test_a_modified_binary_is_a_positive_mismatch(self, signed):
        signed.write_text("#!/bin/sh\necho tampered\n")
        assert binary_resolver.verify_signature(signed) is False

    def test_a_signature_from_another_key_is_a_positive_mismatch(self, signed, tmp_path):
        other = tmp_path / "other.pem"
        _openssl("genrsa", "-out", str(other), "2048")
        _openssl("dgst", "-sha256", "-sign", str(other),
                 "-out", str(signed.with_name("cwpilot.sig")), str(signed))
        assert binary_resolver.verify_signature(signed) is False

    def test_a_deleted_signature_fails_it_does_not_switch_the_check_off(self, signed):
        """Replacing the binary and deleting its .sig must not pass."""
        signed.write_text("#!/bin/sh\necho tampered\n")
        signed.with_name("cwpilot.sig").unlink()
        assert binary_resolver.verify_signature(signed) is False

    def test_a_missing_key_fails(self, signed, versions):
        (versions / "signing.pub").unlink()
        assert binary_resolver.verify_signature(signed) is False

    def test_signature_problem_names_which_half_is_missing(self, signed, versions):
        assert binary_resolver.signature_problem(signed) is None
        (versions / "signing.pub").unlink()
        problem = binary_resolver.signature_problem(signed)
        assert "signing.pub" in problem and "cwpilot.sig" not in problem
        signed.with_name("cwpilot.sig").unlink()
        both = binary_resolver.signature_problem(signed)
        assert "cwpilot.sig" in both and "signing.pub" in both

    def test_a_missing_binary_cannot_tell(self, versions):
        assert binary_resolver.verify_signature(versions / "v9.9.9" / "cwpilot") is None

    def test_an_explicit_signature_path_is_honoured(self, signed, tmp_path):
        moved = tmp_path / "elsewhere.sig"
        signed.with_name("cwpilot.sig").rename(moved)
        assert binary_resolver.verify_signature(signed) is False  # default path now empty
        assert binary_resolver.verify_signature(signed, signature=moved) is True


def test_missing_openssl_cannot_tell(signed, monkeypatch):
    def no_openssl(*args, **kwargs):
        raise FileNotFoundError("openssl")

    monkeypatch.setattr(binary_resolver.subprocess, "run", no_openssl)
    assert binary_resolver.verify_signature(signed) is None
