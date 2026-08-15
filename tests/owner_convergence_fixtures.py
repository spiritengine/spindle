"""Frozen Phase 2 contract model for owner-episode convergence.

This module holds the *oracle*, not an implementation sketch.  It states what a
converged owner-episode record must look like, which duties a terminal owes, and
which durable evidence each terminal origin is reduced from.  Nothing here reads
production control flow: every scenario is built from the durable artifacts a
producer leaves behind (spool record, episode facts, mailbox, sidecars, exit
carriers, ownership inode), so the contract survives any Family A implementation.

Traceability
------------
* campaign plan            brief-20260813-pkh6
* Phase 1 research         finding-20260813-6zyh   (sections 1-6)
* receipt decision         finding-20260813-xt37   (rejected_terminal, natural wins)
* six confirmed defects    finding-20260813-nah1
* convergence checkpoint   brief-20260812-ewry
* round-1 Fell triage      finding-20260813-zd21
* recorded decisions       DECISIONS.md 23, 26-34

Two names are unavoidable because they *are* the primitive boundary Phase 1
selected: the convergence module and its entry point.  Both are reached through
:func:`convergence_api` so a missing name fails one test with a precise message
instead of breaking collection for a whole module.

Every Phase 2 episode - matrix row, direct construction, crash setup, defect
regression - is built by :func:`causal_episode`, which is the only place the raw
``make_episode`` constructor may be reached from.  Phase 2 scenarios are read as
histories, so a durable fact left on a second clock is not untidiness but an
impossible history a converger may refuse; putting the normalisation and its
invariant on one path is what stops the next direct construction from
reintroducing one.  ``test_owner_convergence_writers`` enforces the boundary
mechanically.

Nothing here may skip.  A missing kernel facility is a broken contract
environment, not a reason to silently delete coverage, so every evidence helper
fails loudly instead.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.owner_episode_fixtures import PROVIDER_CUSTODY, make_episode

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- 1. the primitive boundary Phase 1 selected -----------------------------

CONVERGENCE_MODULE = "spindle.owner_episode_convergence"
CONVERGENCE_MODULE_FILE = "owner_episode_convergence.py"

#: Names the Phase 1 recommendation freezes into the module.  ``entry`` is the
#: production entry point; the rest are the result vocabulary consumers read.
CONVERGENCE_NAMES = {
    "converge": "converge_owner_episode",
    "ConvergenceResult": "ConvergenceResult",
    "Obligation": "Obligation",
    "ObserverIdentity": "ObserverIdentity",
    "CapturedOwnerEpisodeEvidence": "CapturedOwnerEpisodeEvidence",
    "TerminalProjection": "TerminalProjection",
}

#: Phase 1 section 6 selects ``converge_owner_episode(spool_id, observer)``.  The
#: observer is an explicit argument, not something the entry point derives for
#: itself: authority is a property of the caller, and a foreign observer has to
#: be expressible without editing the record.
OBSERVER_FACTORY = "for_this_process"

#: Field sets Phase 1 section 6 names for each public type.
RESULT_FIELDS = (
    "classification",
    "reason",
    "may_mutate",
    "refusal_reason",
    "terminal_state",
    "projection",
    "obligations",
    "local_errors",
)
#: "Obligation: kind, idempotency_key, intent, progress, completion evidence/error."
OBLIGATION_FIELDS = ("kind", "idempotency_key", "intent", "progress", "completion", "error")
#: "ObserverIdentity: pid, PID-namespace identity, local effect capabilities."
OBSERVER_FIELDS = ("pid", "namespace", "local_effects")
#: "CapturedOwnerEpisodeEvidence: serialized record/request/receipt/sidecar/output
#: view plus generation/revision and kernel observations."  The capture is a named
#: public type because every decision in this contract is taken from one frozen
#: view; a converger that re-reads a carrier mid-decision is reading two worlds.
CAPTURED_FIELDS = (
    "record",
    "episode",
    "requests",
    "receipts",
    "sidecars",
    "output",
    "generation",
    "revision",
    "observations",
)

#: Durable Family A additions to an owner-episode-v1 record.  The outcome and
#: the owed-duty set are committed in the *same* record, so no observable
#: terminal can exist without its obligations.
CONVERGENCE_KEY = "owner_convergence"
CONVERGENCE_FORMAT = "spindle.owner-convergence/1"

#: ConvergenceResult.classification (Phase 1 section 2, plan "state vocabulary").
CLASSIFICATIONS = ("active", "stopping", "terminalizable", "unverifiable", "store_unhealthy")

#: ConvergenceResult.terminal_state.
TERMINAL_STATES = ("none", "terminal_with_obligations_pending", "fully_converged")

#: Durable obligation kinds (Phase 1 section 2, "Durable versus process-local").
#: ``local_handle`` is deliberately *not* here: it is process-local, attempted by
#: every terminal observer that owns a handle, and never claimed exactly-once.
DURABLE_OBLIGATIONS = ("control_receipts", "failed_shard_preservation", "natural_transcript")
LOCAL_DUTY = "local_handle_reap"

OBLIGATION_PROGRESS = ("pending", "complete")

#: The disposition rule Patrick fixed in finding-20260813-xt37 plus the two
#: dispositions the owner already uses.  This is the whole receipt intent: a
#: converger reading it from the record alone can settle every captured request.
RECEIPT_DISPOSITION = {
    "winner": "cleaned",
    "acknowledged": "cleaned",
    "sibling": "rejected_superseded",
    "no_winner": "rejected_terminal",
}
NO_WINNER_RECEIPT = RECEIPT_DISPOSITION["no_winner"]
SIBLING_RECEIPT = RECEIPT_DISPOSITION["sibling"]

#: The stable reason production already writes with a preserved failed shard.
SHARD_PRESERVED_REASON = "automatic cleanup disabled after agent failure"


class _ConvergenceApi:
    """Resolve one convergence primitive per access so a gap names itself."""

    def __getattr__(self, name):
        import importlib

        try:
            attribute = CONVERGENCE_NAMES[name]
        except KeyError:  # pragma: no cover - the contract fixes these names
            raise AttributeError(name) from None
        try:
            module = importlib.import_module(CONVERGENCE_MODULE)
        except ImportError:
            pytest.fail(
                f"missing owner-episode convergence module: {CONVERGENCE_MODULE} "
                f"(Phase 1 finding-20260813-6zyh section 6 selects this module)"
            )
        value = getattr(module, attribute, None)
        if value is None:
            pytest.fail(f"missing convergence primitive: {CONVERGENCE_MODULE}.{attribute}")
        return value

    def local_observer(self):
        """The observer identity this process converges with."""
        factory = getattr(self.ObserverIdentity, OBSERVER_FACTORY, None)
        if factory is None:
            pytest.fail(
                f"missing observer constructor: {CONVERGENCE_MODULE}.ObserverIdentity.{OBSERVER_FACTORY} "
                f"(convergence takes an explicit observer, so one must be constructible)"
            )
        return factory()

    def converge_here(self, spool_id: str):
        """One convergence pass under this process's own observer identity."""
        return self.converge(spool_id, self.local_observer())


@pytest.fixture
def convergence_api():
    return _ConvergenceApi()


def field_names(value) -> tuple:
    """Public field names of a dataclass instance, class, or plain object."""
    fields = getattr(value, "__dataclass_fields__", None)
    if fields is not None:
        return tuple(fields)
    annotations = getattr(value, "__annotations__", None)
    if annotations:
        return tuple(name for name in annotations if not name.startswith("_"))
    return tuple(name for name in vars(value) if not name.startswith("_"))


# --- 2. observers -----------------------------------------------------------

SAME_NAMESPACE = "same_namespace"
FOREIGN_NAMESPACE = "foreign_namespace"
OBSERVERS = (SAME_NAMESPACE, FOREIGN_NAMESPACE)


def require_pid_namespaces():
    """This observer's PID namespace, or a failure that names what is missing.

    The whole observer-authority contract is "same snapshot, different observer",
    which is only expressible when the kernel reports a namespace coordinate.
    Skipping here would delete the core of Decision 28 silently.
    """
    from spindle.namespace_owner import capture_pid_namespace

    identity = capture_pid_namespace()
    if not identity.is_supported:
        pytest.fail(
            "PID namespaces are unavailable, so observer authority cannot be exercised at all. "
            "The owner-episode contract requires a kernel that reports /proc/self/ns/pid."
        )
    return identity


