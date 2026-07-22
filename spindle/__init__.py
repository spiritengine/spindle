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
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Generator, Optional, Tuple

from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("spindle")

# Set up logging
logger = logging.getLogger(__name__)

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


# Storage directory. SPINDLE_HOME is honored (like the other config env vars
# below) so tests can redirect the whole store to a tmp dir before import and
# never touch the real ~/.spindle, even from an escaped monitor thread.
SPINDLE_DIR = Path(os.environ.get("SPINDLE_HOME", str(Path.home() / ".spindle"))) / "spools"

# Canonical location for lodged profiles (folder-per-profile, each holding a
# profile.json). Sits beside the spool store and honors SPINDLE_HOME the same
# way, so tests redirect both with one env var and never read real profiles.
SPINDLE_PROFILES_DIR = Path(os.environ.get("SPINDLE_HOME", str(Path.home() / ".spindle"))) / "profiles"

# Built-in harness names. These always win over a same-named lodged profile.
BUILTIN_HARNESSES = {"claude-code", "codex", "gemini", "kimi"}

# Live subprocess handles keyed by spool_id, populated by _spawn_detached so
# finalization can capture the child's exit code. Process-local; not persisted.
_PROC_HANDLES: Dict[str, "subprocess.Popen"] = {}
_SPOOL_MONITORS: set[str] = set()
_SPOOL_MONITORS_LOCK = threading.Lock()

# Set while a drain-then-restart is queued (spindle_reload without force), so a
# second reload call doesn't stack another waiter. Process-local. The check-then-
# set in spindle_reload is race-free only because the tool has no await between
# reading and setting this; keep it that way (don't make the systemctl calls
# awaitable) or two waiters could be spawned.
_reload_pending = False

# Concurrency limit (configurable via env var)
MAX_CONCURRENT = int(os.environ.get("SPINDLE_MAX_CONCURRENT", "15"))

# Result-size budgeting for unspool. Spool results are bimodal: most are small
# (~6KB median) but a long tail runs 130KB-280KB and would flood the caller's
# context. When a result exceeds UNSPOOL_MAX_CHARS, unspool() returns the head
# and tail with a breadcrumb explaining how to pull the full text, page through
# it, write it to a file, or search it. Full text always stays in the spool JSON;
# this only affects the default read. full=True / offset / limit bypass it.
UNSPOOL_MAX_CHARS = int(os.environ.get("SPINDLE_UNSPOOL_MAX_CHARS", "50000"))
UNSPOOL_HEAD_CHARS = int(os.environ.get("SPINDLE_UNSPOOL_HEAD_CHARS", "12000"))
UNSPOOL_TAIL_CHARS = int(os.environ.get("SPINDLE_UNSPOOL_TAIL_CHARS", "12000"))

# Timeout for pending spools that never got a PID (seconds)
PENDING_SPAWN_TIMEOUT = 60

# Poll interval for monitoring detached processes
MONITOR_POLL_INTERVAL = 2  # seconds
SPOOL_TERMINAL_LOCK_TIMEOUT = 5.0  # seconds

# Poll interval for draining the queue before a reload (spindle_reload)
RELOAD_DRAIN_POLL_INTERVAL = 5  # seconds

# Tags that mark a spool as a review/fell pass. Review spools get a soft default
# timeout (DEFAULT_REVIEW_TIMEOUT) when the caller didn't pass an explicit one.
# Typical reviews finish in 10-30 min; 90 min caps runaway wedged spools.
# "review" is the literal tag; fell-rN rounds are matched by _is_review_tag()
# so fell has no iteration cap (fell-r6+ spools are covered without enumeration).
REVIEW_TAGS = {"review"}
DEFAULT_REVIEW_TIMEOUT = int(os.environ.get("SPINDLE_REVIEW_TIMEOUT", str(90 * 60)))


def _is_review_tag(tag: str) -> bool:
    """Return True if tag marks a spool as a review/fell pass.

    "review" is matched literally; "fell-rN" (any N) is matched by regex so
    the fell process can iterate past r5 without losing the soft timeout.
    """
    return tag in REVIEW_TAGS or bool(re.match(r"^fell-r\d+$", tag))


# Claude Code stores background-task state here: ~/.claude/tasks/<session_id>/<n>.json
CLAUDE_TASKS_DIR = Path.home() / ".claude" / "tasks"

# Permission profiles for tool restrictions
# These map to Claude Code's --allowedTools flag
# Profiles ending with "+shard" auto-enable shard isolation
RESEARCH_TOOLS = "Read,Grep,Glob,WebFetch,WebSearch,Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(git status:*),Bash(git log:*),Bash(git diff:*),Bash(curl:*),Bash(jq:*),Bash(skein:*)"

# The one tight, inspectable, no-exec tier: Read/Grep/Glob plus a few safe
# read-only Bash rules — no python, no find, no write. `readonly` and its alias
# `manual` are the only Claude tiers whose capability is still governed by an
# allowlist; every other tier is classifier-vetted (auto) or bwrap-contained
# (shard/full).
READONLY_TOOLS = "Read,Grep,Glob,Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(git status:*),Bash(git log:*),Bash(git diff:*),Bash(skein:*)"

# Claude-harness permission profiles map to an --allowedTools string, or None for
# "no allowlist" (the classifier or bwrap governs instead). `careful` is now an
# alias of `auto`: it resolves to None here and selects --permission-mode auto,
# where Claude Code vets each tool call server-side on intent. The old careful
# allowlist gated capability on command PHRASING, not security — it already
# permitted arbitrary python/npm/etc, so `python3 -c ...` ran while
# `PYTHONPATH=x python3 -c ...` or `.venv/bin/python` silently fell through to a
# denial, degrading a reviewer to static analysis without saying so. `auto`
# removes the phrasing gate entirely; the deleted PINNED_INTERPRETERS/VENV_TOOLS
# scar tissue existed only to paper over it.
PERMISSION_PROFILES = {
    "readonly": READONLY_TOOLS,
    "manual": READONLY_TOOLS,  # exact alias of readonly — the tight/manual tier
    # NOTE: no readonly+shard / manual+shard. The readonly/manual tier has no
    # write tools, so pairing it with a shard (an isolated worktree for making
    # changes) is incoherent. The pairing is rejected on the resolved (tier,
    # use_shard) pair at every launch chokepoint (see _readonly_shard_conflict_error),
    # so no spelling — string, shard=True flag, or a stored/respun form — resolves it.
    "careful": None,  # alias of auto: no allowlist, classifier vets each call
    "careful+shard": None,  # auto-vetted; bypassPermissions inside a bwrap-contained shard
    "research": RESEARCH_TOOLS,
    "full": None,  # None means no restrictions
    # Shard variants - same permissions but auto-enable worktree isolation
    "shard": None,  # Full permissions + shard isolation (common combo)
    "research+shard": RESEARCH_TOOLS,
    # Classifier-vetted autonomous mode — CC vets each tool call server-side.
    # No allowedTools restriction: the classifier governs calls dynamically.
    "auto": None,
    "auto+shard": None,  # Same + worktree isolation
}

# Cache for SKEIN availability check (per-directory)
_skein_available: Dict[str, bool] = {}


def _has_skein(working_dir: str) -> bool:
    """
    Check if SKEIN is available for the given project directory.
    Results are cached per-directory for performance.

    Uses 'skein health' which checks git repo, .skein/ dir, and server.

    Args:
        working_dir: The directory to check for SKEIN availability
    """
    global _skein_available

    # Normalize the path for consistent cache keys
    cache_key = str(Path(working_dir).resolve())

    if cache_key in _skein_available:
        return _skein_available[cache_key]

    try:
        result = subprocess.run(
            ["skein", "health", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=working_dir,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            _skein_available[cache_key] = data.get("healthy", False)
        else:
            _skein_available[cache_key] = False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError):
        _skein_available[cache_key] = False

    return _skein_available[cache_key]


def _parse_research_target(research_target: Optional[str]) -> Dict[str, str]:
    if not research_target:
        raise ValueError("research permission requires research_target (site:<id>, file:<path>, or dir:<path>)")
    prefix, sep, value = research_target.partition(":")
    if not sep:
        raise ValueError(f"unknown research_target prefix {research_target!r} (expected site:, file:, or dir:)")
    if prefix not in {"site", "file", "dir"}:
        raise ValueError(f"unknown research_target prefix {prefix!r} (expected site:, file:, or dir:)")
    if not value:
        raise ValueError(f"research_target {prefix}: requires a value")
    return {"type": prefix, "value": value, "raw": research_target}


def _validate_research_target(research_target: Optional[str], working_dir: Optional[str]) -> Dict[str, str]:
    target = _parse_research_target(research_target)
    target_type = target["type"]
    value = target["value"]

    if target_type == "site":
        try:
            result = subprocess.run(
                ["skein", "site", "get", value, "--json"],
                cwd=working_dir or os.getcwd(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"research_target site:{value} could not be validated: {exc}") from exc
        if result.returncode != 0:
            raise ValueError(f"research_target site:{value} does not exist in this project")
        return target

    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"research_target {target_type}:{value} must be an absolute path")

    if target_type == "file":
        parent = path.parent
        if not parent.exists() or not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ValueError(f"research_target file:{value} parent directory must exist and be writable")
        target["path"] = str(path)
        target["writable_bind"] = str(parent)
        return target

    if path.exists():
        if not path.is_dir() or not os.access(path, os.W_OK):
            raise ValueError(f"research_target dir:{value} must be an existing writable directory")
    else:
        parent = path.parent
        if not parent.exists() or not parent.is_dir() or not os.access(parent, os.W_OK):
            raise ValueError(f"research_target dir:{value} parent directory must exist and be writable")
    target["path"] = str(path)
    target["writable_bind"] = str(path)
    return target


def _research_target_preamble(target: Dict[str, str]) -> str:
    target_type = target["type"]
    value = target["value"]
    if target_type == "site":
        target_description = f"SKEIN site {value} (site:{value})"
        target_instruction = (
            f'File findings, notions, briefs via `skein post <type> {value} "..."`.\nDo not write loose files.'
        )
    elif target_type == "file":
        target_description = f"file:{value}"
        target_instruction = f"Write your final report to exactly {value}. Do not write anywhere else."
    else:
        target_description = f"dir:{value}"
        target_instruction = f"Write artifacts within {value}. Do not write outside it."

    return f"""You are a research agent.

You can read from the world (files, web, repos) but you cannot run python,
modify source code, or run dev tools (make, pytest, etc).

Your output target is: {target_description}.

{target_instruction}

Your task:
"""


def _research_writable_path(target: Dict[str, str]) -> str:
    if target["type"] == "file":
        return str(Path(target["value"]).parent)
    if target["type"] == "dir":
        path = Path(target["value"])
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    raise ValueError(f"research_target {target['raw']} does not have a writable path")


def _research_target_is_file_or_dir(research_target: Optional[str]) -> bool:
    if not research_target:
        return False
    try:
        return _parse_research_target(research_target)["type"] in {"file", "dir"}
    except ValueError:
        return False


def _research_omits_shard_commit_preamble(research_target_info: Optional[Dict[str, str]]) -> bool:
    return bool(research_target_info and research_target_info["type"] in {"file", "dir"})


# The tiers codex's --sandbox accepts.
CODEX_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


def _codex_sandbox_for_permission(
    permission: Optional[str],
    research_target: Optional[str],
    *,
    cli_shard_full_access: bool = False,
) -> str:
    if permission in {"research", "research+shard"}:
        # Verified with Codex CLI v0.125.0: --add-dir does not make a path
        # writable under --sandbox read-only, so file/dir research uses
        # workspace-write plus a narrow --add-dir grant for the output target.
        if _research_target_is_file_or_dir(research_target):
            return "workspace-write"
        return "read-only"
    if _base_permission_tier(permission) in NO_WRITE_TIERS:
        # readonly and its alias manual are the tight, no-write inspection tier -> codex
        # read-only. Match on the BASE tier so manual maps exactly like readonly (the
        # incoherent readonly+shard / manual+shard spellings resolve here too, though the
        # conflict check rejects them at every launch chokepoint before this is reached).
        return "read-only"
    if permission == "full" or (cli_shard_full_access and permission == "shard"):
        return "danger-full-access"
    return "workspace-write"


def _codex_respin_sandbox(original_spool: Optional[dict]) -> str:
    """The codex sandbox tier a respin of `original_spool` should continue at.

    Prefers the tier the original run actually passed, so a respin reproduces the session's
    isolation exactly — including a CLI `shard` spool that resolved to danger-full-access,
    which re-deriving from the permission alone would silently narrow. Falls back to
    re-deriving from the recorded permission for records written before the tier was stored
    (their recorded sandbox is the tier that was *intended*, which is the safe reading).
    """
    if not original_spool:
        return "workspace-write"

    recorded = original_spool.get("sandbox")
    if recorded in CODEX_SANDBOX_MODES:
        return recorded

    permission = original_spool.get("permission")
    if permission:
        return _codex_sandbox_for_permission(permission, original_spool.get("research_target"))

    return "workspace-write"


def _resolve_permission(
    permission: Optional[str],
    allowed_tools: Optional[str],
    research_target: Optional[str] = None,
    working_dir: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """
    Resolve permission profile to allowed_tools string and shard flag.

    Args:
        permission: Permission profile name ("readonly", "careful", "full", "shard", etc.) or None
        allowed_tools: Explicit allowed_tools override (takes precedence)

    Returns:
        Tuple of (allowed_tools string or None, should_use_shard bool)
    """
    # Shard intent is determined solely by the permission profile, not by whether
    # allowed_tools is set. Compute it first so the early return below preserves it.
    effective_permission = permission or "careful"
    use_shard = effective_permission == "shard" or effective_permission.endswith("+shard")

    is_research = effective_permission in {"research", "research+shard"}
    research_target_info = None
    if is_research:
        research_target_info = _validate_research_target(research_target, working_dir)

    # Explicit allowed_tools overrides the tool allow-list but not shard intent
    if allowed_tools:
        return allowed_tools, use_shard

    # If no permission specified, use "careful" as default
    if not permission:
        permission = "careful"

    # Look up profile
    if permission in PERMISSION_PROFILES:
        profile = PERMISSION_PROFILES[permission]
        if research_target_info and research_target_info["type"] in {"file", "dir"}:
            profile = f"{profile},Write,Edit"
        return profile, use_shard

    # Unknown profile - use careful, preserve shard intent
    return PERMISSION_PROFILES["careful"], use_shard


def _claude_permission_mode(permission: Optional[str]) -> str:
    """Return the Claude Code --permission-mode for a permission profile.

    This is the claude-harness tier table:
      - auto / auto+shard              -> "auto"            (classifier-vetted)
      - careful / None default         -> "auto"            (careful is now auto)
      - readonly / manual / research   -> "acceptEdits"     (tight allowlist tiers)
      - full / shard / careful+shard / research+shard
                                       -> "bypassPermissions" (bwrap-contained)

    The base tier drives the mode, not the "+shard" suffix alone: auto+shard is
    still auto. Only the tiers that were already bypass-in-shard (careful+shard,
    research+shard, shard) resolve to bypassPermissions via the "+shard"
    fallthrough. The readonly/manual tier has no coherent +shard variant — the
    pairing is rejected on the resolved (tier, use_shard) pair at the launch
    chokepoints (see _readonly_shard_conflict_error), so a no-write tier + shard
    never reaches this table (and so cannot fall through to bypassPermissions).
    """
    perm = permission or "careful"
    if perm.startswith("auto"):
        return "auto"
    if perm in ("readonly", "manual", "research"):
        return "acceptEdits"
    if perm in ("full", "shard") or perm.endswith("+shard"):
        return "bypassPermissions"
    # careful, the None default, and any unknown profile fall back to careful
    # semantics, which is now auto.
    return "auto"


# The readonly/manual tier is the tight, no-write inspection tier. Pairing it with
# a shard (an isolated worktree for making CHANGES) is incoherent no matter how the
# shard intent arrives: permission="readonly+shard"/"manual+shard", permission
# "readonly"/"manual" with shard=True, or the CLI --shard flag. So the authoritative
# check runs on the RESOLVED (tier, use_shard) pair at every launch chokepoint
# (_spin_sync — the common path for spin() and spool_retry — and _respin_sync),
# plus a friendly early copy at the harness-agnostic spin()/CLI entry. No door
# reaches a launch, or _claude_permission_mode, with the pairing.
NO_WRITE_TIERS = ("readonly", "manual")


def _base_permission_tier(permission: Optional[str]) -> Optional[str]:
    """The tier name without a trailing '+shard' (e.g. 'manual+shard' -> 'manual')."""
    if permission and permission.endswith("+shard"):
        return permission[: -len("+shard")]
    return permission


def _permission_implies_shard(permission: Optional[str]) -> bool:
    """Whether a permission string alone carries shard intent (mirrors the use_shard
    rule in _resolve_permission)."""
    return bool(permission) and (permission == "shard" or permission.endswith("+shard"))


def _readonly_shard_conflict_error(permission: Optional[str], use_shard: bool) -> Optional[str]:
    """Return the incoherent-pairing error when the no-write readonly/manual tier is
    combined with a shard, else None.

    `use_shard` must already fold in shard intent from every source (the permission
    string AND the shard flag), so this single check is spelling-agnostic — it fires
    whether the shard arrived as "...+shard", shard=True, or --shard.
    """
    if use_shard and _base_permission_tier(permission) in NO_WRITE_TIERS:
        return (
            "the readonly/manual tier has no write tools; +shard (or shard=True) "
            "adds a worktree it can't write in — use careful+shard or shard for "
            "isolated write work."
        )
    return None


def _detect_default_branch(working_dir: str) -> str:
    """Return the working dir's default branch name.

    Checks origin/HEAD first, then local main/master, falls back to 'master'.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            return ref.rsplit("/", 1)[-1]
    except Exception:
        pass
    for candidate in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            pass
    return "master"


def _spawn_shard(
    agent_id: str, working_dir: str, base_branch: Optional[str] = None
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Create an isolated git worktree (SHARD) for the agent.

    Uses SKEIN if available, falls back to plain git worktree.

    Args:
        agent_id: Identifier for the shard (used in worktree name)
        working_dir: Base directory for the worktree
        base_branch: Branch to fork from (default: auto-detected)

    Returns:
        Tuple of (shard_info, error_message). On success shard_info is a dict
        with keys worktree_path/branch_name/shard_id and error_message is None.
        On failure shard_info is None and error_message describes the problem.
    """
    base_branch = base_branch or _detect_default_branch(working_dir)
    skein_error: Optional[str] = None

    if _has_skein(working_dir):
        # Use SKEIN's shard spawn command
        try:
            cmd = [
                "skein",
                "shard",
                "spawn",
                "--agent",
                agent_id,
                "--description",
                f"Spindle spool for {agent_id}",
                "--base",
                base_branch,
            ]
            result = subprocess.run(
                cmd,
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
                        shard_id = Path(worktree_path).name
                        for line in result.stdout.splitlines():
                            if "Branch:" in line:
                                branch_name = line.split("Branch:")[1].strip()
                        return (
                            {
                                "worktree_path": worktree_path,
                                "branch_name": branch_name or f"shard-{agent_id}",
                                "shard_id": shard_id or agent_id,
                            },
                            None,
                        )
            else:
                # Capture the SKEIN error; fall through to git fallback
                skein_error = (result.stderr or result.stdout or "").strip() or "skein shard spawn failed"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: plain git worktree
    git_error: Optional[str] = None
    try:
        # Create worktrees directory if needed
        worktrees_dir = Path(working_dir) / "worktrees"
        worktrees_dir.mkdir(exist_ok=True)

        # Generate unique worktree name with microseconds to prevent collisions
        now = datetime.now()
        date_str = now.strftime("%Y%m%d-%H%M%S")
        microseconds = now.strftime("%f")[:6]  # Get all 6 digits of microseconds
        worktree_name = f"{agent_id}-{date_str}-{microseconds}"
        worktree_path = worktrees_dir / worktree_name
        branch_name = f"shard-{worktree_name}"

        # Create git worktree with new branch forked from base_branch
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_branch],
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=30,
        )
        if result.returncode == 0:
            return (
                {
                    "worktree_path": str(worktree_path),
                    "branch_name": branch_name,
                    "shard_id": worktree_name,
                },
                None,
            )
        # Surface errors; use friendly message for the branch-not-found case
        if "invalid reference" in result.stderr:
            git_error = (
                f"base branch '{base_branch}' not found in repo at {working_dir}. Try --base-branch <correct-name>."
            )
        else:
            git_error = result.stderr.strip() or "git worktree add failed"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        git_error = str(exc)

    if skein_error and git_error:
        return (None, f"skein shard spawn failed ({skein_error}); git worktree also failed: {git_error}")
    return (None, git_error or skein_error or "shard creation failed")


def _detect_existing_shard(path: str) -> Optional[Dict[str, str]]:
    """
    Check if path is inside an existing shard worktree (root or subdirectory).

    Returns shard_info dict if path is in a shard worktree, None otherwise.
    The returned `worktree_path` is the worktree ROOT, not the input path —
    callers may pass a subdirectory of the worktree, but merge/cleanup logic
    needs the root to derive the main repo via `.parent.parent`.

    A path qualifies when all conditions hold:
    1. `git rev-parse --show-toplevel` resolves to a worktree root
    2. That root is a direct child of <repo-root>/worktrees/ (anchored via git-common-dir)
    3. The worktree's current branch matches shard-*
    """
    resolved = Path(path).resolve()

    # Find the actual worktree root from the input path. This handles both
    # the worktree root itself and any subdirectory of it.
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=str(resolved),
            timeout=10,
        )
        if toplevel.returncode != 0:
            return None
        worktree_root = Path(toplevel.stdout.strip()).resolve()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    # Use git-common-dir to anchor worktrees/ to the actual repo root.
    # For a linked worktree this returns an absolute path to the main .git
    # directory; for the main repo itself it returns the relative string ".git".
    try:
        gcd = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=str(resolved),
            timeout=10,
        )
        if gcd.returncode != 0:
            return None
        common_dir = gcd.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    # A relative result means we're inside the main repo, not a linked worktree.
    if not Path(common_dir).is_absolute():
        return None

    # repo root is the parent of the common .git directory
    repo_root = Path(common_dir).parent.resolve()

    # Worktree root must be directly under <repo-root>/worktrees/
    try:
        relative = worktree_root.relative_to(repo_root / "worktrees")
    except ValueError:
        return None
    if len(relative.parts) != 1:
        return None

    # Get current git branch from the worktree root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(worktree_root),
            timeout=10,
        )
        if result.returncode != 0:
            return None
        branch_name = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if not branch_name.startswith("shard-"):
        return None

    shard_id = branch_name[len("shard-") :]
    return {
        "worktree_path": str(worktree_root),
        "branch_name": branch_name,
        "shard_id": shard_id,
    }


def _close_tender_folios(worktree_name: str, working_dir: str) -> Optional[str]:
    """
    Close any tender folios associated with a worktree after successful merge.

    Queries SKEIN for tender folios with matching worktree_name in metadata,
    then closes them by creating a status thread.

    Args:
        worktree_name: The worktree name to match in tender metadata
        working_dir: The directory to check for SKEIN availability

    Returns:
        Message about closed folios, or None if SKEIN unavailable/no matches
    """
    if not _has_skein(working_dir):
        return None

    try:
        import urllib.error
        import urllib.request

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


def _cleanup_shard(
    shard_info: Dict[str, str], working_dir: str, keep_branch: bool = False, spool_id: Optional[str] = None
) -> bool:
    """
    Clean up a SHARD worktree.

    Args:
        shard_info: Dict with worktree_path, branch_name
        working_dir: Base directory
        keep_branch: If True, don't delete the branch
        spool_id: Optional spool ID for better error logging
    Returns:
        True if successful
    """
    worktree_path = shard_info.get("worktree_path")
    branch_name = shard_info.get("branch_name")

    if not worktree_path:
        return False

    try:
        # Remove worktree
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error(
                f"Failed to remove worktree {worktree_path}"
                + (f" for spool {spool_id}" if spool_id else "")
                + f": {result.stderr.strip()}"
            )
            return False
        # Optionally delete branch
        if not keep_branch and branch_name:
            result = subprocess.run(
                ["git", "branch", "-D", branch_name], capture_output=True, text=True, cwd=working_dir, timeout=10
            )
            if result.returncode != 0:
                logger.warning(
                    f"Failed to delete branch {branch_name}"
                    + (f" for spool {spool_id}" if spool_id else "")
                    + f": {result.stderr.strip()}"
                )
                # Don't return False here - worktree removal succeeded

        # Prune worktree references
        result = subprocess.run(
            ["git", "worktree", "prune"], capture_output=True, text=True, cwd=working_dir, timeout=10
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to prune worktree references"
                + (f" for spool {spool_id}" if spool_id else "")
                + f": {result.stderr.strip()}"
            )
            # Don't return False here - worktree removal succeeded

        return True
    except subprocess.TimeoutExpired as e:
        logger.error(
            f"Timeout during shard cleanup for worktree {worktree_path}"
            + (f" (spool {spool_id})" if spool_id else "")
            + f": {e}"
        )
        return False
    except (FileNotFoundError, OSError) as e:
        logger.error(
            f"Error during shard cleanup for worktree {worktree_path}"
            + (f" (spool {spool_id})" if spool_id else "")
            + f": {e}"
        )
        return False


