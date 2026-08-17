from __future__ import annotations

import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

import trademind.experiment_manifest_creation as creation_module
from trademind.discovery.hypothesis_registry import (
    HypothesisRecord,
    HypothesisRegistry,
    HypothesisState,
)
from trademind.discovery.manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    TradingFrictionV1,
)
from trademind.discovery.split_engine import SplitPlan, chronological_split
from trademind.experiment_manifest_creation import (
    ExperimentManifestCreationConflict,
    ExperimentManifestCreationControl,
    ExperimentManifestCreationSourceError,
    TrustedExperimentSpecificationV1,
)
from trademind.orchestrator.artifact_store import ArtifactRef, ArtifactStore
from trademind.orchestrator.audit_log import AuditLog
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.research_execution import ResearchExecutionControl
from trademind.research_proposal_intake import (
    ResearchProposalIntakeControl,
    ResearchProposalIntakeV1,
)
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
_CREATION_ACTION = "EXPERIMENT_MANIFEST_V1_CREATED_AND_HYPOTHESIS_FROZEN"


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    store: ArtifactStore
    control_plane: ControlPlane
    budget: BudgetManager
    execution_control: ResearchExecutionControl
    registry: HypothesisRegistry
    intake_control: ResearchProposalIntakeControl
    creation_control: ExperimentManifestCreationControl
    task_id: str
    request_hash: str
    intake: ResearchProposalIntakeV1
    hypothesis: HypothesisRecord | None


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


def _budget(path: Path) -> BudgetManager:
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
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": packet.candidate_bindings[0]["candidate_id"],
                    "title": "Predeclared regime continuation",
                    "rationale": "The candidate may depend on a public-data regime.",
                    "falsifiable_claim": "The effect remains positive in the declared regime.",
                    "proposed_test": "Compare predefined chronological public-data subsets.",
                    "rejection_condition": "Reject if the declared effect is non-positive.",
                    "confidence": "LOW",
                }
            ],
        }
    )


def _setup(tmp_path: Path, *, accept: bool = True) -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control_plane = ControlPlane(db_path)
    budget = _budget(db_path)
    report = build_report_v2(
        (_candidate(),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="manifest-creation-test",
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
        packet_ref.hash_ref,
        control_plane=control_plane,
        artifact_store=store,
    )
    execution_control = ResearchExecutionControl(
        control_plane=control_plane,
        budget_manager=budget,
        artifact_store=store,
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
        execution_control=execution_control,
        hypothesis_registry=registry,
    )
    pending = intake_control.ingest_succeeded_research_execution_v1(execution.request_hash)[0]
    hypothesis = None
    intake = pending
    if accept:
        intake, hypothesis = intake_control.accept_for_hypothesis(
            pending.intake_id,
            reviewer_id="operator:reviewer",
        )
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        store=store,
        control_plane=control_plane,
        budget=budget,
        execution_control=execution_control,
        registry=registry,
        intake_control=intake_control,
        creation_control=ExperimentManifestCreationControl(
            intake_control=intake_control,
            artifact_store=store,
        ),
        task_id=task.task_id,
        request_hash=execution.request_hash,
        intake=intake,
        hypothesis=hypothesis,
    )


def _dataset(store: ArtifactStore, payload: bytes = b"timestamp,value\n2026-01-01,1\n"):
    artifact = store.import_snapshot(io.BytesIO(payload), media_type="text/csv")
    return DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=artifact.hash_ref,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
    )


def _split(*, offset: int = 0) -> SplitPlan:
    start = datetime(2026, 1, 1, offset, tzinfo=timezone.utc)
    return chronological_split([start + timedelta(hours=index) for index in range(12)])


def _criteria() -> EvaluationCriteriaV1:
    return EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(
            EvaluationCriterionV1(
                metric="mean_net_r",
                operator=CriterionOperator.GREATER_THAN_OR_EQUAL,
                threshold=0.05,
            ),
        ),
    )


def _friction() -> TradingFrictionV1:
    return TradingFrictionV1(
        model_id="fixed-bps-v1",
        unit="bps-per-round-trip",
        spread=1.0,
        commission=0.5,
        slippage=0.5,
        fees=0.2,
    )


