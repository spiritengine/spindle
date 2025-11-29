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

# Permission profiles for tool restrictions
# These map to Claude Code's --allowedTools flag
# Profiles ending with "+shard" auto-enable shard isolation
PERMISSION_PROFILES = {
    "readonly": "Read,Grep,Glob,Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(git status:*),Bash(git log:*),Bash(git diff:*)",
    "careful": "Read,Write,Edit,Grep,Glob,Bash(git:*),Bash(make:*),Bash(pytest:*),Bash(python:*),Bash(npm:*)",
    "full": None,  # None means no restrictions
    # Shard variants - same permissions but auto-enable worktree isolation
    "shard": None,  # Full permissions + shard isolation (common combo)
    "careful+shard": "Read,Write,Edit,Grep,Glob,Bash(git:*),Bash(make:*),Bash(pytest:*),Bash(python:*),Bash(npm:*)",
}

# Cache for SKEIN availability check
_skein_available: Optional[bool] = None


def _has_skein() -> bool:
    """
    Check if SKEIN is available in the current project.
    Result is cached for performance.
    """
    global _skein_available
    if _skein_available is not None:
        return _skein_available

    # Check if skein command exists and we're in a git repo
    try:
        result = subprocess.run(
            ['skein', '--version'],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0:
            # Also check if we're in a git repo (SKEIN requires git)
            git_check = subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                capture_output=True,
                timeout=5
            )
            _skein_available = git_check.returncode == 0
        else:
            _skein_available = False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        _skein_available = False

    return _skein_available


def _resolve_permission(permission: Optional[str], allowed_tools: Optional[str]) -> tuple[Optional[str], bool]:
    """
    Resolve permission profile to allowed_tools string and shard flag.

    Args:
        permission: Permission profile name ("readonly", "careful", "full", "shard", etc.) or None
        allowed_tools: Explicit allowed_tools override (takes precedence)

    Returns:
        Tuple of (allowed_tools string or None, should_use_shard bool)
    """
    # Explicit allowed_tools takes precedence (no auto-shard)
    if allowed_tools:
        return allowed_tools, False

    # If no permission specified, use "careful" as default
    if not permission:
        permission = "careful"

    # Check if this is a shard profile
    use_shard = permission == "shard" or permission.endswith("+shard")

    # Look up profile
    if permission in PERMISSION_PROFILES:
        return PERMISSION_PROFILES[permission], use_shard

    # Unknown profile - use careful, no shard
    return PERMISSION_PROFILES["careful"], False


