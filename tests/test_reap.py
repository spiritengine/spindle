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
    assert first["scope"]["matched_runs"] == second["scope"]["matched_runs"] == 3
    assert first["scope"]["diagnostics"] == second["scope"]["diagnostics"]
    assert first["scope"]["run_addresses"] == ["run:page-0", "run:page-1"]
    assert second["scope"]["run_addresses"] == ["run:page-2"]
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
        assert "only the evidence represented on this page" in scope["statement"]
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


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("valid_prefix", [False, True])
def test_malformed_summary_preserves_severity_line_and_byte_accounting(terminal, valid_prefix):
    prefix = b"\r\n" + (b"{}\r\n" if valid_prefix else b"")
    complete = prefix + "bad π\r\nalso bad\r\n".encode("utf-8")
    suffix = b'{"partial'
    capture = _decode_capture(complete + suffix, terminal=terminal)
    issue = next(issue for issue in capture.issues if issue.code == "malformed_capture_record")
    assert issue.details == {"count": 2 + terminal, "first_source_line": 2 + valid_prefix}
    assert issue.severity == ("error" if terminal and not valid_prefix else "warning")
    assert capture.observed_bytes == len(complete + suffix)
    assert capture.parsed_prefix_bytes == len(complete) + (len(suffix) if terminal else 0)
    assert capture.ignored_trailing_bytes == (0 if terminal else len(suffix))
    assert [event.index for event in capture.events] == list(range(len(capture.events)))
    assert sum(event.parse_error is not None for event in capture.events) == issue.details["count"]


def test_plain_text_capture_remains_readable_and_does_not_block_healthy_selection(tmp_path):
    _record(tmp_path, "bad", session_id="shared")
    _record(tmp_path, "good", session_id="shared")
    (tmp_path / "good.stdout").write_text(_jsonl({"type": "thread.started"}))
    lines = [f"plain output {i} π" for i in range(400)]
    (tmp_path / "bad.stdout").write_text("\n".join(lines) + "\n")
    outline = reap_store(tmp_path, "bad")
    assert "error" not in outline and outline["ok"] is False
    assert len(render_response(outline)) <= DEFAULT_RESPONSE_CHARS
    issue = outline["runs"][0]["issues"][0]
    assert issue["details"] == {"count": 400, "first_source_line": 1}
    assert issue["severity"] == "error"
    seen, after = [], None
    for _ in range(10):
        page = reap_store(tmp_path, "bad", addresses="run:bad/raw", after=after)
        assert "error" not in page and page["ok"] is False
        assert len(render_response(page)) <= DEFAULT_RESPONSE_CHARS
        assert page["runs"][0]["issues"] == [issue]
        assert page["entries"]
        for entry in page["entries"]:
            assert entry["address"] == f"run:bad/events/{len(seen)}"
            assert entry["content"] == lines[len(seen)]
            assert entry["details"]["parse_error"]
            seen.append(entry["address"])
        after = page.get("entry_truncation", {}).get("retrieve", {}).get("after")
        if after is None:
            break
    assert len(seen) == 400
    full = reap_store(tmp_path, "bad", addresses="run:bad/raw", full=True)
    assert len(full["entries"]) == 200 and full["runs"][0]["issues"] == [issue]

    healthy = reap_store(tmp_path, "shared", session=True, addresses="run:good/final")
    assert healthy["ok"] is True
    assert healthy["entries"][0]["content"] == "final for good"
    assert [run["address"] for run in healthy["runs"]] == healthy["scope"]["run_addresses"] == ["run:good"]
    assert healthy["scope"]["matched_runs"] == 2
    assert healthy["scope"]["diagnostics"]["error_runs"] == 1
    empty = reap_store(tmp_path, "shared", session=True, filters="agent")
    assert empty["ok"] is False and empty["entries"] == empty["runs"] == []
    assert empty["scope"]["diagnostics"]["error_runs"] == 1


