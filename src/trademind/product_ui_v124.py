"""TradeMind Product UI v1.24 with unified Forex and Crypto signal feed.

The module merges the existing FX live runtime with immutable candidates built
from the local Bybit shadow journal. It is a presentation layer only: it does
not call exchanges, recalculate source decisions, publish signals or send
orders.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind import product_ui_v1231 as base
from trademind import product_ui_v1235 as previous

VERSION = "1.24.0"
_BASE_SIGNAL_CARD = base._signal_card
_BASE_SIGNAL_DIALOG = base._signal_dialog
_BASE_MARKET_HTML = previous._market_html


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, Mapping) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                rows.append(dict(payload))
    return rows


def _timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_crypto_candles(
    path: Path,
    symbols: set[str],
    limit: int,
) -> dict[str, list[dict[str, float]]]:
    output: dict[str, list[dict[str, float]]] = {symbol: [] for symbol in symbols}
    if not path.is_file() or not symbols:
        return output
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol not in output:
                continue
            try:
                candle = {
                    "time": float(row.get("start_ms") or 0) / 1000.0,
                    "open": float(row.get("open") or 0),
                    "high": float(row.get("high") or 0),
                    "low": float(row.get("low") or 0),
                    "close": float(row.get("close") or 0),
                }
            except (TypeError, ValueError):
                continue
            if candle["time"] <= 0 or not all(
                math.isfinite(candle[key]) and candle[key] > 0
                for key in ("open", "high", "low", "close")
            ):
                continue
            output[symbol].append(candle)
    for symbol, rows in output.items():
        rows.sort(key=lambda item: item["time"])
        output[symbol] = rows[-limit:]
    return output


def _evaluations(root: Path) -> dict[str, Mapping[str, Any]]:
    payload = _read_json(root / "factory" / "evaluations.json")
    rows = payload.get("evaluations", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    return {
        str(item.get("signal_id") or ""): item
        for item in rows
        if isinstance(item, Mapping) and str(item.get("signal_id") or "")
    }


def _outcomes(root: Path) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("signal_id") or ""): item
        for item in _read_jsonl(root / "outcomes.jsonl")
        if str(item.get("signal_id") or "")
    }


def _entry_weights(plan: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(plan)
    entries = plan.get("entries", [])
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        normalized["entries"] = [
            {
                **dict(item),
                "weight": item.get("allocation"),
            }
            for item in entries
            if isinstance(item, Mapping)
        ]
    return normalized


def crypto_candidate_to_ui(
    candidate: Mapping[str, Any],
    evaluation: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    candles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evaluation = evaluation or {}
    outcome = outcome or {}
    raw_outcome = str(outcome.get("outcome") or "").upper()
    if raw_outcome in {"WIN", "LOSS", "FLAT"}:
        state = f"OUTCOME_{raw_outcome}"
    else:
        state = str(evaluation.get("state") or "SHADOW_ONLY").upper()

    plan = candidate.get("plan", {})
    plan_map = _entry_weights(plan if isinstance(plan, Mapping) else {})
    market = candidate.get("market_features", {})
    factor_scores = candidate.get("factor_scores", {})
    source_quality = candidate.get("source_quality_score")
    quality = evaluation.get("quality_score", source_quality)
    reasons = evaluation.get("reasons", [])
    if not reasons and raw_outcome:
        reasons = [
            f"Теневой исход: {raw_outcome}; результат {outcome.get('net_r', 0)}R"
        ]

    return {
        "signal_id": candidate.get("signal_id"),
        "created_at": candidate.get("created_at"),
        "observed_at": candidate.get("observed_at"),
        "symbol": candidate.get("symbol"),
        "timeframe": candidate.get("timeframe") or "M5",
        "action": plan_map.get("action"),
        "setup_family": candidate.get("setup_family"),
        "setup_family_label": "Крипто MTF Flow",
        "scenario": candidate.get("scenario"),
        "plan": plan_map,
        "market": market if isinstance(market, Mapping) else {},
        "factor_scores": factor_scores if isinstance(factor_scores, Mapping) else {},
        "factor_reasons": candidate.get("factor_reasons", {}),
        "state": state,
        "quality_score": quality,
        "historical_sample": evaluation.get("historical_sample", 0),
        "conservative_probability": evaluation.get("conservative_probability"),
        "expected_value_r": evaluation.get("expected_value_r"),
        "checks": evaluation.get("checks", {}),
        "reasons": reasons,
        "candles": list(candles),
        "asset_class": "CRYPTO",
        "venue": "BYBIT",
        "source_gate_status": candidate.get("source_gate_status"),
        "source_quality_score": source_quality,
    }


def load_crypto_feed(
    crypto_root: Path,
    bars_path: Path,
    *,
    limit: int,
    candle_limit: int,
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    candidates = _read_jsonl(crypto_root / "candidates.jsonl")
    candidates.sort(key=lambda item: _timestamp(item.get("created_at")), reverse=True)
    selected = candidates[:limit]
    symbols = {str(item.get("symbol") or "").upper() for item in selected}
    candles = load_crypto_candles(bars_path, symbols, candle_limit)
    evaluations = _evaluations(crypto_root)
    outcomes = _outcomes(crypto_root)
    rows = [
        crypto_candidate_to_ui(
            item,
            evaluations.get(str(item.get("signal_id") or "")),
            outcomes.get(str(item.get("signal_id") or "")),
            candles.get(str(item.get("symbol") or "").upper(), []),
        )
        for item in selected
    ]
    adapter_status = _read_json(crypto_root / "status.json")
    factory_status = _read_json(crypto_root / "factory" / "status.json")
    status = {
        "state": adapter_status.get("state", "WAITING_SOURCE"),
        "updated_at": adapter_status.get("updated_at"),
        "candidates": len(candidates),
        "displayed": len(rows),
        "outcomes": len(outcomes),
        "factory_state": factory_status.get("state", "WAITING_NO_FRESH_CANDIDATES"),
        "factory_fresh": factory_status.get("fresh_candidates", 0),
        "publishable": factory_status.get("publishable", 0),
        "bars_source": str(bars_path),
        "read_only": True,
    }
    return rows, status


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
    payload = previous.build_payload(fx_source, canonical, fx_limit, candle_limit)
    fx_candidates = [
        {**dict(item), "asset_class": "FOREX", "venue": "MT5"}
        for item in payload.get("candidates", [])
        if isinstance(item, Mapping)
    ]
    crypto_candidates, crypto_status = load_crypto_feed(
        crypto_root,
        bars_path,
        limit=crypto_limit,
        candle_limit=candle_limit,
    )
    combined = base.sort_candidates([*fx_candidates, *crypto_candidates])
    payload["schema_version"] = VERSION
    payload["candidates"] = combined
    payload["crypto"] = crypto_status

    summary = dict(payload.get("summary", {}))
    summary["archive_candidates"] = base.integer(summary.get("archive_candidates")) + base.integer(
        crypto_status.get("candidates")
    )
    summary["displayed_candidates"] = len(combined)
    summary["active_candidates"] = sum(
        not str(item.get("state") or "").upper().startswith("OUTCOME_")
        for item in combined
    )
    summary["buy"] = sum(str(item.get("action") or "").upper() == "BUY" for item in combined)
    summary["sell"] = sum(str(item.get("action") or "").upper() == "SELL" for item in combined)
    summary["completed_outcomes"] = base.integer(summary.get("completed_outcomes")) + base.integer(
        crypto_status.get("outcomes")
    )
    summary["fresh_factory"] = base.integer(summary.get("fresh_factory")) + base.integer(
        crypto_status.get("factory_fresh")
    )
    summary["publishable"] = base.integer(summary.get("publishable")) + base.integer(
        crypto_status.get("publishable")
    )
    summary["forex_displayed"] = len(fx_candidates)
    summary["crypto_displayed"] = len(crypto_candidates)
    payload["summary"] = summary
    return payload


def _crypto_market_html(candidate: Mapping[str, Any]) -> str:
    market = base._mapping(candidate.get("market"))
    structure = base._mapping(market.get("structure"))
    volume = base._mapping(market.get("volume"))
    volatility = base._mapping(market.get("volatility"))
    execution = base._mapping(market.get("execution"))
    sentiment = base._mapping(market.get("sentiment"))
    custom = base._mapping(market.get("custom"))
    confirmation = base._mapping(market.get("confirmation"))
    return f"""
