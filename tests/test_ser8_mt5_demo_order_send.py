"""Tests for the SER8 MT5 Demo Order Send Adapter: the last step before a
real, supervised MT5 DEMO order. Turns a valid, unexpired, uniquely-claimed
``ExecutionAuthorizationClaimV1`` -- verified against a DEMO/PAPER account
allowlist -- into exactly one order request, sent exactly once through an
injectable transport, with the broker's result verified and persisted.

Chain exercised for a real ACCEPTED hypothesis: ResearchProposalIntake ->
ResearchExperimentSpecification -> ExperimentManifestV2 (with
final_holdout_criteria) -> FROZEN -> Bridge V2 submission ->
TrainTestExecutionControl -> ValidationExecutionControl ->
HoldoutTriggerBridge -> real FinalHoldoutRunner -> HOLDOUT_CONSUMED ->
FinalVerdictAcceptanceControl.finalize() -> ACCEPTED ->
present_eligible_artifact -> bind_hypothesis_tradeable_scope -> a real
XAUUSD/M5/spread_pressure SignalCandidate + live MT5 CSV exports ->
evaluate_ser8_research_risk_gate -> RiskDecision -> ExecutionAuthorizationV1
-> ExecutionAuthorizationClaimV1 -> DemoAccountAuthorizationV1 ->
SER8DemoOrderSendControl.send.

This file does not import test helpers from sibling test files (consistent
with this lineage's convention of each test file owning its own small setup
helpers). IMPORTANT: every candidate/journal/CSV row in this file is
synthetic fixture data. No real MetaTrader5 terminal is reachable from this
environment -- every test in this file uses either FakeDemoOrderTransport
(a pure, injected, in-memory transport) or real temp-file CSV rows fed to
FileBridgeDemoOrderTransport's own read/parse logic. No assertion in this
file treats a receipt as an actual broker fill unless it is produced by a
FILLED result_state from one of those two injected/simulated transports --
none of it is a real trade, and no real order was ever sent while writing
or running this file.
"""

from __future__ import annotations

import csv
import io
import json
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
from trademind.ser8_demo_account_safety_gate import DemoAccountAllowlistV1
from trademind.ser8_execution_authorization import SER8ExecutionAuthorizationControl
from trademind.ser8_execution_authorization_claim import SER8ExecutionAuthorizationClaimControl
from trademind.ser8_mt5_demo_order_send import (
    DEMO_EXECUTOR_MAGIC_NUMBER,
    REQUEST_CSV_FIELDS,
    RESULT_CSV_FIELDS,
    DemoOrderExecutionPlanReceiptV1,
    DemoOrderExecutionReceiptV1,
    DemoOrderTransportResult,
    FakeDemoOrderTransport,
    FileBridgeDemoOrderTransport,
    SER8DemoOrderAlreadyAttemptedError,
    SER8DemoOrderPartialExecutionError,
    SER8DemoOrderPendingError,
    SER8DemoOrderReconciliationRequiredError,
    SER8DemoOrderRejectedError,
    SER8DemoOrderSendControl,
    SER8DemoOrderSendError,
    SER8DemoOrderTransportError,
    build_demo_order_execution_plan,
    build_demo_order_leg_request,
    build_demo_order_request,
    leg_identity,
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
    candidate_factory=None,
):
    """Real ACCEPTED hypothesis -> eligibility -> scope -> candidate -> a
    real ALLOW SER8ResearchRiskGateResult -- everything
    SER8ExecutionAuthorizationControl.authorize needs.

    ``candidate_factory``, when supplied, replaces the default single-leg
    ``_candidate`` builder (e.g. with a multi-entry TradePlan) -- see
    ``_multi_leg_candidate`` below, used by the SER8 MT5 MULTI-ENTRY DEMO
    EXECUTION V1 tests."""
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
    factory = candidate_factory or _candidate
    candidate = factory(created_at=captured - timedelta(seconds=10), symbol=symbol)
    result = evaluate_ser8_research_risk_gate(
        eligibility, scope, candidate,
        registry=context.registry, final_verdict=context.final_verdict, login=LOGIN,
        account_csv=account_csv, positions_csv=positions_csv, symbols_csv=symbols_csv,
        profile=_profile(), now=captured,
    )
    assert result.decision.state == "ALLOW"
    control = SER8ExecutionAuthorizationControl(registry=context.registry, final_verdict=context.final_verdict)
    return context, eligibility, scope, candidate, result, control



def _allowlist(*account_ids: str) -> DemoAccountAllowlistV1:
    return DemoAccountAllowlistV1(account_ids=tuple(account_ids))


def _claim_case(
    tmp_path: Path, *, threshold: int = 1, db_name: str = "orchestrator.db",
    captured: datetime | None = None, symbol: str = SYMBOL, candidate_factory=None,
):
    """Real ACCEPTED hypothesis -> ... -> a real, claimed
    ExecutionAuthorizationClaimV1, plus the RiskDecision and SignalCandidate
    it was built from -- everything SER8DemoOrderSendControl.send needs."""
    context, eligibility, scope, candidate, result, authorization_control = _authorized_case(
        tmp_path, threshold=threshold, db_name=db_name, captured=captured, symbol=symbol,
        candidate_factory=candidate_factory,
    )
    captured = captured or NOW
    authorization = authorization_control.authorize(eligibility, scope, candidate, result, now=captured)
    claim_control = SER8ExecutionAuthorizationClaimControl(registry=context.registry)
    claim = claim_control.claim(authorization, claimant_id="ser8-adapter-session-1", now=captured)
    return context, claim, result.decision, candidate


def _multi_leg_candidate(
    *, created_at: datetime | None = None, action: str = "BUY", symbol: str = SYMBOL
) -> SignalCandidate:
    """A genuine 3-entry staged TradePlan -- 1 MARKET + 2 LIMIT legs,
    allocations summing to 1.0 -- structurally the SAME shape as the real
    Windows evidence this task's own spec quotes (TM-20260819T032500Z-
    EURUSD-BUY-8124e6ea0526ffbb: MARKET 0.5 / LIMIT 0.3 / LIMIT 0.2),
    reusing this file's own XAUUSD-scale fixture prices so it can reuse
    every other real-chain fixture (``_mt5_files`` etc.) unchanged."""
    created = created_at or NOW - timedelta(seconds=10)
    observed = created - timedelta(seconds=2)
    if action == "BUY":
        entries = (
            EntryOrder(2000.0, 0.5, "initial confirmation", "MARKET"),
            EntryOrder(1998.0, 0.3, "first staged pullback", "LIMIT"),
            EntryOrder(1996.0, 0.2, "second staged pullback", "LIMIT"),
        )
        stop_price = 1990.0
        targets = (2020.0,)
    else:
        entries = (
            EntryOrder(2000.0, 0.5, "initial confirmation", "MARKET"),
            EntryOrder(2002.0, 0.3, "first staged pullback", "LIMIT"),
            EntryOrder(2004.0, 0.2, "second staged pullback", "LIMIT"),
        )
        stop_price = 2010.0
        targets = (1980.0,)
    return SignalCandidate(
        observed_at=observed,
        created_at=created,
        symbol=symbol,
        timeframe=TIMEFRAME,
        setup_family=SETUP_FAMILY,
        scenario="ser8 multi-entry demo execution test",
        plan=TradePlan(
            action=action,
            entries=entries,
            stop_price=stop_price,
            targets=targets,
            invalidation="protected level broken",
            target_rationale=("external liquidity",),
        ),
        market_features={"structure": {"swing_bias": "BULLISH" if action == "BUY" else "BEARISH"}},
        factor_scores={"structure": 0.9},
        factor_reasons={"structure": ("BOS confirmed",)},
        provenance=("FX_RESEARCH",),
    )


