"""TradeMind v1.34 chronological walk-forward diagnostic for v1.33.1 crypto shadow results.

Diagnostic only. It does not alter candidates, place orders, publish signals, or
call an exchange. The purpose is to avoid cherry-picking symbols from the same
sample used to judge them.

The completed v1.33.1 shadow outcomes are joined to their candidates and split
chronologically into an earlier training segment and a later untouched test
segment. Symbol eligibility is learned from TRAIN only, then frozen and scored
on TEST.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} line {line_number}: invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"{path} line {line_number}: root must be object")
            rows.append(dict(payload))
    return rows


def _profit_factor(rows: Sequence[Mapping[str, Any]]) -> float:
    gross_win = sum(max(0.0, float(row.get("net_r") or 0.0)) for row in rows)
    gross_loss = abs(sum(min(0.0, float(row.get("net_r") or 0.0)) for row in rows))
    if gross_loss <= 1e-12:
        return math.inf if gross_win > 0 else 0.0
    return gross_win / gross_loss


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(str(row.get("outcome") or "").upper() == "WIN" for row in rows)
    losses = sum(str(row.get("outcome") or "").upper() == "LOSS" for row in rows)
    flats = sum(str(row.get("outcome") or "").upper() == "FLAT" for row in rows)
    total_r = sum(float(row.get("net_r") or 0.0) for row in rows)
    pf = _profit_factor(rows)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": wins / n if n else 0.0,
        "ev_r": total_r / n if n else 0.0,
        "net_r": total_r,
        "profit_factor": pf if math.isfinite(pf) else "INF",
    }


def _by_symbol(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("symbol") or "UNKNOWN").upper(), []).append(row)
    result = []
    for symbol, bucket in buckets.items():
        result.append({"symbol": symbol, **_stats(bucket)})
    result.sort(key=lambda item: (-int(item["n"]), item["symbol"]))
    return result


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True, slots=True)
class WalkForwardRun:
    completed: int
    train_n: int
    test_n: int
    selected_symbols: tuple[str, ...]
    selected_test_n: int
    selected_test_ev_r: float
    selected_test_pf: float | str
    output_dir: Path


def run_walkforward(
    candidates_path: Path,
    outcomes_path: Path,
    output_dir: Path,
    *,
    train_fraction: float = 0.70,
    min_train_symbol_n: int = 5,
    min_train_ev_r: float = 0.0,
    min_train_pf: float = 1.0,
) -> WalkForwardRun:
    if not 0.5 <= train_fraction < 0.9:
        raise ValueError("train_fraction must be in [0.5, 0.9)")
    if min_train_symbol_n < 1:
        raise ValueError("min_train_symbol_n must be positive")

    candidates = _read_jsonl(candidates_path)
    outcomes = _read_jsonl(outcomes_path)
    candidate_by_id = {str(row.get("signal_id") or ""): row for row in candidates if str(row.get("signal_id") or "")}

    joined: list[dict[str, Any]] = []
    for outcome in outcomes:
        signal_id = str(outcome.get("signal_id") or "")
        candidate = candidate_by_id.get(signal_id)
        if candidate is None:
            continue
        joined.append(
            {
                **outcome,
                "symbol": str(candidate.get("symbol") or "UNKNOWN").upper(),
                "action": str((candidate.get("plan") or {}).get("action") or "").upper(),
                "observed_at": str(candidate.get("observed_at") or ""),
                "source_quality_score": float(candidate.get("source_quality_score") or 0.0),
                "source_gate_status": str(candidate.get("source_gate_status") or ""),
            }
        )

    joined.sort(key=lambda row: (str(row.get("observed_at") or ""), str(row.get("signal_id") or "")))
    if len(joined) < 20:
        raise ValueError("too few completed joined outcomes for walk-forward diagnostic")

    split = max(1, min(len(joined) - 1, int(len(joined) * train_fraction)))
    train = joined[:split]
    test = joined[split:]
    train_symbols = _by_symbol(train)

    selected = tuple(
        row["symbol"]
        for row in train_symbols
        if int(row["n"]) >= min_train_symbol_n
        and float(row["ev_r"]) > min_train_ev_r
        and (row["profit_factor"] == "INF" or float(row["profit_factor"]) > min_train_pf)
    )
    selected_set = set(selected)
    selected_train = [row for row in train if row["symbol"] in selected_set]
    selected_test = [row for row in test if row["symbol"] in selected_set]

    test_all_stats = _stats(test)
    selected_train_stats = _stats(selected_train)
    selected_test_stats = _stats(selected_test)
    status = {
        "schema_version": "1.34.0",
        "state": "OK",
        "diagnostic_type": "CHRONOLOGICAL_WALK_FORWARD_SYMBOL_STABILITY",
        "completed_joined": len(joined),
        "train_fraction": train_fraction,
        "train_n": len(train),
        "test_n": len(test),
        "selection_rule": {
            "min_train_symbol_n": min_train_symbol_n,
            "min_train_ev_r_strictly_greater_than": min_train_ev_r,
            "min_train_profit_factor_strictly_greater_than": min_train_pf,
        },
        "selected_symbols": list(selected),
        "train_all": _stats(train),
        "test_all": test_all_stats,
        "selected_train": selected_train_stats,
        "selected_test": selected_test_stats,
        "train_by_symbol": train_symbols,
        "test_by_symbol": _by_symbol(test),
        "warning": (
            "Symbol selection is learned on TRAIN only and evaluated unchanged on TEST. "
            "This is still historical validation, not forward proof. Small test samples are not sufficient for deployment."
        ),
        "safety": {
            "read_only": True,
            "orders_enabled": False,
            "publication_enabled": False,
            "exchange_api_called": False,
            "source_files_modified": False,
        },
    }
    root = output_dir.expanduser().resolve()
    _atomic_json(root / "status.json", status)

    return WalkForwardRun(
        completed=len(joined),
        train_n=len(train),
        test_n=len(test),
        selected_symbols=selected,
        selected_test_n=int(selected_test_stats["n"]),
        selected_test_ev_r=float(selected_test_stats["ev_r"]),
        selected_test_pf=selected_test_stats["profit_factor"],
        output_dir=root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeMind v1.34 chronological crypto walk-forward diagnostic")
    parser.add_argument("--candidates", type=Path, default=Path("data/crypto_signal_intelligence_v1_33_1_shadow/candidates.jsonl"))
    parser.add_argument("--outcomes", type=Path, default=Path("data/crypto_signal_intelligence_v1_33_1_backtest/outcomes.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/crypto_signal_intelligence_v1_34_walkforward"))
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-train-symbol-n", type=int, default=5)
    parser.add_argument("--min-train-ev-r", type=float, default=0.0)
    parser.add_argument("--min-train-pf", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        run = run_walkforward(
            args.candidates.expanduser().resolve(),
            args.outcomes.expanduser().resolve(),
            args.output_dir,
            train_fraction=args.train_fraction,
            min_train_symbol_n=args.min_train_symbol_n,
            min_train_ev_r=args.min_train_ev_r,
            min_train_pf=args.min_train_pf,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"v1.34 walk-forward failed: {exc}")
        return 1

    pf = run.selected_test_pf if isinstance(run.selected_test_pf, str) else f"{run.selected_test_pf:.3f}"
    print("TradeMind v1.34 CHRONOLOGICAL WALK-FORWARD")
    print(f"Completed joined: {run.completed}")
    print(f"Train/Test: {run.train_n}/{run.test_n}")
    print("Selected from TRAIN only: " + (", ".join(run.selected_symbols) if run.selected_symbols else "NONE"))
    print(f"Selected TEST trades: {run.selected_test_n}")
    print(f"Selected TEST EV: {run.selected_test_ev_r:.4f} R/trade")
    print(f"Selected TEST PF: {pf}")
    print(f"Output: {run.output_dir}")
    print("READ-ONLY. Historical validation only; forward proof still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
