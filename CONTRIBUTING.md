# Contributing to Spindle

## Development Setup

```bash
# Clone the repo
git clone https://github.com/smythp/spindle.git
cd spindle

# Install with dev dependencies
pip install -e ".[dev]"
```

## Code Style

We use black for formatting and ruff for linting:

```bash
# Format code
black spindle/ tests/

# Lint
ruff check spindle/ tests/

# Fix auto-fixable lint issues
ruff check --fix spindle/ tests/
```

## Running Tests

```bash
pytest
```

## Making Changes

1. Create a branch for your changes
2. Make your changes
3. Run `black` and `ruff check`
4. Run `pytest`
5. Submit a PR

## Code Structure

- `spindle/__init__.py` - Main MCP server implementation
- `tests/` - Test suite

## Key Concepts

- **Spool** - A background task/agent. Has an ID, status, prompt, and result.
- **Shard** - An isolated git worktree for safe parallel work.
- **Permission profile** - Controls what tools a spawned agent can use.
