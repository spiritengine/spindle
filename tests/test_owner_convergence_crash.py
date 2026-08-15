"""The crash oracle: interrupt anywhere, restart, converge to the same truth.

Phase 1 section 4 names the cuts; brief-20260813-pkh6 states the oracle:
"interrupt at an arbitrary point, restart, and converge; authoritative outcome
and provenance equal a single uninterrupted run, receipts are complete, durable
effects are complete, and local effects are safely attempted by their owning
observer.  Outcome facts never regress, obligation state progresses
monotonically, and the fixed point is byte-stable."

Every test here is metamorphic: the same durable evidence is converged twice,
once cleanly and once through an interruption, and the two must agree.  That is
what makes the oracle implementation-independent - it never says *where* the
work happens, only that interrupting it changes nothing observable.

The evidence stops one transition short of release, so a converger still has to
prove the release itself.  Without that, the CAS and release-proof cuts have
nothing to interrupt and would report success for a run they never touched;
every cut therefore carries a fired marker and every test asserts it.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import spindle
from spindle.namespace_owner import iter_control_requests, read_control_receipt
from tests.owner_convergence_fixtures import (
    CUTS,
    GENERATION,
    NATURAL_STDOUT,
    NO_WINNER_RECEIPT,
    OBLIGATION_PROGRESS,
    ORIGINS,
    RELEASED_AT,
    InjectedCrash,
    WatchdogHandle,
    build_scenario,
    causal_episode,
    cleanup_fact,
    converge,
    converge_to_fixed_point,
    converge_without_escape,
    crashed_process,
    immutable_of,
    live_cleanup_clock,
    localize_identities,
    obligation_progress_of,
    obligations_of,
    outcome_of,
    preservation_of,
    provenance_of,
    receipt_outcomes,
    receipt_outcomes_by_kind,
    release_fact,
    restart_and_converge,
)
from tests.owner_episode_fixtures import EPISODE_KEY

BY_EVIDENCE = {row.evidence: row for row in ORIGINS}

#: One scenario carrying every durable obligation kind at once plus a local
#: handle: a natural provider failure inside a shard, with an unsettled
#: current-generation request still in the mailbox, and the release still to
#: prove.
CRASH_EVIDENCE = "natural_failure"


def _signature(record: dict) -> dict:
    """Everything an interruption must not change, minus wall-clock stamps."""
    signature = immutable_of(record)
    signature.pop("completed_at", None)
    signature["provenance"] = provenance_of(record)
    signature["preservation"] = preservation_of(record)
    signature["shard_preserved"] = record.get("shard_cleanup_preserved")
    return signature


def _crash_scenario(store, tmp_path, name: str):
    return build_scenario(
        store,
        BY_EVIDENCE[CRASH_EVIDENCE],
        spool_id=f"crash-{name}",
        tmp_path=tmp_path,
        with_shard=True,
        extra_request="cancel",
        before_release=True,
    )


def _converged_reference(store, tmp_path):
    """One uninterrupted run over the same evidence, for comparison."""
    scenario = _crash_scenario(store, tmp_path, "reference")
    converge_to_fixed_point(scenario.spool_id)
    record = store.read(scenario.spool_id)
    return scenario, record


class _RaisingHandle:
    """A local handle whose reap fails - a local error, never a durable one."""

    def poll(self):
        raise OSError("handle poll failed")

    def wait(self, timeout=None):  # pragma: no cover
        raise OSError("handle wait failed")


class _Cut:
    """One armed interruption plus the evidence that it really happened."""

    def __init__(self, name: str):
        self.name = name
        self.fired = False


def _arm(name: str, monkeypatch) -> _Cut:
    """Arm one named crash cut so the very next chance to take it is taken.

    A cut always models the same event: this process stops existing here.  It
    never models an effect raising - that failure mode has its own injector and
    its own contract.
    """
    cut = _Cut(name)

    def once(owner, attribute, *, call_through=False):
        real = getattr(owner, attribute)

        def wrapper(*args, **kwargs):
            if cut.fired:
                return real(*args, **kwargs)
            cut.fired = True
            if call_through:
                real(*args, **kwargs)
                raise InjectedCrash(f"{name}: {attribute} completed, process lost before its next step")
            raise InjectedCrash(f"{name}: {attribute} interrupted")

        monkeypatch.setattr(owner, attribute, wrapper)

    if name == "CUT-CAPTURE":
        once(spindle, "_read_spool")
    elif name == "CUT-REDUCE":
        once(spindle, "classify_owner_episode")
    elif name == "CUT-CAS":
        once(spindle, "transition_owner_episode")
    elif name == "CUT-RELEASE-PROOF":
        real = spindle.acquire_ownership_lock

        def release_proof(path, **kwargs):
            lock = real(path, **kwargs)
            if cut.fired:
                return lock
            cut.fired = True
            lock.close()
            raise InjectedCrash("CUT-RELEASE-PROOF: inode proved released, commit lost")

        monkeypatch.setattr(spindle, "acquire_ownership_lock", release_proof)
    elif name == "CUT-ATOMIC-RECORD":
        once(spindle, "_write_spool")
    elif name == "CUT-AFTER-INTENT":
        once(spindle, "_write_spool", call_through=True)
    elif name == "CUT-RECEIPT-WRITE":
        once(spindle, "write_control_receipt")
    elif name == "CUT-RECEIPT-MARK":
        once(spindle, "write_control_receipt", call_through=True)
    elif name == "CUT-SHARD-MARK":
        once(spindle, "_preserve_failed_spool_shard", call_through=True)
    elif name == "CUT-TRANSCRIPT":
        once(spindle, "_get_transcript_path")
    elif name == "CUT-LOCAL-HANDLE":
        once(spindle, "_pop_and_reap_process_handle")
    elif name == "CUT-AFTER-EFFECTS":
        once(spindle, "_pop_and_reap_process_handle", call_through=True)
    else:  # pragma: no cover - CUTS fixes the names
        raise KeyError(name)

    return cut


@pytest.mark.parametrize("cut", CUTS)
def test_interrupt_restart_and_reconverge_equals_an_uninterrupted_run(episode_store, tmp_path, monkeypatch, cut):
    """The whole crash oracle in one statement, per named cut."""
    reference_scenario, reference = _converged_reference(episode_store, tmp_path)
    scenario = _crash_scenario(episode_store, tmp_path, cut.lower())

    with monkeypatch.context() as armed:
        armed_cut = _arm(cut, armed)
        with crashed_process():
            converge(scenario.spool_id)

    assert armed_cut.fired, (
        f"{cut} never happened: the step it interrupts was not performed at all, "
        f"so this run proves nothing about interrupting it"
    )

    converge_to_fixed_point(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert _signature(record) == _signature(reference), f"{cut} changed the authoritative outcome"
    assert receipt_outcomes_by_kind(episode_store, scenario.spool_id) == receipt_outcomes_by_kind(
        episode_store, reference_scenario.spool_id
    )
    assert spindle._get_transcript_path(scenario.spool_id).exists()
    assert scenario.spool_id not in spindle._PROC_HANDLES


@pytest.mark.parametrize("cut", CUTS)
def test_a_restarted_convergence_reaches_a_byte_stable_fixed_point(episode_store, tmp_path, monkeypatch, cut):
    """Byte stability is claimed only at the fixed point, never before it."""
    scenario = _crash_scenario(episode_store, tmp_path, f"stable-{cut.lower()}")

    with monkeypatch.context() as armed:
        armed_cut = _arm(cut, armed)
        with crashed_process():
            converge(scenario.spool_id)

    assert armed_cut.fired, (
        f"{cut} never happened: the step it interrupts was not performed at all, so nothing was restarted"
    )

    converge_to_fixed_point(scenario.spool_id)
    settled = episode_store.spool_path(scenario.spool_id).read_bytes()
    converge(scenario.spool_id)

    assert episode_store.spool_path(scenario.spool_id).read_bytes() == settled


def test_a_brand_new_process_finishes_every_duty_the_crashed_one_left(episode_store, tmp_path, monkeypatch):
    """The restart the plan actually asks for: a different interpreter.

    Phase 1 section 5 states the probe literally - interrupt after the terminal
    write and before the effects, "restart in a new process, call convergence".
    Re-entering the same interpreter cannot decide it: module globals, parsed
    caches and the local handle map all survive there, so a converger that
    depended on them would pass a same-process replay and lose the work after a
    real crash.  Here the new process shares nothing but the durable artifacts.
    """
    reference_scenario, reference = _converged_reference(episode_store, tmp_path)
    scenario = _crash_scenario(episode_store, tmp_path, "fresh-process")

    with monkeypatch.context() as armed:
        cut = _arm("CUT-AFTER-INTENT", armed)
        with crashed_process():
            converge(scenario.spool_id)

    assert cut.fired
    assert scenario.spool_id in spindle._PROC_HANDLES, "the crashed process should still own its watchdog handle"

    restart_and_converge(episode_store, scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert _signature(record) == _signature(reference), "a fresh process reached a different authoritative outcome"
    assert receipt_outcomes_by_kind(episode_store, scenario.spool_id) == receipt_outcomes_by_kind(
        episode_store, reference_scenario.spool_id
    )
    assert spindle._get_transcript_path(scenario.spool_id).exists()
    assert set(obligation_progress_of(record).values()) == {"complete"}, (
        "a fresh process could not discharge the duties it found in the record"
    )
    # The durable work is done, but the handle belongs to this process alone: no
    # other process can reap it and none may record that it did.
    assert scenario.spool_id in spindle._PROC_HANDLES, "a foreign process claimed a local handle duty it cannot perform"

    converge(scenario.spool_id)

    assert scenario.spool_id not in spindle._PROC_HANDLES


def test_no_convergence_pass_ever_regresses_an_outcome_or_an_obligation(episode_store, tmp_path, monkeypatch):
    """Monotonicity, checked pass by pass rather than only at the end.

    Every statement below is conditional on there being something to move, so the
    run has to prove it saw an outcome and an obligation at all: a converger that
    publishes neither would otherwise satisfy monotonicity by never deciding.
    """
    scenario = _crash_scenario(episode_store, tmp_path, "monotonic")
    history = []

    with monkeypatch.context() as armed:
        cut = _arm("CUT-AFTER-INTENT", armed)
        with crashed_process():
            converge(scenario.spool_id)
    assert cut.fired
    history.append(episode_store.read(scenario.spool_id))

    for _ in range(4):
        converge(scenario.spool_id)
        history.append(episode_store.read(scenario.spool_id))

    settled_outcome = None
    progress = {}
    for record in history:
        outcome = outcome_of(record)
        if record.get("terminal_origin"):
            if settled_outcome is None:
                settled_outcome = outcome
            assert outcome == settled_outcome, "a published outcome fact moved"
        for kind, state in obligation_progress_of(record).items():
            assert state in OBLIGATION_PROGRESS, f"obligation {kind} reported unknown progress {state!r}"
            assert not (progress.get(kind) == "complete" and state != "complete"), (
                f"obligation {kind} regressed from complete to {state}"
            )
            progress[kind] = state

    assert settled_outcome is not None, "no pass ever published an outcome, so nothing was held monotonic"
    assert progress, "no pass ever recorded an obligation, so obligation progress was never exercised"


def test_a_crash_between_terminal_and_effects_leaves_every_duty_discoverable(episode_store, tmp_path, monkeypatch):
    """CUT-AFTER-INTENT is the reason Family A commits intent with the outcome.

    After the terminal record exists, a brand-new process with no memory of the
    first must be able to find every owed duty by reading the record alone.
    """
    scenario = _crash_scenario(episode_store, tmp_path, "after-intent")

    with monkeypatch.context() as armed:
        cut = _arm("CUT-AFTER-INTENT", armed)
        with crashed_process():
            converge(scenario.spool_id)

    assert cut.fired
    interrupted = episode_store.read(scenario.spool_id)
    assert interrupted.get("terminal_origin"), (
        "the record was written and the process lost, but the record names no terminal origin, "
        f"so a fresh reader cannot tell what was decided (status={interrupted.get('status')!r})"
    )
    assert obligations_of(interrupted), "a terminal was published with no discoverable owed duties"
    assert set(obligation_progress_of(interrupted).values()) != {"complete"}

    converge_to_fixed_point(scenario.spool_id)

    settled = episode_store.read(scenario.spool_id)
    assert set(obligation_progress_of(settled).values()) == {"complete"}


# --- obligation failure injection -------------------------------------------

OBLIGATION_FAILURES = ("control_receipts", "failed_shard_preservation", "natural_transcript")


def _break_effect(obligation: str, monkeypatch) -> _Cut:
    """Make one durable effect's carrier unwritable, the way a full disk would.

    This is not a crash cut.  The process stays alive, so the exception has to be
    absorbed into a pending obligation with a per-duty error; if it escapes the
    public API instead, the caller loses the outcome as well as the effect.
    """
    broken = _Cut(obligation)

    if obligation == "control_receipts":

        def failing_receipt(*args, **kwargs):
            broken.fired = True
            raise OSError("receipt carrier is unwritable")

        monkeypatch.setattr(spindle, "write_control_receipt", failing_receipt)
    elif obligation == "failed_shard_preservation":

        def failing_shard(*args, **kwargs):
            broken.fired = True
            raise OSError("shard preservation carrier is unwritable")

        monkeypatch.setattr(spindle, "_preserve_failed_spool_shard", failing_shard)
    elif obligation == "natural_transcript":
        # A file where the transcript directory has to be: the same refusal a
        # full or read-only filesystem gives, without pretending a crash.
        blocked = spindle.SPINDLE_DIR / "blocked-transcripts"
        blocked.write_text("not a directory")

        def blocked_transcript(spool_id):
            broken.fired = True
            return blocked / f"{spool_id}.transcript"

        monkeypatch.setattr(spindle, "_get_transcript_path", blocked_transcript)
    else:  # pragma: no cover - OBLIGATION_FAILURES fixes the kinds
        raise KeyError(obligation)

    return broken


@pytest.mark.parametrize("obligation", OBLIGATION_FAILURES)
def test_a_failed_obligation_stays_pending_and_retryable(episode_store, tmp_path, monkeypatch, obligation):
    """An exception during an effect never edits the outcome.

    It leaves ``terminal_with_obligations_pending`` with a per-duty error, and a
    later pass finishes the work without republishing anything.
    """
    scenario = _crash_scenario(episode_store, tmp_path, f"obligation-{obligation}")

    with monkeypatch.context() as armed:
        broken = _break_effect(obligation, armed)
        converge_without_escape(scenario.spool_id)
        assert broken.fired, f"the {obligation} effect was never attempted, so its failure proves nothing"
        after_failure = episode_store.read(scenario.spool_id)
        entry = obligations_of(after_failure).get(obligation)
        assert entry is not None, f"{obligation} was not left discoverable after its effect failed"
        assert entry["progress"] == "pending"
        assert entry["error"], f"{obligation} failed without recording why, so a retry cannot report it"

    converge_to_fixed_point(scenario.spool_id)

    settled = episode_store.read(scenario.spool_id)
    assert obligation_progress_of(settled).get(obligation) == "complete"
    assert settled["status"] == after_failure["status"], "finishing an owed duty rewrote the published outcome"
    assert settled["completed_at"] == after_failure["completed_at"]


def test_a_local_handle_error_is_local_and_never_blocks_durable_convergence(episode_store, tmp_path, convergence_api):
    """A process-local failure is reported, not raised, not durable, and retried.

    Phase 1: "Exceptions appear in ConvergenceResult local_errors and are retried
    on the next local observation."  A retry is only possible while the duty is
    still discoverable, so a reap that raised must leave the handle owned rather
    than dropping it: the child was never reaped and no wait was scheduled, and
    the map entry is the only record that the work is outstanding.

    Removing the handle in the test instead - as this used to - asks nothing of
    the next pass, because there is no duty left to perform.
    """
    scenario = _crash_scenario(episode_store, tmp_path, "local-handle")
    spindle._PROC_HANDLES[scenario.spool_id] = _RaisingHandle()

    result = convergence_api.converge_here(scenario.spool_id)

    assert result.local_errors, "a local handle duty failed and the result reported nothing"
    record = episode_store.read(scenario.spool_id)
    assert record.get("terminal_origin") == "natural_failure"
    assert set(obligation_progress_of(record).values()) == {"complete"}
    settled = episode_store.spool_path(scenario.spool_id).read_bytes()
    assert scenario.spool_id in spindle._PROC_HANDLES, (
        "the reap raised and the handle was dropped anyway, so nothing is left to retry "
        "and the child is never reaped by anyone"
    )

    # This process still owns a handle for the spool; the next terminal
    # observation has to attempt it again rather than treat the failure as
    # settled work.
    replacement = WatchdogHandle(0)
    spindle._PROC_HANDLES[scenario.spool_id] = replacement
    assert spindle._check_and_finalize_spool(scenario.spool_id) is True

    assert replacement.polls, "a later terminal observation never retried the local handle duty it owned"
    assert scenario.spool_id not in spindle._PROC_HANDLES, "a retried local duty left its handle registered"
    assert episode_store.spool_path(scenario.spool_id).read_bytes() == settled, (
        "a process-local retry wrote something durable; local duties carry no cross-process completion bit"
    )


# --- concurrency and stale preconditions ------------------------------------


def test_concurrent_reconcilers_publish_one_decision(episode_store, tmp_path, monkeypatch):
    """CUT-CAS: one generation/revision/request-set precondition wins.

    The overlap is forced at the capture-to-CAS boundary: every reconciler is
    held at the moment it is about to take the first production guard of its
    pass, and none is released until all four are standing there.  Nothing after
    that point can happen alone - no record has been rewritten yet, so all four
    enter the critical region against the same durable state and contend for the
    same precondition.

    The rendezvous deliberately holds no production lock.  Waiting *inside*
    capture would deadlock a converger that obeys the plan's capture protocol and
    holds the mailbox guard and the spool lock across its capture, so the test
    would hang on a correct implementation and pass only after a timeout on a
    wrong one; waiting at the public entry, before any of the work, proves only
    that four threads were started.  A barrier that never trips raises out of
    ``pool.map`` and fails the test - there is no branch here that absorbs it.

    Every claim is asserted before any further pass runs: a fixed-point repair
    afterwards would quietly rebuild whatever a losing writer had overwritten.
    """
    scenario = _crash_scenario(episode_store, tmp_path, "concurrent")
    workers = 4
    overlap = {"at_the_boundary": 0}

    def opened():
        # Runs once, in the thread that trips the barrier, while all four are
        # still waiting: this is the measurement, not an estimate of it.
        overlap["at_the_boundary"] = workers

    boundary = threading.Barrier(workers, action=opened, timeout=30)
    entered: set[int] = set()
    entered_lock = threading.Lock()
    real_spool_lock = spindle._spool_lock
    real_mailbox_guard = spindle.mailbox_guard

    def held_nothing_yet() -> bool:
        """Whether this thread is about to take its pass's outermost guard."""
        with entered_lock:
            first = threading.get_ident() not in entered
            entered.add(threading.get_ident())
        return first

    @contextmanager
    def rendezvous_then_lock(spool_id, blocking=True):
        if spool_id == scenario.spool_id and held_nothing_yet():
            boundary.wait()
        with real_spool_lock(spool_id, blocking) as acquired:
            yield acquired

    @contextmanager
    def rendezvous_then_guard(root, spool_id):
        if spool_id == scenario.spool_id and held_nothing_yet():
            boundary.wait()
        with real_mailbox_guard(root, spool_id):
            yield

    def reconcile(_):
        converge(scenario.spool_id)

    with monkeypatch.context() as armed:
        armed.setattr(spindle, "_spool_lock", rendezvous_then_lock)
        armed.setattr(spindle, "mailbox_guard", rendezvous_then_guard)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(reconcile, range(workers)))

    assert overlap["at_the_boundary"] == workers, (
        f"only {overlap['at_the_boundary']} of {workers} reconcilers ever reached the "
        f"capture-to-CAS boundary over {scenario.spool_id}; the calls never overlapped"
    )
    assert len(entered) == workers, (
        f"only {len(entered)} of {workers} concurrent reconcilers entered the critical region "
        f"over {scenario.spool_id}; the rest returned without attempting the decision"
    )
    record = episode_store.read(scenario.spool_id)
    episode = record[EPISODE_KEY]
    assert episode["phase"] == "released"
    assert episode["revision"] == scenario.revision, "more than one release was published for one cleanup proof"
    assert record["terminal_origin"] == "natural_failure"
    assert record["terminal_provenance"]["episode_revision"] == episode["revision"]
    completed_at = record["completed_at"]
    outcome = outcome_of(record)

    mailbox = episode_store.root / f"{scenario.spool_id}.control-mailbox"
    receipts = sorted(path.name for path in mailbox.glob("*.receipt"))
    published = iter_control_requests(episode_store.root, scenario.spool_id)
    expected_receipts = sorted(f"{request.request_id}.receipt" for request in published)
    assert receipts == expected_receipts, "the racing reconcilers did not settle each request exactly once"

    converge(scenario.spool_id)

    repaired = episode_store.read(scenario.spool_id)
    assert repaired["completed_at"] == completed_at, "a later pass rewrote a completion time the race had fixed"
    assert outcome_of(repaired) == outcome


