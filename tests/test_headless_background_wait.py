"""Tests for the headless background-wait (parked turn) handling.

Covers the three-part fix from finding-20260724-2niy:
1. the persistent stream-json driver (spindle_claude_driver) and its sentinel,
2. the parked-turn detector wired into spool finalization,
3. clean respin recovery (no --resume) for parked and legacy-stub spools,
plus the --disallowedTools mitigation on every headless Claude launch.

Event fixtures mirror the real shapes captured from the 2026-07-24 incident
(spools 688e30c3/7de8960e, Claude Code 2.1.219).
"""

import json
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import spindle_claude_driver as driver
from spindle import (
    CLAUDE_PROTOCOL_STREAM_V1,
    _build_transcript_continuation_prompt,
    _check_and_finalize_spool,
    _claude_headless_cmd,
    _format_spool_failure,
    _get_exit_path,
    _get_output_path,
    _get_transcript_path,
    _handle_expired_session,
    _read_spool,
    _respin_sync,
    _sanitize_claude_transcript,
    _spool_has_complete_output,
    _write_spool,
)

DRIVER_SCRIPT = Path(driver.__file__).resolve()


# --- event fixtures ---------------------------------------------------------


def monitor_arm_event(task_id="bhbhqvqfz", timeout_ms=600000, persistent=False):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_arm",
                    "type": "tool_result",
                    "content": (
                        f"Monitor started (task {task_id}, timeout {timeout_ms}ms). You will be notified on each event."
                    ),
                }
            ],
        },
        "tool_use_result": {"taskId": task_id, "timeoutMs": timeout_ms, "persistent": persistent},
    }


def background_bash_event(task_id="bgtask01"):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_bg",
                    "type": "tool_result",
                    "content": f"Command running in background with ID: {task_id}",
                }
            ],
        },
        "tool_use_result": {"backgroundTaskId": task_id},
    }


def wakeup_arm_event(scheduled_for=1784861700000):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_wake",
                    "type": "tool_result",
                    "content": "Next wakeup scheduled for 22:55:00 (in 239s).",
                }
            ],
        },
        "tool_use_result": {"scheduledFor": scheduled_for, "clampedDelaySeconds": 300},
    }


def notification_event(task_id, status, summary="Task finished."):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>toolu_arm</tool-use-id>\n<status>{status}</status>\n"
                f"<summary>{summary}</summary>\n</task-notification>"
            ),
        },
    }


def system_task_notification_event(task_id, status="completed"):
    """Top-level lifecycle event — the ONLY channel a Monitor completion
    arrives on (observed live, Claude Code 2.1.219 smoke 2026-07-24)."""
    return {
        "type": "system",
        "subtype": "task_notification",
        "task_id": task_id,
        "tool_use_id": "toolu_arm",
        "status": status,
        "summary": 'Monitor "watch" stream ended',
    }


def system_task_updated_event(task_id, status="completed"):
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"status": status, "end_time": 1784920194947},
    }


def assistant_text_event(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def result_event(text, session_id="sess-parked-1"):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "session_id": session_id,
        "result": text,
    }


PARKED_STUB = "Waiting for the test suite to finish — will resume once the Monitor notification arrives."


def running_claude_spool(spool_id, **extra):
    spool = {
        "id": spool_id,
        "status": "running",
        "harness": "claude-code",
        "prompt": "review the change",
        "pid": 999999999,
        "created_at": datetime.now().isoformat(),
    }
    spool.update(extra)
    return spool


# --- detector unit tests ----------------------------------------------------


