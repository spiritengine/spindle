"""S2-U: contract tests for namespace-safe shared owner primitives."""

from __future__ import annotations

import asyncio
import errno
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import spindle
from spindle.namespace_owner import (
    OWNER_ARTIFACT_SUFFIXES,
    LegacyAuthority,
    LivenessEvidence,
    LockEvidence,
    NamespaceIdentity,
    ProcessIdentity,
    acquire_ownership_lock,
    active_spool_count,
    assess_process_liveness,
    capture_pid_namespace,
    create_control_request,
    iter_control_requests,
    parse_proc_stat_starttime,
    probe_ownership_lock,
    reconcile_owner_episode,
    write_control_receipt,
)


def test_s2_u_ns_01_namespace_identity_uses_device_and_inode(monkeypatch):
    current = capture_pid_namespace()
    if current.is_supported:
        assert current.same_as(NamespaceIdentity.supported(current.device, current.inode)) is True
        assert current.same_as(NamespaceIdentity.supported(current.device + 1, current.inode)) is False
        assert current.same_as(NamespaceIdentity.supported(current.device, current.inode + 1)) is False
    monkeypatch.setattr(os, "stat", lambda _path: (_ for _ in ()).throw(OSError(errno.ENOSYS, "no proc")))
    unsupported = capture_pid_namespace()
    assert unsupported.is_supported is False
    assert unsupported.reason == "unsupported"


def test_s2_u_ns_02_proc_starttime_parses_comm_from_the_right(proc_stat_record):
    assert parse_proc_stat_starttime(proc_stat_record(starttime="987654")) == "987654"


def _identity(namespace):
    return ProcessIdentity(
        pid=123,
        birth_token="101",
        namespace=namespace,
        owner_generation=2,
        child_pgid=123,
        lock_device=7,
        lock_inode=8,
        lock_created=True,
    )


def test_s2_u_live_01_same_namespace_pidfd_alive(fake_process_ops):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    result = assess_process_liveness(_identity(namespace), ops=fake_process_ops)
    assert result == LivenessEvidence("alive", "pidfd_live")
    assert not [call for call in fake_process_ops.calls if call[0].startswith("signal")]


@pytest.mark.parametrize(
    "pidfd_result,readable,reason",
    [(91, True, "pidfd_exited"), (ProcessLookupError(errno.ESRCH, "gone"), False, "pidfd_esrch")],
)
def test_s2_u_live_02_same_namespace_pidfd_dead(fake_process_ops, pidfd_result, readable, reason):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    fake_process_ops.pidfd_result = pidfd_result
    fake_process_ops.pidfd_readable = readable
    assert assess_process_liveness(_identity(namespace), ops=fake_process_ops) == LivenessEvidence("dead", reason)


def test_pidfd_readable_wins_when_proc_disappears_after_open(fake_process_ops):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    fake_process_ops.pidfd_readable = True
    fake_process_ops.starttime = FileNotFoundError(errno.ENOENT, "proc entry disappeared")

    result = assess_process_liveness(_identity(namespace), ops=fake_process_ops)

    assert result == LivenessEvidence("dead", "pidfd_exited")
    assert fake_process_ops.calls == [
        ("current_namespace",),
        ("pidfd_open", 123),
        ("read_starttime", 123),
        ("pidfd_is_readable", 91),
        ("close", 91),
    ]


def test_s2_u_live_03_different_namespace_is_unverifiable_before_pid_access(fake_process_ops):
    fake_process_ops.namespace = NamespaceIdentity.supported(5, 10)
    record = _identity(NamespaceIdentity.supported(5, 11))
    result = assess_process_liveness(record, ops=fake_process_ops)
    assert result == LivenessEvidence("unverifiable", "namespace_mismatch")
    assert fake_process_ops.calls == [("current_namespace",)]


@pytest.mark.parametrize("error", [FileNotFoundError(errno.ENOENT, "hidden"), PermissionError(errno.EACCES, "hidden")])
def test_s2_u_live_04_hidden_or_missing_proc_is_unverifiable(fake_process_ops, error):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    fake_process_ops.starttime = error
    result = assess_process_liveness(_identity(namespace), ops=fake_process_ops)
    assert result == LivenessEvidence("unverifiable", "proc_unavailable")


