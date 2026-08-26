"""S2-E: the authoritative generation episode, its transitions and consumers.

The corrective RSP replaces independently updated ownership flags, sidecar
interpretation and status-driven capacity with one durable episode, one guarded
transition, one pure classifier and one exact-inode snapshot.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import spindle
from spindle.namespace_owner import (
    LivenessEvidence,
    LockEvidence,
    ProcessIdentity,
    acquire_ownership_lock,
    active_spool_count,
    capture_pid_namespace,
    iter_control_requests,
    probe_ownership_lock,
)
from tests.owner_episode_fixtures import (
    ABSENT,
    ACTORS,
    BOUND_PHASES,
    DEADLINE,
    EPISODE_KEY,
    FACT_LITERALS,
    FORMAT_FIELD,
    OWNER_EPISODE_FORMAT,
    PHASE_PATHS,
    PHASES,
    RESEED_SOURCES,
    SOURCES,
    TABLE_ROWS,
    TRANSITION_TABLE,
    expected_rejection,
    facts_for,
    make_episode,
    required_facts,
)

EPISODE_CAPABILITY = "owner-episode-v1"
CONVERGENCE_CAPABILITY = "owner-convergence-v1"


def _row_id(row) -> str:
    actor, source, destination, _facts = row
    return f"{actor}:{source}->{destination}"


def _seed(
    store,
    spool_id: str,
    source: str,
    *,
    generation: int = 2,
    drop=(),
    path: str | None = None,
) -> dict | None:
    """Publish a record whose episode sits in *source*, minus *drop* facts."""
    if source == ABSENT:
        store.write(spool_id, status="pending")
        return None
    episode = make_episode(source, generation=generation, path=path)
    for name in drop:
        episode.pop(name, None)
    store.write(spool_id, status="running", episode=episode)
    return episode


def _transition_source_path(actor: str, source: str, destination: str) -> str | None:
    if source == "reserved" and actor == "launcher" and destination in {"reserved", "aborted"}:
        return "before_watchdog"
    return None


def _transition(api, store, spool_id, actor, source, destination, *, facts=None, **overrides):
    episode = store.episode(spool_id)
    reseed = source in RESEED_SOURCES and destination == "reserved"
    arguments = {
        "actor": actor,
        "destination": destination,
        "generation": (episode["generation"] + 1 if reseed else episode["generation"]) if episode else 1,
        "expected_revision": episode["revision"] if episode else None,
        "facts": facts_for(*required_facts(actor, source, destination)) if facts is None else facts,
    }
    arguments.update(overrides)
    return api.transition(store.root, spool_id, **arguments)


# --- 1. episode format ------------------------------------------------------


def test_episode_format_marker_is_published(episode_api):
    assert episode_api.OWNER_EPISODE_FORMAT == OWNER_EPISODE_FORMAT


def test_fixture_phase_times_follow_the_selected_episode_path():
    normal = make_episode("released")
    pre_watchdog_abort = make_episode("aborted", path="launcher_before_watchdog")
    pre_acceptance_cleanup = make_episode("cleanup_proven", path="before_acceptance")
    pre_acceptance_release = make_episode("released", path="before_acceptance")

    assert tuple(normal["phase_times"]) == (
        "reserved",
        "lock_bound",
        "accepted",
        "cleanup_proven",
        "released",
    )
    assert tuple(pre_watchdog_abort["phase_times"]) == ("reserved", "aborted")
    assert tuple(pre_acceptance_cleanup["phase_times"]) == (
        "reserved",
        "lock_bound",
        "cleanup_proven",
    )
    assert tuple(pre_acceptance_release["phase_times"]) == (
        "reserved",
        "lock_bound",
        "cleanup_proven",
        "released",
    )


# --- 2. transition table ----------------------------------------------------


@pytest.mark.parametrize("row", TRANSITION_TABLE, ids=_row_id)
def test_allowed_transition_publishes_its_facts_and_bumps_one_revision(episode_api, episode_store, row):
    actor, source, destination, facts = row
    spool_id = f"table-{actor}-{source}-{destination}"
    seeded = _seed(
        episode_store,
        spool_id,
        source,
        drop=facts,
        path=_transition_source_path(actor, source, destination),
    )
    reseed = source in RESEED_SOURCES and destination == "reserved"

    result = _transition(episode_api, episode_store, spool_id, actor, source, destination)

    if seeded is None:
        expected_generation, expected_revision = 1, 1
    elif reseed:
        expected_generation, expected_revision = seeded["generation"] + 1, 1
    else:
        expected_generation, expected_revision = seeded["generation"], seeded["revision"] + 1

    assert result.accepted is True, result.rejection
    stored = episode_store.episode(spool_id)
    assert stored["phase"] == destination
    assert stored[FORMAT_FIELD] == OWNER_EPISODE_FORMAT
    assert stored["generation"] == expected_generation
    assert stored["revision"] == expected_revision
    for name in facts:
        assert stored[name] == FACT_LITERALS[name]
    assert destination in stored["phase_times"]
    assert result.episode == stored
    assert episode_store.read(spool_id)["id"] == spool_id


@pytest.mark.parametrize("actor", ACTORS)
def test_every_row_outside_the_transition_table_is_rejected(episode_api, episode_store, actor):
    """Actor authority and the phase graph are separately enforced."""
    unexpected = {}
    for source in SOURCES:
        for destination in PHASES:
            if (actor, source, destination) in TABLE_ROWS:
                continue
            spool_id = f"outside-{actor}-{source}-{destination}"
            _seed(episode_store, spool_id, source)
            before = episode_store.spool_path(spool_id).read_bytes()
            result = _transition(
                episode_api,
                episode_store,
                spool_id,
                actor,
                source,
                destination,
                facts=facts_for(*FACT_LITERALS),
            )
            reason = expected_rejection(actor, source, destination)
            observed = (result.accepted, result.rejection)
            if observed != (False, reason):
                unexpected[f"{source}->{destination}"] = observed
            assert episode_store.spool_path(spool_id).read_bytes() == before

    assert unexpected == {}


@pytest.mark.parametrize(
    ("row", "omitted"),
    [(row, fact) for row in TRANSITION_TABLE for fact in row[3]],
    ids=lambda value: value if isinstance(value, str) else _row_id(value),
)
def test_transition_without_a_required_fact_is_rejected(episode_api, episode_store, row, omitted):
    actor, source, destination, facts = row
    spool_id = f"missing-{actor}-{source}-{destination}-{omitted}"
    _seed(
        episode_store,
        spool_id,
        source,
        drop=facts,
        path=_transition_source_path(actor, source, destination),
    )
    before = episode_store.spool_path(spool_id).read_bytes()

    result = _transition(
        episode_api,
        episode_store,
        spool_id,
        actor,
        source,
        destination,
        facts=facts_for(*[name for name in facts if name != omitted]),
    )

    assert (result.accepted, result.rejection) == (False, "missing_facts")
    assert episode_store.spool_path(spool_id).read_bytes() == before


@pytest.mark.parametrize("offset", [-1, 1])
def test_transition_carrying_another_generation_is_rejected(episode_api, episode_store, offset):
    spool_id = f"generation-{offset}"
    episode = _seed(episode_store, spool_id, "lock_bound", generation=3)
    before = episode_store.spool_path(spool_id).read_bytes()

    result = _transition(
        episode_api,
        episode_store,
        spool_id,
        "owner",
        "lock_bound",
        "accepted",
        generation=episode["generation"] + offset,
    )

    assert (result.accepted, result.rejection) == (False, "stale_generation")
    assert episode_store.spool_path(spool_id).read_bytes() == before


def test_same_id_reserve_requires_a_generation_above_the_released_episode(episode_api, episode_store):
    spool_id = "same-id-replacement"
    episode = _seed(episode_store, spool_id, "released", generation=3)

    stale = _transition(
        episode_api, episode_store, spool_id, "launcher", "released", "reserved", generation=episode["generation"]
    )
    assert (stale.accepted, stale.rejection) == (False, "stale_generation")

    accepted = _transition(episode_api, episode_store, spool_id, "launcher", "released", "reserved")

    assert accepted.accepted is True, accepted.rejection
    stored = episode_store.episode(spool_id)
    assert (stored["generation"], stored["revision"], stored["phase"]) == (4, 1, "reserved")
    assert "release" not in stored


def test_launcher_can_abort_before_watchdog_publication_at_revision_one(episode_api, episode_store):
    spool_id = "launcher-abort-before-watchdog"
    episode = _seed(episode_store, spool_id, "reserved", path="before_watchdog")

    assert episode["revision"] == 1
    assert "watchdog" not in episode

    result = _transition(episode_api, episode_store, spool_id, "launcher", "reserved", "aborted")

    assert result.accepted is True, result.rejection
    stored = episode_store.episode(spool_id)
    assert (stored["phase"], stored["revision"]) == ("aborted", 2)
    assert "watchdog" not in stored


def test_optional_deadline_survives_watchdog_publication(episode_api, episode_store):
    spool_id = "reservation-with-deadline"
    episode = make_episode("reserved", path="before_watchdog", deadline=DEADLINE)
    episode_store.write(spool_id, status="pending", episode=episode)

    result = _transition(episode_api, episode_store, spool_id, "launcher", "reserved", "reserved")

    assert result.accepted is True, result.rejection
    assert result.episode["deadline"] == DEADLINE


def test_transition_with_a_stale_revision_is_rejected(episode_api, episode_store):
    spool_id = "stale-revision"
    episode = _seed(episode_store, spool_id, "accepted")
    before = episode_store.spool_path(spool_id).read_bytes()

    result = _transition(
        episode_api,
        episode_store,
        spool_id,
        "owner",
        "accepted",
        "cleanup_proven",
        expected_revision=episode["revision"] - 1,
    )

    assert (result.accepted, result.rejection) == (False, "stale_revision")
    assert episode_store.spool_path(spool_id).read_bytes() == before


def test_first_reserve_refuses_a_supplied_revision(episode_api, episode_store):
    spool_id = "first-reserve"
    _seed(episode_store, spool_id, ABSENT)

    result = _transition(episode_api, episode_store, spool_id, "launcher", ABSENT, "reserved", expected_revision=1)

    assert (result.accepted, result.rejection) == (False, "stale_revision")
    assert episode_store.read(spool_id).get(EPISODE_KEY) is None


@pytest.mark.parametrize(
    ("source", "actor", "destination", "contradiction"),
    [
        ("lock_bound", "owner", "accepted", {"lock": {"device": 64, "inode": 988}}),
        ("accepted", "owner", "cleanup_proven", {"owner": {**FACT_LITERALS["owner"], "pid": 5555}}),
        ("accepted", "watchdog", "cleanup_proven", {"provider": {**FACT_LITERALS["provider"], "pid": 5556}}),
        (
            "cleanup_proven",
            "reconciler",
            "released",
            {"release": {**FACT_LITERALS["release"], "inode": 988}},
        ),
        (
            "accepted",
            "owner",
            "cleanup_proven",
            {"cleanup": {**FACT_LITERALS["cleanup"], "provider_reaped": False, "outcome": "descendants_survived"}},
        ),
    ],
    ids=["rebound-inode", "changed-owner", "changed-provider", "foreign-release", "unproven-cleanup"],
)
def test_transition_contradicting_a_durable_fact_is_rejected(
    episode_api, episode_store, source, actor, destination, contradiction
):
    spool_id = f"contradiction-{actor}-{destination}"
    _seed(episode_store, spool_id, source)
    before = episode_store.spool_path(spool_id).read_bytes()

    result = _transition(
        episode_api,
        episode_store,
        spool_id,
        actor,
        source,
        destination,
        facts={**facts_for(*required_facts(actor, source, destination)), **contradiction},
    )

    assert (result.accepted, result.rejection) == (False, "contradictory_facts")
    assert episode_store.spool_path(spool_id).read_bytes() == before


@pytest.mark.parametrize("row", [row for row in TRANSITION_TABLE if row[1] != ABSENT], ids=_row_id)
def test_unknown_episode_format_refuses_every_transition(episode_api, episode_store, row):
    actor, source, destination, _facts = row
    spool_id = f"format-{actor}-{source}-{destination}"
    episode = make_episode(source)
    episode[FORMAT_FIELD] = "spindle.owner-episode/2"
    episode_store.write(spool_id, status="running", episode=episode)
    before = episode_store.spool_path(spool_id).read_bytes()

    result = _transition(episode_api, episode_store, spool_id, actor, source, destination)

    assert (result.accepted, result.rejection) == (False, "unknown_episode_format")
    assert episode_store.spool_path(spool_id).read_bytes() == before


def test_concurrent_transitions_settle_on_one_revision(episode_api, episode_store):
    spool_id = "concurrent-transition"
    episode = _seed(episode_store, spool_id, "accepted")
    barrier = threading.Barrier(2)

    def publish(outcome):
        barrier.wait(timeout=5)
        return _transition(
            episode_api,
            episode_store,
            spool_id,
            "owner",
            "accepted",
            outcome,
            expected_revision=episode["revision"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10)
            for future in [pool.submit(publish, "cleanup_proven"), pool.submit(publish, "accepted")]
        ]

    accepted = [result for result in results if result.accepted]
    rejected = [result for result in results if not result.accepted]
    assert len(accepted) == len(rejected) == 1
    assert rejected[0].rejection == "stale_revision"
    assert episode_store.episode(spool_id)["revision"] == episode["revision"] + 1


# --- 3. pure classifier -----------------------------------------------------

CLASSIFIER_CASES = (
    ("reserved", "absent_legacy", "alive", "active"),
    ("reserved", "absent_legacy", "unverifiable", "active"),
    ("reserved", "absent_legacy", "dead", "retireable"),
    ("lock_bound", "held", "alive", "active"),
    ("lock_bound", "released", "dead", "active"),
    ("lock_bound", "identity_mismatch", "dead", "unhealthy"),
    ("lock_bound", "unreadable", "alive", "unhealthy"),
    ("accepted", "held", "alive", "active"),
    ("accepted", "released", "dead", "active"),
    ("accepted", "identity_mismatch", "alive", "unhealthy"),
    ("accepted", "unreadable", "unverifiable", "unhealthy"),
    ("cleanup_proven", "held", "alive", "active"),
    ("cleanup_proven", "released", "dead", "retireable"),
    ("cleanup_proven", "identity_mismatch", "dead", "unhealthy"),
    ("cleanup_proven", "unreadable", "dead", "unhealthy"),
    ("released", "released", "dead", "retireable"),
    ("released", "held", "dead", "active"),
    ("released", "identity_mismatch", "dead", "unhealthy"),
    ("aborted", "absent_legacy", "dead", "retireable"),
)


@pytest.mark.parametrize(
    ("phase", "lock_state", "liveness_state", "expected"),
    CLASSIFIER_CASES,
    ids=[f"{case[0]}-{case[1]}-{case[2]}" for case in CLASSIFIER_CASES],
)
def test_classifier_matrix(episode_api, phase, lock_state, liveness_state, expected):
    record = {"id": "classified", "status": "running", EPISODE_KEY: make_episode(phase)}

    classification = episode_api.classify(
        record,
        LockEvidence(lock_state, 64, 987),
        LivenessEvidence(liveness_state, f"{liveness_state}_probe"),
    )

    assert classification.state == expected
    assert classification.reason


def test_preacceptance_cleanup_is_valid_without_provider_custody(episode_api):
    episode = make_episode("cleanup_proven", path="before_acceptance")
    record = {"id": "preacceptance-cleanup", "status": "running", EPISODE_KEY: episode}

    classification = episode_api.classify(
        record,
        LockEvidence("held", 64, 987),
        LivenessEvidence("dead", "watchdog_contained"),
    )

    assert "provider" not in episode
    assert "provider_custody" not in episode
    assert episode["containment"]["contained"] is True
    assert classification.state == "active"


MISSING_PHASE_FACT_CASES = [
    (phase, path, fact)
    for phase, paths in PHASE_PATHS.items()
    for path, (facts, _revision) in paths.items()
    for fact in facts
]


@pytest.mark.parametrize(
    ("phase", "path", "missing"),
    MISSING_PHASE_FACT_CASES,
    ids=[f"{phase}-{path}-missing-{fact}" for phase, path, fact in MISSING_PHASE_FACT_CASES],
)
def test_classifier_rejects_a_phase_missing_any_required_fact(episode_api, phase, path, missing):
    episode = make_episode(phase, path=path)
    episode.pop(missing)
    record = {"id": "missing-phase-fact", "status": "running", EPISODE_KEY: episode}
    lock_state = "absent_legacy" if phase in {"reserved", "aborted"} else "held"

    classification = episode_api.classify(
        record,
        LockEvidence(lock_state, 64, 987),
        LivenessEvidence("unverifiable", "fixture"),
    )

    assert classification.state == "unhealthy"
    assert "fact" in classification.reason or "malformed" in classification.reason


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("revision", True),
        ("phase", "unknown"),
        ("phase_times", []),
    ],
)
def test_classifier_rejects_malformed_episode_core_fields(episode_api, field, value):
    episode = make_episode("accepted")
    episode[field] = value
    record = {"id": "malformed-episode", "status": "running", EPISODE_KEY: episode}

    classification = episode_api.classify(
        record,
        LockEvidence("held", 64, 987),
        LivenessEvidence("alive", "fixture"),
    )

    assert classification.state == "unhealthy"
    assert "malformed" in classification.reason or field in classification.reason


@pytest.mark.parametrize("phase", BOUND_PHASES)
@pytest.mark.parametrize("liveness_state", ["alive", "dead", "unverifiable"])
def test_bound_phase_classification_never_depends_on_liveness(episode_api, phase, liveness_state):
    record = {"id": "bound", "status": "running", EPISODE_KEY: make_episode(phase)}
    lock = LockEvidence("held" if phase != "released" else "released", 64, 987)

    classification = episode_api.classify(record, lock, LivenessEvidence(liveness_state, "probe"))
    baseline = episode_api.classify(record, lock, LivenessEvidence("alive", "probe"))

    assert classification.state == baseline.state


@pytest.mark.parametrize("status", ["pending", "running"])
def test_live_record_without_an_episode_is_unhealthy(episode_api, status):
    record = {"id": "pre-episode-live", "status": status, "owner_generation": 1}

    classification = episode_api.classify(
        record, LockEvidence("absent_legacy"), LivenessEvidence("unverifiable", "probe")
    )

    assert classification.state == "unhealthy"
    assert "episode" in classification.reason


@pytest.mark.parametrize("status", ["complete", "error", "timeout"])
def test_terminal_record_without_an_episode_is_retireable(episode_api, status):
    record = {"id": "pre-episode-terminal", "status": status}

    classification = episode_api.classify(
        record, LockEvidence("absent_legacy"), LivenessEvidence("dead", "terminal_record")
    )

    assert classification.state == "retireable"


def test_unknown_episode_format_is_unhealthy(episode_api):
    episode = make_episode("accepted")
    episode[FORMAT_FIELD] = "spindle.owner-episode/2"
    record = {"id": "unknown-format", "status": "running", EPISODE_KEY: episode}

    classification = episode_api.classify(record, LockEvidence("held", 64, 987), LivenessEvidence("alive", "probe"))

    assert classification.state == "unhealthy"
    assert "format" in classification.reason


def test_classification_ignores_missing_stale_and_contradictory_mirrors(episode_api, episode_store):
    """Sidecars are diagnostics; their absence or lag cannot move a verdict."""
    spool_id = "mirror-independent"
    episode = make_episode("accepted", generation=4)
    episode_store.write(spool_id, status="running", episode=episode)
    record = episode_store.read(spool_id)
    lock = LockEvidence("held", 64, 987)
    liveness = LivenessEvidence("unverifiable", "namespace_mismatch")
    baseline = episode_api.classify(record, lock, liveness)

    (episode_store.root / f"{spool_id}.owner-exit").write_text(
        json.dumps({"owner_generation": 1, "provider_reaped": True, "cleanup_outcome": "stopped"})
    )
    (episode_store.root / f"{spool_id}.process-identity").write_text(json.dumps({"owner_generation": 99}))
    stale = episode_api.classify(episode_store.read(spool_id), lock, liveness)

    assert baseline.state == "active"
    assert stale.state == baseline.state


# --- 4. exact-inode snapshot ------------------------------------------------


def _recorded_identity(path: Path, *, device=None, inode=None) -> ProcessIdentity:
    info = path.stat()
    return ProcessIdentity(
        pid=os.getpid(),
        birth_token="snapshot",
        namespace=capture_pid_namespace(),
        owner_generation=1,
        child_pgid=None,
        lock_device=info.st_dev if device is None else device,
        lock_inode=info.st_ino if inode is None else inode,
        lock_created=True,
    )


@pytest.fixture
def flock_schedule(monkeypatch):
    """Run one action inside the probe's flock call, on either branch."""
    real_flock = fcntl.flock

    def install(action, *, foreign_fd=None):
        fired = {"value": False}

        def scheduled(fd, operation):
            if fd != foreign_fd and operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not fired["value"]:
                fired["value"] = True
                action()
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", scheduled)
        return fired

    return install


