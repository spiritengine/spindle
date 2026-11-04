import asyncio
import json
import sys
from pathlib import Path

import pytest

import spindle
from spindle.reap import _decode_capture, reap_store


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
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


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


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_ndjson_unicode_framing_and_byte_positions_survive_append(tmp_path, newline):
    spool_id = "unicode-live"
    _record(tmp_path, spool_id, status="running", result=None)
    values = ["first", "middle\u0085with\u2028JSON\u2029separators and literal \ufffd", "last π"]
    events = [
        {"type": "item.completed", "item": {"id": f"m{i}", "type": "agent_message", "text": text}}
        for i, text in enumerate(values)
    ]
    lines = [json.dumps(event, ensure_ascii=False).encode("utf-8") for event in events]
    prefix = newline.join(lines[:2]) + newline
    suffix = lines[2][:-2]
    path = tmp_path / f"{spool_id}.stdout"
    path.write_bytes(prefix + suffix)
    before = reap_store(tmp_path, spool_id, filters="agent")
    assert [entry["content"] for entry in before["entries"]] == values[:2]
    assert before["runs"][0]["capture"]["parsed_prefix_bytes"] == len(prefix)
    assert before["runs"][0]["capture"]["ignored_trailing_bytes"] == len(suffix)
    assert [issue["code"] for issue in before["runs"][0]["issues"]] == ["partial_trailing_record"]

    # Complete the suffix without an LF yet; its JSON value is already readable.
    with path.open("ab") as stream:
        stream.write(lines[2][len(suffix) :])
    after = reap_store(tmp_path, spool_id, filters="agent")
    assert [entry["content"] for entry in after["entries"]] == values
    assert [entry["address"] for entry in after["entries"]] == [f"run:{spool_id}/events/{i}/parts/0" for i in range(3)]
    assert after["runs"][0]["capture"]["parsed_prefix_bytes"] == len(prefix + lines[2])
    assert after["runs"][0]["capture"]["ignored_trailing_bytes"] == 0
    assert after["runs"][0]["issues"] == []
    raw = reap_store(tmp_path, spool_id, addresses=f"run:{spool_id}/raw", full=True)
    assert [entry["content"] for entry in raw["entries"]] == [line.decode("utf-8") for line in lines]


@pytest.mark.parametrize("framing", ["json", "array", "ndjson"])
@pytest.mark.parametrize("encoded_text", [b"\xef\xbf\xbd", b"\xff", b"\xe2\x82"])
def test_utf8_warning_requires_a_decode_failure(framing, encoded_text):
    event = b'{"text":"' + encoded_text + b'"}'
    data = {"json": event, "array": b"[" + event + b"]", "ndjson": event + b"\n{}\n"}[framing]
    capture = _decode_capture(data, terminal=True)
    assert capture.events[0].value == {"text": "\ufffd"}
    assert [issue.code for issue in capture.issues] == ([] if encoded_text == b"\xef\xbf\xbd" else ["invalid_utf8"])
    assert capture.observed_bytes == capture.parsed_prefix_bytes == len(data)
    assert capture.ignored_trailing_bytes == 0


@pytest.mark.parametrize("suffix", [b'{"text":"\xff\r', b'{"text":"\xe2\x82'])
def test_ndjson_invalid_utf8_suffix_counts_original_bytes(suffix):
    prefix = b'{"text":"\xff"}\r\n\r\n'
    data = prefix + suffix
    capture = _decode_capture(data, terminal=False)
    assert [(event.index, event.value) for event in capture.events] == [(0, {"text": "\ufffd"})]
    assert capture.observed_bytes == len(data)
    assert capture.parsed_prefix_bytes == len(prefix)
    assert capture.ignored_trailing_bytes == len(suffix)
    assert [issue.code for issue in capture.issues] == ["invalid_utf8", "partial_trailing_record"]


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
    last = f"run:{spool_id}/events/1/parts/0"
    result = reap_store(
        tmp_path,
        spool_id,
        addresses=[first, f"{first}..{last}"],
        filters="agent",
    )
    assert [entry["content"] for entry in result["entries"]] == ["one", "two"]
    assert result["selection"]["deduplicated_overlap"] == 1


