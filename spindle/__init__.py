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
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("spindle", stateless_http=True)

# Track server start time for uptime calculation
_server_start_time = datetime.now()


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for monitoring and systemd watchdog."""
    uptime_seconds = (datetime.now() - _server_start_time).total_seconds()
    running_count = _count_running()

    return JSONResponse(
        {
            "status": "healthy",
            "uptime_seconds": int(uptime_seconds),
            "running_spools": running_count,
            "max_concurrent": MAX_CONCURRENT,
        }
    )


# Storage directory
SPINDLE_DIR = Path.home() / ".spindle" / "spools"

# Concurrency limit (configurable via env var)
MAX_CONCURRENT = int(os.environ.get("SPINDLE_MAX_CONCURRENT", "15"))

# Poll interval for monitoring detached processes
MONITOR_POLL_INTERVAL = 2  # seconds

# Permission profiles for tool restrictions
# These map to Claude Code's --allowedTools flag
# Profiles ending with "+shard" auto-enable shard isolation
PERMISSION_PROFILES = {
    "readonly": "Read,Grep,Glob,Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(git status:*),Bash(git log:*),Bash(git diff:*)",
    "careful": "Read,Write,Edit,Grep,Glob,Bash(git:*),Bash(make:*),Bash(pytest:*),Bash(python:*),Bash(npm:*),Bash(skein:*),Bash(muster:*)",
    "full": None,  # None means no restrictions
    # Shard variants - same permissions but auto-enable worktree isolation
    "shard": None,  # Full permissions + shard isolation (common combo)
    "careful+shard": "Read,Write,Edit,Grep,Glob,Bash(git:*),Bash(make:*),Bash(pytest:*),Bash(python:*),Bash(npm:*),Bash(skein:*),Bash(muster:*)",
}

# Cache for SKEIN availability check
_skein_available: Optional[bool] = None


def _has_skein() -> bool:
    """
    Check if SKEIN is available in the current project.
    Result is cached for performance.

    Uses 'skein health' which checks git repo, .skein/ dir, and server.
    """
    global _skein_available
    if _skein_available is not None:
        return _skein_available

    try:
        result = subprocess.run(["skein", "health", "--json"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            _skein_available = data.get("healthy", False)
        else:
            _skein_available = False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError):
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
                ["skein", "shard", "spawn", "--agent", agent_id, "--description", f"Spindle spool for {agent_id}"],
                capture_output=True,
                text=True,
                cwd=working_dir,
                timeout=30,
            )
            if result.returncode == 0:
                # Parse output to get worktree path
                # Output format: "✓ Spawned SHARD: ..."
                for line in result.stdout.splitlines():
                    if "Worktree:" in line:
                        worktree_path = line.split("Worktree:")[1].strip()
                        # Extract other info
                        branch_name = None
                        shard_id = None
                        for l in result.stdout.splitlines():
                            if "Branch:" in l:
                                branch_name = l.split("Branch:")[1].strip()
                            if "Spawned SHARD:" in l:
                                shard_id = l.split("Spawned SHARD:")[1].strip()
                        return {
                            "worktree_path": worktree_path,
                            "branch_name": branch_name or f"shard-{agent_id}",
                            "shard_id": shard_id or agent_id,
                        }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: plain git worktree
    try:
        # Create worktrees directory if needed
        worktrees_dir = Path(working_dir) / "worktrees"
        worktrees_dir.mkdir(exist_ok=True)

        # Generate unique worktree name
        date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        worktree_name = f"{agent_id}-{date_str}"
        worktree_path = worktrees_dir / worktree_name
        branch_name = f"shard-{worktree_name}"

        # Create git worktree with new branch
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=30,
        )
        if result.returncode == 0:
            return {
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "shard_id": worktree_name,
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return None


def _close_tender_folios(worktree_name: str) -> Optional[str]:
    """
    Close any tender folios associated with a worktree after successful merge.

    Queries SKEIN for tender folios with matching worktree_name in metadata,
    then closes them by creating a status thread.

    Args:
        worktree_name: The worktree name to match in tender metadata

    Returns:
        Message about closed folios, or None if SKEIN unavailable/no matches
    """
    if not _has_skein():
        return None

    try:
        import urllib.request
        import urllib.error

        # Query SKEIN for tender folios
        skein_url = os.environ.get("SKEIN_URL", "http://localhost:8001")
        agent_id = os.environ.get("SKEIN_AGENT_ID", "spindle")

        # Get all tender folios
        req = urllib.request.Request(f"{skein_url}/folios?type=tender", headers={"X-Agent-ID": agent_id})

        with urllib.request.urlopen(req, timeout=10) as response:
            folios = json.loads(response.read().decode())

        # Find tenders with matching worktree_name in metadata
        closed_folios = []
        for folio in folios:
            metadata = folio.get("metadata", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    continue

            if metadata.get("worktree_name") == worktree_name:
                folio_id = folio.get("folio_id")
                if not folio_id:
                    continue

                # Check if already closed
                status = folio.get("status", "open")
                if status == "closed":
                    continue

                # Close the folio by creating a status thread
                close_data = json.dumps(
                    {"from_id": folio_id, "to_id": folio_id, "type": "status", "content": "closed"}
                ).encode()

                close_req = urllib.request.Request(
                    f"{skein_url}/threads",
                    data=close_data,
                    headers={"X-Agent-ID": agent_id, "Content-Type": "application/json"},
                    method="POST",
                )

                try:
                    urllib.request.urlopen(close_req, timeout=10)
                    closed_folios.append(folio_id)
                except urllib.error.URLError:
                    pass  # Ignore individual close failures

        if closed_folios:
            return f"Closed tender(s): {', '.join(closed_folios)}"
        return None

    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None  # SKEIN not available or error, continue silently


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
    worktree_path = shard_info.get("worktree_path")
    branch_name = shard_info.get("branch_name")

    if not worktree_path:
        return False

    try:
        # Remove worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path], capture_output=True, cwd=working_dir, timeout=30
        )

        # Optionally delete branch
        if not keep_branch and branch_name:
            subprocess.run(["git", "branch", "-D", branch_name], capture_output=True, cwd=working_dir, timeout=10)

        # Prune worktree references
        subprocess.run(["git", "worktree", "prune"], capture_output=True, cwd=working_dir, timeout=10)

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


def _get_transcript_path(spool_id: str) -> Path:
    """Get path to transcript file for a spool."""
    return SPINDLE_DIR / "transcripts" / f"{spool_id}.txt"


def _write_spool(spool_id: str, data: dict) -> None:
    """Atomically write spool data to disk."""
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    path = _get_spool_path(spool_id)
    tmp_path = path.with_suffix(".tmp")

    with open(tmp_path, "w") as f:
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


def _find_spool_by_session(session_id: str) -> Optional[dict]:
    """Find a spool by its session_id."""
    for spool in _list_spools():
        if spool.get("session_id") == session_id:
            return spool
    return None


def _count_running() -> int:
    """Count currently running spools."""
    return sum(1 for s in _list_spools() if s.get("status") == "running")


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

            spool_id = data.get("id", path.stem)
            created = datetime.fromisoformat(data.get("created_at", ""))
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
    if not spool or spool.get("status") != "running":
        return True  # Already done

    pid = spool.get("pid")
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
                if "result" in data or "error" in data:
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
        spool["result"] = data.get("result", stdout)
        spool["session_id"] = data.get("session_id")
        spool["cost"] = data.get("cost")
        spool["status"] = "complete"
    except json.JSONDecodeError:
        if stdout.strip():
            spool["result"] = stdout
            spool["status"] = "complete"
        elif stderr.strip():
            spool["status"] = "error"
            spool["error"] = stderr[:500]
        else:
            spool["status"] = "error"
            spool["error"] = "Process exited with no output"

    spool["completed_at"] = datetime.now().isoformat()
    _write_spool(spool_id, spool)

    # Save transcript for future respin if session_id exists
    # This preserves conversation context even after CC cleans up sessions
    if spool.get("session_id") and stdout:
        transcript_path = _get_transcript_path(spool_id)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            transcript_path.write_text(stdout)
        except IOError:
            pass  # Non-critical, continue

    # Clean up output files
    if stdout_path.exists():
        stdout_path.unlink()
    if stderr_path.exists():
        stderr_path.unlink()

    return True


def _recover_orphans() -> None:
    """Check all running spools and finalize any that have completed."""
    for spool in _list_spools():
        if spool.get("status") == "running":
            _check_and_finalize_spool(spool["id"])


def _handle_expired_session(spool_id: str, spool: dict) -> bool:
    """
    Handle expired session by retrying with transcript injection.

    Returns True if successfully retried, False otherwise.
    """
    # Find original spool with this session_id
    original_spool = _find_spool_by_session(spool["session_id"])
    if not original_spool:
        return False

    # Check for transcript
    transcript_path = _get_transcript_path(original_spool["id"])
    if not transcript_path.exists():
        return False

    # Kill the failing process
    pid = spool.get("pid")
    if pid and _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
            if _is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    # Read transcript
    try:
        transcript = transcript_path.read_text()
    except IOError:
        return False

    # Build new prompt with transcript context
    context_prompt = f"""Previous conversation transcript:

