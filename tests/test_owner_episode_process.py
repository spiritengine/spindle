"""S2-EP: real-process generation matrix for the authoritative owner episode.

Every crash boundary is driven by an inherited checkpoint socket and settled by
waiting on a real process exit, so no case depends on a sleep.  The episode in
the spool record is the only thing these tests read for lifecycle truth.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import select
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import spindle
from spindle.namespace_owner import create_control_request, iter_control_requests, read_control_receipt
from tests.owner_episode_fixtures import EPISODE_KEY, make_episode

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


def _episode(owner) -> dict:
    record = owner.spool() or {}
    episode = record.get(EPISODE_KEY)
    assert episode is not None, f"spool record publishes no {EPISODE_KEY}: {sorted(record)}"
    return episode


def _bound_inode(owner) -> tuple:
    info = (owner.store / f"{owner.spool_id}.process-owner").stat()
    return info.st_dev, info.st_ino


def _finalize(owner) -> dict:
    with patch("spindle.SPINDLE_DIR", owner.store):
        finalized = spindle._check_and_finalize_spool(owner.spool_id)
    assert finalized is True, "reconciliation refused to settle a proven-terminal episode"
    return owner.spool()


def _reconcile_through_production(owner, entrypoint: str) -> dict:
    with patch("spindle.SPINDLE_DIR", owner.store):
        if entrypoint == "recovery_pass":
            assert spindle._recovery_pass() == []
        else:
            assert entrypoint == "supervisor_step"
            assert spindle._reconcile_spool_step(owner.spool_id) is False
    return owner.spool()


def _assert_terminal_projection_is_idempotent(owner, terminal: dict) -> None:
    record_path = owner.store / f"{owner.spool_id}.json"
    before_bytes = record_path.read_bytes()
    with patch("spindle.SPINDLE_DIR", owner.store):
        assert spindle._check_and_finalize_spool(owner.spool_id) is True
    assert record_path.read_bytes() == before_bytes
    assert owner.spool() == terminal


def _capacity(owner) -> int:
    with patch("spindle.SPINDLE_DIR", owner.store):
        return spindle._count_running()


# --- crash boundaries -------------------------------------------------------


def test_crash_before_binding_aborts_the_reservation(watchdog_owner_case):
    owner = watchdog_owner_case("healthy-turn", pause_checkpoint="identity_lock_acquired", disable_pdeathsig=True)
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "identity_lock_acquired"
    assert _episode(owner)["phase"] == "reserved"

    owner.kill_owner()

    settled = _episode(owner)
    assert settled["phase"] == "aborted"
    assert settled["generation"] == checkpoint["owner_generation"]
    assert settled["failure"]
    assert "lock" not in settled, "an unbound reservation must not claim an inode"
    assert _capacity(owner) == 0


def test_crash_after_binding_ends_in_watchdog_cleanup_then_reconciled_release(watchdog_owner_case):
    owner = watchdog_owner_case("healthy-turn", pause_checkpoint="identity_published", disable_pdeathsig=True)
    assert owner.receive_checkpoint()["name"] == "identity_published"
    bound = _episode(owner)
    assert bound["phase"] == "lock_bound"
    assert (bound["lock"]["device"], bound["lock"]["inode"]) == _bound_inode(owner)

    owner.kill_owner()

    contained = _episode(owner)
    assert contained["phase"] == "cleanup_proven"
    assert contained["containment"]["contained"] is True
    assert contained["generation"] == bound["generation"]

    terminal = _finalize(owner)
    released = terminal[EPISODE_KEY]
    assert released["phase"] == "released"
    assert (released["release"]["device"], released["release"]["inode"]) == (
        bound["lock"]["device"],
        bound["lock"]["inode"],
    )
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert _capacity(owner) == 0


def test_crash_after_acceptance_ends_in_watchdog_cleanup_then_reconciled_release(watchdog_owner_case):
    owner = watchdog_owner_case("healthy-turn", pause_checkpoint="provider_ready", disable_pdeathsig=True)
    assert owner.receive_checkpoint()["name"] == "provider_ready"
    accepted = _episode(owner)
    assert accepted["phase"] == "accepted"
    assert accepted["provider"]["pid"] == owner.provider_pid
    assert accepted["provider"]["namespace"] == accepted["owner"]["namespace"]

    owner.kill_owner()
    assert not Path(f"/proc/{owner.provider_pid}").exists()

    contained = _episode(owner)
    assert contained["phase"] == "cleanup_proven"
    assert contained["containment"]["contained"] is True

    terminal = _finalize(owner)
    assert terminal[EPISODE_KEY]["phase"] == "released"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"


def test_crash_after_cleanup_proven_keeps_the_proven_phase_and_recovers_its_terminal(watchdog_owner_case):
    owner = watchdog_owner_case("ignore-term", pause_checkpoint="cleanup_receipt_durable", disable_pdeathsig=True)
    with patch("spindle.SPINDLE_DIR", owner.store):
        assert "Cancellation requested" in spindle._spin_drop_sync(owner.spool_id)
    assert owner.receive_checkpoint()["name"] == "cleanup_receipt_durable"
    proven = _episode(owner)
    assert proven["phase"] == "cleanup_proven"
    assert proven["cleanup"]["provider_reaped"] is True
    assert proven["winning_request"]["desired_terminal_kind"] == "cancelled"

    owner.kill_owner()

    after_crash = _episode(owner)
    assert after_crash["phase"] == "cleanup_proven"
    assert after_crash["generation"] == proven["generation"]
    assert after_crash["cleanup"] == proven["cleanup"]

    terminal = _finalize(owner)
    assert terminal[EPISODE_KEY]["phase"] == "released"
    assert terminal["error"] == "Cancelled"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    _assert_terminal_projection_is_idempotent(owner, terminal)


@pytest.mark.parametrize("production_entrypoint", ["recovery_pass", "supervisor_step"])
def test_deadline_crossing_after_binding_settles_timeout_without_starting_provider(
    watchdog_owner_case,
    owner_clock,
    production_entrypoint,
):
    _current, advance, clock_fd = owner_clock
    owner = watchdog_owner_case(
        "record-launch",
        timeout=100,
        controlled_clock_fd=clock_fd.fileno(),
        pause_checkpoint="before_provider_start_checks",
    )
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "before_provider_start_checks"
    assert checkpoint["provider_pid"] is None
    assert _episode(owner)["phase"] == "lock_bound"

    advance(101)
    owner.resume()
    assert owner.process.wait(timeout=8) == 124

    proven = _episode(owner)
    assert proven["phase"] == "cleanup_proven"
    assert proven["deadline"] == owner.spool()["wall_deadline_at"]
    assert proven["failure"]["kind"] == "deadline_expired_before_provider_start"
    assert proven["cleanup"]["outcome"] == "deadline_expired_before_provider_start"
    assert proven["cleanup"]["provider_reaped"] is True
    assert not (owner.store / f"{owner.spool_id}.launch-record").exists()
    evidence = json.loads((owner.store / f"{owner.spool_id}.owner-exit").read_text())
    assert evidence["owner_generation"] == proven["generation"]
    assert evidence["cleanup_outcome"] == "deadline_expired_before_provider_start"
    assert list(iter_control_requests(owner.store, owner.spool_id)) == []

    # Later transport evidence cannot downgrade the episode's already durable
    # pre-start timeout proof.
    evidence["owner_crashed"] = True
    (owner.store / f"{owner.spool_id}.owner-exit").write_text(json.dumps(evidence))

    assert owner.spool()["status"] == "pending"
    terminal = _reconcile_through_production(owner, production_entrypoint)
    assert terminal[EPISODE_KEY]["phase"] == "released"
    assert terminal["status"] == "timeout"
    assert terminal["error_kind"] == "deadline_expired_before_provider_start"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "timeout"


def test_provider_exec_failure_after_binding_publishes_spawn_failure_evidence(watchdog_owner_case):
    owner = watchdog_owner_case("missing-interpreter", expect_ready=False)

    assert owner.process.wait(timeout=8) == 127
    proven = _episode(owner)
    assert proven["phase"] == "cleanup_proven"
    assert proven["failure"]["kind"] == "provider_spawn_failure"
    assert proven["cleanup"]["outcome"] == "provider_spawn_failed"
    assert proven["cleanup"]["provider_reaped"] is True
    assert "provider" not in proven
    assert "provider_custody" not in proven
    evidence = json.loads((owner.store / f"{owner.spool_id}.owner-exit").read_text())
    assert evidence["provider_pid"] is None
    assert evidence["provider_exit_code"] is None
    assert evidence["provider_reaped"] is True

    terminal = _finalize(owner)
    assert terminal["status"] == "error"
    assert terminal["terminal_origin"] == "provider_spawn_failure_after_binding"
    assert terminal["error_kind"] == "provider_spawn_failure"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "failed"


@pytest.mark.parametrize(
    ("mode", "spool_overrides", "expected_status"),
    [
        ("gemini-terminal-linger", {"harness": "gemini"}, "complete"),
        (
            "claude-terminal-linger",
            {"harness": "claude-code", "claude_protocol": spindle.CLAUDE_PROTOCOL_STREAM_V1},
            "complete",
        ),
        (
            "claude-intermediate-linger",
            {"harness": "claude-code", "claude_protocol": spindle.CLAUDE_PROTOCOL_STREAM_V1},
            "timeout",
        ),
        ("codex-terminal-linger", {"harness": "codex"}, "timeout"),
    ],
)
def test_terminal_protocol_output_precedes_timeout_but_partial_output_does_not(
    watchdog_owner_case,
    owner_clock,
    mode,
    spool_overrides,
    expected_status,
):
    _current, advance, clock_fd = owner_clock
    if expected_status == "complete":
        spool_overrides = {
            **spool_overrides,
            "output_complete_detected_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        }
    owner = watchdog_owner_case(
        mode,
        timeout=100,
        controlled_clock_fd=clock_fd.fileno(),
        spool_overrides=spool_overrides,
    )
    output_path = owner.store / f"{owner.spool_id}.stdout"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (not output_path.exists() or output_path.stat().st_size == 0):
        time.sleep(0.01)
    assert output_path.exists() and output_path.stat().st_size > 0
    advance(101)

    owner.process.wait(timeout=8)
    terminal = _finalize(owner)
    assert terminal["status"] == expected_status
    if expected_status == "complete":
        assert terminal["terminal_origin"] == "natural_success"
        assert terminal["lifecycle"]["normalized_terminal_kind"] == "complete"
    else:
        assert terminal["terminal_origin"] == "accepted_timeout"
        assert terminal["lifecycle"]["normalized_terminal_kind"] == "timeout"
    _assert_terminal_projection_is_idempotent(owner, terminal)


def test_terminal_output_written_before_deadline_rejects_later_supervisor_timeout(watchdog_owner_case):
    owner = watchdog_owner_case(
        "gemini-terminal-linger",
        timeout=100,
        spool_overrides={"harness": "gemini"},
    )
    output_path = owner.store / f"{owner.spool_id}.stdout"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (not output_path.exists() or output_path.stat().st_size == 0):
        time.sleep(0.01)
    assert output_path.exists() and output_path.stat().st_size > 0

    episode = _episode(owner)
    request = create_control_request(
        owner.store,
        owner.spool_id,
        "timeout",
        episode["generation"],
        "test-supervisor",
        deadline=episode["deadline"],
    )

    owner.process.wait(timeout=8)
    receipt = read_control_receipt(owner.store, owner.spool_id, request.request_id)
    assert receipt is not None
    assert receipt.owner_acknowledged_at is None
    assert receipt.cleanup_outcome == "rejected_terminal"
    terminal = _finalize(owner)
    assert terminal["status"] == "complete"
    assert terminal["terminal_origin"] == "natural_success"
    assert terminal["output_complete_written_at"] <= episode["deadline"]
    _assert_terminal_projection_is_idempotent(owner, terminal)


def test_terminal_output_written_after_deadline_does_not_suppress_owner_timeout(
    watchdog_owner_case,
    owner_clock,
):
    _current, advance, clock_fd = owner_clock
    wall_deadline = (datetime.now(timezone.utc) + timedelta(seconds=4)).isoformat()
    owner = watchdog_owner_case(
        "gemini-late-terminal-linger",
        timeout=100,
        controlled_clock_fd=clock_fd.fileno(),
        spool_overrides={
            "harness": "gemini",
            "wall_deadline_at": wall_deadline,
        },
    )
    output_path = owner.store / f"{owner.spool_id}.stdout"
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and (not output_path.exists() or output_path.stat().st_size == 0):
        time.sleep(0.01)
    assert output_path.exists() and output_path.stat().st_size > 0
    assert datetime.fromtimestamp(output_path.stat().st_mtime, timezone.utc) > datetime.fromisoformat(wall_deadline)

    advance(101)
    owner.process.wait(timeout=8)
    terminal = _finalize(owner)
    assert terminal["status"] == "timeout"
    assert terminal["terminal_origin"] == "accepted_timeout"
    assert terminal["output_complete_written_at"] > wall_deadline
    _assert_terminal_projection_is_idempotent(owner, terminal)


@pytest.mark.parametrize("production_entrypoint", ["recovery_pass", "supervisor_step"])
def test_watchdog_loss_at_provider_start_boundary_uses_owner_containment_authority(
    watchdog_owner_case,
    production_entrypoint,
):
    owner = watchdog_owner_case(
        "record-launch",
        pause_checkpoint="before_provider_start_checks",
    )
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "before_provider_start_checks"
    assert checkpoint["provider_pid"] is None
    owner_pid = checkpoint["owner_pid"]
    assert _episode(owner)["phase"] == "lock_bound"

    owner_pidfd = os.pidfd_open(owner_pid)
    try:
        owner.process.kill()
        assert owner.process.wait(timeout=8) == -9
        owner.resume()
        for _attempt in range(400):
            if _episode(owner)["phase"] == "cleanup_proven":
                break
            select.select([], [], [], 0.02)
        else:
            pytest.fail("owner did not publish watchdog-loss cleanup proof")

        proven = _episode(owner)
        assert proven["failure"]["kind"] == "watchdog_parent_loss"
        assert proven["cleanup"]["provider_reaped"] is True
        assert not (owner.store / f"{owner.spool_id}.launch-record").exists()
        poller = select.poll()
        poller.register(owner_pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        assert poller.poll(8000), "logical owner did not exit after proving watchdog-loss containment"
    finally:
        os.close(owner_pidfd)

    assert owner.spool()["status"] == "pending"
    terminal = _reconcile_through_production(owner, production_entrypoint)
    assert terminal[EPISODE_KEY]["phase"] == "released"
    assert terminal["status"] == "error"
    assert terminal["error_kind"] == "owner_transport_loss"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert terminal["lifecycle"]["watchdog_parent_lost"] is True


def test_owner_unlocks_at_cleanup_proven_and_reconciliation_alone_persists_release(watchdog_owner_case):
    owner = watchdog_owner_case("immediate-exit")
    owner.process.wait(timeout=8)

    proven = _episode(owner)
    assert proven["phase"] == "cleanup_proven", "the owner published a terminal it is not allowed to own"
    assert "release" not in proven
    assert owner.spool()["status"] == "running"

    terminal = _finalize(owner)
    released = terminal[EPISODE_KEY]
    assert released["phase"] == "released"
    assert released["revision"] == proven["revision"] + 1

    with patch("spindle.SPINDLE_DIR", owner.store):
        assert spindle._check_and_finalize_spool(owner.spool_id) is True
    assert _episode(owner)["revision"] == released["revision"], "release was persisted twice"
    _assert_terminal_projection_is_idempotent(owner, terminal)


def test_owner_crash_contains_a_setsid_descendant_and_records_it_in_the_episode(watchdog_owner_case):
    owner = watchdog_owner_case("setsid-grandchild", pause_checkpoint="provider_ready", disable_pdeathsig=True)
    assert owner.receive_checkpoint()["name"] == "provider_ready"
    descendant_path = owner.store / f"{owner.spool_id}.descendant-pid"
    descendant = int(descendant_path.read_text())

    owner.kill_owner()
    assert not Path(f"/proc/{descendant}").exists()

    contained = _episode(owner)
    assert contained["phase"] == "cleanup_proven"
    assert contained["containment"]["contained"] is True
    assert contained["cleanup"]["provider_reaped"] is True


def test_watchdog_loss_makes_owner_contain_before_a_later_owner_death(
    watchdog_owner_case,
    namespace_owner_env,
):
    spool_id = "watchdog-first-death"
    store = namespace_owner_env["store"]
    descendant_ready = store / f"{spool_id}.descendant-ready"
    os.mkfifo(descendant_ready)
    owner = watchdog_owner_case(
        "setsid-grandchild-causal",
        pause_checkpoint="watchdog_loss_cleanup_proven",
        disable_pdeathsig=True,
        spool_id=spool_id,
    )
    owner_pid = owner.owner_pid
    provider_pid = owner.provider_pid
    with descendant_ready.open() as stream:
        descendant_pid = int(stream.readline())
    owner_pidfd = os.pidfd_open(owner_pid)
    try:
        owner.process.kill()
        owner.process.wait(timeout=8)
        checkpoint = owner.receive_checkpoint()
        assert checkpoint["name"] == "watchdog_loss_cleanup_proven"
        assert not Path(f"/proc/{provider_pid}").exists()
        assert not Path(f"/proc/{descendant_pid}").exists()
        proven = _episode(owner)
        assert proven["phase"] == "cleanup_proven"
        assert proven["failure"]["kind"] == "watchdog_parent_loss"
        assert proven["containment"]["contained"] is True

        os.kill(owner_pid, signal.SIGKILL)
        poller = select.poll()
        poller.register(owner_pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        assert poller.poll(8000), "owner did not exit after the second causal kill"
    finally:
        os.close(owner_pidfd)

    terminal = _finalize(owner)
    assert terminal["owner_episode"]["phase"] == "released"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert terminal["lifecycle"]["watchdog_parent_lost"] is True
    assert _capacity(owner) == 0


def test_direct_compatibility_owner_crash_publishes_generation_matched_legacy_evidence(tmp_path):
    spool_id = "legacy-direct-crash"
    ready_fifo = tmp_path / "provider-ready"
    os.mkfifo(ready_fifo)
    provider = [
        sys.executable,
        "-c",
        "import signal,sys; open(sys.argv[1], 'w').write('ready\\n'); signal.pause()",
        str(ready_fifo),
    ]
    with patch("spindle.SPINDLE_DIR", tmp_path):
        spindle._write_spool(
            spool_id,
            {"id": spool_id, "status": "pending", "created_at": datetime.now().isoformat()},
        )
        spindle._spawn_detached(spool_id, provider, str(tmp_path))
        spindle._finish_spawn_barrier(spool_id, start=True)
        watchdog = spindle._PROC_HANDLES[spool_id]
        try:
            with ready_fifo.open() as stream:
                assert stream.readline() == "ready\n"
            running = spindle._read_spool(spool_id)
            assert "owner_episode" not in running
            os.kill(running["owner_pid"], signal.SIGKILL)
            assert watchdog.wait(timeout=8) == 137

            evidence = json.loads(spindle._get_owner_exit_path(spool_id).read_text())
            assert evidence["owner_generation"] == running["owner_generation"]
            assert evidence["provider_reaped"] is True
            assert evidence["cleanup_outcome"] == "watchdog_contained"
            assert spindle._reconcile_spool_ownership(spindle._read_spool(spool_id)).state == "terminalizable"
            assert spindle._check_and_finalize_spool(spool_id) is True
            assert spindle._read_spool(spool_id)["status"] != "running"
            assert spindle._count_running() == 0
        finally:
            if watchdog.poll() is None:
                watchdog.kill()
                watchdog.wait(timeout=8)


# --- stale generation from a real watchdog ----------------------------------


def test_late_prior_generation_watchdog_write_cannot_touch_the_next_reservation(watchdog_owner_case):
    owner = watchdog_owner_case(
        "healthy-turn",
        pause_checkpoint="identity_lock_acquired",
        disable_pdeathsig=True,
        generation=1,
    )
    assert owner.receive_checkpoint()["owner_generation"] == 1
    record = owner.spool()
    replacement = make_episode("reserved", generation=2, path="before_watchdog")
    record[EPISODE_KEY] = replacement
    (owner.store / f"{owner.spool_id}.json").write_text(json.dumps(record))

    owner.kill_owner()

    stored = _episode(owner)
    assert (stored["generation"], stored["phase"], stored["revision"]) == (2, "reserved", 1)
    assert owner.spool()["status"] != "error", "a prior generation settled the current reservation"


# --- synchronous spawn failures --------------------------------------------


def test_synchronous_spawn_failure_aborts_the_reserved_episode(episode_store, tmp_path):
    spool_id = "sync-spawn-failure"
    reserved = make_episode("reserved", generation=1, path="before_watchdog")
    assert reserved["revision"] == 1
    assert "watchdog" not in reserved
    episode_store.write(spool_id, status="pending", episode=reserved)

    error = spindle._start_spool_process({"id": spool_id}, ["/nonexistent/spindle-provider"], str(tmp_path), None)

    assert error is not None and "spawn" in error
    stored = episode_store.episode(spool_id)
    assert stored is not None, "the failed spawn dropped the episode"
    assert (stored["phase"], stored["generation"], stored["revision"]) == ("aborted", 1, 2)
    assert "watchdog" not in stored
    assert stored["failure"]
    assert spindle._count_running() == 0


# --- mirrors and admission against a live owner -----------------------------


def test_live_classification_survives_missing_and_stale_mirrors(watchdog_owner_case):
    owner = watchdog_owner_case("healthy-turn")
    assert _episode(owner)["phase"] == "accepted"
    (owner.store / f"{owner.spool_id}.owner-identity").unlink()
    (owner.store / f"{owner.spool_id}.owner-exit").write_text(
        json.dumps({"owner_generation": 0, "provider_reaped": True, "cleanup_outcome": "stopped"})
    )
    before = (owner.store / f"{owner.spool_id}.json").read_bytes()

    with patch("spindle.SPINDLE_DIR", owner.store):
        result = spindle._reconcile_spool_ownership(owner.spool())
        assert result.state == "active", result.reason
        assert spindle._count_running() == 1
        assert spindle._check_and_finalize_spool(owner.spool_id) is False

    assert (owner.store / f"{owner.spool_id}.json").read_bytes() == before


def test_admitted_request_is_recorded_in_the_episode_and_settled(watchdog_owner_case):
    owner = watchdog_owner_case("cooperative")

    with patch("spindle.SPINDLE_DIR", owner.store):
        message = spindle._spin_drop_sync(owner.spool_id)
    assert "Cancellation requested" in message, message

    owner.process.wait(timeout=8)
    requests = list(iter_control_requests(owner.store, owner.spool_id))
    assert len(requests) == 1
    receipt = read_control_receipt(owner.store, owner.spool_id, requests[0].request_id)
    assert receipt is not None and receipt.owner_acknowledged_at

    proven = _episode(owner)
    assert proven["phase"] == "cleanup_proven"
    assert proven["winning_request"]["request_id"] == requests[0].request_id
    assert proven["acknowledgement"]["acknowledged_at"]

    terminal = _finalize(owner)
    assert terminal[EPISODE_KEY]["phase"] == "released"
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"


FINAL_SWEEP_CALLERS = ("drop", "timeout", "shard_abandon")
EXPIRED_TIMEOUT_DEADLINE = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
FUTURE_LEGACY_TIMEOUT_DEADLINE = datetime(2100, 1, 1, tzinfo=timezone.utc).isoformat()


def _start_final_sweep_case(watchdog_owner_case, caller, tmp_path, checkpoint):
    spool_id = f"final-sweep-{caller}-{checkpoint}"
    overrides = {"prompt": "work"}
    if caller == "timeout":
        overrides["wall_deadline_at"] = FUTURE_LEGACY_TIMEOUT_DEADLINE
    elif caller == "shard_abandon":
        worktree = tmp_path / spool_id / "worktree"
        worktree.mkdir(parents=True)
        overrides["working_dir"] = str(worktree)
        overrides["shard"] = {"worktree_path": str(worktree), "branch_name": "episode-test"}
    owner = watchdog_owner_case(
        "silent-exit",
        pause_checkpoint=checkpoint,
        spool_id=spool_id,
        timeout=100 if caller == "timeout" else None,
        spool_overrides=overrides,
    )
    return owner


def _expire_timeout_at_checkpoint(owner):
    with patch("spindle.SPINDLE_DIR", owner.store):
        with spindle._spool_lock(owner.spool_id) as acquired:
            assert acquired
            spool = spindle._read_spool(owner.spool_id)
            assert spool["timeout"] == 100
            assert spool["wall_deadline_at"] == FUTURE_LEGACY_TIMEOUT_DEADLINE
            spool[EPISODE_KEY]["deadline"] = EXPIRED_TIMEOUT_DEADLINE
            spindle._write_spool(owner.spool_id, spool)
    stored = owner.spool()
    assert stored["timeout"] == 100
    assert stored["wall_deadline_at"] == FUTURE_LEGACY_TIMEOUT_DEADLINE
    assert stored[EPISODE_KEY]["deadline"] == EXPIRED_TIMEOUT_DEADLINE
    assert stored["wall_deadline_at"] != stored[EPISODE_KEY]["deadline"]


def _invoke_final_sweep_caller(caller, owner, tmp_path):
    with patch("spindle.SPINDLE_DIR", owner.store):
        if caller == "drop":
            return spindle._spin_drop_sync(owner.spool_id)
        if caller == "shard_abandon":
            return spindle._shard_abandon_sync(owner.spool_id, False, str(tmp_path))
        return spindle._reconcile_spool_step(owner.spool_id)


@pytest.mark.parametrize("caller", FINAL_SWEEP_CALLERS)
def test_request_wins_before_final_sweep_and_is_durably_settled(
    watchdog_owner_case,
    caller,
    tmp_path,
):
    owner = _start_final_sweep_case(
        watchdog_owner_case,
        caller,
        tmp_path,
        "natural_exit_evidence_published",
    )
    try:
        assert owner.receive_checkpoint()["name"] == "natural_exit_evidence_published"
        if caller == "timeout":
            _expire_timeout_at_checkpoint(owner)

        _invoke_final_sweep_caller(caller, owner, tmp_path)
        requests = list(iter_control_requests(owner.store, owner.spool_id))
    finally:
        if owner.process.poll() is None:
            owner.resume()
            owner.process.wait(timeout=8)
    assert len(requests) == 1, f"{caller} did not win request admission before the final sweep"
    if caller == "timeout":
        assert requests[0].deadline == EXPIRED_TIMEOUT_DEADLINE
    receipt = read_control_receipt(owner.store, owner.spool_id, requests[0].request_id)
    assert receipt is not None
    assert receipt.owner_acknowledged_at is None
    assert receipt.cleanup_outcome == "rejected_terminal"


@pytest.mark.parametrize("caller", FINAL_SWEEP_CALLERS)
def test_final_sweep_guard_wins_and_later_caller_creates_no_request(
    watchdog_owner_case,
    monkeypatch,
    caller,
    tmp_path,
):
    owner = _start_final_sweep_case(
        watchdog_owner_case,
        caller,
        tmp_path,
        "final_mailbox_guard_acquired",
    )
    waiter = None
    owner_resumed = False
    try:
        assert owner.receive_checkpoint(timeout=2)["name"] == "final_mailbox_guard_acquired"
        if caller == "timeout":
            _expire_timeout_at_checkpoint(owner)
        assert list(iter_control_requests(owner.store, owner.spool_id)) == []

        process_context = multiprocessing.get_context("fork")
        attempted = process_context.Event()
        real_guard = spindle.mailbox_guard

        @contextmanager
        def observed_guard(*args, **kwargs):
            attempted.set()
            with real_guard(*args, **kwargs):
                yield

        monkeypatch.setattr(spindle, "mailbox_guard", observed_guard)
        waiter = process_context.Process(target=_invoke_final_sweep_caller, args=(caller, owner, tmp_path))
        waiter.start()
        assert attempted.wait(timeout=2), f"{caller} did not enter mailbox-first admission"
        assert waiter.is_alive(), f"{caller} bypassed the final-sweep mailbox guard"
        owner.resume()
        owner_resumed = True
        waiter.join(timeout=8)
        if waiter.is_alive():
            pytest.fail(f"{caller} remained blocked after the owner released the final-sweep guard")
        assert waiter.exitcode == 0, f"{caller} waiter exited with {waiter.exitcode}"
    finally:
        if not owner_resumed:
            if owner.process.poll() is None:
                owner.resume()
        if waiter is not None:
            if waiter.is_alive():
                waiter.terminate()
                waiter.join(timeout=2)
            if waiter.is_alive():
                waiter.kill()
                waiter.join(timeout=2)
            waiter.close()
        if owner.process.poll() is None:
            owner.process.wait(timeout=8)

    assert list(iter_control_requests(owner.store, owner.spool_id)) == []


# --- real cross-namespace observation ---------------------------------------


@pytest.mark.ns_required
def test_foreign_namespace_observer_classifies_the_episode_without_mutating_it(
    watchdog_owner_case,
    foreign_pid_namespace,
):
    owner = watchdog_owner_case("healthy-turn")
    before = (owner.store / f"{owner.spool_id}.json").read_bytes()
    source = (
        OBSERVER_PROLOGUE
        + r"""
