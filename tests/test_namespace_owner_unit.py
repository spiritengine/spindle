"""S2-U: contract tests for namespace-safe shared owner primitives."""

from __future__ import annotations

import ast
import asyncio
import builtins
import errno
import fcntl
import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import spindle
from spindle.namespace_owner import (
    OWNER_ARTIFACT_SUFFIXES,
    ControlReceipt,
    ControlRequest,
    EpisodeTransitionResult,
    LegacyAuthority,
    LivenessEvidence,
    LockEvidence,
    MalformedControlReceipt,
    NamespaceIdentity,
    ProcessIdentity,
    acquire_ownership_lock,
    active_spool_count,
    assess_process_liveness,
    capture_pid_namespace,
    create_control_request,
    iter_control_requests,
    mailbox_path,
    parse_proc_stat_starttime,
    probe_ownership_lock,
    read_control_receipt,
    reconcile_owner_episode,
    update_control_receipt,
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
    monkeypatch.setattr(
        os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(errno.EACCES, "no"))
    )
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


def _valid_request_payload(**changes):
    value = {
        "request_id": "request-1",
        "kind": "cancel",
        "desired_terminal_kind": "cancelled",
        "owner_generation": 1,
        "requested_at": "2026-08-27T00:00:00+00:00",
        "requested_by": "unit-test",
        "observer_pid": 42,
        "observer_namespace": {"status": "supported", "device": 0, "inode": 7},
        "reason": None,
        "deadline": None,
    }
    value.update(changes)
    return value


def _valid_receipt_payload(**changes):
    value = {
        "request_id": "request-1",
        "owner_generation": 1,
        "owner_acknowledged_at": None,
        "provider_cancel_attempted_at": None,
        "provider_acknowledged_at": None,
        "terminal_observed_at": None,
        "forced_cleanup_started_at": None,
        "forced_cleanup_completed_at": None,
        "child_exit_observed_at": None,
        "cleanup_outcome": "rejected_stale_generation",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ({"status": "supported", "device": 0, "inode": 9}, NamespaceIdentity.supported(0, 9)),
        ({"status": "unsupported", "reason": "no proc"}, NamespaceIdentity.unsupported("no proc")),
    ],
)
def test_namespace_identity_strict_decoder_preserves_valid_encodings(encoded, expected):
    assert NamespaceIdentity.from_dict(encoded) == expected
    assert expected.to_dict() == encoded


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        {},
        {"status": "supported", "device": 1},
        {"status": "supported", "device": 1, "inode": 2, "extra": None},
        {"status": "supported", "device": True, "inode": 2},
        {"status": "supported", "device": 1.0, "inode": 2},
        {"status": "supported", "device": "1", "inode": 2},
        {"status": "supported", "device": None, "inode": 2},
        {"status": "supported", "device": -1, "inode": 2},
        {"status": "unsupported"},
        {"status": "unsupported", "reason": ""},
        {"status": "unsupported", "reason": None},
        {"status": "unsupported", "reason": "why", "device": 1},
        {"status": "unknown", "reason": "why"},
    ],
)
def test_namespace_identity_rejects_coercions_and_malformed_shapes(encoded):
    with pytest.raises(ValueError):
        NamespaceIdentity.from_dict(encoded)


@pytest.mark.parametrize("field", ["owner_generation", "observer_pid"])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None, 0, -1])
def test_control_request_rejects_nonpositive_or_nonplain_integer_fields(field, invalid):
    with pytest.raises(ValueError):
        ControlRequest.from_dict(_valid_request_payload(**{field: invalid}))


@pytest.mark.parametrize("invalid", [True, 1.0, "1", None, -1])
def test_control_request_rejects_nested_namespace_integer_coercions(invalid):
    namespace = {"status": "supported", "device": invalid, "inode": 7}
    with pytest.raises(ValueError):
        ControlRequest.from_dict(_valid_request_payload(observer_namespace=namespace))


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": ""},
        {"request_id": "../escape"},
        {"kind": "timeout"},
        {"requested_at": ""},
        {"requested_by": None},
        {"reason": 1},
        {"deadline": False},
    ],
)
def test_control_request_rejects_invalid_identity_relationship_and_strings(changes):
    with pytest.raises(ValueError):
        ControlRequest.from_dict(_valid_request_payload(**changes))


@pytest.mark.parametrize("shape", ["missing", "extra"])
def test_control_request_requires_exact_shape(shape):
    payload = _valid_request_payload()
    if shape == "missing":
        payload.pop("deadline")
    else:
        payload["extra"] = None
    with pytest.raises(ValueError):
        ControlRequest.from_dict(payload)


@pytest.mark.parametrize("field,invalid", [("owner_generation", True), ("observer_pid", 1.0)])
def test_create_control_request_validates_without_coercion(tmp_path, field, invalid):
    arguments = {"owner_generation": 1, "observer_pid": 42}
    arguments[field] = invalid
    with pytest.raises(ValueError):
        create_control_request(tmp_path, "spool-a", "cancel", requested_by="test", **arguments)
    assert not mailbox_path(tmp_path, "spool-a").exists()


def test_malformed_lexical_first_request_is_preserved_and_does_not_hide_valid_sibling(tmp_path):
    valid = create_control_request(
        tmp_path,
        "spool-a",
        "drop",
        1,
        "test",
        request_id="zzz-valid",
        observer_pid=42,
        observer_namespace=NamespaceIdentity.supported(1, 2),
    )
    damaged_path = mailbox_path(tmp_path, "spool-a") / "000-damaged.request"
    damaged_path.write_text(json.dumps(_valid_request_payload(request_id="payload-id")))
    damaged_bytes = damaged_path.read_bytes()

    assert list(iter_control_requests(tmp_path, "spool-a")) == [valid]
    assert damaged_path.read_bytes() == damaged_bytes


@pytest.mark.parametrize("poison", [True, 1.0], ids=["true-vs-1", "1.0-vs-1"])
def test_malformed_request_collision_is_preserved_and_not_idempotent(tmp_path, poison):
    request_id = "collided-request"
    path = mailbox_path(tmp_path, "spool-a") / f"{request_id}.request"
    path.parent.mkdir()
    path.write_text(json.dumps(_valid_request_payload(request_id=request_id, owner_generation=poison)))
    original = path.read_bytes()

    with pytest.raises(ValueError):
        create_control_request(
            tmp_path,
            "spool-a",
            "cancel",
            1,
            "unit-test",
            request_id=request_id,
            observer_pid=42,
            observer_namespace=NamespaceIdentity.supported(0, 7),
        )

    assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None, 0, -1])
def test_control_receipt_rejects_nonpositive_or_nonplain_generation(invalid):
    with pytest.raises(ValueError):
        ControlReceipt.from_dict(_valid_receipt_payload(owner_generation=invalid))


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": ""},
        {"request_id": "../escape"},
        {"cleanup_outcome": False},
        {"owner_acknowledged_at": 1},
    ],
)
def test_control_receipt_rejects_invalid_identity_and_fact_types(changes):
    with pytest.raises(ValueError):
        ControlReceipt.from_dict(_valid_receipt_payload(**changes))


@pytest.mark.parametrize("shape", ["missing", "extra"])
def test_control_receipt_requires_exact_shape(shape):
    payload = _valid_receipt_payload()
    if shape == "missing":
        payload.pop("cleanup_outcome")
    else:
        payload["extra"] = None
    with pytest.raises(ValueError):
        ControlReceipt.from_dict(payload)


@pytest.mark.parametrize(
    "mismatch",
    [
        {"request_id": "other-request"},
        {"owner_generation": 2},
    ],
    ids=["request-id", "owner-generation"],
)
def test_existing_receipt_identity_mismatch_is_preserved_and_rejected(tmp_path, mismatch):
    request = create_control_request(
        tmp_path,
        "spool-a",
        "cancel",
        1,
        "unit-test",
        request_id="request-1",
        observer_pid=42,
        observer_namespace=NamespaceIdentity.supported(0, 7),
    )
    path = mailbox_path(tmp_path, "spool-a") / "request-1.receipt"
    path.write_text(json.dumps(_valid_receipt_payload(**mismatch)))
    original = path.read_bytes()

    with pytest.raises(MalformedControlReceipt):
        read_control_receipt(tmp_path, "spool-a", request.request_id)
    with pytest.raises(MalformedControlReceipt):
        write_control_receipt(tmp_path, "spool-a", request, current_generation=1)
    assert path.read_bytes() == original


def test_write_control_receipt_rejects_request_generation_not_bound_to_mailbox(tmp_path):
    create_control_request(
        tmp_path,
        "spool-a",
        "cancel",
        1,
        "unit-test",
        request_id="request-1",
        observer_pid=42,
        observer_namespace=NamespaceIdentity.supported(0, 7),
    )
    mismatched = ControlRequest.from_dict(_valid_request_payload(owner_generation=2))

    with pytest.raises(ValueError):
        write_control_receipt(tmp_path, "spool-a", mismatched, current_generation=2)

    assert not (mailbox_path(tmp_path, "spool-a") / "request-1.receipt").exists()


@pytest.mark.parametrize("accepted", [0, 1, "true", 1.0])
def test_write_control_receipt_requires_bool_or_none_accepted(tmp_path, accepted):
    request = create_control_request(tmp_path, "spool-a", "cancel", 1, "unit-test")
    with pytest.raises(ValueError):
        write_control_receipt(tmp_path, "spool-a", request, current_generation=1, accepted=accepted)
    assert read_control_receipt(tmp_path, "spool-a", request.request_id) is None


@pytest.mark.parametrize("current_generation", [True, 1.0, "1", None, 0, -1])
def test_write_control_receipt_requires_positive_plain_current_generation(tmp_path, current_generation):
    request = create_control_request(tmp_path, "spool-a", "cancel", 1, "unit-test")
    with pytest.raises(ValueError):
        write_control_receipt(tmp_path, "spool-a", request, current_generation=current_generation)
    assert read_control_receipt(tmp_path, "spool-a", request.request_id) is None


@pytest.mark.parametrize("rejection_outcome", [False, 1, 1.0])
def test_write_control_receipt_rejects_nonstring_rejection_outcome(tmp_path, rejection_outcome):
    request = create_control_request(tmp_path, "spool-a", "cancel", 1, "unit-test")
    with pytest.raises(ValueError):
        write_control_receipt(
            tmp_path,
            "spool-a",
            request,
            current_generation=1,
            accepted=False,
            rejection_outcome=rejection_outcome,
        )
    assert read_control_receipt(tmp_path, "spool-a", request.request_id) is None


@pytest.mark.parametrize("field,value", [("owner_generation", 2), ("cleanup_outcome", False)])
def test_rejected_receipt_updates_preserve_published_bytes(tmp_path, field, value):
    request = create_control_request(tmp_path, "spool-a", "cancel", 1, "unit-test")
    write_control_receipt(tmp_path, "spool-a", request, current_generation=1)
    path = mailbox_path(tmp_path, "spool-a") / f"{request.request_id}.receipt"
    original = path.read_bytes()

    with pytest.raises(ValueError):
        update_control_receipt(tmp_path, "spool-a", request.request_id, **{field: value})

    assert path.read_bytes() == original