def test_a_reconciler_that_loses_the_race_retries_instead_of_publishing_a_stale_view(
    episode_store, tmp_path, monkeypatch
):
    """A competing reconciler advances the episode between capture and commit.

    The loser holds a view that is now stale.  It must discard its decision and
    re-read, not publish a terminal derived from the superseded revision.
    """
    spool_id = "stale-precondition"
    episode = localize_identities(
        causal_episode("cleanup_proven", cleanup=cleanup_fact("natural_exit", provider_exit_code=0))
    )
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        owner_generation=GENERATION,
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    spindle._get_output_path(spool_id).write_text(json.dumps({"result": "ok", "session_id": "s"}))
    spindle._get_exit_path(spool_id).write_text("0\n")

    winner = {"done": False}
    real_acquire = spindle.acquire_ownership_lock

    def competing(path, **kwargs):
        lock = real_acquire(path, **kwargs)
        if not winner["done"]:
            winner["done"] = True
            record = episode_store.read(spool_id)
            advanced = dict(record[EPISODE_KEY])
            advanced["revision"] = advanced["revision"] + 1
            advanced["phase"] = "released"
            advanced["release"] = release_fact(advanced["lock"])
            advanced["phase_times"] = dict(advanced["phase_times"], released=RELEASED_AT)
            record[EPISODE_KEY] = advanced
            episode_store.spool_path(spool_id).write_text(json.dumps(record))
        return lock

    monkeypatch.setattr(spindle, "acquire_ownership_lock", competing)

    converge_to_fixed_point(spool_id)

    assert winner["done"], "the competing reconciler never ran, so nothing was raced"
    record = episode_store.read(spool_id)
    stored = record[EPISODE_KEY]
    assert stored["phase"] == "released"
    assert stored["revision"] == episode["revision"] + 1, "the loser republished a release over the winner's"
    assert record["terminal_provenance"]["episode_revision"] == stored["revision"]


