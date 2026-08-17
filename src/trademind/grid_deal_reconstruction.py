"""Reconstruct multi-leg deal baskets from a read-only MT5 deal export.

The obsolete grid-strategy CLI wrapper and reporting pipeline that used to sit
on top of this module (grid_basket_analytics.py, grid_basket_audit.py,
grid_snapshot_drawdown.py, robot_control_center.py,
control_center_watchdog.py, and this module's own ``run_reconstruction``/
``main`` CLI entrypoint) were removed once the grid trading strategy was
retired from the product. ``reconstruct_grid_legs`` itself is retained: it
groups deals into baskets purely by (magic, symbol, side) -- generic
multi-leg/position-grouping logic with no grid-specific averaging, step, or
lot-progression rules -- and remains a real, active dependency of
``trademind.breakeven_counterfactual``'s basket break-even analysis, which
has nothing to do with the retired grid strategy.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA_VERSION = "1.15.0"
EPSILON = 1e-9

REQUIRED_FIELDS = (
    "ticket",
    "position_id",
    "time_msc",
    "symbol",
    "magic",
    "deal_type",
    "entry",
    "volume",
    "price",
)

LEG_FIELDS = (
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "leg_no",
    "opened_at",
    "price",
    "volume",
    "closed_at",
    "gross_profit",
    "commission",
    "swap",
    "net_profit",
    "exit_reason",
    "max_drawdown_money",
    "max_drawdown_pct",
    "max_adverse_points",
    "source_ticket",
    "source_position_id",
    "source_comment",
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


def _name(value: Any, prefix: str) -> str:
    text = _text(value).upper()
    if text.startswith(prefix):
        text = text[len(prefix) :]
    return text


def _iso_from_millis(value: Any) -> str:
    milliseconds = _int(value)
    if milliseconds <= 0:
        raise ValueError(f"Invalid time_msc: {value}")
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).isoformat()


def _load_deals(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"MT5 deal file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        missing = [field for field in REQUIRED_FIELDS if field not in fields]
        if missing:
            raise ValueError(f"MT5 deal file misses fields: {', '.join(missing)}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"MT5 deal file is empty: {path}")
    seen: set[int] = set()
    for row in rows:
        ticket = _int(row.get("ticket"))
        if ticket <= 0:
            raise ValueError(f"Invalid deal ticket: {row.get('ticket')}")
        if ticket in seen:
            raise ValueError(f"Duplicate deal ticket: {ticket}")
        seen.add(ticket)
        _iso_from_millis(row.get("time_msc"))
        if _float(row.get("volume")) <= 0 or _float(row.get("price")) <= 0:
            raise ValueError(f"Invalid volume or price for deal {ticket}")
    return sorted(rows, key=lambda row: (_int(row.get("time_msc")), _int(row.get("ticket"))))


@dataclass(slots=True)
class BasketState:
    basket_id: str
    robot: str
    magic: str
    symbol: str
    side: str
    legs: list[dict[str, Any]] = field(default_factory=list)
    positions: set[str] = field(default_factory=set)
    gross_profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0


def reconstruct_grid_legs(deals: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    active_by_key: dict[tuple[str, str, str], BasketState] = {}
    basket_by_position: dict[str, BasketState] = {}
    position_volume: dict[str, float] = defaultdict(float)
    all_baskets: list[BasketState] = []
    orphan_exits = 0
    unsupported_inout = 0

    for deal in deals:
        entry = _name(deal.get("entry"), "DEAL_ENTRY_")
        deal_type = _name(deal.get("deal_type"), "DEAL_TYPE_")
        if deal_type not in {"BUY", "SELL"}:
            continue
        if entry == "INOUT":
            unsupported_inout += 1
            continue
        position_id = _text(deal.get("position_id"))
        if not position_id or position_id == "0":
            continue
        volume = _float(deal.get("volume"))
        magic = _text(deal.get("magic"))
        symbol = _text(deal.get("symbol"))

        if entry == "IN":
            side = deal_type
            key = (magic, symbol, side)
            basket = active_by_key.get(key)
            if basket is None:
                time_msc = _int(deal.get("time_msc"))
                ticket = _int(deal.get("ticket"))
                basket = BasketState(
                    basket_id=f"TM-{magic}-{symbol}-{side}-{time_msc}-{ticket}",
                    robot=_text(deal.get("robot")) or f"MAGIC_{magic}",
                    magic=magic,
                    symbol=symbol,
                    side=side,
                )
                active_by_key[key] = basket
                all_baskets.append(basket)
            leg_no = len(basket.legs) + 1
            basket.legs.append(
                {
                    "basket_id": basket.basket_id,
                    "robot": basket.robot,
                    "magic": magic,
                    "symbol": symbol,
                    "side": side,
                    "leg_no": leg_no,
                    "opened_at": _iso_from_millis(deal.get("time_msc")),
                    "price": _float(deal.get("price")),
                    "volume": volume,
                    "closed_at": "",
                    "gross_profit": "",
                    "commission": "",
                    "swap": "",
                    "net_profit": "",
                    "exit_reason": "",
                    "max_drawdown_money": "",
                    "max_drawdown_pct": "",
                    "max_adverse_points": "",
                    "source_ticket": _int(deal.get("ticket")),
                    "source_position_id": position_id,
                    "source_comment": _text(deal.get("comment")),
                }
            )
            basket.positions.add(position_id)
            basket_by_position[position_id] = basket
            position_volume[position_id] += volume
            basket.commission += _float(deal.get("commission"))
            basket.swap += _float(deal.get("swap"))
            basket.fee += _float(deal.get("fee"))
            continue

        if entry not in {"OUT", "OUT_BY"}:
            continue
        basket = basket_by_position.get(position_id)
        if basket is None:
            orphan_exits += 1
            continue
        basket.gross_profit += _float(deal.get("profit"))
        basket.commission += _float(deal.get("commission"))
        basket.swap += _float(deal.get("swap"))
        basket.fee += _float(deal.get("fee"))
        position_volume[position_id] = max(0.0, position_volume[position_id] - volume)
        if position_volume[position_id] <= EPSILON:
            position_volume.pop(position_id, None)
            basket.positions.discard(position_id)
            basket_by_position.pop(position_id, None)
        if basket.positions:
            continue

        closed_at = _iso_from_millis(deal.get("time_msc"))
        net_profit = basket.gross_profit + basket.commission + basket.swap + basket.fee
        exit_reason = _name(deal.get("reason"), "DEAL_REASON_") or "CLOSED"
        for leg in basket.legs:
            leg["closed_at"] = closed_at
            leg["gross_profit"] = round(basket.gross_profit, 6)
            leg["commission"] = round(basket.commission + basket.fee, 6)
            leg["swap"] = round(basket.swap, 6)
            leg["net_profit"] = round(net_profit, 6)
            leg["exit_reason"] = exit_reason
        active_by_key.pop((basket.magic, basket.symbol, basket.side), None)

    legs = [leg for basket in all_baskets for leg in basket.legs]
    legs.sort(key=lambda row: (row["opened_at"], row["basket_id"], row["leg_no"]))
    stats = {
        "source_deals": len(deals),
        "baskets": len(all_baskets),
        "completed_baskets": sum(bool(basket.legs and basket.legs[0]["closed_at"]) for basket in all_baskets),
        "open_baskets": sum(bool(basket.legs and not basket.legs[0]["closed_at"]) for basket in all_baskets),
        "legs": len(legs),
        "orphan_exits": orphan_exits,
        "unsupported_inout": unsupported_inout,
    }
    return legs, stats
