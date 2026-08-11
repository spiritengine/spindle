"""Focused fixtures for the namespace-safe per-spool owner contract."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@pytest.fixture
def namespace_owner_env(tmp_path):
    home = tmp_path / "home"
    user_home = tmp_path / "user-home"
    private_bin = tmp_path / "bin"
    store = home / "spools"
    for path in (user_home, private_bin, store):
        path.mkdir(parents=True)
    real_store = Path.home() / ".spindle" / "spools"
    before = frozenset(p.name for p in real_store.iterdir()) if real_store.exists() else frozenset()
    env = {
        "SPINDLE_HOME": str(home),
        "HOME": str(user_home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.pathsep.join((str(private_bin), os.environ.get("PATH", ""))),
    }
    return {
        "env": env,
        "home": home,
        "user_home": user_home,
        "bin": private_bin,
        "store": store,
        "real_store": real_store,
        "real_store_before": before,
    }


@pytest.fixture
def fake_provider_factory(namespace_owner_env):
    created = []

    def factory(mode="cooperative"):
        path = namespace_owner_env["bin"] / f"fake-provider-{len(created)}"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import os, signal, time\n"
            f"MODE = {mode!r}\n"
            "print(f'ready {os.getpid()} {os.getpgrp()}', flush=True)\n"
            "if MODE == 'immediate-exit': raise SystemExit(0)\n"
            "if MODE == 'silent-exit': raise SystemExit(17)\n"
            "if MODE == 'ignore-term': signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "while True: time.sleep(0.05)\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        created.append(path)
        return path

    return factory


@pytest.fixture
def owner_checkpoint():
    controller, owner = socket.socketpair()
    yield controller, owner
    controller.close()
    owner.close()


@pytest.fixture
def owner_clock():
    controller, owner = socket.socketpair()
    current = {"value": 0.0}

    def advance(seconds):
        current["value"] += seconds
        controller.sendall(f"{current['value']}\n".encode())

    yield current, advance, owner
    controller.close()
    owner.close()


@dataclass
class FakeProcessOps:
    namespace: object = None
    process_namespace: object = None
    starttime: object = "101"
    pidfd_result: object = 91
    pidfd_readable: bool = False
    calls: list = field(default_factory=list)

    def current_namespace(self):
        self.calls.append(("current_namespace",))
        return self.namespace

    def namespace_for_pid(self, pid):
        self.calls.append(("namespace_for_pid", pid))
        if isinstance(self.process_namespace, BaseException):
            raise self.process_namespace
        return self.process_namespace if self.process_namespace is not None else self.namespace

    def read_starttime(self, pid):
        self.calls.append(("read_starttime", pid))
        if isinstance(self.starttime, BaseException):
            raise self.starttime
        return self.starttime

    def pidfd_open(self, pid):
        self.calls.append(("pidfd_open", pid))
        if isinstance(self.pidfd_result, BaseException):
            raise self.pidfd_result
        return self.pidfd_result

    def pidfd_is_readable(self, fd):
        self.calls.append(("pidfd_is_readable", fd))
        return self.pidfd_readable

    def close(self, fd):
        self.calls.append(("close", fd))

    def signal_pid(self, pid, sig):
        self.calls.append(("signal_pid", pid, sig))
        raise AssertionError("observer attempted to signal a PID")

    def signal_group(self, pgid, sig):
        self.calls.append(("signal_group", pgid, sig))
        raise AssertionError("observer attempted to signal a process group")


@pytest.fixture
def fake_process_ops():
    return FakeProcessOps()


@pytest.fixture
def proc_stat_record():
    def factory(starttime="424242", comm="odd ) 17 18 ( name\nmore"):
        tail = ["S"] + [str(i) for i in range(4, 22)] + [str(starttime), "23", "24"]
        return f"123 ({comm}) " + " ".join(tail)

    return factory


@pytest.fixture
def process_identity_record():
    from spindle.namespace_owner import NamespaceIdentity, ProcessIdentity

    namespace = NamespaceIdentity.supported(7, 11)
    cases = {
        "same_namespace_alive": dict(pid=123, birth_token="101", namespace=namespace, lock_created=True),
        "same_namespace_dead": dict(pid=123, birth_token="101", namespace=namespace, lock_created=True),
        "foreign_namespace": dict(
            pid=123,
            birth_token="101",
            namespace=NamespaceIdentity.supported(7, 12),
            lock_created=True,
        ),
        "pid_reused": dict(pid=123, birth_token="old", namespace=namespace, lock_created=True),
        "legacy_missing_lock": dict(pid=123, birth_token="101", namespace=namespace, lock_created=False),
        "current_missing_lock": dict(pid=123, birth_token="101", namespace=namespace, lock_created=True),
        "replaced_lock": dict(pid=123, birth_token="101", namespace=namespace, lock_created=True),
        "unreadable_lock": dict(pid=123, birth_token="101", namespace=namespace, lock_created=True),
    }

    def factory(name="same_namespace_alive", **overrides):
        values = {
            **cases[name],
            "owner_generation": 3,
            "child_pgid": 123,
            "lock_device": 8,
            "lock_inode": 9,
        }
        values.update(overrides)
        return ProcessIdentity(**values)

    return factory


@dataclass
class HeldLock:
    path: Path
    fd: int
    device: int
    inode: int

    def replace_path(self):
        self.path.unlink()
        replacement = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.close(replacement)

    def close(self):
        import fcntl

        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


@pytest.fixture
def lock_holder():
    import fcntl

    holders = []

    def factory(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        holder = HeldLock(Path(path), fd, info.st_dev, info.st_ino)
        holders.append(holder)
        return holder

    yield factory
    for holder in holders:
        try:
            holder.close()
        except OSError:
            pass


@pytest.fixture
def legacy_root_sweeper():
    def sweep(root):
        root = Path(root)
        return set(root.glob("*.json")), set(root.glob("*.lock"))

    return sweep


@pytest.fixture
def reconciliation_spy():
    class Spy:
        def __init__(self, result):
            self.result = result
            self.callers = []

        def __call__(self, spool, *, caller=None, **_kwargs):
            self.callers.append(caller)
            return self.result

    return Spy


@pytest.fixture
def process_ledger():
    processes = []

    def record(proc):
        processes.append(proc)
        return proc

    yield record
    for proc in reversed(processes):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
