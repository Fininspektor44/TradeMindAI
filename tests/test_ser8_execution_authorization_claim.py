"""Tests for the SER8 Execution Authorization CLAIM layer: turns a verified
``ExecutionAuthorizationV1`` into an atomic, one-shot
``ExecutionAuthorizationClaimV1`` -- and stops there.

Chain exercised for a real ACCEPTED hypothesis: ResearchProposalIntake ->
ResearchExperimentSpecification -> ExperimentManifestV2 (with
final_holdout_criteria) -> FROZEN -> Bridge V2 submission ->
TrainTestExecutionControl -> ValidationExecutionControl ->
HoldoutTriggerBridge -> real FinalHoldoutRunner -> HOLDOUT_CONSUMED ->
FinalVerdictAcceptanceControl.finalize() -> ACCEPTED ->
present_eligible_artifact -> bind_hypothesis_tradeable_scope -> a real
XAUUSD/M5/spread_pressure SignalCandidate + live MT5 CSV exports ->
evaluate_ser8_research_risk_gate -> RiskDecision -> ExecutionAuthorizationV1
-> ExecutionAuthorizationClaimV1.

This file does not import test helpers from sibling test files (consistent
with this lineage's convention of each test file owning its own small setup
helpers). IMPORTANT: every candidate/journal/CSV row in this file is
synthetic fixture data; none of it constitutes a live trade instruction,
and no assertion in this file treats ExecutionAuthorizationClaimV1 as an
order, a fill, or a broker ticket.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.dataset_split_provenance import (
    BoundSplitPlanV1,
    bind_split_plan_to_source,
)
from trademind.discovery.final_verdict_control import FinalVerdictAcceptanceControl
from trademind.discovery.holdout_keys import HoldoutKeyError
from trademind.discovery.holdout_runner import FinalHoldoutRunner
from trademind.discovery.holdout_sealer import FinalHoldoutSealer
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.holdout_trigger_bridge import HoldoutTriggerBridge
from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.discovery.hypothesis_tradeable_scope import bind_hypothesis_tradeable_scope
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    FinalHoldoutCriteriaV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.orchestrator_bridge import DiscoveryOrchestratorBridge
from trademind.discovery.research_eligibility_boundary import present_eligible_artifact
from trademind.discovery.result_ledger import ResultLedger
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.discovery.train_test_execution import TrainTestExecutionControl
from trademind.discovery.validation_execution import ValidationExecutionControl
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.research_execution import ResearchExecutionControl
from trademind.research_experiment_specification import (
    ResearchExperimentSpecificationControl,
    ResearchExperimentSpecificationV1,
)
from trademind.research_proposal_intake import ResearchProposalIntakeControl
from trademind.research_proposal_response import (
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.risk_manager import RiskProfile
from trademind.ser8_execution_authorization import SER8ExecutionAuthorizationControl
from trademind.ser8_execution_authorization_claim import (
    ExecutionAuthorizationClaimV1,
    SER8ExecutionAuthorizationClaimConflict,
    SER8ExecutionAuthorizationClaimControl,
    SER8ExecutionAuthorizationClaimError,
)
from trademind.ser8_research_risk_gate import evaluate_ser8_research_risk_gate
from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan
from trademind.signal_statistics_agent_packet import (
    SignalStatisticsPacketV2,
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import register_verified_packet_v2_task
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2

_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"
_DISCOVERY_ROLE = "market-data"
_VALIDATION_ROLE = "market-data-validation"
_TEST_FAMILY = "deterministic_aggregate_v1"
_METRIC = "avg_net_atr"
_HOLDOUT_METRIC = "rows"

_KEY = bytes(range(32))
_KEY_ID = "ser8-execution-authorization-key-v1"
_EVALUATOR_ARTIFACT = Path(__file__).resolve()
_HOLDOUT_PLAINTEXT = (
    "time,return\n"
    "2026-01-03T00:00:00+00:00,0.10\n"
    "2026-01-03T06:00:00+00:00,-0.05\n"
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
LOGIN = "37365712"
SYMBOL = "XAUUSD"
TIMEFRAME = "M5"
SETUP_FAMILY = "spread_pressure"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    store: ArtifactStore
    control: ControlPlane
    registry: HypothesisRegistry
    holdout_seals: HoldoutSealStore
    sealer: FinalHoldoutSealer
    bridge: DiscoveryOrchestratorBridge
    train_test: TrainTestExecutionControl
    validator: ValidationExecutionControl
    trigger_bridge: HoldoutTriggerBridge
    final_verdict: FinalVerdictAcceptanceControl
    sealed_path: Path
    spec: ResearchExperimentSpecificationV1
    hypothesis_id: str


class _StaticKeys:
    def __init__(self, key: bytes = _KEY, key_id: str = _KEY_ID) -> None:
        self.key = key
        self.key_id = key_id

    def load_key(self, key_id: str) -> bytes:
        if key_id != self.key_id:
            raise HoldoutKeyError("unknown key")
        return self.key


class _CountingEvaluator:
    evaluator_id = "ser8-execution-authorization-aggregate-v1"

    def evaluate(self, plaintext: bytes) -> dict[str, int]:
        rows = max(0, plaintext.count(b"\n") - 1)
        return {_HOLDOUT_METRIC: rows}


def _research_candidate(symbol: str = SYMBOL) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=symbol,
            timeframe=TIMEFRAME,
            feature=SETUP_FAMILY,
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _manager(path: Path) -> BudgetManager:
    return BudgetManager(
        path,
        daily_cost_ceiling=100.0,
        monthly_cost_ceiling=100.0,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=100_000,
        per_task_call_limit=8,
        per_role_call_limit=32,
    )


def _response(packet: SignalStatisticsPacketV2) -> ResearchProposalResponseV1:
    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": candidate_id,
                    "title": "Regime-conditioned continuation",
                    "rationale": "The candidate may depend on volatility regime.",
                    "falsifiable_claim": "The effect remains positive in a predefined regime.",
                    "proposed_test": "Compare predefined public-data subsets for the regime.",
                    "rejection_condition": "Reject if the regime effect is non-positive.",
                    "confidence": "HIGH",
                }
            ],
        }
    )


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="ser8-execution-authorization-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 12) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, base: float = 10.0) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{base + i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup(tmp_path: Path, *, db_name: str = "orchestrator.db", symbol: str = SYMBOL) -> _Context:
    db_path = tmp_path / db_name
    store = ArtifactStore(tmp_path / f"{db_name}-artifacts")
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_research_candidate(symbol),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=_code_provenance(),
        journal_rows=24,
        generated_at="2026-08-14T12:00:00+00:00",
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(
        packet_ref.hash_ref, control_plane=control, artifact_store=store
    )
    execution_control = ResearchExecutionControl(
        control_plane=control, budget_manager=budget, artifact_store=store
    )
    authorization = execution_control.create_authorization(
        task_id=task.task_id,
        task_revision=task.revision,
        reserved_cost=1.0,
        reserved_tokens=100,
        authorized_by="operator:execution",
    )
    execution = execution_control.claim_execution(authorization.authorization_id)
    execution = execution_control.mark_call_in_flight(execution.request_hash)
    execution = execution_control.finalize_success(
        execution.request_hash,
        response=_response(packet),
        actual_cost=0.5,
        actual_tokens=50,
    )
    registry = HypothesisRegistry(db_path)
    intake_control = ResearchProposalIntakeControl(
        execution_control=execution_control, hypothesis_registry=registry
    )
    spec_control = ResearchExperimentSpecificationControl(
        intake_control=intake_control, hypothesis_registry=registry
    )
    pending = intake_control.ingest_succeeded_research_execution_v1(execution.request_hash)[0]
    accepted, _hypothesis = intake_control.accept_for_hypothesis(
        pending.intake_id, reviewer_id="operator:reviewer"
    )

    from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1

    dataset_file = tmp_path / f"{db_name}-spec_dataset.csv"
    dataset_file.write_text("time,symbol,close\n1,XAUUSD,2000.0\n", encoding="utf-8")
    v1_dataset = DatasetArtifactV1.from_path(dataset_file)
    spec = spec_control.create_specification(
        accepted.intake_id,
        reviewer_id="operator:spec-reviewer",
        test_family=_TEST_FAMILY,
        primary_metric=_METRIC,
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        datasets=(v1_dataset,),
        parameters={"horizon": 12},
    )
    holdout_seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=holdout_seals, keys=_StaticKeys())
    bridge = DiscoveryOrchestratorBridge(
        registry=registry, holdout_seals=holdout_seals, control=control, artifacts=store
    )
    train_test = TrainTestExecutionControl(registry=registry, control=control, artifacts=store)
    validator = ValidationExecutionControl(
        registry=registry, control=control, artifacts=store, train_test=train_test
    )
    ledger = ResultLedger(tmp_path / f"{db_name}-results.jsonl")
    runner = FinalHoldoutRunner(
        registry=registry,
        seals=holdout_seals,
        keys=_StaticKeys(),
        ledger=ledger,
        evaluator=_CountingEvaluator(),
        evaluator_artifact_path=_EVALUATOR_ARTIFACT,
    )
    trigger_bridge = HoldoutTriggerBridge(
        registry=registry, control=control, artifacts=store, validator=validator, runner=runner
    )
    final_verdict = FinalVerdictAcceptanceControl(
        registry=registry,
        control=control,
        artifacts=store,
        validator=validator,
        trigger_bridge=trigger_bridge,
    )
    return _Context(
        db_path=db_path,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        sealer=sealer,
        bridge=bridge,
        train_test=train_test,
        validator=validator,
        trigger_bridge=trigger_bridge,
        final_verdict=final_verdict,
        sealed_path=tmp_path / f"{db_name}-final.holdout.json",
        spec=spec,
        hypothesis_id=spec.hypothesis_id,
    )


def _bound_split_plan(
    tmp_path: Path, db_name: str, rows: list[datetime] | None = None
) -> tuple[list[datetime], SplitPlan, BoundSplitPlanV1]:
    rows = rows or _timestamps()
    plan = chronological_split(rows)
    source_path = tmp_path / f"{db_name}-full_source.csv"
    source_path.write_bytes(_csv_bytes(rows))
    bound = bind_split_plan_to_source(str(source_path), split_plan=plan)
    return rows, plan, bound


def _discovery_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan) -> DatasetArtifactV2:
    discovery_rows = rows[: plan.discovery_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(discovery_rows)), media_type="text/csv")
    return DatasetArtifactV2(
        role=_DISCOVERY_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _validation_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan) -> DatasetArtifactV2:
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows)), media_type="text/csv")
    return DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes
    )


def _final_holdout_criteria(*, threshold: int = 1) -> FinalHoldoutCriteriaV1:
    return FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=_HOLDOUT_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=threshold
            ),
        ),
    )


def _build_manifest_v2(
    context: _Context,
    *,
    datasets: tuple[DatasetArtifactV2, ...],
    split_plan: SplitPlan,
    final_holdout_criteria: FinalHoldoutCriteriaV1 | None,
) -> ExperimentManifestV2:
    spec = context.spec
    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id,
        execution_request_hash=spec.request_hash,
        authorization_id=spec.authorization_id,
        task_id=spec.task_id,
        task_revision=spec.task_revision,
        packet_artifact_hash_ref=spec.packet_artifact_hash_ref,
        packet_semantic_hash=spec.packet_semantic_hash,
        result_artifact_hash_ref=spec.result_artifact_hash_ref,
        proposal_index=spec.proposal_index,
        candidate_id=spec.candidate_id,
    )
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric=spec.primary_metric, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
            ),
        ),
    )
    return build_experiment_manifest_v2(
        artifact_store=context.store,
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash,
        proposal_provenance=provenance,
        datasets=datasets,
        split_plan=split_plan,
        split_dataset_role=datasets[0].role,
        test_family=spec.test_family,
        primary_metric=spec.primary_metric,
        evaluation_criteria=criteria,
        alpha=spec.alpha,
        q=spec.q,
        minimum_effect_size=spec.minimum_effect_size,
        max_hypotheses_tests=spec.max_hypotheses_tests,
        trading_friction=None,
        deterministic_seed=None,
        code_provenance=_code_provenance(),
        semantic_parameters=spec.parameters,
        created_at="2026-08-17T00:00:00+00:00",
        created_by="operator:ser8-execution-authorization-test",
        final_holdout_criteria=final_holdout_criteria,
    )


def _freeze_v2(context: _Context, manifest: ExperimentManifestV2) -> None:
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=context.store)
    db = sqlite3.connect(context.db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        context.registry.freeze_manifest_v2_in_transaction(
            db, manifest_artifact_hash_ref=artifact.hash_ref, artifact_store=context.store
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seal_and_isolate(context: _Context, tmp_path: Path) -> None:
    plaintext_path = tmp_path / f"{context.hypothesis_id.replace(':', '_')}-plaintext.csv"
    plaintext_path.write_text(_HOLDOUT_PLAINTEXT, encoding="utf-8")
    context.sealer.seal_file(
        hypothesis_id=context.hypothesis_id,
        plaintext_path=plaintext_path,
        destination_path=context.sealed_path,
        key_id=_KEY_ID,
        evaluator_id=_CountingEvaluator.evaluator_id,
        evaluator_artifact_path=_EVALUATOR_ARTIFACT,
    )
    context.holdout_seals.mark_isolated(
        context.hypothesis_id,
        isolation_receipt_hash="c" * 64,
        public_max_time="2026-01-02T12:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00",
        holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=2,
        holdout_row_count=2,
    )


def _accepted_context(
    tmp_path: Path, *, threshold: int = 1, db_name: str = "orchestrator.db", symbol: str = SYMBOL
) -> _Context:
    """Real chain to ACCEPTED (threshold=1), reused for every test in this
    file that needs a genuinely ACCEPTED hypothesis."""
    context = _setup(tmp_path, db_name=db_name, symbol=symbol)
    rows, plan, bound = _bound_split_plan(tmp_path, db_name)
    discovery_dataset = _discovery_dataset_v2(context, rows, plan)
    validation_dataset = _validation_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(
        context,
        datasets=(discovery_dataset, validation_dataset),
        split_plan=plan,
        final_holdout_criteria=_final_holdout_criteria(threshold=threshold),
    )
    _freeze_v2(context, manifest)
    _seal_and_isolate(context, tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id,
        bound_split_plan=bound,
        dataset_role_bindings={discovery_dataset.role: "DISCOVERY", validation_dataset.role: "VALIDATION"},
    )
    context.train_test.execute(context.hypothesis_id, bound_split_plan=bound)
    context.validator.execute(context.hypothesis_id, bound_split_plan=bound)
    context.trigger_bridge.trigger(context.hypothesis_id, sealed_path=context.sealed_path)
    context.final_verdict.finalize(context.hypothesis_id)
    return context


# ---------------------------------------------------------------------------
# Live MT5 CSV + SignalCandidate fixtures
# ---------------------------------------------------------------------------


def _msc(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _account_fields() -> list[str]:
    return [
        "time_msc", "account_login", "server", "currency", "balance", "equity", "margin",
        "free_margin", "margin_level", "leverage", "open_positions", "trade_allowed", "terminal_connected",
    ]


def _position_fields() -> list[str]:
    return [
        "time_msc", "account_login", "server", "currency", "position_ticket", "position_id",
        "position_time_msc", "symbol", "magic", "side", "volume", "open_price", "current_price",
        "sl", "tp", "profit", "swap", "comment",
    ]


def _symbol_fields() -> list[str]:
    return [
        "time_msc", "account_login", "server", "currency", "symbol", "digits", "trade_mode", "bid", "ask",
        "tick_size", "tick_value", "tick_value_profit", "tick_value_loss", "volume_min", "volume_max",
        "volume_step", "contract_size", "margin_initial", "margin_maintenance", "margin_buy_per_volume",
        "margin_sell_per_volume", "leverage",
    ]


def _mt5_files(
    tmp_path: Path,
    *,
    captured: datetime,
    symbol: str = SYMBOL,
    trade_mode: str = "FULL",
    suffix: str = "",
) -> tuple[Path, Path, Path]:
    account = tmp_path / f"mt5_risk_account_utc_{LOGIN}{suffix}.csv"
    positions = tmp_path / f"mt5_risk_positions_utc_{LOGIN}{suffix}.csv"
    symbols = tmp_path / f"mt5_risk_symbols_utc_{LOGIN}{suffix}.csv"
    _write_csv(
        account,
        _account_fields(),
        [
            {
                "time_msc": _msc(captured), "account_login": LOGIN, "server": "RoboForex-Pro",
                "currency": "USD", "balance": 10_000.0, "equity": 9_950.0, "margin": 200.0,
                "free_margin": 9_750.0, "margin_level": 4975.0, "leverage": 500,
                "open_positions": 0, "trade_allowed": 1, "terminal_connected": 1,
            }
        ],
    )
    _write_csv(positions, _position_fields(), [])
    os.utime(positions, (captured.timestamp(), captured.timestamp()))
    _write_csv(
        symbols,
        _symbol_fields(),
        [
            {
                "time_msc": _msc(captured), "account_login": LOGIN, "server": "RoboForex-Pro",
                "currency": "USD", "symbol": symbol, "digits": 2, "trade_mode": trade_mode,
                "bid": 1999.9, "ask": 2000.0, "tick_size": 0.01, "tick_value": 1.0,
                "tick_value_profit": 1.0, "tick_value_loss": 1.0, "volume_min": 0.01,
                "volume_max": 50.0, "volume_step": 0.01, "contract_size": 100.0,
                "margin_initial": 0.0, "margin_maintenance": 0.0, "margin_buy_per_volume": 40.0,
                "margin_sell_per_volume": 40.0, "leverage": 500,
            }
        ],
    )
    return account, positions, symbols


def _candidate(*, created_at: datetime | None = None, action: str = "BUY", symbol: str = SYMBOL) -> SignalCandidate:
    created = created_at or NOW - timedelta(seconds=10)
    observed = created - timedelta(seconds=2)
    return SignalCandidate(
        observed_at=observed,
        created_at=created,
        symbol=symbol,
        timeframe=TIMEFRAME,
        setup_family=SETUP_FAMILY,
        scenario="ser8 execution authorization test",
        plan=TradePlan(
            action=action,
            entries=(EntryOrder(2000.0, 1.0, "confirmed entry", "MARKET"),),
            stop_price=1990.0 if action == "BUY" else 2010.0,
            targets=(2020.0,) if action == "BUY" else (1980.0,),
            invalidation="protected level broken",
            target_rationale=("external liquidity",),
        ),
        market_features={"structure": {"swing_bias": "BULLISH" if action == "BUY" else "BEARISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
    )


def _profile(**overrides: object) -> RiskProfile:
    fields: dict[str, object] = {"allowed_signal_states": ("APPROVED_MANUAL",)}
    fields.update(overrides)
    return RiskProfile(**fields)


def _authorized_case(
    tmp_path: Path,
    *,
    threshold: int = 1,
    db_name: str = "orchestrator.db",
    captured: datetime | None = None,
    symbol: str = SYMBOL,
):
    """Real ACCEPTED hypothesis -> eligibility -> scope -> candidate -> a
    real ALLOW SER8ResearchRiskGateResult -- everything
    SER8ExecutionAuthorizationControl.authorize needs."""
    context = _accepted_context(tmp_path, threshold=threshold, db_name=db_name, symbol=symbol)
    eligibility = present_eligible_artifact(
        context.hypothesis_id, registry=context.registry, final_verdict=context.final_verdict
    )
    scope = bind_hypothesis_tradeable_scope(
        context.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    captured = captured or NOW
    account_csv, positions_csv, symbols_csv = _mt5_files(
        tmp_path, captured=captured, symbol=symbol, suffix=f"-{db_name}"
    )
    candidate = _candidate(created_at=captured - timedelta(seconds=10), symbol=symbol)
    result = evaluate_ser8_research_risk_gate(
        eligibility, scope, candidate,
        registry=context.registry, final_verdict=context.final_verdict, login=LOGIN,
        account_csv=account_csv, positions_csv=positions_csv, symbols_csv=symbols_csv,
        profile=_profile(), now=captured,
    )
    assert result.decision.state == "ALLOW"
    control = SER8ExecutionAuthorizationControl(registry=context.registry, final_verdict=context.final_verdict)
    return context, eligibility, scope, candidate, result, control


def _claimed_case(
    tmp_path: Path, *, threshold: int = 1, db_name: str = "orchestrator.db",
    captured: datetime | None = None, symbol: str = SYMBOL,
):
    """Real ACCEPTED hypothesis -> ... -> a real ExecutionAuthorizationV1 --
    everything SER8ExecutionAuthorizationClaimControl.claim needs."""
    context, eligibility, scope, candidate, result, authorization_control = _authorized_case(
        tmp_path, threshold=threshold, db_name=db_name, captured=captured, symbol=symbol
    )
    captured = captured or NOW
    authorization = authorization_control.authorize(eligibility, scope, candidate, result, now=captured)
    claim_control = SER8ExecutionAuthorizationClaimControl(registry=context.registry)
    return context, authorization, claim_control


# ---------------------------------------------------------------------------
# 1: valid authorization claims once.
# ---------------------------------------------------------------------------


def test_valid_authorization_claims_once(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)

    claim = claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW)

    assert isinstance(claim, ExecutionAuthorizationClaimV1)
    assert claim.authorization_id == authorization.authorization_id
    assert claim.authorization_hash == authorization.authorization_hash
    assert claim.hypothesis_id == authorization.hypothesis_id
    assert claim.claimant_id == "ser8-adapter-session-1"


# ---------------------------------------------------------------------------
# 2: expired rejected.
# ---------------------------------------------------------------------------


def test_expired_authorization_rejected(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)
    # Just past the authorization's own expires_at (its own freshness
    # boundary, independent of any TTL this test happens to know about).
    just_expired = datetime.fromisoformat(authorization.expires_at) + timedelta(seconds=1)

    with pytest.raises(SER8ExecutionAuthorizationClaimError, match="expired"):
        claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=just_expired)


# ---------------------------------------------------------------------------
# 3: tampered authorization rejected.
# ---------------------------------------------------------------------------


def test_tampered_authorization_rejected(tmp_path: Path) -> None:
    import dataclasses

    context, authorization, claim_control = _claimed_case(tmp_path)
    tampered = dataclasses.replace(authorization, account_id="a-different-account")

    with pytest.raises(SER8ExecutionAuthorizationClaimError, match="does not match the persisted"):
        claim_control.claim(tampered, claimant_id="ser8-adapter-session-1", now=NOW)


# ---------------------------------------------------------------------------
# 4: wrong hypothesis/account rejected.
# ---------------------------------------------------------------------------


def test_unknown_authorization_id_rejected(tmp_path: Path) -> None:
    import dataclasses

    context, authorization, claim_control = _claimed_case(tmp_path)
    unknown = dataclasses.replace(authorization, authorization_id="EA-does-not-exist-0000000000000000")

    with pytest.raises(SER8ExecutionAuthorizationClaimError, match="no such execution authorization"):
        claim_control.claim(unknown, claimant_id="ser8-adapter-session-1", now=NOW)


def test_authorization_from_a_different_hypothesis_rejected(tmp_path: Path) -> None:
    context_a, authorization_a, claim_control_a = _claimed_case(tmp_path, db_name="a.db")
    _context_b, authorization_b, _claim_control_b = _claimed_case(
        tmp_path, db_name="b.db", symbol="EURUSD"
    )
    assert authorization_a.hypothesis_id != authorization_b.hypothesis_id

    # authorization_b does not exist in context_a's own database at all.
    with pytest.raises(SER8ExecutionAuthorizationClaimError, match="no such execution authorization"):
        claim_control_a.claim(authorization_b, claimant_id="ser8-adapter-session-1", now=NOW)


# ---------------------------------------------------------------------------
# 5: identical second claim cannot create another claim (idempotent retry).
# ---------------------------------------------------------------------------


def test_identical_second_claim_is_idempotent(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)

    first = claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW)
    second = claim_control.claim(
        authorization, claimant_id="ser8-adapter-session-1", now=NOW + timedelta(seconds=5)
    )

    assert first.claim_id == second.claim_id
    assert first.claim_hash == second.claim_hash
    assert first.claimed_at == second.claimed_at
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_execution_authorization_claims WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 6: conflicting claimant fails closed.
# ---------------------------------------------------------------------------


def test_conflicting_claimant_fails_closed(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)
    claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW)

    with pytest.raises(SER8ExecutionAuthorizationClaimConflict):
        claim_control.claim(authorization, claimant_id="ser8-adapter-session-2", now=NOW + timedelta(seconds=1))

    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_execution_authorization_claims WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 7: concurrent claims -> exactly one success.
# ---------------------------------------------------------------------------


def test_concurrent_conflicting_claims_exactly_one_succeeds(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)
    results: list[ExecutionAuthorizationClaimV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    claimants = ["ser8-adapter-session-A", "ser8-adapter-session-B"]

    def _worker(claimant_id: str) -> None:
        barrier.wait()
        try:
            results.append(claim_control.claim(authorization, claimant_id=claimant_id, now=NOW))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(claimant_id,)) for claimant_id in claimants]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1, results
    assert len(errors) == 1, errors
    assert isinstance(errors[0], SER8ExecutionAuthorizationClaimConflict)
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_execution_authorization_claims WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()[0]
    assert count == 1


def test_concurrent_identical_claims_both_resolve_to_one_row(tmp_path: Path) -> None:
    # Same claimant racing itself (e.g. a retried request): both calls must
    # succeed and resolve to the exact same claim; still only one row.
    context, authorization, claim_control = _claimed_case(tmp_path)
    results: list[ExecutionAuthorizationClaimV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert results[0].claim_hash == results[1].claim_hash
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_execution_authorization_claims WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 8: claim provenance complete.
# ---------------------------------------------------------------------------


def test_claim_provenance_is_complete(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)
    claim = claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW)

    assert claim.authorization_id == authorization.authorization_id
    assert claim.authorization_hash == authorization.authorization_hash
    assert claim.hypothesis_id == authorization.hypothesis_id
    assert claim.hypothesis_family_id == authorization.hypothesis_family_id
    assert claim.account_id == authorization.account_id
    assert claim.live_candidate_signal_id == authorization.live_candidate_signal_id
    assert claim.risk_gate_evidence_hash == authorization.risk_gate_evidence_hash
    assert claim.risk_decision_id == authorization.risk_decision_id
    assert claim.claim_hash.startswith("sha256:")
    assert claim.claim_id


# ---------------------------------------------------------------------------
# 9: claim persisted atomically.
# ---------------------------------------------------------------------------


def test_claim_persisted_atomically(tmp_path: Path) -> None:
    context, authorization, claim_control = _claimed_case(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        tables_before = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "ser8_execution_authorization_claims" in tables_before

    claim = claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=NOW)

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        tables_after = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        row = db.execute(
            "SELECT * FROM ser8_execution_authorization_claims WHERE authorization_id=?",
            (authorization.authorization_id,),
        ).fetchone()
    assert tables_after == tables_before
    assert row is not None
    assert row["claim_id"] == claim.claim_id
    assert row["claimant_id"] == "ser8-adapter-session-1"


# ---------------------------------------------------------------------------
# 10: no MT5/broker/order side effect.
# ---------------------------------------------------------------------------


def test_module_never_imports_metatrader5_or_calls_order_send() -> None:
    import ast
    import inspect

    import trademind.ser8_execution_authorization_claim as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden_imports = {"MetaTrader5", "requests", "urllib", "socket", "http"}
    assert not (imported & forbidden_imports), imported & forbidden_imports
    for forbidden_call in (
        "order_send(", "OrderSend(", "trade.Buy(", "trade.Sell(", "PositionClose(", "PositionModify(",
        "broker_ticket", "fill_price", "execution_result", "mt5_order_id",
    ):
        assert forbidden_call not in source, forbidden_call


def test_claim_carries_no_broker_ticket_or_fill_field() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ExecutionAuthorizationClaimV1)}
    forbidden = {
        "broker_ticket", "fill", "fill_price", "execution_result", "mt5_order_id",
        "order_id", "order_type", "filled_at", "filled_price",
    }
    assert not (field_names & forbidden), field_names & forbidden


# ---------------------------------------------------------------------------
# 11-12: existing authorization/risk/research tests remain green; full
# pytest green -- run separately as part of this task's own VALIDATION.
# ---------------------------------------------------------------------------
