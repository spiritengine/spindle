"""Tests for Spindle MCP server."""

import asyncio
import contextlib
import inspect
import json
import logging
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
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
    _is_pid_alive,
    _is_review_tag,
    _kimi_bwrap_wrap,
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
    _recovery_pass,
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


def _spin_claude_cmd(tmp_path, permission, *, shard_info=None, model=None):
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
            model=model,
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


def test_claude_opus_5_alias_reaches_cli(tmp_path):
    cmd = _spin_claude_cmd(tmp_path, "careful", model="opus-5")
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5"


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

    def test_claude_shard_respin_reuses_worktree_and_outer_wrapper(self, tmp_path):
        spool_id = "claude-shard-respin"
        worktree = tmp_path / "worktrees" / "claude-shard-respin"
        worktree.mkdir(parents=True)
        shard = {
            "worktree_path": str(worktree),
            "branch_name": "shard-claude-shard-respin",
            "shard_id": "claude-shard-respin",
        }
        wrapped = []
        spawned = []

        def wrap(cmd, shard_info, cwd, **kwargs):
            wrapped.append((list(cmd), shard_info, cwd))
            return ["wrapped-claude"]

        def spawn(spawn_id, cmd, cwd, env=None):
            spawned.append((spawn_id, list(cmd), cwd))
            return 787878

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "session_id": "claude-shard-session",
                    "harness": "claude-code",
                    "permission": "careful+shard",
                    "allowed_tools": None,
                    "working_dir": str(worktree),
                    "shard": shard,
                },
            )
            with patch("spindle._codex_bwrap_wrap", side_effect=wrap):
                with patch("spindle._spawn_detached", side_effect=spawn):
                    with patch("spindle._start_spool_monitor"):
                        result = _respin_sync(spool_id, "continue in shard")

        assert not result.startswith("Error"), result
        assert wrapped[0][1:] == (shard, str(worktree))
        assert wrapped[0][0][0] == "claude"
        assert "--resume" in wrapped[0][0]
        assert spawned[0][1:] == (["wrapped-claude"], str(worktree))

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

    def test_expired_provider_session_stays_terminal_and_preserves_source_transcript(self, tmp_path):
        source_id = "expired-source"
        respin_id = "expired-respin"
        session_id = "expired-provider-session"
        transcript = "saved source conversation\nwith reconstruction context\n"

        with patch("spindle.SPINDLE_DIR", tmp_path):
            source_path = _get_transcript_path(source_id)
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(transcript)
            _write_spool(
                respin_id,
                {
                    "id": respin_id,
                    "status": "running",
                    "session_id": session_id,
                    "harness": "claude-code",
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_stderr_path(respin_id).write_text(f"No conversation found with session ID: {session_id}")
            _get_exit_path(respin_id).write_text("1\n")

            terminalizable = MagicMock(state="terminalizable")
            with patch("spindle._reconcile_spool_ownership", return_value=terminalizable):
                with patch("spindle._spawn_detached", side_effect=AssertionError("must not replace session")):
                    assert spindle._reconcile_spool_step(respin_id) is False
                    terminal = _read_spool(respin_id)
                    assert spindle._reconcile_spool_step(respin_id) is False

            assert _read_spool(respin_id) == terminal
            assert terminal["status"] == "error"
            assert terminal["error"] == f"No conversation found with session ID: {session_id}"
            assert "expired_session_replacement_requested" not in terminal
            assert "used_transcript_fallback" not in terminal
            assert source_path.read_text() == transcript

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
    @pytest.fixture(autouse=True)
    def released_owner(self):
        """Result parsing starts only after unified ownership reconciliation."""
        with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
            yield

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

    def test_finalize_marks_turn_failed_error_and_preserves_new_shard(self, tmp_path):
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
            with patch("spindle._cleanup_shard") as cleanup:
                assert _check_and_finalize_spool(spool_id) is True

            spool = _read_spool(spool_id)
            assert spool["status"] == "error"
            assert spool["error"] == "The requested model is not supported for this account."
            assert spool["result"] == stream
            assert spool["session_id"] == "thread-failed"
            assert spool["shard"]["startup_failure_preserved"] is True
            assert spool["shard_cleanup_preserved"] is True
            assert (tmp_path / "worktree").exists()
            cleanup.assert_not_called()
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
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
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
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
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
            spindle._finish_spawn_barrier(spool_id, start=True)
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

    def test_finalize_after_owner_cleanup_never_signals_and_preserves_failed_shard(self, tmp_path):
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
            with patch("spindle._terminate_process_group", side_effect=AssertionError("observer signal")) as terminate:
                with patch("spindle._cleanup_shard") as cleanup:
                    assert _check_and_finalize_spool(spool_id) is True
            terminate.assert_not_called()
            cleanup.assert_not_called()
            saved = _read_spool(spool_id)
            assert saved["status"] == "error"
            assert saved["error"] == "provider failed"
            assert saved["shard_cleanup_preserved"] is True
            assert "process_group_cleanup_warning" not in saved
            assert _get_output_path(spool_id).exists()

    def test_finalize_never_signals_reused_pid(self, tmp_path):
        spool_id = "codex-reused-pid"
        stream = json.dumps({"type": "turn.failed", "error": {"message": "provider failed"}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "work",
                    "pid": 989898,
                    "process_start_time": "original-birth",
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(stream)
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="unverifiable")):
                with patch("spindle._terminate_process_group") as terminate:
                    assert _check_and_finalize_spool(spool_id) is False
            saved = _read_spool(spool_id)

        terminate.assert_not_called()
        assert saved["status"] == "running"
        assert "process_group_cleanup_warning" not in saved

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
                    with patch("spindle._cleanup_shard") as cleanup:
                        assert _check_and_finalize_spool(spool_id) is True
            cleanup.assert_not_called()
            assert _read_spool(spool_id)["status"] == "error"

    def test_failed_shard_is_always_marked_preserved(self, tmp_path):
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
            assert spindle._preserve_failed_spool_shard(spool) is True
        assert spool["shard_cleanup_preserved"] is True
        assert "shard_cleanup_pending" not in spool

    @pytest.mark.parametrize("created_by_spool", [False, True])
    def test_finalize_never_deletes_failed_shard(self, tmp_path, created_by_spool):
        spool_id = f"codex-preserve-{created_by_spool}"
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

    def test_finalize_preserves_pristine_failed_shard_and_retry_reuses_it(self, tmp_path):
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
            assert spool["working_dir"] == str(worktree)
            assert spool["shard"]["startup_failure_preserved"] is True
            assert worktree.exists()
            assert (
                subprocess.run(
                    ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/shard-test"]
                ).returncode
                == 0
            )

            retry_tool = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
            with patch("spindle._codex_spin_sync", return_value="retry-spool") as retry:
                assert asyncio.run(retry_tool(spool_id)) == "retry-spool"
            assert retry.call_args.args[1] == str(worktree)
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
    @pytest.fixture(autouse=True)
    def released_owner(self):
        with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
            yield

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
    @pytest.fixture(autouse=True)
    def released_owner(self):
        with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
            yield

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

    def test_finalize_after_released_owner_never_signals_descendants(self, tmp_path):
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

            with patch("spindle._terminate_process_group", side_effect=AssertionError("observer signal")) as terminate:
                assert _check_and_finalize_spool(sid) is True
            terminate.assert_not_called()
            assert _read_spool(sid)["status"] == "complete"
            assert _get_output_path(sid).exists()

    def test_complete_output_gives_live_cli_bounded_shutdown_grace(self, tmp_path):
        sid = "claude-live-shutdown"
        live_proc = MagicMock()
        live_proc.pid = 565656
        live_proc.poll.return_value = None
        stream = json.dumps({"type": "result", "subtype": "success", "result": "done", "session_id": "session-live"})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spindle._PROC_HANDLES[sid] = live_proc
            _write_spool(
                sid,
                {
                    "id": sid,
                    "status": "running",
                    "harness": "claude-code",
                    "prompt": "x",
                    "pid": 565656,
                    "created_at": datetime.now().isoformat(),
                    "error": None,
                },
            )
            _get_output_path(sid).write_text(stream)
            with patch("spindle._terminate_process_group", side_effect=AssertionError("observer signal")) as terminate:
                with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
                    assert _check_and_finalize_spool(sid) is False
                    first = _read_spool(sid)
                    assert first["status"] == "running"
                    assert "output_complete_detected_at" not in first
                    terminate.assert_not_called()
                assert _check_and_finalize_spool(sid) is True

            saved = _read_spool(sid)

        terminate.assert_not_called()
        assert saved["status"] == "complete"
        assert saved["result"] == "done"
        assert saved["session_id"] == "session-live"
        assert "error" not in saved

    def test_finalize_really_kills_background_descendant_and_keeps_completed_result(self, tmp_path):
        sid = "codex-real-background-descendant"
        stream = json.dumps({"type": "turn.completed", "usage": {}})
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                sid,
                {
                    "id": sid,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "x",
                    "pid": None,
                    "created_at": datetime.now().isoformat(),
                },
            )
            pid = spindle._spawn_detached(
                sid,
                [
                    "/bin/sh",
                    "-c",
                    'sleep 30 </dev/null >/dev/null 2>&1 & printf "%s\\n" "$1"',
                    "child",
                    stream,
                ],
                str(tmp_path),
            )
            spindle._finish_spawn_barrier(sid, start=True)
            spool = _read_spool(sid)
            spool["pid"] = pid
            spool["process_start_time"] = spindle._process_start_time(pid)
            _write_spool(sid, spool)
            assert spindle._PROC_HANDLES[sid].wait(timeout=5) == 0

            assert _check_and_finalize_spool(sid) is True
            saved = _read_spool(sid)

        assert saved["status"] == "complete"
        assert saved["exit_code"] == 0
        assert saved["result"].strip() == stream

    def test_detached_wrapper_closes_barrier_and_exposes_portable_identity(self, tmp_path):
        if not Path("/proc/self/fd").exists():
            pytest.skip("fd inheritance probe requires procfs")

        spool_id = "portable-process-identity"
        ready = tmp_path / "ready"
        fds = tmp_path / "fds.json"
        child = (
            "import json, os, time; "
            "from pathlib import Path; "
            f"root=Path('/proc/self/fd'); Path({str(fds)!r}).write_text("
            "json.dumps([os.readlink(path) for path in root.iterdir() "
            "if path.name.isdigit() and path.exists()])); "
            f"Path({str(ready)!r}).touch(); time.sleep(30)"
        )
        watchdog_pid = spindle._spawn_detached(
            spool_id,
            [sys.executable, "-c", child],
            str(tmp_path),
        )
        spindle._finish_spawn_barrier(spool_id, start=True)
        handle = spindle._PROC_HANDLES.pop(spool_id)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ready.exists()

            assert all(not target.startswith("pipe:") for target in json.loads(fds.read_text()))
            spool = _read_spool(spool_id)
            identity = json.loads(spindle._get_owner_identity_path(spool_id).read_text())
            assert spool["pid"] == spool["owner_pid"]
            assert spool["owner_pid"] == identity["pid"]
            assert spool["owner_pid"] != watchdog_pid
            assert spool["watchdog_pid"] == watchdog_pid
            assert handle.pid == watchdog_pid
            lock_target = str(spindle._get_owner_lock_path(spool_id))
            assert lock_target not in json.loads(fds.read_text())
            spindle.create_control_request(spindle.SPINDLE_DIR, spool_id, "cancel", spool["owner_generation"], "test")
        finally:
            current = _read_spool(spool_id)
            if current and current.get("owner_generation") and handle.poll() is None:
                spindle.create_control_request(
                    spindle.SPINDLE_DIR,
                    spool_id,
                    "cancel",
                    current["owner_generation"],
                    "test-cleanup",
                )
            handle.wait(timeout=5)

        assert spindle._get_owner_lock_path(spool_id).exists()

    def test_child_environment_strips_supervisor_import_guard(self, monkeypatch):
        guard = spindle.SUPERVISOR_IMPORT_GUARD
        monkeypatch.setenv(guard, "1")

        assert guard not in spindle._process_env()
        assert guard not in spindle._process_env({guard: "1"})


class TestCancellationTermination:
    @pytest.mark.parametrize("tool_path", ["sync", "async"])
    def test_drop_escalates_and_does_not_leave_sigterm_ignoring_group(self, tmp_path, tool_path):
        spool_id = f"cancel-{tool_path}"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {"id": spool_id, "status": "pending", "created_at": datetime.now().isoformat()},
            )
            watchdog_pid = spindle._spawn_detached(
                spool_id,
                ["/bin/sh", "-c", 'trap "" TERM; while :; do sleep 1; done'],
                str(tmp_path),
            )
            spindle._finish_spawn_barrier(spool_id, start=True)
            owner = spindle._PROC_HANDLES.pop(spool_id)
            deadline = time.monotonic() + 5
            while (_read_spool(spool_id) or {}).get("status") != "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            running = _read_spool(spool_id)
            provider_pid = running["provider_pid"]
            try:
                if tool_path == "sync":
                    result = spindle._spin_drop_sync(spool_id)
                else:
                    drop_tool = spindle.spin_drop.fn if hasattr(spindle.spin_drop, "fn") else spindle.spin_drop
                    result = asyncio.run(drop_tool(spool_id))
                assert result.startswith(f"Cancellation requested for spool {spool_id}")
                owner.wait(timeout=5)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
            assert spindle._is_pid_alive(provider_pid) is False
            assert _read_spool(spool_id)["status"] == "error"
            assert watchdog_pid != running["owner_pid"]
            assert running["pid"] == running["owner_pid"]
            assert running["watchdog_pid"] == watchdog_pid
            identity = json.loads(spindle._get_owner_identity_path(spool_id).read_text())
            assert identity["pid"] == running["owner_pid"]

    @pytest.mark.parametrize("tool_path", ["sync", "async"])
    def test_drop_lock_prevents_finalizer_from_overwriting_terminal_state(self, tmp_path, tool_path):
        spool_id = f"cancel-finalize-race-{tool_path}"
        finalize_attempts = []

        real_create_request = spindle.create_control_request

        def request_while_finalizer_races(*args, **kwargs):
            finalize_attempts.append(_check_and_finalize_spool(spool_id))
            return real_create_request(*args, **kwargs)

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "harness": "codex",
                    "prompt": "x",
                    "pid": 464646,
                    "owner_generation": 1,
                    "created_at": datetime.now().isoformat(),
                },
            )
            _get_output_path(spool_id).write_text(json.dumps({"type": "turn.completed", "usage": {}}))
            _get_exit_path(spool_id).write_text("0\n")
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
                with patch("spindle.create_control_request", side_effect=request_while_finalizer_races):
                    if tool_path == "sync":
                        result = spindle._spin_drop_sync(spool_id)
                    else:
                        drop_tool = spindle.spin_drop.fn if hasattr(spindle.spin_drop, "fn") else spindle.spin_drop
                        result = asyncio.run(drop_tool(spool_id))
            saved = _read_spool(spool_id)

        assert result.startswith(f"Cancellation requested for spool {spool_id}")
        assert finalize_attempts == [False]
        assert saved["status"] == "running"
        assert saved["lifecycle"]["public_stop_state"] == "stopping"
        assert "result" not in saved

    def test_drop_keeps_stubborn_owner_slot_counted_until_owner_resolution(self, tmp_path):
        spool_id = "cancel-stubborn-group"
        shard = {"worktree_path": str(tmp_path / "worktree")}
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "pid": 575757,
                    "owner_generation": 2,
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                    "shard_created_by_spool": True,
                },
            )
            _get_output_path(spool_id).write_text("partial output")
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
                with patch("spindle._terminate_process_group", side_effect=AssertionError("observer signal")):
                    result = spindle._spin_drop_sync(spool_id)
            saved = _read_spool(spool_id)
            capture_preserved = _get_output_path(spool_id).exists()
            assert _count_running() == 1

        assert result.startswith(f"Cancellation requested for spool {spool_id}")
        assert saved["status"] == "running"
        assert saved["lifecycle"]["public_stop_state"] == "stopping"
        assert capture_preserved is True

    def test_drop_never_signals_reused_pid(self, tmp_path):
        spool_id = "cancel-reused-pid"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "pid": 919191,
                    "process_start_time": "original-birth",
                    "created_at": datetime.now().isoformat(),
                },
            )
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="unverifiable")):
                with patch("spindle._terminate_process_group") as terminate:
                    result = spindle._spin_drop_sync(spool_id)
            saved = _read_spool(spool_id)

        terminate.assert_not_called()
        assert result.startswith(f"Error: Cannot cancel spool {spool_id}: ownership unverifiable")
        assert saved["status"] == "running"
        assert "process_group_cleanup_warning" not in saved

    def test_drop_lock_contention_returns_bounded_error(self, tmp_path):
        spool_id = "cancel-lock-contention"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "pid": 484848,
                    "created_at": datetime.now().isoformat(),
                },
            )
            with _spool_lock(spool_id):
                with patch("spindle.SPOOL_TERMINAL_LOCK_TIMEOUT", 0.01):
                    started = time.monotonic()
                    result = spindle._spin_drop_sync(spool_id)
                    elapsed = time.monotonic() - started

        assert result == f"Error: Could not lock spool {spool_id} for cancellation"
        assert elapsed < 1


class TestFableGateRefusal:
    """Fable's bio/cyber safety gate surfaces as a distinct, agent-readable state.

    A gate refusal is a successful HTTP 200 with stop_reason "refusal" that the
    CLI reports as an API Error. It is not a task failure — the right response is
    to re-route to another model — so it must be distinguishable from a generic
    error for agents, triage, and skein.
    """

    @pytest.fixture(autouse=True)
    def released_owner(self):
        with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
            yield

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

    def test_spools_idle_keeps_unverifiable_legacy_running_spool_slot_counted(self, tmp_path):
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
            assert spindle._spools_idle() is False
            assert _read_spool("d1")["status"] == "running"

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
        # must not wedge the drain - the gated recovery pass times it out.
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

    def test_preserved_shard_spool_handle_does_not_expire(self, tmp_path):
        old_created = (datetime.now() - timedelta(hours=25)).isoformat()
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "preserved",
                {
                    "id": "preserved",
                    "status": "error",
                    "created_at": old_created,
                    "shard_cleanup_preserved": True,
                },
            )
            _write_spool(
                "ordinary",
                {
                    "id": "ordinary",
                    "status": "complete",
                    "created_at": old_created,
                },
            )

            spindle._cleanup_old_spools()

            assert _read_spool("preserved") is not None
            assert _read_spool("ordinary") is None

    def test_old_pending_reservation_survives_sweep_for_recovery(self, tmp_path):
        spool_id = "old-pending-shard"
        old_created = (datetime.now() - timedelta(hours=25)).isoformat()
        worktree = tmp_path / "worktree"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "pending",
                    "pid": None,
                    "created_at": old_created,
                    "shard": {"worktree_path": str(worktree)},
                    "shard_created_by_spool": True,
                },
            )
            _get_output_path(spool_id).write_text("startup diagnostics")

            spindle._cleanup_old_spools()

            assert _read_spool(spool_id) is not None
            assert _get_output_path(spool_id).read_text() == "startup diagnostics"

            _recovery_pass()

            recovered = _read_spool(spool_id)
            assert recovered["status"] == "error"
            assert recovered["shard_cleanup_preserved"] is True
            assert recovered["shard"]["startup_failure_preserved"] is True
            assert _get_output_path(spool_id).read_text() == "startup diagnostics"

    def test_live_warned_process_keeps_old_spool_record_and_captures(self, tmp_path):
        old_created = (datetime.now() - timedelta(hours=25)).isoformat()
        spool_id = "warned-live-group"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "created_at": old_created,
                    "pid": 595959,
                    "process_group_cleanup_warning": "group survived",
                },
            )
            _get_output_path(spool_id).write_text("still open")
            _get_stderr_path(spool_id).write_text("diagnostic")
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
                spindle._cleanup_old_spools()

            assert _read_spool(spool_id) is not None
            assert _get_output_path(spool_id).read_text() == "still open"
            assert _get_stderr_path(spool_id).read_text() == "diagnostic"

            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
                spindle._cleanup_old_spools()

            assert _read_spool(spool_id) is None
            assert not _get_output_path(spool_id).exists()
            assert not _get_stderr_path(spool_id).exists()

    def test_old_spool_sweep_rechecks_preservation_after_lock(self, tmp_path):
        spool_id = "cleanup-lock-race"
        old_created = (datetime.now() - timedelta(hours=25)).isoformat()

        @contextmanager
        def publish_warning_before_lock_yields(*args, **kwargs):
            current = _read_spool(spool_id)
            current["pid"] = 646464
            current["process_group_cleanup_warning"] = "group survived"
            _write_spool(spool_id, current)
            yield True

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "complete",
                    "created_at": old_created,
                },
            )
            _get_output_path(spool_id).write_text("still open")
            with patch("spindle._spool_lock", side_effect=publish_warning_before_lock_yields):
                with patch("spindle._is_pid_alive", return_value=False):
                    with patch("spindle._is_process_group_alive", return_value=True):
                        spindle._cleanup_old_spools()

            assert _read_spool(spool_id) is not None
            assert _get_output_path(spool_id).read_text() == "still open"

    def test_explicit_shard_abandon_clears_preservation_marker(self, tmp_path):
        spool_id = "preserved-abandon"
        worktree = tmp_path / "worktrees" / "preserved-abandon"
        worktree.mkdir(parents=True)
        cleanup_snapshot = {}

        def cleanup_after_durable_intent(*args, **kwargs):
            cleanup_snapshot.update(_read_spool(spool_id))
            return True

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "error",
                    "created_at": datetime.now().isoformat(),
                    "shard_cleanup_preserved": True,
                    "shard_cleanup_preserved_reason": "automatic cleanup disabled after agent failure",
                    "shard": {
                        "worktree_path": str(worktree),
                        "branch_name": "shard-preserved-abandon",
                        "startup_failure_preserved": True,
                    },
                },
            )
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._cleanup_shard", side_effect=cleanup_after_durable_intent):
                result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result == f"Abandoned shard {spool_id}"
        assert saved["shard"]["abandoned"] is True
        assert "startup_failure_preserved" not in saved["shard"]
        assert "shard_cleanup_preserved" not in saved
        assert "shard_cleanup_preserved_reason" not in saved
        assert cleanup_snapshot["shard"]["abandon_in_progress"] is True
        assert cleanup_snapshot["shard_cleanup_pending"] is True

    def test_terminal_shard_abandon_never_signals_unwarned_stale_pid(self, tmp_path):
        spool_id = "terminal-stale-pid"
        worktree = tmp_path / "worktrees" / "terminal-stale-pid"
        worktree.mkdir(parents=True)
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "error",
                    "pid": 767676,
                    "created_at": datetime.now().isoformat(),
                    "shard_cleanup_preserved": True,
                    "shard": {
                        "worktree_path": str(worktree),
                        "branch_name": "shard-terminal-stale-pid",
                    },
                },
            )
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._is_pid_alive", side_effect=AssertionError("must not probe stale PID")):
                with patch("spindle._terminate_process_group", side_effect=AssertionError("must not signal stale PID")):
                    with patch("spindle._cleanup_shard", return_value=True):
                        result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))

        assert result == f"Abandoned shard {spool_id}"

    def test_running_shard_abandon_refuses_unverifiable_pid(self, tmp_path):
        spool_id = "running-unverifiable-pid"
        worktree = tmp_path / "worktrees" / "running-unverifiable-pid"
        worktree.mkdir(parents=True)
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "pid": 777777,
                    "created_at": datetime.now().isoformat(),
                    "shard": {
                        "worktree_path": str(worktree),
                        "branch_name": "shard-running-unverifiable-pid",
                    },
                },
            )
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="unverifiable")):
                with patch("spindle._terminate_process_group") as terminate:
                    with patch("spindle._cleanup_shard") as cleanup:
                        result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))

        assert result.startswith(f"Error: Cannot abandon active spool {spool_id}: ownership unverifiable")
        terminate.assert_not_called()
        cleanup.assert_not_called()

    def test_running_shard_abandon_requests_owner_cleanup_before_destructive_work(self, tmp_path):
        spool_id = "running-abandon-race"
        worktree = tmp_path / "worktrees" / "running-abandon-race"
        worktree.mkdir(parents=True)
        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "running",
                    "created_at": datetime.now().isoformat(),
                    "pid": 636363,
                    "owner_generation": 3,
                    "shard": {
                        "worktree_path": str(worktree),
                        "branch_name": "shard-running-abandon-race",
                    },
                },
            )
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="active")):
                with patch("spindle._terminate_process_group") as terminate:
                    with patch("spindle._cleanup_shard") as cleanup:
                        result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result.startswith(f"Error: Cancellation requested for spool {spool_id}")
        terminate.assert_not_called()
        cleanup.assert_not_called()
        assert saved["status"] == "running"
        assert saved["lifecycle"]["public_stop_state"] == "stopping"
        assert "abandoned" not in saved["shard"]


class TestProcessUtils:
    """Test process utility functions."""

    def test_is_pid_alive_current_process(self):
        """Current process PID should be alive."""

        assert _is_pid_alive(os.getpid()) is True

    def test_process_identity_detects_pid_reuse_token_mismatch(self):
        token = spindle._process_start_time(os.getpid())
        assert token is not None
        spool = {"id": "identity", "pid": os.getpid(), "process_start_time": token}
        assert spindle._spool_process_identity_matches(spool) is True
        spool["process_start_time"] = "different-process-birth"
        assert spindle._spool_process_identity_matches(spool) is False

    def test_dead_popen_handle_is_not_process_identity(self):
        spool_id = "stale-popen-identity"
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = 0
        spindle._PROC_HANDLES[spool_id] = proc
        try:
            assert spindle._spool_process_identity_matches({"id": spool_id, "pid": 12345}) is False
        finally:
            spindle._PROC_HANDLES.pop(spool_id, None)

    def test_orphan_group_identity_allows_missing_leader_but_rejects_reuse(self):
        spool = {"id": "orphan-group", "pid": 22334, "process_start_time": "original-birth"}
        with patch("spindle._process_start_time", return_value=None):
            with patch("spindle._is_pid_alive", return_value=False):
                with patch("spindle._is_process_group_alive", return_value=True):
                    assert spindle._spool_process_group_identity_matches(spool) is True
        with patch("spindle._process_start_time", return_value="replacement-birth"):
            assert spindle._spool_process_group_identity_matches(spool) is False

    def test_termination_revalidates_identity_before_sigkill(self):
        identity_results = iter([True, False])
        with patch("spindle.os.killpg") as killpg:
            with patch("spindle._is_process_group_alive", return_value=True):
                with patch("spindle.time.sleep", return_value=None):
                    terminated = spindle._terminate_process_group(
                        23456,
                        0.5,
                        identity_check=lambda: next(identity_results),
                    )

        assert terminated is False
        killpg.assert_called_once_with(23456, signal.SIGTERM)

    def test_is_pid_alive_nonexistent(self):
        """Nonexistent PID should not be alive."""
        # Use a very high PID that's unlikely to exist
        assert _is_pid_alive(999999999) is False

    def test_is_pid_alive_never_reaps_popen_exit_status(self):
        with patch("spindle.os.waitpid", side_effect=AssertionError("must not reap")):
            assert _is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_uses_successful_signal_probe_without_proc(self):
        with patch("spindle.os.kill", return_value=None):
            with patch("spindle.Path.is_dir", return_value=False):
                assert _is_pid_alive(12345) is True


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


