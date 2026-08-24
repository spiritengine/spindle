"""Persistent stream-json driver for headless Claude Code spools.

Why this exists: a one-shot ``claude -p`` prints one response and exits. When
the agent ends its turn with a nonpersistent background task still pending
(a Monitor, a background Bash command, a scheduled wakeup), the CLI emits a
successful terminal result and exits — the completion notification that would
have woken the agent in an interactive harness has no process left to deliver
to. Spindle then records the stub turn as a completed spool (the "parked"
false-completion, issue-20260724-tqs3 / finding-20260724-2niy).

This driver keeps the Claude process and its stream-json stdin OPEN after an
end-turn result while any nonpersistent background task is unresolved, so the
CLI can deliver the task notification and run the follow-up model turn. It
emits a spindle-owned terminal sentinel event only when a result arrives with
no unresolved tasks (or when further progress is provably impossible), and
spindle finalizes spools off that sentinel instead of intermediate results.

This module is deliberately a standalone top-level module, NOT part of the
``spindle`` package: importing ``spindle`` runs MCP server startup/recovery
side effects, and both the child driver process and the server need the pure
event-tracking logic below. The dependency points one way — ``spindle``
imports this module, never the reverse.

Event shapes tracked here (observed against Claude Code 2.1.219, incident
transcripts 688e30c3/7de8960e):

- Monitor arm: user event, ``tool_use_result: {"taskId": "bhbhqvqfz",
  "timeoutMs": 600000, "persistent": false}``, tool_result content
  "Monitor started (task bhbhqvqfz, ...)".
- Background Bash arm: user event, ``tool_use_result.backgroundTaskId``,
  content "Command running in background with ID: ...".
- Scheduled wakeup arm: user event, ``tool_use_result: {"scheduledFor":
  1784861700000, ...}``.
- Resolution: a user message (not a tool result) whose text contains a
  ``<task-notification>`` block with ``<task-id>`` and a terminal
  ``<status>`` (completed/failed/stopped).
"""

import argparse
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DRIVER_PROTOCOL = "stream-driver-v1"
SENTINEL_TYPE = "spindle_driver_terminal"

# Statuses inside a <task-notification> that end a task's lifecycle.
# "killed" is delivered when a background command is stopped mid-run
# (KillShell, harness/user stop) — observed in the live spool store and in
# interactive session JSONLs (fell round 4, finding-20260724-ja3l). The
# binary also carries a "timed_out" literal, but it has zero observed
# occurrences and Monitor timeouts are already bounded by the park deadline.
TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "killed"}

TASK_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
TASK_STATUS_RE = re.compile(r"<status>\s*([^<\s]+)\s*</status>")
# Queued-command attachments are interactive-harness control blocks; they are
# stripped alongside task notifications when sanitizing transcripts.
QUEUED_COMMAND_RE = re.compile(r"<queued-command[^>]*>(.*?)</queued-command>", re.DOTALL)

# Text fallbacks for task arms, anchored to the START of a tool_result content
# string so command output that merely quotes these phrases (an agent reading
# an old transcript, for example) cannot arm a phantom task.
MONITOR_STARTED_RE = re.compile(r"\AMonitor started \(task ([A-Za-z0-9_-]+)")
BACKGROUND_CMD_RE = re.compile(r"\ACommand running in background with ID:\s*([A-Za-z0-9_-]+)")

WAKEUP_TASK_ID = "scheduled-wakeup"

CLAUDE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
CLAUDE_ABORT_REASONS = {"aborted_streaming", "aborted_tools"}


def _cap_utf8(value, limit=500):
    """Return a valid UTF-8 string no larger than *limit* encoded bytes."""
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _bounded_summary(parts):
    return _cap_utf8("; ".join(part for part in parts if part), 500)