def test_large_session_selection_metadata_tracks_only_page_owners(tmp_path):
    ids = [f"codex-{i:08x}" for i in range(110)]
    for i, spool_id in enumerate(ids):
        _record(tmp_path, spool_id, session_id="shared", prompt="p", result="r")
        event = {
            "type": "item.started",
            "item": {"id": f"c{i}", "type": "command_execution", "name": f"tool-{i}", "command": "pwd"},
        }
        (tmp_path / f"{spool_id}.stdout").write_text(_jsonl(event))
    single = reap_store(tmp_path, "shared", session=True, addresses=f"run:{ids[0]}/final")
    assert single["ok"] is True
    assert single["scope"]["matched_runs"] == 110 and single["scope"]["returned_runs"] == 1
    assert single["scope"]["run_addresses"] == [f"run:{ids[0]}"]
    assert single["selection"]["available_tool_names"] == []
    assert len(render_response(single)) < 4000
    seen, after = [], None
    for _ in range(10):
        page = reap_store(tmp_path, "shared", session=True, filters="user", after=after)
        assert page["ok"] is True
        assert len(render_response(page)) <= DEFAULT_RESPONSE_CHARS
        owners = [entry["address"].removesuffix("/prompt") for entry in page["entries"]]
        assert owners == [f"run:{spool_id}" for spool_id in ids[len(seen) : len(seen) + len(owners)]]
        assert page["scope"]["run_addresses"] == [run["address"] for run in page["runs"]] == owners
        assert page["scope"]["matched_runs"] == 110 and page["scope"]["returned_runs"] == len(owners)
        seen.extend(owners)
        after = page.get("entry_truncation", {}).get("retrieve", {}).get("after")
        if after is None:
            break
    assert seen == [f"run:{spool_id}" for spool_id in ids]
    empty = reap_store(tmp_path, "shared", session=True, filters="agent")
    assert empty["ok"] is True and empty["entries"] == empty["runs"] == []
    assert empty["scope"]["diagnostics"]["support_counts"]["normalized"] == 110
    assert len(render_response(empty)) < 3000


def test_page_ok_is_explained_by_represented_runs_or_empty_scope_diagnostics(tmp_path):
    ids = _outline_session(tmp_path, 9)
    (tmp_path / f"{ids[-1]}.stdout").write_text("not json\n")
    first = reap_store(tmp_path, "shared", session=True)
    assert first["ok"] is True
    assert first["scope"]["diagnostics"]["error_runs"] == 1
    assert all(issue["severity"] != "error" for run in first["runs"] for issue in run["issues"])
    second = reap_store(tmp_path, "shared", session=True, after=first["run_truncation"]["retrieve"]["after"])
    assert second["ok"] is False
    assert any(issue["severity"] == "error" for run in second["runs"] for issue in run["issues"])
    for index, expected_ok in [(0, True), (-1, False)]:
        page = reap_store(tmp_path, "shared", session=True, addresses=f"run:{ids[index]}/final")
        assert page["ok"] is expected_ok
        assert page["scope"]["run_addresses"] == [f"run:{ids[index]}"]
    end = reap_store(tmp_path, "shared", session=True, after=f"run:{ids[-1]}")
    assert end["ok"] is False and end["runs"] == []
    assert end["scope"]["diagnostics"]["error_runs"] == 1


@pytest.mark.parametrize(
    "harness, capture, support, errors",
    [
        ("gemini", '{"response":"captured"}', "raw-only", 0),
        ("claude-code", '{"type":"system"}', "partial", 0),
        ("codex", "malformed\n", "normalized", 1),
    ],
)
def test_empty_selection_preserves_scope_support_and_error_diagnostics(tmp_path, harness, capture, support, errors):
    _record(tmp_path, "empty", harness=harness)
    (tmp_path / "empty.stdout").write_text(capture)
    result = reap_store(tmp_path, "empty", filters="agent")
    assert result["entries"] == result["runs"] == result["scope"]["run_addresses"] == []
    assert result["ok"] is (errors == 0)
    assert result["scope"]["matched_runs"] == 1 and result["scope"]["returned_runs"] == 0
    diagnostics = result["scope"]["diagnostics"]
    assert diagnostics["error_runs"] == errors and diagnostics["support_counts"][support] == 1
    assert len(render_response(result)) < 3000
    outline = reap_store(tmp_path, **diagnostics["retrieve_outline"])
    assert outline["mode"] == "outline" and outline["runs"][0]["address"] == "run:empty"
    assert outline["runs"][0]["support"] == support


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
    result = json.loads(asyncio.run(tool.fn(spool_id, filters="user", format="json")))
    assert result["ok"] is True
    assert result["entries"][0]["address"] == f"run:{spool_id}/prompt"
    narrowed = json.loads(
        asyncio.run(tool.fn(spool_id, addresses=f"run:{spool_id}/final", filters="user", format="json"))
    )
    assert narrowed["ok"] is True
    assert narrowed["entries"] == []