def test_valid_stale_generation_receipt_keeps_existing_disposition(tmp_path):
    request = create_control_request(tmp_path, "spool-a", "cancel", 1, "unit-test")
    receipt = write_control_receipt(tmp_path, "spool-a", request, current_generation=2)
    assert receipt.cleanup_outcome == "rejected_stale_generation"
    assert read_control_receipt(tmp_path, "spool-a", request.request_id) == receipt


def test_every_owner_exit_code_is_disjoint_from_watchdog_crash_channel():
    from spindle.owner_watchdog import _owner_process_crashed

    for exit_code in range(256):
        assert _owner_process_crashed(exit_code << 8, exception_reported=False) is False
        assert _owner_process_crashed(exit_code << 8, exception_reported=True) is True


def test_natural_exit_retries_descendant_cleanup_before_evidence_or_release(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import DESCENDANTS_SETTLED, DESCENDANTS_SURVIVED, LogicalOwner

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
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner.lock = TrackingLock()
    owner._authority_lost = False
    owner._await_launch_barrier = lambda: True
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda **_kwargs: None
    owner._verify_lock = lambda **_kwargs: True
    owner._next_current_request = lambda: None
    owner._provider_exited = lambda: True
    owner._finish_provider = lambda: 0
    owner._set_lifecycle = lambda **_values: None
    owner._settle_other_requests_unlocked = lambda *_args, **_kwargs: True
    cleanup_calls = []

    def settle_descendants(*, force):
        cleanup_calls.append(force)
        assert owner.lock.closed is False
        return DESCENDANTS_SETTLED if len(cleanup_calls) > 1 else DESCENDANTS_SURVIVED

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


def _natural_exit_owner(tmp_path, spool_id, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner
    from tests.owner_episode_fixtures import make_episode

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=None,
        poll_interval=0,
        watchdog_fd=None,
        disposition_fd=None,
        launch_barrier_fd=None,
    )
    owner.store = tmp_path
    owner.spool_id = spool_id
    owner.generation = 1
    owner.spool_path = tmp_path / f"{spool_id}.json"
    owner.spool_lock_path = tmp_path / f"{spool_id}.lock"
    owner.lock_path = tmp_path / f"{spool_id}.process-owner"
    owner.provider = SimpleNamespace(pid=456)
    owner.provider_pidfd = None
    owner.control = None
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner.lock = acquire_ownership_lock(owner.lock_path)
    owner._authority_lost = False
    owner.adopted_reaped = 0
    owner.wall_deadline_at = None
    owner._contain_own_children = lambda _bound: True
    owner._await_launch_barrier = lambda: True
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda **_kwargs: None
    owner._terminal_output_state = lambda: (False, False, None)
    owner._next_current_request = lambda: None
    owner._provider_exited = lambda: True
    owner._finish_provider = lambda: 0
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "running",
                "owner_episode": make_episode("accepted", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)
    return owner


def test_natural_exit_reproves_before_final_arbitration_after_evidence_checkpoint(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import AUTHORITY_LOST_EXIT_CODE, DESCENDANTS_SETTLED

    owner = _natural_exit_owner(tmp_path, "natural-final-arbitration-loss", monkeypatch)
    owner._settle_descendants = lambda **_kwargs: DESCENDANTS_SETTLED
    owner._write_exit_evidence = lambda *_args, **_kwargs: True
    contender = None

    def checkpoint(name, *_args):
        nonlocal contender
        if name == "natural_exit_evidence_published":
            contender = _replace_and_hold_owner_lock(owner)

    owner.checkpoints = type(
        "Checkpoints", (), {"reach": staticmethod(checkpoint), "socket": None, "pause_name": None}
    )()
    monkeypatch.setattr(
        "spindle.namespace_owner_process.mailbox_guard",
        lambda *_args, **_kwargs: pytest.fail("stale natural-exit owner entered final mailbox arbitration"),
    )

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        assert owner._read_spool()["owner_episode"]["phase"] == "accepted"
    finally:
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)


def test_descendants_survived_lifecycle_reproves_after_containment(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import AUTHORITY_LOST_EXIT_CODE, DESCENDANTS_SURVIVED

    owner = _natural_exit_owner(tmp_path, "descendants-survived-lifecycle-loss", monkeypatch)
    owner.spool_lock_path.touch(mode=0o600)
    contender = None

    def settle_then_replace(**_kwargs):
        nonlocal contender
        contender = _replace_and_hold_owner_lock(owner)
        return DESCENDANTS_SURVIVED

    owner._settle_descendants = settle_then_replace
    owner._write_exit_evidence = lambda *_args, **_kwargs: pytest.fail("exit evidence wrote after stale containment")

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        lifecycle = owner._read_spool().get("lifecycle") or {}
        assert lifecycle.get("cleanup_outcome") != "descendants_survived"
    finally:
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)


def test_evidence_preservation_grace_ships_at_five_seconds():
    """Pin the shipped grace; the lifecycle fixtures run it at a short width.

    finding-20260820-u3go measured a real Claude Code SIGTERM shutdown at 607ms
    for a legacy one-shot and 2.86s for a stream-driver descendant with the
    configured 1.5s default SessionEnd hook budget.  5.0s covers both with
    scheduler margin while keeping the SIGKILL backstop bounded.
    """
    from spindle.namespace_owner_process import (
        DEFAULT_EVIDENCE_PRESERVATION_GRACE_SECONDS,
        EVIDENCE_PRESERVATION_GRACE_SECONDS,
    )

    assert DEFAULT_EVIDENCE_PRESERVATION_GRACE_SECONDS == 5.0
    if "SPINDLE_EVIDENCE_GRACE_SECONDS" not in os.environ:
        assert EVIDENCE_PRESERVATION_GRACE_SECONDS == DEFAULT_EVIDENCE_PRESERVATION_GRACE_SECONDS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inf", 5.0),
        ("Infinity", 5.0),
        ("-inf", 5.0),
        ("nan", 5.0),
        ("not-a-number", 5.0),
        ("", 5.0),
        ("-5", 0.0),
        ("0.2", 0.2),
        ("5.0", 5.0),
        ("50", 5.0),
    ],
)
def test_evidence_grace_override_is_clamped_to_the_production_ceiling(monkeypatch, raw, expected):
    """The override can only shorten the grace, never lengthen or unbound it.

    ``float()`` happily parses "inf"/"nan" without raising, so a leaked or
    malformed SPINDLE_EVIDENCE_GRACE_SECONDS must not let a provider that
    ignores SIGTERM hold off the SIGKILL backstop past the shipped 5.0s
    ceiling - that would silently break the "SIGKILL is unconditional"
    invariant the termination path depends on.
    """
    from spindle.namespace_owner_process import _resolve_evidence_grace_seconds

    monkeypatch.setenv("SPINDLE_EVIDENCE_GRACE_SECONDS", raw)
    assert _resolve_evidence_grace_seconds() == expected


def test_evidence_grace_defaults_when_unset(monkeypatch):
    from spindle.namespace_owner_process import (
        DEFAULT_EVIDENCE_PRESERVATION_GRACE_SECONDS,
        _resolve_evidence_grace_seconds,
    )

    monkeypatch.delenv("SPINDLE_EVIDENCE_GRACE_SECONDS", raising=False)
    assert _resolve_evidence_grace_seconds() == DEFAULT_EVIDENCE_PRESERVATION_GRACE_SECONDS


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


def test_owner_termination_classification_is_exit_code_independent():
    """Authority-loss classification never turns on the 0-255 exit status.

    Mirrors test_every_owner_exit_code_is_disjoint_from_watchdog_crash_channel:
    the disposition pipe, not the exit code, decides whether the watchdog
    takes the bounded authority-loss pass, the unbounded crash retry, or the
    plain natural-exit drain.
    """
    from spindle.owner_watchdog import _classify_owner_termination

    for exit_code in range(256):
        status = exit_code << 8
        assert _classify_owner_termination(status, exception_reported=False, authority_lost=True) == "authority_lost"
        assert _classify_owner_termination(status, exception_reported=True, authority_lost=True) == "authority_lost"
        assert _classify_owner_termination(status, exception_reported=False, authority_lost=False) == "natural"
        assert _classify_owner_termination(status, exception_reported=True, authority_lost=False) == "crashed"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inf", 3.0),
        ("Infinity", 3.0),
        ("-inf", 3.0),
        ("nan", 3.0),
        ("not-a-number", 3.0),
        ("", 3.0),
        ("-5", 0.0),
        ("0.2", 0.2),
        ("3.0", 3.0),
        ("30", 3.0),
    ],
)
def test_containment_bound_override_is_clamped_to_the_production_ceiling(monkeypatch, raw, expected):
    """The override can only shorten the bounded pass, never lengthen it."""
    from spindle.namespace_owner_process import _resolve_containment_bound_seconds

    monkeypatch.setenv("SPINDLE_CONTAINMENT_BOUND_SECONDS", raw)
    assert _resolve_containment_bound_seconds() == expected


def test_containment_bound_defaults_when_unset(monkeypatch):
    from spindle.namespace_owner_process import (
        DEFAULT_CONTAINMENT_BOUND_SECONDS,
        _resolve_containment_bound_seconds,
    )

    monkeypatch.delenv("SPINDLE_CONTAINMENT_BOUND_SECONDS", raising=False)
    assert _resolve_containment_bound_seconds() == DEFAULT_CONTAINMENT_BOUND_SECONDS


def test_both_halves_of_the_pair_share_one_containment_bound():
    """The owner's own pass and the watchdog's cannot drift apart."""
    import spindle.namespace_owner_process as owner_module
    import spindle.owner_watchdog as watchdog_module

    assert watchdog_module.DEFAULT_CONTAINMENT_BOUND_SECONDS is owner_module.DEFAULT_CONTAINMENT_BOUND_SECONDS
    assert watchdog_module.CONTAINMENT_BOUND_SECONDS == owner_module.CONTAINMENT_BOUND_SECONDS


def test_owner_rechecks_inherited_deadline_immediately_before_provider_popen(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import SETTLEMENT_PUBLISHED, LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        watchdog_fd=None,
        stdin_path=None,
        command=["provider-must-not-run"],
        cwd=str(tmp_path),
        disable_pdeathsig=True,
    )
    owner.store = tmp_path
    owner.spool_id = "deadline-before-provider"
    owner.stdout_path = tmp_path / "deadline-before-provider.stdout"
    owner.stderr_path = tmp_path / "deadline-before-provider.stderr"
    owner.wall_deadline_at = "2026-08-11T00:00:00+00:00"
    owner._remaining_wall_budget = lambda: 0.0
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._settle_deadline_expiry_after_binding = lambda: SETTLEMENT_PUBLISHED
    owner.provider = None
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider executed after inherited deadline"),
    )

    assert owner._spawn_provider() == 124


