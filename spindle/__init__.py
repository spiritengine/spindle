#!/usr/bin/env python3
"""
Spindle - MCP server for Claude Code to Claude Code delegation.

Lets CC agents spawn other CC agents, all using Max subscription credits.
Async by default - spin returns immediately, check results later.

Storage: ~/.spindle/spools/{spool_id}.json

Subprocess handling: Uses detached processes that survive MCP reconnects.
A per-store supervisor reconciles durable spool state after callers exit.
"""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Generator, Optional, Tuple

# The stream driver is a standalone top-level module (not part of this
# package) so the driver child process can import the shared background-task
# tracking without triggering this module's server startup side effects. In an
# installed wheel it sits next to the package in site-packages; in a checkout
# it sits at the repo root, which may not be on sys.path when the package is
# imported by path (some test runners), hence the fallback.
try:
    import spindle_claude_driver as _claude_driver
except ImportError:  # checkout where the repo root is not on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import spindle_claude_driver as _claude_driver

# fastmcp imports authlib, which announces a deprecation of its own internals as
# it loads — three lines of stderr in front of the output of every single CLI
# command, about something no spindle user can act on. A warnings filter can't
# stop it: authlib itself calls warnings.simplefilter("always", ...) at import,
# which takes precedence over anything registered earlier. So drop that one
# message for the duration of the import and hand the machinery straight back.
# (The E402s below are the cost of having to sit in front of the import.)
_AUTHLIB_IMPORT_NOISE = "authlib.jose module is deprecated"
_real_showwarning = warnings.showwarning


def _showwarning_without_authlib_noise(message, category, filename, lineno, file=None, line=None):
    if _AUTHLIB_IMPORT_NOISE in str(message):
        return
    _real_showwarning(message, category, filename, lineno, file, line)


warnings.showwarning = _showwarning_without_authlib_noise
try:
    from fastmcp import Context, FastMCP  # noqa: E402
    from starlette.requests import Request  # noqa: E402
    from starlette.responses import JSONResponse  # noqa: E402
finally:
    # Restore only if ours is still the installed hook. Anything imported above
    # may legitimately install its own (logging.captureWarnings does), and
    # blindly reassigning would silently uninstall it.
    if warnings.showwarning is _showwarning_without_authlib_noise:
        warnings.showwarning = _real_showwarning

from ._version import __version__  # noqa: E402
from .namespace_owner import (  # noqa: E402
    LegacyAuthority,
    LivenessEvidence,
    LockEvidence,
    MalformedControlReceipt,
    NamespaceIdentity,
    ProcessIdentity,
    ReconciliationResult,
    assess_process_liveness,
    capture_pid_namespace,
    classify_owner_episode,
    create_control_request,
    iter_control_requests,
    mailbox_guard,
    probe_ownership_lock,
    read_control_receipt,
    reconcile_owner_episode,
    retire_owner_artifacts,
    transition_owner_episode,
    write_control_receipt,
)
from .namespace_owner import (  # noqa: E402
    acquire_ownership_lock as acquire_ownership_lock,
)

# Re-exported for the convergence applicator, which reaches every mailbox writer
# through this module so one patch point covers them all.
from .namespace_owner import (  # noqa: E402
    update_control_receipt as update_control_receipt,
)

mcp = FastMCP("spindle")

# Set up logging
logger = logging.getLogger(__name__)

# Track server start time for uptime calculation
_server_start_time = datetime.now()

# Port the serving process actually bound, set by main() before mcp.run(). None
# in a CLI process (which serves nothing) and in stdio mode (no port at all).
_server_port: Optional[int] = None


def _package_path() -> str:
    """Absolute path of this module - the identity of *which* install this is.

    Two spindles on one machine (a repo checkout and a released wheel) report
    different paths here, which is how `spindle doctor` tells the service it is
    talking to apart from the CLI asking.
    """
    return str(Path(__file__).resolve())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for monitoring, systemd watchdog, and doctor.

    The identity fields (version/package/pid/spool_dir) let a client decide
    whether the service answering this port is the same install it is running
    from. Older services answer without them; doctor treats a missing `version`
    as "unidentifiable", never as "matches".
    """
    uptime_seconds = (datetime.now() - _server_start_time).total_seconds()
    running_count = _count_running()

    return JSONResponse(
        {
            "status": "healthy",
            "uptime_seconds": int(uptime_seconds),
            "running_spools": running_count,
            "max_concurrent": MAX_CONCURRENT,
            "version": __version__,
            "package": _package_path(),
            "pid": os.getpid(),
            "python": sys.executable,
            "spool_dir": str(SPINDLE_DIR),
            "port": _server_port,
            # The PATH the service will actually search when it spawns a harness.
            # A unit's PATH is baked at install time and goes stale (a new node
            # version moves `codex`), and the failure surfaces much later as a
            # spool that dies at spawn. Reporting it lets doctor catch the drift
            # instead of only checking the PATH of the shell asking.
            "path": os.environ.get("PATH", ""),
        }
    )


@dataclass(frozen=True)
class _StoreLayout:
    """Names versioned store roots without creating or activating either one."""

    schema1_root: Path
    schema2_root: Path
    active_root: Path
    active_schema: int


def _resolve_store_layout(spindle_home: Optional[Path] = None) -> _StoreLayout:
    """Resolve today's schema-1 store and the reserved sibling schema-2 path."""
    home = spindle_home or Path(os.environ.get("SPINDLE_HOME", str(Path.home() / ".spindle")))
    schema1_root = home / "spools"
    return _StoreLayout(
        schema1_root=schema1_root,
        schema2_root=home / "spools-v2",
        active_root=schema1_root,
        active_schema=1,
    )


# Storage directory. SPINDLE_HOME is honored (like the other config env vars
# below) so tests can redirect the whole store to a tmp dir before import and
# never touch the real ~/.spindle, even from an escaped monitor thread.
_STORE_LAYOUT = _resolve_store_layout()
SPINDLE_DIR = _STORE_LAYOUT.active_root

# Canonical location for lodged profiles (folder-per-profile, each holding a
# profile.json). Sits beside the spool store and honors SPINDLE_HOME the same
# way, so tests redirect both with one env var and never read real profiles.
SPINDLE_PROFILES_DIR = Path(os.environ.get("SPINDLE_HOME", str(Path.home() / ".spindle"))) / "profiles"

# Built-in harness names. These always win over a same-named lodged profile.
BUILTIN_HARNESSES = {"claude-code", "codex", "gemini", "kimi"}

# Executable each built-in harness shells out to, in the order doctor reports
# them. The value is what has to be on PATH for that harness to run at all.
HARNESS_COMMANDS = {
    "claude-code": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "kimi": "kimi-cli",
}

# HTTP endpoint of the spindle service. Both are env-overridable so a second
# install can serve its own port instead of fighting the first for 8002 - the
# systemd unit written by `spindle install-service --port N` sets SPINDLE_PORT,
# so `spindle status`/`spindle doctor` in that environment resolve the same port
# the service bound.
DEFAULT_HOST = os.environ.get("SPINDLE_HOST", "127.0.0.1")


# The port with no configuration of any kind behind it. Kept separate from
# DEFAULT_PORT, which folds in $SPINDLE_PORT: regenerating an existing service
# must fall back to the former, never to whatever the calling shell exports.
_BASE_DEFAULT_PORT = 8002


