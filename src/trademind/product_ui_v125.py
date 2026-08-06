"""TradeMind Product UI v1.25 with native crypto market structure."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v124 as previous

VERSION = "1.25.0"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    raw = str(value if value is not None else "").strip()
    translations = {
        "BULLISH": "бычий",
        "BEARISH": "медвежий",
        "NEUTRAL": "нейтральный",
        "NONE": "нет",
        "BULLISH_BOS": "бычий BOS",
        "BEARISH_BOS": "медвежий BOS",
        "BULLISH_CHOCH": "бычий CHoCH",
        "BEARISH_CHOCH": "медвежий CHoCH",
        "SSL_SWEEP": "съём ликвидности снизу",
        "BSL_SWEEP": "съём ликвидности сверху",
        "BULLISH_FVG": "бычий FVG",
        "BEARISH_FVG": "медвежий FVG",
        "OK": "готово",
        "DEGRADED": "неполная история",
    }
    return html.escape(translations.get(raw.upper(), raw or "—"))


def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    rendered = f"{number:.{digits}f}".replace(".", ",")
    return f"{rendered}{suffix}"


def _percent(value: Any, digits: int = 1) -> str:
    try:
        return _number(100.0 * float(value), digits, "%")
    except (TypeError, ValueError):
        return "—"


def _yes_no(value: Any) -> str:
    return "да" if bool(value) else "нет"


def _price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    digits = 2 if abs(number) >= 100 else 5
    return _number(number, digits)


def _crypto_market_html(candidate: Mapping[str, Any]) -> str:
    market = _mapping(candidate.get("market"))
    structure = _mapping(market.get("structure"))
    liquidity = _mapping(market.get("liquidity"))
    fibonacci = _mapping(market.get("fibonacci"))
    volume = _mapping(market.get("volume"))
    volatility = _mapping(market.get("volatility"))
    execution = _mapping(market.get("execution"))
    sentiment = _mapping(market.get("sentiment"))
    custom = _mapping(market.get("custom"))
    confirmation = _mapping(market.get("confirmation"))
    return f"""
<div class='market-grid'>
  <section><small>Нативная структура</small><h4>BOS и CHoCH</h4>
    <p><span>Уклон H1</span><b>{_text(structure.get('swing_bias'))}</b></p>
    <p><span>Слом H1</span><b>{_text(structure.get('swing_break'))}</b></p>
    <p><span>Уклон M15</span><b>{_text(structure.get('internal_bias'))}</b></p>
    <p><span>Слом M15</span><b>{_text(structure.get('internal_break'))}</b></p>
  </section>
  <section><small>Ликвидность</small><h4>Sweep и FVG</h4>
    <p><span>Событие sweep</span><b>{_text(liquidity.get('sweep_type'))}</b></p>
    <p><span>Глубина sweep</span><b>{_number(liquidity.get('sweep_depth_atr'), 2, ' ATR')}</b></p>
    <p><span>Ценовой разрыв</span><b>{_text(liquidity.get('fvg'))}</b></p>
    <p><span>Размер FVG</span><b>{_number(liquidity.get('fvg_size_atr'), 2, ' ATR')}</b></p>
  </section>
  <section><small>Fibonacci</small><h4>Коррекция и OTE</h4>
    <p><span>Текущая коррекция</span><b>{_percent(fibonacci.get('retracement'))}</b></p>
    <p><span>Цена 61,8%</span><b>{_price(fibonacci.get('level_618'))}</b></p>
    <p><span>Цена 70,5%</span><b>{_price(fibonacci.get('level_705'))}</b></p>
    <p><span>Цена 79,0%</span><b>{_price(fibonacci.get('level_790'))}</b></p>
    <p><span>Цена в OTE</span><b>{_yes_no(fibonacci.get('ote_hit'))}</b></p>
  </section>
  <section><small>Order Flow</small><h4>Delta и стакан</h4>
    <p><span>Delta M15</span><b>{_number(volume.get('m15_delta_turnover'), 0)}</b></p>
    <p><span>Delta M5</span><b>{_number(volume.get('m5_delta_turnover'), 0)}</b></p>
    <p><span>Стакан M15</span><b>{_number(custom.get('m15_book_imbalance_10'), 3)}</b></p>
    <p><span>Стакан M5</span><b>{_number(custom.get('m5_book_imbalance_10'), 3)}</b></p>
    <p><span>Сделок M5</span><b>{_number(volume.get('m5_trade_count'), 0)}</b></p>
  </section>
  <section><small>Деривативы</small><h4>Funding, basis и OI</h4>
    <p><span>Funding</span><b>{_percent(sentiment.get('funding_rate'), 4)}</b></p>
    <p><span>Basis</span><b>{_number(sentiment.get('basis_bps'), 2, ' б.п.')}</b></p>
    <p><span>Изменение OI H1</span><b>{_percent(sentiment.get('h1_open_interest_change_pct'), 2)}</b></p>
    <p><span>Изменение OI M15</span><b>{_percent(sentiment.get('m15_open_interest_change_pct'), 2)}</b></p>
  </section>
  <section><small>Исполнение</small><h4>ATR, спред и источник</h4>
    <p><span>ATR M5</span><b>{_price(volatility.get('atr_m5'))}</b></p>
    <p><span>ATR M15</span><b>{_price(volatility.get('atr_m15'))}</b></p>
    <p><span>Спред M5</span><b>{_number(execution.get('spread_bps'), 2, ' б.п.')}</b></p>
    <p><span>Дистанция стопа</span><b>{_percent(volatility.get('risk_distance_pct'), 2)}</b></p>
    <p><span>Структурный движок</span><b>{_text(custom.get('structure_state'))}</b></p>
    <p><span>Будущие свечи</span><b>{_yes_no(confirmation.get('future_bars_used'))}</b></p>
  </section>
