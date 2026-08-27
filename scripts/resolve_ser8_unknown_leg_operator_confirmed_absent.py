#!/usr/bin/env python3
"""SER8 UNKNOWN-leg operator-confirmed-absent resolution CLI V1.

ONE narrow, auditable Windows entrypoint for the single situation the
automatic reconciler (``scripts/reconcile_ser8_mt5_execution.py`` /
``trademind.ser8_mt5_execution_reconciliation.run_reconciliation_cycle``)
is deliberately unable to resolve on its own: a persisted ``UNKNOWN``
demo-order leg whose broker outcome was never learned because the local
result was never durably finalized (terminal/EA offline at attempt time,
no broker order receipt, no result-CSV row), AND for which an operator has
now manually inspected the authoritative broker account History and
confirmed **no order, deal, or position of that identity ever existed**.

The automatic reconciler never infers a terminal state from *absence* of a
broker order -- that fail-closed rule is correct for the crash-before-
persist window and must stay. This CLI is the human-authoritative
counterpart: it takes the operator's out-of-band "never reached the
broker" confirmation and drives the EXISTING lifecycle to record it.

It does exactly this and nothing else:

  * loads the leg via ``SER8DemoOrderSendControl``'s public read-only
    accessors (``get_leg_receipt`` / ``get_leg_request``) -- no raw SQL,
    no manual row edit;
  * refuses unless the leg's current ``result_state`` is exactly
    ``UNKNOWN`` (once terminal, always terminal -- never re-decided here);
  * refuses unless the caller-supplied ``--symbol`` / ``--comment`` /
    ``--request-hash`` all match the leg's own persisted request -- the
    wrong leg cannot be transitioned by a typo'd ``--leg-id``;
  * INDEPENDENTLY re-scans the unified executor's own read-only broker
    exports (``mt5_risk_orders_utc_<account>.csv`` /
    ``mt5_risk_deals_utc_<account>.csv``) and ABORTS if ANY order or deal
    matches this leg's request identity (symbol + magic + comment, or an
    exact comment hit) -- real broker truth is never overridden by this
    tool; if the order actually exists, the automatic reconciler must
    handle it instead;
  * transitions ONLY the one named leg, via the authoritative
    ``SER8DemoOrderSendControl.reconcile_unknown_leg`` with a
    ``DemoOrderTransportResult`` that carries NO order_ticket, NO deal,
    NO fill, ``broker_send_performed=False`` and a non-DONE retcode, which
    ``trademind.ser8_mt5_demo_order_send._classify_result`` maps to the
    terminal ``REJECTED`` state -- i.e. "the broker never accepted this
    order". No fill and no P/L is ever manufactured;
  * then finalises the plan's aggregate outcome through the existing
    ``SER8DemoTradeOutcomeControl.capture_execution_plan_outcome`` (a
    single non-position-bearing terminal leg yields ``NO_FILL_TERMINAL``
    with ``total_realized_pl=0.0``), which is the ONLY thing that lets
    ``list_active_execution_plans`` retire the symbol plan;
  * NEVER constructs an authorization/claim, NEVER calls a transport,
    NEVER writes a demo-order request file. The wired transport is a bare
    ``FakeDemoOrderTransport`` with no ``result_factory`` -- any
    accidental send raises instead of placing an order;
  * with ``--dry-run`` performs every validation and prints the planned
    before/after, writing nothing;
  * is idempotent -- re-running after success finds the leg already
    terminal and the plan already retired and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_demo_trade_outcome_capture import (  # noqa: E402
    SER8DemoTradeOutcomeControl,
)
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DemoOrderTransportResult,
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
)
from trademind.ser8_mt5_execution_reconciliation import (  # noqa: E402
    load_deal_history,
    load_order_history,
)

_NON_DONE_RETCODE = 0  # not TRADE_RETCODE_DONE (10009) and not REQUOTE -> _classify_result => REJECTED


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", type=Path, required=True, help="SER8 HypothesisRegistry SQLite path")
    p.add_argument("--account", required=True, help="MT5 demo account id owning the leg")
    p.add_argument("--leg-id", required=True, help="Exact UNKNOWN leg claim_id to transition")
    p.add_argument("--plan-id", required=True, help="Execution plan id the leg belongs to (checked)")
    p.add_argument("--symbol", required=True, help="Expected request symbol (identity guard)")
    p.add_argument("--comment", required=True, help="Expected executor comment identity (identity guard)")
    p.add_argument("--request-hash", required=True, help="Expected persisted request_hash (identity guard)")
    p.add_argument(
        "--mt5-export-dir", type=Path, required=True,
        help="Dir with mt5_risk_orders_utc_<account>.csv / mt5_risk_deals_utc_<account>.csv",
    )
    p.add_argument(
        "--evidence-note", required=True,
        help="Free-text operator provenance recorded as the receipt's retcode_description",
    )
    p.add_argument("--dry-run", action="store_true", help="Validate and preview only; write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    registry = HypothesisRegistry(Path(args.db).expanduser())
    send_control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    outcome_control = SER8DemoTradeOutcomeControl(registry=registry)

    leg_id = args.leg_id
    receipt = send_control.get_leg_receipt(leg_id)
    request = send_control.get_leg_request(leg_id)
    if receipt is None or request is None:
        print(f"FAIL: no persisted send attempt for leg {leg_id!r}", file=sys.stderr)
        return 2

    # ---- identity guards -------------------------------------------------
    if receipt.plan_id != args.plan_id:
        print(
            f"FAIL: leg {leg_id!r} belongs to plan {receipt.plan_id!r}, not {args.plan_id!r}",
            file=sys.stderr,
        )
        return 2
    if request.symbol != args.symbol or request.comment != args.comment:
        print(
            f"FAIL: leg request identity mismatch "
            f"(symbol={request.symbol!r} comment={request.comment!r})",
            file=sys.stderr,
        )
        return 2
    if request.request_hash != args.request_hash:
        print(
            f"FAIL: leg request_hash {request.request_hash!r} != expected {args.request_hash!r}",
            file=sys.stderr,
        )
        return 2

    # ---- idempotency ---------------------------------------------------
    if receipt.result_state != "UNKNOWN":
        active_after = [
            p.plan_id
            for p in send_control.list_active_execution_plans(args.account, symbol=request.symbol)
        ]
        print(
            f"NOOP: leg {leg_id} is already terminal (result_state={receipt.result_state}); "
            f"active {request.symbol} plans now: {active_after or 'none'}"
        )
        return 0

    # ---- independent broker-truth re-scan (never override a real order) --
    export_dir = Path(args.mt5_export_dir).expanduser()
    orders = load_order_history(export_dir / f"mt5_risk_orders_utc_{args.account}.csv")
    deals = load_deal_history(export_dir / f"mt5_risk_deals_utc_{args.account}.csv")
    magic = str(request.magic)
    order_hits = [
        tkt
        for tkt, row in orders.items()
        if row.comment == request.comment
        or (row.symbol == request.symbol and row.magic == magic
            and abs(row.volume - request.volume) <= 1e-9)
    ]
    deal_hits = [
        d.deal_ticket
        for rows in deals.values()
        for d in rows
        if d.symbol == request.symbol and (not d.magic or d.magic == magic)
    ]
    if order_hits or deal_hits:
        print(
            "ABORT: broker export shows order/deal activity matching this leg's identity "
            f"(orders={order_hits}, deals={deal_hits}); this tool refuses to override real broker "
            "truth -- let the automatic reconciler resolve it.",
            file=sys.stderr,
        )
        return 3
    print(
        f"broker re-scan clean: 0 matching orders / 0 matching deals for "
        f"symbol={request.symbol} magic={magic} comment={request.comment!r} "
        f"({len(orders)} order rows, {sum(len(v) for v in deals.values())} deal rows scanned)"
    )

    # ---- plan / active-state before ------------------------------------
    active_before = [
        p.plan_id
        for p in send_control.list_active_execution_plans(args.account, symbol=request.symbol)
    ]
    print(
        f"BEFORE: leg {leg_id} result_state=UNKNOWN order_ticket={receipt.order_ticket!r} "
        f"broker_send_performed={receipt.broker_send_performed}; "
        f"active {request.symbol} plans: {active_before or 'none'}"
    )

    evidence = DemoOrderTransportResult(
        claim_id=leg_id,
        demo_account_id=args.account,
        symbol=request.symbol,
        retcode=_NON_DONE_RETCODE,
        retcode_description=f"OPERATOR_CONFIRMED_ABSENT: {args.evidence_note}",
        order_ticket="",
        deal_ticket="",
        position_ticket="",
        filled_volume=None,
        filled_price=None,
        broker_send_performed=False,
    )

    if args.dry_run:
        print(
            "DRY-RUN: would call reconcile_unknown_leg(evidence=<retcode=0, no ticket, no fill, "
            "broker_send_performed=False>) -> classifies REJECTED; then "
            f"capture_execution_plan_outcome({args.plan_id}) -> NO_FILL_TERMINAL. "
            "order_resent=NO broker_send_performed=NO. Nothing written."
        )
        return 0

    new_receipt = send_control.reconcile_unknown_leg(leg_id, evidence=evidence)
    if new_receipt.result_state != "REJECTED":
        print(
            f"FAIL: expected REJECTED after reconcile_unknown_leg, got {new_receipt.result_state!r}",
            file=sys.stderr,
        )
        return 2

    plan_outcome = outcome_control.capture_execution_plan_outcome(
        args.plan_id, send_control=send_control
    )

    active_after = [
        p.plan_id
        for p in send_control.list_active_execution_plans(args.account, symbol=request.symbol)
    ]
    print(
        f"AFTER: leg {leg_id} result_state={new_receipt.result_state} "
        f"order_ticket={new_receipt.order_ticket!r} broker_send_performed={new_receipt.broker_send_performed} "
        f"order_resent=NO"
    )
    print(
        f"PLAN_OUTCOME: {args.plan_id} -> "
        f"{plan_outcome.aggregate_result if plan_outcome else 'NOT_CAPTURED'} "
        f"(total_realized_pl={plan_outcome.total_realized_pl if plan_outcome else 'n/a'})"
    )
    print(f"ACTIVE {request.symbol} plans after: {active_after or 'none'}")
    still_unknown = send_control.list_unknown_leg_ids_for_account(args.account)
    print(f"UNKNOWN legs for account after: {list(still_unknown) or 'none'}")

    if plan_outcome is None or leg_id in still_unknown or args.plan_id in active_after:
        print("FAIL: post-conditions not fully met", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
