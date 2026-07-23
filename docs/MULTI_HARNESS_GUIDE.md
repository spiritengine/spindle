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
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Spindle MCP Server                                 │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │              Unified API (spin/unspool/respin/spin_harnesses)          │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│       ▼                ▼                ▼                ▼                   │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │  Claude  │  │    Codex     │  │    Gemini    │  │     Kimi     │        │
│  │  Harness │  │    Harness   │  │    Harness   │  │    Harness   │        │
│  └──────────┘  └──────────────┘  └──────────────┘  └──────────────┘        │
│       ▼                ▼                ▼                ▼                   │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │claude CLI│  │  codex CLI   │  │  gemini CLI  │  │  kimi-cli    │        │
│  └──────────┘  └──────────────┘  └──────────────┘  └──────────────┘        │
└──────────────────────────────────────────────────────────────────────────────┘
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

### Gemini CLI

**CLI:** `gemini` (Google's Gemini CLI)
**Models:** Gemini 2.5 Pro, 2.5 Flash, 3.1 Pro, 3.1 Flash Lite (Auto routing by default)
**Startup time:** ~5-10 seconds to first response
**Strengths:** Fast startup, generous free tier, good for quick tasks

The Gemini harness uses Google's Gemini CLI in headless mode. It's a full agent with tool use, file access, and multi-step reasoning. Auth is handled by the CLI (Google account login or API key).

**Usage:**
```python
spin(
    prompt="Summarize this codebase",
    harness="gemini",
    working_dir="/path/to/project"
)
```

**Model aliases:**
- `"flash"` → `gemini-2.5-flash`
- `"pro"` → `gemini-2.5-pro`
- `"3.1-pro"` → `gemini-3.1-pro-preview`
- `"flash-lite"` → `gemini-2.5-flash-lite`
- No model specified → CLI's Auto routing (picks based on task complexity)
- Any other string passes through to the CLI as-is

**Important:** Gemini requires `working_dir` to be specified. Auth via `gemini` interactive login or `GEMINI_API_KEY` env var.

### Kimi CLI

**CLI:** `kimi-cli` (Moonshot AI's Kimi Code CLI)
**Models:** Kimi K3, K2.7 Code, K2.6, K2.5
**Startup time:** ~5-10 seconds to first response
**Strengths:** Fast startup, reasoning, multimodal support

The Kimi harness uses Moonshot AI's Kimi Code CLI in headless mode. Full agent with tool use, file access, and multi-step reasoning. Auth via Kimi/Moonshot API credentials configured in `~/.kimi/config.toml`.

**Usage:**
```python
spin(
    prompt="Analyze this bug and suggest a fix",
    harness="kimi",
    working_dir="/path/to/project"
)
```

**Model aliases:**
- `"k3"` → `moonshot-ai/kimi-k3` with required thinking mode
- `"thinking"` → `moonshot-ai/kimi-k3` with required thinking mode
- `"k2.6"` → `moonshot-ai/kimi-k2.6`
- `"k2.5"` → `moonshot-ai/kimi-k2.5`
- `"latest"` → `moonshot-ai/kimi-k3` with required thinking mode (latest stable)
- No model specified → `moonshot-ai/kimi-k3` with required thinking mode (default)
- Any other string passes through to the CLI as-is

The standalone `kimi-k2-thinking` / `kimi-k2-thinking-turbo` / `kimi-k2-turbo-preview` models the managed provider used to ship are gone. Thinking is now a model capability enabled with kimi-cli's `--thinking` flag. Spindle enables it automatically for always-thinking K3 and K2.7 Code models, regardless of whether they are selected by alias or full model name.

Every alias target must be registered under `[models.…]` in `~/.kimi/config.toml`. A model that isn't registered makes kimi-cli silently fall back to an empty LLM and emit only `LLM not set`; spindle validates the resolved model up front and returns a clear error (with the list of available models) instead. Upgrade kimi-cli and use interactive `/model` to refresh managed models if a newer one is missing.

**Important:** Kimi requires `working_dir` to be specified. Session continuity uses explicit UUID session IDs generated upfront. Auth via `kimi-cli login` or direct API key configuration in `~/.kimi/config.toml`.

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
- `harness` - "claude-code", "codex", "gemini", or "kimi" (use `spin_harnesses()` to discover)
- `model` - Model to use (harness-specific)
- `timeout` - Auto-kill after N seconds
- `tags` - Organization tags

**Claude-specific parameters:**
- `permission` - "readonly" (alias "manual"), "careful" (default, = auto), "full", "shard", "careful+shard", "research", "research+shard", "auto", "auto+shard" (readonly/manual cannot be combined with a shard — rejected however the shard intent arrives: readonly+shard/manual+shard, or readonly/manual with shard=True)
- `shard` - Auto-create git worktree
- `system_prompt` - Custom system instructions
- `allowed_tools` - Explicit tool permissions

**Codex-specific parameters:**
- `working_dir` - Required project directory
- `sandbox` - Derived from permission parameter

**Gemini-specific parameters:**
- `working_dir` - Required project directory
- `system_prompt` - Prepended to prompt (Gemini CLI has no separate system prompt flag)

**Kimi-specific parameters:**
- `working_dir` - Required project directory
- `system_prompt` - Prepended to prompt

### unspool()

Get results from any harness - automatically detects which harness the spool used:

```python
result = unspool(spool_id)  # Works for all harnesses
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

| Metric | Claude Code | Codex CLI | Gemini CLI | Kimi CLI |
|--------|-------------|-----------|------------|----------|
| Startup time | 3-4 minutes | ~10 seconds | ~5-10 seconds | ~5-10 seconds |
| Code understanding | Excellent | Good | Good | Good |
| Reasoning depth | Deep | Moderate | Good (2.5 Pro) | Excellent (K2-Thinking) |
| Best for | Complex tasks | Quick edits | Fast general work | Fast reasoning tasks |
| Cost per task | Higher | Lower | Free tier available | Varies by plan |

**Rule of thumb:** Use Claude for deep thinking, Codex for typing, Gemini for a fast free option, Kimi for fast reasoning with thinking mode.

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

All harnesses support session continuity through `respin()`:

**Claude Code:**
- Uses `--resume <session_id>` flag
- Falls back to transcript injection if session expired
- Maintains full conversation context

**Codex:**
- Uses `codex resume <session_id>` command
- Session IDs extracted from JSON output
- Preserves conversation state

**Gemini:**
- Uses `gemini --resume <session_id>` flag
- Session IDs extracted from JSON output

**Kimi:**
- Uses `kimi-cli --session <session_id>` with explicit UUID
- Session IDs generated upfront and stored in spool metadata

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

### Use Gemini for:

✅ **Fast general tasks with free tier**
```python
spin("Explain this error message", harness="gemini", working_dir="/app")
```

✅ **Quick code generation**
```python
spin("Generate a JSON schema for the user API", harness="gemini", working_dir="/app")
```

✅ **Parallel research tasks** (generous rate limits)
```python
spin("Summarize the test coverage", harness="gemini", working_dir="/app", model="flash")
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

### Use Kimi for:

✅ **Complex reasoning at speed** (thinking mode)
```python
spin("Analyze this race condition and propose a fix", harness="kimi", working_dir="/app")
```

✅ **Deep code analysis**
```python
spin("Review this module for subtle bugs", harness="kimi", working_dir="/app", model="thinking")
```

✅ **Fast general tasks**
```python
spin("Add input validation to the API endpoints", harness="kimi", working_dir="/app", model="turbo")
```

## Sandbox and Permission Models

### Claude Code Permissions

Claude maps each tier to a `--permission-mode` (and, for the tight tier, an `--allowedTools` allowlist):

- **readonly** (alias **manual**) - the one tight, no-exec tier: Read, Grep, Glob, safe bash (ls, cat, git status/log/diff). `acceptEdits` + an allowlist; no python, no find, no write.
- **careful** (default) - now an alias of **auto**: `--permission-mode auto`, no allowlist. Claude Code vets each tool call server-side on intent. (Previously a Bash allowlist that gated capability on command phrasing rather than security; `auto` removes that gate.)
- **full** - No restrictions (`bypassPermissions`)
- **shard** - Full permissions + isolated git worktree (`bypassPermissions` inside the bwrap-contained shard)
- **careful+shard** - `auto` semantics + worktree (`bypassPermissions` inside the bwrap-contained shard)
- **readonly+shard / manual+shard** - rejected: the readonly/manual tier has no write tools, so a shard (isolated worktree for changes) is incoherent; use careful+shard or shard for isolated write work
- **auto** / **auto+shard** - explicit aliases of the careful default
- **research** - WebFetch/WebSearch/curl/jq enabled; no python/find/Write/Edit; requires `research_target` (site:, file:, or dir: prefix)
- **research+shard** - research tools + isolated git worktree

Example:
```python
spin("Analyze code", permission="readonly")
spin("Fix bug", permission="careful")  # Default
spin("Refactor module", permission="shard")  # Isolated worktree
spin("Research deepseek pricing", permission="research", research_target="site:spindle-development")
```

### Codex Sandbox Policies

Codex uses OpenAI's sandbox policies, mapped from Claude permissions:

Every codex spool is launched with an explicit `--sandbox` tier, mapped from Claude permissions:

- `permission="readonly"` → `--sandbox read-only`
- `permission="careful"` → `--sandbox workspace-write`
- `permission="full"` → `--sandbox danger-full-access`
- `permission="shard"` → `--sandbox workspace-write` (the `spindle spin` CLI maps `shard` to `danger-full-access`)
- `permission="research"` → site target: `--sandbox read-only`; file/dir target: `--sandbox workspace-write` + `--add-dir` for the target path (no bwrap — plain research uses Codex native sandbox only)
- `permission="research+shard"` → same conditional sandbox as `research` + isolated worktree; bwrap adds OS-level isolation on top when available

The mapping happens automatically in `_codex_spin_sync()`, and the tier that was actually
passed is stored on the spool record as `sandbox` (alongside the requested `permission`).

## Codex Sandbox Enforcement

Codex does **not** need kernel Landlock. It sandboxes with its own vendored bubblewrap plus
seccomp (`--use-legacy-landlock` is a fallback for older codex builds), so `--sandbox` is
passed unconditionally and enforces on pre-5.13 kernels. Verified 2026-07-16 on kernel 5.4
against codex-cli 0.125.0: a `read-only` spool cannot write even inside its own cwd, and a
`workspace-write` spool can write inside cwd but not to `$HOME`.

Two traps to know about, both of which silently disable enforcement while leaving the
command line looking correct:

**`--full-auto` overrides `--sandbox`.** It is an alias carrying its own tier and it wins
regardless of flag order. `codex exec --full-auto --sandbox read-only` reports
`sandbox: workspace-write [workdir, /tmp, $TMPDIR]` and happily writes outside the
workspace. Spindle therefore never passes it; `codex exec` is already non-interactive
(`approval: never`) without it.

**Enforcement is not guaranteed and is not a property of the version string.** A
`sandbox_mode` in `~/.codex/config.toml` can override `--sandbox`, and the vendored sandbox
can fail to spawn — either way a read-only spool may run with no write boundary while the
command line and the spool record still say read-only. The *same* codex version has been
observed both enforcing and failing open, so a version allow/deny list cannot capture it.

Spindle therefore decides by **behavior, not version**. `_codex_sandbox_enforces()` probes
the resolved binary once (cached per path/version/mtime for the process lifetime) using
codex's no-model `codex sandbox` subcommand under read-only: it runs a shell command that
tries to write inside a scratch cwd and checks the write was blocked. It adds no model call
and is off the per-spool hot path. When a **restrictive** tier (`read-only` /
`workspace-write`) is requested and the probe reports the sandbox is not enforcing, the
launch is **refused** — `_codex_spin_sync`/`_codex_respin_sync` return an error and persist a
spool with `status: "error"` and a `sandbox_error` field (visible via `unspool`/`spool_info`)
rather than silently running unsandboxed. `danger-full-access` (the `full` tier) asks for no
sandbox, so it is never probed and never refused. Each spool still records the resolved
`codex_bin` and `codex_version` for provenance, but they no longer gate anything.

## Spool Management

All harnesses integrate seamlessly with Spindle's spool management:

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

Non-default harness spools are automatically tagged with their harness name ("codex", "gemini", "kimi") for easy filtering.

## Concurrency Limits

**All harnesses share the same concurrency limit:** 15 spools maximum.

This prevents resource exhaustion regardless of which harness you use:

```python
# Mix of harnesses, max 15 total
spin("Task 1", harness="claude-code")  # 1/15
spin("Task 2", harness="codex", working_dir="/app")  # 2/15
spin("Task 3", harness="gemini", working_dir="/app")  # 3/15
spin("Task 4", harness="kimi", working_dir="/app")  # 4/15
# ... up to 15 total
```

If you hit the limit:
```
Error: At concurrency limit (15/15). Wait for spools to complete or kill some.
```

Use `spools()` to check status or `spin_drop(spool_id)` to cancel running work.

## Troubleshooting

### Harness Not Found

**Error:** `"codex: command not found"` or `"gemini: command not found"` or `"kimi-cli: command not found"`

**Solution:** Install the CLI:
```bash
npm i -g @openai/codex       # For Codex
npm i -g @google/gemini-cli  # For Gemini
pip install kimi-cli         # For Kimi
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

**Gemini:**
```bash
gemini --version  # Verify installation
gemini            # Run interactively, select "Login with Google"
# Or set GEMINI_API_KEY environment variable
```

**Kimi:**
```bash
kimi-cli --version  # Verify installation
kimi-cli login      # Authenticate
# Or configure API key in ~/.kimi/config.toml
```

### Sandbox Errors (Codex)

**Error:** every shell command in a codex spool fails with
```
bwrap: execvp codex-linux-sandbox: No such file or directory
```

**Cause:** codex materializes its sandbox helper as a symlink under
`$CODEX_HOME/tmp/arg0/` at startup and prepends that directory to PATH. If `~/.codex` is
not writable, the helper is never created and every sandboxed command fails to exec. Codex
only warns (`WARNING: proceeding, even though we could not update PATH`) and then proceeds,
so the spool runs but can do nothing. This fails closed, not open — no command escapes the
sandbox — but the spool is useless.

**Solution:** make `~/.codex` writable for the process running codex. Anything wrapping
codex in its own sandbox must bind `~/.codex` read-write (spindle's `_codex_bwrap_wrap`
does). Note codex refuses to create the helper when `CODEX_HOME` is under a temp dir
(`Refusing to create helper binaries under temporary dir "/tmp"`).

**Error:** a restrictive codex spool was refused with a `REFUSED: codex sandbox is not
enforcing …` error.

**Cause:** the enforcement probe found that the resolved codex does not actually block writes
under `--sandbox`, so spindle refused to launch rather than run the spool unsandboxed. Check
what was resolved and why:
```bash
spindle unspool <spool_id>   # the record carries codex_bin, codex_version, and sandbox_error
```
Fix the codex install (or point PATH at a codex that enforces) and re-spin, or use
`permission=full` to run intentionally without a sandbox. The probe fails closed, so an
inconclusive probe (codex missing, `codex sandbox` erroring, a timeout) also refuses.

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
5. **Gemini:** Uses `gemini --resume <session_id>`
6. **Kimi:** Uses explicit UUID session ID from spool metadata

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
2. **Use Codex or Gemini for speed** - Much faster startup for simple tasks
3. **Use Gemini for budget work** - Generous free tier with Google account
4. **Use Kimi for fast reasoning** - Thinking mode at speed
5. **Always specify working_dir for Codex/Gemini/Kimi** - Required parameter
5. **Use tags to organize** - Tag by harness, task type, or project
6. **Monitor with spool_dashboard()** - Track mixed harness workloads
7. **Test on your chosen harness** - Validate that tasks work as expected

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

# Quick research with Gemini
research_id = spin(
    "What caching libraries exist for Python? Summarize the top 3.",
    harness="gemini",
    working_dir="/path/to/project",
    model="flash"
)

# Deep reasoning with Kimi
review_id = spin(
    "Review the LRU cache for edge cases and thread safety issues",
    harness="kimi",
    working_dir="/path/to/project",
    model="thinking"
)

# Wait for all
results = spin_wait(f"{analysis_id},{prototype_id},{research_id},{review_id}", mode="gather")

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
