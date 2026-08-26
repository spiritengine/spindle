"""Focused regressions for the confirmed convergence defects, by Fell round.

Every test here runs against the *current* production surface, so each one fails
on the code it was written against with a behavioural mismatch rather than an
import error.  They are deliberately separated from the wider contract so the
implementation owner can run one short module and read precise statements of
what is broken.

Defect map (finding-20260813-nah1, ownership trace in finding-20260813-6zyh
section 6):

1. durable owner_crash ignored without an owner-exit sidecar, and a late sidecar
   overriding a proven natural exit
2. a receiptless current-generation request when no winning request exists
3. pre-bind deadline / preacceptance failure writing terminals outside shared
   bookkeeping
4. terminal re-entry returning before retrying bookkeeping
5. a dead watchdog falling back to a live starter after identity publication
6. signal-terminated providers recorded as code-and-255 from the watchdog handle

Phase 3 round-1 Fell (finding-20260813-k6sw), in the second half of this module:

7. retention retiring a terminal owner-episode record whose convergence
   obligations are still pending, before startup recovery can discharge them
8. compatibility updates rewriting protected terminal fields on an owner-episode
   or already-settled record
9. an accepted receipt with no cleanup terminal counted as a discharged duty
10. legacy finalization publishing without the record lock that serializes it
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

import spindle
from spindle.namespace_owner import LivenessEvidence, mailbox_path, read_control_receipt
from tests.owner_convergence_fixtures import (
    ACKNOWLEDGED_AT,
    CHILD_EXIT_AT,
    GENERATION,
    NATURAL_STDOUT,
    NO_WINNER_RECEIPT,
    ORIGINS,
    TIMEOUT_SECONDS,
    WatchdogHandle,
    acknowledgement_fact,
    build_scenario,
    causal_episode,
    converge,
    converge_to_fixed_point,
    converge_without_escape,
    dead_process_fact,
    failure_fact,
    live_process_fact,
    localize_identities,
    obligation_progress_of,
    obligations_of,
    publish_request,
    request_fact,
    write_receipt,
)
from tests.owner_episode_fixtures import EPISODE_KEY

BY_EVIDENCE = {row.evidence: row for row in ORIGINS}


# --- defect 1: owner-crash evidence precedence ------------------------------


def test_durable_owner_crash_is_recognised_without_an_owner_exit_sidecar(episode_store):
    """The episode failure fact is authority; the sidecar is a mirror.

    finding-20260813-6zyh evidence precedence rule 2 outranks rule 8: a crash the
    watchdog durably published in the episode cannot be ignored merely because
    the ``.owner-exit`` mirror is missing (the watchdog can die between the two
    writes).  Today the crash branch is reachable only through the sidecar, so
    the record falls through to natural provider parsing.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["owner_crash_no_sidecar"])
    assert not (episode_store.root / f"{scenario.spool_id}.owner-exit").exists()

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record.get("error_kind") == "owner_transport_loss", (
        f"durable owner_crash was ignored: status={record.get('status')!r} "
        f"error={record.get('error')!r} error_kind={record.get('error_kind')!r}"
    )
    assert record["lifecycle"]["normalized_terminal_kind"] == "indeterminate"
    assert record["lifecycle"]["transport_state"] == "lost"
    assert record["status"] == "error"


def test_a_late_owner_crash_sidecar_cannot_rewrite_a_proven_natural_exit(episode_store):
    """Proven natural cleanup outranks a crash mirror written afterwards.

    The owner published ``cleanup.outcome == natural_exit`` with a provider exit
    code, then died before the watchdog reaped it.  The watchdog's crash sidecar
    is late evidence about the *owner*, never about the provider's outcome.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural_then_owner_crash"])
    sidecar = json.loads((episode_store.root / f"{scenario.spool_id}.owner-exit").read_text())
    assert sidecar["owner_crashed"] is True

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record["status"] == "complete", "a late crash mirror rewrote a proven natural success"
    assert record["result"] == scenario.proposal_result
    assert record["lifecycle"]["normalized_terminal_kind"] == "complete"
    assert record.get("error_kind") is None


# --- defect 2: the receiptless no-winner request ----------------------------


def test_a_current_generation_request_is_settled_when_no_request_ever_won(episode_store):
    """Patrick's decision in finding-20260813-xt37, made executable.

    A cancel admitted after the owner's final poll never becomes the winning
    request.  It still gets a durable ``rejected_terminal`` receipt, and the
    natural result stays the authoritative public outcome.  Today
    ``_settle_recovered_episode_requests`` returns early with no winner, so the
    request stays unsettled forever.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], extra_request="cancel")
    request_id = scenario.extras["extra_request_id"]
    assert read_control_receipt(episode_store.root, scenario.spool_id, request_id) is None

    converge(scenario.spool_id)

    receipt = read_control_receipt(episode_store.root, scenario.spool_id, request_id)
    assert receipt is not None, "a captured current-generation request was left without a receipt"
    assert receipt.cleanup_outcome == NO_WINNER_RECEIPT
    assert receipt.owner_acknowledged_at is None
    record = episode_store.read(scenario.spool_id)
    assert record["status"] == "complete"
    assert record["result"] == scenario.proposal_result


# --- defect 3: terminals published outside shared bookkeeping ---------------

BYPASSING_PRODUCERS = ("pre_bind_deadline", "preacceptance_failure")


def _seed_bypassed_terminal(store, monkeypatch, tmp_path, producer: str):
    """Reproduce the exact evidence-only state each repaired producer leaves.

    These are not hypothetical records: they are what
    ``LogicalOwner._settle_deadline_expiry_before_binding`` and
    ``owner_watchdog._record_preacceptance_failure`` now write: an aborted
    episode which convergence must project and finish exactly once.
    """
    spool_id = f"bypass-{producer}"
    worktree = tmp_path / f"{spool_id}-worktree"
    worktree.mkdir(parents=True)
    if producer == "pre_bind_deadline":
        episode = causal_episode(
            "aborted",
            failure=failure_fact(
                "deadline_expired_before_provider_start",
                "inherited deadline expired before logical owner binding",
            ),
        )
    elif producer == "preacceptance_failure":
        episode = causal_episode(
            "aborted",
            failure=failure_fact("owner_preacceptance_failure", "logical owner exited before binding"),
        )
    store.write(
        spool_id,
        status="pending",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        owner_generation=GENERATION,
        timeout=17,
        harness="claude-code",
        shard={"worktree_path": str(worktree), "branch_name": f"shard-{spool_id}"},
        shard_created_by_spool=True,
        working_dir=str(worktree),
    )
    handle = WatchdogHandle()
    spindle._PROC_HANDLES[spool_id] = handle
    return spool_id, handle


@pytest.mark.parametrize("producer", BYPASSING_PRODUCERS)
def test_a_terminal_published_outside_convergence_still_owes_its_duties(episode_store, monkeypatch, tmp_path, producer):
    """Every terminal origin discharges the same finite obligation set.

    This is the class the convergence checkpoint (brief-20260812-ewry) named and
    the targeted cycle failed to close: a terminal reached through a private
    writer is skipped by the supervisor, so its failed shard is never marked for
    recovery and its watchdog handle is never reaped.
    """
    spool_id, handle = _seed_bypassed_terminal(episode_store, monkeypatch, tmp_path, producer)

    converge(spool_id)

    record = episode_store.read(spool_id)
    assert record.get("shard_cleanup_preserved") is True, "the failed shard was never preserved for recovery"
    assert record["shard"].get("startup_failure_preserved") is True
    assert spool_id not in spindle._PROC_HANDLES, "the launcher's watchdog handle was retained"


@pytest.mark.parametrize("producer", BYPASSING_PRODUCERS)
def test_retention_never_deletes_a_record_whose_failed_shard_is_unresolved(
    episode_store, monkeypatch, tmp_path, producer
):
    """The recovery handle for a failed shard outlives retention.

    Retention keeps a record while ``shard_cleanup_preserved`` is set.  When the
    preservation obligation is skipped, retention deletes the spool JSON and the
    worktree survives with nothing pointing at it - the loss Claude reviewer
    441e1237 reproduced.
    """
    spool_id, _handle = _seed_bypassed_terminal(episode_store, monkeypatch, tmp_path, producer)

    converge(spool_id)
    spindle._cleanup_old_spools()

    assert episode_store.spool_path(spool_id).exists(), "retention deleted a record whose shard still needs a decision"


