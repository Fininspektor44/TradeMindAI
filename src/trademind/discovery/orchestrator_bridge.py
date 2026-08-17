"""Protective bridge from frozen Discovery hypotheses into Orchestrator tasks.

The bridge is deliberately narrow: it may submit work for a hypothesis only after
its manifest is frozen and externally anchored, and after the final holdout has a
persisted plaintext-isolation attestation. Raw dataset paths are used locally for
deterministic integrity verification but are never exposed in the task envelope or
research brief.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import RiskClass, Task

from .dataset_split_provenance import (
    BoundSplitPlanV1,
    DatasetProvenanceError,
    DatasetSplitMembershipV1,
    verify_dataset_split_membership,
)
from .holdout_store import HoldoutSealStore
from .hypothesis_registry import HypothesisRegistry, HypothesisState, RegistryError
from .manifest import ExperimentManifestV2, ManifestIntegrityError, verify_frozen_manifest

# RFC 2045 media-type shape (type/subtype), the ONLY value the V2 research
# brief legitimately contains a "/" in (DatasetArtifactV2.media_type).
_MEDIA_TYPE_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&.+^_-]*/[A-Za-z0-9][A-Za-z0-9!#$&.+^_-]*$")


class DiscoveryBridgeError(RuntimeError):
    """Raised when a Discovery hypothesis cannot safely enter orchestration."""


@dataclass(frozen=True, slots=True)
class DiscoveryTaskBinding:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_hash: str
    holdout_isolation_receipt_hash: str
    orchestrator_task_id: str


@dataclass(frozen=True, slots=True)
class ValidatedFrozenHypothesis:
    binding: DiscoveryTaskBinding
    manifest_payload: dict


@dataclass(frozen=True, slots=True)
class DiscoveryTaskBindingV2:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    bound_split_plan_hash: str
    dataset_memberships: tuple[DatasetSplitMembershipV1, ...]
    orchestrator_task_id: str


@dataclass(frozen=True, slots=True)
class ValidatedFrozenHypothesisV2:
    binding: DiscoveryTaskBindingV2
    manifest: ExperimentManifestV2


class DiscoveryOrchestratorBridge:
    """Validate a frozen hypothesis and create one bounded Orchestrator task."""

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        holdout_seals: HoldoutSealStore,
        control: ControlPlane,
        artifacts: ArtifactStore,
    ) -> None:
        self.registry = registry
        self.holdout_seals = holdout_seals
        self.control = control
        self.artifacts = artifacts

    @staticmethod
    def _task_id(hypothesis_id: str) -> str:
        digest = hashlib.sha256(hypothesis_id.encode("utf-8")).hexdigest()[:20]
        return f"discovery-{digest}"

    def validate_frozen_hypothesis(
        self,
        hypothesis_id: str,
        *,
        manifest_path: str | Path,
    ) -> ValidatedFrozenHypothesis:
        record = self.registry.get(hypothesis_id)
        if record.state is not HypothesisState.FROZEN:
            raise DiscoveryBridgeError(
                f"hypothesis must be FROZEN before orchestration, got {record.state.value}"
            )
        if record.manifest_hash is None:
            raise DiscoveryBridgeError("frozen hypothesis is missing manifest_hash")

        family = self.registry.family_status(record.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise DiscoveryBridgeError("final holdout has already been consumed for this family")
        if family["terminal_state"]:
            raise DiscoveryBridgeError(
                f"hypothesis family is terminal: {family['terminal_state']}"
            )

        try:
            holdout = self.holdout_seals.get(hypothesis_id)
        except KeyError as exc:
            raise DiscoveryBridgeError(
                "frozen hypothesis has no registered protected final holdout"
            ) from exc
        if not holdout.isolated or holdout.isolation_receipt_hash is None:
            raise DiscoveryBridgeError(
                "final holdout plaintext isolation must be attested before orchestration"
            )
        if holdout.hypothesis_family_id != record.hypothesis_family_id:
            raise DiscoveryBridgeError("protected holdout family does not match hypothesis")
        if holdout.manifest_hash != record.manifest_hash:
            raise DiscoveryBridgeError("protected holdout manifest does not match hypothesis")

        try:
            document = verify_frozen_manifest(
                manifest_path,
                expected_manifest_hash=record.manifest_hash,
                verify_datasets=True,
            )
        except (ManifestIntegrityError, OSError, ValueError) as exc:
            raise DiscoveryBridgeError("frozen manifest verification failed") from exc

        payload = document["manifest"]
        if payload.get("hypothesis_id") != record.hypothesis_id:
            raise DiscoveryBridgeError("manifest hypothesis_id does not match registry")
        if payload.get("hypothesis_family_id") != record.hypothesis_family_id:
            raise DiscoveryBridgeError("manifest family identity does not match registry")
        if payload.get("content_hash") != record.content_hash:
            raise DiscoveryBridgeError("manifest content identity does not match registry")

        binding = DiscoveryTaskBinding(
            hypothesis_id=record.hypothesis_id,
            hypothesis_family_id=record.hypothesis_family_id,
            manifest_hash=record.manifest_hash,
            holdout_isolation_receipt_hash=holdout.isolation_receipt_hash,
            orchestrator_task_id=self._task_id(record.hypothesis_id),
        )
        return ValidatedFrozenHypothesis(binding=binding, manifest_payload=dict(payload))

    @staticmethod
    def _safe_research_brief(validated: ValidatedFrozenHypothesis) -> dict:
        payload = validated.manifest_payload
        return {
            "schema_version": "discovery-orchestrator-bridge-v1",
            "hypothesis_id": validated.binding.hypothesis_id,
            "hypothesis_family_id": validated.binding.hypothesis_family_id,
            "content_hash": payload["content_hash"],
            "manifest_hash": validated.binding.manifest_hash,
            "holdout_isolation_receipt_hash": validated.binding.holdout_isolation_receipt_hash,
            "family_definition": payload["family_definition"],
            "parameters": payload["parameters"],
            "test_family": payload["test_family"],
            "primary_metric": payload["primary_metric"],
            "alpha": payload["alpha"],
            "q": payload["q"],
            "minimum_effect_size": payload["minimum_effect_size"],
            "max_hypotheses_tests": payload["max_hypotheses_tests"],
            "manifest_schema_version": payload["schema_version"],
            "git_commit": payload["git_commit"],
        }

    @staticmethod
    def _iter_strings(value: Any):
        if isinstance(value, str):
            yield value
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(key, str):
                    yield key
                yield from DiscoveryOrchestratorBridge._iter_strings(nested)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                yield from DiscoveryOrchestratorBridge._iter_strings(nested)

    @staticmethod
    def _normalize_path_text(value: str) -> str:
        return value.replace("\\", "/").casefold()

    @classmethod
    def _assert_brief_has_no_raw_paths(
        cls,
        validated: ValidatedFrozenHypothesis,
        *,
        manifest_path: str | Path,
    ) -> dict:
        brief = cls._safe_research_brief(validated)
        forbidden = {str(Path(manifest_path)), str(Path(manifest_path).resolve())}
        datasets = validated.manifest_payload.get("datasets", [])
        if isinstance(datasets, list):
            for raw in datasets:
                if isinstance(raw, dict) and raw.get("file_path"):
                    raw_path = str(raw["file_path"])
                    forbidden.add(raw_path)
                    forbidden.add(str(Path(raw_path).resolve()))

        normalized_forbidden = {
            cls._normalize_path_text(path)
            for path in forbidden
            if path
        }
        for text in cls._iter_strings(brief):
            normalized_text = cls._normalize_path_text(text)
            if any(path in normalized_text for path in normalized_forbidden):
                raise DiscoveryBridgeError(
                    "research brief metadata contains a raw artifact path"
                )
        return brief

    def submit_frozen_hypothesis(
        self,
        hypothesis_id: str,
        *,
        manifest_path: str | Path,
        budget_limit: float = 0.0,
        priority: int = 0,
    ) -> Task:
        """Create exactly one Orchestrator task for a verified frozen hypothesis."""
        if budget_limit < 0:
            raise ValueError("budget_limit must not be negative")

        validated = self.validate_frozen_hypothesis(
            hypothesis_id,
            manifest_path=manifest_path,
        )
        binding = validated.binding
        if self.control.task_store.get(binding.orchestrator_task_id) is not None:
            raise DiscoveryBridgeError("frozen hypothesis already has an Orchestrator task")

        brief_payload = self._assert_brief_has_no_raw_paths(
            validated,
            manifest_path=manifest_path,
        )
        brief = self.artifacts.store_json(
            task_id=binding.orchestrator_task_id,
            revision=1,
            kind="research-brief",
            payload=brief_payload,
        )

        task = Task.new(
            task_id=binding.orchestrator_task_id,
            goal=(
                "Prepare bounded implementation and validation work for the frozen Discovery "
                "hypothesis. Final-holdout exposure to Orchestrator agents is forbidden."
            ),
            scope=("src/trademind/discovery",),
            risk_class=RiskClass.LOW,
            allowed_tools=("orchestrator-tests",),
            budget_limit=budget_limit,
            acceptance_criteria=(
                f"manifest_sha256={binding.manifest_hash}",
                f"hypothesis_family_id={binding.hypothesis_family_id}",
                f"holdout_isolation_sha256={binding.holdout_isolation_receipt_hash}",
                "research brief contains no raw manifest or dataset paths",
                "frozen manifest and dataset hashes remain valid",
                "no final-holdout rows are exposed to Orchestrator agents or consumed by lifecycle",
                "no broker order or live trading side effect is allowed",
            ),
            priority=priority,
        )
        task = replace(task, artifact_refs=(brief.hash_ref,))
        return self.control.create_task(task)

    # -----------------------------------------------------------------
    # V2: additive, CAS-based path. Does not replace or weaken the legacy
    # (file-path-based) methods above; a hypothesis frozen through the
    # legacy path has no manifest_artifact_hash_ref/manifest_schema_version
    # binding and cannot pass validate_frozen_hypothesis_v2, and one frozen
    # through the V2 path (HypothesisRegistry.freeze_manifest_v2_in_
    # transaction) cannot satisfy the legacy path's manifest_hash-only
    # freeze() precondition either -- the two are mutually exclusive by the
    # registry's own one-way FROZEN transition, never both live at once for
    # the same hypothesis_id.
    # -----------------------------------------------------------------

    def validate_frozen_hypothesis_v2(
        self,
        hypothesis_id: str,
        *,
        bound_split_plan: BoundSplitPlanV1,
        dataset_role_bindings: Mapping[str, str],
    ) -> ValidatedFrozenHypothesisV2:
        """Validate a hypothesis frozen through the V2 (ExperimentManifestV2)
        path and prove every dataset it references is genuinely DISCOVERY or
        VALIDATION content, not a caller-forged label.

        ``bound_split_plan`` is a validated caller-supplied claim binding a
        split plan to a source dataset's hash -- exactly like
        ``CodeProvenance`` elsewhere in this codebase, construction proves
        internal consistency, not that the claimed source file is genuine;
        that trust boundary is intentionally outside what a CAS-only bridge
        can independently verify without being handed raw dataset content.
        What IS independently, cryptographically re-verified here -- never
        merely trusted from a caller-supplied label or a pre-built
        ``DatasetSplitMembershipV1`` object -- is that every dataset
        artifact ``ExperimentManifestV2.datasets`` references actually
        contains, by its own row content read fresh from Verified CAS, only
        rows within its claimed DISCOVERY/VALIDATION boundary of
        ``bound_split_plan``. ``dataset_role_bindings`` maps each dataset's
        own ``DatasetArtifactV2.role`` label (a free-form manifest-internal
        name, e.g. ``"market-data"``) to the DATASET_SPLIT_ROLES value
        (``"DISCOVERY"``/``"VALIDATION"``) it is being submitted as; a
        dataset with no entry here fails closed rather than being silently
        skipped.
        """
        if type(bound_split_plan) is not BoundSplitPlanV1:
            raise DiscoveryBridgeError("bound_split_plan must be an exact BoundSplitPlanV1")
        if not isinstance(dataset_role_bindings, Mapping):
            raise DiscoveryBridgeError("dataset_role_bindings must be a mapping")

        try:
            manifest = self.registry.load_bound_manifest_v2(
                hypothesis_id, artifact_store=self.artifacts
            )
        except KeyError as exc:
            raise DiscoveryBridgeError(f"hypothesis does not exist: {exc}") from exc
        except RegistryError as exc:
            raise DiscoveryBridgeError(
                f"hypothesis is not safely bound to a verified V2 manifest: {exc}"
            ) from exc

        record = self.registry.get(manifest.hypothesis_id)
        family = self.registry.family_status(record.hypothesis_family_id)
        if family["holdout_consumed"]:
            raise DiscoveryBridgeError("final holdout has already been consumed for this family")
        if family["terminal_state"]:
            raise DiscoveryBridgeError(
                f"hypothesis family is terminal: {family['terminal_state']}"
            )

        # Same protective bar the legacy V1 path already requires -- V2 does
        # not weaken it: no Orchestrator work begins before the family's
        # final holdout has a persisted, attested plaintext-isolation
        # receipt. This never reads holdout content, only the isolation
        # attestation metadata already established by holdout_store.py.
        try:
            holdout = self.holdout_seals.get(manifest.hypothesis_id)
        except KeyError as exc:
            raise DiscoveryBridgeError(
                "frozen hypothesis has no registered protected final holdout"
            ) from exc
        if not holdout.isolated or holdout.isolation_receipt_hash is None:
            raise DiscoveryBridgeError(
                "final holdout plaintext isolation must be attested before orchestration"
            )
        if holdout.hypothesis_family_id != record.hypothesis_family_id:
            raise DiscoveryBridgeError("protected holdout family does not match hypothesis")
        if holdout.manifest_hash != record.manifest_hash:
            raise DiscoveryBridgeError("protected holdout manifest does not match hypothesis")

        memberships: list[DatasetSplitMembershipV1] = []
        for dataset in manifest.datasets:
            role = dataset_role_bindings.get(dataset.role)
            if role is None:
                raise DiscoveryBridgeError(
                    f"dataset role {dataset.role!r} has no declared split-membership binding"
                )
            try:
                membership = verify_dataset_split_membership(
                    artifact_store=self.artifacts,
                    bound_split_plan=bound_split_plan,
                    role=role,
                    artifact_hash_ref=dataset.artifact_hash_ref,
                    media_type=dataset.media_type,
                )
            except DatasetProvenanceError as exc:
                raise DiscoveryBridgeError(
                    f"dataset {dataset.artifact_hash_ref} failed split-membership verification: {exc}"
                ) from exc
            memberships.append(membership)

        binding = DiscoveryTaskBindingV2(
            hypothesis_id=manifest.hypothesis_id,
            hypothesis_family_id=manifest.hypothesis_family_id,
            manifest_semantic_hash=manifest.manifest_semantic_hash,
            manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
            bound_split_plan_hash=bound_split_plan.bound_split_plan_hash,
            dataset_memberships=tuple(memberships),
            orchestrator_task_id=self._task_id(manifest.hypothesis_id),
        )
        return ValidatedFrozenHypothesisV2(binding=binding, manifest=manifest)

    @staticmethod
    def _safe_research_brief_v2(validated: ValidatedFrozenHypothesisV2) -> dict:
        manifest = validated.manifest
        binding = validated.binding
        return {
            "schema_version": "discovery-orchestrator-bridge-v2",
            "hypothesis_id": binding.hypothesis_id,
            "hypothesis_family_id": binding.hypothesis_family_id,
            "manifest_semantic_hash": binding.manifest_semantic_hash,
            "manifest_artifact_hash_ref": binding.manifest_artifact_hash_ref,
            "bound_split_plan_hash": binding.bound_split_plan_hash,
            "dataset_memberships": [item.to_payload() for item in binding.dataset_memberships],
            "test_family": manifest.test_family,
            "primary_metric": manifest.primary_metric,
            "evaluation_criteria": manifest.evaluation_criteria.to_payload(),
            "alpha": manifest.alpha,
            "q": manifest.q,
            "minimum_effect_size": manifest.minimum_effect_size,
            "max_hypotheses_tests": manifest.max_hypotheses_tests,
            "manifest_schema_version": manifest.schema_version,
        }

    @classmethod
    def _assert_brief_v2_has_no_raw_path_shaped_strings(cls, brief: dict) -> dict:
        """Defense-in-depth: ExperimentManifestV2/DatasetArtifactV2/
        BoundSplitPlanV1/DatasetSplitMembershipV1 structurally carry no raw
        filesystem path field at all (verified by
        test_v2_manifest_and_dataset_schemas_carry_no_file_path_field), so
        this brief can never legitimately contain a path separator, with one
        narrow, explicitly recognized exception: RFC 2045 media-type strings
        (``type/subtype``, exactly one ``/``, no path-shaped structure
        otherwise) that DatasetArtifactV2.media_type legitimately carries.
        Fail closed if any string looks like a real path anyway."""
        for text in cls._iter_strings(brief):
            if "\\" in text:
                raise DiscoveryBridgeError(
                    "V2 research brief metadata contains a path-shaped string"
                )
            if "/" in text and _MEDIA_TYPE_SHAPE.fullmatch(text) is None:
                raise DiscoveryBridgeError(
                    "V2 research brief metadata contains a path-shaped string"
                )
        return brief

    def submit_frozen_hypothesis_v2(
        self,
        hypothesis_id: str,
        *,
        bound_split_plan: BoundSplitPlanV1,
        dataset_role_bindings: Mapping[str, str],
        budget_limit: float = 0.0,
        priority: int = 0,
    ) -> Task:
        """Create exactly one Orchestrator task for a verified V2-frozen
        hypothesis. Additive sibling of ``submit_frozen_hypothesis``;
        neither method's behavior changes because the other exists."""
        if budget_limit < 0:
            raise ValueError("budget_limit must not be negative")

        validated = self.validate_frozen_hypothesis_v2(
            hypothesis_id,
            bound_split_plan=bound_split_plan,
            dataset_role_bindings=dataset_role_bindings,
        )
        binding = validated.binding
        if self.control.task_store.get(binding.orchestrator_task_id) is not None:
            raise DiscoveryBridgeError("frozen hypothesis already has an Orchestrator task")

        brief_payload = self._assert_brief_v2_has_no_raw_path_shaped_strings(
            self._safe_research_brief_v2(validated)
        )
        brief = self.artifacts.store_json(
            task_id=binding.orchestrator_task_id,
            revision=1,
            kind="research-brief-v2",
            payload=brief_payload,
        )

        task = Task.new(
            task_id=binding.orchestrator_task_id,
            goal=(
                "Prepare bounded implementation and validation work for the V2-frozen Discovery "
                "hypothesis. Final-holdout exposure to Orchestrator agents is forbidden."
            ),
            scope=("src/trademind/discovery",),
            risk_class=RiskClass.LOW,
            allowed_tools=("orchestrator-tests",),
            budget_limit=budget_limit,
            acceptance_criteria=(
                f"manifest_semantic_sha256={binding.manifest_semantic_hash}",
                f"hypothesis_family_id={binding.hypothesis_family_id}",
                f"bound_split_plan_sha256={binding.bound_split_plan_hash}",
                "every dataset role is machine-verified DISCOVERY or VALIDATION content",
                "research brief contains no raw dataset filesystem paths",
                "frozen manifest and dataset hashes remain valid",
                "no final-holdout rows are exposed to Orchestrator agents or consumed by lifecycle",
                "no broker order or live trading side effect is allowed",
            ),
            priority=priority,
        )
        task = replace(task, artifact_refs=(brief.hash_ref,))
        return self.control.create_task(task)