def _specification(
    store: ArtifactStore,
    *,
    dataset: DatasetArtifactV2 | None = None,
    split_plan: SplitPlan | None = None,
    alpha: float = 0.05,
    created_at: str = "2026-08-14T13:00:00+00:00",
    created_by: str = "operator:manifest",
    parameters: dict[str, object] | None = None,
) -> TrustedExperimentSpecificationV1:
    return TrustedExperimentSpecificationV1(
        datasets=(dataset or _dataset(store),),
        split_plan=split_plan or _split(),
        split_dataset_role="market-data",
        test_family="chronological-signal-evaluation-v1",
        primary_metric="mean_net_r",
        evaluation_criteria=_criteria(),
        alpha=alpha,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        trading_friction=_friction(),
        deterministic_seed=7,
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="manifest-creation-v1",
            git_commit="f" * 40,
            revision_source="git_worktree",
        ),
        semantic_parameters=parameters or {"horizon": 12, "target": "forward_net_r"},
        created_at=created_at,
        created_by=created_by,
    )


def _audit_payloads(path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(path) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM audit_events ORDER BY id").fetchall()
        ]


def _creation_audits(path: Path) -> list[dict[str, object]]:
    return [payload for payload in _audit_payloads(path) if payload["action"] == _CREATION_ACTION]


def _usage_count(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]


def _persisted_hypothesis_state(path: Path, hypothesis_id: str) -> str:
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT state FROM hypotheses WHERE hypothesis_id=?",
            (hypothesis_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def _source_snapshot(context: _Context) -> tuple[object, object, object, object, int]:
    return (
        context.control_plane.task_store.get(context.task_id),
        context.execution_control.get_execution(context.request_hash),
        context.intake_control.get(context.intake.intake_id),
        context.budget.get_reservation(context.request_hash),
        _usage_count(context.db_path),
    )


def test_creation_uses_authoritative_lineage_and_only_freezes_manifest(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    before = _source_snapshot(context)
    specification = _specification(context.store)

    result = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=specification,
    )

    assert result.created is True
    assert result.hypothesis.state is HypothesisState.FROZEN
    assert result.manifest.hypothesis_id == context.intake.hypothesis_id
    assert result.manifest.hypothesis_family_id == context.hypothesis.hypothesis_family_id
    assert result.manifest.bound_hypothesis_content_hash == context.hypothesis.content_hash
    assert result.manifest.proposal_provenance.candidate_id == context.intake.candidate_id
    assert result.manifest.proposal_provenance.packet_artifact_hash_ref == (
        context.intake.packet_artifact_hash_ref
    )
    assert result.manifest.proposal_provenance.result_artifact_hash_ref == (
        context.intake.result_artifact_hash_ref
    )
    assert result.manifest.split_plan == specification.split_plan
    assert result.manifest.split_plan.holdout_count > 0
    assert result.hypothesis.manifest_hash == result.manifest.manifest_semantic_hash[7:]
    assert result.hypothesis.manifest_artifact_hash_ref == result.manifest_artifact_hash_ref
    assert (
        context.registry.load_bound_manifest_v2(
            context.intake.intake_id,
            artifact_store=context.store,
        )
        == result.manifest
    )
    assert _source_snapshot(context) == before
    task = context.control_plane.task_store.get(context.task_id)
    assert task.state.value == "NEW"
    assert task.assigned_role.value == "OPERATOR"
    assert task.allowed_tools == ()
    assert task.budget_limit == 0

    audits = _creation_audits(context.db_path)
    assert len(audits) == 1
    assert audits[0]["from_state"] == audits[0]["to_state"] == "NEW"
    assert audits[0]["metadata"]["scientifically_validated"] is False
    assert audits[0]["metadata"]["experiment_executed"] is False
    assert audits[0]["metadata"]["creation_status"] == "REQUESTED_AND_COMPLETED"
    assert audits[0]["metadata"]["manifest_artifact_hash_ref"] == (
        result.manifest_artifact_hash_ref
    )


def test_only_accepted_authoritative_intake_can_create_manifest(tmp_path: Path) -> None:
    context = _setup(tmp_path, accept=False)
    before = _source_snapshot(context)

    with pytest.raises(ExperimentManifestCreationSourceError, match="ACCEPTED"):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=_specification(context.store),
        )

    assert _source_snapshot(context) == before
    assert _creation_audits(context.db_path) == []


def test_trusted_input_cannot_substitute_authoritative_source_fields(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    names = {item.name for item in fields(TrustedExperimentSpecificationV1)}
    assert names.isdisjoint(
        {
            "hypothesis_id",
            "hypothesis_family_id",
            "hypothesis_content_hash",
            "candidate_id",
            "packet_artifact_hash_ref",
            "result_artifact_hash_ref",
            "execution_request_hash",
        }
    )
    with pytest.raises(TypeError, match="exact TrustedExperimentSpecificationV1"):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification={"alpha": 0.05},
        )


