"""The frozen Phase 2 oracle for owner-episode convergence.

Written before the implementation exists.  Each section states one obligation of
the Family A architecture Phase 1 selected (finding-20260813-6zyh section 5): the
immutable terminal outcome and a finite discoverable obligation set are committed
atomically in the spool record, then durable obligations are discharged
idempotently, while process-local handle duties are attempted by every terminal
observer that owns a handle and are never claimed durable or exactly-once.

Sections
--------
1. the phase / terminal-origin projection matrix
2. release proof and the deciding revision
3. evidence precedence
4. observer authority and the result vocabulary
5. mailbox serialization
6. reserved-phase liveness
7. provider exit evidence
8. atomic outcome plus obligation intent
9. consumers
10. legacy and mixed-version behaviour
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

import spindle
from spindle.namespace_owner import iter_control_requests, read_control_receipt, transition_owner_episode
from tests.owner_convergence_fixtures import (
    ACCEPTED_AT,
    ACCEPTED_CONTROL_AT,
    ACKNOWLEDGED_AT,
    BOUND_AT,
    CAPTURED_FIELDS,
    CHILD_EXIT_AT,
    CLASSIFICATIONS,
    CONVERGENCE_FORMAT,
    CONVERGENCE_KEY,
    CRASHED_AFTER_CLEANUP_AT,
    DURABLE_OBLIGATIONS,
    FOREIGN_NAMESPACE,
    GENERATION,
    LOCAL_DUTY,
    NEVER_STARTED_ERROR,
    NO_WINNER_RECEIPT,
    NULL,
    OBLIGATION_FIELDS,
    OBSERVER_FIELDS,
    OBSERVERS,
    ORIGINS,
    PROJECTION_FIELDS,
    PROVIDER_CUSTODY_AT,
    RELEASED_AT,
    REQUESTED_AT,
    RESERVED_AT,
    RESULT_FIELDS,
    SAME_NAMESPACE,
    SCENARIO_CLOCK,
    SIBLING_RECEIPT,
    SIDECAR_AT,
    TERMINAL_STATES,
    WatchdogHandle,
    acknowledgement_fact,
    assert_causal_episode,
    assert_obligation_payload,
    build_scenario,
    causal_episode,
    cleanup_fact,
    containment_fact,
    converge,
    converge_to_fixed_point,
    custody_fact,
    dead_process_fact,
    failure_fact,
    field_names,
    idempotency_key,
    live_process_fact,
    localize_identities,
    mailbox_dir,
    obligations_of,
    observing_as,
    outcome_of,
    preservation_of,
    provenance_of,
    publish_request,
    request_fact,
)
from tests.owner_episode_fixtures import EPISODE_KEY

BY_EVIDENCE = {row.evidence: row for row in ORIGINS}


# --- 1. the phase / terminal-origin projection matrix -----------------------


@pytest.mark.parametrize("contract", ORIGINS, ids=[row.evidence for row in ORIGINS])
def test_every_origin_fixture_describes_one_causal_history(episode_store, contract):
    """Every durable fact belongs to one ordered generation history."""
    scenario = build_scenario(episode_store, contract)
    episode = episode_store.episode(scenario.spool_id)
    phase_times = {name: datetime.fromisoformat(value) for name, value in episode["phase_times"].items()}
    reserved_at = phase_times["reserved"]

    ordered_phases = [
        phase_times[name]
        for name in ("reserved", "lock_bound", "accepted", "cleanup_proven", "released")
        if name in phase_times
    ]
    assert ordered_phases == sorted(ordered_phases)

    fact_times = []
    for fact, field in (
        ("provider_custody", "published_at"),
        ("acknowledgement", "acknowledged_at"),
        ("failure", "observed_at"),
        ("containment", "observed_at"),
        ("cleanup", "child_exit_observed_at"),
        ("release", "released_at"),
    ):
        value = (episode.get(fact) or {}).get(field)
        if value is not None:
            fact_times.append((f"{fact}.{field}", datetime.fromisoformat(value)))

    for name, observed_at in fact_times:
        assert observed_at >= reserved_at, f"{name} predates this generation's reservation"

    requests = tuple(iter_control_requests(episode_store.root, scenario.spool_id))
    request_times = [datetime.fromisoformat(request.requested_at) for request in requests]
    acknowledgement = (episode.get("acknowledgement") or {}).get("acknowledged_at")
    if acknowledgement is not None and request_times:
        assert max(request_times) <= datetime.fromisoformat(acknowledgement)

    cleanup_at = (episode.get("cleanup") or {}).get("child_exit_observed_at")
    release_at = (episode.get("release") or {}).get("released_at")
    if cleanup_at is not None and release_at is not None:
        assert datetime.fromisoformat(cleanup_at) <= datetime.fromisoformat(release_at)
    if release_at is not None:
        released_at = datetime.fromisoformat(release_at)
        assert all(observed_at <= released_at for _name, observed_at in fact_times)


def test_the_causal_builder_pulls_every_inherited_fact_onto_the_scenario_clock():
    """The matrix rows are not the only histories, so the rule cannot be theirs.

    A direct construction inherits its release, custody and phase times from the
    pure-transition fixture, which stamps them on its own day.  A scenario that
    then writes one cleanup fact on the scenario clock has built a history no
    producer could leave, and a converger may refuse it as corrupt - which is
    how a row passes for a reason that has nothing to do with what it asserts.
    Every Phase 2 episode goes through this builder, so the rule is stated once,
    here, over the facts that used to be inherited.
    """
    episode = causal_episode("released", cleanup=cleanup_fact("natural_exit", provider_exit_code=0))

    stamps = dict(episode["phase_times"])
    stamps.update(
        {
            "provider_custody": episode["provider_custody"]["published_at"],
            "cleanup": episode["cleanup"]["child_exit_observed_at"],
            "release": episode["release"]["released_at"],
        }
    )
    off_clock = {name: value for name, value in stamps.items() if value not in set(SCENARIO_CLOCK.values())}
    assert off_clock == {}, f"a directly built episode kept facts on another clock: {off_clock}"
    assert episode["provider_custody"]["published_at"] == PROVIDER_CUSTODY_AT
    assert episode["cleanup"]["child_exit_observed_at"] == CHILD_EXIT_AT
    assert episode["release"]["released_at"] == RELEASED_AT
    assert episode["phase_times"]["released"] == RELEASED_AT


#: name, the facts a caller states, and what makes the resulting history
#: impossible.  One row per window the transition table fixes, because a rule
#: nothing exercises is a rule the next repair can delete without noticing.
IMPOSSIBLE_HISTORIES = (
    (
        "a_fact_on_another_clock",
        {
            "cleanup": dict(
                cleanup_fact("natural_exit", provider_exit_code=0),
                child_exit_observed_at="2026-08-11T00:00:05+00:00",
            )
        },
        "no instant of this scenario",
    ),
    (
        "a_cleanup_after_the_release",
        {"cleanup": dict(cleanup_fact("natural_exit", provider_exit_code=0), child_exit_observed_at=SIDECAR_AT)},
        "released ownership before it observed its child exit",
    ),
    (
        # The owner acknowledges a request while it is still in the accepted
        # phase, and only then observes the exit that request produced.  An
        # acknowledgement stamped after the child exit is answered by a provider
        # that was already gone - and it is inside the release, so no other rule
        # would have refused it.
        "an_acknowledgement_after_the_child_exit",
        {
            "winning_request": request_fact("cancel", "request-after-child-exit"),
            "acknowledgement": dict(acknowledgement_fact(), acknowledged_at=RELEASED_AT),
        },
        "observed its child exit before it acknowledged the request that stopped it",
    ),
    (
        "an_acknowledgement_after_the_control_commit",
        {
            "winning_request": request_fact("cancel", "request-after-control-commit"),
            "acknowledgement": dict(acknowledgement_fact(), acknowledged_at=CHILD_EXIT_AT),
        },
        "committed accepted control before recording its acknowledgement",
    ),
    (
        # The fact is captured after ownership is bound and just before the
        # lock-bound -> accepted transition commits it.
        "a_custody_taken_before_ownership_was_bound",
        {"provider_custody": dict(custody_fact(), published_at=RESERVED_AT)},
        "published provider custody before binding ownership",
    ),
    (
        "a_custody_taken_after_the_child_exit",
        {"provider_custody": dict(custody_fact(), published_at=CRASHED_AFTER_CLEANUP_AT)},
        "took custody of a provider that had already exited",
    ),
    (
        "a_custody_fact_after_the_provider_acceptance_commit",
        {"provider_custody": dict(custody_fact(), published_at=REQUESTED_AT)},
        "committed provider acceptance before recording its custody",
    ),
    (
        "an_acknowledgement_before_provider_custody",
        {
            "winning_request": request_fact("cancel", "request-before-custody"),
            "acknowledgement": dict(acknowledgement_fact(), acknowledged_at=BOUND_AT),
        },
        "acknowledged control before taking provider custody",
    ),
    (
        # Custody has a tighter child-exit deadline than the general release
        # deadline, so this row deliberately exercises that tighter rule.
        "a_custody_published_after_the_release",
        {"provider_custody": dict(custody_fact(), published_at=SIDECAR_AT)},
        "took custody of a provider that had already exited",
    ),
    (
        "a_containment_fact_published_after_the_release",
        {
            "path": "before_acceptance",
            "containment": dict(containment_fact(), observed_at=SIDECAR_AT),
            "cleanup": cleanup_fact("watchdog_contained"),
        },
        "carries facts published after its release",
    ),
    (
        "a_failure_fact_published_after_the_release",
        {"failure": failure_fact("owner_crash", "late mirror", at="sidecar")},
        "carries facts published after its release",
    ),
)


@pytest.mark.parametrize(
    ("name", "facts", "complaint"),
    IMPOSSIBLE_HISTORIES,
    ids=[row[0] for row in IMPOSSIBLE_HISTORIES],
)
def test_the_causal_builder_refuses_a_history_no_producer_could_leave(name, facts, complaint):
    """The invariant has teeth on stated facts, not only on inherited ones.

    A fact the caller states is deliberate, so the builder leaves it exactly as
    stated - and then has to prove the whole history it belongs to.  Without
    this, the normalisation would be silently satisfied by facts nobody
    normalises, which is the failure mode the previous two repairs had.
    """
    with pytest.raises(AssertionError, match=complaint):
        causal_episode("released", **facts)


@pytest.mark.parametrize(
    ("fact", "complaint"),
    (
        (
            {
                "winning_request": request_fact("cancel", "request-before-acceptance"),
                "acknowledgement": acknowledgement_fact(),
            },
            "without ever accepting control",
        ),
        ({"provider_custody": custody_fact()}, "without ever accepting the provider"),
    ),
    ids=("acknowledgement_without_acceptance", "provider_custody_without_acceptance"),
)
def test_the_causal_builder_refuses_acceptance_facts_before_the_accepted_phase(fact, complaint):
    """An acceptance fact cannot exist merely because its timestamp is late enough."""
    with pytest.raises(AssertionError, match=complaint):
        causal_episode("lock_bound", **fact)


@pytest.mark.parametrize(
    ("facts", "complaint"),
    (
        ({"acknowledgement": acknowledgement_fact()}, "acknowledgement without the winning request"),
        (
            {"winning_request": request_fact("cancel", "request-without-ack")},
            "winning request without the acknowledgement",
        ),
    ),
    ids=("acknowledgement_without_winner", "winner_without_acknowledgement"),
)
def test_the_causal_builder_refuses_half_of_the_atomic_control_decision(facts, complaint):
    """The accepted -> accepted transition publishes these two facts together."""
    with pytest.raises(AssertionError, match=complaint):
        causal_episode("accepted", **facts)


def test_the_causal_builder_accepts_the_control_history_a_producer_really_leaves():
    """The windows must not be tighter than the run they describe.

    An invariant that refuses a real history gets repaired by deleting the rule,
    so each window needs the history it is supposed to admit stated beside the
    ones it refuses.  This is what an accepted-control row leaves behind: custody
    taken as the provider is accepted, the request acknowledged inside that same
    phase, the child exit observed after it, and the release proved last.
    """
    episode = causal_episode(
        "released",
        provider_custody=custody_fact(),
        winning_request=request_fact("cancel", "request-valid-history"),
        acknowledgement=acknowledgement_fact(),
        cleanup=cleanup_fact("stopped", provider_exit_code=0),
    )

    assert episode["provider_custody"]["published_at"] == PROVIDER_CUSTODY_AT
    assert episode["acknowledgement"]["acknowledged_at"] == ACKNOWLEDGED_AT
    assert episode["phase_times"]["accepted"] == ACCEPTED_CONTROL_AT
    assert episode["cleanup"]["child_exit_observed_at"] == CHILD_EXIT_AT
    assert episode["release"]["released_at"] == RELEASED_AT


def test_the_causal_invariant_accepts_the_episode_the_transition_writer_really_builds(episode_store, monkeypatch):
    """Fact stamps precede the commit stamp that publishes them atomically."""
    spool_id = "producer-clock"
    episode_store.write(spool_id, status="pending")
    commit_times = iter((RESERVED_AT, RESERVED_AT, BOUND_AT, ACCEPTED_AT, ACCEPTED_CONTROL_AT))
    monkeypatch.setattr("spindle.namespace_owner._utc_now", lambda: next(commit_times))

    transitions = (
        (
            "launcher",
            "reserved",
            None,
            {
                "starter": {
                    "pid": 1,
                    "birth_token": "1",
                    "namespace": {"status": "supported", "device": 7, "inode": 11},
                }
            },
        ),
        (
            "launcher",
            "reserved",
            1,
            {
                "watchdog": {
                    "pid": 2,
                    "birth_token": "2",
                    "namespace": {"status": "supported", "device": 7, "inode": 11},
                }
            },
        ),
        (
            "owner",
            "lock_bound",
            2,
            {
                "owner": {
                    "pid": 3,
                    "birth_token": "3",
                    "namespace": {"status": "supported", "device": 7, "inode": 11},
                },
                "lock": {"device": 4, "inode": 5},
            },
        ),
        (
            "owner",
            "accepted",
            3,
            {
                "provider": {
                    "pid": 6,
                    "pgid": 6,
                    "birth_token": "6",
                    "namespace": {"status": "supported", "device": 7, "inode": 11},
                },
                "provider_custody": custody_fact(),
            },
        ),
        (
            "owner",
            "accepted",
            4,
            {
                "winning_request": request_fact("cancel", "producer-request"),
                "acknowledgement": acknowledgement_fact(),
            },
        ),
    )
    result = None
    for actor, destination, expected_revision, facts in transitions:
        result = transition_owner_episode(
            episode_store.root,
            spool_id,
            actor=actor,
            destination=destination,
            generation=1,
            expected_revision=expected_revision,
            facts=facts,
        )
        assert result.accepted, result.rejection

    episode = result.episode
    assert episode["provider_custody"]["published_at"] < episode["phase_times"]["accepted"]
    assert episode["acknowledgement"]["acknowledged_at"] < episode["phase_times"]["accepted"]
    assert_causal_episode(episode, subject="the production transition sequence")


def test_the_causal_invariant_reads_a_stored_episode_the_same_way(episode_store):
    """The invariant is one statement, whether the episode is built or read back.

    Every scenario in this suite - matrix row, direct construction, crash setup
    or defect regression - is proved causal at construction.  This says the same
    proof holds over the record a converger actually reads, so the two can never
    be separately true.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_timeout"])

    assert_causal_episode(episode_store.episode(scenario.spool_id), subject=scenario.spool_id)


