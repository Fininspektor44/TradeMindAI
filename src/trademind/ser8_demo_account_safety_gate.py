"""SER8 Demo Account Safety Gate V1: a small, fail-closed gate that permits
future MT5 execution ONLY for an explicitly configured DEMO/PAPER account.

This module never sends an MT5 order, never calls a broker API, and never
produces any broker side effect. ``DemoAccountAuthorizationV1`` is proof
that one specific, already-claimed execution (identified by its
``ExecutionAuthorizationClaimV1``) targets an account_id that is present in
an explicitly supplied allowlist -- nothing more. It does not itself
authorize a trade; it only narrows WHERE a future execution adapter could
ever be pointed, to accounts an operator explicitly configured as safe.

GATE MAY PRODUCE AN AUTHORIZATION ONLY WHEN (every check below fails
closed, before anything is built):

1. ``claim`` is a genuine ``ExecutionAuthorizationClaimV1`` -- not ``None``,
   not a duck-typed impostor.
2. ``allowlist`` is a genuine, explicitly supplied ``DemoAccountAllowlistV1``
   -- ``None`` (missing config) fails closed exactly like an empty one.
3. ``claim`` passes a self-consistency check: reconstructing an equivalent
   object from its own current field values (via ``dataclasses.replace``,
   which re-runs ``__post_init__`` and recomputes ``claim_hash`` fresh) must
   produce the SAME ``claim_hash`` the supplied object already carries. A
   frozen dataclass field changed via ``object.__setattr__`` after
   construction (bypassing ``__post_init__``) is exactly what this catches
   -- the stored hash goes stale while the field it should cover does not.
4. the allowlist is non-empty -- an empty allowlist denies every account,
   unconditionally; there is no way to configure "allow everything".
5. ``claim.account_id`` is present in ``allowlist.account_ids`` by exact
   string match -- no case-folding, no prefix/substring matching, no fuzzy
   comparison.

There is no override, force, or bypass parameter anywhere in this module.
A denial is always a raised :class:`DemoAccountSafetyGateError`; there is
no "denied but proceed anyway" return value.

DETERMINISM: this module is entirely stateless -- no new SQLite table, no
persistence, no one-shot/conflict tracking (an account-allowlist check has
no such concept: it is always safe to re-check the identical inputs any
number of times). :func:`verify_demo_account_authorization` is a pure
function of its inputs; the SAME ``(claim, allowlist)`` pair always
produces a ``DemoAccountAuthorizationV1`` with the identical ``gate_hash``
(``verified_at`` is the only field excluded from that hash, exactly
mirroring every other artifact in this lineage).

This module does not change the semantics of ``ExecutionAuthorizationClaimV1``,
``ExecutionAuthorizationV1``, ``SER8ResearchRiskGateEvidenceV1``,
``ResearchEligibilityArtifactV1``, ``HypothesisTradeableScopeV1``, or
``risk_manager`` -- none of those modules are imported for anything beyond
the one read-only ``ExecutionAuthorizationClaimV1`` type this gate consumes,
and none of their files are modified.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trademind.ser8_execution_authorization_claim import ExecutionAuthorizationClaimV1
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-demo-account-safety-gate-v1"
_ALLOWLIST_HASH_DOMAIN = b"trademind:ser8:demo-account-allowlist:v1"
_GATE_HASH_DOMAIN = b"trademind:ser8:demo-account-authorization:v1"


class DemoAccountSafetyGateError(RuntimeError):
    """Raised whenever an account fails the demo account safety gate --
    always before any ``DemoAccountAuthorizationV1`` is built. There is no
    other outcome for a denial: this module never returns a "denied"
    object, it only ever raises or succeeds."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DemoAccountSafetyGateError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class DemoAccountAllowlistV1:
    """Immutable, explicit configuration of approved demo/paper account
    ids. Deliberately constructible with zero entries -- this dataclass
    only validates STRUCTURE; :func:`verify_demo_account_authorization`,
    not this constructor, is responsible for denying against an empty
    allowlist, so "empty allowlist" is a directly testable gate outcome
    rather than something structurally unreachable.
    """

    account_ids: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION
    allowlist_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DemoAccountSafetyGateError("unsupported demo account allowlist schema_version")
        if type(self.account_ids) is not tuple:
            raise DemoAccountSafetyGateError("account_ids must be an immutable tuple")
        seen: set[str] = set()
        for value in self.account_ids:
            _nonempty_str(value, field_name="account_ids[]")
            if value in seen:
                raise DemoAccountSafetyGateError(f"duplicate account_id in allowlist: {value!r}")
            seen.add(value)
        object.__setattr__(self, "account_ids", tuple(sorted(self.account_ids)))

        object.__setattr__(
            self,
            "allowlist_hash",
            sha256_bytes(
                _ALLOWLIST_HASH_DOMAIN
                + b"\x00"
                + canonical_json_bytes(
                    {"schema_version": self.schema_version, "account_ids": list(self.account_ids)}
                )
            ),
        )

    def __contains__(self, account_id: object) -> bool:
        return account_id in self.account_ids