{transcript}

---

Continue from above. New message: {spool['prompt'].split(': ', 1)[-1]}"""

    # Spawn new process without --resume flag, with transcript as context
    cmd = ["claude", "-p", context_prompt, "--output-format", "json"]

    try:
        new_pid = _spawn_detached(spool_id, cmd, spool["working_dir"])

        # Update spool with new PID and mark as using transcript fallback
        spool["pid"] = new_pid
        spool["used_transcript_fallback"] = True
        spool["transcript_injected_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)

        return True
    except Exception:
        return False


def _monitor_spool(spool_id: str) -> None:
    """Background thread that monitors a spool until completion."""
    while True:
        # Check for timeout
        spool = _read_spool(spool_id)
        if spool and spool.get("timeout"):
            created = datetime.fromisoformat(spool["created_at"])
            elapsed = (datetime.now() - created).total_seconds()
            if elapsed > spool["timeout"]:
                # Kill the process
                pid = spool.get("pid")
                if pid and _is_pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(0.5)
                        if _is_pid_alive(pid):
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                # Mark as timeout
                spool["status"] = "timeout"
                spool["error"] = f'Timeout after {spool["timeout"]}s'
                spool["completed_at"] = datetime.now().isoformat()
                _write_spool(spool_id, spool)
                break

        # For respin spools, check for "session not found" error early
        if spool and spool.get("session_id") and spool.get("status") == "running":
            stderr_path = _get_stderr_path(spool_id)
            if stderr_path.exists():
                try:
                    stderr_content = stderr_path.read_text()
                    if "No conversation found with session ID" in stderr_content:
                        # Session expired - try transcript fallback
                        if _handle_expired_session(spool_id, spool):
                            break  # Successfully retried with transcript
                except IOError:
                    pass

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

    with open(stdout_path, "w") as stdout_file, open(stderr_path, "w") as stderr_file:
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


def _spin_sync(
    prompt: str,
    permission: Optional[str],
    shard: bool,
    system_prompt: Optional[str],
    working_dir: Optional[str],
    allowed_tools: Optional[str],
    tags: Optional[str],
    model: Optional[str],
    timeout: Optional[int],
    skeinless: bool,
) -> str:
    """Synchronous implementation of spin - runs in thread pool."""
    # Check concurrency limit
    if _count_running() >= MAX_CONCURRENT:
        return f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

    spool_id = str(uuid.uuid4())[:8]

    # Require working_dir - os.getcwd() returns MCP server dir, not caller's project
    if not working_dir:
        return "Error: working_dir required. Pass the project directory."

    cwd = working_dir

    # Resolve permission to allowed_tools and check for auto-shard
    resolved_tools, auto_shard = _resolve_permission(permission, allowed_tools)

    # Use shard if explicitly requested OR if permission profile enables it
    use_shard = shard or auto_shard

    # Handle shard creation
    shard_info = None
    if use_shard:
        shard_info = _spawn_shard(spool_id, cwd)
        if shard_info:
            cwd = shard_info["worktree_path"]
        else:
            return f"Error: Failed to create SHARD worktree. Check git repo status."

    # Inject SKEIN context for shard agents (unless skeinless=True)
    effective_prompt = prompt
    if _has_skein() and shard_info and not skeinless:
        # Prepend SKEIN ignition instructions to the prompt
        worktree_name = shard_info.get("shard_id", spool_id)
        skein_preamble = f"""You are working in an isolated SHARD worktree.

