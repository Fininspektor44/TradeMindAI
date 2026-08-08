"""Conservative historical shadow evaluation for TradeMind v1.33.1 crypto candidates.

This module is diagnostic only. It replays already-recorded Bybit M5 bars after
candidate timestamps, uses the existing TradeMind conservative shadow evaluator
(stop wins when stop and target touch the same bar), includes a configurable
cost in R, and never places orders or publishes signals.

Because v1.33.1 thresholds were tuned after inspecting historical rejection
patterns, these results are in-sample diagnostics, not forward validation.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trademind.crypto_h1_swing_filter import read_flow_bars
from trademind.signal_intelligence import candidate_from_dict
from trademind.signal_shadow import Bar, ShadowOutcome, evaluate_shadow_candidate

VERSION = "1.33.1"


def _read_candidates(path: Path):
    if not path.is_file():
        raise ValueError(f"candidate file not found: {path}")
    rows = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                candidate = candidate_from_dict(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"candidate line {line_number}: {exc}") from exc
            if candidate.signal_id in seen:
                continue
            seen.add(candidate.signal_id)
            rows.append(candidate)
    rows.sort(key=lambda item: item.observed_at)
    return rows


def _load_bars(path: Path) -> list[Bar]:
    grouped = read_flow_bars(path)
    bars: list[Bar] = []
    for symbol, rows in grouped.items():
        for row in rows:
            bars.append(
                Bar(
                    time=datetime.fromtimestamp(row.end_ms / 1000.0, tz=timezone.utc),
                    symbol=symbol,
                    timeframe="M5",
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                )
            )
    bars.sort(key=lambda item: (item.symbol, item.time))
    return bars


def _profit_factor(outcomes: Sequence[ShadowOutcome]) -> float:
    gross_win = sum(max(0.0, item.net_r) for item in outcomes)
    gross_loss = abs(sum(min(0.0, item.net_r) for item in outcomes))
    if gross_loss <= 1e-12:
        return math.inf if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _max_drawdown_r(outcomes: Sequence[ShadowOutcome]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for item in sorted(outcomes, key=lambda value: value.completed_at):
        equity += item.net_r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_text(
        path,
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


@dataclass(frozen=True, slots=True)
class BacktestRun:
    total_candidates: int
    completed: int
    pending: int
    wins: int
    losses: int
    flats: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    total_r: float
    max_drawdown_r: float
    output_dir: Path


def run_backtest(
    candidates_path: Path,
    bars_path: Path,
    output_dir: Path,
    *,
    max_bars: int = 72,
    cost_r: float = 0.04,
) -> BacktestRun:
    if max_bars < 1:
        raise ValueError("max_bars must be positive")
    if cost_r < 0:
        raise ValueError("cost_r cannot be negative")

    candidates = _read_candidates(candidates_path)
    bars = _load_bars(bars_path)
    outcomes: list[ShadowOutcome] = []
    pending = 0
    for candidate in candidates:
        outcome = evaluate_shadow_candidate(
            candidate,
            bars,
            max_bars=max_bars,
            target_index=0,
            cost_r=cost_r,
        )
        if outcome is None:
            pending += 1
        else:
            outcomes.append(outcome)

    wins = sum(item.outcome == "WIN" for item in outcomes)
    losses = sum(item.outcome == "LOSS" for item in outcomes)
    flats = sum(item.outcome == "FLAT" for item in outcomes)
    completed = len(outcomes)
    win_rate = wins / completed if completed else 0.0
    total_r = sum(item.net_r for item in outcomes)
    expectancy = total_r / completed if completed else 0.0
    profit_factor = _profit_factor(outcomes)
    max_drawdown = _max_drawdown_r(outcomes)

    root = output_dir.expanduser().resolve()
    outcome_lines = [
        json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True)
        for item in sorted(outcomes, key=lambda value: value.completed_at)
    ]
    _atomic_text(root / "outcomes.jsonl", "\n".join(outcome_lines) + ("\n" if outcome_lines else ""))

    by_symbol: dict[str, dict[str, float]] = {}
    signal_symbol = {candidate.signal_id: candidate.symbol for candidate in candidates}
    for item in outcomes:
        symbol = signal_symbol.get(item.signal_id, "UNKNOWN")
        bucket = by_symbol.setdefault(symbol, {"n": 0, "wins": 0, "net_r": 0.0})
        bucket["n"] += 1
        bucket["wins"] += int(item.outcome == "WIN")
        bucket["net_r"] += item.net_r
    symbol_rows = [
        {
            "symbol": symbol,
            "n": int(values["n"]),
            "wins": int(values["wins"]),
            "win_rate": values["wins"] / values["n"] if values["n"] else 0.0,
            "net_r": values["net_r"],
            "ev_r": values["net_r"] / values["n"] if values["n"] else 0.0,
        }
        for symbol, values in sorted(by_symbol.items())
    ]

    status = {
        "schema_version": VERSION,
        "state": "OK",
        "diagnostic_type": "HISTORICAL_IN_SAMPLE_SHADOW",
        "candidates": len(candidates),
        "completed": completed,
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": win_rate,
        "profit_factor_r": profit_factor if math.isfinite(profit_factor) else "INF",
        "expectancy_r": expectancy,
        "total_r": total_r,
        "max_drawdown_r": max_drawdown,
        "max_bars": max_bars,
        "cost_r": cost_r,
        "conservative_same_bar_rule": "STOP_WINS",
        "symbols": symbol_rows,
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "exchange_api_called": False,
            "source_files_modified": False,
        },
        "warning": (
            "Thresholds were adjusted after historical rejection inspection. "
            "Treat this as in-sample diagnostics, not forward proof."
        ),
    }
    _atomic_json(root / "status.json", status)

    return BacktestRun(
        total_candidates=len(candidates),
        completed=completed,
        pending=pending,
        wins=wins,
        losses=losses,
        flats=flats,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy_r=expectancy,
        total_r=total_r,
        max_drawdown_r=max_drawdown,
        output_dir=root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.33.1 historical shadow evaluator")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_33_1_shadow/candidates.jsonl"),
    )
    parser.add_argument(
        "--bars",
        type=Path,
        default=Path("data/bybit_v1_9/bybit_bars.csv"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/crypto_signal_intelligence_v1_33_1_backtest"),
    )
    parser.add_argument("--max-bars", type=int, default=72)
    parser.add_argument("--cost-r", type=float, default=0.04)
    args = parser.parse_args(argv)
    try:
        result = run_backtest(
            args.candidates.expanduser().resolve(),
            args.bars.expanduser().resolve(),
            args.output_dir,
            max_bars=args.max_bars,
            cost_r=args.cost_r,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"v1.33.1 shadow backtest failed: {exc}")
        return 1

    pf = "INF" if math.isinf(result.profit_factor) else f"{result.profit_factor:.3f}"
    print("TradeMind v1.33.1 HISTORICAL SHADOW DIAGNOSTIC")
    print("Conservative rule: if stop and target touch in one M5 bar, STOP wins.")
    print(f"Candidates: {result.total_candidates}")
    print(f"Completed: {result.completed}")
    print(f"Pending: {result.pending}")
    print(f"Wins/Losses/Flats: {result.wins}/{result.losses}/{result.flats}")
    print(f"Win rate: {100.0 * result.win_rate:.2f}%")
    print(f"Profit factor: {pf}")
    print(f"Expectancy: {result.expectancy_r:.4f} R/trade")
    print(f"Total: {result.total_r:.2f} R")
    print(f"Max drawdown: {result.max_drawdown_r:.2f} R")
    print(f"Output: {result.output_dir}")
    print("WARNING: in-sample diagnostic, not forward validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
