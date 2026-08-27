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
import sys
import tempfile
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

OWNER_EPISODE_FORMAT = "spindle.owner-episode/1"
OWNER_EPISODE_KEY = "owner_episode"

_EPISODE_PHASES = frozenset({"reserved", "lock_bound", "accepted", "cleanup_proven", "released", "aborted"})
_EPISODE_TRANSITIONS = {
    ("launcher", None, "reserved"): ("starter",),
    ("launcher", "released", "reserved"): ("starter",),
    ("launcher", "aborted", "reserved"): ("starter",),
    ("launcher", "reserved", "reserved"): ("watchdog",),
    ("launcher", "reserved", "aborted"): ("failure",),
    ("owner", "reserved", "aborted"): ("failure",),
    ("owner", "reserved", "lock_bound"): ("owner", "lock"),
    ("owner", "lock_bound", "lock_bound"): ("failure",),
    ("owner", "lock_bound", "cleanup_proven"): ("containment", "cleanup"),
    ("owner", "lock_bound", "accepted"): ("provider", "provider_custody"),
    ("owner", "accepted", "accepted"): ("winning_request", "acknowledgement"),
    ("owner", "accepted", "cleanup_proven"): ("cleanup",),
    ("watchdog", "reserved", "aborted"): ("failure",),
    ("watchdog", "lock_bound", "cleanup_proven"): ("containment", "cleanup"),
    ("watchdog", "accepted", "cleanup_proven"): ("containment", "cleanup"),
    ("reconciler", "cleanup_proven", "released"): ("release",),
}
_EPISODE_EDGES = frozenset((source, destination) for _, source, destination in _EPISODE_TRANSITIONS)

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

_close_fd = os.close


class _DurablePublicationCleanupError(OSError):
    """Publication and private cleanup are durable, but a later close failed."""


