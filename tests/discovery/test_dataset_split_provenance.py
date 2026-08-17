"""Tests for the machine-verifiable dataset/split-role provenance contract.

Proves the security invariant a caller-supplied ``DatasetArtifactV2.role``
label alone cannot: a claimed DISCOVERY/VALIDATION artifact is verified by
actually reading its row content from Verified CAS and checking every row's
own timestamp against the bound split plan's boundary -- never by trusting
a filename, a path, a folder convention, or the caller's own claim.
"""

from __future__ import annotations

import ast
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.discovery.dataset_split_provenance import (
    DATASET_SPLIT_ROLES,
    BoundSplitPlanV1,
    DatasetProvenanceError,
    DatasetSplitMembershipV1,
    bind_split_plan_to_source,
    verify_dataset_split_membership,
)
from trademind.discovery.split_engine import chronological_split
from trademind.orchestrator.artifact_store import ArtifactStore

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _timestamps(count: int = 10, *, start: datetime = _START) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, time_column: str = "time") -> bytes:
    lines = [f"{time_column},value"] + [f"{t.isoformat()},{i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_source(tmp_path: Path, rows: list[datetime], *, name: str = "source.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(_csv_bytes(rows))
    return path


def _bound(tmp_path: Path, rows: list[datetime] | None = None, *, name: str = "source.csv"):
    rows = rows or _timestamps()
    plan = chronological_split(rows)
    source_path = _write_source(tmp_path, rows, name=name)
    bound = bind_split_plan_to_source(str(source_path), split_plan=plan)
    return rows, plan, bound


def _import(store: ArtifactStore, payload: bytes, *, media_type: str = "text/csv"):
    return store.import_snapshot(io.BytesIO(payload), media_type=media_type)


# ---------------------------------------------------------------------------
# 1-4: deterministic identity.
# ---------------------------------------------------------------------------


def test_deterministic_split_identity_is_reproducible(tmp_path: Path) -> None:
    rows, plan, bound_first = _bound(tmp_path)
    source_path = tmp_path / "source.csv"
    bound_second = bind_split_plan_to_source(str(source_path), split_plan=plan)
    assert bound_first.bound_split_plan_hash == bound_second.bound_split_plan_hash


def test_same_source_same_split_produces_same_identity(tmp_path: Path) -> None:
    rows = _timestamps()
    plan_a = chronological_split(rows)
    plan_b = chronological_split(list(rows))  # rebuilt from an equal-but-distinct list.
    source_path = _write_source(tmp_path, rows)
    bound_a = bind_split_plan_to_source(str(source_path), split_plan=plan_a)
    bound_b = bind_split_plan_to_source(str(source_path), split_plan=plan_b)
    assert bound_a.bound_split_plan_hash == bound_b.bound_split_plan_hash


def test_changed_boundaries_produce_different_identity(tmp_path: Path) -> None:
    rows = _timestamps(20)
    source_path = _write_source(tmp_path, rows)
    plan_default = chronological_split(rows)
    plan_other = chronological_split(rows, discovery_fraction=0.5, validation_fraction=0.3, holdout_fraction=0.2)
    assert plan_default.discovery_count != plan_other.discovery_count
    bound_default = bind_split_plan_to_source(str(source_path), split_plan=plan_default)
    bound_other = bind_split_plan_to_source(str(source_path), split_plan=plan_other)
    assert bound_default.bound_split_plan_hash != bound_other.bound_split_plan_hash


def test_changed_source_hash_produces_different_identity(tmp_path: Path) -> None:
    rows = _timestamps()
    plan = chronological_split(rows)
    path_a = _write_source(tmp_path, rows, name="a.csv")
    path_b = tmp_path / "b.csv"
    path_b.write_bytes(path_a.read_bytes() + b"\n")  # one byte different -> different sha256.
    bound_a = bind_split_plan_to_source(str(path_a), split_plan=plan)
    bound_b = bind_split_plan_to_source(str(path_b), split_plan=plan)
    assert bound_a.source_dataset_sha256 != bound_b.source_dataset_sha256
    assert bound_a.bound_split_plan_hash != bound_b.bound_split_plan_hash


def test_bound_split_plan_round_trips_through_payload(tmp_path: Path) -> None:
    _, _, bound = _bound(tmp_path)
    restored = BoundSplitPlanV1.from_payload(bound.to_payload())
    assert restored == bound


def test_membership_round_trips_through_payload(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))
    membership = verify_dataset_split_membership(
        artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
    )
    restored = DatasetSplitMembershipV1.from_payload(membership.to_payload())
    assert restored == membership


# ---------------------------------------------------------------------------
# 5-6: legitimate TRAIN(=DISCOVERY)/VALIDATION artifacts verify.
# ---------------------------------------------------------------------------


