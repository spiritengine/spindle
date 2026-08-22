"""S2-S: same-namespace subprocess contracts for the logical spool owner."""

from __future__ import annotations

import fcntl
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import spindle
from spindle.namespace_owner import (
    LivenessEvidence,
    ProcessIdentity,
    active_spool_count,
    create_control_request,
    iter_control_requests,
    probe_ownership_lock,
    read_control_receipt,
    reconcile_owner_episode,
    retire_owner_artifacts,
)
from spindle.namespace_owner_process import AUTHORITY_LOST_EXIT_CODE

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
    initial_checkpoint: dict | None = None

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

    def wait_terminal(self):
        return wait_until(
            lambda: value if (value := self.spool()) and value.get("status") not in {"pending", "running"} else None,
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
        deadline=None,
        pause_checkpoint=None,
        controlled_clock_fd=None,
        extra_provider_args=(),
        extra_env=None,
        expect_ready=True,
        disable_pdeathsig=False,
    ):
        spool_id = f"owner-{len(handles)}"
        store = namespace_owner_env["store"]
        spool = {
            "id": spool_id,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "timeout": timeout,
        }
        if deadline is not None:
            spool["wall_deadline_at"] = deadline
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
        if deadline is not None:
            args.extend(["--deadline", str(deadline)])
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
        if disable_pdeathsig:
            args.append("--disable-pdeathsig")
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
        candidates = [ready_read]
        if checkpoint_controller is not None:
            candidates.append(checkpoint_controller)
        readable, _, _ = select.select(candidates, [], [], 5)
        if not readable:
            stderr = proc.stderr.read() if proc.poll() is not None else ""
            raise AssertionError(f"owner did not become ready: {stderr}")
        initial_checkpoint = None
        if ready_read in readable:
            with os.fdopen(ready_read) as stream:
                line = stream.readline()
                if expect_ready:
                    ready = json.loads(line)
                else:
                    assert line == "", "provider unexpectedly reached readiness"
                    ready = {}
        else:
            # A checkpoint reached before readiness (e.g. before the
            # provider is ever spawned) leaves ready_fd open and unwritten;
            # the checkpoint pause itself is the earliest observable point.
            os.close(ready_read)
            initial_checkpoint = json.loads(checkpoint_controller.recv(4096).splitlines()[0])
            ready = {}
        handle = OwnerHandle(
            proc, store, spool_id, ready, 1, checkpoint_controller, initial_checkpoint=initial_checkpoint
        )
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


def test_provider_exit_70_is_not_reclassified_as_owner_crash(owner_case):
    owner = owner_case("exit-70")
    evidence = owner.wait_exit()
    assert evidence["provider_exit_code"] == 70
    assert "owner_crashed" not in evidence
    assert "owner_crashed_after_cleanup" not in evidence
    assert owner.process.returncode == 70


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


@pytest.mark.parametrize("grace", [0.3, 0.8])
def test_ignored_term_is_killed_at_the_configured_evidence_grace(owner_case, grace):
    """The evidence grace widens the SIGTERM window; it never removes SIGKILL.

    A provider that ignores SIGTERM must still be gone one grace later, so a
    wedged provider can never hold deadline authority for longer than the
    constant (issue-20260819-c1hm).  Both legs pin the escalation to the width
    that was configured, which fails if the wait ever becomes unbounded.
    """
    owner = owner_case("ignore-term", extra_env={"SPINDLE_EVIDENCE_GRACE_SECONDS": str(grace)})
    request = owner.request()
    owner.wait_terminal()
    receipt = owner.receipt(request.request_id)

    started = datetime.fromisoformat(receipt.forced_cleanup_started_at)
    completed = datetime.fromisoformat(receipt.forced_cleanup_completed_at)
    escalation = (completed - started).total_seconds()
    assert grace <= escalation < grace + 2.0, f"escalation took {escalation}s at a {grace}s grace"
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
    assert (
        probe_ownership_lock(
            owner.lock_path, ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
        ).state
        == "held"
    )
    owner.checkpoint.sendall(b"continue\n")
    owner.wait_terminal()
    assert owner.receipt(request.request_id).forced_cleanup_completed_at


