"""Claude stream-json to provider lifecycle telemetry contract.

These tests intentionally exercise the standalone driver module. Importing it
must not import ``spindle`` or run store maintenance; the durable runtime is
loaded lazily only when the owner store/id environment is present.
"""

import json
from unittest.mock import patch

import pytest

import spindle_claude_driver as driver
from spindle import lifecycle as lc
from tests.lifecycle_fixtures import make_spool_record, read_provider


def _telemetry(tmp_path, monkeypatch, spool_id="claude-telemetry"):
    store = tmp_path / "store"
    make_spool_record(store, spool_id)
    monkeypatch.setenv("SPINDLE_OWNER_STORE", str(store))
    monkeypatch.setenv("SPINDLE_OWNER_SPOOL_ID", spool_id)
    return driver.ClaudeLifecycleTelemetry(), store, spool_id


def test_conversation_summary_is_sorted_bounded_and_private():
    event = {
        "type": "system",
        "subtype": "init",
        "session_id": "session-1",
        "version": "2.1.241",
        "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1", "msg_lifecycle_v1", {"secret": 1}],
        "cwd": "/secret/project",
        "tools": [{"name": "Read", "input": "SECRET"}],
    }
    summary = driver.claude_conversation_summary(event)
    assert summary == (
        "version=2.1.241; capabilities=interrupt_receipt_v1,msg_lifecycle_v1"
    )
    assert len(summary.encode()) <= lc.SUMMARY_MAX
    assert "secret" not in summary.lower()
    assert "/secret" not in summary


def test_conversation_summary_accepts_legacy_claude_code_version_key():
    assert driver.claude_conversation_summary({"claude_code_version": "2.1.81"}) == "version=2.1.81"


@pytest.mark.parametrize(
        ("event", "expected"),
    [
        ({"type": "result", "subtype": "success", "is_error": False}, lc.TERMINAL_COMPLETED),
        (
            {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "refusal"},
            lc.TERMINAL_REFUSED,
        ),
        (
            {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "aborted_tools"},
            lc.TERMINAL_INTERRUPTED,
        ),
        (
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "terminal_reason": "aborted_streaming",
            },
            lc.TERMINAL_INTERRUPTED,
        ),
        ({"type": "result", "subtype": "error_during_execution", "is_error": True}, lc.TERMINAL_FAILED),
        ({"type": "result", "subtype": "max_turns", "is_error": False}, lc.TERMINAL_FAILED),
        ({"type": "result", "subtype": "max_budget_usd", "is_error": False}, lc.TERMINAL_FAILED),
        (
            {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "future_reason"},
            lc.TERMINAL_INDETERMINATE,
        ),
        ({"type": "result", "subtype": "mystery", "is_error": False}, lc.TERMINAL_INDETERMINATE),
        ({"type": "result", "subtype": "success", "is_error": "false"}, lc.TERMINAL_INDETERMINATE),
    ],
)
def test_terminal_normalization_fails_closed(event, expected):
    assert driver.normalize_claude_terminal_kind(event) == expected


def test_terminal_summary_allowlists_scalars_and_excludes_sensitive_data():
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "total_cost_usd": 0.125,
        "permission_denials": [
            {"tool_name": "Write", "tool_input": {"file_path": "/secret", "content": "LEAK"}}
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
            "server_tool_use": {"web_search_requests": 7},
            "bool_is_not_int": True,
            "unknown": 99,
        },
        "modelUsage": {"secret-model": {"costUSD": 42}},
        "result": "MODEL PROSE SECRET",
    }
    summary = driver.claude_terminal_summary(event)
    assert summary == (
        "terminal_reason=completed; permission_denials=1; total_cost_usd=0.125; "
        "input_tokens=10; output_tokens=20; cache_creation_input_tokens=30; cache_read_input_tokens=40"
    )
    assert len(summary.encode()) <= lc.SUMMARY_MAX
    serialized = summary.lower()
    for forbidden in ("model prose", "tool_input", "/secret", "server_tool_use", "secret-model", "unknown"):
        assert forbidden not in serialized


@pytest.mark.parametrize("cost", [True, -1, float("nan"), float("inf"), "0.1"])
def test_terminal_summary_rejects_invalid_cost(cost):
    summary = driver.claude_terminal_summary(
        {"terminal_reason": "completed", "permission_denials": [], "total_cost_usd": cost}
    )
    assert "total_cost_usd" not in summary


def test_init_tasks_approvals_terminal_and_exit_fold_through_apply_event(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.transport_started()
    telemetry.turn_started()
    telemetry.observe(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-1",
            "version": "2.1.241",
            "capabilities": ["msg_lifecycle_v1"],
        }
    )
    telemetry.observe(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "task-a",
            "task_type": "local_agent",
            "description": "SECRET DESCRIPTION",
        }
    )
    telemetry.observe(
        {
            "type": "control_request",
            "request_id": "approval-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_use_id": "tool-1",
                "tool_name": "Write",
                "input": {"file_path": "/secret", "content": "LEAK"},
            },
        }
    )
    telemetry.observe(
        {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": "approval-1", "response": {"behavior": "allow"}},
        }
    )
    telemetry.observe(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "task-a",
            "patch": {"status": "completed", "result": "SECRET TASK RESULT"},
        }
    )
    telemetry.observe(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "stop_reason": "end_turn",
            "terminal_reason": "completed",
            "permission_denials": [],
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "result": "SECRET FINAL PROSE",
        }
    )
    # Results are candidates until the existing driver sentinel decision says
    # which turn is the real terminal one.
    assert read_provider(store, spool_id)["protocol_terminal_kind"] is None
    telemetry.finalize_result("complete")
    telemetry.transport_exited(0)

    provider = read_provider(store, spool_id)
    assert provider["provider_ids"] == {"session_id": "session-1"}
    assert provider["protocol_terminal_kind"] == lc.TERMINAL_COMPLETED
    assert provider["connection_state"] == lc.CONNECTION_EXITED
    assert provider["active_work"] is None
    assert provider["conversation_summary"] == "version=2.1.241; capabilities=msg_lifecycle_v1"
    assert "terminal_reason=completed" in provider["terminal_summary"]
    persisted = json.dumps(provider)
    for forbidden in ("SECRET", "/secret", "tool_input", "file_path"):
        assert forbidden not in persisted


