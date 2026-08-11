"""S2-X: real cross-PID-namespace owner contracts."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import spindle
from spindle.namespace_owner import iter_control_requests, read_control_receipt

pytestmark = pytest.mark.ns_required


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


OBSERVER_PROLOGUE = r"""
import json, os, sys
from pathlib import Path
import spindle
store = Path(sys.argv[1])
spindle.SPINDLE_DIR = store
def forbidden(*args, **kwargs):
    raise AssertionError("foreign observer attempted a numeric signal")
os.kill = forbidden
os.killpg = forbidden
"""


def _terminal(owner, status):
    return wait_until(
        lambda: (
            value
            if (value := owner.spool()) and value.get("status") == status
            else None
        )
    )


def test_s2_x_ns_01_bwrap_observer_cannot_see_owner_pid_but_sees_held_lock(
    watchdog_owner_case,
    foreign_pid_namespace,
):
    owner = watchdog_owner_case("healthy-turn")
    source = OBSERVER_PROLOGUE + r"""
from spindle.namespace_owner import ProcessIdentity, assess_process_liveness, probe_ownership_lock, reconcile_owner_episode
identity = ProcessIdentity.from_dict(json.loads((store / f"{sys.argv[2]}.owner-identity").read_text()))
current = os.stat("/proc/self/ns/pid")
lock = probe_ownership_lock(store / f"{sys.argv[2]}.process-owner", identity)
liveness = assess_process_liveness(identity)
result = reconcile_owner_episode(lock, liveness, exit_evidence=True)
print(json.dumps({
    "namespace": [current.st_dev, current.st_ino],
    "owner_namespace": [identity.namespace.device, identity.namespace.inode],
    "owner_pid": identity.pid,
    "observer_pid": os.getpid(),
    "owner_proc_visible": Path(f"/proc/{identity.pid}").exists(),
    "lock": lock.state,
    "liveness": liveness.state,
    "reason": liveness.reason,
    "reconciliation": result.state,
}))
"""
    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
    assert observed["namespace"] != observed["owner_namespace"]
    assert observed["owner_proc_visible"] is False, (
        observed["owner_pid"],
        observed["observer_pid"],
        observed,
    )
    assert observed["lock"] == "held"
    assert observed["liveness"] == "unverifiable"
    assert observed["reason"] == "namespace_mismatch"
    assert observed["reconciliation"] == "active"


def test_s2_x_ns_02_foreign_finalize_and_timeout_do_not_mutate_held_spool(
    watchdog_owner_case,
    foreign_pid_namespace,
):
    owner = watchdog_owner_case("healthy-turn", pause_checkpoint="control_ack_durable", timeout=1)
    with patch("spindle.SPINDLE_DIR", owner.store):
        spool = owner.spool()
        spool["created_at"] = (datetime.now() - timedelta(seconds=5)).isoformat()
        spindle._write_spool(owner.spool_id, spool)
    stdout_path = owner.store / f"{owner.spool_id}.stdout"
    wait_until(lambda: b"partial provider output" in stdout_path.read_bytes())
    stdout_before = stdout_path.read_bytes()
    lock_before = (owner.store / f"{owner.spool_id}.process-owner").stat()
    source = OBSERVER_PROLOGUE + r"""
before = (store / f"{sys.argv[2]}.json").read_bytes()
finalized = spindle._check_and_finalize_spool(sys.argv[2])
active = spindle._reconcile_spool_step(sys.argv[2])
after = json.loads((store / f"{sys.argv[2]}.json").read_text())
print(json.dumps({"finalized": finalized, "active": active, "status": after["status"], "lifecycle": after["lifecycle"]}))
"""
    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "control_ack_durable"
    assert observed["finalized"] is False
    assert observed["active"] is True
    assert observed["status"] == "running"
    assert observed["lifecycle"]["public_stop_state"] == "stopping"
    assert stdout_path.read_bytes() == stdout_before
    lock_after = (owner.store / f"{owner.spool_id}.process-owner").stat()
    assert (lock_after.st_dev, lock_after.st_ino) == (lock_before.st_dev, lock_before.st_ino)
    owner.resume()
    assert _terminal(owner, "timeout")["lifecycle"]["normalized_terminal_kind"] == "timeout"


@pytest.mark.parametrize("store_kind", ["bridge_schema1", "schema2_legacy"])
def test_s2_x_ctl_01_foreign_cancel_reaches_owner_for_both_store_layouts(
    watchdog_owner_case,
    foreign_pid_namespace,
    store_kind,
):
    owner = watchdog_owner_case(
        "ignore-term",
        pause_checkpoint="after_term_before_kill",
        store_kind=store_kind,
    )
    source = OBSERVER_PROLOGUE + r"""
