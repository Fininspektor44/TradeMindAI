import json

from trademind.discovery.manifest import (
    DatasetArtifact,
    ExperimentManifest,
    ManifestIntegrityError,
    verify_frozen_manifest,
)


def _manifest(dataset):
    return ExperimentManifest.new(
        hypothesis_id="H1",
        hypothesis_family_id="F1",
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


def test_manifest_freeze_and_verify_dataset_hashes(tmp_path):
    dataset = tmp_path / "data.csv"
    dataset.write_text("x\n1\n", encoding="utf-8")
    manifest = _manifest(dataset)
    frozen = tmp_path / "H1.json"
    manifest.freeze(frozen)
    loaded = verify_frozen_manifest(frozen)
    assert loaded["manifest_hash"] == manifest.manifest_hash

    try:
        manifest.freeze(frozen)
    except FileExistsError:
        pass
    else:
        raise AssertionError("frozen manifest must not be overwritten")

    dataset.write_text("x\n2\n", encoding="utf-8")
    try:
        verify_frozen_manifest(frozen)
    except ManifestIntegrityError:
        pass
    else:
        raise AssertionError("dataset hash mutation must invalidate the manifest")


def test_manifest_document_tamper_is_detected(tmp_path):
    dataset = tmp_path / "data.csv"
    dataset.write_text("x\n1\n", encoding="utf-8")
    frozen = tmp_path / "H1.json"
    _manifest(dataset).freeze(frozen)
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    payload["manifest"]["primary_metric"] = "changed_after_freeze"
    frozen.write_text(json.dumps(payload), encoding="utf-8")

    try:
        verify_frozen_manifest(frozen, verify_datasets=False)
    except ManifestIntegrityError:
        pass
    else:
        raise AssertionError("manifest mutation must be rejected")
