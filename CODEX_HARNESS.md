# Codex CLI Harness for Spindle

## Overview

This harness allows Spindle to delegate tasks to OpenAI's Codex CLI, providing an alternative to Claude Code for code generation and editing tasks.

## Features

- **Transparent API**: Use regular `spin()`, `unspool()`, and `respin()` with `harness="codex"`
- **Blocking MCP results**: `spin` returns {spool_id, status, result}; `wait=False` returns the ID immediately
- **Session continuity**: `respin` allows continuing conversations (auto-detects harness)
- **Result retrieval**: `unspool` fetches results when complete (auto-detects harness)
- **Unified spool management**: Codex spools integrate seamlessly with Claude Code spools
- **Automatic tagging**: All Codex spools are tagged with "codex" for easy filtering

## API

### spin (with harness="codex")

Spawn a Codex CLI agent and wait for its result. For parallel launches, pass
`wait=False` to each, then call `spin_wait` for completion. The CLI and synchronous
Python helpers remain detached. See the [MCP contract and recovery instructions](README.md#basic-launch-and-receive-the-result).

```python
completion = spin(
    prompt="Write a function to parse CSV files",
    working_dir="/path/to/project",
    model="gpt-5.6",  # Optional; ChatGPT auth normalizes this to the default Sol tier
    harness="codex",  # Use Codex instead of Claude Code
    permission="full",  # Mapped to sandbox policy
    timeout=300,  # Optional, seconds
    tags="csv,utils",  # Optional, comma-separated tags
)
spool_id = completion["spool_id"]
```

**Parameters:**
- `prompt` (required): The task/question for the Codex agent
- `working_dir` (required): Directory for the agent to work in
- `harness` (required): Set to "codex" to use Codex CLI
- `model` (optional): Model to use (e.g., "sol", "astra", or a full model name)
- `permission` (optional): Mapped to Codex sandbox policy:
  - `"readonly"` / `"manual"` → `"read-only"`
  - `"full"` → `"danger-full-access"`
  - `"shard"` → `"workspace-write"` through MCP `spin`; the `spindle spin` CLI maps it to `"danger-full-access"`
  - `"research"` / `"research+shard"` → a `spindle-research-<id>` permissions profile (fresh per launch) instead of `--sandbox`: the filesystem is read-only except a `file:`/`dir:` target's directory, and the network is on (recorded as `"read-only"` for a `site:` target, `"workspace-write"` for a `file:` or `dir:` target)
  - Other → `"workspace-write"`
- `shard` (optional): Run in an isolated git worktree (also via `permission="shard"` or `"careful+shard"`)
- `timeout` (optional): Kill spool after this many seconds
- `wait` (default True): Wait for the result; False returns an ID immediately
- `wait_timeout` (optional): Bound the wait without stopping the spool
- `tags` (optional): Comma-separated tags for organizing spools

**Returns:** {spool_id, status, result}; spool_id has the "codex-" prefix. With `wait=False`, returns the bare ID string.

### unspool (auto-detects harness)

Read a spool's saved result (works for any spool, waited on or not). Automatically detects whether the spool uses Claude Code or Codex.

```python
result = unspool(spool_id)
```

**Parameters:**
- `spool_id` (required): The spool_id from spin

**Returns:** Result string or status message

### respin (auto-detects harness)

Continue an existing session with a new message. Automatically detects whether the session uses Claude Code or Codex.

```python
completion = respin(session_id="abc-123-def", prompt="Now add tests for that function")
result = completion["result"]
```

**Parameters:**
- `session_id` (required): The session ID to continue
- `prompt` (required): The follow-up message/task

**Returns:** {spool_id, status, result} for the continuation; `wait=False` returns the bare ID immediately.

## Implementation Details

### Command Execution

The harness uses `codex exec` with the following flags:
- `--json`: Structured JSON output for parsing
- `--skip-git-repo-check`: a spool's working dir need not be a git repository (a
  `spindle doctor --smoke` temp dir, a scratch research dir); codex refuses to start in one
  without it. The check only gates startup.
- `--sandbox <mode>`: the tier the spool's permission maps to, passed explicitly on every
  non-research launch
- `-c sandbox_mode=<mode>`: the same tier (non-research), so `~/.codex/config.toml` cannot
  widen it
