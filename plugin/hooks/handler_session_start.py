#!/usr/bin/env python3
"""Handler for SessionStart event.

Injects whatever the configured CLI returns for session start, so a fresh session
lands on the frontier.

By-run file-change attribution used to happen here too (draining an ephemeral
FilesChangesTracker scratch and attributing it onto the active run's WorkNode
evidence).
"""

import sys
import json
import os
import shlex
from pathlib import Path
from typing import Optional

from src.config import LOG_ENVS
from src.hook_logging import redact_session_fields, setup_logger

logger = setup_logger("SessionStart", log_envs=LOG_ENVS, reset_log=True)

# Import the binary seam after logger creation; it resolves only the plugin root.
from src import binary_resolver  # noqa: E402

# Keep the CLI seams available for tests without resolving project-root configuration
# at handler import time. The real callables are loaded only when the handler runs.
invoke_and_get_payload = None
find_cwpilot = None

# SessionStart carries project content from the configured CLI. The short steering rule
# below is plugin-owned because it explains how to interpret a later Stop-hook prompt.
# the consuming project's CLI via call_on_session_start's "additional_context" /
# "system_message"; when it says nothing, nothing is injected -- a placeholder line
# ("Session started.") told the model and the user nothing they did not already know.
#
# Its second sentence exists because a model left to reason this out on its own
# invented a workaround: trying a bare `cwpilot` first, then chaining a hardcoded,
# version-specific install path onto it as an `||` fallback
# (`cwpilot pull || <path> pull`). Both halves of that are wrong. $PATH is never
# trusted for cwpilot ANYWHERE in this plugin (binary_resolver) -- not
# only because of checksum verification, but because this machine can have several
# coding-agent sessions running at once, each against a project pinned to a
# DIFFERENT cwpilot version; a bare `cwpilot` resolves to whatever one $PATH
# happens to point at, which need not be the version THIS session's project needs.
# A hardcoded path is just as wrong from the other direction: it freezes this
# session's resolved version into a command that outlives the session, going stale
# the moment install.sh's current+previous retention prunes it. The only correct
# source for "which cwpilot" is the absolute path this handler resolves fresh
# every session (see the `elif cwpilot_path:` notice below) and hands the model
# for THAT session only. Stated here, once, evergreen, rather than only in the
# checksum/compat notices below, which fire only when this handler has already
# detected a specific problem.
_STEERING_CONTEXT = (
    "Clockwork-Pilot steering: a Stop-hook cwpilot pull prompt is the next-action "
    "steering signal when you have stopped and do not know what to do. If an active "
    "assignment is already known, continue that assignment directly; pull is not "
    "required just because the hook displayed it. This machine can run several "
    "coding-agent sessions at once, each pinned to a different cwpilot version, so "
    "only the cwpilot named in this session's SessionStart notice is trusted. Skills "
    "and prompts write commands as `cwpilot <args>`. If the notice says plain `cwpilot` "
    "works, type it. Otherwise run them by the notice's absolute path (under "
    "cwpilot-versions), never ~/.local/bin/cwpilot. If a refusal names a path, use "
    "exactly that. Never use a path you hardcode, recall from a previous session, or "
    "otherwise discover yourself. If no "
    "path was given, cwpilot is not installed yet: follow the install/check "
    "instructions in the notice (check the install location before and after "
    "installing) and do not substitute a different binary."
)

# One place for the install command, used by both the "not installed" and the
# "checksum mismatch" notices below -- so the two can never drift onto different URLs.
_INSTALL_COMMAND_BASE = "curl -fsSL install.clockwork-pilot.com | sh"


def _install_command(version: Optional[str] = None) -> str:
    """The install one-liner, pinned to ``version`` when it is known.

    install.clockwork-pilot.com's script (verified against its own usage text and
    ~/Projects/binstore/install.sh) takes VERSION as a bare positional argument,
    e.g. ``install.sh 0.0.1`` -- the same un-prefixed shape the release tags use. Piped through curl, a positional argument
    has to go after ``sh -s --``. No version known (the plugin version
    is unreadable) falls back to the plain command, which installs
    whatever the installer considers latest.
    """
    if not version:
        return _INSTALL_COMMAND_BASE
    return f"{_INSTALL_COMMAND_BASE} -s -- {version}"


