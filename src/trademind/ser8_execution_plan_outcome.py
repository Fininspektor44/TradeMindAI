"""Immutable aggregate outcome authority for one SER8 execution plan.

This record is the durable lifecycle boundary between a finished trading
idea and eligibility for another independent plan on the same symbol.  It
contains no broker interaction: it binds the immutable plan identity to
the terminal state of every entry leg and to the already-captured close
outcome hash of every FILLED leg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "ser8-execution-plan-outcome-v1"
_OUTCOME_HASH_DOMAIN = b"trademind:ser8:execution-plan-outcome:v1"

TERMINAL_ENTRY_STATES = frozenset(
    {"FILLED", "REJECTED", "REQUOTE", "PARTIAL_FILL", "MALFORMED", "CANCELLED", "EXPIRED"}
)
AGGREGATE_RESULTS = frozenset(
    {"NO_FILL_TERMINAL", "CLOSED_PROFIT", "CLOSED_LOSS", "CLOSED_FLAT", "CLOSED_PNL_UNKNOWN"}
)


class SER8ExecutionPlanOutcomeError(RuntimeError):
    """Raised when an aggregate execution-plan outcome is invalid."""


def _nonempty(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise SER8ExecutionPlanOutcomeError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class DemoExecutionPlanOutcomeV1:
    """Hash-bound final outcome for one complete execution plan."""

    schema_version: str
    plan_id: str
    plan_hash: str
    candidate_signal_id: str
    account_id: str
    symbol: str
    terminal_leg_states: tuple[tuple[str, str], ...]
    filled_leg_outcome_hashes: tuple[tuple[str, str], ...]
    aggregate_result: str
    total_realized_pl: float | None
    recorded_at: str
    outcome_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SER8ExecutionPlanOutcomeError("unsupported execution-plan outcome schema_version")
        for value, field_name in (
            (self.plan_id, "plan_id"),
            (self.plan_hash, "plan_hash"),
            (self.candidate_signal_id, "candidate_signal_id"),
            (self.account_id, "account_id"),
            (self.symbol, "symbol"),
            (self.aggregate_result, "aggregate_result"),
            (self.recorded_at, "recorded_at"),
        ):
            _nonempty(value, field_name=field_name)
        try:
            recorded_at = datetime.fromisoformat(self.recorded_at)
        except ValueError as exc:
            raise SER8ExecutionPlanOutcomeError("recorded_at must be an ISO timestamp") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise SER8ExecutionPlanOutcomeError("recorded_at must be timezone-aware")
        if not self.terminal_leg_states:
            raise SER8ExecutionPlanOutcomeError("terminal_leg_states must not be empty")
        if self.aggregate_result not in AGGREGATE_RESULTS:
            raise SER8ExecutionPlanOutcomeError("unsupported aggregate_result")

        terminal_ids: list[str] = []
        fully_filled_ids: set[str] = set()
        position_capable_ids: set[str] = set()
        for leg_id, result_state in self.terminal_leg_states:
            terminal_ids.append(_nonempty(leg_id, field_name="terminal leg_id"))
            if result_state not in TERMINAL_ENTRY_STATES:
                raise SER8ExecutionPlanOutcomeError(
                    f"leg {leg_id!r} is not entry-terminal: {result_state!r}"
                )
            if result_state == "FILLED":
                fully_filled_ids.add(leg_id)
            if result_state in {"FILLED", "PARTIAL_FILL"}:
                position_capable_ids.add(leg_id)
        if len(terminal_ids) != len(set(terminal_ids)):
            raise SER8ExecutionPlanOutcomeError("terminal_leg_states contains duplicate leg_id")

        outcome_ids: list[str] = []
        for leg_id, outcome_hash in self.filled_leg_outcome_hashes:
            outcome_ids.append(_nonempty(leg_id, field_name="filled outcome leg_id"))
            _nonempty(outcome_hash, field_name="filled outcome_hash")
        if len(outcome_ids) != len(set(outcome_ids)):
            raise SER8ExecutionPlanOutcomeError("filled_leg_outcome_hashes contains duplicate leg_id")
        outcome_id_set = set(outcome_ids)
        if not fully_filled_ids.issubset(outcome_id_set) or not outcome_id_set.issubset(
            position_capable_ids
        ):
            raise SER8ExecutionPlanOutcomeError(
                "every FILLED leg and only position-bearing FILLED/PARTIAL_FILL legs "
                "may have final outcome hashes"
            )

        if not outcome_id_set:
            if self.aggregate_result != "NO_FILL_TERMINAL" or self.total_realized_pl != 0.0:
                raise SER8ExecutionPlanOutcomeError(
                    "a no-fill terminal plan must record NO_FILL_TERMINAL and total_realized_pl=0.0"
                )
        elif self.aggregate_result == "NO_FILL_TERMINAL":
            raise SER8ExecutionPlanOutcomeError("a FILLED plan cannot record NO_FILL_TERMINAL")
        elif self.total_realized_pl is None:
            if self.aggregate_result != "CLOSED_PNL_UNKNOWN":
                raise SER8ExecutionPlanOutcomeError(
                    "unknown realized P/L must record CLOSED_PNL_UNKNOWN"
                )
        else:
            expected_result = (
                "CLOSED_PROFIT"
                if self.total_realized_pl > 1e-9
                else "CLOSED_LOSS"
                if self.total_realized_pl < -1e-9
                else "CLOSED_FLAT"
            )
            if self.aggregate_result != expected_result:
                raise SER8ExecutionPlanOutcomeError(
                    "aggregate_result does not match total_realized_pl"
                )

        object.__setattr__(
            self,
            "outcome_hash",
            sha256_bytes(
                _OUTCOME_HASH_DOMAIN + b"\x00" + canonical_json_bytes(self.semantic_projection())
            ),
        )
        canonical_json_bytes(self.to_payload())

    def semantic_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "candidate_signal_id": self.candidate_signal_id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "terminal_leg_states": [
                {"leg_id": leg_id, "result_state": result_state}
                for leg_id, result_state in self.terminal_leg_states
            ],
            "filled_leg_outcome_hashes": [
                {"leg_id": leg_id, "outcome_hash": outcome_hash}
                for leg_id, outcome_hash in self.filled_leg_outcome_hashes
            ],
            "aggregate_result": self.aggregate_result,
            "total_realized_pl": self.total_realized_pl,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.semantic_projection()
        payload["recorded_at"] = self.recorded_at
        payload["outcome_hash"] = self.outcome_hash
        return payload


def execution_plan_outcome_from_payload(payload: dict[str, object]) -> DemoExecutionPlanOutcomeV1:
    """Reconstruct and integrity-check one persisted aggregate outcome."""
    outcome = DemoExecutionPlanOutcomeV1(
        schema_version=payload["schema_version"],
        plan_id=payload["plan_id"],
        plan_hash=payload["plan_hash"],
        candidate_signal_id=payload["candidate_signal_id"],
        account_id=payload["account_id"],
        symbol=payload["symbol"],
        terminal_leg_states=tuple(
            (item["leg_id"], item["result_state"])
            for item in payload["terminal_leg_states"]
        ),
        filled_leg_outcome_hashes=tuple(
            (item["leg_id"], item["outcome_hash"])
            for item in payload["filled_leg_outcome_hashes"]
        ),
        aggregate_result=payload["aggregate_result"],
        total_realized_pl=payload["total_realized_pl"],
        recorded_at=payload["recorded_at"],
    )
    if outcome.outcome_hash != payload.get("outcome_hash"):
        raise SER8ExecutionPlanOutcomeError(
            f"persisted aggregate outcome for plan {outcome.plan_id} failed its integrity check"
        )
    return outcome


__all__ = [
    "AGGREGATE_RESULTS",
    "SCHEMA_VERSION",
    "TERMINAL_ENTRY_STATES",
    "DemoExecutionPlanOutcomeV1",
    "SER8ExecutionPlanOutcomeError",
    "execution_plan_outcome_from_payload",
]
