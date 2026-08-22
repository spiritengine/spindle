"""S2-U: contract tests for namespace-safe shared owner primitives."""

from __future__ import annotations

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
    EpisodeTransitionResult,
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
    owner.checkpoints = SimpleNamespace(reach=lambda *_args: None, socket=None, pause_name=None)
    owner.lock = TrackingLock()
    owner._authority_lost = False
    owner._await_launch_barrier = lambda: True
    owner._allocate_generation = lambda: None
    owner._ensure_wall_deadline = lambda: None
    owner._remaining_wall_budget = lambda: None
    owner._publish_owner_identity = lambda: None
    owner._spawn_provider = lambda **_kwargs: None
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
    owner.lock = acquire_ownership_lock(owner.lock_path)
    owner._authority_lost = False
    return owner


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

    def losing_verify_lock():
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
    owner._verify_lock = lambda: (_ for _ in ()).throw(RuntimeError("primary failure"))
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
    monkeypatch.setattr("spindle.namespace_owner_process._starttime", lambda _pid: "test-start")
    monkeypatch.setattr(
        "spindle.namespace_owner_process.os.pidfd_open",
        lambda _pid: (_ for _ in ()).throw(OSError("pidfd unavailable")),
        raising=False,
    )
    return started


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
        if len(verifications) > 1:
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

    owner._verify_lock = lambda: events.append("verify") or True
    monkeypatch.setattr("spindle.namespace_owner_process._atomic_json_write", traced_atomic_write)
    _fake_provider_start(monkeypatch)

    try:
        assert owner._spawn_provider() is None
        # The trailing verify is the readiness boundary; the handshake below
        # is the only thing published after it.
        assert events == [
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


def test_pre_popen_deadline_settlement_treats_an_unreadable_store_as_retryable(tmp_path):
    """The recoverable class is unchanged: nothing is published, and it retries."""
    from spindle.namespace_owner_process import SETTLEMENT_RETRY

    owner = _spawning_owner(tmp_path, "unreadable-deadline")
    owner._verify_lock = lambda: False

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
    owner._verify_lock = lambda: True
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
    owner._verify_lock = lambda: True
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
    # The pre-Popen proof, then two unreadable sightings, then a readable one.
    results = iter([True, False, False, True])
    owner._verify_lock = lambda: next(results)
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
    owner._verify_lock = lambda: True
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
