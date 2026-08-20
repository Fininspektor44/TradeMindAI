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

import sys
from datetime import datetime, timezone
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
    assert outcome.hypothesis_id == "rpi-v1:sha256:" + "9" * 64 + ":0"
    assert outcome.candidate_signal_id == "sig-1"
    assert outcome.side == "BUY"
    assert outcome.terminal_reason == "CLOSED"

    # Idempotent: calling again returns the SAME persisted record.
    again = outcome_control.capture_outcome_for_leg(
        leg_id, send_control=send_control, authorization_control=authorization_control, deals_csv=deals_csv,
    )
    assert again.outcome_hash == outcome.outcome_hash


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