@contextmanager
def observing_as(kind: str, monkeypatch):
    """Run the body as an observer inside (or outside) the recorded namespace.

    The *record* is untouched: only the observer's own PID-namespace coordinate
    moves.  That is the honest way to ask whether one durable snapshot keeps its
    classification while losing mutation authority.

    The move is undone on the way out, so a test can compare what a stranger and
    a local observer each do with the same record.
    """
    import spindle
    import spindle.namespace_owner as owner_module

    local = require_pid_namespaces()
    if kind == SAME_NAMESPACE:
        yield
        return

    foreign_identity = owner_module.NamespaceIdentity.supported(local.device, local.inode + 1)

    def foreign(*args, **kwargs):
        return foreign_identity

    with monkeypatch.context() as patched:
        patched.setattr(owner_module, "capture_pid_namespace", foreign)
        patched.setattr(spindle, "capture_pid_namespace", foreign, raising=False)
        yield


# --- 3. deterministic process identities ------------------------------------

#: Every actor gets its own PID and birth token.  One shared identity across
#: starter, watchdog, owner and provider hides exactly the role-confusion class
#: defect 5 is made of, so the roles are kept apart everywhere.
ROLES = ("starter", "watchdog", "owner", "provider")

_DEAD_IDENTITIES: dict[str, dict] = {}
_ZOMBIE_PIDS: list[int] = []
_IDENTITY_LOCK = threading.Lock()


def _fork_zombie() -> int:
    """Fork a child, wait for it to exit, and deliberately leave it unreaped.

    An unreaped exited child is the one process state that is *provably* dead and
    whose PID cannot be recycled underneath the test: the kernel keeps the slot
    until someone waits on it.  Hunting for an unused PID number, as this fixture
    used to, is neither deterministic nor reuse-proof.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to pytest
        os._exit(0)
    _ZOMBIE_PIDS.append(pid)
    descriptor = os.pidfd_open(pid)
    try:
        readable, _, _ = select.select([descriptor], [], [], 10.0)
        if not readable:
            pytest.fail(f"forked contract child {pid} did not exit; dead-process evidence is unavailable")
    finally:
        os.close(descriptor)
    return pid


def dead_process_fact(role: str = "owner") -> dict:
    """A provably dead, role-distinct identity in this observer's namespace."""
    from spindle.namespace_owner import read_proc_starttime

    namespace = require_pid_namespaces().to_dict()
    with _IDENTITY_LOCK:
        fact = _DEAD_IDENTITIES.get(role)
        if fact is None:
            pid = _fork_zombie()
            try:
                birth_token = read_proc_starttime(pid)
            except OSError as exc:  # pragma: no cover - /proc is required
                pytest.fail(f"/proc/{pid}/stat unreadable for a dead-process fact: {exc!r}")
            fact = {"pid": pid, "birth_token": birth_token, "namespace": namespace}
            _DEAD_IDENTITIES[role] = fact
    return dict(fact)


@pytest.fixture(scope="session", autouse=True)
def _reap_contract_zombies():
    """Reap the identity children once the session that needed them is over."""
    yield
    for pid in _ZOMBIE_PIDS:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError) as exc:  # pragma: no cover - best effort
            if getattr(exc, "errno", None) not in {errno.ECHILD, None}:
                raise
    _ZOMBIE_PIDS.clear()


def live_process_fact() -> dict:
    """This very process, so liveness is positively provable."""
    import spindle

    return {
        "pid": os.getpid(),
        "birth_token": spindle._process_start_time(os.getpid()),
        "namespace": require_pid_namespaces().to_dict(),
    }


def localize_identities(episode: dict) -> dict:
    """Give each recorded role its own proven-dead, locally-namespaced identity.

    ``owner_episode_fixtures`` uses synthetic namespace coordinates so the pure
    transition tests stay hermetic.  Convergence scenarios need the opposite:
    a record an observer in *this* namespace can actually adjudicate, so the
    foreign-observer contract can move the observer rather than the record.
    """
    from tests.owner_episode_fixtures import NAMESPACE

    for role in ROLES:
        fact = episode.get(role)
        if not isinstance(fact, dict) or fact.get("namespace") != NAMESPACE:
            continue
        dead = dead_process_fact(role)
        replacement = dict(fact)
        replacement.update(pid=dead["pid"], birth_token=dead["birth_token"], namespace=dead["namespace"])
        if "pgid" in replacement:
            replacement["pgid"] = dead["pid"]
        episode[role] = replacement
    return episode


# --- 4. the public projection ----------------------------------------------

#: Sentinel: the field must be absent or null.
NULL = "<null>"
#: Sentinel: the field must carry the value the natural proposal supplied.
FROM_PROPOSAL = "<from-proposal>"
#: Sentinel: the field must carry the canonical provider exit code.
CANONICAL_EXIT = "<canonical-exit>"

#: Public outcome fields frozen for every terminal origin (plan "Public
#: projection"; Phase 1 section 2 "Terminal origin closure").
OUTCOME_FIELDS = ("status", "error", "error_kind", "result", "exit_code")
LIFECYCLE_FIELDS = ("ownership_state", "transport_state", "normalized_terminal_kind")

#: Immutable once validly published.  Convergence may only advance obligations.
IMMUTABLE_FIELDS = OUTCOME_FIELDS + ("completed_at", "terminal_origin", "terminal_provenance")

#: "TerminalProjection: all protected public fields and immutable provenance."
#: The lifecycle mirror travels with the projection because it is published in
#: the same atomic commit and is protected by the same closed-writer rule.
PROJECTION_FIELDS = IMMUTABLE_FIELDS + ("lifecycle",)

#: terminal_provenance keys, frozen from Phase 1 section 2: "records
#: owner_generation, deciding episode revision, request id/kind when any,
#: provider-exit carrier, and owner/watchdog failure kind when any".
PROVENANCE_KEYS = (
    "owner_generation",
    "episode_revision",
    "episode_phase",
    "request_id",
    "request_kind",
    "provider_exit_carrier",
    "failure_kind",
)

#: Provider-exit carriers in precedence order (Phase 1 section 2, rule 4).
EXIT_CARRIERS = ("episode_cleanup", "owner_exit", "exit_file")


def outcome_of(record: dict) -> dict:
    """The public outcome a consumer reads, normalised for comparison."""
    lifecycle = record.get("lifecycle") or {}
    projection = {name: record.get(name, NULL) for name in OUTCOME_FIELDS}
    for name in OUTCOME_FIELDS:
        if projection[name] is None:
            projection[name] = NULL
    projection.update({name: lifecycle.get(name, NULL) for name in LIFECYCLE_FIELDS})
    projection["public_stop_state"] = lifecycle.get("public_stop_state", NULL)
    return projection


def provenance_of(record: dict) -> dict:
    provenance = record.get("terminal_provenance") or {}
    return {"terminal_origin": record.get("terminal_origin", NULL)} | {
        key: provenance.get(key, NULL) for key in PROVENANCE_KEYS
    }


def immutable_of(record: dict) -> dict:
    return {name: record.get(name, NULL) for name in IMMUTABLE_FIELDS} | {
        f"lifecycle.{name}": (record.get("lifecycle") or {}).get(name, NULL) for name in LIFECYCLE_FIELDS
    }


def preservation_of(record: dict) -> dict:
    """tags/session/cost/background-task facts a terminal must keep or clear."""
    projection = {
        "tags": record.get("tags", NULL),
        "session_id": record.get("session_id", NULL),
        "cost": record.get("cost", NULL),
        "pending_background_tasks": record.get("pending_background_tasks", NULL),
    }
    return {name: NULL if value is None else value for name, value in projection.items()}


# --- 5. the durable obligation payload --------------------------------------


def obligation_block(record: dict) -> dict:
    block = record.get(CONVERGENCE_KEY)
    return block if isinstance(block, dict) else {}


