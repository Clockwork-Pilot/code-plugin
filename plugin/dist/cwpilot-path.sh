#!/bin/bash
# Prints the command to type for the cwpilot this plugin trusts, and nothing else on stdout:
# plain `cwpilot` when $PATH reaches that binary, else its absolute versioned path.
#
#   <plugin>/dist/cwpilot-path.sh
#
# A thin wrapper over the `handler_cwpilot_path` hook handler, so there is one resolver
# (src/binary_resolver.py) and one install command (shared with SessionStart). It runs
# through run-hook.sh, i.e. the installed hookrunner when there is one, else python3.
# What it prints is what the PreToolUse guard accepts. Exit: 0 command printed;
# 1 not installed; 2 fails its signature (stderr carries the install command).
set -eu
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script is invoked by explicit path, so its own location IS the plugin root; the
# agent's shell does not carry the harness's CLAUDE_PLUGIN_* variables.
export CLAUDE_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-$(dirname "${here}")}}"
export CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${PLUGIN_DATA:-${TMPDIR:-/tmp}/cwpilot-plugin-data}}"
exec "${here}/run-hook.sh" handler_cwpilot_path "$@" </dev/null
