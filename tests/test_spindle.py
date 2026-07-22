"""Tests for Spindle MCP server."""

import asyncio
import contextlib
import inspect
import json
import multiprocessing
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import spindle

# Import the module to test
from spindle import (
    CLAUDE_MODEL_ALIASES,
    DEFAULT_REVIEW_TIMEOUT,
    GEMINI_MODEL_ALIASES,
    KIMI_DEFAULT_MODEL,
    KIMI_MODEL_ALIASES,
    MAX_CONCURRENT,
    PENDING_SPAWN_TIMEOUT,
    PERMISSION_PROFILES,
    READONLY_TOOLS,
    UNSPOOL_HEAD_CHARS,
    UNSPOOL_MAX_CHARS,
    UNSPOOL_TAIL_CHARS,
    _budget_result,
    _check_and_finalize_spool,
    _claude_permission_mode,
    _cleanup_shard,
    _codex_bwrap_wrap,
    _codex_respin_sandbox,
    _codex_respin_sync,
    _codex_sandbox_for_permission,
    _codex_spin_sync,
    _count_running,
    _detect_default_branch,
    _detect_existing_shard,
    _discover_profiles,
    _extract_codex_result,
    _extract_kimi_result,
    _format_spool_failure,
    _gemini_spin_sync,
    _gemini_unspool_sync,
    _get_cc_bg_tasks,
    _get_exit_path,
    _get_harnesses,
    _get_output_path,
    _get_spool_path,
    _get_stderr_path,
    _get_transcript_path,
    _handle_expired_session,
    _is_pid_alive,
    _is_review_tag,
    _kimi_respin_sync,
    _kimi_spin_sync,
    _kimi_unspool_sync,
    _list_spools,
    _load_profile,
    _monitor_spool,
    _op_inject,
    _parse_duration,
    _profile_spawn_env,
    _read_spool,
    _readonly_shard_conflict_error,
    _recover_orphans,
    _refusal_category,
    _resolve_permission,
    _resolve_profile_overrides,
    _resolve_profile_value,
    _resolve_spool_for_respin,
    _respin_sync,
    _spawn_shard,
    _spin_sync,
    _spool_lock,
    _try_reserve_slot_and_create,
    _unspool_sync,
    _write_spool,
    main,
    shard_abandon,
    shard_merge,
    spin,
    spool_info,
    spool_peek,
    spool_retry,
)


class TestPermissionProfiles:
    """Test permission profile resolution."""

    def test_default_permission_is_careful(self):
        """No permission specified defaults to careful, which now resolves to None
        (no allowlist — careful is an alias of auto)."""
        tools, shard = _resolve_permission(None, None)
        assert tools == PERMISSION_PROFILES["careful"]
        assert tools is None
        assert shard is False

    def test_explicit_readonly(self):
        """Readonly permission should return readonly tools."""
        tools, shard = _resolve_permission("readonly", None)
        assert tools == PERMISSION_PROFILES["readonly"]
        assert shard is False
        assert "Read" in tools
        assert "Write" not in tools

    def test_explicit_careful(self):
        """Careful now resolves to None: no allowlist (careful is an alias of auto)."""
        tools, shard = _resolve_permission("careful", None)
        assert tools == PERMISSION_PROFILES["careful"]
        assert tools is None
        assert shard is False

    def test_manual_aliases_readonly_resolution(self):
        """manual resolves to the readonly allowlist, no shard."""
        tools, shard = _resolve_permission("manual", None)
        assert tools == PERMISSION_PROFILES["readonly"]
        assert shard is False

    def test_codex_sandbox_manual_maps_read_only(self):
        """Finding A: on the codex path, manual maps to the read-only sandbox exactly
        like readonly, not the workspace-write default. The base tier decides, so the
        (chokepoint-rejected) readonly+shard / manual+shard spellings resolve here too."""
        for perm in ("readonly", "manual", "readonly+shard", "manual+shard"):
            assert _codex_sandbox_for_permission(perm, None) == "read-only", perm
        # Write-capable tiers are unaffected by the fix.
        assert _codex_sandbox_for_permission("careful", None) == "workspace-write"
        assert _codex_sandbox_for_permission("shard", None) == "workspace-write"
        assert _codex_sandbox_for_permission("full", None) == "danger-full-access"
        assert _codex_sandbox_for_permission(None, None) == "workspace-write"

    def test_readonly_shard_conflict_flags_all_forms(self):
        """The no-write readonly/manual tier + a shard is flagged on the resolved
        (tier, use_shard) pair, whatever spelling carried the shard intent; valid
        tiers pass. This is the authoritative check every launch chokepoint uses."""
        # readonly/manual tier + shard -> conflict (flag form and "+shard" string)
        for perm in ("readonly", "manual", "readonly+shard", "manual+shard"):
            msg = _readonly_shard_conflict_error(perm, True)
            assert msg is not None, perm
            assert "no write tools" in msg
            assert "careful+shard or shard" in msg
        # bare readonly/manual (no shard) is fine
        assert _readonly_shard_conflict_error("readonly", False) is None
        assert _readonly_shard_conflict_error("manual", False) is None
        # every write-capable tier + shard is fine
        for ok in ("careful+shard", "shard", "auto+shard", "research+shard", "careful", "full", None):
            assert _readonly_shard_conflict_error(ok, True) is None, ok

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

    def test_auto_plus_shard_no_allowedtools_flag(self, tmp_path):
        """permission='auto+shard' must not pass --allowedTools (classifier governs dynamically)."""
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
                                prompt="autonomous shard task",
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
        assert "--allowedTools" not in captured_cmd[0]