def obligations_of(record: dict) -> dict:
    """Every durable obligation entry recorded with the outcome, by kind.

    Phase 1 section 2 names what each entry has to carry - executor intent,
    idempotency key, progress, and completion evidence or error - because a
    fresh process that finds only this record has to be able to finish the work.
    An entry that carries kind and progress alone is a decoration, and a Family C
    implementation which re-probes every carrier would pass on it.
    """
    obligations = obligation_block(record).get("obligations") or {}
    if isinstance(obligations, list):
        obligations = {item.get("kind"): item for item in obligations if isinstance(item, dict)}
    if not isinstance(obligations, dict):
        return {}
    return {kind: entry for kind, entry in obligations.items() if isinstance(entry, dict)}


def obligation_progress_of(record: dict) -> dict:
    return {kind: entry.get("progress") for kind, entry in obligations_of(record).items()}


def content_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def receipt_keys(generation: int, request_ids) -> dict:
    """The receipt duty's idempotency keys: Phase 1's ``generation/request_id``.

    One aggregate key cannot express partial progress.  A process interrupted
    between two receipts has to read *which* request is still owed, and
    ``3/control_receipts`` says only that something about receipts is unfinished
    - which forces the Family C rescan of every request Phase 1 rejected.
    """
    return {request_id: f"{generation}/{request_id}" for request_id in sorted(request_ids)}


def receipt_intent(generation: int, requests: dict, winning_request_id=None) -> dict:
    """Phase 1: "committed request IDs, winner, generation, and disposition rule".

    Frozen per request, because the request is the unit of settlement: each entry
    names the kind that was admitted, and the winner plus the rule decide what
    that entry settles as.  The payload stays bounded - one small entry per
    current-generation request, and nothing that grows with provider output.
    """
    return {
        "owner_generation": generation,
        "winning_request_id": winning_request_id,
        "disposition": dict(RECEIPT_DISPOSITION),
        "requests": {request_id: {"kind": kind} for request_id, kind in sorted(requests.items())},
    }


def receipt_disposition(request_id: str, winning_request_id=None) -> str:
    """What one captured request settles as under the frozen rule."""
    if winning_request_id is None:
        return RECEIPT_DISPOSITION["no_winner"]
    if request_id == winning_request_id:
        return RECEIPT_DISPOSITION["winner"]
    return RECEIPT_DISPOSITION["sibling"]


def shard_intent(generation: int, worktree: Path) -> dict:
    """Phase 1: "terminal error or timeout plus shard_created_by_spool"."""
    return {
        "owner_generation": generation,
        "worktree_path": str(worktree),
        "reason": SHARD_PRESERVED_REASON,
    }


def transcript_intent(generation: int, stdout: str, session_id, *, source: Path) -> dict:
    """Phase 1: "accepted natural proposal plus captured stdout and session_id".

    The bytes themselves stay out of the record - a transcript can be megabytes
    and this record is re-read on every convergence pass - so the intent names
    the durable capture they are replayed from, with the digest and length that
    prove a retry persisted the same bytes the terminal was reduced from.  A
    digest alone is not replayable: a fresh process holding only the record would
    have nothing to hash and no way to find the source it must hash.
    """
    return {
        "owner_generation": generation,
        "session_id": session_id,
        "source": "stdout_capture",
        "source_path": str(source),
        "digest": content_digest(stdout),
        "size": len(stdout.encode()),
    }


def idempotency_key(kind: str, generation: int, *, digest: str | None = None) -> str:
    """The stable single-value keys Phase 1 section 2 names.

    ``control_receipts`` is deliberately absent: it is keyed per request through
    :func:`receipt_keys`, because that is the unit its retries are safe in.
    """
    if kind == "failed_shard_preservation":
        return f"{generation}/preserve_failed_shard"
    if kind == "natural_transcript":
        return f"{generation}/transcript/{digest}"
    raise KeyError(kind)  # pragma: no cover - DURABLE_OBLIGATIONS fixes the kinds


def assert_obligation_payload(entry: dict, *, kind: str, key: str, intent: dict) -> None:
    """Every owed duty carries a complete, recoverable durable payload."""
    assert set(entry) == set(OBLIGATION_FIELDS), (
        f"obligation {kind} recorded {sorted(entry)}; a recoverable duty needs exactly {sorted(OBLIGATION_FIELDS)}"
    )
    assert entry["kind"] == kind
    assert entry["idempotency_key"] == key, (
        f"obligation {kind} keyed {entry['idempotency_key']!r}; retry safety needs the stable key {key!r}"
    )
    assert entry["intent"] == intent, (
        f"obligation {kind} intent {entry['intent']!r} cannot be replayed from the record alone; expected {intent!r}"
    )
    assert entry["progress"] in OBLIGATION_PROGRESS
    if entry["progress"] == "complete":
        assert entry["completion"], f"obligation {kind} claims completion with no evidence"
        assert entry["error"] is None, f"obligation {kind} is complete and still carries {entry['error']!r}"
    else:
        assert entry["completion"] in (None, {}, ""), f"obligation {kind} is pending but carries completion evidence"


# --- 6. the terminal-origin matrix ------------------------------------------

TIMEOUT_SECONDS = 17
TIMEOUT_ERROR = f"Timeout after {TIMEOUT_SECONDS}s"
CANCELLED_ERROR = "Cancelled"
OWNER_LOSS_ERROR = "Logical owner exited before terminal publication"
WATCHDOG_LOSS_ERROR = "Containment watchdog exited before terminal publication"
NEVER_STARTED_ERROR = "spawn timeout - never started"

NATURAL_RESULT = "the natural answer"
NATURAL_SESSION = "session-natural"
NATURAL_COST = 0.5
#: A stale *float* cost: production's cost field is a number, and a dict-valued
#: one would fail type-invalid rather than because the clearing rule is missing.
STALE_COST = 9.99
STALE_SESSION = "session-stale"
STALE_BACKGROUND = [{"id": "stale-task", "source": "stale"}]


@dataclass(frozen=True)
class OriginContract:
    """One row of the frozen phase/terminal-origin projection matrix."""

    origin: str
    #: how the durable evidence is built (see ``build_scenario``)
    evidence: str
    status: str
    error: object
    error_kind: object
    result: object
    exit_code: object
    normalized_terminal_kind: str
    transport_state: str
    #: episode phase the decision is taken from; distinguishes the two
    #: deadline-before-provider windows without inventing new vocabulary.
    episode_phase: str
    request_kind: object = NULL
    provider_exit_carrier: object = NULL
    failure_kind: object = NULL
    #: natural terminals keep a proposal's session/cost/background facts; every
    #: nonnatural terminal clears the stale values it inherited.
    preserves_proposal: bool = False
    ownership_state: str = "released"
    #: record status before convergence runs
    initial_status: str = "running"

    def expected_outcome(self, *, exit_code=None, proposal_result=None) -> dict:
        return {
            "status": self.status,
            "error": self.error,
            "error_kind": self.error_kind,
            "result": proposal_result if self.result is FROM_PROPOSAL else self.result,
            "exit_code": exit_code if self.exit_code is CANONICAL_EXIT else self.exit_code,
            "ownership_state": self.ownership_state,
            "transport_state": self.transport_state,
            "normalized_terminal_kind": self.normalized_terminal_kind,
            "public_stop_state": NULL,
        }

    def expected_provenance(self, *, generation: int, revision: int, request_id=NULL) -> dict:
        return {
            "terminal_origin": self.origin,
            "owner_generation": generation,
            "episode_revision": revision,
            "episode_phase": self.episode_phase,
            "request_id": request_id if self.request_kind is not NULL else NULL,
            "request_kind": self.request_kind,
            "provider_exit_carrier": self.provider_exit_carrier,
            "failure_kind": self.failure_kind,
        }


