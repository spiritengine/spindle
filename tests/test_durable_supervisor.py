"""Real-subprocess contracts for the durable per-store spool supervisor.

These tests intentionally avoid importing ``spindle`` in their subprocess
polling path.  A standalone CLI must be able to exit, taking all of its Python
threads with it, while another Spindle-owned process finishes the durable
record without a second Spindle command.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_RECORD = ".supervisor.json"
SUPERVISOR_LIFETIME_LOCK = ".supervisor.lock"
SUPERVISOR_CONTROL_LOCK = ".supervisor-control.lock"


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.025, description: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            last = None
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {description}; last={last!r}")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _wait_spool(store: Path, spool_id: str, *statuses: str, timeout: float = 10.0) -> dict:
    path = store / f"{spool_id}.json"

    def terminal():
        data = _read_json(path)
        return data if data.get("status") in statuses else None

    return _wait_for(terminal, timeout=timeout, description=f"{spool_id} status in {statuses}")


def _parse_spool_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("Spawned spool:"):
            return line.split(":", 1)[1].strip()
    pytest.fail(f"CLI did not report a spool id:\n{stdout}")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_fake_claude(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "claude"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import signal
            import sys
            import time
            from pathlib import Path

            count_path = os.environ.get("FAKE_HARNESS_COUNT")
            if count_path:
                path = Path(count_path)
                try:
                    count = int(path.read_text())
                except (FileNotFoundError, ValueError):
                    count = 0
                count += 1
                path.write_text(str(count))

            pid_path = os.environ.get("FAKE_HARNESS_PID")
            if pid_path:
                Path(pid_path).write_text(str(os.getpid()))

            mode = os.environ.get("FAKE_HARNESS_MODE", "success")
            delay = float(os.environ.get("FAKE_HARNESS_DELAY", "0"))
            if mode == "ignore-term":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if delay:
                time.sleep(delay)
            if mode == "expire-once" and count == 1:
                print("No conversation found with session ID fake-session", file=sys.stderr, flush=True)
                while True:
                    time.sleep(1)
            if mode == "wait-file":
                release = Path(os.environ["FAKE_HARNESS_RELEASE"])
                while not release.exists():
                    time.sleep(0.02)
            if mode in {"hang", "ignore-term"}:
                while True:
                    time.sleep(1)
            if mode == "failure":
                print("fake harness failed", file=sys.stderr, flush=True)
                raise SystemExit(7)
            print(json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "durable result",
                "session_id": "fake-session",
            }), flush=True)
            """
        )
    )
    fake.chmod(0o755)
    return fake


@pytest.fixture
def supervisor_env(tmp_path):
    home = tmp_path / "home"
    store = home / "spools"
    fake_home = tmp_path / "user-home"
    workdir = tmp_path / "work"
    bin_dir = tmp_path / "bin"
    store.mkdir(parents=True)
    fake_home.mkdir()
    workdir.mkdir()
    _write_fake_claude(bin_dir)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(fake_home),
            "SPINDLE_HOME": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "SPINDLE_MONITOR_POLL_INTERVAL": "0.05",
            "SPINDLE_PENDING_SPAWN_TIMEOUT": "0.3",
            "SPINDLE_SUPERVISOR_IDLE_GRACE": "0.4",
            "SPINDLE_SUPERVISOR_POLL_INTERVAL": "0.05",
        }
    )
    return env, store, workdir


def _spin_cli(env: dict[str, str], workdir: Path, *, timeout: int | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "spindle",
        "spin",
        "--permission",
        "careful",
        "--working-dir",
        str(workdir),
        "--skeinless",
        "--human",
    ]
    if timeout is not None:
        cmd.extend(["--timeout", str(timeout)])
    cmd.append("exercise durable ownership")
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize(
    ("mode", "terminal_status", "exit_code"),
    [
        ("success", "complete", 0),
        ("failure", "error", 7),
    ],
)
def test_cli_exit_does_not_abandon_terminal_finalization(
    supervisor_env, mode: str, terminal_status: str, exit_code: int
):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_MODE"] = mode
    env["FAKE_HARNESS_DELAY"] = "0.4"

    cli = _spin_cli(env, workdir)

    assert cli.returncode == 0, cli.stderr
    spool_id = _parse_spool_id(cli.stdout)
    spool = _wait_spool(store, spool_id, terminal_status)
    assert spool["spool_schema_version"] == 1
    assert spool["exit_code"] == exit_code
    assert spool["completed_at"]
    if terminal_status == "complete":
        assert spool["result"] == "durable result"
    else:
        assert "fake harness failed" in spool["error"]


