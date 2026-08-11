"""S2-EP: real-process generation matrix for the authoritative owner episode.

Every crash boundary is driven by an inherited checkpoint socket and settled by
waiting on a real process exit, so no case depends on a sleep.  The episode in
the spool record is the only thing these tests read for lifecycle truth.
"""

from __future__ import annotations

import json
import multiprocessing
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import spindle
from spindle.namespace_owner import (
    LivenessEvidence,
    LockEvidence,
    iter_control_requests,
    read_control_receipt,
)
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


def test_synchronous_replacement_spawn_failure_aborts_the_replacement_generation(
    episode_store, monkeypatch, tmp_path
):
    spool_id = "sync-replacement-failure"
    source_id = "sync-replacement-source"
    proven = make_episode("released", generation=3)
    episode_store.write(
        spool_id,
        status="running",
        episode=proven,
        session_id="session-y",
        prompt="spin: continue",
        working_dir=str(tmp_path),
    )
    episode_store.write(source_id, status="complete", session_id="session-y")
    transcript = spindle._get_transcript_path(source_id)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("prior transcript")

    def refuse(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(spindle, "_spawn_detached", refuse)
    monkeypatch.setattr(
        spindle,
        "_reconcile_spool_ownership",
        lambda spool: spindle.ReconciliationResult(
            "terminalizable",
            "episode_released",
            LivenessEvidence("dead", "pidfd_exited"),
            LockEvidence("released", 64, 987),
        ),
    )

    assert spindle._handle_expired_session_locked(spool_id, episode_store.read(spool_id)) is False

    stored = episode_store.episode(spool_id)
    assert stored is not None, "the failed replacement dropped the episode"
    assert (stored["phase"], stored["generation"]) == ("aborted", proven["generation"] + 1)
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


FINAL_SWEEP_CALLERS = ("drop", "timeout", "expired_session", "shard_abandon")
EXPIRED_TIMEOUT_DEADLINE = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()


def _start_final_sweep_case(watchdog_owner_case, owner_clock, caller, tmp_path, checkpoint):
    spool_id = f"final-sweep-{caller}-{checkpoint}"
    overrides = {"prompt": "work"}
    if caller == "expired_session":
        overrides["session_id"] = "expired-session"
    elif caller == "shard_abandon":
        worktree = tmp_path / spool_id / "worktree"
        worktree.mkdir(parents=True)
        overrides["working_dir"] = str(worktree)
        overrides["shard"] = {"worktree_path": str(worktree), "branch_name": "episode-test"}
    owner = watchdog_owner_case(
        "silent-exit",
        pause_checkpoint=checkpoint,
        spool_id=spool_id,
        spool_overrides=overrides,
        controlled_clock_fd=owner_clock[2].fileno() if caller == "timeout" else None,
    )
    if caller == "expired_session":
        (owner.store / f"{spool_id}.stderr").write_text(
            "No conversation found with session ID: expired-session"
        )
    return owner


def _expire_timeout_at_checkpoint(owner, owner_clock):
    spool = owner.spool()
    spool["timeout"] = 100
    spool["wall_deadline_at"] = EXPIRED_TIMEOUT_DEADLINE
    spool[EPISODE_KEY]["deadline"] = EXPIRED_TIMEOUT_DEADLINE
    with patch("spindle.SPINDLE_DIR", owner.store):
        spindle._write_spool(owner.spool_id, spool)
    owner_clock[1](101)
    stored = owner.spool()
    assert stored["timeout"] == 100
    assert stored["wall_deadline_at"] == EXPIRED_TIMEOUT_DEADLINE
    assert stored[EPISODE_KEY]["deadline"] == EXPIRED_TIMEOUT_DEADLINE


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
    owner_clock,
    caller,
    tmp_path,
):
    owner = _start_final_sweep_case(
        watchdog_owner_case,
        owner_clock,
        caller,
        tmp_path,
        "natural_exit_evidence_published",
    )
    assert owner.receive_checkpoint()["name"] == "natural_exit_evidence_published"
    if caller == "timeout":
        _expire_timeout_at_checkpoint(owner, owner_clock)

    _invoke_final_sweep_caller(caller, owner, tmp_path)
    requests = list(iter_control_requests(owner.store, owner.spool_id))
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
    owner_clock,
    monkeypatch,
    caller,
    tmp_path,
):
    owner = _start_final_sweep_case(
        watchdog_owner_case,
        owner_clock,
        caller,
        tmp_path,
        "final_mailbox_guard_acquired",
    )
    assert owner.receive_checkpoint(timeout=2)["name"] == "final_mailbox_guard_acquired"
    if caller == "timeout":
        _expire_timeout_at_checkpoint(owner, owner_clock)
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
    owner_resumed = False
    try:
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
            owner.resume()
        if waiter.is_alive():
            waiter.terminate()
            waiter.join(timeout=2)
        if waiter.is_alive():
            waiter.kill()
            waiter.join(timeout=2)
        waiter.close()

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
    source = OBSERVER_PROLOGUE + r"""
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

    observed = foreign_pid_namespace(owner.store, source, owner.spool_id)

    assert _episode(owner)["phase"] == "accepted"
    assert observed["namespace"] != observed["owner_namespace"]
    assert observed["owner_proc_visible"] is False
    assert observed["lock"] == "held"
    assert observed["liveness"] == "unverifiable"
    assert observed["state"] == "active"
    assert observed["unchanged"] is True
    assert (owner.store / f"{owner.spool_id}.json").read_bytes() == before
