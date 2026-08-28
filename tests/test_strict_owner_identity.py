"""Regression contract for strict durable owner identity decoding.

These tests intentionally exercise the existing production entry points.  The
durable records are authority-bearing, so present malformed values must never
be normalized into valid PIDs, generations, birth tokens, or inode identities.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

import spindle
from spindle import owner_episode_convergence, owner_watchdog
from spindle.namespace_owner import (
    LivenessEvidence,
    NamespaceIdentity,
    ProcessIdentity,
    active_spool_count,
    assess_process_liveness,
    retire_owner_artifacts,
    transition_owner_episode,
)
from spindle.namespace_owner_process import LogicalOwner, build_parser
from tests.owner_episode_fixtures import LOCK, NAMESPACE, OWNER, PROVIDER, STARTER, WATCHDOG, make_episode

PID_MAX = 2**31 - 1


def _identity_payload(**changes) -> dict:
    payload = {
        "pid": 4244,
        "birth_token": "9003",
        "namespace": deepcopy(NAMESPACE),
        "owner_generation": 7,
        "child_pgid": 4245,
        "lock_device": 64,
        "lock_inode": 987,
        "lock_created": True,
        "legacy_service_identity": None,
    }
    payload.update(changes)
    return payload


def _owner_exit_payload(**changes) -> dict:
    payload = {
        "owner_generation": 7,
        "provider_reaped": True,
        "cleanup_outcome": "natural_exit",
    }
    payload.update(changes)
    return payload


def test_process_identity_round_trips_the_canonical_nine_field_shape():
    payload = _identity_payload()

    decoded = ProcessIdentity.from_dict(payload)

    assert decoded.to_dict() == payload
    assert set(decoded.to_dict()) == {
        "pid",
        "birth_token",
        "namespace",
        "owner_generation",
        "child_pgid",
        "lock_device",
        "lock_inode",
        "lock_created",
        "legacy_service_identity",
    }


@pytest.mark.parametrize("shape", ["missing", "extra"])
def test_process_identity_requires_exact_fields(shape):
    payload = _identity_payload()
    if shape == "missing":
        payload.pop("legacy_service_identity")
    else:
        payload["unexpected"] = None

    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(payload)


@pytest.mark.parametrize("field", ["pid", "owner_generation"])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1", None, 0, -1])
def test_process_identity_rejects_nonplain_or_nonpositive_numeric_fields(field, invalid):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(**{field: invalid}))


@pytest.mark.parametrize("field", ["pid", "child_pgid"])
@pytest.mark.parametrize("invalid", [2**31, 2**63, -(2**31)])
def test_process_identity_rejects_out_of_range_pid_fields(field, invalid):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(**{field: invalid}))


@pytest.mark.parametrize("field", ["child_pgid", "lock_device", "lock_inode"])
@pytest.mark.parametrize("invalid", [True, False, 1.0, "1"])
def test_process_identity_rejects_nullable_numeric_coercions(field, invalid):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(**{field: invalid}))


@pytest.mark.parametrize("field", ["lock_device", "lock_inode"])
def test_process_identity_rejects_negative_lock_coordinates(field):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(**{field: -1}))


@pytest.mark.parametrize("invalid", [0, -1, -4245, -(2**31) + 1])
def test_process_identity_rejects_nonpositive_child_process_group(invalid):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(child_pgid=invalid))


@pytest.mark.parametrize(
    "changes",
    [
        {"birth_token": 9003},
        {"birth_token": ""},
        {"birth_token": "bad\x00token"},
        {"lock_created": 1},
        {"lock_created": "true"},
        {"lock_device": None},
        {"lock_inode": None},
        {"lock_device": None, "lock_inode": None, "lock_created": True},
        {"legacy_service_identity": 1},
        {"legacy_service_identity": ""},
    ],
)
def test_process_identity_rejects_malformed_strings_booleans_and_lock_relationships(changes):
    with pytest.raises(ValueError):
        ProcessIdentity.from_dict(_identity_payload(**changes))


def test_process_identity_accepts_nullable_legacy_fields_when_relationships_hold():
    payload = _identity_payload(
        child_pgid=None,
        lock_device=None,
        lock_inode=None,
        lock_created=False,
        legacy_service_identity="legacy-owner",
    )

    assert ProcessIdentity.from_dict(payload).to_dict() == payload


def test_liveness_compares_birth_tokens_without_string_coercion(fake_process_ops):
    namespace = NamespaceIdentity.supported(5, 9)
    fake_process_ops.namespace = namespace
    fake_process_ops.starttime = "101"
    identity = ProcessIdentity(
        pid=123,
        birth_token=101,
        namespace=namespace,
        owner_generation=1,
        child_pgid=None,
        lock_device=None,
        lock_inode=None,
        lock_created=False,
    )

    assert assess_process_liveness(identity, ops=fake_process_ops) == LivenessEvidence(
        "unverifiable", "identity_mismatch"
    )


@pytest.mark.parametrize(
    "role,fact_changes",
    [
        ("starter", {"pid": True}),
        ("starter", {"birth_token": 9001}),
        ("watchdog", {"pid": 1.0}),
        ("owner", {"namespace": {"status": "supported", "device": True, "inode": 11}}),
        ("provider", {"pgid": "4245"}),
    ],
)
def test_episode_identity_consumers_reject_coerced_role_facts(role, fact_changes):
    episode = make_episode("accepted")
    episode[role] = {**episode[role], **fact_changes}

    assert spindle._episode_process_identity(episode, role) is None


@pytest.mark.parametrize(
    "fact_name,fact",
    [
        ("starter", {**STARTER, "pid": True}),
        ("watchdog", {**WATCHDOG, "birth_token": 9002}),
        ("owner", {**OWNER, "pid": 1.0}),
        ("lock", {**LOCK, "inode": "987"}),
        ("provider", {**PROVIDER, "pgid": False}),
    ],
)
def test_episode_transition_rejects_malformed_identity_facts(tmp_path, fact_name, fact):
    spool_id = f"malformed-{fact_name}"
    if fact_name == "starter":
        result = transition_owner_episode(
            tmp_path,
            spool_id,
            actor="launcher",
            destination="reserved",
            generation=1,
            expected_revision=None,
            facts={"starter": fact},
            create_only=True,
        )
    else:
        phase, actor, destination = {
            "watchdog": ("reserved", "launcher", "reserved"),
            "owner": ("reserved", "owner", "lock_bound"),
            "lock": ("reserved", "owner", "lock_bound"),
            "provider": ("lock_bound", "owner", "accepted"),
        }[fact_name]
        episode = make_episode(phase, generation=2, path="before_watchdog" if phase == "reserved" else None)
        if fact_name in {"owner", "lock"}:
            episode["watchdog"] = deepcopy(WATCHDOG)
            episode["revision"] = 2
        record = {"id": spool_id, "status": "pending", "owner_episode": episode}
        (tmp_path / f"{spool_id}.json").write_text(json.dumps(record), encoding="utf-8")
        facts = {
            "watchdog": {"watchdog": fact},
            "owner": {"owner": fact, "lock": deepcopy(LOCK)},
            "lock": {"owner": deepcopy(OWNER), "lock": fact},
            "provider": {
                "provider": fact,
                "provider_custody": {
                    "pidfd_acquired": True,
                    "containment": "watchdog",
                    "published_at": "2026-08-28T00:00:00+00:00",
                },
            },
        }[fact_name]
        result = transition_owner_episode(
            tmp_path,
            spool_id,
            actor=actor,
            destination=destination,
            generation=2,
            expected_revision=episode["revision"],
            facts=facts,
        )

    assert (result.accepted, result.rejection) == (False, "contradictory_facts")


@pytest.mark.parametrize("invalid", ["7", 7.0, True])
def test_episode_transition_rejects_nonplain_cleanup_exit_codes(tmp_path, invalid):
    spool_id = "malformed-cleanup-exit"
    episode = make_episode("accepted", generation=1)
    (tmp_path / f"{spool_id}.json").write_text(
        json.dumps({"id": spool_id, "status": "running", "owner_episode": episode}), encoding="utf-8"
    )
    cleanup = {
        "outcome": "natural_exit",
        "provider_reaped": True,
        "adopted_children_reaped": 0,
        "child_exit_observed_at": "2026-08-28T00:00:00+00:00",
        "provider_exit_code": invalid,
    }

    result = transition_owner_episode(
        tmp_path,
        spool_id,
        actor="owner",
        destination="cleanup_proven",
        generation=1,
        expected_revision=episode["revision"],
        facts={"cleanup": cleanup},
    )

    assert (result.accepted, result.rejection) == (False, "contradictory_facts")


@pytest.mark.parametrize("invalid", ["7", 7.0, True])
def test_persisted_cleanup_exit_code_never_becomes_terminal_evidence(tmp_path, monkeypatch, invalid):
    spool_id = "persisted-malformed-cleanup-exit"
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    episode = make_episode("cleanup_proven", generation=1)
    episode["cleanup"]["provider_exit_code"] = invalid

    assert owner_episode_convergence._exit_evidence(spool_id, {"id": spool_id}, episode) == (
        None,
        None,
        "episode_cleanup_malformed",
    )


@pytest.mark.parametrize(
    "fact_name,malformed",
    [
        ("starter", {**STARTER, "pid": True}),
        ("watchdog", {**WATCHDOG, "birth_token": 9002}),
        ("owner", {**OWNER, "namespace": {"status": "supported", "device": True, "inode": 11}}),
        ("provider", {**PROVIDER, "pgid": -4245}),
        ("lock", {**LOCK, "inode": "987"}),
    ],
)
def test_classifier_marks_persisted_malformed_episode_identity_unhealthy(tmp_path, monkeypatch, fact_name, malformed):
    spool_id = f"persisted-malformed-{fact_name}"
    episode = make_episode("cleanup_proven", generation=1)
    episode[fact_name] = malformed
    record = {"id": spool_id, "status": "running", "lifecycle": {}, "owner_episode": episode}
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(
        spindle,
        "_owner_episode_observation",
        lambda _record: (
            spindle.LockEvidence("released"),
            spindle.LivenessEvidence("dead", "pidfd_exited"),
        ),
    )
    observer = owner_episode_convergence.ObserverIdentity(
        pid=1,
        namespace=NamespaceIdentity.from_dict(NAMESPACE),
        local_effects={},
    )

    meaning = owner_episode_convergence.classify_owner_episode_record(record, observer)

    assert meaning.classification == "store_unhealthy"
    expected_reason = "ownership_identity_mismatch" if fact_name == "owner" else f"malformed_{fact_name}_identity"
    assert meaning.reason == expected_reason
    assert meaning.may_mutate is False


def test_active_spool_count_does_not_probe_a_malformed_reserved_identity(monkeypatch):
    episode = make_episode("reserved", path="before_watchdog")
    episode["starter"]["pid"] = 1.0
    monkeypatch.setattr(
        spindle.namespace_owner,
        "assess_process_liveness",
        lambda _identity: (_ for _ in ()).throw(AssertionError("malformed PID reached process observation")),
    )

    assert active_spool_count([{"id": "malformed", "status": "pending", "owner_episode": episode}]) == 1


def test_malformed_owner_identity_sidecar_is_unhealthy_before_pid_or_inode_access(tmp_path, monkeypatch):
    spool_id = "malformed-owner-sidecar"
    path = spindle._get_owner_identity_path(spool_id)
    path.write_text(json.dumps(_identity_payload(pid=1.0)), encoding="utf-8")
    monkeypatch.setattr(
        spindle,
        "assess_process_liveness",
        lambda _identity: (_ for _ in ()).throw(AssertionError("malformed identity reached PID observation")),
    )
    monkeypatch.setattr(
        spindle,
        "probe_ownership_lock",
        lambda _path, _identity: (_ for _ in ()).throw(AssertionError("malformed identity reached inode probe")),
    )

    result = spindle._reconcile_spool_ownership({"id": spool_id, "status": "running", "owner_generation": 7})

    assert (result.state, result.reason) == ("store_unhealthy", "owner_identity_unreadable")


@pytest.mark.parametrize("malformation", ["oversized_integer", "deep_nesting"])
def test_owner_identity_loader_contains_json_resource_limits(malformation):
    spool_id = f"owner-identity-{malformation}"
    path = spindle._get_owner_identity_path(spool_id)
    if malformation == "oversized_integer":
        limit = sys.get_int_max_str_digits()
        if not limit:
            pytest.skip("Python integer string conversion limit is disabled")
        path.write_text('{"pid":' + "9" * (limit + 1) + "}", encoding="utf-8")
    else:
        path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

    assert spindle._read_current_owner_identity(spool_id) is None
    result = spindle._reconcile_spool_ownership({"id": spool_id, "status": "running", "owner_generation": 1})
    assert (result.state, result.reason) == ("store_unhealthy", "owner_identity_unreadable")


def test_float_inode_sidecar_cannot_authorize_artifact_retirement(tmp_path, monkeypatch):
    spool_id = "float-inode-retirement"
    lock_path = tmp_path / f"{spool_id}.process-owner"
    lock_path.touch()
    info = lock_path.stat()
    payload = _identity_payload(lock_device=info.st_dev, lock_inode=float(info.st_ino))
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    spindle._get_owner_identity_path(spool_id).write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / f"{spool_id}.json").write_text(json.dumps({"id": spool_id, "status": "complete"}))

    identity = spindle._read_current_owner_identity(spool_id)
    retired = retire_owner_artifacts(tmp_path, spool_id, identity)

    assert retired is False
    assert lock_path.exists()
    assert (tmp_path / f"{spool_id}.json").exists()


@pytest.mark.parametrize(
    "field,invalid",
    [
        *(("launcher_pid", value) for value in (True, 1.0, "1", 0, -1, 2**31)),
        *(("launcher_start_time", value) for value in (True, 1.0, 0, -1, 2**31)),
        *(("launcher_namespace", value) for value in (True, 1.0, "1", 0, -1, 2**31)),
    ],
)
def test_malformed_pending_identity_mirrors_are_unverifiable_not_never_started(monkeypatch, field, invalid):
    spool = {
        "id": "pending-malformed",
        "status": "pending",
        "launcher_pid": 4242,
        "launcher_start_time": "9001",
        "launcher_namespace": deepcopy(NAMESPACE),
    }
    spool[field] = invalid
    monkeypatch.setattr(
        spindle,
        "assess_process_liveness",
        lambda _identity: (_ for _ in ()).throw(AssertionError("malformed pending identity reached PID access")),
    )

    result = spindle._reconcile_spool_ownership(spool)

    assert result.state == "unverifiable"
    assert result.reason == "pending_launcher_identity_malformed"


def test_absent_pending_launcher_identity_preserves_provider_never_started_compatibility():
    result = spindle._reconcile_spool_ownership({"id": "pending-absent", "status": "pending"})

    assert (result.state, result.reason) == ("terminalizable", "pending_provider_never_started")


def _write_spool_record(spool_id: str, record: dict) -> None:
    spindle._get_spool_path(spool_id).write_text(json.dumps(record), encoding="utf-8")


def test_next_owner_generation_uses_the_max_valid_plain_generation():
    spool_id = "valid-generation-allocation"
    _write_spool_record(spool_id, {"id": spool_id, "owner_generation": 3})
    spindle._get_owner_identity_path(spool_id).write_text(
        json.dumps(_identity_payload(owner_generation=7)), encoding="utf-8"
    )

    assert spindle._next_owner_generation(spool_id) == 8


@pytest.mark.parametrize("source", ["spool", "sidecar"])
@pytest.mark.parametrize("invalid", [True, 1.0, "7", 0, -1, None, ""])
def test_next_owner_generation_refuses_malformed_present_evidence(source, invalid):
    spool_id = f"malformed-generation-{source}"
    if source == "spool":
        _write_spool_record(spool_id, {"id": spool_id, "owner_generation": invalid})
    else:
        _write_spool_record(spool_id, {"id": spool_id, "owner_generation": 3})
        payload = _identity_payload(owner_generation=invalid)
        spindle._get_owner_identity_path(spool_id).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="generation"):
        spindle._next_owner_generation(spool_id)


def test_legacy_owner_allocation_refuses_malformed_identity_generation(tmp_path):
    owner = object.__new__(LogicalOwner)
    owner.owner_identity_path = tmp_path / "legacy.owner-identity"
    owner.owner_identity_path.write_text(json.dumps(_identity_payload(owner_generation=1.0)), encoding="utf-8")
    owner.episode_mode = False
    owner.generation = 2
    owner.checkpoints = SimpleNamespace(generation=2)

    with pytest.raises(ValueError, match="generation"):
        owner._allocate_generation()


def test_owner_exit_valid_core_and_extensions_remain_evidence():
    spool_id = "valid-owner-exit"
    spindle._get_owner_exit_path(spool_id).write_text(
        json.dumps(_owner_exit_payload(owner_pid=4244, future_extension={"kept": True})), encoding="utf-8"
    )

    assert spindle._owner_exit_evidence(spool_id, 7) == (True, True)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_generation": True},
        {"owner_generation": 7.0},
        {"owner_generation": "7"},
        {"provider_reaped": 1},
        {"provider_reaped": "true"},
        {"cleanup_outcome": ""},
        {"cleanup_outcome": True},
    ],
)
def test_malformed_owner_exit_is_never_partial_evidence(changes):
    spool_id = "malformed-owner-exit"
    spindle._get_owner_exit_path(spool_id).write_text(json.dumps(_owner_exit_payload(**changes)), encoding="utf-8")

    assert spindle._owner_exit_evidence(spool_id, 7) == (False, False)


@pytest.mark.parametrize("poison", [True, 1.0])
def test_watchdog_preacceptance_guard_rejects_coerced_episode_generation(tmp_path, monkeypatch, poison):
    spool_id = "watchdog-preacceptance-generation"
    episode = make_episode("reserved", generation=1, path="before_watchdog")
    episode["generation"] = poison
    (tmp_path / f"{spool_id}.json").write_text(
        json.dumps({"id": spool_id, "status": "pending", "owner_episode": episode}), encoding="utf-8"
    )
    monkeypatch.setattr(
        owner_watchdog,
        "transition_owner_episode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("coerced generation passed watchdog guard")),
    )

    assert owner_watchdog._record_preacceptance_failure(tmp_path, spool_id, 4244, "9003", 1, 0) is False


@pytest.mark.parametrize("poison", [True, 1.0])
def test_watchdog_compat_guard_rejects_coerced_process_identity_generation(tmp_path, monkeypatch, poison):
    spool_id = "watchdog-process-generation"
    (tmp_path / f"{spool_id}.json").write_text(json.dumps({"id": spool_id, "owner_generation": 1}))
    (tmp_path / f"{spool_id}.process-identity").write_text(
        json.dumps({"owner_generation": poison, "provider_pid": 4245})
    )
    writes = []
    monkeypatch.setattr(owner_watchdog, "_atomic_json_write", lambda *args: writes.append(args))

    owner_watchdog._record_owner_crash(tmp_path, spool_id, 4244, 1, "9003", 0, True)

    assert writes == []


def test_watchdog_compat_guard_rejects_negative_provider_process_group(tmp_path, monkeypatch):
    spool_id = "watchdog-negative-provider-pgid"
    (tmp_path / f"{spool_id}.json").write_text(json.dumps({"id": spool_id, "owner_generation": 1}))
    (tmp_path / f"{spool_id}.process-identity").write_text(
        json.dumps({"owner_generation": 1, "provider_pid": 4245, "provider_pgid": -4245})
    )
    writes = []
    monkeypatch.setattr(owner_watchdog, "_atomic_json_write", lambda *args: writes.append(args))

    owner_watchdog._record_owner_crash(tmp_path, spool_id, 4244, 1, "9003", 0, True)

    assert writes == []


def test_watchdog_episode_crash_ignores_malformed_process_sidecar_and_finishes_evidence(tmp_path):
    spool_id = "watchdog-episode-malformed-process"
    episode = make_episode("accepted", generation=1)
    (tmp_path / f"{spool_id}.json").write_text(
        json.dumps({"id": spool_id, "status": "running", "owner_episode": episode}), encoding="utf-8"
    )
    (tmp_path / f"{spool_id}.process-identity").write_text(
        json.dumps({"owner_generation": 1.0, "provider_pid": 4245}), encoding="utf-8"
    )

    owner_watchdog._record_owner_crash(tmp_path, spool_id, 4244, 1, "9003", 0, True)

    updated = json.loads((tmp_path / f"{spool_id}.json").read_text())
    evidence = json.loads((tmp_path / f"{spool_id}.owner-exit").read_text())
    assert updated["owner_episode"]["phase"] == "cleanup_proven"
    assert evidence["owner_generation"] == 1
    assert evidence["provider_pid"] is None
    assert evidence["owner_crashed"] is True
    assert evidence["provider_reaped"] is True


def test_watchdog_replaces_instead_of_merging_a_malformed_existing_exit(tmp_path, monkeypatch):
    spool_id = "watchdog-malformed-existing-exit"
    (tmp_path / f"{spool_id}.json").write_text(json.dumps({"id": spool_id, "owner_generation": 1}))
    (tmp_path / f"{spool_id}.process-identity").write_text(json.dumps({"owner_generation": 1, "provider_pid": 4245}))
    (tmp_path / f"{spool_id}.owner-exit").write_text(
        json.dumps(
            _owner_exit_payload(
                owner_generation=True,
                owner_crashed_after_cleanup=True,
                attacker_extension="must-not-survive",
            )
        )
    )
    writes = []
    monkeypatch.setattr(owner_watchdog, "_atomic_json_write", lambda path, value: writes.append((path, value)))

    owner_watchdog._record_owner_crash(tmp_path, spool_id, 4244, 1, "9003", 0, True)

    assert len(writes) == 1
    path, evidence = writes[0]
    assert path == tmp_path / f"{spool_id}.owner-exit"
    assert evidence["owner_generation"] == 1
    assert evidence["owner_crashed"] is True
    assert "owner_crashed_after_cleanup" not in evidence
    assert "attacker_extension" not in evidence


@pytest.mark.parametrize("poison", [True, 1.0])
def test_convergence_ignores_coerced_owner_exit_generation(tmp_path, monkeypatch, poison):
    spool_id = "convergence-sidecar-generation"
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    spindle._get_owner_exit_path(spool_id).write_text(
        json.dumps(_owner_exit_payload(owner_generation=poison, provider_exit_code=7)), encoding="utf-8"
    )
    episode = make_episode("cleanup_proven", generation=1)

    assert owner_episode_convergence._exit_evidence(spool_id, {"id": spool_id}, episode) == (
        0,
        "episode_cleanup",
        None,
    )


def test_convergence_request_generation_comparison_is_type_strict(monkeypatch):
    request = SimpleNamespace(owner_generation=1)
    monkeypatch.setattr(spindle, "iter_control_requests", lambda *_args: [request])

    assert owner_episode_convergence._current_generation_requests("strict-generation", True) == ()


def test_convergence_receipt_generation_comparison_is_type_strict():
    receipt = SimpleNamespace(
        owner_generation=1,
        cleanup_outcome="cleaned",
        owner_acknowledged_at=None,
        child_exit_observed_at=None,
    )
    intent = {
        "owner_generation": True,
        "winning_request_id": None,
        "disposition": {"no_winner": "cleaned"},
    }

    assert owner_episode_convergence._receipt_settles(receipt, "request-1", intent) is False


@pytest.mark.parametrize("loader", ["watchdog", "convergence", "owner"])
def test_identity_loaders_treat_over_limit_json_integers_as_malformed(tmp_path, loader):
    limit = sys.get_int_max_str_digits()
    if not limit:
        pytest.skip("Python integer string conversion limit is disabled")
    path = tmp_path / "oversized.json"
    path.write_text('{"owner_generation":' + "9" * (limit + 1) + "}", encoding="utf-8")

    if loader == "watchdog":
        assert owner_watchdog._load(path) is None
    elif loader == "convergence":
        assert owner_episode_convergence._read_json(path) is None
    else:
        owner = object.__new__(LogicalOwner)
        owner.spool_path = path
        owner.spool_id = "oversized"
        record = owner._read_spool()
        assert record["id"] == "oversized"
        assert record["status"] == "pending"
        assert isinstance(record["created_at"], str) and record["created_at"]


@pytest.mark.parametrize("loader", ["watchdog", "convergence", "owner"])
def test_identity_loaders_contain_deep_json_recursion(tmp_path, loader):
    path = tmp_path / "deep.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

    if loader == "watchdog":
        assert owner_watchdog._load(path) is None
    elif loader == "convergence":
        assert owner_episode_convergence._read_json(path) is None
    else:
        owner = object.__new__(LogicalOwner)
        owner.spool_path = path
        owner.spool_id = "deep"
        record = owner._read_spool()
        assert record["id"] == "deep"
        assert record["status"] == "pending"


@pytest.mark.parametrize("function", ["process", "group"])
@pytest.mark.parametrize("invalid", [True, 1.0, "4244", 0, -1, 2**31])
def test_legacy_process_identity_paths_reject_malformed_pids_before_proc_or_signal_access(
    monkeypatch, function, invalid
):
    monkeypatch.setattr(
        spindle,
        "_process_start_time",
        lambda _pid: (_ for _ in ()).throw(AssertionError("malformed PID reached /proc")),
    )
    target = (
        spindle._spool_process_identity_matches
        if function == "process"
        else spindle._spool_process_group_identity_matches
    )

    assert target({"id": "legacy", "pid": invalid, "process_start_time": "9003"}) is False


@pytest.mark.parametrize("invalid", ["+1", "01", "1_0", " 1", "١"])
def test_owner_generation_cli_rejects_noncanonical_decimal_spellings(invalid):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--store", "/tmp/store", "--spool-id", "spool", "--generation", invalid])


def test_watchdog_rejects_noncanonical_generation_before_fork(monkeypatch):
    monkeypatch.setattr(owner_watchdog, "_set_subreaper", lambda: None)
    monkeypatch.setattr(
        owner_watchdog.os,
        "pipe",
        lambda: (_ for _ in ()).throw(AssertionError("watchdog fork setup began before generation validation")),
    )

    with pytest.raises(ValueError, match="generation"):
        owner_watchdog.main(["--store", "/tmp/store", "--spool-id", "spool", "--generation", "01", "--", "provider"])
