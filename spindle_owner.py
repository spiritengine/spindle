#!/usr/bin/env python3
"""Import-safe entry point for the packaged per-spool logical owner."""

import importlib
import os

# Importing a spindle submodule executes spindle.__init__.  The logical owner
# must never run store-wide launcher maintenance before receiving its explicit
# store path.
os.environ.setdefault("_SPINDLE_STORE_SUPERVISOR", "1")

main = importlib.import_module("spindle.owner_watchdog").main


if __name__ == "__main__":
    raise SystemExit(main())
