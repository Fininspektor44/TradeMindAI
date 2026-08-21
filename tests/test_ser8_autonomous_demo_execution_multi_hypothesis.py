"""Tests for scripts/run_ser8_autonomous_demo_execution.py's generalized,
symbol-agnostic multi-hypothesis router -- SER8 FULL SYMBOL UNIVERSE +
RESEARCH RANKING V1.

Reuses the SAME real-chain/candidate-journal/MT5-export fixtures
tests/test_run_ser8_real_demo_pipeline.py and
tests/test_ser8_autonomous_demo_execution.py already built and proved
correct -- this file adds a SECOND accepted symbol sharing one registry/
artifact store (via ``fixtures._full_real_chain``'s new, purely additive
``symbol``/``work_dir``/``db_path``/``artifact_root`` parameters) to
exercise multi-hypothesis dispatch, ambiguous-scope fail-closed
detection, and account-global risk sharing -- never a shortcut, mock, or
invented trading parameter.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

worker_module = importlib.import_module("run_ser8_autonomous_demo_execution")
import test_run_ser8_real_demo_pipeline as fixtures  # noqa: E402
import test_ser8_autonomous_demo_execution as single_hyp  # noqa: E402

from trademind.discovery.hypothesis_tradeable_scope import AllowedActionScope  # noqa: E402
from trademind.ser8_mt5_demo_order_send import DemoOrderTransportResult, FakeDemoOrderTransport  # noqa: E402

_ACCOUNT = fixtures._ACCOUNT
_SYMBOL_A = fixtures._SYMBOL  # "XAUUSD"
_SYMBOL_B = "EURJPY"


def _scope(
    *, hypothesis_id: str, symbol: str, timeframe: str = "M5", setup_family: str = "spread_pressure",
    allowed_action_scope: str = AllowedActionScope.BOTH,
):
    """Hand-builds a valid HypothesisTradeableScopeV1 for the PURE
    _group_ambiguous_hypotheses grouping-logic tests -- deliberately not
    routed through bind_hypothesis_tradeable_scope, since these tests
    exercise ONLY the new grouping algorithm's own field comparisons,
    never the production provenance-verification chain (that chain is
    exercised separately below, via the SAME real fixtures every other
    SER8 worker test uses)."""
    from trademind.discovery.hypothesis_tradeable_scope import HypothesisTradeableScopeV1

    digest = "a" * 64
    return HypothesisTradeableScopeV1(
        hypothesis_id=hypothesis_id, hypothesis_family_id=f"hf_{digest}", bound_hypothesis_content_hash=digest,
        manifest_semantic_hash=f"sha256:{digest}", manifest_artifact_hash_ref=f"sha256:{digest}",
        symbol=symbol, timeframe=timeframe, setup_family=setup_family,
        allowed_action_scope=str(allowed_action_scope),
        source_action_scope="BUY_SELL_DIRECTIONAL", source_candidate_id=f"ssc-v2-{digest}",
        source_candidate_content_hash=f"sha256:{digest}", source_packet_artifact_hash_ref=f"sha256:{digest}",
        source_packet_semantic_hash=f"sha256:{digest}", source_report_artifact_hash_ref=f"sha256:{digest}",
        bound_at="2026-08-20T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# 1. _action_scopes_overlap -- pure truth table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("BOTH", "BOTH", True),
        ("BOTH", "BUY", True),
        ("SELL", "BOTH", True),
        ("BUY", "BUY", True),
        ("SELL", "SELL", True),
        ("BUY", "SELL", False),
        ("SELL", "BUY", False),
    ],
)
def test_action_scopes_overlap_truth_table(a: str, b: str, expected: bool) -> None:
    assert worker_module._action_scopes_overlap(a, b) is expected


# ---------------------------------------------------------------------------
# 2. _group_ambiguous_hypotheses -- pure grouping logic.
# ---------------------------------------------------------------------------


def test_group_ambiguous_same_symbol_both_action_scopes() -> None:
    scopes = {
        "hyp-a": _scope(hypothesis_id="hyp-a", symbol="EURUSD"),
        "hyp-b": _scope(hypothesis_id="hyp-b", symbol="EURUSD"),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == {"hyp-a", "hyp-b"}


def test_group_not_ambiguous_different_symbols() -> None:
    scopes = {
        "hyp-a": _scope(hypothesis_id="hyp-a", symbol="EURUSD"),
        "hyp-b": _scope(hypothesis_id="hyp-b", symbol="GBPUSD"),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == set()


def test_group_not_ambiguous_disjoint_action_scopes() -> None:
    """Same symbol/timeframe/setup_family, but one BUY-only and one
    SELL-only -- a real candidate can only ever carry one action, so
    these can never both match the same candidate."""
    scopes = {
        "hyp-buy": _scope(hypothesis_id="hyp-buy", symbol="EURUSD", allowed_action_scope=AllowedActionScope.BUY),
        "hyp-sell": _scope(hypothesis_id="hyp-sell", symbol="EURUSD", allowed_action_scope=AllowedActionScope.SELL),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == set()


def test_group_ambiguous_both_overlaps_single_direction() -> None:
    scopes = {
        "hyp-both": _scope(hypothesis_id="hyp-both", symbol="EURUSD", allowed_action_scope=AllowedActionScope.BOTH),
        "hyp-buy": _scope(hypothesis_id="hyp-buy", symbol="EURUSD", allowed_action_scope=AllowedActionScope.BUY),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == {"hyp-both", "hyp-buy"}


def test_group_not_ambiguous_different_timeframe() -> None:
    scopes = {
        "hyp-a": _scope(hypothesis_id="hyp-a", symbol="EURUSD", timeframe="M5"),
        "hyp-b": _scope(hypothesis_id="hyp-b", symbol="EURUSD", timeframe="H1"),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == set()


def test_group_three_way_partial_ambiguity() -> None:
    """A and B are ambiguous (same symbol/BOTH); C is a different symbol
    -- only A and B are flagged, C is untouched."""
    scopes = {
        "hyp-a": _scope(hypothesis_id="hyp-a", symbol="EURUSD"),
        "hyp-b": _scope(hypothesis_id="hyp-b", symbol="EURUSD"),
        "hyp-c": _scope(hypothesis_id="hyp-c", symbol="USDJPY"),
    }
    assert worker_module._group_ambiguous_hypotheses(scopes) == {"hyp-a", "hyp-b"}


def test_group_single_hypothesis_never_ambiguous() -> None:
    scopes = {"hyp-a": _scope(hypothesis_id="hyp-a", symbol="EURUSD")}
    assert worker_module._group_ambiguous_hypotheses(scopes) == set()


def test_group_empty_scopes() -> None:
    assert worker_module._group_ambiguous_hypotheses({}) == set()


# ---------------------------------------------------------------------------
# Real multi-symbol fixture: two independently-ACCEPTED hypotheses
# sharing ONE registry/artifact store, ONE shared data_root (candidate
# journal + MT5 exports for BOTH symbols) -- exactly what a single
# multi-hypothesis worker process observes.
# ---------------------------------------------------------------------------


def _prepared_multi_chain(tmp_path: Path):
    chain_a = fixtures._full_real_chain(tmp_path, symbol=_SYMBOL_A, work_dir=tmp_path / "work_a")
    chain_b = fixtures._full_real_chain(
        tmp_path, symbol=_SYMBOL_B, work_dir=tmp_path / "work_b",
        db_path=chain_a.db_path, artifact_root=chain_a.artifact_root,
    )
    single_hyp._accept_chain(chain_a)
    single_hyp._accept_chain(chain_b)

    shared_data_root = tmp_path / "shared_data"
    single_hyp._write_candidate_journal_with_action(shared_data_root, signal_id="sig-a", action="BUY")
    _write_candidate_journal_for_symbol(shared_data_root, signal_id="sig-b", action="BUY", symbol=_SYMBOL_B)
    _write_multi_symbol_mt5_exports(shared_data_root, symbols=(_SYMBOL_A, _SYMBOL_B))
    return chain_a, chain_b, shared_data_root


def _write_candidate_journal_for_symbol(data_root: Path, *, signal_id: str, action: str, symbol: str) -> Path:
    import json
    from datetime import datetime, timezone

    candidates_dir = data_root / "live_signal_runtime_v1"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    stop = 1990.0 if action == "BUY" else 2010.0
    targets = [2020.0] if action == "BUY" else [1980.0]
    payload = {
        "signal_id": signal_id, "observed_at": now, "created_at": now, "symbol": symbol, "timeframe": "M5",
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


def _write_multi_symbol_mt5_exports(data_root: Path, *, symbols: tuple[str, ...], account: str = _ACCOUNT) -> None:
    """The SAME shape fixtures._write_mt5_exports itself writes, reusing
    its own module-level _write_csv/_msc helpers -- extended to carry ONE
    symbol row per configured symbol (a real MT5 Market Watch export
    naturally carries every subscribed symbol in one file)."""
    from datetime import datetime, timedelta, timezone

    mt5_dir = data_root / "mt5"
    captured = datetime.now(timezone.utc) - timedelta(seconds=5)

    account_fields = [
        "time_msc", "account_login", "server", "currency", "balance", "equity", "margin",
        "free_margin", "margin_level", "leverage", "open_positions", "trade_allowed",
        "terminal_connected",
    ]
    account_rows = [{
        "time_msc": fixtures._msc(captured), "account_login": account, "server": "Demo-Server",
        "currency": "USD", "balance": 10_000.0, "equity": 10_000.0, "margin": 0.0,
        "free_margin": 10_000.0, "margin_level": 0, "leverage": 100, "open_positions": 0,
        "trade_allowed": 1, "terminal_connected": 1,
    }]
    fixtures._write_csv(mt5_dir / f"mt5_risk_account_utc_{account}.csv", account_fields, account_rows)

    position_fields = [
        "time_msc", "account_login", "server", "currency", "position_ticket", "position_id",
        "position_time_msc", "symbol", "magic", "side", "volume", "open_price",
        "current_price", "sl", "tp", "profit", "swap", "comment",
    ]
    fixtures._write_csv(mt5_dir / f"mt5_risk_positions_utc_{account}.csv", position_fields, [])

    symbol_fields = [
        "time_msc", "account_login", "server", "currency", "symbol", "digits", "trade_mode",
        "bid", "ask", "tick_size", "tick_value", "tick_value_profit", "tick_value_loss",
        "volume_min", "volume_max", "volume_step", "contract_size", "margin_initial",
        "margin_maintenance", "margin_buy_per_volume", "margin_sell_per_volume", "leverage",
    ]
    symbol_rows = [
        {
            "time_msc": fixtures._msc(captured), "account_login": account, "server": "Demo-Server",
            "currency": "USD", "symbol": symbol, "digits": 2, "trade_mode": "FULL",
            "bid": 1999.9, "ask": 2000.1, "tick_size": 0.01, "tick_value": 1.0,
            "tick_value_profit": 1.0, "tick_value_loss": 1.0, "volume_min": 0.01,
            "volume_max": 100.0, "volume_step": 0.01, "contract_size": 100.0,
            "margin_initial": 0.0, "margin_maintenance": 0.0, "margin_buy_per_volume": 20.0,
            "margin_sell_per_volume": 20.0, "leverage": 100,
        }
        for symbol in symbols
    ]
    fixtures._write_csv(mt5_dir / f"mt5_risk_symbols_utc_{account}.csv", symbol_fields, symbol_rows)


def _multi_worker_args(chain_a, shared_data_root: Path, hypothesis_ids: list[str], **overrides):
    parser = worker_module.build_arg_parser()
    argv = [
        "--data-root", str(shared_data_root),
        "--db", str(chain_a.db_path),
        "--orchestrator-db", str(chain_a.orchestrator_db_path),
        "--artifact-root", str(chain_a.artifact_root),
        "--hypothesis-ids", *hypothesis_ids,
        "--account", _ACCOUNT,
        "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(shared_data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(shared_data_root / "mt5"),
        "--sealed-holdout-path", str(chain_a.sealed_holdout_path),
        "--holdout-key-env", fixtures._KEY_ENV,
        "--holdout-key-id", fixtures._KEY_ID,
        "--holdout-primary-metric", fixtures._METRIC,
        "--risk-profile", str(single_hyp._REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(shared_data_root / "mt5_common"),
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


# ---------------------------------------------------------------------------
# 3. Multiple independently accepted symbols -- both execute
#    independently, zero cross-contamination, zero duplicate sends.
# ---------------------------------------------------------------------------


def test_multiple_independently_accepted_symbols_both_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    args = _multi_worker_args(chain_a, shared_data_root, [chain_a.hypothesis_id, chain_b.hypothesis_id])
    summaries = worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)

    summary_a = summaries[chain_a.hypothesis_id]
    summary_b = summaries[chain_b.hypothesis_id]
    assert summary_a.cycle_status == "EXECUTION_COMPLETE"
    assert summary_b.cycle_status == "EXECUTION_COMPLETE"
    assert summary_a.execution_plan_id != summary_b.execution_plan_id
    assert summary_a.claim_id != summary_b.claim_id
    assert len(fake.calls) == 2  # exactly one real send per symbol -- no duplicates, no cross-contamination.


def test_second_cycle_is_idempotent_across_both_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    args = _multi_worker_args(chain_a, shared_data_root, [chain_a.hypothesis_id, chain_b.hypothesis_id])
    worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)
    assert len(fake.calls) == 2

    summaries = worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)
    assert summaries[chain_a.hypothesis_id].cycle_status == "ALREADY_PROCESSED"
    assert summaries[chain_b.hypothesis_id].cycle_status == "ALREADY_PROCESSED"
    assert len(fake.calls) == 2  # unchanged -- zero new sends on the restart-safe idempotent path.


# ---------------------------------------------------------------------------
# 4. Unaccepted hypothesis never blocks an accepted sibling in the same
#    cycle.
# ---------------------------------------------------------------------------


def test_unaccepted_hypothesis_fails_closed_without_blocking_accepted_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    # A THIRD chain, deliberately left at FROZEN (never advanced to
    # ACCEPTED) -- configured anyway, must fail closed on its own without
    # touching chain_a's independent processing.
    chain_c = fixtures._full_real_chain(
        tmp_path, symbol="GBPUSD", work_dir=tmp_path / "work_c",
        db_path=chain_a.db_path, artifact_root=chain_a.artifact_root,
    )
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    args = _multi_worker_args(
        chain_a, shared_data_root, [chain_a.hypothesis_id, chain_b.hypothesis_id, chain_c.hypothesis_id],
    )
    summaries = worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)

    assert summaries[chain_a.hypothesis_id].cycle_status == "EXECUTION_COMPLETE"
    assert summaries[chain_b.hypothesis_id].cycle_status == "EXECUTION_COMPLETE"
    assert summaries[chain_c.hypothesis_id].cycle_status == "FAIL_CLOSED_NOT_ACCEPTED"
    assert len(fake.calls) == 2  # never sent for the unaccepted hypothesis.


# ---------------------------------------------------------------------------
# 5. Ambiguous overlapping scopes -- both hypotheses fail closed, zero
#    sends for either.
# ---------------------------------------------------------------------------


def test_ambiguous_overlapping_scopes_fail_closed_zero_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independently-ACCEPTED hypotheses proposed from the identical
    symbol/timeframe/setup_family (both built via _candidate_v2's own
    fixed M5/spread_pressure/BUY_SELL_DIRECTIONAL shape) -- a genuinely
    ambiguous configuration an operator must resolve by hand. Neither one
    is ever routed to risk/authorization/claim/send."""
    chain_a = fixtures._full_real_chain(tmp_path, symbol=_SYMBOL_A, work_dir=tmp_path / "work_a")
    # A SECOND hypothesis on the SAME symbol -- a distinct horizon keeps its
    # content_hash distinct from chain_a's (horizon is not part of
    # HypothesisTradeableScopeV1's own identity) while symbol/timeframe/
    # setup_family/action_scope stay identical, producing genuine ambiguity.
    chain_a2 = fixtures._full_real_chain(
        tmp_path, symbol=_SYMBOL_A, horizon=7, work_dir=tmp_path / "work_a2",
        db_path=chain_a.db_path, artifact_root=chain_a.artifact_root,
    )
    single_hyp._accept_chain(chain_a)
    single_hyp._accept_chain(chain_a2)
    assert chain_a.hypothesis_id != chain_a2.hypothesis_id

    shared_data_root = tmp_path / "shared_data"
    single_hyp._write_candidate_journal_with_action(shared_data_root, signal_id="sig-a", action="BUY")
    _write_multi_symbol_mt5_exports(shared_data_root, symbols=(_SYMBOL_A,))
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    args = _multi_worker_args(chain_a, shared_data_root, [chain_a.hypothesis_id, chain_a2.hypothesis_id])
    summaries = worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)

    assert summaries[chain_a.hypothesis_id].cycle_status == "FAIL_CLOSED_AMBIGUOUS_SCOPE"
    assert summaries[chain_a2.hypothesis_id].cycle_status == "FAIL_CLOSED_AMBIGUOUS_SCOPE"
    assert len(fake.calls) == 0  # zero sends -- ambiguity is checked BEFORE any candidate/risk/auth/claim step.