def test_snapshot_revalidates_the_pathname_after_a_blocked_flock(tmp_path, lock_holder, flock_schedule):
    path = tmp_path / "held.process-owner"
    holder = lock_holder(path)
    identity = _recorded_identity(path)

    def replace():
        path.unlink()
        path.touch(mode=0o600)

    fired = flock_schedule(replace, foreign_fd=holder.fd)
    evidence = probe_ownership_lock(path, identity)

    assert fired["value"] is True
    assert evidence.state == "identity_mismatch"
    assert evidence.observed_inode != identity.lock_inode


def test_snapshot_revalidates_the_pathname_after_an_acquired_flock(tmp_path, flock_schedule):
    path = tmp_path / "released.process-owner"
    path.touch(mode=0o600)
    identity = _recorded_identity(path)

    def replace():
        path.unlink()
        path.touch(mode=0o600)

    fired = flock_schedule(replace)
    evidence = probe_ownership_lock(path, identity)

    assert fired["value"] is True
    assert evidence.state == "identity_mismatch"


@pytest.mark.parametrize("branch", ["held", "released"])
def test_snapshot_records_the_post_flock_revalidation_schedule(tmp_path, lock_holder, monkeypatch, branch):
    path = tmp_path / f"{branch}.process-owner"
    path.touch(mode=0o600)
    holder = lock_holder(path) if branch == "held" else None
    identity = _recorded_identity(path)
    schedule = []
    real_fstat, real_stat, real_flock = os.fstat, os.stat, fcntl.flock

    def record(name, function):
        def wrapper(*args, **kwargs):
            schedule.append(name)
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "fstat", record("fstat", real_fstat))
    monkeypatch.setattr(os, "stat", record("stat", real_stat))
    monkeypatch.setattr(fcntl, "flock", record("flock", real_flock))

    evidence = probe_ownership_lock(path, identity)

    assert evidence.state == ("held" if holder is not None else "released")
    assert schedule[schedule.index("flock") :][:3] == ["flock", "fstat", "stat"]


