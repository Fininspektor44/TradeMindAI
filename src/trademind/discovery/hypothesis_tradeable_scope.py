"""Hypothesis -> Tradeable Scope Binding V1: an explicit, immutable,
machine-verifiable contract that binds a research hypothesis to the LIVE
CANDIDATE SCOPE it is allowed to match.

This module solves exactly one problem: ``hypothesis -> allowed live
candidate scope`` (symbol, timeframe, setup-family, allowed action scope).
It does NOT create a ``SignalCandidate``, invent current market data, invent
an entry/SL/TP, authorize trading, call ``risk_manager``, call MT5, or call
any broker/network path. ``HypothesisTradeableScopeV1`` is an ALLOWABLE
SCOPE, not a live trade instruction: presenting it, or even a future exact
match against it, is not authorization to trade -- it only proves that a
live candidate's identity falls within the scope this hypothesis was
proposed for. A separate, explicit SER8 live-authorization/risk gate (not
built here) governs whether, and how, any live action is ever taken; this
module supplies one INPUT that gate would need, nothing more.

SOURCE OF SCOPE: symbol, timeframe, and setup-family (``feature``) are
sourced deterministically from the hypothesis's own already-frozen,
already-immutable ``ExperimentManifestV2.proposal_provenance`` -- by
resolving its ``packet_artifact_hash_ref`` through the SAME Verified-CAS
primitives (``load_packet_v2``, ``load_report_v2``) the research proposal
pipeline already trusts, down to the exact ``CandidateContentV2`` this
hypothesis was proposed from. Nothing is invented, inferred, or textually
matched: every field is either re-verified CAS content or a cross-checked
identity.

Two deliberately separate layers enforce this. ``HypothesisTradeableScopeV1``
itself validates only STRUCTURE (``allowed_action_scope`` must be BUY, SELL,
or BOTH; every identity/hash field must be well-formed) -- it does not know
or care where its fields came from, so it stays a general, honestly-testable
identity contract. :func:`bind_hypothesis_tradeable_scope` owns the TRUST
policy for deriving that structure from research provenance: the candidate's
``action_scope`` vocabulary is deliberately narrow and closed. Originally an
exhaustive repository-wide grep found ``action_scope`` used with exactly one
literal value everywhere in this codebase, ``"BUY_SELL_DIRECTIONAL"``, mapped
to :attr:`AllowedActionScope.BOTH`. ``scripts/bootstrap_first_real_hypothesis.py``
later introduced a SECOND, genuinely distinct, exact one-to-one source: it
sets ``CandidateDefinitionV2.action_scope`` directly to the SAME literal
directional value (``"BUY"`` or ``"SELL"``) its own ``--action-scope`` CLI
argument already uses to filter live candidates by ``SignalCandidate.plan.
action`` -- i.e. ``signal_intelligence.VALID_ACTIONS``'s exact vocabulary,
not a new or invented one. Both source values -- ``"BUY"`` and ``"SELL"`` --
therefore map one-to-one, with NO broadening, to the identically-named
:attr:`AllowedActionScope.BUY`/:attr:`AllowedActionScope.SELL` members this
dataclass already validated and
:func:`trademind.hypothesis_live_candidate_matching.verify_live_candidate_matches_scope`
already enforced exactly (a BUY-scoped hypothesis never matched a SELL
candidate, or vice versa, even before this mapping entry existed -- only the
BUILDER's source-value recognition was the gap). Any OTHER source value
still fails closed here rather than being silently interpreted, broadened,
or narrowed by this builder.

IMMUTABILITY: this module owns no new SQLite table, no new mutable state,
and no "binding event" to persist. A hypothesis's manifest binding is
already immutable once frozen (``HypothesisRegistry`` enforces
``manifest_hash is immutable after freeze``), and every CAS reference this
module resolves through (packet, report, candidate) is itself
content-addressed and immutable. Calling :func:`bind_hypothesis_tradeable_scope`
for the same hypothesis at any point from FROZEN onward -- including after
ACCEPTED, REJECTED_FINAL, or any state in between -- deterministically
recomputes the byte-identical scope (see :meth:`HypothesisTradeableScopeV1.
semantic_projection`, which excludes only the wall-clock ``bound_at``
diagnostic field from the hash). There is nothing to overwrite, no race to
guard, and nothing that could conflict.

This module is independent of ``HypothesisState.ACCEPTED``: a scope binding
answers "what live candidate identity was this hypothesis proposed for",
which is a proposal-time fact available for any hypothesis that has been
frozen, regardless of whether it later reaches ACCEPTED or REJECTED_FINAL.
Whether a hypothesis is ALLOWED to influence a live decision remains a
strictly separate question owned by
``trademind.discovery.research_eligibility_boundary`` (not touched here).

``HypothesisRegistry``, ``FinalVerdictAcceptanceControl``,
``ExperimentManifestV2``, and every other closed module this binder reads
from are never modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping

from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.signal_statistics_agent_packet import load_packet_v2
from trademind.signal_statistics_provenance import (
    canonical_json_bytes,
    sha256_bytes,
    validate_sha256_ref,
)
from trademind.signal_statistics_report import load_report_v2

from .hypothesis_registry import HypothesisRegistry
from .manifest import EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION, load_experiment_manifest_v2

SCHEMA_VERSION = "discovery-hypothesis-tradeable-scope-v1"
_SCOPE_HASH_DOMAIN = b"trademind:discovery:hypothesis-tradeable-scope:v1"

# Explicit, closed, one-to-one mapping from every source ``action_scope``
# value this codebase actually produces to the ``AllowedActionScope`` it
# means -- never inferred, never broadened, never derived from symbol/
# setup-family/candidate/runtime state. Adding a new entry here is the ONLY
# change this module's own docstring anticipates for a genuinely new source
# vocabulary; an unrecognized value still fails closed unconditionally.
#
#   "BUY_SELL_DIRECTIONAL" -> BOTH
#       The original, still-current source value from the LLM-driven
#       research-proposal pipeline (signal_statistics_report.py's own
#       candidate-definition builder) -- a directional-but-unrestricted
#       statistical test scope, verified by exhaustive repository-wide grep
#       to be the only value that pipeline has ever produced.
#   "BUY"  -> BUY   (exact match, never broadened to BOTH or to SELL)
#   "SELL" -> SELL  (exact match, never broadened to BOTH or to BUY)
#       scripts/bootstrap_first_real_hypothesis.py's own
#       ``CandidateDefinitionV2.action_scope`` is set directly to the
#       identical literal value its ``--action-scope`` CLI argument already
#       uses to filter live candidates by ``SignalCandidate.plan.action``
#       (``signal_intelligence.VALID_ACTIONS`` = {"BUY", "SELL"}) -- an
#       exact, pre-existing one-to-one vocabulary match, not a new or
#       inferred category.
_SOURCE_ACTION_SCOPE_MAP: Mapping[str, str] = {
    "BUY_SELL_DIRECTIONAL": "BOTH",
    "BUY": "BUY",
    "SELL": "SELL",
}


class HypothesisTradeableScopeError(RuntimeError):
    """Raised when a hypothesis's tradeable scope cannot be bound or
    independently reverified, or when a scope payload fails validation."""


class AllowedActionScope(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    BOTH = "BOTH"


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise HypothesisTradeableScopeError(f"{field_name} must be a non-empty string")
    return value


def _bare_sha256(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HypothesisTradeableScopeError(f"{field_name} must be 64 lowercase hexadecimal digits")
    return value


def _candidate_id(value: object, *, field_name: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("ssc-v2-")
        or len(value) != len("ssc-v2-") + 64
        or any(character not in "0123456789abcdef" for character in value[len("ssc-v2-") :])
    ):
        raise HypothesisTradeableScopeError(f"{field_name} is not a Candidate Definition v2 identity")
    return value


@dataclass(frozen=True, slots=True)
class HypothesisTradeableScopeV1:
    """Immutable, deterministic proof of the live candidate identity scope
    one hypothesis was proposed for. Construct only via
    :func:`bind_hypothesis_tradeable_scope` -- never by hand.

    This is an ALLOWABLE SCOPE, not a live trade instruction: it deliberately
    excludes every field a live trade would need (current entry, current
    SL/TP, position size, broker account, order type, execution
    authorization). A future, independently fresh ``SignalCandidate`` must
    still be matched against this scope by
    :func:`verify_live_candidate_matches_scope`, and passing that match is
    still not, by itself, authorization to trade.
    """

    hypothesis_id: str
    hypothesis_family_id: str
    bound_hypothesis_content_hash: str
    manifest_semantic_hash: str
    manifest_artifact_hash_ref: str
    symbol: str
    timeframe: str
    setup_family: str
    allowed_action_scope: str
    source_action_scope: str
    source_candidate_id: str
    source_candidate_content_hash: str
    source_packet_artifact_hash_ref: str
    source_packet_semantic_hash: str
    source_report_artifact_hash_ref: str
    bound_at: str
    schema_version: str = SCHEMA_VERSION
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise HypothesisTradeableScopeError("unsupported hypothesis tradeable scope schema_version")
        _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        _nonempty_str(self.hypothesis_family_id, field_name="hypothesis_family_id")
        _bare_sha256(self.bound_hypothesis_content_hash, field_name="bound_hypothesis_content_hash")
        for value, field_name in (
            (self.manifest_semantic_hash, "manifest_semantic_hash"),
            (self.manifest_artifact_hash_ref, "manifest_artifact_hash_ref"),
            (self.source_candidate_content_hash, "source_candidate_content_hash"),
            (self.source_packet_artifact_hash_ref, "source_packet_artifact_hash_ref"),
            (self.source_packet_semantic_hash, "source_packet_semantic_hash"),
            (self.source_report_artifact_hash_ref, "source_report_artifact_hash_ref"),
        ):
            try:
                validate_sha256_ref(value)
            except Exception as exc:  # noqa: BLE001 -- normalized to one error type below.
                raise HypothesisTradeableScopeError(f"{field_name} is invalid") from exc
        _candidate_id(self.source_candidate_id, field_name="source_candidate_id")

        symbol = _nonempty_str(self.symbol, field_name="symbol").strip().upper()
        timeframe = _nonempty_str(self.timeframe, field_name="timeframe").strip().upper()
        _nonempty_str(self.setup_family, field_name="setup_family")
        _nonempty_str(self.source_action_scope, field_name="source_action_scope")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)

        if self.allowed_action_scope not in {scope.value for scope in AllowedActionScope}:
            raise HypothesisTradeableScopeError(
                "allowed_action_scope must be BUY, SELL, or BOTH"
            )
        if type(self.bound_at) is not str:
            raise HypothesisTradeableScopeError("bound_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.bound_at)
        except ValueError as exc:
            raise HypothesisTradeableScopeError("bound_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HypothesisTradeableScopeError("bound_at must be timezone-aware")

        object.__setattr__(
            self,
            "scope_hash",
            sha256_bytes(_SCOPE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Excludes ``bound_at`` (wall-clock, non-deterministic) so binding
        the same hypothesis's scope repeatedly always produces an identical
        ``scope_hash``."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "bound_hypothesis_content_hash": self.bound_hypothesis_content_hash,
            "manifest_semantic_hash": self.manifest_semantic_hash,
            "manifest_artifact_hash_ref": self.manifest_artifact_hash_ref,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "setup_family": self.setup_family,
            "allowed_action_scope": self.allowed_action_scope,
            "source_action_scope": self.source_action_scope,
            "source_candidate_id": self.source_candidate_id,
            "source_candidate_content_hash": self.source_candidate_content_hash,
            "source_packet_artifact_hash_ref": self.source_packet_artifact_hash_ref,
            "source_packet_semantic_hash": self.source_packet_semantic_hash,
            "source_report_artifact_hash_ref": self.source_report_artifact_hash_ref,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["scope_hash"] = self.scope_hash
        payload["diagnostics"] = {"bound_at": self.bound_at}
        return payload


def _load_verified_manifest(hypothesis_id: str, *, registry: HypothesisRegistry, artifact_store: ArtifactStore):
    """Load and re-verify the V2 manifest bound to ``hypothesis_id``,
    regardless of how far its state has legitimately advanced past FROZEN.

    Deliberately does not reuse ``HypothesisRegistry.load_bound_manifest_v2``
    (that helper hardcodes a FROZEN-only precondition) or any other module's
    private accessor. Uses only the public ``load_experiment_manifest_v2``
    CAS primitive and re-implements the identical identity cross-checks --
    the same pattern ``ValidationExecutionControl._load_verified_manifest_
    past_frozen`` and ``research_eligibility_boundary.present_eligible_
    artifact`` already establish, kept local here to avoid a dependency on
    either of those classes.
    """
    try:
        record = registry.get(hypothesis_id)
    except KeyError as exc:
        raise HypothesisTradeableScopeError(f"hypothesis does not exist: {exc}") from exc
    if (
        record.manifest_hash is None
        or record.manifest_artifact_hash_ref is None
        or record.manifest_schema_version != EXPERIMENT_MANIFEST_V2_SCHEMA_VERSION
    ):
        raise HypothesisTradeableScopeError(
            "hypothesis has no complete manifest v2 binding; it must be at least FROZEN"
        )
    try:
        manifest = load_experiment_manifest_v2(
            record.manifest_artifact_hash_ref, artifact_store=artifact_store
        )
    except Exception as exc:  # noqa: BLE001 -- CAS content is untrusted input.
        raise HypothesisTradeableScopeError(f"bound manifest v2 could not be verified: {exc}") from exc
    if (
        manifest.hypothesis_id != record.hypothesis_id
        or manifest.hypothesis_family_id != record.hypothesis_family_id
        or manifest.bound_hypothesis_content_hash != record.content_hash
        or manifest.manifest_semantic_hash.removeprefix("sha256:") != record.manifest_hash
    ):
        raise HypothesisTradeableScopeError(
            "manifest v2 identity does not match this hypothesis's current registry binding"
        )
    return record, manifest


def bind_hypothesis_tradeable_scope(
    hypothesis_id: str,
    *,
    registry: HypothesisRegistry,
    artifact_store: ArtifactStore,
) -> HypothesisTradeableScopeV1:
    """Deterministically bind ``hypothesis_id`` to the live candidate scope
    it was proposed for, sourced entirely from its already-frozen,
    already-immutable manifest provenance chain.

    Fails closed -- raises :class:`HypothesisTradeableScopeError` -- if the
    hypothesis has no manifest binding yet (state PROPOSED), if any step of
    its proposal/packet/report/candidate provenance chain cannot be
    independently re-verified, if the packet does not contain the exact
    candidate this hypothesis was proposed from, if the report's full
    candidate content does not match the packet's own redacted binding for
    it, or if the candidate's ``action_scope`` is not one of this module's
    explicit, recognized source values. Never invents, infers, or broadens
    symbol/timeframe/action-scope/setup-family beyond what the verified
    provenance chain itself states.
    """
    record, manifest = _load_verified_manifest(
        hypothesis_id, registry=registry, artifact_store=artifact_store
    )
    provenance = manifest.proposal_provenance

    try:
        packet = load_packet_v2(provenance.packet_artifact_hash_ref, artifact_store=artifact_store)
    except Exception as exc:  # noqa: BLE001 -- CAS content is untrusted input.
        raise HypothesisTradeableScopeError(f"proposal packet could not be verified: {exc}") from exc
    if packet.packet_semantic_hash != provenance.packet_semantic_hash:
        raise HypothesisTradeableScopeError(
            "verified packet semantic hash does not match this hypothesis's proposal provenance"
        )

    binding = next(
        (item for item in packet.candidate_bindings if item["candidate_id"] == provenance.candidate_id),
        None,
    )
    if binding is None:
        raise HypothesisTradeableScopeError(
            "this hypothesis's proposal candidate_id is not present in its own verified packet"
        )

    try:
        report = load_report_v2(packet.report_artifact_hash_ref, artifact_store=artifact_store)
    except Exception as exc:  # noqa: BLE001 -- CAS content is untrusted input.
        raise HypothesisTradeableScopeError(f"proposal report could not be verified: {exc}") from exc

    candidate = next(
        (item for item in report.candidates if item.candidate_id == provenance.candidate_id),
        None,
    )
    if candidate is None:
        raise HypothesisTradeableScopeError(
            "this hypothesis's proposal candidate_id is not present in its own verified report"
        )

    definition = candidate.candidate_definition
    if (
        binding["candidate_content_hash"] != candidate.content_hash
        or binding["symbol"] != definition.symbol
        or binding["timeframe"] != definition.timeframe
        or binding["feature"] != definition.feature
        or binding["horizon"] != definition.horizon
    ):
        raise HypothesisTradeableScopeError(
            "verified packet candidate binding does not match the verified report's candidate content"
        )

    allowed_action_scope = _SOURCE_ACTION_SCOPE_MAP.get(definition.action_scope)
    if allowed_action_scope is None:
        raise HypothesisTradeableScopeError(
            f"unrecognized source action_scope {definition.action_scope!r}; "
            "refusing to infer or broaden a tradeable scope from it"
        )

    return HypothesisTradeableScopeV1(
        hypothesis_id=record.hypothesis_id,
        hypothesis_family_id=record.hypothesis_family_id,
        bound_hypothesis_content_hash=record.content_hash,
        manifest_semantic_hash=manifest.manifest_semantic_hash,
        manifest_artifact_hash_ref=record.manifest_artifact_hash_ref,
        symbol=definition.symbol,
        timeframe=definition.timeframe,
        setup_family=definition.feature,
        allowed_action_scope=allowed_action_scope,
        source_action_scope=definition.action_scope,
        source_candidate_id=candidate.candidate_id,
        source_candidate_content_hash=candidate.content_hash,
        source_packet_artifact_hash_ref=provenance.packet_artifact_hash_ref,
        source_packet_semantic_hash=provenance.packet_semantic_hash,
        source_report_artifact_hash_ref=packet.report_artifact_hash_ref,
        bound_at=datetime.now(timezone.utc).isoformat(),
    )


__all__ = [
    "SCHEMA_VERSION",
    "AllowedActionScope",
    "HypothesisTradeableScopeError",
    "HypothesisTradeableScopeV1",
    "bind_hypothesis_tradeable_scope",
]