def test_trusted_specification_is_deeply_immutable_and_result_free(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    parameters = {"nested": {"windows": [12, 24]}}
    specification = _specification(context.store, parameters=parameters)
    parameters["nested"]["windows"].append(48)
    assert specification.semantic_parameters["nested"]["windows"] == (12, 24)

    for key in (
        "observed_ev",
        "observed_sharpe",
        "observed_win_rate",
        "observed_p_value",
        "observed_drawdown",
        "experiment_result",
        "holdout_metrics",
    ):
        with pytest.raises(ValueError, match="observed-result"):
            _specification(context.store, parameters={"nested": {key: 1.0}})
    with pytest.raises(ValueError, match="observed-result"):
        _specification(context.store, parameters={"metric": "observed_sharpe"})
    with pytest.raises(ValueError, match="exact finite float"):
        _specification(context.store, alpha=True)
    with pytest.raises(ValueError, match="exact finite float"):
        _specification(context.store, alpha=float("nan"))


def test_missing_or_corrupt_dataset_fails_before_registry_mutation(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    before = _source_snapshot(context)
    missing = DatasetArtifactV2(
        role="market-data",
        artifact_hash_ref=f"sha256:{'9' * 64}",
        media_type="text/csv",
        size_bytes=10,
    )
    with pytest.raises(Exception):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=_specification(context.store, dataset=missing),
        )
    assert _persisted_hypothesis_state(context.db_path, context.intake.intake_id) == "PROPOSED"
    assert _source_snapshot(context) == before
    assert _creation_audits(context.db_path) == []


def test_exact_and_diagnostic_only_retries_use_first_writer_without_duplicate_audit(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    first_specification = _specification(context.store)
    first = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=first_specification,
    )
    exact_retry = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=first_specification,
    )
    diagnostic_retry = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=replace(
            first_specification,
            created_at="2026-08-15T13:00:00+00:00",
            created_by="operator:other",
        ),
    )

    assert first.created is True
    assert exact_retry.created is diagnostic_retry.created is False
    assert exact_retry.manifest_artifact_hash_ref == first.manifest_artifact_hash_ref
    assert diagnostic_retry.manifest_artifact_hash_ref == first.manifest_artifact_hash_ref
    assert diagnostic_retry.manifest == first.manifest
    assert len(_creation_audits(context.db_path)) == 1


def test_conflicting_scientific_manifest_cannot_replace_frozen_binding(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    first = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=_specification(context.store),
    )
    with pytest.raises(ExperimentManifestCreationConflict):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=_specification(context.store, alpha=0.01),
        )
    frozen = context.registry.get(context.intake.intake_id)
    assert frozen.manifest_hash == first.manifest.manifest_semantic_hash[7:]
    assert frozen.manifest_artifact_hash_ref == first.manifest_artifact_hash_ref
    assert len(_creation_audits(context.db_path)) == 1


def test_sixteen_concurrent_identical_creations_make_one_binding_and_audit(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    specification = _specification(context.store)
    barrier = Barrier(16)

    def create(_index: int):
        barrier.wait()
        return context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=specification,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(create, range(16)))

    assert sum(result.created for result in results) == 1
    assert len({result.manifest_artifact_hash_ref for result in results}) == 1
    assert len({result.manifest.manifest_semantic_hash for result in results}) == 1
    assert context.registry.get(context.intake.intake_id).state is HypothesisState.FROZEN
    assert len(_creation_audits(context.db_path)) == 1


def test_two_conflicting_concurrent_creations_have_exactly_one_authoritative_winner(
    tmp_path: Path,
) -> None:
    context = _setup(tmp_path)
    specifications = (_specification(context.store), _specification(context.store, alpha=0.01))
    barrier = Barrier(2)

    def create(specification: TrustedExperimentSpecificationV1):
        barrier.wait()
        try:
            return context.creation_control.create_experiment_manifest_v1(
                context.intake.intake_id,
                specification=specification,
            )
        except ExperimentManifestCreationConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, specifications))

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == len(failures) == 1
    winner = successes[0]
    assert context.registry.get(context.intake.intake_id).manifest_artifact_hash_ref == (
        winner.manifest_artifact_hash_ref
    )
    assert len(_creation_audits(context.db_path)) == 1
    repeated = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=specifications[outcomes.index(winner)],
    )
    assert repeated.created is False
    assert repeated.manifest_artifact_hash_ref == winner.manifest_artifact_hash_ref


