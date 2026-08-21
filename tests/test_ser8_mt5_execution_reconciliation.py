"""Tests for trademind.ser8_mt5_execution_reconciliation -- SER8 AUTOMATIC
MT5 RECONCILIATION V1.

Builds its own self-contained, already-persisted PENDING execution legs
using ONLY SER8DemoOrderSendControl's own production persistence methods
(_persist_plan / _reserve_leg_attempt / _finalize -- never hand-edited
SQLite, and never the full research/authorization/claim chain, which this
module's own logic never touches and is already exhaustively proven
elsewhere), reproducing the EXACT real incident this task's own spec
describes plus every other scenario requirement 13 lists.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention for new SER8 test
modules).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DEMO_EXECUTOR_MAGIC_NUMBER,
    SCHEMA_VERSION,
    DemoOrderExecutionPlanV1,
    DemoOrderPlanLegV1,
    DemoOrderRequestV1,
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
    leg_identity,
)
from trademind.ser8_mt5_execution_reconciliation import (  # noqa: E402
    ReconciliationEvidenceError,
    evaluate_pending_leg,
    inventory_active_pending_orders,
    load_deal_history,
    load_order_history,
    run_reconciliation_cycle,
)

ACCOUNT = "67206924"
NOW = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)

ORDER_HEADER = (
    "time_msc,account_login,order_ticket,symbol,magic,side,order_type,volume,price,"
    "state,time_setup_msc,time_done_msc,position_id\n"
)
DEAL_HEADER = (
    "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,"
    "volume,price,entry,time_deal_msc\n"
)


class _FakeAuth:
    gate_hash = "sha256:" + "a" * 64


def _control(tmp_path: Path) -> tuple[SER8DemoOrderSendControl, Path]:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    transport = FakeDemoOrderTransport()  # no result_factory -- must never be called.
    control = SER8DemoOrderSendControl(registry=registry, transport=transport)
    return control, db_path


def _seed_leg(
    control: SER8DemoOrderSendControl, *, claim_id: str, entry_index: int, total_legs: int,
    order_type: str = "LIMIT", symbol: str = "EURUSD", action: str = "BUY", volume: float = 0.01,
    price: float = 1.15, result_state: str = "PENDING", order_ticket: str = "1",
    deal_ticket: str = "0", position_ticket: str = "0", magic: int = DEMO_EXECUTOR_MAGIC_NUMBER,
    account: str = ACCOUNT,
) -> str:
    """Persists exactly ONE leg -- via the control's own real production
    methods -- with a caller-chosen result_state, for building arbitrary
    multi-leg/multi-claim reconciliation test scenarios directly."""
    leg_id = leg_identity(claim_id, entry_index, total_legs=total_legs)
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id=f"EOP-{leg_id}", claim_id=claim_id,
        authorization_id=f"EA-{claim_id}", decision_id=f"RD-{claim_id}", candidate_signal_id=f"sig-{claim_id}",
        demo_account_id=account, symbol=symbol, action=action,
        legs=(
            DemoOrderPlanLegV1(
                entry_index=entry_index, leg_id=leg_id, order_type=order_type, planned_price=price,
                effective_entry_price=price, allocation=1.0, volume=volume, sl=1.0, tp=1.3,
            ),
        ),
    )
    control._persist_plan(plan, created_at=NOW.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=claim_id, entry_index=entry_index, claim_id=leg_id,
        authorization_id=plan.authorization_id, demo_account_id=account, symbol=symbol, action=action,
        order_type=order_type, volume=volume, price=price, sl=1.0, tp=1.3, magic=magic,
        comment=f"SER8:{leg_id[-20:]}",
    )
    control._reserve_leg_attempt(
        leg_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=entry_index,
        attempt_id=f"EAO-{leg_id}", request=request, demo_authorization=_FakeAuth(), captured_at=NOW,
    )
    control._finalize(
        claim_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=entry_index,
        authorization_id=plan.authorization_id, demo_gate_hash=_FakeAuth.gate_hash, request_hash=request.request_hash,
        attempt_id=f"EAO-{leg_id}", result_state=result_state, recorded_at=NOW.isoformat(),
        retcode=10009, retcode_description=("done" if result_state == "PENDING" else "Request completed"),
        order_ticket=order_ticket, deal_ticket=deal_ticket, position_ticket=position_ticket,
        requested_volume=volume, requested_price=price,
        filled_volume=(volume if result_state == "FILLED" else None),
        filled_price=(price if result_state == "FILLED" else None),
    )
    return leg_id


def _write_orders_csv(path: Path, rows: list[str]) -> None:
    path.write_text(ORDER_HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")


def _write_deals_csv(path: Path, rows: list[str]) -> None:
    path.write_text(DEAL_HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")


def _order_row(ticket: str, *, symbol="EURUSD", magic=DEMO_EXECUTOR_MAGIC_NUMBER, side="BUY",
                order_type="ORDER_TYPE_BUY_LIMIT", volume=0.01, price=1.15, state="PLACED") -> str:
    return f"1,{ACCOUNT},{ticket},{symbol},{magic},{side},{order_type},{volume},{price},{state},1,0,0"


def _deal_row(deal_ticket: str, order_ticket: str, *, position_id="500", symbol="EURUSD",
              magic=DEMO_EXECUTOR_MAGIC_NUMBER, side="BUY", volume=0.01, price=1.15, entry="IN") -> str:
    return f"1,{ACCOUNT},{deal_ticket},{order_ticket},{position_id},{symbol},{magic},{side},{volume},{price},{entry},2"


# ---------------------------------------------------------------------------
# 12: the current real 3-leg incident, end to end.
# ---------------------------------------------------------------------------


def test_real_3_leg_incident_reconciles_all_to_filled_with_zero_sends(tmp_path: Path) -> None:
    control, db_path = _control(tmp_path)
    claim_id = "EAC-67206924-2e40988a6cd689d6"
    _seed_leg(
        control, claim_id=claim_id, entry_index=1, total_legs=3, order_type="MARKET",
        result_state="FILLED", order_ticket="733124500", deal_ticket="900001", position_ticket="900002",
        price=1.15834, volume=0.05,
    )
    _seed_leg(
        control, claim_id=claim_id, entry_index=2, total_legs=3, order_type="LIMIT",
        result_state="PENDING", order_ticket="733124339", price=1.1575214999999999, volume=0.03,
    )
    _seed_leg(
        control, claim_id=claim_id, entry_index=3, total_legs=3, order_type="LIMIT",
        result_state="PENDING", order_ticket="733124518", price=1.1573769999999999, volume=0.02,
    )

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [
        _order_row("733124339", price=1.1575214999999999, volume=0.03, state="FILLED"),
        _order_row("733124518", price=1.1573769999999999, volume=0.02, state="FILLED"),
    ])
    _write_deals_csv(deals_csv, [
        _deal_row("900201", "733124339", position_id="55501", price=1.1575214999999999, volume=0.03),
        _deal_row("900202", "733124518", position_id="55502", price=1.1573769999999999, volume=0.02),
    ])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)

    assert result.cycle_status == "OK"
    assert result.pending_legs_seen == 2
    assert result.newly_filled == 2
    assert result.ambiguous == 0
    assert result.broker_sends == 0

    leg2 = leg_identity(claim_id, 2, total_legs=3)
    leg3 = leg_identity(claim_id, 3, total_legs=3)
    assert control.get_leg_receipt(leg2).result_state == "FILLED"
    assert control.get_leg_receipt(leg3).result_state == "FILLED"
    assert control.get_leg_receipt(leg_identity(claim_id, 1, total_legs=3)).result_state == "FILLED"
    # ZERO broker sends throughout.
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# active pending order remains PENDING.
# ---------------------------------------------------------------------------


def test_active_pending_order_remains_pending(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-still-open"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="1001")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("1001", state="PLACED")])
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.pending_still_open == 1
    assert result.newly_filled == 0
    assert result.ambiguous == 0
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# pending -> FILLED.
# ---------------------------------------------------------------------------


def test_pending_transitions_to_filled_with_authoritative_deal_evidence(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-fills"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="2002")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("2002", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("3003", "2002", position_id="4004")])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.newly_filled == 1
    receipt = control.get_leg_receipt(leg_id)
    assert receipt.result_state == "FILLED"
    assert receipt.deal_ticket == "3003"
    assert receipt.position_ticket == "4004"
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# pending -> cancelled/expired/rejected from authoritative evidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mt5_state", "expected_leg_state"),
    [("CANCELED", "CANCELLED"), ("EXPIRED", "EXPIRED"), ("REJECTED", "REJECTED")],
)
def test_pending_transitions_to_terminal_nonfill_states(tmp_path: Path, mt5_state: str, expected_leg_state: str) -> None:
    control, _ = _control(tmp_path)
    claim_id = f"EAC-test-{mt5_state.lower()}"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="5005")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("5005", state=mt5_state)])
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.newly_cancelled_or_rejected == 1
    assert control.get_leg_receipt(leg_id).result_state == expected_leg_state
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# missing evidence remains fail-closed; disappearance alone is not
# evidence of anything.
# ---------------------------------------------------------------------------


def test_missing_evidence_fails_closed_and_leg_stays_pending(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-missing"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="6006")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    # The ticket simply is not present in this cycle's export.
    _write_orders_csv(orders_csv, [_order_row("9999999", state="PLACED")])
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert result.newly_filled == 0
    assert result.newly_cancelled_or_rejected == 0
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"  # never guessed at.
    assert control.transport.calls == []


def test_disappearance_alone_does_not_imply_cancellation(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-disappeared"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="7007")

    # An EMPTY export -- as if the ticket vanished entirely from this
    # cycle's evidence (e.g. outside the history lookback window). This
    # must NEVER be treated as proof of cancellation.
    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [])
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert result.newly_cancelled_or_rejected == 0
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"


def test_filled_order_state_without_matching_deal_is_ambiguous(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-nodeal"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="8008")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("8008", state="FILLED")])
    _write_deals_csv(deals_csv, [])  # no corresponding deal row at all.

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert result.newly_filled == 0
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"  # never a fabricated fill price.


# ---------------------------------------------------------------------------
# wrong account / wrong ticket / wrong symbol/side rejected.
# ---------------------------------------------------------------------------


def test_wrong_account_never_discovers_the_leg(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-wrongaccount"
    _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="1111", account=ACCOUNT)

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("1111", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("2222", "1111")])

    result = run_reconciliation_cycle(
        control, account="99999999", orders_csv=orders_csv, deals_csv=deals_csv, now=NOW
    )
    assert result.pending_legs_seen == 0  # the leg belongs to a DIFFERENT account.


def test_wrong_ticket_in_evidence_is_ambiguous_never_reconciled(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-wrongticket"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="3333")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    # Evidence for a DIFFERENT ticket only.
    _write_orders_csv(orders_csv, [_order_row("4444", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("5555", "4444")])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"


def test_wrong_symbol_evidence_never_reconciles_the_leg(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-wrongsymbol"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="6666", symbol="EURUSD")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    # Same ticket number, but for a DIFFERENT symbol -- never reconcile
    # one leg from evidence that doesn't genuinely match it.
    _write_orders_csv(orders_csv, [_order_row("6666", symbol="GBPUSD", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("7777", "6666", symbol="GBPUSD")])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"


def test_wrong_side_evidence_never_reconciles_the_leg(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-wrongside"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="8888", action="BUY")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("8888", side="SELL", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("9999", "8888", side="SELL")])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.ambiguous == 1
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"


def test_never_reconciles_one_leg_from_another_legs_evidence(tmp_path: Path) -> None:
    """Two same-symbol/same-side pending LIMIT legs (exactly the real
    incident's own shape) -- each must be matched by its OWN order_ticket
    only, never cross-matched."""
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-crossmatch"
    leg_a = _seed_leg(
        control, claim_id=claim_id, entry_index=1, total_legs=2, order_ticket="111100", price=1.1000, volume=0.02,
    )
    leg_b = _seed_leg(
        control, claim_id=claim_id, entry_index=2, total_legs=2, order_ticket="111200", price=1.0900, volume=0.03,
    )

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    # Only leg_a's ticket has evidence -- leg_b's must remain untouched
    # and never accidentally match leg_a's deal/order rows.
    _write_orders_csv(orders_csv, [_order_row("111100", price=1.1000, volume=0.02, state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("300001", "111100", price=1.1000, volume=0.02)])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert control.get_leg_receipt(leg_a).result_state == "FILLED"
    assert control.get_leg_receipt(leg_b).result_state == "PENDING"  # untouched -- no evidence for IT.
    assert result.newly_filled == 1
    assert result.pending_still_open + result.ambiguous == 1  # leg_b has no evidence -> ambiguous.


# ---------------------------------------------------------------------------
# restart idempotency; already-FILLED legs untouched.
# ---------------------------------------------------------------------------


def test_restart_idempotency_second_cycle_is_a_pure_report(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-restart"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="121212")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("121212", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("131313", "121212")])

    first = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert first.newly_filled == 1
    first_receipt = control.get_leg_receipt(leg_id)

    # A "restart" is simulated by building a BRAND NEW control instance
    # against the SAME on-disk registry -- proving persisted state, not
    # in-memory state, drives idempotency.
    control_after_restart = SER8DemoOrderSendControl(registry=control.registry, transport=FakeDemoOrderTransport())
    second = run_reconciliation_cycle(
        control_after_restart, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW + timedelta(minutes=5)
    )
    # The leg is no longer PENDING, so it is not even in this cycle's
    # discovery set -- zero mutation, zero broker action.
    assert second.pending_legs_seen == 0
    second_receipt = control_after_restart.get_leg_receipt(leg_id)
    assert second_receipt.result_state == first_receipt.result_state == "FILLED"
    assert second_receipt.receipt_hash == first_receipt.receipt_hash  # byte-identical, never re-written.
    assert control_after_restart.transport.calls == []


def test_already_filled_leg_is_never_touched_by_a_cycle(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-alreadyfilled"
    leg_id = _seed_leg(
        control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="141414", result_state="FILLED",
        deal_ticket="1", position_ticket="2",
    )
    before = control.get_leg_receipt(leg_id)

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("141414", state="CANCELED")])  # would be wrong if it ever ran.
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.pending_legs_seen == 0  # never discovered -- not PENDING.
    after = control.get_leg_receipt(leg_id)
    assert after.receipt_hash == before.receipt_hash


# ---------------------------------------------------------------------------
# multiple claims; multiple pending legs; zero duplicate sends.
# ---------------------------------------------------------------------------


def test_multiple_claims_and_multiple_pending_legs_reconciled_independently(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_1 = "EAC-multi-claim-1"
    claim_2 = "EAC-multi-claim-2"
    leg_1a = _seed_leg(control, claim_id=claim_1, entry_index=1, total_legs=2, order_ticket="211", price=1.10)
    leg_1b = _seed_leg(control, claim_id=claim_1, entry_index=2, total_legs=2, order_ticket="212", price=1.09)
    leg_2a = _seed_leg(control, claim_id=claim_2, entry_index=1, total_legs=1, order_ticket="221")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [
        _order_row("211", price=1.10, state="FILLED"),
        _order_row("212", price=1.09, state="CANCELED"),
        _order_row("221", state="FILLED"),
    ])
    _write_deals_csv(deals_csv, [
        _deal_row("311", "211", price=1.10),
        _deal_row("321", "221"),
    ])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.pending_legs_seen == 3
    assert result.newly_filled == 2
    assert result.newly_cancelled_or_rejected == 1
    assert control.get_leg_receipt(leg_1a).result_state == "FILLED"
    assert control.get_leg_receipt(leg_1b).result_state == "CANCELLED"
    assert control.get_leg_receipt(leg_2a).result_state == "FILLED"
    assert control.transport.calls == []


def test_zero_duplicate_sends_across_repeated_cycles(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-noduplicate"
    _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="515151")

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("515151", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("616161", "515151")])

    for cycle in range(5):
        run_reconciliation_cycle(
            control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW + timedelta(minutes=cycle)
        )
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# dry-run causes no mutation.
# ---------------------------------------------------------------------------


def test_dry_run_causes_no_mutation(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-dryrun"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="717171")
    before = control.get_leg_receipt(leg_id)

    orders_csv = tmp_path / "orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_orders_csv(orders_csv, [_order_row("717171", state="FILLED")])
    _write_deals_csv(deals_csv, [_deal_row("818181", "717171")])

    result = run_reconciliation_cycle(
        control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW, dry_run=True
    )
    assert result.newly_filled == 1  # reported as WOULD-reconcile...
    after = control.get_leg_receipt(leg_id)
    assert after.receipt_hash == before.receipt_hash  # ...but nothing was actually written.
    assert after.result_state == "PENDING"
    assert control.transport.calls == []


# ---------------------------------------------------------------------------
# malformed/missing export CSVs fail the WHOLE cycle closed.
# ---------------------------------------------------------------------------


def test_missing_orders_csv_fails_the_whole_cycle_closed(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    claim_id = "EAC-test-missingfile"
    leg_id = _seed_leg(control, claim_id=claim_id, entry_index=1, total_legs=1, order_ticket="919191")

    orders_csv = tmp_path / "does-not-exist-orders.csv"
    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(deals_csv, [])

    result = run_reconciliation_cycle(control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW)
    assert result.cycle_status.startswith("EVIDENCE_ERROR")
    assert control.get_leg_receipt(leg_id).result_state == "PENDING"
    assert control.transport.calls == []


def test_orders_csv_missing_required_columns_fails_closed(tmp_path: Path) -> None:
    orders_csv = tmp_path / "orders.csv"
    orders_csv.write_text("time_msc,account_login\n1,67206924\n", encoding="utf-8")
    with pytest.raises(ReconciliationEvidenceError, match="missing required fields"):
        load_order_history(orders_csv)


def test_deals_csv_missing_required_columns_fails_closed(tmp_path: Path) -> None:
    deals_csv = tmp_path / "deals.csv"
    deals_csv.write_text("time_msc,account_login\n1,67206924\n", encoding="utf-8")
    with pytest.raises(ReconciliationEvidenceError, match="missing required fields"):
        load_deal_history(deals_csv)


# ---------------------------------------------------------------------------
# safety: never a broker send, never a transport reference at all.
# ---------------------------------------------------------------------------


def test_evaluate_pending_leg_never_references_transport() -> None:
    import inspect

    source = inspect.getsource(evaluate_pending_leg)
    # Strip the function's own docstring first -- it legitimately explains,
    # in prose, that this function never touches a transport; only the
    # CODE body must never mention one.
    first_quote = source.index('"""')
    second_quote = source.index('"""', first_quote + 3) + 3
    code_only = source[:first_quote] + source[second_quote:]
    # DemoOrderTransportResult (the immutable evidence type this whole
    # flow is built around) legitimately contains "transport" as a
    # substring -- what actually matters is that no `.transport` attribute
    # is ever accessed and `.send(` is never called.
    assert ".transport" not in code_only
    assert ".send(" not in code_only


def test_reconciliation_module_never_imports_metatrader5_or_constructs_authorization() -> None:
    source = Path(
        Path(__file__).resolve().parent.parent / "src" / "trademind" / "ser8_mt5_execution_reconciliation.py"
    ).read_text(encoding="utf-8")
    assert "import MetaTrader5" not in source
    for forbidden in (
        "SER8ExecutionAuthorizationControl(", "SER8ExecutionAuthorizationClaimControl(",
        "ExecutionAuthorizationV1(", "ExecutionAuthorizationClaimV1(", "DemoOrderTransport.send",
    ):
        assert forbidden not in source


def test_legacy_inventory_maps_known_gtc_and_flags_unmapped_active_order(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    leg_id = _seed_leg(
        control, claim_id="EAC-legacy", entry_index=1, total_legs=1,
        order_ticket="111", order_type="LIMIT", result_state="PENDING",
    )
    orders_csv = tmp_path / "orders-extended.csv"
    extended_header = ORDER_HEADER.rstrip("\n") + ",time_type,expiration_time_msc,comment\n"
    setup_msc = int((NOW - timedelta(minutes=20)).timestamp() * 1000)
    orders_csv.write_text(
        extended_header
        + f"1,{ACCOUNT},111,EURUSD,{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,BUY_LIMIT,0.01,1.15,PLACED,"
          f"{setup_msc},0,0,GTC,0,SER8:legacy\n"
        + f"1,{ACCOUNT},222,EURUSD,{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,BUY_LIMIT,0.02,1.14,PLACED,"
          f"{setup_msc},0,0,GTC,0,SER8:unknown\n",
        encoding="utf-8",
    )
    inventory = inventory_active_pending_orders(
        control, account=ACCOUNT, order_history=load_order_history(orders_csv), now=NOW
    )
    assert inventory[0].leg_id == leg_id
    assert inventory[0].status == "MAPPED_LEGACY_GTC"
    assert inventory[0].age_seconds == pytest.approx(1200.0)
    assert inventory[1].leg_id is None
    assert inventory[1].status == "UNMAPPED_ACTIVE_PENDING_ORDER"
    assert control.transport.calls == []


def test_broker_accept_local_receipt_crash_recovers_unknown_once_by_request_identity(tmp_path: Path) -> None:
    control, _ = _control(tmp_path)
    leg_id = _seed_leg(
        control, claim_id="EAC-crash", entry_index=1, total_legs=1,
        order_ticket="", order_type="LIMIT", result_state="UNKNOWN",
    )
    request = control.get_leg_request(leg_id)
    orders_csv = tmp_path / "orders-crash.csv"
    deals_csv = tmp_path / "deals-crash.csv"
    extended_header = ORDER_HEADER.rstrip("\n") + ",time_type,expiration_time_msc,comment\n"
    orders_csv.write_text(
        extended_header
        + f"1,{ACCOUNT},333,EURUSD,{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,BUY_LIMIT,0.01,1.15,PLACED,"
          f"1,0,0,GTC,0,{request.comment}\n",
        encoding="utf-8",
    )
    _write_deals_csv(deals_csv, [])
    result = run_reconciliation_cycle(
        control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW
    )
    assert result.unknown_legs_seen == 1
    assert result.unknown_recovered == 1
    recovered = control.get_leg_receipt(leg_id)
    assert recovered.result_state == "PENDING"
    assert recovered.order_ticket == "333"
    assert recovered.broker_send_performed is True
    assert control.transport.calls == []

    again = run_reconciliation_cycle(
        control, account=ACCOUNT, orders_csv=orders_csv, deals_csv=deals_csv, now=NOW + timedelta(seconds=1)
    )
    assert again.unknown_legs_seen == 0
    assert control.get_leg_receipt(leg_id).order_ticket == "333"
    assert control.transport.calls == []