def _fsync_directory_after_publication(path: Path, *, publication: bool = True) -> None:
    """Make a directory update durable and preserve fsync over close errors."""
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.close(directory_fd)
        except BaseException:
            pass
        raise
    try:
        os.close(directory_fd)
    except OSError as exc:
        if publication:
            raise _DurablePublicationCleanupError(*exc.args) from exc
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _type_strict_json_equal(left, right) -> bool:
    """Compare JSON values recursively without Python's bool/int coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _type_strict_json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _type_strict_json_equal(left_value, right_value) for left_value, right_value in zip(left, right)
        )
    return left == right


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


@dataclass(frozen=True)
class EpisodeTransitionResult:
    accepted: bool
    rejection: Optional[str]
    episode: Optional[dict]


@dataclass(frozen=True)
class EpisodeClassification:
    state: str
    reason: str


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
            # A blocked flock proves that some descriptor holds this inode, but
            # not that the pathname still names it.  Revalidate both views
            # after the flock result just as on the acquired branch.
            try:
                held_descriptor_info = os.fstat(fd)
                held_pathname_info = os.stat(path)
            except FileNotFoundError:
                return LockEvidence("identity_mismatch", detail="path_replaced_after_flock")
            except OSError as exc:
                return LockEvidence("unreadable", detail=f"post_flock_stat:{exc.errno}")
            held_descriptor = (held_descriptor_info.st_dev, held_descriptor_info.st_ino)
            held_observed = (held_pathname_info.st_dev, held_pathname_info.st_ino)
            if held_descriptor != held_observed or not _identity_matches(*held_observed, identity):
                return LockEvidence(
                    "identity_mismatch",
                    *held_observed,
                    detail="inode_mismatch_after_flock",
                )
            return LockEvidence("held", *held_observed)
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


@contextmanager
def _episode_record_guard(root: Path, spool_id: str):
    """Serialize episode updates with every production spool writer."""
    path = root / f"{spool_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        _cleanup_locked_fd(fd)


def _cleanup_locked_fd(fd: int) -> None:
    """Release and close a lock fd without replacing an active body exception."""
    active_exception = sys.exc_info()[1] is not None
    cleanup_error = None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        cleanup_error = exc
    try:
        _close_fd(fd)
    except OSError as exc:
        if cleanup_error is None:
            cleanup_error = exc
    if cleanup_error is not None and not active_exception:
        raise cleanup_error


def _episode_rejection(actor: str, source: Optional[str], destination: str) -> str:
    if (source, destination) in _EPISODE_EDGES:
        return "illegal_actor"
    return "illegal_transition"


def _valid_cleanup(value) -> bool:
    return (
        isinstance(value, dict)
        and value.get("provider_reaped") is True
        and isinstance(value.get("adopted_children_reaped"), int)
        and not isinstance(value.get("adopted_children_reaped"), bool)
        and value["adopted_children_reaped"] >= 0
        and bool(value.get("child_exit_observed_at"))
        and bool(value.get("outcome"))
    )


def _facts_are_consistent(current: dict, facts: dict) -> bool:
    for name, value in facts.items():
        if name in current and not _type_strict_json_equal(current[name], value):
            return False
    cleanup = facts.get("cleanup")
    if cleanup is not None and not _valid_cleanup(cleanup):
        return False
    lock = facts.get("lock", current.get("lock"))
    release = facts.get("release")
    if release is not None:
        if not isinstance(lock, dict) or not isinstance(release, dict):
            return False
        if not all(_type_strict_json_equal(release.get(name), lock.get(name)) for name in ("device", "inode")):
            return False
        if release.get("proved_by") != "reconciler" or not release.get("released_at"):
            return False
    return True


def transition_owner_episode(
    root: str | os.PathLike[str],
    spool_id: str,
    *,
    actor: str,
    destination: str,
    generation: int,
    expected_revision: Optional[int],
    facts: dict,
    record_updates: Optional[dict] = None,
    record_deletes: Iterable[str] = (),
    record_locked: bool = False,
    create_only: bool = False,
) -> EpisodeTransitionResult:
    """Validate and durably publish one generation-scoped owner transition."""
    root = Path(root)
    spool_path = root / f"{spool_id}.json"
    guard = nullcontext() if record_locked else _episode_record_guard(root, spool_id)
    with guard:
        if create_only:
            record = {"id": spool_id, "status": "pending", "created_at": _utc_now()}
        else:
            try:
                record = json.loads(spool_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                record = {"id": spool_id, "status": "pending", "created_at": _utc_now()}
        current = record.get(OWNER_EPISODE_KEY)
        source = current.get("phase") if isinstance(current, dict) else None

        if create_only and destination != "reserved":
            raise ValueError("create-only owner episode publication must create an initial reservation")
        if current is not None and current.get("format") != OWNER_EPISODE_FORMAT:
            return EpisodeTransitionResult(False, "unknown_episode_format", current)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            return EpisodeTransitionResult(False, "stale_generation", current)
        if current is not None:
            for name in ("generation", "revision"):
                value = current.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    return EpisodeTransitionResult(False, f"malformed_current_{name}", current)
        if expected_revision is not None and (
            not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision <= 0
        ):
            return EpisodeTransitionResult(False, "stale_revision", current)

        reseed = source in {None, "released", "aborted"} and destination == "reserved"
        if source is None:
            expected_generation = 1
        elif reseed:
            expected_generation = current["generation"] + 1
        else:
            expected_generation = current["generation"]
        if generation != expected_generation:
            return EpisodeTransitionResult(False, "stale_generation", current)
        if source is None:
            if expected_revision is not None:
                return EpisodeTransitionResult(False, "stale_revision", current)
        elif expected_revision != current.get("revision"):
            return EpisodeTransitionResult(False, "stale_revision", current)
        required = _EPISODE_TRANSITIONS.get((actor, source, destination))
        if required is None:
            return EpisodeTransitionResult(False, _episode_rejection(actor, source, destination), current)
        if not isinstance(facts, dict) or any(name not in facts for name in required):
            return EpisodeTransitionResult(False, "missing_facts", current)

        basis = {} if reseed else dict(current or {})
        if not _facts_are_consistent(basis, facts):
            return EpisodeTransitionResult(False, "contradictory_facts", current)

        now = _utc_now()
        if reseed:
            episode = {
                "format": OWNER_EPISODE_FORMAT,
                "generation": generation,
                "revision": 1,
                "phase": "reserved",
                "phase_times": {"reserved": now},
            }
            if current is not None:
                episode["predecessor"] = {
                    "generation": current.get("generation"),
                    "revision": current.get("revision"),
                    "phase": current.get("phase"),
                }
        else:
            episode = basis
            episode["revision"] = current["revision"] + 1
            episode["phase"] = destination
            episode.setdefault("phase_times", {})[destination] = now
        episode.update(facts)
        for name in record_deletes:
            if not isinstance(name, str) or name == OWNER_EPISODE_KEY:
                raise ValueError(f"invalid owner episode record deletion: {name!r}")
            record.pop(name, None)
        if record_updates:
            record.update(record_updates)
        record[OWNER_EPISODE_KEY] = episode
        if create_only:
            if not _atomic_json_create(spool_path, record):
                return EpisodeTransitionResult(False, "record_occupied", None)
        else:
            _atomic_json_write(spool_path, record)
        return EpisodeTransitionResult(True, None, episode)


def _episode_required_facts(episode: dict) -> tuple[str, ...]:
    phase = episode.get("phase")
    if phase == "reserved":
        return ("starter", "watchdog") if episode.get("revision", 0) >= 2 else ("starter",)
    if phase == "lock_bound":
        return ("starter", "watchdog", "owner", "lock")
    if phase == "accepted":
        return ("starter", "watchdog", "owner", "lock", "provider", "provider_custody")
    if phase in {"cleanup_proven", "released"}:
        common = ("starter", "watchdog", "owner", "lock", "cleanup")
        route = (
            ("provider", "provider_custody")
            if "provider" in episode or "provider_custody" in episode
            else ("containment",)
        )
        return common + route + (("release",) if phase == "released" else ())
    if phase == "aborted":
        return ("starter", "watchdog", "failure") if episode.get("revision", 0) >= 3 else ("starter", "failure")
    return ()


def _episode_malformed_reason(episode) -> Optional[str]:
    if not isinstance(episode, dict):
        return "malformed_episode"
    if episode.get("format") != OWNER_EPISODE_FORMAT:
        return "unknown_episode_format"
    for name in ("generation", "revision"):
        value = episode.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return f"malformed_{name}"
    phase = episode.get("phase")
    if phase not in _EPISODE_PHASES:
        return "malformed_phase"
    times = episode.get("phase_times")
    if not isinstance(times, dict) or phase not in times or not times:
        return "malformed_phase_times"
    missing = [name for name in _episode_required_facts(episode) if name not in episode]
    if missing:
        return f"missing_phase_fact:{missing[0]}"
    if phase in {"cleanup_proven", "released"} and not _valid_cleanup(episode.get("cleanup")):
        return "malformed_cleanup_fact"
    lock = episode.get("lock")
    release = episode.get("release")
    if phase == "released" and not _facts_are_consistent({"lock": lock}, {"release": release}):
        return "malformed_release_fact"
    return None


def classify_owner_episode(
    record: dict,
    lock: LockEvidence,
    liveness: LivenessEvidence,
) -> EpisodeClassification:
    """Purely classify a spool from its episode and exact kernel evidence."""
    episode = record.get(OWNER_EPISODE_KEY)
    if episode is None:
        if record.get("status") in {"pending", "running"}:
            return EpisodeClassification("unhealthy", "live_record_missing_owner_episode")
        return EpisodeClassification("retireable", "terminal_legacy_record")
    malformed = _episode_malformed_reason(episode)
    if malformed:
        return EpisodeClassification("unhealthy", malformed)

    phase = episode["phase"]
    if phase == "reserved":
        if lock.state != "absent_legacy":
            return EpisodeClassification("unhealthy", "reserved_has_unexpected_lock")
        if liveness.state == "dead":
            return EpisodeClassification("retireable", "reserved_starter_dead")
        return EpisodeClassification("active", "reservation_live_or_unverifiable")
    if phase == "aborted":
        if lock.state == "absent_legacy":
            return EpisodeClassification("retireable", "reservation_aborted")
        return EpisodeClassification("unhealthy", "aborted_has_unexpected_lock")
    if lock.state in {"identity_mismatch", "unreadable"}:
        return EpisodeClassification("unhealthy", f"ownership_{lock.state}")
    if lock.state == "held":
        return EpisodeClassification("active", "exact_ownership_inode_held")
    if lock.state != "released":
        return EpisodeClassification("unhealthy", "bound_episode_missing_exact_inode")
    if phase in {"lock_bound", "accepted"}:
        return EpisodeClassification("active", "released_without_cleanup_proof")
    return EpisodeClassification("retireable", "cleanup_and_release_proven")


def mailbox_path(root: str | os.PathLike[str], spool_id: str) -> Path:
    return Path(root) / f"{spool_id}.control-mailbox"


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory_after_publication(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_json_create(path: Path, value: dict) -> bool:
    """Durably publish complete JSON without replacing any pathname.

    The file-synced temporary stays hidden until a same-filesystem hard link
    atomically claims the destination. Every existing pathname form, including
    a dangling symlink, is a collision. The private link is removed before the
    final directory fsync, which makes both publication (when one occurred) and
    cleanup durable together.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    published = False
    primary = None
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException as exc:
            primary_traceback = exc.__traceback__
            try:
                os.close(fd)
            except BaseException as close_exc:
                raise exc.with_traceback(primary_traceback) from close_exc
            raise
        with stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        else:
            published = True
    except BaseException as exc:
        primary = (exc, exc.__traceback__)

    cleanup_error = None
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    except BaseException as exc:
        cleanup_error = exc

    if primary is not None:
        exc, traceback = primary
        if cleanup_error is not None:
            raise exc.with_traceback(traceback) from cleanup_error
        raise exc.with_traceback(traceback)
    if cleanup_error is not None:
        raise cleanup_error

    _fsync_directory_after_publication(path.parent, publication=published)
    return published


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
        _cleanup_locked_fd(fd)


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
    mailbox_locked: bool = False,
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
    guard = nullcontext() if mailbox_locked else mailbox_guard(root, spool_id)
    with guard:
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
        try:
            requests.append(ControlRequest.from_dict(json.loads(path.read_text())))
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Mailboxes survive rolling upgrades and interrupted atomic writes.
            # One entry without a usable request identity has no duty we can
            # settle, but it must not hide valid siblings in the same mailbox.
            continue
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


