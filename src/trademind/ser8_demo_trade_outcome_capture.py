"""SER8 Demo Trade Outcome Capture V1: the narrowest authoritative bridge
from CLOSED broker evidence (the unified executor's own
``mt5_risk_deals_utc_<login>.csv`` export) to a durable SER8 outcome
record for one already-FILLED execution leg. Deliberately STOPS there --
this module is NOT the future Analytics Core; it persists exactly the
fields listed in the SER8 AUTONOMOUS CONTINUOUS DEMO EXECUTION V1 task
spec's own OUTCOME CAPTURE section and nothing more.

AUDIT (why this bridge exists, why nothing else already does this job):

``trademind.ser8_mt5_execution_reconciliation`` (already closed, proven on
real Windows) answers exactly one question -- "has this leg's own ENTRY
order since filled/been cancelled/expired/rejected?" -- by matching a
PENDING leg's persisted ``order_ticket`` against the orders/deals export.
It has no concept of, and never touches, what happens to a position AFTER
it fills: a take-profit/stop-loss close is a SEPARATE deal, with its own
``order_ticket`` (not the leg's original entry order), linked back to the
leg only via ``DEAL_POSITION_ID`` (== the leg's own persisted
``position_ticket``, recorded once the entry deal fills). Before this
module, nothing in this codebase ever read that CLOSE deal -- a real,
proven demo trade's own realized P/L (``EAC-67206924-2e40988a6cd689d6``,
+39.90 USD) was observed by a human directly in the MT5 terminal, not
captured anywhere durable. This module adds exactly that narrow, missing
read: given an already-FILLED leg's own ``position_ticket``, scan the SAME
deal-history export automatic reconciliation already reads for every
``DEAL_ENTRY_OUT`` deal on that position, and, if authoritative broker
evidence for a close exists, persist one outcome record.

SAFETY: this module never imports MetaTrader5, never constructs a
``DemoOrderTransport``/calls ``.send()`` on one, never touches SL/TP,
lot sizing, or a risk decision, and never mutates any leg receipt or
execution plan -- it only ever READS
``SER8DemoOrderSendControl.get_leg_receipt``/``get_leg_request`` and
``SER8ExecutionAuthorizationControl.get_authorization`` (both already
public, read-only, pre-existing accessors) and WRITES to its own new,
additive table. A position with no ``DEAL_ENTRY_OUT`` evidence yet is
simply still open -- this module never infers a close from a candle,
from time passing, or from the position's absence in a snapshot; "no
evidence yet" always means "not captured yet", never a guess.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trademind.discovery.hypothesis_registry import HypothesisRegistry
from trademind.ser8_execution_authorization import (
    ExecutionAuthorizationV1,
    SER8ExecutionAuthorizationControl,
)
from trademind.ser8_mt5_demo_order_send import (
    DEMO_EXECUTOR_MAGIC_NUMBER,
    SER8DemoOrderSendControl,
)
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-demo-trade-outcome-capture-v1"
_OUTCOME_HASH_DOMAIN = b"trademind:ser8:demo-trade-outcome-capture:v1"

DEAL_HISTORY_OUTCOME_FIELDS = (
    "deal_ticket", "order_ticket", "position_id", "symbol", "magic", "side",
    "volume", "price", "entry", "time_deal_msc",
)


class SER8DemoTradeOutcomeError(RuntimeError):
    """Raised for a structurally invalid deal-history export, an unknown
    leg, or a leg that is not (yet) FILLED -- always before anything is
    persisted."""


def _nonempty_str(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8DemoTradeOutcomeError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class DemoTradeOutcomeV1:
    """Immutable, auditable CLOSED-trade outcome record for ONE execution
    leg -- persisted only once authoritative broker evidence of a close
    (at least one ``DEAL_ENTRY_OUT`` deal on the leg's own
    ``position_ticket``) exists. Carries no invented/inferred field:
    ``realized_pl`` is ``None`` whenever the broker evidence itself did
    not supply a parseable ``profit`` value, never computed by this
    module."""

    schema_version: str
    leg_id: str
    plan_id: str
    parent_claim_id: str
    entry_index: int
    authorization_id: str
    hypothesis_id: str
    candidate_signal_id: str
    account_id: str
    symbol: str
    side: str
    order_type: str
    requested_volume: float
    sl: float
    tp: float
    entry_price: float
    entry_timestamp: str
    entry_order_ticket: str
    entry_deal_ticket: str
    position_ticket: str
    exit_price: float
    exit_timestamp: str
    exit_deal_tickets: tuple[str, ...]
    realized_pl: float | None
    terminal_reason: str
    captured_at: str
    outcome_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8DemoTradeOutcomeError("unsupported demo trade outcome schema_version")
        for value, field_name in (
            (self.leg_id, "leg_id"),
            (self.plan_id, "plan_id"),
            (self.parent_claim_id, "parent_claim_id"),
            (self.authorization_id, "authorization_id"),
            (self.hypothesis_id, "hypothesis_id"),
            (self.candidate_signal_id, "candidate_signal_id"),
            (self.account_id, "account_id"),
            (self.symbol, "symbol"),
            (self.side, "side"),
            (self.order_type, "order_type"),
            (self.entry_timestamp, "entry_timestamp"),
            (self.entry_order_ticket, "entry_order_ticket"),
            (self.position_ticket, "position_ticket"),
            (self.exit_timestamp, "exit_timestamp"),
            (self.terminal_reason, "terminal_reason"),
            (self.captured_at, "captured_at"),
        ):
            _nonempty_str(value, field_name=field_name)
        if self.side not in {"BUY", "SELL"}:
            raise SER8DemoTradeOutcomeError("side must be BUY or SELL")
        if not self.exit_deal_tickets:
            raise SER8DemoTradeOutcomeError("exit_deal_tickets must contain at least one deal ticket")

        object.__setattr__(
            self, "outcome_hash",
            sha256_bytes(_OUTCOME_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "leg_id": self.leg_id,
            "plan_id": self.plan_id,
            "parent_claim_id": self.parent_claim_id,
            "entry_index": self.entry_index,
            "authorization_id": self.authorization_id,
            "hypothesis_id": self.hypothesis_id,
            "candidate_signal_id": self.candidate_signal_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_volume": self.requested_volume,
            "sl": self.sl,
            "tp": self.tp,
            "entry_price": self.entry_price,
            "entry_timestamp": self.entry_timestamp,
            "entry_order_ticket": self.entry_order_ticket,
            "entry_deal_ticket": self.entry_deal_ticket,
            "position_ticket": self.position_ticket,
            "exit_price": self.exit_price,
            "exit_timestamp": self.exit_timestamp,
            "exit_deal_tickets": list(self.exit_deal_tickets),
            "realized_pl": self.realized_pl,
            "terminal_reason": self.terminal_reason,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["captured_at"] = self.captured_at
        payload["outcome_hash"] = self.outcome_hash
        return payload


def _outcome_from_payload(payload: dict[str, object]) -> DemoTradeOutcomeV1:
    return DemoTradeOutcomeV1(
        schema_version=payload["schema_version"],
        leg_id=payload["leg_id"],
        plan_id=payload["plan_id"],
        parent_claim_id=payload["parent_claim_id"],
        entry_index=payload["entry_index"],
        authorization_id=payload["authorization_id"],
        hypothesis_id=payload["hypothesis_id"],
        candidate_signal_id=payload["candidate_signal_id"],
        account_id=payload["account_id"],
        symbol=payload["symbol"],
        side=payload["side"],
        order_type=payload["order_type"],
        requested_volume=payload["requested_volume"],
        sl=payload["sl"],
        tp=payload["tp"],
        entry_price=payload["entry_price"],
        entry_timestamp=payload["entry_timestamp"],
        entry_order_ticket=payload["entry_order_ticket"],
        entry_deal_ticket=payload["entry_deal_ticket"],
        position_ticket=payload["position_ticket"],
        exit_price=payload["exit_price"],
        exit_timestamp=payload["exit_timestamp"],
        exit_deal_tickets=tuple(payload["exit_deal_tickets"]),
        realized_pl=payload["realized_pl"],
        terminal_reason=payload["terminal_reason"],
        captured_at=payload["captured_at"],
    )


@dataclass(frozen=True, slots=True)
class _CloseDealEvidence:
    deal_ticket: str
    price: float
    volume: float
    time_deal_msc: int
    profit: float | None


def _load_close_deals_for_position(
    path: Path, *, position_ticket: str, magic: int
) -> list[_CloseDealEvidence]:
    """Reads the unified executor's own ``mt5_risk_deals_utc_<login>.csv``
    export and returns every ``DEAL_ENTRY_OUT`` deal whose own
    ``position_id``/``magic`` match this leg's persisted
    ``position_ticket``/magic -- in deal_ticket order, never picking one
    arbitrarily. An empty result means "no close evidence yet", never
    "the position was cancelled" or any other inference -- this
    deliberately includes the export file simply not existing yet: the
    unified executor writes this export on its own independent
    risk-refresh timer, so a leg can legitimately fill before this
    particular file has ever been written even once, and outcome capture
    is a best-effort bridge, never a precondition of the core
    authorization/execution safety chain. Fails closed (raises) only for
    a structurally invalid export that DOES exist -- missing a required
    column -- never for a merely-absent or merely-empty (still-open)
    one."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [name for name in DEAL_HISTORY_OUTCOME_FIELDS if name not in fieldnames]
        if missing:
            raise SER8DemoTradeOutcomeError(
                f"{path}: missing required deal-history column(s): {missing}"
            )
        rows = list(reader)

    magic_text = str(magic)
    evidence: list[_CloseDealEvidence] = []
    for row in rows:
        if (row.get("position_id") or "").strip() != position_ticket:
            continue
        if (row.get("magic") or "").strip() != magic_text:
            continue
        if (row.get("entry") or "").strip().upper() != "OUT":
            continue
        try:
            price = float(row["price"])
            volume = float(row["volume"])
            time_deal_msc = int(row["time_deal_msc"])
        except (KeyError, TypeError, ValueError):
            continue
        profit: float | None
        profit_raw = (row.get("profit") or "").strip()
        try:
            profit = float(profit_raw) if profit_raw != "" else None
        except ValueError:
            profit = None
        evidence.append(
            _CloseDealEvidence(
                deal_ticket=(row.get("deal_ticket") or "").strip(),
                price=price, volume=volume, time_deal_msc=time_deal_msc, profit=profit,
            )
        )
    evidence.sort(key=lambda item: (item.time_deal_msc, item.deal_ticket))
    return evidence


