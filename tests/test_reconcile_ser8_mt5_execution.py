"""Tests for scripts/reconcile_ser8_mt5_execution.py -- SER8 AUTOMATIC MT5
RECONCILIATION V1's own CLI entrypoint.

Builds its own self-contained, already-persisted PENDING execution legs
using ONLY SER8DemoOrderSendControl's own production persistence methods
(never hand-edited SQLite, never the full research/authorization/claim
chain, which this CLI's own logic never touches).

This file does not import test helpers from sibling test files (consistent
with this session's own established convention).
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

reconcile_module = importlib.import_module("reconcile_ser8_mt5_execution")

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

ACCOUNT = "67206924"
NOW = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)


class _FakeAuth:
    gate_hash = "sha256:" + "a" * 64


def _seed_one_pending_leg(control: SER8DemoOrderSendControl, *, claim_id: str, order_ticket: str) -> str:
    leg_id = leg_identity(claim_id, 1, total_legs=1)
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id=f"EOP-{leg_id}", claim_id=claim_id, authorization_id="EA-x",
        decision_id="RD-x", candidate_signal_id="sig-x", demo_account_id=ACCOUNT, symbol="EURUSD", action="BUY",
        legs=(
            DemoOrderPlanLegV1(
                entry_index=1, leg_id=leg_id, order_type="LIMIT", planned_price=1.15, effective_entry_price=1.15,
                allocation=1.0, volume=0.01, sl=1.0, tp=1.3,
            ),
        ),
    )
    control._persist_plan(plan, created_at=NOW.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=claim_id, entry_index=1, claim_id=leg_id,
        authorization_id="EA-x", demo_account_id=ACCOUNT, symbol="EURUSD", action="BUY", order_type="LIMIT",
        volume=0.01, price=1.15, sl=1.0, tp=1.3, magic=DEMO_EXECUTOR_MAGIC_NUMBER, comment="SER8:x",
    )
    control._reserve_leg_attempt(
        leg_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=1,
        attempt_id="EAO-x", request=request, demo_authorization=_FakeAuth(), captured_at=NOW,
    )
    control._finalize(
        claim_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=1,
        authorization_id="EA-x", demo_gate_hash=_FakeAuth.gate_hash, request_hash=request.request_hash,
        attempt_id="EAO-x", result_state="PENDING", recorded_at=NOW.isoformat(),
        retcode=10009, retcode_description="done", order_ticket=order_ticket, deal_ticket="0", position_ticket="0",
        requested_volume=0.01, requested_price=1.15, filled_volume=None, filled_price=None,
    )
    return leg_id


def _write_exports(export_dir: Path, *, order_ticket: str, deal_ticket: str = "1", state: str = "FILLED") -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / f"mt5_risk_orders_utc_{ACCOUNT}.csv").write_text(
        "time_msc,account_login,order_ticket,symbol,magic,side,order_type,volume,price,state,"
        "time_setup_msc,time_done_msc,position_id\n"
        f"1,{ACCOUNT},{order_ticket},EURUSD,{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,ORDER_TYPE_BUY_LIMIT,0.01,1.15,{state},1,2,500\n",
        encoding="utf-8",
    )
    (export_dir / f"mt5_risk_deals_utc_{ACCOUNT}.csv").write_text(
        "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,volume,price,entry,time_deal_msc\n"
        f"1,{ACCOUNT},{deal_ticket},{order_ticket},500,EURUSD,{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,0.01,1.15,IN,2\n",
        encoding="utf-8",
    )


def test_main_end_to_end_reconciles_a_pending_leg_to_filled(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    seed_transport = FakeDemoOrderTransport()
    seed_control = SER8DemoOrderSendControl(registry=registry, transport=seed_transport)
    leg_id = _seed_one_pending_leg(seed_control, claim_id="EAC-cli-test", order_ticket="12345")

    export_dir = tmp_path / "mt5"
    _write_exports(export_dir, order_ticket="12345", deal_ticket="99999")

    exit_code = reconcile_module.main(
        ["--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once"]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "cycle_status=OK" in output
    assert "newly_filled=1" in output
    assert "broker_sends=0" in output

    fresh_control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    assert fresh_control.get_leg_receipt(leg_id).result_state == "FILLED"
    assert seed_transport.calls == []


def test_dry_run_flag_causes_no_mutation(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    leg_id = _seed_one_pending_leg(control, claim_id="EAC-cli-dryrun", order_ticket="22222")

    export_dir = tmp_path / "mt5"
    _write_exports(export_dir, order_ticket="22222", deal_ticket="88888")

    exit_code = reconcile_module.main(
        ["--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once", "--dry-run"]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "DRY-RUN" in output
    assert "newly_filled=1" in output

    fresh_control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    assert fresh_control.get_leg_receipt(leg_id).result_state == "PENDING"  # unchanged.


def test_once_mode_runs_exactly_one_cycle_and_returns(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    _seed_one_pending_leg(control, claim_id="EAC-cli-once", order_ticket="33333")
    export_dir = tmp_path / "mt5"
    _write_exports(export_dir, order_ticket="33333")

    exit_code = reconcile_module.main(
        ["--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once"]
    )
    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    cycle_lines = [line for line in lines if "SER8 MT5 RECONCILIATION CYCLE" in line]
    assert len(cycle_lines) == 1  # exactly one cycle -- --once never loops.


def test_lock_file_prevents_overlapping_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "ser8_registry.db"
    HypothesisRegistry(db_path)
    export_dir = tmp_path / "mt5"
    export_dir.mkdir()
    lock_path = tmp_path / "held.lock"
    lock_path.write_text("pid=999999 started_at=stale\n", encoding="utf-8")

    exit_code = reconcile_module.main(
        [
            "--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once",
            "--lock-file", str(lock_path),
        ]
    )
    assert exit_code == 3  # refused -- never silently proceeds while a lock is held.


def test_lock_file_released_after_a_clean_run(tmp_path: Path) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    export_dir = tmp_path / "mt5"
    export_dir.mkdir()
    (export_dir / f"mt5_risk_orders_utc_{ACCOUNT}.csv").write_text(
        "time_msc,account_login,order_ticket,symbol,magic,side,order_type,volume,price,state,"
        "time_setup_msc,time_done_msc,position_id\n",
        encoding="utf-8",
    )
    (export_dir / f"mt5_risk_deals_utc_{ACCOUNT}.csv").write_text(
        "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,volume,price,entry,time_deal_msc\n",
        encoding="utf-8",
    )
    lock_path = tmp_path / "reconcile.lock"

    exit_code = reconcile_module.main(
        [
            "--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once",
            "--lock-file", str(lock_path),
        ]
    )
    assert exit_code == 0
    assert not lock_path.exists()  # released -- a second run must not be blocked.

    exit_code_2 = reconcile_module.main(
        [
            "--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once",
            "--lock-file", str(lock_path),
        ]
    )
    assert exit_code_2 == 0


def test_build_arg_parser_requires_db_account_and_export_dir() -> None:
    parser = reconcile_module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--db", "reg.db", "--account", ACCOUNT, "--mt5-export-dir", "mt5"])
    assert args.dry_run is False
    assert args.once is False
    assert args.poll_interval_seconds == reconcile_module.DEFAULT_POLL_INTERVAL_SECONDS


def test_main_never_constructs_authorization_claim_or_order() -> None:
    source = Path(reconcile_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "SER8ExecutionAuthorizationControl(", "SER8ExecutionAuthorizationClaimControl(",
        "ExecutionAuthorizationV1(", "ExecutionAuthorizationClaimV1(",
    ):
        assert forbidden not in source


def test_main_wires_a_transport_that_would_explode_if_ever_called() -> None:
    import inspect

    source = inspect.getsource(reconcile_module.main)
    assert "FakeDemoOrderTransport()" in source
    assert "result_factory" not in source


def test_ambiguous_result_still_exits_nonzero_but_never_raises(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    _seed_one_pending_leg(control, claim_id="EAC-cli-ambiguous", order_ticket="44444")
    export_dir = tmp_path / "mt5"
    # No matching evidence at all for ticket 44444.
    _write_exports(export_dir, order_ticket="99999999")

    exit_code = reconcile_module.main(
        ["--db", str(db_path), "--account", ACCOUNT, "--mt5-export-dir", str(export_dir), "--once"]
    )
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ambiguous=1" in output
