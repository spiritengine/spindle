"""Owner-episode convergence and its finite durable obligations.

The owner, watchdog, and launcher publish evidence.  This module is the only
place that turns that evidence into a public terminal outcome, proves release,
or records convergence intent.  Outcome fields are immutable after publication;
re-entry only advances idempotent obligation progress and attempts local work.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .namespace_owner import MalformedControlReceipt, NamespaceIdentity

CONVERGENCE_FORMAT = "spindle.owner-convergence/1"
EPISODE_FORMAT = "spindle.owner-episode/1"
EPISODE_KEY = "owner_episode"
CONVERGENCE_KEY = "owner_convergence"
TERMINAL_STATUSES = {"complete", "error", "timeout"}
NONTERMINAL_STATUSES = {"pending", "running"}

#: Public outcome fields only this module may publish.  Phase 1's closed-writer
#: rule is enforced mechanically over production call sites; this is the same set
#: stated as data, so the compatibility applicator can refuse a caller that hands
#: one of them to a record whose outcome convergence already owns.
PROTECTED_RECORD_FIELDS = (
    "completed_at",
    "error",
    "error_kind",
    "exit_code",
    "result",
    "terminal_origin",
    "terminal_provenance",
)
#: Lifecycle mirrors whose terminal values are the terminal act itself.
TERMINAL_LIFECYCLE_VALUES = {
    "ownership_state": frozenset({"released"}),
    "transport_state": frozenset({"reaped", "lost"}),
}
#: Every value of this lifecycle field names a terminal kind.
ALWAYS_TERMINAL_LIFECYCLE = ("normalized_terminal_kind",)

#: Receipt dispositions that settle a request without an owner acknowledgement.
_UNSETTLED_CLEANUP_OUTCOMES = (None, "accepted")


class ProtectedRecordUpdate(RuntimeError):
    """A compatibility writer tried to publish a field convergence owns."""


@dataclass(frozen=True)
class Obligation:
    kind: str
    idempotency_key: Any
    intent: dict
    progress: str
    completion: Any = None
    error: str | None = None


@dataclass(frozen=True)
class ObserverIdentity:
    pid: int
    namespace: Any
    local_effects: Any

    @classmethod
    def for_this_process(cls) -> "ObserverIdentity":
        import spindle

        return cls(
            pid=os.getpid(),
            namespace=spindle.capture_pid_namespace(),
            local_effects={"handle_reap": spindle._pop_and_reap_process_handle},
        )


@dataclass(frozen=True)
class CapturedOwnerEpisodeEvidence:
    record: dict
    episode: dict | None
    requests: tuple
    receipts: dict
    sidecars: dict
    output: dict
    generation: int | None
    revision: int | None
    observations: dict


@dataclass(frozen=True)
class MailboxUnavailable:
    """Typed fail-closed result for a mailbox that cannot be enumerated."""

    error: str


@dataclass(frozen=True)
class TerminalProjection:
    status: str
    error: Any
    error_kind: Any
    result: Any
    exit_code: Any
    completed_at: str
    terminal_origin: str
    terminal_provenance: dict
    lifecycle: dict


@dataclass(frozen=True)
class ConvergenceResult:
    classification: str
    reason: str
    may_mutate: bool
    refusal_reason: str | None
    terminal_state: str
    projection: TerminalProjection | None
    obligations: tuple[Obligation, ...]
    local_errors: tuple[str, ...]


@dataclass(frozen=True)
class _Meaning:
    classification: str
    reason: str
    may_mutate: bool
    refusal_reason: str | None
    lock: Any
    liveness: Any


def _same_namespace(episode: dict, observer: ObserverIdentity) -> bool:
    observed = observer.namespace
    if not isinstance(observed, NamespaceIdentity):
        return False
    for role in ("owner", "watchdog", "starter"):
        namespace = (episode.get(role) or {}).get("namespace")
        if not isinstance(namespace, dict):
            continue
        try:
            recorded = NamespaceIdentity.from_dict(namespace)
        except (KeyError, TypeError, ValueError):
            return False
        return observed.same_as(recorded) is True
    return False


def _normalise_exit(code: Any) -> int | None:
    if code is None or isinstance(code, bool):
        return None
    try:
        value = int(code)
    except (TypeError, ValueError):
        return None
    return 128 + abs(value) if value < 0 else value


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _exit_evidence(spool_id: str, record: dict, episode: dict) -> tuple[int | None, str | None, str | None]:
    """Return canonical provider exit, its carrier, or a contradiction reason."""
    import spindle

    generation = episode.get("generation")
    values: list[tuple[str, int]] = []
    cleanup_code = _normalise_exit((episode.get("cleanup") or {}).get("provider_exit_code"))
    if cleanup_code is not None:
        values.append(("episode_cleanup", cleanup_code))

    sidecar = _read_json(spindle._get_owner_exit_path(spool_id))
    if sidecar and sidecar.get("owner_generation") == generation:
        sidecar_code = _normalise_exit(sidecar.get("provider_exit_code"))
        if sidecar_code is not None:
            values.append(("owner_exit", sidecar_code))

    exit_code = _normalise_exit(spindle._read_exit_code(spool_id))
    if exit_code is not None:
        values.append(("exit_file", exit_code))

    if len({value for _carrier, value in values}) > 1 and isinstance(record.get("lifecycle"), dict):
        rendered = ", ".join(f"{carrier}={value}" for carrier, value in values)
        return None, None, f"contradictory_provider_exit_evidence:{rendered}"
    if not values:
        return None, None, None
    for carrier in ("episode_cleanup", "owner_exit", "exit_file"):
        for observed_carrier, value in values:
            if observed_carrier == carrier:
                return value, carrier, None
    raise AssertionError("unreachable exit carrier")


def classify_owner_episode_record(record: dict, observer: ObserverIdentity | None = None) -> _Meaning:
    """Pure durable meaning plus observer authority for one record snapshot."""
    import spindle

    observer = observer or ObserverIdentity.for_this_process()
    episode = record.get("owner_episode")
    missing = spindle.LockEvidence("absent_legacy", detail="no_recorded_current_owner")
    if episode is None:
        liveness = spindle.LivenessEvidence(
            "dead" if record.get("status") not in {"pending", "running"} else "unverifiable",
            "terminal_record" if record.get("status") not in {"pending", "running"} else "missing_owner_episode",
        )
        classification = spindle.classify_owner_episode(record, missing, liveness)
        if classification.state == "unhealthy":
            return _Meaning(
                "store_unhealthy",
                classification.reason,
                False,
                "live record has no owner episode",
                missing,
                liveness,
            )
        return _Meaning(
            "terminalizable",
            classification.reason,
            False,
            "legacy records are outside owner-episode convergence",
            missing,
            liveness,
        )

    lock, liveness = spindle._owner_episode_observation(record)
    classification = spindle.classify_owner_episode(record, lock, liveness)
    if classification.state == "unhealthy":
        return _Meaning("store_unhealthy", classification.reason, False, classification.reason, lock, liveness)

    _code, _carrier, contradiction = _exit_evidence(str(record.get("id", "")), record, episode)
    if contradiction:
        return _Meaning("store_unhealthy", contradiction, False, contradiction, lock, liveness)

    phase = episode.get("phase")
    # An aborted reservation never acquired an ownership inode.  Its durable
    # failure is sufficient authority to project and settle duties from any
    # observer; namespace authority is required for bound/released episodes.
    local = phase == "aborted" or not isinstance(record.get("lifecycle"), dict) or _same_namespace(episode, observer)
    if classification.state == "active":
        # Admission writes this mirror under the same mailbox-then-record lock
        # order as its request. Active classification therefore needs neither a
        # mailbox scan nor the guard which serializes terminal capture.
        stopping = phase == "accepted" and (record.get("lifecycle") or {}).get("public_stop_state") == "stopping"
        state = "stopping" if stopping else "active"
        reason = "current_generation_control_requested" if stopping else classification.reason
        return _Meaning(state, reason, False, "owner episode is still active", lock, liveness)

    if classification.state == "retireable":
        # cleanup_proven still needs an exact-inode release act.  A foreign PID
        # namespace may understand its meaning but cannot perform that act.
        if phase == "cleanup_proven" and not local:
            return _Meaning(
                "unverifiable",
                "foreign_observer_cannot_prove_release",
                False,
                "observer PID namespace does not match the owner episode",
                lock,
                liveness,
            )
        return _Meaning(
            "terminalizable",
            classification.reason,
            local,
            None if local else "observer PID namespace does not match the owner episode",
            lock,
            liveness,
        )

    return _Meaning("store_unhealthy", classification.reason, False, classification.reason, lock, liveness)


def _current_generation_requests(spool_id: str, generation: int | None) -> tuple | MailboxUnavailable:
    """Read one generation's requests or preserve an unavailable mailbox."""
    import spindle

    try:
        return tuple(
            request
            for request in spindle.iter_control_requests(spindle.SPINDLE_DIR, spool_id)
            if request.owner_generation == generation
        )
    except OSError as exc:
        return MailboxUnavailable(f"{type(exc).__name__}: {exc}")