def _spawn_shard(agent_id: str, working_dir: str) -> Optional[Dict[str, str]]:
    """
    Create an isolated git worktree (SHARD) for the agent.

    Uses SKEIN if available, falls back to plain git worktree.

    Args:
        agent_id: Identifier for the shard (used in worktree name)
        working_dir: Base directory for the worktree

    Returns:
        Dict with shard info if successful, None if failed
        Keys: worktree_path, branch_name, shard_id
    """
    if _has_skein():
        # Use SKEIN's shard spawn command
        try:
            result = subprocess.run(
                [
                    'skein', 'shard', 'spawn',
                    '--agent', agent_id,
                    '--description', f'Spindle spool for {agent_id}'
                ],
                capture_output=True,
                text=True,
                cwd=working_dir,
                timeout=30
            )
            if result.returncode == 0:
                # Parse output to get worktree path
                # Output format: "✓ Spawned SHARD: ..."
                for line in result.stdout.splitlines():
                    if 'Worktree:' in line:
                        worktree_path = line.split('Worktree:')[1].strip()
                        # Extract other info
                        branch_name = None
                        shard_id = None
                        for l in result.stdout.splitlines():
                            if 'Branch:' in l:
                                branch_name = l.split('Branch:')[1].strip()
                            if 'Spawned SHARD:' in l:
                                shard_id = l.split('Spawned SHARD:')[1].strip()
                        return {
                            'worktree_path': worktree_path,
                            'branch_name': branch_name or f'shard-{agent_id}',
                            'shard_id': shard_id or agent_id,
                        }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: plain git worktree
    try:
        # Create worktrees directory if needed
        worktrees_dir = Path(working_dir) / 'worktrees'
        worktrees_dir.mkdir(exist_ok=True)

        # Generate unique worktree name
        date_str = datetime.now().strftime('%Y%m%d-%H%M%S')
        worktree_name = f'{agent_id}-{date_str}'
        worktree_path = worktrees_dir / worktree_name
        branch_name = f'shard-{worktree_name}'

        # Create git worktree with new branch
        result = subprocess.run(
            ['git', 'worktree', 'add', str(worktree_path), '-b', branch_name],
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=30
        )
        if result.returncode == 0:
            return {
                'worktree_path': str(worktree_path),
                'branch_name': branch_name,
                'shard_id': worktree_name,
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return None


def _cleanup_shard(shard_info: Dict[str, str], working_dir: str, keep_branch: bool = False) -> bool:
    """
    Clean up a SHARD worktree.

    Args:
        shard_info: Dict with worktree_path, branch_name
        working_dir: Base directory
        keep_branch: If True, don't delete the branch

    Returns:
        True if successful
    """
    worktree_path = shard_info.get('worktree_path')
    branch_name = shard_info.get('branch_name')

    if not worktree_path:
        return False

    try:
        # Remove worktree
        subprocess.run(
            ['git', 'worktree', 'remove', '--force', worktree_path],
            capture_output=True,
            cwd=working_dir,
            timeout=30
        )

        # Optionally delete branch
        if not keep_branch and branch_name:
            subprocess.run(
                ['git', 'branch', '-D', branch_name],
                capture_output=True,
                cwd=working_dir,
                timeout=10
            )

        # Prune worktree references
        subprocess.run(
            ['git', 'worktree', 'prune'],
            capture_output=True,
            cwd=working_dir,
            timeout=10
        )

        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


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
    permission: Optional[str] = None,
    shard: bool = False,
    system_prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    tags: Optional[str] = None,
) -> str:
    """
    Spawn a Claude Code agent to handle a task. Returns immediately with spool_id.

    The agent runs in background. Use unspool(spool_id) to get the result.

    Args:
        prompt: The task/question for the agent
        permission: Permission profile - "readonly", "careful" (default), "full",
                    "shard" (full + isolation), or "careful+shard"
        shard: Run in isolated git worktree (SKEIN-aware with graceful fallback)
        system_prompt: Optional system prompt to configure behavior
        working_dir: Directory for the agent to work in (defaults to current)
        allowed_tools: Override permission profile with explicit tool list
        tags: Comma-separated tags for organizing spools (e.g. "batch-1,triage")

    Returns:
        spool_id to check result later

    Example:
        spool_id = spin("Research the Python GIL")
        spool_id = spin("Fix the bug", permission="shard")  # full access + isolation
        spool_id = spin("Careful work", permission="careful+shard")
        result = unspool(spool_id)
    """
    # Check concurrency limit
    if _count_running() >= MAX_CONCURRENT:
        return f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

    spool_id = str(uuid.uuid4())[:8]
    cwd = working_dir or os.getcwd()

    # Resolve permission to allowed_tools and check for auto-shard
    resolved_tools, auto_shard = _resolve_permission(permission, allowed_tools)

    # Use shard if explicitly requested OR if permission profile enables it
    use_shard = shard or auto_shard

    # Handle shard creation
    shard_info = None
    if use_shard:
        shard_info = _spawn_shard(spool_id, cwd)
        if shard_info:
            cwd = shard_info['worktree_path']
        else:
            return f"Error: Failed to create SHARD worktree. Check git repo status."

    cmd = ['claude', '-p', prompt, '--output-format', 'json']

    # Auto-accept edits for non-interactive execution
    # Use acceptEdits for careful mode, bypassPermissions for full/shard
    if permission in ('full', 'shard') or (permission and '+shard' in permission):
        cmd.extend(['--permission-mode', 'bypassPermissions'])
    else:
        cmd.extend(['--permission-mode', 'acceptEdits'])

    if system_prompt:
        cmd.extend(['--system-prompt', system_prompt])

    if resolved_tools:
        cmd.extend(['--allowedTools', resolved_tools])

    # Parse tags
    tag_list = [t.strip() for t in tags.split(',')] if tags else []

    # Create spool record
    spool = {
        'id': spool_id,
        'status': 'pending',
        'prompt': prompt,
        'result': None,
        'session_id': None,
        'working_dir': cwd,
        'allowed_tools': resolved_tools,
        'permission': permission or 'careful',
        'system_prompt': system_prompt,
        'tags': tag_list,
        'shard': shard_info,
        'created_at': datetime.now().isoformat(),
        'completed_at': None,
        'pid': None,
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


@mcp.tool()
async def spool_retry(spool_id: str) -> str:
    """
    Re-run a spool with the same parameters.

    Args:
        spool_id: The spool_id to retry

    Returns:
        New spool_id for the retried task

    Example:
        new_id = spool_retry("abc123")  # retry failed spool
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    # Re-spin with same parameters
    return await spin(
        prompt=spool.get('prompt', ''),
        permission=spool.get('permission'),
        shard=bool(spool.get('shard')),
        system_prompt=spool.get('system_prompt'),
        working_dir=spool.get('working_dir'),
        allowed_tools=spool.get('allowed_tools'),
        tags=','.join(spool.get('tags', [])) if spool.get('tags') else None,
    )


@mcp.tool()
async def spool_stats() -> str:
    """
    Get summary statistics for all spools.

    Returns:
        JSON with counts by status and time range

    Example:
        stats = spool_stats()  # {"total": 25, "by_status": {"complete": 10, "error": 2}, ...}
    """
    all_spools = _list_spools()

    stats = {
        'total': len(all_spools),
        'by_status': {},
        'oldest': None,
        'newest': None,
    }

    for spool in all_spools:
        # Count by status
        status = spool.get('status', 'unknown')
        stats['by_status'][status] = stats['by_status'].get(status, 0) + 1

        # Track time range
        created = spool.get('created_at')
        if created:
            if not stats['oldest'] or created < stats['oldest']:
                stats['oldest'] = created
            if not stats['newest'] or created > stats['newest']:
                stats['newest'] = created

    return json.dumps(stats, indent=2)


@mcp.tool()
async def spool_export(
    spool_ids: str,
    format: str = "json",
    output_path: Optional[str] = None,
) -> str:
    """
    Export spool results to a file.

    Args:
        spool_ids: Comma-separated spool IDs, or "all" for all spools
        format: Output format - "json" or "md" (markdown)
        output_path: File path to write (default: ~/.spindle/export.{format})

    Returns:
        Path to exported file

    Example:
        spool_export("abc123,def456", format="md")
        spool_export("all", format="json", output_path="/tmp/results.json")
    """
    # Get spools to export
    if spool_ids.strip().lower() == "all":
        spools_to_export = _list_spools()
    else:
        ids = [s.strip() for s in spool_ids.split(',')]
        spools_to_export = []
        for sid in ids:
            spool = _read_spool(sid)
            if spool:
                spools_to_export.append(spool)
            else:
                return f"Error: Unknown spool_id '{sid}'"

    if not spools_to_export:
        return "No spools to export"

    # Sort by created_at
    spools_to_export.sort(key=lambda s: s.get('created_at', ''))

    # Generate output
    if format == "md":
        lines = ["# Spool Export", "", f"Generated: {datetime.now().isoformat()}", ""]
        for spool in spools_to_export:
            lines.append(f"## {spool.get('id')}")
            lines.append(f"**Status:** {spool.get('status')}")
            lines.append(f"**Created:** {spool.get('created_at')}")
            lines.append("")
            lines.append("### Prompt")
            lines.append(f"```\n{spool.get('prompt', '')}\n```")
            lines.append("")
            lines.append("### Result")
            result = spool.get('result', '')
            if isinstance(result, dict):
                result = json.dumps(result, indent=2)
            lines.append(f"```\n{result}\n```")
            lines.append("")
            lines.append("---")
            lines.append("")
        content = '\n'.join(lines)
        ext = "md"
    else:
        content = json.dumps(spools_to_export, indent=2)
        ext = "json"

    # Write file
    if output_path:
        path = Path(output_path)
    else:
        path = SPINDLE_DIR / f"export.{ext}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    return f"Exported {len(spools_to_export)} spools to {path}"


@mcp.tool()
async def shard_status(spool_id: str) -> str:
    """
    Get the status of a shard associated with a spool.

    Args:
        spool_id: The spool_id that has a shard

    Returns:
        JSON with shard info (worktree path, branch, git status)

    Example:
        shard_status("abc123")  # show shard details
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    shard_info = spool.get('shard')
    if not shard_info:
        return f"Spool {spool_id} has no shard (was not run with shard=True)"

    worktree_path = shard_info.get('worktree_path')
    if not worktree_path or not Path(worktree_path).exists():
        return json.dumps({
            'spool_id': spool_id,
            'shard': shard_info,
            'exists': False,
            'message': 'Worktree no longer exists'
        }, indent=2)

    # Get git status in the shard
    status_info = {
        'spool_id': spool_id,
        'shard': shard_info,
        'exists': True,
        'spool_status': spool.get('status'),
    }

    try:
        # Get git status
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10
        )
        if result.returncode == 0:
            status_info['git_changes'] = result.stdout.strip().split('\n') if result.stdout.strip() else []

        # Get commit count vs master
        result = subprocess.run(
            ['git', 'rev-list', '--count', 'master..HEAD'],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10
        )
        if result.returncode == 0:
            status_info['commits_ahead'] = int(result.stdout.strip())

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        status_info['git_error'] = 'Failed to get git status'

    return json.dumps(status_info, indent=2)