def test_the_watchdog_publishes_preacceptance_evidence_not_a_public_terminal(episode_store):
    """Anchor the bypass fixtures above in the real writer.

    Phase 1's closed-writer rule: for an owner-episode-v1 record, only the
    convergence applicator may persist a public terminal.  The watchdog is an
    evidence producer.  Driving the production function proves the hand-built
    bypass states are what this code really leaves behind, and states the rule
    the implementation must satisfy.
    """
    from spindle import owner_watchdog

    spool_id = "watchdog-preacceptance"
    episode = causal_episode("reserved")
    episode_store.write(spool_id, status="pending", episode=episode, created_at="2026-08-01T00:00:00")

    published = owner_watchdog._record_preacceptance_failure(
        episode_store.root,
        spool_id,
        owner_pid=4244,
        owner_birth_token="9003",
        owner_generation=GENERATION,
        status=0,
    )

    record = episode_store.read(spool_id)
    assert published is True
    assert record[EPISODE_KEY]["phase"] == "aborted"
    assert record[EPISODE_KEY]["failure"]["kind"] == "owner_preacceptance_failure"
    assert record["status"] == "pending", (
        "the watchdog published a public terminal; only the convergence applicator may do that "
        f"(status={record.get('status')!r}, error_kind={record.get('error_kind')!r})"
    )
    assert "completed_at" not in record


# --- defect 4: terminal re-entry skips outstanding bookkeeping --------------


def test_terminal_re_entry_finishes_the_duties_an_interrupted_run_left(episode_store, tmp_path, monkeypatch):
    """A terminal is not converged until its owed duties carry completion evidence.

    The record below is exactly what a crash between terminal publication and
    the bookkeeping call leaves: a durable public terminal, an unpreserved failed
    shard, a live watchdog handle and an unsettled current-generation request.
    Re-entry must finish all three; today it returns at the terminal check.
    """
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_cancel"],
        tmp_path=tmp_path,
        with_shard=True,
        extra_request="drop",
    )
    sibling = scenario.extras["extra_request_id"]

    def effect_unavailable(*args, **kwargs):
        raise OSError("effect carrier unavailable at crash cut")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(spindle, "write_control_receipt", effect_unavailable)
        interrupted.setattr(spindle, "_preserve_failed_spool_shard", effect_unavailable)
        interrupted.setattr(spindle, "_pop_and_reap_process_handle", effect_unavailable)
        converge(scenario.spool_id)

    published = episode_store.read(scenario.spool_id)
    completed_at = published["completed_at"]
    assert published["terminal_origin"] == "accepted_cancel"

    converge(scenario.spool_id)

    reentered = episode_store.read(scenario.spool_id)
    assert reentered.get("shard_cleanup_preserved") is True, "re-entry skipped failed-shard preservation"
    assert scenario.spool_id not in spindle._PROC_HANDLES, "re-entry skipped the local handle duty"
    assert read_control_receipt(episode_store.root, scenario.spool_id, sibling) is not None, (
        "re-entry skipped receipt settlement"
    )
    assert reentered["completed_at"] == completed_at, "re-entry rewrote an immutable completion time"


# --- defect 5: reserved-phase liveness after watchdog publication -----------


def test_a_dead_watchdog_cannot_be_revived_by_a_live_starter(episode_store):
    """Once watchdog identity is published, the starter is no longer a carrier.

    The starter is short-lived by design and its PID may still be alive (or
    reused) long after the watchdog it published has died.  Falling back to it
    keeps an abandoned pre-bind reservation active - and holding capacity -
    indefinitely.
    """
    spool_id = "reserved-dead-watchdog"
    episode = causal_episode(
        "reserved",
        starter=live_process_fact(),
        watchdog=dead_process_fact("watchdog"),
    )
    record = episode_store.write(spool_id, status="pending", episode=episode, created_at="2026-08-01T00:00:00")

    reconciliation = spindle._reconcile_spool_ownership(record)

    assert reconciliation.state == "terminalizable", (
        f"a reservation whose published watchdog is dead stayed {reconciliation.state} "
        f"({reconciliation.reason}) because its starter is still alive"
    )
    assert spindle._count_running() == 0, "an abandoned reservation kept holding capacity"


def test_a_live_watchdog_still_keeps_its_reservation_active(episode_store):
    """The sibling case: watchdog liveness, not starter liveness, is authority."""
    spool_id = "reserved-live-watchdog"
    episode = causal_episode(
        "reserved",
        starter=dead_process_fact("starter"),
        watchdog=live_process_fact(),
    )
    record = episode_store.write(spool_id, status="pending", episode=episode, created_at="2026-08-01T00:00:00")

    assert spindle._reconcile_spool_ownership(record).state == "active"


# --- defect 6: provider exit evidence ---------------------------------------


def test_a_signal_killed_provider_reports_128_plus_signal_not_the_wrapper_status(episode_store):
    """The watchdog's own wait status is never provider exit evidence.

    The owner normalises ``-9`` to ``137`` in every durable carrier, but the
    launcher-held Popen handle belongs to the *watchdog*, whose wrapper exits
    with ``-9 & 0xFF == 247``.  Preferring the handle publishes 247 as the
    provider's exit code.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural_signal"])
    assert scenario.handle.poll() == 247

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record["exit_code"] == 137, "a watchdog wrapper status was published as the provider exit code"


def test_a_live_watchdog_handle_never_supplies_a_provider_exit_code(episode_store):
    """A handle that has not exited must not silence the durable carrier."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)
    spindle._PROC_HANDLES[scenario.spool_id] = WatchdogHandle(None)

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record["exit_code"] == 0
    assert record[EPISODE_KEY]["cleanup"]["provider_exit_code"] == 0


def test_a_request_published_for_a_stale_generation_is_never_settled_as_current(episode_store):
    """Generation scoping is part of the same evidence rule defect 6 lives under."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])
    stale = publish_request(episode_store, scenario.spool_id, "cancel", GENERATION - 1)

    converge(scenario.spool_id)

    receipt = read_control_receipt(episode_store.root, scenario.spool_id, stale)
    assert receipt is None or receipt.cleanup_outcome == "rejected_stale_generation"


# --- defect 7: retention consumes convergence, not terminality ---------------


def _unwritable_transcript(monkeypatch) -> None:
    """Make the transcript carrier unwritable, the way a full disk would.

    A file where the transcript directory has to be is the same refusal a full
    or read-only filesystem gives, and it leaves the duty exactly where a real
    carrier failure leaves it: pending, with the intent still on the record.
    """
    blocked = spindle.SPINDLE_DIR / "blocked-transcripts"
    blocked.write_text("not a directory")
    monkeypatch.setattr(spindle, "_get_transcript_path", lambda spool_id: blocked / f"{spool_id}.transcript")


def test_retention_never_retires_a_record_whose_obligations_are_pending(episode_store, monkeypatch):
    """Retirement deletes the record the owed work is replayable from.

    ``retire_owner_artifacts`` removes the spool JSON, the mailbox, the captures
    and the transcript together.  A terminal whose obligation block still names
    pending work is precisely the record a restarted process has to read to
    finish that work, and the sweep runs on a fresh process before startup
    recovery has: retiring it there does not merely postpone the duty, it
    destroys the only durable statement of it.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)
    with monkeypatch.context() as broken:
        _unwritable_transcript(broken)
        converge(scenario.spool_id)
    interrupted = episode_store.read(scenario.spool_id)
    assert obligation_progress_of(interrupted)["natural_transcript"] == "pending", (
        "the transcript duty was discharged, so this run proves nothing about a pending one"
    )

    spindle._cleanup_old_spools()

    assert episode_store.spool_path(scenario.spool_id).exists(), (
        "retention retired a terminal record whose transcript duty was still owed"
    )
    kept = episode_store.read(scenario.spool_id)
    assert obligations_of(kept)["natural_transcript"]["intent"] == scenario.expected_transcript_intent(), (
        "the retained record no longer carries the intent its owed duty is replayed from"
    )
    assert spindle._get_output_path(scenario.spool_id).exists(), (
        "the stdout capture the owed transcript is replayed from was retired with the artifact set"
    )

    # The guard is a wait, not a leak. Startup's real recovery pass follows
    # cleanup in _run_store_maintenance, so it must notice a terminal record as
    # well as the pending/running records it monitors and discharge the duty.
    assert spindle._recovery_pass() == []
    recovered = episode_store.read(scenario.spool_id)
    assert obligation_progress_of(recovered)["natural_transcript"] == "complete"
    assert spindle._get_transcript_path(scenario.spool_id).read_text() == NATURAL_STDOUT

    # Once recovery has discharged the duty, the next sweep may retire the old
    # record normally.
    spindle._cleanup_old_spools()

    assert not episode_store.spool_path(scenario.spool_id).exists(), (
        "a fully converged record was never retired, so retention now leaks instead of waiting"
    )


# --- defect 8: compatibility updates on a record convergence owns ------------