Before starting work, orient yourself with SKEIN:
1. Run: skein ignite --message "{prompt[:100]}..."
2. Then: skein ready --name "spool-{spool_id}"

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"
2. Tender: skein shard tender {worktree_name} --summary "What you did" --confidence N
   (confidence 1-10: 10=safe/isolated, 5=needs review, 1=risky)
3. Retire: skein torch && skein complete

Your task:
"""
        effective_prompt = skein_preamble + prompt
    elif shard_info:
        # Non-SKEIN shard - still need commit instructions
        shard_preamble = """You are working in an isolated SHARD worktree.

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"

Your task:
"""
        effective_prompt = shard_preamble + prompt

    claude_cmd = ["claude", "-p", effective_prompt, "--output-format", "json"]

    if model:
        claude_cmd.extend(["--model", model])

    # Auto-accept edits for non-interactive execution
    # Use acceptEdits for careful mode, bypassPermissions for full/shard
    if permission in ("full", "shard") or (permission and "+shard" in permission):
        claude_cmd.extend(["--permission-mode", "bypassPermissions"])
    else:
        claude_cmd.extend(["--permission-mode", "acceptEdits"])

    if system_prompt:
        claude_cmd.extend(["--system-prompt", system_prompt])

    if resolved_tools:
        claude_cmd.extend(["--allowedTools", resolved_tools])

    # Wrap in bwrap sandbox for shards - worktree writable, rest read-only
    if shard_info and shutil.which("bwrap"):
        home = str(Path.home())
        cmd = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",  # Root read-only
            "--bind",
            cwd,
            cwd,  # Worktree writable
            "--bind",
            "/tmp",
            "/tmp",  # Tmp writable
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            cwd,
        ]
        # Make git writable for commits in worktrees
        # Worktrees need:
        #   .git/worktrees/<name>/ - index, HEAD, logs
        #   .git/objects/ - store new blobs, trees, commits
        #   .git/refs/heads/ - update branch pointers
        git_file = Path(cwd) / ".git"
        if git_file.exists() and git_file.is_file():
            git_content = git_file.read_text().strip()
            if git_content.startswith("gitdir:"):
                git_worktree_dir = git_content.split("gitdir:")[1].strip()
                if Path(git_worktree_dir).exists():
                    # Worktree metadata (index, HEAD)
                    cmd.extend(["--bind", git_worktree_dir, git_worktree_dir])
                    # Main .git directory for objects and refs
                    # gitdir is like: /path/to/repo/.git/worktrees/<name>
                    main_git = Path(git_worktree_dir).parent.parent
                    if main_git.exists() and main_git.name == ".git":
                        # Objects - for storing commits (append-only)
                        objects_dir = main_git / "objects"
                        if objects_dir.exists():
                            cmd.extend(["--bind", str(objects_dir), str(objects_dir)])
                        # Refs/heads - for branch pointers (not remotes/tags)
                        refs_heads = main_git / "refs" / "heads"
                        if refs_heads.exists():
                            cmd.extend(["--bind", str(refs_heads), str(refs_heads)])
                        # Logs/refs/heads - for reflogs
                        logs_refs_heads = main_git / "logs" / "refs" / "heads"
                        if logs_refs_heads.exists():
                            cmd.extend(["--bind", str(logs_refs_heads), str(logs_refs_heads)])
        # Conditionally bind config dirs/files if they exist
        for config_item in [".claude", ".claude.json", ".anthropic", ".spindle", ".config"]:
            path = f"{home}/{config_item}"
            if Path(path).exists():
                cmd.extend(["--bind", path, path])
        cmd.extend(claude_cmd)
    else:
        cmd = claude_cmd

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": None,
        "working_dir": cwd,
        "allowed_tools": resolved_tools,
        "permission": permission or "careful",
        "system_prompt": system_prompt,
        "tags": tag_list,
        "shard": shard_info,
        "model": model,
        "timeout": timeout,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
    }

    _write_spool(spool_id, spool)

    # Spawn detached process
    pid = _spawn_detached(spool_id, cmd, cwd)

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread (daemon so it won't block shutdown)
    monitor = threading.Thread(target=_monitor_spool, args=(spool_id,), daemon=True)
    monitor.start()

    return spool_id


@mcp.tool()
async def spin(
    prompt: str,
    permission: Optional[str] = None,
    shard: bool = False,
    system_prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    tags: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    skeinless: bool = False,
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
        model: Model to use - "haiku", "sonnet", or "opus" (default: inherit)
        timeout: Kill spool after this many seconds (default: no timeout)
        skeinless: Skip SKEIN context injection for shard agents (default: False)

    Returns:
        spool_id to check result later

    Example:
        spool_id = spin("Research the Python GIL")
        spool_id = spin("Fix the bug", permission="shard")  # full access + isolation
        spool_id = spin("Careful work", permission="careful+shard")
        spool_id = spin("Quick task", model="haiku", timeout=60)
        result = unspool(spool_id)
    """
    return await asyncio.to_thread(
        _spin_sync,
        prompt,
        permission,
        shard,
        system_prompt,
        working_dir,
        allowed_tools,
        tags,
        model,
        timeout,
        skeinless,
    )


