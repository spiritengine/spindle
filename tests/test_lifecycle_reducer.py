"""Contract tests for the pure lifecycle reducer.

The reducer is a pure function reduce(state, event) -> state. These tests define
its contract: monotonic protocol progression, terminal set-once, epoch fencing,
cancellation-vs-terminal evidence in both orderings, fail-closed on unknown
terminals, activity coalescing, privacy caps, and replay convergence.
"""

import copy

import pytest

from spindle import lifecycle as lc
from tests.lifecycle_fixtures import ev

SPOOL = "spool-x"


def fold(events, state=None):
    for event in events:
        state = lc.reduce(state, event)
    return state


# --- initial state and basic progression ------------------------------------


def test_initial_state_shape():
    state = lc.initial_state()
    assert state["sequence"] == 0
    assert state["connection_state"] == lc.CONNECTION_STARTING
    assert state["protocol_state"] == lc.PROTOCOL_STARTING
    assert state["protocol_terminal_kind"] is None
    assert state["connection_epoch"] == 0
    assert state["evidence"] == []


def test_happy_path_progression():
    state = fold(
        [
            ev(SPOOL, lc.TRANSPORT_STARTED),
            ev(SPOOL, lc.PROTOCOL_CONNECTED),
            ev(SPOOL, lc.CONVERSATION_ACCEPTED),
            ev(SPOOL, lc.TURN_STARTED),
        ]
    )
    assert state["connection_state"] == lc.CONNECTION_CONNECTED
    assert state["protocol_state"] == lc.PROTOCOL_ACTIVE
    assert state["sequence"] == 4
    assert state["last_event_type"] == lc.TURN_STARTED


def test_approval_waiting_then_resolve_returns_to_active():
    state = fold(
        [
            ev(SPOOL, lc.CONVERSATION_ACCEPTED),
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.APPROVAL_REQUESTED),
        ]
    )
    assert state["protocol_state"] == lc.PROTOCOL_WAITING
    state = lc.reduce(state, ev(SPOOL, lc.APPROVAL_RESOLVED))
    assert state["protocol_state"] == lc.PROTOCOL_ACTIVE


def test_conversation_summary_is_additive_provider_metadata():
    state = lc.reduce(
        None,
        ev(
            SPOOL,
            lc.CONVERSATION_ACCEPTED,
            summary="version=2.1.241; capabilities=interrupt_receipt_v1,msg_lifecycle_v1",
        ),
    )
    assert state["conversation_summary"] == ("version=2.1.241; capabilities=interrupt_receipt_v1,msg_lifecycle_v1")
    assert state["protocol_state"] == lc.PROTOCOL_ACCEPTED


def test_sequence_is_monotonic_even_for_ignored_events():
    state = fold([ev(SPOOL, lc.ACTIVITY), ev(SPOOL, "wat.unknown"), ev(SPOOL, lc.TRANSPORT_STARTED)])
    assert state["sequence"] == 3


# --- terminal precedence ----------------------------------------------------


def test_terminal_set_once_and_absorbing():
    state = fold(
        [
            ev(SPOOL, lc.CONVERSATION_ACCEPTED),
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", raw_status="ok"),
        ]
    )
    assert state["protocol_state"] == lc.PROTOCOL_TERMINAL
    assert state["protocol_terminal_kind"] == "completed"
    assert state["terminal_sequence"] == 3

    # A later provider terminal is evidence only; the terminal never changes.
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed"))
    assert state["protocol_terminal_kind"] == "completed"
    assert any(e["reason"] == "terminal_after_terminal" for e in state["evidence"])

    # Post-terminal work events do not move protocol_state.
    state = lc.reduce(state, ev(SPOOL, lc.WORK_STARTED, summary="late"))
    assert state["protocol_state"] == lc.PROTOCOL_TERMINAL
    assert state["active_work"] is None


def test_terminal_summary_is_first_terminal_set_once():
    first = "terminal_reason=completed; total_cost_usd=0.01"
    state = lc.reduce(
        None,
        ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", summary=first),
    )
    assert state["terminal_summary"] == first

    state = lc.reduce(
        state,
        ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed", summary="terminal_reason=api_error"),
    )
    assert state["protocol_terminal_kind"] == "completed"
    assert state["terminal_summary"] == first