from spindle.namespace_owner import (
    NamespaceIdentity,
    ProcessIdentity,
    assess_process_liveness,
    classify_owner_episode,
    probe_ownership_lock,
)
record_path = store / f"{sys.argv[2]}.json"
spool = json.loads(record_path.read_text())
episode = spool["owner_episode"]
identity = ProcessIdentity(
    pid=episode["owner"]["pid"],
    birth_token=episode["owner"]["birth_token"],
    namespace=NamespaceIdentity.from_dict(episode["owner"]["namespace"]),
    owner_generation=episode["generation"],
    child_pgid=None,
    lock_device=episode["lock"]["device"],
    lock_inode=episode["lock"]["inode"],
    lock_created=True,
)
before = record_path.read_bytes()
lock = probe_ownership_lock(store / f"{sys.argv[2]}.process-owner", identity)
liveness = assess_process_liveness(identity)
classification = classify_owner_episode(spool, lock, liveness)
current = os.stat("/proc/self/ns/pid")
print(json.dumps({
    "state": classification.state,
    "reason": classification.reason,
    "lock": lock.state,
    "liveness": liveness.state,
    "namespace": [current.st_dev, current.st_ino],
    "owner_namespace": [episode["owner"]["namespace"]["device"], episode["owner"]["namespace"]["inode"]],
    "owner_proc_visible": Path(f"/proc/{identity.pid}").exists(),
    "unchanged": before == record_path.read_bytes(),
}))
"""
    )

    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)

    assert _episode(owner)["phase"] == "accepted"
    assert observed["namespace"] != observed["owner_namespace"]
    assert observed["owner_proc_visible"] is False
    assert observed["lock"] == "held"
    assert observed["liveness"] == "unverifiable"
    assert observed["state"] == "active"
    assert observed["unchanged"] is True
    assert (owner.store / f"{owner.spool_id}.json").read_bytes() == before
