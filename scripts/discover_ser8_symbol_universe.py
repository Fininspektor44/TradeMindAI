#!/usr/bin/env python3
"""SER8 Full Symbol Universe Discovery + Research Ranking V1.

Read-only inventory/ranking CLI: discovers the FULL symbol universe from
REAL broker/runtime metadata (never a handwritten symbol list), classifies
each symbol's research/risk-model/asset-class status, ranks the ones
worth researching first, and optionally overlays REAL, already-persisted
``HypothesisRegistry`` state so already-ACCEPTED/REJECTED/RESEARCHING
symbols are reported correctly.

FULL UNIVERSE != FULL EXECUTION -- this script NEVER sends a broker
order, NEVER advances the research lifecycle (no proposal, no manifest
freeze, no train/test, no validation, no holdout, no final verdict), and
NEVER creates, widens, or infers any hypothesis's ACCEPTED state. It only
observes and reports. The ONLY thing this script writes, and only when
``--persist`` is explicitly given, is the additive
``ser8_symbol_universe`` table -- a point-in-time snapshot for operator
visibility, never consulted by the risk/authorization/execution chain.

INPUTS (all optional except ``--mt5-export-dir``/``--execution-account``):
  * ``--mt5-export-dir``/``--execution-account``: the REAL MT5 symbol export
    (``mt5_risk_symbols_utc_<account>.csv``) the unified executor EA
    already writes -- the SAME file ``trademind.mt5_risk_adapter`` and
    the autonomous worker's own risk gate already consume. This is the
    ONLY required input; discovery of the broker's own symbol universe
    works from this alone.
  * ``--runtime-root``/``--candidates``: the live candidate journal(s) --
    determines LIVE-RUNTIME SUPPORTED and signal-frequency ranking input.
  * ``--historical-inventory``: the authoritative hash-verified historical
    replay readiness inventory. This is the only historical input that can
    advance a symbol to RESEARCH_READY.
  * ``--historical-data-csv``: a backward-compatible ``symbol,rows`` CSV.
    It proves availability only and can never grant RESEARCH_READY.
  * ``--correlations``: defaults to the real, checked-in
    ``config/mt5/correlation_groups_v1.json``.
  * ``--db``/``--hypothesis-map``/``--demo-active``: when supplied,
    overlays REAL ``HypothesisRegistry`` state (ACCEPTED/REJECTED/
    RESEARCHING) onto the discovered universe. ``--hypothesis-map`` is a
    JSON file ``{"SYMBOL": ["hypothesis_id", ...], ...}`` the operator
    maintains by hand or generates from their own records -- this script
    never guesses which hypothesis belongs to which symbol.
  * ``--persist``: also writes the final, classified universe into the
    additive ``ser8_symbol_universe`` table in ``--db`` (requires --db).

OUTPUT: the exact FIRST DELIVERABLE inventory fields this task's own
spec requires, followed by the research queue ranked by readiness (never
by a fabricated profitability score -- see rank_research_readiness's own
docstring).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_historical_replay import load_verified_research_readiness  # noqa: E402
from trademind.ser8_symbol_universe import (  # noqa: E402
    EXECUTION_STATUS_DEMO_ACTIVE,
    RESEARCH_STATUS_ACCEPTED,
    SER8SymbolUniverseControl,
    SymbolUniverseEntryV1,
    apply_research_lifecycle_state,
    discover_symbol_universe,
    rank_research_readiness,
)


def _read_historical_rows_csv(path: Path) -> dict[str, int]:
    """Reads a REAL, operator-supplied ``symbol,rows`` CSV. Never
    fabricates a value; a malformed row is skipped, never guessed."""
    import csv

    rows: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames or "rows" not in reader.fieldnames:
            raise SystemExit(f"--historical-data-csv must have columns symbol,rows: {path}")
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            try:
                count = int(row.get("rows") or 0)
            except ValueError:
                continue
            if symbol:
                rows[symbol] = count
    return rows


def _read_correlation_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"symbols": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_hypothesis_map(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    if not path.is_file():
        raise SystemExit(f"--hypothesis-map file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("--hypothesis-map must be a JSON object of {SYMBOL: [hypothesis_id, ...]}")
    return {str(symbol).strip().upper(): list(ids) for symbol, ids in payload.items()}


def _print_inventory(entries: Sequence[SymbolUniverseEntryV1], *, ranked_queue: Sequence[SymbolUniverseEntryV1]) -> None:
    total = len(entries)
    live_runtime = sum(1 for e in entries if e.live_runtime_supported)
    research_data = sum(1 for e in entries if e.data_available)
    research_ready = sum(1 for e in entries if e.research_status == "RESEARCH_READY")
    accepted = sum(1 for e in entries if e.research_status == RESEARCH_STATUS_ACCEPTED)
    executable = sum(1 for e in entries if e.execution_status == EXECUTION_STATUS_DEMO_ACTIVE)

    print("SER8 FULL SYMBOL UNIVERSE -- REAL INVENTORY")
    print(f"TOTAL BROKER SYMBOLS: {total}")
    print(f"LIVE-RUNTIME SUPPORTED: {live_runtime}")
    print(f"RESEARCH-DATA AVAILABLE: {research_data}")
    print(f"RESEARCH-READY: {research_ready}")
    print(f"CURRENTLY ACCEPTED: {accepted}")
    print(f"CURRENTLY EXECUTABLE: {executable}")
    print()
    print("RESEARCH QUEUE (ranked by readiness -- pre-holdout/operational properties only, never a fabricated score):")
    if not ranked_queue:
        print("  (empty universe)")
    for rank, entry in enumerate(ranked_queue, start=1):
        print(
            f"  {rank:>3}. {entry.symbol:<10} asset_class={entry.asset_class:<8} "
            f"research_status={entry.research_status:<24} execution_status={entry.execution_status:<14} "
            f"risk_model_supported={entry.risk_model_supported!s:<5} data_available={entry.data_available!s:<5} "
            f"historical_rows={entry.historical_rows if entry.historical_rows is not None else '-':<8} "
            f"live_signal_sample_count={entry.live_signal_sample_count}"
            + (f" rejection_reason={entry.rejection_reason!r}" if entry.rejection_reason else "")
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mt5-export-dir", type=Path, required=True, help="Directory containing the real MT5 symbol export CSVs")
    parser.add_argument(
        "--execution-account",
        required=True,
        help="Execution account/login whose real symbol export defines the target universe",
    )
    parser.add_argument(
        "--market-data-account",
        help="Market-data account bound by --historical-inventory; required with that option",
    )
    parser.add_argument("--runtime-root", type=Path, default=None, help="Live candidate runtime root (default: data/live_signal_runtime_v1)")
    parser.add_argument("--candidates", type=Path, nargs="*", default=(), help="Additional candidate journal path(s) to scan")
    parser.add_argument("--data-root", type=Path, default=None)
    historical = parser.add_mutually_exclusive_group()
    historical.add_argument(
        "--historical-inventory", type=Path, default=None,
        help="Hash-verified deterministic replay readiness inventory",
    )
    historical.add_argument(
        "--historical-data-csv", type=Path, default=None,
        help="Legacy symbol,rows availability only; cannot grant RESEARCH_READY",
    )
    parser.add_argument("--correlations", type=Path, default=None, help="Default: config/mt5/correlation_groups_v1.json")
    parser.add_argument("--minimum-live-signal-sample", type=int, default=1)
    parser.add_argument("--db", type=Path, default=None, help="HypothesisRegistry SQLite path -- enables research-lifecycle overlay")
    parser.add_argument("--hypothesis-map", type=Path, default=None, help='JSON {"SYMBOL": ["hypothesis_id", ...]} -- requires --db')
    parser.add_argument("--demo-active", nargs="*", default=(), help="Symbols currently configured for autonomous demo execution")
    parser.add_argument("--persist", action="store_true", help="Also write the classified universe into --db's ser8_symbol_universe table")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the human-readable report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    repo_root = REPO_ROOT
    data_root = Path(args.data_root).expanduser() if args.data_root else repo_root / "data"
    runtime_root = Path(args.runtime_root).expanduser() if args.runtime_root else data_root / "live_signal_runtime_v1"
    mt5_export_dir = Path(args.mt5_export_dir).expanduser()
    symbols_csv = mt5_export_dir / f"mt5_risk_symbols_utc_{args.execution_account}.csv"

    candidates_paths = [runtime_root / "candidates.jsonl"] + [Path(p).expanduser() for p in args.candidates]
    correlations_path = Path(args.correlations).expanduser() if args.correlations else repo_root / "config" / "mt5" / "correlation_groups_v1.json"
    correlation_config = _read_correlation_config(correlations_path)
    verified_research = {}
    if args.historical_inventory:
        if not args.market_data_account:
            print("--historical-inventory requires --market-data-account", file=sys.stderr)
            return 2
        try:
            historical_rows, verified_research = load_verified_research_readiness(
                Path(args.historical_inventory).expanduser().resolve(),
                execution_account_login=args.execution_account,
                market_data_account_login=args.market_data_account,
            )
        except Exception as exc:  # noqa: BLE001 -- one fail-closed operator error.
            print(f"historical readiness inventory failed verification: {exc}", file=sys.stderr)
            return 2
    else:
        historical_rows = (
            _read_historical_rows_csv(Path(args.historical_data_csv).expanduser())
            if args.historical_data_csv
            else {}
        )

    try:
        entries = discover_symbol_universe(
            symbols_csv=symbols_csv, candidates_paths=candidates_paths, correlation_config=correlation_config,
            historical_rows_by_symbol=historical_rows, minimum_live_signal_sample=args.minimum_live_signal_sample,
            verified_research_by_symbol=verified_research,
        )
    except Exception as exc:  # noqa: BLE001 -- reported once, cleanly, never a raw traceback for a real operator run.
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 2

    hypothesis_map = _read_hypothesis_map(Path(args.hypothesis_map).expanduser() if args.hypothesis_map else None)
    registry: HypothesisRegistry | None = None
    if args.db is not None:
        registry = HypothesisRegistry(Path(args.db).expanduser())
        if hypothesis_map:
            entries = apply_research_lifecycle_state(
                entries, registry=registry, symbol_to_hypothesis_ids=hypothesis_map, demo_active_symbols=args.demo_active,
            )

    if args.persist:
        if registry is None:
            print("--persist requires --db", file=sys.stderr)
            return 2
        control = SER8SymbolUniverseControl(registry=registry)
        written = control.persist_universe(entries)
        print(f"persisted {written} symbol row(s) into ser8_symbol_universe", file=sys.stderr)

    ranked_queue = rank_research_readiness(entries)

    if args.json:
        print(json.dumps([e.to_payload() for e in ranked_queue], indent=2, sort_keys=True))
    else:
        _print_inventory(entries, ranked_queue=ranked_queue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
