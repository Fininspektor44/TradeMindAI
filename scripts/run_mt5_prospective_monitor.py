#!/usr/bin/env python3
"""Thin CLI entry point for the MT5 Prospective Monitor V1.

Observational only: reads a signal-journal CSV snapshot read-only, scores
the three already-frozen prospective candidates (see
``trademind.mt5_prospective_monitor``), and prints one JSON status report.
Opens no orders, contacts no broker, makes no model/network call.

Simple command for SER8 (Windows PowerShell, run from the repository root)
against the authoritative live ECN forward source:

    .\\.venv\\Scripts\\python.exe scripts\\run_mt5_prospective_monitor.py `
        --journal "data\\live_signal_runtime_ecn_77053345\\observations.csv"

(POSIX equivalent: .venv/bin/python scripts/run_mt5_prospective_monitor.py
--journal data/live_signal_runtime_ecn_77053345/observations.csv)

``--journal`` accepts any journal- or live-observations-schema CSV snapshot
unchanged -- the live ECN observations.csv already uses the exact column
names the frozen candidates need (see
``trademind.mt5_prospective_monitor.LIVE_OBSERVATIONS_REQUIRED_COLUMNS``),
so no separate flag or format conversion is required. With no ``--journal``
argument it defaults to $TRADEMIND_JOURNAL_DIR/signals.csv, or the legacy
data/journal_ecn/signals.csv if that variable is unset; that default is not
the live ECN source and is unrelated to the command above.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.mt5_prospective_monitor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
