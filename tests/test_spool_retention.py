"""Spool retention and the no-record error (audit S10).

Finished records used to be deleted 24 hours after creation, and a lookup on a
pruned record said only "Unknown spool_id" or "No spool found", which reads
exactly like a typo. friction-20261102-9lh0 (respin of a fixer twelve days into
an open fell) and friction-20261016-r0l4 (a result collected two days later)
both hit this. Records are now kept unless SPINDLE_SPOOL_RETENTION_HOURS sets a
limit, and a missing record says which of the two applies.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

import spindle

ENV = "SPINDLE_SPOOL_RETENTION_HOURS"

KEPT_NOTE = (
    "finished records are kept because SPINDLE_SPOOL_RETENTION_HOURS is not set, so check the ID "
    "(older Spindle versions deleted records 24 hours after creation)"
)


def pruned_note(window: str) -> str:
    return (
        f"finished spool records are pruned {window} after creation "
        "(SPINDLE_SPOOL_RETENTION_HOURS), so it may have been pruned"
    )


def unknown(sid: str, note: str = KEPT_NOTE) -> str:
    return f"Error: Unknown spool_id '{sid}': no record exists; {note}"


def no_handle(handle: str, note: str = KEPT_NOTE) -> str:
    return f"Error: No spool found for handle '{handle}': no record has that spool_id or session_id; {note}"


def tool(name):
    obj = getattr(spindle, name)
    return getattr(obj, "fn", obj)


@pytest.fixture
def no_limit(monkeypatch):
    """Pin the shipped default (no retention limit) whatever the shell exports."""
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", None)


def write_finished(sid: str, age: timedelta) -> None:
    spindle._write_spool(
        sid,
        {
            "id": sid,
            "status": "complete",
            "result": f"result of {sid}",
            "created_at": (datetime.now() - age).isoformat(),
        },
    )
    spindle._get_output_path(sid).write_text("captured stdout")
    spindle._get_stderr_path(sid).write_text("captured stderr")
    spindle._get_exit_path(sid).write_text("0")


def capture_paths(sid: str) -> list[Path]:
    return [
        spindle._get_spool_path(sid),
        spindle._get_output_path(sid),
        spindle._get_stderr_path(sid),
        spindle._get_exit_path(sid),
    ]


# --- the setting -----------------------------------------------------------


def test_default_is_no_limit(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert spindle.SPOOL_RETENTION_HOURS_DEFAULT is None
    assert spindle._spool_retention_hours() is None


@pytest.mark.parametrize(("raw", "hours"), [("48", 48.0), (" 72 ", 72.0), ("1.5", 1.5), ("1e12", 1e12)])
def test_limit_reads_positive_hours_from_env(monkeypatch, raw, hours):
    monkeypatch.setenv(ENV, raw)
    assert spindle._spool_retention_hours() == hours


@pytest.mark.parametrize("raw", ["two weeks", "0", "-6", "nan", "inf"])
def test_unusable_limit_keeps_records_and_says_so(monkeypatch, caplog, raw):
    monkeypatch.setenv(ENV, raw)
    with caplog.at_level(logging.WARNING, logger=spindle.logger.name):
        assert spindle._spool_retention_hours() is None
    assert ENV in caplog.text
    assert repr(raw) in caplog.text
    assert "keeping every finished spool record" in caplog.text


def test_empty_env_value_means_no_limit_without_a_warning(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "")
    with caplog.at_level(logging.WARNING, logger=spindle.logger.name):
        assert spindle._spool_retention_hours() is None
    assert ENV not in caplog.text


@pytest.mark.parametrize(
    ("hours", "text"),
    [(336, "14 days"), (24, "1 day"), (36, "36 hours"), (1, "1 hour"), (1.5, "1.5 hours"), (0.25, "0.25 hours")],
)
def test_limit_is_named_in_days_when_whole(monkeypatch, hours, text):
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", hours)
    assert spindle._spool_retention_window() == text


# --- the sweep -------------------------------------------------------------


def test_with_no_limit_old_finished_records_and_captures_are_kept(no_limit):
    for sid, age in (("two-days-old", timedelta(days=2)), ("year-old", timedelta(days=400))):
        write_finished(sid, age)
    spindle._write_spool("no-timestamp", {"id": "no-timestamp", "status": "error"})
    spindle._cleanup_old_spools()
    for sid in ("two-days-old", "year-old"):
        assert all(path.exists() for path in capture_paths(sid))
        assert spindle._read_spool(sid)["result"] == f"result of {sid}"
    assert spindle._read_spool("no-timestamp") is not None


def test_with_no_limit_the_sweep_reads_no_records(no_limit, monkeypatch):
    # Every CLI command runs this sweep at import; with nothing to prune it must
    # not take a lock on, or parse, each record in the store.
    write_finished("kept", timedelta(days=400))
    touched = []
    monkeypatch.setattr(spindle, "_spool_lock", lambda spool_id, **kwargs: touched.append(spool_id))
    spindle._cleanup_old_spools()
    assert touched == []


def test_finished_record_older_than_a_set_limit_is_pruned_with_its_captures(monkeypatch):
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", 48)
    write_finished("inside", timedelta(hours=47))
    write_finished("outside", timedelta(hours=49))
    spindle._cleanup_old_spools()
    assert all(path.exists() for path in capture_paths("inside"))
    assert not any(path.exists() for path in capture_paths("outside"))
    assert not spindle._get_lock_path("outside").exists()


def test_preservation_rules_still_override_a_set_limit(monkeypatch):
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", 1)
    old = (datetime.now() - timedelta(days=30)).isoformat()
    spindle._write_spool(
        "shard-kept", {"id": "shard-kept", "status": "error", "created_at": old, "shard_cleanup_preserved": True}
    )
    spindle._write_spool("ordinary", {"id": "ordinary", "status": "complete", "created_at": old})
    spindle._cleanup_old_spools()
    assert spindle._read_spool("shard-kept") is not None
    assert spindle._read_spool("ordinary") is None


def test_a_limit_too_large_for_datetime_keeps_everything(monkeypatch):
    # 1e12 hours overflows timedelta; the sweep must keep records, not crash
    # the module-load maintenance pass that every CLI command runs.
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", 1e12)
    spindle._write_spool("ancient", {"id": "ancient", "status": "complete", "created_at": "2000-01-01T00:00:00"})
    spindle._cleanup_old_spools()
    assert spindle._read_spool("ancient") is not None


@pytest.mark.parametrize("limit", [None, 24 * 14])
def test_orphaned_lock_sweep_keeps_its_own_24_hour_horizon(monkeypatch, limit):
    # Orphaned locks have no record to retain; their age check only protects a
    # spool whose JSON is not written yet, so it runs with or without a limit.
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", limit)
    stale_lock = spindle._get_lock_path("orphan")
    fresh_lock = spindle._get_lock_path("fresh-orphan")
    stale_lock.write_text("")
    fresh_lock.write_text("")
    stale = (datetime.now() - timedelta(hours=25)).timestamp()
    os.utime(stale_lock, (stale, stale))
    spindle._cleanup_old_spools()
    assert not stale_lock.exists()
    assert fresh_lock.exists()


def run_import(tmp_path, env_value):
    """Import spindle in a fresh process (which runs the CLI's sweep) against a seeded store."""
    home = tmp_path / "home"
    store = home / "spools"
    store.mkdir(parents=True)
    now = datetime.now()
    for sid, hours in (("thirty-hours", 30), ("fifty-hours", 50), ("fifty-days", 50 * 24)):
        (store / f"{sid}.json").write_text(
            json.dumps({"id": sid, "status": "complete", "created_at": (now - timedelta(hours=hours)).isoformat()})
        )
    env = dict(os.environ, SPINDLE_HOME=str(home))
    env.pop(ENV, None)
    if env_value is not None:
        env[ENV] = env_value
    proc = subprocess.run(
        [sys.executable, "-c", "import spindle; print(spindle.SPOOL_RETENTION_HOURS)"],
        capture_output=True,
        text=True,
        cwd=str(Path(spindle.__file__).parent.parent),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    kept = sorted(path.stem for path in store.glob("*.json"))
    return proc.stdout.strip().splitlines()[-1], kept


def test_import_time_sweep_keeps_everything_without_the_variable(tmp_path):
    printed, kept = run_import(tmp_path, None)
    assert printed == "None"
    assert kept == ["fifty-days", "fifty-hours", "thirty-hours"]


def test_import_time_sweep_uses_a_limit_from_the_env(tmp_path):
    printed, kept = run_import(tmp_path, "40")
    assert printed == "40.0"
    assert kept == ["thirty-hours"]


# --- the no-record error ---------------------------------------------------

LOOKUPS = {
    "unspool_sync": lambda sid: spindle._unspool_sync(sid),
    "codex_unspool": lambda sid: spindle._codex_unspool_sync(sid),
    "gemini_unspool": lambda sid: spindle._gemini_unspool_sync(sid),
    "kimi_unspool": lambda sid: spindle._kimi_unspool_sync(sid),
    "peek_sync": lambda sid: spindle._spool_peek_sync(sid),
    "wait_sync_gather": lambda sid: spindle._spin_wait_sync(sid, mode="gather"),
    "drop": lambda sid: spindle._spin_drop_sync(sid),
    "mcp_unspool": lambda sid: asyncio.run(tool("unspool")(sid)),
    "mcp_peek": lambda sid: asyncio.run(tool("spool_peek")(sid)),
    "mcp_wait_gather": lambda sid: asyncio.run(tool("spin_wait")(sid, mode="gather")),
    "mcp_info": lambda sid: asyncio.run(tool("spool_info")(sid)),
    "mcp_grep": lambda sid: asyncio.run(tool("spool_grep")("x", spool_id=sid)),
    "mcp_retry": lambda sid: asyncio.run(tool("spool_retry")(sid)),
    "mcp_export": lambda sid: asyncio.run(tool("spool_export")(sid)),
    "mcp_shard_status": lambda sid: asyncio.run(tool("shard_status")(sid)),
    "mcp_shard_merge": lambda sid: asyncio.run(tool("shard_merge")(sid, caller_cwd="/nonexistent-caller")),
    "mcp_shard_abandon": lambda sid: asyncio.run(tool("shard_abandon")(sid, caller_cwd="/nonexistent-caller")),
}


@pytest.mark.parametrize("lookup", sorted(LOOKUPS))
def test_missing_record_explains_retention(no_limit, monkeypatch, lookup):
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda **kwargs: None)
    assert LOOKUPS[lookup]("gone1234") == unknown("gone1234")


@pytest.mark.parametrize("lookup", ["unspool_sync", "peek_sync", "wait_sync_gather", "mcp_unspool", "mcp_wait_gather"])
@pytest.mark.parametrize("content", ["{}", "{not json"])
def test_present_but_unreadable_record_keeps_the_bare_message(no_limit, lookup, content):
    # The record file exists, so "no record exists" would be false.
    spindle._get_spool_path("garbled").write_text(content)
    assert LOOKUPS[lookup]("garbled") == "Error: Unknown spool_id 'garbled'"


@pytest.mark.parametrize(("hours", "window"), [(36, "36 hours"), (336, "14 days")])
def test_a_set_limit_is_named_in_the_message(monkeypatch, hours, window):
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", hours)
    assert spindle._unspool_sync("gone1234") == unknown("gone1234", pruned_note(window))
    assert spindle._respin_sync("gone1234", "go") == no_handle("gone1234", pruned_note(window))


@pytest.mark.parametrize("public", [False, True])
def test_yield_wait_reports_the_note_and_remaining(no_limit, public):
    spindle._write_spool("done", {"id": "done", "status": "complete", "result": "ok"})
    if public:
        rendered = asyncio.run(tool("spin_wait")("gone1234,done", mode="yield"))
    else:
        rendered = spindle._spin_wait_sync("gone1234,done", mode="yield")
    assert json.loads(rendered) == {
        "spool_id": "gone1234",
        "error": unknown("gone1234").removeprefix("Error: "),
        "remaining": ["done"],
    }


def test_respin_of_a_missing_handle_explains_retention(no_limit, monkeypatch):
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda **kwargs: None)
    assert spindle._respin_sync("codex-5ccd6bab", "next round") == no_handle("codex-5ccd6bab")