@mcp.tool()
async def shard_merge(spool_id: str, keep_branch: bool = False) -> str:
    """
    Merge a shard's changes back to master and clean up the worktree.

    The spool must be complete (not running). Changes are merged to master
    using a merge commit.

    Args:
        spool_id: The spool_id with a shard to merge
        keep_branch: Keep the branch after merge (default: delete)

    Returns:
        Success or error message

    Example:
        shard_merge("abc123")  # merge and cleanup
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get('status') == 'running':
        return f"Error: Spool {spool_id} is still running. Wait for completion."

    shard_info = spool.get('shard')
    if not shard_info:
        return f"Error: Spool {spool_id} has no shard"

    worktree_path = shard_info.get('worktree_path')
    branch_name = shard_info.get('branch_name')

    if not worktree_path or not Path(worktree_path).exists():
        return f"Error: Worktree no longer exists: {worktree_path}"

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent  # worktrees/name -> repo

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10
        )
        if result.stdout.strip():
            return f"Error: Shard has uncommitted changes. Commit or discard them first."

        # Merge branch to master from main repo
        result = subprocess.run(
            ['git', 'merge', branch_name, '--no-ff', '-m', f'Merge shard {spool_id}: {spool.get("prompt", "")[:50]}'],
            capture_output=True,
            text=True,
            cwd=str(main_repo),
            timeout=30
        )
        if result.returncode != 0:
            return f"Error: Merge failed: {result.stderr}"

        # Cleanup shard
        _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch)

        # Update spool record
        spool['shard']['merged'] = True
        spool['shard']['merged_at'] = datetime.now().isoformat()
        _write_spool(spool_id, spool)

        return f"Successfully merged shard {spool_id} to master"

    except subprocess.TimeoutExpired:
        return "Error: Git operation timed out"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


@mcp.tool()
async def shard_abandon(spool_id: str, keep_branch: bool = False) -> str:
    """
    Abandon a shard, removing the worktree without merging.

    Use this when a shard's work is no longer needed.

    Args:
        spool_id: The spool_id with a shard to abandon
        keep_branch: Keep the branch for later (default: delete)

    Returns:
        Success or error message

    Example:
        shard_abandon("abc123")  # discard shard
    """
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    shard_info = spool.get('shard')
    if not shard_info:
        return f"Error: Spool {spool_id} has no shard"

    worktree_path = shard_info.get('worktree_path')

    if not worktree_path:
        return f"Error: No worktree path in shard info"

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent

    # If spool is running, kill it first
    if spool.get('status') == 'running':
        pid = spool.get('pid')
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        spool['status'] = 'error'
        spool['error'] = 'Shard abandoned'
        spool['completed_at'] = datetime.now().isoformat()

    # Cleanup shard
    success = _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch)

    if success:
        spool['shard']['abandoned'] = True
        spool['shard']['abandoned_at'] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        return f"Abandoned shard {spool_id}" + (" (branch kept)" if keep_branch else "")
    else:
        return f"Warning: Shard cleanup may have been incomplete for {spool_id}"


@mcp.tool()
async def spindle_reload() -> str:
    """
    Signal spindle to reload (requires wrapper script).

    Write a signal file that the wrapper script watches for.
    The wrapper will restart spindle with fresh code.

    Returns:
        Status message
    """
    signal_file = Path.home() / ".spindle" / "reload_signal"
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(datetime.now().isoformat())
    return "Reload signal sent. If running with wrapper, spindle will restart."


if __name__ == "__main__":
    mcp.run()
