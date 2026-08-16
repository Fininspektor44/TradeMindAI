#!/usr/bin/env python3
"""Thin CLI entry point for the MT5 Prospective Monitor V1.

Observational only: reads a signal-journal CSV snapshot read-only, scores
the three already-frozen prospective candidates (see
``trademind.mt5_prospective_monitor``), and prints one JSON status report.
Opens no orders, contacts no broker, makes no model/network call.

Simple command for SER8 (run from the repository root):

    .venv/bin/python scripts/run_mt5_prospective_monitor.py \\
        --journal data/journal_ecn/signals.csv

With no arguments it defaults to $TRADEMIND_JOURNAL_DIR/signals.csv, or
data/journal_ecn/signals.csv if that variable is unset -- the same default
convention ``trademind-candidate-watch`` already uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.mt5_prospective_monitor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
