"""Codex containment: what each tier launches, what a shard grants, and fail-closed startup.

Release audit finding-20261103-i0gq items M8 and M10:

- issue-20260903-58ex: `codex exec` refuses to start outside a git repo, so the doctor smoke
  (which runs in a temp dir) failed its codex leg on every machine. PR 1's fix: pass
  `--skip-git-repo-check` on spin and resume.
- issue-20260903-zigp: Spindle pinned the sandbox tier but not the approval policy, so an
  operator's automatic reviewer could approve commands out of a restrictive sandbox.
- issue-20261022-zebt: inside a shard, codex's own sandbox failed before every command
  (`bwrap: Can't mkdir <repo>/.git/.git: Read-only file system`), and the spool silently ran
  each command through escalation instead.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import spindle
from spindle import (
    _codex_bwrap_wrap,
    _codex_respin_sync,
    _codex_sandbox_for_permission,
    _codex_spin_sync,
    _read_spool,
    _write_spool,
)

FAKE_CODEX = "/fake/bin/codex"
SPINDLE_SOURCE_ROOT = str(Path(spindle.__file__).resolve().parent.parent)
# `patch("spindle.shutil.which")` replaces the shared shutil.which, so keep the real one.
_REAL_WHICH = shutil.which
_REAL_VALIDATE_RESEARCH_TARGET = spindle._validate_research_target


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_worktree(root: Path, name: str = "shard-codex-b"):
    """A real repo with one commit and a linked worktree at <repo>/worktrees/<name>.

    This is the layout Spindle's shards use: the worktree's `.git` is a file pointing at
    `<repo>/.git/worktrees/<name>`.
    """
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    worktree = repo / "worktrees" / name
    worktree.parent.mkdir()
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", name)
    shard_info = {"worktree_path": str(worktree), "branch_name": name, "shard_id": name}
    return repo, worktree, shard_info


def _main_git_write_paths(repo: Path, name: str):
    git = repo / ".git"
    return [
        str(git / "worktrees" / name),
        str(git / "objects"),
        str(git / "refs" / "heads"),
        str(git / "logs" / "refs" / "heads"),
    ]


def _config_value(cmd, key):
    """The value of the last `-c key=value` override in an argv, or None."""
    value = None
    for index, token in enumerate(cmd[:-1]):
        if token == "-c" and cmd[index + 1].startswith(f"{key}="):
            value = cmd[index + 1].split("=", 1)[1]
    return value


def _toml_list(value):
    return tomllib.loads(f"v = {value}")["v"]


def _add_dirs(cmd):
    return [cmd[index + 1] for index, token in enumerate(cmd) if token == "--add-dir"]


def _outer_writable_binds(cmd, codex_index):
    """Writable `--bind SRC DEST` pairs in the bwrap prefix before the codex binary."""
    prefix = cmd[:codex_index]
    return [prefix[i + 1] for i in range(len(prefix) - 2) if prefix[i] == "--bind" and prefix[i + 1] == prefix[i + 2]]


def _is_within(path, roots):
    path = os.path.realpath(path)
    for root in roots:
        root = os.path.realpath(root)
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _bwrap_only(name, path=None):
    """shutil.which stand-in: bwrap always resolves; everything else resolves as usual."""
    if name == "bwrap":
        return "/usr/bin/bwrap"
    return _REAL_WHICH(name, path=path)


def _validate_without_skein(research_target, working_dir):
    """site: validation asks a live SKEIN; file:/dir: validation stays real."""
    if research_target and research_target.startswith("site:"):
        return spindle._parse_research_target(research_target)
    return _REAL_VALIDATE_RESEARCH_TARGET(research_target, working_dir)


@contextmanager
def _codex_launch(tmp_path, *, shard_info=None, shard_start_failure=None):
    """Run a codex launch with a stable fake binary and capture the spawned argv.

    The read-only enforcement probe is stubbed to "enforcing", bwrap always resolves, and
    the shard sandbox start probe is stubbed (default: it starts). Yields (captured argv
    list, the shard start probe mock).
    """
    captured = []

    def fake_detached(spool_id, cmd, cwd, env=None):
        captured.append(list(cmd))
        return 99999

    start_probe = MagicMock(return_value=shard_start_failure)
    with patch("spindle.SPINDLE_DIR", tmp_path / "state"):
        (tmp_path / "state").mkdir(exist_ok=True)
        with (
            patch("spindle._count_running", return_value=0),
            patch("spindle._has_skein", return_value=False),
            patch("spindle._resolve_codex_binary", return_value=FAKE_CODEX),
            patch("spindle._codex_cli_version", return_value="0.154.0"),
            patch("spindle._codex_auth_mode", return_value="chatgpt"),
            patch("spindle._codex_sandbox_enforces", return_value=True),
            patch("spindle._codex_shard_sandbox_start_failure", start_probe, create=True),
            patch("spindle._spawn_shard", return_value=(shard_info, None)),
            patch("spindle._spawn_detached", side_effect=fake_detached),
            patch("spindle.shutil.which", side_effect=_bwrap_only),
            patch("spindle._validate_research_target", side_effect=_validate_without_skein),
        ):
            yield captured, start_probe


def _spin(tmp_path, permission, *, research_target=None, shard=False, working_dir=None):
    sandbox = _codex_sandbox_for_permission(permission, research_target)
    return _codex_spin_sync(
        "do work",
        str(working_dir or tmp_path),
        None,
        sandbox,
        None,
        None,
        None,
        shard=shard,
        research_target=research_target,
        require_research_target=permission in {"research", "research+shard"},
        permission=permission,
    )


def _respin(tmp_path, original):
    _write_spool(original["id"], original)
    return _codex_respin_sync(original["session_id"], "follow up")


def _original(tmp_path, *, sandbox, permission, shard=None, working_dir=None, research_target=None):
    return {
        "id": f"codex-orig-{permission or 'none'}".replace("+", "-"),
        "status": "complete",
        "session_id": f"sess-{permission or 'none'}-{sandbox}",
        "working_dir": str(working_dir or tmp_path),
        "sandbox": sandbox,
        "permission": permission,
        "research_target": research_target,
        "shard": shard,
        "harness": "codex",
        "tags": ["codex"],
    }


# --------------------------------------------------------------------------------------
# issue-20260903-58ex: codex refuses to start outside a git repo (PR 1)
# --------------------------------------------------------------------------------------


class TestSkipGitRepoCheck:
    """A spool's working dir need not be a repo, and codex exits there without the flag.

    codex 0.154.0 (`exec/src/lib.rs`) exits with "Not inside a trusted directory and
    --skip-git-repo-check was not specified" when the cwd has no git root. The check gates
    startup only; it changes no sandbox policy. Adopted from PR 1
    (fix/codex-skip-git-repo-check).
    """

    @pytest.mark.parametrize("permission", ["readonly", "careful", "full", "research", None])
    def test_spin_passes_skip_git_repo_check(self, tmp_path, permission):
        research_target = "site:spindle-development" if permission == "research" else None
        with _codex_launch(tmp_path) as (captured, _):
            _spin(tmp_path, permission, research_target=research_target)

        assert captured, "the spool must spawn"
        assert "--skip-git-repo-check" in captured[0], captured[0]

    @pytest.mark.parametrize("sandbox", ["read-only", "workspace-write", "danger-full-access"])
    def test_respin_passes_skip_git_repo_check_before_resume(self, tmp_path, sandbox):
        permission = {"read-only": "readonly", "workspace-write": "careful", "danger-full-access": "full"}[sandbox]
        with _codex_launch(tmp_path) as (captured, _):
            _respin(tmp_path, _original(tmp_path, sandbox=sandbox, permission=permission))

        cmd = captured[0]
        assert "--skip-git-repo-check" in cmd, cmd
        assert cmd.index("--skip-git-repo-check") < cmd.index("resume"), cmd

    def test_doctor_smoke_codex_spool_passes_skip_git_repo_check(self, tmp_path):
        """The smoke runs in a mkdtemp dir, the exact case that failed on every machine."""
        smoke_dir = tmp_path / "spindle-doctor-smoke"
        smoke_dir.mkdir()
        with _codex_launch(tmp_path) as (captured, _):
            spindle._doctor_smoke_spin("codex", str(smoke_dir), None, 60)

        assert "--skip-git-repo-check" in captured[0], captured[0]


# --------------------------------------------------------------------------------------
# issue-20260903-zigp: a sandbox denial must stay a denial
# --------------------------------------------------------------------------------------


class TestApprovalPolicyPin:
    """Every restrictive tier pins approval_policy=never; danger-full-access does not.

    With `approval_policy = "on-request"` and `approvals_reviewer = "auto_review"` in
    ~/.codex/config.toml, codex exec drops its headless "never" default and lets a reviewer
    model approve a command out of the sandbox (seen in codex-480d49c8). Under "never",
    codex 0.154.0 rejects a `require_escalated` request outright and never retries a
    sandbox-denied command unsandboxed. danger-full-access has no sandbox to escape, so
    escalation cannot widen it and the operator's own policy applies.
    """

    @pytest.mark.parametrize(
        "permission,research_kind,pinned",
        [
            ("readonly", None, True),
            ("manual", None, True),
            ("careful", None, True),
            (None, None, True),
            ("research", "site", True),
            ("research", "file", True),
            ("full", None, False),
        ],
    )
    def test_spin_pins_approval_for_restrictive_tiers(self, tmp_path, permission, research_kind, pinned):
        research_target = None
        if research_kind == "site":
            research_target = "site:spindle-development"
        elif research_kind == "file":
            (tmp_path / "out").mkdir()
            research_target = f"file:{tmp_path / 'out' / 'report.md'}"
        with _codex_launch(tmp_path) as (captured, _):
            _spin(tmp_path, permission, research_target=research_target)

        cmd = captured[0]
        if pinned:
            assert _config_value(cmd, "approval_policy") == "never", cmd
        else:
            assert _config_value(cmd, "approval_policy") is None, cmd

    @pytest.mark.parametrize(
        "permission,research_target,pinned",
        [
            ("careful+shard", None, True),
            ("shard", None, True),  # MCP spin maps shard to workspace-write
            ("research+shard", "site:spindle-development", True),
            ("full", None, False),  # full with shard=True stays danger-full-access
        ],
    )
    def test_shard_spin_pins_approval_for_restrictive_tiers(self, tmp_path, permission, research_target, pinned):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        with _codex_launch(tmp_path, shard_info=shard_info) as (captured, _):
            _spin(tmp_path, permission, research_target=research_target, shard=True, working_dir=repo)

        cmd = captured[0]
        assert cmd[0] == "/usr/bin/bwrap", "a shard spool runs inside the bwrap wrapper"
        if pinned:
            assert _config_value(cmd, "approval_policy") == "never", cmd
        else:
            assert _config_value(cmd, "approval_policy") is None, cmd

    @pytest.mark.parametrize(
        "sandbox,permission,pinned",
        [
            ("read-only", "readonly", True),
            ("workspace-write", "careful", True),
            ("danger-full-access", "full", False),
        ],
    )
    def test_respin_pins_approval_before_resume(self, tmp_path, sandbox, permission, pinned):
        with _codex_launch(tmp_path) as (captured, _):
            _respin(tmp_path, _original(tmp_path, sandbox=sandbox, permission=permission))

        cmd = captured[0]
        if pinned:
            assert _config_value(cmd, "approval_policy") == "never", cmd
            pin_index = cmd.index("approval_policy=never")
            assert pin_index < cmd.index("resume"), f"-c must precede the resume subcommand: {cmd!r}"
        else:
            assert _config_value(cmd, "approval_policy") is None, cmd

    def test_tier_flags_still_agree(self, tmp_path):
        """The approval pin joins, and does not replace, the --sandbox / sandbox_mode pair."""
        with _codex_launch(tmp_path) as (captured, _):
            _spin(tmp_path, "readonly")

        cmd = captured[0]
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert _config_value(cmd, "sandbox_mode") == "read-only"
        assert "--full-auto" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


# --------------------------------------------------------------------------------------
# issue-20261022-zebt: codex's sandbox must be able to start inside the shard wrapper
# --------------------------------------------------------------------------------------


class TestShardWritableRoots:
    """Codex's writable roots inside a shard are exactly what the wrapper makes writable.

    codex 0.154.0 masks `.git`, `.agents` and `.codex` under every writable root it is given
    (linux-sandbox/src/bwrap.rs: a missing one gets a read-only tmpfs, so bwrap must mkdir
    its mount point). Spindle used to hand codex the main repo's `.git` as a writable root
    while the outer wrapper kept that directory read-only, so the mkdir of `.git/.git` failed
    and no command could start. The grant is now the four git paths the wrapper binds.
    """

    def _assert_grants_mirror_wrapper(self, cmd, repo, worktree):
        name = worktree.name
        codex_index = cmd.index(FAKE_CODEX)
        grants = _add_dirs(cmd)
        main_git = str(repo / ".git")

        assert main_git not in grants, f"the main .git must not be a codex writable root: {grants!r}"
        for path in [*_main_git_write_paths(repo, name), str(worktree)]:
            assert path in grants, f"{path} missing from codex grants {grants!r}"

        # pytest's tmp_path sits under /tmp, which the wrapper binds wholesale; a real
        # project does not, so each grant must be covered by a bind of its own.
        outer = [bind for bind in _outer_writable_binds(cmd, codex_index) if bind != "/tmp"]
        for grant in grants:
            assert _is_within(grant, outer), f"codex grant {grant} is not writable in the wrapper: {outer!r}"
        assert main_git not in outer, "the wrapper must keep the main .git itself read-only"

        pinned = _config_value(cmd, "sandbox_workspace_write.writable_roots")
        assert pinned is not None, "a shard launch must pin codex's writable_roots"
        assert _toml_list(pinned) == grants, (pinned, grants)

    def test_careful_shard_spin(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        with _codex_launch(tmp_path, shard_info=shard_info) as (captured, _):
            _spin(tmp_path, "careful+shard", shard=True, working_dir=repo)

        self._assert_grants_mirror_wrapper(captured[0], repo, worktree)

    def test_careful_shard_respin_from_subdirectory(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        subdir = worktree / "pkg" / "deep"
        subdir.mkdir(parents=True)
        original = _original(
            tmp_path, sandbox="workspace-write", permission="careful+shard", shard=shard_info, working_dir=subdir
        )
        with _codex_launch(tmp_path) as (captured, _):
            _respin(tmp_path, original)

        cmd = captured[0]
        self._assert_grants_mirror_wrapper(cmd, repo, worktree)
        resume = cmd.index("resume")
        assert all(index < resume for index, token in enumerate(cmd) if token == "--add-dir"), cmd

    def test_research_shard_grants_only_its_output(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        with _codex_launch(tmp_path, shard_info=shard_info) as (captured, _):
            _spin(tmp_path, "research+shard", research_target=f"dir:{out}", shard=True, working_dir=repo)

        cmd = captured[0]
        assert _add_dirs(cmd) == [str(out)], cmd
        assert _toml_list(_config_value(cmd, "sandbox_workspace_write.writable_roots")) == [str(out)]
        outer = _outer_writable_binds(cmd, cmd.index(FAKE_CODEX))
        assert str(out) in outer
        assert str(worktree) not in outer, "research writes only its target, not the worktree"

    def test_non_shard_launch_leaves_writable_roots_alone(self, tmp_path):
        """Outside a shard there is no wrapper to mirror; the pin is shard-only."""
        with _codex_launch(tmp_path) as (captured, _):
            _spin(tmp_path, "careful")

        assert _config_value(captured[0], "sandbox_workspace_write.writable_roots") is None

    def test_writable_roots_value_is_valid_toml_for_awkward_paths(self):
        paths = ['/w/with "quote"', "/w/back\\slash", "/w/tab\there", "/w/del\x7fchar", "/w/naïve"]
        args = spindle._codex_sandbox_config_args("workspace-write", {"worktree_path": "/w"}, paths)

        assert _toml_list(_config_value(args, "sandbox_workspace_write.writable_roots")) == paths


def _remap_point():
    """An existing directory outside /tmp to remount a scratch tree at, or None.

    Spindle's wrapper binds all of /tmp writable, so a repo under pytest's tmp_path cannot
    reproduce a real project whose main `.git` the wrapper keeps read-only. Mounting the
    scratch tree over a directory outside /tmp, inside a throwaway namespace, does.
    """
    needed = [sys.executable, SPINDLE_SOURCE_ROOT, shutil.which("git") or "/usr/bin/git"]
    for candidate in ("/mnt", "/srv", "/media"):
        if not os.path.isdir(candidate):
            continue
        if any(os.path.realpath(path).startswith(candidate + "/") for path in needed):
            continue
        return candidate
    return None


_REMAPPED_SCENARIO = r"""
import json, os, subprocess, sys
from unittest.mock import MagicMock, patch