def _live_owner_accepting_control(episode_store, spool_id: str) -> dict:
    """The one durable state real admission accepts: accepted phase, inode held.

    No timeout is recorded, so the supervisor's timeout path cannot publish a
    request behind the test's back.
    """
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        harness="claude-code",
        prompt="work",
        owner_generation=GENERATION,
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    episode_store.hold_lock(spool_id)
    return episode


def _owner_proves_cleanup_and_exits(episode_store, spool_id: str) -> None:
    """The trail an owner leaves when its provider exits and it then dies.

    The cleanup facts are stamped now, after the requests this test admitted:
    the owner really did observe the child exit after both were published, and
    an episode that claims otherwise is a history no producer could leave.  The
    phases the owner had already reached keep the times the frozen scenario gave
    them, so the whole history still moves on one clock.
    """
    clock = live_cleanup_clock()
    proven = localize_identities(
        causal_episode(
            "cleanup_proven",
            clock=clock,
            cleanup=cleanup_fact("natural_exit", provider_exit_code=0, clock=clock),
        )
    )
    episode_store.bind_lock(spool_id, proven)
    record = episode_store.read(spool_id)
    record[EPISODE_KEY] = proven
    episode_store.spool_path(spool_id).write_text(json.dumps(record))
    spindle._get_output_path(spool_id).write_text(NATURAL_STDOUT)
    spindle._get_exit_path(spool_id).write_text("0\n")
    episode_store.close()  # the owner is gone, so its ownership inode is free again