def test_restart_reverifies_intake_registry_manifest_dataset_and_split(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    created = context.creation_control.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=_specification(context.store),
    )

    restarted_store = ArtifactStore(context.artifact_root)
    restarted_execution = ResearchExecutionControl(
        control_plane=ControlPlane(context.db_path),
        budget_manager=_budget(context.db_path),
        artifact_store=restarted_store,
    )
    restarted_registry = HypothesisRegistry(context.db_path)
    restarted_intake = ResearchProposalIntakeControl(
        execution_control=restarted_execution,
        hypothesis_registry=restarted_registry,
    )
    restarted_creation = ExperimentManifestCreationControl(
        intake_control=restarted_intake,
        artifact_store=restarted_store,
    )

    assert restarted_intake.reverify(context.intake.intake_id) == context.intake
    loaded = restarted_registry.load_bound_manifest_v2(
        context.intake.intake_id,
        artifact_store=restarted_store,
    )
    assert loaded == created.manifest
    assert loaded.split_plan.semantic_hash == created.manifest.split_plan.semantic_hash
    assert loaded.datasets == created.manifest.datasets
    retried = restarted_creation.create_experiment_manifest_v1(
        context.intake.intake_id,
        specification=_specification(restarted_store),
    )
    assert retried.created is False
    assert retried.manifest_artifact_hash_ref == created.manifest_artifact_hash_ref


@pytest.mark.parametrize(
    "stage",
    ("intake", "hypothesis", "build", "persist", "begin", "freeze", "audit", "commit"),
)
def test_failure_injection_never_commits_freeze_or_half_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    context = _setup(tmp_path)
    before = _source_snapshot(context)
    specification = _specification(context.store)

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"injected {stage} failure")

    if stage == "intake":
        monkeypatch.setattr(ResearchProposalIntakeControl, "_load_verified_intake", fail)
    elif stage == "hypothesis":
        monkeypatch.setattr(
            ResearchProposalIntakeControl,
            "_accepted_hypothesis_in_transaction",
            fail,
        )
    elif stage == "build":
        monkeypatch.setattr(creation_module, "build_experiment_manifest_v2", fail)
    elif stage == "persist":
        monkeypatch.setattr(creation_module, "persist_experiment_manifest_v2", fail)
    elif stage == "begin":
        monkeypatch.setattr(ExperimentManifestCreationControl, "_begin_transaction", fail)
    elif stage == "freeze":
        monkeypatch.setattr(HypothesisRegistry, "freeze_manifest_v2_in_transaction", fail)
    elif stage == "audit":
        monkeypatch.setattr(AuditLog, "append_in_transaction", fail)
    elif stage == "commit":
        monkeypatch.setattr(ExperimentManifestCreationControl, "_commit_transaction", fail)

    with pytest.raises(RuntimeError, match=f"injected {stage} failure"):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=specification,
        )

    assert _persisted_hypothesis_state(context.db_path, context.intake.intake_id) == "PROPOSED"
    assert _creation_audits(context.db_path) == []
    assert _source_snapshot(context) == before


def test_corrupt_manifest_after_cas_persistence_cannot_freeze_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup(tmp_path)
    real_persist = creation_module.persist_experiment_manifest_v2

    def persist_then_corrupt(*args, **kwargs) -> ArtifactRef:
        artifact = real_persist(*args, **kwargs)
        object_path, _metadata_path = context.store._cas_paths(artifact.hash_ref[7:])
        object_path.write_bytes(b"corrupt")
        return artifact

    monkeypatch.setattr(creation_module, "persist_experiment_manifest_v2", persist_then_corrupt)
    with pytest.raises(Exception):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=_specification(context.store),
        )
    assert context.registry.get(context.intake.intake_id).state is HypothesisState.PROPOSED
    assert _creation_audits(context.db_path) == []


def test_wrong_persisted_hypothesis_identity_fails_before_manifest_cas(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET content_hash=? WHERE hypothesis_id=?",
            ("9" * 64, context.intake.intake_id),
        )
    with pytest.raises(ExperimentManifestCreationSourceError, match="authoritative verification"):
        context.creation_control.create_experiment_manifest_v1(
            context.intake.intake_id,
            specification=_specification(context.store),
        )
    assert context.registry.get(context.intake.intake_id).state is HypothesisState.PROPOSED
    assert _creation_audits(context.db_path) == []


def test_artifact_store_must_be_authoritative_execution_store(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with pytest.raises(ExperimentManifestCreationSourceError, match="share"):
        ExperimentManifestCreationControl(
            intake_control=context.intake_control,
            artifact_store=ArtifactStore(tmp_path / "substitute-artifacts"),
        )
