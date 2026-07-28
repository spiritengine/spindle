# Codex CLI Setup Guide

Complete guide for setting up and using OpenAI's Codex CLI with Spindle.

## Prerequisites

- **ChatGPT Plus, Pro, or Enterprise subscription** - Required for Codex CLI access
- **Node.js and npm** - For installing the Codex CLI package
- **Linux kernel 5.13+** - For sandbox support (automatically bypassed on older kernels)

## Installation

### 1. Install Codex CLI

```bash
npm install -g @openai/codex
```

Verify installation:
```bash
codex --version
```

Expected output: `@openai/codex version X.X.X`

### 2. Authenticate

Run Codex interactively to authenticate:

```bash
codex
```

This will:
1. Open your browser for authentication
2. Require a ChatGPT Plus/Pro/Enterprise account
3. Store credentials for future use

Verify authentication:
```bash
codex exec "print('Hello from Codex')"
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
codex exec --json --skip-git-repo-check --sandbox <mode> "your task"
```

`--full-auto` is never passed: it carries its own workspace-write tier that overrides
`--sandbox` regardless of order, and `codex exec` is already non-interactive without it.

`--skip-git-repo-check` is always passed because a working dir is not always a repo — a
`spindle doctor --smoke` temp dir, a scratch research dir. Without it codex refuses to start
there (`Not inside a trusted directory and --skip-git-repo-check was not specified`) unless
that exact path is listed under `[projects]` in `~/.codex/config.toml`, so whether a spool
could run would depend on the operator's personal config. It relaxes no containment: the
sandbox tier above is the boundary, and the git check only ever gated startup.

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
    model="gpt-5.5",  # Explicit model
    working_dir="/path/to/project"
)
```

Available models:
- `gpt-5.6-sol` (default) — flagship tier; `5.6`, `sol`, and `codex` normalize to it
- `gpt-5.6` — official umbrella model; preserved for API-key or unknown authentication,
  but mapped to `gpt-5.6-sol` when `codex login status` reports ChatGPT authentication
  because that route rejects the umbrella ID on this installation
- `gpt-5.6-terra` / `gpt-5.6-luna` — explicit faster tiers; aliases `terra` and `luna`
- `gpt-5.5` — prior default and working compatibility choice
- `gpt-5.3-codex` and other `-codex` ids — API-only; 400 on ChatGPT-account auth

Spindle cannot infer every account's model entitlements. Unknown full model IDs pass through
to Codex, and a structured `turn.failed` response is recorded as a spool error. API-key-backed
installations may expose models that ChatGPT-authenticated installations do not.

See `spin_harnesses()` for the full alias list.

## Using Codex with Spindle

### Basic Usage

```python
# Minimal example
spool_id = spin(
    prompt="Add error handling to process_data()",
    harness="codex",
    working_dir="/path/to/project"
)

# Check result
result = unspool(spool_id)
```

**Important:** `working_dir` is REQUIRED for Codex (optional for Claude Code).

### With Permissions

```python
# Read-only analysis
spin(
    prompt="Analyze the code for security issues",
    harness="codex",
    working_dir="/path/to/project",
    permission="readonly"
)

# Full access for complex changes
spin(
    prompt="Refactor the authentication module",
    harness="codex",
    working_dir="/path/to/project",
    permission="full"
)
```

### Session Continuity

```python
# Initial task
spool_id = spin(
    prompt="Create a user model",
    harness="codex",
    working_dir="/path/to/project"
)

result = unspool(spool_id)
session_id = result["session_id"]

# Continue the conversation
spool_id2 = respin(
    session_id,
    "Add validation methods to the user model"
)
```

### With Timeout

```python
# Auto-kill after 60 seconds if stuck
spin(
    prompt="Quick fix task",
    harness="codex",
    working_dir="/path/to/project",
    timeout=60
)
```

## Performance Characteristics

### Startup Time

**Codex:** ~10 seconds to first response
**Claude Code:** ~3-4 minutes to first response

This makes Codex **~20-30x faster** for quick tasks.

### Throughput

For parallel batch work, Codex's speed provides higher throughput:

```python
# Launch 10 tasks - all start within seconds
tasks = ["Task 1", "Task 2", ..., "Task 10"]
spool_ids = [
    spin(task, harness="codex", working_dir="/app")
    for task in tasks
]

# Process as they complete
for result in spin_wait(",".join(spool_ids), mode="yield"):
    print(f"Done: {result}")
```

With Codex, all 10 tasks start immediately. With Claude, you'd wait 3-4 minutes per task startup.

### When Speed Matters

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

### 3. Sandbox Errors on Older Kernels

**Symptom:** Codex fails with sandbox errors on kernel < 5.13

**Automatic Fix:** Spindle detects this and uses bypass mode automatically

**Manual Verification:**
```bash
uname -r  # Check kernel version
python3 -c "
import platform
import re
v = platform.release()
m = re.match(r'(\d+)\.(\d+)', v)
if m:
    major, minor = int(m.group(1)), int(m.group(2))
    print(f'Landlock: {major > 5 or (major == 5 and minor >= 13)}')