def _lock_owner(tmp_path, spool_id="verify-lock"):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(poll_interval=0)
    owner.store = tmp_path
    owner.spool_id = spool_id
    owner.spool_path = tmp_path / f"{spool_id}.json"
    owner.spool_lock_path = tmp_path / f"{spool_id}.lock"
    owner.lock_path = tmp_path / f"{spool_id}.process-owner"
    owner.owner_identity_path = tmp_path / f"{spool_id}.owner-identity"
    owner.generation = 1
    owner.episode_mode = False
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner.lock = acquire_ownership_lock(owner.lock_path)
    owner._authority_lost = False
    return owner


def _replace_and_hold_owner_lock(owner):
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)
    contender = os.open(owner.lock_path, os.O_RDWR)
    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return contender


def test_verify_lock_latches_authority_lost_on_missing_pathname(tmp_path):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path)
    owner.lock_path.unlink()

    with pytest.raises(AuthorityLost):
        owner._verify_lock()
    assert owner._authority_lost is True
    assert not owner.spool_path.exists(), "a proven-missing pathname must not write a diagnostic marker"


def test_verify_lock_latches_authority_lost_on_replaced_inode(tmp_path):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path)
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)  # a different inode at the same pathname

    with pytest.raises(AuthorityLost):
        owner._verify_lock()
    assert owner._authority_lost is True
    assert not owner.spool_path.exists(), "a proven-different inode must not write a diagnostic marker"


def test_publish_owner_identity_rechecks_authority_before_writing_artifacts(tmp_path):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path, spool_id="pre-identity-loss")
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)

    with pytest.raises(AuthorityLost):
        owner._publish_owner_identity()
    assert owner._authority_lost is True
    assert not owner.owner_identity_path.exists()
    assert not (tmp_path / "pre-identity-loss.journal-guard").exists()
    assert not (tmp_path / "pre-identity-loss.control-mailbox").exists()


def test_publish_owner_identity_rechecks_authority_after_lock_bound_transition(tmp_path):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _lock_owner(tmp_path, spool_id="loss-after-lock-bound")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("reserved", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    contenders = []

    def checkpoint(name, *_args):
        if name == "owner_episode_lock_bound":
            contenders.append(_replace_and_hold_owner_lock(owner))

    owner.checkpoints = SimpleNamespace(reach=checkpoint, socket=None, pause_name=None)

    try:
        with pytest.raises(AuthorityLost):
            owner._publish_owner_identity()
        assert json.loads(owner.spool_path.read_text())["owner_episode"]["phase"] == "lock_bound"
        assert not owner.owner_identity_path.exists()
        assert not (tmp_path / "loss-after-lock-bound.journal-guard").exists()
        assert not (tmp_path / "loss-after-lock-bound.control-mailbox").exists()
    finally:
        for contender in contenders:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_publish_owner_identity_rechecks_authority_after_identity_mirror(tmp_path):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path, spool_id="loss-after-identity-mirror")
    contenders = []

    def checkpoint(name, *_args):
        if name == "owner_identity_mirror_published":
            contenders.append(_replace_and_hold_owner_lock(owner))

    owner.checkpoints = SimpleNamespace(reach=checkpoint, socket=None, pause_name=None)

    try:
        with pytest.raises(AuthorityLost):
            owner._publish_owner_identity()
        assert owner.owner_identity_path.exists()
        assert not (tmp_path / "loss-after-identity-mirror.journal-guard").exists()
        assert not (tmp_path / "loss-after-identity-mirror.control-mailbox").exists()
    finally:
        for contender in contenders:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_publish_owner_identity_rechecks_authority_after_journal_guard(tmp_path):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path, spool_id="loss-after-journal-guard")
    contenders = []

    def checkpoint(name, *_args):
        if name == "owner_journal_guard_published":
            contenders.append(_replace_and_hold_owner_lock(owner))

    owner.checkpoints = SimpleNamespace(reach=checkpoint, socket=None, pause_name=None)

    try:
        with pytest.raises(AuthorityLost):
            owner._publish_owner_identity()
        assert owner.owner_identity_path.exists()
        assert (tmp_path / "loss-after-journal-guard.journal-guard").exists()
        assert not (tmp_path / "loss-after-journal-guard.control-mailbox").exists()
    finally:
        for contender in contenders:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


@pytest.mark.parametrize("cleanup_failure", ["unlock", "close"])
@pytest.mark.parametrize("body_exception", [True, False])
def test_mailbox_guard_cleanup_preserves_active_exception_and_reports_clean_exit_failures(
    tmp_path, monkeypatch, cleanup_failure, body_exception
):
    from spindle.namespace_owner import mailbox_guard
    from spindle.namespace_owner_process import AuthorityLost

    real_flock = fcntl.flock
    real_close = os.close
    closed = []

    def maybe_fail_unlock(fd, operation):
        if cleanup_failure == "unlock" and operation == fcntl.LOCK_UN:
            raise OSError("simulated mailbox unlock failure")
        return real_flock(fd, operation)

    def maybe_fail_close(fd):
        closed.append(fd)
        real_close(fd)
        if cleanup_failure == "close":
            raise OSError("simulated mailbox close failure")

    monkeypatch.setattr("spindle.namespace_owner.fcntl.flock", maybe_fail_unlock)
    monkeypatch.setattr("spindle.namespace_owner._close_fd", maybe_fail_close)

    if body_exception:
        with pytest.raises(AuthorityLost):
            with mailbox_guard(tmp_path, "mailbox-cleanup"):
                raise AuthorityLost()
    else:
        with pytest.raises(OSError, match=f"simulated mailbox {cleanup_failure} failure"):
            with mailbox_guard(tmp_path, "mailbox-cleanup"):
                pass

    assert closed, "mailbox cleanup did not attempt to close the guard descriptor"


def test_authority_loss_inside_mailbox_guard_survives_cleanup_failure_for_watchdog_classification(
    tmp_path, monkeypatch
):
    from types import MethodType, SimpleNamespace

    from spindle.namespace_owner_process import (
        AUTHORITY_LOST_DISPOSITION,
        AUTHORITY_LOST_EXIT_CODE,
        AuthorityLost,
        LogicalOwner,
        mailbox_guard,
    )
    from spindle.owner_watchdog import _classify_owner_termination

    real_flock = fcntl.flock

    def fail_mailbox_unlock(fd, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError("simulated final mailbox unlock failure")
        return real_flock(fd, operation)

    disposition_read, disposition_write = os.pipe()
    spool_id = "mailbox-authority-loss"
    spool_path = tmp_path / f"{spool_id}.json"
    original_record = {"id": spool_id, "status": "pending"}
    spool_path.write_text(json.dumps(original_record, sort_keys=True))

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(watchdog_fd=None, disposition_fd=disposition_write)
    owner.store = tmp_path
    owner.spool_id = spool_id
    owner.spool_path = spool_path
    owner.owner_exit_path = tmp_path / f"{spool_id}.owner-exit"
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = None
    owner._authority_lost = False
    owner._contain_own_children = lambda _bound: True

    def forbidden_shared_write(*_args, **_kwargs):
        raise AssertionError("authority-loss finalizer touched shared owner state")

    def guarded_authority_loss(self):
        with mailbox_guard(self.store, self.spool_id):
            self._authority_lost = True
            raise AuthorityLost()

    owner._run = MethodType(guarded_authority_loss, owner)
    monkeypatch.setattr("spindle.namespace_owner.fcntl.flock", fail_mailbox_unlock)
    monkeypatch.setattr("spindle.namespace_owner_process._atomic_json_write", forbidden_shared_write)
    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", forbidden_shared_write)

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        assert os.read(disposition_read, 4096) == AUTHORITY_LOST_DISPOSITION
        assert (
            _classify_owner_termination(
                AUTHORITY_LOST_EXIT_CODE << 8,
                exception_reported=False,
                authority_lost=True,
            )
            == "authority_lost"
        )
        assert json.loads(spool_path.read_text()) == original_record
        assert not owner.owner_exit_path.exists()
    finally:
        os.close(disposition_read)
        try:
            os.close(disposition_write)
        except OSError:
            pass


def test_authority_lost_latch_is_irreversible_even_after_inode_restore(tmp_path, monkeypatch):
    """Once tripped, the latch never re-derives from the filesystem.

    A later pathname that would look perfectly healthy to a fresh check must
    not clear it - that is what makes the loss irreversible.
    """
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path)
    owner.lock_path.unlink()

    with pytest.raises(AuthorityLost):
        owner._verify_lock()
    assert owner._authority_lost is True

    owner.lock_path.touch(mode=0o600)
    real_stat, real_access = os.stat, os.access

    def tripwire(real):
        def probe(path, *args, **kwargs):
            if str(path) == str(owner.lock_path):
                pytest.fail("latched owner re-checked the ownership pathname")
            return real(path, *args, **kwargs)

        return probe

    monkeypatch.setattr("spindle.namespace_owner_process.os.stat", tripwire(real_stat))
    monkeypatch.setattr("spindle.namespace_owner_process.os.access", tripwire(real_access))
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.fstat",
        lambda _fd: pytest.fail("latched owner re-read its own lock descriptor"),
    )
    with pytest.raises(AuthorityLost):
        owner._verify_lock()


def test_verify_lock_unreadable_pathname_stays_recoverable(tmp_path):
    """The unreadable case is unchanged: it still returns False and recovers."""
    owner = _lock_owner(tmp_path)
    owner.lock_path.chmod(0)
    try:
        assert owner._verify_lock() is False
        assert owner._authority_lost is False
        assert owner._read_spool()["lifecycle"]["ownership_state"] == "unreadable"
    finally:
        owner.lock_path.chmod(0o600)
    assert owner._verify_lock() is True
    assert owner._authority_lost is False
    assert owner._read_spool()["lifecycle"]["ownership_state"] == "held"


def test_spool_record_guard_missing_compatibility_lock_stays_prebinding_noop(tmp_path):
    owner = _lock_owner(tmp_path, spool_id="prebinding-no-record-lock")
    owner.spool_path.write_text(json.dumps({"id": owner.spool_id, "status": "pending"}, sort_keys=True))
    owner._verify_lock = lambda **_kwargs: pytest.fail("absent compatibility lock requested authority proof")

    try:
        assert owner._update_spool(status="running")["status"] == "running"
    finally:
        owner.lock.close()


