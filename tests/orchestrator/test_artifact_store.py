from pathlib import Path

from trademind.orchestrator.artifact_store import ArtifactStore


def test_json_artifact_is_content_addressed_and_verifiable(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.store_json(
        task_id="T1",
        revision=1,
        kind="audit",
        payload={"status": "PASS", "count": 3},
    )

    assert artifact.hash_ref.startswith("sha256:")
    assert artifact.media_type == "application/json"
    assert store.verify(artifact)
    assert Path(artifact.path).is_file()


def test_tampered_artifact_fails_verification(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.store_text(task_id="T1", revision=1, kind="log", text="original")
    Path(artifact.path).write_text("changed", encoding="utf-8")
    assert not store.verify(artifact)


def test_artifact_components_cannot_escape_store_root(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    for task_id, kind in (("../escape", "log"), ("T1", "../escape")):
        try:
            store.store_text(task_id=task_id, revision=1, kind=kind, text="x")
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal must be rejected")


def test_identical_artifact_is_deduplicated(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.store_text(task_id="T1", revision=1, kind="report", text="same")
    second = store.store_text(task_id="T1", revision=1, kind="report", text="same")
    assert first == second
