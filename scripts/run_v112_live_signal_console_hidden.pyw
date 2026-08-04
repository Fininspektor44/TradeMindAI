from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _append_if_exists(arguments: list[str], flag: str, path: Path) -> None:
    if path.exists():
        arguments.extend((flag, str(path)))


def _build_server_arguments(
    project_root: Path,
    host: str,
    port: int,
    stale_after_seconds: int,
) -> list[str]:
    arguments = [
        "--host",
        host,
        "--port",
        str(port),
        "--unified-signals",
        str(project_root / "data" / "unified_signal_center_v1_6" / "signals.csv"),
        "--stale-after-seconds",
        str(stale_after_seconds),
    ]

    _append_if_exists(
        arguments,
        "--mt5-status",
        project_root / "data" / "watchdog_v1_10_1" / "status.json",
    )
    _append_if_exists(
        arguments,
        "--bybit-status",
        project_root / "data" / "bybit_shadow_v1_11" / "status.json",
    )
    for relative_path in (
        Path("data/bybit_shadow_v1_11/control/signals.csv"),
        Path("data/bybit_shadow_v1_11/buy_only/signals.csv"),
        Path("data/bybit_shadow_v1_11/strict_sell/signals.csv"),
    ):
        _append_if_exists(arguments, "--bybit-signals", project_root / relative_path)

    return arguments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Windowless launcher for TradeMind v1.12 Live Signal Console"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stale-after-seconds", type=int, default=600)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)

    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "live_signal_console_v1_12.log"

    with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
        sys.stdout = log_file
        sys.stderr = log_file
        print(
            f"[{datetime.now(UTC).isoformat()}] "
            "Starting TradeMind v1.12 Live Signal Console via pythonw.exe"
        )
        print("Mode: read-only, OrdersEnabled=False")
        try:
            from trademind.live_signal_server import main as server_main

            return server_main(
                _build_server_arguments(
                    project_root,
                    args.host,
                    args.port,
                    args.stale_after_seconds,
                )
            )
        except BaseException:
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