def _preserve_failed_spool_shard(spool: dict) -> bool:
    """Mark a failed spool's newly-created shard for explicit recovery.

    Automatic cleanup cannot atomically prove that ignored output was not
    created between its final Git status check and worktree removal. Preserve
    every shard whose agent failed instead; retry can safely reuse it, and a
    human or SKEIN can inspect and explicitly clean it later.
    """
    if spool.get("status") != "error" or not spool.get("shard_created_by_spool"):
        return False
    shard_info = spool.get("shard") or {}
    shard_info["startup_failure_preserved"] = True
    spool["shard"] = shard_info
    spool["shard_cleanup_preserved"] = True
    spool["shard_cleanup_preserved_reason"] = "automatic cleanup disabled after agent failure"
    spool.pop("shard_cleanup_pending", None)
    spool.pop("shard_cleanup_pending_reason", None)
    return True


def _get_spool_path(spool_id: str) -> Path:
    """Get path to spool JSON file."""
    return SPINDLE_DIR / f"{spool_id}.json"


def _get_output_path(spool_id: str) -> Path:
    """Get path to stdout file for a spool."""
    return SPINDLE_DIR / f"{spool_id}.stdout"


def _get_stderr_path(spool_id: str) -> Path:
    """Get path to stderr file for a spool."""
    return SPINDLE_DIR / f"{spool_id}.stderr"


def _get_exit_path(spool_id: str) -> Path:
    """Get path to the detached wrapper's persisted exit status."""
    return SPINDLE_DIR / f"{spool_id}.exit"


