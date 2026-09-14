#!/usr/bin/env python3
"""Compatibility entry point for running zpool-stats from a source checkout."""

from zpool_stats import main

if __name__ == "__main__":
    raise SystemExit(main())