def _install_location_check(version: Optional[str]) -> str:
    """Agent-facing instruction to look for cwpilot at its deterministic install
    location, before and after running the installer.

    The installer writes <versions_dir>/v<version>/cwpilot and, separately, a
    ~/.local/bin/cwpilot symlink to it. That directory is not necessarily on $PATH
    (some containers' PATH lacks it), so the versioned file is the one thing that is
    always there to look for. SessionStart resolves the path only once, so a binary
    installed mid-session would otherwise stay unknown to the agent until a restart;
    this tells it to re-check itself. ``version`` may be a bare major.minor, hence the
    trailing ``*`` -- the installer resolves it to the newest patch.
    """
    versions_dir = os.environ.get("CWPILOT_VERSIONS_DIR") or "~/.local/share/cwpilot-versions"
    pattern = f"{versions_dir}/v{version or ''}*/cwpilot"
    return (
        f"Before installing, check whether an executable already exists at `{pattern}` "
        "(for example with `ls -d`); if one does, use that exact path and skip the "
        "install. After the install command finishes, check the same location again, "
        "then run `cwpilot` once as a plain command: if $PATH now reaches the "
        "installed binary it works, and if it is not found or the PreToolUse hook "
        "refuses it, run the path you found at that location instead (newest version "
        "if several) and keep using that path for every cwpilot command this session. "
        "Make these two separate commands, never `cwpilot ... || <path> ...`. Do not "
        "wait for a new session. If nothing is at that location "
        "after the install, report the failure to the user."
    )


def _hookrunner_install_command(version: str) -> str:
    """The install.sh command for THIS plugin's hookrunner (``--hookrunner`` installs
    only that, at exactly the plugin's own version, and leaves cwpilot alone)."""
    return f"{_INSTALL_COMMAND_BASE} -s -- --hookrunner {shlex.quote(version)}"


def _cwpilot_version_from_path(cwpilot_path: str) -> Optional[str]:
    """The exact version a resolved cwpilot path is actually installed as, or None.

    Recovered from install.sh's own self-describing on-disk layout
    (``<CWPILOT_VERSIONS_DIR>/v<version>/cwpilot``, with a stable symlink pointing
    into it) rather than any ``cwpilot --version`` flag -- none is assumed to exist.
    None (not a versioned layout at all: a hand-built or system-packaged cwpilot,
    say) means there is nothing to look a trusted checksum up by; the caller fails
    soft exactly like every other undetermined case in this handler.
    """
    try:
        real = Path(cwpilot_path).resolve()
    except OSError:
        return None
    if real.name != "cwpilot" or not real.parent.name.startswith("v"):
        return None
    return real.parent.name[1:] or None


def _major_minor(version: str) -> Optional[str]:
    """The first two dotted components of ``version``, or None if it has fewer."""
    parts = version.split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def _plugin_required_cwpilot_minor() -> Optional[str]:
    """This plugin build's required cwpilot major.minor, or None if undetermined.

    Reuses hook_cli_calls.plugin_version() -- the same .claude-plugin/plugin.json
    read src.hook_cli_calls already treats as canonical for every agent provider
    (even under CWPILOT_AGENT_PROVIDER=codex) -- rather than a second read of the
    same file. The plugin's own version is major.minor-only by convention (no patch
    digit), but only the first two components are ever taken here regardless, so a
    stray third component cannot silently loosen the comparison below. Imported
    lazily, matching invoke_and_get_payload's own deferred import in main(): neither
    should resolve project-root configuration at handler import time.
    """
    from src.hook_cli_calls import plugin_version

    return _major_minor(plugin_version())


def _recommended_cwpilot_version(required_minor: Optional[str]) -> Optional[str]:
    """The version string to recommend installing -- correct for THIS plugin.

    The bare ``required_minor``: install.sh's own VERSION handling resolves a bare
    major.minor like "1.0" to that family's newest patch via the release list, so this
    is a concrete, installable recommendation that can never cross into a different
    major.minor. None (unknown requirement) installs whatever the installer calls latest.
    """
    return required_minor


