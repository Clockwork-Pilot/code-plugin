"""Plugin-owned source package.

This is a regular package rather than an implicit namespace package so that the plugin's
``src`` wins when a consuming project's repository is also present on ``PYTHONPATH``.
The constraint runner intentionally exposes that project path to subprocesses.
"""

import json
import os
from pathlib import Path


def resolve_from_environment(
    envvars: list[str],
    fallback: Path | None = None,
    description: str = "path",
) -> Path:
    """Resolve the first non-empty environment variable, or use ``fallback``.

    A missing value is an error when no fallback is supplied. Keeping this small
    resolver at the package boundary lets configuration modules share the same
    environment-resolution behavior without duplicating it.
    """
    for name in envvars:
        value = os.getenv(name)
        if value:
            return Path(value).resolve()
    if fallback is not None:
        return Path(fallback).resolve()
    raise RuntimeError(
        f"Cannot resolve {description}: set {' or '.join(envvars)}."
    )


def _roots_with_defaults(
    plugin_root_value: Path | None,
    project_root_value: Path | None,
) -> tuple[Path, Path]:
    if plugin_root_value is not None and project_root_value is not None:
        return plugin_root_value, project_root_value

    # Configuration owns environment resolution. Import it lazily so these generic
    # loader helpers do not create a config/__init__ import cycle.
    from .config import plugin_root as configured_plugin_root
    from .config import project_root as configured_project_root

    return (
        (configured_plugin_root()
         if plugin_root_value is None else plugin_root_value),
        (configured_project_root()
         if project_root_value is None else project_root_value),
    )


def _expand_roots(
    arg: str,
    plugin_root: Path | None = None,
    project_root: Path | None = None,
) -> str:
    """Substitute the load-time placeholders only.

    str.replace, never str.format: format would also consume {run_id}/{agent_id},
    which must survive untouched until hook_cli_calls._fill resolves them per call.
    """
    plugin_root, project_root = _roots_with_defaults(plugin_root, project_root)
    result = (arg
            .replace("{plugin_root}", str(plugin_root))
            .replace("{project_root}", str(project_root)))
    # Support {plugin_data} placeholder: resolve from environment (harness-provided
    # in hook subprocesses) with fallback to the installed data directory.
    if "{plugin_data}" in result:
        try:
            from .config import plugin_data as configured_plugin_data
            plugin_data_path = str(configured_plugin_data())
            result = result.replace("{plugin_data}", plugin_data_path)
        except Exception:  # pylint: disable=broad-except
            # If plugin_data() fails (e.g., no env vars set), leave placeholder unchanged
            # so the call can still use the fallback behavior downstream
            pass
    return result


def _load_hook_cli_calls(
    path: Path | None = None,
    *,
    plugin_root: Path | None = None,
    project_root: Path | None = None,
) -> dict:
    """event -> {"cmd": argv, "timeout": seconds|None} or {"policy": {...}}, from ``path``.
    Unusable file -> {}.

    Each entry is an object; the bare argv list it replaced is NOT accepted, so a stale
    file fails visibly (every event resolves to no call) rather than half-working with
    the timeouts silently ignored -- the format predates any release, so there is no
    installed config to be kind to.

    A `policy` entry is a LITERAL: the payload itself, carried in this file, answered
    without a process. It exists because a denylist is data the project authors, not a
    question worth a subprocess -- asking `cwpilot` for it per bash call cost 419ms a
    call, 350ms of that importing pydantic to resolve a lookup whose answer was already
    written down. The value is stored verbatim, with no reshaping or key translation:
    what the project's playbook declares under `policy:` is what a caller receives, so
    the two can be read side by side and compared literally.

    No validation beyond the shape and no warnings: the map is sparse by design --
    callers already treat a missing or empty entry as "no call" -- so a malformed entry
    needs no distinct outcome. A non-numeric timeout is dropped to None, which means the
    default, because a call that runs on the default beats a hook that raises.
    Never raises; this module is imported by every hook subprocess.
    """
    try:
        plugin_root, project_root = _roots_with_defaults(plugin_root, project_root)
        path = (plugin_root / "cw-plugin-cli-hooks.json") if path is None else path
        payload = json.loads(path.read_text(encoding="utf-8"))
        section = payload.get("hook_cli_calls", payload)
        loaded = {}
        for event, entry in section.items():
            if not isinstance(entry, dict):
                continue
            policy = entry.get("policy")
            if isinstance(policy, dict):
                # Carried as-is. No _expand_roots: a literal names no paths, and a
                # policy that silently rewrote its own values would not be verbatim.
                loaded[event] = {"policy": policy}
                continue
            argv = entry.get("cmd")
            if not argv:
                continue
            timeout = entry.get("timeout")
            loaded[event] = {
                "cmd": [
                    _expand_roots(
                        arg,
                        plugin_root=plugin_root,
                        project_root=project_root,
                    )
                    for arg in argv
                ],
                "timeout": timeout if isinstance(timeout, (int, float)) else None,
            }
        return loaded
    except Exception:  # pylint: disable=broad-except
        return {}


def _load_hook_cli_env(
    path: Path | None = None,
    *,
    plugin_root: Path | None = None,
    project_root: Path | None = None,
) -> dict:
    """The top-level "env" block: {NAME: value}, merged over os.environ per call.

    Only LOAD-time placeholders ({plugin_root}/{project_root}) are expanded here, and
    that is the whole intended contents: the roots the CLI needs on every call, said
    once instead of repeated as argv flags in each binding.

    Call-time placeholders ({run_id}/{agent_id}) deliberately do NOT belong here. Those
    can be absent, and hook_cli_calls._fill answers an absent one by abandoning the
    whole call -- writing a fact against a run named "{run_id}" is worse than writing
    none. That check reads argv only, so a placeholder moved into env would survive it
    and the call would run with a bogus value. Keep them in cmd.

    Non-string names/values are dropped rather than coerced: os.environ takes str only,
    and a TypeError here would break the call this module exists to keep fail-soft.
    Never raises; imported by every hook subprocess.
    """
    try:
        plugin_root, project_root = _roots_with_defaults(plugin_root, project_root)
        path = (plugin_root / "cw-plugin-cli-hooks.json") if path is None else path
        payload = json.loads(path.read_text(encoding="utf-8"))
        section = payload.get("env") if isinstance(payload, dict) else None
        if not isinstance(section, dict):
            return {}
        return {
            name: _expand_roots(
                value,
                plugin_root=plugin_root,
                project_root=project_root,
            )
            for name, value in section.items()
            if isinstance(name, str) and isinstance(value, str)
        }
    except Exception:  # pylint: disable=broad-except
        return {}
