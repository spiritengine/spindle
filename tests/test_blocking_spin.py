"""MCP launch/wait contracts, exercised through the real client and transports."""

import asyncio
import itertools
import json

import pytest
from fastmcp import Client
from mcp import types
from mcp.shared.exceptions import McpError

import spindle


@pytest.fixture
def launch_store(monkeypatch):
    """Substitute only provider launch and maintenance; retain the real spool store."""
    launched = []
    counter = itertools.count()
    monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda sid: None)
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda: None)
    # raising=False lets the tests demonstrate failures on the pre-change tree.
    monkeypatch.setattr(spindle, "MCP_WAIT_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(spindle, "MCP_WAIT_HEARTBEAT_INTERVAL", 0.02, raising=False)

    def launch(*args, **kwargs):
        sid = f"test-spool-{next(counter)}"
        spindle._write_spool(
            sid,
            {
                "id": sid,
                "status": "running",
                "prompt": "same prompt",
                "result": None,
                "tags": ["unique-review"],
                "working_dir": "/project",
                "harness": "claude-code",
            },
        )
        launched.append(sid)
        return sid

    monkeypatch.setattr(spindle, "_spin_sync", launch)
    monkeypatch.setattr(spindle, "_respin_sync", launch)
    for harness in ("codex", "gemini", "kimi"):
        monkeypatch.setattr(spindle, f"_{harness}_spin_sync", launch)
    return launched


def finish(sid, status="complete", result="READY", error=None):
    record = spindle._read_spool(sid)
    record.update(status=status, result=result, error=error)
    spindle._write_spool(sid, record)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    "tool,harness",
    [
        ("spin", None),
        ("spin", "codex"),
        ("spin", "gemini"),
        ("spin", "kimi"),
        ("respin", None),
    ],
)
async def test_default_blocks_and_returns_object(launch_store, tool, harness):
    args = {"prompt": "work", "working_dir": "/project"} if tool == "spin" else {"session_id": "old", "prompt": "next"}
    if harness:
        args["harness"] = harness
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool(tool, args))
        await until(lambda: launch_store)
        await asyncio.sleep(0.04)
        assert not task.done(), "default launch returned before the spool finished"
        finish(launch_store[0])
        response = await asyncio.wait_for(task, 3)
    payload = {"spool_id": launch_store[0], "status": "complete", "result": "READY"}
    assert response.structured_content == payload
    assert json.loads(response.content[0].text) == payload


@pytest.mark.parametrize("tool", ["spin", "respin"])
async def test_detached_wire_is_bare_id(launch_store, tool):
    args = {"prompt": "work", "wait": False}
    args.update(working_dir="/project") if tool == "spin" else args.update(session_id="old")
    async with Client(spindle.mcp) as client:
        response = await client.call_tool(tool, args)
    assert response.content[0].text == launch_store[0]
    assert response.structured_content == {"result": launch_store[0]}
    assert spindle._read_spool(launch_store[0])["status"] == "running"


async def test_prelaunch_error_wire_unchanged(monkeypatch):
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("spin", {"prompt": "missing directory"})
    assert response.content[0].text.startswith("Error: working_dir required")
    assert response.structured_content == {"result": response.content[0].text}


@pytest.mark.parametrize("status", ["complete", "error", "timeout", "abandoned"])
async def test_terminal_payload_and_budget(launch_store, status):
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool("spin", {"prompt": "work", "working_dir": "/project"}))
        await until(lambda: launch_store)
        sid = launch_store[0]
        finish(sid, status, "START" + "x" * 100000 + "END", "provider failed")
        response = await asyncio.wait_for(task, 4)
        unspooled = await client.call_tool("unspool", {"spool_id": sid})
    payload = response.structured_content
    assert payload["spool_id"] == sid
    assert payload["status"] == status
    assert payload["result"] == unspooled.content[0].text
    if status == "complete":
        assert payload["result"].startswith("START")
        assert payload["result"].endswith("END")
        assert f'unspool("{sid}", full=True)' in payload["result"]
        assert "spool_export" in payload["result"]
        assert len(payload["result"]) < spindle.UNSPOOL_MAX_CHARS