def test_the_causal_invariant_refuses_a_stored_episode_with_out_of_order_phases():
    """Stored input is not trusted merely because the central builder is ordered."""
    episode = causal_episode("released")
    episode["phase_times"]["lock_bound"] = ACCEPTED_AT
    episode["phase_times"]["accepted"] = BOUND_AT

    with pytest.raises(AssertionError, match="reaches its phases out of order"):
        assert_causal_episode(episode, subject="an out-of-order stored episode")


def test_the_causal_invariant_refuses_a_stored_fact_before_its_generation():
    """An on-clock fact can still belong to before the stored reservation."""
    episode = causal_episode("released", failure=failure_fact("owner_crash", "stale fact", at="reserved"))
    episode["phase_times"]["reserved"] = BOUND_AT

    with pytest.raises(AssertionError, match="predates this generation's reservation"):
        assert_causal_episode(episode, subject="a stored episode with a predecessor fact")


@pytest.mark.parametrize("contract", ORIGINS, ids=[row.evidence for row in ORIGINS])
def test_every_terminal_origin_projects_its_complete_public_outcome(episode_store, contract):
    """One durable evidence set, one closed public projection.

    The matrix is the whole point of Phase 1 section 2: identical origins must
    stop producing different field sets, so correctness becomes falsifiable.
    Status alone is not the contract - error, error_kind, result, exit_code and
    every lifecycle field are named per origin.
    """
    scenario = build_scenario(episode_store, contract)

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert outcome_of(record) == scenario.expected_outcome()