<div class='market-grid'>
  <section><small>Multi-timeframe</small><h4>Контекст H1 / M15 / M5</h4>
    <p><span>Уклон H1</span><b>{previous._text_value(structure.get('swing_bias'))}</b></p>
    <p><span>Уклон M15</span><b>{previous._text_value(structure.get('internal_bias'))}</b></p>
    <p><span>Источник</span><b>Bybit Linear</b></p>
    <p><span>Статус исходного фильтра</span><b>{previous._text_value(custom.get('source_gate_status'))}</b></p>
  </section>
  <section><small>Order Flow</small><h4>Delta и стакан</h4>
    <p><span>Delta M15</span><b>{previous._metric_value(volume.get('m15_delta_turnover'), 0, trim=True)}</b></p>
    <p><span>Delta M5</span><b>{previous._metric_value(volume.get('m5_delta_turnover'), 0, trim=True)}</b></p>
    <p><span>Дисбаланс стакана M15</span><b>{previous._metric_value(custom.get('m15_book_imbalance_10'), 3, trim=True)}</b></p>
    <p><span>Дисбаланс стакана M5</span><b>{previous._metric_value(custom.get('m5_book_imbalance_10'), 3, trim=True)}</b></p>
    <p><span>Сделок в M5</span><b>{previous._metric_value(volume.get('m5_trade_count'), 0, trim=True)}</b></p>
  </section>
  <section><small>Деривативы</small><h4>Funding, basis и OI</h4>
    <p><span>Funding</span><b>{previous._fraction_percent(sentiment.get('funding_rate'), 4)}</b></p>
    <p><span>Basis</span><b>{previous._metric_value(sentiment.get('basis_bps'), 2, ' б.п.', trim=True)}</b></p>
    <p><span>Изменение OI H1</span><b>{previous._fraction_percent(sentiment.get('h1_open_interest_change_pct'), 2)}</b></p>
    <p><span>Изменение OI M15</span><b>{previous._fraction_percent(sentiment.get('m15_open_interest_change_pct'), 2)}</b></p>
  </section>
  <section><small>Исполнение</small><h4>Спред и риск геометрии</h4>
    <p><span>Спред M5</span><b>{previous._metric_value(execution.get('spread_bps'), 2, ' б.п.', trim=True)}</b></p>
    <p><span>Спред к риску</span><b>{previous._fraction_percent(volatility.get('spread_cost_atr'), 2)}</b></p>
    <p><span>Дистанция стопа</span><b>{previous._fraction_percent(volatility.get('risk_distance_pct'), 2)}</b></p>
    <p><span>Компоненты источника</span><b>{previous._text_value(confirmation.get('source_components'))}</b></p>
  </section>
