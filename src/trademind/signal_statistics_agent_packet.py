"""Build a read-only research packet from signal statistics for AI-agent review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from trademind.signal_statistics_report import REPORT_SCHEMA_VERSION

AGENT_PACKET_SCHEMA_VERSION = "signal-statistics-agent-packet-v1"


def _eligible(pattern: Mapping[str, object]) -> bool:
    status = str(pattern.get("status", ""))
    if status not in {"RESEARCH_CANDIDATE", "VALIDATED"}:
        return False

    ci95 = pattern.get("mean_ci95")
    early = pattern.get("early")
    late = pattern.get("late")
    if not isinstance(ci95, list) or len(ci95) != 2:
        return False
    if not isinstance(early, dict) or not isinstance(late, dict):
        return False

    try:
        ci_low = float(ci95[0])
        profit_factor = float(pattern.get("profit_factor_atr", 0.0))
        avg_net_atr = float(pattern.get("avg_net_atr", 0.0))
        early_avg = float(early.get("avg_net_atr", 0.0))
        late_avg = float(late.get("avg_net_atr", 0.0))
    except (TypeError, ValueError):
        return False

    return (
        ci_low > 0.0
        and profit_factor > 1.0
        and avg_net_atr > 0.0
        and early_avg > 0.0
        and late_avg > 0.0
    )


def _rank_key(pattern: Mapping[str, object]) -> tuple[int, float, float, int]:
    status_rank = 1 if str(pattern.get("status")) == "VALIDATED" else 0
    ci95 = pattern.get("mean_ci95")
    ci_low = float(ci95[0]) if isinstance(ci95, list) and ci95 else float("-inf")
    profit_factor = float(pattern.get("profit_factor_atr", 0.0))
    trades = int(pattern.get("trades", 0))
    return status_rank, ci_low, profit_factor, trades


def build_agent_packet(
    report: Mapping[str, object],
    *,
    max_candidates: int = 10,
) -> dict[str, object]:
    """Return a conservative, machine-readable packet for research-only AI analysis."""
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("unsupported signal statistics report schema")
    if report.get("read_only") is not True or report.get("orders_enabled") is not False:
        raise ValueError("signal statistics report must be read-only with orders disabled")

    raw_patterns = report.get("patterns", [])
    if not isinstance(raw_patterns, list):
        raise ValueError("patterns must be a list")

    eligible = [item for item in raw_patterns if isinstance(item, dict) and _eligible(item)]
    eligible.sort(key=_rank_key, reverse=True)
    selected = eligible[:max_candidates]

    return {
        "schema_version": AGENT_PACKET_SCHEMA_VERSION,
        "source_report_schema_version": report.get("schema_version"),
        "source_generated_at": report.get("generated_at"),
        "read_only": True,
        "orders_enabled": False,
        "decision_scope": "research_hypotheses_only",
        "candidate_count": len(selected),
        "selection_policy": {
            "allowed_statuses": ["RESEARCH_CANDIDATE", "VALIDATED"],
            "require_ci95_lower_above_zero": True,
            "require_positive_early_and_late_avg_net_atr": True,
            "require_profit_factor_atr_above_one": True,
            "max_candidates": max_candidates,
        },
        "prohibited_actions": [
            "change_signal_weights",
            "change_signal_generation_logic",
            "enable_orders",
            "publish_or_sell_signals",
        ],
        "analysis_questions": [
            "Which market conditions strengthen or weaken each candidate?",
            "Does the effect persist across time, symbols, horizons, and market regimes?",
            "What falsifiable hypothesis should be tested next on untouched data?",
            "What evidence would cause this candidate to be rejected?",
        ],
        "candidates": selected,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a conservative read-only AI research packet from a signal statistics report"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-candidates", type=int, default=10)
    args = parser.parse_args(argv)

    if not args.report.is_file():
        parser.error(f"report not found: {args.report}")

    report = json.loads(args.report.read_text(encoding="utf-8"))
    packet = build_agent_packet(report, max_candidates=args.max_candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