@pytest.mark.parametrize("branch", ["held", "released"])
def test_snapshot_treats_a_vanished_pathname_after_flock_as_mismatch(tmp_path, lock_holder, flock_schedule, branch):
    path = tmp_path / f"vanished-{branch}.process-owner"
    path.touch(mode=0o600)
    holder = lock_holder(path) if branch == "held" else None
    identity = _recorded_identity(path)

    fired = flock_schedule(path.unlink, foreign_fd=holder.fd if holder else None)
    evidence = probe_ownership_lock(path, identity)

    assert fired["value"] is True
    assert evidence.state == "identity_mismatch"


@pytest.mark.parametrize("branch", ["held", "released"])
def test_snapshot_treats_a_device_change_after_flock_as_mismatch(tmp_path, lock_holder, monkeypatch, branch):
    path = tmp_path / f"device-{branch}.process-owner"
    path.touch(mode=0o600)
    if branch == "held":
        lock_holder(path)
    identity = _recorded_identity(path)
    real_stat = os.stat
    seen = {"count": 0}

    def shifting_stat(target, **kwargs):
        result = real_stat(target, **kwargs)
        if Path(str(target)) == path:
            seen["count"] += 1
            if seen["count"] > 1:
                values = list(result)
                values[2] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", shifting_stat)
    evidence = probe_ownership_lock(path, identity)

    assert seen["count"] >= 2, "the pathname was not re-stat'ed after flock"
    assert evidence.state == "identity_mismatch"