# ---------------------------------------------------------------------------
# 6. Account-global shared inputs: ONE discovery/pipeline build per
#    cycle, never one per hypothesis.
# ---------------------------------------------------------------------------


def test_account_level_inputs_discovered_exactly_once_per_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    call_count = {"n": 0}
    original = worker_module.pipeline_module.discover_inputs

    def _counting_discover_inputs(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(worker_module.pipeline_module, "discover_inputs", _counting_discover_inputs)

    args = _multi_worker_args(chain_a, shared_data_root, [chain_a.hypothesis_id, chain_b.hypothesis_id])
    worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)

    # ONE discovery for the whole cycle -- both hypotheses' risk
    # evaluations therefore read the SAME account/positions snapshot,
    # which is exactly how account-global portfolio risk is preserved
    # without any change to risk_manager.py itself.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# 7. Dry-run purity across multiple hypotheses.
# ---------------------------------------------------------------------------


def test_dry_run_purity_across_multiple_hypotheses(tmp_path: Path) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    args = _multi_worker_args(
        chain_a, shared_data_root, [chain_a.hypothesis_id, chain_b.hypothesis_id], dry_run=True,
    )
    summaries = worker_module.run_one_cycle_for_hypotheses(args, args.hypothesis_ids)
    assert summaries[chain_a.hypothesis_id].cycle_status == "DRY_RUN_WOULD_EXECUTE"
    assert summaries[chain_b.hypothesis_id].cycle_status == "DRY_RUN_WOULD_EXECUTE"
    single_hyp._assert_zero_rows(
        chain_a.db_path, "ser8_execution_authorizations", "ser8_execution_authorization_claims",
        "ser8_demo_order_execution_plans", "ser8_demo_order_leg_attempts",
    )


