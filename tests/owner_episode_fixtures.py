"""Frozen contract literals and fixtures for the authoritative owner episode.

The generation episode does not exist in production yet, so this module holds
the vocabulary the RSP test design pins: the phase graph, the actor/transition
table with its required facts, the rejection vocabulary, and the literal record
shape.  Two production names are unavoidable because they *are* the primitive
boundary (the durable guarded transition and the pure classifier); both are
reached through :func:`episode_api` so the final names can still change and a
missing name fails one test with a precise message instead of breaking
collection for a whole module.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# --- record shape -----------------------------------------------------------

OWNER_EPISODE_FORMAT = "spindle.owner-episode/1"
EPISODE_KEY = "owner_episode"
FORMAT_FIELD = "format"

# --- phase graph and actors -------------------------------------------------

ABSENT = "absent"
PHASES = ("reserved", "lock_bound", "accepted", "cleanup_proven", "released", "aborted")
TERMINAL_PHASES = ("released", "aborted")
BOUND_PHASES = ("lock_bound", "accepted", "cleanup_proven", "released")
SOURCES = (ABSENT,) + PHASES
ACTORS = ("launcher", "owner", "watchdog", "reconciler")

REJECTIONS = (
    "stale_generation",
    "stale_revision",
    "missing_facts",
    "contradictory_facts",
    "illegal_actor",
    "illegal_transition",
    "unknown_episode_format",
)

# actor, source phase, destination phase, facts the transition must publish.
# ``generation`` is a call argument rather than a fact: every row must carry the
# current generation, and only a reserve row may raise it.
TRANSITION_TABLE = (
    ("launcher", ABSENT, "reserved", ("starter",)),
    ("launcher", "released", "reserved", ("starter",)),
    ("launcher", "aborted", "reserved", ("starter",)),
    ("launcher", "reserved", "reserved", ("watchdog",)),
    ("launcher", "reserved", "aborted", ("failure",)),
    ("owner", "reserved", "lock_bound", ("owner", "lock")),
    ("owner", "lock_bound", "lock_bound", ("failure",)),
    ("owner", "lock_bound", "accepted", ("provider", "provider_custody")),
    ("owner", "accepted", "accepted", ("winning_request", "acknowledgement")),
    ("owner", "accepted", "cleanup_proven", ("cleanup",)),
    ("watchdog", "reserved", "aborted", ("failure",)),
    ("watchdog", "lock_bound", "cleanup_proven", ("containment", "cleanup")),
    ("watchdog", "accepted", "cleanup_proven", ("containment", "cleanup")),
    ("reconciler", "cleanup_proven", "released", ("release",)),
)

RESEED_SOURCES = frozenset({ABSENT, "released", "aborted"})
TABLE_EDGES = frozenset((source, destination) for _actor, source, destination, _facts in TRANSITION_TABLE)
TABLE_ROWS = frozenset((actor, source, destination) for actor, source, destination, _facts in TRANSITION_TABLE)


def required_facts(actor: str, source: str, destination: str) -> tuple:
    for row_actor, row_source, row_destination, facts in TRANSITION_TABLE:
        if (row_actor, row_source, row_destination) == (actor, source, destination):
            return facts
    raise KeyError((actor, source, destination))


def expected_rejection(actor: str, source: str, destination: str) -> str:
    """Reason a row outside the table must produce.

    A row whose edge some other actor may take is an actor defect; an edge no
    actor may take is a graph defect.  The two must stay distinguishable so a
    caller cannot silently acquire another actor's authority.
    """
    if (actor, source, destination) in TABLE_ROWS:
        raise KeyError((actor, source, destination))
    return "illegal_actor" if (source, destination) in TABLE_EDGES else "illegal_transition"


# --- literal facts ----------------------------------------------------------

NAMESPACE = {"status": "supported", "device": 7, "inode": 11}
STARTER = {"pid": 4242, "birth_token": "9001", "namespace": NAMESPACE}
WATCHDOG = {"pid": 4243, "birth_token": "9002", "namespace": NAMESPACE}
OWNER = {"pid": 4244, "birth_token": "9003", "namespace": NAMESPACE}
LOCK = {"device": 64, "inode": 987}
PROVIDER = {"pid": 4245, "pgid": 4245, "birth_token": "9004", "namespace": NAMESPACE}
PROVIDER_CUSTODY = {"pidfd_acquired": True, "containment": "watchdog", "published_at": "2026-08-11T00:00:03+00:00"}
WINNING_REQUEST = {"request_id": "req-1", "kind": "cancel", "desired_terminal_kind": "cancelled"}
ACKNOWLEDGEMENT = {"acknowledged_at": "2026-08-11T00:00:04+00:00"}
DEADLINE = "2026-08-11T00:01:00+00:00"
CLEANUP = {
    "outcome": "natural_exit",
    "provider_reaped": True,
    "adopted_children_reaped": 0,
    "child_exit_observed_at": "2026-08-11T00:00:05+00:00",
    "provider_exit_code": 0,
}
CONTAINMENT = {"contained": True, "adopted_children_reaped": 2, "observed_at": "2026-08-11T00:00:05+00:00"}
FAILURE = {"kind": "owner_preacceptance_failure", "detail": "owner exited before binding", "observed_at": "2026-08-11T00:00:02+00:00"}
RELEASE = {"device": 64, "inode": 987, "proved_by": "reconciler", "released_at": "2026-08-11T00:00:06+00:00"}

FACT_LITERALS = {
    "starter": STARTER,
    "watchdog": WATCHDOG,
    "owner": OWNER,
    "lock": LOCK,
    "provider": PROVIDER,
    "provider_custody": PROVIDER_CUSTODY,
    "winning_request": WINNING_REQUEST,
    "acknowledgement": ACKNOWLEDGEMENT,
    "cleanup": CLEANUP,
    "containment": CONTAINMENT,
    "failure": FAILURE,
    "release": RELEASE,
}

# A phase can be reached through more than one valid history.  In particular,
# launcher failure can abort revision 1 before watchdog publication, while a
# watchdog can prove cleanup from ``lock_bound`` before provider acceptance.
# Keeping those paths explicit prevents fixtures from inventing facts which
# were never durably published.
PHASE_PATHS = {
    "reserved": {
        "before_watchdog": (("starter",), 1),
        "watchdog_published": (("starter", "watchdog"), 2),
    },
    "lock_bound": {
        "owner_bound": (("starter", "watchdog", "owner", "lock"), 3),
    },
    "accepted": {
        "provider_accepted": (
            ("starter", "watchdog", "owner", "lock", "provider", "provider_custody"),
            4,
        ),
    },
    "cleanup_proven": {
        "after_acceptance": (
            ("starter", "watchdog", "owner", "lock", "provider", "provider_custody", "cleanup"),
            5,
        ),
        "before_acceptance": (
            ("starter", "watchdog", "owner", "lock", "containment", "cleanup"),
            4,
        ),
    },
    "released": {
        "after_acceptance": (
            (
                "starter",
                "watchdog",
                "owner",
                "lock",
                "provider",
                "provider_custody",
                "cleanup",
                "release",
            ),
            6,
        ),
        "before_acceptance": (
            ("starter", "watchdog", "owner", "lock", "containment", "cleanup", "release"),
            5,
        ),
    },
    "aborted": {
        "launcher_before_watchdog": (("starter", "failure"), 2),
        "watchdog_after_publication": (("starter", "watchdog", "failure"), 3),
    },
}

DEFAULT_PHASE_PATH = {
    "reserved": "watchdog_published",
    "lock_bound": "owner_bound",
    "accepted": "provider_accepted",
    "cleanup_proven": "after_acceptance",
    "released": "after_acceptance",
    "aborted": "watchdog_after_publication",
}

# Compatibility aliases for tests which intentionally exercise the ordinary
# accepted-provider route.
PHASE_FACTS = {phase: paths[DEFAULT_PHASE_PATH[phase]][0] for phase, paths in PHASE_PATHS.items()}
PHASE_REVISIONS = {phase: paths[DEFAULT_PHASE_PATH[phase]][1] for phase, paths in PHASE_PATHS.items()}


def facts_for(*names) -> dict:
    return {name: dict(FACT_LITERALS[name]) for name in names}


def make_episode(
    phase: str = "accepted",
    *,
    generation: int = 2,
    revision: int | None = None,
    path: str | None = None,
    deadline: str | None = None,
    **overrides,
) -> dict:
    """Build the literal episode a production transition sequence must produce."""
    if phase not in PHASES:
        raise KeyError(phase)
    path = path or DEFAULT_PHASE_PATH[phase]
    try:
        phase_facts, path_revision = PHASE_PATHS[phase][path]
    except KeyError:
        raise KeyError((phase, path)) from None
    episode = {
        FORMAT_FIELD: OWNER_EPISODE_FORMAT,
        "generation": generation,
        "revision": path_revision if revision is None else revision,
        "phase": phase,
        "phase_times": {name: "2026-08-11T00:00:00+00:00" for name in _phase_history(phase)},
    }
    episode.update(facts_for(*phase_facts))
    if deadline is not None:
        episode["deadline"] = deadline
    episode.update(overrides)
    return episode


def _phase_history(phase: str) -> tuple:
    if phase == "aborted":
        return ("reserved", "aborted")
    order = ("reserved", "lock_bound", "accepted", "cleanup_proven", "released")
    return order[: order.index(phase) + 1]


# --- production-name indirection -------------------------------------------

PRIMITIVE_NAMES = {
    "OWNER_EPISODE_FORMAT": "OWNER_EPISODE_FORMAT",
    "transition": "transition_owner_episode",
    "classify": "classify_owner_episode",
}


class _EpisodeApi:
    """Resolve one primitive per access so a missing name names itself."""

    def __getattr__(self, name):
        import spindle.namespace_owner as module

        try:
            attribute = PRIMITIVE_NAMES[name]
        except KeyError:  # pragma: no cover - the test suite fixes these names
            raise AttributeError(name) from None
        value = getattr(module, attribute, None)
        if value is None:
            pytest.fail(f"missing owner-episode primitive: spindle.namespace_owner.{attribute}")
        return value


@pytest.fixture
def episode_api():
    return _EpisodeApi()


# --- store helpers ----------------------------------------------------------


@dataclass
class EpisodeStore:
    root: Path
    held: list = field(default_factory=list)

    def spool_path(self, spool_id: str) -> Path:
        return self.root / f"{spool_id}.json"

    def lock_path(self, spool_id: str) -> Path:
        return self.root / f"{spool_id}.process-owner"

    def write(self, spool_id: str, *, status: str = "running", episode: dict | None = None, **fields) -> dict:
        record = {"id": spool_id, "status": status, "created_at": "2026-08-11T00:00:00", "spool_schema_version": 1}
        if episode is not None:
            record[EPISODE_KEY] = episode
        record.update(fields)
        self.spool_path(spool_id).write_text(json.dumps(record))
        return record

    def read(self, spool_id: str) -> dict:
        return json.loads(self.spool_path(spool_id).read_text())

    def episode(self, spool_id: str) -> dict | None:
        return self.read(spool_id).get(EPISODE_KEY)

    def bind_lock(self, spool_id: str, episode: dict) -> dict:
        """Create the real ownership inode this episode claims to have bound."""
        path = self.lock_path(spool_id)
        path.touch(mode=0o600)
        info = path.stat()
        episode["lock"] = {"device": info.st_dev, "inode": info.st_ino}
        if episode.get("release"):
            episode["release"] = {**episode["release"], "device": info.st_dev, "inode": info.st_ino}
        return episode

    def hold_lock(self, spool_id: str) -> int:
        fd = os.open(self.lock_path(spool_id), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(fd)
        return fd

    def close(self) -> None:
        for fd in self.held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self.held.clear()


@pytest.fixture
def episode_store():
    import spindle

    store = EpisodeStore(Path(spindle.SPINDLE_DIR))
    store.root.mkdir(parents=True, exist_ok=True)
    yield store
    store.close()


@dataclass
class GuardOrder:
    events: list = field(default_factory=list)

    @property
    def entries(self) -> list:
        return [name for name, event in self.events if event == "enter"]

    @staticmethod
    def open_stack(events) -> list:
        """Guards still open, outermost first, for one recorded prefix."""
        stack = []
        for name, event in events:
            if event == "enter":
                stack.append(name)
                continue
            for index in range(len(stack) - 1, -1, -1):
                if stack[index] == name:
                    del stack[index]
                    break
        return stack


@pytest.fixture
def guard_order(monkeypatch):
    """Record the guard order public control admission actually takes.

    The mailbox guard is replaced rather than wrapped: admission must own that
    critical section itself, and re-entering the real flock from one process
    would block instead of failing the assertion.
    """
    import spindle

    recorder = GuardOrder()
    real_spool_lock = spindle._spool_lock

    @contextmanager
    def mailbox_guard(_root, _spool_id):
        recorder.events.append(("mailbox", "enter"))
        try:
            yield
        finally:
            recorder.events.append(("mailbox", "exit"))

    @contextmanager
    def spool_lock(spool_id, blocking=True):
        recorder.events.append(("spool", "enter"))
        try:
            with real_spool_lock(spool_id, blocking) as acquired:
                yield acquired
        finally:
            recorder.events.append(("spool", "exit"))

    monkeypatch.setattr(spindle, "mailbox_guard", mailbox_guard, raising=False)
    monkeypatch.setattr(spindle, "_spool_lock", spool_lock)
    return recorder