@dataclass(frozen=True, slots=True)
class DemoAccountAuthorizationV1:
    """Immutable proof that one claimed execution's account_id was found in
    an explicit demo/paper allowlist at one specific moment. Construct only
    via :func:`verify_demo_account_authorization` -- never by hand.

    Carries no order, broker, or execution field of any kind -- this is a
    narrower "where" check layered on top of an already-claimed
    authorization, not a new grant of anything.
    """

    schema_version: str
    account_id: str
    allowlist_hash: str
    claim_id: str
    authorization_id: str
    hypothesis_id: str
    verified_at: str
    gate_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DemoAccountSafetyGateError("unsupported demo account authorization schema_version")
        for value, field_name in (
            (self.account_id, "account_id"),
            (self.allowlist_hash, "allowlist_hash"),
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.hypothesis_id, "hypothesis_id"),
        ):
            _nonempty_str(value, field_name=field_name)

        if type(self.verified_at) is not str:
            raise DemoAccountSafetyGateError("verified_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.verified_at)
        except ValueError as exc:
            raise DemoAccountSafetyGateError("verified_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DemoAccountSafetyGateError("verified_at must be timezone-aware")

        object.__setattr__(
            self,
            "gate_hash",
            sha256_bytes(_GATE_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        """Excludes ``verified_at`` (wall-clock, non-deterministic) so
        verifying the identical (claim, allowlist) pair always produces an
        identical ``gate_hash``."""
        return {
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "allowlist_hash": self.allowlist_hash,
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "hypothesis_id": self.hypothesis_id,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["gate_hash"] = self.gate_hash
        payload["diagnostics"] = {"verified_at": self.verified_at}
        return payload


def verify_demo_account_authorization(
    claim: ExecutionAuthorizationClaimV1,
    *,
    allowlist: DemoAccountAllowlistV1 | None,
    now: datetime | None = None,
) -> DemoAccountAuthorizationV1:
    """Fail-closed demo/paper account check for an already-issued,
    one-shot-claimed execution. Raises :class:`DemoAccountSafetyGateError`
    for every denial listed in the module docstring; there is no override.
    """
    if type(claim) is not ExecutionAuthorizationClaimV1:
        raise DemoAccountSafetyGateError(
            "claim must be a genuine ExecutionAuthorizationClaimV1; none (or an invalid "
            "object) was supplied"
        )
    if type(allowlist) is not DemoAccountAllowlistV1:
        raise DemoAccountSafetyGateError(
            "a DemoAccountAllowlistV1 configuration is required; none was supplied"
        )

    # Self-consistency check: catches a claim mutated via object.__setattr__
    # after construction (bypassing __post_init__, leaving claim_hash stale
    # relative to whatever field was changed).
    reconstructed = dataclasses.replace(claim)
    if reconstructed.claim_hash != claim.claim_hash:
        raise DemoAccountSafetyGateError(
            "supplied claim failed self-consistency verification; refusing to trust a "
            "possibly-tampered object"
        )

    if not allowlist.account_ids:
        raise DemoAccountSafetyGateError(
            "demo account allowlist is empty; no account can ever be authorized"
        )

    if claim.account_id not in allowlist:
        raise DemoAccountSafetyGateError(
            f"account {claim.account_id!r} is not an approved demo/paper account"
        )

    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return DemoAccountAuthorizationV1(
        schema_version=SCHEMA_VERSION,
        account_id=claim.account_id,
        allowlist_hash=allowlist.allowlist_hash,
        claim_id=claim.claim_id,
        authorization_id=claim.authorization_id,
        hypothesis_id=claim.hypothesis_id,
        verified_at=captured_at.isoformat(),
    )


__all__ = [
    "SCHEMA_VERSION",
    "DemoAccountAllowlistV1",
    "DemoAccountAuthorizationV1",
    "DemoAccountSafetyGateError",
    "verify_demo_account_authorization",
]