async def test_spools_exposes_discovery_fields(launch_store):
    spindle._spin_sync()
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("spools", {})
    record = json.loads(response.content[0].text)[launch_store[0]]
    assert record["tags"] == ["unique-review"]
    assert record["working_dir"] == "/project"
    assert record["harness"] == "claude-code"


def test_cli_spools_keeps_original_fields(launch_store):
    spindle._spin_sync()
    record = json.loads(spindle._spools_sync())[launch_store[0]]
    assert set(record) == {"status", "prompt", "created_at", "session_id"}


async def test_concurrent_blocking_calls(launch_store):
    async with Client(spindle.mcp) as client:
        tasks = [
            asyncio.create_task(client.call_tool("spin", {"prompt": str(i), "working_dir": "/project"}))
            for i in range(3)
        ]
        await until(lambda: len(launch_store) == 3)
        await asyncio.sleep(0.04)
        assert all(not t.done() for t in tasks)
        for sid in launch_store:
            finish(sid, result=sid)
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert {r.structured_content["spool_id"] for r in responses} == set(launch_store)


@pytest.mark.parametrize("mode", ["spin", "gather", "yield"])
async def test_wire_progress_is_monotonic_without_total(launch_store, mode):
    progress = []

    async def on_progress(value, total, message):
        progress.append((value, total, message))

    async with Client(spindle.mcp) as client:
        if mode == "spin":
            name, args = "spin", {"prompt": "work", "working_dir": "/project"}
        else:
            sid = spindle._spin_sync()
            name, args = "spin_wait", {"spool_ids": sid, "mode": mode}
        task = asyncio.create_task(client.call_tool(name, args, progress_handler=on_progress))
        await until(lambda: len(progress) >= 3)
        finish(launch_store[0])
        await asyncio.wait_for(task, 3)
    assert all(total is None for _, total, _ in progress)
    values = [value for value, _, _ in progress]
    assert values == sorted(set(values))
    assert all(launch_store[0] in message for _, _, message in progress)


async def test_shared_session_cancel_preserves_spool(launch_store):
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool("spin", {"prompt": "work", "working_dir": "/project"}))
        await until(lambda: launch_store)
        sid = launch_store[0]
        request_id = list(client.session._response_streams)[-1]
        await client.session.send_notification(
            types.ClientNotification(
                types.CancelledNotification(
                    method="notifications/cancelled",
                    params=types.CancelledNotificationParams(requestId=request_id, reason="cancel wait"),
                )
            )
        )
        with pytest.raises(McpError):
            await asyncio.wait_for(task, 2)
        assert spindle._read_spool(sid)["status"] == "running"
        finish(sid)
        response = await client.call_tool("unspool", {"spool_id": sid})
        assert response.content[0].text == "READY"


def http_request(app, message):
    """Real ASGI streamable HTTP session; allow deterministic network loss."""
    events = []
    queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": json.dumps(message).encode(), "more_body": False})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"mcp-protocol-version", b"2025-06-18"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }

    async def send(event):
        events.append(event)

    return asyncio.create_task(app(scope, queue.get, send)), queue, events


@pytest.mark.parametrize("token", [False, True])
@pytest.mark.parametrize("mode", ["spin", "gather", "yield"])
async def test_stateless_cancel_disconnect_releases_waiter(launch_store, monkeypatch, caplog, token, mode):
    polls = []
    monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda sid: polls.append(sid))
    app = spindle.mcp.http_app(path="/mcp", stateless_http=True)
    if mode == "spin":
        params = {"name": "spin", "arguments": {"prompt": "work", "working_dir": "/project"}}
    else:
        sid = spindle._spin_sync()
        params = {"name": "spin_wait", "arguments": {"spool_ids": sid, "mode": mode}}
    if token:
        params["_meta"] = {"progressToken": "watch"}
    async with app.router.lifespan_context(app):
        task, queue, events = http_request(app, {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": params})
        try:
            await until(lambda: len(polls) >= 3)
            sid = launch_store[0]
            before = spindle._get_spool_path(sid).read_bytes()
            cancel, _, _ = http_request(
                app,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 101, "reason": "cancel wait"},
                },
            )
            await asyncio.wait_for(cancel, 2)
            assert spindle._get_spool_path(sid).read_bytes() == before
            await queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 2)
            await asyncio.sleep(0.1)
            stopped_at = len(polls)
            await asyncio.sleep(0.1)
            assert len(polls) == stopped_at, "disconnected waiter is still polling"
            assert not [r for r in caplog.records if r.levelno >= 40], "disconnected request logged an error"
            assert spindle._get_spool_path(sid).read_bytes() == before
            notifications = [e for e in events if b"notifications/progress" in e.get("body", b"")]
            assert bool(notifications) is token
            finish(sid)
        finally:
            for sid in launch_store:
                finish(sid)
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("unspool", {"spool_id": sid})
        assert response.content[0].text == "READY"