def _capture(
    spool_id: str,
    observer: ObserverIdentity,
) -> CapturedOwnerEpisodeEvidence | MailboxUnavailable | None:
    import spindle

    record = spindle._read_spool(spool_id)
    if record is None:
        return None
    episode = record.get("owner_episode")
    generation = episode.get("generation") if isinstance(episode, dict) else None
    requests = _current_generation_requests(spool_id, generation)
    if isinstance(requests, MailboxUnavailable):
        return requests
    receipts = {}
    receipt_errors = {}
    for request in requests:
        try:
            receipts[request.request_id] = spindle.read_control_receipt(
                spindle.SPINDLE_DIR,
                spool_id,
                request.request_id,
            )
        except MalformedControlReceipt as exc:
            receipts[request.request_id] = None
            receipt_errors[request.request_id] = str(exc)
        except OSError as exc:
            return MailboxUnavailable(f"{type(exc).__name__}: {exc}")
    owner_exit = _read_json(spindle._get_owner_exit_path(spool_id))
    stdout_path = spindle._get_output_path(spool_id)
    stderr_path = spindle._get_stderr_path(spool_id)
    try:
        stdout = stdout_path.read_text()
    except (FileNotFoundError, OSError):
        stdout = ""
    try:
        stderr = stderr_path.read_text()
    except (FileNotFoundError, OSError):
        stderr = ""
    meaning = classify_owner_episode_record(record, observer)
    return CapturedOwnerEpisodeEvidence(
        record=record,
        episode=episode if isinstance(episode, dict) else None,
        requests=requests,
        receipts=receipts,
        sidecars={"owner_exit": owner_exit},
        output={"stdout": stdout, "stderr": stderr, "stdout_path": stdout_path},
        generation=generation,
        revision=episode.get("revision") if isinstance(episode, dict) else None,
        observations={
            "meaning": meaning,
            "lock": meaning.lock,
            "liveness": meaning.liveness,
            "receipt_errors": receipt_errors,
        },
    )


def _natural_proposal(record: dict, stdout: str, stderr: str, exit_code: int | None) -> dict:
    """Parse the existing harness formats into a proposed natural outcome."""
    import spindle

    harness = (record.get("harness") or "claude-code").lower()
    proposal: dict[str, Any] = {
        "status": "error",
        "error": None,
        "error_kind": None,
        "result": None,
        "session_id": record.get("session_id"),
        "cost": None,
        "pending_background_tasks": None,
        "gate_category": None,
        "tags": list(record.get("tags") or []),
    }
    no_output = "Process exited with no output" + (f" (exit code {exit_code})" if exit_code is not None else "")
    if harness == "codex":
        extracted = spindle._extract_codex_result(stdout) if stdout.strip() else None
        proposal["result"] = extracted if extracted is not None else (stdout or None)
        state, _failure = spindle._codex_turn_state(stdout)
        failure = spindle._codex_failure_message(stdout)
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                proposal["session_id"] = event.get("thread_id")
            elif event.get("type") == "turn.completed" and event.get("usage"):
                proposal["cost"] = event["usage"]
        if failure:
            proposal["error"] = failure
        elif exit_code is None:
            proposal["error"] = "Codex exit status unavailable"
        elif exit_code != 0:
            proposal["error"] = stderr.strip()[:500] or f"Codex exited with code {exit_code}"
        elif state != "complete":
            proposal["error"] = "Codex exited without a completed turn"
        else:
            proposal["status"] = "complete"
    elif harness == "gemini":
        if stdout.strip():
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                proposal["result"] = data.get("response", stdout)
                proposal["session_id"] = data.get("session_id")
            else:
                proposal["result"] = stdout
            proposal["status"] = "complete"
        elif stderr.strip():
            data = spindle._extract_last_json_object(stderr)
            error = data.get("error") if data else None
            proposal["error"] = (
                error.get("message", str(error)) if isinstance(error, dict) else str(error or stderr[:500])
            )
            if proposal["error"] == "[object Object]":
                proposal["error"] = spindle._extract_gemini_stderr_error(stderr)
            if data:
                proposal["session_id"] = data.get("session_id")
        else:
            proposal["error"] = no_output
    elif harness == "kimi":
        if stdout.strip():
            proposal["result"] = spindle._extract_kimi_result(stdout) or stdout
            proposal["status"] = "complete"
        else:
            proposal["error"] = stderr[:500] if stderr.strip() else no_output
    else:
        driver_stream = record.get("claude_protocol") == spindle.CLAUDE_PROTOCOL_STREAM_V1
        if stdout.strip():
            try:
                data = spindle._claude_driver.parse_ndjson_events(stdout) if driver_stream else json.loads(stdout)
                if driver_stream and not data:
                    raise json.JSONDecodeError("no parseable driver events", stdout, 0)
            except json.JSONDecodeError:
                data = None
            cc_result = spindle._extract_cc_result(data) if data is not None else None
            if cc_result:
                proposal["result"] = cc_result.get("result", stdout)
                proposal["session_id"] = cc_result.get("session_id")
                proposal["cost"] = cc_result.get("cost") or cc_result.get("total_cost_usd")
                proposal["status"] = "error" if cc_result.get("is_error") else "complete"
                proposal["error"] = cc_result.get("result") if cc_result.get("is_error") else None
                if cc_result.get("stop_reason") == "refusal":
                    text = cc_result.get("result") or "Model declined the request (safety refusal)"
                    proposal["status"] = "error"
                    proposal["error"] = text
                    if spindle._is_fable_gate(record.get("model"), text):
                        proposal["error_kind"] = "fable_gate"
                        proposal["gate_category"] = spindle._refusal_category(data)
                        if "fable-gate" not in proposal["tags"]:
                            proposal["tags"].append("fable-gate")
                    else:
                        proposal["error_kind"] = "safety_refusal"
            else:
                proposal["result"] = stdout
                proposal["status"] = "complete"

            sentinel = spindle._claude_driver.find_sentinel(data) if driver_stream and data else None
            parked_eligible = proposal["status"] == "complete" or (
                proposal["status"] == "error" and proposal["error_kind"] is None
            )
            if driver_stream and sentinel is None and cc_result is None:
                proposal["status"] = "error"
                proposal["error"] = (
                    "Stream driver terminated without a result or a verdict "
                    "(claude or the driver crashed mid-stream"
                    + (f", exit code {exit_code}" if exit_code is not None else "")
                    + ")"
                    + (f"; stderr: {stderr.strip()[:300]}" if stderr.strip() else "")
                )
            elif driver_stream and sentinel is not None:
                sentinel_subtype = sentinel.get("subtype")
                if sentinel_subtype == "no_result" and proposal["status"] == "complete":
                    proposal["status"] = "error"
                    detail = sentinel.get("reason") or "claude exited without emitting a result event"
                    sentinel_exit = sentinel.get("claude_exit_code")
                    proposal["error"] = (
                        f"Stream driver reported no result: {detail}"
                        + (f" (claude exit code {sentinel_exit})" if sentinel_exit is not None else "")
                        + (f"; stderr: {stderr.strip()[:300]}" if stderr.strip() else "")
                    )
                elif sentinel_subtype == "parked" and parked_eligible:
                    pending = [
                        {"id": task.get("id"), "source": task.get("source")}
                        for task in (sentinel.get("unresolved_tasks") or sentinel.get("stale_resolved_tasks") or [])
                        if isinstance(task, dict)
                    ] or [{"id": "unknown", "source": "unknown"}]
                    ids = ", ".join(str(task["id"]) for task in pending)
                    prior_error = proposal["error"]
                    proposal.update(
                        status="error",
                        error_kind="headless_background_wait",
                        pending_background_tasks=pending,
                        error=(
                            "Claude's headless turn ended without a result that accounts "
                            f"for its background task(s) [{ids}] "
                            f"({sentinel.get('reason') or 'driver reported the turn parked'}). "
                            "The stored result is not a completed answer. Use respin() to "
                            "continue — it will rebuild a clean session from the transcript."
                            + (f" Original error: {prior_error[:300]}" if prior_error else "")
                        ),
                    )

            if (
                driver_stream
                and sentinel is None
                and isinstance(data, list)
                and (
                    proposal["status"] == "complete"
                    or (proposal["status"] == "error" and proposal["error_kind"] is None)
                )
            ):
                task_state = spindle._claude_driver.background_task_state(data)
                pending_source = task_state["unresolved"] or task_state["stale_resolved"]
                if pending_source:
                    pending = [{"id": task.get("id"), "source": task.get("source")} for task in pending_source]
                    ids = ", ".join(str(task["id"]) for task in pending)
                    prior_error = proposal["error"]
                    proposal.update(
                        status="error",
                        error_kind="headless_background_wait",
                        pending_background_tasks=pending,
                        error=(
                            "Claude's headless turn ended without a result that accounts "
                            f"for its background task(s) [{ids}], and the stream driver "
                            "terminated without a verdict. The stored result is not a completed "
                            "answer. Use respin() to continue — it will rebuild a clean session "
                            "from the transcript." + (f" Original error: {prior_error[:300]}" if prior_error else "")
                        ),
                    )
        elif stderr.strip():
            proposal["error"] = stderr[:500]
        else:
            proposal["error"] = no_output
    if proposal["status"] == "error" and not proposal["error"]:
        proposal["error"] = no_output
    return proposal


