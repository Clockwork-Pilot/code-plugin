"""Conftest for plugin tests.

Tests the hook plugin in isolation without parent project dependencies.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
_TEST_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEST_PLUGIN_DATA = Path(tempfile.mkdtemp(prefix="cwpilot-pytest-data-"))
atexit.register(shutil.rmtree, _TEST_PLUGIN_DATA, ignore_errors=True)

# Always import this checkout's plugin package. A bare ``pytest`` can otherwise find
# an unrelated ``src`` package first (for example, cwpilot's own), whose config module
# has a different interface. config.py also requires the hook-host paths at import time.
sys.path.insert(0, str(_PLUGIN_ROOT))
os.environ.setdefault("CWPILOT_PROJECT_ROOT", str(_TEST_PROJECT_ROOT))
os.environ.setdefault("PLUGIN_ROOT", str(_PLUGIN_ROOT))
os.environ.setdefault("PLUGIN_DATA", str(_TEST_PLUGIN_DATA))

# src.hook_logging binds its logging target when imported, so set a disposable default
# before any test can trigger setup_logger().
_TEST_PLUGIN_LOG_DIR = tempfile.mkdtemp(prefix="cwpilot-pytest-")
atexit.register(shutil.rmtree, _TEST_PLUGIN_LOG_DIR, ignore_errors=True)
os.environ.setdefault(
    "CWPILOT_PLUGIN_LOG_FILE",
    os.path.join(_TEST_PLUGIN_LOG_DIR, "cwpilot-plugin-hooks.log"),
)

import src.config as _config

# config.PROJECT_ROOT is the one designated way to resolve the project root; read it
# here at import time, which is before any test's monkeypatch, so this stays the
# REAL root the snapshot below needs.
_REAL_PROJECT_ROOT = _config.PROJECT_ROOT
_REAL_CHANGES_TRACKER_DIR = _REAL_PROJECT_ROOT / ".changes-tracker"


def _real_changes_tracker_snapshot() -> dict:
    """mtimes of every file under the REAL (non-isolated) .changes-tracker/ dir.

    A test that reads config.CHANGES_TRACKER_DIR before isolate_plugin_dirs applies
    its monkeypatch (module import time, a fixture that forgets the autouse one as a
    dependency, a bare unrestored attribute write, ...) silently writes into this
    live project's own ledger instead of its isolated tmp_path. That already happened
    once: a prior version of isolate_plugin_dirs set os.environ instead of the
    config module attribute ledger code actually reads, so isolation was a total
    no-op and test runs left fake playbook_runs entries in this session's real
    session_main.json. This snapshot is the guard against that recurring silently.
    """
    snap = {}
    if _REAL_CHANGES_TRACKER_DIR.is_dir():
        for p in _REAL_CHANGES_TRACKER_DIR.rglob("*"):
            if p.is_file():
                snap[str(p.relative_to(_REAL_PROJECT_ROOT))] = p.stat().st_mtime
    return snap


_real_snapshot_before = None


def pytest_sessionstart(session):  # noqa: ARG001 -- pytest hook signature
    global _real_snapshot_before
    _real_snapshot_before = _real_changes_tracker_snapshot()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 -- pytest hook signature
    if _real_snapshot_before is None:
        return
    after = _real_changes_tracker_snapshot()
    before = _real_snapshot_before
    changed = {
        k: (before.get(k), after.get(k))
        for k in set(before) | set(after)
        if before.get(k) != after.get(k)
    }
    if changed:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        raise AssertionError(
            f"[{worker}] Test suite modified the REAL {_REAL_CHANGES_TRACKER_DIR} during "
            f"this run -- some test escaped isolation (isolate_plugin_dirs not applied, "
            f"or a test set config.CHANGES_TRACKER_DIR directly without restoring it): "
            f"{changed}"
        )


def pytest_configure(config):
    """Register the serial marker."""
    config.addinivalue_line("markers", "serial: mark test as serial (will not run concurrently with xdist)")


def pytest_collection_modifyitems(config, items):
    """Enforce @pytest.mark.serial by marking with xdist_group for serial execution.

    Serial-marked tests use multiprocessing with barriers/queues which deadlock
    when distributed to xdist worker processes. This hook marks them with
    xdist_group(group_count=1) to ensure serial execution.
    """
    for item in items:
        if item.get_closest_marker('serial'):
            # Mark with xdist_group to prevent parallel execution
            item.add_marker(pytest.mark.xdist_group(name="serial", group_count=1))


@pytest.fixture(autouse=True)
def isolate_plugin_dirs(tmp_path, monkeypatch):
    """Ensure every test uses isolated, per-test log/ledger directories.

    CHANGES_TRACKER_DIR/HOOKS_LOG_FILE are read as module attributes by every call site,
    not re-derived from the
    environment per call -- config.py computes them from os.environ ONCE, at first
    import. Setting os.environ here (the old approach) therefore had no effect after
    that first import already happened during test collection: every test run was
    silently writing into the REAL project's .changes-tracker/session_main.json
    instead of an isolated directory, leaking fake ledger entries across test runs
    into live session state. monkeypatch.setattr on the config module itself is what
    actually redirects every reader, and tmp_path gives each test its own directory
    that pytest cleans up automatically (no more cross-run accumulation).
    """
    monkeypatch.setattr(
        _config, "HOOKS_LOG_FILE", tmp_path / "cwpilot-plugin-hooks.log"
    )
    monkeypatch.setattr(_config, "CHANGES_TRACKER_DIR", tmp_path / ".changes-tracker")
    yield


@pytest.fixture
def _isolated_ledger_dir(tmp_path_factory, monkeypatch):
    """A fresh, pre-created ledger directory, wired up as config.CHANGES_TRACKER_DIR
    for the duration of the test. Distinct from isolate_plugin_dirs' per-test
    tmp_path (which every test gets automatically) only in that tests which want to
    assert against the resolved directory need a named handle to it.
    """
    d = tmp_path_factory.mktemp("ledger")
    monkeypatch.setattr(_config, "CHANGES_TRACKER_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _cwpilot_guard_allows_by_default(monkeypatch):
    """handler_bash's cwpilot guard resolves the real install on the test machine.
    Most handler tests drive bare `cwpilot ...` commands to exercise something else
    (sub-agent gating, heartbeats); stub the guard so they stay about that.
    tests/test_cwpilot_guard.py exercises the guard itself and re-enables it."""
    import hooks.handler_bash as handler_bash
    monkeypatch.setattr(handler_bash, "check_cwpilot_command", lambda command: None)
