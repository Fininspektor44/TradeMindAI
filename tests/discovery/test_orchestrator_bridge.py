import hashlib
import json

from trademind.discovery.holdout_store import HoldoutSealStore
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.manifest import DatasetArtifact, ExperimentManifest
from trademind.discovery.orchestrator_bridge import (
    DiscoveryBridgeError,
    DiscoveryOrchestratorBridge,
)
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import Role, TaskState


BRIDGE_ENVELOPE_HASH = hashlib.sha256(b"bridge-envelope").hexdigest()
BRIDGE_EVALUATOR_HASH = hashlib.sha256(b"bridge-evaluator").hexdigest()
BRIDGE_ISOLATION_HASH = hashlib.sha256(b"bridge-isolation").hexdigest()


def _frozen_case(
    tmp_path,
    *,
    leak_dataset_path_in_parameters: bool = False,
    attest_holdout: bool = True,
):
    dataset = tmp_path / "market.csv"
    dataset.write_text("symbol,timeframe,time,open,high,low,close\n", encoding="utf-8")
    family_definition = {
        "event_type": "synthetic_breakout",
        "direction": "both",
        "feature_set": "bridge-test-v1",
    }
    parameters = {"window": 20}
    if leak_dataset_path_in_parameters:
        parameters["note"] = f"source={dataset}"
    manifest = ExperimentManifest.new(
        hypothesis_id="H-BRIDGE",
        family_definition=family_definition,
        test_family="bridge-tests-v1",
        primary_metric="mean_net_r",
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=10,
        schema_version="discovery-manifest-v1",
        git_commit="test-commit",
        datasets=(DatasetArtifact.from_path(dataset),),
        parameters=parameters,
    )
    registry = HypothesisRegistry(tmp_path / "registry.db")
    registry.register(
        hypothesis_id=manifest.hypothesis_id,
        family_definition=manifest.family_definition,
        content_definition=manifest.content_definition,
    )
    frozen_path = tmp_path / "manifest.json"
    manifest_hash = manifest.freeze(frozen_path)
    registry.freeze(manifest.hypothesis_id, manifest_hash=manifest_hash)

    holdout_seals = HoldoutSealStore(registry)
    holdout_seals.register(
        hypothesis_id=manifest.hypothesis_id,
        envelope_hash=BRIDGE_ENVELOPE_HASH,
        key_id="bridge-key-v1",
        evaluator_id="bridge-tests-v1",
        evaluator_hash=BRIDGE_EVALUATOR_HASH,
    )
    if attest_holdout:
        holdout_seals.mark_isolated(
            manifest.hypothesis_id,
            isolation_receipt_hash=BRIDGE_ISOLATION_HASH,
        )

    control = ControlPlane(tmp_path / "orchestrator.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    bridge = DiscoveryOrchestratorBridge(
        registry=registry,
        holdout_seals=holdout_seals,
        control=control,
        artifacts=artifacts,
    )
    return dataset, frozen_path, manifest, registry, control, artifacts, bridge


def test_frozen_hypothesis_creates_zero_budget_bounded_task(tmp_path):
    dataset, frozen_path, manifest, registry, control, artifacts, bridge = _frozen_case(tmp_path)

    task = bridge.submit_frozen_hypothesis(
        manifest.hypothesis_id,
        manifest_path=frozen_path,
    )

    assert task.task_id.startswith("discovery-")
    assert ":" not in task.task_id
    assert task.state is TaskState.NEW
    assert task.assigned_role is Role.OPERATOR
    assert task.budget_limit == 0.0
    assert task.scope == ("src/trademind/discovery",)
    assert str(frozen_path) not in task.scope
    assert "Final-holdout exposure to Orchestrator agents is forbidden" in task.goal
    assert any("manifest_sha256=" in item for item in task.acceptance_criteria)
    assert any("holdout_isolation_sha256=" in item for item in task.acceptance_criteria)
    assert len(task.artifact_refs) == 1
    assert registry.get(manifest.hypothesis_id).state is HypothesisState.FROZEN
    assert registry.family_status(manifest.hypothesis_family_id)["holdout_consumed"] is False
    assert control.audit_log.verify()

    brief_files = list(artifacts.root.rglob("*research-brief*.json"))
    assert len(brief_files) == 1
    brief_text = brief_files[0].read_text(encoding="utf-8")
    brief = json.loads(brief_text)
    assert "datasets" not in brief
    assert str(dataset) not in brief_text
    assert str(frozen_path) not in brief_text
    assert brief["hypothesis_family_id"] == manifest.hypothesis_family_id
    assert brief["content_hash"] == manifest.content_hash
    assert brief["holdout_isolation_receipt_hash"] == BRIDGE_ISOLATION_HASH


def test_bridge_rejects_unattested_final_holdout(tmp_path):
    _, frozen_path, manifest, registry, _, _, bridge = _frozen_case(
        tmp_path,
        attest_holdout=False,
    )

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("Orchestrator bridge must require final-holdout isolation attestation")

    assert registry.get(manifest.hypothesis_id).state is HypothesisState.FROZEN


def test_positive_budget_is_explicit_opt_in(tmp_path):
    _, frozen_path, manifest, _, _, _, bridge = _frozen_case(tmp_path)
    task = bridge.submit_frozen_hypothesis(
        manifest.hypothesis_id,
        manifest_path=frozen_path,
        budget_limit=1.25,
    )
    assert task.budget_limit == 1.25


def test_unfrozen_hypothesis_is_rejected(tmp_path):
    _, frozen_path, manifest, registry, _, _, bridge = _frozen_case(tmp_path)
    registry.transition(manifest.hypothesis_id, HypothesisState.TRAIN_TESTED)

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("only FROZEN hypotheses may enter orchestration")


def test_manifest_or_dataset_tamper_is_rejected(tmp_path):
    dataset, frozen_path, manifest, _, _, _, bridge = _frozen_case(tmp_path)
    dataset.write_text("tampered\n", encoding="utf-8")

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("dataset hash mutation must block orchestration")


def test_registry_manifest_identity_mismatch_is_rejected(tmp_path):
    _, frozen_path, manifest, _, _, _, bridge = _frozen_case(tmp_path)
    document = json.loads(frozen_path.read_text(encoding="utf-8"))
    document["manifest"]["hypothesis_id"] = "RENAMED"
    frozen_path.write_text(json.dumps(document), encoding="utf-8")

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("manifest identity mutation must block orchestration")


def test_same_frozen_hypothesis_cannot_be_submitted_twice(tmp_path):
    _, frozen_path, manifest, _, _, _, bridge = _frozen_case(tmp_path)
    bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("duplicate orchestration submission must fail closed")


def test_raw_dataset_path_hidden_inside_parameters_is_rejected(tmp_path):
    _, frozen_path, manifest, _, _, artifacts, bridge = _frozen_case(
        tmp_path,
        leak_dataset_path_in_parameters=True,
    )

    try:
        bridge.submit_frozen_hypothesis(manifest.hypothesis_id, manifest_path=frozen_path)
    except DiscoveryBridgeError:
        pass
    else:
        raise AssertionError("metadata must not smuggle a raw dataset path into the brief")

    assert not list(artifacts.root.rglob("*research-brief*.json"))
