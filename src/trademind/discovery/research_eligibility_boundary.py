"""Research -> SER8 Eligibility Boundary V1: the minimal, fail-closed seam
between the completed Discovery Engine research lifecycle and the SER8/
live-runtime lineage.

No production contract wiring ``HypothesisState.ACCEPTED`` to SER8
candidate/live-runtime eligibility existed anywhere in either lineage prior
to this module (verified by inspecting every SER8-lineage module that
touches MT5/live signal/broker paths -- ``mt5_prospective_monitor.py``,
``live_signal_runtime.py``, ``prospective_confirmation.py``,
``smc_pattern_journal_evaluator.py``, ``smc_journal_dataset_import.py``,
``fx_research.py`` -- none of them reference ``HypothesisState``,
``hypothesis_registry``, or any research-lifecycle module at all). Per the
integration task's own explicit instruction, this module does NOT invent
automatic trading authorization. It proves exactly one thing, deterministically
and fail-closed: a specific hypothesis reached the terminal ``ACCEPTED``
outcome, with its full provenance chain intact, and packages that fact as a
read-only, presentation-only artifact. It carries no broker/MT5 field, no
order-authorization flag, no risk parameter, no position size, no symbol/side
routing information -- nothing that could be mistaken for trading permission.

A separate, explicit SER8 authorization/risk gate -- not built here, and
explicitly out of this module's scope -- remains required before any order
path may ever be reached. This module cannot place an order, cannot reach a
broker, and cannot be mistaken for one: it has no import of, or call into,
any provider/network/broker/MT5-shaped code anywhere.

Only ``ACCEPTED`` may ever produce an eligibility artifact.
``REJECTED_FINAL`` and every earlier lifecycle state (``PROPOSED``,
``FROZEN``, ``TRAIN_TESTED``, ``VALIDATION_PASSED``, ``VALIDATION_REJECTED``,
``HOLDOUT_CONSUMED``) are rejected unconditionally -- this module adds its own
explicit gate rather than relying only on ``FinalVerdictAcceptanceControl.
get_evidence``'s own gate (which accepts BOTH terminal outcomes, since it
serves both), so that even a bug or a future weakening of that shared
accessor's own state check could never widen this specific,
narrower-by-design boundary without also touching this module's own explicit
verdict comparison.

Stateless and read-only: this module owns no new SQLite table and persists
nothing new. ``ResearchEligibilityArtifactV1`` is a pure, deterministic,
idempotent projection of the already-closed, already-durable
``FinalVerdictEvidenceV1`` -- calling ``present_eligible_artifact`` twice for
the same ACCEPTED hypothesis returns byte-identical output, and there is
nothing here that could conflict, double-write, or need reconciliation.
``FinalVerdictAcceptanceControl``, ``HypothesisRegistry``, and every other
closed module this boundary reads from are never modified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trademind.signal_statistics_provenance import (
    ProvenanceError,
    canonical_json_bytes,
    freeze_json_object,
    sha256_bytes,
    validate_sha256_ref,
)

from .final_verdict_control import FinalVerdictAcceptanceControl, FinalVerdictError
from .hypothesis_registry import HypothesisRegistry, HypothesisState

RESEARCH_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION = "discovery-research-eligibility-artifact-v1"
_ELIGIBILITY_HASH_DOMAIN = b"trademind:discovery:research-eligibility-artifact:v1"


class ResearchEligibilityError(RuntimeError):
    """Raised when a hypothesis is not eligible to be presented as an
    ACCEPTED research artifact, or when its provenance cannot be
    independently re-verified."""


def _bare_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ResearchEligibilityError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ResearchEligibilityError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ResearchEligibilityArtifactV1:
    """Immutable, read-only, presentation-only proof that one hypothesis
    reached the terminal ``ACCEPTED`` research outcome. Construct only via
    :func:`present_eligible_artifact` -- never by hand.

    Deliberately excludes every field a live-runtime order path would need
    (symbol, side, size, price, broker identity, risk limits) -- this
    artifact proves identity and scientific provenance only. Presenting it
    is not authorization to trade; a separate, explicit SER8 authorization/
    risk gate governs whether, and how, any live action is ever taken.
    """

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    final_verdict_evidence_hash: str
    test_family: str
    primary_metric: str
    verdict: str
    presented_at: str
    schema_version: str = RESEARCH_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION
    artifact_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION:
            raise ResearchEligibilityError("unsupported research eligibility artifact schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        for value, field_name in (
            (self.manifest_semantic_hash, "manifest_semantic_hash"),
            (self.manifest_artifact_hash_ref, "manifest_artifact_hash_ref"),
            (self.final_verdict_evidence_hash, "final_verdict_evidence_hash"),
        ):
            try:
                validate_sha256_ref(value)
            except ProvenanceError as exc:
                raise ResearchEligibilityError(f"{field_name} is invalid") from exc
        _nonempty_str(self.test_family, field_name="test_family")
        _nonempty_str(self.primary_metric, field_name="primary_metric")
        if self.verdict != HypothesisState.ACCEPTED.value:
            raise ResearchEligibilityError(
                "a research eligibility artifact may only ever represent ACCEPTED"
            )
        if type(self.presented_at) is not str:
            raise ResearchEligibilityError("presented_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.presented_at)
        except ValueError as exc:
            raise ResearchEligibilityError("presented_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ResearchEligibilityError("presented_at must be timezone-aware")

        object.__setattr__(
            self,
            "artifact_hash",
            sha256_bytes(_ELIGIBILITY_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Excludes ``presented_at`` (wall-clock, non-deterministic) so a
        repeated presentation of the same ACCEPTED hypothesis always
        produces an identical ``artifact_hash``."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "final_verdict_evidence_hash": self.final_verdict_evidence_hash,
            "test_family": self.test_family,
            "primary_metric": self.primary_metric,
            "verdict": self.verdict,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["artifact_hash"] = self.artifact_hash
        payload["diagnostics"] = {"presented_at": self.presented_at}
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ResearchEligibilityArtifactV1:
        try:
            frozen = freeze_json_object(payload, field_name="research_eligibility_artifact")
        except ProvenanceError as exc:
            raise ResearchEligibilityError(
                "research_eligibility_artifact must be strict bounded JSON"
            ) from exc
        required = frozenset(
            {
                "schema_version",
                "artifact_hash",
                "hypothesis_id",
                "hypothesis_family_id",
                "bound_hypothesis_content_hash",
                "manifest_semantic_hash",
                "manifest_artifact_hash_ref",
                "final_verdict_evidence_hash",
                "test_family",
                "primary_metric",
                "verdict",
                "diagnostics",
            }
        )
        fields = frozenset(frozen)
        missing = required - fields
        unknown = fields - required
        if missing:
            raise ResearchEligibilityError(
                f"research_eligibility_artifact is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ResearchEligibilityError(
                f"research_eligibility_artifact contains unknown fields: {', '.join(sorted(unknown))}"
            )
        diagnostics = frozen["diagnostics"]
        if not isinstance(diagnostics, Mapping) or frozenset(diagnostics) != {"presented_at"}:
            raise ResearchEligibilityError("research_eligibility_artifact.diagnostics is invalid")
        built = cls(
            hypothesis_id=frozen["hypothesis_id"],
            hypothesis_family_id=frozen["hypothesis_family_id"],
            bound_hypothesis_content_hash=frozen["bound_hypothesis_content_hash"],
            manifest_semantic_hash=frozen["manifest_semantic_hash"],
            manifest_artifact_hash_ref=frozen["manifest_artifact_hash_ref"],
            final_verdict_evidence_hash=frozen["final_verdict_evidence_hash"],
            test_family=frozen["test_family"],
            primary_metric=frozen["primary_metric"],
            verdict=frozen["verdict"],
            presented_at=diagnostics["presented_at"],
            schema_version=frozen["schema_version"],
        )
        try:
            claimed_hash = validate_sha256_ref(frozen["artifact_hash"])
        except ProvenanceError as exc:
            raise ResearchEligibilityError("research_eligibility_artifact.artifact_hash is invalid") from exc
        if claimed_hash != built.artifact_hash:
            raise ResearchEligibilityError("research_eligibility_artifact hash identity mismatch")
        return built


def present_eligible_artifact(
    hypothesis_id: str,
    *,
    registry: HypothesisRegistry,
    final_verdict: FinalVerdictAcceptanceControl,
) -> ResearchEligibilityArtifactV1:
    """Re-verify that ``hypothesis_id`` is exactly ``ACCEPTED`` and return a
    fresh, self-consistent ``ResearchEligibilityArtifactV1`` proving it.

    Fails closed -- raises ``ResearchEligibilityError`` -- for every other
    state, including ``REJECTED_FINAL`` and every intermediate research
    state. Presenting this artifact is not, and can never be, an order
    authorization: it carries no field a broker/execution path could act on,
    and this function never imports or calls anything provider/network/
    broker/MT5-shaped. A separate, explicit SER8 authorization/risk gate is
    required before any order path -- that gate is not, and must not be,
    this function.
    """
    try:
        record = registry.get(hypothesis_id)
    except KeyError as exc:
        raise ResearchEligibilityError(f"hypothesis does not exist: {exc}") from exc

    if record.state is not HypothesisState.ACCEPTED:
        raise ResearchEligibilityError(
            f"hypothesis is not eligible for SER8 presentation, state={record.state.value}; "
            "only the terminal ACCEPTED outcome may ever be presented as an eligible "
            "research artifact"
        )

    try:
        evidence = final_verdict.get_evidence(hypothesis_id)
    except FinalVerdictError as exc:
        raise ResearchEligibilityError(f"final verdict evidence could not be verified: {exc}") from exc

    if evidence.verdict != HypothesisState.ACCEPTED.value:
        raise ResearchEligibilityError(
            "final verdict evidence does not attest ACCEPTED; refusing to present"
        )
    if (
        evidence.hypothesis_id != hypothesis_id
        or evidence.hypothesis_family_id != record.hypothesis_family_id
        or evidence.bound_hypothesis_content_hash != record.content_hash
        or evidence.manifest_semantic_hash != f"sha256:{record.manifest_hash}"
        or evidence.manifest_artifact_hash_ref != record.manifest_artifact_hash_ref
    ):
        raise ResearchEligibilityError(
            "final verdict evidence identity does not match this hypothesis's current binding"
        )

    manifest = final_verdict.validator._load_verified_manifest_past_frozen(hypothesis_id)

    return ResearchEligibilityArtifactV1(
        hypothesis_id=record.hypothesis_id,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        manifest_semantic_hash=evidence.manifest_semantic_hash,
        manifest_artifact_hash_ref=evidence.manifest_artifact_hash_ref,
        final_verdict_evidence_hash=evidence.evidence_hash,
        test_family=manifest.test_family,
        primary_metric=manifest.primary_metric,
        verdict=evidence.verdict,
        presented_at=datetime.now(timezone.utc).isoformat(),
    )


__all__ = [
    "RESEARCH_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION",
    "ResearchEligibilityArtifactV1",
    "ResearchEligibilityError",
    "present_eligible_artifact",
]