def test_a_sandbox_refusal_adds_its_metadata_without_a_second_terminal(episode_store):
    """The refusal is already the record's outcome; only its detail is missing.

    ``_persist_codex_sandbox_refusal`` runs after the launcher published this
    same message as pre-spawn failure evidence, which convergence has already
    projected into a public terminal.  Writing the outcome again through the
    compatibility applicator moves a completion time that is immutable.
    """
    spool_id = "sandbox-refusal-settled"
    message = "REFUSED: codex sandbox is not enforcing on /usr/bin/codex"
    episode_store.write(
        spool_id,
        status="pending",
        episode=causal_episode("reserved"),
        created_at="2026-08-01T00:00:00",
        harness="codex",
        timeout=TIMEOUT_SECONDS,
    )
    spindle._record_pre_spawn_failure(spool_id, message)
    published = episode_store.read(spool_id)
    assert published["terminal_origin"] == "launcher_pre_spawn_failure", (
        f"the launcher failure was never projected into a terminal (status={published.get('status')!r})"
    )

    returned = spindle._persist_codex_sandbox_refusal(
        spool_id,
        message,
        sandbox="workspace-write",
        permission="careful",
        codex_bin="/usr/bin/codex",
        codex_version="1.0",
    )

    record = episode_store.read(spool_id)
    assert message in returned
    assert returned.endswith(f"(spool {spool_id})")
    assert "refusal persistence failed" not in returned
    assert record["sandbox_error"] == message, "the refusal detail unspool surfaces was not recorded"
    assert record["codex_version"] == "1.0"
    for name in ("status", "error", "error_kind", "completed_at", "terminal_origin", "terminal_provenance"):
        assert record.get(name) == published.get(name), f"the sandbox refusal republished {name} over a settled record"


PROTECTED_COMPATIBILITY_UPDATES = (
    {"status": "error"},
    {"status": "running"},
    {"completed_at": "2026-08-13T00:00:00"},
    {"error": "rewritten"},
    {"result": "rewritten"},
    {"exit_code": 7},
    {"terminal_origin": "natural_failure"},
    {"terminal_provenance": {"owner_generation": 99}},
    {"lifecycle": {"normalized_terminal_kind": "cancelled"}},
    {"lifecycle": {"ownership_state": "released"}},
    {"lifecycle": {"ownership_state": "active"}},
    {"lifecycle": {"transport_state": "running"}},
    {EPISODE_KEY: {"phase": "released"}},
)


@pytest.mark.parametrize(
    "updates",
    PROTECTED_COMPATIBILITY_UPDATES,
    ids=[",".join(sorted(update)) for update in PROTECTED_COMPATIBILITY_UPDATES],
)
def test_the_compatibility_applicator_refuses_a_protected_update_on_a_settled_record(episode_store, updates):
    """The rule the two callers above obey, stated where it is enforced.

    The closed-writer guard is about which module publishes a terminal.  A dict
    of arbitrary fields handed across that boundary satisfies it while letting
    any caller rewrite an outcome, so the boundary has to carry the rule.
    """
    from spindle.owner_episode_convergence import ProtectedRecordUpdate, publish_record_updates

    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)
    converge_to_fixed_point(scenario.spool_id)
    record = episode_store.read(scenario.spool_id)
    before = episode_store.spool_path(scenario.spool_id).read_bytes()

    with pytest.raises(ProtectedRecordUpdate):
        publish_record_updates(scenario.spool_id, record, updates)

    assert episode_store.spool_path(scenario.spool_id).read_bytes() == before, (
        "the refused update was written to the record anyway"
    )


def test_the_compatibility_applicator_still_publishes_an_unsettled_legacy_terminal(episode_store):
    """An over-broad refusal would silently delete the path's whole purpose."""
    from spindle.owner_episode_convergence import publish_record_updates

    record = episode_store.write("legacy-unsettled", status="pending", created_at="2026-08-01T00:00:00")

    publish_record_updates(
        "legacy-unsettled",
        record,
        {"status": "error", "error": "spawn failed", "completed_at": "2026-08-13T00:00:00"},
    )

    settled = episode_store.read("legacy-unsettled")
    assert (settled["status"], settled["error"]) == ("error", "spawn failed")


def test_the_compatibility_applicator_treats_a_legacy_terminal_without_completed_at_as_settled(episode_store):
    """A malformed old terminal is still an outcome, not an invitation to replace it."""
    from spindle.owner_episode_convergence import ProtectedRecordUpdate, publish_record_updates

    record = episode_store.write("legacy-terminal", status="complete", result="proven")

    with pytest.raises(ProtectedRecordUpdate):
        publish_record_updates(
            "legacy-terminal",
            record,
            {"status": "error", "error": "replacement", "completed_at": "2026-08-13T00:00:00"},
        )

    assert episode_store.read("legacy-terminal") == record


# --- defect 9: an accepted receipt is not a discharged duty -------------------