def test_authority_loss_inside_spool_record_guard_survives_cleanup_failure(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path, spool_id="spool-guard-cleanup-loss")
    owner.spool_lock_path.touch(mode=0o600)
    real_flock = fcntl.flock

    def fail_unlock(fd, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError("simulated spool lock unlock failure")
        return real_flock(fd, operation)

    owner._verify_lock = lambda **_kwargs: (_ for _ in ()).throw(AuthorityLost())
    monkeypatch.setattr("spindle.namespace_owner.fcntl.flock", fail_unlock)

    try:
        with pytest.raises(AuthorityLost):
            with owner._spool_record_guard():
                pytest.fail("guard yielded after authority loss")
    finally:
        monkeypatch.setattr("spindle.namespace_owner.fcntl.flock", real_flock)
        owner.lock.close()


@pytest.mark.parametrize(
    "operation",
    [
        "update_spool",
        "set_lifecycle",
        "terminal_timestamp",
        "direct_guard_running_status",
        "direct_guard_terminal_status",
    ],
)
def test_spool_record_mutations_reprove_after_blocked_record_lock(tmp_path, operation):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _spawning_owner(tmp_path, f"blocked-record-{operation}")
    owner.spool_path.write_text(
        json.dumps({"id": owner.spool_id, "status": "pending", "harness": "gemini"}, sort_keys=True)
    )
    owner.spool_lock_path.touch(mode=0o600)
    if operation == "terminal_timestamp":
        owner.stdout_path.write_text(json.dumps({"response": "complete", "session_id": "s"}) + "\n")
    before = owner.spool_path.read_text()
    guard_fd = os.open(owner.spool_lock_path, os.O_RDWR)
    fcntl.flock(guard_fd, fcntl.LOCK_EX)
    errors = []
    results = []

    def mutate():
        try:
            if operation == "update_spool":
                results.append(owner._update_spool(owner_pid=999))
            elif operation == "set_lifecycle":
                results.append(owner._set_lifecycle(transport_state="connected"))
            elif operation == "terminal_timestamp":
                results.append(owner._terminal_output_state())
            elif operation == "direct_guard_running_status":
                with owner._spool_record_guard():
                    spool = owner._read_spool()
                    spool["status"] = "running"
                    owner._write_spool_unlocked(spool)
            elif operation == "direct_guard_terminal_status":
                with owner._spool_record_guard():
                    spool = owner._read_spool()
                    spool["status"] = "error"
                    spool["completed_at"] = "stale"
                    owner._write_spool_unlocked(spool)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=mutate)
    worker.start()
    contender = None
    try:
        worker.join(timeout=0.05)
        assert worker.is_alive(), "mutation did not block on the held spool record lock"
        contender = _replace_and_hold_owner_lock(owner)
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], AuthorityLost)
        assert owner.spool_path.read_text() == before
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(guard_fd)
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_owner_episode_transition_reproves_after_blocked_record_lock(tmp_path):
    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "blocked-owner-episode-transition")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    owner.spool_lock_path.touch(mode=0o600)
    before_episode = json.dumps(json.loads(owner.spool_path.read_text())["owner_episode"], sort_keys=True)
    guard_fd = os.open(owner.spool_lock_path, os.O_RDWR)
    fcntl.flock(guard_fd, fcntl.LOCK_EX)
    errors = []
    results = []

    def transition():
        try:
            results.append(
                owner._transition_owner_episode_locked(
                    destination="accepted",
                    facts={
                        "provider": {
                            "pid": 4242,
                            "pgid": 4242,
                            "birth_token": "test-start",
                            "namespace": capture_pid_namespace().to_dict(),
                        },
                        "provider_custody": {
                            "pidfd_acquired": False,
                            "containment": "watchdog",
                            "published_at": datetime.now().isoformat(),
                        },
                    },
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=transition)
    worker.start()
    contender = None
    try:
        worker.join(timeout=0.05)
        assert worker.is_alive(), "transition did not block on the held spool record lock"
        contender = _replace_and_hold_owner_lock(owner)
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], AuthorityLost)
        after_episode = json.dumps(json.loads(owner.spool_path.read_text())["owner_episode"], sort_keys=True)
        assert after_episode == before_episode
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(guard_fd)
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_owner_episode_missing_sidecar_transition_reproves_after_creating_blocked_record_lock(tmp_path, monkeypatch):
    import spindle.namespace_owner_process as owner_module
    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "blocked-missing-sidecar-owner-episode")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    assert not owner.spool_lock_path.exists()
    before_episode = json.dumps(json.loads(owner.spool_path.read_text())["owner_episode"], sort_keys=True)
    real_open = owner_module.os.open
    sidecar_created = threading.Event()
    allow_worker_to_flock = threading.Event()
    errors = []
    results = []

    def open_then_pause_on_created_sidecar(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = real_open(path, flags, mode)
        else:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == owner.spool_lock_path and flags & os.O_CREAT and not sidecar_created.is_set():
            sidecar_created.set()
            if not allow_worker_to_flock.wait(timeout=2):
                raise TimeoutError("test did not acquire the newly created sidecar lock")
        return fd

    monkeypatch.setattr("spindle.namespace_owner_process.os.open", open_then_pause_on_created_sidecar)

    def transition():
        try:
            results.append(
                owner._transition_owner_episode_locked(
                    destination="accepted",
                    facts={
                        "provider": {
                            "pid": 4242,
                            "pgid": 4242,
                            "birth_token": "test-start",
                            "namespace": capture_pid_namespace().to_dict(),
                        },
                        "provider_custody": {
                            "pidfd_acquired": False,
                            "containment": "watchdog",
                            "published_at": datetime.now().isoformat(),
                        },
                    },
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=transition)
    worker.start()
    guard_fd = None
    contender = None
    try:
        assert sidecar_created.wait(timeout=2), "transition did not create the fallback record lock"
        guard_fd = real_open(owner.spool_lock_path, os.O_RDWR)
        fcntl.flock(guard_fd, fcntl.LOCK_EX)
        allow_worker_to_flock.set()
        worker.join(timeout=0.05)
        assert worker.is_alive(), "transition did not block on the newly created record lock"
        contender = _replace_and_hold_owner_lock(owner)
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], AuthorityLost)
        after_episode = json.dumps(json.loads(owner.spool_path.read_text())["owner_episode"], sort_keys=True)
        assert after_episode == before_episode
    finally:
        allow_worker_to_flock.set()
        if guard_fd is not None:
            try:
                fcntl.flock(guard_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(guard_fd)
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_spool_record_guard_does_not_self_deadlock_restoring_lifecycle(tmp_path):
    owner = _lock_owner(tmp_path, spool_id="record-guard-no-recursion")
    owner.spool_lock_path.touch(mode=0o600)
    owner.spool_path.write_text(
        json.dumps({"id": owner.spool_id, "lifecycle": {"ownership_state": "unreadable"}}, sort_keys=True)
    )
    results = []
    errors = []

    def enter_guard():
        try:
            with owner._spool_record_guard() as record_locked:
                results.append(record_locked)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=enter_guard, daemon=True)
    worker.start()
    try:
        worker.join(timeout=1)
        assert not worker.is_alive(), "record guard deadlocked while clearing lifecycle"
        assert errors == []
        assert results == [True]
        assert json.loads(owner.spool_path.read_text())["lifecycle"]["ownership_state"] == "unreadable"
        assert owner._verify_lock() is True
        assert json.loads(owner.spool_path.read_text())["lifecycle"]["ownership_state"] == "held"
    finally:
        owner.lock.close()


def test_prebinding_legacy_deadline_with_compat_lock_does_not_require_owner_lock(tmp_path):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(poll_interval=0)
    owner.store = tmp_path
    owner.spool_id = "prebinding-legacy-deadline"
    owner.spool_path = tmp_path / f"{owner.spool_id}.json"
    owner.spool_lock_path = tmp_path / f"{owner.spool_id}.lock"
    owner.owner_exit_path = tmp_path / f"{owner.spool_id}.owner-exit"
    owner.lock_path = tmp_path / f"{owner.spool_id}.process-owner"
    owner.lock = None
    owner.generation = 1
    owner.episode_mode = False
    owner._authority_lost = False
    owner.spool_lock_path.touch(mode=0o600)
    owner.spool_path.write_text(json.dumps({"id": owner.spool_id, "status": "pending", "timeout": 3}, sort_keys=True))
    owner._verify_lock = lambda **_kwargs: pytest.fail("pre-binding legacy settlement attempted owner proof")

    owner._settle_deadline_expiry_before_binding()

    settled = json.loads(owner.spool_path.read_text())
    assert settled["status"] == "timeout"
    assert settled["error_kind"] == "deadline_expired_before_provider_start"
    evidence = json.loads(owner.owner_exit_path.read_text())
    assert evidence["cleanup_outcome"] == "deadline_expired_before_provider_start"


def test_owner_episode_primitive_is_only_called_by_prebinding_paths_and_wrapper():
    source = Path(spindle.namespace_owner_process.__file__).read_text()
    tree = ast.parse(source)
    callers = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "transition_owner_episode":
                callers.append(self.stack[-1] if self.stack else "<module>")
            self.generic_visit(node)

    Visitor().visit(tree)

    assert callers == [
        "_abort_for_watchdog_loss_before_binding",
        "_settle_deadline_expiry_before_binding",
        "_transition_owner_episode_locked",
    ]


@pytest.mark.parametrize("failure", ["read", "write"])
def test_verify_lock_treats_unreadable_marker_clearing_as_best_effort(tmp_path, monkeypatch, failure):
    owner = _lock_owner(tmp_path, spool_id=f"best-effort-{failure}")
    owner.spool_path.write_text(
        json.dumps({"id": owner.spool_id, "lifecycle": {"ownership_state": "unreadable"}}),
        encoding="utf-8",
    )

    if failure == "read":
        monkeypatch.setattr(
            owner,
            "_read_spool",
            lambda: (_ for _ in ()).throw(OSError("spool temporarily unreadable")),
        )
    else:
        monkeypatch.setattr(
            owner,
            "_set_lifecycle",
            lambda **_values: (_ for _ in ()).throw(OSError("lifecycle write failed")),
        )

    assert owner._verify_lock() is True
    assert owner._authority_lost is False
    assert json.loads(owner.spool_path.read_text())["lifecycle"]["ownership_state"] == "unreadable"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file mode bits, so nothing denies access")
def test_verify_lock_latches_on_a_replacement_inode_this_user_cannot_read(tmp_path):
    """Permission on the pathname must not mask a provable inode difference.

    The replacement here is a different inode whose mode denies this user,
    which is exactly what an access check reports as the recoverable
    "unreadable" case.  Identity has to be proven from stat first, or a stale
    owner keeps treating a replaced reservation as its own and goes on writing
    to it (finding-20260821-vbbc).
    """
    from spindle.namespace_owner_process import AuthorityLost

    owner = _lock_owner(tmp_path, spool_id="unreadable-replacement")
    original = owner.lock_path.stat()
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o000)
    assert owner.lock_path.stat().st_ino != original.st_ino
    assert os.access(owner.lock_path, os.R_OK | os.W_OK) is False

    with pytest.raises(AuthorityLost):
        owner._verify_lock()
    assert owner._authority_lost is True
    assert not owner.spool_path.exists(), "a proven replacement must not write a diagnostic marker"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory search permission")
