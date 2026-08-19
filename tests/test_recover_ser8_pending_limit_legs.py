"""Tests for scripts/recover_ser8_pending_limit_legs.py -- SER8 REAL PLAN
RECOVERY CLI V1.

Builds its own self-contained, already-persisted execution plan/leg
receipts using ONLY SER8DemoOrderSendControl's own production persistence
methods (_persist_plan / _reserve_leg_attempt / _finalize -- the exact
same technique this repository's own test suite already uses to simulate
a prior send(), e.g. test_ser8_mt5_demo_order_send.py's
test_reserved_but_never_finalized_leg_self_heals_to_unknown_without_resend
and _persist_legacy_malformed_plan) -- never hand-edited SQLite, and
never the full research/authorization/claim chain (this CLI's own logic
only ever reads/writes the ser8_mt5_demo_order_leg_receipts table, so
that heavier chain is unnecessary here and is already exhaustively proven
elsewhere, e.g. tests/test_ser8_mt5_demo_order_send.py).

Reproduces the EXACT real incident this task's own spec describes: claim
root EAC-67206924-2e40988a6cd689d6, account 67206924, leg #1 FILLED
(MARKET), legs #2/#3 legacy-MALFORMED (LIMIT, broker-accepted pending).

This file does not import test helpers from sibling test files (consistent
with this session's own established convention).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

recovery_module = importlib.import_module("recover_ser8_pending_limit_legs")

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

CLAIM_ROOT = "EAC-67206924-2e40988a6cd689d6"
ACCOUNT = "67206924"
AUTH_ID = "EA-67206924-testauthorization1"
DEMO_GATE_HASH = "sha256:" + "a" * 64
SYMBOL = "EURUSD"
NOW = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)

# entry_index, order_type, allocation, price, result_state, retcode,
# retcode_description, order_ticket, deal_ticket, position_ticket,
# filled_volume, filled_price -- exactly the real incident's own shape.
_REAL_INCIDENT_LEGS = (
    (1, "MARKET", 0.5, 1.15834, "FILLED",
     10009, "Request completed", "733124500", "900001", "900002", 0.05, 1.15834),
    (2, "LIMIT", 0.3, 1.1575214999999999, "MALFORMED",
     10009, "done", "733124517", "0", "0", 0.03, 0.0),
    (3, "LIMIT", 0.2, 1.1573769999999999, "MALFORMED",
     10009, "done", "733124518", "0", "0", 0.02, 0.0),
)


def _seed_real_incident(control: SER8DemoOrderSendControl) -> None:
    """Persists the real incident's own plan/leg-receipts state using
    ONLY SER8DemoOrderSendControl's own production persistence methods."""
    legs = tuple(
        DemoOrderPlanLegV1(
            entry_index=entry_index, leg_id=leg_identity(CLAIM_ROOT, entry_index, total_legs=3),
            order_type=order_type, planned_price=price, effective_entry_price=price,
            allocation=allocation, volume=filled_volume or 0.01, sl=1.1000, tp=1.2000,
        )
        for entry_index, order_type, allocation, price, _state, _rc, _rcd, _ot, _dt, _pt, filled_volume, _fp
        in _REAL_INCIDENT_LEGS
    )
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id="EOP-realincidenttest", claim_id=CLAIM_ROOT,
        authorization_id=AUTH_ID, decision_id="RD-real-incident-test", candidate_signal_id="sig-real-incident",
        demo_account_id=ACCOUNT, symbol=SYMBOL, action="BUY", legs=legs,
    )
    control._persist_plan(plan, created_at=NOW.isoformat())

    for entry_index, order_type, allocation, price, state, rc, rcd, ot, dt, pt, fv, fp in _REAL_INCIDENT_LEGS:
        leg_id = leg_identity(CLAIM_ROOT, entry_index, total_legs=3)
        request = DemoOrderRequestV1(
            schema_version=SCHEMA_VERSION, parent_claim_id=CLAIM_ROOT, entry_index=entry_index,
            claim_id=leg_id, authorization_id=AUTH_ID, demo_account_id=ACCOUNT, symbol=SYMBOL,
            action="BUY", order_type=order_type, volume=fv or 0.01, price=price, sl=1.1000, tp=1.2000,
            magic=DEMO_EXECUTOR_MAGIC_NUMBER, comment=f"SER8:{leg_id[-20:]}",
        )
        control._reserve_leg_attempt(
            leg_id=leg_id, plan_id=plan.plan_id, parent_claim_id=CLAIM_ROOT, entry_index=entry_index,
            attempt_id=f"EAO-test-{entry_index}", request=request,
            demo_authorization=_FakeDemoAuthorization(), captured_at=NOW,
        )
        control._finalize(
            claim_id=leg_id, plan_id=plan.plan_id, parent_claim_id=CLAIM_ROOT, entry_index=entry_index,
            authorization_id=AUTH_ID, demo_gate_hash=DEMO_GATE_HASH, request_hash=request.request_hash,
            attempt_id=f"EAO-test-{entry_index}", result_state=state, recorded_at=NOW.isoformat(),
            retcode=rc, retcode_description=rcd, order_ticket=ot, deal_ticket=dt, position_ticket=pt,
            requested_volume=request.volume, requested_price=request.price, filled_volume=fv, filled_price=fp,
        )


