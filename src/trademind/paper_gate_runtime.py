"""Runtime wrapper that keeps pre-gate and forward wave deduplication separate."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

from trademind.paper_gate_v18 import (
    DECISION_FIELDS,
    PAPER_FIELDS,
    STATE_FIELDS,
    VALID_GATE_STATUSES,
    GateSummary,
    _atomic_csv,
    _atomic_text,
    _load_csv,
    build_decisions,
    build_gate_states,
    build_paper_journal,
    load_or_create_gate_start,
    render_dashboard,
)


def build_forward_decisions(
    unified_rows: Sequence[dict[str, str]],
    unified_states: Sequence[dict[str, str]],
    captured_at: datetime,
    gate_started_at: datetime,
) -> list[dict[str, str]]:
    """Deduplicate history and forward rows independently.

    A historical event immediately before gate activation must never suppress
    the first real forward paper signal after activation.
    """
    historical: list[dict[str, str]] = []
    forward: list[dict[str, str]] = []
    for row in unified_rows:
        value = str(row.get("signal_time") or "").strip()
        if not value:
            continue
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("signal_time must contain a timezone offset")
        target = historical if moment < gate_started_at else forward
        target.append(row)
    decisions = build_decisions(historical, unified_states, captured_at)
    decisions.extend(build_decisions(forward, unified_states, captured_at))
    return sorted(decisions, key=lambda row: (row["signal_time"], row["decision_id"]))


def run_paper_gate(
    unified_signals_path: Path,
    unified_states_path: Path,
    output_dir: Path,
    now: datetime | None = None,
) -> GateSummary:
    captured_at = now or datetime.now().astimezone()
    source_rows = _load_csv(unified_signals_path)
    source_states = _load_csv(unified_states_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_path = output_dir / "gate_meta.json"
    gate_started_at = load_or_create_gate_start(meta_path, captured_at)
    decisions = build_forward_decisions(
        source_rows,
        source_states,
        captured_at,
        gate_started_at,
    )
    states = build_gate_states(source_states, captured_at)
    signals_path = output_dir / "signals.csv"
    existing = _load_csv(signals_path) if signals_path.is_file() else []
    journal = build_paper_journal(decisions, existing, gate_started_at, captured_at)
    decisions_path = output_dir / "decisions.csv"
    states_path = output_dir / "latest.csv"
    dashboard_path = output_dir / "dashboard" / "index.html"
    _atomic_csv(decisions_path, DECISION_FIELDS, decisions)
    _atomic_csv(states_path, STATE_FIELDS, states)
    _atomic_csv(signals_path, PAPER_FIELDS, journal)
    _atomic_text(dashboard_path, render_dashboard(decisions, states, journal, gate_started_at))
    counts = {
        status: sum(row["gate_status"] == status for row in decisions)
        for status in VALID_GATE_STATUSES
    }
    return GateSummary(
        gate_started_at=gate_started_at,
        source_rows=len(source_rows),
        source_states=len(source_states),
        decisions=len(decisions),
        watch=counts["WATCH"],
        candidates=counts["CANDIDATE"],
        validated=counts["VALIDATED"],
        rejected=counts["REJECTED"],
        paper_signals=len(journal),
        completed_paper_signals=sum(row["completed"] == "1" for row in journal),
        decisions_path=decisions_path,
        states_path=states_path,
        signals_path=signals_path,
        dashboard_path=dashboard_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TradeMind v1.8 forward-only paper gate")
    parser.add_argument(
        "--unified-signals",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/signals.csv"),
    )
    parser.add_argument(
        "--unified-states",
        type=Path,
        default=Path("data/unified_signal_center_v1_6/latest.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/paper_signal_gate_v1_8"))
    args = parser.parse_args()
    inputs = (
        args.unified_signals.expanduser().resolve(),
        args.unified_states.expanduser().resolve(),
    )
    for path in inputs:
        if not path.is_file():
            print(f"Required Unified Center file not found: {path}")
            return 1
    try:
        summary = run_paper_gate(inputs[0], inputs[1], args.output_dir.expanduser().resolve())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Paper Signal Gate failed: {exc}")
        return 1
    print("TradeMind v1.8 Forward Paper Signal Gate")
    print(f"Gate started at: {summary.gate_started_at.isoformat()}")
    print(f"Unified signal rows: {summary.source_rows}")
    print(f"Unified state rows: {summary.source_states}")
    print(f"Decisions: {summary.decisions}")
    print(
        "WATCH/CANDIDATE/VALIDATED/REJECTED: "
        f"{summary.watch}/{summary.candidates}/{summary.validated}/{summary.rejected}"
    )
    print(f"Forward paper signals: {summary.paper_signals}")
    print(f"Completed paper signals: {summary.completed_paper_signals}")
    print(f"Decisions: {summary.decisions_path}")
    print(f"States: {summary.states_path}")
    print(f"Signals: {summary.signals_path}")
    print(f"Dashboard: {summary.dashboard_path}")
    print("No orders were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