def test_snapshot_reports_an_unreadable_path_without_guessing(tmp_path, monkeypatch):
    path = tmp_path / "unreadable.process-owner"
    path.touch(mode=0o600)
    identity = _recorded_identity(path)
    monkeypatch.setattr(
        os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied"))
    )

    assert probe_ownership_lock(path, identity).state == "unreadable"


def test_acquisition_revalidates_the_pathname_after_its_own_flock(tmp_path, flock_schedule):
    path = tmp_path / "acquired.process-owner"
    path.touch(mode=0o600)
    original = path.stat().st_ino

    def replace():
        path.unlink()
        path.touch(mode=0o600)

    fired = flock_schedule(replace)
    acquired = acquire_ownership_lock(path, max_attempts=3)
    try:
        assert fired["value"] is True
        assert acquired.inode == os.fstat(acquired.fd).st_ino
        assert acquired.inode == path.stat().st_ino
        assert acquired.inode != original
    finally:
        acquired.close()


# --- 5. writer preservation -------------------------------------------------


def test_normal_spawn_metadata_cannot_lower_a_newer_episode(episode_store):
    spool_id = "writer-normal-spawn"
    current = make_episode("lock_bound", generation=2)
    episode_store.write(spool_id, status="pending", episode=current, prompt="p")
    stale = {
        "id": spool_id,
        "status": "pending",
        "prompt": "p",
        EPISODE_KEY: make_episode("reserved", generation=2),
    }

    assert spindle._prepare_pending_spool_for_spawn(stale) is True

    stored = episode_store.episode(spool_id)
    assert (stored["phase"], stored["revision"]) == ("lock_bound", current["revision"])


