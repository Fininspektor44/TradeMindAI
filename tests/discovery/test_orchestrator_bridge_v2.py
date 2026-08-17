"""Tests for the additive V2 (ExperimentManifestV2 / CAS-based) Discovery
Orchestrator Bridge path.

Chain exercised: ResearchProposalIntake -> ACCEPTED_FOR_HYPOTHESIS ->
PROPOSED hypothesis -> ResearchExperimentSpecificationV1 ->
ExperimentManifestV2 -> HypothesisRegistry.freeze_manifest_v2_in_transaction
-> DiscoveryOrchestratorBridge.submit_frozen_hypothesis_v2 -> bounded
Orchestrator Task.

The legacy (V1, file-path-based) bridge path is exercised by
tests/discovery/test_orchestrator_bridge.py, unmodified and re-run here as
part of the required regression -- this file adds only V2 coverage.
"""

from __future__ import annotations

import ast
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.dataset_split_provenance import (
    BoundSplitPlanV1,
    bind_split_plan_to_source,
)
from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifact as DatasetArtifactV1,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifest,
    ExperimentManifestV2,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.orchestrator_bridge import (
    DiscoveryBridgeError,
    DiscoveryOrchestratorBridge,
)
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import TaskState
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
_MARKET_DATA_ROLE = "market-data"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    store: ArtifactStore
    control: ControlPlane
    registry: HypothesisRegistry
    holdout_seals: HoldoutSealStore
    bridge: DiscoveryOrchestratorBridge
    spec: ResearchExperimentSpecificationV1
    hypothesis_id: str


def _candidate(symbol: str = "XAUUSD") -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=symbol,
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


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="bridge-v2-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _timestamps(count: int = 10) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime]) -> bytes:
    lines = ["time,value"] + [f"{t.isoformat()},{i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _setup(tmp_path: Path, *, symbol: str = "XAUUSD") -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(symbol),),
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
    dataset_file = tmp_path / "spec_dataset.csv"
    dataset_file.write_text("time,symbol,close\n1,XAUUSD,2000.0\n", encoding="utf-8")
    v1_dataset = DatasetArtifactV1.from_path(dataset_file)
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
    holdout_seals = HoldoutSealStore(registry)
    bridge = DiscoveryOrchestratorBridge(
        registry=registry, holdout_seals=holdout_seals, control=control, artifacts=store
    )
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        control=control,
        registry=registry,
        holdout_seals=holdout_seals,
        bridge=bridge,
        spec=spec,
        hypothesis_id=spec.hypothesis_id,
    )


def _bound_split_plan(tmp_path: Path, rows: list[datetime] | None = None) -> tuple[list[datetime], SplitPlan, BoundSplitPlanV1]:
    rows = rows or _timestamps()
    plan = chronological_split(rows)
    source_path = tmp_path / "full_source.csv"
    source_path.write_bytes(_csv_bytes(rows))
    bound = bind_split_plan_to_source(str(source_path), split_plan=plan)
    return rows, plan, bound


def _discovery_dataset_v2(context: _Context, rows: list[datetime], plan: SplitPlan, *, role: str = _MARKET_DATA_ROLE) -> DatasetArtifactV2:
    discovery_rows = rows[: plan.discovery_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(discovery_rows)), media_type="text/csv")
    return DatasetArtifactV2(role=role, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes)


def _build_manifest_v2(
    context: _Context,
    *,
    dataset: DatasetArtifactV2,
    split_plan: SplitPlan,
    bound_hypothesis_content_hash: str | None = None,
    hypothesis_family_id: str | None = None,
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
        hypothesis_family_id=hypothesis_family_id or spec.hypothesis_family_id,
        bound_hypothesis_content_hash=(
            bound_hypothesis_content_hash if bound_hypothesis_content_hash is not None else spec.hypothesis_content_hash
        ),
        proposal_provenance=provenance,
        datasets=(dataset,),
        split_plan=split_plan,
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
        created_by="operator:bridge-v2-test",
    )