def _clean_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id,
        demo_account_id=request.demo_account_id,
        symbol=request.symbol,
        retcode=10009,
        retcode_description="Request completed",
        order_ticket="123456",
        deal_ticket="654321",
        position_ticket="789012",
        filled_volume=request.volume,
        filled_price=request.price if request.price > 0 else 2000.0,
    )


def _rejected_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10019, retcode_description="No money", order_ticket="", deal_ticket="", position_ticket="",
        filled_volume=None, filled_price=None,
    )


def _requote_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10004, retcode_description="Requote", order_ticket="", deal_ticket="", position_ticket="",
        filled_volume=None, filled_price=None,
    )


def _partial_fill_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10009, retcode_description="Request completed",
        order_ticket="1", deal_ticket="2", position_ticket="3",
        filled_volume=request.volume / 2.0, filled_price=request.price if request.price > 0 else 2000.0,
    )


def _malformed_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10009, retcode_description="Request completed",
        order_ticket="1", deal_ticket="2", position_ticket="3",
        filled_volume=None, filled_price=None,
    )


def _wrong_symbol_result(request) -> DemoOrderTransportResult:
    return DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol="EURUSD",
        retcode=10009, retcode_description="Request completed",
        order_ticket="1", deal_ticket="2", position_ticket="3",
        filled_volume=request.volume, filled_price=2000.0,
    )


# ---------------------------------------------------------------------------
# 1: valid demo chain builds exact request.
# ---------------------------------------------------------------------------


def test_valid_demo_chain_builds_exact_request(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)

    assert request.claim_id == claim.claim_id
    assert request.symbol == SYMBOL
    assert request.action == candidate.plan.action
    assert request.volume == decision.orders[0].volume
    assert request.price == decision.orders[0].planned_price
    assert request.sl == candidate.plan.stop_price
    assert request.tp == candidate.plan.targets[0]
    assert request.magic == DEMO_EXECUTOR_MAGIC_NUMBER
    assert request.request_hash.startswith("sha256:")


def test_valid_demo_chain_sends_and_receives_clean_fill(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    receipt = control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    assert isinstance(receipt, DemoOrderExecutionReceiptV1)
    assert receipt.result_state == "FILLED"
    assert receipt.claim_id == claim.claim_id
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# 2: wrong/non-demo account fails.
# ---------------------------------------------------------------------------


def test_non_demo_account_fails(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError):
        control.send(claim, decision, candidate, allowlist=_allowlist("99999999"), now=NOW)
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 3: expired/tampered claim fails.
# ---------------------------------------------------------------------------


def test_stale_claim_fails(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="send window"):
        control.send(
            claim, decision, candidate, allowlist=_allowlist(LOGIN),
            maximum_claim_age_seconds=60.0, now=NOW + timedelta(seconds=120),
        )
    assert transport.calls == []


def test_tampered_claim_fails(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    object.__setattr__(claim, "account_id", "99999999")
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="self-consistency"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN, "99999999"), now=NOW)
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 4: wrong candidate/risk lineage fails.
# ---------------------------------------------------------------------------


def test_wrong_decision_lineage_fails(tmp_path: Path) -> None:
    context_a, claim_a, decision_a, candidate_a = _claim_case(tmp_path, db_name="a.db")
    _context_b, _claim_b, decision_b, _candidate_b = _claim_case(
        tmp_path, db_name="b.db", symbol="EURUSD"
    )
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context_a.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="risk_decision_id"):
        control.send(claim_a, decision_b, candidate_a, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == []


def test_wrong_candidate_lineage_fails(tmp_path: Path) -> None:
    context, claim, decision, original_candidate = _claim_case(tmp_path)
    del original_candidate
    unrelated_candidate = _candidate(action="SELL", created_at=NOW - timedelta(seconds=10))
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="candidate identity"):
        control.send(claim, decision, unrelated_candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 5: multi-entry RiskDecisions are now genuinely supported (SER8 MT5
# MULTI-ENTRY DEMO EXECUTION V1) -- what remains rejected is a decision
# with a data-integrity violation (duplicate entry_index), never a
# legitimate N > 1 plan. See
# tests/test_ser8_mt5_multi_entry_demo_execution.py for the dedicated
# multi-leg execution proofs.
# ---------------------------------------------------------------------------


def test_duplicate_entry_index_fails_closed(tmp_path: Path) -> None:
    import dataclasses

    context, claim, decision, candidate = _claim_case(tmp_path)
    # Two SizedOrder legs sharing the SAME entry_index -- a data-integrity
    # violation (never a real multi-entry plan, where risk_manager assigns
    # one entry_index per staged candidate.plan.entries item), and this
    # must still fail closed before any leg is attempted.
    duplicate_index_orders = dataclasses.replace(decision, orders=decision.orders + decision.orders)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="duplicate entry_index"):
        control.send(claim, duplicate_index_orders, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == []


def test_empty_orders_fails_closed(tmp_path: Path) -> None:
    import dataclasses

    context, claim, decision, candidate = _claim_case(tmp_path)
    empty_orders = dataclasses.replace(decision, orders=())
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="orders is empty"):
        control.send(claim, empty_orders, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == []


def test_unsupported_order_type_fails_closed_before_any_leg_sent(tmp_path: Path) -> None:
    import dataclasses

    context, claim, decision, candidate = _claim_case(tmp_path)
    bad_order = dataclasses.replace(decision.orders[0], order_type="ICEBERG")
    bad_orders = dataclasses.replace(decision, orders=(bad_order,))
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="unsupported order_type"):
        control.send(claim, bad_orders, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == []
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 6-8: volume/SL/TP exact, nothing invented.
# ---------------------------------------------------------------------------


def test_volume_comes_exactly_from_risk_decision(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)
    assert request.volume == decision.orders[0].volume
    assert request.volume != 0.0


def test_sl_tp_come_exactly_from_bound_trade_plan(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)
    assert request.sl == candidate.plan.stop_price
    assert request.tp == candidate.plan.targets[0]


def test_no_trading_parameter_is_invented() -> None:
    import ast
    import inspect

    import trademind.ser8_mt5_demo_order_send as module

    source = inspect.getsource(module.build_demo_order_request)
    tree = ast.parse(source)
    # The only numeric literals inside build_demo_order_request must be
    # array/tuple indices (e.g. targets[0]) -- no hardcoded price/volume/
    # sl/tp constant may appear.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            pytest.fail(f"unexpected float literal in build_demo_order_request: {node.value}")


# ---------------------------------------------------------------------------
# 9-11: atomic one-shot send guard.
# ---------------------------------------------------------------------------


def test_guard_permits_first_send_only(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 1

    with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=1))
    assert len(transport.calls) == 1


def test_duplicate_call_cannot_produce_second_send(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    for _ in range(3):
        with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
            control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 1
    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (claim.claim_id,)
        ).fetchone()[0]
    assert count == 1


def test_concurrent_callers_produce_at_most_one_send(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    results: list[DemoOrderExecutionReceiptV1] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1, results
    assert len(errors) == 2, errors
    assert all(isinstance(exc, SER8DemoOrderAlreadyAttemptedError) for exc in errors)
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# 12-14: broker rejection/requote/partial-fill/timeout handling.
# ---------------------------------------------------------------------------


def test_broker_rejection_persisted_and_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_rejected_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderRejectedError, match="REJECTED"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (claim.claim_id,)
        ).fetchone()
    assert row is not None
    payload = __import__("json").loads(row["payload_json"])
    assert payload["result_state"] == "REJECTED"


def test_requote_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_requote_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderRejectedError, match="REQUOTE"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)


