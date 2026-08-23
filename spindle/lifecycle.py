"""Provider-neutral spool lifecycle: event envelope, pure reducer, durable apply.

This is implementation-order item 3 of
``docs/HARNESS_LIFECYCLE_REMEDIATION_PLAN.md``, right-sized: the separate
append-only journal tier was dropped (see SKEIN finding-20260823-adjf). The store
already has a crash-atomic snapshot writer, so applying one event and durably
rewriting the snapshot per event is crash-consistent without a second on-disk log.

Boundaries this module keeps deliberately narrow:

- The reducer is pure: ``reduce(state, event) -> state``. No I/O, no clocks, no
  randomness. It deep-copies its input and never mutates it, so replaying the
  same event is safe.
- Reduced state lives at ``spool["lifecycle"]["provider"]``. It never touches the
  owner's flat ``lifecycle`` keys (``ownership_state``, ``public_stop_state``,
  ``desired_terminal_kind``, ``normalized_terminal_kind``, flat
  ``transport_state``, ...), which are written by the slice-2 owner process.
- The reducer records provider PROTOCOL terminal evidence only
  (``protocol_terminal_kind``). The public/job terminal (``status``,
  ``normalized_terminal_kind``) stays with the reconciler (DECISIONS #30). A
  driver translates wire events into this envelope; it does not decide job state.
- Privacy: events carry identifiers, statuses, timestamps, bounded summaries, and
  structured errors only -- never prompts, model prose, tool inputs, or raw tool
  output. The envelope constructor enforces an allowlist and byte caps.

Durability point: an event is durably applied when ``apply_event`` returns.
Recovery is simply reading the last durable snapshot; there is no journal replay,
so the delivery contract is at-least-once with convergence of the AUTHORITATIVE
facts only: the protocol terminal (``protocol_terminal_kind`` is set-once by the
first terminal in applied-sequence order), the cancellation evidence
(``acknowledged_at``/``terminal_observed_at`` are first-write-wins), and the
monotonic ``connection_epoch`` fence. The remaining fields are LIVE projections
of the latest delivered event and are intentionally NOT monotonic: ``protocol_state``
moves between ``active`` and ``waiting`` as approvals come and go, ``connection_state``
tracks the last transport event, and ``active_work`` tracks the last work summary.
Re-delivering an event out of order can therefore re-move a live field, but never
changes an authoritative fact. ``activity_count`` and the evidence ring are
best-effort. Downstream (the reconciler) must derive the public/job terminal from
the authoritative facts, not from a live field.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Generator, Mapping, Optional

from spindle.namespace_owner import _atomic_json_write

# --- event kinds ------------------------------------------------------------

TRANSPORT_STARTED = "transport.started"
PROTOCOL_CONNECTED = "protocol.connected"
CONVERSATION_ACCEPTED = "conversation.accepted"
TURN_STARTED = "turn.started"
ACTIVITY = "activity"
WORK_STARTED = "work.started"
WORK_UPDATED = "work.updated"
WORK_FINISHED = "work.finished"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_RESOLVED = "approval.resolved"
TURN_TERMINAL = "turn.terminal"
CANCEL_ACKNOWLEDGED = "cancel.acknowledged"
TRANSPORT_LOST = "transport.lost"
TRANSPORT_EXITED = "transport.exited"

KNOWN_KINDS = frozenset(
    {
        TRANSPORT_STARTED,
        PROTOCOL_CONNECTED,
        CONVERSATION_ACCEPTED,
        TURN_STARTED,
        ACTIVITY,
        WORK_STARTED,
        WORK_UPDATED,
        WORK_FINISHED,
        APPROVAL_REQUESTED,
        APPROVAL_RESOLVED,
        TURN_TERMINAL,
        CANCEL_ACKNOWLEDGED,
        TRANSPORT_LOST,
        TRANSPORT_EXITED,
    }
)

# Only raw activity is coalescible. Work transitions (started/updated/finished),
# approvals, terminals, cancellation, and transport changes are authoritative and
# never coalesced.
COALESCIBLE_KINDS = frozenset({ACTIVITY})

# --- normalized protocol terminal kinds -------------------------------------

TERMINAL_COMPLETED = "completed"
TERMINAL_FAILED = "failed"
TERMINAL_REFUSED = "refused"
TERMINAL_INTERRUPTED = "interrupted"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_INDETERMINATE = "indeterminate"
TERMINAL_ORPHANED = "orphaned"

KNOWN_TERMINAL_KINDS = frozenset(
    {
        TERMINAL_COMPLETED,
        TERMINAL_FAILED,
        TERMINAL_REFUSED,
        TERMINAL_INTERRUPTED,
        TERMINAL_CANCELLED,
        TERMINAL_INDETERMINATE,
        TERMINAL_ORPHANED,
    }
)

# --- protocol / connection state vocabularies -------------------------------
#
# The provider connection field is deliberately named ``connection_state`` (not
# ``transport_state``): the flat ``transport_state`` key is owned by the slice-2
# owner process for OS-process transport ("reaped"/"lost"), and reusing that name
# in the nested provider block would both be confusing and collide with the
# terminal-field guard in tests/test_owner_convergence_writers.py.

PROTOCOL_STARTING = "starting"
PROTOCOL_ACCEPTED = "accepted"
PROTOCOL_ACTIVE = "active"
PROTOCOL_WAITING = "waiting"
PROTOCOL_TERMINAL = "terminal"

CONNECTION_STARTING = "starting"
CONNECTION_CONNECTED = "connected"
CONNECTION_LOST = "lost"
CONNECTION_EXITED = "exited"

# --- byte / count caps (privacy + boundedness) ------------------------------

SUMMARY_MAX = 500
PROVIDER_EVENT_MAX = 200
TERMINAL_KIND_MAX = 64
RAW_STATUS_MAX = 100
STOP_REASON_MAX = 200
OBSERVED_AT_MAX = 64
PROVIDER_ID_VALUE_MAX = 200
ERROR_VALUE_MAX = 500

PROVIDER_ID_KEYS = frozenset({"session_id", "thread_id", "turn_id", "prompt_id", "message_id", "conversation_id"})
ERROR_KEYS = ("type", "code", "message")

EVIDENCE_MAX_COUNT = 32
EVIDENCE_MAX_BYTES = 8192


class LifecycleError(Exception):
    """Base class for lifecycle store errors."""


class SpoolNotFound(LifecycleError):
    """apply_event was asked to act on a spool record that does not exist."""


class LifecycleCorruption(LifecycleError):
    """The on-disk spool record exists but is not a parseable JSON object."""


# --- sanitizing helpers -----------------------------------------------------

# Spool ids are internally generated (hex, optionally a harness prefix, hyphens),
# so a lifecycle event must name a single safe path component. This rejects "/",
# "\\", "..", and leading dots, so a malformed id can never traverse out of the
# store when building "<id>.json"/"<id>.lock".
_SAFE_SPOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_spool_id(spool_id: str) -> None:
    if not isinstance(spool_id, str) or not spool_id:
        raise ValueError("spool_id must be a non-empty string")
    if "/" in spool_id or "\\" in spool_id or ".." in spool_id or not _SAFE_SPOOL_ID.match(spool_id):
        raise ValueError(f"unsafe spool_id: {spool_id!r}")


def _cap_str(value: Optional[str], limit: int) -> Optional[str]:
    """Return *value* truncated to at most *limit* UTF-8 bytes (not characters)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str or None, got {type(value).__name__}")
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    # Drop any partial trailing multibyte sequence left by the byte cut.
    return encoded[:limit].decode("utf-8", errors="ignore")