def _default_port() -> int:
    """Service port from SPINDLE_PORT, falling back to 8002 on a bad value."""
    raw = os.environ.get("SPINDLE_PORT", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("spindle: ignoring non-numeric SPINDLE_PORT=%r, using %d", raw, _BASE_DEFAULT_PORT)
    return _BASE_DEFAULT_PORT


DEFAULT_PORT = _default_port()

# Live subprocess handles keyed by spool_id, populated by _spawn_detached so
# finalization can capture the child's exit code. Process-local; not persisted.
_PROC_HANDLES: Dict[str, "subprocess.Popen"] = {}
# Write ends of one-shot launch barriers. A detached wrapper cannot exec its
# harness until the parent has durably published the wrapper PID. If the parent
# dies first, kernel close-on-exit turns the wrapper's read into EOF and the
# harness is never started.
_SPAWN_BARRIERS: Dict[str, int] = {}
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

# Timeout for pending spools that never got a PID (seconds). Environment
# overrides keep the real-subprocess crash contracts fast without adding a
# production switch that can disable durable ownership.
PENDING_SPAWN_TIMEOUT = float(os.environ.get("SPINDLE_PENDING_SPAWN_TIMEOUT", "60"))
# A live launcher may legitimately spend longer than the stale-reservation
# threshold creating a shard or importing a harness. Its durable PID protects
# that setup window, but only for a bounded interval so a long-lived service
# cannot leave a pending spool wedged after an internal exception.
PENDING_LAUNCH_TIMEOUT = float(os.environ.get("SPINDLE_PENDING_LAUNCH_TIMEOUT", "120"))

# Poll interval for monitoring detached processes
MONITOR_POLL_INTERVAL = float(os.environ.get("SPINDLE_MONITOR_POLL_INTERVAL", "2"))
SPOOL_TERMINAL_LOCK_TIMEOUT = 5.0  # seconds
OUTPUT_COMPLETION_GRACE_SECONDS = 5.0

# One detached supervisor owns all pending/running spools in a resolved store.
# Protocol/schema changes are explicit so a new launcher never asks an old
# process to parse records it does not understand.
SUPERVISOR_PROTOCOL_VERSION = 2
SPOOL_SCHEMA_VERSION = 1
SUPPORTED_SUPERVISOR_PROTOCOL_RANGE = (SUPERVISOR_PROTOCOL_VERSION, SUPERVISOR_PROTOCOL_VERSION)
READABLE_SPOOL_SCHEMAS = (SPOOL_SCHEMA_VERSION,)
WRITABLE_SPOOL_SCHEMA = SPOOL_SCHEMA_VERSION
SUPERVISOR_CAPABILITIES = (
    "supervisor-compatibility-ranges",
    "owner-episode-v1",
    "owner-convergence-v1",
)
REQUIRED_SUPERVISOR_CAPABILITIES = frozenset(SUPERVISOR_CAPABILITIES)
SUPERVISOR_IMPORT_GUARD = "_SPINDLE_STORE_SUPERVISOR"
SUPERVISOR_POLL_INTERVAL = float(os.environ.get("SPINDLE_SUPERVISOR_POLL_INTERVAL", "0.5"))
SUPERVISOR_IDLE_GRACE = float(os.environ.get("SPINDLE_SUPERVISOR_IDLE_GRACE", "5"))

# Poll interval for draining the queue before a reload (spindle_reload)
RELOAD_DRAIN_POLL_INTERVAL = 5  # seconds


@dataclass(frozen=True)
class DrainBlocker:
    """A spool which cannot make protocol-authorized progress during drain."""

    spool_id: str
    reason: str


class DrainBlockedError(RuntimeError):
    """Raised when drain would otherwise wait forever on abandoned custody."""

    def __init__(self, blockers) -> None:
        self.blockers = tuple(blockers)
        details = "; ".join(f"{item.spool_id}: {item.reason}" for item in self.blockers)
        super().__init__(f"drain blocked by unrecoverable spool custody ({details})")


# Tags that mark a spool as a review/fell pass. Review spools get a soft default
# timeout (DEFAULT_REVIEW_TIMEOUT) when the caller didn't pass an explicit one.
# Typical reviews finish in 10-30 min; 90 min caps runaway wedged spools.
# "review" is the literal tag; fell-rN rounds are matched by _is_review_tag()
# so fell has no iteration cap (fell-r6+ spools are covered without enumeration).
REVIEW_TAGS = {"review"}
DEFAULT_REVIEW_TIMEOUT = int(os.environ.get("SPINDLE_REVIEW_TIMEOUT", str(90 * 60)))

# Persistent stream-json driver for headless Claude spools (see
# spindle_claude_driver.py and finding-20260724-2niy). It is the default for
# new spools; set SPINDLE_CLAUDE_STREAM_DRIVER=0 for the legacy one-shot
# rollback path. Each spool records the protocol it actually launched under so
# old one-shot spools and new driver spools coexist across rollouts and server
# restarts.
CLAUDE_STREAM_DRIVER_ENV = "SPINDLE_CLAUDE_STREAM_DRIVER"
CLAUDE_PROTOCOL_STREAM_V1 = _claude_driver.DRIVER_PROTOCOL


def _claude_stream_driver_enabled() -> bool:
    value = os.environ.get(CLAUDE_STREAM_DRIVER_ENV)
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _stream_driver_script_path() -> Optional[Path]:
    """Locate the standalone driver script next to this package.

    Works for both an installed wheel (top-level module beside the package in
    site-packages) and a repo checkout (module at the repo root).
    """
    path = Path(_claude_driver.__file__).resolve()
    return path if path.exists() else None


def _claude_headless_cmd(prompt: str, flags: list, prompt_path: Optional[Path] = None) -> Tuple[list, Optional[str]]:
    """Build the argv for a headless Claude launch, honoring the driver flag.

    Returns ``(cmd, claude_protocol)`` — protocol is CLAUDE_PROTOCOL_STREAM_V1
    when the persistent stream driver wraps the launch, None for the classic
    one-shot ``claude -p``. Every headless launch also gets the background-task
    guard: one-shot turns cannot receive Monitor/ScheduleWakeup notifications
    (the process exits at end of turn, parking the agent — see
    error_kind=headless_background_wait), so those tools are disallowed
    outright. Under the driver, Monitor notifications are deliverable, but
    ScheduleWakeup's fire-while-headless semantics are unverified, so it stays
    disallowed there too. A backgrounded Bash command cannot be blocked by
    tool name; the driver-stream parked detection remains the backstop for it.

    ``prompt_path``: deliver the prompt from this file instead of argv — a
    single argv element is capped at 128KiB on Linux, which rebuilt transcript
    continuations routinely exceed. The caller must have written ``prompt`` to
    that path already. Driver launches read it via --prompt-file; one-shot
    launches take no positional prompt, and the caller MUST spawn them with
    stdin redirected from the file (``claude -p`` reads the prompt from stdin).
    """
    if _claude_stream_driver_enabled():
        driver = _stream_driver_script_path()
        if driver is not None:
            prompt_args = ["--prompt-file", str(prompt_path)] if prompt_path is not None else ["--prompt", prompt]
            cmd = [
                sys.executable,
                str(driver),
                *prompt_args,
                "--",
                "claude",
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--replay-user-messages",
                "--verbose",
                "--disallowedTools",
                "ScheduleWakeup",
                *flags,
            ]
            return cmd, CLAUDE_PROTOCOL_STREAM_V1
        logger.warning(
            "spindle: stream driver is enabled but its script is missing; "
            "falling back to one-shot claude (set %s=0 for an explicit rollback)",
            CLAUDE_STREAM_DRIVER_ENV,
        )
    cmd = [
        "claude",
        "-p",
        *([] if prompt_path is not None else [prompt]),
        "--output-format",
        "json",
        "--disallowedTools",
        "Monitor,ScheduleWakeup",
        *flags,
    ]
    return cmd, None


def _is_review_tag(tag: str) -> bool:
    """Return True if tag marks a spool as a review/fell pass.

    "review" is matched literally; "fell-rN" (any N) is matched by regex so
    the fell process can iterate past r5 without losing the soft timeout.
    """
    return tag in REVIEW_TAGS or bool(re.match(r"^fell-r\d+$", tag))


def _normalize_launch_timeout(timeout, tags=None):
    """Resolve the effective wall timeout before reserving an owner episode."""
    if timeout == 0:
        return None
    if timeout is not None:
        return timeout
    if isinstance(tags, str):
        tag_values = [tag.strip() for tag in tags.split(",") if tag.strip()]
    else:
        tag_values = list(tags or [])
    if any(_is_review_tag(tag) for tag in tag_values):
        return DEFAULT_REVIEW_TIMEOUT
    return None


def _resolve_launch_timeout(timeout, tags=None) -> tuple[Optional[int], bool]:
    """Return the effective timeout and whether explicit zero disabled it.

    ``None`` is also the pre-normalization value for an omitted timeout, so it
    cannot carry explicit-zero intent by itself.  Persist the boolean beside
    the effective value so retry and respin can distinguish "no preference"
    from "never apply a wall deadline".
    """
    return _normalize_launch_timeout(timeout, tags), timeout == 0


def _replay_launch_timeout(spool: dict) -> tuple[Optional[int], bool]:
    """Resolve a fresh episode's timeout without losing explicit-zero intent."""
    if spool.get("timeout_disabled") is True:
        return None, True
    return _resolve_launch_timeout(spool.get("timeout"), spool.get("tags"))


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


def _publish_created_shard(
    spool_id: str,
    source_dir: str,
    base_branch: str,
    shard_info: Dict[str, str],
) -> None:
    """Publish shard recovery identity before returning to general setup."""
    with _spool_lock(spool_id) as acquired:
        if not acquired:
            return
        current = _read_spool(spool_id)
        if not current:
            return
        current.update(
            {
                "working_dir": shard_info["worktree_path"],
                "shard": shard_info,
                "shard_created_by_spool": True,
                "shard_source_dir": source_dir,
                "base_branch": base_branch,
            }
        )
        if current.get("status") != "pending":
            _preserve_failed_spool_shard(current)
        _write_spool(spool_id, current)


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
                        shard_info = {
                            "worktree_path": worktree_path,
                            "branch_name": branch_name or f"shard-{agent_id}",
                            "shard_id": shard_id or agent_id,
                        }
                        _publish_created_shard(agent_id, working_dir, base_branch, shard_info)
                        return shard_info, None
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

        # The spool id is already unique. A deterministic name lets recovery
        # rediscover the worktree after a launcher crash without guessing a
        # timestamp generated only in the dead process.
        worktree_name = agent_id
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
            shard_info = {
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "shard_id": worktree_name,
            }
            _publish_created_shard(agent_id, working_dir, base_branch, shard_info)
            return shard_info, None
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
    """Mark a failed or timed-out spool's new shard for explicit recovery.

    Automatic cleanup cannot atomically prove that ignored output was not
    created between its final Git status check and worktree removal. Preserve
    every shard whose agent failed instead; retry can safely reuse it, and a
    human or SKEIN can inspect and explicitly clean it later.
    """
    if spool.get("status") not in {"error", "timeout"} or not spool.get("shard_created_by_spool"):
        return False
    shard_info = spool.get("shard") or {}
    changed = (
        shard_info.get("startup_failure_preserved") is not True
        or spool.get("shard_cleanup_preserved") is not True
        or spool.get("shard_cleanup_preserved_reason") != "automatic cleanup disabled after agent failure"
        or "shard_cleanup_pending" in spool
        or "shard_cleanup_pending_reason" in spool
    )
    shard_info["startup_failure_preserved"] = True
    spool["shard"] = shard_info
    spool["shard_cleanup_preserved"] = True
    spool["shard_cleanup_preserved_reason"] = "automatic cleanup disabled after agent failure"
    spool.pop("shard_cleanup_pending", None)
    spool.pop("shard_cleanup_pending_reason", None)
    return changed


def _clear_preserved_spool_shard(spool: dict) -> None:
    """Clear recovery markers after an explicit merge or abandon resolves a shard."""
    spool.pop("shard_cleanup_preserved", None)
    spool.pop("shard_cleanup_preserved_reason", None)
    spool.pop("shard_cleanup_pending", None)
    spool.pop("shard_cleanup_pending_reason", None)
    shard_info = spool.get("shard") or {}
    shard_info.pop("startup_failure_preserved", None)
    shard_info.pop("merge_in_progress", None)
    shard_info.pop("merge_in_progress_at", None)
    shard_info.pop("merge_failed", None)
    shard_info.pop("merge_failed_at", None)
    shard_info.pop("merge_error", None)
    shard_info.pop("abandon_in_progress", None)
    shard_info.pop("abandon_in_progress_at", None)


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


def _get_process_identity_path(spool_id: str) -> Path:
    """Get the portable lifetime-lock path for a detached wrapper."""
    return SPINDLE_DIR / f"{spool_id}.process"


def _get_owner_lock_path(spool_id: str) -> Path:
    return SPINDLE_DIR / f"{spool_id}.process-owner"


def _get_owner_identity_path(spool_id: str) -> Path:
    return SPINDLE_DIR / f"{spool_id}.owner-identity"


def _get_owner_exit_path(spool_id: str) -> Path:
    return SPINDLE_DIR / f"{spool_id}.owner-exit"


def _get_prompt_path(spool_id: str) -> Path:
    """Get path to a spool's file-delivered prompt.

    Rebuilt transcript continuations routinely exceed Linux's 128KiB per-argv
    limit (MAX_ARG_STRLEN), so they are written here and delivered via stdin
    (one-shot claude) or --prompt-file (stream driver) instead of argv.
    """
    return SPINDLE_DIR / f"{spool_id}.prompt"


# Prompts above this byte size are delivered by file instead of argv — margin
# under Linux's MAX_ARG_STRLEN (131072 bytes per argv element), which 1,100+
# real transcript-continuation prompts exceed (fell round 2 measurement).
PROMPT_ARGV_LIMIT = 100_000


def _prompt_file_if_oversized(spool_id: str, prompt: str) -> Optional[Path]:
    """Write an over-limit prompt to the spool's prompt file; None if argv fits.

    Raises IOError if the file cannot be written.
    """
    if len(prompt.encode("utf-8", errors="ignore")) <= PROMPT_ARGV_LIMIT:
        return None
    path = _get_prompt_path(spool_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt)
    return path


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

    # Metadata writers may have worked from a snapshot while an owner advanced
    # the authoritative episode.  They may never lower or remove that episode.
    try:
        current = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        current = None
    current_episode = current.get("owner_episode") if isinstance(current, dict) else None
    incoming_episode = data.get("owner_episode")
    if isinstance(current_episode, dict):
        current_order = (current_episode.get("generation", 0), current_episode.get("revision", 0))
        incoming_order = (
            (incoming_episode.get("generation", 0), incoming_episode.get("revision", 0))
            if isinstance(incoming_episode, dict)
            else (-1, -1)
        )
        if incoming_order < current_order:
            data = dict(data)
            data["owner_episode"] = current_episode

    # The reduced provider-lifecycle block (lifecycle.provider) is authoritative
    # like the episode: a metadata writer working from a stale snapshot may never
    # lower its monotonic sequence.  Scoped to the provider sub-key so the owner's
    # flat lifecycle keys are untouched.  This is defense-in-depth; the primary
    # guarantee is that every lifecycle writer holds the same <id>.lock.
    current_provider = None
    if isinstance(current, dict) and isinstance(current.get("lifecycle"), dict):
        maybe = current["lifecycle"].get("provider")
        if isinstance(maybe, dict):
            current_provider = maybe
    if current_provider is not None:
        incoming_lifecycle = data.get("lifecycle")
        incoming_provider = incoming_lifecycle.get("provider") if isinstance(incoming_lifecycle, dict) else None
        incoming_seq = incoming_provider.get("sequence", -1) if isinstance(incoming_provider, dict) else -1
        if incoming_seq < current_provider.get("sequence", 0):
            data = dict(data)
            merged_lifecycle = dict(data.get("lifecycle") or {})
            merged_lifecycle["provider"] = current_provider
            data["lifecycle"] = merged_lifecycle

    # Durable atomic replace: write temp, fsync the file, atomically replace, then
    # fsync the directory so the rename survives a crash.  Without the fsyncs a
    # later write could silently un-durable an authoritative episode or lifecycle
    # block, and a crash could leave a torn <id>.json.
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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


def _canonical_worktree_path(worktree_path: Optional[str]) -> Optional[str]:
    """Return the stable lock key for a shard worktree path."""
    if not worktree_path:
        return None
    return str(Path(worktree_path).resolve())


def _spool_worktree_path(spool: Optional[dict]) -> Optional[str]:
    """Read a canonical shard worktree path from a spool record."""
    shard = (spool or {}).get("shard") or {}
    return _canonical_worktree_path(shard.get("worktree_path"))


@contextmanager
def _worktree_lock(worktree_path: Optional[str], blocking: bool = True) -> Generator[bool, None, None]:
    """Serialize launch and destructive operations that share a shard worktree."""
    canonical = _canonical_worktree_path(worktree_path)
    if canonical is None:
        yield True
        return

    digest = hashlib.sha256(canonical.encode()).hexdigest()
    lock_path = SPINDLE_DIR / ".worktree-locks" / f"{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    acquired = False
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(lock_fd, flags)
            acquired = True
        except BlockingIOError:
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
        if path.name.startswith("."):
            continue
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


def _supervisor_record_path() -> Path:
    return SPINDLE_DIR / ".supervisor.json"


def _supervisor_lifetime_lock_path() -> Path:
    return SPINDLE_DIR / ".supervisor.lock"


def _supervisor_control_lock_path() -> Path:
    return SPINDLE_DIR / ".supervisor-control.lock"


def _supervisor_log_path() -> Path:
    return SPINDLE_DIR / "supervisor.log"


@contextmanager
def _supervisor_control_lock() -> Generator[None, None, None]:
    """Serialize owner compatibility/startup, reservation, and retirement."""
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_supervisor_control_lock_path(), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_supervisor_record() -> Optional[dict]:
    try:
        data = json.loads(_supervisor_record_path().read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


_SUPERVISOR_MANAGED_FIELDS = frozenset(
    {
        "pid",
        "supervisor_protocol_version",
        "spool_schema_version",
        "supported_supervisor_protocol_range",
        "readable_spool_schemas",
        "writable_spool_schema",
        "supervisor_capabilities",
        "package",
        "package_version",
        "started_at",
        "retired_at",
        "store",
    }
)


def _write_supervisor_record(data: dict) -> None:
    previous = _read_supervisor_record() or {}
    record = {key: value for key, value in previous.items() if key not in _SUPERVISOR_MANAGED_FIELDS}
    record.update(data)
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    path = _supervisor_record_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, path)


def _supervisor_identity(pid: int, *, started_at: Optional[str] = None) -> dict:
    return {
        "pid": pid,
        # The scalar fields identify this implementation to scalar readers;
        # current readers use the complete additive negotiation below.
        "supervisor_protocol_version": SUPERVISOR_PROTOCOL_VERSION,
        "spool_schema_version": SPOOL_SCHEMA_VERSION,
        "supported_supervisor_protocol_range": {
            "min": SUPPORTED_SUPERVISOR_PROTOCOL_RANGE[0],
            "max": SUPPORTED_SUPERVISOR_PROTOCOL_RANGE[1],
        },
        "readable_spool_schemas": list(READABLE_SPOOL_SCHEMAS),
        "writable_spool_schema": WRITABLE_SPOOL_SCHEMA,
        "supervisor_capabilities": list(SUPERVISOR_CAPABILITIES),
        "package": _package_path(),
        "package_version": __version__,
        "started_at": started_at or datetime.now().isoformat(),
        "store": str(SPINDLE_DIR.resolve()),
    }


_SUPERVISOR_NEGOTIATION_FIELDS = frozenset(
    {
        "supported_supervisor_protocol_range",
        "readable_spool_schemas",
        "writable_spool_schema",
        "supervisor_capabilities",
    }
)


def _compatibility_owner(record: dict) -> str:
    pid = record.get("pid")
    package = record.get("package")
    if package:
        return f"live owner pid={pid!r} package={package!r}"
    return f"live owner pid={pid!r}"


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_supervisor_negotiation(record: dict) -> tuple[Optional[dict], Optional[str]]:
    protocol_range = record.get("supported_supervisor_protocol_range")
    if not isinstance(protocol_range, dict) or not {"min", "max"}.issubset(protocol_range):
        return None, "supported_supervisor_protocol_range must contain integer min and max"
    protocol_min = protocol_range["min"]
    protocol_max = protocol_range["max"]
    if not _positive_int(protocol_min) or not _positive_int(protocol_max) or protocol_min > protocol_max:
        return None, "supported_supervisor_protocol_range must be a positive ordered range"

    readable = record.get("readable_spool_schemas")
    if not isinstance(readable, list) or not readable or any(not _positive_int(schema) for schema in readable):
        return None, "readable_spool_schemas must be a nonempty list of positive integers"

    writable = record.get("writable_spool_schema")
    if not _positive_int(writable):
        return None, "writable_spool_schema must be a positive integer"

    capabilities = record.get("supervisor_capabilities")
    if not isinstance(capabilities, list) or any(
        not isinstance(capability, str) or not capability for capability in capabilities
    ):
        return None, "supervisor_capabilities must be a list of nonempty strings"

    return {
        "protocol_range": (protocol_min, protocol_max),
        "readable_schemas": frozenset(readable),
        "writable_schema": writable,
        "capabilities": frozenset(capabilities),
    }, None


def _supervisor_compatibility_error(record: Optional[dict]) -> Optional[str]:
    if not record:
        return "active store supervisor did not publish its compatibility record"

    negotiation_fields = _SUPERVISOR_NEGOTIATION_FIELDS.intersection(record)
    if not negotiation_fields:
        # A scalar-only owner cannot prove that it understands convergence's
        # sole-applicator and rollback duties.  Preserve the precise scalar
        # protocol/schema diagnostics, then refuse even an otherwise-current
        # scalar owner because capability absence is incompatibility.
        protocol = record.get("supervisor_protocol_version")
        schema = record.get("spool_schema_version")
        if protocol != SUPERVISOR_PROTOCOL_VERSION:
            return (
                "active store supervisor protocol is incompatible "
                f"({_compatibility_owner(record)}; owner={protocol!r}, launcher={SUPERVISOR_PROTOCOL_VERSION})"
            )
        if schema != SPOOL_SCHEMA_VERSION:
            return (
                "active spool schema is incompatible "
                f"({_compatibility_owner(record)}; owner={schema!r}, launcher={SPOOL_SCHEMA_VERSION})"
            )
        return (
            "active store supervisor capabilities are incompatible "
            f"({_compatibility_owner(record)}; owner_capabilities=[] "
            f"launcher_requires_capabilities={sorted(REQUIRED_SUPERVISOR_CAPABILITIES)})"
        )

    if negotiation_fields != _SUPERVISOR_NEGOTIATION_FIELDS:
        missing = sorted(_SUPERVISOR_NEGOTIATION_FIELDS - negotiation_fields)
        return (
            "active store supervisor compatibility record is incomplete "
            f"({_compatibility_owner(record)}; missing={missing})"
        )

    negotiation, invalid = _parse_supervisor_negotiation(record)
    if invalid:
        return f"active store supervisor compatibility record is invalid ({_compatibility_owner(record)}; {invalid})"
    assert negotiation is not None

    owner_min, owner_max = negotiation["protocol_range"]
    launcher_min, launcher_max = SUPPORTED_SUPERVISOR_PROTOCOL_RANGE
    protocol_compatible = max(owner_min, launcher_min) <= min(owner_max, launcher_max)
    owner_readable = negotiation["readable_schemas"]
    owner_writable = negotiation["writable_schema"]
    missing_capabilities = REQUIRED_SUPERVISOR_CAPABILITIES - negotiation["capabilities"]
    launcher_can_write = WRITABLE_SPOOL_SCHEMA in owner_readable
    launcher_can_read = owner_writable in READABLE_SPOOL_SCHEMAS

    problems = []
    if not protocol_compatible:
        problems.append(
            f"supervisor protocol owner_supported={owner_min}-{owner_max} "
            f"launcher_required={launcher_min}-{launcher_max}"
        )
    if not launcher_can_write:
        problems.append(
            f"owner_readable_spool_schemas={sorted(owner_readable)} "
            f"launcher_requires_writable_schema={WRITABLE_SPOOL_SCHEMA}"
        )
    if not launcher_can_read:
        problems.append(
            f"owner_writable_spool_schema={owner_writable} "
            f"launcher_readable_spool_schemas={list(READABLE_SPOOL_SCHEMAS)}"
        )
    if missing_capabilities:
        problems.append(
            f"owner_capabilities={sorted(negotiation['capabilities'])} "
            f"launcher_requires_capabilities={sorted(REQUIRED_SUPERVISOR_CAPABILITIES)}"
        )
    if problems:
        return (
            "active store supervisor capabilities are incompatible "
            f"({_compatibility_owner(record)}; {'; '.join(problems)})"
        )

    return None


def _supervisor_script_path() -> Path:
    """Locate the packaged detached supervisor entry point."""
    try:
        import spindle_supervisor
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import spindle_supervisor

    return Path(spindle_supervisor.__file__).resolve()


def _launch_store_supervisor(lock_fd: int) -> "subprocess.Popen":
    """Start the detached owner while transferring the already-held flock."""
    script = _supervisor_script_path()
    if not script.exists():
        raise FileNotFoundError(f"store supervisor entry point not found: {script}")
    child_env = os.environ.copy()
    child_env[SUPERVISOR_IMPORT_GUARD] = "1"
    log_path = _supervisor_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        return subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--store",
                str(SPINDLE_DIR.resolve()),
                "--lock-fd",
                str(lock_fd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(SPINDLE_DIR.parent),
            env=child_env,
            start_new_session=True,
            pass_fds=(lock_fd,),
        )


def _reap_supervisor_handle_later(proc: "subprocess.Popen", store: Path) -> None:
    """Reap an idle supervisor and reclaim after an unexpected death."""

    def wait_and_maybe_reclaim() -> None:
        try:
            proc.wait()
        except (ChildProcessError, OSError):
            return
        if SPINDLE_DIR.resolve() != store.resolve() or not _store_supervision_required():
            return
        try:
            _ensure_store_supervisor()
        except Exception:
            logger.exception("spindle: failed to reclaim store supervisor after child exit")

    threading.Thread(target=wait_and_maybe_reclaim, daemon=True).start()


def _store_supervision_required() -> bool:
    """Whether active work or a durable terminal duty still needs an owner."""
    if _count_running():
        return True
    from .owner_episode_convergence import discoverable_duties_outstanding

    for spool in _list_spools():
        try:
            if discoverable_duties_outstanding(spool.get("id", ""), spool):
                return True
        except Exception:
            # An unreadable record or mailbox is not evidence that its durable
            # duty disappeared. Keep ownership so a later pass can retry it.
            return True
    return False


def _ensure_store_supervisor_locked() -> tuple[bool, Optional[str]]:
    """Find a compatible owner or start one. Caller holds the control lock."""
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(_supervisor_lifetime_lock_path(), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            error = _supervisor_compatibility_error(_read_supervisor_record())
            return (error is None, f"Error: {error}" if error else None)

        try:
            proc = _launch_store_supervisor(lock_fd)
        except Exception as exc:
            return False, f"Error: Failed to start store supervisor: {exc}"

        # The inherited descriptor already makes the child the lifetime owner.
        # Publish compatibility before releasing the control lock so a waiting
        # launcher never observes a locked store with no handshake.
        _write_supervisor_record(_supervisor_identity(proc.pid))
        _reap_supervisor_handle_later(proc, SPINDLE_DIR)
        return True, None
    finally:
        os.close(lock_fd)


def _ensure_store_supervisor() -> tuple[bool, Optional[str]]:
    with _supervisor_control_lock():
        return _ensure_store_supervisor_locked()


def _live_owner_refuses_maintenance_locked() -> bool:
    """Compatibility probe for store maintenance. Caller holds the control lock.

    A held lifetime lock whose record fails negotiation, a held lock with no
    readable handshake, or a lifetime lock this process cannot open all refuse
    maintenance: ownership that cannot be assessed is not ours to clean.
    """
    try:
        lock_fd = os.open(_supervisor_lifetime_lock_path(), os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _supervisor_compatibility_error(_read_supervisor_record()) is not None
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(lock_fd)


def _live_owner_blocks_store_maintenance() -> bool:
    """True when a live store owner fails compatibility negotiation.

    Cleanup and orphan recovery rewrite and retire spool records. A process
    that would refuse an incompatible live owner must not mutate that owner's
    store; the rejection diagnostic has to reach the caller with the store
    untouched.
    """
    if not SPINDLE_DIR.is_dir():
        return False
    try:
        with _supervisor_control_lock():
            return _live_owner_refuses_maintenance_locked()
    except OSError:
        return True


def _run_store_maintenance(*, cleanup: bool = False) -> None:
    """Run cleanup and orphan recovery for callers that are not the store owner.

    The compatibility probe and the maintenance mutations share one continuous
    control-lock hold, so an incompatible owner cannot start between the probe
    and the pass. Monitor starts are deferred until the lock is released:
    _start_spool_monitor reacquires the control lock via
    _ensure_store_supervisor, and its negotiation independently refuses
    ownership of a store whose live owner is incompatible.
    """
    if not SPINDLE_DIR.is_dir():
        return
    control_lock = _supervisor_control_lock()
    try:
        control_lock.__enter__()
    except OSError:
        return
    try:
        if _live_owner_refuses_maintenance_locked():
            return
        if cleanup:
            _cleanup_old_spools()
        needs_monitor = _recovery_pass()
    finally:
        control_lock.__exit__(None, None, None)
    for spool_id in needs_monitor or ():
        _start_spool_monitor(spool_id)


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

    Every profile spawn path — the fresh spin, ``_respin_sync``, and
    ``spool_retry`` — routes through here so the alt endpoint, key, model, and
    extra args are rebuilt identically. Centralizing the reconstruction keeps
    a future spawn path from reading the deliberately secret-stripped persisted
    environment and silently using the default endpoint.

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


def _resolve_harness_selection(
    harness: Optional[str],
    model: Optional[str],
    caller_env: Optional[Dict[str, str]],
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, str]], Optional[list], Optional[str]]:
    """Resolve a caller's ``harness`` name against built-ins and lodged profiles.

    Shared by the ``spin`` MCP tool and the ``spindle spin`` CLI so both accept
    exactly the same harness names. Before this was shared, the CLI compared the
    name against codex/gemini/kimi and fell through to Claude Code for anything
    else — so ``spindle spin --harness mimo`` silently ran plain Claude Code
    with no profile env, and ``--harness typo`` ran a spool instead of erroring.

    Built-in names win over a same-named profile (with a warning). A non-built-in
    name must match a lodged profile. A resolved profile always routes through
    the Claude Code path (base harness is enforced to claude-code in v1) and
    contributes its spawn env, model, and extra CLI args.

    Returns ``(harness_lower, model, spawn_env, profile_extra_args, profile_name)``.
    Raises ``ValueError`` with a caller-facing message for an unknown name or a
    malformed profile.
    """
    harness_lower = harness.lower() if harness else None

    profile = None
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
                raise ValueError(
                    f"Unknown harness or profile: {harness!r}. Valid: {', '.join(valid)}. "
                    "Use spin_harnesses() to see details."
                )

    if profile is None:
        return harness_lower, model, caller_env, None, None

    # Reconstruct the child spawn env (carrying fresh secrets), effective model
    # (caller model wins / alias-mapped / profile default), and extra CLI args.
    # strict=True surfaces a malformed profile to the caller instead of silently
    # degrading. caller_env stays the only env safe to persist.
    profile_name = profile["_name"]
    spawn_env, model, profile_extra_args, _ = _profile_spawn_env(profile_name, caller_env, model=model, strict=True)
    return "claude-code", model, spawn_env, profile_extra_args, profile_name


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

    Pending reservations, running work, and structured stopping work all hold
    capacity until a terminal state is durably committed.
    """
    count = 0
    for spool in _list_spools():
        lock, liveness = _owner_episode_observation(spool)
        classification = classify_owner_episode(spool, lock, liveness)
        if classification.state != "retireable" or _foreign_episode_retirement(spool, classification, liveness):
            count += 1
    return count


def _next_owner_generation(spool_id: str) -> int:
    generations = [0]
    spool = _read_spool(spool_id) or {}
    try:
        generations.append(int(spool.get("owner_generation") or 0))
    except (TypeError, ValueError):
        pass
    try:
        identity = json.loads(_get_owner_identity_path(spool_id).read_text())
        generations.append(int(identity.get("owner_generation") or 0))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return max(generations) + 1


def _process_fact(pid: int) -> dict:
    birth = _process_start_time(pid)
    return {
        "pid": pid,
        "birth_token": str(birth) if birth is not None else "unavailable",
        "namespace": capture_pid_namespace().to_dict(),
    }


def _episode_process_identity(episode: dict, role: str) -> Optional[ProcessIdentity]:
    fact = episode.get(role)
    if not isinstance(fact, dict):
        return None
    lock = episode.get("lock") or {}
    try:
        return ProcessIdentity(
            pid=int(fact["pid"]),
            birth_token=str(fact["birth_token"]),
            namespace=NamespaceIdentity.from_dict(fact["namespace"]),
            owner_generation=int(episode["generation"]),
            child_pgid=None,
            lock_device=lock.get("device"),
            lock_inode=lock.get("inode"),
            lock_created=role == "owner",
        )
    except (KeyError, TypeError, ValueError):
        return None


def _owner_episode_observation(spool: dict) -> tuple[LockEvidence, LivenessEvidence]:
    episode = spool.get("owner_episode")
    if not isinstance(episode, dict):
        return LockEvidence("absent_legacy"), LivenessEvidence("dead", "legacy_status")
    phase = episode.get("phase")
    if phase == "reserved":
        # Once the launcher has published the detached watchdog, that process
        # owns the remaining pre-bind startup window.  The short-lived starter
        # is expected to exit, so prefer a watchdog proven alive in this PID
        # namespace.  An unverifiable/foreign watchdog does not erase positive
        # proof that the starter died before binding.
        watchdog = _episode_process_identity(episode, "watchdog")
        if watchdog is not None:
            watchdog_liveness = assess_process_liveness(watchdog)
            if watchdog_liveness.state == "unverifiable" and (
                watchdog_liveness.reason == "identity_mismatch" or not isinstance(spool.get("lifecycle"), dict)
            ):
                starter = _episode_process_identity(episode, "starter")
                if starter is not None:
                    starter_liveness = assess_process_liveness(starter)
                    if starter_liveness.state == "dead":
                        return LockEvidence("absent_legacy"), starter_liveness
            return LockEvidence("absent_legacy"), watchdog_liveness
        role = "starter"
    elif phase == "aborted":
        role = "starter"
    else:
        role = "owner"
    identity = _episode_process_identity(episode, role)
    if identity is None:
        liveness = LivenessEvidence("unverifiable", f"malformed_{role}_identity")
    else:
        liveness = assess_process_liveness(identity)
    if phase in {"reserved", "aborted"}:
        return LockEvidence("absent_legacy"), liveness
    if identity is None:
        return LockEvidence("identity_mismatch", detail="malformed_owner_identity"), liveness
    return probe_ownership_lock(_get_owner_lock_path(spool.get("id", "")), identity), liveness


def _foreign_episode_retirement(
    spool: dict,
    classification,
    liveness: LivenessEvidence,
) -> bool:
    """Refuse production retirement observed from outside the owner PID namespace."""
    episode_phase = (spool.get("owner_episode") or {}).get("phase")
    return (
        classification.state == "retireable"
        and classification.reason == "cleanup_and_release_proven"
        and liveness.reason == "namespace_mismatch"
        and episode_phase in {"cleanup_proven", "released"}
        # Pure primitive fixtures deliberately omit the integrated lifecycle
        # envelope; consumer authority applies to records published by the
        # production launcher/owner path.
        and isinstance(spool.get("lifecycle"), dict)
    )


def _ensure_spool_wall_deadline(spool: dict) -> Optional[str]:
    """Persist one absolute timeout budget before the owner is launched."""
    timeout = spool.get("timeout")
    if timeout == 0:
        timeout = None
        spool["timeout"] = None
    if timeout is None:
        spool.pop("wall_deadline_at", None)
        return None
    if spool.get("wall_deadline_at"):
        return spool["wall_deadline_at"]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=float(timeout))
    spool["wall_deadline_at"] = deadline.isoformat()
    return spool["wall_deadline_at"]


def _store_health_failure_text(item: dict) -> str:
    recorded = f"{item['recorded_device']}:{item['recorded_inode']}"
    observed = f"{item['observed_device']}:{item['observed_inode']}"
    episode_note = "; owner episode missing" if item["reason"] == "owner_identity_missing" else ""
    return f"{item['spool_id']}: {item['reason']}{episode_note} (recorded={recorded}, observed={observed})"


def _spools_idle() -> bool:
    """Finalize any finished-but-unmarked spools, then report whether the queue
    is empty (no running or pending spools). Uses gated maintenance so that both
    a dead-but-unmarked running spool and a stuck pending one (silent spawn
    failure, cleared after PENDING_SPAWN_TIMEOUT) are cleaned - otherwise either
    could hold the queue open and wedge a drain forever."""
    _run_store_maintenance()
    return _count_running() == 0


def _abandoned_custody_reason(spool: dict) -> Optional[str]:
    """Diagnose a bound episode after every protocol-authorized writer died.

    This is deliberately not cleanup or terminal evidence.  A released exact
    ownership inode plus affirmative same-namespace death of both the owner and
    watchdog proves only that nobody can advance the episode.  Provider or
    process-group observations cannot prove descendant cleanup and therefore
    do not participate in this diagnosis.
    """
    if spool.get("status") not in {"pending", "running"}:
        return None
    episode = spool.get("owner_episode")
    if not isinstance(episode, dict) or episode.get("phase") not in {"lock_bound", "accepted"}:
        return None
    # The public episode producers persist integer kernel coordinates, positive
    # integer PIDs, and Linux /proc start-time tokens as ASCII digit strings.
    # Do not let the permissive compatibility parser below coerce malformed JSON
    # values into an apparently exact identity: pidfd ESRCH is only affirmative
    # death after the durable identity itself is exact.
    lock_fact = episode.get("lock")
    if not isinstance(lock_fact, dict) or any(
        not isinstance(lock_fact.get(field), int) or isinstance(lock_fact.get(field), bool) or lock_fact.get(field) < 0
        for field in ("device", "inode")
    ):
        return None
    for role in ("owner", "watchdog"):
        fact = episode.get(role)
        if not isinstance(fact, dict):
            return None
        pid = fact.get("pid")
        birth_token = fact.get("birth_token")
        namespace = fact.get("namespace")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(birth_token, str)
            or not birth_token
            or not birth_token.isascii()
            or not birth_token.isdigit()
            or not isinstance(namespace, dict)
            or namespace.get("status") != "supported"
            or any(
                not isinstance(namespace.get(field), int)
                or isinstance(namespace.get(field), bool)
                or namespace.get(field) < 0
                for field in ("device", "inode")
            )
        ):
            return None
    lock, owner_liveness = _owner_episode_observation(spool)
    classification = classify_owner_episode(spool, lock, owner_liveness)
    if classification.reason != "released_without_cleanup_proof" or owner_liveness.state != "dead":
        return None
    watchdog = _episode_process_identity(episode, "watchdog")
    if watchdog is None or assess_process_liveness(watchdog).state != "dead":
        return None
    return "custody_abandoned_without_cleanup_proof"


def _serialized_abandoned_custody_reason(spool_id: str) -> Optional[str]:
    """Diagnose one fresh record snapshot serialized against episode writers."""
    # A busy writer is progress, not evidence of abandonment.  Callers either
    # retry on the next drain poll or render the ordinary running diagnostic.
    with _spool_lock(spool_id, blocking=False) as acquired:
        if not acquired:
            return None
        spool = _read_spool(spool_id)
        return _abandoned_custody_reason(spool) if spool is not None else None


def _drain_blockers() -> list[DrainBlocker]:
    """Return non-progressing records which make an unforced drain impossible."""
    blockers = []
    for observed in _list_spools():
        spool_id = observed.get("id")
        episode = observed.get("owner_episode")
        if (
            not spool_id
            or observed.get("status") not in {"pending", "running"}
            or not isinstance(episode, dict)
            or episode.get("phase") not in {"lock_bound", "accepted"}
        ):
            continue
        # The list scan may be stale by the time liveness is probed (a watchdog
        # can publish cleanup and exit in between), so re-read under the record
        # lock shared by every episode writer.
        reason = _serialized_abandoned_custody_reason(spool_id)
        if reason:
            blockers.append(DrainBlocker(str(spool_id), reason))
    return blockers


def _wait_until_idle(poll_interval: float = RELOAD_DRAIN_POLL_INTERVAL) -> None:
    """Block until no spools are running or pending. New spins are allowed during
    the wait, so this returns at the next moment the queue happens to be empty."""
    while not _spools_idle():
        blockers = _drain_blockers()
        if blockers:
            raise DrainBlockedError(blockers)
        time.sleep(poll_interval)


def _try_reserve_slot_and_create(
    spool_id: str,
    initial_status: str = "pending",
    reservation_metadata: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
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
    # The owner handshake and minimal reservation share the short control lock.
    # Idle retirement takes the same lock and rescans before exiting, so it
    # cannot miss a reservation that a launcher is about to publish.
    with _supervisor_control_lock():
        compatible, error = _ensure_store_supervisor_locked()
        if not compatible:
            return False, error
        with _concurrency_lock():
            unhealthy = _store_health_failures()
            if unhealthy:
                detail = "; ".join(_store_health_failure_text(item) for item in unhealthy)
                return False, f"Error: Spool store unhealthy; repair ownership artifacts before launch ({detail})"
            running_count = _count_running()
            if running_count >= MAX_CONCURRENT:
                return False, f"Error: Max {MAX_CONCURRENT} concurrent spools. Wait for some to complete."

            spool = {
                "id": spool_id,
                "status": initial_status,
                "created_at": datetime.now().isoformat(),
                "spool_schema_version": SPOOL_SCHEMA_VERSION,
                "launcher_pid": os.getpid(),
            }
            launcher_start_time = _process_start_time(os.getpid())
            if launcher_start_time is not None:
                spool["launcher_start_time"] = launcher_start_time
            spool["launcher_namespace"] = capture_pid_namespace().to_dict()
            if reservation_metadata:
                spool.update(reservation_metadata)
            deadline = _ensure_spool_wall_deadline(spool)
            facts = {"starter": _process_fact(os.getpid())}
            if deadline is not None:
                facts["deadline"] = deadline
            reserved = transition_owner_episode(
                SPINDLE_DIR,
                spool_id,
                actor="launcher",
                destination="reserved",
                generation=1,
                expected_revision=None,
                facts=facts,
                record_updates=spool,
            )
            if not reserved.accepted:
                return False, f"Error: Could not reserve owner episode for {spool_id}: {reserved.rejection}"

            return True, None


def _prepare_pending_spool_for_spawn(spool: dict) -> bool:
    """Publish launch metadata unless recovery already finalized the reservation."""
    spool_id = spool["id"]
    with _spool_lock(spool_id) as acquired:
        if not acquired:
            return False
        current = _read_spool(spool_id)
        if current and current.get("status") == "pending":
            prepared = dict(current)
            for key, value in spool.items():
                if key != "owner_episode":
                    prepared[key] = value
            prepared.setdefault(
                "spool_schema_version",
                current.get("spool_schema_version", SPOOL_SCHEMA_VERSION),
            )
            for key in ("launcher_pid", "launcher_start_time", "launcher_namespace"):
                if key in current:
                    prepared.setdefault(key, current[key])
            episode = prepared.get("owner_episode") or {}
            if episode:
                prepared["owner_generation"] = episode.get("generation")
                deadline = episode.get("deadline")
                if deadline is not None:
                    prepared["wall_deadline_at"] = deadline
            _write_spool(spool_id, prepared)
            return True

        # Setup may have created a shard before stale-reservation recovery won.
        # Attach that recovery information to the terminal record instead of
        # losing the only durable handle to the worktree.
        if current and spool.get("shard_created_by_spool"):
            for key in (
                "working_dir",
                "shard",
                "shard_created_by_spool",
                "shard_source_dir",
                "base_branch",
                "harness",
            ):
                if key in spool:
                    current[key] = spool[key]
            _preserve_failed_spool_shard(current)
            _write_spool(spool_id, current)
        return False


def _record_pre_spawn_failure(
    spool_id: str,
    error: str,
    spool_metadata: Optional[dict] = None,
) -> None:
    """Publish launcher failure evidence, then let convergence project it."""
    with _spool_lock(spool_id) as acquired:
        if not acquired:
            return
        current = _read_spool(spool_id)
        if not current:
            return
        if spool_metadata:
            for key in (
                "working_dir",
                "shard",
                "shard_created_by_spool",
                "shard_source_dir",
                "base_branch",
                "harness",
            ):
                if key in spool_metadata:
                    current[key] = spool_metadata[key]
        current.pop("launcher_pid", None)
        current.pop("launcher_start_time", None)
        _preserve_failed_spool_shard(current)
        _write_spool(spool_id, current)
        episode = current.get("owner_episode") or {}
        if current.get("status") == "pending" and episode.get("phase") == "reserved":
            transition_owner_episode(
                SPINDLE_DIR,
                spool_id,
                actor="launcher",
                destination="aborted",
                generation=episode.get("generation"),
                expected_revision=episode.get("revision"),
                facts={
                    "failure": {
                        "kind": "launcher_pre_spawn_failure",
                        "detail": error,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
                record_locked=True,
            )
    current = _read_spool(spool_id)
    if current and "owner_episode" in current:
        from .owner_episode_convergence import ObserverIdentity, converge_owner_episode

        converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    elif current and current.get("status") == "pending":
        from .owner_episode_convergence import publish_record_updates

        publish_record_updates(
            spool_id,
            current,
            {"status": "error", "error": error, "completed_at": datetime.now().isoformat()},
        )


def _publish_spawned_process(spool_id: str, pid: int, *, record_locked: bool = False) -> bool:
    """Publish the watchdog PID, then release it to create logical-owner identity.

    ``record_locked`` is reserved for callers already holding ``_spool_lock``;
    both the episode transition and its flat compatibility mirrors then reuse
    that acquisition instead of recursively flocking the same record.
    """
    published = False
    try:
        guard = nullcontext(True) if record_locked else _spool_lock(spool_id)
        with guard as acquired:
            current = _read_spool(spool_id) if acquired else None
            episode = (current or {}).get("owner_episode") or {}
            launch_birth = _process_start_time(pid)
            launch_fact = {
                "pid": pid,
                "birth_token": str(launch_birth) if launch_birth is not None else "unavailable",
                "namespace": capture_pid_namespace().to_dict(),
            }
            if current and current.get("status") in {"pending", "running"} and episode.get("phase") == "reserved":
                published_episode = transition_owner_episode(
                    SPINDLE_DIR,
                    spool_id,
                    actor="launcher",
                    destination="reserved",
                    generation=episode.get("generation"),
                    expected_revision=episode.get("revision"),
                    facts={"watchdog": launch_fact},
                    record_locked=True,
                )
                if published_episode.accepted:
                    current = _read_spool(spool_id)
                    if not current or current.get("owner_episode", {}).get("generation") != episode.get("generation"):
                        return False
                    current["watchdog_pid"] = pid
                    current["watchdog_start_time"] = launch_fact["birth_token"]
                    current["watchdog_namespace"] = launch_fact["namespace"]
                    current.pop("launcher_pid", None)
                    current.pop("launcher_start_time", None)
                    current.pop("launcher_namespace", None)
                    _write_spool(spool_id, current)
                    published = True
                    return True
        # Recovery won before publication. Closing the barrier makes the
        # packaged owner exit without ever spawning the provider.
        _pop_and_reap_process_handle(spool_id)
        return False
    finally:
        _finish_spawn_barrier(spool_id, start=published)


def _start_spool_process(
    spool: dict,
    cmd: list,
    cwd: str,
    env: Optional[Dict[str, str]],
    stdin_path: Optional[Path] = None,
) -> Optional[str]:
    """Prepare, spawn, and publish a process while its shard worktree is locked."""
    spool_id = spool["id"]
    worktree_path = _spool_worktree_path(spool)
    with _worktree_lock(worktree_path) as acquired:
        if not acquired:
            return f"Error: Could not lock shard worktree for spool {spool_id} startup"
        if not _prepare_pending_spool_for_spawn(spool):
            return f"Error: Spool {spool_id} was finalized before process startup completed"
        try:
            # Pass stdin_path only when set so tests that fake _spawn_detached
            # with the historical 4-arg signature keep working.
            if stdin_path is not None:
                pid = _spawn_detached(spool_id, cmd, cwd, env, stdin_path=stdin_path)
            else:
                pid = _spawn_detached(spool_id, cmd, cwd, env)
        except Exception as exc:
            _record_pre_spawn_failure(spool_id, f"spawn failed: {exc}", spool)
            return f"Error: Failed to spawn process: {exc}"
        if not _publish_spawned_process(spool_id, pid):
            return f"Error: Spool {spool_id} was finalized before process startup completed"
    return None


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
    if kind == "headless_background_wait":
        pending = spool.get("pending_background_tasks") or []
        ids = ", ".join(str(t.get("id", "?")) for t in pending if isinstance(t, dict)) or "unknown"
        return (
            f"Spool {spool_id} PARKED (headless background wait): the agent ended its "
            f"turn still waiting on background task(s) [{ids}] whose completion "
            f"notification can never reach a finished headless process. The stored "
            f"result is the agent's parked stub, NOT a completed answer. "
            f"respin({spool_id!r}, ...) rebuilds a clean continuation session from the "
            f"transcript with those tasks abandoned. Original message:\n{err}"
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


def _process_start_time(pid: int) -> Optional[str]:
    """Read Linux's non-repeating process birth token for PID reuse protection."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except (FileNotFoundError, OSError):
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _read_current_owner_identity(spool_id: str) -> Optional[ProcessIdentity]:
    try:
        return ProcessIdentity.from_dict(json.loads(_get_owner_identity_path(spool_id).read_text()))
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _owner_exit_evidence(spool_id: str, owner_generation: int) -> tuple[bool, bool]:
    """Return generation-matched exit and cleanup evidence for one owner."""
    try:
        evidence = json.loads(_get_owner_exit_path(spool_id).read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return False, False
    if evidence.get("owner_generation") != owner_generation:
        return False, False
    provider_reaped = bool(evidence.get("provider_reaped"))
    cleanup_complete = provider_reaped and bool(evidence.get("cleanup_outcome"))
    return provider_reaped, cleanup_complete


def _reconcile_spool_ownership(spool: dict) -> ReconciliationResult:
    """Return the one lifecycle authority used by every PID-sensitive caller."""
    spool_id = spool.get("id")
    missing = LockEvidence("absent_legacy", detail="no_recorded_current_owner")

    if "owner_episode" in spool:
        from .owner_episode_convergence import ObserverIdentity, classify_owner_episode_record

        meaning = classify_owner_episode_record(spool, ObserverIdentity.for_this_process())
        state = (
            "unverifiable"
            if meaning.classification == "terminalizable" and not meaning.may_mutate
            else meaning.classification
        )
        return ReconciliationResult(state, meaning.reason, meaning.liveness, meaning.lock)

    if spool.get("status") == "pending":
        launcher_pid = spool.get("launcher_pid") or spool.get("watchdog_pid")
        launcher_birth = spool.get("launcher_start_time") or spool.get("watchdog_start_time")
        launcher_namespace = spool.get("launcher_namespace") or spool.get("watchdog_namespace")
        if not launcher_pid:
            return ReconciliationResult(
                "terminalizable",
                "pending_provider_never_started",
                LivenessEvidence("dead", "no_launcher_pid"),
                missing,
            )
        if launcher_birth is None or not isinstance(launcher_namespace, dict):
            return ReconciliationResult(
                "unverifiable",
                "pending_launcher_identity_incomplete",
                LivenessEvidence("unverifiable", "launcher_identity_incomplete"),
                missing,
            )
        identity = ProcessIdentity(
            pid=int(launcher_pid),
            birth_token=str(launcher_birth),
            namespace=NamespaceIdentity.from_dict(launcher_namespace),
            owner_generation=0,
            child_pgid=None,
            lock_device=None,
            lock_inode=None,
            lock_created=False,
        )
        liveness = assess_process_liveness(identity)
        state = (
            "active" if liveness.state == "alive" else "terminalizable" if liveness.state == "dead" else "unverifiable"
        )
        role = "watchdog" if spool.get("watchdog_pid") else "launcher"
        return ReconciliationResult(state, f"pending_{role}_{liveness.reason}", liveness, missing)

    if spool_id:
        identity_path = _get_owner_identity_path(spool_id)
        identity = _read_current_owner_identity(spool_id)
        if identity is not None:
            lock = probe_ownership_lock(_get_owner_lock_path(spool_id), identity)
            liveness = assess_process_liveness(identity)
            exit_evidence, cleanup_evidence = _owner_exit_evidence(
                spool_id,
                identity.owner_generation,
            )
            return reconcile_owner_episode(
                lock,
                liveness,
                exit_evidence=exit_evidence,
                cleanup_evidence=cleanup_evidence,
                stopping=(spool.get("lifecycle") or {}).get("public_stop_state") == "stopping",
            )
        if identity_path.exists():
            return ReconciliationResult(
                "store_unhealthy",
                "owner_identity_unreadable",
                LivenessEvidence("unverifiable", "owner_identity_unreadable"),
                LockEvidence("unreadable", detail="owner_identity_unreadable"),
            )
        lifecycle = spool.get("lifecycle") or {}
        current_owner_fields = any(
            value is not None
            for value in (
                spool.get("owner_generation"),
                spool.get("owner_pid"),
                spool.get("provider_pid"),
                spool.get("watchdog_pid"),
                lifecycle.get("ownership_state"),
                lifecycle.get("transport_state"),
            )
        )
        if spool.get("status") == "running" and current_owner_fields:
            return ReconciliationResult(
                "store_unhealthy",
                "owner_identity_missing",
                LivenessEvidence("unverifiable", "owner_identity_missing"),
                LockEvidence("identity_mismatch", detail="owner_identity_missing"),
            )

    if spool.get("status") not in {"pending", "running"} and not spool.get("process_group_cleanup_warning"):
        return ReconciliationResult(
            "terminalizable",
            "terminal_legacy_record",
            LivenessEvidence("dead", "terminal_record"),
            missing,
        )

    # Compatibility for an owner launched by this exact process before the
    # logical-owner bridge. A namespace match alone is never authority.
    proc = _PROC_HANDLES.get(spool_id)
    if proc is not None and getattr(proc, "pid", None) == spool.get("pid"):
        exit_code = proc.poll()
        liveness = (
            LivenessEvidence("alive", "recorded_local_handle")
            if exit_code is None
            else LivenessEvidence("dead", "recorded_local_handle_exited")
        )
        lock = LockEvidence("held" if _process_identity_lock_is_held(spool_id) else "absent_legacy")
        if lock.state == "held":
            return ReconciliationResult("active", "legacy_local_owner_lock_held", liveness, lock)
        legacy = LegacyAuthority(recorded="local-process-handle", observer="local-process-handle")
        return reconcile_owner_episode(
            lock,
            liveness,
            exit_evidence=_get_exit_path(spool_id).exists() or exit_code is not None,
            legacy_authority=legacy,
        )

    return ReconciliationResult(
        "unverifiable",
        "legacy_authority_unproven",
        LivenessEvidence("unverifiable", "legacy_authority_unproven"),
        missing,
    )


def _store_health_failures() -> list[dict]:
    """Return every ownership defect which makes new store mutation unsafe."""
    failures = []
    for spool in _list_spools():
        spool_id = spool.get("id")
        if not spool_id:
            continue
        if (
            spool.get("status") not in {"pending", "running"}
            and "owner_episode" not in spool
            and not _get_owner_identity_path(spool_id).exists()
        ):
            continue
        result = _reconcile_spool_ownership(spool)
        if result.state != "store_unhealthy":
            continue
        identity = _episode_process_identity(spool.get("owner_episode") or {}, "owner") or _read_current_owner_identity(
            spool_id
        )
        failures.append(
            {
                "spool_id": spool_id,
                "reason": result.reason,
                "lock_state": result.lock.state,
                "detail": result.lock.detail,
                "recorded_device": identity.lock_device if identity is not None else None,
                "recorded_inode": identity.lock_inode if identity is not None else None,
                "observed_device": result.lock.observed_device,
                "observed_inode": result.lock.observed_inode,
            }
        )
    return failures


def _request_owner_stop_locked(spool: dict, kind: str, requested_by: str) -> tuple[Optional[object], Optional[str]]:
    """Publish a mailbox request while the caller holds the spool lock."""
    result = _reconcile_spool_ownership(spool)
    if result.state == "store_unhealthy":
        return None, f"store unhealthy: {result.reason}"
    if result.state == "unverifiable":
        return None, f"ownership unverifiable: {result.reason}"
    episode = spool.get("owner_episode") or {}
    if episode:
        if episode.get("phase") != "accepted":
            return None, f"owner episode is not accepting control ({episode.get('phase') or 'missing'})"
        if result.lock.state != "held":
            return None, f"owner does not hold the exact recorded inode ({result.lock.state})"
        generation = episode.get("generation")
        deadline = episode.get("deadline")
    else:
        # Compatibility for internal/direct owners created before the episode
        # protocol. Public current launches always carry an episode and cannot
        # reach this branch.
        generation = spool.get("owner_generation")
        deadline = spool.get("wall_deadline_at")
    if not generation:
        return None, "logical owner generation is not published"
    request = create_control_request(
        SPINDLE_DIR,
        spool["id"],
        kind,
        generation,
        requested_by,
        observer_namespace=capture_pid_namespace(),
        reason=f"{kind} requested by {requested_by}",
        deadline=deadline if kind == "timeout" else None,
        mailbox_locked=True,
    )
    lifecycle = dict(spool.get("lifecycle") or {})
    lifecycle.update(
        {
            "public_stop_state": "stopping",
            "desired_terminal_kind": request.desired_terminal_kind,
            "control_request_id": request.request_id,
        }
    )
    spool["lifecycle"] = lifecycle
    _write_spool(spool["id"], spool)
    return request, None


def _request_owner_stop(spool_id: str, kind: str, requested_by: str) -> tuple[Optional[object], Optional[str]]:
    """Take the fixed mailbox-then-record order for public admission."""
    with mailbox_guard(SPINDLE_DIR, spool_id):
        with _spool_lock(spool_id) as acquired:
            if not acquired:
                return None, "spool record lock unavailable"
            spool = _read_spool(spool_id)
            if not spool:
                return None, "spool record missing"
            return _request_owner_stop_locked(spool, kind, requested_by)


def _spool_blocks_destructive_action(spool: dict) -> bool:
    if "owner_episode" in spool:
        return _reconcile_spool_ownership(spool).state != "terminalizable"
    if spool.get("status") in {"pending", "running"}:
        return True
    if _get_owner_identity_path(spool.get("id", "")).exists() or spool.get("process_group_cleanup_warning"):
        return _reconcile_spool_ownership(spool).state != "terminalizable"
    return False


def _spool_process_identity_matches(spool: dict) -> bool:
    """Prove that a stored PID still names the process Spindle launched."""
    pid = spool.get("pid")
    if not pid:
        return False
    expected_start_time = spool.get("process_start_time")
    if expected_start_time is not None:
        return _process_start_time(pid) == str(expected_start_time)
    if _process_identity_lock_is_held(spool.get("id")):
        return _is_pid_alive(pid)
    proc = _PROC_HANDLES.get(spool.get("id"))
    return proc is not None and getattr(proc, "pid", None) == pid and proc.poll() is None


def _process_identity_lock_is_held(spool_id: Optional[str]) -> bool:
    """Whether the original detached wrapper still owns its portable lock."""
    if not spool_id:
        return False
    path = _get_process_identity_path(spool_id)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _spool_process_group_identity_matches(spool: dict) -> bool:
    """Prove a live group still belongs to the spool before signaling it.

    A detached leader can exit while descendants keep its process-group ID
    reserved. In that case the missing leader is safe: Linux cannot reuse its
    PID as a new group leader until the old group disappears. A live leader
    must still match its recorded birth token (or a live local Popen handle).
    """
    pid = spool.get("pid")
    if not pid:
        return False
    expected_start_time = spool.get("process_start_time")
    if expected_start_time is not None:
        current_start_time = _process_start_time(pid)
        if current_start_time is not None:
            return current_start_time == str(expected_start_time)
        return not _is_pid_alive(pid) and _is_process_group_alive(pid)
    if _process_identity_lock_is_held(spool.get("id")):
        return _is_pid_alive(pid)
    if not _is_pid_alive(pid) and _is_process_group_alive(pid):
        # The old group keeps its numeric ID reserved after its leader exits,
        # so no unrelated process can yet reuse that PID as a new group leader.
        return True
    return _spool_process_identity_matches(spool)


def _spool_process_group_is_alive(spool: dict) -> bool:
    """Whether a spool's recorded leader or any member of its group is alive."""
    pid = spool.get("pid")
    return bool(pid) and (_is_pid_alive(pid) or _is_process_group_alive(pid))


def _resolve_spool_process_group(spool: dict, grace_seconds: float) -> str:
    """Safely drain a spool group without ever signaling a reused PID.

    Returns ``gone``, ``terminated``, ``unverifiable``, or ``survived``.
    Liveness is checked before identity so a warning can be retired after the
    original group exits. Identity is mandatory immediately before signaling.
    """
    if not _spool_process_group_is_alive(spool):
        return "gone"
    if not _spool_process_group_identity_matches(spool):
        return "unverifiable"
    if _terminate_process_group(
        spool["pid"],
        grace_seconds,
        identity_check=lambda: _spool_process_group_identity_matches(spool),
    ):
        return "terminated"
    if not _spool_process_group_identity_matches(spool):
        return "unverifiable"
    return "survived"


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running (not a zombie)."""
    try:
        os.kill(pid, 0)  # Doesn't kill, just checks existence
    except (OSError, ProcessLookupError):
        return False

    # os.kill(pid, 0) succeeds for zombie processes too. On Linux, inspect
    # /proc without waitpid: reaping here can steal the real return code from a
    # Popen handle. Without /proc, the successful kill probe is authoritative.
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return True
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    fields = stat[stat.rfind(")") + 2 :].split()
    if fields:
        return fields[0] != "Z"

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


def _terminate_process_group(
    process_group_id: int,
    grace_seconds: float,
    identity_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Terminate a detached spool group, revalidating identity before signals."""
    if identity_check is not None and not identity_check():
        return False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        if identity_check is not None and not identity_check():
            return False
        try:
            os.kill(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return not _is_process_group_alive(process_group_id)
    time.sleep(grace_seconds)
    if not _is_process_group_alive(process_group_id):
        return True
    if identity_check is not None and not identity_check():
        return False
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        if identity_check is not None and not identity_check():
            return False
        try:
            os.kill(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    # Give adopted descendants a brief chance to disappear before reporting
    # failure. Popen.poll()/wait() owns reaping the group leader.
    time.sleep(0.1)
    return not _is_process_group_alive(process_group_id)


def _reap_process_handle_later(proc: "subprocess.Popen") -> None:
    """Reap a still-live detached wrapper without blocking a terminal transition."""

    def wait_for_exit() -> None:
        try:
            proc.wait()
        except (ChildProcessError, OSError):
            pass

    threading.Thread(target=wait_for_exit, daemon=True).start()


_UNOBSERVED_PROCESS_EXIT = object()


def _pop_and_reap_process_handle(spool_id: str, *, observed_exit=_UNOBSERVED_PROCESS_EXIT) -> Optional[int]:
    """Remove a stored process handle and ensure its child is eventually reaped."""
    proc = _PROC_HANDLES.get(spool_id)
    if proc is None:
        return None
    exit_code = proc.poll() if observed_exit is _UNOBSERVED_PROCESS_EXIT else observed_exit
    # Keep the only process-local reminder if polling raises; convergence will
    # report the local error and retry this duty on its next observation.
    _PROC_HANDLES.pop(spool_id, None)
    if exit_code is None:
        _reap_process_handle_later(proc)
    return exit_code


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
        if path.name.startswith("."):
            continue
        spool_id = path.stem
        try:
            # Read and decide only after taking the terminal lock. Otherwise a
            # finalizer can publish a shard/capture preservation marker after
            # this sweep's read but before its unlink.
            with _spool_lock(spool_id, blocking=False) as acquired:
                if not acquired:
                    continue
                with open(path) as f:
                    data = json.load(f)
                if data.get("id", spool_id) != spool_id:
                    continue
                # Startup recovery owns classification of live reservations.
                # Never delete them merely because their creation timestamp is
                # old; recovery may need to finalize them or preserve a shard.
                if data.get("status") in {"pending", "running"}:
                    _reconcile_spool_ownership(data)
                    continue
                # A preserved shard is intentionally recoverable through its
                # spool handle until explicit merge/abandon resolves it.
                if data.get("shard_cleanup_preserved"):
                    continue
                # A terminal spool may still have an unsignalable process
                # holding its capture descriptors. Keep both until the warned-
                # about process group is verifiably gone.
                if data.get("process_group_cleanup_warning"):
                    if _reconcile_spool_ownership(data).state != "terminalizable":
                        continue
                created = datetime.fromisoformat(data.get("created_at", ""))
                if created >= cutoff:
                    continue
                owner_identity = _read_current_owner_identity(spool_id)
                if "owner_episode" in data:
                    if _reconcile_spool_ownership(data).state != "terminalizable":
                        continue
                    # Retirement deletes the record, the mailbox and every
                    # capture with it, so it consumes convergence rather than
                    # terminality: a record whose obligation block still names
                    # owed work is the only place that work is replayable from,
                    # and startup recovery has not run yet on a fresh process.
                    # A record that owes nothing - including a pre-convergence
                    # terminal that never carried an obligation block - retires
                    # exactly as it did before.
                    from .owner_episode_convergence import discoverable_duties_outstanding

                    if discoverable_duties_outstanding(spool_id, data):
                        continue
                    episode_identity = _episode_process_identity(data.get("owner_episode") or {}, "owner")
                    retire_owner_artifacts(SPINDLE_DIR, spool_id, episode_identity)
                    continue
                if owner_identity is not None:
                    if _reconcile_spool_ownership(data).state != "terminalizable":
                        continue
                    retire_owner_artifacts(SPINDLE_DIR, spool_id, owner_identity)
                    continue
                path.unlink()
                # Also clean up output, lock, and transcript files
                stdout_path = _get_output_path(spool_id)
                stderr_path = _get_stderr_path(spool_id)
                exit_path = _get_exit_path(spool_id)
                process_identity_path = _get_process_identity_path(spool_id)
                lock_path = _get_lock_path(spool_id)
                if stdout_path.exists():
                    stdout_path.unlink()
                if stderr_path.exists():
                    stderr_path.unlink()
                if exit_path.exists():
                    exit_path.unlink()
                if process_identity_path.exists():
                    process_identity_path.unlink()
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


def _spool_complete_output_path(spool: dict, stdout_path: Path, stderr_path: Path) -> Optional[Path]:
    """Return the capture file containing a non-Codex terminal JSON object.

    Stream-driver spools are terminal only at the driver's own sentinel event.
    An intermediate result event is NOT terminal there: the driver holds the
    Claude process open across end-of-turn results while background tasks are
    unresolved, so finalizing on a result would recreate the parked-stub false
    completion the driver exists to prevent.
    """
    if spool.get("harness") != "codex" and stdout_path.exists():
        try:
            content = stdout_path.read_text()
            if content.strip():
                if spool.get("claude_protocol") == CLAUDE_PROTOCOL_STREAM_V1:
                    if _claude_driver.ndjson_has_sentinel(content):
                        return stdout_path
                    return None
                data = json.loads(content)
                result = _extract_cc_result(data)
                if result and ("result" in result or "error" in result or "response" in result):
                    return stdout_path
        except (IOError, json.JSONDecodeError):
            pass

    if spool.get("harness") == "gemini" and stderr_path.exists():
        try:
            stderr_content = stderr_path.read_text()
            if stderr_content.strip():
                parsed = _extract_last_json_object(stderr_content)
                if parsed and ("error" in parsed or "session_id" in parsed):
                    return stderr_path
        except IOError:
            pass
    return None


def _spool_has_complete_output(spool: dict, stdout_path: Path, stderr_path: Path) -> bool:
    """Whether a non-Codex harness has published its terminal JSON object."""
    return _spool_complete_output_path(spool, stdout_path, stderr_path) is not None


def _settle_recovered_episode_requests(spool_id: str, episode: dict) -> None:
    """Finish generation-scoped provenance after the winning owner is gone.

    The caller holds the spool lock. Public admission must acquire that same
    lock before publishing, so the request set cannot grow underneath this
    pass; taking the mailbox lock here would invert the mailbox-then-spool
    order used by admission.
    """
    if episode.get("phase") != "released":
        return
    winning_request = episode.get("winning_request") or {}
    winning_request_id = winning_request.get("request_id")
    generation = episode.get("generation")
    if not winning_request_id or not generation:
        return
    for request in iter_control_requests(SPINDLE_DIR, spool_id):
        if request.request_id == winning_request_id:
            continue
        try:
            receipt = read_control_receipt(SPINDLE_DIR, spool_id, request.request_id)
        except MalformedControlReceipt:
            continue
        if receipt is not None:
            continue
        write_control_receipt(
            SPINDLE_DIR,
            spool_id,
            request,
            current_generation=generation,
            accepted=False,
        )


def _post_terminal_bookkeeping(spool_id: str, spool: dict) -> None:
    """Finish idempotent bookkeeping after durable terminal projection.

    The caller holds the spool lock and has already published all terminal
    facts.  This helper may add only failed-shard recovery metadata; provider
    parsing, transcripts, and lifecycle projection remain the caller's work.
    An already preserved shard causes no second record write. Process-handle
    observation belongs to the explicit convergence observer (or the legacy
    caller) and therefore runs separately after this helper.
    """
    if _preserve_failed_spool_shard(spool):
        _write_spool(spool_id, spool)


def _check_and_finalize_spool(spool_id: str) -> bool:
    """Delegate owner-episode meaning and terminal publication to convergence."""
    spool = _read_spool(spool_id)
    if spool is None:
        return True
    if "owner_episode" not in spool:
        # Legacy pending reservations were historically treated as already
        # non-running by this poll helper; only a running legacy record owns a
        # provider result that may be parsed here.
        if spool.get("status") != "running":
            return True
        # The legacy finalizer reads the captures, parses them and writes the
        # record; _write_spool renames through one fixed .tmp name per spool and
        # _post_terminal_bookkeeping documents that its caller holds this lock.
        # Two unserialized finalizers therefore race on that name and can each
        # publish from a snapshot the other has already superseded. The episode
        # path below takes the same lock inside convergence, so this is the only
        # branch that needs it - and it is taken non-blocking, as it was before
        # convergence, so a caller that already holds it never deadlocks and a
        # concurrent finalizer is simply reported as still running.
        with _spool_lock(spool_id, blocking=False) as acquired:
            if not acquired:
                return False
            current = _read_spool(spool_id)
            if current is None:
                return True
            if "owner_episode" in current:
                # An owner published an episode between the unlocked read and
                # this lock; convergence owns the record from the next pass.
                return False
            if current.get("status") != "running":
                return True  # another finalizer won the race
            reconciliation = _reconcile_spool_ownership(current)
            if reconciliation.state != "terminalizable":
                return False
            from .owner_episode_convergence import finalize_legacy_spool

            return finalize_legacy_spool(spool_id, current)
    from .owner_episode_convergence import (
        ObserverIdentity,
        converge_owner_episode,
        has_published_terminal,
        obligations_outstanding,
    )

    # The poll hot path must not reacquire blocking capture guards after this
    # applicator has reached its durable fixed point.  Status-only mixed-version
    # records still need discovery, so only an applicator-owned terminal with no
    # outstanding materialized duty qualifies.
    if (
        spool.get("terminal_origin")
        and has_published_terminal(spool)
        and not obligations_outstanding(spool)
        and spool_id not in _PROC_HANDLES
    ):
        return True

    result = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    return result.terminal_state in {"terminal_with_obligations_pending", "fully_converged"}


def _recover_deterministic_shard(current: dict) -> None:
    """Attach a plain-git or SKEIN-named shard from a minimal reservation."""
    if current.get("shard") or not current.get("shard_requested"):
        return
    source = current.get("launch_working_dir")
    if not source:
        return
    worktree = Path(source) / "worktrees" / current["id"]
    if worktree.exists():
        shard_info = {
            "worktree_path": str(worktree),
            "branch_name": f"shard-{current['id']}",
            "shard_id": current["id"],
        }
    else:
        # SKEIN names a shard `{agent}-{YYYYMMDD}-{seq}` even though Spindle
        # supplies the spool id as `--agent`. Query Git directly so recovery
        # does not depend on SKEIN's server or SQLite metadata surviving the
        # launcher. Exact agent and branch matching avoids adopting another
        # spool's worktree; max() selects the newest zero-padded sequence if a
        # previous crashed attempt with the same id also remains.
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=source,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return
        if result.returncode != 0:
            return

        candidates = []
        candidate_path = None
        candidate_branch = None
        for line in [*result.stdout.splitlines(), ""]:
            if line.startswith("worktree "):
                if candidate_path is not None:
                    candidates.append((candidate_path, candidate_branch))
                candidate_path = Path(line.split(" ", 1)[1])
                candidate_branch = None
            elif line.startswith("branch "):
                candidate_branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
            elif not line and candidate_path is not None:
                candidates.append((candidate_path, candidate_branch))
                candidate_path = None
                candidate_branch = None

        name_pattern = re.compile(rf"^{re.escape(current['id'])}-\d{{8}}-\d{{3}}$")
        matching = [
            (path, branch)
            for path, branch in candidates
            if name_pattern.fullmatch(path.name) and branch == f"shard-{path.name}" and path.exists()
        ]
        if not matching:
            return
        worktree, branch_name = max(matching, key=lambda item: item[0].name)
        shard_info = {
            "worktree_path": str(worktree),
            "branch_name": branch_name,
            "shard_id": worktree.name,
        }

    if not worktree.exists():
        return
    current.update(
        {
            "working_dir": str(worktree),
            "shard": shard_info,
            "shard_created_by_spool": True,
            "shard_source_dir": source,
        }
    )


def _reconcile_pending_spool(spool_id: str) -> bool:
    """Run one bounded stale-reservation check; return whether it stays active."""
    episode_record = False
    with _spool_lock(spool_id, blocking=False) as acquired:
        if not acquired:
            return True
        current = _read_spool(spool_id)
        if not current or current.get("status") != "pending":
            return False
        if "owner_episode" in current:
            before = dict(current)
            _recover_deterministic_shard(current)
            if current != before:
                _write_spool(spool_id, current)
            episode_record = True
        if not episode_record:
            if current.get("pid"):
                return True
            try:
                created = datetime.fromisoformat(current["created_at"])
            except (KeyError, TypeError, ValueError):
                return True
            now = datetime.now(timezone.utc) if created.tzinfo else datetime.now()
            elapsed = (now - created).total_seconds()
            if elapsed <= PENDING_SPAWN_TIMEOUT:
                return True
            reconciliation = _reconcile_spool_ownership(current)
            if reconciliation.state in {"active", "unverifiable", "store_unhealthy"}:
                return True
            if elapsed <= PENDING_LAUNCH_TIMEOUT and reconciliation.state != "terminalizable":
                return True
            _recover_deterministic_shard(current)
            current.pop("launcher_pid", None)
            current.pop("launcher_start_time", None)
            from .owner_episode_convergence import finalize_legacy_stale_reservation

            finalize_legacy_stale_reservation(spool_id, current, now.isoformat())
            return False
    from .owner_episode_convergence import ObserverIdentity, converge_owner_episode

    result = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
    return result.terminal_state == "none"


def _pending_episode_requires_finalization(spool: dict) -> bool:
    """Let terminal owner authority outrank the compatibility pending mirror."""
    episode = spool.get("owner_episode")
    return (
        isinstance(episode, dict)
        and episode.get("phase") in {"cleanup_proven", "released"}
        and _reconcile_spool_ownership(spool).state == "terminalizable"
    )


def _recovery_pass() -> list:
    """Reconcile every active spool; return the IDs that still need a monitor.

    Split from monitor starting so gated maintenance can run this pass under
    the control lock: _start_spool_monitor reacquires that lock and would
    deadlock if called from inside the held section.
    """
    from .owner_episode_convergence import discoverable_duties_outstanding

    needs_monitor = []

    def retain_after_reconciliation(spool_id: str, remains_active: bool) -> None:
        # Reconciliation can atomically replace the active snapshot from this
        # scan with a terminal record that carries a newly materialized duty.
        # Its return value reports terminal publication, not duty completion,
        # so decide supervision from the fresh durable record.
        current = _read_spool(spool_id)
        if remains_active or discoverable_duties_outstanding(spool_id, current):
            needs_monitor.append(spool_id)

    for spool in _list_spools():
        if spool.get("status") == "running":
            retain_after_reconciliation(spool["id"], not _check_and_finalize_spool(spool["id"]))
        elif spool.get("status") == "pending":
            if _pending_episode_requires_finalization(spool):
                remains_active = not _check_and_finalize_spool(spool["id"])
            else:
                remains_active = _reconcile_pending_spool(spool["id"])
            retain_after_reconciliation(spool["id"], remains_active)
        elif "owner_episode" in spool:
            # Cleanup runs before this recovery pass. A terminal record it kept
            # because convergence duties remain must be replayed here; limiting
            # startup recovery to pending/running records would retain the duty
            # forever without ever attempting it.
            if discoverable_duties_outstanding(spool["id"], spool):
                _check_and_finalize_spool(spool["id"])
                retain_after_reconciliation(spool["id"], False)
    return needs_monitor


def _parse_claude_transcript_events(transcript_text: str) -> list:
    """Best-effort parse of a saved Claude transcript into a list of dict events.

    Transcripts are the raw stdout of the original run: a JSON array of events
    (one-shot protocol), NDJSON (stream-driver protocol), or a single JSON
    object (the oldest CLI format). Unparseable text yields an empty list.
    """
    try:
        data = json.loads(transcript_text)
    except json.JSONDecodeError:
        return _claude_driver.parse_ndjson_events(transcript_text)
    if isinstance(data, list):
        return [ev for ev in data if isinstance(ev, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _transcript_unresolved_tasks(transcript_text: str) -> list:
    """Unresolved nonpersistent background tasks recorded in a saved transcript."""
    events = _parse_claude_transcript_events(transcript_text)
    if not events:
        return []
    return _claude_driver.background_task_state(events)["unresolved"]


# Per-item caps for the sanitized transcript render. Tool results dominate raw
# transcripts (full file reads, test logs); the caps bound the rebuilt session's
# context while keeping far more signal than the result stub alone. Applied
# after control-block stripping so a cut can't leave a half-open notification.
SANITIZED_TOOL_RESULT_MAX_CHARS = 20000
SANITIZED_TOOL_CALL_MAX_CHARS = 2000


def _truncate_for_transcript(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text) - limit} chars omitted]"


def _sanitize_claude_transcript(transcript_text: str) -> str:
    """Render a saved Claude transcript as readable conversation prose.

    Keeps user messages, assistant prose, tool calls, and tool output. Strips
    provider control blocks (task notifications, queued-command attachments)
    and lifecycle noise (system/init/thinking/result events). A resumed model
    must never see a stale task-notification control block adjacent to the new
    continuation — that adjacency is exactly what made rescue respins read as
    prompt injection (issue-20260724-tqs3).

    Falls back to the raw text (still control-block-stripped) when the
    transcript does not parse as events.
    """
    events = _parse_claude_transcript_events(transcript_text)
    lines = []
    for ev in events:
        etype = ev.get("type")
        message = ev.get("message")
        if not isinstance(message, dict):
            continue
        if etype == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    lines.append(f"[assistant] {text}")
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        lines.append(f"[assistant] {text}")
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    try:
                        rendered_input = json.dumps(block.get("input", {}))
                    except (TypeError, ValueError):
                        rendered_input = str(block.get("input"))
                    lines.append(
                        f"[tool call] {name}: {_truncate_for_transcript(rendered_input, SANITIZED_TOOL_CALL_MAX_CHARS)}"
                    )
        elif etype == "user":
            for channel, text in _claude_driver.iter_user_texts(ev):
                cleaned = _claude_driver.strip_control_blocks(text).strip()
                if not cleaned:
                    continue
                if channel == "tool_result":
                    lines.append(f"[tool result] {_truncate_for_transcript(cleaned, SANITIZED_TOOL_RESULT_MAX_CHARS)}")
                else:
                    lines.append(f"[user] {cleaned}")
    if not lines:
        return _claude_driver.strip_control_blocks(transcript_text)
    return "\n\n".join(lines)


def _build_transcript_continuation_prompt(
    transcript_text: str, new_message: str, abandoned_tasks: Optional[list] = None
) -> str:
    """Build the fresh-session prompt that continues a saved conversation.

    Parked-spool respin recovery gets a sanitized rendering and an explicit
    statement that old background tasks are dead — never a raw control block
    the model has to second-guess.
    """
    parts = [
        "Previous conversation transcript (sanitized: provider control notifications were removed):",
        "",
        _sanitize_claude_transcript(transcript_text),
        "",
    ]
    if abandoned_tasks:
        ids = ", ".join(str(t.get("id", "?")) for t in abandoned_tasks if isinstance(t, dict))
        parts.extend(
            [
                f"Background task(s) [{ids}] from the previous session were abandoned when its "
                f"process exited: no notification from them will ever arrive. Do not wait for "
                f"them or re-arm monitors on them. If that background work still matters, re-run "
                f"it in the foreground and finish within a single turn.",
                "",
            ]
        )
    parts.extend(
        [
            "---",
            "",
            f"Continue from the conversation above. New message: {new_message}",
        ]
    )
    return "\n".join(parts)


def _spool_elapsed_seconds(spool: dict) -> Optional[float]:
    """Best-effort elapsed age for legacy or partially published records."""
    try:
        created = datetime.fromisoformat(spool["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc) if created.tzinfo else datetime.now()
    return (now - created).total_seconds()


def _absolute_deadline_expired(deadline: Optional[str]) -> Optional[bool]:
    if deadline is None:
        return None
    try:
        parsed = datetime.fromisoformat(deadline)
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= parsed.astimezone(timezone.utc)


def _owner_episode_deadline_expired(spool: dict) -> Optional[bool]:
    return _absolute_deadline_expired((spool.get("owner_episode") or {}).get("deadline"))


def _reconcile_spool_step(spool_id: str) -> bool:
    """Run one bounded reconciliation step; return whether the spool is active."""
    spool = _read_spool(spool_id)
    if not spool:
        return False
    if "owner_episode" in spool:
        episode = spool.get("owner_episode") or {}
        if (
            spool.get("status") == "pending"
            and episode.get("phase") == "reserved"
            and _reconcile_spool_ownership(spool).state == "terminalizable"
        ):
            with _spool_lock(spool_id) as acquired:
                current = _read_spool(spool_id) if acquired else None
                if current is not None:
                    before = dict(current)
                    _recover_deterministic_shard(current)
                    if current != before:
                        _write_spool(spool_id, current)
        from .owner_episode_convergence import ObserverIdentity, converge_owner_episode

        convergence = converge_owner_episode(spool_id, ObserverIdentity.for_this_process())
        spool = _read_spool(spool_id) or spool
        if convergence.terminal_state in {"terminal_with_obligations_pending", "fully_converged"}:
            return False
        if convergence.classification in {"unverifiable", "store_unhealthy"}:
            return True
    if spool.get("status") == "pending":
        if _pending_episode_requires_finalization(spool):
            return not _check_and_finalize_spool(spool_id)
        return _reconcile_pending_spool(spool_id)
    if spool.get("status") != "running":
        return False

    reconciliation = _reconcile_spool_ownership(spool)
    if reconciliation.state == "terminalizable":
        return not _check_and_finalize_spool(spool_id)
    if reconciliation.state in {"unverifiable", "store_unhealthy"}:
        return True

    if spool.get("timeout"):
        deadline_expired = _owner_episode_deadline_expired(spool)
        elapsed = _spool_elapsed_seconds(spool) if deadline_expired is None else None
        if deadline_expired is True or (elapsed is not None and elapsed > spool["timeout"]):
            request, error = _request_owner_stop(spool_id, "timeout", "store-supervisor")
            if error:
                logger.warning("spindle: timeout request for %s deferred: %s", spool_id, error)
            return True

    return True


def _monitor_spool(spool_id: str) -> None:
    """Compatibility loop for internal callers; production uses the supervisor."""
    while _reconcile_spool_step(spool_id):
        time.sleep(MONITOR_POLL_INTERVAL)


def _run_spool_monitor(spool_id: str) -> None:
    try:
        _monitor_spool(spool_id)
    finally:
        with _SPOOL_MONITORS_LOCK:
            _SPOOL_MONITORS.discard(spool_id)


def _start_spool_monitor(spool_id: str) -> None:
    """Compatibility name: ensure the store, not this caller, owns finalization."""
    ok, error = _ensure_store_supervisor()
    if not ok:
        logger.error("spindle: %s could not ensure durable supervisor: %s", spool_id, error)


def _run_store_supervisor(store: str, lock_fd: int) -> None:
    """Own and reconcile one explicit spool store until its idle grace expires."""
    global SPINDLE_DIR, _STORE_LAYOUT
    SPINDLE_DIR = Path(store).resolve()
    _STORE_LAYOUT = _StoreLayout(
        schema1_root=SPINDLE_DIR,
        schema2_root=SPINDLE_DIR.with_name("spools-v2"),
        active_root=SPINDLE_DIR,
        active_schema=1,
    )
    SPINDLE_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat()
    _write_supervisor_record(_supervisor_identity(os.getpid(), started_at=started_at))
    idle_since: Optional[float] = None
    owned_fd = lock_fd
    from .owner_episode_convergence import discoverable_duties_outstanding

    try:
        while True:
            any_active = False
            for spool in _list_spools():
                spool_id = spool["id"]
                try:
                    duty_work = discoverable_duties_outstanding(spool_id, spool)
                    if spool.get("status") not in {"pending", "running"} and not duty_work:
                        continue
                    if _reconcile_spool_step(spool_id):
                        any_active = True
                    elif discoverable_duties_outstanding(spool_id, _read_spool(spool_id)):
                        # A transient durable-effect failure remains supervisor
                        # work even though the public record is already terminal.
                        any_active = True
                except Exception:
                    # One malformed legacy record must not kill ownership for
                    # every healthy sibling and poison every later reclaim.
                    any_active = True
                    logger.exception("spindle: reconciliation failed for spool %r", spool.get("id"))

            if any_active or _count_running():
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= SUPERVISOR_IDLE_GRACE:
                # Release the lifetime claim while still holding the control
                # lock. A waiting launcher then either sees this owner or starts
                # its replacement; it can never reserve in the gap between the
                # final empty scan and ownership release.
                with _supervisor_control_lock():
                    if not _store_supervision_required():
                        record = _supervisor_identity(os.getpid(), started_at=started_at)
                        record["retired_at"] = datetime.now().isoformat()
                        _write_supervisor_record(record)
                        os.close(owned_fd)
                        owned_fd = -1
                        return
                    idle_since = None

            time.sleep(SUPERVISOR_POLL_INTERVAL)
    finally:
        if owned_fd >= 0:
            os.close(owned_fd)


def _finish_spawn_barrier(spool_id: str, *, start: bool) -> None:
    """Release a durably published launch, or close the gate without exec."""
    barrier_fd = _SPAWN_BARRIERS.pop(spool_id, None)
    if barrier_fd is None:
        return
    try:
        if start:
            os.write(barrier_fd, b"go\n")
    except OSError:
        # A child that died before publication will be reconciled from its
        # durable PID and exit-status file like any other failed launch.
        pass
    finally:
        os.close(barrier_fd)


def _spawn_detached(
    spool_id: str,
    cmd: list,
    cwd: str,
    env: Optional[Dict[str, str]] = None,
    stdin_path: Optional[Path] = None,
) -> int:
    """
    Spawn a detached process that survives parent death.
    Returns the PID.

    Args:
        spool_id: The spool ID for output files
        cmd: Command and arguments to execute
        cwd: Working directory
        env: Optional dict of environment variables to merge with current environment
        stdin_path: Optional file to connect as the child's stdin (file-delivered
            prompts that exceed the per-argv size limit). None inherits, as before.
    """
    exit_path = _get_exit_path(spool_id)
    exit_path.unlink(missing_ok=True)
    # A same-ID replacement retains the identity file to allocate the next
    # generation, but evidence from that previous generation cannot authorize
    # settlement of the new owner.
    _get_owner_exit_path(spool_id).unlink(missing_ok=True)

    process_env = _process_env(env)
    executable = str(cmd[0]) if cmd else ""
    resolved_executable = (
        executable if os.path.isabs(executable) else shutil.which(executable, path=process_env.get("PATH"))
    )
    if not resolved_executable or not os.access(resolved_executable, os.X_OK):
        raise FileNotFoundError(f"executable not found or not runnable: {executable}")

    spool = _read_spool(spool_id) or {}
    episode = spool.get("owner_episode") or {}
    direct_compatibility_launch = not episode
    if direct_compatibility_launch:
        spool.setdefault("id", spool_id)
        spool.setdefault("status", "pending")
        spool.setdefault("created_at", datetime.now().isoformat())
        spool.setdefault("spool_schema_version", SPOOL_SCHEMA_VERSION)
        spool.setdefault("owner_generation", _next_owner_generation(spool_id))
        _ensure_spool_wall_deadline(spool)
        _write_spool(spool_id, spool)
        generation = spool["owner_generation"]
        deadline = spool.get("wall_deadline_at")
    else:
        generation = episode.get("generation")
        deadline = episode.get("deadline")
    if not generation or (episode and episode.get("phase") != "reserved"):
        raise RuntimeError("owner episode is not reserved")
    barrier_read_fd, barrier_write_fd = os.pipe()
    owner_cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "spindle_owner.py"),
        "--store",
        str(SPINDLE_DIR),
        "--spool-id",
        spool_id,
        "--generation",
        str(generation),
        "--launch-barrier-fd",
        str(barrier_read_fd),
        "--cwd",
        cwd,
    ]
    timeout = spool.get("timeout")
    if timeout is not None:
        owner_cmd.extend(["--timeout", str(timeout)])
    if deadline is not None:
        owner_cmd.extend(["--deadline", str(deadline)])
    if stdin_path is not None:
        owner_cmd.extend(["--stdin-path", str(stdin_path)])
    owner_cmd.extend(
        [
            "--",
            *cmd,
        ]
    )

    try:
        proc = subprocess.Popen(
            owner_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=process_env,
            start_new_session=True,
            pass_fds=(barrier_read_fd,),
        )
    except Exception:
        os.close(barrier_write_fd)
        raise
    finally:
        os.close(barrier_read_fd)

    # Keep the handle for fast-path polling and reaping. The exit-status file is
    # authoritative after a restart when this in-memory handle no longer exists.
    _PROC_HANDLES[spool_id] = proc
    _SPAWN_BARRIERS[spool_id] = barrier_write_fd
    if direct_compatibility_launch:
        current = _read_spool(spool_id) or spool
        current["watchdog_pid"] = proc.pid
        watchdog_start = _process_start_time(proc.pid)
        if watchdog_start is not None:
            current["watchdog_start_time"] = watchdog_start
        current["watchdog_namespace"] = capture_pid_namespace().to_dict()
        _write_spool(spool_id, current)
    return proc.pid


# Run cleanup and recovery on ordinary module load. The detached supervisor
# sets the guard before importing this package, then receives its explicit
# store path; touching an environment-resolved store first would be incorrect.
# A store held by a live owner this launcher would reject is equally not ours:
# negotiation must reject with the store byte-identical, so maintenance is
# gated on the lifetime-lock compatibility probe inside the same lock hold.
if os.environ.get(SUPERVISOR_IMPORT_GUARD) != "1":
    _run_store_maintenance(cleanup=True)


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
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else []
    timeout, timeout_disabled = _resolve_launch_timeout(timeout, tag_list)

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
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "launch_working_dir": working_dir,
            "base_branch": base_branch,
            "harness": "claude-code",
            "shard_requested": use_shard,
            "tags": tag_list,
            "timeout": timeout,
            "timeout_disabled": timeout_disabled,
        },
    )
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
            error = (
                f"Failed to create SHARD worktree — {shard_error}"
                if shard_error
                else "Failed to create SHARD worktree. Check git repo status."
            )
            _record_pre_spawn_failure(spool_id, error)
            return f"Error: {error}"

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

    claude_flags = []

    if model:
        resolved_model = CLAUDE_MODEL_ALIASES.get(model, model)
        claude_flags.extend(["--model", resolved_model])

    # Select the permission mode for this tier. careful and the None default
    # resolve to auto (no allowlist); readonly/manual keep acceptEdits + their
    # tight allowlist; full/shard/+shard get bypassPermissions. See
    # _claude_permission_mode for the full table.
    claude_flags.extend(["--permission-mode", _claude_permission_mode(permission)])

    if system_prompt:
        claude_flags.extend(["--system-prompt", system_prompt])

    if resolved_tools:
        claude_flags.extend(["--allowedTools", resolved_tools])

    # Profile extra_args are appended verbatim to the claude invocation.
    if extra_args:
        claude_flags.extend(extra_args)

    claude_cmd, claude_protocol = _claude_headless_cmd(effective_prompt, claude_flags)

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
        "shard_created_by_spool": shard_newly_created,
        "shard_source_dir": working_dir if shard_newly_created else None,
        "base_branch": base_branch,
        "model": model,
        "timeout": timeout,
        "timeout_disabled": timeout_disabled,
        "env": env,
        "profile": profile,
        "claude_protocol": claude_protocol,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "claude-code",
    }

    startup_error = _start_spool_process(spool, cmd, cwd, spawn_env)
    if startup_error:
        return startup_error

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
                    Kimi accepts the shared names for API compatibility but has
                    no classifier-vetted "careful" mode. Shared labels do not
                    narrow its powers inside the box; research adds an output
                    target, shard substitutes a worktree but leaves Git metadata
                    read-only, and "full" without shard intent opts out of bwrap.
        research_target: Required for permission="research" or "research+shard".
                         Accepted forms: site:<id>, file:<absolute-path>, dir:<absolute-path>.
        shard: Run in isolated git worktree (SKEIN-aware with graceful fallback)
        system_prompt: Optional system prompt to configure behavior
        working_dir: Directory for the agent to work in (defaults to current)
        allowed_tools: Override permission profile with explicit tool list
        tags: Comma-separated tags for organizing spools (e.g. "batch-1,triage")
        model: Model to use - for Claude: "haiku", "sonnet", "opus", "fable" (claude-fable-5, access ends 2026-07-12), or versioned aliases like "opus-5";
               for Gemini: "flash", "pro", or full model names like "gemini-2.5-pro";
               for Kimi: "k3"/"latest"/"thinking" (K3, always thinking), "k2.6", "k2.5", "k2.7-code"/"code" (coding-focused, thinking-only), "highspeed", or full model names.
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

    # The caller's explicit env is the only env safe to persist on the spool
    # record. Profile-resolved env (which carries ANTHROPIC_API_KEY and any
    # op://-resolved secrets) must never hit disk, so it is built into a
    # separate spawn_env that goes only to the child process.
    caller_env = env
    try:
        harness_lower, model, spawn_env, profile_extra_args, profile_name = _resolve_harness_selection(
            harness, model, caller_env
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

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


def _provider_lifecycle_detail(spool: dict) -> str:
    """Render bounded observational provider state without deriving public status."""
    lifecycle = spool.get("lifecycle")
    provider = lifecycle.get("provider") if isinstance(lifecycle, dict) else None
    if not isinstance(provider, dict):
        return ""

    def _value(key: str) -> str:
        value = provider.get(key)
        if value is None or isinstance(value, (dict, list)):
            return "-"
        return " ".join(str(value).split())[:160] or "-"

    parts = [
        f"protocol={_value('protocol_state')}",
        f"connection={_value('connection_state')}",
        f"last_event={_value('last_event_type')}",
        f"last_activity={_value('last_activity_at')}",
    ]
    if provider.get("active_work") is not None:
        parts.append(f"active_work={_value('active_work')}")
    return "[provider lifecycle: " + " ".join(parts) + "]"


def _running_spool_message(spool: dict) -> str:
    spool_id = str(spool.get("id", "unknown"))
    detail = _provider_lifecycle_detail(spool)
    suffix = f"\n{detail}" if detail else ""
    abandoned_reason = _serialized_abandoned_custody_reason(spool_id)
    if abandoned_reason:
        return (
            f"Spool {spool['id']} still running; ownership is unrecoverable "
            f"({abandoned_reason}). Manual recovery is required before settlement."
        ) + suffix
    reconciliation = _reconcile_spool_ownership(spool)
    if reconciliation.state in {"unverifiable", "store_unhealthy"}:
        return (
            f"Spool {spool['id']} still running; ownership is {reconciliation.state} "
            f"({reconciliation.reason}). Manual recovery is required before settlement."
        ) + suffix
    message = f"Spool {spool['id']} still running: {spool.get('prompt', '')[:50]}..."
    return message + suffix


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
            return _running_spool_message(spool)
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
    _run_store_maintenance()
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
    with mailbox_guard(SPINDLE_DIR, spool_id):
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
    lifecycle = spool.get("lifecycle") or {}
    if lifecycle.get("public_stop_state") == "stopping":
        return f"Cancellation already requested for spool {spool_id}; cleanup is still in progress"

    request, error = _request_owner_stop_locked(spool, "drop", "spin_drop")
    if error:
        return f"Error: Cannot cancel spool {spool_id}: {error}"
    return f"Cancellation requested for spool {spool_id} (request {request.request_id}); waiting for owner cleanup"


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
        detail = _provider_lifecycle_detail(spool)
        if detail:
            header += detail + "\n"
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


def _respin_parked_state(original_spool: dict, force_rebuild: bool = False) -> Tuple[list, Optional[str]]:
    """Whether a Claude spool must respin via transcript rebuild, and its transcript.

    Returns ``(parked_tasks, transcript_text)``. A non-empty task list means
    respin must rebuild a fresh session instead of ``--resume`` (whose orphan
    scan injects a stale stopped notification ahead of the continuation —
    issue-20260724-tqs3). Two triggers:

    - Detector-marked spools (error_kind=headless_background_wait) carry
      their recorded task list.
    - ``force_rebuild`` (the caller's explicit respin(rebuild=True)) forces
      the rebuild for a legacy stub the caller has judged parked.

    Legacy transcripts are deliberately NOT auto-scanned (fell round 2,
    finding-20260724-xvo0): on one-shot output, resolution events are
    structurally invisible, so armed-without-resolution is the signature of a
    NORMAL backgrounded command — measured against the live store it matches
    healthy completed spools and the genuinely parked incident stub alike, at
    roughly 40:1 against genuine parks. No structural rule separates them;
    the human (or orchestrator) holding the stub result decides via
    rebuild=True.
    """
    if original_spool.get("harness", "claude-code") != "claude-code":
        return [], None

    transcript_text = None
    transcript_path = _get_transcript_path(original_spool["id"])
    if transcript_path.exists():
        try:
            transcript_text = transcript_path.read_text()
        except IOError:
            transcript_text = None

    if original_spool.get("error_kind") == "headless_background_wait":
        tasks = [t for t in original_spool.get("pending_background_tasks") or [] if isinstance(t, dict)]
        return tasks or [{"id": "unknown", "source": "unknown"}], transcript_text

    if force_rebuild:
        tasks = []
        if transcript_text:
            state = _claude_driver.background_task_state(_parse_claude_transcript_events(transcript_text))
            tasks = state["unresolved"] or state["stale_resolved"]
        return (
            [{"id": t.get("id"), "source": t.get("source")} for t in tasks]
            or [{"id": "unknown", "source": "forced_rebuild"}]
        ), transcript_text

    return [], transcript_text


def _respin_sync(handle: str, prompt: str, rebuild: bool = False) -> str:
    """Synchronous implementation of respin - auto-detects harness.

    `handle` may be either the spool_id returned by spin() (preferred, and
    consistent with every other spindle entrypoint) or a raw session_id
    (legacy contract). It is resolved to the original spool, and the spool's
    real session_id is what flows down to the harness resume path - never
    the raw caller handle, which may be a spool_id.

    ``rebuild`` (claude-code only) forces the sanitized-transcript rebuild
    instead of ``--resume`` — for legacy parked stubs the caller has judged
    from their result text, since one-shot transcripts carry no sound
    structural park signal.
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
    harness = original_spool.get("harness", "claude-code")

    # A claude spool that parked on background tasks must not --resume: the
    # CLI's resume-time orphan scan inserts a stale "stopped"
    # task-notification as a user message directly ahead of the continuation,
    # and the model correctly reads that contradictory blob as injected input
    # and parks again. The rebuild path needs only the saved transcript — not
    # a session — so this is decided BEFORE the session gate: a spool that
    # parked before any result has no session_id at all, and its transcript
    # is still recoverable (fell round 2, finding-20260724-xvo0).
    parked_tasks: list = []
    parked_transcript = None
    if harness == "claude-code":
        parked_tasks, parked_transcript = _respin_parked_state(original_spool, force_rebuild=rebuild)
        if parked_tasks and not parked_transcript:
            return (
                f"Spool '{original_spool.get('id', handle)}' parked waiting on background "
                f"task(s) [{', '.join(str(t.get('id', '?')) for t in parked_tasks)}] and has no "
                f"saved transcript to rebuild from. Resuming it would replay a stale task "
                f"notification and park again — start a fresh spin() with the task context instead."
            )

    if not parked_tasks and not session_id:
        return f"Spool '{original_spool.get('id', handle)}' completed without a resumable session (status={status})"

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
        respin_timeout, timeout_disabled = _replay_launch_timeout(original_spool)

        # Atomically check concurrency limit and create initial spool entry
        success, error_msg = _try_reserve_slot_and_create(
            spool_id,
            initial_status="pending",
            reservation_metadata={
                "timeout": respin_timeout,
                "timeout_disabled": timeout_disabled,
                "tags": ["respin"],
            },
        )
        if not success:
            return error_msg

        # Slot reserved via spool creation - continue with setup

        prompt_path = None
        if parked_tasks:
            # Fresh session continuing the sanitized transcript; old background
            # tasks are stated dead so the model doesn't wait on them. The
            # rebuilt prompt embeds the whole sanitized transcript and can
            # exceed the 128KiB per-argv limit, in which case it is delivered
            # by file instead of argv (fell round 2, finding-20260724-xvo0).
            effective_prompt = _build_transcript_continuation_prompt(
                parked_transcript, prompt, abandoned_tasks=parked_tasks
            )
            cmd_flags = []
            try:
                prompt_path = _prompt_file_if_oversized(spool_id, effective_prompt)
            except IOError as exc:
                _record_pre_spawn_failure(spool_id, f"could not write rebuild prompt: {exc}")
                return f"Error: could not write rebuild prompt: {exc}"
        else:
            # Ask the provider to resume the recorded session. If the provider
            # no longer has it, this respin terminates with that honest error.
            effective_prompt = prompt
            cmd_flags = ["--resume", session_id]

        # A bare `claude --resume` sets NEITHER --permission-mode NOR --allowedTools,
        # so a resumed spool silently changes capability from the original spin (a
        # bare resume of a careful spool denies `python3 -c ...`, which careful=auto
        # permits). Re-apply the tier the original spool ran under so a careful
        # resume stays auto, a readonly resume keeps its allowlist, etc. The stored
        # allowed_tools mirrors exactly what the original spin used, so no
        # re-resolution (and no research-target re-validation) is needed here.
        # The parked-recovery fresh session needs the same re-application for the
        # same reason.
        orig_allowed_tools = original_spool.get("allowed_tools")
        cmd_flags.extend(["--permission-mode", _claude_permission_mode(orig_permission)])
        if orig_allowed_tools:
            cmd_flags.extend(["--allowedTools", orig_allowed_tools])

        shard_info = original_spool.get("shard")
        cwd = original_spool.get("working_dir") or (shard_info or {}).get("worktree_path") or os.getcwd()

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
                cmd_flags.extend(["--model", CLAUDE_MODEL_ALIASES.get(resume_model, resume_model)])
            if profile_extra_args:
                cmd_flags.extend(profile_extra_args)
        elif parked_tasks and not profile_name and original_spool.get("model"):
            # A --resume session carries its model, but the parked-recovery
            # fresh session does not — re-inject the recorded one so recovery
            # keeps the original spin's model instead of the CLI default.
            # Profile spools only get their model via successful resolution
            # above: a degraded profile (resolved=False) has lost its alt
            # endpoint, and forcing that endpoint's model onto the default
            # endpoint would fail the launch — match the degraded-resume
            # convention and omit --model (fell round 4, finding-20260724-ja3l).
            orig_model = original_spool["model"]
            cmd_flags.extend(["--model", CLAUDE_MODEL_ALIASES.get(orig_model, orig_model)])

        cmd, claude_protocol = _claude_headless_cmd(effective_prompt, cmd_flags, prompt_path=prompt_path)
        # One-shot launches read a file-delivered prompt from stdin; the
        # driver reads its --prompt-file itself.
        spawn_stdin = prompt_path if (prompt_path is not None and claude_protocol is None) else None

        if shard_info:
            cmd = _codex_bwrap_wrap(
                cmd,
                shard_info,
                cwd,
                process_env=_process_env(spawn_env),
            )

        spool = {
            "id": spool_id,
            "status": "pending",
            "prompt": (
                f"Continue {original_spool['id']} (parked recovery): {prompt}"
                if parked_tasks
                else f"Continue {session_id}: {prompt}"
            ),
            "result": None,
            # Parked recovery starts a fresh session; its id is unknown until
            # the CLI's result event and is filled in at finalization.
            "session_id": None if parked_tasks else session_id,
            "working_dir": cwd,
            "allowed_tools": orig_allowed_tools,
            "permission": orig_permission,
            "system_prompt": None,
            "env": caller_env,
            "profile": profile_name,
            "shard": shard_info,
            "tags": ["respin"],
            "timeout": respin_timeout,
            "timeout_disabled": timeout_disabled,
            "claude_protocol": claude_protocol,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
            "pid": None,
            "cost": None,
            "error": None,
            "harness": "claude-code",
        }
        if parked_tasks:
            spool["parked_recovery_of"] = original_spool["id"]
            spool["abandoned_background_tasks"] = parked_tasks

        startup_error = _start_spool_process(spool, cmd, cwd, spawn_env, stdin_path=spawn_stdin)
        if startup_error:
            return startup_error

        # Start background monitor
        _start_spool_monitor(spool_id)

        return spool_id


@mcp.tool()
async def respin(
    session_id: str,
    prompt: str,
    rebuild: bool = False,
) -> str:
    """
    Continue an existing session with a new message.
    Returns immediately with spool_id.

    Auto-detects the harness (claude-code, codex, gemini, kimi) from the
    original spool. An unavailable provider session produces a terminal respin
    error; its saved transcript remains available for manual reconstruction in
    a separate conversation. A Claude spool that parked waiting on background
    tasks (error_kind=headless_background_wait) is not resumed —
    resume would replay a stale task notification into the model's context;
    instead a fresh session is rebuilt from the sanitized transcript with
    those tasks explicitly abandoned.

    Args:
        session_id: The handle of the session to continue. Accepts the
            spool_id returned by spin() (preferred - consistent with every
            other spindle entrypoint) or a raw session_id (legacy). The
            spool's real session_id is resolved internally before resuming.
        prompt: The follow-up message/task
        rebuild: Claude-code only. Force the sanitized-transcript rebuild
            instead of --resume. Use for a LEGACY spool whose stored result
            is a background-wait stub ("Waiting for the ... notification"):
            its one-shot transcript carries no sound structural park signal
            (healthy backgrounded-command spools look identical), so the
            judgment is the caller's. Resuming such a spool replays a stale
            stopped task-notification that reads as prompt injection.

    Returns:
        spool_id to check result later
    """
    return await asyncio.to_thread(_respin_sync, session_id, prompt, rebuild)


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
                elif spool.get("status") == "timeout":
                    remaining_ids = [s for s in ids if s != spool_id]
                    return json.dumps(
                        {
                            "spool_id": spool_id,
                            "error": spool.get("error", "Spool timed out"),
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
                elif spool.get("status") == "timeout":
                    results[spool_id] = f"Error: {spool.get('error', 'Spool timed out')}"
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
            detail = _provider_lifecycle_detail(spool)
            return fallback + (f"\n{detail}" if detail else "")
        message = f"No output yet for spool {spool_id}"
        detail = _provider_lifecycle_detail(spool)
        return message + (f"\n{detail}" if detail else "")

    try:
        with open(stdout_path, "r") as f:
            all_lines = f.readlines()

        if not all_lines:
            fallback = _bg_task_summary()
            if fallback:
                detail = _provider_lifecycle_detail(spool)
                return fallback + (f"\n{detail}" if detail else "")
            message = f"Output file exists but is empty for spool {spool_id}"
            detail = _provider_lifecycle_detail(spool)
            return message + (f"\n{detail}" if detail else "")

        # Get last N lines
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        status = spool.get("status", "unknown")

        header = f"[spool {spool_id} - {status} - {len(all_lines)} total lines, showing last {len(tail)}]\n"
        detail = _provider_lifecycle_detail(spool)
        if detail:
            header += detail + "\n"
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
    retry_timeout = 0 if spool.get("timeout_disabled") is True else spool.get("timeout")

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
            retry_timeout,
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
            retry_timeout,
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
            retry_timeout,
            tags_str,
            spool.get("env"),
            permission=spool.get("permission"),
            shard=_permission_implies_shard(spool.get("permission")) or bool(spool.get("shard")),
            base_branch=spool.get("base_branch"),
            research_target=spool.get("research_target"),
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
            retry_timeout,  # timeout
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
    _run_store_maintenance()
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
    return await asyncio.to_thread(_shard_merge_sync, spool_id, keep_branch, caller_cwd)


def _shard_merge_sync(spool_id: str, keep_branch: bool, caller_cwd: str | None) -> str:
    """Serialize merging with both spool and canonical worktree lifecycles."""
    deadline = time.monotonic() + SPOOL_TERMINAL_LOCK_TIMEOUT
    while True:
        expected_worktree = _spool_worktree_path(_read_spool(spool_id))
        with _worktree_lock(expected_worktree, blocking=False) as worktree_acquired:
            if worktree_acquired:
                with _spool_lock(spool_id, blocking=False) as spool_acquired:
                    if spool_acquired:
                        current = _read_spool(spool_id)
                        if _spool_worktree_path(current) == expected_worktree:
                            return _shard_merge_locked(spool_id, keep_branch, caller_cwd)
        if time.monotonic() >= deadline:
            return f"Error: Could not lock spool {spool_id} and its worktree for shard merge"
        time.sleep(0.05)


def _shard_merge_locked(spool_id: str, keep_branch: bool, caller_cwd: str | None) -> str:
    """Merge a shard while holding its terminal-transition lock."""
    if not caller_cwd:
        return "Error: caller_cwd required. Pass your current working directory to prevent deleting a worktree you're inside of."

    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get("status") in {"pending", "running"}:
        _reconcile_spool_ownership(spool)
        return f"Error: Spool {spool_id} is still starting or running. Wait for completion."

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

    # Check if any active spool has working_dir inside this worktree.
    wt_path = Path(worktree_path).resolve()
    for other in _list_spools():
        other_active = _spool_blocks_destructive_action(other)
        if other_active and other.get("id") != spool_id:
            if _spool_worktree_path(other) == str(wt_path):
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."
            other_wd = other.get("working_dir", "")
            if not other_wd:
                continue
            other_path = Path(other_wd).resolve()
            if other_path == wt_path or wt_path in other_path.parents:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent  # worktrees/name -> repo
    base_branch = _shard_base_branch(spool)

    # A terminal result can outlive a process group that is still writing into
    # the shard. Resolve that explicit warning before any Git or cleanup work.
    if _spool_blocks_destructive_action(spool):
        return f"Error: Spool {spool_id} ownership is not released; shard preserved"
    if spool.get("process_group_cleanup_warning"):
        _pop_and_reap_process_handle(spool_id)
        spool.pop("process_group_cleanup_warning", None)
        _write_spool(spool_id, spool)

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=worktree_path, timeout=10
        )
        if result.stdout.strip():
            return "Error: Shard has uncommitted changes. Commit or discard them first."

        # Persist intent before Git can change the main checkout. A crash during
        # or immediately after merge must leave a durable recovery clue.
        merge_reason = "shard merge in progress; inspect the main checkout before cleanup"
        spool["shard"]["merge_in_progress"] = True
        spool["shard"]["merge_in_progress_at"] = datetime.now().isoformat()
        spool["shard_cleanup_preserved"] = True
        spool["shard_cleanup_preserved_reason"] = merge_reason
        _write_spool(spool_id, spool)

        # Merge branch into the main repo's current HEAD
        result = subprocess.run(
            ["git", "merge", branch_name, "--no-ff", "-m", f"Merge shard {spool_id}: {spool.get('prompt', '')[:50]}"],
            capture_output=True,
            text=True,
            cwd=str(main_repo),
            timeout=30,
        )
        if result.returncode != 0:
            spool["shard"].pop("merge_in_progress", None)
            spool["shard"].pop("merge_in_progress_at", None)
            spool["shard"]["merge_failed"] = True
            spool["shard"]["merge_failed_at"] = datetime.now().isoformat()
            spool["shard"]["merge_error"] = result.stderr.strip() or result.stdout.strip() or "git merge failed"
            reason = "git merge failed; inspect and resolve the main checkout before shard cleanup"
            spool["shard_cleanup_preserved"] = True
            spool["shard_cleanup_preserved_reason"] = reason
            _write_spool(spool_id, spool)
            return f"Error: Merge failed: {result.stderr}"

        # Record the successful merge before attempting destructive worktree
        # cleanup. A crash or cleanup failure must retain a durable handle.
        spool["shard"]["merged"] = True
        spool["shard"]["merged_at"] = datetime.now().isoformat()
        spool["shard"].pop("merge_in_progress", None)
        spool["shard"].pop("merge_in_progress_at", None)
        spool["shard"].pop("merge_failed", None)
        spool["shard"].pop("merge_failed_at", None)
        spool["shard"].pop("merge_error", None)
        cleanup_reason = "merge succeeded; shard worktree cleanup pending"
        spool["shard_cleanup_pending"] = True
        spool["shard_cleanup_pending_reason"] = cleanup_reason
        spool["shard_cleanup_preserved"] = True
        spool["shard_cleanup_preserved_reason"] = cleanup_reason
        _write_spool(spool_id, spool)
        cleanup_succeeded = _cleanup_shard(
            shard_info,
            str(main_repo),
            keep_branch=keep_branch,
            spool_id=spool_id,
        )
        if not cleanup_succeeded:
            reason = "merge succeeded but shard worktree cleanup failed"
            spool["shard_cleanup_pending"] = True
            spool["shard_cleanup_pending_reason"] = reason
            spool["shard_cleanup_preserved"] = True
            spool["shard_cleanup_preserved_reason"] = reason
            _write_spool(spool_id, spool)
            return f"Warning: Merge succeeded to {base_branch}, but shard cleanup failed for {spool_id}"

        _clear_preserved_spool_shard(spool)
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
    return await asyncio.to_thread(_shard_abandon_sync, spool_id, keep_branch, caller_cwd)


def _shard_abandon_sync(spool_id: str, keep_branch: bool, caller_cwd: str | None) -> str:
    """Serialize abandonment with both spool and canonical worktree lifecycles."""
    deadline = time.monotonic() + SPOOL_TERMINAL_LOCK_TIMEOUT
    with mailbox_guard(SPINDLE_DIR, spool_id):
        while True:
            expected_worktree = _spool_worktree_path(_read_spool(spool_id))
            with _worktree_lock(expected_worktree, blocking=False) as worktree_acquired:
                if worktree_acquired:
                    with _spool_lock(spool_id, blocking=False) as spool_acquired:
                        if spool_acquired:
                            current = _read_spool(spool_id)
                            if _spool_worktree_path(current) == expected_worktree:
                                return _shard_abandon_locked(spool_id, keep_branch, caller_cwd)
            if time.monotonic() >= deadline:
                return f"Error: Could not lock spool {spool_id} and its worktree for shard abandonment"
            time.sleep(0.05)


def _shard_abandon_locked(spool_id: str, keep_branch: bool, caller_cwd: str | None) -> str:
    """Abandon a shard while holding its terminal-transition lock."""
    if not caller_cwd:
        return "Error: caller_cwd required. Pass your current working directory to prevent deleting a worktree you're inside of."

    spool = _read_spool(spool_id)

    if not spool:
        return f"Error: Unknown spool_id '{spool_id}'"

    if spool.get("status") == "pending":
        return f"Error: Spool {spool_id} is still starting. Wait for PID publication or cancel it first."

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

    # Check if any OTHER active spool has working_dir inside this worktree.
    wt_path = Path(worktree_path).resolve()
    for other in _list_spools():
        other_active = _spool_blocks_destructive_action(other)
        if other_active and other.get("id") != spool_id:
            if _spool_worktree_path(other) == str(wt_path):
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."
            other_wd = other.get("working_dir", "")
            if not other_wd:
                continue
            other_path = Path(other_wd).resolve()
            if other_path == wt_path or wt_path in other_path.parents:
                return f"Error: Spool {other['id']} is still running in this worktree. Wait for it to complete or use spin_drop() first."

    # Find the main repo path
    main_repo = Path(worktree_path).parent.parent

    # Abandoning the shard cannot repair a conflicted or crash-interrupted
    # merge in the main checkout. Keep the recovery record until Git proves
    # that checkout is clean and no merge remains in progress.
    merge_recovery_pending = bool(shard_info.get("merge_in_progress") or shard_info.get("merge_failed"))
    if merge_recovery_pending:
        try:
            main_status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=str(main_repo),
                timeout=10,
            )
            merge_head = subprocess.run(
                ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                capture_output=True,
                text=True,
                cwd=str(main_repo),
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return f"Error: Could not verify main-checkout merge recovery for spool {spool_id}: {exc}"
        if main_status.returncode != 0 or merge_head.returncode not in {0, 1}:
            return f"Error: Could not verify main-checkout merge recovery for spool {spool_id}; shard preserved"
        if main_status.stdout.strip() or merge_head.returncode == 0:
            return f"Error: Spool {spool_id} has unresolved main-checkout merge recovery; shard preserved"

    is_running = spool.get("status") == "running"
    has_cleanup_warning = bool(spool.get("process_group_cleanup_warning"))
    if is_running:
        request, error = _request_owner_stop_locked(spool, "cancel", "shard-abandon")
        if error:
            return f"Error: Cannot abandon active spool {spool_id}: {error}; shard preserved"
        return f"Error: Cancellation requested for spool {spool_id}; wait for owner cleanup before abandoning"
    elif has_cleanup_warning and _reconcile_spool_ownership(spool).state != "terminalizable":
        return f"Error: Spool {spool_id} ownership is unresolved; shard preserved"
    elif _spool_blocks_destructive_action(spool):
        return f"Error: Spool {spool_id} ownership is not released; shard preserved"
    _pop_and_reap_process_handle(spool_id)
    spool.pop("process_group_cleanup_warning", None)

    # Persist intent before removing the worktree or branch. If this process
    # exits inside cleanup, the spool still explains the missing/partial shard.
    cleanup_reason = "shard abandonment cleanup pending"
    spool["shard"]["abandon_in_progress"] = True
    spool["shard"]["abandon_in_progress_at"] = datetime.now().isoformat()
    spool["shard_cleanup_pending"] = True
    spool["shard_cleanup_pending_reason"] = cleanup_reason
    spool["shard_cleanup_preserved"] = True
    spool["shard_cleanup_preserved_reason"] = cleanup_reason
    _write_spool(spool_id, spool)

    # Cleanup shard
    success = _cleanup_shard(shard_info, str(main_repo), keep_branch=keep_branch, spool_id=spool_id)

    if success:
        spool["shard"]["abandoned"] = True
        spool["shard"]["abandoned_at"] = datetime.now().isoformat()
        _clear_preserved_spool_shard(spool)
        _write_spool(spool_id, spool)
        return f"Abandoned shard {spool_id}" + (" (branch kept)" if keep_branch else "")
    else:
        spool["shard"].pop("abandon_in_progress", None)
        spool["shard"].pop("abandon_in_progress_at", None)
        reason = "shard abandonment failed during worktree cleanup"
        spool["shard_cleanup_pending"] = True
        spool["shard_cleanup_pending_reason"] = reason
        spool["shard_cleanup_preserved"] = True
        spool["shard_cleanup_preserved_reason"] = reason
        _write_spool(spool_id, spool)
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
    # This guard belongs only to the explicitly launched store supervisor.
    # Never let a supervisor retry leak it into a harness (or a nested Spindle
    # server started by that harness).
    process_env.pop(SUPERVISOR_IMPORT_GUARD, None)
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
                # The expected healthy outcome: the sandbox blocked the write, so the
                # target was never created. Logged at debug — as a warning it read like
                # a probe failure and greeted every clean install's first codex spin.
                logger.debug(
                    "spindle: codex sandbox probe (read-only) target absent on %s — write was blocked",
                    codex_bin,
                )
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
    if _read_spool(spool_id) is not None:
        _record_pre_spawn_failure(spool_id, message)
    with _spool_lock(spool_id) as acquired:
        spool = _read_spool(spool_id) if acquired else None
        if not acquired or (spool is not None and spool.get("status") != "error"):
            return f"Error: Spool {spool_id} was finalized before sandbox refusal was recorded"
        metadata = {
            "session_id": session_id,
            "sandbox": sandbox,
            "permission": permission,
            "codex_bin": codex_bin,
            "codex_version": codex_version,
            "sandbox_error": message,
            "harness": "codex",
            "tags": ["codex"],
            "pid": None,
        }
        if spool is None:
            # No record exists, so this refusal creates one and is its whole
            # outcome.
            spool = {"id": spool_id}
            updates = {
                **metadata,
                "status": "error",
                "result": None,
                "error": message,
                "created_at": now,
                "completed_at": now,
            }
        else:
            # _record_pre_spawn_failure already published this same message as
            # the record's outcome - as episode failure evidence convergence
            # projected, or as the legacy terminal. Only the sandbox metadata is
            # still missing; republishing the outcome would move a completion
            # time that is already settled.
            updates = metadata
        from .owner_episode_convergence import publish_record_updates

        publish_record_updates(spool_id, spool, updates)
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

    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else []
    timeout, timeout_disabled = _resolve_launch_timeout(timeout, tag_list)

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
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "launch_working_dir": working_dir,
            "base_branch": base_branch or _detect_default_branch(working_dir),
            "harness": "codex",
            "shard_requested": shard,
            "tags": [*tag_list, "codex"],
            "timeout": timeout,
            "timeout_disabled": timeout_disabled,
        },
    )
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
            error = (
                f"Failed to create SHARD worktree — {shard_error}"
                if shard_error
                else "Failed to create SHARD worktree. Check git repo status."
            )
            _record_pre_spawn_failure(spool_id, error)
            return f"Error: {error}"

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
        "timeout_disabled": timeout_disabled,
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

    startup_error = _start_spool_process(spool, cmd, cwd, process_env)
    if startup_error:
        return startup_error

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
        return _running_spool_message(spool)
    elif status == "complete":
        return spool.get("result", "No result")
    else:
        return f"Spool {spool_id} failed: {spool.get('error', 'Unknown error')}"


def _codex_respin_sync(session_id: str, prompt: str) -> str:
    """Synchronous implementation of codex_respin - continue a Codex session."""
    # Generate spool ID
    spool_id = "codex-" + str(uuid.uuid4())[:8]

    # Resolve the source before reserving so the new owner episode receives its
    # complete effective timeout and freezes a fresh absolute deadline.
    original_spool = _find_spool_by_session(session_id)
    respin_timeout, timeout_disabled = _replay_launch_timeout(original_spool or {})

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "timeout": respin_timeout,
            "timeout_disabled": timeout_disabled,
            "tags": ["codex", "respin"],
        },
    )
    if not success:
        return error_msg

    # Get working_dir, env, and shard info from original spool if possible
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
        "timeout": respin_timeout,
        "timeout_disabled": timeout_disabled,
        "env": env,
        "shard": shard_info,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "codex",
    }

    startup_error = _start_spool_process(spool, cmd, working_dir, process_env)
    if startup_error:
        return startup_error

    # Start background monitor
    _start_spool_monitor(spool_id)

    return spool_id


# Short aliases for common Claude models. Anything not here passes through.
# The plain "haiku"/"sonnet"/"opus" aliases are also accepted by the claude CLI
# directly; they're listed here so spin_harnesses() can advertise them.
# Source of truth: https://platform.claude.com/docs/en/about-claude/models/overview
CLAUDE_MODEL_ALIASES = {
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku-4.5": "claude-haiku-4-5",
    "sonnet-4.6": "claude-sonnet-4-6",
    "opus-4.6": "claude-opus-4-6",
    "opus-4.7": "claude-opus-4-7",
    "opus-4.8": "claude-opus-4-8",
    "opus-5": "claude-opus-5",
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
# "kimi-k2-turbo-preview" models; thinking is now a model capability toggled with
# kimi-cli's --thinking flag. The "thinking" alias therefore resolves to the current
# general model and runs it in thinking mode (see KIMI_THINKING_ALIASES). Upgrade
# kimi-cli and use interactive `/model` if a newly released managed model is missing.
#
# K3 is the current general flagship and is always-thinking. kimi-k2.7-code
# (released 2026-06-12) is a coding-focused upgrade on the k2.6 foundation and is
# also thinking-only. These models MUST always run with --thinking regardless of
# how they were selected (see KIMI_THINKING_REQUIRED). "latest" tracks the newest
# general model, while the explicit k2.7-code / code aliases retain the older
# coding-specialized model.
KIMI_DEFAULT_MODEL = "moonshot-ai/kimi-k3"
KIMI_MODEL_ALIASES = {
    "thinking": "moonshot-ai/kimi-k3",
    "latest": "moonshot-ai/kimi-k3",
    "k3": "moonshot-ai/kimi-k3",
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
    "moonshot-ai/kimi-k3",
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
        f"Upgrade kimi-cli and use interactive `/model` to refresh managed models, "
        f"or add the model to the config."
    )


def _kimi_bwrap_binary(process_env: Dict[str, str]) -> Optional[str]:
    """Resolve Spindle's external filesystem boundary for Kimi."""
    del process_env  # Caller env belongs to kimi-cli, not the trusted wrapper.
    resolved = shutil.which("bwrap")
    if not resolved or not Path(resolved).is_absolute():
        return None
    return resolved


def _kimi_contained_spawn_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Neutralize environment hooks that execute before bwrap creates its namespace."""
    sanitized = dict(env or {})
    unsafe_exact = {
        "BASHOPTS",
        "BASH_ENV",
        "BASH_XTRACEFD",
        "ENV",
        "GCONV_PATH",
        "LOCPATH",
        "NLSPATH",
        "PS4",
        "SHELLOPTS",
    }
    for key in {*os.environ, *sanitized}:
        if key in unsafe_exact or key.startswith(("LD_", "DYLD_", "BASH_FUNC_")):
            sanitized[key] = ""
    return sanitized


def _kimi_bwrap_required_message(permission: Optional[str], shard: bool) -> str:
    if permission == "full" and shard:
        return (
            "bwrap is required because shard intent remains filesystem-contained "
            "even with permission=full; install bubblewrap or omit shard=True to "
            "run Kimi uncontained"
        )
    return (
        "bwrap is required for Kimi filesystem containment; install bubblewrap "
        "or use permission=full without shard intent to run Kimi uncontained"
    )


def _kimi_share_dir(process_env: Dict[str, str], cwd: str) -> Path:
    """Resolve kimi-cli's writable state directory in the child environment."""
    configured = process_env.get("KIMI_SHARE_DIR")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path(cwd) / path
    else:
        path = Path(process_env.get("HOME", str(Path.home()))) / ".kimi"
    return path.resolve()


def _kimi_boundary_write_paths(
    permission: Optional[str],
    cwd: str,
    shard_info: Optional[dict],
    research_target_info: Optional[Dict[str, str]],
    process_env: Dict[str, str],
) -> list[str]:
    """Return the host paths Kimi may write through Spindle's bwrap.

    Kimi's headless mode auto-approves tool calls, and kimi-cli refuses to start
    unless its work directory is writable. Shared permission names therefore do
    not narrow Kimi's in-box powers: the useful boundary is the requested working
    directory (or shard worktree), plus an explicit research output path when
    present. The kimi-cli state directory is also writable so sessions, logs,
    credentials, and downloaded helper binaries continue to work.
    """
    paths: list[Path] = []
    del permission  # Compatibility input; kimi-cli has no corresponding policy.

    work_path = Path(shard_info["worktree_path"]) if shard_info else Path(cwd)
    paths.append(work_path)
    if research_target_info and research_target_info["type"] in {"file", "dir"}:
        paths.append(Path(_research_writable_path(research_target_info)))

    share_dir = _kimi_share_dir(process_env, cwd)
    if not share_dir.exists() or not share_dir.is_dir():
        raise ValueError(
            f"Kimi state directory {share_dir} does not exist; initialize kimi-cli first "
            f"or use permission=full without shard intent for an uncontained setup run"
        )
    paths.append(share_dir)

    result: list[str] = []
    seen: set[str] = set()
    for candidate in paths:
        resolved = str(candidate.resolve())
        if not Path(resolved).exists():
            raise ValueError(f"Kimi writable path does not exist: {resolved}")
        resolved_path = Path(resolved)
        if (
            resolved_path in {Path("/"), Path("/tmp")}
            or resolved_path.is_relative_to("/dev")
            or resolved_path.is_relative_to("/proc")
            or resolved_path.is_relative_to("/run")
            or resolved_path.is_relative_to("/sys")
        ):
            raise ValueError(f"Kimi writable path cannot replace reserved sandbox mount {resolved}")
        if resolved not in seen:
            result.append(resolved)
            seen.add(resolved)
    return result


def _kimi_research_target_for_replay(
    research_target: Optional[str],
    working_dir: str,
) -> Optional[Dict[str, str]]:
    """Reconstruct a persisted target without depending on mutable SKEIN state."""
    if not research_target:
        return None
    parsed = _parse_research_target(research_target)
    if parsed["type"] == "site":
        return parsed
    return _validate_research_target(research_target, working_dir)


def _kimi_bwrap_wrap(
    kimi_cmd: list,
    cwd: str,
    writable_paths: list[str],
    process_env: Dict[str, str],
    bwrap_bin: Optional[str] = None,
) -> list:
    """Put the whole Kimi process behind a read-only-root filesystem boundary.

    This is deliberately external to kimi-cli: headless Kimi auto-approves its
    tools, while bwrap makes those approvals harmless outside the explicit write
    set. A private tmpfs prevents `/tmp` from becoming a host-wide escape hatch.
    """
    resolved_bwrap = bwrap_bin or _kimi_bwrap_binary(process_env)
    if not resolved_bwrap or not Path(resolved_bwrap).is_absolute():
        raise ValueError(
            "bwrap is required for Kimi filesystem containment; install bubblewrap "
            "or use permission=full without shard intent to run Kimi uncontained"
        )

    resolved_cwd = str(Path(cwd).resolve())
    if resolved_cwd != cwd or not Path(resolved_cwd).is_dir():
        raise ValueError(f"Kimi working directory changed or is not canonical: {cwd}")

    command = [
        resolved_bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    resolver_path = Path("/etc/resolv.conf").resolve()
    runtime_root = Path("/run")
    if resolver_path.is_file() and resolver_path.is_relative_to(runtime_root):
        runtime_parents: list[Path] = []
        parent = resolver_path.parent
        while parent != runtime_root:
            runtime_parents.append(parent)
            parent = parent.parent
        for runtime_parent in reversed(runtime_parents):
            command.extend(["--dir", str(runtime_parent)])
        command.extend(["--ro-bind", str(resolver_path), str(resolver_path)])
    for path in writable_paths:
        resolved = str(Path(path).resolve())
        if resolved != path or not Path(resolved).exists():
            raise ValueError(f"Kimi writable path changed or is not canonical: {path}")
        command.extend(["--bind", resolved, resolved])
    command.extend(
        [
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            cwd,
        ]
    )
    command.extend(kimi_cmd)
    return command


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
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else []
    timeout, timeout_disabled = _resolve_launch_timeout(timeout, tag_list)

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
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "launch_working_dir": working_dir,
            "base_branch": base_branch,
            "harness": "gemini",
            "shard_requested": shard,
            "tags": [*tag_list, "gemini"],
            "timeout": timeout,
            "timeout_disabled": timeout_disabled,
        },
    )
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
            error = (
                f"Failed to create SHARD worktree — {shard_error}"
                if shard_error
                else "Failed to create SHARD worktree. Check git repo status."
            )
            _record_pre_spawn_failure(spool_id, error)
            return f"Error: {error}"

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
        "timeout_disabled": timeout_disabled,
        "env": env,
        "research_target": research_target,
        "shard": shard_info,
        "shard_created_by_spool": shard_newly_created,
        "shard_source_dir": working_dir if shard_newly_created else None,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "gemini",
    }

    startup_error = _start_spool_process(spool, gemini_cmd, cwd, env)
    if startup_error:
        return startup_error

    # Start background monitor thread (reuse the standard monitor)
    _start_spool_monitor(spool_id)

    return spool_id