class TestBackgroundTaskDetector:
    def test_monitor_arm_without_notification_is_unresolved(self):
        state = driver.background_task_state([monitor_arm_event(), result_event(PARKED_STUB)])
        assert [t["id"] for t in state["unresolved"]] == ["bhbhqvqfz"]
        assert state["unresolved"][0]["source"] == "monitor"

    def test_background_bash_arm_is_unresolved(self):
        state = driver.background_task_state([background_bash_event("bg42"), result_event("stub")])
        assert [t["id"] for t in state["unresolved"]] == ["bg42"]
        assert state["unresolved"][0]["source"] == "background_shell"

    def test_completed_notification_resolves(self):
        events = [monitor_arm_event("t1"), notification_event("t1", "completed"), result_event("done")]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_failed_stopped_and_killed_notifications_resolve(self):
        """killed is a real delivered terminal status (KillShell / harness
        stop, observed live) — fell r4."""
        for status in ("failed", "stopped", "killed"):
            events = [monitor_arm_event("t1"), notification_event("t1", status)]
            assert driver.background_task_state(events)["unresolved"] == [], status

    def test_killed_system_event_resolves(self):
        events = [
            background_bash_event("bg1"),
            system_task_notification_event("bg1", "killed"),
            result_event("done"),
        ]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_nonterminal_status_does_not_resolve(self):
        events = [monitor_arm_event("t1"), notification_event("t1", "running")]
        assert [t["id"] for t in driver.background_task_state(events)["unresolved"]] == ["t1"]

    def test_persistent_task_is_not_tracked(self):
        events = [monitor_arm_event("t1", persistent=True), result_event("done")]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_scheduled_wakeup_is_unresolved(self):
        state = driver.background_task_state([wakeup_arm_event(), result_event(PARKED_STUB)])
        assert [t["source"] for t in state["unresolved"]] == ["scheduled_wakeup"]

    def test_assistant_prose_mentioning_monitor_does_not_arm(self):
        events = [
            assistant_text_event("I could use Monitor started (task fake1) style waiting here."),
            assistant_text_event(
                "<task-notification>\n<task-id>x</task-id>\n<status>completed</status>\n</task-notification>"
            ),
            result_event("done"),
        ]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_notification_inside_tool_result_does_not_resolve(self):
        """An agent cat-ing an old transcript emits notification XML as a tool
        result; that must not close a genuinely armed task."""
        forged = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_cat",
                        "type": "tool_result",
                        "content": (
                            "<task-notification>\n<task-id>t1</task-id>\n"
                            "<status>completed</status>\n</task-notification>"
                        ),
                    }
                ],
            },
        }
        events = [monitor_arm_event("t1"), forged, result_event("stub")]
        assert [t["id"] for t in driver.background_task_state(events)["unresolved"]] == ["t1"]

    def test_quoted_monitor_text_mid_output_does_not_arm(self):
        """The text fallback is anchored to content start, so command output
        that merely quotes the phrase cannot arm a phantom task."""
        quoted = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_grep",
                        "type": "tool_result",
                        "content": 'transcript line: "Monitor started (task ghost99, timeout 5ms)"',
                    }
                ],
            },
        }
        assert driver.background_task_state([quoted, result_event("done")])["unresolved"] == []

    def test_text_fallback_arms_without_structured_fields(self):
        bare = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_arm",
                        "type": "tool_result",
                        "content": "Monitor started (task fall1, timeout 600000ms).",
                    }
                ],
            },
        }
        state = driver.background_task_state([bare, result_event("stub")])
        assert [t["id"] for t in state["unresolved"]] == ["fall1"]

    def test_rearmed_wakeup_tracks_single_pending(self):
        events = [wakeup_arm_event(1000), wakeup_arm_event(2000), result_event("stub")]
        state = driver.background_task_state(events)
        assert [t["source"] for t in state["unresolved"]] == ["scheduled_wakeup"]

    def test_system_task_notification_resolves_monitor(self):
        """A Monitor's completion arrives only as a system event, never as
        user-level XML — the live smoke's false park without this."""
        events = [
            monitor_arm_event("b7yyr65dc", timeout_ms=30000),
            system_task_notification_event("b7yyr65dc", "completed"),
            result_event("NOTIFIED SMOKE_MARKER_DONE"),
        ]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_system_task_updated_resolves(self):
        events = [
            background_bash_event("bv8qp3uax"),
            system_task_updated_event("bv8qp3uax", "completed"),
            result_event("done"),
        ]
        assert driver.background_task_state(events)["unresolved"] == []

    def test_system_event_with_nonterminal_status_does_not_resolve(self):
        events = [
            monitor_arm_event("t1"),
            system_task_notification_event("t1", "running"),
            result_event("stub"),
        ]
        assert [t["id"] for t in driver.background_task_state(events)["unresolved"]] == ["t1"]

    def test_todo_tool_task_ids_do_not_arm(self):
        """Fell r2 finding 1: TaskUpdate/TaskCreate results carry a taskId but
        no persistent key — they must not arm phantom monitors."""
        todo = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "toolu_todo",
                        "type": "tool_result",
                        "content": "Updated task #1 status",
                    }
                ],
            },
            "tool_use_result": {
                "success": True,
                "taskId": "1",
                "updatedFields": ["status"],
                "statusChange": {"from": "pending", "to": "in_progress"},
            },
        }
        state = driver.background_task_state([todo, result_event("done")])
        assert state["unresolved"] == []
        assert state["stale_resolved"] == []

    def test_resolution_after_final_result_is_stale(self):
        """Ordering awareness: a resolution arriving after the last result
        means that result never accounted for it."""
        events = [
            monitor_arm_event("t7"),
            result_event(PARKED_STUB),
            system_task_notification_event("t7", "completed"),
        ]
        state = driver.background_task_state(events)
        assert state["unresolved"] == []
        assert [t["id"] for t in state["stale_resolved"]] == ["t7"]

    def test_new_result_clears_stale(self):
        events = [
            monitor_arm_event("t7"),
            result_event(PARKED_STUB),
            system_task_notification_event("t7", "completed"),
            result_event("Informed answer."),
        ]
        state = driver.background_task_state(events)
        assert state["unresolved"] == []
        assert state["stale_resolved"] == []


# --- finalize wiring (one-shot protocol) ------------------------------------


