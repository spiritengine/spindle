"""Packaged logical owner for one provider process and its descendants."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .namespace_owner import (
    ProcessIdentity,
    _atomic_json_write,
    _utc_now,
    acquire_ownership_lock,
    capture_pid_namespace,
    create_control_request,
    iter_control_requests,
    parse_proc_stat_starttime,
    read_control_receipt,
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

    def _read_spool(self) -> dict:
        try:
            return json.loads(self.spool_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"id": self.spool_id, "status": "pending", "created_at": _utc_now()}

    def _write_spool(self, spool: dict) -> None:
        _atomic_json_write(self.spool_path, spool)

    def _update_spool(self, **values) -> dict:
        spool = self._read_spool()
        spool.update(values)
        self._write_spool(spool)
        return spool

    def _set_lifecycle(self, **values) -> dict:
        spool = self._read_spool()
        lifecycle = dict(spool.get("lifecycle") or {})
        lifecycle.update(values)
        spool["lifecycle"] = lifecycle
        self._write_spool(spool)
        return spool

    def _allocate_generation(self) -> None:
        try:
            previous = json.loads(self.owner_identity_path.read_text()).get("owner_generation", 0)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            previous = 0
        if self.generation <= previous:
            self.generation = previous + 1
            self.checkpoints.generation = self.generation

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
        _atomic_json_write(self.owner_identity_path, identity.to_dict())
        (self.store / f"{self.spool_id}.journal-guard").touch(exist_ok=True)
        (self.store / f"{self.spool_id}.control-mailbox").mkdir(exist_ok=True)
        return identity

    def _verify_lock(self) -> bool:
        if not os.access(self.lock_path, os.R_OK | os.W_OK):
            self._set_lifecycle(ownership_state="unreadable")
            return False
        try:
            descriptor = os.fstat(self.lock.fd)
            pathname = os.stat(self.lock_path)
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
        lifecycle = (self._read_spool().get("lifecycle") or {})
        if lifecycle.get("ownership_state") == "unreadable":
            self._set_lifecycle(ownership_state="held")
        return True

    def _spawn_provider(self) -> None:
        owner_end, child_end = socket.socketpair()
        owner_end.set_inheritable(False)
        child_end.set_inheritable(True)
        provider_env = os.environ.copy()
        provider_env.pop("_SPINDLE_STORE_SUPERVISOR", None)
        provider_env["SPINDLE_PROVIDER_CONTROL_FD"] = str(child_end.fileno())
        provider_env["SPINDLE_OWNER_STORE"] = str(self.store)
        provider_env["SPINDLE_OWNER_SPOOL_ID"] = self.spool_id
        stdin_stream = open(self.args.stdin_path, "r") if self.args.stdin_path else subprocess.DEVNULL
        try:
            with open(self.stdout_path, "w") as stdout, open(self.stderr_path, "w") as stderr:
                self.checkpoints.reach("provider_forked_before_containment_armed")
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
        finally:
            child_end.close()
            if stdin_stream is not subprocess.DEVNULL:
                stdin_stream.close()
        self.control = owner_end
        self.control.setblocking(False)
        self.provider_pgid = self.provider.pid
        self.provider_birth = _starttime(self.provider.pid)
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
        spool = self._read_spool()
        spool.update(
            {
                "status": "running",
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
        self._write_spool(spool)
        self.checkpoints.reach("provider_ready", self.provider.pid)
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

    def _signal_provider_group(self, sig: int) -> None:
        if self.provider_pgid is None:
            return
        if self.provider is not None and self.provider.poll() is None:
            try:
                if _starttime(self.provider.pid) != self.provider_birth:
                    raise RuntimeError("provider PID identity changed before signal")
            except FileNotFoundError:
                return
        try:
            os.killpg(self.provider_pgid, sig)
        except ProcessLookupError:
            pass

    def _finish_provider(self) -> int:
        if self.provider is None:
            return 127
        try:
            return self.provider.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._signal_provider_group(signal.SIGKILL)
            return self.provider.wait(timeout=1)

    def _write_exit_evidence(self, returncode: int, *, cleanup_outcome: str) -> None:
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

    def _handle_request(self, request) -> int:
        self.checkpoints.reach("control_observed_before_ack", self.provider.pid)
        receipt = write_control_receipt(
            self.store,
            self.spool_id,
            request,
            current_generation=self.generation,
        )
        if receipt.owner_acknowledged_at is None:
            return -1
        self._set_lifecycle(public_stop_state="stopping", desired_terminal_kind=request.desired_terminal_kind)
        self.checkpoints.reach("control_ack_durable", self.provider.pid)
        attempted_at = _utc_now()
        acknowledged = self._send_provider_cancel()
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
            update_control_receipt(
                self.store,
                self.spool_id,
                request.request_id,
                forced_cleanup_started_at=forced_started,
            )
            self._signal_provider_group(signal.SIGTERM)
            if not self._wait_provider(0.2):
                self.checkpoints.reach("after_term_before_kill", self.provider.pid)
                self._signal_provider_group(signal.SIGKILL)
        returncode = self._finish_provider()
        descendants_clean = self._settle_descendants(force=True)
        if forced_started is None and not descendants_clean:
            forced_started = _utc_now()
        if forced_started is not None:
            forced_completed = _utc_now()
        child_exit = _utc_now()
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
        self._write_exit_evidence(returncode, cleanup_outcome="stopped")
        self.checkpoints.reach("cleanup_receipt_durable", self.provider.pid)
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
        spool["error"] = "Timeout" if spool["status"] == "timeout" else "Cancelled"
        spool["completed_at"] = _utc_now()
        self.checkpoints.reach("before_terminal_publish", self.provider.pid)
        self._write_spool(spool)
        return 0

    def run(self) -> int:
        _set_subreaper()
        self.lock = acquire_ownership_lock(self.lock_path)
        self._allocate_generation()
        self.checkpoints.reach("identity_lock_acquired")
        self._publish_owner_identity()
        self.checkpoints.reach("identity_published")
        self._spawn_provider()
        started = self.clock.monotonic()
        accepted_request = None
        result = 0
        try:
            while True:
                if not self._verify_lock():
                    time.sleep(self.args.poll_interval)
                    continue
                if accepted_request is None:
                    for request in iter_control_requests(self.store, self.spool_id):
                        if read_control_receipt(self.store, self.spool_id, request.request_id) is None:
                            accepted_request = request
                            break
                if accepted_request is None and self.args.timeout is not None:
                    if self.clock.monotonic() - started >= self.args.timeout:
                        accepted_request = create_control_request(
                            self.store,
                            self.spool_id,
                            "timeout",
                            self.generation,
                            "logical-owner",
                            observer_namespace=capture_pid_namespace(),
                        )
                if accepted_request is not None:
                    result = self._handle_request(accepted_request)
                    if result >= 0:
                        break
                    accepted_request = None
                if self._provider_exited():
                    returncode = self._finish_provider()
                    self._settle_descendants(force=False)
                    self._write_exit_evidence(returncode, cleanup_outcome="natural_exit")
                    result = returncode
                    break
                time.sleep(self.args.poll_interval)
        finally:
            if self.provider_pidfd is not None:
                os.close(self.provider_pidfd)
                self.provider_pidfd = None
            if self.control is not None:
                self.control.close()
            self.checkpoints.reach("before_lock_release", self.provider.pid if self.provider else None)
            self.lock.close()
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--spool-id", required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--checkpoint-fd", type=int)
    parser.add_argument("--pause-checkpoint")
    parser.add_argument("--clock-fd", type=int)
    parser.add_argument("--timeout", type=float)
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
