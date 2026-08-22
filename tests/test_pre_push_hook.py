import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).parents[1] / ".githooks" / "pre-push"


def _run_hook(tmp_path: Path, version: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "ruff-calls"
    fake_ruff = fake_bin / "ruff"
    fake_ruff.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
  printf '%s\\n' "$FAKE_RUFF_VERSION"
  exit 0
fi
printf '%s\\n' "$*" >> "$FAKE_RUFF_CALL_LOG"
"""
    )
    fake_ruff.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FAKE_RUFF_CALL_LOG": str(call_log),
            "FAKE_RUFF_VERSION": version,
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    completed = subprocess.run(
        ["bash", str(HOOK)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed, call_log


def test_pre_push_hook_rejects_unpinned_ruff(tmp_path: Path):
    completed, call_log = _run_hook(tmp_path, "ruff 0.16.2")

    assert completed.returncode == 1
    assert "expected ruff 0.16.3, found ruff 0.16.2" in completed.stderr
    assert not call_log.exists()


def test_pre_push_hook_runs_checks_with_pinned_ruff(tmp_path: Path):
    completed, call_log = _run_hook(tmp_path, "ruff 0.16.3")

    assert completed.returncode == 0, completed.stderr
    assert call_log.read_text().splitlines() == ["format --check .", "check ."]
