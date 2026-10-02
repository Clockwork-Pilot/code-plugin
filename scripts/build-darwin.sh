#!/usr/bin/env bash
# Builds the portable macOS arm64 hookrunner binary natively and copies it to
# plugin/bin/darwin-arm64/hookrunner. Run from anywhere; paths are resolved relative to
# this script.
#
# No Docker here, unlike build-linux.sh: Nuitka compiles to machine code and cannot
# cross-compile, so the darwin-arm64 artifact can only come from an actual Apple
# Silicon Mac. hookrunner has no compiled dependencies (just python-dotenv + nuitka),
# so this needs nothing beyond Xcode's command line tools -- no MacPorts, no building
# `cryptography` from source, none of the machinery cwpilot's macOS build carries.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# plugin/bin/<platform>/<name> -- where CI's smoke test and release steps pick the artifact up.
# darwin-arm64, matching install.sh's platform name: lowercased `uname -s`
# verbatim, no "macos" alias.
OUT_DIR="${REPO_ROOT}/plugin/bin/darwin-arm64"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "build-darwin.sh: must run on Apple Silicon macOS (Nuitka cannot cross-compile)" >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
venv_dir="$(mktemp -d "${TMPDIR:-/tmp}/hookrunner-build-venv.XXXXXX")"
trap 'rm -rf "${venv_dir}"' EXIT

"${PYTHON}" -m venv "${venv_dir}"
"${venv_dir}/bin/pip" install --no-cache-dir -r "${REPO_ROOT}/requirements-build.txt"

mkdir -p "${OUT_DIR}"

# runpy dispatches handlers by name, so make every handler part of Nuitka's static
# module graph -- same reasoning, and the same list, as docker/Dockerfile.build-linux.
(
  cd "${REPO_ROOT}/plugin"
  "${venv_dir}/bin/python3" -m nuitka \
    --onefile \
    --output-dir="${OUT_DIR}" \
    --output-filename=hookrunner \
    --company-name=ClockworkPilot \
    --product-name=hookrunner \
    --static-libpython=no \
    --include-package=hooks \
    --include-package=src \
    --include-module=hooks.handler_bash \
    --include-module=hooks.handler_compaction \
    --include-module=hooks.handler_edit \
    --include-module=hooks.handler_post_change \
    --include-module=hooks.handler_session_start \
    --include-module=hooks.handler_stop \
    --include-module=hooks.handler_subagent_start \
    --include-module=hooks.handler_subagent_stop \
    --include-module=hooks.handler_write \
    --assume-yes-for-downloads \
    hooks/__main__.py
)

chmod +x "${OUT_DIR}/hookrunner"

echo "Built ${OUT_DIR}/hookrunner"
"${OUT_DIR}/hookrunner" 2>&1 | head -1 || true
