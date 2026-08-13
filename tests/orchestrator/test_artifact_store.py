from __future__ import annotations

import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import BinaryIO

import pytest

from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMediaTypeError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactResourceLimitError,
    ArtifactStore,
    ArtifactStoreCapabilityError,
    InvalidArtifactHashRef,
)


class _ChunkTrackingStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.requests: list[int] = []

    def read(self, size: int) -> bytes:
        self.requests.append(size)
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FailingStream:
    def __init__(self) -> None:
        self.calls = 0

    def read(self, size: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise OSError("simulated source failure")


class _ShortReadStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int) -> bytes:
        length = min(size, 2)
        chunk = self._data[self._offset : self._offset + length]
        self._offset += len(chunk)
        return chunk


class _EndlessStream:
    def __init__(self) -> None:
        self.requests: list[int] = []

    def read(self, size: int) -> bytes:
        self.requests.append(size)
        return b"x" * size


class _SingleValueStream:
    def __init__(self, value: object) -> None:
        self._value = value

    def read(self, size: int) -> object:
        return self._value


class _TrackingHandle:
    def __init__(self, handle: BinaryIO, requests: list[int]) -> None:
        self._handle = handle
        self._requests = requests

    def __enter__(self) -> _TrackingHandle:
        return self

    def __exit__(self, *args: object) -> None:
        self._handle.close()

    def read(self, size: int = -1) -> bytes:
        self._requests.append(size)
        return self._handle.read(size)


class _EndlessHandle:
    def __init__(self) -> None:
        self.requests: list[int] = []

    def __enter__(self) -> _EndlessHandle:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        return b"x" * size


def _cas_path(root: Path, digest: str, suffix: str = ".blob") -> Path:
    return root / ".verified-cas-v1" / "objects" / digest[:2] / digest[2:4] / f"{digest}{suffix}"


def _synchronize_finalize(
    monkeypatch: pytest.MonkeyPatch,
    store: ArtifactStore,
    parties: int,
) -> None:
    barrier = Barrier(parties)
    original = store._finalize_snapshot

    def synchronized_finalize(**kwargs: object) -> ArtifactRef:
        barrier.wait(timeout=5)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_finalize_snapshot", synchronized_finalize)


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


def test_legacy_json_bytes_hash_and_layout_remain_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)

    artifact = store.store_json(
        task_id="T1",
        revision=1,
        kind="audit",
        payload={"status": "PASS", "count": 3},
    )

    assert Path(artifact.path).read_bytes() == b'{"count":3,"status":"PASS"}'
    assert artifact.sha256 == "1df56d78ca5d8eff1c655f87ad7e29487c134f3224f5eb5fd89084cac6898edd"
    assert artifact.artifact_id == "T1_r1_audit_1df56d78ca5d8eff"
    assert Path(artifact.path).relative_to(root) == Path("T1/r1/T1_r1_audit_1df56d78ca5d8eff.json")
    assert not (root / ".verified-cas-v1").exists()


def test_verified_store_survives_restart_and_returns_exact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    payload = b"\x00exact persisted bytes\xff"
    original = ArtifactStore(root).import_snapshot(
        io.BytesIO(payload),
        media_type="application/octet-stream",
    )

    restarted = ArtifactStore(root)
    resolved = restarted.resolve_verified(
        original.hash_ref,
        expected_media_type="application/octet-stream",
    )

    assert resolved.hash_ref == original.hash_ref
    assert resolved.size_bytes == len(payload)
    assert resolved.media_type == "application/octet-stream"
    assert restarted.read_verified(original.hash_ref) == payload


def test_verified_read_rejects_tampered_and_truncated_objects(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"complete"), media_type="application/test")
    object_path = Path(artifact.path)

    object_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="do not match"):
        store.read_verified(artifact.hash_ref)

    object_path.write_bytes(b"complet")
    with pytest.raises(ArtifactIntegrityError, match="truncated"):
        store.read_verified(artifact.hash_ref)