def _read_exit_code(spool_id: str) -> Optional[int]:
    """Read a detached child's persisted exit status, or None if unavailable."""
    try:
        return int(_get_exit_path(spool_id).read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


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


def _get_lock_path(spool_id: str) -> Path:
    """Get path to lock file for a spool."""
    return SPINDLE_DIR / f"{spool_id}.lock"


@contextmanager
def _spool_lock(spool_id: str, blocking: bool = True) -> Generator[bool, None, None]:
    """
    Acquire exclusive lock on a spool for atomic operations.

    Uses fcntl advisory locking. The lock is held for the duration of the
    context manager and automatically released on exit.

    Args:
        spool_id: The spool to lock
        blocking: If True, wait for lock. If False, fail immediately if locked.

    Yields:
        True if lock acquired, False if non-blocking and lock unavailable.
    """
    # Resolve the lock path once and create its parent, so the lock file and the
    # directory we create can't diverge if SPINDLE_DIR changes underneath us.
    lock_path = _get_lock_path(spool_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = None
    acquired = False
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            if blocking:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                acquired = True
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
        except BlockingIOError:
            # Non-blocking mode and lock not available
            acquired = False

        yield acquired
    finally:
        if lock_fd is not None:
            if acquired:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


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


@contextmanager
def _concurrency_lock() -> Generator[None, None, None]:
    """Serialize spool slot reservation across processes."""
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = SPINDLE_DIR / ".concurrency.lock"
    with open(lock_file, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _find_spool_by_session(session_id: str) -> Optional[dict]:
    """Find a spool by its session_id."""
    for spool in _list_spools():
        if spool.get("session_id") == session_id:
            return spool
    return None


def _resolve_spool_for_respin(handle: str) -> Optional[dict]:
    """Resolve a respin handle to its spool.

    `handle` may be either a raw session_id (the legacy respin contract) or
    the spool_id returned by spin() (the natural handle every other spindle
    entrypoint takes). Tries session_id first to preserve existing
    session_id callers, then falls back to matching the spool's own id.

    Returns the spool dict, or None if nothing matches.
    """
    spool = _find_spool_by_session(handle)
    if spool:
        return spool
    for spool in _list_spools():
        if spool.get("id") == handle:
            return spool
    return None


# ---------------------------------------------------------------------------
# Profiles
#
# A profile is a named, lodged configuration: a base harness plus a set of
# overrides (model, alt endpoint env, extra CLI flags). It lives in a folder
# (folder name == profile name == the value passed as `harness`) holding a
# single profile.json. Profiles let a user point the Claude Code harness at any
# Anthropic-compatible endpoint by injecting ANTHROPIC_BASE_URL / API key /
# CLAUDE_CONFIG_DIR into the spawned child, without changing spindle's parsing,
# unspool, or respin paths.
# ---------------------------------------------------------------------------

# Match ${VAR} references for environment expansion in profile values.
_PROFILE_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _profile_roots() -> list:
    """Return the directories scanned for profiles, lowest precedence first.

    1. Canonical: ``SPINDLE_PROFILES_DIR`` (~/.spindle/profiles) — where real,
       private profiles live, physically outside any repo.
    2. Dev convenience: ``./profiles`` relative to the current working dir, if
       it exists and differs from the canonical root. Gitignored; lets a
       developer drop a throwaway profile beside the code.
    """
    roots = [SPINDLE_PROFILES_DIR]
    repo_local = Path.cwd() / "profiles"
    if repo_local.resolve() != SPINDLE_PROFILES_DIR.resolve():
        roots.append(repo_local)
    return roots


def _discover_profiles() -> dict:
    """Discover all lodged profiles, keyed by name.

    Later roots override earlier ones on name collision (repo-local shadows
    canonical), matching the documented load order. A profile whose
    profile.json is missing, unreadable, or not a JSON object is skipped with a
    logged warning — discovery never crashes on a single bad profile.

    Each returned value is the parsed profile.json with two synthetic keys
    added: ``_name`` (folder name) and ``_source`` (path to the profile.json).
    """
    profiles: dict = {}
    for root in _profile_roots():
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.iterdir())
        except OSError as exc:
            logger.warning("spindle: cannot scan profile root %s: %s", root, exc)
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            profile_json = entry / "profile.json"
            if not profile_json.is_file():
                continue
            try:
                with open(profile_json) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("profile.json must contain a JSON object")
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                logger.warning("spindle: skipping malformed profile %s: %s", profile_json, exc)
                continue
            profiles[entry.name] = {**data, "_name": entry.name, "_source": str(profile_json)}
    return profiles


def _load_profile(name: str) -> Optional[dict]:
    """Return the lodged profile with this name, or None if none exists."""
    if not name:
        return None
    return _discover_profiles().get(name)


def _op_inject(value: str, name: str) -> str:
    """Resolve op:// references in ``value`` via strongbox/op inject.

    Uses ``strongbox inject`` if the binary is on PATH, else ``op inject``. The
    value is piped in as a template; stdout is the resolved result. If neither
    tool exists, or the call fails, the literal value is returned and a warning
    is logged — keeping 1Password/strongbox an optional local convenience.
    """
    if shutil.which("strongbox"):
        tool = ["strongbox", "inject"]
    elif shutil.which("op"):
        tool = ["op", "inject"]
    else:
        logger.warning(
            "spindle: profile %s: value contains op:// but neither strongbox nor op is on PATH; leaving literal",
            name,
        )
        return value
    try:
        proc = subprocess.run(tool, input=value, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("spindle: profile %s: %s failed (%s); leaving literal", name, tool[0], exc)
        return value
    if proc.returncode != 0:
        logger.warning(
            "spindle: profile %s: %s exited %s (%s); leaving literal",
            name,
            tool[0],
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return value
    return proc.stdout


def _resolve_profile_value(value, name: str = "<profile>"):
    """Resolve a single profile string value at spawn time.

    1. Expand every ``${ENV_VAR}`` from os.environ. An unset var is left as the
       literal ``${VAR}`` and a warning is logged (never crashes).
    2. If the (expanded) value contains ``op://``, resolve it via
       ``strongbox``/``op`` inject; if neither is installed, leave it literal.

    Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value

    def _expand(match: "re.Match") -> str:
        var = match.group(1)
        if var in os.environ:
            return os.environ[var]
        logger.warning(
            "spindle: profile %s: env var %s is unset; leaving ${%s} literal",
            name,
            var,
            var,
        )
        return match.group(0)

    resolved = _PROFILE_ENV_RE.sub(_expand, value)
    if "op://" in resolved:
        resolved = _op_inject(resolved, name)
    return resolved


def _resolve_profile_overrides(profile: dict) -> dict:
    """Resolve a loaded profile into concrete spawn-time overrides.

    Returns a dict with: ``base_harness``, ``env`` (the child env to inject),
    ``extra_args`` (resolved CLI flags), ``model`` (profile default model, may
    be None), ``model_aliases`` (profile-scoped alias map), and ``config_dir``.

    Secrets are resolved fresh on every call (so rotated keys take effect on
    respin). Raises ValueError for unsupported base-harness / base_url combos.
    """
    name = profile.get("_name", "<profile>")
    base = profile.get("harness") or "claude-code"

    # Validate field types up front so a malformed profile raises a clean
    # ValueError (caught by spin and surfaced as a spin error) instead of an
    # AttributeError/TypeError deep in resolution. Discovery only guarantees the
    # top level is a JSON object; individual fields are still untrusted.
    for field in ("harness", "model", "base_url", "api_key", "config_dir"):
        val = profile.get(field)
        if val is not None and not isinstance(val, str):
            raise ValueError(f"profile {name!r}: {field!r} must be a string, got {type(val).__name__}")
    env_raw = profile.get("env")
    if env_raw is not None and not isinstance(env_raw, dict):
        raise ValueError(f"profile {name!r}: 'env' must be an object, got {type(env_raw).__name__}")
    extra_args_raw = profile.get("extra_args")
    if extra_args_raw is not None and (
        not isinstance(extra_args_raw, list) or not all(isinstance(a, str) for a in extra_args_raw)
    ):
        raise ValueError(f"profile {name!r}: 'extra_args' must be a list of strings")
    aliases_raw = profile.get("model_aliases")
    if aliases_raw is not None and not isinstance(aliases_raw, dict):
        raise ValueError(f"profile {name!r}: 'model_aliases' must be an object, got {type(aliases_raw).__name__}")

    base_url = profile.get("base_url")
    config_dir = profile.get("config_dir")
    api_key = profile.get("api_key")
    env_spec = profile.get("env") or {}
    extra_args = profile.get("extra_args") or []
    model = profile.get("model")
    model_aliases = profile.get("model_aliases") or {}

    # v1 only the Claude Code path supports endpoint/flag injection.
    if base != "claude-code":
        if base_url:
            raise ValueError(f"profile {name!r}: base_url requires base harness 'claude-code', got {base!r}")
        raise ValueError(f"profile {name!r}: base harness {base!r} is not supported; only 'claude-code'")

    # Alt-endpoint profiles are isolated by default: if base_url is set without
    # an explicit config_dir, point CLAUDE_CONFIG_DIR at a per-profile dir so
    # the child does not inherit the user's real ~/.claude MCP servers/CLAUDE.md.
    if base_url and not config_dir:
        config_dir = str(SPINDLE_PROFILES_DIR / name / "claude-config")

    def _r(v):
        return _resolve_profile_value(v, name)

    env: Dict[str, str] = {}
    if base_url:
        env["ANTHROPIC_BASE_URL"] = _r(base_url)
    if api_key:
        env["ANTHROPIC_API_KEY"] = _r(api_key)
    if config_dir:
        resolved_cfg = os.path.expanduser(_r(config_dir))
        env["CLAUDE_CONFIG_DIR"] = resolved_cfg
        try:
            Path(resolved_cfg).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("spindle: profile %s: could not create config_dir %s: %s", name, resolved_cfg, exc)
    for key, val in env_spec.items():
        env[key] = _r(val)

    resolved_extra_args = [_r(arg) for arg in extra_args]

    return {
        "base_harness": base,
        "env": env,
        "extra_args": resolved_extra_args,
        "model": model,
        "model_aliases": model_aliases,
        "config_dir": config_dir,
    }


def _profile_spawn_env(
    profile_name: Optional[str],
    caller_env: Optional[Dict[str, str]],
    model: Optional[str] = None,
    *,
    strict: bool = False,
) -> Tuple[Optional[Dict[str, str]], Optional[str], list, bool]:
    """Reconstruct a profile spool's child spawn env, model, and extra args.

    Every profile spawn path — the fresh spin, ``_respin_sync``,
    ``spool_retry``, and ``_handle_expired_session``'s transcript fallback —
    routes through here so the alt endpoint / key / model / extra_args are
    rebuilt identically at every spawn. Before this helper existed, two of
    those four paths read the (deliberately secret-stripped) persisted env and
    silently ran the profile spool against the default api.anthropic.com
    endpoint; centralizing the reconstruction keeps a future spawn path from
    regressing the same way.

    The profile is re-resolved fresh on every call: ``${ENV}`` and ``op://``
    secrets are re-expanded (so rotated keys take effect) into ``spawn_env``,
    the caller's persisted non-secret env is overlaid on top (caller wins), and
    ``spawn_env`` is returned for the child process ONLY. The resolved secrets
    are never part of the persisted record — callers persist ``caller_env``.

    ``model`` is the caller-supplied (fresh spin) or recorded (resume) model to
    honor: ``None`` falls back to the profile's default model; a value matching
    a profile-scoped alias is mapped; any other value is kept as-is.

    ``strict`` True re-raises a missing/malformed profile's ``ValueError`` so
    the fresh-spin path can surface it to the caller. ``strict`` False (the
    resume/retry default) logs and degrades to the caller env alone — a
    deleted/corrupted profile loses the alt endpoint on resume rather than
    crashing the recovery.

    Returns ``(spawn_env, model, extra_args, resolved)``. ``resolved`` is True
    only when the profile loaded and resolved; resume paths gate re-injecting
    ``--model``/extra_args on it so a degraded resume matches its pre-profile
    behavior.
    """
    if not profile_name:
        return caller_env, model, [], False

    prof = _load_profile(profile_name)
    if prof is None:
        if strict:
            raise ValueError(f"profile {profile_name!r} is not lodged")
        logger.warning(
            "spindle: profile %r no longer lodged; reusing stored env (alt endpoint lost on resume)",
            profile_name,
        )
        return caller_env, model, [], False

    try:
        overrides = _resolve_profile_overrides(prof)
    except ValueError as exc:
        if strict:
            raise
        logger.warning(
            "spindle: profile %r failed to resolve (%s); reusing stored env (alt endpoint lost on resume)",
            profile_name,
            exc,
        )
        return caller_env, model, [], False

    # Spawn env: profile-injected env (fresh secrets) is the base; the caller's
    # persisted non-secret env wins on top. Child process only — never persisted.
    spawn_env = dict(overrides["env"])
    if caller_env:
        spawn_env.update(caller_env)

    # Model: None -> profile default; a profile-scoped alias is mapped;
    # otherwise the caller/recorded model passes through unchanged.
    if model is None:
        model = overrides["model"]
    elif model in overrides["model_aliases"]:
        model = overrides["model_aliases"][model]

    return spawn_env, model, overrides["extra_args"], True


def _get_cc_bg_tasks(session_id: str) -> list:
    """
    Read Claude Code background-task records for a session.

    Claude Code stores Task tool state in ~/.claude/tasks/<session_id>/<n>.json.
    Each file is a JSON object with at minimum: id, subject, status.

    Returns a list of task dicts, empty if none found.
    """
    tasks_dir = CLAUDE_TASKS_DIR / session_id
    if not tasks_dir.exists():
        return []

    tasks = []
    for json_file in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text())
            tasks.append(data)
        except (json.JSONDecodeError, IOError):
            pass
    return tasks


def _count_running() -> int:
    """
    Count currently running spools.

    Includes both "running" and "pending" spools, since pending spools
    represent reserved slots that will become running shortly.
    This prevents TOCTOU race in concurrency limit enforcement.
    """
    return sum(1 for s in _list_spools() if s.get("status") in ("running", "pending"))


def _spools_idle() -> bool:
    """Finalize any finished-but-unmarked spools, then report whether the queue
    is empty (no running or pending spools). Uses _recover_orphans so that both
    a dead-but-unmarked running spool and a stuck pending one (silent spawn
    failure, cleared after PENDING_SPAWN_TIMEOUT) are cleaned - otherwise either
    could hold the queue open and wedge a drain forever."""
    _recover_orphans()
    return _count_running() == 0


def _wait_until_idle(poll_interval: float = RELOAD_DRAIN_POLL_INTERVAL) -> None:
    """Block until no spools are running or pending. New spins are allowed during
    the wait, so this returns at the next moment the queue happens to be empty."""
    while not _spools_idle():
        time.sleep(poll_interval)


def _try_reserve_slot_and_create(spool_id: str, initial_status: str = "pending") -> tuple[bool, Optional[str]]:
    """
    Atomically check if we can spawn a new spool and create the initial spool file.

    This function holds a file lock during both the check AND the spool creation
    to prevent TOCTOU race conditions.

    Args:
        spool_id: The ID for the new spool
        initial_status: Initial status for the spool (default: "pending")

    Returns:
        (success, error_message): success is True if slot reserved and spool created,
                                  False if limit exceeded.

    Uses file locking to prevent TOCTOU race between check and spawn.
    The lock is held during both the check and the initial spool creation.
    """
    with _concurrency_lock():
        # Now we have exclusive access - check the limit
        running_count = _count_running()
        if running_count >= MAX_CONCURRENT:
            return False, f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

        # Slot available - create the spool immediately while holding the lock
        # This ensures the slot is claimed atomically
        spool = {
            "id": spool_id,
            "status": initial_status,
            "created_at": datetime.now().isoformat(),
        }
        _write_spool(spool_id, spool)

        return True, None


def _extract_last_json_object(text: str) -> Optional[dict]:
    """Extract the last JSON object from text that may contain non-JSON lines.

    Tries to parse JSON at every '{' position using json.JSONDecoder.raw_decode,
    which delegates string/escape/nesting handling to the actual JSON parser.
    This makes the scan robust to arbitrary noise (stray quotes, unmatched
    braces) that appears outside of valid JSON objects.
    """
    decoder = json.JSONDecoder()
    last: Optional[dict] = None
    i = 0
    n = len(text)
    while i < n:
        if text[i] in ("{", "["):
            try:
                obj, end = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(obj, dict):
                last = obj
            i = end
            continue
        i += 1
    return last


def _extract_gemini_stderr_error(stderr: str) -> str:
    """Extract a meaningful error message from gemini CLI stderr.

    The gemini CLI has a bug where JS error objects get serialized as
    "[object Object]" instead of their actual message. The real error
    details are in the plain-text portion of stderr before the JSON block.
    """
    import re

    error_lines = []
    for line in stderr.split("\n"):
        line = line.strip()
        if not line or line.startswith("{") or line.startswith("}"):
            continue
        match = re.match(r"^(?:Error:|.*Error:)\s*(.+)", line)
        if match and "[object Object]" not in line:
            error_lines.append(match.group(1).strip())
    if error_lines:
        return error_lines[-1]
    json_start = stderr.find("{")
    if json_start > 0:
        return stderr[:json_start].strip()[-500:]
    return stderr[:500]


def _extract_cc_result(data) -> Optional[dict]:
    """Extract the result dict from Claude Code CLI output.

    Handles both the old format (single JSON object with result/error/session_id)
    and the new format (JSON array of events, with the result in a type=result event).
    Returns the result dict, or None if not found.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        # New format: array of events. Find the result event.
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None


def _refusal_category(data) -> str:
    """Pull the safety-classifier category from a Claude Code refusal stream.

    When Fable's bio/cyber gate declines a session, the result event carries
    stop_reason == "refusal" but no category; the category ("bio", "cyber", ...)
    lives on an assistant message's stop_details. Returns the category, or
    "unknown" when the gate fired without naming one — common, two of the first
    three observed refusals had a null category.
    """
    events = data if isinstance(data, list) else [data]
    for item in events:
        if not isinstance(item, dict):
            continue
        msg = item.get("message")
        # Category lives on the assistant message's stop_details, or — in the
        # old single-object CC format — on the event's own stop_details.
        candidates = []
        if isinstance(msg, dict):
            candidates.append(msg.get("stop_details"))
        candidates.append(item.get("stop_details"))
        for details in candidates:
            if isinstance(details, dict) and details.get("type") == "refusal":
                category = details.get("category")
                if category:
                    return category
    return "unknown"


def _is_fable_gate(model, text) -> bool:
    """True when a Claude safety refusal is specifically Fable's bio/cyber gate.

    ``stop_reason == "refusal"`` is the generic Anthropic safety-refusal signal —
    any model can emit it, so it alone must not be attributed to Fable. When a
    model was recorded, trust it: the gate is Fable's iff the model resolves to
    claude-fable-5; any other model's refusal is not Fable's, even if the task
    text happens to mention "Fable 5". Only when no model was recorded (respins
    and continues, which don't carry one) do we fall back to the CLI's
    Fable-specific gate text. The separate "issue with the selected model
    (claude-fable-5)" unavailability error never reaches here — it carries
    stop_reason "stop_sequence", not "refusal".
    """
    if isinstance(model, str) and model.strip():
        return CLAUDE_MODEL_ALIASES.get(model, model) == "claude-fable-5"
    return isinstance(text, str) and "Fable 5" in text


def _format_spool_failure(spool_id: str, spool: dict) -> str:
    """Render a failed spool's status line, calling out the Fable safety gate.

    A gate refusal is not a task failure — Fable's bio/cybersecurity classifier
    declined the session and the right response is to re-route to another model,
    not to debug the work. Surface that distinctly so an orchestrating agent can
    branch on it instead of parsing the generic error prose.
    """
    err = spool.get("error", "Unknown error")
    kind = spool.get("error_kind")
    if kind == "fable_gate":
        category = spool.get("gate_category", "unknown")
        return (
            f"Spool {spool_id} FABLE SAFETY GATE ({category}): Fable's "
            f"bio/cybersecurity classifier declined this session. This is NOT a "
            f"task failure — re-route to a different model (e.g. opus) and respin. "
            f"Original message:\n{err}"
        )
    if kind == "safety_refusal":
        return (
            f"Spool {spool_id} SAFETY REFUSAL: the model declined this request on "
            f"safety grounds (not a task failure). Consider rephrasing or trying a "
            f"different model. Original message:\n{err}"
        )
    return f"Spool {spool_id} failed: {err}"


def _extract_codex_result(stdout: str) -> Optional[str]:
    """Extract the agent's prose from Codex's newline-delimited JSON stream.

    Codex emits an event per line: agent_message items (the agent's actual text),
    command_execution items (shell commands plus their full captured output),
    reasoning, etc. The command output dominates by volume - a typical result is
    95% command logs - so storing the raw stream as the result floods the caller's
    context. This pulls just the agent_message texts, joined in order. The full
    event stream is preserved separately in the transcript file.

    Returns the joined agent messages, or None if none were found (caller should
    fall back to the raw stdout so nothing is silently dropped).
    """
    messages = []
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    messages.append(text)
    if not messages:
        return None
    return "\n\n".join(messages)


def _codex_turn_state(stdout: str) -> tuple[Optional[str], object]:
    """Return the final Codex turn state and its raw failure payload.

    ``turn.started`` resets an earlier terminal state. Without that transition,
    a stream ending ``turn.completed`` then ``turn.started`` would falsely look
    successful even though its newest turn never terminated.
    """
    state = None
    failure = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "turn.started":
            state = "running"
            failure = None
        elif event_type == "turn.completed":
            state = "complete"
            failure = None
        elif event_type in {"turn.failed", "error"}:
            state = "failed"
            failure = event.get("error")
            if failure is None:
                failure = event.get("message")
    return state, failure


def _codex_failure_message(stdout: str) -> Optional[str]:
    """Return the final failed Codex turn message, if the stream has one.

    Codex sometimes exits successfully at the process level after emitting a
    ``turn.failed`` JSONL event (for example, an account/model HTTP 400). The
    event, rather than the process exit code or the mere presence of stdout,
    is therefore authoritative. Its message may itself be a serialized API
    error object; unwrap that so callers get the actionable provider message.
    """
    state, failure = _codex_turn_state(stdout)
    if state != "failed":
        return None
    if isinstance(failure, dict):
        message = failure.get("message")
        details = {key: value for key, value in failure.items() if key != "message" and value is not None}
        failure = message or (json.dumps(details, sort_keys=True) if details else None)
    if not failure:
        return "Codex failed without an error message"
    if not isinstance(failure, str):
        return str(failure)

    try:
        nested = json.loads(failure)
    except json.JSONDecodeError:
        return failure
    if not isinstance(nested, dict):
        return failure
    api_error = nested.get("error")
    if isinstance(api_error, dict) and api_error.get("message"):
        return str(api_error["message"])
    if nested.get("message"):
        return str(nested["message"])
    return failure


def _extract_kimi_result(stdout: str) -> Optional[str]:
    """Extract the agent's prose from Kimi's stream-json (JSONL) output.

    Kimi emits an event per line. The final answer is in a line with
    role == "assistant"; its `content` is version/mode-dependent:
      - thinking mode: a list of items like [{"type":"think"}, {"type":"text"}]
      - non-thinking mode: a plain string
    The old extractor only handled the list shape, so non-thinking spools (the
    default) fell through to storing the raw JSONL - which embeds role == "tool"
    lines carrying full file/command output, the same bloat Codex had. This
    handles both shapes and keeps the last assistant message with real text,
    never role == "tool" lines. The full stream is preserved in the transcript.

    Returns the assistant prose, or None if none was found (caller should fall
    back to raw stdout as a last resort).
    """
    result_text = None
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("role") != "assistant" or "content" not in data:
            continue
        content = data.get("content")
        if isinstance(content, str):
            if content.strip():
                result_text = content
        elif isinstance(content, list):
            texts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            if texts:
                result_text = "\n".join(texts)
    return result_text


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running (not a zombie)."""
    try:
        os.kill(pid, 0)  # Doesn't kill, just checks existence
    except (OSError, ProcessLookupError):
        return False

    # os.kill(pid, 0) succeeds for zombie processes too.
    # Try to reap it — if it's our zombie child, waitpid will collect it.
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False  # Was a zombie, now reaped
    except ChildProcessError:
        # Not our child process — check /proc status instead
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:"):
                        return "Z" not in line  # Z = zombie
        except (FileNotFoundError, PermissionError):
            pass  # Process disappeared or not accessible

    return True


def _is_process_group_alive(process_group_id: int) -> bool:
    """Whether any process remains in a detached spool's process group.

    ``_spawn_detached`` uses ``start_new_session=True``, making the child PID
    its process-group ID. A leader may exit while descendants still write into
    the shard, so destructive cleanup must prove the whole group is gone.
    Permission errors fail closed because they still prove the group exists.
    """
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True

    # killpg(0) also succeeds for a group containing only zombies. Zombies
    # cannot mutate a worktree and should not make cancellation report failure
    # forever, so on Linux confirm that at least one non-zombie member remains.
    if not Path("/proc").is_dir():
        return True
    try:
        for status_path in Path("/proc").glob("[0-9]*/stat"):
            try:
                stat = status_path.read_text()
            except FileNotFoundError:
                continue  # Process exited between the directory scan and read.
            except OSError:
                return True
            fields = stat[stat.rfind(")") + 2 :].split()
            if len(fields) >= 3 and int(fields[2]) == process_group_id and fields[0] != "Z":
                return True
        return False
    except (OSError, ValueError):
        return True


def _terminate_process_group(process_group_id: int, grace_seconds: float) -> bool:
    """Terminate a detached spool group, returning whether it is fully gone."""
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        try:
            os.kill(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return not _is_process_group_alive(process_group_id)
    time.sleep(grace_seconds)
    if not _is_process_group_alive(process_group_id):
        return True
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        try:
            os.kill(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    # Reap our leader if it became a zombie, then give adopted descendants a
    # brief chance to disappear before reporting failure.
    _is_pid_alive(process_group_id)
    time.sleep(0.1)
    return not _is_process_group_alive(process_group_id)


def _parse_duration(time_str: str) -> Optional[int]:
    """
    Parse a duration string into seconds.

    Supported formats:
    - "30s" - 30 seconds
    - "90m" - 90 minutes
    - "2h" - 2 hours
    - "06:00" or "14:30" - absolute time (wait until that time today/tomorrow)

    Returns:
        Number of seconds to wait, or None if invalid format

    Validation:
        - Minimum: 1 second
        - Maximum: 24 hours (86400 seconds)
        - Rejects negative values
        - Rejects zero
    """
    if not time_str:
        return None

    time_str = time_str.strip()

    # Try relative duration formats: 30s, 90m, 2h
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([smh])$", time_str.lower())
    if match:
        value = float(match.group(1))
        unit = match.group(2)

        # Calculate seconds
        if unit == "s":
            seconds = int(value)
        elif unit == "m":
            seconds = int(value * 60)
        elif unit == "h":
            seconds = int(value * 3600)
        else:
            return None

        # Validate range: minimum 1 second, maximum 24 hours
        if seconds < 1 or seconds > 86400:
            return None

        return seconds

    # Try absolute time format: HH:MM
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if match:
        target_hour = int(match.group(1))
        target_minute = int(match.group(2))

        if target_hour < 0 or target_hour > 23 or target_minute < 0 or target_minute > 59:
            return None

        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

        # If target time is in the past, assume tomorrow
        if target <= now:
            target += timedelta(days=1)

        seconds = int((target - now).total_seconds())

        # Validate range: minimum 1 second, maximum 24 hours
        if seconds < 1 or seconds > 86400:
            return None

        return seconds

    return None


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
                # Use lock to prevent race with finalization
                with _spool_lock(spool_id, blocking=False) as acquired:
                    if not acquired:
                        continue  # Skip if locked
                    path.unlink()
                    # Also clean up output, lock, and transcript files
                    stdout_path = _get_output_path(spool_id)
                    stderr_path = _get_stderr_path(spool_id)
                    exit_path = _get_exit_path(spool_id)
                    lock_path = _get_lock_path(spool_id)
                    if stdout_path.exists():
                        stdout_path.unlink()
                    if stderr_path.exists():
                        stderr_path.unlink()
                    if exit_path.exists():
                        exit_path.unlink()
                    if lock_path.exists():
                        lock_path.unlink()
        except Exception:
            pass

    # Sweep orphaned lock files. _spool_lock never unlinks its file, and spools
    # removed via other paths (shard cleanup, etc.) leave their lock behind; the
    # json loop above only covers spools it removes itself, so without this the
    # locks accumulate without bound. Skip the shared .concurrency.lock and any
    # lock still fresh, to avoid racing a spool whose json isn't written yet.
    for lock_path in SPINDLE_DIR.glob("*.lock"):
        if lock_path.name.startswith("."):
            continue  # e.g. .concurrency.lock - a shared lock, not a per-spool one
        if (SPINDLE_DIR / f"{lock_path.stem}.json").exists():
            continue  # still has a spool record
        try:
            if datetime.fromtimestamp(lock_path.stat().st_mtime) < cutoff:
                lock_path.unlink()
        except (OSError, ValueError):
            pass


def _check_and_finalize_spool(spool_id: str) -> bool:
    """
    Check if a spool's process has finished and finalize it.
    Returns True if the spool was finalized, False if still running.

    Note: claude CLI doesn't exit immediately after writing output, so we also
    check if stdout contains a complete JSON result even if PID is alive.

    Uses file locking to prevent TOCTOU race conditions when multiple processes
    attempt to finalize the same spool concurrently.
    """
    # Use non-blocking lock first for quick check without waiting
    with _spool_lock(spool_id, blocking=False) as acquired:
        if not acquired:
            # Another process is finalizing this spool, treat as "still running"
            # The other process will complete finalization
            return False

        spool = _read_spool(spool_id)
        if not spool or spool.get("status") != "running":
            return True  # Already done

        pid = spool.get("pid")
        if not pid:
            return False  # No PID yet, still starting

        stdout_path = _get_output_path(spool_id)
        stderr_path = _get_stderr_path(spool_id)

        # Check if output has a complete JSON result for harnesses whose CLI may
        # linger after publishing its final object. Codex is intentionally not
        # finalized from stdout alone: its exit status is authoritative, and a
        # terminal event can precede a nonzero process exit.
        output_complete = False
        if stdout_path.exists():
            try:
                content = stdout_path.read_text()
                if content.strip():
                    if spool.get("harness") != "codex":
                        # Claude Code / Gemini output
                        data = json.loads(content)
                        cc_result = _extract_cc_result(data)
                        if cc_result and ("result" in cc_result or "error" in cc_result or "response" in cc_result):
                            output_complete = True
            except (IOError, json.JSONDecodeError):
                pass

        # Gemini CLI writes error JSON to stderr — check there too
        if not output_complete and spool.get("harness") == "gemini" and stderr_path.exists():
            try:
                stderr_content = stderr_path.read_text()
                if stderr_content.strip():
                    parsed = _extract_last_json_object(stderr_content)
                    if parsed and ("error" in parsed or "session_id" in parsed):
                        output_complete = True
            except IOError:
                pass

        # Poll our child handle before the generic PID probe. This preserves its
        # real exit status; _is_pid_alive may reap a zombie via waitpid.
        proc = _PROC_HANDLES.get(spool_id)
        observed_exit_code = proc.poll() if proc is not None else None
        process_alive = observed_exit_code is None if proc is not None else _is_pid_alive(pid)

        # If PID alive and no complete output yet, still running.
        if process_alive and not output_complete:
            return False

        # Once the leader exits (or a non-Codex harness emits its complete JSON
        # object), descendants no longer own the spool. Drain the whole group
        # before parsing and removing capture files so background children
        # cannot pin a slot or keep mutating a shard forever.
        if _is_process_group_alive(pid):
            if not _terminate_process_group(pid, 0.5):
                spool["error"] = "Process-group termination still pending after leader completion"
                _write_spool(spool_id, spool)
                return False
            if proc is not None:
                observed_exit_code = proc.poll()
            process_alive = False
        if spool.get("error") == "Process-group termination still pending after leader completion":
            spool["error"] = None

        # Process finished or output complete - finalize
        # Re-read paths (they're the same but clearer for the finalization section)
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

        # Capture (and reap) the child's exit code if we still hold its handle.
        # poll() returns None if the process hasn't actually exited yet (e.g.
        # output is complete but the CLI lingers); that's fine - exit_code stays
        # unknown for that case. None when there's no handle (orphan recovery).
        proc = _PROC_HANDLES.pop(spool_id, None)
        exit_code = observed_exit_code if proc is not None else _read_exit_code(spool_id)
        if exit_code is not None:
            spool["exit_code"] = exit_code
        # Suffix for the "no output" fallbacks so a silent failure reports the
        # code (distinguishes a kill/exec-failure from a clean but silent exit).
        no_output = "Process exited with no output" + (f" (exit code {exit_code})" if exit_code is not None else "")

        # Parse result based on harness type
        harness_type = spool.get("harness", "claude-code")

        if harness_type == "codex":
            # Parse Codex newline-delimited JSON format
            try:
                if stdout.strip():
                    # Store the agent's prose as the result, not the full event
                    # stream (which is mostly captured command output). The raw
                    # stream is preserved in the transcript. Fall back to stdout
                    # if no agent messages were found, so nothing is dropped.
                    extracted = _extract_codex_result(stdout)
                    spool["result"] = extracted if extracted is not None else stdout

                    codex_turn_state, _ = _codex_turn_state(stdout)
                    codex_failure_message = _codex_failure_message(stdout)
                    for line in stdout.strip().split("\n"):
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "thread.started":
                            spool["session_id"] = event.get("thread_id")
                        elif event.get("type") == "turn.completed":
                            usage = event.get("usage", {})
                            if usage:
                                spool["cost"] = usage

                    if codex_failure_message:
                        spool["status"] = "error"
                        spool["error"] = codex_failure_message
                    elif exit_code is None:
                        spool["status"] = "error"
                        spool["error"] = "Codex exit status unavailable"
                    elif exit_code != 0:
                        spool["status"] = "error"
                        spool["error"] = stderr.strip()[:500] or f"Codex exited with code {exit_code}"
                    elif codex_turn_state != "complete":
                        spool["status"] = "error"
                        spool["error"] = "Codex exited without a completed turn"
                    else:
                        spool["status"] = "complete"
                elif stderr.strip():
                    spool["status"] = "error"
                    spool["error"] = stderr[:500]
                else:
                    spool["status"] = "error"
                    spool["error"] = no_output
            except Exception:
                if stdout.strip():
                    spool["result"] = stdout
                    spool["status"] = "error"
                    spool["error"] = stderr.strip()[:500] or (
                        f"Codex exited with code {exit_code}"
                        if exit_code not in (None, 0)
                        else "Failed to parse Codex output"
                    )
                else:
                    spool["status"] = "error"
                    spool["error"] = "Failed to parse Codex output"

        elif harness_type == "gemini":
            # Gemini CLI: JSON output to stdout on success, error JSON to stderr
            if stdout.strip():
                try:
                    data = json.loads(stdout)
                    if isinstance(data, dict):
                        spool["result"] = data.get("response", stdout)
                        spool["session_id"] = data.get("session_id")
                    else:
                        # Valid JSON but not the expected object - keep raw output
                        spool["result"] = stdout
                    spool["status"] = "complete"
                except json.JSONDecodeError:
                    spool["result"] = stdout
                    spool["status"] = "complete"
            elif stderr.strip():
                # Extract structured error from multi-line JSON in stderr
                data = _extract_last_json_object(stderr)
                if data and "error" in data:
                    err = data["error"]
                    error_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    if error_msg == "[object Object]":
                        error_msg = _extract_gemini_stderr_error(stderr)
                    spool["session_id"] = data.get("session_id")
                    spool["status"] = "error"
                    spool["error"] = error_msg
                else:
                    spool["status"] = "error"
                    spool["error"] = stderr[:500]
            else:
                spool["status"] = "error"
                spool["error"] = no_output

        elif harness_type == "kimi":
            # Kimi CLI: JSONL (stream-json) output, one event per line. Store the
            # assistant's prose, not the raw stream (which embeds role:"tool"
            # lines carrying full file/command output). The full stream is kept
            # in the transcript. Fall back to raw stdout only if no assistant
            # text was found, so nothing is silently dropped.
            if stdout.strip():
                result_text = _extract_kimi_result(stdout)
                spool["result"] = result_text if result_text else stdout
                spool["status"] = "complete"
                # session_id already set when spool was created
            elif stderr.strip():
                # Kimi errors might be in stderr
                spool["status"] = "error"
                spool["error"] = stderr[:500]
            else:
                spool["status"] = "error"
                spool["error"] = no_output

        else:
            # Claude Code: JSON object or JSON array of events
            try:
                data = json.loads(stdout)
                cc_result = _extract_cc_result(data)
                if cc_result:
                    spool["result"] = cc_result.get("result", stdout)
                    spool["session_id"] = cc_result.get("session_id")
                    spool["cost"] = cc_result.get("cost") or cc_result.get("total_cost_usd")
                    spool["status"] = "complete"
                    if cc_result.get("is_error"):
                        spool["status"] = "error"
                        spool["error"] = cc_result.get("result", "Unknown error")
                    if cc_result.get("stop_reason") == "refusal":
                        # A safety classifier declined the session (HTTP 200 +
                        # stop_reason refusal, surfaced by the CLI as an API
                        # Error). Not a task failure — the caller should re-route
                        # or rephrase. Mark it distinctly so agents, triage, and
                        # skein recognize it instead of an undifferentiated error.
                        spool["status"] = "error"
                        refusal_text = cc_result.get("result") or spool.get("error") or ""
                        if not spool.get("error"):
                            spool["error"] = refusal_text or "Model declined the request (safety refusal)"
                        if _is_fable_gate(spool.get("model"), refusal_text):
                            # Fable's bio/cyber gate specifically — categorize and
                            # tag so orchestrators can route around it.
                            spool["error_kind"] = "fable_gate"
                            spool["gate_category"] = _refusal_category(data)
                            tags = spool.get("tags") or []
                            if "fable-gate" not in tags:
                                tags.append("fable-gate")
                            spool["tags"] = tags
                        else:
                            # Generic safety refusal from some other model — flag
                            # it, but don't attribute it to Fable or advise a
                            # re-route that may not help.
                            spool["error_kind"] = "safety_refusal"
                else:
                    # Parsed JSON but no recognizable result structure
                    spool["result"] = stdout
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
                    spool["error"] = no_output

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

        # A provider can reject a Codex turn after work has already landed in
        # ignored files. Preserve every newly created failed shard for explicit
        # inspection; no automatic Git cleanup is race-free here.
        _preserve_failed_spool_shard(spool)
        _write_spool(spool_id, spool)

        # Clean up output files
        if stdout_path.exists():
            stdout_path.unlink()
        if stderr_path.exists():
            stderr_path.unlink()
        exit_path = _get_exit_path(spool_id)
        if exit_path.exists():
            exit_path.unlink()

        return True


def _recover_orphans() -> None:
    """Check all running spools and finalize any that have completed.

    Also cleans up pending spools that never got a PID (spawn timeout).
    """
    now = datetime.now()
    for spool in _list_spools():
        if spool.get("status") == "running":
            if not _check_and_finalize_spool(spool["id"]):
                _start_spool_monitor(spool["id"])
        elif spool.get("status") == "pending" and not spool.get("pid"):
            # Check if this pending spool has been stuck too long
            created_at = spool.get("created_at")
            if created_at:
                try:
                    created_time = datetime.fromisoformat(created_at)
                    if (now - created_time).total_seconds() > PENDING_SPAWN_TIMEOUT:
                        spool["status"] = "error"
                        spool["error"] = "spawn timeout - never started"
                        spool["completed_at"] = now.isoformat()
                        _write_spool(spool["id"], spool)
                except (ValueError, TypeError):
                    pass


def _handle_expired_session(spool_id: str, spool: dict) -> bool:
    """Serialize transcript fallback with every terminal spool transition."""
    with _spool_lock(spool_id, blocking=False) as acquired:
        if not acquired:
            return False
        current = _read_spool(spool_id)
        if current is not None:
            if current.get("status") != "running":
                return True
            spool = current
        return _handle_expired_session_locked(spool_id, spool)


def _handle_expired_session_locked(spool_id: str, spool: dict) -> bool:
    """
    Handle expired session by retrying with transcript injection.

    Returns True if successfully retried, False otherwise.
    """
    # Find original spool with this session_id
    original_spool = _find_spool_by_session(spool["session_id"])
    if not original_spool:
        return False

    # Defense-in-depth: refuse a stored readonly/manual + shard spool before spawning, the
    # same authoritative check _spin_sync/_respin_sync run at creation. Reachability is
    # near-zero (creation rejects the pairing), but the transcript fallback below re-applies
    # the tier via _claude_permission_mode, where a stored "manual+shard"/"readonly+shard"
    # would resolve to bypassPermissions — so guard this chokepoint too instead of trusting an
    # upstream check. Mark the spool error (loud, surfaced by unspool/spool_info) and stop.
    orig_permission = original_spool.get("permission")
    orig_use_shard = _permission_implies_shard(orig_permission) or bool(original_spool.get("shard"))
    conflict = _readonly_shard_conflict_error(orig_permission, orig_use_shard)
    if conflict:
        logger.warning("spindle: expired-session fallback refused for %s: %s", spool_id, conflict)
        spool["status"] = "error"
        spool["error"] = f"Refused: {conflict}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        return True

    # Check for transcript
    transcript_path = _get_transcript_path(original_spool["id"])
    if not transcript_path.exists():
        return False

    # Kill the failing process
    pid = spool.get("pid")
    if pid and (_is_pid_alive(pid) or _is_process_group_alive(pid)):
        if not _terminate_process_group(pid, 0.2):
            logger.error("spindle: cannot retry %s while its old process group remains alive", spool_id)
            return False

    # Read transcript
    try:
        transcript = transcript_path.read_text()
    except IOError:
        return False

    # Build new prompt with transcript context
    context_prompt = f"""Previous conversation transcript:

{transcript}

---

Continue from above. New message: {spool["prompt"].split(": ", 1)[-1]}"""

    # Spawn new process without --resume flag, with transcript as context
    cmd = ["claude", "-p", context_prompt, "--output-format", "json"]

    # A bare `claude -p` (this transcript fallback) sets NEITHER --permission-mode
    # NOR --allowedTools, so the resumed spool silently changes capability from the
    # original spin — the same tier-drop as a bare `--resume`, surviving on the
    # expiry path only. Re-apply the tier the original spool ran under, exactly as
    # _respin_sync does: a careful resume stays auto, a readonly/manual resume keeps
    # its allowlist. This fallback creates no shard, so there is no readonly+shard
    # concern to guard against here.
    orig_permission = original_spool.get("permission")
    orig_allowed_tools = original_spool.get("allowed_tools")
    cmd.extend(["--permission-mode", _claude_permission_mode(orig_permission)])
    if orig_allowed_tools:
        cmd.extend(["--allowedTools", orig_allowed_tools])

    # Profile spools: rebuild the alt endpoint/key spawn env fresh and re-inject
    # --model/extra_args so the transcript fallback hits the same endpoint as the
    # original spin instead of the default api.anthropic.com. The recorded
    # effective model lives on the original spool (the failing respin spool does
    # not persist a model). caller_env (the only persisted env) is overlaid and
    # remains the only env written to disk.
    profile_name = spool.get("profile")
    caller_env = spool.get("env")
    spawn_env, eff_model, profile_extra_args, resolved = _profile_spawn_env(
        profile_name, caller_env, model=original_spool.get("model")
    )
    if resolved:
        if eff_model:
            cmd.extend(["--model", CLAUDE_MODEL_ALIASES.get(eff_model, eff_model)])
        if profile_extra_args:
            cmd.extend(profile_extra_args)

    try:
        new_pid = _spawn_detached(spool_id, cmd, spool["working_dir"], spawn_env)

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
        if not spool or spool.get("status") != "running":
            break
        if spool.get("timeout"):
            created = datetime.fromisoformat(spool["created_at"])
            now = datetime.now(timezone.utc) if created.tzinfo else datetime.now()
            elapsed = (now - created).total_seconds()
            if elapsed > spool["timeout"]:
                termination_pending = False
                with _spool_lock(spool_id, blocking=False) as acquired:
                    if not acquired:
                        time.sleep(MONITOR_POLL_INTERVAL)
                        continue
                    # Finalize/drop may have won while this monitor was waiting.
                    spool = _read_spool(spool_id)
                    if not spool or spool.get("status") != "running":
                        break
                    created = datetime.fromisoformat(spool["created_at"])
                    now = datetime.now(timezone.utc) if created.tzinfo else datetime.now()
                    elapsed = (now - created).total_seconds()
                    if elapsed <= spool["timeout"]:
                        continue
                    pid = spool.get("pid")
                    group_alive = bool(pid) and (_is_pid_alive(pid) or _is_process_group_alive(pid))
                    if group_alive and not _terminate_process_group(pid, 0.5):
                        spool["error"] = "Timeout reached; process-group termination still pending"
                        _write_spool(spool_id, spool)
                        termination_pending = True
                    else:
                        spool["status"] = "timeout"
                        spool["error"] = f"Timeout after {spool['timeout']}s"
                        spool["completed_at"] = datetime.now().isoformat()
                        _write_spool(spool_id, spool)
                        _PROC_HANDLES.pop(spool_id, None)
                        _get_exit_path(spool_id).unlink(missing_ok=True)
                if termination_pending:
                    time.sleep(MONITOR_POLL_INTERVAL)
                    continue
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
                            break  # Handled: retried with transcript, or refused terminally
                except IOError:
                    pass

        if _check_and_finalize_spool(spool_id):
            break
        time.sleep(MONITOR_POLL_INTERVAL)


def _run_spool_monitor(spool_id: str) -> None:
    try:
        _monitor_spool(spool_id)
    finally:
        with _SPOOL_MONITORS_LOCK:
            _SPOOL_MONITORS.discard(spool_id)


def _start_spool_monitor(spool_id: str) -> None:
    """Ensure exactly one background finalizer monitors a running spool."""
    with _SPOOL_MONITORS_LOCK:
        if spool_id in _SPOOL_MONITORS:
            return
        _SPOOL_MONITORS.add(spool_id)
    threading.Thread(target=_run_spool_monitor, args=(spool_id,), daemon=True).start()


def _spawn_detached(spool_id: str, cmd: list, cwd: str, env: Optional[Dict[str, str]] = None) -> int:
    """
    Spawn a detached process that survives parent death.
    Returns the PID.

    Args:
        spool_id: The spool ID for output files
        cmd: Command and arguments to execute
        cwd: Working directory
        env: Optional dict of environment variables to merge with current environment
    """
    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)
    exit_path = _get_exit_path(spool_id)
    exit_path.unlink(missing_ok=True)

    process_env = _process_env(env)
    executable = str(cmd[0]) if cmd else ""
    resolved_executable = (
        executable if os.path.isabs(executable) else shutil.which(executable, path=process_env.get("PATH"))
    )
    if not resolved_executable or not os.access(resolved_executable, os.X_OK):
        raise FileNotFoundError(f"executable not found or not runnable: {executable}")

    # A Popen handle is process-local and disappears when the Spindle server
    # restarts. Keep a tiny detached shell as the process-group leader so it can
    # persist the real child exit status for orphan recovery. The shell writes
    # one integer before exiting, so readers only inspect it after the PID is
    # dead; a partial/missing write fails closed. `"$@"` passes every argv item
    # literally, and no non-POSIX utility path is required.
    wrapper_script = 'status_file="$1"; shift; "$@"; rc=$?; printf "%s\\n" "$rc" > "$status_file"; exit "$rc"'
    wrapped_cmd = ["/bin/sh", "-c", wrapper_script, "spindle-exit-status", str(exit_path), *cmd]

    with open(stdout_path, "w") as stdout_file, open(stderr_path, "w") as stderr_file:
        proc = subprocess.Popen(
            wrapped_cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=process_env,
            start_new_session=True,  # Detach from parent
        )

    # Keep the handle for fast-path polling and reaping. The exit-status file is
    # authoritative after a restart when this in-memory handle no longer exists.
    _PROC_HANDLES[spool_id] = proc
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
    env: Optional[Dict[str, str]],
    base_branch: Optional[str] = None,
    research_target: Optional[str] = None,
    extra_args: Optional[list] = None,
    profile: Optional[str] = None,
    spawn_env: Optional[Dict[str, str]] = None,
) -> str:
    """Synchronous implementation of spin - runs in thread pool.

    ``env`` is the caller's explicit env and is the ONLY env persisted on the
    spool record. ``spawn_env`` (defaulting to ``env``) is what is actually
    injected into the child process; for profile spools it carries freshly
    resolved secrets that must never be written to disk.
    """
    # Env injected into the child; falls back to the persisted caller env when
    # no separate spawn env (i.e. no profile) was supplied.
    if spawn_env is None:
        spawn_env = env
    # Require working_dir - os.getcwd() returns MCP server dir, not caller's project
    if not working_dir:
        return "Error: working_dir required. Pass the project directory."

    # Resolve to absolute path to avoid cwd-dependent resolution
    working_dir = str(Path(working_dir).resolve())
    base_branch = base_branch or _detect_default_branch(working_dir)

    # Resolve permission to allowed_tools and check for auto-shard
    try:
        resolved_tools, auto_shard = _resolve_permission(
            permission,
            allowed_tools,
            research_target=research_target,
            working_dir=working_dir,
        )
        research_target_info = (
            _parse_research_target(research_target)
            if (permission or "careful") in {"research", "research+shard"}
            else None
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Authoritative incoherence check on the RESOLVED (tier, use_shard) pair: the
    # no-write readonly/manual tier + a shard, however the shard intent arrived
    # (permission="...+shard", or readonly/manual with shard=True/--shard). This is
    # the common launch chokepoint for spin() and spool_retry(); reject before a
    # slot is reserved so no spool launches.
    use_shard = shard or auto_shard
    conflict = _readonly_shard_conflict_error(permission, use_shard)
    if conflict:
        return f"Error: {conflict}"

    # Generate spool ID after validation so rejected research spins don't reserve slots.
    spool_id = str(uuid.uuid4())[:8]

    # Atomically check concurrency limit and create initial spool entry
    # This reserves the slot by creating a spool that counts toward the limit
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    # Slot reserved via spool creation - continue with setup

    cwd = working_dir

    # Handle shard creation (use_shard computed above)
    shard_info = None
    shard_error = None
    shard_newly_created = False
    if use_shard:
        # Reuse the worktree if working_dir already points inside an existing shard.
        # When reusing, keep the agent's cwd at the requested working_dir (which may
        # be a subdirectory of the worktree). shard_info["worktree_path"] holds the
        # actual worktree root for merge/cleanup paths.
        shard_info = _detect_existing_shard(cwd)
        if shard_info is None:
            shard_info, shard_error = _spawn_shard(spool_id, cwd, base_branch=base_branch)
            shard_newly_created = shard_info is not None
            if shard_info:
                cwd = shard_info["worktree_path"]
        if shard_info is None:
            if shard_error:
                return f"Error: Failed to create SHARD worktree — {shard_error}"
            return "Error: Failed to create SHARD worktree. Check git repo status."

    # Inject research guidance and SKEIN context for shard agents (unless skeinless=True)
    effective_prompt = prompt
    if research_target_info:
        effective_prompt = _research_target_preamble(research_target_info) + prompt

    omit_shard_commit_preamble = _research_omits_shard_commit_preamble(research_target_info)

    if _has_skein(working_dir) and shard_info and not skeinless and not omit_shard_commit_preamble:
        # Prepend SKEIN ignition instructions to the prompt
        worktree_name = shard_info.get("shard_id", spool_id)
        skein_preamble = f"""You are working in an isolated SHARD worktree.

Before starting work, orient yourself with SKEIN:
1. Run: skein ignite --message "{prompt[:100]}..."
2. Then: skein ready

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"
2. Tender: skein shard tender {worktree_name} --summary "What you did" --confidence N
   (confidence 1-10: 10=safe/isolated, 5=needs review, 1=risky)
3. Retire: skein torch && skein complete

Your task:
"""
        effective_prompt = skein_preamble + effective_prompt
    elif shard_info and not omit_shard_commit_preamble:
        # Non-SKEIN shard - still need commit instructions
        shard_preamble = """You are working in an isolated SHARD worktree.

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"

Your task:
"""
        effective_prompt = shard_preamble + effective_prompt

    claude_cmd = ["claude", "-p", effective_prompt, "--output-format", "json"]

    if model:
        resolved_model = CLAUDE_MODEL_ALIASES.get(model, model)
        claude_cmd.extend(["--model", resolved_model])

    # Select the permission mode for this tier. careful and the None default
    # resolve to auto (no allowlist); readonly/manual keep acceptEdits + their
    # tight allowlist; full/shard/+shard get bypassPermissions. See
    # _claude_permission_mode for the full table.
    claude_cmd.extend(["--permission-mode", _claude_permission_mode(permission)])

    if system_prompt:
        claude_cmd.extend(["--system-prompt", system_prompt])

    if resolved_tools:
        claude_cmd.extend(["--allowedTools", resolved_tools])

    # Profile extra_args are appended verbatim to the claude invocation.
    if extra_args:
        claude_cmd.extend(extra_args)

    # Wrap in bwrap sandbox for shards - worktree writable unless research output
    # is explicitly routed to a file/dir target.
    if shard_info and shutil.which("bwrap"):
        home = str(Path.home())
        # Bind the full worktree (not just cwd) so subdirectory-cwd shards can
        # still read/write any file in the shard.
        worktree_root = shard_info["worktree_path"]
        cmd = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",  # Root read-only
        ]
        if research_target_info and research_target_info["type"] in {"file", "dir"}:
            bind_path = _research_writable_path(research_target_info)
            cmd.extend(["--bind", bind_path, bind_path])
        else:
            cmd.extend(["--bind", worktree_root, worktree_root])
        cmd.extend(
            [
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
        )
        # Make git writable for commits in worktrees
        # Worktrees need:
        #   .git/worktrees/<name>/ - index, HEAD, logs
        #   .git/objects/ - store new blobs, trees, commits
        #   .git/refs/heads/ - update branch pointers
        git_file = Path(worktree_root) / ".git"
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
        for config_item in [".claude", ".claude.json", ".anthropic", ".spindle", ".config", ".cache"]:
            path = f"{home}/{config_item}"
            if Path(path).exists():
                cmd.extend(["--bind", path, path])
        # Extra caller-specified writable bind mounts (SPINDLE_SHARD_WRITABLE_BINDS=path1:path2:...)
        extra_binds_raw = os.environ.get("SPINDLE_SHARD_WRITABLE_BINDS", "")
        for raw in extra_binds_raw.split(":") if extra_binds_raw else []:
            raw = raw.strip()
            if not raw:
                continue
            if not os.path.isabs(raw):
                print(
                    f"spindle: SPINDLE_SHARD_WRITABLE_BINDS: skipping non-absolute path: {raw!r}",
                    file=sys.stderr,
                )
                continue
            if not Path(raw).exists():
                print(
                    f"spindle: SPINDLE_SHARD_WRITABLE_BINDS: skipping non-existent path: {raw!r}",
                    file=sys.stderr,
                )
                continue
            cmd.extend(["--bind", raw, raw])
        cmd.extend(claude_cmd)
    else:
        cmd = claude_cmd

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    # Review-tagged spools get a soft default timeout when the caller didn't
    # specify one. Reviews typically finish in 10-30 min; this caps wedged
    # spools (e.g. a self-referential pgrep bg-task loop — see friction-20260505-b87l).
    if timeout is None and any(_is_review_tag(t) for t in tag_list):
        timeout = DEFAULT_REVIEW_TIMEOUT

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
        "research_target": research_target,
        "system_prompt": system_prompt,
        "tags": tag_list,
        "shard": shard_info,
        "base_branch": base_branch,
        "model": model,
        "timeout": timeout,
        "env": env,
        "profile": profile,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "claude-code",
    }

    _write_spool(spool_id, spool)

    # Spawn detached process (spawn_env carries any profile secrets; never persisted)
    try:
        pid = _spawn_detached(spool_id, cmd, cwd, spawn_env)
    except Exception as e:
        # Spawn failed - mark spool as error so the slot is freed
        spool["status"] = "error"
        spool["error"] = f"spawn failed: {e}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        # Clean up shard worktree only if we created it; don't destroy pre-existing shards
        if shard_newly_created:
            _cleanup_shard(shard_info, working_dir)
        return f"Error: Failed to spawn process: {e}"

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread (daemon so it won't block shutdown)
    _start_spool_monitor(spool_id)

    return spool_id


@mcp.tool()
async def spin(
    prompt: str,
    permission: Optional[str] = None,
    research_target: Optional[str] = None,
    shard: bool = False,
    system_prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    tags: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    skeinless: bool = False,
    harness: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    base_branch: Optional[str] = None,
) -> str:
    """
    Spawn an agent to handle a task. Returns immediately with spool_id.

    The agent runs in background. Use unspool(spool_id) to get the result.

    Args:
        prompt: The task/question for the agent
        permission: Permission profile (claude-code harness). "careful" (default)
                    is now an alias of "auto" — classifier-vetted autonomous mode
                    where CC vets each tool call server-side with no allowlist; use
                    it for most code work including reviews/fells. "readonly" (alias
                    "manual") is the one tight, no-exec tier: Read/Grep/Glob + a few
                    safe read-only Bash rules, no python, enforced by an allowlist.
                    It cannot be combined with a shard (no write tools) — readonly/
                    manual + shard is rejected however the shard intent arrives.
                    "research" for web/file research with required research_target;
                    "full" for setup/install; "shard" or "careful+shard" for any
                    code-modifying work (adds isolated git worktree, bypass inside
                    the bwrap-contained shard); "auto"/"auto+shard" are explicit
                    aliases of the careful default.
        research_target: Required for permission="research" or "research+shard".
                         Accepted forms: site:<id>, file:<absolute-path>, dir:<absolute-path>.
        shard: Run in isolated git worktree (SKEIN-aware with graceful fallback)
        system_prompt: Optional system prompt to configure behavior
        working_dir: Directory for the agent to work in (defaults to current)
        allowed_tools: Override permission profile with explicit tool list
        tags: Comma-separated tags for organizing spools (e.g. "batch-1,triage")
        model: Model to use - for Claude: "haiku", "sonnet", "opus", "fable" (claude-fable-5, access ends 2026-07-12), or versioned aliases like "opus-4.8";
               for Gemini: "flash", "pro", or full model names like "gemini-2.5-pro";
               for Kimi: "thinking" (k2.6 in thinking mode), "k2.6", "k2.5", "latest", "k2.7-code"/"code" (coding-focused, thinking-only), "highspeed", or full model names.
               Use spin_harnesses() to see all available models.
        timeout: Kill spool after this many seconds (default: no timeout).
                 Exception: spools tagged with a review marker ("review", "fell-r1"
                 through "fell-r5") automatically get DEFAULT_REVIEW_TIMEOUT (default
                 90 min) when this is not set. Override SPINDLE_REVIEW_TIMEOUT env var
                 to change the default. Pass timeout=0 explicitly to disable.
        skeinless: Skip SKEIN context injection for shard agents (default: False)
        harness: Which harness to use - "claude-code" (default), "codex", "gemini", "kimi",
                 or the name of a lodged profile. A profile resolves to a base harness plus
                 overrides (model, alt-endpoint env, extra CLI flags); built-in names win
                 over a same-named profile. Use spin_harnesses() to see what's available.
        env: Optional dict of environment variables to set in spawned agent
        base_branch: Branch to fork shard from (default: auto-detected from repo). Only used with shard or careful+shard permissions.

    Returns:
        spool_id to check result later

    Example:
        spool_id = spin("Research the Python GIL")
        spool_id = spin("Fix the bug", permission="shard")  # full access + isolation
        spool_id = spin("Careful work", permission="careful+shard")
        spool_id = spin("Quick task", model="haiku", timeout=60)
        spool_id = spin("Write a parser", harness="codex")  # Use Codex instead
        spool_id = spin("Fix bug fast", harness="codex", permission="shard", model="codex")  # Codex + shard
        spool_id = spin("Summarize this", harness="gemini", model="flash")  # Use Gemini
        spool_id = spin("Analyze code", harness="kimi", model="thinking")  # Use Kimi
        spool_id = spin("Do something", env={"CC_THINKING_BOOST": "1"})
        spool_id = spin("Fork from branch", permission="shard", base_branch="feature-x")
        spool_id = spin("Research deepseek vs kimi", permission="research", research_target="site:spindle-development")
        result = unspool(spool_id)
    """
    # Normalize harness parameter (case-insensitive)
    harness_lower = harness.lower() if harness else None

    # Reject the incoherent readonly/manual + shard pairing up front, harness-
    # agnostic, before any harness resolves or a slot is reserved. This is the
    # friendly early copy (it also covers codex/gemini/kimi); the authoritative
    # check runs again at the claude launch chokepoints (_spin_sync/_respin_sync)
    # so the shard=True flag and retry/respin of a stored form cannot slip past.
    conflict = _readonly_shard_conflict_error(permission, shard or _permission_implies_shard(permission))
    if conflict:
        return json.dumps({"error": conflict})

    # Resolve harness against built-ins and lodged profiles. Built-in names win
    # over a same-named profile (with a warning); otherwise a non-built-in name
    # must match a lodged profile or it's an error.
    profile = None
    profile_extra_args = None
    profile_name = None
    if harness:
        if harness_lower in BUILTIN_HARNESSES:
            if _load_profile(harness) is not None:
                logger.warning(
                    "spindle: profile %r shadows built-in harness %r; using the built-in",
                    harness,
                    harness_lower,
                )
        else:
            profile = _load_profile(harness)
            if profile is None:
                valid = sorted(BUILTIN_HARNESSES | set(_discover_profiles().keys()))
                return json.dumps(
                    {
                        "error": f"Unknown harness or profile: {harness!r}. Valid: {', '.join(valid)}. Use spin_harnesses() to see details.",
                    }
                )

    # The caller's explicit env is the only env safe to persist on the spool
    # record. Profile-resolved env (which carries ANTHROPIC_API_KEY and any
    # op://-resolved secrets) must never hit disk, so it is built into a
    # separate spawn_env that goes only to the child process.
    caller_env = env
    spawn_env = env

    # Apply profile overrides: resolve its base harness, build the injected env,
    # fold in the profile-scoped model alias / default model, and capture its
    # extra CLI args. base harness is enforced to claude-code in v1, so a
    # resolved profile always routes through the Claude Code path below.
    if profile is not None:
        profile_name = profile["_name"]
        # Reconstruct the child spawn env (carrying fresh secrets), effective
        # model (caller model wins / alias-mapped / profile default), and extra
        # CLI args. strict=True surfaces a malformed profile to the caller
        # instead of silently degrading. caller_env stays the only persisted env.
        try:
            spawn_env, model, profile_extra_args, _ = _profile_spawn_env(
                profile_name, caller_env, model=model, strict=True
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        # base harness is enforced to claude-code in v1 (_resolve_profile_overrides
        # raises otherwise), so a resolved profile always routes through CC.
        harness_lower = "claude-code"

    # auto/auto+shard is CC-specific; non-CC harnesses have no classifier-vetted mode
    if permission and permission.startswith("auto") and harness_lower and harness_lower != "claude-code":
        return json.dumps(
            {
                "error": f"permission={permission!r} requires harness='claude-code'; {harness_lower!r} has no classifier-vetted mode.",
            }
        )

    # Route to appropriate harness
    if harness_lower == "codex":
        # Map Claude Code parameters to Codex parameters
        use_shard = shard or (permission and "shard" in permission)
        sandbox = _codex_sandbox_for_permission(permission, research_target)

        result = await asyncio.to_thread(
            _codex_spin_sync,
            prompt,
            working_dir,
            model,
            sandbox,
            timeout,
            tags,
            env,
            shard=use_shard,
            base_branch=base_branch or _detect_default_branch(working_dir or os.getcwd()),
            skeinless=skeinless,
            research_target=research_target,
            require_research_target=permission in {"research", "research+shard"},
            permission=permission,
        )
    elif harness_lower == "gemini":
        use_shard = shard or (permission and "shard" in permission)
        result = await asyncio.to_thread(
            _gemini_spin_sync,
            prompt,
            working_dir,
            model,
            system_prompt,
            timeout,
            tags,
            env,
            permission=permission,
            shard=use_shard,
            base_branch=base_branch or _detect_default_branch(working_dir or os.getcwd()),
            skeinless=skeinless,
            research_target=research_target,
            require_research_target=permission in {"research", "research+shard"},
        )
    elif harness_lower == "kimi":
        use_shard = shard or (permission and "shard" in permission)
        result = await asyncio.to_thread(
            _kimi_spin_sync,
            prompt,
            working_dir,
            model,
            system_prompt,
            timeout,
            tags,
            env,
            permission=permission,
            shard=use_shard,
            base_branch=base_branch or _detect_default_branch(working_dir or os.getcwd()),
            skeinless=skeinless,
            research_target=research_target,
            require_research_target=permission in {"research", "research+shard"},
        )
    else:
        # Default to Claude Code harness
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
            caller_env,
            base_branch=base_branch or _detect_default_branch(working_dir or os.getcwd()),
            research_target=research_target,
            extra_args=profile_extra_args,
            profile=profile_name,
            spawn_env=spawn_env,
        )

    return result


def _budget_result(text: str, spool_id: str) -> str:
    """Truncate very long results to head+tail with a breadcrumb.

    Returns text unchanged if under UNSPOOL_MAX_CHARS. Otherwise returns the
    first UNSPOOL_HEAD_CHARS and last UNSPOOL_TAIL_CHARS joined by a marker that
    tells the caller how to retrieve the rest. The full text remains in the
    spool JSON; this only shapes the default unspool read.
    """
    if len(text) <= UNSPOOL_MAX_CHARS:
        return text

    # Guard TAIL=0: text[-0:] is the whole string, not "", which would duplicate
    # the entire result after the head.
    head = text[:UNSPOOL_HEAD_CHARS]
    tail = text[-UNSPOOL_TAIL_CHARS:] if UNSPOOL_TAIL_CHARS else ""
    # Compute elided from the actual slice lengths (honest even when the windows
    # overlap or run past the text), not from the raw env constants.
    elided = len(text) - len(head) - len(tail)
    crumb = (
        f"\n\n[... {elided:,} of {len(text):,} chars elided "
        f"(showing first {len(head):,} and last {len(tail):,}) ...]\n"
        f'[full:   unspool("{spool_id}", full=True)]\n'
        f'[page:   unspool("{spool_id}", offset={len(head)}, limit=20000)]\n'
        f'[file:   spool_export("{spool_id}", format="md", output_path="/tmp/{spool_id}.md")]\n'
        f'[search: spool_grep("<pattern>", spool_id="{spool_id}")]\n\n'
    )
    truncated = head + crumb + tail
    # Only truncate when it actually saves space. If the windows overlap or the
    # elided slice is smaller than the breadcrumb (both only reachable via env
    # misconfig), the "truncated" form would be >= the original - return as-is
    # rather than emit a longer output with a misleading elided count.
    if len(truncated) >= len(text):
        return text
    return truncated


def _unspool_sync(spool_id: str) -> str:
    """Synchronous implementation of unspool - auto-detects harness."""
    # Auto-detect harness from spool metadata
    spool = _read_spool(spool_id)
    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    harness = spool.get("harness", "claude-code")
    harness_lower = harness.lower() if harness else "claude-code"

    # Route to appropriate harness implementation
    if harness_lower == "codex":
        return _codex_unspool_sync(spool_id)
    elif harness_lower == "gemini":
        return _gemini_unspool_sync(spool_id)
    elif harness_lower == "kimi":
        return _kimi_unspool_sync(spool_id)
    else:
        # Claude Code harness (default)
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
                    return _format_spool_failure(spool_id, spool)
            return f"Spool {spool_id} still running: {spool.get('prompt', '')[:50]}..."
        elif status == "complete":
            return spool.get("result", "No result")
        else:
            return _format_spool_failure(spool_id, spool)


@mcp.tool()
async def unspool(
    spool_id: str,
    full: bool = False,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """
    Get the result of a background spin task.

    Very long results (over ~50K chars) are truncated by default to their head
    and tail with a breadcrumb showing how to pull the rest. Most results are
    small and return whole.

    Args:
        spool_id: The spool to read.
        full: Return the entire result with no truncation.
        offset: Start returning the result at this character index (paging).
        limit: Max characters to return when paging (default: to end).

    Example:
        unspool("abc123")                      # budgeted (head+tail if huge)
        unspool("abc123", full=True)           # entire result
        unspool("abc123", offset=12000, limit=20000)  # page a slice
    """
    import asyncio

    raw = await asyncio.to_thread(_unspool_sync, spool_id)

    # Paging and budgeting only apply to a materialized result. For a spool that
    # isn't complete, _unspool_sync returns a short status/error sentinel
    # ("pending", "still running", "failed: ...") - return it verbatim rather
    # than slicing or head/tail-wrapping a sentinel string.
    spool = _read_spool(spool_id)
    if not spool or spool.get("status") != "complete":
        return raw

    # Results are normally strings, but coerce defensively so paging's slicing
    # and budgeting's len() never hit a dict/list (parity with spool_grep).
    if not isinstance(raw, str):
        raw = json.dumps(raw, indent=2)

    # Paging: return an explicit slice with position markers.
    if offset is not None or limit is not None:
        if limit is not None and limit <= 0:
            return (
                f"[invalid limit {limit}: must be positive] result is "
                f'{len(raw):,} chars; page with unspool("{spool_id}", offset=N, '
                f'limit=M) using M>0, or unspool("{spool_id}", full=True)'
            )
        start = min(max(0, offset or 0), len(raw))
        end = start + limit if limit is not None else len(raw)
        chunk = raw[start:end]
        shown_end = min(end, len(raw))
        header = f"[chars {start:,}-{shown_end:,} of {len(raw):,}]\n"
        footer = ""
        if shown_end < len(raw):
            # Reachable only when limit is not None (a None limit pages to the
            # end, so shown_end == len(raw) and no footer fires).
            footer = f'\n[more: unspool("{spool_id}", offset={shown_end}, limit={limit})]'
        return header + chunk + footer

    if full:
        return raw

    return _budget_result(raw, spool_id)


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


def _spin_drop_sync(spool_id: str) -> str:
    """Synchronous implementation of spin_drop."""
    deadline = time.monotonic() + SPOOL_TERMINAL_LOCK_TIMEOUT
    while True:
        with _spool_lock(spool_id, blocking=False) as acquired:
            if acquired:
                return _spin_drop_locked(spool_id)
        if time.monotonic() >= deadline:
            return f"Error: Could not lock spool {spool_id} for cancellation"
        time.sleep(0.05)


def _spin_drop_locked(spool_id: str) -> str:
    """Cancel a spool while its terminal-transition lock is held."""
    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get("status") != "running":
        return f"Spool {spool_id} is not running (status: {spool.get('status')})"

    pid = spool.get("pid")

    if not pid:
        return f"Spool {spool_id} has no PID recorded yet"

    if not _terminate_process_group(pid, 0.5):
        return f"Error: Could not terminate process group for spool {spool_id}"

    spool["status"] = "error"
    spool["error"] = "Cancelled by user"
    spool["completed_at"] = datetime.now().isoformat()
    _write_spool(spool_id, spool)
    _PROC_HANDLES.pop(spool_id, None)

    stdout_path = _get_output_path(spool_id)
    stderr_path = _get_stderr_path(spool_id)
    if stdout_path.exists():
        stdout_path.unlink()
    if stderr_path.exists():
        stderr_path.unlink()
    _get_exit_path(spool_id).unlink(missing_ok=True)

    return f"Dropped spool {spool_id}"


def _spool_peek_sync(spool_id: str, lines: int = 50) -> str:
    """Synchronous implementation of spool_peek."""
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


def _spin_wait_sync(
    spool_ids: Optional[str] = None,
    mode: str = "gather",
    timeout: Optional[int] = None,
    time_param: Optional[str] = None,
) -> str:
    """Synchronous implementation of spin_wait."""
    import time as time_module

    # Time-based waiting mode (no spool_ids)
    if time_param and not spool_ids:
        duration_seconds = _parse_duration(time_param)
        if duration_seconds is None:
            return f"Error: Invalid time format '{time_param}'. Use: 30s, 90m, 2h, or HH:MM"

        start_time = datetime.now()
        time_module.sleep(duration_seconds)
        elapsed = int((datetime.now() - start_time).total_seconds())

        return json.dumps(
            {
                "waited": time_param,
                "elapsed_seconds": elapsed,
                "interrupted": False,
                "started_at": start_time.isoformat(),
                "ended_at": datetime.now().isoformat(),
            },
            indent=2,
        )

    # Must have spool_ids for spool-waiting mode
    if not spool_ids:
        return "Error: Provide spool_ids to wait for spools, or time to wait for a duration"

    ids = [s.strip() for s in spool_ids.split(",")]
    start_time = datetime.now()
    poll_interval = 3  # seconds

    if mode == "yield":
        # Return first completed spool with its ID so caller can track progress.
        # Includes remaining IDs for the caller to use in the next yield call.
        while True:
            for spool_id in ids:
                _check_and_finalize_spool(spool_id)
                spool = _read_spool(spool_id)
                if not spool:
                    return json.dumps({"spool_id": spool_id, "error": f"Unknown spool_id '{spool_id}'"})
                if spool.get("status") == "complete":
                    remaining = [s for s in ids if s != spool_id]
                    result = spool.get("result", "No result")
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "result": result,
                            "remaining": remaining,
                        }
                    )
                elif spool.get("status") == "error":
                    remaining = [s for s in ids if s != spool_id]
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "error": spool.get("error"),
                            "remaining": remaining,
                        }
                    )
                elif spool.get("status") == "timeout":
                    remaining = [s for s in ids if s != spool_id]
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "error": spool.get("error", "Spool timed out"),
                            "remaining": remaining,
                        }
                    )

            if timeout:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    return f"Timeout after {timeout}s. Spools still running: {', '.join(ids)}"

            time_module.sleep(poll_interval)
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
                elif spool.get("status") == "timeout":
                    results[spool_id] = f"Error: {spool.get('error', 'Spool timed out')}"
                    pending.remove(spool_id)

            if not pending:
                break

            if timeout:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    return f"Timeout after {timeout}s. Still pending: {', '.join(pending)}. Completed: {json.dumps(results)}"

            time_module.sleep(poll_interval)

        return json.dumps(results, indent=2)


@mcp.tool()
async def spools() -> str:
    """
    List all spools (running and completed).

    Returns:
        JSON object with spool statuses
    """
    import asyncio

    return await asyncio.to_thread(_spools_sync)


def _respin_sync(handle: str, prompt: str) -> str:
    """Synchronous implementation of respin - auto-detects harness.

    `handle` may be either the spool_id returned by spin() (preferred, and
    consistent with every other spindle entrypoint) or a raw session_id
    (legacy contract). It is resolved to the original spool, and the spool's
    real session_id is what flows down to the harness resume path - never
    the raw caller handle, which may be a spool_id.
    """
    # Resolve the original spool to detect harness (accepts spool_id or session_id)
    original_spool = _resolve_spool_for_respin(handle)
    if not original_spool:
        return f"Error: No spool found for handle '{handle}'"

    # respin proceeds only when the spool reached a terminal state with a
    # usable session_id. A still-running spool may already have its
    # session_id set - codex sets it mid-stream from the thread_id event
    # while the original process is still working - so a `not session_id`
    # check alone would let a running spool flow to `codex exec resume
    # <thread-id>` while the original process still holds that session: a
    # concurrent-resume hazard. Gate on non-terminal status first.
    #
    # `pending` and `running` are the only non-terminal states; everything
    # else - `complete`, `error`, `timeout` (wall-clock kill in
    # _monitor_spool), and any future terminal status - falls through to the
    # `not session_id` check, which resumes if a session exists or returns
    # the accurate "no resumable session" error otherwise. An allow-list of
    # non-terminal states (rather than a deny-list of terminal ones) keeps a
    # timed-out-with-session spool resumable and avoids mislabeling unknown
    # statuses as "still running".
    status = original_spool.get("status", "unknown")
    if status in ("pending", "running"):
        return (
            f"Spool '{original_spool.get('id', handle)}' is not in a resumable "
            f"state (status={status}); wait for it to complete before respin"
        )

    # Always resume against the spool's real session_id, not the caller's
    # handle (which may be a spool_id). For codex/gemini/kimi this is the
    # opaque harness thread-id; for claude-code it's the claude session id.
    session_id = original_spool.get("session_id")
    if not session_id:
        return f"Spool '{original_spool.get('id', handle)}' completed without a resumable session (status={status})"

    harness = original_spool.get("harness", "claude-code")

    # Route to appropriate harness implementation
    if harness == "codex":
        return _codex_respin_sync(session_id, prompt)
    elif harness == "gemini":
        return _gemini_respin_sync(session_id, prompt, original_spool)
    elif harness == "kimi":
        return _kimi_respin_sync(session_id, prompt, original_spool)
    else:
        # Claude Code harness (default)
        # A stored readonly/manual tier paired with a shard is incoherent (round-2
        # fell): the tier has no write tools, so a resumed worktree spool can't do
        # useful work — and a stored "manual+shard" would otherwise escalate to
        # bypassPermissions via _claude_permission_mode below. Reject on the resolved
        # (tier, use_shard) pair before reserving a slot — the same authoritative
        # check _spin_sync runs.
        orig_permission = original_spool.get("permission")
        orig_use_shard = _permission_implies_shard(orig_permission) or bool(original_spool.get("shard"))
        conflict = _readonly_shard_conflict_error(orig_permission, orig_use_shard)
        if conflict:
            return f"Error: {conflict}"

        # Generate spool ID first
        spool_id = str(uuid.uuid4())[:8]

        # Atomically check concurrency limit and create initial spool entry
        success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
        if not success:
            return error_msg

        # Slot reserved via spool creation - continue with setup

        # Try to resume with session_id first
        # If that fails (session expired), fall back to transcript injection
        cmd = ["claude", "-p", prompt, "--resume", session_id, "--output-format", "json"]

        # A bare `claude --resume` sets NEITHER --permission-mode NOR --allowedTools,
        # so a resumed spool silently changes capability from the original spin (a
        # bare resume of a careful spool denies `python3 -c ...`, which careful=auto
        # permits). Re-apply the tier the original spool ran under so a careful
        # resume stays auto, a readonly resume keeps its allowlist, etc. The stored
        # allowed_tools mirrors exactly what the original spin used, so no
        # re-resolution (and no research-target re-validation) is needed here.
        orig_allowed_tools = original_spool.get("allowed_tools")
        cmd.extend(["--permission-mode", _claude_permission_mode(orig_permission)])
        if orig_allowed_tools:
            cmd.extend(["--allowedTools", orig_allowed_tools])

        cwd = os.getcwd()

        # Check if we have a transcript for this session
        transcript_available = False
        if original_spool:
            transcript_path = _get_transcript_path(original_spool["id"])
            transcript_available = transcript_path.exists()

        # The caller's explicit env is all that was persisted on the original
        # spool (profile secrets are never written to disk). It is what we
        # persist again and what we overlay on a re-resolved profile env.
        caller_env = original_spool.get("env") if original_spool else None
        spawn_env = caller_env

        # Profile spools: re-resolve the profile fresh so the resume hits the
        # same endpoint/config_dir (and picks up any rotated secrets), and
        # re-inject the model + extra_args — a --resume against an alt
        # ANTHROPIC_BASE_URL still needs --model specified explicitly. The
        # re-resolved env carries secrets, so it goes only into spawn_env; the
        # caller's non-secret env overrides are reapplied on top of it.
        #
        # If the profile was deleted or has gone malformed since the original
        # spin, _profile_spawn_env logs and returns resolved=False: we degrade
        # to the persisted caller env, which no longer carries the alt
        # base_url/api_key, so the resume loses the alt endpoint. We skip
        # re-injecting --model/extra_args in that case (matching a non-profile
        # resume) rather than forcing an alt-endpoint model onto the default
        # endpoint.
        profile_name = original_spool.get("profile") if original_spool else None
        # resume_model is the effective model recorded at spin. When it is None
        # (profile sets no default and no caller model was given) we deliberately
        # omit --model and let the alt endpoint use its own default — matching
        # the original spin.
        spawn_env, resume_model, profile_extra_args, resolved = _profile_spawn_env(
            profile_name, caller_env, model=original_spool.get("model")
        )
        if resolved:
            if resume_model:
                cmd.extend(["--model", CLAUDE_MODEL_ALIASES.get(resume_model, resume_model)])
            if profile_extra_args:
                cmd.extend(profile_extra_args)

        spool = {
            "id": spool_id,
            "status": "pending",
            "prompt": f"Continue {session_id}: {prompt}",
            "result": None,
            "session_id": session_id,
            "working_dir": cwd,
            "allowed_tools": orig_allowed_tools,
            "permission": orig_permission,
            "system_prompt": None,
            "transcript_fallback_available": transcript_available,
            "env": caller_env,
            "profile": profile_name,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "pid": None,
            "cost": None,
            "error": None,
            "harness": "claude-code",
        }

        _write_spool(spool_id, spool)

        # Spawn detached process (spawn_env carries any profile secrets; never persisted)
        pid = _spawn_detached(spool_id, cmd, cwd, spawn_env)

        spool["pid"] = pid
        spool["status"] = "running"
        _write_spool(spool_id, spool)

        # Start background monitor
        _start_spool_monitor(spool_id)

        return spool_id


@mcp.tool()
async def respin(
    session_id: str,
    prompt: str,
) -> str:
    """
    Continue an existing session with a new message.
    Returns immediately with spool_id.

    Auto-detects the harness (claude-code, codex, gemini, kimi) from the
    original spool. For Claude Code sessions, falls back to transcript
    injection if the session has expired.

    Args:
        session_id: The handle of the session to continue. Accepts the
            spool_id returned by spin() (preferred - consistent with every
            other spindle entrypoint) or a raw session_id (legacy). The
            spool's real session_id is resolved internally before resuming.
        prompt: The follow-up message/task

    Returns:
        spool_id to check result later
    """
    return await asyncio.to_thread(_respin_sync, session_id, prompt)


@mcp.tool()
async def spin_wait(
    spool_ids: Optional[str] = None,
    mode: str = "gather",
    timeout: Optional[int] = None,
    time: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """
    Block until spools complete.

    Args:
        spool_ids: Comma-separated spool IDs to wait for
        mode: 'gather' (wait for all) or 'yield' (return first completed)
        timeout: Optional timeout in seconds
        time: Duration to wait (when no spool_ids provided).
              Formats: "90m" (minutes), "2h" (hours), "30s" (seconds),
              or "06:00" (absolute time, wait until then)

    Returns:
        Results from completed spools, or wait status if using time parameter.
        In yield mode, returns JSON with {spool_id, result, remaining} so the
        caller knows which spool completed and can pass remaining IDs to the
        next yield call.

    Examples:
        spin_wait("abc123,def456")  # Wait for spools
        spin_wait(time="90m")       # Sleep for 90 minutes
        spin_wait(time="2h")        # Sleep for 2 hours
        spin_wait(time="06:00")     # Wait until 6 AM
    """
    # Heartbeat chunk size: sleep in 60s intervals to keep MCP connection alive.
    # Long blocking sleeps cause the calling CC session's heartbeat to stop.
    HEARTBEAT_INTERVAL = 60  # seconds

    # Time-based waiting mode (no spool_ids)
    if time and not spool_ids:
        duration_seconds = _parse_duration(time)
        if duration_seconds is None:
            return f"Error: Invalid time format '{time}'. Use: 30s, 90m, 2h, or HH:MM"

        start_time = datetime.now()
        remaining = duration_seconds
        total_chunks = max(1, (duration_seconds + HEARTBEAT_INTERVAL - 1) // HEARTBEAT_INTERVAL)
        chunks_done = 0

        while remaining > 0:
            chunk = min(HEARTBEAT_INTERVAL, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk
            chunks_done += 1

            if ctx and remaining > 0:
                elapsed = int((datetime.now() - start_time).total_seconds())
                try:
                    await ctx.report_progress(chunks_done, total_chunks)
                except Exception:
                    pass
                try:
                    await ctx.info(f"spin_wait: {elapsed}s/{duration_seconds}s elapsed")
                except Exception:
                    pass

        elapsed = int((datetime.now() - start_time).total_seconds())
        return json.dumps(
            {
                "waited": time,
                "elapsed_seconds": elapsed,
                "interrupted": False,
                "started_at": start_time.isoformat(),
                "ended_at": datetime.now().isoformat(),
            },
            indent=2,
        )

    # Must have spool_ids for spool-waiting mode
    if not spool_ids:
        return "Error: Provide spool_ids to wait for spools, or time to wait for a duration"

    ids = [s.strip() for s in spool_ids.split(",")]
    start_time = datetime.now()
    poll_interval = 3  # seconds
    last_heartbeat = datetime.now()

    if mode == "yield":
        while True:
            for spool_id in ids:
                await asyncio.to_thread(_check_and_finalize_spool, spool_id)
                spool = await asyncio.to_thread(_read_spool, spool_id)
                if not spool:
                    return json.dumps({"spool_id": spool_id, "error": f"Unknown spool_id '{spool_id}'"})
                if spool.get("status") == "complete":
                    remaining_ids = [s for s in ids if s != spool_id]
                    result = spool.get("result", "No result")
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "result": result,
                            "remaining": remaining_ids,
                        }
                    )
                elif spool.get("status") == "error":
                    remaining_ids = [s for s in ids if s != spool_id]
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "error": spool.get("error"),
                            "remaining": remaining_ids,
                        }
                    )

            if timeout:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    return f"Timeout after {timeout}s. Spools still running: {', '.join(ids)}"

            # Periodic heartbeat to keep MCP connection alive
            now = datetime.now()
            if ctx and (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL:
                elapsed = int((now - start_time).total_seconds())
                try:
                    await ctx.info(f"spin_wait: waiting {elapsed}s, pending: {', '.join(ids)}")
                except Exception:
                    pass
                last_heartbeat = now

            await asyncio.sleep(poll_interval)
    else:
        # gather mode - wait for all
        results = {}
        pending = set(ids)

        while pending:
            for spool_id in list(pending):
                await asyncio.to_thread(_check_and_finalize_spool, spool_id)
                spool = await asyncio.to_thread(_read_spool, spool_id)
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

            # Periodic heartbeat to keep MCP connection alive
            now = datetime.now()
            if ctx and (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL:
                elapsed = int((now - start_time).total_seconds())
                try:
                    await ctx.info(
                        f"spin_wait: waiting {elapsed}s, pending: {', '.join(pending)}, done: {len(results)}/{len(ids)}"
                    )
                except Exception:
                    pass
                last_heartbeat = now

            await asyncio.sleep(poll_interval)

        return json.dumps(results, indent=2)


@mcp.tool()
async def spin_sleep(duration: str) -> str:
    """
    Sleep for a specified duration.

    A simpler interface for timed waiting when you don't need to wait on spools.

    Uses a heartbeat approach with 3.5-minute (210 second) chunks to avoid
    MCP 5-minute timeout issues on long sleeps.

    Args:
        duration: How long to sleep.
            Formats: "90m" (minutes), "2h" (hours), "30s" (seconds),
            or "06:00" (absolute time, wait until then)

    Returns:
        JSON with wait details (elapsed time, timestamps, chunks info)

    Example:
        spin_sleep("90m")       # Sleep for 90 minutes
        spin_sleep("2h")        # Sleep for 2 hours
        spin_sleep("30s")       # Sleep for 30 seconds
        spin_sleep("06:00")     # Wait until 6 AM
    """
    duration_seconds = _parse_duration(duration)
    if duration_seconds is None:
        return f"Error: Invalid duration format '{duration}'. Use: 30s, 90m, 2h, or HH:MM"

    start_time = datetime.now()
    interrupted = False

    # Heartbeat approach: split into 210-second (3.5 minute) chunks
    CHUNK_SIZE = 210  # 3.5 minutes, safely under 5-minute MCP timeout
    total_chunks = (duration_seconds + CHUNK_SIZE - 1) // CHUNK_SIZE  # ceiling division
    chunks_completed = 0

    try:
        remaining = duration_seconds
        while remaining > 0:
            chunk_duration = min(CHUNK_SIZE, remaining)

            # Sleep for this chunk using asyncio.sleep (non-blocking)
            await asyncio.sleep(chunk_duration)

            chunks_completed += 1
            remaining -= chunk_duration

            # Progress tracking (logged but not returned until final)
            # Note: We can't send intermediate updates in MCP tool model,
            # but the asyncio.sleep() keeps the event loop alive

    except asyncio.CancelledError:
        # Handle cancellation gracefully
        interrupted = True
        elapsed = int((datetime.now() - start_time).total_seconds())
        return json.dumps(
            {
                "duration": duration,
                "elapsed_seconds": elapsed,
                "interrupted": True,
                "chunks_completed": chunks_completed,
                "total_chunks": total_chunks,
                "started_at": start_time.isoformat(),
                "ended_at": datetime.now().isoformat(),
            },
            indent=2,
        )
    except Exception as e:
        # Handle any other errors
        interrupted = True
        elapsed = int((datetime.now() - start_time).total_seconds())
        return json.dumps(
            {
                "duration": duration,
                "elapsed_seconds": elapsed,
                "interrupted": True,
                "error": str(e),
                "chunks_completed": chunks_completed,
                "total_chunks": total_chunks,
                "started_at": start_time.isoformat(),
                "ended_at": datetime.now().isoformat(),
            },
            indent=2,
        )

    end_time = datetime.now()
    elapsed = int((end_time - start_time).total_seconds())

    return json.dumps(
        {
            "duration": duration,
            "elapsed_seconds": elapsed,
            "interrupted": interrupted,
            "chunks_completed": chunks_completed,
            "total_chunks": total_chunks,
            "started_at": start_time.isoformat(),
            "ended_at": end_time.isoformat(),
        },
        indent=2,
    )


@mcp.tool()
async def spin_drop(spool_id: str) -> str:
    """
    Cancel a running spool by killing its process.

    Args:
        spool_id: The spool_id to cancel

    Returns:
        Success or error message
    """
    return await asyncio.to_thread(_spin_drop_sync, spool_id)


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
async def spool_grep(pattern: str, spool_id: Optional[str] = None, context: int = 2) -> str:
    """
    Regex search through spool results.

    Without spool_id, sweeps all spools and reports which ones match (the match
    strings plus a count) - good for "which spool mentioned X". With spool_id,
    searches that one result and returns matching lines with surrounding context
    - the way to dig into a single huge result without pulling the whole thing.

    Args:
        pattern: Regular expression pattern to search for (case-insensitive).
        spool_id: Limit the search to one spool and return line-level context.
        context: Lines of context to show around each match (single-spool mode).

    Returns:
        Cross-spool: matching spool IDs with matched strings.
        Single-spool: matching lines with context and line numbers.

    Example:
        spool_grep("friction-[0-9]+-[a-z]+")           # which spools mention it
        spool_grep("error|failed", spool_id="abc123")  # find lines in one result
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    # Single-spool mode: line-level matches with context.
    if spool_id:
        # Finalize first so a just-finished spool reports its result, matching
        # unspool's behavior.
        _check_and_finalize_spool(spool_id)
        spool = _read_spool(spool_id)
        if not spool:
            return f"Error: Unknown spool_id '{spool_id}'"
        result = spool.get("result", "") or ""
        if isinstance(result, (dict, list)):
            result = json.dumps(result, indent=2)
        if not result:
            return f"No result for spool {spool_id} (status: {spool.get('status')})"

        context = max(0, context)
        lines = result.splitlines()
        hit_idxs = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hit_idxs:
            return f"No lines in spool {spool_id} matching '{pattern}'"

        # Build context windows, merging overlapping ranges.
        wanted = set()
        for i in hit_idxs:
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                wanted.add(j)

        out = [f"[spool {spool_id} - {len(hit_idxs)} matching line(s) of {len(lines)} total]"]
        prev = None
        for j in sorted(wanted):
            if prev is not None and j != prev + 1:
                out.append("--")
            marker = ":" if j in hit_idxs else " "
            out.append(f"{j + 1}{marker} {lines[j]}")
            prev = j
        return "\n".join(out)

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

    def _bg_task_summary() -> Optional[str]:
        """Return a bg-task fallback string if tasks exist, else None."""
        if spool.get("harness", "claude-code") != "claude-code":
            return None
        session_id = spool.get("session_id")
        if not session_id:
            return None
        bg_tasks = _get_cc_bg_tasks(session_id)
        if not bg_tasks:
            return None
        task_lines = [f"background tasks for session {session_id}:"]
        for t in bg_tasks:
            status_str = t.get("status", "unknown")
            subject = t.get("subject", "")
            active = t.get("activeForm", "")
            task_lines.append(
                f"  task {t.get('id')}: {status_str} - {subject}"
                + (f" ({active})" if active and active != subject else "")
            )
        return "[spool %s - main output empty, showing bg tasks]\n%s" % (spool_id, "\n".join(task_lines))

    if not stdout_path.exists():
        fallback = _bg_task_summary()
        if fallback:
            return fallback
        return f"No output yet for spool {spool_id}"

    try:
        with open(stdout_path, "r") as f:
            all_lines = f.readlines()

        if not all_lines:
            fallback = _bg_task_summary()
            if fallback:
                return fallback
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

    # Re-spin with same parameters - route to appropriate harness
    tags = spool.get("tags")
    tags_str = ",".join(tags) if tags else None

    harness = spool.get("harness", "claude-code")
    harness_lower = harness.lower() if harness else "claude-code"

    if harness_lower == "codex":
        # Use stored sandbox directly (Codex spools store sandbox, not permission)
        sandbox = spool.get("sandbox", "workspace-write")
        retry_working_dir = spool.get("working_dir")
        retry_base_branch = spool.get("base_branch") or _detect_default_branch(retry_working_dir or os.getcwd())

        return await asyncio.to_thread(
            _codex_spin_sync,
            spool.get("prompt", ""),
            retry_working_dir,
            spool.get("model"),
            sandbox,
            spool.get("timeout"),
            tags_str,
            spool.get("env"),
            shard=bool(spool.get("shard")),
            base_branch=retry_base_branch,
            # Carry the research target + permission so a retried research/file spool
            # keeps its --add-dir grant and target preamble (the sandbox tier already
            # survives via the stored `sandbox` above). Without these a retry runs a
            # plain workspace-write spool that can't write its output.
            research_target=spool.get("research_target"),
            permission=spool.get("permission"),
        )
    elif harness_lower == "gemini":
        return await asyncio.to_thread(
            _gemini_spin_sync,
            spool.get("prompt", ""),
            spool.get("working_dir"),
            spool.get("model"),
            spool.get("system_prompt"),
            spool.get("timeout"),
            tags_str,
            spool.get("env"),
        )
    elif harness_lower == "kimi":
        return await asyncio.to_thread(
            _kimi_spin_sync,
            spool.get("prompt", ""),
            spool.get("working_dir"),
            spool.get("model"),
            spool.get("system_prompt"),
            spool.get("timeout"),
            tags_str,
            spool.get("env"),
        )
    else:
        # Default to Claude Code harness
        retry_working_dir = spool.get("working_dir")
        retry_base_branch = spool.get("base_branch") or _detect_default_branch(retry_working_dir or os.getcwd())
        # Profile spools: re-resolve the profile fresh so the retry hits the same
        # alt endpoint/key with the recorded model + extra_args. The persisted
        # env is caller-only (secrets are never written to disk), so reading it
        # back without re-resolving would silently retry against the default
        # endpoint. caller_env stays the only env persisted on the new spool.
        caller_env = spool.get("env")
        profile_name = spool.get("profile")
        spawn_env, retry_model, profile_extra_args, _ = _profile_spawn_env(
            profile_name, caller_env, model=spool.get("model")
        )
        return await asyncio.to_thread(
            _spin_sync,
            spool.get("prompt", ""),  # prompt
            spool.get("permission"),  # permission
            bool(spool.get("shard")),  # shard
            spool.get("system_prompt"),  # system_prompt
            retry_working_dir,  # working_dir
            spool.get("allowed_tools"),  # allowed_tools
            tags_str,  # tags
            retry_model,  # model
            spool.get("timeout"),  # timeout
            False,  # skeinless
            caller_env,  # env (persisted; caller-only, no secrets)
            retry_base_branch,  # base_branch
            extra_args=profile_extra_args,
            profile=profile_name,
            spawn_env=spawn_env,
        )


def _shard_base_branch(spool: dict) -> str:
    """Resolve the base branch a shard was forked from.

    Prefers the value persisted on the spool record. Falls back to detecting
    the default branch of the main repo (worktree's parent.parent), so older
    spools created before base_branch was persisted still work on main repos.
    """
    base = spool.get("base_branch")
    if base:
        return base
    shard_info = spool.get("shard") or {}
    worktree_path = shard_info.get("worktree_path")
    if worktree_path:
        main_repo = Path(worktree_path).parent.parent
        if main_repo.exists():
            return _detect_default_branch(str(main_repo))
    return "master"


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

    base_branch = _shard_base_branch(spool)

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.returncode != 0:
            return "unknown"
        has_uncommitted = bool(result.stdout.strip())

        # Check for commits ahead of base branch
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=10,
        )
        if result.returncode != 0:
            return "unknown"
        commits_ahead = int(result.stdout.strip())

        if has_uncommitted:
            return "uncommitted"

        if commits_ahead == 0:
            return "no_changes"

        # Check for potential merge conflicts
        main_repo = Path(worktree_path).parent.parent
        branch_name = shard_info.get("branch_name")
        if branch_name:
            # Find merge base first
            result = subprocess.run(
                ["git", "merge-base", base_branch, branch_name],
                capture_output=True,
                text=True,
                cwd=str(main_repo),
                timeout=10,
            )
            if result.returncode == 0:
                merge_base = result.stdout.strip()
                # Use 3-way merge-tree with explicit base (old-style merge-tree)
                result = subprocess.run(
                    ["git", "merge-tree", merge_base, base_branch, branch_name],
                    capture_output=True,
                    text=True,
                    cwd=str(main_repo),
                    timeout=10,
                )
                # Check for conflict markers in output
                if "<<<<<<" in result.stdout or "+<<<<<<" in result.stdout:
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

    base_branch = _shard_base_branch(spool)

    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "--stat-width=1000", f"{base_branch}...HEAD"],
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


def _get_harnesses() -> dict:
    """Return harness metadata. Separate function so tests can import it.

    Built-in harnesses are listed first, then any lodged profiles (keyed by
    profile name) so an agent querying "what's available" sees both. A profile
    that shadows a built-in name is omitted (the built-in wins).
    """
    harnesses = {
        "claude-code": {
            "models": CLAUDE_MODEL_ALIASES,
            "default_model": "sonnet",
            "requires": "claude CLI",
            "note": "Aliases are shortcuts; any model string accepted by claude CLI also works",
        },
        "codex": {
            "models": CODEX_MODEL_ALIASES,
            # gpt-5.6-sol is the default: it works on codex 0.144.4 (verified
            # 2026-07-17). gpt-5.3-codex still 400s on ChatGPT-account auth
            # (see the CODEX_MODEL_ALIASES access note). Keep the spindle
            # service's PATH on a node whose global codex is current — an old
            # node's stale codex (e.g. 0.125.0) 400s on 5.6.
            "default_model": "gpt-5.6-sol",
            "requires": "codex CLI",
            "note": "Aliases are shortcuts; any model string accepted by codex CLI also works",
        },
        "gemini": {
            "models": GEMINI_MODEL_ALIASES,
            "default_model": "gemini-2.5-pro",
            "requires": "gemini CLI",
        },
        "kimi": {
            "models": KIMI_MODEL_ALIASES,
            "default_model": KIMI_DEFAULT_MODEL,
            "requires": "kimi-cli",
        },
    }
    for name, prof in _discover_profiles().items():
        if name in harnesses:
            continue  # built-in wins on collision
        harnesses[name] = {
            "type": "profile",
            "base_harness": prof.get("harness") or "claude-code",
            "default_model": prof.get("model"),
            "description": prof.get("description", ""),
            "source": prof.get("_source"),
        }
    return harnesses


@mcp.tool()
async def spin_harnesses() -> str:
    """
    List available harnesses and their supported models.

    Returns JSON with each harness, its model aliases, default model, and
    required CLI tool. Use this to discover what's available for spin().

    Example:
        harnesses = spin_harnesses()  # See all harnesses and models
    """
    return json.dumps(_get_harnesses(), indent=2)


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

    base_branch = _shard_base_branch(spool)
    status_info = {
        "spool_id": spool_id,
        "shard": shard_info,
        "exists": True,
        "spool_status": spool.get("status"),
        "base_branch": base_branch,
    }

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.returncode == 0:
            status_info["git_changes"] = result.stdout.strip().split("\n") if result.stdout.strip() else []

        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_branch}..HEAD"],
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
            if not other_wd:
                continue
            other_path = Path(other_wd).resolve()
            if other_path == wt_path or wt_path in other_path.parents:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent  # worktrees/name -> repo
    base_branch = _shard_base_branch(spool)

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.stdout.strip():
            return "Error: Shard has uncommitted changes. Commit or discard them first."

        # Merge branch into the main repo's current HEAD
        result = subprocess.run(
            ["git", "merge", branch_name, "--no-ff", "-m", f"Merge shard {spool_id}: {spool.get('prompt', '')[:50]}"],
            capture_output=True,
            text=True,
            cwd=str(main_repo),
            timeout=30,
        )
        if result.returncode != 0:
            return f"Error: Merge failed: {result.stderr}"

        # Cleanup shard
        _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch, spool_id=spool_id)

        # Update spool record
        spool["shard"]["merged"] = True
        spool["shard"]["merged_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)

        # Auto-close any tender folios for this worktree
        worktree_name = shard_info.get("shard_id") or Path(worktree_path).name
        tender_result = _close_tender_folios(worktree_name, str(main_repo))

        msg = f"Successfully merged shard {spool_id} to {base_branch}"
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
        return "Error: No worktree path in shard info"

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
            if not other_wd:
                continue
            other_path = Path(other_wd).resolve()
            if other_path == wt_path or wt_path in other_path.parents:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent

    # If spool is running, kill it first
    if spool.get("status") == "running":
        pid = spool.get("pid")
        if pid and not _terminate_process_group(pid, 0.5):
            return f"Error: Could not terminate process group for spool {spool_id}; shard preserved"

        spool["status"] = "error"
        spool["error"] = "Shard abandoned"
        spool["completed_at"] = datetime.now().isoformat()

    # Cleanup shard
    success = _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch, spool_id=spool_id)

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

    # Detect the main repo's default branch so the agent runs the right
    # diff commands on main-default repos as well as master-default ones.
    main_repo = Path(worktree_path).parent.parent
    base_branch = _detect_default_branch(str(main_repo)) if main_repo.exists() else "master"

    prompt = f"""## Worktree Triage

Assess the work in this worktree and create a tender.

**Worktree:** {worktree_path}
**Name:** {worktree_name}
**Base branch:** {base_branch}

### Steps:

1. Run `git log --oneline {base_branch}..HEAD` to see commits
2. Run `git diff --stat {base_branch}` to see scope of changes
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

    # Surface Claude Code background-task state. Wedged bg tasks (e.g. a pgrep
    # loop that matches the parent claude command line) keep a spool running
    # indefinitely even after the agent has emitted its final output.
    if spool.get("harness", "claude-code") == "claude-code" and spool.get("session_id"):
        bg_tasks = _get_cc_bg_tasks(spool["session_id"])
        if bg_tasks:
            spool["_bg_tasks"] = bg_tasks
            incomplete = [t for t in bg_tasks if t.get("status") != "completed"]
            spool["_bg_tasks_incomplete"] = len(incomplete)

    return json.dumps(spool, indent=2)


# ============================================================================
# CODEX CLI HARNESS
# ============================================================================


# Codex sandboxes with its own vendored bubblewrap + seccomp and does not need kernel
# Landlock, so a pre-5.13 kernel is no reason to skip --sandbox. But enforcement is not a
# property of the version string: the same codex version has been observed both enforcing
# --sandbox correctly and silently running the command UNSANDBOXED (fail open) — e.g. a
# sandbox_mode in ~/.codex/config.toml overriding the flag, or the vendored sandbox failing
# to spawn. A read-only spool that fails open runs with no write boundary while the record
# still says read-only: false provenance a reviewer cannot see.
#
# So enforcement is decided by BEHAVIOR, not version. _codex_sandbox_enforces() probes the
# resolved binary once and a restrictive-tier launch is REFUSED when the probe says the
# sandbox is not enforcing (see _codex_spin_sync / _codex_respin_sync). The version is still
# recorded per spool, for provenance only — it no longer gates anything.
_CODEX_VERSION_CACHE: Dict[str, Optional[str]] = {}

# Cached enforcement-probe results, keyed by binary and Codex config context so a reinstall,
# upgrade, CODEX_HOME/HOME change, or config edit re-probes. The probe is a local no-model exec.
_CODEX_SANDBOX_ENFORCES_CACHE: Dict[tuple, bool] = {}

# Printed by the sandboxed probe command to stdout. Its presence proves the command
# actually executed under the sandbox, which is what separates "write was blocked"
# (enforcing) from "the probe never ran" (inconclusive -> fail closed).
_CODEX_SANDBOX_PROBE_MARKER = "SPINDLE_CODEX_SANDBOX_PROBE_RAN"

# Tiers that promise a write boundary. danger-full-access asks for no sandbox, so there is
# nothing to enforce and nothing to refuse.
_CODEX_RESTRICTIVE_SANDBOX_MODES = frozenset({"read-only", "workspace-write"})


def _process_env(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The exact environment passed to a detached child process."""
    process_env = os.environ.copy()
    if overrides:
        process_env.update(overrides)
    return process_env


