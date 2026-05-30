"""Tests for Spindle MCP server."""

import asyncio
import inspect
import json
import multiprocessing
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module to test
from spindle import (
    DEFAULT_REVIEW_TIMEOUT,
    GEMINI_MODEL_ALIASES,
    KIMI_MODEL_ALIASES,
    MAX_CONCURRENT,
    PENDING_SPAWN_TIMEOUT,
    PERMISSION_PROFILES,
    _check_and_finalize_spool,
    _cleanup_shard,
    _codex_bwrap_wrap,
    _codex_respin_sync,
    _codex_spin_sync,
    _count_running,
    _detect_default_branch,
    _detect_existing_shard,
    _gemini_respin_sync,
    _gemini_spin_sync,
    _gemini_unspool_sync,
    _get_cc_bg_tasks,
    _get_harnesses,
    _get_output_path,
    _get_spool_path,
    _is_pid_alive,
    _is_review_tag,
    _kimi_respin_sync,
    _kimi_spin_sync,
    _kimi_unspool_sync,
    _list_spools,
    _monitor_spool,
    _parse_duration,
    _read_spool,
    _recover_orphans,
    _resolve_permission,
    _resolve_spool_for_respin,
    _respin_sync,
    _spawn_shard,
    _spin_sync,
    _spool_lock,
    _try_reserve_slot_and_create,
    _write_spool,
    shard_abandon,
    shard_merge,
    spin,
    spool_info,
    spool_peek,
)


class TestPermissionProfiles:
    """Test permission profile resolution."""

    def test_default_permission_is_careful(self):
        """No permission specified should default to careful."""
        tools, shard = _resolve_permission(None, None)
        assert tools == PERMISSION_PROFILES["careful"]
        assert shard is False

    def test_explicit_readonly(self):
        """Readonly permission should return readonly tools."""
        tools, shard = _resolve_permission("readonly", None)
        assert tools == PERMISSION_PROFILES["readonly"]
        assert shard is False
        assert "Read" in tools
        assert "Write" not in tools

    def test_explicit_careful(self):
        """Careful permission should return careful tools."""
        tools, shard = _resolve_permission("careful", None)
        assert tools == PERMISSION_PROFILES["careful"]
        assert shard is False
        assert "Write" in tools
        assert "Edit" in tools

    def test_full_permission(self):
        """Full permission should return None (no restrictions)."""
        tools, shard = _resolve_permission("full", None)
        assert tools is None
        assert shard is False

    def test_shard_permission(self):
        """Shard permission should enable shard flag."""
        tools, shard = _resolve_permission("shard", None)
        assert tools is None  # Full permissions
        assert shard is True

    def test_careful_plus_shard(self):
        """careful+shard should combine careful tools with shard."""
        tools, shard = _resolve_permission("careful+shard", None)
        assert tools == PERMISSION_PROFILES["careful+shard"]
        assert shard is True

    def test_explicit_allowed_tools_override(self):
        """Explicit allowed_tools should override permission profile."""
        custom_tools = "Read,Grep"
        tools, shard = _resolve_permission("full", custom_tools)
        assert tools == custom_tools
        assert shard is False  # No auto-shard when explicit tools

    def test_unknown_permission_defaults_to_careful(self):
        """Unknown permission should fall back to careful."""
        tools, shard = _resolve_permission("unknown_profile", None)
        assert tools == PERMISSION_PROFILES["careful"]
        assert shard is False

    # Regression tests for permission=shard + allowed_tools bug (finding-20260511-qsun)

    def test_shard_permission_with_explicit_allowed_tools_sets_use_shard(self):
        """permission='shard' plus explicit allowed_tools must still return use_shard=True."""
        custom_tools = "Read,Grep,Glob,Edit,Write,Bash,WebFetch,WebSearch"
        tools, shard = _resolve_permission("shard", custom_tools)
        assert tools == custom_tools
        assert shard is True

    def test_careful_plus_shard_with_explicit_allowed_tools_sets_use_shard(self):
        """permission='careful+shard' plus explicit allowed_tools must still return use_shard=True."""
        custom_tools = "Read,Grep"
        tools, shard = _resolve_permission("careful+shard", custom_tools)
        assert tools == custom_tools
        assert shard is True

    def test_readonly_permission_with_allowed_tools_no_shard(self):
        """permission='readonly' plus allowed_tools must return use_shard=False."""
        custom_tools = "Read,Grep"
        tools, shard = _resolve_permission("readonly", custom_tools)
        assert tools == custom_tools
        assert shard is False

    def test_shard_permission_without_allowed_tools_still_sets_use_shard(self):
        """permission='shard' without allowed_tools must return use_shard=True (existing behavior)."""
        tools, shard = _resolve_permission("shard", None)
        assert shard is True

    def test_research_requires_target(self, tmp_path):
        """spin with research permission must reject missing research_target before spawning."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(
            _spin("research a topic", permission="research", working_dir=str(tmp_path), skeinless=True)
        )
        assert "Error:" in result
        assert "research_target" in result

    def test_research_target_site_validates_existence(self, tmp_path):
        """site targets must be confirmed through skein before spawn."""
        completed = subprocess.CompletedProcess(["skein"], 1, stdout="", stderr="missing")
        with patch("subprocess.run", return_value=completed):
            with pytest.raises(ValueError, match="does not exist"):
                _resolve_permission(
                    "research",
                    None,
                    research_target="site:not-a-site",
                    working_dir=str(tmp_path),
                )

    def test_research_target_file_validates_parent(self, tmp_path):
        """file targets require an existing writable parent directory."""
        bad_target = tmp_path / "missing" / "report.md"
        with pytest.raises(ValueError, match="parent directory"):
            _resolve_permission(
                "research",
                None,
                research_target=f"file:{bad_target}",
                working_dir=str(tmp_path),
            )

    def test_research_target_dir_validates_path(self, tmp_path):
        """dir targets require the directory or parent directory to exist."""
        bad_target = tmp_path / "missing" / "nested" / "reports"
        with pytest.raises(ValueError, match="parent directory"):
            _resolve_permission(
                "research",
                None,
                research_target=f"dir:{bad_target}",
                working_dir=str(tmp_path),
            )

    def test_research_target_unknown_prefix_errors(self, tmp_path):
        """unknown research_target prefixes must be named in the error."""
        with pytest.raises(ValueError, match="memo"):
            _resolve_permission(
                "research",
                None,
                research_target="memo:abc123",
                working_dir=str(tmp_path),
            )

    def test_research_file_variant_adds_write_to_allowlist(self, tmp_path):
        """file research targets add Write/Edit to the base research profile."""
        tools, shard = _resolve_permission(
            "research",
            None,
            research_target=f"file:{tmp_path / 'report.md'}",
            working_dir=str(tmp_path),
        )
        assert shard is False
        assert "Write" in tools
        assert "Edit" in tools

    def test_research_site_variant_does_not_add_write(self, tmp_path):
        """site research targets keep the no-Write base profile."""
        completed = subprocess.CompletedProcess(["skein"], 0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=completed):
            tools, shard = _resolve_permission(
                "research",
                None,
                research_target="site:research-inbox",
                working_dir=str(tmp_path),
            )
        assert shard is False
        assert "Write" not in tools
        assert "Edit" not in tools

    def test_research_preamble_mentions_target(self, tmp_path):
        """spawned research prompts must include the explicit target instructions."""
        target = tmp_path / "report.md"
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    _spin_sync(
                        prompt="research this",
                        permission="research",
                        shard=False,
                        system_prompt=None,
                        working_dir=str(tmp_path),
                        allowed_tools=None,
                        tags=None,
                        model=None,
                        timeout=None,
                        skeinless=True,
                        env=None,
                        research_target=f"file:{target}",
                    )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        prompt = cmd[cmd.index("-p") + 1]
        assert "You are a research agent." in prompt
        assert f"Your output target is: file:{target}." in prompt
        assert f"Write your final report to exactly {target}" in prompt

    def test_research_shard_file_target_omits_commit_preamble(self, tmp_path):
        """file-target research shards should write the report, not commit a read-only worktree."""
        target = tmp_path / "report.md"
        captured_cmd = []
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._detect_existing_shard", return_value=shard_info):
                    with patch("spindle._has_skein", return_value=True):
                        with patch("spindle._spawn_detached", side_effect=fake_detached):
                            _spin_sync(
                                prompt="research this",
                                permission="research+shard",
                                shard=False,
                                system_prompt=None,
                                working_dir=str(tmp_path),
                                allowed_tools=None,
                                tags=None,
                                model=None,
                                timeout=None,
                                skeinless=False,
                                env=None,
                                research_target=f"file:{target}",
                            )

        prompt = captured_cmd[0][captured_cmd[0].index("-p") + 1]
        assert "Write your final report to exactly" in prompt
        assert "git commit" not in prompt
        assert "skein shard tender" not in prompt

    def test_research_shard_site_target_keeps_commit_preamble(self, tmp_path):
        """site-target research shards still keep shard commit and tender instructions."""
        captured_cmd = []
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        completed = subprocess.CompletedProcess(["skein"], 0, stdout="{}", stderr="")
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._detect_existing_shard", return_value=shard_info):
                    with patch("spindle._has_skein", return_value=True):
                        with patch("subprocess.run", return_value=completed):
                            with patch("spindle._spawn_detached", side_effect=fake_detached):
                                _spin_sync(
                                    prompt="research this",
                                    permission="research+shard",
                                    shard=False,
                                    system_prompt=None,
                                    working_dir=str(tmp_path),
                                    allowed_tools=None,
                                    tags=None,
                                    model=None,
                                    timeout=None,
                                    skeinless=False,
                                    env=None,
                                    research_target="site:research-inbox",
                                )

        prompt = captured_cmd[0][captured_cmd[0].index("-p") + 1]
        assert "You are a research agent." in prompt
        assert "git commit" in prompt
        assert "skein shard tender" in prompt

    def test_auto_permission_resolves_to_none_tools_no_shard(self):
        """auto permission: no allowlist (classifier governs), no shard."""
        tools, shard = _resolve_permission("auto", None)
        assert tools is None
        assert shard is False

    def test_auto_plus_shard_resolves_to_none_tools_with_shard(self):
        """auto+shard permission: no allowlist, shard=True."""
        tools, shard = _resolve_permission("auto+shard", None)
        assert tools is None
        assert shard is True

    def test_auto_permission_mode_in_command(self, tmp_path):
        """permission='auto' must produce --permission-mode auto, not bypassPermissions/acceptEdits."""
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    _spin_sync(
                        prompt="autonomous task",
                        permission="auto",
                        shard=False,
                        system_prompt=None,
                        working_dir=str(tmp_path),
                        allowed_tools=None,
                        tags=None,
                        model=None,
                        timeout=None,
                        skeinless=True,
                        env=None,
                    )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        pm_idx = cmd.index("--permission-mode")
        assert cmd[pm_idx + 1] == "auto"
        assert "bypassPermissions" not in cmd
        assert "acceptEdits" not in cmd

    def test_auto_plus_shard_permission_mode_in_command(self, tmp_path):
        """permission='auto+shard' must produce --permission-mode auto and use shard."""
        captured_cmd = []
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._detect_existing_shard", return_value=shard_info):
                    with patch("spindle._has_skein", return_value=True):
                        with patch("spindle._spawn_detached", side_effect=fake_detached):
                            _spin_sync(
                                prompt="autonomous task with shard",
                                permission="auto+shard",
                                shard=False,
                                system_prompt=None,
                                working_dir=str(tmp_path),
                                allowed_tools=None,
                                tags=None,
                                model=None,
                                timeout=None,
                                skeinless=True,
                                env=None,
                            )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        pm_idx = cmd.index("--permission-mode")
        assert cmd[pm_idx + 1] == "auto"
        assert "bypassPermissions" not in cmd
        assert "acceptEdits" not in cmd

    def test_auto_no_allowedtools_flag(self, tmp_path):
        """permission='auto' must not pass --allowedTools (classifier governs dynamically)."""
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    _spin_sync(
                        prompt="autonomous task",
                        permission="auto",
                        shard=False,
                        system_prompt=None,
                        working_dir=str(tmp_path),
                        allowed_tools=None,
                        tags=None,
                        model=None,
                        timeout=None,
                        skeinless=True,
                        env=None,
                    )

        assert len(captured_cmd) == 1
        assert "--allowedTools" not in captured_cmd[0]


class TestSpoolStorage:
    """Test spool file storage operations."""

    def test_spool_path_generation(self):
        """Spool path should be in spindle directory."""
        path = _get_spool_path("abc123")
        assert path.name == "abc123.json"
        assert "spindle" in str(path)

    def test_write_and_read_spool(self, tmp_path):
        """Should be able to write and read spool data."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "test123"
            data = {
                "id": spool_id,
                "status": "pending",
                "prompt": "Test prompt",
                "created_at": datetime.now().isoformat(),
            }

            _write_spool(spool_id, data)

            # Read it back
            result = _read_spool(spool_id)
            assert result is not None
            assert result["id"] == spool_id
            assert result["status"] == "pending"
            assert result["prompt"] == "Test prompt"

    def test_read_nonexistent_spool(self, tmp_path):
        """Reading nonexistent spool should return None."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _read_spool("nonexistent")
            assert result is None

    def test_write_spool_creates_directory(self, tmp_path):
        """Writing spool should create directory if needed."""
        spindle_dir = tmp_path / "nested" / "spindle"
        with patch("spindle.SPINDLE_DIR", spindle_dir):
            _write_spool("test", {"id": "test"})
            assert spindle_dir.exists()


class TestProcessUtils:
    """Test process utility functions."""

    def test_is_pid_alive_current_process(self):
        """Current process PID should be alive."""

        assert _is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_nonexistent(self):
        """Nonexistent PID should not be alive."""
        # Use a very high PID that's unlikely to exist
        assert _is_pid_alive(999999999) is False


class TestSpoolDataStructure:
    """Test spool data structure and JSON serialization."""

    def test_spool_json_structure(self, tmp_path):
        """Spool should serialize to valid JSON with expected fields."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            now = datetime.now().isoformat()
            spool = {
                "id": "abc12345",
                "status": "running",
                "prompt": "Test the code",
                "result": None,
                "session_id": None,
                "working_dir": "/tmp/test",
                "allowed_tools": "Read,Grep",
                "permission": "careful",
                "system_prompt": None,
                "tags": ["test", "unit"],
                "shard": None,
                "model": "sonnet",
                "timeout": 300,
                "created_at": now,
                "completed_at": None,
                "pid": 12345,
                "error": None,
            }

            _write_spool("abc12345", spool)

            # Read raw JSON
            path = tmp_path / "abc12345.json"
            with open(path) as f:
                raw = json.load(f)

            assert raw["id"] == "abc12345"
            assert raw["status"] == "running"
            assert raw["tags"] == ["test", "unit"]
            assert raw["model"] == "sonnet"