def test_verify_lock_stays_recoverable_when_the_store_denies_search(tmp_path):
    """An unsearchable store proves nothing, so it is recoverable - not a crash.

    Probing the pathname raises EACCES here, and so does the diagnostic write
    that would record the sighting.  Neither may escape as an owner crash, and
    neither may be mistaken for a replacement: the owner has proven nothing
    about who holds the reservation, so service resumes when the directory is
    repaired (finding-20260821-vbbc).
    """
    store = tmp_path / "store"
    store.mkdir()
    owner = _lock_owner(store, spool_id="unsearchable-store")

    store.chmod(0o000)
    try:
        assert owner._verify_lock() is False
        assert owner._authority_lost is False
    finally:
        store.chmod(0o700)

    assert not owner.spool_path.exists(), "the diagnostic write cannot succeed and must not escape"
    assert owner._verify_lock() is True
    assert owner._authority_lost is False


def test_pre_popen_reverify_blocks_provider_after_authority_loss(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost, LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        watchdog_fd=None,
        stdin_path=None,
        command=["provider-must-not-run"],
        cwd=str(tmp_path),
        disable_pdeathsig=True,
    )
    owner.store = tmp_path
    owner.spool_id = "authority-before-provider"
    owner.stdout_path = tmp_path / "authority-before-provider.stdout"
    owner.stderr_path = tmp_path / "authority-before-provider.stderr"
    owner.wall_deadline_at = None
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner.provider = None
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)

    def losing_verify_lock():
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider executed after authority loss"),
    )

    with pytest.raises(AuthorityLost):
        owner._spawn_provider()


def test_run_reverifies_immediately_after_lock_acquisition_before_deadline_write(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AUTHORITY_LOST_EXIT_CODE, AuthorityLost, LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=100,
        poll_interval=0,
        watchdog_fd=None,
        disposition_fd=None,
        launch_barrier_fd=None,
    )
    owner.store = tmp_path
    owner.spool_id = "post-acquire-proof"
    owner.spool_path = tmp_path / "post-acquire-proof.json"
    owner.lock_path = tmp_path / "post-acquire-proof.process-owner"
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = SimpleNamespace(fd=123, close=lambda: None)
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._authority_lost = False
    owner._await_launch_barrier = lambda: True
    owner._read_spool = lambda: {"id": owner.spool_id, "status": "pending"}
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._contain_own_children = lambda _bound: True
    owner._ensure_wall_deadline = lambda: pytest.fail("deadline write ran before post-acquire authority proof")

    def losing_verify_lock():
        owner._authority_lost = True
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)

    assert owner.run() == AUTHORITY_LOST_EXIT_CODE


def test_capture_open_to_popen_reverify_blocks_provider_after_authority_loss(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import AuthorityLost

    owner = _spawning_owner(tmp_path, "capture-open-loss")
    verifications = []

    def losing_after_capture_open():
        verifications.append(len(verifications))
        if len(verifications) > 1:
            raise AuthorityLost()
        return True

    owner._verify_lock = losing_after_capture_open
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider executed after capture-open authority loss"),
    )

    try:
        with pytest.raises(AuthorityLost):
            owner._spawn_provider()
        assert owner.stdout_path.exists()
        assert owner.stderr_path.exists()
        assert owner.provider is None
    finally:
        owner.lock.close()


class _FailingClose:
    def __init__(self, name, fd):
        self.name = name
        self.fd = fd
        self.closed = False
        self.inheritable = None

    def fileno(self):
        return self.fd

    def set_inheritable(self, inheritable):
        self.inheritable = inheritable

    def close(self):
        self.closed = True
        raise OSError(f"simulated {self.name} close failure")


def _install_failing_pre_popen_closes(monkeypatch, stdin_path):
    owner_end = _FailingClose("owner_end", 41)
    child_end = _FailingClose("child_end", 42)
    stdin_stream = _FailingClose("stdin_stream", 43)

    monkeypatch.setattr("spindle.namespace_owner_process.socket.socketpair", lambda: (owner_end, child_end))

    def fake_open(path, mode="r", *args, **kwargs):
        assert path == str(stdin_path)
        assert mode == "r"
        assert not args
        assert not kwargs
        return stdin_stream

    monkeypatch.setattr(builtins, "open", fake_open)
    return owner_end, child_end, stdin_stream


def test_pre_popen_authority_loss_survives_failing_spawn_local_closes(tmp_path, monkeypatch):
    """The local pre-Popen cleanup cannot mask the in-flight authority latch."""
    from types import SimpleNamespace

    from spindle.namespace_owner_process import (
        AUTHORITY_LOST_DISPOSITION,
        AUTHORITY_LOST_EXIT_CODE,
        AuthorityLost,
        LogicalOwner,
    )
    from spindle.owner_watchdog import _classify_owner_termination

    stdin_path = tmp_path / "stdin"
    owner_end, child_end, stdin_stream = _install_failing_pre_popen_closes(monkeypatch, stdin_path)
    disposition_read, disposition_write = os.pipe()

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=None,
        poll_interval=0,
        watchdog_fd=None,
        disposition_fd=disposition_write,
        launch_barrier_fd=None,
        stdin_path=str(stdin_path),
        command=["provider-must-not-run"],
        cwd=str(tmp_path),
        disable_pdeathsig=True,
        ready_fd=None,
    )
    owner.store = tmp_path
    owner.spool_id = "pre-popen-authority-loss-close-failure"
    owner.spool_path = tmp_path / "pre-popen-authority-loss-close-failure.json"
    owner.owner_exit_path = tmp_path / "pre-popen-authority-loss-close-failure.owner-exit"
    owner.lock_path = tmp_path / "pre-popen-authority-loss-close-failure.process-owner"
    owner.stdout_path = tmp_path / "pre-popen-authority-loss-close-failure.stdout"
    owner.stderr_path = tmp_path / "pre-popen-authority-loss-close-failure.stderr"
    owner.wall_deadline_at = None
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = SimpleNamespace(close=lambda: None)
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._authority_lost = False
    owner._drain_reapable = lambda: 0
    owner._direct_children = lambda: []
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner.spool_path.write_text(json.dumps({"id": owner.spool_id, "status": "pending"}, sort_keys=True))

    verify_calls = []

    def losing_verify_lock():
        verify_calls.append(None)
        if len(verify_calls) == 1:
            return True
        owner._authority_lost = True
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider executed after authority loss"),
    )

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        assert child_end.closed is True
        assert stdin_stream.closed is True
        assert owner_end.closed is True
        assert os.read(disposition_read, 4096) == AUTHORITY_LOST_DISPOSITION
        assert (
            _classify_owner_termination(
                AUTHORITY_LOST_EXIT_CODE << 8,
                exception_reported=False,
                authority_lost=True,
            )
            == "authority_lost"
        )
        assert json.loads(owner.spool_path.read_text())["status"] == "pending"
        assert not owner.owner_exit_path.exists()
    finally:
        os.close(disposition_read)
        try:
            os.close(disposition_write)
        except OSError:
            pass


def test_pre_popen_local_close_failure_still_surfaces_without_authority_loss(tmp_path, monkeypatch):
    """Only an active AuthorityLost suppresses these local cleanup failures."""
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    stdin_path = tmp_path / "stdin"
    _owner_end, child_end, _stdin_stream = _install_failing_pre_popen_closes(monkeypatch, stdin_path)

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        watchdog_fd=None,
        stdin_path=str(stdin_path),
        command=["provider-must-not-run"],
        cwd=str(tmp_path),
        disable_pdeathsig=True,
    )
    owner.store = tmp_path
    owner.spool_id = "pre-popen-close-primary"
    owner.stdout_path = tmp_path / "pre-popen-close-primary.stdout"
    owner.stderr_path = tmp_path / "pre-popen-close-primary.stderr"
    owner.wall_deadline_at = None
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._verify_lock = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("primary failure"))
    owner.provider = None
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)

    with pytest.raises(OSError, match="simulated child_end close failure"):
        owner._spawn_provider()
    assert child_end.closed is True


def _spawning_owner(tmp_path, spool_id, *, ready_fd=None, command=("provider",)):
    """An owner wired far enough to drive ``_spawn_provider`` against fakes."""
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        watchdog_fd=None,
        stdin_path=None,
        command=list(command),
        cwd=str(tmp_path),
        disable_pdeathsig=True,
        ready_fd=ready_fd,
        poll_interval=0,
    )
    owner.store = tmp_path
    owner.spool_id = spool_id
    owner.generation = 1
    owner.spool_path = tmp_path / f"{spool_id}.json"
    owner.spool_lock_path = tmp_path / f"{spool_id}.lock"
    owner.stdout_path = tmp_path / f"{spool_id}.stdout"
    owner.stderr_path = tmp_path / f"{spool_id}.stderr"
    owner.process_identity_path = tmp_path / f"{spool_id}.process-identity"
    owner.owner_exit_path = tmp_path / f"{spool_id}.owner-exit"
    owner.exit_path = tmp_path / f"{spool_id}.exit"
    owner.lock_path = tmp_path / f"{spool_id}.process-owner"
    owner.wall_deadline_at = None
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.adopted_reaped = 0
    owner.episode_mode = False
    owner._authority_lost = False
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner.lock = acquire_ownership_lock(owner.lock_path)
    owner.spool_path.write_text(json.dumps({"id": spool_id, "status": "pending"}, sort_keys=True))
    return owner


class _FakeProvider:
    pid = 4242


def _fake_provider_start(monkeypatch, *, spawn_error=None):
    """Replace Popen and the identity/pidfd syscalls it feeds, recording calls."""
    started = []

    def fake_popen(*args, **kwargs):
        started.append((args, kwargs))
        if spawn_error is not None:
            raise spawn_error
        return _FakeProvider()

    monkeypatch.setattr("spindle.namespace_owner_process.subprocess.Popen", fake_popen)
    monkeypatch.setattr("spindle.namespace_owner_process.os.getpgid", lambda _pid: _FakeProvider.pid)
    monkeypatch.setattr("spindle.namespace_owner_process._starttime", lambda _pid: "test-start")
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.pidfd_open",
        lambda _pid: (_ for _ in ()).throw(OSError("pidfd unavailable")),
        raising=False,
    )
    return started


def test_provider_start_persists_the_observed_process_group(tmp_path, monkeypatch):
    owner = _spawning_owner(tmp_path, "observed-provider-pgid")
    started = _fake_provider_start(monkeypatch)
    observed_pgid = _FakeProvider.pid + 17
    monkeypatch.setattr("spindle.namespace_owner_process.os.getpgid", lambda pid: observed_pgid)

    try:
        assert owner._spawn_provider() is None
        assert len(started) == 1
        assert owner.provider_pgid == observed_pgid
        process_identity = json.loads(owner.process_identity_path.read_text())
        assert process_identity["provider_pgid"] == observed_pgid
        assert json.loads(owner.spool_path.read_text())["provider_process_group_id"] == observed_pgid
    finally:
        owner.control.close()
        owner.lock.close()