# ---------------------------------------------------------------------------
# 8. main() CLI dispatch: exactly-one-of validation, and a real --once
#    multi-hypothesis run through the public entrypoint.
# ---------------------------------------------------------------------------


def test_main_fails_closed_when_neither_hypothesis_flag_supplied(tmp_path: Path) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    argv = [
        "--data-root", str(shared_data_root), "--db", str(chain_a.db_path),
        "--orchestrator-db", str(chain_a.orchestrator_db_path), "--artifact-root", str(chain_a.artifact_root),
        "--account", _ACCOUNT, "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(shared_data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(shared_data_root / "mt5"), "--sealed-holdout-path", str(chain_a.sealed_holdout_path),
        "--risk-profile", str(single_hyp._REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(shared_data_root / "mt5_common"), "--once",
    ]
    assert worker_module.main(argv) == 2


def test_main_fails_closed_when_both_hypothesis_flags_supplied(tmp_path: Path) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    argv = [
        "--data-root", str(shared_data_root), "--db", str(chain_a.db_path),
        "--orchestrator-db", str(chain_a.orchestrator_db_path), "--artifact-root", str(chain_a.artifact_root),
        "--hypothesis-id", chain_a.hypothesis_id, "--hypothesis-ids", chain_a.hypothesis_id, chain_b.hypothesis_id,
        "--account", _ACCOUNT, "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(shared_data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(shared_data_root / "mt5"), "--sealed-holdout-path", str(chain_a.sealed_holdout_path),
        "--risk-profile", str(single_hyp._REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(shared_data_root / "mt5_common"), "--once",
    ]
    assert worker_module.main(argv) == 2


def test_main_runs_multi_hypothesis_once_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    fake = _success_transport()
    monkeypatch.setattr(worker_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    argv = [
        "--data-root", str(shared_data_root), "--db", str(chain_a.db_path),
        "--orchestrator-db", str(chain_a.orchestrator_db_path), "--artifact-root", str(chain_a.artifact_root),
        "--hypothesis-ids", chain_a.hypothesis_id, chain_b.hypothesis_id,
        "--account", _ACCOUNT, "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(shared_data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(shared_data_root / "mt5"), "--sealed-holdout-path", str(chain_a.sealed_holdout_path),
        "--holdout-key-env", fixtures._KEY_ENV, "--holdout-key-id", fixtures._KEY_ID,
        "--holdout-primary-metric", fixtures._METRIC,
        "--risk-profile", str(single_hyp._REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(shared_data_root / "mt5_common"), "--once",
    ]
    exit_code = worker_module.main(argv)
    assert exit_code == 0
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# 9. Never one Scheduled Task / EA per symbol -- structural proof this
#    remains ONE worker binary, ONE lock file, regardless of hypothesis
#    count.
# ---------------------------------------------------------------------------


def test_single_lock_file_guards_the_whole_multi_hypothesis_cycle(tmp_path: Path) -> None:
    chain_a, chain_b, shared_data_root = _prepared_multi_chain(tmp_path)
    lock_path = tmp_path / "worker.lock"
    argv = [
        "--data-root", str(shared_data_root), "--db", str(chain_a.db_path),
        "--orchestrator-db", str(chain_a.orchestrator_db_path), "--artifact-root", str(chain_a.artifact_root),
        "--hypothesis-ids", chain_a.hypothesis_id, chain_b.hypothesis_id,
        "--account", _ACCOUNT, "--demo-account-allowlist", _ACCOUNT,
        "--runtime-root", str(shared_data_root / "live_signal_runtime_v1"),
        "--mt5-export-dir", str(shared_data_root / "mt5"), "--sealed-holdout-path", str(chain_a.sealed_holdout_path),
        "--risk-profile", str(single_hyp._REAL_SUPERVISED_DEMO_PROFILE),
        "--common-files-dir", str(shared_data_root / "mt5_common"), "--once", "--lock-file", str(lock_path),
    ]
    with worker_module._LockFile(lock_path):
        exit_code = worker_module.main(argv)
    assert exit_code == 3  # refused to start -- ONE lock file for the whole cycle, not per hypothesis.
