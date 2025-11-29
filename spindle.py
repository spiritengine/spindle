#!/usr/bin/env python3
"""
Spindle - MCP server for Claude Code to Claude Code delegation.

Lets CC agents spawn other CC agents, all using Max subscription credits.
Async by default - spin returns immediately, check results later.

Storage: ~/.spindle/spools/{spool_id}.json

Subprocess handling: Uses detached processes that survive MCP reconnects.
A background thread monitors completion by polling the PID.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from fastmcp import FastMCP

mcp = FastMCP("spindle")

# Storage directory
SPINDLE_DIR = Path.home() / ".spindle" / "spools"

# Concurrency limit
MAX_CONCURRENT = 5

# Poll interval for monitoring detached processes
MONITOR_POLL_INTERVAL = 2  # seconds


def _get_spool_path(spool_id: str) -> Path:
    """Get path to spool JSON file."""
    return SPINDLE_DIR / f"{spool_id}.json"


def _get_output_path(spool_id: str) -> Path:
    """Get path to stdout file for a spool."""
    return SPINDLE_DIR / f"{spool_id}.stdout"


def _get_stderr_path(spool_id: str) -> Path:
    """Get path to stderr file for a spool."""
    return SPINDLE_DIR / f"{spool_id}.stderr"


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

    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


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

            spool_id = data.get('id', path.stem)
            created = datetime.fromisoformat(data.get('created_at', ''))
            if created < cutoff:
                path.unlink()
                # Also clean up output files
                stdout_path = _get_output_path(spool_id)
                stderr_path = _get_stderr_path(spool_id)
                if stdout_path.exists():
                    stdout_path.unlink()
                if stderr_path.exists():
                    stderr_path.unlink()
        except Exception:
            pass


def _check_and_finalize_spool(spool_id: str) -> bool:
    """
    Check if a spool's process has finished and finalize it.
    Returns True if the spool was finalized, False if still running.

    Note: claude CLI doesn't exit immediately after writing output, so we also
    check if stdout contains a complete JSON result even if PID is alive.
    """
    spool = _read_spool(spool_id)
    if not spool or spool.get('status') != 'running':
        return True  # Already done

    pid = spool.get('pid')
    if not pid:
        return False  # No PID yet, still starting

    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)

    # Check if stdout has complete JSON result (claude may not exit promptly)
    stdout_complete = False
    if stdout_path.exists():
        try:
            content = stdout_path.read_text()
            if content.strip():
                data = json.loads(content)
                if 'result' in data or 'error' in data:
                    stdout_complete = True
        except (IOError, json.JSONDecodeError):
            pass

    # If PID alive and no complete output yet, still running
    if _is_pid_alive(pid) and not stdout_complete:
        return False

    # Process finished or output complete - finalize
    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)

    stdout = ""
    stderr = ""

    if stdout_path.exists():
        try:
            stdout = stdout_path.read_text()
        except IOError:
            pass

    if stderr_path.exists():
        try:
            stderr = stderr_path.read_text()
        except IOError:
            pass

    # Parse result
    try:
        data = json.loads(stdout)
        spool['result'] = data.get('result', stdout)
        spool['session_id'] = data.get('session_id')
        spool['cost'] = data.get('cost')
        spool['status'] = 'complete'
    except json.JSONDecodeError:
        if stdout.strip():
            spool['result'] = stdout
            spool['status'] = 'complete'
        elif stderr.strip():
            spool['status'] = 'error'
            spool['error'] = stderr[:500]
        else:
            spool['status'] = 'error'
            spool['error'] = 'Process exited with no output'

    spool['completed_at'] = datetime.now().isoformat()
    _write_spool(spool_id, spool)

    # Clean up output files
    if stdout_path.exists():
        stdout_path.unlink()
    if stderr_path.exists():
        stderr_path.unlink()

    return True


def _recover_orphans() -> None:
    """Check all running spools and finalize any that have completed."""
    for spool in _list_spools():
        if spool.get('status') == 'running':
            _check_and_finalize_spool(spool['id'])


def _monitor_spool(spool_id: str) -> None:
    """Background thread that monitors a spool until completion."""
    while True:
        if _check_and_finalize_spool(spool_id):
            break
        time.sleep(MONITOR_POLL_INTERVAL)


def _spawn_detached(spool_id: str, cmd: list, cwd: str) -> int:
    """
    Spawn a detached process that survives parent death.
    Returns the PID.
    """
    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)

    with open(stdout_path, 'w') as stdout_file, open(stderr_path, 'w') as stderr_file:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=os.environ.copy(),
            start_new_session=True,  # Detach from parent
        )

    return proc.pid


# Run cleanup and recovery on module load
_cleanup_old_spools()
_recover_orphans()


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

    # Spawn detached process
    pid = _spawn_detached(spool_id, cmd, cwd)

    # Update spool with PID and status
    spool['pid'] = pid
    spool['status'] = 'running'
    _write_spool(spool_id, spool)

    # Start background monitor thread (daemon so it won't block shutdown)
    monitor = threading.Thread(target=_monitor_spool, args=(spool_id,), daemon=True)
    monitor.start()

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
    # First check if we need to finalize
    _check_and_finalize_spool(spool_id)

    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    status = spool.get('status')

    if status == 'pending':
        return f"Spool {spool_id} pending (not yet started)"
    elif status == 'running':
        # Double-check if process is still alive
        pid = spool.get('pid')
        if pid and not _is_pid_alive(pid):
            _check_and_finalize_spool(spool_id)
            spool = _read_spool(spool_id)
            if spool.get('status') == 'complete':
                return spool.get('result', 'No result')
            elif spool.get('status') == 'error':
                return f"Spool {spool_id} failed: {spool.get('error', 'Unknown error')}"
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
    # Check for any finished spools first
    _recover_orphans()

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
        'status': 'pending',
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

    # Spawn detached process
    pid = _spawn_detached(spool_id, cmd, cwd)

    spool['pid'] = pid
    spool['status'] = 'running'
    _write_spool(spool_id, spool)

    # Start background monitor
    monitor = threading.Thread(target=_monitor_spool, args=(spool_id,), daemon=True)
    monitor.start()

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
                _check_and_finalize_spool(spool_id)
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
                _check_and_finalize_spool(spool_id)
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

    # Kill the process group (since we used start_new_session)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # Already dead
    except OSError:
        # Try killing just the process
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    # Update spool status
    spool['status'] = 'error'
    spool['error'] = 'Cancelled by user'
    spool['completed_at'] = datetime.now().isoformat()
    _write_spool(spool_id, spool)

    # Clean up output files
    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)
    if stdout_path.exists():
        stdout_path.unlink()
    if stderr_path.exists():
        stderr_path.unlink()

    return f"Dropped spool {spool_id}"


@mcp.tool()
async def spool_search(
    query: str,
    field: str = "both",
) -> str:
    """
    Search spool prompts and/or results for a string.

    Args:
        query: The search string (case-insensitive)
        field: Where to search - "prompt", "result", or "both" (default)

    Returns:
        Matching spool IDs with context snippets

    Example:
        spool_search("triage")              # search both
        spool_search("human review", field="result")  # results only
    """
    all_spools = _list_spools()
    matches = []
    query_lower = query.lower()

    for spool in all_spools:
        spool_id = spool.get('id', 'unknown')
        prompt = spool.get('prompt', '') or ''
        result = spool.get('result', '') or ''

        # Convert result to string if it's a dict
        if isinstance(result, dict):
            result = json.dumps(result)

        prompt_match = query_lower in prompt.lower() if field in ('prompt', 'both') else False
        result_match = query_lower in result.lower() if field in ('result', 'both') else False

        if prompt_match or result_match:
            match_info = {
                'id': spool_id,
                'status': spool.get('status'),
                'created_at': spool.get('created_at'),
            }

            # Add context snippets
            if prompt_match:
                idx = prompt.lower().find(query_lower)
                start = max(0, idx - 30)
                end = min(len(prompt), idx + len(query) + 30)
                match_info['prompt_match'] = f"...{prompt[start:end]}..."

            if result_match:
                idx = result.lower().find(query_lower)
                start = max(0, idx - 50)
                end = min(len(result), idx + len(query) + 50)
                match_info['result_match'] = f"...{result[start:end]}..."

            matches.append(match_info)

    if not matches:
        return f"No spools found matching '{query}' in {field}"

    return json.dumps(matches, indent=2)


@mcp.tool()
async def spool_results(
    status: str = "complete",
    since: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Bulk fetch spool results with filtering.

    Args:
        status: Filter by status - "complete", "error", "running", or "all" (default: complete)
        since: Time filter - "1h", "6h", "1d", "7d" (default: no filter)
        limit: Max results to return (default: 10)

    Returns:
        List of spool results matching filters

    Example:
        spool_results()                      # last 10 completed
        spool_results(status="error")        # failed spools
        spool_results(since="1h")            # last hour
    """
    all_spools = _list_spools()
    now = datetime.now()

    # Parse since filter
    since_cutoff = None
    if since:
        since_map = {
            '1h': timedelta(hours=1),
            '6h': timedelta(hours=6),
            '12h': timedelta(hours=12),
            '1d': timedelta(days=1),
            '7d': timedelta(days=7),
        }
        delta = since_map.get(since)
        if delta:
            since_cutoff = now - delta
        else:
            return f"Invalid since value '{since}'. Use: 1h, 6h, 12h, 1d, 7d"

    # Filter spools
    filtered = []
    for spool in all_spools:
        # Status filter
        if status != "all" and spool.get('status') != status:
            continue

        # Time filter
        if since_cutoff:
            created_str = spool.get('created_at')
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    if created < since_cutoff:
                        continue
                except ValueError:
                    continue

        filtered.append(spool)

    # Sort by created_at descending
    filtered.sort(key=lambda s: s.get('created_at', ''), reverse=True)

    # Apply limit
    filtered = filtered[:limit]

    # Format output
    results = []
    for spool in filtered:
        result_text = spool.get('result', '')
        if isinstance(result_text, dict):
            result_text = json.dumps(result_text)

        results.append({
            'id': spool.get('id'),
            'status': spool.get('status'),
            'prompt': spool.get('prompt', '')[:100],
            'result': result_text[:500] if result_text else None,
            'created_at': spool.get('created_at'),
            'session_id': spool.get('session_id'),
        })

    if not results:
        return f"No spools found with status='{status}'" + (f" since {since}" if since else "")

    return json.dumps(results, indent=2)


@mcp.tool()
async def spool_grep(pattern: str) -> str:
    """
    Regex search through all spool results.

    Args:
        pattern: Regular expression pattern to search for

    Returns:
        Matching spool IDs with matched text

    Example:
        spool_grep("friction-[0-9]+-[a-z]+")    # find friction IDs in results
        spool_grep("error|failed|exception")    # find error-related text
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    all_spools = _list_spools()
    matches = []

    for spool in all_spools:
        spool_id = spool.get('id', 'unknown')
        result = spool.get('result', '') or ''

        # Convert result to string if it's a dict
        if isinstance(result, dict):
            result = json.dumps(result)

        found = regex.findall(result)
        if found:
            # Get unique matches and limit to first 10
            unique_matches = list(dict.fromkeys(found))[:10]
            matches.append({
                'id': spool_id,
                'status': spool.get('status'),
                'prompt': spool.get('prompt', '')[:80],
                'matches': unique_matches,
                'match_count': len(found),
            })

    if not matches:
        return f"No results matching pattern '{pattern}'"

    return json.dumps(matches, indent=2)


if __name__ == "__main__":
    mcp.run()