def test_an_accepted_receipt_is_not_a_settled_duty_until_its_cleanup_is_terminal(episode_store):
    """The owner writes the winner's receipt twice; only the second one settles.

    ``accepted`` is what an owner writes the moment it takes a control request -
    before it has signalled the provider, seen the child exit, or proved
    cleanup.  An owner that dies in between leaves exactly that receipt.  A
    converger which reads any receipt as settlement leaves the request durably
    claiming an owner is still working on it, forever.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_cancel"])
    # Rewind the winner's receipt to the moment its owner acknowledged the
    # request and then died: acknowledged, with no cleanup terminal on it.
    write_receipt(
        episode_store,
        scenario.spool_id,
        {
            "request_id": scenario.request_id,
            "owner_generation": GENERATION,
            "owner_acknowledged_at": ACKNOWLEDGED_AT,
            "provider_cancel_attempted_at": ACKNOWLEDGED_AT,
            "cleanup_outcome": "accepted",
        },
    )

    converge_to_fixed_point(scenario.spool_id)

    receipt = read_control_receipt(episode_store.root, scenario.spool_id, scenario.request_id)
    assert receipt.cleanup_outcome == "cleaned", (
        f"an unfinished accepted receipt was counted as settled (cleanup_outcome={receipt.cleanup_outcome!r}); "
        f"the episode proves the cleanup it should have been finished from"
    )
    assert receipt.child_exit_observed_at == CHILD_EXIT_AT
    record = episode_store.read(scenario.spool_id)
    entry = obligations_of(record)["control_receipts"]
    assert entry["progress"] == "complete"
    assert entry["completion"]["receipts"][scenario.request_id] == "cleaned", (
        "the duty completed with evidence that names an unsettled receipt"
    )
    assert record["terminal_origin"] == "accepted_cancel", "finishing a receipt moved the published outcome"

    receipt_path = mailbox_path(episode_store.root, scenario.spool_id) / f"{scenario.request_id}.receipt"
    settled_receipt = receipt_path.read_bytes()
    settled_record = episode_store.spool_path(scenario.spool_id).read_bytes()
    converge(scenario.spool_id)
    assert receipt_path.read_bytes() == settled_receipt, "receipt completion was rewritten on terminal re-entry"
    assert episode_store.spool_path(scenario.spool_id).read_bytes() == settled_record


# --- defect 11: legacy finalization without the lock that serializes it -------


class _LegacyProcessHandle:
    """The provider handle a pre-episode launcher kept for its own child."""

    def __init__(self, pid: int, status: int = 0):
        self.pid = pid
        self._status = status
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._status

    def wait(self, timeout=None):  # pragma: no cover - reaping path only
        return self._status


def test_two_legacy_finalizers_serialize_under_the_record_lock(episode_store, monkeypatch):
    """One fixed .tmp name per spool, so two real finalizers must not overlap.

    ``_write_spool`` renames through ``<spool>.tmp`` and
    ``_post_terminal_bookkeeping`` documents that its caller holds the record
    lock.  An unserialized legacy finalizer parses the captures, then publishes
    from a snapshot another finalizer may already have superseded - and both
    write the same temporary pathname. The first production call is paused
    after acquiring the lock and before publishing. A second production call
    must report the spool as still running without entering the publisher.
    """
    spool_id = "legacy-finalize-serialized"
    handle = _LegacyProcessHandle(424242)
    episode_store.write(
        spool_id,
        status="running",
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        prompt="work",
        pid=handle.pid,
    )
    spindle._PROC_HANDLES[spool_id] = handle
    spindle._get_output_path(spool_id).write_text(NATURAL_STDOUT)
    spindle._get_exit_path(spool_id).write_text("0\n")
    assert spindle._reconcile_spool_ownership(episode_store.read(spool_id)).state == "terminalizable", (
        "this legacy record is not finalizable at all, so serialization is never reached"
    )

    from spindle import owner_episode_convergence

    real_finalize = owner_episode_convergence.finalize_legacy_spool
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = {}
    errors = {}

    def paused_finalize(*args, **kwargs):
        calls.append(threading.get_ident())
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release the first legacy finalizer")
        return real_finalize(*args, **kwargs)

    def run_finalizer(name):
        try:
            results[name] = spindle._check_and_finalize_spool(spool_id)
        except BaseException as exc:  # noqa: BLE001 - preserve worker failure for the main test thread
            errors[name] = exc

    monkeypatch.setattr(owner_episode_convergence, "finalize_legacy_spool", paused_finalize)
    first = threading.Thread(target=run_finalizer, args=("first",))
    second = threading.Thread(target=run_finalizer, args=("second",))
    first.start()
    try:
        assert entered.wait(timeout=2), "the first finalizer never reached its publication path"
        second.start()
        second.join(timeout=2)
        assert not second.is_alive(), "the second finalizer blocked instead of using the non-blocking spool lock"
        assert results.get("second") is False
        assert len(calls) == 1, "both finalizers entered the legacy publisher at once"
        during = episode_store.read(spool_id)
    finally:
        release.set()
        first.join(timeout=2)
        if second.is_alive():
            second.join(timeout=2)

    assert not first.is_alive(), "the first finalizer did not finish after the test released it"
    assert errors == {}
    assert results.get("first") is True
    assert during["status"] == "running", "a second finalizer published a terminal beside the one holding the lock"
    assert episode_store.read(spool_id)["status"] == "complete"


# --- Phase 3 Fell round-2: six accepted defects -----------------------------


def test_release_projection_and_obligation_intent_are_one_atomic_record_commit(episode_store, tmp_path, monkeypatch):
    """A crash after release cannot expose released/running/no-intent.

    This drives the exact-inode release branch through the production
    reconciliation step.  The injected crash fires immediately after the
    transition writer returns, before convergence can perform any later record
    write.  Therefore every field observed here had to be committed by the same
    CAS-protected transition as ``phase=released``.
    """

    class InjectedCrash(RuntimeError):
        pass

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="atomic-release-terminal-intent",
        tmp_path=tmp_path,
        with_shard=True,
        with_handle=False,
        before_release=True,
    )
    before_release = episode_store.read(scenario.spool_id)
    before_release.update(
        error="stale error",
        error_kind="stale_error",
        gate_category="stale-gate",
        pending_background_tasks=[{"id": "stale", "source": "stale"}],
    )
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(before_release))
    real_transition = spindle.transition_owner_episode
    released_snapshots = []

    def crash_after_release(*args, **kwargs):
        result = real_transition(*args, **kwargs)
        if kwargs.get("destination") == "released" and result.accepted:
            released_snapshots.append(episode_store.read(scenario.spool_id))
            raise InjectedCrash("after authoritative release commit")
        return result

    monkeypatch.setattr(spindle, "transition_owner_episode", crash_after_release)

    with pytest.raises(InjectedCrash):
        converge(scenario.spool_id)

    assert len(released_snapshots) == 1, "the exact-inode release transition was never reached"
    record = released_snapshots[0]
    assert record[EPISODE_KEY]["phase"] == "released"
    assert record["status"] == "complete", "release became visible with the stale running projection"
    assert record["terminal_origin"] == "natural_success"
    assert "error" not in record
    assert "error_kind" not in record
    assert "gate_category" not in record
    assert "pending_background_tasks" not in record
    assert obligations_of(record), "release became visible without durable convergence intent"
    assert obligation_progress_of(record) == {
        "natural_transcript": "pending",
    }, "the atomic commit did not preserve the exact terminal's owed transcript intent"


def test_watchdog_cleanup_decides_owner_crash_before_a_partial_winner_receipt_is_repaired(episode_store):
    """A partial receipt is an obligation, not evidence about cleanup order.

    The real watchdog publishes ``owner_crash`` and containment cleanup from an
    accepted owner episode.  The winner's receipt is deliberately left at its
    first durable form: acknowledged and accepted, without a child-exit fact.
    Cleanup and winner facts must decide the immutable timeout before the
    obligation executor advances that receipt to ``cleaned``.
    """
    from spindle import owner_watchdog

    spool_id = "watchdog-crash-partial-winner"
    request_id = publish_request(
        episode_store,
        spool_id,
        "timeout",
        GENERATION,
        request_id="req-watchdog-timeout",
    )
    write_receipt(
        episode_store,
        spool_id,
        {
            "request_id": request_id,
            "owner_generation": GENERATION,
            "owner_acknowledged_at": ACKNOWLEDGED_AT,
            "provider_cancel_attempted_at": ACKNOWLEDGED_AT,
            "cleanup_outcome": "accepted",
        },
    )
    episode = localize_identities(
        causal_episode(
            "accepted",
            winning_request=request_fact("timeout", request_id),
            acknowledgement=acknowledgement_fact(),
        )
    )
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        owner_generation=GENERATION,
        timeout=TIMEOUT_SECONDS,
        harness="claude-code",
        created_at="2026-08-01T00:00:00",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )

    owner_watchdog._record_owner_crash(
        episode_store.root,
        spool_id,
        owner_pid=episode["owner"]["pid"],
        owner_generation=GENERATION,
        owner_birth_token=episode["owner"]["birth_token"],
        status=9,
        contained=True,
    )
    converge(spool_id)

    record = episode_store.read(spool_id)
    receipt = read_control_receipt(episode_store.root, spool_id, request_id)
    assert record[EPISODE_KEY]["failure"]["kind"] == "owner_crash"
    assert record[EPISODE_KEY]["cleanup"]["outcome"] == "watchdog_contained"
    assert record["terminal_origin"] == "owner_crash_after_acknowledged_cleanup"
    assert (record["status"], record["error_kind"]) == ("timeout", "timeout")
    assert receipt.cleanup_outcome == "cleaned"
    assert receipt.child_exit_observed_at == record[EPISODE_KEY]["cleanup"]["child_exit_observed_at"]


def test_convergence_uses_the_explicit_observer_handle_reap_and_reports_its_failure(episode_store):
    """The caller-supplied local capability owns local observation."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="explicit-observer-handle-reap",
        with_handle=False,
    )
    calls = []

    def failed_reap(spool_id):
        calls.append(spool_id)
        raise OSError("observer cannot inspect its process handle")

    observer = ObserverIdentity(
        pid=os.getpid(),
        namespace=spindle.capture_pid_namespace(),
        local_effects={"handle_reap": failed_reap},
    )

    result = converge_owner_episode(scenario.spool_id, observer)

    assert calls == [scenario.spool_id], "convergence bypassed the explicit observer capability"
    assert result.local_errors == ("OSError: observer cannot inspect its process handle",)
    assert result.terminal_state == "terminal_with_obligations_pending", (
        "convergence claimed a fixed point after the requested local observation failed"
    )


@pytest.mark.parametrize("malformed_recorded", (False, True), ids=("unsupported", "malformed"))
def test_indeterminate_namespace_observer_cannot_release_cleanup_proven_episode(episode_store, malformed_recorded):
    """Indeterminate namespace identities cannot prove an observer is local.

    The exact ownership inode is released and cleanup is durable, but the owner
    identity is unsupported or malformed and the observer failed to capture its
    own PID-namespace coordinates.  Missing coordinates are indeterminate, not
    equal, so the observer must not receive release authority.
    """
    from spindle.namespace_owner import NamespaceIdentity
    from spindle.owner_episode_convergence import ObserverIdentity, classify_owner_episode_record

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="unsupported-namespace-cleanup-proven",
        with_handle=False,
        before_release=True,
    )
    record = episode_store.read(scenario.spool_id)
    recorded_namespace = (
        {"status": "supported"}
        if malformed_recorded
        else NamespaceIdentity.unsupported("owner namespace capture failed").to_dict()
    )
    record[EPISODE_KEY]["owner"]["namespace"] = recorded_namespace
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))
    observer = ObserverIdentity(
        pid=os.getpid(),
        namespace=NamespaceIdentity.unsupported("observer namespace capture failed"),
        local_effects={},
    )

    result = classify_owner_episode_record(episode_store.read(scenario.spool_id), observer)

    assert result.may_mutate is False
    if malformed_recorded:
        assert result.classification == "store_unhealthy"
        assert result.reason == "ownership_identity_mismatch"
    else:
        assert result.classification == "unverifiable"
        assert result.reason == "foreign_observer_cannot_prove_release"
        assert result.refusal_reason == "observer PID namespace does not match the owner episode"


def test_pending_episode_recovers_deterministic_shard_under_lock_before_terminalization(
    episode_store, tmp_path, monkeypatch
):
    """The episode fast path cannot discard crash-created shard metadata."""
    spool_id = "pending-episode-deterministic-shard"
    source = tmp_path / "source"
    worktree = source / "worktrees" / spool_id
    worktree.mkdir(parents=True)
    episode = localize_identities(causal_episode("reserved"))
    episode_store.write(
        spool_id,
        status="pending",
        episode=episode,
        owner_generation=GENERATION,
        created_at="2026-08-01T00:00:00",
        shard_requested=True,
        launch_working_dir=str(source),
        harness="claude-code",
    )
    real_recover = spindle._recover_deterministic_shard
    lock_observations = []

    def recover_while_locked(record):
        with spindle._spool_lock(spool_id, blocking=False) as acquired:
            lock_observations.append(acquired)
        real_recover(record)

    monkeypatch.setattr(spindle, "_recover_deterministic_shard", recover_while_locked)

    assert spindle._reconcile_pending_spool(spool_id) is False

    record = episode_store.read(spool_id)
    assert lock_observations == [False], "deterministic shard recovery ran without the spool record lock"
    assert record["shard"]["worktree_path"] == str(worktree)
    assert record["working_dir"] == str(worktree)
    assert record["shard_created_by_spool"] is True
    assert record["shard_cleanup_preserved"] is True
    assert obligations_of(record)["failed_shard_preservation"]["progress"] == "complete"