def _spin_claude_cmd(tmp_path, permission, *, shard_info=None):
    """Run _spin_sync (claude harness) with _spawn_detached stubbed, returning the
    captured claude argv. Pass shard_info to exercise the shard path without git;
    when bwrap is present the claude flags are still present in the wrapped argv."""
    captured = []

    def fake_detached(spool_id, cmd, cwd, env=None):
        captured.append(list(cmd))
        raise OSError("stop after capture")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("spindle.SPINDLE_DIR", tmp_path))
        stack.enter_context(patch("spindle._count_running", return_value=0))
        stack.enter_context(patch("spindle._spawn_detached", side_effect=fake_detached))
        if shard_info is not None:
            stack.enter_context(patch("spindle._detect_existing_shard", return_value=shard_info))
            stack.enter_context(patch("spindle._has_skein", return_value=True))
        _spin_sync(
            prompt="task",
            permission=permission,
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

    assert len(captured) == 1
    return captured[0]


def _mode_of(cmd):
    """The value passed to --permission-mode in a captured argv."""
    return cmd[cmd.index("--permission-mode") + 1]


def _allowed_tools_of(cmd):
    """The --allowedTools value in a captured argv, or None when the flag is absent."""
    if "--allowedTools" not in cmd:
        return None
    return cmd[cmd.index("--allowedTools") + 1]


class TestClaudePermissionMode:
    """The claude-harness permission-mode table (_claude_permission_mode)."""

    @pytest.mark.parametrize(
        "permission,expected",
        [
            (None, "auto"),
            ("careful", "auto"),
            ("auto", "auto"),
            ("auto+shard", "auto"),
            ("readonly", "acceptEdits"),
            ("manual", "acceptEdits"),
            ("research", "acceptEdits"),
            ("full", "bypassPermissions"),
            ("shard", "bypassPermissions"),
            ("careful+shard", "bypassPermissions"),
            ("research+shard", "bypassPermissions"),
            ("unknown-profile", "auto"),
        ],
    )
    def test_permission_mode_table(self, permission, expected):
        assert _claude_permission_mode(permission) == expected


class TestClaudePermissionCommandShape:
    """The claude command each tier emits: --permission-mode + --allowedTools.

    This is the contract from the careful-redesign brief: careful and the None
    default are classifier-vetted auto with no allowlist; readonly/manual are the
    tight allowlist tier; full/shard/careful+shard are bypassPermissions; and a
    resume re-applies the original tier rather than degrading to a bare resume.
    """

    def test_careful_emits_auto_no_allowlist(self, tmp_path):
        cmd = _spin_claude_cmd(tmp_path, "careful")
        assert _mode_of(cmd) == "auto"
        assert "--allowedTools" not in cmd

    def test_default_emits_auto_no_allowlist(self, tmp_path):
        cmd = _spin_claude_cmd(tmp_path, None)
        assert _mode_of(cmd) == "auto"
        assert "--allowedTools" not in cmd

    def test_readonly_emits_acceptedits_with_allowlist(self, tmp_path):
        cmd = _spin_claude_cmd(tmp_path, "readonly")
        assert _mode_of(cmd) == "acceptEdits"
        assert _allowed_tools_of(cmd) == PERMISSION_PROFILES["readonly"]

    def test_manual_emits_acceptedits_with_readonly_allowlist(self, tmp_path):
        cmd = _spin_claude_cmd(tmp_path, "manual")
        assert _mode_of(cmd) == "acceptEdits"
        assert _allowed_tools_of(cmd) == PERMISSION_PROFILES["readonly"]

    def test_full_emits_bypass(self, tmp_path):
        cmd = _spin_claude_cmd(tmp_path, "full")
        assert _mode_of(cmd) == "bypassPermissions"
        assert "--allowedTools" not in cmd

    def test_shard_emits_bypass(self, tmp_path):
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}
        cmd = _spin_claude_cmd(tmp_path, "shard", shard_info=shard_info)
        assert _mode_of(cmd) == "bypassPermissions"
        assert "--allowedTools" not in cmd

    def test_careful_shard_emits_bypass_no_allowlist(self, tmp_path):
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}
        cmd = _spin_claude_cmd(tmp_path, "careful+shard", shard_info=shard_info)
        assert _mode_of(cmd) == "bypassPermissions"
        assert "--allowedTools" not in cmd

    def test_respin_careful_reemits_auto_not_bare_resume(self, tmp_path):
        """A claude respin of a careful spool must re-apply --permission-mode auto,
        not leave a bare `claude --resume` (which would silently change capability)."""
        spool_id = "careful01"
        session_id = "claude-session-careful"
        captured_cmd = []

        def fake_detached(sid, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 4242

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "session_id": session_id,
                    "harness": "claude-code",
                    "permission": "careful",
                    "allowed_tools": None,
                },
            )
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        result = _respin_sync(spool_id, "keep going")

        assert not result.startswith("Error"), result
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"
        assert "--allowedTools" not in cmd

    def test_respin_readonly_reemits_acceptedits_and_allowlist(self, tmp_path):
        """A claude respin of a readonly spool must re-apply acceptEdits + the
        stored allowlist so the resumed spool stays as tight as the original."""
        spool_id = "readonly1"
        session_id = "claude-session-readonly"
        captured_cmd = []

        def fake_detached(sid, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 4242

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "session_id": session_id,
                    "harness": "claude-code",
                    "permission": "readonly",
                    "allowed_tools": PERMISSION_PROFILES["readonly"],
                },
            )
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._monitor_spool"):
                        result = _respin_sync(spool_id, "keep going")

        assert not result.startswith("Error"), result
        cmd = captured_cmd[0]
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert cmd[cmd.index("--allowedTools") + 1] == PERMISSION_PROFILES["readonly"]

    def test_expired_session_fallback_reemits_readonly_tier(self, tmp_path):
        """The transcript-injection fallback (a claude --resume whose session expired)
        must re-apply the original tier, not spawn a bare `claude -p`. A readonly
        original keeps acceptEdits + its allowlist on the expiry path."""
        _write_spool(
            "exp-orig-ro",
            {
                "id": "exp-orig-ro",
                "status": "complete",
                "session_id": "sess-exp-ro",
                "harness": "claude-code",
                "permission": "readonly",
                "allowed_tools": PERMISSION_PROFILES["readonly"],
                "working_dir": str(tmp_path),
                "created_at": datetime.now().isoformat(),
            },
        )
        transcript = _get_transcript_path("exp-orig-ro")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior transcript")
        failing = {
            "id": "exp-fail-ro",
            "status": "running",
            "session_id": "sess-exp-ro",
            "prompt": "Continue sess-exp-ro: keep going",
            "working_dir": str(tmp_path),
            "pid": None,
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            assert _handle_expired_session("exp-fail-ro", failing) is True

        cmd = captured["cmd"]
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert cmd[cmd.index("--allowedTools") + 1] == PERMISSION_PROFILES["readonly"]

    def test_expired_session_fallback_reemits_careful_auto_no_allowlist(self, tmp_path):
        """A careful original re-emits --permission-mode auto on the expiry fallback and,
        having no allowlist, adds no --allowedTools."""
        _write_spool(
            "exp-orig-careful",
            {
                "id": "exp-orig-careful",
                "status": "complete",
                "session_id": "sess-exp-careful",
                "harness": "claude-code",
                "permission": "careful",
                "allowed_tools": None,
                "working_dir": str(tmp_path),
                "created_at": datetime.now().isoformat(),
            },
        )
        transcript = _get_transcript_path("exp-orig-careful")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior transcript")
        failing = {
            "id": "exp-fail-careful",
            "status": "running",
            "session_id": "sess-exp-careful",
            "prompt": "Continue sess-exp-careful: keep going",
            "working_dir": str(tmp_path),
            "pid": None,
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            assert _handle_expired_session("exp-fail-careful", failing) is True

        cmd = captured["cmd"]
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"
        assert "--allowedTools" not in cmd

    def test_expired_session_fallback_refuses_stored_manual_shard(self, tmp_path):
        """Finding C: a stored readonly/manual + shard spool reaching the transcript
        fallback is refused before spawning — it must not launch with bypassPermissions
        via _claude_permission_mode. The failing spool is marked error, not retried.
        The transcript is present, so a missing guard would spawn (and trip the guard
        below) rather than silently bail."""
        _write_spool(
            "exp-orig-ms",
            {
                "id": "exp-orig-ms",
                "status": "complete",
                "session_id": "sess-exp-ms",
                "harness": "claude-code",
                "permission": "manual+shard",
                "allowed_tools": PERMISSION_PROFILES["readonly"],
                "shard": {"worktree_path": str(tmp_path), "shard_id": "sh"},
                "working_dir": str(tmp_path),
                "created_at": datetime.now().isoformat(),
            },
        )
        transcript = _get_transcript_path("exp-orig-ms")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior transcript")
        failing = {
            "id": "exp-fail-ms",
            "status": "running",
            "session_id": "sess-exp-ms",
            "prompt": "Continue sess-exp-ms: keep going",
            "working_dir": str(tmp_path),
            "pid": None,
        }

        with patch("spindle._spawn_detached", side_effect=AssertionError("must not launch")):
            assert _handle_expired_session("exp-fail-ms", failing) is True

        refused = _read_spool("exp-fail-ms")
        assert refused["status"] == "error"
        assert "no write tools" in refused["error"]

    @pytest.mark.parametrize(
        "permission,shard_flag",
        [
            ("readonly+shard", False),
            ("manual+shard", False),
            ("readonly", True),
            ("manual", True),
        ],
    )
    def test_readonly_manual_plus_shard_rejected_at_spin_entry(self, permission, shard_flag, tmp_path):
        """spin() rejects the readonly/manual + shard pairing with a clear error and
        launches no spool — whether the shard arrived as a "+shard" string or the
        shard=True flag. Harness-agnostic: it fires before any harness routes."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # If routing were reached, these would blow up — assert they are not.
            with patch("spindle._spin_sync", side_effect=AssertionError("must not launch")):
                with patch("spindle._codex_spin_sync", side_effect=AssertionError("must not launch")):
                    result = asyncio.run(
                        _spin(
                            "do something",
                            permission=permission,
                            shard=shard_flag,
                            working_dir=str(tmp_path),
                            skeinless=True,
                        )
                    )
        assert "no write tools" in result
        assert "careful+shard or shard" in result
        # No spool file was written (rejected before slot reservation).
        assert list(tmp_path.glob("*.json")) == []

    def test_spin_sync_chokepoint_rejects_flag_form_no_spool(self, tmp_path):
        """The _spin_sync chokepoint (spin() claude AND spool_retry) rejects
        readonly/manual + shard=True before reserving a slot."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=AssertionError("must not launch")):
                result = _spin_sync(
                    prompt="task",
                    permission="manual",
                    shard=True,
                    system_prompt=None,
                    working_dir=str(tmp_path),
                    allowed_tools=None,
                    tags=None,
                    model=None,
                    timeout=None,
                    skeinless=True,
                    env=None,
                )
        assert result.startswith("Error")
        assert "no write tools" in result
        assert list(tmp_path.glob("*.json")) == []

    def test_stored_manual_shard_cannot_be_respun(self, tmp_path):
        """A stored permission='manual+shard' spool is rejected on respin — it must
        not escalate to bypassPermissions via _claude_permission_mode."""
        stored = dict(
            id="stored01",
            status="complete",
            session_id="sess-x",
            harness="claude-code",
            permission="manual+shard",
            allowed_tools=PERMISSION_PROFILES["readonly"],
            shard=dict(worktree_path=str(tmp_path), shard_id="sh"),
        )
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("stored01", stored)
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._monitor_spool"):
                    with patch("spindle._spawn_detached", side_effect=AssertionError("must not launch")):
                        result = _respin_sync("stored01", "go")
        assert result.startswith("Error")
        assert "no write tools" in result
        assert set(p.name for p in tmp_path.glob("*.json")) == set(["stored01.json"])

    def test_stored_manual_shard_cannot_be_retried(self, tmp_path):
        """spool_retry of a stored manual+shard spool is rejected at the _spin_sync
        chokepoint and launches no new spool."""
        _retry = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
        stored = dict(
            id="stored02",
            status="error",
            harness="claude-code",
            permission="manual+shard",
            allowed_tools=PERMISSION_PROFILES["readonly"],
            working_dir=str(tmp_path),
            shard=dict(worktree_path=str(tmp_path), shard_id="sh"),
        )
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("stored02", stored)
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=AssertionError("must not launch")):
                    result = asyncio.run(_retry("stored02"))
        assert "no write tools" in result
        assert set(p.name for p in tmp_path.glob("*.json")) == set(["stored02.json"])

    def test_valid_shard_tiers_still_launch(self, tmp_path):
        """careful+shard, shard, auto+shard still resolve and build a command (no
        conflict); bare readonly/manual still emit acceptEdits + the readonly allowlist."""
        shard_info = dict(worktree_path=str(tmp_path), shard_id="shard-test")
        for perm in ("careful+shard", "shard", "auto+shard"):
            cmd = _spin_claude_cmd(tmp_path, perm, shard_info=shard_info)
            assert "--permission-mode" in cmd
        for perm in ("readonly", "manual"):
            cmd = _spin_claude_cmd(tmp_path, perm)
            assert _mode_of(cmd) == "acceptEdits"
            assert _allowed_tools_of(cmd) == PERMISSION_PROFILES["readonly"]


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


class TestResultBudgeting:
    """Test head/tail truncation and paging of large unspool results."""

    def test_small_result_unchanged(self):
        text = "x" * 100
        assert _budget_result(text, "id1") == text

    def test_result_at_threshold_unchanged(self):
        text = "x" * UNSPOOL_MAX_CHARS
        assert _budget_result(text, "id1") == text

    def test_large_result_truncated_with_head_and_tail(self):
        head = "H" * UNSPOOL_HEAD_CHARS
        tail = "T" * UNSPOOL_TAIL_CHARS
        text = head + ("M" * 40000) + tail
        out = _budget_result(text, "spool-xyz")
        assert len(out) < len(text)
        assert out.startswith("H" * 200)
        assert out.endswith("T" * 200)
        assert "M" * 200 not in out  # middle elided

    def test_breadcrumb_mentions_all_retrieval_paths(self):
        text = "z" * (UNSPOOL_MAX_CHARS + 50000)
        out = _budget_result(text, "spool-abc")
        assert 'unspool("spool-abc", full=True)' in out
        assert "offset=" in out
        assert "spool_export" in out
        assert 'spool_grep("<pattern>", spool_id="spool-abc")' in out

    def test_overlapping_windows_return_whole_text(self):
        # If HEAD+TAIL >= len(text), there is nothing to elide: return as-is
        # rather than emit a negative count or duplicate the middle.
        text = "a" * (UNSPOOL_MAX_CHARS + 100)
        with patch("spindle.UNSPOOL_HEAD_CHARS", len(text)), patch("spindle.UNSPOOL_TAIL_CHARS", len(text)):
            assert _budget_result(text, "id1") == text

    def test_zero_tail_does_not_duplicate_text(self):
        # text[-0:] is the whole string; with TAIL=0 the tail must be empty so
        # output stays head + crumb, never head + crumb + full text.
        text = "b" * (UNSPOOL_MAX_CHARS + 50000)
        with patch("spindle.UNSPOOL_HEAD_CHARS", 100), patch("spindle.UNSPOOL_TAIL_CHARS", 0):
            out = _budget_result(text, "id1")
            assert len(out) < len(text)
            assert out.startswith("b" * 100)

    def test_unspool_full_bypasses_budget(self, tmp_path):
        big = "q" * (UNSPOOL_MAX_CHARS + 30000)
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("sp1", {"id": "sp1", "status": "complete", "result": big})
            full = asyncio.run(spindle.unspool.fn("sp1", full=True))
            assert full == big
            budgeted = asyncio.run(spindle.unspool.fn("sp1"))
            assert len(budgeted) < len(big)
            assert "full=True" in budgeted

    def test_unspool_paging_returns_slice_with_markers(self, tmp_path):
        big = "abcdefghij" * 10000  # 100k chars
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("sp2", {"id": "sp2", "status": "complete", "result": big})
            page = asyncio.run(spindle.unspool.fn("sp2", offset=12000, limit=5000))
            lines = page.split("\n")
            assert lines[0].startswith("[chars 12,000-17,000 of 100,000]")
            assert "[more:" in page
            assert big[12000:17000] in page

    def test_unspool_paging_last_slice_has_no_more_crumb(self, tmp_path):
        big = "y" * 60000
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("sp3", {"id": "sp3", "status": "complete", "result": big})
            page = asyncio.run(spindle.unspool.fn("sp3", offset=55000))
            assert "[more:" not in page

    def test_output_never_longer_than_input(self):
        # elided positive but smaller than the breadcrumb: truncating would grow
        # the output, so _budget_result must return the text unchanged.
        text = "c" * 50001
        with (
            patch("spindle.UNSPOOL_MAX_CHARS", 50000),
            patch("spindle.UNSPOOL_HEAD_CHARS", 25000),
            patch("spindle.UNSPOOL_TAIL_CHARS", 24999),
        ):
            assert _budget_result(text, "id1") == text

    def test_head_zero_uses_tail_only(self):
        text = "d" * (UNSPOOL_MAX_CHARS + 50000)
        with patch("spindle.UNSPOOL_HEAD_CHARS", 0), patch("spindle.UNSPOOL_TAIL_CHARS", 100):
            out = _budget_result(text, "id1")
            assert len(out) < len(text)
            assert out.endswith("d" * 100)

    def test_unspool_paging_invalid_limit(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("p1", {"id": "p1", "status": "complete", "result": "z" * 200})
            for bad in (-5, 0):
                out = asyncio.run(spindle.unspool.fn("p1", limit=bad))
                assert "invalid limit" in out
                assert "[chars" not in out
                assert "[more:" not in out

    def test_unspool_paging_offset_past_end(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("p2", {"id": "p2", "status": "complete", "result": "z" * 100})
            out = asyncio.run(spindle.unspool.fn("p2", offset=500))
            assert out.splitlines()[0] == "[chars 100-100 of 100]"
            assert "[more:" not in out

    def test_paging_non_complete_returns_sentinel(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("p3", {"id": "p3", "status": "pending", "result": None})
            out = asyncio.run(spindle.unspool.fn("p3", offset=0, limit=50))
            assert "pending" in out
            assert "[chars" not in out

    def test_unspool_coerces_non_string_result(self, tmp_path):
        # A structured result must not crash paging or budgeting.
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("p4", {"id": "p4", "status": "complete", "result": {"k": "v"}})
            out = asyncio.run(spindle.unspool.fn("p4", offset=0, limit=100))
            assert '"k": "v"' in out
            assert out.splitlines()[0].startswith("[chars 0-")
            # budgeting path on a structured result also must not raise
            assert asyncio.run(spindle.unspool.fn("p4")) == json.dumps({"k": "v"}, indent=2)


class TestSingleSpoolGrep:
    """Test single-spool line-level grep with context."""

    def test_unknown_spool(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            out = asyncio.run(spindle.spool_grep.fn("x", spool_id="nope"))
            assert "Unknown spool_id" in out

    def test_matching_lines_with_context(self, tmp_path):
        result = "alpha\nbeta\nERROR here\ndelta\nepsilon\n"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g1", {"id": "g1", "status": "complete", "result": result})
            out = asyncio.run(spindle.spool_grep.fn("ERROR", spool_id="g1", context=1))
            assert "matching line(s)" in out.splitlines()[0]
            assert "beta" in out  # context before
            assert "ERROR here" in out
            assert "delta" in out  # context after
            assert "epsilon" not in out  # outside context window

    def test_no_match(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g2", {"id": "g2", "status": "complete", "result": "nothing\n"})
            out = asyncio.run(spindle.spool_grep.fn("zzz", spool_id="g2"))
            assert "No lines" in out

    def test_dict_result_searched_as_json(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g3", {"id": "g3", "status": "complete", "result": {"finding": "ERROR in auth"}})
            out = asyncio.run(spindle.spool_grep.fn("ERROR", spool_id="g3"))
            assert "ERROR in auth" in out

    def test_list_result_searched_as_json(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g3b", {"id": "g3b", "status": "complete", "result": ["alpha", "ERROR beta"]})
            out = asyncio.run(spindle.spool_grep.fn("ERROR", spool_id="g3b"))
            assert "ERROR beta" in out

    def test_context_zero_shows_only_hits(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g4", {"id": "g4", "status": "complete", "result": "a\nMATCH\nb\n"})
            out = asyncio.run(spindle.spool_grep.fn("MATCH", spool_id="g4", context=0))
            assert "2: MATCH" in out
            assert "1  a" not in out  # no context line shown

    def test_negative_context_treated_as_zero(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g4b", {"id": "g4b", "status": "complete", "result": "a\nMATCH\nb\n"})
            out = asyncio.run(spindle.spool_grep.fn("MATCH", spool_id="g4b", context=-1))
            assert "2: MATCH" in out  # hit line still shown, not dropped

    def test_window_merge_separator(self, tmp_path):
        lines = ["L0 hit", "L1", "L2", "L3", "L4", "L5", "L6 hit", "L7"]
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool("g5", {"id": "g5", "status": "complete", "result": "\n".join(lines)})
            out = asyncio.run(spindle.spool_grep.fn("hit", spool_id="g5", context=1))
            assert "--" in out  # gap between the two windows
            # adjacent matches with overlapping windows should NOT show a separator
            close = ["A hit", "B", "C hit", "D"]
            _write_spool("g6", {"id": "g6", "status": "complete", "result": "\n".join(close)})
            out2 = asyncio.run(spindle.spool_grep.fn("hit", spool_id="g6", context=1))
            assert "--" not in out2  # windows merge into one contiguous block


class TestCodexResultExtraction:
    """Codex results should store the agent's prose, not the raw event stream."""

    def _git_shard(self, tmp_path):
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / ".gitignore").write_text("ignored/\n*.log\n")
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Spindle Test",
                "-c",
                "user.email=spindle@example.test",
                "commit",
                "-q",
                "-m",
                "base",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "shard-test", str(worktree)], check=True)
        return repo, worktree

    def _stream(self):
        # A realistic codex stream: messages plus a huge command-output item.
        big_output = "LOG LINE\n" * 5000
        events = [
            {"type": "thread.started", "thread_id": "thread-abc"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Planning the review."}},
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "command_execution", "command": "ls", "aggregated_output": big_output},
            },
            {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Final verdict: clean."}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
        ]
        return "\n".join(json.dumps(e) for e in events), big_output

    def test_extract_joins_agent_messages(self):
        stream, _ = self._stream()
        out = _extract_codex_result(stream)
        assert out == "Planning the review.\n\nFinal verdict: clean."

    def test_extract_excludes_command_output(self):
        stream, big_output = self._stream()
        out = _extract_codex_result(stream)
        assert "LOG LINE" not in out
        assert len(out) < len(big_output)

    def test_extract_returns_none_without_messages(self):
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x"}},
            {"type": "turn.completed", "usage": {}},
        ]
        stream = "\n".join(json.dumps(e) for e in events)
        assert _extract_codex_result(stream) is None

    def test_extract_empty_and_whitespace_stream(self):
        assert _extract_codex_result("") is None
        assert _extract_codex_result("   \n  \n") is None

    def test_extract_tolerates_malformed_and_non_dict_lines(self):
        # Interleave a non-JSON line, a bare scalar, and a JSON array among
        # valid events. None should raise; the agent messages still come out.
        lines = [
            "not json at all",
            "42",
            "[1, 2, 3]",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello"}}),
            "true",
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "World"}}),
        ]
        out = _extract_codex_result("\n".join(lines))
        assert out == "Hello\n\nWorld"

    def test_extract_multiple_turns(self):
        events = [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Turn one."}},
            {"type": "turn.completed", "usage": {}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Turn two."}},
            {"type": "turn.completed", "usage": {}},
        ]
        stream = "\n".join(json.dumps(e) for e in events)
        assert _extract_codex_result(stream) == "Turn one.\n\nTurn two."

    def test_failure_message_unwraps_provider_error(self):
        provider_error = json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {
                    "type": "invalid_request_error",
                    "message": "The 'gpt-5.6' model is not supported when using Codex with a ChatGPT account.",
                },
            }
        )
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-failed"}),
                json.dumps({"type": "turn.failed", "error": {"message": provider_error}}),
            ]
        )

        assert spindle._codex_failure_message(stream) == (
            "The 'gpt-5.6' model is not supported when using Codex with a ChatGPT account."
        )

    @pytest.mark.parametrize(
        "failed_event",
        [
            {"type": "turn.failed"},
            {"type": "turn.failed", "error": {}},
            {"type": "turn.failed", "error": {"message": ""}},
        ],
    )
    def test_failure_message_is_generic_when_provider_omits_message(self, failed_event):
        assert spindle._codex_failure_message(json.dumps(failed_event)) == "Codex failed without an error message"

    def test_top_level_codex_error_is_terminal(self):
        stream = json.dumps({"type": "error", "message": "unrecoverable stream failure"})
        assert spindle._codex_failure_message(stream) == "unrecoverable stream failure"

    def test_failure_message_uses_last_terminal_event(self):
        recovered = "\n".join(
            [
                json.dumps({"type": "turn.failed", "error": {"message": "stream interrupted"}}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        )
        later_failure = "\n".join(
            [
                json.dumps({"type": "turn.completed", "usage": {}}),
                json.dumps({"type": "turn.failed", "error": {"code": "rate_limit"}}),
            ]
        )

        assert spindle._codex_failure_message(recovered) is None
        assert spindle._codex_failure_message(later_failure) == '{"code": "rate_limit"}'

    def test_finalize_reports_recovered_later_turn_as_complete(self, tmp_path):
        stream = "\n".join(
            [
                json.dumps({"type": "turn.failed", "error": {"message": "stream interrupted"}}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Recovered."}}),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 3}}),
            ]
        )
        spool_id = "codex-recovered-turn"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spool["status"] == "complete"
        assert spool["result"] == "Recovered."
        assert spool["cost"]["output_tokens"] == 3

    def test_finalize_marks_turn_failed_error_and_cleans_unchanged_new_shard(self, tmp_path):
        provider_error = json.dumps(
            {"status": 400, "error": {"message": "The requested model is not supported for this account."}}
        )
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-failed"}),
                json.dumps({"type": "turn.failed", "error": {"message": provider_error}}),
            ]
        )
        spool_id = "codex-failed-model"
        shard = {
            "worktree_path": str(tmp_path / "worktree"),
            "branch_name": "shard-codex-failed-model",
        }
        with patch("spindle.SPINDLE_DIR", tmp_path):
            (tmp_path / "worktree").mkdir()
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(tmp_path / "repo"),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            with patch("spindle._shard_cleanup_state", return_value="pristine"):
                with patch("spindle._shard_cleanup_expected_head", return_value="base-oid"):
                    with patch("spindle._cleanup_shard", return_value=True) as cleanup:
                        assert _check_and_finalize_spool(spool_id) is True

            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert spool["error"] == "The requested model is not supported for this account."
            assert spool["result"] == stream
            assert spool["session_id"] == "thread-failed"
            assert spool["shard"]["startup_failure_cleaned"] is True
            assert spool["working_dir"] == str(tmp_path / "repo")
            cleanup.assert_called_once()
            cleanup_args, cleanup_kwargs = cleanup.call_args
            assert cleanup_args[0]["worktree_path"] == shard["worktree_path"]
            assert cleanup_args[1] == str(tmp_path / "repo")
            assert cleanup_kwargs == {
                "spool_id": spool_id,
                "force": False,
                "expected_head": "base-oid",
            }
            assert _get_transcript_path(spool_id).read_text() == stream

    def test_finalize_waits_for_process_exit_after_failure_event(self, tmp_path):
        spool_id = "codex-terminal-failure"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": os.getpid(),
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is False
            assert _read_spool(spool_id)["status"] == "running"

    def test_finalize_waits_for_process_exit_after_completed_event(self, tmp_path):
        spool_id = "codex-terminal-success-live"
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        live_proc = MagicMock()
        live_proc.poll.return_value = None
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[spool_id] = live_proc
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 12345,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is False
            assert _read_spool(spool_id)["status"] == "running"
            assert spindle._PROC_HANDLES.pop(spool_id) is live_proc

    @pytest.mark.parametrize(
        "stream",
        [
            json.dumps({"type": "thread.started", "thread_id": "partial"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ],
    )
    def test_nonzero_codex_exit_with_stdout_is_error(self, tmp_path, stream):
        spool_id = "codex-nonzero-partial"
        failed_proc = MagicMock()
        failed_proc.poll.return_value = 7
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[spool_id] = failed_proc
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 12345,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_stderr_path(spool_id).write_text("codex crashed")
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spool["status"] == "error"
        assert spool["error"] == "codex crashed"
        assert spool["exit_code"] == 7
        assert spool["result"] == stream

    def test_zero_codex_exit_without_completed_turn_is_error(self, tmp_path):
        spool_id = "codex-zero-partial"
        partial_proc = MagicMock()
        partial_proc.poll.return_value = 0
        stream = json.dumps({"type": "thread.started", "thread_id": "partial"})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[spool_id] = partial_proc
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 12345,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spool["status"] == "error"
        assert spool["error"] == "Codex exited without a completed turn"
        assert spool["exit_code"] == 0

    def test_new_turn_after_completion_requires_its_own_terminal_event(self, tmp_path):
        spool_id = "codex-complete-then-started"
        finished_proc = MagicMock()
        finished_proc.poll.return_value = 0
        stream = "\n".join(
            [
                json.dumps({"type": "turn.completed", "usage": {}}),
                json.dumps({"type": "turn.started"}),
            ]
        )
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[spool_id] = finished_proc
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 12345,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spindle._codex_failure_message(stream) is None
        assert spool["status"] == "error"
        assert spool["error"] == "Codex exited without a completed turn"

    def test_orphan_recovery_reads_persisted_nonzero_exit_status(self, tmp_path):
        spool_id = "codex-orphan-nonzero"
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": None,
                    "created_at": datetime.now().isoformat(),
                },
            )
            pid = spindle._spawn_detached(
                spool_id,
                ["/bin/sh", "-c", 'printf "%s\\n" "$1"; exit 7', "child", stream],
                str(tmp_path),
                {"PATH": str(tmp_path / "empty-path")},
            )
            proc = spindle._PROC_HANDLES.pop(spool_id)
            assert proc.wait(timeout=5) == 7
            spool = _read_spool(spool_id)
            spool["pid"] = pid
            _write_spool(spool_id, spool)

            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spool["status"] == "error"
        assert spool["exit_code"] == 7
        assert "code 7" in spool["error"]
        assert not list(tmp_path.glob(f"{spool_id}.exit.tmp.*"))

    def test_orphan_recovery_fails_closed_without_exit_status(self, tmp_path):
        spool_id = "codex-orphan-no-status"
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)

        assert spool["status"] == "error"
        assert spool["error"] == "Codex exit status unavailable"

    def test_process_group_liveness_sees_descendant_after_leader_exit(self):
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 30 </dev/null >/dev/null 2>&1 & exit 0"],
            start_new_session=True,
        )
        proc.wait(timeout=5)
        try:
            assert spindle._is_process_group_alive(proc.pid) is True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_finalize_waits_and_preserves_failed_shard_while_process_group_lives(self, tmp_path):
        spool_id = "codex-live-group"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "shard": {"worktree_path": str(tmp_path / "worktree")},
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(tmp_path / "repo"),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            with patch("spindle._is_process_group_alive", return_value=True):
                with patch("spindle._cleanup_shard") as cleanup:
                    assert _check_and_finalize_spool(spool_id) is False
            cleanup.assert_not_called()
            assert _read_spool(spool_id)["status"] == "running"
            assert _get_output_path(spool_id).exists()

    @pytest.mark.parametrize(
        "other_spool",
        [
            {"id": "other-pending", "status": "pending"},
            {"id": "other-running", "status": "running", "working_dir": "WORKTREE/subdir"},
        ],
    )
    def test_finalize_preserves_failed_shard_while_another_spool_may_use_it(self, tmp_path, other_spool):
        spool_id = "codex-owner-failed"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        other_spool = dict(other_spool)
        if other_spool.get("working_dir"):
            other_spool["working_dir"] = other_spool["working_dir"].replace("WORKTREE", str(worktree))
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(other_spool["id"], other_spool)
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "shard": {"worktree_path": str(worktree)},
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(tmp_path / "repo"),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            with patch("spindle._shard_cleanup_state", return_value="pristine"):
                with patch("spindle._cleanup_shard") as cleanup:
                    assert _check_and_finalize_spool(spool_id) is True
            cleanup.assert_not_called()
            assert _read_spool(spool_id)["status"] == "error"

    def test_finalize_preserves_failed_shard_for_terminal_reuser_with_live_group(self, tmp_path):
        spool_id = "codex-owner-terminal-reuser"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "other-complete",
                {
                    "id": "other-complete",
                    "status": "complete",
                    "working_dir": str(worktree / "subdir"),
                    "pid": 22222,
                },
            )
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 11111,
                    "created_at": datetime.now().isoformat(),
                    "shard": {"worktree_path": str(worktree)},
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(tmp_path / "repo"),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            with patch("spindle._is_pid_alive", return_value=False):
                with patch("spindle._is_process_group_alive", side_effect=lambda pid: pid == 22222):
                    with patch("spindle._shard_cleanup_state", return_value="pristine"):
                        with patch("spindle._cleanup_shard") as cleanup:
                            assert _check_and_finalize_spool(spool_id) is True
            cleanup.assert_not_called()
            assert _read_spool(spool_id)["status"] == "error"

    def test_deferred_cleanup_retries_after_transient_reuser_clears(self, tmp_path):
        spool_id = "codex-deferred-cleanup"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        spool = {
            "id": spool_id,
            "status": "error",
            "pid": None,
            "working_dir": str(worktree),
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-deferred"},
            "shard_created_by_spool": True,
            "shard_source_dir": str(tmp_path / "repo"),
        }
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._shard_has_other_active_spool", return_value=True):
                assert spindle._cleanup_failed_spool_shard(spool) is False
            assert spool["shard_cleanup_pending"] is True
            _write_spool(spool_id, spool)

            with patch("spindle.time.sleep"):
                with patch("spindle._shard_has_other_active_spool", return_value=False):
                    with patch("spindle._shard_cleanup_state", return_value="pristine"):
                        with patch("spindle._shard_cleanup_expected_head", return_value="base-oid"):
                            with patch("spindle._cleanup_shard", return_value=True) as cleanup:
                                spindle._monitor_deferred_shard_cleanup(spool_id)

            repaired = _read_spool(spool_id)
        cleanup.assert_called_once()
        assert repaired["working_dir"] == str(tmp_path / "repo")
        assert repaired["shard"]["startup_failure_cleaned"] is True
        assert "shard_cleanup_pending" not in repaired

    def test_changed_failed_shard_is_preserved_without_deferred_deletion(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        spool = {
            "id": "codex-changed-preserved",
            "status": "error",
            "pid": None,
            "shard": {"worktree_path": str(worktree)},
            "shard_created_by_spool": True,
            "shard_source_dir": str(tmp_path / "repo"),
        }
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._shard_cleanup_state", return_value="changed"):
                assert spindle._cleanup_failed_spool_shard(spool) is False
        assert spool["shard_cleanup_preserved"] is True
        assert "shard_cleanup_pending" not in spool

    def test_cleanup_reports_success_if_branch_step_times_out_after_worktree_removal(self, tmp_path):
        shard = {"worktree_path": str(tmp_path / "worktree"), "branch_name": "shard-partial"}
        removed = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "spindle.subprocess.run",
            side_effect=[removed, subprocess.TimeoutExpired("git branch", 10)],
        ):
            assert _cleanup_shard(shard, str(tmp_path)) is True

    def test_automatic_cleanup_nonforce_preserves_output_created_after_pristine_check(self, tmp_path):
        repo, worktree = self._git_shard(tmp_path)
        late_output = worktree / "late.txt"
        spool = {
            "id": "codex-late-output",
            "status": "error",
            "pid": None,
            "working_dir": str(worktree),
            "base_branch": "main",
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
            "shard_created_by_spool": True,
            "shard_source_dir": str(repo),
        }

        def pristine_then_write(_spool):
            late_output.write_text("valuable late output\n")
            return "pristine"

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._shard_cleanup_state", side_effect=pristine_then_write):
                assert spindle._cleanup_failed_spool_shard(spool) is False

        assert worktree.exists()
        assert late_output.read_text() == "valuable late output\n"
        assert spool["shard_cleanup_pending"] is True

    def test_automatic_cleanup_preserves_ignored_output_created_after_pristine_check(self, tmp_path):
        repo, worktree = self._git_shard(tmp_path)
        ignored_output = worktree / "ignored" / "report.md"
        spool = {
            "id": "codex-late-ignored-output",
            "status": "error",
            "pid": None,
            "working_dir": str(worktree),
            "base_branch": "main",
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
            "shard_created_by_spool": True,
            "shard_source_dir": str(repo),
        }

        def pristine_then_write(_spool):
            ignored_output.parent.mkdir()
            ignored_output.write_text("valuable ignored output\n")
            return "pristine"

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._shard_cleanup_state", side_effect=pristine_then_write):
                assert spindle._cleanup_failed_spool_shard(spool) is False

        assert worktree.exists()
        assert ignored_output.read_text() == "valuable ignored output\n"
        assert spool["shard_cleanup_pending"] is True

    def test_automatic_cleanup_preserves_commit_created_after_pristine_check(self, tmp_path):
        repo, worktree = self._git_shard(tmp_path)
        spool = {
            "id": "codex-late-commit",
            "status": "error",
            "pid": None,
            "working_dir": str(worktree),
            "base_branch": "main",
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
            "shard_created_by_spool": True,
            "shard_source_dir": str(repo),
        }

        def pristine_then_commit(_spool):
            (worktree / "late-commit.txt").write_text("valuable committed work\n")
            subprocess.run(["git", "-C", str(worktree), "add", "late-commit.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "-c",
                    "user.name=Spindle Test",
                    "-c",
                    "user.email=spindle@example.test",
                    "commit",
                    "-q",
                    "-m",
                    "late work",
                ],
                check=True,
            )
            return "pristine"

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._shard_cleanup_state", side_effect=pristine_then_commit):
                assert spindle._cleanup_failed_spool_shard(spool) is True

        assert not worktree.exists()
        late_commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "shard-test"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        saved = subprocess.run(
            ["git", "-C", str(repo), "show", f"{late_commit}:late-commit.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert saved == "valuable committed work\n"

    def test_cleanup_intent_is_persisted_before_removal_and_repairs_after_restart(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        spool_id = "codex-cleanup-crash"
        spool = {
            "id": spool_id,
            "status": "error",
            "pid": None,
            "working_dir": str(worktree),
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-crash"},
            "shard_created_by_spool": True,
            "shard_source_dir": str(tmp_path / "repo"),
        }

        def remove_then_crash(*args, **kwargs):
            worktree.rmdir()
            raise RuntimeError("simulated server crash")

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(spool_id, spool)
            with patch("spindle._shard_cleanup_state", return_value="pristine"):
                with patch("spindle._shard_cleanup_expected_head", return_value="base-oid"):
                    with patch("spindle._cleanup_shard", side_effect=remove_then_crash):
                        with pytest.raises(RuntimeError, match="simulated server crash"):
                            spindle._cleanup_failed_spool_shard(spool)

            persisted = _read_spool(spool_id)
            assert persisted["shard_cleanup_pending"] is True
            assert persisted["working_dir"] == str(worktree)
            assert spindle._cleanup_failed_spool_shard(persisted) is True
            _write_spool(spool_id, persisted)
            repaired = _read_spool(spool_id)

        assert repaired["working_dir"] == str(tmp_path / "repo")
        assert repaired["shard"]["startup_failure_cleaned"] is True
        assert "shard_cleanup_pending" not in repaired

    def test_stale_pending_reservation_expires_then_stops_blocking(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        stale_time = (datetime.now() - timedelta(seconds=PENDING_SPAWN_TIMEOUT + 1)).isoformat()
        other = {"id": "stale-pending", "status": "pending", "created_at": stale_time}
        shard = {"worktree_path": str(worktree)}
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(other["id"], other)
            assert spindle._shard_has_other_active_spool("owner", shard) is True
            assert _read_spool(other["id"])["status"] == "error"
            assert spindle._shard_has_other_active_spool("owner", shard) is False

    def test_dead_post_restart_running_record_does_not_block_cleanup(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        other = {
            "id": "dead-running",
            "status": "running",
            "working_dir": str(worktree),
            "pid": 999999999,
        }
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(other["id"], other)
            assert spindle._shard_has_other_active_spool("owner", {"worktree_path": str(worktree)}) is False

    @pytest.mark.parametrize("created_by_spool,pristine", [(False, True), (True, False)])
    def test_finalize_preserves_reused_or_changed_failed_shard(self, tmp_path, created_by_spool, pristine):
        spool_id = f"codex-preserve-{created_by_spool}-{pristine}"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "shard": {"worktree_path": str(tmp_path / "worktree")},
                    "shard_created_by_spool": created_by_spool,
                    "shard_source_dir": str(tmp_path / "repo"),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            state = "pristine" if pristine else "changed"
            with patch("spindle._shard_cleanup_state", return_value=state):
                with patch("spindle._cleanup_shard") as cleanup:
                    assert _check_and_finalize_spool(spool_id) is True
            cleanup.assert_not_called()
            assert _read_spool(spool_id)["status"] == "error"

    @pytest.mark.parametrize("failure_point", ["status", "rev-list"])
    def test_shard_commit_status_fails_closed_on_git_error(self, tmp_path, failure_point):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        spool = {
            "base_branch": "main",
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
        }
        ok = MagicMock(returncode=0, stdout="")
        failed = MagicMock(returncode=128, stdout="", stderr="bad revision")
        responses = [failed] if failure_point == "status" else [ok, failed]
        with patch("spindle.subprocess.run", side_effect=responses):
            assert spindle._get_shard_commit_status(spool) == "unknown"

    @pytest.mark.parametrize("failure_point", ["status", "rev-list"])
    def test_cleanup_pristine_check_fails_closed_on_git_error(self, tmp_path, failure_point):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        spool = {
            "base_branch": "main",
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
        }
        ok = MagicMock(returncode=0, stdout="")
        failed = MagicMock(returncode=128, stdout="", stderr="bad revision")
        responses = [failed] if failure_point == "status" else [ok, failed]
        with patch("spindle.subprocess.run", side_effect=responses):
            assert spindle._shard_is_pristine_for_cleanup(spool) is False

    def test_finalize_preserves_failed_shard_with_ignored_output(self, tmp_path):
        repo, worktree = self._git_shard(tmp_path)
        (worktree / "ignored").mkdir()
        (worktree / "ignored" / "report.md").write_text("valuable report\n")
        (worktree / "run.log").write_text("valuable log\n")
        spool_id = "codex-ignored-output"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "working_dir": str(worktree),
                    "base_branch": "main",
                    "shard": {"worktree_path": str(worktree), "branch_name": "shard-test"},
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(repo),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")
            assert _check_and_finalize_spool(spool_id) is True

        assert worktree.exists()
        assert (worktree / "ignored" / "report.md").read_text() == "valuable report\n"
        assert (worktree / "run.log").read_text() == "valuable log\n"

    def test_finalize_really_removes_pristine_failed_shard_and_retry_uses_source_repo(self, tmp_path):
        repo, worktree = self._git_shard(tmp_path)
        spool_id = "codex-pristine-failure"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        shard = {"worktree_path": str(worktree), "branch_name": "shard-test"}
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                    "working_dir": str(worktree),
                    "model": "gpt-5.6-sol",
                    "sandbox": "workspace-write",
                    "permission": "careful+shard",
                    "base_branch": "main",
                    "shard": shard,
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(repo),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")
            assert _check_and_finalize_spool(spool_id) is True

            spool = _read_spool(spool_id)
            assert spool["working_dir"] == str(repo)
            assert spool["shard"]["startup_failure_cleaned"] is True
            assert not worktree.exists()
            assert (
                subprocess.run(
                    ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/shard-test"]
                ).returncode
                != 0
            )

            retry_tool = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
            with patch("spindle._codex_spin_sync", return_value="retry-spool") as retry:
                assert asyncio.run(retry_tool(spool_id)) == "retry-spool"
            assert retry.call_args.args[1] == str(repo)
            assert retry.call_args.kwargs["shard"] is True

    def test_finalize_falls_back_to_raw_stream_when_no_messages(self, tmp_path):
        # No agent_message items, but valid session/cost events and a non-dict
        # line that must not break session_id extraction.
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread-xyz"}),
            "99",  # non-dict line - must be skipped, not abort the loop
            json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "OUT"}}),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 7}}),
        ]
        stream = "\n".join(lines)
        spool_id = "codex-fin2"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "complete"
            # No agent messages -> result falls back to the raw stream
            assert spool["result"] == stream
            # session_id and cost still extracted despite the non-dict line
            assert spool["session_id"] == "thread-xyz"
            assert spool["cost"]["output_tokens"] == 7

    def test_finalize_stores_prose_keeps_stream_in_transcript(self, tmp_path):
        stream, big_output = self._stream()
        spool_id = "codex-fin1"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "review",
                    "pid": 999999999,  # dead → finalize proceeds
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            _get_exit_path(spool_id).write_text("0\n")

            assert _check_and_finalize_spool(spool_id) is True

            spool = _read_spool(spool_id)
            assert spool["status"] == "complete"
            assert spool["result"] == "Planning the review.\n\nFinal verdict: clean."
            assert "LOG LINE" not in spool["result"]
            assert spool["session_id"] == "thread-abc"
            assert spool["cost"]["output_tokens"] == 20

            # Full event stream (including command output) preserved in transcript
            transcript = _get_transcript_path(spool_id).read_text()
            assert "LOG LINE" in transcript
            assert "command_execution" in transcript


