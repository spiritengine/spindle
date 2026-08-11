"""Unit contracts for bridge supervisor negotiation and store layout."""

from __future__ import annotations

import json

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


def test_legacy_scalar_v1_record_uses_exact_fallback():
    legacy = {
        "pid": 123,
        "supervisor_protocol_version": 1,
        "spool_schema_version": 1,
        "future_diagnostic": {"ignored": True},
    }

    assert spindle._supervisor_compatibility_error(legacy) is None
    assert "protocol is incompatible" in spindle._supervisor_compatibility_error(
        {**legacy, "supervisor_protocol_version": 2}
    )
    assert "schema is incompatible" in spindle._supervisor_compatibility_error({**legacy, "spool_schema_version": 2})


def test_bridge_identity_is_truthful_and_accepted_by_pre_bridge_reader():
    record = spindle._supervisor_identity(123)

    assert record["supervisor_protocol_version"] == 1
    assert record["spool_schema_version"] == 1
    assert record["supported_supervisor_protocol_range"] == {"min": 1, "max": 1}
    assert record["readable_spool_schemas"] == [1]
    assert record["writable_spool_schema"] == 1
    assert record["supervisor_capabilities"] == ["supervisor-compatibility-ranges"]
    assert _pre_bridge_reader_accepts(record)
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
            {"min": 2, "max": 3},
            "owner_supported=2-3 launcher_required=1-1",
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
            [],
            "launcher_requires_capabilities=['supervisor-compatibility-ranges']",
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
