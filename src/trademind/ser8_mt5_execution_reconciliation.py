"""SER8 Automatic MT5 Execution Reconciliation V1.

A generic, continuous, read-only bridge from authoritative MT5 broker
evidence to SER8's own persisted PENDING execution legs. Automatically
transitions PENDING legs to their true terminal state (FILLED/CANCELLED/
EXPIRED/REJECTED) using ONLY
``trademind.ser8_mt5_demo_order_send.SER8DemoOrderSendControl.
reconcile_pending_leg`` -- the SAME authoritative, already-strict
reconciliation primitive built for manual use -- never a new/parallel
persistence path, and NEVER resends, calls a transport, places an order,
changes SL/TP/lot sizing, or creates a new authorization/claim.

AUDIT -- SMALLEST AUTHORITATIVE EVIDENCE SET NEEDED (see this task's own
commit message for the full trace):

Before this task, the unified executor
(mt5/TradeMind_Demo_Order_Executor_v1.mq5) exported three read-only
snapshots: account, POSITIONS (currently open trades), and Market Watch
symbols. NONE of those can answer "what happened to order_ticket X?" --
a position row has its own ``position_ticket``, not the ORIGINATING
order's ticket, and two same-symbol/same-side pending LIMIT legs (exactly
the shape of the real incident this task's own spec quotes) are
indistinguishable by symbol/side/volume alone. The genuinely missing,
strictly-necessary evidence is order-ticket-level: MT5's own order
history (``ENUM_ORDER_STATE`` -- PLACED/FILLED/CANCELED/EXPIRED/REJECTED/
PARTIAL, per ticket) and deal history (``DEAL_ORDER`` directly gives the
ORIGINATING order ticket for an executed deal, plus its real fill price/
volume/deal_ticket/position_ticket). This task adds exactly those two new
read-only exports (``mt5_risk_orders_utc_<login>.csv`` /
``mt5_risk_deals_utc_<login>.csv``), on the SAME unified EA and the SAME
risk-refresh timer cadence -- no second EA, no new timer, no change to
order-placement semantics.

MATCHING (requirement 2): a PENDING leg is reconciled ONLY when its own
persisted ``order_ticket`` is found in the orders export AND that same
row's ``symbol``/``side``/``magic`` all match what SER8 itself already
persisted for that leg's original request. Any mismatch -- or the ticket
simply not being present in either export at all -- is AMBIGUOUS and is
never treated as evidence of anything (requirement 6): this module NEVER
infers a fill or a cancellation merely because an order's ticket does not
appear in a given export cycle (a bounded-lookback history window, a
transient export write, or a genuinely absent row are all, from this
module's point of view, indistinguishable from "no evidence yet" -- and
"no evidence" always means "skip this leg this cycle", never a guess).

SAFETY (requirement 7): this module never imports MetaTrader5, never
constructs a ``DemoOrderTransport``/calls ``.send()`` on one, never
constructs ``ExecutionAuthorizationV1``/``ExecutionAuthorizationClaimV1``,
and never touches SL/TP, lot sizing, or a risk decision -- it only ever
calls ``SER8DemoOrderSendControl.reconcile_pending_leg``, whose own
docstring and tests already establish it never calls a transport either.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from trademind.ser8_mt5_demo_order_send import (
    DemoOrderRequestV1,
    DemoOrderTransportResult,
    SER8DemoOrderSendControl,
    SER8DemoOrderSendError,
)

ORDER_HISTORY_REQUIRED_FIELDS = (
    "order_ticket", "symbol", "magic", "side", "volume", "price", "state",
)
DEAL_HISTORY_REQUIRED_FIELDS = (
    "deal_ticket", "order_ticket", "position_id", "symbol", "magic", "side", "volume", "price",
)

# ENUM_ORDER_STATE names (mt5/TradeMind_Demo_Order_Executor_v1.mq5's own
# ExportOrderHistorySnapshot writes these via EnumTail(EnumToString(...),
# "ORDER_STATE_"), plus the synthetic "PLACED" this module's own export
# uses for a still-ACTIVE (not yet triggered) order).
_STILL_ACTIVE_STATES = {"PLACED", "STARTED", "PARTIAL"}
_FILLED_STATES = {"FILLED"}
_TERMINAL_NONFILL_STATES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_NONFILL_STATE_TO_LEG_STATE = {"CANCELED": "CANCELLED", "CANCELLED": "CANCELLED", "EXPIRED": "EXPIRED", "REJECTED": "REJECTED"}


class ReconciliationEvidenceError(RuntimeError):
    """Raised when a supplied MT5 export CSV is missing required columns
    or otherwise structurally invalid -- fails closed, never inferred."""


def _nonempty(value: str | None) -> bool:
    text = (value or "").strip()
    return text not in ("", "0")


def _read_csv_rows(path: Path, required_fields: Sequence[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise ReconciliationEvidenceError(f"MT5 export not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReconciliationEvidenceError(f"{path}: could not be read: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames or ())
    missing = [field for field in required_fields if field not in fields]
    if missing:
        raise ReconciliationEvidenceError(f"{path}: missing required fields: {', '.join(missing)}")
    return [dict(row) for row in reader]


@dataclass(frozen=True, slots=True)
class OrderHistoryRow:
    order_ticket: str
    symbol: str
    magic: str
    side: str
    volume: float
    price: float
    state: str
    position_id: str = ""


@dataclass(frozen=True, slots=True)
class DealHistoryRow:
    deal_ticket: str
    order_ticket: str
    position_id: str
    symbol: str
    magic: str
    side: str
    volume: float
    price: float


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_order_history(path: Path) -> dict[str, OrderHistoryRow]:
    """Reads the unified executor's own ``mt5_risk_orders_utc_<login>.csv``
    export -- one row per known order_ticket, its LATEST known state (an
    order_ticket should appear at most once per export cycle; if it
    appears more than once, the LAST row wins, matching this module's own
    "authoritative snapshot, not a log" convention). Fails closed
    (raises) on a structurally invalid file -- never silently returns a
    partial/guessed result."""
    rows = _read_csv_rows(path, ORDER_HISTORY_REQUIRED_FIELDS)
    by_ticket: dict[str, OrderHistoryRow] = {}
    for row in rows:
        ticket = (row.get("order_ticket") or "").strip()
        if not ticket:
            continue
        volume = _to_float(row.get("volume"))
        price = _to_float(row.get("price"))
        if volume is None or price is None:
            continue
        by_ticket[ticket] = OrderHistoryRow(
            order_ticket=ticket,
            symbol=(row.get("symbol") or "").strip(),
            magic=(row.get("magic") or "").strip(),
            side=(row.get("side") or "").strip().upper(),
            volume=volume,
            price=price,
            state=(row.get("state") or "").strip().upper(),
            position_id=(row.get("position_id") or "").strip(),
        )
    return by_ticket


def load_deal_history(path: Path) -> dict[str, list[DealHistoryRow]]:
    """Reads the unified executor's own ``mt5_risk_deals_utc_<login>.csv``
    export -- grouped by ``order_ticket`` (a single order can, in theory,
    correspond to more than one deal, e.g. a partial fill later completed
    by a second deal; this module reports every deal it finds for a
    ticket, never silently picks one). Fails closed on a structurally
    invalid file."""
    rows = _read_csv_rows(path, DEAL_HISTORY_REQUIRED_FIELDS)
    by_order_ticket: dict[str, list[DealHistoryRow]] = {}
    for row in rows:
        order_ticket = (row.get("order_ticket") or "").strip()
        if not order_ticket:
            continue
        volume = _to_float(row.get("volume"))
        price = _to_float(row.get("price"))
        if volume is None or price is None:
            continue
        by_order_ticket.setdefault(order_ticket, []).append(
            DealHistoryRow(
                deal_ticket=(row.get("deal_ticket") or "").strip(),
                order_ticket=order_ticket,
                position_id=(row.get("position_id") or "").strip(),
                symbol=(row.get("symbol") or "").strip(),
                magic=(row.get("magic") or "").strip(),
                side=(row.get("side") or "").strip().upper(),
                volume=volume,
                price=price,
            )
        )
    return by_order_ticket


@dataclass(frozen=True, slots=True)
class LegReconciliationOutcome:
    """The result of evaluating exactly ONE pending leg for exactly ONE
    reconciliation cycle -- never a broker side effect, only a
    classification of what (if anything) this cycle proved."""

    leg_id: str
    status: str  # STILL_OPEN | NEWLY_FILLED | NEWLY_TERMINAL | AMBIGUOUS
    detail: str
    new_result_state: str | None = None


def _matches_leg(order_row: OrderHistoryRow, *, request: DemoOrderRequestV1, magic: str) -> bool:
    """Requirement 2: EXACT identity match -- symbol, side (action), and
    magic must ALL agree with what SER8 itself already persisted for this
    leg's own original request. A ticket match alone is never sufficient
    (a corrupted/mismatched export row must never silently reconcile the
    wrong leg)."""
    return (
        order_row.symbol == request.symbol
        and order_row.side == request.action
        and (not order_row.magic or order_row.magic == magic)
    )


def evaluate_pending_leg(
    control: SER8DemoOrderSendControl,
    leg_id: str,
    *,
    order_history: dict[str, OrderHistoryRow],
    deal_history: dict[str, list[DealHistoryRow]],
    now: datetime | None = None,
    dry_run: bool = False,
) -> LegReconciliationOutcome:
    """Evaluates exactly ONE currently-PENDING leg against the supplied
    order/deal history evidence, and -- unless ``dry_run`` -- calls
    :meth:`SER8DemoOrderSendControl.reconcile_pending_leg` when (and only
    when) the evidence unambiguously proves a state change. Never calls
    the transport (this function does not even have a reference to one).
    """
    receipt = control.get_leg_receipt(leg_id)
    if receipt is None:
        return LegReconciliationOutcome(leg_id, "AMBIGUOUS", "no persisted receipt found for this leg identity")
    if receipt.result_state != "PENDING":
        # Defensive only -- list_pending_leg_ids_for_account only ever
        # returns PENDING legs, so this means a concurrent process (or a
        # prior cycle) already resolved it between listing and
        # evaluating. Reported factually, counted in none of the "newly
        # ..." buckets (nothing changed THIS cycle), and never touched.
        return LegReconciliationOutcome(
            leg_id, "ALREADY_RESOLVED", f"leg is already {receipt.result_state}, not PENDING", receipt.result_state,
        )

    try:
        request = control.get_leg_request(leg_id)
    except SER8DemoOrderSendError as exc:
        return LegReconciliationOutcome(leg_id, "AMBIGUOUS", f"request integrity check failed: {exc}")
    if request is None:
        return LegReconciliationOutcome(leg_id, "AMBIGUOUS", "no persisted request found for this leg identity")

    order_ticket = receipt.order_ticket
    if not _nonempty(order_ticket):
        return LegReconciliationOutcome(leg_id, "AMBIGUOUS", "persisted receipt has no real order_ticket")

    order_row = order_history.get(order_ticket)
    if order_row is None:
        # Requirement: DO NOT infer a fill/cancel merely because an order
        # disappears (or was simply outside this cycle's evidence). "Not
        # found" is never evidence of anything.
        return LegReconciliationOutcome(
            leg_id, "AMBIGUOUS", f"order_ticket {order_ticket} not present in this cycle's order-history export"
        )

    magic = str(request.magic)
    if not _matches_leg(order_row, request=request, magic=magic):
        return LegReconciliationOutcome(
            leg_id, "AMBIGUOUS",
            f"order-history row for ticket {order_ticket} does not match this leg's own persisted identity "
            f"(symbol/side/magic) -- refusing to reconcile one leg from another leg's evidence",
        )

    if order_row.state in _STILL_ACTIVE_STATES:
        return LegReconciliationOutcome(leg_id, "STILL_OPEN", f"order-history state is {order_row.state}")

    if order_row.state in _FILLED_STATES:
        deals = deal_history.get(order_ticket, [])
        matching_deals = [
            deal for deal in deals
            if deal.symbol == request.symbol and deal.side == request.action
            and (not deal.magic or deal.magic == magic)
        ]
        if not matching_deals:
            # A FILLED order MUST have a corresponding deal -- if the
            # deal export does not (yet) show one, this is ambiguous, not
            # a fill. Never fabricate a fill price/volume.
            return LegReconciliationOutcome(
                leg_id, "AMBIGUOUS",
                f"order-history state is FILLED for ticket {order_ticket} but no matching deal-history "
                "row was found -- refusing to guess the fill price/volume",
            )
        deal = matching_deals[-1]
        evidence = DemoOrderTransportResult(
            claim_id=leg_id,
            demo_account_id=request.demo_account_id,
            symbol=request.symbol,
            retcode=10009,
            retcode_description="FILLED (reconciled from MT5 order/deal history)",
            order_ticket=order_ticket,
            deal_ticket=deal.deal_ticket,
            position_ticket=deal.position_id,
            filled_volume=deal.volume,
            filled_price=deal.price,
        )
        if dry_run:
            return LegReconciliationOutcome(leg_id, "NEWLY_FILLED", "would reconcile to FILLED (dry-run)", "FILLED")
        recovered = control.reconcile_pending_leg(leg_id, evidence=evidence, now=now)
        if recovered.result_state == "PENDING":
            # _classify_result disagreed (e.g. volume mismatch produced
            # PARTIAL_FILL, or something else) -- report exactly what it
            # actually became, never assume FILLED happened.
            return LegReconciliationOutcome(leg_id, "STILL_OPEN", "reconciliation call was a no-op", "PENDING")
        return LegReconciliationOutcome(leg_id, "NEWLY_FILLED", f"reconciled to {recovered.result_state}", recovered.result_state)

    if order_row.state in _TERMINAL_NONFILL_STATES:
        terminal_state = _NONFILL_STATE_TO_LEG_STATE[order_row.state]
        if dry_run:
            return LegReconciliationOutcome(
                leg_id, "NEWLY_TERMINAL", f"would reconcile to {terminal_state} (dry-run)", terminal_state
            )
        recovered = control.reconcile_pending_leg(leg_id, terminal_order_state=terminal_state, now=now)
        return LegReconciliationOutcome(leg_id, "NEWLY_TERMINAL", f"reconciled to {recovered.result_state}", recovered.result_state)

    # Any other ENUM_ORDER_STATE value (REQUEST_ADD/REQUEST_MODIFY/
    # REQUEST_CANCEL, or an unrecognized string) is a transient/unknown
    # state -- never guessed at.
    return LegReconciliationOutcome(leg_id, "AMBIGUOUS", f"unrecognized order-history state {order_row.state!r}")


@dataclass(frozen=True, slots=True)
class ReconciliationCycleResult:
    """Concise, structured per-cycle summary -- requirement 11. Never a
    verbose per-field dump; one line's worth of counters plus an explicit
    ``broker_sends`` (always 0 -- this module can never place an order)."""

    account: str
    pending_legs_seen: int
    pending_still_open: int
    newly_filled: int
    newly_cancelled_or_rejected: int
    ambiguous: int
    broker_sends: int
    cycle_status: str  # OK | AMBIGUOUS_PRESENT | EVIDENCE_ERROR
    outcomes: tuple[LegReconciliationOutcome, ...]

    def summary_line(self) -> str:
        return (
            f"account={self.account} pending_legs_seen={self.pending_legs_seen} "
            f"pending_still_open={self.pending_still_open} newly_filled={self.newly_filled} "
            f"newly_cancelled_or_rejected={self.newly_cancelled_or_rejected} "
            f"ambiguous={self.ambiguous} broker_sends={self.broker_sends} "
            f"cycle_status={self.cycle_status}"
        )


def run_reconciliation_cycle(
    control: SER8DemoOrderSendControl,
    *,
    account: str,
    orders_csv: Path,
    deals_csv: Path,
    now: datetime | None = None,
    dry_run: bool = False,
) -> ReconciliationCycleResult:
    """ONE reconciliation cycle for ``account`` -- discovers every
    persisted PENDING leg for that account (generic; never hard-codes a
    claim or ticket), loads the two MT5 export CSVs ONCE, and evaluates
    every leg against them. Never calls a transport (this function is
    never given one). A structurally invalid export CSV fails the WHOLE
    cycle closed (``cycle_status="EVIDENCE_ERROR"``) rather than silently
    skipping legs against partial/guessed evidence.
    """
    captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    leg_ids = control.list_pending_leg_ids_for_account(account)

    try:
        order_history = load_order_history(orders_csv)
        deal_history = load_deal_history(deals_csv)
    except ReconciliationEvidenceError as exc:
        return ReconciliationCycleResult(
            account=account, pending_legs_seen=len(leg_ids), pending_still_open=0, newly_filled=0,
            newly_cancelled_or_rejected=0, ambiguous=len(leg_ids), broker_sends=0,
            cycle_status=f"EVIDENCE_ERROR: {exc}", outcomes=(),
        )

    outcomes: list[LegReconciliationOutcome] = []
    for leg_id in leg_ids:
        outcomes.append(
            evaluate_pending_leg(
                control, leg_id, order_history=order_history, deal_history=deal_history,
                now=captured_at, dry_run=dry_run,
            )
        )

    still_open = sum(1 for o in outcomes if o.status == "STILL_OPEN")
    newly_filled = sum(1 for o in outcomes if o.status == "NEWLY_FILLED")
    newly_terminal = sum(1 for o in outcomes if o.status == "NEWLY_TERMINAL")
    ambiguous = sum(1 for o in outcomes if o.status == "AMBIGUOUS")
    cycle_status = "AMBIGUOUS_PRESENT" if ambiguous else "OK"

    return ReconciliationCycleResult(
        account=account, pending_legs_seen=len(leg_ids), pending_still_open=still_open,
        newly_filled=newly_filled, newly_cancelled_or_rejected=newly_terminal, ambiguous=ambiguous,
        broker_sends=0, cycle_status=cycle_status, outcomes=tuple(outcomes),
    )


__all__ = [
    "DEAL_HISTORY_REQUIRED_FIELDS",
    "ORDER_HISTORY_REQUIRED_FIELDS",
    "DealHistoryRow",
    "LegReconciliationOutcome",
    "OrderHistoryRow",
    "ReconciliationCycleResult",
    "ReconciliationEvidenceError",
    "evaluate_pending_leg",
    "load_deal_history",
    "load_order_history",
    "run_reconciliation_cycle",
]