def _gemini_respin_sync(session_id: str, prompt: str, original_spool: dict) -> str:
    """Synchronous implementation of gemini respin - continue a Gemini session."""
    spool_id = "gemini-" + str(uuid.uuid4())[:8]
    respin_timeout, timeout_disabled = _replay_launch_timeout(original_spool)

    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "timeout": respin_timeout,
            "timeout_disabled": timeout_disabled,
            "tags": ["gemini", "respin"],
        },
    )
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
        "timeout": respin_timeout,
        "timeout_disabled": timeout_disabled,
        "env": env,
        "model": model or "auto",
        "shard": original_spool.get("shard"),
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "gemini",
    }

    startup_error = _start_spool_process(spool, gemini_cmd, working_dir, env)
    if startup_error:
        return startup_error

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
        return _running_spool_message(spool)
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
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else []
    timeout, timeout_disabled = _resolve_launch_timeout(timeout, tag_list)
    process_env = _process_env(env)
    # Shard intent always wins: full is uncontained only without a shard.
    use_bwrap = permission != "full" or shard

    try:
        research_target_info = (
            _validate_research_target(research_target, working_dir)
            if (research_target or require_research_target or permission in {"research", "research+shard"})
            else None
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Resolve model aliases (default to K3 if no model specified) and decide
    # whether to run kimi-cli in thinking mode. Validate the resolved model against the
    # kimi config BEFORE reserving a slot or creating a shard: an unregistered model makes
    # kimi-cli silently fall back to an empty LLM and emit only "LLM not set".
    resolved_model = KIMI_MODEL_ALIASES.get(model, model) if model else KIMI_DEFAULT_MODEL
    # Thinking is enabled when the caller picked a thinking alias, OR when the
    # resolved model is thinking-only (K3 and k2.7-code reject requests with
    # thinking disabled, regardless of whether reached via alias or full name).
    enable_thinking = (bool(model) and model in KIMI_THINKING_ALIASES) or (resolved_model in KIMI_THINKING_REQUIRED)
    model_error = _kimi_validate_model(resolved_model)
    if model_error:
        return model_error

    # Refuse before reserving a slot or creating a worktree. bwrap itself cannot
    # fail open: if it later cannot construct the namespace, kimi-cli never runs.
    bwrap_bin = _kimi_bwrap_binary(process_env) if use_bwrap else None
    if use_bwrap and not bwrap_bin:
        return f"Error: {_kimi_bwrap_required_message(permission, shard)}"
    configured_share_dir = process_env.get("KIMI_SHARE_DIR")
    if use_bwrap and shard and configured_share_dir and not Path(configured_share_dir).is_absolute():
        return (
            "Error: relative KIMI_SHARE_DIR is not supported with Kimi shard intent; "
            "use an absolute state-directory path"
        )

    if use_bwrap:
        try:
            # This preflight catches a missing Kimi state directory before a slot
            # is reserved. Recompute after shard creation to include its worktree.
            _kimi_boundary_write_paths(permission, working_dir, None, research_target_info, process_env)
        except ValueError as exc:
            return f"Error: {exc}"

    # Generate spool ID and session ID
    spool_id = "kimi-" + str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())  # Generate our own session ID

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "launch_working_dir": working_dir,
            "base_branch": base_branch,
            "harness": "kimi",
            "shard_requested": shard,
            "tags": [*tag_list, "kimi"],
            "timeout": timeout,
            "timeout_disabled": timeout_disabled,
        },
    )
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
                shard_info = dict(shard_info)
                shard_info["worktree_path"] = str(Path(shard_info["worktree_path"]).resolve())
                cwd = shard_info["worktree_path"]
        elif shard_info:
            shard_info = dict(shard_info)
            shard_info["worktree_path"] = str(Path(shard_info["worktree_path"]).resolve())
        if shard_info is None:
            error = (
                f"Failed to create SHARD worktree — {shard_error}"
                if shard_error
                else "Failed to create SHARD worktree. Check git repo status."
            )
            _record_pre_spawn_failure(spool_id, error)
            return f"Error: {error}"

    effective_prompt = prompt
    if research_target_info:
        effective_prompt = _research_target_preamble(research_target_info) + prompt

    if shard_info and not _research_omits_shard_commit_preamble(research_target_info):
        shard_preamble = """You are working in an isolated SHARD worktree.

Modify the requested files and leave the changes in the worktree for the caller.
Git metadata outside the worktree is read-only inside this Kimi boundary.
Do not commit, tender, torch, or run another SKEIN lifecycle command.

Your task:
"""
        effective_prompt = shard_preamble + effective_prompt

    # Build kimi command: headless mode with auto-approve, stream-json output, and
    # explicit session ID. Kimi has no classifier-vetted "careful" mode; every
    # non-full launch is bounded externally below.
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

    writable_paths: list[str] = []
    readable_rebinds: list[str] = []
    filesystem_boundary = {
        "kind": "none",
        "writable_paths": [],
        "readable_rebinds": [],
        "private_tmp": False,
        "private_run": False,
        "isolated_processes": False,
    }
    if use_bwrap:
        try:
            writable_paths = _kimi_boundary_write_paths(
                permission,
                cwd,
                shard_info,
                research_target_info,
                process_env,
            )
            kimi_cmd = _kimi_bwrap_wrap(
                kimi_cmd,
                cwd,
                writable_paths,
                process_env,
                bwrap_bin=bwrap_bin,
            )
        except ValueError as exc:
            _record_pre_spawn_failure(
                spool_id,
                str(exc),
                {
                    "working_dir": working_dir,
                    "shard": shard_info,
                    "shard_created_by_spool": shard_newly_created,
                    "shard_source_dir": working_dir if shard_newly_created else None,
                    "base_branch": base_branch,
                    "harness": "kimi",
                },
            )
            return f"Error: {exc}"
        filesystem_boundary = {
            "kind": "bwrap",
            "root": "read-only",
            "writable_paths": writable_paths,
            "readable_rebinds": readable_rebinds,
            "private_tmp": True,
            "private_run": True,
            "isolated_processes": True,
        }

    tag_list.append("kimi")  # Auto-tag as kimi spool

    # Create spool record
    spool = {
        "id": spool_id,
        "status": "pending",
        "prompt": prompt,
        "result": None,
        "session_id": session_id,  # Store our generated session ID
        "working_dir": working_dir,
        "execution_cwd": cwd,
        "model": resolved_model or "auto",
        "thinking": enable_thinking,
        "system_prompt": system_prompt,
        "tags": tag_list,
        "timeout": timeout,
        "timeout_disabled": timeout_disabled,
        "env": env,
        "permission": permission,
        "filesystem_boundary": filesystem_boundary,
        "research_target": research_target,
        "shard": shard_info,
        "shard_created_by_spool": shard_newly_created,
        "shard_source_dir": working_dir if shard_newly_created else None,
        "base_branch": base_branch,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "kimi",
    }

    launch_env = _kimi_contained_spawn_env(env) if use_bwrap else env
    startup_error = _start_spool_process(spool, kimi_cmd, cwd, launch_env)
    if startup_error:
        return startup_error

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
    execution_cwd = original_spool.get("execution_cwd")
    if not execution_cwd:
        shard_info = original_spool.get("shard")
        if original_spool.get("shard_created_by_spool") and isinstance(shard_info, dict):
            execution_cwd = shard_info.get("worktree_path")
    execution_cwd = str(Path(execution_cwd or working_dir).resolve())
    process_env = _process_env(original_spool.get("env"))

    original_boundary = original_spool.get("filesystem_boundary")
    if isinstance(original_boundary, dict) and original_boundary.get("kind") in {"bwrap", "none"}:
        use_bwrap = original_boundary["kind"] == "bwrap"
    else:
        # Legacy Kimi records did not persist a boundary or permission. Contain
        # them at the default working-directory write set rather than inheriting
        # the old unrestricted launch.
        use_bwrap = original_spool.get("permission") != "full" or bool(original_spool.get("shard"))

    bwrap_bin = _kimi_bwrap_binary(process_env) if use_bwrap else None
    if use_bwrap and not bwrap_bin:
        message = _kimi_bwrap_required_message(
            original_spool.get("permission"),
            bool(original_spool.get("shard")),
        )
        return f"Error: {message}"

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

    research_target = original_spool.get("research_target")

    if use_bwrap:
        try:
            research_target_info = _kimi_research_target_for_replay(research_target, working_dir)
            computed_paths = _kimi_boundary_write_paths(
                original_spool.get("permission"),
                execution_cwd,
                original_spool.get("shard"),
                research_target_info,
                process_env,
            )
            recorded_paths = (
                original_boundary.get("writable_paths")
                if isinstance(original_boundary, dict) and original_boundary.get("kind") == "bwrap"
                else None
            )
            if recorded_paths is not None and not isinstance(recorded_paths, list):
                return "Error: original Kimi spool has invalid filesystem boundary metadata"
            if recorded_paths is not None and recorded_paths != computed_paths:
                return (
                    "Error: Kimi boundary paths changed since the original spin; "
                    "start a fresh spin instead of widening the recorded boundary"
                )
            recorded_rebinds = (
                original_boundary.get("readable_rebinds")
                if isinstance(original_boundary, dict) and original_boundary.get("kind") == "bwrap"
                else None
            )
            if recorded_rebinds is not None and not isinstance(recorded_rebinds, list):
                return "Error: original Kimi spool has invalid filesystem boundary metadata"
            if recorded_rebinds:
                return (
                    "Error: original Kimi spool contains unsupported external readable mounts; "
                    "start a fresh spin with the simplified boundary"
                )
            writable_paths = computed_paths
            readable_rebinds = []
        except ValueError as exc:
            return f"Error: {exc}"
    else:
        writable_paths = []
        readable_rebinds = []

    # Generate new spool ID
    spool_id = "kimi-" + str(uuid.uuid4())[:8]
    respin_timeout, timeout_disabled = _replay_launch_timeout(original_spool)

    # Atomically check concurrency limit and create initial spool entry
    success, error_msg = _try_reserve_slot_and_create(
        spool_id,
        initial_status="pending",
        reservation_metadata={
            "timeout": respin_timeout,
            "timeout_disabled": timeout_disabled,
            "tags": ["kimi", "respin"],
        },
    )
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

    if execution_cwd:
        kimi_cmd.extend(["-w", execution_cwd])

    if use_bwrap:
        try:
            kimi_cmd = _kimi_bwrap_wrap(
                kimi_cmd,
                execution_cwd,
                writable_paths,
                process_env,
                bwrap_bin=bwrap_bin,
            )
        except ValueError as exc:
            _record_pre_spawn_failure(spool_id, str(exc))
            return f"Error: {exc}"

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
        "execution_cwd": execution_cwd,
        "model": model or "auto",
        "thinking": enable_thinking,
        "system_prompt": None,
        "tags": tag_list,
        "timeout": respin_timeout,
        "timeout_disabled": timeout_disabled,
        "env": original_spool.get("env"),
        "permission": original_spool.get("permission"),
        "filesystem_boundary": {
            "kind": "bwrap" if use_bwrap else "none",
            "root": "read-only" if use_bwrap else None,
            "writable_paths": writable_paths,
            "readable_rebinds": readable_rebinds,
            "private_tmp": use_bwrap,
            "private_run": use_bwrap,
            "isolated_processes": use_bwrap,
        },
        "research_target": research_target,
        "shard": original_spool.get("shard"),
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "pid": None,
        "error": None,
        "harness": "kimi",
    }

    original_env = original_spool.get("env")
    launch_env = _kimi_contained_spawn_env(original_env) if use_bwrap else original_env
    startup_error = _start_spool_process(spool, kimi_cmd, execution_cwd, launch_env)
    if startup_error:
        return startup_error

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
        return _running_spool_message(spool)
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

    Restarts the unit THIS service was installed as, which install-service bakes
    into the unit as SPINDLE_SERVICE_NAME. Assuming "spindle" meant that a second
    install (say spindle-release on its own port) restarted the *default* one
    instead: the caller's own service went unreloaded while another install's
    in-flight spools were interrupted.

    Returns:
        Status message
    """
    global _reload_pending

    service = _own_service_name()
    unit = f"{service}.service"

    # Check if systemd service exists (even if not running)
    result = subprocess.run(["systemctl", "--user", "list-unit-files", unit], capture_output=True, text=True)

    if unit not in result.stdout:
        return f"Error: {unit} not found. Restart manually."

    # Check if currently active
    is_active = subprocess.run(["systemctl", "--user", "is-active", service], capture_output=True).returncode == 0

    def _do_restart():
        action = "restart" if is_active else "start"
        # We've already returned to the caller, so surface a failed restart to
        # the server log rather than letting it vanish in the daemon thread.
        proc = subprocess.run(["systemctl", "--user", action, service], capture_output=True, text=True)
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

    # Recovery may finalize ordinary dead owners, but simultaneous unwitnessed
    # owner/watchdog loss has no lawful producer of cleanup proof.  Diagnose it
    # before promising an asynchronous restart that can never occur, including
    # on a repeat call while an earlier drain waiter is still pending.
    _run_store_maintenance()
    blockers = _drain_blockers()
    if blockers:
        return f"Error: {DrainBlockedError(blockers)}. Use force=True to restart without draining."

    if _reload_pending:
        return f"Reload already pending; will restart when idle ({_count_running()} spool(s) active)."

    _reload_pending = True

    def drain_and_restart():
        global _reload_pending
        try:
            _wait_until_idle()
            time.sleep(0.5)  # Give time for response to be sent
            _do_restart()
        except DrainBlockedError as exc:
            print(f"[spindle] reload drain aborted: {exc}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# spindle doctor
#
# One command that answers "is this install actually working, and is the thing
# on the port actually *this* install?". The second half matters on any machine
# that already runs a spindle from a checkout: a fresh wheel install used to
# report the older service's health as its own, because `spindle status` curled
# a hardcoded 127.0.0.1:8002 and printed whatever answered.
# ---------------------------------------------------------------------------

# Sentinel the smoke asks for and checks. Deliberately not a word an agent would
# emit spontaneously, so a matching result means the agent really answered.
DOCTOR_SMOKE_TOKEN = "spindle-doctor-ok"
DOCTOR_SMOKE_PROMPT = f"Reply with exactly this token and nothing else: {DOCTOR_SMOKE_TOKEN}"

# Harnesses the default smoke will run. Codex and Claude are the established
# smoke pair; Kimi's external bwrap boundary is exercised by a direct behavioral
# test, and Gemini still has no enforced read-only tier here.
DOCTOR_SMOKE_HARNESSES = ("claude-code", "codex")

# Statuses that end a spool, i.e. stop the smoke's poll loop.
_TERMINAL_SPOOL_STATUSES = {"complete", "error", "killed", "timeout"}


def _doctor_result(name: str, status: str, detail: str, lines: Optional[list] = None, **data) -> dict:
    """One doctor check outcome.

    status is "ok", "warn", "fail", or "skip". Only "fail" makes doctor exit 1;
    a warn is something to know about, not something that stops the install
    working (no service running is normal for stdio-only MCP use).
    """
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "lines": lines or [],
        "data": data,
    }


def _fetch_health(host: str, port: int, timeout: float = 2.0) -> Tuple[Optional[dict], Optional[str]]:
    """GET /health from a spindle service. Returns (payload, error_message)."""
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except urllib.error.URLError as exc:
        return None, f"{url} unreachable: {exc.reason}"
    except OSError as exc:
        return None, f"{url} unreachable: {exc}"

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, f"{url} answered with non-JSON ({body[:80]!r})"
    if not isinstance(payload, dict):
        return None, f"{url} answered with non-object JSON"
    return payload, None


def _script_interpreter(path: str) -> Optional[str]:
    """Interpreter from a console script's shebang, or None if unreadable.

    `#!/usr/bin/env python3` names the launcher, not the interpreter, so the
    argument after `env` is the useful part — otherwise every env-shebang script
    compares as living in /usr/bin.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].strip().split()
    if not parts:
        return None
    if os.path.basename(parts[0]) == "env":
        # Skip `env` itself, its options, and any VAR=value assignments. What is
        # left is a bare command name, which must be resolved through PATH before
        # anyone compares it as a path — `Path("python3").parent` is ".", which
        # equals no interpreter directory, so an unresolved name reports every
        # install as foreign.
        skip_next = False
        for token in parts[1:]:
            if skip_next:
                skip_next = False
                continue
            if token in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                skip_next = True
                continue
            if token.startswith("-") or "=" in token:
                continue
            return shutil.which(token) or token
        return None
    return parts[0]


