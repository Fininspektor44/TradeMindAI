"""TradeMind Product UI v1.26 for focused H1 swing opportunities."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v125 as previous

VERSION = "1.26.0"
SETUP_FAMILY = "CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT"
_BASE_BUILD_PAYLOAD = previous.build_payload
_BASE_RENDER = previous.render
_BASE_MARKET_HTML = previous._market_html
_BASE_CRYPTO_MARKET_HTML = previous._crypto_market_html


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.{digits}f}".replace(".", ",") + suffix


def _price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    digits = 2 if abs(number) >= 100 else 5
    return _number(number, digits)


def _crypto_market_html(candidate: Mapping[str, Any]) -> str:
    base_html = _BASE_CRYPTO_MARKET_HTML(candidate)
    if str(candidate.get("setup_family") or "") != SETUP_FAMILY:
        return base_html

    market = _mapping(candidate.get("market"))
    structure = _mapping(market.get("structure"))
    volume = _mapping(market.get("volume"))
    volatility = _mapping(market.get("volatility"))
    confirmation = _mapping(market.get("confirmation"))
    custom = _mapping(market.get("custom"))
    plan = _mapping(candidate.get("plan"))
    targets = plan.get("targets") if isinstance(plan.get("targets"), list) else []
    target = targets[0] if targets else custom.get("h1_target")

    focused = f"""
  <section><small>H1 Swing v1.26</small><h4>Карта сделки</h4>
    <p><span>Направление H1</span><b>{html.escape(str(structure.get('swing_bias') or '—'))}</b></p>
    <p><span>M15 veto</span><b>нет</b></p>
    <p><span>Последний M5 экстремум</span><b>{_price(confirmation.get('breakout_level'))}</b></p>
    <p><span>Закрытие за уровнем</span><b>да</b></p>
    <p><span>Цель H1</span><b>{_price(target)}</b></p>
  </section>
  <section><small>Подтверждение M5</small><h4>Объём и delta</h4>
    <p><span>Объём к медиане 20</span><b>{_number(volume.get('m5_volume_ratio_20'), 2, 'x')}</b></p>
    <p><span>Объём M5</span><b>{_number(volume.get('m5_volume'), 0)}</b></p>
    <p><span>Медиана объёма</span><b>{_number(volume.get('m5_median_volume_20'), 0)}</b></p>
    <p><span>Delta M5</span><b>{_number(volume.get('m5_delta_turnover'), 0)}</b></p>
  </section>
  <section><small>Потенциал</small><h4>Не мелкая сделка</h4>
    <p><span>RR до H1-цели</span><b>{_number(custom.get('target_rr'), 2, 'R')}</b></p>
    <p><span>Ход до цели</span><b>{_number(volatility.get('target_distance_atr_h1'), 2, ' ATR H1')}</b></p>
    <p><span>Минимум RR</span><b>1,80R</b></p>
    <p><span>Минимум хода</span><b>0,70 ATR H1</b></p>
  </section>
"""
    return base_html.replace("<div class='market-grid'>", "<div class='market-grid'>" + focused, 1)


def _market_html(candidate: Mapping[str, Any]) -> str:
    if str(candidate.get("asset_class") or "").upper() == "CRYPTO":
        return _crypto_market_html(candidate)
    return _BASE_MARKET_HTML(candidate)


def build_payload(
    fx_source: Mapping[str, Any],
    canonical: Path | None,
    crypto_root: Path,
    bars_path: Path,
    *,
    fx_limit: int,
    crypto_limit: int,
    candle_limit: int,
) -> dict[str, Any]:
    payload = _BASE_BUILD_PAYLOAD(
        fx_source,
        canonical,
        crypto_root,
        bars_path,
        fx_limit=fx_limit,
        crypto_limit=crypto_limit,
        candle_limit=candle_limit,
    )
    payload["schema_version"] = VERSION
    candidates = payload.get("candidates", [])
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        for item in candidates:
            if (
                isinstance(item, dict)
                and str(item.get("asset_class") or "").upper() == "CRYPTO"
                and str(item.get("setup_family") or "") == SETUP_FAMILY
            ):
                item["setup_family_label"] = "H1 Swing + M5 объёмный пробой"
    return payload


def render(data: Mapping[str, Any]) -> str:
    original_market = previous._market_html
    try:
        previous._market_html = _market_html
        page = _BASE_RENDER(data)
    finally:
        previous._market_html = original_market
    page = page.replace("TradeMind Product UI v1.25.2", "TradeMind Product UI v1.26")
    page = page.replace(
        "Движок проверяет Forex и нативную структуру Crypto, затем допускает только "
        "статистически подтверждённые сетапы.",
        "Crypto: направление H1, veto M15 и закрытие M5 за последним локальным "
        "экстремумом на подтверждённом объёме.",
    )
    return page


def run_product_ui(
    runtime_root: Path,
    crypto_root: Path,
    bars_path: Path,
    *,
    fx_limit: int = 24,
    crypto_limit: int = 24,
    candle_limit: int = 48,
) -> tuple[Path, Mapping[str, Any]]:
    original_build = previous.build_payload
    original_render = previous.render
    original_version = previous.VERSION
    try:
        previous.build_payload = build_payload
        previous.render = render
        previous.VERSION = VERSION
        index, payload = previous.run_product_ui(
            runtime_root,
            crypto_root,
            bars_path,
            fx_limit=fx_limit,
            crypto_limit=crypto_limit,
            candle_limit=candle_limit,
        )
    finally:
        previous.build_payload = original_build
        previous.render = original_render
        previous.VERSION = original_version

    status_path = runtime_root.expanduser().resolve() / "product" / "status.json"
    status = previous.base.read_json(status_path)
    status = dict(status)
    status.update(
        {
            "schema_version": VERSION,
            "crypto_setup_family": SETUP_FAMILY,
            "crypto_decision_chain": (
                "H1_DIRECTION>M15_VETO>M5_LAST_EXTREMUM_CLOSE_BREAK>"
                "M5_VOLUME_DELTA>H1_TARGET_SPACE"
            ),
            "crypto_position_sizing_available": False,
            "read_only": True,
        }
    )
    previous.base.atomic_write(
        status_path,
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True),
    )
    return index, payload


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
        "crypto_position_sizing_available": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Product UI v1.26 H1 Swing + M5 volume breakout"
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument(
        "--crypto-root",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_26"),
    )
    parser.add_argument(
        "--bybit-bars",
        type=Path,
        default=Path("data/bybit_v1_9/bybit_bars.csv"),
    )
    parser.add_argument("--fx-limit", type=int, default=24)
    parser.add_argument("--crypto-limit", type=int, default=24)
    parser.add_argument("--candle-limit", type=int, default=48)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(argv)
    try:
        index, payload = run_product_ui(
            args.runtime_root,
            args.crypto_root,
            args.bybit_bars,
            fx_limit=args.fx_limit,
            crypto_limit=args.crypto_limit,
            candle_limit=args.candle_limit,
        )
    except (OSError, ValueError, TypeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"TradeMind Product UI v1.26 failed: {exc}")
        return 1

    summary = previous.base._mapping(payload.get("summary"))
    print("TradeMind Product UI v1.26")
    print("H1 Swing + M5 volume breakout. Read-only. Orders OFF. Publication OFF.")
    print(f"Forex displayed: {previous.base.integer(summary.get('forex_displayed'))}")
    print(f"Crypto displayed: {previous.base.integer(summary.get('crypto_displayed'))}")
    print("Crypto position sizing: NOT CALCULATED")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
