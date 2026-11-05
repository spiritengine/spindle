import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import spindle
from spindle.reap import DEFAULT_RESPONSE_CHARS, _decode_capture, reap_store, render_response


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


def _outline_session(store, count):
    """Ordinary substantial runs matching the independent session-outline repro."""
    text = "word " * 100
    ids = [f"codex-{i:08x}" for i in range(count)]
    for index, spool_id in enumerate(ids):
        _record(
            store, spool_id, session_id="shared", prompt=text, result=text, created_at=f"2026-11-05T12:{index:02d}:00"
        )
        events = [{"type": "thread.started", "thread_id": "shared"}]
        for i in range(5):
            events.append({"type": "item.completed", "item": {"id": f"m{i}", "type": "agent_message", "text": text}})
            events.extend(_codex_call(f"c{i}", text, text))
        (store / f"{spool_id}.stdout").write_text(_jsonl(*events))
    return ids


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


def test_claude_user_entries_preserve_replay_and_synthetic_provenance(tmp_path):
    _record(tmp_path, "claude-flags", harness="claude-code", prompt="same text")
    events = [
        {"type": "user", "message": {"role": "user", "content": "same text"}, **flags}
        for flags in [{"isReplay": True}, {"isSynthetic": True, "isReplay": False}, {}]
    ]
    (tmp_path / "claude-flags.stdout").write_text(_jsonl(*events))
    entries = reap_store(tmp_path, "claude-flags", filters="user")["entries"]
    assert [entry["content"] for entry in entries] == ["same text"] * 4
    assert [entry.get("details", {}) for entry in entries] == [
        {"source": "spool record prompt"},
        {"isReplay": True},
        {"isReplay": False, "isSynthetic": True},
        {},
    ]
    assert len({entry["source_address"] for entry in entries}) == 4


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


@pytest.mark.parametrize("framing", ["json", "array", "ndjson"])
@pytest.mark.parametrize("terminal", [False, True])
def test_unfinished_utf8_is_not_invalid_until_terminal(framing, terminal):
    event = json.dumps({"text": "日本"}, ensure_ascii=False).encode("utf-8")
    cut = event[: event.index("本".encode("utf-8")) + 1]
    prefix = {"json": b"", "array": b'[{"text":"first"}', "ndjson": b'{"text":"first"}\r\n'}[framing]
    tail = (b"," if framing == "array" else b"") + cut
    capture = _decode_capture(prefix + tail, terminal=terminal)
    assert ("invalid_utf8" in [issue.code for issue in capture.issues]) is terminal
    if not terminal:
        assert [event.value for event in capture.events] == ([] if framing == "json" else [{"text": "first"}])
        assert capture.parsed_prefix_bytes == len(prefix)
        assert capture.ignored_trailing_bytes == len(tail)
        assert any(issue.code.startswith("partial_") for issue in capture.issues)
        completed = prefix + (b"," if framing == "array" else b"") + event + (b"]" if framing == "array" else b"")
        after = _decode_capture(completed, terminal=False)
        assert after.events[-1].value == {"text": "日本"}
        assert after.issues == []
        assert [event.index for event in after.events[: len(capture.events)]] == [
            event.index for event in capture.events
        ]


@pytest.mark.parametrize("prefix_text", ["π\ufffd".encode("utf-8"), b"\xff\xe2\x82"])
@pytest.mark.parametrize("tail", [b',{"t":"\xff\xff\xff', b',{"t":"\xe6'])
def test_array_byte_accounting_uses_original_bytes_after_repairs(prefix_text, tail):
    prefix = b'  [{"text":"' + prefix_text + b'"}'
    capture = _decode_capture(prefix + tail, terminal=False)
    assert len(capture.events) == 1
    assert capture.events[0].value["text"] == prefix_text.decode("utf-8", errors="replace")
    assert capture.parsed_prefix_bytes == len(prefix)
    assert capture.ignored_trailing_bytes == len(tail)
    assert capture.observed_bytes == len(prefix + tail)
    assert ("invalid_utf8" in [issue.code for issue in capture.issues]) is (b"\xff" in prefix + tail)


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


