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

## API

| Tool | Purpose |
|------|---------|
| `spin(prompt, system_prompt?, working_dir?, allowed_tools?)` | Spawn agent, return spool_id |
| `unspool(spool_id)` | Get result (non-blocking) |
| `spools()` | List all spools |
| `spin_wait(spool_ids, mode?, timeout?)` | Block until done |
| `respin(session_id, prompt)` | Continue session |
| `spin_drop(spool_id)` | Cancel by killing process |

## Storage

Spools persist to `~/.spindle/spools/{spool_id}.json`:

```json
{
  "id": "abc12345",
  "status": "complete",
  "prompt": "...",
  "result": "...",
  "session_id": "...",
  "pid": 12345,
  "created_at": "2025-11-26T...",
  "completed_at": "2025-11-26T..."
}
```

## Limits

- Max 5 concurrent spools
- 24h auto-cleanup of old spools
- Orphaned spools (dead process) marked as error on restart
