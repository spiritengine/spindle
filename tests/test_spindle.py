"""Tests for Spindle MCP server."""

import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
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
    _cleanup_shard,
    PERMISSION_PROFILES,
    SPINDLE_DIR,
    _try_reserve_slot_and_create,
    _count_running,
    _list_spools,
    PERMISSION_PROFILES,
    MAX_CONCURRENT,
    _spawn_shard,
    # Gemini harness functions
    GEMINI_DEFAULT_MODEL,
    GEMINI_MODEL_ALIASES,
    _gemini_spin_sync,
    _gemini_unspool_sync,
    _check_and_finalize_gemini_spool,
    _cleanup_gemini_script,
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
        assert _parse_duration("1441m") is None   # 1 minute over 24h
        assert _parse_duration("25h") is None     # 1 hour over
        assert _parse_duration("999999s") is None # Large overflow

    def test_parse_accepts_boundary_values(self):
        """Values at the boundaries should work correctly."""
        assert _parse_duration("1s") == 1         # Minimum
        assert _parse_duration("86400s") == 86400 # Maximum (24 hours)
        assert _parse_duration("1440m") == 86400  # 24 hours in minutes
        assert _parse_duration("24h") == 86400    # 24 hours


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
            assert success_count + failure_count == num_threads, \
                f"Expected {num_threads} results, got {success_count + failure_count}"

            # We started with initial_running, so only (MAX_CONCURRENT - initial_running)
            # new slots should be available
            max_new_slots = MAX_CONCURRENT - initial_running

            assert success_count == max_new_slots, \
                f"Expected exactly {max_new_slots} successful reservations, got {success_count}"

            # The rest should have been rejected
            expected_failures = num_threads - max_new_slots
            assert failure_count == expected_failures, \
                f"Expected {expected_failures} rejections, got {failure_count}"

            # Verify we never exceeded the limit by checking total running
            all_spools = _list_spools()
            running_count = sum(1 for s in all_spools if s.get("status") == "running")
            assert running_count == MAX_CONCURRENT, \
                f"Expected exactly {MAX_CONCURRENT} running spools, got {running_count}"

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

    @patch('spindle.subprocess.run')
    @patch('spindle.logger')
    def test_cleanup_shard_logs_worktree_removal_failure(self, mock_logger, mock_run):
        """Failed worktree removal should be logged and return False."""
        # Mock subprocess to return error for worktree removal
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "fatal: worktree not found"
        mock_run.return_value = mock_result

        shard_info = {
            "worktree_path": "/tmp/test-worktree",
            "branch_name": "test-branch"
        }

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        assert success is False
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "Failed to remove worktree" in error_msg
        assert "/tmp/test-worktree" in error_msg
        assert "test123" in error_msg
        assert "fatal: worktree not found" in error_msg

    @patch('spindle.subprocess.run')
    @patch('spindle.logger')
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

        shard_info = {
            "worktree_path": "/tmp/test-worktree",
            "branch_name": "test-branch"
        }

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        # Should still succeed since worktree removal worked
        assert success is True
        # But should log warning about branch deletion
        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args_list[0][0][0]
        assert "Failed to delete branch" in warning_msg
        assert "test-branch" in warning_msg
        assert "test123" in warning_msg

    @patch('spindle.subprocess.run')
    @patch('spindle.logger')
    def test_cleanup_shard_logs_timeout(self, mock_logger, mock_run):
        """Timeout during cleanup should be logged."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("git", 30)

        shard_info = {
            "worktree_path": "/tmp/test-worktree",
            "branch_name": "test-branch"
        }

        success = _cleanup_shard(shard_info, "/tmp/repo", spool_id="test123")

        assert success is False
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "Timeout during shard cleanup" in error_msg
        assert "/tmp/test-worktree" in error_msg
        assert "test123" in error_msg

    @patch('spindle.subprocess.run')
    def test_cleanup_shard_works_without_spool_id(self, mock_run):
        """Cleanup should work without spool_id for logging."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        shard_info = {
            "worktree_path": "/tmp/test-worktree",
            "branch_name": "test-branch"
        }

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
            shard1 = _spawn_shard("test-agent-1", str(git_dir))
            shard2 = _spawn_shard("test-agent-2", str(git_dir))

        # Both should succeed
        assert shard1 is not None, "First shard creation failed"
        assert shard2 is not None, "Second shard creation failed"

        # Worktree names should be different
        shard1_id = shard1["shard_id"]
        shard2_id = shard2["shard_id"]
        assert shard1_id != shard2_id, f"Shard IDs collided: {shard1_id} == {shard2_id}"

        # Branch names should also be different
        assert shard1["branch_name"] != shard2["branch_name"], \
            f"Branch names collided: {shard1['branch_name']} == {shard2['branch_name']}"

        # Verify both worktrees exist
        assert Path(shard1["worktree_path"]).exists(), f"Worktree 1 doesn't exist: {shard1['worktree_path']}"
        assert Path(shard2["worktree_path"]).exists(), f"Worktree 2 doesn't exist: {shard2['worktree_path']}"

        # Cleanup - remove worktrees
        subprocess.run(["git", "worktree", "remove", shard1["worktree_path"]], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "worktree", "remove", shard2["worktree_path"]], cwd=git_dir, capture_output=True)


