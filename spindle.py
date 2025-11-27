#!/usr/bin/env python3
"""
Spindle - MCP server for Claude Code to Claude Code delegation.

Lets CC agents spawn other CC agents, all using Max subscription credits.
Async by default - spin returns immediately, check results later.

Storage: ~/.spindle/spools/{spool_id}.json
"""

import asyncio
import json
import os
import signal
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from fastmcp import FastMCP

mcp = FastMCP("spindle")

# Storage directory
SPINDLE_DIR = Path.home() / ".spindle" / "spools"

# In-memory tracking of background asyncio tasks (not persisted)
_background_tasks: Dict[str, asyncio.Task] = {}

# Concurrency limit
MAX_CONCURRENT = 5


def _get_spool_path(spool_id: str) -> Path:
    """Get path to spool JSON file."""
    return SPINDLE_DIR / f"{spool_id}.json"


def _write_spool(spool_id: str, data: dict) -> None:
    """Atomically write spool data to disk."""
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    path = _get_spool_path(spool_id)
    tmp_path = path.with_suffix('.tmp')

    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)

    os.rename(tmp_path, path)


def _read_spool(spool_id: str) -> Optional[dict]:
    """Read spool data from disk."""
    path = _get_spool_path(spool_id)
    if not path.exists():
        return None

    with open(path) as f:
        return json.load(f)


def _list_spools() -> list[dict]:
    """List all spool files."""
    if not SPINDLE_DIR.exists():
        return []

    spools = []
    for path in SPINDLE_DIR.glob("*.json"):
        try:
            with open(path) as f:
                spools.append(json.load(f))
        except Exception:
            pass
    return spools


def _count_running() -> int:
    """Count currently running spools."""
    return sum(1 for s in _list_spools() if s.get('status') == 'running')


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)  # Doesn't kill, just checks
        return True
    except (OSError, ProcessLookupError):
        return False


def _cleanup_old_spools() -> None:
    """Remove spool files older than 24 hours."""
    if not SPINDLE_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(hours=24)

    for path in SPINDLE_DIR.glob("*.json"):
        try:
            with open(path) as f:
                data = json.load(f)

            created = datetime.fromisoformat(data.get('created_at', ''))
            if created < cutoff:
                path.unlink()
        except Exception:
            pass


def _recover_orphans() -> None:
    """Mark spools as error if their process died."""
    for spool in _list_spools():
        if spool.get('status') != 'running':
            continue

        pid = spool.get('pid')
        if pid and not _is_pid_alive(pid):
            spool['status'] = 'error'
            spool['error'] = 'Process died unexpectedly (orphaned on server restart)'
            spool['completed_at'] = datetime.now().isoformat()
            _write_spool(spool['id'], spool)


# Run cleanup and recovery on module load
_cleanup_old_spools()
_recover_orphans()


async def _run_claude(spool_id: str, cmd: list, cwd: str):
    """Background task that runs claude and stores result."""
    spool = _read_spool(spool_id)
    if not spool:
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=os.environ.copy()
        )

        # Store PID immediately
        spool['pid'] = proc.pid
        _write_spool(spool_id, spool)

        stdout, stderr = await proc.communicate()

        spool = _read_spool(spool_id)  # Re-read in case of updates
        if not spool:
            return

        if proc.returncode != 0:
            spool['status'] = 'error'
            spool['error'] = f"Exit {proc.returncode}: {stderr.decode()[:500]}"
        else:
            try:
                data = json.loads(stdout.decode())
                spool['result'] = data.get('result', stdout.decode())
                spool['session_id'] = data.get('session_id')
                spool['cost'] = data.get('cost')
            except json.JSONDecodeError:
                spool['result'] = stdout.decode()
            spool['status'] = 'complete'

        spool['completed_at'] = datetime.now().isoformat()
        _write_spool(spool_id, spool)

    except asyncio.CancelledError:
        spool = _read_spool(spool_id)
        if spool:
            spool['status'] = 'error'
            spool['error'] = 'Spool was cancelled'
            spool['completed_at'] = datetime.now().isoformat()
            _write_spool(spool_id, spool)
        raise
    except Exception as e:
        spool = _read_spool(spool_id)
        if spool:
            spool['status'] = 'error'
            spool['error'] = str(e)
            spool['completed_at'] = datetime.now().isoformat()
            _write_spool(spool_id, spool)
    finally:
        # Clean up background task reference
        _background_tasks.pop(spool_id, None)


@mcp.tool()
async def spin(
    prompt: str,
    system_prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
) -> str:
    """
    Spawn a Claude Code agent to handle a task. Returns immediately with spool_id.

    The agent runs in background. Use unspool(spool_id) to get the result.

    Args:
        prompt: The task/question for the agent
        system_prompt: Optional system prompt to configure behavior
        working_dir: Directory for the agent to work in (defaults to current)
        allowed_tools: Restrict tools (e.g. "Read,Write,Bash(git:*)")

    Returns:
        spool_id to check result later

    Example:
        spool_id = spin("Research the Python GIL")
        # ... do other work ...
        result = unspool(spool_id)
    """
    # Check concurrency limit
    if _count_running() >= MAX_CONCURRENT:
        return f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

    spool_id = str(uuid.uuid4())[:8]

    cmd = ['claude', '-p', prompt, '--output-format', 'json']

    if system_prompt:
        cmd.extend(['--system-prompt', system_prompt])

    if allowed_tools:
        cmd.extend(['--allowedTools', allowed_tools])

    cwd = working_dir or os.getcwd()

    # Create spool record
    spool = {
        'id': spool_id,
        'status': 'pending',
        'prompt': prompt,
        'result': None,
        'session_id': None,
        'working_dir': cwd,
        'allowed_tools': allowed_tools,
        'system_prompt': system_prompt,
        'created_at': datetime.now().isoformat(),
        'completed_at': None,
        'pid': None,
        'cost': None,
        'error': None,
    }

    _write_spool(spool_id, spool)

    # Update status to running
    spool['status'] = 'running'
    _write_spool(spool_id, spool)

    # Fire and forget - run in background
    bg_task = asyncio.create_task(_run_claude(spool_id, cmd, cwd))
    _background_tasks[spool_id] = bg_task

    return spool_id


