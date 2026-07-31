"""TradeMind v1.4.4 read-only watchdog for the 24/7 research pipeline."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.4.4"
SEVERITY = {"OK": 0, "WARN": 1, "ERROR": 2}
RUNNING_TASK_RESULT = 267009  # 0x41301, Task Scheduler: task is currently running.

DEFAULT_SYMBOLS = (
    "XAUUSD",
    "XAGUSD",
    ".USTECHCASH",
    ".US500CASH",
    ".US30CASH",
    "WTI",
    "BRENT",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
)


@dataclass(frozen=True, slots=True)
class WatchdogCheck:
    name: str
    status: str
    message: str
    age_minutes: float | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WatchdogSnapshot:
    schema_version: str
    generated_at: str
    overall_status: str
    notify_required: bool
    checks: tuple[WatchdogCheck, ...]
    validation_counts: dict[str, int]
    paths: dict[str, str]


def _now_utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("watchdog timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _market_closed(now: datetime) -> bool:
    """Treat the normal Friday-night to Sunday-night FX closure as non-fatal."""
    weekday = now.weekday()
    if weekday == 5:
        return True
    if weekday == 4 and now.hour >= 22:
        return True
    return weekday == 6 and now.hour < 22


def _age_minutes(path: Path, now: datetime) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (now - modified).total_seconds() / 60.0)


def _status_for_age(age: float, maximum: int, now: datetime) -> tuple[str, str]:
    if age <= maximum:
        return "OK", "fresh"
    if _market_closed(now):
        return "WARN", f"stale while market is closed: {age:.1f} min"
    return "ERROR", f"stale: {age:.1f} min, limit {maximum} min"


def _file_check(name: str, path: Path, maximum_age: int, now: datetime) -> WatchdogCheck:
    if not path.is_file():
        return WatchdogCheck(name, "ERROR", f"missing file: {path}")
    if path.stat().st_size <= 0:
        return WatchdogCheck(name, "ERROR", f"empty file: {path}")
    age = _age_minutes(path, now)
    status, message = _status_for_age(age, maximum_age, now)
    return WatchdogCheck(
        name,
        status,
        message,
        age_minutes=age,
        details={"path": str(path), "bytes": path.stat().st_size},
    )


def _last_csv_row(path: Path) -> tuple[dict[str, str] | None, int]:
    last: dict[str, str] | None = None
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            last = {key: str(value or "").strip() for key, value in dict(row).items()}
            count += 1
    return last, count


def inspect_source_streams(
    source_dir: Path,
    expected_symbols: Iterable[str],
    *,
    maximum_age: int,
    now: datetime,
) -> WatchdogCheck:
    expected = {symbol.upper() for symbol in expected_symbols}
    if not source_dir.is_dir():
        return WatchdogCheck("MT5 source streams", "ERROR", f"missing directory: {source_dir}")

    latest_by_symbol: dict[str, tuple[Path, dict[str, str], int]] = {}
    invalid_files: list[str] = []
    for path in sorted(source_dir.glob("volume_*_M5.csv")):
        try:
            row, rows = _last_csv_row(path)
            if not row:
                invalid_files.append(f"{path.name}: no rows")
                continue
            symbol = row.get("symbol", "").upper()
            if not symbol:
                invalid_files.append(f"{path.name}: missing symbol")
                continue
            current = latest_by_symbol.get(symbol)
            timestamp = int(row.get("time", "0") or "0")
            current_time = int(current[1].get("time", "0") or "0") if current else -1
            if current is None or timestamp >= current_time:
                latest_by_symbol[symbol] = (path, row, rows)
        except (OSError, TypeError, ValueError) as exc:
            invalid_files.append(f"{path.name}: {exc}")

    found = set(latest_by_symbol)
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    stale: list[str] = []
    bad_status: list[str] = []
    ages: list[float] = []
    total_rows = 0
    for symbol in sorted(expected & found):
        path, row, rows = latest_by_symbol[symbol]
        total_rows += rows
        age = _age_minutes(path, now)
        ages.append(age)
        if age > maximum_age:
            stale.append(f"{symbol}={age:.1f}m")
        if row.get("tick_copy_status", "").upper() != "OK":
            bad_status.append(f"{symbol}={row.get('tick_copy_status', 'missing')}")
        try:
            if int(row.get("tick_count", "0") or "0") <= 0:
                bad_status.append(f"{symbol}=zero_ticks")
        except ValueError:
            bad_status.append(f"{symbol}=invalid_ticks")

    messages: list[str] = []
    status = "OK"
    if missing:
        status = "ERROR"
        messages.append("missing: " + ",".join(missing))
    if invalid_files:
        status = "ERROR"
        messages.append("invalid: " + " | ".join(invalid_files[:5]))
    if bad_status:
        status = "ERROR"
        messages.append("bad stream: " + ",".join(bad_status))
    if stale:
        stale_status = "WARN" if _market_closed(now) else "ERROR"
        if SEVERITY[stale_status] > SEVERITY[status]:
            status = stale_status
        messages.append("stale: " + ",".join(stale))
    if extra:
        messages.append("extra: " + ",".join(extra))
    if not messages:
        messages.append(f"all {len(expected)} streams healthy")

    return WatchdogCheck(
        "MT5 source streams",
        status,
        "; ".join(messages),
        age_minutes=max(ages) if ages else None,
        details={
            "expected": len(expected),
            "found": len(found & expected),
            "source_files": len(latest_by_symbol),
            "rows": total_rows,
        },
    )


def inspect_task_snapshot(path: Path) -> list[WatchdogCheck]:
    if not path.is_file():
        return [WatchdogCheck("Windows tasks", "ERROR", f"missing task snapshot: {path}")]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [WatchdogCheck("Windows tasks", "ERROR", f"invalid task snapshot: {exc}")]
    rows = payload if isinstance(payload, list) else [payload]
    checks: list[WatchdogCheck] = []
    for raw in rows:
        row = dict(raw or {})
        name = str(row.get("name") or "unknown task")
        state = str(row.get("state") or "UNKNOWN").upper()
        exists = bool(row.get("exists", True))
        enabled = bool(row.get("enabled", True))
        try:
            result = int(row.get("last_task_result", -1))
        except (TypeError, ValueError):
            result = -1
        if not exists:
            status, message = "ERROR", "task not found"
        elif not enabled or state == "DISABLED":
            status, message = "ERROR", "task disabled"
        elif result == 0:
            status, message = "OK", f"state={state}; last result=0"
        elif result == RUNNING_TASK_RESULT or state == "RUNNING":
            status, message = "WARN", f"task currently running; result={result}"
        else:
            status, message = "ERROR", f"state={state}; last result={result}"
        checks.append(
            WatchdogCheck(
                f"Task: {name}",
                status,
                message,
                details={
                    "last_run_time": str(row.get("last_run_time") or ""),
                    "next_run_time": str(row.get("next_run_time") or ""),
                },
            )
        )
    return checks or [WatchdogCheck("Windows tasks", "ERROR", "empty task snapshot")]


def inspect_validation_states(path: Path) -> tuple[WatchdogCheck, dict[str, int]]:
    counts: dict[str, int] = {}
    if not path.is_file():
        return WatchdogCheck("FX validation states", "ERROR", f"missing file: {path}"), counts
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
        return WatchdogCheck("FX validation states", "ERROR", f"invalid CSV: {exc}"), counts
    if rows == 0:
        return WatchdogCheck("FX validation states", "ERROR", "no validation rows"), counts
    if malformed:
        return (
            WatchdogCheck(
                "FX validation states",
                "ERROR",
                f"{malformed} rows without status",
                details={"rows": rows, "counts": counts},
            ),
            counts,
        )
    return (
        WatchdogCheck(
            "FX validation states",
            "OK",
            f"{rows} validation rows",
            details={"rows": rows, "counts": counts},
        ),
        counts,
    )


def _overall(checks: Iterable[WatchdogCheck]) -> str:
    severity = max((SEVERITY.get(check.status, 2) for check in checks), default=2)
    return ("OK", "WARN", "ERROR")[severity]


def _previous_status(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(payload.get("overall_status") or "").upper() or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _render_text(snapshot: WatchdogSnapshot) -> str:
    lines = [
        "TradeMind AI v1.4.4 Watchdog",
        f"Generated UTC: {snapshot.generated_at}",
        f"Overall status: {snapshot.overall_status}",
        f"Notify required: {snapshot.notify_required}",
        "",
    ]
    for check in snapshot.checks:
        age = f" age={check.age_minutes:.1f}m" if check.age_minutes is not None else ""
        lines.append(f"[{check.status}] {check.name}{age}: {check.message}")
    lines.extend(["", "Validation counts"])
    if snapshot.validation_counts:
        for key, value in sorted(snapshot.validation_counts.items()):
            lines.append(f"{key}: {value}")
    else:
        lines.append("none")
    return "\n".join(lines) + "\n"


def _render_html(snapshot: WatchdogSnapshot) -> str:
    palette = {"OK": "#35d07f", "WARN": "#f6c85f", "ERROR": "#ff647c"}
    cards = []
    for check in snapshot.checks:
        age = f" · {check.age_minutes:.1f} мин" if check.age_minutes is not None else ""
        cards.append(
            '<article class="card">'
            f'<div class="status {check.status.lower()}">{html.escape(check.status)}</div>'
            f"<h3>{html.escape(check.name)}</h3>"
            f"<p>{html.escape(check.message)}{html.escape(age)}</p>"
            "</article>"
        )
    counts = "".join(
        f"<li><span>{html.escape(name)}</span><b>{value}</b></li>"
        for name, value in sorted(snapshot.validation_counts.items())
    ) or "<li><span>Нет данных</span><b>0</b></li>"
    status_color = palette.get(snapshot.overall_status, palette["ERROR"])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind Watchdog</title><style>
:root{{--bg:#0a1020;--panel:#111a2e;--text:#eef3ff;--muted:#98a5c5;--ok:#35d07f;--warn:#f6c85f;--error:#ff647c}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#07101e,#111a33);color:var(--text);font:16px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1180px;margin:auto;padding:34px 22px 60px}}header{{padding:28px;border:1px solid #263457;border-radius:22px;background:#0d1629cc;box-shadow:0 24px 70px #0006}}
h1{{margin:0 0 8px;font-size:clamp(30px,5vw,56px)}}.pill{{display:inline-block;padding:8px 14px;border-radius:999px;background:{status_color};color:#07101e;font-weight:900}}
.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin-top:22px}}.card{{padding:20px;border-radius:18px;background:var(--panel);border:1px solid #263457}}
.card h3{{margin:10px 0 6px}}.card p{{margin:0;color:var(--muted)}}.status{{font-size:12px;font-weight:900;letter-spacing:.12em}}.status.ok{{color:var(--ok)}}.status.warn{{color:var(--warn)}}.status.error{{color:var(--error)}}
section{{margin-top:30px}}ul{{list-style:none;padding:0;max-width:480px}}li{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #263457}}
</style></head><body><main><header><span class="pill">{html.escape(snapshot.overall_status)}</span><h1>TradeMind Watchdog</h1>
<p class="muted">Контроль 24/7 контура · обновлено {html.escape(snapshot.generated_at)}</p></header>
<div class="grid">{''.join(cards)}</div><section><h2>Статусы исследования</h2><ul>{counts}</ul></section></main></body></html>"""