def test_s2_u_live_05_pid_reuse_and_namespace_mismatch_are_distinct(fake_process_ops):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    fake_process_ops.starttime = "202"
    assert assess_process_liveness(_identity(namespace), ops=fake_process_ops).reason == "identity_mismatch"
    fake_process_ops.calls.clear()
    foreign = _identity(NamespaceIdentity.supported(5, 99))
    assert assess_process_liveness(foreign, ops=fake_process_ops).reason == "namespace_mismatch"


def test_s2_u_lock_01_exact_held_inode_reports_held(tmp_path, lock_holder, process_identity_record):
    path = tmp_path / "one.process-owner"
    holder = lock_holder(path)
    record = process_identity_record(lock_device=holder.device, lock_inode=holder.inode)
    assert probe_ownership_lock(path, record) == LockEvidence("held", holder.device, holder.inode)
    assert path.exists()


def test_s2_u_lock_02_created_released_inode_reports_released(tmp_path, process_identity_record):
    path = tmp_path / "two.process-owner"
    path.touch()
    info = path.stat()
    record = process_identity_record(lock_device=info.st_dev, lock_inode=info.st_ino)
    assert probe_ownership_lock(path, record) == LockEvidence("released", info.st_dev, info.st_ino)
    assert path.exists()


def test_s2_u_lock_03_missing_legacy_lock_is_absent_legacy(tmp_path, process_identity_record):
    record = process_identity_record("legacy_missing_lock")
    assert probe_ownership_lock(tmp_path / "missing", record).state == "absent_legacy"


def test_s2_u_lock_04_missing_current_and_replaced_inode_are_identity_mismatch(
    tmp_path, lock_holder, process_identity_record
):
    missing = probe_ownership_lock(tmp_path / "missing", process_identity_record("current_missing_lock"))
    assert missing.state == "identity_mismatch"
    path = tmp_path / "replaced.process-owner"
    holder = lock_holder(path)
    record = process_identity_record(lock_device=holder.device, lock_inode=holder.inode)
    holder.replace_path()
    evidence = probe_ownership_lock(path, record)
    assert evidence.state == "identity_mismatch"
    assert evidence.observed_inode != holder.inode


def test_s2_u_lock_05_open_errors_are_unreadable(tmp_path, monkeypatch, process_identity_record):
    path = tmp_path / "unreadable.process-owner"
    path.touch()
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no")))
    assert probe_ownership_lock(path, process_identity_record()).state == "unreadable"


def test_s2_u_lock_06_acquisition_retries_after_fstat_stat_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "owner.process-owner"
    real_stat = os.stat
    calls = {"count": 0}

    def transient_stat(target, **kwargs):
        result = real_stat(target, **kwargs)
        if Path(target) == path and calls["count"] == 0:
            calls["count"] += 1
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", transient_stat)
    acquired = acquire_ownership_lock(path, max_attempts=2)
    try:
        assert acquired.attempts == 2
        assert acquired.inode == os.fstat(acquired.fd).st_ino
    finally:
        acquired.close()


def test_s2_u_lock_07_lock_identity_includes_device(tmp_path, process_identity_record):
    path = tmp_path / "owner.process-owner"
    path.touch()
    info = path.stat()
    record = process_identity_record(lock_device=info.st_dev + 1, lock_inode=info.st_ino)
    assert probe_ownership_lock(path, record).state == "identity_mismatch"


