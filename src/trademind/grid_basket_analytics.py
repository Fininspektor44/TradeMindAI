"""Read-only analytics for grid and averaging baskets.

The module consumes one immutable CSV row per basket leg and produces basket,
leg-risk, symbol and portfolio-overlap reports. It never imports a broker API,
never sends orders and never mutates the source CSV.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "1.15.0"
SOURCE_ID = "MT5_GRID_BASKET_ANALYTICS"

REQUIRED_FIELDS = (
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "leg_no",
    "opened_at",
    "price",
    "volume",
)

BASKET_FIELDS = (
    "schema_version",
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "opened_at",
    "closed_at",
    "completed",
    "max_legs",
    "first_entry_price",
    "last_entry_price",
    "weighted_average_entry",
    "total_volume",
    "gross_profit",
    "commission",
    "swap",
    "net_profit",
    "outcome",
    "exit_reason",
    "max_drawdown_money",
    "max_drawdown_pct",
    "max_adverse_points",
    "duration_minutes",
)

LEG_RISK_FIELDS = (
    "schema_version",
    "leg_no",
    "baskets_reaching_leg",
    "completed_baskets",
    "next_leg_count",
    "next_leg_rate",
    "closed_at_leg_count",
    "closed_at_leg_rate",
    "wins",
    "losses",
    "stop_exits",
    "stop_exit_rate",
    "average_net_profit",
    "median_net_profit",
    "average_max_drawdown_money",
    "worst_max_drawdown_money",
    "average_max_drawdown_pct",
    "worst_max_drawdown_pct",
    "median_duration_minutes",
)

SYMBOL_FIELDS = (
    "schema_version",
    "robot",
    "magic",
    "symbol",
    "side",
    "baskets",
    "completed",
    "wins",
    "losses",
    "win_rate",
    "net_profit",
    "average_net_profit",
    "profit_factor",
    "average_max_legs",
    "max_legs",
    "average_max_drawdown_money",
    "worst_max_drawdown_money",
    "average_max_drawdown_pct",
    "worst_max_drawdown_pct",
    "median_duration_minutes",
)

OVERLAP_FIELDS = (
    "schema_version",
    "symbol_a",
    "side_a",
    "symbol_b",
    "side_b",
    "overlapping_pairs",
    "overlap_minutes",
    "both_loss_count",
    "combined_net_profit",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _last_nonblank(rows: Sequence[dict[str, Any]], field: str, default: Any = "") -> Any:
    for row in reversed(rows):
        value = row.get(field)
        if _text(value):
            return value
    return default


def _mean(values: Iterable[float]) -> float:
    sample = list(values)
    return statistics.fmean(sample) if sample else 0.0


def _median(values: Iterable[float]) -> float:
    sample = list(values)
    return statistics.median(sample) if sample else 0.0


def _profit_factor(values: Sequence[float]) -> float:
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    if gross_loss <= 0:
        return 999.0 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _atomic_csv(path: Path, fields: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _load_legs(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Grid basket leg file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        missing = [field for field in REQUIRED_FIELDS if field not in fields]
        if missing:
            raise ValueError(f"Grid basket leg file misses fields: {', '.join(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"Grid basket leg file is empty: {path}")

    seen: set[tuple[str, int]] = set()
    for row in rows:
        basket_id = _text(row.get("basket_id"))
        leg_no = _int(row.get("leg_no"))
        side = _text(row.get("side")).upper()
        if not basket_id:
            raise ValueError("basket_id must not be blank")
        if leg_no < 1:
            raise ValueError(f"Invalid leg_no for basket {basket_id}: {leg_no}")
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid side for basket {basket_id}: {side}")
        if _float(row.get("price")) <= 0 or _float(row.get("volume")) <= 0:
            raise ValueError(f"Invalid price or volume for basket {basket_id}, leg {leg_no}")
        _parse_time(row.get("opened_at"))
        if _text(row.get("closed_at")):
            _parse_time(row.get("closed_at"))
        key = (basket_id, leg_no)
        if key in seen:
            raise ValueError(f"Duplicate basket leg: {basket_id}, leg {leg_no}")
        seen.add(key)
        row["side"] = side
    return rows


def _build_baskets(legs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in legs:
        grouped[_text(row.get("basket_id"))].append(row)

    baskets: list[dict[str, Any]] = []
    for basket_id, group in grouped.items():
        ordered = sorted(
            group,
            key=lambda row: (_int(row.get("leg_no")), _parse_time(row.get("opened_at")) or _now()),
        )
        first = ordered[0]
        identity = (
            _text(first.get("robot")),
            _text(first.get("magic")),
            _text(first.get("symbol")),
            _text(first.get("side")).upper(),
        )
        for row in ordered[1:]:
            row_identity = (
                _text(row.get("robot")),
                _text(row.get("magic")),
                _text(row.get("symbol")),
                _text(row.get("side")).upper(),
            )
            if row_identity != identity:
                raise ValueError(f"Basket identity changes inside {basket_id}")

        opened_at = min(_parse_time(row.get("opened_at")) for row in ordered)
        closed_candidates = [
            _parse_time(row.get("closed_at"))
            for row in ordered
            if _text(row.get("closed_at"))
        ]
        closed_at = max(closed_candidates) if closed_candidates else None
        completed = closed_at is not None
        total_volume = sum(_float(row.get("volume")) for row in ordered)
        weighted_average = (
            sum(_float(row.get("price")) * _float(row.get("volume")) for row in ordered)
            / total_volume
        )
        gross_profit = _float(_last_nonblank(ordered, "gross_profit"))
        commission = _float(_last_nonblank(ordered, "commission"))
        swap = _float(_last_nonblank(ordered, "swap"))
        explicit_net = _last_nonblank(ordered, "net_profit")
        net_profit = _float(explicit_net) if _text(explicit_net) else gross_profit + commission + swap
        if not completed:
            outcome = "OPEN"
        elif net_profit > 1e-9:
            outcome = "WIN"
        elif net_profit < -1e-9:
            outcome = "LOSS"
        else:
            outcome = "FLAT"
        duration = (
            max(0.0, (closed_at - opened_at).total_seconds() / 60.0)
            if opened_at and closed_at
            else 0.0
        )
        baskets.append(
            {
                "schema_version": SCHEMA_VERSION,
                "basket_id": basket_id,
                "robot": identity[0],
                "magic": identity[1],
                "symbol": identity[2],
                "side": identity[3],
                "opened_at": _iso(opened_at),
                "closed_at": _iso(closed_at),
                "completed": int(completed),
                "max_legs": max(_int(row.get("leg_no")) for row in ordered),
                "first_entry_price": _float(ordered[0].get("price")),
                "last_entry_price": _float(ordered[-1].get("price")),
                "weighted_average_entry": round(weighted_average, 10),
                "total_volume": round(total_volume, 6),
                "gross_profit": round(gross_profit, 6),
                "commission": round(commission, 6),
                "swap": round(swap, 6),
                "net_profit": round(net_profit, 6),
                "outcome": outcome,
                "exit_reason": _text(_last_nonblank(ordered, "exit_reason")),
                "max_drawdown_money": max(
                    (_float(row.get("max_drawdown_money")) for row in ordered), default=0.0
                ),
                "max_drawdown_pct": max(
                    (_float(row.get("max_drawdown_pct")) for row in ordered), default=0.0
                ),
                "max_adverse_points": max(
                    (_float(row.get("max_adverse_points")) for row in ordered), default=0.0
                ),
                "duration_minutes": round(duration, 3),
            }
        )
    return sorted(baskets, key=lambda row: (row["opened_at"], row["basket_id"]))


def _leg_risk_rows(baskets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    max_leg = max((_int(row.get("max_legs")) for row in baskets), default=0)
    rows: list[dict[str, Any]] = []
    for leg_no in range(1, max_leg + 1):
        reached = [row for row in baskets if _int(row.get("max_legs")) >= leg_no]
        completed = [row for row in reached if _int(row.get("completed")) == 1]
        next_leg = [row for row in reached if _int(row.get("max_legs")) > leg_no]
        closed_at_leg = [row for row in completed if _int(row.get("max_legs")) == leg_no]
        net = [_float(row.get("net_profit")) for row in completed]
        dd_money = [_float(row.get("max_drawdown_money")) for row in reached]
        dd_pct = [_float(row.get("max_drawdown_pct")) for row in reached]
        durations = [_float(row.get("duration_minutes")) for row in completed]
        stop_exits = [
            row
            for row in completed
            if any(token in _text(row.get("exit_reason")).upper() for token in ("STOP", "SL"))
        ]
        denominator = len(reached)
        completed_count = len(completed)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "leg_no": leg_no,
                "baskets_reaching_leg": denominator,
                "completed_baskets": completed_count,
                "next_leg_count": len(next_leg),
                "next_leg_rate": round(len(next_leg) / denominator, 6) if denominator else 0.0,
                "closed_at_leg_count": len(closed_at_leg),
                "closed_at_leg_rate": (
                    round(len(closed_at_leg) / completed_count, 6) if completed_count else 0.0
                ),
                "wins": sum(value > 1e-9 for value in net),
                "losses": sum(value < -1e-9 for value in net),
                "stop_exits": len(stop_exits),
                "stop_exit_rate": (
                    round(len(stop_exits) / completed_count, 6) if completed_count else 0.0
                ),
                "average_net_profit": round(_mean(net), 6),
                "median_net_profit": round(_median(net), 6),
                "average_max_drawdown_money": round(_mean(dd_money), 6),
                "worst_max_drawdown_money": round(max(dd_money, default=0.0), 6),
                "average_max_drawdown_pct": round(_mean(dd_pct), 6),
                "worst_max_drawdown_pct": round(max(dd_pct, default=0.0), 6),
                "median_duration_minutes": round(_median(durations), 3),
            }
        )
    return rows


def _symbol_rows(baskets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in baskets:
        grouped[(row["robot"], row["magic"], row["symbol"], row["side"])].append(row)
    result: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        completed = [row for row in group if _int(row.get("completed")) == 1]
        net = [_float(row.get("net_profit")) for row in completed]
        legs = [_int(row.get("max_legs")) for row in group]
        dd_money = [_float(row.get("max_drawdown_money")) for row in group]
        dd_pct = [_float(row.get("max_drawdown_pct")) for row in group]
        durations = [_float(row.get("duration_minutes")) for row in completed]
        wins = sum(value > 1e-9 for value in net)
        losses = sum(value < -1e-9 for value in net)
        result.append(
            {
                "schema_version": SCHEMA_VERSION,
                "robot": key[0],
                "magic": key[1],
                "symbol": key[2],
                "side": key[3],
                "baskets": len(group),
                "completed": len(completed),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / len(completed), 6) if completed else 0.0,
                "net_profit": round(sum(net), 6),
                "average_net_profit": round(_mean(net), 6),
                "profit_factor": round(_profit_factor(net), 6),
                "average_max_legs": round(_mean(legs), 3),
                "max_legs": max(legs, default=0),
                "average_max_drawdown_money": round(_mean(dd_money), 6),
                "worst_max_drawdown_money": round(max(dd_money, default=0.0), 6),
                "average_max_drawdown_pct": round(_mean(dd_pct), 6),
                "worst_max_drawdown_pct": round(max(dd_pct, default=0.0), 6),
                "median_duration_minutes": round(_median(durations), 3),
            }
        )
    return result


def _overlap_rows(baskets: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    completed = [
        row
        for row in baskets
        if _int(row.get("completed")) == 1
        and _parse_time(row.get("opened_at"))
        and _parse_time(row.get("closed_at"))
    ]
    aggregates: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(
        lambda: {"pairs": 0.0, "minutes": 0.0, "both_loss": 0.0, "net": 0.0}
    )
    boundaries: list[tuple[datetime, int]] = []
    for row in completed:
        boundaries.append((_parse_time(row["opened_at"]), 1))  # type: ignore[arg-type]
        boundaries.append((_parse_time(row["closed_at"]), -1))  # type: ignore[arg-type]
    boundaries.sort(key=lambda item: (item[0], item[1]))
    active = peak = 0
    for _, delta in boundaries:
        active += delta
        peak = max(peak, active)

    for index, first in enumerate(completed):
        first_open = _parse_time(first["opened_at"])
        first_close = _parse_time(first["closed_at"])
        for second in completed[index + 1 :]:
            second_open = _parse_time(second["opened_at"])
            second_close = _parse_time(second["closed_at"])
            if not first_open or not first_close or not second_open or not second_close:
                continue
            start = max(first_open, second_open)
            end = min(first_close, second_close)
            if end <= start:
                continue
            left = (first["symbol"], first["side"])
            right = (second["symbol"], second["side"])
            if right < left:
                left, right = right, left
            key = (left[0], left[1], right[0], right[1])
            item = aggregates[key]
            item["pairs"] += 1
            item["minutes"] += (end - start).total_seconds() / 60.0
            item["both_loss"] += int(
                _float(first.get("net_profit")) < 0 and _float(second.get("net_profit")) < 0
            )
            item["net"] += _float(first.get("net_profit")) + _float(second.get("net_profit"))

    rows = [
        {
            "schema_version": SCHEMA_VERSION,
            "symbol_a": key[0],
            "side_a": key[1],
            "symbol_b": key[2],
            "side_b": key[3],
            "overlapping_pairs": int(value["pairs"]),
            "overlap_minutes": round(value["minutes"], 3),
            "both_loss_count": int(value["both_loss"]),
            "combined_net_profit": round(value["net"], 6),
        }
        for key, value in aggregates.items()
    ]
    rows.sort(key=lambda row: (-_float(row["overlap_minutes"]), row["symbol_a"], row["symbol_b"]))
    return rows, peak


def _render_dashboard(
    status: dict[str, Any],
    leg_rows: Sequence[dict[str, Any]],
    symbol_rows: Sequence[dict[str, Any]],
) -> str:
    leg_html = "".join(
        "<tr>"
        f"<td>{row['leg_no']}</td><td>{row['baskets_reaching_leg']}</td>"
        f"<td>{100 * _float(row['next_leg_rate']):.1f}%</td>"
        f"<td>{100 * _float(row['stop_exit_rate']):.1f}%</td>"
        f"<td>{_float(row['average_max_drawdown_money']):.2f}</td>"
        f"<td>{_float(row['worst_max_drawdown_money']):.2f}</td>"
        f"<td>{_float(row['average_net_profit']):.2f}</td>"
        "</tr>"
        for row in leg_rows
    )
    symbol_html = "".join(
        "<tr>"
        f"<td>{html.escape(_text(row['robot']))}</td>"
        f"<td>{html.escape(_text(row['symbol']))} {html.escape(_text(row['side']))}</td>"
        f"<td>{row['completed']}</td><td>{100 * _float(row['win_rate']):.1f}%</td>"
        f"<td>{_float(row['net_profit']):.2f}</td><td>{_float(row['profit_factor']):.2f}</td>"
        f"<td>{_float(row['worst_max_drawdown_money']):.2f}</td><td>{row['max_legs']}</td>"
        "</tr>"
        for row in sorted(symbol_rows, key=lambda item: _float(item["net_profit"]), reverse=True)
    )
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>TradeMind Grid Basket Analytics v1.15</title><style>
body{{background:#07131e;color:#e8f5ff;font-family:Arial;margin:28px}}h1{{font-size:38px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
.card{{background:#0c293d;border:1px solid #1d5878;border-radius:15px;padding:18px}}
.card b{{font-size:30px}}table{{width:100%;border-collapse:collapse;margin:18px 0 34px}}
th,td{{padding:9px;border-bottom:1px solid #183f56;text-align:left}}.ok{{color:#31e4a5}}.warn{{color:#ffd36d}}
</style></head><body><h1>Grid Basket Analytics v1.15</h1>
<p class='ok'>Только чтение. Ордера выключены. Исходный CSV не изменяется.</p>
<p class='warn'>Это аудит риска сетки, а не генератор торговых сигналов.</p>
<section class='cards'>
<div class='card'><span>Корзин</span><br><b>{status['baskets']}</b></div>
<div class='card'><span>Закрыто</span><br><b>{status['completed_baskets']}</b></div>
<div class='card'><span>Чистый результат</span><br><b>{status['net_profit']:.2f}</b></div>
<div class='card'><span>Худшая DD</span><br><b>{status['worst_drawdown_money']:.2f}</b></div>
<div class='card'><span>Макс. одновременно</span><br><b>{status['max_concurrent_baskets']}</b></div>
</section>
<h2>Риск по коленям</h2><table><thead><tr><th>Колено</th><th>Дошли</th><th>Пошли дальше</th><th>Стоп</th><th>Средняя DD</th><th>Худшая DD</th><th>Средний net</th></tr></thead><tbody>{leg_html}</tbody></table>
<h2>Роботы и пары</h2><table><thead><tr><th>Робот</th><th>Инструмент</th><th>N</th><th>Win rate</th><th>Net</th><th>PF</th><th>Худшая DD</th><th>Макс. колен</th></tr></thead><tbody>{symbol_html}</tbody></table>
</body></html>"""


