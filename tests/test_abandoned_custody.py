"""Regression contract for an owner episode abandoned without cleanup proof."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import spindle
from spindle import main
from tests.owner_convergence_fixtures import live_process_fact, localize_identities
from tests.owner_episode_fixtures import CLEANUP, CONTAINMENT, make_episode

ABANDONED_REASON = "custody_abandoned_without_cleanup_proof"
ATTESTATION = (
    "I attest that the recorded owner and watchdog are dead and cleanup cannot be proven; "
    "settle this spool as indeterminate abandonment."
)


def _abandoned_record(episode_store, phase: str = "accepted") -> dict:
    spool_id = f"abandoned-{phase}"
    episode = localize_identities(make_episode(phase))
    episode_store.bind_lock(spool_id, episode)
    return episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        prompt="work whose custody was lost",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )


def _fake_systemctl(restarted: threading.Event):
    def run(cmd, *args, **kwargs):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        if "list-unit-files" in cmd:
            result.stdout = "spindle.service enabled"
        elif "restart" in cmd or "start" in cmd:
            restarted.set()
        return result

    return run


@pytest.mark.parametrize("phase", ("lock_bound", "accepted"))
def test_released_episode_with_dead_owner_and_watchdog_is_a_drain_blocker(episode_store, phase):
    record = _abandoned_record(episode_store, phase)
    before = episode_store.spool_path(record["id"]).read_bytes()

    blockers = spindle._drain_blockers()

    assert [(item.spool_id, item.reason) for item in blockers] == [(record["id"], ABANDONED_REASON)]
    assert episode_store.spool_path(record["id"]).read_bytes() == before, (
        "diagnosis fabricated cleanup or terminal evidence"
    )


def test_live_watchdog_keeps_released_dead_owner_episode_out_of_abandoned_diagnosis(episode_store):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    current["owner_episode"]["watchdog"] = live_process_fact()
    episode_store.write(
        record["id"], **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}}
    )

    assert spindle._drain_blockers() == []


def test_drain_blocker_revalidates_after_watchdog_publishes_cleanup(episode_store):
    record = _abandoned_record(episode_store)
    stale = deepcopy(record)
    cleanup = localize_identities(make_episode("cleanup_proven"))
    episode_store.bind_lock(record["id"], cleanup)

    def scan_then_publish_cleanup():
        episode_store.write(
            record["id"],
            status="running",
            episode=cleanup,
            prompt=record["prompt"],
            lifecycle=record["lifecycle"],
        )
        return [stale]

    with patch("spindle._list_spools", side_effect=scan_then_publish_cleanup):
        blockers = spindle._drain_blockers()

    assert episode_store.read(record["id"])["owner_episode"]["phase"] == "cleanup_proven"
    assert blockers == [], "a stale accepted snapshot refused a drain after cleanup became durable"


def test_drain_blocker_scan_skips_history_that_cannot_be_abandoned(episode_store):
    episode_store.write("terminal-history", status="complete", result="done")
    record = _abandoned_record(episode_store)

    with patch(
        "spindle._serialized_abandoned_custody_reason",
        wraps=spindle._serialized_abandoned_custody_reason,
    ) as diagnose:
        blockers = spindle._drain_blockers()

    assert [item.spool_id for item in blockers] == [record["id"]]
    diagnose.assert_called_once_with(record["id"])


@pytest.mark.parametrize("failure", ("foreign_namespace", "reused_pid"))
def test_unverifiable_owner_identity_never_becomes_abandoned(episode_store, failure):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    owner = current["owner_episode"]["owner"]
    if failure == "foreign_namespace":
        owner["namespace"] = {"status": "supported", "device": 999_991, "inode": 999_993}
    else:
        live = live_process_fact()
        owner.update(live, birth_token=f"wrong-{live['birth_token']}")
    episode_store.write(
        record["id"], **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}}
    )

    assert spindle._drain_blockers() == []


@pytest.mark.parametrize("role", ("owner", "watchdog"))
@pytest.mark.parametrize("invalid", ("unavailable", "malformed"))
def test_inexact_role_identity_never_becomes_abandoned(episode_store, role, invalid):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    namespace = spindle.capture_pid_namespace().to_dict()
    if invalid == "unavailable":
        current["owner_episode"][role] = {
            "pid": 999_999_991,
            "birth_token": "unavailable",
            "namespace": namespace,
        }
    else:
        current["owner_episode"][role] = {
            "pid": "999999992",
            "birth_token": ["not-a-token"],
            "namespace": namespace,
        }
    episode_store.write(
        record["id"],
        **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}},
    )

    assert spindle._drain_blockers() == []


@pytest.mark.parametrize("role", ("owner", "watchdog"))
@pytest.mark.parametrize("field", ("device", "inode"))
@pytest.mark.parametrize("coerce", (str, float), ids=("numeric_string", "float"))
def test_coerced_role_namespace_never_becomes_abandoned(episode_store, role, field, coerce):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    namespace = current["owner_episode"][role]["namespace"]
    namespace[field] = coerce(namespace[field])
    episode_store.write(
        record["id"],
        **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}},
    )

    assert spindle._drain_blockers() == []


@pytest.mark.parametrize("field", ("device", "inode"))
@pytest.mark.parametrize("coerce", (str, float), ids=("numeric_string", "float"))
def test_coerced_lock_coordinate_never_becomes_abandoned(episode_store, field, coerce):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    lock = current["owner_episode"]["lock"]
    lock[field] = coerce(lock[field])
    episode_store.write(
        record["id"],
        **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}},
    )

    assert spindle._drain_blockers() == []


@pytest.mark.parametrize("role", ("owner", "watchdog"))
def test_unicode_decimal_birth_token_never_becomes_abandoned(episode_store, role):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    current["owner_episode"][role]["birth_token"] = "\u0661\u0662\u0663"
    episode_store.write(
        record["id"],
        **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}},
    )

    assert spindle._drain_blockers() == []


def test_wait_until_idle_raises_when_active_work_becomes_abandoned():
    blocker = spindle.DrainBlocker("late-abandonment", ABANDONED_REASON)
    with (
        patch("spindle._spools_idle", return_value=False) as idle,
        patch("spindle._drain_blockers", side_effect=[[], [blocker]]),
        patch("spindle.time.sleep"),
    ):
        with pytest.raises(spindle.DrainBlockedError) as raised:
            spindle._wait_until_idle(poll_interval=0.01)

    assert raised.value.blockers == (blocker,)
    assert idle.call_count == 2


def test_mcp_reload_refuses_abandoned_custody_before_starting_a_waiter():
    restarted = threading.Event()
    blocker = spindle.DrainBlocker("stuck-spool", ABANDONED_REASON)
    with (
        patch("spindle.subprocess.run", _fake_systemctl(restarted)),
        patch("spindle._run_store_maintenance"),
        patch("spindle._drain_blockers", return_value=[blocker]),
        patch("spindle._wait_until_idle") as wait,
    ):
        output = asyncio.run(spindle.spindle_reload.fn())

    assert "stuck-spool" in output
    assert ABANDONED_REASON in output
    assert "force=True" in output
    assert spindle._reload_pending is False
    wait.assert_not_called()
    assert not restarted.is_set()


def test_repeat_mcp_reload_surfaces_abandonment_instead_of_repromising_restart():
    restarted = threading.Event()
    blocker = spindle.DrainBlocker("repeat-stuck", ABANDONED_REASON)
    spindle._reload_pending = True
    try:
        with (
            patch("spindle.subprocess.run", _fake_systemctl(restarted)),
            patch("spindle._run_store_maintenance"),
            patch("spindle._drain_blockers", return_value=[blocker]),
        ):
            output = asyncio.run(spindle.spindle_reload.fn())
    finally:
        spindle._reload_pending = False

    assert "repeat-stuck" in output
    assert ABANDONED_REASON in output
    assert "already pending" not in output.lower()
    assert not restarted.is_set()


def test_async_mcp_drain_logs_abandonment_and_clears_pending(capsys):
    restarted = threading.Event()
    waiter_finished = threading.Event()
    blocker = spindle.DrainBlocker("late-spool", ABANDONED_REASON)

    def fail_after_start():
        waiter_finished.set()
        raise spindle.DrainBlockedError([blocker])

    with (
        patch("spindle.subprocess.run", _fake_systemctl(restarted)),
        patch("spindle._run_store_maintenance"),
        patch("spindle._drain_blockers", return_value=[]),
        patch("spindle._count_running", return_value=1),
        patch("spindle._wait_until_idle", fail_after_start),
    ):
        output = asyncio.run(spindle.spindle_reload.fn())
        assert waiter_finished.wait(2)
        for _ in range(100):
            if not spindle._reload_pending:
                break
            threading.Event().wait(0.01)

    assert "Draining" in output
    assert spindle._reload_pending is False
    assert "late-spool" in capsys.readouterr().err
    assert not restarted.is_set()


def test_cli_reload_refuses_abandoned_custody(capsys):
    restarted = threading.Event()
    blocker = spindle.DrainBlocker("cli-stuck", ABANDONED_REASON)
    with (
        patch("sys.argv", ["spindle", "reload"]),
        patch("spindle.subprocess.run", _fake_systemctl(restarted)),
        patch("spindle._reload_warn_on_store_mismatch", return_value=False),
        patch("spindle._run_store_maintenance"),
        patch("spindle._drain_blockers", return_value=[blocker]),
        pytest.raises(SystemExit) as raised,
    ):
        main()

    assert raised.value.code == 1
    assert "cli-stuck" in capsys.readouterr().err
    assert not restarted.is_set()


def test_running_message_names_abandoned_custody(episode_store):
    record = _abandoned_record(episode_store)

    message = spindle._running_spool_message(record)

    assert record["id"] in message
    assert ABANDONED_REASON in message
    assert "manual recovery" in message.lower()


def test_running_message_revalidates_after_watchdog_publishes_cleanup(episode_store):
    record = _abandoned_record(episode_store)
    stale = deepcopy(record)
    cleanup = localize_identities(make_episode("cleanup_proven"))
    episode_store.bind_lock(record["id"], cleanup)
    episode_store.write(
        record["id"],
        status="running",
        episode=cleanup,
        prompt=record["prompt"],
        lifecycle=record["lifecycle"],
    )

    message = spindle._running_spool_message(stale)

    assert ABANDONED_REASON not in message
    assert "unrecoverable" not in message


def _assert_repair_refused_without_mutation(episode_store, spool_id: str, expected: str) -> None:
    before = episode_store.spool_path(spool_id).read_bytes()

    output = spindle._repair_abandoned_custody(spool_id, attest_dead=True, source="test")

    assert output.startswith("Error: Refusing to repair")
    assert expected in output
    assert episode_store.spool_path(spool_id).read_bytes() == before


@pytest.mark.parametrize("phase", ("lock_bound", "accepted"))
def test_repair_converges_abandoned_custody_to_indeterminate_terminal(episode_store, monkeypatch, phase):
    record = _abandoned_record(episode_store, phase)
    monkeypatch.setattr(
        spindle,
        "_repair_attester_identity",
        lambda source: {"kind": "local_user", "user": "operator", "uid": 1000, "source": source},
    )

    output = spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")
    repaired = episode_store.read(record["id"])

    assert output == f"Spool {record['id']} settled as indeterminate abandonment."
    assert repaired["status"] == "abandoned"
    assert repaired["error_kind"] == ABANDONED_REASON
    assert "cleanup proof" in repaired["error"]
    assert "abandoned" in repaired["result"]
    assert repaired["exit_code"] is None
    assert repaired["completed_at"]
    assert repaired["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert repaired["lifecycle"]["normalized_terminal_kind"] not in {"failed", "complete", "completed"}
    assert repaired["owner_episode"]["phase"] == "abandoned"
    assert repaired["owner_episode"]["revision"] == record["owner_episode"]["revision"] + 1
    assert repaired["owner_episode"]["abandonment"]["reason"] == ABANDONED_REASON
    assert spindle._count_running() == 0
    assert "INDETERMINATE ABANDONMENT" in spindle._unspool_sync(record["id"])


@pytest.mark.parametrize("harness", ("codex", "gemini", "kimi"))
def test_each_harness_unspool_returns_abandonment_cleanly(episode_store, harness):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    current["harness"] = harness
    spindle._write_spool(record["id"], current)
    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")

    assert "INDETERMINATE ABANDONMENT" in spindle._unspool_sync(record["id"])


def test_wait_treats_repaired_abandonment_as_terminal(episode_store):
    record = _abandoned_record(episode_store)
    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")

    yielded = spindle._spin_wait_sync(record["id"], mode="yield")
    gathered = spindle._spin_wait_sync(record["id"], mode="gather")

    assert '"terminal_kind": "indeterminate"' in yielded
    assert "Indeterminate abandonment" in gathered


def test_repair_persists_attester_timestamp_and_exact_evidence(episode_store, monkeypatch):
    record = _abandoned_record(episode_store)
    attester = {"kind": "local_user", "user": "operator", "uid": 1000, "source": "mcp"}
    monkeypatch.setattr(spindle, "_repair_attester_identity", lambda source: {**attester, "source": source})

    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="mcp")
    repaired = episode_store.read(record["id"])
    abandonment = repaired["owner_episode"]["abandonment"]
    evidence = abandonment["evidence"]

    assert abandonment["attester"] == attester
    assert abandonment["attested_at"] == repaired["completed_at"]
    assert evidence["episode_phase"] == "accepted"
    assert evidence["lock"] == {
        "state": "released",
        "device": record["owner_episode"]["lock"]["device"],
        "inode": record["owner_episode"]["lock"]["inode"],
        "detail": None,
    }
    for role in ("owner", "watchdog"):
        assert evidence[role]["pid"] == record["owner_episode"][role]["pid"]
        assert evidence[role]["birth_token"] == record["owner_episode"][role]["birth_token"]
        assert evidence[role]["liveness"]["state"] == "dead"
        assert evidence[role]["liveness"]["reason"] in {"pidfd_esrch", "pidfd_exited"}
    assert repaired["terminal_provenance"]["abandonment"] == abandonment


def test_repair_requires_explicit_attestation_for_internal_and_mcp_paths(episode_store):
    record = _abandoned_record(episode_store)
    before = episode_store.spool_path(record["id"]).read_bytes()

    internal = spindle._repair_abandoned_custody(record["id"], attest_dead=False, source="test")
    mcp = asyncio.run(spindle.spindle_repair.fn(record["id"], attest_dead=False))

    assert ATTESTATION in internal
    assert ATTESTATION in mcp
    assert episode_store.spool_path(record["id"]).read_bytes() == before


def test_cli_repair_requires_attestation_flag(episode_store, capsys):
    record = _abandoned_record(episode_store)
    before = episode_store.spool_path(record["id"]).read_bytes()

    with patch("sys.argv", ["spindle", "repair", record["id"]]), pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert ATTESTATION in capsys.readouterr().err
    assert episode_store.spool_path(record["id"]).read_bytes() == before


def test_cli_and_mcp_repair_entrypoints_settle_records(episode_store, monkeypatch, capsys):
    cli_record = _abandoned_record(episode_store, "accepted")
    monkeypatch.setattr(
        spindle,
        "_repair_attester_identity",
        lambda source: {"kind": "local_user", "user": "operator", "uid": 1000, "source": source},
    )

    with (
        patch("sys.argv", ["spindle", "repair", cli_record["id"], "--attest-dead"]),
        pytest.raises(SystemExit) as raised,
    ):
        main()
    assert raised.value.code == 0
    assert "settled as indeterminate abandonment" in capsys.readouterr().out

    mcp_record = _abandoned_record(episode_store, "lock_bound")
    output = asyncio.run(spindle.spindle_repair.fn(mcp_record["id"], attest_dead=True))
    assert output == f"Spool {mcp_record['id']} settled as indeterminate abandonment."
    assert episode_store.read(mcp_record["id"])["status"] == "abandoned"


@pytest.mark.parametrize("phase", ("reserved", "aborted", "cleanup_proven", "released"))
def test_repair_refuses_wrong_episode_phase_without_mutation(episode_store, phase):
    spool_id = f"wrong-phase-{phase}"
    episode = localize_identities(make_episode(phase))
    if phase not in {"reserved", "aborted"}:
        episode_store.bind_lock(spool_id, episode)
    episode_store.write(spool_id, status="running", episode=episode)

    _assert_repair_refused_without_mutation(episode_store, spool_id, f"phase is {phase}")


def test_repair_refuses_held_ownership_lock_without_mutation(episode_store):
    record = _abandoned_record(episode_store)
    episode_store.hold_lock(record["id"])

    _assert_repair_refused_without_mutation(episode_store, record["id"], "ownership lock is held")


@pytest.mark.parametrize(
    ("owner_state", "watchdog_state"),
    (
        ("dead", "unverifiable"),
        ("unverifiable", "dead"),
        ("unverifiable", "unverifiable"),
        ("alive", "dead"),
        ("dead", "alive"),
    ),
)
def test_repair_refuses_non_dead_custody_liveness_without_mutation(
    episode_store, monkeypatch, owner_state, watchdog_state
):
    record = _abandoned_record(episode_store)
    episode = record["owner_episode"]
    states = {
        episode["owner"]["pid"]: spindle.LivenessEvidence(owner_state, f"owner_{owner_state}"),
        episode["watchdog"]["pid"]: spindle.LivenessEvidence(watchdog_state, f"watchdog_{watchdog_state}"),
    }
    monkeypatch.setattr(spindle, "assess_process_liveness", lambda identity: states[identity.pid])

    expected = "unverifiable" if "unverifiable" in {owner_state, watchdog_state} else "alive"
    _assert_repair_refused_without_mutation(episode_store, record["id"], expected)


@pytest.mark.parametrize(
    ("role", "reason"),
    (
        ("owner", "identity_mismatch"),
        ("watchdog", "identity_mismatch"),
        ("owner", "namespace_mismatch"),
        ("watchdog", "namespace_mismatch"),
    ),
)
def test_repair_refuses_pid_reuse_and_foreign_namespace_without_mutation(episode_store, monkeypatch, role, reason):
    record = _abandoned_record(episode_store)
    episode = record["owner_episode"]
    refused_pid = episode[role]["pid"]

    def liveness(identity):
        if identity.pid == refused_pid:
            return spindle.LivenessEvidence("unverifiable", reason)
        return spindle.LivenessEvidence("dead", "pidfd_esrch")

    monkeypatch.setattr(spindle, "assess_process_liveness", liveness)

    _assert_repair_refused_without_mutation(episode_store, record["id"], reason)


def test_repair_refuses_missing_and_unrelated_terminal_records_without_mutation(episode_store):
    missing = spindle._repair_abandoned_custody("not-present", attest_dead=True, source="test")
    assert missing == "Error: Refusing to repair spool 'not-present': record is missing."

    terminal = episode_store.write("terminal-history", status="complete", result="done", completed_at="then")
    before = episode_store.spool_path(terminal["id"]).read_bytes()
    output = spindle._repair_abandoned_custody(terminal["id"], attest_dead=True, source="test")
    assert "already terminal with status complete" in output
    assert episode_store.spool_path(terminal["id"]).read_bytes() == before


@pytest.mark.parametrize(
    ("embedded_id", "expected"),
    (
        (None, "record id is missing"),
        (17, "record id is malformed"),
        ("foreign-embedded-id", "does not match requested spool id"),
    ),
)
def test_repair_refuses_missing_malformed_and_foreign_embedded_ids_without_mutation(
    episode_store, embedded_id, expected
):
    record = _abandoned_record(episode_store)
    spool_id = record["id"]
    current = episode_store.read(spool_id)
    if embedded_id is None:
        current.pop("id")
    else:
        current["id"] = embedded_id
        if isinstance(embedded_id, str):
            episode_store.bind_lock(embedded_id, current["owner_episode"])
    spindle._write_spool(spool_id, current)

    _assert_repair_refused_without_mutation(episode_store, spool_id, expected)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ({"cleanup": CLEANUP}, "owner episode carries cleanup proof"),
        ({"release": {"proved_by": "late-reconciler"}}, "owner episode carries a release fact"),
        ({"abandonment": {"reason": "premature"}}, "owner episode carries an abandonment fact"),
        ({"phase_time": None}, "current phase timestamp is malformed"),
        ({"phase_time": ""}, "current phase timestamp is malformed"),
    ),
    ids=("cleanup", "release", "abandonment", "phase_time_null", "phase_time_empty"),
)
def test_repair_refuses_contradictory_or_malformed_episode_shape_without_mutation(episode_store, mutation, expected):
    record = _abandoned_record(episode_store)
    current = episode_store.read(record["id"])
    episode = current["owner_episode"]
    if "phase_time" in mutation:
        episode["phase_times"][episode["phase"]] = mutation["phase_time"]
    else:
        episode.update(mutation)
    spindle._write_spool(record["id"], current)

    _assert_repair_refused_without_mutation(episode_store, record["id"], expected)


def test_repair_is_idempotent_after_first_settlement(episode_store):
    record = _abandoned_record(episode_store)
    first = spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")
    after_first = episode_store.spool_path(record["id"]).read_bytes()

    second = spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")

    assert "settled as indeterminate abandonment" in first
    assert second == f"Spool {record['id']} was already settled as indeterminate abandonment."
    assert episode_store.spool_path(record["id"]).read_bytes() == after_first


def test_repaired_terminal_survives_recovery_and_reload_maintenance(episode_store):
    record = _abandoned_record(episode_store)
    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")
    repaired = episode_store.spool_path(record["id"]).read_bytes()

    spindle._recovery_pass()
    spindle._run_store_maintenance()

    assert episode_store.spool_path(record["id"]).read_bytes() == repaired
    assert episode_store.read(record["id"])["status"] == "abandoned"
    assert spindle._count_running() == 0


def test_repaired_terminal_is_preserved_from_destructive_actions_and_retention(episode_store):
    record = _abandoned_record(episode_store)
    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")
    repaired = episode_store.spool_path(record["id"]).read_bytes()

    assert spindle._spool_blocks_destructive_action(episode_store.read(record["id"])) is True

    spindle._cleanup_old_spools()

    assert episode_store.spool_path(record["id"]).read_bytes() == repaired


def test_malformed_abandonment_fact_is_unhealthy_instead_of_crashing_consumers(episode_store):
    record = _abandoned_record(episode_store)
    spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test")
    current = episode_store.read(record["id"])
    current["owner_episode"]["abandonment"] = "corrupted-to-a-string"
    spindle._write_spool(record["id"], current)

    reconciliation = spindle._reconcile_spool_ownership(current)
    failures = spindle._store_health_failures()
    storage = spindle._doctor_storage_check()

    assert reconciliation.state == "store_unhealthy"
    assert reconciliation.reason == "malformed_abandonment_fact"
    assert [failure["spool_id"] for failure in failures] == [record["id"]]
    assert storage["status"] == "fail"


def test_doctor_warns_about_abandoned_custody_without_marking_store_unhealthy(episode_store):
    record = _abandoned_record(episode_store)

    check = spindle._doctor_abandoned_custody_check()

    assert check["status"] == "warn"
    assert check["data"]["spool_ids"] == [record["id"]]
    assert check["lines"] == [f"{record['id']} — run: spindle repair {record['id']} --attest-dead"]
    assert spindle._store_health_failures() == []


def test_doctor_warning_keeps_report_healthy_and_phantom_does_not_block_launch(episode_store, monkeypatch):
    record = _abandoned_record(episode_store)

    def ok(name):
        return spindle._doctor_result(name, "ok", "ok")

    monkeypatch.setattr(spindle, "_doctor_cli_check", lambda: ok("cli"))
    monkeypatch.setattr(spindle, "_doctor_service_check", lambda *args, **kwargs: ok("service"))
    monkeypatch.setattr(spindle, "_doctor_storage_check", lambda: ok("storage"))
    monkeypatch.setattr(spindle, "_doctor_harness_check", lambda **kwargs: ok("harnesses"))
    monkeypatch.setattr(spindle, "_doctor_shard_check", lambda: ok("shards"))

    report = spindle._doctor_run()

    assert report["ok"] is True
    assert report["failed"] == []
    advisory = next(check for check in report["checks"] if check["name"] == "abandoned-custody")
    assert advisory["status"] == "warn"
    assert record["id"] in spindle._doctor_render(report)

    monkeypatch.setattr(spindle, "_ensure_store_supervisor_locked", lambda: (True, None))
    monkeypatch.setattr(spindle, "MAX_CONCURRENT", 2)
    reserved, error = spindle._try_reserve_slot_and_create("fresh-after-phantom")
    assert (reserved, error) == (True, None)


def test_repair_gate_and_terminal_write_share_episode_writer_lock(episode_store, monkeypatch):
    from spindle import namespace_owner

    record = _abandoned_record(episode_store)
    gate_observed = threading.Event()
    writer_attempted = threading.Event()
    real_gate = spindle._abandoned_custody_reason
    real_episode_guard = namespace_owner._episode_record_guard

    def pause_after_gate(current):
        reason = real_gate(current)
        if reason == ABANDONED_REASON:
            gate_observed.set()
            assert writer_attempted.wait(2)
        return reason

    @contextmanager
    def observed_episode_guard(root, spool_id):
        writer_attempted.set()
        with real_episode_guard(root, spool_id):
            yield

    monkeypatch.setattr(spindle, "_abandoned_custody_reason", pause_after_gate)
    monkeypatch.setattr(namespace_owner, "_episode_record_guard", observed_episode_guard)
    repair_result = []
    writer_result = []

    def repair():
        repair_result.append(spindle._repair_abandoned_custody(record["id"], attest_dead=True, source="test"))

    def publish_cleanup():
        assert gate_observed.wait(2)
        episode = record["owner_episode"]
        writer_result.append(
            spindle.transition_owner_episode(
                spindle.SPINDLE_DIR,
                record["id"],
                actor="watchdog",
                destination="cleanup_proven",
                generation=episode["generation"],
                expected_revision=episode["revision"],
                facts={"containment": CONTAINMENT, "cleanup": CLEANUP},
            )
        )

    repair_thread = threading.Thread(target=repair)
    writer_thread = threading.Thread(target=publish_cleanup)
    repair_thread.start()
    writer_thread.start()
    repair_thread.join(3)
    writer_thread.join(3)

    assert not repair_thread.is_alive()
    assert not writer_thread.is_alive()
    assert "settled as indeterminate abandonment" in repair_result[0]
    assert writer_result[0].accepted is False
    assert writer_result[0].rejection in {"illegal_transition", "stale_revision"}
    assert episode_store.read(record["id"])["owner_episode"]["phase"] == "abandoned"
