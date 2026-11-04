# Codex CLI Setup Guide

Complete guide for setting up and using OpenAI's Codex CLI with Spindle.

## Prerequisites

- **ChatGPT Plus, Pro, or Enterprise subscription** - Required for Codex CLI access
- **Node.js and npm** - For installing the Codex CLI package
- **Linux with bubblewrap** - Codex sandboxes with bubblewrap plus seccomp; no kernel
  Landlock or minimum kernel version is needed (see [Codex Sandboxing](#codex-sandboxing))

## Installation

### 1. Install Codex CLI

```bash
npm install -g @openai/codex
```

Verify installation:
```bash
codex --version
```

Expected output: `codex-cli X.Y.Z` (for example `codex-cli 0.154.0`)

### 2. Authenticate

Run Codex interactively to authenticate:

```bash
codex
```

This will:
1. Open your browser for authentication
2. Require a ChatGPT Plus/Pro/Enterprise account
3. Store credentials for future use

Verify authentication from inside a git repository (codex refuses to start in an
untrusted directory otherwise; `--skip-git-repo-check` lifts that for a one-off test):
```bash
codex exec --sandbox read-only "print('Hello from Codex')"
```

If successful, you'll see Codex execute the command.

### 3. Check the Codex Version (Linux)

Sandbox enforcement depends on which codex the running process resolves, so check the one
on *its* PATH — a login shell and a long-lived server often resolve different installs:

```bash
which codex && codex --version
```

Enforcement is checked by behavior, not version: `_codex_sandbox_enforces()`
(`spindle/__init__.py`) probes the resolved binary once with codex's no-model
`codex sandbox` subcommand. A restrictive-tier spool whose binary does not actually enforce
is refused rather than run unsandboxed. Every spool records the resolved `codex_bin` /
`codex_version` for provenance.

No kernel Landlock is required — see below.

## Codex Sandboxing

### How it works

Codex sandboxes with its own vendored bubblewrap plus seccomp. It does **not** require
kernel Landlock (that is a legacy fallback in current codex builds), so `--sandbox`
enforces on kernels older than 5.13 — verified on kernel 5.4 with codex-cli 0.125.0.

Bubblewrap must be available, and codex needs a writable `~/.codex`: it materializes its
sandbox helper under `$CODEX_HOME/tmp/arg0/` at startup. If that directory cannot be
written, every sandboxed command fails with `bwrap: execvp codex-linux-sandbox: No such
file or directory` (fails closed — nothing escapes, but nothing runs either).

### How Spindle invokes it

Spindle always passes the tier explicitly:

```bash
codex exec --json --sandbox <mode> "your task"
```

`--full-auto` is never passed: it carries its own workspace-write tier that overrides
`--sandbox` regardless of order, and `codex exec` is already non-interactive without it.

### Sandbox Policies

Spindle maps permission levels to Codex sandbox policies:

| Spindle Permission | Codex Sandbox Policy | Description |
|-------------------|---------------------|-------------|
| `readonly` | `read-only` | Can only read files |
| `careful` (default) | `workspace-write` | Can read/write in workspace |
| `full` | `danger-full-access` | Full filesystem access |
| `shard` | `workspace-write` via `spin()`; `danger-full-access` via the `spindle spin` CLI | Isolated worktree |
| `research` (site target) | `read-only` | Web research against a site; no writes |
| `research` (file/dir target) | `workspace-write` | Adds `--add-dir` for the writable target path |
| `research+shard` (site target) | `read-only` | research + isolated worktree; no writes |
| `research+shard` (file/dir target) | `workspace-write` | Adds `--add-dir` for the writable target path + isolated worktree |

Note that codex's `workspace-write` treats `/tmp` and `$TMPDIR` as writable in addition to
the workspace, so a `careful` spool can still write there. Only `read-only` prevents all
writes.

## Configuration

### Environment Variables

None required for basic use. Codex CLI uses its own configuration.

### Model Selection

Specify model via the `model` parameter:

```python
spin(
    prompt="Your task",
    harness="codex",
    model="sol",  # Alias for gpt-5.6-sol, the default
    working_dir="/path/to/project",
)
```

Available models (account catalog checked 2026-09-25 on codex 0.154.0):
- `gpt-6-astra` — frontier tier; aliases `astra`, `6`, `6-astra`
- `gpt-reserve` — fast/affordable agentic coding tier; alias `reserve`. Hidden from the
  TUI model picker but served on ChatGPT-account auth
- `gpt-5.6-sol` (default) — flagship tier; `5.6`, `sol`, and `codex` normalize to it.
  The catalog now labels the 5.6 tiers "Older"; the default stays here deliberately
- `gpt-5.6` — official umbrella model; preserved for API-key or unknown authentication,
  but mapped to `gpt-5.6-sol` when `codex login status` reports ChatGPT authentication
  because that route rejects the umbrella ID on this installation
- `gpt-5.6-terra` / `gpt-5.6-luna` — explicit faster tiers; aliases `terra` and `luna`
- `gpt-5.5` — prior default; retired 2026-10-14, so a pin to it now fails
- `gpt-6-sol` / `gpt-6-luna` — on the public API docs but API-only here; 400 on
  ChatGPT-account auth. Aliases `6-sol` / `6-luna` pass them through for API-key installs
- `gpt-5.3-codex` and other `-codex` ids — API-only; 400 on ChatGPT-account auth

Spindle cannot infer every account's model entitlements. Unknown full model IDs pass through
to Codex, and a structured `turn.failed` response is recorded as a spool error. API-key-backed
installations may expose models that ChatGPT-authenticated installations do not.

See `spin_harnesses()` for the full alias list.

## Using Codex with Spindle

### Basic Usage

```python
# Minimal example
completion = spin(prompt="Add error handling to process_data()", harness="codex", working_dir="/path/to/project")
spool_id = completion["spool_id"]
result = completion["result"]
```

`working_dir` is required for every harness. MCP `spin`/`respin` block by default;
use `wait=False` to get an ID immediately and `spin_wait` to receive completion.
The CLI remains detached. See the [MCP contract and recovery instructions](../README.md#basic-launch-and-receive-the-result).

### With Permissions

```python
# Read-only analysis
spin(
    prompt="Analyze the code for security issues",
    harness="codex",
    working_dir="/path/to/project",
    permission="readonly",
)

# Full access for complex changes
spin(prompt="Refactor the authentication module", harness="codex", working_dir="/path/to/project", permission="full")
```

### Session Continuity

```python
# Initial task completes before continuing
completion = spin(prompt="Create a user model", harness="codex", working_dir="/path/to/project")

# Continue using the completed spool ID
next_completion = respin(completion["spool_id"], "Add validation methods to the user model")
```

### With Timeout

```python
# Auto-kill after 60 seconds if stuck
spin(prompt="Quick fix task", harness="codex", working_dir="/path/to/project", timeout=60)
```

## Parallel Batches

For parallel batch work, launch every spool detached and collect them as they finish:

```python
import json

# Launch 10 tasks without waiting on any of them
tasks = ["Task 1", "Task 2", ..., "Task 10"]
spool_ids = [spin(task, harness="codex", working_dir="/app", wait=False) for task in tasks]

# Process one completion at a time, always dropping IDs already returned
remaining = spool_ids
while remaining:
    completion = json.loads(spin_wait(",".join(remaining), mode="yield"))
    print(f"Done: {completion.get('result', completion.get('error'))}")
    remaining = completion.get("remaining", [])
```

## When to Use Which

**Use Codex for:**
- Interactive development workflows
- Rapid prototyping sessions
- Quick bug fixes during debugging
- High-volume batch processing
- Time-sensitive tasks

**Use Claude for:**
- Complex refactoring (better reasoning)
- Architectural decisions (deeper analysis)
- Code review (more thorough)
- Multi-file changes (better understanding)

## Known Issues and Workarounds

### 1. "codex: command not found"

**Cause:** Codex CLI not installed or not in PATH

**Solution:**
```bash
npm install -g @openai/codex
which codex  # Verify it's in PATH
```

If not in PATH:
```bash
export PATH="$PATH:$(npm root -g)/@openai/codex/bin"
```

### 2. Authentication Failures

**Cause:** Not authenticated or subscription expired

**Solution:**
```bash
codex  # Re-authenticate interactively
```

Verify you have an active ChatGPT Plus/Pro/Enterprise subscription.

### 3. Sandbox Errors

**Symptom:** every shell command in a codex spool fails, or a restrictive spool is refused
with `REFUSED: codex sandbox is not enforcing …`

Spindle never falls back to an unsandboxed run: it always passes the tier, and refuses a
restrictive spool whose codex does not enforce it. See
[Codex Sandbox Enforcement](MULTI_HARNESS_GUIDE.md#codex-sandbox-enforcement) and
[Sandbox Errors (Codex)](MULTI_HARNESS_GUIDE.md#sandbox-errors-codex) for causes and fixes.

### 4. JSON Output Parsing Failures

**Symptom:** Spindle can't extract session_id or results

**Cause:** Codex CLI changed JSON output format

**Workaround:** Check the raw output Spindle captured:
```bash
cat ~/.spindle/spools/<spool_id>.stdout   # or $SPINDLE_HOME/spools/
```

Report format changes as an issue.

### 5. Slow First Execution

**Symptom:** First Codex execution takes longer than expected

**Cause:** Cold start - Codex CLI initializing

**Solution:** Subsequent executions are usually faster

### 6. Max Concurrent Limit

**Symptom:** `"At concurrency limit (15/15)"`

**Cause:** Combined Claude + Codex spools exceed limit

**Solution:**
```python
# Check current spools
spools()

# Kill stuck ones
spin_drop("spool_id")

# Or wait for completion
spin_wait("id1,id2,id3")
```

Remember: Codex and Claude share the 15-spool limit.

## Upgrading

### Update Codex CLI

```bash
npm update -g @openai/codex
```

### After Upgrade

Verify Spindle compatibility (run inside a git repository):
```bash
codex exec --json --sandbox read-only "print('test')"
```

If the JSON format changed, Spindle may need updates to parsing logic.

## Debugging

### Enable Verbose Logging

Check Spindle logs for Codex command execution:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
```

### Check Codex Output

Raw output is stored in the spool store (`~/.spindle/spools/`, or `$SPINDLE_HOME/spools/`):
```bash
cat ~/.spindle/spools/<spool_id>.stdout
cat ~/.spindle/spools/<spool_id>.stderr
```

### Verify Sandbox Enforcement

Check which codex spindle will resolve, and whether it actually enforces (this runs the same
behavioral probe spindle uses to decide whether to refuse a restrictive spool):
```python
from spindle import _codex_cli_version, _codex_sandbox_enforces, _resolve_codex_binary

binary = _resolve_codex_binary()
version = _codex_cli_version(binary)
print(f"codex: {binary} ({version})")
print(f"enforces sandbox: {_codex_sandbox_enforces(binary)}")
```
If that prints `False`, spindle will refuse `readonly`/`careful` (and other restrictive)
codex spools rather than run them unsandboxed.

To confirm enforcement end-to-end, ask a read-only run to write and check nothing appears:
```bash
cd /tmp && rm -f /tmp/probe.txt
codex exec --sandbox read-only "run: echo x > /tmp/probe.txt ; report the literal error"
ls /tmp/probe.txt   # must not exist
```
Expected: `zsh:1: read-only file system: /tmp/probe.txt`, and no file. If the file appears,
this codex is not enforcing `--sandbox` — do not use it for readonly spools.

### Test Codex Manually

Isolate Spindle vs Codex issues:

```bash
# Direct Codex test, at the tier a careful spool uses (run inside a git repository)
codex exec --json --sandbox workspace-write "create a hello world script"
```

If manual execution fails, the issue is with Codex CLI setup, not Spindle.

## Security Considerations

Spindle never passes `--dangerously-bypass-approvals-and-sandbox`. Every spool runs at the
`--sandbox` tier its permission maps to (see [Sandbox Policies](#sandbox-policies)); only
`permission="full"`, or `shard` through the `spindle spin` CLI, gets `danger-full-access`.
A restrictive spool whose codex does not enforce its tier is refused, not run unsandboxed;
see [Codex Sandbox Enforcement](MULTI_HARNESS_GUIDE.md#codex-sandbox-enforcement).

Known gap: Spindle pins the sandbox tier but not codex's approval policy. If your
`~/.codex/config.toml` lets escalation requests be approved without you (for example
`approval_policy = "on-request"` with an automatic reviewer), a command the sandbox denied
can be re-run outside it, so a `readonly` spool is not a hard boundary on that machine.

**Still exercise caution:**
- Use `permission="careful"` for most tasks; use `readonly` only for pure inspection or
  synthesis where no code execution is needed
- Use shards (`permission="shard"` or `careful+shard`) for code changes, and review them
  before merging
- Use `timeout` to limit execution time

## Best Practices

### 1. Always Specify working_dir

```python
# Good
spin("task", harness="codex", working_dir="/path/to/project")

# Bad - will fail
spin("task", harness="codex")
```

### 2. Use Tags for Organization

```python
spin("task", harness="codex", working_dir="/app", tags="quick-fix,frontend,priority-high")

# Filter later
spool_search("quick-fix")
```

### 3. Set Timeouts for Quick Tasks

```python
# Prevent runaway processes
spin("simple task", harness="codex", working_dir="/app", timeout=60)
```

### 4. Leverage Speed for Iteration

```python
# Rapid iteration loop
for attempt in range(3):
    completion = spin(f"Attempt {attempt}: optimize function", harness="codex", working_dir="/app", timeout=30)
    result = completion["result"]
    if "success" in result:
        break
```

## Comparison: Claude vs Codex

| Feature | Claude Code | Codex CLI |
|---------|-------------|-----------|
| **Code reasoning** | Excellent | Good |
| **Complexity handling** | Deep | Moderate |
| **Best for** | Complex tasks | Quick tasks |
| **Subscription** | Anthropic API/Claude | ChatGPT Plus/Pro |
| **Sandboxing** | Permission mode and tool allowlist | Codex `--sandbox` tier (bubblewrap + seccomp) |
| **Session continuity** | `--resume` | `codex exec resume` |
| **working_dir required** | Yes | Yes |

## FAQ

**Q: Do I need both Claude Code and Codex?**
A: No, but having both gives you flexibility. Use Claude for complex work, Codex for speed.

**Q: Can I use Codex without ChatGPT Plus?**
A: No, Codex CLI requires a paid ChatGPT subscription (Plus, Pro, or Enterprise).

**Q: What if I'm on Windows or macOS?**
A: Spindle does not support Windows; use WSL2. On macOS, which is supported but not tested
extensively, Spindle passes the same `--sandbox` tier and runs the same enforcement probe as
on Linux; shard worktrees there have no bwrap layer around codex.

**Q: Can I force sandbox mode on?**
A: It is always on. Spindle passes the tier on every launch and refuses a restrictive spool
whose codex does not enforce it; only `danger-full-access` (see above) runs without one.

**Q: Does Codex support shards (isolated worktrees)?**
A: Yes. `shard` and `careful+shard` create a worktree, run codex there at `workspace-write`
(the `spindle spin` CLI maps `shard` to `danger-full-access`), and on Linux wrap it in
bwrap. Inside that wrapper the worktree, `/tmp`, the git metadata a commit needs, and
these home directories when they exist are writable: `~/.codex` (or `$CODEX_HOME`),
`~/.claude`, `~/.claude.json`, `~/.anthropic`, `~/.gemini`, `~/.spindle`, `~/.config` and
`~/.cache`. Everything else is read-only. With `danger-full-access` this outer wrapper is
the only boundary, so a full-access spool can write those home directories.

**Q: How do I switch from Claude to Codex for an existing task?**
A: You can't switch mid-session. Finish with Claude, then start a new spool with Codex if needed.

**Q: Can I mix Claude and Codex in parallel?**
A: Yes! They share the 15-spool limit but run independently:
```python
claude_id = spin("Complex analysis", working_dir="/app", wait=False)
codex_id = spin("Quick fix", harness="codex", working_dir="/app", wait=False)
spin_wait(f"{claude_id},{codex_id}")
```

**Q: Is working_dir required?**
A: Yes, for every harness: the server's working directory is not your project.

## See Also

- [docs/MULTI_HARNESS_GUIDE.md](MULTI_HARNESS_GUIDE.md) - Multi-harness architecture overview
- [CODEX_HARNESS.md](../CODEX_HARNESS.md) - Implementation details
- [README.md](../README.md) - Main Spindle documentation
- [OpenAI Codex CLI Docs](https://developers.openai.com/codex/cli/) - Official Codex documentation