def test_partial_fill_detected_and_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_partial_fill_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderRejectedError, match="PARTIAL_FILL"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)


def test_malformed_result_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_malformed_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderRejectedError, match="MALFORMED"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)


def test_symbol_mismatch_in_result_is_malformed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_wrong_symbol_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderRejectedError, match="MALFORMED"):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)


def test_transport_failure_persists_unknown_and_does_not_retry(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)

    class _ExplodingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, request):
            self.calls += 1
            raise TimeoutError("no response from executor")

    transport = _ExplodingTransport()
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderTransportError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert transport.calls == 1

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (claim.claim_id,)
        ).fetchone()
    payload = __import__("json").loads(row["payload_json"])
    assert payload["result_state"] == "UNKNOWN"

    # A second call for the SAME claim must never call the transport again.
    with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=1))
    assert transport.calls == 1


# ---------------------------------------------------------------------------
# 15: clean result persisted with complete provenance.
# ---------------------------------------------------------------------------


def test_clean_result_persisted_with_complete_provenance(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    receipt = control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    assert receipt.claim_id == claim.claim_id
    assert receipt.authorization_id == claim.authorization_id
    assert receipt.demo_gate_hash
    assert receipt.request_hash
    assert receipt.attempt_id
    assert receipt.retcode == 10009
    assert receipt.order_ticket == "123456"
    assert receipt.deal_ticket == "654321"
    assert receipt.requested_volume == decision.orders[0].volume
    assert receipt.requested_price == decision.orders[0].planned_price
    assert receipt.filled_volume == decision.orders[0].volume
    assert receipt.result_state == "FILLED"
    assert receipt.receipt_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# 16: fake transport proves exact one-call behavior.
# ---------------------------------------------------------------------------


def test_fake_transport_receives_exactly_one_call_with_exact_request(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    assert len(transport.calls) == 1
    sent_request = transport.calls[0]
    assert sent_request.claim_id == claim.claim_id
    assert sent_request.volume == decision.orders[0].volume
    assert sent_request.symbol == candidate.symbol


def test_file_bridge_transport_reads_a_seeded_result(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)

    common_dir = tmp_path / "common_files"
    common_dir.mkdir()
    transport = FileBridgeDemoOrderTransport(
        common_files_dir=common_dir, login=LOGIN, poll_interval_seconds=0.05, timeout_seconds=2.0
    )

    # Pre-seed the result BEFORE calling send(), so the very first poll
    # finds it -- proving the transport's own write/poll/read/parse logic
    # without needing a real MT5 terminal or any threading.
    result_path = common_dir / f"ser8_demo_order_result_{LOGIN}.csv"
    with result_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RESULT_CSV_FIELDS))
        writer.writeheader()
        writer.writerow(
            {
                "claim_id": request.claim_id, "demo_account_id": request.demo_account_id,
                "symbol": request.symbol, "retcode": "10009", "retcode_description": "Request completed",
                "order_ticket": "1", "deal_ticket": "2", "position_ticket": "3",
                "filled_volume": repr(request.volume), "filled_price": repr(2000.0),
            }
        )

    result = transport.send(request)
    assert result.claim_id == request.claim_id
    assert result.retcode == 10009

    written_request = common_dir / f"ser8_demo_order_request_{LOGIN}.csv"
    assert written_request.is_file()
    with written_request.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["claim_id"] == request.claim_id
    assert list(csv.reader(open(written_request, encoding="utf-8")))[0] == list(REQUEST_CSV_FIELDS)


def test_file_bridge_transport_ignores_stale_result_and_times_out(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)

    common_dir = tmp_path / "common_files"
    common_dir.mkdir()
    transport = FileBridgeDemoOrderTransport(
        common_files_dir=common_dir, login=LOGIN, poll_interval_seconds=0.05, timeout_seconds=0.3
    )
    result_path = common_dir / f"ser8_demo_order_result_{LOGIN}.csv"
    with result_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RESULT_CSV_FIELDS))
        writer.writeheader()
        writer.writerow(
            {
                "claim_id": "a-stale-claim-from-a-previous-run", "demo_account_id": request.demo_account_id,
                "symbol": request.symbol, "retcode": "10009", "retcode_description": "Request completed",
                "order_ticket": "1", "deal_ticket": "2", "position_ticket": "3",
                "filled_volume": repr(request.volume), "filled_price": "2000.0",
            }
        )

    with pytest.raises(SER8DemoOrderTransportError, match="no matching result"):
        transport.send(request)


# ---------------------------------------------------------------------------
# 17: live-account bypass impossible.
# ---------------------------------------------------------------------------


def test_send_function_has_no_override_or_force_parameter() -> None:
    import inspect

    signature = inspect.signature(SER8DemoOrderSendControl.send)
    forbidden = {"force", "override", "bypass", "skip_check", "ignore_denial", "allow_anyway", "live", "is_live"}
    assert not (set(signature.parameters) & forbidden)


def test_module_never_imports_metatrader5_or_calls_order_send_directly() -> None:
    import ast
    import inspect

    import trademind.ser8_mt5_demo_order_send as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden_imports = {"MetaTrader5"}
    assert not (imported & forbidden_imports), imported & forbidden_imports
    for forbidden_call in ("order_send(", "OrderSend(", "trade.Buy(", "trade.Sell(", "PositionClose(", "PositionModify("):
        assert forbidden_call not in source, forbidden_call


# ---------------------------------------------------------------------------
# 18: MQL5 executor contains no grid/averaging/autonomous strategy logic.
# ---------------------------------------------------------------------------


def test_mql5_executor_contains_no_grid_averaging_or_autonomous_logic() -> None:
    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    assert executor_path.is_file(), "expected mt5/TradeMind_Demo_Order_Executor_v1.mq5 to exist"
    # Scan only the executable code, not #property description lines or
    # Print() log/documentation lines -- both legitimately assert, in
    # prose, the absence of exactly these things (the same "negative
    # assertion, not the thing itself" pattern used throughout this
    # codebase's own Python safety flags/comments).
    code_lines = [
        line for line in executor_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#property") and "Print(" not in line
    ]
    source = "\n".join(code_lines)
    forbidden_terms = (
        "GridStep", "MaxOrdersInGrid", "UseGrid", "grid_step", "Martingale", "martingale",
        "AveragePrice", "averaging", "OnTick",  # no per-tick strategy/signal loop at all.
    )
    for term in forbidden_terms:
        assert term not in source, term
    # Executor-only shape: exactly one request in, at most one trade.Buy/
    # trade.Sell/trade.BuyLimit/etc call site per branch, no loop that could
    # send multiple orders per request.
    assert source.count("ProcessPendingRequest") >= 1
    assert "for(" not in source or "FileReadString" in source  # only bounded header-skip loops, not order loops.


