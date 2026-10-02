"""SessionStart's binary notice and cwpilot presence-check behavior."""

import io
import json
import os

import pytest

from hooks import handler_session_start

# Captured at import time, before neutral_cwpilot_compat's autouse patch ever runs,
# so the one test that exercises the real implementation can restore it.
_REAL_PLUGIN_REQUIRED_CWPILOT_MINOR = handler_session_start._plugin_required_cwpilot_minor
_REAL_PLUGIN_VERSION = handler_session_start.binary_resolver.plugin_version


class _Payload(dict):
    """The status-bearing mapping returned by the hook CLI seam."""

    def __init__(self, exit_code):
        super().__init__()
        self.exit_code = exit_code


@pytest.fixture(autouse=True)
def neutral_cwpilot_compat(monkeypatch):
    """The compat gate must never fire for a test that isn't testing it -- pin the
    required minor to undetermined (None) by default, same reasoning as
    test_binary_resolver.py's neutral_versions_dir: every test that seeds a
    CWPILOT_BIN under install.sh's v<version>/cwpilot layout (e.g.
    _seed_versioned_cwpilot) would otherwise be compared against THIS machine's real
    plugin.json version and could spuriously block. Tests that actually exercise the
    gate override this."""
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: None)


@pytest.fixture(autouse=True)
def neutral_binaries(monkeypatch, tmp_path):
    """No test may depend on what is installed on the machine running it: the versions
    directory points at an empty temp dir, hookrunner counts as installed (so its
    notice stays out of the way unless a test asks for it), the plugin version is
    undetermined and no signature can be checked. Tests exercising any of those
    override this explicitly."""
    monkeypatch.setenv("CWPILOT_VERSIONS_DIR", str(tmp_path / "no-versions"))
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "find_hookrunner", lambda: "/fake/hookrunner"
    )
    monkeypatch.setattr(handler_session_start.binary_resolver, "plugin_version", lambda: "")
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "verify_signature",
        lambda path, signature=None: None,
    )


@pytest.fixture(autouse=True)
def neutral_cwpilot_path(monkeypatch):
    """find_cwpilot's $PATH and versioned-install fallback must never depend on
    what happens to be installed on the machine actually running these tests --
    same reasoning as test_binary_resolver.py's neutral_versions_dir. Defaults to
    exactly the $CWPILOT_BIN-only resolution find_cwpilot() itself starts with
    (never $PATH, never the versions-directory scan), so
    every test that sets CWPILOT_BIN still resolves through it unchanged. Tests
    that exercise $PATH or the versioned-install fallback override this
    explicitly."""
    monkeypatch.setattr(
        handler_session_start, "find_cwpilot", lambda: os.environ.get("CWPILOT_BIN")
    )


@pytest.mark.parametrize(
    ("exit_code", "expected", "unexpected"),
    [
        (127, "cwpilot: not installed", "found but not executable"),
        (126, "cwpilot: found but not executable", "curl -fsSL"),
    ],
)
def test_cwpilot_presence_notice_is_actionable_and_distinct(
    monkeypatch, capsys, exit_code, expected, unexpected
):
    monkeypatch.setattr(
        handler_session_start,
        "invoke_and_get_payload",
        lambda event: _Payload(exit_code),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "0.0")

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["systemMessage"]
    assert expected in notice
    assert unexpected not in notice


def test_cwpilot_not_installed_advises_the_agent_once(monkeypatch, capsys):
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(127)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.delenv("CWPILOT_AGENT_PROVIDER", raising=False)
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "0.0")

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "curl -fsSL install.clockwork-pilot.com | sh" in context
    assert "once" in context
    assert "do not repeat" in context
    assert "AskUserQuestion" not in context