@pytest.mark.parametrize("later_kind", ["cancel", "timeout", "drop"])
def test_overlapping_requests_receive_superseded_receipts(owner_case, later_kind):
    owner = owner_case("ignore-term", pause_checkpoint="control_observed_before_ack")
    winner = owner.request("cancel")
    checkpoint = json.loads(owner.checkpoint.recv(4096).decode())
    assert checkpoint["name"] == "control_observed_before_ack"
    later = owner.request(later_kind)
    owner.checkpoint.sendall(b"continue\n")
    owner.wait_terminal()

    winning_receipt = owner.receipt(winner.request_id)
    superseded = owner.receipt(later.request_id)
    assert winning_receipt.owner_acknowledged_at
    assert winning_receipt.cleanup_outcome == "cleaned"
    assert superseded.owner_acknowledged_at is None
    assert superseded.cleanup_outcome == "rejected_superseded"


def test_malformed_receipt_is_preserved_while_owner_handles_valid_sibling(owner_case):
    owner = owner_case("ignore-term", pause_checkpoint="provider_ready")
    readable, _, _ = select.select([owner.checkpoint], [], [], 5)
    assert readable
    checkpoint = json.loads(owner.checkpoint.recv(4096).splitlines()[0])
    assert checkpoint["name"] == "provider_ready"

    damaged = owner.request("cancel", request_id="000-malformed-receipt")
    damaged_path = owner.store / f"{owner.spool_id}.control-mailbox" / f"{damaged.request_id}.receipt"
    damaged_path.write_text("{")
    valid = owner.request("drop", request_id="zzz-valid-request")
    owner.checkpoint.sendall(b"continue\n")

    terminal = owner.wait_terminal()
    owner.process.wait(timeout=8)
    stderr = owner.process.stderr.read()
    valid_receipt = owner.receipt(valid.request_id)

    assert terminal["status"] == "error"
    assert valid_receipt.owner_acknowledged_at
    assert valid_receipt.cleanup_outcome == "cleaned"
    assert damaged_path.read_text() == "{"
    assert f"malformed control receipt {damaged_path.name}" in stderr


def _replace_lock_path(owner):
    """Simulate an external actor replacing the process-owner pathname.

    Returns the exclusively-flocked fd on the fresh inode; the caller must
    unlock and close it (typically in a ``finally``).
    """
    old = owner.lock_path.stat()
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)
    assert owner.lock_path.stat().st_ino != old.st_ino
    contender = os.open(owner.lock_path, os.O_RDWR)
    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return contender


def _store_snapshot(store):
    """Every file in the store, recursively, as raw bytes."""
    return {
        path.relative_to(store).as_posix(): path.read_bytes() for path in sorted(store.rglob("*")) if path.is_file()
    }