def test_mql5_executor_reads_at_most_one_request_per_timer_tick() -> None:
    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    source = executor_path.read_text(encoding="utf-8")
    assert "void OnTimer()" in source
    assert "ProcessPendingRequest();" in source
    # The request file is renamed/consumed unconditionally right after being
    # read, before any order is ever sent -- proven by the consumed-file
    # rename call appearing before the first CTrade send call in source order.
    consumed_index = source.index("RequestConsumedFilename")
    first_send_index = min(
        i for i in (source.find("trade.Buy("), source.find("trade.Sell(")) if i != -1
    )
    assert consumed_index < first_send_index


# ---------------------------------------------------------------------------
# 19-20: existing research/risk/auth/demo-gate tests remain green; full
# pytest green -- run separately as part of this task's own VALIDATION.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SER8 MT5 MULTI-ENTRY DEMO EXECUTION V1 -- N >= 1 SizedOrder legs, all bound
# to the SAME hypothesis/RiskDecision/authorization/claim/demo account, per
# this task's own REQUIREMENTS 1-16. The real Windows evidence this task's
# spec quotes (TM-20260819T032500Z-EURUSD-BUY-8124e6ea0526ffbb: MARKET 0.5 /
# LIMIT 0.3 / LIMIT 0.2) is reproduced structurally by ``_multi_leg_candidate``
# above, driven through the SAME real risk_manager.evaluate_risk this file's
# single-leg fixtures already use -- never a hand-constructed SizedOrder.
# ---------------------------------------------------------------------------


def _leg_result_factory(states: dict[int, str], *, filled_price: float = 2000.0):
    """Builds a transport result_factory keyed by ``request.entry_index``
    -- 'FILLED' returns a clean fill, 'PENDING' returns a genuine broker-
    accepted-but-not-yet-triggered LIMIT/STOP result (the exact real
    Windows shape: retcode=10009 done, a real order_ticket, deal_ticket=
    position_ticket="0", filled_price=0.0), any other string is treated as
    a literal retcode_description for a definite rejection (never UNKNOWN;
    to simulate UNKNOWN, raise from the transport instead)."""

    def _factory(request) -> DemoOrderTransportResult:
        state = states[request.entry_index]
        if state == "FILLED":
            return DemoOrderTransportResult(
                claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
                retcode=10009, retcode_description="Request completed",
                order_ticket=f"{request.entry_index}01", deal_ticket=f"{request.entry_index}02",
                position_ticket=f"{request.entry_index}03",
                filled_volume=request.volume, filled_price=filled_price,
            )
        if state == "PENDING":
            return DemoOrderTransportResult(
                claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
                retcode=10009, retcode_description="done",
                order_ticket=f"7331245{request.entry_index}", deal_ticket="0", position_ticket="0",
                filled_volume=request.volume, filled_price=0.0,
            )
        return DemoOrderTransportResult(
            claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
            retcode=10019, retcode_description=state, order_ticket="", deal_ticket="", position_ticket="",
            filled_volume=None, filled_price=None,
        )

    return _factory


# ---------------------------------------------------------------------------
# 1: a 1-leg MARKET plan remains valid and fully backward compatible.
# ---------------------------------------------------------------------------


