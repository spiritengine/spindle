"""Contract tests for the durable lifecycle store (apply_event) and integration.

Covers: durable per-event snapshot writes under the record lock, crash-atomicity
(crash before replace leaves the old snapshot and reconverges; crash after replace
keeps the new content), the _write_spool monotonicity guard, coexistence with the
owner's episode/flat-lifecycle keys, corrupt/absent record handling, and the
driver-side activity coalescer bounding the durable-write rate.
"""

import fcntl
import json
import os

import pytest

import spindle
from spindle import lifecycle as lc
from tests.lifecycle_fixtures import (
    FakeDriver,
    ev,
    make_spool_record,
    read_provider,
    read_record,
)


class InjectedCrash(Exception):
    """A deterministic interruption injected at a named store step."""


# --- basic apply ------------------------------------------------------------


def test_apply_event_writes_provider_block(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s1")
    lc.apply_event(store, "s1", ev("s1", lc.TRANSPORT_STARTED))
    provider = read_provider(store, "s1")
    assert provider["sequence"] == 1
    assert provider["connection_state"] == lc.CONNECTION_STARTING


def test_apply_event_sequence_advances_across_calls(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s1")
    lc.apply_event(store, "s1", ev("s1", lc.CONVERSATION_ACCEPTED))
    provider = lc.apply_event(store, "s1", ev("s1", lc.TURN_STARTED))
    assert provider["sequence"] == 2
    assert provider["protocol_state"] == lc.PROTOCOL_ACTIVE


def test_missing_spool_raises_spool_not_found(tmp_path):
    with pytest.raises(lc.SpoolNotFound):
        lc.apply_event(tmp_path, "nope", ev("nope", lc.ACTIVITY))


def test_corrupt_record_raises_corruption_not_reinit(tmp_path):
    store = tmp_path / "store"
    store.mkdir(parents=True)
    (store / "s1.json").write_text("{ this is not json")
    with pytest.raises(lc.LifecycleCorruption):
        lc.apply_event(store, "s1", ev("s1", lc.ACTIVITY))
    # The corrupt record is untouched, never silently reinitialized.
    assert (store / "s1.json").read_text() == "{ this is not json"


def test_spool_id_mismatch_raises(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s1")
    with pytest.raises(ValueError):
        lc.apply_event(store, "s1", ev("other", lc.ACTIVITY))


def test_apply_event_rejects_unsafe_spool_id(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s1")
    victim = tmp_path / "victim.json"
    victim.write_text('{"id": "victim", "status": "running"}')
    # The unsafe id is rejected before any lock or write; the sibling is untouched.
    with pytest.raises(ValueError):
        lc.apply_event(store, "../victim", ev("s1", lc.ACTIVITY))
    assert victim.read_text() == '{"id": "victim", "status": "running"}'


# --- integration: coexistence with owner keys -------------------------------


def test_apply_event_preserves_owner_episode_and_flat_lifecycle(tmp_path):
    store = tmp_path / "store"
    make_spool_record(
        store,
        "s1",
        extra={
            "owner_episode": {"generation": 2, "revision": 5, "phase": "accepted"},
            "lifecycle": {"ownership_state": "held", "transport_state": "reaped"},
        },
    )
    lc.apply_event(store, "s1", ev("s1", lc.CONVERSATION_ACCEPTED, provider_ids={"thread_id": "t1"}))
    record = read_record(store, "s1")
    assert record["owner_episode"] == {"generation": 2, "revision": 5, "phase": "accepted"}
    # Owner's flat lifecycle keys are untouched...
    assert record["lifecycle"]["ownership_state"] == "held"
    assert record["lifecycle"]["transport_state"] == "reaped"
    # ...and live beside the nested reduced provider block.
    assert record["lifecycle"]["provider"]["protocol_state"] == lc.PROTOCOL_ACCEPTED
    assert record["lifecycle"]["provider"]["provider_ids"] == {"thread_id": "t1"}


# --- record lock discipline -------------------------------------------------


def test_lock_path_parity_with_spindle():
    # apply_event must lock the same pathname spindle._spool_lock uses.
    assert lc._lock_path(spindle.SPINDLE_DIR, "s1") == spindle._get_lock_path("s1")


def test_apply_event_holds_record_lock_during_write(tmp_path, monkeypatch):
    store = tmp_path / "store"
    make_spool_record(store, "s1")
    real_write = lc._atomic_json_write
    observed = {}

    def probing(path, value):
        fd = os.open(str(store / "s1.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["locked_out"] = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                observed["locked_out"] = True
        finally:
            os.close(fd)
        return real_write(path, value)

    monkeypatch.setattr(lc, "_atomic_json_write", probing)
    lc.apply_event(store, "s1", ev("s1", lc.TRANSPORT_STARTED))
    assert observed["locked_out"] is True, "record lock must be held during the durable write"


# --- crash atomicity --------------------------------------------------------


def test_crash_before_replace_keeps_old_snapshot_and_reconverges(tmp_path, monkeypatch):
    # Uninterrupted reference outcome.
    ref = tmp_path / "ref"
    make_spool_record(ref, "s")
    lc.apply_event(ref, "s", ev("s", lc.TURN_TERMINAL, terminal_kind="completed"))
    reference_bytes = (ref / "s.json").read_bytes()

    crash = tmp_path / "crash"
    make_spool_record(crash, "s")
    original_bytes = (crash / "s.json").read_bytes()

    def boom(src, dst):
        raise InjectedCrash("replace interrupted")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(InjectedCrash):
            lc.apply_event(crash, "s", ev("s", lc.TURN_TERMINAL, terminal_kind="completed"))

    # The prior snapshot is intact and readable; no torn temp left behind.
    assert (crash / "s.json").read_bytes() == original_bytes
    assert not list(crash.glob("*.tmp"))

    # Re-applying reaches a byte-identical fixed point equal to the reference.
    lc.apply_event(crash, "s", ev("s", lc.TURN_TERMINAL, terminal_kind="completed"))
    assert (crash / "s.json").read_bytes() == reference_bytes


def test_crash_after_replace_before_dir_fsync_keeps_new_content(tmp_path, monkeypatch):
    store = tmp_path / "store"
    make_spool_record(store, "s")
    real_fsync = os.fsync
    calls = {"n": 0}

    def boom(fd):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = file fsync, 2 = directory fsync (after replace)
            raise InjectedCrash("crash after replace, before directory fsync")
        return real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(os, "fsync", boom)
        with pytest.raises(InjectedCrash):
            lc.apply_event(store, "s", ev("s", lc.TURN_TERMINAL, terminal_kind="completed"))

    # The replace already happened, so the new content is present and parseable.
    provider = read_provider(store, "s")
    assert provider["protocol_terminal_kind"] == "completed"

    # A driver re-send after the ambiguous ack converges the authoritative state,
    # even though the non-authoritative sequence advances (at-least-once).
    lc.apply_event(store, "s", ev("s", lc.TURN_TERMINAL, terminal_kind="completed"))
    reconverged = read_provider(store, "s")
    assert reconverged["protocol_terminal_kind"] == "completed"
    assert reconverged["sequence"] > provider["sequence"]


# --- monotonicity guard in _write_spool -------------------------------------


def test_write_spool_guard_preserves_higher_provider_sequence():
    store = spindle.SPINDLE_DIR  # _write_spool operates on the module store
    make_spool_record(store, "s")
    for _ in range(3):
        lc.apply_event(store, "s", ev("s", lc.ACTIVITY))
    assert read_provider(store, "s")["sequence"] == 3

    # A stale metadata writer carries a lower provider.sequence plus a new field.
    stale = read_record(store, "s")
    stale["lifecycle"] = {"provider": {"sequence": 1, "protocol_state": "starting"}}
    stale["metadata_field"] = "written"
    spindle._write_spool("s", stale)

    after = read_record(store, "s")
    assert after["lifecycle"]["provider"]["sequence"] == 3, "guard must keep the higher on-disk provider block"
    assert after["metadata_field"] == "written", "non-guarded fields still persist"


def test_write_spool_guard_allows_equal_or_higher_sequence():
    store = spindle.SPINDLE_DIR
    make_spool_record(store, "s")
    lc.apply_event(store, "s", ev("s", lc.ACTIVITY))
    record = read_record(store, "s")
    record["lifecycle"]["provider"]["sequence"] = 9
    record["lifecycle"]["provider"]["protocol_state"] = "active"
    spindle._write_spool("s", record)
    assert read_provider(store, "s")["sequence"] == 9


# --- driver-side coalescing bounds durable writes ---------------------------


def test_coalescer_bounds_durable_write_rate(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s")
    driver = FakeDriver(store, "s", threshold=50)
    driver.emit(lc.CONVERSATION_ACCEPTED)
    driver.emit(lc.TURN_STARTED)
    for i in range(10_000):
        driver.activity(summary=f"tok{i}")
    driver.emit(lc.TURN_TERMINAL, terminal_kind="completed")

    provider = driver.provider()
    assert provider["protocol_terminal_kind"] == "completed"
    # 10k raw observations must not become 10k durable writes.
    assert driver.applied < 400
    assert driver.coalescer.total_observed == 10_000
    # The snapshot stays bounded regardless of activity volume.
    assert len(json.dumps(provider)) < 4000


def test_fake_driver_reconnect_ignores_late_old_epoch_terminal(tmp_path):
    store = tmp_path / "store"
    make_spool_record(store, "s")
    driver = FakeDriver(store, "s")
    driver.emit(lc.PROTOCOL_CONNECTED)
    driver.emit(lc.CONVERSATION_ACCEPTED)
    driver.emit(lc.TURN_STARTED)
    driver.emit(lc.APPROVAL_REQUESTED)
    assert driver.provider()["protocol_state"] == lc.PROTOCOL_WAITING

    driver.reconnect()  # connection epoch -> 1
    driver.emit(lc.PROTOCOL_CONNECTED)
    assert driver.provider()["protocol_state"] == lc.PROTOCOL_STARTING

    # A terminal from the stale epoch arrives out of order: ignored as evidence.
    driver.emit_raw(ev("s", lc.TURN_TERMINAL, terminal_kind="completed", epoch=0))
    provider = driver.provider()
    assert provider["protocol_terminal_kind"] is None
    assert provider["connection_epoch"] == 1

    # The current epoch reaches its own real terminal.
    driver.emit(lc.CONVERSATION_ACCEPTED)
    driver.emit(lc.TURN_STARTED)
    driver.emit(lc.TURN_TERMINAL, terminal_kind="completed")
    assert driver.provider()["protocol_terminal_kind"] == "completed"
