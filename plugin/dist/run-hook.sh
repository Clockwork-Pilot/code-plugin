#!/bin/bash
# The plugin's only hook entrypoint. Every entry in hooks/hooks.json calls:
#
#   ${CLAUDE_PLUGIN_ROOT}/dist/run-hook.sh <handler_name>
#
# The hookrunner is the binary install.sh puts at
#   <versions_dir>/hookrunner-v<plugin version>/hookrunner
# (versions_dir: $CWPILOT_VERSIONS_DIR, default ~/.local/share/cwpilot-versions). That is
# the only place it is looked for: $HOOKRUNNER_BIN and $PATH are never consulted, because
# it runs on every hook with nobody typing a command, so an environment able to name it
# would be arbitrary code in the process that sees every prompt and tool call.
# A hook never downloads anything. SessionStart tells the user how to install it; until
# then, and whenever it is absent, the same handlers run from source below.
set -eu

handler="${1:?run-hook.sh: missing handler name}"
shift

# $CLAUDE_PLUGIN_ROOT is what the harness guarantees to every hook subprocess. The
# Codex hook host uses PLUGIN_ROOT instead, so accept it only as a fallback. No
# fallback to ../ from $0: a wrong root would run the wrong checkout's handlers,
# which is worse than not running at all.
plugin_root="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-}}"
if [ -z "${plugin_root}" ]; then
  echo "run-hook.sh: CLAUDE_PLUGIN_ROOT or PLUGIN_ROOT is unset" >&2
  exit 1
fi

# This plugin's version, from the manifest of the host it runs under (Codex ships
# .codex-plugin/plugin.json, Claude Code .claude-plugin/plugin.json; the other is only a
# fallback). The hookrunner is released under the same version as the plugin.
if [ "${CWPILOT_AGENT_PROVIDER:-}" = "codex" ]; then
  manifests=".codex-plugin/plugin.json .claude-plugin/plugin.json"
else
  manifests=".claude-plugin/plugin.json .codex-plugin/plugin.json"
fi
version=""
for manifest in ${manifests}; do
  version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${plugin_root}/${manifest}" 2>/dev/null | head -1)"
  [ -n "${version}" ] && break
done

binary=""
if [ -n "${version}" ]; then
  binary="${CWPILOT_VERSIONS_DIR:-${HOME:-}/.local/share/cwpilot-versions}/hookrunner-v${version}/hookrunner"
fi
if [ -n "${binary}" ] && [ -x "${binary}" ]; then
  exec "${binary}" "$handler" "$@"
fi

# Source fallback: no binary anywhere, so run the same handlers from the checkout.
cd "$plugin_root"
hook_python="${CWPILOT_PYTHON:-python3}"
exec "$hook_python" -m hooks "$handler" "$@"