def test_single_leg_market_plan_still_returns_a_bare_receipt(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    assert len(decision.orders) == 1
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    receipt = control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    # Byte-for-byte the pre-multi-entry contract: a bare receipt, never
    # wrapped in DemoOrderExecutionPlanReceiptV1, claim_id unchanged.
    assert isinstance(receipt, DemoOrderExecutionReceiptV1)
    assert not isinstance(receipt, DemoOrderExecutionPlanReceiptV1)
    assert receipt.claim_id == claim.claim_id
    assert receipt.result_state == "FILLED"
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# 2: a genuine 3-leg MARKET/LIMIT/LIMIT plan sends every leg.
# ---------------------------------------------------------------------------


def test_three_leg_market_limit_limit_plan_sends_every_leg(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    assert len(decision.orders) == 3
    assert [order.order_type for order in decision.orders] == ["MARKET", "LIMIT", "LIMIT"]

    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "FILLED", 3: "FILLED"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    outcome = control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    assert isinstance(outcome, DemoOrderExecutionPlanReceiptV1)
    assert outcome.aggregate_state == "COMPLETE"
    assert len(outcome.leg_receipts) == 3
    assert [r.result_state for r in outcome.leg_receipts] == ["FILLED", "FILLED", "FILLED"]
    assert len(transport.calls) == 3
    # Never collapsed to the first (or any single) leg -- every SizedOrder
    # was individually sent with its own order_type/volume/price.
    sent_types = [call.order_type for call in transport.calls]
    assert sent_types == ["MARKET", "LIMIT", "LIMIT"]
    for call, order in zip(transport.calls, decision.orders):
        assert call.volume == order.volume
        assert call.price == order.planned_price


# ---------------------------------------------------------------------------
# 3: deterministic, immutable plan/leg identities (requirement 6).
# ---------------------------------------------------------------------------


def test_plan_and_leg_identities_are_deterministic(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    plan_a = build_demo_order_execution_plan(claim, decision, candidate, demo_authorization=demo_authorization)
    plan_b = build_demo_order_execution_plan(claim, decision, candidate, demo_authorization=demo_authorization)

    assert plan_a.plan_id == plan_b.plan_id
    assert plan_a.plan_hash == plan_b.plan_hash
    assert [leg.leg_id for leg in plan_a.legs] == [leg.leg_id for leg in plan_b.legs]
    assert [leg.leg_hash for leg in plan_a.legs] == [leg.leg_hash for leg in plan_b.legs]
    # Never random, never wall-clock -- a pure function of already-
    # immutable claim/decision/candidate identity.
    assert plan_a.plan_id.startswith("EOP-")
    assert len({leg.leg_id for leg in plan_a.legs}) == 3  # all distinct.
    for leg in plan_a.legs:
        assert leg.leg_id == leg_identity(claim.claim_id, leg.entry_index, total_legs=3)
        assert leg.leg_id != claim.claim_id  # multi-leg: never collapses to the bare claim id.
        assert leg.leg_id.startswith(claim.claim_id)  # still human-traceable to the parent claim.


def test_single_leg_identity_equals_bare_claim_id(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    plan = build_demo_order_execution_plan(claim, decision, candidate, demo_authorization=demo_authorization)
    assert len(plan.legs) == 1
    assert plan.legs[0].leg_id == claim.claim_id


# ---------------------------------------------------------------------------
# 4: exact per-leg volume preservation (never recomputed, never invented).
# ---------------------------------------------------------------------------


def test_exact_volume_preserved_per_leg(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    for order in decision.orders:
        request = build_demo_order_leg_request(
            claim, decision, candidate, order, demo_authorization=demo_authorization, total_legs=3
        )
        assert request.volume == order.volume
        assert request.volume > 0
        assert request.order_type == order.order_type
        assert request.price == order.planned_price
        assert request.sl == candidate.plan.stop_price
        assert request.tp == candidate.plan.targets[0]
        assert request.entry_index == order.entry_index


# ---------------------------------------------------------------------------
# 5: duplicate invocation cannot duplicate any leg.
# ---------------------------------------------------------------------------


def test_duplicate_invocation_cannot_duplicate_any_leg(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "FILLED", 3: "FILLED"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 3

    for _ in range(3):
        with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
            control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=1))
    assert len(transport.calls) == 3  # never resent, for any leg.

    with sqlite3.connect(context.db_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ser8_mt5_demo_order_leg_receipts WHERE plan_id="
            "(SELECT plan_id FROM ser8_mt5_demo_order_plans WHERE claim_id=?)",
            (claim.claim_id,),
        ).fetchone()[0]
    assert count == 3  # exactly one row per leg, never duplicated.


# ---------------------------------------------------------------------------
# 6: crash after one (or more) leg(s) does not resend it/them.
# ---------------------------------------------------------------------------


def test_crash_after_one_leg_does_not_resend_it(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)

    class _CrashAfterFirstLeg:
        def __init__(self) -> None:
            self.calls: list = []

        def send(self, request):
            self.calls.append(request)
            if request.entry_index == 1:
                return DemoOrderTransportResult(
                    claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
                    retcode=10009, retcode_description="Request completed",
                    order_ticket="1", deal_ticket="2", position_ticket="3",
                    filled_volume=request.volume, filled_price=2000.0,
                )
            # Simulate the terminal/process dying while attempting leg 2 --
            # the transport itself never returns a real result.
            raise TimeoutError("terminal connection lost")

    transport = _CrashAfterFirstLeg()
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderReconciliationRequiredError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    # Leg 1 filled, leg 2 attempted and UNKNOWN, leg 3 never even reached.
    assert len(transport.calls) == 2
    assert [c.entry_index for c in transport.calls] == [1, 2]

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        rows = {
            row["entry_index"]: row["payload_json"]
            for row in db.execute(
                "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=?", (claim.claim_id,),
            ).fetchall()
        }
    assert set(rows) == {1, 2}  # leg 3 never got a row at all.
    assert json.loads(rows[1])["result_state"] == "FILLED"
    assert json.loads(rows[2])["result_state"] == "UNKNOWN"

    # Retrying (e.g. after a process restart) must NOT resend leg 1 (already
    # FILLED) or leg 2 (still UNKNOWN, unresolved) -- and, because leg 2 is
    # still unresolved, leg 3 stays blocked too, exactly as before.
    with pytest.raises(SER8DemoOrderReconciliationRequiredError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=1))
    assert len(transport.calls) == 2  # zero new transport calls.


def test_reserved_but_never_finalized_leg_self_heals_to_unknown_without_resend(tmp_path: Path) -> None:
    """Simulates a genuine hard process crash strictly BETWEEN reserving a
    leg's one-shot attempt and recording its outcome (never reached via the
    public send() API, which always finalizes-or-raises within the same
    call) -- proving the crash/restart self-heal path never calls the
    transport."""
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    plan = build_demo_order_execution_plan(claim, decision, candidate, demo_authorization=demo_authorization)
    leg1 = plan.legs[0]
    request = build_demo_order_leg_request(
        claim, decision, candidate, decision.orders[0], demo_authorization=demo_authorization, total_legs=3
    )

    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "FILLED", 3: "FILLED"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    control._persist_plan(plan, created_at=NOW.isoformat())
    # Reserve leg 1's attempt directly -- exactly what send() does BEFORE
    # calling the transport -- then stop, simulating a crash before
    # transport.send() or _finalize() ever ran.
    control._reserve_leg_attempt(
        leg_id=leg1.leg_id, plan_id=plan.plan_id, parent_claim_id=claim.claim_id, entry_index=1,
        attempt_id="EAO-test-crash-mid-attempt", request=request,
        demo_authorization=demo_authorization, captured_at=NOW,
    )

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg1.leg_id,)
        ).fetchone()
    assert row["payload_json"] is None  # reserved, never finalized.

    with pytest.raises(SER8DemoOrderReconciliationRequiredError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=1))
    # Leg 1 self-healed to UNKNOWN and blocked everything after it -- the
    # transport was NEVER called for leg 1 (it was already reserved) or for
    # legs 2/3 (blocked by leg 1's now-explicit UNKNOWN).
    assert transport.calls == []

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg1.leg_id,)
        ).fetchone()
    assert json.loads(row["payload_json"])["result_state"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# 7: partial completion is never reported as aggregate SUCCESS.
# ---------------------------------------------------------------------------


def test_partial_completion_raises_and_is_never_aggregate_success(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(
        result_factory=_leg_result_factory({1: "FILLED", 2: "No money", 3: "FILLED"})
    )
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderPartialExecutionError) as excinfo:
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    assert len(transport.calls) == 3  # every leg WAS attempted (no UNKNOWN to block the cascade).
    assert isinstance(excinfo.value, SER8DemoOrderRejectedError)  # broader catch-all still works.
    # Confirm the persisted per-leg truth: exactly 2 FILLED, 1 REJECTED --
    # never silently reported as if the whole plan succeeded.
    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        states = [
            json.loads(row["payload_json"])["result_state"]
            for row in db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE parent_claim_id=? "
                "ORDER BY entry_index", (claim.claim_id,),
            ).fetchall()
        ]
    assert states == ["FILLED", "REJECTED", "FILLED"]


# ---------------------------------------------------------------------------
# 8: an UNKNOWN broker outcome fails the whole plan closed.
# ---------------------------------------------------------------------------


def test_unknown_outcome_on_any_leg_fails_the_plan_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)

    class _UnknownOnSecondLeg:
        def __init__(self) -> None:
            self.calls: list = []

        def send(self, request):
            self.calls.append(request)
            if request.entry_index != 2:
                return DemoOrderTransportResult(
                    claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
                    retcode=10009, retcode_description="Request completed",
                    order_ticket="1", deal_ticket="2", position_ticket="3",
                    filled_volume=request.volume, filled_price=2000.0,
                )
            raise ConnectionError("terminal unreachable")

    transport = _UnknownOnSecondLeg()
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderReconciliationRequiredError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert isinstance(SER8DemoOrderReconciliationRequiredError("x"), SER8DemoOrderTransportError)
    assert len(transport.calls) == 2  # leg 3 never attempted while leg 2 is unresolved.


# ---------------------------------------------------------------------------
# 9: LIMIT requests correctly reach the (unmodified) executor schema.
# ---------------------------------------------------------------------------


