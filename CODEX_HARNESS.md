# Codex CLI Harness for Spindle

## Overview

This harness allows Spindle to delegate tasks to OpenAI's Codex CLI, providing an alternative to Claude Code for code generation and editing tasks.

## Features

- **Transparent API**: Use regular `spin()`, `unspool()`, and `respin()` with `harness="codex"`
- **Async execution**: `spin` returns immediately with a spool_id
- **Session continuity**: `respin` allows continuing conversations (auto-detects harness)
- **Result retrieval**: `unspool` fetches results when complete (auto-detects harness)
- **Unified spool management**: Codex spools integrate seamlessly with Claude Code spools
- **Automatic tagging**: All Codex spools are tagged with "codex" for easy filtering

## API

### spin (with harness="codex")

Spawn a Codex CLI agent to handle a task. Returns immediately with spool_id.

```python
spool_id = spin(
    prompt="Write a function to parse CSV files",
    working_dir="/path/to/project",
    model="gpt-5-codex",  # Optional, defaults to configured model
    harness="codex",  # Use Codex instead of Claude Code
    permission="full",  # Mapped to sandbox policy
    timeout=300,  # Optional, seconds
    tags="csv,utils"  # Optional, comma-separated tags
)
```

**Parameters:**
- `prompt` (required): The task/question for the Codex agent
- `working_dir` (required): Directory for the agent to work in
- `harness` (required): Set to "codex" to use Codex CLI
- `model` (optional): Model to use (e.g., "gpt-5-codex")
- `permission` (optional): Mapped to Codex sandbox policy:
  - `"readonly"` → `"read-only"`
  - `"full"` or `"shard"` → `"danger-full-access"`
  - Other → `"workspace-write"`
- `timeout` (optional): Kill spool after this many seconds
- `tags` (optional): Comma-separated tags for organizing spools

**Returns:** spool_id (string) with "codex-" prefix

### unspool (auto-detects harness)

Get the result of a background spin task. Automatically detects whether the spool uses Claude Code or Codex.

```python
result = unspool(spool_id)
```

**Parameters:**
- `spool_id` (required): The spool_id from spin

**Returns:** Result string or status message

### respin (auto-detects harness)

Continue an existing session with a new message. Automatically detects whether the session uses Claude Code or Codex.

```python
spool_id = respin(
    session_id="abc-123-def",
    prompt="Now add tests for that function"
)
result = unspool(spool_id)
```

**Parameters:**
- `session_id` (required): The session ID to continue
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
spool_id = spin(
    prompt="Create a Python function to validate email addresses using regex",
    working_dir="/home/user/myproject",
    harness="codex"
)

# Check status (auto-detects harness)
result = unspool(spool_id)
# Returns: "Spool codex-abc123 still running..." or actual result

# When complete, extract session_id from result if needed for continuation
# (Session ID is in the JSON output from Codex CLI)

# Continue the conversation (auto-detects harness)
session_id = "extracted-from-previous-result"
spool_id2 = respin(
    session_id=session_id,
    prompt="Add unit tests for that email validator"
)

result2 = unspool(spool_id2)
```

## Differences from Claude Code Harness

| Feature | Claude Code (`harness="claude-code"`) | Codex CLI (`harness="codex"`) |
|---------|---------------------|-------------------------|
| Command | `claude exec` | `codex exec` |
| Spool prefix | 8-char UUID | `codex-` + 8-char UUID |
| Shard support | Full (via `permission="shard"`) | Not yet implemented |
| SKEIN integration | Full | Not yet implemented |
| Permission modes | careful, readonly, full, shard | Mapped to sandbox policies (read-only, workspace-write, danger-full-access) |
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
   result = await call_tool("spin", {
       "prompt": "Write hello world in Python",
       "working_dir": "/tmp/test",
       "harness": "codex"
   })
   ```

3. Check spool status (auto-detects harness):
   ```python
   result = await call_tool("unspool", {
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
