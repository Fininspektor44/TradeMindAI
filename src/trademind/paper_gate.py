"""Frozen out-of-sample paper-signal gate for TradeMind AI v1.3.

The gate reads the existing MT5 research journal, validates exact
symbol/pattern/action/horizon combinations only on rows before a fixed cutoff,
and writes matching rows after the cutoff to a deterministic paper journal.
It never opens, modifies or closes a MetaTrader order.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trademind.action_validation import (
    ActionPatternValidation,
    apply_benjamini_hochberg,
    collect_action_validations,
    feature_labels,
    non_overlapping_rows,
)

_PAPER_FIELDS = (
    "paper_signal_id",
    "generated_at",
    "rule_id",
    "tier",
    "source_signal_id",
    "signal_time",
    "symbol",
    "timeframe",
    "action",
    "label",
    "horizon",
    "entry_price",
    "score",
    "confidence",
    "spread_points",
    "spread_cost_atr",
    "training_status",
    "training_trades",
    "training_days",
    "training_win_rate",
    "training_pf_atr",
    "training_avg_net_atr",
    "training_early_avg_net_atr",
    "training_late_avg_net_atr",
    "training_max_drawdown_atr",
    "training_max_loss_streak",
    "training_ci_low",
    "training_ci_high",
    "training_p_value",
    "training_q_value",
    "exit_time",
    "net_move",
    "progress_atr",
    "mfe_atr",
    "mae_atr",
    "outcome",
)
_GATE_STATUS_FIELDS = (
    "generated_at",
    "rule_id",
    "tier",
    "symbol",
    "label",
    "action",
    "horizon",
    "enabled",
    "eligible",
    "status",
    "trades",
    "trading_days",
    "win_rate",
    "pf_atr",
    "avg_net_atr",
    "early_avg_net_atr",
    "late_avg_net_atr",
    "late_to_early_ratio",
    "max_drawdown_atr",
    "max_loss_streak",
    "ci_low",
    "ci_high",
    "p_value",
    "q_value",
    "reasons",
)


@dataclass(frozen=True)
class GateRule:
    rule_id: str
    symbol: str
    label: str
    action: str
    horizon: int
    tier: str
    enabled: bool = True
    allowed_statuses: tuple[str, ...] = ("RESEARCH_CANDIDATE", "VALIDATED")
    max_q_value: float = 1.0
    max_drawdown_atr: float = 25.0
    max_loss_streak: int = 10
    minimum_late_ratio: float = 0.20

    @property
    def key(self) -> tuple[str, str, str, int]:
        return self.symbol.upper(), self.label.upper(), self.action.upper(), self.horizon


@dataclass(frozen=True)
class GateConfig:
    version: str
    schema_version: str
    training_cutoff: datetime
    candidate_minimum: int
    research_minimum: int
    minimum_training_days: int
    fdr_alpha: float
    rules: tuple[GateRule, ...]


@dataclass(frozen=True)
class GateDecision:
    rule: GateRule
    validation: ActionPatternValidation | None
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class GateSummary:
    decisions: tuple[GateDecision, ...]
    paper_signals: int
    output_path: Path
    status_path: Path


def _as_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("training_cutoff must include a timezone offset")
    return result


def _tuple_of_strings(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("allowed_statuses must be a list of strings")
    return tuple(item.upper() for item in value)


def load_config(path: Path) -> GateConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("paper gate config must contain at least one rule")

    rules: list[GateRule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise ValueError("each paper gate rule must be an object")
        rule_id = str(raw["id"]).strip()
        if not rule_id or rule_id in seen:
            raise ValueError(f"duplicate or empty rule id: {rule_id!r}")
        seen.add(rule_id)
        rule = GateRule(
            rule_id=rule_id,
            symbol=str(raw["symbol"]).strip().upper(),
            label=str(raw["label"]).strip().upper(),
            action=str(raw["action"]).strip().upper(),
            horizon=int(raw["horizon"]),
            tier=str(raw.get("tier", "SHADOW_OOS")).strip().upper(),
            enabled=bool(raw.get("enabled", True)),
            allowed_statuses=_tuple_of_strings(
                raw.get("allowed_statuses"), ("RESEARCH_CANDIDATE", "VALIDATED")
            ),
            max_q_value=float(raw.get("max_q_value", 1.0)),
            max_drawdown_atr=float(raw.get("max_drawdown_atr", 25.0)),
            max_loss_streak=int(raw.get("max_loss_streak", 10)),
            minimum_late_ratio=float(raw.get("minimum_late_ratio", 0.20)),
        )
        if rule.action not in {"BUY", "SELL"}:
            raise ValueError(f"rule {rule_id}: action must be BUY or SELL")
        if rule.horizon < 1:
            raise ValueError(f"rule {rule_id}: horizon must be positive")
        if not 0 <= rule.max_q_value <= 1:
            raise ValueError(f"rule {rule_id}: max_q_value must be between 0 and 1")
        if rule.max_drawdown_atr <= 0 or rule.max_loss_streak < 1:
            raise ValueError(f"rule {rule_id}: risk limits must be positive")
        if not 0 <= rule.minimum_late_ratio <= 1:
            raise ValueError(f"rule {rule_id}: minimum_late_ratio must be between 0 and 1")
        rules.append(rule)

    config = GateConfig(
        version=str(payload.get("version", "1.3")),
        schema_version=str(payload.get("schema_version", "1.1")),
        training_cutoff=_as_datetime(str(payload["training_cutoff"])),
        candidate_minimum=int(payload.get("candidate_minimum", 30)),
        research_minimum=int(payload.get("research_minimum", 300)),
        minimum_training_days=int(payload.get("minimum_training_days", 10)),
        fdr_alpha=float(payload.get("fdr_alpha", 0.10)),
        rules=tuple(rules),
    )
    if config.candidate_minimum < 2:
        raise ValueError("candidate_minimum must be at least 2")
    if config.research_minimum < config.candidate_minimum:
        raise ValueError("research_minimum must be at least candidate_minimum")
    if config.minimum_training_days < 1:
        raise ValueError("minimum_training_days must be positive")
    if not 0 < config.fdr_alpha <= 1:
        raise ValueError("fdr_alpha must be between 0 and 1")
    return config


def _load_journal(path: Path, schema_version: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            dict(row)
            for row in csv.DictReader(handle)
            if str(row.get("schema_version", "")).strip() == schema_version
        ]


def _signal_time(row: dict[str, str]) -> datetime:
    return datetime.fromisoformat(row["signal_time"])


def _format(value: float) -> str:
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if math.isnan(value):
        return "nan"
    return f"{value:.12g}"


def _validation_map(
    training_rows: list[dict[str, str]], config: GateConfig
) -> dict[tuple[str, str, str, int], ActionPatternValidation]:
    symbols = sorted({rule.symbol.upper() for rule in config.rules})
    horizons = sorted({rule.horizon for rule in config.rules})
    maximum_drawdown = max(rule.max_drawdown_atr for rule in config.rules)
    maximum_streak = max(rule.max_loss_streak for rule in config.rules)
    minimum_late_ratio = min(rule.minimum_late_ratio for rule in config.rules)
    validations = collect_action_validations(
        training_rows,
        symbols,
        horizons,
        candidate_minimum=config.candidate_minimum,
        research_minimum=config.research_minimum,
        minimum_trading_days=config.minimum_training_days,
        maximum_drawdown_atr=maximum_drawdown,
        maximum_loss_streak=maximum_streak,
        minimum_late_ratio=minimum_late_ratio,
    )
    validations = apply_benjamini_hochberg(validations, fdr_alpha=config.fdr_alpha)
    return {item.key: item for item in validations}


def decide_rules(
    config: GateConfig,
    validation_map: dict[tuple[str, str, str, int], ActionPatternValidation],
) -> list[GateDecision]:
    decisions: list[GateDecision] = []
    for rule in config.rules:
        validation = validation_map.get(rule.key)
        reasons: list[str] = []
        if not rule.enabled:
            reasons.append("rule disabled")
        if validation is None:
            reasons.append("no matching training validation state")
        else:
            result = validation.result
            if result.status not in rule.allowed_statuses:
                reasons.append(
                    f"status {result.status} not in {','.join(rule.allowed_statuses)}"
                )
            if result.q_value > rule.max_q_value:
                reasons.append(
                    f"q-value {result.q_value:.4f} exceeds rule limit {rule.max_q_value:.4f}"
                )
            if result.max_drawdown_atr > rule.max_drawdown_atr:
                reasons.append(
                    f"drawdown {result.max_drawdown_atr:.3f} exceeds rule limit "
                    f"{rule.max_drawdown_atr:.3f}"
                )
            if result.max_loss_streak > rule.max_loss_streak:
                reasons.append(
                    f"loss streak {result.max_loss_streak} exceeds rule limit "
                    f"{rule.max_loss_streak}"
                )
            if result.late_to_early_ratio < rule.minimum_late_ratio:
                reasons.append(
                    f"late ratio {result.late_to_early_ratio:.3f} below rule limit "
                    f"{rule.minimum_late_ratio:.3f}"
                )
        decisions.append(
            GateDecision(
                rule=rule,
                validation=validation,
                eligible=not reasons,
                reasons=tuple(reasons),
            )
        )
    return decisions


def _matching_rows(
    rows: list[dict[str, str]], rule: GateRule
) -> list[dict[str, str]]:
    matched = [
        row
        for row in rows
        if str(row.get("symbol", "")).upper() == rule.symbol.upper()
        and str(row.get("action", "")).upper() == rule.action.upper()
        and rule.label.upper() in feature_labels(row)
    ]
    return non_overlapping_rows(matched, rule.horizon)


def _paper_row(
    row: dict[str, str],
    decision: GateDecision,
    generated_at: datetime,
) -> dict[str, str]:
    rule = decision.rule
    validation = decision.validation
    assert validation is not None
    result = validation.result
    horizon = rule.horizon
    source_id = str(row.get("signal_id", ""))
    paper_id = f"{source_id}|{rule.rule_id}|H{horizon}"
    return {
        "paper_signal_id": paper_id,
        "generated_at": generated_at.isoformat(),
        "rule_id": rule.rule_id,
        "tier": rule.tier,
        "source_signal_id": source_id,
        "signal_time": str(row.get("signal_time", "")),
        "symbol": rule.symbol.upper(),
        "timeframe": str(row.get("timeframe", "")),
        "action": rule.action.upper(),
        "label": rule.label.upper(),
        "horizon": str(horizon),
        "entry_price": str(row.get("entry_price", "")),
        "score": str(row.get("score", "")),
        "confidence": str(row.get("confidence", "")),
        "spread_points": str(row.get("spread_points", "")),
        "spread_cost_atr": str(row.get("spread_cost_atr", "")),
        "training_status": result.status,
        "training_trades": str(result.total.trades),
        "training_days": str(result.trading_days),
        "training_win_rate": _format(result.total.win_rate),
        "training_pf_atr": _format(result.total.profit_factor_atr),
        "training_avg_net_atr": _format(result.total.avg_net_atr),
        "training_early_avg_net_atr": _format(result.early.avg_net_atr),
        "training_late_avg_net_atr": _format(result.late.avg_net_atr),
        "training_max_drawdown_atr": _format(result.max_drawdown_atr),
        "training_max_loss_streak": str(result.max_loss_streak),
        "training_ci_low": _format(result.mean_ci_low),
        "training_ci_high": _format(result.mean_ci_high),
        "training_p_value": _format(result.p_value),
        "training_q_value": _format(result.q_value),
        "exit_time": str(row.get(f"exit_time_{horizon}", "")),
        "net_move": str(row.get(f"net_move_{horizon}", "")),
        "progress_atr": str(row.get(f"progress_atr_{horizon}", "")),
        "mfe_atr": str(row.get(f"mfe_atr_{horizon}", "")),
        "mae_atr": str(row.get(f"mae_atr_{horizon}", "")),
        "outcome": str(row.get(f"outcome_{horizon}", "")),
    }


def _status_row(decision: GateDecision, generated_at: datetime) -> dict[str, str]:
    rule = decision.rule
    validation = decision.validation
    if validation is None:
        return {
            "generated_at": generated_at.isoformat(),
            "rule_id": rule.rule_id,
            "tier": rule.tier,
            "symbol": rule.symbol,
            "label": rule.label,
            "action": rule.action,
            "horizon": str(rule.horizon),
            "enabled": "1" if rule.enabled else "0",
            "eligible": "0",
            "status": "MISSING",
            "reasons": " | ".join(decision.reasons),
        }
    result = validation.result
    return {
        "generated_at": generated_at.isoformat(),
        "rule_id": rule.rule_id,
        "tier": rule.tier,
        "symbol": rule.symbol,
        "label": rule.label,
        "action": rule.action,
        "horizon": str(rule.horizon),
        "enabled": "1" if rule.enabled else "0",
        "eligible": "1" if decision.eligible else "0",
        "status": result.status,
        "trades": str(result.total.trades),
        "trading_days": str(result.trading_days),
        "win_rate": _format(result.total.win_rate),
        "pf_atr": _format(result.total.profit_factor_atr),
        "avg_net_atr": _format(result.total.avg_net_atr),
        "early_avg_net_atr": _format(result.early.avg_net_atr),
        "late_avg_net_atr": _format(result.late.avg_net_atr),
        "late_to_early_ratio": _format(result.late_to_early_ratio),
        "max_drawdown_atr": _format(result.max_drawdown_atr),
        "max_loss_streak": str(result.max_loss_streak),
        "ci_low": _format(result.mean_ci_low),
        "ci_high": _format(result.mean_ci_high),
        "p_value": _format(result.p_value),
        "q_value": _format(result.q_value),
        "reasons": " | ".join(decision.reasons or result.reasons),
    }


def _atomic_write(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def run_gate(
    journal_path: Path,
    config_path: Path,
    output_path: Path,
    status_path: Path,
    *,
    generated_at: datetime | None = None,
) -> GateSummary:
    config = load_config(config_path)
    rows = _load_journal(journal_path, config.schema_version)
    training_rows = [row for row in rows if _signal_time(row) < config.training_cutoff]
    out_of_sample_rows = [row for row in rows if _signal_time(row) >= config.training_cutoff]
    validation_map = _validation_map(training_rows, config)
    decisions = decide_rules(config, validation_map)
    timestamp = generated_at or datetime.now().astimezone()

    paper_rows: list[dict[str, str]] = []
    for decision in decisions:
        if not decision.eligible or decision.validation is None:
            continue
        paper_rows.extend(
            _paper_row(row, decision, timestamp)
            for row in _matching_rows(out_of_sample_rows, decision.rule)
        )
    paper_rows.sort(key=lambda row: (row["signal_time"], row["rule_id"]))
    status_rows = [_status_row(decision, timestamp) for decision in decisions]
    _atomic_write(output_path, _PAPER_FIELDS, paper_rows)
    _atomic_write(status_path, _GATE_STATUS_FIELDS, status_rows)
    return GateSummary(
        decisions=tuple(decisions),
        paper_signals=len(paper_rows),
        output_path=output_path,
        status_path=status_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate frozen out-of-sample TradeMind paper signals; never send orders"
    )
    default_journal = Path(os.getenv("TRADEMIND_JOURNAL_DIR", "data/journal_ecn")) / "signals.csv"
    parser.add_argument("--journal", type=Path, default=default_journal)
    parser.add_argument("--config", type=Path, default=Path("config/paper_gate_v1.3.json"))
    parser.add_argument("--output", type=Path, default=Path("data/paper_signals/signals.csv"))
    parser.add_argument(
        "--status-output",
        type=Path,
        default=Path("data/paper_signals/gate_status.csv"),
    )
    args = parser.parse_args()

    journal = args.journal.expanduser().resolve()
    config = args.config.expanduser().resolve()
    if not journal.is_file():
        print(f"Signal journal not found: {journal}")
        return 1
    if not config.is_file():
        print(f"Paper gate config not found: {config}")
        return 1
    try:
        summary = run_gate(
            journal,
            config,
            args.output.expanduser().resolve(),
            args.status_output.expanduser().resolve(),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Paper gate failed: {exc}")
        return 1

    print("TradeMind v1.3 paper signal gate")
    for decision in summary.decisions:
        state = "ELIGIBLE" if decision.eligible else "BLOCKED"
        reasons = "; ".join(decision.reasons) if decision.reasons else "training rules passed"
        print(f"{state}: {decision.rule.rule_id} - {reasons}")
    print(f"Paper signals: {summary.paper_signals}")
    print(f"Paper journal: {summary.output_path}")
    print(f"Gate status: {summary.status_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