def test_unknown_terminal_kind_fails_closed_indeterminate():
    state = lc.reduce(None, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="ka-boom"))
    assert state["protocol_terminal_kind"] == lc.TERMINAL_INDETERMINATE
    assert state["protocol_state"] == lc.PROTOCOL_TERMINAL


def test_missing_terminal_kind_fails_closed_indeterminate():
    state = lc.reduce(None, ev(SPOOL, lc.TURN_TERMINAL))
    assert state["protocol_terminal_kind"] == lc.TERMINAL_INDETERMINATE


# --- unknown kinds ----------------------------------------------------------


def test_unknown_event_kind_is_evidence_only():
    state = lc.reduce(None, ev(SPOOL, "provider.mystery", provider_event="weird"))
    assert state["evidence"], "unknown kind should be retained as evidence"
    assert state["evidence"][-1]["reason"] == "unknown_kind"
    # It changed no authoritative field beyond the sequence counter.
    assert state["protocol_state"] == lc.PROTOCOL_STARTING
    assert state["last_event_type"] is None


# --- epoch fencing ----------------------------------------------------------


def test_older_epoch_event_is_evidence_only():
    state = fold(
        [
            ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=1),
            ev(SPOOL, lc.CONVERSATION_ACCEPTED, epoch=1),
        ]
    )
    assert state["connection_epoch"] == 1
    assert state["protocol_state"] == lc.PROTOCOL_ACCEPTED
    before = copy.deepcopy(state)
    state = lc.reduce(state, ev(SPOOL, lc.TURN_STARTED, epoch=0))
    assert state["protocol_state"] == lc.PROTOCOL_ACCEPTED  # unchanged
    assert state["connection_epoch"] == 1
    assert state["evidence"][-1]["reason"] == "stale_epoch"
    # Only sequence and evidence moved.
    assert state["sequence"] == before["sequence"] + 1


def test_older_epoch_terminal_never_wins():
    state = fold(
        [
            ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=2),
            ev(SPOOL, lc.CONVERSATION_ACCEPTED, epoch=2),
        ]
    )
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", epoch=1))
    assert state["protocol_terminal_kind"] is None
    assert state["protocol_state"] == lc.PROTOCOL_ACCEPTED
    assert state["evidence"][-1]["reason"] == "stale_epoch"


def test_newer_epoch_resets_protocol_state():
    # Stuck waiting on an approval, then the transport reconnects (new epoch).
    state = fold(
        [
            ev(SPOOL, lc.CONVERSATION_ACCEPTED),
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.APPROVAL_REQUESTED),
        ]
    )
    assert state["protocol_state"] == lc.PROTOCOL_WAITING
    state = lc.reduce(state, ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=1))
    assert state["connection_epoch"] == 1
    assert state["protocol_state"] == lc.PROTOCOL_STARTING
    assert state["connection_state"] == lc.CONNECTION_CONNECTED
    # The re-established handshake can progress again.
    state = fold([ev(SPOOL, lc.CONVERSATION_ACCEPTED, epoch=1), ev(SPOOL, lc.TURN_STARTED, epoch=1)], state)
    assert state["protocol_state"] == lc.PROTOCOL_ACTIVE


def test_newer_epoch_after_terminal_does_not_unterminalize():
    state = fold(
        [
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed"),
        ]
    )
    state = lc.reduce(state, ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=1))
    assert state["protocol_state"] == lc.PROTOCOL_TERMINAL
    assert state["protocol_terminal_kind"] == "completed"
    # Epoch counter still advances (evidence of the reconnect), but state is frozen.
    assert state["connection_epoch"] == 1


# --- cancellation vs terminal, both orderings -------------------------------


def test_cancel_then_terminal_records_both():
    state = fold(
        [
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.CANCEL_ACKNOWLEDGED, observed_at="t-ack"),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="interrupted", observed_at="t-term"),
        ]
    )
    assert state["protocol_terminal_kind"] == "interrupted"
    assert state["cancel_evidence"]["acknowledged_at"] == "t-ack"
    assert state["cancel_evidence"]["terminal_observed_at"] == "t-term"


