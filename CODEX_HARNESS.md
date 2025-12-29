# Codex CLI Harness for Spindle

## Overview

This harness allows Spindle to delegate tasks to OpenAI's Codex CLI, providing an alternative to Claude Code for code generation and editing tasks.

## Features

- **Async execution**: `codex_spin` returns immediately with a spool_id
- **Session continuity**: `codex_respin` allows continuing conversations
- **Result retrieval**: `codex_unspool` fetches results when complete
- **Unified spool management**: Codex spools integrate with existing Spindle infrastructure
- **Automatic tagging**: All Codex spools are tagged with "codex" for easy filtering

## API

### codex_spin

Spawn a Codex CLI agent to handle a task. Returns immediately with spool_id.

```python
spool_id = codex_spin(
    prompt="Write a function to parse CSV files",
    working_dir="/path/to/project",
    model="gpt-5-codex",  # Optional, defaults to configured model
    sandbox="workspace-write",  # Optional: read-only, workspace-write, danger-full-access
    timeout=300,  # Optional, seconds
    tags="csv,utils"  # Optional, comma-separated tags
)
```

**Parameters:**
- `prompt` (required): The task/question for the Codex agent
- `working_dir` (required): Directory for the agent to work in
- `model` (optional): Model to use (e.g., "gpt-5-codex")
- `sandbox` (optional): Sandbox policy - "read-only", "workspace-write", or "danger-full-access"
- `timeout` (optional): Kill spool after this many seconds
- `tags` (optional): Comma-separated tags for organizing spools

**Returns:** spool_id (string) with "codex-" prefix

### codex_unspool

Get the result of a background Codex spin task.

```python
result = codex_unspool(spool_id)
```

**Parameters:**
- `spool_id` (required): The spool_id from codex_spin

**Returns:** Result string or status message

### codex_respin

Continue an existing Codex session with a new message.

```python
spool_id = codex_respin(
    session_id="abc-123-def",
    prompt="Now add tests for that function"
)
result = codex_unspool(spool_id)
```

**Parameters:**
- `session_id` (required): The Codex session ID to continue
- `prompt` (required): The follow-up message/task

**Returns:** spool_id (string) for the continuation

## Implementation Details

### Command Execution

The harness uses `codex exec` with the following flags:
- `--json`: Structured JSON output for parsing
- `--full-auto`: Non-interactive execution (combines workspace-write sandbox with on-failure approvals)

### Spool Structure

Codex spools include a `harness` field set to "codex" to distinguish them from Claude Code spools:

```json
{
  "id": "codex-abc12345",
  "harness": "codex",
  "status": "running",
  "prompt": "Write a CSV parser",
  "working_dir": "/path/to/project",
  "model": "gpt-5-codex",
  "sandbox": "workspace-write",
  "tags": ["codex", "csv"],
  "session_id": null,
  "created_at": "2025-12-29T00:00:00",
  "pid": 12345
}
```

### Session Continuity

The harness uses `codex resume <session_id>` to continue conversations. Session IDs are extracted from the Codex CLI JSON output and stored in the spool metadata.

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
spool_id = codex_spin(
    prompt="Create a Python function to validate email addresses using regex",
    working_dir="/home/user/myproject"
)

# Check status
result = codex_unspool(spool_id)
# Returns: "Spool codex-abc123 still running..." or actual result

# When complete, extract session_id from result if needed for continuation
# (Session ID is in the JSON output from Codex CLI)

# Continue the conversation
session_id = "extracted-from-previous-result"
spool_id2 = codex_respin(
    session_id=session_id,
    prompt="Add unit tests for that email validator"
)

result2 = codex_unspool(spool_id2)
```

## Differences from Claude Code Harness

| Feature | Claude Code (`spin`) | Codex CLI (`codex_spin`) |
|---------|---------------------|-------------------------|
| Command | `claude exec` | `codex exec` |
| Spool prefix | 8-char UUID | `codex-` + 8-char UUID |
| Shard support | Full (via `permission="shard"`) | Not yet implemented |
| SKEIN integration | Full | Not yet implemented |
| Permission modes | careful, readonly, full, shard | sandbox policies (read-only, workspace-write, danger-full-access) |
| Model selection | sonnet, opus, haiku | gpt-5-codex, gpt-5 |
| Session resume | `--resume` flag | `codex resume` command |

## Future Enhancements

- [ ] Add shard support for Codex (isolated git worktrees)
- [ ] SKEIN integration for Codex agents
- [ ] Extract session_id automatically from Codex JSON output
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
   result = await call_tool("codex_spin", {
       "prompt": "Write hello world in Python",
       "working_dir": "/tmp/test"
   })
   ```

3. Check spool status:
   ```python
   result = await call_tool("codex_unspool", {
       "spool_id": "codex-abc12345"
   })
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
- Review logs in `~/.spindle/<spool_id>.stdout`
- Kill stuck process and retry

**"Max concurrent spools" error**
- Codex and Claude Code spools share the same limit (15)
- Wait for running spools to complete or kill them
- Use `spools()` to check current status