def _number_text(value):
    """Return a compact finite non-negative number, or None for unsafe input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        rendered = str(value)
    except (OverflowError, ValueError):
        return None
    return rendered if len(rendered) <= 100 else None


def claude_conversation_summary(event):
    """Build the bounded, non-prose summary persisted for Claude init."""
    if not isinstance(event, dict):
        return ""
    parts = []
    version = event.get("version")
    if not isinstance(version, str) or not version:
        version = event.get("claude_code_version")
    if isinstance(version, str) and version:
        parts.append(f"version={version}")
    capabilities = event.get("capabilities")
    if isinstance(capabilities, list):
        clean = sorted({item for item in capabilities if isinstance(item, str) and item})
        if clean:
            parts.append(f"capabilities={','.join(clean)}")
    return _bounded_summary(parts)


def normalize_claude_terminal_kind(event):
    """Conservatively normalize a Claude result without treating unknowns as success."""
    if not isinstance(event, dict):
        return "indeterminate"
    if event.get("stop_reason") == "refusal":
        return "refused"
    terminal_reason = event.get("terminal_reason")
    if terminal_reason in CLAUDE_ABORT_REASONS:
        return "interrupted"
    subtype = event.get("subtype")
    if event.get("is_error") is True or (
        isinstance(subtype, str)
        and (
            subtype.startswith("error")
            or subtype in {"failed", "api_error", "max_turns", "max_budget_usd", "execution_error"}
        )
    ):
        return "failed"
    if (
        subtype == "success"
        and event.get("is_error") is False
        and terminal_reason in (None, "completed")
    ):
        return "completed"
    return "indeterminate"


def claude_terminal_summary(event):
    """Build the terminal scalar allowlist; never include model or tool prose."""
    if not isinstance(event, dict):
        return ""
    parts = []
    terminal_reason = event.get("terminal_reason")
    if isinstance(terminal_reason, str) and terminal_reason:
        parts.append(f"terminal_reason={terminal_reason}")
    denials = event.get("permission_denials")
    if isinstance(denials, list):
        parts.append(f"permission_denials={len(denials)}")
    cost = event.get("total_cost_usd")
    cost_text = _number_text(cost)
    if cost_text is not None:
        parts.append(f"total_cost_usd={cost_text}")
    usage = event.get("usage")
    if isinstance(usage, dict):
        for key in CLAUDE_USAGE_KEYS:
            value = usage.get(key)
            value_text = _number_text(value)
            if value_text is not None:
                parts.append(f"{key}={value_text}")
    return _bounded_summary(parts)


def _load_lifecycle_runtime():
    """Lazily import the durable reducer without starting the Spindle server."""
    guard = "_SPINDLE_STORE_SUPERVISOR"
    previous = os.environ.get(guard)
    os.environ[guard] = "1"
    try:
        from spindle import lifecycle
    finally:
        if previous is None:
            os.environ.pop(guard, None)
        else:
            os.environ[guard] = previous
    return lifecycle


class ClaudeLifecycleTelemetry:
    """Best-effort Claude-event adapter for the provider lifecycle reducer.

    The owner and this emitter both serialize each short record transition with
    ``<id>.lock``. The emitter never calls ``apply_event`` while holding that
    lock itself, so there is no recursive acquisition or lock-order inversion.
    """

    def __init__(self):
        self.enabled = False
        self._runtime = None
        self._store = None
        self._spool_id = None
        self._coalescer = None
        self._active_tasks = set()
        self._approval_requests = set()
        self._result_candidate = None
        store = os.environ.get("SPINDLE_OWNER_STORE")
        spool_id = os.environ.get("SPINDLE_OWNER_SPOOL_ID")
        if not store or not spool_id:
            return
        try:
            self._runtime = _load_lifecycle_runtime()
            self._store = Path(store)
            self._spool_id = spool_id
            self._coalescer = self._runtime.ActivityCoalescer(spool_id)
            self.enabled = True
        except Exception as exc:
            self._disable(exc)

    def _disable(self, exc):
        print(f"spindle_claude_driver: lifecycle telemetry disabled: {exc}", file=sys.stderr)
        self.enabled = False

    def _apply(self, event):
        if not self.enabled:
            return False
        try:
            self._runtime.apply_event(self._store, self._spool_id, event)
            return True
        except Exception as exc:
            self._disable(exc)
            return False

    def _event(self, kind, **kwargs):
        return self._runtime.LifecycleEvent(spool_id=self._spool_id, kind=kind, **kwargs)

    def _flush_activity(self):
        if self.enabled:
            pending = self._coalescer.flush()
            if pending is not None:
                self._apply(pending)

    def _emit(self, kind, **kwargs):
        if not self.enabled:
            return
        self._flush_activity()
        if self.enabled:
            try:
                event = self._event(kind, **kwargs)
            except Exception as exc:
                self._disable(exc)
                return
            self._apply(event)

    def activity(self, summary="Claude activity"):
        if not self.enabled:
            return
        event = self._coalescer.observe(summary=summary)
        if event is not None:
            self._apply(event)

    def transport_started(self):
        if self.enabled:
            self._emit(self._runtime.TRANSPORT_STARTED, provider_event="claude.spawned")

    def transport_lost(self, message=None):
        if self.enabled:
            error = {"type": "ClaudeTransportError", "message": message} if isinstance(message, str) else None
            self._emit(self._runtime.TRANSPORT_LOST, provider_event="claude.stdout_lost", error=error)

    def transport_exited(self, exit_code):
        if self.enabled:
            raw = str(exit_code) if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
            self._emit(self._runtime.TRANSPORT_EXITED, provider_event="claude.exited", raw_status=raw)

    def turn_started(self):
        if self.enabled:
            self._emit(self._runtime.TURN_STARTED, provider_event="user.dispatched")

    def _work_summary(self):
        count = len(self._active_tasks)
        if count == 1:
            return "1 Claude task active"
        if count > 1:
            return f"{count} Claude tasks active"
        return None

    def _observe_task(self, event, subtype):
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            self.activity()
            return
        self.activity()
        if subtype == "task_started":
            self._active_tasks.add(task_id)
            self._emit(
                self._runtime.WORK_STARTED,
                provider_event="system.task_started",
                summary=self._work_summary(),
            )
            return
        if subtype == "task_progress":
            self._emit(
                self._runtime.WORK_UPDATED,
                provider_event="system.task_progress",
                summary=self._work_summary(),
            )
            return
        if subtype == "task_updated":
            patch = event.get("patch")
            status = patch.get("status") if isinstance(patch, dict) else None
        else:
            status = event.get("status")
        if not isinstance(status, str) or status not in TERMINAL_TASK_STATUSES | {
            "pending",
            "running",
            "paused",
        }:
            return
        if status in TERMINAL_TASK_STATUSES:
            self._active_tasks.discard(task_id)
            kind = self._runtime.WORK_UPDATED if self._active_tasks else self._runtime.WORK_FINISHED
        else:
            already_active = task_id in self._active_tasks
            self._active_tasks.add(task_id)
            kind = self._runtime.WORK_UPDATED if already_active or status == "paused" else self._runtime.WORK_STARTED
        self._emit(
            kind,
            provider_event=f"system.{subtype}",
            raw_status=status if isinstance(status, str) else None,
            summary=self._work_summary(),
        )

    def _remember_result(self, event):
        ids = {}
        session_id = event.get("session_id")
        if isinstance(session_id, str):
            ids["session_id"] = session_id
        errors = event.get("errors")
        error = None
        if isinstance(errors, list):
            first = next((item for item in errors if isinstance(item, str) and item), None)
            if first:
                error = {"type": "ClaudeResultError", "message": first}
        api_error_status = event.get("api_error_status")
        if error is None and isinstance(api_error_status, (str, int)) and not isinstance(api_error_status, bool):
            error = {"type": "ClaudeResultError", "code": api_error_status}
        subtype = event.get("subtype")
        self._result_candidate = {
            "provider_event": f"result.{subtype}" if isinstance(subtype, str) else "result",
            "provider_ids": ids,
            "terminal_kind": normalize_claude_terminal_kind(event),
            "raw_status": subtype if isinstance(subtype, str) else None,
            "stop_reason": event.get("stop_reason") if isinstance(event.get("stop_reason"), str) else None,
            "error": error,
            "summary": claude_terminal_summary(event),
        }

    def observe(self, event):
        if not self.enabled or not isinstance(event, dict):
            return
        try:
            self._observe(event)
        except Exception as exc:
            self._disable(exc)

    def _observe(self, event):
        etype = event.get("type")
        subtype = event.get("subtype")
        if etype == "system" and subtype == "init":
            ids = {}
            if isinstance(event.get("session_id"), str):
                ids["session_id"] = event["session_id"]
            self._emit(self._runtime.PROTOCOL_CONNECTED, provider_event="system.init", provider_ids=ids)
            self._emit(
                self._runtime.CONVERSATION_ACCEPTED,
                provider_event="system.init",
                provider_ids=ids,
                summary=claude_conversation_summary(event),
            )
        elif etype == "system" and subtype in {
            "task_started",
            "task_progress",
            "task_updated",
            "task_notification",
        }:
            self._observe_task(event, subtype)
        elif etype == "control_request":
            request = event.get("request")
            request_id = event.get("request_id")
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool" and isinstance(request_id, str):
                self._approval_requests.add(request_id)
                self._emit(self._runtime.APPROVAL_REQUESTED, provider_event="control_request.can_use_tool")
        elif etype == "control_response":
            response = event.get("response")
            request_id = response.get("request_id") if isinstance(response, dict) else None
            if isinstance(request_id, str) and request_id in self._approval_requests:
                self._approval_requests.discard(request_id)
                self._emit(self._runtime.APPROVAL_RESOLVED, provider_event="control_response")
        elif etype == "result":
            self._remember_result(event)
        elif etype in {"assistant", "user", "rate_limit_event", "stream_event"} or (
            etype == "system" and isinstance(subtype, str) and subtype.startswith("hook_")
        ):
            self.activity()
        else:
            self.activity()

    def finalize_result(self, sentinel_subtype):
        if not self.enabled:
            return
        candidate = self._result_candidate
        self._result_candidate = None
        if candidate is not None and sentinel_subtype in {"complete", "parked"}:
            self._emit(self._runtime.TURN_TERMINAL, **candidate)


def iter_user_texts(event):
    """Yield ("plain"|"tool_result", text) pairs from a user event's message content.

    Plain text is the channel task notifications arrive on; tool_result text is
    the channel task arms are confirmed on. Keeping them separate lets arms be
    read only from tool results and resolutions only from plain messages, so
    neither can be forged from the other channel.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield ("plain", content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                yield ("plain", text)
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                yield ("tool_result", inner)
            elif isinstance(inner, list):
                for part in inner:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str):
                            yield ("tool_result", text)