def test_cwpilot_not_installed_on_claude_code_prompts_via_ask_user_question(
    monkeypatch, capsys
):
    """Claude Code has AskUserQuestion; the agent should be told to use it, with the
    plugin's major.minor surfaced. Codex, above, keeps the passive advisory instead."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(127)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", "claude-code")
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "0.0")

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "AskUserQuestion" in context
    assert "curl -fsSL install.clockwork-pilot.com | sh -s -- 0.0" in context
    assert "do not ask again" in context


def test_cwpilot_install_command_is_pinned_to_the_plugins_required_minor(monkeypatch, capsys):
    """install.clockwork-pilot.com's script (and ~/Projects/binstore/install.sh) take
    VERSION as a bare positional argument -- piped through curl that has to go after
    `sh -s --`. Both the systemMessage and the agent notice must carry it, not just
    the bare install one-liner, or a user who runs it gets "latest" instead of the
    major.minor family this plugin requires."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(127)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.delenv("CWPILOT_AGENT_PROVIDER", raising=False)
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "1.0")

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert "curl -fsSL install.clockwork-pilot.com | sh -s -- 1.0" in output["systemMessage"]
    assert (
        "curl -fsSL install.clockwork-pilot.com | sh -s -- 1.0"
        in output["hookSpecificOutput"]["additionalContext"]
    )


def test_cwpilot_install_command_falls_back_when_version_unknown(monkeypatch, capsys):
    """No required minor resolvable (plugin.json unreadable): fall back to the
    plain command rather than emitting a broken `-s --` with nothing after it."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(127)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.delenv("CWPILOT_AGENT_PROVIDER", raising=False)

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    notice = json.loads(capsys.readouterr().out)["systemMessage"]
    assert notice.strip().endswith("curl -fsSL install.clockwork-pilot.com | sh")
    assert "-s --" not in notice


@pytest.mark.parametrize(
    ("required_minor", "expected"),
    [("1.0", "1.0"), ("2.3", "2.3"), (None, None)],
)
def test_recommended_cwpilot_version_is_the_required_minor(required_minor, expected):
    """No live/manifest version is consulted: the recommendation is the plugin's own
    major.minor, which install.sh resolves to that family's newest patch."""
    assert handler_session_start._recommended_cwpilot_version(required_minor) == expected


def test_cwpilot_not_executable_has_no_agent_advisory(monkeypatch, capsys):
    """126 has no install command to run, so it must not reach additionalContext."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(126)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "install.sh" not in context


def test_successful_cwpilot_payload_has_no_install_notice(monkeypatch, capsys):
    monkeypatch.setattr(
        handler_session_start,
        "invoke_and_get_payload",
        lambda event: _Payload(0),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert "cwpilot:" not in output["systemMessage"]
    assert "Stop-hook cwpilot pull prompt" in output["hookSpecificOutput"]["additionalContext"]
    assert "active assignment is already known" in output["hookSpecificOutput"]["additionalContext"]


def test_cwpilot_version_from_path_matches_installers_own_layout(tmp_path):
    path = tmp_path / "cwpilot-versions" / "v1.2.3" / "cwpilot"
    path.parent.mkdir(parents=True)
    path.touch()
    assert handler_session_start._cwpilot_version_from_path(str(path)) == "1.2.3"


def test_cwpilot_version_from_path_none_for_a_plain_path(tmp_path):
    path = tmp_path / "opt" / "cwpilot" / "cwpilot"
    path.parent.mkdir(parents=True)
    path.touch()
    assert handler_session_start._cwpilot_version_from_path(str(path)) is None


def _seed_versioned_cwpilot(tmp_path, version):
    """A file shaped like install.sh's own layout
    (<...>/cwpilot-versions/v<version>/cwpilot), so
    handler_session_start._cwpilot_version_from_path can recover `version` from it."""
    path = tmp_path / "cwpilot-versions" / f"v{version}" / "cwpilot"
    path.parent.mkdir(parents=True)
    path.touch()
    return path


def test_cwpilot_signature_mismatch_produces_actionable_notice(monkeypatch, capsys, tmp_path):
    called = []
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload",
        lambda *a, **k: called.append(a) or _Payload(0),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "0.1.7")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "verify_signature",
        lambda path, signature=None: False,
    )

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    notice = output["systemMessage"]
    assert "binary does not match its stored signature" in notice
    assert "curl -fsSL" in notice
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "curl -fsSL install.clockwork-pilot.com | sh" in context
    assert "once" in context
    assert "do not repeat" in context
    assert "does not match its stored signature" in context
    assert "checksum" not in notice and "checksum" not in context
    # Verified BEFORE it runs: an unverified binary must never be executed by the plugin.
    assert called == [], "cwpilot was run despite failing signature verification"


def test_cwpilot_signature_match_has_no_notice(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "0.1.6")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))
    calls = []
    monkeypatch.setattr(
        handler_session_start.binary_resolver,
        "verify_signature",
        lambda path, signature=None: calls.append(str(path)) or True,
    )

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert "signature" not in output["systemMessage"]
    assert "install.sh" not in output["hookSpecificOutput"]["additionalContext"]
    assert calls == [str(cwpilot_path)]


def test_cwpilot_signature_check_is_skipped_with_no_resolvable_path(monkeypatch, capsys):
    """CWPILOT_BIN unset and nothing on PATH: fail soft, and never call
    verify_signature with a path that isn't real -- there is nothing to verify."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "find_cwpilot", lambda: None)
    calls = []
    monkeypatch.setattr(
        handler_session_start.binary_resolver,
        "verify_signature",
        lambda path, signature=None: calls.append(path) or False,
    )

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    notice = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "signature" not in notice
    assert calls == []


