"""SER8 CORE_8 MARKET-ONLY Execution Policy V1: the one fail-closed
operational policy that narrows supervised demo execution to the eight
researched CORE_8 FX symbols, executed with a single MARKET entry only.

WHY THIS MODULE EXISTS
----------------------
The already-closed SER8 execution chain (research risk gate ->
``SER8ExecutionAuthorizationControl`` -> ``SER8ExecutionAuthorizationClaimControl``
-> ``SER8DemoOrderSendControl`` -> unified MT5 EA) is deliberately
SYMBOL-AGNOSTIC and GEOMETRY-AGNOSTIC mechanism: which symbol may trade is
supplied from above by a hypothesis's own ``HypothesisTradeableScopeV1``,
and which legs exist is supplied by ``RiskDecision.orders``. Those layers
are shared by research, screening, and reconciliation flows and are
intentionally not the place to hard-code a deployment policy.

CORE_8 + MARKET_ONLY is exactly such a deployment policy, established by
already-completed research (do not rerun; do not modify its evidence):

  * authoritative signal source: ``trademind.ote_engine.build_ote_signals``
  * MARKET_ONLY beats MARKET+LIMIT+LIMIT on CORE_8
  * CORE_8 is positive in all three chronological windows

So this module owns that policy in ONE place, and the production execution
entrypoint applies it BEFORE any risk evaluation, authorization, claim, or
broker send can occur. Nothing here sends an order, prices an order, sizes
an order, contacts a broker, reads or writes the protected holdout, or
touches historical acquisition.

SIZING AUTHORITY IS UNCHANGED
-----------------------------
This module never computes a volume, lot size, risk amount, or margin
figure, and never constructs a ``SizedOrder``. The Python Risk Manager
(``trademind.risk_manager``) remains the SOLE sizing authority. This policy
only decides (a) whether a symbol may execute at all and (b) the ENTRY
GEOMETRY (one MARKET leg instead of a MARKET+LIMIT+LIMIT basket) that the
Risk Manager is then asked to size, exactly as it already sizes whatever
``candidate.plan.entries`` it is given.

STOP AND PRIMARY TARGET SEMANTICS ARE PRESERVED
-----------------------------------------------
:func:`market_only_plan` reproduces the SAME transform the already-published
execution-geometry research validated as ``MARKET_ONLY_SAME_TARGET`` in
``trademind.ser8_execution_geometry_experiment``: keep the plan's single
MARKET entry at its original price, raise its allocation to 1.0, drop every
LIMIT entry, keep ``stop_price`` byte-identical, and keep the PRIMARY target
(``targets[0]``) byte-identical. No stop is recomputed, no target is
recomputed, and no new R-multiple is invented here.

It is reimplemented in this module rather than imported from
``ser8_execution_geometry_experiment`` on purpose: that module is labelled
RESEARCH/SCREENING ONLY and transitively imports historical-dataset, replay,
shadow-evaluation, and checkpoint machinery that has no business being
loaded into the live execution path. To guarantee the two never silently
diverge, ``tests/test_ser8_core8_market_only_policy.py`` asserts that this
function and ``variant_trade_plan(plan, VARIANT_MARKET_SAME_TARGET)`` produce
the IDENTICAL EXECUTABLE GEOMETRY for the same input -- same ``action``, same
single MARKET entry price/allocation/order_type, same ``stop_price``, same
``targets``. The two intentionally differ ONLY in human-readable prose: the
research variant stamps its entry/target rationale with
``"execution-geometry experiment (SCREENING ONLY)"``, which would be a false
label on a real supervised demo order, so this module preserves the original
signal's own rationale text instead. No executable number differs.

FAIL-CLOSED, NO OVERRIDE
------------------------
There is no force, override, bypass, or "allow everything" parameter
anywhere in this module, mirroring the convention
``ser8_demo_account_safety_gate`` already established. Symbol matching is
EXACT uppercase string equality against :data:`CORE_8_SYMBOLS` -- no
case-folding, no broker-suffix tolerance (``"EURJPY.raw"`` is NOT CORE_8),
no prefix/substring/fuzzy matching. A symbol this module does not recognise
is denied, never guessed at.

This policy is ADDITIVE. It never replaces, weakens, or duplicates the
existing demo-account safety gate, the authorization/claim one-shot
controls, or the unified MT5 executor -- every one of those remains
mandatory and unmodified downstream of this check.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from trademind.signal_intelligence import EntryOrder, TradePlan

POLICY_VERSION = "ser8-core8-market-only-policy-v1"

#: The eight researched CORE_8 FX symbols -- the ONLY symbols this policy
#: permits to reach supervised demo execution. Deliberately a frozen,
#: explicit, hand-verified literal (not discovered, ranked, or inferred at
#: runtime): widening execution authority must be a reviewed source change,
#: never a side effect of broker metadata or a screening run.
CORE_8_SYMBOLS = frozenset(
    {
        "CHFJPY",
        "EURJPY",
        "EURNZD",
        "GBPAUD",
        "GBPNZD",
        "NZDCAD",
        "NZDCHF",
        "USDJPY",
    }
)

#: The only order type CORE_8 supervised demo execution may ever place.
MARKET_ONLY_ORDER_TYPE = "MARKET"


class SER8Core8PolicyError(RuntimeError):
    """Raised for every CORE_8 / MARKET_ONLY policy denial.

    Always raised BEFORE anything is sized, authorized, claimed, or sent.
    There is no "denied but proceed anyway" return value.
    """


def is_core8_symbol(symbol: object) -> bool:
    """Pure, side-effect-free membership test -- exact match only."""
    return type(symbol) is str and symbol in CORE_8_SYMBOLS


def verify_core8_symbol(symbol: object) -> str:
    """Return ``symbol`` unchanged iff it is exactly a CORE_8 symbol.

    Fails closed with :class:`SER8Core8PolicyError` for every other input,
    including ``None``, a non-string, a lowercase spelling, and a
    broker-suffixed variant of a CORE_8 name.
    """
    if type(symbol) is not str or not symbol:
        raise SER8Core8PolicyError(
            "symbol must be a non-empty string; refusing to execute an unidentified instrument"
        )
    if symbol not in CORE_8_SYMBOLS:
        raise SER8Core8PolicyError(
            f"symbol {symbol!r} is not one of the {len(CORE_8_SYMBOLS)} operationalized CORE_8 "
            f"symbols ({', '.join(sorted(CORE_8_SYMBOLS))}); refusing to execute"
        )
    return symbol


def verify_market_only_order_types(order_types: Sequence[object] | Iterable[object]) -> None:
    """Fail closed unless this is exactly ONE leg, and that leg is MARKET.

    Applied to ``RiskDecision.orders`` order types before a send, and to a
    persisted execution plan's leg order types before a resume, so a
    MARKET+LIMIT+LIMIT basket can never reach the broker under this policy.
    """
    types = list(order_types)
    if not types:
        raise SER8Core8PolicyError("execution plan has no legs; refusing to execute")
    if len(types) != 1:
        raise SER8Core8PolicyError(
            f"CORE_8 execution is MARKET_ONLY: expected exactly 1 leg, found {len(types)} "
            f"({', '.join(str(item) for item in types)}); refusing to execute"
        )
    if types[0] != MARKET_ONLY_ORDER_TYPE:
        raise SER8Core8PolicyError(
            f"CORE_8 execution is MARKET_ONLY: leg order_type {types[0]!r} is not "
            f"{MARKET_ONLY_ORDER_TYPE!r}; refusing to execute"
        )


def verify_core8_market_only_execution(
    *, symbol: object, order_types: Sequence[object] | Iterable[object]
) -> None:
    """Both policy checks together -- the single call an execution caller
    makes immediately before handing a plan to the unified MT5 executor."""
    verify_core8_symbol(symbol)
    verify_market_only_order_types(order_types)


def _sole_market_entry(plan: TradePlan) -> EntryOrder:
    market_entries = [item for item in plan.entries if item.order_type == MARKET_ONLY_ORDER_TYPE]
    if len(market_entries) != 1:
        raise SER8Core8PolicyError(
            f"expected exactly one MARKET entry in the source plan, found {len(market_entries)}; "
            "refusing to guess which entry to execute"
        )
    return market_entries[0]


def market_only_plan(plan: TradePlan) -> TradePlan:
    """Return the MARKET_ONLY form of ``plan``.

    Keeps the single MARKET entry at its original price with allocation
    1.0, drops every LIMIT entry, and preserves ``stop_price`` and the
    PRIMARY target (``targets[0]``) exactly. Semantically identical to the
    researched ``MARKET_ONLY_SAME_TARGET`` variant.

    Fails closed rather than adjusting an invalid geometry: if removing the
    LIMIT legs raises the average entry enough that ``TradePlan``'s own
    BUY/SELL stop/target validation would reject the result, this raises
    :class:`SER8Core8PolicyError` instead of fabricating a workaround.
    """
    if type(plan) is not TradePlan:
        raise SER8Core8PolicyError("plan must be a genuine TradePlan")
    market_entry = _sole_market_entry(plan)
    try:
        return TradePlan(
            action=plan.action,
            entries=(
                EntryOrder(
                    price=market_entry.price,
                    allocation=1.0,
                    rationale=market_entry.rationale,
                    order_type=MARKET_ONLY_ORDER_TYPE,
                ),
            ),
            stop_price=plan.stop_price,
            targets=(plan.targets[0],),
            invalidation=plan.invalidation,
            target_rationale=(plan.target_rationale[:1] or ("Primary target",)),
        )
    except ValueError as exc:
        raise SER8Core8PolicyError(
            f"MARKET_ONLY geometry is invalid for this plan: {exc}"
        ) from exc


__all__ = [
    "CORE_8_SYMBOLS",
    "MARKET_ONLY_ORDER_TYPE",
    "POLICY_VERSION",
    "SER8Core8PolicyError",
    "is_core8_symbol",
    "market_only_plan",
    "verify_core8_market_only_execution",
    "verify_core8_symbol",
    "verify_market_only_order_types",
]