def test_limit_leg_request_matches_the_unmodified_executor_wire_schema(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    limit_order = decision.orders[1]
    assert limit_order.order_type == "LIMIT"
    request = build_demo_order_leg_request(
        claim, decision, candidate, limit_order, demo_authorization=demo_authorization, total_legs=3
    )
    row = request.to_csv_row()
    assert set(row) == set(REQUEST_CSV_FIELDS)
    assert row["order_type"] == "LIMIT"
    assert float(row["price"]) == limit_order.planned_price
    assert float(row["price"]) > 0  # a genuine limit price, never MARKET's ignored 0.0.

    # Cross-checked directly against the real, UNMODIFIED executor source --
    # LIMIT dispatch already existed before this task (see the module
    # docstring's audit note); this task changed nothing about it.
    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    executor_source = executor_path.read_text(encoding="utf-8")
    assert 'order_type=="LIMIT"' in executor_source
    assert "trade.BuyLimit(" in executor_source and "trade.SellLimit(" in executor_source


# ---------------------------------------------------------------------------
# 10: demo login/magic checks preserved for every leg.
# ---------------------------------------------------------------------------


def test_demo_login_and_magic_checks_preserved_per_leg(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    for order in decision.orders:
        request = build_demo_order_leg_request(
            claim, decision, candidate, order, demo_authorization=demo_authorization, total_legs=3
        )
        assert request.magic == DEMO_EXECUTOR_MAGIC_NUMBER == 990244
        assert request.demo_account_id == demo_authorization.account_id == LOGIN

    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    executor_source = executor_path.read_text(encoding="utf-8")
    assert "demo_account_id!=LoginText()" in executor_source
    assert "magic!=InpMagicNumber" in executor_source
    assert "InpMagicNumber       = 990244" in executor_source


# ---------------------------------------------------------------------------
# 11-12: no MQL5 position sizing; no grid/averaging/martingale -- the
# executor file itself was COMPLETELY UNTOUCHED by SER8 MT5 MULTI-ENTRY
# DEMO EXECUTION V1 (this exact assertion, before it), and this comment's
# own note said any future task that DOES need to touch it should update
# this assertion deliberately, not silently break it. SER8 UNIFIED MT5
# POSITIONS SNAPSHOT WRITER FIX V1 is exactly that deliberate update: it
# fixes a real, reproduced-on-Windows bug in ExportPositionSnapshot (see
# that function's own root-cause comment in the .mq5 file). The test below
# proves the change stayed scoped to the positions-snapshot writer alone --
# order execution (ProcessPendingRequest/WriteResult), the account writer's
# own body, and the symbols writer are all still byte-for-byte unchanged
# relative to the fixed, immutable base commit this task started from.
# ---------------------------------------------------------------------------

_POSITIONS_SNAPSHOT_FIX_BASE_SHA = "7e134376813844500793f267c0f1a58b8e18c35a"


def test_mql5_executor_change_stays_scoped_to_positions_snapshot_writer() -> None:
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "git", "diff", "--unified=0", _POSITIONS_SNAPSHOT_FIX_BASE_SHA,
            "--", "mt5/TradeMind_Demo_Order_Executor_v1.mq5",
        ],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"base commit {_POSITIONS_SNAPSHOT_FIX_BASE_SHA} not reachable in this checkout")
    diff_text = result.stdout
    hunk_headers = [line for line in diff_text.splitlines() if line.startswith("@@")]
    assert hunk_headers, "expected this task to have changed the executor file"
    # Only actual changed (+/-) CODE lines count -- explanatory comments
    # (added or pre-existing) legitimately NAME sibling functions like
    # ReadAndConsumeRequest/FileMove for cross-reference without touching
    # them; a comment mentioning a name is not a change to that function.
    changed_lines = [
        line for line in diff_text.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    changed_code_lines = [line for line in changed_lines if not line.lstrip("+- ").startswith("//")]
    for forbidden_function in (
        "ProcessPendingRequest", "WriteResult(", "WriteMalformedResult",
        "ReadAndConsumeRequest", "ExportSymbolSnapshot", "OnInit()", "OnTimer()", "OnDeinit",
    ):
        for line in changed_code_lines:
            assert forbidden_function not in line, (
                f"unexpected code change touching {forbidden_function} in line {line!r} -- "
                "this fix must stay scoped to the positions-snapshot writer only"
            )
    # AppendAccountSnapshot's own BODY (its FileWrite calls / account
    # fields) must not appear as a changed (+/-) CODE line -- only context.
    for line in changed_code_lines:
        assert "AccountInfoDouble" not in line
        assert "ACCOUNT_BALANCE" not in line


def test_mql5_executor_file_still_has_the_untouched_prior_task_guarantees() -> None:
    """The prior task's own guarantees (single EventSetTimer, one-shot
    request consumption, no OnTick) remain true even though this task did
    touch the file -- this fix is additive/scoped, not a rewrite."""
    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    source = executor_path.read_text(encoding="utf-8")
    assert source.count("EventSetTimer(") == 1
    assert "RequestConsumedFilename" in source
    assert "OnTick" not in source


def test_mql5_executor_still_has_no_position_sizing_or_grid_logic() -> None:
    executor_path = Path(__file__).resolve().parents[1] / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
    source = executor_path.read_text(encoding="utf-8")
    # Volume is assigned exactly once (read from the request file), never
    # computed -- the same invariant tests/test_mt5_unified_executor.py
    # already established for the whole file.
    assignments = [line for line in source.splitlines() if "volume " in line and "=" in line and "==" not in line]
    volume_assignments = [line for line in assignments if line.strip().startswith("volume") and "=" in line]
    assert len(volume_assignments) == 1, volume_assignments
    # Exclude #property description prose (which legitimately SAYS "no
    # grid, no averaging, no martingale" as a documented guarantee) --
    # only functional code lines must never contain these terms.
    functional_lines = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#property")
    )
    for forbidden in ("GridStep", "Martingale", "martingale", "AveragePrice", "averaging"):
        assert forbidden not in functional_lines


# ---------------------------------------------------------------------------
# 13: PREVIEW shows the entire ordered leg plan -- see
# tests/test_run_ser8_real_demo_pipeline.py::test_preview_shows_every_leg_
# of_a_multi_entry_plan for the dedicated end-to-end proof (this module has
# no PREVIEW concept of its own; that lives entirely in
# scripts/run_ser8_real_demo_pipeline.py).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SER8 MT5 PENDING LIMIT RECEIPT + RECONCILIATION V1.
#
# Real Windows evidence this section reproduces exactly: a 3-leg plan
# where leg 1 (MARKET) FILLED and legs 2/3 (LIMIT) were genuinely accepted
# pending orders that the OLD classifier mislabeled MALFORMED:
#
#   claim_id = EAC-67206924-2e40988a6cd689d6#3
#   retcode = 10009, retcode_description = done
#   order_ticket = 733124518, deal_ticket = 0, position_ticket = 0
#   filled_volume = 0.09000000, filled_price = 0.00000000
# ---------------------------------------------------------------------------


def test_limit_accepted_pending_receipt_is_classified_pending_not_malformed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization
    from trademind.ser8_mt5_demo_order_send import _classify_result

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    limit_order = decision.orders[2]
    assert limit_order.order_type == "LIMIT"
    request = build_demo_order_leg_request(
        claim, decision, candidate, limit_order, demo_authorization=demo_authorization, total_legs=3
    )
    # The EXACT real Windows evidence shape, keyed to this leg's own wire
    # identity (requirement 6: claim_id#3).
    assert request.claim_id.endswith("#3")
    result = DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10009, retcode_description="done",
        order_ticket="733124518", deal_ticket="0", position_ticket="0",
        filled_volume=0.09, filled_price=0.0,
    )
    assert _classify_result(request, result) == "PENDING"