@pytest.mark.parametrize("contract", ORIGINS, ids=[row.evidence for row in ORIGINS])
def test_every_terminal_origin_records_its_provenance(episode_store, contract):
    """Failure origin stays separate from normalized outcome.

    Two timeouts are not the same event: a deadline that expired before the
    provider started and an accepted timeout request normalize to different
    origins. Provenance is what lets a consumer tell them apart, and what stops
    the collapse into "generic spawn timeout" the plan forbids.
    """
    scenario = build_scenario(episode_store, contract)

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert provenance_of(record) == scenario.expected_provenance()


@pytest.mark.parametrize("contract", ORIGINS, ids=[row.evidence for row in ORIGINS])
def test_every_terminal_origin_preserves_or_clears_inherited_facts(episode_store, contract):
    """tags survive; cost and background tasks come only from a natural proposal."""
    scenario = build_scenario(episode_store, contract)

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert preservation_of(record) == scenario.expected_preservation()


@pytest.mark.parametrize("contract", ORIGINS, ids=[row.evidence for row in ORIGINS])
def test_terminal_re_entry_preserves_every_outcome_field_byte_for_byte(episode_store, contract):
    """Re-entry may only advance obligations, never restate the outcome."""
    scenario = build_scenario(episode_store, contract)
    converge_to_fixed_point(scenario.spool_id)
    settled = episode_store.spool_path(scenario.spool_id).read_bytes()

    converge(scenario.spool_id)

    assert episode_store.spool_path(scenario.spool_id).read_bytes() == settled


def test_the_two_deadline_windows_stay_distinguishable(episode_store):
    """Same public timeout, different provenance.

    A deadline that expired before lock binding and one that expired after it
    are the same user-visible timeout but different recovery stories: the second
    proves ownership was held and cleanly released.
    """
    before = build_scenario(episode_store, BY_EVIDENCE["deadline_before_binding"])
    after = build_scenario(episode_store, BY_EVIDENCE["deadline_after_binding"])

    converge(before.spool_id)
    converge(after.spool_id)

    first = episode_store.read(before.spool_id)
    second = episode_store.read(after.spool_id)
    assert first["status"] == second["status"] == "timeout"
    assert first["error_kind"] == second["error_kind"] == "deadline_expired_before_provider_start"
    assert first["terminal_provenance"]["episode_phase"] == "aborted"
    assert second["terminal_provenance"]["episode_phase"] == "released"


# --- 2. release proof and the deciding revision -----------------------------


