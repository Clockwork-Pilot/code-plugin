"""Regression tests for the post-reorganization Linux build paths."""

import re
import shlex
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.build-linux"
BASE_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.build-base"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-linux.sh"


def _nuitka_command() -> list[str]:
    """Parse the Dockerfile's Nuitka invocation."""
    lines = DOCKERFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.lstrip().startswith("RUN /venv/bin/python3 -m nuitka"))
    command_lines = []
    for line in lines[start:]:
        fragment = line.strip()
        if fragment.startswith("RUN "):
            fragment = fragment[4:]
        command_lines.append(fragment.rstrip("\\").strip())
        if not line.rstrip().endswith("\\"):
            break

    command = shlex.split(" ".join(command_lines))
    assert command[:4] == ["/venv/bin/python3", "-m", "nuitka", "--onefile"]
    return command


def _build_script_out_dir() -> Path:
    """Parse and expand the build script's OUT_DIR assignment."""
    match = re.search(r"^OUT_DIR=(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))$",
                      BUILD_SCRIPT.read_text(), re.MULTILINE)
    assert match, "build-linux.sh must define OUT_DIR"
    expression = next(value for value in match.groups() if value is not None)
    expanded = expression.replace("${REPO_ROOT}", str(REPO_ROOT))
    expanded = expanded.replace("$REPO_ROOT", str(REPO_ROOT))
    return Path(expanded)


def test_nuitka_entrypoint_and_output_are_in_distributed_plugin_tree():
    """The Docker build must compile the reorganized plugin entrypoint."""
    command = _nuitka_command()

    assert command[-1] == "hooks/__main__.py"
    assert "--output-dir=/out/linux-x86_64" in command
    assert "--static-libpython=yes" in command
    entrypoint = REPO_ROOT / "plugin" / command[-1]
    assert entrypoint.is_file()


def test_nuitka_build_has_the_portable_linux_toolchain():
    """The Docker image must target x86_64 and preflight Nuitka's native tools.

    The toolchain (gcc/patchelf) lives in the published base image
    (docker/Dockerfile.build-base), not in this Dockerfile directly. The base is the
    PyPA manylinux_2_28 image, which carries both plus the CPython 3.11 build.
    """
    dockerfile = DOCKERFILE.read_text()
    base_dockerfile = BASE_DOCKERFILE.read_text()

    assert "ARG BUILD_BASE_IMAGE=ghcr.io/clockwork-pilot/code-plugin-build-base:3" in dockerfile
    assert "FROM --platform=linux/amd64 ${BUILD_BASE_IMAGE}" in dockerfile
    assert "/venv/bin/python3 -c 'import nuitka'" in dockerfile

    assert "FROM --platform=linux/amd64 quay.io/pypa/manylinux_2_28_x86_64" in base_dockerfile
    assert "/opt/python/cp311-cp311/bin" in base_dockerfile
    # Nuitka links manylinux's static libpython, shipped as a tarball until unpacked.
    assert "static-libs-for-embedding-only.tar.xz" in base_dockerfile
    assert "patchelf" in base_dockerfile


def test_build_script_builds_and_selects_a_local_base_image():
    script = BUILD_SCRIPT.read_text()

    assert 'BASE_IMAGE_TAG="claude-plugin-build-base:local"' in script
    assert '-f "${REPO_ROOT}/docker/Dockerfile.build-base"' in script
    assert '"${BASE_IMAGE_TAG}"' in script
    assert '--build-arg "BUILD_BASE_IMAGE=${BASE_IMAGE_TAG}"' in script


def test_nuitka_includes_dynamic_handlers_and_runtime_data():
    command = _nuitka_command()

    for handler in ("bash", "edit", "post_change", "session_start", "stop",
                    "subagent_start", "subagent_stop", "write"):
        assert f"--include-module=hooks.handler_{handler}" in command


def test_build_output_lands_where_ci_collects_it():
    """The copied binary must land where the CI smoke-test and release steps read it."""
    out_dir = _build_script_out_dir()

    assert out_dir == REPO_ROOT / "plugin" / "bin" / "linux-x86_64"
