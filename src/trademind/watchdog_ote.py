"""TradeMind v1.5.1 watchdog extension for the SMC OTE shadow pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from trademind.watchdog import (
    DEFAULT_SYMBOLS,
    WatchdogCheck,
    WatchdogSnapshot,
    _atomic_json,
    _atomic_text,
    _file_check,
    _now_utc,
    _overall,
    _previous_status,
    _render_html,
    _render_text,
    inspect_source_streams,
    inspect_task_snapshot,
    inspect_validation_states,
)

SCHEMA_VERSION = "1.5.1"


def inspect_ote_states(path: Path) -> tuple[WatchdogCheck, dict[str, int]]:
    """Validate the OTE comparison-state CSV and count its research statuses."""
    counts: dict[str, int] = {}
    if not path.is_file():
        return WatchdogCheck("SMC OTE comparison states", "ERROR", f"missing file: {path}"), counts

    rows = 0
    malformed = 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                status = str(row.get("status") or "").strip().upper()
                if not status:
                    malformed += 1
                    continue
                counts[status] = counts.get(status, 0) + 1
    except OSError as exc:
        return WatchdogCheck("SMC OTE comparison states", "ERROR", f"invalid CSV: {exc}"), counts

    if rows == 0:
        return WatchdogCheck("SMC OTE comparison states", "ERROR", "no comparison rows"), counts
    if malformed:
        return (
            WatchdogCheck(
                "SMC OTE comparison states",
                "ERROR",
                f"{malformed} rows without status",
                details={"rows": rows, "counts": counts},
            ),
            counts,
        )
    return (
        WatchdogCheck(
            "SMC OTE comparison states",
            "OK",
            f"{rows} comparison rows",
            details={"rows": rows, "counts": counts},
        ),
        counts,
    )


def run_watchdog_ote(
    *,
    source_dir: Path,
    volume_path: Path,
    observations_path: Path,
    states_path: Path,
    dashboard_path: Path,
    ote_signals_path: Path,
    ote_states_path: Path,
    ote_dashboard_path: Path,
    task_snapshot_path: Path,
    status_path: Path,
    report_path: Path,
    html_path: Path,
    expected_symbols: Iterable[str] = DEFAULT_SYMBOLS,
    source_max_age_minutes: int = 20,
    derived_max_age_minutes: int = 20,
    now: datetime | None = None,
) -> WatchdogSnapshot:
    """Check the base pipeline plus v1.5 SMC OTE files and scheduled task."""
    report_time = _now_utc(now)
    previous = _previous_status(status_path)
    checks: list[WatchdogCheck] = [
        inspect_source_streams(
            source_dir,
            expected_symbols,
            maximum_age=source_max_age_minutes,
            now=report_time,
        ),
        _file_check("Canonical volume archive", volume_path, derived_max_age_minutes, report_time),
        _file_check("FX observations", observations_path, derived_max_age_minutes, report_time),
        _file_check("FX latest states", states_path, derived_max_age_minutes, report_time),
        _file_check("FX dashboard", dashboard_path, derived_max_age_minutes, report_time),
        _file_check("SMC OTE signals", ote_signals_path, derived_max_age_minutes, report_time),
        _file_check("SMC OTE latest states", ote_states_path, derived_max_age_minutes, report_time),
        _file_check("SMC OTE dashboard", ote_dashboard_path, derived_max_age_minutes, report_time),
    ]
    checks.extend(inspect_task_snapshot(task_snapshot_path))

    fx_check, fx_counts = inspect_validation_states(states_path)
    ote_check, ote_counts = inspect_ote_states(ote_states_path)
    checks.extend((fx_check, ote_check))

    counts = {f"FX_{name}": value for name, value in fx_counts.items()}
    counts.update({f"OTE_{name}": value for name, value in ote_counts.items()})
    overall = _overall(checks)
    notify_required = overall == "ERROR" and previous != "ERROR"
    snapshot = WatchdogSnapshot(
        schema_version=SCHEMA_VERSION,
        generated_at=report_time.isoformat(),
        overall_status=overall,
        notify_required=notify_required,
        checks=tuple(checks),
        validation_counts=counts,
        paths={
            "source_dir": str(source_dir),
            "volume": str(volume_path),
            "observations": str(observations_path),
            "states": str(states_path),
            "dashboard": str(dashboard_path),
            "ote_signals": str(ote_signals_path),
            "ote_states": str(ote_states_path),
            "ote_dashboard": str(ote_dashboard_path),
        },
    )
    _atomic_json(status_path, asdict(snapshot))
    _atomic_text(
        report_path,
        _render_text(snapshot).replace(
            "TradeMind AI v1.4.4 Watchdog", "TradeMind AI v1.5.1 Watchdog"
        ),
    )
    _atomic_text(html_path, _render_html(snapshot))
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check TradeMind base research and v1.5 SMC OTE shadow pipeline"
    )
    appdata = Path(os.getenv("APPDATA", ""))
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=appdata
        / "MetaQuotes"
        / "Terminal"
        / "Common"
        / "Files"
        / "TradeMindAI_Volume_v1_4",
    )
    parser.add_argument("--volume", type=Path, default=Path("data/volume_v1_4/volume_bars.csv"))
    parser.add_argument(
        "--observations", type=Path, default=Path("data/fx_research_v1_4_2/observations.csv")
    )
    parser.add_argument(
        "--states", type=Path, default=Path("data/fx_research_v1_4_2/latest.csv")
    )
    parser.add_argument(
        "--dashboard", type=Path, default=Path("data/fx_research_v1_4_2/dashboard/index.html")
    )
    parser.add_argument(
        "--ote-signals", type=Path, default=Path("data/smc_ote_v1_5/signals.csv")
    )
    parser.add_argument(
        "--ote-states", type=Path, default=Path("data/smc_ote_v1_5/latest.csv")
    )
    parser.add_argument(
        "--ote-dashboard", type=Path, default=Path("data/smc_ote_v1_5/dashboard/index.html")
    )
    parser.add_argument(
        "--task-snapshot", type=Path, default=Path("data/watchdog_v1_4_4/tasks.json")
    )
    parser.add_argument("--status", type=Path, default=Path("data/watchdog_v1_4_4/status.json"))
    parser.add_argument("--report", type=Path, default=Path("data/watchdog_v1_4_4/report.txt"))
    parser.add_argument("--html", type=Path, default=Path("data/watchdog_v1_4_4/index.html"))
    parser.add_argument("--source-max-age-minutes", type=int, default=20)
    parser.add_argument("--derived-max-age-minutes", type=int, default=20)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()

    if args.source_max_age_minutes < 1 or args.derived_max_age_minutes < 1:
        parser.error("maximum ages must be positive")
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols:
        parser.error("--symbols must contain at least one symbol")

    try:
        snapshot = run_watchdog_ote(
            source_dir=args.source_dir.expanduser().resolve(),
            volume_path=args.volume.expanduser().resolve(),
            observations_path=args.observations.expanduser().resolve(),
            states_path=args.states.expanduser().resolve(),
            dashboard_path=args.dashboard.expanduser().resolve(),
            ote_signals_path=args.ote_signals.expanduser().resolve(),
            ote_states_path=args.ote_states.expanduser().resolve(),
            ote_dashboard_path=args.ote_dashboard.expanduser().resolve(),
            task_snapshot_path=args.task_snapshot.expanduser().resolve(),
            status_path=args.status.expanduser().resolve(),
            report_path=args.report.expanduser().resolve(),
            html_path=args.html.expanduser().resolve(),
            expected_symbols=symbols,
            source_max_age_minutes=args.source_max_age_minutes,
            derived_max_age_minutes=args.derived_max_age_minutes,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Watchdog failed: {exc}")
        return 2

    print("TradeMind AI v1.5.1 Watchdog")
    print(f"Overall status: {snapshot.overall_status}")
    for check in snapshot.checks:
        age = f" age={check.age_minutes:.1f}m" if check.age_minutes is not None else ""
        print(f"[{check.status}] {check.name}{age}: {check.message}")
    print(f"Notify required: {snapshot.notify_required}")
    print(f"Status JSON: {args.status.expanduser().resolve()}")
    print(f"Report: {args.report.expanduser().resolve()}")
    print(f"HTML: {args.html.expanduser().resolve()}")
    print("No orders were sent.")
    return 2 if snapshot.overall_status == "ERROR" else 0


if __name__ == "__main__":
    raise SystemExit(main())