def test_release_is_proved_from_cleanup_proven_before_any_terminal_is_published(episode_store):
    """The reconciler alone persists release, and only with the exact inode.

    Decision 30: cleanup_proven is retireable only after the recorded inode is
    observed released, and the public terminal follows that transition - never
    precedes it.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], before_release=True)
    episode = episode_store.episode(scenario.spool_id)

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    stored = record[EPISODE_KEY]
    assert stored["phase"] == "released"
    assert stored["revision"] == episode["revision"] + 1
    assert (stored["release"]["device"], stored["release"]["inode"]) == (
        episode["lock"]["device"],
        episode["lock"]["inode"],
    )
    assert record["terminal_provenance"]["episode_revision"] == stored["revision"], (
        "provenance must name the revision the decision was taken from"
    )


def test_a_held_ownership_inode_refuses_release_and_stays_active(episode_store):
    """A live owner is not terminalizable however complete its other evidence is."""
    spool_id = "release-refused"
    episode = localize_identities(causal_episode("cleanup_proven"))
    episode_store.bind_lock(spool_id, episode)
    record = episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )
    episode_store.hold_lock(spool_id)

    assert spindle._reconcile_spool_ownership(record).state == "active"

    converge(spool_id)

    assert episode_store.read(spool_id)[EPISODE_KEY]["phase"] == "cleanup_proven"
    assert episode_store.read(spool_id)["status"] == "running"


# --- 3. evidence precedence -------------------------------------------------


def test_a_stale_generation_sidecar_contributes_no_evidence(episode_store):
    """Generation-scoped settlement, Decision 24.

    A leftover ``.owner-exit`` from a previous generation must not decide the
    current one, in either direction.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])
    (episode_store.root / f"{scenario.spool_id}.owner-exit").write_text(
        json.dumps({"owner_generation": GENERATION - 1, "owner_crashed": True})
    )

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert outcome_of(record) == scenario.expected_outcome()


def test_flat_mirrors_cannot_override_the_episode(episode_store):
    """Decision 29: flat fields and sidecars are mirrors, never authority."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])
    record = episode_store.read(scenario.spool_id)
    record.update(
        owner_generation=GENERATION - 1,
        pid=999999999,
        provider_pid=999999998,
        lifecycle={"ownership_state": "held", "transport_state": "connected", "public_stop_state": "stopping"},
    )
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    converge(scenario.spool_id)

    settled = episode_store.read(scenario.spool_id)
    assert outcome_of(settled) == scenario.expected_outcome()


CONTRADICTORY_CARRIERS = ("exit_file", "owner_exit")


@pytest.mark.parametrize("carrier", CONTRADICTORY_CARRIERS)
def test_contradictory_generation_matched_exit_carriers_are_unhealthy(episode_store, carrier):
    """Contradiction is not a licence to pick the lower-precedence carrier.

    ``episode.cleanup.provider_exit_code`` says the provider exited 0 while a
    generation-matched second carrier says otherwise.  One of them is wrong and
    the store cannot tell which, so no terminal may be published and an operator
    must repair it.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])
    if carrier == "exit_file":
        spindle._get_exit_path(scenario.spool_id).write_text("3\n")
    else:
        (episode_store.root / f"{scenario.spool_id}.owner-exit").write_text(
            json.dumps(
                {
                    "owner_generation": GENERATION,
                    "provider_exit_code": 3,
                    "provider_reaped": True,
                    "cleanup_outcome": "natural_exit",
                }
            )
        )
    record = episode_store.read(scenario.spool_id)

    assert spindle._reconcile_spool_ownership(record).state == "store_unhealthy"

    converge(scenario.spool_id)

    settled = episode_store.read(scenario.spool_id)
    assert settled["status"] == "running", "a contradictory exit carrier still produced a terminal"


def test_a_malformed_episode_is_unhealthy_rather_than_weakly_interpreted(episode_store):
    """An impossible phase/evidence combination refuses every mutation."""
    spool_id = "unhealthy-episode"
    episode = localize_identities(causal_episode("released"))
    episode.pop("cleanup")
    episode_store.bind_lock(spool_id, episode)
    record = episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        lifecycle={"ownership_state": "held", "transport_state": "connected"},
    )

    assert spindle._reconcile_spool_ownership(record).state == "store_unhealthy"

    converge(spool_id)

    assert episode_store.read(spool_id)["status"] == "running"


# --- 4. observer authority and the result vocabulary ------------------------


def test_the_convergence_module_publishes_every_name_phase_1_selected(convergence_api):
    """The primitive boundary is a named module with a named result vocabulary.

    Phase 1 section 6 selects ``converge_owner_episode(spool_id, observer)`` plus
    ConvergenceResult, Obligation, ObserverIdentity, CapturedOwnerEpisodeEvidence
    and TerminalProjection.  Resolving each one here means a later rename fails
    with a precise message instead of leaving the contract quietly asserting
    nothing about a type nobody built.  The capture and the projection are not
    decoration: the capture is the single frozen view every decision is taken
    from, and the projection is the protected field set the closed-writer rule
    keys on, so a module that omits them has not drawn the boundary Phase 1
    selected.
    """
    assert set(RESULT_FIELDS) <= set(field_names(convergence_api.ConvergenceResult))
    assert set(OBLIGATION_FIELDS) <= set(field_names(convergence_api.Obligation))
    assert set(OBSERVER_FIELDS) <= set(field_names(convergence_api.ObserverIdentity))
    assert set(CAPTURED_FIELDS) <= set(field_names(convergence_api.CapturedOwnerEpisodeEvidence))
    assert set(PROJECTION_FIELDS) <= set(field_names(convergence_api.TerminalProjection))

    observer = convergence_api.local_observer()

    assert observer.pid == os.getpid()
    assert observer.namespace is not None, "an observer without a namespace coordinate has no authority to check"