def _doctor_cli_check(argv0: Optional[str] = None) -> dict:
    """Identify the CLI that is running: version, module path, python, PATH entry.

    Also answers "if I type `spindle` in a shell, do I get *this* one?" — the
    answer is no whenever a checkout and a wheel are both installed, and that
    mismatch explains most "I fixed it but nothing changed" confusion.
    """
    module_path = _package_path()
    lines = [f"python {sys.version.split()[0]} at {sys.executable}"]
    status = "ok"

    on_path = shutil.which("spindle")
    if not on_path:
        status = "warn"
        lines.append("no 'spindle' console script on PATH (run it via `python -m spindle`, or fix PATH)")
    else:
        lines.append(f"console script on PATH: {on_path}")
        invoked = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
        invoked_path = Path(invoked) if invoked else None
        foreign = None
        if invoked_path is not None and invoked_path.name == "spindle" and invoked_path.exists():
            # Invoked as a console script: compare the actual files.
            if invoked_path.resolve() != Path(on_path).resolve():
                foreign = f"you ran {invoked_path.resolve()}"
        else:
            # Invoked some other way (python -m spindle, a test, an import).
            # Compare the interpreter directory the PATH script points at with
            # ours: comparing the interpreters themselves is useless, since a
            # venv python resolves to the base interpreter it was created from.
            interp = _script_interpreter(on_path)
            if interp and Path(interp).parent != Path(sys.executable).parent:
                foreign = f"it runs {interp}; this process runs {sys.executable}"
        if foreign:
            status = "warn"
            lines.append(
                f"the `spindle` on PATH is a DIFFERENT install from the one running this check "
                f"({foreign}) — typing `spindle ...` will not reach this install"
            )

    return _doctor_result(
        "cli",
        status,
        f"spindle {__version__} ({module_path})",
        lines,
        version=__version__,
        package=module_path,
        python=sys.executable,
        console_script=on_path,
    )


