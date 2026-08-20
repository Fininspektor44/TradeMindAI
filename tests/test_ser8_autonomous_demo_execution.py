"""Tests for scripts/run_ser8_autonomous_demo_execution.py -- SER8
AUTONOMOUS CONTINUOUS DEMO EXECUTION V1.

Reuses the SAME real-chain/candidate-journal/MT5-export fixtures
tests/test_run_ser8_real_demo_pipeline.py already built and proved
correct (imported directly, exactly the way that file itself imports
``run_ser8_real_demo_pipeline`` as a module) -- this file does not
reimplement the genuine ACCEPTED-hypothesis construction, it drives the
autonomous worker's own ``run_one_cycle``/``main`` against that SAME real
state. A handful of scenarios seed an execution plan/leg directly via
``SER8DemoOrderSendControl``'s own production persistence methods
(``_persist_plan``/``_reserve_leg_attempt``/``_finalize``) -- the SAME
established, session-wide exception for simulating already-persisted
state without hand-editing SQLite, matching
tests/test_ser8_mt5_execution_reconciliation.py's own ``_seed_leg``.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

worker_module = importlib.import_module("run_ser8_autonomous_demo_execution")
import test_run_ser8_real_demo_pipeline as fixtures  # noqa: E402

from trademind.discovery.hypothesis_registry import HypothesisRegistry  # noqa: E402
from trademind.ser8_demo_trade_outcome_capture import SER8DemoTradeOutcomeControl  # noqa: E402
from trademind.ser8_execution_authorization import (  # noqa: E402
    SER8ExecutionAuthorizationControl,
)
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DEMO_EXECUTOR_MAGIC_NUMBER,
    SCHEMA_VERSION,
    DemoOrderExecutionPlanV1,
    DemoOrderPlanLegV1,
    DemoOrderRequestV1,
    DemoOrderTransportResult,
    FakeDemoOrderTransport,
    SER8DemoOrderSendControl,
    leg_identity,
)
from trademind.ser8_research_risk_gate import evaluate_ser8_research_risk_gate  # noqa: E402
from trademind.signal_intelligence import candidate_from_dict  # noqa: E402

_ACCOUNT = fixtures._ACCOUNT
_SYMBOL = fixtures._SYMBOL
_REAL_SUPERVISED_DEMO_PROFILE = fixtures._REAL_SUPERVISED_DEMO_PROFILE
_REAL_STANDARD_PROFILE = fixtures._REAL_STANDARD_PROFILE


def _assert_zero_rows(db_path: Path, *tables: str) -> None:
    """Every control's own __init__ unconditionally creates its (empty)
    schema, so the meaningful safety property is zero ROWS, never that a
    table doesn't exist."""
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for table in tables:
            exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchall()
            if not exists:
                continue
            row = con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            assert row["n"] == 0, f"expected zero rows in {table}"
    finally:
        con.close()


def _last_candidate_signal_id(chain: fixtures._Chain, *, runtime_root: Path | None = None) -> str:
    """``SignalCandidate.signal_id`` is a COMPUTED property (observed_at +
    symbol + action + content hash) -- the candidate journal's own
    "signal_id" JSON field is never honored verbatim by
    ``candidate_from_dict``. Every test here that needs the REAL id a
    seeded candidate will resolve to must read it back through the SAME
    parser, never assume the journal's own string."""
    journal_path = (runtime_root or (chain.data_root / "live_signal_runtime_v1")) / "candidates.jsonl"
    last_line = journal_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    return candidate_from_dict(json.loads(last_line)).signal_id