- `-c approval_policy=never`: on every tier except `danger-full-access`, so an approval
  setting in config.toml cannot re-run a denied command outside the sandbox (a command
  matching a saved codex `allow` rule still runs outside it; see
  [CODEX_SETUP.md](docs/CODEX_SETUP.md#saved-allow-rules))
- Research tiers: no `--sandbox`, no `sandbox_mode`, no `--add-dir`;
  `-c default_permissions="spindle-research-<id>"` plus that profile's filesystem and
  `network.enabled=true` (see [CODEX_SETUP.md](docs/CODEX_SETUP.md#how-spindle-invokes-it))
- In a shard: `--cd <worktree>`; for a non-research shard at `workspace-write`, also
  `--add-dir` and a pinned `-c sandbox_workspace_write.writable_roots` for the git metadata
  the bwrap wrapper makes writable and the worktree root (see
  [CODEX_SETUP.md](docs/CODEX_SETUP.md#codex-in-shards))

`--full-auto` is deliberately not used. It combines a workspace-write sandbox with
on-failure approvals, and that bundled tier silently overrides `--sandbox` regardless of
flag order — `codex exec --full-auto --sandbox read-only` runs at workspace-write. It is
also unnecessary: `codex exec` is already non-interactive on its own.

### Spool Structure

Codex spools include a `harness` field set to "codex" to distinguish them from Claude Code spools:

```json
{
  "id": "codex-abc12345",
  "harness": "codex",
  "status": "running",
  "prompt": "Write a CSV parser",
  "working_dir": "/path/to/project",
  "model": "gpt-5.6-sol",
  "sandbox": "workspace-write",
  "tags": ["codex", "csv"],
  "session_id": null,
  "created_at": "2025-12-29T00:00:00",
  "pid": 12345
}
```

### Session Continuity

The harness uses `codex exec resume <session_id>` to continue conversations. Session IDs are extracted from the Codex CLI JSON output and stored in the spool metadata.

### Integration with Spindle

Codex spools:
- Count toward the global MAX_CONCURRENT limit (15 concurrent spools)
- Can be monitored with `spools()` and filtered by the "codex" tag
- Support the same timeout and monitoring as Claude Code spools
- Use the same background monitoring and finalization process

## Requirements

- OpenAI Codex CLI installed (`npm i -g @openai/codex`)
- Codex CLI authenticated (requires ChatGPT Plus/Pro/Enterprise)
- `codex` command available in PATH

## Example Workflow

```python
# Start a task
completion = spin(
    prompt="Create a Python function to validate email addresses using regex",
    working_dir="/home/user/myproject",
    harness="codex",
)

result = completion["result"]

# Continue the completed spool (the provider session ID is resolved internally)
next_completion = respin(completion["spool_id"], "Add unit tests for that email validator")
result2 = next_completion["result"]
```

## Differences from Claude Code Harness

| Feature | Claude Code (`harness="claude-code"`) | Codex CLI (`harness="codex"`) |
|---------|---------------------|-------------------------|
| Command | `claude -p` | `codex exec` |
| Spool prefix | 8-char UUID | `codex-` + 8-char UUID |
| Shard support | `shard`, `careful+shard` (bwrap-contained on Linux) | `shard`, `careful+shard` (worktree at `workspace-write`, bwrap-wrapped on Linux) |
| SKEIN integration | In a SKEIN project, shards spawn through `skein shard spawn` and shard agents get SKEIN orientation unless `skeinless=True` | Same |
| Permission modes | careful, readonly, full, shard | Mapped to sandbox policies (read-only, workspace-write, danger-full-access) |
| Model selection | sonnet, opus, haiku | gpt-5.6-sol default; ChatGPT auth maps gpt-5.6 to Sol |
| Session resume | `--resume` flag | `codex exec resume` command |

## Future Enhancements

- [ ] Support Codex-specific features (screenshots, web search, MCP tools)
- [ ] Add reasoning effort control (`--reasoning-effort`)
- [ ] Support for Codex agent skills
- [ ] Structured output with `--output-schema`

## Testing

To test the Codex harness:

1. Ensure Codex CLI is installed and authenticated:
   ```bash
   codex --version
   ```

2. Use the MCP tools via a client:
   ```python
   # Via MCP client
   result = await call_tool(
       "spin", {"prompt": "Write hello world in Python", "working_dir": "/tmp/test", "harness": "codex"}
   )
   ```

3. Check spool status (auto-detects harness):
   ```python
   result = await call_tool("unspool", {"spool_id": "codex-abc12345"})
   ```

## Troubleshooting

**Error: "codex: command not found"**
- Install Codex CLI: `npm i -g @openai/codex`
- Ensure it's in PATH

**Error: "Authentication required"**
- Run `codex` interactively to authenticate
- Requires ChatGPT Plus/Pro/Enterprise subscription

**Spool stuck in "running" state**
- Check if Codex process is still alive
- Review the captured output in `~/.spindle/spools/<spool_id>.stdout` (or `$SPINDLE_HOME/spools/`)
- Kill stuck process and retry

**"Max concurrent spools" error**
- Codex and Claude Code spools share the same limit (15)
- Wait for running spools to complete or kill them
- Use `spools()` to check current status
