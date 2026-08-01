"""TradeMind v1.7 watchdog for Unified Signal Center and crypto streams."""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from trademind.crypto_watch import inspect_crypto_streams
from trademind.watchdog import (
    DEFAULT_SYMBOLS,
    WatchdogCheck,
    _atomic_json,
    _atomic_text,
    _file_check,
    _overall,
    _previous_status,
    _render_html,
    _render_text,
)
from trademind.watchdog_ote import run_watchdog_ote

SCHEMA_VERSION = "1.7.0"


def inspect_unified_states(path: Path) -> tuple[WatchdogCheck, dict[str, int]]:
    counts: dict[str, int] = {}
    if not path.is_file():
        return WatchdogCheck("Unified comparison states", "ERROR", f"missing file: {path}"), counts
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
        return WatchdogCheck("Unified comparison states", "ERROR", f"invalid CSV: {exc}"), counts
    if rows == 0:
        return WatchdogCheck("Unified comparison states", "ERROR", "no comparison rows"), counts
    if malformed:
        return (
            WatchdogCheck(
                "Unified comparison states",
                "ERROR",
                f"{malformed} rows without status",
                details={"rows": rows, "counts": counts},
            ),
            counts,
        )
    return (
        WatchdogCheck(
            "Unified comparison states",
            "OK",
            f"{rows} comparison rows",
            details={"rows": rows, "counts": counts},
        ),
        counts,
    )


def run_watchdog_unified(
    *,
    source_dir: Path,
    volume_path: Path,
    observations_path: Path,
    states_path: Path,
    dashboard_path: Path,
    ote_signals_path: Path,
    ote_states_path: Path,
    ote_dashboard_path: Path,
    unified_signals_path: Path,
    unified_states_path: Path,
    unified_dashboard_path: Path,
    task_snapshot_path: Path,
    status_path: Path,
    report_path: Path,
    html_path: Path,
    crypto_manifest_path: Path | None = None,
    expected_symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    source_max_age_minutes: int = 20,
    derived_max_age_minutes: int = 20,
):
    previous = _previous_status(status_path)
    snapshot = run_watchdog_ote(
        source_dir=source_dir,
        volume_path=volume_path,
        observations_path=observations_path,
        states_path=states_path,
        dashboard_path=dashboard_path,
        ote_signals_path=ote_signals_path,
        ote_states_path=ote_states_path,
        ote_dashboard_path=ote_dashboard_path,
        task_snapshot_path=task_snapshot_path,
        status_path=status_path,
        report_path=report_path,
        html_path=html_path,
        expected_symbols=expected_symbols,
        source_max_age_minutes=source_max_age_minutes,
        derived_max_age_minutes=derived_max_age_minutes,
    )
    now = datetime.fromisoformat(snapshot.generated_at)
    checks = list(snapshot.checks)
    checks.extend(
        (
            _file_check(
                "Unified signal rows",
                unified_signals_path,
                derived_max_age_minutes,
                now,
            ),
            _file_check(
                "Unified latest states",
                unified_states_path,
                derived_max_age_minutes,
                now,
            ),
            _file_check(
                "Unified dashboard",
                unified_dashboard_path,
                derived_max_age_minutes,
                now,
            ),
        )
    )
    unified_check, unified_counts = inspect_unified_states(unified_states_path)
    checks.append(unified_check)

    manifest = crypto_manifest_path or source_dir / "crypto_manifest.csv"
    checks.append(
        inspect_crypto_streams(
            manifest,
            source_dir,
            maximum_age=source_max_age_minutes,
            now=now,
        )
    )

    counts = dict(snapshot.validation_counts)
    counts.update({f"UNIFIED_{name}": value for name, value in unified_counts.items()})
    overall = _overall(checks)
    notify_required = overall == "ERROR" and previous != "ERROR"
    paths = dict(snapshot.paths)
    paths.update(
        {
            "unified_signals": str(unified_signals_path),
            "unified_states": str(unified_states_path),
            "unified_dashboard": str(unified_dashboard_path),
            "crypto_manifest": str(manifest),
        }
    )
    snapshot = replace(
        snapshot,
        schema_version=SCHEMA_VERSION,
        overall_status=overall,
        notify_required=notify_required,
        checks=tuple(checks),
        validation_counts=counts,
        paths=paths,
    )
    _atomic_json(status_path, asdict(snapshot))
    _atomic_text(
        report_path,
        _render_text(snapshot).replace(
            "TradeMind AI v1.4.4 Watchdog", "TradeMind AI v1.7.0 Watchdog"
        ),
    )
    _atomic_text(html_path, _render_html(snapshot))
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Check TradeMind Unified and crypto pipeline")
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
        "--unified-signals",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/signals.csv"),
    )
    parser.add_argument(
        "--unified-states",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/latest.csv"),
    )
    parser.add_argument(
        "--unified-dashboard",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/dashboard/index.html"),
    )
    parser.add_argument("--crypto-manifest", type=Path, default=None)
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

    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols:
        parser.error("--symbols must contain at least one symbol")
    if args.source_max_age_minutes < 1 or args.derived_max_age_minutes < 1:
        parser.error("maximum ages must be positive")
    source_dir = args.source_dir.expanduser().resolve()
    crypto_manifest = (
        args.crypto_manifest.expanduser().resolve() if args.crypto_manifest is not None else None
    )
    try:
        snapshot = run_watchdog_unified(
            source_dir=source_dir,
            volume_path=args.volume.expanduser().resolve(),
            observations_path=args.observations.expanduser().resolve(),
            states_path=args.states.expanduser().resolve(),
            dashboard_path=args.dashboard.expanduser().resolve(),
            ote_signals_path=args.ote_signals.expanduser().resolve(),
            ote_states_path=args.ote_states.expanduser().resolve(),
            ote_dashboard_path=args.ote_dashboard.expanduser().resolve(),
            unified_signals_path=args.unified_signals.expanduser().resolve(),
            unified_states_path=args.unified_states.expanduser().resolve(),
            unified_dashboard_path=args.unified_dashboard.expanduser().resolve(),
            task_snapshot_path=args.task_snapshot.expanduser().resolve(),
            status_path=args.status.expanduser().resolve(),
            report_path=args.report.expanduser().resolve(),
            html_path=args.html.expanduser().resolve(),
            crypto_manifest_path=crypto_manifest,
            expected_symbols=symbols,
            source_max_age_minutes=args.source_max_age_minutes,
            derived_max_age_minutes=args.derived_max_age_minutes,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Watchdog failed: {exc}")
        return 2

    print("TradeMind AI v1.7.0 Watchdog")
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
