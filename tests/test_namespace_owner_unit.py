"""S2-U: contract tests for namespace-safe shared owner primitives."""

from __future__ import annotations

import asyncio
import errno
import os
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