def test_public_routes_accept_outline_after_cursors(tmp_path, monkeypatch, capsys):
    ids = _outline_session(tmp_path, 7)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    tool = asyncio.run(spindle.mcp.get_tool("spool_reap"))
    first_wire = asyncio.run(tool.fn(ids[0], session=True, format="json"))
    first = json.loads(first_wire)
    assert len(first_wire) <= DEFAULT_RESPONSE_CHARS
    after = first["run_truncation"]["retrieve"]["after"]
    second = json.loads(asyncio.run(tool.fn(ids[0], session=True, after=after, format="json")))
    assert first["scope"]["run_addresses"] + second["scope"]["run_addresses"] == [f"run:{spool_id}" for spool_id in ids]
    monkeypatch.setattr(sys, "argv", ["spindle", "reap", ids[0], "--session", "--after", after, "--json"])
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
    wire = asyncio.run(tool.fn("mcp-budget", addresses="run:mcp-budget/final", format="json"))
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
        [sys.executable, "-B", "-m", "spindle", "reap", "cli-unicode", "run:cli-unicode/final", "--json"],
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
    monkeypatch.setattr(
        sys, "argv", ["spindle", "reap", spool_id, f"run:{spool_id}/{node}", "--filter", "user", "--json"]
    )
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "selection"
    assert [entry["address"] for entry in result["entries"]] == ([f"run:{spool_id}/prompt"] if node == "prompt" else [])


# Compact presentation exercises the same resolved entries as the JSON reader.
def _presentation_timeline(store, count=24, **record):
    _record(store, "view", **record)
    events = []
    for i in range(count):
        events.append(
            {
                "type": "item.completed",
                "item": {"id": f"m{i}", "type": "agent_message", "text": f"message {i} " + "π" * 100},
            }
        )
        events.extend(_codex_call(f"c{i}", f"inspect {i}", f"output {i}"))
    (store / "view.stdout").write_text(_jsonl(*events))
    return reap_store(store, "view", addresses="run:view", full=True)["entries"]


def _group_addresses(store, group):
    result = reap_store(store, **group["read"], full=True)
    assert result["ok"], result
    addresses = [entry["address"] for entry in result["entries"]]
    assert len(addresses) == group["count"]
    return addresses


def test_compact_outline_covers_whole_large_chronology_and_recursively_progresses(tmp_path):
    _presentation_timeline(tmp_path, 170)
    overview = reap_store(tmp_path, "view", format="text")
    assert overview["selection"]["matched_entries"] == 511
    assert len(overview["groups"]) == 8
    assert max(g["count"] for g in overview["groups"]) - min(g["count"] for g in overview["groups"]) <= 1
    seen = []
    for group in overview["groups"]:
        represented = _group_addresses(tmp_path, group)
        seen.extend(represented)
        zoom = reap_store(tmp_path, **group["zoom"])
        assert len(zoom["groups"]) == 8
        assert max(g["count"] for g in zoom["groups"]) < group["count"]
        assert [a for g in zoom["groups"] for a in _group_addresses(tmp_path, g)] == represented
    assert len(seen) == len(set(seen)) == 511
    assert seen[0] == "run:view/prompt" and seen[-1] == "run:view/events/509/parts/0"
    wire = render_response(overview, "text")
    assert "Saved result: 14 chars final" in wire and "Raw: 510 events raw" in wire
    assert wire.splitlines()[0] == "complete; 171 messages, 170 calls; 511 selected entries view"
    assert "0 remaining" not in wire and " · " not in wire
    assert "final for view" not in wire
    assert len(wire) < 3500


@pytest.mark.parametrize("count", [1, 8, 9])
def test_compact_balanced_nine_and_bounded_leaves(tmp_path, count):
    _presentation_timeline(tmp_path, count)
    overview = reap_store(tmp_path, "view", filters="agent", outline=True)
    assert [g["count"] for g in overview["groups"]] == ([2] + [1] * 7 if count == 9 else [1] * count)
    for group in overview["groups"]:
        assert all(len(clue["preview"]) <= 80 for clue in group["clues"])
        if group["count"] == 1:
            assert group["chars"] > 80 and group["leaf"]["role"] == "agent"
    wire = render_response(overview, "text")
    assert "  agent: message 0…" in wire
    first_selector = overview["groups"][0]["selector"].replace("run:view/", "")
    assert wire.splitlines()[2].endswith(" " + first_selector)
    if count == 9:
        nested = reap_store(tmp_path, **overview["groups"][0]["zoom"])
        assert [g["count"] for g in nested["groups"]] == [1, 1]


