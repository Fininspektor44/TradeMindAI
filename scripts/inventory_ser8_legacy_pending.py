#!/usr/bin/env python3
"""Read-only SER8 active/legacy pending-order inventory."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
)
from trademind.ser8_mt5_execution_reconciliation import (  # noqa: E402
    inventory_active_pending_orders,
    load_order_history,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--mt5-export-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    orders_csv = args.mt5_export_dir / f"mt5_risk_orders_utc_{args.account}.csv"
    registry = HypothesisRegistry(args.db)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    inventory = inventory_active_pending_orders(
        control,
        account=args.account,
        order_history=load_order_history(orders_csv),
        now=datetime.now(timezone.utc),
    )
    payload = {
        "account": args.account,
        "active_pending_count": len(inventory),
        "unmapped_active_pending_count": sum(
            item.status == "UNMAPPED_ACTIVE_PENDING_ORDER" for item in inventory
        ),
        "legacy_gtc_count": sum("LEGACY_GTC" in item.status for item in inventory),
        "broker_actions_performed": 0,
        "orders": [item.as_dict() for item in inventory],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(args.output)
    print(text, end="")
    return 2 if payload["unmapped_active_pending_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