ORIGINS = (
    OriginContract(
        origin="natural_success",
        evidence="natural",
        status="complete",
        error=NULL,
        error_kind=NULL,
        result=FROM_PROPOSAL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="complete",
        transport_state="reaped",
        episode_phase="released",
        provider_exit_carrier="episode_cleanup",
        preserves_proposal=True,
    ),
    OriginContract(
        origin="natural_failure",
        evidence="natural_failure",
        status="error",
        error="the provider failed",
        # Phase 1: "proposal error and proposal-specific error_kind, or
        # provider_failed if none".  This proposal names no kind of its own.
        error_kind="provider_failed",
        result="the provider failed",
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="released",
        provider_exit_carrier="episode_cleanup",
        preserves_proposal=True,
    ),
    OriginContract(
        origin="natural_failure",
        evidence="natural_signal",
        status="error",
        error="the provider failed",
        error_kind="provider_failed",
        result="the provider failed",
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="released",
        provider_exit_carrier="episode_cleanup",
        preserves_proposal=True,
    ),
    OriginContract(
        origin="natural_success",
        evidence="natural_then_owner_crash",
        status="complete",
        error=NULL,
        error_kind=NULL,
        result=FROM_PROPOSAL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="complete",
        transport_state="reaped",
        episode_phase="released",
        provider_exit_carrier="episode_cleanup",
        preserves_proposal=True,
    ),
    OriginContract(
        origin="accepted_cancel",
        evidence="accepted_cancel",
        status="error",
        error=CANCELLED_ERROR,
        error_kind="cancelled",
        result=NULL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="cancelled",
        transport_state="reaped",
        episode_phase="released",
        request_kind="cancel",
        provider_exit_carrier="episode_cleanup",
    ),
    OriginContract(
        origin="accepted_drop",
        evidence="accepted_drop",
        status="error",
        error=CANCELLED_ERROR,
        error_kind="cancelled",
        result=NULL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="cancelled",
        transport_state="reaped",
        episode_phase="released",
        request_kind="drop",
        provider_exit_carrier="episode_cleanup",
    ),
    OriginContract(
        origin="accepted_timeout",
        evidence="accepted_timeout",
        status="timeout",
        error=TIMEOUT_ERROR,
        error_kind="timeout",
        result=NULL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="timeout",
        transport_state="reaped",
        episode_phase="released",
        request_kind="timeout",
        provider_exit_carrier="episode_cleanup",
    ),
    OriginContract(
        origin="deadline_expired_before_provider_start",
        evidence="deadline_before_binding",
        status="timeout",
        error=TIMEOUT_ERROR,
        error_kind="deadline_expired_before_provider_start",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="timeout",
        transport_state="reaped",
        episode_phase="aborted",
        failure_kind="deadline_expired_before_provider_start",
    ),
    OriginContract(
        origin="deadline_expired_before_provider_start",
        evidence="deadline_after_binding",
        status="timeout",
        error=TIMEOUT_ERROR,
        error_kind="deadline_expired_before_provider_start",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="timeout",
        transport_state="reaped",
        episode_phase="released",
        failure_kind="deadline_expired_before_provider_start",
    ),
    OriginContract(
        origin="launcher_pre_spawn_failure",
        evidence="launcher_pre_spawn_failure",
        status="error",
        error="spawn failed: no such executable",
        error_kind="launcher_pre_spawn_failure",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="aborted",
        failure_kind="launcher_pre_spawn_failure",
        initial_status="pending",
    ),
    OriginContract(
        origin="owner_preacceptance_failure",
        evidence="owner_preacceptance_failure",
        status="error",
        error="logical owner exited before binding",
        error_kind="owner_preacceptance_failure",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="aborted",
        failure_kind="owner_preacceptance_failure",
    ),
    OriginContract(
        origin="provider_spawn_failure_after_binding",
        evidence="provider_spawn_failure",
        status="error",
        error="provider spawn failed: exec format error",
        error_kind="provider_spawn_failure",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="released",
        failure_kind="provider_spawn_failure",
    ),
    OriginContract(
        origin="stale_dead_reservation",
        evidence="stale_dead_reservation",
        status="error",
        error=NEVER_STARTED_ERROR,
        error_kind="stale_dead_reservation",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="failed",
        transport_state="reaped",
        episode_phase="reserved",
        initial_status="pending",
    ),
    OriginContract(
        origin="owner_crash_before_cleanup",
        evidence="owner_crash_no_sidecar",
        status="error",
        error=OWNER_LOSS_ERROR,
        error_kind="owner_transport_loss",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="indeterminate",
        transport_state="lost",
        episode_phase="released",
        failure_kind="owner_crash",
    ),
    OriginContract(
        origin="owner_crash_before_cleanup",
        evidence="owner_crash_with_sidecar",
        status="error",
        error=OWNER_LOSS_ERROR,
        error_kind="owner_transport_loss",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="indeterminate",
        transport_state="lost",
        episode_phase="released",
        failure_kind="owner_crash",
    ),
    OriginContract(
        origin="owner_crash_after_acknowledged_cleanup",
        evidence="owner_crash_after_cleanup",
        status="timeout",
        error=TIMEOUT_ERROR,
        error_kind="timeout",
        result=NULL,
        exit_code=CANONICAL_EXIT,
        normalized_terminal_kind="timeout",
        transport_state="lost",
        episode_phase="released",
        request_kind="timeout",
        provider_exit_carrier="episode_cleanup",
        failure_kind="owner_crashed_after_cleanup",
    ),
    OriginContract(
        origin="watchdog_parent_loss_before_binding",
        evidence="watchdog_loss_before_binding",
        status="error",
        error=WATCHDOG_LOSS_ERROR,
        error_kind="watchdog_parent_loss",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="failed",
        transport_state="lost",
        episode_phase="aborted",
        failure_kind="watchdog_parent_loss",
    ),
    OriginContract(
        origin="watchdog_parent_loss_after_binding",
        evidence="watchdog_loss_after_binding",
        status="error",
        error=WATCHDOG_LOSS_ERROR,
        error_kind="owner_transport_loss",
        result=NULL,
        exit_code=NULL,
        normalized_terminal_kind="indeterminate",
        transport_state="lost",
        episode_phase="released",
        failure_kind="watchdog_parent_loss",
    ),
)

ORIGIN_IDS = tuple(row.evidence for row in ORIGINS)
assert len(set(ORIGIN_IDS)) == len(ORIGIN_IDS), "each matrix row needs a unique evidence id"


# --- 7. one ordered scenario clock, and scenario construction ----------------

GENERATION = 3

NATURAL_STDOUT = json.dumps(
    {
        "result": NATURAL_RESULT,
        "session_id": NATURAL_SESSION,
        "total_cost_usd": NATURAL_COST,
    }
)
NATURAL_FAILURE_STDOUT = json.dumps(
    {
        "result": "the provider failed",
        "is_error": True,
        "session_id": NATURAL_SESSION,
        "total_cost_usd": NATURAL_COST,
    }
)

#: Evidence ids whose durable history includes an accepted control request.
ACCEPTED_EVIDENCE = {"accepted_cancel", "accepted_drop", "accepted_timeout", "owner_crash_after_cleanup"}

#: One ordered wall clock for every scenario.  Phases, the control history and
#: the cleanup facts all come from the same run, so the durable artifacts
#: describe a history a producer could really have left: the request is
#: published while the owner is accepting control, the owner acknowledges it,
#: the child exits, and only then is the release proved.  A receipt acknowledged
#: at 00:00:04 for a request published afterwards is not untidy - it is
#: impossible, and a converger is entitled to refuse it as corrupt rather than
#: treat it as evidence.
#:
#: The clock is a mapping of named instants rather than loose literals so that a
#: scenario needing a different anchor - a history whose cleanup really was
#: observed at this moment, because the requests before it went through real
#: admission - moves the whole history together instead of stamping one fact off
#: on its own.
SCENARIO_EPOCH = datetime(2026, 8, 13, tzinfo=timezone.utc)

#: Each instant of a generation's life, as seconds after the epoch.  Two names
#: may share a second when they are the same event seen from two sides: proving
#: cleanup *is* observing the child exit, and a failure that ends an episode
#: arrives with the acknowledgement it answered.
CLOCK_OFFSETS = {
    "reserved": 0,
    "lock_bound": 1,
    # The owner captures custody before the lock-bound -> accepted transition
    # commits its phase timestamp.
    "provider_custody": 1.5,
    "accepted": 2,
    "requested": 3,
    "acknowledged": 4,
    # accepted -> accepted publishes the winner and acknowledgement, replacing
    # the phase timestamp after those facts have been captured.
    "accepted_control": 4.5,
    "failed": 4,
    "child_exit": 5,
    "cleanup_proven": 5,
    # An owner that dies *after* proving cleanup dies after that proof exists.
    "crashed_after_cleanup": 6,
    "released": 7,
    "sidecar": 9,
}


