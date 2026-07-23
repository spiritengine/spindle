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

We use ruff for both formatting and linting - the same commands CI runs, so a
clean local run is a clean CI run. (Do not format with black: its output differs
from `ruff format` and CI will reject it.)

```bash
# Format code
ruff format .

# Check formatting without writing (what CI does)
ruff format --check .

# Lint
ruff check .

# Fix auto-fixable lint issues
ruff check --fix .
```

## Running Tests

```bash
pytest
```

## Making Changes

1. Create a branch for your changes
2. Make your changes
3. Run `ruff format` and `ruff check`
4. Run `pytest`
5. Submit a PR

## Code Structure

- `spindle/__init__.py` - Main MCP server implementation
- `tests/` - Test suite

## Key Concepts

- **Spool** - A background task/agent. Has an ID, status, prompt, and result.
- **Shard** - An isolated git worktree for safe parallel work.
- **Permission profile** - Controls what tools a spawned agent can use.