@pytest.mark.parametrize("full", [False, True])
def test_large_collection_continues_after_stable_address(tmp_path, full):
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
    first = reap_store(tmp_path, spool_id, addresses=span, filters="agent,messages", full=full)
    count = len(first["entries"])
    assert 0 < count <= 200
    if full:
        assert count == 200
        assert "character" not in first["entry_truncation"]["reason"]
    else:
        assert len(render_response(first)) <= DEFAULT_RESPONSE_CHARS
    assert first["selection"]["matched_entries"] == 205
    assert first["entries"][0]["content"] == "1"
    after = first["entry_truncation"]["retrieve"]["after"]
    second = reap_store(tmp_path, spool_id, addresses=span, filters="agent,messages", after=after, full=full)
    assert [entry["content"] for entry in second["entries"]] == [str(i) for i in range(count + 1, 206)]
    assert second["selection"]["entries_before_page"] == count
    assert second["selection"]["omitted_entries"] == 0
    for excluded in [f"run:{spool_id}/events/0/parts/0", f"run:{spool_id}/events/3/parts/0"]:
        invalid = reap_store(tmp_path, spool_id, addresses=span, filters="agent", after=excluded)
        assert invalid["error"]["code"] == "invalid_after"


@pytest.mark.parametrize("selection", ["filter", "collection", "batch-range", "raw"])
def test_aggregate_budget_pages_all_selected_entries_in_source_order(tmp_path, selection):
    spool_id = "budget"
    _record(tmp_path, spool_id)
    events = [event for i in range(12) for event in _codex_call(f"c{i}", "cat log", 'π\n"\\' * 7500)]
    (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*events))
    first, last = "run:budget/events/1/parts/0", "run:budget/events/23/parts/0"
    options = {
        "filter": {"filters": "tool-results"},
        "collection": {"addresses": "run:budget/tool-results"},
        "batch-range": {"addresses": [f"{first}..{last}", last], "filters": "tool-results"},
        "raw": {"addresses": "run:budget/raw"},
    }[selection]
    complete = reap_store(tmp_path, spool_id, **options, full=True)
    assert len(render_response(complete)) > DEFAULT_RESPONSE_CHARS
    expected = {entry["address"]: entry["content"] for entry in complete["entries"]}
    seen = []
    after = None
    pages = 0
    while True:
        page = reap_store(tmp_path, spool_id, **options, after=after)
        assert page["ok"] is True
        assert len(render_response(page)) <= DEFAULT_RESPONSE_CHARS
        assert page["entries"]  # Every continuation must make progress.
        assert page["selection"]["entries_before_page"] == len(seen)
        assert page["selection"]["deduplicated_overlap"] == (1 if selection == "batch-range" else 0)
        for entry in page["entries"]:
            address = entry["address"]
            seen.append(address)
            content = entry["content"]
            cursor = entry.get("truncation", {}).get("retrieve")
            while cursor:
                chunk = reap_store(
                    tmp_path, spool_id, addresses=address, offset=cursor["offset"], limit=cursor["limit"]
                )
                assert len(render_response(chunk)) <= DEFAULT_RESPONSE_CHARS
                part = chunk["entries"][0]
                assert part["content"]
                content += part["content"]
                cursor = part.get("truncation", {}).get("retrieve")
            assert content == expected[address]
        pages += 1
        if "entry_truncation" not in page:
            break
        after = page["entry_truncation"]["retrieve"]["after"]
        assert after == seen[-1]
    assert pages > 1
    assert seen == list(expected)  # Same identities, order and deduplication as full.


