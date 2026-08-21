"""SER8 MT5 Demo Order Send Adapter V2: the last step before a real,
supervised MT5 DEMO order. Turns a valid, unexpired, uniquely-claimed
``ExecutionAuthorizationClaimV1`` -- verified against a DEMO/PAPER account
allowlist -- into ONE ORDERED EXECUTION PLAN of one or more staged order
requests, sent through an injectable transport one round-trip per leg, with
every leg's broker result verified and persisted independently.

MULTI-ENTRY V2 (this module previously accepted exactly one
``RiskDecision.SizedOrder`` -- an MVP restriction, not a real trading-model
limit): ``RiskDecision.orders`` may legitimately contain N >= 1
``SizedOrder`` legs -- ``risk_manager.evaluate_risk`` already creates one
``SizedOrder`` per staged ``candidate.plan.entries`` item (a real,
intentional multi-entry TradePlan, e.g. one MARKET + two LIMIT legs scaling
into the same position), and this module now supports the COMPLETE
``RiskDecision.orders`` tuple, not just its first element.

AUDITED, UNCHANGED FOUNDATIONS THIS DESIGN RELIES ON (see this task's own
commit message for the full trace):

* ``RiskDecision.orders: tuple[SizedOrder, ...]`` and
  ``SizedOrder.entry_index``/``order_type``/``planned_price``/
  ``effective_entry_price``/``allocation``/``volume`` already exist,
  unmodified, in ``trademind.risk_manager`` -- Risk Manager is, and remains,
  the SOLE sizing authority; this module never recomputes a volume.
* ``ExecutionAuthorizationV1``/``ExecutionAuthorizationClaimV1`` already
  bind to one ``risk_decision_id`` (the WHOLE ``RiskDecision``, not one
  order) -- so ALL legs of a plan are already, structurally, bound to
  exactly one hypothesis/RiskDecision/authorization/claim/demo account/
  symbol+action plan, with ZERO changes required to
  ``ser8_execution_authorization.py`` or
  ``ser8_execution_authorization_claim.py``.
* ``mt5/TradeMind_Demo_Order_Executor_v1.mq5`` ALREADY sends BUY/SELL LIMIT
  orders (``trade.BuyLimit``/``trade.SellLimit``, alongside its existing
  MARKET/STOP dispatch) and ALREADY fails closed with a
  ``REJECTED_BY_EXECUTOR`` malformed result for any ``order_type`` outside
  {MARKET, LIMIT, STOP} -- confirmed by direct source inspection, not
  assumed. LIMIT support did not need to be added; this task makes ZERO
  changes to that file. The executor also already reads one request row,
  consumes (renames) it immediately, sends exactly one order, and writes
  exactly one result row per timer tick -- this module reuses that SAME
  one-shot file protocol UNCHANGED, once per leg, sequentially.
* The request/result CSV wire schema (``REQUEST_CSV_FIELDS``/
  ``RESULT_CSV_FIELDS``, 13 fixed columns each, positionally read by the
  unmodified executor) is UNCHANGED. Per-leg identity is carried entirely
  inside the EXISTING ``claim_id`` wire column (see ``DemoOrderRequestV1``
  below) -- the executor already treats that column as an opaque string it
  only echoes back, never interprets, so no executor change was needed to
  support it either.

PER-LEG IDENTITY (deterministic, immutable, requirement 6): every leg's
wire/persistence identity is ``claim.claim_id`` unchanged when the plan has
exactly one leg (100% backward compatible with every pre-existing
single-leg request/receipt in this codebase), or
``f"{claim.claim_id}#{entry_index}"`` when the plan has more than one leg --
a pure function of already-immutable inputs, never random, never wall-clock.
The AGGREGATE plan itself gets its own deterministic identity,
``plan_id``, derived from ``(claim_id, decision_id, candidate.signal_id)``,
plus a content hash (``plan_hash``) over every leg's own full content.

ORDER MAY BE SENT ONLY WHEN (every check below fails closed, always before
``transport.send`` is ever called for a given leg):

1-2. the claim passes the existing, unmodified demo-account safety gate
   (``verify_demo_account_authorization``) -- checked ONCE for the whole
   plan (a claim-level property, not a per-leg one).
3. the claim is not STALE at send time -- checked ONCE for the whole plan.
4. the supplied ``RiskDecision`` and ``SignalCandidate`` genuinely belong to
   this exact claim -- checked ONCE for the whole plan.
5. ``decision.orders`` is non-empty and every leg's ``order_type`` is one
   this module/executor actually supports (MARKET/LIMIT/STOP) -- an unknown
   order_type fails the ENTIRE plan closed before any leg is attempted,
   never partially.
6. for EACH leg, independently: the one-shot send-attempt guard has never
   been used for this leg's own identity before -- see ATOMIC INVARIANT.

ATOMIC INVARIANT (one leg identity -> at most one broker send attempt,
ever): this module owns two new, additive SQLite tables in the SAME
database file as ``HypothesisRegistry`` (``registry.path``):
``ser8_mt5_demo_order_plans`` (PRIMARY KEY ``plan_id``, one row per
aggregate plan, written BEFORE any leg is ever attempted -- requirement 7)
and ``ser8_mt5_demo_order_leg_receipts`` (PRIMARY KEY ``claim_id``, holding
each leg's own wire/leg identity -- one row per leg, reserved inside one
``BEGIN IMMEDIATE`` transaction before that leg's transport call, exactly
mirroring the prior single-order module's own guard, just at leg
granularity). If a row already exists for a given leg identity -- in ANY
state, including a prior failure or an unresolved ``UNKNOWN`` -- this
module refuses to call the transport again for that leg, unconditionally.
There is no "retry a failed leg" path in this module.

This module previously used one table, ``ser8_mt5_demo_order_receipts``
(``claim_id`` PRIMARY KEY, single-order MVP). A per-leg primary key is
required for multi-entry support, so -- per this repository's standing rule
to never modify an already-closed schema in place -- this module now
writes exclusively to the two NEW, additive tables above; the old table is
left untouched (not dropped, not migrated) rather than mutated in place.
No other module in this repository reads it.

CRASH/RESTART SAFETY (requirement 9): if the transport itself fails
(raises, times out, or the terminal never responds) after a leg's guard row
was already reserved, that leg's row is updated to
``result_state="UNKNOWN"`` and the WHOLE PLAN's aggregate outcome becomes
``PENDING_RECONCILIATION`` -- every leg AFTER the UNKNOWN one is left
entirely un-attempted (no row is created for it at all), and this module
never automatically resends or automatically continues past an UNKNOWN
leg. If a prior process crashed strictly BETWEEN reserving a leg's attempt
and recording its outcome (a reserved row with a NULL payload), the next
call self-heals that ambiguity into an explicit ``UNKNOWN`` receipt --
WITHOUT ever calling the transport again -- rather than leaving it silently
NULL forever. Legs already resolved (FILLED, or a DEFINITE non-fill like
REJECTED/REQUOTE/PARTIAL_FILL/MALFORMED) are never re-attempted on a resume
call either; the plan simply continues from wherever it left off. Manual
reconciliation of an ``UNKNOWN`` outcome (checking the real MT5 terminal/
account by hand) is the only way to unblock the legs after it; this module
does not attempt it.

PARTIAL EXECUTION (requirement 10): this module NEVER reports aggregate
success unless every leg's own result_state is FILLED. The exact per-leg
outcome is always persisted individually. ``SER8DemoOrderSendControl.send``
distinguishes five aggregate outcomes:

  * COMPLETE               -- every leg FILLED. Returned as a value (never
                               an exception).
  * ACCEPTED_PENDING       -- every leg is either FILLED or a genuine,
                               broker-ACCEPTED PENDING working LIMIT/STOP
                               order (e.g. FILLED MARKET + PENDING LIMIT +
                               PENDING LIMIT) -- never a failure, never
                               ambiguous, just not yet complete. Raises
                               :class:`SER8DemoOrderPendingError`. PENDING
                               is never treated as FILLED (requirement 3);
                               advancing a PENDING leg to a terminal state
                               later requires
                               :meth:`SER8DemoOrderSendControl.
                               reconcile_pending_leg` with fresh,
                               authoritative evidence (requirement 8) --
                               never automatic, never a resend.
  * PARTIAL                -- at least one leg FILLED and at least one leg
                               has a DEFINITE non-fill outcome (REJECTED/
                               REQUOTE/PARTIAL_FILL/MALFORMED), with no
                               UNKNOWN among them. Raises
                               :class:`SER8DemoOrderPartialExecutionError`.
  * FAILED                 -- every attempted leg has a DEFINITE non-fill
                               outcome (zero FILLED, zero PENDING, zero
                               UNKNOWN). Raises
                               :class:`SER8DemoOrderRejectedError` (the
                               SAME exception type this module has always
                               raised for a single-leg non-fill -- for a
                               one-leg plan, FAILED and "the leg was
                               rejected" are the same thing).
  * PENDING_RECONCILIATION -- at least one leg's outcome is UNKNOWN (the
                               broker outcome was never even learned, e.g.
                               a transport failure -- distinct from
                               ACCEPTED_PENDING, where the outcome IS
                               known: the broker accepted a working
                               order). Raises
                               :class:`SER8DemoOrderReconciliationRequiredError`
                               (a subclass of the existing
                               :class:`SER8DemoOrderTransportError`, so
                               every pre-existing single-leg
                               ``pytest.raises(SER8DemoOrderTransportError)``
                               caller keeps working unchanged).

BACKWARD COMPATIBILITY (requirement 14): for a single-leg plan
(``len(decision.orders) == 1``, the ONLY shape this module previously
supported), ``send()`` returns the SAME :class:`DemoOrderExecutionReceiptV1`
as before -- unwrapped, byte-for-byte the same fields, same
``claim_id == claim.claim_id`` -- and raises the SAME exception types
(:class:`SER8DemoOrderRejectedError` for a non-fill,
:class:`SER8DemoOrderTransportError` for a transport failure,
:class:`SER8DemoOrderAlreadyAttemptedError` for any repeat call) with the
same messages. For a multi-leg plan (N > 1), ``send()`` returns a NEW
:class:`DemoOrderExecutionPlanReceiptV1` aggregate wrapping one
:class:`DemoOrderExecutionReceiptV1` per leg.

This module never imports MetaTrader5 and never talks to a broker
directly. The one MT5-shaped side effect this whole chain can ever have --
an actual ``OrderSend``/``OrderSendAsync`` call -- happens exclusively
inside the companion, already-compiled MQL5 executor
(mt5/TradeMind_Demo_Order_Executor_v1.mq5) running on the real Windows
terminal; this module only writes one-shot request files for it (one per
leg, sequentially) and reads back its one-shot result files, through the
SAME kind of common-files CSV exchange every other MT5 integration in this
repository already uses.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.risk_manager import PendingRiskReservation, RiskDecision, SizedOrder
from trademind.ser8_demo_account_safety_gate import (
    DemoAccountAllowlistV1,
    DemoAccountAuthorizationV1,
    DemoAccountSafetyGateError,
    verify_demo_account_authorization,
)
from trademind.ser8_execution_authorization import ExecutionAuthorizationV1
from trademind.ser8_execution_authorization_claim import ExecutionAuthorizationClaimV1
from trademind.ser8_execution_plan_outcome import execution_plan_outcome_from_payload
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-mt5-demo-order-send-v2"
_REQUEST_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-request:v2"
_RECEIPT_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-receipt:v2"
_PLAN_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-execution-plan:v1"
_LEG_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-execution-leg:v1"
_RESUME_AUTHORITY_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-resume-authority:v1"

# One fixed magic number for this whole demo-executor product line -- not a
# trading parameter, only broker-side bookkeeping metadata, so this MVP
# does not derive it from anything per-hypothesis. Must match
# InpMagicNumber's default in mt5/TradeMind_Demo_Order_Executor_v1.mq5.
DEMO_EXECUTOR_MAGIC_NUMBER = 990244

DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS = 60.0

# The standing, never-weakened signal-freshness ceiling this whole
# codebase enforces (see trademind.risk_manager.RiskProfile.
# maximum_signal_age_seconds's own default and every SER8 task's
# standing "signal freshness = 900 sec, never weakened" rule). Reused
# here as a FIXED bound for durable plan-resume eligibility (SER8
# DURABLE PARTIAL PLAN RESUME CONTRACT V1) -- deliberately NOT read from
# a live, possibly-different risk profile file at resume time, so a
# later configuration change can never silently widen how long an
# already-persisted plan remains resumable.
DURABLE_RESUME_SIGNAL_FRESHNESS_CEILING_SECONDS = 900.0

VALID_ACTIONS = {"BUY", "SELL"}
# Confirmed by direct inspection of mt5/TradeMind_Demo_Order_Executor_v1.mq5
# (ProcessPendingRequest's order_type dispatch): all three are ALREADY
# implemented via CTrade (Buy/Sell, BuyLimit/SellLimit, BuyStop/SellStop);
# anything outside this set is ALREADY rejected by the executor itself
# (REJECTED_BY_EXECUTOR malformed result, no order sent). This module
# mirrors that same closed set so an unsupported order_type fails closed
# here too, before a single byte is written to the request file.
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP"}

# MT5's own retcode constants (TRADE_RETCODE_DONE / TRADE_RETCODE_REQUOTE).
# Referenced by value, not imported, since this module never imports
# MetaTrader5.
_RETCODE_DONE = 10009
_RETCODE_REQUOTE = 10004

REQUEST_CSV_FIELDS = (
    "claim_id", "authorization_id", "demo_account_id", "symbol", "action",
    "order_type", "volume", "price", "sl", "tp", "magic", "comment", "expires_at_epoch", "request_hash",
)
RESULT_CSV_FIELDS = (
    "claim_id", "demo_account_id", "symbol", "retcode", "retcode_description",
    "order_ticket", "deal_ticket", "position_ticket", "filled_volume", "filled_price",
    "broker_send_performed",
)

# FILLED/REJECTED/REQUOTE/PARTIAL_FILL/MALFORMED/CANCELLED are terminal --
# a leg in one of these states is never touched again by this module.
# PENDING and UNKNOWN are NOT terminal: PENDING means the broker genuinely
# accepted a working LIMIT/STOP order that has not yet triggered (requires
# reconcile_pending_leg + authoritative evidence to advance -- requirement
# 8); UNKNOWN means the broker outcome itself was never learned (requires
# manual reconciliation before this module will touch that leg again at
# all). Both are still valid, recognized values for a persisted receipt's
# own result_state.
_TERMINAL_LEG_STATES = {"FILLED", "REJECTED", "REQUOTE", "PARTIAL_FILL", "MALFORMED", "CANCELLED", "EXPIRED"}
_NONTERMINAL_LEG_STATES = {"PENDING", "UNKNOWN"}
_RECOGNIZED_LEG_STATES = _TERMINAL_LEG_STATES | _NONTERMINAL_LEG_STATES


class SER8DemoOrderSendError(RuntimeError):
    """Raised whenever any precondition for sending a demo order fails --
    always before the transport is called, EXCEPT where the module
    docstring explicitly says otherwise (a transport failure or a non-clean
    broker result, both of which are persisted first and then raised)."""


class SER8DemoOrderAlreadyAttemptedError(SER8DemoOrderSendError):
    """Raised when a send attempt already exists for this leg identity --
    in any state. This module never sends a second time for the same leg,
    regardless of the first attempt's outcome."""


