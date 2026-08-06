"""TradeMind Product UI v1.26.1 for focused H1 swing opportunities."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v125 as previous

VERSION = "1.26.1"
SETUP_FAMILY = "CRYPTO_H1_SWING_M5_VOLUME_BREAKOUT"
v124 = previous.previous
base = v124.base
_BASE_BUILD_PAYLOAD = previous.build_payload
_BASE_RENDER = previous.render
_BASE_MARKET_HTML = previous._market_html
_BASE_CRYPTO_MARKET_HTML = previous._crypto_market_html
_BASE_V124_CARD = v124._signal_card
_BASE_V124_DIALOG = v124._signal_dialog
_BASE_CANDLE_SVG = base.candle_svg


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _float(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _float(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}".replace(".", ",") + suffix


def _price_digits(value: float) -> int:
    absolute = abs(value)
    if absolute >= 100:
        return 2
    if absolute >= 1:
        return 4
    return 5


def _price(value: Any) -> str:
    number = _float(value)
    if not math.isfinite(number):
        return "—"
    return _number(number, _price_digits(number))


def _price_plain(value: Any) -> str:
    number = _float(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{_price_digits(number)}f}"


def _is_v126_crypto(candidate: Mapping[str, Any]) -> bool:
    return (
        str(candidate.get("asset_class") or "").upper() == "CRYPTO"
        and str(candidate.get("setup_family") or "") == SETUP_FAMILY
    )


def _decision_reasons(candidate: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for raw in _sequence(candidate.get("reasons")):
        value = str(raw or "").strip()
        if value and value not in reasons:
            reasons.append(value)

    factor_reasons = _mapping(candidate.get("factor_reasons"))
    for key in ("structure", "confirmation", "volume", "volatility"):
        for raw in _sequence(factor_reasons.get(key)):
            value = str(raw or "").strip()
            if value and value not in reasons:
                reasons.append(value)
    return reasons[:6]


def _price_scale_svg(candidate: Mapping[str, Any]) -> str:
    if not _is_v126_crypto(candidate):
        return _BASE_CANDLE_SVG(candidate)

    candles = [_mapping(item) for item in _sequence(candidate.get("candles"))]
    if not candles:
        return _BASE_CANDLE_SVG(candidate)

    plan = _mapping(candidate.get("plan"))
    targets = _sequence(plan.get("targets"))
    entry = _float(plan.get("average_entry"))
    stop = _float(plan.get("stop_price"))
    target = _float(targets[0]) if targets else math.nan

    values = [
        _float(item.get(key))
        for item in candles
        for key in ("low", "high")
    ]
    values.extend((entry, stop, target))
    values = [value for value in values if math.isfinite(value) and value > 0]
    if not values:
        return _BASE_CANDLE_SVG(candidate)

    low = min(values)
    high = max(values)
    pad = max((high - low) * 0.09, abs(high) * 0.0005, 1e-8)
    low -= pad
    high += pad

    width = 620.0
    height = 202.0
    left = 10.0
    right = 132.0
    top = 12.0
    bottom = 18.0
    plot_right = width - right
    plot_width = plot_right - left
    plot_height = height - top - bottom
    step = plot_width / max(1, len(candles))
    body_width = max(2.0, min(7.0, step * 0.58))

    def y(value: float) -> float:
        return top + (high - value) / max(high - low, 1e-12) * plot_height

    parts = [
        f"<svg class='price-scale-chart' viewBox='0 0 {width:.0f} {height:.0f}' "
        "aria-label='M5 свечи со шкалой входа, стопа и цели'>",
        f"<rect class='price-chart-bg' x='{left}' y='{top}' width='{plot_width}' "
        f"height='{plot_height}' rx='8'/>",
    ]

    if all(math.isfinite(value) for value in (entry, target)):
        zone_y = min(y(entry), y(target))
        zone_height = max(1.0, abs(y(entry) - y(target)))
        parts.append(
            f"<rect class='reward-zone' x='{left}' y='{zone_y:.2f}' "
            f"width='{plot_width}' height='{zone_height:.2f}'/>"
        )
    if all(math.isfinite(value) for value in (entry, stop)):
        zone_y = min(y(entry), y(stop))
        zone_height = max(1.0, abs(y(entry) - y(stop)))
        parts.append(
            f"<rect class='risk-zone' x='{left}' y='{zone_y:.2f}' "
            f"width='{plot_width}' height='{zone_height:.2f}'/>"
        )

    for index in range(6):
        tick = high - (high - low) * index / 5
        tick_y = y(tick)
        parts.append(
            f"<line class='axis-grid' x1='{left}' x2='{plot_right}' "
            f"y1='{tick_y:.2f}' y2='{tick_y:.2f}'/>"
            f"<text class='axis-tick' x='{width - 7:.0f}' y='{tick_y + 4:.2f}' "
            f"text-anchor='end'>{_price_plain(tick)}</text>"
        )
    parts.append(
        f"<line class='axis-line' x1='{plot_right}' x2='{plot_right}' "
        f"y1='{top}' y2='{height - bottom}'/>"
    )

    for index, candle in enumerate(candles):
        open_price = _float(candle.get("open"))
        high_price = _float(candle.get("high"))
        low_price = _float(candle.get("low"))
        close_price = _float(candle.get("close"))
        if not all(
            math.isfinite(value)
            for value in (open_price, high_price, low_price, close_price)
        ):
            continue
        x = left + step * index + step / 2
        css = "up" if close_price >= open_price else "down"
        open_y = y(open_price)
        close_y = y(close_price)
        parts.append(
            f"<line class='wick {css}' x1='{x:.2f}' x2='{x:.2f}' "
            f"y1='{y(high_price):.2f}' y2='{y(low_price):.2f}'/>"
        )
        parts.append(
            f"<rect class='body {css}' x='{x - body_width / 2:.2f}' "
            f"y='{min(open_y, close_y):.2f}' width='{body_width:.2f}' "
            f"height='{max(1.4, abs(open_y - close_y)):.2f}' rx='1'/>"
        )

    overlays = (
        ("TP", target, "target"),
        ("ВХОД", entry, "entry"),
        ("СТОП", stop, "stop"),
    )
    tag_x = plot_right + 7
    tag_width = right - 14
    for label, value, css in overlays:
        if not math.isfinite(value) or value <= 0:
            continue
        line_y = y(value)
        parts.append(
            f"<line class='trade-level {css}' x1='{left}' x2='{plot_right}' "
            f"y1='{line_y:.2f}' y2='{line_y:.2f}'/>"
            f"<rect class='trade-tag {css}' x='{tag_x:.2f}' y='{line_y - 10:.2f}' "
            f"width='{tag_width:.2f}' height='20' rx='5'/>"
            f"<text class='trade-tag-text {css}' x='{tag_x + 6:.2f}' "
            f"y='{line_y + 4:.2f}'>{label} {_price_plain(value)}</text>"
        )

    custom = _mapping(_mapping(candidate.get("market")).get("custom"))
    rr = _float(plan.get("first_target_rr"), _float(custom.get("target_rr")))
    if math.isfinite(rr):
        parts.append(
            "<rect class='rr-tag-bg' x='18' y='18' width='72' height='22' rx='7'/>"
            f"<text class='rr-tag-text' x='28' y='33'>RR {rr:.2f}R</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def _signal_card(candidate: Mapping[str, Any], index: int) -> str:
    result = _BASE_V124_CARD(candidate, index)
    if not _is_v126_crypto(candidate):
        return result
    return result.replace(
        "<article class='signal-card'",
        "<article class='signal-card swing-price-card' data-price-scale='true'",
        1,
    )


def _signal_dialog(
    candidate: Mapping[str, Any], decision: Mapping[str, Any], index: int
) -> str:
    result = _BASE_V124_DIALOG(candidate, decision, index)
    if not _is_v126_crypto(candidate):
        return result

    result = result.replace("<h3>Лесенка входов</h3>", "<h3>Точка входа</h3>", 1)
    reasons = _decision_reasons(candidate)
    if reasons:
        reason_html = "".join(f"<li>{html.escape(item)}</li>" for item in reasons)
        result = result.replace(
            "<li>Причины будут добавлены после следующей проверки gate.</li>",
            reason_html,
            1,
        )
    result = result.replace(
        "<small>Рыночный контекст</small><h3>SMC, Fibonacci, объёмы и волатильность</h3>",
        "<small>Карта сделки</small><h3>H1 Swing и подтверждение M5</h3>",
        1,
    )
    return result


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
    targets = _sequence(plan.get("targets"))
    target = targets[0] if targets else custom.get("h1_target")

    core = f"""