@dataclass(frozen=True, slots=True)
class GridAnalyticsSummary:
    source_rows: int
    baskets: int
    completed_baskets: int
    output_dir: Path
    status: dict[str, Any]


def run_grid_analytics(
    legs_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> GridAnalyticsSummary:
    captured_at = now or _now()
    legs = _load_legs(legs_path)
    baskets = _build_baskets(legs)
    leg_rows = _leg_risk_rows(baskets)
    symbol_rows = _symbol_rows(baskets)
    overlap_rows, peak_concurrency = _overlap_rows(baskets)
    completed = [row for row in baskets if _int(row.get("completed")) == 1]
    net = [_float(row.get("net_profit")) for row in completed]
    dashboard = output_dir / "dashboard" / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "source_id": SOURCE_ID,
        "source_path": str(legs_path),
        "source_rows": len(legs),
        "baskets": len(baskets),
        "completed_baskets": len(completed),
        "open_baskets": len(baskets) - len(completed),
        "wins": sum(value > 1e-9 for value in net),
        "losses": sum(value < -1e-9 for value in net),
        "net_profit": sum(net),
        "profit_factor": _profit_factor(net),
        "worst_drawdown_money": max(
            (_float(row.get("max_drawdown_money")) for row in baskets), default=0.0
        ),
        "worst_drawdown_pct": max(
            (_float(row.get("max_drawdown_pct")) for row in baskets), default=0.0
        ),
        "max_legs": max((_int(row.get("max_legs")) for row in baskets), default=0),
        "max_concurrent_baskets": peak_concurrency,
        "orders_enabled": False,
        "logic_changed": False,
        "source_modified": False,
        "signal_generation_enabled": False,
        "dashboard": str(dashboard),
        "output_dir": str(output_dir),
    }
    _atomic_csv(output_dir / "basket_history.csv", BASKET_FIELDS, baskets)
    _atomic_csv(output_dir / "risk_by_leg.csv", LEG_RISK_FIELDS, leg_rows)
    _atomic_csv(output_dir / "symbol_report.csv", SYMBOL_FIELDS, symbol_rows)
    _atomic_csv(output_dir / "portfolio_overlap.csv", OVERLAP_FIELDS, overlap_rows)
    _atomic_json(output_dir / "status.json", status)
    _atomic_text(dashboard, _render_dashboard(status, leg_rows, symbol_rows))
    return GridAnalyticsSummary(
        source_rows=len(legs),
        baskets=len(baskets),
        completed_baskets=len(completed),
        output_dir=output_dir,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.15 read-only grid basket analytics")
    parser.add_argument(
        "--legs",
        type=Path,
        default=Path("data/grid_basket_v1_15/basket_legs.csv"),
        help="One immutable CSV row per basket leg",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/grid_basket_analytics_v1_15"),
    )
    parser.add_argument("--open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run_grid_analytics(
            args.legs.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Grid basket analytics failed: {exc}")
        return 1

    status = summary.status
    print("TradeMind v1.15 Grid Basket Analytics")
    print("Read-only audit. No signals. No orders. Source CSV unchanged.")
    print(f"Source rows: {summary.source_rows}")
    print(f"Baskets: {summary.baskets}, completed: {summary.completed_baskets}")
    print(f"Net: {status['net_profit']:.2f}, PF: {status['profit_factor']:.3f}")
    print(
        f"Worst DD: {status['worst_drawdown_money']:.2f} / "
        f"{status['worst_drawdown_pct']:.2f}%"
    )
    print(f"Max legs: {status['max_legs']}")
    print(f"Max concurrent baskets: {status['max_concurrent_baskets']}")
    print(f"Output: {summary.output_dir}")
    if args.open_dashboard:
        os.startfile(summary.output_dir / "dashboard" / "index.html")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