def _write_released_owner_evidence(root: Path, spool_id: str, *, pid: int = 999_999_999, exit_code: int = 0):
    lock_path = root / f"{spool_id}.process-owner"
    lock_path.touch()
    info = lock_path.stat()
    identity = spindle.ProcessIdentity(
        pid=pid,
        birth_token="test-owner-birth",
        namespace=spindle.capture_pid_namespace(),
        owner_generation=1,
        child_pgid=None,
        lock_device=info.st_dev,
        lock_inode=info.st_ino,
        lock_created=True,
    )
    (root / f"{spool_id}.owner-identity").write_text(json.dumps(identity.to_dict()))
    (root / f"{spool_id}.owner-exit").write_text(
        json.dumps(
            {
                "owner_generation": 1,
                "provider_exit_code": exit_code,
                "provider_reaped": True,
                "cleanup_outcome": "natural_exit",
            }
        )
    )
    (root / f"{spool_id}.exit").write_text(f"{exit_code}\n")


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
        _write_released_owner_evidence(tmp_path, spool_id)

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
            assert spool["status"] == "pending"
            assert spool["watchdog_pid"] == 12345
            assert spool["pid"] is None
            assert "owner_pid" not in spool

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


def test_kimi_bwrap_resolution_ignores_caller_path():
    with patch("spindle.shutil.which", return_value="/usr/bin/bwrap") as which:
        assert spindle._kimi_bwrap_binary({"PATH": "/project/bin"}) == "/usr/bin/bwrap"

    which.assert_called_once_with("bwrap")


def test_kimi_bwrap_resolution_rejects_relative_service_path():
    with patch("spindle.shutil.which", return_value="project-bin/bwrap") as which:
        assert spindle._kimi_bwrap_binary({"PATH": "/malicious/caller/bin"}) is None

    which.assert_called_once_with("bwrap")


def test_kimi_bwrap_wrapper_rejects_relative_explicit_binary(tmp_path):
    with pytest.raises(ValueError, match="bwrap is required"):
        _kimi_bwrap_wrap(
            ["kimi-cli"],
            str(tmp_path),
            [str(tmp_path)],
            {},
            bwrap_bin="project-bin/bwrap",
        )


class TestKimiHarness:
    """Test Kimi CLI harness implementation."""

    @pytest.fixture(autouse=True)
    def _skip_model_validation(self, tmp_path, monkeypatch):
        """Disable model-config validation by default so command-construction tests
        don't depend on the machine's ~/.kimi/config.toml. Validation-specific tests
        re-patch _kimi_registered_models with an explicit set."""
        fake_home = tmp_path / "default-home"
        (fake_home / ".kimi").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        with patch("spindle._kimi_registered_models", return_value=None):
            with patch("spindle._kimi_bwrap_binary", return_value="/usr/bin/bwrap"):
                yield

    def test_kimi_model_aliases(self):
        """Aliases should resolve only to models the managed provider actually serves."""
        assert KIMI_MODEL_ALIASES["thinking"] == "moonshot-ai/kimi-k3"
        assert KIMI_MODEL_ALIASES["k3"] == "moonshot-ai/kimi-k3"
        assert KIMI_MODEL_ALIASES["k2.6"] == "moonshot-ai/kimi-k2.6"
        assert KIMI_MODEL_ALIASES["k2.5"] == "moonshot-ai/kimi-k2.5"
        assert KIMI_MODEL_ALIASES["latest"] == "moonshot-ai/kimi-k3"
        # The retired standalone thinking/turbo models must not reappear as alias targets.
        assert "moonshot-ai/kimi-k2-thinking" not in KIMI_MODEL_ALIASES.values()
        assert "moonshot-ai/kimi-k2-turbo-preview" not in KIMI_MODEL_ALIASES.values()
        # Default model must be a real, registerable model (regression: was kimi-k2-thinking).
        assert KIMI_DEFAULT_MODEL == "moonshot-ai/kimi-k3"
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
        """The 'thinking' alias resolves to K3 and enables thinking mode."""
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

        assert "moonshot-ai/kimi-k3" in captured_cmd
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
        """No model specified defaults to K3 with its required thinking mode."""
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
        assert captured_cmd[m_idx + 1] == "moonshot-ai/kimi-k3"
        assert "--thinking" in captured_cmd

    @pytest.mark.parametrize("model", ["k3", "latest", "moonshot-ai/kimi-k3"])
    def test_kimi_k3_forces_thinking_for_every_selection_path(self, tmp_path, model):
        """K3 is always-thinking whether selected by alias or full model name."""
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
                        model=model,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                    )

        assert "moonshot-ai/kimi-k3" in captured_cmd
        assert "--thinking" in captured_cmd

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
        assert "interactive `/model`" in result
        assert "/setup" not in result
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
            assert spool["status"] == "pending"
            assert spool["watchdog_pid"] == 12345
            assert spool["pid"] is None
            assert "owner_pid" not in spool
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

        assert captured_cmd[0] == "/usr/bin/bwrap"
        assert "--ro-bind" in captured_cmd
        assert "--tmpfs" in captured_cmd
        assert "kimi-cli" in captured_cmd
        assert "--session" in captured_cmd
        assert "--print" in captured_cmd
        assert "--yolo" in captured_cmd
        assert "--output-format" in captured_cmd
        assert "stream-json" in captured_cmd
        assert "-p" in captured_cmd
        assert "-m" in captured_cmd
        assert "moonshot-ai/kimi-k2.6" in captured_cmd
        assert "-w" in captured_cmd

    def test_kimi_default_boundary_records_real_write_set(self, tmp_path):
        """Kimi's shared default label compiles to an external filesystem boundary,
        not a claim that kimi-cli itself has a careful approval mode."""
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch("spindle._count_running", return_value=0):
                    spool_id = _kimi_spin_sync(
                        prompt="Inspect this",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"HOME": str(fake_home)},
                    )

            spool = _read_spool(spool_id)

        assert spool["filesystem_boundary"] == {
            "kind": "bwrap",
            "root": "read-only",
            "writable_paths": [str(tmp_path.resolve()), str(kimi_state.resolve())],
            "readable_rebinds": [],
            "private_tmp": True,
            "private_run": True,
            "isolated_processes": True,
        }
        assert spool["permission"] is None
        assert spool["execution_cwd"] == str(tmp_path.resolve())

    def test_kimi_nonshard_boundary_never_expands_cwd_from_git_metadata(self, tmp_path):
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        working_dir = tmp_path / "subdir"
        kimi_state.mkdir(parents=True)
        working_dir.mkdir()

        with patch(
            "spindle._detect_existing_shard",
            side_effect=AssertionError("non-shard boundary must not inspect Git metadata"),
        ):
            paths = spindle._kimi_boundary_write_paths(
                "careful",
                str(working_dir),
                None,
                None,
                {"HOME": str(fake_home)},
            )

        assert paths == [str(working_dir.resolve()), str(kimi_state.resolve())]

    @pytest.mark.parametrize("permission", ["readonly", "manual", "careful"])
    def test_kimi_shared_permission_names_do_not_claim_narrower_box(self, tmp_path, permission):
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    spool_id = _kimi_spin_sync(
                        prompt="Inspect this",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"HOME": str(fake_home)},
                        permission=permission,
                    )

            spool = _read_spool(spool_id)

        binds = {
            (captured_cmd[i + 1], captured_cmd[i + 2]) for i, token in enumerate(captured_cmd[:-2]) if token == "--bind"
        }
        assert (str(kimi_state.resolve()), str(kimi_state.resolve())) in binds
        assert (str(tmp_path.resolve()), str(tmp_path.resolve())) in binds
        assert spool["filesystem_boundary"]["writable_paths"] == [
            str(tmp_path.resolve()),
            str(kimi_state.resolve()),
        ]

    def test_kimi_full_is_explicitly_uncontained(self, tmp_path):
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    spool_id = _kimi_spin_sync(
                        prompt="Set up Kimi",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                        permission="full",
                    )

            spool = _read_spool(spool_id)

        assert captured_cmd[0] == "kimi-cli"
        assert spool["filesystem_boundary"]["kind"] == "none"
        assert spool["filesystem_boundary"]["writable_paths"] == []

    def test_kimi_missing_bwrap_refuses_before_slot_or_shard(self, tmp_path):
        with patch("spindle._kimi_bwrap_binary", return_value=None):
            with patch("spindle._try_reserve_slot_and_create") as reserve:
                with patch("spindle._spawn_shard") as spawn_shard:
                    result = _kimi_spin_sync(
                        prompt="Change code",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                        permission="shard",
                        shard=True,
                    )

        assert result.startswith("Error:")
        assert "bwrap is required" in result
        assert "permission=full" in result
        reserve.assert_not_called()
        spawn_shard.assert_not_called()

    def test_kimi_full_with_shard_still_requires_bwrap(self, tmp_path):
        with patch("spindle._kimi_bwrap_binary", return_value=None):
            with patch("spindle._try_reserve_slot_and_create") as reserve:
                with patch("spindle._spawn_shard") as spawn_shard:
                    result = _kimi_spin_sync(
                        prompt="Change code",
                        working_dir=str(tmp_path),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env=None,
                        permission="full",
                        shard=True,
                    )

        assert result.startswith("Error:")
        assert "shard intent remains filesystem-contained" in result
        assert "omit shard=True" in result
        reserve.assert_not_called()
        spawn_shard.assert_not_called()

    def test_kimi_relative_state_dir_with_shard_refuses_before_reservation(self, tmp_path):
        with patch("spindle._try_reserve_slot_and_create") as reserve:
            with patch("spindle._spawn_shard") as spawn_shard:
                result = _kimi_spin_sync(
                    prompt="Change code",
                    working_dir=str(tmp_path),
                    model=None,
                    system_prompt=None,
                    timeout=None,
                    tags=None,
                    env={"KIMI_SHARE_DIR": ".kimi-state"},
                    permission="shard",
                    shard=True,
                )

        assert result.startswith("Error: relative KIMI_SHARE_DIR")
        reserve.assert_not_called()
        spawn_shard.assert_not_called()

    @pytest.mark.parametrize(
        "reserved_path_source",
        [
            "cwd",
            "state",
            "research",
            "dev-research",
            "proc-state",
            "run-state",
            "sys-state",
        ],
    )
    def test_kimi_reserved_mounts_refuse_before_reservation(self, tmp_path, reserved_path_source):
        working_dir = tmp_path / "project"
        kimi_state = tmp_path / "kimi-state"
        working_dir.mkdir()
        kimi_state.mkdir()
        env = {"KIMI_SHARE_DIR": str(kimi_state)}
        permission = "careful"
        research_target = None
        if reserved_path_source == "cwd":
            working_dir = Path("/tmp")
        elif reserved_path_source == "state":
            env = {"KIMI_SHARE_DIR": "/tmp"}
        elif reserved_path_source == "research":
            permission = "research"
            research_target = "file:/tmp/kimi-report.md"
        elif reserved_path_source == "dev-research":
            permission = "research"
            research_target = "file:/dev/shm/kimi-report.md"
        elif reserved_path_source == "run-state":
            env = {"KIMI_SHARE_DIR": "/run"}
        elif reserved_path_source == "sys-state":
            env = {"KIMI_SHARE_DIR": "/sys"}
        else:
            env = {"KIMI_SHARE_DIR": "/proc/self"}

        with patch("spindle._try_reserve_slot_and_create") as reserve:
            result = _kimi_spin_sync(
                prompt="Inspect",
                working_dir=str(working_dir),
                model=None,
                system_prompt=None,
                timeout=None,
                tags=None,
                env=env,
                permission=permission,
                research_target=research_target,
                require_research_target=permission == "research",
            )

        assert result.startswith("Error: Kimi writable path cannot replace reserved sandbox mount ")
        reserve.assert_not_called()

    def test_kimi_bwrap_blocks_sibling_and_symlink_escape(self, tmp_path):
        """Execute the real wrapper: cwd and Kimi state write, a sibling and a
        symlink into that sibling do not, and sandbox /tmp is not host /tmp."""
        bwrap = shutil.which("bwrap")
        if not bwrap:
            pytest.skip("bubblewrap is not installed")

        root = tmp_path / "boundary"
        work = root / "work"
        outside = root / "outside"
        share = root / "kimi-state"
        work.mkdir(parents=True)
        outside.mkdir()
        share.mkdir()
        (work / "escape").symlink_to(outside, target_is_directory=True)
        private_tmp_marker = f"kimi-bwrap-{tmp_path.name}"
        host_run_device = str(os.stat("/run").st_dev)
        host_pid = str(os.getpid())
        host_start_time = Path("/proc/self/stat").read_text().split()[21]

        inner = [
            "/bin/sh",
            "-c",
            (
                'touch "$1/work-ok" && touch "$2/state-ok" && '
                'touch "/tmp/$3" && ! touch "$4/sibling-bad" && '
                '! touch "$1/escape/symlink-bad" && '
                '[ "$(stat -c %d /run)" != "$5" ] && '
                "test -s /etc/resolv.conf && "
                '[ "$(cut -d " " -f 22 "/proc/$6/stat" 2>/dev/null)" != "$7" ]'
            ),
            "sh",
            str(work),
            str(share),
            private_tmp_marker,
            str(outside),
            host_run_device,
            host_pid,
            host_start_time,
        ]
        command = _kimi_bwrap_wrap(
            inner,
            str(work),
            [str(work), str(share)],
            os.environ.copy(),
            bwrap_bin=bwrap,
        )
        proc = subprocess.run(command, capture_output=True, text=True)

        assert proc.returncode == 0, proc.stderr
        assert (work / "work-ok").exists()
        assert (share / "state-ok").exists()
        assert not (outside / "sibling-bad").exists()
        assert not (outside / "symlink-bad").exists()
        assert not (Path("/tmp") / private_tmp_marker).exists()

    def test_kimi_bwrap_payload_stops_when_detached_group_is_terminated(self, tmp_path):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            pytest.skip("bubblewrap is not installed")

        work = tmp_path / "work"
        share = tmp_path / "kimi-state"
        spool_dir = tmp_path / "spools"
        for path in (work, share, spool_dir):
            path.mkdir()
        heartbeat = work / "heartbeat"
        command = _kimi_bwrap_wrap(
            [
                "/bin/sh",
                "-c",
                'while :; do printf x >> "$1"; sleep 0.05; done',
                "sh",
                str(heartbeat),
            ],
            str(work),
            [str(work), str(share)],
            os.environ.copy(),
            bwrap_bin=bwrap,
        )
        spool_id = "kimi-terminate-test"
        pid = None
        handle = None

        with patch("spindle.SPINDLE_DIR", spool_dir):
            try:
                pid = spindle._spawn_detached(
                    spool_id,
                    command,
                    str(work),
                    spindle._kimi_contained_spawn_env(None),
                )
                spindle._finish_spawn_barrier(spool_id, start=True)
                deadline = time.monotonic() + 5
                while (not heartbeat.exists() or heartbeat.stat().st_size < 2) and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert heartbeat.exists() and heartbeat.stat().st_size >= 2

                assert spindle._terminate_process_group(pid, 0.2) is True
                handle = spindle._PROC_HANDLES.pop(spool_id, None)
                if handle is not None:
                    handle.wait(timeout=2)
                stopped_size = heartbeat.stat().st_size
                time.sleep(0.15)
                assert heartbeat.stat().st_size == stopped_size
            finally:
                if pid is not None and spindle._is_process_group_alive(pid):
                    spindle._terminate_process_group(pid, 0.1)
                if handle is None:
                    handle = spindle._PROC_HANDLES.pop(spool_id, None)
                if handle is not None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        handle.wait(timeout=2)

    def test_kimi_linked_worktree_git_metadata_stays_read_only(self, tmp_path):
        """Kimi may edit a worktree but cannot stage through external Git metadata."""
        bwrap = shutil.which("bwrap")
        if not bwrap:
            pytest.skip("bubblewrap is not installed")

        repo = tmp_path / "repo"
        worktree = tmp_path / "feature-worktree"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature", str(worktree), "main"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (worktree / "tracked.txt").write_text("changed\n")

        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)
        process_env = os.environ.copy()
        process_env["HOME"] = str(fake_home)
        writable_paths = spindle._kimi_boundary_write_paths(
            "careful",
            str(worktree),
            None,
            None,
            process_env,
        )
        assert writable_paths == [str(worktree.resolve()), str(kimi_state.resolve())]

        command = _kimi_bwrap_wrap(
            ["/bin/sh", "-c", "printf 'contained\\n' > tracked.txt && ! git add tracked.txt"],
            str(worktree),
            writable_paths,
            process_env,
            bwrap_bin=bwrap,
        )
        proc = subprocess.run(command, capture_output=True, text=True, env=process_env)

        assert proc.returncode == 0, proc.stderr
        assert (worktree / "tracked.txt").read_text() == "contained\n"
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree,
            capture_output=True,
        )
        assert staged.returncode == 0

    def test_kimi_shard_prompt_leaves_changes_uncommitted(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        fake_home = tmp_path / "home"
        (fake_home / ".kimi").mkdir(parents=True)
        shard_info = {
            "worktree_path": str(worktree),
            "branch_name": "shard-kimi",
            "shard_id": "kimi",
        }
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._detect_existing_shard", return_value=shard_info):
                with patch("spindle._spawn_detached", side_effect=fake_spawn):
                    with patch("spindle._count_running", return_value=0):
                        _kimi_spin_sync(
                            prompt="Change code",
                            working_dir=str(worktree),
                            model=None,
                            system_prompt=None,
                            timeout=None,
                            tags=None,
                            env={"HOME": str(fake_home)},
                            permission="shard",
                            shard=True,
                        )

        prompt = captured_cmd[captured_cmd.index("-p") + 1]
        assert "Do not commit" in prompt
        assert "git add" not in prompt
        assert "skein ignite" not in prompt

    def test_kimi_existing_shard_preserves_requested_subdirectory(self, tmp_path):
        worktree = tmp_path / "worktree"
        requested_cwd = worktree / "src"
        requested_cwd.mkdir(parents=True)
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)
        shard_info = {
            "worktree_path": str(worktree),
            "branch_name": "shard-kimi",
            "shard_id": "kimi",
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = list(cmd)
            captured["cwd"] = cwd
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._detect_existing_shard", return_value=shard_info):
                with patch("spindle._spawn_detached", side_effect=fake_spawn):
                    with patch("spindle._count_running", return_value=0):
                        spool_id = _kimi_spin_sync(
                            prompt="Change code",
                            working_dir=str(requested_cwd),
                            model=None,
                            system_prompt=None,
                            timeout=None,
                            tags=None,
                            env={"HOME": str(fake_home)},
                            permission="shard",
                            shard=True,
                        )

            spool = _read_spool(spool_id)

        assert captured["cwd"] == str(requested_cwd.resolve())
        assert captured["cmd"][captured["cmd"].index("--chdir") + 1] == str(requested_cwd.resolve())
        assert captured["cmd"][captured["cmd"].index("-w") + 1] == str(requested_cwd.resolve())
        assert spool["execution_cwd"] == str(requested_cwd.resolve())
        assert spool["shard"]["worktree_path"] == str(worktree.resolve())
        assert spool["filesystem_boundary"]["writable_paths"] == [
            str(worktree.resolve()),
            str(kimi_state.resolve()),
        ]

    def test_kimi_new_shard_uses_canonical_worktree_path(self, tmp_path):
        source = tmp_path / "source"
        actual_store = tmp_path / "actual-worktrees"
        source.mkdir()
        actual_store.mkdir()
        (source / "worktrees").symlink_to(actual_store, target_is_directory=True)
        raw_worktree = source / "worktrees" / "kimi-test"
        actual_worktree = actual_store / "kimi-test"
        actual_worktree.mkdir()
        kimi_state = tmp_path / "kimi-state"
        kimi_state.mkdir()
        shard_info = {
            "worktree_path": str(raw_worktree),
            "branch_name": "shard-kimi-test",
            "shard_id": "kimi-test",
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cwd"] = cwd
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._detect_existing_shard", return_value=None):
                with patch("spindle._spawn_shard", return_value=(shard_info, None)):
                    with patch("spindle._spawn_detached", side_effect=fake_spawn):
                        with patch("spindle._count_running", return_value=0):
                            spool_id = _kimi_spin_sync(
                                prompt="Change code",
                                working_dir=str(source),
                                model=None,
                                system_prompt=None,
                                timeout=None,
                                tags=None,
                                env={"KIMI_SHARE_DIR": str(kimi_state)},
                                permission="shard",
                                shard=True,
                            )

            spool = _read_spool(spool_id)

        assert captured["cwd"] == str(actual_worktree.resolve())
        assert spool["execution_cwd"] == str(actual_worktree.resolve())
        assert spool["shard"]["worktree_path"] == str(actual_worktree.resolve())

    def test_kimi_post_shard_boundary_failure_preserves_shard(self, tmp_path):
        source = tmp_path / "source"
        worktree = tmp_path / "worktree"
        kimi_state = tmp_path / "kimi-state"
        source.mkdir()
        worktree.mkdir()
        kimi_state.mkdir()
        shard_info = {
            "worktree_path": str(worktree),
            "branch_name": "shard-kimi-test",
            "shard_id": "kimi-test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._detect_existing_shard", return_value=None):
                with patch("spindle._spawn_shard", return_value=(shard_info, None)):
                    with patch(
                        "spindle._kimi_boundary_write_paths",
                        side_effect=[
                            [str(source), str(kimi_state)],
                            ValueError("post-shard boundary failure"),
                        ],
                    ):
                        with patch("spindle._count_running", return_value=0):
                            result = _kimi_spin_sync(
                                prompt="Change code",
                                working_dir=str(source),
                                model=None,
                                system_prompt=None,
                                timeout=None,
                                tags=None,
                                env={"KIMI_SHARE_DIR": str(kimi_state)},
                                permission="shard",
                                shard=True,
                            )

            spool_files = list((tmp_path / "spools").glob("kimi-*.json"))
            assert len(spool_files) == 1
            spool = json.loads(spool_files[0].read_text())

        assert result == "Error: post-shard boundary failure"
        assert spool["status"] == "error"
        assert spool["shard"]["worktree_path"] == str(worktree.resolve())
        assert spool["shard"]["startup_failure_preserved"] is True
        assert spool["shard_cleanup_preserved"] is True

    def test_kimi_terminal_recovery_race_still_attaches_created_shard(self, tmp_path):
        spool_id = "kimi-race"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        completed_at = datetime.now().isoformat()
        terminal_reservation = {
            "id": spool_id,
            "status": "error",
            "error": "pending reservation expired before shard returned",
            "created_at": datetime.now().isoformat(),
            "completed_at": completed_at,
        }
        shard = {
            "worktree_path": str(worktree),
            "branch_name": "shard-kimi-race",
            "shard_id": "kimi-race",
        }
        metadata = {
            "working_dir": str(tmp_path),
            "shard": shard,
            "shard_created_by_spool": True,
            "shard_source_dir": str(tmp_path),
            "base_branch": "main",
            "harness": "kimi",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(spool_id, terminal_reservation)
            spindle._record_pre_spawn_failure(
                spool_id,
                "later boundary failure",
                metadata,
            )
            spool = _read_spool(spool_id)

        assert spool["error"] == terminal_reservation["error"]
        assert spool["completed_at"] == completed_at
        assert spool["shard"] == {
            **shard,
            "startup_failure_preserved": True,
        }
        assert spool["shard_created_by_spool"] is True
        assert spool["shard_cleanup_preserved"] is True

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
                        env={
                            "KIMI_API_KEY": "test-key",
                            "LD_PRELOAD": "/project/escape.so",
                            "LD_AUDIT": "/project/audit.so",
                            "BASH_ENV": "/project/bash-env",
                            "SHELLOPTS": "xtrace",
                            "BASHOPTS": "extdebug",
                            "PS4": "$(touch /tmp/pre-bwrap)",
                            "BASH_XTRACEFD": "9",
                            "BASH_FUNC_printf%%": "() { touch /tmp/pre-bwrap; }",
                        },
                    )

        assert captured_env[0]["KIMI_API_KEY"] == "test-key"
        assert captured_env[0]["LD_PRELOAD"] == ""
        assert captured_env[0]["LD_AUDIT"] == ""
        assert captured_env[0]["BASH_ENV"] == ""
        assert captured_env[0]["SHELLOPTS"] == ""
        assert captured_env[0]["BASHOPTS"] == ""
        assert captured_env[0]["PS4"] == ""
        assert captured_env[0]["BASH_XTRACEFD"] == ""
        assert captured_env[0]["BASH_FUNC_printf%%"] == ""

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

    def test_kimi_research_adds_output_to_required_writable_working_dir(self, tmp_path):
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)
        working_dir = tmp_path / "project"
        working_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=fake_spawn):
                    spool_id = _kimi_spin_sync(
                        prompt="research this",
                        working_dir=str(working_dir),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"HOME": str(fake_home)},
                        permission="research",
                        research_target=f"dir:{output_dir}",
                        require_research_target=True,
                    )

            spool = _read_spool(spool_id)

        binds = {
            (captured_cmd[i + 1], captured_cmd[i + 2]) for i, token in enumerate(captured_cmd[:-2]) if token == "--bind"
        }
        assert (str(output_dir.resolve()), str(output_dir.resolve())) in binds
        assert (str(kimi_state.resolve()), str(kimi_state.resolve())) in binds
        assert (str(working_dir.resolve()), str(working_dir.resolve())) in binds
        assert spool["filesystem_boundary"]["writable_paths"] == [
            str(working_dir.resolve()),
            str(output_dir.resolve()),
            str(kimi_state.resolve()),
        ]

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

    def test_kimi_respin_replays_recorded_boundary_and_execution_cwd(self, tmp_path):
        fake_home = tmp_path / "home"
        kimi_state = fake_home / ".kimi"
        kimi_state.mkdir(parents=True)
        source = tmp_path / "source"
        worktree = tmp_path / "worktree"
        source.mkdir()
        worktree.mkdir()
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = list(cmd)
            captured["cwd"] = cwd
            captured["env"] = env
            return 12345

        original_spool = {
            "id": "kimi-original",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "working_dir": str(source),
            "execution_cwd": str(worktree),
            "model": "moonshot-ai/kimi-k2.6",
            "thinking": False,
            "env": {
                "HOME": str(fake_home),
                "LD_PRELOAD": "/project/escape.so",
                "ENV": "/project/sh-env",
                "SHELLOPTS": "xtrace",
                "PS4": "$(touch /tmp/pre-bwrap)",
            },
            "permission": "shard",
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-kimi",
                "shard_id": "kimi",
            },
            "filesystem_boundary": {
                "kind": "bwrap",
                "root": "read-only",
                "writable_paths": [str(worktree), str(kimi_state)],
                "readable_rebinds": [],
                "private_tmp": True,
                "private_run": True,
                "isolated_processes": True,
            },
            "research_target": "site:closed-after-original-spin",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    with patch(
                        "spindle._validate_research_target",
                        side_effect=AssertionError("recorded boundary must not revalidate target"),
                    ):
                        spool_id = _kimi_respin_sync(
                            session_id=original_spool["session_id"],
                            prompt="Continue",
                            original_spool=original_spool,
                        )

            spool = _read_spool(spool_id)

        assert captured["cwd"] == str(worktree.resolve())
        assert captured["cmd"][0] == "/usr/bin/bwrap"
        assert captured["cmd"][captured["cmd"].index("--chdir") + 1] == str(worktree.resolve())
        assert captured["cmd"][captured["cmd"].index("-w") + 1] == str(worktree.resolve())
        assert captured["env"]["HOME"] == str(fake_home)
        assert captured["env"]["LD_PRELOAD"] == ""
        assert captured["env"]["ENV"] == ""
        assert captured["env"]["SHELLOPTS"] == ""
        assert captured["env"]["PS4"] == ""
        assert spool["execution_cwd"] == str(worktree.resolve())
        assert spool["filesystem_boundary"] == original_spool["filesystem_boundary"]

    @pytest.mark.parametrize("shard_created_by_spool", [False, True])
    def test_kimi_legacy_respin_recovers_original_execution_cwd(self, tmp_path, shard_created_by_spool):
        fake_home = tmp_path / "home"
        (fake_home / ".kimi").mkdir(parents=True)
        source = tmp_path / "source"
        worktree = tmp_path / "worktree"
        requested_subdir = worktree / "src"
        source.mkdir()
        requested_subdir.mkdir(parents=True)
        working_dir = source if shard_created_by_spool else requested_subdir
        expected_cwd = worktree if shard_created_by_spool else requested_subdir
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cmd"] = list(cmd)
            captured["cwd"] = cwd
            return 12345

        original_spool = {
            "id": "kimi-legacy",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "working_dir": str(working_dir),
            "model": "moonshot-ai/kimi-k2.6",
            "thinking": False,
            "env": {"HOME": str(fake_home)},
            "permission": "shard",
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-kimi",
                "shard_id": "kimi",
            },
            "shard_created_by_spool": shard_created_by_spool,
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    spool_id = _kimi_respin_sync(
                        session_id=original_spool["session_id"],
                        prompt="Continue",
                        original_spool=original_spool,
                    )

            spool = _read_spool(spool_id)

        expected_cwd = str(expected_cwd.resolve())
        assert captured["cwd"] == expected_cwd
        assert captured["cmd"][captured["cmd"].index("--chdir") + 1] == expected_cwd
        assert captured["cmd"][captured["cmd"].index("-w") + 1] == expected_cwd
        assert spool["execution_cwd"] == expected_cwd
        assert spool["filesystem_boundary"]["writable_paths"][0] == str(worktree.resolve())

    @pytest.mark.parametrize("substituted_path", ["work", "state"])
    def test_kimi_respin_refuses_symlink_substitution(self, tmp_path, substituted_path):
        work = tmp_path / "work"
        kimi_state = tmp_path / "home" / ".kimi"
        victim = tmp_path / "victim"
        work.mkdir()
        kimi_state.mkdir(parents=True)
        victim.mkdir()
        original_spool = {
            "id": "kimi-original",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "working_dir": str(work),
            "execution_cwd": str(work),
            "model": "moonshot-ai/kimi-k2.6",
            "thinking": False,
            "env": {"HOME": str(tmp_path / "home")},
            "permission": "careful",
            "filesystem_boundary": {
                "kind": "bwrap",
                "root": "read-only",
                "writable_paths": [str(work), str(kimi_state)],
                "readable_rebinds": [],
                "private_tmp": True,
                "private_run": True,
                "isolated_processes": True,
            },
        }
        target = work if substituted_path == "work" else kimi_state
        target.rename(tmp_path / f"original-{substituted_path}")
        target.symlink_to(victim, target_is_directory=True)

        with patch("spindle._try_reserve_slot_and_create") as reserve:
            with patch("spindle._spawn_detached") as spawn:
                result = _kimi_respin_sync(
                    session_id=original_spool["session_id"],
                    prompt="Continue",
                    original_spool=original_spool,
                )

        assert result.startswith("Error: Kimi boundary paths changed")
        reserve.assert_not_called()
        spawn.assert_not_called()

    def test_kimi_retry_creates_fresh_shard_preserving_research_boundary(self, tmp_path):
        _retry = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
        source = tmp_path / "source"
        original_worktree = tmp_path / "original-worktree"
        retry_worktree = tmp_path / "retry-worktree"
        kimi_state = tmp_path / "home" / ".kimi"
        output = tmp_path / "research-output"
        for path in (source, original_worktree, retry_worktree, kimi_state, output):
            path.mkdir(parents=True)
        original_shard = {
            "worktree_path": str(original_worktree),
            "branch_name": "shard-kimi-original",
            "shard_id": "kimi-original",
        }
        retry_shard = {
            "worktree_path": str(retry_worktree),
            "branch_name": "shard-kimi-retry",
            "shard_id": "kimi-retry",
        }
        original_writable_paths = [
            str(original_worktree.resolve()),
            str(output.resolve()),
            str(kimi_state.resolve()),
        ]
        original = {
            "id": "kimi-retry-original",
            "status": "error",
            "prompt": "Continue the research edit",
            "working_dir": str(source),
            "execution_cwd": str(original_worktree),
            "model": "moonshot-ai/kimi-k2.6",
            "system_prompt": "Keep the report concise",
            "timeout": 120,
            "tags": ["kimi", "research"],
            "env": {"HOME": str(tmp_path / "home")},
            "permission": "research+shard",
            "research_target": f"dir:{output}",
            "shard": original_shard,
            "base_branch": "main",
            "filesystem_boundary": {
                "kind": "bwrap",
                "root": "read-only",
                "writable_paths": original_writable_paths,
                "readable_rebinds": [],
                "private_tmp": True,
                "private_run": True,
                "isolated_processes": True,
            },
            "harness": "kimi",
            "created_at": datetime.now().isoformat(),
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cwd"] = cwd
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(original["id"], original)
            with patch("spindle._detect_existing_shard", return_value=None):
                with patch("spindle._spawn_shard", return_value=(retry_shard, None)) as spawn_shard:
                    with patch("spindle._spawn_detached", side_effect=fake_spawn):
                        with patch("spindle._start_spool_monitor"):
                            with patch("spindle._count_running", return_value=0):
                                retry_id = asyncio.run(_retry(original["id"]))
            retry = _read_spool(retry_id)

        spawn_shard.assert_called_once()
        assert captured["cwd"] == str(retry_worktree.resolve())
        assert retry["execution_cwd"] == str(retry_worktree.resolve())
        assert retry["permission"] == original["permission"]
        assert retry["research_target"] == original["research_target"]
        assert retry["base_branch"] == original["base_branch"]
        assert retry["shard"]["worktree_path"] == str(retry_worktree.resolve())
        assert retry["filesystem_boundary"]["writable_paths"] == [
            str(retry_worktree.resolve()),
            str(output.resolve()),
            str(kimi_state.resolve()),
        ]
        assert str(source.resolve()) not in retry["filesystem_boundary"]["writable_paths"]
        assert str(original_worktree.resolve()) not in retry["filesystem_boundary"]["writable_paths"]

    def test_kimi_retry_permission_only_shard_intent_creates_shard(self, tmp_path):
        _retry = spool_retry.fn if hasattr(spool_retry, "fn") else spool_retry
        source = tmp_path / "source"
        worktree = tmp_path / "worktree"
        kimi_state = tmp_path / "home" / ".kimi"
        for path in (source, worktree, kimi_state):
            path.mkdir(parents=True)
        original = {
            "id": "kimi-retry-shard-intent",
            "status": "error",
            "prompt": "Retry the edit",
            "working_dir": str(source),
            "model": "moonshot-ai/kimi-k2.6",
            "tags": ["kimi"],
            "env": {"HOME": str(tmp_path / "home")},
            "permission": "shard",
            "shard": None,
            "base_branch": "main",
            "harness": "kimi",
            "created_at": datetime.now().isoformat(),
        }
        shard = {
            "worktree_path": str(worktree),
            "branch_name": "shard-kimi-retry-intent",
            "shard_id": "kimi-retry-intent",
        }
        captured = {}

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured["cwd"] = cwd
            return 12345

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            _write_spool(original["id"], original)
            with patch("spindle._detect_existing_shard", return_value=None):
                with patch("spindle._spawn_shard", return_value=(shard, None)) as spawn_shard:
                    with patch("spindle._spawn_detached", side_effect=fake_spawn):
                        with patch("spindle._start_spool_monitor"):
                            with patch("spindle._count_running", return_value=0):
                                retry_id = asyncio.run(_retry(original["id"]))
            retry = _read_spool(retry_id)

        spawn_shard.assert_called_once()
        assert captured["cwd"] == str(worktree.resolve())
        assert retry["permission"] == "shard"
        assert retry["shard"]["worktree_path"] == str(worktree.resolve())
        assert retry["filesystem_boundary"]["writable_paths"] == [
            str(worktree.resolve()),
            str(kimi_state.resolve()),
        ]
        assert str(source.resolve()) not in retry["filesystem_boundary"]["writable_paths"]

    def test_kimi_full_respin_stays_uncontained(self, tmp_path):
        captured_cmd = []

        def fake_spawn(spool_id, cmd, cwd, env=None):
            captured_cmd.extend(cmd)
            return 12345

        original_spool = {
            "id": "kimi-original",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "working_dir": str(tmp_path),
            "execution_cwd": str(tmp_path),
            "model": "moonshot-ai/kimi-k2.6",
            "thinking": False,
            "env": None,
            "permission": "full",
            "filesystem_boundary": {
                "kind": "none",
                "writable_paths": [],
                "private_tmp": False,
                "private_run": False,
                "isolated_processes": False,
            },
            "research_target": "site:closed-after-original-spin",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    with patch(
                        "spindle._validate_research_target",
                        side_effect=AssertionError("uncontained respin must not revalidate target"),
                    ):
                        spool_id = _kimi_respin_sync(
                            session_id=original_spool["session_id"],
                            prompt="Continue",
                            original_spool=original_spool,
                        )

            spool = _read_spool(spool_id)

        assert captured_cmd[0] == "kimi-cli"
        assert spool["filesystem_boundary"]["kind"] == "none"


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
        """spin_harnesses should advertise the fable and current Opus aliases."""
        result = _get_harnesses()
        models = result["claude-code"]["models"]
        assert models["fable"] == "claude-fable-5"
        assert models["fable-5"] == "claude-fable-5"
        assert models["opus-4.8"] == "claude-opus-4-8"
        assert models["opus-5"] == "claude-opus-5"

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
        working_dir = tmp_path / "work"
        kimi_home = tmp_path / "home"
        working_dir.mkdir()
        (kimi_home / ".kimi").mkdir(parents=True)
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._spawn_detached", side_effect=FileNotFoundError("kimi-cli not found")):
                    result = _kimi_spin_sync(
                        prompt="test prompt",
                        working_dir=str(working_dir),
                        model=None,
                        system_prompt=None,
                        timeout=None,
                        tags=None,
                        env={"HOME": str(kimi_home)},
                    )

            assert "Error" in result

            spools = _list_spools()
            assert len(spools) == 1
            spool = spools[0]
            assert spool["status"] == "error"
            assert "spawn failed" in spool["error"]

    @pytest.mark.parametrize("harness", ["claude-code", "gemini", "kimi"])
    def test_new_shard_spawn_failure_is_preserved_for_every_harness(self, tmp_path, harness):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        shard = {
            "worktree_path": str(worktree),
            "branch_name": f"shard-{harness}",
            "shard_id": f"spawn-{harness}",
        }
        common = {
            "prompt": "test prompt",
            "working_dir": str(tmp_path),
            "model": None,
            "system_prompt": None,
            "timeout": None,
            "tags": None,
            "env": None,
            "shard": True,
            "skeinless": True,
        }

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle._count_running", return_value=0):
                with patch("spindle._detect_existing_shard", return_value=None):
                    with patch("spindle._spawn_shard", return_value=(shard, None)):
                        with patch("spindle._has_skein", return_value=False):
                            with patch("spindle._spawn_detached", side_effect=OSError("boom")):
                                with patch("spindle._cleanup_shard") as cleanup:
                                    if harness == "claude-code":
                                        result = _spin_sync(
                                            permission=None,
                                            allowed_tools=None,
                                            **common,
                                        )
                                    elif harness == "gemini":
                                        result = spindle._gemini_spin_sync(**common)
                                    else:
                                        kimi_share_dir = tmp_path / "kimi-state"
                                        kimi_share_dir.mkdir()
                                        kimi_common = {
                                            **common,
                                            "env": {"KIMI_SHARE_DIR": str(kimi_share_dir)},
                                        }
                                        with (
                                            patch("spindle._kimi_validate_model", return_value=None),
                                            patch("spindle._kimi_bwrap_binary", return_value="/usr/bin/bwrap"),
                                        ):
                                            result = spindle._kimi_spin_sync(**kimi_common)

            cleanup.assert_not_called()
            spool = _list_spools()[0]

        assert result.startswith("Error: Failed to spawn process")
        assert spool["status"] == "error"
        assert spool["shard_created_by_spool"] is True
        assert spool["shard_cleanup_preserved"] is True
        assert spool["shard"]["startup_failure_preserved"] is True
        assert worktree.exists()

    def test_codex_spawn_failure_preserves_pristine_shard_for_inspection(self, tmp_path):
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
            assert spool["working_dir"] == str(worktree)
            assert spool["shard"]["startup_failure_preserved"] is True

        assert worktree.exists()
        assert (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/shard-spawn-failure"], cwd=repo
            ).returncode
            == 0
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


class TestRecoveryPassPending:
    """Test that the gated recovery pass cleans up stale pending spools."""

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
                    "shard": {"worktree_path": str(tmp_path / "worktree")},
                    "shard_created_by_spool": True,
                },
            )

            _recovery_pass()

            spool = _read_spool("stale1")
            assert spool["status"] == "error"
            assert "spawn timeout" in spool["error"]
            assert spool["completed_at"] is not None
            assert spool["shard_cleanup_preserved"] is True
            assert spool["shard"]["startup_failure_preserved"] is True

    def test_stale_pending_preservation_holds_terminal_lock(self, tmp_path):
        stale_time = (datetime.now() - timedelta(seconds=PENDING_SPAWN_TIMEOUT + 60)).isoformat()
        original_preserve = spindle._preserve_failed_spool_shard
        lock_attempts = []

        def assert_locked(spool):
            with spindle._spool_lock(spool["id"], blocking=False) as acquired:
                lock_attempts.append(acquired)
            original_preserve(spool)

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                "locked-stale",
                {
                    "id": "locked-stale",
                    "status": "pending",
                    "pid": None,
                    "created_at": stale_time,
                    "shard": {"worktree_path": str(tmp_path / "worktree")},
                    "shard_created_by_spool": True,
                },
            )
            with patch("spindle._preserve_failed_spool_shard", side_effect=assert_locked):
                _recovery_pass()

            assert lock_attempts == [False]
            assert _read_spool("locked-stale")["shard_cleanup_preserved"] is True

    def test_late_owner_barrier_is_closed_without_observer_signal_when_recovery_wins(self, tmp_path):
        spool_id = "recovered-before-pid"
        proc = MagicMock()
        proc.pid = 737373
        proc.poll.return_value = -15
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "error",
                    "error": "spawn timeout - never started",
                    "created_at": datetime.now().isoformat(),
                },
            )
            spindle._PROC_HANDLES[spool_id] = proc
            with patch("spindle._terminate_process_group") as terminate:
                assert spindle._publish_spawned_process(spool_id, 737373) is False
            saved = _read_spool(spool_id)

        terminate.assert_not_called()
        proc.poll.assert_called_once_with()
        assert spool_id not in spindle._PROC_HANDLES
        assert saved["status"] == "error"
        assert "pid" not in saved

    def test_late_owner_does_not_publish_pid_or_warning_after_recovery_wins(self, tmp_path):
        spool_id = "recovered-live-process"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "error",
                    "error": "spawn timeout - never started",
                    "process_start_time": "stale-birth-token",
                    "created_at": datetime.now().isoformat(),
                },
            )
            with patch("spindle._terminate_process_group") as terminate:
                assert spindle._publish_spawned_process(spool_id, 747474) is False
            saved = _read_spool(spool_id)

        terminate.assert_not_called()
        assert "pid" not in saved
        assert saved["process_start_time"] == "stale-birth-token"
        assert "process_group_cleanup_warning" not in saved

    def test_setup_does_not_overwrite_recovered_shard_reservation(self, tmp_path):
        spool_id = "recovered-during-setup"
        worktree = tmp_path / "worktree"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "error",
                    "error": "spawn timeout - never started",
                    "created_at": datetime.now().isoformat(),
                },
            )
            prepared = spindle._prepare_pending_spool_for_spawn(
                {
                    "id": spool_id,
                    "status": "pending",
                    "working_dir": str(worktree),
                    "shard": {"worktree_path": str(worktree)},
                    "shard_created_by_spool": True,
                    "shard_source_dir": str(tmp_path),
                    "harness": "codex",
                    "created_at": datetime.now().isoformat(),
                }
            )
            saved = _read_spool(spool_id)

        assert prepared is False
        assert saved["status"] == "error"
        assert saved["shard_cleanup_preserved"] is True
        assert saved["shard"]["startup_failure_preserved"] is True

    def test_pre_spawn_shard_failure_keeps_recovery_winner(self, tmp_path):
        spool_id = "codex-fixed123"

        def recovery_wins_during_setup(*args, **kwargs):
            recovered = _read_spool(spool_id)
            recovered["status"] = "error"
            recovered["error"] = "spawn timeout - never started"
            recovered["completed_at"] = datetime.now().isoformat()
            _write_spool(spool_id, recovered)
            return None, "simulated shard setup failure"

        with patch("spindle.SPINDLE_DIR", tmp_path / "spools"):
            with patch("spindle.uuid.uuid4", return_value="fixed123"):
                with patch("spindle._count_running", return_value=0):
                    with patch("spindle._codex_sandbox_refusal", return_value=None):
                        with patch("spindle._resolve_codex_binary", return_value="/fake/codex"):
                            with patch("spindle._codex_cli_version", return_value="test"):
                                with patch("spindle._codex_auth_mode", return_value="chatgpt"):
                                    with patch("spindle._detect_default_branch", return_value="main"):
                                        with patch("spindle._detect_existing_shard", return_value=None):
                                            with patch(
                                                "spindle._spawn_shard",
                                                side_effect=recovery_wins_during_setup,
                                            ):
                                                result = _codex_spin_sync(
                                                    "work",
                                                    str(tmp_path),
                                                    None,
                                                    "danger-full-access",
                                                    None,
                                                    None,
                                                    None,
                                                    shard=True,
                                                )
            saved = _read_spool(spool_id)

        assert result == "Error: Failed to create SHARD worktree — simulated shard setup failure"
        assert saved["status"] == "error"
        assert saved["error"] == "spawn timeout - never started"

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

            _recovery_pass()

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
                    needs_monitor = _recovery_pass()
            assert needs_monitor == ["running-after-restart"]
            start.assert_not_called()

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

            _recovery_pass()

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

            _recovery_pass()

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
    """Verify that _spin_sync preserves every shard on spawn failure."""

    def _make_fake_shard_info(self, path):
        return {
            "worktree_path": str(path),
            "branch_name": "shard-test-20260426-001",
            "shard_id": "test-20260426-001",
        }

    def test_newly_created_shard_preserved_on_spawn_failure(self, tmp_path):
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
                                    mock_cleanup.assert_not_called()
            spool = _list_spools()[0]
            assert spool["status"] == "error"
            assert spool["shard_cleanup_preserved"] is True
            assert spool["shard"]["startup_failure_preserved"] is True

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