def scenario_clock(epoch: datetime = SCENARIO_EPOCH) -> dict:
    """One ordered clock anchored at *epoch*, by instant name."""
    return {name: (epoch + timedelta(seconds=offset)).isoformat() for name, offset in CLOCK_OFFSETS.items()}


SCENARIO_CLOCK = scenario_clock()

RESERVED_AT = SCENARIO_CLOCK["reserved"]
BOUND_AT = SCENARIO_CLOCK["lock_bound"]
PROVIDER_CUSTODY_AT = SCENARIO_CLOCK["provider_custody"]
ACCEPTED_AT = SCENARIO_CLOCK["accepted"]
REQUESTED_AT = SCENARIO_CLOCK["requested"]
ACKNOWLEDGED_AT = SCENARIO_CLOCK["acknowledged"]
ACCEPTED_CONTROL_AT = SCENARIO_CLOCK["accepted_control"]
FAILED_AT = SCENARIO_CLOCK["failed"]
CHILD_EXIT_AT = SCENARIO_CLOCK["child_exit"]
CLEANUP_PROVEN_AT = SCENARIO_CLOCK["cleanup_proven"]
CRASHED_AFTER_CLEANUP_AT = SCENARIO_CLOCK["crashed_after_cleanup"]
RELEASED_AT = SCENARIO_CLOCK["released"]
SIDECAR_AT = SCENARIO_CLOCK["sidecar"]


def live_cleanup_clock() -> dict:
    """The scenario clock with everything from the child exit onward at *now*.

    A scenario that admits control requests through real admission gets their
    wall-clock ``requested_at``.  An owner that then observes its child exit did
    so after those requests, which only the real clock can express; the phases
    the owner had already reached stay where the frozen scenario put them.  The
    anchor never moves back before the frozen child exit, so a run that starts
    inside the epoch's own first seconds still leaves an ordered history rather
    than a cleanup that precedes the acknowledgement it followed.
    """
    anchor = max(datetime.now(timezone.utc), datetime.fromisoformat(SCENARIO_CLOCK["child_exit"]))
    epoch = anchor - timedelta(seconds=CLOCK_OFFSETS["child_exit"])
    later = scenario_clock(epoch)
    return {
        name: later[name] if CLOCK_OFFSETS[name] >= CLOCK_OFFSETS["child_exit"] else stamp
        for name, stamp in SCENARIO_CLOCK.items()
    }


#: When each episode phase was reached, in instants of the clock.
#: ``make_episode`` stamps one literal time for every phase it builds, which
#: cannot order a control history against the phase that admitted it.
PHASE_INSTANTS = {
    "reserved": "reserved",
    "lock_bound": "lock_bound",
    "accepted": "accepted",
    "cleanup_proven": "cleanup_proven",
    "released": "released",
    "aborted": "failed",
}

#: The phase order a single generation passes through.  ``aborted`` closes the
#: other branch, and no history holds both it and a release.
PHASE_ORDER = ("reserved", "lock_bound", "accepted", "cleanup_proven", "released", "aborted")

#: Every timestamped durable episode fact, with the instant it is observed at.
#: This is the whole surface a scenario clock has to cover: a fact missing from
#: here keeps whatever literal ``make_episode`` gave it, which is how a history
#: acquires two clocks.
FACT_INSTANTS = (
    ("provider_custody", "published_at", "provider_custody"),
    ("acknowledgement", "acknowledged_at", "acknowledged"),
    ("failure", "observed_at", "failed"),
    ("containment", "observed_at", "child_exit"),
    ("cleanup", "child_exit_observed_at", "child_exit"),
    ("release", "released_at", "released"),
)


def cleanup_fact(outcome: str, *, provider_exit_code=None, reaped: bool = True, clock: dict = SCENARIO_CLOCK) -> dict:
    return {
        "outcome": outcome,
        "provider_reaped": reaped,
        "adopted_children_reaped": 0,
        "child_exit_observed_at": clock["child_exit"],
        "provider_exit_code": provider_exit_code,
    }


def failure_fact(kind: str, detail: str, *, at: str = "failed", clock: dict = SCENARIO_CLOCK) -> dict:
    return {"kind": kind, "detail": detail, "observed_at": clock[at]}


def containment_fact(clock: dict = SCENARIO_CLOCK) -> dict:
    return {"contained": True, "adopted_children_reaped": 0, "observed_at": clock["child_exit"]}


def acknowledgement_fact(clock: dict = SCENARIO_CLOCK) -> dict:
    return {"acknowledged_at": clock["acknowledged"]}


def custody_fact(clock: dict = SCENARIO_CLOCK) -> dict:
    """The custody the owner publishes with the transition that accepts a provider.

    Only the stamp is this module's business: what custody *is* - the pidfd and
    the containment it was taken under - belongs to the pure fixture, and
    restating it here would be a second source for the same fact.
    """
    return dict(PROVIDER_CUSTODY, published_at=clock["provider_custody"])


def release_fact(lock: dict, *, proved_by: str = "reconciler", clock: dict = SCENARIO_CLOCK) -> dict:
    """The release a reconciler publishes over the exact inode it observed free."""
    return {
        "device": lock["device"],
        "inode": lock["inode"],
        "proved_by": proved_by,
        "released_at": clock["released"],
    }


def request_fact(kind: str, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "kind": kind,
        "desired_terminal_kind": "timeout" if kind == "timeout" else "cancelled",
    }


def _stamps_at(instant: str) -> tuple:
    """The durable fact stamps that report *instant*, read off the causal model.

    The child exit is one event with two observers - the owner's cleanup fact
    and the watchdog's containment fact - so a rule stated against "the child
    exit" has to hold against whichever of them a history actually carries.
    """
    return tuple(f"{fact}.{field_name}" for fact, field_name, named in FACT_INSTANTS if named == instant)


def assert_causal_episode(episode: dict, *, clock: dict = SCENARIO_CLOCK, subject: str = "episode") -> dict:
    """Prove one built episode is a history a producer could really have left.

    Two separate statements, because they fail differently.  Every stamp has to
    be a named instant of *one* clock - a fact left on the literal clock
    ``make_episode`` ships is the drift this builder exists to stop, and it is
    invisible to any ordering check that only compares facts to each other.  And
    the instants those names pick out have to describe a possible run.

    That second statement is read off the transition table, which fixes the
    window each fact can be published in: phases reached in order, nothing
    before this generation's reservation, custody taken by the very transition
    that accepts the provider, the acknowledgement given while the owner is
    still in that accepted phase, both of them before the child exit that ends
    it, cleanup before release, and no episode fact at all after the release.
    That last rule is what keeps custody and the acknowledgement out of the
    window after the release too, so neither has to say it a second time.
    """
    instants = set(clock.values())
    stamps = {}
    for fact, field_name, _instant in FACT_INSTANTS:
        value = (episode.get(fact) or {}).get(field_name)
        if value is not None:
            stamps[f"{fact}.{field_name}"] = value
    for name, stamp in {**(episode.get("phase_times") or {}), **stamps}.items():
        assert stamp in instants, (
            f"{subject} stamps {name} at {stamp!r}, which is no instant of this scenario's clock; "
            f"a durable fact on its own clock is a history no producer could leave"
        )

    times = {name: datetime.fromisoformat(stamp) for name, stamp in stamps.items()}
    phase_times = {name: datetime.fromisoformat(stamp) for name, stamp in (episode.get("phase_times") or {}).items()}
    ordered = [phase_times[name] for name in PHASE_ORDER if name in phase_times]
    assert ordered == sorted(ordered), f"{subject} reaches its phases out of order: {episode.get('phase_times')}"

    reserved_at = phase_times.get("reserved")
    if reserved_at is not None:
        for name, observed_at in times.items():
            assert observed_at >= reserved_at, f"{subject} {name} predates this generation's reservation"
    accepted_at = phase_times.get("accepted")
    acknowledged_at = times.get("acknowledgement.acknowledged_at")
    if acknowledged_at is not None:
        assert accepted_at is not None, f"{subject} acknowledged a request without ever accepting control"
        assert episode.get("winning_request") is not None, (
            f"{subject} carries an acknowledgement without the winning request published beside it"
        )
    elif episode.get("winning_request") is not None:
        raise AssertionError(f"{subject} carries a winning request without the acknowledgement published beside it")
    observed_child_exit = [times[name] for name in _stamps_at("child_exit") if name in times]
    child_exit_at = min(observed_child_exit) if observed_child_exit else None
    if acknowledged_at is not None and child_exit_at is not None:
        assert acknowledged_at <= child_exit_at, (
            f"{subject} observed its child exit before it acknowledged the request that stopped it"
        )
    if acknowledged_at is not None:
        assert acknowledged_at <= accepted_at, (
            f"{subject} committed accepted control before recording its acknowledgement"
        )
    custody_at = times.get("provider_custody.published_at")
    if custody_at is not None:
        assert accepted_at is not None, f"{subject} published provider custody without ever accepting the provider"
        lock_bound_at = phase_times.get("lock_bound")
        if lock_bound_at is not None:
            assert custody_at >= lock_bound_at, f"{subject} published provider custody before binding ownership"
    if custody_at is not None and child_exit_at is not None:
        assert custody_at <= child_exit_at, f"{subject} took custody of a provider that had already exited"
    if custody_at is not None:
        assert custody_at <= accepted_at, f"{subject} committed provider acceptance before recording its custody"
    if custody_at is not None and acknowledged_at is not None:
        assert custody_at <= acknowledged_at, f"{subject} acknowledged control before taking provider custody"
    cleanup_at = times.get("cleanup.child_exit_observed_at")
    released_at = times.get("release.released_at")
    if cleanup_at is not None and released_at is not None:
        assert cleanup_at <= released_at, f"{subject} released ownership before it observed its child exit"
    if released_at is not None:
        late = sorted(name for name, observed_at in times.items() if observed_at > released_at)
        assert not late, f"{subject} carries facts published after its release: {late}"
    return episode