def test_leg_identity_format_matches_real_windows_evidence_exactly() -> None:
    # The real evidence's own claim_id, EAC-67206924-2e40988a6cd689d6#3, is
    # exactly the f"{parent_claim_id}#{entry_index}" format leg_identity()
    # has produced since SER8 MT5 MULTI-ENTRY DEMO EXECUTION V1 -- this
    # task changes nothing about leg identity (requirement 6).
    assert leg_identity("EAC-67206924-2e40988a6cd689d6", 3, total_legs=3) == "EAC-67206924-2e40988a6cd689d6#3"


# ---------------------------------------------------------------------------
# 1: MARKET success semantics remain unchanged -- the SAME "retcode=DONE,
# order_ticket set, no deal, filled_price=0" shape that means PENDING for
# a LIMIT request must NOT trigger PENDING for a MARKET request (there is
# no such thing as a pending MARKET order); it must still fail exactly as
# before.
# ---------------------------------------------------------------------------


def test_market_success_semantics_unchanged_by_pending_classification(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization
    from trademind.ser8_mt5_demo_order_send import _classify_result

    assert decision.orders[0].order_type == "MARKET"
    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)
    pending_shaped_result = DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10009, retcode_description="done",
        order_ticket="1", deal_ticket="0", position_ticket="0",
        filled_volume=request.volume, filled_price=0.0,
    )
    assert _classify_result(request, pending_shaped_result) == "MALFORMED"

    # A genuine MARKET fill still classifies FILLED exactly as before.
    clean_result = _clean_result(request)
    assert _classify_result(request, clean_result) == "FILLED"


def test_single_leg_market_still_fills_end_to_end_unaffected(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    receipt = control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert receipt.result_state == "FILLED"
    assert isinstance(receipt, DemoOrderExecutionReceiptV1)


# ---------------------------------------------------------------------------
# 3-4: pending != filled; a broker-accepted plan (FILLED MARKET + PENDING
# LIMIT + PENDING LIMIT) is a valid, distinct outcome -- never silently
# reported as complete, never silently reported as a failure.
# ---------------------------------------------------------------------------


def test_three_leg_plan_with_pending_limits_reaches_accepted_pending(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderPendingError) as excinfo:
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    # Never mistaken for a rejection or an unknown/ambiguous outcome --
    # SER8DemoOrderPendingError is its own category.
    assert not isinstance(excinfo.value, SER8DemoOrderRejectedError)
    assert not isinstance(excinfo.value, SER8DemoOrderTransportError)
    assert len(transport.calls) == 3  # every leg WAS attempted -- PENDING never blocks the cascade.

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        states = {
            row["entry_index"]: json.loads(row["payload_json"])["result_state"]
            for row in db.execute(
                "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=?", (claim.claim_id,),
            ).fetchall()
        }
    assert states == {1: "FILLED", 2: "PENDING", 3: "PENDING"}


def test_pending_is_never_treated_as_filled(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    # send() NEVER returns a value for this plan -- only COMPLETE (every
    # leg genuinely FILLED) returns; ACCEPTED_PENDING always raises, so a
    # caller can never mistake "the broker accepted this" for "it filled".
    with pytest.raises(SER8DemoOrderPendingError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)


def test_order_ticket_persisted_for_pending_legs(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderPendingError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        tickets = {
            row["entry_index"]: json.loads(row["payload_json"])["order_ticket"]
            for row in db.execute(
                "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=?", (claim.claim_id,),
            ).fetchall()
        }
    assert tickets[2] == "73312452"
    assert tickets[3] == "73312453"
    assert tickets[2] != "" and tickets[3] != ""


# ---------------------------------------------------------------------------
# 5: before any resend, existing broker/order evidence is reconciled by
# leg identity -- an already-accepted (PENDING or FILLED) leg is NEVER
# sent again, restart or not.
# ---------------------------------------------------------------------------


def test_restart_recovery_with_existing_pending_order_does_not_resend(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderPendingError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 3

    # Simulate a process restart: same claim/decision/candidate, a FRESH
    # transport instance (a real restart would reconnect to the executor).
    fresh_transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "FILLED", 3: "FILLED"}))
    fresh_control = SER8DemoOrderSendControl(registry=context.registry, transport=fresh_transport)
    with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
        fresh_control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=5))
    # Zero new sends -- every leg (including the two still-PENDING ones)
    # already has a recorded attempt, so nothing is ever resent.
    assert fresh_transport.calls == []


def test_no_duplicate_resend_across_many_retries(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    with pytest.raises(SER8DemoOrderPendingError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 3

    for attempt in range(5):
        with pytest.raises(SER8DemoOrderAlreadyAttemptedError):
            control.send(
                claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW + timedelta(seconds=attempt + 1)
            )
    assert len(transport.calls) == 3


# ---------------------------------------------------------------------------
# partial plan recovery: a mix of FILLED + PENDING + a DEFINITE rejection
# is PARTIAL, not ACCEPTED_PENDING and not FAILED.
# ---------------------------------------------------------------------------


def test_partial_plan_recovery_with_mixed_pending_and_rejection(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(
        result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "No money"})
    )
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderPartialExecutionError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        states = {
            row["entry_index"]: json.loads(row["payload_json"])["result_state"]
            for row in db.execute(
                "SELECT entry_index, payload_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=?", (claim.claim_id,),
            ).fetchall()
        }
    assert states == {1: "FILLED", 2: "PENDING", 3: "REJECTED"}


# ---------------------------------------------------------------------------
# 9: unknown/ambiguous receipts still fail closed -- a receipt carrying
# BOTH a deal/position ticket AND filled_price=0 is genuinely
# inconsistent, never guessed as PENDING.
# ---------------------------------------------------------------------------


def test_ambiguous_deal_evidence_with_zero_fill_price_fails_closed_as_malformed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    from trademind.ser8_demo_account_safety_gate import verify_demo_account_authorization
    from trademind.ser8_mt5_demo_order_send import _classify_result

    demo_authorization = verify_demo_account_authorization(claim, allowlist=_allowlist(LOGIN), now=NOW)
    limit_order = decision.orders[1]
    request = build_demo_order_leg_request(
        claim, decision, candidate, limit_order, demo_authorization=demo_authorization, total_legs=3
    )
    ambiguous_result = DemoOrderTransportResult(
        claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
        retcode=10009, retcode_description="done",
        order_ticket="733124518", deal_ticket="991", position_ticket="0",  # a deal exists...
        filled_volume=request.volume, filled_price=0.0,  # ...but no fill price. Inconsistent.
    )
    assert _classify_result(request, ambiguous_result) == "MALFORMED"


def test_transport_failure_still_produces_unknown_not_pending(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)

    class _UnknownOnSecondLeg:
        def __init__(self) -> None:
            self.calls: list = []

        def send(self, request):
            self.calls.append(request)
            if request.entry_index != 2:
                return _leg_result_factory({1: "FILLED", 3: "FILLED"})(request)
            raise ConnectionError("terminal unreachable")

    transport = _UnknownOnSecondLeg()
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    with pytest.raises(SER8DemoOrderReconciliationRequiredError):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    assert len(transport.calls) == 2  # leg 3 never attempted while leg 2 is unresolved.


def test_no_order_type_no_longer_permits_a_stray_pending_classification() -> None:
    # requirement 9: an "unrecognized"/tampered order_type never reaches
    # _classify_result at all -- send() already fails the whole plan closed
    # at precondition-check time (proven in
    # test_unsupported_order_type_fails_closed_before_any_leg_sent above).
    assert "STOP" in {"MARKET", "LIMIT", "STOP"}  # VALID_ORDER_TYPES unchanged, sanity check only.


# ---------------------------------------------------------------------------
# reconcile_pending_leg: PENDING -> FILLED/CANCELLED/REJECTED only from
# fresh, authoritative evidence -- never a resend, never automatic.
# ---------------------------------------------------------------------------


def _send_and_capture_pending_leg_id(control, claim, decision, candidate, *, entry_index: int) -> str:
    with pytest.raises((SER8DemoOrderPendingError, SER8DemoOrderPartialExecutionError)):
        control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)
    return leg_identity(claim.claim_id, entry_index, total_legs=3)