class TestPermissionProfileContents:
    """Test that permission profiles contain expected tools."""

    def test_readonly_has_read_tools(self):
        """Readonly should have Read, Grep, Glob but not Write."""
        readonly = PERMISSION_PROFILES["readonly"]
        assert "Read" in readonly
        assert "Grep" in readonly
        assert "Glob" in readonly
        assert "Write" not in readonly
        assert "Edit" not in readonly

    def test_careful_has_edit_tools(self):
        """Careful should have Read, Write, Edit."""
        careful = PERMISSION_PROFILES["careful"]
        assert "Read" in careful
        assert "Write" in careful
        assert "Edit" in careful
        assert "Grep" in careful

    def test_careful_has_common_bash(self):
        """Careful should allow git, make, pytest, python, npm."""
        careful = PERMISSION_PROFILES["careful"]
        assert "Bash(git:*)" in careful
        assert "Bash(make:*)" in careful
        assert "Bash(pytest:*)" in careful
        assert "Bash(python:*)" in careful
        assert "Bash(npm:*)" in careful

    def test_readonly_excludes_python_bash(self):
        """Readonly must not allow Bash(python:*) — it is an arbitrary-execution escape hatch."""
        assert "Bash(python:" not in PERMISSION_PROFILES["readonly"]

    def test_readonly_excludes_find_bash(self):
        """Readonly must not allow Bash(find:*) — find -exec is an arbitrary-execution escape hatch."""
        assert "Bash(find:" not in PERMISSION_PROFILES["readonly"]

    def test_readonly_still_includes_inspection_tools(self):
        """Readonly must still contain all genuine inspection tools."""
        readonly = PERMISSION_PROFILES["readonly"]
        for tool in ["Read", "Grep", "Glob", "Bash(git status:*)", "Bash(ls:*)", "Bash(skein:*)"]:
            assert tool in readonly, f"readonly is missing inspection tool: {tool}"

    @pytest.mark.parametrize("tool", [
        "Bash(python3:*)",
        "Bash(npx:*)",
        "Bash(node:*)",
        "Bash(ruff:*)",
        "Bash(black:*)",
        "Bash(mypy:*)",
        "Bash(pip:*)",
        "Bash(uv:*)",
    ])
    def test_careful_includes_new_dev_tools(self, tool):
        """Careful and careful+shard must include common dev tools."""
        assert tool in PERMISSION_PROFILES["careful"], f"careful missing: {tool}"
        assert tool in PERMISSION_PROFILES["careful+shard"], f"careful+shard missing: {tool}"

    @pytest.mark.parametrize("tool", [
        "Bash(ls:*)",
        "Bash(cat:*)",
        "Bash(head:*)",
        "Bash(tail:*)",
        "Bash(wc:*)",
        "Bash(diff:*)",
    ])
    def test_careful_includes_basic_unix_tools(self, tool):
        """Careful and careful+shard must include basic Unix inspection tools."""
        assert tool in PERMISSION_PROFILES["careful"], f"careful missing: {tool}"
        assert tool in PERMISSION_PROFILES["careful+shard"], f"careful+shard missing: {tool}"

    def test_careful_includes_python3(self):
        """Careful and careful+shard must include python3 (most invocations use python3 explicitly)."""
        assert "Bash(python3:*)" in PERMISSION_PROFILES["careful"]
        assert "Bash(python3:*)" in PERMISSION_PROFILES["careful+shard"]

    def test_full_and_shard_unchanged_none(self):
        """Full and shard profiles must remain None (unrestricted)."""
        assert PERMISSION_PROFILES["full"] is None
        assert PERMISSION_PROFILES["shard"] is None

    def test_research_profile_contains_web_tools(self):
        """Research must include web and narrow parsing tools."""
        research = PERMISSION_PROFILES["research"]
        for tool in ["WebFetch", "WebSearch", "Bash(curl:*)", "Bash(jq:*)"]:
            assert tool in research, f"research missing: {tool}"
            assert tool in PERMISSION_PROFILES["research+shard"], f"research+shard missing: {tool}"

    def test_research_profile_excludes_python_and_find(self):
        """Research must not include arbitrary execution escape hatches."""
        research = PERMISSION_PROFILES["research"]
        assert "Bash(python:" not in research
        assert "Bash(python3:" not in research
        assert "Bash(find:" not in research

    def test_research_profile_excludes_write_edit_at_base(self):
        """Base research profile routes output through research_target, not Write/Edit."""
        research = PERMISSION_PROFILES["research"]
        assert "Write" not in research
        assert "Edit" not in research


class TestParseDuration:
    """Test duration parsing for spin_sleep."""

    def test_parse_seconds(self):
        """Parse seconds format."""
        assert _parse_duration("30s") == 30
        assert _parse_duration("1s") == 1

    def test_parse_minutes(self):
        """Parse minutes format."""
        assert _parse_duration("90m") == 90 * 60
        assert _parse_duration("1m") == 60

    def test_parse_hours(self):
        """Parse hours format."""
        assert _parse_duration("2h") == 2 * 3600
        assert _parse_duration("1h") == 3600

    def test_parse_with_whitespace(self):
        """Handle whitespace in duration strings."""
        assert _parse_duration(" 30s ") == 30
        assert _parse_duration("  5m  ") == 5 * 60

    def test_parse_invalid_returns_none(self):
        """Invalid formats should return None."""
        assert _parse_duration("invalid") is None
        assert _parse_duration("30x") is None
        assert _parse_duration("") is None
        assert _parse_duration("abc") is None

    def test_parse_absolute_time(self):
        """Parse absolute time format (HH:MM)."""
        # Just verify it returns a positive integer
        result = _parse_duration("06:00")
        assert result is not None
        assert result > 0

    def test_parse_invalid_absolute_time(self):
        """Invalid absolute times should return None."""
        assert _parse_duration("25:00") is None
        assert _parse_duration("12:60") is None

    def test_parse_rejects_negative_values(self):
        """Negative values should be rejected (regex won't match, but test for completeness)."""
        # The regex pattern doesn't allow negative values, so these return None
        assert _parse_duration("-5s") is None
        assert _parse_duration("-10m") is None
        assert _parse_duration("-1h") is None

    def test_parse_rejects_zero(self):
        """Zero duration should be rejected."""
        assert _parse_duration("0s") is None
        assert _parse_duration("0m") is None
        assert _parse_duration("0h") is None

    def test_parse_rejects_overflow(self):
        """Values exceeding 24 hours should be rejected."""
        # 24 hours = 86400 seconds
        assert _parse_duration("86401s") is None  # 1 second over
        assert _parse_duration("1441m") is None  # 1 minute over 24h
        assert _parse_duration("25h") is None  # 1 hour over
        assert _parse_duration("999999s") is None  # Large overflow

    def test_parse_accepts_boundary_values(self):
        """Values at the boundaries should work correctly."""
        assert _parse_duration("1s") == 1  # Minimum
        assert _parse_duration("86400s") == 86400  # Maximum (24 hours)
        assert _parse_duration("1440m") == 86400  # 24 hours in minutes
        assert _parse_duration("24h") == 86400  # 24 hours


class TestSpoolLocking:
    """Test file locking for spool operations."""

    def test_lock_acquire_release(self, tmp_path):
        """Lock should be acquired and released properly."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with _spool_lock("test123", blocking=True) as acquired:
                assert acquired is True
                # Lock file should exist
                lock_path = tmp_path / "test123.lock"
                assert lock_path.exists()

    def test_nonblocking_lock_fails_when_held(self, tmp_path):
        """Non-blocking lock should fail when lock is held."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Hold the lock
            with _spool_lock("test123", blocking=True) as first:
                assert first is True
                # Try to get another non-blocking lock
                with _spool_lock("test123", blocking=False) as second:
                    assert second is False

    def test_different_spools_independent_locks(self, tmp_path):
        """Locks on different spools should be independent."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with _spool_lock("spool1", blocking=True) as first:
                assert first is True
                with _spool_lock("spool2", blocking=True) as second:
                    assert second is True


def _finalize_worker(tmp_path_str: str, spool_id: str, result_queue):
    """Worker function for concurrent finalization test."""
    import spindle

    tmp_path = Path(tmp_path_str)

    # Patch SPINDLE_DIR in this process
    with patch.object(spindle, "SPINDLE_DIR", tmp_path):
        result = _check_and_finalize_spool(spool_id)
        result_queue.put(result)


class TestConcurrentFinalization:
    """Test concurrent spool finalization with locking."""

    def test_concurrent_finalize_no_corruption(self, tmp_path):
        """Two processes finalizing same spool should not corrupt data."""
        spool_id = "concurrent_test"

        # Create a spool in running state with a dead PID
        spool = {
            "id": spool_id,
            "status": "running",
            "prompt": "Test",
            "pid": 999999999,  # Non-existent PID
            "created_at": datetime.now().isoformat(),
        }
        # Write directly to tmp_path
        (tmp_path).mkdir(parents=True, exist_ok=True)
        spool_path = tmp_path / f"{spool_id}.json"
        with open(spool_path, "w") as f:
            json.dump(spool, f)

        # Create stdout output file so it can finalize
        stdout_path = tmp_path / f"{spool_id}.stdout"
        stdout_path.write_text(json.dumps({"result": "test result"}))

        # Spawn two processes to finalize concurrently
        result_queue = multiprocessing.Queue()

        with patch("spindle.SPINDLE_DIR", tmp_path):
            p1 = multiprocessing.Process(target=_finalize_worker, args=(str(tmp_path), spool_id, result_queue))
            p2 = multiprocessing.Process(target=_finalize_worker, args=(str(tmp_path), spool_id, result_queue))

            p1.start()
            p2.start()

            p1.join(timeout=5)
            p2.join(timeout=5)

        # Collect results
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        assert len(results) == 2

        # One should return True (finalized), one should return False (lock not acquired)
        # OR both return True if one completes before the other starts
        # The key is: no crash, no corruption
        assert all(r in [True, False] for r in results)

        # Verify spool was finalized properly (status should be complete)
        with open(spool_path) as f:
            final_spool = json.load(f)
        assert final_spool["status"] == "complete"
        assert final_spool.get("result") == "test result"

    def test_finalize_returns_false_when_locked(self, tmp_path):
        """Finalize should return False if another process holds the lock."""
        import spindle

        spool_id = "lock_test"

        # Create a running spool
        spool = {
            "id": spool_id,
            "status": "running",
            "prompt": "Test",
            "pid": 999999999,
            "created_at": datetime.now().isoformat(),
        }
        (tmp_path).mkdir(parents=True, exist_ok=True)
        spool_path = tmp_path / f"{spool_id}.json"
        with open(spool_path, "w") as f:
            json.dump(spool, f)

        with patch.object(spindle, "SPINDLE_DIR", tmp_path):
            # Hold the lock
            with _spool_lock(spool_id, blocking=True) as acquired:
                assert acquired is True

                # Try to finalize - should return False immediately
                result = _check_and_finalize_spool(spool_id)
                assert result is False

            # Now without lock, it should work (though may error since no output)
            # The key is it doesn't block or corrupt


class TestConcurrencyLimit:
    """Test that concurrency limit is enforced atomically."""

    def test_try_reserve_slot_basic(self, tmp_path):
        """Basic slot reservation should work when under limit."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Mock _count_running to return 0
            with patch("spindle._count_running", return_value=0):
                success, error = _try_reserve_slot_and_create("test123")
                assert success is True
                assert error is None
                # Verify spool was created
                spool_file = tmp_path / "test123.json"
                assert spool_file.exists()

    def test_try_reserve_slot_at_limit(self, tmp_path):
        """Should reject when at max concurrent limit."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Mock _count_running to return MAX_CONCURRENT
            with patch("spindle._count_running", return_value=MAX_CONCURRENT):
                success, error = _try_reserve_slot_and_create("test123")
                assert success is False
                assert "Max" in error
                assert str(MAX_CONCURRENT) in error
                # Verify no spool was created
                spool_file = tmp_path / "test123.json"
                assert not spool_file.exists()

    def test_concurrent_reservation_respects_limit(self, tmp_path):
        """
        Regression test for TOCTOU race condition (brief-20251229-79ly).

        Simulates 20 threads trying to reserve slots concurrently.
        Only MAX_CONCURRENT should succeed, rest should be rejected.

        This tests that the file locking in _try_reserve_slot_and_create() prevents
        the race between check and spawn that allowed exceeding the limit.
        """
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Create some mock running spools to start near the limit
            initial_running = MAX_CONCURRENT - 5
            for i in range(initial_running):
                spool = {
                    "id": f"initial{i}",
                    "status": "running",
                    "created_at": datetime.now().isoformat(),
                }
                _write_spool(f"initial{i}", spool)

            # Track results from concurrent attempts
            results = {"success": [], "failure": []}
            results_lock = threading.Lock()

            def attempt_reservation(thread_id):
                """Attempt to reserve a slot and record the result."""
                spool_id = f"thread{thread_id}"
                success, error = _try_reserve_slot_and_create(spool_id, initial_status="running")

                with results_lock:
                    if success:
                        results["success"].append(thread_id)
                    else:
                        results["failure"].append(thread_id)

            # Launch 20 concurrent threads trying to reserve slots
            num_threads = 20
            threads = []
            for i in range(num_threads):
                t = threading.Thread(target=attempt_reservation, args=(i,))
                threads.append(t)

            # Start all threads at once
            for t in threads:
                t.start()

            # Wait for all to complete
            for t in threads:
                t.join(timeout=5.0)

            # Verify results
            success_count = len(results["success"])
            failure_count = len(results["failure"])

            # All threads should have completed
            assert success_count + failure_count == num_threads, (
                f"Expected {num_threads} results, got {success_count + failure_count}"
            )

            # We started with initial_running, so only (MAX_CONCURRENT - initial_running)
            # new slots should be available
            max_new_slots = MAX_CONCURRENT - initial_running

            assert success_count == max_new_slots, (
                f"Expected exactly {max_new_slots} successful reservations, got {success_count}"
            )

            # The rest should have been rejected
            expected_failures = num_threads - max_new_slots
            assert failure_count == expected_failures, f"Expected {expected_failures} rejections, got {failure_count}"

            # Verify we never exceeded the limit by checking total running
            all_spools = _list_spools()
            running_count = sum(1 for s in all_spools if s.get("status") == "running")
            assert running_count == MAX_CONCURRENT, (
                f"Expected exactly {MAX_CONCURRENT} running spools, got {running_count}"
            )

    def test_lock_file_created(self, tmp_path):
        """Lock file should be created during reservation."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                _try_reserve_slot_and_create("test123")
                lock_file = tmp_path / ".concurrency.lock"
                assert lock_file.exists()

    def test_count_running_includes_pending(self, tmp_path):
        """_count_running should count both running and pending spools."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Create mix of running and pending spools
            _write_spool("running1", {"id": "running1", "status": "running"})
            _write_spool("running2", {"id": "running2", "status": "running"})
            _write_spool("pending1", {"id": "pending1", "status": "pending"})
            _write_spool("completed1", {"id": "completed1", "status": "completed"})

            count = _count_running()
            assert count == 3  # 2 running + 1 pending


class TestShardCleanup:
    """Test shard cleanup returncode checking and logging."""

    @patch("spindle.subprocess.run")
    @patch("spindle.logger")
    def test_cleanup_shard_logs_worktree_removal_failure(self, mock_logger, mock_run):
        """Failed worktree removal should be logged and return False."""
        # Mock subprocess to return error for worktree removal
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: worktree not found"
        mock_run.return_value = mock_result

        shard_info = {"worktree_path": "/tmp/test-worktree", "branch_name": "test-branch"}

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        assert success is False
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "Failed to remove worktree" in error_msg
        assert "/tmp/test-worktree" in error_msg
        assert "test123" in error_msg
        assert "fatal: worktree not found" in error_msg

    @patch("spindle.subprocess.run")
    @patch("spindle.logger")
    def test_cleanup_shard_logs_branch_deletion_failure(self, mock_logger, mock_run):
        """Failed branch deletion should be logged but not fail cleanup."""

        # Mock subprocess: worktree removal succeeds, branch deletion fails
        def mock_run_side_effect(*args, **kwargs):
            result = MagicMock()
            cmd = args[0]
            if "worktree" in cmd and "remove" in cmd:
                result.returncode = 0
                result.stderr = ""
            elif "branch" in cmd and "-D" in cmd:
                result.returncode = 1
                result.stderr = "error: branch 'test-branch' not found"
            elif "worktree" in cmd and "prune" in cmd:
                result.returncode = 0
                result.stderr = ""
            return result

        mock_run.side_effect = mock_run_side_effect

        shard_info = {"worktree_path": "/tmp/test-worktree", "branch_name": "test-branch"}

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        # Should still succeed since worktree removal worked
        assert success is True
        # But should log warning about branch deletion
        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args_list[0][0][0]
        assert "Failed to delete branch" in warning_msg
        assert "test-branch" in warning_msg
        assert "test123" in warning_msg

    @patch("spindle.subprocess.run")
    @patch("spindle.logger")
    def test_cleanup_shard_logs_timeout(self, mock_logger, mock_run):
        """Timeout during cleanup should be logged."""
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired("git", 30)

        shard_info = {"worktree_path": "/tmp/test-worktree", "branch_name": "test-branch"}

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        assert success is False
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "Timeout during shard cleanup" in error_msg
        assert "/tmp/test-worktree" in error_msg
        assert "test123" in error_msg

    @patch("spindle.subprocess.run")
    def test_cleanup_shard_works_without_spool_id(self, mock_run):
        """Cleanup should work without spool_id for logging."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        shard_info = {"worktree_path": "/tmp/test-worktree", "branch_name": "test-branch"}

        # Should not raise exception even without spool_id
        success = _cleanup_shard(shard_info, "/tmp/repo")
        assert success is True


