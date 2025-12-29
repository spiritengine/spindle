"""Tests for Spindle MCP server."""

import json
import multiprocessing
import os
import tempfile
import time
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
    _parse_duration,
    _spool_lock,
    _check_and_finalize_spool,
    _get_output_path,
    PERMISSION_PROFILES,
    SPINDLE_DIR,
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
    with patch.object(spindle, 'SPINDLE_DIR', tmp_path):
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
            p1 = multiprocessing.Process(
                target=_finalize_worker,
                args=(str(tmp_path), spool_id, result_queue)
            )
            p2 = multiprocessing.Process(
                target=_finalize_worker,
                args=(str(tmp_path), spool_id, result_queue)
            )

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

        with patch.object(spindle, 'SPINDLE_DIR', tmp_path):
            # Hold the lock
            with _spool_lock(spool_id, blocking=True) as acquired:
                assert acquired is True

                # Try to finalize - should return False immediately
                result = _check_and_finalize_spool(spool_id)
                assert result is False

            # Now without lock, it should work (though may error since no output)
            # The key is it doesn't block or corrupt