message = spindle._spin_drop_locked(sys.argv[2])
ns = os.stat("/proc/self/ns/pid")
print(json.dumps({"message": message, "namespace": [ns.st_dev, ns.st_ino]}))
"""
    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "after_term_before_kill"
    request = list(iter_control_requests(owner.store, owner.spool_id))[0]
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert "Cancellation requested" in observed["message"]
    assert [request.observer_namespace.device, request.observer_namespace.inode] == observed["namespace"]
    assert request.owner_generation == owner.spool()["owner_generation"]
    assert receipt.owner_acknowledged_at
    assert owner.spool()["lifecycle"]["public_stop_state"] == "stopping"
    with patch("spindle.SPINDLE_DIR", owner.store):
        assert spindle._count_running() == 1
    owner.resume()
    terminal = _terminal(owner, "error")
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert terminal["error"] == "Cancelled"
    assert receipt.forced_cleanup_started_at
    assert receipt.forced_cleanup_completed_at
    assert not Path(f"/proc/{owner.provider_pid}").exists()


@pytest.mark.parametrize("store_kind", ["bridge_schema1", "schema2_legacy"])
def test_s2_x_ctl_02_foreign_timeout_reaches_owner_for_both_store_layouts(
    watchdog_owner_case,
    foreign_pid_namespace,
    store_kind,
):
    episode_deadline = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    legacy_deadline = datetime(2100, 1, 1, tzinfo=timezone.utc).isoformat()
    owner = watchdog_owner_case(
        "ignore-term",
        pause_checkpoint="provider_ready",
        timeout=100,
        spool_overrides={"wall_deadline_at": legacy_deadline},
        store_kind=store_kind,
    )
    owner_resumed = False
    try:
        assert owner.receive_checkpoint()["name"] == "provider_ready"
        with patch("spindle.SPINDLE_DIR", owner.store):
            with spindle._spool_lock(owner.spool_id) as acquired:
                assert acquired
                spool = spindle._read_spool(owner.spool_id)
                assert spool["wall_deadline_at"] == legacy_deadline
                spool["owner_episode"]["deadline"] = episode_deadline
                spindle._write_spool(owner.spool_id, spool)
        stored = owner.spool()
        assert stored["wall_deadline_at"] == legacy_deadline
        assert stored["owner_episode"]["deadline"] == episode_deadline
        assert stored["wall_deadline_at"] != stored["owner_episode"]["deadline"]
        source = OBSERVER_PROLOGUE + r"""