def test_cwpilot_signature_mismatch_is_reported_for_a_non_versioned_install(
    monkeypatch, capsys
):
    """The signature sits beside the binary, so it no longer needs install.sh's
    v<version>/cwpilot layout: a positive mismatch on any resolved path is reported."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setenv("CWPILOT_BIN", "/opt/cwpilot/cwpilot")
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "verify_signature",
        lambda path, signature=None: False,
    )

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    notice = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "binary does not match its stored signature" in notice


def test_cwpilot_signature_undetermined_has_no_notice(monkeypatch, capsys, tmp_path):
    """No key, .sig or openssl (verify_signature -> None) is "can't tell", not a
    mismatch."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "0.0.9")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))
    monkeypatch.setattr(
        handler_session_start.binary_resolver,
        "verify_signature",
        lambda path, signature=None: None,
    )

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    notice = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "signature" not in notice


def test_host_model_is_forwarded_to_configured_cli(monkeypatch, capsys):
    calls = []


    def invoke(event, **kwargs):
        calls.append((event, kwargs))
        return _Payload(0)

    monkeypatch.setattr(handler_session_start, "invoke_and_get_payload", invoke)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"model":"codex-model"}'))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    assert calls == [("call_on_session_start", {"extra": {"model": "codex-model"}})]


def test_cwpilot_major_minor_mismatch_blocks_the_call(monkeypatch, capsys, tmp_path):
    """A CONFIRMED major.minor mismatch must never reach call_on_session_start."""

    def unexpected_call(event, **kwargs):
        raise AssertionError("call_on_session_start must not run when incompatible")

    monkeypatch.setattr(handler_session_start, "invoke_and_get_payload", unexpected_call)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "1.0")
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "2.1.0")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert "not compatible with this plugin (needs 1.0.x)" in output["systemMessage"]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "not API-compatible" in context
    assert "Do not run the installed incompatible cwpilot" in context
    assert "on $PATH" in context
    assert "After the install command finishes" in context
    assert "two separate commands" in context


def test_cwpilot_matching_major_minor_ignores_the_patch(monkeypatch, capsys, tmp_path):
    """1.0.7 satisfies a required 1.0 -- only major.minor is ever compared."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "1.0")
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "1.0.7")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert "not compatible" not in output["systemMessage"]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert f"`{cwpilot_path}`" in context


def test_successful_cwpilot_notice_gives_the_path_as_authoritative_not_a_fallback(
    monkeypatch, capsys, tmp_path
):
    """A working, verified cwpilot IS surfaced as a path -- $PATH is never trusted
    for cwpilot at all (other concurrent sessions may be pinned to a different
    version), so the model needs an authoritative path to use for every cwpilot
    command. What must never happen is this being phrased as a fallback ("if bare
    cwpilot isn't found, try this") -- that phrasing is what previously led a model
    to try bare `cwpilot` first and only chain this path on with `||`, which still
    lets $PATH resolve first."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "1.0.0")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert f"`{cwpilot_path}`" in context
    assert "will be refused" in context  # $PATH has no cwpilot here
    assert "if bare" not in context.lower()


def _run_success_notice(monkeypatch, capsys, tmp_path, symlink_on_path):
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "1.0.0")
    monkeypatch.setenv("CWPILOT_BIN", str(cwpilot_path))
    cwpilot_path.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if symlink_on_path:
        (bindir / "cwpilot").symlink_to(cwpilot_path)
    monkeypatch.setenv("PATH", str(bindir))
    with pytest.raises(SystemExit):
        handler_session_start.main()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    return cwpilot_path, context


