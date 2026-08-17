"""Resolve actual basket outcomes against the read-only shadow BE journal.

The resolver never talks to MT5 and never modifies orders or source files. It joins
v1.28 shadow break-even epochs to the existing read-only MT5 deal export and asks
one narrow question: after a shadow BE trigger/revisit, did the real basket later
finish as a winner or a loser?

Money fields named ``*_proxy`` are deliberately not presented as simulated BE P/L.
They use the *actual* final basket P/L only to quantify what was subsequently lost
or avoided after the observed revisit. Exact counterfactual execution costs are not
invented.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# grid_deal_reconstruction retains only its generic (magic, symbol, side)
# basket/leg reconstruction utility -- unrelated to the retired grid trading
# strategy -- specifically because this module depends on it. See that
# module's own docstring.
from trademind.grid_deal_reconstruction import _load_deals, reconstruct_grid_legs

VERSION = "1.29.0"
EPSILON = 1e-9

REPORT_FIELDS = (
    "basket_id",
    "robot",
    "magic",
    "symbol",
    "side",
    "basket_opened_at",
    "basket_closed_at",
    "actual_net_profit",
    "actual_exit_reason",
    "actual_max_legs",
    "mapped_shadow_epochs",
    "be_triggered",
    "be_revisited_after_trigger",
    "effect_class",
    "loss_avoided_proxy_money",
    "opportunity_cost_proxy_money",
    "net_effect_proxy_money",
    "shadow_resolution",
    "notes",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(_text(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REPORT_FIELDS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _load_shadow_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"shadow state not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("epochs"), dict):
        raise ValueError("shadow state must contain an epochs object")
    return {
        str(epoch_id): dict(record)
        for epoch_id, record in payload["epochs"].items()
        if isinstance(record, dict)
    }


def _basket_records(legs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        grouped[_text(leg.get("basket_id"))].append(dict(leg))

    baskets: list[dict[str, Any]] = []
    for basket_id, rows in grouped.items():
        rows.sort(key=lambda row: (_text(row.get("opened_at")), int(_float(row.get("leg_no")))))
        first = rows[0]
        opened = min((_parse_time(row.get("opened_at")) for row in rows), default=None)
        closed = _parse_time(first.get("closed_at"))
        position_ids = {
            _text(row.get("source_position_id"))
            for row in rows
            if _text(row.get("source_position_id"))
        }
        baskets.append(
            {
                "basket_id": basket_id,
                "robot": _text(first.get("robot")),
                "magic": _text(first.get("magic")),
                "symbol": _text(first.get("symbol")).upper(),
                "side": _text(first.get("side")).upper(),
                "opened_at": opened,
                "closed_at": closed,
                "position_ids": position_ids,
                "net_profit": _float(first.get("net_profit")),
                "exit_reason": _text(first.get("exit_reason")),
                "max_legs": max((int(_float(row.get("leg_no"))) for row in rows), default=0),
            }
        )
    return baskets


def _epoch_matches_basket(epoch: Mapping[str, Any], basket: Mapping[str, Any]) -> bool:
    if _text(epoch.get("magic")) != _text(basket.get("magic")):
        return False
    if _text(epoch.get("symbol")).upper() != _text(basket.get("symbol")).upper():
        return False
    if _text(epoch.get("side")).upper() != _text(basket.get("side")).upper():
        return False

    epoch_positions = {
        _text(value) for value in epoch.get("position_ids", []) if _text(value)
    }
    basket_positions = set(basket.get("position_ids", set()))
    if epoch_positions and not epoch_positions.issubset(basket_positions):
        return False

    first_seen = _parse_time(epoch.get("first_seen_at"))
    opened_at = basket.get("opened_at")
    closed_at = basket.get("closed_at")
    if first_seen is None or opened_at is None:
        return False
    if first_seen < opened_at:
        return False
    return closed_at is None or first_seen <= closed_at


def _map_epochs(
    epochs: Mapping[str, Mapping[str, Any]],
    baskets: Sequence[Mapping[str, Any]],
    login: str,
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    mapped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmapped = 0
    ambiguous = 0

    for epoch_id, epoch in epochs.items():
        if login and _text(epoch.get("account_login")) != login:
            continue
        matches = [basket for basket in baskets if _epoch_matches_basket(epoch, basket)]
        if not matches:
            unmapped += 1
            continue
        if len(matches) > 1:
            # Exact/subset position ids normally make the mapping unique. Do not guess if
            # the historical export contains overlapping candidates.
            ambiguous += 1
            continue
        mapped[_text(matches[0].get("basket_id"))].append({"epoch_id": epoch_id, **epoch})
    return mapped, unmapped, ambiguous


def _effect_class(mapped_epochs: Sequence[Mapping[str, Any]], actual_net: float) -> str:
    if not mapped_epochs:
        return "NO_SHADOW_COVERAGE"
    triggered = any(bool(epoch.get("be_triggered")) for epoch in mapped_epochs)
    revisited = any(bool(epoch.get("be_revisited")) for epoch in mapped_epochs)
    if not triggered:
        return "NOT_TRIGGERED"
    if not revisited:
        return "TRIGGERED_NO_REVISIT"
    if actual_net > EPSILON:
        return "WINNER_CUT_BY_BE"
    if actual_net < -EPSILON:
        return "LOSS_AVOIDED_BY_BE"
    return "NEUTRAL_AFTER_BE_REVISIT"


def run_counterfactual(
    shadow_state_path: Path,
    deals_path: Path,
    output_dir: Path,
    *,
    login: str = "",
) -> dict[str, Any]:
    epochs = _load_shadow_state(shadow_state_path)
    deals = _load_deals(deals_path)
    legs, reconstruction = reconstruct_grid_legs(deals)
    baskets = _basket_records(legs)
    mapped, unmapped_epochs, ambiguous_epochs = _map_epochs(epochs, baskets, login)

    rows: list[dict[str, Any]] = []
    for basket in sorted(
        baskets,
        key=lambda item: (
            item.get("opened_at") or datetime.min.replace(tzinfo=timezone.utc),
            _text(item.get("basket_id")),
        ),
    ):
        closed_at = basket.get("closed_at")
        if closed_at is None:
            continue
        basket_id = _text(basket.get("basket_id"))
        shadow_epochs = mapped.get(basket_id, [])
        actual_net = _float(basket.get("net_profit"))
        effect = _effect_class(shadow_epochs, actual_net)
        triggered = any(bool(epoch.get("be_triggered")) for epoch in shadow_epochs)
        revisited = any(bool(epoch.get("be_revisited")) for epoch in shadow_epochs)

        loss_avoided = -actual_net if effect == "LOSS_AVOIDED_BY_BE" else 0.0
        opportunity_cost = actual_net if effect == "WINNER_CUT_BY_BE" else 0.0
        net_effect = loss_avoided - opportunity_cost
        notes = ""
        if revisited:
            notes = (
                "Proxy uses actual final basket P/L after an observed snapshot-level BE revisit; "
                "exact hypothetical BE execution costs are not simulated."
            )

        rows.append(
            {
                "basket_id": basket_id,
                "robot": basket.get("robot", ""),
                "magic": basket.get("magic", ""),
                "symbol": basket.get("symbol", ""),
                "side": basket.get("side", ""),
                "basket_opened_at": basket["opened_at"].isoformat() if basket.get("opened_at") else "",
                "basket_closed_at": closed_at.isoformat(),
                "actual_net_profit": round(actual_net, 6),
                "actual_exit_reason": basket.get("exit_reason", ""),
                "actual_max_legs": basket.get("max_legs", 0),
                "mapped_shadow_epochs": len(shadow_epochs),
                "be_triggered": triggered,
                "be_revisited_after_trigger": revisited,
                "effect_class": effect,
                "loss_avoided_proxy_money": round(loss_avoided, 6),
                "opportunity_cost_proxy_money": round(opportunity_cost, 6),
                "net_effect_proxy_money": round(net_effect, 6),
                "shadow_resolution": "SNAPSHOT_LEVEL_ONLY",
                "notes": notes,
            }
        )

    covered = [row for row in rows if row["mapped_shadow_epochs"] > 0]
    affected = [row for row in rows if row["be_revisited_after_trigger"]]
    protected = [row for row in rows if row["effect_class"] == "LOSS_AVOIDED_BY_BE"]
    cut_winners = [row for row in rows if row["effect_class"] == "WINNER_CUT_BY_BE"]
    trigger_no_revisit = [row for row in rows if row["effect_class"] == "TRIGGERED_NO_REVISIT"]

    loss_avoided_proxy = sum(_float(row["loss_avoided_proxy_money"]) for row in protected)
    opportunity_cost_proxy = sum(
        _float(row["opportunity_cost_proxy_money"]) for row in cut_winners
    )
    status = {
        "schema_version": VERSION,
        "state": "OK" if ambiguous_epochs == 0 else "WARN_AMBIGUOUS_MAPPING",
        "mode": "READ_ONLY_BE_COUNTERFACTUAL",
        "login": login,
        "source_deals": reconstruction.get("source_deals", len(deals)),
        "reconstructed_baskets": reconstruction.get("baskets", len(baskets)),
        "completed_baskets": len(rows),
        "shadow_epochs": len(epochs),
        "covered_completed_baskets": len(covered),
        "affected_by_shadow_be_baskets": len(affected),
        "losses_avoided_count": len(protected),
        "winners_cut_count": len(cut_winners),
        "triggered_without_revisit_count": len(trigger_no_revisit),
        "unmapped_shadow_epochs": unmapped_epochs,
        "ambiguous_shadow_epochs": ambiguous_epochs,
        "loss_avoided_proxy_money": round(loss_avoided_proxy, 6),
        "opportunity_cost_proxy_money": round(opportunity_cost_proxy, 6),
        "net_effect_proxy_money": round(loss_avoided_proxy - opportunity_cost_proxy, 6),
        "interpretation": (
            "Proxy compares actual final basket P/L after an observed shadow BE revisit. "
            "It is not simulated execution P/L and does not invent commissions, swap, slippage, "
            "or intraminute touches."
        ),
        "safety": {
            "read_only": True,
            "shadow_only": True,
            "orders_enabled": False,
            "position_modify_called": False,
            "broker_api_called": False,
            "source_deals_modified": False,
            "robot_settings_modified": False,
        },
    }

    _atomic_csv(output_dir / "basket_be_counterfactual.csv", rows)
    _atomic_json(output_dir / "status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeMind read-only BE counterfactual resolver")
    parser.add_argument("--shadow-state", required=True, type=Path)
    parser.add_argument("--deals", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--login", default="")
    args = parser.parse_args()

    try:
        status = run_counterfactual(
            args.shadow_state,
            args.deals,
            args.output_dir,
            login=args.login,
        )
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
        print(f"BreakEven counterfactual failed: {exc}")
        return 1

    print("TradeMind v1.29 BreakEven Counterfactual")
    print("READ-ONLY / SHADOW ONLY / ORDERS OFF")
    print(f"Covered completed baskets: {status['covered_completed_baskets']}")
    print(f"Losses avoided: {status['losses_avoided_count']}")
    print(f"Winners cut: {status['winners_cut_count']}")
    print(f"Net effect proxy: {status['net_effect_proxy_money']}")
    print(f"Output: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