class TestKimiResultExtraction:
    """Kimi results should store the assistant's prose, not the raw JSONL stream.

    The stream embeds role:"tool" lines with full file/command output, and the
    final assistant content is a list (thinking mode) or a plain string
    (non-thinking, the default). Both must extract cleanly.
    """

    def test_string_content_non_thinking(self):
        # The default/non-thinking case the old extractor missed.
        events = [
            {"role": "tool", "content": "     1\t# README\n" + ("X" * 19000)},
            {"role": "assistant", "content": "Spindle is an MCP server."},
        ]
        stream = "\n".join(json.dumps(e) for e in events)
        out = _extract_kimi_result(stream)
        assert out == "Spindle is an MCP server."
        assert "X" * 100 not in out  # tool output excluded

    def test_list_content_thinking_skips_think_block(self):
        events = [
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "text": "internal reasoning"},
                    {"type": "text", "text": "Final answer."},
                ],
            },
        ]
        out = _extract_kimi_result("\n".join(json.dumps(e) for e in events))
        assert out == "Final answer."
        assert "internal reasoning" not in out

    def test_tool_lines_never_picked_up(self):
        events = [{"role": "tool", "content": "huge file dump"}]
        assert _extract_kimi_result("\n".join(json.dumps(e) for e in events)) is None

    def test_last_assistant_message_wins(self):
        events = [
            {"role": "assistant", "content": "first"},
            {"role": "tool", "content": "x"},
            {"role": "assistant", "content": "second"},
        ]
        assert _extract_kimi_result("\n".join(json.dumps(e) for e in events)) == "second"

    def test_tolerates_malformed_and_non_dict_lines(self):
        lines = [
            "not json",
            "42",
            json.dumps({"role": "assistant", "content": "answer"}),
        ]
        assert _extract_kimi_result("\n".join(lines)) == "answer"

    def test_empty_returns_none(self):
        assert _extract_kimi_result("") is None

    def test_finalize_extracts_prose_keeps_stream_in_transcript(self, tmp_path):
        big_tool = "FILEDUMP\n" * 3000
        events = [
            {"role": "tool", "content": big_tool},
            {"role": "assistant", "content": "The answer is 42."},
        ]
        stream = "\n".join(json.dumps(e) for e in events)
        spool_id = "kimi-fin1"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "kimi",
                    "prompt": "q",
                    "session_id": "kimi-sess",  # kimi sets this at creation
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "complete"
            assert spool["result"] == "The answer is 42."
            assert "FILEDUMP" not in spool["result"]
            # Full stream (with tool output) preserved in transcript
            transcript = _get_transcript_path(spool_id).read_text()
            assert "FILEDUMP" in transcript

    def test_finalize_falls_back_to_raw_when_no_assistant_text(self, tmp_path):
        stream = "\n".join(
            json.dumps(e)
            for e in [
                {"role": "tool", "content": "only tool output"},
            ]
        )
        spool_id = "kimi-fin2"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "kimi",
                    "prompt": "q",
                    "session_id": "s2",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "complete"
            assert spool["result"] == stream  # last-resort fallback