def _origin(captured: CapturedOwnerEpisodeEvidence) -> tuple[str, str | None, str | None]:
    record = captured.record
    episode = captured.episode or {}
    failure_kind = (episode.get("failure") or {}).get("kind")
    cleanup = episode.get("cleanup") or {}
    outcome = cleanup.get("outcome")
    winning = episode.get("winning_request") or {}
    acknowledged = bool((episode.get("acknowledgement") or {}).get("acknowledged_at"))
    durable_cleanup = bool(
        cleanup.get("provider_reaped") is True and cleanup.get("child_exit_observed_at") and cleanup.get("outcome")
    )

    # Pre-integration fixtures and records did not carry the lifecycle envelope.
    # Preserve their established precedence while all owner-episode-v1 records
    # emitted by the integrated path use the stricter durable-evidence order.
    if not isinstance(record.get("lifecycle"), dict):
        compatibility_mapping = {
            "deadline_expired_before_provider_start": "deadline_expired_before_provider_start",
            "watchdog_parent_loss": (
                "watchdog_parent_loss_before_binding"
                if episode.get("phase") == "aborted"
                else "watchdog_parent_loss_after_binding"
            ),
        }
        if failure_kind in compatibility_mapping:
            return compatibility_mapping[failure_kind], None, failure_kind
        owner_exit = captured.sidecars.get("owner_exit") or {}
        if owner_exit.get("owner_generation") == episode.get("generation") and owner_exit.get("owner_crashed"):
            return "owner_crash_before_cleanup", None, "owner_crash"
        if winning and acknowledged:
            kind = winning.get("kind")
            return f"accepted_{kind}", kind, failure_kind

    if outcome == "natural_exit":
        return "natural", None, failure_kind
    if failure_kind in {"owner_crashed_after_cleanup", "owner_crash"} and winning and acknowledged and durable_cleanup:
        kind = winning.get("kind")
        return "owner_crash_after_acknowledged_cleanup", kind, failure_kind
    if failure_kind == "owner_crash":
        return "owner_crash_before_cleanup", None, failure_kind
    if winning and acknowledged and outcome:
        kind = winning.get("kind")
        return f"accepted_{kind}", kind, failure_kind
    mapping = {
        "deadline_expired_before_provider_start": "deadline_expired_before_provider_start",
        "launcher_pre_spawn_failure": "launcher_pre_spawn_failure",
        "owner_preacceptance_failure": "owner_preacceptance_failure",
        "provider_spawn_failure": "provider_spawn_failure_after_binding",
        "owner_crash": "owner_crash_before_cleanup",
        "watchdog_parent_loss": (
            "watchdog_parent_loss_before_binding"
            if episode.get("phase") == "aborted"
            else "watchdog_parent_loss_after_binding"
        ),
    }
    if failure_kind in mapping:
        return mapping[failure_kind], None, failure_kind
    if episode.get("phase") == "reserved":
        return "stale_dead_reservation", None, None
    return "natural", None, failure_kind