def test_single_entry_budget_counts_escaping_and_advances_content_cursor(tmp_path):
    payload = "\x00\ud83dπ" * 10_000
    _record(tmp_path, "escaped-budget", result=payload)
    address = "run:escaped-budget/final"
    offset = 0
    pieces = []
    while offset < len(payload):
        page = reap_store(tmp_path, "escaped-budget", addresses=address, offset=offset, limit=100_000)
        wire = render_response(page)
        assert len(wire) <= DEFAULT_RESPONSE_CHARS
        assert json.loads(wire) == page
        assert wire.encode("utf-8")
        entry = page["entries"][0]
        assert entry["content"]
        pieces.append(entry["content"])
        offset += len(entry["content"])
        assert entry["truncation"]["shown_chars"][1] == offset
        cursor = entry["truncation"]["retrieve"]
        assert cursor is None or cursor["offset"] == offset
    assert "".join(pieces) == payload
    full = reap_store(tmp_path, "escaped-budget", addresses=address, full=True)
    assert len(render_response(full)) > DEFAULT_RESPONSE_CHARS
    assert full["entries"][0]["content"] == payload
    assert "truncation" not in full["entries"][0]


def test_raw_small_limits_still_budget_metadata_and_keep_every_event(tmp_path):
    _record(tmp_path, "raw-budget")
    events = [{"role": "assistant", "content": "x" * 250} for _ in range(180)]
    (tmp_path / "raw-budget.stdout").write_text(_jsonl(*events))
    seen = []
    after = None
    pages = 0
    while True:
        page = reap_store(tmp_path, "raw-budget", addresses="run:raw-budget/raw", limit=200, after=after)
        assert page["ok"] is True
        assert len(render_response(page)) <= DEFAULT_RESPONSE_CHARS
        assert page["entries"]
        seen.extend(entry["address"] for entry in page["entries"])
        assert all(len(entry["content"]) == 200 for entry in page["entries"])
        pages += 1
        if "entry_truncation" not in page:
            break
        after = page["entry_truncation"]["retrieve"]["after"]
    assert pages > 1
    assert seen == [f"run:raw-budget/events/{i}" for i in range(180)]


def test_session_budget_continuation_keeps_saved_run_scope(tmp_path):
    for index in range(3):
        _record(tmp_path, f"page-{index}", session_id="shared", prompt=str(index) * 19_000)
    first = reap_store(tmp_path, "page-0", session=True, filters="user")
    assert len(render_response(first)) <= DEFAULT_RESPONSE_CHARS
    assert len(first["entries"]) == 2
    after = first["entry_truncation"]["retrieve"]["after"]
    second = reap_store(tmp_path, "page-0", session=True, filters="user", after=after)
    assert first["scope"] == second["scope"]
    assert len(render_response(second)) <= DEFAULT_RESPONSE_CHARS
    assert [entry["address"] for entry in first["entries"] + second["entries"]] == [
        f"run:page-{i}/prompt" for i in range(3)
    ]
    assert second["selection"]["omitted_entries"] == 0
    assert "entry_truncation" not in second


def test_metadata_that_cannot_fit_returns_error_not_empty_continuation(tmp_path):
    _record(tmp_path, "huge-metadata")
    events = _codex_call("c" * 60_000, "pwd", "output")
    (tmp_path / "huge-metadata.stdout").write_text(_jsonl(*events))
    for options in [{}, {"filters": "tool-results"}]:
        result = reap_store(tmp_path, "huge-metadata", **options)
        assert result["ok"] is False
        if options:
            assert result["error"]["code"] == "response_metadata_exceeds_budget"
            assert "full" in result["error"]["message"]
        else:
            assert result["error"]["code"] == "outline_run_exceeds_budget"
            assert result["error"]["run_address"] == "run:huge-metadata"
            assert "full" not in result["error"]["message"]
        assert "entry_truncation" not in result
        assert len(render_response(result)) <= DEFAULT_RESPONSE_CHARS
    full = reap_store(tmp_path, "huge-metadata", filters="tool-results", full=True)
    assert json.loads(full["entries"][0]["content"])["aggregated_output"] == "output"


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


