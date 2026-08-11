"""Namespace-safe shared process-owner primitives.

The module is deliberately provider-neutral.  It supplies durable identity,
ownership, control, and reconciliation facts; provider protocol reduction stays
outside this slice.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

OWNER_ARTIFACT_SUFFIXES = (
    ".control-mailbox",
    ".request",
    ".receipt",
    ".owner-identity",
    ".process-identity",
    ".process-owner",
    ".owner-exit",
    ".journal-guard",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class NamespaceIdentity:
    status: str
    device: Optional[int] = None
    inode: Optional[int] = None
    reason: Optional[str] = None

    @classmethod
    def supported(cls, device: int, inode: int) -> "NamespaceIdentity":
        return cls("supported", int(device), int(inode))

    @classmethod
    def unsupported(cls, reason: str = "unsupported") -> "NamespaceIdentity":
        return cls("unsupported", reason=reason)

    @property
    def is_supported(self) -> bool:
        return self.status == "supported"

    def same_as(self, other: "NamespaceIdentity") -> Optional[bool]:
        if not self.is_supported or not other.is_supported:
            return None
        return (self.device, self.inode) == (other.device, other.inode)

    def to_dict(self) -> dict:
        result = {"status": self.status}
        if self.is_supported:
            result.update({"device": self.device, "inode": self.inode})
        elif self.reason:
            result["reason"] = self.reason
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "NamespaceIdentity":
        if value.get("status") == "supported":
            return cls.supported(value["device"], value["inode"])
        return cls.unsupported(value.get("reason", "unsupported"))


def capture_pid_namespace(path: str | os.PathLike[str] = "/proc/self/ns/pid") -> NamespaceIdentity:
    try:
        info = os.stat(path)
    except OSError:
        return NamespaceIdentity.unsupported()
    return NamespaceIdentity.supported(info.st_dev, info.st_ino)


def parse_proc_stat_starttime(record: str) -> str:
    """Return field 22, finding the end of comm from the right.

    Linux permits whitespace and parentheses inside field 2.  The stable parse
    point is the final close parenthesis, after which tail index 19 is field 22.
    """
    end = record.rfind(")")
    if end < 0:
        raise ValueError("proc stat record has no closing comm delimiter")
    tail = record[end + 1 :].split()
    if len(tail) <= 19:
        raise ValueError("proc stat record is missing field 22")
    return tail[19]


def read_proc_starttime(pid: int) -> str:
    return parse_proc_stat_starttime((Path("/proc") / str(pid) / "stat").read_text())


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    birth_token: str
    namespace: NamespaceIdentity
    owner_generation: int
    child_pgid: Optional[int]
    lock_device: Optional[int]
    lock_inode: Optional[int]
    lock_created: bool
    legacy_service_identity: Optional[str] = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["namespace"] = self.namespace.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "ProcessIdentity":
        data = dict(value)
        data["namespace"] = NamespaceIdentity.from_dict(data["namespace"])
        return cls(**data)


@dataclass(frozen=True)
class LivenessEvidence:
    state: str
    reason: str


class RealProcessOps:
    def current_namespace(self) -> NamespaceIdentity:
        return capture_pid_namespace()

    def read_starttime(self, pid: int) -> str:
        return read_proc_starttime(pid)

    def pidfd_open(self, pid: int) -> int:
        if not hasattr(os, "pidfd_open"):
            raise OSError(errno.ENOSYS, "pidfd_open unavailable")
        return os.pidfd_open(pid)

    def pidfd_is_readable(self, fd: int) -> bool:
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return bool(poller.poll(0))

    def close(self, fd: int) -> None:
        os.close(fd)


def assess_process_liveness(identity: ProcessIdentity, *, ops=None) -> LivenessEvidence:
    """Observe one recorded process without crossing PID namespace coordinates."""
    ops = ops or RealProcessOps()
    observer = ops.current_namespace()
    relation = observer.same_as(identity.namespace)
    if relation is False:
        return LivenessEvidence("unverifiable", "namespace_mismatch")
    if relation is None:
        return LivenessEvidence("unverifiable", "namespace_unavailable")

    try:
        pidfd = ops.pidfd_open(identity.pid)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return LivenessEvidence("dead", "pidfd_esrch")
        return LivenessEvidence("unverifiable", "pidfd_unavailable")
    try:
        try:
            observed_birth = ops.read_starttime(identity.pid)
        except OSError as exc:
            if exc.errno == errno.ENOENT and ops.pidfd_is_readable(pidfd):
                return LivenessEvidence("dead", "pidfd_exited")
            if exc.errno in {errno.ENOENT, errno.EACCES, errno.EPERM}:
                return LivenessEvidence("unverifiable", "proc_unavailable")
            return LivenessEvidence("unverifiable", "proc_error")
        if str(observed_birth) != str(identity.birth_token):
            return LivenessEvidence("unverifiable", "identity_mismatch")
        if ops.pidfd_is_readable(pidfd):
            return LivenessEvidence("dead", "pidfd_exited")
        return LivenessEvidence("alive", "pidfd_live")
    finally:
        ops.close(pidfd)


@dataclass(frozen=True)
class LockEvidence:
    state: str
    observed_device: Optional[int] = None
    observed_inode: Optional[int] = None
    detail: Optional[str] = None


@dataclass
class OwnershipLock:
    path: Path
    fd: int
    device: int
    inode: int
    attempts: int

    def close(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _identity_matches(device: int, inode: int, identity: ProcessIdentity) -> bool:
    return (device, inode) == (identity.lock_device, identity.lock_inode)


def probe_ownership_lock(path: str | os.PathLike[str], identity: ProcessIdentity) -> LockEvidence:
    """Classify custody of the exact recorded pathname inode without unlinking."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        state = "identity_mismatch" if identity.lock_created else "absent_legacy"
        return LockEvidence(state, detail="path_missing")
    except OSError as exc:
        return LockEvidence("unreadable", detail=f"open:{exc.errno}")

    acquired = False
    try:
        try:
            descriptor_info = os.fstat(fd)
            pathname_info = os.stat(path)
        except OSError as exc:
            return LockEvidence("unreadable", detail=f"stat:{exc.errno}")

        observed = (pathname_info.st_dev, pathname_info.st_ino)
        descriptor = (descriptor_info.st_dev, descriptor_info.st_ino)
        if descriptor != observed or not _identity_matches(*observed, identity):
            return LockEvidence("identity_mismatch", *observed, detail="inode_mismatch")

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            return LockEvidence("held", *observed)
        except OSError as exc:
            return LockEvidence("unreadable", *observed, detail=f"flock:{exc.errno}")
        try:
            acquired_descriptor_info = os.fstat(fd)
            acquired_pathname_info = os.stat(path)
        except FileNotFoundError:
            return LockEvidence("identity_mismatch", detail="path_replaced_after_flock")
        except OSError as exc:
            return LockEvidence("unreadable", detail=f"post_flock_stat:{exc.errno}")
        acquired_descriptor = (acquired_descriptor_info.st_dev, acquired_descriptor_info.st_ino)
        acquired_observed = (acquired_pathname_info.st_dev, acquired_pathname_info.st_ino)
        if acquired_descriptor != acquired_observed or not _identity_matches(*acquired_observed, identity):
            return LockEvidence(
                "identity_mismatch",
                *acquired_observed,
                detail="inode_mismatch_after_flock",
            )
        return LockEvidence("released", *acquired_observed)
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def acquire_ownership_lock(path: str | os.PathLike[str], *, max_attempts: int = 8) -> OwnershipLock:
    """Acquire a stable path inode, retrying the whole cycle after replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    last_mismatch = None
    for attempt in range(1, max_attempts + 1):
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptor_info = os.fstat(fd)
            pathname_info = os.stat(path)
            descriptor = (descriptor_info.st_dev, descriptor_info.st_ino)
            pathname = (pathname_info.st_dev, pathname_info.st_ino)
            if descriptor == pathname:
                os.set_inheritable(fd, False)
                return OwnershipLock(path, fd, descriptor[0], descriptor[1], attempt)
            last_mismatch = (descriptor, pathname)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            os.close(fd)
            raise
        os.close(fd)
    raise RuntimeError(f"ownership path did not stabilize after {max_attempts} attempts: {last_mismatch}")


def mailbox_path(root: str | os.PathLike[str], spool_id: str) -> Path:
    return Path(root) / f"{spool_id}.control-mailbox"


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json_create(path: Path, value: dict) -> bool:
    """Durably publish *value* at *path* only when the pathname is absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def mailbox_guard(root: str | os.PathLike[str], spool_id: str):
    """Serialize request publication with the owner's final arbitration pass."""
    path = Path(root) / f"{spool_id}.journal-guard"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    kind: str
    desired_terminal_kind: str
    owner_generation: int
    requested_at: str
    requested_by: str
    observer_pid: int
    observer_namespace: NamespaceIdentity
    reason: Optional[str] = None
    deadline: Optional[str] = None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["observer_namespace"] = self.observer_namespace.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "ControlRequest":
        data = dict(value)
        data["observer_namespace"] = NamespaceIdentity.from_dict(data["observer_namespace"])
        return cls(**data)


