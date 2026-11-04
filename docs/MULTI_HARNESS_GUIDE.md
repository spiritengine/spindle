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
**Models:** Claude Fable, Opus, Sonnet, Haiku (Fable 5.1, Opus 5.5, Sonnet 5, Haiku 4.5 as of 2026-09-25)
**Strengths:** Complex reasoning, architecture design, code review

The Claude Code harness is Spindle's default and most capable option. It excels at:
- Deep code understanding and refactoring
- Architectural decisions and design
- Complex multi-file changes
- Thorough code review and analysis

**Usage:**
```python
# Explicit
spin("Refactor auth to use dependency injection", harness="claude-code", working_dir="/project")

# Implicit (default)
spin("Refactor auth to use dependency injection", working_dir="/project")
```

### Codex CLI

**CLI:** `codex` (OpenAI's Codex CLI)
**Models:** GPT-6 Astra (`astra`), GPT-5.6 Sol/Terra/Luna (`sol` is the default), GPT-Reserve (`reserve`); see [CODEX_SETUP.md](CODEX_SETUP.md) for what the ChatGPT-account route serves
**Strengths:** Speed, quick edits, prototyping

The Codex harness prioritizes speed over depth. Ideal for:
- Quick bug fixes and simple edits
- Rapid prototyping and exploration
- Running many parallel tasks
- Time-sensitive operations

**Usage:**
```python
spin(prompt="Add error handling to process_data()", harness="codex", working_dir="/path/to/project")
```

**Important:** `working_dir` is required for Codex, as for every harness.

**Claude model aliases:**
- `"haiku"` / `"sonnet"` / `"opus"` / `"fable"` → passed through as the CLI's rolling "latest of this tier" alias (`fable` is Fable 5.1, `opus` is Opus 5.5 as of 2026-09-25)
- `"fable-5.1"` → `claude-fable-5-1`, `"opus-5.5"` → `claude-opus-5-5`, `"sonnet-5"` → `claude-sonnet-5`, `"haiku-4.5"` → `claude-haiku-4-5`
- `"fable-5"`, `"opus-5"`, `"opus-4.8"`, `"opus-4.7"`, `"opus-4.6"`, `"sonnet-4.6"` → the corresponding legacy ids (still served)
- No model specified → `sonnet`
- Any other string passes through to the CLI as-is

### Gemini CLI

**CLI:** `gemini` (Google's Gemini CLI)
**Models:** Gemini 3.8 Flash, 3.1 Pro Preview (default), 3.5 Flash-Lite, and explicit older aliases
**Strengths:** Generous free tier, good for quick tasks

The Gemini harness uses Google's Gemini CLI in headless mode. It's a full agent with tool use, file access, and multi-step reasoning. Auth is handled by the CLI (Google account login or API key).

**Usage:**
```python
spin(prompt="Summarize this codebase", harness="gemini", working_dir="/path/to/project")
```

**Model aliases:**
- `"flash"` / `"video"` / `"3.8-flash"` → `gemini-3.8-flash`
- `"pro"` / `"3-pro"` → `gemini-3.1-pro-preview` (3 Pro Preview has shut down)
- `"3.1-pro"` → `gemini-3.1-pro-preview`
- `"flash-lite"` / `"3.5-flash-lite"` → `gemini-3.5-flash-lite`
- `"2.5-pro"`, `"2.5-flash"`, `"2.5-flash-lite"` → the corresponding 2.5 models
- No model specified → `gemini-3.1-pro-preview`; Spindle selects it explicitly
- Any other string passes through to the CLI as-is

**Important:** Gemini requires `working_dir` to be specified. Auth via `gemini` interactive login or `GEMINI_API_KEY` env var.

For native video, use `spin("Describe @clip.mp4", harness="gemini", model="video", working_dir="/path/to/clips")`.
Gemini CLI 0.39.1 attaches the video bytes; Spindle does not extract frames.
See [video setup, trust, and limits](GEMINI_VIDEO.md).

### Kimi CLI

**CLI:** `kimi-cli` (Moonshot AI's Kimi Code CLI)
**Models:** Kimi K3, K2.7 Code, K2.6
**Strengths:** Reasoning, multimodal support

The Kimi harness uses Moonshot AI's Kimi Code CLI in headless mode. Full agent with tool use, file access, and multi-step reasoning. Auth via Kimi/Moonshot API credentials configured in `~/.kimi/config.toml`.

Headless Kimi auto-approves tool calls and has no classifier-vetted or genuinely
"careful" mode. Spindle does not claim otherwise: every Kimi process except
`full` without shard intent runs inside Spindle's own bwrap filesystem boundary.
The default write set is the requested working directory plus Kimi's state
directory; the rest of the host is read-only, `/tmp` is private, and `/run` is
replaced so filesystem-path Docker, D-Bus, SSH-agent, and similar runtime sockets
are not reachable. Resolver data needed for outbound API access is restored as a
read-only file. Host process, IPC, UTS, and terminal-session state are isolated.
The network namespace remains shared so Kimi can reach its model API; localhost
services and abstract-namespace Unix sockets such as X11 or D-Bus therefore
remain reachable. Kimi refuses to start if its work directory is read-only, so
`readonly`, `manual`, `careful`, and `research` do not narrow its powers inside
that box. A shard substitutes its worktree, but external Git metadata stays
read-only: Kimi edits the shard and leaves its changes uncommitted. The caller
inspects and commits them inside the worktree, then calls `shard_merge`.
Research adds its explicit file/directory target. Shard intent always stays
contained; `full` without a shard is the explicit uncontained escape hatch.
Bubblewrap is therefore required for normal Kimi use.

**Usage:**
```python
spin(prompt="Analyze this bug and suggest a fix", harness="kimi", working_dir="/path/to/project")
```

**Model aliases:**
- `"k3"` → `moonshot-ai/kimi-k3` with required thinking mode
- `"thinking"` → `moonshot-ai/kimi-k3` with required thinking mode
- `"k2.6"` → `moonshot-ai/kimi-k2.6`
- `"k2.7-code"` / `"code"` → `moonshot-ai/kimi-k2.7-code` with required thinking mode; `"highspeed"` → the highspeed variant
- `"latest"` → `moonshot-ai/kimi-k3` with required thinking mode (latest stable)
- No model specified → `moonshot-ai/kimi-k3` with required thinking mode (default)
- Any other string passes through to the CLI as-is

The standalone `kimi-k2-thinking` / `kimi-k2-thinking-turbo` / `kimi-k2-turbo-preview` models the managed provider used to ship are gone. Thinking is now a model capability enabled with kimi-cli's `--thinking` flag. Spindle enables it automatically for always-thinking K3 and K2.7 Code models, regardless of whether they are selected by alias or full model name.

Every alias target must be registered under `[models.…]` in `~/.kimi/config.toml`. A model that isn't registered makes kimi-cli silently fall back to an empty LLM and emit only `LLM not set`; spindle validates the resolved model up front and returns a clear error (with the list of available models) instead. Upgrade kimi-cli and use interactive `/model` to refresh managed models if a newer one is missing.

**Important:** Kimi requires `working_dir` to be specified. Session continuity uses explicit UUID session IDs generated upfront. Auth via `kimi-cli login` or direct API key configuration in `~/.kimi/config.toml`.

## Unified API

All harnesses use the same API surface, making them interchangeable:

### spin()

MCP `spin` blocks by default and returns `{spool_id, status, result}`. `respin`
uses the same default. Pass `wait=False` for an immediate ID, then `spin_wait`
for completion; use this for every launch in a parallel batch. Headless clients
wait in-band. The CLI and synchronous Python helpers remain detached.

`working_dir` is required for all harnesses. `wait_timeout` limits only the wait;
`timeout` kills the agent. See [result shapes and recovery](../README.md#basic-launch-and-receive-the-result)
for budgeting, tags, cancellation, and reconnecting after reload.

Launch detached agents with any harness:

```python
# Claude Code (default)
spool_id = spin("Complex refactoring task", wait=False, working_dir="/project")

# Codex
spool_id = spin(prompt="Quick edit task", harness="codex", working_dir="/path/to/project", wait=False)
```

**Common parameters (work with all harnesses):**
- `prompt` - The task description
- `working_dir` - Required project directory for every harness
- `wait` - True waits for the result; False returns the ID immediately
- `wait_timeout` - Optional wait deadline, which does not stop the spool
- `harness` - "claude-code", "codex", "gemini", "kimi", or a lodged profile name (use `spin_harnesses()` to discover)
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
- `permission` - Shared Spindle compatibility input, not a Kimi approval mode.
  Shared labels do not narrow its in-box powers; research adds an output target,
  shard substitutes a worktree, and `full` without shard intent opts out.

### unspool()

Get results from any harness - automatically detects which harness the spool used:

```python
result = unspool(spool_id)  # Works for all harnesses
```

The harness is stored in the spool metadata and automatically loaded when you call unspool.

### respin()

Continue a conversation with any harness:

```python
# Initial task finishes before continuing
completion = spin("Write a CSV parser", harness="codex", working_dir="/project")

# Continue using its spool ID (the harness and session are resolved internally)
next_completion = respin(completion["spool_id"], "Add validation to the parser")
```

Session continuity is harness-aware - Spindle remembers which harness was used and routes the respin call appropriately.

## Performance Comparison

| Metric | Claude Code | Codex CLI | Gemini CLI | Kimi CLI |
|--------|-------------|-----------|------------|----------|
| Code understanding | Excellent | Good | Good | Good |
| Reasoning depth | Deep | Moderate | Good (3.1 Pro) | Excellent (K3) |
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
- Reports the provider error if the upstream session is unavailable or expired
- Keeps the saved transcript available for manual reconstruction in a new conversation

**Codex:**
- Uses `codex exec resume <session_id>`
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
spin("Refactor the auth module to use a plugin architecture", working_dir="/project")
```

✅ **Architecture decisions**
```python
spin("Design a caching layer for the API with Redis", working_dir="/project")
```

✅ **Code review and analysis**
```python
spin("Review the payment processor for security issues", working_dir="/project")
```

✅ **Multi-file changes**
```python
spin("Add logging throughout the application", permission="shard", working_dir="/project")
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
ids = [spin(task, harness="codex", working_dir="/app", wait=False) for task in quick_tasks]
spin_wait(",".join(ids))
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

✅ **Coding tasks**
```python
spin("Add input validation to the API endpoints", harness="kimi", working_dir="/app", model="code")
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
spin("Analyze code", permission="readonly", working_dir="/project")
spin("Fix bug", permission="careful", working_dir="/project")  # Default
spin("Refactor module", permission="shard", working_dir="/project")  # Isolated worktree
spin(
    "Research deepseek pricing",
    permission="research",
    research_target="site:spindle-development",
    working_dir="/project",
)
```

### Codex Sandbox Policies

Codex uses OpenAI's sandbox policies, mapped from Claude permissions:

Every codex spool is launched with an explicit `--sandbox` tier, mapped from Claude permissions:

- `permission="readonly"` → `--sandbox read-only`
- `permission="careful"` → `--sandbox workspace-write`
- `permission="full"` → `--sandbox danger-full-access`
- `permission="shard"` → `--sandbox workspace-write` (the `spindle spin` CLI maps `shard` to `danger-full-access`)
- `permission="research"` → no `--sandbox`; the `spindle-research` permissions profile instead: the filesystem is read-only except a `file:`/`dir:` target's directory, and the network is on so `skein post` and `curl` work (no bwrap; codex's own sandbox only)
- `permission="research+shard"` → the same profile + isolated worktree, inside the bwrap wrapper; nothing in the worktree is writable (see [CODEX_SETUP.md](CODEX_SETUP.md#codex-in-shards))

Every tier except `danger-full-access` also gets `-c approval_policy=never`, so a command the
sandbox denies cannot be approved again outside it by an approval setting in
`~/.codex/config.toml`. A command matching a saved codex `allow` rule still runs outside the
sandbox (see [CODEX_SETUP.md](CODEX_SETUP.md#saved-allow-rules)). In a shard, codex's own writable roots are exactly the git metadata
Spindle's bwrap wrapper makes writable, and a shard spool whose codex sandbox cannot start
inside the wrapper is refused (see [Codex Sandbox Enforcement](#codex-sandbox-enforcement)).

The mapping happens automatically in `_codex_spin_sync()`, and the tier that was actually
passed is stored on the spool record as `sandbox` (alongside the requested `permission`).

### Kimi Filesystem Containment

Kimi's `--print` mode auto-approves its tools, so Spindle treats the harness as
fully autonomous inside an external filesystem box:

- default/shared `careful` input: working directory and Kimi state writable
- `readonly` / `manual`: same box; kimi-cli will not run with a read-only work directory
- `research`: same box plus the explicit file/directory output target
- `shard` / `careful+shard`: shard worktree and Kimi state writable; external
  Git metadata remains read-only, so changes are left for the caller to inspect,
  commit inside the worktree, and then merge with `shard_merge`
- `full` without shard intent: no bwrap and no filesystem containment
- `full` with shard intent: shard box; shard intent always wins

`spool_retry` is a fresh rerun. A Kimi shard retry creates a fresh contained
worktree; the original failed worktree remains available for inspection or
recovery.

The shared `careful` string remains an API compatibility input. It is not a
claim about Kimi's behavior. Each spool records the actual boundary, execution
directory, and writable paths. `respin` recomputes the narrow write set and
refuses if a recorded path has been substituted or would widen.

Writable paths may be located beneath `/tmp`, but cannot equal or replace the
sandbox's reserved mounts (`/`, `/tmp`, `/dev`, `/proc`, `/run`, or `/sys`).
For example, use `dir:/tmp/report-output`, not `file:/tmp/report.md`. With shard intent,
`KIMI_SHARE_DIR` must be absolute so the same state path is preflighted before
the worktree is created and used after launch.

Contained launches neutralize dynamic-loader and shell-startup hooks before the
outer wrapper runs (`LD_*`, `DYLD_*`, `BASH_ENV`, `ENV`, `SHELLOPTS`,
`BASHOPTS`, `BASH_FUNC_*`, `PS4`, `BASH_XTRACEFD`, `GCONV_PATH`, `LOCPATH`,
and `NLSPATH`). Ordinary Kimi configuration and secret environment variables
remain available inside the boundary. `full` without shard intent is
uncontained and does not apply this sanitization.

## Codex Sandbox Enforcement

Codex does **not** need kernel Landlock. It sandboxes with its own vendored bubblewrap plus
seccomp (`--use-legacy-landlock` is a fallback for older codex builds), so `--sandbox` is
passed unconditionally and enforces on pre-5.13 kernels. Verified 2026-07-16 on kernel 5.4
against codex-cli 0.125.0: a `read-only` spool cannot write even inside its own cwd, and a
`workspace-write` spool can write inside cwd but not to `$HOME`.

Three traps to know about, each of which silently disables enforcement while leaving the
command line looking correct:

**`--full-auto` overrides `--sandbox`.** It is an alias carrying its own tier and it wins
regardless of flag order. `codex exec --full-auto --sandbox read-only` reports
`sandbox: workspace-write [workdir, /tmp, $TMPDIR]` and happily writes outside the
workspace. Spindle therefore never passes it; `codex exec` is already non-interactive
without it.

**An approval setting can re-run a denied command outside the sandbox.** `codex exec`
defaults to `approval: never`, but only while `~/.codex/config.toml` names no automatic
reviewer. With `approval_policy = "on-request"` and `approvals_reviewer = "auto_review"`, a
read-only spool asked to escalate commands the sandbox had blocked, and a reviewer model ran
them unsandboxed. Spindle therefore pins `-c approval_policy=never` on every restrictive
tier, spin and respin alike. Under `never`, codex 0.154 rejects an escalation request
(`approval policy is Never; reject command`) and never retries a denied command
unsandboxed. `danger-full-access` has no sandbox to escape, so it keeps the operator's
approval settings. The pin does not cover a command that matches a saved codex `allow` rule:
codex runs that command outside its sandbox at any approval policy (see
[CODEX_SETUP.md](CODEX_SETUP.md#saved-allow-rules)).

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

**Inside a shard, codex's sandbox must also be able to start.** On Linux it runs inside
Spindle's bwrap wrapper, and codex 0.154 masks `.git`, `.agents` and `.codex` under every
writable root it is given, mounting an empty read-only directory where one is missing. bwrap
must create that mount point, so every codex writable root has to be writable in the
wrapper too. Spindle grants codex exactly the git paths the wrapper binds
(`<repo>/.git/worktrees/<name>`, `objects`, `refs/heads`, `logs/refs/heads`) plus the
worktree root, and pins `sandbox_workspace_write.writable_roots` to that list. Before each restrictive shard launch,
spin or respin, it runs one no-model `codex sandbox` command through that same wrapper with
the same settings, and refuses the spool if the command did not run, quoting the reason.
That check fails closed: no marker, an error or a timeout all refuse.

**The research tiers run a permissions profile, probed the same way.** codex's `read-only`
sandbox blocks all network, loopback included, so a research spool on it could not
`skein post` to its site. Research tiers therefore pass no `--sandbox`; they select the
`spindle-research` profile on the command line (read the whole filesystem, write only a
`file:`/`dir:` target's directory, network on, approval never). The enforcement probe has a
research variant: under that profile, with a probe-only directory as its one writable path,
the write to the probe's cwd must be refused and the write to that directory must land. A
codex that ignores the profile falls back to some other policy, fails one half, and research
spools are refused.

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
spin("Task 1", harness="claude-code", working_dir="/project", wait=False)  # 1/15
spin("Task 2", harness="codex", working_dir="/app", wait=False)  # 2/15
spin("Task 3", harness="gemini", working_dir="/app", wait=False)  # 3/15
spin("Task 4", harness="kimi", working_dir="/app", wait=False)  # 4/15
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
under `--sandbox` (or, for a research spool, does not apply the research permissions
profile; the message names it), so spindle refused to launch rather than run the spool
unsandboxed. Check what was resolved and why:
```bash
spindle unspool <spool_id>   # the record carries codex_bin, codex_version, and sandbox_error
```
Fix the codex install (or point PATH at a codex that enforces) and re-spin, or use
`permission=full` to run intentionally without a sandbox. The probe fails closed, so an
inconclusive probe (codex missing, `codex sandbox` erroring, a timeout) also refuses.

**Error:** a shard codex spool was refused with `REFUSED: codex's sandbox cannot start inside
shard <worktree> (<reason>)`.

**Cause:** codex's sandbox could not start inside Spindle's bwrap wrapper, so every command
the agent ran would have failed. The reason is the sandbox's own error, usually `bwrap: Can't
mkdir <path>: Read-only file system`, naming a mount point codex needed under a path the
wrapper keeps read-only, which means codex needed to write somewhere the wrapper does not
bind. One known case is a `$TMPDIR` outside
`/tmp`: codex keeps its mount bookkeeping there (`failed to create synthetic bubblewrap
mount registry …`), so unset `TMPDIR` or point it under `/tmp` for the Spindle service.
Otherwise report it with the spool's `error`. A refused new shard is kept for inspection,
like any failed spool's shard.

### Wrong Harness Used

If you get unexpected behavior, verify the harness:

```python
# Check spool metadata: the record's "harness" field
spool_info(spool_id)
```

Harness defaults to "claude-code" if not specified.

### Session Continuity Failures

If `respin()` fails:

1. **Check the session exists:** `spool_info(original_spool_id)` shows the record's
   `session_id`

2. **Verify harness:** the same record's `harness` field (absent means `claude-code`)

3. **Claude Code:** An unavailable or expired upstream session terminates the respin with
   the provider error; use the saved transcript to reconstruct context manually in a new
   conversation
4. **Codex:** Session ID must be valid from previous `codex exec --json` output
5. **Gemini:** Uses `gemini --resume <session_id>`
6. **Kimi:** Uses explicit UUID session ID from spool metadata

## Future Enhancements

Planned improvements to the harness system:

- [ ] Add Horizon harness for specialized perspectives
- [ ] Automatic harness selection based on task complexity
- [ ] Cost tracking per harness
- [ ] Lodged profiles on base harnesses other than Claude Code
- [ ] Provider auto-detection for Horizon models

## Best Practices

1. **Default to Claude for complex work** - Better reasoning and code understanding
2. **Use Codex or Gemini for quick tasks** - Simple edits and lookups
3. **Use Gemini for budget work** - Generous free tier with Google account
4. **Use Kimi for reasoning** - Thinking mode
5. **Always specify working_dir** - Required for every harness
6. **Use tags to organize** - Tag by harness, task type, or project
7. **Monitor with spool_dashboard()** - Track mixed harness workloads
8. **Test on your chosen harness** - Validate that tasks work as expected

## Examples

### Mixed Harness Workflow

```python
# Complex analysis with Claude
analysis_id = spin(
    "Analyze the caching strategy and recommend improvements", permission="readonly", wait=False, working_dir="/project"
)

# Quick prototype with Codex
prototype_id = spin(
    "Create a basic LRU cache implementation", harness="codex", working_dir="/path/to/project", wait=False
)

# Quick research with Gemini
research_id = spin(
    "What caching libraries exist for Python? Summarize the top 3.",
    harness="gemini",
    working_dir="/path/to/project",
    model="flash",
    wait=False,
)

# Deep reasoning with Kimi
review_id = spin(
    "Review the LRU cache for edge cases and thread safety issues",
    harness="kimi",
    working_dir="/path/to/project",
    model="thinking",
    wait=False,
)

# Wait for all
results = spin_wait(f"{analysis_id},{prototype_id},{research_id},{review_id}", mode="gather")

# Continue with Claude based on analysis
implementation = respin(analysis_id, "Implement your recommendation using the prototype as a starting point")
```

### Parallel Quick Edits

```python
import json

# Launch 10 quick fixes with Codex
tasks = [
    "Add type hints to utils.py",
    "Add docstrings to helpers.py",
    "Format code in main.py",
    # ... more quick tasks
]

spool_ids = [spin(task, harness="codex", working_dir="/app", wait=False) for task in tasks]

# Process one completion at a time, always dropping IDs already returned
remaining = spool_ids
while remaining:
    completion = json.loads(spin_wait(",".join(remaining), mode="yield"))
    print(f"Completed: {completion.get('result', completion.get('error'))}")
    remaining = completion.get("remaining", [])
```

### Fallback Strategy

```python
# Try fast harness first and inspect its terminal status
completion = spin("Implement user authentication", harness="codex", working_dir="/app", timeout=60)

# If it fails or times out, use Claude
if completion["status"] in {"error", "timeout"}:
    completion = spin(
        "Implement user authentication with proper error handling and tests",
        working_dir="/app",
        permission="shard",
    )
```

## See Also

- [docs/CODEX_SETUP.md](CODEX_SETUP.md) - Detailed Codex installation and configuration
- [CODEX_HARNESS.md](../CODEX_HARNESS.md) - Codex harness implementation details
- [README.md](../README.md) - Main Spindle documentation
