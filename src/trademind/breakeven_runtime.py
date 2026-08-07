"""Autonomous read-only runtime for break-even statistics.

The runtime only orchestrates existing immutable MT5 CSV exports. It updates the
v1.28 shadow journal, resolves v1.29 counterfactual outcomes, and writes one
combined health/status file. It never connects to a broker or changes robot state.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from trademind.breakeven_counterfactual import run_counterfactual
from trademind.breakeven_stat_monitor import run_monitor

VERSION = "1.30.0"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_meta(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "bytes": stat.st_size,
        "last_write_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _safety() -> dict[str, bool]:
    return {
        "read_only": True,
        "shadow_only": True,
        "orders_enabled": False,
        "broker_api_called": False,
        "source_csv_modified": False,
        "robot_settings_modified": False,
    }


def run_runtime(
    positions_csv: Path,
    deals_csv: Path,
    shadow_output_dir: Path,
    counterfactual_output_dir: Path,
    status_path: Path,
    *,
    login: str,
) -> dict[str, Any]:
    if not positions_csv.is_file():
        raise ValueError(f"positions CSV not found: {positions_csv}")
    if not deals_csv.is_file():
        raise ValueError(f"deals CSV not found: {deals_csv}")

    shadow = run_monitor(positions_csv, shadow_output_dir)
    counterfactual = run_counterfactual(
        shadow_output_dir / "state.json",
        deals_csv,
        counterfactual_output_dir,
        login=login,
    )

    counter_state = str(counterfactual.get("state", ""))
    state = "OK" if shadow.get("state") == "OK" and counter_state == "OK" else "WARN"
    if counter_state.startswith("WARN_") and shadow.get("state") == "OK":
        state = counter_state

    status = {
        "schema_version": VERSION,
        "state": state,
        "mode": "AUTONOMOUS_READ_ONLY_BE_STATISTICS",
        "login": login,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "positions": _file_meta(positions_csv),
            "deals": _file_meta(deals_csv),
        },
        "shadow": {
            "trackable_basket_epochs": shadow.get("trackable_basket_epochs", 0),
            "open_trackable_epochs": shadow.get("open_trackable_epochs", 0),
            "be_triggered_epochs": shadow.get("be_triggered_epochs", 0),
            "be_revisited_after_trigger_epochs": shadow.get(
                "be_revisited_after_trigger_epochs", 0
            ),
        },
        "counterfactual": {
            "completed_baskets": counterfactual.get("completed_baskets", 0),
            "covered_completed_baskets": counterfactual.get(
                "covered_completed_baskets", 0
            ),
            "losses_avoided_count": counterfactual.get("losses_avoided_count", 0),
            "winners_cut_count": counterfactual.get("winners_cut_count", 0),
            "triggered_without_revisit_count": counterfactual.get(
                "triggered_without_revisit_count", 0
            ),
            "net_effect_proxy_money": counterfactual.get("net_effect_proxy_money", 0.0),
            "unmapped_shadow_epochs": counterfactual.get("unmapped_shadow_epochs", 0),
            "ambiguous_shadow_epochs": counterfactual.get("ambiguous_shadow_epochs", 0),
        },
        "outputs": {
            "shadow": str(shadow_output_dir.resolve()),
            "counterfactual": str(counterfactual_output_dir.resolve()),
        },
        "safety": _safety(),
    }
    _atomic_json(status_path, status)
    return status


def _write_error_status(status_path: Path, login: str, exc: Exception) -> None:
    _atomic_json(
        status_path,
        {
            "schema_version": VERSION,
            "state": "ERROR",
            "mode": "AUTONOMOUS_READ_ONLY_BE_STATISTICS",
            "login": login,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "safety": _safety(),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeMind autonomous read-only BE runtime")
    parser.add_argument("--login", required=True)
    parser.add_argument("--positions-csv", required=True, type=Path)
    parser.add_argument("--deals-csv", required=True, type=Path)
    parser.add_argument("--shadow-output-dir", required=True, type=Path)
    parser.add_argument("--counterfactual-output-dir", required=True, type=Path)
    parser.add_argument("--status", required=True, type=Path)
    args = parser.parse_args()

    try:
        status = run_runtime(
            args.positions_csv,
            args.deals_csv,
            args.shadow_output_dir,
            args.counterfactual_output_dir,
            args.status,
            login=args.login,
        )
    except Exception as exc:  # scheduled boundary: persist failure for later inspection
        _write_error_status(args.status, args.login, exc)
        print(f"BreakEven autonomous runtime failed: {exc}")
        return 1

    print("TradeMind v1.30 Autonomous BreakEven Runtime")
    print("READ-ONLY / SHADOW ONLY / ORDERS OFF")
    print(f"Open basket epochs: {status['shadow']['open_trackable_epochs']}")
    print(f"BE triggers: {status['shadow']['be_triggered_epochs']}")
    print(f"Covered completed baskets: {status['counterfactual']['covered_completed_baskets']}")
    print(f"Losses avoided: {status['counterfactual']['losses_avoided_count']}")
    print(f"Winners cut: {status['counterfactual']['winners_cut_count']}")
    print(f"Net effect proxy: {status['counterfactual']['net_effect_proxy_money']}")
    print(f"Status: {args.status.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