def test_terminal_then_late_cancel_ack():
    state = fold(
        [
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", observed_at="t-term"),
            ev(SPOOL, lc.CANCEL_ACKNOWLEDGED, observed_at="t-ack"),
        ]
    )
    # Provider terminal won; the late cancel ack is recorded as provenance only.
    assert state["protocol_terminal_kind"] == "completed"
    assert state["cancel_evidence"]["acknowledged_at"] == "t-ack"
    assert state["cancel_evidence"]["terminal_observed_at"] is None


# --- activity coalescing in the reducer -------------------------------------


def test_activity_folds_without_growing_evidence():
    state = None
    for i in range(1000):
        state = lc.reduce(state, ev(SPOOL, lc.ACTIVITY, summary=f"tok-{i}"))
    assert state["activity_count"] == 1000
    assert state["active_work"] == "tok-999"
    assert state["evidence"] == []
    assert state["protocol_state"] == lc.PROTOCOL_STARTING


def test_authoritative_transitions_preserved_between_activity():
    events = [ev(SPOOL, lc.CONVERSATION_ACCEPTED)]
    events += [ev(SPOOL, lc.ACTIVITY)] * 5
    events += [ev(SPOOL, lc.TURN_STARTED)]
    events += [ev(SPOOL, lc.ACTIVITY)] * 5
    events += [ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed")]
    state = fold(events)
    assert state["protocol_terminal_kind"] == "completed"
    assert state["activity_count"] == 10


# --- purity and replay convergence ------------------------------------------


def test_reduce_does_not_mutate_input():
    state = lc.reduce(None, ev(SPOOL, lc.TURN_STARTED))
    snapshot = copy.deepcopy(state)
    _ = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed"))
    assert state == snapshot, "reduce must not mutate its input state"


def test_replay_converges_on_authoritative_state():
    # At-least-once: replaying the terminal twice keeps the authoritative outcome,
    # even though non-authoritative counters advance.
    state = fold([ev(SPOOL, lc.TURN_STARTED), ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed")])
    once = state["protocol_terminal_kind"]
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed"))
    assert state["protocol_terminal_kind"] == once == "failed"


# --- privacy and boundedness ------------------------------------------------


def test_summary_is_length_capped():
    big = "x" * (lc.SUMMARY_MAX + 500)
    event = ev(SPOOL, lc.ACTIVITY, summary=big)
    assert len(event.summary) == lc.SUMMARY_MAX


def test_provider_ids_allowlisted_and_capped():
    event = ev(
        SPOOL,
        lc.CONVERSATION_ACCEPTED,
        provider_ids={"thread_id": "t1", "secret_prompt": "leak", "turn_id": "y" * 999},
    )
    assert event.provider_ids == {"thread_id": "t1", "turn_id": "y" * lc.PROVIDER_ID_VALUE_MAX}
    assert "secret_prompt" not in event.provider_ids


def test_error_is_shaped_and_capped():
    event = ev(
        SPOOL,
        lc.TURN_TERMINAL,
        terminal_kind="failed",
        error={"type": "Boom", "message": "z" * 999, "raw_stack": "SENSITIVE"},
    )
    assert set(event.error) <= {"type", "code", "message"}
    assert "raw_stack" not in event.error
    assert len(event.error["message"]) == lc.ERROR_VALUE_MAX


def test_unknown_envelope_field_rejected():
    with pytest.raises(TypeError):
        lc.LifecycleEvent(spool_id=SPOOL, kind=lc.ACTIVITY, prompt="do not accept this")


def test_spool_id_and_epoch_validation():
    with pytest.raises(ValueError):
        lc.LifecycleEvent(spool_id="", kind=lc.ACTIVITY)
    with pytest.raises(ValueError):
        lc.LifecycleEvent(spool_id=SPOOL, kind=lc.ACTIVITY, epoch=-1)
    with pytest.raises(ValueError):
        lc.LifecycleEvent(spool_id=SPOOL, kind="")


def test_evidence_ring_is_bounded():
    state = lc.reduce(None, ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=5))
    for i in range(lc.EVIDENCE_MAX_COUNT * 3):
        state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", epoch=1, summary=f"s{i}"))
    assert len(state["evidence"]) <= lc.EVIDENCE_MAX_COUNT
    assert len(__import__("json").dumps(state["evidence"])) <= lc.EVIDENCE_MAX_BYTES


# --- round-1 Fell hardening --------------------------------------------------


@pytest.mark.parametrize("bad", ["../victim", "a/b", "..", ".", "a\\b", ""])
def test_unsafe_spool_id_rejected(bad):
    with pytest.raises(ValueError):
        lc.LifecycleEvent(spool_id=bad, kind=lc.ACTIVITY)


def test_envelope_nested_mappings_are_read_only():
    event = ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed", provider_ids={"thread_id": "t"}, error={"type": "E"})
    with pytest.raises(TypeError):
        event.provider_ids["secret_prompt"] = "leak"
    with pytest.raises(TypeError):
        event.error["raw_stack"] = "leak"


def test_provider_error_is_defensively_copied_not_aliased():
    event = ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed", error={"type": "Boom", "message": "orig"})
    state = lc.reduce(None, event)
    assert state["provider_error"] == {"type": "Boom", "message": "orig"}
    # A plain dict distinct from the event's read-only mapping.
    assert isinstance(state["provider_error"], dict)
    assert state["provider_error"] is not event.error


def test_unknown_kind_does_not_merge_provider_ids():
    state = lc.reduce(None, ev(SPOOL, lc.CONVERSATION_ACCEPTED, provider_ids={"thread_id": "real"}))
    assert state["provider_ids"] == {"thread_id": "real"}
    state = lc.reduce(state, ev(SPOOL, "provider.mystery", provider_ids={"thread_id": "wrong"}))
    assert state["provider_ids"] == {"thread_id": "real"}, "unknown kind must not overwrite authoritative ids"
    assert state["evidence"][-1]["reason"] == "unknown_kind"


def test_stale_epoch_does_not_merge_provider_ids():
    state = lc.reduce(None, ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=2))
    state = lc.reduce(state, ev(SPOOL, lc.CONVERSATION_ACCEPTED, epoch=2, provider_ids={"thread_id": "real"}))
    state = lc.reduce(state, ev(SPOOL, lc.CONVERSATION_ACCEPTED, epoch=1, provider_ids={"thread_id": "wrong"}))
    assert state["provider_ids"] == {"thread_id": "real"}


def test_terminal_clears_active_work_and_late_activity_cannot_revive_it():
    state = fold(
        [
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.WORK_STARTED, summary="live"),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed"),
        ]
    )
    assert state["active_work"] is None
    state = lc.reduce(state, ev(SPOOL, lc.ACTIVITY, summary="late"))
    assert state["active_work"] is None, "post-terminal activity must not revive active_work"