@pytest.mark.parametrize(
    "checkpoint",
    ["control_observed_before_ack", "after_term_before_kill", "before_terminal_publish"],
)
def test_stop_boundaries_converge_after_authority_loss(owner_case, checkpoint):
    """A proven lock replacement latches instead of looping while refusing authority.

    Regression coverage for finding-20260820-540u: the pre-fix owner kept
    calling ``_verify_lock`` forever once the pathname was replaced, writing
    ``identity_mismatch`` on every poll and never releasing its watchdog or
    the provider it refused to act on.
    """
    owner = owner_case("ignore-term", pause_checkpoint=checkpoint)
    request = owner.request()
    observed = json.loads(owner.checkpoint.recv(4096).decode())
    assert observed["name"] == checkpoint
    provider_pid = owner.ready["provider_pid"]

    contender = _replace_lock_path(owner)
    try:
        owner.checkpoint.sendall(b"continue\n")
        # The watchdog wrapper converges within a bound instead of looping
        # forever while the owner refuses to exercise dead authority.
        owner.process.wait(timeout=5)

        # No shared-store write happens once the latch trips: no diagnostic
        # ownership_state marker, and no fabricated terminal status either -
        # the spool is left exactly where it stood at the moment of loss.
        after = owner.spool()
        assert after["status"] == "running"
        assert after.get("lifecycle", {}).get("ownership_state") == "held"

        # The provider is never signaled by this owner's own recorded PID or
        # PGID; it is orphaned to the watchdog subreaper on owner exit and
        # reaped there, bounded by SPINDLE_CONTAINMENT_BOUND_SECONDS.
        wait_until(lambda: not Path(f"/proc/{provider_pid}").exists())

        if checkpoint == "control_observed_before_ack":
            assert owner.receipt(request.request_id) is None
        else:
            assert owner.receipt(request.request_id).owner_acknowledged_at
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_owner_reverifies_exact_inode_before_release(owner_case):
    owner = owner_case("ignore-term", pause_checkpoint="before_lock_release")
    owner.request()
    observed = json.loads(owner.checkpoint.recv(4096).decode())
    assert observed["name"] == "before_lock_release"
    before = owner.spool()
    assert before["status"] == "error"

    contender = _replace_lock_path(owner)
    try:
        owner.checkpoint.sendall(b"continue\n")
        owner.process.wait(timeout=5)
        # The already-durable terminal status from before the loss survives
        # untouched; the freshly discovered mismatch gets no diagnostic write.
        after = owner.spool()
        assert after["status"] == "error"
        assert after["lifecycle"]["ownership_state"] == before["lifecycle"]["ownership_state"]
        assert after["lifecycle"]["ownership_state"] != "identity_mismatch"
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_pre_popen_reverify_never_starts_a_provider_after_authority_loss(owner_case):
    """A loss proven before the provider is spawned must never spawn one."""
    owner = owner_case("record-launch", pause_checkpoint="before_provider_start_checks")
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "before_provider_start_checks"
    assert checkpoint["provider_pid"] is None

    contender = _replace_lock_path(owner)
    try:
        owner.checkpoint.sendall(b"continue\n")
        owner.process.wait(timeout=5)
        assert not (owner.store / f"{owner.spool_id}.launch-record").exists()
        assert owner.spool()["status"] == "pending"
        assert not owner.process_identity_path.exists()
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_pre_popen_authority_loss_does_not_truncate_replacement_captures(owner_case):
    """A stale owner must not erase capture bytes from the generation replacing it."""
    owner = owner_case("record-launch", pause_checkpoint="identity_published")
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "identity_published"
    assert checkpoint["provider_pid"] is None

    stdout_path = owner.store / f"{owner.spool_id}.stdout"
    stderr_path = owner.store / f"{owner.spool_id}.stderr"
    replacement_stdout = b"replacement generation stdout\n"
    replacement_stderr = b"replacement generation stderr\n"
    stdout_path.write_bytes(replacement_stdout)
    stderr_path.write_bytes(replacement_stderr)

    contender = _replace_lock_path(owner)
    try:
        owner.checkpoint.sendall(b"continue\n")
        assert owner.process.wait(timeout=8) == AUTHORITY_LOST_EXIT_CODE
        assert stdout_path.read_bytes() == replacement_stdout
        assert stderr_path.read_bytes() == replacement_stderr
        assert not (owner.store / f"{owner.spool_id}.launch-record").exists()
        assert not owner.process_identity_path.exists()
        assert owner.spool()["status"] == "pending"
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_already_exited_provider_gets_no_fabricated_terminal_after_authority_loss(owner_case):
    """Loss discovered right after a provider's own clean exit still wins.

    The provider already finished successfully by the time the owner would
    next reverify; that must not turn into a fabricated success/terminal
    write once the lock is proven replaced first.
    """
    owner = owner_case("immediate-exit", pause_checkpoint="provider_ready")
    checkpoint = json.loads(owner.checkpoint.recv(4096).decode())
    assert checkpoint["name"] == "provider_ready"

    contender = _replace_lock_path(owner)
    try:
        owner.checkpoint.sendall(b"continue\n")
        owner.process.wait(timeout=5)
        # "running" is the last legitimate pre-latch write (set when the
        # provider was spawned, before this checkpoint); it must not advance
        # to a fabricated terminal state just because the provider is known
        # to have already finished cleanly.
        assert owner.spool()["status"] == "running"
        assert not owner.owner_exit_path.exists()
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_post_fork_publication_never_overwrites_a_replacement_generation(owner_case):
    """A lock replaced after the fork blocks every publication that follows.

    The pre-Popen proof is already stale once the provider exists, so without
    a post-fork reverify the stale owner writes its own process identity,
    spool record and readiness over the generation that replaced it
    (finding-20260821-ac82).  This seeds exactly those artifacts and holds the
    whole store to a byte-for-byte comparison.
    """
    owner = owner_case("ignore-term", pause_checkpoint="provider_forked_before_containment_armed")
    checkpoint = owner.receive_checkpoint()
    assert checkpoint["name"] == "provider_forked_before_containment_armed"
    provider_pid = checkpoint["provider_pid"]
    assert provider_pid is not None
    assert not owner.process_identity_path.exists(), "nothing is published before this checkpoint"

    # Let the forked provider finish its own first write so the captures are
    # stable, then stand in for the generation that replaced this one.
    stdout_path = owner.store / f"{owner.spool_id}.stdout"
    wait_until(lambda: stdout_path.stat().st_size or None)
    contender = _replace_lock_path(owner)
    try:
        owner.process_identity_path.write_text(
            json.dumps({"owner_pid": -1, "owner_generation": 2, "provider_pid": -2}, sort_keys=True)
        )
        (owner.store / f"{owner.spool_id}.json").write_text(
            json.dumps(dict(owner.spool(), owner_generation=2, status="running"), sort_keys=True)
        )
        seeded = _store_snapshot(owner.store)

        owner.checkpoint.sendall(b"continue\n")
        assert owner.process.wait(timeout=8) == AUTHORITY_LOST_EXIT_CODE
        wait_until(lambda: not Path(f"/proc/{provider_pid}").exists())
        assert _store_snapshot(owner.store) == seeded
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file mode bits, so nothing denies access")
def test_replacement_inode_this_user_cannot_read_still_converges(owner_case):
    """A replacement whose mode denies access is still a proven replacement.

    An access check reports it exactly as it reports a permission problem on
    our own file, so a live owner that asked about permission first kept
    running against a reservation it no longer held (finding-20260821-vbbc).
    """
    owner = owner_case("ignore-term")
    provider_pid = owner.ready["provider_pid"]
    before = owner.spool()
    old = owner.lock_path.stat()
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o000)
    try:
        assert owner.lock_path.stat().st_ino != old.st_ino
        assert owner.process.wait(timeout=8) == AUTHORITY_LOST_EXIT_CODE
        wait_until(lambda: not Path(f"/proc/{provider_pid}").exists())
        after = owner.spool()
        assert after["status"] == before["status"]
        assert after.get("lifecycle", {}).get("ownership_state") == "held"
        assert not owner.owner_exit_path.exists()
    finally:
        owner.lock_path.chmod(0o600)


