"""SER8 Execution Authorization Claim V1: an atomic, one-shot claim on an
already-issued ``ExecutionAuthorizationV1``. STOPS there.

This module never sends an MT5 order, never calls a broker API, never
modifies or closes a position, and never produces a broker ticket, an
order id, or a fill. ``ExecutionAuthorizationClaimV1`` is proof that one
specific execution session (identified only by an opaque ``claimant_id``
string this module never interprets) took exclusive, one-shot possession
of one specific authorization at one specific moment. Claiming is NOT
broker execution -- it is the last purely-informational step before a
future, separate, MT5 write-side adapter (which does not exist yet) could
act; this module makes that future adapter's job of avoiding a double
order possible, without itself placing one.

CLAIM MAY BE CREATED ONLY WHEN (every check below fails closed, before a
claim is built or persisted):

1. the authorization EXISTS -- looked up by ``authorization_id`` in
   ``SER8ExecutionAuthorizationControl``'s own persisted table; a claim can
   never be attempted against an authorization this database has never
   issued.
2. the authorization is AUTHENTIC -- the caller-supplied ``authorization``
   object is reconstructed independently from the persisted row and its
   ``authorization_hash`` must match exactly; a caller-tampered object (or
   one for a stale/replaced authorization_id) is rejected before anything
   else is checked.
3. the authorization belongs to the exact hypothesis/account/candidate/risk
   evidence it claims to -- guaranteed by the same hash equality in (2)
   (``authorization_hash`` is a domain hash over every one of those
   fields), reinforced by explicit field cross-checks for a clearer error.
4. the authorization is NOT EXPIRED -- ``authorization.is_usable(now=...)``
   must be ``True`` at claim time.
5. the authorization has NEVER BEEN CLAIMED BEFORE by a different claimant
   -- see IDEMPOTENCY below.

ATOMIC INVARIANT: one ``authorization_id`` maps to at most one successful
claim, ever. This module owns one new, additive SQLite table
(``ser8_execution_authorization_claims``) in the SAME database file as
``HypothesisRegistry`` (``registry.path``), with ``authorization_id`` as its
PRIMARY KEY. Every check-then-insert happens inside one ``BEGIN IMMEDIATE``
transaction (SQLite serializes writers on this database file), and the
primary-key uniqueness constraint is a second, independent backstop against
any race the transaction boundary itself might somehow miss -- a concurrent
``INSERT`` that violates it raises ``sqlite3.IntegrityError``, which this
module treats exactly like losing the race: re-read the now-committed
row and resolve it the same way any other observed existing claim is
resolved (idempotent return for the same claimant, conflict for a
different one).

IDEMPOTENCY: calling :meth:`SER8ExecutionAuthorizationClaimControl.claim`
again for the same ``authorization_id`` with the SAME ``claimant_id``
returns the SAME already-persisted claim, unchanged (including its
original ``claimed_at`` -- retrying never creates a second row and never
advances the claim's own timestamp). Calling it again for the same
``authorization_id`` with a DIFFERENT ``claimant_id`` raises
:class:`SER8ExecutionAuthorizationClaimConflict` -- exactly one claimant
may ever hold a given authorization's claim.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.ser8_execution_authorization import ExecutionAuthorizationV1, _authorization_from_row
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-execution-authorization-claim-v1"
_CLAIM_HASH_DOMAIN = b"trademind:ser8:execution-authorization-claim:v1"


class SER8ExecutionAuthorizationClaimError(RuntimeError):
    """Raised whenever any precondition for creating a claim fails --
    always before a claim is built or persisted."""


class SER8ExecutionAuthorizationClaimConflict(SER8ExecutionAuthorizationClaimError):
    """Raised specifically when an authorization has already been claimed
    by a DIFFERENT claimant -- distinct from an idempotent retry by the
    same claimant."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8ExecutionAuthorizationClaimError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionAuthorizationClaimV1:
    """Immutable proof that one claimant took exclusive, one-shot
    possession of one specific ``ExecutionAuthorizationV1`` at one specific
    moment. Construct only via :meth:`SER8ExecutionAuthorizationClaimControl.
    claim` -- never by hand.

    Deliberately carries no broker ticket, order id, or fill -- none of
    those exist yet, and this record does not invent them.
    """

    schema_version: str
    claim_id: str
    authorization_id: str
    authorization_hash: str
    hypothesis_id: str
    hypothesis_family_id: str
    account_id: str
    live_candidate_signal_id: str
    risk_gate_evidence_hash: str
    risk_decision_id: str
    claimant_id: str
    claimed_at: str
    claim_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8ExecutionAuthorizationClaimError("unsupported execution authorization claim schema_version")
        for value, field_name in (
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.authorization_hash, "authorization_hash"),
            (self.hypothesis_id, "hypothesis_id"),
            (self.hypothesis_family_id, "hypothesis_family_id"),
            (self.account_id, "account_id"),
            (self.live_candidate_signal_id, "live_candidate_signal_id"),
            (self.risk_gate_evidence_hash, "risk_gate_evidence_hash"),
            (self.risk_decision_id, "risk_decision_id"),
            (self.claimant_id, "claimant_id"),
        ):
            _nonempty_str(value, field_name=field_name)

        if type(self.claimed_at) is not str:
            raise SER8ExecutionAuthorizationClaimError("claimed_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.claimed_at)
        except ValueError as exc:
            raise SER8ExecutionAuthorizationClaimError("claimed_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SER8ExecutionAuthorizationClaimError("claimed_at must be timezone-aware")

        object.__setattr__(
            self,
            "claim_hash",
            sha256_bytes(_CLAIM_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """``claimed_at`` IS part of the hash, mirroring
        ``ExecutionAuthorizationV1``'s own design: a claim is inherently
        about when possession was taken, not incidental diagnostics. The
        idempotency guarantee lives in :meth:`SER8ExecutionAuthorizationClaimControl.
        claim` returning the SAME already-persisted row for a retry, not in
        two differently-timed claim objects hashing the same."""
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "authorization_hash": self.authorization_hash,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_family_id": self.hypothesis_family_id,
            "account_id": self.account_id,
            "live_candidate_signal_id": self.live_candidate_signal_id,
            "risk_gate_evidence_hash": self.risk_gate_evidence_hash,
            "risk_decision_id": self.risk_decision_id,
            "claimant_id": self.claimant_id,
            "claimed_at": self.claimed_at,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["claim_id"] = self.claim_id
        payload["claim_hash"] = self.claim_hash
        return payload


class SER8ExecutionAuthorizationClaimControl:
    """Owns one new, additive SQLite table in the same database file as
    ``HypothesisRegistry`` (``registry.path``). Never modifies
    ``HypothesisRegistry``'s or ``SER8ExecutionAuthorizationControl``'s own
    schema, tables, or semantics -- only reads
    ``ser8_execution_authorizations`` rows they already own."""

    def __init__(self, *, registry: HypothesisRegistry) -> None:
        self.registry = registry
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
                CREATE TABLE IF NOT EXISTS ser8_execution_authorization_claims (
                    authorization_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    claimant_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def claim(
        self,
        authorization: ExecutionAuthorizationV1,
        *,
        claimant_id: str,
        now: datetime | None = None,
    ) -> ExecutionAuthorizationClaimV1:
        """Create (or idempotently return) an ``ExecutionAuthorizationClaimV1``.
        Fails closed -- raises :class:`SER8ExecutionAuthorizationClaimError`
        or :class:`SER8ExecutionAuthorizationClaimConflict` -- for every
        precondition listed in the module docstring, always before anything
        is built or persisted.
        """
        claimant_id = _nonempty_str(claimant_id, field_name="claimant_id")
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # 1: the authorization must exist in the source of truth.
                authorization_row = db.execute(
                    "SELECT * FROM ser8_execution_authorizations WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise SER8ExecutionAuthorizationClaimError(
                        f"no such execution authorization: {authorization.authorization_id}"
                    )

                # 2-3: the supplied authorization must be authentic and
                # belong to exactly what is persisted -- reconstructed
                # independently from the row, never trusting the caller's
                # object at face value.
                stored_authorization = _authorization_from_row(authorization_row)
                if stored_authorization.authorization_hash != authorization.authorization_hash:
                    raise SER8ExecutionAuthorizationClaimError(
                        "supplied execution authorization does not match the persisted, "
                        "authoritative authorization for this authorization_id -- refusing to trust it"
                    )
                if (
                    stored_authorization.hypothesis_id != authorization.hypothesis_id
                    or stored_authorization.account_id != authorization.account_id
                    or stored_authorization.live_candidate_signal_id != authorization.live_candidate_signal_id
                    or stored_authorization.risk_gate_evidence_hash != authorization.risk_gate_evidence_hash
                    or stored_authorization.risk_decision_id != authorization.risk_decision_id
                ):
                    raise SER8ExecutionAuthorizationClaimError(
                        "supplied execution authorization does not belong to the exact "
                        "hypothesis/account/candidate/risk evidence on record"
                    )

                # 4: not expired.
                if not stored_authorization.is_usable(now=captured_at):
                    raise SER8ExecutionAuthorizationClaimError(
                        f"execution authorization {authorization.authorization_id} has expired"
                    )

                # 5: at most one claimant, ever.
                existing = db.execute(
                    "SELECT * FROM ser8_execution_authorization_claims WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if existing is not None:
                    if existing["claimant_id"] == claimant_id:
                        db.commit()
                        return _claim_from_row(existing)
                    db.rollback()
                    raise SER8ExecutionAuthorizationClaimConflict(
                        f"execution authorization {authorization.authorization_id} has already been "
                        "claimed by a different claimant"
                    )

                claim = ExecutionAuthorizationClaimV1(
                    schema_version=SCHEMA_VERSION,
                    claim_id=(
                        f"EAC-{stored_authorization.account_id}-"
                        f"{sha256_bytes(canonical_json_bytes({'authorization_id': authorization.authorization_id, 'claimant_id': claimant_id, 'claimed_at': captured_at.isoformat()})).removeprefix('sha256:')[:16]}"
                    ),
                    authorization_id=stored_authorization.authorization_id,
                    authorization_hash=stored_authorization.authorization_hash,
                    hypothesis_id=stored_authorization.hypothesis_id,
                    hypothesis_family_id=stored_authorization.hypothesis_family_id,
                    account_id=stored_authorization.account_id,
                    live_candidate_signal_id=stored_authorization.live_candidate_signal_id,
                    risk_gate_evidence_hash=stored_authorization.risk_gate_evidence_hash,
                    risk_decision_id=stored_authorization.risk_decision_id,
                    claimant_id=claimant_id,
                    claimed_at=captured_at.isoformat(),
                )
                try:
                    db.execute(
                        """
                        INSERT INTO ser8_execution_authorization_claims(
                            authorization_id, claim_id, claimant_id, claimed_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            claim.authorization_id,
                            claim.claim_id,
                            claim.claimant_id,
                            claim.claimed_at,
                            canonical_json_bytes(claim.to_payload()).decode("utf-8"),
                        ),
                    )
                except sqlite3.IntegrityError:
                    # BEGIN IMMEDIATE already serializes writers on this
                    # database file, so this branch should be unreachable in
                    # practice -- kept only as a second, independent
                    # backstop against the PRIMARY KEY constraint. Resolve
                    # exactly as if the existing-claim check above had
                    # observed the other writer's row in the first place.
                    raced_row = db.execute(
                        "SELECT * FROM ser8_execution_authorization_claims WHERE authorization_id=?",
                        (authorization.authorization_id,),
                    ).fetchone()
                    if raced_row is not None and raced_row["claimant_id"] == claimant_id:
                        db.commit()
                        return _claim_from_row(raced_row)
                    raise SER8ExecutionAuthorizationClaimConflict(
                        f"execution authorization {authorization.authorization_id} has already been "
                        "claimed by a different claimant"
                    ) from None
                db.commit()
                return claim
            except Exception:
                db.rollback()
                raise

    def get_claim(self, authorization_id: str) -> ExecutionAuthorizationClaimV1 | None:
        """Public, read-only accessor for an already-persisted claim by
        its ``authorization_id`` (the table's own primary key) --
        ``None`` if this authorization has never been claimed.
        Reconstructed independently from the persisted row via the SAME
        :func:`_claim_from_row` helper :meth:`claim` itself uses. Gives
        downstream read-only consumers (e.g. an autonomous execution
        worker resuming an already-persisted plan across a restart) a
        stable way to recover the exact claim object an execution plan
        was built from, without granting or mutating anything."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ser8_execution_authorization_claims WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        if row is None:
            return None
        return _claim_from_row(row)


def _claim_from_row(row: sqlite3.Row) -> ExecutionAuthorizationClaimV1:
    payload = json.loads(row["payload_json"])
    return ExecutionAuthorizationClaimV1(
        schema_version=payload["schema_version"],
        claim_id=payload["claim_id"],
        authorization_id=payload["authorization_id"],
        authorization_hash=payload["authorization_hash"],
        hypothesis_id=payload["hypothesis_id"],
        hypothesis_family_id=payload["hypothesis_family_id"],
        account_id=payload["account_id"],
        live_candidate_signal_id=payload["live_candidate_signal_id"],
        risk_gate_evidence_hash=payload["risk_gate_evidence_hash"],
        risk_decision_id=payload["risk_decision_id"],
        claimant_id=payload["claimant_id"],
        claimed_at=payload["claimed_at"],
    )


__all__ = [
    "SCHEMA_VERSION",
    "ExecutionAuthorizationClaimV1",
    "SER8ExecutionAuthorizationClaimConflict",
    "SER8ExecutionAuthorizationClaimControl",
    "SER8ExecutionAuthorizationClaimError",
]