def run_watchdog(
    *,
    source_dir: Path,
    volume_path: Path,
    observations_path: Path,
    states_path: Path,
    dashboard_path: Path,
    task_snapshot_path: Path,
    status_path: Path,
    report_path: Path,
    html_path: Path,
    expected_symbols: Iterable[str] = DEFAULT_SYMBOLS,
    source_max_age_minutes: int = 20,
    derived_max_age_minutes: int = 20,
    now: datetime | None = None,
) -> WatchdogSnapshot:
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
    ]
    checks.extend(inspect_task_snapshot(task_snapshot_path))
    validation_check, counts = inspect_validation_states(states_path)
    checks.append(validation_check)
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
        },
    )
    payload = asdict(snapshot)
    _atomic_json(status_path, payload)
    _atomic_text(report_path, _render_text(snapshot))
    _atomic_text(html_path, _render_html(snapshot))
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the TradeMind 24/7 research pipeline")
    appdata = Path(os.getenv("APPDATA", ""))
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=appdata / "MetaQuotes" / "Terminal" / "Common" / "Files" / "TradeMindAI_Volume_v1_4",
    )
    parser.add_argument("--volume", type=Path, default=Path("data/volume_v1_4/volume_bars.csv"))
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path("data/fx_research_v1_4_2/observations.csv"),
    )
    parser.add_argument(
        "--states",
        type=Path,
        default=Path("data/fx_research_v1_4_2/latest.csv"),
    )
    parser.add_argument(
        "--dashboard",
        type=Path,
        default=Path("data/fx_research_v1_4_2/dashboard/index.html"),
    )
    parser.add_argument(
        "--task-snapshot",
        type=Path,
        default=Path("data/watchdog_v1_4_4/tasks.json"),
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
        snapshot = run_watchdog(
            source_dir=args.source_dir.expanduser().resolve(),
            volume_path=args.volume.expanduser().resolve(),
            observations_path=args.observations.expanduser().resolve(),
            states_path=args.states.expanduser().resolve(),
            dashboard_path=args.dashboard.expanduser().resolve(),
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
    print("TradeMind AI v1.4.4 Watchdog")
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