@pytest.mark.parametrize("tool", ["spin", "respin"])
async def test_wait_deadline_returns_handle_without_stopping_spool(launch_store, tool):
    args = {"prompt": "work", "wait_timeout": -1}
    args.update(working_dir="/project") if tool == "spin" else args.update(session_id="old")
    async with Client(spindle.mcp) as client:
        response = await client.call_tool(tool, args)
    result = response.structured_content
    assert result["spool_id"] == launch_store[0]
    assert result["status"] == "running"
    assert result["wait_timed_out"] is True
    assert "this call stopped waiting" in result["result"]
    assert f'spin_wait("{launch_store[0]}")' in result["result"]
    assert spindle._read_spool(launch_store[0])["status"] == "running"


async def test_json_prelaunch_error_preserves_text_and_envelope():
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("spin", {"prompt": "x", "permission": "readonly", "shard": True})
    assert "error" in json.loads(response.content[0].text)
    assert response.structured_content == {"result": response.content[0].text}


async def test_respin_prelaunch_error_preserves_text_and_envelope():
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("respin", {"session_id": "missing", "prompt": "next"})
    assert response.content[0].text.startswith("Error:")
    assert response.structured_content == {"result": response.content[0].text}


async def test_spin_schema_advertises_wait_and_is_not_readonly():
    async with Client(spindle.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("spin", "respin"):
        tool = tools[name]
        assert tool.inputSchema["properties"]["wait"]["default"] is True
        assert not tool.annotations or tool.annotations.readOnlyHint is not True
        first_lines = tool.description.strip().splitlines()[:2]
        assert "blocks" in first_lines[0]
        assert "spin_wait" in first_lines[1]


async def test_structured_saved_result_uses_unspool_coercion(launch_store):
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool("spin", {"prompt": "work", "working_dir": "/project"}))
        await until(lambda: launch_store)
        sid = launch_store[0]
        finish(sid, result={"body": "x" * 100000})
        response = await asyncio.wait_for(task, 3)
        unspooled = await client.call_tool("unspool", {"spool_id": sid})
    assert response.structured_content["result"] == unspooled.content[0].text


async def test_cancel_during_launch_does_not_abort_publication(launch_store, monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()
    original_launch = spindle._spin_sync

    def delayed_launch(*args, **kwargs):
        entered.set()
        assert release.wait(3), "test failed to release launch"
        return original_launch(*args, **kwargs)

    monkeypatch.setattr(spindle, "_spin_sync", delayed_launch)
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool("spin", {"prompt": "work", "working_dir": "/project"}))
        try:
            await until(entered.is_set)
            request_id = list(client.session._response_streams)[-1]
            await client.session.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        method="notifications/cancelled", params=types.CancelledNotificationParams(requestId=request_id)
                    )
                )
            )
            with pytest.raises(McpError):
                await asyncio.wait_for(task, 1)
        finally:
            release.set()
        await until(lambda: launch_store)
        sid = launch_store[0]
        assert spindle._read_spool(sid)["status"] == "running"
        finish(sid)
        response = await client.call_tool("unspool", {"spool_id": sid})
        assert response.content[0].text == "READY"


