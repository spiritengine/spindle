"""S2-S: same-namespace subprocess contracts for the logical spool owner."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

import spindle
from spindle.namespace_owner import (
    LivenessEvidence,
    ProcessIdentity,
    active_spool_count,
    create_control_request,
    probe_ownership_lock,
    read_control_receipt,
    reconcile_owner_episode,
    retire_owner_artifacts,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER_SCRIPT = REPO_ROOT / "spindle_owner.py"


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true")


@dataclass
class OwnerHandle:
    process: subprocess.Popen
    store: Path
    spool_id: str
    ready: dict
    generation: int
    checkpoint: socket.socket | None = None
    descendants: list[int] = field(default_factory=list)

    @property
    def lock_path(self):
        return self.store / f"{self.spool_id}.process-owner"

    @property
    def owner_identity_path(self):
        return self.store / f"{self.spool_id}.owner-identity"

    @property
    def process_identity_path(self):
        return self.store / f"{self.spool_id}.process-identity"

    @property
    def owner_exit_path(self):
        return self.store / f"{self.spool_id}.owner-exit"

    def spool(self):
        try:
            return json.loads((self.store / f"{self.spool_id}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def request(self, kind="cancel", request_id=None):
        return create_control_request(
            self.store,
            self.spool_id,
            kind,
            self.generation,
            "subprocess-test",
            request_id=request_id,
        )

    def receipt(self, request_id):
        return read_control_receipt(self.store, self.spool_id, request_id)

    def wait_terminal(self):
        return wait_until(
            lambda: (
                value
                if (value := self.spool())
                and value.get("status") not in {"pending", "running"}
                else None
            ),
            timeout=8,
        )

    def wait_exit(self):
        self.process.wait(timeout=8)
        return json.loads(self.owner_exit_path.read_text())

    def close(self):
        if self.process.poll() is None:
            try:
                self.request()
                self.process.wait(timeout=3)
            except Exception:
                provider_pgid = self.ready.get("provider_pgid")
                if provider_pgid:
                    try:
                        os.killpg(provider_pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.process.kill()
                self.process.wait(timeout=3)
        if self.checkpoint is not None:
            self.checkpoint.close()


@pytest.fixture
def owner_case(namespace_owner_env, fake_provider_factory, process_ledger):
    handles = []

    def start(
        mode="cooperative",
        *,
        timeout=None,
        pause_checkpoint=None,
        controlled_clock_fd=None,
        extra_provider_args=(),
        extra_env=None,
    ):
        spool_id = f"owner-{len(handles)}"
        store = namespace_owner_env["store"]
        spool = {
            "id": spool_id,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "timeout": timeout,
        }
        (store / f"{spool_id}.json").write_text(json.dumps(spool))
        provider = fake_provider_factory(mode)
        ready_read, ready_write = os.pipe()
        pass_fds = [ready_write]
        checkpoint_controller = None
        args = [
            sys.executable,
            str(OWNER_SCRIPT),
            "--store",
            str(store),
            "--spool-id",
            spool_id,
            "--generation",
            "1",
            "--ready-fd",
            str(ready_write),
        ]
        if timeout is not None:
            args.extend(["--timeout", str(timeout)])
        if pause_checkpoint is not None:
            checkpoint_controller, checkpoint_owner = socket.socketpair()
            pass_fds.append(checkpoint_owner.fileno())
            args.extend(
                [
                    "--checkpoint-fd",
                    str(checkpoint_owner.fileno()),
                    "--pause-checkpoint",
                    pause_checkpoint,
                ]
            )
        else:
            checkpoint_owner = None
        if controlled_clock_fd is not None:
            pass_fds.append(controlled_clock_fd)
            args.extend(["--clock-fd", str(controlled_clock_fd)])
        args.extend(["--", str(provider), *extra_provider_args])
        env = dict(namespace_owner_env["env"])
        if extra_env:
            env.update(extra_env)
        proc = process_ledger(
            subprocess.Popen(
                args,
                cwd=REPO_ROOT,
                env=env,
                pass_fds=tuple(pass_fds),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
        os.close(ready_write)
        if checkpoint_owner is not None:
            checkpoint_owner.close()
        readable, _, _ = select.select([ready_read], [], [], 5)
        if not readable:
            stderr = proc.stderr.read() if proc.poll() is not None else ""
            raise AssertionError(f"owner did not become ready: {stderr}")
        with os.fdopen(ready_read) as stream:
            ready = json.loads(stream.readline())
        handle = OwnerHandle(proc, store, spool_id, ready, 1, checkpoint_controller)
        handles.append(handle)
        return handle

    yield start
    for handle in handles:
        handle.close()


def test_s2_s_own_01_launcher_exit_does_not_affect_owner_or_provider(owner_case):
    result = {}

    def launch():
        result["owner"] = owner_case("healthy-turn")

    thread = threading.Thread(target=launch)
    thread.start()
    thread.join(timeout=5)
    owner = result["owner"]
    assert owner.process.poll() is None
    assert os.pidfd_open(owner.ready["provider_pid"]) >= 0


def test_s2_s_own_02_owner_publishes_namespace_birth_lock_and_pidfd_identity(owner_case):
    owner = owner_case("healthy-turn")
    identity = json.loads(owner.owner_identity_path.read_text())
    process = json.loads(owner.process_identity_path.read_text())
    namespace_info = os.stat(f"/proc/{owner.process.pid}/ns/pid")
    lock_info = owner.lock_path.stat()
    assert (identity["namespace"]["device"], identity["namespace"]["inode"]) == (
        namespace_info.st_dev,
        namespace_info.st_ino,
    )
    assert (identity["lock_device"], identity["lock_inode"]) == (lock_info.st_dev, lock_info.st_ino)
    assert identity["birth_token"]
    assert process["provider_pid"] == owner.ready["provider_pid"]
    pidfd = os.pidfd_open(process["provider_pid"])
    try:
        assert not select.poll().poll(0)
    finally:
        os.close(pidfd)
    assert owner.spool()["status"] == "running"


def test_s2_s_own_03_immediate_provider_exit_is_observed_without_registration_race(owner_case):
    owner = owner_case("immediate-exit")
    evidence = owner.wait_exit()
    assert evidence["provider_exit_code"] == 0
    assert evidence["provider_reaped"] is True
    assert owner.process.poll() == 0


def test_s2_s_own_04_launcher_thread_exit_does_not_trigger_provider_pdeath(owner_case):
    holder = []

    thread = threading.Thread(target=lambda: holder.append(owner_case("healthy-turn")))
    thread.start()
    thread.join(timeout=5)
    owner = holder[0]
    time.sleep(0.1)
    assert owner.process.poll() is None
    assert Path(f"/proc/{owner.ready['provider_pid']}").exists()


def test_s2_s_ctl_01_cooperative_cancel_records_all_nonforced_facts(owner_case):
    owner = owner_case("cooperative")
    request = owner.request()
    terminal = owner.wait_terminal()
    receipt = wait_until(lambda: owner.receipt(request.request_id))
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    assert receipt.owner_acknowledged_at
    assert receipt.provider_cancel_attempted_at
    assert receipt.provider_acknowledged_at
    assert receipt.child_exit_observed_at
    assert receipt.forced_cleanup_started_at is None
    assert receipt.forced_cleanup_completed_at is None
    assert owner.process.wait(timeout=2) == 0


def test_s2_s_ctl_02_ignored_term_forces_cleanup_before_terminal(owner_case):
    owner = owner_case("ignore-term")
    request = owner.request()
    terminal = owner.wait_terminal()
    receipt = owner.receipt(request.request_id)
    assert terminal["lifecycle"]["normalized_terminal_kind"] == "cancelled"
    assert receipt.owner_acknowledged_at
    assert receipt.provider_cancel_attempted_at
    assert receipt.provider_acknowledged_at is None
    assert receipt.forced_cleanup_started_at
    assert receipt.forced_cleanup_completed_at
    assert receipt.child_exit_observed_at
    assert not Path(f"/proc/{owner.ready['provider_pid']}").exists()


@pytest.mark.parametrize("kind", ["cancel", "timeout", "drop"])
def test_s2_s_ctl_03_cancel_drop_and_timeout_are_mailbox_only_from_observers(owner_case, kind):
    owner = owner_case("ignore-term")
    request = owner.request(kind)
    request_path = owner.store / f"{owner.spool_id}.control-mailbox" / f"{request.request_id}.request"
    assert request_path.exists()
    owner.wait_terminal()


def test_s2_s_ctl_04_stubborn_child_remains_slot_counted_until_resolution(owner_case):
    owner = owner_case("ignore-term", pause_checkpoint="after_term_before_kill")
    request = owner.request()
    checkpoint = json.loads(owner.checkpoint.recv(4096).decode())
    assert checkpoint["name"] == "after_term_before_kill"
    spool = owner.spool()
    assert spool["status"] == "running"
    assert spool["lifecycle"]["public_stop_state"] == "stopping"
    assert active_spool_count([spool]) == 1
    assert probe_ownership_lock(owner.lock_path, ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))).state == "held"
    owner.checkpoint.sendall(b"continue\n")
    owner.wait_terminal()
    assert owner.receipt(request.request_id).forced_cleanup_completed_at


def test_s2_s_own_05_setsid_descendant_is_cleaned_and_never_inherits_custody(owner_case):
    owner = owner_case("setsid-grandchild")
    descendant_path = owner.store / f"{owner.spool_id}.descendant-pid"
    descendant = int(wait_until(lambda: descendant_path.read_text() if descendant_path.exists() else None))
    lock_inode = owner.lock_path.stat().st_ino
    for pid in (owner.ready["provider_pid"], descendant):
        targets = []
        for fd_path in Path(f"/proc/{pid}/fd").iterdir():
            try:
                targets.append(os.stat(fd_path).st_ino)
            except OSError:
                pass
        assert lock_inode not in targets
    owner.request()
    owner.wait_terminal()
    assert not Path(f"/proc/{descendant}").exists()


def test_s2_s_own_06_one_sigchld_drains_all_reapable_descendants(owner_case):
    owner = owner_case("fork-burst")
    evidence = owner.wait_exit()
    assert evidence["adopted_children_reaped"] >= 6
    assert not list(Path(f"/proc/{owner.process.pid}/task").glob("*/children")) if owner.process.poll() is None else True


def test_s2_s_lock_01_held_lock_survives_live_reconciliation(owner_case):
    owner = owner_case("healthy-turn")
    before = owner.lock_path.stat()
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    lock = probe_ownership_lock(owner.lock_path, identity)
    result = reconcile_owner_episode(lock, LivenessEvidence("alive", "pidfd_live"), exit_evidence=True)
    after = owner.lock_path.stat()
    assert result.state == "active"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_s2_s_lock_02_unlinked_and_replaced_held_lock_makes_store_unhealthy(owner_case):
    owner = owner_case("healthy-turn")
    old = owner.lock_path.stat()
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)
    assert owner.lock_path.stat().st_ino != old.st_ino
    spool = wait_until(
        lambda: (
            value
            if (value := owner.spool())
            and value.get("lifecycle", {}).get("ownership_state") == "identity_mismatch"
            else None
        )
    )
    assert spool["status"] == "running"


def test_s2_s_lock_03_permission_repair_restores_service_without_death_guess(owner_case):
    owner = owner_case("healthy-turn")
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    generation = owner.spool()["owner_generation"]
    owner.lock_path.chmod(0)
    try:
        wait_until(lambda: owner.spool().get("lifecycle", {}).get("ownership_state") == "unreadable")
        assert probe_ownership_lock(owner.lock_path, identity).state == "unreadable"
        assert owner.process.poll() is None
        assert owner.spool()["status"] == "running"
    finally:
        owner.lock_path.chmod(0o600)
    wait_until(lambda: owner.spool().get("lifecycle", {}).get("ownership_state") == "held")
    assert probe_ownership_lock(owner.lock_path, identity).state == "held"
    assert owner.spool()["owner_generation"] == generation


def test_s2_s_clean_01_frozen_legacy_sweeper_cannot_enumerate_new_sidecars(owner_case, legacy_root_sweeper):
    owner = owner_case("healthy-turn")
    json_paths, lock_paths = legacy_root_sweeper(owner.store)
    assert {path.name for path in json_paths} == {f"{owner.spool_id}.json"}
    assert lock_paths == set()


def test_s2_s_fin_01_real_exit_sidecar_and_released_lock_authorize_finalization(owner_case):
    owner = owner_case("immediate-exit")
    owner.wait_exit()
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    lock = probe_ownership_lock(owner.lock_path, identity)
    result = reconcile_owner_episode(lock, LivenessEvidence("dead", "pidfd_exited"), exit_evidence=True)
    assert lock.state == "released"
    assert result.state == "terminalizable"
    assert owner.lock_path.exists()


def test_s2_s_time_01_owner_self_timeout_uses_control_and_reaps_before_terminal(owner_case, owner_clock):
    _current, advance, owner_clock_socket = owner_clock
    owner = owner_case("ignore-term", timeout=10, controlled_clock_fd=owner_clock_socket.fileno())
    advance(11)
    terminal = owner.wait_terminal()
    receipts = list((owner.store / f"{owner.spool_id}.control-mailbox").glob("*.receipt"))
    assert terminal["status"] == "timeout"
    assert len(receipts) == 1
    receipt = read_control_receipt(owner.store, owner.spool_id, receipts[0].stem)
    assert receipt.owner_acknowledged_at
    assert receipt.forced_cleanup_completed_at
    assert receipt.child_exit_observed_at


def test_s2_s_mig_01_legacy_terminal_spools_remain_readable(tmp_path):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
        (tmp_path / "legacy.json").write_text(
            json.dumps({"id": "legacy", "status": "complete", "result": "old result", "created_at": datetime.now().isoformat()})
        )
        assert spindle._read_spool("legacy")["result"] == "old result"
        assert [item["id"] for item in spindle._list_spools()] == ["legacy"]


def test_s2_s_launch_01_owner_preserves_existing_child_launch_semantics(owner_case):
    owner = owner_case(
        "record-launch",
        extra_provider_args=("arg-one", "arg two"),
        extra_env={"OWNER_TEST_ENV": "preserved"},
    )
    evidence = owner.wait_exit()
    record = json.loads((owner.store / f"{owner.spool_id}.launch-record").read_text())
    assert record["argv"][1:] == ["arg-one", "arg two"]
    assert record["env"] == "preserved"
    assert Path(record["cwd"]) == REPO_ROOT
    assert evidence["provider_exit_code"] == 0


def test_s2_s_ret_01_retirement_requires_the_recorded_released_inode(owner_case):
    owner = owner_case("immediate-exit")
    owner.wait_exit()
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    assert retire_owner_artifacts(owner.store, owner.spool_id, identity) is True
    assert not owner.lock_path.exists()


def test_s2_s_slot_02_restart_recount_neither_doubles_nor_leaks(tmp_path, monkeypatch):
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    for spool_id, value in {
        "running": {"id": "running", "status": "running"},
        "stopping": {"id": "stopping", "status": "running", "lifecycle": {"public_stop_state": "stopping"}},
    }.items():
        (tmp_path / f"{spool_id}.json").write_text(json.dumps(value))
    assert spindle._count_running() == 2
    assert spindle._count_running() == 2
    (tmp_path / "stopping.json").write_text(json.dumps({"id": "stopping", "status": "timeout"}))
    assert spindle._count_running() == 1