class TestWorktreeNameUniqueness:
    """Test that worktree names are unique even when created rapidly."""

    def test_rapid_shard_creation_unique_names(self, tmp_path):
        """
        Regression test for brief-20251229-3agj.

        Worktree names should include microseconds to prevent collisions
        when multiple shards are created in the same second.
        """
        # Create a mock git repo
        git_dir = tmp_path / "test_repo"
        git_dir.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=git_dir, capture_output=True)

        # Create initial commit
        test_file = git_dir / "test.txt"
        test_file.write_text("test")
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=git_dir, capture_output=True)

        # Create two shards rapidly (without SKEIN, using plain git worktree)
        # Mock _has_skein to return False so we use the plain git path
        with patch("spindle._has_skein", return_value=False):
            shard1, _ = _spawn_shard("test-agent-1", str(git_dir))
            shard2, _ = _spawn_shard("test-agent-2", str(git_dir))

        # Both should succeed
        assert shard1 is not None, "First shard creation failed"
        assert shard2 is not None, "Second shard creation failed"

        # Worktree names should be different
        shard1_id = shard1["shard_id"]
        shard2_id = shard2["shard_id"]
        assert shard1_id != shard2_id, f"Shard IDs collided: {shard1_id} == {shard2_id}"

        # Branch names should also be different
        assert shard1["branch_name"] != shard2["branch_name"], (
            f"Branch names collided: {shard1['branch_name']} == {shard2['branch_name']}"
        )

        # Verify both worktrees exist
        assert Path(shard1["worktree_path"]).exists(), f"Worktree 1 doesn't exist: {shard1['worktree_path']}"
        assert Path(shard2["worktree_path"]).exists(), f"Worktree 2 doesn't exist: {shard2['worktree_path']}"

        # Cleanup - remove worktrees
        subprocess.run(["git", "worktree", "remove", shard1["worktree_path"]], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "worktree", "remove", shard2["worktree_path"]], cwd=git_dir, capture_output=True)


class TestGeminiHarness:
    """Test Gemini CLI harness implementation."""

    def test_gemini_model_aliases(self):
        """Model aliases should resolve to full model names."""
        assert GEMINI_MODEL_ALIASES["flash"] == "gemini-2.5-flash"
        assert GEMINI_MODEL_ALIASES["pro"] == "gemini-2.5-pro"
        assert GEMINI_MODEL_ALIASES["3.1-pro"] == "gemini-3.1-pro-preview"
        assert GEMINI_MODEL_ALIASES["flash-lite"] == "gemini-2.5-flash-lite"

    def test_gemini_spin_resolves_alias(self, tmp_path):
        """Gemini spin should resolve model aliases in the CLI command."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _gemini_spin_sync(
                        prompt="Test",
                        working_dir=str(tmp_path),
                        model="pro",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "gemini-2.5-pro" in captured_cmd

    def test_gemini_spin_requires_working_dir(self, tmp_path):
        """Gemini spin should require working_dir."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _gemini_spin_sync(
                prompt="Test prompt",
                working_dir=None,
                model=None,
                system_prompt=None,
                timeout=None,
                tags=None,
                env=None,
            )
            assert "working_dir required" in result

    def test_gemini_spin_creates_spool(self, tmp_path):
        """Gemini spin should create spool record with correct harness metadata."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch("spindle._count_running", return_value=0):
                    result = _gemini_spin_sync(
                        prompt="Test prompt",
                        working_dir=str(tmp_path),
                        model="gemini-2.5-flash",
                        system_prompt=None,
                        timeout=60,
                        tags="test",
                        env=None,
                    )

            assert result.startswith("gemini-")

            spool_files = list(tmp_path.glob("gemini-*.json"))
            assert len(spool_files) == 1

            with open(spool_files[0]) as f:
                spool = json.load(f)

            assert spool["harness"] == "gemini"
            assert spool["prompt"] == "Test prompt"
            assert spool["model"] == "gemini-2.5-flash"
            assert spool["timeout"] == 60
            assert "gemini" in spool["tags"]
            assert "test" in spool["tags"]
            assert spool["status"] == "running"

    def test_gemini_spin_builds_correct_command(self, tmp_path):
        """Gemini spin should build the correct CLI command."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _gemini_spin_sync(
                        prompt="Explain this code",
                        working_dir=str(tmp_path),
                        model="gemini-2.5-pro",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert captured_cmd[0] == "gemini"
        assert "-p" in captured_cmd
        assert "-s" in captured_cmd
        assert "-o" in captured_cmd
        assert "json" in captured_cmd
        assert "-m" in captured_cmd
        assert "gemini-2.5-pro" in captured_cmd

    def test_gemini_spin_system_prompt_prepended(self, tmp_path):
        """System prompt should be prepended to the prompt."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _gemini_spin_sync(
                        prompt="What is 2+2?",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt="You are a math tutor",
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        # The prompt passed to -p should contain both system prompt and user prompt
        p_idx = captured_cmd.index("-p")
        combined = captured_cmd[p_idx + 1]
        assert "You are a math tutor" in combined
        assert "What is 2+2?" in combined

    def test_gemini_spin_passes_env(self, tmp_path):
        """Gemini spin should pass env to _spawn_detached."""
        captured_env = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_env.append(env)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _gemini_spin_sync(
                        prompt="Test",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"GEMINI_API_KEY": "test-key"},
                    )

        assert captured_env[0] == {"GEMINI_API_KEY": "test-key"}

    def test_gemini_research_requires_target(self, tmp_path):
        """Gemini research permission must reject missing research_target before spawn."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(
            _spin(
                "research a topic",
                harness="gemini",
                permission="research",
                working_dir=str(tmp_path),
            )
        )
        assert "Error:" in result
        assert "research_target" in result

    def test_gemini_research_preamble_injected(self, tmp_path):
        """Gemini research prompt should carry the shared research preamble."""
        target = tmp_path / "report.md"
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_spawn):
                    _gemini_spin_sync(
                        prompt="research this",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                        permission="research",
                        research_target=f"file:{target}",
                        require_research_target=True,
                    )

        prompt = captured_cmd[captured_cmd.index("-p") + 1]
        assert "You are a research agent" in prompt
        assert f"Write your final report to exactly {target}" in prompt

    def test_gemini_research_bad_prefix_errors(self, tmp_path):
        """Gemini research must reject malformed research_target prefixes."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(
            _spin(
                "research a topic",
                harness="gemini",
                permission="research",
                research_target="memo:abc123",
                working_dir=str(tmp_path),
            )
        )
        assert "Error:" in result
        assert "research_target" in result
        assert "memo" in result

    def test_gemini_unspool_complete(self, tmp_path):
        """Unspool should return result for complete spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-test123"
            spool = {
                "id": spool_id,
                "status": "complete",
                "result": "Hello from Gemini CLI",
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _gemini_unspool_sync(spool_id)
            assert result == "Hello from Gemini CLI"

    def test_gemini_unspool_error(self, tmp_path):
        """Unspool should return error message for failed spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-test456"
            spool = {
                "id": spool_id,
                "status": "error",
                "error": "gemini CLI not found",
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _gemini_unspool_sync(spool_id)
            assert "failed" in result
            assert "gemini CLI not found" in result

    def test_gemini_unspool_nonexistent(self, tmp_path):
        """Unspool should return error for nonexistent spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _gemini_unspool_sync("gemini-nonexistent")
            assert "Unknown spool_id" in result

    def test_gemini_shard_bwrap_binds_gemini_dir(self, tmp_path):
        """bwrap shard should bind ~/.gemini when it exists."""
        fake_home = tmp_path / "home"
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir(parents=True)

        worktree_path = tmp_path / "worktrees" / "gemini-bwrap-test"
        worktree_path.mkdir(parents=True)
        shard_info = {
            "worktree_path": str(worktree_path),
            "branch_name": "shard-gemini-bwrap-test",
            "shard_id": "gemini-bwrap-test",
        }

        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            with patch.object(Path, "home", return_value=fake_home):
                cmd = _codex_bwrap_wrap(
                    ["gemini", "-p", "test", "-s", "-o", "json"],
                    shard_info,
                    str(worktree_path),
                )

        gemini_dir_str = str(gemini_dir)
        bind_triple_found = any(
            cmd[i] == "--bind" and cmd[i + 1] == gemini_dir_str and cmd[i + 2] == gemini_dir_str
            for i in range(len(cmd) - 2)
        )
        assert cmd[0] == "bwrap", f"Expected bwrap wrapper, got {cmd[0]!r}"
        assert bind_triple_found, f"Expected '--bind {gemini_dir_str} {gemini_dir_str}' in bwrap cmd: {cmd!r}"


class TestKimiHarness:
    """Test Kimi CLI harness implementation."""

    def test_kimi_model_aliases(self):
        """Model aliases should resolve to full model names."""
        assert KIMI_MODEL_ALIASES["thinking"] == "moonshot-ai/kimi-k2-thinking"
        assert KIMI_MODEL_ALIASES["thinking-turbo"] == "moonshot-ai/kimi-k2-thinking-turbo"
        assert KIMI_MODEL_ALIASES["turbo"] == "moonshot-ai/kimi-k2-turbo-preview"
        assert KIMI_MODEL_ALIASES["latest"] == "moonshot-ai/kimi-k2.6"

    def test_kimi_spin_resolves_alias(self, tmp_path):
        """Kimi spin should resolve model aliases in the CLI command."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _kimi_spin_sync(
                        prompt="Test",
                        working_dir=str(tmp_path),
                        model="thinking",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "moonshot-ai/kimi-k2-thinking" in captured_cmd

    def test_kimi_spin_requires_working_dir(self, tmp_path):
        """Kimi spin should require working_dir."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _kimi_spin_sync(
                prompt="Test prompt",
                working_dir=None,
                model=None,
                system_prompt=None,
                timeout=None,
                tags=None,
                env=None,
            )
            assert "working_dir required" in result

    def test_kimi_spin_creates_spool(self, tmp_path):
        """Kimi spin should create spool record with correct harness metadata."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch("spindle._count_running", return_value=0):
                    result = _kimi_spin_sync(
                        prompt="Test prompt",
                        working_dir=str(tmp_path),
                        model="moonshot-ai/kimi-k2.5",
                        system_prompt=None,
                        timeout=60,
                        tags="test",
                        env=None,
                    )

            assert result.startswith("kimi-")

            spool_files = list(tmp_path.glob("kimi-*.json"))
            assert len(spool_files) == 1

            with open(spool_files[0]) as f:
                spool = json.load(f)

            assert spool["harness"] == "kimi"
            assert spool["prompt"] == "Test prompt"
            assert spool["model"] == "moonshot-ai/kimi-k2.5"
            assert spool["timeout"] == 60
            assert "kimi" in spool["tags"]
            assert "test" in spool["tags"]
            assert spool["status"] == "running"
            # Kimi generates session_id upfront
            assert spool["session_id"] is not None
            assert len(spool["session_id"]) == 36  # UUID length

    def test_kimi_spin_builds_correct_command(self, tmp_path):
        """Kimi spin should build the correct CLI command with session ID."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _kimi_spin_sync(
                        prompt="Explain this code",
                        working_dir=str(tmp_path),
                        model="moonshot-ai/kimi-k2-thinking-turbo",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert captured_cmd[0] == "kimi-cli"
        assert "--session" in captured_cmd
        assert "--print" in captured_cmd
        assert "--yolo" in captured_cmd
        assert "--output-format" in captured_cmd
        assert "stream-json" in captured_cmd
        assert "-p" in captured_cmd
        assert "-m" in captured_cmd
        assert "moonshot-ai/kimi-k2-thinking-turbo" in captured_cmd
        assert "-w" in captured_cmd

    def test_kimi_spin_system_prompt_prepended(self, tmp_path):
        """System prompt should be prepended to the prompt."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _kimi_spin_sync(
                        prompt="What is 2+2?",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt="You are a math tutor",
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        # The prompt passed to -p should contain both system prompt and user prompt
        p_idx = captured_cmd.index("-p")
        combined = captured_cmd[p_idx + 1]
        assert "You are a math tutor" in combined
        assert "What is 2+2?" in combined

    def test_kimi_spin_passes_env(self, tmp_path):
        """Kimi spin should pass env to _spawn_detached."""
        captured_env = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_env.append(env)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    _kimi_spin_sync(
                        prompt="Test",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"KIMI_API_KEY": "test-key"},
                    )

        assert captured_env[0] == {"KIMI_API_KEY": "test-key"}

    def test_kimi_research_requires_target(self, tmp_path):
        """Kimi research permission must reject missing research_target before spawn."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(
            _spin(
                "research a topic",
                harness="kimi",
                permission="research",
                working_dir=str(tmp_path),
            )
        )
        assert "Error:" in result
        assert "research_target" in result

    def test_kimi_research_preamble_injected(self, tmp_path):
        """Kimi research prompt should carry the shared research preamble."""
        target = tmp_path / "report.md"
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_spawn):
                    _kimi_spin_sync(
                        prompt="research this",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                        permission="research",
                        research_target=f"file:{target}",
                        require_research_target=True,
                    )

        prompt = captured_cmd[captured_cmd.index("-p") + 1]
        assert "You are a research agent" in prompt
        assert f"Write your final report to exactly {target}" in prompt

    def test_kimi_unspool_complete(self, tmp_path):
        """Unspool should return result for complete spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "kimi-test123"
            spool = {
                "id": spool_id,
                "status": "complete",
                "result": "Hello from Kimi CLI",
                "harness": "kimi",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _kimi_unspool_sync(spool_id)
            assert result == "Hello from Kimi CLI"

    def test_kimi_unspool_error(self, tmp_path):
        """Unspool should return error message for failed spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "kimi-test456"
            spool = {
                "id": spool_id,
                "status": "error",
                "error": "kimi CLI not found",
                "harness": "kimi",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _kimi_unspool_sync(spool_id)
            assert "failed" in result
            assert "kimi CLI not found" in result

    def test_kimi_unspool_nonexistent(self, tmp_path):
        """Unspool should return error for nonexistent spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _kimi_unspool_sync("kimi-nonexistent")
            assert "Unknown spool_id" in result

    def test_kimi_respin_uses_explicit_session(self, tmp_path):
        """Kimi respin should use explicit session ID from original spool."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    original_spool = {
                        "id": "kimi-original",
                        "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "working_dir": str(tmp_path),
                        "model": "moonshot-ai/kimi-k2-thinking",
                        "env": None,
                    }

                    _kimi_respin_sync(
                        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        prompt="Follow up question",
                        original_spool=original_spool,
                    )

        assert "kimi-cli" in captured_cmd
        assert "--session" in captured_cmd
        # The exact session ID should be in the command
        assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in captured_cmd
        assert "--print" in captured_cmd
        assert "Follow up question" in captured_cmd