def _projection(captured: CapturedOwnerEpisodeEvidence) -> TerminalProjection:
    record = captured.record
    episode = captured.episode or {}
    exit_code, carrier, contradiction = _exit_evidence(str(record.get("id", "")), record, episode)
    if contradiction:
        raise ValueError(contradiction)
    origin, request_kind, failure_kind = _origin(captured)
    proposal = _natural_proposal(record, captured.output["stdout"], captured.output["stderr"], exit_code)
    timeout_error = f"Timeout after {record.get('timeout')}s"
    failure = episode.get("failure") or {}
    status = "error"
    error: Any = failure.get("detail")
    error_kind: Any = failure_kind
    result: Any = None
    transport = "reaped"
    normalized = "failed"

    if origin == "natural":
        status = proposal["status"]
        error = proposal["error"]
        error_kind = proposal["error_kind"] or ("provider_failed" if status == "error" else None)
        result = proposal["result"]
        normalized = "complete" if status == "complete" else "failed"
        origin = "natural_success" if status == "complete" else "natural_failure"
    elif origin in {"accepted_cancel", "accepted_drop"}:
        status, error, error_kind, normalized = "error", "Cancelled", "cancelled", "cancelled"
    elif origin == "accepted_timeout":
        status, error, error_kind, normalized = "timeout", timeout_error, "timeout", "timeout"
    elif origin == "owner_crash_after_acknowledged_cleanup":
        if request_kind == "timeout":
            status, error, error_kind, normalized = "timeout", timeout_error, "timeout", "timeout"
        else:
            status, error, error_kind, normalized = "error", "Cancelled", "cancelled", "cancelled"
        transport = "lost"
        failure_kind = "owner_crashed_after_cleanup"
    elif origin == "deadline_expired_before_provider_start":
        status, error, error_kind, normalized = "timeout", timeout_error, origin, "timeout"
        exit_code, carrier = None, None
    elif origin == "stale_dead_reservation":
        status, error, error_kind = "error", "spawn timeout - never started", origin
        exit_code, carrier = None, None
    elif origin == "owner_crash_before_cleanup":
        status = "error"
        error = "Logical owner exited before terminal publication"
        error_kind, normalized, transport = "owner_transport_loss", "indeterminate", "lost"
    elif origin == "watchdog_parent_loss_before_binding":
        status = "error"
        error = "Containment watchdog exited before terminal publication"
        error_kind, transport = "watchdog_parent_loss", "lost"
        exit_code, carrier = None, None
    elif origin == "watchdog_parent_loss_after_binding":
        status = "error"
        error = "Containment watchdog exited before terminal publication"
        error_kind, normalized, transport = "owner_transport_loss", "indeterminate", "lost"
        exit_code, carrier = None, None
    else:
        exit_code, carrier = None, None

    lifecycle = dict(record.get("lifecycle") or {})
    lifecycle.pop("public_stop_state", None)
    lifecycle.update(
        ownership_state="released",
        transport_state=transport,
        normalized_terminal_kind=normalized,
    )
    if origin == "owner_crash_after_acknowledged_cleanup":
        lifecycle["owner_crashed_after_cleanup"] = True
    elif origin == "owner_crash_before_cleanup":
        lifecycle["owner_crashed"] = True
    elif origin in {"watchdog_parent_loss_before_binding", "watchdog_parent_loss_after_binding"}:
        lifecycle["watchdog_parent_lost"] = True
    provenance = {
        "owner_generation": episode.get("generation"),
        "episode_revision": episode.get("revision"),
        "episode_phase": episode.get("phase"),
    }
    winning = episode.get("winning_request") or {}
    if request_kind is not None:
        provenance.update(request_id=winning.get("request_id"), request_kind=request_kind)
    if carrier is not None:
        provenance["provider_exit_carrier"] = carrier
    if failure_kind is not None:
        provenance["failure_kind"] = failure_kind
    return TerminalProjection(
        status=status,
        error=error,
        error_kind=error_kind,
        result=result,
        exit_code=exit_code,
        completed_at=record.get("completed_at") or datetime.now().isoformat(),
        terminal_origin=origin,
        terminal_provenance=provenance,
        lifecycle=lifecycle,
    )


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _entry(kind: str, key: Any, intent: dict) -> dict:
    return asdict(Obligation(kind, key, intent, "pending"))


def _obligations(captured: CapturedOwnerEpisodeEvidence, projection: TerminalProjection) -> dict:
    record = captured.record
    episode = captured.episode or {}
    generation = episode.get("generation")
    requests = {request.request_id: request.kind for request in captured.requests}
    winning_id = (episode.get("winning_request") or {}).get("request_id")
    entries = {}
    if requests:
        keys = {request_id: f"{generation}/{request_id}" for request_id in sorted(requests)}
        intent = {
            "owner_generation": generation,
            "winning_request_id": winning_id,
            "disposition": {
                "winner": "cleaned",
                "acknowledged": "cleaned",
                "sibling": "rejected_superseded",
                "no_winner": "rejected_terminal",
            },
            "requests": {request_id: {"kind": requests[request_id]} for request_id in sorted(requests)},
        }
        entries["control_receipts"] = _entry("control_receipts", keys, intent)
    if projection.status in {"error", "timeout"} and record.get("shard_created_by_spool"):
        worktree = (record.get("shard") or {}).get("worktree_path") or record.get("working_dir")
        intent = {
            "owner_generation": generation,
            "worktree_path": worktree,
            "reason": "automatic cleanup disabled after agent failure",
        }
        entries["failed_shard_preservation"] = _entry(
            "failed_shard_preservation", f"{generation}/preserve_failed_shard", intent
        )
    stdout = captured.output["stdout"]
    if projection.terminal_origin in {"natural_success", "natural_failure"} and stdout:
        digest = _digest(stdout)
        session_id = _natural_proposal(record, stdout, captured.output["stderr"], projection.exit_code).get(
            "session_id"
        )
        if session_id or record.get("claude_protocol"):
            intent = {
                "owner_generation": generation,
                "session_id": session_id,
                "source": "stdout_capture",
                "source_path": str(captured.output["stdout_path"]),
                "digest": digest,
                "size": len(stdout.encode()),
            }
            entries["natural_transcript"] = _entry("natural_transcript", f"{generation}/transcript/{digest}", intent)
    return entries


def _status_only_obligations(captured: CapturedOwnerEpisodeEvidence) -> dict:
    """Recover explicit duties without inventing a mixed-version origin."""
    record = captured.record
    episode = captured.episode or {}
    generation = episode.get("generation")
    requests = {
        request.request_id: request.kind for request in captured.requests if request.owner_generation == generation
    }
    entries = {}
    if requests:
        winning_id = (episode.get("winning_request") or {}).get("request_id")
        keys = {request_id: f"{generation}/{request_id}" for request_id in sorted(requests)}
        intent = {
            "owner_generation": generation,
            "winning_request_id": winning_id,
            "disposition": {
                "winner": "cleaned",
                "acknowledged": "cleaned",
                "sibling": "rejected_superseded",
                "no_winner": "rejected_terminal",
            },
            "requests": {request_id: {"kind": requests[request_id]} for request_id in sorted(requests)},
        }
        entries["control_receipts"] = _entry("control_receipts", keys, intent)
    if record.get("status") in {"error", "timeout"} and record.get("shard_created_by_spool"):
        worktree = (record.get("shard") or {}).get("worktree_path") or record.get("working_dir")
        intent = {
            "owner_generation": generation,
            "worktree_path": worktree,
            "reason": "automatic cleanup disabled after agent failure",
        }
        entries["failed_shard_preservation"] = _entry(
            "failed_shard_preservation", f"{generation}/preserve_failed_shard", intent
        )
    return entries


def _status_only_has_explicit_duties(spool_id: str, record: dict) -> bool | MailboxUnavailable:
    """Cheaply identify old terminal records with incomplete discovered work."""
    generation = (record.get(EPISODE_KEY) or {}).get("generation")
    requests = _current_generation_requests(spool_id, generation)
    if isinstance(requests, MailboxUnavailable):
        return requests
    discovered_keys = {}
    if requests:
        discovered_keys["control_receipts"] = {
            request.request_id: f"{generation}/{request.request_id}"
            for request in sorted(requests, key=lambda item: item.request_id)
        }
    if record.get("status") in {"error", "timeout"} and record.get("shard_created_by_spool"):
        discovered_keys["failed_shard_preservation"] = f"{generation}/preserve_failed_shard"

    entries = _obligation_entries(record)
    return any(
        not isinstance(entries.get(kind), dict)
        or entries[kind].get("progress") != "complete"
        or entries[kind].get("idempotency_key") != key
        for kind, key in discovered_keys.items()
    )


def _materialize_status_only_obligations(spool_id: str, captured: CapturedOwnerEpisodeEvidence) -> bool:
    """Freeze an old terminal's discovered duties before any release attempt."""
    import spindle

    record = captured.record
    if not (has_published_terminal(record) and not record.get("terminal_origin")):
        return False
    discovered = _status_only_obligations(captured)
    entries = dict(_obligation_entries(record))
    additions = {
        kind: entry
        for kind, entry in discovered.items()
        if kind not in entries
        or (
            entries[kind].get("progress") == "complete"
            and entries[kind].get("idempotency_key") != entry.get("idempotency_key")
        )
    }
    if not additions:
        return False
    entries.update(additions)
    record[CONVERGENCE_KEY] = {"format": CONVERGENCE_FORMAT, "obligations": entries}
    spindle._write_spool(spool_id, record)
    return True