class MalformedControlReceipt(ValueError):
    """A published receipt exists but cannot be interpreted safely."""

    def __init__(self, path: Path, cause: Exception):
        self.path = path
        self.cause = cause
        super().__init__(f"malformed control receipt {path.name}: {type(cause).__name__}: {cause}")


def _load_control_receipt(path: Path) -> ControlReceipt:
    try:
        return ControlReceipt.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MalformedControlReceipt(path, exc) from exc


def write_control_receipt(
    root: str | os.PathLike[str],
    spool_id: str,
    request: ControlRequest,
    *,
    current_generation: int,
    accepted: Optional[bool] = None,
    rejection_outcome: str = "rejected_superseded",
    owner_acknowledged_at: Optional[str] = None,
    **facts,
) -> ControlReceipt:
    path = mailbox_path(root, spool_id) / f"{request.request_id}.receipt"
    if path.exists():
        return _load_control_receipt(path)
    generation_matches = request.owner_generation == current_generation
    if accepted is None:
        accepted = generation_matches
    accepted = bool(accepted and generation_matches)
    cleanup_outcome = (
        "accepted" if accepted else "rejected_stale_generation" if not generation_matches else rejection_outcome
    )
    receipt = ControlReceipt(
        request_id=request.request_id,
        owner_generation=request.owner_generation,
        owner_acknowledged_at=(owner_acknowledged_at or _utc_now()) if accepted else None,
        cleanup_outcome=cleanup_outcome,
        **facts,
    )
    if _atomic_json_create(path, asdict(receipt)):
        return receipt
    return _load_control_receipt(path)


