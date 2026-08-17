"""End-to-end proof that the ported ExperimentManifestV2 contract closes the
content-identity / family-identity gap between Research Proposal Intake V1
and the Discovery Engine manifest/freeze chain, for a REAL intake-created
hypothesis (not a synthetic ``registry.register(...)`` fixture).

Chain exercised:
  ResearchProposalIntake -> ACCEPTED_FOR_HYPOTHESIS -> PROPOSED hypothesis ->
  ResearchExperimentSpecificationV1 -> ExperimentManifestV2 -> verify manifest ->
  registry freeze (freeze_manifest_v2_in_transaction) -> bound-manifest
  reverification (HypothesisRegistry.load_bound_manifest_v2), which performs
  the same content_hash / hypothesis_family_id / manifest_semantic_hash
  identity checks a bridge-level V2 validator would need.

DiscoveryOrchestratorBridge itself is NOT modified or exercised here: it has
no V2-aware method on the sibling branch this contract was ported from, and
none is invented in this task (see the task's own scope limits). The
identity checks a bridge validator would perform are already fully
performed by HypothesisRegistry.load_bound_manifest_v2, which this test
uses as the bridge-equivalent validation step.

No final holdout is read, sealed, isolated, or consumed anywhere in this
file -- confirmed by the absence of any holdout_store/holdout_sealer/
holdout_crypto/orchestrator_bridge import.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import (
    HypothesisRegistry,
    HypothesisState,
    ManifestV2FreezeResult,
    RegistryError,
)
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifestV2,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.split_engine import SplitPlan, chronological_split
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

from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1  # noqa: E402  (V1, unused directly -- import proves it is untouched)

_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    store: ArtifactStore
    registry: HypothesisRegistry
    intake_control: ResearchProposalIntakeControl
    spec_control: ResearchExperimentSpecificationControl
    spec: ResearchExperimentSpecificationV1
    hypothesis_content_hash: str
    hypothesis_family_id: str


def _candidate() -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol="XAUUSD",
            timeframe="M5",
            feature="spread_pressure",
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


def _setup(tmp_path: Path) -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="manifest-v2-reconciliation-test",
            git_commit="1" * 40,
            revision_source="git_worktree",
        ),
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
    accepted, hypothesis = intake_control.accept_for_hypothesis(
        pending.intake_id, reviewer_id="operator:reviewer"
    )
    assert hypothesis.state is HypothesisState.PROPOSED

    dataset_file = tmp_path / "dataset.csv"
    dataset_file.write_text("time,symbol,close\n1,XAUUSD,2000.0\n", encoding="utf-8")
    v1_dataset = DatasetArtifactV1.from_path(dataset_file)  # unrelated V1 artifact, proves V1 untouched.

    spec = spec_control.create_specification(
        accepted.intake_id,
        reviewer_id="operator:spec-reviewer",
        test_family="smc_pattern_journal_v1",
        primary_metric="avg_net_atr",
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        datasets=(v1_dataset,),
        parameters={"horizon": 12},
    )
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        registry=registry,
        intake_control=intake_control,
        spec_control=spec_control,
        spec=spec,
        hypothesis_content_hash=hypothesis.content_hash,
        hypothesis_family_id=hypothesis.hypothesis_family_id,
    )


def _v2_dataset(store: ArtifactStore, payload: bytes = b"time,symbol,close\n1,XAUUSD,2000.0\n") -> DatasetArtifactV2:
    artifact = store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    return DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )


def _split() -> SplitPlan:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return chronological_split([start + timedelta(hours=index) for index in range(12)])


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="manifest-v2-reconciliation-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _build_manifest_v2_from_specification(
    context: _Context,
    *,
    bound_hypothesis_content_hash: str | None = None,
    hypothesis_family_id: str | None = None,
    dataset: DatasetArtifactV2 | None = None,
) -> ExperimentManifestV2:
    """The additive adapter step: map a ResearchExperimentSpecificationV1's
    ALREADY-CAPTURED fields onto ExperimentManifestV2's constructor. No
    change to research_experiment_specification.py was required -- every
    field ExperimentManifestV2.proposal_provenance needs is already a public
    field on the specification."""
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
    bound_dataset = dataset or _v2_dataset(context.store)
    return build_experiment_manifest_v2(
        artifact_store=context.store,
        hypothesis_id=spec.hypothesis_id,
        hypothesis_family_id=hypothesis_family_id or spec.hypothesis_family_id,
        bound_hypothesis_content_hash=(
            bound_hypothesis_content_hash
            if bound_hypothesis_content_hash is not None
            else spec.hypothesis_content_hash
        ),
        proposal_provenance=provenance,
        datasets=(bound_dataset,),
        split_plan=_split(),
        split_dataset_role="market-data",
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
        created_by="operator:manifest-v2-reconciliation-test",
    )


def _freeze_in_transaction(context: _Context, manifest_artifact_hash_ref: str) -> ManifestV2FreezeResult:
    db = sqlite3.connect(context.db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        result = context.registry.freeze_manifest_v2_in_transaction(
            db,
            manifest_artifact_hash_ref=manifest_artifact_hash_ref,
            artifact_store=context.store,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HARD RULE: manifest content_hash == HypothesisRegistry.content_hash,
# manifest hypothesis_family_id == HypothesisRegistry.hypothesis_family_id,
# proven for a REAL Intake-created hypothesis.
# ---------------------------------------------------------------------------


def test_v2_manifest_binds_exact_intake_created_hypothesis_identity(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    manifest = _build_manifest_v2_from_specification(context)

    # HARD RULE, proven directly on the constructed manifest before any freeze:
    assert manifest.bound_hypothesis_content_hash == context.hypothesis_content_hash
    assert manifest.hypothesis_family_id == context.hypothesis_family_id
    assert manifest.hypothesis_id == context.spec.hypothesis_id

    artifact = persist_experiment_manifest_v2(manifest, artifact_store=context.store)

    freeze_result = _freeze_in_transaction(context, artifact.hash_ref)
    assert freeze_result.created is True
    assert freeze_result.record.state is HypothesisState.FROZEN
    assert freeze_result.record.manifest_artifact_hash_ref == artifact.hash_ref

    after = context.registry.get(context.spec.hypothesis_id)
    assert after.state is HypothesisState.FROZEN
    assert after.content_hash == context.hypothesis_content_hash  # content_hash is IMMUTABLE across freeze.
    assert after.hypothesis_family_id == context.hypothesis_family_id

    # Bridge-equivalent validation: re-derives and re-checks content_hash,
    # hypothesis_family_id, and manifest_semantic_hash against the live
    # registry row, exactly the identity checks a bridge validator needs.
    bound = context.registry.load_bound_manifest_v2(
        context.spec.hypothesis_id, artifact_store=context.store
    )
    assert bound.bound_hypothesis_content_hash == after.content_hash
    assert bound.hypothesis_family_id == after.hypothesis_family_id
    assert bound.manifest_semantic_hash.removeprefix("sha256:") == after.manifest_hash


def test_v2_manifest_rejects_wrong_content_hash_binding(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    tampered = _build_manifest_v2_from_specification(
        context, bound_hypothesis_content_hash="0" * 64
    )
    artifact = persist_experiment_manifest_v2(tampered, artifact_store=context.store)

    with pytest.raises(RegistryError, match="content identity"):
        _freeze_in_transaction(context, artifact.hash_ref)
    after = context.registry.get(context.spec.hypothesis_id)
    assert after.state is HypothesisState.PROPOSED
    assert after.manifest_hash is None


def test_v2_manifest_rejects_wrong_family_binding(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    tampered = _build_manifest_v2_from_specification(
        context, hypothesis_family_id="hf_" + "1" * 64
    )
    artifact = persist_experiment_manifest_v2(tampered, artifact_store=context.store)

    with pytest.raises(RegistryError, match="family identity"):
        _freeze_in_transaction(context, artifact.hash_ref)
    after = context.registry.get(context.spec.hypothesis_id)
    assert after.state is HypothesisState.PROPOSED


def test_v2_freeze_is_idempotent_for_the_same_manifest(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    manifest = _build_manifest_v2_from_specification(context)
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=context.store)

    first = _freeze_in_transaction(context, artifact.hash_ref)
    second = _freeze_in_transaction(context, artifact.hash_ref)
    assert first.created is True
    assert second.created is False
    assert first.record == second.record


def test_v2_freeze_rejects_a_conflicting_second_manifest(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    first_manifest = _build_manifest_v2_from_specification(context)
    first_artifact = persist_experiment_manifest_v2(first_manifest, artifact_store=context.store)
    _freeze_in_transaction(context, first_artifact.hash_ref)

    second_manifest = _build_manifest_v2_from_specification(
        context, dataset=_v2_dataset(context.store, payload=b"different,payload\n1,2\n")
    )
    second_artifact = persist_experiment_manifest_v2(second_manifest, artifact_store=context.store)
    assert second_artifact.hash_ref != first_artifact.hash_ref

    with pytest.raises(RegistryError, match="conflicting manifest"):
        _freeze_in_transaction(context, second_artifact.hash_ref)


def test_v2_hypothesis_stays_proposed_before_freeze(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    _build_manifest_v2_from_specification(context)  # building alone must not freeze anything.
    still = context.registry.get(context.spec.hypothesis_id)
    assert still.state is HypothesisState.PROPOSED
    assert still.manifest_hash is None


def test_no_final_holdout_module_imported_by_this_test_file() -> None:
    import ast

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    # Import-level check: no discovery holdout-sealing/quarantine module, and
    # no *discovery* orchestrator_bridge (trademind.signal_statistics_
    # orchestrator_bridge is an unrelated, non-holdout-gated module and is
    # legitimately imported above for Packet->Task registration).
    forbidden_imports = ("discovery.holdout_store", "discovery.holdout_sealer", "discovery.holdout_crypto")
    for name in imported:
        for term in forbidden_imports:
            assert term not in name, f"unexpected holdout-shaped import: {name!r}"
    assert "trademind.discovery.orchestrator_bridge" not in imported
    assert "DiscoveryOrchestratorBridge" not in imported
    # Call-level check: no holdout sealing/isolation call anywhere in this file.
    forbidden_calls = {"HoldoutSealStore", "mark_isolated", "FinalHoldoutSealer", "seal_file", "seal_and_quarantine"}
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