def test_verified_read_rejects_appended_byte_after_one_byte_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"four"), media_type="application/test")
    Path(artifact.path).write_bytes(b"four+")
    requests: list[int] = []
    original = store._open_regular_no_follow

    def tracked_open(path: Path) -> BinaryIO:
        handle = original(path)
        if path.suffix == ".blob":
            return _TrackingHandle(handle, requests)  # type: ignore[return-value]
        return handle

    monkeypatch.setattr(store, "_open_regular_no_follow", tracked_open)

    with pytest.raises(ArtifactIntegrityError, match="larger"):
        store.read_verified(artifact.hash_ref)

    assert requests == [4, 1]


def test_verified_read_never_reads_growing_object_beyond_expected_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    endless = _EndlessHandle()
    monkeypatch.setattr(store, "_open_regular_no_follow", lambda _path: endless)

    with pytest.raises(ArtifactIntegrityError, match="larger"):
        store._verify_object(
            Path(store.root / "unused"),
            digest="0" * 64,
            expected_size=7,
            return_bytes=True,
        )

    assert endless.requests == [7, 1]


def test_verified_read_accepts_zero_byte_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.import_snapshot(io.BytesIO(b""), media_type="application/octet-stream")

    assert artifact.size_bytes == 0
    assert store.read_verified(artifact.hash_ref) == b""


def test_verified_read_limit_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    writer = ArtifactStore(root, verified_import_max_bytes=5)
    at_limit = writer.import_snapshot(io.BytesIO(b"1234"), media_type="application/test")
    over_limit = writer.import_snapshot(io.BytesIO(b"12345"), media_type="application/test")
    reader = ArtifactStore(root, verified_read_max_bytes=4)

    assert reader.read_verified(at_limit.hash_ref) == b"1234"
    assert reader.resolve_verified(over_limit.hash_ref).size_bytes == 5
    with pytest.raises(ArtifactResourceLimitError, match="verified_read_max_bytes"):
        reader.read_verified(over_limit.hash_ref)


def test_verified_read_limit_rejects_before_object_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    artifact = ArtifactStore(root).import_snapshot(
        io.BytesIO(b"too large"),
        media_type="application/test",
    )
    reader = ArtifactStore(root, verified_read_max_bytes=1)
    original = reader._open_regular_no_follow
    object_was_opened = False

    def guarded_open(path: Path) -> BinaryIO:
        nonlocal object_was_opened
        if path.suffix == ".blob":
            object_was_opened = True
        return original(path)

    monkeypatch.setattr(reader, "_open_regular_no_follow", guarded_open)

    with pytest.raises(ArtifactResourceLimitError):
        reader.read_verified(artifact.hash_ref)

    assert not object_was_opened


