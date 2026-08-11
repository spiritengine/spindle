"""Unit contracts for bridge supervisor negotiation and store layout."""

from __future__ import annotations

import fcntl
import json
import os

import pytest

import spindle


def _pre_bridge_reader_accepts(record: dict) -> bool:
    """Frozen model of the exact scalar check shipped before the bridge."""
    return record.get("supervisor_protocol_version") == 1 and record.get("spool_schema_version") == 1


def test_store_layout_names_sibling_v2_but_activates_only_v1(tmp_path):
    home = tmp_path / "spindle-home"

    layout = spindle._resolve_store_layout(home)

    assert layout.schema1_root == home / "spools"
    assert layout.schema2_root == home / "spools-v2"
    assert layout.active_root == layout.schema1_root
    assert layout.active_schema == 1
    assert not home.exists()


def test_scalar_record_uses_exact_current_fallback():
    current = {
        "pid": 123,
        "supervisor_protocol_version": 2,
        "spool_schema_version": 1,
        "future_diagnostic": {"ignored": True},
    }

    assert spindle._supervisor_compatibility_error(current) is None
    assert "protocol is incompatible" in spindle._supervisor_compatibility_error(
        {**current, "supervisor_protocol_version": 1}
    )
    assert "schema is incompatible" in spindle._supervisor_compatibility_error({**current, "spool_schema_version": 2})


def test_current_identity_is_truthful_and_rejected_by_pre_bridge_reader():
    record = spindle._supervisor_identity(123)

    assert record["supervisor_protocol_version"] == 2
    assert record["spool_schema_version"] == 1
    assert record["supported_supervisor_protocol_range"] == {"min": 2, "max": 2}
    assert record["readable_spool_schemas"] == [1]
    assert record["writable_spool_schema"] == 1
    assert record["supervisor_capabilities"] == [
        "supervisor-compatibility-ranges",
        "owner-episode-v1",
    ]
    assert not _pre_bridge_reader_accepts(record)
    assert spindle._supervisor_compatibility_error(record) is None


def test_complete_negotiation_replaces_scalar_equality():
    record = spindle._supervisor_identity(123)
    record["supervisor_protocol_version"] = 999
    record["spool_schema_version"] = 999

    assert spindle._supervisor_compatibility_error(record) is None


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        (
            "supported_supervisor_protocol_range",
            {"min": 3, "max": 4},
            "owner_supported=3-4 launcher_required=2-2",
        ),
        (
            "readable_spool_schemas",
            [2],
            "owner_readable_spool_schemas=[2] launcher_requires_writable_schema=1",
        ),
        (
            "writable_spool_schema",
            2,
            "owner_writable_spool_schema=2 launcher_readable_spool_schemas=[1]",
        ),
        (
            "supervisor_capabilities",
            ["supervisor-compatibility-ranges"],
            "launcher_requires_capabilities=['owner-episode-v1', 'supervisor-compatibility-ranges']",
        ),
    ],
)
def test_present_incompatible_negotiation_is_precise(field, value, diagnostic):
    record = spindle._supervisor_identity(456)
    record["package"] = "/foreign/spindle"
    record[field] = value

    error = spindle._supervisor_compatibility_error(record)

    assert error is not None
    assert "live owner pid=456 package='/foreign/spindle'" in error
    assert diagnostic in error


def test_partial_or_malformed_negotiation_fails_closed():
    incomplete = spindle._supervisor_identity(123)
    incomplete.pop("supervisor_capabilities")
    malformed = spindle._supervisor_identity(123)
    malformed["supported_supervisor_protocol_range"] = {"min": True, "max": 1}

    assert "compatibility record is incomplete" in spindle._supervisor_compatibility_error(incomplete)
    assert "compatibility record is invalid" in spindle._supervisor_compatibility_error(malformed)


def test_unknown_nested_negotiation_fields_are_ignored_safely():
    record = spindle._supervisor_identity(123)
    record["supported_supervisor_protocol_range"]["future_preference"] = 1

    assert spindle._supervisor_compatibility_error(record) is None