class TestSpinHarnesses:
    """Test the spin_harnesses discovery tool."""

    def test_returns_all_harnesses(self):
        """spin_harnesses should list all four harnesses."""
        result = _get_harnesses()
        assert set(result.keys()) == {"claude-code", "codex", "gemini", "kimi"}

    def test_each_harness_has_required_keys(self):
        """Each harness entry should have models, default_model, and requires."""
        result = _get_harnesses()
        for name, info in result.items():
            assert "models" in info, f"{name} missing 'models'"
            assert "default_model" in info, f"{name} missing 'default_model'"
            assert "requires" in info, f"{name} missing 'requires'"

    def test_gemini_models_match_aliases(self):
        """Gemini models in harnesses should match GEMINI_MODEL_ALIASES."""
        result = _get_harnesses()
        assert result["gemini"]["models"] == GEMINI_MODEL_ALIASES

    def test_kimi_models_match_aliases(self):
        """Kimi models in harnesses should match KIMI_MODEL_ALIASES."""
        result = _get_harnesses()
        assert result["kimi"]["models"] == KIMI_MODEL_ALIASES

    def test_claude_code_models(self):
        """Claude Code should at least list the plain haiku/sonnet/opus aliases."""
        result = _get_harnesses()
        assert {"haiku", "sonnet", "opus"} <= set(result["claude-code"]["models"].keys())

    def test_unknown_harness_returns_error(self):
        """spin() should return error JSON for unknown harness names."""
        # spin may be a FunctionTool (with .fn) or a plain function depending on fastmcp version
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(_spin("test prompt", harness="bogus"))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "bogus" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_codex_research_site_permission_maps_to_read_only_sandbox(self, tmp_path):
        """Codex site research spins must use the read-only sandbox."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        captured = {}

        def fake_codex(prompt, working_dir, model, sandbox, timeout, tags, env, **kwargs):
            captured["sandbox"] = sandbox
            captured["kwargs"] = kwargs
            return "codex-research"

        with patch("spindle._codex_spin_sync", side_effect=fake_codex):
            result = asyncio.run(
                _spin(
                    "research a topic",
                    harness="codex",
                    permission="research",
                    research_target="site:research-inbox",
                    working_dir=str(tmp_path),
                )
            )

        assert result == "codex-research"
        assert captured["sandbox"] == "read-only"
        assert captured["kwargs"]["require_research_target"] is True

    def test_codex_research_file_permission_uses_workspace_write_with_add_dir(self, tmp_path):
        """Codex file research uses workspace-write plus a target-parent add-dir grant."""
        target = tmp_path / "report.md"
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_landlock_support", return_value=True):
                    with patch("spindle._spawn_detached", side_effect=fake_spawn):
                        result = _codex_spin_sync(
                            prompt="research a topic",
                            working_dir=str(tmp_path),
                            model=None,
                            sandbox="workspace-write",
                            timeout=None,
                            tags=None,
                            env=None,
                            research_target=f"file:{target}",
                            require_research_target=True,
                        )

        assert result.startswith("codex-")
        sandbox_idx = captured_cmd.index("--sandbox")
        assert captured_cmd[sandbox_idx + 1] == "workspace-write"
        add_dirs = [captured_cmd[i + 1] for i, tok in enumerate(captured_cmd) if tok == "--add-dir"]
        assert str(tmp_path) in add_dirs

    def test_spin_codex_research_file_maps_to_workspace_write(self, tmp_path):
        """spin() must choose workspace-write for writable Codex research targets."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        captured = {}

        def fake_codex(prompt, working_dir, model, sandbox, timeout, tags, env, **kwargs):
            captured["sandbox"] = sandbox
            captured["kwargs"] = kwargs
            return "codex-research"

        with patch("spindle._codex_spin_sync", side_effect=fake_codex):
            result = asyncio.run(
                _spin(
                    "research a topic",
                    harness="codex",
                    permission="research",
                    research_target=f"file:{tmp_path / 'report.md'}",
                    working_dir=str(tmp_path),
                )
            )

        assert result == "codex-research"
        assert captured["sandbox"] == "workspace-write"
        assert captured["kwargs"]["require_research_target"] is True