def _resolve_codex_binary(process_env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Absolute path that `codex` resolves to in the child environment, or None.

    Recorded per spool: spindle's PATH and a login shell's often resolve different
    codex installs, and they do not enforce --sandbox alike.
    """
    path = process_env.get("PATH") if process_env is not None else None
    return shutil.which("codex", path=path)


def _codex_cli_version(binary: Optional[str], process_env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Version of a codex binary (e.g. "0.125.0"), or None if it can't be determined.

    Cached per path: this shells out, and spin is on the hot path.
    """
    if not binary:
        return None
    if binary in _CODEX_VERSION_CACHE:
        return _CODEX_VERSION_CACHE[binary]

    version = None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env,
        )
        # e.g. "codex-cli 0.125.0"
        match = re.search(r"codex-cli\s+(\S+)", proc.stdout or "")
        if match:
            version = match.group(1)
    except Exception as exc:
        logger.warning("spindle: codex --version failed for %s: %s", binary, exc)
        version = None

    _CODEX_VERSION_CACHE[binary] = version
    return version


def _codex_auth_mode(binary: Optional[str], process_env: Optional[Dict[str, str]] = None) -> str:
    """Return ``chatgpt``, ``api``, or ``unknown`` in the launch environment.

    This intentionally runs for each fresh spin. Login state can change while
    the server remains alive, and CODEX_HOME/HOME may vary between callers.
    """
    # CODEX_API_KEY is a per-invocation override supported by ``codex exec``.
    # ``codex login status`` reports only the saved login, so consulting it
    # first would misclassify an API-key child as ChatGPT.
    if process_env and process_env.get("CODEX_API_KEY"):
        return "api"
    if not binary:
        return "unknown"
    mode = "unknown"
    try:
        proc = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env,
        )
        status = f"{proc.stdout}\n{proc.stderr}".lower()
        if proc.returncode == 0 and "logged in using chatgpt" in status:
            mode = "chatgpt"
        elif proc.returncode == 0 and ("api key" in status or "api-key" in status):
            mode = "api"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return mode