@pytest.mark.parametrize("wait_args", [{}, {"wait": False}], ids=["default", "detached"])
async def test_mcp_respin_returns_the_note_as_its_refusal(no_limit, monkeypatch, wait_args):
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda **kwargs: None)
    async with Client(spindle.mcp) as client:
        response = await client.call_tool("respin", {"session_id": "gone1234", "prompt": "go", **wait_args})
    assert response.content[0].text == no_handle("gone1234")
    assert response.structured_content == {"result": no_handle("gone1234")}


def test_respin_with_an_unreadable_record_keeps_the_bare_message(no_limit, monkeypatch):
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda **kwargs: None)
    spindle._get_spool_path("garbled").write_text("{not json")
    assert spindle._respin_sync("garbled", "go") == "Error: No spool found for handle 'garbled'"


def test_respin_with_an_overlong_handle_still_answers(no_limit, monkeypatch):
    # A sentence passed as a handle is longer than a filename may be. The
    # record check must not turn the refusal into ENAMETOOLONG.
    monkeypatch.setattr(spindle, "_run_store_maintenance", lambda **kwargs: None)
    handle = "please continue the review " * 20
    assert spindle._respin_sync(handle, "go") == no_handle(handle)


# --- what script callers parse ---------------------------------------------
#
# speakbot tools/spindle.py treats an unspool result starting with "Error:" as
# an error and, on a nonzero exit, any output containing "running" as still
# running. Mill's executor treats a `spindle wait` value starting with
# "Error: " as a failure. The new text must keep both prefixes and never say
# "running", or a missing spool would be polled forever.


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["spindle", *argv])
    with pytest.raises(SystemExit) as exc:
        spindle.main()
    return exc.value.code, capsys.readouterr().out


def test_cli_unspool_keeps_the_error_envelope(no_limit, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, "unspool", "gone1234")
    assert code == 0
    result = json.loads(out)["result"]
    assert result == unknown("gone1234")
    assert result.startswith("Error:")
    assert "running" not in result.lower()


def test_cli_wait_keeps_the_error_prefix(no_limit, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, "wait", "gone1234")
    assert code == 0
    assert out == unknown("gone1234") + "\n"
    assert "running" not in out.lower()


def test_cli_peek_reports_the_note(no_limit, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, "peek", "gone1234")
    assert code == 0
    assert json.loads(out) == {"spool_id": "gone1234", "output": unknown("gone1234")}


@pytest.mark.parametrize("hours", [None, 336, 36, 1, 0.25, 1e12])
def test_note_never_says_running(monkeypatch, hours):
    monkeypatch.setattr(spindle, "SPOOL_RETENTION_HOURS", hours)
    assert "running" not in spindle._unspool_sync("gone1234").lower()
    assert "running" not in spindle._respin_sync("gone1234", "go").lower()