def _may_materialize_before_cleanup_release(
    record: dict,
    observer: ObserverIdentity,
    meaning: _Meaning,
) -> bool:
    """Whether a local valid cleanup proof permits intent-only mutation."""
    episode = record.get(EPISODE_KEY)
    if not (
        isinstance(episode, dict) and episode.get("phase") == "cleanup_proven" and _same_namespace(episode, observer)
    ):
        return False
    if meaning.classification == "terminalizable" and meaning.may_mutate:
        return True
    if meaning.reason == "exact_ownership_inode_held":
        return getattr(meaning.lock, "state", None) == "held"
    if meaning.reason == "ownership_identity_mismatch":
        return getattr(meaning.lock, "state", None) == "identity_mismatch" and getattr(
            meaning.lock, "detail", None
        ) in {
            "path_missing",
            "inode_mismatch",
            "path_replaced_after_flock",
            "inode_mismatch_after_flock",
        }
    return False


def discoverable_duties_outstanding(spool_id: str, record: dict | None) -> bool:
    """Whether retention/recovery must preserve or materialize terminal work."""
    record = record or {}
    if obligations_outstanding(record):
        return True
    if not (
        isinstance(record.get(EPISODE_KEY), dict)
        and has_published_terminal(record)
        and not record.get("terminal_origin")
    ):
        return False
    discovery = _status_only_has_explicit_duties(spool_id, record)
    return True if isinstance(discovery, MailboxUnavailable) else discovery


def _apply_projection(record: dict, projection: TerminalProjection, captured: CapturedOwnerEpisodeEvidence) -> dict:
    """Build the one atomic terminal plus intent record."""
    value = dict(record)
    value.update(
        {
            "status": projection.status,
            "error": projection.error,
            "error_kind": projection.error_kind,
            "result": projection.result,
            "exit_code": projection.exit_code,
            "completed_at": projection.completed_at,
            "terminal_origin": projection.terminal_origin,
            "terminal_provenance": projection.terminal_provenance,
            "lifecycle": projection.lifecycle,
        }
    )
    if projection.error is None:
        value.pop("error", None)
    if projection.error_kind is None:
        value.pop("error_kind", None)
    if projection.exit_code is None:
        value.pop("exit_code", None)
    if projection.terminal_origin in {"natural_success", "natural_failure"}:
        proposal = _natural_proposal(record, captured.output["stdout"], captured.output["stderr"], projection.exit_code)
        if proposal.get("session_id") is not None:
            value["session_id"] = proposal["session_id"]
        if proposal.get("cost") is not None:
            value["cost"] = proposal["cost"]
        else:
            value.pop("cost", None)
        value["tags"] = proposal["tags"]
        if proposal.get("gate_category") is not None:
            value["gate_category"] = proposal["gate_category"]
        else:
            value.pop("gate_category", None)
        if proposal.get("pending_background_tasks") is not None:
            value["pending_background_tasks"] = proposal["pending_background_tasks"]
        else:
            value.pop("pending_background_tasks", None)
    else:
        value.pop("cost", None)
        value.pop("gate_category", None)
        value.pop("pending_background_tasks", None)
    value["owner_convergence"] = {
        "format": CONVERGENCE_FORMAT,
        "obligations": _obligations(captured, projection),
    }
    return value


def _capture_after_release(captured: CapturedOwnerEpisodeEvidence, release: dict) -> CapturedOwnerEpisodeEvidence:
    """Project the snapshot the exact-inode release transition will commit."""
    episode = copy.deepcopy(captured.episode or {})
    episode.update(
        phase="released",
        revision=(captured.revision or 0) + 1,
        release=release,
    )
    record = copy.deepcopy(captured.record)
    record[EPISODE_KEY] = episode
    return CapturedOwnerEpisodeEvidence(
        record=record,
        episode=episode,
        requests=captured.requests,
        receipts=captured.receipts,
        sidecars=captured.sidecars,
        output=captured.output,
        generation=captured.generation,
        revision=episode["revision"],
        observations=captured.observations,
    )


def _raise_if_crash(exc: Exception) -> None:
    if exc.__class__.__name__ == "InjectedCrash":
        raise exc


def _mark(record: dict, kind: str, *, completion: dict | None = None, error: Exception | None = None) -> None:
    import spindle

    entry = record["owner_convergence"]["obligations"][kind]
    if error is None:
        entry.update(progress="complete", completion=completion or {"complete": True}, error=None)
    else:
        entry.update(progress="pending", completion=None, error=f"{type(error).__name__}: {error}")
    spindle._write_spool(record["id"], record)


def _disposition_of(request_id: str, intent: dict) -> str:
    """What one captured request settles as under this episode's frozen rule."""
    winning = intent.get("winning_request_id")
    if winning is None:
        return intent["disposition"]["no_winner"]
    if request_id == winning:
        return intent["disposition"]["winner"]
    return intent["disposition"]["sibling"]


def _receipt_settles(receipt, request_id: str, intent: dict) -> bool:
    """Whether an existing receipt already discharges this request's duty.

    Existence is not settlement.  The owner writes the winner's receipt the
    moment it accepts control - ``accepted``, with no child exit on it yet - and
    only fills in the cleanup terminal once the provider is really gone.  A
    converger that reads the first form as done leaves a request whose durable
    receipt says an owner is still working on it, forever.  So all three facts
    the obligation names have to hold: the receipt belongs to this generation,
    it carries the disposition the intent assigned it, and its cleanup fields
    are terminal.
    """
    if receipt.owner_generation != intent.get("owner_generation"):
        return False
    if receipt.cleanup_outcome in _UNSETTLED_CLEANUP_OUTCOMES:
        return False
    if receipt.owner_acknowledged_at:
        return bool(
            receipt.child_exit_observed_at
            and receipt.cleanup_outcome == intent["disposition"].get("acknowledged", "cleaned")
        )
    return receipt.cleanup_outcome == _disposition_of(request_id, intent)


def _finish_receipt(spool_id: str, receipt, request_id: str, intent: dict, record: dict):
    """Advance one unfinished receipt onto the settlement its duty names.

    An acknowledged request is finished from the episode's own proven cleanup -
    the child exit and outcome the owner or watchdog published - because that is
    the evidence the dead owner would have written the receipt from.  A request
    nobody acknowledged simply takes its disposition.
    """
    import spindle

    if receipt.owner_acknowledged_at:
        episode = record.get(EPISODE_KEY) or {}
        cleanup = episode.get("cleanup") or {}
        child_exit = cleanup.get("child_exit_observed_at") or (episode.get("containment") or {}).get("observed_at")
        cleanup_outcome = cleanup.get("outcome")
        if not child_exit or not cleanup_outcome:
            raise OSError(
                f"control request {request_id} was acknowledged and never finished, and this episode "
                f"proves no cleanup to finish its receipt from"
            )
        # The episode describes why the generation stopped (``stopped``,
        # ``watchdog_contained``, ...); the receipt describes whether provider
        # cleanup finished. Preserve the established receipt vocabulary the
        # live owner writes instead of copying the episode outcome verbatim.
        receipt_outcome = (
            "descendants_survived"
            if cleanup_outcome == "descendants_survived" or cleanup.get("provider_reaped") is False
            else "cleaned"
        )
        facts = {"child_exit_observed_at": child_exit, "cleanup_outcome": receipt_outcome}
    else:
        facts = {"cleanup_outcome": _disposition_of(request_id, intent)}
    return spindle.update_control_receipt(spindle.SPINDLE_DIR, spool_id, request_id, **facts)