def _doctor_service_check(host: str, port: int, timeout: float = 2.0) -> dict:
    """Probe the service on host:port and decide whether it is *this* install.

    The identity comparison is the point. A service is only "ok" when it reports
    the same version AND the same package path as the CLI asking. Anything else
    is reported as what it is: a foreign install, a stale build, or a service too
    old to identify itself.
    """
    endpoint = f"http://{host}:{port}/health"
    health, err = _fetch_health(host, port, timeout)
    if err:
        return _doctor_result(
            "service",
            "warn",
            f"no spindle service answering on {endpoint}",
            [
                err,
                "this is fine for stdio MCP use; start one with `spindle start` (HTTP mode)",
            ],
            endpoint=endpoint,
            running=False,
        )

    if health.get("status") != "healthy":
        # Something is listening and speaking JSON, but it is not a healthy
        # spindle. Only a failing *spindle* is this install's problem — an
        # unrelated app on the port is a warning, the same tier as nothing
        # listening at all, or a stdio-only user gets exit 1 from a fine install.
        looks_like_spindle = "version" in health or "running_spools" in health
        return _doctor_result(
            "service",
            "fail" if looks_like_spindle else "warn",
            f"{endpoint} answered but is not a healthy spindle service",
            [
                f"payload: {json.dumps(health)[:200]}",
                (
                    "that looks like a spindle in a bad state"
                    if looks_like_spindle
                    else "that looks like some other application on this port; "
                    "point spindle elsewhere with --port or SPINDLE_PORT"
                ),
            ],
            endpoint=endpoint,
            running=True,
            health=health,
        )

    svc_version = health.get("version")
    svc_package = health.get("package")
    svc_pid = health.get("pid")
    svc_spool_dir = health.get("spool_dir")
    # Only report fields the service actually sent — an older service omits
    # most of them, and "pid None" reads as a fact rather than a gap.
    facts = []
    if svc_pid is not None:
        facts.append(f"pid {svc_pid}")
    facts.append(f"uptime {health.get('uptime_seconds')}s")
    facts.append(f"{health.get('running_spools')} running spool(s)")
    facts.append(f"max {health.get('max_concurrent')}")
    lines = [", ".join(facts)]
    if svc_package:
        lines.append(f"service package: {svc_package}")
    if svc_spool_dir:
        lines.append(f"service spool dir: {svc_spool_dir}")

    def result(status: str, detail: str, extra: Optional[list] = None, same_install: bool = False) -> dict:
        return _doctor_result(
            "service",
            status,
            detail,
            lines + (extra or []),
            endpoint=endpoint,
            running=True,
            service_version=svc_version,
            service_package=svc_package,
            cli_version=__version__,
            cli_package=_package_path(),
            # Identity, reported separately from status. A confirmed-same service
            # can still be "warn" (e.g. a divergent spool store), and callers that
            # need to know whether to trust what it reports must key off identity,
            # not off the status of the whole check.
            same_install=same_install,
            health=health,
        )

    if svc_version is None:
        return result(
            "warn",
            f"a spindle service is running on {endpoint} but does not report its version",
            [
                "it predates version reporting (spindle < 1.2.0), so it cannot be confirmed "
                "to be this install — restart it with `spindle reload` after upgrading",
            ],
        )

    cli_package = Path(_package_path()).resolve()
    if svc_package and Path(svc_package).resolve() != cli_package:
        return result(
            "fail",
            f"{endpoint} is served by a DIFFERENT spindle install (not the one you are running)",
            [
                f"service: {svc_version} from {svc_package}",
                f"this CLI: {__version__} from {cli_package}",
                "use a separate port for this install: `spindle install-service --name <name> --port <port>` "
                "and set SPINDLE_PORT, or point this check at the right port with `--port`",
            ],
        )

    if not svc_package:
        # Version alone is not identity: any responder can claim a version string,
        # and a matching one would otherwise be treated as this install and have
        # the rest of its report (notably its PATH) believed.
        return result(
            "warn",
            f"{endpoint} reports version {svc_version} but not which install it is",
            [
                "every spindle from 1.2.0 on reports its package path; something else is answering "
                "this port, or the response was truncated",
            ],
        )

    if svc_version != __version__:
        return result(
            "fail",
            f"version skew: service {svc_version}, CLI {__version__}",
            ["the service is running older code from the same install — restart it with `spindle reload`"],
        )

    extra = []
    status = "ok"
    if svc_spool_dir and Path(svc_spool_dir).resolve() != Path(SPINDLE_DIR).resolve():
        status = "warn"
        extra.append(
            f"the service stores spools in {svc_spool_dir} but this CLI reads {SPINDLE_DIR} — "
            "spools spun through the service will not be visible to this CLI (SPINDLE_HOME differs)"
        )
    # Identity is settled by version + package; the store warning above does not
    # unsettle it, so this stays same_install=True and its PATH stays trustworthy.
    return result(status, f"spindle {svc_version} on {endpoint} — same install as this CLI", extra, same_install=True)