def test_probe_reverifies_recorded_inode_after_observer_acquires_flock(tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "racing.process-owner"
    path.touch(mode=0o600)
    held_fd = os.open(path, os.O_RDWR)
    real_flock = fcntl.flock
    real_flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    recorded = path.stat()
    identity = ProcessIdentity(
        pid=os.getpid(),
        birth_token="test",
        namespace=capture_pid_namespace(),
        owner_generation=1,
        child_pgid=None,
        lock_device=recorded.st_dev,
        lock_inode=recorded.st_ino,
        lock_created=True,
    )
    replaced = {"value": False}

    def replace_between_observation_and_flock(fd, operation):
        if fd != held_fd and operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not replaced["value"]:
            real_flock(held_fd, fcntl.LOCK_UN)
            path.unlink()
            path.touch(mode=0o600)
            replaced["value"] = True
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", replace_between_observation_and_flock)
    try:
        evidence = probe_ownership_lock(path, identity)
    finally:
        os.close(held_fd)

    assert replaced["value"] is True
    assert evidence.state == "identity_mismatch"
    assert evidence.detail == "inode_mismatch_after_flock"
    reconciliation = reconcile_owner_episode(
        evidence,
        LivenessEvidence("dead", "pidfd_exited"),
        exit_evidence=True,
    )
    assert reconciliation.state == "store_unhealthy"


@pytest.mark.parametrize("kind,terminal", [("cancel", "cancelled"), ("timeout", "timeout"), ("drop", "cancelled")])
def test_s2_u_ctl_01_control_request_records_complete_provenance(tmp_path, kind, terminal):
    namespace = NamespaceIdentity.supported(13, 17)
    request = create_control_request(
        tmp_path,
        "spool-a",
        kind,
        owner_generation=8,
        requested_by="unit-test",
        observer_pid=4242,
        observer_namespace=namespace,
        reason="because",
    )
    loaded = list(iter_control_requests(tmp_path, "spool-a"))[0]
    assert loaded == request
    assert loaded.desired_terminal_kind == terminal
    assert loaded.owner_generation == 8
    assert loaded.observer_namespace == namespace
    assert loaded.requested_at


def test_s2_u_ctl_02_requests_are_idempotent_and_generation_scoped(tmp_path):
    request_id = "fixed-request"
    first = create_control_request(tmp_path, "spool-a", "cancel", 3, "test", request_id=request_id)
    second = create_control_request(tmp_path, "spool-a", "cancel", 3, "test", request_id=request_id)
    distinct = create_control_request(tmp_path, "spool-a", "cancel", 3, "test")
    assert first == second
    assert len(list(iter_control_requests(tmp_path, "spool-a"))) == 2
    stale = write_control_receipt(tmp_path, "spool-a", first, current_generation=4)
    current = write_control_receipt(tmp_path, "spool-a", distinct, current_generation=3)
    assert stale.cleanup_outcome == "rejected_stale_generation"
    assert stale.owner_acknowledged_at is None
    assert current.owner_acknowledged_at is not None
    assert stale.request_id == first.request_id


def test_duplicate_request_writers_are_atomic_and_conflicts_fail(tmp_path):
    request_id = "concurrent-request"
    barrier = threading.Barrier(2)

    def publish(kind):
        barrier.wait(timeout=2)
        return create_control_request(tmp_path, "spool-a", kind, 3, "writer", request_id=request_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, kind) for kind in ("cancel", "timeout")]
        outcomes = []
        errors = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=3))
            except ValueError as exc:
                errors.append(str(exc))

    assert len(outcomes) == len(errors) == 1
    assert "different payload" in errors[0]
    stored = list(iter_control_requests(tmp_path, "spool-a"))
    assert stored == outcomes


def test_every_owner_exit_code_is_disjoint_from_watchdog_crash_channel():
    from spindle.owner_watchdog import _owner_process_crashed

    for exit_code in range(256):
        assert _owner_process_crashed(exit_code << 8, exception_reported=False) is False
        assert _owner_process_crashed(exit_code << 8, exception_reported=True) is True


def test_natural_exit_retries_descendant_cleanup_before_evidence_or_release(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    class TrackingLock:
        fd = -1

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(timeout=None, poll_interval=0)
    owner.store = tmp_path
    owner.spool_id = "descendant-retry"
    owner.provider = SimpleNamespace(pid=456)
    owner.provider_pidfd = None
    owner.control = None
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None)
    owner.lock = TrackingLock()
    owner._await_launch_barrier = lambda: True
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda: None
    owner._verify_lock = lambda: True
    owner._next_current_request = lambda: None
    owner._provider_exited = lambda: True
    owner._finish_provider = lambda: 0
    owner._set_lifecycle = lambda **_values: None
    owner._settle_other_requests_unlocked = lambda *_args, **_kwargs: True
    cleanup_calls = []

    def settle_descendants(*, force):
        cleanup_calls.append(force)
        assert owner.lock.closed is False
        return len(cleanup_calls) > 1

    evidence_calls = []

    def write_exit_evidence(returncode, *, cleanup_outcome):
        assert cleanup_calls == [False, False]
        assert owner.lock.closed is False
        evidence_calls.append((returncode, cleanup_outcome))
        return True

    owner._settle_descendants = settle_descendants
    owner._write_exit_evidence = write_exit_evidence
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)
    owner.lock_path = tmp_path / "descendant-retry.process-owner"

    assert owner.run() == 0
    assert evidence_calls == [(0, "natural_exit")]
    assert owner.lock.closed is True