@pytest.mark.parametrize(
    ("filters", "tool_name", "parts"),
    [
        ("agent,tool-calls", "Read", [(1, 0), (1, 1)]),
        ("tool-calls,tool-results", "Read", [(1, 1), (2, 0)]),
        (None, "Read", [(1, 1)]),
        ("user", None, [(2, 1)]),
        ("user,agent,messages", None, [(1, 0), (2, 1)]),
        ("tool-calls", "missing-tool", []),
    ],
)
def test_mixed_filters_narrow_batch_selections(tmp_path, filters, tool_name, parts):
    spool_id = "mixed-filter"
    _record(tmp_path, spool_id, harness="claude-code")
    outside = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "outside range"}]},
    }
    events = [
        outside,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "inside range"},
                    {"type": "tool_use", "id": "r1", "name": "Read", "input": {"path": "one"}},
                    {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "pwd"}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "r1", "content": "file body"},
                    {"type": "text", "text": "follow up"},
                ],
            },
        },
        outside,
    ]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    first = f"run:{spool_id}/events/1/parts/0"
    last = f"run:{spool_id}/events/2/parts/1"
    # Both endpoints and the range overlap; final is selected but matches no filter.
    result = reap_store(
        tmp_path,
        spool_id,
        addresses=[last, f"run:{spool_id}/final", f"{first}..{last}", first],
        filters=filters,
        tool_name=tool_name,
    )
    assert result["ok"] is True
    assert [entry["address"] for entry in result["entries"]] == [
        f"run:{spool_id}/events/{event}/parts/{part}" for event, part in parts
    ]
    assert result["selection"]["deduplicated_overlap"] == 2


def test_large_collection_continues_after_stable_address(tmp_path):
    spool_id = "many-messages"
    _record(tmp_path, spool_id)
    events = []
    for index in range(210):
        events.append(
            {"type": "item.completed", "item": {"id": f"m{index}", "type": "agent_message", "text": str(index)}}
        )
        events.append(_codex_call(f"c{index}", "pwd", "")[0])
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    # The span has 205 messages interleaved with tool calls; filter before paging.
    span = f"run:{spool_id}/events/2/parts/0..run:{spool_id}/events/410/parts/0"
    first = reap_store(tmp_path, spool_id, addresses=span, filters="agent,messages")
    assert len(first["entries"]) == 200
    assert first["selection"]["matched_entries"] == 205
    assert first["entries"][0]["content"] == "1"
    after = first["entry_truncation"]["retrieve"]["after"]
    second = reap_store(tmp_path, spool_id, addresses=span, filters="agent,messages", after=after)
    assert [entry["content"] for entry in second["entries"]] == ["201", "202", "203", "204", "205"]
    assert second["selection"]["entries_before_page"] == 200
    assert second["selection"]["omitted_entries"] == 0
    for excluded in [f"run:{spool_id}/events/0/parts/0", f"run:{spool_id}/events/3/parts/0"]:
        invalid = reap_store(tmp_path, spool_id, addresses=span, filters="agent", after=excluded)
        assert invalid["error"]["code"] == "invalid_after"


def test_session_view_inventories_only_exact_matching_saved_runs(tmp_path):
    _record(tmp_path, "run-a", session_id="shared", created_at="2026-11-04T12:00:00")
    _record(tmp_path, "run-b", session_id="shared", created_at="2026-11-04T12:01:00", prompt="continued")
    _record(tmp_path, "other", session_id="different")
    result = reap_store(tmp_path, "run-a", session=True)
    assert result["scope"]["view"] == "session"
    assert result["scope"]["run_addresses"] == ["run:run-a", "run:run-b"]
    assert "provider history not captured" in result["scope"]["statement"]
    assert len(result["runs"]) == 2
    scoped = reap_store(tmp_path, "run-a", session=True, filters="user")
    assert [entry["address"] for entry in scoped["entries"]] == ["run:run-a/prompt", "run:run-b/prompt"]
    narrowed = reap_store(tmp_path, "run-a", session=True, addresses="run:run-a/messages", filters="user")
    assert [entry["address"] for entry in narrowed["entries"]] == ["run:run-a/prompt"]


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
    narrowed = json.loads(asyncio.run(tool.fn(spool_id, addresses=f"run:{spool_id}/final", filters="user")))
    assert narrowed["ok"] is True
    assert narrowed["entries"] == []


@pytest.mark.parametrize("node", ["prompt", "final"])
def test_public_cli_reap_outputs_json_and_status(tmp_path, monkeypatch, capsys, node):
    spool_id = "cli-one"
    _record(tmp_path, spool_id)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["spindle", "reap", spool_id, f"run:{spool_id}/{node}", "--filter", "user"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "selection"
    assert [entry["address"] for entry in result["entries"]] == ([f"run:{spool_id}/prompt"] if node == "prompt" else [])