def _cwpilot_api_compatible(installed_version: Optional[str],
                            required_minor: Optional[str]) -> Optional[bool]:
    """Whether ``installed_version``'s major.minor exactly matches ``required_minor``.

    None (not False) whenever either side is unknown -- an undetermined cwpilot
    version (a system-packaged or hand-built install with no v<version> layout) or
    an unreadable plugin.json must never be treated as a confirmed mismatch. Same
    "can't tell" contract: only a POSITIVE major.minor mismatch blocks anything.
    """
    if not installed_version or not required_minor:
        return None
    installed_minor = _major_minor(installed_version)
    if installed_minor is None:
        return None
    return installed_minor == required_minor


def emit_additional_context(event_name: str, text: str,
                            system_message: Optional[str] = None) -> None:
    """Print the hook JSON for ``event_name``.

    Two channels, two audiences:
      • ``hookSpecificOutput.additionalContext`` → injected into the AGENT's context
        (silent; the model acts on it).
      • ``systemMessage`` → rendered to the USER by Claude Code when the hook fires
        (the visible hint), independent of whether the model surfaces anything.
    """
    hook_output: dict = {"hookEventName": event_name}
    if text:
        hook_output["additionalContext"] = text
    out: dict = {"hookSpecificOutput": hook_output}
    if system_message:
        out["systemMessage"] = system_message
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")