def create_control_request(
    root: str | os.PathLike[str],
    spool_id: str,
    kind: str,
    owner_generation: int,
    requested_by: str,
    *,
    request_id: Optional[str] = None,
    observer_pid: Optional[int] = None,
    observer_namespace: Optional[NamespaceIdentity] = None,
    reason: Optional[str] = None,
    deadline: Optional[str] = None,
) -> ControlRequest:
    if kind not in {"cancel", "timeout", "drop"}:
        raise ValueError(f"unsupported control request kind: {kind}")
    request_id = request_id or uuid.uuid4().hex
    path = mailbox_path(root, spool_id) / f"{request_id}.request"
    request = ControlRequest(
        request_id=request_id,
        kind=kind,
        desired_terminal_kind="timeout" if kind == "timeout" else "cancelled",
        owner_generation=int(owner_generation),
        requested_at=_utc_now(),
        requested_by=requested_by,
        observer_pid=os.getpid() if observer_pid is None else int(observer_pid),
        observer_namespace=observer_namespace or capture_pid_namespace(),
        reason=reason,
        deadline=deadline,
    )
    with mailbox_guard(root, spool_id):
        if _atomic_json_create(path, request.to_dict()):
            return request
        existing = ControlRequest.from_dict(json.loads(path.read_text()))
        expected = request.to_dict()
        observed = existing.to_dict()
        # The first durable publication owns its timestamp. Reusing the ID is
        # idempotent only when every caller-controlled field agrees.
        expected.pop("requested_at", None)
        observed.pop("requested_at", None)
        if observed != expected:
            raise ValueError(f"control request ID {request_id!r} already has a different payload")
        return existing