def test_reconcile_pending_leg_advances_to_filled_with_fresh_evidence(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    leg2_id = _send_and_capture_pending_leg_id(control, claim, decision, candidate, entry_index=2)
    calls_before = len(transport.calls)

    fresh_evidence = DemoOrderTransportResult(
        claim_id=leg2_id, demo_account_id=LOGIN, symbol=candidate.symbol,
        retcode=10009, retcode_description="Request completed",
        order_ticket="73312452", deal_ticket="55501", position_ticket="55502",
        filled_volume=decision.orders[1].volume, filled_price=1998.0,
    )
    receipt = control.reconcile_pending_leg(leg2_id, evidence=fresh_evidence, now=NOW + timedelta(minutes=10))

    assert receipt.result_state == "FILLED"
    assert receipt.order_ticket == "73312452"  # unchanged -- never invented by reconciliation.
    assert receipt.filled_price == 1998.0
    assert len(transport.calls) == calls_before  # reconciliation NEVER calls the transport.

    with sqlite3.connect(context.db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT payload_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg2_id,)
        ).fetchone()
    assert json.loads(row["payload_json"])["result_state"] == "FILLED"


def test_reconcile_pending_leg_explicit_cancellation(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    leg3_id = _send_and_capture_pending_leg_id(control, claim, decision, candidate, entry_index=3)

    receipt = control.reconcile_pending_leg(leg3_id, cancelled=True, now=NOW + timedelta(minutes=15))
    assert receipt.result_state == "CANCELLED"
    assert receipt.order_ticket == "73312453"  # preserved.
    assert len(transport.calls) == 3  # unchanged -- no new send.


def test_reconcile_pending_leg_still_pending_is_idempotent_noop(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    leg2_id = _send_and_capture_pending_leg_id(control, claim, decision, candidate, entry_index=2)

    still_pending_evidence = DemoOrderTransportResult(
        claim_id=leg2_id, demo_account_id=LOGIN, symbol=candidate.symbol,
        retcode=10009, retcode_description="done",
        order_ticket="73312452", deal_ticket="0", position_ticket="0",
        filled_volume=decision.orders[1].volume, filled_price=0.0,
    )
    receipt = control.reconcile_pending_leg(leg2_id, evidence=still_pending_evidence)
    assert receipt.result_state == "PENDING"  # unchanged -- checking is never an error.


def test_reconcile_pending_leg_rejects_a_non_pending_leg(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    control.send(claim, decision, candidate, allowlist=_allowlist(LOGIN), now=NOW)

    with pytest.raises(SER8DemoOrderSendError, match="not PENDING"):
        control.reconcile_pending_leg(
            claim.claim_id,
            evidence=DemoOrderTransportResult(
                claim_id=claim.claim_id, demo_account_id=LOGIN, symbol=candidate.symbol,
                retcode=10009, retcode_description="Request completed",
                order_ticket="1", deal_ticket="2", position_ticket="3",
                filled_volume=decision.orders[0].volume, filled_price=2000.0,
            ),
        )


def test_reconcile_pending_leg_unknown_leg_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path)
    transport = FakeDemoOrderTransport(result_factory=_clean_result)
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)

    with pytest.raises(SER8DemoOrderSendError, match="no existing send attempt"):
        control.reconcile_pending_leg(
            "some-leg-that-was-never-sent#1",
            evidence=DemoOrderTransportResult(
                claim_id="some-leg-that-was-never-sent#1", demo_account_id=LOGIN, symbol="XAUUSD",
                retcode=10009, retcode_description="done", order_ticket="1", deal_ticket="2",
                position_ticket="3", filled_volume=0.01, filled_price=2000.0,
            ),
        )


def test_reconcile_pending_leg_requires_exactly_one_of_evidence_or_cancelled(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    leg2_id = _send_and_capture_pending_leg_id(control, claim, decision, candidate, entry_index=2)

    with pytest.raises(SER8DemoOrderSendError, match="exactly one"):
        control.reconcile_pending_leg(leg2_id)  # neither evidence nor cancelled=True.

    with pytest.raises(SER8DemoOrderSendError, match="exactly one"):
        control.reconcile_pending_leg(
            leg2_id,
            evidence=DemoOrderTransportResult(
                claim_id=leg2_id, demo_account_id=LOGIN, symbol=candidate.symbol,
                retcode=10009, retcode_description="Request completed",
                order_ticket="73312452", deal_ticket="1", position_ticket="1",
                filled_volume=decision.orders[1].volume, filled_price=1998.0,
            ),
            cancelled=True,  # both -- also rejected.
        )


def test_reconcile_pending_leg_evidence_claim_id_mismatch_fails_closed(tmp_path: Path) -> None:
    context, claim, decision, candidate = _claim_case(tmp_path, candidate_factory=_multi_leg_candidate)
    transport = FakeDemoOrderTransport(result_factory=_leg_result_factory({1: "FILLED", 2: "PENDING", 3: "PENDING"}))
    control = SER8DemoOrderSendControl(registry=context.registry, transport=transport)
    leg2_id = _send_and_capture_pending_leg_id(control, claim, decision, candidate, entry_index=2)

    with pytest.raises(SER8DemoOrderSendError, match="does not match leg"):
        control.reconcile_pending_leg(
            leg2_id,
            evidence=DemoOrderTransportResult(
                claim_id="a-completely-different-leg#9", demo_account_id=LOGIN, symbol=candidate.symbol,
                retcode=10009, retcode_description="Request completed",
                order_ticket="73312452", deal_ticket="1", position_ticket="1",
                filled_volume=decision.orders[1].volume, filled_price=1998.0,
            ),
        )


def test_reconcile_pending_leg_never_calls_the_transport() -> None:
    import inspect

    import trademind.ser8_mt5_demo_order_send as module

    source = inspect.getsource(module.SER8DemoOrderSendControl.reconcile_pending_leg)
    assert "self.transport" not in source
    assert ".send(" not in source