def _run_obligations(spool_id: str, record: dict) -> dict:
    import spindle

    entries = (record.get("owner_convergence") or {}).get("obligations") or {}
    generation = (record.get("owner_episode") or {}).get("generation")
    for kind in tuple(entries):
        entry = entries[kind]
        if entry.get("progress") == "complete":
            continue
        try:
            if kind == "control_receipts":
                outcomes = {}
                receipt_errors = []
                intent = entry["intent"]
                requests = {
                    request.request_id: request
                    for request in spindle.iter_control_requests(spindle.SPINDLE_DIR, spool_id)
                }
                for request_id in intent["requests"]:
                    try:
                        receipt = spindle.read_control_receipt(spindle.SPINDLE_DIR, spool_id, request_id)
                        if receipt is None:
                            request = requests[request_id]
                            winning = request_id == intent.get("winning_request_id")
                            acknowledgement = (record.get(EPISODE_KEY) or {}).get("acknowledgement") or {}
                            receipt = spindle.write_control_receipt(
                                spindle.SPINDLE_DIR,
                                spool_id,
                                request,
                                current_generation=generation,
                                accepted=winning,
                                rejection_outcome=_disposition_of(request_id, intent),
                                owner_acknowledged_at=(acknowledgement.get("acknowledged_at") if winning else None),
                            )
                        if not _receipt_settles(receipt, request_id, intent):
                            # A receipt an owner opened and never finished - accepted,
                            # with no cleanup terminal on it - is the duty still owed,
                            # not evidence that it was discharged.
                            receipt = _finish_receipt(spool_id, receipt, request_id, intent, record)
                        if not _receipt_settles(receipt, request_id, intent):
                            raise OSError(f"control request {request_id} still has no settling receipt")
                        outcomes[request_id] = receipt.cleanup_outcome
                    except MalformedControlReceipt as exc:
                        receipt_errors.append(str(exc))
                # Stale-generation requests are not convergence obligations for
                # this episode, but the established mailbox protocol still
                # gives them their generation-scoped rejection when observed.
                for request in requests.values():
                    if request.owner_generation == generation:
                        continue
                    try:
                        receipt = spindle.read_control_receipt(spindle.SPINDLE_DIR, spool_id, request.request_id)
                        if receipt is None:
                            spindle.write_control_receipt(
                                spindle.SPINDLE_DIR,
                                spool_id,
                                request,
                                current_generation=generation,
                                accepted=False,
                            )
                    except MalformedControlReceipt as exc:
                        receipt_errors.append(str(exc))
                if receipt_errors:
                    raise ValueError("; ".join(receipt_errors))
                _mark(record, kind, completion={"receipts": outcomes})
            elif kind == "failed_shard_preservation":
                spindle._preserve_failed_spool_shard(record)
                _mark(
                    record,
                    kind,
                    completion={
                        "startup_failure_preserved": bool((record.get("shard") or {}).get("startup_failure_preserved")),
                        "shard_cleanup_preserved": bool(record.get("shard_cleanup_preserved")),
                    },
                )
            elif kind == "natural_transcript":
                intent = entry["intent"]
                source = Path(intent["source_path"])
                stdout = source.read_text()
                if _digest(stdout) != intent["digest"] or len(stdout.encode()) != intent["size"]:
                    raise OSError("stdout capture no longer matches transcript intent")
                transcript = spindle._get_transcript_path(spool_id)
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(stdout)
                if _digest(transcript.read_text()) != intent["digest"]:
                    raise OSError("persisted transcript digest mismatch")
                _mark(record, kind, completion={"path": str(transcript), "digest": intent["digest"]})
        except Exception as exc:  # durable effects report errors and remain retryable
            _raise_if_crash(exc)
            _mark(record, kind, error=exc)
    return record


def _projection_from_record(record: dict) -> TerminalProjection | None:
    if not record.get("terminal_origin"):
        return None
    lifecycle = dict(record.get("lifecycle") or {})
    return TerminalProjection(
        status=record.get("status"),
        error=record.get("error"),
        error_kind=record.get("error_kind"),
        result=record.get("result"),
        exit_code=record.get("exit_code"),
        completed_at=record.get("completed_at"),
        terminal_origin=record.get("terminal_origin"),
        terminal_provenance=dict(record.get("terminal_provenance") or {}),
        lifecycle=lifecycle,
    )


def _obligation_entries(record: dict | None) -> dict:
    entries = ((record or {}).get(CONVERGENCE_KEY) or {}).get("obligations") or {}
    return entries if isinstance(entries, dict) else {}


def obligations_outstanding(record: dict | None) -> tuple[str, ...]:
    """Durable duties this record's published terminal still owes, by kind.

    Retention must read this before deleting the artifact set: the block is the
    only place the owed work is replayable from, so dropping it while a duty is
    pending destroys the duty.
    """
    return tuple(
        sorted(kind for kind, entry in _obligation_entries(record).items() if entry.get("progress") != "complete")
    )


def has_published_terminal(record: dict | None) -> bool:
    """Whether *record* already carries a settled public outcome."""
    record = record or {}
    if record.get("terminal_origin"):
        return True
    # Old or interrupted writers may have omitted completed_at. Their terminal
    # status still names an outcome; the compatibility path may not replace it
    # merely because the record is malformed or incomplete.
    return record.get("status") in TERMINAL_STATUSES


def applicator_owns_outcome(record: dict | None) -> bool:
    """Whether only this module may still write *record*'s public outcome.

    Two record classes qualify: an owner-episode record, whose outcome is
    reduced from episode evidence by convergence alone, and any record that
    already published a terminal - a settled outcome is immutable whoever wrote
    it, so a second writer is publishing over proven meaning either way.
    """
    record = record or {}
    return isinstance(record.get(EPISODE_KEY), dict) or has_published_terminal(record)


def _protected_update_fields(updates: dict | None) -> set:
    """Protected outcome fields a compatibility update would write."""
    written = set()
    for name, value in (updates or {}).items():
        if name in PROTECTED_RECORD_FIELDS or name == EPISODE_KEY:
            written.add(name)
        elif name == "status":
            written.add(name)
        elif name == "lifecycle" and isinstance(value, dict):
            for field_name in value:
                if field_name in ALWAYS_TERMINAL_LIFECYCLE or field_name in TERMINAL_LIFECYCLE_VALUES:
                    written.add(f"lifecycle.{field_name}")
    return written


_CODEX_RESPIN_SESSION_BINDING = "_codex_respin_source_session_id"
_CODEX_REFUSAL_PROJECTION_KEYS = frozenset(
    {
        "codex_bin",
        "codex_version",
        "completed_at",
        "error",
        "harness",
        "permission",
        "pid",
        "result",
        "sandbox",
        "sandbox_error",
        "session_id",
        "status",
        "tags",
    }
)
_CODEX_REFUSAL_FORBIDDEN_RECORD_KEYS = frozenset(
    {
        "codex_bin",
        "codex_version",
        "completed_at",
        "error",
        "error_kind",
        "exit_code",
        "harness",
        "launcher_pid",
        "launcher_start_time",
        "lifecycle",
        "owner_convergence",
        "permission",
        "pid",
        "result",
        "sandbox",
        "sandbox_error",
        "session_id",
        "terminal_origin",
        "terminal_provenance",
    }
)


