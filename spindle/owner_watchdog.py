"""Packaged crash-containment parent for the per-spool logical owner."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from .namespace_owner import _atomic_json_write, _utc_now
from .namespace_owner_process import _set_subreaper
from .namespace_owner_process import main as owner_main


def _direct_children() -> list[int]:
    try:
        return [int(value) for value in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()]
    except (FileNotFoundError, OSError, ValueError):
        return []


def _drain_reapable() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _contain_adopted_descendants(timeout: float = 3.0) -> bool:
    """Kill every child adopted after owner death, including later reparents."""
    deadline = time.monotonic() + timeout
    empty_scans = 0
    while time.monotonic() < deadline:
        _drain_reapable()
        children = _direct_children()
        if not children:
            empty_scans += 1
            if empty_scans >= 3:
                return True
            time.sleep(0.01)
            continue
        empty_scans = 0
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.01)
    _drain_reapable()
    return not _direct_children()


@contextmanager
def _record_guard(store: Path, spool_id: str):
    lock_path = store / f"{spool_id}.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
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


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return None


def _record_preacceptance_failure(store: Path, spool_id: str, owner_generation: int) -> None:
    with _record_guard(store, spool_id):
        spool_path = store / f"{spool_id}.json"
        spool = _load(spool_path)
        if not spool or spool.get("status") not in {"pending", "running"}:
            return
        if spool.get("replacement_starting") and spool.get("owner_generation") != owner_generation:
            return
        spool.update(
            {
                "status": "error",
                "error": "Logical owner exited before identity publication",
                "error_kind": "owner_preacceptance_failure",
                "failed_owner_generation": owner_generation,
                "completed_at": _utc_now(),
            }
        )
        spool.pop("replacement_starting", None)
        spool.pop("replacement_owner_generation", None)
        lifecycle = dict(spool.get("lifecycle") or {})
        lifecycle.pop("public_stop_state", None)
        spool["lifecycle"] = lifecycle
        _atomic_json_write(spool_path, spool)


def _record_owner_crash(
    store: Path,
    spool_id: str,
    owner_pid: int,
    owner_generation: int,
    status: int,
    contained: bool,
) -> None:
    identity_path = store / f"{spool_id}.owner-identity"
    process_path = store / f"{spool_id}.process-identity"
    exit_path = store / f"{spool_id}.owner-exit"
    identity = _load(identity_path)
    process = _load(process_path) or {}
    existing_exit = _load(exit_path)
    owner_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    owner_exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None

    if (
        identity is None
        or identity.get("pid") != owner_pid
        or identity.get("owner_generation") != owner_generation
    ):
        # The stable ownership inode may exist, but no running identity was
        # accepted and no provider was published.  This is the one owner crash
        # phase that can be classified as a prelaunch failure immediately.
        _record_preacceptance_failure(store, spool_id, owner_generation)
        return

    generation = owner_generation
    if existing_exit and existing_exit.get("owner_generation") == generation:
        evidence = dict(existing_exit)
        evidence.update(
            {
                "owner_crashed_after_cleanup": True,
                "watchdog_contained": contained,
                "owner_signal": owner_signal,
                "owner_exit_code": owner_exit_code,
                "watchdog_observed_at": _utc_now(),
            }
        )
    else:
        evidence = {
            "owner_pid": owner_pid,
            "owner_generation": generation,
            "provider_pid": process.get("provider_pid"),
            "provider_exit_code": None,
            "provider_reaped": contained,
            "adopted_children_reaped": None,
            "cleanup_outcome": "watchdog_contained" if contained else "descendants_survived",
            "owner_crashed": True,
            "watchdog_contained": contained,
            "owner_signal": owner_signal,
            "owner_exit_code": owner_exit_code,
            "observed_at": _utc_now(),
        }
    _atomic_json_write(exit_path, evidence)


def _owner_process_crashed(status: int, *, exception_reported: bool) -> bool:
    """Keep ordinary owner return codes disjoint from the crash channel."""
    return os.WIFSIGNALED(status) or exception_reported


def main(argv=None) -> int:
    """Fork the logical owner below a longer-lived containment subreaper."""
    _set_subreaper()
    args = list(argv) if argv is not None else None
    # Parse only the two values the watchdog needs.  The child still owns the
    # authoritative parser and receives the byte-for-byte argument vector.
    import sys

    raw = list(sys.argv[1:] if args is None else args)
    store = Path(raw[raw.index("--store") + 1]).resolve()
    spool_id = raw[raw.index("--spool-id") + 1]
    owner_generation = int(raw[raw.index("--generation") + 1]) if "--generation" in raw else 1
    error_read, error_write = os.pipe()
    owner_pid = os.fork()
    if owner_pid == 0:
        os.close(error_read)
        try:
            code = owner_main(raw)
        except BaseException as exc:
            try:
                os.write(error_write, repr(exc).encode(errors="replace")[:4096])
            except OSError:
                pass
            os._exit(1)
        finally:
            os.close(error_write)
        os._exit(int(code) & 0xFF)

    os.close(error_write)
    _waited, status = os.waitpid(owner_pid, 0)
    exception_reported = bool(os.read(error_read, 4096))
    os.close(error_read)
    crashed = _owner_process_crashed(status, exception_reported=exception_reported)
    if crashed:
        contained = _contain_adopted_descendants()
        _record_owner_crash(store, spool_id, owner_pid, owner_generation, status, contained)
    else:
        # A normal owner drains its own adopted children.  Reap anything which
        # crossed the parent boundary during the final exit instructions.
        _drain_reapable()
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)