def _unspool_sync(spool_id: str) -> str:
    """Synchronous implementation of unspool."""
    _check_and_finalize_spool(spool_id)
    spool = _read_spool(spool_id)
    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"
    status = spool.get("status")
    if status == "pending":
        return f"Spool {spool_id} pending (not yet started)"
    elif status == "running":
        pid = spool.get("pid")
        if pid and not _is_pid_alive(pid):
            _check_and_finalize_spool(spool_id)
            spool = _read_spool(spool_id)
            if spool.get("status") == "complete":
                return spool.get("result", "No result")
            elif spool.get("status") == "error":
                return f"Spool {spool_id} failed: {spool.get('error', 'Unknown error')}"
        return f"Spool {spool_id} still running: {spool.get('prompt', '')[:50]}..."
    elif status == "complete":
        return spool.get("result", "No result")
    else:
        return f"Spool {spool_id} failed: {spool.get('error', 'Unknown error')}"


@mcp.tool()
async def unspool(spool_id: str) -> str:
    """
    Get the result of a background spin task.
    """
    import asyncio

    return await asyncio.to_thread(_unspool_sync, spool_id)


def _spools_sync() -> str:
    """Synchronous implementation of spools."""
    _recover_orphans()
    all_spools = _list_spools()
    return json.dumps(
        {
            spool["id"]: {
                "status": spool.get("status"),
                "prompt": spool.get("prompt", "")[:100],
                "created_at": spool.get("created_at"),
                "session_id": spool.get("session_id"),
            }
            for spool in all_spools
        },
        indent=2,
    )


@mcp.tool()
async def spools() -> str:
    """
    List all spools (running and completed).

    Returns:
        JSON object with spool statuses
    """
    import asyncio

    return await asyncio.to_thread(_spools_sync)


def _respin_sync(session_id: str, prompt: str) -> str:
    """Synchronous implementation of respin."""
    # Check concurrency limit
    if _count_running() >= MAX_CONCURRENT:
        return f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

    spool_id = str(uuid.uuid4())[:8]

    # Try to resume with session_id first
    # If that fails (session expired), fall back to transcript injection
    cmd = ["claude", "-p", prompt, "--resume", session_id, "--output-format", "json"]

    cwd = os.getcwd()

    # Check if we have a transcript for this session
    original_spool = _find_spool_by_session(session_id)
    transcript_available = False
    if original_spool:
        transcript_path = _get_transcript_path(original_spool["id"])
        transcript_available = transcript_path.exists()

    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": f"Continue {session_id}: {prompt}",
        "result": None,
        "session_id": session_id,
        "working_dir": cwd,
        "allowed_tools": None,
        "system_prompt": None,
        "transcript_fallback_available": transcript_available,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "cost": None,
        "error": None,
    }

    _write_spool(spool_id, spool)

    # Spawn detached process
    pid = _spawn_detached(spool_id, cmd, cwd)

    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor
    monitor = threading.Thread(target=_monitor_spool, args=(spool_id,), daemon=True)
    monitor.start()

    return spool_id


@mcp.tool()
async def respin(
    session_id: str,
    prompt: str,
) -> str:
    """
    Continue an existing Claude Code session with a new message.
    Returns immediately with spool_id.

    If the session has expired on Claude's end, automatically falls back
    to transcript injection to recreate context.

    Args:
        session_id: The session ID to continue
        prompt: The follow-up message/task

    Returns:
        spool_id to check result later
    """
    return await asyncio.to_thread(_respin_sync, session_id, prompt)


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
    ids = [s.strip() for s in spool_ids.split(",")]
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
                if spool.get("status") == "complete":
                    return spool.get("result", "No result")
                elif spool.get("status") == "error":
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
                if spool.get("status") == "complete":
                    results[spool_id] = spool.get("result", "No result")
                    pending.remove(spool_id)
                elif spool.get("status") == "error":
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

    if spool.get("status") != "running":
        return f"Spool {spool_id} is not running (status: {spool.get('status')})"

    pid = spool.get("pid")

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
    spool["status"] = "error"
    spool["error"] = "Cancelled by user"
    spool["completed_at"] = datetime.now().isoformat()
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
        spool_id = spool.get("id", "unknown")
        prompt = spool.get("prompt", "") or ""
        result = spool.get("result", "") or ""

        # Convert result to string if it's a dict
        if isinstance(result, dict):
            result = json.dumps(result)

        prompt_match = query_lower in prompt.lower() if field in ("prompt", "both") else False
        result_match = query_lower in result.lower() if field in ("result", "both") else False

        if prompt_match or result_match:
            match_info = {
                "id": spool_id,
                "status": spool.get("status"),
                "created_at": spool.get("created_at"),
            }

            # Add context snippets
            if prompt_match:
                idx = prompt.lower().find(query_lower)
                start = max(0, idx - 30)
                end = min(len(prompt), idx + len(query) + 30)
                match_info["prompt_match"] = f"...{prompt[start:end]}..."

            if result_match:
                idx = result.lower().find(query_lower)
                start = max(0, idx - 50)
                end = min(len(result), idx + len(query) + 50)
                match_info["result_match"] = f"...{result[start:end]}..."

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
            "1h": timedelta(hours=1),
            "6h": timedelta(hours=6),
            "12h": timedelta(hours=12),
            "1d": timedelta(days=1),
            "7d": timedelta(days=7),
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
        if status != "all" and spool.get("status") != status:
            continue

        # Time filter
        if since_cutoff:
            created_str = spool.get("created_at")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    if created < since_cutoff:
                        continue
                except ValueError:
                    continue

        filtered.append(spool)

    # Sort by created_at descending
    filtered.sort(key=lambda s: s.get("created_at", ""), reverse=True)

    # Apply limit
    filtered = filtered[:limit]

    # Format output
    results = []
    for spool in filtered:
        result_text = spool.get("result", "")
        if isinstance(result_text, dict):
            result_text = json.dumps(result_text)

        results.append(
            {
                "id": spool.get("id"),
                "status": spool.get("status"),
                "prompt": spool.get("prompt", "")[:100],
                "result": result_text[:500] if result_text else None,
                "created_at": spool.get("created_at"),
                "session_id": spool.get("session_id"),
            }
        )

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
        spool_id = spool.get("id", "unknown")
        result = spool.get("result", "") or ""

        # Convert result to string if it's a dict
        if isinstance(result, dict):
            result = json.dumps(result)

        found = regex.findall(result)
        if found:
            # Get unique matches and limit to first 10
            unique_matches = list(dict.fromkeys(found))[:10]
            matches.append(
                {
                    "id": spool_id,
                    "status": spool.get("status"),
                    "prompt": spool.get("prompt", "")[:80],
                    "matches": unique_matches,
                    "match_count": len(found),
                }
            )

    if not matches:
        return f"No results matching pattern '{pattern}'"

    return json.dumps(matches, indent=2)