def read_control_receipt(
    root: str | os.PathLike[str],
    spool_id: str,
    request_id: str,
) -> Optional[ControlReceipt]:
    path = mailbox_path(root, spool_id) / f"{request_id}.receipt"
    try:
        return _load_control_receipt(path)
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
        episode = spool.get(OWNER_EPISODE_KEY)
        if isinstance(episode, dict) and episode.get("phase") == "reserved":
            starter = episode.get("starter") or {}
            try:
                identity = ProcessIdentity(
                    pid=starter["pid"],
                    birth_token=starter["birth_token"],
                    namespace=NamespaceIdentity.from_dict(starter["namespace"]),
                    owner_generation=episode.get("generation", 0),
                    child_pgid=None,
                    lock_device=None,
                    lock_inode=None,
                    lock_created=False,
                )
                liveness = assess_process_liveness(identity)
            except (KeyError, TypeError, ValueError):
                liveness = LivenessEvidence("unverifiable", "malformed_starter")
            lock = LockEvidence("absent_legacy")
        elif isinstance(episode, dict):
            phase = episode.get("phase")
            lock = LockEvidence("released" if phase == "released" else "held")
            liveness = LivenessEvidence("unverifiable", "capacity_without_store")
        else:
            lock = LockEvidence("absent_legacy")
            liveness = LivenessEvidence("dead", "legacy_status")
        classification = classify_owner_episode(spool, lock, liveness)
        if classification.state != "retireable":
            count += 1
    return count


