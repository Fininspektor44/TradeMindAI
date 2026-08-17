from __future__ import annotations

import ast
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import DatasetArtifact, ExperimentManifest
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.budget import BudgetManager
from trademind.orchestrator.control_plane import ControlPlane
from trademind.research_execution import ResearchExecutionControl, ResearchExecutionRecordV1
from trademind.research_experiment_specification import (
    MAX_DATASETS,
    ResearchExperimentSpecificationControl,
    ResearchExperimentSpecificationConflict,
    ResearchExperimentSpecificationDatabaseError,
    ResearchExperimentSpecificationSourceError,
    ResearchExperimentSpecificationV1,
)
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

_SPEC_KWARGS = {
    "reviewer_id": "operator:spec-reviewer",
    "test_family": "smc_pattern_journal_v1",
    "primary_metric": "avg_net_atr",
    "alpha": 0.05,
    "q": 0.10,
    "minimum_effect_size": 0.0,
    "max_hypotheses_tests": 1,
    "parameters": {"horizon": 12},
}


@dataclass(frozen=True, slots=True)
class _Context:
    db_path: Path
    artifact_root: Path
    control: ControlPlane
    budget: BudgetManager
    execution_control: ResearchExecutionControl
    registry: HypothesisRegistry
    intake_control: ResearchProposalIntakeControl
    spec_control: ResearchExperimentSpecificationControl
    task: object
    packet: SignalStatisticsPacketV2
    execution: ResearchExecutionRecordV1
    accepted: ResearchProposalIntakeV1
    hypothesis: object
    dataset: DatasetArtifact


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


def _dataset(tmp_path: Path, *, name: str = "dataset.csv", content: str = "time,symbol,close\n1,XAUUSD,2000.0\n") -> DatasetArtifact:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return DatasetArtifact.from_path(path)


def _setup(tmp_path: Path, *, symbol: str = "XAUUSD", accept: bool = True) -> _Context:
    db_path = tmp_path / "orchestrator.db"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    control = ControlPlane(db_path)
    budget = _manager(db_path)
    report = build_report_v2(
        (_candidate(symbol),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="spec-test",
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
    accepted = pending
    hypothesis = None
    if accept:
        accepted, hypothesis = intake_control.accept_for_hypothesis(
            pending.intake_id, reviewer_id="operator:reviewer"
        )
    dataset = _dataset(tmp_path)
    return _Context(
        db_path=db_path,
        artifact_root=artifact_root,
        control=control,
        budget=budget,
        execution_control=execution_control,
        registry=registry,
        intake_control=intake_control,
        spec_control=spec_control,
        task=task,
        packet=packet,
        execution=execution,
        accepted=accepted,
        hypothesis=hypothesis,
        dataset=dataset,
    )


def _create(context: _Context, **overrides) -> ResearchExperimentSpecificationV1:
    kwargs = dict(_SPEC_KWARGS)
    kwargs.update(overrides)
    datasets = kwargs.pop("datasets", (context.dataset,))
    return context.spec_control.create_specification(
        context.accepted.intake_id, datasets=datasets, **kwargs
    )


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "research_proposal_intakes",
                "hypotheses",
                "hypothesis_families",
                "research_experiment_specifications",
                "audit_events",
            )
        }


def _restart(context: _Context) -> ResearchExperimentSpecificationControl:
    execution_control = ResearchExecutionControl(
        control_plane=ControlPlane(context.db_path),
        budget_manager=_manager(context.db_path),
        artifact_store=ArtifactStore(context.artifact_root),
    )
    registry = HypothesisRegistry(context.db_path)
    intake_control = ResearchProposalIntakeControl(
        execution_control=execution_control, hypothesis_registry=registry
    )
    return ResearchExperimentSpecificationControl(
        intake_control=intake_control, hypothesis_registry=registry
    )


# ---------------------------------------------------------------------------
# 1-9: upstream preconditions.
# ---------------------------------------------------------------------------


def test_accepted_intake_can_create_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    assert spec.hypothesis_id == context.accepted.hypothesis_id
    assert spec.intake_id == context.accepted.intake_id