def test_filtered_outline_commands_roundtrip_and_preserve_tool_name(tmp_path, monkeypatch, capsys):
    import shlex

    _presentation_timeline(tmp_path, 12)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    # Quotes/metacharacters are data in the emitted shell command.
    tool_name = "inspect ' ; $(echo wrong)"
    capture = (
        (tmp_path / "view.stdout")
        .read_text()
        .replace(
            '"command_execution", "command"', '"command_execution", "name": ' + json.dumps(tool_name) + ', "command"'
        )
    )
    (tmp_path / "view.stdout").write_text(capture)
    overview = reap_store(
        tmp_path, "view", filters="agent,tool-calls", tool_name=tool_name, outline=True, format="text"
    )
    expected = reap_store(tmp_path, "view", filters="agent,tool-calls", tool_name=tool_name, full=True)
    assert [a for g in overview["groups"] for a in _group_addresses(tmp_path, g)] == [
        e["address"] for e in expected["entries"]
    ]
    wire = render_response(overview, "text")
    read_line = next(line.removeprefix("Read: ") for line in wire.splitlines() if line.startswith("Read: "))
    argv = shlex.split(read_line)
    assert "--tool=" + tool_name in argv and argv.count("--filter") == 2
    monkeypatch.setattr(sys, "argv", [*argv, "--json"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    assert [e["address"] for e in json.loads(capsys.readouterr().out)["entries"]] == _group_addresses(
        tmp_path, overview["groups"][0]
    )


def test_sparse_filtered_outline_pages_gaps_without_widening(tmp_path):
    _presentation_timeline(tmp_path, 30)
    agents = reap_store(tmp_path, "view", filters="agent")["entries"]
    chosen = [entry["address"] for entry in agents[::2]]
    request = dict(handle="view", addresses=chosen, filters="agent", outline=True, format="text")
    first = reap_store(tmp_path, **request)
    assert len(first["groups"]) == 8 and first["selection"]["omitted_entries"] == 7
    assert [a for g in first["groups"] for a in _group_addresses(tmp_path, g)] == chosen[:8]
    second = reap_store(tmp_path, **first["entry_truncation"]["retrieve"], format="text")
    assert [a for g in second["groups"] for a in _group_addresses(tmp_path, g)] == chosen[8:]
    assert "entry_truncation" not in second
    assert second["selection"]["entries_before_page"] == 8
    assert "--after" in render_response(first, "text")


def test_outline_separates_spools_and_raw_families_but_content_batches_work(tmp_path):
    _presentation_timeline(tmp_path, 2, session_id="shared")
    _record(tmp_path, "other", session_id="shared")
    request = dict(handle="shared", session=True, addresses="run:view/prompt,run:other/final")
    content = reap_store(tmp_path, **request, format="text")
    assert len(content["entries"]) == 2
    assert "run:view/prompt" in render_response(content, "text")
    assert reap_store(tmp_path, **request, outline=True)["error"]["code"] == "multiple_spool_outline"
    assert (
        reap_store(tmp_path, "view", addresses="prompt,raw", outline=True)["error"]["code"] == "mixed_outline_families"
    )
    raw = reap_store(tmp_path, "view", addresses="raw", outline=True)
    assert [a for g in raw["groups"] for a in _group_addresses(tmp_path, g)] == [
        f"run:view/events/{i}" for i in range(6)
    ]


def test_outline_navigation_retains_meaning_after_live_append(tmp_path):
    _presentation_timeline(tmp_path, 10, status="running", result=None)
    before = reap_store(tmp_path, "view", format="text")
    addresses = [_group_addresses(tmp_path, g) for g in before["groups"]]
    with (tmp_path / "view.stdout").open("a") as stream:
        stream.write(
            _jsonl({"type": "item.completed", "item": {"id": "appended", "type": "agent_message", "text": "later"}})
        )
    assert [_group_addresses(tmp_path, g) for g in before["groups"]] == addresses
    assert reap_store(tmp_path, "view", format="text")["selection"]["matched_entries"] == 32


def test_relative_addresses_ranges_and_cursors_canonicalize(tmp_path):
    _presentation_timeline(tmp_path, 3)
    for relative in (
        "prompt",
        "final",
        "messages",
        "tool-calls",
        "tool-results",
        "raw",
        "events/0",
        "events/0/parts/0",
    ):
        actual = reap_store(tmp_path, "view", addresses=relative)
        expected = reap_store(tmp_path, "view", addresses="run:view/" + relative)
        assert actual == expected
    ranged = reap_store(tmp_path, "view", addresses="prompt..events/3/parts/0", after="events/0/parts/0")
    assert ranged == reap_store(
        tmp_path, "view", addresses="run:view/prompt..run:view/events/3/parts/0", after="run:view/events/0/parts/0"
    )
    for kwargs in (
        {"addresses": "prompt"},
        {"addresses": "run:view/prompt..final"},
        {"filters": "messages", "after": "prompt"},
    ):
        result = reap_store(tmp_path, "view", session=True, **kwargs)
        assert not result["ok"] and "canonical" in result["error"]["message"]
    for invalid in ("../prompt", "./final", "events/0/../1", "1", "/prompt"):
        assert reap_store(tmp_path, "view", addresses=invalid)["error"]["code"] == "invalid_address"


def test_exact_leaf_is_direct_but_singleton_collection_is_labelled(tmp_path):
    _presentation_timeline(tmp_path, 1)
    exact = reap_store(tmp_path, "view", addresses="final", format="text")
    assert render_response(exact, "text") == "final for view"
    collection = reap_store(tmp_path, "view", filters="agent", format="text")
    assert render_response(collection, "text").startswith("agent events/0/parts/0\n")
    results = reap_store(tmp_path, "view", addresses="tool-results", format="text")
    assert render_response(results, "text").startswith("tool result command_execution events/2/parts/0\n")
    final_newline = "\n body with trailing whitespace \n\t"
    _record(tmp_path, "view", result=final_newline)
    assert render_response(reap_store(tmp_path, "view", addresses="final", format="text"), "text") == final_newline


@pytest.mark.parametrize("budget", [4096, 50_000])
def test_text_budget_counts_escaped_output_and_content_cursor_advances(tmp_path, budget):
    _presentation_timeline(tmp_path, 1, result="\x1b\r\b\0\x85\x9b\ud800π😀\t\n" * 7000)
    offset, chunks = 0, []
    payload = json.loads((tmp_path / "view.json").read_text())["result"]
    for _ in range(150):
        response = reap_store(
            tmp_path, "view", addresses="final", offset=offset, limit=70_000, format="text", max_chars=budget
        )
        assert response["ok"] and response["entries"]
        wire = render_response(response, "text")
        assert len(wire) <= budget and wire.encode("utf-8")
        assert not any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) <= 159 for c in wire)
        entry = response["entries"][0]
        chunks.append(entry["content"])
        continuation = entry.get("truncation", {}).get("retrieve")
        if continuation is None:
            break
        assert f"Spindle: chars {offset}..{continuation['offset']} of {len(payload)} final; more content:" in wire
        assert continuation["offset"] > offset
        offset = continuation["offset"]
    assert "".join(chunks) == payload
    assert (
        json.loads(render_response(reap_store(tmp_path, "view", addresses="final", full=True)))["entries"][0]["content"]
        == payload
    )