def iter_control_requests(root: str | os.PathLike[str], spool_id: str) -> Iterable[ControlRequest]:
    mailbox = mailbox_path(root, spool_id)
    if not mailbox.exists():
        return ()
    requests = []
    for path in sorted(mailbox.glob("*.request")):
        requests.append(ControlRequest.from_dict(json.loads(path.read_text())))
    return tuple(requests)


@dataclass(frozen=True)
class ControlReceipt:
    request_id: str
    owner_generation: int
    owner_acknowledged_at: Optional[str] = None
    provider_cancel_attempted_at: Optional[str] = None
    provider_acknowledged_at: Optional[str] = None
    terminal_observed_at: Optional[str] = None
    forced_cleanup_started_at: Optional[str] = None
    forced_cleanup_completed_at: Optional[str] = None
    child_exit_observed_at: Optional[str] = None
    cleanup_outcome: Optional[str] = None

    @classmethod
    def from_dict(cls, value: dict) -> "ControlReceipt":
        return cls(**value)


def write_control_receipt(
    root: str | os.PathLike[str],
    spool_id: str,
    request: ControlRequest,
    *,
    current_generation: int,
    accepted: Optional[bool] = None,
    rejection_outcome: str = "rejected_superseded",
    **facts,
) -> ControlReceipt:
    path = mailbox_path(root, spool_id) / f"{request.request_id}.receipt"
    if path.exists():
        return ControlReceipt.from_dict(json.loads(path.read_text()))
    generation_matches = request.owner_generation == current_generation
    if accepted is None:
        accepted = generation_matches
    accepted = bool(accepted and generation_matches)
    cleanup_outcome = (
        "accepted"
        if accepted
        else "rejected_stale_generation"
        if not generation_matches
        else rejection_outcome
    )
    receipt = ControlReceipt(
        request_id=request.request_id,
        owner_generation=request.owner_generation,
        owner_acknowledged_at=_utc_now() if accepted else None,
        cleanup_outcome=cleanup_outcome,
        **facts,
    )
    if _atomic_json_create(path, asdict(receipt)):
        return receipt
    return ControlReceipt.from_dict(json.loads(path.read_text()))

