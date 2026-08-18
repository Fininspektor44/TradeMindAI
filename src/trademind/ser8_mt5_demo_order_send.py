"""SER8 MT5 Demo Order Send Adapter V1: the last step before a real,
supervised MT5 DEMO order. Turns a valid, unexpired, uniquely-claimed
``ExecutionAuthorizationClaimV1`` -- verified against a DEMO/PAPER account
allowlist -- into exactly one order request, sent exactly once through an
injectable transport, with the broker's result verified and persisted.

SCOPE (first demo order only): exactly one ``RiskDecision.SizedOrder``,
SL/TP taken directly from the SAME bound ``SignalCandidate.plan`` (no
separate SL/TP layer, no invented or recomputed trading parameter of any
kind), one broker send attempt per claim_id ever, and MVP-strict result
handling: only a clean, fully-matched fill is accepted as success -- every
other outcome (rejection, requote, partial fill, a malformed/mismatched
result, or a transport failure) is persisted and then raised as an error.
There is no automatic retry anywhere in this module.

This module never imports MetaTrader5 and never talks to a broker
directly. The one MT5-shaped side effect this whole chain can ever have --
an actual ``OrderSend`` call -- happens exclusively inside a companion,
already-compiled MQL5 executor (mt5/TradeMind_Demo_Order_Executor_v1.mq5)
running on the real Windows terminal; this module only writes a one-shot
request file for it and reads back its one-shot result file, through the
SAME kind of common-files CSV exchange every other MT5 integration in this
repository already uses. All of that transport logic sits behind the
``DemoOrderTransport`` protocol so the rest of this module -- lineage
verification, the one-shot send guard, result classification, and
persistence -- is fully unit-testable with an injected fake transport in
an environment (like this one) that cannot run a real MT5 terminal.

ORDER MAY BE SENT ONLY WHEN (every check below fails closed, always before
``transport.send`` is ever called):

1-2. the claim passes the existing, unmodified demo-account safety gate
   (``verify_demo_account_authorization``) -- ACCEPTED-lineage re-
   verification, claim self-consistency/tamper detection, and demo-account
   allowlist membership are all inherited from that gate unchanged.
3. the claim is not STALE at send time -- a NEW freshness boundary specific
   to this layer (``now - claim.claimed_at`` must be within
   ``maximum_claim_age_seconds``), distinct from the authorization's own
   expiry (already enforced when the claim was originally taken).
4. the supplied ``RiskDecision`` and ``SignalCandidate`` genuinely belong to
   this exact claim -- ``decision.decision_id`` and ``candidate.signal_id``
   are cross-checked against the claim's own recorded
   ``risk_decision_id``/``live_candidate_signal_id``.
5. ``decision.orders`` contains EXACTLY one ``SizedOrder`` -- this MVP
   never sends a multi-entry staged order.
6. the one-shot send-attempt guard has never been used for this claim_id
   before -- see ATOMIC INVARIANT below.

ATOMIC INVARIANT (one claim_id -> at most one broker send attempt): this
module owns one new, additive SQLite table
(``ser8_mt5_demo_order_receipts``, PRIMARY KEY ``claim_id``) in the SAME
database file as ``HypothesisRegistry`` (``registry.path``). The guard row
is reserved (state ``ATTEMPTING``) inside one ``BEGIN IMMEDIATE``
transaction, committed, and ONLY THEN is the transport ever called --
never while holding the database write lock, so a slow or hung transport
call cannot block other database readers/writers. If a row already exists
for this claim_id -- in ANY state, including a prior failure or an
unresolved ``UNKNOWN`` -- this module refuses to call the transport again,
unconditionally. There is no "retry a failed send" path in this module.

If the transport itself fails (raises, times out, or the terminal never
responds) after the guard row was already reserved, the row is updated to
``result_state="UNKNOWN"`` and the caller receives a raised error -- never
a fabricated success, and never an automatic resend. Manual reconciliation
of an ``UNKNOWN`` outcome (checking the real MT5 terminal/account by hand)
is the only way forward for that claim_id; this module does not attempt it.
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
from trademind.risk_manager import RiskDecision
from trademind.ser8_demo_account_safety_gate import (
    DemoAccountAllowlistV1,
    DemoAccountAuthorizationV1,
    DemoAccountSafetyGateError,
    verify_demo_account_authorization,
)
from trademind.ser8_execution_authorization_claim import ExecutionAuthorizationClaimV1
from trademind.signal_intelligence import SignalCandidate
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-mt5-demo-order-send-v1"
_REQUEST_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-request:v1"
_RECEIPT_HASH_DOMAIN = b"trademind:ser8:mt5-demo-order-receipt:v1"

# One fixed magic number for this whole demo-executor product line -- not a
# trading parameter, only broker-side bookkeeping metadata, so this MVP
# does not derive it from anything per-hypothesis. Must match
# InpMagicNumber's default in mt5/TradeMind_Demo_Order_Executor_v1.mq5.
DEMO_EXECUTOR_MAGIC_NUMBER = 990244

DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS = 60.0

VALID_ACTIONS = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP"}

# MT5's own retcode constants (TRADE_RETCODE_DONE / TRADE_RETCODE_REQUOTE).
# Referenced by value, not imported, since this module never imports
# MetaTrader5.
_RETCODE_DONE = 10009
_RETCODE_REQUOTE = 10004

REQUEST_CSV_FIELDS = (
    "claim_id", "authorization_id", "demo_account_id", "symbol", "action",
    "order_type", "volume", "price", "sl", "tp", "magic", "comment", "request_hash",
)
RESULT_CSV_FIELDS = (
    "claim_id", "demo_account_id", "symbol", "retcode", "retcode_description",
    "order_ticket", "deal_ticket", "position_ticket", "filled_volume", "filled_price",
)


class SER8DemoOrderSendError(RuntimeError):
    """Raised whenever any precondition for sending a demo order fails --
    always before the transport is called, EXCEPT where the module
    docstring explicitly says otherwise (a transport failure or a non-clean
    broker result, both of which are persisted first and then raised)."""


class SER8DemoOrderAlreadyAttemptedError(SER8DemoOrderSendError):
    """Raised when a send attempt already exists for this claim_id -- in
    any state. This module never sends a second time for the same claim,
    regardless of the first attempt's outcome."""


