"""S2-C: checkpoint-driven logical-owner crash contracts."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import spindle
from spindle.namespace_owner import (
    ProcessIdentity,
    capture_pid_namespace,
    iter_control_requests,
    read_control_receipt,
)


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


def _provider_gone(owner):
    pid = owner.provider_pid
    return pid is None or not Path(f"/proc/{pid}").exists()


def _finalize_after_crash(owner):
    before = owner.spool()
    before_episode = before["owner_episode"]
    assert before["status"] == "running"
    assert before_episode["phase"] == "cleanup_proven"
    assert "release" not in before_episode
    assert "normalized_terminal_kind" not in (before.get("lifecycle") or {})
    with patch("spindle.SPINDLE_DIR", owner.store):
        assert spindle._count_running() == 0
        assert spindle._check_and_finalize_spool(owner.spool_id) is True
        assert spindle._count_running() == 0
    terminal = owner.spool()
    released = terminal["owner_episode"]
    assert released["phase"] == "released"
    assert released["release"]["proved_by"] == "reconciler"
    return terminal


def _public_cancel(owner):
    with patch("spindle.SPINDLE_DIR", owner.store):
        message = spindle._spin_drop_locked(owner.spool_id)
    assert "Cancellation requested" in message
    return list(iter_control_requests(owner.store, owner.spool_id))[0]


def test_s2_c_own_01_healthy_owner_crash_contains_provider_and_preserves_evidence(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="provider_ready",
        disable_pdeathsig=True,
    )
    assert owner.receive_checkpoint()["name"] == "provider_ready"
    stdout_path = owner.store / f"{owner.spool_id}.stdout"
    wait_until(lambda: b"partial provider output" in stdout_path.read_bytes())
    owner.kill_owner()
    assert wait_until(lambda: _provider_gone(owner))
    evidence = json.loads((owner.store / f"{owner.spool_id}.owner-exit").read_text())
    assert evidence["owner_crashed"] is True
    assert evidence["watchdog_contained"] is True
    assert evidence["provider_reaped"] is True
    assert b"partial provider output" in stdout_path.read_bytes()
    assert (owner.store / f"{owner.spool_id}.owner-identity").exists()
    terminal = _finalize_after_crash(owner)
    assert terminal["error_kind"] == "owner_transport_loss"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert terminal["status"] == "error"


def test_s2_c_ctl_01_owner_crash_before_control_ack_preserves_unacked_request(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="control_observed_before_ack",
        disable_pdeathsig=True,
    )
    request = _public_cancel(owner)
    assert owner.receive_checkpoint()["name"] == "control_observed_before_ack"
    owner.kill_owner()
    assert wait_until(lambda: _provider_gone(owner))
    assert read_control_receipt(owner.store, owner.spool_id, request.request_id) is None
    stopping = owner.spool()
    assert stopping["status"] == "running"
    assert stopping["lifecycle"]["public_stop_state"] == "stopping"
    terminal = _finalize_after_crash(owner)
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert terminal["error_kind"] == "owner_transport_loss"


def test_s2_c_ctl_02_owner_crash_after_ack_before_cleanup_is_indeterminate(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="control_ack_durable",
        disable_pdeathsig=True,
    )
    request = _public_cancel(owner)
    assert owner.receive_checkpoint()["name"] == "control_ack_durable"
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert receipt.owner_acknowledged_at
    assert receipt.provider_cancel_attempted_at is None
    assert receipt.child_exit_observed_at is None
    owner.kill_owner()
    assert wait_until(lambda: _provider_gone(owner))
    terminal = _finalize_after_crash(owner)
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert terminal["error"] != "Cancelled"


def test_s2_c_ctl_03_crash_after_cleanup_receipt_recovers_requested_terminal(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "ignore-term",
        pause_checkpoint="cleanup_receipt_durable",
        disable_pdeathsig=True,
    )
    request = _public_cancel(owner)
    assert owner.receive_checkpoint()["name"] == "cleanup_receipt_durable"
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert receipt.owner_acknowledged_at
    assert receipt.child_exit_observed_at
    assert receipt.cleanup_outcome == "cleaned"
    assert _provider_gone(owner)
    owner.kill_owner()
    terminal = _finalize_after_crash(owner)
    assert terminal["error"] == "Cancelled"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    assert terminal["lifecycle"]["owner_crashed_after_cleanup"] is True


def test_s2_c_arm_01_fork_parent_death_race_does_not_orphan_provider(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="provider_forked_before_containment_armed",
        disable_pdeathsig=True,
    )
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "provider_forked_before_containment_armed"
    assert checkpoint["provider_pid"], checkpoint
    lock_inode = (owner.store / f"{owner.spool_id}.process-owner").stat().st_ino
    # The watchdog never inherits the ownership FD.
    for fd_path in Path(f"/proc/{owner.process.pid}/fd").glob("*"):
        try:
            assert os.stat(fd_path).st_ino != lock_inode
        except OSError:
            pass
    owner.kill_owner()
    assert wait_until(lambda: _provider_gone(owner))


def test_s2_c_own_02_crash_before_identity_publication_is_prelaunch_failure(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="identity_lock_acquired",
        disable_pdeathsig=True,
    )
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "identity_lock_acquired"
    assert checkpoint["provider_pid"] is None
    owner.kill_owner()
    spool = owner.spool()
    assert spool["status"] == "error"
    assert spool["error_kind"] == "owner_preacceptance_failure"
    lock_path = owner.store / f"{owner.spool_id}.process-owner"
    assert lock_path.exists()
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_same_id_replacement_crash_and_cancel_use_new_generation_without_slot_leak(
    watchdog_owner_case,
    namespace_owner_env,
):
    store = namespace_owner_env["store"]
    spool_id = "same-id-replacement"
    stale_identity = ProcessIdentity(
        pid=os.getpid(),
        birth_token=spindle._process_start_time(os.getpid()),
        namespace=capture_pid_namespace(),
        owner_generation=1,
        child_pgid=None,
        lock_device=1,
        lock_inode=1,
        lock_created=True,
    )
    (store / f"{spool_id}.owner-identity").write_text(json.dumps(stale_identity.to_dict()))
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="identity_lock_acquired",
        disable_pdeathsig=True,
        generation=2,
        spool_id=spool_id,
        spool_overrides={
            "status": "running",
            "created_at": (datetime.now() - timedelta(days=2)).isoformat(),
            "owner_generation": 2,
            "replacement_starting": True,
            "replacement_owner_generation": 2,
        },
    )
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["owner_generation"] == 2
    reserved = owner.spool()["owner_episode"]
    assert (reserved["generation"], reserved["phase"]) == (2, "reserved")

    with patch("spindle.SPINDLE_DIR", store):
        message = spindle._spin_drop_locked(spool_id)
    assert "not accepting control (reserved)" in message
    assert list(iter_control_requests(store, spool_id)) == []
    mailbox = store / f"{spool_id}.control-mailbox"
    assert not list(mailbox.glob("*.request"))
    assert not list(mailbox.glob("*.receipt"))

    owner.kill_owner()
    spool = owner.spool()
    assert spool["status"] == "error"
    assert spool["error_kind"] == "owner_preacceptance_failure"
    episode = spool["owner_episode"]
    assert (episode["generation"], episode["phase"]) == (2, "aborted")
    assert episode["failure"]
    assert "lock" not in episode
    identity_path = store / f"{spool_id}.owner-identity"
    assert json.loads(identity_path.read_text()) == stale_identity.to_dict()
    assert not (store / f"{spool_id}.owner-exit").exists()
    with patch("spindle.SPINDLE_DIR", store):
        assert spindle._reconcile_spool_ownership(spool).state == "terminalizable"
        assert spindle._spool_blocks_destructive_action(spool) is False
        assert spindle._count_running() == 0
        spindle._cleanup_old_spools()
        assert not (store / f"{spool_id}.json").exists()
        assert not (store / f"{spool_id}.process-owner").exists()
        assert not (store / f"{spool_id}.owner-identity").exists()


def test_s2_c_own_03_owner_crash_contains_setsid_escaped_descendant(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "setsid-grandchild",
        pause_checkpoint="provider_ready",
        disable_pdeathsig=True,
    )
    assert owner.receive_checkpoint()["name"] == "provider_ready"
    descendant_path = owner.store / f"{owner.spool_id}.descendant-pid"
    descendant = int(wait_until(lambda: descendant_path.read_text() if descendant_path.exists() else None))
    owner.kill_owner()
    assert wait_until(lambda: _provider_gone(owner))
    assert wait_until(lambda: not Path(f"/proc/{descendant}").exists())
    terminal = _finalize_after_crash(owner)
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