@mcp.tool()
async def unspool(spool_id: str) -> str:
    """
    Get the result of a background spin task.

    Args:
        spool_id: The spool_id returned by spin()

    Returns:
        Result if complete, status if still running, error if failed
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    status = spool.get('status')

    if status == 'pending':
        return f"Spool {spool_id} pending (not yet started)"
    elif status == 'running':
        return f"Spool {spool_id} still running: {spool.get('prompt', '')[:50]}..."
    elif status == 'complete':
        return spool.get('result', 'No result')
    else:  # error
        return f"Spool {spool_id} failed: {spool.get('error', 'Unknown error')}"


@mcp.tool()
async def spools() -> str:
    """
    List all spools (running and completed).

    Returns:
        JSON object with spool statuses
    """
    all_spools = _list_spools()

    return json.dumps({
        spool['id']: {
            'status': spool.get('status'),
            'prompt': spool.get('prompt', '')[:100],
            'created_at': spool.get('created_at'),
            'session_id': spool.get('session_id'),
        }
        for spool in all_spools
    }, indent=2)


@mcp.tool()
async def respin(
    session_id: str,
    prompt: str,
) -> str:
    """
    Continue an existing Claude Code session with a new message.
    Returns immediately with spool_id.

    Args:
        session_id: The session ID to continue
        prompt: The follow-up message/task

    Returns:
        spool_id to check result later
    """
    # Check concurrency limit
    if _count_running() >= MAX_CONCURRENT:
        return f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

    spool_id = str(uuid.uuid4())[:8]

    cmd = [
        'claude', '-p', prompt,
        '--resume', session_id,
        '--output-format', 'json'
    ]

    cwd = os.getcwd()

    spool = {
        'id': spool_id,
        'status': 'running',
        'prompt': f"Continue {session_id}: {prompt}",
        'result': None,
        'session_id': session_id,
        'working_dir': cwd,
        'allowed_tools': None,
        'system_prompt': None,
        'created_at': datetime.now().isoformat(),
        'completed_at': None,
        'pid': None,
        'cost': None,
        'error': None,
    }

    _write_spool(spool_id, spool)

    bg_task = asyncio.create_task(_run_claude(spool_id, cmd, cwd))
    _background_tasks[spool_id] = bg_task

    return spool_id


@mcp.tool()
async def spin_wait(
    spool_ids: str,
    mode: str = "gather",
    timeout: Optional[int] = None,
) -> str:
    """
    Block until spools complete.

    Args:
        spool_ids: Comma-separated spool IDs to wait for
        mode: 'gather' (wait for all) or 'yield' (return first completed)
        timeout: Optional timeout in seconds

    Returns:
        Results from completed spools
    """
    ids = [s.strip() for s in spool_ids.split(',')]
    start_time = datetime.now()
    poll_interval = 3  # seconds

    if mode == "yield":
        # Return as soon as any completes
        while True:
            for spool_id in ids:
                spool = _read_spool(spool_id)
                if not spool:
                    return f"Error: Unknown spool_id '{spool_id}'"
                if spool.get('status') == 'complete':
                    return spool.get('result', 'No result')
                elif spool.get('status') == 'error':
                    return f"Error: {spool.get('error')}"

            if timeout:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    return f"Timeout after {timeout}s. Spools still running: {', '.join(ids)}"

            await asyncio.sleep(poll_interval)
    else:
        # gather mode - wait for all
        results = {}
        pending = set(ids)

        while pending:
            for spool_id in list(pending):
                spool = _read_spool(spool_id)
                if not spool:
                    return f"Error: Unknown spool_id '{spool_id}'"
                if spool.get('status') == 'complete':
                    results[spool_id] = spool.get('result', 'No result')
                    pending.remove(spool_id)
                elif spool.get('status') == 'error':
                    results[spool_id] = f"Error: {spool.get('error')}"
                    pending.remove(spool_id)

            if not pending:
                break

            if timeout:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    return f"Timeout after {timeout}s. Still pending: {', '.join(pending)}. Completed: {json.dumps(results)}"

            await asyncio.sleep(poll_interval)

        return json.dumps(results, indent=2)


@mcp.tool()
async def spin_drop(spool_id: str) -> str:
    """
    Cancel a running spool by killing its process.

    Args:
        spool_id: The spool_id to cancel

    Returns:
        Success or error message
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get('status') != 'running':
        return f"Spool {spool_id} is not running (status: {spool.get('status')})"

    pid = spool.get('pid')

    if not pid:
        return f"Spool {spool_id} has no PID recorded yet"

    # Cancel the asyncio task if we have a reference
    bg_task = _background_tasks.get(spool_id)
    if bg_task:
        bg_task.cancel()

    # Kill the process
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Already dead
    except OSError as e:
        return f"Error killing process: {e}"

    # Update spool status
    spool['status'] = 'error'
    spool['error'] = 'Cancelled by user'
    spool['completed_at'] = datetime.now().isoformat()
    _write_spool(spool_id, spool)

    return f"Dropped spool {spool_id}"


if __name__ == "__main__":
    mcp.run()
