#!/usr/bin/env python3
"""Detached per-store spool supervisor entry point."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Spindle store supervisor")
    parser.add_argument("--store", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    args = parser.parse_args()

    # This must be set before importing the package: ordinary import recovery
    # resolves SPINDLE_HOME and may otherwise touch a different store.
    os.environ["_SPINDLE_STORE_SUPERVISOR"] = "1"
    import spindle

    spindle._run_store_supervisor(args.store, args.lock_fd)


if __name__ == "__main__":
    main()