def test_text_budget_does_not_count_invisible_json_metadata(tmp_path):
    _presentation_timeline(tmp_path, 1, result="r" * 6000, session_id="s" * 20_000)
    response = reap_store(tmp_path, "view", addresses="final", format="text", max_chars=4096)
    assert response["ok"]
    assert len(response["entries"][0]["content"]) > 3700
    assert len(render_response(response, "text")) <= 4096
    # Full means complete content, with the pre-existing 200-entry cap.
    _presentation_timeline(tmp_path, 210)
    response = reap_store(tmp_path, "view", filters="agent", full=True, format="text", max_chars=10)
    assert len(response["entries"]) == 200
    assert response["entry_truncation"]["retrieve"]["full"] is True


def test_text_entry_continuation_and_content_continuation_are_distinct(tmp_path):
    _presentation_timeline(tmp_path, 12)
    first = reap_store(tmp_path, "view", filters="agent", limit=10, format="text", max_chars=1000)
    assert first["entries"] and first["entry_truncation"]
    wire = render_response(first, "text")
    assert "more content:" in wire and "more entries:" in wire
    seen = [e["address"] for e in first["entries"]]
    page = first
    for _ in range(15):
        continuation = page.get("entry_truncation", {}).get("retrieve")
        if not continuation:
            break
        page = reap_store(
            tmp_path, **{k: v for k, v in continuation.items() if k != "instruction"}, format="text", max_chars=1000
        )
        assert page["entries"] and len(render_response(page, "text")) <= 1000
        seen.extend(e["address"] for e in page["entries"])
    assert len(seen) == len(set(seen)) == 12
    impossible = reap_store(tmp_path, "view", addresses="final", format="text", max_chars=1)
    assert not impossible["ok"] and "truncation" not in impossible