def test_notice_allows_plain_cwpilot_without_the_long_path_when_path_reaches_it(
    monkeypatch, capsys, tmp_path
):
    """Plain `cwpilot` is offered exactly when the guard would accept it, and the long
    path is then NOT in context (a model copies a concrete path even as a 'fallback')."""
    cwpilot_path, context = _run_success_notice(monkeypatch, capsys, tmp_path, True)
    assert "Run cwpilot as plain `cwpilot ...`" in context
    assert str(cwpilot_path) not in context


def test_notice_steers_to_the_versioned_path_when_path_does_not_reach_it(
    monkeypatch, capsys, tmp_path
):
    """The host rebuilds the Bash tool's $PATH (no ~/.local/bin). When a bare `cwpilot`
    would be refused, say so and give the versioned path -- never ~/.local/bin."""
    cwpilot_path, context = _run_success_notice(monkeypatch, capsys, tmp_path, False)
    assert f"`{cwpilot_path} <args>`" in context
    assert "will be refused" in context
    assert "Never use ~/.local/bin/cwpilot" in context
    assert "Run cwpilot as plain" not in context


def test_cwpilot_undetermined_version_is_not_blocked(monkeypatch, capsys):
    """A system-wide/hand-built cwpilot with no v<version> layout is "can't tell",
    never a confirmed mismatch -- same fail-soft contract as the signature check."""
    calls = []

    def invoke(event, **kwargs):
        calls.append((event, kwargs))
        return _Payload(0)

    monkeypatch.setattr(handler_session_start, "invoke_and_get_payload", invoke)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "1.0")
    monkeypatch.setenv("CWPILOT_BIN", "/opt/cwpilot/cwpilot")

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    assert calls  # call_on_session_start still ran -- undetermined never blocks
    output = json.loads(capsys.readouterr().out)
    assert "not compatible" not in output["systemMessage"]


def test_cwpilot_found_via_versioned_install_when_not_on_path(monkeypatch, capsys, tmp_path):
    """The exact scenario this fix is for: CWPILOT_BIN unset, cwpilot removed from
    PATH (e.g. for debugging), but install.clockwork-pilot.com already wrote
    <CWPILOT_VERSIONS_DIR>/v1.0.0/cwpilot -- SessionStart must find it via
    find_cwpilot()'s fallback rather than reporting "not installed", and the
    resolved call_on_session_start must actually run (not skipped as
    incompatible)."""
    calls = []

    def invoke(event, **kwargs):
        calls.append((event, kwargs))
        return _Payload(0)

    monkeypatch.setattr(handler_session_start, "invoke_and_get_payload", invoke)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cwpilot_path = _seed_versioned_cwpilot(tmp_path, "1.0.0")
    monkeypatch.delenv("CWPILOT_BIN", raising=False)
    monkeypatch.setattr(handler_session_start, "find_cwpilot", lambda: str(cwpilot_path))

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    assert calls  # call_on_session_start actually ran against the resolved binary
    output = json.loads(capsys.readouterr().out)
    assert "not installed" not in output["systemMessage"]