def test_requery_result_replaces_intermediate_terminal_candidate(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.observe(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": "task-a",
            "task_type": "local_agent",
        }
    )
    telemetry.observe(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "terminal_reason": "completed",
            "total_cost_usd": 0.01,
            "result": "parked stub",
        }
    )
    telemetry.observe(
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "task-a",
            "patch": {"status": "completed"},
        }
    )
    telemetry.observe(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "terminal_reason": "api_error",
            "total_cost_usd": 0.02,
            "errors": ["provider overloaded", {"secret": "LEAK"}],
            "result": "second result prose",
        }
    )
    telemetry.finalize_result("complete")

    provider = read_provider(store, spool_id)
    assert provider["protocol_terminal_kind"] == lc.TERMINAL_FAILED
    assert "terminal_reason=api_error" in provider["terminal_summary"]
    assert "total_cost_usd=0.02" in provider["terminal_summary"]
    assert "0.01" not in provider["terminal_summary"]


def test_overlapping_task_finish_does_not_clear_sibling(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    for task_id in ("a", "b"):
        telemetry.observe({"type": "system", "subtype": "task_started", "task_id": task_id})
    assert read_provider(store, spool_id)["active_work"] == "2 Claude tasks active"

    telemetry.observe(
        {"type": "system", "subtype": "task_updated", "task_id": "a", "patch": {"status": "completed"}}
    )
    assert read_provider(store, spool_id)["active_work"] == "1 Claude task active"

    telemetry.observe(
        {"type": "system", "subtype": "task_notification", "task_id": "b", "status": "killed"}
    )
    assert read_provider(store, spool_id)["active_work"] is None


def test_unmatched_or_malformed_control_response_does_not_resolve_approval(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.turn_started()
    telemetry.observe(
        {
            "type": "control_request",
            "request_id": "approval-1",
            "request": {"subtype": "can_use_tool", "tool_use_id": "tool-1"},
        }
    )
    telemetry.observe(
        {
            "type": "control_request",
            "request_id": "approval-2",
            "request": {"subtype": "can_use_tool", "tool_use_id": "tool-2"},
        }
    )
    assert read_provider(store, spool_id)["protocol_state"] == lc.PROTOCOL_WAITING
    telemetry.observe({"type": "control_response", "response": {"request_id": "other"}})
    telemetry.observe({"type": "control_response", "response": "malformed"})
    # The unmatched frames are only coalesced activity; neither resolves wait.
    assert read_provider(store, spool_id)["protocol_state"] == lc.PROTOCOL_WAITING
    telemetry.observe({"type": "control_response", "response": {"request_id": "approval-1"}})
    assert read_provider(store, spool_id)["protocol_state"] == lc.PROTOCOL_WAITING
    telemetry.observe({"type": "control_response", "response": {"request_id": "approval-2"}})
    assert read_provider(store, spool_id)["protocol_state"] == lc.PROTOCOL_ACTIVE


def test_task_updated_missing_unknown_or_non_dict_status_never_finishes(tmp_path, monkeypatch):
    telemetry, store, spool_id = _telemetry(tmp_path, monkeypatch)
    telemetry.observe({"type": "system", "subtype": "task_started", "task_id": "a"})
    for patch_value in (None, "completed", {}, {"status": "future"}):
        telemetry.observe(
            {"type": "system", "subtype": "task_updated", "task_id": "a", "patch": patch_value}
        )
        assert read_provider(store, spool_id)["active_work"] == "1 Claude task active"


def test_missing_owner_environment_disables_telemetry(monkeypatch):
    monkeypatch.delenv("SPINDLE_OWNER_STORE", raising=False)
    monkeypatch.delenv("SPINDLE_OWNER_SPOOL_ID", raising=False)
    with patch.object(driver, "_load_lifecycle_runtime") as load:
        telemetry = driver.ClaudeLifecycleTelemetry()
        telemetry.transport_started()
        telemetry.observe({"type": "result", "subtype": "success", "is_error": False})
        telemetry.finalize_result("complete")
    load.assert_not_called()
    assert telemetry.enabled is False


def test_apply_failure_is_nonfatal_and_disables_future_telemetry(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    monkeypatch.setenv("SPINDLE_OWNER_STORE", str(missing))
    monkeypatch.setenv("SPINDLE_OWNER_SPOOL_ID", "missing-spool")
    telemetry = driver.ClaudeLifecycleTelemetry()

    telemetry.transport_started()  # absent spool makes apply_event raise
    telemetry.observe({"type": "result", "subtype": "success", "is_error": False})
    telemetry.finalize_result("complete")

    assert telemetry.enabled is False
    assert "telemetry disabled" in capsys.readouterr().err
    assert not missing.exists()


def test_summary_is_valid_utf8_and_bounded_for_many_capabilities():
    summary = driver.claude_conversation_summary(
        {"version": "🔥" * 500, "capabilities": [f"cap-{i}-" + "🔥" * 100 for i in range(200)]}
    )
    assert len(summary.encode("utf-8")) <= lc.SUMMARY_MAX
    summary.encode("utf-8").decode("utf-8")