def test_supervisor_record_write_preserves_unknown_additive_fields(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    path = store / ".supervisor.json"
    path.write_text(
        json.dumps(
            {
                "pid": 1,
                "supervisor_protocol_version": 1,
                "spool_schema_version": 1,
                "retired_at": "stale",
                "future_metadata": {"opaque": [1, 2, 3]},
            }
        )
    )

    spindle._write_supervisor_record(spindle._supervisor_identity(789))
    written = json.loads(path.read_text())

    assert written["pid"] == 789
    assert written["future_metadata"] == {"opaque": [1, 2, 3]}
    assert "retired_at" not in written


def test_maintenance_gate_open_without_store_lock_or_holder(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)

    assert spindle._live_owner_blocks_store_maintenance() is False
    assert not store.exists()

    store.mkdir()
    assert spindle._live_owner_blocks_store_maintenance() is False

    (store / ".supervisor.lock").touch()
    assert spindle._live_owner_blocks_store_maintenance() is False


def test_maintenance_gate_blocks_live_incompatible_or_silent_owner(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    lock_fd = os.open(store / ".supervisor.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        # A held lifetime lock with no handshake is not ours to maintain.
        assert spindle._live_owner_blocks_store_maintenance() is True

        record = spindle._supervisor_identity(os.getpid())
        (store / ".supervisor.json").write_text(json.dumps(record))
        assert spindle._live_owner_blocks_store_maintenance() is False

        record["supported_supervisor_protocol_range"] = {"min": 3, "max": 4}
        (store / ".supervisor.json").write_text(json.dumps(record))
        assert spindle._live_owner_blocks_store_maintenance() is True
    finally:
        os.close(lock_fd)


@pytest.mark.skipif(os.geteuid() == 0, reason="permission probes are meaningless as root")
def test_maintenance_gate_fails_safe_on_unreadable_lock(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    lock = store / ".supervisor.lock"
    lock.touch()
    lock.chmod(0)
    try:
        assert spindle._live_owner_blocks_store_maintenance() is True
    finally:
        lock.chmod(0o600)


def _control_lock_is_free(store) -> bool:
    fd = os.open(store / ".supervisor-control.lock", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_store_maintenance_holds_control_lock_and_defers_monitors(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    events = []
    monkeypatch.setattr(
        spindle, "_cleanup_old_spools", lambda: events.append(("cleanup", _control_lock_is_free(store)))
    )
    monkeypatch.setattr(
        spindle,
        "_recovery_pass",
        lambda: events.append(("pass", _control_lock_is_free(store))) or ["needs-monitor"],
    )
    monkeypatch.setattr(
        spindle,
        "_start_spool_monitor",
        lambda spool_id: events.append(("monitor", spool_id, _control_lock_is_free(store))),
    )

    spindle._run_store_maintenance(cleanup=True)

    # The probe and both mutating passes share one lock hold; monitor starts
    # happen after release because they reacquire the control lock themselves.
    assert events == [("cleanup", False), ("pass", False), ("monitor", "needs-monitor", True)]


def test_store_maintenance_refused_only_while_incompatible_owner_lives(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    ran = []
    monkeypatch.setattr(spindle, "_cleanup_old_spools", lambda: ran.append("cleanup"))
    monkeypatch.setattr(spindle, "_recovery_pass", lambda: ran.append("pass") or [])
    record = spindle._supervisor_identity(os.getpid())
    record["supported_supervisor_protocol_range"] = {"min": 3, "max": 4}
    (store / ".supervisor.json").write_text(json.dumps(record))
    lock_fd = os.open(store / ".supervisor.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        spindle._run_store_maintenance(cleanup=True)
        assert ran == []
    finally:
        os.close(lock_fd)

    spindle._run_store_maintenance(cleanup=True)
    assert ran == ["cleanup", "pass"]


def test_compatibility_checks_do_not_rewrite_unknown_spool_fields(monkeypatch, tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
    spool = {
        "id": "opaque",
        "status": "complete",
        "spool_schema_version": 1,
        "future_lifecycle": {"state": "unrecognized", "sequence": 7},
    }
    spindle._write_spool("opaque", spool)
    before = (store / "opaque.json").read_bytes()

    assert spindle._supervisor_compatibility_error(spindle._supervisor_identity(123)) is None

    assert (store / "opaque.json").read_bytes() == before
    assert spindle._read_spool("opaque") == spool