def strip_control_blocks(text):
    """Remove task-notification and queued-command control blocks from text."""
    if not isinstance(text, str):
        return text
    text = TASK_NOTIFICATION_RE.sub("", text)
    text = QUEUED_COMMAND_RE.sub("", text)
    return text


class BackgroundTaskTracker:
    """Track nonpersistent background-task lifecycle across a Claude event stream.

    ``observe(event)`` folds one stream event into the state. ``unresolved``
    lists tasks that were armed and never saw a terminal notification — at
    turn end, a non-empty list means the agent parked: it expects a wakeup no
    exited one-shot process can ever deliver.
    """

    def __init__(self):
        self._armed = {}  # task_id -> {"id", "source", "timeout_ms", "armed_at"}
        self._resolved = set()
        self.ever_armed = False
        self.saw_result = False
        # Tasks resolved AFTER the most recent result event: that result
        # provably never accounted for them. Cleared by each new result.
        self.stale_resolved = []

    def _arm(self, task_id, source, timeout_ms=None):
        if not task_id:
            return
        self.ever_armed = True
        if task_id in self._resolved:
            return
        entry = self._armed.setdefault(task_id, {"id": task_id, "source": source, "armed_at": time.time()})
        if timeout_ms is not None:
            entry["timeout_ms"] = timeout_ms

    def _resolve(self, task_id, status):
        if status not in TERMINAL_TASK_STATUSES:
            return
        entry = self._armed.pop(task_id, None)
        self._resolved.add(task_id)
        if entry is not None and self.saw_result:
            self.stale_resolved.append({"id": task_id, "source": entry.get("source")})

    def observe(self, event):
        if not isinstance(event, dict):
            return

        if event.get("type") == "result":
            self.saw_result = True
            # This result post-dates every resolution seen so far.
            self.stale_resolved = []
            return

        # Structured lifecycle events (observed live on Claude Code 2.1.219):
        # the CLI reports task completion as top-level system events — a
        # Monitor's completion arrives ONLY on this channel, with no user-level
        # XML notification. Top-level event types cannot be forged by tool
        # output (which is confined to text inside tool_result blocks), so
        # this is also the most trustworthy resolution source.
        if event.get("type") == "system":
            subtype = event.get("subtype")
            task_id = event.get("task_id")
            if subtype == "task_notification" and isinstance(task_id, str):
                self._resolve(task_id, event.get("status"))
            elif subtype == "task_updated" and isinstance(task_id, str):
                patch = event.get("patch")
                if isinstance(patch, dict):
                    self._resolve(task_id, patch.get("status"))
            return

        if event.get("type") != "user":
            return

        tur = event.get("tool_use_result")
        has_structured = isinstance(tur, dict)
        if has_structured:
            task_id = tur.get("taskId")
            # Genuine Monitor arms carry an EXPLICIT persistent key (observed
            # live: {"taskId", "timeoutMs", "persistent": false}). Requiring
            # the key excludes the todo-list tools (TaskUpdate/TaskCreate),
            # whose results also carry a taskId but never a persistent field —
            # without this, every todo status change armed a phantom monitor
            # that nothing could ever resolve (fell r2 finding 1).
            if isinstance(task_id, str) and "persistent" in tur and not tur.get("persistent"):
                self._arm(task_id, "monitor", timeout_ms=tur.get("timeoutMs"))
            bg_id = tur.get("backgroundTaskId")
            if isinstance(bg_id, str):
                self._arm(bg_id, "background_shell")
            if tur.get("scheduledFor") is not None:
                # A later ScheduleWakeup replaces the pending one; there is
                # only ever one armed wakeup.
                self._armed.pop(WAKEUP_TASK_ID, None)
                self._resolved.discard(WAKEUP_TASK_ID)
                self._arm(WAKEUP_TASK_ID, "scheduled_wakeup")

        for channel, text in iter_user_texts(event):
            if channel == "tool_result":
                # Text fallbacks in case a CLI version drops the structured
                # tool_use_result fields. Only consulted when the structured
                # record is absent — when present it is authoritative (it may
                # legitimately decline to arm, e.g. persistent tasks). Anchored
                # so quoted output can't arm.
                if has_structured:
                    continue
                match = MONITOR_STARTED_RE.match(text)
                if match:
                    self._arm(match.group(1), "monitor")
                match = BACKGROUND_CMD_RE.match(text)
                if match:
                    self._arm(match.group(1), "background_shell")
            else:
                # Task notifications arrive as plain user messages, never as
                # tool results — a tool result quoting notification XML (an
                # agent cat-ing an old transcript) must not resolve tasks.
                for notif in TASK_NOTIFICATION_RE.finditer(text):
                    body = notif.group(1)
                    tid = TASK_ID_RE.search(body)
                    status = TASK_STATUS_RE.search(body)
                    if tid and status:
                        self._resolve(tid.group(1), status.group(1))

    @property
    def unresolved(self):
        return [dict(entry) for entry in self._armed.values()]