def test_pre_spawn_failure_aborts_the_reservation_it_finalizes(episode_store):
    spool_id = "writer-pre-spawn-failure"
    reserved = make_episode("reserved", generation=2, path="before_watchdog")
    assert reserved["revision"] == 1
    assert "watchdog" not in reserved
    episode_store.write(spool_id, status="pending", episode=reserved)

    spindle._record_pre_spawn_failure(spool_id, "spawn failed: boom")

    record = episode_store.read(spool_id)
    stored = record.get(EPISODE_KEY)
    assert stored is not None, "pre-spawn failure dropped the episode"
    assert (record["status"], stored["phase"], stored["generation"], stored["revision"]) == (
        "error",
        "aborted",
        2,
        2,
    )
    assert "watchdog" not in stored
    assert stored["failure"]


def test_sandbox_refusal_preserves_and_aborts_the_episode(episode_store):
    spool_id = "writer-sandbox-refusal"
    reserved = make_episode("reserved", generation=2, path="before_watchdog")
    assert reserved["revision"] == 1
    assert "watchdog" not in reserved
    episode_store.write(spool_id, status="pending", episode=reserved)

    message = spindle._persist_codex_sandbox_refusal(
        spool_id,
        "codex sandbox is unavailable",
        sandbox="workspace-write",
        permission="careful",
        codex_bin="/usr/bin/codex",
        codex_version="1.0",
    )

    record = episode_store.read(spool_id)
    stored = record.get(EPISODE_KEY)
    assert message.startswith("Error:")
    assert message.endswith(f"(spool {spool_id})")
    assert "refusal persistence failed" not in message
    assert stored is not None, "sandbox refusal replaced the record and dropped the episode"
    assert (stored["phase"], stored["generation"], stored["revision"]) == ("aborted", 2, 2)
    assert "watchdog" not in stored


def test_sandbox_refusal_keeps_owner_spool_after_durable_abort_cleanup_failure(episode_store, monkeypatch):
    spool_id = "writer-sandbox-refusal-durable-abort"
    reserved = make_episode("reserved", generation=2, path="before_watchdog")
    episode_store.write(spool_id, status="pending", episode=reserved)
    real_close = os.close
    directory = episode_store.root.stat()
    directory_closes = 0

    def fail_abort_directory_close(fd):
        nonlocal directory_closes
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == (directory.st_dev, directory.st_ino):
            directory_closes += 1
            real_close(fd)
            if directory_closes == 2:
                raise OSError(errno.EIO, "owner directory close failed after durable abort")
            return None
        return real_close(fd)

    monkeypatch.setattr(os, "close", fail_abort_directory_close)

    message = spindle._persist_codex_sandbox_refusal(
        spool_id,
        "REFUSED: codex sandbox is unavailable",
        sandbox="workspace-write",
        permission="careful",
        codex_bin="/usr/bin/codex",
        codex_version="1.0",
    )

    record = episode_store.read(spool_id)
    assert message == f"Error: REFUSED: codex sandbox is unavailable (spool {spool_id})"
    assert directory_closes == 2
    assert record["status"] == "pending"
    assert record[EPISODE_KEY]["phase"] == "aborted"
    assert record[EPISODE_KEY]["failure"]["detail"] == "REFUSED: codex sandbox is unavailable"
    assert "sandbox_error" not in record


def test_sandbox_refusal_episode_path_is_utf8_independent_of_text_defaults(episode_store, monkeypatch):
    spool_id = "writer-sandbox-refusal-utf8"
    reserved = make_episode("reserved", generation=2, path="before_watchdog")
    record = episode_store.write(spool_id, status="pending", episode=reserved, prompt="caf\u00e9 \u2615")
    episode_store.spool_path(spool_id).write_bytes(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    real_open = open
    real_read_text = Path.read_text
    real_fdopen = os.fdopen

    def ascii_default_open(file, mode="r", *args, **kwargs):
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "ascii"
        return real_open(file, mode, *args, **kwargs)

    def ascii_default_read_text(self, *args, **kwargs):
        if "encoding" not in kwargs:
            kwargs["encoding"] = "ascii"
        return real_read_text(self, *args, **kwargs)

    def ascii_default_fdopen(fd, mode="r", *args, **kwargs):
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "ascii"
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", ascii_default_open)
    monkeypatch.setattr(Path, "read_text", ascii_default_read_text)
    monkeypatch.setattr(os, "fdopen", ascii_default_fdopen)

    message = spindle._persist_codex_sandbox_refusal(
        spool_id,
        "REFUSED: codex sandbox is unavailable",
        sandbox="workspace-write",
        permission="careful",
        codex_bin="/usr/bin/codex",
        codex_version="1.0",
    )

    stored_record = json.loads(episode_store.spool_path(spool_id).read_bytes())
    assert message == f"Error: REFUSED: codex sandbox is unavailable (spool {spool_id})"
    assert stored_record["status"] == "error"
    assert stored_record["error"] == "REFUSED: codex sandbox is unavailable"
    assert stored_record["sandbox_error"] == "REFUSED: codex sandbox is unavailable"
    assert stored_record["prompt"] == "caf\u00e9 \u2615"
    assert stored_record[EPISODE_KEY]["phase"] == "aborted"
    assert "watchdog" not in stored_record[EPISODE_KEY]


def test_finalization_persists_release_from_cleanup_proven(episode_store):
    spool_id = "writer-finalization"
    episode = make_episode("cleanup_proven", generation=2)
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(spool_id, status="running", episode=episode, owner_generation=2)

    assert spindle._check_and_finalize_spool(spool_id) is True

    record = episode_store.read(spool_id)
    stored = record[EPISODE_KEY]
    assert (stored["phase"], stored["revision"]) == ("released", episode["revision"] + 1)
    assert stored["generation"] == 2
    assert (stored["release"]["device"], stored["release"]["inode"]) == (
        episode["lock"]["device"],
        episode["lock"]["inode"],
    )
    assert record["status"] != "running"


def test_finalization_projects_a_released_episode_when_public_terminal_is_missing(episode_store):
    spool_id = "writer-released-before-projection"
    episode = make_episode("released", generation=2)
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="running",
        episode=episode,
        owner_generation=2,
        completed_at=None,
    )

    assert spindle._check_and_finalize_spool(spool_id) is True

    record = episode_store.read(spool_id)
    assert record[EPISODE_KEY] == episode
    assert record["status"] not in {"pending", "running"}
    assert record["completed_at"]