def _doctor_storage_check() -> dict:
    """Confirm the spool store exists and is actually writable."""
    store = Path(SPINDLE_DIR)
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _doctor_result("storage", "fail", f"cannot create spool store {store}: {exc}", spool_dir=str(store))

    probe = store / f".doctor-probe-{os.getpid()}"
    try:
        probe.write_text("ok")
        probe.read_text()
    except OSError as exc:
        return _doctor_result("storage", "fail", f"spool store {store} is not writable: {exc}", spool_dir=str(store))
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    count = sum(1 for path in store.glob("*.json") if not path.name.startswith("."))
    unhealthy = _store_health_failures()
    if unhealthy:
        lines = [_store_health_failure_text(item) for item in unhealthy]
        lines.append("repair the recorded ownership pathname/inode or permissions, then rerun `spindle doctor`")
        return _doctor_result(
            "storage",
            "fail",
            f"{store} has {len(unhealthy)} unhealthy ownership artifact(s)",
            lines,
            spool_dir=str(store),
            spools=count,
            ownership_failures=unhealthy,
        )
    return _doctor_result(
        "storage",
        "ok",
        f"{store} writable, {count} spool record(s)",
        [
            f"override with SPINDLE_HOME (currently {os.environ.get('SPINDLE_HOME', '<unset>, defaulting to ~/.spindle')})"
        ],
        spool_dir=str(store),
        spools=count,
    )


def _probe_command(command: str, timeout: float = 5.0) -> Tuple[Optional[str], Optional[str]]:
    """Locate a harness CLI and read its version. Returns (path, version)."""
    path = shutil.which(command)
    if not path:
        return None, None
    try:
        proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return path, out[0].strip() if out else None
    except (OSError, subprocess.SubprocessError):
        return path, None


def _doctor_harness_check(
    service_path: Optional[str] = None,
    service_name: Optional[str] = None,
    service_port: Optional[int] = None,
) -> dict:
    """Report which harness CLIs are actually installed, plus lodged profiles.

    Presence only. Whether the CLI is *logged in* cannot be read off the binary,
    and an installed-but-unauthenticated harness is the likeliest first-run
    failure on a fresh machine — so this check says what it knows and points at
    --smoke, which is the part that actually proves a spool can run.

    ``service_path`` is the running service's PATH (from /health). When given,
    every harness this shell can see is re-resolved against it: a service unit
    carries a PATH baked at install time, and when that goes stale the harness
    is findable from your shell and unfindable from the service that has to
    spawn it.
    """
    lines = []
    detected = {}
    for harness in ("claude-code", "codex", "gemini", "kimi"):
        command = HARNESS_COMMANDS[harness]
        path, version = _probe_command(command)
        if path:
            detected[harness] = {"command": command, "path": path, "version": version}
            lines.append(f"{harness}: {version or 'version unknown'} ({path})")
        else:
            lines.append(f"{harness}: not found — `{command}` is not on PATH")

    profiles = sorted(_discover_profiles().keys())
    if profiles:
        lines.append(f"lodged profiles: {', '.join(profiles)}")
    else:
        lines.append("lodged profiles: none")

    if not detected:
        return _doctor_result(
            "harnesses",
            "fail",
            "no harness CLI found — spindle cannot spawn anything",
            lines + ["install at least one: claude, codex, gemini, or kimi-cli"],
            detected=detected,
            profiles=profiles,
        )

    status = "ok" if len(detected) > 1 else "warn"

    # Drift between this shell's PATH and the service's baked one.
    unreachable = []
    if service_path is not None:
        # `is not None`, not truthiness: a service reporting an EMPTY PATH can
        # resolve nothing at all, which is the loudest version of this failure
        # and used to be the one case that skipped the check.
        for harness, info in detected.items():
            if not shutil.which(info["command"], path=service_path):
                unreachable.append(harness)
    if unreachable:
        status = "warn"
        # Name the service in the remedy. A bare `install-service --force` writes
        # the DEFAULT unit on the DEFAULT port, so an operator whose stale service
        # is `spindle-b` on 8042 would rewrite (or create) the wrong one and leave
        # the stale service exactly as it was.
        suffix = f" --name {service_name}" if service_name and service_name != "spindle" else ""
        where = f" --port {service_port}" if service_port else ""
        lines.append(
            f"the running service cannot find {', '.join(sorted(unreachable))} — its PATH was baked at "
            "install time and has gone stale; spools it spawns will fail at launch. "
            f"Re-run `spindle install-service{suffix}{where} --force` from a shell with the right "
            f"PATH, then `spindle reload{suffix}`."
        )

    lines.append("found on PATH is not the same as logged in — `spindle doctor --smoke` checks that a spool runs")
    return _doctor_result(
        "harnesses",
        status,
        f"{len(detected)} of 4 harness CLIs detected: {', '.join(sorted(detected))}",
        lines,
        detected=detected,
        profiles=profiles,
        unreachable_from_service=unreachable,
    )


def _doctor_shard_check() -> dict:
    """git and bwrap: shard isolation and its containment both depend on them."""
    lines = []
    status = "ok"
    git_path, git_version = _probe_command("git")
    if git_path:
        lines.append(f"git: {git_version or 'version unknown'} ({git_path})")
    else:
        status = "warn"
        lines.append("git: not found — shards (isolated worktrees) will not work")

    bwrap_path = shutil.which("bwrap")
    if bwrap_path:
        lines.append(f"bwrap: {bwrap_path} — shard containment available")
    else:
        status = "warn"
        lines.append(
            "bwrap: not found — shard worktrees are created but NOT filesystem-contained; "
            "do not treat `careful+shard` as containment on this machine. "
            "Kimi refuses every launch that requires containment; only `full` "
            "without shard intent remains available."
        )

    detail = "git and bwrap present" if status == "ok" else "shard support is incomplete"
    return _doctor_result("shards", status, detail, lines, git=git_path, bwrap=bwrap_path)


def _doctor_smoke_spin(harness: str, working_dir: str, model: Optional[str], timeout: int) -> str:
    """Spawn one read-only smoke spool. Returns a spool_id or an 'Error: ...'."""
    if harness == "codex":
        return _codex_spin_sync(
            DOCTOR_SMOKE_PROMPT,
            working_dir,
            model,
            "read-only",
            timeout,
            "doctor,smoke",
            None,
            permission="readonly",
        )
    return _spin_sync(
        DOCTOR_SMOKE_PROMPT,
        "readonly",
        False,
        None,
        working_dir,
        None,
        "doctor,smoke",
        model,
        timeout,
        True,  # skeinless: a smoke needs no SKEIN context
        None,
    )


def _doctor_smoke_check(harness: str, timeout: int = 240, model: Optional[str] = None) -> dict:
    """Run one harmless read-only headless spool end to end and check the answer.

    This is the only check that proves the whole path: spawn a real headless
    agent with the installed CLI's own code, persist the spool, finalize it, and
    read the result back. Runs in a throwaway working dir so no repo is touched.
    """
    harness = (harness or "").lower()
    name = f"smoke:{harness}"
    if harness not in DOCTOR_SMOKE_HARNESSES:
        # A harness that exists but has no enforced read-only tier is a legitimate
        # skip. A name that is not a harness at all is a mistake, and skipping it
        # would let `doctor --smoke --harness codxe` exit 0 having smoked nothing.
        if harness in BUILTIN_HARNESSES or harness in _discover_profiles():
            return _doctor_result(
                name,
                "skip",
                f"no read-only smoke for {harness} "
                f"(only {', '.join(DOCTOR_SMOKE_HARNESSES)} have an enforced read-only tier)",
            )
        return _doctor_result(
            name,
            "fail",
            f"unknown harness {harness!r} — nothing was smoked",
            [f"smokeable harnesses: {', '.join(DOCTOR_SMOKE_HARNESSES)}"],
            harness=harness,
        )

    # timeout <= 0 would mean "no timeout" to the spin contract while the poll
    # loop below exits immediately — spawning a real agent and abandoning it.
    timeout = max(1, int(timeout))
    working_dir = tempfile.mkdtemp(prefix="spindle-doctor-")
    keep_working_dir = False
    try:
        spool_id = _doctor_smoke_spin(harness, working_dir, model, timeout)
        if spool_id.startswith("Error:"):
            # A full queue says nothing about whether this install works.
            status = "skip" if "concurrent" in spool_id.lower() else "fail"
            return _doctor_result(name, status, f"could not spawn: {spool_id}", harness=harness)

        # From here an agent is alive in that directory. Keep it until the spool
        # is known to have finished — a Ctrl-C or an exception mid-poll would
        # otherwise delete the working dir out from under a running process.
        keep_working_dir = True

        deadline = time.time() + timeout
        spool = None
        while time.time() < deadline:
            _check_and_finalize_spool(spool_id)
            spool = _read_spool(spool_id)
            if spool and spool.get("status") in _TERMINAL_SPOOL_STATUSES:
                break
            time.sleep(2)

        status = (spool or {}).get("status")
        if status in _TERMINAL_SPOOL_STATUSES:
            keep_working_dir = False
        else:
            # _spin_drop_sync sends SIGTERM and marks the spool terminal; it does
            # not wait or escalate. Say what was actually done rather than claim
            # the process is gone, and leave the agent's working dir in place —
            # deleting it out from under a process that may still be alive is how
            # a hung smoke turns into something worse.
            _spin_drop_sync(spool_id)
            return _doctor_result(
                name,
                "fail",
                f"spool {spool_id} did not finish within {timeout}s (sent it a kill signal)",
                [
                    f"confirm it stopped with: spindle unspool {spool_id}",
                    f"its working dir was left in place: {working_dir}",
                ],
                harness=harness,
                spool_id=spool_id,
            )

        result = (spool or {}).get("result") or ""
        if status != "complete":
            return _doctor_result(
                name,
                "fail",
                f"spool {spool_id} ended {status}: {_format_spool_failure(spool_id, spool)[:200]}",
                harness=harness,
                spool_id=spool_id,
            )
        if DOCTOR_SMOKE_TOKEN not in result:
            return _doctor_result(
                name,
                "fail",
                f"spool {spool_id} completed but did not return the smoke token",
                [f"result: {result[:200]!r}"],
                harness=harness,
                spool_id=spool_id,
            )
        return _doctor_result(
            name,
            "ok",
            f"read-only headless spool {spool_id} returned the smoke token",
            [f"retrieve it with: spindle unspool {spool_id}"],
            harness=harness,
            spool_id=spool_id,
        )
    finally:
        if not keep_working_dir:
            shutil.rmtree(working_dir, ignore_errors=True)