def _is_real_episode_int(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _is_exact_episode_namespace(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("status") == "supported":
        return set(value) == {"status", "device", "inode"} and all(
            isinstance(value[field], int) and not isinstance(value[field], bool) and value[field] >= 0
            for field in ("device", "inode")
        )
    return (
        value.get("status") == "unsupported"
        and set(value) == {"status", "reason"}
        and isinstance(value.get("reason"), str)
        and bool(value["reason"])
    )


def _is_exact_episode_starter(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"pid", "birth_token", "namespace"}
        and isinstance(value.get("pid"), int)
        and not isinstance(value.get("pid"), bool)
        and value["pid"] > 0
        and isinstance(value.get("birth_token"), str)
        and bool(value["birth_token"])
        and _is_exact_episode_namespace(value.get("namespace"))
    )


def _is_exact_aborted_episode_shape(episode: dict) -> bool:
    required = {"format", "generation", "revision", "phase", "phase_times", "starter", "failure"}
    allowed = required | {"deadline"}
    phase_times = episode.get("phase_times")
    return bool(
        frozenset(episode) in {frozenset(required), frozenset(allowed)}
        and isinstance(phase_times, dict)
        and set(phase_times) == {"reserved", "aborted"}
        and all(isinstance(value, str) and bool(value) for value in phase_times.values())
        and ("deadline" not in episode or isinstance(episode.get("deadline"), str) and bool(episode["deadline"]))
    )


def _is_exact_codex_refusal_projection(record: dict | None, updates: dict | None) -> bool:
    record = record or {}
    updates = updates or {}
    episode = record.get(EPISODE_KEY)
    failure = episode.get("failure") if isinstance(episode, dict) else None
    binding = record.get(_CODEX_RESPIN_SESSION_BINDING)
    optional_strings = ("sandbox", "permission", "codex_bin", "codex_version")
    return bool(
        record.get("status") == "pending"
        and isinstance(record.get("id"), str)
        and bool(record["id"])
        and record.get("tags") == ["codex", "respin"]
        and isinstance(binding, str)
        and bool(binding)
        and not (_CODEX_REFUSAL_FORBIDDEN_RECORD_KEYS & record.keys())
        and isinstance(episode, dict)
        and episode.get("format") == EPISODE_FORMAT
        and _is_real_episode_int(episode.get("generation"), 1)
        and _is_real_episode_int(episode.get("revision"), 2)
        and episode.get("phase") == "aborted"
        and _is_exact_aborted_episode_shape(episode)
        and _is_exact_episode_starter(episode.get("starter"))
        and isinstance(failure, dict)
        and set(failure) == {"kind", "detail", "observed_at"}
        and failure.get("kind") == "launcher_pre_spawn_failure"
        and isinstance(failure.get("detail"), str)
        and bool(failure["detail"])
        and isinstance(failure.get("observed_at"), str)
        and bool(failure["observed_at"])
        and set(updates) == _CODEX_REFUSAL_PROJECTION_KEYS
        and updates.get("session_id") == binding
        and updates.get("harness") == "codex"
        and updates.get("tags") == ["codex", "respin"]
        and "pid" in updates
        and updates.get("pid") is None
        and updates.get("status") == "error"
        and updates.get("error") == failure.get("detail")
        and updates.get("sandbox_error") == failure.get("detail")
        and "result" in updates
        and updates.get("result") is None
        and isinstance(updates.get("completed_at"), str)
        and bool(updates["completed_at"])
        and all(updates.get(name) is None or isinstance(updates.get(name), str) for name in optional_strings)
    )


def protected_update_refusal(record: dict | None, updates: dict | None) -> str | None:
    """Name the protected fields *updates* may not write on *record*, if any."""
    protected_fields = _protected_update_fields(updates)
    if _is_exact_codex_refusal_projection(record, updates):
        return None
    if not applicator_owns_outcome(record):
        return None
    refused = sorted(protected_fields)
    if not refused:
        return None
    subject = "an owner-episode record" if isinstance((record or {}).get(EPISODE_KEY), dict) else "a settled record"
    return f"refusing to write {', '.join(refused)} on {subject}"


def _result(meaning: _Meaning, record: dict | None, local_errors=()) -> ConvergenceResult:
    entries = _obligation_entries(record)
    obligations = tuple(Obligation(**entry) for entry in entries.values())
    if not has_published_terminal(record):
        terminal_state = "none"
    elif not obligations_outstanding(record) and not local_errors:
        terminal_state = "fully_converged"
    else:
        terminal_state = "terminal_with_obligations_pending"
    return ConvergenceResult(
        classification=meaning.classification,
        reason=meaning.reason,
        may_mutate=meaning.may_mutate,
        refusal_reason=meaning.refusal_reason,
        terminal_state=terminal_state,
        projection=_projection_from_record(record or {}),
        obligations=obligations,
        local_errors=tuple(local_errors),
    )


def _mailbox_unavailable_result(
    record: dict,
    observer: ObserverIdentity,
    unavailable: MailboxUnavailable,
) -> ConvergenceResult:
    """Keep one unreadable mailbox retryable without projecting an outcome."""
    observed = classify_owner_episode_record(record, observer)
    meaning = _Meaning(
        "unverifiable",
        "mailbox_unavailable",
        False,
        unavailable.error,
        observed.lock,
        observed.liveness,
    )
    return _result(meaning, record, (unavailable.error,))


def converge_owner_episode(spool_id: str, observer: ObserverIdentity) -> ConvergenceResult:
    """Capture, reduce, CAS, and discharge one bounded convergence pass."""
    import spindle

    local_errors: list[str] = []
    for _attempt in range(8):
        # The common supervisor pass is a live owner. Classify it from the
        # record and kernel evidence before taking blocking guards or reading
        # captures. Terminalizable transitions revalidate under the frozen
        # mailbox+record capture protocol below before mutating anything.
        lightweight_record = spindle._read_spool(spool_id)
        if lightweight_record is None:
            missing = _Meaning("store_unhealthy", "spool_missing", False, "spool missing", None, None)
            return _result(missing, None)
        lightweight_meaning = classify_owner_episode_record(lightweight_record, observer)
        status_only = has_published_terminal(lightweight_record) and not lightweight_record.get("terminal_origin")
        status_only_with_authorized_duties = False
        if status_only:
            discovery = _status_only_has_explicit_duties(spool_id, lightweight_record)
            if isinstance(discovery, MailboxUnavailable):
                return _mailbox_unavailable_result(lightweight_record, observer, discovery)
            if not discovery:
                return _result(lightweight_meaning, lightweight_record)
            status_only_with_authorized_duties = _may_materialize_before_cleanup_release(
                lightweight_record,
                observer,
                lightweight_meaning,
            )
        if (
            lightweight_meaning.classification != "terminalizable" or not lightweight_meaning.may_mutate
        ) and not status_only_with_authorized_duties:
            return _result(lightweight_meaning, lightweight_record)
        with spindle.mailbox_guard(spindle.SPINDLE_DIR, spool_id):
            with spindle._spool_lock(spool_id) as acquired:
                if not acquired:
                    missing = _Meaning(
                        "unverifiable",
                        "spool_record_lock_unavailable",
                        False,
                        "spool record lock unavailable",
                        None,
                        None,
                    )
                    return _result(missing, spindle._read_spool(spool_id))
                captured = _capture(spool_id, observer)
                if captured is None:
                    missing = _Meaning("store_unhealthy", "spool_missing", False, "spool missing", None, None)
                    return _result(missing, None)
                if isinstance(captured, MailboxUnavailable):
                    current = spindle._read_spool(spool_id)
                    if current is None:
                        missing = _Meaning("store_unhealthy", "spool_missing", False, "spool missing", None, None)
                        return _result(missing, None)
                    return _mailbox_unavailable_result(current, observer, captured)
                meaning: _Meaning = captured.observations["meaning"]
                for error in captured.observations.get("receipt_errors", {}).values():
                    if error not in local_errors:
                        local_errors.append(error)
                authorized = meaning.classification == "terminalizable" and meaning.may_mutate
                pre_release_authorized = _may_materialize_before_cleanup_release(
                    captured.record,
                    observer,
                    meaning,
                )
                if not authorized and not pre_release_authorized:
                    return _result(meaning, captured.record, local_errors)
                published = _materialize_status_only_obligations(spool_id, captured)
                if captured.episode is None or not authorized:
                    return _result(meaning, captured.record, local_errors)

                episode = captured.episode
                if episode.get("phase") == "cleanup_proven":
                    lock_fact = episode.get("lock") or {}
                    try:
                        held = spindle.acquire_ownership_lock(spindle._get_owner_lock_path(spool_id))
                    except (BlockingIOError, OSError, RuntimeError):
                        refused = _Meaning(
                            "active",
                            "exact_ownership_inode_held",
                            False,
                            "ownership inode is still held",
                            meaning.lock,
                            meaning.liveness,
                        )
                        return _result(refused, captured.record)
                    try:
                        if (held.device, held.inode) != (lock_fact.get("device"), lock_fact.get("inode")):
                            unhealthy = _Meaning(
                                "store_unhealthy",
                                "ownership_identity_mismatch",
                                False,
                                "ownership pathname no longer names the recorded inode",
                                meaning.lock,
                                meaning.liveness,
                            )
                            return _result(unhealthy, captured.record)
                        release = {
                            "device": held.device,
                            "inode": held.inode,
                            "proved_by": "reconciler",
                            "released_at": datetime.now(timezone.utc).isoformat(),
                        }
                        released_capture = _capture_after_release(captured, release)
                        if has_published_terminal(released_capture.record) and not released_capture.record.get(
                            "terminal_origin"
                        ):
                            terminal_record = released_capture.record
                        else:
                            projection = _projection(released_capture)
                            terminal_record = _apply_projection(
                                released_capture.record,
                                projection,
                                released_capture,
                            )
                        record_updates = dict(terminal_record)
                        record_updates.pop(EPISODE_KEY, None)
                        record_deletes = tuple(
                            name
                            for name in released_capture.record
                            if name != EPISODE_KEY and name not in terminal_record
                        )
                        released = spindle.transition_owner_episode(
                            spindle.SPINDLE_DIR,
                            spool_id,
                            actor="reconciler",
                            destination="released",
                            generation=captured.generation,
                            expected_revision=captured.revision,
                            facts={"release": release},
                            record_updates=record_updates,
                            record_deletes=record_deletes,
                            record_locked=True,
                        )
                    finally:
                        held.close()
                    if not released.accepted:
                        continue
                    published = True
                    captured = _capture(spool_id, observer)
                    if captured is None:
                        continue
                    if isinstance(captured, MailboxUnavailable):
                        current = spindle._read_spool(spool_id)
                        if current is None:
                            continue
                        return _mailbox_unavailable_result(current, observer, captured)
                    meaning = captured.observations["meaning"]
                    for error in captured.observations.get("receipt_errors", {}).values():
                        if error not in local_errors:
                            local_errors.append(error)

                record = captured.record
                if not has_published_terminal(record):
                    projection = _projection(captured)
                    record = _apply_projection(record, projection, captured)
                    spindle._write_spool(spool_id, record)
                    published = True
                record = _run_obligations(spool_id, record)
                if published:
                    try:
                        spindle._post_terminal_bookkeeping(spool_id, record)
                    except Exception as exc:
                        _raise_if_crash(exc)
                        local_errors.append(f"{type(exc).__name__}: {exc}")

        effects = observer.local_effects
        handle_reap = effects.get("handle_reap") if isinstance(effects, dict) else getattr(effects, "handle_reap", None)
        if handle_reap is not None:
            try:
                handle_reap(spool_id)
            except Exception as exc:
                _raise_if_crash(exc)
                local_errors.append(f"{type(exc).__name__}: {exc}")
        latest = spindle._read_spool(spool_id) or record
        return _result(meaning, latest, local_errors)

    latest = spindle._read_spool(spool_id)
    stale = _Meaning(
        "unverifiable",
        "capture_precondition_changed_repeatedly",
        False,
        "generation or revision kept changing",
        None,
        None,
    )
    return _result(stale, latest)


def publish_record_updates(spool_id: str, record: dict, updates: dict) -> None:
    """Compatibility applicator for explicitly non-episode terminal writers.

    The closed-writer rule is about which *module* publishes a terminal, and a
    dict of arbitrary fields handed across that boundary would satisfy it while
    letting any caller rewrite ``completed_at`` or an outcome on a record whose
    meaning is already proven.  So the boundary carries the rule with it: a
    caller may take a terminal here only on a record convergence does not own -
    a legacy record that has not settled yet, or one being created.
    """
    import spindle

    refusal = protected_update_refusal(record, updates)
    if refusal:
        raise ProtectedRecordUpdate(f"{spool_id}: {refusal}")
    value = dict(record)
    value.update(updates)
    spindle._write_spool(spool_id, value)


def finalize_legacy_stale_reservation(spool_id: str, record: dict, completed_at: str) -> None:
    """Publish an expired pre-envelope reservation and preserve its shard."""
    import spindle

    value = dict(record)
    value.update(
        status="error",
        error="spawn timeout - never started",
        completed_at=completed_at,
    )
    spindle._preserve_failed_spool_shard(value)
    spindle._write_spool(spool_id, value)


def finalize_legacy_spool(spool_id: str, record: dict) -> bool:
    """Preserve the pre-episode natural finalizer behind the applicator boundary."""
    import spindle

    if record.get("status") not in {"pending", "running"}:
        return True
    try:
        stdout = spindle._get_output_path(spool_id).read_text()
    except (FileNotFoundError, OSError):
        stdout = ""
    try:
        stderr = spindle._get_stderr_path(spool_id).read_text()
    except (FileNotFoundError, OSError):
        stderr = ""
    process = spindle._PROC_HANDLES.get(spool_id)
    observed_exit = process.poll() if process is not None else None
    exit_code = _normalise_exit(observed_exit)
    if exit_code is None:
        exit_code = _normalise_exit(spindle._read_exit_code(spool_id))
    proposal = _natural_proposal(record, stdout, stderr, exit_code)
    value = dict(record)
    value.update(
        status=proposal["status"],
        error=proposal["error"],
        error_kind=proposal["error_kind"],
        result=proposal["result"],
        exit_code=exit_code,
        completed_at=datetime.now().isoformat(),
    )
    if proposal["error"] is None:
        value.pop("error", None)
    if proposal["error_kind"] is None:
        value.pop("error_kind", None)
    if exit_code is None:
        value.pop("exit_code", None)
    if proposal.get("session_id") is not None:
        value["session_id"] = proposal["session_id"]
    if proposal.get("cost") is not None:
        value["cost"] = proposal["cost"]
    value["tags"] = proposal["tags"]
    if proposal.get("gate_category") is not None:
        value["gate_category"] = proposal["gate_category"]
    else:
        value.pop("gate_category", None)
    if proposal.get("pending_background_tasks") is not None:
        value["pending_background_tasks"] = proposal["pending_background_tasks"]
    else:
        value.pop("pending_background_tasks", None)
    spindle._write_spool(spool_id, value)
    if stdout and (proposal.get("session_id") or record.get("claude_protocol")):
        try:
            transcript = spindle._get_transcript_path(spool_id)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(stdout)
        except IOError:
            pass
    try:
        spindle._post_terminal_bookkeeping(spool_id, value)
    finally:
        spindle._pop_and_reap_process_handle(spool_id, observed_exit=observed_exit)
    return True
