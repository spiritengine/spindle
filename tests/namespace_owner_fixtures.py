"""Focused fixtures for the namespace-safe per-spool owner contract."""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
        if mode == "missing-interpreter":
            path.write_text("#!/definitely/missing/spindle-test-interpreter\n")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            created.append(path)
            return path
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, signal, socket, sys, time\n"
            "from pathlib import Path\n"
            f"MODE = {mode!r}\n"
            "STORE = Path(os.environ['SPINDLE_OWNER_STORE'])\n"
            "SPOOL_ID = os.environ['SPINDLE_OWNER_SPOOL_ID']\n"
            "CONTROL_FD = int(os.environ['SPINDLE_PROVIDER_CONTROL_FD'])\n"
            "control = socket.socket(fileno=CONTROL_FD)\n"
            "if MODE == 'gemini-terminal-linger':\n"
            "    print(json.dumps({'response': 'durable gemini result', 'session_id': 'gemini-session'}), flush=True)\n"
            "    time.sleep(10.0)\n"
            "    raise SystemExit(0)\n"
            "if MODE == 'gemini-incomplete-linger':\n"
            "    print('{incomplete', flush=True)\n"
            "    time.sleep(1.0)\n"
            "    raise SystemExit(0)\n"
            "if MODE == 'codex-terminal-linger':\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n"
            "    time.sleep(1.0)\n"
            "    raise SystemExit(0)\n"
            "if MODE in {'claude-terminal-linger', 'claude-intermediate-linger'}:\n"
            "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
            "        'session_id': 'claude-session', 'result': 'durable claude result'}), flush=True)\n"
            "    if MODE == 'claude-terminal-linger':\n"
            "        print(json.dumps({'type': 'spindle_driver_terminal', 'subtype': 'complete',\n"
            "            'protocol': 'spindle-claude-driver/1', 'unresolved_tasks': []}), flush=True)\n"
            "    time.sleep(10.0 if MODE == 'claude-terminal-linger' else 1.0)\n"
            "    raise SystemExit(0)\n"
            "if MODE in {'setsid-grandchild', 'setsid-grandchild-causal'}:\n"
            "    child = os.fork()\n"
            "    if child == 0:\n"
            "        os.setsid()\n"
            "        signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "        while True: time.sleep(0.05)\n"
            "    (STORE / f'{SPOOL_ID}.descendant-pid').write_text(str(child))\n"
            "    if MODE == 'setsid-grandchild-causal':\n"
            "        with (STORE / f'{SPOOL_ID}.descendant-ready').open('w') as ready:\n"
            "            ready.write(f'{child}\\n')\n"
            "print(f'ready {os.getpid()} {os.getpgrp()}', flush=True)\n"
            "if MODE == 'immediate-exit': raise SystemExit(0)\n"
            "if MODE == 'silent-exit': raise SystemExit(17)\n"
            "if MODE.startswith('exit-'): raise SystemExit(int(MODE.split('-', 1)[1]))\n"
            "if MODE == 'record-launch':\n"
            "    targets = []\n"
            "    for item in Path('/proc/self/fd').iterdir():\n"
            "        try: targets.append(os.readlink(item))\n"
            "        except OSError: pass\n"
            "    (STORE / f'{SPOOL_ID}.launch-record').write_text(json.dumps({\n"
            "        'argv': sys.argv, 'env': os.environ.get('OWNER_TEST_ENV'),\n"
            "        'cwd': os.getcwd(), 'fds': targets}))\n"
            "    raise SystemExit(0)\n"
            "if MODE == 'fork-burst':\n"
            "    for _ in range(6):\n"
            "        child = os.fork()\n"
            "        if child == 0:\n"
            "            time.sleep(0.05)\n"
            "            raise SystemExit(0)\n"
            "    raise SystemExit(0)\n"
            "if MODE == 'ignore-term':\n"
            "    signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "    while True: time.sleep(0.05)\n"
            "if MODE == 'healthy-turn':\n"
            "    print('partial provider output', flush=True)\n"
            "message = control.recv(4096)\n"
            "if b'cancel' in message:\n"
            "    control.sendall(b'ack\\n')\n"
            "raise SystemExit(0)\n"
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
    stopped = threading.Event()
    primed = threading.Event()
    errors = []

    def advance(seconds):
        if not primed.wait(timeout=2):
            raise AssertionError("owner clock was not primed before readiness")
        current["value"] += seconds

    def respond():
        pending = b""
        while not stopped.is_set():
            try:
                chunk = controller.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line == b"now":
                    try:
                        controller.sendall(f"{current['value']}\n".encode())
                    except OSError as exc:
                        if stopped.is_set() or exc.errno in {
                            errno.EBADF,
                            errno.EPIPE,
                            errno.ECONNRESET,
                            errno.ENOTCONN,
                        }:
                            return
                        errors.append(exc)
                        return
                    primed.set()
                else:
                    errors.append(AssertionError(f"unexpected owner clock request: {line!r}"))
                    return

    responder = threading.Thread(target=respond, daemon=True)
    responder.start()
    yield current, advance, owner
    stopped.set()
    controller.close()
    owner.close()
    responder.join(timeout=1)
    if responder.is_alive():
        pytest.fail("owner clock responder did not stop")
    if errors:
        pytest.fail(f"owner clock responder failed: {errors[0]!r}")


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