def _clean_ids(value: Optional[Mapping]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("provider_ids must be a mapping or None")
    out: dict = {}
    for key, val in value.items():
        if key in PROVIDER_ID_KEYS and isinstance(val, str):
            out[key] = _cap_str(val, PROVIDER_ID_VALUE_MAX)
    return out


def _clean_error(value: Optional[Mapping]) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("error must be a mapping or None")
    out: dict = {}
    for key in ERROR_KEYS:
        if key not in value or value[key] is None:
            continue
        raw = value[key]
        # Keep only scalars. A nested container is dropped rather than stringified,
        # so a payload like {"message": {"tool_input": "SECRET"}} cannot leak
        # through str() past the structured-error allowlist.
        if isinstance(raw, str):
            out[key] = _cap_str(raw, ERROR_VALUE_MAX)
        elif isinstance(raw, bool) or isinstance(raw, (int, float)):
            out[key] = _cap_str(str(raw), ERROR_VALUE_MAX)
    return out or None


# --- event envelope ---------------------------------------------------------


@dataclass(frozen=True)
class LifecycleEvent:
    """A provider-neutral lifecycle event.

    Constructing an unknown keyword argument raises ``TypeError`` (unknown
    envelope fields are rejected). Nested mappings are cleaned, byte-capped, and
    then wrapped read-only (``MappingProxyType``) during construction, so a frozen
    instance is genuinely immutable and bounded — a caller cannot add a disallowed
    key to ``provider_ids`` or ``error`` after the fact.
    """

    spool_id: str
    kind: str
    epoch: int = 0
    provider_event: Optional[str] = None
    provider_ids: Mapping = field(default_factory=dict)
    terminal_kind: Optional[str] = None
    raw_status: Optional[str] = None
    stop_reason: Optional[str] = None
    error: Optional[Mapping] = None
    summary: Optional[str] = None
    observed_at: Optional[str] = None

    def __post_init__(self) -> None:
        _validate_spool_id(self.spool_id)
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be a non-empty string")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        object.__setattr__(self, "provider_event", _cap_str(self.provider_event, PROVIDER_EVENT_MAX))
        object.__setattr__(self, "terminal_kind", _cap_str(self.terminal_kind, TERMINAL_KIND_MAX))
        object.__setattr__(self, "raw_status", _cap_str(self.raw_status, RAW_STATUS_MAX))
        object.__setattr__(self, "stop_reason", _cap_str(self.stop_reason, STOP_REASON_MAX))
        object.__setattr__(self, "summary", _cap_str(self.summary, SUMMARY_MAX))
        object.__setattr__(self, "observed_at", _cap_str(self.observed_at, OBSERVED_AT_MAX))
        object.__setattr__(self, "provider_ids", MappingProxyType(_clean_ids(self.provider_ids)))
        cleaned_error = _clean_error(self.error)
        object.__setattr__(self, "error", MappingProxyType(cleaned_error) if cleaned_error is not None else None)


# --- reducer state ----------------------------------------------------------


def initial_state() -> dict:
    """A fresh reduced provider-lifecycle state (JSON-serializable)."""
    return {
        "sequence": 0,
        "connection_state": CONNECTION_STARTING,
        "connection_epoch": 0,
        "protocol_state": PROTOCOL_STARTING,
        "protocol_terminal_kind": None,
        "terminal_sequence": None,
        "raw_terminal_status": None,
        "stop_reason": None,
        "provider_error": None,
        "provider_ids": {},
        "last_event_type": None,
        "last_activity_at": None,
        "activity_count": 0,
        "active_work": None,
        # Provider-observed cancellation facts only. The request side
        # (requested_at, forced_kill_at) belongs to the owner/control path in the
        # flat lifecycle keys, not to this reducer. Presence is tracked by explicit
        # booleans so a timestamp-less event (observed_at is optional) still records
        # the fact and is first-write-wins independent of the timestamp value.
        "cancel_evidence": {
            "acknowledged": False,
            "acknowledged_at": None,
            "terminal_observed": False,
            "terminal_observed_at": None,
        },
        "evidence": [],
    }


def _evidence_entry(state: dict, event: "LifecycleEvent", reason: str) -> dict:
    return {
        "reason": reason,
        "sequence": state["sequence"],
        "kind": event.kind[:TERMINAL_KIND_MAX],
        "epoch": event.epoch,
        "provider_event": _cap_str(event.provider_event, PROVIDER_EVENT_MAX),
        "terminal_kind": _cap_str(event.terminal_kind, TERMINAL_KIND_MAX),
        "observed_at": event.observed_at,
        "summary": _cap_str(event.summary, 200),
    }


def _append_evidence(state: dict, event: "LifecycleEvent", reason: str) -> None:
    evidence = state["evidence"]
    evidence.append(_evidence_entry(state, event, reason))
    while len(evidence) > EVIDENCE_MAX_COUNT:
        evidence.pop(0)
    while len(evidence) > 1 and len(json.dumps(evidence)) > EVIDENCE_MAX_BYTES:
        evidence.pop(0)


def reduce(state: Optional[dict], event: "LifecycleEvent") -> dict:
    """Fold *event* into *state*, returning a new state (input untouched)."""
    state = copy.deepcopy(state) if state is not None else initial_state()
    state["sequence"] += 1

    kind = event.kind

    # Unknown kinds are evidence only. This guard runs before epoch handling so a
    # bogus unknown event cannot advance the connection_epoch fence (which would
    # stale out later legitimate events), nor touch any other authoritative or
    # live state.
    if kind not in KNOWN_KINDS:
        _append_evidence(state, event, "unknown_kind")
        return state

    # Epoch fencing: an older-epoch event is evidence only; it cannot change
    # authoritative state (classic fencing-token rule).
    if event.epoch < state["connection_epoch"]:
        _append_evidence(state, event, "stale_epoch")
        return state

    # Adopting a newer epoch means the transport reconnected. connection_state is a
    # pure live projection, so it tracks the reconnect even after terminal; the
    # protocol handshake is reset so a re-established turn can progress -- unless
    # already terminal (terminal is absorbing; a new epoch never un-terminalizes).
    if event.epoch > state["connection_epoch"]:
        state["connection_epoch"] = event.epoch
        state["connection_state"] = CONNECTION_CONNECTED
        if state["protocol_state"] != PROTOCOL_TERMINAL:
            state["protocol_state"] = PROTOCOL_STARTING
            state["active_work"] = None

    if kind == ACTIVITY:
        state["activity_count"] += 1
        state["last_activity_at"] = event.observed_at
        # active_work is a live projection; a late activity must not revive it
        # once the turn has terminated.
        if event.summary is not None and state["protocol_state"] != PROTOCOL_TERMINAL:
            state["active_work"] = event.summary
        state["last_event_type"] = kind
        return state

    # Merge provider identifiers only for recognized, non-activity events.
    if event.provider_ids:
        merged = dict(state["provider_ids"])
        merged.update(event.provider_ids)
        state["provider_ids"] = merged

    state["last_event_type"] = kind
    terminal = state["protocol_state"] == PROTOCOL_TERMINAL

    # connection_state is a pure live projection of the latest transport event and
    # updates unconditionally (even after terminal) so it never gets stuck; the
    # authoritative protocol state below is what terminal absorbs.
    if kind == TRANSPORT_STARTED:
        state["connection_state"] = CONNECTION_STARTING
    elif kind == PROTOCOL_CONNECTED:
        state["connection_state"] = CONNECTION_CONNECTED
    elif kind == TRANSPORT_LOST:
        state["connection_state"] = CONNECTION_LOST
    elif kind == TRANSPORT_EXITED:
        state["connection_state"] = CONNECTION_EXITED
    elif kind == CONVERSATION_ACCEPTED:
        if state["protocol_state"] == PROTOCOL_STARTING:
            state["protocol_state"] = PROTOCOL_ACCEPTED
    elif kind == TURN_STARTED:
        if state["protocol_state"] in (PROTOCOL_STARTING, PROTOCOL_ACCEPTED, PROTOCOL_WAITING):
            state["protocol_state"] = PROTOCOL_ACTIVE
    elif kind == WORK_STARTED:
        if not terminal:
            state["active_work"] = event.summary
    elif kind == WORK_UPDATED:
        if not terminal:
            state["active_work"] = event.summary
    elif kind == WORK_FINISHED:
        if not terminal:
            state["active_work"] = None
    elif kind == APPROVAL_REQUESTED:
        if state["protocol_state"] in (PROTOCOL_ACCEPTED, PROTOCOL_ACTIVE):
            state["protocol_state"] = PROTOCOL_WAITING
    elif kind == APPROVAL_RESOLVED:
        if state["protocol_state"] == PROTOCOL_WAITING:
            state["protocol_state"] = PROTOCOL_ACTIVE
    elif kind == CANCEL_ACKNOWLEDGED:
        cancel = state["cancel_evidence"]
        if not cancel["acknowledged"]:
            cancel["acknowledged"] = True
            cancel["acknowledged_at"] = event.observed_at
    elif kind == TURN_TERMINAL:
        if state["protocol_terminal_kind"] is None:
            # Record terminal-observed evidence only while accepting the FIRST
            # terminal, so an at-least-once replay of a terminal cannot fabricate
            # cancel-associated evidence after the fact. Keyed on the acknowledged
            # flag, not the (optional) timestamp value.
            cancel = state["cancel_evidence"]
            if cancel["acknowledged"] and not cancel["terminal_observed"]:
                cancel["terminal_observed"] = True
                cancel["terminal_observed_at"] = event.observed_at
            normalized = event.terminal_kind
            if normalized not in KNOWN_TERMINAL_KINDS:
                # Fail closed: an unknown terminal discriminator is never success.
                normalized = TERMINAL_INDETERMINATE
            state["protocol_terminal_kind"] = normalized
            state["terminal_sequence"] = state["sequence"]
            state["raw_terminal_status"] = event.raw_status
            state["stop_reason"] = event.stop_reason
            # Defensive plain-dict copy: never alias the event's (read-only)
            # error mapping into persisted state.
            state["provider_error"] = dict(event.error) if event.error is not None else None
            state["active_work"] = None
            state["protocol_state"] = PROTOCOL_TERMINAL
        else:
            # Terminal is set-once; a later provider terminal is evidence.
            _append_evidence(state, event, "terminal_after_terminal")

    return state


# --- durable store ----------------------------------------------------------


def _spool_path(store_root: Path, spool_id: str) -> Path:
    return store_root / f"{spool_id}.json"


def _lock_path(store_root: Path, spool_id: str) -> Path:
    return store_root / f"{spool_id}.lock"


@contextmanager
def _record_lock(store_root: Path, spool_id: str) -> Generator[None, None, None]:
    """Hold the per-spool record lock (``<id>.lock``).

    This is the exact pathname and advisory-lock mechanism used by
    ``spindle._spool_lock``, so ``apply_event`` serializes with every other
    record writer (launchers, observers, the owner) when *store_root* is the
    active store directory.
    """
    lock_path = _lock_path(store_root, spool_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_record_strict(spool_path: Path) -> dict:
    try:
        text = spool_path.read_text()
    except FileNotFoundError as exc:
        raise SpoolNotFound(f"spool record {spool_path} does not exist") from exc
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LifecycleCorruption(f"spool record {spool_path} is not parseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LifecycleCorruption(f"spool record {spool_path} is not a JSON object")
    return data


def apply_event(store_root, spool_id: str, event: "LifecycleEvent") -> dict:
    """Durably fold *event* into the spool's reduced provider-lifecycle block.

    Under the per-spool record lock: read the record (which must already exist and
    be parseable), reduce the event into ``lifecycle.provider``, and durably
    rewrite the record (temp -> fsync -> atomic replace -> directory fsync). The
    owner's flat ``lifecycle`` keys and every other record field are preserved.

    Returns the new ``provider`` sub-state. Raises :class:`SpoolNotFound` if the
    record is absent and :class:`LifecycleCorruption` if it exists but is not a
    JSON object -- never silently reinitializes over a corrupt record.
    """
    if not isinstance(event, LifecycleEvent):
        raise TypeError("event must be a LifecycleEvent")
    _validate_spool_id(spool_id)
    if event.spool_id != spool_id:
        raise ValueError("event.spool_id does not match the target spool_id")
    root = Path(store_root)
    spool_path = _spool_path(root, spool_id)
    # Defense in depth: a validated spool_id is already a safe single component,
    # but confirm the resolved record path stays inside the store root before any
    # lock or write, so a malformed id can never touch a sibling record.
    if spool_path.parent.resolve() != root.resolve():
        raise ValueError(f"spool_id escapes the store root: {spool_id!r}")
    with _record_lock(root, spool_id):
        record = _read_record_strict(spool_path)
        lifecycle = dict(record.get("lifecycle") or {})
        new_provider = reduce(lifecycle.get("provider"), event)
        lifecycle["provider"] = new_provider
        record["lifecycle"] = lifecycle
        _atomic_json_write(spool_path, record)
    return new_provider


# --- driver-side activity coalescing ----------------------------------------


class ActivityCoalescer:
    """Collapse many raw activity observations into occasional summary events.

    One ``apply_event`` is one durable snapshot write, so writing per raw token
    would mean one fsync per token. The driver feeds raw observations here; the
    coalescer emits an ``activity`` :class:`LifecycleEvent` at most once per
    ``threshold`` observations (plus a final ``flush``), bounding the durable
    write rate. It is deterministic (count-based, no wall-clock). Authoritative
    events bypass the coalescer entirely and are applied immediately; the driver
    should ``flush`` any pending activity before applying one so ordering holds.
    """

    def __init__(self, spool_id: str, *, threshold: int = 50, epoch: int = 0) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.spool_id = spool_id
        self.threshold = threshold
        self.epoch = epoch
        self.total_observed = 0
        self._pending = 0
        self._last_summary: Optional[str] = None

    @property
    def pending(self) -> int:
        return self._pending

    def observe(self, summary: Optional[str] = None, *, observed_at: Optional[str] = None) -> Optional[LifecycleEvent]:
        """Record one raw observation; return an event to apply, or ``None``."""
        self._pending += 1
        self.total_observed += 1
        if summary is not None:
            self._last_summary = summary
        if self._pending >= self.threshold:
            return self._emit(observed_at)
        return None

    def flush(self, *, observed_at: Optional[str] = None) -> Optional[LifecycleEvent]:
        """Emit a summary for any buffered observations, or ``None`` if empty."""
        if self._pending == 0:
            return None
        return self._emit(observed_at)

    def _emit(self, observed_at: Optional[str]) -> LifecycleEvent:
        event = LifecycleEvent(
            spool_id=self.spool_id,
            kind=ACTIVITY,
            epoch=self.epoch,
            summary=self._last_summary,
            observed_at=observed_at,
        )
        self._pending = 0
        # Reset the batch summary so a later summary-less batch does not re-emit a
        # stale summary (which would revive work already cleared by work.finished).
        self._last_summary = None
        return event
