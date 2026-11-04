import asyncio
import json
import sys
from pathlib import Path

import pytest

import spindle
from spindle.reap import reap_store


def _record(store: Path, spool_id: str, **overrides):
    record = {
        "id": spool_id,
        "status": "complete",
        "harness": "codex",
        "prompt": f"prompt for {spool_id}",
        "result": f"final for {spool_id}",
        "session_id": f"session-{spool_id}",
        "created_at": "2026-11-04T12:00:00",
    }
    record.update(overrides)
    (store / f"{spool_id}.json").write_text(json.dumps(record))
    return record


def _jsonl(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


def _codex_call(item_id, command, output):
    return (
        {"type": "item.started", "item": {"id": item_id, "type": "command_execution", "command": command}},
        {
            "type": "item.completed",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "status": "completed",
                "exit_code": 0,
            },
        },
    )


def test_claude_outline_normalizes_roles_and_links_tool_results(tmp_path):
    spool_id = "claude-one"
    _record(tmp_path, spool_id, harness="claude-code")
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will inspect it."},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "a.py"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "file body"}],
            },
        },
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]}},
        {"type": "result", "result": "done"},
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(json.dumps(events))

    outline = reap_store(tmp_path, spool_id)
    run = outline["runs"][0]
    counts = {item["address"].rsplit("/", 1)[-1]: item["count"] for item in run["collections"]}
    assert outline["ok"] is True
    assert counts == {"messages": 3, "tool-calls": 1, "tool-results": 1, "raw": 4}
    assert run["nodes"][0]["address"] == f"run:{spool_id}/prompt"

    users = reap_store(tmp_path, spool_id, filters="user")
    assert [entry["content"] for entry in users["entries"]] == [f"prompt for {spool_id}", "continue"]
    assert "file body" not in [entry["content"] for entry in users["entries"]]

    calls = reap_store(tmp_path, spool_id, filters="tool-calls")
    results = reap_store(tmp_path, spool_id, filters="tool-results")
    assert calls["entries"][0]["result_addresses"] == [results["entries"][0]["address"]]
    assert results["entries"][0]["call_address"] == calls["entries"][0]["address"]


def test_codex_filters_multiple_calls_and_exact_tool_name(tmp_path):
    spool_id = "codex-many"
    _record(tmp_path, spool_id)
    first = _codex_call("call-1", "pwd", "/work\n")
    second = _codex_call("call-2", "false", "failure\n")
    events = [
        {"type": "thread.started", "thread_id": "s"},
        *first,
        {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "checking"}},
        *second,
        {"type": "item.completed", "item": {"id": "msg-2", "type": "agent_message", "text": "finished"}},
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))

    calls = reap_store(tmp_path, spool_id, filters="tool-calls", tool_name="command_execution")
    assert [entry["provider_id"] for entry in calls["entries"]] == ["call-1", "call-2"]
    assert all(entry["result_addresses"] for entry in calls["entries"])
    assert calls["selection"]["available_tool_names"] == ["command_execution"]

    agents = reap_store(tmp_path, spool_id, filters="agent")
    assert [entry["content"] for entry in agents["entries"]] == ["checking", "finished"]


def test_addresses_stay_stable_when_live_capture_appends(tmp_path):
    spool_id = "codex-live"
    _record(tmp_path, spool_id, status="running", result=None)
    first_message = {
        "type": "item.completed",
        "item": {"id": "m1", "type": "agent_message", "text": "first"},
    }
    path = tmp_path / f"{spool_id}.stdout"
    path.write_text(_jsonl(first_message) + '{"type":"item.comp')
    before = reap_store(tmp_path, spool_id, filters="agent")
    assert before["entries"][0]["address"] == f"run:{spool_id}/events/0/parts/0"
    assert before["runs"][0]["capture"]["ignored_trailing_bytes"] > 0

    second_message = {
        "type": "item.completed",
        "item": {"id": "m2", "type": "agent_message", "text": "second"},
    }
    path.write_text(_jsonl(first_message, second_message))
    after = reap_store(tmp_path, spool_id, filters="agent")
    assert [entry["address"] for entry in after["entries"]] == [
        f"run:{spool_id}/events/0/parts/0",
        f"run:{spool_id}/events/1/parts/0",
    ]