def test_cli_spin_and_wait_preserve_detached_output(launch_store, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["spindle", "spin", "work", "--working-dir", "/project"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    sid = launch_store[0]
    assert capsys.readouterr().out == json.dumps({"spool_id": sid}) + "\n"
    assert spindle._read_spool(sid)["status"] == "running"
    body = "BEGIN" + "x" * 100000 + "END"
    finish(sid, result=body)
    monkeypatch.setattr(sys, "argv", ["spindle", "wait", sid, "--timeout", "1"])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == json.dumps({sid: body}, indent=2) + "\n"


@pytest.mark.parametrize("mode", ["gather", "yield"])
async def test_empty_saved_record_is_unknown_instead_of_waiting(launch_store, mode):
    # The previous loops treated falsey JSON records exactly like missing files.
    sid = "empty-record"
    spindle._write_spool(sid, {})
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("spin_wait", {"spool_ids": sid, "mode": mode, "timeout": -1})
    if mode == "gather":
        assert response.content[0].text == f"Error: Unknown spool_id '{sid}'"
    else:
        assert json.loads(response.content[0].text) == {
            "spool_id": sid,
            "error": f"Unknown spool_id '{sid}'",
            "remaining": [],
        }


@pytest.mark.parametrize("wait_args", [{}, {"wait": False}], ids=["default", "detached"])
@pytest.mark.parametrize("state", ["pending", "running", "complete", "error", "timeout", "parked"])
async def test_respin_refusals_preserve_text_and_envelope(monkeypatch, wait_args, state):
    # Keep the actual resolver and parked-state detector, including long refusals.
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda: None)
    sid = "refused-spool"
    record = {"id": sid, "status": state, "harness": "claude-code"}
    if state in {"pending", "running"}:
        record["session_id"] = "already-running-session"
        expected = f"Spool '{sid}' is not in a resumable state (status={state}); wait for it to complete before respin"
    elif state == "parked":
        task_ids = [f"background-task-{i}" for i in range(5)]
        record.update(
            status="error",
            error_kind="headless_background_wait",
            pending_background_tasks=[{"id": task_id} for task_id in task_ids],
        )
        expected = (
            f"Spool '{sid}' parked waiting on background task(s) [{', '.join(task_ids)}] and has no "
            "saved transcript to rebuild from. Resuming it would replay a stale task "
            "notification and park again — start a fresh spin() with the task context instead."
        )
        assert len(expected.encode()) > 255
    else:
        expected = f"Spool '{sid}' completed without a resumable session (status={state})"
    spindle._write_spool(sid, record)
    before = spindle._get_spool_path(sid).read_bytes()
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("respin", {"session_id": sid, "prompt": "next", **wait_args})
    assert response.content[0].text == expected
    assert response.structured_content == {"result": expected}
    assert spindle._get_spool_path(sid).read_bytes() == before
    assert list(spindle.SPINDLE_DIR.glob("*.json")) == [spindle._get_spool_path(sid)]


@pytest.mark.parametrize(
    "tool,harness", [("spin", None), ("spin", "codex"), ("spin", "gemini"), ("spin", "kimi"), ("respin", None)]
)
@pytest.mark.parametrize("launch_result", ["missing-record", "x" * 300, "Error: refused", '{"error": "refused"}'])
async def test_unpublished_launch_returns_unchanged(monkeypatch, tool, harness, launch_result):
    launcher = "_respin_sync" if tool == "respin" else f"_{harness}_spin_sync" if harness else "_spin_sync"
    monkeypatch.setattr(spindle, launcher, lambda *a, **kw: launch_result)
    args = (
        {"session_id": "old", "prompt": "next"} if tool == "respin" else {"working_dir": "/project", "prompt": "work"}
    )
    if harness:
        args["harness"] = harness
    async with Client(spindle.mcp) as client:
        response = await client.call_tool(tool, args)
    assert response.content[0].text == launch_result
    assert response.structured_content == {"result": launch_result}


@pytest.mark.parametrize("tool", ["spin", "respin"])
@pytest.mark.parametrize("status", ["complete", "error", "timeout", "abandoned"])
async def test_completion_during_wait_expiry_returns_terminal_result(launch_store, monkeypatch, tool, status):
    original_wait = spindle._wait_for_spools

    async def finish_at_deadline(ids, *args):
        outcome = await original_wait(ids, *args)
        assert outcome.timed_out
        finish(ids[0], status=status, error="provider failed")
        return outcome

    monkeypatch.setattr(spindle, "_wait_for_spools", finish_at_deadline)
    args = {"prompt": "work", "wait_timeout": -1}
    args.update(working_dir="/project") if tool == "spin" else args.update(session_id="old")
    async with Client(spindle.mcp) as client:
        response = await client.call_tool(tool, args)
        unspooled = await client.call_tool("unspool", {"spool_id": launch_store[0]})
    assert response.structured_content == {
        "spool_id": launch_store[0],
        "status": status,
        "result": unspooled.content[0].text,
    }


@pytest.mark.parametrize("tool", ["spin", "respin"])
async def test_zero_wait_timeout_has_no_deadline(launch_store, tool):
    args = {"prompt": "work", "wait_timeout": 0}
    args.update(working_dir="/project") if tool == "spin" else args.update(session_id="old")
    async with Client(spindle.mcp) as client:
        task = asyncio.create_task(client.call_tool(tool, args))
        await until(lambda: launch_store)
        await asyncio.sleep(0.04)
        assert not task.done()
        finish(launch_store[0])
        response = await asyncio.wait_for(task, 3)
        description = next(t.description for t in await client.list_tools() if t.name == tool)
    assert response.structured_content["result"] == "READY"
    assert "0 means no deadline" in description


@pytest.mark.parametrize("token", [False, True])
@pytest.mark.parametrize("mode", ["spin", "respin", "gather", "yield"])
@pytest.mark.parametrize("exit_kind", ["complete", "deadline", "reply_handoff"])
async def test_disconnect_before_final_reply_is_quiet(launch_store, monkeypatch, caplog, token, mode, exit_kind):
    from mcp.shared.session import RequestResponder

    # No second heartbeat can retire the call before its final response.
    monkeypatch.setattr(spindle, "MCP_WAIT_HEARTBEAT_INTERVAL", 30)
    entered_reply = asyncio.Event()
    release_reply = asyncio.Event()
    reply_done = asyncio.Event()
    original_respond = RequestResponder.respond

    async def respond(self, response):
        entered_reply.set()
        if exit_kind == "reply_handoff":
            await release_reply.wait()
        try:
            return await original_respond(self, response)
        finally:
            reply_done.set()

    monkeypatch.setattr(RequestResponder, "respond", respond)
    if mode in {"spin", "respin"}:
        args = {"prompt": "work"}
        args.update(working_dir="/project") if mode == "spin" else args.update(session_id="old")
        if exit_kind == "deadline":
            args["wait_timeout"] = 1
        params = {"name": mode, "arguments": args}
    else:
        sid = spindle._spin_sync()
        args = {"spool_ids": sid, "mode": mode}
        if exit_kind == "deadline":
            args["timeout"] = 1
        params = {"name": "spin_wait", "arguments": args}
    if token:
        params["_meta"] = {"progressToken": "watch"}
    app = spindle.mcp.http_app(path="/mcp", stateless_http=True)
    async with app.router.lifespan_context(app):
        task, queue, events = http_request(app, {"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": params})
        try:
            await until(lambda: any(b"notifications/message" in event.get("body", b"") for event in events))
            sid = launch_store[0]
            if exit_kind == "reply_handoff":
                finish(sid)
                await asyncio.wait_for(entered_reply.wait(), 2)
            await queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 2)
            if exit_kind == "complete":
                finish(sid)
            before = spindle._get_spool_path(sid).read_bytes()
            release_reply.set()
            await asyncio.wait_for(reply_done.wait(), 2)
        finally:
            release_reply.set()
            for sid in launch_store:
                if spindle._read_spool(sid)["status"] == "running" and exit_kind != "deadline":
                    finish(sid)
    assert spindle._get_spool_path(sid).read_bytes() == before
    assert not [r for r in caplog.records if r.levelno >= 40], "final reply on a disconnected stream logged an error"


@pytest.mark.parametrize(
    "exception", [spindle.ClosedResourceError, spindle.BrokenResourceError, ConnectionError, ValueError]
)
async def test_reply_guard_does_not_swallow_tool_failures(monkeypatch, exception):
    def fail(*args):
        raise exception("launch failed")

    monkeypatch.setattr(spindle, "_respin_sync", fail)
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("respin", {"session_id": "old", "prompt": "work"}, raise_on_error=False)
    assert response.is_error
    assert "launch failed" in response.content[0].text