@pytest.mark.parametrize(
    "capture,harness,status,notice",
    [
        (None, "codex", "complete", "no stdout"),
        ('{"response":"saved"}', "gemini", "error", "raw-only"),
        ('{"type":"system"}', "claude-code", "timeout", "partial"),
        ("broken\n", "codex", "complete", "malformed"),
    ],
)
def test_compact_empty_selection_keeps_abnormal_evidence(tmp_path, capture, harness, status, notice):
    _record(tmp_path, "empty", harness=harness, status=status)
    if capture is not None:
        (tmp_path / "empty.stdout").write_text(capture)
    for kwargs in ({}, {"filters": "agent"}, {"filters": "agent", "outline": True}):
        result = reap_store(tmp_path, "empty", **kwargs, format="text")
        wire = render_response(result, "text")
        assert notice in wire and "Spindle:" in wire
        notices = [line for line in wire.splitlines() if line.startswith("Spindle:")]
        assert len(notices) == 1 and notices[0].endswith(" run:empty")
        assert not notices[0].startswith("Spindle: run:")
        if status != "complete":
            assert status in wire
    if capture is not None:
        raw = reap_store(tmp_path, "empty", addresses="raw", outline=True, format="text")
        assert raw["groups"][0]["count"] == 1
        assert raw["groups"][0]["clues"][0]["preview"]


def test_compact_session_pages_eight_saved_spools_with_fallback(tmp_path):
    ids = _outline_session(tmp_path, 10)
    record = json.loads((tmp_path / f"{ids[0]}.json").read_text())
    record.update(prompt=None, result=None, status="timeout")
    (tmp_path / f"{ids[0]}.json").write_text(json.dumps(record))
    (tmp_path / f"{ids[0]}.stdout").write_text(_jsonl(*_codex_call("only", "useful input", "useful result")))
    first = reap_store(tmp_path, "shared", session=True, format="text")
    assert [r["spool_id"] for r in first["runs"]] == ids[:8]
    assert first["scope"]["omitted_runs"] == 2
    wire = render_response(first, "text")
    assert "timeout" in wire and "useful input" in wire
    assert wire.splitlines()[0].startswith("Session: 10 saved spools; showing 8; 2 remaining; this page: ")
    assert wire.splitlines()[1] == f"timeout; 1 call {ids[0]}"
    second = reap_store(tmp_path, **first["run_truncation"]["retrieve"], format="text")
    assert [r["spool_id"] for r in second["runs"]] == ids[8:]
    assert "run_truncation" not in second
    assert (
        render_response(second, "text").splitlines()[0]
        == "Session: 10 saved spools; showing 2; this page: 12 messages, 10 calls"
    )
    # An entry cursor cannot masquerade as a saved-spool cursor.
    invalid = reap_store(tmp_path, "shared", session=True, after=f"run:{ids[0]}/prompt", format="text")
    assert invalid["error"]["code"] == "invalid_after"


@pytest.mark.parametrize("controls", [{"offset": 1}, {"limit": 1}, {"full": True}])
def test_outline_rejects_content_controls(tmp_path, controls):
    _presentation_timeline(tmp_path, 1)
    for kwargs in ({"format": "text"}, {"outline": True}, {"outline": True, "filters": "agent"}):
        assert reap_store(tmp_path, "view", **kwargs, **controls)["error"]["code"] == "outline_content_controls"


