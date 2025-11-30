# Contributing to Spindle

Thanks for your interest in contributing to Spindle!

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/OWNER/spindle.git
   cd spindle
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. Install in development mode with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

## Requirements

- Python 3.10+
- Claude CLI (`claude`) installed and configured
- Git (for shard/worktree functionality)

## Running Tests

```bash
pytest
```

## Code Style

We use [ruff](https://github.com/astral-sh/ruff) for linting:

```bash
ruff check .
ruff format .  # If you want to auto-format
```

## Making Changes

1. Create a branch for your changes
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## Pull Request Guidelines

- Keep PRs focused on a single change
- Include tests for new functionality
- Update documentation if needed
- Ensure all tests pass

## Architecture Overview

Spindle is an MCP (Model Context Protocol) server that enables Claude Code to spawn child Claude Code agents:

- **Spool**: A background task/agent instance. Has an ID, status, prompt, and result.
- **Shard**: An isolated git worktree for a spool to work in safely.
- **Storage**: Spools persist to `~/.spindle/spools/{id}.json`

Key components:
- `spin()` - Spawn a new agent
- `unspool()` - Get result from an agent
- `shard_*` - Manage isolated git worktrees
- `spool_*` - Search, filter, export spool data

## Questions?

Open an issue for questions or discussion.
