"""Protective bridge from frozen Discovery hypotheses into Orchestrator tasks.

The bridge is deliberately narrow: it may submit work for a hypothesis only after
its manifest is frozen and externally anchored in the hypothesis registry. Raw
dataset paths are used locally for verification but are never exposed in the task
envelope or research brief, because the source artifact may contain final holdout.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.orchestrator.control_plane import ControlPlane
from trademind.orchestrator.models import RiskClass, Task

from .hypothesis_registry import HypothesisRegistry, HypothesisState
from .manifest import ManifestIntegrityError, verify_frozen_manifest


class DiscoveryBridgeError(RuntimeError):
    """Raised when a Discovery hypothesis cannot safely enter orchestration."""


@dataclass(frozen=True, slots=True)
class DiscoveryTaskBinding:
    hypothesis_id: str
    hypothesis_family_id: str
    manifest_hash: str
    orchestrator_task_id: str


@dataclass(frozen=True, slots=True)
class ValidatedFrozenHypothesis:
    binding: DiscoveryTaskBinding
    manifest_payload: dict


class DiscoveryOrchestratorBridge:
    """Validate a frozen hypothesis and create one bounded Orchestrator task."""

    def __init__(
        self,
        *,
        registry: HypothesisRegistry,
        control: ControlPlane,
        artifacts: ArtifactStore,
    ) -> None:
        self.registry = registry
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

    def submit_frozen_hypothesis(
        self,
        hypothesis_id: str,
        *,
        manifest_path: str | Path,
        budget_limit: float = 0.0,
        priority: int = 0,
    ) -> Task:
        """Create exactly one Orchestrator task for a verified frozen hypothesis.

        A zero budget is intentional and safe by default. Enabling paid AI calls
        requires the caller to opt in by passing a positive task budget.
        """
        if budget_limit < 0:
            raise ValueError("budget_limit must not be negative")

        validated = self.validate_frozen_hypothesis(
            hypothesis_id,
            manifest_path=manifest_path,
        )
        binding = validated.binding
        if self.control.task_store.get(binding.orchestrator_task_id) is not None:
            raise DiscoveryBridgeError("frozen hypothesis already has an Orchestrator task")

        brief = self.artifacts.store_json(
            task_id=binding.orchestrator_task_id,
            revision=1,
            kind="research-brief",
            payload=self._safe_research_brief(validated),
        )

        task = Task.new(
            task_id=binding.orchestrator_task_id,
            goal=(
                "Implement and validate the frozen Discovery hypothesis using only "
                "discovery and validation data. Final-holdout access is forbidden."
            ),
            scope=("src/trademind/discovery",),
            risk_class=RiskClass.LOW,
            budget_limit=budget_limit,
            acceptance_criteria=(
                f"manifest_sha256={binding.manifest_hash}",
                f"hypothesis_family_id={binding.hypothesis_family_id}",
                "research brief contains no raw dataset paths",
                "frozen manifest and dataset hashes remain valid",
                "no final-holdout data is read, returned, or consumed",
                "no broker order or live trading side effect is allowed",
            ),
            priority=priority,
        )
        task = replace(task, artifact_refs=(brief.hash_ref,))
        return self.control.create_task(task)