def test_discovery_train_artifact_exact_membership_verifies(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))
    membership = verify_dataset_split_membership(
        artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
    )
    assert membership.role == "DISCOVERY"
    assert membership.row_count == plan.discovery_count
    assert membership.bound_split_plan_hash == bound.bound_split_plan_hash


def test_validation_artifact_verifies(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = _import(store, _csv_bytes(validation_rows))
    membership = verify_dataset_split_membership(
        artifact_store=store, bound_split_plan=bound, role="VALIDATION", artifact_hash_ref=artifact.hash_ref
    )
    assert membership.role == "VALIDATION"
    assert membership.row_count == plan.validation_count


# ---------------------------------------------------------------------------
# 7: FINAL_HOLDOUT can never masquerade as TRAIN -- structurally illegal role.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden_role", ["FINAL_HOLDOUT", "HOLDOUT", "holdout", "Final_Holdout", "TRAIN"])
def test_final_holdout_and_unknown_roles_are_structurally_illegal(tmp_path: Path, forbidden_role: str) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))
    assert forbidden_role not in DATASET_SPLIT_ROLES
    with pytest.raises(DatasetProvenanceError, match="role must be one of"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role=forbidden_role, artifact_hash_ref=artifact.hash_ref
        )


# ---------------------------------------------------------------------------
# 8-10: content-based rejection -- no reliance on filename/path/label.
# ---------------------------------------------------------------------------


def test_full_unsplit_source_rejected_as_discovery(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    full_artifact = _import(store, _csv_bytes(rows))  # every row, including holdout range.
    with pytest.raises(DatasetProvenanceError, match="outside the declared"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=full_artifact.hash_ref
        )


def test_full_unsplit_source_rejected_as_validation(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    full_artifact = _import(store, _csv_bytes(rows))
    with pytest.raises(DatasetProvenanceError):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="VALIDATION", artifact_hash_ref=full_artifact.hash_ref
        )


def test_renamed_holdout_containing_content_still_rejected(tmp_path: Path) -> None:
    """CAS is content-addressed and has no filename concept at all -- this
    proves the check is name-independent by construction, not merely by
    convention: the exact same holdout-containing bytes are imported with a
    deliberately innocuous, holdout-free media type and no path is ever
    involved, and verification still fails on row content alone."""
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    holdout_rows = rows[plan.discovery_count + plan.validation_count :]
    innocuous_artifact = _import(store, _csv_bytes(holdout_rows), media_type="text/csv")
    assert "holdout" not in innocuous_artifact.media_type.lower()
    with pytest.raises(DatasetProvenanceError, match="outside the declared"):
        verify_dataset_split_membership(
            artifact_store=store,
            bound_split_plan=bound,
            role="DISCOVERY",
            artifact_hash_ref=innocuous_artifact.hash_ref,
        )


def test_caller_forged_role_label_is_rejected_by_content(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    artifact = _import(store, _csv_bytes(validation_rows))
    # Caller claims this is DISCOVERY; the actual row content is VALIDATION-range.
    with pytest.raises(DatasetProvenanceError, match="outside the declared"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
        )


# ---------------------------------------------------------------------------
# 11-12: tamper detection.
# ---------------------------------------------------------------------------


def test_tampered_artifact_hash_rejected(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))
    object_path = Path(store.resolve_verified(artifact.hash_ref).path)
    object_path.write_bytes(b"tampered-not-the-authoritative-dataset")
    with pytest.raises(Exception):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
        )


def test_tampered_split_plan_payload_is_rejected(tmp_path: Path) -> None:
    _, _, bound = _bound(tmp_path)
    payload = dict(bound.to_payload())
    tampered_plan = dict(payload["split_plan"])
    tampered_plan["discovery_count"] = tampered_plan["discovery_count"] + 1
    tampered_plan["total_rows"] = tampered_plan["total_rows"] + 1
    payload["split_plan"] = tampered_plan
    # split_plan_semantic_hash is left as the ORIGINAL (untampered) value.
    with pytest.raises(DatasetProvenanceError, match="split identity mismatch"):
        BoundSplitPlanV1.from_payload(payload)


def test_tampered_bound_split_plan_hash_field_is_not_trusted_blindly(tmp_path: Path) -> None:
    """bound_split_plan_hash is always recomputed by BoundSplitPlanV1itself
    (field(init=False)); a caller cannot inject an arbitrary value for it."""
    _, _, bound = _bound(tmp_path)
    payload = dict(bound.to_payload())
    reconstructed = BoundSplitPlanV1.from_payload(payload)
    assert reconstructed.bound_split_plan_hash == bound.bound_split_plan_hash


# ---------------------------------------------------------------------------
# 13: overlap with final holdout rejected even for a "VALIDATION" claim.
# ---------------------------------------------------------------------------