def test_legacy_transcript_ioerror_cannot_skip_terminal_bookkeeping_or_handle_cleanup(
    episode_store, tmp_path, monkeypatch
):
    """Transcript persistence remains best-effort after terminal durability."""
    spool_id = "legacy-transcript-ioerror"
    handle = _LegacyProcessHandle(424242)
    episode_store.write(
        spool_id,
        status="running",
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        prompt="work",
        pid=handle.pid,
        session_id="legacy-session",
    )
    spindle._PROC_HANDLES[spool_id] = handle
    spindle._get_output_path(spool_id).write_text(NATURAL_STDOUT)
    spindle._get_exit_path(spool_id).write_text("0\n")
    blocked_transcript = tmp_path / "transcript-is-a-directory"
    blocked_transcript.mkdir()
    monkeypatch.setattr(spindle, "_get_transcript_path", lambda _spool_id: blocked_transcript)

    assert spindle._check_and_finalize_spool(spool_id) is True

    record = episode_store.read(spool_id)
    assert record["status"] == "complete", "the terminal record was not durable"
    assert record["result"] == "the natural answer"
    assert handle.polls, "terminal bookkeeping never observed the process handle"
    assert spool_id not in spindle._PROC_HANDLES, "transcript IOError skipped process-handle cleanup"


# --- Phase 3 Fell round-3: six accepted defects -----------------------------


def test_public_reconciliation_preserves_a_status_only_owner_terminal_byte_for_byte(episode_store):
    """A rolling-upgrade terminal stays terminal without invented provenance."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="mixed-version-status-only-terminal",
        with_handle=False,
    )
    record = episode_store.read(scenario.spool_id)
    record.update(
        status="complete",
        result="terminal from the older writer",
        completed_at="2026-08-01T00:00:05",
    )
    record.pop("terminal_origin", None)
    record.pop("terminal_provenance", None)
    record.pop("owner_convergence", None)
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))
    before = episode_store.spool_path(scenario.spool_id).read_bytes()

    assert spindle._reconcile_spool_step(scenario.spool_id) is False
    assert episode_store.spool_path(scenario.spool_id).read_bytes() == before

    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    result = converge_owner_episode(scenario.spool_id, ObserverIdentity.for_this_process())
    assert result.terminal_state == "fully_converged"
    assert result.projection is None
    assert episode_store.spool_path(scenario.spool_id).read_bytes() == before


def test_an_acknowledged_nonwinner_receipt_settles_from_proven_cleanup(episode_store):
    """Acknowledgement makes cleanup, not rejection, the terminal disposition."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_cancel"],
        spool_id="acknowledged-nonwinner",
        extra_request="drop",
    )
    sibling = scenario.extras["extra_request_id"]
    write_receipt(
        episode_store,
        scenario.spool_id,
        {
            "request_id": sibling,
            "owner_generation": GENERATION,
            "owner_acknowledged_at": ACKNOWLEDGED_AT,
            "provider_cancel_attempted_at": ACKNOWLEDGED_AT,
            "cleanup_outcome": "accepted",
        },
    )

    converge_to_fixed_point(scenario.spool_id)

    receipt = read_control_receipt(episode_store.root, scenario.spool_id, sibling)
    entry = obligations_of(episode_store.read(scenario.spool_id))["control_receipts"]
    assert receipt.cleanup_outcome == "cleaned"
    assert receipt.child_exit_observed_at == CHILD_EXIT_AT
    assert entry["intent"]["disposition"]["acknowledged"] == "cleaned"
    assert entry["progress"] == "complete"
    assert entry["completion"]["receipts"][sibling] == "cleaned"


def test_missing_winner_receipt_survives_a_crash_and_reaches_terminal_cleanup(episode_store, monkeypatch):
    """Recovery may crash after recreating the owner acknowledgement."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    class InjectedCrash(RuntimeError):
        pass

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_timeout"],
        spool_id="missing-winner-receipt-crash",
    )
    receipt_path = mailbox_path(episode_store.root, scenario.spool_id) / f"{scenario.request_id}.receipt"
    receipt_path.unlink()
    real_write_receipt = spindle.write_control_receipt
    fired = False

    def crash_after_durable_receipt(*args, **kwargs):
        nonlocal fired
        receipt = real_write_receipt(*args, **kwargs)
        if not fired:
            fired = True
            raise InjectedCrash("after missing winner receipt became durable")
        return receipt

    with monkeypatch.context() as armed:
        armed.setattr(spindle, "write_control_receipt", crash_after_durable_receipt)
        with pytest.raises(InjectedCrash):
            converge_owner_episode(scenario.spool_id, ObserverIdentity.for_this_process())

    partial = read_control_receipt(episode_store.root, scenario.spool_id, scenario.request_id)
    assert fired and partial is not None
    assert partial.owner_acknowledged_at == ACKNOWLEDGED_AT
    assert partial.cleanup_outcome == "accepted"
    assert obligations_of(episode_store.read(scenario.spool_id))["control_receipts"]["progress"] == "pending"

    converge_to_fixed_point(scenario.spool_id)

    settled = read_control_receipt(episode_store.root, scenario.spool_id, scenario.request_id)
    entry = obligations_of(episode_store.read(scenario.spool_id))["control_receipts"]
    assert settled.cleanup_outcome == "cleaned"
    assert settled.child_exit_observed_at == CHILD_EXIT_AT
    assert entry["intent"]["disposition"]["winner"] == "cleaned"
    assert entry["progress"] == "complete"


# --- final Fell: three accepted defects -------------------------------------


def _status_only_outcome_bytes(record: dict) -> bytes:
    """Canonical bytes for the fields a mixed-version terminal already owns."""
    names = (
        "status",
        "result",
        "error",
        "error_kind",
        "exit_code",
        "completed_at",
        "terminal_origin",
        "terminal_provenance",
        "lifecycle",
    )
    return json.dumps({name: record[name] for name in names if name in record}, separators=(",", ":")).encode()


def _seed_status_only_cleanup_proven_request(episode_store, spool_id):
    """Leave one old-writer terminal whose exact inode is not yet released."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id=spool_id,
        with_handle=False,
        extra_request="drop",
        before_release=True,
    )
    record = episode_store.read(spool_id)
    record.update(
        status="complete",
        result="terminal from the older writer",
        completed_at="2026-08-01T00:00:05",
        lifecycle={"ownership_state": "held", "transport_state": "reaped"},
    )
    record.pop("terminal_origin", None)
    record.pop("terminal_provenance", None)
    record.pop("owner_convergence", None)
    episode_store.spool_path(spool_id).write_text(json.dumps(record))
    return scenario, _status_only_outcome_bytes(record)


