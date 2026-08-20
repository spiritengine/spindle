"""Regression contract for an owner episode abandoned without cleanup proof."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import spindle
from spindle import main
from tests.owner_convergence_fixtures import live_process_fact, localize_identities
from tests.owner_episode_fixtures import make_episode

ABANDONED_REASON = "custody_abandoned_without_cleanup_proof"


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
    episode_store.write(record["id"], **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}})

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
    episode_store.write(record["id"], **{key: value for key, value in current.items() if key not in {"id", "status", "created_at"}})

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