def read_control_receipt(
    root: str | os.PathLike[str],
    spool_id: str,
    request_id: str,
) -> Optional[ControlReceipt]:
    path = mailbox_path(root, spool_id) / f"{request_id}.receipt"
    try:
        return ControlReceipt.from_dict(json.loads(path.read_text()))
    except FileNotFoundError:
        return None


def update_control_receipt(
    root: str | os.PathLike[str],
    spool_id: str,
    request_id: str,
    **facts,
) -> ControlReceipt:
    path = mailbox_path(root, spool_id) / f"{request_id}.receipt"
    current = read_control_receipt(root, spool_id, request_id)
    if current is None:
        raise FileNotFoundError(path)
    value = asdict(current)
    for key, fact in facts.items():
        if key not in value:
            raise ValueError(f"unknown receipt fact: {key}")
        value[key] = fact
    updated = ControlReceipt.from_dict(value)
    _atomic_json_write(path, asdict(updated))
    return updated


@dataclass(frozen=True)
class LegacyAuthority:
    recorded: Optional[str]
    observer: Optional[str]
    manual_recovery: bool = False

    @property
    def authorized(self) -> bool:
        return self.manual_recovery or (bool(self.recorded) and self.recorded == self.observer)


@dataclass(frozen=True)
class ReconciliationResult:
    state: str
    reason: str
    liveness: LivenessEvidence
    lock: LockEvidence


def reconcile_owner_episode(
    lock: LockEvidence,
    liveness: LivenessEvidence,
    *,
    exit_evidence: bool = False,
    cleanup_evidence: bool = False,
    stopping: bool = False,
    legacy_authority: Optional[LegacyAuthority] = None,
) -> ReconciliationResult:
    if lock.state == "held":
        return ReconciliationResult("stopping" if stopping else "active", "exact_ownership_inode_held", liveness, lock)
    if lock.state in {"unreadable", "identity_mismatch"}:
        return ReconciliationResult("store_unhealthy", f"ownership_{lock.state}", liveness, lock)
    if lock.state == "released":
        if liveness.state == "dead" and (exit_evidence or cleanup_evidence):
            return ReconciliationResult("terminalizable", "released_with_exit_evidence", liveness, lock)
        return ReconciliationResult("unverifiable", "released_without_complete_exit_evidence", liveness, lock)
    if lock.state == "absent_legacy":
        if (
            legacy_authority is not None
            and legacy_authority.authorized
            and liveness.state == "dead"
            and (exit_evidence or cleanup_evidence)
        ):
            return ReconciliationResult("terminalizable", "authorized_legacy_exit", liveness, lock)
        return ReconciliationResult("unverifiable", "legacy_authority_unproven", liveness, lock)
    return ReconciliationResult("unverifiable", "unknown_ownership_evidence", liveness, lock)


def active_spool_count(spools: Iterable[dict]) -> int:
    count = 0
    for spool in spools:
        lifecycle = spool.get("lifecycle") or {}
        if spool.get("status") in {"pending", "running"} or lifecycle.get("public_stop_state") == "stopping":
            count += 1
    return count


def retire_owner_artifacts(
    root: str | os.PathLike[str],
    spool_id: str,
    identity: ProcessIdentity,
) -> bool:
    """Retire a complete terminal artifact set under its recorded inode."""
    root = Path(root)
    lock_path = root / f"{spool_id}.process-owner"
    if probe_ownership_lock(lock_path, identity).state != "released":
        return False
    held = acquire_ownership_lock(lock_path)
    try:
        if (held.device, held.inode) != (identity.lock_device, identity.lock_inode):
            return False
        for suffix in OWNER_ARTIFACT_SUFFIXES:
            path = root / f"{spool_id}{suffix}"
            if path == lock_path:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for suffix in (".json", ".stdout", ".stderr", ".exit", ".prompt"):
            (root / f"{spool_id}{suffix}").unlink(missing_ok=True)
        (root / "transcripts" / f"{spool_id}.txt").unlink(missing_ok=True)
    finally:
        held.close()
    lock_path.unlink()
    return True