@pytest.mark.parametrize("count", [7, 24])
def test_session_outline_pages_every_run_once_with_honest_scope(tmp_path, count):
    ids = _outline_session(tmp_path, count)
    _record(tmp_path, "foreign", session_id="different")
    expected = [f"run:{spool_id}" for spool_id in ids]
    seen = []
    after = None
    pages = 0
    while True:
        page = reap_store(tmp_path, ids[0], session=True, after=after)
        assert page["ok"] is True
        wire = render_response(page)
        assert len(wire) <= DEFAULT_RESPONSE_CHARS
        assert json.loads(wire) == page
        assert page["mode"] == "outline"
        addresses = [run["address"] for run in page["runs"]]
        assert addresses
        assert addresses == expected[len(seen) : len(seen) + len(addresses)]
        scope = page["scope"]
        assert scope["run_addresses"] == addresses
        assert scope["matched_runs"] == count
        assert scope["returned_runs"] == len(addresses)
        assert scope["runs_before_page"] == len(seen)
        assert scope["omitted_runs"] == count - len(seen) - len(addresses)
        assert f"exactly {count} readable saved" in scope["statement"]
        assert "only the runs returned on this outline page" in scope["statement"]
        assert "provider history not captured" in scope["statement"]
        seen.extend(addresses)
        pages += 1
        if "run_truncation" not in page:
            assert scope["omitted_runs"] == 0
            break
        assert scope["omitted_runs"] > 0
        after = page["run_truncation"]["retrieve"]["after"]
        assert after == page["run_truncation"]["last_returned_address"] == seen[-1]
    assert pages > 1
    assert seen == expected
    end = reap_store(tmp_path, "shared", session=True, after=seen[-1])
    assert end["runs"] == end["scope"]["run_addresses"] == []
    assert end["scope"]["runs_before_page"] == count
    assert end["scope"]["returned_runs"] == end["scope"]["omitted_runs"] == 0
    assert "run_truncation" not in end


def test_long_outline_does_not_repeat_all_run_addresses_in_page_metadata(tmp_path):
    ids = [f"run-{i:04d}-" + "x" * 220 for i in range(240)]
    for spool_id in ids:
        _record(tmp_path, spool_id, session_id="shared", prompt="p", result="r")
    # This list alone would exceed the response budget before adding any outline.
    assert len(json.dumps([f"run:{spool_id}" for spool_id in ids])) > DEFAULT_RESPONSE_CHARS
    page = reap_store(tmp_path, "shared", session=True)
    assert page["ok"] is True
    assert len(render_response(page)) <= DEFAULT_RESPONSE_CHARS
    count = len(page["runs"])
    assert 0 < count < len(ids)
    assert page["scope"]["run_addresses"] == [f"run:{spool_id}" for spool_id in ids[:count]]
    assert page["scope"]["matched_runs"] == len(ids)
    assert page["scope"]["omitted_runs"] == len(ids) - count
    assert page["run_truncation"]["retrieve"]["after"] == f"run:{ids[count - 1]}"


@pytest.mark.parametrize(
    "after", ["run:foreign", "run:missing", "member", "run:member/", "run:member/final", "session:shared"]
)
def test_outline_after_requires_an_exact_scoped_run_address(tmp_path, after):
    _record(tmp_path, "member", session_id="shared")
    _record(tmp_path, "foreign", session_id="different")
    page = reap_store(tmp_path, "member", session=True, after=after)
    assert page["ok"] is False
    assert page["error"]["code"] == "invalid_after"
    assert "run:<spool-id>" in page["error"]["message"]


def test_single_run_outline_keeps_its_tree_and_can_reach_end(tmp_path):
    spool_id = _outline_session(tmp_path, 1)[0]
    single = reap_store(tmp_path, spool_id)
    session = reap_store(tmp_path, spool_id, session=True)
    assert single["runs"] == session["runs"]
    assert single["scope"]["run_addresses"] == [f"run:{spool_id}"]
    assert single["scope"]["matched_runs"] == single["scope"]["returned_runs"] == 1
    assert single["scope"]["runs_before_page"] == single["scope"]["omitted_runs"] == 0
    assert "run_truncation" not in single
    end = reap_store(tmp_path, spool_id, after=f"run:{spool_id}")
    assert end["ok"] is True and end["runs"] == []
    assert "run_truncation" not in end
    assert reap_store(tmp_path, spool_id, full=True)["error"]["code"] == "paging_requires_selection"