class TestShardMergeCleanupFailure:
    def test_shard_identity_blocks_active_respin_even_when_cwd_is_outside(self, tmp_path):
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "shared-identity"
        worktree.mkdir(parents=True)
        target_id = "shared-identity-target"
        active_id = "shared-identity-respin"
        shard = {"worktree_path": str(worktree), "branch_name": "shard-shared-identity"}
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(
                target_id,
                {
                    "id": target_id,
                    "status": "complete",
                    "base_branch": "main",
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                },
            )
            _write_spool(
                active_id,
                {
                    "id": active_id,
                    "status": "running",
                    "working_dir": str(tmp_path / "outside"),
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                },
            )
            merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._cleanup_shard") as cleanup:
                merge_result = asyncio.run(merge(target_id, caller_cwd=str(tmp_path / "outside")))
                abandon_result = asyncio.run(abandon(target_id, caller_cwd=str(tmp_path / "outside")))

        assert active_id in merge_result
        assert active_id in abandon_result
        cleanup.assert_not_called()

    def test_shared_live_warned_group_blocks_merge_and_abandon(self, tmp_path):
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "shared-warned"
        worktree.mkdir(parents=True)
        target_id = "shared-warned-target"
        warned_id = "shared-warned-other"
        shard = {"worktree_path": str(worktree), "branch_name": "shard-shared-warned"}
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(
                target_id,
                {
                    "id": target_id,
                    "status": "complete",
                    "base_branch": "main",
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                },
            )
            _write_spool(
                warned_id,
                {
                    "id": warned_id,
                    "status": "complete",
                    "working_dir": str(tmp_path / "outside"),
                    "pid": 787878,
                    "process_group_cleanup_warning": "group survived",
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                },
            )
            merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
            abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
            with patch("spindle._is_pid_alive", return_value=True):
                with patch("spindle._cleanup_shard") as cleanup:
                    merge_result = asyncio.run(merge(target_id, caller_cwd=str(tmp_path / "outside")))
                    abandon_result = asyncio.run(abandon(target_id, caller_cwd=str(tmp_path / "outside")))

        assert warned_id in merge_result
        assert warned_id in abandon_result
        cleanup.assert_not_called()

    def test_shared_dead_warned_group_does_not_block_merge_or_abandon(self, tmp_path):
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "shared-dead-warned"
        worktree.mkdir(parents=True)
        merge_id = "shared-dead-merge"
        abandon_id = "shared-dead-abandon"
        warned_id = "shared-dead-warned-other"
        shard = {"worktree_path": str(worktree), "branch_name": "shard-shared-dead-warned"}
        base = {
            "status": "complete",
            "base_branch": "main",
            "created_at": datetime.now().isoformat(),
            "shard": shard,
        }
        git_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(merge_id, {"id": merge_id, **base})
            _write_spool(abandon_id, {"id": abandon_id, **base})
            _write_spool(
                warned_id,
                {
                    "id": warned_id,
                    "status": "complete",
                    "pid": 797979,
                    "process_start_time": "departed-process-token",
                    "process_group_cleanup_warning": "group survived",
                    "created_at": datetime.now().isoformat(),
                    "shard": shard,
                },
            )
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
                with patch("spindle.subprocess.run", side_effect=[git_ok, git_ok]):
                    with patch("spindle._cleanup_shard", return_value=True):
                        with patch("spindle._close_tender_folios", return_value=None):
                            merge_result = asyncio.run(merge(merge_id, caller_cwd=str(tmp_path / "outside")))
                            abandon_result = asyncio.run(abandon(abandon_id, caller_cwd=str(tmp_path / "outside")))

        assert merge_result == f"Successfully merged shard {merge_id} to main"
        assert abandon_result == f"Abandoned shard {abandon_id}"

    def test_merge_blocks_new_process_publication_in_same_worktree(self, tmp_path):
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "shared-launch"
        worktree.mkdir(parents=True)
        merge_id = "merge-owner"
        launch_id = "late-launch"
        merge_spool = {
            "id": merge_id,
            "status": "complete",
            "prompt": "merge",
            "base_branch": "main",
            "created_at": datetime.now().isoformat(),
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-shared-launch"},
        }
        launch_spool = {
            "id": launch_id,
            "status": "pending",
            "working_dir": str(worktree),
            "created_at": datetime.now().isoformat(),
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-shared-launch"},
            "harness": "codex",
        }
        merge_entered = threading.Event()
        release_merge = threading.Event()
        process_spawned = threading.Event()
        merge_results = []
        launch_results = []

        def git_run(*args, **kwargs):
            if not merge_entered.is_set():
                merge_entered.set()
                assert release_merge.wait(timeout=5)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def spawn(*args, **kwargs):
            process_spawned.set()
            if not Path(args[2]).exists():
                raise FileNotFoundError("worktree was merged and removed")
            return 757575

        def cleanup(*args, **kwargs):
            worktree.rmdir()
            return True

        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(merge_id, merge_spool)
            _write_spool(launch_id, {"id": launch_id, "status": "pending", "created_at": datetime.now().isoformat()})
            with patch("spindle.subprocess.run", side_effect=git_run):
                with patch("spindle._cleanup_shard", side_effect=cleanup):
                    with patch("spindle._close_tender_folios", return_value=None):
                        with patch("spindle._spawn_detached", side_effect=spawn):
                            merge_thread = threading.Thread(
                                target=lambda: merge_results.append(
                                    asyncio.run(merge(merge_id, caller_cwd=str(tmp_path / "outside")))
                                )
                            )
                            merge_thread.start()
                            assert merge_entered.wait(timeout=5)

                            launch_thread = threading.Thread(
                                target=lambda: launch_results.append(
                                    spindle._start_spool_process(launch_spool, ["codex"], str(worktree), None)
                                )
                            )
                            launch_thread.start()
                            assert not process_spawned.wait(timeout=0.2)

                            release_merge.set()
                            merge_thread.join(timeout=5)
                            launch_thread.join(timeout=5)
            launched = _read_spool(launch_id)

        assert not merge_thread.is_alive()
        assert not launch_thread.is_alive()
        assert merge_results == [f"Successfully merged shard {merge_id} to main"]
        assert launch_results == ["Error: Failed to spawn process: worktree was merged and removed"]
        assert launched["status"] == "error"
        assert "worktree was merged and removed" in launched["error"]

    def test_different_spool_handles_serialize_operations_on_shared_worktree(self, tmp_path):
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "shared-operations"
        worktree.mkdir(parents=True)
        merge_id = "shared-merge"
        abandon_id = "shared-abandon"
        base_spool = {
            "status": "complete",
            "prompt": "resolve shared shard",
            "base_branch": "main",
            "created_at": datetime.now().isoformat(),
            "shard": {"worktree_path": str(worktree), "branch_name": "shard-shared-operations"},
        }
        merge_entered = threading.Event()
        release_merge = threading.Event()
        abandon_finished = threading.Event()
        cleanup_calls = []

        def git_run(*args, **kwargs):
            if not merge_entered.is_set():
                merge_entered.set()
                assert release_merge.wait(timeout=5)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        def cleanup(*args, **kwargs):
            cleanup_calls.append(kwargs["spool_id"])
            return True

        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(merge_id, {"id": merge_id, **base_spool})
            _write_spool(abandon_id, {"id": abandon_id, **base_spool})
            with patch("spindle.subprocess.run", side_effect=git_run):
                with patch("spindle._cleanup_shard", side_effect=cleanup):
                    with patch("spindle._close_tender_folios", return_value=None):
                        merge_thread = threading.Thread(
                            target=lambda: asyncio.run(merge(merge_id, caller_cwd=str(tmp_path / "outside")))
                        )
                        merge_thread.start()
                        assert merge_entered.wait(timeout=5)

                        abandon_thread = threading.Thread(
                            target=lambda: (
                                asyncio.run(abandon(abandon_id, caller_cwd=str(tmp_path / "outside"))),
                                abandon_finished.set(),
                            )
                        )
                        abandon_thread.start()
                        assert not abandon_finished.wait(timeout=0.2)
                        assert cleanup_calls == []

                        release_merge.set()
                        merge_thread.join(timeout=5)
                        abandon_thread.join(timeout=5)

        assert not merge_thread.is_alive()
        assert not abandon_thread.is_alive()
        assert cleanup_calls == [merge_id, abandon_id]

    def test_pending_target_cannot_be_merged_or_abandoned(self, tmp_path):
        spool_id = "pending-shard-operation"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "pending-shard-operation"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "pending",
            "working_dir": str(worktree),
            "created_at": datetime.now().isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-pending-operation",
            },
        }
        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle._cleanup_shard") as cleanup:
                merge_result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
                abandon_result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert "still starting" in merge_result
        assert "still starting" in abandon_result
        assert saved["status"] == "pending"
        cleanup.assert_not_called()

    def test_merge_holds_terminal_lock_after_owner_released_without_signaling(self, tmp_path):
        spool_id = "merge-warned-group"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "merge-warned-group"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "complete",
            "prompt": "merge me",
            "base_branch": "main",
            "pid": 747474,
            "process_start_time": "warned-start-token",
            "process_group_cleanup_warning": "group survived normal finalization",
            "created_at": datetime.now().isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-merge-warned-group",
                "shard_id": "merge-warned-group",
            },
        }
        lock_attempts = []

        def git_run(*args, **kwargs):
            with spindle._spool_lock(spool_id, blocking=False) as acquired:
                lock_attempts.append(acquired)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
                with patch("spindle._terminate_process_group") as terminate:
                    with patch("spindle._pop_and_reap_process_handle") as reap:
                        with patch("spindle.subprocess.run", side_effect=git_run):
                            with patch("spindle._cleanup_shard", return_value=True):
                                with patch("spindle._close_tender_folios", return_value=None):
                                    result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result == f"Successfully merged shard {spool_id} to main"
        assert lock_attempts == [False, False]
        terminate.assert_not_called()
        reap.assert_called_once_with(spool_id)
        assert "process_group_cleanup_warning" not in saved

    def test_merge_clears_warning_after_process_group_has_exited(self, tmp_path):
        spool_id = "merge-dead-warned-group"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "merge-dead-warned-group"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "complete",
            "prompt": "merge me",
            "base_branch": "main",
            "pid": 757575,
            "process_start_time": "departed-process-token",
            "process_group_cleanup_warning": "group survived normal finalization",
            "created_at": datetime.now().isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-merge-dead-warned-group",
            },
        }
        git_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
                with patch(
                    "spindle._terminate_process_group",
                    side_effect=AssertionError("released owners need no signal"),
                ):
                    with patch("spindle.subprocess.run", side_effect=[git_ok, git_ok]):
                        with patch("spindle._cleanup_shard", return_value=True):
                            with patch("spindle._close_tender_folios", return_value=None):
                                result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result == f"Successfully merged shard {spool_id} to main"
        assert "process_group_cleanup_warning" not in saved

    def test_abandon_clears_warning_after_process_group_has_exited(self, tmp_path):
        spool_id = "abandon-dead-warned-group"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "abandon-dead-warned-group"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "error",
            "pid": 767676,
            "process_start_time": "departed-process-token",
            "process_group_cleanup_warning": "group survived normal finalization",
            "created_at": datetime.now().isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-abandon-dead-warned-group",
            },
        }
        abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle._reconcile_spool_ownership", return_value=MagicMock(state="terminalizable")):
                with patch(
                    "spindle._terminate_process_group",
                    side_effect=AssertionError("released owners need no signal"),
                ):
                    with patch("spindle._cleanup_shard", return_value=True):
                        result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result == f"Abandoned shard {spool_id}"
        assert "process_group_cleanup_warning" not in saved

    def test_merge_persists_intent_and_success_before_destructive_steps(self, tmp_path):
        spool_id = "merge-durable-transitions"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "merge-durable-transitions"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "complete",
            "prompt": "merge me",
            "base_branch": "main",
            "created_at": datetime.now().isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-merge-durable-transitions",
            },
        }
        observed = {}

        def git_run(cmd, **kwargs):
            if cmd[:2] == ["git", "merge"]:
                observed["before_merge"] = _read_spool(spool_id)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        def cleanup(*args, **kwargs):
            observed["before_cleanup"] = _read_spool(spool_id)
            return True

        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle.subprocess.run", side_effect=git_run):
                with patch("spindle._cleanup_shard", side_effect=cleanup):
                    with patch("spindle._close_tender_folios", return_value=None):
                        result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

        assert result == f"Successfully merged shard {spool_id} to main"
        assert observed["before_merge"]["shard"]["merge_in_progress"] is True
        assert observed["before_merge"]["shard_cleanup_preserved"] is True
        assert observed["before_cleanup"]["shard"]["merged"] is True
        assert observed["before_cleanup"]["shard_cleanup_pending"] is True
        assert "merge_in_progress" not in saved["shard"]
        assert "shard_cleanup_pending" not in saved

    def test_failed_merge_keeps_durable_recovery_marker(self, tmp_path):
        spool_id = "merge-conflict-preserved"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "merge-conflict-preserved"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "complete",
            "prompt": "merge me",
            "base_branch": "main",
            "created_at": (datetime.now() - timedelta(hours=25)).isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-merge-conflict-preserved",
            },
        }
        clean = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        conflict = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="Auto-merging file.py\nCONFLICT",
            stderr="Automatic merge failed; fix conflicts and commit the result.\n",
        )
        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge
        abandon = shard_abandon.fn if hasattr(shard_abandon, "fn") else shard_abandon

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle.subprocess.run", side_effect=[clean, conflict]):
                with patch("spindle._cleanup_shard") as cleanup:
                    result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)
            dirty_main = subprocess.CompletedProcess(args=[], returncode=0, stdout="UU file.py\n", stderr="")
            merge_head = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr="")
            with patch("spindle.subprocess.run", side_effect=[dirty_main, merge_head]):
                with patch("spindle._cleanup_shard") as abandon_cleanup:
                    abandon_result = asyncio.run(abandon(spool_id, caller_cwd=str(tmp_path / "outside")))
            after_abandon_attempt = _read_spool(spool_id)
            spindle._cleanup_old_spools()
            retained = _read_spool(spool_id)

        assert result.startswith("Error: Merge failed:")
        assert saved["shard"]["merge_failed"] is True
        assert "Automatic merge failed" in saved["shard"]["merge_error"]
        assert saved["shard_cleanup_preserved"] is True
        assert abandon_result == (
            f"Error: Spool {spool_id} has unresolved main-checkout merge recovery; shard preserved"
        )
        assert after_abandon_attempt["shard"]["merge_failed"] is True
        assert after_abandon_attempt["shard_cleanup_preserved"] is True
        assert retained is not None
        cleanup.assert_not_called()
        abandon_cleanup.assert_not_called()

    def test_successful_merge_preserves_handle_when_worktree_cleanup_fails(self, tmp_path):
        spool_id = "merge-cleanup-failure"
        state_dir = tmp_path / "spools"
        worktree = tmp_path / "worktrees" / "merge-cleanup-failure"
        worktree.mkdir(parents=True)
        spool = {
            "id": spool_id,
            "status": "complete",
            "prompt": "merge me",
            "base_branch": "main",
            "created_at": (datetime.now() - timedelta(hours=25)).isoformat(),
            "shard": {
                "worktree_path": str(worktree),
                "branch_name": "shard-merge-cleanup-failure",
                "shard_id": "merge-cleanup-failure",
            },
        }
        clean_status = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        merged = subprocess.CompletedProcess(args=[], returncode=0, stdout="merged", stderr="")
        merge = shard_merge.fn if hasattr(shard_merge, "fn") else shard_merge

        with patch("spindle.SPINDLE_DIR", state_dir):
            _write_spool(spool_id, spool)
            with patch("spindle.subprocess.run", side_effect=[clean_status, merged]):
                with patch("spindle._cleanup_shard", return_value=False):
                    with patch("spindle._close_tender_folios") as close_tenders:
                        result = asyncio.run(merge(spool_id, caller_cwd=str(tmp_path / "outside")))
            saved = _read_spool(spool_id)

            spindle._cleanup_old_spools()
            retained = _read_spool(spool_id)

        assert result == f"Warning: Merge succeeded to main, but shard cleanup failed for {spool_id}"
        assert saved["shard"]["merged"] is True
        assert saved["shard_cleanup_pending"] is True
        assert saved["shard_cleanup_preserved"] is True
        assert retained is not None
        close_tenders.assert_not_called()


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
        records = list(spool_dir.glob("*.json"))
        assert len(records) == 1
        saved = json.loads(records[0].read_text())
        assert saved["status"] == "error"
        assert saved["error"] == "Failed to create SHARD worktree — worktree creation bombed"

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

    def test_timeout_publishes_owner_request_without_signaling(self, tmp_path):
        spool_id = "test-timeout-request"
        spool = {
            "id": spool_id,
            "status": "running",
            "owner_generation": 7,
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            active = MagicMock(state="active")
            with patch("spindle._reconcile_spool_ownership", return_value=active) as reconcile:
                with patch(
                    "spindle._terminate_process_group",
                    side_effect=AssertionError("observer must not signal"),
                ):
                    assert spindle._reconcile_spool_step(spool_id) is True
            result = _read_spool(spool_id)

        assert reconcile.call_count == 2
        assert result["status"] == "running"
        assert result["lifecycle"]["public_stop_state"] == "stopping"
        assert result["lifecycle"]["desired_terminal_kind"] == "timeout"
        requests = list((tmp_path / f"{spool_id}.control-mailbox").glob("*.request"))
        assert len(requests) == 1
        assert json.loads(requests[0].read_text())["kind"] == "timeout"

    @pytest.mark.parametrize("ownership_state", ["unverifiable", "store_unhealthy"])
    def test_timeout_with_unresolved_ownership_stays_active(self, tmp_path, ownership_state):
        spool_id = f"test-timeout-{ownership_state}"
        spool = {
            "id": spool_id,
            "status": "running",
            "owner_generation": 7,
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            with patch(
                "spindle._reconcile_spool_ownership",
                return_value=MagicMock(state=ownership_state),
            ):
                with patch(
                    "spindle._terminate_process_group",
                    side_effect=AssertionError("observer must not signal"),
                ):
                    assert spindle._reconcile_spool_step(spool_id) is True
            result = _read_spool(spool_id)

        assert result == spool
        assert not (tmp_path / f"{spool_id}.control-mailbox").exists()

    def test_terminalizable_completed_output_is_parsed_without_signaling(self, tmp_path):
        spool_id = "test-timeout-completed-output"
        stream = json.dumps({"type": "result", "subtype": "success", "result": "done", "session_id": "session-timeout"})
        spool = {
            "id": spool_id,
            "status": "running",
            "harness": "claude-code",
            "timeout": 1,
            "created_at": (datetime.now() - timedelta(seconds=5)).isoformat(),
            "prompt": "test",
        }

        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(spool_id, spool)
            _get_output_path(spool_id).write_text(stream)
            with patch(
                "spindle._reconcile_spool_ownership",
                return_value=MagicMock(state="terminalizable"),
            ):
                with patch(
                    "spindle._terminate_process_group",
                    side_effect=AssertionError("observer must not signal"),
                ):
                    assert spindle._reconcile_spool_step(spool_id) is False
            result = _read_spool(spool_id)

        assert result["status"] == "complete"
        assert result["result"] == "done"
        assert result["session_id"] == "session-timeout"

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

    @pytest.mark.parametrize("mode", ["yield", "gather"])
    def test_spin_wait_treats_spool_timeout_status_as_terminal(self, tmp_path, mode):
        spool_id = f"test-spin-wait-timeout-{mode}"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "timeout",
                    "error": "Timeout after 10s",
                    "created_at": datetime.now().isoformat(),
                },
            )
            result = json.loads(spindle._spin_wait_sync(spool_id, mode=mode))

        if mode == "yield":
            assert result == {
                "spool_id": spool_id,
                "error": "Timeout after 10s",
                "remaining": [],
            }
        else:
            assert result == {spool_id: "Error: Timeout after 10s"}

    @pytest.mark.parametrize("mode", ["yield", "gather"])
    def test_public_spin_wait_treats_spool_timeout_status_as_terminal(self, tmp_path, mode):
        spool_id = f"test-public-spin-wait-timeout-{mode}"
        with patch("spindle.SPINDLE_DIR", tmp_path):
            _write_spool(
                spool_id,
                {
                    "id": spool_id,
                    "status": "timeout",
                    "error": "Timeout after 10s",
                    "created_at": datetime.now().isoformat(),
                },
            )
            public_spin_wait = getattr(spindle.spin_wait, "fn", spindle.spin_wait)
            result = json.loads(asyncio.run(public_spin_wait(spool_id, mode=mode)))

        if mode == "yield":
            assert result == {
                "spool_id": spool_id,
                "error": "Timeout after 10s",
                "remaining": [],
            }
        else:
            assert result == {spool_id: "Error: Timeout after 10s"}


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
        """Spin, respin, and retry reconstruct profile spawn env through one helper."""
        monkeypatch.setenv("ALT_KEY", "k")
        _make_profile(
            profiles_root,
            "alt",
            {"base_url": "https://api.example.com/anthropic", "api_key": "${ALT_KEY}", "model": "big"},
        )

        def fake_spawn(spool_id, cmd, cwd, env=None):
            return 1

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
        wrapped = MagicMock(side_effect=_profile_spawn_env)
        with patch("spindle._profile_spawn_env", wrapped):
            with patch("spindle._spawn_detached", side_effect=fake_spawn):
                with patch("spindle._count_running", return_value=0):
                    asyncio.run(self._spin()("x", harness="alt", working_dir=str(tmp_path)))  # 1. spin
                    _respin_sync("sess-respin", "again")  # 2. respin
                    asyncio.run(spool_retry.fn("route-retry"))  # 3. retry

        called_profiles = [c.args[0] for c in wrapped.call_args_list]
        # Every path asked the helper to reconstruct the "alt" profile spawn env.
        assert called_profiles.count("alt") == 3

    # --- retry profile re-resolution --------------------------------------

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