"
```

### 4. JSON Output Parsing Failures

**Symptom:** Spindle can't extract session_id or results

**Cause:** Codex CLI changed JSON output format

**Workaround:** Check Spindle logs for raw output:
```bash
cat ~/.spindle/<spool_id>.stdout
```

Report format changes as an issue.

### 5. Slow First Execution

**Symptom:** First Codex execution takes longer than expected

**Cause:** Cold start - Codex CLI initializing

**Solution:** Subsequent executions will be faster (~10s typical)

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

Verify Spindle compatibility:
```bash
codex exec --json --full-auto "print('test')"
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

Raw output is stored in:
```bash
cat ~/.spindle/<spool_id>.stdout
cat ~/.spindle/<spool_id>.stderr
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
# Direct Codex test
codex exec --json --full-auto "create a hello world script"

# With bypass (for older kernels)
codex exec --json --dangerously-bypass-approvals-and-sandbox "create a hello world script"
```

If manual execution fails, the issue is with Codex CLI setup, not Spindle.

## Security Considerations

### Bypass Mode Implications

When using `--dangerously-bypass-approvals-and-sandbox`:

⚠️ **No filesystem restrictions** - Agent can read/write anywhere
⚠️ **No approval prompts** - Agent executes without confirmation
⚠️ **Full system access** - Use only in trusted environments

**Mitigations:**
1. Use `permission="careful"` for most tasks; use `readonly` only for pure inspection or synthesis where no code execution is needed
2. Run in isolated environments (containers, VMs)
3. Use `timeout` to limit execution time
4. Review changes before merging (use shards when supported)

### Sandbox Mode (Kernel >= 5.13)

With Landlock sandboxing enabled:

✅ **Filesystem restrictions** - Confined to workspace by default
✅ **Policy enforcement** - read-only, workspace-write, or full-access
✅ **Approval prompts** - For destructive operations (with `--full-auto`: on-failure only)

**Still exercise caution:**
- Review agent changes before production deployment
- Use version control to track modifications
- Test in non-production environments first

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
spin(
    "task",
    harness="codex",
    working_dir="/app",
    tags="quick-fix,frontend,priority-high"
)

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
    spool_id = spin(
        f"Attempt {attempt}: optimize function",
        harness="codex",
        working_dir="/app",
        timeout=30
    )
    result = unspool(spool_id)
    if "success" in result:
        break
```

### 5. Monitor Kernel Version

If you upgrade your kernel to >= 5.13, you'll automatically get sandbox support. No Spindle changes needed.

```bash
# After kernel upgrade
uname -r  # Verify new version
# Spindle will detect and use sandboxing on next run
```

## Comparison: Claude vs Codex

| Feature | Claude Code | Codex CLI |
|---------|-------------|-----------|
| **Startup time** | ~3-4 minutes | ~10 seconds |
| **Code reasoning** | Excellent | Good |
| **Complexity handling** | Deep | Moderate |
| **Speed** | Slow | Fast |
| **Best for** | Complex tasks | Quick tasks |
| **Subscription** | Anthropic API/Claude | ChatGPT Plus/Pro |
| **Sandboxing** | Tool-based | Landlock-based |
| **Kernel requirement** | None | 5.13+ for sandbox |
| **Session continuity** | `--resume` | `codex resume` |
| **working_dir required** | No | Yes |

## FAQ

**Q: Do I need both Claude Code and Codex?**
A: No, but having both gives you flexibility. Use Claude for complex work, Codex for speed.

**Q: Can I use Codex without ChatGPT Plus?**
A: No, Codex CLI requires a paid ChatGPT subscription (Plus, Pro, or Enterprise).

**Q: What if I'm on Windows or macOS?**
A: Codex CLI works, but Landlock detection is Linux-specific. Spindle will use bypass mode on other platforms.

**Q: Can I force sandbox mode on?**
A: Not through Spindle. If you need explicit sandbox control, run `codex` directly instead of through Spindle.

**Q: Does Codex support shards (isolated worktrees)?**
A: Not yet. Shard support is planned for future Codex integration.

**Q: How do I switch from Claude to Codex for an existing task?**
A: You can't switch mid-session. Finish with Claude, then start a new spool with Codex if needed.

**Q: Can I mix Claude and Codex in parallel?**
A: Yes! They share the 15-spool limit but run independently:
```python
claude_id = spin("Complex analysis")
codex_id = spin("Quick fix", harness="codex", working_dir="/app")
spin_wait(f"{claude_id},{codex_id}")
```

**Q: Why is working_dir required for Codex but not Claude?**
A: Codex CLI requires an explicit working directory. Claude Code can infer from session context.

**Q: How do I know which kernel version I have?**
A: Run `uname -r` on Linux. For Ubuntu, `lsb_release -a` shows the distribution version (20.04 = kernel ~5.4, 22.04 = kernel ~5.15).

## See Also

- [docs/MULTI_HARNESS_GUIDE.md](MULTI_HARNESS_GUIDE.md) - Multi-harness architecture overview
- [CODEX_HARNESS.md](../CODEX_HARNESS.md) - Implementation details
- [README.md](../README.md) - Main Spindle documentation
- [OpenAI Codex CLI Docs](https://developers.openai.com/codex/cli/) - Official Codex documentation