class TestStoreIsolation:
    """The test store must never resolve to the real ~/.spindle (conftest)."""

    def test_autouse_points_at_per_test_tmp(self, tmp_path):
        assert spindle.SPINDLE_DIR == tmp_path / "spindle-spools"

    def test_spindle_home_env_is_set(self):
        assert os.environ.get("SPINDLE_HOME")

    def test_spindle_home_honored_at_import(self, tmp_path):
        # Genuinely exercise the import-time computation in a fresh process:
        # SPINDLE_HOME must steer SPINDLE_DIR to <home>/spools.
        import subprocess
        import sys

        home = tmp_path / "alt-home"
        result = subprocess.run(
            [sys.executable, "-c", "import spindle; print(spindle.SPINDLE_DIR)"],
            capture_output=True,
            text=True,
            env={**os.environ, "SPINDLE_HOME": str(home)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(home / "spools")


class TestExitCodeCapture:
    """Finalization captures the child's exit code via the process handle."""

    class _FakeProc:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    def test_no_output_includes_exit_code(self, tmp_path):
        sid = "ec1"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[sid] = self._FakeProc(3)
            _write_spool(
                sid,
                {
                    "id": sid,
                    "status": "running",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            assert _check_and_finalize_spool(sid) is True
            spool = _read_spool(sid)
            assert spool["status"] == "error"
            assert spool["exit_code"] == 3
            assert "exit code 3" in spool["error"]
            assert sid not in spindle._PROC_HANDLES  # handle reaped/popped

    def test_no_output_without_handle_omits_code(self, tmp_path):
        sid = "ec2"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                sid,
                {
                    "id": sid,
                    "status": "running",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            assert _check_and_finalize_spool(sid) is True
            spool = _read_spool(sid)
            assert spool["status"] == "error"
            assert spool["error"] == "Process exited with no output"
            assert "exit_code" not in spool

    def test_finalize_waits_for_descendants_after_group_leader_exits(self, tmp_path):
        sid = "codex-descendant-still-running"
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[sid] = self._FakeProc(0)
            _write_spool(
                sid,
                {
                    "id": sid,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "x",
                    "pid": 454545,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(sid).write_text(stream)

            with patch("spindle._is_process_group_alive", return_value=True):
                assert _check_and_finalize_spool(sid) is False
            assert _read_spool(sid)["status"] == "running"
            assert _get_output_path(sid).exists()
            assert sid in spindle._PROC_HANDLES

            with patch("spindle._is_process_group_alive", return_value=False):
                assert _check_and_finalize_spool(sid) is True
            assert _read_spool(sid)["status"] == "complete"
            assert not _get_output_path(sid).exists()


class TestCancellationTermination:
    @pytest.mark.parametrize("tool_path", ["sync", "async"])
    def test_drop_escalates_and_does_not_leave_sigterm_ignoring_group(self, tmp_path, tool_path):
        proc = subprocess.Popen(
            ["/bin/sh", "-c", 'trap "" TERM; while :; do sleep 1; done'],
            start_new_session=True,
        )
        spool_id = f"cancel-{tool_path}"
        try:
            time.sleep(0.2)
            with patch("spindle.SPINDLE_DIR", tmp_path):
                _write_spool(
                    spool_id,
                    {
                        "id": spool_id,
                        "status": "running",
                        "pid": proc.pid,
                        "created_at": datetime.now().isoformat(),
                    },
                )
                if tool_path == "sync":
                    result = spindle._spin_drop_sync(spool_id)
                else:
                    drop_tool = spindle.spin_drop.fn if hasattr(spindle.spin_drop, "fn") else spindle.spin_drop
                    result = asyncio.run(drop_tool(spool_id))

                assert result == f"Dropped spool {spool_id}"
                assert spindle._is_process_group_alive(proc.pid) is False
                assert _read_spool(spool_id)["status"] == "error"
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @pytest.mark.parametrize("tool_path", ["sync", "async"])
    def test_drop_lock_prevents_finalizer_from_overwriting_terminal_state(self, tmp_path, tool_path):
        spool_id = f"cancel-finalize-race-{tool_path}"
        finalize_attempts = []

        def terminate_while_finalizer_races(pid, grace):
            finalize_attempts.append(_check_and_finalize_spool(spool_id))
            return True

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "x",
                    "pid": 464646,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(json.dumps({"type": "turn.completed", "usage": {}}))
            _get_exit_path(spool_id).write_text("0\n")
            with patch("spindle._terminate_process_group", side_effect=terminate_while_finalizer_races):
                if tool_path == "sync":
                    result = spindle._spin_drop_sync(spool_id)
                else:
                    drop_tool = spindle.spin_drop.fn if hasattr(spindle.spin_drop, "fn") else spindle.spin_drop
                    result = asyncio.run(drop_tool(spool_id))
            saved = _read_spool(spool_id)

        assert result == f"Dropped spool {spool_id}"
        assert finalize_attempts == [False]
        assert saved["status"] == "error"
        assert saved["error"] == "Cancelled by user"
        assert "result" not in saved


class TestFableGateRefusal:
    """Fable's bio/cyber safety gate surfaces as a distinct, agent-readable state.

    A gate refusal is a successful HTTP 200 with stop_reason "refusal" that the
    CLI reports as an API Error. It is not a task failure — the right response is
    to re-route to another model — so it must be distinguishable from a generic
    error for agents, triage, and skein.
    """

    @staticmethod
    def _cc_refusal_stream(category, message="API Error: Fable 5 has safety measures ...", is_error=True):
        # stop_reason lives ONLY on the result event (where the code reads it).
        # The assistant message carries stop_details (where the category lives)
        # but no stop_reason, so a regression that read message.stop_reason would
        # fail to detect the refusal.
        return json.dumps(
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "stop_details": {"type": "refusal", "category": category, "explanation": None},
                        "content": [],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": is_error,
                    "stop_reason": "refusal",
                    "result": message,
                    "session_id": "sess-gate",
                    "total_cost_usd": 1.23,
                },
            ]
        )

    def test_finalize_marks_gate_refusal_with_category(self, tmp_path):
        spool_id = "gate-bio"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "deprescribing synth",
                    "pid": 999999999,
                    "tags": ["review"],
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream("bio"))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert spool["error_kind"] == "fable_gate"
            assert spool["gate_category"] == "bio"
            # Existing tags preserved; marker appended, not duplicated.
            assert spool["tags"] == ["review", "fable-gate"]

    def test_finalize_unknown_category_when_gate_unnamed(self, tmp_path):
        spool_id = "gate-null"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream(None))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["error_kind"] == "fable_gate"
            assert spool["gate_category"] == "unknown"
            assert "fable-gate" in spool["tags"]

    def test_ordinary_cc_error_is_not_marked_as_gate(self, tmp_path):
        spool_id = "plain-err"
        stream = json.dumps(
            [
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "You've hit your session limit",
                    "session_id": "s",
                },
            ]
        )
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert "error_kind" not in spool
            assert "fable-gate" not in (spool.get("tags") or [])

    def test_refusal_category_scans_assistant_events(self):
        data = [
            {"type": "assistant", "message": {"stop_details": {"type": "refusal", "category": None}}},
            {"type": "assistant", "message": {"stop_details": {"type": "refusal", "category": "cyber"}}},
            {"type": "result", "stop_reason": "refusal"},
        ]
        assert _refusal_category(data) == "cyber"

    def test_refusal_category_defaults_unknown(self):
        assert _refusal_category([{"type": "result", "stop_reason": "refusal"}]) == "unknown"
        assert _refusal_category("not-a-list") == "unknown"

    def test_format_failure_calls_out_gate(self):
        msg = _format_spool_failure(
            "g1",
            {
                "error": "API Error: Fable 5 has safety measures ...",
                "error_kind": "fable_gate",
                "gate_category": "bio",
            },
        )
        assert "FABLE SAFETY GATE (bio)" in msg
        assert "re-route" in msg
        assert "API Error: Fable 5" in msg

    def test_format_failure_plain_error_unchanged(self):
        assert _format_spool_failure("g2", {"error": "boom"}) == "Spool g2 failed: boom"

    def test_unspool_surfaces_gate_to_agent(self, tmp_path):
        spool_id = "gate-unspool"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream("bio"))
            out = _unspool_sync(spool_id)
            assert "FABLE SAFETY GATE (bio)" in out
            assert "re-route to a different model" in out

    def test_fable_detected_via_model_when_gate_text_absent(self, tmp_path):
        # Model is Fable, but the refusal text doesn't echo the CLI gate string.
        # Detection must still attribute it to Fable via the model alias.
        spool_id = "gate-bymodel"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "fable",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream("cyber", message="declined"))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["error_kind"] == "fable_gate"
            assert spool["gate_category"] == "cyber"

    def test_respin_without_model_detected_via_gate_text(self, tmp_path):
        # Continues/respins don't record a model; the Fable gate text is the
        # only signal, and it must be enough.
        spool_id = "gate-respin"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            msg = "API Error: Fable 5 has measures that flagged something in this session"
            _get_output_path(spool_id).write_text(self._cc_refusal_stream(None, message=msg))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["error_kind"] == "fable_gate"

    def test_non_fable_refusal_marked_safety_refusal_not_gate(self, tmp_path):
        # A genuine refusal from another model must NOT be attributed to Fable.
        spool_id = "refuse-opus"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "opus",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream(None, message="I can't help with that."))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert spool["error_kind"] == "safety_refusal"
            assert "fable-gate" not in (spool.get("tags") or [])
            assert "gate_category" not in spool

    def test_non_fable_model_ignores_gate_text(self, tmp_path):
        # A recorded non-Fable model is trusted: even if the refusal text quotes
        # "Fable 5", it must NOT be attributed to Fable's gate.
        spool_id = "refuse-textquote"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "opus",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(
                self._cc_refusal_stream(None, message="I can't help write about Fable 5 exploits.")
            )
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["error_kind"] == "safety_refusal"
            assert "fable-gate" not in (spool.get("tags") or [])

    def test_gate_tag_not_duplicated(self, tmp_path):
        spool_id = "gate-dupe"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "fable",
                    "prompt": "x",
                    "pid": 999999999,
                    "tags": ["fable-gate"],
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream("bio"))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["tags"] == ["fable-gate"]

    def test_refusal_sets_error_when_is_error_absent(self, tmp_path):
        # Defensive: even if a future refusal arrives without is_error, the
        # refusal text must land in spool["error"] so rendering isn't blank.
        spool_id = "gate-noerr"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "fable",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            msg = "API Error: Fable 5 has safety measures ..."
            _get_output_path(spool_id).write_text(self._cc_refusal_stream("bio", message=msg, is_error=False))
            assert _check_and_finalize_spool(spool_id) is True
            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert spool["error_kind"] == "fable_gate"
            assert spool["error"] == msg

    def test_refusal_category_single_dict_old_format(self):
        # Old single-object CC format carrying stop_details on the event itself.
        assert _refusal_category({"type": "result", "stop_details": {"type": "refusal", "category": "bio"}}) == "bio"
        # And on a nested message.
        nested = {"type": "assistant", "message": {"stop_details": {"type": "refusal", "category": "cyber"}}}
        assert _refusal_category(nested) == "cyber"

    def test_format_failure_safety_refusal(self):
        msg = _format_spool_failure("s1", {"error": "I can't help", "error_kind": "safety_refusal"})
        assert "SAFETY REFUSAL" in msg
        assert "FABLE" not in msg
        assert "I can't help" in msg

    def test_unspool_surfaces_safety_refusal(self, tmp_path):
        spool_id = "refuse-unspool"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "claude-code",
                    "model": "opus",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(self._cc_refusal_stream(None, message="declined"))
            out = _unspool_sync(spool_id)
            assert "SAFETY REFUSAL" in out
            assert "FABLE" not in out


class TestReloadDrain:
    """spindle_reload drains the queue by default; force=True restarts now."""

    @staticmethod
    def _fake_systemctl(restart_evt, present=True, active=True):
        def run(cmd, *a, **k):
            m = MagicMock()
            m.returncode = 0
            if "list-unit-files" in cmd:
                m.stdout = "spindle.service enabled" if present else ""
            elif "is-active" in cmd:
                m.returncode = 0 if active else 3
            elif "restart" in cmd or "start" in cmd:
                restart_evt.set()
            return m

        return run

    def test_missing_service_errors(self):
        import threading as _t

        evt = _t.Event()
        with patch("spindle.subprocess.run", self._fake_systemctl(evt, present=False)):
            out = asyncio.run(spindle.spindle_reload.fn())
        assert "not found" in out
        assert not evt.is_set()

    def test_force_restarts_immediately(self):
        import threading as _t

        evt = _t.Event()
        with patch("spindle.subprocess.run", self._fake_systemctl(evt)), patch("spindle.time.sleep", lambda *_: None):
            out = asyncio.run(spindle.spindle_reload.fn(force=True))
            assert "force" in out.lower()
            assert evt.wait(2), "force should restart without draining"

    def test_default_idle_restarts(self):
        # Empty store -> queue already idle -> restarts promptly.
        import threading as _t

        evt = _t.Event()
        with patch("spindle.subprocess.run", self._fake_systemctl(evt)), patch("spindle.time.sleep", lambda *_: None):
            out = asyncio.run(spindle.spindle_reload.fn())
            assert "No spools active" in out
            assert evt.wait(2)

    def test_default_active_reports_draining_then_restarts(self):
        import threading as _t

        evt = _t.Event()
        with (
            patch("spindle.subprocess.run", self._fake_systemctl(evt)),
            patch("spindle.time.sleep", lambda *_: None),
            patch("spindle._count_running", return_value=2),
            patch("spindle._wait_until_idle", lambda *a, **k: None),
        ):
            out = asyncio.run(spindle.spindle_reload.fn())
            assert "Draining: 2 spool(s)" in out
            assert evt.wait(2), "should restart after draining"

    def test_reload_already_pending_does_not_stack(self):
        import threading as _t

        evt = _t.Event()
        spindle._reload_pending = True
        try:
            with (
                patch("spindle.subprocess.run", self._fake_systemctl(evt)),
                patch("spindle._count_running", return_value=1),
            ):
                out = asyncio.run(spindle.spindle_reload.fn())
            assert "already pending" in out
            assert not evt.is_set()
        finally:
            spindle._reload_pending = False


class TestWaitUntilIdle:
    """_wait_until_idle / _spools_idle drain semantics."""

    def test_wait_loops_until_idle(self):
        with patch("spindle._spools_idle", side_effect=[False, False, True]) as si, patch("spindle.time.sleep") as sl:
            spindle._wait_until_idle(poll_interval=0.01)
        assert si.call_count == 3
        assert sl.call_count == 2  # slept after each non-idle check

    def test_spools_idle_finalizes_dead_running_spool(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "d1",
                {
                    "id": "d1",
                    "status": "running",
                    "prompt": "x",
                    "pid": 999999999,
                    "created_at": datetime.now().isoformat(),
                },
            )
            # dead pid, no output -> finalize flips it off "running" -> idle
            assert spindle._spools_idle() is True
            assert _read_spool("d1")["status"] != "running"

    def test_spools_idle_false_when_pending(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "d2",
                {
                    "id": "d2",
                    "status": "pending",
                    "prompt": "x",
                    "created_at": datetime.now().isoformat(),
                },
            )
            assert spindle._spools_idle() is False

    def test_spools_idle_clears_stuck_pending(self, tmp_path):
        # A pending spool that never got a PID and aged past the spawn timeout
        # must not wedge the drain - _recover_orphans times it out.
        old = (datetime.now() - timedelta(seconds=spindle.PENDING_SPAWN_TIMEOUT + 10)).isoformat()
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "d3",
                {
                    "id": "d3",
                    "status": "pending",
                    "prompt": "x",
                    "pid": None,
                    "created_at": old,
                },
            )
            assert spindle._spools_idle() is True
            assert _read_spool("d3")["status"] == "error"