class TestGeminiHarness:
    """Test Gemini harness implementation."""

    def test_gemini_default_model(self):
        """Gemini default model should be set."""
        assert GEMINI_DEFAULT_MODEL == "gemini-2.0-flash"

    def test_gemini_model_aliases(self):
        """Model aliases should resolve to full model names."""
        assert GEMINI_MODEL_ALIASES["flash"] == "gemini-2.0-flash"
        assert GEMINI_MODEL_ALIASES["pro"] == "gemini-1.5-pro"
        assert GEMINI_MODEL_ALIASES["flash-lite"] == "gemini-2.0-flash-lite"
        assert GEMINI_MODEL_ALIASES["2.5-flash"] == "gemini-2.5-flash"
        assert GEMINI_MODEL_ALIASES["1.5-flash"] == "gemini-1.5-flash"

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

    def test_gemini_spin_requires_api_key(self, tmp_path):
        """Gemini spin should require API key."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            # Clear any existing API key
            with patch.dict(os.environ, {}, clear=True):
                result = _gemini_spin_sync(
                    prompt="Test prompt",
                    working_dir=str(tmp_path),
                    model=None,
                    system_prompt=None,
                    timeout=None,
                    tags=None,
                    env=None,
                )
                assert "GOOGLE_API_KEY" in result or "GEMINI_API_KEY" in result

    def test_gemini_spin_creates_spool(self, tmp_path):
        """Gemini spin should create spool record."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
                    with patch("spindle._count_running", return_value=0):
                        result = _gemini_spin_sync(
                            prompt="Test prompt",
                            working_dir=str(tmp_path),
                            model="flash",
                            system_prompt="Be helpful",
                            timeout=60,
                            tags="test,gemini",
                            env=None,
                        )

            # Result should be a spool_id
            assert result.startswith("gemini-")

            # Spool file should exist
            spool_files = list(tmp_path.glob("gemini-*.json"))
            assert len(spool_files) == 1

            # Read spool and verify contents
            with open(spool_files[0]) as f:
                spool = json.load(f)

            assert spool["harness"] == "gemini"
            assert spool["prompt"] == "Test prompt"
            assert spool["model"] == "gemini-2.0-flash"  # alias resolved
            assert spool["system_prompt"] == "Be helpful"
            assert spool["timeout"] == 60
            assert "gemini" in spool["tags"]
            assert "test" in spool["tags"]
            assert spool["status"] == "running"

    def test_gemini_spin_creates_script_file(self, tmp_path):
        """Gemini spin should create temporary Python script."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch.dict(os.environ, {"GOOGLE_API_KEY": "test-key"}):
                    with patch("spindle._count_running", return_value=0):
                        spool_id = _gemini_spin_sync(
                            prompt="Test prompt",
                            working_dir=str(tmp_path),
                            model=None,
                            system_prompt=None,
                            timeout=None,
                            tags=None,
                            env=None,
                        )

            # Script file should exist
            script_path = tmp_path / f"{spool_id}.py"
            assert script_path.exists()

            # Script should contain google.genai import
            script_content = script_path.read_text()
            assert "from google import genai" in script_content
            assert "genai.Client" in script_content

    def test_gemini_unspool_nonexistent(self, tmp_path):
        """Unspool should return error for nonexistent spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            result = _gemini_unspool_sync("gemini-nonexistent")
            assert "Unknown spool_id" in result

    def test_gemini_unspool_complete(self, tmp_path):
        """Unspool should return result for complete spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-test123"
            spool = {
                "id": spool_id,
                "status": "complete",
                "result": "Test response from Gemini",
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _gemini_unspool_sync(spool_id)
            assert result == "Test response from Gemini"

    def test_gemini_unspool_error(self, tmp_path):
        """Unspool should return error message for failed spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-test456"
            spool = {
                "id": spool_id,
                "status": "error",
                "error": "API key invalid",
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _gemini_unspool_sync(spool_id)
            assert "failed" in result
            assert "API key invalid" in result

    def test_gemini_unspool_timeout(self, tmp_path):
        """Unspool should return timeout message for timed out spool."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-test789"
            spool = {
                "id": spool_id,
                "status": "timeout",
                "error": "Timeout after 60s",
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            result = _gemini_unspool_sync(spool_id)
            assert "timed out" in result

    def test_gemini_finalize_parses_json_output(self, tmp_path):
        """Finalize should parse JSON output correctly."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-finalize1"

            # Create running spool
            spool = {
                "id": spool_id,
                "status": "running",
                "pid": 999999999,  # Non-existent PID
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            # Create stdout with valid JSON output
            stdout_path = tmp_path / f"{spool_id}.stdout"
            stdout_data = {
                "result": "Hello from Gemini!",
                "model": "gemini-2.0-flash",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                }
            }
            stdout_path.write_text(json.dumps(stdout_data))

            # Finalize
            _check_and_finalize_gemini_spool(spool_id)

            # Read spool and verify
            finalized = _read_spool(spool_id)
            assert finalized["status"] == "complete"
            assert finalized["result"] == "Hello from Gemini!"
            assert finalized["cost"]["prompt_tokens"] == 10

    def test_gemini_finalize_handles_error_output(self, tmp_path):
        """Finalize should handle error in JSON output."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-finalize2"

            # Create running spool
            spool = {
                "id": spool_id,
                "status": "running",
                "pid": 999999999,
                "harness": "gemini",
                "created_at": datetime.now().isoformat(),
            }
            _write_spool(spool_id, spool)

            # Create stdout with error output
            stdout_path = tmp_path / f"{spool_id}.stdout"
            stdout_path.write_text(json.dumps({"error": "Invalid API key"}))

            # Finalize
            _check_and_finalize_gemini_spool(spool_id)

            # Read spool and verify
            finalized = _read_spool(spool_id)
            assert finalized["status"] == "error"
            assert finalized["error"] == "Invalid API key"

    def test_gemini_cleanup_script(self, tmp_path):
        """Cleanup should remove script file."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            spool_id = "gemini-cleanup1"
            script_path = tmp_path / f"{spool_id}.py"
            script_path.write_text("# test script")

            assert script_path.exists()
            _cleanup_gemini_script(spool_id)
            assert not script_path.exists()

    def test_gemini_spin_uses_env_api_key(self, tmp_path):
        """Gemini spin should use API key from env parameter."""
        with patch("spindle.SPINDLE_DIR", tmp_path):
            with patch("spindle._spawn_detached", return_value=12345):
                with patch("spindle._count_running", return_value=0):
                    # Don't set env var, but pass in env dict
                    with patch.dict(os.environ, {}, clear=True):
                        result = _gemini_spin_sync(
                            prompt="Test prompt",
                            working_dir=str(tmp_path),
                            model=None,
                            system_prompt=None,
                            timeout=None,
                            tags=None,
                            env={"GOOGLE_API_KEY": "key-from-env-param"},
                        )

            # Should succeed with API key from env param
            assert result.startswith("gemini-")