def test_cwpilot_resolved_path_is_absent_with_nothing_resolved(monkeypatch, capsys):
    """No CWPILOT_BIN and nothing on PATH: the informational path line must not
    claim a path that does not exist."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(0)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setattr(handler_session_start, "find_cwpilot", lambda: None)

    with pytest.raises(SystemExit) as stopped:
        handler_session_start.main()

    assert stopped.value.code == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "cwpilot for this session only is at" not in context


def test_plugin_required_cwpilot_minor_reads_the_major_minor_of_plugin_json(monkeypatch):
    """Reuses hook_cli_calls.plugin_version() -- verify the two agree rather than
    asserting against a hardcoded literal that would drift from a future bump.

    Restores the real implementation over neutral_cwpilot_compat's autouse patch:
    this test is specifically about that implementation, not a caller of it."""
    monkeypatch.setattr(
        handler_session_start, "_plugin_required_cwpilot_minor",
        _REAL_PLUGIN_REQUIRED_CWPILOT_MINOR,
    )
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "plugin_version", _REAL_PLUGIN_VERSION
    )
    from src.hook_cli_calls import plugin_version

    major, minor, *_ = plugin_version().split(".")
    assert handler_session_start._plugin_required_cwpilot_minor() == f"{major}.{minor}"


@pytest.mark.parametrize(
    ("installed", "required", "expected"),
    [
        ("1.0.0", "1.0", True),
        ("1.0.9", "1.0", True),
        ("1.1.0", "1.0", False),
        ("2.0.0", "1.0", False),
        (None, "1.0", None),
        ("1.0.0", None, None),
        ("not-a-version", "1.0", None),
    ],
)
def test_cwpilot_api_compatible_matrix(installed, required, expected):
    assert handler_session_start._cwpilot_api_compatible(installed, required) is expected


def _run_main(monkeypatch, capsys, exit_code=0):
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(exit_code)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit):
        handler_session_start.main()
    return json.loads(capsys.readouterr().out)


def test_hookrunner_not_installed_tells_the_user_the_install_command(monkeypatch, capsys):
    monkeypatch.setattr(handler_session_start.binary_resolver, "find_hookrunner", lambda: None)
    monkeypatch.setattr(handler_session_start.binary_resolver, "plugin_version", lambda: "1.0.4")

    output = _run_main(monkeypatch, capsys)

    assert (
        "hookrunner: not installed, hooks run from Python source. Install with: "
        "curl -fsSL install.clockwork-pilot.com | sh -s -- --hookrunner 1.0.4"
    ) in output["systemMessage"]
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "--hookrunner 1.0.4" in context
    assert "do not ask again" in context


def test_hookrunner_notice_is_absent_when_it_is_installed(monkeypatch, capsys):
    monkeypatch.setattr(
        handler_session_start.binary_resolver, "find_hookrunner", lambda: "/v/hookrunner"
    )
    monkeypatch.setattr(handler_session_start.binary_resolver, "plugin_version", lambda: "1.0.4")

    output = _run_main(monkeypatch, capsys)

    assert "hookrunner" not in output["systemMessage"]
    assert "hookrunner" not in output["hookSpecificOutput"]["additionalContext"]


def test_hookrunner_notice_needs_a_known_plugin_version(monkeypatch, capsys):
    """Without a plugin version there is no exact hookrunner to name, so no command."""
    monkeypatch.setattr(handler_session_start.binary_resolver, "find_hookrunner", lambda: None)
    monkeypatch.setattr(handler_session_start.binary_resolver, "plugin_version", lambda: "")

    output = _run_main(monkeypatch, capsys)

    assert "--hookrunner" not in output["systemMessage"]
    assert "--hookrunner" not in output["hookSpecificOutput"]["additionalContext"]


def test_the_handler_never_installs_or_executes_anything_itself(monkeypatch, capsys):
    """A remote install script is the user's to approve: the handler only reports. The
    sole subprocess-shaped seam is the configured CLI call, stubbed here."""
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("SessionStart must not run or install anything itself")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(handler_session_start.binary_resolver, "find_hookrunner", lambda: None)
    monkeypatch.setattr(handler_session_start.binary_resolver, "plugin_version", lambda: "1.0.4")

    output = _run_main(monkeypatch, capsys)

    assert "--hookrunner 1.0.4" in output["systemMessage"]


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_cwpilot_not_installed_tells_agent_to_check_install_location_before_and_after(
    monkeypatch, capsys, provider
):
    """SessionStart resolves the path once, so an install run mid-session must be
    followed by the agent re-checking the deterministic versioned location itself."""
    monkeypatch.setattr(
        handler_session_start, "invoke_and_get_payload", lambda event: _Payload(127)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    monkeypatch.setenv("CWPILOT_AGENT_PROVIDER", provider)
    monkeypatch.setenv("CWPILOT_VERSIONS_DIR", "/opt/cw")
    monkeypatch.setattr(handler_session_start, "_plugin_required_cwpilot_minor", lambda: "1.0")

    with pytest.raises(SystemExit):
        handler_session_start.main()

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "`/opt/cw/v1.0*/cwpilot`" in context
    assert "Before installing" in context
    assert "After the install command finishes" in context