def causal_episode(
    phase: str = "accepted",
    *,
    generation: int = GENERATION,
    clock: dict = SCENARIO_CLOCK,
    **overrides,
) -> dict:
    """Build one Phase 2 owner episode on a single ordered clock.

    Every Phase 2 episode is built here.  ``make_episode`` is the pure-transition
    fixture: it stamps one literal time on every phase and ships fact literals
    from its own day, which is correct for the transition tests and impossible
    for a convergence scenario, where a control history has to be orderable
    against the phase that admitted it.  Normalising at each call site instead
    moved the defect to whichever site was written next, so the normalisation and
    the invariant that checks it live on this one path and
    ``test_owner_convergence_writers`` keeps the raw constructor out of every
    other Phase 2 module.

    A stamp the caller states is left exactly as stated: ``failure_fact`` at the
    crash instant, or a cleanup on the live clock, is a deliberate fact about
    this history, not a literal to be overwritten.  Only the facts nobody stated
    are pulled onto *clock*, and then the whole result has to prove itself
    causal - so a stated fact that contradicts the history fails here, at the
    construction that made it, rather than in whichever test reads it later.

    Identity is deliberately not this function's business: ``localize_identities``
    is the separate, already-single path for giving a recorded role an identity
    this observer can adjudicate, and scenarios differ in whether they want that.
    """
    episode = make_episode(phase, generation=generation, **overrides)
    stated = frozenset(overrides)
    episode["phase_times"] = {name: clock[PHASE_INSTANTS[name]] for name in episode.get("phase_times") or {}}
    if episode.get("acknowledgement") is not None and "accepted" in episode["phase_times"]:
        # Production's accepted -> accepted control transition captures the
        # acknowledgement first, then replaces the accepted phase timestamp.
        episode["phase_times"]["accepted"] = clock["accepted_control"]
    for fact, field_name, instant in FACT_INSTANTS:
        if fact in stated or not isinstance(episode.get(fact), dict):
            continue
        episode[fact][field_name] = clock[instant]
    return assert_causal_episode(episode, clock=clock, subject=f"a {phase} episode")


@dataclass
class Scenario:
    """One durable pre-convergence state plus the facts the oracle needs."""

    spool_id: str
    contract: OriginContract
    generation: int
    revision: int
    request_id: object = NULL
    canonical_exit: object = None
    proposal_result: object = None
    stdout: object = None
    handle: object = None
    worktree: Path | None = None
    extras: dict = field(default_factory=dict)

    @property
    def requests(self) -> dict:
        """Every current-generation request in this mailbox, by id, with its kind."""
        requests = {}
        if self.request_id is not NULL:
            requests[self.request_id] = self.extras["winning_request_kind"]
        extra = self.extras.get("extra_request_id")
        if extra is not None:
            requests[extra] = self.extras["extra_request_kind"]
        return requests

    @property
    def request_ids(self) -> list:
        return sorted(self.requests)

    def expected_outcome(self) -> dict:
        return self.contract.expected_outcome(
            exit_code=self.canonical_exit,
            proposal_result=self.proposal_result,
        )

    def expected_preservation(self) -> dict:
        """tags/session/cost/background-task rules for this origin."""
        natural = self.contract.preserves_proposal
        return {
            "tags": ["keep-me"],
            "session_id": NATURAL_SESSION if natural else STALE_SESSION,
            "cost": NATURAL_COST if natural else NULL,
            "pending_background_tasks": NULL,
        }

    def expected_provenance(self) -> dict:
        return self.contract.expected_provenance(
            generation=self.generation,
            revision=self.revision,
            request_id=self.request_id,
        )

    @property
    def winning_request_id(self):
        return None if self.request_id is NULL else self.request_id

    def expected_receipt_keys(self) -> dict:
        return receipt_keys(self.generation, self.request_ids)

    def expected_receipt_intent(self) -> dict:
        return receipt_intent(self.generation, self.requests, self.winning_request_id)

    def expected_shard_intent(self) -> dict:
        return shard_intent(self.generation, self.worktree)

    def expected_transcript_intent(self) -> dict:
        import spindle

        return transcript_intent(
            self.generation,
            self.stdout,
            NATURAL_SESSION,
            source=spindle._get_output_path(self.spool_id),
        )


class WatchdogHandle:
    """The launcher-held *watchdog* Popen handle - never provider evidence."""

    def __init__(self, status=None):
        self._status = status
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._status

    def wait(self, timeout=None):  # pragma: no cover - reaping path only
        return self._status


def _causal_lifecycle(episode: dict) -> dict | None:
    """The lifecycle mirror a producer really left, for this episode's history.

    A record whose owner never bound the ownership inode was never ``held``, and
    one whose provider never started was never attached to a transport.  Writing
    those anyway invents a state no producer can reach and turns a projection
    test red for a reason that is not the missing projection.
    """
    if "lock" not in episode:
        return None
    if "provider" in episode:
        return {"ownership_state": "held", "transport_state": "connected"}
    return {"ownership_state": "held"}


def _holds_watchdog_handle(episode: dict) -> bool:
    """Whether the launching process can still own this episode's Popen handle.

    ``_PROC_HANDLES`` is filled by the launcher when it spawns the watchdog, so a
    handle exists only where a watchdog was published, and only where the
    launching process is still the one converging.  A stale reservation is stale
    precisely because that process is gone.
    """
    return "watchdog" in episode and episode.get("phase") != "reserved"