def _accept_chain(chain: fixtures._Chain) -> None:
    """``_full_real_chain`` deliberately stops at FROZEN(+sealed) -- see
    its own docstring in test_run_ser8_real_demo_pipeline.py. This
    worker never advances the research lifecycle itself (by explicit
    design -- see run_ser8_autonomous_demo_execution.py's own module
    docstring), so every test fixture here advances the SAME real chain
    to ACCEPTED up front, via the SAME authoritative
    ``advance_research_state`` the pipeline script itself uses."""
    pipeline = fixtures.pipeline_module.build_research_pipeline(
        db_path=chain.db_path, orchestrator_db_path=chain.orchestrator_db_path, artifact_root=chain.artifact_root,
        holdout_key_env=fixtures._KEY_ENV, holdout_key_id=fixtures._KEY_ID,
        holdout_primary_metric=fixtures._METRIC, holdout_parameters={},
    )
    fixtures.pipeline_module.advance_research_state(
        pipeline, chain.hypothesis_id, research_source_csv=chain.research_source_csv,
        sealed_holdout_path=chain.sealed_holdout_path,
    )


def _worker_args(chain: fixtures._Chain, *, runtime_root: Path | None = None, **overrides):
    parser = worker_module.build_arg_parser()
    argv = [
        "--data-root", str(chain.data_root),
        "--db", str(chain.db_path),
        "--orchestrator-db", str(chain.orchestrator_db_path),
        "--artifact-root", str(chain.artifact_root),
        "--hypothesis-id", chain.hypothesis_id,
        "--account", _ACCOUNT,
        "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(runtime_root or (chain.data_root / "live_signal_runtime_v1")),
        "--mt5-export-dir", str(chain.data_root / "mt5"),
        "--sealed-holdout-path", str(chain.sealed_holdout_path),
        "--holdout-key-env", fixtures._KEY_ENV,
        "--holdout-key-id", fixtures._KEY_ID,
        "--holdout-primary-metric", fixtures._METRIC,
        "--risk-profile", str(_REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(chain.data_root / "mt5_common"),
    ]
    args = parser.parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _success_transport() -> FakeDemoOrderTransport:
    return FakeDemoOrderTransport(
        result_factory=lambda request: DemoOrderTransportResult(
            claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
            retcode=10009, retcode_description="TRADE_RETCODE_DONE", order_ticket="1001", deal_ticket="2001",
            position_ticket="3001", filled_volume=request.volume, filled_price=request.price or 2000.0,
        )
    )


def _prepared_chain(tmp_path: Path, *, multi_leg: bool = False, action: str = "BUY", signal_id: str = "sig-1"):
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    if multi_leg:
        fixtures._write_multi_leg_candidate_journal(chain.data_root, signal_id=signal_id)
    else:
        _write_candidate_journal_with_action(chain.data_root, signal_id=signal_id, action=action)
    fixtures._write_mt5_exports(chain.data_root)
    return chain


def _write_candidate_journal_with_action(data_root: Path, *, signal_id: str, action: str, observed_at=None) -> Path:
    candidates_dir = data_root / "live_signal_runtime_v1"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    now = (observed_at or datetime.now(timezone.utc)).isoformat()
    stop = 1990.0 if action == "BUY" else 2010.0
    targets = [2020.0] if action == "BUY" else [1980.0]
    payload = {
        "signal_id": signal_id, "observed_at": now, "created_at": now, "symbol": _SYMBOL, "timeframe": "M5",
        "setup_family": "spread_pressure", "scenario": "continuation",
        "plan": {
            "action": action,
            "entries": [{"price": 2000.0, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
            "stop_price": stop, "targets": targets, "invalidation": "close beyond stop", "target_rationale": ["r1"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {}, "provenance": ["test"],
        "generated_from_market_data": True, "robot_context_only": {},
    }
    journal_path = candidates_dir / "candidates.jsonl"
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return journal_path


def _seed_authorization(
    authorization_control: SER8ExecutionAuthorizationControl, *, authorization_id: str, candidate_signal_id: str,
    hypothesis_id: str, account: str = _ACCOUNT, symbol: str = _SYMBOL, action: str = "BUY", now: datetime | None = None,
) -> None:
    """Directly inserts an authorization row via the control's own real
    table -- mirroring tests/test_ser8_demo_trade_outcome_capture.py's
    own ``_seed_authorization`` -- so a directly-seeded FILLED leg (never
    routed through the real authorize() chain) still has a real,
    recoverable authorization for the outcome-capture bridge's own
    ``get_authorization`` lookup to find."""
    from trademind.ser8_execution_authorization import ExecutionAuthorizationV1
    from trademind.signal_statistics_provenance import canonical_json_bytes

    now = now or datetime.now(timezone.utc)
    authorization = ExecutionAuthorizationV1(
        schema_version="ser8-execution-authorization-v1", authorization_id=authorization_id,
        hypothesis_id=hypothesis_id, hypothesis_family_id=hypothesis_id,
        research_eligibility_artifact_hash="sha256:" + "b" * 64, hypothesis_tradeable_scope_hash="sha256:" + "c" * 64,
        live_candidate_signal_id=candidate_signal_id, risk_gate_evidence_hash="sha256:" + "d" * 64,
        risk_decision_id="RD-" + authorization_id, account_id=account, market_account_snapshot_hash="sha256:" + "e" * 64,
        symbol=symbol, action=action, authorized_at=now.isoformat(), expires_at=now.replace(hour=23).isoformat(),
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


def _seed_leg(
    control: SER8DemoOrderSendControl, *, claim_id: str, entry_index: int = 1, total_legs: int = 1,
    order_type: str = "MARKET", symbol: str = _SYMBOL, action: str = "BUY", volume: float = 0.5,
    price: float = 2000.0, result_state: str = "FILLED", order_ticket: str = "1", deal_ticket: str = "2",
    position_ticket: str = "3", account: str = _ACCOUNT, now: datetime | None = None,
) -> str:
    """Persists exactly ONE leg via the control's OWN real production
    methods -- the same technique tests/test_ser8_mt5_execution_reconciliation.py's
    own ``_seed_leg`` uses -- to build already-processed/PENDING/FILLED
    scenarios without hand-editing SQLite."""
    now = now or datetime.now(timezone.utc)
    leg_id = leg_identity(claim_id, entry_index, total_legs=total_legs)
    plan = DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION, plan_id=f"EOP-{leg_id}", claim_id=claim_id,
        authorization_id=f"EA-{claim_id}", decision_id=f"RD-{claim_id}", candidate_signal_id=claim_id,
        demo_account_id=account, symbol=symbol, action=action,
        legs=(
            DemoOrderPlanLegV1(
                entry_index=entry_index, leg_id=leg_id, order_type=order_type, planned_price=price,
                effective_entry_price=price, allocation=1.0, volume=volume, sl=1.0, tp=1.3,
            ),
        ),
    )
    control._persist_plan(plan, created_at=now.isoformat())
    request = DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION, parent_claim_id=claim_id, entry_index=entry_index, claim_id=leg_id,
        authorization_id=plan.authorization_id, demo_account_id=account, symbol=symbol, action=action,
        order_type=order_type, volume=volume, price=price, sl=1.0, tp=1.3, magic=DEMO_EXECUTOR_MAGIC_NUMBER,
        comment=f"SER8:{leg_id[-20:]}",
    )

    class _FakeAuth:
        gate_hash = "sha256:" + "a" * 64

    control._reserve_leg_attempt(
        leg_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=entry_index,
        attempt_id=f"EAO-{leg_id}", request=request, demo_authorization=_FakeAuth(), captured_at=now,
    )
    control._finalize(
        claim_id=leg_id, plan_id=plan.plan_id, parent_claim_id=claim_id, entry_index=entry_index,
        authorization_id=plan.authorization_id, demo_gate_hash=_FakeAuth.gate_hash, request_hash=request.request_hash,
        attempt_id=f"EAO-{leg_id}", result_state=result_state, recorded_at=now.isoformat(),
        retcode=10009, retcode_description=("done" if result_state == "PENDING" else "Request completed"),
        order_ticket=order_ticket, deal_ticket=deal_ticket, position_ticket=position_ticket,
        requested_volume=volume, requested_price=price,
        filled_volume=(volume if result_state == "FILLED" else None),
        filled_price=(price if result_state == "FILLED" else None),
    )
    return leg_id


# ---------------------------------------------------------------------------
# 1: no candidate.
# ---------------------------------------------------------------------------


def test_no_candidate_journal_line_is_clean_no_action(tmp_path: Path, monkeypatch) -> None:
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    (chain.data_root / "live_signal_runtime_v1").mkdir(parents=True, exist_ok=True)
    (chain.data_root / "live_signal_runtime_v1" / "candidates.jsonl").write_text("", encoding="utf-8")
    fixtures._write_mt5_exports(chain.data_root)
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: _success_transport())

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "NO_ELIGIBLE_CANDIDATE"
    assert summary.candidate_seen == "NO"
    assert summary.broker_sends_this_cycle == 0


# ---------------------------------------------------------------------------
# 2: stale candidate.
# ---------------------------------------------------------------------------


def test_stale_candidate_blocks_on_freshness(tmp_path: Path) -> None:
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    _write_candidate_journal_with_action(chain.data_root, signal_id="sig-stale", action="BUY", observed_at=stale_at)
    fixtures._write_mt5_exports(chain.data_root)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "RISK_BLOCK"
    assert summary.risk_state == "BLOCK"
    assert summary.broker_sends_this_cycle == 0


# ---------------------------------------------------------------------------
# 3/4: wrong hypothesis scope (symbol mismatch) -- no candidate eligible.
# ---------------------------------------------------------------------------


def test_scope_mismatched_candidate_is_never_eligible(tmp_path: Path) -> None:
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    candidates_dir = chain.data_root / "live_signal_runtime_v1"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "signal_id": "sig-wrong-symbol", "observed_at": now, "created_at": now, "symbol": "GBPUSD",
        "timeframe": "M5", "setup_family": "spread_pressure", "scenario": "continuation",
        "plan": {
            "action": "BUY", "entries": [{"price": 1.3, "allocation": 1.0, "rationale": "t", "order_type": "MARKET"}],
            "stop_price": 1.29, "targets": [1.32], "invalidation": "x", "target_rationale": ["r"],
        },
        "market_features": {}, "factor_scores": {}, "factor_reasons": {}, "provenance": ["test"],
        "generated_from_market_data": True, "robot_context_only": {},
    }
    (candidates_dir / "candidates.jsonl").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    fixtures._write_mt5_exports(chain.data_root)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "NO_ELIGIBLE_CANDIDATE"


# ---------------------------------------------------------------------------
# 5: Risk BLOCK (real default standard_v1.json profile).
# ---------------------------------------------------------------------------


def test_risk_block_with_standard_profile_creates_no_authorization(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path)
    args = _worker_args(chain, risk_profile=_REAL_STANDARD_PROFILE)
    summary = worker_module.run_one_cycle(args)
    assert summary.cycle_status == "RISK_BLOCK"
    assert "SIGNAL_NOT_APPROVED" in summary.risk_block_reason

    _assert_zero_rows(chain.db_path, "ser8_execution_authorizations", "ser8_execution_authorization_claims", "ser8_mt5_demo_order_leg_receipts")


# ---------------------------------------------------------------------------
# 6: valid single-entry MARKET -- reaches EXECUTION_COMPLETE, exactly 1 send.
# ---------------------------------------------------------------------------


def test_valid_single_entry_market_executes_exactly_once(tmp_path: Path, monkeypatch) -> None:
    chain = _prepared_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "EXECUTION_COMPLETE"
    assert summary.legs_total == 1
    assert summary.filled == 1
    assert summary.broker_sends_this_cycle == 1


def test_broken_deals_export_never_masks_a_genuine_execution_complete(tmp_path: Path, monkeypatch, capsys) -> None:
    """Outcome capture is a best-effort bridge -- a structurally invalid
    deals export (e.g. an operator-edited CSV missing a required column)
    must be reported on its own line, never allowed to hide that the
    candidate itself was genuinely, safely executed this cycle."""
    chain = _prepared_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)
    deals_csv = chain.data_root / "mt5" / f"mt5_risk_deals_utc_{_ACCOUNT}.csv"
    deals_csv.parent.mkdir(parents=True, exist_ok=True)
    deals_csv.write_text("time_msc,account_login,deal_ticket\n1,67206924,901\n", encoding="utf-8")

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "EXECUTION_COMPLETE"
    assert summary.filled == 1
    assert summary.outcomes_ingested == 0
    assert "outcome capture failed this cycle (non-fatal" in capsys.readouterr().err
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 7: valid three-leg MARKET+LIMIT+LIMIT.
# ---------------------------------------------------------------------------


def test_valid_three_leg_plan_executes_all_legs(tmp_path: Path, monkeypatch) -> None:
    chain = _prepared_chain(tmp_path, multi_leg=True, signal_id="sig-multi-1")
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "EXECUTION_COMPLETE"
    assert summary.legs_total == 3
    assert summary.filled == 3
    assert summary.broker_sends_this_cycle == 3
    assert len(fake.calls) == 3


# ---------------------------------------------------------------------------
# 8/9/25: already-processed candidate; repeated scheduler ticks; zero
# duplicate sends across repeated cycles.
# ---------------------------------------------------------------------------


def test_already_processed_candidate_never_resent_across_ten_ticks(tmp_path: Path, monkeypatch) -> None:
    chain = _prepared_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    first = worker_module.run_one_cycle(_worker_args(chain))
    assert first.cycle_status == "EXECUTION_COMPLETE"
    assert len(fake.calls) == 1

    for _ in range(9):
        summary = worker_module.run_one_cycle(_worker_args(chain))
        assert summary.cycle_status == "ALREADY_PROCESSED"
        assert summary.broker_sends_this_cycle == 0

    assert len(fake.calls) == 1  # zero duplicate broker sends across 10 total ticks.


# ---------------------------------------------------------------------------
# 10: process restart mid-multi-leg-send -- UNKNOWN leg #2 must never be
# resent, and the worker must never touch this candidate again.
# ---------------------------------------------------------------------------


def test_restart_after_unknown_leg_never_resends(tmp_path: Path, monkeypatch) -> None:
    chain = _prepared_chain(tmp_path, multi_leg=True, signal_id="sig-multi-crash")

    call_count = {"n": 0}

    def _flaky_result_factory(request):
        call_count["n"] += 1
        if request.entry_index == 2:
            raise RuntimeError("simulated transport crash on leg #2")
        return DemoOrderTransportResult(
            claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
            retcode=10009, retcode_description="TRADE_RETCODE_DONE", order_ticket="1", deal_ticket="2",
            position_ticket="3", filled_volume=request.volume, filled_price=request.price or 2000.0,
        )

    fake = FakeDemoOrderTransport(result_factory=_flaky_result_factory)
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    first = worker_module.run_one_cycle(_worker_args(chain))
    # Leg #1 fills, leg #2 goes UNKNOWN (transport raised), the plan
    # processing stops there (never attempts leg #3) -- aggregate is
    # PENDING_RECONCILIATION, which this single-leg-unaware worker path
    # surfaces as a fail-closed status, never a crash.
    assert first.cycle_status in ("EXECUTION_PENDING_RECONCILIATION", "FAIL_CLOSED_SEND_DENIED")
    calls_after_first = len(fake.calls)
    assert calls_after_first == 2  # leg #1 (sent) + leg #2 (attempted, UNKNOWN) -- leg #3 never touched.

    # "Restart": a brand new run_one_cycle call, simulating a fresh
    # process. The plan already exists for this candidate -- the worker
    # must NEVER re-evaluate, re-authorize, re-claim, or re-send.
    second = worker_module.run_one_cycle(_worker_args(chain))
    assert second.cycle_status == "ALREADY_PROCESSED"
    assert second.broker_sends_this_cycle == 0
    assert len(fake.calls) == calls_after_first  # no new transport calls at all.

    third = worker_module.run_one_cycle(_worker_args(chain))
    assert third.cycle_status == "ALREADY_PROCESSED"
    assert len(fake.calls) == calls_after_first


# ---------------------------------------------------------------------------
# 11/12: authorization already exists -- idempotent reuse vs conflict.
# ---------------------------------------------------------------------------


def test_conflicting_authorization_fails_closed_never_bypassed(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path)
    args = _worker_args(chain)

    pipeline = fixtures.pipeline_module.build_research_pipeline(
        db_path=chain.db_path, orchestrator_db_path=chain.orchestrator_db_path, artifact_root=chain.artifact_root,
        holdout_key_env=fixtures._KEY_ENV, holdout_key_id=fixtures._KEY_ID,
        holdout_primary_metric=fixtures._METRIC, holdout_parameters={},
    )
    from trademind.discovery.hypothesis_tradeable_scope import bind_hypothesis_tradeable_scope
    from trademind.discovery.research_eligibility_boundary import present_eligible_artifact
    from trademind.signal_intelligence import candidate_from_dict

    eligibility = present_eligible_artifact(chain.hypothesis_id, registry=pipeline.registry, final_verdict=pipeline.final_verdict)
    scope = bind_hypothesis_tradeable_scope(chain.hypothesis_id, registry=pipeline.registry, artifact_store=pipeline.artifacts)
    candidate_line = (chain.data_root / "live_signal_runtime_v1" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()[0]
    candidate = candidate_from_dict(json.loads(candidate_line))

    result = evaluate_ser8_research_risk_gate(
        eligibility, scope, candidate, registry=pipeline.registry, final_verdict=pipeline.final_verdict,
        login=_ACCOUNT, account_csv=chain.data_root / "mt5" / f"mt5_risk_account_utc_{_ACCOUNT}.csv",
        positions_csv=chain.data_root / "mt5" / f"mt5_risk_positions_utc_{_ACCOUNT}.csv",
        symbols_csv=chain.data_root / "mt5" / f"mt5_risk_symbols_utc_{_ACCOUNT}.csv",
        profile=fixtures.pipeline_module.profile_from_dict(json.loads(_REAL_SUPERVISED_DEMO_PROFILE.read_text(encoding="utf-8"))),
        requested_risk_pct=0.33,  # deliberately different from the worker's own default -- different decision_id.
    )
    assert result.decision.state == "ALLOW"
    authorization_control = SER8ExecutionAuthorizationControl(registry=pipeline.registry, final_verdict=pipeline.final_verdict)
    authorization_control.authorize(eligibility, scope, candidate, result)

    summary = worker_module.run_one_cycle(args)
    assert summary.cycle_status == "FAIL_CLOSED_AUTHORIZATION_CONFLICT"
    assert summary.broker_sends_this_cycle == 0

    _assert_zero_rows(chain.db_path, "ser8_mt5_demo_order_leg_receipts")  # never even attempted a send.


# ---------------------------------------------------------------------------
# 13: existing PENDING execution -- observed, not resent.
# ---------------------------------------------------------------------------


def test_existing_pending_leg_is_observed_not_resent(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path, signal_id="sig-pending-1")
    real_signal_id = _last_candidate_signal_id(chain)
    registry = HypothesisRegistry(chain.db_path)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    _seed_leg(control, claim_id=real_signal_id, result_state="PENDING", order_ticket="55", deal_ticket="0", position_ticket="0")

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "ALREADY_PROCESSED"
    assert summary.pending == 1
    assert summary.filled == 0
    assert summary.broker_sends_this_cycle == 0


# ---------------------------------------------------------------------------
# 14/24: existing FILLED execution -- observed; outcome capture ingests
# BUY/IN + SELL/OUT deal history once close evidence exists.
# ---------------------------------------------------------------------------


def test_existing_filled_leg_observed_and_outcome_captured_on_close_evidence(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path, signal_id="sig-filled-1")
    real_signal_id = _last_candidate_signal_id(chain)
    registry = HypothesisRegistry(chain.db_path)
    control = SER8DemoOrderSendControl(registry=registry, transport=FakeDemoOrderTransport())
    _seed_leg(control, claim_id=real_signal_id, result_state="FILLED", order_ticket="900", deal_ticket="901", position_ticket="777")
    authorization_control = SER8ExecutionAuthorizationControl.__new__(SER8ExecutionAuthorizationControl)
    authorization_control.registry = registry
    authorization_control.final_verdict = None
    authorization_control.path = Path(registry.path)
    authorization_control._init_schema()
    _seed_authorization(
        authorization_control, authorization_id=f"EA-{real_signal_id}", candidate_signal_id=real_signal_id,
        hypothesis_id=chain.hypothesis_id,
    )

    # First cycle: FILLED but no close evidence in the deals CSV yet.
    deals_csv = chain.data_root / "mt5" / f"mt5_risk_deals_utc_{_ACCOUNT}.csv"
    deals_csv.write_text(
        "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,volume,price,entry,time_deal_msc,profit\n"
        f"1,{_ACCOUNT},901,900,777,{_SYMBOL},{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,0.5,2000.0,IN,1700000000000,\n",
        encoding="utf-8",
    )
    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "ALREADY_PROCESSED"
    assert summary.filled == 1
    assert summary.outcomes_ingested == 0  # still open -- no OUT deal yet.

    # Second cycle: authoritative CLOSE (OUT) evidence with a real profit.
    deals_csv.write_text(
        "time_msc,account_login,deal_ticket,order_ticket,position_id,symbol,magic,side,volume,price,entry,time_deal_msc,profit\n"
        f"1,{_ACCOUNT},901,900,777,{_SYMBOL},{DEMO_EXECUTOR_MAGIC_NUMBER},BUY,0.5,2000.0,IN,1700000000000,\n"
        f"2,{_ACCOUNT},902,903,777,{_SYMBOL},{DEMO_EXECUTOR_MAGIC_NUMBER},SELL,0.5,2020.0,OUT,1700000600000,10.0\n",
        encoding="utf-8",
    )
    summary2 = worker_module.run_one_cycle(_worker_args(chain))
    assert summary2.outcomes_ingested == 1

    outcome_control = SER8DemoTradeOutcomeControl(registry=registry)
    leg_id = leg_identity(real_signal_id, 1, total_legs=1)
    outcome = outcome_control.get_outcome(leg_id)
    assert outcome is not None
    assert outcome.exit_price == 2020.0
    assert outcome.realized_pl == 10.0
    assert outcome.entry_deal_ticket == "901"
    assert outcome.exit_deal_tickets == ("902",)

    # Idempotent: a third cycle must not duplicate or re-derive the outcome.
    summary3 = worker_module.run_one_cycle(_worker_args(chain))
    assert summary3.outcomes_ingested == 0
    assert outcome_control.get_outcome(leg_id).outcome_hash == outcome.outcome_hash


# ---------------------------------------------------------------------------
# 16: reconciliation independence -- static proof.
# ---------------------------------------------------------------------------


def test_worker_never_imports_or_calls_reconciliation_module() -> None:
    source = Path(worker_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    assert "trademind.ser8_mt5_execution_reconciliation" not in imported_modules
    assert "reconcile_pending_leg" not in source


# ---------------------------------------------------------------------------
# 17: multiple candidate records -- only the newest eligible one is used.
# ---------------------------------------------------------------------------


def test_only_newest_eligible_candidate_is_selected(tmp_path: Path, monkeypatch) -> None:
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    older = datetime.now(timezone.utc) - timedelta(seconds=30)
    newer = datetime.now(timezone.utc)
    _write_candidate_journal_with_action(chain.data_root, signal_id="sig-older", action="BUY", observed_at=older)
    _write_candidate_journal_with_action(chain.data_root, signal_id="sig-newer", action="BUY", observed_at=newer)
    expected_signal_id = _last_candidate_signal_id(chain)  # the LAST-written line is the newer candidate.
    fixtures._write_mt5_exports(chain.data_root)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.candidate_id == expected_signal_id


# ---------------------------------------------------------------------------
# 18: overlap protection -- lock file busy.
# ---------------------------------------------------------------------------


def test_lock_file_prevents_overlapping_runs(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path)
    lock_path = tmp_path / "worker.lock"
    with worker_module._LockFile(lock_path):
        argv = [
            "--db", str(chain.db_path), "--orchestrator-db", str(chain.orchestrator_db_path),
            "--artifact-root", str(chain.artifact_root), "--data-root", str(chain.data_root),
            "--hypothesis-id", chain.hypothesis_id, "--account", _ACCOUNT,
            "--demo-account-allowlist", _ACCOUNT,
            "--runtime-root", str(chain.data_root / "live_signal_runtime_v1"),
            "--mt5-export-dir", str(chain.data_root / "mt5"),
            "--sealed-holdout-path", str(chain.sealed_holdout_path),
            "--holdout-key-env", fixtures._KEY_ENV, "--holdout-key-id", fixtures._KEY_ID,
            "--holdout-primary-metric", fixtures._METRIC, "--risk-profile", str(_REAL_SUPERVISED_DEMO_PROFILE),
            "--common-files-dir", str(chain.data_root / "mt5_common"), "--once", "--lock-file", str(lock_path),
        ]
        exit_code = worker_module.main(argv)
    assert exit_code == 3


# ---------------------------------------------------------------------------
# 19: dry-run followed by real run.
# ---------------------------------------------------------------------------


def test_dry_run_then_real_run_both_succeed(tmp_path: Path, monkeypatch) -> None:
    chain = _prepared_chain(tmp_path)
    dry_summary = worker_module.run_one_cycle(_worker_args(chain, dry_run=True))
    assert dry_summary.cycle_status == "DRY_RUN_WOULD_EXECUTE"
    assert dry_summary.legs_total == 1

    _assert_zero_rows(chain.db_path, "ser8_execution_authorizations", "ser8_execution_authorization_claims", "ser8_mt5_demo_order_leg_receipts")

    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)
    real_summary = worker_module.run_one_cycle(_worker_args(chain))
    assert real_summary.cycle_status == "EXECUTION_COMPLETE"
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 20: account mismatch -- fail closed at startup.
# ---------------------------------------------------------------------------


def test_account_not_in_allowlist_fails_closed_at_startup(tmp_path: Path) -> None:
    chain = _prepared_chain(tmp_path)
    argv = [
        "--db", str(chain.db_path), "--orchestrator-db", str(chain.orchestrator_db_path),
        "--artifact-root", str(chain.artifact_root), "--data-root", str(chain.data_root),
        "--hypothesis-id", chain.hypothesis_id, "--account", _ACCOUNT,
        "--demo-account-allowlist", "99999999",  # deliberately does not include --account.
        "--runtime-root", str(chain.data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(chain.data_root / "mt5"),
        "--sealed-holdout-path", str(chain.sealed_holdout_path),
        "--holdout-key-env", fixtures._KEY_ENV, "--holdout-key-id", fixtures._KEY_ID,
        "--holdout-primary-metric", fixtures._METRIC, "--risk-profile", str(_REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(chain.data_root / "mt5_common"), "--once",
    ]
    exit_code = worker_module.main(argv)
    assert exit_code == 2
    assert not (chain.db_path.with_suffix(".autonomous.lock")).exists()


# ---------------------------------------------------------------------------
# 21: genericity -- no hard-coded EURUSD/account/claim id anywhere.
# ---------------------------------------------------------------------------


def test_worker_source_never_hardcodes_a_symbol_account_or_claim() -> None:
    source = Path(worker_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("EURUSD", "67206924", "EAC-67206924-2e40988a6cd689d6", "990244"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 22/23: BUY and SELL candidates both execute via the SAME generic path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_buy_and_sell_candidates_both_execute(tmp_path: Path, monkeypatch, action: str) -> None:
    chain = fixtures._full_real_chain(tmp_path)
    _accept_chain(chain)
    _write_candidate_journal_with_action(chain.data_root, signal_id=f"sig-{action.lower()}", action=action)
    fixtures._write_mt5_exports(chain.data_root)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    summary = worker_module.run_one_cycle(_worker_args(chain))
    assert summary.cycle_status == "EXECUTION_COMPLETE"
    assert len(fake.calls) == 1
