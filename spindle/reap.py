"""Read-only, addressable views over Spindle's existing saved captures.

The reader deliberately has no dependency on Spindle's launch/finalization
machinery.  It observes one fixed prefix of each file it opens and builds an
ephemeral view; it never writes an index or asks a provider for more history.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

REAP_SCHEMA = "spindle.reap/v1"
DEFAULT_CONTENT_CHARS = 20_000
DEFAULT_RESPONSE_CHARS = 50_000
TREE_PREVIEW_CHARS = 160
TREE_PREVIEW_ITEMS = 3
MAX_RETURNED_ENTRIES = 200
_TERMINAL_STATUSES = {"complete", "error", "timeout", "cancelled", "abandoned"}
_FILTERS = {"user", "agent", "messages", "tool-calls", "tool-results"}
_RUN_ADDRESS = re.compile(r"^run:([^/]+)(?:/(.*))?$")


@dataclass
class _Issue:
    code: str
    message: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class _RawEvent:
    index: int
    value: Any
    raw_text: str
    parse_error: Optional[str] = None


@dataclass
class _Entry:
    address: str
    parent: str
    kind: str
    position: tuple[int, int]
    content: str
    source_address: Optional[str]
    role: Optional[str] = None
    tool_name: Optional[str] = None
    provider_id: Optional[str] = None
    call_address: Optional[str] = None
    result_addresses: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Capture:
    source: Optional[str]
    observed_bytes: int
    parsed_prefix_bytes: int
    ignored_trailing_bytes: int
    format: str
    events: list[_RawEvent]
    issues: list[_Issue]


@dataclass
class _Run:
    spool_id: str
    record: Optional[dict[str, Any]]
    record_error: Optional[str]
    capture: _Capture
    entries: list[_Entry]
    raw_entries: list[_Entry]
    support: str
    support_note: str
    issues: list[_Issue]

    @property
    def address(self) -> str:
        return f"run:{self.spool_id}"

    def all_entries(self) -> list[_Entry]:
        return [*self.entries, *self.raw_entries]


def _read_observed_prefix(path: Path) -> tuple[bytes, int]:
    """Read at most the size observed after opening ``path``.

    Appends after fstat are deliberately excluded from this view.  A concurrent
    truncate can produce fewer bytes, which is still a consistent prefix of
    what this reader actually observed and never gets padded or guessed.
    """
    with path.open("rb") as stream:
        observed_size = os.fstat(stream.fileno()).st_size
        data = stream.read(observed_size)
    return data, len(data)


def _safe_store_file(store: Path, name: str) -> Optional[Path]:
    path = store / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if not path.resolve().is_relative_to(store.resolve()):
            return None
    except OSError:
        return None
    return path


def _read_record(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        data, _ = _read_observed_prefix(path)
    except OSError as exc:
        return None, f"could not read spool record: {exc}"
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"malformed spool record: {exc}"
    if not isinstance(value, dict):
        return None, f"malformed spool record: expected a JSON object, found {type(value).__name__}"
    return value, None


def _record_paths(store: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    if not store.is_dir():
        return paths
    for path in store.glob("*.json"):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        paths[path.stem] = path
    return paths


def _decode_array_prefix(text: str) -> tuple[list[_RawEvent], int, list[_Issue]]:
    """Decode complete values from a possibly growing top-level JSON array."""
    decoder = json.JSONDecoder()
    events: list[_RawEvent] = []
    issues: list[_Issue] = []
    cursor = 1
    last_complete = cursor
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text) and text[cursor] == "]":
            cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            return events, cursor, issues
        if cursor < len(text) and text[cursor] == ",":
            cursor += 1
            continue
        start = cursor
        try:
            value, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            break
        raw = text[start:end]
        events.append(_RawEvent(len(events), value, raw))
        cursor = end
        last_complete = end
    if text[last_complete:].strip():
        issues.append(
            _Issue(
                "partial_json_array",
                "capture ends during a JSON array value; only complete preceding events are shown",
            )
        )
    return events, last_complete, issues


def _decode_utf8(data: bytes, *, terminal: bool) -> tuple[str, list[tuple[int, int]], bool]:
    """Replacement-decode while retaining original byte boundaries after repairs.

    A running prefix can stop inside a valid codepoint. Keep its placeholder
    for framing, but do not mistake that unfinished suffix for corrupt bytes.
    """
    pieces: list[str] = []
    repairs: list[tuple[int, int]] = []
    byte_offset = char_offset = 0
    invalid = False
    while byte_offset < len(data):
        try:
            pieces.append(data[byte_offset:].decode("utf-8"))
            break
        except UnicodeDecodeError as exc:
            prefix = exc.object[: exc.start].decode("utf-8")
            pieces.extend((prefix, "\ufffd"))
            char_offset += len(prefix) + 1
            byte_offset += exc.end
            repairs.append((char_offset, byte_offset))
            if terminal or exc.reason != "unexpected end of data":
                invalid = True
    return "".join(pieces), repairs, invalid


def _original_byte_offset(text: str, position: int, repairs: list[tuple[int, int]]) -> int:
    char_start = byte_start = 0
    for char_end, byte_end in repairs:
        if char_end > position:
            break
        char_start, byte_start = char_end, byte_end
    return byte_start + len(text[char_start:position].encode("utf-8"))


def _decode_capture(data: bytes, *, terminal: bool) -> _Capture:
    issues: list[_Issue] = []
    text, repairs, invalid = _decode_utf8(data, terminal=terminal)
    if invalid:
        issues.append(_Issue("invalid_utf8", "capture contains invalid UTF-8; replacement characters are shown"))

    stripped = text.lstrip()
    leading = len(text) - len(stripped)
    if not stripped:
        return _Capture(None, len(data), len(data), 0, "empty", [], issues)

    if stripped.startswith("["):
        events, parsed_chars, array_issues = _decode_array_prefix(stripped)
        issues.extend(array_issues)
        trailing = stripped[parsed_chars:]
        if terminal and trailing.strip():
            events.append(
                _RawEvent(
                    len(events),
                    None,
                    trailing,
                    "terminal JSON array has malformed or trailing captured material",
                )
            )
            issues = [issue for issue in issues if issue.code != "partial_json_array"]
            issues.append(
                _Issue(
                    "malformed_capture_record",
                    "terminal JSON-array capture contains malformed trailing material, available as raw evidence",
                    "error" if not events[:-1] else "warning",
                )
            )
        consumed = _original_byte_offset(text, leading + parsed_chars, repairs)
        ignored = len(data) - consumed
        return _Capture(None, len(data), len(data) - ignored, ignored, "json-array", events, issues)

    # A complete top-level JSON value is common for Gemini and legacy Claude.
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        values = value if isinstance(value, list) else [value]
        events = [
            _RawEvent(i, item, text if len(values) == 1 else json.dumps(item, ensure_ascii=False))
            for i, item in enumerate(values)
        ]
        return _Capture(None, len(data), len(data), 0, "json", events, issues)

    # NDJSON is framed only by LF (optionally preceded by CR), not by Unicode
    # line separators inside JSON strings. Count original bytes so replacement
    # decoding cannot inflate the ignored suffix or move its source boundary.
    # A final non-LF fragment is withheld while running unless it parses whole.
    lines = data.split(b"\n")
    events: list[_RawEvent] = []
    consumed_bytes = 0
    for line_no, line in enumerate(lines):
        complete_line = line_no < len(lines) - 1
        framed_bytes = len(line) + int(complete_line)
        raw = (line.removesuffix(b"\r") if complete_line else line).decode("utf-8", errors="replace")
        if not raw.strip():
            consumed_bytes += framed_bytes
            continue
        if not complete_line and not terminal:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                issues.append(
                    _Issue(
                        "partial_trailing_record",
                        "capture has an incomplete trailing record; it is withheld until a later read",
                    )
                )
                break
        try:
            parsed = json.loads(raw)
            events.append(_RawEvent(len(events), parsed, raw))
        except json.JSONDecodeError as exc:
            events.append(_RawEvent(len(events), None, raw, str(exc)))
            issues.append(
                _Issue(
                    "malformed_capture_record",
                    f"capture record at source line {line_no + 1} is malformed and is available as raw evidence",
                )
            )
        consumed_bytes += framed_bytes
    ignored = len(data) - consumed_bytes
    capture_format = "ndjson" if events else "unparsed"
    if terminal and events and not any(event.parse_error is None for event in events):
        for issue in issues:
            if issue.code == "malformed_capture_record":
                issue.severity = "error"
    return _Capture(None, len(data), len(data) - ignored, ignored, capture_format, events, issues)


def _capture_for_run(store: Path, spool_id: str, record: Optional[dict[str, Any]]) -> _Capture:
    status = record.get("status") if record else None
    terminal = status in _TERMINAL_STATUSES
    candidates = [f"{spool_id}.stdout", f"transcripts/{spool_id}.txt"]
    empty_capture: Optional[_Capture] = None
    for relative in candidates:
        path = _safe_store_file(store, relative)
        if path is None:
            continue
        try:
            data, observed = _read_observed_prefix(path)
        except OSError as exc:
            return _Capture(
                relative,
                0,
                0,
                0,
                "unreadable",
                [],
                [_Issue("capture_read_error", f"could not read {relative}: {exc}", "error")],
            )
        capture = _decode_capture(data, terminal=terminal)
        capture.source = relative
        capture.observed_bytes = observed
        if not data:
            empty_capture = capture
            continue
        return capture
    if empty_capture is not None:
        return empty_capture
    return _Capture(
        None,
        0,
        0,
        0,
        "missing",
        [],
        [_Issue("missing_capture", "no stdout or saved transcript capture exists for this spool")],
    )


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _block_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
            else:
                texts.append(_render(item))
        return "\n".join(texts)
    return _render(value)


def _part_address(spool_id: str, event_index: int, part_index: int) -> str:
    return f"run:{spool_id}/events/{event_index}/parts/{part_index}"


def _event_address(spool_id: str, event_index: int) -> str:
    return f"run:{spool_id}/events/{event_index}"


def _add_part(
    entries: list[_Entry],
    spool_id: str,
    event_index: int,
    part_index: int,
    kind: str,
    content: Any,
    **metadata: Any,
) -> _Entry:
    entry = _Entry(
        address=_part_address(spool_id, event_index, part_index),
        parent=f"run:{spool_id}/{kind if kind in {'prompt', 'final'} else _collection_for_kind(kind)}",
        kind=kind,
        position=(event_index, part_index),
        content=_render(content),
        source_address=_event_address(spool_id, event_index),
        **metadata,
    )
    entries.append(entry)
    return entry


def _collection_for_kind(kind: str) -> str:
    return {
        "message": "messages",
        "tool_call": "tool-calls",
        "tool_result": "tool-results",
        "raw_event": "raw",
    }.get(kind, kind)


def _claude_entries(spool_id: str, events: list[_RawEvent]) -> list[_Entry]:
    entries: list[_Entry] = []
    for raw in events:
        event = raw.value
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role") or event.get("type")
        role = "agent" if role == "assistant" else "user" if role == "user" else None
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        for block_index, block in enumerate(blocks):
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "tool_use":
                    _add_part(
                        entries,
                        spool_id,
                        raw.index,
                        block_index,
                        "tool_call",
                        block.get("input", {}),
                        tool_name=str(block.get("name") or "unknown"),
                        provider_id=str(block["id"]) if block.get("id") is not None else None,
                    )
                    continue
                if block_type == "tool_result":
                    _add_part(
                        entries,
                        spool_id,
                        raw.index,
                        block_index,
                        "tool_result",
                        _block_text(block.get("content", "")),
                        provider_id=(str(block["tool_use_id"]) if block.get("tool_use_id") is not None else None),
                        details={"is_error": block.get("is_error")} if "is_error" in block else {},
                    )
                    continue
                if block_type != "text":
                    continue
                text = block.get("text", "")
            else:
                text = block
            # A Claude user event made only of tool_result blocks is deliberately
            # absent from conversational messages.  This is the key role
            # normalization that prevents command output becoming human speech.
            if role in {"user", "agent"} and isinstance(text, str) and text:
                _add_part(
                    entries,
                    spool_id,
                    raw.index,
                    block_index,
                    "message",
                    text,
                    role=role,
                    details={key: event[key] for key in ("isReplay", "isSynthetic") if key in event},
                )
    return entries


_CODEX_NON_TOOL_ITEMS = {"agent_message", "reasoning", "error"}


def _codex_tool_name(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "tool")
    if item_type == "mcp_tool_call":
        server = item.get("server") or item.get("server_name")
        name = item.get("tool") or item.get("name") or "unknown"
        return f"{server}.{name}" if server else str(name)
    return str(item.get("name") or item_type)


def _codex_call_content(item: dict[str, Any]) -> Any:
    if "command" in item:
        return item["command"]
    for key in ("arguments", "input", "query", "changes"):
        if key in item:
            return item[key]
    return {k: v for k, v in item.items() if k not in {"id", "type", "status", "aggregated_output", "output"}}


def _codex_result_content(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    result: dict[str, Any] = {}
    for key in ("aggregated_output", "output", "result", "error", "status", "exit_code"):
        if key in item:
            result[key] = item[key]
    return result or None


def _codex_entries(spool_id: str, events: list[_RawEvent]) -> list[_Entry]:
    entries: list[_Entry] = []
    calls: dict[str, _Entry] = {}
    messages: dict[str, _Entry] = {}
    for raw in events:
        event = raw.value
        if not isinstance(event, dict) or not isinstance(event.get("item"), dict):
            continue
        item = event["item"]
        item_type = item.get("type")
        provider_id = str(item["id"]) if item.get("id") is not None else None
        event_type = event.get("type")
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            identity = provider_id or f"{raw.index}:0"
            if identity not in messages and (event_type == "item.completed" or item.get("text")):
                messages[identity] = _add_part(
                    entries,
                    spool_id,
                    raw.index,
                    0,
                    "message",
                    item["text"],
                    role="agent",
                    provider_id=provider_id,
                )
            elif identity in messages and event_type == "item.completed":
                messages[identity].content = item["text"]
                messages[identity].details["completed_source_address"] = _event_address(spool_id, raw.index)
            continue
        if item_type in _CODEX_NON_TOOL_ITEMS or not item_type:
            continue
        identity = provider_id or f"event:{raw.index}"
        part_index = 0
        if identity not in calls:
            calls[identity] = _add_part(
                entries,
                spool_id,
                raw.index,
                part_index,
                "tool_call",
                _codex_call_content(item),
                tool_name=_codex_tool_name(item),
                provider_id=provider_id,
            )
            part_index += 1
        if event_type == "item.completed":
            result = _codex_result_content(item)
            if result is not None:
                _add_part(
                    entries,
                    spool_id,
                    raw.index,
                    part_index,
                    "tool_result",
                    result,
                    provider_id=provider_id,
                )
    return entries


def _kimi_entries(spool_id: str, events: list[_RawEvent]) -> list[_Entry]:
    entries: list[_Entry] = []
    for raw in events:
        event = raw.value
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        content = event.get("content")
        part = 0
        if role in {"assistant", "user"}:
            normalized_role = "agent" if role == "assistant" else "user"
            blocks = content if isinstance(content, list) else [content]
            for block_index, block in enumerate(blocks):
                if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call"}:
                    _add_part(
                        entries,
                        spool_id,
                        raw.index,
                        block_index,
                        "tool_call",
                        block.get("input", block.get("arguments", {})),
                        tool_name=str(block.get("name") or "unknown"),
                        provider_id=str(block["id"]) if block.get("id") is not None else None,
                    )
                    continue
                if isinstance(block, dict):
                    if block.get("type") != "text":
                        continue
                    text = block.get("text")
                else:
                    text = block
                if isinstance(text, str) and text:
                    _add_part(
                        entries,
                        spool_id,
                        raw.index,
                        block_index,
                        "message",
                        text,
                        role=normalized_role,
                    )
            part = len(blocks)
        tool_calls = event.get("tool_calls")
        if isinstance(tool_calls, list):
            for index, call in enumerate(tool_calls, start=part):
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else call
                _add_part(
                    entries,
                    spool_id,
                    raw.index,
                    index,
                    "tool_call",
                    function.get("arguments", function.get("input", {})),
                    tool_name=str(function.get("name") or call.get("name") or "unknown"),
                    provider_id=(str(call["id"]) if call.get("id") is not None else None),
                )
        if role == "tool":
            _add_part(
                entries,
                spool_id,
                raw.index,
                0,
                "tool_result",
                _block_text(content),
                tool_name=str(event["name"]) if event.get("name") is not None else None,
                provider_id=(
                    str(event.get("tool_call_id") or event.get("tool_use_id"))
                    if event.get("tool_call_id") is not None or event.get("tool_use_id") is not None
                    else None
                ),
            )
    return entries


def _link_calls_and_results(entries: list[_Entry]) -> None:
    calls: dict[str, _Entry] = {}
    for entry in entries:
        if entry.kind == "tool_call" and entry.provider_id is not None:
            calls.setdefault(entry.provider_id, entry)
    for entry in entries:
        if entry.kind != "tool_result" or entry.provider_id is None:
            continue
        call = calls.get(entry.provider_id)
        if call is None:
            continue
        entry.call_address = call.address
        if entry.address not in call.result_addresses:
            call.result_addresses.append(entry.address)


def _build_run(store: Path, spool_id: str, record_path: Path) -> _Run:
    record, record_error = _read_record(record_path)
    capture = _capture_for_run(store, spool_id, record)
    issues = list(capture.issues)
    if record_error:
        issues.insert(0, _Issue("malformed_record", record_error, "error"))
    elif record and record.get("id") not in {None, spool_id}:
        issues.append(
            _Issue(
                "record_id_mismatch",
                f"record field id={record.get('id')!r} does not match filename handle {spool_id!r}",
                "error",
            )
        )

    harness = str(record.get("harness", "claude-code")) if record else "unknown"
    if harness == "codex":
        entries = _codex_entries(spool_id, capture.events)
        support = "normalized"
        support_note = "Codex NDJSON messages and tool items are normalized; other event types remain under raw."
    elif harness == "claude-code":
        entries = _claude_entries(spool_id, capture.events)
        recognized = any(
            isinstance(raw.value, dict) and isinstance(raw.value.get("message"), dict) for raw in capture.events
        )
        support = "normalized" if recognized else "partial"
        support_note = (
            "Claude message content blocks and tool-use/result IDs are normalized."
            if recognized
            else "No Claude message event shape was captured; prompt/final and raw saved evidence remain available."
        )
    elif harness == "kimi":
        entries = _kimi_entries(spool_id, capture.events)
        support = "normalized"
        support_note = "Kimi role messages and recognized tool-call/result shapes are normalized."
    elif harness == "gemini":
        entries = []
        support = "raw-only"
        support_note = "Gemini's saved JSON does not contain a stable conversation event stream; use raw and final."
    else:
        entries = []
        support = "raw-only"
        support_note = f"Harness {harness!r} has no role classifier; captured evidence remains available under raw."

    root = f"run:{spool_id}"
    if record and isinstance(record.get("prompt"), str):
        entries.append(
            _Entry(
                address=f"{root}/prompt",
                parent=f"{root}/messages",
                kind="message",
                position=(-1, 0),
                content=record["prompt"],
                source_address=f"{root}/prompt",
                role="user",
                details={"source": "spool record prompt"},
            )
        )
    final_value: Any = record.get("result") if record else None
    if final_value is None and harness == "claude-code":
        for raw in reversed(capture.events):
            if isinstance(raw.value, dict) and raw.value.get("type") == "result" and "result" in raw.value:
                final_value = raw.value["result"]
                break
    if final_value is not None:
        entries.append(
            _Entry(
                address=f"{root}/final",
                parent=root,
                kind="final",
                position=(len(capture.events) + 1, 0),
                content=_render(final_value),
                source_address=f"{root}/final",
                details={"source": "spool record result" if record and record.get("result") is not None else "capture"},
            )
        )
    entries.sort(key=lambda item: item.position)
    _link_calls_and_results(entries)

    raw_entries: list[_Entry] = []
    for raw in capture.events:
        if raw.parse_error:
            content = raw.raw_text
        else:
            content = raw.raw_text
        raw_entries.append(
            _Entry(
                address=_event_address(spool_id, raw.index),
                parent=f"{root}/raw",
                kind="raw_event",
                position=(raw.index, -1),
                content=content,
                source_address=_event_address(spool_id, raw.index),
                details={"parse_error": raw.parse_error} if raw.parse_error else {},
            )
        )
    return _Run(
        spool_id,
        record,
        record_error,
        capture,
        entries,
        raw_entries,
        support,
        support_note,
        issues,
    )


def _entry_summary(entry: _Entry) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "address": entry.address,
        "kind": entry.kind,
        "preview": entry.content[:TREE_PREVIEW_CHARS],
        "chars": len(entry.content),
    }
    if len(entry.content) > TREE_PREVIEW_CHARS:
        summary["preview_truncated"] = True
    for key in ("role", "tool_name", "provider_id"):
        value = getattr(entry, key)
        if value is not None:
            summary[key] = value
    return summary


def _capture_facts(run: _Run) -> dict[str, Any]:
    return {
        "source": run.capture.source,
        "format": run.capture.format,
        "observed_bytes": run.capture.observed_bytes,
        "parsed_prefix_bytes": run.capture.parsed_prefix_bytes,
        "ignored_trailing_bytes": run.capture.ignored_trailing_bytes,
        "raw_event_count": len(run.raw_entries),
    }


def _run_outline(run: _Run) -> dict[str, Any]:
    record = run.record or {}
    groups = {
        "messages": [item for item in run.entries if item.kind == "message"],
        "tool-calls": [item for item in run.entries if item.kind == "tool_call"],
        "tool-results": [item for item in run.entries if item.kind == "tool_result"],
        "raw": run.raw_entries,
    }
    collections = []
    for name, entries in groups.items():
        collections.append(
            {
                "address": f"{run.address}/{name}",
                "count": len(entries),
                "preview": [_entry_summary(entry) for entry in entries[:TREE_PREVIEW_ITEMS]],
                "preview_omitted": max(0, len(entries) - TREE_PREVIEW_ITEMS),
            }
        )
    direct = [item for item in run.entries if item.address in {f"{run.address}/prompt", f"{run.address}/final"}]
    return {
        "address": run.address,
        "spool_id": run.spool_id,
        "harness": record.get("harness", "unknown"),
        "status": record.get("status", "unknown"),
        "created_at": record.get("created_at"),
        "session_id": record.get("session_id"),
        "support": run.support,
        "support_note": run.support_note,
        "capture": _capture_facts(run),
        "nodes": [_entry_summary(entry) for entry in direct],
        "collections": collections,
        "issues": [issue.as_dict() for issue in run.issues],
    }


def _scope(handle: str, session: bool, runs: list[_Run], session_id: Optional[str]) -> dict[str, Any]:
    if session:
        statement = (
            f"exactly {len(runs)} readable saved Spindle run(s) whose records carry session_id={session_id!r}; "
            "provider history not captured by Spindle is not included"
        )
        return {
            "view": "session",
            "address": f"session:{session_id}",
            "requested_handle": handle,
            "session_id": session_id,
            "statement": statement,
            "run_addresses": [run.address for run in runs],
        }
    return {
        "view": "spool",
        "address": runs[0].address,
        "requested_handle": handle,
        "statement": "one saved Spindle run; provider history outside this run's saved capture is not included",
        "run_addresses": [runs[0].address],
    }


def _error(handle: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema": REAP_SCHEMA,
        "ok": False,
        "requested_handle": handle,
        "error": {"code": code, "message": message},
    }


def _resolve_runs(
    store: Path, handle: str, session: bool
) -> tuple[list[_Run], Optional[str], Optional[dict[str, Any]]]:
    paths = _record_paths(store)
    if not paths:
        return [], None, _error(handle, "empty_store", f"no spool records exist in {store}")
    if not session:
        path = paths.get(handle)
        if path is None:
            return [], None, _error(handle, "unknown_spool", f"no saved spool record has handle {handle!r}")
        return [_build_run(store, handle, path)], None, None

    session_id: Optional[str] = None
    if handle in paths:
        record, error = _read_record(paths[handle])
        if error:
            return [], None, _error(handle, "malformed_record", error)
        session_id = record.get("session_id") if record else None
        if not session_id:
            return (
                [],
                None,
                _error(
                    handle,
                    "missing_session_id",
                    f"spool {handle!r} has no session_id; its explicit session view cannot be formed",
                ),
            )
    else:
        session_id = handle

    matching: list[tuple[str, Path, dict[str, Any]]] = []
    for spool_id, path in paths.items():
        record, error = _read_record(path)
        if error or not record:
            continue
        if record.get("session_id") == session_id:
            matching.append((spool_id, path, record))
    if not matching:
        return (
            [],
            session_id,
            _error(
                handle,
                "unknown_session",
                f"no readable saved spool records have session_id={session_id!r}",
            ),
        )
    matching.sort(key=lambda item: (str(item[2].get("created_at") or ""), item[0]))
    return [_build_run(store, spool_id, path) for spool_id, path, _ in matching], session_id, None


def _normalize_values(value: Optional[str | Iterable[str]]) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in values:
        result.extend(piece.strip() for piece in str(item).split(",") if piece.strip())
    return result


def _entries_for_collection(run: _Run, collection: str) -> list[_Entry]:
    if collection == "messages":
        return [item for item in run.entries if item.kind == "message"]
    if collection == "tool-calls":
        return [item for item in run.entries if item.kind == "tool_call"]
    if collection == "tool-results":
        return [item for item in run.entries if item.kind == "tool_result"]
    if collection == "raw":
        return list(run.raw_entries)
    raise ValueError(collection)


def _select_exact(address: str, run_by_id: dict[str, _Run]) -> tuple[list[_Entry], Optional[str]]:
    match = _RUN_ADDRESS.fullmatch(address)
    if not match:
        return [], (
            f"invalid address {address!r}; copy a fully qualified run:<spool>/... address from the reap outline"
        )
    spool_id, suffix = match.groups()
    run = run_by_id.get(spool_id)
    if run is None:
        return [], f"address {address!r} is outside this reap scope"
    if suffix in {None, ""}:
        return list(run.entries), None
    if suffix in {"messages", "tool-calls", "tool-results", "raw"}:
        return _entries_for_collection(run, suffix), None
    entries = {item.address: item for item in run.all_entries()}
    entry = entries.get(address)
    if entry is None:
        return [], (
            f"address {address!r} does not exist in the observed capture; use {run.address} to refresh its outline"
        )
    return [entry], None


def _select_range(selector: str, run_by_id: dict[str, _Run]) -> tuple[list[_Entry], Optional[str]]:
    if selector.count("..") != 1:
        return [], f"invalid range {selector!r}; use <full-address>..<full-address>"
    start_text, end_text = selector.split("..", 1)
    start_items, start_error = _select_exact(start_text, run_by_id)
    end_items, end_error = _select_exact(end_text, run_by_id)
    if start_error or end_error:
        return [], start_error or end_error
    if len(start_items) != 1 or len(end_items) != 1:
        return [], f"range endpoints must be individual entry addresses, not collections: {selector!r}"
    start, end = start_items[0], end_items[0]
    start_run = _RUN_ADDRESS.fullmatch(start.address).group(1)  # type: ignore[union-attr]
    end_run = _RUN_ADDRESS.fullmatch(end.address).group(1)  # type: ignore[union-attr]
    if start_run != end_run:
        return [], f"range endpoints must belong to the same run: {selector!r}"
    raw_family = start.kind == "raw_event"
    if raw_family != (end.kind == "raw_event"):
        return [], f"range endpoints must both be raw events or both be normalized entries: {selector!r}"
    if start.position > end.position:
        return [], f"range start follows range end in the captured source: {selector!r}"
    run = run_by_id[start_run]
    candidates = run.raw_entries if raw_family else run.entries
    return [item for item in candidates if start.position <= item.position <= end.position], None


def _filter_entries(entries: list[_Entry], filter_name: str, tool_name: Optional[str]) -> list[_Entry]:
    if filter_name == "user":
        return [item for item in entries if item.kind == "message" and item.role == "user"]
    if filter_name == "agent":
        return [item for item in entries if item.kind == "message" and item.role == "agent"]
    if filter_name == "messages":
        return [item for item in entries if item.kind == "message"]
    if filter_name == "tool-calls":
        return [
            item for item in entries if item.kind == "tool_call" and (tool_name is None or item.tool_name == tool_name)
        ]
    if filter_name == "tool-results":
        return [item for item in entries if item.kind == "tool_result"]
    raise ValueError(filter_name)


def _render_entry(entry: _Entry, *, offset: int, limit: Optional[int], full: bool) -> dict[str, Any]:
    total = len(entry.content)
    if full:
        start, end = 0, total
    else:
        start = min(offset, total)
        effective_limit = DEFAULT_CONTENT_CHARS if limit is None else limit
        end = min(total, start + effective_limit)
    rendered: dict[str, Any] = {
        "address": entry.address,
        "parent": entry.parent,
        "kind": entry.kind,
        "source_address": entry.source_address,
        "content": entry.content[start:end],
    }
    for key in ("role", "tool_name", "provider_id", "call_address"):
        value = getattr(entry, key)
        if value is not None:
            rendered[key] = value
    if entry.result_addresses:
        rendered["result_addresses"] = entry.result_addresses
    if entry.details:
        rendered["details"] = entry.details
    if start != 0 or end != total:
        rendered["truncation"] = {
            "shown_chars": [start, end],
            "total_chars": total,
            "omitted_before": start,
            "omitted_after": total - end,
            "retrieve": {
                "address": entry.address,
                "offset": end,
                "limit": effective_limit,
            }
            if end < total
            else None,
        }
    return rendered


def render_response(response: dict[str, Any]) -> str:
    """The shared wire representation, including JSON escaping in size counts.

    Keep ordinary Unicode readable, but escape lone surrogates from provider
    JSON so both CLI stdout and MCP transports can encode this as UTF-8.
    """
    return json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8", errors="backslashreplace").decode("utf-8")


def _budget_error(handle: str) -> dict[str, Any]:
    return _error(
        handle,
        "response_metadata_exceeds_budget",
        "response metadata cannot fit the read budget; narrow the scope/selection, "
        "or retrieve selected addresses with full=True (--full) to bypass the character budget",
    )


def reap_store(
    store: Path,
    handle: str,
    *,
    addresses: Optional[str | Iterable[str]] = None,
    filters: Optional[str | Iterable[str]] = None,
    tool_name: Optional[str] = None,
    session: bool = False,
    after: Optional[str] = None,
    offset: int = 0,
    limit: Optional[int] = None,
    full: bool = False,
    max_chars: int = DEFAULT_RESPONSE_CHARS,
) -> dict[str, Any]:
    """Build an addressable, read-only view of saved spool evidence."""
    if offset < 0:
        return _error(handle, "invalid_offset", "offset must be zero or greater")
    if limit is not None and limit <= 0:
        return _error(handle, "invalid_limit", "limit must be a positive number of characters")
    if full and (offset or limit is not None):
        return _error(handle, "incompatible_paging", "full cannot be combined with offset or limit")

    selectors = _normalize_values(addresses)
    filter_names = _normalize_values(filters)
    invalid_filters = [name for name in filter_names if name not in _FILTERS]
    if invalid_filters:
        return _error(
            handle,
            "invalid_filter",
            f"unknown filter(s) {invalid_filters!r}; choose from {sorted(_FILTERS)}",
        )
    if tool_name is not None and filter_names and "tool-calls" not in filter_names:
        return _error(handle, "tool_filter_without_calls", "tool_name requires the tool-calls filter")
    if tool_name is not None and not filter_names:
        filter_names = ["tool-calls"]

    runs, session_id, resolution_error = _resolve_runs(Path(store), handle, session)
    if resolution_error:
        return resolution_error
    scope = _scope(handle, session, runs, session_id)
    fatal_issues = [issue for run in runs for issue in run.issues if issue.severity == "error"]

    if not selectors and not filter_names:
        if after is not None:
            return _error(handle, "after_requires_selection", "after requires at least one address or filter")
        if full or offset or limit is not None:
            return _error(
                handle,
                "paging_requires_selection",
                "full, offset, and limit apply to selected entries; provide an address or filter first",
            )
        outline = {
            "schema": REAP_SCHEMA,
            "ok": not fatal_issues,
            "mode": "outline",
            "scope": scope,
            "address_scheme": {
                "run": "run:<spool-id>",
                "collections": ["messages", "tool-calls", "tool-results", "raw"],
                "entry": "run:<spool-id>/events/<stable-source-position>/parts/<stable-part-position>",
                "raw_event": "run:<spool-id>/events/<stable-source-position>",
                "direct": ["prompt", "final"],
                "range": "<full-entry-address>..<full-entry-address>",
            },
            "runs": [_run_outline(run) for run in runs],
        }
        return outline if len(render_response(outline)) <= max_chars else _budget_error(handle)

    run_by_id = {run.spool_id: run for run in runs}
    selected: list[_Entry] = []
    for selector in selectors:
        if ".." in selector:
            items, error = _select_range(selector, run_by_id)
        else:
            items, error = _select_exact(selector, run_by_id)
        if error:
            return _error(handle, "invalid_address", error)
        selected.extend(items)
    if not selectors:
        selected = [entry for run in runs for entry in run.entries]

    # Union the explicit selections first, then narrow that set to entries
    # matching any requested filter. Filters must never add out-of-range items.
    deduplicated = {entry.address: entry for entry in selected}
    deduplicated_overlap = len(selected) - len(deduplicated)
    candidates = list(deduplicated.values())
    if filter_names:
        deduplicated = {
            entry.address: entry
            for filter_name in filter_names
            for entry in _filter_entries(candidates, filter_name, tool_name)
        }
    run_order = {run.spool_id: index for index, run in enumerate(runs)}

    def source_order(entry: _Entry) -> tuple[int, int, int, int]:
        spool_id = _RUN_ADDRESS.fullmatch(entry.address).group(1)  # type: ignore[union-attr]
        raw_rank = 0 if entry.kind == "raw_event" else 1
        return (run_order[spool_id], entry.position[0], entry.position[1], raw_rank)

    ordered = sorted(deduplicated.values(), key=source_order)
    entry_start = 0
    if after is not None:
        selected_addresses = [entry.address for entry in ordered]
        try:
            entry_start = selected_addresses.index(after) + 1
        except ValueError:
            return _error(
                handle,
                "invalid_after",
                f"after address {after!r} is not in this selection; repeat the same addresses and filters",
            )
    available_tools = sorted(
        {item.tool_name for run in runs for item in run.entries if item.kind == "tool_call" and item.tool_name}
    )
    response: dict[str, Any] = {
        "schema": REAP_SCHEMA,
        "ok": not fatal_issues,
        "mode": "selection",
        "scope": scope,
        "selection": {
            "addresses": selectors,
            "filters": filter_names,
            "tool_name": tool_name,
            "matched_entries": len(ordered),
            "returned_entries": 0,
            "entries_before_page": entry_start,
            "deduplicated_overlap": deduplicated_overlap,
            "omitted_entries": len(ordered) - entry_start,
            "available_tool_names": available_tools,
        },
        "entries": [],
        "runs": [
            {
                "address": run.address,
                "support": run.support,
                "support_note": run.support_note,
                "capture": _capture_facts(run),
                "issues": [issue.as_dict() for issue in run.issues],
            }
            for run in runs
        ],
    }

    def update_page() -> None:
        count = len(response["entries"])
        omitted = len(ordered) - entry_start - count
        response["selection"].update(returned_entries=count, omitted_entries=omitted)
        if count and omitted:
            last_address = response["entries"][-1]["address"]
            response["entry_truncation"] = {
                "reason": (
                    f"selection exceeds the {MAX_RETURNED_ENTRIES}-entry response bound"
                    if full
                    else f"selection exceeds the {MAX_RETURNED_ENTRIES}-entry or {max_chars}-character response bound"
                ),
                "last_returned_address": last_address,
                "retrieve": {
                    "after": last_address,
                    "instruction": "repeat the same addresses, filters and content controls with this stable after address",
                },
            }
        else:
            response.pop("entry_truncation", None)

    for entry in ordered[entry_start : entry_start + MAX_RETURNED_ENTRIES]:
        response["entries"].append(_render_entry(entry, offset=offset, limit=limit, full=full))
        update_page()
        if full or len(render_response(response)) <= max_chars:
            continue
        if len(response["entries"]) > 1:
            response["entries"].pop()
            update_page()
            break

        # One entry alone can overflow through escaping or an explicit limit.
        # Find a fitting, nonempty content chunk; its offset cursor must advance.
        # If metadata alone prevents progress, return an actionable error, not
        # an empty page with a cursor that repeats forever. Explicit full is the
        # escape hatch for unusually large provider metadata as well as content.
        low, high = 1, len(response["entries"][0]["content"]) - 1
        fitting = None
        while low <= high:
            count = (low + high) // 2
            response["entries"][0] = _render_entry(entry, offset=offset, limit=count, full=False)
            if len(render_response(response)) <= max_chars:
                fitting = response["entries"][0]
                low = count + 1
            else:
                high = count - 1
        if fitting is None:
            return _budget_error(handle)
        response["entries"][0] = fitting
        break

    if not full and len(render_response(response)) > max_chars:
        return _budget_error(handle)
    return response