def test_a_request_admitted_after_a_capture_is_still_settled_eventually(episode_store):
    """CUT-ADMISSION's other side: the request set grew after a reducer read it.

    Both halves of this setup are load-bearing.

    The request is admitted through the real mailbox-then-spool path while the
    owner is still accepting control, because that is the only way one can
    exist.  Writing a request straight into the mailbox of a record convergence
    has already released forges an artifact admission refuses to create - Phase 1
    is explicit that a caller reaching admission after terminal intent publishes
    neither request nor receipt - so requiring a converger to settle it demands
    the settlement of something that can never be there.

    And it is admitted *between* convergence passes rather than from inside one,
    because publishing takes the mailbox guard that a converger obeying the
    capture protocol already holds while it reduces: injecting the publication
    mid-reduction would deadlock a correct implementation and pass only for a
    wrong one.
    """
    spool_id = "late-request"
    _live_owner_accepting_control(episode_store, spool_id)

    early, refusal = spindle._request_owner_stop(spool_id, "drop", "contract-test")
    assert early is not None, f"admission refused a request while the owner was live: {refusal}"

    converge(spool_id)
    captured = receipt_outcomes(episode_store, spool_id)

    assert set(captured) == {early.request_id}, "the first pass captured a request set nobody published"
    assert captured[early.request_id] is None, "a live owner's request was settled by a converger"

    late, refusal = spindle._request_owner_stop(spool_id, "cancel", "contract-test")

    assert late is not None, f"admission refused a request while the owner was still accepting control: {refusal}"
    assert late.request_id not in captured, "the late request was already in the first pass's captured set"

    # The provider then exited, the owner proved cleanup and died before proving
    # the release: the pass that captures *this* evidence is the one that has to
    # settle both requests.
    _owner_proves_cleanup_and_exits(episode_store, spool_id)

    converge_to_fixed_point(spool_id)

    settled = receipt_outcomes(episode_store, spool_id)
    assert sorted(settled) == sorted([early.request_id, late.request_id])
    unsettled = sorted(request_id for request_id, outcome in settled.items() if outcome is None)
    assert unsettled == [], f"requests left without receipts: {unsettled}"
    # Nothing ever won this episode: it terminated naturally, so every captured
    # request - early or late - takes the disposition Patrick fixed.
    assert set(settled.values()) == {NO_WINNER_RECEIPT}
    assert read_control_receipt(episode_store.root, spool_id, late.request_id) is not None
    assert episode_store.read(spool_id)["terminal_origin"] == "natural_success"
