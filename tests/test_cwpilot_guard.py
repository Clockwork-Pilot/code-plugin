"""cwpilot_guard: an agent may run only the plugin's resolved, signature-verified cwpilot."""

import os

import pytest

from src import cwpilot_guard


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A fake versioned install the resolver 'finds', and a private verify cache."""
    binary = tmp_path / "v1.2.3" / "cwpilot"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(cwpilot_guard, "find_cwpilot", lambda: str(binary))
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    return binary


def _verdict(monkeypatch, value, calls=None):
    def fake(path):
        if calls is not None:
            calls.append(str(path))
        return value
    monkeypatch.setattr(cwpilot_guard.binary_resolver, "verify_signature", fake)


@pytest.fixture
def no_path_cwpilot(monkeypatch, tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


class TestPathLookupIsRefused:
    def test_bare_name(self, installed, no_path_cwpilot):
        assert str(installed) in cwpilot_guard.check_command("cwpilot pull")

    def test_bare_name_resolving_to_a_different_binary_is_refused(
            self, installed, tmp_path, monkeypatch):
        other = tmp_path / "pathbin" / "cwpilot"
        other.parent.mkdir()
        other.write_text("x")
        other.chmod(0o755)
        monkeypatch.setenv("PATH", str(other.parent))
        assert str(installed) in cwpilot_guard.check_command("cwpilot pull")

    def test_bare_name_resolving_to_the_resolved_binary_is_allowed(
            self, installed, tmp_path, monkeypatch):
        """Bare `cwpilot` is fine exactly while $PATH reaches the verified binary."""
        _verdict(monkeypatch, True)
        bindir = tmp_path / "pathbin"
        bindir.mkdir()
        (bindir / "cwpilot").symlink_to(installed)
        monkeypatch.setenv("PATH", str(bindir))
        assert cwpilot_guard.check_command("cwpilot pull") is None

    @pytest.mark.parametrize("cmd", [
        "FOO=1 cwpilot pull", "env cwpilot pull", "cd x && cwpilot pull",
        "echo hi | cwpilot pull", "bash -c 'cwpilot pull'", "sudo -n cwpilot pull",
    ])
    def test_through_wrappers_and_shells(self, installed, no_path_cwpilot, cmd):
        assert cwpilot_guard.check_command(cmd)

    @pytest.mark.parametrize("cmd", [
        "grep cwpilot README.md", "echo cwpilot", "cat cwpilot.txt", "ls", "",
    ])
    def test_a_mere_mention_is_allowed(self, installed, cmd):
        assert cwpilot_guard.check_command(cmd) is None


class TestResolvedBinary:
    def test_the_resolved_absolute_path_is_allowed(self, installed, monkeypatch):
        _verdict(monkeypatch, True)
        assert cwpilot_guard.check_command(f"{installed} pull") is None

    def test_a_symlink_to_the_resolved_binary_is_refused_as_a_path(self, installed, tmp_path, monkeypatch):
        """As a PATH, only the resolved versioned path is accepted. Even a symlink
        that points at that very file (the installer's ~/.local/bin/cwpilot) is refused,
        and the refusal says which path to use."""
        _verdict(monkeypatch, True)
        link = tmp_path / "cwpilot"
        link.symlink_to(installed)
        reason = cwpilot_guard.check_command(f"{link} pull")
        assert reason and str(installed) in reason and "never use ~/.local/bin/cwpilot" in reason

    def test_a_symlink_to_another_version_is_refused(self, installed, tmp_path):
        other = tmp_path / "v9.9.9" / "cwpilot"
        other.parent.mkdir()
        other.write_text("x")
        link = tmp_path / "cwpilot"
        link.symlink_to(other)
        assert str(installed) in cwpilot_guard.check_command(f"{link} pull")

    def test_a_dotdot_spelling_of_the_resolved_path_is_allowed(self, installed, monkeypatch):
        _verdict(monkeypatch, True)
        assert cwpilot_guard.check_command(f"{installed.parent}/../v1.2.3/cwpilot pull") is None

    def test_a_different_binary_is_refused(self, installed, tmp_path):
        other = tmp_path / "other" / "cwpilot"
        other.parent.mkdir()
        other.write_text("x")
        assert cwpilot_guard.check_command(f"{other} pull")

    def test_nothing_installed_refuses_every_form(self, monkeypatch, no_path_cwpilot):
        monkeypatch.setattr(cwpilot_guard, "find_cwpilot", lambda: None)
        assert cwpilot_guard.check_command("cwpilot pull")
        assert cwpilot_guard.check_command("/x/cwpilot pull")


class TestIntegrity:
    def test_a_positive_mismatch_is_refused(self, installed, monkeypatch):
        _verdict(monkeypatch, False)
        assert "does not match its stored signature" in cwpilot_guard.check_command(f"{installed} pull")

    def test_a_binary_with_no_stored_signature_is_refused(self, installed, tmp_path, monkeypatch):
        """Real resolver, nothing patched: no .sig/key beside the binary means unverified,
        so deleting the signature cannot be a way past the guard."""
        monkeypatch.setenv("CWPILOT_VERSIONS_DIR", str(tmp_path))
        reason = cwpilot_guard.check_command(f"{installed} pull")
        assert reason and "or has none" in reason

    def test_cannot_tell_fails_soft(self, installed, monkeypatch):
        _verdict(monkeypatch, None)
        assert cwpilot_guard.check_command(f"{installed} pull") is None

    def test_a_good_result_is_computed_once_until_the_file_changes(self, installed, monkeypatch):
        calls = []
        _verdict(monkeypatch, True, calls)
        cwpilot_guard.check_command(f"{installed} a")
        cwpilot_guard.check_command(f"{installed} b")
        assert len(calls) == 1
        installed.write_text("#!/bin/sh\n# tampered\n")
        os.utime(installed, ns=(1, 1))
        _verdict(monkeypatch, False, calls)
        assert cwpilot_guard.check_command(f"{installed} c")
        assert len(calls) == 2


class TestWiredIntoPreToolUse:
    def _run(self, monkeypatch, command, agent_id):
        import io
        import json
        from hooks import handler_bash
        monkeypatch.setattr(handler_bash, "check_cwpilot_command", cwpilot_guard.check_command)
        monkeypatch.setattr(handler_bash, "invoke_and_get_payload", lambda *a, **k: None)
        monkeypatch.setattr(handler_bash, "invoke_throttled_sync", lambda *a, **k: None)
        monkeypatch.setattr(handler_bash, "invoke_hook_cli", lambda *a, **k: None)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "hook_event_name": "PreToolUse", "agent_id": agent_id,
            "tool_input": {"command": command},
        })))
        with pytest.raises(SystemExit) as exit_info:
            handler_bash.main()
        return exit_info.value.code

    @pytest.mark.parametrize("agent_id", [None, "agent-1"])
    def test_blocks_bare_cwpilot_for_main_session_and_subagents(
            self, installed, no_path_cwpilot, monkeypatch, agent_id):
        assert self._run(monkeypatch, "cwpilot pull", agent_id) == 2

    def test_allows_the_resolved_binary(self, installed, monkeypatch):
        _verdict(monkeypatch, True)
        assert self._run(monkeypatch, f"{installed} pull", None) == 0


class TestSpellingsThatHideTheName:
    @pytest.mark.parametrize("cmd", [
        "~/.local/bin/cwpilot pull", "$HOME/.local/bin/cwpilot pull",
        '"$HOME"/.local/bin/cwpilot pull', "${HOME}/.local/bin/cwpilot pull",
        "~/.local/bin/../bin/cwpilot pull",
    ])
    def test_the_local_bin_symlink_is_refused_however_home_is_spelled(
            self, installed, tmp_path, monkeypatch, cmd):
        _verdict(monkeypatch, True)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        (home / ".local" / "bin" / "cwpilot").symlink_to(installed)
        monkeypatch.setenv("HOME", str(home))
        reason = cwpilot_guard.check_command(cmd)
        assert reason and str(installed) in reason

    @pytest.mark.parametrize("pattern", ["cwpil*", "cw?ilot", "cw[p]ilot"])
    def test_a_glob_that_expands_to_cwpilot_is_refused(self, installed, tmp_path, pattern):
        other = tmp_path / "bin"
        other.mkdir()
        (other / "cwpilot").write_text("x")
        assert cwpilot_guard.check_command(f"{other}/{pattern} pull")

    @pytest.mark.parametrize("cmd", ["$(which cwpilot) pull", "`which cwpilot` pull",
                                     "C=$(which cwpilot); $C pull",
                                     "export C=$(which cwpilot); $C pull"])
    def test_an_unresolvable_command_naming_cwpilot_is_refused(self, installed, cmd):
        assert "Cannot tell" in cwpilot_guard.check_command(cmd)

    def test_an_unresolvable_command_not_naming_cwpilot_is_allowed(self, installed):
        assert cwpilot_guard.check_command("$(git rev-parse --show-toplevel)/run.sh") is None

    @pytest.mark.parametrize("cmd", [
        "grep cwpilot notes.txt && $(pwd)/check.sh",
        "echo cwpilot; $X",
        "$(git rev-parse --show-toplevel)/run.sh --tool cwpilot",
    ])
    def test_a_mention_of_cwpilot_elsewhere_does_not_condemn_an_expansion(self, installed, cmd):
        assert cwpilot_guard.check_command(cmd) is None

    def test_a_variable_holding_the_resolved_path_is_allowed(self, installed, monkeypatch):
        _verdict(monkeypatch, True)
        assert cwpilot_guard.check_command(f"C={installed}; $C pull") is None
        assert cwpilot_guard.check_command(f"C={installed} && ${{C}} pull") is None

    def test_a_variable_holding_another_path_is_refused(self, installed, tmp_path):
        reason = cwpilot_guard.check_command(f"C={tmp_path}/other/cwpilot; $C pull")
        assert reason and str(installed) in reason

    def test_a_variable_holding_bare_cwpilot_is_judged_as_bare(self, installed, no_path_cwpilot):
        assert "not available on $PATH" in cwpilot_guard.check_command("C=cwpilot; $C pull")