class SER8DemoOrderTransportError(SER8DemoOrderSendError):
    """Raised when the transport itself fails (raises, times out, or
    returns something unparseable) -- the attempt is persisted as
    ``result_state="UNKNOWN"`` before this is raised."""


class SER8DemoOrderReconciliationRequiredError(SER8DemoOrderTransportError):
    """Raised for a multi-leg plan whose aggregate outcome is
    PENDING_RECONCILIATION -- at least one leg's broker outcome is UNKNOWN.
    A subclass of :class:`SER8DemoOrderTransportError` so every existing
    single-leg ``pytest.raises(SER8DemoOrderTransportError)`` caller
    (and any real caller catching that broader type) keeps working
    unchanged; for a genuinely single-leg plan this module raises the
    plain :class:`SER8DemoOrderTransportError` directly instead, byte-for-
    byte matching this module's pre-multi-entry behavior."""


class SER8DemoOrderRejectedError(SER8DemoOrderSendError):
    """Raised when the broker responded but the result was not a clean,
    fully-matched fill (rejection, requote, partial fill, or a malformed/
    mismatched result) -- the attempt is persisted with its real
    ``result_state`` before this is raised. For a multi-leg plan, raised
    when EVERY attempted leg has a definite non-fill outcome (the FAILED
    aggregate state)."""


class SER8DemoOrderPartialExecutionError(SER8DemoOrderRejectedError):
    """Raised for a multi-leg plan whose aggregate outcome is PARTIAL --
    at least one leg FILLED and at least one leg has a definite non-fill
    outcome, with no UNKNOWN among them. Never raised for a single-leg
    plan (partial execution is impossible with exactly one leg). A
    subclass of :class:`SER8DemoOrderRejectedError` so this is never
    mistaken for a clean success by any caller catching that broader
    type."""


class SER8DemoOrderPendingError(SER8DemoOrderSendError):
    """Raised when the broker genuinely ACCEPTED an order (a real,
    nonzero order_ticket) but it has not yet triggered a deal/fill --
    result_state ``PENDING``, or aggregate_state ``ACCEPTED_PENDING`` for
    a multi-leg plan. This is deliberately NOT a subclass of
    :class:`SER8DemoOrderRejectedError` (nothing was rejected) and NOT a
    subclass of :class:`SER8DemoOrderTransportError`/
    :class:`SER8DemoOrderReconciliationRequiredError` (the broker outcome
    is fully KNOWN, not unresolved) -- it is its own, third category, so a
    caller can never mistake "the broker accepted this and it is working"
    for either a failure or an unknown/ambiguous outcome (requirement 3:
    do not pretend pending == filled, and equally do not pretend pending
    == failed or pending == unknown). Advancing a PENDING leg to a
    terminal state later requires
    :meth:`SER8DemoOrderSendControl.reconcile_pending_leg` with fresh,
    authoritative evidence -- never automatic, never a resend."""


class SER8DemoOrderResumeWindowExpiredError(SER8DemoOrderSendError):
    """Raised by :meth:`SER8DemoOrderSendControl.resume_plan` when the
    persisted plan's own durable resume authority (``resume_until``) has
    passed -- the plan was validly created, may still have genuinely
    unattempted legs, but the bounded window during which continuing it
    is authoritatively safe has closed. Distinct from every other
    :class:`SER8DemoOrderSendError` so a caller (the autonomous worker)
    can report this exact, narrow condition (``RESUME_WINDOW_EXPIRED`` /
    an explicitly incomplete-and-expired plan) rather than folding it
    into a generic denial. Never sends anything before raising; never
    itself a bypass -- the plan simply stops there, permanently, until a
    human reviews it."""


class SER8PendingTTLExpiredError(SER8DemoOrderSendError):
    """A pending leg's immutable broker expiry has already been reached.

    Raised before the send-attempt guard and before transport invocation,
    therefore it always means zero broker sends.
    """


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8DemoOrderSendError(f"{field_name} must be a non-empty string")
    return value


def leg_identity(parent_claim_id: str, entry_index: int, total_legs: int) -> str:
    """Deterministic, immutable per-leg wire/persistence identity
    (requirement 6). Pure function of already-immutable inputs -- never
    random, never wall-clock, always reproducible across process restarts.
    Equals ``parent_claim_id`` unchanged when this is the sole leg of a
    single-leg plan (100% backward compatible with every pre-existing
    single-leg request/receipt in this codebase); otherwise
    ``f"{parent_claim_id}#{entry_index}"``, still prefixed by the true
    parent claim id for human traceability even from the raw wire CSV."""
    if total_legs == 1:
        return parent_claim_id
    return f"{parent_claim_id}#{entry_index}"


def _plan_id(*, claim_id: str, decision_id: str, candidate_signal_id: str) -> str:
    digest = sha256_bytes(
        canonical_json_bytes(
            {"claim_id": claim_id, "decision_id": decision_id, "candidate_signal_id": candidate_signal_id}
        )
    )
    return "EOP-" + digest.removeprefix("sha256:")[:16]


def _attempt_id(account_id: str, leg_id: str) -> str:
    return (
        f"EAO-{account_id}-"
        f"{sha256_bytes(canonical_json_bytes({'leg_id': leg_id})).removeprefix('sha256:')[:16]}"
    )