def build_scenario(
    store,
    contract: OriginContract,
    *,
    spool_id: str | None = None,
    tmp_path: Path | None = None,
    with_shard: bool = False,
    with_handle: bool | None = None,
    extra_request: str | None = None,
    before_release: bool = False,
) -> Scenario:
    """Publish exactly the durable artifacts *contract*'s producers leave behind.

    No public terminal is written: producers publish evidence and convergence
    publishes the outcome.  ``with_shard`` adds a failed-shard obligation,
    ``extra_request`` adds an unsettled current-generation request, and
    ``before_release`` stops one transition short so a converger still has to
    prove the release itself.
    """
    import spindle

    spool_id = spool_id or f"cvg-{contract.evidence}"
    evidence = contract.evidence
    generation = GENERATION
    request_id = NULL
    canonical_exit = None
    episode = None
    sidecar = None
    stdout = None
    exit_file = None
    proposal_result = None
    extras: dict = {}

    if evidence in {"natural", "natural_then_owner_crash"}:
        canonical_exit = 0
        episode = causal_episode(
            "released", generation=generation, cleanup=cleanup_fact("natural_exit", provider_exit_code=0)
        )
        stdout, exit_file, proposal_result = NATURAL_STDOUT, "0", NATURAL_RESULT
        if evidence == "natural_then_owner_crash":
            sidecar = {
                "owner_generation": generation,
                "owner_crashed": True,
                "cleanup_outcome": "watchdog_contained",
                "observed_at": SIDECAR_AT,
            }
    elif evidence == "natural_failure":
        canonical_exit = 1
        episode = causal_episode(
            "released", generation=generation, cleanup=cleanup_fact("natural_exit", provider_exit_code=1)
        )
        stdout, exit_file, proposal_result = NATURAL_FAILURE_STDOUT, "1", "the provider failed"
    elif evidence == "natural_signal":
        # SIGKILL: Popen reports -9, every durable carrier normalises to 128+9.
        canonical_exit = 137
        episode = causal_episode(
            "released", generation=generation, cleanup=cleanup_fact("natural_exit", provider_exit_code=137)
        )
        stdout, exit_file, proposal_result = NATURAL_FAILURE_STDOUT, "137", "the provider failed"
        extras["handle_status"] = -9 & 0xFF
    elif evidence in {"accepted_cancel", "accepted_drop", "accepted_timeout"}:
        kind = evidence.removeprefix("accepted_")
        request_id = f"req-{kind}"
        canonical_exit = 0
        episode = causal_episode(
            "released",
            generation=generation,
            cleanup=cleanup_fact("stopped", provider_exit_code=0),
            winning_request=request_fact(kind, request_id),
            acknowledgement=acknowledgement_fact(),
        )
        exit_file = "0"
        extras["winning_request_kind"] = kind
    elif evidence == "deadline_before_binding":
        episode = causal_episode(
            "aborted",
            generation=generation,
            failure=failure_fact("deadline_expired_before_provider_start", "inherited deadline expired before binding"),
        )
    elif evidence == "deadline_after_binding":
        episode = causal_episode(
            "released",
            generation=generation,
            path="before_acceptance",
            cleanup=cleanup_fact("deadline_expired_before_provider_start"),
            failure=failure_fact(
                "deadline_expired_before_provider_start",
                "inherited deadline expired after binding and before provider start",
            ),
        )
    elif evidence == "launcher_pre_spawn_failure":
        episode = causal_episode(
            "aborted",
            generation=generation,
            path="launcher_before_watchdog",
            failure=failure_fact("launcher_pre_spawn_failure", "spawn failed: no such executable"),
        )
    elif evidence == "owner_preacceptance_failure":
        episode = causal_episode(
            "aborted",
            generation=generation,
            failure=failure_fact("owner_preacceptance_failure", "logical owner exited before binding"),
        )
    elif evidence == "provider_spawn_failure":
        episode = causal_episode(
            "released",
            generation=generation,
            path="before_acceptance",
            cleanup=cleanup_fact("provider_spawn_failed"),
            failure=failure_fact("provider_spawn_failure", "provider spawn failed: exec format error"),
        )
    elif evidence == "stale_dead_reservation":
        episode = causal_episode("reserved", generation=generation)
    elif evidence in {"owner_crash_no_sidecar", "owner_crash_with_sidecar"}:
        episode = causal_episode(
            "released",
            generation=generation,
            path="before_acceptance",
            cleanup=cleanup_fact("watchdog_contained"),
            containment=containment_fact(),
            failure=failure_fact("owner_crash", "logical owner crashed"),
        )
        if evidence == "owner_crash_with_sidecar":
            sidecar = {
                "owner_generation": generation,
                "owner_crashed": True,
                "cleanup_outcome": "watchdog_contained",
                "observed_at": SIDECAR_AT,
            }
    elif evidence == "owner_crash_after_cleanup":
        request_id = "req-timeout"
        canonical_exit = 0
        episode = causal_episode(
            "released",
            generation=generation,
            cleanup=cleanup_fact("stopped", provider_exit_code=0),
            winning_request=request_fact("timeout", request_id),
            acknowledgement=acknowledgement_fact(),
            failure=failure_fact(
                "owner_crashed_after_cleanup",
                "owner crashed after acknowledged cleanup",
                at="crashed_after_cleanup",
            ),
        )
        sidecar = {
            "owner_generation": generation,
            "owner_crashed_after_cleanup": True,
            "cleanup_outcome": "stopped",
            "provider_exit_code": 0,
            "observed_at": SIDECAR_AT,
        }
        extras["winning_request_kind"] = "timeout"
    elif evidence == "watchdog_loss_before_binding":
        episode = causal_episode(
            "aborted",
            generation=generation,
            failure=failure_fact("watchdog_parent_loss", "logical owner detected containment watchdog exit"),
        )
    elif evidence == "watchdog_loss_after_binding":
        episode = causal_episode(
            "released",
            generation=generation,
            path="before_acceptance",
            cleanup=cleanup_fact("watchdog_parent_lost_contained"),
            containment=containment_fact(),
            failure=failure_fact("watchdog_parent_loss", "logical owner detected containment watchdog exit"),
        )
    else:  # pragma: no cover - the matrix fixes the evidence names
        raise KeyError(evidence)

    deciding_revision = episode["revision"]
    if before_release:
        if episode.get("phase") != "released":
            raise ValueError(f"{evidence} has no release to withhold")
        # The owner proved cleanup and died; nobody has published the release
        # yet, so a converger must still acquire the exact inode and prove it.
        episode.pop("release", None)
        episode["phase"] = "cleanup_proven"
        episode["revision"] = deciding_revision - 1
        episode["phase_times"] = {
            name: stamp for name, stamp in (episode.get("phase_times") or {}).items() if name != "released"
        }
        assert_causal_episode(episode, subject=f"{evidence} with its release withheld")

    localize_identities(episode)
    if "lock" in episode:
        store.bind_lock(spool_id, episode)

    record_fields = {
        "harness": "claude-code",
        "timeout": TIMEOUT_SECONDS,
        "created_at": "2026-08-01T00:00:00",
        "owner_generation": generation,
        "prompt": "work",
        "tags": ["keep-me"],
        "cost": STALE_COST,
        "session_id": STALE_SESSION,
        "pending_background_tasks": STALE_BACKGROUND,
    }
    lifecycle = _causal_lifecycle(episode)
    if lifecycle is not None:
        record_fields["lifecycle"] = lifecycle
    worktree = None
    if with_shard:
        assert tmp_path is not None, "a failed-shard scenario needs tmp_path"
        worktree = tmp_path / f"{spool_id}-worktree"
        worktree.mkdir(parents=True, exist_ok=True)
        record_fields.update(
            shard={"worktree_path": str(worktree), "branch_name": f"shard-{spool_id}"},
            shard_created_by_spool=True,
            working_dir=str(worktree),
        )

    # The control history comes first, in the order a producer really leaves it:
    # an observer publishes the request while the owner is still accepting
    # control, the owner acknowledges it, and only then are the winner, cleanup
    # and release facts published.  Publishing the request after the released
    # episode - as this fixture used to - stamps it later than the
    # acknowledgement that answered it, which is a history no producer can leave
    # and which a converger may legitimately refuse as corrupt.
    if evidence in ACCEPTED_EVIDENCE:
        publish_request(
            store,
            spool_id,
            extras["winning_request_kind"],
            generation,
            request_id=request_id,
            requested_at=REQUESTED_AT,
        )
        write_receipt(
            store,
            spool_id,
            {
                "request_id": request_id,
                "owner_generation": generation,
                "owner_acknowledged_at": ACKNOWLEDGED_AT,
                "provider_cancel_attempted_at": ACKNOWLEDGED_AT,
                "child_exit_observed_at": CHILD_EXIT_AT,
                "cleanup_outcome": "cleaned",
            },
        )
    if extra_request is not None:
        # A sibling nobody accepted, admitted in the same accepting window.
        extras["extra_request_kind"] = extra_request
        extras["extra_request_id"] = publish_request(
            store, spool_id, extra_request, generation, requested_at=REQUESTED_AT
        )

    store.write(spool_id, status=contract.initial_status, episode=episode, **record_fields)

    if sidecar is not None:
        (store.root / f"{spool_id}.owner-exit").write_text(json.dumps(sidecar))
    if stdout is not None:
        spindle._get_output_path(spool_id).write_text(stdout)
    if exit_file is not None:
        spindle._get_exit_path(spool_id).write_text(f"{exit_file}\n")

    handle = None
    if with_handle is None:
        with_handle = _holds_watchdog_handle(episode)
    if with_handle:
        handle = WatchdogHandle(extras.get("handle_status"))
        spindle._PROC_HANDLES[spool_id] = handle

    return Scenario(
        spool_id=spool_id,
        contract=contract,
        generation=generation,
        revision=deciding_revision,
        request_id=request_id,
        canonical_exit=canonical_exit,
        proposal_result=proposal_result,
        stdout=stdout,
        handle=handle,
        worktree=worktree,
        extras=extras,
    )


