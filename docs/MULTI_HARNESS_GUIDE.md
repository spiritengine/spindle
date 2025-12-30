# Multi-Harness Architecture Guide

## Overview

Spindle's multi-harness architecture allows you to delegate tasks to different AI agent backends while using a unified API. This gives you the flexibility to choose the right tool for each task based on performance, capabilities, and cost requirements.

## Architecture

Spindle abstracts the underlying AI agent implementation through a "harness" layer. Each harness wraps a specific CLI tool (Claude Code, Codex CLI, etc.) and provides:

- Unified spawn/unspool/respin API
- Automatic harness detection from spool metadata
- Session continuity across respin operations
- Transparent integration with Spindle's spool management

```
┌─────────────────────────────────────────────┐
│           Spindle MCP Server                │
│  ┌────────────────────────────────────────┐ │
│  │    Unified API (spin/unspool/respin)   │ │
│  └────────────────────────────────────────┘ │
│              ▼                  ▼            │
│  ┌──────────────────┐  ┌─────────────────┐  │
│  │  Claude Harness  │  │  Codex Harness  │  │
│  └──────────────────┘  └─────────────────┘  │
│         ▼                      ▼             │
│  ┌──────────────────┐  ┌─────────────────┐  │
│  │    claude CLI    │  │   codex CLI     │  │
│  └──────────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────┘
```

## Available Harnesses

### Claude Code (Default)