def _service_port(value: str) -> int:
    """argparse type for a port a service can actually bind."""
    import argparse as _argparse

    try:
        port = int(value)
    except ValueError:
        raise _argparse.ArgumentTypeError(f"{value!r} is not a port number")
    if not (1 <= port <= 65535):
        raise _argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _positive_seconds(value: str) -> int:
    """argparse type for a timeout that must actually allow work to happen.

    A non-positive smoke timeout means "no timeout" to the spin contract while
    the poll loop exits at once — spawning a real, billable agent and abandoning
    or immediately killing it. Reject it at the parser instead.
    """
    import argparse as _argparse

    try:
        seconds = int(value)
    except ValueError:
        raise _argparse.ArgumentTypeError(f"{value!r} is not a whole number of seconds")
    if seconds < 1:
        raise _argparse.ArgumentTypeError("must be at least 1 second")
    return seconds


def _own_service_name() -> str:
    """The systemd unit this process was started as.

    `install-service` bakes SPINDLE_SERVICE_NAME into the unit, so a service
    knows which unit is its own. Without it, anything that restarts "spindle"
    from inside a second install restarts the first install instead.
    """
    name = os.environ.get("SPINDLE_SERVICE_NAME", "").strip()
    return name if _valid_service_name(name) else "spindle"


def _systemd_user_dir() -> Path:
    """Directory systemd reads user units from.

    XDG_CONFIG_HOME, not a hardcoded ~/.config: with it set elsewhere, every unit
    written to ~/.config/systemd/user is somewhere systemd never looks, so
    install-service reports success and the service never starts.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _unit_file_path(name: str) -> Path:
    """Path of the systemd user unit for a service name."""
    return _systemd_user_dir() / f"{name}.service"


def _join_line_continuations(text: str) -> str:
    """Fold systemd's backslash-newline continuations into single lines.

    Only the horizontal whitespace after the newline is eaten. `\\s*` also
    matches newlines, which folded a blank line and then the NEXT directive into
    the continued line — so an `Environment=` following a wrapped `ExecStart`
    disappeared from the parse entirely.

    Comment lines inside a continuation are skipped, as systemd skips them: a
    directive continued across a comment is still one directive.
    """
    text = re.sub(r"\\\n(?:[^\S\n]*[#;][^\n]*\n)+", "\\\\\n", text)
    return re.sub(r"\\\n[^\S\n]*", " ", text)


# systemd's C-style escapes, from systemd.syntax(7). `\s` for space is the one
# that is easy to miss and the one most likely to appear in a path.
_SYSTEMD_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "s": " ",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def _decode_systemd_numeric_escape(line: str, i: int) -> Tuple[Optional[str], int]:
    """Decode `\\xNN`, `\\NNN` (octal), `\\uXXXX`, `\\UXXXXXXXX` at ``line[i]``.

    All four are real systemd escapes and all four were being dropped to their
    letters — `/srv/a\\x20b` read as `/srv/ax20b` rather than `/srv/a b`, which
    is a plausible hand-edit since `\\s` is the obscure spelling. Verified
    against systemd on a live unit. A malformed escape makes systemd discard the
    assignment, so this reports failure rather than guessing.

    Returns ``(decoded, characters consumed)``; ``(None, 0)`` if malformed.
    """
    kind = line[i + 1]
    specs = {"x": (16, 2), "u": (16, 4), "U": (16, 8)}
    if kind in specs:
        base, width = specs[kind]
        digits = line[i + 2 : i + 2 + width]
        # int() accepts surrounding whitespace, so `\x2 ` parsed as 2 and the
        # escape swallowed the space that separated the next assignment —
        # merging a foreign one into spindle's word.
        if len(digits) < width or not all(ch in string.hexdigits for ch in digits):
            return None, 0
        try:
            return chr(int(digits, base)), 2 + width
        except ValueError:
            return None, 0
    # Octal: up to three digits after the backslash.
    digits = ""
    j = i + 1
    while j < len(line) and len(digits) < 3 and line[j] in "01234567":
        digits += line[j]
        j += 1
    if not digits:
        return None, 0
    try:
        return chr(int(digits, 8)), 1 + len(digits)
    except ValueError:
        return None, 0


def _parse_systemd_env_line(line: str) -> Optional[list]:
    """Split one `Environment=` value into its (name, value) assignments.

    This implements systemd's syntax rather than the subset spindle happens to
    emit, because the file being read is frequently NOT one spindle wrote — and
    a value misread here is written straight back into the regenerated service,
    pointing it at a store that does not exist.

    What the previous hand-rolled versions missed, each verified against systemd
    on this machine: several assignments per line, single quotes as well as
    double, whitespace around the `=`, C-style escapes (`a\\sb` is `a b`, not
    `asb`), `%%` for a literal percent, and an unterminated quote, which makes
    systemd discard the whole line.

    Returns None when the line is malformed (unterminated quote), which is
    systemd's "ignore this line" — distinct from an empty list, which is a
    deliberate reset.
    """
    assignments = []
    buf = []
    quote = None  # None, '"' or "'"
    started = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            # systemd interprets escapes inside single quotes too, unlike a
            # shell — verified by differential-testing against systemctl show.
            nxt = line[i + 1]
            if nxt in "xuU01234567":
                decoded, consumed = _decode_systemd_numeric_escape(line, i)
                if decoded is None:
                    # systemd drops the bad assignment and everything after it,
                    # but keeps what came before — returning None for the whole
                    # line lost a store the service is genuinely running on.
                    return assignments
                buf.append(decoded)
                started = True
                i += consumed
                continue
            if nxt not in _SYSTEMD_ESCAPES:
                # systemd rejects any escape outside its table and drops the
                # assignment and everything after it.
                return assignments
            buf.append(_SYSTEMD_ESCAPES[nxt])
            started = True
            i += 2
            continue
        if quote is None and char in "\"'":
            quote = char
            started = True
            i += 1
            continue
        if quote is not None and char == quote:
            quote = None
            i += 1
            continue
        if char.isspace() and quote is None:
            if started:
                token = "".join(buf)
                buf = []
                started = False
                if "=" in token:
                    name, _, value = token.partition("=")
                    assignments.append((name, value.replace("%%", "%")))
            i += 1
            continue
        buf.append(char)
        started = True
        i += 1

    if quote is not None:
        # Unterminated quote: systemd drops this word and the rest of the line,
        # keeping the assignments that parsed before it.
        return assignments
    if started:
        token = "".join(buf)
        if "=" in token:
            name, _, value = token.partition("=")
            assignments.append((name, value.replace("%%", "%")))
    return assignments


def _env_from_unit_text(text: str, var: str) -> Optional[str]:
    """Read one Environment= value out of unit text, as systemd would resolve it.

    A later assignment wins, and a bare `Environment=` clears everything set so
    far — both verified against `systemctl show`. Regenerating from the first
    value, or from one that a reset discarded, moves the service off what it is
    actually running.
    """
    resolved = {}
    for line in _join_line_continuations(text).splitlines():
        match = re.match(r"^\s*Environment\s*=(.*)$", line)
        if not match:
            continue
        rhs = match.group(1).strip()
        if not rhs:
            resolved.clear()  # bare Environment= resets the list
            continue
        assignments = _parse_systemd_env_line(rhs)
        if assignments is None:
            continue  # malformed line, ignored entirely
        for name, value in assignments:
            resolved[name] = value
    return resolved.get(var)


def _env_from_unit(name: str, var: str) -> Optional[str]:
    """Read one Environment= value out of a named unit, quoted or not."""
    try:
        text = _unit_file_path(name).read_text(errors="replace")
    except OSError:
        return None
    return _env_from_unit_text(text, var)


def _service_settings_from_file(path: Path) -> dict:
    """Read the port and spool store out of an existing service file.

    ONE rule governs regeneration, and this is the half that reads:

        an explicit argument  >  what the service is already configured with
                              >  the default

    Without the middle term, `install-service --name X --force` — the command
    doctor's own remedies tell you to run — rebuilds the service purely from
    argv, so every setting the operator is not repeating on the command line is
    silently discarded. Omitting `--port` moved a service off its port onto 8002
    (colliding with the default install); omitting `--home` moved it off its
    spool store onto ~/.spindle, stranding every spool in the old one.

    Handles both formats so the rule cannot hold on one platform and not the
    other. Returns {"port": int|None, "home": str|None, "readable": bool}.
    ``readable`` distinguishes "this service is configured with the defaults"
    from "nothing here could be parsed" — the caller must not claim to be
    keeping a value it never actually read.
    """
    settings = {"port": None, "home": None, "readable": False, "home_specifier": False, "env_file": False}

    # launchd: plistlib is the canonical reader. Hand-walking the XML meant not
    # handling launchd's own binary format (plutil -convert binary1 is routine),
    # where a hand walk sees mojibake and reports "nothing configured" for an
    # agent that is configured.
    if path.suffix == ".plist":
        import plistlib

        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            return settings
        if not isinstance(data, dict):
            return settings
        settings["readable"] = True

        env = data.get("EnvironmentVariables")
        if isinstance(env, dict):
            home = env.get("SPINDLE_HOME")
            if isinstance(home, str):
                settings["home"] = home
            port = env.get("SPINDLE_PORT")
            try:
                settings["port"] = int(port)
            except (TypeError, ValueError):
                pass

        # ProgramArguments is what launchd actually runs, so it wins on port.
        # The LAST --port wins, matching argparse: an agent whose arguments say
        # `--port 8075 --port 9001` binds 9001.
        argv = data.get("ProgramArguments")
        if isinstance(argv, list):
            argv = [a for a in argv if isinstance(a, str)]
            for i in range(len(argv) - 2, -1, -1):
                if argv[i] == "--port":
                    try:
                        settings["port"] = int(argv[i + 1])
                    except ValueError:
                        pass
                    break
        return settings

    try:
        text = path.read_text(errors="replace")
    except OSError:
        return settings
    settings["readable"] = True

    # systemd unit. ExecStart wins on port: it is what the service binds.
    # Anchored to the ExecStart line, so an ExecStartPre that happens to mention
    # `serve --http --port` cannot supply the port the unit is rewritten with.
    settings["env_file"] = bool(re.search(r"^\s*EnvironmentFile\s*=", text, re.MULTILINE))
    settings["home"] = _env_from_unit_text(text, "SPINDLE_HOME")
    # A literal percent is written `%%` and a specifier is a bare `%`, but both
    # come back as `%` once unescaped — so the raw text is the only place the
    # two can be told apart. Without this, a store genuinely containing a `%`
    # was reported as a specifier and its usable hint thrown away.
    raw_home = _env_from_unit_text(text.replace("%%", "\x00PCT\x00"), "SPINDLE_HOME")
    settings["home_specifier"] = bool(raw_home and "%" in raw_home.replace("\x00PCT\x00", ""))
    env_port = _env_from_unit_text(text, "SPINDLE_PORT")
    if env_port:
        try:
            settings["port"] = int(env_port)
        except ValueError:
            pass
    # Continuations are folded first: an ExecStart wrapped across lines is one
    # command to systemd, and anchoring without folding stopped seeing its port.
    for line in _join_line_continuations(text).splitlines():
        if not re.match(r"^\s*ExecStart\s*=", line):
            continue
        # Last wins, as argparse resolves a repeated flag, and both spellings.
        matches = re.findall(r"--port[= ]\s*(\d+)", line)
        if matches:
            settings["port"] = int(matches[-1])
    return settings


def _service_record_path(name: str) -> Path:
    """Where spindle records what it installed a service with.

    Six review rounds went into reading spindle's own settings back out of a
    systemd unit, and each round found another place where the reader and
    systemd disagreed: multi-assignment lines, single quotes, whitespace around
    the `=`, C escapes, the bare `Environment=` reset, numeric escapes, and
    finally `%h` and friends — specifiers whose value depends on the runtime
    context of the service, which a file parser cannot know at all. Reading a
    value slightly wrong is worse than not reading it, because it is written
    straight back and moves the service somewhere that does not exist.

    So spindle keeps its own record of what it installed, in a format it owns.
    The unit file stays systemd's; this is spindle's. Nothing here needs to
    parse anyone else's syntax.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "spindle" / "services" / f"{name}.json"


def _digest_text(text: str) -> str:
    """SHA-256 of the exact content spindle is about to write."""
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _service_file_digest(path: Path) -> Optional[str]:
    """SHA-256 of a service file, or None if it cannot be read."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write_service_record(name: str, port: int, home: Optional[str], service_file: Path, content: str = "") -> None:
    """Record what a service was installed with, and which exact file that was.

    The digest is what makes the record trustworthy later. A record on its own
    is just state that can drift: someone edits the unit's port by hand, and a
    later regeneration would confidently rewrite it back to the recorded value.
    Comparing the digest tells us whether the file on disk is still the one this
    record describes — without parsing it, which is the thing that never worked.
    """
    record = {
        "name": name,
        "port": port,
        "home": home,
        "service_file": str(service_file),
        # Hash of the bytes spindle wrote, not a re-read of the file: the record
        # is written after activation, and re-reading would bless an edit that
        # landed in between as spindle's own.
        "service_sha256": _digest_text(content) if content else _service_file_digest(service_file),
        "spindle_version": __version__,
    }
    path = _service_record_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
    except OSError as exc:
        # Not fatal: the service is installed and running. The next regeneration
        # simply has to be told its settings rather than reading them back.
        logger.warning("spindle: could not record service settings in %s: %s", path, exc)
        print(
            f"Warning: could not write {path} ({exc}). The service is installed, but the next "
            f"`install-service --name {name} --force` will ask you to restate --port and --home.",
            file=sys.stderr,
        )


def _read_service_record(name: str) -> Optional[dict]:
    """What spindle installed this service with, or None if there is no usable record.

    Every field is type-checked. A record that is present but malformed must
    read as "no record" so the caller takes the refuse-and-ask path: falling
    through to the defaults instead is how a wrong record silently moved a
    service onto port 8002 and the default store.
    """
    try:
        record = json.loads(_service_record_path(name).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    # bool is a subclass of int; `"port": true` would otherwise render as --port True.
    port = record.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if "home" not in record or not isinstance(record["home"], (str, type(None))):
        return None
    # The digest is required, not optional. Treating a missing one as "skip the
    # check" meant every record written before digests existed was trusted
    # unconditionally — which is the staleness hole this was meant to close.
    if not isinstance(record.get("service_sha256"), str) or not record["service_sha256"]:
        return None
    port_value = record["port"]
    if not (1 <= port_value <= 65535):
        return None
    return record


def _systemd_escape_length(rhs: str, i: int) -> int:
    """Characters consumed by the escape at ``rhs[i]``, or 0 if systemd rejects it.

    systemd's unescaper fails on ANY escape outside its table — not just the
    backslash-space that one round happened to report. `\\q`, `\\.`, `\\-` and a
    trailing backslash all make it drop the assignment and everything after it,
    so they are all breaks.
    """
    if i + 1 >= len(rhs):
        return 0  # trailing backslash
    nxt = rhs[i + 1]
    if nxt in "xuU01234567":
        _, consumed = _decode_systemd_numeric_escape(rhs, i)
        return consumed
    return 2 if nxt in _SYSTEMD_ESCAPES else 0


def _split_systemd_words(rhs: str) -> Tuple[list, bool]:
    """Split an `Environment=` value into its raw, undecoded words.

    Quoting is honoured so a quoted space does not split a word, but escapes are
    NOT decoded — this decides which assignments belong to spindle, and that
    needs the variable NAME, never the value. Every round that decoded values
    for display ended up reimplementing systemd's unescaping and getting it
    wrong somewhere new.

    Returns ``(words, ok)``. ``ok`` is False when the value breaks partway, and
    the words are the ones that completed BEFORE the break — which is what
    systemd keeps. Re-splitting the raw text to recover them, as this used to,
    cut at the first quote character even when it was properly closed, so a
    store of "/srv/spindle store" displayed as empty.
    """
    words = []
    buf = []
    quote = None
    started = False
    i = 0
    while i < len(rhs):
        char = rhs[i]
        if char == "\\":
            consumed = _systemd_escape_length(rhs, i)
            if not consumed:
                # An escape systemd rejects: it drops this assignment and
                # everything after it, keeping what completed before.
                return words, False
            buf.append(rhs[i : i + consumed])
            started = True
            i += consumed
            continue
        if quote is None and char in "\"'":
            quote = char
            started = True
            i += 1
            continue
        if quote is not None and char == quote:
            quote = None
            i += 1
            continue
        if char.isspace() and quote is None:
            if started:
                words.append("".join(buf))
                buf = []
                started = False
            i += 1
            continue
        buf.append(char)
        started = True
        i += 1
    if quote is not None:
        return words, False  # unterminated: systemd keeps what completed before it
    if started:
        words.append("".join(buf))
    return words, True


def _is_spindle_assignment(word: str) -> bool:
    """True if this word assigns one of spindle's own variables.

    The NAME is what decides, not the word's prefix: `SPINDLE_TOKEN_sk-secret`
    is not an assignment at all, and printing it because it starts with the
    right letters is the leak this filter exists to prevent. The name must also
    be a name systemd accepts — `SPINDLE_OTHER-BAD=sk-secret` is rejected by
    systemd, and showing it would print a value from a line systemd ignores.

    A name written with escapes (`\\x53PINDLE_HOME`) reads as foreign and is
    hidden. That is the safe direction: it shows less, never more.
    """
    name, sep, _ = word.partition("=")
    if not sep or not name.startswith("SPINDLE_"):
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _redact_foreign_assignments(line: str) -> Optional[str]:
    """An `Environment=` line with non-spindle variables hidden.

    Per assignment, because systemd allows several on one line and a line-level
    filter printed a secret that shared a line with the store. Spindle's own
    assignments keep the file's own text; nothing is decoded and re-encoded,
    which changed what they meant.
    """
    _, _, rhs = line.partition("=")
    words, ok = _split_systemd_words(rhs.strip())
    ours = [w for w in words if _is_spindle_assignment(w)]
    hidden = len(words) - len(ours)
    if not ours and ok:
        return None
    rendered = " ".join(f'"{w}"' if " " in w else w for w in ours)
    parts = []
    if hidden:
        parts.append(f"{hidden} other assignment(s) hidden")
    if not ok:
        parts.append("rest of line malformed; systemd drops it from there")
    suffix = f"   [{'; '.join(parts)}]" if parts else ""
    # Assembled from pieces; a str.replace over the finished line rewrote a
    # spindle value that happened to contain the marker text.
    return "Environment=" + rendered + suffix if rendered else "Environment=" + suffix.strip()


def _service_file_excerpt(path: Path, max_lines: int = 12, max_chars: int = 200) -> Tuple[str, str]:
    """What a service file says about its port and store, quoted safely.

    Returns ``(excerpt, note)``. This text is what the operator reads and
    retypes, so "verbatim" is not sufficient and not even always safe:

    - continuations are folded first, or a wrapped ExecStart hides the `--port`
      on its next line and the file appears to state no port;
    - only spindle's own variables are shown, per assignment. Unrelated ones
      belong to the operator, and this text lands in agent transcripts;
    - a bare `Environment=` is kept: it is a reset that clears every assignment
      above it, so hiding it shows a store the service is not using;
    - control characters are stripped and long lines truncated, so a value
      cannot clear the terminal or run to megabytes;
    - a file that is not decodable text is named, not dumped.

    The store is additionally reported as the differential-tested reader
    resolves it, because the raw line is written in systemd's escaping: a store
    of `/srv/100%pure` appears as `100%%pure`, and retyping that produces a
    two-percent directory.
    """
    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read(256 * 1024)
    except OSError as exc:
        return "", f"{path.name} could not be read ({exc}); nothing below is a reading of it."
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        if path.suffix == ".plist":
            return "", f"{path.name} is a binary plist; read it with: plutil -p {path}"
        return "", f"{path.name} is not text, so spindle cannot quote it."

    def clean(line: str) -> str:
        line = "".join(ch for ch in line.rstrip() if ch == "\t" or ch.isprintable())
        return line if len(line) <= max_chars else line[:max_chars] + " ..."

    note = ""
    lines = []
    if path.suffix == ".plist":
        wanted = ("--port", "SPINDLE_HOME", "SPINDLE_PORT", "ProgramArguments")
        raw = text.splitlines()
        keep = set()
        for i, line in enumerate(raw):
            if any(token in line for token in wanted):
                keep.add(i)
                if i + 1 < len(raw):
                    keep.add(i + 1)  # in a plist the value is the next line
        lines = [clean(raw[i]) for i in sorted(keep)]
    else:
        for line in _join_line_continuations(text).splitlines():
            stripped = line.strip()
            if re.match(r"^ExecStart\s*=", stripped) or re.match(r"^EnvironmentFile\s*=", stripped):
                lines.append(clean(stripped))
            elif re.match(r"^Environment\s*=", stripped):
                if not stripped.partition("=")[2].strip():
                    lines.append(clean(stripped) + "   [clears every assignment above]")
                    continue
                redacted = _redact_foreign_assignments(stripped)
                if redacted is not None:
                    lines.append(clean(redacted))

        # No claim about what any of this resolves to.
        #
        # Every round that tried to state the effective store had to model more
        # of systemd to do it: quoting, then escapes, then resets, then
        # EnvironmentFile precedence, then byte-level numeric escapes — and each
        # round a reviewer found a value systemd resolves differently, printed
        # as the one to type. systemd is the only thing that knows; it can be
        # asked directly, so it is asked directly.
        note = (
            "Spindle will not tell you what these resolve to — systemd applies quoting, escapes, "
            "resets and any EnvironmentFile, and getting that subtly wrong would move your service. "
            f"Ask systemd instead:\n    systemctl --user show -p Environment {path.stem}"
        )
        if re.search(r"^\s*EnvironmentFile\s*=", _join_line_continuations(text), re.MULTILINE):
            note += (
                "\nThis unit also reads an EnvironmentFile, which systemd applies after Environment= "
                "and which may set the store; `systemctl show` does not expand it either."
            )

    if not lines:
        return "", note
    shown = lines[:max_lines]
    excerpt = "\n".join(f"    {line}" for line in shown)
    if len(lines) > max_lines:
        excerpt += f"\n    ... and {len(lines) - max_lines} more line(s)"
    return excerpt, note


def _resolve_service_settings(
    existing: Path,
    arg_port: Optional[int],
    arg_home: Optional[str],
    name: Optional[str] = None,
) -> Tuple[Optional[int], Optional[str], list, Optional[str]]:
    """Decide what to regenerate a service with.

        an explicit argument
          > spindle's own record of what it installed
            > the default, for a service that does not exist yet

    Returns ``(port, home, notes, blocker)``. A non-None ``blocker`` means the
    settings could NOT be established and the caller must not write: a service
    file exists that spindle has no record of, so its port and store are
    whatever a human put there. Guessing them from the file is what the previous
    six rounds kept getting wrong, and a wrong guess silently moves a running
    service. Asking for them explicitly is one copy-paste, and the message
    carries spindle's best reading of the file as the hint.
    """
    notes = []
    record = _read_service_record(name) if name else None
    exists = existing.exists()

    # A record only describes the file it was written for. If the file has been
    # edited since — a hand-tuned port, a replaced unit — the record is stale,
    # and regenerating from it would rewrite the service back to settings it is
    # no longer running with, announcing that it "kept" them.
    if record is not None and not exists and (arg_port is None or arg_home is None):
        notes.append(f"{existing.name} is gone; recreating it from spindle's record of what it installed.")
    if record is not None and exists:
        if _service_file_digest(existing) != record["service_sha256"]:
            notes.append(f"{existing.name} has been edited since spindle installed it; its record is stale.")
            record = None

    if record is None and exists and (arg_port is None or arg_home is None):
        # Deliberately NOT a runnable command.
        #
        # Four rounds in a row found this suggestion printing a pasteable
        # command that would move the service: 8002 offered as "the file's
        # port"; `--home ''` offered for a store set with a %h specifier; an
        # Environment=SPINDLE_PORT fallback standing in for an ExecStart port
        # the reader could not parse; a repeated --port read first-wins where
        # systemd binds last-wins; a value overridden by an EnvironmentFile.
        #
        # Every one was the same shape: the reader interprets the file with less
        # authority than systemd applies, and then prints its interpretation as
        # something to run. The refusal exists precisely because this file cannot
        # be interpreted reliably — so it quotes the file instead. The operator
        # reads their own settings and types them; nothing spindle believes about
        # the file can be pasted back into it.
        excerpt, excerpt_note = _service_file_excerpt(existing)
        blocker = (
            f"{existing} exists but spindle has no record of installing it (or the file changed "
            f"since), so what it currently runs with cannot be established from the file alone.\n"
            f"Tell spindle what it should be, and it will keep the record from then on:\n"
            f"  spindle install-service --name {name} --port <port> --home <store> --force\n"
            f"(--home '' means the default store, ~/.spindle)"
        )
        if excerpt:
            blocker += f"\n\nWhat the file says:\n{excerpt}"
        if excerpt_note:
            blocker += f"\n{excerpt_note}"
        return None, None, notes, blocker

    # Past the refusal above, only three states remain: an explicit argument, a
    # usable record, or a service that does not exist yet. An existing file with
    # no usable record has already returned a blocker, so there is no fourth
    # case — two earlier rounds added `elif exists:` fallbacks for one, and they
    # were unreachable from the moment the record replaced reading the file. A
    # mutation study found the suite could not tell they were gone.
    if arg_port is not None:
        port = arg_port
    elif record is not None:
        port = record["port"]
        notes.append(f"Keeping the port spindle installed {name} with: {port} (pass --port to change it)")
    else:
        port = DEFAULT_PORT

    if arg_home is not None:
        home = arg_home or None
    elif record is not None:
        home = record["home"]
        shown = home if home else "the default"
        notes.append(f"Keeping the spool store spindle installed {name} with: {shown} (pass --home to change it)")
    else:
        home = os.environ.get("SPINDLE_HOME")

    return port, home, notes, None


def _port_from_unit(name: str) -> Optional[int]:
    """Read the port a named unit binds, or None.

    `reload --name X` has to probe the service it is about to restart, and the
    operator's shell env says nothing about which port that unit bound — the
    unit's own SPINDLE_PORT is the service's environment, not theirs. Guessing
    the default instead means probing a *different* service, whose store happens
    to match, so the wrong-store warning stays silent exactly when it matters.

    ExecStart wins over the Environment line: it is what the service actually
    binds, so if someone edited only one of the two, that is the truthful one.
    """
    # The record is authoritative when there is one; reading the file is the
    # fallback and can be fooled by shapes that are not argv (a `--port=` inside
    # a shell-wrapped command, a decoy in a comment). Wrong here only
    # mis-addresses a probe, but the record costs nothing and is exact.
    unit = _unit_file_path(name)
    record = _read_service_record(name)
    if record is not None:
        # Digest the file the RECORD names. Assuming the systemd path meant a
        # launchd install never matched, so `doctor --name X` on macOS fell back
        # to the default port instead of the one it installed.
        recorded_file = record.get("service_file")
        described = Path(recorded_file) if isinstance(recorded_file, str) else unit
        if _service_file_digest(described) == record["service_sha256"]:
            return record["port"]
    return _service_settings_from_file(unit)["port"]


def _reload_warn_on_store_mismatch(host: str, port: int) -> Optional[str]:
    """Warn when a reload would drain a different store than the service uses.

    `reload` waits for *this* process's spool store to go idle before restarting.
    Once two installs can each have their own SPINDLE_HOME, that wait can be
    about the wrong store — reporting an idle queue while the service it is
    about to restart has agents mid-flight. Returns the warning text (also
    printed to stderr), or None when the stores agree or nothing answered.
    """
    health, err = _fetch_health(host, port, timeout=2.0)
    if err or not isinstance(health, dict):
        return None
    service_store = health.get("spool_dir")
    if not service_store:
        return None
    try:
        same = Path(service_store).resolve() == Path(SPINDLE_DIR).resolve()
    except OSError:
        same = False
    if same:
        return None
    warning = (
        f"warning: the service on {host}:{port} stores spools in {service_store}, "
        f"but this command drains {SPINDLE_DIR}. The drain cannot see that service's "
        f"in-flight spools. Re-run with SPINDLE_HOME={Path(service_store).parent} to drain the right store."
    )
    print(warning, file=sys.stderr)
    return warning


def _doctor_run(
    host: str = DEFAULT_HOST,
    port: Optional[int] = None,
    smoke: bool = False,
    smoke_harnesses: Optional[list] = None,
    smoke_timeout: int = 240,
    service_timeout: float = 2.0,
    service_name: Optional[str] = None,
) -> dict:
    """Run every doctor check and return the full report.

    ``service_name`` is the systemd unit this report is about, used only to make
    the remedies name the right service.
    """
    port = DEFAULT_PORT if port is None else port
    service = _doctor_service_check(host, port, service_timeout)
    # Only trust the reported PATH when the service is confirmed to be this same
    # install; another install's PATH says nothing about whether ours can spawn a
    # harness. Keyed off identity, not off the check's status — a confirmed-same
    # service warns when its spool store differs, and gating on status meant the
    # stale-PATH check silently switched itself off in exactly the two-install
    # setup the README documents.
    service_path = None
    if service["data"].get("same_install"):
        service_path = (service["data"].get("health") or {}).get("path")
    checks = [
        _doctor_cli_check(),
        service,
        _doctor_storage_check(),
        _doctor_harness_check(service_path=service_path, service_name=service_name, service_port=port),
        _doctor_shard_check(),
    ]

    requested = list(smoke_harnesses) if smoke_harnesses else list(DOCTOR_SMOKE_HARNESSES)
    if smoke:
        for harness in requested:
            checks.append(_doctor_smoke_check(harness, timeout=smoke_timeout))
    else:
        # Offer it explicitly rather than running an agent nobody asked for.
        checks.append(
            _doctor_result(
                "smoke",
                "skip",
                "no headless smoke run (add --smoke to actually spawn one)",
                [
                    f"`spindle doctor --smoke` spins one read-only spool per harness "
                    f"({', '.join(requested)}) in a temp dir and checks the answer",
                ],
            )
        )

    failed = [c["name"] for c in checks if c["status"] == "fail"]
    return {
        "ok": not failed,
        "version": __version__,
        "package": _package_path(),
        "endpoint": f"http://{host}:{port}/health",
        "failed": failed,
        "checks": checks,
    }


# Marker written into every generated unit/plist. `install-service` refuses to
# overwrite a service file that lacks it, even with --force: a hand-written unit
# (or another tool's) is not ours to clobber, and clobbering the one already
# serving a machine's spools is not a recoverable mistake.
SERVICE_MARKER = "managed-by: spindle install-service"


# Fingerprints of the service files spindle wrote BEFORE it started marking them
# (<= 1.1.0). Without these, upgrading would be a dead end: the old unit has no
# marker, so `install-service --force` would refuse to replace spindle's own
# file. Each fingerprint is a line only the generator emits — the 1.1.0 unit's
# literal `%h` PATH, and the 1.1.0 plist's fixed label plus its portless
# ExecStart. A hand-written unit for the same service does not carry them (the
# whole point is to keep refusing those).


def _service_file_is_marked(path: Path) -> bool:
    """True if this service file carries spindle's marker in its header.

    Anchored deliberately. Matching the marker anywhere in the file lets a unit
    whose comment reads "NOT managed-by: spindle install-service, hand-written"
    be claimed as ours, and lets any file that merely quotes the docs be claimed
    too. The generators put the marker in the first line (unit) or third (plist),
    so only a leading comment line counts, and it must *start* with the marker
    once its comment punctuation is stripped.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            head = [next(fh, "") for _ in range(10)]
    except OSError:
        return False
    for line in head:
        stripped = line.strip().lstrip("#").lstrip("<!-").strip()
        if stripped.startswith(SERVICE_MARKER):
            return True
    return False