# ---------------------------------------------------------------------------
# Release identity: version single-sourcing, /health identity, spindle doctor,
# and the service files install-service writes.
#
# These tests reference symbols through the `spindle` module rather than the
# top-of-file import list, so they stay independent of it.
# ---------------------------------------------------------------------------


class TestVersionSingleSource:
    """The version must have exactly one source, or CLI and service can skew."""

    def test_version_exposed(self):
        assert isinstance(spindle.__version__, str)
        assert spindle.__version__.strip() == spindle.__version__
        # PEP 440-ish: at least major.minor
        assert spindle.__version__.count(".") >= 1
        from spindle import _version

        assert _version.__version__ == spindle.__version__

    def test_version_module_has_no_imports(self):
        """_version.py must stay import-free so setuptools can read it statically.

        The build reads the version with `attr:`, which only avoids importing the
        package while the value is a plain literal in a module it can parse.
        """
        source = Path(spindle.__file__).parent / "_version.py"
        text = source.read_text()
        assert "\nimport " not in text
        assert "\nfrom " not in text

    def test_pyproject_reads_the_version_from_the_package(self):
        """A static version in pyproject would let the wheel disagree with the code."""
        pyproject = Path(spindle.__file__).parent.parent / "pyproject.toml"
        if not pyproject.exists():  # installed wheel, not a checkout
            pytest.skip("no pyproject.toml (running against an installed wheel)")
        tomllib = pytest.importorskip("tomllib")  # 3.11+; the check is skipped on 3.10
        data = tomllib.loads(pyproject.read_text())
        assert "version" in data["project"]["dynamic"]
        assert "version" not in data["project"]  # no static version to drift
        assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "spindle._version.__version__"


class TestHealthIdentity:
    """/health must carry enough identity for a client to recognize the install."""

    def _health(self):
        return asyncio.run(spindle.health_check(MagicMock()))

    def test_health_reports_identity(self):
        payload = json.loads(self._health().body)
        assert payload["status"] == "healthy"
        assert payload["version"] == spindle.__version__
        assert payload["package"] == str(Path(spindle.__file__).resolve())
        assert payload["pid"] == os.getpid()
        assert payload["spool_dir"] == str(spindle.SPINDLE_DIR)

    def test_health_keeps_legacy_fields(self):
        """Existing monitors read these; adding identity must not drop them."""
        payload = json.loads(self._health().body)
        for key in ("status", "uptime_seconds", "running_spools", "max_concurrent"):
            assert key in payload


class TestDoctorServiceIdentity:
    """The check that stops a fresh install reporting another one's service as its own."""

    def _check(self, monkeypatch, payload, error=None):
        monkeypatch.setattr(spindle, "_fetch_health", lambda host, port, timeout=2.0: (payload, error))
        return spindle._doctor_service_check("127.0.0.1", 8002)

    def _healthy(self, **overrides):
        payload = {
            "status": "healthy",
            "uptime_seconds": 10,
            "running_spools": 0,
            "max_concurrent": 15,
            "version": spindle.__version__,
            "package": str(Path(spindle.__file__).resolve()),
            "pid": 4242,
            "spool_dir": str(spindle.SPINDLE_DIR),
        }
        payload.update(overrides)
        return payload

    def test_no_service_is_a_warning_not_a_failure(self, monkeypatch):
        result = self._check(monkeypatch, None, error="connection refused")
        assert result["status"] == "warn"
        assert result["data"]["running"] is False

    def test_same_install_is_ok(self, monkeypatch):
        result = self._check(monkeypatch, self._healthy())
        assert result["status"] == "ok"
        assert "same install" in result["detail"]

    def test_foreign_install_on_the_port_fails(self, monkeypatch):
        """The dev-service confusion: another spindle answering our port."""
        result = self._check(monkeypatch, self._healthy(package="/opt/other/spindle/__init__.py"))
        assert result["status"] == "fail"
        assert "DIFFERENT" in result["detail"]
        assert any("/opt/other/spindle/__init__.py" in line for line in result["lines"])

    def test_version_skew_fails(self, monkeypatch):
        result = self._check(monkeypatch, self._healthy(version="0.0.1"))
        assert result["status"] == "fail"
        assert "skew" in result["detail"]
        assert "0.0.1" in result["detail"]

    def test_unversioned_service_is_never_claimed_as_ours(self, monkeypatch):
        """A pre-1.2.0 service reports no version; that is 'unknown', not 'match'."""
        payload = self._healthy()
        del payload["version"]
        del payload["package"]
        result = self._check(monkeypatch, payload)
        assert result["status"] == "warn"
        assert "does not report its version" in result["detail"]

    def test_divergent_spool_store_warns(self, monkeypatch):
        result = self._check(monkeypatch, self._healthy(spool_dir="/somewhere/else/spools"))
        assert result["status"] == "warn"
        assert any("not be visible to this CLI" in line for line in result["lines"])

    def test_unhealthy_spindle_fails(self, monkeypatch):
        """A spindle in a bad state is this install's problem."""
        result = self._check(monkeypatch, {"status": "starting", "version": "1.2.0", "running_spools": 0})
        assert result["status"] == "fail"

    def test_another_application_on_the_port_only_warns(self, monkeypatch):
        """A stdio-only user with something else on 8002 has a healthy install."""
        result = self._check(monkeypatch, {"status": "ok", "service": "grafana"})
        assert result["status"] == "warn"
        assert any("some other application" in line for line in result["lines"])

    def test_fetch_health_on_a_closed_port_reports_an_error(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        payload, err = spindle._fetch_health("127.0.0.1", port, timeout=0.5)
        assert payload is None
        assert err


class TestDoctorStorage:
    def test_writable_store_is_ok(self, tmp_path, monkeypatch):
        store = tmp_path / "spools"
        monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
        spindle._write_spool("abc12345", {"id": "abc12345", "status": "complete"})
        (store / ".supervisor.json").write_text('{"pid": 123}')
        result = spindle._doctor_storage_check()
        assert result["status"] == "ok"
        assert result["data"]["spools"] == 1
        assert not list(store.glob(".doctor-probe-*"))  # probe cleaned up

    def test_uncreatable_store_fails(self, tmp_path, monkeypatch):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setattr(spindle, "SPINDLE_DIR", blocker / "spools")
        result = spindle._doctor_storage_check()
        assert result["status"] == "fail"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_unwritable_store_fails(self, tmp_path, monkeypatch):
        store = tmp_path / "spools"
        store.mkdir()
        store.chmod(0o500)
        monkeypatch.setattr(spindle, "SPINDLE_DIR", store)
        try:
            result = spindle._doctor_storage_check()
        finally:
            store.chmod(0o700)
        assert result["status"] == "fail"
        assert "not writable" in result["detail"]


class TestDoctorHarnesses:
    def test_no_harness_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(spindle, "_probe_command", lambda cmd, timeout=5.0: (None, None))
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        result = spindle._doctor_harness_check()
        assert result["status"] == "fail"
        assert result["data"]["detected"] == {}

    def test_detected_harnesses_are_reported_with_paths(self, monkeypatch):
        def fake_probe(cmd, timeout=5.0):
            table = {"claude": ("/bin/claude", "2.1.216"), "codex": ("/bin/codex", "codex-cli 0.144.5")}
            return table.get(cmd, (None, None))

        monkeypatch.setattr(spindle, "_probe_command", fake_probe)
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {"mimo": {}})
        result = spindle._doctor_harness_check()
        assert result["status"] == "ok"
        assert set(result["data"]["detected"]) == {"claude-code", "codex"}
        assert result["data"]["profiles"] == ["mimo"]
        assert any("gemini: not found" in line for line in result["lines"])
        assert any("mimo" in line for line in result["lines"])

    def test_single_harness_warns(self, monkeypatch):
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: ("/bin/claude", "2.1") if cmd == "claude" else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        assert spindle._doctor_harness_check()["status"] == "warn"

    def test_harness_commands_cover_every_builtin(self):
        assert set(spindle.HARNESS_COMMANDS) == spindle.BUILTIN_HARNESSES


class TestDoctorShards:
    def test_missing_bwrap_explains_kimi_refusal(self, monkeypatch):
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: ("/usr/bin/git", "git version 2.43.0"),
        )
        monkeypatch.setattr(spindle.shutil, "which", lambda cmd: None)

        result = spindle._doctor_shard_check()

        assert result["status"] == "warn"
        assert any(
            "Kimi refuses every launch that requires containment" in line
            and "`full` without shard intent remains available" in line
            for line in result["lines"]
        )