def main():
    global invoke_and_get_payload, find_cwpilot
    if invoke_and_get_payload is None:
        from src.hook_cli_calls import invoke_and_get_payload as configured_invoke

        invoke_and_get_payload = configured_invoke
    if find_cwpilot is None:
        from src.hook_cli_calls import find_cwpilot as configured_find_cwpilot

        find_cwpilot = configured_find_cwpilot

    input_data = sys.stdin.read()
    hook_input = json.loads(input_data) if input_data else {}

    log_entry = {
        "hook_input": redact_session_fields(hook_input),
    }

    logger.info(log_entry)

    # Nothing is downloaded here. hookrunner is optional: dist/run-hook.sh falls back to
    # `python3 -m hooks` when it is not installed, and the mode line below says which
    # implementation is running. When it is missing, the user is told the one command that
    # installs it -- running a remote install script is theirs to approve, never done
    # silently from a hook.
    notices = []
    hookrunner_agent_notice = ""
    path = binary_resolver.find_hookrunner()
    if path is None:
        version = binary_resolver.plugin_version()
        logger.warning("hookrunner not installed; hooks run from Python source")
        if version:
            install_command = _hookrunner_install_command(version)
            notices.append(
                "hookrunner: not installed, hooks run from Python source. Install with: "
                f"{install_command}"
            )
            hookrunner_agent_notice = (
                "The plugin's hookrunner binary is not installed, so hooks run from "
                "Python source (slower, but fully working). Once, early in this "
                "session, offer the user to install it by running "
                f"`{install_command}`; run it only if they agree, and do not ask again "
                "this session either way."
            )
    else:
        logger.info(f"hookrunner resolved: {path}")

    # This process knows its own mode: compiled runners expose either sys.frozen or
    # Nuitka's __compiled__ marker, while neither is set under the interpreter.
    # Reported for the hookrunner only -- cwpilot is a subprocess this hook shells out
    # to, while the hookrunner is the thing currently executing this line.
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        notices.append("using plugin binary.")
    else:
        notices.append("using python plugin.")
    # This hook records no session roots of its own. The configured CLI is handed both
    # roots as arguments (see cw-plugin-cli-hooks.json) and owns the per-session store it
    # shares with the rest of that project -- the store's layout is that project's
    # business, not this plugin's, and a second writer here only invited the two to
    # disagree. Inside a hook subprocess CLAUDE_PROJECT_DIR/CLAUDE_PLUGIN_ROOT are set by
    # the harness, so src.config resolves both without reading anything.
    #
    # Resolve cwpilot's path/version ONCE, before deciding whether to even call it --
    # reused below both to gate the call on API compatibility and (on a successful
    # call) to verify its checksum, so the two can never resolve a different binary
    # from each other. Calls the exact same seam the configured {cwpilot_bin} call
    # itself resolves through (src.hook_cli_calls.find_cwpilot), so the two can never
    # disagree about where cwpilot is.
    from src.hook_cli_calls import path_cwpilot_matches

    cwpilot_path = find_cwpilot()
    installed_version = _cwpilot_version_from_path(cwpilot_path) if cwpilot_path else None
    required_minor = _plugin_required_cwpilot_minor()
    api_compatible = _cwpilot_api_compatible(installed_version, required_minor)

    # agent_notice carries the actionable half of a notice into additionalContext, so
    # the MODEL, not just the user, knows to raise it. Told explicitly to say it once:
    # this fires on every SessionStart, and a binary left unfixed must not turn into
    # the model repeating the same advisory on every later turn of the same session.
    agent_notice = ""

    if api_compatible is False:
        # A CONFIRMED major.minor mismatch: never call an incompatible cwpilot. This
        # is the one case where the binary genuinely is present and IS the one that
        # would have run -- unlike 127 (nothing found) or a checksum mismatch
        # (present, possibly tampered), so the call is skipped outright rather than
        # made and then reported on.
        recommended_version = _recommended_cwpilot_version(required_minor)
        install_command = _install_command(recommended_version)
        notices.append(
            f"cwpilot: installed version {installed_version} is not compatible with "
            f"this plugin (needs {required_minor}.x). Update with: {install_command}"
        )
        agent_notice = (
            f"The installed cwpilot ({installed_version}) is not API-compatible with "
            f"this plugin (needs {required_minor}.x). Advise the user once, early in "
            f"this session, that running `{install_command}` is advised, then do not "
            "repeat the suggestion again this session. Do not run the installed "
            f"incompatible cwpilot ({cwpilot_path}), whether by that path or as "
            "`cwpilot` on $PATH. Once a compatible version is installed, use the "
            "versioned install path instead. "
            + _install_location_check(recommended_version)
        )
        session_start_payload: dict = {}
    elif cwpilot_path and binary_resolver.verify_signature(Path(cwpilot_path)) is False:
        # Verified BEFORE it runs, not after: a binary that fails its signature (or has
        # none stored) must never be executed by this plugin, and a check made after the
        # call would only report on something already run. Like the incompatible case,
        # the call is skipped outright.
        problem = binary_resolver.signature_problem(Path(cwpilot_path))
        reason = f" Missing: {problem}." if problem else ""
        logger.warning(f"cwpilot has no valid stored signature: {cwpilot_path}.{reason}")
        install_command = _install_command(_recommended_cwpilot_version(required_minor))
        notices.append(
            f"cwpilot: binary does not match its stored signature, or has none. Not run."
            f"{reason} Update with: {install_command}"
        )
        agent_notice = (
            "The installed cwpilot binary does not match its stored signature, or has "
            "none, so the plugin did not run it. Advise the user once, early in this "
            f"session, that running `{install_command}` is advised, then do not repeat "
            "the suggestion again this session. Do not run that cwpilot yourself."
        )
        session_start_payload = {}
    else:
        # call_on_session_start's stdout, if a JSON object, may carry "additional_context"
        # (agent-facing) and/or "system_message" (user-facing) text to splice into this
        # SessionStart hook's own output below. See cli_stubs/call_on_session_start for the
        # documented payload shape; a stub that prints nothing/non-JSON degrades to {}.
        model = hook_input.get("model")
        if model is None:
            # Keep the seam compatible with minimal test/stub callables that only accept
            # the event name when the host supplied no model at all.
            session_start_result = invoke_and_get_payload("call_on_session_start")
        else:
            session_start_result = invoke_and_get_payload(
                "call_on_session_start", extra={"model": str(model)}
            )
        if isinstance(session_start_result, tuple) and len(session_start_result) == 2:
            session_start_payload, cwpilot_exit_code = session_start_result
        else:
            cwpilot_exit_code = getattr(session_start_result, "exit_code", None)
            if cwpilot_exit_code is None and isinstance(session_start_result, dict):
                cwpilot_exit_code = session_start_result.get("exit_code", 0)
            cwpilot_exit_code = cwpilot_exit_code or 0
            session_start_payload = session_start_result
        if not isinstance(session_start_payload, dict):
            session_start_payload = {}

        # The configured cwpilot call is the presence check. 127 means no executable was
        # found; 126 means one was found but could not be executed. Keep those actionable
        # notices distinct, while leaving every other CLI failure fail-soft as before.
        if cwpilot_exit_code == 127:
            # Pin the command to this plugin's own major.minor, so whichever notice
            # below gets read, the install can never land an incompatible family.
            install_version = _recommended_cwpilot_version(required_minor)
            install_command = _install_command(install_version)
            notices.append(f"cwpilot: not installed. Install with: {install_command}")
            # Claude Code has AskUserQuestion, an interactive tool the model can call
            # mid-session; Codex (CWPILOT_AGENT_PROVIDER=codex) has no equivalent, so it
            # keeps the passive advisory below instead.
            if os.environ.get("CWPILOT_AGENT_PROVIDER") == "claude-code":
                agent_notice = (
                    "cwpilot is not installed. Once, early in this session, use "
                    "AskUserQuestion to ask the user whether to install it by running "
                    f"`{install_command}`. Run that command only if they agree; do not "
                    "ask again this session either way. "
                    + _install_location_check(install_version)
                )
            else:
                agent_notice = (
                    "cwpilot is not installed. Advise the user once, early in this "
                    f"session, that running `{install_command}` is advised, then do not "
                    "repeat the suggestion again this session. "
                    + _install_location_check(install_version)
                )
        elif cwpilot_exit_code == 126:
            notices.append(
                "cwpilot: found but not executable. Check CWPILOT_BIN and execute permissions."
            )
        elif cwpilot_exit_code == 0:
            if cwpilot_path:
                # The skills show snippets like `cwpilot pull`. Whether that bare form may
                # be used is decided by path_cwpilot_matches -- the same test the PreToolUse
                # guard applies -- so this notice and the enforcement cannot disagree.
                if path_cwpilot_matches(cwpilot_path):
                    # Deliberately NO absolute path in this branch: a concrete path in
                    # context is what a model copies, even when told it is a fallback.
                    # If a plain `cwpilot` is ever refused, the refusal names the path.
                    agent_notice = (
                        "Run cwpilot as plain `cwpilot ...` (e.g. `cwpilot pull`): $PATH "
                        "reaches this session's verified binary. If a plain `cwpilot` is "
                        "ever refused, use the full path the refusal names."
                    )
                else:
                    agent_notice = (
                        f"cwpilot for this session only is at `{cwpilot_path}` (the "
                        "versioned install under cwpilot-versions). A bare `cwpilot` is "
                        "not on your $PATH and will be refused: the skills write commands "
                        f"as `cwpilot <args>`, so run them as `{cwpilot_path} <args>` for "
                        "every cwpilot command this session. Never use "
                        "~/.local/bin/cwpilot."
                    )

    binaries_notice = " ".join(notices)

    # The configured CLI supplies project-specific context; prepend the stable steering
    # rule so Stop-hook output cannot be mistaken for a mandatory restart of navigation
    # while an already-known assignment is being executed.
    cli_context = session_start_payload.get("additional_context") or ""
    extra_context = " ".join(
        part for part in (_STEERING_CONTEXT, agent_notice, hookrunner_agent_notice, cli_context)
        if part
    )
    extra_system_message = session_start_payload.get("system_message") or ""

    emit_additional_context(
        "SessionStart",
        extra_context,
        # The binaries notice goes to the USER only: which binaries this session will
        # actually call (or that there are none) is an install fact they can act on,
        # and it is worthless context for the model.
        system_message=" ".join(
            part for part in (binaries_notice, extra_system_message) if part
        ),
    )

    sys.exit(0)


if __name__ == '__main__':
    main()