@pytest.mark.parametrize(
    ("origin", "request_kind", "terminal_kind", "failure_kind", "owner_exit_kind"),
    [
        ("cancel", "cancel", "cancelled", None, None),
        ("drop", "drop", "cancelled", None, None),
        ("timeout", "timeout", "timeout", None, None),
        ("pre-popen-deadline", None, "timeout", "deadline_expired_before_provider_start", None),
        ("owner-transport-loss", None, "indeterminate", None, "owner_crashed"),
        ("watchdog-transport-loss", None, "indeterminate", "watchdog_parent_loss", None),
        ("natural-provider-failure", None, None, None, None),
    ],
)
def test_every_terminal_origin_runs_idempotent_post_terminal_bookkeeping(
    episode_store,
    monkeypatch,
    tmp_path,
    origin,
    request_kind,
    terminal_kind,
    failure_kind,
    owner_exit_kind,
):
    spool_id = f"terminal-bookkeeping-{origin}"
    natural = origin == "natural-provider-failure"
    episode = None
    if not natural:
        path = "before_acceptance" if failure_kind else "after_acceptance"
        overrides = {}
        if request_kind:
            overrides.update(
                winning_request={
                    "request_id": f"{origin}-request",
                    "kind": request_kind,
                    "desired_terminal_kind": terminal_kind,
                },
                acknowledgement={"acknowledged_at": "2026-08-11T00:00:04+00:00"},
            )
        if failure_kind:
            overrides["failure"] = {
                "kind": failure_kind,
                "detail": origin,
                "observed_at": "2026-08-11T00:00:05+00:00",
            }
        episode = make_episode("released", generation=3, path=path, **overrides)
        episode_store.bind_lock(spool_id, episode)

    worktree = tmp_path / f"{spool_id}-worktree"
    worktree.mkdir()
    record_fields = {
        "pid": 999999999,
        "timeout": 17,
        "harness": "codex",
        "shard": {"worktree_path": str(worktree), "branch_name": f"shard-{spool_id}"},
        "shard_created_by_spool": True,
    }
    if episode:
        record_fields["owner_generation"] = 3
    episode_store.write(spool_id, status="running", episode=episode, **record_fields)
    if natural:
        monkeypatch.setattr(
            spindle,
            "_reconcile_spool_ownership",
            lambda _spool: SimpleNamespace(state="terminalizable"),
        )
    if owner_exit_kind:
        (episode_store.root / f"{spool_id}.owner-exit").write_text(
            json.dumps({"owner_generation": 3, owner_exit_kind: True})
        )

    provider_stream = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "natural-session"}),
            json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}}),
        ]
    )
    spindle._get_output_path(spool_id).write_text(provider_stream)
    spindle._get_exit_path(spool_id).write_text("1\n")

    class LiveWatchdogHandle:
        def poll(self):
            return None

    handle = LiveWatchdogHandle()
    spindle._PROC_HANDLES[spool_id] = handle
    bookkeeping_calls = []
    reap_calls = []
    real_bookkeeping = spindle._post_terminal_bookkeeping

    def record_bookkeeping(bookkeeping_spool_id, spool):
        bookkeeping_calls.append(bookkeeping_spool_id)
        real_bookkeeping(bookkeeping_spool_id, spool)

    monkeypatch.setattr(spindle, "_post_terminal_bookkeeping", record_bookkeeping)
    monkeypatch.setattr(spindle, "_reap_process_handle_later", reap_calls.append)

    assert spindle._check_and_finalize_spool(spool_id) is True

    terminal = episode_store.read(spool_id)
    assert terminal["status"] in {"error", "timeout"}
    assert terminal["shard_cleanup_preserved"] is True
    assert terminal["shard"]["startup_failure_preserved"] is True
    assert spool_id not in spindle._PROC_HANDLES
    assert bookkeeping_calls == [spool_id]
    assert reap_calls == [handle]
    if natural:
        assert terminal["error"] == "provider failed"
        assert spindle._get_transcript_path(spool_id).read_text() == provider_stream
    else:
        assert terminal["lifecycle"]["normalized_terminal_kind"] == terminal_kind
        assert not spindle._get_transcript_path(spool_id).exists()
        assert "session_id" not in terminal

    terminal_bytes = episode_store.spool_path(spool_id).read_bytes()
    assert spindle._check_and_finalize_spool(spool_id) is True
    assert episode_store.spool_path(spool_id).read_bytes() == terminal_bytes
    assert bookkeeping_calls == [spool_id]
    assert reap_calls == [handle]


def _advance_episode_during(monkeypatch, episode_store, spool_id, advanced):
    def cleanup(*_args, **_kwargs):
        record = episode_store.read(spool_id)
        record[EPISODE_KEY] = advanced
        episode_store.spool_path(spool_id).write_text(json.dumps(record))
        return True

    monkeypatch.setattr(spindle, "_cleanup_shard", cleanup)


def test_shard_abandon_preserves_an_episode_advanced_while_it_worked(episode_store, monkeypatch, tmp_path):
    spool_id = "writer-shard-abandon"
    worktree = tmp_path / "repo" / "worktrees" / "shard"
    worktree.mkdir(parents=True)
    episode = make_episode("released", generation=2)
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="error",
        episode=episode,
        shard={"worktree_path": str(worktree), "branch_name": "shard-branch"},
        working_dir=str(worktree),
    )
    advanced = make_episode("released", generation=2, revision=episode["revision"] + 1, retired=True)
    episode_store.bind_lock(spool_id, advanced)
    _advance_episode_during(monkeypatch, episode_store, spool_id, advanced)

    message = spindle._shard_abandon_locked(spool_id, False, str(tmp_path))

    assert "Abandoned shard" in message, message
    assert episode_store.episode(spool_id) == advanced


