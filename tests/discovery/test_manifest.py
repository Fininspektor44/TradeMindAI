import hashlib
import json

from trademind.discovery.hypothesis_registry import derive_hypothesis_family_id
from trademind.discovery.manifest import (
    DatasetArtifact,
    ExperimentManifest,
    ManifestIntegrityError,
    verify_frozen_manifest,
)


def _family():
    return {
        "event_type": "breakout",
        "features": ["h1_bias", "m5_volume"],
        "direction": "trend",
        "outcome": "forward_r",
    }


def _manifest(dataset):
    return ExperimentManifest.new(
        hypothesis_id="H1",
        family_definition=_family(),
        test_family="breakout-events-v1",
        primary_metric="mean_net_r",
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=20,
        schema_version="discovery-manifest-v1",
        git_commit="abc123",
        datasets=(DatasetArtifact.from_path(dataset),),
        parameters={"window": 20},
    )


def _canonical(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def test_manifest_freeze_and_verify_dataset_hashes(tmp_path):
    dataset = tmp_path / "data.csv"
    dataset.write_text("x\n1\n", encoding="utf-8")
    manifest = _manifest(dataset)
    assert manifest.hypothesis_family_id == derive_hypothesis_family_id(_family())
    frozen = tmp_path / "H1.json"
    frozen_hash = manifest.freeze(frozen)
    loaded = verify_frozen_manifest(frozen, expected_manifest_hash=frozen_hash)
    assert loaded["manifest_hash"] == manifest.manifest_hash

    try:
        manifest.freeze(frozen)
    except FileExistsError:
        pass
    else:
        raise AssertionError("frozen manifest must not be overwritten")

    dataset.write_text("x\n2\n", encoding="utf-8")
    try:
        verify_frozen_manifest(frozen, expected_manifest_hash=frozen_hash)
    except ManifestIntegrityError:
        pass
    else:
        raise AssertionError("dataset hash mutation must invalidate the manifest")


def test_manifest_document_tamper_is_detected_even_if_internal_hash_is_recomputed(tmp_path):
    dataset = tmp_path / "data.csv"
    dataset.write_text("x\n1\n", encoding="utf-8")
    frozen = tmp_path / "H1.json"
    frozen_hash = _manifest(dataset).freeze(frozen)
    document = json.loads(frozen.read_text(encoding="utf-8"))
    document["manifest"]["primary_metric"] = "changed_after_freeze"
    document["manifest_hash"] = hashlib.sha256(
        _canonical(document["manifest"]).encode("utf-8")
    ).hexdigest()
    frozen.write_text(json.dumps(document), encoding="utf-8")

    try:
        verify_frozen_manifest(
            frozen,
            expected_manifest_hash=frozen_hash,
            verify_datasets=False,
        )
    except ManifestIntegrityError:
        pass
    else:
        raise AssertionError("external registry hash must reject a re-hashed manifest rewrite")


def test_manifest_nested_inputs_cannot_be_mutated_through_returned_properties(tmp_path):
    dataset = tmp_path / "data.csv"
    dataset.write_text("x\n1\n", encoding="utf-8")
    manifest = _manifest(dataset)
    original_hash = manifest.manifest_hash

    parameters = manifest.parameters
    parameters["window"] = 999
    family = manifest.family_definition
    family["event_type"] = "rewritten"

    assert manifest.parameters["window"] == 20
    assert manifest.family_definition["event_type"] == "breakout"
    assert manifest.manifest_hash == original_hash