class TestOrphanedLockSweep:
    """_cleanup_old_spools sweeps orphaned per-spool locks but spares the rest."""

    def test_sweeps_only_old_orphaned_per_spool_locks(self, tmp_path):
        import time as _time

        old = _time.time() - 25 * 3600  # past the 24h cutoff
        with patch("spindle.SPINDLE_DIR", tmp_path):
            orphan = tmp_path / "deadspool.lock"
            orphan.write_text("")
            os.utime(orphan, (old, old))

            fresh = tmp_path / "newspool.lock"  # orphaned but recent -> keep (race)
            fresh.write_text("")

            conc = tmp_path / ".concurrency.lock"  # shared lock -> keep (dotfile)
            conc.write_text("")
            os.utime(conc, (old, old))

            live = tmp_path / "alive.lock"  # has a live json -> keep
            live.write_text("")
            os.utime(live, (old, old))
            _write_spool(
                "alive",
                {
                    "id": "alive",
                    "status": "complete",
                    "created_at": datetime.now().isoformat(),
                },
            )

            spindle._cleanup_old_spools()

            assert not orphan.exists()  # swept
            assert fresh.exists()  # too fresh to be safe
            assert conc.exists()  # shared concurrency lock spared
            assert live.exists()  # spool json still present


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

    def test_careful_and_careful_shard_have_no_allowlist(self):
        """careful is now an alias of auto: no allowlist string (None)."""
        assert PERMISSION_PROFILES["careful"] is None
        assert PERMISSION_PROFILES["careful+shard"] is None

    def test_manual_is_exact_readonly_allowlist(self):
        """manual is an exact alias of readonly. There is no manual+shard key — the
        incoherent readonly/manual + shard combos are rejected at spin entry."""
        assert PERMISSION_PROFILES["manual"] == PERMISSION_PROFILES["readonly"]
        assert PERMISSION_PROFILES["manual"] == READONLY_TOOLS
        assert "manual+shard" not in PERMISSION_PROFILES
        assert "readonly+shard" not in PERMISSION_PROFILES

    def test_manual_excludes_python_and_write(self):
        """manual, like readonly, must exclude python execution and write tools."""
        manual = PERMISSION_PROFILES["manual"]
        assert "Bash(python:" not in manual
        assert "Bash(python3:" not in manual
        assert "Write" not in manual
        assert "Edit" not in manual

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
            cmd = _codex_bwrap_wrap(
                ["gemini", "-p", "test", "-s", "-o", "json"],
                shard_info,
                str(worktree_path),
                process_env={"HOME": str(fake_home)},
            )

        gemini_dir_str = str(gemini_dir)
        bind_triple_found = any(
            cmd[i] == "--bind" and cmd[i + 1] == gemini_dir_str and cmd[i + 2] == gemini_dir_str
            for i in range(len(cmd) - 2)
        )
        assert cmd[0] == "/usr/bin/bwrap", f"Expected resolved bwrap wrapper, got {cmd[0]!r}"
        assert bind_triple_found, f"Expected '--bind {gemini_dir_str} {gemini_dir_str}' in bwrap cmd: {cmd!r}"

    def test_bwrap_uses_resolved_binary_and_caller_home_context(self, tmp_path):
        caller_home = tmp_path / "caller-home"
        codex_home = tmp_path / "caller-codex"
        host_home = tmp_path / "host-home"
        worktree = tmp_path / "worktree"
        for path in [caller_home / ".config", codex_home, host_home / ".codex", worktree]:
            path.mkdir(parents=True)
        shard_info = {"worktree_path": str(worktree), "branch_name": "shard-env"}

        with patch("shutil.which", return_value="/caller/bin/bwrap") as which:
            cmd = _codex_bwrap_wrap(
                ["/caller/bin/codex", "exec"],
                shard_info,
                str(worktree),
                process_env={
                    "HOME": str(caller_home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": "/caller/bin",
                },
            )

        binds = {(cmd[index + 1], cmd[index + 2]) for index, item in enumerate(cmd[:-2]) if item == "--bind"}
        which.assert_called_once_with("bwrap", path="/caller/bin")
        assert cmd[0] == "/caller/bin/bwrap"
        assert (str(caller_home / ".config"), str(caller_home / ".config")) in binds
        assert (str(codex_home), str(codex_home)) in binds
        assert (str(host_home / ".codex"), str(host_home / ".codex")) not in binds


class TestKimiHarness:
    """Test Kimi CLI harness implementation."""

    @pytest.fixture(autouse=True)
    def _skip_model_validation(self):
        """Disable model-config validation by default so command-construction tests
        don't depend on the machine's ~/.kimi/config.toml. Validation-specific tests
        re-patch _kimi_registered_models with an explicit set."""
        with patch("spindle._kimi_registered_models", return_value=None):
            yield

    def test_kimi_model_aliases(self):
        """Aliases should resolve only to models the managed provider actually serves."""
        assert KIMI_MODEL_ALIASES["thinking"] == "moonshot-ai/kimi-k2.6"
        assert KIMI_MODEL_ALIASES["k2.6"] == "moonshot-ai/kimi-k2.6"
        assert KIMI_MODEL_ALIASES["k2.5"] == "moonshot-ai/kimi-k2.5"
        assert KIMI_MODEL_ALIASES["latest"] == "moonshot-ai/kimi-k2.6"
        # The retired standalone thinking/turbo models must not reappear as alias targets.
        assert "moonshot-ai/kimi-k2-thinking" not in KIMI_MODEL_ALIASES.values()
        assert "moonshot-ai/kimi-k2-turbo-preview" not in KIMI_MODEL_ALIASES.values()
        # Default model must be a real, registerable model (regression: was kimi-k2-thinking).
        assert KIMI_DEFAULT_MODEL == "moonshot-ai/kimi-k2.6"
        # k2.7-code (2026-06-12): coding-specialized aliases resolve to the served model.
        assert KIMI_MODEL_ALIASES["k2.7-code"] == "moonshot-ai/kimi-k2.7-code"
        assert KIMI_MODEL_ALIASES["k2.7"] == "moonshot-ai/kimi-k2.7-code"
        assert KIMI_MODEL_ALIASES["code"] == "moonshot-ai/kimi-k2.7-code"
        assert KIMI_MODEL_ALIASES["highspeed"] == "moonshot-ai/kimi-k2.7-code-highspeed"

    def test_kimi_k2_7_code_forces_thinking_via_alias(self, tmp_path):
        """k2.7-code is thinking-only; selecting it by alias must add --thinking."""
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
                        model="k2.7-code",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "moonshot-ai/kimi-k2.7-code" in captured_cmd
        assert "--thinking" in captured_cmd

    def test_kimi_k2_7_code_forces_thinking_via_full_model(self, tmp_path):
        """k2.7-code reached by full model name (not an alias) must still force
        --thinking — the endpoint rejects requests with thinking disabled."""
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
                        model="moonshot-ai/kimi-k2.7-code-highspeed",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "moonshot-ai/kimi-k2.7-code-highspeed" in captured_cmd
        assert "--thinking" in captured_cmd

    def test_kimi_spin_resolves_alias(self, tmp_path):
        """The 'thinking' alias resolves to kimi-k2.6 and enables thinking mode."""
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

        assert "moonshot-ai/kimi-k2.6" in captured_cmd
        assert "--thinking" in captured_cmd

    def test_kimi_spin_full_model_no_thinking_flag(self, tmp_path):
        """A full model name (not the 'thinking' alias) must not force thinking mode."""
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
                        model="moonshot-ai/kimi-k2.6",
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "moonshot-ai/kimi-k2.6" in captured_cmd
        assert "--thinking" not in captured_cmd

    def test_kimi_spin_default_model(self, tmp_path):
        """No model specified defaults to kimi-k2.6 (regression: was the unregistered
        kimi-k2-thinking, which made kimi-cli report 'LLM not set')."""
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
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        m_idx = captured_cmd.index("-m")
        assert captured_cmd[m_idx + 1] == "moonshot-ai/kimi-k2.6"
        assert "--thinking" not in captured_cmd

    def test_kimi_spin_rejects_unregistered_model(self, tmp_path):
        """An unregistered model is rejected up front with a clear error instead of
        letting kimi-cli silently fall back and emit only 'LLM not set'."""
        spawned = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            spawned.append(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    # Only k2.6 is registered; the retired kimi-k2-thinking is not.
                    with patch(
                        "spindle._kimi_registered_models",
                        return_value={"moonshot-ai/kimi-k2.6"},
                    ):
                        result = _kimi_spin_sync(
                            prompt="Test",
                            working_dir=str(tmp_path),
                            model="moonshot-ai/kimi-k2-thinking",
                            system_prompt=None,
                            timeout=None,
                            tags=None,
                            env=None,
                        )

        assert result.startswith("Error:")
        assert "moonshot-ai/kimi-k2-thinking" in result
        assert "LLM not set" in result
        # No process spawned and no spool slot left behind.
        assert spawned == []
        assert list(tmp_path.glob("kimi-*.json")) == []

    def test_kimi_registered_models_unknown_when_no_config(self, tmp_path):
        """Validation degrades to 'allow' (None) when the config can't be read."""
        import spindle

        missing = tmp_path / "nope" / "config.toml"
        with patch("spindle._kimi_config_path", return_value=missing):
            assert spindle._kimi_registered_models() is None

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
                        model="moonshot-ai/kimi-k2.6",
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
        assert "moonshot-ai/kimi-k2.6" in captured_cmd
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

    def test_kimi_respin_inherits_thinking_flag(self, tmp_path):
        """Respinning a spool whose stored state has thinking=True re-appends --thinking."""
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
                        "model": "moonshot-ai/kimi-k2.6",
                        "thinking": True,
                        "env": None,
                    }

                    _kimi_respin_sync(
                        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        prompt="Follow up question",
                        original_spool=original_spool,
                    )

        assert "--thinking" in captured_cmd

    def test_kimi_respin_without_thinking_omits_flag(self, tmp_path):
        """Respinning a spool whose stored state lacks thinking must not add --thinking."""
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
                        "model": "moonshot-ai/kimi-k2.6",
                        "thinking": False,
                        "env": None,
                    }

                    _kimi_respin_sync(
                        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        prompt="Follow up question",
                        original_spool=original_spool,
                    )

        assert "--thinking" not in captured_cmd


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

    def test_claude_code_models_match_aliases(self):
        """Claude Code models in harnesses should match CLAUDE_MODEL_ALIASES."""
        result = _get_harnesses()
        assert result["claude-code"]["models"] == CLAUDE_MODEL_ALIASES

    def test_claude_code_advertises_frontier_aliases(self):
        """spin_harnesses should advertise the fable and opus-4.8 aliases."""
        result = _get_harnesses()
        models = result["claude-code"]["models"]
        assert models["fable"] == "claude-fable-5"
        assert models["fable-5"] == "claude-fable-5"
        assert models["opus-4.8"] == "claude-opus-4-8"

    def test_claude_code_default_model_unchanged(self):
        """Adding frontier aliases must not change the claude-code default."""
        result = _get_harnesses()
        assert result["claude-code"]["default_model"] == "sonnet"

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
                with patch("spindle._codex_sandbox_enforces", return_value=True):
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

    def test_codex_manual_permission_maps_to_read_only_sandbox(self, tmp_path):
        """Finding A: spin(harness='codex', permission='manual') resolves to the
        read-only sandbox, exactly like readonly — not the workspace-write default."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        captured = {}

        def fake_codex(prompt, working_dir, model, sandbox, timeout, tags, env, **kwargs):
            captured["sandbox"] = sandbox
            return "codex-manual"

        with patch("spindle._codex_spin_sync", side_effect=fake_codex):
            result = asyncio.run(
                _spin(
                    "inspect the tree",
                    harness="codex",
                    permission="manual",
                    working_dir=str(tmp_path),
                    skeinless=True,
                )
            )

        assert result == "codex-manual"
        assert captured["sandbox"] == "read-only"

    def test_codex_spin_pins_sandbox_mode_config(self, tmp_path):
        """Finding B: every codex exec launch pairs --sandbox <mode> with a matching
        -c sandbox_mode=<mode>, so ~/.codex/config.toml can never widen the tier."""
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._codex_sandbox_enforces", return_value=True):
                    with patch("spindle._spawn_detached", side_effect=fake_spawn):
                        result = _codex_spin_sync(
                            prompt="inspect the tree",
                            working_dir=str(tmp_path),
                            model=None,
                            sandbox="read-only",
                            timeout=None,
                            tags=None,
                            env=None,
                            permission="manual",
                        )

        assert result.startswith("codex-")
        sandbox_idx = captured_cmd.index("--sandbox")
        assert captured_cmd[sandbox_idx + 1] == "read-only"
        # The matching -c sandbox_mode=read-only pins the tier against a config.toml override.
        c_positions = [i for i, tok in enumerate(captured_cmd) if tok == "-c"]
        assert any(captured_cmd[i + 1] == "sandbox_mode=read-only" for i in c_positions), captured_cmd

    def test_codex_sandbox_probe_logs_fail_closed_reason(self, caplog):
        """Finding D: a fail-closed probe logs why (the binary + read-only mode + the
        reason) instead of swallowing it, so a refusal that blocks real spools is
        debuggable. Here no CLI shape emits the marker, so the probe fails closed."""
        import logging

        # Every candidate shape returns clean output WITHOUT the probe marker, so the
        # command is treated as never-having-run -> no known shape -> fail closed.
        clean = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with caplog.at_level(logging.WARNING):
            with patch("spindle.subprocess.run", return_value=clean):
                result = spindle._codex_sandbox_probe("/fake/codex/bin")

        assert result is False
        assert "no known CLI shape" in caplog.text
        assert "/fake/codex/bin" in caplog.text
        assert "read-only" in caplog.text

    def test_auto_permission_on_codex_returns_error(self):
        """permission='auto' on codex must return an error, not silently degrade."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(_spin("autonomous task", harness="codex", permission="auto"))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "codex" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_auto_plus_shard_permission_on_codex_returns_error(self):
        """permission='auto+shard' on codex must return an error, not silently degrade."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(_spin("autonomous task", harness="codex", permission="auto+shard"))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "codex" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_auto_permission_on_gemini_returns_error(self):
        """permission='auto' on gemini must return an error, not silently degrade."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(_spin("autonomous task", harness="gemini", permission="auto"))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "gemini" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_auto_permission_on_kimi_returns_error(self):
        """permission='auto' on kimi must return an error, not silently degrade."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        result = asyncio.run(_spin("autonomous task", harness="kimi", permission="auto"))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "kimi" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_auto_permission_does_not_spawn_on_non_cc_harness(self):
        """Verifies the error is returned before any spawn attempt on codex."""
        _spin = spin.fn if hasattr(spin, "fn") else spin
        with patch("spindle._codex_spin_sync") as mock_codex:
            result = asyncio.run(_spin("autonomous task", harness="codex", permission="auto"))
        parsed = json.loads(result)
        assert "error" in parsed
        mock_codex.assert_not_called()


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

    def test_codex_spawn_failure_really_cleans_pristine_shard_and_repairs_retry_dir(self, tmp_path):
        repo = tmp_path / "repo"
        worktree = tmp_path / "worktree"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "shard-spawn-failure", str(worktree), "main"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        shard = {
            "worktree_path": str(worktree),
            "branch_name": "shard-spawn-failure",
            "shard_id": "spawn-failure",
        }
        spool_dir = tmp_path / "spools"

        with patch("spindle.SPINDLE_DIR", spool_dir):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._detect_existing_shard", return_value=None):
                        with patch("spindle._spawn_shard", return_value=(shard, None)):
                            with patch("spindle._resolve_codex_binary", return_value="/fake/codex"):
                                with patch("spindle._codex_sandbox_enforces", return_value=True):
                                    with patch("spindle._spawn_detached", side_effect=OSError("boom")):
                                        result = _codex_spin_sync(
                                            "do work",
                                            str(repo),
                                            None,
                                            "workspace-write",
                                            None,
                                            None,
                                            None,
                                            shard=True,
                                            base_branch="main",
                                        )

            assert result.startswith("Error: Failed to spawn process")
            spool = _list_spools()[0]
            assert spool["status"] == "error"
            assert spool["working_dir"] == str(repo)
            assert spool["shard"]["startup_failure_cleaned"] is True

        assert not worktree.exists()
        assert (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/shard-spawn-failure"], cwd=repo
            ).returncode
            != 0
        )

    def test_codex_spawn_failure_preserves_shard_used_by_running_spool(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        shard = {
            "worktree_path": str(worktree),
            "branch_name": "shard-spawn-failure",
            "shard_id": "spawn-failure",
        }
        spool_dir = tmp_path / "spools"
        with patch("spindle.SPINDLE_DIR", spool_dir):
            _write_spool(
                "other-running",
                {
                    "id": "other-running",
                    "status": "running",
                    "working_dir": str(worktree),
                    "pid": 22222,
                },
            )
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._detect_existing_shard", return_value=None):
                        with patch("spindle._spawn_shard", return_value=(shard, None)):
                            with patch("spindle._resolve_codex_binary", return_value="/fake/codex"):
                                with patch("spindle._codex_sandbox_enforces", return_value=True):
                                    with patch("spindle._spawn_detached", side_effect=OSError("boom")):
                                        with patch("spindle._shard_cleanup_state", return_value="pristine"):
                                            with patch("spindle._is_pid_alive", return_value=True):
                                                with patch("spindle._cleanup_shard") as cleanup:
                                                    _codex_spin_sync(
                                                        "do work",
                                                        str(tmp_path),
                                                        None,
                                                        "workspace-write",
                                                        None,
                                                        None,
                                                        None,
                                                        shard=True,
                                                    )
            cleanup.assert_not_called()


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

    def test_recovery_restarts_monitor_for_still_running_process(self, tmp_path):
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "running-after-restart",
                {
                    "id": "running-after-restart",
                    "status": "running",
                    "pid": 12345,
                    "created_at": datetime.now().isoformat(),
                },
            )
            with patch("spindle._check_and_finalize_spool", return_value=False):
                with patch("spindle._start_spool_monitor") as start:
                    _recover_orphans()
            start.assert_called_once_with("running-after-restart")

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
                            with patch("spindle._codex_sandbox_enforces", return_value=True):
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
                    with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                        with patch("spindle._detect_existing_shard", return_value=None):
                            with patch("spindle._codex_sandbox_enforces", return_value=True):
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
                    with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                        with patch("spindle._detect_existing_shard", return_value=None):
                            with patch("shutil.which", return_value="/usr/bin/bwrap"):
                                with patch("spindle._resolve_codex_binary", return_value="/usr/bin/codex"):
                                    with patch("spindle._codex_sandbox_enforces", return_value=True):
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
        assert cmd[0] == "/usr/bin/bwrap", f"Expected bwrap wrapper for shard, got {cmd[0]!r}"
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
        assert "/usr/bin/codex" in cmd, f"Expected resolved codex in bwrap-wrapped command: {cmd!r}"

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
                    with patch("spindle._spawn_shard", return_value=(fake_shard, None)):
                        with patch("spindle._detect_existing_shard", return_value=None):
                            with patch("shutil.which", return_value=None):
                                with patch("spindle._codex_sandbox_enforces", return_value=True):
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
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._codex_sandbox_enforces", return_value=True):
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
                f"--add-dir at index {idx} must come before `resume` at index {resume_idx}; cmd={cmd!r}"
            )
        cd_idx = next((i for i, tok in enumerate(cmd) if tok == "--cd"), None)
        assert cd_idx is not None, "--cd must be present in codex respin command"
        assert cd_idx < resume_idx, (
            f"--cd at index {cd_idx} must come before `resume` at index {resume_idx}; cmd={cmd!r}"
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
                    with patch("spindle._codex_sandbox_enforces", return_value=True):
                        _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1, "Expected one spawn for codex respin"
        cmd = captured_cmd[0]
        assert "--cd" in cmd, f"Expected --cd in codex respin command, got {cmd!r}"
        cd_idx = cmd.index("--cd")
        resume_idx = cmd.index("resume")
        assert cd_idx < resume_idx, f"Expected --cd before resume in codex respin command, got {cmd!r}"
        assert cmd[cd_idx + 1] == str(worktree_path)

    def test_codex_respin_pins_sandbox_mode_config(self, tmp_path):
        """Finding B: a codex respin also pairs --sandbox <tier> with a matching
        -c sandbox_mode=<tier>, so config.toml can't widen the resumed tier."""
        session_id = "codex-session-pin"
        original_spool = {
            "id": "codex-original-pin",
            "status": "complete",
            "session_id": session_id,
            "working_dir": str(tmp_path),
            "harness": "codex",
            "permission": "readonly",
            "sandbox": "read-only",
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
                    with patch("spindle._codex_sandbox_enforces", return_value=True):
                        _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "read-only"
        c_positions = [i for i, tok in enumerate(cmd) if tok == "-c"]
        assert any(cmd[i + 1] == "sandbox_mode=read-only" for i in c_positions), cmd

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
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("shutil.which", return_value="/usr/bin/bwrap"):
                        with patch("spindle._resolve_codex_binary", return_value="/usr/bin/codex"):
                            with patch("spindle._codex_sandbox_enforces", return_value=True):
                                _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1, "Expected one spawn for codex respin"
        cmd = captured_cmd[0]
        assert cmd[0] == "/usr/bin/bwrap", f"Expected bwrap wrapper for respin shard, got {cmd[0]!r}"
        assert "--ro-bind" in cmd
        worktree_root = str(worktree_path)
        rw_bind_found = any(
            cmd[i] == "--bind" and cmd[i + 1] == worktree_root and cmd[i + 2] == worktree_root
            for i in range(len(cmd) - 2)
        )
        assert rw_bind_found, f"Expected '--bind {worktree_root} {worktree_root}' in respin cmd: {cmd!r}"
        assert "--chdir" in cmd
        assert "/usr/bin/codex" in cmd

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
            with patch("spindle._spawn_detached", side_effect=fake_detached):
                with patch("spindle._count_running", return_value=0):
                    with patch("shutil.which", return_value=None):
                        with patch("spindle._codex_sandbox_enforces", return_value=True):
                            _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == "codex", f"Expected direct codex respin when bwrap missing, got {cmd[0]!r}"
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "bwrap" in out.lower()

    def test_codex_respin_research_shard_binds_output_dir_writable(self, tmp_path):
        """A research+shard respin must bind its research OUTPUT dir writable in the
        outer bwrap, not the worktree root — mirroring _codex_spin_sync. Without
        research_target_info flowing to _codex_bwrap_wrap, codex --add-dir grants the
        output dir but bwrap only binds the worktree root, so the write is blocked at
        the bwrap layer even though codex was granted it."""
        worktree_path = tmp_path / "worktrees" / "codex-respin-research"
        worktree_path.mkdir(parents=True)
        output_dir = tmp_path / "research-out"
        output_dir.mkdir()
        session_id = "codex-session-research"
        original_spool = {
            "id": "codex-original-research",
            "status": "complete",
            "session_id": session_id,
            "working_dir": str(worktree_path),
            "shard": {
                "worktree_path": str(worktree_path),
                "branch_name": "shard-codex-respin-research",
                "shard_id": "codex-respin-research",
            },
            "permission": "research+shard",
            "research_target": f"dir:{output_dir}",
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
                    with patch("shutil.which", return_value="/usr/bin/bwrap"):
                        with patch("spindle._codex_sandbox_enforces", return_value=True):
                            _codex_respin_sync(session_id, "follow up")

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == "/usr/bin/bwrap", f"expected bwrap wrapper, got {cmd[0]!r}"
        out = str(output_dir)
        wt = str(worktree_path)
        # The research output dir is the writable bind...
        out_bound = any(cmd[i] == "--bind" and cmd[i + 1] == out and cmd[i + 2] == out for i in range(len(cmd) - 2))
        assert out_bound, f"research output dir must be bound writable (--bind {out} {out}): {cmd!r}"
        # ...not the worktree root (which the non-research path binds instead).
        wt_bound = any(cmd[i] == "--bind" and cmd[i + 1] == wt and cmd[i + 2] == wt for i in range(len(cmd) - 2))
        assert not wt_bound, f"worktree root must not be the writable bind for a research respin: {cmd!r}"
        # And codex still gets the --add-dir grant for that dir.
        add_dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
        assert out in add_dirs, f"codex must --add-dir the research output dir {out}: {add_dirs!r}"


class TestCodexSandboxEnforcement:
    """Codex must actually run at the sandbox tier its permission asks for.

    Codex sandboxes with its own vendored bubblewrap and needs no kernel Landlock, so the
    tier is passed unconditionally. These tests pin the flag onto the command, the truth of
    the record, and the tier surviving a respin.
    """

    @contextmanager
    def _captured_codex_spin(self, tmp_path, enforces=True, auth_mode="chatgpt"):
        """Run _codex_spin_sync with a stable codex binary, capturing the spawned argv.

        The enforcement probe is stubbed (default: enforcing) so these tests never shell out
        to a real codex; set enforces=False to drive the fail-closed refusal path.
        """
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._resolve_codex_binary", return_value="/fake/bin/codex"):
                        with patch("spindle._codex_cli_version", return_value="0.125.0"):
                            with patch("spindle._codex_auth_mode", return_value=auth_mode):
                                with patch("spindle._codex_sandbox_enforces", return_value=enforces):
                                    with patch("spindle._spawn_detached", side_effect=fake_detached):
                                        yield captured_cmd

    @pytest.mark.parametrize(
        "permission,expected_sandbox",
        [
            ("readonly", "read-only"),
            ("careful", "workspace-write"),
            ("research", "read-only"),
            ("full", "danger-full-access"),
            (None, "workspace-write"),
        ],
    )
    def test_sandbox_flag_passed_for_each_tier(self, tmp_path, permission, expected_sandbox):
        """Every tier reaches codex as an explicit --sandbox value."""
        sandbox = _codex_sandbox_for_permission(permission, None)
        assert sandbox == expected_sandbox, f"permission {permission!r} should map to {expected_sandbox!r}"

        with self._captured_codex_spin(tmp_path) as captured_cmd:
            _codex_spin_sync(
                "do work",
                str(tmp_path),
                None,
                sandbox,
                None,
                None,
                None,
                permission=permission,
            )

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert "--sandbox" in cmd, f"Expected --sandbox in codex command, got {cmd!r}"
        assert cmd[cmd.index("--sandbox") + 1] == expected_sandbox, f"got {cmd!r}"

    def test_normal_spin_never_bypasses_the_sandbox(self, tmp_path):
        """The bypass flag disables enforcement outright; it must not appear on the spin path."""
        with self._captured_codex_spin(tmp_path) as captured_cmd:
            _codex_spin_sync("do work", str(tmp_path), None, "read-only", None, None, None, permission="readonly")

        cmd = captured_cmd[0]
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd, f"got {cmd!r}"

    @pytest.mark.parametrize(
        "requested,expected",
        [
            (None, "gpt-5.6-sol"),
            ("gpt-5.6", "gpt-5.6-sol"),
            ("5.6", "gpt-5.6-sol"),
            ("sol", "gpt-5.6-sol"),
            ("gpt-5.6-terra", "gpt-5.6-terra"),
            ("gpt-5.6-luna", "gpt-5.6-luna"),
        ],
    )
    def test_gpt_56_family_spelling_normalizes_to_concrete_tier(self, tmp_path, requested, expected):
        with self._captured_codex_spin(tmp_path) as captured_cmd:
            with patch("spindle.threading.Thread"):
                _codex_spin_sync("do work", str(tmp_path), requested, "read-only", None, None, None)

        cmd = captured_cmd[0]
        assert cmd[cmd.index("--model") + 1] == expected
        spool = json.loads(next(tmp_path.glob("codex-*.json")).read_text())
        assert spool["model"] == expected

    @pytest.mark.parametrize("auth_mode", ["api", "unknown"])
    def test_explicit_gpt_56_is_preserved_outside_chatgpt_auth(self, tmp_path, auth_mode):
        with self._captured_codex_spin(tmp_path, auth_mode=auth_mode) as captured_cmd:
            with patch("spindle.threading.Thread"):
                _codex_spin_sync("do work", str(tmp_path), "gpt-5.6", "read-only", None, None, None)

        cmd = captured_cmd[0]
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6"
        spool = json.loads(next(tmp_path.glob("codex-*.json")).read_text())
        assert spool["model"] == "gpt-5.6"
        assert spool["codex_auth_mode"] == auth_mode

    def test_spin_probes_and_launches_codex_from_caller_environment(self, tmp_path):
        bin_dir = tmp_path / "custom-bin"
        codex_home = tmp_path / "custom-codex-home"
        bin_dir.mkdir()
        codex_home.mkdir()
        codex = bin_dir / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo \'codex-cli 9.9.9\'; exit 0; fi\n'
            'if [ "$1" = "login" ]; then echo \'Logged in using ChatGPT\'; exit 0; fi\n'
        )
        codex.chmod(0o755)
        caller_env = {"PATH": str(bin_dir), "CODEX_HOME": str(codex_home)}
        captured = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured.append((list(cmd), dict(env)))
            return 99999

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._has_skein", return_value=False):
                    with patch("spindle._codex_sandbox_enforces", return_value=True) as enforces:
                        with patch("spindle._spawn_detached", side_effect=fake_detached):
                            with patch("spindle.threading.Thread"):
                                spool_id = _codex_spin_sync(
                                    "do work",
                                    str(tmp_path),
                                    "gpt-5.6",
                                    "read-only",
                                    None,
                                    None,
                                    caller_env,
                                )

            spool = _read_spool(spool_id)

        cmd, launched_env = captured[0]
        assert cmd[0] == str(codex)
        assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
        assert launched_env["PATH"] == str(bin_dir)
        assert launched_env["CODEX_HOME"] == str(codex_home)
        assert spool["codex_bin"] == str(codex)
        assert spool["codex_version"] == "9.9.9"
        assert spool["codex_auth_mode"] == "chatgpt"
        assert enforces.call_args.args[0] == str(codex)
        assert enforces.call_args.args[1]["CODEX_HOME"] == str(codex_home)

    def test_spin_never_passes_full_auto(self, tmp_path):
        """--full-auto silently overrides --sandbox with its own workspace-write tier.

        Verified against codex 0.125.0: `codex exec --full-auto --sandbox read-only` reports
        "sandbox: workspace-write [workdir, /tmp, $TMPDIR]" and writes outside the workspace,
        while the argv still reads --sandbox read-only. It buys nothing either — `codex exec`
        is already non-interactive. This is the regression guard for re-adding it.
        """
        with self._captured_codex_spin(tmp_path) as captured_cmd:
            _codex_spin_sync("do work", str(tmp_path), None, "read-only", None, None, None, permission="readonly")

        cmd = captured_cmd[0]
        assert "--full-auto" not in cmd, f"--full-auto would nullify --sandbox, got {cmd!r}"

    def test_respin_never_passes_full_auto(self, tmp_path):
        """A respin must not re-widen the tier via --full-auto either."""
        original = {
            "id": "codex-orig-fullauto",
            "status": "complete",
            "session_id": "sess-fullauto",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",
            "permission": "readonly",
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, _, _ = self._respin_with_spool(tmp_path, original)

        assert "--full-auto" not in cmd, f"--full-auto would nullify --sandbox, got {cmd!r}"

    def test_record_sandbox_is_what_was_actually_passed(self, tmp_path):
        """The record must state the tier codex ran at, not the one that was merely requested."""
        with self._captured_codex_spin(tmp_path) as captured_cmd:
            spool_id = _codex_spin_sync(
                "do work",
                str(tmp_path),
                None,
                "read-only",
                None,
                None,
                None,
                permission="readonly",
            )

        cmd = captured_cmd[0]
        passed_sandbox = cmd[cmd.index("--sandbox") + 1]

        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool = _read_spool(spool_id)

        assert spool["sandbox"] == passed_sandbox == "read-only"
        assert spool["permission"] == "readonly"

    def test_record_carries_resolved_codex_binary_and_version(self, tmp_path):
        """Enforcement varies by codex version, so which binary ran has to be recoverable."""
        with self._captured_codex_spin(tmp_path):
            spool_id = _codex_spin_sync(
                "do work", str(tmp_path), None, "read-only", None, None, None, permission="readonly"
            )

        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool = _read_spool(spool_id)

        assert spool["codex_bin"] == "/fake/bin/codex"
        assert spool["codex_version"] == "0.125.0"

    @pytest.mark.parametrize(
        "permission,sandbox",
        [("readonly", "read-only"), ("careful", "workspace-write"), (None, "workspace-write")],
    )
    def test_spin_refuses_restrictive_tier_when_sandbox_not_enforcing(self, tmp_path, permission, sandbox):
        """A fail-open codex must not run a restrictive spool: refuse, launch nothing, record why."""
        with self._captured_codex_spin(tmp_path, enforces=False) as captured_cmd:
            result = _codex_spin_sync("do work", str(tmp_path), None, sandbox, None, None, None, permission=permission)

        # No process launched.
        assert captured_cmd == [], f"expected no launch on refusal, got {captured_cmd!r}"

        # The refusal is in the returned value...
        assert "REFUSED" in result
        assert "not enforcing" in result

        # ...and persisted in the spool record so unspool/spool_info surface it.
        spool_id = result.split("spool ")[-1].rstrip(")")
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert "REFUSED" in spool["sandbox_error"]
        assert "REFUSED" in spool["error"]
        assert spool["sandbox"] == sandbox
        assert spool["codex_bin"] == "/fake/bin/codex"

    def test_spin_does_not_refuse_full_access_when_sandbox_not_enforcing(self, tmp_path):
        """danger-full-access asks for no sandbox, so a fail-open probe must not block it."""
        with self._captured_codex_spin(tmp_path, enforces=False) as captured_cmd:
            result = _codex_spin_sync(
                "do work", str(tmp_path), None, "danger-full-access", None, None, None, permission="full"
            )

        assert "REFUSED" not in result
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"

    def test_spin_does_not_probe_for_full_access(self, tmp_path):
        """The enforcement probe must not even run for a tier that expects no sandbox."""
        with self._captured_codex_spin(tmp_path) as _:
            with patch("spindle._codex_sandbox_enforces", side_effect=AssertionError("must not probe")):
                result = _codex_spin_sync(
                    "do work", str(tmp_path), None, "danger-full-access", None, None, None, permission="full"
                )
        assert result.startswith("codex-"), f"expected a spool id, got {result!r}"

    def test_enforcing_binary_is_not_refused(self, tmp_path):
        """The healthy path: an enforcing binary runs a restrictive spool normally, no refusal."""
        with self._captured_codex_spin(tmp_path, enforces=True) as captured_cmd:
            result = _codex_spin_sync(
                "do work", str(tmp_path), None, "read-only", None, None, None, permission="readonly"
            )
        assert result.startswith("codex-")
        assert "REFUSED" not in result
        assert len(captured_cmd) == 1

    def _respin_with_spool(self, tmp_path, original_spool, enforces=True):
        """Run _codex_respin_sync against a stored spool, capturing the spawned argv.

        The enforcement probe is stubbed (default: enforcing); set enforces=False to drive
        the fail-closed refusal path. When refused nothing spawns, so callers that expect a
        refusal read the returned value / spool record rather than captured_cmd.
        """
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        state = tmp_path / "spindle_state"
        state.mkdir(exist_ok=True)
        with patch("spindle.SPINDLE_DIR", state):
            _write_spool(original_spool["id"], original_spool)
            with patch("spindle._resolve_codex_binary", return_value="/fake/bin/codex"):
                with patch("spindle._codex_cli_version", return_value="0.125.0"):
                    with patch("spindle._codex_sandbox_enforces", return_value=enforces):
                        with patch("spindle._spawn_detached", side_effect=fake_detached):
                            with patch("spindle._count_running", return_value=0):
                                result = _codex_respin_sync(original_spool["session_id"], "follow up")
        cmd = captured_cmd[0] if captured_cmd else None
        return cmd, result, state

    def test_respin_carries_the_tier(self, tmp_path):
        """A respin of a readonly session must stay read-only."""
        original = {
            "id": "codex-orig-ro",
            "status": "complete",
            "session_id": "sess-readonly",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",
            "permission": "readonly",
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, _, _ = self._respin_with_spool(tmp_path, original)

        assert "--sandbox" in cmd, f"Expected --sandbox in respin command, got {cmd!r}"
        assert cmd[cmd.index("--sandbox") + 1] == "read-only", f"got {cmd!r}"
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd, f"got {cmd!r}"

    def test_respin_sandbox_flag_precedes_resume_subcommand(self, tmp_path):
        """`codex exec resume` rejects --sandbox; it is only valid before the subcommand."""
        original = {
            "id": "codex-orig-order",
            "status": "complete",
            "session_id": "sess-order",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",
            "permission": "readonly",
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, _, _ = self._respin_with_spool(tmp_path, original)

        assert cmd.index("--sandbox") < cmd.index("resume"), f"got {cmd!r}"

    def test_respin_record_carries_tier_forward_for_chained_respins(self, tmp_path):
        """A respin record shares its session_id, so it must carry the tier itself.

        _list_spools globs in arbitrary order, so a second respin may resolve to this
        record rather than the original spin; both must yield the same tier.
        """
        original = {
            "id": "codex-orig-chain",
            "status": "complete",
            "session_id": "sess-chain",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",
            "permission": "readonly",
            "harness": "codex",
            "tags": ["codex"],
        }
        _, respin_id, state = self._respin_with_spool(tmp_path, original)

        with patch("spindle.SPINDLE_DIR", state):
            respin_spool = _read_spool(respin_id)

        assert respin_spool["sandbox"] == "read-only"
        assert respin_spool["permission"] == "readonly"
        assert _codex_respin_sandbox(respin_spool) == "read-only"

    def test_respin_of_legacy_record_without_permission_uses_recorded_sandbox(self, tmp_path):
        """Pre-fix records carry no permission; their recorded tier is the intended one."""
        legacy = {
            "id": "codex-legacy",
            "status": "complete",
            "session_id": "sess-legacy",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",  # recorded but never applied by the old code
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, _, _ = self._respin_with_spool(tmp_path, legacy)

        assert cmd[cmd.index("--sandbox") + 1] == "read-only", f"got {cmd!r}"

    def test_respin_sandbox_resolution_precedence(self):
        """Recorded tier wins; permission is the fallback; workspace-write is the floor."""
        assert _codex_respin_sandbox({"sandbox": "read-only", "permission": "full"}) == "read-only"
        assert _codex_respin_sandbox({"permission": "readonly"}) == "read-only"
        assert _codex_respin_sandbox({"sandbox": "bogus-mode", "permission": "readonly"}) == "read-only"
        assert _codex_respin_sandbox({}) == "workspace-write"
        assert _codex_respin_sandbox(None) == "workspace-write"

    def test_cli_shard_full_access_survives_respin(self, tmp_path):
        """A CLI `shard` spool resolves to danger-full-access; re-deriving would narrow it."""
        original = {
            "id": "codex-orig-cli-shard",
            "status": "complete",
            "session_id": "sess-cli-shard",
            "working_dir": str(tmp_path),
            "sandbox": _codex_sandbox_for_permission("shard", None, cli_shard_full_access=True),
            "permission": "shard",
            "harness": "codex",
            "tags": ["codex"],
        }
        assert original["sandbox"] == "danger-full-access"
        cmd, _, _ = self._respin_with_spool(tmp_path, original)

        assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access", f"got {cmd!r}"

    def test_respin_refuses_restrictive_tier_when_sandbox_not_enforcing(self, tmp_path):
        """A respin of a readonly session on a fail-open codex must refuse, like a fresh spin."""
        original = {
            "id": "codex-orig-refuse",
            "status": "complete",
            "session_id": "sess-refuse",
            "working_dir": str(tmp_path),
            "sandbox": "read-only",
            "permission": "readonly",
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, result, state = self._respin_with_spool(tmp_path, original, enforces=False)

        assert cmd is None, f"expected no launch on refusal, got {cmd!r}"
        assert "REFUSED" in result
        spool_id = result.split("spool ")[-1].rstrip(")")
        with patch("spindle.SPINDLE_DIR", state):
            spool = _read_spool(spool_id)
        assert spool["status"] == "error"
        assert "REFUSED" in spool["sandbox_error"]
        assert spool["sandbox"] == "read-only"
        assert spool["session_id"] == "sess-refuse"

    def test_respin_does_not_refuse_full_access_when_sandbox_not_enforcing(self, tmp_path):
        """A danger-full-access session respins normally even when the probe reports fail-open."""
        original = {
            "id": "codex-orig-full-noref",
            "status": "complete",
            "session_id": "sess-full-noref",
            "working_dir": str(tmp_path),
            "sandbox": "danger-full-access",
            "permission": "full",
            "harness": "codex",
            "tags": ["codex"],
        }
        cmd, result, _ = self._respin_with_spool(tmp_path, original, enforces=False)

        assert cmd is not None, "danger-full-access respin must not be refused"
        assert "REFUSED" not in result
        assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"

    def test_codex_retry_preserves_research_target_grant(self, tmp_path):
        """spool_retry re-launches a codex spool through _codex_spin_sync and must
        thread the stored research_target + permission — otherwise a retried research
        spool loses its --add-dir output grant and runs plain workspace-write with no
        way to write its output. (The sandbox tier already survives via stored sandbox.)"""
        _retry = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
        output_dir = tmp_path / "research-out"
        output_dir.mkdir()
        stored = {
            "id": "codex-research-stored",
            "status": "error",
            "prompt": "research the thing",
            "harness": "codex",
            "permission": "research",
            "research_target": f"dir:{output_dir}",
            "sandbox": "workspace-write",
            "working_dir": str(tmp_path),
            "model": "gpt-5.6-sol",
            "tags": ["codex"],
        }
        captured_cmd = []

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            return 99999

        with patch("spindle._count_running", return_value=0):
            _write_spool("codex-research-stored", stored)
            with patch("spindle._resolve_codex_binary", return_value="/fake/bin/codex"):
                with patch("spindle._codex_cli_version", return_value="0.125.0"):
                    with patch("spindle._codex_sandbox_enforces", return_value=True):
                        with patch("spindle._spawn_detached", side_effect=fake_detached):
                            result = asyncio.run(_retry("codex-research-stored"))

        assert not result.startswith("Error"), result
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        add_dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
        assert str(output_dir) in add_dirs, (
            f"retried codex research spool must keep its --add-dir {output_dir} grant; got {add_dirs!r}"
        )
        # The new spool record carries the research target + permission forward.
        retry_spool = _read_spool(result)
        assert retry_spool["research_target"] == f"dir:{output_dir}"
        assert retry_spool["permission"] == "research"


class TestCodexSandboxEnforcesProbe:
    """The behavioral sandbox probe (_codex_sandbox_enforces) and the fail-closed refusal.

    Enforcement is decided by running codex's no-model `codex sandbox` under read-only and
    checking a cwd write was BLOCKED — not by a version string. The probe must fail closed on
    any inconclusive outcome, cache per binary/config context, and never refuse a
    danger-full-access tier.
    """

    def _proc(self, stdout, returncode=0):
        proc = MagicMock()
        proc.stdout = stdout
        proc.returncode = returncode
        return proc

    @pytest.mark.parametrize(
        "stdout,returncode,expected",
        [
            ("Logged in using ChatGPT\n", 0, "chatgpt"),
            ("Logged in using an API key\n", 0, "api"),
            ("not logged in\n", 1, "unknown"),
        ],
    )
    def test_codex_auth_mode_is_detected_fresh_in_launch_environment(self, stdout, returncode, expected):
        process_env = {"PATH": "/custom/bin", "CODEX_HOME": "/custom/codex-home"}
        with patch("spindle.subprocess.run", return_value=self._proc(stdout, returncode)) as run:
            assert spindle._codex_auth_mode("/fake/codex", process_env) == expected
            assert spindle._codex_auth_mode("/fake/codex", process_env) == expected
        assert run.call_count == 2
        run.assert_called_with(
            ["/fake/codex", "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env,
        )

    def test_codex_auth_mode_prefers_per_run_api_key_over_saved_chatgpt_login(self):
        process_env = {
            "PATH": "/custom/bin",
            "CODEX_HOME": "/custom/codex-home",
            "CODEX_API_KEY": "per-run-key",
        }
        with patch("spindle.subprocess.run") as run:
            assert spindle._codex_auth_mode("/fake/codex", process_env) == "api"
        run.assert_not_called()

    def test_probe_true_when_write_blocked_and_command_ran(self, caplog):
        """Marker on stdout proves the command ran; a missing file means the write was blocked."""
        with patch("spindle.subprocess.run", return_value=self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")):
            assert spindle._codex_sandbox_probe("/fake/codex") is True
        assert "could not stat target" not in caplog.text

    def test_probe_fails_closed_on_unexpected_stat_error(self, caplog):
        """Only a missing target proves the write was blocked; other stat errors are inconclusive."""
        with patch("spindle.subprocess.run", return_value=self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")):
            with patch("spindle.os.stat", side_effect=PermissionError("denied")):
                assert spindle._codex_sandbox_probe("/fake/codex") is False
        assert "could not stat target" in caplog.text

    def test_probe_false_when_write_succeeded(self):
        """Fail open: the command ran, but its write to cwd landed — the sandbox did not block."""

        def run(cmd, **kwargs):
            with open(os.path.join(kwargs["cwd"], "enforce_probe.txt"), "w") as fh:
                fh.write("BROKEN")
            return self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")

        with patch("spindle.subprocess.run", side_effect=run):
            assert spindle._codex_sandbox_probe("/fake/codex") is False

    def test_probe_false_when_zero_byte_file_was_created(self):
        """Any created target violates read-only, even if it has no content."""

        def run(cmd, **kwargs):
            Path(kwargs["cwd"], "enforce_probe.txt").touch()
            return self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n", returncode=1)

        with patch("spindle.subprocess.run", side_effect=run):
            assert spindle._codex_sandbox_probe("/fake/codex") is False

    def test_probe_false_when_command_never_ran(self):
        """No marker: the missing file is not evidence of a boundary — treat as not enforcing."""
        with patch("spindle.subprocess.run", return_value=self._proc("codex: some error\n", returncode=1)):
            assert spindle._codex_sandbox_probe("/fake/codex") is False

    def test_probe_false_on_exception(self):
        """A timeout or any error during the probe fails closed."""
        with patch("spindle.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)):
            assert spindle._codex_sandbox_probe("/fake/codex") is False

    def test_probe_falls_through_to_next_cli_shape(self):
        """When the first CLI shape does not run (wrong for this version), a later shape decides."""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                # First shape: codex rejects it, the command never runs (no marker).
                return self._proc("error: unrecognized subcommand\n", returncode=2)
            # Second shape runs and the sandbox blocks the write (marker, no file).
            return self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")

        with patch("spindle.subprocess.run", side_effect=run):
            assert spindle._codex_sandbox_probe("/fake/codex") is True
        assert len(calls) == 2, "must try the next CLI shape when the first does not run"

    def test_probe_fail_open_does_not_fall_through(self):
        """A shape that runs and writes is authoritative fail-open — do not try another shape."""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            with open(os.path.join(kwargs["cwd"], "enforce_probe.txt"), "w") as fh:
                fh.write("BROKEN")
            return self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")

        with patch("spindle.subprocess.run", side_effect=run):
            assert spindle._codex_sandbox_probe("/fake/codex") is False
        assert len(calls) == 1, "a definitive fail-open reading must not fall through"

    def test_enforces_false_for_missing_binary(self):
        assert spindle._codex_sandbox_enforces(None) is False

    def test_enforces_is_cached_per_binary(self):
        """The probe runs once for an unchanged binary/config context."""
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()
        calls = []

        def probe(codex_bin, process_env=None):
            calls.append(codex_bin)
            return True

        with patch("spindle._codex_sandbox_probe", side_effect=probe):
            with patch("spindle._codex_sandbox_probe_key", return_value=("/fake/codex", "0.144.4", 123.0)):
                assert spindle._codex_sandbox_enforces("/fake/codex") is True
                assert spindle._codex_sandbox_enforces("/fake/codex") is True
        assert len(calls) == 1, f"probe must be reused for an unchanged context, ran {len(calls)}x"
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()

    def test_enforces_reprobes_when_binary_changes(self):
        """A changed mtime (reinstall/upgrade) invalidates the cache and re-probes."""
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()
        calls = []
        keys = iter([("/fake/codex", "0.144.4", 1.0), ("/fake/codex", "0.144.4", 2.0)])

        def probe(codex_bin, process_env=None):
            calls.append(codex_bin)
            return True

        with patch("spindle._codex_sandbox_probe", side_effect=probe):
            with patch("spindle._codex_sandbox_probe_key", side_effect=lambda b, env=None: next(keys)):
                spindle._codex_sandbox_enforces("/fake/codex")
                spindle._codex_sandbox_enforces("/fake/codex")
        assert len(calls) == 2, "a changed binary must re-probe"
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()

    def test_enforces_reprobes_when_codex_home_changes(self):
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()
        calls = []

        def probe(codex_bin, process_env=None):
            calls.append(process_env["CODEX_HOME"])
            return True

        def key(codex_bin, process_env=None):
            return (codex_bin, process_env["CODEX_HOME"])

        with patch("spindle._codex_sandbox_probe", side_effect=probe):
            with patch("spindle._codex_sandbox_probe_key", side_effect=key):
                assert spindle._codex_sandbox_enforces("/fake/codex", {"CODEX_HOME": "/one"})
                assert spindle._codex_sandbox_enforces("/fake/codex", {"CODEX_HOME": "/two"})

        assert calls == ["/one", "/two"]
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()

    def test_refusal_none_for_danger_full_access_without_probing(self):
        """danger-full-access expects no sandbox: never probe, never refuse."""
        with patch("spindle._codex_sandbox_enforces", side_effect=AssertionError("must not probe")):
            assert spindle._codex_sandbox_refusal("danger-full-access", "full", "/fake/codex", "0.144.4") is None

    @pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
    def test_refusal_none_when_enforcing(self, sandbox):
        with patch("spindle._codex_sandbox_enforces", return_value=True):
            assert spindle._codex_sandbox_refusal(sandbox, "readonly", "/fake/codex", "0.144.4") is None

    @pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
    def test_refusal_message_when_not_enforcing(self, sandbox):
        with patch("spindle._codex_sandbox_enforces", return_value=False):
            msg = spindle._codex_sandbox_refusal(sandbox, "readonly", "/fake/codex", "0.144.4")
        assert msg is not None
        assert "REFUSED" in msg
        assert sandbox in msg
        assert "/fake/codex" in msg
        assert "0.144.4" in msg

    @pytest.mark.skipif(spindle._resolve_codex_binary() is None, reason="codex not on PATH")
    def test_real_codex_binary_enforces(self):
        """Healthy path on this box: a real codex actually enforces, so it is never false-refused."""
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()
        assert spindle._codex_sandbox_enforces(spindle._resolve_codex_binary()) is True
        spindle._CODEX_SANDBOX_ENFORCES_CACHE.clear()


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

    def test_monitor_spool_terminates_live_group_after_leader_exits(self, tmp_path):
        spool_id = "test-timeout-orphan-group"
        spool = {
            "id": spool_id,
            "status": "running",
            "pid": 424242,
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            with patch("spindle._is_pid_alive", return_value=False):
                with patch("spindle._is_process_group_alive", return_value=True):
                    with patch("spindle._terminate_process_group", return_value=True) as terminate:
                        _monitor_spool(spool_id)
            result = _read_spool(spool_id)

        terminate.assert_called_once_with(424242, 0.5)
        assert result["status"] == "timeout"

    def test_monitor_spool_stays_running_while_timeout_group_survives(self, tmp_path):
        spool_id = "test-timeout-stubborn-group"
        spool = {
            "id": spool_id,
            "status": "running",
            "pid": 434343,
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            with patch("spindle._is_pid_alive", return_value=False):
                with patch("spindle._is_process_group_alive", return_value=True):
                    with patch("spindle._terminate_process_group", return_value=False):
                        with patch("spindle.time.sleep", side_effect=RuntimeError("stop monitor")):
                            with pytest.raises(RuntimeError, match="stop monitor"):
                                _monitor_spool(spool_id)
            result = _read_spool(spool_id)

        assert result["status"] == "running"
        assert result["error"] == "Timeout reached; process-group termination still pending"

    def test_monitor_spool_does_not_overwrite_terminal_spool_as_timeout(self, tmp_path):
        spool_id = "test-terminal-spool-past-timeout"
        completed_at = datetime.now().isoformat()
        spool = {
            "id": spool_id,
            "status": "complete",
            "result": "done",
            "pid": 999999999,
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
            "completed_at": completed_at,
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            _monitor_spool(spool_id)
            result = _read_spool(spool_id)

        assert result["status"] == "complete"
        assert result["result"] == "done"
        assert result["completed_at"] == completed_at
        assert "error" not in result


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
        (sess_dir / "1.json").write_text(
            json.dumps({"id": "1", "subject": "pgrep loop", "status": "running", "activeForm": "waiting for pytest"})
        )

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
            f"claude resume must use the resolved session_id {session_id!r}, not the spool_id {spool_id!r}; got {cmd!r}"
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
            with (
                patch("spindle._codex_respin_sync") as codex_resume,
                patch("spindle._gemini_respin_sync") as gemini_resume,
                patch("spindle._kimi_respin_sync") as kimi_resume,
                patch("spindle._spawn_detached") as spawn,
            ):
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
        assert default is None, f"_spawn_shard base_branch default must be None, got {default!r}"

    def test_spin_sync_default_param_is_none(self):
        sig = inspect.signature(_spin_sync)
        default = sig.parameters["base_branch"].default
        assert default is None, f"_spin_sync base_branch default must be None, got {default!r}"

    def test_codex_spin_sync_default_param_is_none(self):
        sig = inspect.signature(_codex_spin_sync)
        default = sig.parameters["base_branch"].default
        assert default is None, f"_codex_spin_sync base_branch default must be None, got {default!r}"

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
            shard_info, shard_error = _spawn_shard("test-agent", str(repo), base_branch="nonexistent-branch-abc")

        assert shard_info is None
        assert shard_error is not None
        assert "nonexistent-branch-abc" in shard_error
        assert "--base-branch" in shard_error


class TestCLIArgparse:
    """Test CLI argument parsing for permission profiles."""

    def test_permission_auto_accepted_by_argparse(self, capsys):
        """'auto' must be a valid --permission choice (argparse must not reject it)."""
        with patch("sys.argv", ["spindle", "spin", "--permission", "auto", "test prompt"]):
            with patch("spindle._spin_sync", return_value="spool-abc123"):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code != 2, "argparse rejected 'auto' as invalid --permission choice"

    def test_permission_auto_plus_shard_accepted_by_argparse(self, capsys):
        """'auto+shard' must be a valid --permission choice (argparse must not reject it)."""
        with patch("sys.argv", ["spindle", "spin", "--permission", "auto+shard", "test prompt"]):
            with patch("spindle._spin_sync", return_value="spool-abc123"):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code != 2, "argparse rejected 'auto+shard' as invalid --permission choice"

    def test_auto_permission_on_codex_cli_exits_nonzero(self, capsys):
        """CLI: --permission auto --harness codex must exit 1 with an error message."""
        with patch("sys.argv", ["spindle", "spin", "--permission", "auto", "--harness", "codex", "test"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_auto_permission_on_codex_cli_prints_error(self, capsys):
        """CLI: --permission auto --harness codex must print an error mentioning codex and claude-code."""
        with patch("sys.argv", ["spindle", "spin", "--permission", "auto", "--harness", "codex", "test"]):
            with pytest.raises(SystemExit):
                main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "error" in parsed
        assert "codex" in parsed["error"]
        assert "claude-code" in parsed["error"]

    def test_auto_permission_on_codex_cli_does_not_spawn(self, capsys):
        """CLI: --permission auto --harness codex must not invoke _codex_spin_sync."""
        with patch("sys.argv", ["spindle", "spin", "--permission", "auto", "--harness", "codex", "test"]):
            with patch("spindle._codex_spin_sync") as mock_codex:
                with pytest.raises(SystemExit):
                    main()
        mock_codex.assert_not_called()


class TestShardWritableBinds:
    """Tests for SPINDLE_SHARD_WRITABLE_BINDS extra bind mounts in the bwrap sandbox."""

    def _run_shard_spin(self, tmp_path, monkeypatch, extra_env=None):
        """Spin a shard and capture the bwrap command. Returns the captured cmd list."""
        captured_cmd = []
        shard_info = {"worktree_path": str(tmp_path), "shard_id": "shard-test"}

        def fake_detached(spool_id, cmd, cwd, env=None):
            captured_cmd.append(list(cmd))
            raise OSError("stop after capture")

        if extra_env:
            for k, v in extra_env.items():
                monkeypatch.setenv(k, v)

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._detect_existing_shard", return_value=shard_info):
                    with patch("spindle._has_skein", return_value=True):
                        with patch("shutil.which", return_value="/usr/bin/bwrap"):
                            with patch("spindle._spawn_detached", side_effect=fake_detached):
                                _spin_sync(
                                    prompt="test task",
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

        assert len(captured_cmd) == 1
        return captured_cmd[0]

    def test_existing_dir_is_bound_writable(self, tmp_path, monkeypatch):
        """A valid absolute existing path in SPINDLE_SHARD_WRITABLE_BINDS gets --bind <p> <p>."""
        target = tmp_path / "output"
        target.mkdir()
        cmd = self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": str(target)},
        )
        assert cmd[0] == "bwrap"
        found = any(
            cmd[i] == "--bind" and cmd[i + 1] == str(target) and cmd[i + 2] == str(target) for i in range(len(cmd) - 2)
        )
        assert found, f"Expected --bind {target} {target} in bwrap cmd: {cmd!r}"

    def test_multiple_paths_all_bound(self, tmp_path, monkeypatch):
        """Multiple colon-separated existing paths are each bound read-write."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        cmd = self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": f"{dir_a}:{dir_b}"},
        )
        for d in (dir_a, dir_b):
            found = any(
                cmd[i] == "--bind" and cmd[i + 1] == str(d) and cmd[i + 2] == str(d) for i in range(len(cmd) - 2)
            )
            assert found, f"Expected --bind {d} {d} in bwrap cmd: {cmd!r}"

    def test_nonexistent_path_is_skipped(self, tmp_path, monkeypatch):
        """A non-existent path in SPINDLE_SHARD_WRITABLE_BINDS is silently skipped (no crash)."""
        ghost = str(tmp_path / "does-not-exist")
        cmd = self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": ghost},
        )
        assert cmd[0] == "bwrap"
        assert ghost not in cmd

    def test_relative_path_is_skipped(self, tmp_path, monkeypatch):
        """A relative path in SPINDLE_SHARD_WRITABLE_BINDS is silently skipped (no crash)."""
        cmd = self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": "relative/path"},
        )
        assert cmd[0] == "bwrap"
        assert "relative/path" not in cmd

    def test_env_unset_no_extra_binds(self, tmp_path, monkeypatch):
        """With SPINDLE_SHARD_WRITABLE_BINDS unset, bwrap wraps normally."""
        monkeypatch.delenv("SPINDLE_SHARD_WRITABLE_BINDS", raising=False)
        cmd = self._run_shard_spin(tmp_path, monkeypatch)
        assert cmd[0] == "bwrap"

    def test_nonexistent_path_warns_to_stderr(self, tmp_path, monkeypatch, capsys):
        """A non-existent path generates a warning on stderr."""
        ghost = str(tmp_path / "does-not-exist")
        self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": ghost},
        )
        err = capsys.readouterr().err
        assert "SPINDLE_SHARD_WRITABLE_BINDS" in err
        assert ghost in err

    def test_relative_path_warns_to_stderr(self, tmp_path, monkeypatch, capsys):
        """A relative path generates a warning on stderr."""
        self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": "relative/path"},
        )
        err = capsys.readouterr().err
        assert "SPINDLE_SHARD_WRITABLE_BINDS" in err
        assert "relative/path" in err

    def test_mixed_valid_invalid_only_valid_bound(self, tmp_path, monkeypatch):
        """With a mix of valid and invalid paths, only valid ones get --bind entries."""
        valid_dir = tmp_path / "valid"
        valid_dir.mkdir()
        ghost = str(tmp_path / "ghost")
        cmd = self._run_shard_spin(
            tmp_path,
            monkeypatch,
            extra_env={"SPINDLE_SHARD_WRITABLE_BINDS": f"{valid_dir}:{ghost}:relative/bad"},
        )
        found_valid = any(
            cmd[i] == "--bind" and cmd[i + 1] == str(valid_dir) and cmd[i + 2] == str(valid_dir)
            for i in range(len(cmd) - 2)
        )
        assert found_valid, f"Expected valid dir bound in: {cmd!r}"
        assert ghost not in cmd
        assert "relative/bad" not in cmd


def _make_profile(root, name, config):
    """Create <root>/<name>/profile.json with the given config dict."""
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.json").write_text(json.dumps(config))
    return pdir


class TestProfiles:
    """Tests for the lodged-profile feature (base harness + overrides)."""

    @pytest.fixture
    def profiles_root(self, tmp_path, monkeypatch):
        """Isolate the canonical profiles root to a per-test tmp dir.

        Also chdir into tmp_path so the cwd-relative ``./profiles`` scan in
        _profile_roots() resolves to the same path as SPINDLE_PROFILES_DIR and
        is deduped away. Without this, a test could read a developer's real
        (gitignored) ./profiles/<name>/ and become nondeterministic.
        """
        root = tmp_path / "profiles"
        root.mkdir()
        monkeypatch.setattr(spindle, "SPINDLE_PROFILES_DIR", root)
        monkeypatch.chdir(tmp_path)
        return root

    # --- discovery ---------------------------------------------------------

    def test_discovery_and_load(self, profiles_root):
        _make_profile(profiles_root, "alt", {"description": "an alt endpoint", "model": "m1"})
        profiles = _discover_profiles()
        assert "alt" in profiles
        assert profiles["alt"]["description"] == "an alt endpoint"
        assert profiles["alt"]["_name"] == "alt"
        assert profiles["alt"]["_source"].endswith("alt/profile.json")
        assert _load_profile("alt") is not None
        assert _load_profile("nonexistent") is None

    def test_malformed_profile_skipped(self, profiles_root):
        _make_profile(profiles_root, "good", {"model": "ok"})
        bad = profiles_root / "bad"
        bad.mkdir()
        (bad / "profile.json").write_text("{ this is not json")
        # A non-object profile.json is also skipped.
        arr = profiles_root / "arr"
        arr.mkdir()
        (arr / "profile.json").write_text("[1, 2, 3]")

        profiles = _discover_profiles()
        assert "good" in profiles
        assert "bad" not in profiles
        assert "arr" not in profiles

    def test_profile_appears_in_harnesses(self, profiles_root):
        _make_profile(
            profiles_root,
            "alt",
            {"description": "alt endpoint", "harness": "claude-code", "model": "big"},
        )
        harnesses = _get_harnesses()
        # Built-ins still present.
        assert {"claude-code", "codex", "gemini", "kimi"} <= set(harnesses.keys())
        assert "alt" in harnesses
        entry = harnesses["alt"]
        assert entry["type"] == "profile"
        assert entry["base_harness"] == "claude-code"
        assert entry["default_model"] == "big"
        assert entry["description"] == "alt endpoint"
        assert entry["source"].endswith("alt/profile.json")

    # --- value resolution --------------------------------------------------

    def test_resolve_env_var_set(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret-123")
        assert _resolve_profile_value("${MY_TOKEN}", "p") == "secret-123"
        assert _resolve_profile_value("prefix-${MY_TOKEN}-suffix", "p") == "prefix-secret-123-suffix"

    def test_resolve_env_var_unset_left_literal(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
        assert _resolve_profile_value("${DEFINITELY_UNSET_VAR}", "p") == "${DEFINITELY_UNSET_VAR}"

    def test_resolve_non_string_passthrough(self):
        assert _resolve_profile_value(42, "p") == 42
        assert _resolve_profile_value(None, "p") is None

    def test_op_inject_resolved_with_mock(self, monkeypatch):
        # strongbox on PATH, returns the resolved secret on stdout.
        monkeypatch.setattr(spindle.shutil, "which", lambda tool: "/usr/bin/strongbox" if tool == "strongbox" else None)

        calls = {}

        def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
            calls["cmd"] = cmd
            calls["input"] = input
            return MagicMock(returncode=0, stdout="resolved-secret", stderr="")

        monkeypatch.setattr(spindle.subprocess, "run", fake_run)
        out = _resolve_profile_value("op://Private/key/field", "p")
        assert out == "resolved-secret"
        assert calls["cmd"] == ["strongbox", "inject"]
        assert calls["input"] == "op://Private/key/field"

    def test_op_inject_falls_back_to_op(self, monkeypatch):
        monkeypatch.setattr(spindle.shutil, "which", lambda tool: "/usr/bin/op" if tool == "op" else None)

        def fake_run(cmd, input=None, **kwargs):
            return MagicMock(returncode=0, stdout="from-op", stderr="")

        monkeypatch.setattr(spindle.subprocess, "run", fake_run)
        assert _op_inject("op://x/y/z", "p") == "from-op"

    def test_op_inject_no_tool_left_literal(self, monkeypatch):
        monkeypatch.setattr(spindle.shutil, "which", lambda tool: None)
        val = "op://Private/key/field"
        assert _resolve_profile_value(val, "p") == val

    # --- override assembly -------------------------------------------------

    def test_overrides_assemble_env(self, profiles_root, monkeypatch):
        monkeypatch.setenv("ALT_KEY", "key-abc")
        _make_profile(
            profiles_root,
            "alt",
            {
                "harness": "claude-code",
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "env": {"EXTRA_TAG": "tag-1"},
                "extra_args": ["--verbose"],
                "model": "big",
            },
        )
        ov = _resolve_profile_overrides(_load_profile("alt"))
        assert ov["base_harness"] == "claude-code"
        assert ov["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        assert ov["env"]["ANTHROPIC_API_KEY"] == "key-abc"
        assert ov["env"]["EXTRA_TAG"] == "tag-1"
        # base_url with no config_dir => isolated default, created under the root.
        cfg = ov["env"]["CLAUDE_CONFIG_DIR"]
        assert cfg == str(profiles_root / "alt" / "claude-config")
        assert Path(cfg).is_dir()
        assert ov["extra_args"] == ["--verbose"]
        assert ov["model"] == "big"

    def test_same_endpoint_profile_no_config_dir(self, profiles_root):
        _make_profile(profiles_root, "flagsonly", {"extra_args": ["--verbose"]})
        ov = _resolve_profile_overrides(_load_profile("flagsonly"))
        assert "CLAUDE_CONFIG_DIR" not in ov["env"]
        assert "ANTHROPIC_BASE_URL" not in ov["env"]

    def test_base_url_non_claude_raises(self, profiles_root):
        _make_profile(profiles_root, "weird", {"harness": "codex", "base_url": "https://x"})
        with pytest.raises(ValueError) as exc:
            _resolve_profile_overrides(_load_profile("weird"))
        assert "base_url" in str(exc.value)
        assert "claude-code" in str(exc.value)

    def test_non_claude_base_raises(self, profiles_root):
        _make_profile(profiles_root, "cdx", {"harness": "codex", "model": "5.5"})
        with pytest.raises(ValueError) as exc:
            _resolve_profile_overrides(_load_profile("cdx"))
        assert "codex" in str(exc.value)

    # --- spin() wiring -----------------------------------------------------

    def _spin(self):
        return spin.fn if hasattr(spin, "fn") else spin

    def test_spin_injects_env_to_spawn(self, profiles_root, tmp_path, monkeypatch):
        monkeypatch.setenv("ALT_KEY", "key-xyz")
        _make_profile(
            profiles_root,
            "alt",
            {
                "harness": "claude-code",
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "env": {"EXTRA_TAG": "tag-2"},
                "model": "big",
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 4321

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                spool_id = asyncio.run(self._spin()("do a thing", harness="alt", working_dir=str(tmp_path)))

        env = captured["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        assert env["ANTHROPIC_API_KEY"] == "key-xyz"
        assert env["CLAUDE_CONFIG_DIR"] == str(profiles_root / "alt" / "claude-config")
        assert env["EXTRA_TAG"] == "tag-2"
        # Default model from the profile flows to --model.
        assert "--model" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "big"

        # Profile name persisted on the spool.
        spool = _read_spool(spool_id)
        assert spool["profile"] == "alt"
        assert spool["harness"] == "claude-code"

    def test_spin_caller_model_wins(self, profiles_root, tmp_path):
        _make_profile(profiles_root, "alt", {"model": "big"})
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                asyncio.run(self._spin()("x", harness="alt", model="opus", working_dir=str(tmp_path)))
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "opus"

    def test_spin_profile_model_alias(self, profiles_root, tmp_path):
        _make_profile(
            profiles_root,
            "alt",
            {"model": "big", "model_aliases": {"fast": "small-model"}},
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                asyncio.run(self._spin()("x", harness="alt", model="fast", working_dir=str(tmp_path)))
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "small-model"

    def test_spin_appends_extra_args(self, profiles_root, tmp_path):
        _make_profile(profiles_root, "alt", {"extra_args": ["--verbose"]})
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                asyncio.run(self._spin()("x", harness="alt", working_dir=str(tmp_path)))
        assert "--verbose" in captured["cmd"]

    def test_unknown_profile_errors(self, profiles_root, tmp_path):
        result = asyncio.run(self._spin()("x", harness="ghost-profile", working_dir=str(tmp_path)))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "ghost-profile" in parsed["error"]

    def test_base_url_non_claude_spin_errors(self, profiles_root, tmp_path):
        _make_profile(profiles_root, "weird", {"harness": "codex", "base_url": "https://x"})
        result = asyncio.run(self._spin()("x", harness="weird", working_dir=str(tmp_path)))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "base_url" in parsed["error"]

    def test_builtin_wins_collision(self, profiles_root, tmp_path, caplog):
        # A profile named like a built-in must not shadow it.
        _make_profile(
            profiles_root,
            "claude-code",
            {"base_url": "https://should-not-be-used.example.com/anthropic", "api_key": "nope"},
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            return 1

        import logging

        with caplog.at_level(logging.WARNING):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    asyncio.run(self._spin()("x", harness="claude-code", working_dir=str(tmp_path)))

        # Built-in path: no profile env injected.
        assert captured["env"] is None or "ANTHROPIC_BASE_URL" not in (captured["env"] or {})
        assert "shadows built-in" in caplog.text

    # --- respin re-injection ----------------------------------------------

    def test_respin_reinjects_env_and_args(self, profiles_root, monkeypatch):
        monkeypatch.setenv("ALT_KEY", "rotated-key")
        _make_profile(
            profiles_root,
            "alt",
            {
                "harness": "claude-code",
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "extra_args": ["--verbose"],
            },
        )
        # An original completed spool that used the profile. The persisted env is
        # the caller's explicit (non-secret) override only — secrets were never
        # written to disk.
        _write_spool(
            "orig1",
            {
                "id": "orig1",
                "status": "complete",
                "result": "done",
                "session_id": "sess-abc",
                "model": "big",
                "profile": "alt",
                "env": {"CALLER_TAG": "from-caller"},
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 9999

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                _respin_sync("sess-abc", "continue please")

        env = captured["env"]
        # Secret re-resolved fresh: rotated key is injected at spawn.
        assert env["ANTHROPIC_API_KEY"] == "rotated-key"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        # Caller's non-secret env override is reapplied on top of the profile env.
        assert env["CALLER_TAG"] == "from-caller"
        cmd = captured["cmd"]
        assert "--resume" in cmd and "sess-abc" in cmd
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "big"
        assert "--verbose" in cmd

    def test_respin_non_profile_unchanged(self, monkeypatch):
        # A normal (non-profile) spool resumes with stored env and no --model.
        _write_spool(
            "orig2",
            {
                "id": "orig2",
                "status": "complete",
                "result": "done",
                "session_id": "sess-def",
                "model": "opus",
                "profile": None,
                "env": {"SOME_VAR": "v"},
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                _respin_sync("sess-def", "more")

        assert captured["env"] == {"SOME_VAR": "v"}
        # Non-profile respin does not re-specify --model (legacy behavior).
        assert "--model" not in captured["cmd"]

    # --- secret handling ---------------------------------------------------

    def test_resolved_secret_never_persisted(self, profiles_root, tmp_path, monkeypatch):
        """A profile-resolved secret reaches the spawned child but is never
        written to the spool JSON, nor surfaced via spool_info / spool_export."""
        sentinel = "SENTINEL-SECRET-DO-NOT-PERSIST-9f3a2b"
        monkeypatch.setenv("ALT_KEY", sentinel)
        _make_profile(
            profiles_root,
            "alt",
            {
                "harness": "claude-code",
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "model": "big",
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            return 4321

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                spool_id = asyncio.run(self._spin()("do a thing", harness="alt", working_dir=str(tmp_path)))

        # The resolved secret IS passed to the spawned child.
        assert captured["env"]["ANTHROPIC_API_KEY"] == sentinel

        # The secret is NOT written to the on-disk spool JSON.
        on_disk = _get_spool_path(spool_id).read_text()
        assert sentinel not in on_disk

        # The persisted env carries no profile-resolved values at all.
        spool = _read_spool(spool_id)
        assert spool["profile"] == "alt"
        assert spool.get("env") in (None, {})

        # The secret is NOT surfaced via spool_info.
        info = asyncio.run(spindle.spool_info.fn(spool_id))
        assert sentinel not in info

        # The secret is NOT surfaced via spool_export(format="json").
        export_path = tmp_path / "export.json"
        asyncio.run(spindle.spool_export.fn(spool_id, format="json", output_path=str(export_path)))
        assert sentinel not in export_path.read_text()

    def test_caller_env_persisted_secret_resolved_separately(self, profiles_root, tmp_path, monkeypatch):
        """The caller's explicit non-secret env is persisted; the profile secret
        is overlaid only into the spawn env."""
        sentinel = "SENTINEL-CALLER-TEST-7c1d"
        monkeypatch.setenv("ALT_KEY", sentinel)
        _make_profile(
            profiles_root,
            "alt",
            {"base_url": "https://api.example.com/anthropic", "api_key": "${ALT_KEY}"},
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                spool_id = asyncio.run(
                    self._spin()(
                        "x",
                        harness="alt",
                        env={"CALLER_TAG": "from-caller"},
                        working_dir=str(tmp_path),
                    )
                )

        # Spawn env has both the resolved secret and the caller override.
        assert captured["env"]["ANTHROPIC_API_KEY"] == sentinel
        assert captured["env"]["CALLER_TAG"] == "from-caller"

        # Persisted env is the caller's explicit env only — no secret.
        spool = _read_spool(spool_id)
        assert spool["env"] == {"CALLER_TAG": "from-caller"}
        assert sentinel not in _get_spool_path(spool_id).read_text()

    # --- shared spawn-env helper ------------------------------------------

    def test_profile_spawn_env_resolves_overlays_and_degrades(self, profiles_root, monkeypatch):
        """The shared helper re-resolves a profile fresh, overlays caller env,
        and degrades to caller env alone when the profile is gone."""
        monkeypatch.setenv("ALT_KEY", "key-fresh")
        _make_profile(
            profiles_root,
            "alt",
            {
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "extra_args": ["--verbose"],
                "model": "big",
                "model_aliases": {"fast": "small-model"},
            },
        )

        # Valid profile: secrets resolved into spawn env, caller env overlaid,
        # profile default model + extra_args returned, resolved=True.
        spawn_env, model, extra_args, resolved = _profile_spawn_env("alt", {"CALLER": "c"})
        assert resolved is True
        assert spawn_env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        assert spawn_env["ANTHROPIC_API_KEY"] == "key-fresh"
        assert spawn_env["CALLER"] == "c"
        assert model == "big"
        assert extra_args == ["--verbose"]

        # A caller model matching a profile alias is mapped; an unrelated one passes through.
        _, mapped, _, _ = _profile_spawn_env("alt", None, model="fast")
        assert mapped == "small-model"
        _, passthru, _, _ = _profile_spawn_env("alt", None, model="opus")
        assert passthru == "opus"

        # Missing profile degrades to caller env alone, resolved=False — no alt endpoint.
        se, m, ea, r = _profile_spawn_env("ghost", {"CALLER": "c"}, model="opus")
        assert (se, m, ea, r) == ({"CALLER": "c"}, "opus", [], False)

        # No profile name is a no-op passthrough.
        assert _profile_spawn_env(None, {"X": "y"}, model="m") == ({"X": "y"}, "m", [], False)

    def test_profile_spawn_env_strict_raises_on_missing(self, profiles_root):
        """strict=True surfaces a missing/malformed profile as ValueError (spin path)."""
        with pytest.raises(ValueError):
            _profile_spawn_env("ghost", None, strict=True)
        _make_profile(profiles_root, "bad", {"env": "not-a-dict"})
        with pytest.raises(ValueError):
            _profile_spawn_env("bad", None, strict=True)

    def test_all_profile_spawn_paths_route_through_helper(self, profiles_root, tmp_path, monkeypatch):
        """spin, respin, retry, and the expired-session fallback all reconstruct
        the profile spawn env through the one shared helper."""
        monkeypatch.setenv("ALT_KEY", "k")
        _make_profile(
            profiles_root,
            "alt",
            {"base_url": "https://api.example.com/anthropic", "api_key": "${ALT_KEY}", "model": "big"},
        )

        def fake_spawn(spool_id, cmd, cwd, env=None):
            return 1

        # Distinct originals per path so _find_spool_by_session stays unambiguous.
        _write_spool(
            "route-respin",
            {
                "id": "route-respin",
                "status": "complete",
                "session_id": "sess-respin",
                "model": "big",
                "profile": "alt",
                "env": None,
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        _write_spool(
            "route-retry",
            {
                "id": "route-retry",
                "status": "error",
                "prompt": "do it",
                "permission": "careful",
                "model": "big",
                "profile": "alt",
                "env": None,
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "tags": [],
                "created_at": datetime.now().isoformat(),
            },
        )
        _write_spool(
            "route-exp-orig",
            {
                "id": "route-exp-orig",
                "status": "complete",
                "session_id": "sess-exp-route",
                "model": "big",
                "profile": "alt",
                "env": None,
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        exp_transcript = _get_transcript_path("route-exp-orig")
        exp_transcript.parent.mkdir(parents=True, exist_ok=True)
        exp_transcript.write_text("prior transcript")
        failing = {
            "id": "route-exp-fail",
            "status": "running",
            "session_id": "sess-exp-route",
            "prompt": "Continue sess-exp-route: keep going",
            "profile": "alt",
            "env": None,
            "working_dir": str(tmp_path),
            "pid": None,
        }

        wrapped = MagicMock(side_effect=_profile_spawn_env)
        with patch("spindle._profile_spawn_env", wrapped):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    asyncio.run(self._spin()("x", harness="alt", working_dir=str(tmp_path)))  # 1. spin
                    _respin_sync("sess-respin", "again")  # 2. respin
                    asyncio.run(spool_retry.fn("route-retry"))  # 3. retry
                    assert _handle_expired_session("route-exp-fail", failing) is True  # 4. expired

        called_profiles = [c.args[0] for c in wrapped.call_args_list]
        # Every path asked the helper to reconstruct the "alt" profile spawn env.
        assert called_profiles.count("alt") == 4

    # --- retry / expired-session profile re-resolution --------------------

    def test_retry_reresolves_profile_secret_not_persisted(self, profiles_root, tmp_path, monkeypatch):
        """Retrying a profile spool re-resolves the alt endpoint/key/model/extra_args
        and applies the caller overlay, while the secret never hits disk."""
        sentinel = "SENTINEL-RETRY-3e9a"
        monkeypatch.setenv("ALT_KEY", sentinel)
        _make_profile(
            profiles_root,
            "alt",
            {
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "extra_args": ["--verbose"],
                "model": "big",
            },
        )
        # A failed profile spool: persisted env is caller-only (no secret).
        _write_spool(
            "retry-orig",
            {
                "id": "retry-orig",
                "status": "error",
                "prompt": "do the thing",
                "permission": "careful",
                "model": "big",
                "profile": "alt",
                "env": {"CALLER_TAG": "from-caller"},
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "tags": [],
                "created_at": datetime.now().isoformat(),
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 4321

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                new_id = asyncio.run(spool_retry.fn("retry-orig"))

        # The retried spawn re-resolves the alt endpoint, key, model, and extra_args.
        env = captured["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        assert env["ANTHROPIC_API_KEY"] == sentinel
        assert env["CALLER_TAG"] == "from-caller"
        cmd = captured["cmd"]
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "big"
        assert "--verbose" in cmd

        # The re-resolved secret is never persisted on the retried spool.
        assert sentinel not in _get_spool_path(new_id).read_text()
        new_spool = _read_spool(new_id)
        assert new_spool["profile"] == "alt"
        assert new_spool["env"] == {"CALLER_TAG": "from-caller"}
        info = asyncio.run(spindle.spool_info.fn(new_id))
        assert sentinel not in info
        export_path = tmp_path / "retry-export.json"
        asyncio.run(spindle.spool_export.fn(new_id, format="json", output_path=str(export_path)))
        assert sentinel not in export_path.read_text()

    def test_retry_non_profile_uses_stored_env(self, tmp_path):
        """A non-profile retry spawns with the stored caller env and no profile injection."""
        _write_spool(
            "retry-plain",
            {
                "id": "retry-plain",
                "status": "error",
                "prompt": "plain task",
                "permission": "careful",
                "model": "opus",
                "profile": None,
                "env": {"SOME_VAR": "v"},
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "tags": [],
                "created_at": datetime.now().isoformat(),
            },
        )
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            with patch("spindle._count_running", return_value=0):
                asyncio.run(spool_retry.fn("retry-plain"))

        assert captured["env"] == {"SOME_VAR": "v"}
        assert "ANTHROPIC_BASE_URL" not in captured["env"]

    def test_expired_session_reresolves_profile_secret_not_persisted(self, profiles_root, tmp_path, monkeypatch):
        """The transcript-fallback respin of a profile spool rebuilds the alt
        endpoint/key/model rather than running against the default endpoint."""
        sentinel = "SENTINEL-EXPIRED-b71c"
        monkeypatch.setenv("ALT_KEY", sentinel)
        _make_profile(
            profiles_root,
            "alt",
            {
                "base_url": "https://api.example.com/anthropic",
                "api_key": "${ALT_KEY}",
                "extra_args": ["--verbose"],
                "model": "big",
            },
        )
        # The original (resumable) spool carries the effective model + profile.
        _write_spool(
            "exp-orig",
            {
                "id": "exp-orig",
                "status": "complete",
                "session_id": "sess-exp",
                "model": "big",
                "profile": "alt",
                "env": {"CALLER_TAG": "from-caller"},
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        transcript = _get_transcript_path("exp-orig")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior conversation")

        # The failing respin spool (session expired) carries the caller env + profile.
        failing = {
            "id": "exp-fail",
            "status": "running",
            "session_id": "sess-exp",
            "prompt": "Continue sess-exp: keep going",
            "profile": "alt",
            "env": {"CALLER_TAG": "from-caller"},
            "working_dir": str(tmp_path),
            "pid": None,
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 7777

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            assert _handle_expired_session("exp-fail", failing) is True

        # The fallback spawn hits the alt endpoint with the re-resolved key.
        env = captured["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
        assert env["ANTHROPIC_API_KEY"] == sentinel
        assert env["CALLER_TAG"] == "from-caller"
        cmd = captured["cmd"]
        # Re-injected recorded model + extra_args; no --resume on a transcript fallback.
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "big"
        assert "--verbose" in cmd
        assert "--resume" not in cmd

        # The fallback never persists the secret on the spool record.
        assert sentinel not in _get_spool_path("exp-fail").read_text()

    def test_expired_session_non_profile_unchanged(self, tmp_path):
        """A non-profile expired-session fallback keeps the stored env and adds no --model."""
        _write_spool(
            "exp-orig2",
            {
                "id": "exp-orig2",
                "status": "complete",
                "session_id": "sess-exp2",
                "model": "opus",
                "profile": None,
                "env": {"SOME_VAR": "v"},
                "working_dir": str(tmp_path),
                "harness": "claude-code",
                "created_at": datetime.now().isoformat(),
            },
        )
        transcript = _get_transcript_path("exp-orig2")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("prior")
        failing = {
            "id": "exp-fail2",
            "status": "running",
            "session_id": "sess-exp2",
            "prompt": "Continue sess-exp2: go",
            "profile": None,
            "env": {"SOME_VAR": "v"},
            "working_dir": str(tmp_path),
            "pid": None,
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["env"] = env
            captured["cmd"] = cmd
            return 1

        with patch("spindle._spawn_detached", side_effect=fake_spawn):
            assert _handle_expired_session("exp-fail2", failing) is True

        assert captured["env"] == {"SOME_VAR": "v"}
        assert "--model" not in captured["cmd"]

    # --- malformed profile fields -----------------------------------------

    def test_malformed_env_type_clean_spin_error(self, profiles_root, tmp_path):
        """A non-object env produces a clean spin error, not an uncaught crash."""
        _make_profile(profiles_root, "bad", {"env": "not-a-dict"})
        result = asyncio.run(self._spin()("x", harness="bad", working_dir=str(tmp_path)))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "env" in parsed["error"]
        assert "bad" in parsed["error"]

    def test_malformed_extra_args_type_clean_spin_error(self, profiles_root, tmp_path):
        """A non-list extra_args produces a clean spin error, not a crash."""
        _make_profile(profiles_root, "bad", {"extra_args": 5})
        result = asyncio.run(self._spin()("x", harness="bad", working_dir=str(tmp_path)))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "extra_args" in parsed["error"]

    def test_malformed_extra_args_non_string_items_clean_error(self, profiles_root, tmp_path):
        """extra_args with non-string items also produces a clean spin error."""
        _make_profile(profiles_root, "bad", {"extra_args": ["--ok", 7]})
        result = asyncio.run(self._spin()("x", harness="bad", working_dir=str(tmp_path)))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "extra_args" in parsed["error"]

    def test_malformed_field_types_raise_value_error(self, profiles_root):
        """_resolve_profile_overrides raises ValueError on bad field types."""
        _make_profile(profiles_root, "p1", {"env": "x"})
        with pytest.raises(ValueError):
            _resolve_profile_overrides(_load_profile("p1"))
        _make_profile(profiles_root, "p2", {"extra_args": 5})
        with pytest.raises(ValueError):
            _resolve_profile_overrides(_load_profile("p2"))
        _make_profile(profiles_root, "p3", {"model": 42})
        with pytest.raises(ValueError):
            _resolve_profile_overrides(_load_profile("p3"))