def test_setsid_descendant_is_contained_after_authority_loss(owner_case):
    """A same-namespace but different-session descendant is not left behind.

    Neither the owner nor the watchdog signals it by a persisted PID or PGID.
    It is reached by the bounded custody passes, which walk live kernel
    parentage: the descendant reparents onto a subreaper when its own parent
    is killed, and the next scan finds it there.
    """
    owner = owner_case("setsid-grandchild")
    descendant_path = owner.store / f"{owner.spool_id}.descendant-pid"
    descendant = int(wait_until(lambda: descendant_path.read_text() if descendant_path.exists() else None))
    provider_pid = owner.ready["provider_pid"]

    contender = _replace_lock_path(owner)
    try:
        owner.process.wait(timeout=5)
        wait_until(lambda: not Path(f"/proc/{provider_pid}").exists())
        wait_until(lambda: not Path(f"/proc/{descendant}").exists())
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)


def test_containment_pass_exits_even_when_it_cannot_finish(owner_case):
    """The bounded pass is one attempt, not a retry-until-clean loop.

    Unlike the unbounded retry a genuine owner crash still gets
    (``_contain_adopted_descendants_until_clean``), an authority-loss
    disposition gets exactly one pass bounded by
    SPINDLE_CONTAINMENT_BOUND_SECONDS.  With PDEATHSIG disabled and the
    bound clamped to zero, that pass cannot possibly kill the provider - the
    watchdog must still converge instead of hanging, and this test (not the
    watchdog) is responsible for the provider it deliberately left alive.
    """
    owner = owner_case(
        "ignore-term",
        disable_pdeathsig=True,
        extra_env={"SPINDLE_CONTAINMENT_BOUND_SECONDS": "0"},
    )
    provider_pid = owner.ready["provider_pid"]
    try:
        contender = _replace_lock_path(owner)
        try:
            owner.process.wait(timeout=5)
        finally:
            fcntl.flock(contender, fcntl.LOCK_UN)
            os.close(contender)
        assert Path(f"/proc/{provider_pid}").exists()
    finally:
        try:
            os.kill(provider_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_replacement_generation_stays_alive_and_untouched(
    owner_case, namespace_owner_env, fake_provider_factory, process_ledger
):
    """A fresh generation on the replaced pathname is unaffected and unedited."""
    stale = owner_case("ignore-term")
    contender = _replace_lock_path(stale)
    try:
        stale.process.wait(timeout=5)
    finally:
        fcntl.flock(contender, fcntl.LOCK_UN)
        os.close(contender)

    # A second owner binds the same spool_id at the now-replaced pathname
    # and self-allocates the next generation - it must run to completion
    # untouched by the converged stale owner.
    provider = fake_provider_factory("healthy-turn")
    ready_read, ready_write = os.pipe()
    args = [
        sys.executable,
        str(OWNER_SCRIPT),
        "--store",
        str(stale.store),
        "--spool-id",
        stale.spool_id,
        "--generation",
        "1",
        "--ready-fd",
        str(ready_write),
        "--",
        str(provider),
    ]
    proc = process_ledger(
        subprocess.Popen(
            args,
            cwd=REPO_ROOT,
            env=namespace_owner_env["env"],
            pass_fds=(ready_write,),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    )
    os.close(ready_write)
    readable, _, _ = select.select([ready_read], [], [], 5)
    if not readable:
        stderr = proc.stderr.read() if proc.poll() is not None else ""
        raise AssertionError(f"replacement generation did not become ready: {stderr}")
    with os.fdopen(ready_read) as stream:
        ready = json.loads(stream.readline())
    fresh = OwnerHandle(proc, stale.store, stale.spool_id, ready, ready["owner_generation"])
    try:
        assert fresh.generation == 2
        stdout_path = fresh.store / f"{fresh.spool_id}.stdout"
        wait_until(lambda: stdout_path.read_text().count("\n") >= 2 or None)
        captured = stdout_path.read_bytes()
        assert captured.endswith(b"partial provider output\n")
        fresh.request()
        fresh.wait_terminal()
        # The converged stale owner never touched the replacement's capture:
        # it is exactly what the live provider wrote, byte for byte.
        assert stdout_path.read_bytes() == captured
        assert fresh.spool()["status"] not in {None, "pending"}
        assert fresh.spool()["owner_generation"] == 2
    finally:
        fresh.close()


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
    assert (
        not list(Path(f"/proc/{owner.process.pid}/task").glob("*/children")) if owner.process.poll() is None else True
    )


def test_s2_s_lock_01_held_lock_survives_live_reconciliation(owner_case):
    owner = owner_case("healthy-turn")
    before = owner.lock_path.stat()
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    lock = probe_ownership_lock(owner.lock_path, identity)
    result = reconcile_owner_episode(lock, LivenessEvidence("alive", "pidfd_live"), exit_evidence=True)
    after = owner.lock_path.stat()
    assert result.state == "active"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_s2_s_lock_02_unlinked_and_replaced_lock_converges_without_a_store_write(owner_case):
    """A permanent inode mismatch latches instead of publishing a diagnostic.

    The owner itself writes nothing once it detects the replacement, but an
    external reconciler probing the live (still-replaced) pathname
    independently classifies the record unhealthy from kernel/filesystem
    evidence - the damaged record stays nonterminal/store_unhealthy, per
    finding-20260821-1o6k, without the owner ever having to say so itself.
    """
    from spindle.namespace_owner import LivenessEvidence, ProcessIdentity, probe_ownership_lock, reconcile_owner_episode

    owner = owner_case("healthy-turn")
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    provider_pid = owner.ready["provider_pid"]
    old = owner.lock_path.stat()
    owner.lock_path.unlink()
    owner.lock_path.touch(mode=0o600)
    assert owner.lock_path.stat().st_ino != old.st_ino

    owner.process.wait(timeout=5)
    spool = owner.spool()
    assert spool["status"] == "running"
    assert spool.get("lifecycle", {}).get("ownership_state") == "held"
    wait_until(lambda: not Path(f"/proc/{provider_pid}").exists())

    lock = probe_ownership_lock(owner.lock_path, identity)
    assert lock.state == "identity_mismatch"
    result = reconcile_owner_episode(lock, LivenessEvidence("dead", "pidfd_exited"))
    assert result.state == "store_unhealthy"


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


def test_natural_exit_final_mailbox_settlement_receipts_racing_request(owner_case):
    owner = owner_case("silent-exit", pause_checkpoint="natural_exit_evidence_published")
    readable, _, _ = select.select([owner.checkpoint], [], [], 5)
    assert readable
    checkpoint = json.loads(owner.checkpoint.recv(4096).splitlines()[0])
    assert checkpoint["name"] == "natural_exit_evidence_published"
    assert owner.owner_exit_path.exists()

    request = owner.request(request_id="natural-exit-race")
    assert owner.receipt(request.request_id) is None
    owner.checkpoint.sendall(b"continue\n")
    evidence = owner.wait_exit()
    receipt = owner.receipt(request.request_id)

    assert evidence["cleanup_outcome"] == "natural_exit"
    assert receipt.owner_generation == owner.generation
    assert receipt.owner_acknowledged_at is None
    assert receipt.cleanup_outcome == "rejected_terminal"
    identity = ProcessIdentity.from_dict(json.loads(owner.owner_identity_path.read_text()))
    assert probe_ownership_lock(owner.lock_path, identity).state == "released"


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


def test_restarted_owner_preserves_future_absolute_deadline(owner_case, owner_clock):
    _current, advance, owner_clock_socket = owner_clock
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    owner = owner_case(
        "ignore-term",
        timeout=100,
        deadline=deadline,
        controlled_clock_fd=owner_clock_socket.fileno(),
    )
    time.sleep(0.1)
    assert list(iter_control_requests(owner.store, owner.spool_id)) == []
    assert owner.spool()["wall_deadline_at"] == deadline
    advance(31)
    assert owner.wait_terminal()["status"] == "timeout"


def test_restarted_owner_turns_overdue_deadline_into_durable_timeout(owner_case):
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    owner = owner_case("record-launch", timeout=100, deadline=deadline, expect_ready=False)
    assert owner.process.wait(timeout=8) == 124
    terminal = owner.wait_terminal()
    requests = list(iter_control_requests(owner.store, owner.spool_id))
    assert terminal["status"] == "timeout"
    assert requests == []
    assert terminal["wall_deadline_at"] == deadline
    assert terminal["error_kind"] == "deadline_expired_before_provider_start"
    assert not (owner.store / f"{owner.spool_id}.launch-record").exists()
    evidence = json.loads(owner.owner_exit_path.read_text())
    assert evidence["owner_generation"] == owner.generation
    assert evidence["cleanup_outcome"] == "deadline_expired_before_provider_start"


def test_s2_s_mig_01_legacy_terminal_spools_remain_readable(tmp_path):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
        (tmp_path / "legacy.json").write_text(
            json.dumps(
                {"id": "legacy", "status": "complete", "result": "old result", "created_at": datetime.now().isoformat()}
            )
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