def test_error_nested_value_is_dropped_not_stringified():
    event = ev(
        SPOOL, lc.TURN_TERMINAL, terminal_kind="failed", error={"type": "E", "message": {"tool_input": "SECRET"}}
    )
    assert event.error == {"type": "E"}
    assert "SECRET" not in __import__("json").dumps(dict(event.error))
    # An error whose only field is a nested container collapses to None.
    only_nested = ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="failed", error={"message": {"tool_input": "SECRET"}})
    assert only_nested.error is None


def test_caps_are_byte_bounded_for_multibyte_input():
    summary = "🔥" * lc.SUMMARY_MAX  # 4 bytes each, far exceeds the byte cap
    event = ev(SPOOL, lc.ACTIVITY, summary=summary)
    assert len(event.summary.encode("utf-8")) <= lc.SUMMARY_MAX
    # And the truncation leaves valid UTF-8 (no partial code point).
    event.summary.encode("utf-8").decode("utf-8")


# --- round-2 Fell hardening --------------------------------------------------


def test_unknown_kind_does_not_advance_epoch_fence():
    state = lc.reduce(None, ev(SPOOL, lc.PROTOCOL_CONNECTED, epoch=1))
    assert state["connection_epoch"] == 1
    # A bogus unknown event at a future epoch must not move the fence.
    state = lc.reduce(state, ev(SPOOL, "provider.mystery", epoch=99))
    assert state["connection_epoch"] == 1, "unknown kind must not advance the epoch fence"
    assert state["evidence"][-1]["reason"] == "unknown_kind"
    # So a legitimate terminal at the real epoch is still accepted, not staled out.
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", epoch=1))
    assert state["protocol_terminal_kind"] == "completed"


