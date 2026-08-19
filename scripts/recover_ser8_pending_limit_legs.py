#!/usr/bin/env python3
"""SER8 Pending Limit Recovery CLI V1.

ONE safe Windows CLI entrypoint to recover an EXISTING real demo
execution plan's legacy-misclassified LIMIT legs (persisted MALFORMED by
an older version of ``trademind.ser8_mt5_demo_order_send``, before it
recognized a genuinely broker-accepted pending LIMIT/STOP order) -- with
NO manual Python snippets, NO manual SQLite edits, and NO broker send of
any kind.

This script:

  * loads the persisted execution plan/legs from the authoritative SER8
    store via ``SER8DemoOrderSendControl``'s own public, read-only
    accessors (``list_leg_ids_for_claim`` / ``get_leg_receipt`` /
    ``get_leg_request``) -- never raw/ad-hoc SQL, never a manual edit;
  * identifies ONLY legs under the EXACT claim root supplied via
    ``--claim-id`` (an exact SQL ``=`` match, never a prefix/LIKE match);
  * leaves an already-FILLED (or any other non-MALFORMED) leg completely
    untouched -- reported, never mutated;
  * for each MALFORMED leg, reconstructs the recovery evidence
    EXCLUSIVELY from that SAME leg's own already-persisted original
    broker receipt (its retcode/order_ticket/deal_ticket/position_ticket/
    filled_price, exactly as this module already stored them at the time
    of the original send) and its own persisted request (for
    demo_account_id/symbol) -- NEVER a guess, NEVER invented, NEVER
    borrowed from a different leg or claim;
  * calls ``SER8DemoOrderSendControl.recover_misclassified_pending_leg``,
    which enforces every one of its own strict invariants (persisted
    order_type must be LIMIT/STOP, request integrity check, order_ticket
    consistency, reclassification must yield exactly PENDING) -- this
    script adds NO weaker path around them and cannot bypass them;
  * NEVER constructs a new ``ExecutionAuthorizationV1``/
    ``ExecutionAuthorizationClaimV1``, NEVER calls a transport, NEVER
    creates an order. The transport wired into ``SER8DemoOrderSendControl``
    here is deliberately a bare ``FakeDemoOrderTransport`` with NO
    configured ``result_factory`` -- if ANY code path ever accidentally
    tried to send, it would raise immediately instead of silently placing
    a real order. This is a second, independent guarantee on top of
    ``recover_misclassified_pending_leg`` itself never calling the
    transport;
  * prints an explicit before/after line for every leg it inspects:
    leg_id, order_type, old_state, new_state, order_ticket, and
    ``broker_send_performed=NO`` (always -- this script can never place an
    order, so this is always true, and is printed explicitly rather than
    left implicit);
  * with ``--dry-run``, performs EVERY validation but writes nothing --
    ``recover_misclassified_pending_leg``'s own ``dry_run=True`` parameter
    is threaded straight through, so dry-run behavior is the SAME
    authoritative code path this script uses for a real recovery, never a
    separately maintained approximation that could drift out of sync;
  * is fully idempotent -- re-running after a successful recovery finds
    every leg already PENDING (or FILLED, etc.) and reports it unchanged,
    performing zero further writes and zero broker action.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DemoOrderExecutionReceiptV1,
    DemoOrderRequestV1,
    DemoOrderTransportResult,
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
    SER8DemoOrderSendError,
)

_RECOVERABLE_STATE = "MALFORMED"


def _required_evidence_fields(receipt: DemoOrderExecutionReceiptV1) -> list[str]:
    """Names of every field this script requires to be genuinely present
    on a persisted receipt before it will even attempt to reconstruct
    recovery evidence from it. This is deliberately looser than
    ``recover_misclassified_pending_leg``'s own full validation (which
    stays the sole authority on whether evidence actually PROVES a
    pending placement) -- this check only rules out the case where there
    is not even enough raw data to attempt the reconstruction at all
    (e.g. a leg whose original send never reached the broker: retcode
    still the -1 default, or no order_ticket was ever recorded). Never
    substitutes a guessed value for anything reported here."""
    missing: list[str] = []
    if receipt.retcode is None or receipt.retcode < 0:
        missing.append("retcode")
    if not receipt.order_ticket or not receipt.order_ticket.strip() or receipt.order_ticket.strip() == "0":
        missing.append("order_ticket")
    if receipt.filled_price is None:
        missing.append("filled_price")
    return missing


def _build_evidence_from_persisted_receipt(
    leg_id: str, receipt: DemoOrderExecutionReceiptV1, request: DemoOrderRequestV1
) -> DemoOrderTransportResult:
    """Reconstructs the EXACT evidence object
    ``recover_misclassified_pending_leg`` needs, using ONLY values already
    persisted on THIS leg's own receipt/request at the time of the
    original send -- never a guess, never inferred, never borrowed from
    any other leg or claim."""
    return DemoOrderTransportResult(
        claim_id=leg_id,
        demo_account_id=request.demo_account_id,
        symbol=request.symbol,
        retcode=receipt.retcode,
        retcode_description=receipt.retcode_description,
        order_ticket=receipt.order_ticket,
        deal_ticket=receipt.deal_ticket,
        position_ticket=receipt.position_ticket,
        filled_volume=receipt.filled_volume,
        filled_price=receipt.filled_price,
    )


def _print_leg_line(
    *, leg_id: str, order_type: str, old_state: str, new_state: str, order_ticket: str
) -> None:
    print(
        f"  leg_id={leg_id} order_type={order_type} old_state={old_state} "
        f"new_state={new_state} order_ticket={order_ticket or '(none)'} "
        f"broker_send_performed=NO"
    )


def recover_claim(
    control: SER8DemoOrderSendControl, *, claim_id: str, account: str, dry_run: bool
) -> int:
    """Recovers every recoverable MALFORMED leg under ``claim_id``.
    Returns 0 if every leg was either already-fine or successfully
    recovered/validated; returns 2 if any leg was skipped/refused (fail
    closed), so a calling script/scheduler can detect a partial result
    without parsing stdout."""
    leg_ids = control.list_leg_ids_for_claim(claim_id)
    if not leg_ids:
        print(f"no legs found under claim root {claim_id!r} in this registry", file=sys.stderr)
        return 2

    print("SER8 PENDING LIMIT RECOVERY")
    print(f"  claim root  = {claim_id}")
    print(f"  account     = {account}")
    print(f"  mode        = {'DRY-RUN (no mutation)' if dry_run else 'REAL RECOVERY'}")
    print(f"  legs found  = {len(leg_ids)}")
    print()

    exit_code = 0
    for leg_id in leg_ids:
        receipt = control.get_leg_receipt(leg_id)
        if receipt is None:
            print(f"  leg_id={leg_id}: no persisted receipt found -- skipped")
            exit_code = 2
            continue

        try:
            request = control.get_leg_request(leg_id)
        except SER8DemoOrderSendError as exc:
            print(f"  leg_id={leg_id}: FAILED CLOSED -- persisted request integrity check failed: {exc}")
            exit_code = 2
            continue
        if request is None:
            print(f"  leg_id={leg_id}: no persisted request found -- skipped")
            exit_code = 2
            continue

        # Account identity cross-check -- never silently operate on a
        # different account than the operator explicitly named.
        if request.demo_account_id != account:
            print(
                f"  leg_id={leg_id}: FAILED CLOSED -- persisted demo_account_id "
                f"{request.demo_account_id!r} does not match --account {account!r}"
            )
            exit_code = 2
            continue

        old_state = receipt.result_state
        if old_state != _RECOVERABLE_STATE:
            # Nothing to do -- an already-FILLED leg (or any other
            # already-terminal/PENDING leg) is reported, never touched.
            _print_leg_line(
                leg_id=leg_id, order_type=request.order_type, old_state=old_state,
                new_state=old_state, order_ticket=receipt.order_ticket,
            )
            continue

        missing = _required_evidence_fields(receipt)
        if missing:
            print(
                f"  leg_id={leg_id}: FAILED CLOSED -- persisted receipt is missing required "
                f"evidence: {', '.join(missing)}. Refusing to infer it."
            )
            exit_code = 2
            continue

        evidence = _build_evidence_from_persisted_receipt(leg_id, receipt, request)
        try:
            recovered = control.recover_misclassified_pending_leg(leg_id, evidence=evidence, dry_run=dry_run)
        except SER8DemoOrderSendError as exc:
            print(f"  leg_id={leg_id}: FAILED CLOSED -- {exc}")
            exit_code = 2
            continue

        _print_leg_line(
            leg_id=leg_id, order_type=request.order_type, old_state=old_state,
            new_state=recovered.result_state, order_ticket=recovered.order_ticket,
        )

    print()
    if dry_run:
        print("DRY RUN COMPLETE -- no state was mutated, no broker action was taken.")
    else:
        print("RECOVERY COMPLETE -- no broker action was taken.")
    return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="Path to the SER8 HypothesisRegistry SQLite file")
    parser.add_argument(
        "--account", required=True,
        help="MT5 demo login/account id -- cross-checked against every leg's own persisted demo_account_id",
    )
    parser.add_argument(
        "--claim-id", required=True,
        help="Exact claim root to recover, e.g. EAC-67206924-2e40988a6cd689d6 (exact match only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate and report only -- performs every check but never mutates persisted state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    registry = HypothesisRegistry(Path(args.db).expanduser())
    # A deliberately non-functional transport: recovery NEVER calls
    # transport.send() at all, but wiring in a transport that would raise
    # immediately if it somehow WERE invoked is a second, independent
    # guarantee that this script can never place a real order.
    transport = FakeDemoOrderTransport()
    control = SER8DemoOrderSendControl(registry=registry, transport=transport)
    return recover_claim(control, claim_id=args.claim_id, account=args.account, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