class TestSpawnFailureRecovery:
    """Test that spawn failures mark spool as error instead of leaving it pending."""

    def test_spin_sync_spawn_failure_marks_error(self, tmp_path):
        """If _spawn_detached raises, spool should be marked as error."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=OSError("No such file")):
                    result = _spin_sync(
                        prompt="test prompt",
                        permission=None,
                        shard=False,
                        system_prompt=None,
                        working_dir="/tmp",
                        allowed_tools=None,
                        tags=None,
                        model=None,
                        timeout=None,
                        skeinless=True,
                        env=None,
                    )

            # Should return an error string
            assert "Error" in result
            assert "spawn" in result.lower()

            # Spool should be marked as error, not pending
            spools = _list_spools()
            assert len(spools) == 1
            spool = spools[0]
            assert spool["status"] == "error"
            assert "spawn failed" in spool["error"]
            assert spool["completed_at"] is not None

    def test_spin_sync_spawn_failure_frees_slot(self, tmp_path):
        """After spawn failure, the slot should not count toward concurrency limit."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=OSError("No such file")):
                    _spin_sync(
                        prompt="test",
                        permission=None,
                        shard=False,
                        system_prompt=None,
                        working_dir="/tmp",
                        allowed_tools=None,
                        tags=None,
                        model=None,
                        timeout=None,
                        skeinless=True,
                        env=None,
                    )

            # Error spools should not count as running
            count = _count_running()
            assert count == 0

    def test_kimi_spin_sync_spawn_failure_marks_error(self, tmp_path):
        """If _spawn_detached raises in kimi path, spool should be marked as error."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=FileNotFoundError("kimi-cli not found")):
                    result = _kimi_spin_sync(
                        prompt="test prompt",
                        working_dir="/tmp",
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

            assert "Error" in result

            spools = _list_spools()
            assert len(spools) == 1
            spool = spools[0]
            assert spool["status"] == "error"
            assert "spawn failed" in spool["error"]


class TestRecoverOrphansPending:
    """Test that _recover_orphans cleans up stale pending spools."""

    def test_stale_pending_spool_marked_error(self, tmp_path):
        """Pending spool with no PID older than timeout should be marked error."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Create a stale pending spool (created 120 seconds ago)
            stale_time = (datetime.now() - timedelta(seconds=PENDING_SPAWN_TIMEOUT + 60)).isoformat()
            _write_spool(
                "stale1",
                {
                    "id": "stale1",
                    "status": "pending",
                    "pid": None,
                    "created_at": stale_time,
                },
            )

            _recover_orphans()

            spool = _read_spool("stale1")
            assert spool["status"] == "error"
            assert "spawn timeout" in spool["error"]
            assert spool["completed_at"] is not None

    def test_fresh_pending_spool_not_touched(self, tmp_path):
        """Pending spool within timeout should remain pending."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Create a fresh pending spool (just now)
            _write_spool(
                "fresh1",
                {
                    "id": "fresh1",
                    "status": "pending",
                    "pid": None,
                    "created_at": datetime.now().isoformat(),
                },
            )

            _recover_orphans()

            spool = _read_spool("fresh1")
            assert spool["status"] == "pending"

    def test_pending_spool_with_pid_not_touched(self, tmp_path):
        """Pending spool that has a PID should not be marked as error."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            stale_time = (datetime.now() - timedelta(seconds=PENDING_SPAWN_TIMEOUT + 60)).isoformat()
            _write_spool(
                "haspid",
                {
                    "id": "haspid",
                    "status": "pending",
                    "pid": 12345,
                    "created_at": stale_time,
                },
            )

            _recover_orphans()

            spool = _read_spool("haspid")
            assert spool["status"] == "pending"

    def test_stale_pending_frees_concurrency_slot(self, tmp_path):
        """After recovery, stale pending spool should not count toward concurrency."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            stale_time = (datetime.now() - timedelta(seconds=PENDING_SPAWN_TIMEOUT + 60)).isoformat()
            _write_spool(
                "stale2",
                {
                    "id": "stale2",
                    "status": "pending",
                    "pid": None,
                    "created_at": stale_time,
                },
            )

            # Before recovery, counts as running
            assert _count_running() == 1

            _recover_orphans()

            # After recovery, no longer counts
            assert _count_running() == 0


class TestDetectExistingShard:
    """Tests for _detect_existing_shard — reuse an existing shard worktree.

    Regression guard for brief-20260426-4swg: when --working-dir already points
    at a shard worktree, spindle must not spawn a second one.
    """

    def _make_repo_with_shard(self, tmp_path):
        """Helper: create a git repo with one shard worktree, return (repo_dir, worktree_path, branch_name)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "README.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        with patch("spindle._has_skein", return_value=False):
            shard_info, _ = _spawn_shard("test-agent", str(repo))

        assert shard_info is not None, "Shard creation failed in test setup"
        return repo, shard_info["worktree_path"], shard_info["branch_name"]

    def test_detects_shard_worktree(self, tmp_path):
        """Should return shard_info when path is under worktrees/ on a shard-* branch."""
        _repo, wt_path, branch = self._make_repo_with_shard(tmp_path)

        result = _detect_existing_shard(wt_path)

        assert result is not None, "Should detect existing shard"
        assert result["worktree_path"] == str(Path(wt_path).resolve())
        assert result["branch_name"] == branch
        assert result["shard_id"] == branch[len("shard-") :]

    def test_returns_none_for_non_worktree_path(self, tmp_path):
        """Should return None when path is a plain repo (not under worktrees/)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        result = _detect_existing_shard(str(repo))

        assert result is None, "Should not detect plain repo as shard"

    def test_returns_none_for_non_shard_branch(self, tmp_path):
        """Should return None when path is under worktrees/ but branch doesn't start with shard-."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        # Create a worktree under worktrees/ but on a non-shard branch
        wt_dir = repo / "worktrees" / "feature-branch"
        wt_dir.parent.mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(wt_dir), "-b", "feature-branch", "HEAD"],
            cwd=repo,
            capture_output=True,
        )

        result = _detect_existing_shard(str(wt_dir))

        assert result is None, "Should not detect non-shard-prefixed branch as shard"

    def test_spin_reuses_existing_shard_instead_of_spawning_new(self, tmp_path):
        """
        Repro test for brief-20260426-4swg.

        spin(..., permission="shard", working_dir=<existing-shard-worktree>) must
        place the agent in the existing worktree, not create a fresh one.
        """
        repo, wt_path, branch = self._make_repo_with_shard(tmp_path)

        spawned = []
        original_spawn = __import__("spindle")._spawn_shard

        def tracking_spawn(*args, **kwargs):
            result = original_spawn(*args, **kwargs)
            spawned.append(result)
            return result

        captured_cwd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cwd.append(cwd)
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path / "spindle_state"):
            (tmp_path / "spindle_state").mkdir()
            with patch("spindle._has_skein", return_value=False):
                with patch("spindle._spawn_shard", side_effect=tracking_spawn):
                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                        with patch("spindle._count_running", return_value=0):
                            _spin_sync(
                                "pwd && git log --oneline -1",
                                "shard",
                                False,
                                None,
                                wt_path,
                                None,
                                None,
                                None,
                                None,
                                False,
                                None,
                            )

        # _spawn_shard must NOT have been called — the existing shard was reused
        assert len(spawned) == 0, (
            f"_spawn_shard was called {len(spawned)} time(s); should be 0 when working_dir is already a shard worktree"
        )

        # The agent's cwd should be the existing worktree, not a new one
        assert len(captured_cwd) == 1
        assert str(Path(wt_path).resolve()) == str(Path(captured_cwd[0]).resolve()), (
            f"Agent cwd {captured_cwd[0]!r} does not match existing shard {wt_path!r}"
        )

    def test_returns_none_for_worktrees_in_path_but_not_under_repo_root(self, tmp_path):
        """
        Regression for detection broadness: /home/worktrees/my-project on a shard-*
        branch must NOT be detected as a shard worktree — 'worktrees' must be a
        sub-directory of the repo root, not just anywhere in the path.
        """
        # Create a standalone repo whose path happens to contain "worktrees"
        worktrees_dir = tmp_path / "worktrees"
        worktrees_dir.mkdir()
        repo = worktrees_dir / "my-project"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Create and switch to a shard-* branch
        subprocess.run(
            ["git", "checkout", "-b", "shard-fake-20260426-001"],
            cwd=repo,
            capture_output=True,
        )

        result = _detect_existing_shard(str(repo))

        assert result is None, (
            "A repo whose path contains 'worktrees' but is not under another "
            "repo's worktrees/ dir should not be detected as a shard"
        )

    def test_returns_none_for_substring_worktrees_in_path_component(self, tmp_path):
        """
        Regression for substring matching: a path component that *contains*
        the substring 'worktrees' (e.g. 'my-worktrees-copy') must not be
        treated as the worktrees/ dir. Using `git rev-parse --git-common-dir`
        anchors detection to the actual repo root, not string matching.
        """
        # Repo path component literally contains 'worktrees' as a substring
        wrap = tmp_path / "my-worktrees-copy"
        wrap.mkdir()
        repo = wrap / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        subprocess.run(
            ["git", "checkout", "-b", "shard-fake-20260426-002"],
            cwd=repo,
            capture_output=True,
        )

        # Add a worktrees/ subdirectory that is NOT actually a git worktree —
        # just a plain directory. Detection must not be fooled.
        (repo / "worktrees").mkdir()
        (repo / "worktrees" / "stash-foo").mkdir()

        # Probe both the repo root and the bogus worktrees/<name> subdir
        for probe in [repo, repo / "worktrees" / "stash-foo"]:
            result = _detect_existing_shard(str(probe))
            assert result is None, (
                f"{probe} should not be detected as a shard — path component "
                f"'my-worktrees-copy' merely contains the substring 'worktrees'"
            )

    def test_existing_shard_worktree_subdirectory(self, tmp_path):
        """
        Regression: when --working-dir points at a subdirectory inside an
        existing shard worktree, the agent must land in that subdirectory but
        shard_info['worktree_path'] must be the worktree ROOT so that
        merge/drop logic (which does .parent.parent) computes the correct
        main repo.
        """
        repo, wt_path, branch = self._make_repo_with_shard(tmp_path)

        subdir = Path(wt_path) / "src" / "deep"
        subdir.mkdir(parents=True)

        captured_cwd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cwd.append(cwd)
            return 99999

        spawn_calls = []
        original_spawn = __import__("spindle")._spawn_shard

        def tracking_spawn(*args, **kwargs):
            spawn_calls.append((args, kwargs))
            return original_spawn(*args, **kwargs)

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            with patch("spindle._has_skein", return_value=False):
                with patch("spindle._spawn_shard", side_effect=tracking_spawn):
                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                        with patch("spindle._count_running", return_value=0):
                            spool_id = _spin_sync(
                                "echo from subdir",
                                "shard",
                                False,
                                None,
                                str(subdir),
                                None,
                                None,
                                None,
                                None,
                                False,
                                None,
                            )
                            # Read the spool while SPINDLE_DIR is still patched
                            spool = _read_spool(spool_id)

        # Existing shard should be reused, not a new one spawned
        assert spawn_calls == [], (
            f"_spawn_shard called {len(spawn_calls)} time(s); existing shard "
            f"should have been reused for subdirectory cwd"
        )

        # Agent lands in the subdirectory the user pointed at
        assert len(captured_cwd) == 1
        assert Path(captured_cwd[0]).resolve() == subdir.resolve(), (
            f"Agent cwd {captured_cwd[0]!r} should be the requested subdir {str(subdir)!r}"
        )

        # shard_info['worktree_path'] is the worktree ROOT, not the subdir
        assert spool is not None
        shard_info = spool.get("shard")
        assert shard_info is not None
        assert Path(shard_info["worktree_path"]).resolve() == Path(wt_path).resolve(), (
            f"shard_info['worktree_path'] {shard_info['worktree_path']!r} should be "
            f"worktree root {wt_path!r}, not the subdirectory cwd"
        )

        # Verify merge/drop's main_repo derivation (worktrees/<name> -> repo)
        main_repo = Path(shard_info["worktree_path"]).parent.parent
        assert main_repo.resolve() == Path(repo).resolve(), (
            f"main_repo derived via .parent.parent {main_repo!r} must match the actual repo root {repo!r}"
        )

    def test_codex_spin_sync_reuses_existing_shard(self, tmp_path):
        """
        Mirror of test_spin_reuses_existing_shard_instead_of_spawning_new for
        the codex harness — same reuse semantics must hold for _codex_spin_sync.
        """
        repo, wt_path, branch = self._make_repo_with_shard(tmp_path)

        spawn_calls = []
        original_spawn = __import__("spindle")._spawn_shard

        def tracking_spawn(*args, **kwargs):
            spawn_calls.append((args, kwargs))
            return original_spawn(*args, **kwargs)

        captured_cwd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cwd.append(cwd)
            return 99999

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            with patch("spindle._has_skein", return_value=False):
                with patch("spindle._spawn_shard", side_effect=tracking_spawn):
                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                        with patch("spindle._count_running", return_value=0):
                            _codex_spin_sync(
                                "pwd",
                                wt_path,
                                None,
                                None,
                                None,
                                None,
                                None,
                                shard=True,
                            )

        assert spawn_calls == [], (
            f"_spawn_shard called {len(spawn_calls)} time(s); _codex_spin_sync "
            f"should reuse the existing shard when working_dir is already a "
            f"shard worktree"
        )
        assert len(captured_cwd) == 1
        assert str(Path(wt_path).resolve()) == str(Path(captured_cwd[0]).resolve()), (
            f"Codex agent cwd {captured_cwd[0]!r} does not match existing shard {wt_path!r}"
        )


class TestSpinSyncShardCleanupOnFailure:
    """Verify that _spin_sync cleans up newly created shards on spawn failure
    but leaves pre-existing shards untouched."""

    def _make_fake_shard_info(self, path):
        return {
            "worktree_path": str(path),
            "branch_name": "shard-test-20260426-001",
            "shard_id": "test-20260426-001",
        }

    def test_newly_created_shard_cleaned_up_on_spawn_failure(self, tmp_path):
        """When _spin_sync creates a new shard and spawn fails, _cleanup_shard is called."""
        fake_shard = self._make_fake_shard_info(tmp_path)

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                        with patch("spindle._detect_existing_shard", return_value=None):
                            with patch("spindle._spawn_detached", side_effect=OSError("boom")):
                                with patch("spindle._cleanup_shard") as mock_cleanup:
                                    _spin_sync(
                                        "do work",
                                        "shard",
                                        False,
                                        None,
                                        str(tmp_path),
                                        None,
                                        None,
                                        None,
                                        None,
                                        True,
                                        None,
                                    )
                                    mock_cleanup.assert_called_once_with(fake_shard, str(tmp_path))

    def test_preexisting_shard_not_cleaned_up_on_spawn_failure(self, tmp_path):
        """When _spin_sync reuses an existing shard and spawn fails, _cleanup_shard is NOT called."""
        fake_shard = self._make_fake_shard_info(tmp_path)

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._detect_existing_shard", return_value=fake_shard):
                        with patch("spindle._spawn_detached", side_effect=OSError("boom")):
                            with patch("spindle._cleanup_shard") as mock_cleanup:
                                _spin_sync(
                                    "do work",
                                    "shard",
                                    False,
                                    None,
                                    str(tmp_path),
                                    None,
                                    None,
                                    None,
                                    None,
                                    True,
                                    None,
                                )
                                mock_cleanup.assert_not_called()


class TestShardSpawnPreamblesAndCodexCd:
    def _make_fake_shard_info(self, path):
        return {
            "worktree_path": str(path),
            "branch_name": "shard-codex-20260503-001",
            "shard_id": Path(path).name,
        }

    def test_spin_sync_skein_preamble_omits_ready_name_flag(self, tmp_path):
        fake_shard = self._make_fake_shard_info(tmp_path / "worktrees" / "codex-20260503-001")
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=True):
                    with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                        with patch("spindle._detect_existing_shard", return_value=None):
                            with patch("spindle._spawn_detached", side_effect=fake_detached):
                                _spin_sync(
                                    "do shard work",
                                    "careful",
                                    True,
                                    None,
                                    str(tmp_path),
                                    None,
                                    None,
                                    None,
                                    None,
                                    False,
                                    None,
                                )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        prompt = cmd[cmd.index("-p") + 1]
        assert "skein ready --name" not in prompt
        assert "2. Then: skein ready" in prompt

    def test_codex_spin_sync_adds_cd_and_omits_ready_name_flag(self, tmp_path):
        fake_shard_path = tmp_path / "worktrees" / "codex-20260503-001"
        fake_shard = self._make_fake_shard_info(fake_shard_path)
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=True):
                    with patch("spindle._has_landlock_support", return_value=False):
                        with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                            with patch("spindle._detect_existing_shard", return_value=None):
                                with patch("spindle._spawn_detached", side_effect=fake_detached):
                                    _codex_spin_sync(
                                        "do codex shard work",
                                        str(tmp_path),
                                        None,
                                        None,
                                        None,
                                        None,
                                        None,
                                        shard=True,
                                    )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert "--cd" in cmd, f"Expected --cd in codex spin command, got {cmd!r}"
        cd_idx = cmd.index("--cd")
        assert cmd[cd_idx + 1] == str(fake_shard_path)
        prompt = cmd[-1]
        assert "skein ready --name" not in prompt
        assert "2. Then: skein ready" in prompt

    def test_codex_spin_sync_wraps_in_bwrap_for_shard(self, tmp_path):
        """bwrap should wrap codex commands for shards when bwrap is available."""
        fake_shard_path = tmp_path / "worktrees" / "codex-20260503-bwrap"
        fake_shard = self._make_fake_shard_info(fake_shard_path)
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._has_landlock_support", return_value=False):
                        with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                            with patch("spindle._detect_existing_shard", return_value=None):
                                with patch("shutil.which", return_value="/usr/bin/bwrap"):
                                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                                        _codex_spin_sync(
                                            "do codex shard work",
                                            str(tmp_path),
                                            None,
                                            None,
                                            None,
                                            None,
                                            None,
                                            shard=True,
                                        )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == "bwrap", f"Expected bwrap wrapper for shard, got {cmd[0]!r}"
        assert "--ro-bind" in cmd
        worktree_root = str(fake_shard_path)
        # Verify the read-write bind for the worktree exists as a specific triple,
        # not just that worktree_root appears somewhere (it also appears in --cd and --chdir).
        rw_bind_found = any(
            cmd[i] == "--bind" and cmd[i + 1] == worktree_root and cmd[i + 2] == worktree_root
            for i in range(len(cmd) - 2)
        )
        assert rw_bind_found, f"Expected '--bind {worktree_root} {worktree_root}' triple in cmd: {cmd!r}"
        assert "--dev" in cmd, f"Expected --dev in bwrap command: {cmd!r}"
        assert "--proc" in cmd, f"Expected --proc in bwrap command: {cmd!r}"
        assert "--chdir" in cmd, f"Expected --chdir in bwrap command: {cmd!r}"
        assert "codex" in cmd, f"Expected codex in bwrap-wrapped command: {cmd!r}"

    def test_codex_spin_sync_warns_when_bwrap_unavailable_for_shard(self, tmp_path, capsys):
        """When bwrap is not available for a shard, log a warning and run without it."""
        fake_shard_path = tmp_path / "worktrees" / "codex-20260503-nobwrap"
        fake_shard = self._make_fake_shard_info(fake_shard_path)
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._has_landlock_support", return_value=False):
                        with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                            with patch("spindle._detect_existing_shard", return_value=None):
                                with patch("shutil.which", return_value=None):
                                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                                        _codex_spin_sync(
                                            "do codex shard work",
                                            str(tmp_path),
                                            None,
                                            None,
                                            None,
                                            None,
                                            None,
                                            shard=True,
                                        )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == "codex", f"Expected direct codex command when bwrap missing, got {cmd[0]!r}"
        out = capsys.readouterr().out
        assert "WARNING" in out, f"Expected WARNING in output when bwrap unavailable, got: {out!r}"
        assert "bwrap" in out.lower(), f"Expected 'bwrap' in warning, got: {out!r}"