def test_watchdog_retries_after_first_descendant_confirmation_deadline(monkeypatch):
    import spindle.owner_watchdog as watchdog

    outcomes = iter((False, True))
    calls = []

    def containment_pass():
        calls.append("pass")
        return next(outcomes)

    monkeypatch.setattr(watchdog, "_contain_adopted_descendants", containment_pass)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _seconds: None)

    watchdog._contain_adopted_descendants_until_clean()

    assert calls == ["pass", "pass"]


@pytest.mark.parametrize(
    "lock,liveness,exit_evidence,legacy,result",
    [
        (LockEvidence("held", 1, 2), LivenessEvidence("unverifiable", "namespace_mismatch"), True, None, "active"),
        (LockEvidence("held", 1, 2), LivenessEvidence("alive", "pidfd_live"), False, None, "active"),
        (LockEvidence("released", 1, 2), LivenessEvidence("dead", "pidfd_exited"), True, None, "terminalizable"),
        (LockEvidence("released", 1, 2), LivenessEvidence("dead", "pidfd_exited"), False, None, "unverifiable"),
        (LockEvidence("unreadable"), LivenessEvidence("alive", "pidfd_live"), False, None, "store_unhealthy"),
        (LockEvidence("identity_mismatch"), LivenessEvidence("dead", "pidfd_exited"), True, None, "store_unhealthy"),
        (LockEvidence("absent_legacy"), LivenessEvidence("unverifiable", "namespace_mismatch"), False, None, "unverifiable"),
        (
            LockEvidence("absent_legacy"),
            LivenessEvidence("dead", "pidfd_exited"),
            True,
            LegacyAuthority(recorded="owner-a", observer="owner-a"),
            "terminalizable",
        ),
        (
            LockEvidence("absent_legacy"),
            LivenessEvidence("dead", "pidfd_exited"),
            True,
            LegacyAuthority(recorded="owner-a", observer="other"),
            "unverifiable",
        ),
    ],
)
def test_s2_u_rec_01_reconciliation_precedence_matrix(lock, liveness, exit_evidence, legacy, result):
    reconciled = reconcile_owner_episode(lock, liveness, exit_evidence=exit_evidence, legacy_authority=legacy)
    assert reconciled.state == result
    assert reconciled.reason


@pytest.mark.parametrize(
    "caller",
    [
        "finalize",
        "reconcile_step",
        "claude_unspool",
        "codex_unspool",
        "gemini_unspool",
        "kimi_unspool",
        "spool_grep",
        "spin_wait",
        "pending_recovery",
        "expired_replacement",
        "retention",
        "timeout",
        "drop",
        "shard_merge",
        "shard_abandon",
    ],
)
def test_s2_u_rec_02_every_pid_sensitive_caller_uses_reconciliation(
    tmp_path,
    reconciliation_spy,
    caller,
):
    spool_id = f"rec-{caller}"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    old = (datetime.now() - timedelta(days=2)).isoformat()
    spool = {
        "id": spool_id,
        "status": "running",
        "harness": "claude-code",
        "created_at": old,
        "prompt": "still owned",
        "result": "needle",
        "owner_generation": 3,
        "working_dir": str(worktree),
        "shard": {"worktree_path": str(worktree), "branch_name": "test-branch"},
    }
    if caller == "pending_recovery":
        spool.update(
            status="pending",
            launcher_pid=424242,
            launcher_start_time="101",
            launcher_namespace=NamespaceIdentity.supported(7, 11).to_dict(),
        )
    if caller == "timeout":
        spool["timeout"] = 1
    if caller == "expired_replacement":
        spool["session_id"] = "session-expired"
        source = {
            "id": "rec-expired-source",
            "status": "complete",
            "session_id": "session-expired",
            "created_at": old,
        }
        spindle._write_spool(source["id"], source)
        transcript = spindle._get_transcript_path(source["id"])
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior transcript")

    spindle._write_spool(spool_id, spool)
    record_path = spindle._get_spool_path(spool_id)
    before = record_path.read_bytes()
    reconciliation_spy.forbid_mutation()

    if caller == "finalize":
        assert spindle._check_and_finalize_spool(spool_id) is False
    elif caller in {"reconcile_step", "timeout"}:
        assert spindle._reconcile_spool_step(spool_id) is True
    elif caller == "claude_unspool":
        assert "still running" in spindle._unspool_sync(spool_id)
    elif caller == "codex_unspool":
        assert "still running" in spindle._codex_unspool_sync(spool_id)
    elif caller == "gemini_unspool":
        assert "still running" in spindle._gemini_unspool_sync(spool_id)
    elif caller == "kimi_unspool":
        assert "still running" in spindle._kimi_unspool_sync(spool_id)
    elif caller == "spool_grep":
        grep = spindle.spool_grep.fn if hasattr(spindle.spool_grep, "fn") else spindle.spool_grep
        assert "needle" in asyncio.run(grep("needle", spool_id=spool_id))
    elif caller == "spin_wait":
        assert "Still pending" in spindle._spin_wait_sync(spool_id, timeout=-1)
    elif caller == "pending_recovery":
        assert spindle._reconcile_pending_spool(spool_id) is True
    elif caller == "expired_replacement":
        assert spindle._handle_expired_session(spool_id, spool) is False
    elif caller == "retention":
        spindle._cleanup_old_spools()
    elif caller == "drop":
        assert "ownership unverifiable" in spindle._spin_drop_locked(spool_id)
    elif caller == "shard_merge":
        assert "still starting or running" in spindle._shard_merge_locked(spool_id, False, str(tmp_path))
    elif caller == "shard_abandon":
        assert "ownership unverifiable" in spindle._shard_abandon_locked(spool_id, False, str(tmp_path))
    else:  # pragma: no cover - the parameter list is exhaustive
        raise AssertionError(caller)

    assert reconciliation_spy.calls, f"{caller} bypassed unified reconciliation"
    assert spool_id in reconciliation_spy.calls
    assert reconciliation_spy.violations == []
    assert record_path.read_bytes() == before


