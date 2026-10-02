#!/usr/bin/env bash
# Builds the portable Linux x86_64 hookrunner binary via Docker and copies it to
# plugin/bin/linux-x86_64/hookrunner. Run from anywhere; paths are resolved relative to
# this script.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_IMAGE_TAG="claude-plugin-build-base:local"
IMAGE_TAG="claude-plugin-hookrunner-build"
# plugin/bin/<platform>/<name> -- where CI's smoke test and release steps pick the artifact up.
OUT_DIR="${REPO_ROOT}/plugin/bin/linux-x86_64"

docker build \
  -f "${REPO_ROOT}/docker/Dockerfile.build-base" \
  -t "${BASE_IMAGE_TAG}" \
  "${REPO_ROOT}"

docker build \
  -f "${REPO_ROOT}/docker/Dockerfile.build-linux" \
  --build-arg "BUILD_BASE_IMAGE=${BASE_IMAGE_TAG}" \
  -t "${IMAGE_TAG}" \
  "${REPO_ROOT}"

container_id="$(docker create "${IMAGE_TAG}")"
trap 'docker rm -f "${container_id}" >/dev/null' EXIT

mkdir -p "${OUT_DIR}"
docker cp "${container_id}:/out/linux-x86_64/hookrunner" "${OUT_DIR}/hookrunner"
chmod +x "${OUT_DIR}/hookrunner"

echo "Built ${OUT_DIR}/hookrunner"
"${OUT_DIR}/hookrunner" 2>&1 | head -1 || true