@mcp.tool()
async def spool_peek(spool_id: str, lines: int = 50) -> str:
    """
    See partial output of a running spool.

    Useful for debugging stuck spools or monitoring progress.

    Args:
        spool_id: The spool_id to peek at
        lines: Number of lines to return from the end (default: 50)

    Returns:
        Last N lines of stdout, or error if spool not found

    Example:
        spool_peek("abc123")          # see last 50 lines
        spool_peek("abc123", lines=100)  # see last 100 lines
    """
    spool = _read_spool(spool_id)
    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    stdout_path = _get_output_path(spool_id)
    if not stdout_path.exists():
        return f"No output yet for spool {spool_id}"

    try:
        with open(stdout_path, "r") as f:
            all_lines = f.readlines()

        if not all_lines:
            return f"Output file exists but is empty for spool {spool_id}"

        # Get last N lines
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        status = spool.get("status", "unknown")

        header = f"[spool {spool_id} - {status} - {len(all_lines)} total lines, showing last {len(tail)}]\n"
        return header + "".join(tail)
    except Exception as e:
        return f"Error reading output: {e}"


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
        prompt=spool.get("prompt", ""),
        permission=spool.get("permission"),
        shard=bool(spool.get("shard")),
        system_prompt=spool.get("system_prompt"),
        working_dir=spool.get("working_dir"),
        allowed_tools=spool.get("allowed_tools"),
        tags=",".join(spool.get("tags", [])) if spool.get("tags") else None,
    )


def _get_shard_commit_status(spool: dict) -> Optional[str]:
    """
    Determine commit status for a shard spool.

    Returns:
        - None: No shard
        - "merged": Already merged
        - "has_commit": Has commits on branch
        - "uncommitted": Has uncommitted changes
        - "conflict": Would have merge conflicts
        - "no_worktree": Worktree doesn't exist
    """
    shard_info = spool.get("shard")
    if not shard_info:
        return None

    # Check if already merged
    if shard_info.get("merged"):
        return "merged"

    worktree_path = shard_info.get("worktree_path")
    if not worktree_path or not Path(worktree_path).exists():
        return "no_worktree"

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        has_uncommitted = bool(result.stdout.strip()) if result.returncode == 0 else False

        # Check for commits ahead of master
        result = subprocess.run(
            ["git", "rev-list", "--count", "master..HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10,
        )
        commits_ahead = int(result.stdout.strip()) if result.returncode == 0 else 0

        if has_uncommitted:
            return "uncommitted"

        if commits_ahead == 0:
            return "no_changes"

        # Check for potential merge conflicts
        main_repo = Path(worktree_path).parent.parent
        branch_name = shard_info.get("branch_name")
        if branch_name:
            result = subprocess.run(
                ["git", "merge-tree", "--write-tree", "master", branch_name],
                capture_output=True,
                text=True,
                cwd=str(main_repo),
                timeout=10,
            )
            # Non-zero exit means conflicts
            if result.returncode != 0:
                return "conflict"

        return "has_commit"

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return "unknown"


def _get_shard_change_stats(spool: dict) -> Optional[dict]:
    """
    Get stats about changes in a shard.

    Returns dict with files_changed, insertions, deletions or None.
    """
    shard_info = spool.get("shard")
    if not shard_info:
        return None

    worktree_path = shard_info.get("worktree_path")
    if not worktree_path or not Path(worktree_path).exists():
        return None

    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "--stat-width=1000", "master...HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        # Parse the summary line: " X files changed, Y insertions(+), Z deletions(-)"
        lines = result.stdout.strip().split("\n")
        if not lines:
            return None

        summary = lines[-1]
        stats = {"files_changed": 0, "insertions": 0, "deletions": 0}

        files_match = re.search(r"(\d+) files? changed", summary)
        ins_match = re.search(r"(\d+) insertions?\(\+\)", summary)
        del_match = re.search(r"(\d+) deletions?\(-\)", summary)

        if files_match:
            stats["files_changed"] = int(files_match.group(1))
        if ins_match:
            stats["insertions"] = int(ins_match.group(1))
        if del_match:
            stats["deletions"] = int(del_match.group(1))

        return stats

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return None


def _spool_dashboard_sync() -> str:
    """Synchronous implementation of spool_dashboard."""
    _recover_orphans()
    all_spools = _list_spools()
    now = datetime.now()
    hour_ago = now - timedelta(hours=1)

    # Count by status
    running = []
    complete_last_hour = []
    errors = []

    for spool in all_spools:
        status = spool.get("status")
        if status == "running":
            running.append(spool)
        elif status == "error":
            errors.append(spool)
        elif status == "complete":
            completed_at = spool.get("completed_at")
            if completed_at:
                try:
                    completed_dt = datetime.fromisoformat(completed_at)
                    if completed_dt >= hour_ago:
                        complete_last_hour.append(spool)
                except ValueError:
                    pass

    # Build recent completions list (last hour, sorted by completion time)
    recent = []
    for spool in sorted(complete_last_hour, key=lambda s: s.get("completed_at", ""), reverse=True)[:10]:
        spool_id = spool.get("id")
        completed_at = spool.get("completed_at")

        # Calculate age
        age_str = "unknown"
        if completed_at:
            try:
                completed_dt = datetime.fromisoformat(completed_at)
                age_mins = int((now - completed_dt).total_seconds() / 60)
                age_str = f"{age_mins}m ago"
            except ValueError:
                pass

        # Get task name (first 60 chars of prompt)
        prompt = spool.get("prompt", "")[:60]
        if len(spool.get("prompt", "")) > 60:
            prompt += "..."

        commit_status = _get_shard_commit_status(spool)

        recent.append(
            {
                "spool_id": spool_id,
                "task": prompt,
                "status": "complete",
                "age": age_str,
                "commit_status": commit_status,
            }
        )

    # Needing attention: shards with uncommitted changes or large changesets
    needing_attention = []
    for spool in all_spools:
        if spool.get("status") != "complete":
            continue

        shard_info = spool.get("shard")
        if not shard_info:
            continue

        commit_status = _get_shard_commit_status(spool)
        needs_attention = False
        reason = None

        if commit_status == "uncommitted":
            needs_attention = True
            reason = "uncommitted changes"
        elif commit_status == "conflict":
            needs_attention = True
            reason = "merge conflict"

        # Check for large changes
        if commit_status == "has_commit":
            stats = _get_shard_change_stats(spool)
            if stats:
                total_changes = stats.get("insertions", 0) + stats.get("deletions", 0)
                if total_changes > 500 or stats.get("files_changed", 0) > 10:
                    needs_attention = True
                    reason = f"large changeset ({stats['files_changed']} files, +{stats['insertions']}/-{stats['deletions']})"

        if needs_attention:
            needing_attention.append(
                {
                    "spool_id": spool.get("id"),
                    "task": spool.get("prompt", "")[:60],
                    "commit_status": commit_status,
                    "reason": reason,
                    "worktree": shard_info.get("worktree_path"),
                }
            )

    # Also add errors from last hour as needing attention
    for spool in errors:
        created_at = spool.get("created_at")
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at)
                if created_dt >= hour_ago:
                    needing_attention.append(
                        {
                            "spool_id": spool.get("id"),
                            "task": spool.get("prompt", "")[:60],
                            "commit_status": None,
                            "reason": f"error: {spool.get('error', 'unknown')[:50]}",
                        }
                    )
            except ValueError:
                pass

    dashboard = {
        "summary": {
            "running": len(running),
            "complete_last_hour": len(complete_last_hour),
            "errors": len(errors),
            "total_spools": len(all_spools),
        },
        "running": [
            {
                "spool_id": s.get("id"),
                "task": s.get("prompt", "")[:60],
                "started": s.get("created_at"),
            }
            for s in running
        ],
        "recent_completions": recent,
        "needing_attention": needing_attention,
    }

    return json.dumps(dashboard, indent=2)


