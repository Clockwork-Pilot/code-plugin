"""PreToolUse guard: an agent may only run the cwpilot this plugin resolved and verified.

binary_resolver never consults $PATH for cwpilot, but that binds the
plugin's OWN calls, not the agent's Bash tool. An agent that types `cwpilot ...` gets
whatever $PATH holds -- possibly a different version than the one the project's config
was written for. This module closes that gap at PreToolUse, for every agent (main
session and sub-agents alike):

  * a bare `cwpilot` (no "/") is a $PATH lookup: allowed only while it resolves, through
    symlinks, to the very binary binary_resolver resolves; otherwise refused, and the
    refusal names the versioned path to use instead;
  * a path-prefixed cwpilot must be exactly that resolved versioned path. The installer's
    ~/.local/bin/cwpilot symlink is unversioned and shared by every agent on the machine,
    moving whenever any one of them reinstalls, so it is not accepted as a path;
  * that file must match the detached signature install.sh stored beside it.

It is a guardrail on the Bash command text, not a sandbox: command substitution
(`$(which cwpilot)`) or an interpreter script can still reach a binary. Pair it with
an exec-policy rule where the agent host offers one.
"""

import glob
import json
import os
import re
import shlex
from pathlib import Path
from typing import Optional

from src import binary_resolver
from src.config import SHELL_SEPARATOR_PATTERN
from src.hook_cli_calls import find_cwpilot, path_cwpilot_matches
from src.hook_logging import setup_logger

logger = setup_logger("CwpilotGuard")

CWPILOT = "cwpilot"
_SEPARATOR_RE = re.compile(SHELL_SEPARATOR_PATTERN)
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Prefixes that run the next word as a command; looked through to find what is really run.
_WRAPPERS = {"env", "command", "exec", "nohup", "time", "sudo", "xargs"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_MAX_DEPTH = 3  # nested `bash -c "bash -c '...'"`


def _tokens(segment: str) -> list:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _cwpilot_invocations(command: str, depth: int = 0) -> list:
    """Every token that is being run AS cwpilot in `command` (a bare or path-prefixed name)."""
    found = []
    for segment in _SEPARATOR_RE.split(command):
        tokens = _tokens(segment)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _ENV_ASSIGNMENT_RE.match(tok) or tok in _WRAPPERS or (
                    tok.startswith("-") and i > 0 and tokens[i - 1] in _WRAPPERS):
                i += 1
                continue
            break
        if i >= len(tokens):
            continue
        head = tokens[i]
        expanded = os.path.expandvars(os.path.expanduser(head))
        if any(c in expanded for c in "*?["):
            # A glob can spell cwpilot without the literal name (~/.local/bin/cwpil*).
            found.extend(m for m in glob.glob(expanded) if os.path.basename(m) == CWPILOT)
        elif ("$" in expanded or "`" in expanded) and CWPILOT in command:
            # Command substitution / an unset variable as the command, in a command that
            # names cwpilot (`C=cwpilot; $C`, `$(which cwpilot)`): what runs is unknowable.
            found.append(expanded)
        elif os.path.basename(head) == CWPILOT:
            found.append(expanded)
        elif os.path.basename(head) in _SHELLS and depth < _MAX_DEPTH and "-c" in tokens[i:]:
            inner = tokens[tokens.index("-c", i) + 1:]
            if inner:
                found.extend(_cwpilot_invocations(inner[0], depth + 1))
    return found


def _cache_file() -> Optional[Path]:
    data = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("PLUGIN_DATA")
    return Path(data) / "cwpilot-verified.json" if data else None


def _stat_key(real: Path) -> Optional[str]:
    try:
        st = real.stat()
    except OSError:
        return None
    return f"{real}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _integrity_ok(real: Path) -> bool:
    """False on a signature mismatch OR a missing stored .sig/key. "Can't tell" (no
    openssl, unreadable) passes, same fail-soft contract as SessionStart.

    A good result is remembered per (path, inode, size, mtime) so the binary is verified
    once, not on every Bash call; any change to the file invalidates it.
    """
    key = _stat_key(real)
    if not key:
        return True
    cache_file = _cache_file()
    cache = {}
    if cache_file:
        try:
            cache = json.loads(cache_file.read_text())
        except (OSError, ValueError):
            cache = {}
        if cache.get("verified") == key:
            return True
    verdict = binary_resolver.verify_signature(real)
    if verdict is False:
        return False
    if verdict is True and cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"verified": key}))
        except OSError as exc:
            logger.warning(f"could not write {cache_file}: {exc}")
    return True


def check_command(command: str) -> Optional[str]:
    """None if `command` may run, else the reason it must be rejected."""
    invoked = _cwpilot_invocations(command) if command else []
    if not invoked:
        return None
    resolved = find_cwpilot()
    if not resolved:
        return ("cwpilot is not installed where the plugin looks for it, so no cwpilot "
                "may be run. Ask the user to install it.")
    resolved_path = os.path.abspath(resolved)
    for tok in invoked:
        if "$" in tok or "`" in tok:
            return (f"Cannot tell which binary `{tok}` runs. Invoke cwpilot literally, "
                    f"by absolute path: {resolved}")
        if "/" not in tok:
            # A bare `cwpilot` is fine exactly while $PATH reaches the resolved binary --
            # the same test SessionStart's notice used to say so.
            if path_cwpilot_matches(resolved):
                continue
            return (f"`cwpilot` is not available on $PATH here (it is missing, or is a "
                    f"different binary). Run the plugin's verified install by its full "
                    f"path instead: {resolved} <args>")
        expanded = os.path.expandvars(os.path.expanduser(tok))
        if os.path.abspath(expanded) == resolved_path:
            continue
        # A path-prefixed cwpilot must be exactly the resolved versioned path: not the
        # installer's ~/.local/bin/cwpilot symlink (shared by every agent on the machine
        # and moved by any reinstall), not any other spelling.
        return (f"`{tok}` is not the plugin's cwpilot (never use ~/.local/bin/cwpilot or "
                f"another path). Run the versioned install: {resolved} <args>")
    if not _integrity_ok(Path(resolved_path)):
        problem = binary_resolver.signature_problem(Path(resolved_path))
        detail = f" Missing: {problem}." if problem else ""
        return (f"{resolved} does not match its stored signature, or has none; refusing to run it."
                f"{detail} Ask the user to reinstall cwpilot.")
    return None
