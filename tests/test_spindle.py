"""Tests for Spindle MCP server."""

import asyncio
import json
import multiprocessing
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import the module to test
from spindle import (
    GEMINI_MODEL_ALIASES,
    KIMI_MODEL_ALIASES,
    MAX_CONCURRENT,
    PENDING_SPAWN_TIMEOUT,
    PERMISSION_PROFILES,
    _check_and_finalize_spool,
    _cleanup_shard,
    _codex_respin_sync,
    _codex_spin_sync,
    _count_running,
    _detect_default_branch,
    _detect_existing_shard,
    _gemini_spin_sync,
    _gemini_unspool_sync,
    _get_harnesses,
    _get_spool_path,
    _is_pid_alive,
    _kimi_respin_sync,
    _kimi_spin_sync,
    _kimi_unspool_sync,
    _list_spools,
    _parse_duration,
    _read_spool,
    _recover_orphans,
    _resolve_permission,
    _spawn_shard,
    _spin_sync,
    _spool_lock,
    _try_reserve_slot_and_create,
    _write_spool,
    shard_abandon,
    shard_merge,
    spin,
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
        assert cmd[cd_idx + 1] == str(worktree_path)


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