class TestShardOpsBlockedBySubdirectorySpool:
    """Regression for round-3 fell finding A: shard_merge / shard_abandon must
    block when another running spool's working_dir is a *subdirectory* of the
    worktree, not just an exact match. After the round-2 fix that lets
    --working-dir point at a subdir of an existing shard, the prior equality
    check would let merge/abandon clobber the worktree out from under a live
    spool."""

    def _setup_spools(self, tmp_path, other_working_dir):
        """Write a 'complete' spool with a shard plus an 'other' running spool
        whose working_dir is at `other_working_dir`. Returns the merge-target
        spool_id and worktree_path."""
        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        wt_path = tmp_path / "worktrees" / "shard-test-001"
        wt_path.mkdir(parents=True)
        # Mark wt_path as a directory git would consider a worktree by giving
        # it a .git file (won't actually be used — merge stops at the in-use
        # check before any git invocation).
        (wt_path / ".git").write_text("gitdir: irrelevant")

        target_id = "spool-target"
        target = {
            "id": target_id,
            "status": "complete",
            "result": "done",
            "shard": {
                "worktree_path": str(wt_path),
                "branch_name": "shard-test-001",
                "shard_id": "test-001",
            },
            "harness": "claude-code",
        }

        other_id = "spool-other-running"
        other = {
            "id": other_id,
            "status": "running",
            "working_dir": str(other_working_dir),
            "harness": "claude-code",
        }

        with patch("spindle.SPINDLE_DIR", spindle_state):
            _write_spool(target_id, target)
            _write_spool(other_id, other)

        return spindle_state, target_id, wt_path

    def test_shard_merge_blocked_by_active_spool_in_subdirectory(self, tmp_path):
        """A running spool with working_dir inside the worktree (subdir) must
        block shard_merge from cleaning up that worktree."""
        wt_path = tmp_path / "worktrees" / "shard-test-001"
        subdir = wt_path / "src" / "deep"
        spindle_state, target_id, _ = self._setup_spools(tmp_path, subdir)
        subdir.mkdir(parents=True)

        # caller_cwd is outside the worktree so we don't trip the cwd guard
        caller_cwd = str(tmp_path / "outside")
        (tmp_path / "outside").mkdir()

        _shard_merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        with patch("spindle.SPINDLE_DIR", spindle_state):
            result = asyncio.run(_shard_merge(target_id, caller_cwd=caller_cwd))

        assert "still running" in result, (
            f"shard_merge should refuse when another spool has a subdirectory "
            f"working_dir inside the worktree; got: {result!r}"
        )

    def test_shard_abandon_blocked_by_active_spool_in_subdirectory(self, tmp_path):
        """Same regression as shard_merge, for shard_abandon."""
        wt_path = tmp_path / "worktrees" / "shard-test-001"
        subdir = wt_path / "src" / "deep"
        spindle_state, target_id, _ = self._setup_spools(tmp_path, subdir)
        subdir.mkdir(parents=True)

        caller_cwd = str(tmp_path / "outside")
        (tmp_path / "outside").mkdir()

        _shard_abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
        with patch("spindle.SPINDLE_DIR", spindle_state):
            result = asyncio.run(_shard_abandon(target_id, caller_cwd=caller_cwd))

        assert "still running" in result, (
            f"shard_abandon should refuse when another spool has a subdirectory "
            f"working_dir inside the worktree; got: {result!r}"
        )


class TestCodexRespinPreservesGitAccess:
    """Regression for round-3 fell finding B: _codex_respin_sync must derive
    .git from shard_info['worktree_path'], not working_dir, so a subdirectory
    cwd doesn't lose --add-dir grants for the main repo and worktree root."""

    def test_codex_respin_subdirectory_cwd_preserves_git_access(self, tmp_path):
        # Build a real repo + worktree so the gitdir resolution exercises
        # actual filesystem state instead of mocks.
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "README.md").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        wt_path = repo / "worktrees" / "shard-codex-respin-001"
        wt_path.parent.mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "shard-codex-respin-001"],
            cwd=repo,
            capture_output=True,
        )
        # Subdirectory inside the worktree — what working_dir will point at
        subdir = wt_path / "pkg" / "deep"
        subdir.mkdir(parents=True)

        session_id = "codex-session-xyz"
        original_id = "codex-original"
        original_spool = {
            "id": original_id,
            "status": "complete",
            "session_id": session_id,
            "working_dir": str(subdir),
            "shard": {
                "worktree_path": str(wt_path),
                "branch_name": "shard-codex-respin-001",
                "shard_id": "codex-respin-001",
            },
            "harness": "codex",
            "tags": ["codex"],
        }

        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            _write_spool(original_id, original_spool)
            with patch("spindle._has_landlock_support", return_value=True):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    with patch("spindle._count_running", return_value=0):
                        _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1, "Expected one spawn for codex respin"
        cmd = captured_cmd[0]
        # Collect every --add-dir argument
        add_dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
        resolved = {str(Path(d).resolve()) for d in add_dirs}

        main_git = (repo / ".git").resolve()
        assert str(main_git) in resolved, (
            f"codex respin must --add-dir the main repo's .git ({main_git}); got {add_dirs!r}"
        )
        assert str(wt_path.resolve()) in resolved, (
            f"codex respin must --add-dir the worktree root ({wt_path}) so a "
            f"subdirectory cwd retains write access to sibling files; got {add_dirs!r}"
        )

        # Position guards: --add-dir and --cd are `codex exec` flags, not `codex exec
        # resume` flags — they must appear before the `resume` subcommand keyword.
        resume_idx = cmd.index("resume")
        add_dir_indices = [i for i, tok in enumerate(cmd) if tok == "--add-dir"]
        for idx in add_dir_indices:
            assert idx < resume_idx, (
                f"--add-dir at index {idx} must come before `resume` at index {resume_idx}; "
                f"cmd={cmd!r}"
            )
        cd_idx = next((i for i, tok in enumerate(cmd) if tok == "--cd"), None)
        assert cd_idx is not None, "--cd must be present in codex respin command"
        assert cd_idx < resume_idx, (
            f"--cd at index {cd_idx} must come before `resume` at index {resume_idx}; "
            f"cmd={cmd!r}"
        )

    def test_codex_respin_sets_cd_to_shard_worktree(self, tmp_path):
        session_id = "codex-session-cd"
        worktree_path = tmp_path / "worktrees" / "codex-respin-001"
        worktree_path.mkdir(parents=True)

        original_spool = {
            "id": "codex-original-cd",
            "status": "complete",
            "session_id": session_id,
            "working_dir": str(worktree_path / "nested"),
            "shard": {
                "worktree_path": str(worktree_path),
                "branch_name": "shard-codex-respin-001",
                "shard_id": "codex-respin-001",
            },
            "harness": "codex",
            "tags": ["codex"],
        }

        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            _write_spool(original_spool["id"], original_spool)
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._has_landlock_support", return_value=False):
                        _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1, "Expected one spawn for codex respin"
        cmd = captured_cmd[0]
        assert "--cd" in cmd, f"Expected --cd in codex respin command, got {cmd!r}"
        cd_idx = cmd.index("--cd")
        resume_idx = cmd.index("resume")
        assert cd_idx < resume_idx, f"Expected --cd before resume in codex respin command, got {cmd!r}"
        assert cmd[cd_idx + 1] == str(worktree_path)

    def _make_respin_spool(self, tmp_path, session_id, worktree_path):
        return {
            "id": "codex-original",
            "status": "complete",
            "session_id": session_id,
            "working_dir": str(worktree_path),
            "shard": {
                "worktree_path": str(worktree_path),
                "branch_name": "shard-codex-respin-001",
                "shard_id": "codex-respin-001",
            },
            "harness": "codex",
            "tags": ["codex"],
        }

    def test_codex_respin_sync_wraps_in_bwrap_for_shard(self, tmp_path):
        """bwrap wraps codex respin commands for shards when bwrap is available."""
        worktree_path = tmp_path / "worktrees" / "codex-respin-bwrap"
        worktree_path.mkdir(parents=True)
        session_id = "codex-session-bwrap"
        original_spool = self._make_respin_spool(tmp_path, session_id, worktree_path)
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            _write_spool(original_spool["id"], original_spool)
            with patch("spindle._has_landlock_support", return_value=False):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    with patch("spindle._count_running", return_value=0):
                        with patch("shutil.which", return_value="/usr/bin/bwrap"):
                            _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1, "Expected one spawn for codex respin"
        cmd = captured_cmd[0]
        assert cmd[0] == "bwrap", f"Expected bwrap wrapper for respin shard, got {cmd[0]!r}"
        assert "--ro-bind" in cmd
        worktree_root = str(worktree_path)
        rw_bind_found = any(
            cmd[i] == "--bind" and cmd[i + 1] == worktree_root and cmd[i + 2] == worktree_root
            for i in range(len(cmd) - 2)
        )
        assert rw_bind_found, f"Expected '--bind {worktree_root} {worktree_root}' in respin cmd: {cmd!r}"
        assert "--chdir" in cmd
        assert "codex" in cmd

    def test_codex_respin_sync_warns_when_bwrap_unavailable_for_shard(self, tmp_path, capsys):
        """Warning is logged when bwrap is absent for a respin shard."""
        worktree_path = tmp_path / "worktrees" / "codex-respin-nobwrap"
        worktree_path.mkdir(parents=True)
        session_id = "codex-session-nobwrap"
        original_spool = self._make_respin_spool(tmp_path, session_id, worktree_path)
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        spindle_state = tmp_path / "spindle_state"
        spindle_state.mkdir()
        with patch("spindle.SPINDLE_DIR", spindle_state):
            _write_spool(original_spool["id"], original_spool)
            with patch("spindle._has_landlock_support", return_value=False):
                with patch("spindle._spawn_detached", side_effect=fake_detached):
                    with patch("spindle._count_running", return_value=0):
                        with patch("shutil.which", return_value=None):
                            _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == "codex", f"Expected direct codex respin when bwrap missing, got {cmd[0]!r}"
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "bwrap" in out.lower()


class TestDetectDefaultBranch:
    """Tests for _detect_default_branch and improved shard error messages.

    Regression guard for brief-20260429-2ace: spindle hardcoded 'master' as
    the default base branch, causing silent failures on repos that use 'main'.
    """

    @patch("spindle.subprocess.run")
    def test_returns_main_when_origin_head_is_main(self, mock_run):
        """Returns 'main' when origin/HEAD points at main."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "refs/remotes/origin/main\n"
        mock_run.return_value = m
        assert _detect_default_branch("/any") == "main"

    @patch("spindle.subprocess.run")
    def test_returns_master_when_origin_head_is_master(self, mock_run):
        """Returns 'master' when origin/HEAD points at master."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "refs/remotes/origin/master\n"
        mock_run.return_value = m
        assert _detect_default_branch("/any") == "master"

    @patch("spindle.subprocess.run")
    def test_falls_back_to_local_main_when_no_origin_head(self, mock_run):
        """Falls back to local branch check when origin/HEAD is missing."""

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if "symbolic-ref" in cmd:
                m.returncode = 1
                m.stdout = ""
            elif "rev-parse" in cmd:
                m.returncode = 0 if "main" in cmd else 1
            return m

        mock_run.side_effect = side_effect
        assert _detect_default_branch("/any") == "main"

    @patch("spindle.subprocess.run")
    def test_returns_master_last_resort(self, mock_run):
        """Returns 'master' when no origin/HEAD and neither main nor master exists locally."""
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        mock_run.return_value = m
        assert _detect_default_branch("/any") == "master"

    def test_spawn_shard_succeeds_on_main_only_repo(self, tmp_path):
        """_spawn_shard with base_branch='main' works on a main-only repo."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Rename default branch to main regardless of git's default
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True)

        with patch("spindle._has_skein", return_value=False):
            shard_info, shard_error = _spawn_shard("test-agent", str(repo), base_branch="main")

        assert shard_error is None, f"Unexpected error: {shard_error}"
        assert shard_info is not None, "Shard creation failed on main-only repo"
        assert Path(shard_info["worktree_path"]).exists()

    @patch("spindle.subprocess.run")
    def test_spawn_shard_uses_worktree_name_for_shard_id(self, mock_run):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join(
            [
                "✓ Spawned SHARD: shard-codex-7d69d943-20260503-001",
                "Branch: shard-codex-7d69d943-20260503-001",
                "Worktree: /tmp/spindle/worktrees/codex-7d69d943-20260503-001",
            ]
        )
        mock_run.return_value = result

        with patch("spindle._has_skein", return_value=True):
            shard_info, shard_error = _spawn_shard("codex-7d69d943", "/repo", base_branch="main")

        assert shard_error is None
        assert shard_info is not None
        assert shard_info["shard_id"] == "codex-7d69d943-20260503-001"
        assert shard_info["shard_id"] != "shard-codex-7d69d943-20260503-001"

    def test_spawn_shard_error_message_names_bad_branch(self, tmp_path):
        """When shard creation fails due to invalid base branch, error names it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        with patch("spindle._has_skein", return_value=False):
            shard_info, shard_error = _spawn_shard("test-agent", str(repo), base_branch="bogus-branch-xyz")

        assert shard_info is None
        assert shard_error is not None
        assert "bogus-branch-xyz" in shard_error
        assert "--base-branch" in shard_error

    def test_spin_auto_detects_main_branch_end_to_end(self, tmp_path):
        """spin() against a main-default repo (no base_branch arg) creates a
        shard forked from main.

        This is the regression that the brief's acceptance criterion targets:
        the user-visible failure was `spin --permission shard` against a main
        repo silently failing because base_branch defaulted to 'master'. The
        unit tests for _detect_default_branch all mock subprocess and the
        existing _spawn_shard test passes base_branch='main' explicitly, so
        nothing exercises spin() -> _detect_default_branch(real_main_repo) ->
        _spawn_shard end-to-end. This test does.
        """
        repo = tmp_path / "main-repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        # Force default branch to main regardless of git's global init.defaultBranch.
        # Also delete master if it exists so any leftover hardcoded "master"
        # reference would fail loudly rather than masquerade as success.
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "branch", "-D", "master"], cwd=repo, capture_output=True)

        spool_dir = tmp_path / "spools"
        spool_dir.mkdir()

        _spin = spin.fn if hasattr(spin, "fn") else spin
        with patch("spindle.SPINDLE_DIR", spool_dir):
            with patch("spindle._has_skein", return_value=False):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._count_running", return_value=0):
                        result = asyncio.run(
                            _spin(
                                "test prompt",
                                permission="shard",
                                working_dir=str(repo),
                            )
                        )

        # Should not be an error
        assert not result.startswith("Error"), f"spin returned error: {result}"
        spool_id = result.split("\n")[0].strip()

        # Worktree must exist on disk under repo/worktrees/
        worktrees = list((repo / "worktrees").iterdir())
        assert len(worktrees) == 1, f"Expected exactly one worktree, got {worktrees}"
        worktree_path = worktrees[0]
        assert worktree_path.is_dir()

        # The shard branch must descend from main (auto-detected). If
        # base_branch had defaulted to 'master', _spawn_shard would have
        # failed because we deleted master. Verify by checking merge-base.
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        ).stdout.strip()
        main_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head_sha == main_sha, (
            f"Shard HEAD {head_sha} does not match main {main_sha} — shard was not forked from main"
        )

        # Spool record should have base_branch persisted as 'main' so retries
        # don't fall back to literal 'master'.
        spool_path = spool_dir / f"{spool_id}.json"
        assert spool_path.exists()
        spool = json.loads(spool_path.read_text())
        assert spool.get("base_branch") == "main", (
            f"Expected spool.base_branch='main', got {spool.get('base_branch')!r}"
        )


