"""S2-C: checkpoint-driven logical-owner crash contracts."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import spindle
from spindle.namespace_owner import create_control_request, iter_control_requests, read_control_receipt


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


def test_s2_c_ctl_02_watchdog_cleanup_preserves_the_acknowledged_terminal(
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
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    assert terminal["lifecycle"]["owner_crashed_after_cleanup"] is True
    assert terminal["error"] == "Cancelled"
    settled = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert settled.child_exit_observed_at == terminal["owner_episode"]["cleanup"]["child_exit_observed_at"]
    assert settled.cleanup_outcome == "cleaned"


@pytest.mark.parametrize(
    ("request_kind", "expected_status", "expected_error", "expected_terminal"),
    (
        ("cancel", "error", "Cancelled", "cancelled"),
        ("drop", "error", "Cancelled", "cancelled"),
        ("timeout", "timeout", "Timeout after 100s", "timeout"),
    ),
)
def test_s2_c_ctl_03_crash_after_cleanup_receipt_recovers_requested_terminal(
    watchdog_owner_case,
    request_kind,
    expected_status,
    expected_error,
    expected_terminal,
):
    owner = watchdog_owner_case(
        "ignore-term",
        pause_checkpoint="cleanup_receipt_child_exit_durable",
        disable_pdeathsig=True,
        timeout=100,
    )
    request = _public_cancel(owner) if request_kind == "drop" else owner.request(request_kind)
    assert owner.receive_checkpoint()["name"] == "cleanup_receipt_child_exit_durable"
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert receipt.owner_acknowledged_at
    assert receipt.child_exit_observed_at
    assert receipt.cleanup_outcome == "cleaned"
    assert _provider_gone(owner)
    assert owner.spool()["owner_episode"]["phase"] == "accepted"
    assert not (owner.store / f"{owner.spool_id}.owner-exit").exists()
    owner.kill_owner()
    terminal = _finalize_after_crash(owner)
    assert terminal["status"] == expected_status
    assert terminal["error"] == expected_error
    assert terminal["lifecycle"]["normalized_terminal_kind"] == expected_terminal
    assert terminal["lifecycle"]["owner_crashed_after_cleanup"] is True


def test_recovery_settles_late_same_generation_request_after_winning_cleanup_receipt(
    watchdog_owner_case,
):
    owner = watchdog_owner_case(
        "ignore-term",
        pause_checkpoint="cleanup_receipt_child_exit_durable",
        disable_pdeathsig=True,
        timeout=100,
        generation=2,
    )
    winner = owner.request("cancel")
    assert owner.receive_checkpoint()["name"] == "cleanup_receipt_child_exit_durable"
    late_timeout = owner.request("timeout")
    stale = create_control_request(owner.store, owner.spool_id, "drop", 1, "stale-stage4-test")
    assert read_control_receipt(owner.store, owner.spool_id, late_timeout.request_id) is None
    assert read_control_receipt(owner.store, owner.spool_id, stale.request_id) is None

    owner.kill_owner()
    terminal = _finalize_after_crash(owner)

    assert terminal["status"] == "error"
    assert terminal["error"] == "Cancelled"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    assert read_control_receipt(owner.store, owner.spool_id, winner.request_id).cleanup_outcome == "cleaned"
    superseded = read_control_receipt(owner.store, owner.spool_id, late_timeout.request_id)
    assert superseded is not None
    assert superseded.owner_acknowledged_at is None
    assert superseded.cleanup_outcome == "rejected_superseded"
    stale_receipt = read_control_receipt(owner.store, owner.spool_id, stale.request_id)
    assert stale_receipt is not None
    assert stale_receipt.owner_acknowledged_at is None
    assert stale_receipt.cleanup_outcome == "rejected_stale_generation"


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
