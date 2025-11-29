#!/bin/bash
# Spindle wrapper with hot reload support
#
# For stdio MCP, we need stdin/stdout to pass through directly.
# We exec python directly and use a separate watcher process.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPINDLE_PY="$SCRIPT_DIR/spindle.py"
SIGNAL_FILE="$HOME/.spindle/reload_signal"

mkdir -p "$HOME/.spindle"

# Get signal file mtime
get_signal_mtime() {
    if [ -f "$SIGNAL_FILE" ]; then
        stat -c %Y "$SIGNAL_FILE" 2>/dev/null || stat -f %m "$SIGNAL_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

# Clear cache
find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Record initial signal mtime
last_signal_mtime=$(get_signal_mtime)
echo "$last_signal_mtime" > "$HOME/.spindle/last_signal_mtime"

# Start watcher in background - it will kill us when reload needed
(
    while true; do
        sleep 1
        current_mtime=$(get_signal_mtime)
        last_mtime=$(cat "$HOME/.spindle/last_signal_mtime" 2>/dev/null || echo 0)
        if [ "$current_mtime" != "$last_mtime" ] && [ "$current_mtime" != "0" ]; then
            echo "$current_mtime" > "$HOME/.spindle/last_signal_mtime"
            echo "Reload signal detected, killing spindle..." >&2
            # Kill parent (wrapper) - Claude Code will restart us
            kill -TERM $$ 2>/dev/null
            exit 0
        fi
    done
) &

# Exec replaces this process with python - stdin/stdout pass through
exec python "$SPINDLE_PY"
