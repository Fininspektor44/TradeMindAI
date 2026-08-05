"""Enrich reconstructed MT5 grid baskets with live position and account snapshots.

The module is read-only with respect to MT5 exports and reconstructed legs. It
writes a new enriched leg file plus transparent snapshot diagnostics. It never
imports a broker API and never sends or modifies orders.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = "1.15.1"

LEG_REQUIRED_FIELDS = (
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
POSITION_REQUIRED_FIELDS = (
    "time_msc",
    "symbol",
    "magic",
    "side",
    "volume",
    "profit",
    "swap",
)
ACCOUNT_REQUIRED_FIELDS = (
    "time_msc",
    "balance",
    "equity",
)

SNAPSHOT_FIELDS = (
    "schema_version",
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "max_legs",
    "opened_at",
    "closed_at",
    "snapshot_count",
    "first_snapshot_at",
    "last_snapshot_at",
    "worst_snapshot_at",
    "max_drawdown_money",
    "max_drawdown_pct",
    "latest_floating_money",
    "latest_drawdown_money",
    "latest_drawdown_pct",
    "latest_volume",
    "latest_positions",
    "basket_age_minutes",
    "observed_minutes",
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


def _parse_iso(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _from_millis(value: Any) -> datetime:
    milliseconds = _int(value)
    if milliseconds <= 0:
        raise ValueError(f"Invalid time_msc: {value}")
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _read_csv(
    path: Path,
    required: Sequence[str],
    *,
    allow_empty: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or ())
        missing = [field for field in required if field not in fields]
        if missing:
            raise ValueError(f"{path} misses fields: {', '.join(missing)}")
        rows = [
            {key: _text(value) for key, value in dict(row).items()}
            for row in reader
        ]
    if not rows and not allow_empty:
        raise ValueError(f"CSV file is empty: {path}")
    return fields, rows


def _atomic_csv(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(frozen=True, slots=True)
class BasketWindow:
    basket_id: str
    robot: str
    magic: str
    symbol: str
    side: str
    max_legs: int
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccountPoint:
    captured_at: datetime
    balance: float
    equity: float


@dataclass(frozen=True, slots=True)
class SnapshotEnrichmentSummary:
    output_path: Path
    summary_dir: Path
    status: dict[str, Any]


def _basket_windows(
    leg_rows: Sequence[dict[str, str]],
) -> tuple[list[BasketWindow], dict[str, list[dict[str, str]]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in leg_rows:
        basket_id = _text(row.get("basket_id"))
        if not basket_id:
            raise ValueError("basket_id must not be blank")
        grouped[basket_id].append(row)

    windows: list[BasketWindow] = []
    for basket_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda item: _int(item.get("leg_no")))
        first = ordered[0]
        opened = [_parse_iso(row.get("opened_at")) for row in ordered]
        if any(value is None for value in opened):
            raise ValueError(f"Basket {basket_id} has an invalid opened_at")
        opened_at = min(value for value in opened if value is not None)
        closed = [
            value
            for value in (_parse_iso(row.get("closed_at")) for row in ordered)
            if value is not None
        ]
        windows.append(
            BasketWindow(
                basket_id=basket_id,
                robot=_text(first.get("robot")),
                magic=_text(first.get("magic")),
                symbol=_text(first.get("symbol")),
                side=_text(first.get("side")).upper(),
                max_legs=max((_int(row.get("leg_no")) for row in ordered), default=0),
                opened_at=opened_at,
                closed_at=max(closed) if closed else None,
            )
        )
    windows.sort(key=lambda item: (item.opened_at, item.basket_id))
    return windows, grouped


def _position_identity(row: dict[str, str]) -> tuple[int, str]:
    position_id = _text(row.get("position_id"))
    ticket = _text(row.get("position_ticket"))
    return (_int(row.get("time_msc")), position_id or ticket)


def _deduplicate_position_rows(
    rows: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    deduplicated: dict[tuple[int, str], dict[str, str]] = {}
    for row in rows:
        key = _position_identity(row)
        if key[0] <= 0 or not key[1]:
            raise ValueError("Position snapshot has invalid time or position identity")
        deduplicated[key] = dict(row)
    return sorted(
        deduplicated.values(),
        key=lambda row: (_int(row.get("time_msc")), _position_identity(row)[1]),
    )


def _deduplicate_account_rows(
    rows: Sequence[dict[str, str]],
) -> list[AccountPoint]:
    deduplicated: dict[int, AccountPoint] = {}
    for row in rows:
        captured_at = _from_millis(row.get("time_msc"))
        balance = _float(row.get("balance"))
        equity = _float(row.get("equity"))
        if balance < 0 or equity < 0:
            raise ValueError("Account snapshot has negative balance or equity")
        deduplicated[_int(row.get("time_msc"))] = AccountPoint(
            captured_at=captured_at,
            balance=balance,
            equity=equity,
        )
    return [deduplicated[key] for key in sorted(deduplicated)]


def _account_at(
    captured_at: datetime,
    points: Sequence[AccountPoint],
    timestamps: Sequence[datetime],
) -> AccountPoint | None:
    if not points:
        return None
    index = bisect.bisect_right(timestamps, captured_at) - 1
    if index < 0:
        return None
    return points[index]


def _basket_for_snapshot(
    captured_at: datetime,
    key: tuple[str, str, str],
    by_key: dict[tuple[str, str, str], list[BasketWindow]],
) -> BasketWindow | None:
    candidates = [
        basket
        for basket in by_key.get(key, ())
        if basket.opened_at <= captured_at
        and (basket.closed_at is None or captured_at <= basket.closed_at)
    ]
    return max(candidates, key=lambda item: item.opened_at) if candidates else None


def _merge_optional_max(existing: Any, measured: float) -> float:
    text = _text(existing)
    return max(_float(existing), measured) if text else measured


def _account_metrics(points: Sequence[AccountPoint]) -> dict[str, Any]:
    if not points:
        return {
            "account_snapshot_rows": 0,
            "account_snapshot_started_at": "",
            "account_snapshot_latest_at": "",
            "latest_balance": 0.0,
            "latest_equity": 0.0,
            "latest_account_floating_money": 0.0,
            "latest_account_floating_drawdown_money": 0.0,
            "latest_account_floating_drawdown_pct": 0.0,
            "worst_account_floating_drawdown_money": 0.0,
            "worst_account_floating_drawdown_pct": 0.0,
            "worst_account_peak_equity_drawdown_money": 0.0,
            "worst_account_peak_equity_drawdown_pct": 0.0,
        }

    worst_floating_money = 0.0
    worst_floating_pct = 0.0
    worst_peak_money = 0.0
    worst_peak_pct = 0.0
    peak_equity = 0.0
    for point in points:
        floating_dd = max(0.0, point.balance - point.equity)
        floating_pct = 100.0 * floating_dd / point.balance if point.balance > 0 else 0.0
        worst_floating_money = max(worst_floating_money, floating_dd)
        worst_floating_pct = max(worst_floating_pct, floating_pct)

        peak_equity = max(peak_equity, point.equity)
        peak_dd = max(0.0, peak_equity - point.equity)
        peak_pct = 100.0 * peak_dd / peak_equity if peak_equity > 0 else 0.0
        worst_peak_money = max(worst_peak_money, peak_dd)
        worst_peak_pct = max(worst_peak_pct, peak_pct)

    latest = points[-1]
    latest_floating = latest.equity - latest.balance
    latest_dd = max(0.0, -latest_floating)
    latest_dd_pct = 100.0 * latest_dd / latest.balance if latest.balance > 0 else 0.0
    return {
        "account_snapshot_rows": len(points),
        "account_snapshot_started_at": _iso(points[0].captured_at),
        "account_snapshot_latest_at": _iso(latest.captured_at),
        "latest_balance": round(latest.balance, 6),
        "latest_equity": round(latest.equity, 6),
        "latest_account_floating_money": round(latest_floating, 6),
        "latest_account_floating_drawdown_money": round(latest_dd, 6),
        "latest_account_floating_drawdown_pct": round(latest_dd_pct, 6),
        "worst_account_floating_drawdown_money": round(worst_floating_money, 6),
        "worst_account_floating_drawdown_pct": round(worst_floating_pct, 6),
        "worst_account_peak_equity_drawdown_money": round(worst_peak_money, 6),
        "worst_account_peak_equity_drawdown_pct": round(worst_peak_pct, 6),
    }


def enrich_grid_legs(
    legs_path: Path,
    positions_path: Path,
    output_path: Path,
    summary_dir: Path,
    *,
    account_snapshots_path: Path | None = None,
    now: datetime | None = None,
) -> SnapshotEnrichmentSummary:
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    leg_fields, leg_rows = _read_csv(legs_path, LEG_REQUIRED_FIELDS)
    _, raw_positions = _read_csv(
        positions_path,
        POSITION_REQUIRED_FIELDS,
        allow_empty=True,
    )
    positions = _deduplicate_position_rows(raw_positions)

    account_points: list[AccountPoint] = []
    if account_snapshots_path is not None and account_snapshots_path.is_file():
        _, account_rows = _read_csv(
            account_snapshots_path,
            ACCOUNT_REQUIRED_FIELDS,
            allow_empty=True,
        )
        account_points = _deduplicate_account_rows(account_rows)
    account_times = [point.captured_at for point in account_points]

    windows, grouped_legs = _basket_windows(leg_rows)
    by_key: dict[tuple[str, str, str], list[BasketWindow]] = defaultdict(list)
    for basket in windows:
        by_key[(basket.magic, basket.symbol, basket.side)].append(basket)

    aggregates: dict[tuple[str, datetime], dict[str, Any]] = {}
    matched_rows = 0
    unmatched_rows = 0
    for row in positions:
        captured = _from_millis(row.get("time_msc"))
        key = (
            _text(row.get("magic")),
            _text(row.get("symbol")),
            _text(row.get("side")).upper(),
        )
        basket = _basket_for_snapshot(captured, key, by_key)
        if basket is None:
            unmatched_rows += 1
            continue
        matched_rows += 1
        aggregate_key = (basket.basket_id, captured)
        aggregate = aggregates.setdefault(
            aggregate_key,
            {
                "basket": basket,
                "captured_at": captured,
                "floating_money": 0.0,
                "volume": 0.0,
                "positions": set(),
            },
        )
        aggregate["floating_money"] += _float(row.get("profit")) + _float(row.get("swap"))
        aggregate["volume"] += _float(row.get("volume"))
        aggregate["positions"].add(
            _text(row.get("position_id")) or _text(row.get("position_ticket"))
        )

    by_basket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for aggregate in aggregates.values():
        account = _account_at(
            aggregate["captured_at"],
            account_points,
            account_times,
        )
        floating = float(aggregate["floating_money"])
        drawdown = max(0.0, -floating)
        drawdown_pct: float | str = ""
        if account is not None and account.balance > 0:
            drawdown_pct = 100.0 * drawdown / account.balance
        aggregate["drawdown_money"] = drawdown
        aggregate["drawdown_pct"] = drawdown_pct
        aggregate["position_count"] = len(aggregate["positions"])
        by_basket[aggregate["basket"].basket_id].append(aggregate)

    snapshot_rows: list[dict[str, Any]] = []
    for basket_id, samples in sorted(by_basket.items()):
        ordered = sorted(samples, key=lambda item: item["captured_at"])
        basket: BasketWindow = ordered[0]["basket"]
        worst = max(
            ordered,
            key=lambda item: (
                float(item["drawdown_money"]),
                item["captured_at"],
            ),
        )
        latest = ordered[-1]
        pct_values = [
            float(sample["drawdown_pct"])
            for sample in ordered
            if _text(sample.get("drawdown_pct"))
        ]
        first_at = ordered[0]["captured_at"]
        last_at = latest["captured_at"]
        snapshot_rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "basket_id": basket_id,
                "robot": basket.robot,
                "magic": basket.magic,
                "symbol": basket.symbol,
                "side": basket.side,
                "max_legs": basket.max_legs,
                "opened_at": _iso(basket.opened_at),
                "closed_at": _iso(basket.closed_at),
                "snapshot_count": len(ordered),
                "first_snapshot_at": _iso(first_at),
                "last_snapshot_at": _iso(last_at),
                "worst_snapshot_at": _iso(worst["captured_at"]),
                "max_drawdown_money": round(float(worst["drawdown_money"]), 6),
                "max_drawdown_pct": round(max(pct_values), 6) if pct_values else "",
                "latest_floating_money": round(float(latest["floating_money"]), 6),
                "latest_drawdown_money": round(float(latest["drawdown_money"]), 6),
                "latest_drawdown_pct": (
                    round(float(latest["drawdown_pct"]), 6)
                    if _text(latest.get("drawdown_pct"))
                    else ""
                ),
                "latest_volume": round(float(latest["volume"]), 6),
                "latest_positions": int(latest["position_count"]),
                "basket_age_minutes": round(
                    max(0.0, (last_at - basket.opened_at).total_seconds() / 60.0),
                    3,
                ),
                "observed_minutes": round(
                    max(0.0, (last_at - first_at).total_seconds() / 60.0),
                    3,
                ),
            }
        )

    snapshot_lookup = {row["basket_id"]: row for row in snapshot_rows}
    for basket_id, rows in grouped_legs.items():
        measured = snapshot_lookup.get(basket_id)
        if measured is None:
            continue
        for row in rows:
            row["max_drawdown_money"] = round(
                _merge_optional_max(
                    row.get("max_drawdown_money"),
                    float(measured["max_drawdown_money"]),
                ),
                6,
            )
            if _text(measured.get("max_drawdown_pct")):
                row["max_drawdown_pct"] = round(
                    _merge_optional_max(
                        row.get("max_drawdown_pct"),
                        float(measured["max_drawdown_pct"]),
                    ),
                    6,
                )

    output_fields = list(leg_fields)
    for field in (
        "max_drawdown_money",
        "max_drawdown_pct",
        "max_adverse_points",
    ):
        if field not in output_fields:
            output_fields.append(field)

    enriched_rows = [row for basket in windows for row in grouped_legs[basket.basket_id]]
    enriched_rows.sort(
        key=lambda row: (
            _parse_iso(row.get("opened_at")) or captured_at,
            _text(row.get("basket_id")),
            _int(row.get("leg_no")),
        )
    )
    _atomic_csv(output_path, output_fields, enriched_rows)
    _atomic_csv(
        summary_dir / "basket_snapshot_drawdown.csv",
        SNAPSHOT_FIELDS,
        snapshot_rows,
    )

    position_times = [_from_millis(row.get("time_msc")) for row in positions]
    monitoring_times = [
        *position_times,
        *(point.captured_at for point in account_points),
    ]
    status = {
        "schema_version": SCHEMA_VERSION,
        "state": "OK",
        "updated_at": captured_at.isoformat(),
        "source_legs_path": str(legs_path),
        "position_snapshots_path": str(positions_path),
        "account_snapshots_path": (
            str(account_snapshots_path) if account_snapshots_path is not None else ""
        ),
        "output_path": str(output_path),
        "summary_dir": str(summary_dir),
        "source_modified": False,
        "orders_enabled": False,
        "logic_changed": False,
        "signal_generation_enabled": False,
        "account_scope": "WHOLE_ACCOUNT_UNFILTERED",
        "baskets": len(windows),
        "snapshot_covered_baskets": len(snapshot_rows),
        "snapshot_coverage": len(snapshot_rows) / len(windows) if windows else 0.0,
        "position_snapshot_rows": len(positions),
        "matched_position_snapshot_rows": matched_rows,
        "unmatched_position_snapshot_rows": unmatched_rows,
        "monitoring_started_at": _iso(min(monitoring_times)) if monitoring_times else "",
        "monitoring_latest_at": _iso(max(monitoring_times)) if monitoring_times else "",
        **_account_metrics(account_points),
    }
    _atomic_json(summary_dir / "status.json", status)
    return SnapshotEnrichmentSummary(
        output_path=output_path,
        summary_dir=summary_dir,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enrich TradeMind grid baskets with live MT5 snapshot drawdown"
    )
    parser.add_argument("--legs", type=Path, required=True)
    parser.add_argument("--positions", type=Path, required=True)
    parser.add_argument("--account-snapshots", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = enrich_grid_legs(
            args.legs.expanduser().resolve(),
            args.positions.expanduser().resolve(),
            args.output.expanduser().resolve(),
            args.summary_dir.expanduser().resolve(),
            account_snapshots_path=(
                args.account_snapshots.expanduser().resolve()
                if args.account_snapshots is not None
                else None
            ),
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Grid snapshot drawdown failed: {exc}")
        return 1

    status = summary.status
    print("TradeMind v1.15.1 Grid Snapshot Drawdown")
    print("Read-only. Orders OFF. Source files unchanged.")
    print(
        "Snapshots: "
        f"{status['position_snapshot_rows']} rows, "
        f"{status['snapshot_covered_baskets']}/{status['baskets']} baskets covered"
    )
    print(
        "Matched/unmatched rows: "
        f"{status['matched_position_snapshot_rows']}/"
        f"{status['unmatched_position_snapshot_rows']}"
    )
    print(
        "Account floating DD, latest/worst: "
        f"{status['latest_account_floating_drawdown_money']:.2f}/"
        f"{status['worst_account_floating_drawdown_money']:.2f}"
    )
    print(f"Output: {summary.output_path}")
    print(f"Snapshot report: {summary.summary_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