class TestDoctorSmoke:
    """The smoke runs real agents, so these tests stub the spawn and drive the states."""

    def _seed(self, spool_id, status, result):
        spindle._write_spool(spool_id, {"id": spool_id, "status": status, "result": result, "prompt": "x"})

    def _no_finalize(self, monkeypatch):
        monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda spool_id: True)

    def test_token_match_is_ok(self, monkeypatch):
        self._no_finalize(monkeypatch)
        self._seed("smoke001", "complete", spindle.DOCTOR_SMOKE_TOKEN)
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smoke001")
        result = spindle._doctor_smoke_check("codex", timeout=5)
        assert result["status"] == "ok"
        assert result["data"]["spool_id"] == "smoke001"

    def test_missing_token_fails(self, monkeypatch):
        self._no_finalize(monkeypatch)
        self._seed("smoke002", "complete", "I cannot comply")
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smoke002")
        result = spindle._doctor_smoke_check("codex", timeout=5)
        assert result["status"] == "fail"
        assert "smoke token" in result["detail"]

    def test_errored_spool_fails(self, monkeypatch):
        self._no_finalize(monkeypatch)
        self._seed("smoke003", "error", "")
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smoke003")
        assert spindle._doctor_smoke_check("codex", timeout=5)["status"] == "fail"

    def test_spawn_error_fails(self, monkeypatch):
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "Error: codex CLI not found")
        result = spindle._doctor_smoke_check("codex", timeout=5)
        assert result["status"] == "fail"
        assert "codex CLI not found" in result["detail"]

    def test_hung_spool_is_dropped_and_failed(self, monkeypatch):
        self._no_finalize(monkeypatch)
        self._seed("smoke004", "running", "")
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smoke004")
        dropped = []
        monkeypatch.setattr(spindle, "_spin_drop_sync", lambda spool_id: dropped.append(spool_id) or "dropped")
        monkeypatch.setattr(spindle.time, "sleep", lambda seconds: None)
        result = spindle._doctor_smoke_check("codex", timeout=0)
        assert result["status"] == "fail"
        assert dropped == ["smoke004"]

    def test_smoke_is_limited_to_harnesses_with_a_read_only_tier(self):
        """kimi runs --yolo and gemini has no enforced read-only tier: no 'harmless' smoke."""
        assert spindle.DOCTOR_SMOKE_HARNESSES == ("claude-code", "codex")
        for harness in ("kimi", "gemini"):
            assert spindle._doctor_smoke_check(harness)["status"] == "skip"

    def test_smoke_working_dir_is_temporary(self, monkeypatch):
        """A smoke must not run inside the user's repo."""
        self._no_finalize(monkeypatch)
        self._seed("smoke005", "complete", spindle.DOCTOR_SMOKE_TOKEN)
        seen = {}

        def fake_spin(harness, working_dir, model, timeout):
            seen["working_dir"] = working_dir
            return "smoke005"

        monkeypatch.setattr(spindle, "_doctor_smoke_spin", fake_spin)
        spindle._doctor_smoke_check("codex", timeout=5)
        assert "spindle-doctor-" in seen["working_dir"]
        assert not Path(seen["working_dir"]).exists()  # cleaned up


class TestDoctorRun:
    def _stub_all(self, monkeypatch, service_status="ok"):
        monkeypatch.setattr(spindle, "_doctor_cli_check", lambda: spindle._doctor_result("cli", "ok", "cli"))
        monkeypatch.setattr(
            spindle,
            "_doctor_service_check",
            lambda host, port, timeout=2.0: spindle._doctor_result("service", service_status, "service"),
        )
        monkeypatch.setattr(spindle, "_doctor_storage_check", lambda: spindle._doctor_result("storage", "ok", "store"))
        monkeypatch.setattr(
            spindle,
            "_doctor_harness_check",
            lambda service_path=None, service_name=None, service_port=None: spindle._doctor_result(
                "harnesses", "ok", "harnesses"
            ),
        )
        monkeypatch.setattr(spindle, "_doctor_shard_check", lambda: spindle._doctor_result("shards", "ok", "shards"))

    def test_clean_run_is_ok_and_offers_the_smoke(self, monkeypatch):
        self._stub_all(monkeypatch)
        report = spindle._doctor_run()
        assert report["ok"] is True
        assert report["version"] == spindle.__version__
        smoke = [c for c in report["checks"] if c["name"] == "smoke"][0]
        assert smoke["status"] == "skip"
        assert "--smoke" in smoke["detail"]

    def test_a_failing_check_fails_the_report(self, monkeypatch):
        self._stub_all(monkeypatch, service_status="fail")
        report = spindle._doctor_run()
        assert report["ok"] is False
        assert report["failed"] == ["service"]

    def test_warnings_do_not_fail_the_report(self, monkeypatch):
        self._stub_all(monkeypatch, service_status="warn")
        assert spindle._doctor_run()["ok"] is True

    def test_smoke_runs_per_requested_harness(self, monkeypatch):
        self._stub_all(monkeypatch)
        ran = []

        def fake_smoke(harness, timeout=240, model=None):
            ran.append(harness)
            return spindle._doctor_result(f"smoke:{harness}", "ok", "smoked")

        monkeypatch.setattr(spindle, "_doctor_smoke_check", fake_smoke)
        report = spindle._doctor_run(smoke=True, smoke_harnesses=["codex"])
        assert ran == ["codex"]
        assert [c["name"] for c in report["checks"] if c["name"].startswith("smoke")] == ["smoke:codex"]

    def test_render_is_one_line_per_status(self, monkeypatch):
        self._stub_all(monkeypatch, service_status="fail")
        text = spindle._doctor_render(spindle._doctor_run())
        lines = text.splitlines()
        assert lines[0].startswith("spindle doctor")
        assert any(line.startswith("fail: service") for line in lines)
        assert lines[-1].startswith("FAILED: service")
        assert "|" not in text  # plain text, no table columns


class TestServiceFileGeneration:
    def test_unit_carries_marker_port_and_home(self):
        unit = spindle._systemd_unit_text("/opt/venv/bin/spindle", 8042, home="/tmp/store", name="spindle-release")
        assert spindle.SERVICE_MARKER in unit
        assert 'ExecStart="/opt/venv/bin/spindle" serve --http --port 8042' in unit
        assert "Environment=SPINDLE_PORT=8042" in unit
        assert 'Environment="SPINDLE_HOME=/tmp/store"' in unit
        assert "spindle-release" in unit

    def test_unit_omits_home_when_not_set(self):
        unit = spindle._systemd_unit_text("/opt/venv/bin/spindle", 8002)
        assert "SPINDLE_HOME" not in unit

    def test_plist_carries_marker_and_port(self):
        plist = spindle._launchd_plist_text("com.spindle.server", "/usr/local/bin/spindle", 8042, home="/tmp/store")
        assert spindle.SERVICE_MARKER in plist
        assert "<string>8042</string>" in plist
        assert "SPINDLE_HOME" in plist

    def test_marked_service_file_is_recognized(self, tmp_path):
        missing = tmp_path / "none.service"
        assert spindle._service_file_is_marked(missing) is False

        ours = tmp_path / "ours.service"
        ours.write_text(spindle._systemd_unit_text("/bin/spindle", 8002))
        assert spindle._service_file_is_marked(ours) is True

        theirs = tmp_path / "theirs.service"
        theirs.write_text("[Unit]\nDescription=Hand-written spindle\n")
        assert spindle._service_file_is_marked(theirs) is False

    def test_marker_must_lead_a_header_line(self, tmp_path):
        """Substring matching let a file disclaim ownership and be claimed anyway."""
        disclaimed = tmp_path / "disclaimed.service"
        disclaimed.write_text(f"# NOT {spindle.SERVICE_MARKER} - hand-written, do not touch\n[Unit]\n")
        assert spindle._service_file_is_marked(disclaimed) is False

        buried = tmp_path / "buried.service"
        buried.write_text("[Unit]\n" + "\n" * 40 + f"# {spindle.SERVICE_MARKER}\n")
        assert spindle._service_file_is_marked(buried) is False

    def test_plist_marker_inside_its_xml_comment_counts(self, tmp_path):
        plist = tmp_path / "com.spindle.server.plist"
        plist.write_text(spindle._launchd_plist_text("com.spindle.server", "/bin/spindle", 8002))
        assert spindle._service_file_is_marked(plist) is True

    def test_service_path_env_dedupes_and_prunes(self, tmp_path):
        real = tmp_path / "bin"
        real.mkdir()
        gone = tmp_path / "vanished"
        raw = os.pathsep.join([str(real), str(gone), str(real), ""])
        resolved = spindle._service_path_env(raw)
        assert resolved.split(os.pathsep) == [str(real)]

    def test_service_path_env_never_empty(self, tmp_path):
        assert spindle._service_path_env(str(tmp_path / "nope"))


class TestPortResolution:
    def test_default_port(self, monkeypatch):
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        assert spindle._default_port() == 8002

    def test_port_from_env(self, monkeypatch):
        monkeypatch.setenv("SPINDLE_PORT", "8042")
        assert spindle._default_port() == 8042

    def test_bad_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("SPINDLE_PORT", "not-a-port")
        assert spindle._default_port() == 8002


class TestHarnessSelectionSharedByCliAndTool:
    """`spindle spin --harness X` must accept exactly what the spin tool accepts."""

    @pytest.fixture
    def profiles_root(self, tmp_path, monkeypatch):
        root = tmp_path / "profiles"
        root.mkdir()
        monkeypatch.setattr(spindle, "SPINDLE_PROFILES_DIR", root)
        monkeypatch.chdir(tmp_path)
        return root

    def test_builtin_passes_through(self):
        harness, model, env, extra, name = spindle._resolve_harness_selection("CODEX", "gpt-5.6-sol", None)
        assert harness == "codex"
        assert model == "gpt-5.6-sol"
        assert (extra, name) == (None, None)

    def test_no_harness_is_none(self):
        assert spindle._resolve_harness_selection(None, None, None)[0] is None

    def test_unknown_harness_raises(self, profiles_root):
        with pytest.raises(ValueError) as exc:
            spindle._resolve_harness_selection("nope", None, None)
        assert "Unknown harness or profile" in str(exc.value)

    def test_profile_resolves_to_claude_code_with_its_env(self, profiles_root):
        _make_profile(
            profiles_root,
            "alt",
            {"base_url": "https://alt.example/v1", "api_key": "k", "model": "alt-default", "extra_args": ["--flag"]},
        )
        harness, model, env, extra, name = spindle._resolve_harness_selection("alt", None, None)
        assert harness == "claude-code"
        assert name == "alt"
        assert model == "alt-default"
        assert extra == ["--flag"]
        assert env["ANTHROPIC_BASE_URL"] == "https://alt.example/v1"

    def test_cli_spin_rejects_unknown_harness(self, profiles_root, capsys):
        """Regression: the CLI used to run plain Claude Code for any unknown --harness."""
        argv = ["spindle", "spin", "hello", "--harness", "nope", "--working-dir", str(profiles_root)]
        with patch.object(spindle.sys, "argv", argv):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        assert exc.value.code == 1
        assert "Unknown harness or profile" in json.loads(capsys.readouterr().out)["error"]

    def test_cli_spin_routes_a_profile_through_the_profile_path(self, profiles_root, capsys):
        """Regression: a lodged profile name used to fall through to plain Claude Code."""
        _make_profile(profiles_root, "alt", {"base_url": "https://alt.example/v1", "model": "alt-default"})
        captured = {}

        def fake_spin_sync(**kwargs):
            captured.update(kwargs)
            return "abc12345"

        with patch.object(spindle, "_spin_sync", fake_spin_sync):
            argv = ["spindle", "spin", "hello", "--harness", "alt", "--working-dir", str(profiles_root)]
            with patch.object(spindle.sys, "argv", argv):
                with pytest.raises(SystemExit) as exc:
                    spindle.main()
        assert exc.value.code == 0
        assert captured["profile"] == "alt"
        assert captured["model"] == "alt-default"
        assert captured["spawn_env"]["ANTHROPIC_BASE_URL"] == "https://alt.example/v1"
        assert json.loads(capsys.readouterr().out)["spool_id"] == "abc12345"


class TestCliDoctorCommand:
    def test_doctor_exit_code_and_json(self, monkeypatch, capsys):
        report = {"ok": False, "version": spindle.__version__, "failed": ["service"], "checks": [], "endpoint": "x"}
        monkeypatch.setattr(spindle, "_doctor_run", lambda **kwargs: report)
        with patch.object(spindle.sys, "argv", ["spindle", "doctor", "--json"]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["failed"] == ["service"]

    def test_doctor_passes_flags_through(self, monkeypatch, capsys):
        seen = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "version": spindle.__version__, "failed": [], "checks": [], "endpoint": "x"}

        monkeypatch.setattr(spindle, "_doctor_run", fake_run)
        argv = ["spindle", "doctor", "--smoke", "--harness", "codex,claude-code", "--port", "8042"]
        with patch.object(spindle.sys, "argv", argv):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        assert exc.value.code == 0
        assert seen["smoke"] is True
        assert seen["smoke_harnesses"] == ["codex", "claude-code"]
        assert seen["port"] == 8042
        capsys.readouterr()

    def test_status_flags_a_foreign_service(self, monkeypatch, capsys):
        """`spindle status` must not present another install's health as its own."""
        payload = {"status": "healthy", "version": "9.9.9", "package": "/opt/other/spindle/__init__.py"}
        monkeypatch.setattr(spindle, "_fetch_health", lambda host, port, timeout=2.0: (payload, None))
        with patch.object(spindle.sys, "argv", ["spindle", "status"]):
            with pytest.raises(SystemExit):
                spindle.main()
        captured = capsys.readouterr()
        assert json.loads(captured.out)["version"] == "9.9.9"
        assert "different install" in captured.err

    def test_status_reports_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(spindle, "_fetch_health", lambda host, port, timeout=2.0: (None, "refused"))
        with patch.object(spindle.sys, "argv", ["spindle", "status"]):
            with pytest.raises(SystemExit):
                spindle.main()
        assert "Not running" in capsys.readouterr().out


class TestDoctorCliIdentity:
    """ "Is the `spindle` on PATH the one I'm running?" - the two-installs question."""

    def test_console_script_mismatch_warns(self, tmp_path, monkeypatch):
        theirs = tmp_path / "theirs" / "spindle"
        theirs.parent.mkdir()
        theirs.write_text("#!/usr/bin/python3\n")
        ours = tmp_path / "ours" / "spindle"
        ours.parent.mkdir()
        ours.write_text("#!/usr/bin/python3\n")
        monkeypatch.setattr(spindle.shutil, "which", lambda cmd: str(theirs))
        result = spindle._doctor_cli_check(argv0=str(ours))
        assert result["status"] == "warn"
        assert any("DIFFERENT install" in line for line in result["lines"])

    def test_same_console_script_is_ok(self, tmp_path, monkeypatch):
        script = tmp_path / "spindle"
        script.write_text("#!/usr/bin/python3\n")
        monkeypatch.setattr(spindle.shutil, "which", lambda cmd: str(script))
        assert spindle._doctor_cli_check(argv0=str(script))["status"] == "ok"

    def test_module_invocation_compares_interpreter_dirs(self, tmp_path, monkeypatch):
        """A venv python resolves to its base interpreter, so compare bin dirs."""
        script = tmp_path / "spindle"
        script.write_text("#!/opt/other-venv/bin/python3\n")
        monkeypatch.setattr(spindle.shutil, "which", lambda cmd: str(script))
        result = spindle._doctor_cli_check(argv0=str(tmp_path / "spindle" / "__main__.py"))
        assert result["status"] == "warn"

        script.write_text(f"#!{Path(sys.executable).parent / 'python3'}\n")
        assert spindle._doctor_cli_check(argv0="/some/pkg/__main__.py")["status"] == "ok"

    def test_missing_console_script_warns(self, monkeypatch):
        monkeypatch.setattr(spindle.shutil, "which", lambda cmd: None)
        result = spindle._doctor_cli_check()
        assert result["status"] == "warn"
        assert result["data"]["console_script"] is None
        assert result["data"]["version"] == spindle.__version__


def test_cli_import_emits_no_third_party_deprecation_noise():
    """Every CLI command used to print an authlib deprecation warning first.

    Import spindle in a clean subprocess with warnings enabled and assert stderr
    stays quiet: the filter has to be registered before fastmcp is imported, so a
    reordering of the imports would silently bring the noise back.
    """
    proc = subprocess.run(
        [sys.executable, "-W", "default", "-c", "import spindle"],
        capture_output=True,
        text=True,
        cwd=str(Path(spindle.__file__).parent.parent),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "authlib" not in proc.stderr


class TestImportNoiseSuppression:
    """authlib's import-time deprecation used to precede every command's output."""

    def test_authlib_message_is_dropped(self, monkeypatch):
        seen = []
        monkeypatch.setattr(spindle, "_real_showwarning", lambda *args: seen.append(args))
        spindle._showwarning_without_authlib_noise(
            "authlib.jose module is deprecated, please use joserfc instead.",
            DeprecationWarning,
            "authlib/jose.py",
            10,
        )
        assert seen == []

    def test_other_warnings_still_surface(self, monkeypatch):
        seen = []
        monkeypatch.setattr(spindle, "_real_showwarning", lambda *args: seen.append(args))
        spindle._showwarning_without_authlib_noise("something you should know", UserWarning, "x.py", 1)
        assert len(seen) == 1

    def test_warnings_machinery_is_handed_back(self):
        """The shim must only be installed for the duration of the fastmcp import."""
        import warnings as warnings_module

        assert warnings_module.showwarning is not spindle._showwarning_without_authlib_noise


class TestCodexProbeLogging:
    """The blocked-write outcome is the probe PASSING; it must not look like a failure."""

    def _proc(self, stdout, returncode=0):
        proc = MagicMock()
        proc.stdout = stdout
        proc.returncode = returncode
        return proc

    def test_blocked_write_does_not_warn(self, caplog):
        """A missing target means the sandbox held. Warning about it alarmed every first run."""
        with patch("spindle.subprocess.run", return_value=self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")):
            with caplog.at_level(logging.WARNING, logger="spindle"):
                assert spindle._codex_sandbox_probe("/fake/codex") is True
        assert caplog.records == []

    def test_a_real_stat_error_still_warns(self, caplog):
        """Anything other than "the file isn't there" still warns — and fails closed.

        An unreadable probe target cannot prove the sandbox held, so the probe
        must not report an enforcing sandbox (master's fail-closed semantics).
        """

        def boom(path):
            raise PermissionError("denied")

        with patch("spindle.subprocess.run", return_value=self._proc(f"{spindle._CODEX_SANDBOX_PROBE_MARKER}\n")):
            with patch("spindle.os.stat", side_effect=boom):
                with caplog.at_level(logging.WARNING, logger="spindle"):
                    assert spindle._codex_sandbox_probe("/fake/codex") is False
        assert any("could not stat target" in r.message for r in caplog.records)


def test_harness_check_says_presence_is_not_authentication(monkeypatch):
    """Installed-but-not-logged-in is the likeliest fresh-machine failure."""
    monkeypatch.setattr(
        spindle,
        "_probe_command",
        lambda cmd, timeout=5.0: (f"/bin/{cmd}", "1.0") if cmd != "kimi-cli" else (None, None),
    )
    monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
    result = spindle._doctor_harness_check()
    assert any("not the same as logged in" in line for line in result["lines"])
    assert any("--smoke" in line for line in result["lines"])


# --- 1.1.0 unit that install-service must still recognize as its own ---------
_LEGACY_UNIT = """\
[Unit]
Description=Spindle MCP Server
After=network.target

[Service]
Type=simple
ExecStart=/home/u/.local/bin/spindle serve --http
Restart=on-failure
RestartSec=5
Environment=PATH=%h/.local/bin:/usr/bin

[Install]
WantedBy=default.target
"""

# A unit a person wrote for the same service. Superficially similar, and must
# never be overwritten - it carries settings spindle knows nothing about.
_HAND_WRITTEN_UNIT = """\
[Unit]
Description=Spindle MCP Server (HTTP)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/u/projects/spindle
ExecStart=/home/u/.pyenv/versions/3.12.0/bin/spindle serve --http --port 8002
Restart=on-failure
Environment=PATH=/home/u/.nvm/versions/node/v20.20.2/bin:/usr/bin
EnvironmentFile=-/home/u/.spindle/env

[Install]
WantedBy=default.target
"""

_LEGACY_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.spindle.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/spindle</string>
        <string>serve</string>
        <string>--http</string>
    </array>
</dict>
</plist>
"""


class TestUnmarkedServiceFilesAreBackedUpNotGuessedAt:
    """Ownership of an unmarked file cannot be inferred, so it is never destroyed.

    Fingerprinting the shape a previous spindle generated was tried and is wrong
    in both directions: that shape is also what users copy out of examples/ and
    then edit (so their customized unit reads as ours and gets clobbered), and a
    1.1.0 user who edited the fingerprinted PATH line - the single most likely
    edit - reads as foreign with no way forward. Backing the file up instead is
    correct for every case without having to tell them apart.
    """

    def test_legacy_and_hand_written_are_both_unmarked(self, tmp_path):
        for name, text in (("legacy", _LEGACY_UNIT), ("hand", _HAND_WRITTEN_UNIT), ("plist", _LEGACY_PLIST)):
            path = tmp_path / f"{name}.service"
            path.write_text(text)
            assert spindle._service_file_is_marked(path) is False, name

    def test_backup_preserves_the_original(self, tmp_path):
        unit = tmp_path / "spindle.service"
        unit.write_text(_HAND_WRITTEN_UNIT)
        backup = spindle._backup_service_file(unit)
        assert backup is not None
        assert backup.read_text() == _HAND_WRITTEN_UNIT
        assert backup.name.startswith("spindle.service.bak-")

    def test_backups_do_not_overwrite_each_other(self, tmp_path):
        unit = tmp_path / "spindle.service"
        unit.write_text("first")
        first = spindle._backup_service_file(unit)
        unit.write_text("second")
        second = spindle._backup_service_file(unit)
        assert first != second
        assert first.read_text() == "first"
        assert second.read_text() == "second"

    def test_current_unit_round_trips(self, tmp_path):
        unit = tmp_path / "spindle.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 8002))
        assert spindle._service_file_is_marked(unit) is True

    def test_service_name_cannot_escape_the_unit_directory(self):
        assert spindle._valid_service_name("spindle") is True
        assert spindle._valid_service_name("spindle-release_2.1") is True
        assert spindle._valid_service_name("../../evil") is False
        assert spindle._valid_service_name("a/b") is False
        assert spindle._valid_service_name("") is False
        assert spindle._valid_service_name("-leading") is False


class TestSystemdQuoting:
    """systemd splits unquoted Environment= on whitespace and expands %."""

    def test_path_with_a_space_survives(self):
        unit = spindle._systemd_unit_text("/bin/spindle", 8002, path_env="/usr/bin")
        path_line = [ln for ln in unit.splitlines() if ln.startswith("Environment=") and "PATH=" in ln][0]
        assert path_line.startswith('Environment="PATH=')
        assert path_line.endswith('"')

    def test_percent_is_escaped_everywhere(self, tmp_path):
        """An unescaped % makes systemd drop the whole assignment, not just the value."""
        weird = tmp_path / "100%dir"
        weird.mkdir()
        unit = spindle._systemd_unit_text("/opt/100%bin/spindle", 8002, home="/srv/100%home", path_env=str(weird))
        for line in unit.splitlines():
            if line.startswith(("Environment=", "ExecStart=")):
                # every literal % must be doubled
                assert "%%" in line or "%" not in line, line

    def test_home_with_a_space_is_quoted(self):
        unit = spindle._systemd_unit_text("/bin/spindle", 8002, home="/mnt/c/Users/My Name/.spindle")
        home_line = [ln for ln in unit.splitlines() if "SPINDLE_HOME" in ln][0]
        assert home_line == 'Environment="SPINDLE_HOME=/mnt/c/Users/My Name/.spindle"'

    def test_execstart_path_with_a_space_is_quoted(self):
        unit = spindle._systemd_unit_text("/opt/my apps/bin/spindle", 8002)
        exec_line = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")][0]
        assert exec_line == 'ExecStart="/opt/my apps/bin/spindle" serve --http --port 8002'

    def test_relative_path_entries_are_dropped(self, tmp_path, monkeypatch):
        """They resolve for whoever ran install-service and for nobody else."""
        real = tmp_path / "bin"
        real.mkdir()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "node_modules").mkdir()
        resolved = spindle._service_path_env(os.pathsep.join(["./node_modules", str(real)]))
        assert resolved.split(os.pathsep) == [str(real)]


class TestLaunchdEscaping:
    """A plist that does not parse is a service that silently never starts."""

    def test_ampersand_in_a_path_still_parses(self):
        import xml.etree.ElementTree as ET

        plist = spindle._launchd_plist_text(
            "com.spindle.server",
            "/Users/A&B/bin/spindle",
            8002,
            home="/Users/A&B/.spindle",
            path_env="/usr/bin",
        )
        ET.fromstring(plist)  # raises if malformed
        assert "A&amp;B" in plist

    def test_angle_brackets_are_escaped(self):
        import xml.etree.ElementTree as ET

        plist = spindle._launchd_plist_text("com.spindle.server", "/opt/<odd>/spindle", 8002, path_env="/usr/bin")
        ET.fromstring(plist)


class TestServiceStalePath:
    """A unit's PATH is baked at install time and rots; the failure lands much later."""

    def _detected(self, monkeypatch):
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: (f"/new/bin/{cmd}", "1.0") if cmd in ("claude", "codex") else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})

    def test_harness_unreachable_from_the_service_warns(self, monkeypatch, tmp_path):
        """Found from this shell, unfindable from the service that has to spawn it."""
        self._detected(monkeypatch)
        result = spindle._doctor_harness_check(service_path=str(tmp_path))
        assert result["status"] == "warn"
        assert set(result["data"]["unreachable_from_service"]) == {"claude-code", "codex"}
        assert any("cannot find" in line for line in result["lines"])

    def test_no_warning_when_the_service_can_see_them(self, monkeypatch, tmp_path):
        self._detected(monkeypatch)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for cmd in ("claude", "codex"):
            exe = bin_dir / cmd
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
        result = spindle._doctor_harness_check(service_path=str(bin_dir))
        assert result["data"]["unreachable_from_service"] == []
        assert result["status"] == "ok"

    def test_no_service_means_no_path_claim(self, monkeypatch):
        self._detected(monkeypatch)
        result = spindle._doctor_harness_check(service_path=None)
        assert result["data"]["unreachable_from_service"] == []

    def test_health_reports_the_path_the_service_will_search(self):
        payload = json.loads(asyncio.run(spindle.health_check(MagicMock())).body)
        assert payload["path"] == os.environ.get("PATH", "")

    def test_doctor_only_trusts_the_path_of_a_matching_install(self, monkeypatch):
        """A foreign service's PATH says nothing about whether ours can spawn."""
        seen = {}

        def fake_harness_check(service_path=None, service_name=None, service_port=None):
            seen["service_path"] = service_path
            return spindle._doctor_result("harnesses", "ok", "harnesses")

        monkeypatch.setattr(spindle, "_doctor_cli_check", lambda: spindle._doctor_result("cli", "ok", "cli"))
        monkeypatch.setattr(spindle, "_doctor_storage_check", lambda: spindle._doctor_result("storage", "ok", "s"))
        monkeypatch.setattr(spindle, "_doctor_shard_check", lambda: spindle._doctor_result("shards", "ok", "s"))
        monkeypatch.setattr(spindle, "_doctor_harness_check", fake_harness_check)

        foreign = spindle._doctor_result(
            "service", "fail", "different install", health={"path": "/foreign/bin"}, same_install=False
        )
        monkeypatch.setattr(spindle, "_doctor_service_check", lambda host, port, timeout=2.0: foreign)
        spindle._doctor_run()
        assert seen["service_path"] is None

        ours = spindle._doctor_result("service", "ok", "same install", health={"path": "/our/bin"}, same_install=True)
        monkeypatch.setattr(spindle, "_doctor_service_check", lambda host, port, timeout=2.0: ours)
        spindle._doctor_run()
        assert seen["service_path"] == "/our/bin"