def test_pending_review_intake_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path, accept=False)
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="ACCEPTED_FOR_HYPOTHESIS"):
        _create(context)


def test_rejected_intake_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path, accept=False)
    from trademind.research_proposal_intake import ResearchProposalRejectionReason

    context.intake_control.reject(
        context.accepted.intake_id,
        reviewer_id="operator:reviewer",
        reason_codes=(ResearchProposalRejectionReason.OUT_OF_SCOPE,),
    )
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="ACCEPTED_FOR_HYPOTHESIS"):
        _create(context)


def test_missing_intake_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    bogus_intake_id = "rpi-v1:" + context.accepted.result_artifact_hash_ref + ":7"
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="does not exist"):
        context.spec_control.create_specification(
            bogus_intake_id, datasets=(context.dataset,), **_SPEC_KWARGS
        )


def test_missing_hypothesis_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:  # foreign_keys off on this raw connection
        db.execute("DELETE FROM hypotheses WHERE hypothesis_id=?", (context.hypothesis.hypothesis_id,))
    with pytest.raises(ResearchExperimentSpecificationSourceError):
        _create(context)


def test_wrong_hypothesis_binding_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    other = _setup(tmp_path / "other", symbol="EURUSD")
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE research_proposal_intakes SET hypothesis_id=? WHERE intake_id=?",
            (other.hypothesis.hypothesis_id, context.accepted.intake_id),
        )
    with pytest.raises(ResearchExperimentSpecificationSourceError):
        _create(context)


def test_non_proposed_hypothesis_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    context.registry.freeze(context.hypothesis.hypothesis_id, manifest_hash="a" * 64)
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="PROPOSED"):
        _create(context)


def test_hypothesis_with_manifest_hash_already_set_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypotheses SET manifest_hash=? WHERE hypothesis_id=?",
            ("b" * 64, context.hypothesis.hypothesis_id),
        )
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="manifest_hash"):
        _create(context)


def test_terminal_family_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypothesis_families SET terminal_state=? WHERE family_id=?",
            ("REJECTED_FINAL", context.hypothesis.hypothesis_family_id),
        )
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="terminal"):
        _create(context)


def test_holdout_consumed_family_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE hypothesis_families SET holdout_consumed=1 WHERE family_id=?",
            (context.hypothesis.hypothesis_family_id,),
        )
    with pytest.raises(ResearchExperimentSpecificationSourceError, match="holdout"):
        _create(context)


# ---------------------------------------------------------------------------
# 10-12: deterministic identity / idempotency / conflict.
# ---------------------------------------------------------------------------