@pytest.mark.parametrize("observed", [OSError("getpgid failed"), 0, -4242, 2**31, True])
def test_unverified_provider_group_uses_only_bounded_child_custody(tmp_path, monkeypatch, observed):
    from spindle.namespace_owner_process import CONTAINMENT_BOUND_SECONDS

    owner = _spawning_owner(tmp_path, "unverified-provider-pgid")
    _fake_provider_start(monkeypatch)
    custody_passes = []
    owner._contain_own_children = lambda bound: custody_passes.append(bound) or True
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.getpgid",
        lambda _pid: (_ for _ in ()).throw(observed) if isinstance(observed, OSError) else observed,
    )
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.killpg",
        lambda *_args: pytest.fail("an unverified provider group was signalled"),
    )

    try:
        with pytest.raises(RuntimeError, match="process group could not be verified"):
            owner._spawn_provider()
        assert owner.provider_pgid is None
        assert custody_passes == [CONTAINMENT_BOUND_SECONDS]
        assert owner.control is None
        assert not owner.process_identity_path.exists()
        assert json.loads(owner.spool_path.read_text())["status"] == "pending"
    finally:
        owner.lock.close()


def test_pre_popen_reverify_retries_recoverable_unreadable_before_provider_start(tmp_path, monkeypatch):
    owner = _spawning_owner(tmp_path, "unreadable-before-provider")

    # One recoverable unreadable sighting, then a provable claim for the
    # pre-Popen check and for every post-fork publication boundary.
    verifications = []

    def counted_verify_lock():
        verifications.append(len(verifications))
        return len(verifications) > 1

    owner._verify_lock = counted_verify_lock
    monkeypatch.setattr("spindle.namespace_owner_process.time.sleep", lambda _seconds: None)
    started = _fake_provider_start(monkeypatch)

    try:
        assert owner._spawn_provider() is None
        assert len(started) == 1
        assert owner.provider is not None
        assert owner.process_identity_path.exists()
    finally:
        owner.control.close()
        owner.lock.close()


def test_post_fork_reverify_blocks_every_publication_after_authority_loss(tmp_path, monkeypatch):
    """A loss proven after the fork publishes nothing at all.

    The pre-Popen proof is already stale once the provider exists, and the
    reservation can be replaced in exactly that window.  Without this
    reverify the stale owner writes its own process identity, spool record
    and readiness handshake over the generation that replaced it
    (finding-20260821-ac82).
    """
    from spindle.namespace_owner_process import AuthorityLost

    ready_read, ready_write = os.pipe()
    owner = _spawning_owner(tmp_path, "post-fork-loss", ready_fd=ready_write)
    before = owner.spool_path.read_bytes()
    verifications = []

    def losing_verify_lock():
        verifications.append(len(verifications))
        if len(verifications) > 2:
            raise AuthorityLost()
        return True

    owner._verify_lock = losing_verify_lock
    started = _fake_provider_start(monkeypatch)

    try:
        with pytest.raises(AuthorityLost):
            owner._spawn_provider()
        assert len(started) == 1, "the fork itself is not what this blocks"
        assert not owner.process_identity_path.exists()
        assert owner.spool_path.read_bytes() == before
        os.set_blocking(ready_read, False)
        with pytest.raises(BlockingIOError):
            os.read(ready_read, 4096)
    finally:
        owner.control.close()
        owner.lock.close()
        os.close(ready_read)
        os.close(ready_write)


def test_post_fork_publication_proves_authority_at_every_boundary(tmp_path, monkeypatch):
    """Each shared publication after the fork is preceded by its own proof."""
    ready_read, ready_write = os.pipe()
    owner = _spawning_owner(tmp_path, "post-fork-boundaries", ready_fd=ready_write)
    events = []
    real_atomic_write = spindle.namespace_owner_process._atomic_json_write

    def traced_atomic_write(path, payload):
        suffix = Path(path).suffix.lstrip(".")
        events.append(f"write:{'spool' if suffix == 'json' else suffix}")
        return real_atomic_write(path, payload)

    owner._verify_lock = lambda **_kwargs: events.append("verify") or True
    monkeypatch.setattr("spindle.namespace_owner_process._atomic_json_write", traced_atomic_write)
    _fake_provider_start(monkeypatch)

    try:
        assert owner._spawn_provider() is None
        # The trailing verify is the readiness boundary; the handshake below
        # is the only thing published after it.
        assert events == [
            "verify",
            "verify",
            "verify",
            "write:process-identity",
            "verify",
            "write:spool",
            "verify",
        ]
        assert json.loads(os.read(ready_read, 4096))["provider_pid"] == _FakeProvider.pid
    finally:
        owner.control.close()
        owner.lock.close()
        os.close(ready_read)


def test_episode_acceptance_rechecks_authority_after_episode_read(tmp_path, monkeypatch):
    from types import MethodType

    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "episode-acceptance-read-loss")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    contenders = []
    replaced = False
    real_read_spool = owner._read_spool

    def replacing_read_spool(self):
        nonlocal replaced
        record = real_read_spool()
        if not replaced and self.process_identity_path.exists():
            contenders.append(_replace_and_hold_owner_lock(self))
            replaced = True
        return record

    owner._read_spool = MethodType(replacing_read_spool, owner)
    _fake_provider_start(monkeypatch)

    try:
        with pytest.raises(AuthorityLost):
            owner._spawn_provider()
        assert owner.process_identity_path.exists()
        after = json.loads(owner.spool_path.read_text())
        assert after["owner_episode"]["phase"] == "lock_bound"
        assert after["status"] == "pending"
    finally:
        for contender in contenders:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        if owner.control is not None:
            owner.control.close()
        owner.lock.close()


def test_episode_acceptance_rechecks_authority_before_episode_read(tmp_path, monkeypatch):
    from types import MethodType, SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "episode-acceptance-pre-read-loss")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    contenders = []
    real_read_spool = owner._read_spool

    def checkpoint(name, *_args):
        if name == "process_identity_published":
            contenders.append(_replace_and_hold_owner_lock(owner))

    def guarded_read_spool(self):
        if self.process_identity_path.exists():
            pytest.fail("owner read shared episode state after authority loss")
        return real_read_spool()

    owner.checkpoints = SimpleNamespace(reach=checkpoint, socket=None, pause_name=None)
    owner._read_spool = MethodType(guarded_read_spool, owner)
    _fake_provider_start(monkeypatch)

    try:
        with pytest.raises(AuthorityLost):
            owner._spawn_provider()
        assert owner.process_identity_path.exists()
        after = json.loads(owner.spool_path.read_text())
        assert after["owner_episode"]["phase"] == "lock_bound"
        assert after["status"] == "pending"
    finally:
        for contender in contenders:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        if owner.control is not None:
            owner.control.close()
        owner.lock.close()


def test_pre_popen_deadline_settlement_treats_an_unreadable_store_as_retryable(tmp_path):
    """The recoverable class is unchanged: nothing is published, and it retries."""
    from spindle.namespace_owner_process import SETTLEMENT_RETRY

    owner = _spawning_owner(tmp_path, "unreadable-deadline")
    owner._verify_lock = lambda **_kwargs: False

    try:
        assert owner._settle_deadline_expiry_after_binding() == SETTLEMENT_RETRY
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock.close()


def test_pre_popen_deadline_settlement_converges_on_an_unsettleable_episode(tmp_path, monkeypatch):
    """A rejection no retry can clear converges instead of holding the lock.

    The settlement failed identically whether the store was momentarily
    unreadable or the episode had moved somewhere this generation can never
    transition from.  Retrying the second forever kept an expired reservation
    open with nothing ever published (finding-20260821-a01s).
    """
    from spindle.namespace_owner_process import EPISODE_UNSETTLEABLE_EXIT_CODE, SETTLEMENT_UNSETTLEABLE

    owner = _spawning_owner(tmp_path, "unsettleable-deadline")
    owner.episode_mode = True
    owner._verify_lock = lambda **_kwargs: True
    owner._deadline_expired = lambda: True
    attempts = []

    def rejecting_transition(*_args, **kwargs):
        attempts.append(kwargs["destination"])
        return EpisodeTransitionResult(False, "illegal_transition", {})

    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", rejecting_transition)
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider started after an expired deadline"),
    )

    try:
        assert owner._settle_deadline_expiry_after_binding() == SETTLEMENT_UNSETTLEABLE
        assert owner._spawn_provider() == EPISODE_UNSETTLEABLE_EXIT_CODE
        # One attempt per call above - the expired deadline converges rather
        # than re-attempting a rejection that can never change.
        assert attempts == ["cleanup_proven", "cleanup_proven"]
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock.close()


def test_pre_popen_deadline_settlement_retries_a_revision_another_actor_moved(tmp_path, monkeypatch):
    """The one rejection a re-read can clear still converges on a durable timeout."""
    owner = _spawning_owner(tmp_path, "revision-race-deadline")
    owner.episode_mode = True
    owner._verify_lock = lambda **_kwargs: True
    owner._deadline_expired = lambda: True
    outcomes = iter(
        [
            EpisodeTransitionResult(False, "stale_revision", {}),
            EpisodeTransitionResult(True, None, {}),
        ]
    )
    monkeypatch.setattr(
        "spindle.namespace_owner_process.transition_owner_episode",
        lambda *_args, **_kwargs: next(outcomes),
    )
    monkeypatch.setattr(
        "spindle.namespace_owner_process.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("provider started after an expired deadline"),
    )

    try:
        assert owner._spawn_provider() == 124
        assert next(outcomes, "exhausted") == "exhausted"
        evidence = json.loads(owner.owner_exit_path.read_text())
        assert evidence["cleanup_outcome"] == "deadline_expired_before_provider_start"
    finally:
        owner.lock.close()


@pytest.mark.parametrize(
    ("rejection", "expected"),
    [
        ("stale_revision", "retry"),
        ("stale_generation", "unsettleable"),
        ("illegal_transition", "unsettleable"),
        ("illegal_actor", "unsettleable"),
        ("missing_facts", "unsettleable"),
        ("contradictory_facts", "unsettleable"),
        ("unknown_episode_format", "unsettleable"),
        (None, "unsettleable"),
    ],
)
def test_only_a_moved_revision_is_worth_retrying(rejection, expected):
    """Every rejection a fresh read cannot change has to converge instead."""
    from spindle.namespace_owner_process import _settlement_outcome_for

    assert _settlement_outcome_for(rejection) == expected


def test_provider_spawn_failure_retries_through_a_transient_unreadable_store(tmp_path, monkeypatch):
    """A spawn failure plus a momentarily unreadable lock is not a crash.

    The settlement was attempted exactly once and a False return became a
    RuntimeError, so a transient sighting turned a durable
    provider_spawn_failure into an owner crash (finding-20260821-o0h1).
    """
    owner = _spawning_owner(tmp_path, "spawn-failure-retry")
    # The two pre-Popen proofs, then two unreadable settlement sightings, then
    # the readable settlement plus both sidecar proofs.
    results = iter([True, True, False, False, True, True, True])
    owner._verify_lock = lambda **_kwargs: next(results)
    _fake_provider_start(monkeypatch, spawn_error=OSError("exec format error"))

    try:
        assert owner._spawn_provider() == 127
        assert next(results, "exhausted") == "exhausted"
        settled = owner._read_spool()
        assert settled["status"] == "error"
        assert settled["error_kind"] == "provider_spawn_failure"
        evidence = json.loads(owner.owner_exit_path.read_text())
        assert evidence["cleanup_outcome"] == "provider_spawn_failed"
    finally:
        owner.lock.close()