def test_growing_claude_array_reads_only_complete_observed_prefix(tmp_path):
    spool_id = "claude-live"
    _record(tmp_path, spool_id, harness="claude-code", status="running", result=None)
    complete = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "complete"}]},
    }
    (tmp_path / f"{spool_id}.stdout").write_text("[" + json.dumps(complete) + ',{"type":"assistant"')
    result = reap_store(tmp_path, spool_id, filters="agent")
    assert [entry["content"] for entry in result["entries"]] == ["complete"]
    assert result["runs"][0]["capture"]["ignored_trailing_bytes"] > 0
    assert any(issue["code"] == "partial_json_array" for issue in result["runs"][0]["issues"])


def test_terminal_empty_stdout_falls_back_to_saved_transcript(tmp_path):
    spool_id = "transcript-fallback"
    _record(tmp_path, spool_id)
    (tmp_path / f"{spool_id}.stdout").write_text("")
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    event = {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "saved"}}
    (transcript_dir / f"{spool_id}.txt").write_text(_jsonl(event))
    result = reap_store(tmp_path, spool_id, filters="agent")
    assert result["entries"][0]["content"] == "saved"
    assert result["runs"][0]["capture"]["source"] == f"transcripts/{spool_id}.txt"


def test_full_and_chunked_entry_retrieval_have_explicit_truncation(tmp_path):
    spool_id = "large-result"
    payload = "π" * 25_000
    _record(tmp_path, spool_id, result=payload)
    address = f"run:{spool_id}/final"

    default = reap_store(tmp_path, spool_id, addresses=address)
    assert len(default["entries"][0]["content"]) == 20_000
    assert default["entries"][0]["truncation"]["total_chars"] == 25_000
    assert default["entries"][0]["truncation"]["retrieve"] == {
        "address": address,
        "offset": 20_000,
        "limit": 20_000,
    }

    chunk = reap_store(tmp_path, spool_id, addresses=address, offset=100, limit=50)
    assert len(chunk["entries"][0]["content"]) == 50
    assert chunk["entries"][0]["truncation"]["shown_chars"] == [100, 150]
    assert reap_store(tmp_path, spool_id, addresses=address, full=True)["entries"][0]["content"] == payload


def test_batch_range_and_filter_overlap_deduplicate_in_source_order(tmp_path):
    spool_id = "range-one"
    _record(tmp_path, spool_id)
    events = [
        {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "one"}},
        {"type": "item.completed", "item": {"id": "m2", "type": "agent_message", "text": "two"}},
        {"type": "item.completed", "item": {"id": "m3", "type": "agent_message", "text": "three"}},
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    first = f"run:{spool_id}/events/0/parts/0"
    last = f"run:{spool_id}/events/2/parts/0"
    result = reap_store(
        tmp_path,
        spool_id,
        addresses=[first, f"{first}..{last}"],
        filters="agent",
    )
    assert [entry["content"] for entry in result["entries"]] == ["one", "two", "three"]
    assert result["selection"]["deduplicated_overlap"] == 4