class _FakeDemoAuthorization:
    """Minimal stand-in exposing only the one attribute
    _reserve_leg_attempt actually reads (gate_hash) -- never a real
    DemoAccountAuthorizationV1, and never needed to be one for this
    narrow persistence-only setup helper."""

    gate_hash = DEMO_GATE_HASH


def _control(tmp_path: Path) -> tuple[SER8DemoOrderSendControl, Path]:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    transport = FakeDemoOrderTransport()  # no result_factory -- must never be called.
    control = SER8DemoOrderSendControl(registry=registry, transport=transport)
    return control, db_path


def _leg_states(db_path: Path) -> dict[int, str]:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
            "WHERE parent_claim_id=? ORDER BY entry_index", (CLAIM_ROOT,),
        ).fetchall()
    return {row["entry_index"]: json.loads(row["payload_json"])["result_state"] for row in rows}


# ---------------------------------------------------------------------------
# 8: exact current real claim regression test.
# ---------------------------------------------------------------------------


def test_real_incident_dry_run_then_real_recovery_then_idempotent_rerun(tmp_path: Path, capsys) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)
    assert _leg_states(db_path) == {1: "FILLED", 2: "MALFORMED", 3: "MALFORMED"}

    # 1: dry-run proves recoverable -- zero mutation.
    exit_code = recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=True)
    dry_run_output = capsys.readouterr().out
    assert exit_code == 0
    assert _leg_states(db_path) == {1: "FILLED", 2: "MALFORMED", 3: "MALFORMED"}  # unchanged.
    assert "new_state=PENDING" in dry_run_output
    assert "DRY RUN COMPLETE" in dry_run_output
    assert "broker_send_performed=NO" in dry_run_output

    # 2: real recovery.
    exit_code = recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=False)
    real_output = capsys.readouterr().out
    assert exit_code == 0
    assert "RECOVERY COMPLETE" in real_output

    # 3: FILLED + PENDING + PENDING.
    assert _leg_states(db_path) == {1: "FILLED", 2: "PENDING", 3: "PENDING"}

    # 4: order_ticket preserved exactly.
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        tickets = {
            row["entry_index"]: json.loads(row["payload_json"])["order_ticket"]
            for row in db.execute(
                "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=?", (CLAIM_ROOT,),
            ).fetchall()
        }
    assert tickets == {1: "733124500", 2: "733124517", 3: "733124518"}

    # 5: second recovery is a no-op.
    exit_code = recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=False)
    rerun_output = capsys.readouterr().out
    assert exit_code == 0
    assert _leg_states(db_path) == {1: "FILLED", 2: "PENDING", 3: "PENDING"}  # unchanged.
    # Every leg is reported as old_state==new_state on the re-run -- FILLED
    # was never MALFORMED to begin with, and the two PENDING legs are now
    # already-recovered, so recover_claim reports them untouched too
    # (neither branch calls recover_misclassified_pending_leg a second
    # time for a leg that is not currently MALFORMED).
    assert "old_state=FILLED new_state=FILLED" in rerun_output
    assert "old_state=PENDING new_state=PENDING" in rerun_output
    assert rerun_output.count("old_state=MALFORMED") == 0

    # 6: ZERO broker sends throughout the entire scenario.
    assert control.transport.calls == []


def test_real_incident_reported_leg_lines_match_exact_shape(tmp_path: Path, capsys) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)

    recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=False)
    output = capsys.readouterr().out

    assert (
        "leg_id=EAC-67206924-2e40988a6cd689d6#1 order_type=MARKET old_state=FILLED "
        "new_state=FILLED order_ticket=733124500 broker_send_performed=NO"
    ) in output
    assert (
        "leg_id=EAC-67206924-2e40988a6cd689d6#2 order_type=LIMIT old_state=MALFORMED "
        "new_state=PENDING order_ticket=733124517 broker_send_performed=NO"
    ) in output
    assert (
        "leg_id=EAC-67206924-2e40988a6cd689d6#3 order_type=LIMIT old_state=MALFORMED "
        "new_state=PENDING order_ticket=733124518 broker_send_performed=NO"
    ) in output


