"""A running logical owner reaps what it adopts and idles without burning CPU.

Both defects showed up on long spools (issue-20261103-13yg: hundreds of zombie
children under live owners; issue-20260830-i7q8: owners at 20-50 percent of a
core).  These cases run the packaged watchdog and owner against fake providers
in a throwaway store.  The cost cases measure the owner over a real time
window, so their bounds are deliberately generous: they separate a 50 Hz
whole-capture rescan from a bounded idle wait, not one tuning from another.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import spindle
from tests.namespace_owner_fixtures import LARGE_CAPTURE_BYTES, ORPHAN_BURST

STREAM_SPOOL = {"harness": "claude-code", "claude_protocol": spindle.CLAUDE_PROTOCOL_STREAM_V1}


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _kernel_children(pid: int) -> list[int]:
    return [int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


def _cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _voluntary_switches(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("voluntary_ctxt_switches:"):
            return int(line.split()[1])
    raise AssertionError(f"/proc/{pid}/status has no voluntary_ctxt_switches")


def test_running_owner_reaps_adopted_orphans_and_leaves_the_provider_status_to_popen(watchdog_owner_case):
    owner = watchdog_owner_case("orphan-burst")
    owner_pid, provider_pid = owner.ready["owner_pid"], owner.ready["provider_pid"]
    assert _wait_for((owner.store / f"{owner.spool_id}.burst-done").exists, timeout=10)

    # Every orphan has exited by now or does so within milliseconds.  While
    # the provider still runs, the owner's only remaining child is the
    # provider: nothing it adopted may linger as a zombie until spool exit.
    _wait_for(lambda: _kernel_children(owner_pid) == [provider_pid], timeout=3)
    assert _kernel_children(owner_pid) == [provider_pid]
    assert owner.spool()["owner_episode"]["phase"] == "accepted"

    with patch("spindle.SPINDLE_DIR", owner.store):
        assert "Cancellation requested" in spindle._spin_drop_sync(owner.spool_id)
    owner.process.wait(timeout=10)

    cleanup = owner.spool()["owner_episode"]["cleanup"]
    assert cleanup["outcome"] == "stopped"
    # 23 is the provider's own status; a reaper that consumed the provider
    # would leave Popen reading ECHILD, which it reports as exit 0.
    assert cleanup["provider_exit_code"] == 23
    assert cleanup["adopted_children_reaped"] >= ORPHAN_BURST


@pytest.mark.parametrize("capture", ["small", "large"])
def test_idle_owner_neither_busy_polls_nor_rescans_an_unchanged_capture(watchdog_owner_case, capture):
    if capture == "large":
        owner = watchdog_owner_case("large-stream-capture", spool_overrides=STREAM_SPOOL)
        stdout_path = owner.store / f"{owner.spool_id}.stdout"
        assert _wait_for((owner.store / f"{owner.spool_id}.capture-done").exists, timeout=20)
        assert stdout_path.stat().st_size >= LARGE_CAPTURE_BYTES * 0.9
    else:
        owner = watchdog_owner_case("healthy-turn", spool_overrides=STREAM_SPOOL)
    owner_pid = owner.ready["owner_pid"]
    # Let the owner take its first look at the finished capture.
    time.sleep(0.3)

    cpu_before, wakeups_before, started = _cpu_seconds(owner_pid), _voluntary_switches(owner_pid), time.monotonic()
    time.sleep(2.0)
    cpu_after, wakeups_after, ended = _cpu_seconds(owner_pid), _voluntary_switches(owner_pid), time.monotonic()
    elapsed = ended - started

    assert owner.process.poll() is None
    cpu_share = (cpu_after - cpu_before) / elapsed
    wakeups_per_second = (wakeups_after - wakeups_before) / elapsed
    # Before the fix: about 0.45 of a core on a 5 MB capture and 47 wakeups a
    # second on an empty one.  After: well under 0.02 and about 10.
    assert cpu_share < 0.10, f"idle owner used {cpu_share:.0%} of a core"
    assert wakeups_per_second < 25, f"idle owner woke {wakeups_per_second:.0f} times a second"
