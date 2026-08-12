"""Packaged crash-containment parent for the per-spool logical owner."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from .namespace_owner import (
    _atomic_json_write,
    _utc_now,
    read_proc_starttime,
    transition_owner_episode,
)
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


def _contain_adopted_descendants_until_clean() -> None:
    """Retain watchdog custody across confirmation deadlines until cleanup is proven."""
    while not _contain_adopted_descendants():
        time.sleep(0.01)


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


def _record_preacceptance_failure(
    store: Path,
    spool_id: str,
    owner_pid: int,
    owner_birth_token: str,
    owner_generation: int,
    status: int,
) -> bool:
    """Abort only the exact never-bound reservation; never bind a pathname."""
    spool_path = store / f"{spool_id}.json"
    spool = _load(spool_path) or {}
    episode = spool.get("owner_episode") or {}
    if episode.get("generation") != owner_generation or episode.get("phase") != "reserved":
        return False
    detail = "logical owner exited before binding"
    result = transition_owner_episode(
        store,
        spool_id,
        actor="watchdog",
        destination="aborted",
        generation=owner_generation,
        expected_revision=episode.get("revision"),
        facts={
            "failure": {
                "kind": "owner_preacceptance_failure",
                "detail": detail,
                "observed_at": _utc_now(),
                "owner_pid": owner_pid,
                "owner_birth_token": owner_birth_token,
                "owner_signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
                "owner_exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
            }
        },
    )
    if not result.accepted:
        return False
    with _record_guard(store, spool_id):
        current = _load(spool_path) or {}
        current_episode = current.get("owner_episode") or {}
        if current_episode.get("generation") != owner_generation or current_episode.get("phase") != "aborted":
            return False
        current.update(
            {
                "status": "error",
                "error": "Logical owner exited before identity publication",
                "error_kind": "owner_preacceptance_failure",
                "failed_owner_generation": owner_generation,
                "owner_generation": owner_generation,
                "completed_at": _utc_now(),
            }
        )
        lifecycle = dict(current.get("lifecycle") or {})
        lifecycle.pop("public_stop_state", None)
        lifecycle["transport_state"] = "reaped"
        lifecycle["normalized_terminal_kind"] = "failed"
        current["lifecycle"] = lifecycle
        _atomic_json_write(spool_path, current)
    return True


def _record_owner_crash(
    store: Path,
    spool_id: str,
    owner_pid: int,
    owner_generation: int,
    owner_birth_token: str,
    status: int,
    contained: bool,
) -> None:
    process_path = store / f"{spool_id}.process-identity"
    exit_path = store / f"{spool_id}.owner-exit"
    process = _load(process_path) or {}
    existing_exit = _load(exit_path)
    owner_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    owner_exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None

    spool = _load(store / f"{spool_id}.json") or {}
    episode = spool.get("owner_episode") or {}
    if episode:
        if episode.get("generation") != owner_generation:
            return
        if episode.get("phase") == "reserved":
            _record_preacceptance_failure(
                store,
                spool_id,
                owner_pid,
                owner_birth_token,
                owner_generation,
                status,
            )
            return
    elif spool.get("owner_generation") != owner_generation or process.get("owner_generation") != owner_generation:
        # Direct/internal compatibility owners deliberately have no episode.
        # Retain the same generation guard using their durable flat/process
        # mirrors, then publish the legacy evidence reconciliation consumes.
        return

    if episode and episode.get("phase") in {"lock_bound", "accepted"}:
        observed_at = _utc_now()
        lifecycle = dict(spool.get("lifecycle") or {})
        lifecycle["public_stop_state"] = "stopping"
        transition_owner_episode(
            store,
            spool_id,
            actor="watchdog",
            destination="cleanup_proven",
            generation=owner_generation,
            expected_revision=episode.get("revision"),
            facts={
                "containment": {
                    "contained": contained,
                    "adopted_children_reaped": 0,
                    "observed_at": observed_at,
                },
                "cleanup": {
                    "outcome": "watchdog_contained" if contained else "descendants_survived",
                    "provider_reaped": contained,
                    "adopted_children_reaped": 0,
                    "child_exit_observed_at": observed_at,
                    "provider_exit_code": None,
                },
                "failure": {
                    "kind": "owner_crash",
                    "detail": "logical owner crashed",
                    "observed_at": observed_at,
                    "owner_signal": owner_signal,
                    "owner_exit_code": owner_exit_code,
                },
            },
            record_updates={"lifecycle": lifecycle},
        )

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
    watchdog_read, watchdog_write = os.pipe()
    owner_pid = os.fork()
    if owner_pid == 0:
        os.close(error_read)
        os.close(watchdog_write)
        delimiter = raw.index("--") if "--" in raw else len(raw)
        raw[delimiter:delimiter] = ["--watchdog-fd", str(watchdog_read)]
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
    os.close(watchdog_read)
    try:
        owner_birth_token = read_proc_starttime(owner_pid)
    except OSError:
        # The child is still retained by this parent and its terminal identity
        # is published only after waitpid.  An unavailable token therefore
        # fails closed on any later PID reuse while still allowing ESRCH pidfd
        # evidence to prove this reaped generation dead.
        owner_birth_token = "unavailable"
    try:
        _waited, status = os.waitpid(owner_pid, 0)
    finally:
        os.close(watchdog_write)
    exception_reported = bool(os.read(error_read, 4096))
    os.close(error_read)
    crashed = _owner_process_crashed(status, exception_reported=exception_reported)
    if crashed:
        _contain_adopted_descendants_until_clean()
        _record_owner_crash(store, spool_id, owner_pid, owner_generation, owner_birth_token, status, True)
    else:
        # A normal owner drains its own adopted children.  Reap anything which
        # crossed the parent boundary during the final exit instructions.
        _drain_reapable()
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)