def test_convergence_takes_an_explicit_observer_rather_than_deriving_one(episode_store, convergence_api):
    """Authority belongs to the caller, so it is an argument, not an assumption.

    The projection it returns is the same one it published: a TerminalProjection
    that disagrees with the record would let a consumer act on an outcome the
    store never committed.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)

    result = convergence_api.converge(scenario.spool_id, convergence_api.local_observer())

    assert result.classification in CLASSIFICATIONS
    assert result.terminal_state in TERMINAL_STATES
    assert isinstance(result.projection, convergence_api.TerminalProjection), (
        f"a terminalizable convergence returned {result.projection!r} rather than a TerminalProjection"
    )
    record = episode_store.read(scenario.spool_id)
    assert result.projection.status == record["status"] == "complete"
    assert result.projection.terminal_origin == record["terminal_origin"] == "natural_success"
    assert result.projection.completed_at == record["completed_at"]


#: classification, whether a same-namespace observer may mutate, and the record
#: the durable evidence is built from.  Together these are the five public
#: action states the plan freezes.
RESULT_STATES = (
    ("active", "accepted_live"),
    ("stopping", "accepted_live_stopping"),
    ("terminalizable", "released_natural"),
    ("unverifiable", "cleanup_proven_foreign"),
    ("store_unhealthy", "released_without_cleanup"),
)


def _result_case(episode_store, case: str):
    """Build one durable state and say which observer reads it."""
    spool_id = f"result-{case}"
    if case in {"accepted_live", "accepted_live_stopping"}:
        episode = localize_identities(causal_episode("accepted"))
        episode_store.bind_lock(spool_id, episode)
        lifecycle = {"ownership_state": "held", "transport_state": "connected"}
        if case == "accepted_live_stopping":
            lifecycle["public_stop_state"] = "stopping"
        episode_store.write(
            spool_id,
            status="running",
            episode=episode,
            created_at="2026-08-01T00:00:00",
            timeout=17,
            prompt="work",
            owner_generation=GENERATION,
            lifecycle=lifecycle,
        )
        episode_store.hold_lock(spool_id)
        if case == "accepted_live_stopping":
            publish_request(episode_store, spool_id, "cancel", GENERATION)
        return spool_id, SAME_NAMESPACE
    if case == "released_natural":
        scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], spool_id=spool_id, with_handle=False)
        return scenario.spool_id, SAME_NAMESPACE
    if case == "cleanup_proven_foreign":
        # Release is *not* yet proven durably: proving it needs the exact inode,
        # which is the relationship a foreign observer cannot safely establish.
        scenario = build_scenario(
            episode_store, BY_EVIDENCE["natural"], spool_id=spool_id, with_handle=False, before_release=True
        )
        return scenario.spool_id, FOREIGN_NAMESPACE
    if case == "released_without_cleanup":
        episode = localize_identities(causal_episode("released"))
        episode.pop("cleanup")
        episode_store.bind_lock(spool_id, episode)
        episode_store.write(
            spool_id,
            status="running",
            episode=episode,
            created_at="2026-08-01T00:00:00",
            prompt="work",
            lifecycle={"ownership_state": "held", "transport_state": "connected"},
        )
        return spool_id, SAME_NAMESPACE
    raise KeyError(case)  # pragma: no cover - RESULT_STATES fixes the cases


@pytest.mark.parametrize(
    ("classification", "case"),
    RESULT_STATES,
    ids=[row[0] for row in RESULT_STATES],
)
def test_the_result_reports_each_public_action_state_and_its_permission(
    episode_store, monkeypatch, convergence_api, classification, case
):
    """Five states, and a permission that is a separate answer from the state.

    Only ``terminalizable`` under an authorised observer may mutate.  Everything
    else has to say why it refused, because a refusal without a reason is
    indistinguishable from a converger that silently did nothing.
    """
    spool_id, observer = _result_case(episode_store, case)
    before = episode_store.spool_path(spool_id).read_bytes()

    with observing_as(observer, monkeypatch):
        result = convergence_api.converge_here(spool_id)

    assert result.classification == classification
    assert result.reason, "every classification names the evidence it came from"
    if classification == "terminalizable":
        assert result.may_mutate is True
        assert result.refusal_reason is None
    else:
        assert result.may_mutate is False, f"a {classification} record was reported mutable"
        assert result.refusal_reason, f"a {classification} record refused without saying why"
        assert result.terminal_state == "none"
        assert episode_store.spool_path(spool_id).read_bytes() == before


@pytest.mark.parametrize("observer", OBSERVERS)
def test_one_snapshot_keeps_its_classification_but_not_its_authority(
    episode_store, monkeypatch, convergence_api, observer
):
    """Decision 28: classification and permission are separate results.

    The same released cleanup episode is durably terminalizable for anyone: its
    cleanup and its release are both already proven in the record.  Only a
    same-namespace observer may act on it; a foreign observer must refuse
    without mutating, retiring, or freeing capacity.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)
    record = episode_store.read(scenario.spool_id)
    before = episode_store.spool_path(scenario.spool_id).read_bytes()

    with observing_as(observer, monkeypatch):
        lock, liveness = spindle._owner_episode_observation(record)
        classification = spindle.classify_owner_episode(record, lock, liveness)
        reconciliation = spindle._reconcile_spool_ownership(record)
        result = convergence_api.converge_here(scenario.spool_id)
        capacity = spindle._count_running()

    assert classification.state == "retireable", "the durable meaning changed with the observer"
    assert classification.reason == "cleanup_and_release_proven"
    assert result.classification == "terminalizable", "a proven terminal stopped being terminalizable for a stranger"
    if observer == SAME_NAMESPACE:
        assert reconciliation.state == "terminalizable"
        assert result.may_mutate is True
        assert result.refusal_reason is None
        assert capacity == 0
        assert episode_store.read(scenario.spool_id)["status"] == "complete"
    else:
        # The consumer-facing mapping stays deliberately conservative: capacity
        # is never freed on a stranger's word, even though the durable meaning
        # is settled.
        assert reconciliation.state == "unverifiable"
        assert result.may_mutate is False
        assert result.refusal_reason, "a foreign observer refused without naming the namespace"
        assert capacity == 1, "a foreign observer freed capacity it cannot prove"
        assert episode_store.spool_path(scenario.spool_id).read_bytes() == before


def test_retirement_deletes_a_converged_record_here_and_refuses_it_from_outside(episode_store, monkeypatch):
    """Retention consumes authority, not just terminality.

    Both records are the same fully converged terminal.  The foreign observer
    must leave its artifacts alone, and the comparison is only meaningful
    because the same sweep really does delete the local one.
    """
    foreign = build_scenario(episode_store, BY_EVIDENCE["natural"], spool_id="retire-foreign", with_handle=False)
    local = build_scenario(episode_store, BY_EVIDENCE["natural"], spool_id="retire-local", with_handle=False)
    converge_to_fixed_point(foreign.spool_id)
    converge_to_fixed_point(local.spool_id)
    assert episode_store.read(foreign.spool_id)["status"] == "complete", "the retirement case never became terminal"

    with observing_as(FOREIGN_NAMESPACE, monkeypatch):
        spindle._cleanup_old_spools()

    assert episode_store.spool_path(foreign.spool_id).exists(), "a foreign observer retired a record it cannot prove"
    assert episode_store.lock_path(foreign.spool_id).exists()

    spindle._cleanup_old_spools()

    assert not episode_store.spool_path(local.spool_id).exists(), "the same sweep never retires anything locally"
    assert not episode_store.spool_path(foreign.spool_id).exists()


# --- 5. mailbox serialization -----------------------------------------------


def test_a_request_published_before_capture_is_settled(episode_store):
    """Every captured current-generation request leaves with a receipt.

    The winner's own receipt was written by the owner that accepted it and only
    ever advances; the sibling is the converger's owed duty.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_cancel"], extra_request="drop")
    sibling = scenario.extras["extra_request_id"]

    converge(scenario.spool_id)

    winner = read_control_receipt(episode_store.root, scenario.spool_id, scenario.request_id)
    loser = read_control_receipt(episode_store.root, scenario.spool_id, sibling)
    assert winner is not None, "the accepted request lost the receipt its owner wrote"
    assert winner.owner_acknowledged_at is not None
    assert winner.cleanup_outcome == "cleaned", "convergence rewrote a settled winning receipt"
    assert loser is not None, "a captured sibling request was left unsettled"
    assert loser.cleanup_outcome == SIBLING_RECEIPT
    assert loser.owner_acknowledged_at is None


def test_the_no_winner_case_uses_rejected_terminal_and_keeps_the_natural_outcome(episode_store):
    """finding-20260813-xt37, the single product decision Phase 1 left open."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], extra_request="timeout")
    request_id = scenario.extras["extra_request_id"]

    converge(scenario.spool_id)

    receipt = read_control_receipt(episode_store.root, scenario.spool_id, request_id)
    assert receipt is not None
    assert receipt.cleanup_outcome == NO_WINNER_RECEIPT
    record = episode_store.read(scenario.spool_id)
    assert record["status"] == "complete"
    assert record["terminal_origin"] == "natural_success"