def test_oversized_run_outline_reports_run_and_allows_continuing_past_it(tmp_path):
    for spool_id in ["a", "b", "c"]:
        _record(tmp_path, spool_id, session_id="shared")
        (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(*_codex_call("id", "pwd", "output")))
    (tmp_path / "b.stdout").write_text(_jsonl(*_codex_call("id" * 30_000, "pwd", "output")))
    first = reap_store(tmp_path, "shared", session=True)
    assert first["scope"]["run_addresses"] == ["run:a"]
    assert first["scope"]["omitted_runs"] == 2
    error = reap_store(tmp_path, "shared", session=True, after=first["run_truncation"]["retrieve"]["after"])
    assert error["error"]["code"] == "outline_run_exceeds_budget"
    assert error["error"]["run_address"] == "run:b"
    assert "run:b/raw" in error["error"]["message"] and "full" not in error["error"]["message"]
    assert len(render_response(error)) <= DEFAULT_RESPONSE_CHARS
    raw = reap_store(tmp_path, "b", addresses="run:b/raw")
    assert raw["ok"] is True and raw["entries"]
    last = reap_store(tmp_path, "shared", session=True, after=error["error"]["run_address"])
    assert last["scope"]["run_addresses"] == ["run:c"]
    assert last["scope"]["runs_before_page"] == 2
    assert last["scope"]["omitted_runs"] == 0
    assert "run_truncation" not in last


def test_outline_budget_error_does_not_echo_oversized_session_metadata(tmp_path):
    session_id = "s" * 60_000
    _record(tmp_path, "one", session_id=session_id)
    result = reap_store(tmp_path, session_id, session=True)
    assert result["error"]["run_address"] == "run:one"
    assert len(render_response(result)) <= DEFAULT_RESPONSE_CHARS
    assert "full" not in result["error"]["message"]


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
        ({"after": "run:bad-select/final"}, "invalid_after"),
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


def test_public_routes_accept_outline_after_cursors(tmp_path, monkeypatch, capsys):
    ids = _outline_session(tmp_path, 7)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    tool = asyncio.run(spindle.mcp.get_tool("spool_reap"))
    first_wire = asyncio.run(tool.fn(ids[0], session=True))
    first = json.loads(first_wire)
    assert len(first_wire) <= DEFAULT_RESPONSE_CHARS
    after = first["run_truncation"]["retrieve"]["after"]
    second = json.loads(asyncio.run(tool.fn(ids[0], session=True, after=after)))
    assert first["scope"]["run_addresses"] + second["scope"]["run_addresses"] == [f"run:{spool_id}" for spool_id in ids]
    monkeypatch.setattr(sys, "argv", ["spindle", "reap", ids[0], "--session", "--after", after])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    wire = capsys.readouterr().out
    assert len(wire.rstrip("\n")) <= DEFAULT_RESPONSE_CHARS
    assert json.loads(wire) == second


@pytest.mark.parametrize("budget", [50_000, 4096])
def test_public_mcp_reap_uses_existing_budget_and_safe_encoding(tmp_path, monkeypatch, budget):
    _record(tmp_path, "mcp-budget", result="\ud83dπ" * 30_000)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    monkeypatch.setattr(spindle, "UNSPOOL_MAX_CHARS", budget)
    tool = asyncio.run(spindle.mcp.get_tool("spool_reap"))
    wire = asyncio.run(tool.fn("mcp-budget", addresses="run:mcp-budget/final"))
    assert len(wire) <= budget
    assert wire.encode("utf-8")
    entry = json.loads(wire)["entries"][0]
    assert entry["content"]
    assert entry["truncation"]["retrieve"]["offset"] > 0


def test_cli_subprocess_preserves_unicode_and_escapes_lone_surrogates(tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    payload = "\ud83dπ\udc00😀\ufffd"
    _record(store, "cli-unicode", result=payload)
    env = {**os.environ, "SPINDLE_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8:strict"}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "spindle", "reap", "cli-unicode", "run:cli-unicode/final"],
        env=env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    wire = result.stdout.decode("utf-8")
    assert "\\ud83d" in wire and "π" in wire and "😀" in wire
    assert json.loads(wire)["entries"][0]["content"] == payload


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
