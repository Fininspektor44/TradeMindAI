"""Pure identity matcher between a :class:`HypothesisTradeableScopeV1` and a
live :class:`SignalCandidate`.

This was the first module in the codebase to import from both the
Discovery Engine research lineage (``trademind.discovery.*``) and the live
signal/risk lineage (``trademind.signal_intelligence``); every other module
in either lineage keeps them completely separate (see
``trademind.discovery.research_eligibility_boundary``'s own docstring for
the exhaustive verification that this was true before this module existed).
``trademind.ser8_research_risk_gate`` is the one other module that
deliberately crosses this seam -- it composes this matcher, unchanged, with
``present_eligible_artifact`` and ``bind_hypothesis_tradeable_scope`` to
reach a verified ``RiskDecision``. Confining the cross-lineage identity
checks to these two narrow, single-purpose, easy-to-audit modules -- rather
than letting the boundary blur across the codebase -- keeps the seam
impossible to miss.

``verify_live_candidate_matches_scope`` performs ONLY an exact, deterministic
identity/scope comparison -- no fuzzy matching, no semantic similarity, no
inference. A ``True`` result means the candidate's symbol, timeframe,
setup-family, and action all fall within the scope's declared, verified
identity dimensions; it does NOT mean the candidate is fresh, that its
``TradePlan`` is valid, that any risk limit is satisfied, or that a trade
should ever be sent. Freshness, market data, ``TradePlan``, account
snapshots, and risk all remain the sole responsibility of the existing
live/risk pipeline (``risk_manager.evaluate_risk``, ``mt5_risk_adapter``),
which this module never calls, imports, or reaches. This module also never
constructs a ``SignalCandidate``, never invents an entry/SL/TP, and never
produces a risk decision or execution authorization of any kind.
"""

from __future__ import annotations

from trademind.discovery.hypothesis_tradeable_scope import (
    AllowedActionScope,
    HypothesisTradeableScopeV1,
)
from trademind.signal_intelligence import SignalCandidate


def verify_live_candidate_matches_scope(
    scope: HypothesisTradeableScopeV1,
    candidate: SignalCandidate,
) -> bool:
    """Return ``True`` only if ``candidate`` exactly matches every identity
    dimension ``scope`` declares: symbol, timeframe, setup-family, and an
    action consistent with the scope's allowed action scope.

    Exact string equality only (both sides are already-normalized machine
    identifiers -- ``HypothesisTradeableScopeV1`` upper-cases symbol/
    timeframe in ``__post_init__`` exactly as ``SignalCandidate`` does).
    Does not check freshness, market data, the candidate's ``TradePlan``,
    account state, or risk -- those remain the live/risk pipeline's job.
    """
    if type(scope) is not HypothesisTradeableScopeV1:
        raise TypeError("scope must be a HypothesisTradeableScopeV1")
    if type(candidate) is not SignalCandidate:
        raise TypeError("candidate must be a SignalCandidate")

    if candidate.symbol != scope.symbol:
        return False
    if candidate.timeframe != scope.timeframe:
        return False
    if candidate.setup_family != scope.setup_family:
        return False

    action = candidate.plan.action
    if scope.allowed_action_scope == AllowedActionScope.BOTH.value:
        return action in (AllowedActionScope.BUY.value, AllowedActionScope.SELL.value)
    return action == scope.allowed_action_scope


__all__ = ["verify_live_candidate_matches_scope"]