def test_status_only_request_does_not_authorize_mutating_an_active_accepted_owner(episode_store):
    """Discovery is not authority to write while the exact owner is active."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    spool_id = "status-only-active-accepted-owner"
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="complete",
        episode=episode,
        result="terminal from the older writer",
        completed_at="2026-08-01T00:00:05",
        owner_generation=GENERATION,
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    publish_request(episode_store, spool_id, "drop", GENERATION)
    episode_store.hold_lock(spool_id)
    before = episode_store.spool_path(spool_id).read_bytes()

    result = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())

    assert result.classification == "active"
    assert result.may_mutate is False
    assert episode_store.spool_path(spool_id).read_bytes() == before
    assert "owner_convergence" not in episode_store.read(spool_id)


@pytest.mark.parametrize("observer_kind", ("foreign", "unsupported"))
def test_status_only_request_does_not_authorize_a_nonlocal_cleanup_observer(episode_store, observer_kind):
    """Only a local observer may freeze work before proving exact release."""
    from spindle.namespace_owner import NamespaceIdentity
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    spool_id = f"status-only-cleanup-{observer_kind}-observer"
    _seed_status_only_cleanup_proven_request(episode_store, spool_id)
    local = spindle.capture_pid_namespace()
    assert local.is_supported
    namespace = (
        NamespaceIdentity.supported(local.device, local.inode + 1)
        if observer_kind == "foreign"
        else NamespaceIdentity.unsupported("observer namespace capture failed")
    )
    observer = ObserverIdentity(pid=os.getpid(), namespace=namespace, local_effects={})
    before = episode_store.spool_path(spool_id).read_bytes()

    result = converge_owner_episode(spool_id, observer)

    assert result.classification == "unverifiable"
    assert result.reason == "foreign_observer_cannot_prove_release"
    assert result.may_mutate is False
    assert episode_store.spool_path(spool_id).read_bytes() == before
    assert "owner_convergence" not in episode_store.read(spool_id)


@pytest.mark.parametrize("lock_state", ("held", "identity_mismatch"))
def test_status_only_cleanup_refusal_first_freezes_its_discovered_request_duty(episode_store, lock_state):
    """Release refusal cannot leave an old terminal's known duty implicit."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    spool_id = f"status-only-cleanup-refusal-{lock_state}"
    scenario, outcome = _seed_status_only_cleanup_proven_request(episode_store, spool_id)
    if lock_state == "held":
        episode_store.hold_lock(spool_id)
    else:
        lock_path = episode_store.lock_path(spool_id)
        old_inode = os.open(lock_path, os.O_RDWR)
        episode_store.held.append(old_inode)
        lock_path.unlink()
        lock_path.touch(mode=0o600)

    first = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    after_first = episode_store.read(spool_id)
    first_bytes = episode_store.spool_path(spool_id).read_bytes()
    entry = obligations_of(after_first)["control_receipts"]

    assert first.classification == ("active" if lock_state == "held" else "store_unhealthy")
    assert first.may_mutate is False
    if lock_state == "identity_mismatch":
        assert first.reason == "ownership_identity_mismatch"
    assert first.terminal_state == "terminal_with_obligations_pending"
    assert tuple(obligation.kind for obligation in first.obligations) == ("control_receipts",)
    assert entry["progress"] == "pending"
    assert entry["idempotency_key"] == {
        scenario.extras["extra_request_id"]: f"{GENERATION}/{scenario.extras['extra_request_id']}"
    }
    assert read_control_receipt(episode_store.root, spool_id, scenario.extras["extra_request_id"]) is None
    assert after_first[EPISODE_KEY]["phase"] == "cleanup_proven"
    assert _status_only_outcome_bytes(after_first) == outcome
    assert "terminal_origin" not in after_first
    assert "terminal_provenance" not in after_first

    second = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())

    assert second.terminal_state == "terminal_with_obligations_pending"
    assert len(second.obligations) == 1
    assert episode_store.spool_path(spool_id).read_bytes() == first_bytes


def test_status_only_cleanup_duty_settles_after_the_exact_inode_is_released(episode_store):
    """A later valid release completes frozen work without changing old outcome."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    spool_id = "status-only-cleanup-later-release"
    scenario, outcome = _seed_status_only_cleanup_proven_request(episode_store, spool_id)
    episode_store.hold_lock(spool_id)

    refused = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    assert refused.terminal_state == "terminal_with_obligations_pending"

    episode_store.close()
    settled = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    record = episode_store.read(spool_id)
    receipt = read_control_receipt(episode_store.root, spool_id, scenario.extras["extra_request_id"])

    assert settled.terminal_state == "fully_converged"
    assert record[EPISODE_KEY]["phase"] == "released"
    assert obligations_of(record)["control_receipts"]["progress"] == "complete"
    assert receipt is not None
    assert receipt.cleanup_outcome == NO_WINNER_RECEIPT
    assert _status_only_outcome_bytes(record) == outcome
    assert "terminal_origin" not in record
    assert "terminal_provenance" not in record


@pytest.mark.parametrize("duty", ("current_request", "failed_shard"))
def test_cleanup_then_recovery_materializes_status_only_owner_duties_without_rewriting_outcome(
    episode_store, tmp_path, duty
):
    """Startup retention cannot erase duties emitted by an older terminal writer."""
    spool_id = f"status-only-startup-{duty}"
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural_failure"],
        spool_id=spool_id,
        tmp_path=tmp_path,
        with_shard=duty == "failed_shard",
        with_handle=False,
        extra_request="drop" if duty == "current_request" else None,
    )
    record = episode_store.read(spool_id)
    record.update(
        status="error",
        result=None,
        error="terminal from the older writer",
        error_kind="provider_failed",
        completed_at="2026-08-01T00:00:05",
        lifecycle={
            "ownership_state": "released",
            "transport_state": "reaped",
            "normalized_terminal_kind": "failed",
        },
    )
    record.pop("terminal_origin", None)
    record.pop("terminal_provenance", None)
    record.pop("owner_convergence", None)
    episode_store.spool_path(spool_id).write_text(json.dumps(record))
    outcome = _status_only_outcome_bytes(record)

    spindle._cleanup_old_spools()

    assert episode_store.spool_path(spool_id).exists(), f"retention deleted the discoverable {duty} duty"
    assert spindle._recovery_pass() == []

    recovered = episode_store.read(spool_id)
    assert _status_only_outcome_bytes(recovered) == outcome
    assert "terminal_origin" not in recovered
    assert "terminal_provenance" not in recovered
    entry_name = "control_receipts" if duty == "current_request" else "failed_shard_preservation"
    assert obligations_of(recovered)[entry_name]["progress"] == "complete"
    if duty == "current_request":
        request_id = scenario.extras["extra_request_id"]
        receipt = read_control_receipt(episode_store.root, spool_id, request_id)
        assert receipt is not None
        assert receipt.cleanup_outcome == NO_WINNER_RECEIPT
    else:
        assert recovered.get("shard_cleanup_preserved") is True
        assert recovered["shard"].get("startup_failure_preserved") is True


@pytest.mark.parametrize("evidence", ("natural", "accepted_cancel"))
def test_before_release_atomic_projection_clears_stale_fields_and_preserves_current_facts(
    episode_store, monkeypatch, evidence
):
    """The release CAS applies both projection writes and explicit deletions."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE[evidence],
        spool_id=f"before-release-deletions-{evidence}",
        with_handle=False,
        before_release=True,
    )
    record = episode_store.read(scenario.spool_id)
    record.update(
        error="stale error",
        error_kind="stale_error",
        exit_code=99,
        cost=99.0,
        gate_category="stale-gate",
        pending_background_tasks=[{"id": "stale", "source": "stale"}],
    )
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    class InjectedCrash(RuntimeError):
        pass

    real_transition = spindle.transition_owner_episode
    released_snapshots = []

    def crash_after_release(*args, **kwargs):
        result = real_transition(*args, **kwargs)
        if kwargs.get("destination") == "released" and result.accepted:
            released_snapshots.append(episode_store.read(scenario.spool_id))
            raise InjectedCrash("after atomic release projection")
        return result

    monkeypatch.setattr(spindle, "transition_owner_episode", crash_after_release)

    with pytest.raises(InjectedCrash):
        converge(scenario.spool_id)

    assert len(released_snapshots) == 1
    released = released_snapshots[0]
    assert released[EPISODE_KEY]["phase"] == "released"
    assert released["tags"] == ["keep-me"]
    assert released["session_id"] == scenario.expected_preservation()["session_id"]
    assert "pending_background_tasks" not in released
    assert "gate_category" not in released
    assert released["exit_code"] == 0
    if evidence == "natural":
        assert "error" not in released
        assert "error_kind" not in released
        assert released["cost"] == scenario.expected_preservation()["cost"]
    else:
        assert released["error"] == "Cancelled"
        assert released["error_kind"] == "cancelled"
        assert "cost" not in released


def test_active_supervisor_reconciliation_uses_no_capture_or_mailbox_guard(episode_store, monkeypatch):
    """A clearly live episode takes the lightweight classification return."""
    spool_id = "active-supervisor-lightweight"
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        owner_generation=GENERATION,
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    episode_store.hold_lock(spool_id)
    guard_entries = []
    real_guard = spindle.mailbox_guard

    @contextmanager
    def counted_guard(root, guarded_spool_id):
        guard_entries.append(guarded_spool_id)
        with real_guard(root, guarded_spool_id):
            yield

    class ForbiddenCapture:
        def read_text(self):
            raise AssertionError("active reconciliation read a full capture")

    monkeypatch.setattr(spindle, "mailbox_guard", counted_guard)
    monkeypatch.setattr(spindle, "_get_output_path", lambda _spool_id: ForbiddenCapture())
    monkeypatch.setattr(spindle, "_get_stderr_path", lambda _spool_id: ForbiddenCapture())

    assert spindle._reconcile_spool_step(spool_id) is True
    assert guard_entries == []