def test_coalescer_does_not_reemit_stale_summary():
    coalescer = lc.ActivityCoalescer(SPOOL, threshold=2)
    coalescer.observe("work-A")
    emitted = coalescer.observe("work-A")
    assert emitted is not None and emitted.summary == "work-A"
    # A later summary-less batch must not carry the retained summary.
    coalescer.observe()
    followup = coalescer.flush()
    assert followup is not None and followup.summary is None


def test_finished_work_not_revived_by_stale_coalesced_summary():
    coalescer = lc.ActivityCoalescer(SPOOL, threshold=2)
    state = fold([ev(SPOOL, lc.TURN_STARTED), ev(SPOOL, lc.WORK_STARTED, summary="A")])
    assert state["active_work"] == "A"
    coalescer.observe("A")
    state = lc.reduce(state, coalescer.observe("A"))  # emits summary "A"
    state = lc.reduce(state, ev(SPOOL, lc.WORK_FINISHED))
    assert state["active_work"] is None
    coalescer.observe()  # new, summary-less batch
    state = lc.reduce(state, coalescer.flush())
    assert state["active_work"] is None, "a stale coalesced summary must not revive finished work"


def test_cancel_evidence_robust_to_missing_timestamp():
    # observed_at is optional; a timestamp-less cancel.acknowledged must still
    # record the acknowledgement (presence, not the timestamp value) and be
    # first-write-wins, and a following terminal must still record terminal_observed.
    state = fold([ev(SPOOL, lc.TURN_STARTED), ev(SPOOL, lc.CANCEL_ACKNOWLEDGED)])
    assert state["cancel_evidence"]["acknowledged"] is True
    assert state["cancel_evidence"]["acknowledged_at"] is None
    # A later timestamped ack does not overwrite the first.
    state = lc.reduce(state, ev(SPOOL, lc.CANCEL_ACKNOWLEDGED, observed_at="later"))
    assert state["cancel_evidence"]["acknowledged_at"] is None
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="interrupted", observed_at="t"))
    assert state["cancel_evidence"]["terminal_observed"] is True
    assert state["cancel_evidence"]["terminal_observed_at"] == "t"


def test_connection_state_tracks_transport_events_after_terminal():
    state = fold(
        [
            ev(SPOOL, lc.PROTOCOL_CONNECTED),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed"),
            ev(SPOOL, lc.TRANSPORT_EXITED),
        ]
    )
    assert state["connection_state"] == lc.CONNECTION_EXITED
    # connection_state is a live projection: a later transport reconnect still
    # updates it, while the protocol terminal stays frozen.
    state = fold([ev(SPOOL, lc.TRANSPORT_STARTED), ev(SPOOL, lc.PROTOCOL_CONNECTED)], state)
    assert state["connection_state"] == lc.CONNECTION_CONNECTED
    assert state["protocol_state"] == lc.PROTOCOL_TERMINAL


def test_terminal_replay_after_cancel_does_not_fabricate_evidence():
    # The terminal arrives BEFORE any cancel; a cancel ack follows; then the same
    # terminal is redelivered (at-least-once). terminal_observed_at must stay None
    # because the terminal genuinely preceded the cancel.
    state = fold(
        [
            ev(SPOOL, lc.TURN_STARTED),
            ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", observed_at="t0"),
            ev(SPOOL, lc.CANCEL_ACKNOWLEDGED, observed_at="t1"),
        ]
    )
    assert state["cancel_evidence"]["terminal_observed_at"] is None
    state = lc.reduce(state, ev(SPOOL, lc.TURN_TERMINAL, terminal_kind="completed", observed_at="t0"))
    assert state["cancel_evidence"]["terminal_observed_at"] is None, "replay must not fabricate cancel evidence"
    assert state["protocol_terminal_kind"] == "completed"
    assert state["evidence"][-1]["reason"] == "terminal_after_terminal"
