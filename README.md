# Spindle

<!-- Uncomment when published:
[![PyPI version](https://badge.fury.io/py/spindle.svg)](https://badge.fury.io/py/spindle)
[![CI](https://github.com/smythp/spindle/actions/workflows/ci.yml/badge.svg)](https://github.com/smythp/spindle/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
-->

MCP server for Claude Code to Claude Code delegation. Spawn background agents that run asynchronously, with optional git worktree isolation for safe parallel work.

## Features

- **Async agent spawning** - Fire-and-forget pattern with spool IDs
- **Optional blocking with gather/yield** - Wait for all results at once, or stream them as agents complete. Alternatively, agent can continue other work, spins are nonblocking by default
- **Permission profiles** - Control what tools child agents can use (readonly, careful, full)
- **Shard isolation** - Run agents in sandboxed git worktrees to prevent conflicts
- **Model selection** - Route tasks to haiku, sonnet, or opus per-agent
- **Session continuity** - Resume conversations with child agents (auto-recovers expired sessions)
- **Rich querying** - Search, filter, peek at running output, export results

## Requirements

- Python 3.10+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Git (for shard/worktree functionality)

## Install

```bash
pip install spindle-mcp
```

Add to Claude Code's MCP config (`~/.claude.json`):

```json
{
  "mcpServers": {
    "spindle": {
      "command": "spindle"
    }
  }
}
```

## Usage

### Basic: Spawn and collect

```
# Spawn an agent
spool_id = spin("Research the Python GIL")

# Do other work...

# Check result
result = unspool(spool_id)
```

### Permission profiles

Control what tools the spawned agent can use:

```
# Read-only: Can only search and read
spin("Analyze the codebase", permission="readonly")

# Careful (default): Can read/write but limited bash
spin("Fix this bug", permission="careful")

# Full access: No restrictions
spin("Implement the feature", permission="full")

# Shard: Full access + auto-isolated worktree (common for risky work)
spin("Refactor the auth system", permission="shard")

# Careful + shard: Limited tools but isolated
spin("Update configs", permission="careful+shard")
```

Profiles:
- `readonly`: Read, Grep, Glob, safe bash (ls, cat, git status/log/diff)
- `careful`: Read, Write, Edit, Grep, Glob, bash for git/make/pytest/python/npm
- `full`: No restrictions
- `shard`: Full access + auto-creates isolated worktree
- `careful+shard`: Careful permissions + auto-creates isolated worktree

You can also pass explicit `allowed_tools` to override the profile.

### Isolated workspaces with shards

Run agents in isolated git worktrees to prevent conflicts:

```
# Agent works in its own worktree
spool_id = spin("Refactor auth module", shard=True)

# Check shard status
shard_status(spool_id)

# Merge changes back when done
shard_merge(spool_id)

# Or discard if not needed
shard_abandon(spool_id)
```

Shards create a git worktree + branch. If SKEIN is available, uses `skein shard spawn` for richer tracking. Falls back to plain git worktree otherwise.

### Wait for completion

```
# Spawn multiple agents
id1 = spin("Research competitor A")
id2 = spin("Research competitor B")
id3 = spin("Research competitor C")

# Gather: block until all complete, get all results
results = spin_wait("id1,id2,id3", mode="gather")

# Yield: return as each completes
# Great when results are independent - process each as it lands
result = spin_wait("id1,id2,id3", mode="yield")  # Returns first to finish

# With timeout
results = spin_wait("id1,id2", mode="gather", timeout=300)
```

Yield mode keeps you responsive instead of blocking on the slowest agent.

### Model selection and timeouts

```
# Route quick tasks to haiku (fast, cheap)
spin("Summarize this file", model="haiku")

# Complex work to opus
spin("Design the new architecture", model="opus")

# Auto-kill if it takes too long
spin("Should be quick", timeout=60)
```

### Continue a session

```
# Get session ID from completed spool
result = unspool(spool_id)  # includes session_id

# Continue that conversation
new_id = respin(session_id, "Follow up question")
```

If the session has expired on Claude's end, respin automatically falls back to transcript injection to recreate context.

### Cancel running work

```
spin_drop(spool_id)
```

### List all spools

```
spools()
```

### Search and filter

```
# Search prompts and results
spool_search("authentication")

# Filter by status and time
spool_results(status="error", since="1h")

# Regex search results
spool_grep("error|failed|exception")

# Get statistics
spool_stats()

# Export to file
spool_export("all", format="md")
```

## API

| Tool | Purpose |
|------|---------|
| `spin(prompt, permission?, shard?, system_prompt?, working_dir?, allowed_tools?, tags?)` | Spawn agent, return spool_id |
| `unspool(spool_id)` | Get result (non-blocking) |
| `spools()` | List all spools |
| `spin_wait(spool_ids, mode?, timeout?)` | Block until done |
| `respin(session_id, prompt)` | Continue session |
| `spin_drop(spool_id)` | Cancel by killing process |
| `spool_search(query, field?)` | Search prompts/results |
| `spool_results(status?, since?, limit?)` | Bulk fetch with filters |
| `spool_grep(pattern)` | Regex search results |
| `spool_retry(spool_id)` | Re-run with same params |
| `spool_peek(spool_id, lines?)` | See partial output while running |
| `spool_dashboard()` | Overview of running/complete/needs-attention |
| `spool_stats()` | Get summary statistics |
| `spool_export(spool_ids, format?, output_path?)` | Export to file |
| `shard_status(spool_id)` | Check shard worktree status |
| `shard_merge(spool_id, keep_branch?)` | Merge shard to master |
| `shard_abandon(spool_id, keep_branch?)` | Discard shard |

## Storage

Spools persist to `~/.spindle/spools/{spool_id}.json`:

```json
{
  "id": "abc12345",
  "status": "complete",
  "prompt": "...",
  "result": "...",
  "session_id": "...",
  "permission": "careful",
  "allowed_tools": "...",
  "tags": ["batch-1"],
  "shard": {
    "worktree_path": "/path/to/worktrees/abc12345-...",
    "branch_name": "shard-abc12345-...",
    "shard_id": "..."
  },
  "pid": 12345,
  "created_at": "2025-11-26T...",
  "completed_at": "2025-11-26T..."
}
```

## CLI Commands

```bash
spindle start   # Start via systemd (or background if no service)
spindle reload  # Restart via systemd to pick up code changes
spindle status  # Check if running (hits /health endpoint)
spindle serve --http  # Run MCP server directly (what systemd calls)
```

### systemd Service

For production, use the systemd service:

```bash
# Install service (copy to ~/.config/systemd/user/spindle.service)
systemctl --user daemon-reload
systemctl --user enable spindle
systemctl --user start spindle
```

Then `spindle reload` works to restart after code changes.

### Hot Reload (MCP tool)

From within Claude Code, call `spindle_reload()` to restart the server and pick up code changes.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SPINDLE_MAX_CONCURRENT` | `15` | Maximum concurrent spools |

Storage location: `~/.spindle/spools/`

## How It Works

1. **spin()** spawns a detached `claude` CLI process with the given prompt
2. The process runs in background, writing output to temporary files
3. A monitor thread polls for completion
4. **unspool()** returns the result once complete (non-blocking check)
5. Spool metadata persists to JSON files, surviving server restarts

For shards:
1. A git worktree is created with a new branch
2. The agent runs inside that worktree
3. After completion, merge back with `shard_merge()` or discard with `shard_abandon()`

## Limits

- Max 15 concurrent spools (configurable via `SPINDLE_MAX_CONCURRENT`)
- 24h auto-cleanup of old spools
- Orphaned spools (dead process) marked as error on restart

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT - see [LICENSE](LICENSE).