def test_owner_exit_evidence_is_scoped_to_current_generation(tmp_path):
    spool_id = "generation-evidence"
    spindle._get_owner_exit_path(spool_id).write_text(
        '{"owner_generation":1,"provider_reaped":true,"cleanup_outcome":"stopped"}'
    )
    assert spindle._owner_exit_evidence(spool_id, 1) == (True, True)
    assert spindle._owner_exit_evidence(spool_id, 2) == (False, False)


@pytest.mark.parametrize(
    ("lock_state", "artifact"),
    [("identity_mismatch", "missing-current"), ("identity_mismatch", "replaced"), ("unreadable", "unreadable")],
)
def test_store_health_authority_blocks_launch_doctor_and_recovers(tmp_path, monkeypatch, lock_state, artifact):
    spool_id = f"unhealthy-{artifact}"
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    spindle._write_spool(spool_id, {"id": spool_id, "status": "running"})
    repaired = {"value": False}
    recorded_identity = ProcessIdentity(
        pid=os.getpid(),
        birth_token="test",
        namespace=capture_pid_namespace(),
        owner_generation=1,
        child_pgid=None,
        lock_device=11,
        lock_inode=12,
        lock_created=True,
    )

    def reconcile(_spool):
        state = "active" if repaired["value"] else "store_unhealthy"
        observed = (22, 33) if artifact == "replaced" else (None, None)
        return spindle.ReconciliationResult(
            state,
            "exact_ownership_inode_held" if repaired["value"] else f"ownership_{lock_state}",
            LivenessEvidence("alive", "pidfd_live"),
            LockEvidence("held" if repaired["value"] else lock_state, *observed, detail=artifact),
        )

    monkeypatch.setattr(spindle, "_ensure_store_supervisor_locked", lambda: (True, None))
    monkeypatch.setattr(spindle, "_reconcile_spool_ownership", reconcile)
    monkeypatch.setattr(spindle, "_read_current_owner_identity", lambda _spool_id: recorded_identity)

    success, error = spindle._try_reserve_slot_and_create("rejected")
    assert success is False
    assert f"ownership_{lock_state}" in error
    assert "recorded=11:12" in error
    assert f"observed={'22:33' if artifact == 'replaced' else 'None:None'}" in error
    assert not spindle._get_spool_path("rejected").exists()
    assert spindle._count_running() == 1
    diagnosis = spindle._doctor_storage_check()
    assert diagnosis["status"] == "fail"
    assert diagnosis["data"]["ownership_failures"][0]["spool_id"] == spool_id
    assert "recorded=11:12" in diagnosis["lines"][0]
    assert any("repair" in line for line in diagnosis["lines"])

    repaired["value"] = True
    success, error = spindle._try_reserve_slot_and_create("accepted")
    assert (success, error) == (True, None)
    assert spindle._doctor_storage_check()["status"] == "ok"
    assert spindle._count_running() == 2


