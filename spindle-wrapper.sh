#!/bin/bash
# Spindle wrapper with hot reload support
#
# Usage: ./spindle-wrapper.sh
#
# This wrapper runs spindle.py and watches for reload signals.
# When ~/.spindle/reload_signal is updated, it restarts spindle.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPINDLE_PY="$SCRIPT_DIR/spindle.py"
SIGNAL_FILE="$HOME/.spindle/reload_signal"
PID_FILE="$HOME/.spindle/wrapper.pid"

# Write our PID
mkdir -p "$HOME/.spindle"
echo $$ > "$PID_FILE"

cleanup() {
    if [ -n "$SPINDLE_PID" ] && kill -0 "$SPINDLE_PID" 2>/dev/null; then
        kill "$SPINDLE_PID" 2>/dev/null
        wait "$SPINDLE_PID" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    exit 0
}

trap cleanup EXIT INT TERM

# Get initial signal file mtime (or 0 if doesn't exist)
get_signal_mtime() {
    if [ -f "$SIGNAL_FILE" ]; then
        stat -c %Y "$SIGNAL_FILE" 2>/dev/null || stat -f %m "$SIGNAL_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

last_signal_mtime=$(get_signal_mtime)

while true; do
    # Clear Python cache to ensure fresh code
    find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

    # Start spindle
    python "$SPINDLE_PY" &
    SPINDLE_PID=$!

    # Monitor for reload signal or process exit
    while kill -0 "$SPINDLE_PID" 2>/dev/null; do
        current_mtime=$(get_signal_mtime)
        if [ "$current_mtime" != "$last_signal_mtime" ] && [ "$current_mtime" != "0" ]; then
            last_signal_mtime=$current_mtime
            echo "Reload signal detected, restarting spindle..." >&2
            kill "$SPINDLE_PID" 2>/dev/null
            wait "$SPINDLE_PID" 2>/dev/null
            sleep 0.5
            break
        fi
        sleep 1
    done

    # Small delay before restart
    sleep 0.5
done