<div class='market-grid core-market-grid'>
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
</div>
"""
    extra = f"""
<details class='extra-context'>
  <summary><span>Дополнительный контекст</span><small>FVG, OTE, funding, OI и стакан</small></summary>
  <div class='extra-context-body'>{base_html}</div>
</details>
"""
    return core + extra


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


EXTRA_CSS = r"""
<style>
.swing-price-card .chart{height:218px;padding:0 8px 8px}
.price-scale-chart .price-chart-bg{fill:#101624}
.price-scale-chart .axis-grid{stroke:#2a354c;stroke-width:.8;stroke-dasharray:3 5}
.price-scale-chart .axis-line{stroke:#49556f;stroke-width:1}
.price-scale-chart .axis-tick{fill:#8f9bb2;font-size:9px;font-weight:650}
.price-scale-chart .reward-zone{fill:#2ed6a612}
.price-scale-chart .risk-zone{fill:#ff667814}
.price-scale-chart .trade-level{stroke-width:1.7}
.price-scale-chart .trade-level.entry{stroke:#a18dff}
.price-scale-chart .trade-level.stop{stroke:#ff6678}
.price-scale-chart .trade-level.target{stroke:#2ed6a6}
.price-scale-chart .trade-tag.entry{fill:#6552c9}
.price-scale-chart .trade-tag.stop{fill:#9f3445}
.price-scale-chart .trade-tag.target{fill:#167d65}
.price-scale-chart .trade-tag-text{fill:#fff;font-size:9px;font-weight:850}
.price-scale-chart .rr-tag-bg{fill:#282145;stroke:#8068ff88}
.price-scale-chart .rr-tag-text{fill:#cabfff;font-size:10px;font-weight:850}
.core-market-grid{grid-template-columns:repeat(3,1fr)}
.extra-context{margin-top:12px;border:1px solid var(--line);border-radius:14px;background:#0d1320;overflow:hidden}
.extra-context summary{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;color:#c7d0df;font-weight:800}
.extra-context summary small{color:var(--muted);font-weight:500}
.extra-context[open] summary{border-bottom:1px solid var(--line);background:#121a2a}
.extra-context-body{padding:12px}
.extra-context-body .market-grid{grid-template-columns:repeat(3,1fr)}
@media(max-width:1100px){.core-market-grid,.extra-context-body .market-grid{grid-template-columns:1fr 1fr}}
@media(max-width:780px){.swing-price-card .chart{height:205px}.core-market-grid,.extra-context-body .market-grid{grid-template-columns:1fr}.extra-context summary{align-items:flex-start;flex-direction:column}}
</style>
"""


def render(data: Mapping[str, Any]) -> str:
    original_market = previous._market_html
    original_card = v124._signal_card
    original_dialog = v124._signal_dialog
    original_candle = base.candle_svg
    try:
        previous._market_html = _market_html
        v124._signal_card = _signal_card
        v124._signal_dialog = _signal_dialog
        base.candle_svg = _price_scale_svg
        page = _BASE_RENDER(data)
    finally:
        previous._market_html = original_market
        v124._signal_card = original_card
        v124._signal_dialog = original_dialog
        base.candle_svg = original_candle

    if "TradeMind Product UI v1.25.2" in page:
        page = page.replace(
            "TradeMind Product UI v1.25.2",
            "TradeMind Product UI v1.26.1",
        )
    elif "TradeMind Product UI v1.26" in page:
        page = page.replace(
            "TradeMind Product UI v1.26",
            "TradeMind Product UI v1.26.1",
        )
    page = page.replace(
        "Движок проверяет Forex и нативную структуру Crypto, затем допускает только "
        "статистически подтверждённые сетапы.",
        "Crypto: направление H1, veto M15 и закрытие M5 за последним локальным "
        "экстремумом на подтверждённом объёме.",
    )
    page = page.replace("</head>", EXTRA_CSS + "</head>", 1)
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
    status = base.read_json(status_path)
    status = dict(status)
    status.update(
        {
            "schema_version": VERSION,
            "crypto_setup_family": SETUP_FAMILY,
            "crypto_decision_chain": (
                "H1_DIRECTION>M15_VETO>M5_LAST_EXTREMUM_CLOSE_BREAK>"
                "M5_VOLUME_DELTA>H1_TARGET_SPACE"
            ),
            "crypto_price_scale_lines": True,
            "crypto_secondary_context_collapsed": True,
            "crypto_position_sizing_available": False,
            "read_only": True,
        }
    )
    base.atomic_write(
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
        description="TradeMind Product UI v1.26.1 H1 Swing + price scale lines"
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
        print(f"TradeMind Product UI v1.26.1 failed: {exc}")
        return 1

    summary = base._mapping(payload.get("summary"))
    print("TradeMind Product UI v1.26.1")
    print("Price scale lines. Read-only. Orders OFF. Publication OFF.")
    print(f"Forex displayed: {base.integer(summary.get('forex_displayed'))}")
    print(f"Crypto displayed: {base.integer(summary.get('crypto_displayed'))}")
    print("Crypto position sizing: NOT CALCULATED")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