</div>
"""


def _market_html(candidate: Mapping[str, Any]) -> str:
    if str(candidate.get("asset_class") or "").upper() == "CRYPTO":
        return _crypto_market_html(candidate)
    return _BASE_MARKET_HTML(candidate)


def _signal_card(candidate: Mapping[str, Any], index: int) -> str:
    result = _BASE_SIGNAL_CARD(candidate, index)
    asset = str(candidate.get("asset_class") or "FOREX").upper()
    timeframe = html.escape(str(candidate.get("timeframe") or "M5").upper())
    symbol = html.escape(str(candidate.get("symbol") or ""))
    result = result.replace(
        "<article class='signal-card'",
        f"<article class='signal-card' data-market='{asset}'",
        1,
    )
    result = result.replace(
        f"<b>{symbol}</b><small>M5</small>",
        f"<b>{symbol}</b><small>{timeframe} · {asset}</small>",
        1,
    )
    return result


def _signal_dialog(
    candidate: Mapping[str, Any], decision: Mapping[str, Any], index: int
) -> str:
    result = _BASE_SIGNAL_DIALOG(candidate, decision, index)
    asset = str(candidate.get("asset_class") or "FOREX").upper()
    timeframe = html.escape(str(candidate.get("timeframe") or "M5").upper())
    symbol = html.escape(str(candidate.get("symbol") or ""))
    result = result.replace(
        f"<h2>{symbol} <small>M5</small></h2>",
        f"<h2>{symbol} <small>{timeframe} · {asset}</small></h2>",
        1,
    )
    return result


MARKET_FILTER_SCRIPT = r"""
<script>
(() => {
  const buttons = [...document.querySelectorAll('[data-market-filter]')];
  const cards = [...document.querySelectorAll('.signal-card[data-market]')];
  const apply = (market) => {
    cards.forEach(card => card.classList.toggle('market-hidden', market !== 'ALL' && card.dataset.market !== market));
    buttons.forEach(button => button.classList.toggle('active', button.dataset.marketFilter === market));
  };
  buttons.forEach(button => button.addEventListener('click', () => apply(button.dataset.marketFilter)));
  apply('ALL');
})();
</script>
"""


def render(data: Mapping[str, Any]) -> str:
    original_market = previous._market_html
    original_card = base._signal_card
    original_dialog = base._signal_dialog
    try:
        previous._market_html = _market_html
        base._signal_card = _signal_card
        base._signal_dialog = _signal_dialog
        page = previous.render(data)
    finally:
        previous._market_html = original_market
        base._signal_card = original_card
        base._signal_dialog = original_dialog

    crypto = base._mapping(data.get("crypto"))
    summary = base._mapping(data.get("summary"))
    market_strip = (
        "<div class='market-strip'>"
        f"<span><b>FOREX</b> {base.integer(summary.get('forex_displayed'))} в ленте</span>"
        f"<span><b>CRYPTO · BYBIT</b> {base.integer(summary.get('crypto_displayed'))} в ленте</span>"
        f"<span><b>Crypto Factory</b> {html.escape(base.human_state(crypto.get('factory_state')))}</span>"
        "</div>"
    )
    page = page.replace("<div class='metric-grid'>", market_strip + "<div class='metric-grid'>", 1)
    page = page.replace(
        "<div class='filters'><input id='search' placeholder='Инструмент или сценарий'>",
        "<div class='filters'><input id='search' placeholder='Инструмент или сценарий'>"
        "<button class='active' data-market-filter='ALL'>Все рынки</button>"
        "<button data-market-filter='FOREX'>Forex</button>"
        "<button data-market-filter='CRYPTO'>Crypto</button>",
        1,
    )
    extra_css = (
        "<style>.market-strip{display:flex;gap:10px;flex-wrap:wrap;margin:17px 0 0}"
        ".market-strip span{border:1px solid var(--line);border-radius:999px;background:var(--surface);"
        "padding:9px 13px;color:var(--muted);font-size:12px}.market-strip b{color:var(--text);margin-right:5px}"
        ".signal-card.market-hidden{display:none!important}</style>"
    )
    page = page.replace("</head>", extra_css + "</head>", 1)
    page = page.replace("</body>", MARKET_FILTER_SCRIPT + "</body>", 1)
    page = page.replace("TradeMind Product UI v1.23.5", "TradeMind Product UI v1.24")
    page = page.replace(
        "Движок фильтрует рынок и не показывает сырой шум как готовый сигнал.",
        "Движок фильтрует Forex и Crypto и не показывает сырой шум как готовый сигнал.",
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
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind Product UI v1.24 Forex + Crypto")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("data/live_signal_runtime_v1"),
    )
    parser.add_argument(
        "--crypto-root",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_24"),
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
        print(f"TradeMind Product UI v1.24 failed: {exc}")
        return 1

    summary = base._mapping(payload.get("summary"))
    print("TradeMind Product UI v1.24")
    print("Unified Forex + Crypto feed. Read-only. Orders OFF. Publication OFF.")
    print(f"Forex displayed: {base.integer(summary.get('forex_displayed'))}")
    print(f"Crypto displayed: {base.integer(summary.get('crypto_displayed'))}")
    print(f"Product UI: {index}")
    if args.open and hasattr(os, "startfile"):
        os.startfile(index)  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