source, base, home, store, mode = sys.argv[1:6]
os.environ["SPINDLE_HOME"] = store
sys.path.insert(0, source)
import spindle

name = "shard-codex-b"
repo = os.path.join(base, "repo")
worktree = os.path.join(repo, "worktrees", name)


def git(*args, cwd=repo):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


os.makedirs(repo)
git("init", "-q")
git("config", "user.email", "t@example.com")
git("config", "user.name", "T")
with open(os.path.join(repo, "README.md"), "w") as handle:
    handle.write("hello\n")
git("add", ".")
git("commit", "-qm", "init")
os.makedirs(os.path.dirname(worktree))
git("worktree", "add", "-q", worktree, "-b", name)

shard_info = {"worktree_path": worktree, "branch_name": name, "shard_id": name}
env = dict(os.environ, HOME=home)
env.pop("CODEX_HOME", None)


def launch_grants():
    # The --add-dir grants a real careful+shard spin hands codex, read off its argv.
    captured = []

    def fake_detached(spool_id, cmd, cwd, env=None):
        captured.append(list(cmd))
        return 99999

    patches = {
        "_count_running": MagicMock(return_value=0),
        "_has_skein": MagicMock(return_value=False),
        "_resolve_codex_binary": MagicMock(return_value="/fake/bin/codex"),
        "_codex_cli_version": MagicMock(return_value="0.154.0"),
        "_codex_auth_mode": MagicMock(return_value="chatgpt"),
        "_codex_sandbox_enforces": MagicMock(return_value=True),
        "_spawn_shard": MagicMock(return_value=(shard_info, None)),
        "_spawn_detached": fake_detached,
        "_monitor_spool": MagicMock(),
        "_ensure_store_supervisor": MagicMock(return_value=(True, None)),
    }
    if hasattr(spindle, "_codex_shard_sandbox_start_failure"):
        patches["_codex_shard_sandbox_start_failure"] = MagicMock(return_value=None)
    with patch.multiple(spindle, **patches):
        result = spindle._codex_spin_sync(
            "x", repo, None, "workspace-write", None, None, None, shard=True, permission="careful+shard"
        )
    if not captured:
        raise SystemExit(f"spin did not launch: {result}")
    cmd = captured[0]
    return [cmd[i + 1] for i, token in enumerate(cmd) if token == "--add-dir"]