def test_provider_spawn_failure_converges_when_the_episode_cannot_be_settled(tmp_path, monkeypatch):
    """An unsettleable episode ends the spawn-failure path instead of spinning."""
    from spindle.namespace_owner_process import EPISODE_UNSETTLEABLE_EXIT_CODE

    owner = _spawning_owner(tmp_path, "spawn-failure-unsettleable")
    owner.episode_mode = True
    owner._verify_lock = lambda **_kwargs: True
    attempts = []

    def rejecting_transition(*_args, **_kwargs):
        attempts.append(None)
        return EpisodeTransitionResult(False, "stale_generation", {})

    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", rejecting_transition)
    _fake_provider_start(monkeypatch, spawn_error=OSError("exec format error"))

    try:
        assert owner._spawn_provider() == EPISODE_UNSETTLEABLE_EXIT_CODE
        assert len(attempts) == 1
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock.close()


def test_provider_spawn_failure_defers_to_a_proven_authority_loss(tmp_path, monkeypatch):
    """A replaced reservation outranks publishing this generation's failure."""
    from spindle.namespace_owner_process import AuthorityLost

    owner = _spawning_owner(tmp_path, "spawn-failure-authority-loss")
    verifications = []

    def losing_verify_lock():
        verifications.append(len(verifications))
        if len(verifications) > 1:
            raise AuthorityLost()
        return True

    owner._verify_lock = losing_verify_lock
    _fake_provider_start(monkeypatch, spawn_error=OSError("exec format error"))

    try:
        with pytest.raises(AuthorityLost):
            owner._spawn_provider()
        assert not owner.owner_exit_path.exists()
        assert owner._read_spool()["status"] == "pending"
    finally:
        owner.lock.close()


def test_replacement_between_exit_carriers_blocks_generation_blind_exit_file(tmp_path):
    from types import MethodType

    from spindle.namespace_owner_process import AuthorityLost

    owner = _spawning_owner(tmp_path, "exit-carrier-replacement")
    owner.provider = _FakeProvider()
    contender = None
    real_write = owner._atomic_json_write_after_authority_proof

    def write_sidecar_then_replace(self, path, value):
        nonlocal contender
        real_write(path, value)
        contender = _replace_and_hold_owner_lock(self)

    owner._atomic_json_write_after_authority_proof = MethodType(write_sidecar_then_replace, owner)

    try:
        with pytest.raises(AuthorityLost):
            owner._write_exit_evidence(17, cleanup_outcome="natural_exit")
        assert owner.owner_exit_path.exists()
        assert not owner.exit_path.exists()
        assert not list(tmp_path.glob(".exit-carrier-replacement.exit.*.tmp"))
    finally:
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_prebinding_deadline_rejection_does_not_publish_owner_exit(tmp_path, monkeypatch):
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "prebinding-deadline-rejected")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )

    monkeypatch.setattr(
        "spindle.namespace_owner_process.transition_owner_episode",
        lambda *_args, **_kwargs: EpisodeTransitionResult(False, "illegal_transition", {}),
    )

    try:
        owner._settle_deadline_expiry_before_binding()
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock.close()


def test_handle_request_reproves_inside_blocked_mailbox_guard_before_accepting(tmp_path):
    from spindle.namespace_owner import read_control_receipt
    from spindle.namespace_owner_process import AuthorityLost

    owner = _spawning_owner(tmp_path, "blocked-mailbox-accept")
    owner.provider = _FakeProvider()
    request = create_control_request(tmp_path, owner.spool_id, "cancel", owner.generation, "test")
    guard_path = tmp_path / f"{owner.spool_id}.journal-guard"
    guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    fcntl.flock(guard_fd, fcntl.LOCK_EX)
    observed = threading.Event()
    errors = []
    results = []

    def checkpoint(name, *_args):
        if name == "control_observed_before_ack":
            observed.set()

    owner.checkpoints = type(
        "Checkpoints", (), {"reach": staticmethod(checkpoint), "socket": None, "pause_name": None}
    )()

    def handle():
        try:
            results.append(owner._handle_request(request))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=handle)
    worker.start()
    contender = None
    try:
        assert observed.wait(timeout=2)
        worker.join(timeout=0.05)
        assert worker.is_alive(), "owner did not block on the mailbox guard"
        contender = _replace_and_hold_owner_lock(owner)
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], AuthorityLost)
        assert read_control_receipt(tmp_path, owner.spool_id, request.request_id) is None
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(guard_fd)
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_handle_request_reproves_after_receipt_settlement_before_episode_ack(tmp_path, monkeypatch):
    from spindle.namespace_owner import read_control_receipt
    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "post-receipt-accept-replacement")
    owner.episode_mode = True
    owner.provider = _FakeProvider()
    owner._reported_malformed_receipts = set()
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "running",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    request = create_control_request(tmp_path, owner.spool_id, "cancel", owner.generation, "test")
    real_settle = owner._settle_other_requests_unlocked
    contender = None

    def settle_then_replace(*args, **kwargs):
        nonlocal contender
        result = real_settle(*args, **kwargs)
        contender = _replace_and_hold_owner_lock(owner)
        return result

    def forbidden_transition(*_args, **_kwargs):
        pytest.fail("owner acknowledged the episode after post-receipt authority loss")

    owner._settle_other_requests_unlocked = settle_then_replace
    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", forbidden_transition)

    try:
        with pytest.raises(AuthorityLost):
            owner._handle_request(request)
        receipt = read_control_receipt(tmp_path, owner.spool_id, request.request_id)
        assert receipt is not None
        assert receipt.cleanup_outcome == "accepted"
        assert receipt.owner_acknowledged_at is not None
        assert owner._read_spool()["owner_episode"]["phase"] == "lock_bound"
    finally:
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_owner_timeout_request_reproves_immediately_before_create(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AUTHORITY_LOST_EXIT_CODE, AuthorityLost, LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=100,
        poll_interval=0,
        watchdog_fd=None,
        disposition_fd=None,
        launch_barrier_fd=None,
    )
    owner.store = tmp_path
    owner.spool_id = "timeout-create-proof"
    owner.spool_path = tmp_path / "timeout-create-proof.json"
    owner.lock_path = tmp_path / "timeout-create-proof.process-owner"
    owner.provider = _FakeProvider()
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = SimpleNamespace(fd=123, close=lambda: None)
    owner.clock = SimpleNamespace(monotonic=iter([0.0, 101.0]).__next__)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._authority_lost = False
    owner.wall_deadline_at = "2026-08-22T00:00:00+00:00"
    owner._await_launch_barrier = lambda: True
    owner._read_spool = lambda: {"id": owner.spool_id, "status": "running"}
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._spawn_provider = lambda **_kwargs: None
    owner._terminal_output_state = lambda: (False, False, None)
    owner._next_current_request = lambda: None
    owner._contain_own_children = lambda _bound: True
    verifications = []

    def losing_before_timeout_create():
        verifications.append(len(verifications))
        if len(verifications) >= 3:
            owner._authority_lost = True
            raise AuthorityLost()
        return True

    owner._verify_lock = losing_before_timeout_create
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)
    monkeypatch.setattr(
        "spindle.namespace_owner_process.create_control_request",
        lambda *_args, **_kwargs: pytest.fail("timeout request was created after authority loss"),
    )

    assert owner.run() == AUTHORITY_LOST_EXIT_CODE


def test_spawn_failure_sidecar_reproves_after_transition_before_owner_exit(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "spawn-failure-sidecar-loss")
    owner.episode_mode = True
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "pending",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    verifications = []

    def losing_after_transition(**_kwargs):
        verifications.append(len(verifications))
        if len(verifications) >= 4:
            raise AuthorityLost()
        return True

    owner._verify_lock = losing_after_transition

    try:
        with pytest.raises(AuthorityLost):
            owner._settle_provider_spawn_failure(OSError("exec failed"))
        assert owner._read_spool()["owner_episode"]["phase"] == "cleanup_proven"
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock.close()


def test_watchdog_loss_sidecar_reproves_after_cleanup_transition(tmp_path, monkeypatch):
    import spindle.namespace_owner_process as owner_module
    from spindle.namespace_owner_process import DESCENDANTS_SETTLED, AuthorityLost
    from tests.owner_episode_fixtures import make_episode

    owner = _spawning_owner(tmp_path, "watchdog-loss-sidecar-replacement")
    owner.episode_mode = True
    owner.provider = None
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "running",
                "owner_episode": make_episode("lock_bound", generation=owner.generation),
            },
            sort_keys=True,
        )
    )
    owner._settle_descendants = lambda **_kwargs: DESCENDANTS_SETTLED
    real_transition = owner_module.transition_owner_episode
    contender = None

    def transition_then_replace(*args, **kwargs):
        nonlocal contender
        result = real_transition(*args, **kwargs)
        contender = _replace_and_hold_owner_lock(owner)
        return result

    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", transition_then_replace)

    try:
        with pytest.raises(AuthorityLost):
            owner._contain_after_watchdog_loss()
        assert owner._read_spool()["owner_episode"]["phase"] == "cleanup_proven"
        assert not owner.owner_exit_path.exists()
    finally:
        if contender is not None:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        owner.lock.close()


def test_settle_descendants_reports_unproven_authority_without_killing_or_writing(tmp_path, monkeypatch):
    from spindle.namespace_owner_process import DESCENDANTS_UNPROVEN

    owner = _spawning_owner(tmp_path, "descendant-unproven")
    owner.provider = None
    owner._direct_children = lambda: [987654]
    owner._drain_reapable = lambda: 0
    owner._verify_lock = lambda **_kwargs: False
    monkeypatch.setattr("spindle.namespace_owner_process.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.kill",
        lambda *_args: pytest.fail("unproven authority attempted descendant mutation"),
    )

    try:
        assert owner._settle_descendants(force=True, grace=0) == DESCENDANTS_UNPROVEN
        assert not owner.spool_path.exists() or "descendants_survived" not in owner.spool_path.read_text()
    finally:
        owner.lock.close()


def test_terminal_output_state_reproves_before_timestamp_mutation(tmp_path):
    from spindle.namespace_owner_process import LogicalOwner

    owner = _spawning_owner(tmp_path, "terminal-output-loss")
    owner.spool_path.write_text(
        json.dumps({"id": owner.spool_id, "status": "running", "harness": "gemini"}, sort_keys=True)
    )
    owner.stdout_path.write_text(json.dumps({"response": "complete", "session_id": "s"}) + "\n")
    before = owner.spool_path.read_text()
    owner._verify_lock = lambda **_kwargs: False

    try:
        assert LogicalOwner._terminal_output_state(owner) == (False, False, None)
        assert owner.spool_path.read_text() == before
    finally:
        owner.lock.close()