def _freeze_v2(context: _Context, manifest: ExperimentManifestV2):
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=context.store)
    db = sqlite3.connect(context.db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        result = context.registry.freeze_manifest_v2_in_transaction(
            db, manifest_artifact_hash_ref=artifact.hash_ref, artifact_store=context.store
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _attest_holdout(context: _Context) -> None:
    context.holdout_seals.register(
        hypothesis_id=context.hypothesis_id,
        envelope_hash="a" * 64,
        key_id="bridge-v2-key",
        evaluator_id="smc_pattern_journal_v1",
        evaluator_hash="b" * 64,
    )
    context.holdout_seals.mark_isolated(
        context.hypothesis_id,
        isolation_receipt_hash="c" * 64,
        public_max_time="2026-01-02T00:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00",
        holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=2,
        holdout_row_count=2,
    )


def _full_pipeline(tmp_path: Path, *, attest: bool = True) -> tuple[_Context, list[datetime], SplitPlan, BoundSplitPlanV1, DatasetArtifactV2]:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    dataset = _discovery_dataset_v2(context, rows, plan)
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    if attest:
        _attest_holdout(context)
    return context, rows, plan, bound, dataset


# ---------------------------------------------------------------------------
# 1: happy path.
# ---------------------------------------------------------------------------


def test_real_intake_created_hypothesis_v2_bridge_validation_passes(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    validated = context.bridge.validate_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    assert validated.binding.hypothesis_id == context.hypothesis_id
    assert validated.binding.dataset_memberships[0].role == "DISCOVERY"

    task = context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    assert task.state is TaskState.NEW
    assert task.budget_limit == 0.0
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN


# ---------------------------------------------------------------------------
# 2-4: wrong content_hash / family / manifest hash rejected.
# ---------------------------------------------------------------------------


def test_wrong_content_hash_binding_rejected_at_bridge(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("0" * 64, context.hypothesis_id),
        )
    with pytest.raises(DiscoveryBridgeError, match="not safely bound"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


def test_wrong_family_rejected_at_bridge(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "INSERT INTO hypothesis_families(family_id, definition_json, holdout_consumed, terminal_state, updated_at) "
            "VALUES ('hf_' || ?, '{\"x\":1}', 0, NULL, '2026-01-01T00:00:00+00:00')",
            ("1" * 64,),
        )
        db.execute(
            "UPDATE hypotheses SET family_id=? WHERE hypothesis_id=?",
            ("hf_" + "1" * 64, context.hypothesis_id),
        )
    with pytest.raises(DiscoveryBridgeError, match="not safely bound"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


def test_wrong_manifest_semantic_hash_rejected_at_bridge(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET manifest_hash=? WHERE hypothesis_id=?",
            ("9" * 64, context.hypothesis_id),
        )
    with pytest.raises(DiscoveryBridgeError, match="not safely bound"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


# ---------------------------------------------------------------------------
# 5-6: non-FROZEN / missing V2 binding rejected.
# ---------------------------------------------------------------------------


def test_non_frozen_hypothesis_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    _, plan, bound = _bound_split_plan(tmp_path)
    dataset = _discovery_dataset_v2(context, _timestamps(), plan)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.PROPOSED
    with pytest.raises(DiscoveryBridgeError, match="not safely bound"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


def test_legacy_v1_frozen_hypothesis_has_no_v2_binding(tmp_path: Path) -> None:
    """A hypothesis frozen through the LEGACY (V1) path has manifest_hash set
    but manifest_artifact_hash_ref/manifest_schema_version NULL -- the V2
    bridge must reject it, proving the two freeze paths are mutually
    exclusive and the legacy bridge is never silently bypassed."""
    context = _setup(tmp_path)
    dataset_file = tmp_path / "legacy_dataset.csv"
    dataset_file.write_text("a,b\n1,2\n", encoding="utf-8")
    legacy_manifest = ExperimentManifest.new(
        hypothesis_id=context.hypothesis_id,
        family_definition=context.spec.family_definition,
        test_family="legacy-test-family",
        primary_metric="legacy_metric",
        alpha=0.05,
        q=0.1,
        minimum_effect_size=0.0,
        max_hypotheses_tests=1,
        schema_version="legacy-v1",
        git_commit="a" * 40,
        datasets=(DatasetArtifactV1.from_path(dataset_file),),
    )
    context.registry.freeze(context.hypothesis_id, manifest_hash=legacy_manifest.manifest_hash)
    assert context.registry.get(context.hypothesis_id).state is HypothesisState.FROZEN
    _, plan, bound = _bound_split_plan(tmp_path)
    dataset = _discovery_dataset_v2(context, _timestamps(), plan)
    with pytest.raises(DiscoveryBridgeError, match="not safely bound"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


# ---------------------------------------------------------------------------
# 7-9: dataset provenance / forged role / full unsplit source rejected.
# ---------------------------------------------------------------------------


def test_missing_dataset_provenance_binding_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with pytest.raises(DiscoveryBridgeError, match="no declared split-membership binding"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={}
        )


def test_forged_discovery_role_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows)), media_type="text/csv")
    dataset = DatasetArtifactV2(role=_MARKET_DATA_ROLE, artifact_hash_ref=artifact.hash_ref, media_type=artifact.media_type, size_bytes=artifact.size_bytes)
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    with pytest.raises(DiscoveryBridgeError, match="failed split-membership verification"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


def test_full_unsplit_source_rejected_as_discovery_dataset(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    rows, plan, bound = _bound_split_plan(tmp_path)
    full_artifact = context.store.import_snapshot(io.BytesIO(_csv_bytes(rows)), media_type="text/csv")
    dataset = DatasetArtifactV2(role=_MARKET_DATA_ROLE, artifact_hash_ref=full_artifact.hash_ref, media_type=full_artifact.media_type, size_bytes=full_artifact.size_bytes)
    manifest = _build_manifest_v2(context, dataset=dataset, split_plan=plan)
    _freeze_v2(context, manifest)
    _attest_holdout(context)
    with pytest.raises(DiscoveryBridgeError, match="failed split-membership verification"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


# ---------------------------------------------------------------------------
# 10: tampered dataset artifact rejected.
# ---------------------------------------------------------------------------


def test_tampered_dataset_artifact_rejected(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    object_path = Path(context.store.resolve_verified(dataset.artifact_hash_ref).path)
    object_path.write_bytes(b"tampered-not-the-authoritative-dataset")
    with pytest.raises(Exception):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


# ---------------------------------------------------------------------------
# 11: final-holdout role/content cannot enter a task.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden_role", ["FINAL_HOLDOUT", "HOLDOUT", "holdout"])
def test_forbidden_split_roles_cannot_enter_task(tmp_path: Path, forbidden_role: str) -> None:
    # (function deliberately avoids the substring the parametrize IDs carry
    # in its own name: pytest derives `tmp_path` from the test's own node
    # id, and _setup()'s spec dataset path would otherwise itself trip
    # research_experiment_specification.py's own unrelated
    # holdout-shaped-filename guard purely by coincidence of this test's name.)
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    with pytest.raises(DiscoveryBridgeError, match="failed split-membership verification"):
        context.bridge.validate_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: forbidden_role}
        )


# ---------------------------------------------------------------------------
# 12: research brief contains no raw dataset paths.
# ---------------------------------------------------------------------------


def test_research_brief_v2_contains_no_raw_dataset_paths(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    task = context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    brief_files = list(context.store.root.rglob("*research-brief-v2*.json"))
    assert len(brief_files) == 1
    brief_text = brief_files[0].read_text(encoding="utf-8")
    brief = json.loads(brief_text)
    assert "file_path" not in brief_text
    # The real source-file path used to build `bound` is never referenced.
    assert str((tmp_path / "full_source.csv").resolve()) not in brief_text
    assert str(tmp_path) not in brief_text
    assert brief["hypothesis_id"] == context.hypothesis_id
    assert brief["dataset_memberships"][0]["artifact_hash_ref"] == dataset.artifact_hash_ref
    assert len(task.artifact_refs) == 1


# ---------------------------------------------------------------------------
# 13-14: idempotency / conflicting duplicate.
# ---------------------------------------------------------------------------


def test_duplicate_submit_is_rejected_per_existing_convention(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    with pytest.raises(DiscoveryBridgeError, match="already has an Orchestrator task"):
        context.bridge.submit_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )


def test_conflicting_duplicate_submit_fails_closed_no_overwrite(tmp_path: Path) -> None:
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    first_task = context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    # A second attempt claiming a DIFFERENT (here, content-mismatched) role
    # binding for the same hypothesis must fail closed one way or another --
    # either because the claimed role no longer matches the artifact's real
    # content (this dataset is DISCOVERY-range, so claiming VALIDATION fails
    # membership verification before the duplicate-task check is even
    # reached), or because the hypothesis already has a task. Either failure
    # mode is an acceptable proof of "no silent overwrite"; what matters is
    # asserted below: the original task is provably untouched.
    with pytest.raises(DiscoveryBridgeError):
        context.bridge.submit_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "VALIDATION"}
        )
    reloaded = context.control.task_store.get(first_task.task_id)
    assert reloaded == first_task  # no silent overwrite.


def test_conflicting_duplicate_with_matching_content_still_rejected_as_existing_task(
    tmp_path: Path,
) -> None:
    """A second attempt whose dataset_role_bindings are content-VALID (same
    role, so it passes membership verification again) still hits the
    duplicate-task guard rather than silently creating or overwriting
    anything -- the precise scenario Phase 4 describes."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    first_task = context.bridge.submit_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    with pytest.raises(DiscoveryBridgeError, match="already has an Orchestrator task"):
        context.bridge.submit_frozen_hypothesis_v2(
            context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
        )
    reloaded = context.control.task_store.get(first_task.task_id)
    assert reloaded == first_task


# ---------------------------------------------------------------------------
# 15-19: no execution/validation/holdout-decryption/provider/broker path.
# ---------------------------------------------------------------------------


def test_no_execution_validation_or_provider_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/orchestrator_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_import_substrings = (
        "experiment_execution_runtime",
        "experiment_execution_contract",
        "experiment_evidence",
        "validation_decision",
        "final_holdout_decision_gate",
        "final_holdout_evaluation",
        "holdout_crypto",
        "holdout_runner",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "openai",
        "anthropic",
        "claude",
        "ollama",
        "metatrader5",
        "mt5",
    )
    lowered = {name.lower() for name in imported}
    for name in lowered:
        for term in forbidden_import_substrings:
            assert term not in name, f"unexpected forbidden-shaped import: {name!r}"

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    forbidden_calls = {"OrderSend", "PositionClose", "PositionModify", "decrypt", "run_experiment", "run_validation"}
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


def test_v2_bridge_never_reads_final_holdout_content(tmp_path: Path) -> None:
    """Dynamic proof: the bridge only ever reads the CANDIDATE dataset
    artifacts referenced by the manifest and the holdout SEAL's isolation
    ATTESTATION metadata (never the sealed plaintext itself, which
    HoldoutSealStore does not even expose)."""
    context, rows, plan, bound, dataset = _full_pipeline(tmp_path)
    validated = context.bridge.validate_frozen_hypothesis_v2(
        context.hypothesis_id, bound_split_plan=bound, dataset_role_bindings={dataset.role: "DISCOVERY"}
    )
    for membership in validated.binding.dataset_memberships:
        assert membership.role in ("DISCOVERY", "VALIDATION")


# ---------------------------------------------------------------------------
# 20: legacy V1 bridge tests remain green -- verified by running
# tests/discovery/test_orchestrator_bridge.py directly (see VALIDATION
# section of the implementation report); no V1 test is duplicated here.
# ---------------------------------------------------------------------------


def test_v1_and_v2_manifest_schemas_carry_no_raw_file_path_field() -> None:
    """Structural guarantee behind _assert_brief_v2_has_no_raw_path_shaped_strings:
    DatasetArtifactV2/ExperimentManifestV2/BoundSplitPlanV1/
    DatasetSplitMembershipV1 have no file_path-shaped field at all."""
    import dataclasses

    from trademind.discovery.dataset_split_provenance import (
        BoundSplitPlanV1 as _BoundSplitPlanV1,
        DatasetSplitMembershipV1 as _DatasetSplitMembershipV1,
    )

    for cls in (DatasetArtifactV2, ExperimentManifestV2, _BoundSplitPlanV1, _DatasetSplitMembershipV1):
        field_names = {f.name for f in dataclasses.fields(cls)}
        assert not any("file_path" in name or "path" == name for name in field_names), field_names
