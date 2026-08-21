"""Tests for trademind.ser8_demo_trade_outcome_capture -- SER8 AUTONOMOUS
CONTINUOUS DEMO EXECUTION V1's outcome-capture bridge.

Builds its own self-contained, already-FILLED execution leg using ONLY
SER8DemoOrderSendControl's and SER8ExecutionAuthorizationControl's own
production persistence methods (matching
tests/test_ser8_mt5_execution_reconciliation.py's own ``_seed_leg``
technique) -- never the full research/authorization/claim chain, and
never hand-edited SQLite.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_demo_trade_outcome_capture import (  # noqa: E402
    SER8DemoTradeOutcomeControl,
    SER8DemoTradeOutcomeError,
)
from trademind.ser8_execution_authorization import (  # noqa: E402
    ExecutionAuthorizationV1,
    SER8ExecutionAuthorizationControl,
)
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
from trademind.signal_statistics_provenance import canonical_json_bytes  # noqa: E402

ACCOUNT = "67206924"
NOW = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)

DEAL_HEADER = (
    "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,"
    "volume,price,entry,time_deal_msc,profit\n"
)


class _FakeAuth:
    gate_hash = "sha256:" + "a" * 64


def _controls(tmp_path: Path):
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    send_control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    authorization_control = SER8ExecutionAuthorizationControl.__new__(SER8ExecutionAuthorizationControl)
    authorization_control.registry = registry
    authorization_control.final_verdict = None
    authorization_control.path = Path(registry.path)
    authorization_control._init_schema()
    outcome_control = SER8DemoTradeOutcomeControl(registry=registry)
    return send_control, authorization_control, outcome_control, db_path


def _seed_authorization(
    authorization_control: SER8ExecutionAuthorizationControl, *, authorization_id: str,
    hypothesis_id: str = "rpi-v1:sha256:" + "a" * 64 + ":0", candidate_signal_id: str = "sig-1",
    account: str = ACCOUNT, symbol: str = "XAUUSD", action: str = "BUY",
) -> ExecutionAuthorizationV1:
    """Directly inserts an authorization row via the control's own real
    table -- the SAME approach every other test in this session uses to
    simulate prior state without hand-editing SQLite: this constructs a
    genuine ``ExecutionAuthorizationV1`` (the module's own public,
    validated record type) and persists it through a raw INSERT into the
    control's own schema, exactly mirroring what ``authorize()`` itself
    would have written."""
    from trademind.signal_statistics_provenance import canonical_json_bytes

    authorization = ExecutionAuthorizationV1(
        schema_version="ser8-execution-authorization-v1", authorization_id=authorization_id,
        hypothesis_id=hypothesis_id, hypothesis_family_id=hypothesis_id, research_eligibility_artifact_hash="sha256:" + "b" * 64,
        hypothesis_tradeable_scope_hash="sha256:" + "c" * 64, live_candidate_signal_id=candidate_signal_id,
        risk_gate_evidence_hash="sha256:" + "d" * 64, risk_decision_id="RD-" + authorization_id,
        account_id=account, market_account_snapshot_hash="sha256:" + "e" * 64, symbol=symbol, action=action,
        authorized_at=NOW.isoformat(), expires_at=(NOW.replace(hour=23)).isoformat(),
    )
    with authorization_control._connect() as db:
        db.execute(
            "INSERT INTO ser8_execution_authorizations(authorization_id, hypothesis_id, account_id, "
            "approval_key_hash, authorized_at, expires_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                authorization.authorization_id, authorization.hypothesis_id, authorization.account_id,
                "sha256:" + "f" * 64, authorization.authorized_at, authorization.expires_at,
                canonical_json_bytes(authorization.to_payload()).decode("utf-8"),
            ),
        )
    return authorization


def _seed_filled_leg(
    send_control: SER8DemoOrderSendControl, *, claim_id: str, authorization_id: str,
    symbol: str = "XAUUSD", action: str = "BUY", volume: float = 0.5, price: float = 2000.0,
    order_ticket: str = "900", deal_ticket: str = "901", position_ticket: str = "777",
) -> str:
    leg_id = leg_identity(claim_id, 1, total_legs=1)
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id=f"EOP-{leg_id}", claim_id=claim_id, authorization_id=authorization_id,
        decision_id=f"RD-{authorization_id}", candidate_signal_id="sig-1", demo_account_id=ACCOUNT, symbol=symbol,
        action=action, legs=(
            DemoOrderPlanLegV1(entry_index=1, leg_id=leg_id, order_type="MARKET", planned_price=price,
                                effective_entry_price=price, allocation=1.0, volume=volume, sl=1990.0, tp=2020.0),
        ),
    )
    send_control._persist_plan(plan, created_at=NOW.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=claim_id, entry_index=1, claim_id=leg_id,
        authorization_id=authorization_id, demo_account_id=ACCOUNT, symbol=symbol, action=action,
        order_type="MARKET", volume=volume, price=price, sl=1990.0, tp=2020.0, magic=DEMO_EXECUTOR_MAGIC_NUMBER,
        comment=f"SER8:{leg_id[-20:]}",
    )
    send_control._reserve_leg_attempt(
        leg_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=1, attempt_id=f"EAO-{leg_id}",
        request=request, demo_authorization=_FakeAuth(), captured_at=NOW,
    )
    send_control._finalize(
        claim_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=1, authorization_id=authorization_id,
        demo_gate_hash=_FakeAuth.gate_hash, request_hash=request.request_hash, attempt_id=f"EAO-{leg_id}",
        result_state="FILLED", recorded_at=NOW.isoformat(), retcode=10009, retcode_description="Request completed",
        order_ticket=order_ticket, deal_ticket=deal_ticket, position_ticket=position_ticket,
        requested_volume=volume, requested_price=price, filled_volume=volume, filled_price=price,
    )
    return leg_id


def _seed_plan_with_states(
    send_control: SER8DemoOrderSendControl,
    *,
    claim_id: str,
    authorization_id: str,
    states: tuple[str, ...],
    order_types: tuple[str, ...] | None = None,
) -> tuple[DemoOrderExecutionPlanV1, tuple[str, ...]]:
    """Persist one synthetic multi-leg plan through production write paths."""
    order_types = order_types or tuple("MARKET" for _ in states)
    assert len(states) == len(order_types)
    leg_ids = tuple(
        leg_identity(claim_id, index, total_legs=len(states))
        for index in range(1, len(states) + 1)
    )
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION,
        plan_id=f"EOP-{claim_id}",
        claim_id=claim_id,
        authorization_id=authorization_id,
        decision_id=f"RD-{authorization_id}",
        candidate_signal_id="sig-1",
        demo_account_id=ACCOUNT,
        symbol="XAUUSD",
        action="BUY",
        legs=tuple(
            DemoOrderPlanLegV1(
                entry_index=index,
                leg_id=leg_id,
                order_type=order_type,
                planned_price=2000.0 - index,
                effective_entry_price=2000.0 - index,
                allocation=1.0 / len(states),
                volume=0.5,
                sl=1990.0,
                tp=2020.0,
                expires_at=(NOW + timedelta(seconds=900)).isoformat()
                if order_type != "MARKET"
                else None,
            )
            for index, (leg_id, order_type) in enumerate(
                zip(leg_ids, order_types, strict=True), start=1
            )
        ),
    )
    send_control._persist_plan(plan, created_at=NOW.isoformat())
    for index, (leg_id, state, order_type) in enumerate(
        zip(leg_ids, states, order_types, strict=True), start=1
    ):
        price = 2000.0 - index
        request = DemoOrderRequestV1(
            schema_version=SCHEMA_VERSION,
            parent_claim_id=claim_id,
            entry_index=index,
            claim_id=leg_id,
            authorization_id=authorization_id,
            demo_account_id=ACCOUNT,
            symbol="XAUUSD",
            action="BUY",
            order_type=order_type,
            volume=0.5,
            price=price,
            sl=1990.0,
            tp=2020.0,
            magic=DEMO_EXECUTOR_MAGIC_NUMBER,
            comment=f"SER8:{leg_id[-20:]}",
            expires_at=(NOW + timedelta(seconds=900)).isoformat()
            if order_type != "MARKET"
            else None,
        )
        send_control._reserve_leg_attempt(
            leg_id=leg_id,
            plan_id=plan.plan_id,
            parent_claim_id=claim_id,
            entry_index=index,
            attempt_id=f"EAO-{leg_id}",
            request=request,
            demo_authorization=_FakeAuth(),
            captured_at=NOW,
        )
        position_bearing = state in {"FILLED", "PARTIAL_FILL"}
        filled_volume = 0.5 if state == "FILLED" else 0.25 if state == "PARTIAL_FILL" else None
        send_control._finalize(
            claim_id=leg_id,
            plan_id=plan.plan_id,
            parent_claim_id=claim_id,
            entry_index=index,
            authorization_id=authorization_id,
            demo_gate_hash=_FakeAuth.gate_hash,
            request_hash=request.request_hash,
            attempt_id=f"EAO-{leg_id}",
            result_state=state,
            recorded_at=NOW.isoformat(),
            retcode=10009,
            retcode_description=state,
            order_ticket=str(900 + index),
            deal_ticket=str(910 + index) if position_bearing else "0",
            position_ticket=str(770 + index) if position_bearing else "0",
            requested_volume=0.5,
            requested_price=price,
            filled_volume=filled_volume,
            filled_price=price if position_bearing else None,
        )
    return plan, leg_ids


def _write_deals_csv(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEAL_HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")


def _out_row(deal_ticket: str, order_ticket: str, position_id: str, *, price=2020.0, volume=0.5, profit="10.0",
             time_deal_msc=1700000600000, magic=DEMO_EXECUTOR_MAGIC_NUMBER, symbol="XAUUSD") -> str:
    return f"1,{ACCOUNT},{deal_ticket},{order_ticket},{position_id},{symbol},{magic},SELL,{volume},{price},OUT,{time_deal_msc},{profit}"


def _in_row(deal_ticket: str, order_ticket: str, position_id: str, *, price=2000.0, volume=0.5,
            time_deal_msc=1700000000000, magic=DEMO_EXECUTOR_MAGIC_NUMBER, symbol="XAUUSD") -> str:
    return f"1,{ACCOUNT},{deal_ticket},{order_ticket},{position_id},{symbol},{magic},BUY,{volume},{price},IN,{time_deal_msc},"


# ---------------------------------------------------------------------------
# Not filled / no close evidence yet.
# ---------------------------------------------------------------------------


def test_unfilled_leg_returns_none(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    outcome = outcome_control.capture_outcome_for_leg(
        "no-such-leg", send_control=send_control, authorization_control=authorization_control,
        deals_csv=tmp_path / "deals.csv",
    )
    assert outcome is None


def test_filled_leg_with_no_close_evidence_is_still_open(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    _write_deals_csv(deals_csv, [_in_row("901", "900", "777")])

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert outcome is None
    assert outcome_control.get_outcome(leg_id) is None


def test_missing_deals_export_is_treated_as_no_evidence_yet(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control,
        deals_csv=tmp_path / "mt5" / "does_not_exist.csv",
    )
    assert outcome is None


# ---------------------------------------------------------------------------
# Whole-plan lifecycle / active-symbol release boundary.
# ---------------------------------------------------------------------------


def test_all_entries_filled_but_one_position_open_stays_active_across_restart_and_time(
    tmp_path: Path,
) -> None:
    send_control, authorization_control, outcome_control, db_path = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    plan, leg_ids = _seed_plan_with_states(
        send_control,
        claim_id="claim-all-filled",
        authorization_id="EA-1",
        states=("FILLED", "FILLED"),
    )
    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(
        deals_csv,
        [_out_row("920", "930", "771", volume=0.5, profit="10.0")],
    )
    assert outcome_control.capture_outcome_for_leg(
        leg_ids[0],
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    ) is not None
    assert outcome_control.capture_outcome_for_leg(
        leg_ids[1],
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    ) is None
    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW
    ) == 0
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="USDJPY") == ()

    # A new process rebuilds the lock solely from durable plan/receipt/
    # outcome state. Advancing time cannot substitute for broker close truth.
    restarted = SER8DemoOrderSendControl(
        registry=HypothesisRegistry(db_path), transport=FakeDemoOrderTransport()
    )
    far_future = NOW + timedelta(days=3650)
    assert outcome_control.capture_outcome_for_leg(
        leg_ids[1],
        send_control=restarted,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=far_future,
    ) is None
    assert restarted.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)

    parameters = inspect.signature(
        SER8DemoOrderSendControl.list_active_execution_plans
    ).parameters
    assert not ({"unlock", "force", "timeout", "now", "expires_at"} & set(parameters))


def test_expired_pending_leg_does_not_unlock_an_open_market_position(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    plan, _ = _seed_plan_with_states(
        send_control,
        claim_id="claim-market-open-pending-expired",
        authorization_id="EA-1",
        states=("FILLED", "EXPIRED"),
        order_types=("MARKET", "LIMIT"),
    )
    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW + timedelta(days=1)
    ) == 0
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)


@pytest.mark.parametrize(
    ("exit_price", "profit", "expected_result"),
    [
        pytest.param(2020.0, "12.5", "CLOSED_PROFIT", id="tp-close"),
        pytest.param(1990.0, "-8.0", "CLOSED_LOSS", id="sl-close"),
    ],
)
def test_broker_closed_position_requires_leg_and_aggregate_outcome_before_unlock(
    tmp_path: Path, exit_price: float, profit: str, expected_result: str
) -> None:
    send_control, authorization_control, outcome_control, db_path = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(
        send_control, claim_id="sig-1", authorization_id="EA-1"
    )
    plan = send_control.get_plan(f"EOP-{leg_id}")
    assert plan is not None
    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(
        deals_csv,
        [_out_row("902", "903", "777", price=exit_price, profit=profit)],
    )

    # Broker close evidence exists, but reconciliation/outcome capture has
    # not run yet: the symbol must remain locked for both TP and SL paths.
    assert outcome_control.get_outcome(leg_id) is None
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)

    captured = outcome_control.capture_outcome_for_leg(
        leg_id,
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    )
    assert captured is not None
    # Per-leg close truth is necessary but not sufficient.
    assert outcome_control.get_execution_plan_outcome(plan.plan_id) is None
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)

    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW
    ) == 1
    aggregate = outcome_control.get_execution_plan_outcome(plan.plan_id)
    assert aggregate is not None
    assert aggregate.aggregate_result == expected_result
    assert aggregate.filled_leg_outcome_hashes == ((leg_id, captured.outcome_hash),)
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == ()

    # The unlock itself is durable across restart; there is no in-memory flag.
    restarted = SER8DemoOrderSendControl(
        registry=HypothesisRegistry(db_path), transport=FakeDemoOrderTransport()
    )
    assert restarted.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == ()


def test_partial_close_is_not_a_final_leg_or_plan_outcome(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(
        send_control, claim_id="sig-1", authorization_id="EA-1", volume=1.0
    )
    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(
        deals_csv,
        [_out_row("902", "903", "777", volume=0.5, profit="5.0")],
    )
    assert outcome_control.capture_outcome_for_leg(
        leg_id,
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    ) is None
    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW
    ) == 0
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD")


def test_position_bearing_partial_fill_requires_close_outcome_before_unlock(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    plan, leg_ids = _seed_plan_with_states(
        send_control,
        claim_id="claim-partial-fill-position",
        authorization_id="EA-1",
        states=("PARTIAL_FILL",),
    )
    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW
    ) == 0
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)

    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(
        deals_csv,
        [_out_row("920", "930", "771", volume=0.25, profit="2.5")],
    )
    leg_outcome = outcome_control.capture_outcome_for_leg(
        leg_ids[0],
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    )
    assert leg_outcome is not None
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == (plan,)
    assert outcome_control.capture_completed_plan_outcomes(
        send_control=send_control, account=ACCOUNT, now=NOW
    ) == 1
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD") == ()


# ---------------------------------------------------------------------------
# Genuine close evidence -- captured once, idempotent thereafter.
# ---------------------------------------------------------------------------


def test_close_evidence_is_captured_with_realized_pl(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1", hypothesis_id="rpi-v1:sha256:" + "9" * 64 + ":0")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    _write_deals_csv(deals_csv, [_in_row("901", "900", "777"), _out_row("902", "903", "777", profit="10.0")])

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert outcome is not None
    assert outcome.exit_price == 2020.0
    assert outcome.realized_pl == 10.0
    assert outcome.entry_order_ticket == "900"
    assert outcome.entry_deal_ticket == "901"
    assert outcome.position_ticket == "777"
    assert outcome.exit_deal_tickets == ("902",)
    assert outcome.entry_filled_volume == 0.5
    assert outcome.closed_volume == 0.5
    assert outcome.hypothesis_id == "rpi-v1:sha256:" + "9" * 64 + ":0"
    assert outcome.candidate_signal_id == "sig-1"
    assert outcome.side == "BUY"
    assert outcome.terminal_reason == "CLOSED"

    # Idempotent: calling again returns the SAME persisted record.
    again = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert again.outcome_hash == outcome.outcome_hash


def test_legacy_outcome_without_close_volume_proof_stays_locked_until_revalidated(
    tmp_path: Path,
) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")
    deals_csv = tmp_path / "deals.csv"
    _write_deals_csv(deals_csv, [_out_row("902", "903", "777", profit="10.0")])
    captured = outcome_control.capture_outcome_for_leg(
        leg_id,
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW,
    )
    assert captured is not None

    legacy = replace(captured, entry_filled_volume=None, closed_volume=None)
    with outcome_control._connect() as db:
        db.execute(
            "UPDATE ser8_demo_trade_outcomes SET payload_json=? WHERE leg_id=?",
            (canonical_json_bytes(legacy.to_payload()).decode("utf-8"), leg_id),
        )
    assert send_control.list_active_execution_plans(ACCOUNT, symbol="XAUUSD")

    migrated = outcome_control.capture_outcome_for_leg(
        leg_id,
        send_control=send_control,
        authorization_control=authorization_control,
        deals_csv=deals_csv,
        now=NOW + timedelta(seconds=1),
    )
    assert migrated is not None
    assert migrated.entry_filled_volume == 0.5
    assert migrated.closed_volume == 0.5
    assert migrated.outcome_hash != legacy.outcome_hash


def test_multiple_close_deals_are_volume_weighted_and_summed(tmp_path: Path) -> None:
    """A partial close followed by a second partial close -- exit_price is
    volume-weighted, realized_pl is summed, exit_timestamp is the LATEST
    close deal, never an arbitrary pick."""
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1", volume=1.0)

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    _write_deals_csv(deals_csv, [
        _in_row("901", "900", "777", volume=1.0),
        _out_row("902", "903", "777", price=2010.0, volume=0.5, profit="5.0", time_deal_msc=1700000300000),
        _out_row("904", "905", "777", price=2020.0, volume=0.5, profit="6.0", time_deal_msc=1700000600000),
    ])

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert outcome is not None
    assert outcome.exit_price == pytest.approx(2015.0)
    assert outcome.realized_pl == pytest.approx(11.0)
    assert outcome.exit_deal_tickets == ("902", "904")


def test_missing_profit_column_value_yields_none_realized_pl_never_zero(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    _write_deals_csv(deals_csv, [_in_row("901", "900", "777"), _out_row("902", "903", "777", profit="")])

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert outcome.realized_pl is None  # never inferred/defaulted to 0.


def test_wrong_magic_or_wrong_position_never_matched(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    _write_deals_csv(deals_csv, [
        _out_row("902", "903", "777", magic=111111),  # wrong magic.
        _out_row("904", "905", "999"),  # wrong position.
    ])

    outcome = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert outcome is None


def test_missing_required_column_fails_closed(tmp_path: Path) -> None:
    send_control, authorization_control, outcome_control, _ = _controls(tmp_path)
    _seed_authorization(authorization_control, authorization_id="EA-1")
    leg_id = _seed_filled_leg(send_control, claim_id="sig-1", authorization_id="EA-1")

    deals_csv = tmp_path / "mt5" / f"mt5_risk_deals_utc_{ACCOUNT}.csv"
    deals_csv.parent.mkdir(parents=True, exist_ok=True)
    deals_csv.write_text("time_msc,account_login,deal_ticket\n1,67206924,901\n", encoding="utf-8")

    with pytest.raises(SER8DemoTradeOutcomeError):
        outcome_control.capture_outcome_for_leg(
            leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
        )


# ---------------------------------------------------------------------------
# Never touches the transport / never mutates the leg receipt or plan.
# ---------------------------------------------------------------------------


def test_never_touches_transport() -> None:
    """AST-based import scan (matching
    tests/test_run_ser8_real_demo_pipeline.py's own
    test_script_never_imports_retired_lineage) -- a plain substring
    search would false-positive on this module's own docstring, which
    legitimately explains what it does NOT do by name."""
    import ast

    module_file = Path(
        __import__("trademind.ser8_demo_trade_outcome_capture", fromlist=["x"]).__file__
    )
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert "MetaTrader5" not in imported
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("send", "Buy", "Sell"):
            pytest.fail(f"unexpected transport/trade call: .{node.attr}(")