# ---------------------------------------------------------------------------
# requirement 3: only legs under the EXACT claim root; FILLED untouched.
# ---------------------------------------------------------------------------


def test_only_legs_under_the_exact_claim_root_are_touched(tmp_path: Path) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)

    # A DIFFERENT claim root that happens to share a prefix.
    other_root = CLAIM_ROOT + "-other"
    legs = (
        DemoOrderPlanLegV1(
            entry_index=1, leg_id=other_root, order_type="LIMIT", planned_price=1.1, effective_entry_price=1.1,
            allocation=1.0, volume=0.01, sl=1.0, tp=1.2,
        ),
    )
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id="EOP-otherclaim", claim_id=other_root, authorization_id=AUTH_ID,
        decision_id="RD-other", candidate_signal_id="sig-other", demo_account_id=ACCOUNT, symbol=SYMBOL,
        action="BUY", legs=legs,
    )
    control._persist_plan(plan, created_at=NOW.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=other_root, entry_index=1, claim_id=other_root,
        authorization_id=AUTH_ID, demo_account_id=ACCOUNT, symbol=SYMBOL, action="BUY", order_type="LIMIT",
        volume=0.01, price=1.1, sl=1.0, tp=1.2, magic=DEMO_EXECUTOR_MAGIC_NUMBER, comment="SER8:other",
    )
    control._reserve_leg_attempt(
        leg_id=other_root, plan_id=plan.plan_id, parent_claim_id=other_root, entry_index=1,
        attempt_id="EAO-other", request=request, demo_authorization=_FakeDemoAuthorization(), captured_at=NOW,
    )
    control._finalize(
        claim_id=other_root, plan_id=plan.plan_id, parent_claim_id=other_root, entry_index=1,
        authorization_id=AUTH_ID, demo_gate_hash=DEMO_GATE_HASH, request_hash=request.request_hash,
        attempt_id="EAO-other", result_state="MALFORMED", recorded_at=NOW.isoformat(),
        retcode=10009, retcode_description="done", order_ticket="1", deal_ticket="0", position_ticket="0",
        requested_volume=0.01, requested_price=1.1, filled_volume=0.01, filled_price=0.0,
    )

    recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=False)

    # The exact claim root was recovered...
    assert _leg_states(db_path) == {1: "FILLED", 2: "PENDING", 3: "PENDING"}
    # ...but the OTHER claim (different root) was never touched at all.
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        other_state = json.loads(
            db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (other_root,)
            ).fetchone()["payload_json"]
        )["result_state"]
    assert other_state == "MALFORMED"  # unchanged.


# ---------------------------------------------------------------------------
# requirement 4: insufficient evidence fails closed and names what is
# missing -- never inferred.
# ---------------------------------------------------------------------------


def test_missing_order_ticket_fails_closed_and_names_it(tmp_path: Path, capsys) -> None:
    control, db_path = _control(tmp_path)
    plan_legs = (
        DemoOrderPlanLegV1(
            entry_index=1, leg_id=CLAIM_ROOT, order_type="LIMIT", planned_price=1.1, effective_entry_price=1.1,
            allocation=1.0, volume=0.01, sl=1.0, tp=1.2,
        ),
    )
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id="EOP-noticket", claim_id=CLAIM_ROOT, authorization_id=AUTH_ID,
        decision_id="RD-noticket", candidate_signal_id="sig-noticket", demo_account_id=ACCOUNT, symbol=SYMBOL,
        action="BUY", legs=plan_legs,
    )
    control._persist_plan(plan, created_at=NOW.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=CLAIM_ROOT, entry_index=1, claim_id=CLAIM_ROOT,
        authorization_id=AUTH_ID, demo_account_id=ACCOUNT, symbol=SYMBOL, action="BUY", order_type="LIMIT",
        volume=0.01, price=1.1, sl=1.0, tp=1.2, magic=DEMO_EXECUTOR_MAGIC_NUMBER, comment="SER8:noticket",
    )
    control._reserve_leg_attempt(
        leg_id=CLAIM_ROOT, plan_id=plan.plan_id, parent_claim_id=CLAIM_ROOT, entry_index=1,
        attempt_id="EAO-noticket", request=request, demo_authorization=_FakeDemoAuthorization(), captured_at=NOW,
    )
    # A genuinely insufficient legacy record -- no order_ticket was ever
    # captured (e.g. a transport-level failure the old code still
    # persisted as MALFORMED with an empty ticket).
    control._finalize(
        claim_id=CLAIM_ROOT, plan_id=plan.plan_id, parent_claim_id=CLAIM_ROOT, entry_index=1,
        authorization_id=AUTH_ID, demo_gate_hash=DEMO_GATE_HASH, request_hash=request.request_hash,
        attempt_id="EAO-noticket", result_state="MALFORMED", recorded_at=NOW.isoformat(),
        retcode=10019, retcode_description="No money", order_ticket="", deal_ticket="", position_ticket="",
        requested_volume=0.01, requested_price=1.1, filled_volume=None, filled_price=None,
    )

    exit_code = recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account=ACCOUNT, dry_run=False)
    output = capsys.readouterr().out
    assert exit_code == 2
    assert "FAILED CLOSED" in output
    assert "missing required evidence" in output
    assert "order_ticket" in output
    assert "filled_price" in output
    # Never inferred/guessed -- state stays exactly MALFORMED.
    assert _leg_states(db_path) == {1: "MALFORMED"}