</div>
"""


def _market_html(candidate: Mapping[str, Any]) -> str:
    if str(candidate.get("asset_class") or "").upper() == "CRYPTO":
        return _crypto_market_html(candidate)
    return previous._BASE_MARKET_HTML(candidate)


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
    payload = previous.build_payload(
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
            ):
                item["setup_family_label"] = "Крипто Structure + Flow"
    return payload


def render(data: Mapping[str, Any]) -> str:
    original = previous._market_html
    try:
        previous._market_html = _market_html
        page = previous.render(data)
    finally:
        previous._market_html = original
    page = page.replace("TradeMind Product UI v1.24", "TradeMind Product UI v1.25")
    page = page.replace(
        "Движок фильтрует Forex и Crypto и не показывает сырой шум как готовый сигнал.",
        "Движок проверяет Forex и нативную структуру Crypto, затем допускает только "
        "статистически подтверждённые сетапы.",
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
    base = previous.base
    root = runtime_root.expanduser().resolve()
    source = base.read_json(root / "dashboard" / "data.json")
    runtime = base._mapping(source.get("runtime"))
    paths = base._mapping(runtime.get("paths"))
    canonical_text = base.text(paths.get("canonical_volume"))
    canonical = Path(canonical_text).expanduser().resolve() if canonical_text else None
    payload = build_payload(
        source,
        canonical,
        crypto_root.expanduser().resolve(),
        bars_path.expanduser().resolve(),
        fx_limit=fx_limit,
        crypto_limit=crypto_limit,
        candle_limit=candle_limit,
    )
    output = root / "product"
    index = output / "index.html"
    base.atomic_write(index, render(payload))
    base.atomic_write(
        output / "data.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )
    crypto_status = base._mapping(payload.get("crypto"))
    base.atomic_write(
        output / "status.json",
        json.dumps(
            {
                "schema_version": VERSION,
                "state": "OK",
                "index": str(index),
                "signals": len(payload.get("candidates", [])),
                "forex_signals": base.integer(
                    base._mapping(payload.get("summary")).get("forex_displayed")
                ),
                "crypto_signals": base.integer(crypto_status.get("displayed")),
                "crypto_state": crypto_status.get("state"),
                "native_crypto_structure": True,
                "read_only": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return index, payload


def safety_contract() -> Mapping[str, Any]:
    return {
        "read_only": True,
        "orders_enabled": False,
        "publication_enabled": False,
        "broker_api_called": False,
        "future_bars_used": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradeMind Product UI v1.25 Forex + native Crypto structure"
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument(
        "--crypto-root",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_25"),
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
        print(f"TradeMind Product UI v1.25 failed: {exc}")
        return 1

    summary = previous.base._mapping(payload.get("summary"))
    print("TradeMind Product UI v1.25")
    print("Forex + native Crypto structure. Read-only. Orders OFF. Publication OFF.")
    print(f"Forex displayed: {previous.base.integer(summary.get('forex_displayed'))}")
    print(f"Crypto displayed: {previous.base.integer(summary.get('crypto_displayed'))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
