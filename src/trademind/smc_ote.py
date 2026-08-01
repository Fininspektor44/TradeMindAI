"""CLI and orchestration for TradeMind SMC OTE shadow research."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Iterable

from trademind.ote_engine import build_ote_signals
from trademind.ote_models import DEFAULT_SYMBOLS, HORIZON_BARS, OteSummary, load_volume_rows
from trademind.ote_report import build_states, write_outputs

CRYPTO_SYMBOLS = (
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "LTCUSD",
    "BCHUSD",
    "ADAUSD",
    "DOGEUSD",
)
MONITORED_SYMBOLS = DEFAULT_SYMBOLS + CRYPTO_SYMBOLS


def run_ote_research(
    volume_path: Path,
    signals_path: Path,
    states_path: Path,
    dashboard_path: Path,
    *,
    symbols: Iterable[str] = MONITORED_SYMBOLS,
    server_utc_offset_hours: int = 0,
) -> OteSummary:
    rows, source_rows = load_volume_rows(volume_path, symbols)
    signals = build_ote_signals(rows, server_utc_offset_hours=server_utc_offset_hours)
    states = build_states(signals, datetime.now().astimezone())
    write_outputs(signals_path, states_path, dashboard_path, signals, states)
    completed = {
        horizon: sum(
            row[f"outcome_{horizon.lower()}"] in {"WIN", "LOSS", "TIMEOUT"}
            for row in signals
        )
        for horizon, _bars in HORIZON_BARS
    }
    return OteSummary(
        source_rows=source_rows,
        healthy_rows=len(rows),
        signals=len(signals),
        completed_h3=completed["H3"],
        completed_h6=completed["H6"],
        completed_h12=completed["H12"],
        states=len(states),
        signals_path=signals_path,
        states_path=states_path,
        dashboard_path=dashboard_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only SMC + Fibonacci OTE research")
    parser.add_argument("--volume", type=Path, default=Path("data/volume_v1_4/volume_bars.csv"))
    parser.add_argument("--signals", type=Path, default=Path("data/smc_ote_v1_5/signals.csv"))
    parser.add_argument("--states", type=Path, default=Path("data/smc_ote_v1_5/latest.csv"))
    parser.add_argument(
        "--dashboard", type=Path, default=Path("data/smc_ote_v1_5/dashboard/index.html")
    )
    parser.add_argument("--server-utc-offset-hours", type=int, default=0)
    parser.add_argument("--symbols", default=",".join(MONITORED_SYMBOLS))
    args = parser.parse_args()
    if not -14 <= args.server_utc_offset_hours <= 14:
        parser.error("server UTC offset must be between -14 and 14")
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols:
        parser.error("symbols must not be empty")
    volume = args.volume.expanduser().resolve()
    if not volume.is_file():
        print(f"Canonical volume file not found: {volume}")
        return 1
    try:
        summary = run_ote_research(
            volume,
            args.signals.expanduser().resolve(),
            args.states.expanduser().resolve(),
            args.dashboard.expanduser().resolve(),
            symbols=symbols,
            server_utc_offset_hours=args.server_utc_offset_hours,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"SMC OTE research failed: {exc}")
        return 1
    print("TradeMind SMC + Fibonacci OTE shadow research")
    print(f"Canonical source rows: {summary.source_rows}")
    print(f"Healthy M5 rows: {summary.healthy_rows}")
    print(f"OTE signals: {summary.signals}")
    print(
        f"Completed H3/H6/H12: {summary.completed_h3}/"
        f"{summary.completed_h6}/{summary.completed_h12}"
    )
    print(f"Comparison states: {summary.states}")
    print(f"Signals: {summary.signals_path}")
    print(f"Latest: {summary.states_path}")
    print(f"Dashboard: {summary.dashboard_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
