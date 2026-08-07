"""Read-only break-even statistics monitor for MT5 basket snapshots.

This module observes immutable/current CSV snapshots produced by the existing
TradeMind MT5 risk exporter. It NEVER modifies positions, stop-losses, take-
profits, robot settings, terminal settings, or source CSV files.

A basket observation is grouped by account + magic + symbol + side and by the
exact set of currently open position ids. The first valid snapshot of an epoch
freezes the weighted basket entry and protective stop as the R reference.
When price reaches +1R, the monitor records a *shadow* BE trigger. If a later
snapshot revisits the basket entry, it records that a hypothetical BE stop
would have been touched at snapshot resolution. No trading action is taken.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

VERSION = "1.28.0"
TRIGGER_R = 1.0
VALID_SIDES = {"BUY", "SELL"}

POSITION_REQUIRED_FIELDS = (
    "time_msc",
    "account_login",
    "position_ticket",
    "position_id",
    "symbol",
    "side",
    "volume",
    "open_price",
    "current_price",
    "sl",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(_text(value).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_text(value)))
    except (TypeError, ValueError):
        return default


def _iso_from_millis(value: Any) -> str:
    milliseconds = _int(value)
    if milliseconds <= 0:
        raise ValueError(f"invalid time_msc: {value}")
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    items = list(rows)
    if not items:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in items:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def _read_positions(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"positions CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(reader.fieldnames or ())
        missing = [field for field in POSITION_REQUIRED_FIELDS if field not in fields]
        if missing:
            raise ValueError(f"positions CSV misses fields: {', '.join(missing)}")
        rows = [
            {key: _text(value) for key, value in dict(row).items()}
            for row in reader
            if any(_text(value) for value in dict(row).values())
        ]
    if not rows:
        return []
    latest_msc = max(_int(row.get("time_msc")) for row in rows)
    if latest_msc <= 0:
        raise ValueError("positions CSV has no valid time_msc")
    return [dict(row) for row in rows if _int(row.get("time_msc")) == latest_msc]


def _position_identity(row: Mapping[str, Any]) -> str:
    return _text(row.get("position_id")) or _text(row.get("position_ticket"))


def _group_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    side = _text(row.get("side")).upper()
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side: {side}")
    return (
        _text(row.get("account_login")),
        _text(row.get("magic")),
        _text(row.get("symbol")).upper(),
        side,
    )


def _epoch_id(group: tuple[str, str, str, str], position_ids: Sequence[str]) -> str:
    raw = "|".join((*group, *sorted(position_ids)))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"BE:{group[0]}:{group[1]}:{group[2]}:{group[3]}:{digest}"


def _directional_r(side: str, entry: float, current: float, risk_distance: float) -> float:
    if risk_distance <= 0:
        return 0.0
    move = current - entry if side == "BUY" else entry - current
    return move / risk_distance


def _revisited_entry(side: str, entry: float, current: float) -> bool:
    return current <= entry if side == "BUY" else current >= entry


@dataclass(frozen=True, slots=True)
class BasketObservation:
    epoch_id: str
    captured_at: str
    account_login: str
    magic: str
    symbol: str
    side: str
    position_ids: tuple[str, ...]
    position_count: int
    total_volume: float
    weighted_entry: float
    current_price: float
    reference_sl: float
    risk_distance: float
    progress_r: float


def _observation(rows: Sequence[dict[str, str]]) -> BasketObservation | None:
    if not rows:
        return None
    group = _group_key(rows[0])
    if any(_group_key(row) != group for row in rows[1:]):
        raise ValueError("mixed basket identity")

    position_ids = tuple(sorted(_position_identity(row) for row in rows))
    if any(not value for value in position_ids):
        raise ValueError("position identity is blank")

    volumes = [_float(row.get("volume")) for row in rows]
    if any(value <= 0 for value in volumes):
        raise ValueError("position volume must be positive")
    total_volume = sum(volumes)
    entries = [_float(row.get("open_price")) for row in rows]
    currents = [_float(row.get("current_price")) for row in rows]
    stops = [_float(row.get("sl")) for row in rows]
    if any(value <= 0 for value in entries + currents):
        raise ValueError("position price must be positive")
    if any(value <= 0 for value in stops):
        return None

    weighted_entry = sum(price * volume for price, volume in zip(entries, volumes)) / total_volume
    current_price = sum(price * volume for price, volume in zip(currents, volumes)) / total_volume

    # Basket robots normally share one physical stop. Mixed stops are not
    # silently averaged because that would invent a risk reference.
    stop_min = min(stops)
    stop_max = max(stops)
    tolerance = max(abs(weighted_entry), 1.0) * 1e-9
    if stop_max - stop_min > tolerance:
        return None
    reference_sl = sum(stops) / len(stops)

    side = group[3]
    risk_distance = (
        weighted_entry - reference_sl if side == "BUY" else reference_sl - weighted_entry
    )
    if risk_distance <= 0:
        return None

    return BasketObservation(
        epoch_id=_epoch_id(group, position_ids),
        captured_at=_iso_from_millis(rows[0].get("time_msc")),
        account_login=group[0],
        magic=group[1],
        symbol=group[2],
        side=side,
        position_ids=position_ids,
        position_count=len(rows),
        total_volume=round(total_volume, 8),
        weighted_entry=round(weighted_entry, 10),
        current_price=round(current_price, 10),
        reference_sl=round(reference_sl, 10),
        risk_distance=round(risk_distance, 10),
        progress_r=round(_directional_r(side, weighted_entry, current_price, risk_distance), 6),
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": VERSION, "epochs": {}, "event_keys": []}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("state root must be an object")
    payload.setdefault("epochs", {})
    payload.setdefault("event_keys", [])
    return payload


def _event_key(epoch_id: str, event: str) -> str:
    return f"{epoch_id}|{event}"


def run_monitor(positions_csv: Path, output_dir: Path) -> dict[str, Any]:
    rows = _read_positions(positions_csv)
    state_path = output_dir / "state.json"
    events_path = output_dir / "events.jsonl"
    status_path = output_dir / "status.json"
    state = _load_state(state_path)
    epochs: dict[str, Any] = dict(state.get("epochs", {}))
    event_keys = set(str(value) for value in state.get("event_keys", []))

    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(_group_key(row), []).append(row)

    observations = [value for value in (_observation(group) for group in grouped.values()) if value]
    active_ids = {item.epoch_id for item in observations}
    new_events: list[dict[str, Any]] = []

    for item in observations:
        record = dict(epochs.get(item.epoch_id, {}))
        if not record:
            record = {
                "epoch_id": item.epoch_id,
                "first_seen_at": item.captured_at,
                "account_login": item.account_login,
                "magic": item.magic,
                "symbol": item.symbol,
                "side": item.side,
                "position_ids": list(item.position_ids),
                "position_count": item.position_count,
                "total_volume": item.total_volume,
                "weighted_entry": item.weighted_entry,
                "reference_sl": item.reference_sl,
                "risk_distance": item.risk_distance,
                "trigger_r": TRIGGER_R,
                "be_triggered": False,
                "be_revisited": False,
                "max_favorable_r": item.progress_r,
                "max_adverse_r": item.progress_r,
                "state": "OPEN",
            }
            key = _event_key(item.epoch_id, "EPOCH_STARTED")
            if key not in event_keys:
                new_events.append({
                    "schema_version": VERSION,
                    "event": "EPOCH_STARTED",
                    "observed_at": item.captured_at,
                    "epoch_id": item.epoch_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "shadow_only": True,
                })
                event_keys.add(key)

        record["last_seen_at"] = item.captured_at
        record["last_price"] = item.current_price
        record["last_progress_r"] = item.progress_r
        record["max_favorable_r"] = max(_float(record.get("max_favorable_r")), item.progress_r)
        record["max_adverse_r"] = min(_float(record.get("max_adverse_r")), item.progress_r)
        record["state"] = "OPEN"

        if item.progress_r >= TRIGGER_R and not bool(record.get("be_triggered")):
            record["be_triggered"] = True
            record["be_triggered_at"] = item.captured_at
            record["be_trigger_price"] = item.current_price
            key = _event_key(item.epoch_id, "BE_TRIGGER_REACHED")
            if key not in event_keys:
                new_events.append({
                    "schema_version": VERSION,
                    "event": "BE_TRIGGER_REACHED",
                    "observed_at": item.captured_at,
                    "epoch_id": item.epoch_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "trigger_r": TRIGGER_R,
                    "weighted_entry": item.weighted_entry,
                    "observed_price": item.current_price,
                    "shadow_only": True,
                })
                event_keys.add(key)

        if bool(record.get("be_triggered")) and not bool(record.get("be_revisited")):
            if _revisited_entry(item.side, _float(record.get("weighted_entry")), item.current_price):
                record["be_revisited"] = True
                record["be_revisited_at"] = item.captured_at
                key = _event_key(item.epoch_id, "BE_REVISIT_OBSERVED")
                if key not in event_keys:
                    new_events.append({
                        "schema_version": VERSION,
                        "event": "BE_REVISIT_OBSERVED",
                        "observed_at": item.captured_at,
                        "epoch_id": item.epoch_id,
                        "symbol": item.symbol,
                        "side": item.side,
                        "shadow_only": True,
                        "resolution": "SNAPSHOT_LEVEL_ONLY",
                    })
                    event_keys.add(key)
        epochs[item.epoch_id] = record

    for epoch_id, record in epochs.items():
        if record.get("state") == "OPEN" and epoch_id not in active_ids:
            record["state"] = "NO_LONGER_OPEN"
            key = _event_key(epoch_id, "EPOCH_NO_LONGER_OPEN")
            if key not in event_keys:
                new_events.append({
                    "schema_version": VERSION,
                    "event": "EPOCH_NO_LONGER_OPEN",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "epoch_id": epoch_id,
                    "symbol": record.get("symbol", ""),
                    "side": record.get("side", ""),
                    "shadow_only": True,
                    "final_trade_result": "NOT_INFERRED_FROM_POSITION_SNAPSHOT",
                })
                event_keys.add(key)

    epoch_values = list(epochs.values())
    triggered = [record for record in epoch_values if bool(record.get("be_triggered"))]
    revisited = [record for record in triggered if bool(record.get("be_revisited"))]
    status = {
        "schema_version": VERSION,
        "state": "OK",
        "mode": "READ_ONLY_SHADOW_BREAKEVEN_STATISTICS",
        "trigger_r": TRIGGER_R,
        "snapshot_rows": len(rows),
        "trackable_basket_epochs": len(epoch_values),
        "open_trackable_epochs": sum(record.get("state") == "OPEN" for record in epoch_values),
        "be_triggered_epochs": len(triggered),
        "be_revisited_after_trigger_epochs": len(revisited),
        "be_trigger_rate": round(len(triggered) / len(epoch_values), 6) if epoch_values else 0.0,
        "be_revisit_rate_after_trigger": round(len(revisited) / len(triggered), 6) if triggered else 0.0,
        "new_events": len(new_events),
        "limitations": [
            "Snapshot resolution can miss intrainterval touches.",
            "Closed trade P/L is not inferred from a disappearing position snapshot.",
            "Basket epochs with missing or inconsistent initial SL are not fabricated.",
        ],
        "safety": {
            "read_only": True,
            "shadow_only": True,
            "orders_enabled": False,
            "position_modify_called": False,
            "broker_api_called": False,
            "source_csv_modified": False,
            "robot_settings_modified": False,
        },
    }

    state = {
        "schema_version": VERSION,
        "epochs": epochs,
        "event_keys": sorted(event_keys),
    }
    _atomic_json(state_path, state)
    _append_jsonl(events_path, new_events)
    _atomic_json(status_path, status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeMind read-only 1R break-even statistics monitor")
    parser.add_argument("--positions-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    status = run_monitor(Path(args.positions_csv), Path(args.output_dir))
    print("TradeMind v1.28 BreakEven Statistics Monitor")
    print("READ-ONLY / SHADOW ONLY / ORDERS OFF")
    print(f"Trackable basket epochs: {status['trackable_basket_epochs']}")
    print(f"BE trigger reached: {status['be_triggered_epochs']}")
    print(f"BE revisit after trigger: {status['be_revisited_after_trigger_epochs']}")
    print(f"Output: {Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
