#!/usr/bin/env python3
"""
Spindle - MCP server for Claude Code to Claude Code delegation.

Lets CC agents spawn other CC agents, all using Max subscription credits.

Usage:
    # Add to ~/.claude/mcp.json or use --mcp-config
    {
        "mcpServers": {
            "spindle": {
                "command": "python",
                "args": ["/path/to/mcp_servers/spindle.py"]
            }
        }
    }

Tools:
    spin - Spawn a new Claude Code agent to handle a task
    spin_continue - Continue an existing Claude Code session
    spin_sessions - List recent Claude Code sessions
"""

import asyncio
import json
import subprocess
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP("spindle")


@mcp.tool()
async def spin(
    prompt: str,
    system_prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    timeout: int = 300
) -> str:
    """
    Spawn a Claude Code agent to handle a task. Returns the result.

    Use this to delegate work to another CC instance. The child agent
    runs to completion and returns its response.

    Args:
        prompt: The task/question for the agent
        system_prompt: Optional system prompt to configure the agent's behavior
        working_dir: Directory for the agent to work in (defaults to current)
        allowed_tools: Restrict tools (e.g. "Read,Write,Bash(git:*)")
        timeout: Max seconds to wait (default 300)

    Returns:
        The agent's response text

    Example:
        spin("Research the history of the Python GIL")
        spin("Review this code for bugs", working_dir="/path/to/project")
    """
    cmd = ['claude', '-p', prompt, '--output-format', 'json']

    if system_prompt:
        cmd.extend(['--system-prompt', system_prompt])

    if allowed_tools:
        cmd.extend(['--allowedTools', allowed_tools])

    cwd = working_dir or os.getcwd()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout
        )

        if proc.returncode != 0:
            return f"Error (exit {proc.returncode}): {stderr.decode()}"

        # Parse JSON output
        try:
            data = json.loads(stdout.decode())
            # Claude Code JSON format has 'result' field
            return data.get('result', stdout.decode())
        except json.JSONDecodeError:
            return stdout.decode()

    except asyncio.TimeoutError:
        return f"Error: Agent timed out after {timeout} seconds"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def spin_continue(
    session_id: str,
    prompt: str,
    timeout: int = 300
) -> str:
    """
    Continue an existing Claude Code session with a new message.

    Args:
        session_id: The session ID to continue (from spin_sessions)
        prompt: The follow-up message/task
        timeout: Max seconds to wait (default 300)

    Returns:
        The agent's response text
    """
    cmd = [
        'claude', '-p', prompt,
        '--resume', session_id,
        '--output-format', 'json'
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout
        )

        if proc.returncode != 0:
            return f"Error (exit {proc.returncode}): {stderr.decode()}"

        try:
            data = json.loads(stdout.decode())
            return data.get('result', stdout.decode())
        except json.JSONDecodeError:
            return stdout.decode()

    except asyncio.TimeoutError:
        return f"Error: Agent timed out after {timeout} seconds"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def spin_sessions(limit: int = 5) -> str:
    """
    List recent Claude Code sessions.

    Args:
        limit: Max sessions to return (default 5)

    Returns:
        JSON list of recent sessions with IDs and metadata
    """
    # Claude Code stores sessions in ~/.claude/projects/
    projects_dir = Path.home() / ".claude" / "projects"

    if not projects_dir.exists():
        return json.dumps({"sessions": [], "message": "No sessions found"})

    sessions = []

    # Walk through project directories
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        # Check for session files
        sessions_file = project_dir / "sessions.json"
        if sessions_file.exists():
            try:
                with open(sessions_file) as f:
                    data = json.load(f)
                    for session in data.get('sessions', [])[-limit:]:
                        sessions.append({
                            'id': session.get('id'),
                            'project': project_dir.name,
                            'created': session.get('created'),
                            'messages': session.get('messageCount', 0)
                        })
            except Exception:
                pass

    # Sort by created time, newest first
    sessions.sort(key=lambda x: x.get('created', ''), reverse=True)

    return json.dumps({
        "sessions": sessions[:limit],
        "count": len(sessions)
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