@pytest.mark.parametrize("kind", ("cancel", "timeout", "drop"))
def test_admission_racing_release_refuses_without_a_request_or_a_receipt(episode_store, kind):
    """Decision 32: a refusal creates no request and no receipt.

    The episode has already proved cleanup, so no owner can accept anything.
    Admission must publish nothing rather than leave a request nobody will
    settle.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)

    request, error = spindle._request_owner_stop(scenario.spool_id, kind, "contract-test")

    assert request is None
    assert error
    assert not list(mailbox_dir(episode_store, scenario.spool_id).glob("*.request"))
    assert not list(mailbox_dir(episode_store, scenario.spool_id).glob("*.receipt"))


def test_admission_takes_the_mailbox_guard_before_the_spool_lock(episode_store, guard_order, tmp_path):
    """The one lock order capture and admission share (plan, capture protocol)."""
    spool_id = "admission-order"
    episode = localize_identities(causal_episode("accepted"))
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        created_at="2026-08-01T00:00:00",
        timeout=1,
        prompt="work",
    )
    episode_store.hold_lock(spool_id)

    spindle._request_owner_stop(spool_id, "cancel", "contract-test")

    assert guard_order.entries[:2] == ["mailbox", "spool"]


# --- 6. reserved-phase liveness ---------------------------------------------

RESERVED_LIVENESS = (
    ("starter_live_no_watchdog", "live", None, 1, "active"),
    ("watchdog_live", "dead", "live", 2, "active"),
    ("watchdog_dead_starter_live", "live", "dead", 2, "terminalizable"),
    ("watchdog_dead_starter_dead", "dead", "dead", 2, "terminalizable"),
    ("starter_dead_no_watchdog", "dead", None, 1, "terminalizable"),
)


@pytest.mark.parametrize(
    ("name", "starter", "watchdog", "revision", "expected"),
    RESERVED_LIVENESS,
    ids=[row[0] for row in RESERVED_LIVENESS],
)
def test_reserved_phase_liveness_carriers(episode_store, name, starter, watchdog, revision, expected):
    """After watchdog publication the starter is no longer a positive carrier.

    Before publication the starter is the only carrier there is.  After it, the
    watchdog owns the pre-bind window and a dead watchdog ends the reservation
    however alive the short-lived starter still looks.  Each role carries its
    own PID and birth token, so a role confusion cannot pass unnoticed.
    """
    spool_id = f"reserved-{name}"
    facts = {"starter": live_process_fact() if starter == "live" else dead_process_fact("starter")}
    if watchdog is not None:
        facts["watchdog"] = live_process_fact() if watchdog == "live" else dead_process_fact("watchdog")
    assert facts["starter"]["pid"] != facts.get("watchdog", {}).get("pid"), "roles must not share an identity"
    episode = causal_episode(
        "reserved",
        path="watchdog_published" if watchdog is not None else "before_watchdog",
        **facts,
    )
    record = episode_store.write(spool_id, status="pending", episode=episode, created_at="2026-08-01T00:00:00")

    assert spindle._reconcile_spool_ownership(record).state == expected


def test_a_stale_dead_reservation_keeps_its_own_failure_origin(episode_store):
    """A never-started reservation is not a generic spawn timeout."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["stale_dead_reservation"])

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record["error"] == NEVER_STARTED_ERROR
    assert record["error_kind"] == "stale_dead_reservation"
    assert record["terminal_origin"] == "stale_dead_reservation"


# --- 7. provider exit evidence ----------------------------------------------

#: Every row here is an episode that really accepted provider custody, so a
#: provider exit exists and the only question is which carrier proves it.  The
#: no-provider case is not a row: an episode holding provider and custody facts
#: contradicts "no provider started", and the row passed on that contradiction
#: whenever nothing was published at all.  It has its own test below.
EXIT_CASES = (
    ("episode_cleanup_wins", {"cleanup": 0, "sidecar": None, "exit_file": None}, 0, "episode_cleanup"),
    ("owner_exit_when_no_cleanup_code", {"cleanup": None, "sidecar": 7, "exit_file": None}, 7, "owner_exit"),
    ("exit_file_last", {"cleanup": None, "sidecar": None, "exit_file": 5}, 5, "exit_file"),
    ("raw_signal_normalises", {"cleanup": -9, "sidecar": None, "exit_file": None}, 137, "episode_cleanup"),
)


@pytest.mark.parametrize(
    ("name", "carriers", "expected", "expected_carrier"),
    EXIT_CASES,
    ids=[row[0] for row in EXIT_CASES],
)
def test_provider_exit_carrier_precedence_and_normalisation(episode_store, name, carriers, expected, expected_carrier):
    """One adapter, one normalisation, one carrier order.

    Python reports ``-N`` for signal N and the mature convention is ``128+N``;
    the conversion happens once, in the evidence adapter, and never twice.  The
    provenance has to name the carrier that actually decided, or a later reader
    cannot tell a proven exit code from a guessed one.
    """
    spool_id = f"exit-{name}"
    cleanup = cleanup_fact("natural_exit", provider_exit_code=carriers["cleanup"])
    episode = localize_identities(causal_episode("released", cleanup=cleanup))
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
    if carriers["sidecar"] is not None:
        (episode_store.root / f"{spool_id}.owner-exit").write_text(
            json.dumps(
                {
                    "owner_generation": GENERATION,
                    "provider_exit_code": carriers["sidecar"],
                    "provider_reaped": True,
                    "cleanup_outcome": "natural_exit",
                }
            )
        )
    if carriers["exit_file"] is not None:
        spindle._get_exit_path(spool_id).write_text(f"{carriers['exit_file']}\n")

    converge(spool_id)

    record = episode_store.read(spool_id)
    assert record.get("exit_code", NULL) == expected
    carrier = (record.get("terminal_provenance") or {}).get("provider_exit_carrier", NULL)
    assert carrier == expected_carrier, (
        f"provenance named {carrier!r} as the deciding carrier, not {expected_carrier!r}"
    )


