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
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.risk_manager import RiskDecision, SizedOrder
from trademind.ser8_demo_account_safety_gate import (
    DemoAccountAllowlistV1,
    DemoAccountAuthorizationV1,
    DemoAccountSafetyGateError,
    verify_demo_account_authorization,
)
from trademind.ser8_execution_authorization_claim import ExecutionAuthorizationClaimV1
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-mt5-demo-order-send-v2"
_REQUEST_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-request:v2"
_RECEIPT_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-receipt:v2"
_PLAN_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-execution-plan:v1"
_LEG_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-execution-leg:v1"

# One fixed magic number for this whole demo-executor product line -- not a
# trading parameter, only broker-side bookkeeping metadata, so this MVP
# does not derive it from anything per-hypothesis. Must match
# InpMagicNumber's default in mt5/TradeMind_Demo_Order_Executor_v1.mq5.
DEMO_EXECUTOR_MAGIC_NUMBER = 990244

DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS = 60.0

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

# Unchanged wire schema -- see the module docstring's "AUDITED, UNCHANGED
# FOUNDATIONS" section for why this stays exactly 13 columns, in this exact
# order, for every leg's request/result round trip.
REQUEST_CSV_FIELDS = (
    "claim_id", "authorization_id", "demo_account_id", "symbol", "action",
    "order_type", "volume", "price", "sl", "tp", "magic", "comment", "request_hash",
)
RESULT_CSV_FIELDS = (
    "claim_id", "demo_account_id", "symbol", "retcode", "retcode_description",
    "order_ticket", "deal_ticket", "position_ticket", "filled_volume", "filled_price",
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
    but are NOT part of the fixed 13-column wire CSV schema (see
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

        object.__setattr__(
            self,
            "request_hash",
            sha256_bytes(_REQUEST_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
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

        object.__setattr__(
            self,
            "leg_hash",
            sha256_bytes(_LEG_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
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
    :func:`build_demo_order_execution_plan` -- never by hand."""

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
    plan_hash: str = field(init=False)

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

        object.__setattr__(
            self,
            "plan_hash",
            sha256_bytes(_PLAN_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
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

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["plan_hash"] = self.plan_hash
        return payload


def build_demo_order_execution_plan(
    claim: ExecutionAuthorizationClaimV1,
    decision: RiskDecision,
    candidate: SignalCandidate,
    *,
    demo_authorization: DemoAccountAuthorizationV1,
) -> DemoOrderExecutionPlanV1:
    """Pure and deterministic -- no I/O, no persistence, no transport call.
    Builds the COMPLETE ordered leg plan straight from ``decision.orders``
    (Risk Manager's own sizing output, the sole sizing authority) and
    ``candidate.plan`` (the already-bound SL/TP), in ``entry_index`` order.
    Calling this twice for the identical, untouched inputs yields a
    byte-identical ``plan_id``/``plan_hash`` and byte-identical legs --
    required for safe crash/restart resumption (requirement 9)."""
    total_legs = len(decision.orders)
    ordered = sorted(decision.orders, key=lambda order: order.entry_index)
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
        )
        for order in ordered
    )
    plan_id = _plan_id(
        claim_id=claim.claim_id, decision_id=decision.decision_id, candidate_signal_id=candidate.signal_id
    )
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
    )


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
            )
        except (KeyError, ValueError):
            return None


def _has_ticket(value: str | None) -> bool:
    """True iff ``value`` is a genuine, nonzero broker ticket string --
    MT5 echoes an absent ticket as either an empty string or the literal
    "0"; neither counts as a real ticket."""
    text = (value or "").strip()
    return text not in ("", "0")


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
        return {
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
        re-evaluated, re-authorized, re-claimed, or re-sent, even across
        a process restart. ``plan_id`` is itself content-derived from
        ``(claim_id, decision_id, candidate_signal_id)``, so a fresh risk
        re-evaluation after a live account/market snapshot has moved on
        can legitimately compute a DIFFERENT ``decision_id`` for the
        "same" candidate; calling :meth:`send` again in that case would
        risk treating an already-attempted leg as brand new. Checking
        this accessor FIRST, before any fresh risk/authorize/claim
        attempt, is what makes that unsafe path unreachable -- once any
        plan exists for a candidate, an autonomous caller must only
        observe it (e.g. via :meth:`list_leg_ids_for_claim` +
        :meth:`get_leg_receipt`), never resend it. When more than one
        plan somehow exists for the same candidate, the most recently
        created one is returned. Never touches the transport."""
        with self._connect() as db:
            row = db.execute(
                "SELECT claim_id FROM ser8_mt5_demo_order_plans WHERE candidate_signal_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (candidate_signal_id,),
            ).fetchone()
        return row["claim_id"] if row is not None else None

    def list_filled_leg_ids_for_account(self, demo_account_id: str) -> tuple[str, ...]:
        """Every leg identity, across ALL claims, currently persisted
        FILLED for this demo account -- the generic discovery entrypoint
        an outcome-capture bridge needs (never hard-codes a specific
        claim or ticket), mirroring :meth:`list_pending_leg_ids_for_account`
        exactly. Read-only; never touches the transport."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT claim_id, payload_json, request_json FROM ser8_mt5_demo_order_leg_receipts "
                "WHERE payload_json IS NOT NULL ORDER BY entry_index"
            ).fetchall()
        filled: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("result_state") != "FILLED":
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
        """
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
        plan = build_demo_order_execution_plan(claim, decision, candidate, demo_authorization=demo_authorization)

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
                **common_kwargs,
            )
            leg_receipts.append(receipt)

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


__all__ = [
    "DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS",
    "DEMO_EXECUTOR_MAGIC_NUMBER",
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
    "SER8DemoOrderSendControl",
    "SER8DemoOrderSendError",
    "SER8DemoOrderTransportError",
    "build_demo_order_execution_plan",
    "build_demo_order_leg_request",
    "build_demo_order_request",
    "leg_identity",
]
