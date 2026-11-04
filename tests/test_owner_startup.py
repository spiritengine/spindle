"""The detached helper processes must not pay for the MCP server they never run.

Every spool launch starts spindle_owner.py (the watchdog, then the logical
owner), and every store gets a spindle_supervisor.py. Each imports the spindle
package for its spool helpers. fastmcp was 6.1 s of the owner's 7.1 s import at
load 18 (friction-20261103-mbj2), so owner fixtures with a 5 s ready window
failed in bulk whenever the box was busy.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Top-level packages that only the MCP server needs. Any of them in a helper
# process means the server stack was imported.
SERVER_STACK = ("fastmcp", "mcp", "starlette", "uvicorn", "anyio", "httpx", "pydantic", "authlib")

_REPORT = (
    "import json, sys, time; "
    "print(json.dumps({'cpu': time.process_time(), 'server_stack': sorted({m.split('.')[0] for m in sys.modules} "
    f"& set({SERVER_STACK!r}))}}))"
)


def _run(code, *, guard):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO_ROOT))
    env.pop("_SPINDLE_STORE_SUPERVISOR", None)
    if guard:
        env["_SPINDLE_STORE_SUPERVISOR"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", f"{code}; {_REPORT}"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_owner_entry_does_not_import_the_server_stack():
    """spindle_owner.py sets the helper guard itself; nothing else may be needed."""
    report = _run("import spindle_owner", guard=False)
    assert report["server_stack"] == []


def test_supervisor_import_does_not_import_the_server_stack():
    """spindle_supervisor.main() sets the guard and then imports the package."""
    report = _run("import spindle", guard=True)
    assert report["server_stack"] == []


def test_helper_import_keeps_the_spool_helpers_the_owner_reaches():
    code = (
        "import spindle_owner, spindle; "
        "names = ['_read_spool', '_write_spool', '_spool_lock', '_post_terminal_bookkeeping', "
        "'_run_store_supervisor', '_extract_cc_result', 'SPINDLE_DIR']; "
        "missing = [n for n in names if not hasattr(spindle, n)]; "
        "assert not missing, missing"
    )
    assert _run(code, guard=False)["server_stack"] == []


def test_helper_import_refuses_to_serve():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO_ROOT), _SPINDLE_STORE_SUPERVISOR="1")
    proc = subprocess.run(
        [sys.executable, "-c", "import spindle; spindle.mcp.run()"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode != 0
    assert "_SPINDLE_STORE_SUPERVISOR" in proc.stderr


def test_service_and_cli_import_still_load_the_server():
    """Without the guard nothing changes: tools are FastMCP tools on a real server."""
    code = (
        "import spindle; from fastmcp import FastMCP; from fastmcp.tools.tool import FunctionTool; "
        "assert isinstance(spindle.mcp, FastMCP); assert isinstance(spindle.spin, FunctionTool)"
    )
    assert "fastmcp" in _run(code, guard=False)["server_stack"]


def test_owner_import_costs_a_fraction_of_the_server_import():
    """Startup cost, measured as CPU time so a loaded box does not decide it.

    Wall-clock budgets are what failed under load; CPU time spent importing
    barely moves with the load average, and comparing against a full server
    import in the same environment cancels the speed of the machine.
    """
    owner = _run("import spindle_owner", guard=False)["cpu"]
    server = _run("import spindle", guard=False)["cpu"]
    assert owner < 0.5 * server, f"owner import {owner:.2f}s CPU vs server import {server:.2f}s CPU"