def test_a_terminal_with_no_provider_evidence_publishes_a_null_exit_and_no_carrier(episode_store):
    """No provider ever started, so there is nothing to normalise and no carrier.

    The claim is only worth anything on a history that really has no provider.
    A deadline that expired after binding is exactly that: the owner held the
    inode, the deadline passed before any provider was spawned, and the episode
    carries no provider or custody fact for a carrier to be derived from.  The
    terminal still has to be *published*, with a null exit code and a provenance
    that names no carrier - an unpublished record reports the same two nulls and
    proves nothing.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["deadline_after_binding"])
    episode = episode_store.episode(scenario.spool_id)
    assert "provider" not in episode and "provider_custody" not in episode, (
        "the no-provider case was built from an episode that had accepted provider custody"
    )
    assert episode["cleanup"]["provider_exit_code"] is None
    assert not (episode_store.root / f"{scenario.spool_id}.owner-exit").exists()
    assert not spindle._get_exit_path(scenario.spool_id).exists()

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record.get("terminal_origin") == "deadline_expired_before_provider_start", (
        "a record with no provider evidence was left unclassified rather than terminalised"
    )
    assert outcome_of(record) == scenario.expected_outcome()
    assert provenance_of(record) == scenario.expected_provenance()


def test_a_watchdog_handle_is_never_promoted_to_provider_exit_evidence(episode_store):
    """The in-memory handle belongs to the watchdog wrapper, not the provider."""
    spool_id = "exit-watchdog-handle-only"
    cleanup = cleanup_fact("natural_exit", provider_exit_code=None)
    episode = localize_identities(causal_episode("released", cleanup=cleanup))
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
    spindle._PROC_HANDLES[spool_id] = WatchdogHandle(247)

    converge(spool_id)

    record = episode_store.read(spool_id)
    assert record.get("exit_code", NULL) == NULL


def test_a_nonnatural_terminal_never_borrows_a_launcher_or_watchdog_code(episode_store):
    scenario = build_scenario(episode_store, BY_EVIDENCE["owner_crash_no_sidecar"])
    assert scenario.handle is not None, "this origin's launcher really does still hold a watchdog handle"
    scenario.handle._status = 125

    converge(scenario.spool_id)

    assert episode_store.read(scenario.spool_id).get("exit_code", NULL) == NULL


# --- 8. atomic outcome plus obligation intent -------------------------------


def test_no_observable_terminal_exists_without_its_owed_duty_set(episode_store, tmp_path):
    """The Family A promise: outcome and obligations share one atomic commit.

    Each entry has to be replayable from the record alone - key, intent, progress
    and completion evidence - because after a crash the record is all a fresh
    process has.  An entry that names only a kind and a progress bit forces the
    Family C rescan Phase 1 rejected.  The receipt duty is keyed per request,
    the unit Phase 1 makes idempotent, so a process that stopped between two
    receipts can read which request it still owes.
    """
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_timeout"],
        tmp_path=tmp_path,
        with_shard=True,
        extra_request="cancel",
    )

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record.get("terminal_origin"), "no terminal was published"
    block = record.get(CONVERGENCE_KEY)
    assert isinstance(block, dict), "a terminal was published without its obligation set"
    assert block.get("format") == CONVERGENCE_FORMAT
    entries = obligations_of(record)
    assert set(entries) == {"control_receipts", "failed_shard_preservation"}
    assert set(entries) <= set(DURABLE_OBLIGATIONS)
    assert_obligation_payload(
        entries["control_receipts"],
        kind="control_receipts",
        key=scenario.expected_receipt_keys(),
        intent=scenario.expected_receipt_intent(),
    )
    assert_obligation_payload(
        entries["failed_shard_preservation"],
        kind="failed_shard_preservation",
        key=idempotency_key("failed_shard_preservation", scenario.generation),
        intent=scenario.expected_shard_intent(),
    )


def test_a_natural_terminal_owes_its_transcript_with_the_captured_digest(episode_store):
    """The transcript duty carries the content it promised to persist.

    respin rebuilds a session from the transcript, so "best effort" is not a
    disposition it can live with: the digest is what proves the persisted bytes
    are the ones the terminal was reduced from.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])

    converge(scenario.spool_id)

    entries = obligations_of(episode_store.read(scenario.spool_id))
    intent = scenario.expected_transcript_intent()
    assert "natural_transcript" in entries, "a natural terminal owed no transcript"
    assert_obligation_payload(
        entries["natural_transcript"],
        kind="natural_transcript",
        key=idempotency_key("natural_transcript", scenario.generation, digest=intent["digest"]),
        intent=intent,
    )


def test_the_result_distinguishes_pending_obligations_from_full_convergence(episode_store, convergence_api, tmp_path):
    """terminal_with_obligations_pending is a durable state, not a return code."""
    scenario = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_timeout"],
        tmp_path=tmp_path,
        with_shard=True,
    )

    first = convergence_api.converge_here(scenario.spool_id)
    second = convergence_api.converge_here(scenario.spool_id)

    assert first.terminal_state in TERMINAL_STATES
    assert first.terminal_state in {"terminal_with_obligations_pending", "fully_converged"}
    assert second.terminal_state == "fully_converged"
    assert second.classification in CLASSIFICATIONS
    assert second.classification == "terminalizable"
    assert second.may_mutate is True
    assert list(second.local_errors) == []
    assert {obligation.kind for obligation in second.obligations} <= set(DURABLE_OBLIGATIONS)


def test_a_process_local_duty_is_never_recorded_as_a_durable_obligation(episode_store, tmp_path):
    """A ledger can describe a local duty; it cannot make it exactly-once.

    Handle reaping is attempted by every terminal observer that owns a handle,
    so it must not appear as a durable obligation with a completion bit another
    process could read as "already done".
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_cancel"], tmp_path=tmp_path, with_shard=True)

    converge(scenario.spool_id)

    assert LOCAL_DUTY not in obligations_of(episode_store.read(scenario.spool_id))
    assert scenario.spool_id not in spindle._PROC_HANDLES


def test_a_second_observer_still_attempts_its_own_local_handle_duty(episode_store):
    """Another process published the terminal; this one still owns its handle."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_cancel"], with_handle=False)
    converge_to_fixed_point(scenario.spool_id)
    late = WatchdogHandle(0)
    spindle._PROC_HANDLES[scenario.spool_id] = late

    converge(scenario.spool_id)

    assert scenario.spool_id not in spindle._PROC_HANDLES, (
        "a terminal observer that owns a handle skipped its local duty because another process won"
    )


# --- 9. consumers -----------------------------------------------------------


def _consumer_record(episode_store, kind: str, tmp_path):
    """One record per classification, built from durable evidence only."""
    spool_id = f"consumer-{kind}"
    if kind == "active":
        episode = localize_identities(causal_episode("accepted"))
        episode_store.bind_lock(spool_id, episode)
        record = episode_store.write(
            spool_id,
            status="running",
            episode=episode,
            created_at="2026-08-01T00:00:00",
            prompt="work",
            lifecycle={"ownership_state": "held", "transport_state": "connected"},
        )
        episode_store.hold_lock(spool_id)
        return record
    if kind == "terminalizable":
        scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], spool_id=spool_id, with_handle=False)
        return episode_store.read(scenario.spool_id)
    if kind == "store_unhealthy":
        episode = localize_identities(causal_episode("released"))
        episode.pop("cleanup")
        episode_store.bind_lock(spool_id, episode)
        return episode_store.write(
            spool_id,
            status="running",
            episode=episode,
            created_at="2026-08-01T00:00:00",
            prompt="work",
            lifecycle={"ownership_state": "held", "transport_state": "connected"},
        )
    raise KeyError(kind)


