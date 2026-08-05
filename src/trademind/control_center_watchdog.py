"""Read-only watchdog for the TradeMind multi-robot control center."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

WATCHDOG_VERSION = "1.15.3"
WATCHDOG_START = "<!-- TRADEMIND_WATCHDOG_START -->"
WATCHDOG_END = "<!-- TRADEMIND_WATCHDOG_END -->"
ALERT_FIELDS = (
    "detected_at",
    "severity",
    "robot",
    "account_login",
    "code",
    "message",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        text = _text(value).replace(",", ".")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_text(value)))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _atomic_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALERT_FIELDS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _is_completed(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "yes"}


def _alert(
    now: datetime,
    severity: str,
    robot: str,
    account_login: str,
    code: str,
    message: str,
) -> dict[str, str]:
    return {
        "detected_at": now.isoformat(),
        "severity": severity,
        "robot": robot,
        "account_login": account_login,
        "code": code,
        "message": message,
    }


def _snapshot_lookup(rows: Sequence[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        _text(row.get("basket_id")): dict(row)
        for row in rows
        if _text(row.get("basket_id"))
    }


def evaluate_watchdog(
    control_status: dict[str, Any],
    previous_state: dict[str, Any],
    *,
    now: datetime,
    stale_minutes: int = 15,
    leg_warning: int = 6,
    age_warning_hours: int = 72,
    age_critical_hours: int = 168,
) -> tuple[dict[str, Any], dict[str, Any]]:
    active: list[dict[str, str]] = []
    events: list[dict[str, str]] = list(previous_state.get("recent_events", []))[-50:]
    previous_robots = previous_state.get("robots", {})
    next_robots: dict[str, Any] = {}

    for item in control_status.get("robots", []):
        robot = _text(item.get("robot")) or "UNKNOWN"
        account = _text(item.get("account_login"))
        key = f"{robot}|{account}"
        report_dir = Path(_text(item.get("report_dir"))).expanduser()
        snapshot_dir = report_dir / "snapshots"
        snapshot_status = _read_json(snapshot_dir / "status.json")
        history = _read_csv(report_dir / "basket_history.csv")
        snapshots = _read_csv(snapshot_dir / "basket_snapshot_drawdown.csv")
        by_basket = _snapshot_lookup(snapshots)
        open_rows = [row for row in history if not _is_completed(row.get("completed"))]

        latest_at = _parse_time(
            item.get("collector_latest_at")
            or snapshot_status.get("monitoring_latest_at")
        )
        if latest_at is None:
            active.append(
                _alert(
                    now,
                    "critical",
                    robot,
                    account,
                    "MONITOR_TIME_MISSING",
                    "Нет времени последнего снимка. Монитор данных не подтверждён.",
                )
            )
        else:
            stale_for = max(0.0, (now - latest_at).total_seconds() / 60.0)
            if stale_for > stale_minutes:
                active.append(
                    _alert(
                        now,
                        "critical",
                        robot,
                        account,
                        "MONITOR_STALE",
                        f"Монитор не обновлялся {stale_for:.0f} мин. Порог: {stale_minutes} мин.",
                    )
                )

        unmatched = _int(item.get("unmatched_position_snapshot_rows"))
        if unmatched > 0:
            active.append(
                _alert(
                    now,
                    "critical",
                    robot,
                    account,
                    "UNMATCHED_POSITION_SNAPSHOTS",
                    f"Не сопоставлено снимков позиций: {unmatched}.",
                )
            )

        position_rows = _int(item.get("position_snapshot_rows"))
        open_count = _int(item.get("open_baskets"), len(open_rows))
        if open_count > 0 and position_rows == 0:
            active.append(
                _alert(
                    now,
                    "critical",
                    robot,
                    account,
                    "OPEN_BASKETS_WITHOUT_POSITION_SNAPSHOTS",
                    f"Открыто корзин: {open_count}, но снимков позиций нет.",
                )
            )

        for row in open_rows:
            basket_id = _text(row.get("basket_id"))
            symbol = _text(row.get("symbol")) or "?"
            side = _text(row.get("side")).upper() or "?"
            snapshot = by_basket.get(basket_id)
            if snapshot is None:
                active.append(
                    _alert(
                        now,
                        "critical",
                        robot,
                        account,
                        "OPEN_BASKET_SNAPSHOT_MISSING",
                        f"{symbol} {side}: открытая корзина потеряла снимок.",
                    )
                )
                snapshot = {}

            legs = max(_int(row.get("max_legs")), _int(snapshot.get("latest_positions")))
            if legs >= leg_warning:
                severity = "critical" if legs >= leg_warning + 2 else "warning"
                active.append(
                    _alert(
                        now,
                        severity,
                        robot,
                        account,
                        "DANGEROUS_LEG",
                        f"{symbol} {side}: достигнуто колено {legs}. Порог: {leg_warning}.",
                    )
                )

            age_minutes = _float(snapshot.get("basket_age_minutes"))
            if age_minutes <= 0:
                opened_at = _parse_time(row.get("opened_at"))
                if opened_at is not None:
                    age_minutes = max(0.0, (now - opened_at).total_seconds() / 60.0)
            age_hours = age_minutes / 60.0
            if age_hours >= age_critical_hours:
                active.append(
                    _alert(
                        now,
                        "critical",
                        robot,
                        account,
                        "BASKET_STUCK_CRITICAL",
                        f"{symbol} {side}: корзина висит {age_hours:.1f} ч.",
                    )
                )
            elif age_hours >= age_warning_hours:
                active.append(
                    _alert(
                        now,
                        "warning",
                        robot,
                        account,
                        "BASKET_STUCK_WARNING",
                        f"{symbol} {side}: корзина висит {age_hours:.1f} ч.",
                    )
                )

        worst_dd = _float(item.get("worst_account_drawdown_money"))
        previous_worst = _float(previous_robots.get(key, {}).get("worst_account_drawdown_money"), -1.0)
        if previous_worst >= 0 and worst_dd > previous_worst + 0.01:
            events.append(
                _alert(
                    now,
                    "warning",
                    robot,
                    account,
                    "NEW_ACCOUNT_DD_RECORD",
                    f"Новый максимум DD счёта: {worst_dd:.2f} против {previous_worst:.2f}.",
                )
            )
        next_robots[key] = {
            "worst_account_drawdown_money": worst_dd,
            "collector_latest_at": latest_at.isoformat() if latest_at else "",
        }

    events = events[-50:]
    critical = sum(1 for row in active if row["severity"] == "critical")
    warning = sum(1 for row in active if row["severity"] == "warning")
    watch_status = {
        "schema_version": WATCHDOG_VERSION,
        "state": "CRITICAL" if critical else "WARNING" if warning else "OK",
        "updated_at": now.isoformat(),
        "orders_enabled": False,
        "logic_changed": False,
        "source_modified": False,
        "thresholds": {
            "stale_minutes": stale_minutes,
            "leg_warning": leg_warning,
            "age_warning_hours": age_warning_hours,
            "age_critical_hours": age_critical_hours,
        },
        "active_alerts": active,
        "recent_events": events,
        "active_critical": critical,
        "active_warning": warning,
    }
    next_state = {
        "schema_version": WATCHDOG_VERSION,
        "updated_at": now.isoformat(),
        "robots": next_robots,
        "recent_events": events,
    }
    return watch_status, next_state


def _render_alert_rows(rows: Sequence[dict[str, str]]) -> str:
    if not rows:
        return "<div class='watch-ok'>Сторож: активных предупреждений нет.</div>"
    rendered = []
    for row in rows:
        severity = html.escape(row.get("severity", "warning"))
        rendered.append(
            "<div class='watch-alert "
            + severity
            + "'><b>"
            + html.escape(row.get("robot", ""))
            + "</b> · "
            + html.escape(row.get("message", ""))
            + "</div>"
        )
    return "".join(rendered)


def inject_dashboard(dashboard_path: Path, watch_status: dict[str, Any]) -> None:
    source = dashboard_path.read_text(encoding="utf-8")
    source = re.sub(
        re.escape(WATCHDOG_START) + r".*?" + re.escape(WATCHDOG_END),
        "",
        source,
        flags=re.DOTALL,
    )
    if "http-equiv='refresh'" not in source and 'http-equiv="refresh"' not in source:
        source = source.replace(
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
            "<meta http-equiv='refresh' content='300'>",
            1,
        )

    active = watch_status.get("active_alerts", [])
    events = list(watch_status.get("recent_events", []))[-5:]
    state = _text(watch_status.get("state"))
    event_html = ""
    if events:
        event_html = (
            "<details class='watch-events'><summary>Последние события сторожа</summary>"
            + _render_alert_rows(events)
            + "</details>"
        )
    block = f"""
{WATCHDOG_START}
<style>
.watchdog{{margin:18px 0;padding:16px 18px;background:#0b2638;border:1px solid #1e5a78;
border-radius:12px}}.watchdog h2{{margin:0 0 10px}}.watch-ok{{color:#39e4a8}}
.watch-alert{{padding:10px 12px;margin:8px 0;border-radius:8px;background:#243244}}
.watch-alert.warning{{border-left:4px solid #ffd36d;color:#ffe39a}}
.watch-alert.critical{{border-left:4px solid #ff6b6b;color:#ffb3b3}}
.watch-events{{margin-top:12px;color:#b8d0df}}.watch-events summary{{cursor:pointer}}
</style>
<section class='watchdog'>
<h2>Сторож Control Center · {html.escape(state)}</h2>
{_render_alert_rows(active)}
{event_html}
<small>Панель автоматически перезагружается каждые 5 минут.</small>
</section>
{WATCHDOG_END}
"""
    marker = "<div class='robot-grid'>"
    if marker in source:
        source = source.replace(marker, block + marker, 1)
    else:
        source = source.replace("</body>", block + "</body>", 1)
    _atomic_text(dashboard_path, source)


def run_watchdog(
    control_status_path: Path,
    *,
    state_path: Path,
    output_path: Path,
    alerts_csv_path: Path,
    dashboard_path: Path | None = None,
    now: datetime | None = None,
    stale_minutes: int = 15,
    leg_warning: int = 6,
    age_warning_hours: int = 72,
    age_critical_hours: int = 168,
) -> dict[str, Any]:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    control_status = _read_json(control_status_path)
    if not control_status:
        raise ValueError(f"Control center status not found: {control_status_path}")
    previous_state = _read_json(state_path)
    watch_status, next_state = evaluate_watchdog(
        control_status,
        previous_state,
        now=captured_at,
        stale_minutes=stale_minutes,
        leg_warning=leg_warning,
        age_warning_hours=age_warning_hours,
        age_critical_hours=age_critical_hours,
    )
    _atomic_json(output_path, watch_status)
    _atomic_json(state_path, next_state)
    _atomic_csv(alerts_csv_path, watch_status["active_alerts"] + watch_status["recent_events"])
    target_dashboard = dashboard_path
    if target_dashboard is None:
        target_dashboard = Path(_text(control_status.get("dashboard")))
    if target_dashboard and target_dashboard.is_file():
        inject_dashboard(target_dashboard, watch_status)
    return watch_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Control Center read-only watchdog")
    parser.add_argument(
        "--control-status",
        type=Path,
        default=Path("data/control_center_v1_15/status.json"),
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--alerts-csv", type=Path)
    parser.add_argument("--dashboard", type=Path)
    parser.add_argument("--stale-minutes", type=int, default=15)
    parser.add_argument("--leg-warning", type=int, default=6)
    parser.add_argument("--age-warning-hours", type=int, default=72)
    parser.add_argument("--age-critical-hours", type=int, default=168)
    args = parser.parse_args(argv)
    try:
        control_path = args.control_status.expanduser().resolve()
        root = control_path.parent
        watch_status = run_watchdog(
            control_path,
            state_path=(args.state or root / "watch_state.json").expanduser().resolve(),
            output_path=(args.output or root / "watch_status.json").expanduser().resolve(),
            alerts_csv_path=(args.alerts_csv or root / "alerts.csv").expanduser().resolve(),
            dashboard_path=args.dashboard.expanduser().resolve() if args.dashboard else None,
            stale_minutes=max(1, args.stale_minutes),
            leg_warning=max(1, args.leg_warning),
            age_warning_hours=max(1, args.age_warning_hours),
            age_critical_hours=max(args.age_warning_hours, args.age_critical_hours),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"TradeMind watchdog failed: {exc}")
        return 1

    print(f"TradeMind Control Center Watchdog v{WATCHDOG_VERSION}")
    print("Read-only. Orders OFF. Strategy logic unchanged. Source files unchanged.")
    print(
        f"State: {watch_status['state']} | critical={watch_status['active_critical']} "
        f"warning={watch_status['active_warning']}"
    )
    for row in watch_status["active_alerts"]:
        print(f"[{row['severity'].upper()}] {row['robot']}: {row['message']}")
    print(f"Output: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