class TestReloadStoreMismatch:
    """`reload` drains this process's store; that can be the wrong one."""

    def test_divergent_store_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(
            spindle,
            "_fetch_health",
            lambda host, port, timeout=2.0: ({"spool_dir": "/elsewhere/spools"}, None),
        )
        warning = spindle._reload_warn_on_store_mismatch("127.0.0.1", 8042)
        assert warning and "cannot see that service's" in warning
        assert "/elsewhere/spools" in capsys.readouterr().err

    def test_matching_store_is_silent(self, monkeypatch, capsys):
        monkeypatch.setattr(
            spindle,
            "_fetch_health",
            lambda host, port, timeout=2.0: ({"spool_dir": str(spindle.SPINDLE_DIR)}, None),
        )
        assert spindle._reload_warn_on_store_mismatch("127.0.0.1", 8042) is None
        assert capsys.readouterr().err == ""

    def test_no_service_is_silent(self, monkeypatch):
        monkeypatch.setattr(spindle, "_fetch_health", lambda host, port, timeout=2.0: (None, "refused"))
        assert spindle._reload_warn_on_store_mismatch("127.0.0.1", 8042) is None


def test_launchd_log_is_named_for_the_service():
    """Two installs must not interleave output in one ~/.spindle/spindle.log."""
    plist = spindle._launchd_plist_text("com.spindle-release.server", "/bin/spindle", 8042, name="spindle-release")
    assert "spindle-release.log" in plist


class TestShippedExamplesMatchTheGenerator:
    """The examples are a manual-install path, so they must not drift from the tool."""

    def _examples(self):
        root = Path(spindle.__file__).parent.parent / "examples"
        if not root.exists():
            pytest.skip("no examples/ (running against an installed wheel)")
        return root

    def test_example_unit_is_recognized_as_ours(self):
        """Copied in unedited, it stays replaceable; the header says to drop the
        marker if you customize it, which is what makes ownership knowable."""
        unit = self._examples() / "spindle.service"
        assert spindle._service_file_is_marked(unit) is True

    def test_example_plist_is_recognized_as_ours(self):
        plist = self._examples() / "com.spindle.server.plist"
        assert spindle._service_file_is_marked(plist) is True

    def test_example_unit_quotes_its_environment_values(self):
        """The example is copied verbatim; an unquoted PATH would truncate at a space."""
        text = (self._examples() / "spindle.service").read_text()
        for line in text.splitlines():
            if line.startswith("Environment=") and ("PATH=" in line or "SPINDLE_HOME=" in line):
                assert line.startswith(('Environment="', "#")), line

    def test_example_unit_carries_what_the_generator_emits(self):
        text = (self._examples() / "spindle.service").read_text()
        assert spindle.SERVICE_MARKER in text
        assert "serve --http --port" in text  # not the portless 1.1.0 ExecStart
        assert "Environment=SPINDLE_PORT=" in text
        assert "SPINDLE_HOME" in text

    def test_example_plist_carries_what_the_generator_emits(self):
        text = (self._examples() / "com.spindle.server.plist").read_text()
        assert spindle.SERVICE_MARKER in text
        assert "<key>SPINDLE_PORT</key>" in text
        assert "<key>PATH</key>" in text

    def test_example_plist_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        ET.parse(self._examples() / "com.spindle.server.plist")


class TestStalePathSurvivesADivergentStore:
    """The two-install recipe in the README is exactly where this used to switch itself off."""

    def _service(self, monkeypatch, spool_dir):
        payload = {
            "status": "healthy",
            "uptime_seconds": 5,
            "running_spools": 0,
            "max_concurrent": 15,
            "version": spindle.__version__,
            "package": str(Path(spindle.__file__).resolve()),
            "spool_dir": spool_dir,
            "path": "/nonexistent/bin",
        }
        monkeypatch.setattr(spindle, "_fetch_health", lambda host, port, timeout=2.0: (payload, None))
        return spindle._doctor_service_check("127.0.0.1", 8042)

    def test_divergent_store_warns_but_identity_still_holds(self, monkeypatch):
        result = self._service(monkeypatch, "/elsewhere/spools")
        assert result["status"] == "warn"
        assert result["data"]["same_install"] is True

    def test_matching_store_is_ok_and_identified(self, monkeypatch):
        result = self._service(monkeypatch, str(spindle.SPINDLE_DIR))
        assert result["status"] == "ok"
        assert result["data"]["same_install"] is True

    def test_stale_path_is_still_reported_when_the_store_differs(self, monkeypatch):
        """`doctor --port 8042` without that service's SPINDLE_HOME is the documented flow."""
        self._service(monkeypatch, "/elsewhere/spools")
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: (f"/real/bin/{cmd}", "1.0") if cmd == "claude" else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        monkeypatch.setattr(spindle, "_doctor_cli_check", lambda: spindle._doctor_result("cli", "ok", "cli"))
        monkeypatch.setattr(spindle, "_doctor_storage_check", lambda: spindle._doctor_result("storage", "ok", "s"))
        monkeypatch.setattr(spindle, "_doctor_shard_check", lambda: spindle._doctor_result("shards", "ok", "s"))
        report = spindle._doctor_run(port=8042)
        harnesses = [c for c in report["checks"] if c["name"] == "harnesses"][0]
        assert harnesses["data"]["unreachable_from_service"] == ["claude-code"]

    def test_remedy_names_the_service_it_is_about(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: (f"/real/bin/{cmd}", "1.0") if cmd == "claude" else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        result = spindle._doctor_harness_check(service_path=str(tmp_path), service_name="spindle-b")
        remedy = [line for line in result["lines"] if "install-service" in line][0]
        assert "--name spindle-b" in remedy
        assert "reload --name spindle-b" in remedy


class TestReloadFindsTheRightPort:
    """`reload --name X` used to probe the default port, i.e. a different service."""

    def _unit(self, tmp_path, monkeypatch, text):
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "spindle-b.service").write_text(text)
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        # Not the developer's: _systemd_user_dir prefers XDG_CONFIG_HOME, so an
        # ambient one would send this lookup outside tmp_path.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def test_port_read_from_a_quoted_environment_line(self, tmp_path, monkeypatch):
        self._unit(tmp_path, monkeypatch, spindle._systemd_unit_text("/bin/spindle", 8042, name="spindle-b"))
        assert spindle._port_from_unit("spindle-b") == 8042

    def test_port_read_from_execstart_when_env_is_absent(self, tmp_path, monkeypatch):
        self._unit(tmp_path, monkeypatch, "[Service]\nExecStart=/bin/spindle serve --http --port 8055\n")
        assert spindle._port_from_unit("spindle-b") == 8055

    def test_missing_unit_is_none(self, tmp_path, monkeypatch):
        self._unit(tmp_path, monkeypatch, "[Service]\n")
        assert spindle._port_from_unit("nope") is None


class TestSmokeHarnessSelection:
    def test_unknown_harness_fails_rather_than_skipping(self, monkeypatch):
        """`--harness codxe` used to exit 0 having smoked nothing."""
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        result = spindle._doctor_smoke_check("codxe")
        assert result["status"] == "fail"
        assert "unknown harness" in result["detail"]

    def test_harness_names_are_case_insensitive(self, monkeypatch):
        seeded = {"id": "smokeX", "status": "complete", "result": spindle.DOCTOR_SMOKE_TOKEN, "prompt": "x"}
        spindle._write_spool("smokeX", seeded)
        monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda spool_id: True)
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smokeX")
        assert spindle._doctor_smoke_check("CODEX", timeout=5)["status"] == "ok"

    def test_known_harness_without_a_readonly_tier_still_skips(self, monkeypatch):
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        assert spindle._doctor_smoke_check("kimi")["status"] == "skip"

    def test_a_full_queue_is_a_skip_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(
            spindle, "_doctor_smoke_spin", lambda *a, **k: "Error: Max 15 concurrent spools reached, try later"
        )
        assert spindle._doctor_smoke_check("codex")["status"] == "skip"

    def test_zero_timeout_does_not_spawn_and_abandon(self, monkeypatch):
        """timeout=0 means 'no timeout' to spin, while the poll loop exits at once."""
        spindle._write_spool("smokeY", {"id": "smokeY", "status": "complete", "result": spindle.DOCTOR_SMOKE_TOKEN})
        seen = {}

        def fake_spin(harness, working_dir, model, timeout):
            seen["timeout"] = timeout
            return "smokeY"

        monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda spool_id: True)
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", fake_spin)
        result = spindle._doctor_smoke_check("codex", timeout=0)
        assert seen["timeout"] >= 1
        assert result["status"] == "ok"

    def test_a_hung_smoke_keeps_its_working_dir(self, monkeypatch):
        """rmtree under a process that may still be alive is worse than leaving it."""
        spindle._write_spool("smokeZ", {"id": "smokeZ", "status": "running", "result": ""})
        monkeypatch.setattr(spindle, "_check_and_finalize_spool", lambda spool_id: True)
        monkeypatch.setattr(spindle, "_doctor_smoke_spin", lambda *a, **k: "smokeZ")
        monkeypatch.setattr(spindle, "_spin_drop_sync", lambda spool_id: "dropped")
        monkeypatch.setattr(spindle.time, "sleep", lambda seconds: None)
        result = spindle._doctor_smoke_check("codex", timeout=1)
        assert result["status"] == "fail"
        assert "kill signal" in result["detail"]
        import shutil as _shutil

        kept = [line for line in result["lines"] if "working dir" in line][0]
        working_dir = kept.split(": ", 1)[1]
        assert Path(working_dir).exists()
        _shutil.rmtree(working_dir, ignore_errors=True)


class TestScriptInterpreterParsing:
    def test_env_shebang_resolves_the_interpreter_through_path(self, tmp_path):
        """A bare name compares as a path with parent ".", which matches nothing."""
        script = tmp_path / "spindle"
        script.write_text("#!/usr/bin/env python3\n")
        resolved = spindle._script_interpreter(str(script))
        assert resolved == (shutil.which("python3") or "python3")
        assert Path(resolved).is_absolute()

    def test_env_shebang_skips_options_and_assignments(self, tmp_path):
        script = tmp_path / "spindle"
        script.write_text("#!/usr/bin/env -u PYTHONPATH FOO=bar python3\n")
        assert spindle._script_interpreter(str(script)) == (shutil.which("python3") or "python3")

    def test_plain_shebang(self, tmp_path):
        script = tmp_path / "spindle"
        script.write_text("#!/opt/venv/bin/python\n")
        assert spindle._script_interpreter(str(script)) == "/opt/venv/bin/python"

    def test_no_shebang(self, tmp_path):
        script = tmp_path / "spindle"
        script.write_text("not a script\n")
        assert spindle._script_interpreter(str(script)) is None


class TestRegeneratingAUnitDoesNotLoseSettings:
    """--force rebuilds a unit from argv; what it doesn't know, it must not drop."""

    @pytest.fixture
    def unit_dir(self, tmp_path, monkeypatch):
        d = tmp_path / ".config" / "systemd" / "user"
        d.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return d

    def test_spool_store_is_read_back_out_of_the_unit(self, unit_dir):
        """The store is the setting whose silent loss merges two installs' spools."""
        unit_dir.joinpath("svc.service").write_text(
            spindle._systemd_unit_text("/bin/spindle", 8065, home="/srv/store", name="svc")
        )
        assert spindle._env_from_unit("svc", "SPINDLE_HOME") == "/srv/store"

    def test_store_with_a_space_round_trips(self, unit_dir):
        home = "/mnt/c/Users/My Name/.spindle"
        unit_dir.joinpath("svc.service").write_text(
            spindle._systemd_unit_text("/bin/spindle", 8065, home=home, name="svc")
        )
        assert spindle._env_from_unit("svc", "SPINDLE_HOME") == home

    def test_store_with_a_percent_round_trips(self, unit_dir):
        home = "/srv/100%store"
        unit_dir.joinpath("svc.service").write_text(
            spindle._systemd_unit_text("/bin/spindle", 8065, home=home, name="svc")
        )
        assert spindle._env_from_unit("svc", "SPINDLE_HOME") == home

    def test_absent_store_reads_as_none(self, unit_dir):
        unit_dir.joinpath("svc.service").write_text(spindle._systemd_unit_text("/bin/spindle", 8065, name="svc"))
        assert spindle._env_from_unit("svc", "SPINDLE_HOME") is None

    def test_execstart_port_wins_over_a_stale_environment_line(self, unit_dir):
        """Whichever the service actually binds is the truthful one."""
        unit_dir.joinpath("svc.service").write_text(
            '[Service]\nEnvironment="SPINDLE_PORT=8002"\nExecStart=/bin/spindle serve --http --port 9000\n'
        )
        assert spindle._port_from_unit("svc") == 9000

    def test_unit_records_its_own_service_name(self, unit_dir):
        unit_dir.joinpath("svc.service").write_text(
            spindle._systemd_unit_text("/bin/spindle", 8065, name="spindle-release")
        )
        assert spindle._env_from_unit("svc", "SPINDLE_SERVICE_NAME") == "spindle-release"


class TestSystemdBackslashQuoting:
    """A value ending in a backslash escapes the closing quote and unterminates it."""

    def test_trailing_backslash_is_doubled(self):
        unit = spindle._systemd_unit_text("/bin/spindle", 8002, home="/srv/tail\\")
        home_line = [ln for ln in unit.splitlines() if "SPINDLE_HOME" in ln][0]
        assert home_line == 'Environment="SPINDLE_HOME=/srv/tail\\\\"'
        # the closing quote is a real terminator, not an escaped one
        assert home_line.count('"') == 2

    def test_backslash_is_escaped_before_the_quote_it_inserts(self):
        """Wrong order double-escapes the backslash this function adds itself."""
        assert spindle._systemd_quote('a"b') == '"a\\"b"'
        assert spindle._systemd_quote("a\\b") == '"a\\\\b"'
        assert spindle._systemd_quote('a\\"b') == '"a\\\\\\"b"'

    def test_percent_still_doubled(self):
        assert spindle._systemd_quote("100%") == '"100%%"'


class TestOwnServiceName:
    def test_reads_the_name_baked_into_the_unit(self, monkeypatch):
        monkeypatch.setenv("SPINDLE_SERVICE_NAME", "spindle-release")
        assert spindle._own_service_name() == "spindle-release"

    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("SPINDLE_SERVICE_NAME", raising=False)
        assert spindle._own_service_name() == "spindle"

    def test_rejects_a_name_that_is_not_a_valid_unit(self, monkeypatch):
        monkeypatch.setenv("SPINDLE_SERVICE_NAME", "../../evil")
        assert spindle._own_service_name() == "spindle"