@pytest.mark.parametrize(
    ("identity_artifact", "reason"),
    [("missing", "owner_identity_missing"), ("incomplete", "owner_identity_unreadable")],
)
def test_current_owner_identity_defects_block_production_admission_and_doctor(
    tmp_path,
    monkeypatch,
    identity_artifact,
    reason,
):
    spool_id = f"current-{identity_artifact}"
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(spindle, "_ensure_store_supervisor_locked", lambda: (True, None))
    spindle._write_spool(
        spool_id,
        {
            "id": spool_id,
            "status": "running",
            "spool_schema_version": 1,
            "owner_generation": 4,
            "owner_pid": os.getpid(),
            "lifecycle": {"ownership_state": "held", "transport_state": "connected"},
        },
    )
    spindle._write_spool(
        "legacy-schema1",
        {
            "id": "legacy-schema1",
            "status": "running",
            "spool_schema_version": 1,
            "pid": os.getpid(),
            "process_start_time": spindle._process_start_time(os.getpid()),
        },
    )
    if identity_artifact == "incomplete":
        spindle._get_owner_identity_path(spool_id).write_text('{"pid":1,"owner_generation":4}')

    success, error = spindle._try_reserve_slot_and_create("must-not-exist")

    assert success is False
    assert reason in error
    assert not spindle._get_spool_path("must-not-exist").exists()
    assert spindle._reconcile_spool_ownership(spindle._read_spool("legacy-schema1")).reason == (
        "legacy_authority_unproven"
    )
    diagnosis = spindle._doctor_storage_check()
    assert diagnosis["status"] == "fail"
    assert diagnosis["data"]["ownership_failures"] == [
        {
            "spool_id": spool_id,
            "reason": reason,
            "lock_state": "identity_mismatch" if identity_artifact == "missing" else "unreadable",
            "detail": reason,
            "recorded_device": None,
            "recorded_inode": None,
            "observed_device": None,
            "observed_inode": None,
        }
    ]
    assert reason in diagnosis["lines"][0]


def test_s2_u_slot_01_pending_running_and_stopping_count_as_active():
    spools = [
        {"status": "pending"},
        {"status": "running"},
        {"status": "running", "lifecycle": {"public_stop_state": "stopping"}},
        {"status": "complete"},
        {"status": "error", "ownership_state": "unreadable"},
    ]
    assert active_spool_count(spools) == 3


def test_s2_u_art_01_new_artifact_names_evade_legacy_root_globs(tmp_path, legacy_root_sweeper):
    for suffix in OWNER_ARTIFACT_SUFFIXES:
        path = tmp_path / f"sample{suffix}"
        if suffix.endswith("mailbox"):
            path.mkdir()
        else:
            path.touch()
    (tmp_path / "compat.json").touch()
    (tmp_path / "old.lock").touch()
    json_paths, lock_paths = legacy_root_sweeper(tmp_path)
    assert {p.name for p in json_paths} == {"compat.json"}
    assert {p.name for p in lock_paths} == {"old.lock"}
    assert not any(suffix.endswith((".json", ".lock")) for suffix in OWNER_ARTIFACT_SUFFIXES)


def test_s2_u_leg_01_legacy_authority_is_not_inferred_from_namespace_alone():
    lock = LockEvidence("absent_legacy")
    dead = LivenessEvidence("dead", "same_namespace_legacy")
    unrelated = reconcile_owner_episode(
        lock,
        dead,
        exit_evidence=True,
        legacy_authority=LegacyAuthority(recorded="service-token", observer="other-token"),
    )
    authorized = reconcile_owner_episode(
        lock,
        dead,
        exit_evidence=True,
        legacy_authority=LegacyAuthority(recorded="service-token", observer="service-token"),
    )
    manual = reconcile_owner_episode(
        lock,
        dead,
        exit_evidence=True,
        legacy_authority=LegacyAuthority(recorded="service-token", observer="other-token", manual_recovery=True),
    )
    assert unrelated.state == "unverifiable"
    assert authorized.state == manual.state == "terminalizable"