# --- 8. mailbox helpers ------------------------------------------------------


def mailbox_dir(store, spool_id: str) -> Path:
    return store.root / f"{spool_id}.control-mailbox"


@contextmanager
def _publishing_at(stamp):
    """Publish through the real durable writer with a producer's own clock.

    ``create_control_request`` stamps ``requested_at`` from the wall clock, which
    is always *now* - later than every frozen fact in a scenario's history.  The
    artifact is still written by production; only the moment it claims to have
    been written at is the one the scenario says it was.
    """
    import spindle.namespace_owner as owner_module

    if stamp is None:
        yield
        return
    real = owner_module._utc_now
    owner_module._utc_now = lambda: stamp
    try:
        yield
    finally:
        owner_module._utc_now = real


def publish_request(
    store,
    spool_id: str,
    kind: str,
    generation: int,
    *,
    request_id: str | None = None,
    requested_at: str | None = None,
) -> str:
    """Publish a durable control request without going through admission.

    ``requested_at`` is the clock the publishing observer had.  A request an
    owner acknowledged at 00:00:04 was published before that, so a scenario that
    freezes the acknowledgement has to freeze the publication too.
    """
    from spindle.namespace_owner import create_control_request

    with _publishing_at(requested_at):
        request = create_control_request(
            store.root,
            spool_id,
            kind,
            generation,
            "contract-test",
            request_id=None if request_id is NULL else request_id,
        )
    return request.request_id


def write_receipt(store, spool_id: str, payload: dict) -> None:
    path = mailbox_dir(store, spool_id) / f"{payload['request_id']}.receipt"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "request_id": payload["request_id"],
        "owner_generation": payload["owner_generation"],
        "owner_acknowledged_at": payload.get("owner_acknowledged_at"),
        "provider_cancel_attempted_at": payload.get("provider_cancel_attempted_at"),
        "provider_acknowledged_at": payload.get("provider_acknowledged_at"),
        "terminal_observed_at": payload.get("terminal_observed_at"),
        "forced_cleanup_started_at": payload.get("forced_cleanup_started_at"),
        "forced_cleanup_completed_at": payload.get("forced_cleanup_completed_at"),
        "child_exit_observed_at": payload.get("child_exit_observed_at"),
        "cleanup_outcome": payload.get("cleanup_outcome"),
    }
    path.write_text(json.dumps(body))


def receipt_outcomes(store, spool_id: str) -> dict:
    """Every current request's settled disposition, by request id."""
    from spindle.namespace_owner import iter_control_requests, read_control_receipt

    outcomes = {}
    for request in iter_control_requests(store.root, spool_id):
        receipt = read_control_receipt(store.root, spool_id, request.request_id)
        outcomes[request.request_id] = None if receipt is None else receipt.cleanup_outcome
    return outcomes


def receipt_outcomes_by_kind(store, spool_id: str) -> dict:
    """The same dispositions keyed by request kind, for cross-scenario comparison.

    Request IDs are per-publication UUIDs, so two runs over the *same* evidence
    never share them; the kind is what makes "these two runs settled the same
    control provenance" a comparable statement.
    """
    from spindle.namespace_owner import iter_control_requests, read_control_receipt

    outcomes = {}
    for request in iter_control_requests(store.root, spool_id):
        receipt = read_control_receipt(store.root, spool_id, request.request_id)
        outcomes[request.kind] = None if receipt is None else receipt.cleanup_outcome
    return outcomes


# --- 9. the convergence driver seam -----------------------------------------


def converge(spool_id: str) -> None:
    """Run one authorised convergence pass over *spool_id*.

    Phase 1 section 6 makes the store's reconciliation step a thin convergence
    caller, so this is the production entry every consumer already reaches
    convergence through.  Tests that assert the module's own result vocabulary
    use :func:`convergence_api` instead.
    """
    import spindle

    spindle._reconcile_spool_step(spool_id)


def converge_without_escape(spool_id: str) -> None:
    """Converge and require that no effect exception reaches the caller.

    A durable effect that cannot write its carrier leaves a pending obligation;
    it never becomes an exception the public API raises.  Swallowing that
    exception in the test instead would let the defect pass as a crash cut.
    """
    try:
        converge(spool_id)
    except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
        pytest.fail(f"an obligation failure escaped the public convergence API: {exc!r}")


def converge_to_fixed_point(spool_id: str, *, limit: int = 8) -> int:
    """Converge until the record stops changing; return the number of passes."""
    import spindle

    path = spindle._get_spool_path(spool_id)
    for passes in range(1, limit + 1):
        before = path.read_bytes() if path.exists() else None
        converge(spool_id)
        after = path.read_bytes() if path.exists() else None
        if before == after:
            return passes
    raise AssertionError(f"{spool_id} did not reach a fixed point in {limit} convergence passes")


# --- 10. restarting in a genuinely new process -------------------------------

#: Converge in a fresh interpreter that shares nothing with the crashed one but
#: the durable artifacts.  Phase 1 section 5 requires exactly this probe:
#: "interrupt immediately after public terminal write but before effects,
#: restart in a new process, call convergence".  Re-entering the same
#: interpreter cannot prove it, because module globals, caches and the local
#: handle map all survive there.
_RESTART_PROGRAM = """
import os
import pathlib
import sys

import spindle

spindle.SPINDLE_DIR = pathlib.Path(os.environ["SPINDLE_CONTRACT_STORE"])
spool_id = sys.argv[1]
path = spindle._get_spool_path(spool_id)
for _ in range(8):
    before = path.read_bytes() if path.exists() else None
    spindle._reconcile_spool_step(spool_id)
    after = path.read_bytes() if path.exists() else None
    if before == after:
        break
"""


def restart_and_converge(store, spool_id: str) -> subprocess.CompletedProcess:
    """Run convergence to its fixed point in a brand-new interpreter."""
    environment = dict(os.environ)
    environment.update(
        SPINDLE_HOME=tempfile.mkdtemp(prefix="spindle-restart-home-"),
        SPINDLE_CONTRACT_STORE=str(store.root),
        PYTHONPATH=str(REPO_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    completed = subprocess.run(
        [sys.executable, "-c", _RESTART_PROGRAM, spool_id],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"a restarted convergence process failed for {spool_id} "
            f"(exit {completed.returncode}):\n{completed.stderr.strip()}"
        )
    return completed


# --- 11. crash cuts -----------------------------------------------------------


class InjectedCrash(RuntimeError):
    """Raised at a named crash cut from Phase 1 section 4."""


#: Phase 1 section 4 named cuts.  ``CUT_ADMISSION`` is exercised by the mailbox
#: serialization tests rather than by generic interruption.
CUTS = (
    "CUT-CAPTURE",
    "CUT-REDUCE",
    "CUT-CAS",
    "CUT-RELEASE-PROOF",
    "CUT-ATOMIC-RECORD",
    "CUT-AFTER-INTENT",
    "CUT-RECEIPT-WRITE",
    "CUT-RECEIPT-MARK",
    "CUT-SHARD-MARK",
    "CUT-TRANSCRIPT",
    "CUT-LOCAL-HANDLE",
    "CUT-AFTER-EFFECTS",
)


@contextmanager
def crashed_process():
    """Absorb the injected crash, and only that.

    A cut models the process disappearing.  It does not model an effect raising
    ``OSError``: that is a durable-obligation failure the public API has to
    absorb itself, and absorbing it here would let the defect pass silently.
    """
    try:
        yield
    except InjectedCrash:
        pass
