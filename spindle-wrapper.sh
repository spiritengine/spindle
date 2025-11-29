#!/bin/bash
# Spindle wrapper - clears cache and runs spindle
# Hot reload: use spindle_reload() tool which exits the process
# Claude Code will auto-restart, picking up code changes

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Clear Python cache for fresh code
find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Run spindle - exec replaces shell so stdin/stdout pass through
exec python "$SCRIPT_DIR/spindle.py"
