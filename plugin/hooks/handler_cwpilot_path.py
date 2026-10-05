#!/usr/bin/env python3
"""Handler for ``cwpilot_path``: print the cwpilot this plugin trusts.

Not a harness event. It is run on request (``dist/cwpilot-path.sh``) so the agent is
handed the command the PreToolUse guard accepts instead of looking for one itself.

stdout carries the command to type for cwpilot and nothing else: plain ``cwpilot`` when
$PATH reaches the trusted binary (the same test the guard and SessionStart apply), else
its absolute versioned path. On failure stdout stays empty and stderr says why, with the
exact install command (the one SessionStart recommends):

    0  command printed
    1  no executable cwpilot of this plugin's major.minor is installed
    2  the installed cwpilot fails its stored signature (or has none)
"""

import sys
from pathlib import Path

from src import binary_resolver


def main() -> int:
    path = binary_resolver.find_cwpilot()
    command = binary_resolver.whole_install_command(
        binary_resolver.major_minor(binary_resolver.plugin_version()))
    if path is None:
        print(f"cwpilot is not installed in {binary_resolver.versions_dir()}. Install "
              f"with (after the user agrees): {command}", file=sys.stderr)
        return 1
    if binary_resolver.verify_signature(Path(path)) is False:
        problem = binary_resolver.signature_problem(Path(path))
        detail = f" Missing: {problem}." if problem else ""
        print(f"{path} does not match its stored signature, or has none.{detail} "
              f"Reinstall with (after the user agrees): {command}", file=sys.stderr)
        return 2
    print("cwpilot" if binary_resolver.path_cwpilot_matches(path) else path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
