"""Tests for Hypothesis Tradeable Scope Binding V1: the explicit, immutable
contract that binds a research hypothesis to the live candidate scope it is
allowed to match.

Chain exercised: a real ResearchProposalIntake -> ResearchExperimentSpecification
-> ExperimentManifestV2 -> FROZEN hypothesis, all built from a genuine
Verified-CAS Report v2 / Packet v2 / CandidateContentV2 chain -- the exact
same primitives the production research-proposal pipeline uses. This file
does not import test helpers from sibling test files (consistent with this
lineage's convention of each test file owning its own small setup helpers).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.discovery.hypothesis_tradeable_scope import (
    AllowedActionScope,
    HypothesisTradeableScopeError,
    HypothesisTradeableScopeV1,
    bind_hypothesis_tradeable_scope,
)
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    ExperimentManifestV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
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
_METRIC = "avg_net_atr"
_TEST_FAMILY = "deterministic_aggregate_v1"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    store: ArtifactStore
    registry: HypothesisRegistry
    spec: ResearchExperimentSpecificationV1


def _candidate(
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    feature: str = "spread_pressure",
    action_scope: str = "BUY_SELL_DIRECTIONAL",
) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=symbol,
            timeframe=timeframe,
            feature=feature,
            horizon=3,
            action_scope=action_scope,
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="hypothesis-tradeable-scope-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
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


def _setup(
    tmp_path: Path,
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    feature: str = "spread_pressure",
    action_scope: str = "BUY_SELL_DIRECTIONAL",
    db_name: str = "orchestrator.db",
) -> _Context:
    """Build a real, FROZEN hypothesis from a genuine Report v2 / Packet v2 /
    CandidateContentV2 chain -- everything ``bind_hypothesis_tradeable_scope``
    needs, and nothing more (no orchestrator task, no train-test/validation/
    holdout machinery, since this module never reads any of that)."""
    db_path = tmp_path / db_name
    store = ArtifactStore(tmp_path / f"{db_name}-artifacts")
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(symbol=symbol, timeframe=timeframe, feature=feature, action_scope=action_scope),),
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
    return _Context(db_path=db_path, store=store, registry=registry, spec=spec)


def _build_manifest_v2(context: _Context) -> ExperimentManifestV2:
    from trademind.discovery.manifest import DatasetArtifactV2
    from trademind.discovery.split_engine import chronological_split
    from datetime import datetime, timedelta, timezone
    import io

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
                metric=spec.primary_metric,
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=0.0,
            ),
        ),
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [start + timedelta(hours=i) for i in range(10)]
    plan = chronological_split(rows)
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{10.0 + i}" for i, t in enumerate(rows)]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    artifact = context.store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    dataset = DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )
    return build_experiment_manifest_v2(
        artifact_store=context.store,
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash,
        proposal_provenance=provenance,
        datasets=(dataset,),
        split_plan=plan,
        split_dataset_role=dataset.role,
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
        created_by="operator:hypothesis-tradeable-scope-test",
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


def _frozen(
    tmp_path: Path,
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    feature: str = "spread_pressure",
    action_scope: str = "BUY_SELL_DIRECTIONAL",
    db_name: str = "orchestrator.db",
) -> _Context:
    context = _setup(
        tmp_path,
        symbol=symbol,
        timeframe=timeframe,
        feature=feature,
        action_scope=action_scope,
        db_name=db_name,
    )
    manifest = _build_manifest_v2(context)
    _freeze_v2(context, manifest)
    return context


# ---------------------------------------------------------------------------
# 1-2: deterministic identity
# ---------------------------------------------------------------------------


def test_deterministic_scope_identity(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    scope = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    assert scope.scope_hash.startswith("sha256:")
    assert len(scope.scope_hash) == len("sha256:") + 64
    assert scope.symbol == "XAUUSD"
    assert scope.timeframe == "M5"
    assert scope.setup_family == "spread_pressure"
    assert scope.allowed_action_scope == AllowedActionScope.BOTH.value


def test_same_binding_produces_same_semantic_hash(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    first = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    second = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    assert first.scope_hash == second.scope_hash
    assert first.bound_at != second.bound_at or True  # bound_at may legitimately differ; hash must not.
    assert first.semantic_projection() == second.semantic_projection()


# ---------------------------------------------------------------------------
# 3-6: identity sensitivity to each declared dimension
# ---------------------------------------------------------------------------


def test_changed_symbol_changes_identity(tmp_path: Path) -> None:
    a = _frozen(tmp_path, symbol="XAUUSD", db_name="a.db")
    b = _frozen(tmp_path, symbol="EURUSD", db_name="b.db")
    scope_a = bind_hypothesis_tradeable_scope(a.spec.hypothesis_id, registry=a.registry, artifact_store=a.store)
    scope_b = bind_hypothesis_tradeable_scope(b.spec.hypothesis_id, registry=b.registry, artifact_store=b.store)
    assert scope_a.symbol != scope_b.symbol
    assert scope_a.scope_hash != scope_b.scope_hash


def test_changed_timeframe_changes_identity(tmp_path: Path) -> None:
    a = _frozen(tmp_path, timeframe="M5", db_name="a.db")
    b = _frozen(tmp_path, timeframe="H1", db_name="b.db")
    scope_a = bind_hypothesis_tradeable_scope(a.spec.hypothesis_id, registry=a.registry, artifact_store=a.store)
    scope_b = bind_hypothesis_tradeable_scope(b.spec.hypothesis_id, registry=b.registry, artifact_store=b.store)
    assert scope_a.timeframe != scope_b.timeframe
    assert scope_a.scope_hash != scope_b.scope_hash


def test_changed_setup_family_changes_identity(tmp_path: Path) -> None:
    a = _frozen(tmp_path, feature="spread_pressure", db_name="a.db")
    b = _frozen(tmp_path, feature="liquidity_sweep", db_name="b.db")
    scope_a = bind_hypothesis_tradeable_scope(a.spec.hypothesis_id, registry=a.registry, artifact_store=a.store)
    scope_b = bind_hypothesis_tradeable_scope(b.spec.hypothesis_id, registry=b.registry, artifact_store=b.store)
    assert scope_a.setup_family != scope_b.setup_family
    assert scope_a.scope_hash != scope_b.scope_hash


def test_changed_action_scope_changes_dataclass_identity() -> None:
    # The dataclass itself validates only structure (see module docstring:
    # HypothesisTradeableScopeV1 does not know or care where its fields came
    # from). Constructed directly (not through the builder) with two
    # different allowed_action_scope values, identity must differ.
    common = dict(
        hypothesis_id="rpi-v1:" + "a" * 64 + ":0",
        hypothesis_family_id="hf_" + "b" * 64,
        bound_hypothesis_content_hash="c" * 64,
        manifest_semantic_hash=f"sha256:{'d' * 64}",
        manifest_artifact_hash_ref=f"sha256:{'e' * 64}",
        symbol="XAUUSD",
        timeframe="M5",
        setup_family="spread_pressure",
        source_action_scope="BUY_SELL_DIRECTIONAL",
        source_candidate_id="ssc-v2-" + "f" * 64,
        source_candidate_content_hash=f"sha256:{'0' * 64}",
        source_packet_artifact_hash_ref=f"sha256:{'1' * 64}",
        source_packet_semantic_hash=f"sha256:{'2' * 64}",
        source_report_artifact_hash_ref=f"sha256:{'3' * 64}",
        bound_at="2026-08-18T00:00:00+00:00",
    )
    both = HypothesisTradeableScopeV1(allowed_action_scope="BOTH", **common)
    sell_only = HypothesisTradeableScopeV1(allowed_action_scope="SELL", **common)
    assert both.scope_hash != sell_only.scope_hash


def test_unrecognized_source_action_scope_fails_closed_in_builder(tmp_path: Path) -> None:
    # The BUILDER (not the dataclass) owns the trust policy: an
    # action_scope value other than the one this codebase has ever produced
    # must never be silently mapped to any allowed_action_scope.
    context = _frozen(tmp_path, action_scope="SELL_ONLY_UNRECOGNIZED")
    with pytest.raises(HypothesisTradeableScopeError, match="unrecognized source action_scope"):
        bind_hypothesis_tradeable_scope(
            context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
        )


# ---------------------------------------------------------------------------
# 7: wrong hypothesis/family/content/manifest rejected
# ---------------------------------------------------------------------------


def test_tampered_registry_binding_rejected(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("9" * 64, context.spec.hypothesis_id),
        )
    with pytest.raises(HypothesisTradeableScopeError):
        bind_hypothesis_tradeable_scope(
            context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
        )


def test_unfrozen_hypothesis_has_no_scope() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "registry.db"
        store = ArtifactStore(Path(tmp) / "artifacts")
        registry = HypothesisRegistry(db_path)
        registry.register(
            hypothesis_id="rpi-v1:" + "a" * 64 + ":0",
            family_definition={"candidate_id": "ssc-v2-" + "b" * 64, "claim": "x"},
            content_definition={"proposal": {"falsifiable_claim": "x"}},
        )
        with pytest.raises(HypothesisTradeableScopeError, match="at least FROZEN"):
            bind_hypothesis_tradeable_scope(
                "rpi-v1:" + "a" * 64 + ":0", registry=registry, artifact_store=store
            )


def test_unknown_hypothesis_rejected(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    with pytest.raises(HypothesisTradeableScopeError, match="does not exist"):
        bind_hypothesis_tradeable_scope(
            "rpi-v1:" + "9" * 64 + ":0", registry=context.registry, artifact_store=context.store
        )


# ---------------------------------------------------------------------------
# 8: provenance genuinely bound to the trusted proposal lineage
# ---------------------------------------------------------------------------


def test_scope_provenance_matches_real_proposal_lineage(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    scope = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    spec = context.spec
    assert scope.source_candidate_id == spec.candidate_id
    assert scope.source_packet_artifact_hash_ref == spec.packet_artifact_hash_ref
    assert scope.source_packet_semantic_hash == spec.packet_semantic_hash
    assert scope.hypothesis_id == spec.hypothesis_id
    assert scope.hypothesis_family_id == spec.hypothesis_family_id


# ---------------------------------------------------------------------------
# 9: immutability
# ---------------------------------------------------------------------------


def test_scope_is_frozen_and_cannot_be_mutated(tmp_path: Path) -> None:
    context = _frozen(tmp_path)
    scope = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.symbol = "EURUSD"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 16-19: no live/execution surface anywhere in this module
# ---------------------------------------------------------------------------


def test_module_never_imports_signal_candidate_risk_or_broker() -> None:
    import ast
    import inspect

    import trademind.discovery.hypothesis_tradeable_scope as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {
        "trademind.signal_intelligence",
        "trademind.risk_manager",
        "trademind.mt5_risk_adapter",
        "trademind.signal_to_risk_bridge",
        "requests",
        "urllib",
        "socket",
        "http",
        "MetaTrader5",
    }
    assert not (imported & forbidden), imported & forbidden


def test_module_never_builds_a_trade_plan_or_risk_decision() -> None:
    import inspect

    import trademind.discovery.hypothesis_tradeable_scope as module

    source = inspect.getsource(module)
    for forbidden_call in (
        "TradePlan(",
        "SignalCandidate(",
        "evaluate_risk(",
        "RiskDecision(",
        "order_send(",
        "OrderSend(",
    ):
        assert forbidden_call not in source, forbidden_call


# ---------------------------------------------------------------------------
# SER8 BUY/SELL TRADEABLE SCOPE COMPATIBILITY V1 -- regression tests.
#
# Root cause: scripts/bootstrap_first_real_hypothesis.py sets
# CandidateDefinitionV2.action_scope directly to the SAME literal
# BUY/SELL value SignalCandidate.plan.action already uses (an exact,
# pre-existing one-to-one vocabulary match -- signal_intelligence.
# VALID_ACTIONS), but bind_hypothesis_tradeable_scope's own source-value
# recognition only ever knew "BUY_SELL_DIRECTIONAL" -> BOTH, so it failed
# closed on the genuine "BUY"/"SELL" source values a real bootstrap-frozen
# hypothesis actually carries. The matcher side
# (verify_live_candidate_matches_scope) already enforced BUY-only/SELL-only
# scopes correctly before this fix -- only the builder's recognized source
# vocabulary was the gap.
# ---------------------------------------------------------------------------


def _real_candidate(*, action: str, symbol: str = "XAUUSD", timeframe: str = "M5", setup_family: str = "spread_pressure"):
    from datetime import datetime, timedelta, timezone

    from trademind.signal_intelligence import EntryOrder, SignalCandidate, TradePlan

    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    return SignalCandidate(
        observed_at=now - timedelta(seconds=70),
        created_at=now - timedelta(seconds=60),
        symbol=symbol,
        timeframe=timeframe,
        setup_family=setup_family,
        scenario="buy sell tradeable scope compatibility test",
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


def test_genuine_source_action_scope_buy_binds_only_buy(tmp_path: Path) -> None:
    """Requirement 1: genuine source action_scope BUY binds only BUY."""
    context = _frozen(tmp_path, action_scope="BUY")
    scope = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    assert scope.source_action_scope == "BUY"
    assert scope.allowed_action_scope == AllowedActionScope.BUY.value


def test_genuine_source_action_scope_sell_binds_only_sell(tmp_path: Path) -> None:
    """Requirement 2: genuine source action_scope SELL binds only SELL."""
    context = _frozen(tmp_path, action_scope="SELL", db_name="orchestrator2.db")
    scope = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    assert scope.source_action_scope == "SELL"
    assert scope.allowed_action_scope == AllowedActionScope.SELL.value


def test_buy_candidate_cannot_pass_sell_scope_and_vice_versa(tmp_path: Path) -> None:
    """Requirement 3: BUY candidate cannot pass SELL scope and vice versa --
    exercised end to end through the real, now-fixed builder AND the
    unmodified matcher, never a hand-constructed scope."""
    from trademind.hypothesis_live_candidate_matching import verify_live_candidate_matches_scope

    buy_context = _frozen(tmp_path, action_scope="BUY", db_name="buy.db")
    buy_scope = bind_hypothesis_tradeable_scope(
        buy_context.spec.hypothesis_id, registry=buy_context.registry, artifact_store=buy_context.store
    )
    sell_context = _frozen(tmp_path, action_scope="SELL", db_name="sell.db")
    sell_scope = bind_hypothesis_tradeable_scope(
        sell_context.spec.hypothesis_id, registry=sell_context.registry, artifact_store=sell_context.store
    )

    buy_candidate = _real_candidate(action="BUY")
    sell_candidate = _real_candidate(action="SELL")

    assert verify_live_candidate_matches_scope(buy_scope, buy_candidate) is True
    assert verify_live_candidate_matches_scope(buy_scope, sell_candidate) is False
    assert verify_live_candidate_matches_scope(sell_scope, sell_candidate) is True
    assert verify_live_candidate_matches_scope(sell_scope, buy_candidate) is False


def test_unknown_action_scope_still_fails_closed(tmp_path: Path) -> None:
    """Requirement 4: unknown action scope still fails closed -- the same
    pre-existing invariant, still true after adding BUY/SELL entries."""
    context = _frozen(tmp_path, action_scope="LONG_SHORT_HEDGED", db_name="unknown.db")
    with pytest.raises(HypothesisTradeableScopeError, match="unrecognized source action_scope"):
        bind_hypothesis_tradeable_scope(
            context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
        )


def test_bare_buy_or_sell_never_silently_widens_to_both(tmp_path: Path) -> None:
    """Requirement 6: no broadening/wildcard behavior introduced -- a BUY
    or SELL source scope must never be reported, or usable, as BOTH."""
    buy_context = _frozen(tmp_path, action_scope="BUY", db_name="widen-buy.db")
    buy_scope = bind_hypothesis_tradeable_scope(
        buy_context.spec.hypothesis_id, registry=buy_context.registry, artifact_store=buy_context.store
    )
    sell_context = _frozen(tmp_path, action_scope="SELL", db_name="widen-sell.db")
    sell_scope = bind_hypothesis_tradeable_scope(
        sell_context.spec.hypothesis_id, registry=sell_context.registry, artifact_store=sell_context.store
    )
    assert buy_scope.allowed_action_scope != AllowedActionScope.BOTH.value
    assert sell_scope.allowed_action_scope != AllowedActionScope.BOTH.value
    assert buy_scope.allowed_action_scope == AllowedActionScope.BUY.value
    assert sell_scope.allowed_action_scope == AllowedActionScope.SELL.value
    # BUY_SELL_DIRECTIONAL is still the ONLY source value that ever maps to
    # BOTH -- proven directly against the module's own closed vocabulary.
    from trademind.discovery.hypothesis_tradeable_scope import _SOURCE_ACTION_SCOPE_MAP

    assert _SOURCE_ACTION_SCOPE_MAP == {
        "BUY_SELL_DIRECTIONAL": "BOTH",
        "BUY": "BUY",
        "SELL": "SELL",
    }


def test_current_style_frozen_manifest_requires_no_mutation(tmp_path: Path) -> None:
    """Requirement 5: a hypothesis already frozen the way
    scripts/bootstrap_first_real_hypothesis.py freezes real hypotheses
    (CandidateDefinitionV2.action_scope literally "BUY") becomes usable by
    this code fix ALONE -- calling the builder twice against the SAME,
    completely untouched registry/manifest/CAS content (no re-freeze, no
    registry write, no SQLite edit of any kind) must succeed both times
    with the byte-identical scope_hash."""
    context = _frozen(tmp_path, action_scope="BUY", db_name="resume.db")
    first = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    second = bind_hypothesis_tradeable_scope(
        context.spec.hypothesis_id, registry=context.registry, artifact_store=context.store
    )
    assert first.scope_hash == second.scope_hash
    assert first.allowed_action_scope == AllowedActionScope.BUY.value