def test_cli_exit_does_not_abandon_timeout(supervisor_env):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_MODE"] = "ignore-term"
    env["FAKE_HARNESS_PID"] = str(store / "harness.pid")

    cli = _spin_cli(env, workdir, timeout=1)

    assert cli.returncode == 0, cli.stderr
    spool_id = _parse_spool_id(cli.stdout)
    spool = _wait_spool(store, spool_id, "timeout", timeout=8)
    harness_pid = int((store / "harness.pid").read_text())
    _wait_for(lambda: not _pid_alive(harness_pid), timeout=3, description="timed-out harness process death")
    assert spool["error"] == "Timeout after 1s"


def test_concurrent_launchers_share_one_store_owner(supervisor_env):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.8"
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "spindle",
                "spin",
                "--working-dir",
                str(workdir),
                "--skeinless",
                "--human",
                f"candidate {index}",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    completed = [proc.communicate(timeout=10) for proc in procs]
    spool_ids = [_parse_spool_id(stdout) for stdout, _ in completed]
    record = _wait_for(
        lambda: _read_json(store / SUPERVISOR_RECORD),
        description="supervisor owner record",
    )

    assert record["supervisor_protocol_version"] >= 1
    assert record["spool_schema_version"] >= 1
    assert _pid_alive(record["pid"])
    terminal = [_wait_spool(store, spool_id, "complete") for spool_id in spool_ids]
    assert {spool["result"] for spool in terminal} == {"durable result"}
    assert len({spool["completed_at"] for spool in terminal}) == 2


def test_poison_running_record_does_not_crash_whole_supervisor(supervisor_env):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.5"
    cli = _spin_cli(env, workdir)
    assert cli.returncode == 0, cli.stderr
    healthy_id = _parse_spool_id(cli.stdout)
    _wait_spool(store, healthy_id, "running")

    # This record exercises the running-timeout parser but remains observably
    # alive, so only the malformed timestamp distinguishes it from a legitimate
    # legacy spool. It must not kill the owner of the healthy sibling.
    (store / "poison.json").write_text(
        json.dumps(
            {
                "id": "poison",
                "status": "running",
                "pid": os.getpid(),
                "harness": "claude-code",
                "timeout": 1,
                "created_at": "not-an-iso-timestamp",
            }
        )
    )

    assert _wait_spool(store, healthy_id, "complete", timeout=8)["result"] == "durable result"
    owner = _read_json(store / SUPERVISOR_RECORD)
    assert _pid_alive(owner["pid"])