class TestSystemdUserDirHonorsXdg:
    def test_xdg_config_home_is_used(self, tmp_path, monkeypatch):
        """A unit written where systemd does not look never starts."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert spindle._systemd_user_dir() == tmp_path / "cfg" / "systemd" / "user"

    def test_falls_back_to_home_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        assert spindle._systemd_user_dir() == tmp_path / ".config" / "systemd" / "user"


class TestEmptyServicePathIsStillChecked:
    def test_empty_path_reports_every_harness_unreachable(self, monkeypatch):
        """An empty PATH resolves nothing; truthiness skipped exactly that case."""
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: (f"/bin/{cmd}", "1.0") if cmd == "claude" else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        result = spindle._doctor_harness_check(service_path="")
        assert result["data"]["unreachable_from_service"] == ["claude-code"]

    def test_no_service_still_means_no_claim(self, monkeypatch):
        monkeypatch.setattr(spindle, "_probe_command", lambda cmd, timeout=5.0: (f"/bin/{cmd}", "1.0"))
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        assert spindle._doctor_harness_check(service_path=None)["data"]["unreachable_from_service"] == []

    def test_remedy_names_the_port_that_was_probed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            spindle,
            "_probe_command",
            lambda cmd, timeout=5.0: (f"/bin/{cmd}", "1.0") if cmd == "claude" else (None, None),
        )
        monkeypatch.setattr(spindle, "_discover_profiles", lambda: {})
        result = spindle._doctor_harness_check(service_path=str(tmp_path), service_name="svc", service_port=8042)
        remedy = [line for line in result["lines"] if "install-service" in line][0]
        assert "--port 8042" in remedy
        assert "<its port>" not in remedy


class TestReloadRefusesRatherThanDrainingTheWrongStore:
    def test_mismatch_returns_the_warning_for_the_caller_to_act_on(self, monkeypatch, capsys):
        monkeypatch.setattr(
            spindle, "_fetch_health", lambda host, port, timeout=2.0: ({"spool_dir": "/elsewhere/spools"}, None)
        )
        assert spindle._reload_warn_on_store_mismatch("127.0.0.1", 8042) is not None
        capsys.readouterr()

    def test_no_mismatch_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            spindle, "_fetch_health", lambda host, port, timeout=2.0: ({"spool_dir": str(spindle.SPINDLE_DIR)}, None)
        )
        assert spindle._reload_warn_on_store_mismatch("127.0.0.1", 8042) is None


class TestSmokeTimeoutIsRejectedNotClamped:
    def test_zero_is_rejected(self):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            spindle._positive_seconds("0")

    def test_negative_is_rejected(self):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            spindle._positive_seconds("-5")

    def test_non_numeric_is_rejected(self):
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            spindle._positive_seconds("soon")

    def test_positive_passes(self):
        assert spindle._positive_seconds("240") == 240


class TestInstallServiceEndToEnd:
    """Drive `install-service` through main(), on both platforms.

    The resolver could be perfectly correct and simply not wired into a branch —
    which is exactly the defect this replaced, and it passed every test because
    the tests called the resolver directly. These call the command.
    """

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")
        return tmp_path

    def _run(self, argv, monkeypatch, system="Linux", env=None):
        """Run install-service with the OS and every subprocess call stubbed."""
        import platform as platform_mod

        monkeypatch.setattr(platform_mod, "system", lambda: system)
        monkeypatch.setattr(spindle.subprocess, "run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        with patch.object(spindle.sys, "argv", ["spindle", "install-service", *argv]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        return exc.value.code

    def _unit(self, home):
        return home / ".config" / "systemd" / "user" / "svc.service"

    def _plist(self, home):
        return home / "Library" / "LaunchAgents" / "com.svc.server.plist"

    # --- systemd -------------------------------------------------------------

    def test_reinstall_without_arguments_keeps_port_and_store(self, home, monkeypatch, capsys):
        assert self._run(["--name", "svc", "--port", "8075", "--home", "/srv/store"], monkeypatch) == 0
        capsys.readouterr()
        assert self._run(["--name", "svc", "--force"], monkeypatch) == 0
        settings = spindle._service_settings_from_file(self._unit(home))
        assert (settings["port"], settings["home"]) == (8075, "/srv/store")
        assert "Keeping the port" in capsys.readouterr().out

    def test_ambient_spindle_home_does_not_move_an_existing_service(self, home, monkeypatch, capsys):
        """Installed on the default store; a later shell exporting one must not move it."""
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch) == 0
        capsys.readouterr()
        assert self._run(["--name", "svc", "--force"], monkeypatch, env={"SPINDLE_HOME": "/srv/other"}) == 0
        assert spindle._service_settings_from_file(self._unit(home))["home"] is None
        assert "SPINDLE_HOME" not in self._unit(home).read_text()

    def test_ambient_spindle_home_still_applies_on_a_first_install(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch, env={"SPINDLE_HOME": "/srv/env"}) == 0
        assert spindle._service_settings_from_file(self._unit(home))["home"] == "/srv/env"

    def test_explicit_arguments_still_win_on_reinstall(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8075", "--home", "/srv/store"], monkeypatch) == 0
        assert self._run(["--name", "svc", "--port", "9001", "--home", "/srv/new", "--force"], monkeypatch) == 0
        settings = spindle._service_settings_from_file(self._unit(home))
        assert (settings["port"], settings["home"]) == (9001, "/srv/new")

    def test_reinstall_backs_the_previous_unit_up(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch) == 0
        assert self._run(["--name", "svc", "--force"], monkeypatch) == 0
        backups = list(self._unit(home).parent.glob("svc.service.bak-*"))
        assert len(backups) == 1
        assert "8075" in backups[0].read_text()

    def test_existing_unit_without_force_is_refused(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch) == 0
        assert self._run(["--name", "svc"], monkeypatch) == 1

    # --- launchd -------------------------------------------------------------

    def test_darwin_reinstall_without_arguments_keeps_port_and_store(self, home, monkeypatch, capsys):
        assert self._run(["--name", "svc", "--port", "8075", "--home", "/srv/store"], monkeypatch, "Darwin") == 0
        capsys.readouterr()
        assert self._run(["--name", "svc", "--force"], monkeypatch, "Darwin") == 0
        settings = spindle._service_settings_from_file(self._plist(home))
        assert (settings["port"], settings["home"]) == (8075, "/srv/store")
        assert "Keeping the port" in capsys.readouterr().out

    def test_darwin_reinstall_backs_up_a_marked_plist(self, home, monkeypatch):
        """macOS backed up only unmarked files, so its own agents were replaced blind."""
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch, "Darwin") == 0
        assert self._run(["--name", "svc", "--force"], monkeypatch, "Darwin") == 0
        backups = list(self._plist(home).parent.glob("com.svc.server.plist.bak-*"))
        assert len(backups) == 1
        assert "8075" in backups[0].read_text()

    def test_darwin_ambient_home_does_not_move_an_existing_agent(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8075"], monkeypatch, "Darwin") == 0
        assert self._run(["--name", "svc", "--force"], monkeypatch, "Darwin", env={"SPINDLE_HOME": "/srv/other"}) == 0
        assert spindle._service_settings_from_file(self._plist(home))["home"] is None

    def test_darwin_plist_stays_valid_xml_through_a_reinstall(self, home, monkeypatch):
        import xml.etree.ElementTree as ET

        assert self._run(["--name", "svc", "--port", "8075", "--home", "/srv/A&B"], monkeypatch, "Darwin") == 0
        assert self._run(["--name", "svc", "--force"], monkeypatch, "Darwin") == 0
        ET.parse(self._plist(home))
        assert spindle._service_settings_from_file(self._plist(home))["home"] == "/srv/A&B"


class TestServiceFileScanRobustness:
    """A wrong read gets written back into the service; a missing read is safe."""

    def test_execstartpre_cannot_supply_the_port(self, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\n"
            "ExecStartPre=/bin/echo serve --http --port 9999\n"
            "ExecStart=/bin/spindle serve --http --port 8075\n"
        )
        assert spindle._service_settings_from_file(unit)["port"] == 8075

    def test_a_nested_dict_cannot_override_the_real_environment(self, tmp_path):
        plist = tmp_path / "com.svc.server.plist"
        plist.write_text(
            '<?xml version="1.0"?>\n<plist version="1.0"><dict>\n'
            "<key>EnvironmentVariables</key><dict>"
            "<key>SPINDLE_HOME</key><string>/real/store</string></dict>\n"
            "<key>SomethingElse</key><dict>"
            "<key>SPINDLE_HOME</key><string>/decoy</string></dict>\n"
            "</dict></plist>\n"
        )
        assert spindle._service_settings_from_file(plist)["home"] == "/real/store"

    def test_an_unrelated_array_cannot_supply_the_port(self, tmp_path):
        plist = tmp_path / "com.svc.server.plist"
        plist.write_text(
            '<?xml version="1.0"?>\n<plist version="1.0"><dict>\n'
            "<key>ProgramArguments</key><array>"
            "<string>/bin/spindle</string><string>--port</string><string>8075</string></array>\n"
            "<key>WatchPaths</key><array><string>--port</string><string>9999</string></array>\n"
            "</dict></plist>\n"
        )
        assert spindle._service_settings_from_file(plist)["port"] == 8075


class TestUnitEnvironmentReading:
    """systemd's own Environment= syntax, not the subset spindle happens to emit."""

    def test_several_assignments_on_one_line(self):
        """A regex to end-of-line reads the store as '/srv/store" "FOO=bar'."""
        text = 'Environment="SPINDLE_HOME=/srv/store" "FOO=bar"\n'
        assert spindle._env_from_unit_text(text, "SPINDLE_HOME") == "/srv/store"
        assert spindle._env_from_unit_text(text, "FOO") == "bar"

    def test_store_not_first_on_the_line_is_still_found(self):
        text = 'Environment="PATH=/usr/bin" "SPINDLE_HOME=/srv/store"\n'
        assert spindle._env_from_unit_text(text, "SPINDLE_HOME") == "/srv/store"

    def test_a_later_assignment_wins(self):
        """systemd runs with the second value, so regenerating from the first moves it."""
        text = "Environment=SPINDLE_HOME=/old\nEnvironment=SPINDLE_HOME=/actual\n"
        assert spindle._env_from_unit_text(text, "SPINDLE_HOME") == "/actual"

    def test_unquoted_and_quoted_forms_agree(self):
        assert spindle._env_from_unit_text("Environment=SPINDLE_PORT=8075\n", "SPINDLE_PORT") == "8075"
        assert spindle._env_from_unit_text('Environment="SPINDLE_PORT=8075"\n', "SPINDLE_PORT") == "8075"

    def test_escapes_round_trip(self):
        for value in ("/srv/a b", "/srv/100%dir", '/srv/quote"d', "/srv/back\\slash", "/srv/tail\\"):
            unit = spindle._systemd_unit_text("/bin/spindle", 8002, home=value)
            assert spindle._env_from_unit_text(unit, "SPINDLE_HOME") == value, value

    def test_absent_variable_is_none(self):
        assert spindle._env_from_unit_text("Environment=FOO=bar\n", "SPINDLE_HOME") is None

    def test_execstart_continuation_via_file(self, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nExecStart=/bin/spindle \\\n    serve --http --port 8075\n")
        assert spindle._service_settings_from_file(unit)["port"] == 8075


class TestDarwinFailureLadder:
    """Every rung past the unload must put the old agent back."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")
        return tmp_path

    def _plist(self, home):
        return home / "Library" / "LaunchAgents" / "com.svc.server.plist"

    def _install(self, monkeypatch, argv, runner=None):
        import platform as platform_mod

        monkeypatch.setattr(platform_mod, "system", lambda: "Darwin")
        monkeypatch.setattr(
            spindle.subprocess, "run", runner or (lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
        )
        with patch.object(spindle.sys, "argv", ["spindle", "install-service", *argv]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        return exc.value.code

    def test_a_rejected_plist_restores_and_reloads_the_previous_agent(self, home, monkeypatch, capsys):
        assert self._install(monkeypatch, ["--name", "svc", "--port", "8075"]) == 0
        original = self._plist(home).read_text()
        capsys.readouterr()

        calls = []

        def runner(cmd, *a, **k):
            calls.append(cmd)
            # the load of the NEW plist fails; the restore's load succeeds
            if cmd[:2] == ["launchctl", "load"] and len([c for c in calls if c[:2] == ["launchctl", "load"]]) == 1:
                return MagicMock(returncode=1, stdout="", stderr="Load failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._install(monkeypatch, ["--name", "svc", "--port", "9001", "--force"], runner) == 1
        assert self._plist(home).read_text() == original
        out = capsys.readouterr().out
        assert "Restored the previous plist" in out and "reloaded it" in out

    def test_a_failed_write_restores_the_previous_agent(self, home, monkeypatch, capsys):
        assert self._install(monkeypatch, ["--name", "svc", "--port", "8075"]) == 0
        original = self._plist(home).read_text()
        capsys.readouterr()

        real_write = spindle.Path.write_text

        def boom(self, *a, **k):
            if self.name.endswith(".plist") and "bak-" not in self.name:
                raise OSError("No space left on device")
            return real_write(self, *a, **k)

        monkeypatch.setattr(spindle.Path, "write_text", boom)
        assert self._install(monkeypatch, ["--name", "svc", "--port", "9001", "--force"]) == 1
        monkeypatch.undo()
        assert self._plist(home).read_text() == original
        assert "Could not write" in capsys.readouterr().out

    def test_a_failed_unload_is_not_reported_as_no_agent_loaded(self, home, monkeypatch, capsys):
        """If the unload failed the OLD agent is still running; saying otherwise is backwards."""
        assert self._install(monkeypatch, ["--name", "svc", "--port", "8075"]) == 0
        capsys.readouterr()

        def runner(cmd, *a, **k):
            if cmd[:2] in (["launchctl", "unload"], ["launchctl", "load"]):
                return MagicMock(returncode=1, stdout="", stderr="already loaded")
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._install(monkeypatch, ["--name", "svc", "--port", "9001", "--force"], runner) == 1
        out = capsys.readouterr().out
        assert "it is still running" in out
        assert "No agent is loaded" not in out

    def test_a_backup_that_cannot_be_written_stops_before_the_unload(self, home, monkeypatch, capsys):
        assert self._install(monkeypatch, ["--name", "svc", "--port", "8075"]) == 0
        original = self._plist(home).read_text()
        capsys.readouterr()

        monkeypatch.setattr(spindle, "_backup_service_file", lambda path: None)
        calls = []

        def runner(cmd, *a, **k):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._install(monkeypatch, ["--name", "svc", "--force"], runner) == 1
        assert self._plist(home).read_text() == original
        assert not any(c[:2] == ["launchctl", "unload"] for c in calls)


class TestSystemdEnvironmentSyntax:
    """Every expectation here was read off systemd itself, not off the man page.

    The values were produced by installing a unit carrying each line, starting
    it, and having it `printenv` the variable into a file — so these assert what
    systemd on a real machine does, which is the standard a reader of somebody
    else's unit file has to meet. Three hand-rolled readers in a row got this
    wrong in a way that silently rewrites a running service's spool store.
    """

    # (Environment= line(s), what systemd resolves V to)
    CASES = [
        ("Environment=V=/srv/plain", "/srv/plain"),
        ('Environment="V=/srv/with space"', "/srv/with space"),
        ("Environment='V=/srv/single quote'", "/srv/single quote"),
        ('Environment = "V=/srv/spaced-eq"', "/srv/spaced-eq"),
        ("Environment=V=a\\sb", "a b"),
        ("Environment=V=a\\\\b", "a\\b"),
        ("Environment=V=100%%pct", "100%pct"),
        ('Environment="A=/x" "V=/srv/second-on-line"', "/srv/second-on-line"),
        ("Environment=V=first\nEnvironment=V=second", "second"),
        ("Environment=V=first\nEnvironment=\nEnvironment=V=after-reset", "after-reset"),
        ("Environment=V=", ""),
        ("Environment=V=a=b", "a=b"),
        ("Environment=V=tail\\\\", "tail\\"),
        ("Environment=V=/srv/a\\tb", "/srv/a\tb"),
        ('Environment="V=quote\\"inside"', 'quote"inside'),
        ("Environment=V=%%h", "%h"),
        ("Environment=V=x \\\n    W=y", "x"),
    ]

    @pytest.mark.parametrize("line,expected", CASES)
    def test_matches_systemd(self, line, expected):
        assert spindle._env_from_unit_text(line + "\n", "V") == expected

    def test_an_unterminated_quote_drops_that_word_and_the_rest(self):
        """systemd keeps what parsed before the bad word; only the tail is lost."""
        assert spindle._env_from_unit_text('Environment="V=/srv/open\n', "V") is None
        assert spindle._parse_systemd_env_line('"V=/srv/open') == []
        # an assignment before the break survives, as it does in systemd
        assert spindle._env_from_unit_text('Environment=V=/srv/kept "X=/srv/open\n', "V") == "/srv/kept"

    def test_a_bare_environment_line_resets_everything_before_it(self):
        assert spindle._env_from_unit_text("Environment=V=gone\nEnvironment=\n", "V") is None

    def test_continuation_does_not_swallow_the_next_directive(self):
        """`\\s*` after the newline ate blank lines and then the following line."""
        text = 'ExecStart=/bin/spindle \\\n    serve --http --port 8075\n\nEnvironment="SPINDLE_HOME=/srv/store"\n'
        assert spindle._env_from_unit_text(text, "SPINDLE_HOME") == "/srv/store"

    def test_round_trip_through_the_generator_is_exact(self):
        for value in ("/srv/a b", "/srv/100%dir", '/srv/q"d', "/srv/back\\slash", "/srv/tail\\", "", "/srv/a=b"):
            unit = spindle._systemd_unit_text("/bin/spindle", 8002, home=value)
            assert spindle._env_from_unit_text(unit, "SPINDLE_HOME") == value, repr(value)


class TestPlistReadingUsesPlistlib:
    def test_binary_plists_are_read(self, tmp_path):
        """launchd's own preferred format; a hand XML walk sees mojibake."""
        import plistlib

        path = tmp_path / "com.svc.server.plist"
        data = {
            "Label": "com.svc.server",
            "ProgramArguments": ["/bin/spindle", "serve", "--http", "--port", "8075"],
            "EnvironmentVariables": {"SPINDLE_HOME": "/srv/store", "SPINDLE_PORT": "8075"},
        }
        with open(path, "wb") as fh:
            plistlib.dump(data, fh, fmt=plistlib.FMT_BINARY)
        settings = spindle._service_settings_from_file(path)
        assert (settings["port"], settings["home"], settings["readable"]) == (8075, "/srv/store", True)

    def test_the_last_port_argument_wins(self, tmp_path):
        """argparse takes the last; reading the first rewrites the agent's port."""
        import plistlib

        path = tmp_path / "com.svc.server.plist"
        with open(path, "wb") as fh:
            plistlib.dump({"ProgramArguments": ["/bin/spindle", "--port", "8075", "--port", "9001"]}, fh)
        assert spindle._service_settings_from_file(path)["port"] == 9001

    def test_generated_plists_round_trip(self, tmp_path):
        path = tmp_path / "com.svc.server.plist"
        path.write_text(spindle._launchd_plist_text("com.svc.server", "/bin/spindle", 8075, home="/srv/A&B store"))
        settings = spindle._service_settings_from_file(path)
        assert (settings["port"], settings["home"], settings["readable"]) == (8075, "/srv/A&B store", True)

    def test_unreadable_plist_is_marked_unreadable(self, tmp_path):
        path = tmp_path / "com.svc.server.plist"
        path.write_bytes(b"\x00\x01 not a plist")
        settings = spindle._service_settings_from_file(path)
        assert settings["readable"] is False
        assert settings["port"] is None


class TestLaunchctlExceptionsRestore:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")
        return tmp_path

    def _install(self, monkeypatch, argv, runner=None):
        import platform as platform_mod

        monkeypatch.setattr(platform_mod, "system", lambda: "Darwin")
        monkeypatch.setattr(
            spindle.subprocess, "run", runner or (lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
        )
        with patch.object(spindle.sys, "argv", ["spindle", "install-service", *argv]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        return exc.value.code

    def test_launchctl_raising_still_restores_the_previous_agent(self, home, monkeypatch, capsys):
        """An OSError past the unload used to escape, leaving no agent at all."""
        assert self._install(monkeypatch, ["--name", "svc", "--port", "8075"]) == 0
        original = (home / "Library" / "LaunchAgents" / "com.svc.server.plist").read_text()
        capsys.readouterr()

        state = {"loads": 0}

        def runner(cmd, *a, **k):
            if cmd[:2] == ["launchctl", "load"]:
                state["loads"] += 1
                if state["loads"] == 1:
                    raise OSError("launchctl: not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._install(monkeypatch, ["--name", "svc", "--port", "9001", "--force"], runner) == 1
        assert (home / "Library" / "LaunchAgents" / "com.svc.server.plist").read_text() == original
        assert "Restored the previous plist" in capsys.readouterr().out


class TestServiceRecord:
    """Spindle's own record of what it installed, in a format spindle owns.

    Six rounds of review went into reading these settings back out of a systemd
    unit, and each round found another divergence between the reader and
    systemd — ending at `%h`, whose value depends on the runtime context of the
    service and cannot be known from the file at all. A value read slightly
    wrong is worse than one not read, because it is written straight back.
    """

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def test_round_trips(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("body")
        spindle._write_service_record("svc", 8075, "/srv/store", unit, "body")
        record = spindle._read_service_record("svc")
        assert (record["port"], record["home"], record["name"]) == (8075, "/srv/store", "svc")
        assert record["spindle_version"] == spindle.__version__

    def test_absent_record_is_none(self, config):
        assert spindle._read_service_record("never-installed") is None

    def test_corrupt_record_is_none(self, config):
        path = spindle._service_record_path("svc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert spindle._read_service_record("svc") is None

    def test_a_record_survives_values_no_parser_could_read(self, config, tmp_path):
        """%h, escapes, quotes: exactly the values that defeated the readers."""
        unit = tmp_path / "svc.service"
        unit.write_text("body")
        for home in ("%h/spindle-store", "/srv/a\\sb", "/srv/'q'", "/srv/tail\\", "", "/srv/A&B"):
            spindle._write_service_record("svc", 8075, home, unit, "body")
            assert spindle._read_service_record("svc")["home"] == home, repr(home)


class TestRegenerationUsesTheRecord:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def _existing(self, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 8075, home="/srv/store", name="svc"))
        return unit

    def test_record_supplies_omitted_arguments(self, config, tmp_path):
        unit = self._existing(tmp_path)
        spindle._write_service_record("svc", 8075, "/srv/store", unit)
        port, home, notes, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home, blocker) == (8075, "/srv/store", None)
        assert any("Keeping the port" in n for n in notes)

    def test_explicit_arguments_beat_the_record(self, config, tmp_path):
        unit = self._existing(tmp_path)
        spindle._write_service_record("svc", 8075, "/srv/store", unit)
        port, home, _, blocker = spindle._resolve_service_settings(unit, 9001, "/srv/new", name="svc")
        assert (port, home, blocker) == (9001, "/srv/new", None)

    def test_ambient_environment_cannot_move_a_recorded_service(self, config, tmp_path, monkeypatch):
        unit = self._existing(tmp_path)
        spindle._write_service_record("svc", 8075, None, unit)
        monkeypatch.setenv("SPINDLE_HOME", "/srv/decoy")
        monkeypatch.setenv("SPINDLE_PORT", "9999")
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home, blocker) == (8075, None, None)

    def test_a_recorded_value_no_parser_could_read_survives(self, config, tmp_path):
        """%h defeated the file reader; the record carries it exactly."""
        unit = self._existing(tmp_path)
        spindle._write_service_record("svc", 8075, "%h/spindle-store", unit)
        _, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (home, blocker) == ("%h/spindle-store", None)

    def test_first_install_has_no_file_and_no_record(self, config, tmp_path, monkeypatch):
        monkeypatch.setenv("SPINDLE_HOME", "/srv/env")
        monkeypatch.setattr(spindle, "DEFAULT_PORT", 8002)
        port, home, notes, blocker = spindle._resolve_service_settings(tmp_path / "new.service", None, None, name="new")
        assert (port, home, blocker, notes) == (8002, "/srv/env", None, [])

    def test_an_unrecorded_service_is_not_guessed_at(self, config, tmp_path):
        """The change of approach: refuse to invent settings rather than move a service."""
        unit = self._existing(tmp_path)  # exists, no record
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home) == (None, None)
        assert blocker is not None
        assert "--port <port>" in blocker
        assert "--home <store>" in blocker

    def test_explicit_arguments_unblock_an_unrecorded_service(self, config, tmp_path):
        unit = self._existing(tmp_path)
        port, home, _, blocker = spindle._resolve_service_settings(unit, 9001, "/srv/new", name="svc")
        assert (port, home, blocker) == (9001, "/srv/new", None)

    def test_a_file_stating_no_port_gets_a_placeholder_not_the_default(self, config, tmp_path):
        """8002 is not a reading of the file; it is the default install's port."""
        unit = tmp_path / "svc.service"
        unit.write_text("[Unit]\nDescription=hand written, no port anywhere\n")
        _, _, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert blocker is not None
        assert "--port <port>" in blocker
        assert str(spindle._BASE_DEFAULT_PORT) not in blocker


class TestInstallServiceRefusesToGuess:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")
        return tmp_path

    def _run(self, argv, monkeypatch, system="Linux"):
        import platform as platform_mod

        monkeypatch.setattr(platform_mod, "system", lambda: system)
        monkeypatch.setattr(spindle.subprocess, "run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
        with patch.object(spindle.sys, "argv", ["spindle", "install-service", *argv]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        return exc.value.code

    def test_a_hand_written_unit_is_not_regenerated_from_a_guess(self, home, monkeypatch, capsys):
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        original = "[Unit]\nDescription=mine\n[Service]\nEnvironment=SPINDLE_HOME=%h/mystore\nExecStart=/bin/true\n"
        (unit_dir / "svc.service").write_text(original)
        assert self._run(["--name", "svc", "--force"], monkeypatch) == 1
        assert (unit_dir / "svc.service").read_text() == original  # untouched
        error = capsys.readouterr().err
        assert "--port <port>" in error
        assert "--home <store>" in error

    def test_stating_the_settings_installs_and_records_them(self, home, monkeypatch):
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "svc.service").write_text("[Unit]\nDescription=mine\n")
        assert self._run(["--name", "svc", "--port", "8115", "--home", "/srv/mine", "--force"], monkeypatch) == 0
        record = spindle._read_service_record("svc")
        assert (record["port"], record["home"]) == (8115, "/srv/mine")

    def test_after_that_a_bare_reinstall_works(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8115", "--home", "/srv/mine"], monkeypatch) == 0
        assert self._run(["--name", "svc", "--force"], monkeypatch) == 0
        record = spindle._read_service_record("svc")
        assert (record["port"], record["home"]) == (8115, "/srv/mine")
        unit = (home / ".config" / "systemd" / "user" / "svc.service").read_text()
        assert "--port 8115" in unit and "/srv/mine" in unit


class TestRecordIsBoundToTheFileItDescribes:
    """A record is state that can drift; the digest is what makes it trustworthy."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def _installed(self, tmp_path, port=8075, home="/srv/store"):
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", port, home=home, name="svc"))
        spindle._write_service_record("svc", port, home, unit)
        return unit

    def test_an_untouched_file_is_trusted(self, config, tmp_path):
        unit = self._installed(tmp_path)
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home, blocker) == (8075, "/srv/store", None)

    def test_a_hand_edited_file_makes_the_record_stale(self, config, tmp_path):
        """Editing the unit's port by hand must not be silently rewritten back."""
        unit = self._installed(tmp_path)
        unit.write_text(unit.read_text().replace("--port 8075", "--port 9001"))
        port, home, notes, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home) == (None, None)
        assert blocker is not None
        assert any("edited since" in n for n in notes)

    def test_a_replaced_file_makes_the_record_stale(self, config, tmp_path):
        unit = self._installed(tmp_path)
        unit.write_text("[Unit]\nDescription=somebody else's\n")
        _, _, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert blocker is not None

    def test_explicit_arguments_still_work_on_a_stale_record(self, config, tmp_path):
        unit = self._installed(tmp_path)
        unit.write_text("[Unit]\nDescription=edited\n")
        port, home, _, blocker = spindle._resolve_service_settings(unit, 9001, "/srv/new", name="svc")
        assert (port, home, blocker) == (9001, "/srv/new", None)


class TestMalformedRecordsRefuseRatherThanDefault:
    """A record that cannot be trusted must take the ask-me path, not the defaults."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def _write_raw(self, record):
        path = spindle._service_record_path("svc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    @pytest.mark.parametrize(
        "record",
        [
            {"port": "8075", "home": "/srv/store"},  # port as a string
            {"port": True, "home": "/srv/store"},  # bool is an int subclass
            {"port": 8075, "home": []},  # home wrong type
            {"port": 8075},  # home missing entirely
            {"home": "/srv/store"},  # port missing
            {"port": 8075, "home": "/srv/store", "service_sha256": 12},  # digest wrong type
        ],
    )
    def test_unusable_records_read_as_absent(self, config, record):
        self._write_raw(record)
        assert spindle._read_service_record("svc") is None

    def test_and_therefore_block_instead_of_defaulting(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 8115, home="/srv/mine", name="svc"))
        self._write_raw({"port": "8115", "home": "/srv/mine"})
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home) == (None, None)
        assert blocker is not None

    def test_a_good_record_still_reads(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("x")
        spindle._write_service_record("svc", 8075, None, unit)
        record = spindle._read_service_record("svc")
        assert record["port"] == 8075 and record["home"] is None


class TestRecordFollowsActivation:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")
        return tmp_path

    def _run(self, argv, monkeypatch, system="Linux", runner=None):
        import platform as platform_mod

        monkeypatch.setattr(platform_mod, "system", lambda: system)
        monkeypatch.setattr(
            spindle.subprocess, "run", runner or (lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
        )
        with patch.object(spindle.sys, "argv", ["spindle", "install-service", *argv]):
            with pytest.raises(SystemExit) as exc:
                spindle.main()
        return exc.value.code

    def test_darwin_a_failed_load_leaves_the_record_describing_the_running_agent(self, home, monkeypatch):
        """The record must not claim settings the restore just undid."""
        assert self._run(["--name", "svc", "--port", "8115"], monkeypatch, "Darwin") == 0
        assert spindle._read_service_record("svc")["port"] == 8115

        state = {"loads": 0}

        def runner(cmd, *a, **k):
            if cmd[:2] == ["launchctl", "load"]:
                state["loads"] += 1
                if state["loads"] == 1:
                    return MagicMock(returncode=1, stdout="", stderr="rejected")
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._run(["--name", "svc", "--port", "9001", "--force"], monkeypatch, "Darwin", runner) == 1
        assert spindle._read_service_record("svc")["port"] == 8115
        plist = home / "Library" / "LaunchAgents" / "com.svc.server.plist"
        assert spindle._service_settings_from_file(plist)["port"] == 8115

    def test_linux_a_failed_enable_writes_no_record(self, home, monkeypatch):
        def runner(cmd, *a, **k):
            if cmd[:3] == ["systemctl", "--user", "enable"]:
                return MagicMock(returncode=1, stdout="", stderr="no such unit")
            return MagicMock(returncode=0, stdout="", stderr="")

        assert self._run(["--name", "svc", "--port", "8115"], monkeypatch, "Linux", runner) == 1
        assert spindle._read_service_record("svc") is None

    def test_a_successful_install_records_the_file_it_wrote(self, home, monkeypatch):
        assert self._run(["--name", "svc", "--port", "8115"], monkeypatch) == 0
        unit = home / ".config" / "systemd" / "user" / "svc.service"
        assert spindle._read_service_record("svc")["service_sha256"] == spindle._service_file_digest(unit)


class TestDigestIsRequiredNotOptional:
    """ "No digest" must mean "cannot verify", not "skip the check"."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def _raw_record(self, record):
        path = spindle._service_record_path("svc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    @pytest.mark.parametrize("digest", [None, "", "absent"])
    def test_a_record_without_a_usable_digest_is_not_a_record(self, config, digest):
        record = {"name": "svc", "port": 8075, "home": "/srv/store", "service_file": "/x"}
        if digest != "absent":
            record["service_sha256"] = digest
        self._raw_record(record)
        assert spindle._read_service_record("svc") is None

    def test_a_pre_digest_record_no_longer_silently_regenerates(self, config, tmp_path):
        """Every record written before digests existed looked like this."""
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 9001, home="/srv/handedited", name="svc"))
        self._raw_record({"name": "svc", "port": 8075, "home": "/srv/store", "service_file": str(unit)})
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home) == (None, None)
        assert blocker is not None

    def test_the_digest_covers_the_bytes_written_not_a_later_read(self, config, tmp_path):
        """A write landing between activation and recording must not be blessed."""
        unit = tmp_path / "svc.service"
        unit.write_text("what spindle wrote")
        spindle._write_service_record("svc", 8075, None, unit, "what spindle wrote")
        good = spindle._read_service_record("svc")["service_sha256"]

        unit.write_text("edited before the record landed")
        spindle._write_service_record("svc", 8075, None, unit, "what spindle wrote")
        assert spindle._read_service_record("svc")["service_sha256"] == good
        # and that record is therefore stale against the file now on disk
        _, _, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert blocker is not None

    def test_out_of_range_ports_are_rejected(self, config):
        for port in (0, -1, 70000):
            self._raw_record({"name": "svc", "port": port, "home": None, "service_file": "/x", "service_sha256": "abc"})
            assert spindle._read_service_record("svc") is None, port

    def test_the_cli_rejects_an_impossible_port(self):
        import argparse

        for bad in ("0", "70000", "-1", "http"):
            with pytest.raises(argparse.ArgumentTypeError):
                spindle._service_port(bad)
        assert spindle._service_port("8075") == 8075


class TestOrphanedRecordIsAnnounced:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        return tmp_path

    def test_recreating_a_deleted_service_says_where_the_values_came_from(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("body")
        spindle._write_service_record("svc", 8115, "/srv/store", unit, "body")
        unit.unlink()
        port, home, notes, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home, blocker) == (8115, "/srv/store", None)
        assert any(f"{unit.name} is gone" in note and "recreating it from spindle's record" in note for note in notes)


class TestTheRefusalQuotesRatherThanInterprets:
    """The suggestion must never be runnable.

    Four consecutive review rounds found the suggestion printing a pasteable
    command that would move the service: 8002 offered as the file's port; an
    empty store offered for a `%h` specifier; an Environment=SPINDLE_PORT
    fallback standing in for an unparsed ExecStart port; a repeated --port read
    first-wins where systemd binds last-wins; a value an EnvironmentFile
    overrides. All the same shape — the reader interprets with less authority
    than systemd applies, then offers its interpretation as something to run.
    So it offers nothing to run.
    """

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def _blocker(self, unit):
        return spindle._resolve_service_settings(unit, None, None, name="svc")[3]

    def _command(self, blocker):
        return [line for line in blocker.splitlines() if "install-service" in line][0]

    def test_the_suggested_command_is_never_runnable(self, config, tmp_path):
        """One mixed unit covers the invariant; its spellings cannot affect the fixed template."""
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\n"
            "ExecStart=/b spindle serve --http --port 8115 --port 9000\n"
            "Environment=SPINDLE_HOME=%h/store SPINDLE_PORT=8115\n"
            "EnvironmentFile=/etc/spindle.env\n"
        )
        command = self._command(self._blocker(unit))
        assert "--port <port>" in command
        assert "--home <store>" in command
        # no value read out of the file may appear as an argument
        for value in ("8115", "9000", "8002", "%h/store"):
            assert f"--port {value}" not in command
            assert f"--home {value}" not in command

    def test_relevant_unit_lines_are_quoted_for_the_operator(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\n"
            "ExecStart=/b spindle serve --http --port 8115\n"
            "Environment=SPINDLE_HOME=%h/store\n"
            "EnvironmentFile=/etc/spindle.env\n"
        )
        blocker = self._blocker(unit)
        assert "ExecStart=/b spindle serve --http --port 8115" in blocker
        assert "SPINDLE_HOME=%h/store" in blocker
        assert "EnvironmentFile=/etc/spindle.env" in blocker

    def test_a_plist_is_quoted_too(self, config, tmp_path):
        path = tmp_path / "com.svc.server.plist"
        path.write_text(spindle._launchd_plist_text("com.svc.server", "/b/spindle", 8115, home="/srv/store"))
        blocker = self._blocker(path)
        assert "--port <port>" in self._command(blocker)
        assert "SPINDLE_HOME" in blocker and "/srv/store" in blocker

    def test_the_excerpt_is_bounded(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\n" + "Environment=SPINDLE_EXTRA=x\n" * 50)
        excerpt, _ = spindle._service_file_excerpt(unit)
        assert len(excerpt.splitlines()) == 13  # twelve entries and one truncation marker

    def test_an_unreadable_file_still_refuses_without_inventing(self, config, tmp_path):
        path = tmp_path / "com.svc.server.plist"
        path.write_bytes(b"\x00\x01 not a plist")
        command = self._command(self._blocker(path))
        assert "--port <port>" in command and "8002" not in command


class TestExecStartPortReadingMatchesArgparse:
    """The reader is probe-only now, but reload/doctor still ask it for a port."""

    def _port(self, tmp_path, execstart):
        unit = tmp_path / "svc.service"
        unit.write_text(f"[Service]\n{execstart}\n")
        return spindle._service_settings_from_file(unit)["port"]

    def test_last_repeated_port_wins(self, tmp_path):
        assert self._port(tmp_path, "ExecStart=/b spindle serve --http --port 8115 --port 9000") == 9000

    def test_equals_form_is_read(self, tmp_path):
        assert self._port(tmp_path, "ExecStart=/b spindle serve --http --port=8115") == 8115

    def test_whitespace_around_the_directive(self, tmp_path):
        assert self._port(tmp_path, "ExecStart = /b spindle serve --http --port 8115") == 8115

    def test_a_port_after_another_flag_is_read(self, tmp_path):
        assert self._port(tmp_path, "ExecStart=/b spindle serve --http --host 1.2.3.4 --port 8115") == 8115


def test_service_files_are_written_as_utf8(tmp_path, monkeypatch):
    """The digest is UTF-8; a locale-encoded write could never match it."""
    monkeypatch.setattr(spindle.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("SPINDLE_HOME", raising=False)
    monkeypatch.delenv("SPINDLE_PORT", raising=False)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "spindle").write_text("#!/bin/sh\n")

    import platform as platform_mod

    monkeypatch.setattr(platform_mod, "system", lambda: "Linux")
    monkeypatch.setattr(spindle.subprocess, "run", lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""))
    argv = ["spindle", "install-service", "--name", "svc", "--port", "8115", "--home", "/srv/café"]
    with patch.object(spindle.sys, "argv", argv):
        with pytest.raises(SystemExit) as exc:
            spindle.main()
    assert exc.value.code == 0

    unit = tmp_path / ".config" / "systemd" / "user" / "svc.service"
    assert unit.read_bytes().decode("utf-8")  # written as UTF-8, whatever the locale
    # and therefore the record's digest matches the file on disk
    assert spindle._read_service_record("svc")["service_sha256"] == spindle._service_file_digest(unit)
    assert spindle._resolve_service_settings(unit, None, None, name="svc")[3] is None


