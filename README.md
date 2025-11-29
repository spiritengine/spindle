# Spindle

MCP server for Claude Code to Claude Code delegation. Fire-and-forget async work using Max subscription credits.

## Install

```bash
pip install -e .
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
# Spawn multiple
id1 = spin("Task 1")
id2 = spin("Task 2")

# Wait for all (gather mode)
results = spin_wait("id1,id2", mode="gather")

# Or get first to complete (yield mode)
first = spin_wait("id1,id2", mode="yield")
```

### Continue a session

```
# Get session ID from completed spool
result = unspool(spool_id)  # includes session_id

# Continue that conversation
new_id = respin(session_id, "Follow up question")
```

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

## Development: Hot Reload

For development, use the wrapper script to enable hot reload:

```json
{
  "mcpServers": {
    "spindle": {
      "command": "/path/to/spindle/spindle-wrapper.sh"
    }
  }
}
```

Then after making code changes, call `spindle_reload()` and the server restarts with fresh code - no need to restart Claude Code.

## Limits

- Max 5 concurrent spools
- 24h auto-cleanup of old spools
- Orphaned spools (dead process) marked as error on restart