def _backup_service_file(path: Path) -> Optional[Path]:
    """Copy a service file aside before replacing it. Returns the backup path.

    Ownership of a service file cannot be inferred reliably: the shape spindle
    generated is also the shape users copy from `examples/` and then edit, so any
    fingerprint that recognizes an upgrader's old unit also claims a unit they
    customized. Rather than guess, spindle keeps a copy of anything it did not
    demonstrably write, so replacing it is never destructive.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        logger.warning("spindle: could not back up %s: %s", path, exc)
        return None
    return backup


def _valid_service_name(name: str) -> bool:
    """Service names become filenames; keep them from escaping the unit dir."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name or ""))


def _systemd_quote(value: str) -> str:
    """Quote one value for a systemd unit's Environment=/ExecStart= line.

    Three characters are special and all three have bitten this file:

    - whitespace splits an unquoted assignment into several, so the tail is lost;
    - `%` starts a specifier, and an unresolvable one makes systemd drop the
      entire assignment and fall back to its minimal environment;
    - `\\` escapes inside a quoted string, so a value ending in one escapes the
      closing quote and unterminates the string — dropping the assignment again.

    Order matters: backslashes are doubled first, or the backslash this function
    inserts in front of a quote would be doubled in turn.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _service_path_env(path_env: Optional[str] = None) -> str:
    """PATH to bake into a service file, de-duplicated and pruned.

    The calling shell's PATH is the right starting point (it is what makes
    `claude`/`codex`/`gemini` resolvable for the service, which a bare systemd
    user unit's minimal PATH would not), but it routinely carries duplicates and
    per-process temp dirs that will not exist when the service starts. Those are
    dropped so the unit stays readable and honest about what it points at.

    Relative entries are dropped too. A service has no meaningful working
    directory, so `./node_modules/.bin` resolves to something else (or nothing)
    once systemd starts it — while still resolving for whoever runs doctor from
    the directory it was captured in, which would make doctor certify a PATH the
    service cannot actually use.
    """
    raw = path_env if path_env is not None else os.environ.get("PATH", "")
    kept = []
    seen = set()
    for entry in raw.split(os.pathsep):
        if not entry or entry in seen:
            continue
        seen.add(entry)
        if not os.path.isabs(entry):
            continue
        try:
            if not Path(entry).is_dir():
                continue
        except OSError:
            continue
        kept.append(entry)
    return os.pathsep.join(kept) or "/usr/local/bin:/usr/bin:/bin"


def _systemd_unit_text(
    spindle_path: str,
    port: int,
    home: Optional[str] = None,
    path_env: Optional[str] = None,
    name: str = "spindle",
) -> str:
    """Render a systemd user unit for this install.

    PATH is baked in from the calling shell because a systemd user unit
    otherwise inherits a minimal PATH and cannot find `claude`/`codex`/`gemini`,
    which fails at spawn time with a confusing "not found". SPINDLE_PORT and
    SPINDLE_HOME are set so `spindle status`/`doctor` in the same environment
    resolve the same port and store the service uses.

    Every interpolated value is quoted and `%`-escaped. systemd splits an
    unquoted `Environment=` on whitespace and runs specifier expansion on `%`,
    so a PATH holding `/mnt/c/Program Files/...` (the WSL default) silently
    truncates at the space, and a `%` anywhere makes systemd drop the whole
    assignment and fall back to its minimal PATH. Both failures start cleanly
    and answer /health; only the spawned agents die.
    """

    env_lines = [f"Environment=SPINDLE_PORT={port}"]
    env_lines.append(f"Environment={_systemd_quote('SPINDLE_SERVICE_NAME=' + str(name))}")
    # `is not None`: an empty store is a setting the service behaves by, and
    # dropping it here would undo the resolver's decision to keep it.
    if home is not None:
        env_lines.append(f"Environment={_systemd_quote('SPINDLE_HOME=' + str(home))}")
    env_lines.append(f"Environment={_systemd_quote('PATH=' + _service_path_env(path_env))}")
    env_block = "\n".join(env_lines)
    q = _systemd_quote
    return f"""\
# {SERVICE_MARKER} (spindle {__version__})
[Unit]
Description=Spindle MCP Server ({name}, port {port})
After=network.target

[Service]
Type=simple
ExecStart={q(spindle_path)} serve --http --port {port}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
{env_block}

[Install]
WantedBy=default.target
"""


def _launchd_plist_text(
    label: str,
    spindle_path: str,
    port: int,
    home: Optional[str] = None,
    path_env: Optional[str] = None,
    name: str = "spindle",
) -> str:
    """Render a launchd plist for this install (macOS counterpart of the unit).

    The log is named for the service, so two installs do not both write
    ~/.spindle/spindle.log and interleave their output.

    Every interpolated value is XML-escaped. These are filesystem paths and a
    whole PATH: one `&` in any directory name (legal on macOS, and PATH
    aggregates directories the user never chose) produces a plist that will not
    parse, which launchd reports only by never starting the service.
    """
    from xml.sax.saxutils import escape

    env_entries = [("SPINDLE_PORT", str(port)), ("PYTHONUNBUFFERED", "1")]
    if home is not None:
        env_entries.append(("SPINDLE_HOME", str(home)))
    env_entries.append(("PATH", _service_path_env(path_env)))
    env_xml = "\n".join(f"        <key>{escape(k)}</key>\n        <string>{escape(v)}</string>" for k, v in env_entries)
    log_path = Path.home() / ".spindle" / f"{name or 'spindle'}.log"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {SERVICE_MARKER} (spindle {__version__}) -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{escape(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{escape(spindle_path)}</string>
        <string>serve</string>
        <string>--http</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_path))}</string>
</dict>
</plist>
"""


def _doctor_render(report: dict) -> str:
    """Plain-text report: one status per line, no columns, no tables."""
    out = [f"spindle doctor — spindle {report['version']}"]
    for check in report["checks"]:
        out.append(f"{check['status']}: {check['name']} — {check['detail']}")
        for line in check["lines"]:
            out.append(f"    {line}")
    out.append("ok" if report["ok"] else f"FAILED: {', '.join(report['failed'])}")
    return "\n".join(out)


def main():
    import argparse
    import atexit
    import traceback

    parser = argparse.ArgumentParser(description="Spindle MCP server")
    parser.add_argument("--version", "-V", action="version", version=f"spindle {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # serve command (default)
    serve_parser = subparsers.add_parser("serve", help="Run the MCP server")
    serve_parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    serve_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT}, or $SPINDLE_PORT)"
    )
    serve_parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"HTTP host (default: {DEFAULT_HOST}, or $SPINDLE_HOST)"
    )

    # start command - start via systemd or background
    _start_parser = subparsers.add_parser("start", help="Start spindle (via systemd if available)")
    _start_parser.add_argument("--name", default="spindle", help="systemd unit name (default: spindle)")
    _start_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port for the background fallback (no systemd unit)"
    )

    # reload command - restart spindle
    _reload_parser = subparsers.add_parser("reload", help="Reload spindle to pick up code changes")
    _reload_parser.add_argument(
        "--force",
        action="store_true",
        help="Restart immediately instead of draining (may interrupt in-flight spools)",
    )
    _reload_parser.add_argument("--name", default="spindle", help="systemd unit name (default: spindle)")
    _reload_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port of the service being restarted (default: read from its unit file)",
    )
    _reload_parser.add_argument("--host", default=DEFAULT_HOST, help="Host of the service being restarted")

    # status command
    _status_parser = subparsers.add_parser("status", help="Check spindle status")
    _status_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Service port (default: {DEFAULT_PORT})"
    )
    _status_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Service host (default: {DEFAULT_HOST})")

    # doctor command - diagnose this install
    doctor_parser = subparsers.add_parser("doctor", help="Diagnose this spindle install")
    doctor_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Service port (default: read from the named unit, else {DEFAULT_PORT})",
    )
    doctor_parser.add_argument("--host", default=DEFAULT_HOST, help=f"Service host (default: {DEFAULT_HOST})")
    doctor_parser.add_argument(
        "--smoke",
        action="store_true",
        help="Actually run a read-only headless spool per harness (spawns real agents)",
    )
    doctor_parser.add_argument(
        "--harness",
        help=f"Comma-separated harnesses to smoke (default: {','.join(DOCTOR_SMOKE_HARNESSES)})",
    )
    doctor_parser.add_argument(
        "--smoke-timeout",
        type=_positive_seconds,
        default=240,
        help="Seconds to wait per smoke spool (must be positive)",
    )
    doctor_parser.add_argument(
        "--name",
        default="spindle",
        help="systemd unit this report is about, so remedies name the right service (default: spindle)",
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable report instead of plain text")

    # install-service command
    install_service_parser = subparsers.add_parser("install-service", help="Install systemd/launchd user service")
    install_service_parser.add_argument("--force", action="store_true", help="Overwrite existing service file")
    install_service_parser.add_argument(
        "--name",
        default="spindle",
        help="Service name (default: spindle). Use a distinct name to run a second install alongside the first.",
    )
    install_service_parser.add_argument(
        "--port",
        type=_service_port,
        default=None,
        help=f"Port the service binds (default: keep the existing service's port, else {DEFAULT_PORT})",
    )
    install_service_parser.add_argument(
        "--home",
        help="SPINDLE_HOME to bake into the unit (default: the current SPINDLE_HOME, if set)",
    )

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
        help="Model to use (e.g. haiku/sonnet/opus/opus-5/fable for Claude, flash/pro for Gemini, k3/latest/thinking/k2.6/k2.7-code for Kimi)",
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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=DEFAULT_HOST, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Handle subcommands
    if args.command == "start":
        unit = f"{args.name}.service"
        # Check if systemd service exists
        result = subprocess.run(["systemctl", "--user", "list-unit-files", unit], capture_output=True, text=True)
        if unit in result.stdout:
            subprocess.run(["systemctl", "--user", "start", args.name])
            print(f"Started {unit} via systemd")
        else:
            # Start in background. Launch the package by module name, not by
            # __file__: an installed wheel's __init__.py run as a script has no
            # package context, so `python -m spindle` is the portable form.
            #
            # cwd matters here. `-m` puts the working directory at the front of
            # sys.path, so running this from a spindle checkout (or any repo with
            # a spindle/ directory) would start the service from THAT code while
            # the console script is the installed one — precisely the "which
            # install is answering the port?" confusion this release removes.
            # -P / PYTHONSAFEPATH would be cleaner but are 3.11+, and the floor
            # here is 3.10.
            subprocess.Popen(
                [sys.executable, "-m", "spindle", "serve", "--http", "--port", str(args.port)],
                cwd=str(Path.home()),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"Started in background on port {args.port} (no {unit} systemd unit found)")
        sys.exit(0)

    elif args.command == "reload":
        unit = f"{args.name}.service"
        # Check if systemd service exists
        result = subprocess.run(["systemctl", "--user", "list-unit-files", unit], capture_output=True, text=True)
        if unit not in result.stdout:
            print("No systemd service. Kill and run: spindle start")
            sys.exit(0)
        # Drain by default: wait for the queue to empty so in-flight spools
        # aren't interrupted. --force skips the wait. New spins are still
        # allowed during the wait, so this restarts at the next idle moment.
        if not args.force:
            # The drain reads THIS process's store. If the unit being restarted
            # serves a different one, "no spools running" is a statement about
            # the wrong store. Ask the unit which port it bound rather than the
            # shell that ran us — but honor an explicitly passed --port, which is
            # how an operator works around a unit file they know is stale.
            probe_port = args.port if args.port is not None else (_port_from_unit(args.name) or DEFAULT_PORT)
            if _reload_warn_on_store_mismatch(args.host, probe_port):
                # Warning and restarting anyway is the worst of both: a drain that
                # promised to protect in-flight agents interrupts them instead.
                print(
                    "Refusing to restart: this would be an undrained restart of that service. "
                    "Re-run with the matching SPINDLE_HOME, or --force to restart regardless.",
                    file=sys.stderr,
                )
                sys.exit(1)
            _run_store_maintenance()
            blockers = _drain_blockers()
            if blockers:
                print(
                    f"Refusing to wait forever: {DrainBlockedError(blockers)}. "
                    "Use --force to restart without draining.",
                    file=sys.stderr,
                )
                sys.exit(1)
            active = _count_running()
            if active:
                # flush: this is the last output before a potentially long block,
                # and stdout is block-buffered when redirected (not a tty).
                print(f"Draining: waiting for {active} spool(s) to finish (--force to restart now)...", flush=True)
            try:
                _wait_until_idle()
            except DrainBlockedError as exc:
                print(
                    f"Drain aborted: {exc}. Use --force to restart without draining.",
                    file=sys.stderr,
                )
                sys.exit(1)
        proc = subprocess.run(["systemctl", "--user", "restart", args.name], capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"Restarted {unit} via systemd")
            sys.exit(0)
        print(f"Restart failed (exit {proc.returncode}): {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    elif args.command == "status":
        # Ask the port directly (stdlib, no curl) and say whose service answered.
        # Printing a foreign install's health as if it were ours is exactly the
        # confusion this used to cause on a machine running two spindles.
        health, err = _fetch_health(args.host, args.port)
        if err:
            print(f"Not running ({err})")
            sys.exit(0)
        print(json.dumps(health))
        svc_version = health.get("version")
        svc_package = health.get("package")
        if svc_version is None or not svc_package:
            # Same standard as doctor: a version without a package path is not
            # identity, and presenting it as this install is the confusion the
            # release exists to remove.
            print(
                f"note: that service does not report which install it is, so it cannot be confirmed to be "
                f"this one (spindle {__version__} at {_package_path()})",
                file=sys.stderr,
            )
        elif svc_package and Path(svc_package).resolve() != Path(_package_path()).resolve():
            print(
                f"note: port {args.port} is served by a different install "
                f"({svc_version} at {svc_package}), not this one "
                f"({__version__} at {_package_path()}). Run `spindle doctor` for details.",
                file=sys.stderr,
            )
        elif svc_version != __version__:
            print(
                f"note: version skew — service {svc_version}, CLI {__version__}. Run `spindle reload`.",
                file=sys.stderr,
            )
        sys.exit(0)

    elif args.command == "doctor":
        smoke_harnesses = [h.strip() for h in args.harness.split(",") if h.strip()] if args.harness else None
        # --name and --port describe the same service; if only the name is given,
        # read the port out of that unit rather than probing the default one.
        doctor_port = args.port if args.port is not None else (_port_from_unit(args.name) or DEFAULT_PORT)
        report = _doctor_run(
            host=args.host,
            port=doctor_port,
            smoke=args.smoke,
            smoke_harnesses=smoke_harnesses,
            service_name=args.name,
            smoke_timeout=args.smoke_timeout,
        )
        print(json.dumps(report, indent=2) if args.json else _doctor_render(report))
        sys.exit(0 if report["ok"] else 1)

    elif args.command == "spin":
        working_dir = os.path.abspath(args.working_dir or os.getcwd())
        conflict = _readonly_shard_conflict_error(
            args.permission, args.shard or _permission_implies_shard(args.permission)
        )
        if conflict:
            if args.human:
                print(f"Error: {conflict}", file=sys.stderr)
            else:
                print(json.dumps({"error": conflict}))
            sys.exit(1)
        # Same resolution the MCP spin tool uses, so --harness accepts exactly
        # the same names (built-ins AND lodged profiles) and rejects unknown
        # ones instead of quietly running plain Claude Code.
        try:
            harness_lower, model, spawn_env, profile_extra_args, profile_name = _resolve_harness_selection(
                args.harness, args.model, None
            )
        except ValueError as exc:
            if args.human:
                print(f"Error: {exc}", file=sys.stderr)
            else:
                print(json.dumps({"error": str(exc)}))
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
                model,
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
                model,
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
                model,
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
                model=model,
                timeout=args.timeout,
                skeinless=args.skeinless,
                base_branch=args.base_branch or _detect_default_branch(working_dir),
                research_target=args.research_target,
                env=None,
                extra_args=profile_extra_args,
                profile=profile_name,
                spawn_env=spawn_env,
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

        system = platform.system()

        # Find spindle executable path. Prefer the console script that belongs
        # to THIS interpreter (a venv install is next to its own python) over
        # whatever `spindle` PATH happens to resolve to — the unit must start
        # the install the user just ran install-service from, not another one.
        sibling = Path(sys.executable).with_name("spindle")
        spindle_path = str(sibling) if sibling.exists() else shutil.which("spindle")
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

        # The name becomes a filename in the unit/agent directory; `../../x` would
        # write outside it.
        if not _valid_service_name(args.name):
            print(
                f"Invalid service name {args.name!r}: use letters, digits, dot, dash, underscore "
                "(it becomes a filename).",
                file=sys.stderr,
            )
            sys.exit(1)

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

            service_dir = _systemd_user_dir()
            service_file = service_dir / f"{args.name}.service"

            # Explicit argument > what this service is already configured with >
            # default. Read before anything is written.
            service_port, service_home, notes, blocker = _resolve_service_settings(
                service_file, args.port, args.home, name=args.name
            )
            if blocker:
                print(blocker, file=sys.stderr)
                sys.exit(1)
            for note in notes:
                print(note)
            service_content = _systemd_unit_text(spindle_path, service_port, home=service_home, name=args.name)

            unmanaged = service_file.exists() and not _service_file_is_marked(service_file)
            if service_file.exists() and not args.force:
                print(f"Service file already exists: {service_file}")
                if unmanaged:
                    print("It does not carry spindle's marker, so spindle did not write it (or you edited it).")
                    print(f"To leave it alone: spindle install-service --name {args.name}-2 --port <other-port>")
                print("--force replaces it, keeping a timestamped backup beside it first.")
                sys.exit(1)

            service_dir.mkdir(parents=True, exist_ok=True)
            # Back up on EVERY replace, not only for files spindle did not write.
            # A regenerated unit is built from argv, so a marked unit's hand-added
            # directives (an EnvironmentFile, an extra Environment=) are dropped
            # by --force just as surely as an unmarked one's — being spindle's
            # file says nothing about whether it was edited since.
            if service_file.exists():
                backup = _backup_service_file(service_file)
                if backup is None:
                    print(f"Refusing to replace {service_file}: it could not be backed up first.")
                    sys.exit(1)
                origin = "was not written by spindle" if unmanaged else "is being regenerated"
                print(f"{service_file} {origin}; backed it up to {backup}")
            service_file.write_text(service_content, encoding="utf-8")
            print(f"Wrote {service_file} (port {service_port}, spindle {__version__})")
            if service_home:
                print(f"Spool store baked into the unit: {service_home}")

            # Check both exit codes. Printing "Reloaded"/"Enabled" regardless
            # put spindle's success claim directly beneath systemd's error and
            # still exited 0.
            reloaded = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
            if reloaded.returncode != 0:
                print(f"systemctl daemon-reload failed (exit {reloaded.returncode}): {(reloaded.stderr or '').strip()}")
                sys.exit(1)
            print("Reloaded systemd")

            enabled = subprocess.run(["systemctl", "--user", "enable", args.name], capture_output=True, text=True)
            if enabled.returncode != 0:
                print(f"systemctl enable failed (exit {enabled.returncode}): {(enabled.stderr or '').strip()}")
                sys.exit(1)
            print(f"Enabled {args.name} service")

            # Record only once the service is actually installed and enabled. A
            # record written earlier would outlive a failed activation and
            # describe a service that was never put in place.
            _write_service_record(args.name, service_port, service_home, service_file, service_content)

            print(f"\nTo start now: spindle start --name {args.name}")
            print(f"To check status: spindle status --port {service_port}")
            print(f"To verify the install: spindle doctor --name {args.name} --port {service_port}")

        elif system == "Darwin":
            label = f"com.{args.name}.server"
            launch_agents = Path.home() / "Library" / "LaunchAgents"
            plist_file = launch_agents / f"{label}.plist"

            # Same precedence rule as the systemd branch, from the same reader.
            # This branch previously rebuilt the agent from argv alone, so a
            # reinstall without --port/--home moved it off both.
            service_port, service_home, notes, blocker = _resolve_service_settings(
                plist_file, args.port, args.home, name=args.name
            )
            if blocker:
                print(blocker, file=sys.stderr)
                sys.exit(1)
            for note in notes:
                print(note)
            plist_content = _launchd_plist_text(label, spindle_path, service_port, home=service_home, name=args.name)

            unmanaged = plist_file.exists() and not _service_file_is_marked(plist_file)
            if plist_file.exists() and not args.force:
                print(f"Plist already exists: {plist_file}")
                if unmanaged:
                    print("It does not carry spindle's marker, so spindle did not write it (or you edited it).")
                    print(f"To leave it alone: spindle install-service --name {args.name}-2 --port <other-port>")
                print("--force replaces it, keeping a timestamped backup beside it first.")
                sys.exit(1)

            launch_agents.mkdir(parents=True, exist_ok=True)
            # Back up EVERY replacement, marked or not, and before the unload —
            # so a failed load below has something to put back.
            backup = None
            if plist_file.exists():
                backup = _backup_service_file(plist_file)
                if backup is None:
                    print(f"Refusing to replace {plist_file}: it could not be backed up first.")
                    sys.exit(1)
                origin = "was not written by spindle" if unmanaged else "is being regenerated"
                print(f"{plist_file} {origin}; backed it up to {backup}")

            # Unload if already loaded. Its exit code decides what a later
            # failure means: if the unload failed, the OLD agent is still running,
            # so "no agent is loaded" would be exactly wrong.
            def _launchctl(action: str):
                """Run launchctl, turning an exec failure into a nonzero result.

                An OSError escaping here (launchctl missing, fork failure) after
                the unload would leave the machine with no agent and skip every
                restore below, so it is reported like any other failure.
                """
                try:
                    return subprocess.run(["launchctl", action, str(plist_file)], capture_output=True, text=True)
                except OSError as exc:
                    return subprocess.CompletedProcess([], 1, "", str(exc))

            was_loaded = plist_file.exists()
            unload_failed = False
            if was_loaded:
                unload_failed = _launchctl("unload").returncode != 0

            def _restore_previous(reason: str) -> None:
                """Put the old agent back after any failure past the unload."""
                print(reason)
                if backup is None:
                    return
                try:
                    shutil.copy2(backup, plist_file)
                except OSError as exc:
                    print(f"Could not restore the previous plist from {backup}: {exc}")
                    return
                if unload_failed:
                    print(f"Restored the previous plist from {backup}; it was never unloaded, so it is still running.")
                    return
                reloaded = _launchctl("load")
                if reloaded.returncode == 0:
                    print(f"Restored the previous plist from {backup} and reloaded it.")
                else:
                    print(
                        f"Restored the previous plist from {backup}, but reloading it also failed "
                        f"(exit {reloaded.returncode}): {(reloaded.stderr or '').strip()}. "
                        f"Run: launchctl load {plist_file}"
                    )

            # Everything from here has already unloaded the old agent, so every
            # failure below must put it back — not just a rejected load. A write
            # that fails on a full disk used to leave the machine with no agent,
            # a truncated plist, and a backup nothing would ever mention again.
            try:
                plist_file.write_text(plist_content, encoding="utf-8")
            except OSError as exc:
                _restore_previous(f"Could not write {plist_file}: {exc}")
                sys.exit(1)
            print(f"Wrote {plist_file} (port {service_port}, spindle {__version__})")

            # Ensure log directory exists
            try:
                (Path.home() / ".spindle").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _restore_previous(f"Could not create the log directory: {exc}")
                sys.exit(1)

            # Load the service. launchctl's exit code is the only signal that the
            # plist was accepted; announcing success without it means a plist that
            # never loads reads as a successful install.
            loaded = _launchctl("load")
            if loaded.returncode != 0:
                # Report and restore BEFORE suggesting plutil: the previous
                # ordering told the operator to lint a file that the next line
                # then reverted, so they linted the wrong plist.
                _restore_previous(f"launchctl load failed (exit {loaded.returncode}): {(loaded.stderr or '').strip()}")
                print(f"The plist that failed is the backup's replacement; check it with: plutil -lint {plist_file}")
                sys.exit(1)
            print("Loaded launchd service")

            # After the load, not before: a failed load restores the previous
            # plist, and a record written earlier would then describe settings
            # the machine is not running — so the next routine reinstall would
            # "keep" them and move the service for real.
            _write_service_record(args.name, service_port, service_home, plist_file, plist_content)

            print("\nService is now running.")
            print(f"To check status: spindle status --port {service_port}")
            print(f"To verify the install: spindle doctor --name {args.name} --port {service_port}")
            print(f"To stop: launchctl unload {plist_file}")

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

    # Log startup. Record the bound port so /health can report it — that's how a
    # client tells which of several services on a machine it just reached.
    global _server_port
    _server_port = args.port if args.http else None
    mode = f"HTTP {args.host}:{args.port}" if args.http else "stdio"
    log(f"STARTUP pid={os.getpid()} version={__version__} mode={mode} spools={SPINDLE_DIR}")

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