class SER8DemoTradeOutcomeControl:
    """Owns one new, additive SQLite table in the same database file as
    ``HypothesisRegistry`` (``registry.path``). Never modifies
    ``HypothesisRegistry``'s, ``SER8DemoOrderSendControl``'s, or
    ``SER8ExecutionAuthorizationControl``'s own schema, tables, or
    write paths -- only reads their already-public accessors."""

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
                CREATE TABLE IF NOT EXISTS ser8_demo_trade_outcomes (
                    leg_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    candidate_signal_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def get_outcome(self, leg_id: str) -> DemoTradeOutcomeV1 | None:
        """Public, read-only accessor for an already-captured outcome, or
        ``None`` if this leg has never been captured (still open, not yet
        FILLED, or not yet evidenced by a close deal)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM ser8_demo_trade_outcomes WHERE leg_id=?", (leg_id,)
            ).fetchone()
        if row is None:
            return None
        return _outcome_from_payload(json.loads(row["payload_json"]))

    def capture_outcome_for_leg(
        self,
        leg_id: str,
        *,
        send_control: SER8DemoOrderSendControl,
        authorization_control: SER8ExecutionAuthorizationControl,
        deals_csv: Path,
        magic: int = DEMO_EXECUTOR_MAGIC_NUMBER,
        now: datetime | None = None,
    ) -> DemoTradeOutcomeV1 | None:
        """Idempotently captures the CLOSED outcome for one already-FILLED
        execution leg, using ONLY:

          * ``send_control.get_leg_receipt``/``get_leg_request`` -- this
            leg's own already-persisted FILLED receipt/request (never
            re-derived, never guessed);
          * ``authorization_control.get_authorization`` -- the
            hypothesis/candidate lineage the leg's authorization was
            issued for;
          * fresh ``DEAL_ENTRY_OUT`` evidence from ``deals_csv`` for this
            leg's own ``position_ticket``.

        Returns the (new-or-already-captured) :class:`DemoTradeOutcomeV1`
        once close evidence exists, or ``None`` if the leg is not FILLED
        yet, or is FILLED but no close evidence exists yet (still open --
        not an error). Never calls a transport, never sends an order,
        never mutates the leg receipt or execution plan. Calling this
        again for an already-captured leg returns the SAME persisted
        record unchanged (idempotent, no duplicate row, no re-derivation
        from possibly-different later evidence)."""
        captured_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        existing = self.get_outcome(leg_id)
        if existing is not None:
            return existing

        receipt = send_control.get_leg_receipt(leg_id)
        if receipt is None or receipt.result_state != "FILLED":
            return None
        if not receipt.position_ticket or receipt.position_ticket == "0":
            return None

        request = send_control.get_leg_request(leg_id)
        if request is None:
            raise SER8DemoTradeOutcomeError(
                f"leg {leg_id} has a FILLED receipt but no persisted request; cannot capture outcome"
            )

        authorization: ExecutionAuthorizationV1 | None = authorization_control.get_authorization(
            receipt.authorization_id
        )
        if authorization is None:
            raise SER8DemoTradeOutcomeError(
                f"leg {leg_id}'s own authorization_id {receipt.authorization_id!r} was not found; "
                "cannot recover hypothesis/candidate lineage"
            )

        close_deals = _load_close_deals_for_position(
            deals_csv, position_ticket=receipt.position_ticket, magic=magic
        )
        if not close_deals:
            return None

        total_volume = sum(item.volume for item in close_deals)
        if total_volume > 0:
            exit_price = sum(item.price * item.volume for item in close_deals) / total_volume
        else:
            exit_price = close_deals[-1].price
        exit_time_msc = max(item.time_deal_msc for item in close_deals)
        exit_timestamp = datetime.fromtimestamp(exit_time_msc / 1000, tz=timezone.utc).isoformat()
        realized_pl = (
            sum(item.profit for item in close_deals)  # type: ignore[misc]
            if all(item.profit is not None for item in close_deals)
            else None
        )

        outcome = DemoTradeOutcomeV1(
            schema_version=SCHEMA_VERSION,
            leg_id=leg_id,
            plan_id=receipt.plan_id,
            parent_claim_id=receipt.parent_claim_id,
            entry_index=receipt.entry_index,
            authorization_id=receipt.authorization_id,
            hypothesis_id=authorization.hypothesis_id,
            candidate_signal_id=authorization.live_candidate_signal_id,
            account_id=authorization.account_id,
            symbol=request.symbol,
            side=request.action,
            order_type=request.order_type,
            requested_volume=request.volume,
            sl=request.sl,
            tp=request.tp,
            entry_price=receipt.filled_price if receipt.filled_price is not None else receipt.requested_price,
            entry_timestamp=receipt.recorded_at,
            entry_order_ticket=receipt.order_ticket,
            entry_deal_ticket=receipt.deal_ticket,
            position_ticket=receipt.position_ticket,
            exit_price=exit_price,
            exit_timestamp=exit_timestamp,
            exit_deal_tickets=tuple(item.deal_ticket for item in close_deals),
            realized_pl=realized_pl,
            terminal_reason="CLOSED",
            captured_at=captured_at.isoformat(),
        )

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO ser8_demo_trade_outcomes(
                        leg_id, hypothesis_id, candidate_signal_id, account_id, captured_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(leg_id) DO NOTHING
                    """,
                    (
                        outcome.leg_id, outcome.hypothesis_id, outcome.candidate_signal_id,
                        outcome.account_id, outcome.captured_at,
                        canonical_json_bytes(outcome.to_payload()).decode("utf-8"),
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

        # Re-read: a concurrent writer may have already inserted this
        # exact leg_id between the check above and this INSERT -- return
        # whatever is now authoritatively persisted, never this call's
        # own possibly-discarded object.
        return self.get_outcome(leg_id)


__all__ = [
    "DEAL_HISTORY_OUTCOME_FIELDS",
    "SCHEMA_VERSION",
    "DemoTradeOutcomeV1",
    "SER8DemoTradeOutcomeControl",
    "SER8DemoTradeOutcomeError",
]