class TestTheExcerptIsSafeToRead:
    """Quoting is what the operator now retypes, so "verbatim" is not enough."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def _blocker(self, unit):
        return spindle._resolve_service_settings(unit, None, None, name="svc")[3]

    def test_a_wrapped_execstart_still_shows_its_port(self, config, tmp_path):
        """Unfolded, the port sat on a continuation line and vanished from view."""
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\nExecStart=/usr/bin/spindle serve --http \\\n    --port 8115 \\\n    --host 127.0.0.1\n"
        )
        blocker = self._blocker(unit)
        assert "--port 8115" in blocker

    def test_an_execstart_with_space_before_equals_is_shown(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nExecStart = /b spindle serve --http --port 8115\n")
        excerpt, _ = spindle._service_file_excerpt(unit)
        assert "--port 8115" in excerpt

    def test_no_resolution_is_claimed_for_a_specifier_store(self, config, tmp_path):
        """Spindle shows the line and sends the operator to systemd for its value."""
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nEnvironment=SPINDLE_HOME=%h/store\nExecStart=/b serve --http --port 8115\n")
        blocker = self._blocker(unit)
        assert "%h/store" in blocker
        assert "systemctl --user show -p Environment" in blocker

    def test_no_effective_store_value_is_claimed(self, config, tmp_path):
        """The excerpt quotes ambiguous input but never presents an interpretation as fact."""
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nEnvironment=SPINDLE_HOME=/srv/a\\x20b\nEnvironment=\n")
        blocker = self._blocker(unit)
        for false_claim in ("The store resolves to", "No store survives", "runs on the default store"):
            assert false_claim not in blocker

    def test_unrelated_environment_lines_are_not_printed(self, config, tmp_path):
        """This text lands in agent transcripts and spool records."""
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\nEnvironment=OPENAI_API_KEY=sk-do-not-print-me\n"
            'Environment="SPINDLE_HOME=/srv/store"\nExecStart=/b serve --http --port 8115\n'
        )
        blocker = self._blocker(unit)
        assert "sk-do-not-print-me" not in blocker
        assert "/srv/store" in blocker

    def test_an_environmentfile_path_is_still_shown(self, config, tmp_path):
        """The path matters (it may hold the store); its contents are not read."""
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nEnvironmentFile=/etc/spindle.env\nExecStart=/b serve --http --port 8115\n")
        assert "/etc/spindle.env" in self._blocker(unit)

    def test_control_characters_are_stripped(self, config, tmp_path):
        """An escape sequence in a value could clear the screen and hide the rest."""
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/\x1b[2Jstore"\nExecStart=/b --port 1\n')
        blocker = self._blocker(unit)
        assert "\x1b" not in blocker

    def test_a_very_long_quoted_line_is_truncated(self, config, tmp_path):
        """The quoted line is bounded, so a value cannot run to megabytes."""
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/' + "x" * 5000 + '"\n')
        blocker = self._blocker(unit)
        quoted = [ln for ln in blocker.splitlines() if ln.startswith("    Environment=")][0]
        assert len(quoted) < 300
        assert quoted.endswith("...")

    def test_a_binary_plist_is_named_not_dumped(self, config, tmp_path):
        import plistlib

        path = tmp_path / "com.svc.server.plist"
        with open(path, "wb") as fh:
            plistlib.dump({"EnvironmentVariables": {"SPINDLE_HOME": "/srv/store"}}, fh, fmt=plistlib.FMT_BINARY)
        excerpt, note = spindle._service_file_excerpt(path)
        assert excerpt == ""
        assert path.name in note
        assert "bplist" not in note

    def test_an_unreadable_file_says_so(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nExecStart=/b serve --http --port 1\n")
        unit.chmod(0o000)
        try:
            excerpt, note = spindle._service_file_excerpt(unit)
        finally:
            unit.chmod(0o600)
        assert excerpt == ""
        assert note

    def test_a_file_with_nothing_to_say_is_not_confused_with_one_that_could_not_be_read(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text("[Unit]\nDescription=nothing here\n")
        excerpt, _ = spindle._service_file_excerpt(unit)
        assert excerpt == ""

    def test_a_unit_without_environmentfile_gets_no_caveat(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/store"\n')
        _, note = spindle._service_file_excerpt(unit)
        assert "This unit also reads an EnvironmentFile" not in note


class TestPortProbePrefersTheRecord:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def test_a_recorded_port_beats_a_decoy_in_the_unit(self, config):
        """`sh -c '... --port 8115 # --port=9001'` binds 8115; the text says otherwise."""
        unit = spindle._unit_file_path("svc")
        unit.parent.mkdir(parents=True, exist_ok=True)
        body = "[Service]\nExecStart=/bin/sh -c '/b/spindle serve --http --port 8115 # --port=9001'\n"
        unit.write_text(body)
        spindle._write_service_record("svc", 8115, None, unit, body)
        assert spindle._port_from_unit("svc") == 8115

    def test_without_a_record_it_falls_back_to_the_file(self, config):
        unit = spindle._unit_file_path("svc")
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\nExecStart=/b/spindle serve --http --port 8115\n")
        assert spindle._port_from_unit("svc") == 8115


def test_the_recreate_note_is_silent_when_both_values_came_from_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    unit = tmp_path / "svc.service"
    unit.write_text("body")
    spindle._write_service_record("svc", 8115, "/srv/store", unit, "body")
    unit.unlink()
    _, _, notes_argv, _ = spindle._resolve_service_settings(unit, 9001, "/srv/new", name="svc")
    assert notes_argv == []
    _, _, notes_bare, _ = spindle._resolve_service_settings(unit, None, None, name="svc")
    assert notes_bare


def test_service_files_are_written_utf8_under_a_c_locale(tmp_path):
    """The suite's own locale hides this; PEP 540 turns UTF-8 mode on for C."""
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys, pathlib\n"
        f"sys.path.insert(0, {str(Path(spindle.__file__).parent.parent)!r})\n"
        "import spindle\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "content = spindle._systemd_unit_text('/bin/spindle', 8115, home='/srv/caf\\u00e9')\n"
        "p.write_text(content, encoding='utf-8')\n"
        "print(spindle._digest_text(content) == spindle._service_file_digest(p))\n"
    )
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "svc.service")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("True"), proc.stdout


class TestExcerptPrecedenceAndLeaks:
    """Round 12: two regressions from the previous commit's filter, and the leak."""

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def _blocker(self, unit):
        return spindle._resolve_service_settings(unit, None, None, name="svc")[3]

    def test_a_bare_environment_reset_is_shown(self, config, tmp_path):
        """It clears everything above it; hiding it shows a store nothing uses."""
        unit = tmp_path / "svc.service"
        unit.write_text(
            "[Service]\nEnvironment=SPINDLE_HOME=/data/store\nEnvironment=\nExecStart=/b serve --http --port 8115\n"
        )
        blocker = self._blocker(unit)
        assert blocker.count("Environment=") >= 2
        assert "/data/store" in blocker
        assert "clears every assignment above" in blocker

    def test_a_secret_sharing_a_line_with_the_store_is_hidden(self, config, tmp_path):
        """systemd allows several assignments per line; the filter must be per assignment."""
        unit = tmp_path / "svc.service"
        unit.write_text(
            '[Service]\nEnvironment="SPINDLE_HOME=/srv/store" "OPENAI_API_KEY=sk-do-not-print"\n'
            "ExecStart=/b serve --http --port 8115\n"
        )
        blocker = self._blocker(unit)
        assert "sk-do-not-print" not in blocker
        assert "OPENAI_API_KEY" not in blocker
        assert "/srv/store" in blocker

    def test_a_secret_on_a_continuation_of_the_store_line_is_hidden(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/store" \\\n    "OPENAI_API_KEY=sk-continued"\n')
        assert "sk-continued" not in self._blocker(unit)


class TestPortProbeVerifiesTheRecord:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def _install(self, port):
        unit = spindle._unit_file_path("svc")
        unit.parent.mkdir(parents=True, exist_ok=True)
        body = spindle._systemd_unit_text("/bin/spindle", port, name="svc")
        unit.write_text(body)
        spindle._write_service_record("svc", port, None, unit, body)
        return unit

    def test_an_intact_record_supplies_the_port(self, config):
        self._install(8115)
        assert spindle._port_from_unit("svc") == 8115

    def test_a_stale_record_does_not(self, config):
        """reload would otherwise probe the port the service used to bind."""
        unit = self._install(8115)
        unit.write_text(
            unit.read_text().replace("--port 8115", "--port 9001").replace("SPINDLE_PORT=8115", "SPINDLE_PORT=9001")
        )
        assert spindle._port_from_unit("svc") == 9001


class TestNumericEscapesMatchSystemd:
    """Verified on live units: the value each line gives a service's environment."""

    CASES = [
        ("V=/srv/a\\x20b", "/srv/a b"),
        ("V=/srv/a\\101b", "/srv/aAb"),
        ("V=/srv/a\\u0041b", "/srv/aAb"),
        ("V=/srv/a\\U00000041b", "/srv/aAb"),
        ("V=/srv/a\\sb", "/srv/a b"),
    ]

    @pytest.mark.parametrize("line,expected", CASES)
    def test_decoded_like_systemd(self, line, expected):
        assert dict(spindle._parse_systemd_env_line(line))["V"] == expected


class TestMalformedLinesAreNotEchoed:
    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        return tmp_path

    def _blocker(self, unit):
        return spindle._resolve_service_settings(unit, None, None, name="svc")[3]

    def test_an_unterminated_quote_does_not_leak(self, config, tmp_path):
        """Returning the raw line re-opened the leak the redactor exists to close."""
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment=OPENAI_API_KEY="sk-proj-LEAKME\nExecStart=/b --port 1\n')
        blocker = self._blocker(unit)
        assert "sk-proj-LEAKME" not in blocker
        assert "systemd drops it" in blocker

    def test_the_shown_assignment_keeps_the_files_own_text(self, config, tmp_path):
        """Not decoded and re-encoded — that changed what the assignment meant."""
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/a\\\\b"\n')
        blocker = self._blocker(unit)
        assert "SPINDLE_HOME=/srv/a\\\\b" in blocker


def test_port_probe_digests_the_file_the_record_names(tmp_path, monkeypatch):
    """A launchd record names a plist; digesting an assumed unit path never matched."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    plist = tmp_path / "com.svc.server.plist"
    body = spindle._launchd_plist_text("com.svc.server", "/bin/spindle", 8115, name="svc")
    plist.write_text(body)
    spindle._write_service_record("svc", 8115, None, plist, body)
    assert spindle._port_from_unit("svc") == 8115


class TestContinuedEnvironmentFile:
    def test_it_is_still_shown(self, tmp_path):
        """`EnvironmentFile\\` + newline + `=/path` is one directive to systemd."""
        unit = tmp_path / "svc.service"
        unit.write_text("[Service]\nEnvironmentFile \\\n    =/etc/spindle/env\nExecStart=/b --port 1\n")
        excerpt, _ = spindle._service_file_excerpt(unit)
        assert "/etc/spindle/env" in excerpt


class TestPartialLinesFollowSystemd:
    """systemd drops the bad word and everything after it, keeping what parsed."""

    def test_an_assignment_before_a_bad_quote_survives(self):
        assert spindle._env_from_unit_text('Environment=V=/srv/kept "X=open\n', "V") == "/srv/kept"

    def test_an_assignment_before_a_bad_escape_survives(self):
        assert spindle._env_from_unit_text("Environment=V=/srv/kept X=/x\\x2\n", "V") == "/srv/kept"

    def test_the_bad_word_itself_is_dropped(self):
        assert spindle._env_from_unit_text('Environment=V=/srv/kept "X=open\n', "X") is None

    def test_the_store_is_still_shown_when_its_line_breaks_later(self, tmp_path, monkeypatch):
        """Hiding it made the refusal claim the service was on the default store."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        unit = tmp_path / "svc.service"
        unit.write_text('[Service]\nEnvironment=SPINDLE_HOME=/srv/s OTHER="sk-LEAK\n')
        blocker = spindle._resolve_service_settings(unit, None, None, name="svc")[3]
        assert "/srv/s" in blocker
        assert "sk-LEAK" not in blocker


def test_shown_assignments_are_not_decoded(tmp_path, monkeypatch):
    """Values are shown as the file writes them; decoding was where every round went wrong."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    unit = tmp_path / "svc.service"
    unit.write_text('[Service]\nEnvironment="SPINDLE_HOME=/srv/a\\x20b" "SPINDLE_ALT=%h/x"\n')
    blocker = spindle._resolve_service_settings(unit, None, None, name="svc")[3]
    assert "/srv/a\\x20b" in blocker  # not "/srv/a b"
    assert "%h/x" in blocker  # not "%%h/x", and not expanded


class TestMalformedTailKeepsWhatSystemdKeeps:
    """The fallback used to re-split naively and falsify spindle's own values."""

    def _shown(self, rhs):
        return spindle._redact_foreign_assignments("Environment=" + rhs)

    def test_a_quoted_store_before_the_break_survives(self):
        """Cutting at the first quote showed an empty store for a real one."""
        shown = self._shown('SPINDLE_HOME="/srv/spindle store" SPINDLE_PORT=7 KEY="sk-LEAK')
        assert "/srv/spindle store" in shown
        assert "SPINDLE_PORT=7" in shown
        assert "sk-LEAK" not in shown

    def test_an_escaped_quote_does_not_truncate(self):
        shown = self._shown('SPINDLE_HOME=/srv/a\\"b SPINDLE_PORT=1 KEY="sk-LEAK')
        assert '/srv/a\\"b' in shown
        assert "SPINDLE_PORT=1" in shown
        assert "sk-LEAK" not in shown

    def test_a_quoted_foreign_assignment_ahead_of_the_break_is_counted_not_shown(self):
        shown = self._shown("TZ='UTC' SPINDLE_HOME=/srv/store KEY=\"sk-LEAK")
        assert "/srv/store" in shown
        assert "UTC" not in shown
        assert "1 other assignment(s) hidden" in shown

    def test_an_escaped_space_voids_the_line(self):
        """systemd rejects `\\ ` and drops the line; merging the words leaked the next one."""
        shown = self._shown("SPINDLE_HOME=/srv/a\\ OPENAI_API_KEY=sk-SECRET1")
        assert "sk-SECRET1" not in shown

    def test_a_balanced_line_has_no_truncation_warning(self):
        shown = self._shown('SPINDLE_HOME="/srv/a b" OTHER=x')
        assert "/srv/a b" in shown
        assert "systemd drops it" not in shown


class TestRedactionMatchesOnTheVariableName:
    def test_a_spindle_prefixed_non_assignment_is_not_shown(self):
        """`SPINDLE_TOKEN_sk-secret` is not an assignment; printing it is the leak."""
        assert spindle._redact_foreign_assignments("Environment=SPINDLE_TOKEN_sk-secret") is None

    def test_a_real_assignment_is_shown(self):
        shown = spindle._redact_foreign_assignments("Environment=SPINDLE_HOME=/srv/store")
        assert "/srv/store" in shown

    def test_a_word_without_an_equals_does_not_count_as_ours(self):
        shown = spindle._redact_foreign_assignments("Environment=SPINDLE_HOME=/srv/store SPINDLE_BARE")
        assert "/srv/store" in shown
        assert "SPINDLE_BARE" not in shown


def test_a_continuation_across_a_comment_is_still_one_directive(tmp_path, monkeypatch):
    """systemd skips comment lines inside a continuation; the caveat must too."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    unit = tmp_path / "svc.service"
    unit.write_text("[Service]\nEnvironmentFile \\\n# a comment in the middle\n    =/etc/spindle/env\nExecStart=/b\n")
    blocker = spindle._resolve_service_settings(unit, None, None, name="svc")[3]
    assert "applies after Environment=" in blocker


class TestEveryRejectedEscapeIsABreak:
    """`\\ ` was one member of a class; systemd rejects any escape outside its table."""

    def _shown(self, rhs):
        return spindle._redact_foreign_assignments("Environment=" + rhs)

    @pytest.mark.parametrize("bad", ["\\q", "\\"])
    def test_a_rejected_escape_marks_the_break(self, bad):
        shown = self._shown(f"SPINDLE_PORT=7 SPINDLE_HOME=/srv/a{bad} KEY=sk-SECRET")
        assert "SPINDLE_PORT=7" in shown  # systemd keeps what completed before
        assert "/srv/a" not in shown  # the broken assignment is not offered
        assert "sk-SECRET" not in shown

    @pytest.mark.parametrize("good", ["\\s", "\\\\", "\\x41", "\\u0041"])
    def test_an_accepted_escape_is_not_a_break(self, good):
        shown = self._shown(f"SPINDLE_HOME=/srv/a{good}b")
        assert f"/srv/a{good}b" in shown  # shown as the file writes it

    def test_a_malformed_numeric_escape_is_a_break(self):
        shown = self._shown("SPINDLE_PORT=7 SPINDLE_ALT=/srv/a\\x2 AFTER=secret")
        assert "SPINDLE_PORT=7" in shown
        assert "secret" not in shown

    def test_the_hint_parser_agrees(self):
        assert spindle._env_from_unit_text("Environment=V=/srv/kept X=/srv/a\\q\n", "V") == "/srv/kept"
        assert spindle._env_from_unit_text("Environment=V=/srv/a\\q\n", "V") is None


class TestAssignmentNamesMustBeNames:
    def test_a_name_systemd_rejects_is_not_shown(self):
        """systemd drops `SPINDLE_OTHER-BAD=...`; showing its value prints a dead line."""
        assert spindle._redact_foreign_assignments("Environment=SPINDLE_OTHER-BAD=sk-secret") is None

    def test_an_escaped_name_reads_as_foreign(self):
        """Hiding it shows less, never more — the safe direction."""
        shown = spindle._redact_foreign_assignments("Environment=\\x53PINDLE_HOME=/srv/store")
        assert shown is None or "/srv/store" not in shown

    @pytest.mark.parametrize("name", ["SPINDLE_HOME", "SPINDLE_X_1"])
    def test_ordinary_names_are_ours(self, name):
        assert spindle._is_spindle_assignment(f"{name}=/srv/x")

    @pytest.mark.parametrize("word", ["SPINDLE_TOKEN_sk", "SPINDLE-HOME=/x", "OTHER=x"])
    def test_non_assignments_and_foreign_names_are_not(self, word):
        assert not spindle._is_spindle_assignment(word)


class TestContinuationFoldsThroughEveryComment:
    def test_two_comment_lines(self):
        """re.sub does not rescan its replacement, so only the first was folded."""
        folded = spindle._join_line_continuations("Environment=FOO=bar \\\n# one\n# two\nSPINDLE_HOME=/srv/real\n")
        assert "SPINDLE_HOME=/srv/real" in folded.splitlines()[0]

    def test_a_store_after_two_comments_is_still_read(self):
        text = "Environment=FOO=bar \\\n# one\n# two\nSPINDLE_HOME=/srv/real\n"
        assert spindle._env_from_unit_text(text, "SPINDLE_HOME") == "/srv/real"

    def test_semicolon_comments_too(self):
        folded = spindle._join_line_continuations("Environment=A=1 \\\n; one\n; two\nB=2\n")
        assert "B=2" in folded.splitlines()[0]

    def test_a_continuation_with_no_comments_is_unaffected(self):
        folded = spindle._join_line_continuations("ExecStart=/b \\\n    --port 8115\n")
        assert folded.splitlines()[0].endswith("--port 8115")


def test_a_value_containing_the_marker_text_is_not_rewritten(tmp_path, monkeypatch):
    """The suffix is assembled, not patched into the finished line."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    shown = spindle._redact_foreign_assignments('Environment=SPINDLE_X="Environment=   [x"')
    assert "SPINDLE_X=Environment=   [x" in shown


class TestNumericEscapeDigitsAreDigits:
    """int() accepts surrounding whitespace; the escape then ate a word separator."""

    @pytest.mark.parametrize("bad", ["\\x2 ", "\\U0000004 "])
    def test_a_non_digit_does_not_complete_an_escape(self, bad):
        assert spindle._systemd_escape_length(bad, 0) == 0

    @pytest.mark.parametrize("good,length", [("\\x41", 4), ("\\U00000041", 10)])
    def test_real_digits_do(self, good, length):
        assert spindle._systemd_escape_length(good, 0) == length

    def test_the_separator_is_not_swallowed(self):
        shown = spindle._redact_foreign_assignments("Environment=SPINDLE_PORT=7 SPINDLE_ALT=/a\\x2 KEY=sk-SECRET")
        assert "sk-SECRET" not in shown
        assert "SPINDLE_PORT=7" in shown


class TestResolverHasNoUnreachableFallback:
    """After the refusal, only three states remain — nothing else needs a branch.

    Two review rounds were spent adding fallbacks for a fourth state that the
    record redesign had already made impossible. A mutation study proved the
    suite could not tell they were gone, which is the only reason they survived.
    """

    @pytest.fixture
    def config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        monkeypatch.delenv("SPINDLE_HOME", raising=False)
        monkeypatch.delenv("SPINDLE_PORT", raising=False)
        return tmp_path

    def test_an_existing_file_without_a_record_always_refuses(self, config, tmp_path):
        """This is what makes the fourth state impossible; pin it directly."""
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 8115, name="svc"))
        for arg_port, arg_home in [(None, None), (None, "/srv/x"), (9001, None)]:
            _, _, _, blocker = spindle._resolve_service_settings(unit, arg_port, arg_home, name="svc")
            assert blocker is not None, (arg_port, arg_home)

    def test_both_arguments_together_are_the_only_way_past_it(self, config, tmp_path):
        unit = tmp_path / "svc.service"
        unit.write_text(spindle._systemd_unit_text("/bin/spindle", 8115, name="svc"))
        port, home, _, blocker = spindle._resolve_service_settings(unit, 9001, "/srv/x", name="svc")
        assert (port, home, blocker) == (9001, "/srv/x", None)

    def test_every_reachable_state_is_covered(self, config, tmp_path):
        """Explicit argument, usable record, or no file — exhaustively."""
        unit = tmp_path / "svc.service"
        body = spindle._systemd_unit_text("/bin/spindle", 8115, home="/srv/rec", name="svc")

        # no file at all
        assert spindle._resolve_service_settings(unit, None, None, name="svc")[0] == spindle.DEFAULT_PORT

        # usable record
        unit.write_text(body)
        spindle._write_service_record("svc", 8115, "/srv/rec", unit, body)
        port, home, _, blocker = spindle._resolve_service_settings(unit, None, None, name="svc")
        assert (port, home, blocker) == (8115, "/srv/rec", None)

        # explicit arguments win over it
        assert spindle._resolve_service_settings(unit, 9001, "/srv/x", name="svc")[:2] == (9001, "/srv/x")