@pytest.mark.parametrize("value", [0, -1, True, 1024**4 + 1, 1.5, "1"])
def test_verified_resource_limits_require_bounded_exact_positive_ints(
    tmp_path: Path,
    value: object,
) -> None:
    root = tmp_path / "artifacts"
    with pytest.raises(ValueError):
        ArtifactStore(root, verified_import_max_bytes=value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ArtifactStore(root, verified_read_max_bytes=value)  # type: ignore[arg-type]
    assert not root.exists()


def test_verified_read_rejects_missing_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    missing = f"sha256:{'a' * 64}"

    with pytest.raises(ArtifactNotFoundError, match="metadata not found"):
        store.resolve_verified(missing)
    with pytest.raises(ArtifactNotFoundError, match="metadata not found"):
        store.read_verified(missing)


@pytest.mark.parametrize(
    "value",
    [
        "a" * 64,
        "A" * 64,
        f"sha256:{'A' * 64}",
        f"sha256:{'a' * 63}",
        f"sha256:{'a' * 65}",
        f"sha1:{'a' * 64}",
        f" sha256:{'a' * 64}",
        f"sha256:{'a' * 64}\n",
        f"sha256:{'a' * 32}\n{'a' * 32}",
        f"sha256:{'a' * 32}\t{'a' * 32}",
        f"sha256:{'a' * 32} {'a' * 32}",
        f"sha256:{'a' * 64}\x00",
        f"sha256%3A{'a' * 64}",
        f"sha256::{'a' * 64}",
        "../sha256:" + "a" * 64,
        "C:\\artifacts\\" + "a" * 64,
        "https://example.test/" + "a" * 64,
        f"sha256：{'a' * 64}",
        f"ѕha256:{'a' * 64}",
        f"sha\u0300256:{'a' * 64}",
        f"sha256:{'а' * 64}",
        Path(f"sha256:{'a' * 64}"),
        f"sha256:{'a' * 64}".encode(),
        object(),
    ],
)
def test_verified_apis_reject_non_strict_hash_refs(tmp_path: Path, value: object) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(InvalidArtifactHashRef):
        store.resolve_verified(value)
    with pytest.raises(InvalidArtifactHashRef):
        store.read_verified(value)


def test_verified_media_type_is_persisted_and_checked_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    artifact = ArtifactStore(root).import_snapshot(
        io.BytesIO(b"json-like bytes"),
        media_type="application/json",
    )
    restarted = ArtifactStore(root)

    with pytest.raises(ArtifactMediaTypeError, match="does not match"):
        restarted.resolve_verified(
            artifact.hash_ref,
            expected_media_type="text/plain",
        )
    assert (
        restarted.read_verified(
            artifact.hash_ref,
            expected_media_type="application/json",
        )
        == b"json-like bytes"
    )


@pytest.mark.parametrize(
    "media_type",
    ["", " application/test", "application/test ", "application/\ntest", "текст", b"text/plain"],
)
def test_verified_media_type_requires_bounded_safe_ascii(
    tmp_path: Path,
    media_type: object,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError):
        store.import_snapshot(io.BytesIO(b"x"), media_type=media_type)  # type: ignore[arg-type]


def test_verified_media_type_is_advisory_first_writer_label_and_case_sensitive(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.import_snapshot(io.BytesIO(b"same"), media_type="Application/Test")

    assert artifact.sha256 == hashlib.sha256(b"same").hexdigest()
    with pytest.raises(ArtifactMediaTypeError, match="does not match"):
        store.read_verified(artifact.hash_ref, expected_media_type="application/test")
    with pytest.raises(ArtifactMediaTypeError, match="different media type"):
        store.import_snapshot(io.BytesIO(b"same"), media_type="application/test")


def test_snapshot_import_is_idempotent_and_content_addressed(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    first = store.import_snapshot(io.BytesIO(b"same"), media_type="application/test")
    second = store.import_snapshot(io.BytesIO(b"same"), media_type="application/test")
    different = store.import_snapshot(io.BytesIO(b"different"), media_type="application/test")

    assert first == second
    assert first.hash_ref != different.hash_ref
    assert Path(first.path).read_bytes() == b"same"


def test_concurrent_snapshot_import_is_atomic_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    payload = b"same concurrent snapshot"
    store = ArtifactStore(root)
    parties = 4
    _synchronize_finalize(monkeypatch, store, parties)

    def import_once() -> ArtifactRef:
        return store.import_snapshot(
            io.BytesIO(payload),
            media_type="application/test",
        )

    with ThreadPoolExecutor(max_workers=parties) as pool:
        artifacts = list(pool.map(lambda _index: import_once(), range(parties)))

    assert len({artifact.hash_ref for artifact in artifacts}) == 1
    assert ArtifactStore(root).read_verified(artifacts[0].hash_ref) == payload
    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []


def test_concurrent_conflicting_media_types_have_one_verified_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    _synchronize_finalize(monkeypatch, store, 2)

    def import_once(media_type: str) -> ArtifactRef | Exception:
        try:
            return store.import_snapshot(io.BytesIO(b"same"), media_type=media_type)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(import_once, ["application/type-a", "application/type-b"]))

    successes = [outcome for outcome in outcomes if isinstance(outcome, ArtifactRef)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ArtifactMediaTypeError)
    assert (
        ArtifactStore(root).read_verified(
            successes[0].hash_ref,
            expected_media_type=successes[0].media_type,
        )
        == b"same"
    )
    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []


def test_same_bytes_with_conflicting_media_type_fail_closed(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    store.import_snapshot(io.BytesIO(b"same"), media_type="application/type-a")

    with pytest.raises(ArtifactMediaTypeError, match="different media type"):
        store.import_snapshot(io.BytesIO(b"same"), media_type="application/type-b")


def test_snapshot_import_uses_bounded_chunks_without_a_source_path(tmp_path: Path) -> None:
    payload = os.urandom(3 * 1024 * 1024 + 17)
    source = _ChunkTrackingStream(payload)
    store = ArtifactStore(tmp_path / "artifacts")

    artifact = store.import_snapshot(source, media_type="application/octet-stream")

    assert store.read_verified(artifact.hash_ref) == payload
    assert len(source.requests) >= 5
    assert set(source.requests) == {1024 * 1024}
    assert artifact.sha256 == hashlib.sha256(payload).hexdigest()


def test_snapshot_import_accepts_zero_and_irregular_short_reads(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    empty = store.import_snapshot(io.BytesIO(b""), media_type="application/octet-stream")
    irregular = store.import_snapshot(
        _ShortReadStream(b"irregular"),
        media_type="application/octet-stream",
    )

    assert store.read_verified(empty.hash_ref) == b""
    assert store.read_verified(irregular.hash_ref) == b"irregular"


def test_snapshot_import_limit_boundaries_and_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root, verified_import_max_bytes=4)
    accepted = store.import_snapshot(io.BytesIO(b"1234"), media_type="application/test")

    with pytest.raises(ArtifactResourceLimitError, match="verified_import_max_bytes"):
        store.import_snapshot(io.BytesIO(b"12345"), media_type="application/test")

    assert store.read_verified(accepted.hash_ref) == b"1234"
    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []


def test_endless_snapshot_stops_at_import_limit_without_unbounded_disk(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    source = _EndlessStream()
    store = ArtifactStore(root, verified_import_max_bytes=1024 * 1024 + 3)

    with pytest.raises(ArtifactResourceLimitError):
        store.import_snapshot(source, media_type="application/test")

    assert source.requests == [1024 * 1024, 4]
    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []


@pytest.mark.parametrize("value", ["text", bytearray(b"x"), memoryview(b"x")])
def test_snapshot_import_requires_exact_bytes(tmp_path: Path, value: object) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(TypeError, match="exact bytes"):
        store.import_snapshot(_SingleValueStream(value), media_type="application/test")


def test_verified_cas_capability_failure_is_explicit_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    monkeypatch.setattr(os, "link", lambda *_args: (_ for _ in ()).throw(OSError("unsupported")))

    with pytest.raises(ArtifactStoreCapabilityError, match="hard-link capability"):
        store.import_snapshot(io.BytesIO(b"snapshot"), media_type="application/test")

    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []
    legacy = store.store_text(task_id="T1", revision=1, kind="log", text="legacy")
    assert store.verify(legacy)


def test_metadata_capability_failure_leaves_recoverable_object_without_temp_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    original_link = os.link
    calls = 0

    def fail_second_link(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata hard links unsupported")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", fail_second_link)
    with pytest.raises(ArtifactStoreCapabilityError):
        store.import_snapshot(io.BytesIO(b"snapshot"), media_type="application/test")

    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []
    monkeypatch.setattr(os, "link", original_link)
    recovered = store.import_snapshot(io.BytesIO(b"snapshot"), media_type="application/test")
    assert store.read_verified(recovered.hash_ref) == b"snapshot"


def test_metadata_fsync_failure_leaves_recoverable_object_without_temp_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    original_fsync = os.fsync
    calls = 0

    def fail_metadata_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(store, "_fsync_directory", lambda _directory: None)
    monkeypatch.setattr(os, "fsync", fail_metadata_fsync)
    with pytest.raises(OSError, match="metadata fsync failed"):
        store.import_snapshot(io.BytesIO(b"snapshot"), media_type="application/test")

    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []
    assert list((root / ".verified-cas-v1" / "objects").rglob(".metadata-*")) == []
    monkeypatch.setattr(os, "fsync", original_fsync)
    recovered = store.import_snapshot(io.BytesIO(b"snapshot"), media_type="application/test")
    assert store.read_verified(recovered.hash_ref) == b"snapshot"


def test_failed_snapshot_import_cleans_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)

    with pytest.raises(OSError, match="simulated source failure"):
        store.import_snapshot(_FailingStream(), media_type="application/test")

    temporary = root / ".verified-cas-v1" / "tmp"
    assert list(temporary.iterdir()) == []


def test_existing_wrong_object_for_target_digest_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    payload = b"intended bytes"
    digest = hashlib.sha256(payload).hexdigest()
    target = _cas_path(root, digest)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong existing bytes")
    store = ArtifactStore(root)

    with pytest.raises(ArtifactIntegrityError, match="larger|do not match"):
        store.import_snapshot(io.BytesIO(payload), media_type="application/test")
    assert list((root / ".verified-cas-v1" / "tmp").iterdir()) == []


@pytest.mark.parametrize(
    "metadata_bytes, message",
    [
        (b"{", "malformed"),
        (
            b'{"hash_ref":"sha256:'
            + b"0" * 64
            + b'","hash_ref":"sha256:'
            + b"0" * 64
            + b'","media_type":"application/test","schema_version":"artifact-store-verified-cas-v1","size_bytes":1}',
            "duplicate keys",
        ),
        (
            json.dumps(
                {
                    "hash_ref": f"sha256:{'0' * 64}",
                    "media_type": "application/test",
                    "schema_version": "artifact-store-verified-cas-v1",
                    "size_bytes": 1,
                    "unknown": True,
                },
                separators=(",", ":"),
            ).encode(),
            "fields do not match",
        ),
    ],
)
def test_verified_read_rejects_malformed_metadata(
    tmp_path: Path,
    metadata_bytes: bytes,
    message: str,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"x"), media_type="application/test")
    metadata = _cas_path(root, artifact.sha256, ".meta.json")
    metadata.write_bytes(metadata_bytes)

    with pytest.raises(ArtifactIntegrityError, match=message):
        store.read_verified(artifact.hash_ref)
    with pytest.raises(ArtifactIntegrityError):
        store.import_snapshot(io.BytesIO(b"x"), media_type="application/test")
    assert metadata.read_bytes() == metadata_bytes


def test_verified_read_rejects_metadata_bound_to_another_hash(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"x"), media_type="application/test")
    metadata = _cas_path(root, artifact.sha256, ".meta.json")
    payload = json.loads(metadata.read_text(encoding="ascii"))
    payload["hash_ref"] = f"sha256:{'0' * 64}"
    metadata.write_text(json.dumps(payload), encoding="ascii")

    with pytest.raises(ArtifactIntegrityError, match="hash does not match"):
        store.resolve_verified(artifact.hash_ref)


def test_verified_read_rejects_symlink_escape(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"outside object"
    digest = hashlib.sha256(payload).hexdigest()
    safe_parent = root / ".verified-cas-v1" / "objects" / digest[:2]
    safe_parent.mkdir(parents=True)
    link = safe_parent / digest[2:4]
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    store = ArtifactStore(root)

    with pytest.raises(ArtifactIntegrityError, match="symlink or reparse"):
        store.import_snapshot(io.BytesIO(payload), media_type="application/test")


def test_verified_read_rejects_object_replaced_by_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"inside"), media_type="application/test")
    object_path = Path(artifact.path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"inside")
    object_path.unlink()
    try:
        object_path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    with pytest.raises(ArtifactIntegrityError, match="symlink or reparse"):
        store.read_verified(artifact.hash_ref)


def test_verified_read_rejects_metadata_replaced_by_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"inside"), media_type="application/test")
    metadata = _cas_path(root, artifact.sha256, ".meta.json")
    outside = tmp_path / "outside.json"
    outside.write_bytes(metadata.read_bytes())
    metadata.unlink()
    try:
        metadata.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not permitted")

    with pytest.raises(ArtifactIntegrityError, match="symlink or reparse"):
        store.resolve_verified(artifact.hash_ref)


def test_verified_read_rejects_object_replaced_by_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    artifact = store.import_snapshot(io.BytesIO(b"inside"), media_type="application/test")
    object_path = Path(artifact.path)
    object_path.unlink()
    os.mkfifo(object_path)

    with pytest.raises(ArtifactIntegrityError, match="not a regular file"):
        store.read_verified(artifact.hash_ref)


def test_verified_resolution_does_not_trust_external_artifact_path(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    artifact = ArtifactStore(root).import_snapshot(
        io.BytesIO(b"inside"),
        media_type="application/test",
    )

    restarted = ArtifactStore(root)
    resolved = restarted.resolve_verified(artifact.hash_ref)

    assert Path(resolved.path) != outside
    assert restarted.read_verified(artifact.hash_ref) == b"inside"