def test_account_mismatch_fails_closed(tmp_path: Path, capsys) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)

    exit_code = recovery_module.recover_claim(control, claim_id=CLAIM_ROOT, account="99999999", dry_run=False)
    output = capsys.readouterr().out
    assert exit_code == 2
    assert "FAILED CLOSED" in output
    assert "does not match --account" in output
    assert _leg_states(db_path) == {1: "FILLED", 2: "MALFORMED", 3: "MALFORMED"}  # unchanged.


def test_unknown_claim_root_reports_no_legs(tmp_path: Path, capsys) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)

    exit_code = recovery_module.recover_claim(
        control, claim_id="EAC-99999999-doesnotexist", account=ACCOUNT, dry_run=False
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "no legs found" in captured.err


# ---------------------------------------------------------------------------
# requirements 1/9: never resend, never call transport, never a broker
# side effect anywhere in recovery, including via the CLI wiring.
# ---------------------------------------------------------------------------


def test_cli_wires_a_transport_that_would_explode_if_ever_called(tmp_path: Path) -> None:
    control, db_path = _control(tmp_path)
    _seed_real_incident(control)
    assert isinstance(control.transport, FakeDemoOrderTransport)
    assert control.transport.result_factory is None
    with pytest.raises(Exception):  # noqa: B017 -- deliberately generic: any call must explode.
        control.transport.send(object())


def test_main_builds_control_with_a_non_functional_transport(tmp_path: Path) -> None:
    import inspect

    source = inspect.getsource(recovery_module.main)
    assert "FakeDemoOrderTransport()" in source
    assert "result_factory" not in source  # never configured -- would explode if ever called.


def test_recover_claim_never_references_transport_send() -> None:
    import inspect

    source = inspect.getsource(recovery_module.recover_claim)
    assert ".send(" not in source
    assert "transport" not in source.lower()


# ---------------------------------------------------------------------------
# requirement 6: idempotent -- and the CLI itself never constructs a new
# authorization/claim/order.
# ---------------------------------------------------------------------------


def test_cli_module_never_constructs_authorization_claim_or_order(tmp_path: Path) -> None:
    source = Path(recovery_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "SER8ExecutionAuthorizationControl(", "SER8ExecutionAuthorizationClaimControl(",
        "ExecutionAuthorizationV1(", "ExecutionAuthorizationClaimV1(",
    ):
        assert forbidden not in source


def test_dry_run_flag_reaches_recover_misclassified_pending_leg(tmp_path: Path) -> None:
    import inspect

    source = inspect.getsource(recovery_module.recover_claim)
    assert "dry_run=dry_run" in source


# ---------------------------------------------------------------------------
# CLI parser sanity.
# ---------------------------------------------------------------------------


def test_build_arg_parser_requires_db_account_and_claim_id() -> None:
    parser = recovery_module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(
        ["--db", "registry.db", "--account", ACCOUNT, "--claim-id", CLAIM_ROOT]
    )
    assert args.dry_run is False
    args_dry = parser.parse_args(
        ["--db", "registry.db", "--account", ACCOUNT, "--claim-id", CLAIM_ROOT, "--dry-run"]
    )
    assert args_dry.dry_run is True


def test_main_end_to_end_against_a_real_db_file(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "ser8_registry.db"
    registry = HypothesisRegistry(db_path)
    seed_transport = FakeDemoOrderTransport()
    seed_control = SER8DemoOrderSendControl(registry=registry, transport=seed_transport)
    _seed_real_incident(seed_control)

    exit_code = recovery_module.main(
        ["--db", str(db_path), "--account", ACCOUNT, "--claim-id", CLAIM_ROOT]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "RECOVERY COMPLETE" in output
    assert _leg_states(db_path) == {1: "FILLED", 2: "PENDING", 3: "PENDING"}
    assert seed_transport.calls == []