def test_specification_identity_is_deterministic(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    first = _create(context)
    second = context.spec_control.get_for_hypothesis(context.hypothesis.hypothesis_id)
    assert second == first
    assert second.specification_id == first.specification_id


def test_exact_duplicate_is_idempotent(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    first = _create(context)
    second = _create(context)
    assert first == second
    assert _counts(context.db_path)["research_experiment_specifications"] == 1


def test_conflicting_duplicate_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    _create(context)
    with pytest.raises(ResearchExperimentSpecificationConflict):
        _create(context, alpha=0.10)
    assert _counts(context.db_path)["research_experiment_specifications"] == 1


def test_two_different_candidates_get_different_specification_identity(tmp_path: Path) -> None:
    context_a = _setup(tmp_path / "a", symbol="EURUSD")
    context_b = _setup(tmp_path / "b", symbol="GBPUSD")
    spec_a = _create(context_a)
    spec_b = _create(context_b)
    assert spec_a.specification_id != spec_b.specification_id
    assert spec_a.hypothesis_id != spec_b.hypothesis_id


# ---------------------------------------------------------------------------
# 13-16: trusted boundary / fail-closed on missing or invalid trusted fields.
# ---------------------------------------------------------------------------


def test_high_confidence_does_not_auto_approve_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    assert context.accepted.proposal.confidence.value == "HIGH"
    # Nothing about acceptance/ingestion alone creates a specification.
    assert context.spec_control.get_for_hypothesis(context.hypothesis.hypothesis_id) is None
    assert _counts(context.db_path)["research_experiment_specifications"] == 0


def test_proposal_text_cannot_set_trusted_statistical_controls(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    # The proposal's own falsifiable_claim/proposed_test/rejection_condition
    # never flow into any trusted numeric control -- they only appear
    # (verbatim, for provenance) inside the family_definition captured from
    # the already-registered hypothesis.
    assert spec.test_family == _SPEC_KWARGS["test_family"]
    assert spec.primary_metric == _SPEC_KWARGS["primary_metric"]
    assert spec.family_definition["falsifiable_claim"] == context.accepted.proposal.falsifiable_claim
    assert "falsifiable_claim" not in {"test_family", "primary_metric", "alpha", "q"}


def test_missing_required_trusted_field_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with pytest.raises(TypeError):
        context.spec_control.create_specification(
            context.accepted.intake_id,
            reviewer_id="operator:reviewer",
            test_family="smc_pattern_journal_v1",
            primary_metric="avg_net_atr",
            alpha=0.05,
            q=0.10,
            minimum_effect_size=0.0,
            # max_hypotheses_tests intentionally omitted.
            datasets=(context.dataset,),
        )
    assert _counts(context.db_path)["research_experiment_specifications"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("alpha", 0.0),
        ("alpha", 1.5),
        ("q", -0.1),
        ("q", 0.0),
        ("minimum_effect_size", -1.0),
        ("max_hypotheses_tests", 0),
        ("test_family", ""),
        ("primary_metric", "   "),
        ("reviewer_id", ""),
    ],
)
def test_invalid_statistical_or_domain_values_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    context = _setup(tmp_path)
    with pytest.raises(ValueError):
        _create(context, **{field: value})
    assert _counts(context.db_path)["research_experiment_specifications"] == 0


# ---------------------------------------------------------------------------
# 17-19: dataset / final-holdout safety.
# ---------------------------------------------------------------------------


def test_dataset_identity_must_be_immutable_and_verifiable(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    tampered = DatasetArtifact(
        file_path=context.dataset.file_path, sha256="0" * 64, size_bytes=context.dataset.size_bytes
    )
    with pytest.raises(ValueError, match="verification"):
        _create(context, datasets=(tampered,))


def test_dataset_bound_after_hashing_but_changed_on_disk_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    Path(context.dataset.file_path).write_text("time,symbol,close\n2,XAUUSD,3000.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="verification"):
        _create(context)


def test_holdout_shaped_dataset_path_is_rejected(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    holdout_like = _dataset(tmp_path, name="sealed_holdout_plaintext.csv")
    with pytest.raises(ValueError, match="holdout"):
        _create(context, datasets=(holdout_like,))


def test_no_holdout_related_imports_in_specification_module() -> None:
    source = Path("src/trademind/research_experiment_specification.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("holdout_sealer", "holdout_store", "holdout_crypto", "orchestrator_bridge")
    lowered = {name.lower() for name in imported}
    for name in lowered:
        for term in forbidden:
            assert term not in name, f"unexpected holdout-shaped import: {name}"


# ---------------------------------------------------------------------------
# 20-25: absolute state boundary.
# ---------------------------------------------------------------------------


def test_hypothesis_remains_proposed_after_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    _create(context)
    after = context.registry.get(context.hypothesis.hypothesis_id)
    assert after.state is HypothesisState.PROPOSED


def test_manifest_hash_remains_none_after_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    _create(context)
    after = context.registry.get(context.hypothesis.hypothesis_id)
    assert after.manifest_hash is None


def test_no_manifest_freeze_call_anywhere_in_specification_module() -> None:
    source = Path("src/trademind/research_experiment_specification.py").read_text(encoding="utf-8")
    assert ".freeze(" not in source
    assert "registry.transition(" not in source
    assert "HypothesisState.FROZEN" not in source


def test_no_experiment_execution_or_validation_imports() -> None:
    source = Path("src/trademind/research_experiment_specification.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "experiment_execution_runtime",
        "experiment_execution_contract",
        "experiment_evidence",
        "validation_decision",
        "final_holdout_decision_gate",
        "final_holdout_evaluation",
    )
    lowered = {name.lower() for name in imported}
    for name in lowered:
        for term in forbidden:
            assert term not in name, f"unexpected execution/validation import: {name}"


# ---------------------------------------------------------------------------
# 26-27: provider/network/broker structural safety.
# ---------------------------------------------------------------------------


def test_no_provider_network_or_broker_trading_path() -> None:
    source = Path("src/trademind/research_experiment_specification.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_import_substrings = (
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
    lowered_imports = {name.lower() for name in imported}
    for name in lowered_imports:
        for term in forbidden_import_substrings:
            assert term not in name, f"unexpected network/provider-shaped import: {name!r}"

    forbidden_call_shaped = (
        "OrderSend(",
        "PositionClose(",
        "PositionModify(",
        "CTrade",
        "TRADE_ACTION_DEAL",
    )
    for token in forbidden_call_shaped:
        assert token not in source, f"unexpected broker-trading token: {token!r}"


# ---------------------------------------------------------------------------
# 28: provenance chain complete.
# ---------------------------------------------------------------------------


def test_provenance_chain_is_complete(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    assert spec.hypothesis_id == context.hypothesis.hypothesis_id
    assert spec.hypothesis_family_id == context.hypothesis.hypothesis_family_id
    assert spec.hypothesis_content_hash == context.hypothesis.content_hash
    assert spec.intake_id == context.accepted.intake_id
    assert spec.request_hash == context.accepted.request_hash
    assert spec.authorization_id == context.accepted.authorization_id
    assert spec.task_id == context.accepted.task_id
    assert spec.task_revision == context.accepted.task_revision
    assert spec.packet_artifact_hash_ref == context.accepted.packet_artifact_hash_ref
    assert spec.packet_semantic_hash == context.accepted.packet_semantic_hash
    assert spec.result_artifact_hash_ref == context.accepted.result_artifact_hash_ref
    assert spec.proposal_index == context.accepted.proposal_index
    assert spec.candidate_id == context.accepted.candidate_id
    assert spec.candidate_id == context.packet.candidate_bindings[0]["candidate_id"]


# ---------------------------------------------------------------------------
# 29-30: tamper / restart durability.
# ---------------------------------------------------------------------------


def test_tampered_persisted_content_hash_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE research_experiment_specifications SET alpha=0.99 WHERE specification_id=?",
            (spec.specification_id,),
        )
    with pytest.raises(ResearchExperimentSpecificationDatabaseError):
        context.spec_control.get(spec.specification_id)


def test_tampered_family_definition_fails_closed(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    with sqlite3.connect(context.db_path) as db:
        db.execute(
            "UPDATE research_experiment_specifications SET family_definition_json=? WHERE specification_id=?",
            (json.dumps({"tampered": True}), spec.specification_id),
        )
    with pytest.raises(ResearchExperimentSpecificationSourceError):
        context.spec_control.reverify(spec.specification_id)


def test_restart_durability(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    created = _create(context)
    restarted = _restart(context)
    reloaded = restarted.get(created.specification_id)
    assert reloaded == created
    assert restarted.reverify(created.specification_id) == created
    after = HypothesisRegistry(context.db_path).get(context.hypothesis.hypothesis_id)
    assert after.state is HypothesisState.PROPOSED
    assert after.manifest_hash is None


# ---------------------------------------------------------------------------
# 31-32: concurrency.
# ---------------------------------------------------------------------------


def test_sixteen_concurrent_identical_creates_produce_one_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    barrier = threading.Barrier(16)

    def create(_index: int):
        barrier.wait()
        return _create(context)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(create, range(16)))
    assert all(result == results[0] for result in results)
    assert _counts(context.db_path)["research_experiment_specifications"] == 1


def test_concurrent_conflicting_creates_never_silently_overwrite(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    barrier = threading.Barrier(2)

    def create_a():
        barrier.wait()
        return _create(context, alpha=0.05)

    def create_b():
        barrier.wait()
        return _create(context, alpha=0.10)

    successes = []
    conflicts = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(create_a), executor.submit(create_b))
        for future in futures:
            try:
                successes.append(future.result())
            except ResearchExperimentSpecificationConflict as exc:
                conflicts.append(exc)
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert _counts(context.db_path)["research_experiment_specifications"] == 1


# ---------------------------------------------------------------------------
# 33-35: immutability of upstream/adjacent records.
# ---------------------------------------------------------------------------


def test_task_remains_immutable(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    task_before = context.control.task_store.get(context.task.task_id)
    _create(context)
    assert context.control.task_store.get(context.task.task_id) == task_before == context.task


def test_research_execution_remains_immutable(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    execution_before = context.execution_control.get_execution(context.execution.request_hash)
    _create(context)
    assert context.execution_control.get_execution(context.execution.request_hash) == execution_before


def test_intake_record_remains_unchanged_by_specification(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    intake_before = context.intake_control.get(context.accepted.intake_id)
    _create(context)
    assert context.intake_control.get(context.accepted.intake_id) == intake_before


# ---------------------------------------------------------------------------
# Extra: audit trail, and future-ExperimentManifest compatibility (Phase 21/22).
# ---------------------------------------------------------------------------


def test_audit_event_is_recorded_without_mutating_task_state(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    with sqlite3.connect(context.db_path) as db:
        before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    _create(context)
    with sqlite3.connect(context.db_path) as db:
        rows = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM audit_events ORDER BY id"
            ).fetchall()[before:]
        ]
    actions = [row["action"] for row in rows]
    assert "RESEARCH_EXPERIMENT_SPECIFICATION_CREATED" in actions
    created_event = next(r for r in rows if r["action"] == "RESEARCH_EXPERIMENT_SPECIFICATION_CREATED")
    assert created_event["from_state"] == created_event["to_state"] == "NEW"
    assert created_event["metadata"]["manifest_created"] is False
    assert created_event["metadata"]["manifest_frozen"] is False


def test_future_experiment_manifest_family_identity_is_compatible(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    spec = _create(context)
    manifest = ExperimentManifest.new(
        hypothesis_id=spec.hypothesis_id,
        family_definition=spec.family_definition,
        test_family=spec.test_family,
        primary_metric=spec.primary_metric,
        alpha=spec.alpha,
        q=spec.q,
        minimum_effect_size=spec.minimum_effect_size,
        max_hypotheses_tests=spec.max_hypotheses_tests,
        schema_version="experiment-manifest-v1",
        git_commit="f" * 40,
        datasets=spec.datasets,
        parameters=spec.parameters,
    )
    assert manifest.hypothesis_family_id == spec.hypothesis_family_id == context.hypothesis.hypothesis_family_id
    assert manifest.hypothesis_id == spec.hypothesis_id


def test_dataset_count_bound(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    too_many = tuple(
        _dataset(tmp_path, name=f"extra_{i}.csv", content=f"a,b\n{i},{i}\n")
        for i in range(MAX_DATASETS + 1)
    )
    with pytest.raises(ValueError, match="bound"):
        _create(context, datasets=too_many)


def test_control_requires_matching_intake_and_registry_database(tmp_path: Path) -> None:
    context = _setup(tmp_path)
    other_registry = HypothesisRegistry(tmp_path / "other.db")
    with pytest.raises(ResearchExperimentSpecificationDatabaseError):
        ResearchExperimentSpecificationControl(
            intake_control=context.intake_control, hypothesis_registry=other_registry
        )


def test_incomplete_or_unknown_specification_schema_fails_closed(tmp_path: Path) -> None:
    incomplete = _setup(tmp_path / "incomplete")
    with sqlite3.connect(incomplete.db_path) as db:
        db.execute("DROP TABLE research_experiment_specifications")
    with pytest.raises(ResearchExperimentSpecificationDatabaseError):
        ResearchExperimentSpecificationControl(
            intake_control=incomplete.intake_control, hypothesis_registry=incomplete.registry
        )

    unknown = _setup(tmp_path / "unknown")
    with sqlite3.connect(unknown.db_path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "UPDATE research_experiment_specification_meta SET schema_version='unknown-v9' WHERE id=1"
        )
    with pytest.raises(ResearchExperimentSpecificationDatabaseError):
        ResearchExperimentSpecificationControl(
            intake_control=unknown.intake_control, hypothesis_registry=unknown.registry
        )