def test_public_text_default_json_opt_in_parity_and_strict_format(tmp_path, monkeypatch, capsys):
    _presentation_timeline(tmp_path, 2)
    monkeypatch.setattr(spindle, "SPINDLE_DIR", tmp_path)
    tool = asyncio.run(spindle.mcp.get_tool("spool_reap"))
    for options, args in [
        ({}, []),
        ({"addresses": "final"}, ["final"]),
        ({"filters": "agent", "outline": True}, ["--filter", "agent", "--outline"]),
        ({"format": "json"}, ["--json"]),
        ({"outline": True, "format": "json"}, ["--outline", "--json"]),
        ({"addresses": "missing"}, ["missing"]),
    ]:
        wire = asyncio.run(tool.fn("view", **options))
        monkeypatch.setattr(sys, "argv", ["spindle", "reap", "view", *args])
        with pytest.raises(SystemExit) as exc:
            spindle.main()
        assert exc.value.code == (1 if options.get("addresses") == "missing" else 0)
        assert capsys.readouterr().out == wire + "\n"
        if options.get("format") == "json":
            assert json.loads(wire)["mode"] == ("selection-outline" if options.get("outline") else "outline")
    for invalid in ("xml", "TEXT", "", None):
        assert asyncio.run(tool.fn("view", format=invalid)) == "Spindle: format must be 'text' or 'json'"
    # The appended controls leave every historical positional argument intact.
    assert asyncio.run(tool.fn("view", "final", None, None, False, None, 0, None, False)) == "final for view"
    (tmp_path / "view.stdout").write_text("malformed\n")
    monkeypatch.setattr(sys, "argv", ["spindle", "reap", "view", "final"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 1 and "Spindle:" in capsys.readouterr().out


def test_emitted_navigation_quotes_shell_data_and_terminal_controls():
    from spindle.reap_text import command

    name = "shell ' ; $(printf WRONG) `printf WRONG` \x1b\x85 π"
    emitted = command(
        {"handle": "fixture", "addresses": ["run:fixture/prompt"], "filters": ["tool-calls"], "tool_name": name}
    )
    assert "\x1b" not in emitted and "\x85" not in emitted
    # A local shell stub captures argv; no Spindle process or store is touched.
    result = subprocess.run(
        ["bash", "-c", 'spindle() { printf "%s\\0" "$@"; }; ' + emitted], capture_output=True, check=True, timeout=10
    )
    assert result.stdout.decode().split("\0") == [
        "reap",
        "fixture",
        "prompt",
        "--filter",
        "tool-calls",
        "--tool=" + name,
        "",
    ]


def test_text_cli_subprocess_escapes_controls_in_exact_leaf(tmp_path):
    store = tmp_path / "spools"
    store.mkdir()
    _record(store, "safe", result="\x1b[2J\r\b\0\x85\x9b\ud800π😀")
    (store / "safe.stdout").write_text(_jsonl({"type": "thread.started"}))
    env = {**os.environ, "SPINDLE_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8:strict"}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "spindle", "reap", "safe", "final"],
        env=env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.decode() == "\\x1b[2J\\x0d\\x08\\x00\\x85\\x9b\\ud800π😀\n"


@pytest.mark.parametrize(
    "harness,capture,status,summary",
    [
        ("codex", None, "complete", "1 spool with missing capture"),
        ("claude-code", "{}", "complete", "1 spool with partial capture"),
        ("gemini", "{}", "complete", "1 spool with raw-only capture"),
        ("codex", "broken\n", "complete", "1 spool with read errors"),
        ("codex", "{}", "timeout", "1 spool with status timeout"),
    ],
)
def test_text_session_notices_include_nonmatching_spools_without_repeating_owners(
    tmp_path, harness, capture, status, summary
):
    _record(tmp_path, "good", session_id="shared")
    (tmp_path / "good.stdout").write_text(
        _jsonl({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "healthy answer"}})
    )
    _record(tmp_path, "other", session_id="shared", harness=harness, status=status)
    if capture is not None:
        (tmp_path / "other.stdout").write_text(capture)
    for outline in (False, True):
        page = reap_store(
            tmp_path, "shared", session=True, filters="agent", outline=outline, format="text", max_chars=4096
        )
        assert page["ok"] is True and page["scope"]["run_addresses"] == ["run:good"]
        snapshot = json.dumps(page)
        wire = render_response(page, "text")
        assert len(wire) <= 4096 and wire.count("Spindle: Elsewhere in this scope:") == 1
        assert summary in wire and "Inspect: spindle reap shared --session" in wire
        assert "run:other" not in wire
        assert render_response(page, "text") == wire and json.dumps(page) == snapshot
    empty = reap_store(tmp_path, "shared", session=True, filters="tool-calls", format="text")
    assert not empty["entries"] and not empty["runs"]
    assert empty["ok"] is (harness != "codex" or capture != "broken\n")
    assert summary in render_response(empty, "text")
    represented = reap_store(
        tmp_path, "shared", session=True, addresses="run:good/final,run:other/final", format="text"
    )
    assert represented["ok"] == empty["ok"]
    notices = [line for line in render_response(represented, "text").splitlines() if line.startswith("Spindle:")]
    assert len(notices) == 1 and notices[0].endswith(" run:other")
    assert not notices[0].startswith("Spindle: run:") and "Elsewhere" not in notices[0]