CONSUMER_MATRIX = (
    # classification, holds capacity, blocks destructive action, admits control
    ("active", True, True, True),
    ("terminalizable", False, False, False),
    ("store_unhealthy", True, True, False),
)


@pytest.mark.parametrize(
    ("kind", "holds_capacity", "blocks_destructive", "admits_control"),
    CONSUMER_MATRIX,
    ids=[row[0] for row in CONSUMER_MATRIX],
)
def test_every_consumer_reads_one_classification_and_keeps_its_own_policy(
    episode_store, tmp_path, kind, holds_capacity, blocks_destructive, admits_control
):
    """Capacity, destructive blocking, admission and diagnostics agree on meaning.

    They must not agree on *policy*: capacity counts, destructive blocking
    refuses, admission publishes.  What they may never do is reimplement episode
    meaning, which is how the same record started producing different verdicts.
    """
    record = _consumer_record(episode_store, kind, tmp_path)
    spool_id = record["id"]

    assert spindle._reconcile_spool_ownership(record).state == kind
    assert spindle._count_running() == (1 if holds_capacity else 0)
    assert spindle._spool_blocks_destructive_action(record) is blocks_destructive, (
        f"destructive blocking disagreed with capacity about a {kind} record; "
        f"consumers must read one classification rather than the status mirror"
    )
    request, error = spindle._request_owner_stop(spool_id, "cancel", "contract-test")
    assert (request is not None) is admits_control, error
    message = spindle._running_spool_message(record)
    if kind == "store_unhealthy":
        assert "store_unhealthy" in message


def test_store_health_refuses_new_launches_while_a_record_is_unhealthy(episode_store, tmp_path):
    """The two halves are one claim, so neither may cover for the other.

    Reporting the defect but launching anyway is the failure this is about, and
    an ``or`` over the two answers would call that a pass.
    """
    _consumer_record(episode_store, "store_unhealthy", tmp_path)

    failures = spindle._store_health_failures()
    success, error = spindle._try_reserve_slot_and_create("new-work")

    assert failures, "store health reported nothing about an unhealthy ownership record"
    assert success is False, "a launch was reserved while the store was unhealthy"
    assert "unhealthy" in error


def test_retention_retires_only_a_fully_converged_record(episode_store, tmp_path):
    """Retention timing stays outside the primitive but consumes its result."""
    converged = build_scenario(episode_store, BY_EVIDENCE["natural"], spool_id="retain-done", with_handle=False)
    pending = build_scenario(
        episode_store,
        BY_EVIDENCE["accepted_cancel"],
        spool_id="retain-shard",
        tmp_path=tmp_path,
        with_shard=True,
        with_handle=False,
    )
    converge_to_fixed_point(converged.spool_id)
    converge_to_fixed_point(pending.spool_id)

    spindle._cleanup_old_spools()

    assert not episode_store.spool_path(converged.spool_id).exists()
    assert episode_store.spool_path(pending.spool_id).exists(), (
        "retention deleted a record whose failed shard still needs an explicit decision"
    )


# --- 10. legacy and mixed-version behaviour ---------------------------------


def test_a_schema_1_record_without_an_episode_is_left_alone(episode_store):
    """Legacy terminal history stays readable and untouched (Decision 34)."""
    record = episode_store.write("legacy-terminal", status="complete", result="old result", created_at="2026-08-01")
    before = episode_store.spool_path("legacy-terminal").read_bytes()

    converge("legacy-terminal")

    after = episode_store.read("legacy-terminal")
    assert after["result"] == "old result"
    assert CONVERGENCE_KEY not in after
    assert "terminal_origin" not in after
    assert episode_store.spool_path("legacy-terminal").read_bytes() == before
    assert spindle._spool_blocks_destructive_action(record) is False


def test_a_live_record_without_an_episode_is_unhealthy_not_converged(episode_store):
    record = episode_store.write("legacy-live", status="running", owner_generation=1, created_at="2026-08-01")

    assert spindle._reconcile_spool_ownership(record).state == "store_unhealthy"

    converge("legacy-live")

    assert episode_store.read("legacy-live")["status"] == "running"


def test_an_unknown_episode_format_refuses_convergence(episode_store):
    episode = localize_identities(causal_episode("released"))
    episode["format"] = "spindle.owner-episode/2"
    episode_store.bind_lock("odd-format", episode)
    record = episode_store.write("odd-format", status="running", episode=episode, created_at="2026-08-01")
    before = episode_store.spool_path("odd-format").read_bytes()

    assert spindle._reconcile_spool_ownership(record).state == "store_unhealthy"

    converge("odd-format")

    assert episode_store.spool_path("odd-format").read_bytes() == before


def test_a_replaced_ownership_inode_is_unhealthy_not_terminalizable(episode_store):
    """Exact-inode authority, Decision 17: identity is device plus inode."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"], with_handle=False)
    record = episode_store.read(scenario.spool_id)
    record[EPISODE_KEY]["lock"] = {"device": 1, "inode": 1}
    episode_store.spool_path(scenario.spool_id).write_text(json.dumps(record))

    assert spindle._reconcile_spool_ownership(episode_store.read(scenario.spool_id)).state == "store_unhealthy"


def test_the_natural_parser_keeps_its_existing_behaviour(episode_store):
    """Harness parsing stays outside the primitive and is unchanged.

    The parser proposes a natural outcome; convergence accepts or overrides it by
    episode origin.  Its own extraction rules are not this RSP's business.
    """
    scenario = build_scenario(episode_store, BY_EVIDENCE["natural"])
    spindle._get_output_path(scenario.spool_id).write_text(
        json.dumps(
            [
                {"type": "system", "subtype": "init"},
                {"type": "result", "result": "parsed answer", "session_id": "sess-array", "total_cost_usd": 2.5},
            ]
        )
    )

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record["result"] == "parsed answer"
    assert record["session_id"] == "sess-array"
    assert record["cost"] == 2.5
    assert record["status"] == "complete"


def test_a_natural_proposal_is_overridden_by_an_authoritative_nonnatural_origin(episode_store):
    """Complete provider output does not outrank an accepted cancel."""
    scenario = build_scenario(episode_store, BY_EVIDENCE["accepted_cancel"])
    spindle._get_output_path(scenario.spool_id).write_text(
        json.dumps({"result": "the provider finished anyway", "session_id": "sess-late"})
    )

    converge(scenario.spool_id)

    record = episode_store.read(scenario.spool_id)
    assert record.get("result") is None, "a late natural proposal overrode an accepted cancel"
    assert record["status"] == "error"
    assert record["error"] == "Cancelled"
    assert record["terminal_origin"] == "accepted_cancel"