@dataclass
class ReconciliationSpy:
    """Predetermined unified result with opt-in mutation tripwires."""

    monkeypatch: object
    state: str = "unverifiable"
    calls: list = field(default_factory=list)
    violations: list = field(default_factory=list)

    def __call__(self, spool):
        from spindle.namespace_owner import LivenessEvidence, LockEvidence, ReconciliationResult

        self.calls.append(spool.get("id"))
        return ReconciliationResult(
            self.state,
            f"spy_{self.state}",
            LivenessEvidence("unverifiable", "namespace_mismatch"),
            LockEvidence("held", detail="foreign-owner"),
        )

    def forbid_mutation(self):
        import spindle

        def forbidden(*_args, **_kwargs):
            self.violations.append((_args, _kwargs))
            raise AssertionError("PID-sensitive observer attempted destructive mutation")

        for name in (
            "_is_pid_alive",
            "_is_process_group_alive",
            "_terminate_process_group",
            "_process_start_time",
            "_write_spool",
            "_cleanup_shard",
            "retire_owner_artifacts",
        ):
            self.monkeypatch.setattr(spindle, name, forbidden)
        self.monkeypatch.setattr(Path, "unlink", forbidden)


@pytest.fixture
def reconciliation_spy(monkeypatch):
    import spindle

    spy = ReconciliationSpy(monkeypatch)
    monkeypatch.setattr(spindle, "_reconcile_spool_ownership", spy)
    return spy


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
class WatchdogOwnerHandle:
    process: subprocess.Popen
    store: Path
    spool_id: str
    ready: dict
    checkpoint: socket.socket | None
    initial_checkpoint: dict | None = None

    def spool(self):
        try:
            return json.loads((self.store / f"{self.spool_id}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @property
    def owner_pid(self):
        if self.initial_checkpoint:
            return self.initial_checkpoint["owner_pid"]
        if self.ready.get("owner_pid"):
            return self.ready["owner_pid"]
        try:
            return json.loads((self.store / f"{self.spool_id}.owner-identity").read_text())["pid"]
        except FileNotFoundError:
            raise

    @property
    def provider_pid(self):
        return self.ready.get("provider_pid") or (self.initial_checkpoint or {}).get("provider_pid")

    def request(self, kind="cancel"):
        from spindle.namespace_owner import create_control_request

        generation = self.spool().get("owner_generation") or self.ready.get("owner_generation", 1)
        return create_control_request(self.store, self.spool_id, kind, generation, "stage4-test")

    def receive_checkpoint(self, timeout=5):
        if self.initial_checkpoint is not None:
            value, self.initial_checkpoint = self.initial_checkpoint, None
            return value
        readable, _, _ = select.select([self.checkpoint], [], [], timeout)
        if not readable:
            raise AssertionError("owner did not reach checkpoint")
        payload = self.checkpoint.recv(4096)
        if not payload:
            raise AssertionError("owner exited before reaching checkpoint")
        return json.loads(payload.splitlines()[0])

    def resume(self):
        self.checkpoint.sendall(b"continue\n")

    def kill_owner(self):
        os.kill(self.owner_pid, signal.SIGKILL)
        self.process.wait(timeout=8)


@pytest.fixture
def watchdog_owner_case(namespace_owner_env, fake_provider_factory, process_ledger):
    handles = []
    repo_root = Path(__file__).resolve().parents[1]
    owner_script = repo_root / "spindle_owner.py"

    def start(
        mode="healthy-turn",
        *,
        pause_checkpoint=None,
        disable_pdeathsig=False,
        timeout=None,
        generation=1,
        spool_id=None,
        spool_overrides=None,
        controlled_clock_fd=None,
        store_kind="bridge_schema1",
        expect_ready=True,
    ):
        from spindle.namespace_owner import capture_pid_namespace, read_proc_starttime
        from tests.owner_episode_fixtures import make_episode

        store = (
            namespace_owner_env["store"]
            if store_kind == "bridge_schema1"
            else namespace_owner_env["home"] / "spools-v2"
        )
        store.mkdir(parents=True, exist_ok=True)
        spool_id = spool_id or f"stage4-{len(handles)}"
        previous_generation = 0
        for path in (store / f"{spool_id}.json", store / f"{spool_id}.owner-identity"):
            try:
                previous = json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            previous_generation = max(
                previous_generation,
                int((previous.get("owner_episode") or {}).get("generation") or 0),
                int(previous.get("owner_generation") or 0),
            )
        if generation <= previous_generation or generation <= 0:
            raise AssertionError(
                f"generation {generation} must be positive and above durable generation {previous_generation}"
            )
        spool = {
            "id": spool_id,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "timeout": timeout,
            "spool_schema_version": 1 if store_kind == "bridge_schema1" else 2,
        }
        if spool_overrides:
            spool.update(spool_overrides)
        effective_timeout = spool.get("timeout")
        deadline = spool.get("wall_deadline_at")
        if effective_timeout is not None:
            if deadline is None:
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=effective_timeout)).isoformat()
            spool["wall_deadline_at"] = deadline
        starter = {
            "pid": os.getpid(),
            "birth_token": read_proc_starttime(os.getpid()),
            "namespace": capture_pid_namespace().to_dict(),
        }
        spool["owner_episode"] = make_episode(
            "reserved",
            generation=generation,
            path="before_watchdog",
            deadline=deadline if deadline is not None else None,
            starter=starter,
        )
        (store / f"{spool_id}.json").write_text(json.dumps(spool))
        provider = fake_provider_factory(mode)
        ready_read, ready_write = os.pipe()
        barrier_read, barrier_write = os.pipe()
        pass_fds = [ready_write, barrier_read]
        args = [
            sys.executable,
            str(owner_script),
            "--store",
            str(store),
            "--spool-id",
            spool_id,
            "--generation",
            str(generation),
            "--ready-fd",
            str(ready_write),
            "--launch-barrier-fd",
            str(barrier_read),
        ]
        checkpoint_controller = None
        if pause_checkpoint:
            checkpoint_controller, checkpoint_owner = socket.socketpair()
            pass_fds.append(checkpoint_owner.fileno())
            args.extend(["--checkpoint-fd", str(checkpoint_owner.fileno()), "--pause-checkpoint", pause_checkpoint])
        else:
            checkpoint_owner = None
        if controlled_clock_fd is not None:
            pass_fds.append(controlled_clock_fd)
            args.extend(["--clock-fd", str(controlled_clock_fd)])
        if effective_timeout is not None:
            args.extend(["--timeout", str(effective_timeout)])
            if deadline is not None:
                args.extend(["--deadline", deadline])
        if disable_pdeathsig:
            args.append("--disable-pdeathsig")
        args.extend(["--", str(provider)])
        try:
            proc = process_ledger(
                subprocess.Popen(
                    args,
                    cwd=repo_root,
                    env=dict(namespace_owner_env["env"]),
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        finally:
            os.close(barrier_read)
        os.close(ready_write)
        if checkpoint_owner is not None:
            checkpoint_owner.close()
        try:
            record = json.loads((store / f"{spool_id}.json").read_text())
            episode = record["owner_episode"]
            assert (episode["generation"], episode["revision"], episode["phase"]) == (
                generation,
                1,
                "reserved",
            )
            episode["watchdog"] = {
                "pid": proc.pid,
                "birth_token": read_proc_starttime(proc.pid),
                "namespace": capture_pid_namespace().to_dict(),
            }
            episode["revision"] = 2
            (store / f"{spool_id}.json").write_text(json.dumps(record))
            os.write(barrier_write, b"go\n")
        finally:
            os.close(barrier_write)
        candidates = [ready_read]
        if checkpoint_controller is not None:
            candidates.append(checkpoint_controller)
        readable, _, _ = select.select(candidates, [], [], 5)
        if not readable:
            stderr = proc.stderr.read() if proc.poll() is not None else ""
            raise AssertionError(f"watchdog owner did not start: {stderr}")
        initial = None
        ready = {}
        if ready_read in readable:
            with os.fdopen(ready_read) as stream:
                line = stream.readline()
                if expect_ready:
                    ready = json.loads(line)
                else:
                    assert line == "", "provider unexpectedly reached readiness"
                    ready = {}
        else:
            os.close(ready_read)
            initial = json.loads(checkpoint_controller.recv(4096).splitlines()[0])
            ready = {
                "owner_pid": initial["owner_pid"],
                "provider_pid": initial.get("provider_pid"),
                "owner_generation": initial["owner_generation"],
            }
        handle = WatchdogOwnerHandle(proc, store, spool_id, ready, checkpoint_controller, initial)
        handles.append(handle)
        return handle

    yield start
    for handle in handles:
        if handle.process.poll() is None:
            try:
                if handle.checkpoint is not None:
                    handle.resume()
                handle.request()
                handle.process.wait(timeout=3)
            except Exception:
                handle.process.kill()
                handle.process.wait(timeout=3)
        if handle.checkpoint is not None:
            handle.checkpoint.close()


@pytest.fixture
def foreign_pid_namespace(namespace_owner_env):
    repo_root = Path(__file__).resolve().parents[1]

    def run(store, source, *args):
        env = dict(namespace_owner_env["env"])
        env["_SPINDLE_STORE_SUPERVISOR"] = "1"
        command = [
            "bwrap",
            "--unshare-pid",
            "--as-pid-1",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--bind",
            str(store),
            str(store),
            "--",
            sys.executable,
            "-c",
            source,
            str(store),
            *map(str, args),
        ]
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode != 0:
            pytest.fail(f"ns_required bwrap observer failed: {completed.stderr or completed.stdout}")
        return json.loads(completed.stdout)

    return run


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