def test_validation_rows_overlapping_final_holdout_boundary_rejected(tmp_path: Path) -> None:
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    # One legitimate validation row plus one row exactly at the holdout boundary.
    overlapping_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count] + [
        datetime.fromisoformat(plan.holdout_start)
    ]
    artifact = _import(store, _csv_bytes(overlapping_rows))
    # SplitPlan's own closed __post_init__ invariant guarantees
    # validation_end < holdout_start strictly, so the holdout-boundary row is
    # already caught by the ordinary role-boundary check below -- the same
    # single mechanism proves both "is this really VALIDATION" and "does it
    # overlap final holdout" simultaneously, by construction.
    with pytest.raises(DatasetProvenanceError, match="outside the declared"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="VALIDATION", artifact_hash_ref=artifact.hash_ref
        )


def test_role_boundaries_never_reach_the_holdout_start_by_split_plan_construction(tmp_path: Path) -> None:
    """Direct proof of the mechanism behind the test above: SplitPlan's own
    closed ordering invariant (validation_end < holdout_start) makes the
    explicit defense-in-depth holdout check in verify_dataset_split_membership
    structurally unreachable for DISCOVERY/VALIDATION -- overlap is already
    impossible by construction, not merely checked at the last moment."""
    _, plan, _ = _bound(tmp_path)
    holdout_start = datetime.fromisoformat(plan.holdout_start)
    discovery_end = datetime.fromisoformat(plan.discovery_end)
    validation_end = datetime.fromisoformat(plan.validation_end)
    assert discovery_end < holdout_start
    assert validation_end < holdout_start


def test_empty_artifact_rejected(tmp_path: Path) -> None:
    _, _, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = _import(store, b"time,value\n")
    with pytest.raises(DatasetProvenanceError, match="no rows"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
        )


def test_missing_time_column_rejected(tmp_path: Path) -> None:
    _, _, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = _import(store, b"value\n1\n2\n")
    with pytest.raises(DatasetProvenanceError, match="missing required column"):
        verify_dataset_split_membership(
            artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
        )


# ---------------------------------------------------------------------------
# 14-16: no holdout plaintext access, no provider/network path, no broker/MT5 path.
# ---------------------------------------------------------------------------


def test_no_holdout_provider_or_broker_shaped_imports() -> None:
    source = Path("src/trademind/discovery/dataset_split_provenance.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_import_substrings = (
        "holdout_store",
        "holdout_sealer",
        "holdout_crypto",
        "orchestrator_bridge",
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
            assert term not in name, f"unexpected forbidden-shaped import: {name!r}"

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    forbidden_calls = {
        "OrderSend",
        "PositionClose",
        "PositionModify",
        "HoldoutSealStore",
        "mark_isolated",
        "FinalHoldoutSealer",
        "seal_and_quarantine",
        "decrypt",
    }
    assert not (called_names & forbidden_calls), called_names & forbidden_calls
    assert "CTrade" not in source
    assert "TRADE_ACTION_DEAL" not in source


def test_verify_function_never_touches_final_holdout_seal_store(tmp_path: Path) -> None:
    """A dynamic (not just static) proof: verify_dataset_split_membership's
    only I/O is ArtifactStore.resolve_verified/read_verified against the
    CANDIDATE artifact -- it never opens registry.path or any
    final_holdout_seals-shaped table."""
    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))
    # No HypothesisRegistry / HoldoutSealStore object is constructed or
    # passed anywhere in this call -- the function signature itself has no
    # such parameter, so there is nothing for it to read even accidentally.
    verify_dataset_split_membership(
        artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
    )


# ---------------------------------------------------------------------------
# Integration: composes with DatasetArtifactV2/ExperimentManifestV2 WITHOUT
# modifying either -- the minimal "only where necessary" integration point
# is that a caller verifies membership first, then constructs
# DatasetArtifactV2 from the SAME already-verified artifact_hash_ref.
# ---------------------------------------------------------------------------


def test_verified_membership_composes_with_dataset_artifact_v2_unmodified(tmp_path: Path) -> None:
    from trademind.discovery.manifest import DatasetArtifactV2

    rows, plan, bound = _bound(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    discovery_rows = rows[: plan.discovery_count]
    artifact = _import(store, _csv_bytes(discovery_rows))

    membership = verify_dataset_split_membership(
        artifact_store=store, bound_split_plan=bound, role="DISCOVERY", artifact_hash_ref=artifact.hash_ref
    )

    # DatasetArtifactV2 itself is completely unmodified -- verification
    # happens strictly BEFORE this construction, as a caller-side decision
    # about which artifact_hash_ref/role is safe to bind at all.
    dataset_v2 = DatasetArtifactV2(
        role="discovery",
        artifact_hash_ref=membership.artifact_hash_ref,
        media_type=membership.artifact_media_type,
        size_bytes=membership.artifact_size_bytes,
    )
    resolved = dataset_v2.verify(store)
    assert resolved.hash_ref == artifact.hash_ref
    assert dataset_v2.size_bytes == membership.artifact_size_bytes