active = spindle._reconcile_spool_step(sys.argv[2])
ns = os.stat("/proc/self/ns/pid")
print(json.dumps({"active": active, "namespace": [ns.st_dev, ns.st_ino]}))
"""
        observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
        assert observed["active"] is True
        requests = list(iter_control_requests(owner.store, owner.spool_id))
        assert len(requests) == 1
        assert owner.spool()["status"] == "running"
        with patch("spindle.SPINDLE_DIR", owner.store):
            assert spindle._count_running() == 1
        owner.resume()
        owner_resumed = True
        owner.process.wait(timeout=8)
        terminal = owner.spool()
        assert terminal["status"] == "timeout"
        request = requests[0]
        assert request.kind == "timeout"
        assert request.deadline == episode_deadline
        assert [request.observer_namespace.device, request.observer_namespace.inode] == observed["namespace"]
        assert terminal["error"] == "Timeout after 100s"
    finally:
        if owner.process.poll() is None:
            if not owner_resumed:
                try:
                    owner.resume()
                except OSError:
                    pass
            try:
                owner.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                owner.process.terminate()
                try:
                    owner.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    owner.process.kill()
                    owner.process.wait(timeout=2)


def test_s2_x_time_01_owner_self_timeout_does_not_depend_on_foreign_observer(
    watchdog_owner_case,
    foreign_pid_namespace,
    owner_clock,
):
    _current, advance, clock_fd = owner_clock
    owner = watchdog_owner_case("ignore-term", timeout=100, controlled_clock_fd=clock_fd.fileno())
    reserved = owner.spool()
    assert reserved["owner_episode"]["deadline"] == reserved["wall_deadline_at"]
    source = OBSERVER_PROLOGUE + r"""
spool = json.loads((store / f"{sys.argv[2]}.json").read_text())
print(json.dumps({"status": spool["status"], "visible": Path(f"/proc/{spool['owner_pid']}").exists()}))
"""
    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
    assert observed == {"status": "running", "visible": False}
    advance(101)
    assert _terminal(owner, "timeout")["lifecycle"]["normalized_terminal_kind"] == "timeout"


def test_s2_x_leg_01_foreign_legacy_missing_lock_is_unverifiable_and_slot_counted(
    namespace_owner_env,
    foreign_pid_namespace,
):
    store = namespace_owner_env["store"]
    spool_id = "foreign-legacy"
    record = {
        "id": spool_id,
        "status": "running",
        "pid": 424242,
        "process_start_time": "101",
        "created_at": datetime.now().isoformat(),
        "prompt": "legacy",
    }
    (store / f"{spool_id}.json").write_text(json.dumps(record))
    source = OBSERVER_PROLOGUE + r"""
before = (store / f"{sys.argv[2]}.json").read_bytes()
finalized = spindle._check_and_finalize_spool(sys.argv[2])
message = spindle._unspool_sync(sys.argv[2])
count = spindle._count_running()
after = (store / f"{sys.argv[2]}.json").read_bytes()
print(json.dumps({"finalized": finalized, "message": message, "count": count, "unchanged": before == after}))
"""
    observed = foreign_pid_namespace(store, source, spool_id)
    assert observed["finalized"] is False
    assert observed["count"] == 1
    assert observed["unchanged"] is True
    assert "ownership is unverifiable" in observed["message"]
    assert "Manual recovery" in observed["message"]


def test_s2_x_own_01_dead_owner_foreign_observer_cannot_settle_or_free(
    watchdog_owner_case,
    foreign_pid_namespace,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="provider_ready",
        disable_pdeathsig=True,
    )
    assert owner.receive_checkpoint()["name"] == "provider_ready"
    owner.kill_owner()
    assert wait_until(lambda: not Path(f"/proc/{owner.provider_pid}").exists())
    record_path = owner.store / f"{owner.spool_id}.json"
    before = record_path.read_bytes()
    source = OBSERVER_PROLOGUE + r"""
spool = json.loads((store / f"{sys.argv[2]}.json").read_text())
state = spindle._reconcile_spool_ownership(spool).state
finalized = spindle._check_and_finalize_spool(sys.argv[2])
count = spindle._count_running()
after = (store / f"{sys.argv[2]}.json").read_bytes()
print(json.dumps({"state": state, "finalized": finalized, "count": count, "record_size": len(after)}))
"""
    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)
    assert observed["state"] == "unverifiable"
    assert observed["finalized"] is False
    assert observed["count"] == 1
    assert record_path.read_bytes() == before

    with patch("spindle.SPINDLE_DIR", owner.store):
        assert spindle._check_and_finalize_spool(owner.spool_id) is True
        assert spindle._count_running() == 0
    terminal = owner.spool()
    assert terminal["error_kind"] == "owner_transport_loss"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
