"""Compact presentation of the reader's resolved evidence and navigation.

This module does no loading or selection. Requests in the response are also
the source of the copyable CLI commands, including filters and continuations.
"""

from __future__ import annotations

import shlex
from collections import Counter
from typing import Any


def safe_text(text: str) -> str:
    """Make terminal controls visible without losing ordinary Unicode."""
    return "".join(
        f"\\x{ord(char):02x}"
        if (ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159
        else f"\\u{ord(char):04x}"
        if 0xD800 <= ord(char) <= 0xDFFF
        else char
        for char in text
    )


def _word(value: str) -> str:
    if safe_text(value) == value:
        return shlex.quote(value)
    # Bash/zsh ANSI-C quoting preserves control characters in a reusable
    # command without placing those characters directly on the terminal.
    quoted = "".join(
        f"\\u{ord(char):04x}" if 128 <= ord(char) <= 159 else safe_text(char)
        for char in value.replace("\\", "\\\\").replace("'", "\\'")
    )
    return "$'" + quoted + "'"


def address_text(address: str, request: dict[str, Any]) -> str:
    if not request.get("session"):
        return address.removeprefix(f"run:{request['handle']}/")
    return address


def command(request: dict[str, Any]) -> str:
    words = ["spindle", "reap", request["handle"]]
    addresses = request.get("addresses", [])
    if isinstance(addresses, str):
        addresses = [addresses]
    for selector in addresses:
        words.append("..".join(address_text(part, request) for part in selector.split("..")))
    if request.get("session"):
        words.append("--session")
    for name in request.get("filters", []):
        words.extend(("--filter", name))
    if request.get("tool_name") is not None:
        words.append("--tool=" + request["tool_name"])
    if request.get("outline"):
        words.append("--outline")
    if request.get("after"):
        words.extend(("--after", address_text(request["after"], request)))
    for key in ("offset", "limit"):
        if request.get(key) is not None and (key != "offset" or request[key]):
            words.extend((f"--{key}", str(request[key])))
    if request.get("full"):
        words.append("--full")
    return " ".join(_word(word) for word in words)


def _count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def _counts(counts: dict[str, int]) -> str:
    nouns = {
        "message": "message",
        "tool_call": "call",
        "tool_result": "result",
        "raw_event": "raw event",
        "final": "saved result",
    }
    return (
        ", ".join(
            _count(number, nouns[kind])
            for kind, number in counts.items()
            if number and not (kind == "tool_result" and number == counts.get("tool_call"))
        )
        or "0 entries"
    )


def _label(entry: dict[str, Any]) -> str:
    label = {
        "message": entry.get("role", "message"),
        "tool_call": "tool call",
        "tool_result": "tool result",
        "raw_event": "raw event",
        "final": "saved result",
    }[entry["kind"]]
    return label + (" " + entry["tool_name"] if entry.get("tool_name") else "")


def _excerpt(preview: str, truncated: bool = False) -> str:
    text = safe_text(preview)
    if len(text) <= 80 and not truncated:
        return text
    prefix = text[:79]
    if len(text) > 79 and not text[79].isspace() and " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + "…"


def _clues(row: dict[str, Any]) -> str:
    return " → ".join(
        f"{_label(clue)}: {_excerpt(clue['preview'], clue.get('preview_truncated', False))}"
        for clue in row.get("clues", [])
    )


def _notices(response: dict[str, Any]) -> list[str]:
    notices = []
    for run in response.get("runs", []):
        facts = []
        if run.get("status") in {"error", "timeout", "cancelled", "abandoned"} and response["mode"] == "selection":
            facts.append("status " + run["status"])
        if run["support"] != "normalized":
            facts.append(run["support"] + ": " + run["support_note"])
        facts.extend(dict.fromkeys(issue["message"] for issue in run["issues"]))
        if facts:
            notices.append(f"{run['address']}: " + "; ".join(facts))
    if not response.get("runs"):
        diagnostics = response["scope"]["diagnostics"]
        facts = [
            f"{_count(diagnostics[key], 'spool')} {noun}"
            for key, noun in (
                ("error_runs", "with read errors"),
                ("warning_runs", "with capture warnings"),
                ("missing_capture_runs", "with missing capture"),
            )
            if diagnostics[key]
        ]
        facts.extend(
            f"{_count(count, 'spool')} with {kind} capture"
            for kind, count in diagnostics["support_counts"].items()
            if kind != "normalized" and count
        )
        facts.extend(
            f"{_count(count, 'spool')} with status {status}"
            for status, count in diagnostics.get("abnormal_status_counts", {}).items()
            if count
        )
        if facts:
            notices.append("; ".join(facts) + ". Inspect: " + command(diagnostics["retrieve_outline"]))
    return notices


def render_text(response: dict[str, Any]) -> str:
    if "error" in response:
        return safe_text("Spindle: " + response["error"]["message"])
    request = response["presentation"]["request"]
    mode = response["mode"]
    lines: list[str] = []
    notices = _notices(response)
    if mode == "session-outline":
        scope = response["scope"]
        total = scope["matched_runs"]
        shown = len(response["runs"])
        counts: Counter[str] = Counter()
        for run in response["runs"]:
            counts.update(run["counts"])
        title = f"Session: {_count(total, 'saved spool')}"
        if shown != total:
            title += f"; showing {shown}"
        if scope["omitted_runs"]:
            title += f"; {scope['omitted_runs']} remaining"
        if shown:
            title += ("; this page: " if shown != total else "; ") + _counts(counts)
        lines.append(title)
        for run in response["runs"]:
            lines.append(f"{run['status']}; {_counts(run['counts'])} {run['spool_id']}")
            lines.append("  " + _clues(run))
        if response["runs"]:
            lines.extend(("", "Browse a spool: " + command({"handle": response["runs"][0]["spool_id"]})))
    elif mode == "selection-outline":
        selection = response["selection"]
        runs = response["runs"]
        title = runs[0]["status"] if len(runs) == 1 else "Selection"
        if response["presentation"].get("bare") and runs:
            title += "; " + _counts(runs[0]["counts"])
        title += "; " + _count(selection["matched_entries"], "selected entry", "selected entries")
        if selection["omitted_entries"]:
            title += f"; {selection['omitted_entries']} remaining"
        if len(runs) == 1:
            title += " " + runs[0]["spool_id"]
        lines.append(title)
        if request.get("filters"):
            lines.append(
                "Filters: "
                + ", ".join(request["filters"])
                + (f"; tool: {request['tool_name']}" if request.get("tool_name") is not None else "")
            )
        for group in response["groups"]:
            selector = "..".join(address_text(part, request) for part in group["selector"].split(".."))
            description = (
                _label(group["leaf"]) + f"; {group['chars']} chars" if "leaf" in group else _counts(group["counts"])
            )
            lines.append(f"{description} {selector}")
            lines.append("  " + _clues(group))
        if response["presentation"].get("bare") and runs:
            run = runs[0]
            if run.get("final"):
                lines.append(
                    f"Saved result: {run['final']['chars']} chars {address_text(run['final']['address'], request)}"
                )
            lines.append(
                f"Raw: {_count(run['capture']['raw_event_count'], 'event')} {address_text(run['address'] + '/raw', request)}"
            )
        if not response["groups"]:
            lines.append("No selected chronology entries.")
        else:
            first = response["groups"][0]
            lines.extend(("", "Read: " + command(first["read"]), "Zoom: " + command(first["zoom"])))
            lines.append("Replace the address/range with any row above; keep the filters.")
    else:
        exact = response["presentation"]["exact_leaf"]
        for entry in response["entries"]:
            if not exact:
                lines.append(f"{_label(entry)} {address_text(entry['address'], request)}")
            lines.append(entry["content"])
            truncation = entry.get("truncation")
            if truncation:
                start, end = truncation["shown_chars"]
                notice = (
                    f"{address_text(entry['address'], request)}: chars {start}..{end} of {truncation['total_chars']}"
                )
                if truncation["retrieve"]:
                    more = {
                        "handle": request["handle"],
                        "session": request.get("session", False),
                        "addresses": [entry["address"]],
                        "offset": end,
                        "limit": truncation["retrieve"]["limit"],
                    }
                    notice += "; more content: " + command(more)
                notices.append(notice)
            if not exact:
                lines.append("")
        if not response["entries"]:
            lines.append("No matching entries.")
    for key, noun in (("entry_truncation", "entries"), ("run_truncation", "saved spools")):
        if key in response:
            continuation = response[key]["retrieve"]
            remaining = (
                response.get("selection", {}).get("omitted_entries")
                if key == "entry_truncation"
                else response["scope"]["omitted_runs"]
            )
            notices.append(f"{remaining} more {noun}: " + command(continuation))
    # Never strip the body of an exact leaf: its whitespace is captured data.
    body = "\n".join(
        lines
        if mode == "selection" and response["presentation"].get("exact_leaf")
        else lines[:-1]
        if lines and lines[-1] == ""
        else lines
    )
    if notices:
        body += "\n\n" + "\n".join("Spindle: " + notice for notice in notices)
    return safe_text(body)