def test_shard_merge_preserves_an_episode_advanced_while_it_worked(episode_store, monkeypatch, tmp_path):
    spool_id = "writer-shard-merge"
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True)
    worktree = repo / "worktrees" / "shard"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "shard-branch", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    episode = make_episode("released", generation=2)
    episode_store.bind_lock(spool_id, episode)
    episode_store.write(
        spool_id,
        status="complete",
        episode=episode,
        base_branch="main",
        shard={"worktree_path": str(worktree), "branch_name": "shard-branch"},
        working_dir=str(worktree),
    )
    advanced = make_episode("released", generation=2, revision=episode["revision"] + 1, retired=True)
    episode_store.bind_lock(spool_id, advanced)
    _advance_episode_during(monkeypatch, episode_store, spool_id, advanced)

    message = spindle._shard_merge_locked(spool_id, False, str(tmp_path))

    assert "Successfully merged" in message, message
    assert episode_store.episode(spool_id) == advanced


# --- 6. mailbox-then-spool control admission --------------------------------

ADMISSION_CALLERS = ("drop", "timeout", "shard_abandon")


def _publish_owner_mirrors(episode_store, spool_id, episode):
    identity = ProcessIdentity(
        pid=os.getpid(),
        birth_token=spindle._process_start_time(os.getpid()),
        namespace=capture_pid_namespace(),
        owner_generation=episode["generation"],
        child_pgid=None,
        lock_device=episode["lock"]["device"],
        lock_inode=episode["lock"]["inode"],
        lock_created=True,
    )
    (episode_store.root / f"{spool_id}.owner-identity").write_text(json.dumps(identity.to_dict()))
    (episode_store.root / f"{spool_id}.owner-exit").unlink(missing_ok=True)


def _admission_record(episode_store, caller, spool_id, phase, tmp_path, *, publish_generation):
    episode = make_episode(phase, generation=5)
    episode_store.bind_lock(spool_id, episode)
    fields = {"created_at": (datetime.now() - timedelta(hours=1)).isoformat(), "prompt": "work"}
    if publish_generation:
        fields["owner_generation"] = episode["generation"]
    if caller == "timeout":
        fields["timeout"] = 1
    if caller == "shard_abandon":
        worktree = tmp_path / "repo" / "worktrees" / spool_id
        worktree.mkdir(parents=True)
        fields["shard"] = {"worktree_path": str(worktree), "branch_name": "shard-branch"}
    episode_store.write(spool_id, status="running", episode=episode, **fields)
    episode_store.hold_lock(spool_id)
    _publish_owner_mirrors(episode_store, spool_id, episode)
    return episode


def _invoke_admission(caller, spool_id, tmp_path):
    if caller == "drop":
        return spindle._spin_drop_sync(spool_id)
    if caller == "shard_abandon":
        return spindle._shard_abandon_sync(spool_id, False, str(tmp_path))
    return spindle._reconcile_spool_step(spool_id)


@pytest.mark.parametrize("caller", ADMISSION_CALLERS)
def test_accepted_episode_admits_exactly_one_generation_scoped_request(episode_store, caller, tmp_path):
    spool_id = f"admit-{caller}"
    episode = _admission_record(episode_store, caller, spool_id, "accepted", tmp_path, publish_generation=False)

    _invoke_admission(caller, spool_id, tmp_path)

    requests = list(iter_control_requests(episode_store.root, spool_id))
    assert len(requests) == 1, f"{caller} published {len(requests)} durable requests"
    assert requests[0].owner_generation == episode["generation"]


@pytest.mark.parametrize("caller", ADMISSION_CALLERS)
def test_proven_cleanup_refuses_admission_without_creating_a_request(episode_store, caller, tmp_path):
    spool_id = f"settled-{caller}"
    _admission_record(episode_store, caller, spool_id, "cleanup_proven", tmp_path, publish_generation=True)

    _invoke_admission(caller, spool_id, tmp_path)

    assert list(iter_control_requests(episode_store.root, spool_id)) == []
    lifecycle = episode_store.read(spool_id).get("lifecycle") or {}
    assert lifecycle.get("public_stop_state") != "stopping"


@pytest.mark.parametrize("caller", ADMISSION_CALLERS)
def test_admission_holds_the_mailbox_guard_outside_the_record_guard(
    episode_store, guard_order, monkeypatch, caller, tmp_path
):
    spool_id = f"order-{caller}"
    _admission_record(episode_store, caller, spool_id, "accepted", tmp_path, publish_generation=False)
    real_create = spindle.create_control_request
    observed = []

    def recording_create(*args, **kwargs):
        observed.append(list(guard_order.events))
        return real_create(*args, **kwargs)

    monkeypatch.setattr(spindle, "create_control_request", recording_create)

    _invoke_admission(caller, spool_id, tmp_path)

    assert observed, f"{caller} never published a durable control request"
    assert guard_order.open_stack(observed[0]) == ["mailbox", "spool"]


# --- 7. one classifier behind both capacity APIs ----------------------------


def _live_starter() -> dict:
    return {
        "pid": os.getpid(),
        "birth_token": spindle._process_start_time(os.getpid()),
        "namespace": capture_pid_namespace().to_dict(),
    }


def _dead_starter() -> dict:
    return {"pid": 424242, "birth_token": "101", "namespace": capture_pid_namespace().to_dict()}


def _capacity_matrix(episode_store):
    records = []
    for spool_id, status, episode in (
        ("cap-reserved", "pending", make_episode("reserved", starter=_live_starter())),
        ("cap-accepted-held", "running", make_episode("accepted")),
        ("cap-accepted-released", "running", make_episode("accepted")),
        ("cap-cleanup", "running", make_episode("cleanup_proven")),
        ("cap-released", "running", make_episode("released")),
        ("cap-aborted", "running", make_episode("aborted")),
        ("cap-no-episode-live", "running", None),
        ("cap-no-episode-terminal", "complete", None),
        ("cap-error-released", "error", make_episode("released")),
    ):
        if episode is not None and "lock" in episode:
            episode_store.bind_lock(spool_id, episode)
        records.append(episode_store.write(spool_id, status=status, episode=episode))
    episode_store.hold_lock("cap-accepted-held")
    return records