def background_task_state(events):
    """Pure fold of ``BackgroundTaskTracker`` over a full event list.

    Returns ``{"unresolved": [...], "stale_resolved": [...]}``. ``unresolved``
    entries carry id/source (and timeout_ms when the arm published one);
    ``stale_resolved`` lists tasks whose resolution arrived only after the
    final result event, meaning that result never accounted for them. Used by
    spindle's finalizer on driver streams (the only channel where resolution
    events are reliably present) and shared with the live driver loop.
    """
    tracker = BackgroundTaskTracker()
    for event in events if isinstance(events, list) else []:
        tracker.observe(event)
    return {"unresolved": tracker.unresolved, "stale_resolved": tracker.stale_resolved}


def parse_ndjson_events(text):
    """Parse newline-delimited JSON into a list of dict events, skipping noise."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def find_sentinel(events):
    """Return the driver's terminal sentinel event from a list, or None."""
    for event in reversed(events if isinstance(events, list) else []):
        if isinstance(event, dict) and event.get("type") == SENTINEL_TYPE:
            return event
    return None


def ndjson_has_sentinel(text):
    """Whether raw driver stdout contains the terminal sentinel (scans tail first)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if SENTINEL_TYPE not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == SENTINEL_TYPE:
            return True
    return False


def build_sentinel(subtype, unresolved, claude_exit_code=None, reason=None, stale_resolved=None):
    sentinel = {
        "type": SENTINEL_TYPE,
        "subtype": subtype,
        "protocol": DRIVER_PROTOCOL,
        "unresolved_tasks": unresolved,
    }
    if claude_exit_code is not None:
        sentinel["claude_exit_code"] = claude_exit_code
    if reason:
        sentinel["reason"] = reason
    if stale_resolved:
        # Tasks whose completion arrived only AFTER the final result event:
        # the recorded answer provably never accounted for them.
        sentinel["stale_resolved_tasks"] = stale_resolved
    return sentinel


# --- driver process ---------------------------------------------------------


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class _ReaderFailure:
    def __init__(self, error):
        self.error = error


def _reader(stream, out_queue):
    try:
        for line in stream:
            out_queue.put(line)
    except OSError as exc:
        out_queue.put(_ReaderFailure(exc))
    finally:
        out_queue.put(None)


def _finish(proc, grace_seconds=10.0):
    """Close stdin so the CLI exits on end-of-input; escalate if it lingers."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        return proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            return proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def _park_deadline(unresolved, default_task_timeout, grace):
    """Latest moment any unresolved task could still plausibly notify."""
    deadlines = []
    for entry in unresolved:
        timeout_ms = entry.get("timeout_ms")
        budget = (timeout_ms / 1000.0) if isinstance(timeout_ms, (int, float)) else default_task_timeout
        deadlines.append(entry.get("armed_at", time.time()) + budget)
    return max(deadlines) + grace


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spindle persistent stream-json driver for Claude Code")
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Initial user message to send on stdin")
    prompt_group.add_argument(
        "--prompt-file",
        help=(
            "Read the initial user message from this file. Required for large "
            "prompts (rebuilt transcript continuations): a single argv element "
            "is capped at 128KiB on Linux (MAX_ARG_STRLEN)."
        ),
    )
    parser.add_argument(
        "--park-grace-seconds",
        type=float,
        default=float(os.environ.get("SPINDLE_DRIVER_PARK_GRACE_SECONDS", "45")),
        help="Extra linger past the longest task timeout before declaring the turn parked",
    )
    parser.add_argument(
        "--default-task-timeout-seconds",
        type=float,
        default=float(os.environ.get("SPINDLE_DRIVER_DEFAULT_TASK_TIMEOUT_SECONDS", "600")),
        help="Assumed lifetime for an unresolved task that published no timeoutMs",
    )
    parser.add_argument(
        "--complete-grace-seconds",
        type=float,
        default=float(os.environ.get("SPINDLE_DRIVER_COMPLETE_GRACE_SECONDS", "5")),
        help=(
            "How long an IDLE stream (result seen, nothing unresolved, no turn in "
            "progress) waits for a CLI-queued follow-up turn to start before the "
            "driver finishes. Notifications can land mid-turn without the model "
            "consuming them; the CLI then auto-runs a requery turn that an immediate "
            "stdin close would cut off. Never applied while a turn is active."
        ),
    )
    parser.add_argument(
        "--requery-start-window-seconds",
        type=float,
        default=float(os.environ.get("SPINDLE_DRIVER_REQUERY_WINDOW_SECONDS", "30")),
        help=(
            "Idle wait used instead of the plain grace when a post-result user "
            "message (a delivered notification) suggests a requery is coming — "
            "dispatch of that inference can exceed the plain grace."
        ),
    )
    parser.add_argument("claude_cmd", nargs=argparse.REMAINDER, help="-- followed by the claude invocation")
    args = parser.parse_args(argv)

    claude_cmd = args.claude_cmd
    if claude_cmd and claude_cmd[0] == "--":
        claude_cmd = claude_cmd[1:]
    if not claude_cmd:
        print("spindle_claude_driver: no claude command given after --", file=sys.stderr)
        return 2

    if args.prompt is not None:
        prompt = args.prompt
    else:
        try:
            with open(args.prompt_file, "r") as handle:
                prompt = handle.read()
        except OSError as exc:
            print(f"spindle_claude_driver: cannot read --prompt-file: {exc}", file=sys.stderr)
            return 2

    telemetry = ClaudeLifecycleTelemetry()
    proc = subprocess.Popen(
        claude_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # inherit: claude's stderr lands in the spool's stderr capture
        text=True,
        bufsize=1,
    )
    telemetry.transport_started()

    def _handle_term(signum, frame):
        proc.terminate()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_term)

    try:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
        proc.stdin.flush()
        telemetry.turn_started()
    except (BrokenPipeError, OSError):
        pass  # claude died at startup; the read loop below drains and reports

    lines = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()

    # State machine (fell r2 finding 2): the driver must never terminate an
    # ACTIVE turn — silence during inference or a long tool execution is
    # routine, so timers apply only while the stream is idle (result seen, no
    # turn in progress). turn_active is True from process start (our stdin
    # message begins the first turn) until a result, and again from any
    # init/assistant event (a requery turn starting). The external spool
    # timeout bounds active turns, exactly as it bounds the first one.
    tracker = BackgroundTaskTracker()
    turn_active = True
    user_after_result = False  # a delivered notification suggests a requery is coming
    grace_started = None  # entered the idle-clean phase at this time

    def _linger_terminal():
        """Terminal sentinel once the idle grace is exhausted.

        Resolutions that arrived after the final result mean that result
        provably never accounted for them — report parked (spindle marks
        headless_background_wait; respin rebuilds) instead of blessing a
        stale answer as complete: the original false-completion otherwise
        returns through this exit.
        """
        sentinel_subtype = "parked" if tracker.stale_resolved else "complete"
        telemetry.finalize_result(sentinel_subtype)
        if tracker.stale_resolved:
            _emit(
                build_sentinel(
                    "parked",
                    [],
                    reason="background tasks resolved after the final result; no requery turn ran",
                    stale_resolved=tracker.stale_resolved,
                )
            )
        else:
            _emit(build_sentinel("complete", []))
        exit_code = _finish(proc)
        telemetry.transport_exited(exit_code)
        return exit_code or 0

    while True:
        # Idle-deadline checks run every iteration — with or without events —
        # so a noisy stream cannot outrun them and a quiet one cannot dodge
        # them. Nothing here ever fires while a turn is active.
        if not turn_active and tracker.saw_result:
            now = time.time()
            unresolved = tracker.unresolved
            if unresolved:
                if now > _park_deadline(unresolved, args.default_task_timeout_seconds, args.park_grace_seconds):
                    telemetry.finalize_result("parked")
                    _emit(
                        build_sentinel(
                            "parked",
                            unresolved,
                            reason="no task notification arrived before the task timeout deadline",
                        )
                    )
                    exit_code = _finish(proc)
                    telemetry.transport_exited(exit_code)
                    return exit_code or 0
            elif grace_started is not None:
                idle_grace = args.requery_start_window_seconds if user_after_result else args.complete_grace_seconds
                if now - grace_started >= idle_grace:
                    return _linger_terminal()

        try:
            line = lines.get(timeout=0.25 if (not turn_active and tracker.saw_result) else 1.0)
        except queue.Empty:
            continue

        if isinstance(line, _ReaderFailure):
            telemetry.transport_lost(str(line.error))
            continue

        if line is None:
            # claude exited on its own; report what the stream established.
            exit_code = proc.wait()
            unresolved = tracker.unresolved
            if unresolved:
                telemetry.finalize_result("parked")
                _emit(
                    build_sentinel(
                        "parked",
                        unresolved,
                        claude_exit_code=exit_code,
                        reason="claude exited with unresolved background tasks",
                    )
                )
            elif tracker.saw_result and tracker.stale_resolved:
                telemetry.finalize_result("parked")
                _emit(
                    build_sentinel(
                        "parked",
                        [],
                        claude_exit_code=exit_code,
                        reason="claude exited after tasks resolved past the final result; no requery turn ran",
                        stale_resolved=tracker.stale_resolved,
                    )
                )
            elif tracker.saw_result:
                telemetry.finalize_result("complete")
                _emit(build_sentinel("complete", [], claude_exit_code=exit_code))
            else:
                telemetry.finalize_result("no_result")
                _emit(
                    build_sentinel(
                        "no_result",
                        [],
                        claude_exit_code=exit_code,
                        reason="claude exited without emitting a result event",
                    )
                )
            telemetry.transport_exited(exit_code)
            return exit_code

        sys.stdout.write(line if line.endswith("\n") else line + "\n")
        sys.stdout.flush()

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        tracker.observe(event)
        telemetry.observe(event)

        etype = event.get("type")
        if etype == "result":
            turn_active = False
            user_after_result = False
            if not tracker.unresolved and not tracker.ever_armed:
                # No background tasks ever existed, so no notification can be
                # queued — finish immediately (the common fast path).
                telemetry.finalize_result("complete")
                _emit(build_sentinel("complete", []))
                exit_code = _finish(proc)
                telemetry.transport_exited(exit_code)
                return exit_code or 0
        elif etype == "assistant" or (etype == "system" and event.get("subtype") == "init"):
            # Inference is in progress; all idle timers are off until its
            # result arrives.
            turn_active = True
        elif etype == "user" and not turn_active and tracker.saw_result:
            # A post-result user message (a replayed/delivered notification):
            # dispatch of the requery inference may take a while, so restart
            # the idle window at its longer setting.
            user_after_result = True
            grace_started = None

        # Recompute the idle-clean anchor: it exists exactly while idle with a
        # result and nothing unresolved, and starts when that phase is entered
        # (a resolution event can enter it long after the result). Noise never
        # resets an existing anchor.
        if turn_active or not tracker.saw_result or tracker.unresolved:
            grace_started = None
        elif grace_started is None:
            grace_started = time.time()


if __name__ == "__main__":
    sys.exit(main())