grants = launch_grants()
if mode == "masks":
    # The mount points codex's sandbox creates at startup: .git/.agents/.codex under each
    # writable root it is handed (its cwd, the shard worktree, plus the grants), when missing.
    lines = []
    for root in [worktree, *grants]:
        for meta in (".git", ".agents", ".codex"):
            target = os.path.join(root, meta)
            if not os.path.lexists(target):
                lines.append(f"mkdir '{target}' || exit 10")
                lines.append(f"rmdir '{target}' || exit 11")
    lines += [
        "echo change >> README.md || exit 20",
        "git add README.md || exit 21",
        "git commit -qm change || exit 22",
        f"if (echo x >> '{repo}/.git/config') 2>/dev/null; then exit 30; fi",
        f"if (echo x > '{repo}/.git/hooks/post-checkout') 2>/dev/null; then exit 31; fi",
        f"if (echo x > '{repo}/stray') 2>/dev/null; then exit 32; fi",
        "echo SCENARIO_OK",
    ]
    cmd = spindle._codex_bwrap_wrap(["/bin/sh", "-c", "\n".join(lines)], shard_info, worktree, process_env=env)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(json.dumps({"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "grants": grants}))
else:
    codex = sys.argv[6]
    env["PATH"] = os.path.dirname(codex) + os.pathsep + env.get("PATH", "")
    old = [os.path.join(repo, ".git"), worktree]
    probe = spindle._codex_shard_sandbox_start_failure
    result = {
        "fixed": probe(codex, "workspace-write", shard_info, grants, None, env),
        "old_grant": probe(codex, "workspace-write", shard_info, old, None, env),
    }
    print(json.dumps(result))
"""


def _run_remapped(tmp_path, mode, *extra):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap is not installed")
    point = _remap_point()
    if point is None:
        pytest.skip("no directory outside /tmp to remap a scratch tree onto")
    scratch = tmp_path / "remapped"
    scratch.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    base = os.path.join(point, "spindle-codex-b")
    (scratch / "spindle-codex-b").mkdir()
    proc = subprocess.run(
        [
            bwrap,
            "--bind",
            "/",
            "/",
            "--bind",
            str(scratch),
            point,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--",
            sys.executable,
            "-c",
            _REMAPPED_SCENARIO,
            SPINDLE_SOURCE_ROOT,
            base,
            str(home),
            str(store),
            mode,
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.ns_required
def test_shard_wrapper_lets_codex_sandbox_create_its_mount_points(tmp_path):
    """Run the real wrapper around what codex's sandbox does at startup, then a commit.

    For every writable root Spindle hands codex, create and remove the `.git`, `.agents` and
    `.codex` mount points codex's bwrap makes for its read-only masks. On the old grant
    (the main `.git`) this fails exactly as issue-20261022-zebt reports. The same run commits
    in the worktree, and still cannot touch the main repo's config, hooks or tree.
    """
    result = _run_remapped(tmp_path, "masks")

    assert result["rc"] == 0, result
    assert "SCENARIO_OK" in result["stdout"], result


@pytest.mark.ns_required
@pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI is not installed")
def test_real_codex_sandbox_starts_inside_shard_wrapper(tmp_path):
    """The real binary: the probe passes on Spindle's grants and catches the old grant.

    No model call; `codex sandbox` runs one shell command under the sandbox. HOME points at
    an empty scratch dir, so no operator config or credentials are read.
    """
    result = _run_remapped(tmp_path, "codex", shutil.which("codex"))

    assert result["fixed"] is None, result
    assert result["old_grant"] and ".git/.git" in result["old_grant"], result


# --------------------------------------------------------------------------------------
# Fail closed when codex's sandbox cannot start inside the shard
# --------------------------------------------------------------------------------------

_MKDIR_FAILURE = "bwrap: Can't mkdir /p/repo/.git/.git: Read-only file system"


class TestShardSandboxFailsClosed:
    def test_spin_refuses_and_spawns_nothing(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        with _codex_launch(tmp_path, shard_info=shard_info, shard_start_failure=_MKDIR_FAILURE) as (captured, _):
            result = _spin(tmp_path, "careful+shard", shard=True, working_dir=repo)
            spools = [path.stem for path in (tmp_path / "state").glob("codex-*.json")]
            record = _read_spool(spools[0]) if spools else None

        assert captured == [], "no agent may start when its sandbox cannot"
        assert result.startswith("Error: REFUSED"), result
        assert _MKDIR_FAILURE in result
        assert record is not None and record["status"] == "error", record
        assert _MKDIR_FAILURE in record["error"]
        assert record["shard"]["worktree_path"] == str(worktree)
        assert record.get("shard_cleanup_preserved") is True, "a refused new shard is kept for inspection"

    def test_respin_refuses_and_spawns_nothing(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        original = _original(tmp_path, sandbox="workspace-write", permission="careful+shard", shard=shard_info)
        with _codex_launch(tmp_path, shard_start_failure=_MKDIR_FAILURE) as (captured, _):
            result = _respin(tmp_path, original)
            refused = [
                _read_spool(path.stem)
                for path in (tmp_path / "state").glob("codex-*.json")
                if path.stem != original["id"]
            ]

        assert captured == []
        assert result.startswith("Error: REFUSED"), result
        assert len(refused) == 1 and refused[0]["status"] == "error", refused
        assert _MKDIR_FAILURE in refused[0]["error"]

    def test_probe_gets_the_launch_grants(self, tmp_path):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        with _codex_launch(tmp_path, shard_info=shard_info) as (captured, probe):
            _spin(tmp_path, "careful+shard", shard=True, working_dir=repo)

        probe.assert_called_once()
        codex_bin, sandbox, probed_shard, grants, research_info, _env = probe.call_args.args
        assert (codex_bin, sandbox, probed_shard, research_info) == (FAKE_CODEX, "workspace-write", shard_info, None)
        assert grants == _add_dirs(captured[0])

    @pytest.mark.parametrize(
        "permission,shard",
        [
            ("full", True),  # danger-full-access: codex starts no sandbox of its own
            ("careful", False),  # no wrapper, so nothing nests
        ],
    )
    def test_probe_runs_only_where_codex_sandbox_nests(self, tmp_path, permission, shard):
        repo, worktree, shard_info = _repo_with_worktree(tmp_path)
        with _codex_launch(tmp_path, shard_info=shard_info) as (captured, probe):
            _spin(tmp_path, permission, shard=shard, working_dir=repo)

        probe.assert_not_called()
        assert len(captured) == 1


class TestShardSandboxStartProbe:
    """`_codex_shard_sandbox_start_failure`: a no-model `codex sandbox` run in the wrapper."""

    @staticmethod
    def _run(tmp_path, results, sandbox="workspace-write", research_info=None):
        shard_info = {"worktree_path": str(tmp_path / "wt")}
        (tmp_path / "wt").mkdir(exist_ok=True)
        grants = [str(tmp_path / "wt")]
        runs = []

        def fake_run(cmd, **kwargs):
            runs.append((list(cmd), kwargs))
            outcome = results[len(runs) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
        with (
            patch("spindle.subprocess.run", side_effect=fake_run),
            patch("spindle.shutil.which", side_effect=_bwrap_only),
        ):
            detail = spindle._codex_shard_sandbox_start_failure(
                FAKE_CODEX, sandbox, shard_info, grants, research_info, env
            )
            expected_outer = _codex_bwrap_wrap(
                [], shard_info, shard_info["worktree_path"], research_target_info=research_info, process_env=env
            )
        return detail, runs, shard_info, grants, expected_outer

    @staticmethod
    def _done(stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def test_marker_means_started(self, tmp_path):
        marker = spindle._CODEX_SANDBOX_PROBE_MARKER
        detail, runs, shard_info, grants, expected_outer = self._run(tmp_path, [self._done(stdout=f"{marker}\n")])

        assert detail is None
        cmd, kwargs = runs[0]
        assert expected_outer and expected_outer[0] == "/usr/bin/bwrap"
        assert cmd[: len(expected_outer)] == expected_outer, "the probe runs in the launch's own wrapper"
        inner = cmd[len(expected_outer) :]
        config = spindle._codex_sandbox_config_args("workspace-write", shard_info, grants)
        assert inner[: 1 + len(config)] == [FAKE_CODEX, *config], "the probe pins what the launch pins"
        assert inner[1 + len(config)] == "sandbox"
        assert kwargs["cwd"] == shard_info["worktree_path"]
        assert kwargs["timeout"] > 0

    def test_failed_start_reports_the_bwrap_line(self, tmp_path):
        stderr = "WARNING: proceeding, even though we could not create PATH aliases\n" + _MKDIR_FAILURE + "\n"
        failed = self._done(stderr=stderr, returncode=1)
        detail, runs, _, _, _ = self._run(
            tmp_path, [failed, self._done(stderr="panic: Failed to execvp linux", returncode=101)]
        )

        assert detail == _MKDIR_FAILURE
        assert len(runs) == 2, "the older CLI shape is tried before refusing"

    def test_failed_start_reports_a_panic_message_not_the_backtrace_hint(self, tmp_path):
        """codex 0.154 with a $TMPDIR the wrapper keeps read-only panics like this."""
        message = (
            "failed to create synthetic bubblewrap mount registry "
            "/var/tmp/codex-bwrap-synthetic-mount-targets-1000: Read-only file system (os error 30)"
        )
        stderr = (
            "\nthread 'main' (3722863) panicked at linux-sandbox/src/linux_run_main.rs:1293:9:\n"
            f"{message}\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n"
        )
        failed = self._done(stderr=stderr, returncode=101)
        detail, _, _, _, _ = self._run(tmp_path, [failed, failed])

        assert detail == message

    def test_older_cli_shape_can_pass(self, tmp_path):
        marker = spindle._CODEX_SANDBOX_PROBE_MARKER
        detail, runs, _, _, _ = self._run(
            tmp_path, [self._done(stderr="error: unexpected argument", returncode=2), self._done(stdout=marker)]
        )

        assert detail is None
        assert runs[1][0][-5:-3] == ["linux", "--"]

    def test_timeout_fails_closed(self, tmp_path):
        detail, runs, _, _, _ = self._run(tmp_path, [subprocess.TimeoutExpired("codex", 30)])

        assert detail and "timed out" in detail
        assert len(runs) == 1

    def test_unrunnable_fails_closed(self, tmp_path):
        detail, _, _, _, _ = self._run(tmp_path, [OSError("No such file"), OSError("No such file")])

        assert detail and "No such file" in detail

    def test_silent_failure_still_fails_closed(self, tmp_path):
        detail, _, _, _, _ = self._run(tmp_path, [self._done(returncode=1), self._done(returncode=1)])

        assert detail and "exit 1" in detail
