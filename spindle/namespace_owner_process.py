"""Packaged logical owner for one provider process and its descendants."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .namespace_owner import (
    MalformedControlReceipt,
    ProcessIdentity,
    _atomic_json_write,
    _utc_now,
    acquire_ownership_lock,
    capture_pid_namespace,
    create_control_request,
    iter_control_requests,
    mailbox_guard,
    parse_proc_stat_starttime,
    read_control_receipt,
    transition_owner_episode,
    update_control_receipt,
    write_control_receipt,
)

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _set_subreaper() -> None:
    if sys.platform.startswith("linux"):
        _prctl(PR_SET_CHILD_SUBREAPER, 1)


def _provider_preexec(expected_parent: int, disable_pdeathsig: bool):
    def prepare() -> None:
        if not sys.platform.startswith("linux") or disable_pdeathsig:
            return
        _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != expected_parent:
            os._exit(125)

    return prepare


def _starttime(pid: int) -> str:
    return parse_proc_stat_starttime((Path("/proc") / str(pid) / "stat").read_text())


class Checkpoints:
    def __init__(self, fd: Optional[int], pause_name: Optional[str], generation: int):
        self.socket = socket.socket(fileno=fd) if fd is not None else None
        self.pause_name = pause_name
        self.generation = generation

    def reach(self, name: str, provider_pid: Optional[int] = None) -> None:
        if self.socket is None or name != self.pause_name:
            return
        message = {
            "name": name,
            "owner_pid": os.getpid(),
            "owner_generation": self.generation,
            "provider_pid": provider_pid,
        }
        self.socket.sendall((json.dumps(message, sort_keys=True) + "\n").encode())
        response = b""
        while not response.endswith(b"\n"):
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError(f"checkpoint controller closed at {name}")
            response += chunk
        if response.strip() != b"continue":
            raise RuntimeError(f"unexpected checkpoint response at {name}: {response!r}")


class OwnerClock:
    def __init__(self, fd: Optional[int]):
        self.socket = socket.socket(fileno=fd) if fd is not None else None

    def monotonic(self) -> float:
        if self.socket is None:
            return time.monotonic()
        self.socket.sendall(b"now\n")
        response = b""
        while not response.endswith(b"\n"):
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("controlled clock closed")
            response += chunk
        return float(response.strip())


class LogicalOwner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.store = Path(args.store).resolve()
        self.spool_id = args.spool_id
        self.store.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.store / f"{self.spool_id}.process-owner"
        self.owner_identity_path = self.store / f"{self.spool_id}.owner-identity"
        self.process_identity_path = self.store / f"{self.spool_id}.process-identity"
        self.owner_exit_path = self.store / f"{self.spool_id}.owner-exit"
        self.spool_path = self.store / f"{self.spool_id}.json"
        self.spool_lock_path = self.store / f"{self.spool_id}.lock"
        self.stdout_path = self.store / f"{self.spool_id}.stdout"
        self.stderr_path = self.store / f"{self.spool_id}.stderr"
        self.exit_path = self.store / f"{self.spool_id}.exit"
        self.lock = None
        self.provider: Optional[subprocess.Popen] = None
        self.provider_pidfd: Optional[int] = None
        self.provider_pgid: Optional[int] = None
        self.provider_birth: Optional[str] = None
        self.control: Optional[socket.socket] = None
        self.generation = int(args.generation)
        self.checkpoints = Checkpoints(args.checkpoint_fd, args.pause_checkpoint, self.generation)
        self.clock = OwnerClock(args.clock_fd)
        self.adopted_reaped = 0
        self.wall_deadline_at: Optional[str] = args.deadline
        self.episode_mode = False
        self._reported_malformed_receipts: set[str] = set()

    def _watchdog_alive(self) -> bool:
        """Whether the mandatory containment parent still holds its lease FD."""
        fd = getattr(self.args, "watchdog_fd", None)
        if fd is None:
            return True
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return not bool(poller.poll(0))

    def _deadline_expired(self) -> bool:
        return getattr(self, "wall_deadline_at", None) is not None and self._remaining_wall_budget() <= 0

    def _watchdog_loss_facts(self, *, provider_exit_code: Optional[int]) -> tuple[dict, dict, dict]:
        observed_at = _utc_now()
        cleanup = {
            "outcome": "watchdog_parent_lost_contained",
            "provider_reaped": True,
            "adopted_children_reaped": self.adopted_reaped,
            "child_exit_observed_at": observed_at,
            "provider_exit_code": provider_exit_code,
        }
        containment = {
            "contained": True,
            "adopted_children_reaped": self.adopted_reaped,
            "observed_at": observed_at,
        }
        failure = {
            "kind": "watchdog_parent_loss",
            "detail": "logical owner detected containment watchdog exit",
            "observed_at": observed_at,
        }
        return cleanup, containment, failure

    def _abort_for_watchdog_loss_before_binding(self) -> None:
        episode = self._read_spool().get("owner_episode") or {}
        if not self.episode_mode or episode.get("phase") != "reserved":
            return
        _cleanup, _containment, failure = self._watchdog_loss_facts(provider_exit_code=None)
        transition_owner_episode(
            self.store,
            self.spool_id,
            actor="owner",
            destination="aborted",
            generation=self.generation,
            expected_revision=episode.get("revision"),
            facts={"failure": failure},
        )

    def _settle_deadline_expiry_before_binding(self) -> None:
        observed_at = _utc_now()
        failure = {
            "kind": "deadline_expired_before_provider_start",
            "detail": "inherited deadline expired before logical owner binding",
            "observed_at": observed_at,
        }
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="aborted",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={"failure": failure},
            )
        else:
            lifecycle = {
                **(self._read_spool().get("lifecycle") or {}),
                "ownership_state": "released",
                "transport_state": "reaped",
                "normalized_terminal_kind": "timeout",
            }
            updates = {
                "status": "timeout",
                "error": f"Timeout after {self._read_spool().get('timeout')}s",
                "error_kind": "deadline_expired_before_provider_start",
                "completed_at": observed_at,
                "lifecycle": lifecycle,
            }
            self._update_spool(**updates)
        _atomic_json_write(
            self.owner_exit_path,
            {
                "owner_pid": os.getpid(),
                "owner_generation": self.generation,
                "provider_pid": None,
                "provider_exit_code": None,
                "provider_reaped": True,
                "adopted_children_reaped": 0,
                "cleanup_outcome": "deadline_expired_before_provider_start",
                "observed_at": observed_at,
            },
        )

    def _settle_deadline_expiry_after_binding(self) -> bool:
        """Prove a bound generation timed out without ever starting a provider."""
        if not self._verify_lock():
            return False
        observed_at = _utc_now()
        cleanup = {
            "outcome": "deadline_expired_before_provider_start",
            "provider_reaped": True,
            "adopted_children_reaped": 0,
            "child_exit_observed_at": observed_at,
            "provider_exit_code": None,
        }
        failure = {
            "kind": "deadline_expired_before_provider_start",
            "detail": "inherited deadline expired after logical owner binding and before provider start",
            "observed_at": observed_at,
        }
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            settled = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="cleanup_proven",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={
                    "containment": {
                        "contained": True,
                        "adopted_children_reaped": 0,
                        "observed_at": observed_at,
                    },
                    "cleanup": cleanup,
                    "failure": failure,
                },
            )
            if not settled.accepted:
                return False
        else:
            spool = self._read_spool()
            lifecycle = {
                **(spool.get("lifecycle") or {}),
                "ownership_state": "held",
                "transport_state": "reaped",
                "normalized_terminal_kind": "timeout",
            }
            self._update_spool(
                status="timeout",
                error=f"Timeout after {spool.get('timeout')}s",
                error_kind="deadline_expired_before_provider_start",
                completed_at=observed_at,
                lifecycle=lifecycle,
            )
        _atomic_json_write(
            self.owner_exit_path,
            {
                "owner_pid": os.getpid(),
                "owner_generation": self.generation,
                "provider_pid": None,
                "provider_exit_code": None,
                "provider_reaped": True,
                "adopted_children_reaped": 0,
                "cleanup_outcome": cleanup["outcome"],
                "observed_at": observed_at,
            },
        )
        return True

    def _settle_provider_spawn_failure(self, error: OSError) -> bool:
        """Publish a bound episode that never transferred provider custody."""
        if not self._verify_lock():
            return False
        observed_at = _utc_now()
        detail = f"provider spawn failed: {error}"
        cleanup = {
            "outcome": "provider_spawn_failed",
            "provider_reaped": True,
            "adopted_children_reaped": 0,
            "child_exit_observed_at": observed_at,
            "provider_exit_code": None,
        }
        failure = {
            "kind": "provider_spawn_failure",
            "detail": detail,
            "observed_at": observed_at,
        }
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            settled = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="cleanup_proven",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={
                    "containment": {
                        "contained": True,
                        "adopted_children_reaped": 0,
                        "observed_at": observed_at,
                    },
                    "cleanup": cleanup,
                    "failure": failure,
                },
            )
            if not settled.accepted:
                return False
        else:
            lifecycle = {
                **(self._read_spool().get("lifecycle") or {}),
                "ownership_state": "held",
                "transport_state": "reaped",
                "normalized_terminal_kind": "failed",
            }
            self._update_spool(
                status="error",
                error=detail,
                error_kind="provider_spawn_failure",
                completed_at=observed_at,
                lifecycle=lifecycle,
            )
        _atomic_json_write(
            self.owner_exit_path,
            {
                "owner_pid": os.getpid(),
                "owner_generation": self.generation,
                "provider_pid": None,
                "provider_exit_code": None,
                "provider_reaped": True,
                "adopted_children_reaped": 0,
                "cleanup_outcome": cleanup["outcome"],
                "observed_at": observed_at,
            },
        )
        return True

    def _contain_after_watchdog_loss(self) -> bool:
        """Take over containment, publish proof, and leave no live descendants."""
        returncode = None
        if self.provider is not None:
            if not self._signal_provider_group(signal.SIGKILL):
                return False
            returncode = self._finish_provider()
            if returncode is None:
                return False
        while not self._settle_descendants(force=True):
            if not self._verify_lock():
                return False
            time.sleep(self.args.poll_interval)
        provider_exit_code = None if returncode is None else 128 - returncode if returncode < 0 else returncode
        cleanup, containment, failure = self._watchdog_loss_facts(provider_exit_code=provider_exit_code)
        if not self._verify_lock():
            return False
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            result = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="cleanup_proven",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={"cleanup": cleanup, "containment": containment, "failure": failure},
            )
            if not result.accepted:
                return False
        evidence = {
            "owner_pid": os.getpid(),
            "owner_generation": self.generation,
            "provider_pid": self.provider.pid if self.provider else None,
            "provider_exit_code": provider_exit_code,
            "provider_reaped": True,
            "adopted_children_reaped": self.adopted_reaped,
            "cleanup_outcome": cleanup["outcome"],
            "watchdog_parent_lost": True,
            "observed_at": cleanup["child_exit_observed_at"],
        }
        _atomic_json_write(self.owner_exit_path, evidence)
        self.checkpoints.reach("watchdog_loss_cleanup_proven", self.provider.pid if self.provider else None)
        return True

    def _contain_after_watchdog_loss_until_proven(self) -> None:
        """Never abandon the owner role after its watchdog lease is lost."""
        while not self._contain_after_watchdog_loss():
            time.sleep(self.args.poll_interval)

    def _await_launch_barrier(self) -> bool:
        fd = self.args.launch_barrier_fd
        if fd is None:
            return True
        try:
            return bool(os.read(fd, 3))
        finally:
            os.close(fd)
            self.args.launch_barrier_fd = None

    def _read_spool(self) -> dict:
        try:
            return json.loads(self.spool_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"id": self.spool_id, "status": "pending", "created_at": _utc_now()}

    def _write_spool_unlocked(self, spool: dict) -> None:
        _atomic_json_write(self.spool_path, spool)

    @contextmanager
    def _spool_record_guard(self):
        """Join the launcher's compatibility record lock when it exists."""
        try:
            fd = os.open(self.spool_lock_path, os.O_RDWR)
        except FileNotFoundError:
            # Direct primitive users have no legacy record-lock sidecar.  The
            # owner must not introduce a new ``*.lock`` artifact which an old
            # store sweep can enumerate; public launch always creates this
            # historical lock before releasing the owner barrier.
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _update_spool(self, **values) -> dict:
        with self._spool_record_guard():
            spool = self._read_spool()
            spool.update(values)
            self._write_spool_unlocked(spool)
            return spool

    def _set_lifecycle(self, **values) -> dict:
        with self._spool_record_guard():
            spool = self._read_spool()
            lifecycle = dict(spool.get("lifecycle") or {})
            lifecycle.update(values)
            spool["lifecycle"] = lifecycle
            self._write_spool_unlocked(spool)
            return spool

    def _allocate_generation(self) -> None:
        if self.episode_mode:
            return
        try:
            previous = json.loads(self.owner_identity_path.read_text()).get("owner_generation", 0)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            previous = 0
        if self.generation <= previous:
            self.generation = previous + 1
            self.checkpoints.generation = self.generation

    def _ensure_wall_deadline(self) -> None:
        """Persist one UTC deadline and reuse it across owner recovery."""
        episode = self._read_spool().get("owner_episode") or {}
        if episode:
            self.wall_deadline_at = episode.get("deadline")
            return
        if self.args.timeout is None:
            return
        with self._spool_record_guard():
            spool = self._read_spool()
            deadline = spool.get("wall_deadline_at") or self.wall_deadline_at
            if deadline is None:
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=self.args.timeout)).isoformat()
            self.wall_deadline_at = deadline
            if spool.get("wall_deadline_at") != deadline:
                spool["wall_deadline_at"] = deadline
                self._write_spool_unlocked(spool)

    def _remaining_wall_budget(self) -> Optional[float]:
        if not self.wall_deadline_at:
            return None
        try:
            deadline = datetime.fromisoformat(self.wall_deadline_at)
        except (TypeError, ValueError):
            return 0.0
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())

    def _terminal_output_state(self) -> tuple[bool, bool, Optional[datetime]]:
        """Recognize terminal output, its durable write time, and linger state."""
        import spindle

        if not all(hasattr(self, name) for name in ("spool_path", "stdout_path", "stderr_path")):
            return False, False, None
        spool = self._read_spool()
        if (spool.get("harness") or "claude-code").lower() not in {"claude-code", "gemini"}:
            return False, False, None
        output_path = spindle._spool_complete_output_path(spool, self.stdout_path, self.stderr_path)
        if output_path is None:
            return False, False, None
        try:
            filesystem_written = datetime.fromtimestamp(output_path.stat().st_mtime, timezone.utc)
        except OSError:
            return False, False, None
        detected_raw = spool.get("output_complete_detected_at")
        written_raw = spool.get("output_complete_written_at")
        try:
            detected = datetime.fromisoformat(detected_raw) if detected_raw else None
        except (TypeError, ValueError):
            detected = None
        try:
            written = datetime.fromisoformat(written_raw) if written_raw else None
        except (TypeError, ValueError):
            written = None
        now = datetime.now(timezone.utc)
        updates = {}
        if detected is None:
            detected = now
            updates["output_complete_detected_at"] = detected.isoformat()
        if written is None:
            written = filesystem_written
            updates["output_complete_written_at"] = written.isoformat()
        if updates:
            self._update_spool(**updates)
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        shutdown_due = (
            now - detected.astimezone(timezone.utc)
        ).total_seconds() >= spindle.OUTPUT_COMPLETION_GRACE_SECONDS
        return True, shutdown_due, written.astimezone(timezone.utc)

    def _terminal_output_precedes_timeout(self, request, written_at: Optional[datetime]) -> bool:
        """Whether terminal evidence was durably written before this timeout."""
        return self._terminal_output_precedes_boundary(written_at, request.deadline or request.requested_at)

    def _terminal_output_precedes_deadline(self, written_at: Optional[datetime]) -> bool:
        """Whether terminal evidence predates the episode's absolute deadline."""
        if self.wall_deadline_at is None:
            # Legacy timeout episodes may have only a monotonic budget. Preserve
            # their prior completed-output precedence; new reservations always
            # carry the absolute deadline needed for exact ordering.
            return written_at is not None
        return self._terminal_output_precedes_boundary(written_at, self.wall_deadline_at)

    @staticmethod
    def _terminal_output_precedes_boundary(written_at: Optional[datetime], boundary_raw: Optional[str]) -> bool:
        if written_at is None:
            return False
        try:
            boundary = datetime.fromisoformat(boundary_raw)
        except (TypeError, ValueError):
            return False
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        return written_at <= boundary.astimezone(timezone.utc)

    def _reject_timeout_for_terminal_output(self, request) -> bool:
        """Durably settle a timeout that lost to pre-deadline terminal output."""
        with mailbox_guard(self.store, self.spool_id):
            if not self._verify_lock():
                return False
            write_control_receipt(
                self.store,
                self.spool_id,
                request,
                current_generation=self.generation,
                accepted=False,
                rejection_outcome="rejected_terminal",
            )
        return True

    def _publish_owner_identity(self) -> ProcessIdentity:
        namespace = capture_pid_namespace()
        identity = ProcessIdentity(
            pid=os.getpid(),
            birth_token=_starttime(os.getpid()),
            namespace=namespace,
            owner_generation=self.generation,
            child_pgid=None,
            lock_device=self.lock.device,
            lock_inode=self.lock.inode,
            lock_created=True,
        )
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            result = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="lock_bound",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={
                    "owner": {
                        "pid": identity.pid,
                        "birth_token": identity.birth_token,
                        "namespace": identity.namespace.to_dict(),
                    },
                    "lock": {"device": self.lock.device, "inode": self.lock.inode},
                },
            )
            if not result.accepted:
                raise RuntimeError(f"owner lock binding rejected: {result.rejection}")
        _atomic_json_write(self.owner_identity_path, identity.to_dict())
        (self.store / f"{self.spool_id}.journal-guard").touch(exist_ok=True)
        (self.store / f"{self.spool_id}.control-mailbox").mkdir(exist_ok=True)
        return identity

    def _verify_lock(self) -> bool:
        if not os.access(self.lock_path, os.R_OK | os.W_OK):
            if self.lock_path.exists():
                self._set_lifecycle(ownership_state="unreadable")
            else:
                self._set_lifecycle(
                    ownership_state="identity_mismatch",
                    recorded_lock_device=self.lock.device,
                    recorded_lock_inode=self.lock.inode,
                    observed_lock_device=None,
                    observed_lock_inode=None,
                )
            return False
        try:
            descriptor = os.fstat(self.lock.fd)
            pathname = os.stat(self.lock_path)
        except FileNotFoundError:
            self._set_lifecycle(
                ownership_state="identity_mismatch",
                recorded_lock_device=self.lock.device,
                recorded_lock_inode=self.lock.inode,
                observed_lock_device=None,
                observed_lock_inode=None,
            )
            return False
        except OSError:
            self._set_lifecycle(ownership_state="unreadable")
            return False
        exact = (descriptor.st_dev, descriptor.st_ino) == (pathname.st_dev, pathname.st_ino)
        if not exact:
            self._set_lifecycle(
                ownership_state="identity_mismatch",
                recorded_lock_device=descriptor.st_dev,
                recorded_lock_inode=descriptor.st_ino,
                observed_lock_device=pathname.st_dev,
                observed_lock_inode=pathname.st_ino,
            )
            return False
        lifecycle = self._read_spool().get("lifecycle") or {}
        if lifecycle.get("ownership_state") in {"unreadable", "identity_mismatch"}:
            self._set_lifecycle(ownership_state="held")
        return True

    def _spawn_provider(
        self,
        *,
        started: Optional[float] = None,
        monotonic_budget: Optional[float] = None,
    ) -> Optional[int]:
        owner_end, child_end = socket.socketpair()
        owner_end.set_inheritable(False)
        child_end.set_inheritable(True)
        provider_env = os.environ.copy()
        provider_env.pop("_SPINDLE_STORE_SUPERVISOR", None)
        provider_env["SPINDLE_PROVIDER_CONTROL_FD"] = str(child_end.fileno())
        provider_env["SPINDLE_OWNER_STORE"] = str(self.store)
        provider_env["SPINDLE_OWNER_SPOOL_ID"] = self.spool_id
        stdin_stream = open(self.args.stdin_path, "r") if self.args.stdin_path else subprocess.DEVNULL
        spawn_error = None
        try:
            with open(self.stdout_path, "w") as stdout, open(self.stderr_path, "w") as stderr:
                self.checkpoints.reach("before_provider_start_checks")
                if not self._watchdog_alive():
                    self._contain_after_watchdog_loss_until_proven()
                    return 125
                monotonic_expired = (
                    started is not None
                    and monotonic_budget is not None
                    and self.clock.monotonic() - started >= monotonic_budget
                )
                if self._deadline_expired() or monotonic_expired:
                    if not self._settle_deadline_expiry_after_binding():
                        raise RuntimeError("could not durably settle inherited deadline before provider start")
                    return 124
                try:
                    self.provider = subprocess.Popen(
                        self.args.command,
                        cwd=self.args.cwd,
                        env=provider_env,
                        stdin=stdin_stream,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                        pass_fds=(child_end.fileno(),),
                        preexec_fn=_provider_preexec(os.getpid(), self.args.disable_pdeathsig),
                    )
                except OSError as exc:
                    spawn_error = exc
        finally:
            child_end.close()
            if stdin_stream is not subprocess.DEVNULL:
                stdin_stream.close()
            if self.provider is None:
                owner_end.close()
        if spawn_error is not None:
            if not self._settle_provider_spawn_failure(spawn_error):
                raise RuntimeError("could not durably settle provider spawn failure") from spawn_error
            return 127
        self.control = owner_end
        self.control.setblocking(False)
        self.provider_pgid = self.provider.pid
        self.provider_birth = _starttime(self.provider.pid)
        # The packaged watchdog is already the primary containment parent.
        # This checkpoint exercises owner death in the narrow interval before
        # the remaining provider identity/pidfd publication completes.
        self.checkpoints.reach("provider_forked_before_containment_armed", self.provider.pid)
        try:
            self.provider_pidfd = os.pidfd_open(self.provider.pid)
        except (AttributeError, OSError):
            self.provider_pidfd = None
        process_identity = {
            "owner_pid": os.getpid(),
            "owner_generation": self.generation,
            "owner_namespace": capture_pid_namespace().to_dict(),
            "provider_pid": self.provider.pid,
            "provider_pgid": self.provider_pgid,
            "provider_birth_token": self.provider_birth,
            "provider_pidfd_acquired": self.provider_pidfd is not None,
            "lock_device": self.lock.device,
            "lock_inode": self.lock.inode,
        }
        _atomic_json_write(self.process_identity_path, process_identity)
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            accepted = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="accepted",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={
                    "provider": {
                        "pid": self.provider.pid,
                        "pgid": self.provider_pgid,
                        "birth_token": self.provider_birth,
                        "namespace": capture_pid_namespace().to_dict(),
                    },
                    "provider_custody": {
                        "pidfd_acquired": self.provider_pidfd is not None,
                        "containment": "watchdog",
                        "published_at": _utc_now(),
                    },
                },
            )
            if not accepted.accepted:
                raise RuntimeError(f"provider acceptance rejected: {accepted.rejection}")
        with self._spool_record_guard():
            spool = self._read_spool()
            spool.update(
                {
                    "status": "running",
                    "pid": os.getpid(),
                    "owner_pid": os.getpid(),
                    "provider_pid": self.provider.pid,
                    "provider_process_group_id": self.provider_pgid,
                    "owner_generation": self.generation,
                    "process_start_time": _starttime(os.getpid()),
                    "lifecycle": {
                        **(spool.get("lifecycle") or {}),
                        "ownership_state": "held",
                        "transport_state": "connected",
                    },
                }
            )
            self._write_spool_unlocked(spool)
        if self.args.ready_fd is not None:
            ready = {
                "owner_pid": os.getpid(),
                "provider_pid": self.provider.pid,
                "provider_pgid": self.provider_pgid,
                "owner_generation": self.generation,
                "lock_device": self.lock.device,
                "lock_inode": self.lock.inode,
            }
            os.write(self.args.ready_fd, (json.dumps(ready, sort_keys=True) + "\n").encode())
            os.close(self.args.ready_fd)
            self.args.ready_fd = None
        # A checkpoint named provider_ready is a test-only causal boundary: do
        # not report it before the launched program has executed far enough to
        # publish its first output (or has already exited).  Waiting on the
        # pidfd keeps this deterministic without adding a production delay.
        if self.checkpoints.socket is not None and self.checkpoints.pause_name == "provider_ready":
            deadline = time.monotonic() + 2.0
            readiness_poller = None
            if self.provider_pidfd is not None:
                readiness_poller = select.poll()
                readiness_poller.register(self.provider_pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
            while time.monotonic() < deadline:
                try:
                    if self.stdout_path.stat().st_size:
                        break
                except OSError:
                    pass
                if self._provider_exited():
                    break
                if readiness_poller is not None:
                    readiness_poller.poll(10)
                else:
                    time.sleep(0.01)
        self.checkpoints.reach("provider_ready", self.provider.pid)
        return None

    def _provider_exited(self) -> bool:
        if self.provider is None:
            return True
        if self.provider_pidfd is not None:
            poller = select.poll()
            poller.register(self.provider_pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
            if poller.poll(0):
                self.provider.poll()
        return self.provider.poll() is not None

    def _wait_provider(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._provider_exited():
                return True
            time.sleep(0.01)
        return self._provider_exited()

    def _drain_reapable(self) -> int:
        reaped = 0
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            reaped += 1
        self.adopted_reaped += reaped
        return reaped

    def _direct_children(self) -> list[int]:
        path = Path(f"/proc/self/task/{os.getpid()}/children")
        try:
            return [int(value) for value in path.read_text().split()]
        except (FileNotFoundError, OSError, ValueError):
            return []

    def _settle_descendants(self, *, force: bool) -> bool:
        deadline = time.monotonic() + (0.2 if force else 0.5)
        while time.monotonic() < deadline:
            self._drain_reapable()
            children = [pid for pid in self._direct_children() if self.provider is None or pid != self.provider.pid]
            if not children:
                return True
            time.sleep(0.01)
        children = [pid for pid in self._direct_children() if self.provider is None or pid != self.provider.pid]
        if children and not self._verify_lock():
            return False
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self._drain_reapable()
            children = [pid for pid in self._direct_children() if self.provider is None or pid != self.provider.pid]
            if not children:
                return True
            time.sleep(0.01)
        return not children

    def _send_provider_cancel(self) -> bool:
        if self.control is None:
            return False
        try:
            self.control.sendall(b'{"type":"cancel"}\n')
        except OSError:
            return False
        readable, _, _ = select.select([self.control], [], [], 0.2)
        if not readable:
            return False
        try:
            return b"ack" in self.control.recv(4096)
        except OSError:
            return False

    def _signal_provider_group(self, sig: int) -> bool:
        if not self._verify_lock():
            return False
        if self.provider_pgid is None:
            return True
        if self.provider is not None and self.provider.poll() is None:
            try:
                if _starttime(self.provider.pid) != self.provider_birth:
                    raise RuntimeError("provider PID identity changed before signal")
            except FileNotFoundError:
                return True
        try:
            os.killpg(self.provider_pgid, sig)
        except ProcessLookupError:
            pass
        return True

    def _finish_provider(self) -> Optional[int]:
        if self.provider is None:
            return 127
        try:
            return self.provider.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if not self._signal_provider_group(signal.SIGKILL):
                return None
            try:
                return self.provider.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return None

    def _write_exit_evidence(self, returncode: int, *, cleanup_outcome: str) -> bool:
        if not self._verify_lock():
            return False
        legacy_code = 128 - returncode if returncode < 0 else returncode
        _atomic_json_write(
            self.owner_exit_path,
            {
                "owner_pid": os.getpid(),
                "owner_generation": self.generation,
                "provider_pid": self.provider.pid if self.provider else None,
                "provider_exit_code": legacy_code,
                "provider_reaped": True,
                "adopted_children_reaped": self.adopted_reaped,
                "cleanup_outcome": cleanup_outcome,
                "observed_at": _utc_now(),
            },
        )
        temporary = self.exit_path.with_name(f".{self.exit_path.name}.{os.getpid()}.tmp")
        with open(temporary, "w") as stream:
            stream.write(f"{legacy_code}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.exit_path)
        directory_fd = os.open(self.store, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True

    def _next_current_request(self):
        """Return the next current-generation request and reject stale siblings."""
        with mailbox_guard(self.store, self.spool_id):
            for request in iter_control_requests(self.store, self.spool_id):
                if self._receipt_exists_or_is_malformed(request.request_id):
                    continue
                if request.owner_generation != self.generation:
                    if not self._verify_lock():
                        return None
                    write_control_receipt(
                        self.store,
                        self.spool_id,
                        request,
                        current_generation=self.generation,
                        accepted=False,
                    )
                    continue
                return request
        return None

    def _settle_other_requests_unlocked(
        self,
        accepted_request_id: Optional[str],
        *,
        rejection_outcome: str = "rejected_superseded",
    ) -> bool:
        """Give every durable non-winning request its generation-scoped receipt."""
        if not self._verify_lock():
            return False
        for request in iter_control_requests(self.store, self.spool_id):
            if request.request_id == accepted_request_id:
                continue
            if self._receipt_exists_or_is_malformed(request.request_id):
                continue
            write_control_receipt(
                self.store,
                self.spool_id,
                request,
                current_generation=self.generation,
                accepted=False,
                rejection_outcome=rejection_outcome,
            )
        return True

    def _receipt_exists_or_is_malformed(self, request_id: str) -> bool:
        """Preserve an unreadable receipt while allowing independent siblings."""
        try:
            return read_control_receipt(self.store, self.spool_id, request_id) is not None
        except MalformedControlReceipt as exc:
            if request_id not in self._reported_malformed_receipts:
                print(f"Spindle owner: {exc}", file=sys.stderr, flush=True)
                self._reported_malformed_receipts.add(request_id)
            return True

    def _settle_other_requests(self, accepted_request_id: str) -> bool:
        with mailbox_guard(self.store, self.spool_id):
            return self._settle_other_requests_unlocked(accepted_request_id)

    def _handle_request(self, request) -> int:
        self.checkpoints.reach("control_observed_before_ack", self.provider.pid)
        if not self._verify_lock():
            return -2
        with mailbox_guard(self.store, self.spool_id):
            receipt = write_control_receipt(
                self.store,
                self.spool_id,
                request,
                current_generation=self.generation,
                accepted=True,
            )
            if not self._settle_other_requests_unlocked(request.request_id):
                return -2
            if self.episode_mode:
                episode = self._read_spool().get("owner_episode") or {}
                accepted = transition_owner_episode(
                    self.store,
                    self.spool_id,
                    actor="owner",
                    destination="accepted",
                    generation=self.generation,
                    expected_revision=episode.get("revision"),
                    facts={
                        "winning_request": {
                            "request_id": request.request_id,
                            "kind": request.kind,
                            "desired_terminal_kind": request.desired_terminal_kind,
                        },
                        "acknowledgement": {"acknowledged_at": receipt.owner_acknowledged_at},
                    },
                )
                if not accepted.accepted:
                    return -2
        if receipt.owner_acknowledged_at is None:
            return -1
        if not self._verify_lock():
            return -2
        self._set_lifecycle(public_stop_state="stopping", desired_terminal_kind=request.desired_terminal_kind)
        self.checkpoints.reach("control_ack_durable", self.provider.pid)
        if not self._verify_lock():
            return -2
        attempted_at = _utc_now()
        acknowledged = self._send_provider_cancel()
        if not self._verify_lock():
            return -2
        update_control_receipt(
            self.store,
            self.spool_id,
            request.request_id,
            provider_cancel_attempted_at=attempted_at,
            provider_acknowledged_at=_utc_now() if acknowledged else None,
        )
        forced_started = None
        forced_completed = None
        if not self._wait_provider(0.25):
            forced_started = _utc_now()
            if not self._verify_lock():
                return -2
            update_control_receipt(
                self.store,
                self.spool_id,
                request.request_id,
                forced_cleanup_started_at=forced_started,
            )
            if not self._signal_provider_group(signal.SIGTERM):
                return -2
            if not self._wait_provider(0.2):
                self.checkpoints.reach("after_term_before_kill", self.provider.pid)
                if not self._signal_provider_group(signal.SIGKILL):
                    return -2
        returncode = self._finish_provider()
        if returncode is None:
            return -2
        descendants_clean = self._settle_descendants(force=True)
        while not descendants_clean:
            if forced_started is None:
                forced_started = _utc_now()
            if not self._verify_lock():
                return -2
            update_control_receipt(
                self.store,
                self.spool_id,
                request.request_id,
                forced_cleanup_started_at=forced_started,
                forced_cleanup_completed_at=None,
                child_exit_observed_at=None,
                cleanup_outcome="descendants_survived",
            )
            time.sleep(self.args.poll_interval)
            descendants_clean = self._settle_descendants(force=True)
        if forced_started is not None:
            forced_completed = _utc_now()
        child_exit = _utc_now()
        if not self._verify_lock():
            return -2
        update_control_receipt(
            self.store,
            self.spool_id,
            request.request_id,
            terminal_observed_at=child_exit if acknowledged else None,
            forced_cleanup_started_at=forced_started,
            forced_cleanup_completed_at=forced_completed,
            child_exit_observed_at=child_exit,
            cleanup_outcome="cleaned" if descendants_clean else "descendants_survived",
        )
        self.checkpoints.reach("cleanup_receipt_child_exit_durable", self.provider.pid)
        if not self._settle_other_requests(request.request_id):
            return -2
        if not self._write_exit_evidence(returncode, cleanup_outcome="stopped"):
            return -2
        if self.episode_mode:
            episode = self._read_spool().get("owner_episode") or {}
            cleaned = transition_owner_episode(
                self.store,
                self.spool_id,
                actor="owner",
                destination="cleanup_proven",
                generation=self.generation,
                expected_revision=episode.get("revision"),
                facts={
                    "cleanup": {
                        "outcome": "stopped",
                        "provider_reaped": True,
                        "adopted_children_reaped": self.adopted_reaped,
                        "child_exit_observed_at": child_exit,
                        "provider_exit_code": 128 - returncode if returncode < 0 else returncode,
                    }
                },
            )
            if not cleaned.accepted:
                return -2
        self.checkpoints.reach("cleanup_receipt_durable", self.provider.pid)
        self.checkpoints.reach("before_terminal_publish", self.provider.pid)
        if not self._verify_lock():
            return -2
        if not self.episode_mode:
            with self._spool_record_guard():
                spool = self._read_spool()
                lifecycle = dict(spool.get("lifecycle") or {})
                lifecycle.update(
                    {
                        "public_stop_state": None,
                        "transport_state": "reaped",
                        "normalized_terminal_kind": request.desired_terminal_kind,
                        "ownership_state": "held",
                    }
                )
                spool["lifecycle"] = lifecycle
                spool["status"] = "timeout" if request.desired_terminal_kind == "timeout" else "error"
                spool["error"] = (
                    f"Timeout after {spool.get('timeout')}s" if spool["status"] == "timeout" else "Cancelled"
                )
                spool["completed_at"] = _utc_now()
                self._write_spool_unlocked(spool)
        if not self._settle_other_requests(request.request_id):
            return -2
        return 0

    def run(self) -> int:
        if not self._await_launch_barrier():
            return 125
        self.episode_mode = (
            isinstance(self._read_spool().get("owner_episode"), dict) if hasattr(self, "spool_path") else False
        )
        if not self._watchdog_alive():
            self._abort_for_watchdog_loss_before_binding()
            return 125
        if self._deadline_expired():
            self._settle_deadline_expiry_before_binding()
            return 124
        _set_subreaper()
        self.lock = acquire_ownership_lock(self.lock_path)
        self._allocate_generation()
        self._ensure_wall_deadline()
        self.checkpoints.reach("identity_lock_acquired")
        self._publish_owner_identity()
        self.checkpoints.reach("identity_published")
        # Establish the timeout epoch before publishing provider readiness.
        # Callers may advance an injected clock as soon as readiness is
        # visible; sampling the epoch afterward would move the deadline by
        # that advance and leave the owner waiting forever at a frozen clock.
        started = self.clock.monotonic()
        remaining_wall_budget = self._remaining_wall_budget()
        monotonic_budget = self.args.timeout
        if monotonic_budget is not None and remaining_wall_budget is not None:
            monotonic_budget = min(float(self.args.timeout), remaining_wall_budget)
        if not self._watchdog_alive():
            self._contain_after_watchdog_loss_until_proven()
            return 125
        prestart_result = self._spawn_provider(started=started, monotonic_budget=monotonic_budget)
        if prestart_result is not None:
            self.checkpoints.reach("before_lock_release")
            self._verify_lock()
            self.lock.close()
            if getattr(self.args, "watchdog_fd", None) is not None:
                os.close(self.args.watchdog_fd)
                self.args.watchdog_fd = None
            return prestart_result
        accepted_request = None
        natural_exit_settled = False
        natural_cleanup = None
        result = 0
        try:
            while True:
                if not self._verify_lock():
                    time.sleep(self.args.poll_interval)
                    continue
                if not self._watchdog_alive():
                    self._contain_after_watchdog_loss_until_proven()
                    result = 125
                    break
                terminal_output, shutdown_due, terminal_written_at = self._terminal_output_state()
                if accepted_request is None:
                    accepted_request = self._next_current_request()
                terminal_precedes_deadline = terminal_output and self._terminal_output_precedes_deadline(
                    terminal_written_at
                )
                if accepted_request is None and self.args.timeout is not None and not terminal_precedes_deadline:
                    if self.clock.monotonic() - started >= monotonic_budget:
                        # Close the output/deadline observation race in favor of
                        # an already durable terminal protocol record.
                        terminal_output, shutdown_due, terminal_written_at = self._terminal_output_state()
                        if not (terminal_output and self._terminal_output_precedes_deadline(terminal_written_at)):
                            accepted_request = create_control_request(
                                self.store,
                                self.spool_id,
                                "timeout",
                                self.generation,
                                "logical-owner",
                                observer_namespace=capture_pid_namespace(),
                                reason="durable wall deadline elapsed",
                                deadline=self.wall_deadline_at,
                            )
                if accepted_request is not None and accepted_request.kind == "timeout":
                    # A supervisor may publish while the owner is between its
                    # output check and mailbox read. Recheck before accepting;
                    # only evidence written by the request's deadline wins.
                    terminal_output, shutdown_due, terminal_written_at = self._terminal_output_state()
                    if terminal_output and self._terminal_output_precedes_timeout(
                        accepted_request, terminal_written_at
                    ):
                        if not self._reject_timeout_for_terminal_output(accepted_request):
                            time.sleep(self.args.poll_interval)
                            continue
                        accepted_request = None
                if accepted_request is not None:
                    result = self._handle_request(accepted_request)
                    if result >= 0:
                        break
                    accepted_request = None
                if shutdown_due and not self._provider_exited():
                    if not self._signal_provider_group(signal.SIGTERM):
                        time.sleep(self.args.poll_interval)
                        continue
                    if not self._wait_provider(0.25) and not self._signal_provider_group(signal.SIGKILL):
                        time.sleep(self.args.poll_interval)
                        continue
                if self._provider_exited():
                    returncode = self._finish_provider()
                    descendants_clean = self._settle_descendants(force=False)
                    if not descendants_clean:
                        self._set_lifecycle(cleanup_outcome="descendants_survived")
                        time.sleep(self.args.poll_interval)
                        continue
                    if returncode is None or not self._write_exit_evidence(returncode, cleanup_outcome="natural_exit"):
                        time.sleep(self.args.poll_interval)
                        continue
                    if self.episode_mode:
                        natural_cleanup = {
                            "outcome": "natural_exit",
                            "provider_reaped": True,
                            "adopted_children_reaped": self.adopted_reaped,
                            "child_exit_observed_at": _utc_now(),
                            "provider_exit_code": 128 - returncode if returncode < 0 else returncode,
                        }
                    self.checkpoints.reach("natural_exit_evidence_published", self.provider.pid)
                    natural_exit_settled = True
                    result = returncode
                    break
                time.sleep(self.args.poll_interval)
        finally:
            if self.provider_pidfd is not None:
                os.close(self.provider_pidfd)
                self.provider_pidfd = None
            if self.control is not None:
                self.control.close()
            with mailbox_guard(self.store, self.spool_id):
                self.checkpoints.reach("final_mailbox_guard_acquired", self.provider.pid if self.provider else None)
                if self.episode_mode and natural_cleanup is not None:
                    episode = self._read_spool().get("owner_episode") or {}
                    cleaned = transition_owner_episode(
                        self.store,
                        self.spool_id,
                        actor="owner",
                        destination="cleanup_proven",
                        generation=self.generation,
                        expected_revision=episode.get("revision"),
                        facts={"cleanup": natural_cleanup},
                    )
                    if not cleaned.accepted:
                        result = -2
                if accepted_request is not None:
                    self._settle_other_requests_unlocked(accepted_request.request_id)
                elif natural_exit_settled:
                    self._settle_other_requests_unlocked(None, rejection_outcome="rejected_terminal")
                self.checkpoints.reach("before_lock_release", self.provider.pid if self.provider else None)
                self._verify_lock()
                self.lock.close()
            if getattr(self.args, "watchdog_fd", None) is not None:
                os.close(self.args.watchdog_fd)
                self.args.watchdog_fd = None
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--spool-id", required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--launch-barrier-fd", type=int)
    parser.add_argument("--checkpoint-fd", type=int)
    parser.add_argument("--pause-checkpoint")
    parser.add_argument("--clock-fd", type=int)
    parser.add_argument("--watchdog-fd", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--deadline")
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--stdin-path")
    parser.add_argument("--disable-pdeathsig", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        raise SystemExit("provider command is required after --")
    return LogicalOwner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
