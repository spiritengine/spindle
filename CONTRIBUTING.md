# Contributing to Spindle

## Development Setup

Spindle runs on Linux; the test suite does too. Use a CPython 3.11+ whose `os`
module has `pidfd_open` (see [Running Tests](#running-tests)).

```bash
# Clone the repo
git clone https://github.com/spiritengine/spindle.git
cd spindle

# Install with dev dependencies (in a virtualenv, or with the interpreter you test with)
pip install -e ".[dev]"

# Optional: run CI's lint job before every push
git config core.hooksPath .githooks
```

## Code Style

We use ruff for both formatting and linting - the same commands CI runs, so a
clean local run is a clean CI run. The dev extra, both CI jobs and the pre-push
hook all pin ruff 0.16.3; another ruff version can format differently. (Do not
format with black: its output differs from `ruff format` and CI will reject it.)
`ruff format` also formats the Python code blocks in Markdown files.

```bash
# Format code
ruff format .

# Check formatting without writing (what CI does)
ruff format --check .

# Lint
ruff check .

# Fix auto-fixable lint issues
ruff check --fix .
```

## Running Tests

**Interpreter.** Use a CPython whose `os` module has `pidfd_open`; the owner
tests call it directly and fail without it. python.org, distribution and pyenv
builds have it. Some standalone builds do not, including the uv-managed
3.11.11, 3.12.8 and 3.13.1 builds from late 2024 (current uv-managed builds
have it). Check with:

```bash
python -c 'import os; print(hasattr(os, "pidfd_open"))'
```

**Bubblewrap.** Tests marked `ns_required` need `bwrap` and a working
unprivileged PID namespace. CI installs bubblewrap and runs them on Python
3.12 only; on Ubuntu 24.04 it also has to set
`kernel.apparmor_restrict_unprivileged_userns=0`. Without bubblewrap, deselect
them as CI does on the other versions:

```bash
python -m pytest tests/ -m "not ns_required"
```

**Your own spool store.** The suite never writes to `~/.spindle`: conftest
points `SPINDLE_HOME` at a temporary directory before anything imports
spindle. It does check, at the end of the session, that `~/.spindle/spools`
gained no files, so if a Spindle service on the same machine launches a spool
during the run, the session fails with a false leak report. If you have a live
store, mask it with an empty read-only directory.

**A writable checkout.** Keep the checkout writable inside any sandbox you run
the suite in; the release audit's run with the checkout mounted read-only
failed its owner subprocess tests.

**A quiet machine.** The owner subprocess tests give each owner process five
seconds to start. On a heavily loaded machine about a hundred of them fail
together with "owner did not become ready" or "watchdog owner did not start"
(with empty stderr), and a few other tests have similar budgets. Rerun
failures on their own, on a quiet machine, before treating them as
regressions.

With no live store on the machine, plain `python -m pytest tests/` is enough.
Otherwise, this is the command the release audit used, run from the checkout
root:

```bash
mkdir -p /tmp/spindle-empty-spools
bwrap --ro-bind / / --bind /tmp /tmp --bind "$PWD" "$PWD" \
  --ro-bind /tmp/spindle-empty-spools "$HOME/.spindle/spools" \
  --dev /dev --proc /proc --chdir "$PWD" -- \
  env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/ -p no:cacheprovider -q --tb=short -ra
```

The full suite takes over ten minutes.

## Making Changes

1. Create a branch for your changes
2. Make your changes
3. Run `ruff format` and `ruff check`
4. Run the tests
5. Submit a PR

## Code Structure

- `spindle/__init__.py` - MCP server and tools, CLI, spool store, harness
  launchers, and the store supervisor loop
- `spindle/_version.py` - the version; the wheel, `spindle --version`,
  `/health` and `spindle doctor` all read it
- `spindle/__main__.py` - `python -m spindle`
- `spindle/namespace_owner.py` - provider-neutral owner primitives: process
  identity, liveness evidence, ownership locks, the control mailbox
- `spindle/namespace_owner_process.py` - the per-spool logical owner that runs
  one provider process and its descendants
- `spindle/owner_watchdog.py` - the owner's parent, which contains the owner's
  descendants if it crashes
- `spindle/owner_episode_convergence.py` - turns owner, watchdog and launcher
  evidence into a spool's terminal outcome
- `spindle/lifecycle.py` - provider-neutral lifecycle events, reducer and
  crash-atomic snapshot
- `spindle_owner.py`, `spindle_supervisor.py`, `spindle_claude_driver.py` -
  top-level entry points for the owner, the per-store supervisor and the
  Claude stream driver, packaged beside `spindle` as `py-modules` so they can
  set process guards before importing the package
- `tests/` - test suite; `conftest.py` isolates the spool store
- `.githooks/pre-push` - local copy of CI's lint job
- `DECISIONS.md` - recorded design decisions for the owner and watchdog
- `docs/` - harness guides, plans, and `docs/research/` (the filesystem research
  behind the supported spool storage policy in the README)

## Key Concepts

- **Spool** - A background task/agent. Has an ID, status, prompt, and result.
- **Shard** - An isolated git worktree for safe parallel work.
- **Permission profile** - Controls what tools a spawned agent can use.
- **Owner** - The per-spool process that runs the harness CLI and records how
  it ended; the store supervisor turns that record into the spool's result.