@mcp.tool()
async def spool_dashboard() -> str:
    """
    Single-view dashboard of spool status for QMs.

    Shows:
    - Summary counts: running, complete (last hour), errors
    - Currently running spools with task and start time
    - Recent completions with spool_id, task, age, commit status
    - Items needing attention: uncommitted changes, large changesets, conflicts

    Commit status values:
    - uncommitted: Has uncommitted changes in worktree
    - has_commit: Has commits ready for merge
    - merged: Already merged to master
    - conflict: Would have merge conflicts
    - no_worktree: Worktree no longer exists
    - None: Not a shard spool

    Returns:
        JSON dashboard with summary, running, recent_completions, needing_attention

    Example:
        dashboard = spool_dashboard()  # Get full status overview
    """
    return await asyncio.to_thread(_spool_dashboard_sync)


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
        "total": len(all_spools),
        "by_status": {},
        "oldest": None,
        "newest": None,
    }

    for spool in all_spools:
        # Count by status
        status = spool.get("status", "unknown")
        stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

        # Track time range
        created = spool.get("created_at")
        if created:
            if not stats["oldest"] or created < stats["oldest"]:
                stats["oldest"] = created
            if not stats["newest"] or created > stats["newest"]:
                stats["newest"] = created

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
        ids = [s.strip() for s in spool_ids.split(",")]
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
    spools_to_export.sort(key=lambda s: s.get("created_at", ""))

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
            result = spool.get("result", "")
            if isinstance(result, dict):
                result = json.dumps(result, indent=2)
            lines.append(f"```\n{result}\n```")
            lines.append("")
            lines.append("---")
            lines.append("")
        content = "\n".join(lines)
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


