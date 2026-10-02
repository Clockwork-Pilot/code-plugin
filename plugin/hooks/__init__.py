"""Hooks utilities."""

import sys
import json
import fnmatch
from pathlib import Path

# The checkout root, so the sibling `src` package is importable. Done HERE, once:
# importing hooks.handler_* imports this package first, so every handler body runs
# with the path already set and none of them repeat it. Redundant under the normal
# entrypoint (run-hook.sh cd's to the plugin root and runs `python3 -m hooks`, which
# puts the cwd on sys.path) and inside the frozen binary (which bundles src outright)
# -- kept for an in-process import from another cwd, e.g. a test harness.
sys.path.insert(0, str(Path(__file__).parent.parent))

def send_error(message: str, file_path: str = None) -> None:
    """Send error message to Claude about blocked operation.

    Args:
        message: Error message to display
        file_path: Optional file path that triggered the error
    """
    error_output = {
        "type": "error",
        "message": message,
        "file_path": file_path
    }
    print(json.dumps(error_output), file=sys.stderr)

def is_knowledge_file(file_path: str) -> bool:
    """Check if file is in knowledge files registry.

    Args:
        file_path: Path to check (can be relative or absolute).

    Returns:
        True if the file is a registered knowledge file, False otherwise.
    """
    if not file_path:
        return False

    # Check if file matches knowledge document patterns
    knowledge_patterns = ["*.k.json", "*.k.md"]
    for pattern in knowledge_patterns:
        if fnmatch.fnmatch(file_path, f"*/{pattern}") or fnmatch.fnmatch(file_path, pattern):
            return True
    return False

def _spec_has_unverified(spec_path: Path) -> bool:
    """Return the contains_unverified_constraints flag for a spec file.

    Missing or unreadable specs are treated as not-blocking (fail-open); the
    constraint checker remains the source of truth, and fail-closed would
    strand edits on a transient path error.
    """
    if not spec_path.exists():
        return False
    try:
        spec_data = json.loads(spec_path.read_text())
        return spec_data.get("contains_unverified_constraints", False)
    except Exception:
        return False

def have_unverified_constraints() -> bool:
    """Check whether any spec reachable from PROJECT_ROOT has unverified constraints.

    When PROJECT_ROOT/project.k.json exists, iterate every spec it references
    and return True if any sub-spec's contains_unverified_constraints flag is
    True. Otherwise, fall back to reading PROJECT_ROOT/spec.k.json directly.

    Unverified constraints (fails_count < 1) have never failed and must be
    resolved before edits are allowed.
    """
    from src.config import PROJECT_ROOT, TEMPORARY_BYPASS_UNVERIFIED_CONSTRAINTS_BLOCK

    if TEMPORARY_BYPASS_UNVERIFIED_CONSTRAINTS_BLOCK:
        return False

    project_path = PROJECT_ROOT / "project.k.json"
    if project_path.exists():
        try:
            project_data = json.loads(project_path.read_text())
            for spec_ref in project_data.get("specs", {}).values():
                spec_dir = spec_ref.get("spec_dir", "") or "."
                spec_dir_path = Path(spec_dir)
                if not spec_dir_path.is_absolute():
                    spec_dir_path = PROJECT_ROOT / spec_dir_path
                if _spec_has_unverified(spec_dir_path / "spec.k.json"):
                    return True
            return False
        except Exception:
            return False

    return _spec_has_unverified(PROJECT_ROOT / "spec.k.json")

def is_edit_blocked_by_unverified_constraints(file_path: str = None) -> bool:
    """Check if editing is blocked due to unverified constraints.

    This function is used in hooks (handler_write.py, handler_edit.py) to prevent
    modifications when the spec has unverified constraints.

    Args:
        file_path: Optional file path being edited (for context, not currently used)

    Returns:
        True if unverified constraints exist and editing should be blocked, False otherwise.
    """
    return have_unverified_constraints()

__all__ = [
    "send_error",
    "is_knowledge_file",
    "have_unverified_constraints",
    "is_edit_blocked_by_unverified_constraints",
]