def test_dead_supervisor_is_reclaimed_without_relaunching_harness(supervisor_env):
    env, store, workdir = supervisor_env
    run_count = store / "harness.count"
    release = store / "harness.release"
    env["FAKE_HARNESS_COUNT"] = str(run_count)
    env["FAKE_HARNESS_MODE"] = "wait-file"
    env["FAKE_HARNESS_RELEASE"] = str(release)

    cli = _spin_cli(env, workdir)
    assert cli.returncode == 0, cli.stderr
    spool_id = _parse_spool_id(cli.stdout)
    running = _wait_spool(store, spool_id, "running")
    original_harness_pid = running["pid"]
    owner = _wait_for(
        lambda: _read_json(store / SUPERVISOR_RECORD),
        description="first supervisor owner",
    )
    os.kill(owner["pid"], signal.SIGKILL)
    _wait_for(lambda: not _pid_alive(owner["pid"]), description="first supervisor death")

    # A later Spindle process reclaims the store.  It may inspect/recover, but
    # must not launch a second copy of the already-running task.
    subprocess.run(
        [sys.executable, "-m", "spindle", "spools", "--human"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    replacement = _wait_for(
        lambda: (
            record
            if (record := _read_json(store / SUPERVISOR_RECORD)).get("pid") != owner["pid"]
            else None
        ),
        description="replacement supervisor owner",
    )
    assert _pid_alive(replacement["pid"])
    release.touch()
    spool = _wait_spool(store, spool_id, "complete")
    assert spool["pid"] == original_harness_pid
    assert run_count.read_text() == "1"


def test_abandoned_minimal_reservation_is_terminalized(supervisor_env):
    env, store, _ = supervisor_env
    spool_id = "abandoned-minimal"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import spindle; "
                f"ok, error = spindle._try_reserve_slot_and_create({spool_id!r}); "
                "assert ok, error"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    spool = _wait_spool(store, spool_id, "error", timeout=5)
    assert "spawn timeout" in spool["error"]
    assert spool.get("pid") is None


def _start_supervisor_lock_holder(
    env: dict[str, str],
    store: Path,
    *,
    protocol: int,
    schema: int,
    package: str,
) -> tuple[subprocess.Popen, Path]:
    ready = store / "holder.ready"
    stop = store / "holder.stop"
    script = textwrap.dedent(
        f"""\
        import fcntl, json, os, time
        from pathlib import Path
        store = Path({str(store)!r})
        fd = os.open(store / {SUPERVISOR_LIFETIME_LOCK!r}, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        (store / {SUPERVISOR_RECORD!r}).write_text(json.dumps({{
            "pid": os.getpid(),
            "supervisor_protocol_version": {protocol},
            "spool_schema_version": {schema},
            "package": {package!r},
            "package_version": "foreign",
        }}))
        Path({str(ready)!r}).touch()
        while not Path({str(stop)!r}).exists():
            time.sleep(0.02)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=REPO_ROOT, env=env)
    _wait_for(ready.exists, description="fake supervisor lock holder")
    return proc, stop


@pytest.mark.parametrize(("protocol", "schema"), [(999, 1), (1, 999)])
def test_incompatible_owner_is_rejected_before_reservation(supervisor_env, protocol: int, schema: int):
    env, store, workdir = supervisor_env
    holder, stop = _start_supervisor_lock_holder(
        env,
        store,
        protocol=protocol,
        schema=schema,
        package="/foreign/spindle",
    )
    before = {path.name for path in store.glob("*.json")}
    try:
        cli = _spin_cli(env, workdir)
        after = {path.name for path in store.glob("*.json")}
        assert cli.returncode != 0 or "Error:" in cli.stdout
        assert after == before
    finally:
        stop.touch()
        holder.wait(timeout=5)


def test_compatible_foreign_package_identity_is_diagnostic_not_rejected(supervisor_env):
    env, store, workdir = supervisor_env
    holder, stop = _start_supervisor_lock_holder(
        env,
        store,
        protocol=1,
        schema=1,
        package="/foreign/spindle",
    )
    try:
        cli = _spin_cli(env, workdir)
        assert cli.returncode == 0, cli.stderr
        spool_id = _parse_spool_id(cli.stdout)
        pending = _read_json(store / f"{spool_id}.json")
        assert pending["status"] in {"pending", "running"}
    finally:
        stop.touch()
        holder.wait(timeout=5)


def test_idle_retirement_serializes_with_new_reservation(supervisor_env):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.05"
    first = _spin_cli(env, workdir)
    first_id = _parse_spool_id(first.stdout)
    _wait_spool(store, first_id, "complete")

    control_fd = os.open(store / SUPERVISOR_CONTROL_LOCK, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(control_fd, fcntl.LOCK_EX)
        contender = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "spindle",
                "spin",
                "--working-dir",
                str(workdir),
                "--skeinless",
                "--human",
                "retirement race",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)
    finally:
        fcntl.flock(control_fd, fcntl.LOCK_UN)
        os.close(control_fd)

    stdout, stderr = contender.communicate(timeout=10)
    assert contender.returncode == 0, stderr
    second_id = _parse_spool_id(stdout)
    assert _wait_spool(store, second_id, "complete")["result"] == "durable result"


def test_cli_process_group_death_does_not_kill_detached_owner_or_harness(supervisor_env):
    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.8"
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""\
                import spindle, time
                result = spindle._spin_sync(
                    "group-death", None, False, None, {str(workdir)!r},
                    None, None, None, None, True, None,
                )
                print(result, flush=True)
                time.sleep(30)
                """
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert helper.stdout is not None
    spool_id = helper.stdout.readline().strip()
    assert spool_id
    running = _wait_spool(store, spool_id, "running")
    owner = _wait_for(
        lambda: _read_json(store / SUPERVISOR_RECORD),
        description="detached supervisor record",
    )

    os.killpg(helper.pid, signal.SIGKILL)
    helper.wait(timeout=5)
    assert _pid_alive(running["pid"])
    assert _pid_alive(owner["pid"])
    assert _wait_spool(store, spool_id, "complete")["result"] == "durable result"


def test_launcher_death_after_shard_creation_preserves_recovery_metadata(supervisor_env, tmp_path):
    env, store, _ = supervisor_env
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Spindle Test",
            "-c",
            "user.email=spindle@example.invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    spool_id = "shard-crash-window"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""\
                import os
                import spindle
                ok, error = spindle._try_reserve_slot_and_create({spool_id!r})
                assert ok, error
                shard, error = spindle._spawn_shard({spool_id!r}, {str(repo)!r}, base_branch="main")
                assert shard, error
                os._exit(77)
                """
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 77, probe.stderr

    spool = _wait_spool(store, spool_id, "error", timeout=5)
    assert spool["shard_created_by_spool"] is True
    assert spool["shard_source_dir"] == str(repo)
    assert spool["base_branch"] == "main"
    assert spool["working_dir"] == spool["shard"]["worktree_path"]
    worktree = Path(spool["working_dir"])
    assert worktree.exists()
    assert worktree.name == spool_id
    assert spool["shard"]["branch_name"] == f"shard-{spool_id}"
    assert spool["shard_cleanup_preserved"] is True


def test_expired_session_replacement_remains_owned_to_terminal(supervisor_env):
    env, store, workdir = supervisor_env
    count = store / "harness.count"
    env["FAKE_HARNESS_MODE"] = "expire-once"
    env["FAKE_HARNESS_COUNT"] = str(count)
    original_id = "000-original"
    (store / "transcripts").mkdir()
    (store / f"{original_id}.json").write_text(
        json.dumps(
            {
                "id": original_id,
                "status": "complete",
                "harness": "claude-code",
                "session_id": "fake-session",
                "working_dir": str(workdir),
                "permission": "careful",
                "allowed_tools": None,
                "model": None,
                "env": {
                    "FAKE_HARNESS_MODE": "expire-once",
                    "FAKE_HARNESS_COUNT": str(count),
                },
                "created_at": datetime.now().isoformat(),
                "completed_at": datetime.now().isoformat(),
            }
        )
    )
    (store / "transcripts" / f"{original_id}.txt").write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "old result",
                "session_id": "fake-session",
            }
        )
    )

    respin = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import spindle; "
                "print(spindle._respin_sync('fake-session', 'continue after expiry'), flush=True)"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert respin.returncode == 0, respin.stderr
    spool_id = respin.stdout.strip().splitlines()[-1]
    spool = _wait_spool(store, spool_id, "complete", timeout=8)
    assert spool["used_transcript_fallback"] is True
    assert spool["result"] == "durable result"
    assert count.read_text() == "2"


def _mcp_result_text(result) -> str:
    if isinstance(result.data, str):
        return result.data
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            return text
    pytest.fail(f"MCP result did not contain text: {result!r}")


@pytest.mark.asyncio
async def test_stdio_launch_survives_stdio_parent_exit(supervisor_env):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.5"
    transport = StdioTransport(
        sys.executable,
        ["-m", "spindle", "serve"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with Client(transport) as client:
        result = await client.call_tool(
            "spin",
            {
                "prompt": "stdio durable ownership",
                "working_dir": str(workdir),
                "skeinless": True,
            },
        )
        spool_id = _mcp_result_text(result)

    assert _wait_spool(store, spool_id, "complete")["result"] == "durable result"


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_http_service_launch_reaps_idle_supervisor(supervisor_env):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    env, store, workdir = supervisor_env
    env["FAKE_HARNESS_DELAY"] = "0.1"
    port = _unused_local_port()
    service = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "spindle",
            "serve",
            "--http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        def healthy():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as response:
                    return response.status == 200
            except OSError:
                return False

        _wait_for(healthy, timeout=10, description="HTTP service health")
        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp")) as client:
            result = await client.call_tool(
                "spin",
                {
                    "prompt": "HTTP durable ownership",
                    "working_dir": str(workdir),
                    "skeinless": True,
                },
            )
            spool_id = _mcp_result_text(result)
        assert _wait_spool(store, spool_id, "complete")["result"] == "durable result"
        owner_pid = _read_json(store / SUPERVISOR_RECORD)["pid"]
        _wait_for(
            lambda: not _pid_alive(owner_pid),
            timeout=5,
            description="idle supervisor reaped by surviving service",
        )
    finally:
        service.terminate()
        service.wait(timeout=5)


def test_explicit_store_contains_all_supervisor_artifacts(supervisor_env):
    env, store, workdir = supervisor_env
    cli = _spin_cli(env, workdir)
    spool_id = _parse_spool_id(cli.stdout)
    _wait_spool(store, spool_id, "complete")

    assert (store / SUPERVISOR_RECORD).exists()
    assert (store / SUPERVISOR_LIFETIME_LOCK).exists()
    assert (store / SUPERVISOR_CONTROL_LOCK).exists()
    assert list(store.glob(f"{spool_id}.*"))
    assert not list(Path(env["HOME"]).glob(".spindle/spools/*.json"))