class TestFinalizeParkedDetection:
    """Backstop detection for DRIVER streams whose driver died sentinel-less.

    One-shot output is deliberately exempt (fell r2 finding 3): resolution
    events are structurally invisible there, so armed-without-resolution
    matches healthy backgrounded-command spools and genuine parks alike.
    """

    def _finalize(self, tmp_path, events, spool_extra=None):
        spool_id = "parked-fin"
        extra = {"claude_protocol": CLAUDE_PROTOCOL_STREAM_V1}
        extra.update(spool_extra or {})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, running_claude_spool(spool_id, **extra))
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            return _read_spool(spool_id)

    def test_pending_monitor_with_stub_result_is_error(self, tmp_path):
        spool = self._finalize(tmp_path, [monitor_arm_event(), result_event(PARKED_STUB)])
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "bhbhqvqfz", "source": "monitor"}]
        assert spool["result"] == PARKED_STUB
        assert "respin" in spool["error"]

    def test_pending_background_bash_is_error(self, tmp_path):
        spool = self._finalize(tmp_path, [background_bash_event("bg7"), result_event("stub")])
        assert spool["status"] == "error"
        assert spool["pending_background_tasks"] == [{"id": "bg7", "source": "background_shell"}]

    def test_error_shaped_result_without_sentinel_still_parks(self, tmp_path):
        """Fell r4: arm -> is_error result -> driver dies pre-sentinel. The
        backstop must record parked metadata on the kind-less error too, or
        respin --resumes the parked session."""
        error_result = dict(result_event("API error: connection reset"), is_error=True)
        events = [monitor_arm_event("t3"), error_result]
        spool = self._finalize(tmp_path, events)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "t3", "source": "monitor"}]
        assert "API error: connection reset" in spool["error"]

    def test_stale_resolution_without_sentinel_is_error(self, tmp_path):
        """SIGTERM-killed driver: resolution after the last result, no
        sentinel — the ordering-aware fold must not bless the stub."""
        events = [
            monitor_arm_event("t2"),
            result_event(PARKED_STUB),
            system_task_notification_event("t2", "completed"),
        ]
        spool = self._finalize(tmp_path, events)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "t2", "source": "monitor"}]

    def test_completed_notification_permits_completion(self, tmp_path):
        events = [
            monitor_arm_event("t1"),
            notification_event("t1", "completed"),
            assistant_text_event("All 12 tests passed."),
            result_event("All 12 tests passed."),
        ]
        spool = self._finalize(tmp_path, events)
        assert spool["status"] == "complete"
        assert spool["result"] == "All 12 tests passed."
        assert "pending_background_tasks" not in spool

    def test_genuine_report_with_unresolved_task_still_flagged_and_preserved(self, tmp_path):
        report = "FINDINGS: two issues in _foo(); details follow."
        spool = self._finalize(tmp_path, [monitor_arm_event("t9"), result_event(report)])
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["result"] == report

    def test_prose_mention_does_not_flag(self, tmp_path):
        events = [
            assistant_text_event("I considered a Monitor here but ran tests in the foreground."),
            result_event("Review complete: no findings."),
        ]
        spool = self._finalize(tmp_path, events)
        assert spool["status"] == "complete"

    def test_one_shot_stream_is_never_park_flagged(self, tmp_path):
        """Fell r2 finding 3: one-shot output must not be park-detected —
        armed-without-resolution there matches healthy spools (measured ~9%
        of the live store, incl. genuine CLEAN verdicts)."""
        spool_id = "oneshot-ok"
        events = [monitor_arm_event("t5"), result_event("Verdict: CLEAN")]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, running_claude_spool(spool_id))
            _get_output_path(spool_id).write_text(json.dumps(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "complete"
        assert spool["result"] == "Verdict: CLEAN"
        assert "pending_background_tasks" not in spool

    def test_format_spool_failure_explains_parked(self, tmp_path):
        spool = self._finalize(tmp_path, [monitor_arm_event(), result_event(PARKED_STUB)])
        rendered = _format_spool_failure("parked-fin", spool)
        assert "PARKED" in rendered
        assert "bhbhqvqfz" in rendered
        assert "respin" in rendered


# --- headless launch command builder ----------------------------------------


class TestHeadlessCmdBuilder:
    def test_one_shot_disallows_monitor_and_wakeup(self, monkeypatch):
        monkeypatch.delenv("SPINDLE_CLAUDE_STREAM_DRIVER", raising=False)
        cmd, protocol = _claude_headless_cmd("do work", ["--permission-mode", "auto"])
        assert protocol is None
        assert cmd[:5] == ["claude", "-p", "do work", "--output-format", "json"]
        assert cmd[cmd.index("--disallowedTools") + 1] == "Monitor,ScheduleWakeup"
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"

    def test_driver_mode_wraps_with_stream_flags(self, monkeypatch):
        monkeypatch.setenv("SPINDLE_CLAUDE_STREAM_DRIVER", "1")
        cmd, protocol = _claude_headless_cmd("do work", ["--model", "claude-sonnet-5"])
        assert protocol == CLAUDE_PROTOCOL_STREAM_V1
        assert cmd[0] == sys.executable
        assert cmd[1] == str(DRIVER_SCRIPT)
        assert cmd[cmd.index("--prompt") + 1] == "do work"
        claude_part = cmd[cmd.index("--") + 1 :]
        assert claude_part[0] == "claude"
        for flag in ("--input-format", "--output-format", "--replay-user-messages", "--verbose"):
            assert flag in claude_part
        # Monitor is deliverable under the driver; ScheduleWakeup stays blocked.
        assert claude_part[claude_part.index("--disallowedTools") + 1] == "ScheduleWakeup"
        assert claude_part[claude_part.index("--model") + 1] == "claude-sonnet-5"

    def test_spin_records_protocol_and_guard(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SPINDLE_CLAUDE_STREAM_DRIVER", raising=False)
        captured = {}

        def fake_detached(sid, cmd, cwd, env=None):
            captured["cmd"] = list(cmd)
            return 4242

        from spindle import _spin_sync

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        spool_id = _spin_sync(
                            prompt="quick task",
                            permission="careful",
                            shard=False,
                            system_prompt=None,
                            working_dir=str(tmp_path),
                            allowed_tools=None,
                            tags=None,
                            model=None,
                            timeout=None,
                            skeinless=True,
                            env=None,
                        )
            assert not spool_id.startswith("Error"), spool_id
            spool = _read_spool(spool_id)

        cmd = captured["cmd"]
        assert cmd[cmd.index("--disallowedTools") + 1] == "Monitor,ScheduleWakeup"
        assert spool["claude_protocol"] is None


# --- driver-protocol finalization -------------------------------------------


def ndjson(events):
    return "\n".join(json.dumps(ev) for ev in events) + "\n"


class TestStreamDriverFinalization:
    def test_intermediate_result_without_sentinel_is_not_complete(self, tmp_path):
        spool = running_claude_spool("drv1", claude_protocol=CLAUDE_PROTOCOL_STREAM_V1)
        out = tmp_path / "out.txt"
        out.write_text(ndjson([monitor_arm_event(), result_event(PARKED_STUB)]))
        assert _spool_has_complete_output(spool, out, tmp_path / "err.txt") is False

    def test_sentinel_marks_output_complete(self, tmp_path):
        spool = running_claude_spool("drv2", claude_protocol=CLAUDE_PROTOCOL_STREAM_V1)
        out = tmp_path / "out.txt"
        out.write_text(ndjson([result_event("done"), driver.build_sentinel("complete", [])]))
        assert _spool_has_complete_output(spool, out, tmp_path / "err.txt") is True

    def test_one_shot_result_still_complete_without_sentinel(self, tmp_path):
        spool = running_claude_spool("one1")
        out = tmp_path / "out.txt"
        out.write_text(json.dumps([result_event("done")]))
        assert _spool_has_complete_output(spool, out, tmp_path / "err.txt") is True

    def test_driver_parked_stream_finalizes_as_error(self, tmp_path):
        spool_id = "drv-parked"
        events = [
            monitor_arm_event("t3"),
            result_event(PARKED_STUB),
            driver.build_sentinel(
                "parked",
                [{"id": "t3", "source": "monitor"}],
                reason="no task notification arrived before the task timeout deadline",
            ),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["result"] == PARKED_STUB

    def test_driver_no_result_sentinel_finalizes_as_error(self, tmp_path):
        """Fell r1 finding 3: a no_result sentinel (claude died before any
        result) used to finalize as status=complete with raw NDJSON as the
        result."""
        spool_id = "drv-nores"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s"},
            driver.build_sentinel(
                "no_result",
                [],
                claude_exit_code=1,
                reason="claude exited without emitting a result event",
            ),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert "no result" in spool["error"].lower()
        assert "claude exit code 1" in spool["error"]

    def test_driver_stale_parked_sentinel_finalizes_as_error(self, tmp_path):
        """Fell r1 findings 2+3: a parked sentinel with an EMPTY unresolved
        list (tasks resolved after the final result) must still park the
        spool, sourcing pending ids from the stale list."""
        spool_id = "drv-stale"
        events = [
            monitor_arm_event("t8"),
            result_event(PARKED_STUB),
            system_task_notification_event("t8", "completed"),
            driver.build_sentinel(
                "parked",
                [],
                claude_exit_code=0,
                reason="claude exited after tasks resolved past the final result; no requery turn ran",
                stale_resolved=[{"id": "t8", "source": "monitor"}],
            ),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "t8", "source": "monitor"}]
        assert spool["result"] == PARKED_STUB

    def test_parked_sentinel_lands_on_error_shaped_result(self, tmp_path):
        """Fell r3 finding 1: arm -> is_error result -> parked sentinel must
        still record error_kind/pending so respin rebuilds instead of
        --resume replaying the stale notification."""
        spool_id = "drv-err-park"
        error_result = dict(result_event("API error: stream interrupted"), is_error=True)
        events = [
            monitor_arm_event("t6"),
            error_result,
            driver.build_sentinel(
                "parked",
                [{"id": "t6", "source": "monitor"}],
                reason="claude exited with unresolved background tasks",
            ),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "t6", "source": "monitor"}]
        assert "API error: stream interrupted" in spool["error"]

    def test_parked_sentinel_does_not_override_refusal_kind(self, tmp_path):
        """A safety-refusal classification outranks parked metadata — the
        actionable response is re-routing, and error_kind must say so."""
        spool_id = "drv-refusal"
        refusal_result = dict(result_event("I can't help with that."), stop_reason="refusal")
        events = [
            monitor_arm_event("t6"),
            refusal_result,
            driver.build_sentinel("parked", [{"id": "t6", "source": "monitor"}]),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "safety_refusal"

    def test_sentinel_less_crash_before_result_is_error(self, tmp_path):
        """Fell r3 finding 2: a parseable driver stream with neither a result
        nor a sentinel (driver SIGKILLed pre-result) must not finalize
        complete."""
        spool_id = "drv-crash"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s"},
            assistant_text_event("Starting work..."),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            _get_exit_path(spool_id).write_text("137\n")
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert "without a result or a verdict" in spool["error"]
        assert "137" in spool["error"]

    def test_sentinel_less_stream_with_result_and_no_tasks_stays_complete(self, tmp_path):
        """SIGTERM after a clean no-task result: the result exists and no
        task evidence contradicts it — complete."""
        spool_id = "drv-sigterm-ok"
        events = [
            assistant_text_event("Answer."),
            result_event("Answer."),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "complete"
        assert spool["result"] == "Answer."

    def test_driver_notified_stream_finalizes_as_complete(self, tmp_path):
        spool_id = "drv-ok"
        events = [
            monitor_arm_event("t4"),
            result_event(PARKED_STUB),
            notification_event("t4", "completed"),
            assistant_text_event("Suite green; review passes."),
            result_event("Suite green; review passes."),
            driver.build_sentinel("complete", []),
        ]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(ndjson(events))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "complete"
        assert spool["result"] == "Suite green; review passes."
        assert spool["session_id"] == "sess-parked-1"


# --- sanitized transcript rendering -----------------------------------------


class TestSanitizedTranscript:
    def test_strips_notifications_keeps_conversation(self):
        transcript = json.dumps(
            [
                {"type": "system", "subtype": "init", "session_id": "s"},
                assistant_text_event("Starting the review now."),
                monitor_arm_event("t1"),
                notification_event("t1", "stopped", summary="No completion record was found."),
                assistant_text_event("Compiling findings."),
                result_event(PARKED_STUB),
            ]
        )
        rendered = _sanitize_claude_transcript(transcript)
        assert "task-notification" not in rendered
        assert "Starting the review now." in rendered
        assert "Compiling findings." in rendered
        assert "Monitor started" in rendered  # tool output is kept

    def test_continuation_prompt_states_abandonment_and_new_message(self):
        transcript = json.dumps([assistant_text_event("Earlier work."), result_event("stub")])
        prompt = _build_transcript_continuation_prompt(
            transcript,
            "The suite finished: 2919 passed. Produce your report.",
            abandoned_tasks=[{"id": "bhbhqvqfz", "source": "monitor"}],
        )
        assert "task-notification" not in prompt
        assert "bhbhqvqfz" in prompt
        assert "abandoned" in prompt
        assert prompt.rstrip().endswith("Produce your report.")

    def test_unparseable_transcript_falls_back_to_stripped_raw(self):
        raw = "plain prior transcript <task-notification><task-id>x</task-id></task-notification> tail"
        rendered = _sanitize_claude_transcript(raw)
        assert "task-notification" not in rendered
        assert "plain prior transcript" in rendered
        assert "tail" in rendered


# --- respin routing ---------------------------------------------------------


class TestRespinParkedRecovery:
    def _respin(
        self,
        tmp_path,
        spool,
        transcript_events=None,
        prompt="continue now",
        rebuild=False,
        transcript_text=None,
    ):
        captured = {}

        def fake_detached(sid, cmd, cwd, env=None, **kwargs):
            captured["cmd"] = list(cmd)
            captured["stdin_path"] = kwargs.get("stdin_path")
            return 4242

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool["id"], spool)
            if transcript_events is not None or transcript_text is not None:
                tpath = _get_transcript_path(spool["id"])
                tpath.parent.mkdir(parents=True, exist_ok=True)
                tpath.write_text(transcript_text if transcript_text is not None else json.dumps(transcript_events))
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        result = _respin_sync(spool["id"], prompt, rebuild)
            new_spool = _read_spool(result) if not result.startswith(("Error", "Spool")) else None
        return result, captured.get("cmd"), new_spool, captured.get("stdin_path")

    def _parked_spool(self, spool_id="parked01"):
        return {
            "id": spool_id,
            "status": "error",
            "error": "parked",
            "error_kind": "headless_background_wait",
            "pending_background_tasks": [{"id": "bhbhqvqfz", "source": "monitor"}],
            "session_id": "sess-parked",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "model": "sonnet",
            "working_dir": "/tmp",
            "result": PARKED_STUB,
        }

    def test_parked_spool_rebuilds_without_resume(self, tmp_path):
        events = [assistant_text_event("Prior work."), monitor_arm_event(), result_event(PARKED_STUB)]
        result, cmd, new_spool, _ = self._respin(tmp_path, self._parked_spool(), events)
        assert not result.startswith("Error"), result
        assert "--resume" not in cmd
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "task-notification" not in prompt_arg
        assert "bhbhqvqfz" in prompt_arg  # abandonment note
        assert prompt_arg.rstrip().endswith("continue now")
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"
        # "sonnet" is its own alias in CLAUDE_MODEL_ALIASES; the point is that
        # the recorded model is re-injected at all (a fresh session has none).
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        assert new_spool["parked_recovery_of"] == "parked01"
        assert new_spool["session_id"] is None
        assert new_spool["abandoned_background_tasks"] == [{"id": "bhbhqvqfz", "source": "monitor"}]

    def test_legacy_complete_stub_resumes_by_default(self, tmp_path):
        """Fell r2 finding 3: no auto transcript scan — one-shot transcripts
        carry no sound structural park signal, so a legacy complete spool
        keeps --resume unless the caller explicitly asks for a rebuild."""
        legacy = {
            "id": "legacy01",
            "status": "complete",
            "session_id": "sess-legacy",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "working_dir": "/tmp",
            "result": PARKED_STUB,
        }
        events = [monitor_arm_event("oldtask"), result_event(PARKED_STUB)]
        result, cmd, new_spool, _ = self._respin(tmp_path, legacy, events)
        assert not result.startswith("Error"), result
        assert cmd[cmd.index("--resume") + 1] == "sess-legacy"
        assert "parked_recovery_of" not in new_spool

    def test_rebuild_flag_forces_transcript_rebuild(self, tmp_path):
        """respin(rebuild=True): the caller judged the stub parked — rebuild
        the sanitized fresh session, no --resume."""
        legacy = {
            "id": "legacy02",
            "status": "complete",
            "session_id": "sess-legacy2",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "working_dir": "/tmp",
            "result": PARKED_STUB,
        }
        events = [monitor_arm_event("oldtask"), result_event(PARKED_STUB)]
        result, cmd, new_spool, _ = self._respin(tmp_path, legacy, events, rebuild=True)
        assert not result.startswith("Error"), result
        assert "--resume" not in cmd
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "oldtask" in prompt_arg
        assert "task-notification" not in prompt_arg
        assert new_spool["parked_recovery_of"] == "legacy02"

    def test_ordinary_complete_spool_still_resumes(self, tmp_path):
        clean = {
            "id": "clean01",
            "status": "complete",
            "session_id": "sess-clean",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "working_dir": "/tmp",
            "result": "All done.",
        }
        events = [
            monitor_arm_event("t1"),
            notification_event("t1", "completed"),
            result_event("All done."),
        ]
        result, cmd, new_spool, _ = self._respin(tmp_path, clean, events)
        assert not result.startswith("Error"), result
        assert cmd[cmd.index("--resume") + 1] == "sess-clean"
        assert new_spool["session_id"] == "sess-clean"
        assert "parked_recovery_of" not in new_spool

    def test_complete_spool_without_transcript_still_resumes(self, tmp_path):
        bare = {
            "id": "bare01",
            "status": "complete",
            "session_id": "sess-bare",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "working_dir": "/tmp",
        }
        result, cmd, _, _ = self._respin(tmp_path, bare, transcript_events=None)
        assert not result.startswith("Error"), result
        assert cmd[cmd.index("--resume") + 1] == "sess-bare"

    def test_parked_without_transcript_refuses_resume(self, tmp_path):
        result, cmd, _, _ = self._respin(tmp_path, self._parked_spool("noTrans1"), transcript_events=None)
        assert cmd is None  # nothing spawned
        assert "parked" in result
        assert "fresh spin()" in result

    def test_parked_spool_without_session_still_rebuilds(self, tmp_path):
        """Fell r2 finding 5: a spool that parked before any result has no
        session_id; the rebuild needs only the transcript, so the session
        gate must not block it."""
        spool = self._parked_spool("nosess01")
        spool["session_id"] = None
        events = [assistant_text_event("Partial work before the crash."), monitor_arm_event()]
        result, cmd, new_spool, _ = self._respin(tmp_path, spool, events)
        assert not result.startswith(("Error", "Spool")), result
        assert "--resume" not in cmd
        assert new_spool["parked_recovery_of"] == "nosess01"

    def test_oversized_rebuild_prompt_is_file_delivered(self, tmp_path):
        """Fell r2 finding 4: a rebuilt prompt beyond the per-argv limit must
        go via prompt file + stdin, not argv (execve E2BIG)."""
        big_text = "x" * 200_000
        events = [
            {"type": "user", "message": {"role": "user", "content": big_text}},
            monitor_arm_event("bigtask"),
            result_event(PARKED_STUB),
        ]
        result, cmd, new_spool, stdin_path = self._respin(tmp_path, self._parked_spool("bigrb01"), events)
        assert not result.startswith(("Error", "Spool")), result
        assert "--resume" not in cmd
        for arg in cmd:
            assert len(arg.encode()) < 131072
        assert stdin_path is not None
        content = Path(stdin_path).read_text()
        assert big_text in content
        assert content.rstrip().endswith("continue now")

    def test_parked_recovery_omits_model_when_profile_unresolvable(self, tmp_path):
        """Fell r4: a parked alt-profile spool whose profile no longer
        resolves degrades to the default endpoint — the endpoint-specific
        recorded model must not be forced onto it."""
        spool = self._parked_spool("ghostprof")
        spool["profile"] = "no-such-profile"
        spool["model"] = "alt-endpoint-only-model"
        events = [monitor_arm_event(), result_event(PARKED_STUB)]
        result, cmd, _, _ = self._respin(tmp_path, spool, events)
        assert not result.startswith(("Error", "Spool")), result
        assert "--resume" not in cmd
        assert "--model" not in cmd

    def test_parked_recovery_disallows_background_tools(self, tmp_path):
        events = [monitor_arm_event(), result_event(PARKED_STUB)]
        _, cmd, _, _ = self._respin(tmp_path, self._parked_spool("guard01"), events)
        assert cmd[cmd.index("--disallowedTools") + 1] == "Monitor,ScheduleWakeup"

    def test_ordinary_resume_disallows_background_tools(self, tmp_path):
        clean = {
            "id": "clean02",
            "status": "complete",
            "session_id": "sess-clean2",
            "harness": "claude-code",
            "permission": "careful",
            "allowed_tools": None,
            "working_dir": "/tmp",
        }
        _, cmd, _, _ = self._respin(tmp_path, clean, transcript_events=None)
        assert cmd[cmd.index("--disallowedTools") + 1] == "Monitor,ScheduleWakeup"


# --- expired-session fallback shares the sanitized renderer ------------------


class TestExpiredSessionSanitized:
    def test_expired_fallback_prompt_is_sanitized_and_notes_abandoned_tasks(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "exp-orig-s",
                {
                    "id": "exp-orig-s",
                    "status": "complete",
                    "session_id": "sess-exp-s",
                    "harness": "claude-code",
                    "permission": "careful",
                    "allowed_tools": None,
                    "working_dir": str(tmp_path),
                    "created_at": datetime.now().isoformat(),
                },
            )
            tpath = _get_transcript_path("exp-orig-s")
            tpath.parent.mkdir(parents=True, exist_ok=True)
            tpath.write_text(
                json.dumps(
                    [
                        assistant_text_event("Earlier analysis."),
                        monitor_arm_event("deadtask"),
                        notification_event("deadtask", "stopped"),
                        result_event(PARKED_STUB),
                    ]
                )
            )
            failing = {
                "id": "exp-fail-s",
                "status": "running",
                "session_id": "sess-exp-s",
                "prompt": "Continue sess-exp-s: pick it back up",
                "working_dir": str(tmp_path),
                "pid": None,
            }
            captured = {}

            def fake_spawn(spool_id, cmd, cwd, env=None):
                captured["cmd"] = cmd
                return 1

            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                assert _handle_expired_session("exp-fail-s", failing) is True

        cmd = captured["cmd"]
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "task-notification" not in prompt_arg
        assert "Earlier analysis." in prompt_arg
        assert prompt_arg.rstrip().endswith("pick it back up")


# --- fake-Claude stream-json end-to-end --------------------------------------


FAKE_CLAUDE_TEMPLATE = textwrap.dedent(
    """
    import json, sys, threading, time

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    # Read the initial user message from stream-json stdin.
    first = sys.stdin.readline()
    msg = json.loads(first)
    assert msg["type"] == "user", msg

    emit({"type": "system", "subtype": "init", "session_id": "fake-sess"})
    MODE = {mode!r}

    emit({
        "type": "user",
        "message": {"role": "user", "content": [
            {"tool_use_id": "t", "type": "tool_result",
             "content": "Monitor started (task faketask, timeout {timeout_ms}ms)."}]},
        "tool_use_result": {"taskId": "faketask", "timeoutMs": {timeout_ms}, "persistent": False},
    })
    if MODE == "requery_preresult":
        # Run-2 ordering: notifications land mid-turn, BEFORE the model ends
        # its turn with a stale stub.
        emit({"type": "system", "subtype": "task_updated", "task_id": "faketask",
              "patch": {"status": "completed", "end_time": 1}})
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
    emit({"type": "result", "subtype": "success", "is_error": False,
          "stop_reason": "end_turn", "session_id": "fake-sess",
          "result": "Waiting for the background task."})

    if MODE == "requery_preresult":
        # The CLI-queued requery turn with the real answer.
        time.sleep(0.15)
        emit({"type": "system", "subtype": "init", "session_id": "fake-sess"})
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Requery answer."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Requery answer."})
        sys.stdin.read()
    elif MODE == "requery":
        # Run-1 ordering: the turn ends parked, notifications arrive after the
        # stub result, and the CLI auto-runs a requery turn with the answer.
        emit({"type": "system", "subtype": "task_updated", "task_id": "faketask",
              "patch": {"status": "completed", "end_time": 1}})
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
        time.sleep(0.15)
        emit({"type": "system", "subtype": "init", "session_id": "fake-sess"})
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Requery answer."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Requery answer."})
        sys.stdin.read()
    elif MODE == "notify_system":
        # Resolution via the system lifecycle channel only (the real Monitor
        # completion path observed live) — no user-level XML notification.
        time.sleep(0.3)
        emit({"type": "system", "subtype": "task_updated", "task_id": "faketask",
              "patch": {"status": "completed", "end_time": 1}})
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Final answer after wakeup."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Final answer after wakeup."})
        sys.stdin.read()
    elif MODE == "notify":
        time.sleep(0.3)
        emit({"type": "user", "message": {"role": "user", "content":
             "<task-notification>\\n<task-id>faketask</task-id>\\n<status>completed</status>\\n<summary>done</summary>\\n</task-notification>"}})
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Final answer after wakeup."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Final answer after wakeup."})
        # Stay alive (like a real CLI with open stdin) until stdin closes.
        sys.stdin.read()
    elif MODE == "slow_requery":
        # Fell r2 finding 2: the requery turn contains a silent gap far longer
        # than the idle grace (inference/tool time). The driver must wait for
        # its result instead of killing the turn.
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
        time.sleep(0.2)
        emit({"type": "system", "subtype": "init", "session_id": "fake-sess"})
        time.sleep(1.5)  # >> grace (0.4) and window (0.8): mid-turn silence
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Slow requery answer."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Slow requery answer."})
        sys.stdin.read()
    elif MODE == "noise_after_result":
        # Fell r1 finding 1: lifecycle noise after the task resolved and the
        # result arrived must not cancel the bounded path to a sentinel.
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
        emit({"type": "assistant", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Informed answer."}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "stop_reason": "end_turn", "session_id": "fake-sess",
              "result": "Informed answer."})
        time.sleep(0.1)
        emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
        sys.stdin.read()
    elif MODE == "resolve_exit":
        # Fell r1 finding 2 (EOF variant): the task resolves only AFTER the
        # stub result, then claude exits without a requery turn.
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
    elif MODE == "resolve_silent":
        # Fell r1 finding 2 (linger variant): resolution after the stub
        # result, then silence — no requery ever arrives.
        emit({"type": "system", "subtype": "task_notification", "task_id": "faketask",
              "status": "completed", "summary": "done"})
        sys.stdin.read()
    elif MODE == "silent":
        # Never notify; keep running until killed or stdin closes.
        sys.stdin.read()
    elif MODE == "exit":
        pass  # exit immediately with the task unresolved (one-shot behavior)
    """
)


def write_fake_claude(tmp_path, mode, timeout_ms=600000):
    script = tmp_path / f"fake_claude_{mode}.py"
    script.write_text(FAKE_CLAUDE_TEMPLATE.replace("{timeout_ms}", str(timeout_ms)).replace("{mode!r}", repr(mode)))
    return script


def run_driver(tmp_path, mode, timeout_ms=600000, extra_args=()):
    fake = write_fake_claude(tmp_path, mode, timeout_ms)
    cmd = [
        sys.executable,
        str(DRIVER_SCRIPT),
        "--prompt",
        "do the fake work",
        "--complete-grace-seconds",
        "0.4",
        "--requery-start-window-seconds",
        "0.8",
        *extra_args,
        "--",
        sys.executable,
        str(fake),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    events = driver.parse_ndjson_events(proc.stdout)
    return proc, events


class TestDriverEndToEnd:
    def test_driver_survives_intermediate_result_and_completes_on_notification(self, tmp_path):
        proc, events = run_driver(tmp_path, "notify")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "complete"
        assert sentinel["unresolved_tasks"] == []
        results = [ev for ev in events if ev.get("type") == "result"]
        assert len(results) == 2  # stub + real: the driver did not stop at the stub
        assert results[-1]["result"] == "Final answer after wakeup."

    def test_driver_completes_on_system_channel_notification(self, tmp_path):
        proc, events = run_driver(tmp_path, "notify_system")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "complete"
        results = [ev for ev in events if ev.get("type") == "result"]
        assert results[-1]["result"] == "Final answer after wakeup."

    def test_driver_waits_for_requery_after_postresult_notifications(self, tmp_path):
        """Run-1 ordering: stub result, then notifications, then a requery
        turn — the requery's answer must replace the stub."""
        proc, events = run_driver(tmp_path, "requery")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel["subtype"] == "complete"
        results = [ev for ev in events if ev.get("type") == "result"]
        assert results[-1]["result"] == "Requery answer."

    def test_driver_lingers_past_clean_result_for_queued_requery(self, tmp_path):
        """Run-2 ordering: notifications landed mid-turn (result arrives with
        nothing unresolved), but a queued requery follows — an immediate
        stdin close would cut it off and keep the stale stub."""
        proc, events = run_driver(tmp_path, "requery_preresult")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel["subtype"] == "complete"
        results = [ev for ev in events if ev.get("type") == "result"]
        assert len(results) == 2
        assert results[-1]["result"] == "Requery answer."

    def test_driver_bounded_exit_despite_noise_after_clean_result(self, tmp_path):
        """Fell r1 finding 1: a lifecycle event after the clean result used to
        clear the linger timer with nothing to restart it — unbounded hang."""
        proc, events = run_driver(tmp_path, "noise_after_result")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "complete"
        assert [ev for ev in events if ev.get("type") == "result"][-1]["result"] == "Informed answer."

    def test_driver_marks_stale_result_parked_on_exit(self, tmp_path):
        """Fell r1 finding 2 (EOF): resolution after the stub result with no
        requery used to finalize as complete, reviving the false completion."""
        proc, events = run_driver(tmp_path, "resolve_exit")
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "parked"
        assert sentinel["unresolved_tasks"] == []
        assert [t["id"] for t in sentinel["stale_resolved_tasks"]] == ["faketask"]

    def test_driver_marks_stale_result_parked_after_linger(self, tmp_path):
        """Fell r1 finding 2 (linger): same stale-result case when claude
        stays alive silently instead of exiting."""
        proc, events = run_driver(tmp_path, "resolve_silent")
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None, proc.stdout
        assert sentinel["subtype"] == "parked"
        assert [t["id"] for t in sentinel["stale_resolved_tasks"]] == ["faketask"]

    def test_driver_never_kills_an_active_requery_turn(self, tmp_path):
        """Fell r2 finding 2: silence INSIDE an active requery turn (inference
        or a long tool run) must not trigger the idle grace — the requery's
        answer must land, however slow the turn is."""
        proc, events = run_driver(tmp_path, "slow_requery")
        assert proc.returncode == 0, proc.stderr
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "complete"
        results = [ev for ev in events if ev.get("type") == "result"]
        assert results[-1]["result"] == "Slow requery answer."

    def test_driver_parks_after_task_deadline(self, tmp_path):
        proc, events = run_driver(
            tmp_path,
            "silent",
            timeout_ms=200,
            extra_args=("--park-grace-seconds", "0.3"),
        )
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None, proc.stdout
        assert sentinel["subtype"] == "parked"
        assert [t["id"] for t in sentinel["unresolved_tasks"]] == ["faketask"]

    def test_driver_reports_parked_when_claude_exits_early(self, tmp_path):
        proc, events = run_driver(tmp_path, "exit")
        sentinel = driver.find_sentinel(events)
        assert sentinel is not None
        assert sentinel["subtype"] == "parked"
        assert sentinel["claude_exit_code"] == 0

    def test_driver_stream_finalizes_complete_through_spindle(self, tmp_path):
        """Full path: driver output on disk -> spindle finalization -> complete."""
        proc, events = run_driver(tmp_path, "notify")
        spool_id = "e2e-ok"
        with patch("spindle.SPINDLE_DIR", tmp_path / "store"):
            (tmp_path / "store").mkdir(exist_ok=True)
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(proc.stdout)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "complete"
        assert spool["result"] == "Final answer after wakeup."
        assert spool["session_id"] == "fake-sess"

    def test_driver_parked_stream_finalizes_error_through_spindle(self, tmp_path):
        proc, events = run_driver(tmp_path, "exit")
        spool_id = "e2e-parked"
        with patch("spindle.SPINDLE_DIR", tmp_path / "store"):
            (tmp_path / "store").mkdir(exist_ok=True)
            _write_spool(
                spool_id,
                running_claude_spool(spool_id, claude_protocol=CLAUDE_PROTOCOL_STREAM_V1),
            )
            _get_output_path(spool_id).write_text(proc.stdout)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert spool["error_kind"] == "headless_background_wait"
        assert spool["pending_background_tasks"] == [{"id": "faketask", "source": "monitor"}]


# --- sentinel helpers --------------------------------------------------------


class TestSentinelHelpers:
    def test_ndjson_has_sentinel(self):
        text = ndjson([result_event("x"), driver.build_sentinel("complete", [])])
        assert driver.ndjson_has_sentinel(text) is True

    def test_ndjson_without_sentinel(self):
        assert driver.ndjson_has_sentinel(ndjson([result_event("x")])) is False

    def test_quoted_sentinel_text_does_not_count(self):
        line = json.dumps({"type": "assistant", "note": "mentions spindle_driver_terminal only"})
        assert driver.ndjson_has_sentinel(line) is False
