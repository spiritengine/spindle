"""Tests for Spindle MCP server."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the module to test
from spindle import (
    _resolve_permission,
    _get_spool_path,
    _write_spool,
    _read_spool,
    _is_pid_alive,
    PERMISSION_PROFILES,
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
        import os

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