def retire_owner_artifacts(
    root: str | os.PathLike[str],
    spool_id: str,
    identity: Optional[ProcessIdentity],
) -> bool:
    """Retire a complete terminal artifact set under its recorded inode."""
    root = Path(root)
    lock_path = root / f"{spool_id}.process-owner"
    try:
        record = json.loads((root / f"{spool_id}.json").read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return False
    episode = record.get(OWNER_EPISODE_KEY)
    unbound_abort = (
        isinstance(episode, dict)
        and episode.get("format") == OWNER_EPISODE_FORMAT
        and episode.get("phase") == "aborted"
        and "lock" not in episode
        and _episode_malformed_reason(episode) is None
    )
    if unbound_abort:
        lock_fd = None
        try:
            try:
                lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
            except FileNotFoundError:
                pass
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return False
                try:
                    descriptor = os.fstat(lock_fd)
                    pathname = os.stat(lock_path)
                except OSError:
                    return False
                if (descriptor.st_dev, descriptor.st_ino) != (pathname.st_dev, pathname.st_ino):
                    return False
            _remove_owner_artifact_set(root, spool_id, lock_path)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
        return True

    if identity is None:
        return False
    if probe_ownership_lock(lock_path, identity).state != "released":
        return False
    held = acquire_ownership_lock(lock_path)
    try:
        if (held.device, held.inode) != (identity.lock_device, identity.lock_inode):
            return False
        _remove_owner_artifact_set(root, spool_id, lock_path)
    finally:
        held.close()
    lock_path.unlink()
    return True


def _remove_owner_artifact_set(root: Path, spool_id: str, lock_path: Path) -> None:
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