def test_terminal_output_state_existing_timestamps_remain_a_pure_read(tmp_path):
    owner = _spawning_owner(tmp_path, "terminal-output-pure-read")
    written_at = datetime.now().astimezone().isoformat()
    owner.spool_path.write_text(
        json.dumps(
            {
                "id": owner.spool_id,
                "status": "running",
                "harness": "gemini",
                "output_complete_detected_at": written_at,
                "output_complete_written_at": written_at,
            },
            sort_keys=True,
        )
    )
    owner.stdout_path.write_text(json.dumps({"response": "complete", "session_id": "s"}) + "\n")
    owner._verify_lock = lambda **_kwargs: pytest.fail("pure terminal-output read requested an authority proof")

    try:
        terminal, _shutdown_due, observed_written = owner._terminal_output_state()
        assert terminal is True
        assert observed_written is not None
    finally:
        owner.lock.close()


def test_bounded_custody_pass_uses_only_live_kernel_children(tmp_path, monkeypatch):
    """Containment after the latch signals current custody, never persisted ids.

    A PID the kernel still lists as this process's unreaped child cannot have
    been recycled onto another program, which is what makes it safe to signal
    when the recorded provider PID and PGID no longer are.  Descendants that
    reparent onto the subreaper mid-pass are picked up by a later scan.
    """
    owner = _spawning_owner(tmp_path, "bounded-custody")
    owner.provider = _FakeProvider()
    owner.provider_pgid = _FakeProvider.pid
    scans = iter([[41, 42], [43], [], [], []])
    owner._direct_children = lambda: next(scans, [])
    owner._drain_reapable = lambda: 0
    killed = []
    monkeypatch.setattr("spindle.namespace_owner_process.os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.killpg",
        lambda *_args: pytest.fail("containment signalled a persisted process group"),
    )

    try:
        assert owner._contain_own_children(5.0) is True
        assert killed == [(41, signal.SIGKILL), (42, signal.SIGKILL), (43, signal.SIGKILL)]
    finally:
        owner.lock.close()


def test_bounded_custody_pass_exits_even_when_children_never_clear(tmp_path, monkeypatch):
    """One bounded attempt, not the crash path's retry-until-clean loop."""
    owner = _spawning_owner(tmp_path, "unfinished-custody")
    owner._direct_children = lambda: [51]
    owner._drain_reapable = lambda: 0
    monkeypatch.setattr("spindle.namespace_owner_process.os.kill", lambda _pid, _sig: None)

    try:
        assert owner._contain_own_children(0.05) is False
    finally:
        owner.lock.close()


def test_authority_loss_finalize_takes_one_bounded_pass_and_exits_anyway(tmp_path):
    """The owner contains its own subtree before exiting, and exits regardless.

    A lost watchdog adopts nothing and PDEATHSIG is optional, so a combined
    loss would otherwise strand the provider and its setsid descendants
    (finding-20260821-ikzy).  The pass is bounded, taken once, and the owner
    converges even when it does not finish.
    """
    from types import SimpleNamespace

    from spindle.namespace_owner_process import (
        AUTHORITY_LOST_EXIT_CODE,
        CONTAINMENT_BOUND_SECONDS,
        LogicalOwner,
    )

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(watchdog_fd=None, disposition_fd=None)
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = None
    passes = []
    owner._contain_own_children = lambda bound: passes.append(bound) or False

    assert owner._finalize_authority_lost() == AUTHORITY_LOST_EXIT_CODE
    assert passes == [CONTAINMENT_BOUND_SECONDS]


def test_authority_loss_survives_failing_local_closes_in_the_owner_loop(tmp_path, monkeypatch):
    """A failing descriptor close must not replace the in-flight latch.

    ``_run``'s teardown closes the provider pidfd and control socket while an
    AuthorityLost may be unwinding through it.  An OSError raised there used
    to replace the latch, so ``run()`` never reached the finalizer, the
    disposition was never reported, and the watchdog read the escaping error
    as an owner crash - publishing crash evidence over a reservation this
    process no longer owns (finding-20260821-g7i0).
    """
    from types import SimpleNamespace

    from spindle.namespace_owner_process import (
        AUTHORITY_LOST_DISPOSITION,
        AUTHORITY_LOST_EXIT_CODE,
        AuthorityLost,
        LogicalOwner,
    )

    class FailingControl:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            raise OSError("simulated control close failure")

    disposition_read, disposition_write = os.pipe()
    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=None,
        poll_interval=0,
        watchdog_fd=None,
        disposition_fd=disposition_write,
    )
    owner.store = tmp_path
    owner.spool_id = "authority-loss-close-failure"
    owner.spool_path = tmp_path / "authority-loss-close-failure.json"
    owner.lock_path = tmp_path / "authority-loss-close-failure.process-owner"
    owner.provider = None
    # A descriptor number the kernel refuses, so the close fails for real
    # instead of through a patched os.close.
    owner.provider_pidfd = 987654321
    control = FailingControl()
    owner.control = control
    owner.lock = SimpleNamespace(close=lambda: None)
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._authority_lost = False
    owner._drain_reapable = lambda: 0
    owner._direct_children = lambda: []
    owner._await_launch_barrier = lambda: True
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda **_kwargs: None

    def losing_verify_lock():
        owner._authority_lost = True
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        assert control.closed is True, "the injected close never ran, so nothing was exercised"
        assert owner.control is None, "the failing close still clears the local reference"
        assert owner.provider_pidfd is None
        assert os.read(disposition_read, 4096) == AUTHORITY_LOST_DISPOSITION
    finally:
        os.close(disposition_read)
        try:
            os.close(disposition_write)
        except OSError:
            pass


def test_authority_loss_finalize_closes_local_resources_without_shared_writes(tmp_path, monkeypatch):
    """Everything the required invariant forbids past the latch is exercised.

    No mailbox guard, no episode/store write, and no signal by this
    process's own PID/PGID bookkeeping - only local descriptor and lock
    closing plus the private disposition report to the watchdog.
    """
    from types import SimpleNamespace

    from spindle.namespace_owner_process import (
        AUTHORITY_LOST_DISPOSITION,
        AUTHORITY_LOST_EXIT_CODE,
        AuthorityLost,
        LogicalOwner,
    )

    class TrackingLock:
        fd = -1

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            raise OSError("simulated local lock-close failure")

    disposition_read, disposition_write = os.pipe()
    watchdog_read, watchdog_write = os.pipe()
    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(
        timeout=None,
        poll_interval=0,
        watchdog_fd=watchdog_read,
        disposition_fd=disposition_write,
    )
    owner.store = tmp_path
    owner.spool_id = "authority-loss-finalize"
    owner.provider = None
    owner.provider_pidfd = None
    owner.control = None
    owner.lock = TrackingLock()
    owner.clock = SimpleNamespace(monotonic=lambda: 0.0)
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner._authority_lost = False
    owner._drain_reapable = lambda: 0
    # The bounded custody pass is real here; only the kernel scan is stubbed,
    # so this test never signals a child of the process running it.
    owner._direct_children = lambda: []
    owner._await_launch_barrier = lambda: True
    owner._watchdog_alive = lambda: True
    owner._deadline_expired = lambda: False
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda **_kwargs: None

    def losing_verify_lock():
        owner._authority_lost = True
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock

    def forbidden(*_args, **_kwargs):
        raise AssertionError("authority-loss teardown touched the shared store")

    owner._settle_other_requests_unlocked = forbidden
    monkeypatch.setattr("spindle.namespace_owner_process._set_subreaper", lambda: None)
    monkeypatch.setattr("spindle.namespace_owner_process.acquire_ownership_lock", lambda _path: owner.lock)
    monkeypatch.setattr("spindle.namespace_owner_process.mailbox_guard", forbidden)
    monkeypatch.setattr("spindle.namespace_owner_process.transition_owner_episode", forbidden)
    owner.lock_path = tmp_path / "authority-loss-finalize.process-owner"

    try:
        assert owner.run() == AUTHORITY_LOST_EXIT_CODE
        assert owner.lock.closed is True
        assert owner.args.watchdog_fd is None
        assert owner.args.disposition_fd is None
        # The disposition is published even though the later local lock close
        # raised, and _finalize_authority_lost already closed both pipe fds.
        assert os.read(disposition_read, 4096) == AUTHORITY_LOST_DISPOSITION
    finally:
        os.close(disposition_read)
        os.close(watchdog_write)
        try:
            os.close(watchdog_read)
        except OSError:
            pass
        try:
            os.close(disposition_write)
        except OSError:
            pass


def test_watchdog_loss_combined_with_authority_loss_does_not_loop_forever(monkeypatch):
    """The pre-fix bug: refusing dead authority forever instead of converging.

    ``_contain_after_watchdog_loss_until_proven`` retries
    ``_contain_after_watchdog_loss`` until it proves cleanup - exactly the
    shape that looped forever in finding-20260820-540u once ``_verify_lock``
    kept returning False every pass.  A latch that raises instead breaks the
    retry loop immediately, even while containment itself has not converged.
    """
    from types import SimpleNamespace

    from spindle.namespace_owner_process import AuthorityLost, LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(poll_interval=0)
    owner.provider = None
    owner._settle_descendants = lambda **_kwargs: False

    def losing_verify_lock():
        raise AuthorityLost()

    owner._verify_lock = losing_verify_lock
    monkeypatch.setattr("spindle.namespace_owner_process.time.sleep", lambda _seconds: None)

    with pytest.raises(AuthorityLost):
        owner._contain_after_watchdog_loss_until_proven()


def test_owner_does_not_exit_after_watchdog_loss_until_containment_is_proven(monkeypatch):
    from types import SimpleNamespace

    from spindle.namespace_owner_process import LogicalOwner

    owner = object.__new__(LogicalOwner)
    owner.args = SimpleNamespace(poll_interval=0)
    outcomes = iter((False, False, True))
    attempts = []
    owner._contain_after_watchdog_loss = lambda: attempts.append("contain") or next(outcomes)
    monkeypatch.setattr("spindle.namespace_owner_process.time.sleep", lambda _seconds: None)

    owner._contain_after_watchdog_loss_until_proven()

    assert attempts == ["contain", "contain", "contain"]


@pytest.mark.parametrize(
    "lock,liveness,exit_evidence,legacy,result",
    [
        (LockEvidence("held", 1, 2), LivenessEvidence("unverifiable", "namespace_mismatch"), True, None, "active"),
        (LockEvidence("held", 1, 2), LivenessEvidence("alive", "pidfd_live"), False, None, "active"),
        (LockEvidence("released", 1, 2), LivenessEvidence("dead", "pidfd_exited"), True, None, "terminalizable"),
        (LockEvidence("released", 1, 2), LivenessEvidence("dead", "pidfd_exited"), False, None, "unverifiable"),
        (LockEvidence("unreadable"), LivenessEvidence("alive", "pidfd_live"), False, None, "store_unhealthy"),
        (LockEvidence("identity_mismatch"), LivenessEvidence("dead", "pidfd_exited"), True, None, "store_unhealthy"),
        (
            LockEvidence("absent_legacy"),
            LivenessEvidence("unverifiable", "namespace_mismatch"),
            False,
            None,
            "unverifiable",
        ),
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