def test_text_content_pages_budget_scope_notices_and_subtract_page_owners(tmp_path):
    _record(tmp_path, "a", session_id="shared", result="π\x1b" * 6000)
    _record(tmp_path, "b", session_id="shared", result="last")
    (tmp_path / "b.stdout").write_text("{}")
    # Many nonmatching spools must still produce one fixed-category summary,
    # with no enumeration of their identities or repeated represented details.
    for i in range(20):
        _record(tmp_path, f"hidden-{i}", session_id="shared")
    first = reap_store(
        tmp_path, "shared", session=True, addresses="run:a/final,run:b/final", format="text", max_chars=1600
    )
    assert first["ok"] and first["scope"]["run_addresses"] == ["run:a"]
    wire = render_response(first, "text")
    assert len(wire) <= 1600 and "\x1b" not in wire and "\\x1b" in wire
    assert wire.count("Spindle: Elsewhere in this scope:") == 1
    assert "20 spools with missing capture" in wire and "21 spools with missing capture" not in wire
    assert "hidden-" not in wire and "no stdout or saved transcript capture exists for this spool run:a" in wire
    entry = first["entries"][0]
    offset = entry["truncation"]["retrieve"]["offset"]
    assert offset > 0
    notice = next(line for line in wire.splitlines() if line.startswith("Spindle: chars"))
    assert notice.startswith(f"Spindle: chars 0..{offset} of 12000 run:a/final; more content: ")
    more_content = reap_store(
        tmp_path, "shared", session=True, addresses="run:a/final", offset=offset, format="text", max_chars=1600
    )
    assert more_content["entries"][0]["truncation"]["retrieve"]["offset"] > offset
    assert len(render_response(more_content, "text")) <= 1600
    continuation = first["entry_truncation"]["retrieve"]
    second = reap_store(
        tmp_path, **{k: v for k, v in continuation.items() if k != "instruction"}, format="text", max_chars=1600
    )
    assert second["ok"] and [entry["address"] for entry in second["entries"]] == ["run:b/final"]
    wire = render_response(second, "text")
    assert len(wire) <= 1600 and "21 spools with missing capture" in wire
    assert "no stdout" not in wire and "entry_truncation" not in second


def test_text_session_outline_pages_keep_unrepresented_failures_visible(tmp_path):
    ids = _outline_session(tmp_path, 9)
    (tmp_path / f"{ids[0]}.stdout").unlink()
    (tmp_path / f"{ids[-1]}.stdout").write_text("broken\n")
    first = reap_store(tmp_path, "shared", session=True, format="text", max_chars=4096)
    assert first["ok"] is True and len(first["runs"]) == 8
    wire = render_response(first, "text")
    assert len(wire) <= 4096
    details, summary = [line for line in wire.splitlines() if line.startswith("Spindle:")][:2]
    assert details.endswith(f" run:{ids[0]}") and details.startswith("Spindle: no stdout")
    assert summary.startswith("Spindle: Elsewhere in this scope: 1 spool with read errors")
    assert "missing capture" not in summary and "capture warnings" not in summary
    second = reap_store(tmp_path, **first["run_truncation"]["retrieve"], format="text", max_chars=4096)
    assert second["ok"] is False and len(second["runs"]) == 1
    wire = render_response(second, "text")
    assert len(wire) <= 4096
    details, summary = [line for line in wire.splitlines() if line.startswith("Spindle:")]
    assert details.endswith(f" run:{ids[-1]}") and details.startswith("Spindle: capture contains malformed")
    assert "1 spool with missing capture" in summary and "read errors" not in summary
