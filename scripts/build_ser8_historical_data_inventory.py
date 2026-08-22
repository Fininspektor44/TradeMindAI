#!/usr/bin/env python3
"""Build/verify the authoritative SER8 real-MT5 historical inventory."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.ser8_historical_data import (  # noqa: E402
    INVENTORY_SCHEMA_VERSION,
    CHUNK_POLICY_VERSION,
    CHUNK_ACQUISITION_CODE_SHA256,
    COLLECTOR_VERSION,
    EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION,
    HistoricalDataError,
    MetaTrader5HistorySource,
    assert_historical_artifact_isolation,
    build_dataset_manifest,
    collector_code_sha256,
    discover_available_coverage,
    load_canonical_execution_universe,
    load_inventory,
    parse_utc,
    publish_dataset,
    publish_canonical_execution_universe_snapshot,
    source_proof_result,
    validate_active_account_contract,
    verify_dataset,
    verify_canonical_execution_universe_artifact,
    verify_inventory_account_identities,
    write_inventory_artifacts,
)
from trademind.ser8_historical_replay import load_research_policy  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("verify-source", "collect", "verify-inventory"), required=True)
    parser.add_argument(
        "--execution-account",
        required=True,
        help="Account whose real broker export defines the research/execution universe",
    )
    parser.add_argument(
        "--market-data-account",
        required=True,
        help="Already-authenticated MT5 Python account used only for historical rates",
    )
    parser.add_argument("--mt5-export-dir", type=Path)
    parser.add_argument("--terminal-path", type=Path)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--from-utc")
    parser.add_argument("--to-utc")
    parser.add_argument("--proof-symbol-limit", type=int, default=0)
    parser.add_argument(
        "--policy",
        type=Path,
        default=REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json",
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_market_data",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_market_data" / "historical_inventory.json",
    )
    parser.add_argument(
        "--compatibility-csv",
        type=Path,
        default=REPO_ROOT / "data" / "ser8_historical_market_data" / "historical_rows.csv",
    )
    return parser


def _require_export(args: argparse.Namespace) -> Path:
    if args.mt5_export_dir is None:
        raise HistoricalDataError("BROKER_UNIVERSE_PATH_REQUIRED", "--mt5-export-dir is required")
    return (
        args.mt5_export_dir.expanduser().resolve()
        / f"mt5_risk_symbols_utc_{args.execution_account}.csv"
    )


def _collect(args: argparse.Namespace) -> dict[str, object]:
    if not args.from_utc or not args.to_utc:
        raise HistoricalDataError("COLLECTION_RANGE_REQUIRED", "--from-utc and --to-utc are required")
    policy = load_research_policy(args.policy.expanduser().resolve())
    if policy.get("collector_version") != COLLECTOR_VERSION:
        raise HistoricalDataError(
            "COLLECTOR_POLICY_MISMATCH",
            f"policy collector version must be {COLLECTOR_VERSION}",
        )
    if policy.get("chunk_policy_version") != CHUNK_POLICY_VERSION:
        raise HistoricalDataError(
            "CHUNK_POLICY_MISMATCH",
            f"policy chunk version must be {CHUNK_POLICY_VERSION}",
        )
    timeframe = args.timeframe.strip().upper()
    expected_interval = int(policy["expected_interval_seconds"])
    if timeframe == policy["initial_timeframe"]:
        expected_interval = int(policy["expected_interval_seconds"])
    else:
        raise HistoricalDataError(
            "TIMEFRAME_NOT_POLICY_APPROVED",
            f"{timeframe} is not the current policy timeframe {policy['initial_timeframe']}",
        )
    requested_from = parse_utc(args.from_utc)
    requested_to = parse_utc(args.to_utc)
    if requested_to <= requested_from:
        raise HistoricalDataError("INVALID_TIME_RANGE", "--to-utc must follow --from-utc")
    if args.proof_symbol_limit < 0:
        raise HistoricalDataError("PROOF_LIMIT_INVALID", "--proof-symbol-limit cannot be negative")
    symbols_csv = _require_export(args)
    dataset_root, inventory_path, compatibility_path = assert_historical_artifact_isolation(
        dataset_root=args.dataset_root,
        inventory_path=args.inventory,
        compatibility_path=args.compatibility_csv,
    )
    canonical_universe = load_canonical_execution_universe(
        symbols_csv,
        account_login=args.execution_account,
    )
    universe = canonical_universe.symbols
    canonical_snapshot_path = publish_canonical_execution_universe_snapshot(
        dataset_root,
        canonical_universe,
    )
    source = MetaTrader5HistorySource(
        market_data_account_login=args.market_data_account,
        terminal_path=args.terminal_path.expanduser().resolve() if args.terminal_path else None,
    )
    captured_at = datetime.now(timezone.utc)
    entries: list[dict[str, object]] = []
    try:
        proof = source.source_proof()
        selected = {
            item.symbol
            for item in (universe[: args.proof_symbol_limit] if args.proof_symbol_limit else universe)
        }
        collector_sha256 = collector_code_sha256()
        for symbol_index, broker_symbol in enumerate(universe, start=1):
            base: dict[str, object] = {
                "symbol": broker_symbol.symbol,
                "broker_trade_mode": broker_symbol.trade_mode,
                "asset_class": broker_symbol.asset_class,
                "risk_model_supported": broker_symbol.risk_model_supported,
                "risk_model_reason": broker_symbol.risk_model_reason or None,
                "row_count": 0,
                "accepted_historical_data": False,
                "dataset_id": None,
                "dataset_sha256": None,
                "dataset_dir": None,
                "chunk_policy_version": CHUNK_POLICY_VERSION,
            }
            print(
                f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol}",
                file=sys.stderr,
                flush=True,
            )
            if broker_symbol.symbol not in selected:
                entries.append({**base, "status": "NOT_REQUESTED_PROOF_LIMIT", "status_reason": "small proof limit"})
                print(
                    f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                    "final=NOT_REQUESTED_PROOF_LIMIT row_count=0",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if broker_symbol.trade_mode in {"DISABLED", "CLOSEONLY"}:
                entries.append(
                    {
                        **base,
                        "status": "BROKER_SYMBOL_DISABLED",
                        "status_reason": f"trade_mode={broker_symbol.trade_mode}",
                    }
                )
                print(
                    f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                    "final=BROKER_SYMBOL_DISABLED row_count=0",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            try:
                symbol_metadata = source.symbol_metadata(broker_symbol.symbol)
                if symbol_metadata.get("visible") is not True:
                    raise HistoricalDataError(
                        "BROKER_SYMBOL_DISABLED",
                        "symbol is not visible/enabled; Market Watch selection is never mutated",
                    )
                if symbol_metadata.get("trade_mode_name") in {"DISABLED", "CLOSEONLY"}:
                    raise HistoricalDataError(
                        "BROKER_SYMBOL_DISABLED",
                        f"current MT5 trade mode is {symbol_metadata['trade_mode_name']}",
                    )
                progress_counts = {"cached": 0, "acquired": 0, "empty": 0, "failed": 0, "skipped": 0}

                def report_chunk(
                    chunk_index: int,
                    chunk_total: int,
                    audit: dict[str, object],
                ) -> None:
                    method = str(audit["acquisition_method"])
                    status = str(audit["status"])
                    progress_counts["cached"] += int(method == "CACHE" and status != "FAILED")
                    progress_counts["acquired"] += int(method == "MT5" and status != "FAILED")
                    progress_counts["empty"] += int(status == "EMPTY")
                    progress_counts["failed"] += int(status == "FAILED")
                    progress_counts["skipped"] += int(status == "SKIPPED_UNAVAILABLE_PREFIX")
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        f"chunk {chunk_index}/{chunk_total} status={status.lower()} "
                        f"cached={progress_counts['cached']} acquired={progress_counts['acquired']} "
                        f"empty={progress_counts['empty']} failed={progress_counts['failed']} "
                        f"skipped={progress_counts['skipped']}",
                        file=sys.stderr,
                        flush=True,
                    )

                coverage = discover_available_coverage(
                    source=source,
                    source_proof=proof,
                    symbol=broker_symbol.symbol,
                    timeframe=timeframe,
                    requested_from_utc=requested_from,
                    requested_to_utc=requested_to,
                    staging_root=dataset_root / "staging",
                    collector_code_sha256=CHUNK_ACQUISITION_CODE_SHA256,
                    progress=report_chunk,
                )
                coverage_summary = coverage.manifest_summary()
                # A generic failure is never the coverage boundary: only a
                # positively classified genuine-unavailable chunk (see
                # discover_available_coverage) may ever truncate coverage.
                # Every other chunk-level failure leaves the whole symbol
                # unresolved/integrity-failed and fails it closed here —
                # never reinterpreted as "short broker history".
                if coverage.unresolved_error_code is not None:
                    entries.append(
                        {
                            **base,
                            **coverage_summary,
                            "status": "HISTORICAL_CHUNK_ACQUISITION_FAILED",
                            "status_reason": (
                                "transient/unresolved acquisition failure "
                                f"({coverage.unresolved_error_code}) could not be positively classified "
                                "as genuine historical unavailability after bounded retry; no truncated "
                                f"dataset published: {coverage.unresolved_error}"
                            ),
                        }
                    )
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        "final=HISTORICAL_CHUNK_ACQUISITION_FAILED row_count=0",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if coverage.integrity_error_code is not None:
                    entries.append(
                        {
                            **base,
                            **coverage_summary,
                            "status": "DATA_INTEGRITY_FAILED",
                            "status_reason": (
                                f"data-integrity failure ({coverage.integrity_error_code}) during "
                                f"acquisition: {coverage.integrity_error}"
                            ),
                        }
                    )
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        "final=DATA_INTEGRITY_FAILED row_count=0",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if coverage.merge_integrity_error_code is not None:
                    entries.append(
                        {
                            **base,
                            **coverage_summary,
                            "status": "DATA_INTEGRITY_FAILED",
                            "status_reason": coverage.merge_integrity_error,
                        }
                    )
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        "final=DATA_INTEGRITY_FAILED row_count=0",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if coverage.accepted_chunk_count == 0:
                    # A genuine boundary was proven, but it covers the entire
                    # requested range: there is no usable broker history at all.
                    entries.append(
                        {
                            **base,
                            **coverage_summary,
                            "status": "BROKER_HISTORY_UNAVAILABLE",
                            "status_reason": (
                                "broker positively reports no retained history anywhere in the "
                                f"requested range ({coverage.requested_chunk_count} chunks probed/skipped)"
                            ),
                        }
                    )
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        "final=BROKER_HISTORY_UNAVAILABLE row_count=0",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                bars = coverage.bars
                if not bars:
                    entries.append(
                        {
                            **base,
                            **coverage_summary,
                            "status": "BROKER_HISTORY_UNAVAILABLE",
                            "status_reason": "every accepted chunk completed but returned no bars",
                        }
                    )
                    print(
                        f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                        "final=BROKER_HISTORY_UNAVAILABLE row_count=0",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                manifest, bars_bytes = build_dataset_manifest(
                    bars=bars,
                    source_proof=proof,
                    symbol_metadata=symbol_metadata,
                    broker_symbol=broker_symbol,
                    execution_account_login=args.execution_account,
                    execution_universe_source=symbols_csv.name,
                    execution_universe=canonical_universe,
                    timeframe=timeframe,
                    requested_from_utc=requested_from,
                    requested_to_utc=requested_to,
                    expected_interval_seconds=expected_interval,
                    source_capture_utc=captured_at,
                    collector_code_sha256=collector_sha256,
                    coverage_discovery=coverage_summary,
                )
                dataset_dir, persisted_manifest, _created = publish_dataset(
                    dataset_root, manifest, bars_bytes
                )
                quality = persisted_manifest["quality"]
                accepted = persisted_manifest["accepted_historical_data"] is True
                truncated = persisted_manifest["coverage_truncated_at_requested_start"] is True
                truncation_note = (
                    f"; effective coverage starts {persisted_manifest['effective_coverage_start_utc']} "
                    f"(requested {persisted_manifest['requested_from_utc']})"
                    if truncated
                    else ""
                )
                if not accepted:
                    status = "DATA_INTEGRITY_FAILED"
                    reason = "canonical broker bars failed deterministic integrity validation"
                elif int(quality["row_count"]) < int(policy["minimum_rows_for_replay_attempt"]):
                    status = "HISTORICAL_DATA_INSUFFICIENT"
                    reason = (
                        f"{quality['row_count']} rows below explicit replay-attempt minimum "
                        f"{policy['minimum_rows_for_replay_attempt']}"
                    )
                elif not broker_symbol.risk_model_supported or broker_symbol.asset_class != "FX":
                    status = "RISK_MODEL_UNSUPPORTED"
                    reason = broker_symbol.risk_model_reason or f"asset_class={broker_symbol.asset_class}"
                else:
                    status = "HISTORICAL_DATA_READY"
                    reason = "historical integrity passed; deterministic replay still required"
                reason = f"{reason}{truncation_note}"
                entries.append(
                    {
                        **base,
                        "status": status,
                        "status_reason": reason,
                        "row_count": quality["row_count"],
                        "accepted_historical_data": accepted,
                        "dataset_id": persisted_manifest["dataset_id"],
                        "dataset_sha256": persisted_manifest["dataset_sha256"],
                        "dataset_dir": str(dataset_dir),
                        "quality": quality,
                        **coverage_summary,
                    }
                )
                print(
                    f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                    f"final={status} row_count={quality['row_count']}",
                    file=sys.stderr,
                    flush=True,
                )
            except HistoricalDataError as exc:
                entries.append({**base, "status": exc.code, "status_reason": str(exc)})
                print(
                    f"symbol {symbol_index}/{len(universe)} {broker_symbol.symbol} "
                    f"final={exc.code} row_count=0",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        source.close()

    entries.sort(key=lambda item: str(item["symbol"]))
    inventory: dict[str, object] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "captured_at_utc": captured_at.isoformat().replace("+00:00", "Z"),
        "source_proof": dict(proof),
        "execution_account_login": str(args.execution_account),
        "market_data_account_login": str(args.market_data_account),
        "execution_universe_source": symbols_csv.name,
        "execution_universe_sha256": canonical_universe.canonical_sha256,
        "execution_universe_raw_sha256": canonical_universe.raw_sha256,
        "execution_universe_canonical_sha256": canonical_universe.canonical_sha256,
        "execution_universe_canonical_schema_version": (
            EXECUTION_UNIVERSE_CANONICAL_SCHEMA_VERSION
        ),
        "execution_universe_canonical_snapshot": dict(canonical_universe.canonical_snapshot),
        "execution_universe_canonical_snapshot_artifact": str(canonical_snapshot_path),
        "market_data_source_type": proof["source_type"],
        "market_data_account_server": proof["market_data_account_server"],
        "market_data_account_company": proof.get("market_data_account_company")
        or proof.get("terminal_company"),
        "market_data_account_currency": proof.get("market_data_account_currency"),
        "cross_account_provenance": {
            "execution_account_login": str(args.execution_account),
            "market_data_account_login": str(args.market_data_account),
            "accounts_claimed_equivalent": False,
            "price_feeds_claimed_byte_identical": False,
            "research_evidence_scope": "MARKET_DATA_SOURCE_OBSERVED_NOT_EXECUTION_PRICE_EQUIVALENCE",
        },
        "broker_universe_path": str(symbols_csv),
        "broker_universe_raw_sha256": canonical_universe.raw_sha256,
        "timeframe": timeframe,
        "requested_from_utc": requested_from.isoformat().replace("+00:00", "Z"),
        "requested_to_utc": requested_to.isoformat().replace("+00:00", "Z"),
        "expected_interval_seconds": expected_interval,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "staging_artifacts_canonical": False,
        "total_broker_symbols": len(universe),
        "proof_symbol_limit": args.proof_symbol_limit or None,
        "entries": entries,
        "accepted_dataset_count": sum(entry["accepted_historical_data"] is True for entry in entries),
        "research_ready_count": 0,
        "research_ready_requires_replay": True,
        "internet_fallback_used": False,
        "orders_sent": 0,
        "orders_canceled": 0,
        "positions_modified": 0,
    }
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility_path,
        payload=inventory,
    )
    return load_inventory(inventory_path)


def _verify_inventory(args: argparse.Namespace) -> dict[str, object]:
    payload = load_inventory(args.inventory.expanduser().resolve())
    verify_inventory_account_identities(
        payload,
        execution_account_login=args.execution_account,
        market_data_account_login=args.market_data_account,
    )
    snapshot_artifact = payload.get("execution_universe_canonical_snapshot_artifact")
    if not isinstance(snapshot_artifact, str):
        raise HistoricalDataError(
            "INVENTORY_CANONICAL_UNIVERSE_ARTIFACT_MISSING",
            "canonical execution-universe snapshot artifact path is missing",
        )
    verify_canonical_execution_universe_artifact(
        Path(snapshot_artifact),
        expected_sha256=str(payload["execution_universe_canonical_sha256"]),
    )
    verified = 0
    for entry in payload["entries"]:
        dataset_dir = entry.get("dataset_dir")
        if dataset_dir is not None:
            manifest = verify_dataset(Path(str(dataset_dir)))
            if (
                manifest["dataset_sha256"] != entry.get("dataset_sha256")
                or manifest["execution_account_login"]
                != payload["execution_account_login"]
                or manifest["market_data_account_login"]
                != payload["market_data_account_login"]
                or manifest["execution_universe_canonical_sha256"]
                != payload["execution_universe_canonical_sha256"]
            ):
                raise HistoricalDataError(
                    "INVENTORY_DATASET_MISMATCH",
                    f"inventory dataset mismatch for {entry['symbol']}",
                )
            verified += 1
    return {
        "status": "INVENTORY_VERIFIED",
        "execution_account_login": payload["execution_account_login"],
        "market_data_account_login": payload["market_data_account_login"],
        "inventory_sha256": payload["inventory_sha256"],
        "total_broker_symbols": payload["total_broker_symbols"],
        "verified_dataset_count": verified,
        "accepted_dataset_count": payload["accepted_dataset_count"],
        "orders_sent": 0,
        "orders_canceled": 0,
        "positions_modified": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    source = None
    try:
        validate_active_account_contract(
            execution_account_login=args.execution_account,
            market_data_account_login=args.market_data_account,
        )
        if args.mode == "verify-source":
            _require_export(args)
            # Universe validation is part of capability proof, not merely MT5 initialization.
            universe_path = _require_export(args)
            canonical_universe = load_canonical_execution_universe(
                universe_path,
                account_login=args.execution_account,
            )
            source = MetaTrader5HistorySource(
                market_data_account_login=args.market_data_account,
                terminal_path=args.terminal_path.expanduser().resolve() if args.terminal_path else None,
            )
            result = source_proof_result(
                source.source_proof(),
                execution_account_login=args.execution_account,
                execution_universe_source=universe_path.name,
                execution_universe=canonical_universe,
            )
        elif args.mode == "collect":
            result = _collect(args)
        else:
            result = _verify_inventory(args)
    except (HistoricalDataError, OSError, ValueError, TypeError) as exc:
        code = exc.code if isinstance(exc, HistoricalDataError) else "UNEXPECTED_READ_FAILURE"
        print(json.dumps({"status": "FAILED", "error_code": code, "error": str(exc)}, sort_keys=True))
        return 1
    finally:
        if source is not None:
            source.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
