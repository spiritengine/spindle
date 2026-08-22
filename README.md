# Spindle

<!-- Uncomment when published:
[![PyPI version](https://badge.fury.io/py/spindle.svg)](https://badge.fury.io/py/spindle)
[![CI](https://github.com/spiritengine/spindle/actions/workflows/ci.yml/badge.svg)](https://github.com/spiritengine/spindle/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
-->

MCP server for multi-harness AI agent delegation. Spawn background agents (Claude Code, Codex, Gemini, Kimi) that run asynchronously, with optional git worktree isolation for safe parallel work.

## Features

- **Async agent spawning** - Fire-and-forget pattern with spool IDs
- **Optional blocking with gather/yield** - Wait for all results at once, or stream them as agents complete. Alternatively, agent can continue other work, spins are nonblocking by default
- **Permission profiles** - Control what tools child agents can use (readonly, careful, full)
- **Shard isolation** - Run agents in sandboxed git worktrees to prevent conflicts
- **Model selection** - Route tasks to different models per-agent
- **Session continuity** - Resume conversations while the upstream provider session remains available; saved transcripts remain available for manual reconstruction when it does not
- **Rich querying** - Search, filter, peek at running output, export results
- **`spindle doctor`** - One command to check the install: versions, paths, spool store, detected harnesses, and an optional live read-only smoke

## Requirements

- Python 3.11+
- At least one harness CLI, installed and already authenticated. Spindle shells
  out to the CLI you already use and inherits its login — it never asks for an
  API key of its own:
  - [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — the default harness
  - [Codex CLI](https://developers.openai.com/codex/cli/) (`codex`)
  - [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`)
  - [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) (`kimi-cli`)
- Git (for shard/worktree functionality)
- `bwrap` (bubblewrap), on Linux, for filesystem-contained shards and every
  non-`full` Kimi launch

## Install

```bash
pip install spindle-mcp
```

Check what the install can actually see and do:

```bash
spindle doctor
```

Doctor reports this CLI's version and path, whether the service on the port is
this same install, whether the spool store is writable, and which harness CLIs
it found. Add `--smoke` to have it spawn one real read-only headless agent per
harness and verify the answer comes back:

```bash
spindle doctor --smoke
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

That runs spindle over stdio, which needs no background service. Run a service
only if you want the HTTP transport or a shared long-lived instance — see
[Background service](#background-service).

## Command line

The core spawn-and-collect loop is available as subcommands, so you can drive
spindle from a shell or a script without an MCP client. (The querying tools —
search, grep, stats, export, and the shard commands — are MCP-only for now.)
Commands print JSON by default; `--human` prints text.

```bash
# Spawn an agent (returns a spool id immediately)
spindle spin "Summarize this module" --harness codex --permission readonly -d /path/to/project

# Any built-in harness or lodged profile name works
spindle spin "Quick pass" --harness claude-code --model haiku
spindle spin "Alt endpoint" --harness my-profile

# Collect
spindle unspool <spool_id>
spindle spools --human
spindle peek <spool_id> -n 100
spindle wait <id1>,<id2> --mode yield
spindle drop <spool_id>

# This install
spindle --version
spindle doctor [--smoke] [--json] [--port N]
spindle status [--port N]
```

`spindle spin` takes the same harness names as the `spin` tool: the four
built-ins plus any lodged profile. An unknown name is an error, not a silent
fallback to Claude Code.

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
# Read-only / manual: the one tight, no-exec tier (allowlist-enforced)
spin("Analyze the codebase", permission="readonly")   # "manual" is an alias

# Careful (default): classifier-vetted auto — CC vets each tool call server-side
spin("Fix this bug", permission="careful")

# Full access: for initial setup, dependency installs, environment provisioning
spin("Set up a new Python project with dependencies", permission="full")

# Shard: Full access + auto-isolated worktree (common for risky work)
spin("Refactor the auth system", permission="shard")

# Careful + shard: classifier-vetted, isolated in a bwrap-contained worktree
spin("Update configs", permission="careful+shard")

# Research: web/file research routed to a SKEIN site, a single file, or a directory
spin("Research deepseek vs kimi", permission="research", research_target="site:spindle-development")
```

Profiles (claude-code harness):
- `readonly` (alias `manual`): Read, Grep, Glob, safe bash (ls, cat, git status/log/diff). The only tier still governed by an allowlist — no python, no find, no write. This is the tight, inspectable, manual option.
- `careful` (default): now an alias of `auto`. No allowlist; runs under `--permission-mode auto`, where Claude Code vets each tool call server-side on intent. Use it for most code work including reviews/fells. (It used to be a Bash allowlist that gated capability on command *phrasing*, not security — `auto` removes that gate.)
- `full`: No restrictions
- `shard`: Full access + auto-creates isolated worktree (bypass inside the bwrap-contained shard)
- `careful+shard`: `auto` semantics + auto-creates isolated worktree (bypass inside the bwrap-contained shard)
- `research`: Read, Grep, Glob, WebFetch, WebSearch, curl, jq, safe bash; no python/find; requires `research_target` (Write/Edit added when target is `file:` or `dir:`)
- `research+shard`: research tools + auto-creates isolated worktree

Web-egress work (WebFetch, WebSearch, curl) belongs in `research` — the other code tiers intentionally have no web access so they're safe for code review and code-modifying work.

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
id1 = spin("Find all TODO comments")
id2 = spin("List unused imports")
id3 = spin("Check for type errors")

# Gather: block until all complete, get all results
results = spin_wait("id1,id2,id3", mode="gather")

# Yield: return as each completes
# Great when results are independent - process each as it lands
result = spin_wait("id1,id2,id3", mode="yield")  # Returns first to finish

# With timeout
results = spin_wait("id1,id2", mode="gather", timeout=300)
```

Yield mode keeps you responsive instead of blocking on the slowest agent.

### Time-based waiting

Simple timed waiting with `spin_sleep`:

```
spin_sleep("90m")       # Sleep for 90 minutes
spin_sleep("2h")        # Sleep for 2 hours
spin_sleep("30s")       # Sleep for 30 seconds
spin_sleep("06:00")     # Wait until 6 AM
```

Or use `spin_wait` with the `time` parameter:

```
spin_wait(time="90m")
spin_wait(time="06:00")  # Handles next-day wraparound
```

Useful for periodic check-in loops (e.g., QM/dancing partner patterns).

### Model selection and timeouts

```
# Route quick tasks to haiku (fast, cheap)
spin("Summarize this file", model="haiku")

# Complex work to opus
spin("Design the new architecture", model="opus")

# Pin Claude Opus 5
spin("Implement the new architecture", model="opus-5")

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

If Claude reports that the provider session is unavailable or expired, the respin terminates with that provider error.
The saved transcript remains available for manual reconstruction in a new conversation.

### Cancel running work

```
spin_drop(spool_id)
```

### List all spools

```
spools()
```

### Large results

Most results are small and return whole. Very long results (over ~50K chars)
are truncated by `unspool()` to their head and tail, with a breadcrumb showing
how to retrieve the rest. The full text always stays in the spool; truncation
only shapes the default read.

```
# Default read - budgeted (head + tail if the result is huge)
unspool(spool_id)

# Get the entire result, no truncation
unspool(spool_id, full=True)

# Page through a slice
unspool(spool_id, offset=12000, limit=20000)

# Write the full result to a file (agent-driven, not automatic)
spool_export(spool_id, format="md", output_path="/tmp/result.md")
```

Tune the thresholds with `SPINDLE_UNSPOOL_MAX_CHARS` (default 50000),
`SPINDLE_UNSPOOL_HEAD_CHARS` (12000), and `SPINDLE_UNSPOOL_TAIL_CHARS` (12000).

### Search and filter

```
# Search prompts and results
spool_search("authentication")

# Filter by status and time
spool_results(status="error", since="1h")

# Regex search across all spool results
spool_grep("error|failed|exception")

# Dig into one huge result - matching lines with context, no full pull
spool_grep("error|failed", spool_id="abc123", context=3)

# Get statistics
spool_stats()

# Export to file
spool_export("all", format="md")
```

## Multi-Harness Support

Spindle supports multiple AI agent harnesses, allowing you to choose the best tool for each task.

### Available Harnesses

**Claude Code** (default) - Anthropic's Claude models via `claude` CLI
- Superior code understanding and reasoning
- Best for complex refactoring, architecture decisions
- Slower startup (~3-4 minutes to first response)
- Use `harness="claude-code"` or omit harness parameter

**Codex CLI** - OpenAI's GPT-5 Codex models via `codex` CLI
- Extremely fast startup (~10 seconds to first response)
- Good for quick edits, simple tasks, prototyping
- Requires ChatGPT Plus/Pro/Enterprise
- Use `harness="codex"`

**Gemini CLI** - Google's Gemini models via `gemini` CLI
- Fast startup (~5-10 seconds to first response)
- Full agent with tool use, file access, multi-step reasoning
- Generous free tier (1000 req/day with Google account)
- Models: `"flash"`, `"pro"`, or any full model name
- Use `harness="gemini"`

**Kimi CLI** - Moonshot AI's Kimi models via `kimi-cli`
- Fast startup (~5-10 seconds to first response)
- Thinking mode for complex reasoning
- Models: `"k3"`/`"latest"`/`"thinking"` (K3, always thinking; default), `"k2.7-code"`, `"k2.6"`, `"k2.5"`, or any full model name
- Kimi headless mode auto-approves its tool calls; it has no classifier-vetted
  or genuinely "careful" mode. Spindle therefore wraps every Kimi process
  except `full` without shard intent in its own bwrap filesystem boundary. The
  default makes only the requested working directory and Kimi's state directory
  writable, with private `/tmp` and filesystem-path runtime sockets hidden behind
  a private `/run`; host processes and IPC are in separate namespaces. The
  network namespace remains shared, so localhost services and abstract Unix
  sockets remain reachable. Shards substitute their worktree, and research adds
  its explicit output target. External Git metadata stays read-only, so Kimi leaves shard changes
  uncommitted. The caller inspects and commits them in the worktree, then calls
  `shard_merge`. Kimi refuses to
  start with a read-only work directory, so shared names such as `readonly` and
  `careful` do not narrow its powers inside that box. Shard intent always stays
  contained; `full` without a shard explicitly runs uncontained. Shard launches
  require an absolute `KIMI_SHARE_DIR` when that override is used.
- Use `harness="kimi"`

### Basic Usage

```python
# Claude Code (default) - best for complex work
spool_id = spin("Refactor the auth module to use dependency injection")

# Codex CLI - fast for simple tasks
spool_id = spin(prompt="Add error handling to this function", harness="codex", working_dir="/path/to/project")

# Gemini CLI - fast with free tier
spool_id = spin(prompt="Summarize this codebase", harness="gemini", working_dir="/path/to/project")

# Kimi CLI - fast reasoning with thinking mode
spool_id = spin(prompt="Analyze this bug", harness="kimi", working_dir="/path/to/project")

# All harnesses use the same API
result = unspool(spool_id)  # Auto-detects harness
```

### Choosing a Harness

**Use Claude Code when:**
- Task requires deep reasoning or architecture decisions
- Working on complex refactoring across multiple files
- Need thorough code review or analysis

**Use Codex when:**
- Need quick edits or simple implementations
- Prototyping or exploring ideas rapidly

**Use Gemini when:**
- Want fast results without API key management (Google account login)
- Running many parallel tasks on a budget (free tier)
- Need a quick general-purpose agent

**Use Kimi when:**
- Need thinking mode for complex reasoning at speed
- Want fast startup with strong reasoning capabilities

### Requirements

**Claude Code:**
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

**Codex CLI:**
- [Codex CLI](https://developers.openai.com/codex/cli/) installed (`npm i -g @openai/codex`)
- ChatGPT Plus/Pro/Enterprise subscription

**Gemini CLI:**
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) installed (`npm i -g @google/gemini-cli`)
- Google account login (`gemini` → "Login with Google") or `GEMINI_API_KEY` env var

**Kimi CLI:**
- [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) installed (`pip install kimi-cli`)
- Auth via `kimi-cli login` or API key in `~/.kimi/config.toml`
- Bubblewrap (`bwrap`) installed for normal use; only `permission="full"` opts
  out of Spindle's external filesystem containment

See [docs/MULTI_HARNESS_GUIDE.md](docs/MULTI_HARNESS_GUIDE.md) and [docs/CODEX_SETUP.md](docs/CODEX_SETUP.md) for detailed documentation.

## Profiles

A **profile** is a named, lodged configuration: a base harness plus a set of
overrides (model, alt-endpoint env, extra CLI flags). The motivating use is
running any Anthropic-compatible model through the existing Claude Code harness
by injecting `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `CLAUDE_CONFIG_DIR`
into the spawned child — spindle's output parsing, `unspool`, and `respin` all
work unchanged because the child is still plain Claude Code.

A profile is a **folder** (the folder name is the profile name) containing a
single `profile.json`. Profiles are discovered from two locations, later
overriding earlier:

1. **Canonical:** `~/.spindle/profiles/<name>/profile.json` — where real,
   private profiles live, outside any repo.
2. **Dev convenience:** `./profiles/<name>/profile.json` relative to the current
   working directory (gitignored).

Use a profile by passing its name as `harness`:

```python
# Define ~/.spindle/profiles/my-endpoint/profile.json, then:
spool_id = spin("Summarize this module", harness="my-endpoint", working_dir="/proj")
spool_id = spin("Quick pass", harness="my-endpoint", model="fast")  # profile model_aliases
result = unspool(spool_id)
```

Built-in harness names (`claude-code`, `codex`, `gemini`, `kimi`) always win
over a same-named profile. `spin_harnesses()` lists lodged profiles alongside
the built-ins.

### profile.json fields

All fields are optional except that the file must parse as a JSON object:

- `description` — one-liner shown in `spin_harnesses()`
- `harness` — base harness (default `"claude-code"`; only `"claude-code"` is
  supported as a base in v1)
- `model` — default model (a caller-passed `model` still wins)
- `model_aliases` — profile-scoped alias map applied to the caller's `model`
- `base_url` — sets `ANTHROPIC_BASE_URL`
- `api_key` — sets `ANTHROPIC_API_KEY`
- `config_dir` — sets `CLAUDE_CONFIG_DIR` (defaults to an isolated per-profile
  dir when `base_url` is set, so the child doesn't load your real `~/.claude`)
- `env` — arbitrary extra child env vars
- `extra_args` — flags appended verbatim to the `claude` CLI

### Secret resolution

Every string value is resolved fresh at spawn time (so rotated secrets take
effect on respin):

1. `${ENV_VAR}` is expanded from the environment; an unset var is left literal
   and a warning is logged.
2. A value containing `op://` is resolved via `strongbox inject` (if `strongbox`
   is on PATH) or `op inject` (if `op` is on PATH); if neither exists it's left
   literal. This keeps 1Password/strongbox an optional convenience — the
   `${ENV}` path needs no external tool.

See [examples/profiles/anthropic-compatible/](examples/profiles/anthropic-compatible/)
for a worked example and the full schema reference.

## API

### Unified API (works with all harnesses)

| Tool | Purpose |
|------|---------|
| `spin(prompt, permission?, shard?, system_prompt?, working_dir?, allowed_tools?, tags?, model?, timeout?, harness?)` | Spawn agent, return spool_id |
| `unspool(spool_id, full?, offset?, limit?)` | Get result (auto-detects harness, non-blocking; truncates huge results to head+tail by default) |
| `respin(session_id, prompt)` | Continue session (auto-detects harness) |

**spin() parameters:**
- `prompt` (required): The task for the agent
- `harness` (optional): "claude-code" (default), "codex", "gemini", or "kimi"
- `working_dir` (optional for Claude, required for Codex/Gemini/Kimi): Project directory
- `permission` (optional): "readonly" (alias "manual"), "careful" (default, = auto), "full", "shard", "careful+shard", "research", "research+shard", "auto", "auto+shard" (readonly/manual cannot be combined with shard intent; `auto` variants are Claude-only; other names map to harness-specific enforcement, and Kimi accepts them for compatibility but has no "careful" approval mode)
- `model` (optional): Model to use ("sonnet", "opus", "opus-5", "haiku" for Claude; "flash", "pro" for Gemini; "k3", "latest", "thinking", "k2.7-code", "k2.6", "k2.5" for Kimi)
- `timeout` (optional): Auto-kill after N seconds
- `tags` (optional): Comma-separated tags for organization
- `shard` (optional): Create isolated git worktree (can also use `permission="shard"`)
- `system_prompt` (optional): Custom system prompt for Claude Code
- `allowed_tools` (optional): Override permission profile with explicit tool list

### Spool Management (works with all harnesses)

| Tool | Purpose |
|------|---------|
| `spools()` | List all spools |
| `spin_wait(spool_ids?, mode?, timeout?, time?)` | Block until spools complete, or wait for duration |
| `spin_sleep(duration)` | Sleep for a duration (90m, 2h, 30s, HH:MM) |
| `spin_drop(spool_id)` | Cancel by killing process |
| `spool_search(query, field?)` | Search prompts/results |
| `spool_results(status?, since?, limit?)` | Bulk fetch with filters |
| `spool_grep(pattern, spool_id?, context?)` | Regex search results; pass spool_id for line-level matches with context in one result |
| `spool_retry(spool_id)` | Re-run with same params |
| `spool_peek(spool_id, lines?)` | See partial output while running |
| `spool_dashboard()` | Overview of running/complete/needs-attention |
| `spool_stats()` | Get summary statistics |
| `spin_harnesses()` | List available harnesses, models, and defaults |
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

## Service commands

```bash
spindle install-service  # Install background service (Linux/macOS)
spindle start            # Start via systemd (or background if no service)
spindle reload           # Drain (wait for spools to finish), then restart
spindle reload --force   # Restart immediately, interrupting in-flight spools
spindle status           # Health of the service on this port
spindle doctor           # Diagnose this install (see Install, above)
spindle serve --http     # Run MCP server directly
```

### Background Service

For persistent background operation:

```bash
# Install and enable the service (Linux or macOS)
spindle install-service

# Start it
spindle start
```

**Linux**: Writes a systemd user service to `~/.config/systemd/user/spindle.service`

**macOS**: Writes a launchd plist to `~/Library/LaunchAgents/com.spindle.server.plist` and loads it immediately

The generated unit bakes in the current `PATH`, `SPINDLE_PORT`, and
`SPINDLE_HOME`. `PATH` matters: a systemd user unit otherwise starts with a
minimal one and cannot find `claude`/`codex`/`gemini`, which shows up much later
as a spool that fails at spawn.

Because that `PATH` is a snapshot, it goes stale — installing a harness
somewhere new, or a node upgrade relocating `codex`, leaves the service unable
to find what your shell finds fine. `spindle doctor` compares the two and says
so, naming the service. The fix is to re-run `install-service --name <name>
--port <port> --force` from a shell with the right `PATH`, then
`spindle reload --name <name>`.

`--force` overwrites a service file spindle wrote earlier. If the file does not
carry spindle's marker — because you wrote it, or because you copied an example
and edited it — `--force` first copies it aside as `<name>.service.bak-<date>`
and says where. Ownership of a service file cannot be inferred reliably, so
spindle keeps a copy rather than guess.

Then `spindle reload` restarts the service to pick up code changes.

### Two installs on one machine

A released wheel and a working checkout can serve at once, as long as the second
gets its own service name, port, and spool store:

```bash
SPINDLE_HOME=~/.spindle-release spindle install-service --name spindle-release --port 8042
spindle start --name spindle-release
spindle doctor --port 8042
```

`spindle status` and `spindle doctor` compare the version and package path the
service reports at `/health` against the CLI that is asking. A service that is a
different install, or the same install running older code, is reported as such
rather than counted as healthy — so a fresh install cannot mistake an existing
service for its own. Point either command at a specific service with `--port`,
or set `SPINDLE_PORT` once in the environment.

### Windows

On Windows, run spindle manually:

```bash
spindle serve --http
```

Or use [NSSM](https://nssm.cc/) to create a Windows service.

### WSL

In WSL2 with systemd enabled, `spindle install-service` works like native Linux. If systemd isn't enabled, you'll get instructions to enable it or run manually.

### Hot Reload (MCP tool)

From within Claude Code, call `spindle_reload()` to pick up code changes. By
default it drains first: it returns immediately and restarts in the background
once no spools are running or pending, so in-flight agents finish cleanly. New
spins are still accepted while draining; the restart happens at the next idle
moment. Pass `force=True` to restart immediately (the old behavior), which may
interrupt in-flight spools and leave them to orphan recovery on the next boot.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SPINDLE_HOME` | `~/.spindle` | Spool store (`$SPINDLE_HOME/spools`) and lodged profiles |
| `SPINDLE_PORT` | `8002` | Port used by `serve --http`, `status`, and `doctor` |
| `SPINDLE_HOST` | `127.0.0.1` | Host used by the same |
| `SPINDLE_MAX_CONCURRENT` | `15` | Maximum concurrent spools |
| `SPINDLE_UNSPOOL_MAX_CHARS` | `50000` | Results longer than this are truncated to head+tail by `unspool()` |
| `SPINDLE_UNSPOOL_HEAD_CHARS` | `12000` | Chars kept from the start of a truncated result |
| `SPINDLE_UNSPOOL_TAIL_CHARS` | `12000` | Chars kept from the end of a truncated result |

Storage location: `~/.spindle/spools/`, or `$SPINDLE_HOME/spools/` when set.
`spindle doctor` reports the store it resolved and whether it is writable.

## How It Works

1. **spin()** spawns a detached CLI process (claude, codex, gemini, or kimi-cli) with the given prompt
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
