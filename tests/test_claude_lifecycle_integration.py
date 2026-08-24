"""Durable/in-process integration for optional Claude lifecycle telemetry."""

import ast
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import spindle_claude_driver as driver
from spindle import lifecycle as lc
from tests.lifecycle_fixtures import make_spool_record, read_provider


def _telemetry(tmp_path, monkeypatch, spool_id="claude-integration"):
    store = tmp_path / "store"
    make_spool_record(store, spool_id)
    monkeypatch.setenv("SPINDLE_OWNER_STORE", str(store))
    monkeypatch.setenv("SPINDLE_OWNER_SPOOL_ID", spool_id)
    return driver.ClaudeLifecycleTelemetry(), store, spool_id


def test_driver_has_one_durable_telemetry_sink_and_no_spool_json_writer():
    source = Path(driver.__file__).read_text()
    tree = ast.parse(source)
    apply_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "apply_event"
    ]
    assert len(apply_calls) == 1
    assert "SPINDLE_OWNER_SPOOL_ID}.json" not in source
    assert "SPINDLE_OWNER_STORE}.json" not in source


def test_guarded_lazy_load_restores_absent_and_existing_environment(monkeypatch):
    guard = "_SPINDLE_STORE_SUPERVISOR"
    monkeypatch.delenv(guard, raising=False)
    runtime = driver._load_lifecycle_runtime()
    assert runtime.apply_event is not lc.apply_event
    assert runtime.TRANSPORT_STARTED == lc.TRANSPORT_STARTED
    assert guard not in os.environ

    monkeypatch.setenv(guard, "outer-owner")
    runtime = driver._load_lifecycle_runtime()
    assert runtime.TRANSPORT_STARTED == lc.TRANSPORT_STARTED
    assert os.environ[guard] == "outer-owner"


def test_guarded_lazy_load_fresh_process_does_not_run_store_maintenance(tmp_path):
    home = tmp_path / "home"
    store = home / "spools"
    store.mkdir(parents=True)
    record = store / "old-pending.json"
    record.write_text(
        json.dumps(
            {
                "id": "old-pending",
                "status": "pending",
                "created_at": "2020-01-01T00:00:00+00:00",
                "pid": None,
            },
            indent=2,
        )
    )
    before = record.read_bytes()
    code = (
        "import os, sys; import spindle_claude_driver as d; "
        "d._load_lifecycle_runtime(); "
        "assert '_SPINDLE_STORE_SUPERVISOR' not in os.environ; "
        "assert 'spindle' not in sys.modules"
    )
    env = {
        **os.environ,
        "SPINDLE_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("_SPINDLE_STORE_SUPERVISOR", None)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(driver.__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert record.read_bytes() == before
    assert not list(store.glob(".supervisor*"))


def test_activity_threshold_and_authoritative_flush_order(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    for _ in range(49):
        telemetry.activity()
    assert read_provider(store, spool_id) is None

    telemetry.activity()
    provider = read_provider(store, spool_id)
    assert provider["sequence"] == 1
    assert provider["activity_count"] == 1

    telemetry.activity()
    telemetry.transport_started()
    provider = read_provider(store, spool_id)
    assert provider["sequence"] == 3
    assert provider["activity_count"] == 2
    assert provider["last_event_type"] == lc.TRANSPORT_STARTED


def test_activity_timestamps_survive_threshold_and_authoritative_flush(tmp_path, monkeypatch):
    values = iter(f"2026-08-24T14:00:{second:02d}+00:00" for second in range(60))
    monkeypatch.setattr(driver, "_utc_observed_at", lambda: next(values))
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    for _ in range(50):
        telemetry.activity()
    assert read_provider(store, spool_id)["last_activity_at"] == "2026-08-24T14:00:49+00:00"

    telemetry.activity()
    telemetry.transport_started()
    provider = read_provider(store, spool_id)
    assert provider["last_activity_at"] == "2026-08-24T14:00:50+00:00"
    assert provider["last_event_type"] == lc.TRANSPORT_STARTED


def test_raw_activity_never_overwrites_or_revives_structured_work(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.observe({"type": "system", "subtype": "task_started", "task_id": "a"})
    for _ in range(50):
        telemetry.observe({"type": "assistant", "message": {"content": "SECRET"}})
    assert read_provider(store, spool_id)["active_work"] == "1 Claude task active"

    telemetry.observe(
        {"type": "system", "subtype": "task_notification", "task_id": "a", "status": "completed"}
    )
    for _ in range(50):
        telemetry.observe({"type": "assistant", "message": {"content": "SECRET"}})
    assert read_provider(store, spool_id)["active_work"] is None


def test_telemetry_skips_busy_owner_lock_and_recovers_on_next_event(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    fd = os.open(str(store / f"{spool_id}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        telemetry.transport_started()
        assert read_provider(store, spool_id) is None
        assert telemetry.enabled is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    telemetry.turn_started()
    assert read_provider(store, spool_id)["protocol_state"] == lc.PROTOCOL_ACTIVE


def test_transport_loss_exit_and_terminal_are_independent(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.transport_started()
    telemetry.transport_lost("reader closed")
    assert read_provider(store, spool_id)["connection_state"] == lc.CONNECTION_LOST
    telemetry.observe({"type": "result", "subtype": "success", "is_error": False})
    telemetry.finalize_result("complete")
    telemetry.transport_exited(0)
    provider = read_provider(store, spool_id)
    assert provider["protocol_terminal_kind"] == lc.TERMINAL_COMPLETED
    assert provider["connection_state"] == lc.CONNECTION_EXITED


@pytest.mark.parametrize("status", ["pending", "running", "paused", "future", None, 7])
def test_task_notification_nonterminal_or_malformed_status_does_not_finish(tmp_path, monkeypatch, status):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch, spool_id=f"task-{status}")
    telemetry.observe({"type": "system", "subtype": "task_started", "task_id": "a"})
    telemetry.observe(
        {"type": "system", "subtype": "task_notification", "task_id": "a", "status": status}
    )
    assert read_provider(store, spool_id)["active_work"] == "1 Claude task active"
