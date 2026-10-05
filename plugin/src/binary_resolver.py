"""Where the plugin's binaries are installed, and whether their stored signature holds.

Nothing here downloads. ``install.sh`` (install.clockwork-pilot.com) is the only thing
that puts a binary on disk, and it lays out one shared directory:

    <versions_dir>/v<X.Y.Z>/cwpilot                    + cwpilot.sig
    <versions_dir>/hookrunner-v<X.Y.Z>/hookrunner      + hookrunner.sig
    <versions_dir>/signing.pub                         the key those signatures verify with

``<versions_dir>`` is $CWPILOT_VERSIONS_DIR, default ~/.local/share/cwpilot-versions.
$PATH and $CWPILOT_BIN are never consulted: a hook must not run a binary the installer
did not place and sign.

Two kinds of answer, both fail-soft (a missing binary is a degraded session, never a
broken one):

    find_cwpilot() / find_hookrunner()   a path, or None
    verify_signature(path)               True / False, or None when it cannot be decided
                                         (no openssl, unreadable). A MISSING .sig or key is
                                         False, not "can't tell": deleting the signature
                                         must not be a way to switch the check off.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from src.config import PLUGIN_ROOT
from src.hook_logging import setup_logger

logger = setup_logger()

OPENSSL_TIMEOUT_SECONDS = 30
_VERSION_DIR_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)")


def versions_dir() -> Path:
    return Path(os.environ.get("CWPILOT_VERSIONS_DIR")
                or Path.home() / ".local" / "share" / "cwpilot-versions")


def plugin_version() -> str:
    """The installed plugin version for the consuming CLI's environment.

    Each host reads the manifest it actually installed: Codex ships
    .codex-plugin/plugin.json, Claude Code .claude-plugin/plugin.json. The other is a
    fallback only. Also the hookrunner version this plugin was built with -- the public
    release tag v<plugin version> carries both.
    """
    dirs = [".claude-plugin", ".codex-plugin"]
    if os.environ.get("CWPILOT_AGENT_PROVIDER") == "codex":
        dirs.reverse()
    for directory in dirs:
        try:
            metadata = json.loads(
                (Path(PLUGIN_ROOT) / directory / "plugin.json").read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-except
            continue
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if isinstance(version, str) and version:
            return version
    return ""


# One place for the install command, used by every "not installed" / "update" notice and
# by the cwpilot-path handler, so they can never drift onto different URLs.
INSTALL_COMMAND_BASE = "curl -fsSL install.clockwork-pilot.com | sh"


def install_command(version: Optional[str] = None) -> str:
    """The install one-liner, pinned to ``version`` when it is known.

    install.sh takes VERSION as a bare positional argument (``install.sh 0.0.1``); piped
    through curl it goes after ``sh -s --``. No version falls back to the plain command,
    which installs whatever the installer considers latest.
    """
    if not version:
        return INSTALL_COMMAND_BASE
    return f"{INSTALL_COMMAND_BASE} -s -- {version}"


def whole_install_command(fallback_version: Optional[str] = None) -> str:
    """The one install.sh command for THIS plugin's whole cwpilot world.

    With no component named, install.sh installs both. It is given the plugin's full
    version: hookrunner is per-patch and takes it exactly, while cwpilot only consumes
    the major.minor and resolves to that family's newest patch. With no readable plugin
    version, :func:`install_command` pinned to ``fallback_version`` (e.g. major.minor).
    """
    version = plugin_version()
    if version:
        return f"{INSTALL_COMMAND_BASE} -s -- {shlex.quote(version)}"
    return install_command(fallback_version)


def major_minor(version: str) -> Optional[str]:
    """The first two dotted components of ``version``, or None if it has fewer."""
    parts = version.split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_cwpilot() -> Optional[str]:
    """The newest installed cwpilot of THIS plugin's major.minor, or None.

    Only the plugin's own family counts: the versions directory is shared by every agent
    on the machine, each possibly on a different plugin release, and an incompatible
    family's newer install must not win. With no readable plugin version there is no
    family to narrow by, so the newest install of any family is returned.
    """
    wanted = major_minor(plugin_version())
    best_key, best_path = None, None
    try:
        entries = list(versions_dir().iterdir())
    except OSError:
        return None
    for entry in entries:
        match = _VERSION_DIR_RE.match(entry.name)
        binary = entry / "cwpilot"
        if not match or not _usable(binary):
            continue
        if wanted and f"{match.group(1)}.{match.group(2)}" != wanted:
            continue
        key = tuple(int(g) for g in match.groups())
        if best_key is None or key > best_key:
            best_key, best_path = key, binary
    return str(best_path) if best_path else None


def find_hookrunner() -> Optional[str]:
    """This plugin's own hookrunner (``hookrunner-v<plugin version>``), or None."""
    version = plugin_version()
    if not version:
        return None
    binary = versions_dir() / f"hookrunner-v{version}" / "hookrunner"
    return str(binary) if _usable(binary) else None


def path_cwpilot_matches(resolved: str, tok: str = "cwpilot") -> bool:
    """True when a bare `cwpilot` (looked up on $PATH) is the same file as `resolved`
    once symlinks are followed.

    The ONE test deciding whether plain `cwpilot` may be used: SessionStart's notice, the
    PreToolUse guard and the cwpilot-path handler all call it, so what the agent is told
    and what is enforced can never disagree. If $PATH has no cwpilot, or a different one,
    it is False and the agent is steered to the versioned install path instead.
    """
    candidate = shutil.which(tok)
    if not candidate:
        return False
    return _same_file(candidate, resolved)


def _same_file(candidate: str, resolved: str) -> bool:
    try:
        return os.path.realpath(candidate) == os.path.realpath(resolved)
    except OSError:
        return False


def signature_problem(path, signature: Optional[Path] = None) -> Optional[str]:
    """Which stored file is missing for ``path``, or None when both are present.

    Only names what is absent, so a failed check can say WHICH half to restore (the .sig
    beside the binary, or install.sh's signing.pub in the versions dir) instead of one
    vague "no signature".
    """
    path = Path(path)
    sig = Path(signature) if signature else path.with_name(path.name + ".sig")
    key = versions_dir() / "signing.pub"
    missing = []
    if not sig.is_file():
        missing.append(f"the signature {sig}")
    if not key.is_file():
        missing.append(f"the public key {key} (install.sh writes it)")
    return " and ".join(missing) if missing else None


def verify_signature(path, signature: Optional[Path] = None) -> Optional[bool]:
    """Whether ``path`` matches its stored detached signature.

    True: verified. False: it does not match, OR the signature or key that install.sh
    stores is missing -- an install with no signature is unverified, and treating that as
    "can't tell" would let anyone who replaces the binary also delete the ``.sig``. None
    only when the check itself cannot run (no openssl, a missing binary).

    The signature defaults to ``<name>.sig`` beside the binary, the key to
    ``<versions_dir>/signing.pub`` -- both written by install.sh. This detects a corrupt
    or replaced binary; the key lives beside it, so it is not a defence against someone
    able to rewrite that whole directory.
    """
    path = Path(path)
    if not path.is_file():
        return None
    sig = Path(signature) if signature else path.with_name(path.name + ".sig")
    key = versions_dir() / "signing.pub"
    if not (sig.is_file() and key.is_file()):
        logger.warning(f"{path} has no stored signature or key ({sig}, {key})")
        return False
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(key),
             "-signature", str(sig), str(path)],
            capture_output=True, text=True, timeout=OPENSSL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"could not run openssl to verify {path}: {exc}")
        return None
    return result.returncode == 0
