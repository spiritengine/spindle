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
import os
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
                        f"Monitor started (task {task_id}, timeout {timeout_ms}ms). "
                        "You will be notified on each event."
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

    def test_failed_and_stopped_notifications_resolve(self):
        for status in ("failed", "stopped"):
            events = [monitor_arm_event("t1"), notification_event("t1", status)]
            assert driver.background_task_state(events)["unresolved"] == [], status

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


# --- finalize wiring (one-shot protocol) ------------------------------------


class TestFinalizeParkedDetection:
    def _finalize(self, tmp_path, events, spool_extra=None):
        spool_id = "parked-fin"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, running_claude_spool(spool_id, **(spool_extra or {})))
            _get_output_path(spool_id).write_text(json.dumps(events))
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
        out.write_text(
            ndjson([result_event("done"), driver.build_sentinel("complete", [])])
        )
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
    def _respin(self, tmp_path, spool, transcript_events=None, prompt="continue now"):
        captured = {}

        def fake_detached(sid, cmd, cwd, env=None):
            captured["cmd"] = list(cmd)
            return 4242

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool["id"], spool)
            if transcript_events is not None:
                tpath = _get_transcript_path(spool["id"])
                tpath.parent.mkdir(parents=True, exist_ok=True)
                tpath.write_text(json.dumps(transcript_events))
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        result = _respin_sync(spool["id"], prompt)
            new_spool = _read_spool(result) if not result.startswith(("Error", "Spool")) else None
        return result, captured.get("cmd"), new_spool

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
        result, cmd, new_spool = self._respin(tmp_path, self._parked_spool(), events)
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

    def test_legacy_complete_stub_selects_rebuild_not_resume(self, tmp_path):
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
        result, cmd, new_spool = self._respin(tmp_path, legacy, events)
        assert not result.startswith("Error"), result
        assert "--resume" not in cmd
        assert "oldtask" in cmd[cmd.index("-p") + 1]
        assert new_spool["parked_recovery_of"] == "legacy01"

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
        result, cmd, new_spool = self._respin(tmp_path, clean, events)
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
        result, cmd, _ = self._respin(tmp_path, bare, transcript_events=None)
        assert not result.startswith("Error"), result
        assert cmd[cmd.index("--resume") + 1] == "sess-bare"

    def test_parked_without_transcript_refuses_resume(self, tmp_path):
        result, cmd, _ = self._respin(tmp_path, self._parked_spool("noTrans1"), transcript_events=None)
        assert cmd is None  # nothing spawned
        assert "parked" in result
        assert "fresh spin()" in result

    def test_parked_recovery_disallows_background_tools(self, tmp_path):
        events = [monitor_arm_event(), result_event(PARKED_STUB)]
        _, cmd, _ = self._respin(tmp_path, self._parked_spool("guard01"), events)
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
        _, cmd, _ = self._respin(tmp_path, clean, transcript_events=None)
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
    emit({
        "type": "user",
        "message": {"role": "user", "content": [
            {"tool_use_id": "t", "type": "tool_result",
             "content": "Monitor started (task faketask, timeout {timeout_ms}ms)."}]},
        "tool_use_result": {"taskId": "faketask", "timeoutMs": {timeout_ms}, "persistent": False},
    })
    emit({"type": "result", "subtype": "success", "is_error": False,
          "stop_reason": "end_turn", "session_id": "fake-sess",
          "result": "Waiting for the background task."})

    MODE = {mode!r}
    if MODE == "notify":
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