def _codex_sandbox_probe_key(codex_bin: str, process_env: Optional[Dict[str, str]] = None) -> tuple:
    """Cache key that changes with the binary or its effective Codex config."""
    try:
        mtime = os.path.getmtime(codex_bin)
    except OSError as exc:
        logger.debug("spindle: codex sandbox probe-key mtime unavailable for %s: %s", codex_bin, exc)
        mtime = None
    effective_env = process_env or os.environ
    home = effective_env.get("HOME", str(Path.home()))
    codex_home = effective_env.get("CODEX_HOME") or str(Path(home) / ".codex")
    config_path = Path(codex_home) / "config.toml"
    try:
        config_stat = config_path.stat()
        config_signature = (config_stat.st_mtime_ns, config_stat.st_size)
    except OSError:
        config_signature = None
    return (codex_bin, _codex_cli_version(codex_bin, process_env), mtime, codex_home, config_signature)


def _codex_sandbox_probe_argvs(codex_bin: str, shell_cmd: str) -> list:
    """Candidate no-model `codex sandbox` invocations, most-current CLI shape first.

    codex's no-model sandbox-exec has changed shape across versions: 0.144.x runs the command
    directly (`codex sandbox -- <cmd>`), while 0.125.x nests it under a platform subcommand
    (`codex sandbox linux -- <cmd>`). Each is tried until one actually executes the command
    (proven by the stdout marker); a shape that is wrong for this version simply never runs.

    read-only is forced via `-c sandbox_mode=read-only`, the SAME config override the real
    spin/respin launch now pairs with its `--sandbox` flag (see _codex_spin_sync): a
    command-line -c beats config.toml, so both probe and real launch resolve the tier the same
    config-proof way and "probe enforces" faithfully predicts "real spin enforces". The
    `codex sandbox` subcommand does not accept `--sandbox` (only `codex exec` does), so the tier
    is pinned via `-c` alone here — do not add `--sandbox`, it errors and the shape never runs.
    """
    base = [codex_bin, "-c", "sandbox_mode=read-only", "sandbox"]
    return [
        base + ["--", "/bin/sh", "-c", shell_cmd],  # codex >= 0.144.x
        base + ["linux", "--", "/bin/sh", "-c", shell_cmd],  # codex ~0.125.x
    ]