def test_active_reconciliation_does_not_block_control_admission(episode_store, monkeypatch):
    """A slow lightweight observation owns no guard needed by admission."""
    from spindle import owner_episode_convergence

    spool_id = "active-reconciliation-concurrent-admission"
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        owner_generation=GENERATION,
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    episode_store.hold_lock(spool_id)
    classified = threading.Event()
    release = threading.Event()
    real_classify = owner_episode_convergence.classify_owner_episode_record
    results = {}

    def pause_after_classification(record, observer=None):
        meaning = real_classify(record, observer)
        if threading.current_thread().name == "active-reconciler":
            classified.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release active classification")
        return meaning

    monkeypatch.setattr(owner_episode_convergence, "classify_owner_episode_record", pause_after_classification)

    reconciliation = threading.Thread(
        target=lambda: results.setdefault("reconciled", spindle._reconcile_spool_step(spool_id)),
        name="active-reconciler",
    )
    admission = threading.Thread(
        target=lambda: results.setdefault(
            "admitted", spindle._request_owner_stop(spool_id, "cancel", "active-fast-path-test")
        ),
        name="control-admission",
    )
    reconciliation.start()
    try:
        assert classified.wait(timeout=2), "active reconciliation never reached classification"
        admission.start()
        admission.join(timeout=1)
        assert not admission.is_alive(), "admission blocked behind active reconciliation"
    finally:
        release.set()
        reconciliation.join(timeout=2)
        if admission.is_alive():
            admission.join(timeout=2)

    assert not reconciliation.is_alive()
    request, error = results["admitted"]
    assert request is not None, error
    assert results["reconciled"] is True


# --- finding-20260813-wy2y: accepted convergence repairs -------------------


def test_malformed_mailbox_entry_does_not_hide_a_valid_status_only_duty(episode_store, tmp_path):
    """One bad mixed-version request cannot erase a valid current-generation duty."""
    spool_id = "status-only-valid-request-beside-malformed"
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural_failure"],
        spool_id=spool_id,
        tmp_path=tmp_path,
        with_handle=False,
        extra_request="drop",
    )
    record = episode_store.read(spool_id)
    record.update(status="error", error="older terminal", completed_at="2026-08-01T00:00:05")
    record.pop("terminal_origin", None)
    record.pop("terminal_provenance", None)
    record.pop("owner_convergence", None)
    episode_store.spool_path(spool_id).write_text(json.dumps(record))
    mailbox = mailbox_path(episode_store.root, spool_id)
    (mailbox / "000-malformed.request").write_text("{")

    assert spindle._recovery_pass() == []

    request_id = scenario.extras["extra_request_id"]
    receipt = read_control_receipt(episode_store.root, spool_id, request_id)
    assert receipt is not None, "the malformed sibling hid the valid current-generation request"
    assert receipt.cleanup_outcome == NO_WINNER_RECEIPT
    assert obligations_of(episode_store.read(spool_id))["control_receipts"]["progress"] == "complete"


@pytest.mark.parametrize("damaged_id", ("000-malformed-receipt", "zzz-malformed-receipt"))
@pytest.mark.parametrize("damaged_body", ("{", "[]"), ids=("truncated-json", "non-object-json"))
def test_malformed_receipt_does_not_hide_valid_sibling_duties(
    episode_store,
    tmp_path,
    damaged_id,
    damaged_body,
):
    """A corrupt receipt stays visible without blocking independent siblings."""
    spool_id = f"malformed-receipt-{damaged_id[:3]}-{len(damaged_body)}"
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural_failure"],
        spool_id=spool_id,
        tmp_path=tmp_path,
        with_handle=False,
        extra_request="drop",
    )
    valid_id = scenario.extras["extra_request_id"]
    publish_request(
        episode_store,
        spool_id,
        "cancel",
        GENERATION,
        request_id=damaged_id,
    )
    damaged_path = mailbox_path(episode_store.root, spool_id) / f"{damaged_id}.receipt"
    damaged_path.write_text(damaged_body)
    damaged_bytes = damaged_path.read_bytes()

    converge_without_escape(spool_id)

    valid_receipt = read_control_receipt(episode_store.root, spool_id, valid_id)
    record = episode_store.read(spool_id)
    entry = obligations_of(record)["control_receipts"]
    assert valid_receipt is not None
    assert valid_receipt.cleanup_outcome == NO_WINNER_RECEIPT
    assert damaged_path.read_bytes() == damaged_bytes
    assert record["status"] == "error"
    assert entry["progress"] == "pending"
    assert damaged_id in entry["error"]
    assert "malformed control receipt" in entry["error"]

    converge_without_escape(spool_id)
    assert damaged_path.read_bytes() == damaged_bytes
    assert read_control_receipt(episode_store.root, spool_id, valid_id) == valid_receipt


def test_durable_supervisor_revisits_terminal_obligations_until_complete(episode_store, monkeypatch):
    """A transient terminal-duty failure remains supervisor work on the next pass."""
    spool_id = "terminal-obligation-supervisor-retry"
    episode_store.write(
        spool_id,
        status="complete",
        completed_at="2026-08-01T00:00:05",
        owner_convergence={
            "format": "spindle.owner-convergence/1",
            "obligations": {
                "natural_transcript": {
                    "kind": "natural_transcript",
                    "idempotency_key": "2/transcript/example",
                    "intent": {},
                    "progress": "pending",
                    "completion": None,
                    "error": "OSError: transient write failure",
                }
            },
        },
    )
    attempts = []

    def reconcile(target):
        attempts.append(target)
        if len(attempts) == 2:
            record = episode_store.read(target)
            entry = record["owner_convergence"]["obligations"]["natural_transcript"]
            entry.update(progress="complete", completion={"complete": True}, error=None)
            spindle._write_spool(target, record)
        return False

    monkeypatch.setattr(spindle, "_list_spools", lambda: [episode_store.read(spool_id)])
    monkeypatch.setattr(spindle, "_reconcile_spool_step", reconcile)
    monkeypatch.setattr(spindle, "_count_running", lambda: 0)
    monkeypatch.setattr(spindle, "SUPERVISOR_IDLE_GRACE", 0)
    monkeypatch.setattr(spindle.time, "sleep", lambda _seconds: None)
    ticks = iter(range(20))
    monkeypatch.setattr(spindle.time, "monotonic", lambda: next(ticks))
    lock_fd = os.open(episode_store.root / ".supervisor.lock", os.O_CREAT | os.O_RDWR)

    spindle._run_store_supervisor(str(episode_store.root), lock_fd)

    assert attempts == [spool_id, spool_id]
    assert obligations_of(episode_store.read(spool_id))["natural_transcript"]["progress"] == "complete"


def test_completed_status_only_duties_do_not_prevent_retirement(episode_store, tmp_path):
    """Mixed-version discovery ends when every materialized duty is complete."""
    spool_id = "status-only-complete-duty-retires"
    build_scenario(
        episode_store,
        BY_EVIDENCE["natural_failure"],
        spool_id=spool_id,
        tmp_path=tmp_path,
        with_handle=False,
        extra_request="drop",
    )
    record = episode_store.read(spool_id)
    record.update(status="error", error="older terminal", completed_at="2026-08-01T00:00:05")
    record.pop("terminal_origin", None)
    record.pop("terminal_provenance", None)
    record.pop("owner_convergence", None)
    episode_store.spool_path(spool_id).write_text(json.dumps(record))
    assert spindle._recovery_pass() == []
    assert obligations_of(episode_store.read(spool_id))["control_receipts"]["progress"] == "complete"

    spindle._cleanup_old_spools()

    assert not episode_store.spool_path(spool_id).exists(), "completed mixed-version duties pinned retention"


def test_check_and_finalize_bypasses_only_fully_converged_records(episode_store, monkeypatch):
    """Polling a fixed point is cheap, while pending duties still re-enter convergence."""
    from spindle import owner_episode_convergence

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="check-finalize-converged-fast-path",
        with_handle=False,
    )
    converge_to_fixed_point(scenario.spool_id)
    real_converge = owner_episode_convergence.converge_owner_episode
    calls = []

    def counted_converge(spool_id, observer):
        calls.append(spool_id)
        return real_converge(spool_id, observer)

    monkeypatch.setattr(owner_episode_convergence, "converge_owner_episode", counted_converge)

    assert spindle._check_and_finalize_spool(scenario.spool_id) is True
    assert calls == [], "an already-converged poll re-entered blocking convergence"

    record = episode_store.read(scenario.spool_id)
    entry = next(iter(record["owner_convergence"]["obligations"].values()))
    entry.update(progress="pending", completion=None, error="retry me")
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    assert spindle._check_and_finalize_spool(scenario.spool_id) is True
    assert calls == [scenario.spool_id], "the fast path skipped an outstanding durable duty"


def test_reserved_watchdog_identity_mismatch_falls_back_to_dead_starter(episode_store, monkeypatch):
    """A reused watchdog PID cannot keep a reservation alive after its starter died."""
    episode = localize_identities(causal_episode("reserved"))
    spool_id = "reserved-reused-watchdog-dead-starter"
    episode_store.write(
        spool_id,
        status="pending",
        episode=episode,
        owner_generation=GENERATION,
        lifecycle={"ownership_state": "reserved", "transport_state": "starting"},
    )
    starter_pid = episode["starter"]["pid"]
    watchdog_pid = episode["watchdog"]["pid"]

    def liveness(identity):
        if identity.pid == watchdog_pid:
            return LivenessEvidence("unverifiable", "identity_mismatch")
        assert identity.pid == starter_pid
        return LivenessEvidence("dead", "pidfd_exited")

    monkeypatch.setattr(spindle, "assess_process_liveness", liveness)

    reconciliation = spindle._reconcile_spool_ownership(episode_store.read(spool_id))

    assert reconciliation.state == "terminalizable"
    assert reconciliation.reason == "reserved_starter_dead"


