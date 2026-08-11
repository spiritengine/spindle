"""Shared test fixtures and isolation for the spindle test suite.

Spindle's storage path is resolved at import time from SPINDLE_HOME. We set that
to a throwaway session directory *before* any test module imports spindle, so no
test - and no monitor thread that escapes a test's own ``with patch(...)`` block -
can ever write into the real ~/.spindle store. A per-test autouse fixture then
points the store at a fresh tmp dir for each test, and a session-end check fails
loudly if the real store was touched anyway.
"""

import os
import tempfile
from pathlib import Path

# Real store location, captured before we redirect, for the session-end backstop.
_REAL_SPINDLE_SPOOLS = Path.home() / ".spindle" / "spools"

# Redirect the store before spindle is imported anywhere. SPINDLE_DIR reads
# SPINDLE_HOME at import; setting it here (conftest is imported before test
# modules) guarantees the module global never points at the real store.
os.environ["SPINDLE_HOME"] = tempfile.mkdtemp(prefix="spindle-test-home-")

import pytest  # noqa: E402, I001


pytest_plugins = ["tests.namespace_owner_fixtures", "tests.owner_episode_fixtures"]


def _snapshot(path: Path) -> frozenset:
    if not path.exists():
        return frozenset()
    return frozenset(p.name for p in path.iterdir())


def pytest_configure(config):
    # Snapshot the real store so we can detect any leak at session end.
    config._spindle_real_snapshot = _snapshot(_REAL_SPINDLE_SPOOLS)


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_spindle_real_snapshot", frozenset())
    after = _snapshot(_REAL_SPINDLE_SPOOLS)
    leaked = sorted(after - before)
    if leaked:
        raise pytest.UsageError(
            f"Tests wrote to the real spindle store {_REAL_SPINDLE_SPOOLS}: {leaked}. "
            "A test likely let a monitor thread outlive its SPINDLE_DIR patch."
        )


@pytest.fixture(autouse=True)
def _isolate_spindle_store(tmp_path, monkeypatch):
    """Point SPINDLE_DIR at a per-test tmp dir for every test.

    Tests that patch SPINDLE_DIR themselves still work (the explicit patch wins
    inside its block and restores to this per-test tmp on exit - never the real
    store). Tests that don't patch get isolation for free.
    """
    import spindle

    store = tmp_path / "spindle-spools"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)

    # Start each test with an empty process-handle registry so a handle written
    # by one test can never bleed into the next.
    spindle._PROC_HANDLES.clear()
    spindle._reload_pending = False

    # Don't let *_spin_sync spawn a real background monitor thread: with the
    # subprocess mocked there's nothing to monitor. Durable-supervisor contract
    # tests use fresh subprocesses and therefore do not inherit these patches.
    monkeypatch.setattr(spindle, "_monitor_spool", lambda spool_id: None)
    monkeypatch.setattr(spindle, "_ensure_store_supervisor_locked", lambda: (True, None))