def _codex_sandbox_probe(codex_bin: str, process_env: Optional[Dict[str, str]] = None) -> bool:
    """One uncached run of the enforcement probe. See _codex_sandbox_enforces.

    True only on a definite "the sandbox blocked the write" reading. Any other outcome —
    the write succeeded (fail open), no known CLI shape executed the command, an error or
    timeout — returns False, so uncertainty fails closed.
    """
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="spindle-codex-probe-")
        target = os.path.join(probe_dir, "enforce_probe.txt")
        # The marker is emitted only after the relative write was attempted. Its presence
        # proves that attempt completed under the sandbox; any target existence then proves
        # the read-only boundary failed. This runs with no model turn.
        shell_cmd = (
            f"printf BROKEN > enforce_probe.txt; write_rc=$?; echo {_CODEX_SANDBOX_PROBE_MARKER}; exit $write_rc"
        )
        for argv in _codex_sandbox_probe_argvs(codex_bin, shell_cmd):
            Path(target).unlink(missing_ok=True)  # no stale file from a prior shape
            try:
                proc = subprocess.run(
                    argv,
                    cwd=probe_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=process_env,
                )
            except Exception as exc:
                logger.warning("spindle: codex sandbox probe (read-only) argv failed on %s: %s", codex_bin, exc)
                continue
            if _CODEX_SANDBOX_PROBE_MARKER not in (proc.stdout or ""):
                continue  # wrong CLI shape for this version — the command never ran
            try:
                os.stat(target)
                wrote = True
            except FileNotFoundError:
                # Expected success case: read-only prevented creation entirely.
                wrote = False
            except OSError as exc:
                logger.warning(
                    "spindle: codex sandbox probe (read-only) could not stat target on %s: %s",
                    codex_bin,
                    exc,
                )
                return False
            # The command demonstrably ran: blocked write -> enforcing, landed write -> fail
            # open. This shape is authoritative; do not fall through to another.
            return not wrote
        # No known CLI shape executed the probe — inconclusive.
        logger.warning(
            "spindle: codex sandbox probe (read-only) ran no known CLI shape on %s; failing closed",
            codex_bin,
        )
        return False
    except Exception as exc:
        logger.warning(
            "spindle: codex sandbox probe (read-only) errored on %s: %s; failing closed",
            codex_bin,
            exc,
        )
        return False
    finally:
        if probe_dir:
            shutil.rmtree(probe_dir, ignore_errors=True)


def _codex_sandbox_enforces(codex_bin: Optional[str], process_env: Optional[Dict[str, str]] = None) -> bool:
    """Whether `codex_bin` actually enforces its sandbox right now.

    Behavioral, not version-based: runs codex's no-model `codex sandbox` subcommand under
    read-only, attempts a write inside a scratch cwd, and reports whether the write was
    BLOCKED. Cached per binary/config context for the process lifetime, so identical launch
    contexts reuse the result. It is never a model call.

    Fails closed: a missing binary, an inconclusive probe, or any error returns False.
    """
    if not codex_bin:
        return False

    key = _codex_sandbox_probe_key(codex_bin, process_env)
    cached = _CODEX_SANDBOX_ENFORCES_CACHE.get(key)
    if cached is not None:
        return cached

    result = _codex_sandbox_probe(codex_bin, process_env)
    _CODEX_SANDBOX_ENFORCES_CACHE[key] = result
    return result


