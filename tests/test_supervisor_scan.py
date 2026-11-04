"""The store supervisor's pass cost tracks live work, not history (issue-20261103-0k0c).

Spool records are kept indefinitely by default, so the supervisor remembers
finished records that owe nothing and skips them until their file changes.
These tests pin what may and may not be remembered; the invariant is written
out beside SUPERVISOR_SETTLE_MARGIN_NS in spindle/__init__.py.
"""

import os
from collections import Counter

import pytest

import spindle
import spindle.owner_episode_convergence as convergence
from spindle.namespace_owner import create_control_request

PENDING_OBLIGATION = {
    "format": "spindle.owner-convergence/1",
    "obligations": {
        "natural_transcript": {
            "kind": "natural_transcript",
            "idempotency_key": "1/transcript/example",
            "intent": {},
            "progress": "pending",
            "completion": None,
            "error": "OSError: transient write failure",
        }
    },
}


@pytest.fixture
def spies(monkeypatch):
    """Count duty checks, capacity checks and reconciliations by spool ID."""
    calls = {"duties": Counter(), "capacity": Counter(), "reconcile": Counter()}
    real_duties = convergence.discoverable_duties_outstanding
    real_capacity = getattr(spindle, "_spool_holds_capacity", None)

    def duties(spool_id, record):
        calls["duties"][spool_id] += 1
        return real_duties(spool_id, record)

    def capacity(spool):
        calls["capacity"][spool.get("id")] += 1
        return real_capacity(spool)

    def reconcile(spool_id):
        calls["reconcile"][spool_id] += 1
        return (spindle._read_spool(spool_id) or {}).get("status") in {"pending", "running"}

    monkeypatch.setattr(convergence, "discoverable_duties_outstanding", duties)
    # raising=False lets the loop test run, and fail, on the pre-change tree.
    monkeypatch.setattr(spindle, "_spool_holds_capacity", capacity, raising=False)
    monkeypatch.setattr(spindle, "_reconcile_spool_step", reconcile)
    return calls


@pytest.fixture
def no_margin(monkeypatch):
    # Records written by the test are milliseconds old; the margin has its own test.
    monkeypatch.setattr(spindle, "SUPERVISOR_SETTLE_MARGIN_NS", 0, raising=False)


def finished(sid, **fields):
    spindle._write_spool(sid, {"id": sid, "status": "complete", "result": "done", **fields})


def passes(settled, count):
    return [spindle._supervisor_pass(settled) for _ in range(count)]


def test_finished_duty_free_records_are_checked_once(spies, no_margin):
    for sid in ("a", "b", "c"):
        finished(sid)
    settled = {}
    assert passes(settled, 4) == [False] * 4
    assert spies["duties"] == {"a": 1, "b": 1, "c": 1}
    assert spies["capacity"] == {"a": 1, "b": 1, "c": 1}
    assert spies["reconcile"] == {}
    assert sorted(settled) == ["a.json", "b.json", "c.json"]


def test_a_record_younger_than_the_margin_is_not_remembered(spies):
    finished("fresh")
    settled = {}
    passes(settled, 3)
    assert spies["duties"]["fresh"] == 3
    assert settled == {}


def test_rewritten_record_is_rechecked_and_new_duties_are_reconciled(spies, no_margin):
    finished("later-duty")
    settled = {}
    passes(settled, 2)
    assert spies["duties"]["later-duty"] == 1

    finished("later-duty", owner_convergence=PENDING_OBLIGATION)
    assert spindle._supervisor_pass(settled) is True
    assert spies["reconcile"]["later-duty"] == 1
    assert "later-duty.json" not in settled

    # Still owed: picked up on every pass until the record says otherwise.
    spindle._supervisor_pass(settled)
    assert spies["reconcile"]["later-duty"] == 2

    finished("later-duty")
    passes(settled, 3)
    assert spies["reconcile"]["later-duty"] == 2
    assert "later-duty.json" in settled


def test_record_rewritten_as_running_is_reconciled(spies, no_margin):
    finished("reused")
    settled = {}
    passes(settled, 2)
    spindle._write_spool("reused", {"id": "reused", "status": "running", "prompt": "again"})
    assert passes(settled, 3) == [True] * 3
    assert spies["reconcile"]["reused"] == 3
    assert "reused.json" not in settled


@pytest.mark.parametrize("status", ["pending", "running"])
def test_pending_and_running_are_reconciled_on_every_pass(spies, no_margin, status):
    spindle._write_spool("live", {"id": "live", "status": status})
    finished("done")
    settled = {}
    assert passes(settled, 4) == [True] * 4
    assert spies["reconcile"]["live"] == 4
    assert "live.json" not in settled
    assert spies["duties"]["done"] == 1


