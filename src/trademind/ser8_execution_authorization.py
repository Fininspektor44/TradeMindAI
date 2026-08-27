"""SER8 Execution Authorization V1: the terminal layer of the research ->
risk-gate chain. Turns a verified ``SER8ResearchRiskGateEvidenceV1`` +
``RiskDecision(state="ALLOW")`` into an explicit, immutable
``ExecutionAuthorizationV1`` -- and STOPS there.

This module never sends an MT5 order, never calls a broker API, never
modifies or closes a position, and never produces a broker ticket, a fill,
an execution result, or an MT5 order id. ``ExecutionAuthorizationV1`` is
proof that every precondition this codebase currently knows how to check
was satisfied at one specific moment -- it is deliberately NOT the act of
trading, and it does not itself trigger anything. Consuming an
authorization to actually place an order is explicitly out of scope for
this module and is left to a future, separate, MT5 write-side adapter that
does not exist yet; this module only makes that future adapter's job
possible to do safely -- one authorization per (hypothesis, account) at a
time, with a short, explicit expiry.

AUTHORIZATION MAY BE CREATED ONLY WHEN (every check below fails closed,
before an authorization is built or persisted):

1. the research hypothesis is ACCEPTED -- re-verified fresh by calling the
   unmodified ``present_eligible_artifact``, never trusting the
   caller-supplied ``eligibility`` object at face value.
2. the supplied ``scope`` is re-verified fresh by calling the unmodified
   ``bind_hypothesis_tradeable_scope``, likewise never caller-trusted.
3. the supplied ``candidate`` still matches that scope, re-checked via the
   unmodified ``verify_live_candidate_matches_scope``.
4. the supplied ``eligibility``/``scope``/``candidate`` objects genuinely
   correspond to what ``result.evidence`` was itself built from (their
   hashes/identities must match the evidence's own recorded hashes) --
   catching a caller who supplies a genuine, fresh, but WRONG (mismatched)
   eligibility/scope/candidate alongside a real evidence record.
5. ``result.evidence.risk_decision_id`` and ``result.evidence.risk_
   decision_state`` match ``result.decision.decision_id``/``.state``
   exactly -- catching a caller who pairs a real evidence record with a
   real but unrelated ``RiskDecision``.
6. ``result.decision.state == "ALLOW"`` -- BLOCK never produces an
   authorization, unconditionally.
7. the risk-gate evidence is still within its own freshness window
   (``now - decision.evaluated_at <= maximum_evidence_age_seconds``) --
   an ALLOW decision from an evidence record that is no longer fresh
   cannot silently become authorization later; this is a NEW freshness
   boundary specific to authorization creation, distinct from (and in
   addition to) the account/candidate freshness the risk gate itself
   already checked at its own evaluation time.
8. no conflicting authorization already exists for the same
   (hypothesis_id, account_id) pair -- see IDEMPOTENCY below.

IDEMPOTENCY: this module owns one new, additive SQLite table in the SAME
database file as ``HypothesisRegistry`` (``registry.path``), mirroring the
pattern every other control in this lineage already uses
(``ValidationExecutionControl``, ``FinalVerdictAcceptanceControl``, ...).
At most one ACTIVE (not yet expired) authorization may exist per
``(hypothesis_id, account_id)`` at a time. Calling :meth:`SER8ExecutionAuthorizationControl.authorize`
again with the identical approved inputs (the same evidence_hash bound to
the same decision_id) returns the SAME existing authorization unchanged
(idempotent, no duplicate row). Calling it again for the same
``(hypothesis_id, account_id)`` with a DIFFERENT evidence/decision while an
active authorization still exists fails closed with
:class:`SER8ExecutionAuthorizationConflict`. Once an authorization expires,
a fresh call for the same ``(hypothesis_id, account_id)`` may create a new
one -- an expired authorization never blocks a new one and is never itself
made usable again.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
from trademind.risk_manager import RiskDecision
from trademind.ser8_research_risk_gate import (
    Core8OperationalRouteV1,
    SER8ResearchRiskGateError,
    SER8ResearchRiskGateResult,
    verify_core8_operational_candidate,
)
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-execution-authorization-v1"
_AUTHORIZATION_HASH_DOMAIN = b"trademind:ser8:execution-authorization:v1"

DEFAULT_MAXIMUM_EVIDENCE_AGE_SECONDS = 120.0
DEFAULT_AUTHORIZATION_TTL_SECONDS = 300.0


class SER8ExecutionAuthorizationError(RuntimeError):
    """Raised whenever any precondition for creating an execution
    authorization fails -- always before any authorization is built or
    persisted."""


class SER8ExecutionAuthorizationConflict(SER8ExecutionAuthorizationError):
    """Raised specifically when an active authorization already exists for
    the same (hypothesis_id, account_id) pair with a DIFFERENT
    evidence/decision -- a conflicting duplicate, distinct from an
    idempotent retry of the identical approved inputs."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8ExecutionAuthorizationError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionAuthorizationV1:
    """Immutable proof that one specific, fully-verified ALLOW risk
    decision, for one specific ACCEPTED research hypothesis and one
    specific fresh live candidate, was authorized at one specific moment,
    valid only until ``expires_at``.

    Deliberately carries no broker ticket, fill, execution result, or MT5
    order id -- none of those exist yet, and this record does not invent
    them. This is not itself an order, and receiving it does not send one.
    """

    schema_version: str
    authorization_id: str
    hypothesis_id: str
    hypothesis_family_id: str
    research_eligibility_artifact_hash: str
    hypothesis_tradeable_scope_hash: str
    live_candidate_signal_id: str
    risk_gate_evidence_hash: str
    risk_decision_id: str
    account_id: str
    market_account_snapshot_hash: str
    symbol: str
    action: str
    authorized_at: str
    expires_at: str
    authorization_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8ExecutionAuthorizationError("unsupported execution authorization schema_version")
        for value, field_name in (
            (self.authorization_id, "authorization_id"),
            (self.hypothesis_id, "hypothesis_id"),
            (self.hypothesis_family_id, "hypothesis_family_id"),
            (self.research_eligibility_artifact_hash, "research_eligibility_artifact_hash"),
            (self.hypothesis_tradeable_scope_hash, "hypothesis_tradeable_scope_hash"),
            (self.live_candidate_signal_id, "live_candidate_signal_id"),
            (self.risk_gate_evidence_hash, "risk_gate_evidence_hash"),
            (self.risk_decision_id, "risk_decision_id"),
            (self.account_id, "account_id"),
            (self.market_account_snapshot_hash, "market_account_snapshot_hash"),
            (self.symbol, "symbol"),
            (self.action, "action"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.action not in {"BUY", "SELL"}:
            raise SER8ExecutionAuthorizationError("action must be BUY or SELL")

        for value, field_name in ((self.authorized_at, "authorized_at"), (self.expires_at, "expires_at")):
            if type(value) is not str:
                raise SER8ExecutionAuthorizationError(f"{field_name} must be a string")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise SER8ExecutionAuthorizationError(f"{field_name} must be an ISO timestamp") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SER8ExecutionAuthorizationError(f"{field_name} must be timezone-aware")
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.authorized_at):
            raise SER8ExecutionAuthorizationError("expires_at must be strictly after authorized_at")

        object.__setattr__(
            self,
            "authorization_hash",
            sha256_bytes(
                _AUTHORIZATION_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Excludes nothing wall-clock-only here except this record's own
        content is already timestamp-bearing by design (``authorized_at``/
        ``expires_at`` are part of what is being authorized, not
        incidental diagnostics) -- so, unlike the evidence/scope/
        eligibility artifacts upstream, both timestamps ARE part of the
        hash. Re-authorizing identical approved inputs at a materially
        different moment is intentionally a different authorization; the
        idempotency guarantee lives in :meth:`SER8ExecutionAuthorizationControl.
        authorize` returning the SAME already-persisted row for a request
        that arrives while that row is still active, not in two
        differently-timed authorizations hashing the same."""
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "research_eligibility_artifact_hash": self.research_eligibility_artifact_hash,
            "hypothesis_tradeable_scope_hash": self.hypothesis_tradeable_scope_hash,
            "live_candidate_signal_id": self.live_candidate_signal_id,
            "risk_gate_evidence_hash": self.risk_gate_evidence_hash,
            "risk_decision_id": self.risk_decision_id,
            "account_id": self.account_id,
            "market_account_snapshot_hash": self.market_account_snapshot_hash,
            "symbol": self.symbol,
            "action": self.action,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["authorization_id"] = self.authorization_id
        payload["authorization_hash"] = self.authorization_hash
        return payload

    def is_usable(self, *, now: datetime | None = None) -> bool:
        """Pure freshness check only -- ``True`` iff ``now`` is strictly
        before ``expires_at``. Carries no opinion about one-shot broker
        consumption (that state lives in ``SER8ExecutionAuthorizationControl``'s
        own storage, not in this immutable record, and is not implemented
        by this module)."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return current < datetime.fromisoformat(self.expires_at)


def _approval_key_hash(*, evidence_hash: str, decision_id: str) -> str:
    """A stable identity for "this exact approved input", independent of
    wall-clock authorization time -- used only to detect an idempotent
    retry versus a genuinely conflicting duplicate; never persisted as the
    authorization's own hash."""
    return sha256_bytes(
        _AUTHORIZATION_HASH_DOMAIN
        + b"\x00approval\x00"
        + canonical_json_bytes({"evidence_hash": evidence_hash, "decision_id": decision_id})
    )


class SER8ExecutionAuthorizationControl:
    """Owns one new, additive SQLite table in the same database file as
    ``HypothesisRegistry`` (``registry.path``). Never modifies
    ``HypothesisRegistry``'s own schema, tables, or state machine."""

    def __init__(self, *, registry: HypothesisRegistry, final_verdict: FinalVerdictAcceptanceControl) -> None:
        self.registry = registry
        self.final_verdict = final_verdict
        self.path = Path(registry.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ser8_execution_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    approval_key_hash TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ser8_execution_authorizations_active_idx
                    ON ser8_execution_authorizations(hypothesis_id, account_id, expires_at);
                """
            )

    def authorize(
        self,
        eligibility: ResearchEligibilityArtifactV1 | None,
        scope: HypothesisTradeableScopeV1 | None,
        candidate: SignalCandidate,
        result: SER8ResearchRiskGateResult,
        *,
        maximum_evidence_age_seconds: float = DEFAULT_MAXIMUM_EVIDENCE_AGE_SECONDS,
        authorization_ttl_seconds: float = DEFAULT_AUTHORIZATION_TTL_SECONDS,
        operational_route: Core8OperationalRouteV1 | None = None,
        now: datetime | None = None,
    ) -> ExecutionAuthorizationV1:
        """Create (or idempotently return) an ``ExecutionAuthorizationV1``.
        Fails closed -- raises :class:`SER8ExecutionAuthorizationError` or
        :class:`SER8ExecutionAuthorizationConflict` -- for every precondition
        listed in the module docstring, always before anything is built or
        persisted.
        """
        if maximum_evidence_age_seconds <= 0 or authorization_ttl_seconds <= 0:
            raise SER8ExecutionAuthorizationError("freshness/ttl windows must be positive")
        evidence = result.evidence
        decision: RiskDecision = result.decision
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        # 4: supplied lineage/candidate genuinely corresponds to what this
        # evidence record was itself built from.
        if (
            evidence.live_candidate_signal_id != candidate.signal_id
            or evidence.live_candidate_symbol != candidate.symbol
            or evidence.live_candidate_timeframe != candidate.timeframe
            or evidence.live_candidate_action != candidate.plan.action
        ):
            raise SER8ExecutionAuthorizationError(
                "supplied live candidate does not match the risk-gate evidence's own recorded "
                "candidate identity"
            )

        if operational_route is not None:
            if eligibility is not None or scope is not None:
                raise SER8ExecutionAuthorizationError(
                    "CORE8 operational authorization must not be combined with research lifecycle artifacts"
                )
            try:
                verify_core8_operational_candidate(operational_route, candidate)
            except SER8ResearchRiskGateError as exc:
                raise SER8ExecutionAuthorizationError(str(exc)) from exc
            if (
                evidence.research_hypothesis_id != operational_route.hypothesis_id
                or evidence.research_hypothesis_family_id != operational_route.hypothesis_family_id
                or evidence.research_eligibility_artifact_hash != operational_route.eligibility_hash
                or evidence.hypothesis_tradeable_scope_hash != operational_route.scope_hash
            ):
                raise SER8ExecutionAuthorizationError(
                    "CORE8 operational route does not match the risk-gate evidence lineage"
                )
            hypothesis_id = operational_route.hypothesis_id
            hypothesis_family_id = operational_route.hypothesis_family_id
            eligibility_hash = operational_route.eligibility_hash
            scope_hash = operational_route.scope_hash
        else:
            if type(eligibility) is not ResearchEligibilityArtifactV1:
                raise SER8ExecutionAuthorizationError("a genuine research eligibility artifact is required")
            if type(scope) is not HypothesisTradeableScopeV1:
                raise SER8ExecutionAuthorizationError("a genuine hypothesis tradeable scope is required")
            if evidence.research_eligibility_artifact_hash != eligibility.artifact_hash:
                raise SER8ExecutionAuthorizationError(
                    "supplied eligibility artifact does not match the risk-gate evidence's own "
                    "recorded eligibility artifact hash"
                )
            if evidence.hypothesis_tradeable_scope_hash != scope.scope_hash:
                raise SER8ExecutionAuthorizationError(
                    "supplied hypothesis tradeable scope does not match the risk-gate evidence's own "
                    "recorded scope hash"
                )
            if (
                scope.hypothesis_id != eligibility.hypothesis_id
                or scope.hypothesis_family_id != eligibility.hypothesis_family_id
                or scope.bound_hypothesis_content_hash != eligibility.bound_hypothesis_content_hash
            ):
                raise SER8ExecutionAuthorizationError(
                    "supplied scope does not belong to the same hypothesis/family/content as the "
                    "supplied eligibility artifact"
                )

            # 1-3: re-verify the eligibility/scope/candidate lineage fresh;
            # never trust any caller-supplied object at face value.
            fresh_eligibility = present_eligible_artifact(
                eligibility.hypothesis_id, registry=self.registry, final_verdict=self.final_verdict
            )
            if fresh_eligibility.artifact_hash != eligibility.artifact_hash:
                raise SER8ExecutionAuthorizationError(
                    "supplied eligibility artifact does not match a fresh, independent re-verification"
                )
            fresh_scope = bind_hypothesis_tradeable_scope(
                eligibility.hypothesis_id, registry=self.registry, artifact_store=self.final_verdict.artifacts
            )
            if fresh_scope.scope_hash != scope.scope_hash:
                raise SER8ExecutionAuthorizationError(
                    "supplied hypothesis tradeable scope does not match a fresh, independent re-derivation"
                )
            if not verify_live_candidate_matches_scope(scope, candidate):
                raise SER8ExecutionAuthorizationError(
                    "live candidate no longer matches the hypothesis tradeable scope on re-verification"
                )
            hypothesis_id = eligibility.hypothesis_id
            hypothesis_family_id = eligibility.hypothesis_family_id
            eligibility_hash = eligibility.artifact_hash
            scope_hash = scope.scope_hash

        # 5: evidence <-> decision pairing.
        if evidence.risk_decision_id != decision.decision_id or evidence.risk_decision_state != decision.state:
            raise SER8ExecutionAuthorizationError(
                "risk-gate evidence does not belong to the exact supplied RiskDecision"
            )

        # 6: BLOCK never produces an authorization.
        if decision.state != "ALLOW":
            raise SER8ExecutionAuthorizationError(
                f"RiskDecision.state is {decision.state!r}, not ALLOW; refusing to authorize"
            )

        # 7: the risk-gate evidence must still be within its own freshness
        # window at authorization-creation time.
        evidence_age = (captured_at - decision.evaluated_at.astimezone(timezone.utc)).total_seconds()
        if evidence_age < 0 or evidence_age > maximum_evidence_age_seconds:
            raise SER8ExecutionAuthorizationError(
                f"risk-gate evidence age {evidence_age:.1f}s is outside the allowed "
                f"{maximum_evidence_age_seconds:.1f}s authorization freshness window"
            )

        approval_key_hash = _approval_key_hash(
            evidence_hash=evidence.evidence_hash, decision_id=decision.decision_id
        )
        authorized_at = captured_at.isoformat()
        expires_at = (captured_at + timedelta(seconds=authorization_ttl_seconds)).isoformat()
        authorization_id = (
            f"EA-{evidence.account_id}-"
            f"{sha256_bytes(canonical_json_bytes({'approval_key_hash': approval_key_hash})).removeprefix('sha256:')[:16]}"
        )

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # 8: no conflicting active authorization for (hypothesis_id, account_id).
                active = db.execute(
                    """
                    SELECT * FROM ser8_execution_authorizations
                    WHERE hypothesis_id=? AND account_id=? AND expires_at > ?
                    ORDER BY authorized_at DESC LIMIT 1
                    """,
                    (hypothesis_id, evidence.account_id, authorized_at),
                ).fetchone()
                if active is not None:
                    if active["approval_key_hash"] == approval_key_hash:
                        db.commit()
                        return _authorization_from_row(active)
                    db.rollback()
                    raise SER8ExecutionAuthorizationConflict(
                        "an active execution authorization already exists for this "
                        "(hypothesis_id, account_id) pair with a different approved risk decision"
                    )

                authorization = ExecutionAuthorizationV1(
                    schema_version=SCHEMA_VERSION,
                    authorization_id=authorization_id,
                    hypothesis_id=hypothesis_id,
                    hypothesis_family_id=hypothesis_family_id,
                    research_eligibility_artifact_hash=eligibility_hash,
                    hypothesis_tradeable_scope_hash=scope_hash,
                    live_candidate_signal_id=candidate.signal_id,
                    risk_gate_evidence_hash=evidence.evidence_hash,
                    risk_decision_id=decision.decision_id,
                    account_id=evidence.account_id,
                    market_account_snapshot_hash=evidence.market_account_snapshot_hash,
                    symbol=evidence.live_candidate_symbol,
                    action=evidence.live_candidate_action,
                    authorized_at=authorized_at,
                    expires_at=expires_at,
                )
                try:
                    db.execute(
                        """
                        INSERT INTO ser8_execution_authorizations(
                            authorization_id, hypothesis_id, account_id, approval_key_hash,
                            authorized_at, expires_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            authorization.authorization_id,
                            authorization.hypothesis_id,
                            authorization.account_id,
                            approval_key_hash,
                            authorization.authorized_at,
                            authorization.expires_at,
                            canonical_json_bytes(authorization.to_payload()).decode("utf-8"),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    db.rollback()
                    raise SER8ExecutionAuthorizationError(
                        f"execution authorization already exists: {exc}"
                    ) from exc
                db.commit()
                return authorization
            except Exception:
                db.rollback()
                raise

    def get_authorization(self, authorization_id: str) -> ExecutionAuthorizationV1 | None:
        """Public, read-only accessor for one already-persisted
        authorization by its ``authorization_id`` -- ``None`` if no such
        row exists. Reconstructed independently from the persisted row
        via the SAME :func:`_authorization_from_row` helper
        :meth:`authorize` itself uses; the returned object's own
        ``authorization_hash`` re-verification is inherited automatically
        via ``ExecutionAuthorizationV1.__post_init__``. Gives downstream
        read-only consumers (e.g. an outcome-capture bridge that only
        holds a leg receipt's ``authorization_id``) a stable way to
        recover the hypothesis/account/candidate lineage an authorization
        was issued for, without granting any new authorization or
        touching this table's write path."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ser8_execution_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        if row is None:
            return None
        return _authorization_from_row(row)


def _authorization_from_row(row: sqlite3.Row) -> ExecutionAuthorizationV1:
    payload = json.loads(row["payload_json"])
    return ExecutionAuthorizationV1(
        schema_version=payload["schema_version"],
        authorization_id=payload["authorization_id"],
        hypothesis_id=payload["hypothesis_id"],
        hypothesis_family_id=payload["hypothesis_family_id"],
        research_eligibility_artifact_hash=payload["research_eligibility_artifact_hash"],
        hypothesis_tradeable_scope_hash=payload["hypothesis_tradeable_scope_hash"],
        live_candidate_signal_id=payload["live_candidate_signal_id"],
        risk_gate_evidence_hash=payload["risk_gate_evidence_hash"],
        risk_decision_id=payload["risk_decision_id"],
        account_id=payload["account_id"],
        market_account_snapshot_hash=payload["market_account_snapshot_hash"],
        symbol=payload["symbol"],
        action=payload["action"],
        authorized_at=payload["authorized_at"],
        expires_at=payload["expires_at"],
    )


__all__ = [
    "DEFAULT_AUTHORIZATION_TTL_SECONDS",
    "DEFAULT_MAXIMUM_EVIDENCE_AGE_SECONDS",
    "SCHEMA_VERSION",
    "ExecutionAuthorizationV1",
    "SER8ExecutionAuthorizationConflict",
    "SER8ExecutionAuthorizationControl",
    "SER8ExecutionAuthorizationError",
]