**CLI:** `claude` (Anthropic's Claude Code CLI)
**Models:** Claude Sonnet, Opus, Haiku
**Startup time:** ~3-4 minutes to first response
**Strengths:** Complex reasoning, architecture design, code review

The Claude Code harness is Spindle's default and most capable option. It excels at:
- Deep code understanding and refactoring
- Architectural decisions and design
- Complex multi-file changes
- Thorough code review and analysis

**Usage:**
```python
# Explicit
spin("Refactor auth to use dependency injection", harness="claude-code")

# Implicit (default)
spin("Refactor auth to use dependency injection")
```

### Codex CLI

**CLI:** `codex` (OpenAI's Codex CLI)
**Models:** GPT-5 Codex
**Startup time:** ~10 seconds to first response
**Strengths:** Speed, quick edits, prototyping

The Codex harness prioritizes speed over depth. Ideal for:
- Quick bug fixes and simple edits
- Rapid prototyping and exploration
- Running many parallel tasks
- Time-sensitive operations

**Usage:**
```python
spin(
    prompt="Add error handling to process_data()",
    harness="codex",
    working_dir="/path/to/project"
)
```

**Important:** Codex requires `working_dir` to be specified. Claude Code can infer it from the current session.

## Unified API

All harnesses use the same API surface, making them interchangeable:

### spin()

Spawn an agent with any harness:

```python
# Claude Code (default)
spool_id = spin("Complex refactoring task")

# Codex
spool_id = spin(
    prompt="Quick edit task",
    harness="codex",
    working_dir="/path/to/project"
)
```

**Common parameters (work with all harnesses):**
- `prompt` - The task description
- `harness` - "claude-code" or "codex"
- `model` - Model to use (harness-specific)
- `timeout` - Auto-kill after N seconds
- `tags` - Organization tags

**Claude-specific parameters:**
- `permission` - "readonly", "careful", "full", "shard"
- `shard` - Auto-create git worktree
- `system_prompt` - Custom system instructions
- `allowed_tools` - Explicit tool permissions

**Codex-specific parameters:**
- `working_dir` - Required project directory
- `sandbox` - Derived from permission parameter

### unspool()

Get results from any harness - automatically detects which harness the spool used:

```python
result = unspool(spool_id)  # Works for both Claude and Codex
```

The harness is stored in the spool metadata and automatically loaded when you call unspool.

### respin()

Continue a conversation with any harness:

```python
# Initial task
spool_id1 = spin("Write a CSV parser", harness="codex", working_dir="/project")
result1 = unspool(spool_id1)

# Extract session_id from result metadata
session_id = result1["session_id"]

# Continue (auto-detects harness from session)
spool_id2 = respin(session_id, "Add validation to the parser")
```

Session continuity is harness-aware - Spindle remembers which harness was used and routes the respin call appropriately.

## Performance Comparison

| Metric | Claude Code | Codex CLI |
|--------|-------------|-----------|
| Startup time | 3-4 minutes | ~10 seconds |
| Code understanding | Excellent | Good |
| Reasoning depth | Deep | Moderate |
| Best for | Complex tasks | Quick edits |
| Cost per task | Higher | Lower |

**Rule of thumb:** Use Claude for thinking, Codex for typing.

## Automatic Harness Detection

Spindle stores the harness type in spool metadata, enabling automatic detection:

```json
{
  "id": "codex-abc12345",
  "harness": "codex",
  "prompt": "...",
  "session_id": "xyz-789",
  ...
}
```

When you call `unspool()` or `respin()`, Spindle:
1. Reads the spool metadata
2. Checks the `harness` field
3. Routes to the appropriate harness implementation
4. Returns results in a unified format

This means you can work with spools without remembering which harness created them.

## Session Continuity

Both harnesses support session continuity through `respin()`:

**Claude Code:**
- Uses `--resume <session_id>` flag
- Falls back to transcript injection if session expired
- Maintains full conversation context

**Codex:**
- Uses `codex resume <session_id>` command
- Session IDs extracted from JSON output
- Preserves conversation state

## Choosing the Right Harness

### Use Claude Code for:

✅ **Complex refactoring**
```python
spin("Refactor the auth module to use a plugin architecture")
```

✅ **Architecture decisions**
```python
spin("Design a caching layer for the API with Redis")
```

✅ **Code review and analysis**
```python
spin("Review the payment processor for security issues")
```

✅ **Multi-file changes**
```python
spin("Add logging throughout the application", permission="shard")
```

### Use Codex for:

✅ **Quick bug fixes**
```python
spin("Fix the off-by-one error in line 42", harness="codex", working_dir="/app")
```

✅ **Simple implementations**
```python
spin("Add a helper function to validate email addresses", harness="codex", working_dir="/app")
```

✅ **Rapid prototyping**
```python
spin("Create a basic REST API for user CRUD", harness="codex", working_dir="/app")
```

✅ **Parallel batch work** (faster = more throughput)
```python
# Launch 10 quick edits in parallel
for task in quick_tasks:
    spin(task, harness="codex", working_dir="/app")
```

## Sandbox and Permission Models

### Claude Code Permissions

Claude uses a tool-based permission system:

- **readonly** - Only read operations (Read, Grep, Glob, safe bash)
- **careful** - Read/write with limited bash (default)
- **full** - No restrictions
- **shard** - Full permissions + isolated git worktree
- **careful+shard** - Careful permissions + worktree

Example:
```python
spin("Analyze code", permission="readonly")
spin("Fix bug", permission="careful")  # Default
spin("Refactor module", permission="shard")  # Isolated worktree
```

### Codex Sandbox Policies

Codex uses OpenAI's sandbox policies, mapped from Claude permissions:

- `permission="readonly"` → `--sandbox read-only`
- `permission="careful"` → `--full-auto` (workspace-write + approvals)
- `permission="full"` → `--dangerously-bypass-approvals-and-sandbox`
- `permission="shard"` → `--dangerously-bypass-approvals-and-sandbox`

The mapping happens automatically in `_codex_spin_sync()`.

## Landlock Detection (Codex)

Codex CLI requires Linux kernel 5.13+ for Landlock sandbox support. Spindle automatically detects kernel version and adjusts:

**Kernel 5.13+:**
```bash
codex exec --json --full-auto "task"
```

**Kernel < 5.13:**
```bash
codex exec --json --dangerously-bypass-approvals-and-sandbox "task"
```

Detection happens in `_has_landlock_support()`:
1. Parse kernel version from `platform.release()`
2. Check for major.minor >= 5.13
3. Verify `/sys/kernel/security/landlock` exists
4. Use bypass flag if check fails

This ensures Codex works on older systems (e.g., Ubuntu 20.04 with kernel 5.4) without manual configuration.

## Spool Management

Both harnesses integrate seamlessly with Spindle's spool management:

```python
# List all spools (mixed harnesses)
spools()

# Filter by harness using tags
spool_search("codex")  # All Codex spools (auto-tagged)

# Dashboard shows both
spool_dashboard()

# Wait for completion (any harness)
spin_wait("id1,id2,id3")
```

Codex spools are automatically tagged with "codex" for easy filtering.

## Concurrency Limits

**All harnesses share the same concurrency limit:** 15 spools maximum.

This prevents resource exhaustion regardless of which harness you use:

```python
# Mix of harnesses, max 15 total
spin("Task 1", harness="claude-code")  # 1/15
spin("Task 2", harness="codex", working_dir="/app")  # 2/15
# ... up to 15 total
```

If you hit the limit:
```
Error: At concurrency limit (15/15). Wait for spools to complete or kill some.
```

Use `spools()` to check status or `spin_drop(spool_id)` to cancel running work.

## Troubleshooting

### Harness Not Found

**Error:** `"codex: command not found"`

**Solution:** Install the CLI:
```bash
npm i -g @openai/codex  # For Codex
```

### Authentication Issues

**Claude Code:**
```bash
claude --version  # Verify installation
claude login      # Authenticate
```

**Codex:**
```bash
codex --version   # Verify installation
codex             # Run interactively to authenticate (requires ChatGPT Plus/Pro)
```

### Landlock Errors (Codex)

**Error:** Sandbox failures on kernel < 5.13

**Solution:** Spindle automatically detects and bypasses. If you see this error, check:
```bash
uname -r  # Check kernel version
ls /sys/kernel/security/landlock  # Verify Landlock availability
```

Spindle will log:
```
[Spindle] Kernel 5.4.0 lacks Landlock support (needs 5.13+), using bypass mode for Codex
```

### Wrong Harness Used

If you get unexpected behavior, verify the harness:

```python
# Check spool metadata
spool = _read_spool(spool_id)
print(spool.get("harness", "claude-code"))
```

Harness defaults to "claude-code" if not specified.

### Session Continuity Failures

If `respin()` fails:

1. **Check session exists:**
   ```python
   spool = _read_spool(original_spool_id)
   session_id = spool.get("session_id")
   ```

2. **Verify harness:**
   ```python
   harness = spool.get("harness", "claude-code")
   ```

3. **Claude Code:** Falls back to transcript injection automatically
4. **Codex:** Session ID must be valid from previous `codex exec --json` output

## Future Enhancements

Planned improvements to the harness system:

- [ ] Add Horizon harness for specialized perspectives
- [ ] Support Codex shard isolation (git worktrees)
- [ ] SKEIN integration for Codex agents
- [ ] Automatic harness selection based on task complexity
- [ ] Cost tracking per harness
- [ ] Harness-specific configuration profiles
- [ ] Provider auto-detection for Horizon models

## Best Practices

1. **Default to Claude for complex work** - Better reasoning and code understanding
2. **Use Codex for speed** - 30x faster startup for simple tasks
3. **Batch quick tasks with Codex** - Higher throughput for parallel work
4. **Always specify working_dir for Codex** - Required parameter
5. **Use tags to organize** - Tag by harness, task type, or project
6. **Monitor with spool_dashboard()** - Track mixed harness workloads
7. **Test on both harnesses** - Validate that tasks work with your chosen harness

## Examples

### Mixed Harness Workflow

```python
# Complex analysis with Claude
analysis_id = spin(
    "Analyze the caching strategy and recommend improvements",
    permission="readonly"
)

# Quick prototype with Codex
prototype_id = spin(
    "Create a basic LRU cache implementation",
    harness="codex",
    working_dir="/path/to/project"
)

# Wait for both
results = spin_wait(f"{analysis_id},{prototype_id}", mode="gather")

# Continue with Claude based on analysis
session = _read_spool(analysis_id)["session_id"]
implementation_id = respin(
    session,
    "Implement your recommendation using the prototype as a starting point"
)
```

### Parallel Quick Edits

```python
# Launch 10 quick fixes with Codex (fast startup)
tasks = [
    "Add type hints to utils.py",
    "Add docstrings to helpers.py",
    "Format code in main.py",
    # ... more quick tasks
]

spool_ids = [
    spin(task, harness="codex", working_dir="/app")
    for task in tasks
]

# Process results as they complete (yield mode)
for result in spin_wait(",".join(spool_ids), mode="yield"):
    print(f"Completed: {result}")
```

### Fallback Strategy

```python
# Try fast harness first
spool_id = spin(
    "Implement user authentication",
    harness="codex",
    working_dir="/app",
    timeout=60
)

result = unspool(spool_id)

# If it fails or times out, use Claude
if result.get("status") == "error" or result.get("status") == "timeout":
    spool_id = spin(
        "Implement user authentication with proper error handling and tests",
        permission="shard"  # Isolated worktree for safety
    )
```

## See Also

- [docs/CODEX_SETUP.md](CODEX_SETUP.md) - Detailed Codex installation and configuration
- [CODEX_HARNESS.md](../CODEX_HARNESS.md) - Codex harness implementation details
- [README.md](../README.md) - Main Spindle documentation