def test_both_capacity_apis_route_every_record_through_the_classifier(episode_store, monkeypatch):
    records = _capacity_matrix(episode_store)
    expected_ids = [record["id"] for record in records]
    states = {
        spool_id: ("retireable" if index % 3 == 0 else "unhealthy" if index % 3 == 1 else "active")
        for index, spool_id in enumerate(expected_ids)
    }
    expected_count = sum(state != "retireable" for state in states.values())
    seen = []

    def classify(record, _lock, _liveness):
        seen.append(record["id"])
        return SimpleNamespace(state=states[record["id"]], reason="capacity-spy")

    import spindle.namespace_owner as owner_module

    monkeypatch.setattr(owner_module, "classify_owner_episode", classify, raising=False)
    monkeypatch.setattr(spindle, "classify_owner_episode", classify, raising=False)

    assert spindle._count_running() == expected_count
    assert sorted(seen) == sorted(expected_ids)
    assert len(seen) == len(expected_ids)

    seen.clear()
    assert active_spool_count(records) == expected_count
    assert sorted(seen) == sorted(expected_ids)
    assert len(seen) == len(expected_ids)


def test_capacity_releases_a_reservation_whose_starter_is_proven_dead(episode_store):
    episode_store.write("cap-dead-starter", status="pending", episode=make_episode("reserved", starter=_dead_starter()))

    assert spindle._count_running() == 0


# --- 8. episode-aware compatibility -----------------------------------------


def _frozen_pre_episode_launcher_accepts(record: dict) -> bool:
    """Exact negotiation a pre-episode launcher shipped with."""
    protocol = record.get("supported_supervisor_protocol_range") or {}
    capabilities = set(record.get("supervisor_capabilities") or ())
    overlap = max(protocol.get("min", 0), 1) <= min(protocol.get("max", 0), 1)
    return overlap and {"supervisor-compatibility-ranges"} <= capabilities


def _live_pre_episode_record() -> dict:
    return {
        "pid": 321,
        "supervisor_protocol_version": 1,
        "spool_schema_version": 1,
        "supported_supervisor_protocol_range": {"min": 1, "max": 1},
        "readable_spool_schemas": [1],
        "writable_spool_schema": 1,
        "supervisor_capabilities": ["supervisor-compatibility-ranges"],
        "package": "/old/spindle",
    }


def test_supervisor_protocol_requires_the_episode_capability():
    assert spindle.SUPERVISOR_PROTOCOL_VERSION == 2
    assert spindle.SUPPORTED_SUPERVISOR_PROTOCOL_RANGE == (2, 2)
    assert EPISODE_CAPABILITY in spindle.SUPERVISOR_CAPABILITIES
    assert EPISODE_CAPABILITY in spindle.REQUIRED_SUPERVISOR_CAPABILITIES
    assert CONVERGENCE_CAPABILITY in spindle.SUPERVISOR_CAPABILITIES
    assert CONVERGENCE_CAPABILITY in spindle.REQUIRED_SUPERVISOR_CAPABILITIES


def test_pre_convergence_supervisor_is_incompatible_even_when_episode_aware():
    record = spindle._supervisor_identity(os.getpid())
    record["supervisor_capabilities"].remove(CONVERGENCE_CAPABILITY)

    error = spindle._supervisor_compatibility_error(record)

    assert error is not None
    assert CONVERGENCE_CAPABILITY in error


def test_pre_episode_and_episode_aware_supervisors_refuse_each_other():
    error = spindle._supervisor_compatibility_error(_live_pre_episode_record())

    assert error is not None
    assert "1-1" in error and "2-2" in error
    assert EPISODE_CAPABILITY in error
    assert not _frozen_pre_episode_launcher_accepts(spindle._supervisor_identity(os.getpid()))


def test_a_live_pre_episode_owner_must_drain_before_maintenance(episode_store, monkeypatch):
    (episode_store.root / ".supervisor.json").write_text(json.dumps(_live_pre_episode_record()))
    lock_fd = os.open(episode_store.root / ".supervisor.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    before = (episode_store.root / ".supervisor.json").read_bytes()
    maintenance = []
    monkeypatch.setattr(spindle, "_cleanup_old_spools", lambda: maintenance.append("cleanup"))
    monkeypatch.setattr(spindle, "_recovery_pass", lambda: maintenance.append("recovery") or False)
    try:
        assert spindle._live_owner_blocks_store_maintenance() is True
        spindle._run_store_maintenance(cleanup=True)
        assert maintenance == []
        assert (episode_store.root / ".supervisor.json").read_bytes() == before
    finally:
        os.close(lock_fd)

    assert spindle._live_owner_blocks_store_maintenance() is False
    spindle._run_store_maintenance(cleanup=True)

    assert maintenance == ["cleanup", "recovery"]
    assert (episode_store.root / ".supervisor.json").read_bytes() == before

    success, error = spindle._try_reserve_slot_and_create("post-drain-current")

    assert success is True, error
    reserved = episode_store.read("post-drain-current")
    assert reserved[EPISODE_KEY][FORMAT_FIELD] == OWNER_EPISODE_FORMAT
    assert (reserved[EPISODE_KEY]["phase"], reserved[EPISODE_KEY]["revision"]) == ("reserved", 1)


def test_reservation_refuses_a_live_pre_episode_record_without_mutating_it(episode_store):
    # The spool id deliberately avoids the words the diagnosis must supply.
    episode_store.write("legacy-live", status="running", owner_generation=1, prompt="old")
    before = episode_store.spool_path("legacy-live").read_bytes()

    success, error = spindle._try_reserve_slot_and_create("new-work")

    assert success is False
    assert "episode" in error
    assert episode_store.spool_path("legacy-live").read_bytes() == before
    assert not episode_store.spool_path("new-work").exists()


def test_reservation_refuses_an_unknown_episode_format_without_mutating_it(episode_store):
    episode = make_episode("accepted")
    episode[FORMAT_FIELD] = "spindle.owner-episode/2"
    episode_store.write("odd-marker-live", status="running", episode=episode)
    before = episode_store.spool_path("odd-marker-live").read_bytes()

    success, error = spindle._try_reserve_slot_and_create("new-work")

    assert success is False
    assert "format" in error
    assert episode_store.spool_path("odd-marker-live").read_bytes() == before


def test_terminal_pre_episode_records_stay_readable(episode_store, episode_api):
    record = episode_store.write("legacy-terminal", status="complete", result="old result")

    assert spindle._read_spool("legacy-terminal")["result"] == "old result"
    assert [item["id"] for item in spindle._list_spools()] == ["legacy-terminal"]
    assert spindle._spool_blocks_destructive_action(record) is False
    classification = episode_api.classify(
        record, LockEvidence("absent_legacy"), LivenessEvidence("dead", "terminal_record")
    )
    assert classification.state == "retireable"