class TestShardFailLoud:
    """Shard creation failures must surface loudly — never silent fall-through.

    Regression guard for friction-20260507-i39h: when worktree creation fails,
    spindle was returning (None, None) from _spawn_shard and using a generic
    error in _spin_sync instead of the underlying git/skein error. In the worst
    case a SKEIN non-zero was silently swallowed so the git fallback ran with
    the same bad branch and also failed, still with no useful message.
    """

    def test_spawn_shard_git_error_non_invalid_reference(self, tmp_path):
        """When git worktree add fails for a reason other than 'invalid reference',
        the full stderr is returned rather than (None, None)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            if "worktree" in cmd and "add" in cmd:
                m.returncode = 128
                m.stderr = "fatal: something unexpected went wrong\n"
                m.stdout = ""
            else:
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("spindle._has_skein", return_value=False):
            with patch("spindle.subprocess.run", side_effect=fake_run):
                shard_info, shard_error = _spawn_shard("agent", str(repo), base_branch="master")

        assert shard_info is None
        assert shard_error is not None, "Expected an error message, got None"
        assert "something unexpected went wrong" in shard_error

    def test_spawn_shard_skein_fails_git_succeeds(self, tmp_path):
        """When skein shard spawn returns non-zero, _spawn_shard falls through to git
        and still creates the worktree successfully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "skein":
                m = MagicMock()
                m.returncode = 1
                m.stderr = "skein: base branch not found\n"
                m.stdout = ""
                return m
            return real_run(cmd, **kwargs)

        with patch("spindle._has_skein", return_value=True):
            with patch("spindle.subprocess.run", side_effect=fake_run):
                shard_info, shard_error = _spawn_shard("agent", str(repo), base_branch="master")

        assert shard_error is None, f"Expected no error, got: {shard_error}"
        assert shard_info is not None, "Git fallback should have created the shard"
        assert Path(shard_info["worktree_path"]).exists()

    def test_spawn_shard_skein_and_git_both_fail_error_has_both(self, tmp_path):
        """When both skein and git fail, the returned error message contains details
        from both so the caller can diagnose the root cause."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            if cmd[0] == "skein":
                m.returncode = 1
                m.stderr = "skein spawn error detail\n"
                m.stdout = ""
            elif "worktree" in cmd:
                m.returncode = 128
                m.stderr = "git worktree error detail\n"
                m.stdout = ""
            else:
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("spindle._has_skein", return_value=True):
            with patch("spindle.subprocess.run", side_effect=fake_run):
                shard_info, shard_error = _spawn_shard("agent", str(repo), base_branch="master")

        assert shard_info is None
        assert shard_error is not None
        assert "skein spawn error detail" in shard_error
        assert "git worktree error detail" in shard_error

    def test_spin_shard_failure_returns_error_not_success(self, tmp_path):
        """When shard creation fails, spin() returns an Error: string and the agent
        does NOT run. The spool must not be started on the main checkout."""
        spool_dir = tmp_path / "spools"
        spool_dir.mkdir()
        spawn_called = []

        def tracking_spawn(*args, **kwargs):
            spawn_called.append(True)
            return 99999

        with patch("spindle.SPINDLE_DIR", spool_dir):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._spawn_shard", return_value=(None, "worktree creation bombed")):
                        with patch("spindle._spawn_detached", side_effect=tracking_spawn):
                            result = _spin_sync(
                                prompt="do work",
                                permission="shard",
                                shard=False,
                                system_prompt=None,
                                working_dir=str(tmp_path),
                                allowed_tools=None,
                                tags=None,
                                model=None,
                                timeout=None,
                                skeinless=True,
                                env=None,
                            )

        assert result.startswith("Error"), f"Expected Error: return, got: {result!r}"
        assert "worktree creation bombed" in result
        assert not spawn_called, "Agent was spawned despite shard creation failure — silent fall-through!"

    def test_shard_with_allowed_tools_uses_shard_not_main_repo(self, tmp_path):
        """Regression for finding-20260511-qsun: permission='shard' + allowed_tools
        must set use_shard=True so the agent gets an isolated worktree, not main repo.

        Before the fix, _resolve_permission returned (allowed_tools, False) when
        allowed_tools was set, silently bypassing shard creation.
        """
        spool_dir = tmp_path / "spools"
        spool_dir.mkdir()
        spawn_called = []

        def tracking_spawn(*args, **kwargs):
            spawn_called.append(True)
            return 99999

        with patch("spindle.SPINDLE_DIR", spool_dir):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._detect_existing_shard", return_value=None):
                        with patch("spindle._spawn_shard", return_value=(None, "shard bombed")):
                            with patch("spindle._spawn_detached", side_effect=tracking_spawn):
                                result = _spin_sync(
                                    prompt="do work",
                                    permission="shard",
                                    shard=False,
                                    system_prompt=None,
                                    working_dir=str(tmp_path),
                                    allowed_tools="Read,Grep,Glob,Edit,Write,Bash",
                                    tags=None,
                                    model=None,
                                    timeout=None,
                                    skeinless=True,
                                    env=None,
                                )

        # With the fix, use_shard=True so _spawn_shard is called and its failure surfaces
        assert result.startswith("Error"), f"Expected Error: return, got: {result!r}"
        assert "SHARD" in result or "shard bombed" in result
        assert not spawn_called, "Agent launched in main repo despite permission='shard'!"

    def test_shard_none_handled_by_early_return(self, tmp_path):
        """When _spawn_shard returns (None, None), _spin_sync returns an error string
        via the early-return at the shard creation block and does not launch the agent.
        """
        spool_dir = tmp_path / "spools"
        spool_dir.mkdir()
        spawn_called = []

        def tracking_spawn(*args, **kwargs):
            spawn_called.append(True)
            return 99999

        with patch("spindle.SPINDLE_DIR", spool_dir):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._detect_existing_shard", return_value=None):
                        with patch("spindle._spawn_shard", return_value=(None, None)):
                            with patch("spindle._spawn_detached", side_effect=tracking_spawn):
                                result = _spin_sync(
                                    prompt="do work",
                                    permission="shard",
                                    shard=False,
                                    system_prompt=None,
                                    working_dir=str(tmp_path),
                                    allowed_tools=None,
                                    tags=None,
                                    model=None,
                                    timeout=None,
                                    skeinless=True,
                                    env=None,
                                )

        assert "SHARD" in result or "Error" in result, f"Expected shard error, got: {result!r}"
        assert not spawn_called, "Agent launched in main repo when shard_info was None!"


class TestReviewTagTimeout:
    """Review-tagged spools get a soft default timeout (friction-20260505-b87l)."""

    def test_review_tag_applies_soft_timeout(self, tmp_path):
        """A spool tagged 'review' with no explicit timeout gets DEFAULT_REVIEW_TIMEOUT."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._monitor_spool"):
                        _spin_sync(
                            prompt="review task",
                            permission="readonly",
                            shard=False,
                            system_prompt=None,
                            working_dir="/tmp",
                            allowed_tools=None,
                            tags="review",
                            model=None,
                            timeout=None,
                            skeinless=True,
                            env=None,
                        )

            spools = _list_spools()
            assert len(spools) == 1
            assert spools[0]["timeout"] == DEFAULT_REVIEW_TIMEOUT

    def test_fell_r1_tag_applies_soft_timeout(self, tmp_path):
        """A spool tagged 'fell-r1' gets the review soft timeout."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._monitor_spool"):
                        _spin_sync(
                            prompt="fell review pass",
                            permission="readonly",
                            shard=False,
                            system_prompt=None,
                            working_dir="/tmp",
                            allowed_tools=None,
                            tags="fell-r1,batch-3",
                            model=None,
                            timeout=None,
                            skeinless=True,
                            env=None,
                        )

            spools = _list_spools()
            assert len(spools) == 1
            assert spools[0]["timeout"] == DEFAULT_REVIEW_TIMEOUT

    def test_non_review_tag_no_soft_timeout(self, tmp_path):
        """A spool tagged with something other than a review marker gets no default timeout."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._monitor_spool"):
                        _spin_sync(
                            prompt="regular task",
                            permission=None,
                            shard=False,
                            system_prompt=None,
                            working_dir="/tmp",
                            allowed_tools=None,
                            tags="batch-1,triage",
                            model=None,
                            timeout=None,
                            skeinless=True,
                            env=None,
                        )

            spools = _list_spools()
            assert len(spools) == 1
            assert spools[0]["timeout"] is None

    def test_untagged_spool_no_timeout(self, tmp_path):
        """A spool with no tags gets no default timeout."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._monitor_spool"):
                        _spin_sync(
                            prompt="plain task",
                            permission=None,
                            shard=False,
                            system_prompt=None,
                            working_dir="/tmp",
                            allowed_tools=None,
                            tags=None,
                            model=None,
                            timeout=None,
                            skeinless=True,
                            env=None,
                        )

            spools = _list_spools()
            assert len(spools) == 1
            assert spools[0]["timeout"] is None

    def test_explicit_timeout_not_overridden_by_review_tag(self, tmp_path):
        """An explicit timeout takes precedence over the review-tag soft default."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", return_value=12345):
                    with patch("spindle._monitor_spool"):
                        _spin_sync(
                            prompt="review with custom timeout",
                            permission="readonly",
                            shard=False,
                            system_prompt=None,
                            working_dir="/tmp",
                            allowed_tools=None,
                            tags="review",
                            model=None,
                            timeout=300,
                            skeinless=True,
                            env=None,
                        )

            spools = _list_spools()
            assert len(spools) == 1
            assert spools[0]["timeout"] == 300

    def test_review_tag_detection(self):
        """_is_review_tag matches 'review' and any fell-rN without a cap."""
        assert _is_review_tag("review")
        # All finite fell rounds match
        for i in range(1, 8):  # includes 6 and 7 to confirm no cap at 5
            assert _is_review_tag(f"fell-r{i}")
        # Unrelated tags do not match
        assert not _is_review_tag("batch-1")
        assert not _is_review_tag("fell-notanumber")
        assert not _is_review_tag("triage")

    def test_monitor_spool_kills_timed_out_process(self, tmp_path):
        """_monitor_spool sends SIGTERM to a process that exceeds its timeout."""
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
        )
        spool_id = "test-timeout-kill"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle.MONITOR_POLL_INTERVAL", 0.1):
                # created_at is 5s in the past so the 1s timeout is already expired
                spool = {
                    "id": spool_id,
                    "status": "running",
                    "pid": proc.pid,
                    "timeout": 1,
                    "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
                    "prompt": "test",
                }
                _write_spool(spool_id, spool)
                _monitor_spool(spool_id)

                pid_alive_after = _is_pid_alive(proc.pid)
                result = _read_spool(spool_id)

        assert not pid_alive_after, "process should have been killed by _monitor_spool"
        assert result["status"] == "timeout"
        assert "Timeout" in result["error"]


