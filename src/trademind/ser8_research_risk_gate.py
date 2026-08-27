"""SER8 Research Risk Gate V1: connects an ACCEPTED research hypothesis's
verified eligibility and tradeable scope to a fresh, independently-produced
live candidate, and -- only if every identity/scope/freshness check passes
-- calls the existing, unchanged ``risk_manager.evaluate_risk`` to produce
a verified ``RiskDecision``.

This module STOPS at a verified ``RiskDecision``. It never sends an MT5
order, never calls a broker API, never modifies or closes a position, and
never grants execution authorization of any kind -- ``RiskDecision(state=
"ALLOW")`` is sizing-and-limits information only, exactly as
``risk_manager.py`` itself documents ("Sizing and limits only. Orders OFF.
Broker API not called."), and this module adds nothing that could be
mistaken for more than that.

Alongside the second cross-lineage module this repository has
(``trademind.hypothesis_live_candidate_matching``, which this module reuses
unchanged), this is the ONLY OTHER module that imports from both the
Discovery Engine research lineage and the live signal/risk lineage. It
exists specifically to compose four already-closed, already-tested
contracts -- ``present_eligible_artifact``, ``bind_hypothesis_tradeable_
scope``, ``verify_live_candidate_matches_scope``, and ``risk_manager.
evaluate_risk`` -- without modifying any of their semantics.

REQUIRED INPUT CHAIN (every check below fails closed, before
``evaluate_risk`` is ever called, unless noted otherwise):

1. hypothesis is ACCEPTED -- re-verified by calling ``present_eligible_
   artifact`` fresh (never trusting the caller-supplied ``eligibility``
   object at face value).
2. eligibility artifact provenance is authentic -- the caller-supplied
   ``eligibility.artifact_hash`` must equal the freshly re-derived
   artifact's own hash.
3 & 4. the caller-supplied ``scope`` belongs to the same hypothesis/family/
   content/manifest as ``eligibility`` (explicit field cross-check, for a
   clear error message) AND is re-derived fresh from trusted proposal/CAS
   provenance via ``bind_hypothesis_tradeable_scope`` and must hash-match
   the caller-supplied ``scope`` (never caller-trusted either).
5. the ``candidate`` is whatever the caller independently supplies -- this
   module never constructs one.
6. ``verify_live_candidate_matches_scope(scope, candidate)`` must be
   ``True``, using the SAME unmodified matcher this repository already has.
7. candidate freshness -- enforced by ``risk_manager.evaluate_risk`` itself
   (``profile.maximum_signal_age_seconds``), unchanged.
8. MT5/account snapshot freshness -- enforced by ``mt5_risk_adapter.
   adapt_mt5_exports`` itself (``maximum_age_seconds``), unchanged; this
   module calls it, never reimplements it.
9. symbol trade_mode/action compatibility -- also enforced entirely inside
   ``adapt_mt5_exports`` (the only place in this codebase that ever checks
   MT5 ``trade_mode``); this module calls it, never reimplements it.
10. the existing ``RiskProfile`` is used completely unchanged -- this
    module never mutates or replaces any of its fields.

HARD SAFETY RULE: ACCEPTED != RiskDecision.ALLOW != execution authorization.
Every call to :func:`evaluate_ser8_research_risk_gate` returns after
producing a ``SER8ResearchRiskGateEvidenceV1`` and a ``RiskDecision`` --
even ``state="ALLOW"`` -- and triggers nothing further.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.final_verdict_control import FinalVerdictAcceptanceControl
from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.discovery.hypothesis_tradeable_scope import (
    HypothesisTradeableScopeV1,
    bind_hypothesis_tradeable_scope,
)
from trademind.discovery.research_eligibility_boundary import (
    ResearchEligibilityArtifactV1,
    present_eligible_artifact,
)
from trademind.hypothesis_live_candidate_matching import verify_live_candidate_matches_scope
from trademind.mt5_risk_adapter import adapt_mt5_exports
from trademind.risk_manager import (
    PendingRiskReservation,
    PortfolioSnapshot,
    RiskDecision,
    RiskProfile,
    evaluate_risk,
)
from trademind.ser8_core8_market_only_policy import (
    CORE_8_SYMBOLS,
    SER8Core8PolicyError,
    verify_core8_market_only_execution,
)
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-research-risk-gate-evidence-v1"
_EVIDENCE_HASH_DOMAIN = b"trademind:ser8:research-risk-gate-evidence:v1"

CORE8_OPERATIONAL_ROUTE_PREFIX = "core8-op:"
CORE8_OPERATIONAL_SETUP_FAMILY = "SMC_OTE_PIVOT_BREAK"
CORE8_AUTHORITATIVE_OTE_PROVENANCE = "OTE_ENGINE_BUILD_OTE_SIGNALS"
CORE8_OPERATIONAL_FAMILY_ID = "core8-operational-v1"
_CORE8_OPERATIONAL_ELIGIBILITY_HASH_DOMAIN = b"trademind:ser8:core8-operational-eligibility:v1"
_CORE8_OPERATIONAL_SCOPE_HASH_DOMAIN = b"trademind:ser8:core8-operational-scope:v1"

# The only signal_state this gate ever asserts to risk_manager.evaluate_risk.
# "PUBLISHABLE" is the live signal_intelligence pipeline's own automatic
# historical-evidence gate state (see signal_to_risk_bridge.py) -- a
# candidate reaching this gate did NOT go through that gate, so claiming it
# would misrepresent how this candidate was approved. "APPROVED_MANUAL"
# (already part of risk_manager.VALID_SIGNAL_STATES; nothing new invented)
# honestly labels this as a distinct, non-automatic approval channel: an
# independently-verified ACCEPTED research finding plus an exact scope
# match, not the live heuristic publication gate. RiskProfile.
# allowed_signal_states defaults to ("PUBLISHABLE",) only, so this channel
# produces BLOCK by default unless an operator explicitly reconfigures
# their RiskProfile to accept "APPROVED_MANUAL" -- this module never
# changes that default.
_GATE_STATE = "APPROVED_MANUAL"


class SER8ResearchRiskGateError(RuntimeError):
    """Raised whenever any precondition in this gate's required input chain
    fails -- always before ``risk_manager.evaluate_risk`` is called."""


@dataclass(frozen=True, slots=True)
class Core8OperationalRouteV1:
    """Closed, deterministic identity for one ``core8-op:<SYMBOL>`` route.

    This is deliberately not a research eligibility artifact or hypothesis
    scope.  It grants no execution authority; it only identifies the narrow
    operational path whose genuine OTE candidate may skip those two legacy
    research lookups while retaining every downstream gate.
    """

    route_id: str
    symbol: str
    hypothesis_family_id: str = CORE8_OPERATIONAL_FAMILY_ID
    timeframe: str = "M5"
    setup_family: str = CORE8_OPERATIONAL_SETUP_FAMILY
    allowed_action_scope: str = "BOTH"
    eligibility_hash: str = field(init=False)
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.route_id) is not str or type(self.symbol) is not str:
            raise SER8ResearchRiskGateError("CORE8 operational route identity must be strings")
        if self.symbol not in CORE_8_SYMBOLS or self.route_id != CORE8_OPERATIONAL_ROUTE_PREFIX + self.symbol:
            raise SER8ResearchRiskGateError(
                f"invalid CORE8 operational route {self.route_id!r}; only exact core8-op:<CORE8> routes are allowed"
            )
        if (
            self.hypothesis_family_id != CORE8_OPERATIONAL_FAMILY_ID
            or self.setup_family != CORE8_OPERATIONAL_SETUP_FAMILY
            or self.timeframe != "M5"
            or self.allowed_action_scope != "BOTH"
        ):
            raise SER8ResearchRiskGateError("CORE8 operational route constants cannot be widened")
        projection = {"route_id": self.route_id, "symbol": self.symbol}
        object.__setattr__(
            self,
            "eligibility_hash",
            sha256_bytes(
                _CORE8_OPERATIONAL_ELIGIBILITY_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(projection)
            ),
        )
        object.__setattr__(
            self,
            "scope_hash",
            sha256_bytes(
                _CORE8_OPERATIONAL_SCOPE_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(projection)
            ),
        )

    @property
    def hypothesis_id(self) -> str:
        return self.route_id


def resolve_core8_operational_route(route_id: object) -> Core8OperationalRouteV1 | None:
    """Resolve an exact operational route, or ``None`` for a legacy id.

    A value beginning with the reserved prefix but not naming one of the
    exact eight routes fails closed instead of falling back to research.
    """
    if type(route_id) is not str or not route_id.startswith(CORE8_OPERATIONAL_ROUTE_PREFIX):
        return None
    symbol = route_id.removeprefix(CORE8_OPERATIONAL_ROUTE_PREFIX)
    return Core8OperationalRouteV1(route_id=route_id, symbol=symbol)


def verify_core8_operational_candidate(
    route: Core8OperationalRouteV1,
    candidate: SignalCandidate,
) -> None:
    """Verify candidate-owned proof for the direct operational path."""
    if type(route) is not Core8OperationalRouteV1:
        raise SER8ResearchRiskGateError("operational_route must be a genuine Core8OperationalRouteV1")
    if type(candidate) is not SignalCandidate:
        raise SER8ResearchRiskGateError("candidate must be a genuine SignalCandidate")
    if candidate.symbol != route.symbol:
        raise SER8ResearchRiskGateError(
            f"candidate symbol {candidate.symbol!r} does not match operational route {route.route_id!r}"
        )
    if candidate.symbol not in CORE_8_SYMBOLS:
        raise SER8ResearchRiskGateError("operational candidate symbol is not in canonical CORE8")
    if candidate.setup_family != CORE8_OPERATIONAL_SETUP_FAMILY:
        raise SER8ResearchRiskGateError(
            f"operational candidate setup_family must be {CORE8_OPERATIONAL_SETUP_FAMILY!r}"
        )
    if CORE8_AUTHORITATIVE_OTE_PROVENANCE not in candidate.provenance:
        raise SER8ResearchRiskGateError(
            f"operational candidate lacks authoritative OTE provenance marker "
            f"{CORE8_AUTHORITATIVE_OTE_PROVENANCE!r}"
        )
    if candidate.generated_from_market_data is not True:
        raise SER8ResearchRiskGateError("operational candidate is not marked as generated from market data")
    try:
        verify_core8_market_only_execution(
            symbol=candidate.symbol,
            order_types=[entry.order_type for entry in candidate.plan.entries],
        )
    except SER8Core8PolicyError as exc:
        raise SER8ResearchRiskGateError(str(exc)) from exc
    if candidate.plan.entries[0].allocation != 1.0:
        raise SER8ResearchRiskGateError(
            "CORE8 operational MARKET_ONLY entry allocation must be exactly 1.0"
        )


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8ResearchRiskGateError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class SER8ResearchRiskGateEvidenceV1:
    """Immutable, auditable record binding an ACCEPTED research hypothesis's
    verified eligibility and tradeable scope to the fresh live candidate and
    risk decision they were evaluated against.

    Deliberately carries no broker-order authorization field. ``risk_
    decision_state`` mirrors ``RiskDecision.state`` verbatim (ALLOW/BLOCK)
    and means exactly what ``risk_manager.py`` says it means: sizing and
    limits only. This record is proof of what was evaluated and what came
    out -- it is not, and cannot be read as, permission to trade.
    """

    schema_version: str
    research_hypothesis_id: str
    research_hypothesis_family_id: str
    research_eligibility_artifact_hash: str
    hypothesis_tradeable_scope_hash: str
    live_candidate_signal_id: str
    live_candidate_symbol: str
    live_candidate_timeframe: str
    live_candidate_action: str
    market_account_snapshot_hash: str
    account_id: str
    risk_profile_hash: str
    risk_profile_name: str
    risk_decision_id: str
    risk_decision_state: str
    evaluated_at: str
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8ResearchRiskGateError("unsupported SER8 research risk gate evidence schema_version")
        for value, field_name in (
            (self.research_hypothesis_id, "research_hypothesis_id"),
            (self.research_hypothesis_family_id, "research_hypothesis_family_id"),
            (self.research_eligibility_artifact_hash, "research_eligibility_artifact_hash"),
            (self.hypothesis_tradeable_scope_hash, "hypothesis_tradeable_scope_hash"),
            (self.live_candidate_signal_id, "live_candidate_signal_id"),
            (self.live_candidate_symbol, "live_candidate_symbol"),
            (self.live_candidate_timeframe, "live_candidate_timeframe"),
            (self.live_candidate_action, "live_candidate_action"),
            (self.market_account_snapshot_hash, "market_account_snapshot_hash"),
            (self.account_id, "account_id"),
            (self.risk_profile_hash, "risk_profile_hash"),
            (self.risk_profile_name, "risk_profile_name"),
            (self.risk_decision_id, "risk_decision_id"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.risk_decision_state not in {"ALLOW", "BLOCK"}:
            raise SER8ResearchRiskGateError("risk_decision_state must be ALLOW or BLOCK")
        if type(self.evaluated_at) is not str:
            raise SER8ResearchRiskGateError("evaluated_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.evaluated_at)
        except ValueError as exc:
            raise SER8ResearchRiskGateError("evaluated_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SER8ResearchRiskGateError("evaluated_at must be timezone-aware")

        object.__setattr__(
            self,
            "evidence_hash",
            sha256_bytes(_EVIDENCE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Excludes ``evaluated_at`` (wall-clock, non-deterministic) so
        evaluating the identical input at a different wall-clock instant
        still produces the same ``evidence_hash``."""
        return {
            "schema_version": self.schema_version,
            "research_hypothesis_id": self.research_hypothesis_id,
            "research_hypothesis_family_id": self.research_hypothesis_family_id,
            "research_eligibility_artifact_hash": self.research_eligibility_artifact_hash,
            "hypothesis_tradeable_scope_hash": self.hypothesis_tradeable_scope_hash,
            "live_candidate_signal_id": self.live_candidate_signal_id,
            "live_candidate_symbol": self.live_candidate_symbol,
            "live_candidate_timeframe": self.live_candidate_timeframe,
            "live_candidate_action": self.live_candidate_action,
            "market_account_snapshot_hash": self.market_account_snapshot_hash,
            "account_id": self.account_id,
            "risk_profile_hash": self.risk_profile_hash,
            "risk_profile_name": self.risk_profile_name,
            "risk_decision_id": self.risk_decision_id,
            "risk_decision_state": self.risk_decision_state,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["evidence_hash"] = self.evidence_hash
        payload["diagnostics"] = {"evaluated_at": self.evaluated_at}
        return payload


@dataclass(frozen=True, slots=True)
class SER8ResearchRiskGateResult:
    """Everything :func:`evaluate_ser8_research_risk_gate` returns: the
    immutable evidence record and the full, unmodified ``RiskDecision`` it
    binds. Nothing about receiving this result -- including
    ``decision.state == "ALLOW"`` -- authorizes any further action."""

    evidence: SER8ResearchRiskGateEvidenceV1
    decision: RiskDecision
    portfolio: PortfolioSnapshot


def _snapshot_hash(*, account, instrument, portfolio) -> str:
    payload = {
        "account": account.as_dict(),
        "instrument": instrument.as_dict(),
        "portfolio": portfolio.as_dict(),
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _profile_hash(profile: RiskProfile) -> str:
    return sha256_bytes(canonical_json_bytes(profile.as_dict()))


def evaluate_ser8_research_risk_gate(
    eligibility: ResearchEligibilityArtifactV1 | None,
    scope: HypothesisTradeableScopeV1 | None,
    candidate: SignalCandidate,
    *,
    registry: HypothesisRegistry,
    final_verdict: FinalVerdictAcceptanceControl,
    login: str,
    account_csv: Path,
    positions_csv: Path,
    symbols_csv: Path,
    profile: RiskProfile | None = None,
    correlations: Path | None = None,
    requested_risk_pct: float | None = None,
    pending_reservations: tuple[PendingRiskReservation, ...] = (),
    maximum_mt5_age_seconds: float = 120.0,
    operational_route: Core8OperationalRouteV1 | None = None,
    now: datetime | None = None,
) -> SER8ResearchRiskGateResult:
    """Evaluate the SER8 research risk gate and STOP at a verified
    ``RiskDecision``. Raises :class:`SER8ResearchRiskGateError` for every
    failure in the required input chain (see module docstring), always
    before ``risk_manager.evaluate_risk`` is called. Never sends an order,
    calls a broker, or produces anything beyond a ``RiskDecision`` and its
    evidence record.
    """
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if operational_route is not None:
        # The only direct-connect exception: there is deliberately no
        # ResearchEligibilityArtifactV1 or HypothesisTradeableScopeV1 to
        # look up.  The closed route plus candidate-owned OTE proof replace
        # only those two legacy lookups; risk and every later gate remain.
        if eligibility is not None or scope is not None:
            raise SER8ResearchRiskGateError(
                "CORE8 operational evaluation must not be combined with research lifecycle artifacts"
            )
        verify_core8_operational_candidate(operational_route, candidate)
        hypothesis_id = operational_route.hypothesis_id
        hypothesis_family_id = operational_route.hypothesis_family_id
        eligibility_hash = operational_route.eligibility_hash
        scope_hash = operational_route.scope_hash
    else:
        # 1-2: re-verify ACCEPTED eligibility fresh; never trust the
        # caller-supplied object at face value.
        fresh_eligibility = present_eligible_artifact(
            eligibility.hypothesis_id, registry=registry, final_verdict=final_verdict
        )
        if fresh_eligibility.artifact_hash != eligibility.artifact_hash:
            raise SER8ResearchRiskGateError(
                "supplied research eligibility artifact does not match a fresh, independent "
                "re-verification of the same hypothesis_id -- refusing to trust it"
            )

        # 3: explicit identity cross-check between eligibility and scope,
        # for a clear error before attempting to re-derive the scope at all.
        if (
            scope.hypothesis_id != eligibility.hypothesis_id
            or scope.hypothesis_family_id != eligibility.hypothesis_family_id
            or scope.bound_hypothesis_content_hash != eligibility.bound_hypothesis_content_hash
            or scope.manifest_semantic_hash != eligibility.manifest_semantic_hash
            or scope.manifest_artifact_hash_ref != eligibility.manifest_artifact_hash_ref
        ):
            raise SER8ResearchRiskGateError(
                "supplied hypothesis tradeable scope does not belong to the same "
                "hypothesis/family/content/manifest as the supplied eligibility artifact"
            )

        # 4: re-derive the scope fresh from trusted proposal/CAS provenance;
        # never trust the caller-supplied scope object at face value either.
        fresh_scope = bind_hypothesis_tradeable_scope(
            eligibility.hypothesis_id, registry=registry, artifact_store=final_verdict.artifacts
        )
        if fresh_scope.scope_hash != scope.scope_hash:
            raise SER8ResearchRiskGateError(
                "supplied hypothesis tradeable scope does not match a fresh, independent "
                "re-derivation from trusted proposal/CAS provenance -- refusing to trust it"
            )

        # 5-6: the candidate is whatever the caller independently supplies;
        # it must exactly match the verified scope's identity.
        if not verify_live_candidate_matches_scope(scope, candidate):
            raise SER8ResearchRiskGateError(
                "live candidate does not exactly match the hypothesis tradeable scope "
                "(symbol/timeframe/setup_family/action)"
            )
        hypothesis_id = eligibility.hypothesis_id
        hypothesis_family_id = eligibility.hypothesis_family_id
        eligibility_hash = eligibility.artifact_hash
        scope_hash = scope.scope_hash

    # 8-9: MT5/account snapshot freshness and symbol trade_mode/action
    # compatibility are enforced entirely inside adapt_mt5_exports, unchanged.
    rules = profile or RiskProfile()
    try:
        bundle = adapt_mt5_exports(
            account_csv=account_csv,
            positions_csv=positions_csv,
            symbols_csv=symbols_csv,
            symbol=candidate.symbol,
            action=candidate.plan.action,
            correlations=correlations,
            maximum_age_seconds=maximum_mt5_age_seconds,
            maximum_clock_skew_seconds=rules.maximum_clock_skew_seconds,
            now=captured_at,
        )
    except Exception as exc:  # noqa: BLE001 -- untrusted CSV export content.
        raise SER8ResearchRiskGateError(f"MT5 account/instrument snapshot could not be verified: {exc}") from exc

    login = _nonempty_str(login, field_name="login")
    if bundle.account.account_id != login:
        raise SER8ResearchRiskGateError(
            f"requested login {login} does not match MT5 snapshot {bundle.account.account_id}"
        )

    # Broker-active pending legs consume their ORIGINAL planned risk and
    # known margin before this decision is evaluated.  The live positions
    # snapshot remains the sole source for FILLED risk, so PENDING->FILLED
    # cannot be counted twice.
    portfolio = PortfolioSnapshot(
        positions=bundle.portfolio.positions,
        open_trades=bundle.portfolio.open_trades,
        reserved_risk_money=sum(item.risk_money for item in pending_reservations),
        reserved_margin=sum(item.margin_required or 0.0 for item in pending_reservations),
        pending_reservations=tuple(pending_reservations),
    )

    # 7, 10: candidate freshness and the existing RiskProfile (unchanged)
    # are both enforced entirely inside evaluate_risk, unchanged.
    decision = evaluate_risk(
        candidate,
        gate_state=_GATE_STATE,
        account=bundle.account,
        instrument=bundle.instrument,
        profile=rules,
        portfolio=portfolio,
        requested_risk_pct=requested_risk_pct,
        now=captured_at,
    )

    evidence = SER8ResearchRiskGateEvidenceV1(
        schema_version=SCHEMA_VERSION,
        research_hypothesis_id=hypothesis_id,
        research_hypothesis_family_id=hypothesis_family_id,
        research_eligibility_artifact_hash=eligibility_hash,
        hypothesis_tradeable_scope_hash=scope_hash,
        live_candidate_signal_id=candidate.signal_id,
        live_candidate_symbol=candidate.symbol,
        live_candidate_timeframe=candidate.timeframe,
        live_candidate_action=candidate.plan.action,
        market_account_snapshot_hash=_snapshot_hash(
            account=bundle.account, instrument=bundle.instrument, portfolio=portfolio
        ),
        account_id=bundle.account.account_id,
        risk_profile_hash=_profile_hash(rules),
        risk_profile_name=rules.name,
        risk_decision_id=decision.decision_id,
        risk_decision_state=decision.state,
        evaluated_at=captured_at.isoformat(),
    )
    return SER8ResearchRiskGateResult(evidence=evidence, decision=decision, portfolio=portfolio)


__all__ = [
    "CORE8_AUTHORITATIVE_OTE_PROVENANCE",
    "CORE8_OPERATIONAL_FAMILY_ID",
    "CORE8_OPERATIONAL_ROUTE_PREFIX",
    "CORE8_OPERATIONAL_SETUP_FAMILY",
    "SCHEMA_VERSION",
    "Core8OperationalRouteV1",
    "SER8ResearchRiskGateError",
    "SER8ResearchRiskGateEvidenceV1",
    "SER8ResearchRiskGateResult",
    "evaluate_ser8_research_risk_gate",
    "resolve_core8_operational_route",
    "verify_core8_operational_candidate",
]