def _shard_status_sync(spool_id: str) -> str:
    """Synchronous implementation of shard_status."""
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    shard_info = spool.get("shard")
    if not shard_info:
        return f"Spool {spool_id} has no shard (was not run with shard=True)"

    worktree_path = shard_info.get("worktree_path")
    if not worktree_path or not Path(worktree_path).exists():
        return json.dumps(
            {"spool_id": spool_id, "shard": shard_info, "exists": False, "message": "Worktree no longer exists"},
            indent=2,
        )

    status_info = {
        "spool_id": spool_id,
        "shard": shard_info,
        "exists": True,
        "spool_status": spool.get("status"),
    }

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.returncode == 0:
            status_info["git_changes"] = result.stdout.strip().split("\n") if result.stdout.strip() else []

        result = subprocess.run(
            ["git", "rev-list", "--count", "master..HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10,
        )
        if result.returncode == 0:
            status_info["commits_ahead"] = int(result.stdout.strip())

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        status_info["git_error"] = "Failed to get git status"

    return json.dumps(status_info, indent=2)


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
    import asyncio

    return await asyncio.to_thread(_shard_status_sync, spool_id)


@mcp.tool()
async def shard_merge(spool_id: str, keep_branch: bool = False, caller_cwd: str | None = None) -> str:
    """
    Merge a shard's changes back to master and clean up the worktree.

    The spool must be complete (not running). Changes are merged to master
    using a merge commit.

    Args:
        spool_id: The spool_id with a shard to merge
        keep_branch: Keep the branch after merge (default: delete)
        caller_cwd: Optional current working directory of the caller. If provided
            and the cwd is inside the worktree, the operation will be refused to
            prevent breaking the caller's shell.

    Returns:
        Success or error message

    Example:
        shard_merge("abc123")  # merge and cleanup
    """
    if not caller_cwd:
        return "Error: caller_cwd required. Pass your current working directory to prevent deleting a worktree you're inside of."

    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get("status") == "running":
        return f"Error: Spool {spool_id} is still running. Wait for completion."

    shard_info = spool.get("shard")
    if not shard_info:
        return f"Error: Spool {spool_id} has no shard"

    worktree_path = shard_info.get("worktree_path")
    branch_name = shard_info.get("branch_name")

    if not worktree_path or not Path(worktree_path).exists():
        return f"Error: Worktree no longer exists: {worktree_path}"

    # Check if caller's cwd is inside the worktree (would break their shell)
    if caller_cwd:
        caller_path = Path(caller_cwd).resolve()
        wt_path = Path(worktree_path).resolve()
        if caller_path == wt_path or wt_path in caller_path.parents:
            main_repo = wt_path.parent.parent
            return f"Error: Cannot delete worktree - your working directory is inside it. Run `cd {main_repo}` first."

    # Check if any running spool has working_dir inside this worktree
    wt_path = Path(worktree_path).resolve()
    for other in _list_spools():
        if other.get("status") == "running" and other.get("id") != spool_id:
            other_wd = other.get("working_dir", "")
            if other_wd and Path(other_wd).resolve() == wt_path:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent  # worktrees/name -> repo

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.stdout.strip():
            return f"Error: Shard has uncommitted changes. Commit or discard them first."

        # Merge branch to master from main repo
        result = subprocess.run(
            ["git", "merge", branch_name, "--no-ff", "-m", f'Merge shard {spool_id}: {spool.get("prompt", "")[:50]}'],
            capture_output=True,
            text=True,
            cwd=str(main_repo),
            timeout=30,
        )
        if result.returncode != 0:
            return f"Error: Merge failed: {result.stderr}"

        # Cleanup shard
        _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch)

        # Update spool record
        spool["shard"]["merged"] = True
        spool["shard"]["merged_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)

        # Auto-close any tender folios for this worktree
        worktree_name = shard_info.get("shard_id") or Path(worktree_path).name
        tender_result = _close_tender_folios(worktree_name)

        msg = f"Successfully merged shard {spool_id} to master"
        if tender_result:
            msg += f". {tender_result}"
        return msg

    except subprocess.TimeoutExpired:
        return "Error: Git operation timed out"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


@mcp.tool()
async def shard_abandon(spool_id: str, keep_branch: bool = False, caller_cwd: str | None = None) -> str:
    """
    Abandon a shard, removing the worktree without merging.

    Use this when a shard's work is no longer needed.

    Args:
        spool_id: The spool_id with a shard to abandon
        keep_branch: Keep the branch for later (default: delete)
        caller_cwd: Optional current working directory of the caller. If provided
            and the cwd is inside the worktree, the operation will be refused to
            prevent breaking the caller's shell.

    Returns:
        Success or error message

    Example:
        shard_abandon("abc123")  # discard shard
    """
    if not caller_cwd:
        return "Error: caller_cwd required. Pass your current working directory to prevent deleting a worktree you're inside of."

    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    shard_info = spool.get("shard")
    if not shard_info:
        return f"Error: Spool {spool_id} has no shard"

    worktree_path = shard_info.get("worktree_path")

    if not worktree_path:
        return f"Error: No worktree path in shard info"

    # Check if caller's cwd is inside the worktree (would break their shell)
    if caller_cwd:
        caller_path = Path(caller_cwd).resolve()
        wt_path = Path(worktree_path).resolve()
        if wt_path.exists() and (caller_path == wt_path or wt_path in caller_path.parents):
            main_repo = wt_path.parent.parent
            return f"Error: Cannot delete worktree - your working directory is inside it. Run `cd {main_repo}` first."

    # Check if any OTHER running spool has working_dir inside this worktree
    wt_path = Path(worktree_path).resolve()
    for other in _list_spools():
        if other.get("status") == "running" and other.get("id") != spool_id:
            other_wd = other.get("working_dir", "")
            if other_wd and Path(other_wd).resolve() == wt_path:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent

    # If spool is running, kill it first
    if spool.get("status") == "running":
        pid = spool.get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        spool["status"] = "error"
        spool["error"] = "Shard abandoned"
        spool["completed_at"] = datetime.now().isoformat()

    # Cleanup shard
    success = _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch)

    if success:
        spool["shard"]["abandoned"] = True
        spool["shard"]["abandoned_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        return f"Abandoned shard {spool_id}" + (" (branch kept)" if keep_branch else "")
    else:
        return f"Warning: Shard cleanup may have been incomplete for {spool_id}"


@mcp.tool()
async def triage(worktree_path: str) -> str:
    """
    Assess orphan worktree and create tender with recommendation.

    Spins an agent to review the worktree, assess the work, and create
    a tender with status and confidence score.

    Args:
        worktree_path: Path to the worktree to triage

    Returns:
        spool_id for the triage agent
    """
    # Validate path exists and is a git worktree
    if not Path(worktree_path).exists():
        return f"Error: Path does not exist: {worktree_path}"

    # Extract worktree name for tender command
    worktree_name = Path(worktree_path).name

    prompt = f"""## Worktree Triage

Assess the work in this worktree and create a tender.

**Worktree:** {worktree_path}
**Name:** {worktree_name}

### Steps:

1. Run `git log --oneline master..HEAD` to see commits
2. Run `git diff --stat master` to see scope of changes
3. Run `git status` to see uncommitted work
4. Read key files if needed to understand intent
5. If there are uncommitted changes worth keeping, commit them:
   `git add -A && git commit -m "Triage: <description of changes>"`

### Then tender with your assessment:

```bash
skein shard tender {worktree_name} --status <status> --confidence <1-10> --summary "<summary>"
```

**Status options:**
- `complete` - Work is done, ready for merge consideration
- `incomplete` - Partial work, may be salvageable
- `abandoned` - Nothing useful, recommend discard (still tender it for the record)

**Confidence scale (merge risk):**
- 10: Safe, additive, isolated (auto-merge candidate)
- 7-9: Small changes, low-risk, clear intent
- 4-6: Moderate changes, needs review
- 1-3: Big refactor, critical path, risky

Always tender something - even abandoned work should be tendered with a note explaining why.

If status is `incomplete` and work is worth continuing, create a brief for the remaining work."""

    return await asyncio.to_thread(
        _spin_sync,
        prompt,
        "careful",  # permission - needs git, skein commands
        False,  # shard
        None,  # system_prompt
        worktree_path,  # working_dir
        None,  # allowed_tools
        "triage",  # tags
        None,  # model
        None,  # timeout
        True,  # skeinless
    )


@mcp.tool()
async def spool_info(spool_id: str) -> str:
    """
    Get detailed information about a spool for debugging.

    Shows complete spool metadata including session_id, transcript availability,
    working_dir, timestamps, and other internal state.

    Args:
        spool_id: The spool_id to inspect

    Returns:
        JSON with full spool details

    Example:
        spool_info("abc123")  # Get complete spool info
    """
    spool = _read_spool(spool_id)
    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    # Add transcript availability info
    if spool.get("session_id"):
        original_spool = _find_spool_by_session(spool["session_id"])
        if original_spool:
            transcript_path = _get_transcript_path(original_spool["id"])
            spool["_transcript_available"] = transcript_path.exists()
            if transcript_path.exists():
                try:
                    spool["_transcript_size"] = len(transcript_path.read_text())
                except IOError:
                    spool["_transcript_size"] = "error"

    return json.dumps(spool, indent=2)


@mcp.tool()
async def spindle_reload() -> str:
    """
    Restart spindle to pick up code changes.

    Uses systemctl --user restart spindle. Requires spindle systemd service.

    Returns:
        Status message
    """
    # Check if systemd service exists (even if not running)
    result = subprocess.run(
        ["systemctl", "--user", "list-unit-files", "spindle.service"], capture_output=True, text=True
    )

    if "spindle.service" not in result.stdout:
        return "Error: spindle.service not found. Restart manually."

    # Check if currently active
    is_active = subprocess.run(["systemctl", "--user", "is-active", "spindle"], capture_output=True).returncode == 0

    def delayed_restart():
        time.sleep(0.5)  # Give time for response to be sent
        if is_active:
            subprocess.run(["systemctl", "--user", "restart", "spindle"])
        else:
            subprocess.run(["systemctl", "--user", "start", "spindle"])

    restart_thread = threading.Thread(target=delayed_restart, daemon=True)
    restart_thread.start()

    return "Restarting via systemd..." if is_active else "Starting via systemd..."


def main():
    import sys
    import argparse
    import traceback
    import atexit

    parser = argparse.ArgumentParser(description="Spindle MCP server")
    subparsers = parser.add_subparsers(dest="command")

    # serve command (default)
    serve_parser = subparsers.add_parser("serve", help="Run the MCP server")
    serve_parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    serve_parser.add_argument("--port", type=int, default=8002, help="HTTP port (default: 8002)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")

    # start command - start via systemd or background
    start_parser = subparsers.add_parser("start", help="Start spindle (via systemd if available)")

    # reload command - restart spindle
    reload_parser = subparsers.add_parser("reload", help="Reload spindle to pick up code changes")

    # status command
    status_parser = subparsers.add_parser("status", help="Check spindle status")

    # Legacy flags for backward compat
    parser.add_argument("--http", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8002, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Handle subcommands
    if args.command == "start":
        # Check if systemd service exists
        result = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "spindle.service"], capture_output=True, text=True
        )
        if "spindle.service" in result.stdout:
            subprocess.run(["systemctl", "--user", "start", "spindle"])
            print("Started via systemd")
        else:
            # Start in background
            subprocess.Popen(
                [sys.executable, __file__, "serve", "--http"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("Started in background (no systemd service found)")
        sys.exit(0)

    elif args.command == "reload":
        # Check if systemd service exists
        result = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "spindle.service"], capture_output=True, text=True
        )
        if "spindle.service" in result.stdout:
            subprocess.run(["systemctl", "--user", "restart", "spindle"])
            print("Restarted via systemd")
        else:
            print("No systemd service. Kill and run: spindle start")
        sys.exit(0)

    elif args.command == "status":
        result = subprocess.run(["curl", "-s", "http://127.0.0.1:8002/health"], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("Not running")
        sys.exit(0)

    # Default to serve if no command or using legacy --http flag
    if args.command is None and not args.http:
        parser.print_help()
        sys.exit(0)

    log_path = Path.home() / ".spindle" / "spindle.log"

    def log(msg: str):
        with open(log_path, "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")

    # Ensure spindle directory exists
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Log startup
    mode = f"HTTP {args.host}:{args.port}" if args.http else "stdio"
    log(f"STARTUP pid={os.getpid()} mode={mode}")

    # Log uncaught exceptions
    def exception_handler(exc_type, exc_value, exc_tb):
        log(f"EXCEPTION {exc_type.__name__}: {exc_value}")
        with open(log_path, "a") as f:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_handler

    # Log signals
    def signal_handler(signum, frame):
        log(f"SIGNAL received: {signum} ({signal.Signals(signum).name})")
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal_handler)

    # Log exit
    def exit_handler():
        log("EXIT")

    atexit.register(exit_handler)

    log("STARTING mcp.run()")
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()
    log("FINISHED mcp.run()")


if __name__ == "__main__":
    main()
