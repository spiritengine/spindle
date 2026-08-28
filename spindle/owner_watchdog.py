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
    _cleanup_locked_fd,
    _owner_exit_from_dict,
    _positive_json_integer,
    _process_identity_sidecar_from_dict,
    _type_strict_json_equal,
    _utc_now,
    read_proc_starttime,
    transition_owner_episode,
)
from .namespace_owner_process import (
    AUTHORITY_LOST_DISPOSITION,
    CONTAINMENT_BOUND_SECONDS,
    DEFAULT_CONTAINMENT_BOUND_SECONDS,
    _parse_generation_argument,
    _set_subreaper,
)
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


def _contain_adopted_descendants(timeout: float = DEFAULT_CONTAINMENT_BOUND_SECONDS) -> bool:
    """Kill every child adopted after owner death, including later reparents.

    The bound is defined beside the owner because both halves of the pair use
    it: the owner takes one pass over its own live kernel children before
    exiting, and this takes a second over what reparents here afterwards.
    Genuine owner crashes still use the unbounded
    _contain_adopted_descendants_until_clean below - the bound applies only to
    the disposition-driven takeover, where the owner has already proven it
    holds no authority to keep retrying under.
    """
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
        _cleanup_locked_fd(fd)


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, RecursionError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _same_generation(recorded, expected) -> bool:
    try:
        recorded = _positive_json_integer(recorded, "recorded owner generation")
        expected = _positive_json_integer(expected, "expected owner generation")
    except ValueError:
        return False
    return _type_strict_json_equal(recorded, expected)


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
    if not _same_generation(episode.get("generation"), owner_generation) or episode.get("phase") != "reserved":
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
    raw_process = _load(process_path)
    try:
        process = _process_identity_sidecar_from_dict(raw_process) if raw_process is not None else {}
    except (TypeError, ValueError):
        process = None
    raw_existing_exit = _load(exit_path)
    try:
        existing_exit = _owner_exit_from_dict(raw_existing_exit) if raw_existing_exit is not None else None
    except (TypeError, ValueError):
        existing_exit = None
    owner_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
    owner_exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None

    spool = _load(store / f"{spool_id}.json") or {}
    episode = spool.get("owner_episode") or {}
    if episode:
        if not _same_generation(episode.get("generation"), owner_generation):
            return
        if episode.get("phase") == "reserved":
            published = _record_preacceptance_failure(
                store,
                spool_id,
                owner_pid,
                owner_birth_token,
                owner_generation,
                status,
            )
            if published:
                # The watchdog remains an evidence producer.  Once that write
                # is durable it may notify the sole applicator in this process;
                # direct calls to _record_preacceptance_failure still publish
                # evidence only.
                import spindle

                from .owner_episode_convergence import ObserverIdentity, converge_owner_episode

                spindle.SPINDLE_DIR = store
                converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
            return
    elif (
        process is None
        or not _same_generation(spool.get("owner_generation"), owner_generation)
        or not _same_generation(process.get("owner_generation"), owner_generation)
    ):
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
    if existing_exit and _same_generation(existing_exit.get("owner_generation"), generation):
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
            # A malformed sidecar carries no usable identity evidence.  The
            # episode transition above still records the crash and containment
            # result; do not recover one field by partially trusting it.
            "provider_pid": process.get("provider_pid") if process is not None else None,
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


def _classify_owner_termination(status: int, *, exception_reported: bool, authority_lost: bool) -> str:
    """Return 'authority_lost', 'crashed', or 'natural' - never the exit code.

    ``authority_lost`` comes solely from the private disposition pipe, so
    this classification (and everything it gates: whether the watchdog
    writes crash evidence, whether containment is bounded or retried until
    clean) is disjoint from the 0-255 exit status space by construction, the
    same way ``_owner_process_crashed`` already keeps the crash channel
    disjoint from ordinary return codes.
    """
    if authority_lost:
        return "authority_lost"
    if _owner_process_crashed(status, exception_reported=exception_reported):
        return "crashed"
    return "natural"


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
    owner_generation = _parse_generation_argument(raw[raw.index("--generation") + 1]) if "--generation" in raw else 1
    error_read, error_write = os.pipe()
    watchdog_read, watchdog_write = os.pipe()
    disposition_read, disposition_write = os.pipe()
    owner_pid = os.fork()
    if owner_pid == 0:
        os.close(error_read)
        os.close(watchdog_write)
        os.close(disposition_read)
        delimiter = raw.index("--") if "--" in raw else len(raw)
        raw[delimiter:delimiter] = [
            "--watchdog-fd",
            str(watchdog_read),
            "--disposition-fd",
            str(disposition_write),
        ]
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
    os.close(disposition_write)
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
    # The owner already exited by the time waitpid returns above, so its copy
    # of disposition_write is closed and this read cannot block: it returns
    # whatever marker was written, or b"" if none was.
    authority_lost = os.read(disposition_read, 4096) == AUTHORITY_LOST_DISPOSITION
    os.close(disposition_read)
    classification = _classify_owner_termination(
        status, exception_reported=exception_reported, authority_lost=authority_lost
    )
    if classification == "authority_lost":
        # The owner has already proven it holds no authority to act on this
        # store.  Take one bounded custody pass over whatever it leaves
        # behind (a still-live provider reparents to this subreaper the
        # moment the owner exits) and converge either way: no crash evidence,
        # no unbounded retry, because there is no further authority to prove
        # an incomplete pass true against.
        _contain_adopted_descendants(timeout=CONTAINMENT_BOUND_SECONDS)
    elif classification == "crashed":
        _contain_adopted_descendants_until_clean()
        _record_owner_crash(store, spool_id, owner_pid, owner_generation, owner_birth_token, status, True)
    else:
        # A normal owner drains its own adopted children.  Reap anything which
        # crossed the parent boundary during the final exit instructions.
        _drain_reapable()
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)