# --- finding-20260813-rhr9: accepted post-repair defects -------------------


@contextmanager
def _exited_leader_with_live_group(tmp_path):
    """Yield one exact detached launch whose leader exits before containment."""
    release = tmp_path / "release-leader"
    child_path = tmp_path / "group-child.pid"
    script = """
import os
import signal
import sys
import time
from pathlib import Path

release = Path(sys.argv[1])
child_path = Path(sys.argv[2])
while not release.exists():
    time.sleep(0.005)
child = os.fork()
if child:
    child_path.write_text(str(child))
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""
    leader = subprocess.Popen(
        [sys.executable, "-c", script, str(release), str(child_path)],
        start_new_session=True,
    )
    leader_pidfd = os.pidfd_open(leader.pid)
    child_pid = None
    child_pidfd = None
    try:
        birth = spindle._process_start_time(leader.pid)
        assert birth is not None
        release.touch()
        readable, _, _ = select.select([leader_pidfd], [], [], 5)
        assert readable, "the detached group leader did not exit"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_path.exists():
            time.sleep(0.01)
        child_pid = int(child_path.read_text())
        child_pidfd = os.pidfd_open(child_pid)
        yield leader, birth, child_pid
    finally:
        if child_pidfd is not None:
            try:
                signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(child_pidfd)
        os.close(leader_pidfd)
        try:
            leader.wait(timeout=1)
        except subprocess.TimeoutExpired:
            leader.kill()
            leader.wait(timeout=5)


def test_capture_mailbox_oserror_is_typed_fail_closed_and_retryable(episode_store, monkeypatch):
    """Capture reports mailbox unavailability instead of returning a boolean."""
    from spindle.owner_episode_convergence import ObserverIdentity, converge_owner_episode

    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="capture-mailbox-unavailable",
        with_handle=False,
        extra_request="cancel",
    )
    real_iter = spindle.iter_control_requests
    with monkeypatch.context() as unavailable:
        unavailable.setattr(
            spindle,
            "iter_control_requests",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mailbox storage unavailable")),
        )
        result = converge_owner_episode(scenario.spool_id, ObserverIdentity.for_this_process())

    assert result.classification == "unverifiable"
    assert result.reason == "mailbox_unavailable"
    assert result.terminal_state == "none"
    assert "OSError: mailbox storage unavailable" in result.local_errors
    assert episode_store.read(scenario.spool_id)["status"] == "running"

    monkeypatch.setattr(spindle, "iter_control_requests", real_iter)
    converge_to_fixed_point(scenario.spool_id)
    assert episode_store.read(scenario.spool_id)["status"] == "complete"


def test_status_only_mailbox_oserror_retains_one_spool_and_recovers_later_siblings(episode_store, monkeypatch):
    """One unavailable mailbox remains supervised without aborting startup."""
    damaged = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="000-status-only-mailbox-unavailable",
        with_handle=False,
        extra_request="cancel",
    )
    healthy = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="999-status-only-mailbox-healthy",
        with_handle=False,
        extra_request="cancel",
    )
    for scenario in (damaged, healthy):
        record = episode_store.read(scenario.spool_id)
        record.update(status="complete", result="older terminal", completed_at="2026-08-01T00:00:05")
        record.pop("terminal_origin", None)
        record.pop("terminal_provenance", None)
        record.pop("owner_convergence", None)
        episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    real_iter = spindle.iter_control_requests

    def unavailable_for_one(root, spool_id):
        if spool_id == damaged.spool_id:
            raise OSError("damaged mailbox carrier")
        return real_iter(root, spool_id)

    with monkeypatch.context() as unavailable:
        unavailable.setattr(spindle, "iter_control_requests", unavailable_for_one)
        needs_monitor = spindle._recovery_pass()

    assert needs_monitor == [damaged.spool_id]
    assert read_control_receipt(episode_store.root, damaged.spool_id, damaged.extras["extra_request_id"]) is None
    healthy_receipt = read_control_receipt(
        episode_store.root,
        healthy.spool_id,
        healthy.extras["extra_request_id"],
    )
    assert healthy_receipt is not None, "the damaged mailbox aborted recovery before a later spool"
    assert healthy_receipt.cleanup_outcome == NO_WINNER_RECEIPT

    assert spindle._recovery_pass() == []
    damaged_receipt = read_control_receipt(
        episode_store.root,
        damaged.spool_id,
        damaged.extras["extra_request_id"],
    )
    assert damaged_receipt is not None
    assert damaged_receipt.cleanup_outcome == NO_WINNER_RECEIPT


def test_startup_retains_a_duty_created_while_reconciling_an_active_record(episode_store, monkeypatch):
    """The startup scan re-reads an active record after it becomes terminal."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="startup-active-to-terminal-duty",
        with_handle=False,
    )

    with monkeypatch.context() as broken:
        _unwritable_transcript(broken)
        needs_monitor = spindle._recovery_pass()

    interrupted = episode_store.read(scenario.spool_id)
    assert interrupted["status"] == "complete"
    assert obligation_progress_of(interrupted)["natural_transcript"] == "pending"
    assert needs_monitor == [scenario.spool_id]

    assert spindle._recovery_pass() == []
    recovered = episode_store.read(scenario.spool_id)
    assert obligation_progress_of(recovered)["natural_transcript"] == "complete"
    assert spindle._get_transcript_path(scenario.spool_id).read_text() == NATURAL_STDOUT


def test_lifecycle_less_terminal_mirror_cannot_hide_an_active_exact_owner(episode_store):
    """Destructive actions classify durable authority before trusting status."""
    spool_id = "terminal-mirror-active-owner"
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    record = episode_store.write(spool_id, status="complete", episode=episode)
    episode_store.hold_lock(spool_id)

    assert "lifecycle" not in record
    assert spindle._reconcile_spool_ownership(record).state == "active"
    assert spindle._spool_blocks_destructive_action(record) is True


def test_lifecycle_less_terminal_mirror_with_released_owner_remains_destructible(episode_store):
    """A released compatibility record remains safe after episode classification."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["natural"],
        spool_id="terminal-mirror-released-owner",
        with_handle=False,
    )
    record = episode_store.read(scenario.spool_id)
    record.update(status="complete", result="older terminal", completed_at="2026-08-01T00:00:05")
    record.pop("lifecycle", None)
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    assert spindle._reconcile_spool_ownership(record).state == "terminalizable"
    assert spindle._spool_blocks_destructive_action(record) is False


def test_startup_keeps_supervisor_ownership_for_a_terminal_obligation(episode_store, monkeypatch):
    """Startup recovery turns an unsuccessful terminal-duty pass into ownership."""
    spool_id = "startup-terminal-duty-supervision"
    episode_store.write(
        spool_id,
        status="complete",
        completed_at="2026-08-01T00:00:05",
        episode=localize_identities(causal_episode("released")),
        owner_convergence={
            "format": "spindle.owner-convergence/1",
            "obligations": {
                "natural_transcript": {
                    "kind": "natural_transcript",
                    "idempotency_key": "2/transcript/retry",
                    "intent": {},
                    "progress": "pending",
                    "completion": None,
                    "error": "OSError: retry",
                }
            },
        },
    )
    ensured = []
    monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda _spool_id: True)
    monkeypatch.setattr(spindle, "_start_spool_monitor", ensured.append)

    spindle._run_store_maintenance()

    assert ensured == [spool_id]


def test_supervisor_reaper_reacquires_for_terminal_obligations(episode_store, monkeypatch):
    """Unexpected supervisor death cannot strand a terminal retry until a command."""
    spool_id = "reaper-terminal-duty-supervision"
    record = episode_store.write(
        spool_id,
        status="complete",
        completed_at="2026-08-01T00:00:05",
        owner_convergence={
            "format": "spindle.owner-convergence/1",
            "obligations": {
                "natural_transcript": {
                    "kind": "natural_transcript",
                    "idempotency_key": "2/transcript/retry",
                    "intent": {},
                    "progress": "pending",
                    "completion": None,
                    "error": "OSError: retry",
                }
            },
        },
    )
    ensured = []

    class ExitedSupervisor:
        def wait(self):
            return 9

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(spindle.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(spindle, "_count_running", lambda: 0)
    monkeypatch.setattr(spindle, "_list_spools", lambda: [record])
    monkeypatch.setattr(spindle, "_ensure_store_supervisor", lambda: ensured.append(spool_id) or (True, None))

    spindle._reap_supervisor_handle_later(ExitedSupervisor(), episode_store.root)

    assert ensured == [spool_id]