class SER8DemoOrderTransportError(SER8DemoOrderSendError):
    """Raised when the transport itself fails (raises, times out, or
    returns something unparseable) -- the attempt is persisted as
    ``result_state="UNKNOWN"`` before this is raised."""


class SER8DemoOrderRejectedError(SER8DemoOrderSendError):
    """Raised when the broker responded but the result was not a clean,
    fully-matched fill (rejection, requote, partial fill, or a malformed/
    mismatched result) -- the attempt is persisted with its real
    ``result_state`` before this is raised."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8DemoOrderSendError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class DemoOrderRequestV1:
    """Immutable, exact MT5 order request. Every trading field (symbol,
    action, order_type, volume, price, sl, tp) is copied verbatim from the
    already-verified claim/decision/candidate lineage -- nothing here is
    computed, guessed, or defaulted. Construct only via
    :func:`build_demo_order_request` -- never by hand."""

    schema_version: str
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
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.demo_account_id, "demo_account_id"),
            (self.symbol, "symbol"),
            (self.comment, "comment"),
        ):
            _nonempty_str(value, field_name=field_name)
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


def build_demo_order_request(
    claim: ExecutionAuthorizationClaimV1,
    decision: RiskDecision,
    candidate: SignalCandidate,
    *,
    demo_authorization: DemoAccountAuthorizationV1,
) -> DemoOrderRequestV1:
    """Build the exact order request from already-verified lineage. Every
    trading field is copied verbatim: ``volume``/``order_type``/``price``
    from ``decision.orders[0]`` (the SAME ``SizedOrder`` risk_manager
    already computed), ``sl``/``tp`` from ``candidate.plan`` (the SAME
    bound ``TradePlan``'s ``stop_price`` and nearest ``targets[0]``).
    Never invents or recomputes a trading parameter. Callers are expected
    to have already checked ``len(decision.orders) == 1``.
    """
    sized_order = decision.orders[0]
    comment = f"SER8:{claim.claim_id[-20:]}"
    return DemoOrderRequestV1(
        schema_version=SCHEMA_VERSION,
        claim_id=claim.claim_id,
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
    so tests can assert exactly-once-call behavior."""

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
    writes plain CSV files. Its own read/write/poll/timeout logic is fully
    testable by pre-seeding or omitting a result file; only the real
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
            f"no matching result appeared within {self.timeout_seconds:.1f}s for claim {request.claim_id}"
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
            # Stale result left over from a previous claim -- ignore and
            # keep polling; never treat someone else's result as ours.
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
    if result.filled_volume is None or result.filled_price is None:
        return "MALFORMED"
    if result.filled_volume <= 0 or result.filled_price <= 0:
        return "MALFORMED"
    if abs(result.filled_volume - request.volume) > 1e-9:
        return "PARTIAL_FILL"
    return "FILLED"


@dataclass(frozen=True, slots=True)
class DemoOrderExecutionReceiptV1:
    """Immutable, auditable record of one send attempt for one claim_id --
    persisted for EVERY outcome (clean fill, rejection, requote, partial
    fill, malformed result, or transport failure), never only for success.
    Carries the full request and result identity; carries no invented
    field."""

    schema_version: str
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
            (self.claim_id, "claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.demo_gate_hash, "demo_gate_hash"),
            (self.request_hash, "request_hash"),
            (self.attempt_id, "attempt_id"),
            (self.result_state, "result_state"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.result_state not in {
            "ATTEMPTING", "FILLED", "REJECTED", "REQUOTE", "PARTIAL_FILL", "MALFORMED", "UNKNOWN",
        }:
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


class SER8DemoOrderSendControl:
    """Owns one new, additive SQLite table in the same database file as
    ``HypothesisRegistry`` (``registry.path``). Never modifies
    ``HypothesisRegistry``'s, the claim control's, or the demo-account
    gate's own schema, tables, or semantics."""

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
                CREATE TABLE IF NOT EXISTS ser8_mt5_demo_order_receipts (
                    claim_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    demo_gate_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    payload_json TEXT
                );
                """
            )

    def _reserve_attempt(
        self, *, claim: ExecutionAuthorizationClaimV1, attempt_id: str, request: DemoOrderRequestV1,
        demo_authorization: DemoAccountAuthorizationV1, captured_at: datetime,
    ) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT 1 FROM ser8_mt5_demo_order_receipts WHERE claim_id=?", (claim.claim_id,)
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    raise SER8DemoOrderAlreadyAttemptedError(
                        f"a send attempt already exists for claim {claim.claim_id}; refusing to send again"
                    )
                db.execute(
                    """
                    INSERT INTO ser8_mt5_demo_order_receipts(
                        claim_id, attempt_id, authorization_id, demo_gate_hash,
                        request_json, request_hash, attempted_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        claim.claim_id, attempt_id, claim.authorization_id, demo_authorization.gate_hash,
                        canonical_json_bytes(request.to_payload()).decode("utf-8"), request.request_hash,
                        captured_at.isoformat(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                raise SER8DemoOrderAlreadyAttemptedError(
                    f"a send attempt already exists for claim {claim.claim_id}; refusing to send again"
                ) from None
            except Exception:
                db.rollback()
                raise

    def _finalize(
        self, *, claim_id: str, authorization_id: str, demo_gate_hash: str, request_hash: str,
        attempt_id: str, result_state: str, recorded_at: str,
        retcode: int = -1, retcode_description: str = "", order_ticket: str = "",
        deal_ticket: str = "", position_ticket: str = "",
        requested_volume: float = 0.0, requested_price: float = 0.0,
        filled_volume: float | None = None, filled_price: float | None = None,
    ) -> DemoOrderExecutionReceiptV1:
        receipt = DemoOrderExecutionReceiptV1(
            schema_version=SCHEMA_VERSION,
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
                    "UPDATE ser8_mt5_demo_order_receipts SET payload_json=? WHERE claim_id=?",
                    (json.dumps(receipt.to_payload(), sort_keys=True), claim_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return receipt

    def send(
        self,
        claim: ExecutionAuthorizationClaimV1,
        decision: RiskDecision,
        candidate: SignalCandidate,
        *,
        allowlist: DemoAccountAllowlistV1,
        maximum_claim_age_seconds: float = DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS,
        now: datetime | None = None,
    ) -> DemoOrderExecutionReceiptV1:
        """Send exactly one demo order for ``claim``, or fail closed.
        Raises :class:`SER8DemoOrderSendError` (or a more specific
        subclass) for every precondition/outcome the module docstring
        describes; returns a receipt ONLY for a clean, fully-matched fill.
        """
        if maximum_claim_age_seconds <= 0:
            raise SER8DemoOrderSendError("maximum_claim_age_seconds must be positive")
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        # 1-2: demo account gate -- ACCEPTED lineage, claim self-
        # consistency, and allowlist membership all inherited unchanged.
        try:
            demo_authorization = verify_demo_account_authorization(claim, allowlist=allowlist, now=captured_at)
        except DemoAccountSafetyGateError as exc:
            raise SER8DemoOrderSendError(f"demo account safety gate denied this claim: {exc}") from exc

        # 3: claim staleness -- a NEW freshness boundary for this layer.
        claimed_at = datetime.fromisoformat(claim.claimed_at)
        claim_age = (captured_at - claimed_at).total_seconds()
        if claim_age < 0 or claim_age > maximum_claim_age_seconds:
            raise SER8DemoOrderSendError(
                f"claim age {claim_age:.1f}s is outside the allowed "
                f"{maximum_claim_age_seconds:.1f}s send window"
            )

        # 4: lineage cross-check.
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

        # 5: exactly one SizedOrder.
        if len(decision.orders) != 1:
            raise SER8DemoOrderSendError(
                f"expected exactly one SizedOrder for this MVP, found {len(decision.orders)}"
            )

        # 6-8: build the exact order request -- no invented/recomputed
        # trading parameter.
        request = build_demo_order_request(claim, decision, candidate, demo_authorization=demo_authorization)

        attempt_id = (
            f"EAO-{claim.account_id}-"
            f"{sha256_bytes(canonical_json_bytes({'claim_id': claim.claim_id})).removeprefix('sha256:')[:16]}"
        )

        # 9-11: atomic one-shot send-attempt guard, reserved BEFORE the
        # transport is ever called.
        self._reserve_attempt(
            claim=claim, attempt_id=attempt_id, request=request,
            demo_authorization=demo_authorization, captured_at=captured_at,
        )

        common_kwargs = dict(
            claim_id=claim.claim_id,
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
        except Exception as exc:
            # 12-14: transport failure -- persist UNKNOWN, never retry.
            self._finalize(result_state="UNKNOWN", **common_kwargs)
            raise SER8DemoOrderTransportError(f"transport failed for claim {claim.claim_id}: {exc}") from exc

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

        if result_state != "FILLED":
            raise SER8DemoOrderRejectedError(
                f"order result for claim {claim.claim_id} was {result_state}, not a clean fill"
            )
        return receipt


__all__ = [
    "DEFAULT_MAXIMUM_CLAIM_AGE_SECONDS",
    "DEMO_EXECUTOR_MAGIC_NUMBER",
    "REQUEST_CSV_FIELDS",
    "RESULT_CSV_FIELDS",
    "SCHEMA_VERSION",
    "DemoOrderExecutionReceiptV1",
    "DemoOrderRequestV1",
    "DemoOrderTransport",
    "DemoOrderTransportResult",
    "FakeDemoOrderTransport",
    "FileBridgeDemoOrderTransport",
    "SER8DemoOrderAlreadyAttemptedError",
    "SER8DemoOrderRejectedError",
    "SER8DemoOrderSendControl",
    "SER8DemoOrderSendError",
    "SER8DemoOrderTransportError",
    "build_demo_order_request",
]