def _codex_sandbox_refusal(
    sandbox: Optional[str],
    permission: Optional[str],
    codex_bin: Optional[str],
    codex_version: Optional[str],
    process_env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Refusal message if a restrictive-tier launch must be blocked, else None.

    A restrictive tier (read-only / workspace-write) promises a write boundary. If the
    resolved codex binary does not actually enforce its sandbox, running the spool anyway
    would silently drop that boundary while the record still claims it — so refuse loudly.
    danger-full-access asks for no sandbox, so it is never refused.
    """
    if sandbox not in _CODEX_RESTRICTIVE_SANDBOX_MODES:
        return None
    if _codex_sandbox_enforces(codex_bin, process_env):
        return None

    tier = permission or sandbox
    return (
        f"REFUSED: codex sandbox is not enforcing on {codex_bin or 'codex (not found on PATH)'} "
        f"({codex_version or 'unknown version'}); refusing to run a {tier} spool (sandbox "
        f"{sandbox}) unsandboxed. Fix the codex install or use permission=full to run without "
        f"a sandbox."
    )


def _persist_codex_sandbox_refusal(
    spool_id: str,
    message: str,
    *,
    sandbox: Optional[str],
    permission: Optional[str],
    codex_bin: Optional[str],
    codex_version: Optional[str],
    session_id: Optional[str] = None,
) -> str:
    """Record a refused (would-be-unsandboxed) launch as an error spool and return the error.

    The refusal is visible both ways the brief requires: the returned string, and a
    persisted spool with status "error" plus a `sandbox_error` field so unspool/spool_info
    surface it. status "error" does not count against concurrency, so no slot leaks.
    """
    now = datetime.now().isoformat()
    spool = {
        "id": spool_id,
        "status": "error",
        "result": None,
        "session_id": session_id,
        "sandbox": sandbox,
        "permission": permission,
        "codex_bin": codex_bin,
        "codex_version": codex_version,
        "sandbox_error": message,
        "error": message,
        "harness": "codex",
        "tags": ["codex"],
        "pid": None,
        "created_at": now,
        "completed_at": now,
    }
    _write_spool(spool_id, spool)
    return f"Error: {message} (spool {spool_id})"


def _codex_bwrap_wrap(
    codex_cmd: list,
    shard_info: dict,
    cwd: str,
    research_target_info: Optional[Dict[str, str]] = None,
    process_env: Optional[Dict[str, str]] = None,
) -> list:
    """Wrap codex_cmd in bwrap for shard isolation.

    Returns the (possibly bwrap-wrapped) command. If bwrap is not available,
    logs a warning and returns codex_cmd unchanged.
    """
    effective_env = process_env or os.environ
    bwrap_bin = shutil.which("bwrap", path=effective_env.get("PATH"))
    if not bwrap_bin:
        print(
            "[Spindle] WARNING: bwrap not available — codex shard isolation is "
            "advisory only (prompt-enforced, not OS-enforced). Install bwrap for enforcement."
        )
        return codex_cmd

    home = effective_env.get("HOME", str(Path.home()))
    worktree_root = shard_info["worktree_path"]
    cmd = [
        bwrap_bin,
        "--ro-bind",
        "/",
        "/",
    ]
    if research_target_info and research_target_info["type"] in {"file", "dir"}:
        bind_path = _research_writable_path(research_target_info)
        cmd.extend(["--bind", bind_path, bind_path])
    else:
        cmd.extend(["--bind", worktree_root, worktree_root])
    cmd.extend(
        [
            "--bind",
            "/tmp",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            cwd,
        ]
    )
    # Make git writable for commits inside the worktree
    git_file = Path(worktree_root) / ".git"
    if git_file.exists() and git_file.is_file():
        git_content = git_file.read_text().strip()
        if git_content.startswith("gitdir:"):
            git_worktree_dir = git_content.split("gitdir:")[1].strip()
            if Path(git_worktree_dir).exists():
                cmd.extend(["--bind", git_worktree_dir, git_worktree_dir])
                main_git = Path(git_worktree_dir).parent.parent
                if main_git.exists() and main_git.name == ".git":
                    objects_dir = main_git / "objects"
                    if objects_dir.exists():
                        cmd.extend(["--bind", str(objects_dir), str(objects_dir)])
                    refs_heads = main_git / "refs" / "heads"
                    if refs_heads.exists():
                        cmd.extend(["--bind", str(refs_heads), str(refs_heads)])
                    logs_refs_heads = main_git / "logs" / "refs" / "heads"
                    if logs_refs_heads.exists():
                        cmd.extend(["--bind", str(logs_refs_heads), str(logs_refs_heads)])
    config_paths = [
        Path(home) / config_item
        for config_item in [
            ".claude",
            ".claude.json",
            ".anthropic",
            ".codex",
            ".gemini",
            ".spindle",
            ".config",
            ".cache",
        ]
    ]
    codex_home = effective_env.get("CODEX_HOME")
    if codex_home:
        config_paths.append(Path(codex_home))
    seen_paths = set()
    for config_path in config_paths:
        path = str(config_path)
        if path not in seen_paths and config_path.exists():
            cmd.extend(["--bind", path, path])
            seen_paths.add(path)
    cmd.extend(codex_cmd)
    return cmd


def _codex_spin_sync(
    prompt: str,
    working_dir: Optional[str],
    model: Optional[str],
    sandbox: Optional[str],
    timeout: Optional[int],
    tags: Optional[str],
    env: Optional[Dict[str, str]],
    shard: bool = False,
    base_branch: Optional[str] = None,
    skeinless: bool = False,
    research_target: Optional[str] = None,
    require_research_target: bool = False,
    permission: Optional[str] = None,
) -> str:
    """Synchronous implementation of codex_spin - runs Codex CLI in background.

    `sandbox` is the codex tier to enforce; `permission` is the tier it was derived from and
    is recorded so a respin of this session can re-derive the same sandbox.
    """
    # Require working_dir
    if not working_dir:
        return "Error: working_dir required. Pass the project directory."

    try:
        research_target_info = (
            _validate_research_target(research_target, working_dir)
            if (research_target or require_research_target)
            else None
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Fail closed: for a restrictive tier, refuse to launch when the resolved codex binary
    # does not actually enforce its sandbox. Checked here — before any slot is reserved or
    # shard created — so a refusal leaves nothing to clean up. The probe is cached for an
    # unchanged binary/config context, so repeated callers in that context reuse it.
    effective_sandbox = sandbox or "workspace-write"
    process_env = _process_env(env)
    codex_bin = _resolve_codex_binary(process_env)
    codex_version = _codex_cli_version(codex_bin, process_env)
    codex_auth_mode = _codex_auth_mode(codex_bin, process_env)

    # Short aliases are installation-independent. The official umbrella model
    # `gpt-5.6` needs one compatibility exception: this box's ChatGPT-account
    # route rejects it while serving the concrete Sol tier. Preserve an explicit
    # umbrella request for API-key and unknown auth rather than silently changing
    # semantics on installations where the official model works.
    resolved_model = CODEX_MODEL_ALIASES.get(model, model) if model else "gpt-5.6-sol"
    if model == "gpt-5.6" and codex_auth_mode == "chatgpt":
        resolved_model = "gpt-5.6-sol"

    refusal = _codex_sandbox_refusal(effective_sandbox, permission, codex_bin, codex_version, process_env)
    if refusal:
        return _persist_codex_sandbox_refusal(
            "codex-" + str(uuid.uuid4())[:8],
            refusal,
            sandbox=effective_sandbox,
            permission=permission,
            codex_bin=codex_bin,
            codex_version=codex_version,
        )

    # Generate spool ID
    spool_id = "codex-" + str(uuid.uuid4())[:8]

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    cwd = working_dir
    base_branch = base_branch or _detect_default_branch(working_dir)

    # Handle shard creation
    shard_info = None
    shard_error = None
    shard_newly_created = False
    if shard:
        # Reuse the worktree if working_dir already points inside an existing shard.
        # When reusing, keep the agent's cwd at the requested working_dir (which may
        # be a subdirectory of the worktree). shard_info["worktree_path"] holds the
        # actual worktree root for merge/cleanup paths.
        shard_info = _detect_existing_shard(cwd)
        if shard_info is None:
            shard_info, shard_error = _spawn_shard(spool_id, cwd, base_branch=base_branch)
            shard_newly_created = shard_info is not None
            if shard_info:
                cwd = shard_info["worktree_path"]
        if shard_info is None:
            # Clean up reserved slot
            spool_path = SPINDLE_DIR / f"{spool_id}.json"
            spool_path.unlink(missing_ok=True)
            if shard_error:
                return f"Error: Failed to create SHARD worktree — {shard_error}"
            return "Error: Failed to create SHARD worktree. Check git repo status."

    # Inject shard instructions into prompt
    effective_prompt = prompt
    if research_target_info:
        effective_prompt = _research_target_preamble(research_target_info) + prompt

    omit_shard_commit_preamble = _research_omits_shard_commit_preamble(research_target_info)

    if shard_info and not omit_shard_commit_preamble:
        if _has_skein(working_dir) and not skeinless:
            worktree_name = shard_info.get("shard_id", spool_id)
            skein_preamble = f"""You are working in an isolated SHARD worktree.

Before starting work, orient yourself with SKEIN:
1. Run: skein ignite --message "{prompt[:100]}..."
2. Then: skein ready

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"
2. Tender: skein shard tender {worktree_name} --summary "What you did" --confidence N
   (confidence 1-10: 10=safe/isolated, 5=needs review, 1=risky)
3. Retire: skein torch && skein complete

Your task:
"""
            effective_prompt = skein_preamble + effective_prompt
        else:
            shard_preamble = """You are working in an isolated SHARD worktree.

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"

Your task:
"""
            effective_prompt = shard_preamble + effective_prompt

    # Build codex exec command.
    #
    # --sandbox is always passed and always decides the tier, and it is paired with a matching
    # `-c sandbox_mode=<tier>` so ~/.codex/config.toml's sandbox_mode can never override it: a
    # command-line -c beats config.toml (verified on codex 0.144.5: config.toml sandbox_mode is
    # ignored once -c sandbox_mode is on the CLI). The two always agree, so this is a no-op on
    # versions where the flag already wins and closes the hole on any version where config.toml
    # could widen a restrictive spool. A spool's isolation comes from the permission it was spun
    # with, not from whatever the local config happens to say. This is the same -c sandbox_mode
    # pin the enforcement probe validates (see _codex_sandbox_probe_argvs), so "probe enforces"
    # faithfully predicts "this launch enforces". effective_sandbox / codex_bin / codex_version
    # were resolved above, where the fail-closed enforcement check runs.

    # --json for structured output.
    #
    # --full-auto is deliberately NOT passed. It is an alias that sets a tier of its own, and
    # it silently WINS over --sandbox regardless of flag order: `codex exec --full-auto
    # --sandbox read-only` reports "sandbox: workspace-write [workdir, /tmp, $TMPDIR]" and
    # lets a spool write outside its workspace, while the command line still reads
    # "--sandbox read-only". It is not needed for non-interactive use either — `codex exec`
    # already reports "approval: never" on its own. Verified against codex 0.125.0,
    # 2026-07-16. Re-adding it silently disables every tier below workspace-write.
    codex_cmd = [codex_bin or "codex", "exec", "--json"]

    if resolved_model:
        codex_cmd.extend(["--model", resolved_model])

    codex_cmd.extend(["--sandbox", effective_sandbox, "-c", f"sandbox_mode={effective_sandbox}"])

    if shard_info:
        codex_cmd.extend(["--cd", shard_info["worktree_path"]])

    if research_target_info and research_target_info["type"] in {"file", "dir"}:
        codex_cmd.extend(["--add-dir", _research_writable_path(research_target_info)])

    # For shards, grant write access to main repo's .git for commits
    if shard_info:
        # Resolve .git via the worktree root, since cwd may be a subdirectory.
        if not (research_target_info and research_target_info["type"] in {"file", "dir"}):
            git_file = Path(shard_info["worktree_path"]) / ".git"
            if git_file.exists() and git_file.is_file():
                git_content = git_file.read_text().strip()
                if git_content.startswith("gitdir:"):
                    git_worktree_dir = git_content.split("gitdir:")[1].strip()
                    main_git = Path(git_worktree_dir).parent.parent
                    if main_git.exists() and main_git.name == ".git":
                        codex_cmd.extend(["--add-dir", str(main_git)])
                        # Also grant write access to the worktree root so a
                        # subdirectory cwd doesn't lock the agent out of sibling files.
                        codex_cmd.extend(["--add-dir", shard_info["worktree_path"]])

    # Prompt goes last
    codex_cmd.append(effective_prompt)

    # Wrap in bwrap sandbox for shards - worktree writable, rest read-only.
    # Codex's own bubblewrap sandbox nests inside this one, so both layers run as
    # defense-in-depth.
    if shard_info:
        cmd = _codex_bwrap_wrap(
            codex_cmd,
            shard_info,
            cwd,
            research_target_info=research_target_info,
            process_env=process_env,
        )
    else:
        cmd = codex_cmd

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    tag_list.append("codex")  # Auto-tag as codex spool

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": None,  # Will be extracted from output
        "working_dir": cwd,
        "model": resolved_model or "default",
        # The tier actually passed to codex, not the one that was merely asked for.
        "sandbox": effective_sandbox,
        # The requested tier, kept so a respin can re-derive the same sandbox.
        "permission": permission,
        # Which codex actually ran: enforcement varies by version, and PATH decides.
        "codex_bin": codex_bin,
        "codex_version": codex_version,
        "codex_auth_mode": codex_auth_mode,
        "research_target": research_target,
        "tags": tag_list,
        "timeout": timeout,
        "env": env,
        "shard": shard_info,
        # Failed agents keep newly created shards for explicit inspection and
        # cleanup; pre-existing shards are never relabeled as Spindle-created.
        "shard_created_by_spool": shard_newly_created,
        "shard_source_dir": working_dir if shard_newly_created else None,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "codex",  # Mark as codex harness
    }

    _write_spool(spool_id, spool)

    # Spawn detached process
    try:
        pid = _spawn_detached(spool_id, cmd, cwd, process_env)
    except Exception as e:
        # Spawn failed - mark spool as error so the slot is freed
        spool["status"] = "error"
        spool["error"] = f"spawn failed: {e}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        _preserve_failed_spool_shard(spool)
        _write_spool(spool_id, spool)
        return f"Error: Failed to spawn process: {e}"

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread
    _start_spool_monitor(spool_id)

    return spool_id


def _codex_unspool_sync(spool_id: str) -> str:
    """Synchronous implementation of codex_unspool."""
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


def _codex_respin_sync(session_id: str, prompt: str) -> str:
    """Synchronous implementation of codex_respin - continue a Codex session."""
    # Generate spool ID
    spool_id = "codex-" + str(uuid.uuid4())[:8]

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    # Get working_dir, env, and shard info from original spool if possible
    original_spool = _find_spool_by_session(session_id)
    working_dir = original_spool.get("working_dir") if original_spool else os.getcwd()
    env = original_spool.get("env") if original_spool else None
    shard_info = original_spool.get("shard") if original_spool else None

    # Continue at the tier the session was spun with — a respin must not widen it.
    permission = original_spool.get("permission") if original_spool else None
    sandbox = _codex_respin_sandbox(original_spool)

    process_env = _process_env(env)
    codex_bin = _resolve_codex_binary(process_env)
    codex_version = _codex_cli_version(codex_bin, process_env)
    # Fail closed: a respin at a restrictive tier is refused when the binary does not enforce
    # its sandbox, exactly like a fresh spin. The reserved slot is reused for the error
    # record (status "error" frees it), so no slot leaks.
    refusal = _codex_sandbox_refusal(sandbox, permission, codex_bin, codex_version, process_env)
    if refusal:
        return _persist_codex_sandbox_refusal(
            spool_id,
            refusal,
            sandbox=sandbox,
            permission=permission,
            codex_bin=codex_bin,
            codex_version=codex_version,
            session_id=session_id,
        )

    # Re-grant the original run's writable research target; `resume` inherits neither the
    # sandbox tier nor its --add-dir grants, so without this a research respin runs
    # workspace-write with no way to write its output.
    research_target = original_spool.get("research_target") if original_spool else None
    try:
        research_target_info = _validate_research_target(research_target, working_dir) if research_target else None
    except ValueError:
        research_target_info = None

    # Build codex exec resume command.
    # --sandbox, --cd and --add-dir are `codex exec` flags that `codex exec resume` does not
    # accept, so they must all precede the `resume` subcommand. --sandbox is paired with a
    # matching `-c sandbox_mode=<tier>` so config.toml can't widen the tier on a respin either
    # (see the note in _codex_spin_sync).
    codex_cmd = [codex_bin or "codex", "exec", "--sandbox", sandbox, "-c", f"sandbox_mode={sandbox}"]
    if shard_info:
        codex_cmd.extend(["--cd", shard_info["worktree_path"]])

    if research_target_info and research_target_info["type"] in {"file", "dir"}:
        codex_cmd.extend(["--add-dir", _research_writable_path(research_target_info)])

    # For shards, grant write access to main repo's .git for commits. Resolve .git via the
    # worktree root (shard_info), not working_dir, since working_dir may be a subdirectory
    # inside the worktree.
    if shard_info and not (research_target_info and research_target_info["type"] in {"file", "dir"}):
        git_file = Path(shard_info["worktree_path"]) / ".git"
        if git_file.exists() and git_file.is_file():
            git_content = git_file.read_text().strip()
            if git_content.startswith("gitdir:"):
                git_worktree_dir = git_content.split("gitdir:")[1].strip()
                main_git = Path(git_worktree_dir).parent.parent
                if main_git.exists() and main_git.name == ".git":
                    codex_cmd.extend(["--add-dir", str(main_git)])
                    # Also grant write access to the worktree root so a
                    # subdirectory cwd doesn't lock the agent out of sibling files.
                    codex_cmd.extend(["--add-dir", shard_info["worktree_path"]])

    codex_cmd.extend(["resume", session_id, "--json"])

    # --full-auto is deliberately NOT passed here either: it overrides --sandbox with its own
    # workspace-write tier. See the note in _codex_spin_sync.

    # The prompt is passed as additional argument to resume
    codex_cmd.append(prompt)

    # Wrap in bwrap sandbox for shards - worktree writable, rest read-only.
    # Codex's own bubblewrap sandbox nests inside this one, so both layers run as
    # defense-in-depth. A research+shard respin binds its output dir writable (not
    # the worktree root) — mirror _codex_spin_sync so the --add-dir grant above is
    # actually bindable at the outer bwrap layer.
    if shard_info:
        cmd = _codex_bwrap_wrap(
            codex_cmd,
            shard_info,
            working_dir,
            research_target_info=research_target_info,
            process_env=process_env,
        )
    else:
        cmd = codex_cmd

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": f"Continue {session_id}: {prompt}",
        "result": None,
        "session_id": session_id,
        "working_dir": working_dir,
        # Carried forward, not just recorded: a later respin of this same session_id may
        # resolve to this record instead of the original spin (both share the session_id,
        # and _list_spools globs in arbitrary order), so the tier has to survive here too.
        "sandbox": sandbox,
        "permission": permission,
        "research_target": research_target,
        "codex_bin": codex_bin,
        "codex_version": codex_version,
        "tags": ["codex", "respin"],
        "env": env,
        "shard": shard_info,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "codex",
    }

    _write_spool(spool_id, spool)

    # Spawn detached process
    try:
        pid = _spawn_detached(spool_id, cmd, working_dir, process_env)
    except Exception as e:
        spool["status"] = "error"
        spool["error"] = f"spawn failed: {e}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        return f"Error: Failed to spawn process: {e}"

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor
    _start_spool_monitor(spool_id)

    return spool_id


# Short aliases for common Claude models. Anything not here passes through.
# The plain "haiku"/"sonnet"/"opus" aliases are also accepted by the claude CLI
# directly; they're listed here so spin_harnesses() can advertise them.
# Source of truth: anthropics/skills/skills/claude-api/shared/models.md
CLAUDE_MODEL_ALIASES = {
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku-4.5": "claude-haiku-4-5",
    "sonnet-4.6": "claude-sonnet-4-6",
    "opus-4.6": "claude-opus-4-6",
    "opus-4.7": "claude-opus-4-7",
    "opus-4.8": "claude-opus-4-8",
    # Fable access sunsets 2026-07-12; after that the claude CLI will reject
    # the model (the alias itself stays harmless — unknowns pass through).
    "fable": "claude-fable-5",
    "fable-5": "claude-fable-5",
    # Opus 4.7 already has 1M context at standard pricing; the [1m] suffix
    # is preserved for explicit-context callers and back-compat.
    "opus-4.7-1m": "claude-opus-4-7[1m]",
    "opus-1m": "claude-opus-4-7[1m]",
}


# Short aliases for common Codex/OpenAI models. Anything not here passes through.
# Source of truth: the codex CLI's own resolver, which reports the current latest
# model (returns gpt-5.6-sol as of 2026-07-09):
#   node ~/.codex/skills/.system/openai-docs/scripts/resolve-latest-model-info.js
#
# ACCESS REALITY (ChatGPT-account auth on this box):
#   * gpt-5.6-sol/terra/luna: LIVE and the default (verified 2026-07-17 on codex
#     0.144.4). Earlier (2026-07-09) codex 0.144.0 400'd with "requires a newer
#     version of Codex", so 5.6 was staged; 0.144.4 speaks it. Gotcha: the 400
#     is per codex-BINARY version, and the spindle service resolves `codex` off
#     its systemd-unit PATH — if that PATH leads with an old node whose global
#     codex is stale (e.g. 0.125.0), every spin 400s on 5.6 while an interactive
#     shell on a newer node works. Keep the unit PATH on a current-codex node.
#   * gpt-5.3-codex and the other API-only *-codex ids: 400 "not supported when
#     using Codex with a ChatGPT account" — unusable on this auth.
#   * gpt-5.5: works; prior default, kept as an alias.
CODEX_MODEL_ALIASES = {
    # GPT-5.6 series (Sol/Terra/Luna) — LIVE on codex 0.144.4 (see access note
    # above). Sol/Terra/Luna are durable capability tiers (flagship /
    # balanced mini-like / fast nano-like); no separate "-codex" variant.
    "5.6": "gpt-5.6-sol",
    "5.6-sol": "gpt-5.6-sol",
    "sol": "gpt-5.6-sol",
    "5.6-terra": "gpt-5.6-terra",
    "terra": "gpt-5.6-terra",
    "5.6-luna": "gpt-5.6-luna",
    "luna": "gpt-5.6-luna",
    # GPT-5.5 series — prior default; "codex" now tracks the 5.6 flagship
    "5.5": "gpt-5.5",
    "5.5-pro": "gpt-5.5-pro",
    "codex": "gpt-5.6-sol",
    # GPT-5.3 / 5.1 codex variants — API-only; 400 on ChatGPT-account auth
    "5.3-codex": "gpt-5.3-codex",
    "5.1-codex-max": "gpt-5.1-codex-max",
    "5.1-codex-mini": "gpt-5.1-codex-mini",
    "codex-mini": "gpt-5.1-codex-mini",
    # GPT-5.4 series
    "5.4": "gpt-5.4",
    "5.4-mini": "gpt-5.4-mini",
    "5.4-nano": "gpt-5.4-nano",
    # GPT-5 / 5.1 base
    "5": "gpt-5",
    "5-mini": "gpt-5-mini",
    "5.1": "gpt-5.1",
    # GPT-4.1 series — cheap no-reasoning text
    "4.1": "gpt-4.1",
    "4.1-mini": "gpt-4.1-mini",
    "4.1-nano": "gpt-4.1-nano",
}


# ============================================================================
# Gemini Harness Implementation
# ============================================================================
# Uses Google's Gemini CLI in headless mode (-p flag), matching the pattern
# used by Claude Code and Codex harnesses.

# Short aliases for common models. Anything not here passes through to the CLI.
# Source of truth: Generative Language API — verify with:
#   curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY&pageSize=200"
# All values verified 2026-06-20: every alias below resolves to a live generateContent model.
GEMINI_MODEL_ALIASES = {
    # 2.5 family — current CLI default
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
    "flash-lite": "gemini-2.5-flash-lite",
    # 3.x family — pro variants preview-only; flash-lite and 3.5-flash GA; all live as of 2026-06-20
    "3-pro": "gemini-3-pro-preview",
    "3-flash": "gemini-3-flash-preview",
    "3.1-pro": "gemini-3.1-pro-preview",
    "3.1-flash-lite": "gemini-3.1-flash-lite",
    "3.1-customtools": "gemini-3.1-pro-preview-customtools",
    "3.5-flash": "gemini-3.5-flash",
    # Gemma 4 open-weight variants
    "gemma-4": "gemma-4-31b-it",
    "gemma-4-mini": "gemma-4-26b-a4b-it",
}

# Source of truth: MoonshotAI/kimi-cli; aliases use the kimi-cli managed-provider
# key format ("moonshot-ai/<model>"). Every value here MUST be registered under
# [models.…] in the local kimi config (see ~/.kimi/config.toml) — kimi-cli silently
# ignores an unknown -m model, falls back to an empty LLM, and reports "LLM not set"
# (see _kimi_registered_models below, which guards against this).
#
# The moonshot-ai managed provider no longer ships standalone "kimi-k2-thinking" /
# "kimi-k2-turbo-preview" models; thinking is now a capability of kimi-k2.5/kimi-k2.6
# toggled with kimi-cli's --thinking flag. The "thinking" alias therefore resolves to
# kimi-k2.6 and runs it in thinking mode (see KIMI_THINKING_ALIASES). Run `kimi /setup`
# if a newer model isn't yet in the local config.
#
# kimi-k2.7-code (released 2026-06-12) is a coding-focused upgrade on the k2.6
# foundation — ~30% fewer reasoning tokens on long-horizon coding tasks. It is a
# thinking-only model: the moonshot endpoint rejects any request that disables
# thinking ("only type=enabled is allowed for this model"), so it MUST always run
# with --thinking regardless of how it was selected (see KIMI_THINKING_REQUIRED).
# k2.6 stays the general default; "latest" tracks the newest *general* model, while
# the explicit k2.7-code / code aliases opt into the coding-specialized model.
KIMI_DEFAULT_MODEL = "moonshot-ai/kimi-k2.6"
KIMI_MODEL_ALIASES = {
    "thinking": "moonshot-ai/kimi-k2.6",
    "latest": "moonshot-ai/kimi-k2.6",
    "k2.6": "moonshot-ai/kimi-k2.6",
    "k2.5": "moonshot-ai/kimi-k2.5",
    # k2.7-code — coding-specialized, thinking-only (see KIMI_THINKING_REQUIRED)
    "k2.7-code": "moonshot-ai/kimi-k2.7-code",
    "k2.7": "moonshot-ai/kimi-k2.7-code",
    "code": "moonshot-ai/kimi-k2.7-code",
    "k2.7-code-highspeed": "moonshot-ai/kimi-k2.7-code-highspeed",
    "highspeed": "moonshot-ai/kimi-k2.7-code-highspeed",
}
# Aliases whose resolved model should run with kimi-cli thinking mode enabled.
KIMI_THINKING_ALIASES = {"thinking"}
# Resolved models that are thinking-ONLY: the endpoint rejects type=disabled, so we
# force --thinking no matter how the model was selected (alias or full model string).
KIMI_THINKING_REQUIRED = {
    "moonshot-ai/kimi-k2.7-code",
    "moonshot-ai/kimi-k2.7-code-highspeed",
}


def _kimi_config_path() -> Path:
    """Path to the kimi-cli config, mirroring kimi-cli's own resolution.

    kimi-cli reads ~/.kimi/config.toml unless overridden by --config-file; we honor
    the same KIMI_CONFIG_FILE env override that kimi-cli respects for headless runs.
    """
    override = os.environ.get("KIMI_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kimi" / "config.toml"


def _kimi_registered_models() -> Optional[set]:
    """Model keys registered under [models.*] in the kimi config.

    Returns None (skip validation) when the config can't be read or parsed — e.g.
    Python <3.11 without tomllib, a missing/malformed config, or a config with no
    [models] table at all. Returning None means "don't block". Only an explicitly
    present-but-empty [models] table yields the empty set ("config readable, models
    table present, but no models registered").
    """
    try:
        import tomllib
    except ImportError:
        return None
    try:
        with open(_kimi_config_path(), "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return None
    models = data.get("models")
    if not isinstance(models, dict):
        return None
    return set(models.keys())


def _kimi_validate_model(resolved_model: Optional[str]) -> Optional[str]:
    """Return an error string if resolved_model isn't registered in the kimi config.

    Guards against kimi-cli's silent fallback: an unknown -m model produces an empty
    LLM and the opaque "LLM not set" message with no agent output. We surface a clear,
    actionable error instead. Best-effort — returns None (allow) when the config can't
    be inspected (see _kimi_registered_models).
    """
    if not resolved_model:
        return None
    registered = _kimi_registered_models()
    if registered is None or resolved_model in registered:
        return None
    available = ", ".join(sorted(registered)) if registered else "(none)"
    return (
        f"Error: Kimi model '{resolved_model}' is not registered in the kimi config "
        f"({_kimi_config_path()}). kimi-cli would silently fall back to an empty LLM "
        f"and report 'LLM not set'. Available models: {available}. "
        f"Run `kimi /setup` to refresh managed models, or add the model to the config."
    )


def _gemini_spin_sync(
    prompt: str,
    working_dir: Optional[str],
    model: Optional[str],
    system_prompt: Optional[str],
    timeout: Optional[int],
    tags: Optional[str],
    env: Optional[Dict[str, str]],
    permission: Optional[str] = None,
    shard: bool = False,
    base_branch: Optional[str] = None,
    skeinless: bool = False,
    research_target: Optional[str] = None,
    require_research_target: bool = False,
) -> str:
    """Synchronous implementation of gemini_spin - runs Gemini CLI in background."""
    # Require working_dir
    if not working_dir:
        return "Error: working_dir required. Pass the project directory."

    # Resolve to absolute path to avoid cwd-dependent resolution
    working_dir = str(Path(working_dir).resolve())
    base_branch = base_branch or _detect_default_branch(working_dir)

    try:
        research_target_info = (
            _validate_research_target(research_target, working_dir)
            if (research_target or require_research_target or permission in {"research", "research+shard"})
            else None
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Generate spool ID
    spool_id = "gemini-" + str(uuid.uuid4())[:8]

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    cwd = working_dir
    shard_info = None
    shard_error = None
    shard_newly_created = False
    if shard:
        shard_info = _detect_existing_shard(cwd)
        if shard_info is None:
            shard_info, shard_error = _spawn_shard(spool_id, cwd, base_branch=base_branch)
            shard_newly_created = shard_info is not None
            if shard_info:
                cwd = shard_info["worktree_path"]
        if shard_info is None:
            spool_path = SPINDLE_DIR / f"{spool_id}.json"
            spool_path.unlink(missing_ok=True)
            if shard_error:
                return f"Error: Failed to create SHARD worktree — {shard_error}"
            return "Error: Failed to create SHARD worktree. Check git repo status."

    # Resolve model aliases (default to pro if no model specified)
    resolved_model = GEMINI_MODEL_ALIASES.get(model, model) if model else "gemini-2.5-pro"

    effective_prompt = prompt
    if research_target_info:
        effective_prompt = _research_target_preamble(research_target_info) + prompt

    if shard_info and not _research_omits_shard_commit_preamble(research_target_info):
        if _has_skein(working_dir) and not skeinless:
            worktree_name = shard_info.get("shard_id", spool_id)
            skein_preamble = f"""You are working in an isolated SHARD worktree.

Before starting work, orient yourself with SKEIN:
1. Run: skein ignite --message "{prompt[:100]}..."
2. Then: skein ready

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"
2. Tender: skein shard tender {worktree_name} --summary "What you did" --confidence N
   (confidence 1-10: 10=safe/isolated, 5=needs review, 1=risky)
3. Retire: skein torch && skein complete

Your task:
"""
            effective_prompt = skein_preamble + effective_prompt
        else:
            shard_preamble = """You are working in an isolated SHARD worktree.

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"

Your task:
"""
            effective_prompt = shard_preamble + effective_prompt

    # Build gemini command: headless mode with sandbox and JSON output.
    #
    # Web-search reliability (verified empirically 2026-06-28): in headless -p
    # mode the google_web_search tool DOES fire and is auto-accepted — the stats
    # block in -o json output shows tools.byName.google_web_search with
    # decisions.accept>0 and totalFail=0, so there is no approval gate to defeat
    # (-y / --approval-mode are irrelevant here). -s does not block it either:
    # searches return correct live data through the sandbox (confirmed against
    # current npm versions and an obscure Royal Road serial premise).
    #
    # The real failure mode is INTERMITTENT EMPTY GROUNDING: the Gemini API's
    # Search grounding sometimes returns no content (a documented server-side
    # regression — empty groundingMetadata.webSearchQueries), and on an empty
    # result gemini CONFABULATES instead of reporting failure. The -o json output
    # exposes only `response` + `stats`, not groundingMetadata, so spindle cannot
    # tell a grounded answer from a fabricated one. Treat gemini as UNRELIABLE for
    # correctness-critical web research — route that to Claude/Codex and use gemini
    # for offline synthesis or cheap, separately-verified pre-research.
    #
    # Gemini has no allowedTools-equivalent; research restrictions are prompt-level,
    # plus bwrap's filesystem boundary when running in shard mode.
    gemini_cmd = ["gemini", "-p", effective_prompt, "-s", "-o", "json"]

    if resolved_model:
        gemini_cmd.extend(["-m", resolved_model])

    if system_prompt:
        # Gemini CLI doesn't have a separate system prompt flag,
        # so prepend it to the prompt
        combined_prompt = f"System instructions: {system_prompt}\n\n{effective_prompt}"
        gemini_cmd[2] = combined_prompt

    if shard_info:
        gemini_cmd = _codex_bwrap_wrap(
            gemini_cmd,
            shard_info,
            cwd,
            research_target_info=research_target_info,
            process_env=_process_env(env),
        )

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    tag_list.append("gemini")  # Auto-tag as gemini spool

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": None,
        "working_dir": working_dir,
        "model": resolved_model or "auto",
        "system_prompt": system_prompt,
        "tags": tag_list,
        "timeout": timeout,
        "env": env,
        "research_target": research_target,
        "shard": shard_info,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "gemini",
    }

    _write_spool(spool_id, spool)

    # Spawn detached process
    try:
        pid = _spawn_detached(spool_id, gemini_cmd, cwd, env)
    except Exception as e:
        # Spawn failed - mark spool as error so the slot is freed
        spool["status"] = "error"
        spool["error"] = f"spawn failed: {e}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        # Clean up shard worktree only if we created it; don't destroy pre-existing shards
        if shard_newly_created:
            _cleanup_shard(shard_info, working_dir)
        return f"Error: Failed to spawn process: {e}"

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread (reuse the standard monitor)
    _start_spool_monitor(spool_id)

    return spool_id


def _gemini_respin_sync(session_id: str, prompt: str, original_spool: dict) -> str:
    """Synchronous implementation of gemini respin - continue a Gemini session."""
    spool_id = "gemini-" + str(uuid.uuid4())[:8]

    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    # Build gemini resume command
    gemini_cmd = ["gemini", "--resume", session_id, "-p", prompt, "-y", "-o", "json"]

    # Use model from original spool if set
    model = original_spool.get("model")
    if model and model != "auto":
        gemini_cmd.extend(["-m", model])

    working_dir = original_spool.get("working_dir") or os.getcwd()
    env = original_spool.get("env")

    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": f"Continue {session_id}: {prompt}",
        "result": None,
        "session_id": session_id,
        "working_dir": working_dir,
        "tags": ["gemini", "respin"],
        "env": env,
        "model": model or "auto",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "gemini",
    }

    _write_spool(spool_id, spool)

    pid = _spawn_detached(spool_id, gemini_cmd, working_dir, env)

    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    _start_spool_monitor(spool_id)

    return spool_id


def _gemini_unspool_sync(spool_id: str) -> str:
    """Synchronous implementation of gemini_unspool."""
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


def _kimi_spin_sync(
    prompt: str,
    working_dir: Optional[str],
    model: Optional[str],
    system_prompt: Optional[str],
    timeout: Optional[int],
    tags: Optional[str],
    env: Optional[Dict[str, str]],
    permission: Optional[str] = None,
    shard: bool = False,
    base_branch: Optional[str] = None,
    skeinless: bool = False,
    research_target: Optional[str] = None,
    require_research_target: bool = False,
) -> str:
    """Synchronous implementation of kimi_spin - runs Kimi CLI in background."""
    # Require working_dir
    if not working_dir:
        return "Error: working_dir required. Pass the project directory."

    working_dir = str(Path(working_dir).resolve())
    base_branch = base_branch or _detect_default_branch(working_dir)

    try:
        research_target_info = (
            _validate_research_target(research_target, working_dir)
            if (research_target or require_research_target or permission in {"research", "research+shard"})
            else None
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Resolve model aliases (default to kimi-k2.6 if no model specified) and decide
    # whether to run kimi-cli in thinking mode. Validate the resolved model against the
    # kimi config BEFORE reserving a slot or creating a shard: an unregistered model makes
    # kimi-cli silently fall back to an empty LLM and emit only "LLM not set".
    resolved_model = KIMI_MODEL_ALIASES.get(model, model) if model else KIMI_DEFAULT_MODEL
    # Thinking is enabled when the caller picked a thinking alias, OR when the
    # resolved model is thinking-only (k2.7-code rejects requests with thinking
    # disabled, regardless of whether it was reached via alias or full model name).
    enable_thinking = (bool(model) and model in KIMI_THINKING_ALIASES) or (resolved_model in KIMI_THINKING_REQUIRED)
    model_error = _kimi_validate_model(resolved_model)
    if model_error:
        return model_error

    # Generate spool ID and session ID
    spool_id = "kimi-" + str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())  # Generate our own session ID

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    cwd = working_dir
    shard_info = None
    shard_error = None
    shard_newly_created = False
    if shard:
        shard_info = _detect_existing_shard(cwd)
        if shard_info is None:
            shard_info, shard_error = _spawn_shard(spool_id, cwd, base_branch=base_branch)
            shard_newly_created = shard_info is not None
            if shard_info:
                cwd = shard_info["worktree_path"]
        if shard_info is None:
            spool_path = SPINDLE_DIR / f"{spool_id}.json"
            spool_path.unlink(missing_ok=True)
            if shard_error:
                return f"Error: Failed to create SHARD worktree — {shard_error}"
            return "Error: Failed to create SHARD worktree. Check git repo status."

    effective_prompt = prompt
    if research_target_info:
        effective_prompt = _research_target_preamble(research_target_info) + prompt

    if shard_info and not _research_omits_shard_commit_preamble(research_target_info):
        if _has_skein(working_dir) and not skeinless:
            worktree_name = shard_info.get("shard_id", spool_id)
            skein_preamble = f"""You are working in an isolated SHARD worktree.

Before starting work, orient yourself with SKEIN:
1. Run: skein ignite --message "{prompt[:100]}..."
2. Then: skein ready

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"
2. Tender: skein shard tender {worktree_name} --summary "What you did" --confidence N
   (confidence 1-10: 10=safe/isolated, 5=needs review, 1=risky)
3. Retire: skein torch && skein complete

Your task:
"""
            effective_prompt = skein_preamble + effective_prompt
        else:
            shard_preamble = """You are working in an isolated SHARD worktree.

After completing work:
1. Commit: git add -A && git commit -m "Your commit message"

Your task:
"""
            effective_prompt = shard_preamble + effective_prompt

    # Build kimi command: headless mode with auto-approve, stream-json output, and explicit session ID
    # Kimi has no allowedTools-equivalent; research restrictions are prompt-level,
    # plus bwrap's filesystem boundary when running in shard mode.
    kimi_cmd = [
        "kimi-cli",
        "--session",
        session_id,
        "--print",
        "--yolo",
        "--output-format",
        "stream-json",
        "-p",
        effective_prompt,
    ]

    if resolved_model:
        kimi_cmd.extend(["-m", resolved_model])

    if enable_thinking:
        kimi_cmd.append("--thinking")

    if cwd:
        kimi_cmd.extend(["-w", cwd])

    if system_prompt:
        # Kimi CLI doesn't have a separate system prompt flag,
        # so prepend it to the prompt
        combined_prompt = f"System instructions: {system_prompt}\n\n{effective_prompt}"
        # Find and replace the prompt argument
        prompt_idx = kimi_cmd.index("-p") + 1
        kimi_cmd[prompt_idx] = combined_prompt

    if shard_info:
        kimi_cmd = _codex_bwrap_wrap(
            kimi_cmd,
            shard_info,
            cwd,
            research_target_info=research_target_info,
            process_env=_process_env(env),
        )

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    tag_list.append("kimi")  # Auto-tag as kimi spool

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": session_id,  # Store our generated session ID
        "working_dir": working_dir,
        "model": resolved_model or "auto",
        "thinking": enable_thinking,
        "system_prompt": system_prompt,
        "tags": tag_list,
        "timeout": timeout,
        "env": env,
        "research_target": research_target,
        "shard": shard_info,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "kimi",
    }

    # Write spool to disk
    _write_spool(spool_id, spool)

    # Spawn the process detached
    try:
        pid = _spawn_detached(spool_id, kimi_cmd, cwd, env)
    except Exception as e:
        # Spawn failed - mark spool as error so the slot is freed
        spool["status"] = "error"
        spool["error"] = f"spawn failed: {e}"
        spool["completed_at"] = datetime.now().isoformat()
        _write_spool(spool_id, spool)
        # Clean up shard worktree only if we created it; don't destroy pre-existing shards
        if shard_newly_created:
            _cleanup_shard(shard_info, working_dir)
        return f"Error: Failed to spawn process: {e}"

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread
    _start_spool_monitor(spool_id)

    return spool_id


def _kimi_respin_sync(
    session_id: str,
    prompt: str,
    original_spool: dict,
) -> str:
    """Synchronous implementation of kimi_respin - continues Kimi session."""
    working_dir = original_spool.get("working_dir")
    if not working_dir:
        return "Error: original spool missing working_dir"

    # Inherit model from original spool. The stored model is already a resolved
    # "moonshot-ai/<model>" key (or "auto"); validate it before reserving a slot so a
    # stale model fails cleanly rather than as kimi-cli's opaque "LLM not set".
    model = original_spool.get("model")
    if model == "auto":
        model = None  # Let CLI choose
    model_error = _kimi_validate_model(model)
    if model_error:
        return model_error

    # Inherit thinking mode from the original spool.
    enable_thinking = bool(original_spool.get("thinking"))

    # Generate new spool ID
    spool_id = "kimi-" + str(uuid.uuid4())[:8]

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(spool_id, initial_status="pending")
    if not success:
        return error_msg

    # Build kimi resume command: use explicit session ID
    kimi_cmd = [
        "kimi-cli",
        "--session",
        session_id,  # Use the explicit session ID from the original spool
        "--print",
        "--yolo",
        "--output-format",
        "stream-json",
        "-p",
        prompt,
    ]

    if model:
        kimi_cmd.extend(["-m", model])

    if enable_thinking:
        kimi_cmd.append("--thinking")

    if working_dir:
        kimi_cmd.extend(["-w", working_dir])

    # Parse tags
    tag_list = ["kimi", "respin"]

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": session_id,  # Keep reference to original
        "working_dir": working_dir,
        "model": model or "auto",
        "thinking": enable_thinking,
        "system_prompt": None,
        "tags": tag_list,
        "timeout": original_spool.get("timeout"),
        "env": original_spool.get("env"),
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "kimi",
    }

    # Write spool to disk
    _write_spool(spool_id, spool)

    # Spawn the process detached
    pid = _spawn_detached(spool_id, kimi_cmd, working_dir, original_spool.get("env"))

    # Update spool with PID and status
    spool["pid"] = pid
    spool["status"] = "running"
    _write_spool(spool_id, spool)

    # Start background monitor thread
    _start_spool_monitor(spool_id)

    return spool_id


def _kimi_unspool_sync(spool_id: str) -> str:
    """Synchronous implementation of kimi_unspool - checks Kimi spool status."""
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
async def spindle_reload(force: bool = False) -> str:
    """
    Restart spindle to pick up code changes.

    By default this drains first: it returns immediately and a background thread
    waits until no spools are running or pending, then restarts - so in-flight
    agents finish cleanly instead of being orphaned. New spins are still accepted
    while draining; the restart simply happens at the next moment the queue is
    empty (which is the point - let normal work proceed and reload when it can).

    force=True restarts immediately without waiting, which may interrupt in-flight
    spools and leave them to orphan recovery on the next boot. This is the old
    behavior.

    Uses systemctl --user restart spindle. Requires the spindle systemd service.

    Returns:
        Status message
    """
    global _reload_pending

    # Check if systemd service exists (even if not running)
    result = subprocess.run(
        ["systemctl", "--user", "list-unit-files", "spindle.service"], capture_output=True, text=True
    )

    if "spindle.service" not in result.stdout:
        return "Error: spindle.service not found. Restart manually."

    # Check if currently active
    is_active = subprocess.run(["systemctl", "--user", "is-active", "spindle"], capture_output=True).returncode == 0

    def _do_restart():
        action = "restart" if is_active else "start"
        # We've already returned to the caller, so surface a failed restart to
        # the server log rather than letting it vanish in the daemon thread.
        proc = subprocess.run(["systemctl", "--user", action, "spindle"], capture_output=True, text=True)
        if proc.returncode != 0:
            print(
                f"[spindle] reload {action} failed (exit {proc.returncode}): {proc.stderr.strip()}",
                file=sys.stderr,
            )

    if force:

        def immediate():
            time.sleep(0.5)  # Give time for response to be sent
            _do_restart()

        threading.Thread(target=immediate, daemon=True).start()
        return "Restarting now via systemd (force)..." if is_active else "Starting via systemd..."

    if _reload_pending:
        return f"Reload already pending; will restart when idle ({_count_running()} spool(s) active)."

    _reload_pending = True

    def drain_and_restart():
        global _reload_pending
        try:
            _wait_until_idle()
            time.sleep(0.5)  # Give time for response to be sent
            _do_restart()
        finally:
            _reload_pending = False

    threading.Thread(target=drain_and_restart, daemon=True).start()

    active = _count_running()
    if active == 0:
        return "No spools active; restarting via systemd..." if is_active else "Starting via systemd..."
    return (
        f"Draining: {active} spool(s) active. Will restart via systemd when the queue is empty. "
        "Use force=True to restart now."
    )


def main():
    import argparse
    import atexit
    import traceback

    parser = argparse.ArgumentParser(description="Spindle MCP server")
    subparsers = parser.add_subparsers(dest="command")

    # serve command (default)
    serve_parser = subparsers.add_parser("serve", help="Run the MCP server")
    serve_parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    serve_parser.add_argument("--port", type=int, default=8002, help="HTTP port (default: 8002)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")

    # start command - start via systemd or background
    _start_parser = subparsers.add_parser("start", help="Start spindle (via systemd if available)")

    # reload command - restart spindle
    _reload_parser = subparsers.add_parser("reload", help="Reload spindle to pick up code changes")
    _reload_parser.add_argument(
        "--force",
        action="store_true",
        help="Restart immediately instead of draining (may interrupt in-flight spools)",
    )

    # status command
    _status_parser = subparsers.add_parser("status", help="Check spindle status")

    # install-service command
    install_service_parser = subparsers.add_parser("install-service", help="Install systemd user service")
    install_service_parser.add_argument("--force", action="store_true", help="Overwrite existing service file")

    # spin command - spawn an agent
    spin_parser = subparsers.add_parser("spin", help="Spawn an agent to handle a task")
    spin_parser.add_argument("prompt", help="The task/question for the agent")
    spin_parser.add_argument(
        "--permission",
        "-p",
        choices=[
            "readonly",
            "manual",
            "careful",
            "research",
            "full",
            "shard",
            "careful+shard",
            "research+shard",
            "auto",
            "auto+shard",
        ],
        help="Permission profile (default: careful, which is classifier-vetted auto)",
    )
    spin_parser.add_argument(
        "--research-target",
        help=(
            "Required when --permission is 'research' or 'research+shard'. "
            "Three forms: site:<skein-site-id> (output via skein post, Write/Edit not added to profile), "
            "file:<absolute-path> (single-file output, Write/Edit enabled), "
            "dir:<absolute-path> (multi-file output within dir, Write/Edit enabled)."
        ),
    )
    spin_parser.add_argument("--shard", "-s", action="store_true", help="Run in isolated git worktree")
    spin_parser.add_argument("--system-prompt", help="Optional system prompt")
    spin_parser.add_argument("--working-dir", "-d", help="Directory for the agent (default: current)")
    spin_parser.add_argument("--allowed-tools", help="Override permission profile with explicit tool list")
    spin_parser.add_argument("--tags", help="Comma-separated tags for organizing spools")
    spin_parser.add_argument(
        "--model",
        "-m",
        help="Model to use (e.g. haiku/sonnet/opus/fable for Claude, flash/pro for Gemini, thinking/k2.6/k2.5/k2.7-code for Kimi)",
    )
    spin_parser.add_argument("--harness", help="Harness to use: claude-code (default), codex, gemini, or kimi")
    spin_parser.add_argument("--timeout", "-t", type=int, help="Kill spool after N seconds")
    spin_parser.add_argument("--skeinless", action="store_true", help="Skip SKEIN context injection for shard agents")
    spin_parser.add_argument(
        "--base-branch", default=None, help="Branch to fork shard from (default: auto-detected from repo)"
    )
    spin_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

    # unspool command - get result
    unspool_parser = subparsers.add_parser("unspool", help="Get the result of a background spin task")
    unspool_parser.add_argument("spool_id", help="The spool ID to get output from")
    unspool_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

    # spools command - list all
    spools_parser = subparsers.add_parser("spools", help="List all spools (running and completed)")
    spools_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

    # wait command - wait for spools to complete
    wait_parser = subparsers.add_parser("wait", help="Wait for spools to complete")
    wait_parser.add_argument("spool_ids", nargs="?", help="Comma-separated spool IDs to wait for")
    wait_parser.add_argument(
        "--mode",
        "-m",
        choices=["gather", "yield"],
        default="gather",
        help="Wait mode: gather (all) or yield (first completed)",
    )
    wait_parser.add_argument("--timeout", "-t", type=int, help="Timeout in seconds")
    wait_parser.add_argument("--time", help="Duration to wait (e.g., 90m, 2h, 30s, 06:00)")
    wait_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

    # drop command - cancel a spool
    drop_parser = subparsers.add_parser("drop", help="Cancel a running spool")
    drop_parser.add_argument("spool_id", help="The spool ID to cancel")
    drop_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

    # peek command - see partial output
    peek_parser = subparsers.add_parser("peek", help="See partial output of a running spool")
    peek_parser.add_argument("spool_id", help="The spool ID to peek at")
    peek_parser.add_argument("--lines", "-n", type=int, default=50, help="Number of lines to show (default: 50)")
    peek_parser.add_argument("--human", action="store_true", help="Human-readable output instead of JSON")

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
        if "spindle.service" not in result.stdout:
            print("No systemd service. Kill and run: spindle start")
            sys.exit(0)
        # Drain by default: wait for the queue to empty so in-flight spools
        # aren't interrupted. --force skips the wait. New spins are still
        # allowed during the wait, so this restarts at the next idle moment.
        if not args.force:
            active = _count_running()
            if active:
                # flush: this is the last output before a potentially long block,
                # and stdout is block-buffered when redirected (not a tty).
                print(f"Draining: waiting for {active} spool(s) to finish (--force to restart now)...", flush=True)
            _wait_until_idle()
        proc = subprocess.run(["systemctl", "--user", "restart", "spindle"], capture_output=True, text=True)
        if proc.returncode == 0:
            print("Restarted via systemd")
            sys.exit(0)
        print(f"Restart failed (exit {proc.returncode}): {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    elif args.command == "status":
        result = subprocess.run(["curl", "-s", "http://127.0.0.1:8002/health"], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("Not running")
        sys.exit(0)

    elif args.command == "spin":
        working_dir = os.path.abspath(args.working_dir or os.getcwd())
        harness_lower = args.harness.lower() if args.harness else None
        conflict = _readonly_shard_conflict_error(
            args.permission, args.shard or _permission_implies_shard(args.permission)
        )
        if conflict:
            if args.human:
                print(f"Error: {conflict}", file=sys.stderr)
            else:
                print(json.dumps({"error": conflict}))
            sys.exit(1)
        if args.permission and args.permission.startswith("auto") and harness_lower and harness_lower != "claude-code":
            error_msg = f"permission={args.permission!r} requires harness='claude-code'; {harness_lower!r} has no classifier-vetted mode."
            if args.human:
                print(f"Error: {error_msg}", file=sys.stderr)
            else:
                print(json.dumps({"error": error_msg}))
            sys.exit(1)
        if harness_lower == "codex":
            sandbox = _codex_sandbox_for_permission(
                args.permission,
                args.research_target,
                cli_shard_full_access=True,
            )
            use_shard = args.shard or (args.permission and "shard" in args.permission)
            result = _codex_spin_sync(
                args.prompt,
                working_dir,
                args.model,
                sandbox,
                args.timeout,
                args.tags,
                None,
                shard=use_shard,
                base_branch=args.base_branch or _detect_default_branch(working_dir),
                skeinless=args.skeinless,
                research_target=args.research_target,
                require_research_target=args.permission in {"research", "research+shard"},
                permission=args.permission,
            )
        elif harness_lower == "gemini":
            use_shard = args.shard or (args.permission and "shard" in args.permission)
            result = _gemini_spin_sync(
                args.prompt,
                working_dir,
                args.model,
                args.system_prompt,
                args.timeout,
                args.tags,
                None,
                permission=args.permission,
                shard=use_shard,
                base_branch=args.base_branch or _detect_default_branch(working_dir),
                skeinless=args.skeinless,
                research_target=args.research_target,
                require_research_target=args.permission in {"research", "research+shard"},
            )
        elif harness_lower == "kimi":
            use_shard = args.shard or (args.permission and "shard" in args.permission)
            result = _kimi_spin_sync(
                args.prompt,
                working_dir,
                args.model,
                args.system_prompt,
                args.timeout,
                args.tags,
                None,
                permission=args.permission,
                shard=use_shard,
                base_branch=args.base_branch or _detect_default_branch(working_dir),
                skeinless=args.skeinless,
                research_target=args.research_target,
                require_research_target=args.permission in {"research", "research+shard"},
            )
        else:
            result = _spin_sync(
                prompt=args.prompt,
                permission=args.permission,
                shard=args.shard,
                system_prompt=args.system_prompt,
                working_dir=working_dir,
                allowed_tools=args.allowed_tools,
                tags=args.tags,
                model=args.model,
                timeout=args.timeout,
                skeinless=args.skeinless,
                base_branch=args.base_branch or _detect_default_branch(working_dir),
                research_target=args.research_target,
                env=None,
            )
        if result.startswith("Error:"):
            if args.human:
                print(f"Error: {result}")
            else:
                print(json.dumps({"error": result}))
            sys.exit(1)
        else:
            # Result is spool_id
            if args.human:
                print(f"Spawned spool: {result}")
            else:
                print(json.dumps({"spool_id": result}))
        sys.exit(0)

    elif args.command == "unspool":
        result = _unspool_sync(args.spool_id)
        if args.human:
            # For human output, just print the result directly
            print(result)
        else:
            # Try to parse as JSON if the spool result is JSON
            try:
                parsed = json.loads(result)
                print(json.dumps(parsed, indent=2))
            except json.JSONDecodeError:
                # Plain text result - wrap in JSON
                print(json.dumps({"result": result}))
        sys.exit(0)

    elif args.command == "spools":
        result = _spools_sync()
        if args.human:
            # Format as human-readable table
            spools_data = json.loads(result)
            if not spools_data:
                print("No spools found")
            else:
                print(f"{'ID':<12} {'Status':<10} {'Prompt':<50}")
                print("-" * 72)
                for spool_id, info in spools_data.items():
                    status = info.get("status", "unknown")
                    prompt = info.get("prompt", "")[:47]
                    if len(info.get("prompt", "")) > 47:
                        prompt += "..."
                    print(f"{spool_id:<12} {status:<10} {prompt}")
        else:
            print(result)
        sys.exit(0)

    elif args.command == "wait":
        result = _spin_wait_sync(
            spool_ids=args.spool_ids,
            mode=args.mode,
            timeout=args.timeout,
            time_param=args.time,
        )
        if args.human:
            if result.startswith("Error:"):
                print(result)
            else:
                try:
                    parsed = json.loads(result)
                    if "elapsed_seconds" in parsed:
                        # Time-based wait result
                        print(f"Waited {parsed.get('waited', 'unknown')} ({parsed['elapsed_seconds']}s)")
                        if parsed.get("interrupted"):
                            print("(interrupted)")
                    else:
                        # Spool results
                        for spool_id, res in parsed.items():
                            print(f"\n=== {spool_id} ===")
                            print(res[:500] if len(res) > 500 else res)
                except json.JSONDecodeError:
                    print(result)
        else:
            print(result)
        sys.exit(0)

    elif args.command == "drop":
        result = _spin_drop_sync(args.spool_id)
        if args.human:
            print(result)
        else:
            if result.startswith("Error:") or result.startswith("Spool"):
                print(json.dumps({"message": result}))
            else:
                print(json.dumps({"dropped": args.spool_id}))
        sys.exit(0)

    elif args.command == "peek":
        result = _spool_peek_sync(args.spool_id, lines=args.lines)
        if args.human:
            print(result)
        else:
            print(json.dumps({"spool_id": args.spool_id, "output": result}))
        sys.exit(0)

    elif args.command == "install-service":
        import platform
        import shutil

        system = platform.system()

        # Find spindle executable path
        spindle_path = shutil.which("spindle")
        if not spindle_path:
            if system == "Darwin":
                # Common locations on macOS
                for p in ["/usr/local/bin/spindle", "/opt/homebrew/bin/spindle"]:
                    if Path(p).exists():
                        spindle_path = p
                        break
                if not spindle_path:
                    spindle_path = "/usr/local/bin/spindle"
            else:
                spindle_path = str(Path.home() / ".local" / "bin" / "spindle")

        if system == "Linux":
            # Check if systemd is actually running (important for WSL)
            systemd_check = subprocess.run(["systemctl", "--user", "status"], capture_output=True, text=True)
            if systemd_check.returncode != 0 and "Failed to connect" in systemd_check.stderr:
                # Detect WSL
                is_wsl = False
                try:
                    with open("/proc/version", "r") as f:
                        if "microsoft" in f.read().lower():
                            is_wsl = True
                except Exception:
                    pass

                if is_wsl:
                    print("WSL detected but systemd is not running.")
                    print("\nTo enable systemd in WSL2, add to /etc/wsl.conf:")
                    print("  [boot]")
                    print("  systemd=true")
                    print("\nThen restart WSL: wsl --shutdown")
                    print("\nOr run spindle manually: spindle serve --http")
                else:
                    print("systemd user session not available.")
                    print("Run spindle manually: spindle serve --http")
                sys.exit(1)

            service_content = f"""\
[Unit]
Description=Spindle MCP Server
After=network.target

[Service]
Type=simple
ExecStart={spindle_path} serve --http
Restart=on-failure
RestartSec=5
Environment=PATH=%h/.local/bin:/usr/bin

[Install]
WantedBy=default.target
"""
            service_dir = Path.home() / ".config" / "systemd" / "user"
            service_file = service_dir / "spindle.service"

            if service_file.exists() and not args.force:
                print(f"Service file already exists: {service_file}")
                print("Use --force to overwrite")
                sys.exit(1)

            service_dir.mkdir(parents=True, exist_ok=True)
            service_file.write_text(service_content)
            print(f"Wrote {service_file}")

            subprocess.run(["systemctl", "--user", "daemon-reload"])
            print("Reloaded systemd")

            subprocess.run(["systemctl", "--user", "enable", "spindle"])
            print("Enabled spindle service")

            print("\nTo start now: spindle start")
            print("To check status: spindle status")

        elif system == "Darwin":
            plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.spindle.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>{spindle_path}</string>
        <string>serve</string>
        <string>--http</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.spindle/spindle.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.spindle/spindle.log</string>
</dict>
</plist>
"""
            launch_agents = Path.home() / "Library" / "LaunchAgents"
            plist_file = launch_agents / "com.spindle.server.plist"

            if plist_file.exists() and not args.force:
                print(f"Plist already exists: {plist_file}")
                print("Use --force to overwrite")
                sys.exit(1)

            # Unload if already loaded
            if plist_file.exists():
                subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True)

            launch_agents.mkdir(parents=True, exist_ok=True)
            plist_file.write_text(plist_content)
            print(f"Wrote {plist_file}")

            # Ensure log directory exists
            (Path.home() / ".spindle").mkdir(parents=True, exist_ok=True)

            # Load the service
            subprocess.run(["launchctl", "load", str(plist_file)])
            print("Loaded launchd service")

            print("\nService is now running.")
            print("To check status: spindle status")
            print("To stop: launchctl unload ~/Library/LaunchAgents/com.spindle.server.plist")

        else:
            print(f"Service installation not supported on {system}.")
            print("Run spindle manually:")
            print("  spindle serve --http")
            print("\nOn Windows, consider using NSSM to create a service.")
            sys.exit(1)

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
        mcp.run(transport="streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run()
    log("FINISHED mcp.run()")


if __name__ == "__main__":
    main()