@dataclass(frozen=True, slots=True)
class DemoOrderRequestV1:
    """Immutable, exact MT5 order request for ONE leg. Every trading field
    (symbol, action, order_type, volume, price, sl, tp) is copied verbatim
    from the already-verified claim/decision/candidate lineage -- nothing
    here is computed, guessed, or defaulted. Construct only via
    :func:`build_demo_order_leg_request` (or, for a single-leg plan,
    :func:`build_demo_order_request`) -- never by hand.

    ``claim_id`` is this leg's own wire/round-trip identity (see
    :func:`leg_identity`) -- byte-identical to ``parent_claim_id`` for a
    single-leg plan. ``parent_claim_id`` and ``entry_index`` are carried on
    this Python object and in the persisted JSON payload for full lineage,
    but are NOT part of the fixed 14-column wire CSV schema (see
    ``REQUEST_CSV_FIELDS``) -- the executor never needs them; it only ever
    echoes ``claim_id`` back verbatim.
    """

    schema_version: str
    parent_claim_id: str
    entry_index: int
    claim_id: str
    authorization_id: str
    demo_account_id: str
    symbol: str
    action: str
    order_type: str
    volume: float
    price: float
    sl: float
    tp: float
    magic: int
    comment: str
    expires_at: str | None = None
    request_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8DemoOrderSendError("unsupported demo order request schema_version")
        for value, field_name in (
            (self.parent_claim_id, "parent_claim_id"),
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.demo_account_id, "demo_account_id"),
            (self.symbol, "symbol"),
            (self.comment, "comment"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.entry_index < 1:
            raise SER8DemoOrderSendError("entry_index must be >= 1")
        if self.action not in VALID_ACTIONS:
            raise SER8DemoOrderSendError("action must be BUY or SELL")
        if self.order_type not in VALID_ORDER_TYPES:
            raise SER8DemoOrderSendError("order_type must be MARKET, LIMIT, or STOP")
        if self.volume <= 0:
            raise SER8DemoOrderSendError("volume must be positive")
        if self.order_type != "MARKET" and self.price <= 0:
            raise SER8DemoOrderSendError("price must be positive for a LIMIT/STOP order")
        if self.sl <= 0 or self.tp <= 0:
            raise SER8DemoOrderSendError("sl and tp must both be positive")
        if type(self.magic) is not int or self.magic <= 0:
            raise SER8DemoOrderSendError("magic must be a positive integer")
        if self.expires_at is not None:
            if type(self.expires_at) is not str:
                raise SER8DemoOrderSendError("expires_at must be an ISO timestamp or None")
            try:
                expires_at = datetime.fromisoformat(self.expires_at)
            except ValueError as exc:
                raise SER8DemoOrderSendError("expires_at must be an ISO timestamp") from exc
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise SER8DemoOrderSendError("expires_at must be timezone-aware")
        if self.order_type == "MARKET" and self.expires_at is not None:
            raise SER8DemoOrderSendError("MARKET requests must not carry pending-order expires_at")

        object.__setattr__(
            self,
            "request_hash",
            sha256_bytes(_REQUEST_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "parent_claim_id": self.parent_claim_id,
            "entry_index": self.entry_index,
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "demo_account_id": self.demo_account_id,
            "symbol": self.symbol,
            "action": self.action,
            "order_type": self.order_type,
            "volume": self.volume,
            "price": self.price,
            "sl": self.sl,
            "tp": self.tp,
            "magic": self.magic,
            "comment": self.comment,
        }
        # Omit only for legacy rows/unchanged MARKET semantics.  New pending
        # requests always include this field and therefore bind it into the
        # request hash.
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        return payload

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["request_hash"] = self.request_hash
        return payload

    def to_csv_row(self) -> dict[str, str]:
        """Exactly the 13 fixed wire columns the executor reads
        positionally -- unchanged from the single-leg schema. ``claim_id``
        here is this leg's own wire identity (see class docstring)."""
        return {
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "demo_account_id": self.demo_account_id,
            "symbol": self.symbol,
            "action": self.action,
            "order_type": self.order_type,
            "volume": repr(self.volume),
            "price": repr(self.price),
            "sl": repr(self.sl),
            "tp": repr(self.tp),
            "magic": str(self.magic),
            "comment": self.comment,
            "expires_at_epoch": (
                str(int(datetime.fromisoformat(self.expires_at).timestamp()))
                if self.expires_at is not None else "0"
            ),
            "request_hash": self.request_hash,
        }


def build_demo_order_leg_request(
    claim: ExecutionAuthorizationClaimV1,
    decision: RiskDecision,
    candidate: SignalCandidate,
    sized_order: SizedOrder,
    *,
    demo_authorization: DemoAccountAuthorizationV1,
    total_legs: int,
) -> DemoOrderRequestV1:
    """Build the exact order request for ONE leg from already-verified
    lineage. ``volume``/``order_type``/``price`` come from ``sized_order``
    (one element of ``decision.orders`` -- the SAME ``SizedOrder``
    risk_manager already computed for this staged entry); ``sl``/``tp``
    come from ``candidate.plan`` (the SAME bound ``TradePlan``'s
    ``stop_price`` and nearest ``targets[0]``, applied identically to every
    leg -- the current TradePlan schema defines SL/TP at the plan level,
    shared across all staged entries, not per entry; this function does not
    invent a per-leg SL/TP the upstream schema does not have). Never
    invents or recomputes a trading parameter.
    """
    leg_id = leg_identity(claim.claim_id, sized_order.entry_index, total_legs)
    comment = f"SER8:{leg_id[-20:]}"
    return DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION,
        parent_claim_id=claim.claim_id,
        entry_index=sized_order.entry_index,
        claim_id=leg_id,
        authorization_id=claim.authorization_id,
        demo_account_id=demo_authorization.account_id,
        symbol=candidate.symbol,
        action=candidate.plan.action,
        order_type=sized_order.order_type,
        volume=sized_order.volume,
        price=sized_order.planned_price,
        sl=candidate.plan.stop_price,
        tp=candidate.plan.targets[0],
        magic=DEMO_EXECUTOR_MAGIC_NUMBER,
        comment=comment,
        expires_at=(
            (candidate.created_at.astimezone(timezone.utc) + timedelta(
                seconds=DURABLE_RESUME_SIGNAL_FRESHNESS_CEILING_SECONDS
            )).isoformat()
            if sized_order.order_type != "MARKET" else None
        ),
    )


def build_demo_order_request(
    claim: ExecutionAuthorizationClaimV1,
    decision: RiskDecision,
    candidate: SignalCandidate,
    *,
    demo_authorization: DemoAccountAuthorizationV1,
) -> DemoOrderRequestV1:
    """Backward-compatible single-leg builder (requirement 14): builds the
    request for ``decision.orders[0]`` alone, with ``total_legs=1`` so its
    wire ``claim_id`` is byte-identical to ``claim.claim_id`` -- exactly
    the same object this function has always returned. Callers are
    expected to have already checked ``len(decision.orders) == 1`` (this
    function itself does not enforce it, matching its pre-existing
    contract); for a genuine multi-leg plan use
    :func:`build_demo_order_leg_request` per leg instead, or
    :func:`build_demo_order_execution_plan` for the whole ordered plan.
    """
    return build_demo_order_leg_request(
        claim, decision, candidate, decision.orders[0], demo_authorization=demo_authorization, total_legs=1
    )


def _request_from_payload(payload: dict[str, object]) -> DemoOrderRequestV1:
    """Reconstructs the immutable original request from its own persisted
    JSON payload (never re-derived from caller-supplied objects) -- used
    by :meth:`SER8DemoOrderSendControl.reconcile_pending_leg` so a
    reconciliation call can never silently substitute a different volume/
    price/symbol/order_type than what was actually sent."""
    return DemoOrderRequestV1(
        schema_version=payload["schema_version"],
        parent_claim_id=payload["parent_claim_id"],
        entry_index=payload["entry_index"],
        claim_id=payload["claim_id"],
        authorization_id=payload["authorization_id"],
        demo_account_id=payload["demo_account_id"],
        symbol=payload["symbol"],
        action=payload["action"],
        order_type=payload["order_type"],
        volume=payload["volume"],
        price=payload["price"],
        sl=payload["sl"],
        tp=payload["tp"],
        magic=payload["magic"],
        comment=payload["comment"],
        expires_at=payload.get("expires_at"),
    )


@dataclass(frozen=True, slots=True)
class DemoOrderPlanLegV1:
    """One immutable, deterministically-identified leg of an execution
    plan. Preserves exactly: ``entry_index``, ``order_type``,
    ``planned_price``/``effective_entry_price`` (both price semantics
    ``SizedOrder`` itself distinguishes), ``allocation``, ``volume``,
    ``sl``, ``tp`` -- requirement 5."""

    entry_index: int
    leg_id: str
    order_type: str
    planned_price: float
    effective_entry_price: float
    allocation: float
    volume: float
    sl: float
    tp: float
    risk_money: float | None = None
    margin_required: float | None = None
    expires_at: str | None = None
    leg_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.entry_index < 1:
            raise SER8DemoOrderSendError("entry_index must be >= 1")
        _nonempty_str(self.leg_id, field_name="leg_id")
        if self.order_type not in VALID_ORDER_TYPES:
            raise SER8DemoOrderSendError("order_type must be MARKET, LIMIT, or STOP")
        if self.volume <= 0:
            raise SER8DemoOrderSendError("volume must be positive")
        if self.allocation <= 0 or self.allocation > 1:
            raise SER8DemoOrderSendError("allocation must be within (0, 1]")
        if self.sl <= 0 or self.tp <= 0:
            raise SER8DemoOrderSendError("sl and tp must both be positive")
        if self.risk_money is not None and self.risk_money < 0:
            raise SER8DemoOrderSendError("risk_money cannot be negative")
        if self.margin_required is not None and self.margin_required < 0:
            raise SER8DemoOrderSendError("margin_required cannot be negative")
        if self.expires_at is not None:
            try:
                parsed_expiry = datetime.fromisoformat(self.expires_at)
            except (TypeError, ValueError) as exc:
                raise SER8DemoOrderSendError("pending expires_at must be an ISO timestamp") from exc
            if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
                raise SER8DemoOrderSendError("pending expires_at must be timezone-aware")
        if self.order_type == "MARKET" and self.expires_at is not None:
            raise SER8DemoOrderSendError("MARKET plan legs must not carry pending-order expires_at")

        object.__setattr__(
            self,
            "leg_hash",
            sha256_bytes(_LEG_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        payload = {
            "entry_index": self.entry_index,
            "leg_id": self.leg_id,
            "order_type": self.order_type,
            "planned_price": self.planned_price,
            "effective_entry_price": self.effective_entry_price,
            "allocation": self.allocation,
            "volume": self.volume,
            "sl": self.sl,
            "tp": self.tp,
        }
        # Conditional keys preserve the historical hash of legacy plans
        # while binding every new plan's original risk and pending expiry.
        if self.risk_money is not None:
            payload["risk_money"] = self.risk_money
        if self.margin_required is not None:
            payload["margin_required"] = self.margin_required
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        return payload

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["leg_hash"] = self.leg_hash
        return payload


@dataclass(frozen=True, slots=True)
class DemoOrderExecutionPlanV1:
    """Immutable, deterministically-identified ordered execution plan for
    one claim (requirement 6). ``plan_id`` is a pure function of
    ``(claim_id, decision_id, candidate_signal_id)``; ``plan_hash`` is a
    content hash over every leg. Construct only via
    :func:`build_demo_order_execution_plan` -- never by hand.

    ``resume_until`` (SER8 DURABLE PARTIAL PLAN RESUME CONTRACT V1) is
    the plan's own persisted, bounded DURABLE RESUME AUTHORITY -- an ISO
    timestamp computed ONCE, at plan-creation time, from the ORIGINAL
    authorization's own expiry and the standing signal-freshness ceiling
    (never re-derived, never extended, never read from a live/possibly-
    different risk profile later). ``None`` means this plan carries no
    durable resume authority at all (e.g. a plan built without supplying
    ``authorization`` to :func:`build_demo_order_execution_plan` -- the
    legacy/single-shot-pipeline shape) and :meth:`SER8DemoOrderSendControl.
    resume_plan` refuses to resume such a plan, unconditionally.
    Deliberately excluded from ``semantic_projection``/``plan_hash``:
    this is operational metadata about WHEN continuation remains
    authorized, not part of WHAT was authorized -- mirroring every other
    wall-clock-only field excluded from an identity hash elsewhere in
    this lineage (e.g. ``SER8ResearchRiskGateEvidenceV1.evaluated_at``).

    ``hypothesis_id``/``created_at``/``resume_until`` are an ALL-OR-
    NOTHING bundle -- either every one is supplied (a plan built WITH
    durable resume authority) or none is (``resume_authority_hash`` is
    then also ``None``, and :meth:`resume_plan` refuses it
    unconditionally; SER8 DURABLE RESUME AUTHORITY INTEGRITY V1,
    requirement 9 -- a legacy/no-authority plan is never granted
    inferred resume authority).

    ``resume_authority_hash`` (SER8 DURABLE RESUME AUTHORITY INTEGRITY
    V1) is a SEPARATE, independently-computed content hash binding
    ``resume_until`` to every identity field it is meaningless without --
    ``plan_id``/``candidate_signal_id``/``hypothesis_id``/``demo_account_id``/
    ``authorization_id``/``claim_id``/``decision_id``/``plan_hash``/
    ``created_at`` -- exactly mirroring the SAME established
    reconstruct-then-compare pattern this codebase already uses for
    ``DemoOrderRequestV1.request_hash`` (see :func:`get_leg_request`'s own
    ``request.request_hash != request_payload.get("request_hash")``
    check): computed fresh, `field(init=False)`, from whatever raw values
    were just supplied, and ONLY meaningful when the CALLER (here,
    :func:`_plan_from_payload`) explicitly compares it against the
    SEPARATELY, independently persisted ``resume_authority_hash`` value
    read directly from storage. If ``resume_until`` (or any of the
    other bound fields) is altered in persisted storage without also
    recomputing this stored hash to match, the mismatch is detected at
    the very next reconstruction -- BEFORE ``resume_until`` is ever
    checked or used (requirement 2)."""

    schema_version: str
    plan_id: str
    claim_id: str
    authorization_id: str
    decision_id: str
    candidate_signal_id: str
    demo_account_id: str
    symbol: str
    action: str
    legs: tuple[DemoOrderPlanLegV1, ...]
    resume_until: str | None = None
    hypothesis_id: str | None = None
    created_at: str | None = None
    candidate_created_at: str | None = None
    correlation_group: str | None = None
    plan_hash: str = field(init=False)
    resume_authority_hash: str | None = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8DemoOrderSendError("unsupported demo order execution plan schema_version")
        for value, field_name in (
            (self.plan_id, "plan_id"),
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.decision_id, "decision_id"),
            (self.candidate_signal_id, "candidate_signal_id"),
            (self.demo_account_id, "demo_account_id"),
            (self.symbol, "symbol"),
            (self.action, "action"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.action not in VALID_ACTIONS:
            raise SER8DemoOrderSendError("action must be BUY or SELL")
        if not self.legs:
            raise SER8DemoOrderSendError("an execution plan must contain at least one leg")
        seen_indices: set[int] = set()
        for leg in self.legs:
            if leg.entry_index in seen_indices:
                raise SER8DemoOrderSendError(f"duplicate entry_index {leg.entry_index} in execution plan")
            seen_indices.add(leg.entry_index)
        if tuple(leg.entry_index for leg in self.legs) != tuple(sorted(seen_indices)):
            raise SER8DemoOrderSendError("execution plan legs must be strictly ordered by entry_index")

        for value, field_name in (
            (self.resume_until, "resume_until"),
            (self.created_at, "created_at"),
            (self.candidate_created_at, "candidate_created_at"),
        ):
            if value is not None:
                if type(value) is not str:
                    raise SER8DemoOrderSendError(f"{field_name} must be a string or None")
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError as exc:
                    raise SER8DemoOrderSendError(f"{field_name} must be an ISO timestamp") from exc
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise SER8DemoOrderSendError(f"{field_name} must be timezone-aware")
        if self.hypothesis_id is not None:
            _nonempty_str(self.hypothesis_id, field_name="hypothesis_id")
        if self.correlation_group is not None:
            _nonempty_str(self.correlation_group, field_name="correlation_group")

        # requirement 9: durable resume authority is all-or-nothing --
        # never partially inferred.
        bundle = (self.resume_until, self.hypothesis_id, self.created_at)
        if any(item is not None for item in bundle) and any(item is None for item in bundle):
            raise SER8DemoOrderSendError(
                "resume_until/hypothesis_id/created_at must be supplied together or not at all -- "
                "durable resume authority is never partially granted"
            )

        object.__setattr__(
            self,
            "plan_hash",
            sha256_bytes(_PLAN_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )

        resume_authority_hash = None
        if self.resume_until is not None:
            resume_authority_hash = _resume_authority_hash(
                plan_id=self.plan_id, candidate_signal_id=self.candidate_signal_id,
                hypothesis_id=self.hypothesis_id, account_id=self.demo_account_id,
                authorization_id=self.authorization_id, claim_id=self.claim_id, decision_id=self.decision_id,
                plan_hash=self.plan_hash, created_at=self.created_at, resume_until=self.resume_until,
            )
        object.__setattr__(self, "resume_authority_hash", resume_authority_hash)

        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "decision_id": self.decision_id,
            "candidate_signal_id": self.candidate_signal_id,
            "demo_account_id": self.demo_account_id,
            "symbol": self.symbol,
            "action": self.action,
            "legs": [leg.to_payload() for leg in self.legs],
        }
        if self.candidate_created_at is not None:
            payload["candidate_created_at"] = self.candidate_created_at
        if self.correlation_group is not None:
            payload["correlation_group"] = self.correlation_group
        return payload

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["plan_hash"] = self.plan_hash
        payload["resume_until"] = self.resume_until
        payload["hypothesis_id"] = self.hypothesis_id
        payload["created_at"] = self.created_at
        payload["resume_authority_hash"] = self.resume_authority_hash
        return payload


def _resume_authority_hash(
    *, plan_id: str, candidate_signal_id: str, hypothesis_id: str, account_id: str,
    authorization_id: str, claim_id: str, decision_id: str, plan_hash: str,
    created_at: str, resume_until: str,
) -> str:
    """Binds the durable resume deadline to every identity field it is
    meaningless without (SER8 DURABLE RESUME AUTHORITY INTEGRITY V1).
    ``plan_hash`` is itself already a content hash over every leg
    (volume/price/order_type/sl/tp), so including it here transitively
    binds resume authority to the exact authorized basket too -- a
    tampered leg invalidates this hash exactly as surely as a tampered
    ``resume_until`` does."""
    payload = {
        "execution_plan_id": plan_id,
        "candidate_signal_id": candidate_signal_id,
        "hypothesis_id": hypothesis_id,
        "account_id": account_id,
        "authorization_id": authorization_id,
        "claim_id": claim_id,
        "decision_id": decision_id,
        "plan_hash": plan_hash,
        "plan_created_at": created_at,
        "resume_until": resume_until,
    }
    return sha256_bytes(_RESUME_AUTHORITY_HASH_DOMAIN + b"\x00" + canonical_json_bytes(payload))


def _durable_resume_until(
    *, authorization: ExecutionAuthorizationV1, candidate: SignalCandidate
) -> str:
    """Computes the plan's bounded DURABLE RESUME AUTHORITY deadline --
    the minimum of every applicable persisted ORIGINAL limit: the
    authorization's own ``expires_at`` (never extended beyond it -- this
    is the SAME TTL ``authorize()`` already committed to, read back
    verbatim, not re-derived or assumed as a default) and the standing,
    never-weakened signal-freshness ceiling measured from the candidate's
    own ``created_at``. Deliberately does NOT read a live risk-profile
    file (which could differ from -- or be weakened relative to -- what
    was true when this plan was actually authorized)."""
    authorization_deadline = datetime.fromisoformat(authorization.expires_at)
    signal_deadline = candidate.created_at.astimezone(timezone.utc) + timedelta(
        seconds=DURABLE_RESUME_SIGNAL_FRESHNESS_CEILING_SECONDS
    )
    return min(authorization_deadline, signal_deadline).isoformat()


def build_demo_order_execution_plan(
    claim: ExecutionAuthorizationClaimV1,
    decision: RiskDecision,
    candidate: SignalCandidate,
    *,
    demo_authorization: DemoAccountAuthorizationV1,
    authorization: ExecutionAuthorizationV1 | None = None,
    now: datetime | None = None,
) -> DemoOrderExecutionPlanV1:
    """Pure and deterministic -- no I/O, no persistence, no transport call.
    Builds the COMPLETE ordered leg plan straight from ``decision.orders``
    (Risk Manager's own sizing output, the sole sizing authority) and
    ``candidate.plan`` (the already-bound SL/TP), in ``entry_index`` order.
    Calling this twice for the identical, untouched inputs (including the
    identical ``now``, when supplied) yields a byte-identical
    ``plan_id``/``plan_hash``/``resume_authority_hash`` and byte-identical
    legs -- required for safe crash/restart resumption (requirement 9).
    ``plan_id`` itself NEVER depends on ``now``/wall-clock creation time
    (SER8 DURABLE RESUME AUTHORITY INTEGRITY V1, requirement 7/8) -- only
    the SEPARATE ``resume_authority_hash``/``created_at`` fields do.

    ``authorization``, when supplied, MUST be the exact
    ``ExecutionAuthorizationV1`` this ``claim`` was claimed from (checked
    below) -- its own ``expires_at`` and ``hypothesis_id`` are read to
    compute and persist this plan's bounded, integrity-protected durable
    resume authority (``resume_until``/``resume_authority_hash`` -- see
    :func:`_durable_resume_until`/:func:`_resume_authority_hash`).
    Omitting it (the default) produces a plan with NO durable resume
    authority at all (``resume_until``/``hypothesis_id``/``created_at``/
    ``resume_authority_hash`` all ``None``); :meth:`SER8DemoOrderSendControl.
    resume_plan` refuses to resume such a plan unconditionally. This
    keeps every existing caller that never supplies ``authorization``
    (e.g. ``run_ser8_real_demo_pipeline.py``) byte-for-byte unaffected."""
    total_legs = len(decision.orders)
    ordered = sorted(decision.orders, key=lambda order: order.entry_index)
    pending_expires_at = (
        candidate.created_at.astimezone(timezone.utc)
        + timedelta(seconds=DURABLE_RESUME_SIGNAL_FRESHNESS_CEILING_SECONDS)
    ).isoformat()
    legs = tuple(
        DemoOrderPlanLegV1(
            entry_index=order.entry_index,
            leg_id=leg_identity(claim.claim_id, order.entry_index, total_legs),
            order_type=order.order_type,
            planned_price=order.planned_price,
            effective_entry_price=order.effective_entry_price,
            allocation=order.allocation,
            volume=order.volume,
            sl=candidate.plan.stop_price,
            tp=candidate.plan.targets[0],
            risk_money=order.risk_money,
            margin_required=order.margin_required,
            expires_at=(pending_expires_at if order.order_type != "MARKET" else None),
        )
        for order in ordered
    )
    plan_id = _plan_id(
        claim_id=claim.claim_id, decision_id=decision.decision_id, candidate_signal_id=candidate.signal_id
    )
    resume_until = None
    hypothesis_id = None
    created_at = None
    if authorization is not None:
        if authorization.authorization_id != claim.authorization_id:
            raise SER8DemoOrderSendError(
                "supplied authorization does not match the claim's own recorded authorization_id -- "
                "refusing to compute a durable resume window from mismatched lineage"
            )
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        created_at = captured_at.isoformat()
        hypothesis_id = authorization.hypothesis_id
        resume_until = _durable_resume_until(authorization=authorization, candidate=candidate)
    return DemoOrderExecutionPlanV1(
        schema_version=SCHEMA_VERSION,
        plan_id=plan_id,
        claim_id=claim.claim_id,
        authorization_id=claim.authorization_id,
        decision_id=decision.decision_id,
        candidate_signal_id=candidate.signal_id,
        demo_account_id=demo_authorization.account_id,
        symbol=candidate.symbol,
        action=candidate.plan.action,
        legs=legs,
        resume_until=resume_until,
        hypothesis_id=hypothesis_id,
        created_at=created_at,
        candidate_created_at=candidate.created_at.astimezone(timezone.utc).isoformat(),
        correlation_group=decision.correlation_group,
    )


def _plan_leg_from_payload(payload: dict[str, object]) -> DemoOrderPlanLegV1:
    return DemoOrderPlanLegV1(
        entry_index=payload["entry_index"],
        leg_id=payload["leg_id"],
        order_type=payload["order_type"],
        planned_price=payload["planned_price"],
        effective_entry_price=payload["effective_entry_price"],
        allocation=payload["allocation"],
        volume=payload["volume"],
        sl=payload["sl"],
        tp=payload["tp"],
        risk_money=payload.get("risk_money"),
        margin_required=payload.get("margin_required"),
        expires_at=payload.get("expires_at"),
    )


def _plan_from_payload(payload: dict[str, object]) -> DemoOrderExecutionPlanV1:
    """Reconstructs the immutable, already-persisted execution plan from
    its own persisted JSON payload -- never re-derived from a freshly
    re-evaluated RiskDecision/SignalCandidate. This is what makes crash/
    restart resumption of unattempted legs possible (see
    :meth:`SER8DemoOrderSendControl.resume_plan`): the plan's own frozen
    leg data (order_type/planned_price/volume/sl/tp) is EXACTLY what
    :func:`build_demo_order_execution_plan` computed from the ORIGINAL,
    genuine ALLOW decision at plan-creation time -- reading it back is not
    re-deriving or inventing anything.

    SER8 DURABLE RESUME AUTHORITY INTEGRITY V1 (requirement 2): the
    reconstructed plan's own FRESHLY recomputed ``resume_authority_hash``
    (derived from whatever ``resume_until``/``hypothesis_id``/
    ``created_at``/identity fields this payload currently holds) is
    explicitly compared against the SEPARATE, independently-persisted
    ``resume_authority_hash`` value stored alongside them in this SAME
    payload -- exactly the established reconstruct-then-compare pattern
    :func:`SER8DemoOrderSendControl.get_leg_request` already uses for
    ``request_hash``. A mismatch (any bound field altered without also
    recomputing the stored hash to match) fails closed HERE, before
    ``resume_until`` is ever read by a caller for any purpose -- observing
    a candidate's plan state or resuming it alike."""
    plan = DemoOrderExecutionPlanV1(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        claim_id=payload["claim_id"],
        authorization_id=payload["authorization_id"],
        decision_id=payload["decision_id"],
        candidate_signal_id=payload["candidate_signal_id"],
        demo_account_id=payload["demo_account_id"],
        symbol=payload["symbol"],
        action=payload["action"],
        legs=tuple(_plan_leg_from_payload(leg) for leg in payload["legs"]),
        resume_until=payload.get("resume_until"),
        hypothesis_id=payload.get("hypothesis_id"),
        created_at=payload.get("created_at"),
        candidate_created_at=payload.get("candidate_created_at"),
        correlation_group=payload.get("correlation_group"),
    )
    if plan.plan_hash != payload.get("plan_hash"):
        raise SER8DemoOrderSendError(
            f"persisted execution plan {plan.plan_id} failed its own plan_hash integrity check"
        )
    if plan.resume_authority_hash != payload.get("resume_authority_hash"):
        raise SER8DemoOrderSendError(
            f"persisted execution plan {plan.plan_id}'s durable resume authority failed its own "
            "integrity check -- resume_until (or an identity field it is bound to) does not match "
            "its own persisted resume_authority_hash; refusing to trust it for any purpose"
        )
    return plan


@dataclass(frozen=True, slots=True)
class DemoOrderTransportResult:
    """The broker's raw, unclassified response to one order request,
    exactly as the executor reported it. Carries no interpretation --
    :func:`_classify_result` decides FILLED/REJECTED/REQUOTE/PARTIAL_FILL/
    MALFORMED from these fields."""

    claim_id: str
    demo_account_id: str
    symbol: str
    retcode: int
    retcode_description: str
    order_ticket: str
    deal_ticket: str
    position_ticket: str
    filled_volume: float | None
    filled_price: float | None
    broker_send_performed: bool | None = None


class DemoOrderTransport(Protocol):
    def send(self, request: DemoOrderRequestV1) -> DemoOrderTransportResult: ...


@dataclass(slots=True)
class FakeDemoOrderTransport:
    """Injectable, deterministic transport for tests. Never touches a
    file, a network socket, or MetaTrader5. Records every call it receives
    so tests can assert exactly-once-per-leg-call behavior."""

    result_factory: object = None
    calls: list[DemoOrderRequestV1] = field(default_factory=list)

    def send(self, request: DemoOrderRequestV1) -> DemoOrderTransportResult:
        self.calls.append(request)
        if self.result_factory is None:
            raise SER8DemoOrderTransportError("FakeDemoOrderTransport has no configured result")
        return self.result_factory(request)


class FileBridgeDemoOrderTransport:
    """Production-capable transport: writes one request CSV row into the
    same MT5 common-files folder every other exporter/executor in this
    repository already uses, then polls for a matching result CSV row
    written by mt5/TradeMind_Demo_Order_Executor_v1.mq5. Never imports
    MetaTrader5 and never talks to a broker directly -- only reads and
    writes plain CSV files. Called once per leg (sequential round trips);
    each call is independently one-shot at the file level, exactly as it
    was for the single-order MVP. Its own read/write/poll/timeout logic is
    fully testable by pre-seeding or omitting a result file; only the real
    MQL5 executor side (compiling, attaching, and actually calling
    OrderSend) requires a real Windows MT5 terminal this environment does
    not have.
    """

    def __init__(
        self,
        *,
        common_files_dir: Path,
        login: str,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        if poll_interval_seconds <= 0 or timeout_seconds <= 0:
            raise SER8DemoOrderTransportError("poll_interval_seconds and timeout_seconds must be positive")
        self.common_files_dir = Path(common_files_dir)
        self.login = _nonempty_str(login, field_name="login")
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds

    def _request_path(self) -> Path:
        return self.common_files_dir / f"ser8_demo_order_request_{self.login}.csv"

    def _result_path(self) -> Path:
        return self.common_files_dir / f"ser8_demo_order_result_{self.login}.csv"

    def send(self, request: DemoOrderRequestV1) -> DemoOrderTransportResult:
        _validate_pending_expiration(request, now=datetime.now(timezone.utc))
        self.common_files_dir.mkdir(parents=True, exist_ok=True)
        request_path = self._request_path()
        temporary = request_path.with_suffix(request_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(REQUEST_CSV_FIELDS))
            writer.writeheader()
            writer.writerow(request.to_csv_row())
        temporary.replace(request_path)

        deadline = time.monotonic() + self.timeout_seconds
        result_path = self._result_path()
        while time.monotonic() < deadline:
            if result_path.is_file():
                parsed = self._read_result(result_path, expected_claim_id=request.claim_id)
                if parsed is not None:
                    return parsed
            time.sleep(self.poll_interval_seconds)
        raise SER8DemoOrderTransportError(
            f"no matching result appeared within {self.timeout_seconds:.1f}s for leg {request.claim_id}"
        )

    @staticmethod
    def _read_result(path: Path, *, expected_claim_id: str) -> DemoOrderTransportResult | None:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
        except (OSError, UnicodeError):
            return None
        if not rows:
            return None
        row = rows[-1]
        if row.get("claim_id") != expected_claim_id:
            # Stale result left over from a previous leg/claim -- ignore
            # and keep polling; never treat someone else's result as ours.
            return None
        try:
            return DemoOrderTransportResult(
                claim_id=row["claim_id"],
                demo_account_id=row["demo_account_id"],
                symbol=row["symbol"],
                retcode=int(row["retcode"]),
                retcode_description=row.get("retcode_description", ""),
                order_ticket=row.get("order_ticket", ""),
                deal_ticket=row.get("deal_ticket", ""),
                position_ticket=row.get("position_ticket", ""),
                filled_volume=(float(row["filled_volume"]) if row.get("filled_volume") not in (None, "") else None),
                filled_price=(float(row["filled_price"]) if row.get("filled_price") not in (None, "") else None),
                broker_send_performed=(
                    row.get("broker_send_performed", "").strip().lower() in {"1", "true", "yes"}
                    if row.get("broker_send_performed", "").strip() else None
                ),
            )
        except (KeyError, ValueError):
            return None


def _has_ticket(value: str | None) -> bool:
    """True iff ``value`` is a genuine, nonzero broker ticket string --
    MT5 echoes an absent ticket as either an empty string or the literal
    "0"; neither counts as a real ticket."""
    text = (value or "").strip()
    return text not in ("", "0")


def _validate_pending_expiration(request: DemoOrderRequestV1, *, now: datetime) -> None:
    """Fail closed before file transport for every LIMIT/STOP request."""
    if request.order_type == "MARKET":
        return
    if request.expires_at is None:
        raise SER8DemoOrderSendError(
            "PENDING_EXPIRATION_UNSUPPORTED: pending request has no immutable expires_at"
        )
    deadline = datetime.fromisoformat(request.expires_at).astimezone(timezone.utc)
    captured_at = now.astimezone(timezone.utc)
    if deadline <= captured_at:
        raise SER8PendingTTLExpiredError(
            f"PENDING_TTL_EXPIRED: pending request expired at {request.expires_at}; refusing broker send"
        )


def _is_pending_placement(result: DemoOrderTransportResult) -> bool:
    """True iff ``result`` describes a genuinely broker-ACCEPTED but not
    yet triggered LIMIT/STOP order: the broker created a real order
    (order_ticket present) but no deal or position exists yet (both
    absent) and no fill price exists yet. Never true for a result that
    also carries deal/position evidence -- that combination is ambiguous
    and must fail closed as MALFORMED instead of being guessed at
    (requirement 9)."""
    has_order = _has_ticket(result.order_ticket)
    has_deal = _has_ticket(result.deal_ticket)
    has_position = _has_ticket(result.position_ticket)
    no_fill_price = result.filled_price is None or result.filled_price <= 0
    return has_order and not has_deal and not has_position and no_fill_price


def _classify_result(request: DemoOrderRequestV1, result: DemoOrderTransportResult) -> str:
    if (
        result.claim_id != request.claim_id
        or result.demo_account_id != request.demo_account_id
        or result.symbol != request.symbol
    ):
        return "MALFORMED"
    if result.retcode == _RETCODE_REQUOTE:
        return "REQUOTE"
    if result.retcode != _RETCODE_DONE:
        return "REJECTED"

    # A genuinely PENDING (broker-accepted, not yet triggered) LIMIT/STOP
    # order -- requirement 2. Real Windows evidence: retcode=10009 (DONE),
    # order_ticket set, deal_ticket=0, position_ticket=0, filled_price=0.0
    # (no fill has happened yet), filled_volume sometimes echoed as the
    # PLACED (not filled) volume. This is impossible for a MARKET request
    # (which either fills immediately or fails), so MARKET classification
    # below is completely unaffected -- requirement 1.
    if request.order_type != "MARKET" and _is_pending_placement(result):
        return "PENDING"

    if result.filled_volume is None or result.filled_price is None:
        return "MALFORMED"
    if result.filled_volume <= 0 or result.filled_price <= 0:
        return "MALFORMED"
    if abs(result.filled_volume - request.volume) > 1e-9:
        return "PARTIAL_FILL"
    return "FILLED"


@dataclass(frozen=True, slots=True)
class DemoOrderExecutionReceiptV1:
    """Immutable, auditable record of one send attempt for ONE LEG --
    persisted for EVERY outcome (clean fill, rejection, requote, partial
    fill, malformed result, or transport failure), never only for success.
    Carries the full request and result identity plus its parent plan/
    claim/entry_index lineage; carries no invented field.

    ``claim_id`` here is this LEG's own wire identity (see
    :func:`leg_identity`) -- byte-identical to ``parent_claim_id`` for a
    single-leg plan, exactly matching every pre-existing assertion in this
    codebase that this field equals ``claim.claim_id``.
    """

    schema_version: str
    plan_id: str
    parent_claim_id: str
    entry_index: int
    claim_id: str
    authorization_id: str
    demo_gate_hash: str
    request_hash: str
    attempt_id: str
    retcode: int
    retcode_description: str
    order_ticket: str
    deal_ticket: str
    position_ticket: str
    requested_volume: float
    requested_price: float
    filled_volume: float | None
    filled_price: float | None
    result_state: str
    recorded_at: str
    broker_send_performed: bool | None = None
    receipt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8DemoOrderSendError("unsupported demo order execution receipt schema_version")
        for value, field_name in (
            (self.plan_id, "plan_id"),
            (self.parent_claim_id, "parent_claim_id"),
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.demo_gate_hash, "demo_gate_hash"),
            (self.request_hash, "request_hash"),
            (self.attempt_id, "attempt_id"),
            (self.result_state, "result_state"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.entry_index < 1:
            raise SER8DemoOrderSendError("entry_index must be >= 1")
        if self.result_state not in _RECOGNIZED_LEG_STATES and self.result_state != "ATTEMPTING":
            raise SER8DemoOrderSendError(f"unrecognized result_state: {self.result_state!r}")
        if type(self.recorded_at) is not str:
            raise SER8DemoOrderSendError("recorded_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.recorded_at)
        except ValueError as exc:
            raise SER8DemoOrderSendError("recorded_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SER8DemoOrderSendError("recorded_at must be timezone-aware")

        object.__setattr__(
            self,
            "receipt_hash",
            sha256_bytes(_RECEIPT_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "parent_claim_id": self.parent_claim_id,
            "entry_index": self.entry_index,
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "demo_gate_hash": self.demo_gate_hash,
            "request_hash": self.request_hash,
            "attempt_id": self.attempt_id,
            "retcode": self.retcode,
            "retcode_description": self.retcode_description,
            "order_ticket": self.order_ticket,
            "deal_ticket": self.deal_ticket,
            "position_ticket": self.position_ticket,
            "requested_volume": self.requested_volume,
            "requested_price": self.requested_price,
            "filled_volume": self.filled_volume,
            "filled_price": self.filled_price,
            "result_state": self.result_state,
        }
        if self.broker_send_performed is not None:
            payload["broker_send_performed"] = self.broker_send_performed
        return payload

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["receipt_hash"] = self.receipt_hash
        payload["diagnostics"] = {"recorded_at": self.recorded_at}
        return payload


def _receipt_from_payload(payload: dict[str, object]) -> DemoOrderExecutionReceiptV1:
    return DemoOrderExecutionReceiptV1(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        parent_claim_id=payload["parent_claim_id"],
        entry_index=payload["entry_index"],
        claim_id=payload["claim_id"],
        authorization_id=payload["authorization_id"],
        demo_gate_hash=payload["demo_gate_hash"],
        request_hash=payload["request_hash"],
        attempt_id=payload["attempt_id"],
        retcode=payload["retcode"],
        retcode_description=payload["retcode_description"],
        order_ticket=payload["order_ticket"],
        deal_ticket=payload["deal_ticket"],
        position_ticket=payload["position_ticket"],
        requested_volume=payload["requested_volume"],
        requested_price=payload["requested_price"],
        filled_volume=payload["filled_volume"],
        filled_price=payload["filled_price"],
        result_state=payload["result_state"],
        recorded_at=payload["diagnostics"]["recorded_at"],
        broker_send_performed=payload.get("broker_send_performed"),
    )


@dataclass(frozen=True, slots=True)
class DemoOrderExecutionPlanReceiptV1:
    """Aggregate outcome of a multi-leg (N > 1) execution plan --
    distinguishes COMPLETE/PARTIAL/FAILED/PENDING_RECONCILIATION
    explicitly (requirement 10). Never constructed for a single-leg plan
    (see :meth:`SER8DemoOrderSendControl.send`'s own backward-compatible
    unwrapping)."""

    schema_version: str
    plan_id: str
    claim_id: str
    authorization_id: str
    aggregate_state: str
    leg_receipts: tuple[DemoOrderExecutionReceiptV1, ...]

    def __post_init__(self) -> None:
        if self.aggregate_state not in {
            "COMPLETE", "PARTIAL", "FAILED", "PENDING_RECONCILIATION", "ACCEPTED_PENDING",
        }:
            raise SER8DemoOrderSendError(f"unrecognized aggregate_state: {self.aggregate_state!r}")
        if not self.leg_receipts:
            raise SER8DemoOrderSendError("an execution plan receipt must contain at least one leg receipt")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "claim_id": self.claim_id,
            "authorization_id": self.authorization_id,
            "aggregate_state": self.aggregate_state,
            "leg_receipts": [leg.to_payload() for leg in self.leg_receipts],
        }


def _aggregate_state(leg_receipts: list[DemoOrderExecutionReceiptV1], *, total_legs: int) -> str:
    if any(receipt.result_state == "UNKNOWN" for receipt in leg_receipts):
        return "PENDING_RECONCILIATION"
    if len(leg_receipts) < total_legs:
        # Defensive only -- in this module's own control flow, processing
        # only ever stops before reaching every leg when an UNKNOWN outcome
        # was hit (already handled above). Never claim COMPLETE on
        # incomplete data.
        return "PENDING_RECONCILIATION"
    filled = sum(1 for receipt in leg_receipts if receipt.result_state == "FILLED")
    pending = sum(1 for receipt in leg_receipts if receipt.result_state == "PENDING")
    if filled == total_legs:
        return "COMPLETE"
    if filled + pending == total_legs:
        # Every leg is either FILLED or a genuinely broker-ACCEPTED
        # pending order -- e.g. FILLED MARKET + PENDING LIMIT + PENDING
        # LIMIT (requirement 4). Never a failure and never ambiguous:
        # distinct from PARTIAL, which means some legs definitely did NOT
        # succeed.
        return "ACCEPTED_PENDING"
    if filled == 0 and pending == 0:
        return "FAILED"
    return "PARTIAL"


class SER8DemoOrderSendControl:
    """Owns two new, additive SQLite tables in the same database file as
    ``HypothesisRegistry`` (``registry.path``). Never modifies
    ``HypothesisRegistry``'s, the claim control's, or the demo-account
    gate's own schema, tables, or semantics; never modifies the prior
    single-order module's own ``ser8_mt5_demo_order_receipts`` table
    either (left in place, unwritten, per this repository's standing rule
    against modifying already-closed schema in place)."""

    def __init__(self, *, registry: HypothesisRegistry, transport: DemoOrderTransport) -> None:
        self.registry = registry
        self.transport = transport
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
                CREATE TABLE IF NOT EXISTS ser8_mt5_demo_order_plans (
                    plan_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    candidate_signal_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    leg_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ser8_mt5_demo_order_plans_claim_idx
                    ON ser8_mt5_demo_order_plans(claim_id);
                CREATE TABLE IF NOT EXISTS ser8_mt5_demo_order_leg_receipts (
                    claim_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    parent_claim_id TEXT NOT NULL,
                    entry_index INTEGER NOT NULL,
                    attempt_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    demo_gate_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT
                );
                CREATE INDEX IF NOT EXISTS ser8_mt5_demo_order_leg_receipts_plan_idx
                    ON ser8_mt5_demo_order_leg_receipts(plan_id);
                """
            )

    def _persist_plan(self, plan: DemoOrderExecutionPlanV1, *, created_at: str) -> None:
        """Persists the WHOLE plan BEFORE any leg is ever attempted
        (requirement 7). Idempotent: ``plan_id`` is content-derived, so a
        resume call for the identical untouched inputs recomputes the same
        row and this INSERT becomes a harmless no-op."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO ser8_mt5_demo_order_plans(
                        plan_id, claim_id, authorization_id, decision_id, candidate_signal_id,
                        plan_hash, leg_count, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO NOTHING
                    """,
                    (
                        plan.plan_id, plan.claim_id, plan.authorization_id, plan.decision_id,
                        plan.candidate_signal_id, plan.plan_hash, len(plan.legs),
                        canonical_json_bytes(plan.to_payload()).decode("utf-8"), created_at,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _existing_leg_receipt(self, leg_id: str) -> DemoOrderExecutionReceiptV1 | None:
        """Returns the already-persisted outcome for this leg identity, or
        ``None`` if this leg has never been attempted. Never calls the
        transport. If a row exists but was never finalized (a prior
        process crashed strictly between reserving the attempt and
        recording its outcome), self-heals it into an explicit UNKNOWN
        receipt -- see the module docstring's CRASH/RESTART SAFETY
        section."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg_id,)
            ).fetchone()
        if row is None:
            return None
        if row["payload_json"] is not None:
            return _receipt_from_payload(json.loads(row["payload_json"]))
        return self._finalize(
            claim_id=leg_id,
            plan_id=row["plan_id"],
            parent_claim_id=row["parent_claim_id"],
            entry_index=row["entry_index"],
            authorization_id=row["authorization_id"],
            demo_gate_hash=row["demo_gate_hash"],
            request_hash=row["request_hash"],
            attempt_id=row["attempt_id"],
            result_state="UNKNOWN",
            recorded_at=row["attempted_at"],
        )

    def _reserve_leg_attempt(
        self, *, leg_id: str, plan_id: str, parent_claim_id: str, entry_index: int, attempt_id: str,
        request: DemoOrderRequestV1, demo_authorization: DemoAccountAuthorizationV1, captured_at: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT 1 FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg_id,)
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    raise SER8DemoOrderAlreadyAttemptedError(
                        f"a send attempt already exists for leg {leg_id}; refusing to send again"
                    )
                db.execute(
                    """
                    INSERT INTO ser8_mt5_demo_order_leg_receipts(
                        claim_id, plan_id, parent_claim_id, entry_index, attempt_id, authorization_id,
                        demo_gate_hash, request_json, request_hash, attempted_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        leg_id, plan_id, parent_claim_id, entry_index, attempt_id, request.authorization_id,
                        demo_authorization.gate_hash, canonical_json_bytes(request.to_payload()).decode("utf-8"),
                        request.request_hash, captured_at.isoformat(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                raise SER8DemoOrderAlreadyAttemptedError(
                    f"a send attempt already exists for leg {leg_id}; refusing to send again"
                ) from None
            except Exception:
                db.rollback()
                raise

    def _finalize(
        self, *, claim_id: str, plan_id: str, parent_claim_id: str, entry_index: int, authorization_id: str,
        demo_gate_hash: str, request_hash: str, attempt_id: str, result_state: str, recorded_at: str,
        retcode: int = -1, retcode_description: str = "", order_ticket: str = "",
        deal_ticket: str = "", position_ticket: str = "",
        requested_volume: float = 0.0, requested_price: float = 0.0,
        filled_volume: float | None = None, filled_price: float | None = None,
        broker_send_performed: bool | None = None,
    ) -> DemoOrderExecutionReceiptV1:
        receipt = DemoOrderExecutionReceiptV1(
            schema_version=SCHEMA_VERSION,
            plan_id=plan_id,
            parent_claim_id=parent_claim_id,
            entry_index=entry_index,
            claim_id=claim_id,
            authorization_id=authorization_id,
            demo_gate_hash=demo_gate_hash,
            request_hash=request_hash,
            attempt_id=attempt_id,
            retcode=retcode,
            retcode_description=retcode_description,
            order_ticket=order_ticket,
            deal_ticket=deal_ticket,
            position_ticket=position_ticket,
            requested_volume=requested_volume,
            requested_price=requested_price,
            filled_volume=filled_volume,
            filled_price=filled_price,
            result_state=result_state,
            recorded_at=recorded_at,
            broker_send_performed=broker_send_performed,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "UPDATE ser8_mt5_demo_order_leg_receipts SET payload_json=? WHERE claim_id=?",
                    (json.dumps(receipt.to_payload(), sort_keys=True), claim_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return receipt

    def reconcile_pending_leg(
        self,
        leg_id: str,
        *,
        evidence: DemoOrderTransportResult | None = None,
        cancelled: bool = False,
        terminal_order_state: str | None = None,
        now: datetime | None = None,
    ) -> DemoOrderExecutionReceiptV1:
        """Advances ONE currently-PENDING leg to FILLED/REJECTED/REQUOTE/
        MALFORMED/CANCELLED/EXPIRED using ONLY fresh, authoritative broker
        evidence supplied by the caller (requirement 8) -- NEVER auto-
        guessed, and NEVER calls the transport (this is reconciliation,
        not a resend -- requirement 5/11). Exactly ONE of the following
        must be supplied:

          * ``evidence`` -- a freshly observed :class:`DemoOrderTransportResult`
            for this SAME leg identity (e.g. a fresh read of the
            executor's own result CSV row, or a result reconstructed from
            a genuine deal-history lookup showing the pending order has
            since filled), reclassified via the same authoritative
            :func:`_classify_result` this module uses everywhere else;
          * ``cancelled=True`` -- an explicit, out-of-band confirmation
            the order was cancelled (kept for backward compatibility;
            exactly equivalent to ``terminal_order_state="CANCELLED"``);
          * ``terminal_order_state`` -- one of ``"CANCELLED"``,
            ``"EXPIRED"``, or ``"REJECTED"``, for a caller (typically the
            automatic MT5 reconciliation layer, driven by authoritative
            order-history evidence showing ``ENUM_ORDER_STATE`` values
            like ``ORDER_STATE_CANCELED``/``ORDER_STATE_EXPIRED``/
            ``ORDER_STATE_REJECTED``) that already knows the SPECIFIC
            terminal outcome, not just "not pending anymore". EXPIRED is
            the narrowest new state this module adds for exactly this --
            the existing model had no way to distinguish "the broker
            cancelled/rejected this" from "this LIMIT/STOP order simply
            timed out without ever triggering", and those are genuinely
            different, useful-to-know outcomes.

        Fails closed (requirement 9) if: no send attempt exists for this
        leg identity at all; the attempt was never finalized (still
        genuinely in-flight, not this method's concern); the leg's current
        state is anything other than PENDING (this method never moves a
        FILLED/REJECTED/REQUOTE/MALFORMED/UNKNOWN/CANCELLED/EXPIRED leg --
        once terminal, always terminal, and PENDING is the only state this
        method is willing to advance FROM); the persisted request payload
        fails its own integrity check (receipt_hash/request_hash
        recomputed and compared, never trusted at face value); the
        supplied evidence's own claim_id does not match this leg's wire
        identity; or more than one (or none) of evidence/cancelled/
        terminal_order_state was supplied. If the supplied evidence still
        shows the order as genuinely pending (nothing changed), this call
        is a harmless, idempotent no-op that returns the unchanged
        current receipt -- never an error, since "still pending" is
        itself a legitimate, expected outcome of checking.
        """
        supplied_count = sum([evidence is not None, cancelled, terminal_order_state is not None])
        if supplied_count != 1:
            raise SER8DemoOrderSendError(
                "reconcile_pending_leg requires exactly one of evidence=, cancelled=True, "
                "or terminal_order_state="
            )
        if terminal_order_state is not None and terminal_order_state not in {"CANCELLED", "EXPIRED", "REJECTED"}:
            raise SER8DemoOrderSendError(f"unsupported terminal_order_state: {terminal_order_state!r}")
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg_id,)
            ).fetchone()
        if row is None:
            raise SER8DemoOrderSendError(f"no existing send attempt found for leg {leg_id}; cannot reconcile")
        if row["payload_json"] is None:
            raise SER8DemoOrderSendError(
                f"leg {leg_id} has a reserved but never-finalized attempt; cannot reconcile an in-flight send"
            )
        current = _receipt_from_payload(json.loads(row["payload_json"]))
        if current.result_state != "PENDING":
            raise SER8DemoOrderSendError(
                f"leg {leg_id} is not PENDING (result_state={current.result_state!r}); refusing to reconcile"
            )

        request_payload = json.loads(row["request_json"])
        original_request = _request_from_payload(request_payload)
        if original_request.request_hash != request_payload.get("request_hash"):
            raise SER8DemoOrderSendError(
                f"persisted request payload for leg {leg_id} failed its own integrity check"
            )

        if cancelled or terminal_order_state is not None:
            new_state = terminal_order_state or "CANCELLED"
            transport_result: DemoOrderTransportResult | None = None
        else:
            if evidence.claim_id != leg_id:
                raise SER8DemoOrderSendError(
                    f"reconciliation evidence claim_id {evidence.claim_id!r} does not match leg {leg_id!r}"
                )
            new_state = _classify_result(original_request, evidence)
            if new_state == "PENDING":
                # Still genuinely pending -- nothing authoritative changed;
                # idempotent no-op, not an error.
                return current
            transport_result = evidence

        return self._finalize(
            claim_id=leg_id,
            plan_id=current.plan_id,
            parent_claim_id=current.parent_claim_id,
            entry_index=current.entry_index,
            authorization_id=current.authorization_id,
            demo_gate_hash=current.demo_gate_hash,
            request_hash=current.request_hash,
            attempt_id=current.attempt_id,
            result_state=new_state,
            recorded_at=captured_at.isoformat(),
            retcode=(transport_result.retcode if transport_result is not None else current.retcode),
            retcode_description=(
                transport_result.retcode_description if transport_result is not None else f"{new_state}_BY_RECONCILIATION"
            ),
            order_ticket=current.order_ticket,  # never invented/changed by reconciliation.
            deal_ticket=(transport_result.deal_ticket if transport_result is not None else current.deal_ticket),
            position_ticket=(
                transport_result.position_ticket if transport_result is not None else current.position_ticket
            ),
            requested_volume=current.requested_volume,
            requested_price=current.requested_price,
            filled_volume=(transport_result.filled_volume if transport_result is not None else None),
            filled_price=(transport_result.filled_price if transport_result is not None else None),
            broker_send_performed=(
                transport_result.broker_send_performed
                if transport_result is not None and transport_result.broker_send_performed is not None
                else current.broker_send_performed
            ),
        )

    def recover_misclassified_pending_leg(
        self,
        leg_id: str,
        *,
        evidence: DemoOrderTransportResult,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> DemoOrderExecutionReceiptV1:
        """One-time, explicit recovery path for a leg an OLDER version of
        this module (before this fix -- ``SER8 MT5 PENDING LIMIT RECEIPT +
        RECONCILIATION V1`` and its own predecessor) persisted as
        MALFORMED, when fresh, authoritative broker evidence NOW proves it
        was actually a genuinely accepted pending LIMIT/STOP order all
        along. This is exactly the real incident this path exists for:
        FILLED MARKET + two legs the pre-fix classifier persisted
        MALFORMED for retcode=10009 (DONE), a real nonzero order_ticket,
        deal_ticket=position_ticket="0", filled_price=0.0 -- the SAME
        shape :meth:`reconcile_pending_leg` now classifies PENDING for a
        FRESH send, but that method refuses to touch a leg whose
        PERSISTED state is not already PENDING (it was never meant to
        reclassify old data). This method is the narrow, explicit bridge
        for that one-time migration.

        NEVER calls the transport and NEVER resends (requirements 1/9) --
        confirmed by direct source inspection just like
        :meth:`reconcile_pending_leg`. Recovers ONLY a leg whose CURRENTLY
        PERSISTED state is exactly MALFORMED (never touches FILLED/
        REJECTED/REQUOTE/PARTIAL_FILL/UNKNOWN/CANCELLED/PENDING legs --
        requirement 8's "existing FILLED leg untouched" and every other
        terminal state stays terminal), and only when EVERY one of the
        following is independently proven, never assumed (requirement 3/4
        -- any ambiguity fails closed):

          * the persisted request's own ``order_type`` is LIMIT or STOP,
            never MARKET (a MARKET leg can never be "recovered" as
            PENDING -- there is no such thing as a pending MARKET order);
          * the persisted request payload's own integrity check passes
            (its recomputed ``request_hash`` matches what was stored);
          * the supplied evidence's ``claim_id`` exactly equals ``leg_id``;
          * if the already-persisted MALFORMED receipt itself captured a
            nonzero ``order_ticket`` (it always has, since this module has
            always persisted the raw transport fields regardless of
            result_state), the supplied evidence's ``order_ticket`` must
            match it exactly -- catching evidence that describes a
            DIFFERENT order by mistake;
          * reclassifying the evidence against the persisted request via
            the SAME authoritative :func:`_classify_result` this module
            uses everywhere else yields exactly ``"PENDING"`` -- i.e. a
            successful retcode, a real nonzero order_ticket, absent deal/
            position tickets, and a zero fill price, all at once. Any
            other reclassification (still MALFORMED, or anything else)
            means the evidence does not unambiguously prove a pending
            placement, and this method refuses to recover the leg.

        ``order_ticket`` is preserved from the leg's own already-persisted
        value whenever one exists (requirement 5) -- never invented, never
        silently replaced by the evidence's own copy of the same value.

        Idempotent (requirement 6): calling this again for a leg already
        recovered (now PENDING) with evidence consistent with what is
        already persisted is a harmless no-op that returns the current
        receipt unchanged, never a duplicate write and never an error.
        Once recovered to PENDING, the normal :meth:`reconcile_pending_leg`
        is the correct entrypoint for any further PENDING -> FILLED/
        CANCELLED/etc. transition (requirement 7).

        ``dry_run=True`` performs EVERY validation above exactly as a real
        call would -- and returns the SAME receipt that would be persisted
        -- but never calls :meth:`_finalize` (the ONLY mutating step in
        this method), so nothing is written. Used by
        ``scripts/recover_ser8_pending_limit_legs.py``'s own ``--dry-run``.
        """
        if evidence.claim_id != leg_id:
            raise SER8DemoOrderSendError(
                f"recovery evidence claim_id {evidence.claim_id!r} does not match leg {leg_id!r}"
            )

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg_id,)
            ).fetchone()
        if row is None:
            raise SER8DemoOrderSendError(f"no existing send attempt found for leg {leg_id}; cannot recover")
        if row["payload_json"] is None:
            raise SER8DemoOrderSendError(
                f"leg {leg_id} has a reserved but never-finalized attempt; cannot recover an in-flight send"
            )
        current = _receipt_from_payload(json.loads(row["payload_json"]))

        if current.result_state == "PENDING":
            # Idempotent re-run (requirement 6): this leg was already
            # recovered (or was already genuinely PENDING for some other
            # reason). A no-op ONLY if the evidence is consistent with
            # what is already persisted -- anything inconsistent still
            # fails closed rather than silently succeeding.
            if _has_ticket(current.order_ticket) and current.order_ticket != evidence.order_ticket:
                raise SER8DemoOrderSendError(
                    f"recovery evidence order_ticket {evidence.order_ticket!r} does not match the "
                    f"already-recovered order_ticket {current.order_ticket!r} for leg {leg_id}; "
                    "refusing to treat this as the same recovery"
                )
            return current
        if current.result_state != "MALFORMED":
            raise SER8DemoOrderSendError(
                f"leg {leg_id} is not MALFORMED and not already-recovered PENDING "
                f"(result_state={current.result_state!r}); this recovery path only applies to legacy "
                "misclassified MALFORMED legs -- once any other terminal state is reached it stays "
                "terminal, and a leg already PENDING for a fresh send uses reconcile_pending_leg instead"
            )

        request_payload = json.loads(row["request_json"])
        original_request = _request_from_payload(request_payload)
        if original_request.request_hash != request_payload.get("request_hash"):
            raise SER8DemoOrderSendError(
                f"persisted request payload for leg {leg_id} failed its own integrity check"
            )
        if original_request.order_type == "MARKET":
            raise SER8DemoOrderSendError(
                f"leg {leg_id} is a MARKET order; a MARKET leg can never be recovered as PENDING"
            )

        if _has_ticket(current.order_ticket) and current.order_ticket != evidence.order_ticket:
            raise SER8DemoOrderSendError(
                f"recovery evidence order_ticket {evidence.order_ticket!r} does not match the "
                f"already-persisted order_ticket {current.order_ticket!r} for leg {leg_id}; "
                "refusing to recover -- this evidence may describe a different order"
            )

        new_state = _classify_result(original_request, evidence)
        if new_state != "PENDING":
            raise SER8DemoOrderSendError(
                f"recovery evidence for leg {leg_id} does not unambiguously prove a pending "
                f"placement (reclassified as {new_state!r}, not PENDING); refusing to recover -- "
                "every condition (successful retcode, nonzero order_ticket, absent deal/position "
                "tickets, zero fill price) must hold at once"
            )

        preserved_order_ticket = current.order_ticket if _has_ticket(current.order_ticket) else evidence.order_ticket
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        finalize_kwargs = dict(
            claim_id=leg_id,
            plan_id=current.plan_id,
            parent_claim_id=current.parent_claim_id,
            entry_index=current.entry_index,
            authorization_id=current.authorization_id,
            demo_gate_hash=current.demo_gate_hash,
            request_hash=current.request_hash,
            attempt_id=current.attempt_id,
            result_state="PENDING",
            recorded_at=captured_at.isoformat(),
            retcode=evidence.retcode,
            retcode_description=evidence.retcode_description,
            order_ticket=preserved_order_ticket,
            deal_ticket=evidence.deal_ticket,
            position_ticket=evidence.position_ticket,
            requested_volume=current.requested_volume,
            requested_price=current.requested_price,
            filled_volume=evidence.filled_volume,
            filled_price=evidence.filled_price,
            broker_send_performed=(
                evidence.broker_send_performed
                if evidence.broker_send_performed is not None else current.broker_send_performed
            ),
        )
        if dry_run:
            # Every validation above already ran unchanged -- only the
            # actual write is skipped. Constructing this object touches no
            # I/O at all; only self._finalize (never called here) writes
            # to SQLite.
            return DemoOrderExecutionReceiptV1(schema_version=SCHEMA_VERSION, **finalize_kwargs)
        return self._finalize(**finalize_kwargs)

    def list_leg_ids_for_claim(self, parent_claim_id: str) -> tuple[str, ...]:
        """Every leg identity persisted under this EXACT claim root
        (``parent_claim_id`` -- never a prefix/LIKE match), in
        ``entry_index`` order. Read-only; never touches the transport.
        Gives inspection/recovery tooling (e.g.
        ``scripts/recover_ser8_pending_limit_legs.py``) a stable, public
        way to discover which legs exist for a claim without needing to
        know this module's own internal table schema."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT claim_id FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE parent_claim_id=? ORDER BY entry_index",
                (parent_claim_id,),
            ).fetchall()
        return tuple(row["claim_id"] for row in rows)

    def list_pending_leg_ids_for_account(self, demo_account_id: str) -> tuple[str, ...]:
        """Every leg identity, across ALL claims, currently persisted
        PENDING for this demo account -- the GENERIC discovery entrypoint
        automatic reconciliation needs (never hard-codes a specific claim
        or ticket). Read-only; never touches the transport. Only rows
        with a non-NULL, already-finalized ``payload_json`` are ever
        considered (a reserved-but-never-finalized attempt is not this
        method's concern -- ``send()``'s own crash-self-heal handles
        that)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT claim_id, payload_json, request_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE payload_json IS NOT NULL ORDER BY entry_index"
            ).fetchall()
        pending: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("result_state") != "PENDING":
                continue
            request_payload = json.loads(row["request_json"])
            if request_payload.get("demo_account_id") != demo_account_id:
                continue
            pending.append(row["claim_id"])
        return tuple(pending)

    def list_unknown_leg_ids_for_account(self, demo_account_id: str) -> tuple[str, ...]:
        """Every UNKNOWN leg for crash recovery; read-only discovery."""
        return tuple(
            leg_id
            for leg_id in self.list_leg_ids_for_account(demo_account_id)
            if (
                (receipt := self.get_leg_receipt(leg_id)) is not None
                and receipt.result_state == "UNKNOWN"
            )
        )

    def find_unknown_leg_by_request_identity(
        self,
        demo_account_id: str,
        *,
        symbol: str,
        action: str,
        magic: str,
        comment: str,
        volume: float,
        price: float,
    ) -> str | None:
        """Map broker truth after accept/local-persistence crash.

        Ticket cannot be used because that is exactly the field the crash
        prevented SER8 from recording.  Matching therefore requires the
        executor-owned comment plus every broker-exported request identity
        field, and succeeds only when exactly one UNKNOWN leg matches.
        """
        matches: list[str] = []
        for leg_id in self.list_unknown_leg_ids_for_account(demo_account_id):
            receipt = self.get_leg_receipt(leg_id)
            request = self.get_leg_request(leg_id)
            if receipt is None or request is None or _has_ticket(receipt.order_ticket):
                continue
            if (
                request.symbol == symbol
                and request.action == action
                and str(request.magic) == magic
                and request.comment == comment
                and abs(request.volume - volume) <= 1e-9
                and abs(request.price - price) <= 1e-9
            ):
                matches.append(leg_id)
        return matches[0] if len(matches) == 1 else None

    def reconcile_unknown_leg(
        self,
        leg_id: str,
        *,
        evidence: DemoOrderTransportResult | None = None,
        terminal_order_state: str | None = None,
        order_ticket: str | None = None,
        now: datetime | None = None,
    ) -> DemoOrderExecutionReceiptV1:
        """Recover UNKNOWN from uniquely matched authoritative broker truth.

        This never calls the transport and never resends.  It is the crash
        counterpart to :meth:`reconcile_pending_leg` for the narrow window
        where the broker accepted an order but the local result was not
        durably finalized.
        """
        if (evidence is None) == (terminal_order_state is None):
            raise SER8DemoOrderSendError(
                "reconcile_unknown_leg requires exactly one of evidence or terminal_order_state"
            )
        if terminal_order_state is not None and terminal_order_state not in {
            "CANCELLED", "EXPIRED", "REJECTED"
        }:
            raise SER8DemoOrderSendError(f"unsupported terminal_order_state: {terminal_order_state!r}")
        current = self.get_leg_receipt(leg_id)
        request = self.get_leg_request(leg_id)
        if current is None or request is None:
            raise SER8DemoOrderSendError(f"UNKNOWN recovery authority missing for leg {leg_id}")
        if current.result_state != "UNKNOWN":
            raise SER8DemoOrderSendError(
                f"leg {leg_id} is not UNKNOWN (result_state={current.result_state!r})"
            )
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if evidence is not None:
            if evidence.claim_id != leg_id:
                raise SER8DemoOrderSendError("UNKNOWN recovery evidence claim_id mismatch")
            new_state = _classify_result(request, evidence)
            if new_state in {"UNKNOWN", "MALFORMED"}:
                raise SER8DemoOrderSendError(
                    f"UNKNOWN recovery evidence remained ambiguous ({new_state})"
                )
            recovered_ticket = evidence.order_ticket
        else:
            new_state = terminal_order_state
            recovered_ticket = order_ticket or ""
            if not _has_ticket(recovered_ticket):
                raise SER8DemoOrderSendError("terminal UNKNOWN recovery requires the authoritative order_ticket")
        return self._finalize(
            claim_id=leg_id, plan_id=current.plan_id, parent_claim_id=current.parent_claim_id,
            entry_index=current.entry_index, authorization_id=current.authorization_id,
            demo_gate_hash=current.demo_gate_hash, request_hash=current.request_hash,
            attempt_id=current.attempt_id, result_state=new_state, recorded_at=captured_at.isoformat(),
            retcode=(evidence.retcode if evidence is not None else current.retcode),
            retcode_description=(
                evidence.retcode_description if evidence is not None else f"{new_state}_BY_UNKNOWN_RECOVERY"
            ),
            order_ticket=recovered_ticket,
            deal_ticket=(evidence.deal_ticket if evidence is not None else ""),
            position_ticket=(evidence.position_ticket if evidence is not None else ""),
            requested_volume=request.volume, requested_price=request.price,
            filled_volume=(evidence.filled_volume if evidence is not None else None),
            filled_price=(evidence.filled_price if evidence is not None else None),
            broker_send_performed=True,
        )

    def list_leg_ids_for_account(self, demo_account_id: str) -> tuple[str, ...]:
        """All attempted leg identities for one account, in stable order."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT claim_id, request_json FROM ser8_mt5_demo_order_leg_receipts ORDER BY attempted_at, entry_index"
            ).fetchall()
        return tuple(
            row["claim_id"]
            for row in rows
            if json.loads(row["request_json"]).get("demo_account_id") == demo_account_id
        )

    def find_leg_id_by_order_ticket(self, demo_account_id: str, order_ticket: str) -> str | None:
        """Return a unique authoritative mapping, otherwise fail closed as unmapped."""
        matches: list[str] = []
        for leg_id in self.list_leg_ids_for_account(demo_account_id):
            receipt = self.get_leg_receipt(leg_id)
            if receipt is not None and receipt.order_ticket == order_ticket:
                matches.append(leg_id)
        return matches[0] if len(matches) == 1 else None

    def list_active_execution_plans(
        self, demo_account_id: str, *, symbol: str | None = None
    ) -> tuple[DemoOrderExecutionPlanV1, ...]:
        """Plans whose whole trading idea has not durably finished.

        A plan remains active through unattempted/PENDING/UNKNOWN entry
        work, every still-open FILLED position, per-leg outcome capture,
        and final aggregate plan-outcome persistence. No timeout or mutable
        unlock flag participates in this decision.
        """
        target_symbol = symbol.upper() if symbol else None
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans ORDER BY created_at"
            ).fetchall()
            outcome_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ser8_demo_trade_outcomes'"
            ).fetchone() is not None
            outcome_payloads: dict[str, str] = {}
            if outcome_table:
                outcome_payloads = {
                    row["leg_id"]: row["payload_json"]
                    for row in db.execute(
                        "SELECT leg_id, payload_json FROM ser8_demo_trade_outcomes"
                    ).fetchall()
                }
            plan_outcome_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='ser8_demo_execution_plan_outcomes'"
            ).fetchone() is not None
            plan_outcome_payloads: dict[str, str] = {}
            if plan_outcome_table:
                plan_outcome_payloads = {
                    row["plan_id"]: row["payload_json"]
                    for row in db.execute(
                        "SELECT plan_id, payload_json FROM ser8_demo_execution_plan_outcomes"
                    ).fetchall()
                }

        # Local import avoids a module cycle: outcome capture imports this
        # send control, while this read path needs its pure payload verifier.
        from trademind.ser8_demo_trade_outcome_capture import demo_trade_outcome_from_payload

        active: list[DemoOrderExecutionPlanV1] = []
        for row in rows:
            plan = _plan_from_payload(json.loads(row["payload_json"]))
            if plan.demo_account_id != demo_account_id:
                continue
            if target_symbol is not None and plan.symbol.upper() != target_symbol:
                continue
            plan_active = False
            terminal_leg_states: list[tuple[str, str]] = []
            filled_leg_outcome_hashes: list[tuple[str, str]] = []
            for leg in plan.legs:
                receipt = self.get_leg_receipt(leg.leg_id)
                if receipt is None or receipt.result_state not in _TERMINAL_LEG_STATES:
                    plan_active = True
                    break
                terminal_leg_states.append((leg.leg_id, receipt.result_state))
                position_bearing = receipt.result_state == "FILLED" or (
                    receipt.result_state == "PARTIAL_FILL"
                    and receipt.filled_volume is not None
                    and receipt.filled_volume > 0
                )
                if position_bearing:
                    payload_json = outcome_payloads.get(leg.leg_id)
                    if payload_json is None:
                        plan_active = True
                        break
                    try:
                        outcome = demo_trade_outcome_from_payload(json.loads(payload_json))
                    except (KeyError, TypeError, ValueError, RuntimeError):
                        plan_active = True
                        break
                    if (
                        outcome.plan_id != plan.plan_id
                        or outcome.candidate_signal_id != plan.candidate_signal_id
                        or outcome.account_id != plan.demo_account_id
                        or outcome.symbol.upper() != plan.symbol.upper()
                        or outcome.terminal_reason != "CLOSED"
                        or outcome.entry_filled_volume is None
                        or outcome.closed_volume is None
                        or outcome.closed_volume < outcome.entry_filled_volume - 1e-9
                    ):
                        plan_active = True
                        break
                    filled_leg_outcome_hashes.append((leg.leg_id, outcome.outcome_hash))

            if not plan_active:
                payload_json = plan_outcome_payloads.get(plan.plan_id)
                if payload_json is None:
                    plan_active = True
                else:
                    try:
                        plan_outcome = execution_plan_outcome_from_payload(json.loads(payload_json))
                    except (KeyError, TypeError, ValueError, RuntimeError):
                        plan_active = True
                    else:
                        if (
                            plan_outcome.plan_id != plan.plan_id
                            or plan_outcome.plan_hash != plan.plan_hash
                            or plan_outcome.candidate_signal_id != plan.candidate_signal_id
                            or plan_outcome.account_id != plan.demo_account_id
                            or plan_outcome.symbol.upper() != plan.symbol.upper()
                            or plan_outcome.terminal_leg_states != tuple(terminal_leg_states)
                            or plan_outcome.filled_leg_outcome_hashes
                            != tuple(filled_leg_outcome_hashes)
                        ):
                            plan_active = True
            if plan_active:
                active.append(plan)
        return tuple(active)

    def pending_risk_reservations(self, demo_account_id: str) -> tuple[PendingRiskReservation, ...]:
        """Authoritative original risk for every PENDING/UNKNOWN leg.

        UNKNOWN is intentionally treated like a broker-active pending order:
        until reconciliation proves a terminal outcome its full reservation
        remains.  FILLED is excluded because the MT5 positions snapshot owns
        its position risk, preventing double counting during transition.
        """
        reservations: list[PendingRiskReservation] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans ORDER BY created_at"
            ).fetchall()
        for row in rows:
            plan = _plan_from_payload(json.loads(row["payload_json"]))
            if plan.demo_account_id != demo_account_id:
                continue
            for leg in plan.legs:
                receipt = self.get_leg_receipt(leg.leg_id)
                if receipt is None or receipt.result_state not in {"PENDING", "UNKNOWN"}:
                    continue
                if leg.risk_money is None or plan.correlation_group is None:
                    raise SER8DemoOrderSendError(
                        f"PENDING_RISK_AUTHORITY_MISSING: leg {leg.leg_id} has active/unknown broker "
                        "state but its legacy plan has no immutable original risk authority"
                    )
                reservations.append(
                    PendingRiskReservation(
                        leg_id=leg.leg_id,
                        symbol=plan.symbol,
                        correlation_group=plan.correlation_group,
                        risk_money=leg.risk_money,
                        margin_required=leg.margin_required,
                    )
                )
        return tuple(reservations)

    def get_leg_receipt(self, leg_id: str) -> DemoOrderExecutionReceiptV1 | None:
        """Public, read-only accessor for a leg's current persisted
        receipt (``None`` if this leg identity has never been attempted).
        Reuses the exact same lookup/self-heal logic :meth:`send` itself
        uses internally; never touches the transport."""
        return self._existing_leg_receipt(leg_id)

    def get_leg_request(self, leg_id: str) -> DemoOrderRequestV1 | None:
        """Public, read-only accessor for a leg's original persisted
        request (``None`` if this leg identity has never been attempted),
        with its own integrity re-verified (recomputed ``request_hash``
        compared against what was stored) before being returned -- never
        trusted at face value."""
        with self._connect() as db:
            row = db.execute(
                "SELECT request_json FROM ser8_mt5_demo_order_leg_receipts WHERE claim_id=?", (leg_id,)
            ).fetchone()
        if row is None:
            return None
        request_payload = json.loads(row["request_json"])
        request = _request_from_payload(request_payload)
        if request.request_hash != request_payload.get("request_hash"):
            raise SER8DemoOrderSendError(
                f"persisted request payload for leg {leg_id} failed its own integrity check"
            )
        return request

    def get_plan_claim_id_for_candidate(self, candidate_signal_id: str) -> str | None:
        """Public, read-only accessor: does an execution plan already
        exist for this ``candidate_signal_id``, and if so, which
        ``claim_id`` root does it live under? Returns ``None`` if no plan
        has ever been persisted for this candidate.

        This is the ONE-SHOT anchor an autonomous execution worker uses
        to decide whether a candidate has ALREADY produced an
        authoritative execution plan -- and must therefore never be
        RE-EVALUATED, RE-AUTHORIZED, or RE-CLAIMED, even across a process
        restart (``plan_id`` is itself content-derived from ``(claim_id,
        decision_id, candidate_signal_id)``, so a fresh risk
        re-evaluation after a live account/market snapshot has moved on
        can legitimately compute a DIFFERENT ``decision_id`` for the
        "same" candidate; calling :meth:`send` fresh in that case would
        risk treating an already-attempted leg as brand new). An existing
        plan is NOT automatically "fully processed", though -- a leg with
        no send attempt yet may still be safely resumed, but ONLY through
        :meth:`resume_plan`, never through a fresh :meth:`send` call (see
        :meth:`get_plan_for_candidate`/:meth:`resume_plan` for the full
        resumption path). When more than one plan somehow exists for the
        same candidate, the most recently created one is returned. Never
        touches the transport."""
        plan = self.get_plan_for_candidate(candidate_signal_id)
        return plan.claim_id if plan is not None else None

    def get_plan_for_candidate(self, candidate_signal_id: str) -> DemoOrderExecutionPlanV1 | None:
        """Public, read-only accessor: the full, already-persisted
        execution plan for this ``candidate_signal_id`` -- ``None`` if no
        plan has ever been persisted for this candidate. Reconstructed
        independently from the persisted row (never re-derived from a
        freshly re-evaluated RiskDecision/SignalCandidate); the returned
        object's own ``plan_hash`` re-verification is inherited
        automatically via ``DemoOrderExecutionPlanV1.__post_init__``.
        Gives an autonomous caller everything :meth:`resume_plan` needs to
        continue this plan's unattempted legs, and everything needed to
        inspect its ``authorization_id``/``decision_id``/``claim_id``
        lineage, without granting any new authorization or touching this
        table's write path. When more than one plan somehow exists for
        the same candidate, the most recently created one is returned."""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans WHERE candidate_signal_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (candidate_signal_id,),
            ).fetchone()
        if row is None:
            return None
        return _plan_from_payload(json.loads(row["payload_json"]))

    def get_plan(self, plan_id: str) -> DemoOrderExecutionPlanV1 | None:
        """Read one plan by its immutable plan identity."""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return _plan_from_payload(json.loads(row["payload_json"])) if row is not None else None

    def list_execution_plans_for_account(
        self, demo_account_id: str
    ) -> tuple[DemoOrderExecutionPlanV1, ...]:
        """Read every immutable execution plan for one account in creation order."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans ORDER BY created_at"
            ).fetchall()
        plans = tuple(_plan_from_payload(json.loads(row["payload_json"])) for row in rows)
        return tuple(plan for plan in plans if plan.demo_account_id == demo_account_id)

    def _load_plan_for_claim(self, claim_id: str) -> DemoOrderExecutionPlanV1 | None:
        """Private counterpart of :meth:`get_plan_for_candidate`, looked
        up by ``claim_id`` instead -- used internally by
        :meth:`resume_plan`, which is handed a claim, not a candidate."""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM ser8_mt5_demo_order_plans WHERE claim_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
        if row is None:
            return None
        return _plan_from_payload(json.loads(row["payload_json"]))

    def list_filled_leg_ids_for_account(self, demo_account_id: str) -> tuple[str, ...]:
        """Every position-bearing leg identity across all account claims.

        Includes clean FILLED receipts and PARTIAL_FILL receipts carrying
        positive filled volume. A missing position ticket remains discoverable
        and therefore locked fail-closed. This is the generic outcome-capture
        entrypoint; read-only, never transport-touching. The historical method
        name remains API-compatible.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT claim_id, payload_json, request_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE payload_json IS NOT NULL ORDER BY entry_index"
            ).fetchall()
        filled: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            filled_volume = payload.get("filled_volume")
            position_bearing = payload.get("result_state") == "FILLED" or (
                payload.get("result_state") == "PARTIAL_FILL"
                and isinstance(filled_volume, (int, float))
                and filled_volume > 0
            )
            if not position_bearing:
                continue
            request_payload = json.loads(row["request_json"])
            if request_payload.get("demo_account_id") != demo_account_id:
                continue
            filled.append(row["claim_id"])
        return tuple(filled)

    def send(
        self,
        claim: ExecutionAuthorizationClaimV1,
        decision: RiskDecision,
        candidate: SignalCandidate,
        *,
        allowlist: DemoAccountAllowlistV1,
        authorization: ExecutionAuthorizationV1 | None = None,
        maximum_claim_age_seconds: float = DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS,
        now: datetime | None = None,
    ) -> DemoOrderExecutionReceiptV1 | DemoOrderExecutionPlanReceiptV1:
        """Send every leg of ``decision.orders``, in ``entry_index`` order,
        or fail closed. For a single-leg plan, returns the SAME
        :class:`DemoOrderExecutionReceiptV1` this module has always
        returned and raises the SAME exception types (requirement 14). For
        a multi-leg plan, returns a :class:`DemoOrderExecutionPlanReceiptV1`
        aggregate, or raises :class:`SER8DemoOrderPartialExecutionError`/
        :class:`SER8DemoOrderReconciliationRequiredError`/
        :class:`SER8DemoOrderRejectedError` for PARTIAL/PENDING_
        RECONCILIATION/FAILED respectively. Never collapses the plan to
        its first (or any single) leg (requirement 3); never resends a leg
        that already has an attempt row, in any state (requirement 8).

        ``authorization`` (SER8 DURABLE PARTIAL PLAN RESUME CONTRACT V1),
        when supplied, must be the exact ``ExecutionAuthorizationV1``
        this ``claim`` was claimed from -- its ``expires_at`` is used to
        compute and persist this plan's bounded durable-resume deadline
        (see :func:`build_demo_order_execution_plan`/
        :func:`_durable_resume_until`), consumed later ONLY by
        :meth:`resume_plan`. Omitting it (the default, preserving every
        existing caller byte-for-byte) produces a plan with no durable
        resume authority at all -- :meth:`resume_plan` will then refuse
        to resume it, unconditionally. This parameter changes nothing
        about THIS call's own INITIAL-send semantics (the 60-second
        ``maximum_claim_age_seconds`` bound below is completely
        unaffected by it, by design -- see requirement 1)."""
        if maximum_claim_age_seconds <= 0:
            raise SER8DemoOrderSendError("maximum_claim_age_seconds must be positive")
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        # 1-2: demo account gate -- ACCEPTED lineage, claim self-
        # consistency, and allowlist membership all inherited unchanged.
        # Checked ONCE for the whole plan.
        try:
            demo_authorization = verify_demo_account_authorization(claim, allowlist=allowlist, now=captured_at)
        except DemoAccountSafetyGateError as exc:
            raise SER8DemoOrderSendError(f"demo account safety gate denied this claim: {exc}") from exc

        # 3: claim staleness -- a NEW freshness boundary for this layer.
        # Checked ONCE for the whole plan.
        claimed_at = datetime.fromisoformat(claim.claimed_at)
        claim_age = (captured_at - claimed_at).total_seconds()
        if claim_age < 0 or claim_age > maximum_claim_age_seconds:
            raise SER8DemoOrderSendError(
                f"claim age {claim_age:.1f}s is outside the allowed "
                f"{maximum_claim_age_seconds:.1f}s send window"
            )

        # 4: lineage cross-check. Checked ONCE for the whole plan.
        if type(decision) is not RiskDecision:
            raise SER8DemoOrderSendError("decision must be a genuine RiskDecision")
        if type(candidate) is not SignalCandidate:
            raise SER8DemoOrderSendError("candidate must be a genuine SignalCandidate")
        if decision.decision_id != claim.risk_decision_id:
            raise SER8DemoOrderSendError(
                "supplied RiskDecision does not match the claim's own recorded risk_decision_id"
            )
        if candidate.signal_id != claim.live_candidate_signal_id:
            raise SER8DemoOrderSendError(
                "supplied SignalCandidate does not match the claim's own recorded candidate identity"
            )

        # 5: at least one SizedOrder, and every leg's order_type is one
        # this module/executor actually supports -- the WHOLE plan fails
        # closed before any leg is attempted, never partially.
        if not decision.orders:
            raise SER8DemoOrderSendError("RiskDecision.orders is empty; nothing to send")
        for order in decision.orders:
            if order.order_type not in VALID_ORDER_TYPES:
                raise SER8DemoOrderSendError(
                    f"unsupported order_type {order.order_type!r} for entry {order.entry_index}; "
                    "refusing to send any leg of this plan"
                )

        # 6: build the deterministic, immutable aggregate execution plan --
        # pure, no side effects yet.
        plan = build_demo_order_execution_plan(
            claim, decision, candidate, demo_authorization=demo_authorization, authorization=authorization,
            now=captured_at,
        )

        # 7: persist the plan BEFORE any leg is ever attempted.
        self._persist_plan(plan, created_at=captured_at.isoformat())

        orders_by_index = {order.entry_index: order for order in decision.orders}

        # Fast-path idempotency check: if EVERY leg of this plan already has
        # an attempt recorded (in any state), there is no new work this
        # call could possibly do -- fail exactly like the pre-existing
        # single-leg "already attempted" contract (requirement 14), rather
        # than silently re-returning a stale success. A call that finds
        # only SOME legs already attempted is a genuine crash/restart
        # resume (requirement 9) and falls through to the loop below,
        # which skips already-attempted legs without re-sending them
        # (requirement 8) and only attempts whatever is genuinely new.
        already_attempted = {leg.leg_id: self._existing_leg_receipt(leg.leg_id) for leg in plan.legs}
        if all(receipt is not None for receipt in already_attempted.values()):
            raise SER8DemoOrderAlreadyAttemptedError(
                f"a send attempt already exists for claim {claim.claim_id}; refusing to send again"
            )

        leg_receipts: list[DemoOrderExecutionReceiptV1] = []
        blocked = False
        for leg in plan.legs:
            if blocked:
                break

            existing = already_attempted[leg.leg_id]
            if existing is not None:
                leg_receipts.append(existing)
                if existing.result_state == "UNKNOWN":
                    blocked = True
                continue

            sized_order = orders_by_index[leg.entry_index]
            request = build_demo_order_leg_request(
                claim, decision, candidate, sized_order,
                demo_authorization=demo_authorization, total_legs=len(plan.legs),
            )
            _validate_pending_expiration(request, now=captured_at)
            attempt_id = _attempt_id(claim.account_id, leg.leg_id)

            # 9-11: atomic one-shot send-attempt guard, reserved BEFORE the
            # transport is ever called, for THIS leg only.
            self._reserve_leg_attempt(
                leg_id=leg.leg_id, plan_id=plan.plan_id, parent_claim_id=claim.claim_id,
                entry_index=leg.entry_index, attempt_id=attempt_id, request=request,
                demo_authorization=demo_authorization, captured_at=captured_at,
            )

            common_kwargs = dict(
                claim_id=leg.leg_id,
                plan_id=plan.plan_id,
                parent_claim_id=claim.claim_id,
                entry_index=leg.entry_index,
                authorization_id=claim.authorization_id,
                demo_gate_hash=demo_authorization.gate_hash,
                request_hash=request.request_hash,
                attempt_id=attempt_id,
                recorded_at=captured_at.isoformat(),
                requested_volume=request.volume,
                requested_price=request.price,
            )

            try:
                transport_result = self.transport.send(request)
            except Exception:
                # 12-14: transport failure -- persist UNKNOWN, never
                # retry, never continue to a later leg while this one's
                # true outcome is unresolved.
                receipt = self._finalize(result_state="UNKNOWN", **common_kwargs)
                leg_receipts.append(receipt)
                blocked = True
                continue

            result_state = _classify_result(request, transport_result)
            receipt = self._finalize(
                result_state=result_state,
                retcode=transport_result.retcode,
                retcode_description=transport_result.retcode_description,
                order_ticket=transport_result.order_ticket,
                deal_ticket=transport_result.deal_ticket,
                position_ticket=transport_result.position_ticket,
                filled_volume=transport_result.filled_volume,
                filled_price=transport_result.filled_price,
                broker_send_performed=transport_result.broker_send_performed,
                **common_kwargs,
            )
            leg_receipts.append(receipt)

        return self._resolve_plan_outcome(plan, claim, leg_receipts)

    def _resolve_plan_outcome(
        self,
        plan: DemoOrderExecutionPlanV1,
        claim: ExecutionAuthorizationClaimV1,
        leg_receipts: list[DemoOrderExecutionReceiptV1],
    ) -> DemoOrderExecutionReceiptV1 | DemoOrderExecutionPlanReceiptV1:
        """Shared tail for :meth:`send` and :meth:`resume_plan`: turns a
        list of per-leg receipts (whichever mix of pre-existing and
        freshly-attempted this call produced) into the SAME return value/
        exception :meth:`send` has always produced -- extracted verbatim,
        not re-derived, so both entrypoints are provably identical in
        their outcome semantics."""
        aggregate_state = _aggregate_state(leg_receipts, total_legs=len(plan.legs))

        if len(plan.legs) == 1:
            # Backward compatible (requirement 14): unwrap to the single
            # leg's own receipt, and raise exactly the same exception
            # types every pre-existing single-leg caller has always
            # depended on.
            solo = leg_receipts[0]
            if aggregate_state == "COMPLETE":
                return solo
            if aggregate_state == "PENDING_RECONCILIATION":
                raise SER8DemoOrderTransportError(
                    f"transport failed for claim {claim.claim_id}: leg result_state={solo.result_state}"
                )
            if aggregate_state == "ACCEPTED_PENDING":
                raise SER8DemoOrderPendingError(
                    f"order for claim {claim.claim_id} was accepted by the broker (order_ticket="
                    f"{solo.order_ticket!r}) but has not yet triggered a fill; result_state=PENDING, "
                    f"not FILLED and not a failure -- reconcile with fresh evidence once it resolves"
                )
            raise SER8DemoOrderRejectedError(
                f"order result for claim {claim.claim_id} was {solo.result_state}, not a clean fill"
            )

        plan_receipt = DemoOrderExecutionPlanReceiptV1(
            schema_version=SCHEMA_VERSION,
            plan_id=plan.plan_id,
            claim_id=claim.claim_id,
            authorization_id=claim.authorization_id,
            aggregate_state=aggregate_state,
            leg_receipts=tuple(leg_receipts),
        )

        if aggregate_state == "COMPLETE":
            return plan_receipt
        leg_states = [(receipt.entry_index, receipt.result_state) for receipt in leg_receipts]
        if aggregate_state == "PENDING_RECONCILIATION":
            raise SER8DemoOrderReconciliationRequiredError(
                f"execution plan {plan.plan_id} for claim {claim.claim_id} has at least one leg with "
                f"an UNKNOWN broker outcome; refusing to resend or continue further legs "
                f"automatically -- manual reconciliation against the real MT5 account is required. "
                f"leg states: {leg_states}"
            )
        if aggregate_state == "ACCEPTED_PENDING":
            raise SER8DemoOrderPendingError(
                f"execution plan {plan.plan_id} for claim {claim.claim_id} was fully accepted by the "
                f"broker (every leg is FILLED or a genuine PENDING working order) but is not yet "
                f"complete; leg states: {leg_states}"
            )
        if aggregate_state == "PARTIAL":
            raise SER8DemoOrderPartialExecutionError(
                f"execution plan {plan.plan_id} for claim {claim.claim_id} only partially completed. "
                f"leg states: {leg_states}"
            )
        raise SER8DemoOrderRejectedError(
            f"execution plan {plan.plan_id} for claim {claim.claim_id} failed on every leg. "
            f"leg states: {leg_states}"
        )

    def resume_plan(
        self,
        claim: ExecutionAuthorizationClaimV1,
        *,
        allowlist: DemoAccountAllowlistV1,
        now: datetime | None = None,
    ) -> DemoOrderExecutionReceiptV1 | DemoOrderExecutionPlanReceiptV1:
        """Resumes an ALREADY-PERSISTED execution plan for this claim,
        attempting ONLY the legs that have no send-attempt record yet --
        using EXCLUSIVELY the plan's own already-persisted, immutable leg
        data (frozen at the original :meth:`send` call's own plan-
        creation step), NEVER a freshly re-evaluated ``RiskDecision``
        (which cannot be safely reconstructed after a process restart --
        see this module's own crash/restart-safety discussion). This is
        the ONLY authoritative way to continue a plan across a restart.
        It NEVER creates a new plan (the plan is looked up, never
        rebuilt via :func:`build_demo_order_execution_plan`) and NEVER
        creates a new claim (the SAME, already-persisted ``claim`` is
        independently re-verified here, never re-claimed).

        DURABLE RESUME AUTHORITY (SER8 DURABLE PARTIAL PLAN RESUME
        CONTRACT V1): unlike :meth:`send`, this method does NOT re-check
        the claim's own ``claimed_at`` against a 60-second freshness
        bound -- that bound governs ONLY the moment a plan is first
        created (see :meth:`send`'s own docstring, requirement 1, and
        this module's standing architectural rule: initial claim
        freshness is never weakened). An old claim being reused here is
        NOT "the claim presented as freshly valid again" -- it is proof
        that a specific, already-authorized, already-persisted plan
        exists, and continuation is governed entirely by the PLAN's own
        durable resume authority, ``plan.resume_until`` (a bounded
        deadline computed ONCE at plan-creation time from the ORIGINAL
        authorization's own ``expires_at`` and the standing 900-second
        signal-freshness ceiling -- never re-derived, never extended
        beyond either). A plan with no persisted ``resume_until``
        (created without ``authorization`` supplied to :meth:`send`) can
        never be resumed at all. A plan whose ``resume_until`` has
        passed raises :class:`SER8DemoOrderResumeWindowExpiredError` --
        FAIL CLOSED, zero sends, permanently, until a human reviews it;
        this window is never extended or silently renewed by any retry.

        Every OTHER invariant this reuses is the SAME one :meth:`send`
        itself enforces: the demo account gate, and this module's own
        established per-leg one-shot guard (``_reserve_leg_attempt``) --
        an already-attempted leg, in ANY state (including UNKNOWN), is
        NEVER resent; an UNKNOWN leg BLOCKS every leg after it from being
        attempted, exactly like a fresh :meth:`send` call.

        Raises :class:`SER8DemoOrderSendError` if no plan has ever been
        persisted for this claim (there is nothing to resume -- call
        :meth:`send` for a genuinely new claim instead), if the
        persisted plan does not belong to the exact supplied claim, or if
        the plan carries no durable resume authority at all. Raises
        :class:`SER8DemoOrderResumeWindowExpiredError` if the resume
        window has passed. Raises :class:`SER8DemoOrderAlreadyAttemptedError`
        if every leg already has a send attempt (nothing to resume).
        Otherwise raises the SAME exception types as :meth:`send` --
        :class:`SER8DemoOrderPendingError`/
        :class:`SER8DemoOrderReconciliationRequiredError`/
        :class:`SER8DemoOrderPartialExecutionError`/
        :class:`SER8DemoOrderRejectedError` -- for the SAME aggregate
        outcomes, and returns the SAME receipt types on COMPLETE.
        """
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        # 1: demo account gate -- inherited unchanged, re-verified fresh.
        # This check is stateless/timeless (see
        # ser8_demo_account_safety_gate.py's own docstring) -- it is
        # never affected by how much wall-clock time has passed.
        try:
            demo_authorization = verify_demo_account_authorization(claim, allowlist=allowlist, now=captured_at)
        except DemoAccountSafetyGateError as exc:
            raise SER8DemoOrderSendError(f"demo account safety gate denied this claim: {exc}") from exc

        # 2: the plan must already exist -- resume_plan() never creates
        # or rebuilds one.
        plan = self._load_plan_for_claim(claim.claim_id)
        if plan is None:
            raise SER8DemoOrderSendError(
                f"no execution plan has ever been persisted for claim {claim.claim_id}; nothing to "
                "resume -- call send() to create one"
            )
        if plan.claim_id != claim.claim_id or plan.authorization_id != claim.authorization_id:
            raise SER8DemoOrderSendError(
                "persisted execution plan does not belong to the exact supplied claim -- refusing to resume"
            )

        # 3: durable resume authority -- REPLACES (never re-applies) the
        # tight initial-claim-age bound; see this method's own docstring.
        if plan.resume_until is None:
            raise SER8DemoOrderSendError(
                f"plan {plan.plan_id} carries no persisted durable resume authority (resume_until); "
                "cannot safely resume -- this plan was created without an authorization supplied to send()"
            )
        resume_deadline = datetime.fromisoformat(plan.resume_until)
        if captured_at > resume_deadline:
            raise SER8DemoOrderResumeWindowExpiredError(
                f"plan {plan.plan_id}'s durable resume window expired at {plan.resume_until} "
                f"(now={captured_at.isoformat()}); refusing to send any remaining leg -- "
                "this plan is permanently incomplete until a human reviews it"
            )

        already_attempted = {leg.leg_id: self._existing_leg_receipt(leg.leg_id) for leg in plan.legs}
        if all(receipt is not None for receipt in already_attempted.values()):
            raise SER8DemoOrderAlreadyAttemptedError(
                f"every leg of plan {plan.plan_id} already has a send attempt; nothing to resume"
            )

        leg_receipts: list[DemoOrderExecutionReceiptV1] = []
        blocked = False
        for leg in plan.legs:
            if blocked:
                break

            existing = already_attempted[leg.leg_id]
            if existing is not None:
                leg_receipts.append(existing)
                if existing.result_state == "UNKNOWN":
                    blocked = True
                continue

            # Built EXCLUSIVELY from the plan's own already-persisted,
            # immutable leg + top-level fields -- never a freshly
            # re-evaluated RiskDecision/SignalCandidate/SizedOrder. This
            # is byte-identical to what build_demo_order_leg_request
            # would have produced originally: plan.legs[i]'s own
            # order_type/planned_price/volume/sl/tp ARE that original
            # computation's output, frozen at plan-creation time.
            request = DemoOrderRequestV1(
                schema_version=SCHEMA_VERSION, parent_claim_id=plan.claim_id, entry_index=leg.entry_index,
                claim_id=leg.leg_id, authorization_id=plan.authorization_id, demo_account_id=plan.demo_account_id,
                symbol=plan.symbol, action=plan.action, order_type=leg.order_type, volume=leg.volume,
                price=leg.planned_price, sl=leg.sl, tp=leg.tp, magic=DEMO_EXECUTOR_MAGIC_NUMBER,
                comment=f"SER8:{leg.leg_id[-20:]}", expires_at=leg.expires_at,
            )
            _validate_pending_expiration(request, now=captured_at)
            attempt_id = _attempt_id(claim.account_id, leg.leg_id)

            self._reserve_leg_attempt(
                leg_id=leg.leg_id, plan_id=plan.plan_id, parent_claim_id=claim.claim_id,
                entry_index=leg.entry_index, attempt_id=attempt_id, request=request,
                demo_authorization=demo_authorization, captured_at=captured_at,
            )

            common_kwargs = dict(
                claim_id=leg.leg_id,
                plan_id=plan.plan_id,
                parent_claim_id=claim.claim_id,
                entry_index=leg.entry_index,
                authorization_id=claim.authorization_id,
                demo_gate_hash=demo_authorization.gate_hash,
                request_hash=request.request_hash,
                attempt_id=attempt_id,
                recorded_at=captured_at.isoformat(),
                requested_volume=request.volume,
                requested_price=request.price,
            )

            try:
                transport_result = self.transport.send(request)
            except Exception:
                receipt = self._finalize(result_state="UNKNOWN", **common_kwargs)
                leg_receipts.append(receipt)
                blocked = True
                continue

            result_state = _classify_result(request, transport_result)
            receipt = self._finalize(
                result_state=result_state,
                retcode=transport_result.retcode,
                retcode_description=transport_result.retcode_description,
                order_ticket=transport_result.order_ticket,
                deal_ticket=transport_result.deal_ticket,
                position_ticket=transport_result.position_ticket,
                filled_volume=transport_result.filled_volume,
                filled_price=transport_result.filled_price,
                broker_send_performed=transport_result.broker_send_performed,
                **common_kwargs,
            )
            leg_receipts.append(receipt)

        return self._resolve_plan_outcome(plan, claim, leg_receipts)


__all__ = [
    "DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS",
    "DEMO_EXECUTOR_MAGIC_NUMBER",
    "DURABLE_RESUME_SIGNAL_FRESHNESS_CEILING_SECONDS",
    "REQUEST_CSV_FIELDS",
    "RESULT_CSV_FIELDS",
    "SCHEMA_VERSION",
    "DemoOrderExecutionPlanReceiptV1",
    "DemoOrderExecutionPlanV1",
    "DemoOrderExecutionReceiptV1",
    "DemoOrderPlanLegV1",
    "DemoOrderRequestV1",
    "DemoOrderTransport",
    "DemoOrderTransportResult",
    "FakeDemoOrderTransport",
    "FileBridgeDemoOrderTransport",
    "SER8DemoOrderAlreadyAttemptedError",
    "SER8DemoOrderPartialExecutionError",
    "SER8DemoOrderPendingError",
    "SER8DemoOrderReconciliationRequiredError",
    "SER8DemoOrderRejectedError",
    "SER8DemoOrderResumeWindowExpiredError",
    "SER8DemoOrderSendControl",
    "SER8DemoOrderSendError",
    "SER8DemoOrderTransportError",
    "build_demo_order_execution_plan",
    "build_demo_order_leg_request",
    "build_demo_order_request",
    "leg_identity",
]