class TestCCBgTasks:
    """Tests for _get_cc_bg_tasks and bg-task surfacing in spool_info/spool_peek."""

    def test_get_cc_bg_tasks_no_directory(self, tmp_path):
        """Returns empty list when the session tasks directory doesn't exist."""
        with patch("spindle.CLAUDE_TASKS_DIR", tmp_path):
            result = _get_cc_bg_tasks("nonexistent-session")
        assert result == []

    def test_get_cc_bg_tasks_empty_directory(self, tmp_path):
        """Returns empty list for an empty session directory."""
        (tmp_path / "sess1").mkdir()
        with patch("spindle.CLAUDE_TASKS_DIR", tmp_path):
            result = _get_cc_bg_tasks("sess1")
        assert result == []

    def test_get_cc_bg_tasks_reads_json_files(self, tmp_path):
        """Returns parsed task dicts from numbered JSON files."""
        sess_dir = tmp_path / "sess1"
        sess_dir.mkdir()
        (sess_dir / "1.json").write_text(json.dumps({"id": "1", "subject": "spin agents", "status": "completed"}))
        (sess_dir / "2.json").write_text(json.dumps({"id": "2", "subject": "wait loop", "status": "running"}))

        with patch("spindle.CLAUDE_TASKS_DIR", tmp_path):
            result = _get_cc_bg_tasks("sess1")

        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert result[1]["status"] == "running"

    def test_get_cc_bg_tasks_skips_invalid_json(self, tmp_path):
        """Silently skips files that are not valid JSON."""
        sess_dir = tmp_path / "sess2"
        sess_dir.mkdir()
        (sess_dir / "1.json").write_text("not json{{{")
        (sess_dir / "2.json").write_text(json.dumps({"id": "2", "status": "completed"}))

        with patch("spindle.CLAUDE_TASKS_DIR", tmp_path):
            result = _get_cc_bg_tasks("sess2")

        assert len(result) == 1
        assert result[0]["id"] == "2"

    async def test_spool_info_surfaces_bg_tasks(self, tmp_path):
        """spool_info includes _bg_tasks when cc bg tasks exist."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        sess_dir = tasks_dir / "mysession"
        sess_dir.mkdir()
        (sess_dir / "1.json").write_text(json.dumps({"id": "1", "subject": "wait loop", "status": "running"}))

        spool_data = {
            "id": "abc123",
            "status": "running",
            "harness": "claude-code",
            "session_id": "mysession",
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("abc123", spool_data)
            with patch("spindle.CLAUDE_TASKS_DIR", tasks_dir):
                result = await spool_info.fn("abc123")

        data = json.loads(result)
        assert "_bg_tasks" in data
        assert len(data["_bg_tasks"]) == 1
        assert data["_bg_tasks"][0]["subject"] == "wait loop"
        assert data["_bg_tasks_incomplete"] == 1

    async def test_spool_info_no_bg_tasks_key_when_none(self, tmp_path):
        """spool_info does not include _bg_tasks when there are none."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        spool_data = {
            "id": "abc124",
            "status": "running",
            "harness": "claude-code",
            "session_id": "no-tasks-session",
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("abc124", spool_data)
            with patch("spindle.CLAUDE_TASKS_DIR", tasks_dir):
                result = await spool_info.fn("abc124")

        data = json.loads(result)
        assert "_bg_tasks" not in data

    async def test_spool_peek_falls_back_to_bg_tasks_when_empty(self, tmp_path):
        """spool_peek returns bg task info when stdout is empty and tasks exist."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        sess_dir = tasks_dir / "mysession"
        sess_dir.mkdir()
        (sess_dir / "1.json").write_text(json.dumps({"id": "1", "subject": "pgrep loop", "status": "running", "activeForm": "waiting for pytest"}))

        spool_data = {
            "id": "peek1",
            "status": "running",
            "harness": "claude-code",
            "session_id": "mysession",
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("peek1", spool_data)
            stdout_path = _get_output_path("peek1")
            stdout_path.write_text("")
            with patch("spindle.CLAUDE_TASKS_DIR", tasks_dir):
                result = await spool_peek.fn("peek1")

        assert "background tasks" in result
        assert "pgrep loop" in result

    async def test_spool_peek_falls_back_to_bg_tasks_when_no_stdout(self, tmp_path):
        """spool_peek returns bg task info when stdout doesn't exist and tasks exist."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        sess_dir = tasks_dir / "mysession2"
        sess_dir.mkdir()
        (sess_dir / "1.json").write_text(json.dumps({"id": "1", "subject": "wait task", "status": "running"}))

        spool_data = {
            "id": "peek2",
            "status": "running",
            "harness": "claude-code",
            "session_id": "mysession2",
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("peek2", spool_data)
            with patch("spindle.CLAUDE_TASKS_DIR", tasks_dir):
                result = await spool_peek.fn("peek2")

        assert "background tasks" in result
        assert "wait task" in result

    async def test_spool_peek_normal_output_not_replaced(self, tmp_path):
        """spool_peek returns normal stdout when output exists and is non-empty."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        spool_data = {
            "id": "peek3",
            "status": "running",
            "harness": "claude-code",
            "session_id": "sess3",
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("peek3", spool_data)
            stdout_path = _get_output_path("peek3")
            stdout_path.write_text("normal agent output line\n")
            with patch("spindle.CLAUDE_TASKS_DIR", tasks_dir):
                result = await spool_peek.fn("peek3")

        assert "normal agent output line" in result
        assert "background tasks" not in result


class TestRespinHandleResolution:
    """Bug fix (brief-20260519-guj8): respin must accept the spool_id
    returned by spin() (the natural handle every other entrypoint takes),
    not only a raw session_id. The spool's real session_id must be what
    flows down to the harness resume path - never the caller's raw handle,
    which may be a spool_id that the harness resume command can't use.
    """

    # All four harnesses route through _respin_sync.
    HARNESS_PARAMS = [
        ("codex", "codex-abcd1234", "019e3e07-0ee2-7770-bebc-7acbf2b14542"),
        ("gemini", "gemini-abcd1234", "gemini-thread-uuid-001"),
        ("kimi", "kimi-abcd1234", "kimi-thread-uuid-001"),
        ("claude-code", "ab12cd34", "claude-session-uuid-001"),
    ]

    @pytest.mark.parametrize("harness,spool_id,session_id", HARNESS_PARAMS)
    def test_resolver_finds_by_session_id(self, tmp_path, harness, spool_id, session_id):
        """Backward compat: resolving by raw session_id still works."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "complete", "session_id": session_id, "harness": harness},
            )
            resolved = _resolve_spool_for_respin(session_id)
        assert resolved is not None
        assert resolved["id"] == spool_id

    @pytest.mark.parametrize("harness,spool_id,session_id", HARNESS_PARAMS)
    def test_resolver_finds_by_spool_id(self, tmp_path, harness, spool_id, session_id):
        """New: resolving by the spool_id returned by spin() works."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "complete", "session_id": session_id, "harness": harness},
            )
            resolved = _resolve_spool_for_respin(spool_id)
        assert resolved is not None
        assert resolved["id"] == spool_id
        assert resolved["session_id"] == session_id

    def test_resolver_returns_none_for_unknown_handle(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            assert _resolve_spool_for_respin("does-not-exist") is None

    def test_respin_codex_by_spool_id_passes_real_thread_id(self, tmp_path):
        """The original reproduction: respin(<spool_id>) for a completed
        codex spool must resolve, and the codex resume path must receive
        the codex thread-uuid, not the spool_id."""
        spool_id = "codex-3a3d2c48"
        thread_id = "019e3e07-0ee2-7770-bebc-7acbf2b14542"
        captured = {}

        def fake_codex_respin(session_id, prompt):
            captured["session_id"] = session_id
            captured["prompt"] = prompt
            return "codex-newspool"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "complete", "session_id": thread_id, "harness": "codex"},
            )
            with patch("spindle._codex_respin_sync", side_effect=fake_codex_respin):
                result = _respin_sync(spool_id, "continue please")

        assert result == "codex-newspool"
        assert captured["session_id"] == thread_id, (
            f"codex resume must receive the thread-uuid, got {captured['session_id']!r} "
            f"(the spool_id {spool_id!r} would fail `codex exec resume`)"
        )
        assert captured["prompt"] == "continue please"

    def test_respin_codex_by_session_id_backward_compat(self, tmp_path):
        """Existing session_id callers must keep working unchanged."""
        spool_id = "codex-3a3d2c48"
        thread_id = "019e3e07-0ee2-7770-bebc-7acbf2b14542"
        captured = {}

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "complete", "session_id": thread_id, "harness": "codex"},
            )
            with patch(
                "spindle._codex_respin_sync",
                side_effect=lambda sid, p: captured.update(session_id=sid) or "codex-new",
            ):
                result = _respin_sync(thread_id, "more work")

        assert result == "codex-new"
        assert captured["session_id"] == thread_id

    @pytest.mark.parametrize(
        "harness,patch_target",
        [
            ("codex", "spindle._codex_respin_sync"),
            ("gemini", "spindle._gemini_respin_sync"),
            ("kimi", "spindle._kimi_respin_sync"),
        ],
    )
    def test_respin_passes_resolved_session_to_each_harness(self, tmp_path, harness, patch_target):
        spool_id = f"{harness}-abcd1234"
        real_session = f"{harness}-real-thread-uuid"
        captured = {}

        def fake(session_id, prompt, *rest):
            captured["session_id"] = session_id
            return f"{harness}-new"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "session_id": real_session,
                    "harness": harness,
                    "working_dir": str(tmp_path),
                },
            )
            with patch(patch_target, side_effect=fake):
                result = _respin_sync(spool_id, "go")

        assert result == f"{harness}-new"
        assert captured["session_id"] == real_session

    def test_respin_claude_code_resumes_with_resolved_session_id(self, tmp_path):
        """claude-code branch is inline; the resolved session_id must reach
        `claude --resume <session_id>`, not the spool_id handle."""
        spool_id = "ab12cd34"
        session_id = "claude-session-uuid-001"
        captured_cmd = []

        def fake_detached(sid, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 4242

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "complete", "session_id": session_id, "harness": "claude-code"},
            )
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        result = _respin_sync(spool_id, "keep going")

        assert not result.startswith("Error"), result
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == session_id, (
            f"claude resume must use the resolved session_id {session_id!r}, "
            f"not the spool_id {spool_id!r}; got {cmd!r}"
        )

    def test_respin_running_spool_no_session_distinct_error(self, tmp_path):
        """A running spool whose thread_id hasn't been parsed yet must give
        a clear distinct error, not the misleading 'No spool found'."""
        spool_id = "codex-running1"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "running", "session_id": None, "harness": "codex"},
            )
            result = _respin_sync(spool_id, "continue")

        assert "not in a resumable state" in result
        assert "status=running" in result
        assert "No spool found" not in result

    def test_respin_running_spool_with_session_id_set_distinct_error(self, tmp_path):
        """A spool that is status=running but already has session_id set
        (codex sets it mid-stream from the thread_id event while the original
        process is still working) must NOT flow to the harness resume path -
        that would be a concurrent resume of a live session. It must return
        the distinct running error, and no harness resume fn may be called.
        """
        spool_id = "codex-runwithsess"
        thread_id = "019e3e07-0ee2-7770-bebc-7acbf2b14542"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "session_id": thread_id,
                    "harness": "codex",
                },
            )
            with patch("spindle._codex_respin_sync") as codex_resume, patch(
                "spindle._gemini_respin_sync"
            ) as gemini_resume, patch("spindle._kimi_respin_sync") as kimi_resume, patch(
                "spindle._spawn_detached"
            ) as spawn:
                result = _respin_sync(spool_id, "continue")

        assert "not in a resumable state" in result
        assert "status=running" in result
        assert "No spool found" not in result
        codex_resume.assert_not_called()
        gemini_resume.assert_not_called()
        kimi_resume.assert_not_called()
        spawn.assert_not_called()

    def test_respin_timeout_spool_with_session_proceeds(self, tmp_path):
        """Regression: _monitor_spool sets status='timeout' on a wall-clock
        kill (terminal - process dead, completed_at set). codex/claude-code
        commonly have session_id set mid-stream before that kill. The
        deny-list guard `status not in ('complete', 'error')` misclassified
        'timeout' as non-terminal and blocked the legitimate "my run timed
        out, continue it" path. The non-terminal allow-list lets it through:
        the harness resume fn MUST be called with the resolved session_id.

        This FAILS against edb6d00 (blocked with the running error) and
        PASSES after the allow-list inversion.
        """
        spool_id = "codex-timedout1"
        thread_id = "019e3e07-0ee2-7770-bebc-7acbf2b14542"
        captured = {}

        def fake_codex_respin(session_id, prompt):
            captured["session_id"] = session_id
            captured["prompt"] = prompt
            return "codex-resumed"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "timeout",
                    "session_id": thread_id,
                    "harness": "codex",
                    "working_dir": str(tmp_path),
                },
            )
            with patch("spindle._codex_respin_sync", side_effect=fake_codex_respin):
                result = _respin_sync(spool_id, "continue after timeout")

        assert result == "codex-resumed", result
        assert captured["session_id"] == thread_id, (
            f"a timed-out spool with a session must resume against its real "
            f"session_id, got {captured.get('session_id')!r}"
        )
        assert captured["prompt"] == "continue after timeout"

    def test_respin_timeout_spool_without_session_distinct_error(self, tmp_path):
        """A timed-out spool with no session_id is terminal-but-unresumable:
        it must fall through to the 'completed without a resumable session'
        error (carrying status=timeout), not the non-terminal running error
        and not 'No spool found'."""
        spool_id = "codex-timedout2"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "timeout",
                    "session_id": None,
                    "harness": "codex",
                },
            )
            result = _respin_sync(spool_id, "continue")

        assert "completed without a resumable session" in result
        assert "status=timeout" in result
        assert "not in a resumable state" not in result
        assert "No spool found" not in result

    @pytest.mark.parametrize(
        "harness,patch_target",
        [
            ("codex", "spindle._codex_respin_sync"),
            ("gemini", "spindle._gemini_respin_sync"),
            ("kimi", "spindle._kimi_respin_sync"),
        ],
    )
    def test_respin_terminal_spool_with_session_proceeds(self, tmp_path, harness, patch_target):
        """Happy path not regressed by the status guard: a terminal spool
        (status=complete) with a session_id resolves and reaches the harness
        resume path normally."""
        spool_id = f"{harness}-doneabcd"
        real_session = f"{harness}-real-thread-uuid"
        captured = {}

        def fake(session_id, prompt, *rest):
            captured["session_id"] = session_id
            return f"{harness}-new"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "session_id": real_session,
                    "harness": harness,
                    "working_dir": str(tmp_path),
                },
            )
            with patch(patch_target, side_effect=fake):
                result = _respin_sync(spool_id, "go")

        assert result == f"{harness}-new"
        assert captured["session_id"] == real_session

    def test_respin_unknown_handle_error(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _respin_sync("totally-unknown", "hi")
        assert "No spool found for handle" in result


class TestBaseBranchDefaultIsNone:
    """Regression guard for brief-20260520-2wfo: three functions that previously
    defaulted base_branch to 'master' now default to None and auto-detect inside.

    Each signature test FAILS against an inversion where the default is 'master'
    (inspect reports a str default, not None). Each behaviour test FAILS against
    an inversion where the function ignores the None default and uses 'master'
    literally (main-only fixture repo would raise an error because 'master'
    doesn't exist).
    """

    def test_spawn_shard_default_param_is_none(self):
        sig = inspect.signature(_spawn_shard)
        default = sig.parameters["base_branch"].default
        assert default is None, (
            f"_spawn_shard base_branch default must be None, got {default!r}"
        )

    def test_spin_sync_default_param_is_none(self):
        sig = inspect.signature(_spin_sync)
        default = sig.parameters["base_branch"].default
        assert default is None, (
            f"_spin_sync base_branch default must be None, got {default!r}"
        )

    def test_codex_spin_sync_default_param_is_none(self):
        sig = inspect.signature(_codex_spin_sync)
        default = sig.parameters["base_branch"].default
        assert default is None, (
            f"_codex_spin_sync base_branch default must be None, got {default!r}"
        )

    def test_spawn_shard_falls_back_to_detect(self, tmp_path):
        """Calling _spawn_shard without base_branch on a main-only repo succeeds.

        Fails against inversion: if default stayed 'master', git worktree add
        would fail (master deleted) and shard_error would be non-None.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "branch", "-D", "master"], cwd=repo, capture_output=True)

        with patch("spindle._has_skein", return_value=False):
            shard_info, shard_error = _spawn_shard("test-agent", str(repo))

        assert shard_error is None, f"Expected no error, got: {shard_error}"
        assert shard_info is not None, "Shard creation should succeed on main-only repo"
        assert Path(shard_info["worktree_path"]).exists()

    def test_worktree_creation_error_names_branch(self, tmp_path):
        """When git worktree add fails due to a missing branch, error names the branch.

        Fails against inversion: if the error message didn't include the branch
        name, the assertion on the branch string would fail.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        with patch("spindle._has_skein", return_value=False):
            shard_info, shard_error = _spawn_shard(
                "test-agent", str(repo), base_branch="nonexistent-branch-abc"
            )

        assert shard_info is None
        assert shard_error is not None
        assert "nonexistent-branch-abc" in shard_error
        assert "--base-branch" in shard_error