def test_large_collection_continues_after_stable_address(tmp_path):
    spool_id = "many-messages"
    _record(tmp_path, spool_id)
    events = [
        {
            "type": "item.completed",
            "item": {"id": f"m{index}", "type": "agent_message", "text": str(index)},
        }
        for index in range(205)
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    first = reap_store(tmp_path, spool_id, filters="agent")
    assert len(first["entries"]) == 200
    after = first["entry_truncation"]["retrieve"]["after"]
    second = reap_store(tmp_path, spool_id, filters="agent", after=after)
    assert [entry["content"] for entry in second["entries"]] == ["200", "201", "202", "203", "204"]
    assert second["selection"]["entries_before_page"] == 200


def test_session_view_inventories_only_exact_matching_saved_runs(tmp_path):
    _record(tmp_path, "run-a", session_id="shared", created_at="2026-11-04T12:00:00")
    _record(tmp_path, "run-b", session_id="shared", created_at="2026-11-04T12:01:00", prompt="continued")
    _record(tmp_path, "other", session_id="different")
    result = reap_store(tmp_path, "run-a", session=True)
    assert result["scope"]["view"] == "session"
    assert result["scope"]["run_addresses"] == ["run:run-a", "run:run-b"]
    assert "provider history not captured" in result["scope"]["statement"]
    assert len(result["runs"]) == 2


def test_missing_capture_is_visible_without_hiding_record_nodes(tmp_path):
    spool_id = "missing-output"
    _record(tmp_path, spool_id)
    outline = reap_store(tmp_path, spool_id)
    run = outline["runs"][0]
    assert run["capture"]["source"] is None
    assert {node["address"] for node in run["nodes"]} == {
        f"run:{spool_id}/prompt",
        f"run:{spool_id}/final",
    }
    assert any(issue["code"] == "missing_capture" for issue in run["issues"])


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"filters": "thoughts"}, "invalid_filter"),
        ({"addresses": "not-an-address"}, "invalid_address"),
        ({"offset": -1}, "invalid_offset"),
        ({"limit": 0}, "invalid_limit"),
        ({"full": True, "limit": 2}, "incompatible_paging"),
        ({"after": "run:bad-select/final"}, "after_requires_selection"),
        ({"offset": 1}, "paging_requires_selection"),
    ],
)
def test_invalid_selection_errors_are_actionable(tmp_path, kwargs, code):
    _record(tmp_path, "bad-select")
    result = reap_store(tmp_path, "bad-select", **kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert result["error"]["message"]


def test_malformed_record_and_capture_are_not_empty_successes(tmp_path):
    (tmp_path / "bad-record.json").write_text('{"id":')
    malformed_record = reap_store(tmp_path, "bad-record")
    assert malformed_record["ok"] is False
    assert malformed_record["runs"][0]["issues"][0]["code"] == "malformed_record"

    spool_id = "bad-capture"
    _record(tmp_path, spool_id, status="complete")
    (tmp_path / f"{spool_id}.stdout").write_text('{"type":"turn.started"}\nnot json\n')
    result = reap_store(tmp_path, spool_id, addresses=f"run:{spool_id}/raw")
    assert result["ok"] is True
    assert len(result["entries"]) == 2
    assert result["entries"][1]["details"]["parse_error"]
    assert any(issue["code"] == "malformed_capture_record" for issue in result["runs"][0]["issues"])

    only_bad = "only-bad-capture"
    _record(tmp_path, only_bad, status="complete")
    (tmp_path / f"{only_bad}.stdout").write_text("not json\n")
    fatal = reap_store(tmp_path, only_bad)
    assert fatal["ok"] is False
    assert fatal["runs"][0]["collections"][-1]["count"] == 1


@pytest.mark.parametrize("harness", ["gemini", "private-profile"])
def test_unsupported_conversation_formats_retain_raw_evidence(tmp_path, harness):
    spool_id = harness.replace("-", "")
    _record(tmp_path, spool_id, harness=harness)
    (tmp_path / f"{spool_id}.stdout").write_text(json.dumps({"response": "captured", "stats": {"turns": 1}}))
    outline = reap_store(tmp_path, spool_id)
    assert outline["runs"][0]["support"] == "raw-only"
    raw = reap_store(tmp_path, spool_id, addresses=f"run:{spool_id}/raw")
    assert raw["entries"][0]["kind"] == "raw_event"
    assert "captured" in raw["entries"][0]["content"]


def test_kimi_role_and_tool_shapes_are_normalized(tmp_path):
    spool_id = "kimi-one"
    _record(tmp_path, spool_id, harness="kimi")
    events = [
        {"role": "assistant", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "k1", "function": {"name": "shell", "arguments": {"cmd": "pwd"}}}],
        },
        {"role": "tool", "tool_call_id": "k1", "content": "/work"},
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    calls = reap_store(tmp_path, spool_id, filters="tool-calls", tool_name="shell")
    assert calls["entries"][0]["result_addresses"]
    assert reap_store(tmp_path, spool_id, filters="agent")["entries"][0]["content"] == "hello"


def test_public_mcp_route_uses_shared_reader(tmp_path, monkeypatch):
    spool_id = "mcp-one"
    _record(tmp_path, spool_id)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(
        spindle,
        "_run_store_maintenance",
        lambda *args, **kwargs: pytest.fail("reap must not run store maintenance"),
    )
    monkeypatch.setattr(
        spindle,
        "_check_and_finalize_spool",
        lambda *args, **kwargs: pytest.fail("reap must not finalize a spool"),
    )
    tool = asyncio.run(spindle.mcp.get_tool("spool_reap"))
    result = json.loads(asyncio.run(tool.fn(spool_id, filters="user")))
    assert result["ok"] is True
    assert result["entries"][0]["address"] == f"run:{spool_id}/prompt"


def test_public_cli_reap_outputs_json_and_status(tmp_path, monkeypatch, capsys):
    spool_id = "cli-one"
    _record(tmp_path, spool_id)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["spindle", "reap", spool_id, "--filter", "user"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "selection"
    assert result["entries"][0]["role"] == "user"