def test_status_only_terminal_is_never_remembered_and_sees_a_new_request(spies, no_margin):
    # An episode record with a terminal status but no terminal_origin: its
    # duties also come from the control mailbox, which can change without the
    # record changing.
    sid = "status-only"
    spindle._write_spool(
        sid, {"id": sid, "status": "complete", "owner_episode": {"generation": 3, "phase": "released"}}
    )
    assert convergence.duties_depend_only_on_record(spindle._read_spool(sid)) is False
    settled = {}
    passes(settled, 3)
    assert spies["duties"][sid] == 3
    assert spies["reconcile"][sid] == 0
    assert settled == {}

    before = spindle._get_spool_path(sid).stat()
    create_control_request(spindle.SPINDLE_DIR, sid, "drop", 3, "test")
    assert spindle._get_spool_path(sid).stat().st_mtime_ns == before.st_mtime_ns
    spindle._supervisor_pass(settled)
    assert spies["reconcile"][sid] == 1


def test_record_that_holds_capacity_is_not_remembered(spies, no_margin, monkeypatch):
    finished("still-owned")
    monkeypatch.setattr(spindle, "_spool_holds_capacity", lambda spool: True)
    settled = {}
    assert passes(settled, 3) == [True] * 3
    assert spies["duties"]["still-owned"] == 3
    assert settled == {}


def test_malformed_records_do_not_stop_the_pass_or_get_remembered(spies, no_margin, monkeypatch, caplog):
    spindle._write_spool("live", {"id": "live", "status": "running"})
    spindle._get_spool_path("no-id").write_text('{"status": "complete"}')
    finished("raises")
    real = convergence.discoverable_duties_outstanding

    def duties(spool_id, record):
        if spool_id == "raises":
            raise ValueError("poisoned record")
        return real(spool_id, record)

    monkeypatch.setattr(convergence, "discoverable_duties_outstanding", duties)
    settled = {}
    assert passes(settled, 2) == [True, True]
    assert spies["reconcile"]["live"] == 2
    assert settled == {}
    assert "reconciliation failed for spool 'raises'" in caplog.text
    assert "reconciliation failed for spool 'no-id'" in caplog.text


def test_files_that_are_not_records_are_skipped_and_read_again_only_after_a_change(
    spies, no_margin, monkeypatch, caplog
):
    # A default spool_export writes a JSON list into the store; the old loop
    # crashed on it ("list indices must be integers").
    spindle._get_spool_path("export").write_text('[{"id": "a"}, {"id": "b"}]')
    spindle._get_spool_path("not-json").write_text("{torn")
    reads = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        reads.append(os.path.basename(str(path)))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    settled = {}
    assert passes(settled, 3) == [False] * 3
    assert sorted(settled) == ["export.json", "not-json.json"]
    assert reads.count("export.json") == 1
    assert reads.count("not-json.json") == 1
    assert "failed" not in caplog.text

    # Rewritten into a real live record: read and reconciled on the next pass.
    spindle._write_spool("export", {"id": "export", "status": "running"})
    assert spindle._supervisor_pass(settled) is True
    assert spies["reconcile"]["export"] == 1


def test_deleted_record_leaves_the_memory(spies, no_margin):
    finished("gone")
    finished("kept")
    settled = {}
    passes(settled, 2)
    spindle._get_spool_path("gone").unlink()
    spindle._supervisor_pass(settled)
    assert sorted(settled) == ["kept.json"]


def test_retirement_still_reads_every_record(spies, no_margin, monkeypatch):
    # The memory may miss ownership evidence that changes without a record
    # write; the retirement decision does not use it.
    finished("lock-replaced-later")
    settled = {}
    assert spindle._supervisor_pass(settled) is False
    assert "lock-replaced-later.json" in settled
    monkeypatch.setattr(spindle, "_spool_holds_capacity", lambda spool: True)
    assert spindle._supervisor_pass(settled) is False
    assert spindle._store_supervision_required() is True


def test_the_supervisor_loop_keeps_its_memory_across_passes(spies, no_margin, monkeypatch):
    for sid in ("x", "y"):
        finished(sid)
    pass_count = []
    real_pass = getattr(spindle, "_supervisor_pass", None)

    def counting_pass(settled):
        pass_count.append(1)
        return real_pass(settled)

    monkeypatch.setattr(spindle, "_supervisor_pass", counting_pass, raising=False)
    monkeypatch.setattr(spindle, "SUPERVISOR_IDLE_GRACE", 5)
    monkeypatch.setattr(spindle.time, "sleep", lambda _seconds: None)
    ticks = iter(range(100))
    monkeypatch.setattr(spindle.time, "monotonic", lambda: next(ticks))
    lock_fd = os.open(spindle.SPINDLE_DIR / ".supervisor.lock", os.O_CREAT | os.O_RDWR)

    spindle._run_store_supervisor(str(spindle.SPINDLE_DIR), lock_fd)

    # One check from the first pass and one from the retirement scan, however
    # many passes ran in between.
    assert spies["duties"] == {"x": 2, "y": 2}
    assert len(pass_count) >= 5
