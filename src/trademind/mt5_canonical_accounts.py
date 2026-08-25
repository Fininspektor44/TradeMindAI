"""Canonical MT5 account identities and their roles (v1).

EXACTLY TWO real MT5 account identities may exist anywhere in this project,
and each has ONE role that the other may never assume:

  77053345  MARKET DATA ONLY.   Read-only quotes/history/symbol metadata.
                                 It may NEVER place, modify, or resume an
                                 order, under any circumstance.
  67206924  DEMO EXECUTION ONLY. The single demo account supervised demo
                                 execution may target, and only through the
                                 existing demo-account safety gate.

Every other login -- an obsolete real account, a production account, a
typo, an empty string, a non-string -- FAILS CLOSED here. There is no
force, override, bypass, or "allow everything" parameter in this module,
and no environment variable or config file can widen the set: widening
execution authority must be a reviewed source change, exactly like
``ser8_core8_market_only_policy.CORE_8_SYMBOLS``.

WHY A SEPARATE MODULE
---------------------
These constants previously lived only in ``ser8_historical_data`` (a
research/historical module that transitively pulls dataset and replay
machinery). Runtime execution paths must be able to ask "is this login
allowed to execute?" without importing research code, so this module owns
the identities and both modules now agree by construction -- see
``ser8_historical_data``, which imports them from here rather than
redeclaring its own copies.

RELATIONSHIP TO THE DEMO ACCOUNT SAFETY GATE
--------------------------------------------
This module is ADDITIVE and does not replace
``ser8_demo_account_safety_gate``. That gate answers "did an operator
explicitly allowlist this account for this claim?"; this module answers
the prior, non-negotiable question "is this login permitted to execute AT
ALL?". The gate calls :func:`verify_execution_account` first, so an
operator who mistakenly allowlists the market-data account -- or any other
login -- is still refused. Both checks must pass; neither can substitute
for the other.
"""

from __future__ import annotations

#: Read-only market data. Never execution.
MARKET_DATA_ACCOUNT_LOGIN = "77053345"

#: The only account supervised demo execution may ever target.
DEMO_EXECUTION_ACCOUNT_LOGIN = "67206924"

#: Every real MT5 account identity this project recognises, in any role.
CANONICAL_ACCOUNT_LOGINS = frozenset(
    {MARKET_DATA_ACCOUNT_LOGIN, DEMO_EXECUTION_ACCOUNT_LOGIN}
)

#: Human-readable role per canonical login, for error messages and audit.
ACCOUNT_ROLES = {
    MARKET_DATA_ACCOUNT_LOGIN: "MARKET_DATA_ONLY",
    DEMO_EXECUTION_ACCOUNT_LOGIN: "DEMO_EXECUTION_ONLY",
}


class MT5AccountRoleError(RuntimeError):
    """Raised whenever a login is used in a role it may not hold.

    Always raised BEFORE anything is authorized, claimed, or sent. There is
    no "denied but proceed anyway" return value.
    """


def _as_login(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise MT5AccountRoleError(
            f"{field_name} must be a non-empty string; refusing to act on an "
            "unidentified MT5 account"
        )
    return value


def is_canonical_execution_account(login: object) -> bool:
    """Exact-match test -- no case-folding, no whitespace tolerance."""
    return type(login) is str and login == DEMO_EXECUTION_ACCOUNT_LOGIN


def is_canonical_market_data_account(login: object) -> bool:
    """Exact-match test -- no case-folding, no whitespace tolerance."""
    return type(login) is str and login == MARKET_DATA_ACCOUNT_LOGIN


def verify_execution_account(login: object) -> str:
    """Return ``login`` iff it is exactly the demo execution account.

    Fails closed for every other value. The market-data account is called
    out explicitly, because that is the one mistake most likely to be made
    by a human wiring a task or an allowlist by hand.
    """
    value = _as_login(login, field_name="execution account login")
    if is_canonical_execution_account(value):
        return value
    if is_canonical_market_data_account(value):
        raise MT5AccountRoleError(
            f"account {value!r} is MARKET DATA ONLY and may never execute; "
            f"supervised demo execution requires exactly "
            f"{DEMO_EXECUTION_ACCOUNT_LOGIN!r}"
        )
    raise MT5AccountRoleError(
        f"account {value!r} is not the canonical demo execution account "
        f"{DEMO_EXECUTION_ACCOUNT_LOGIN!r}; refusing to execute"
    )


def verify_market_data_account(login: object) -> str:
    """Return ``login`` iff it is exactly the market-data account."""
    value = _as_login(login, field_name="market data account login")
    if is_canonical_market_data_account(value):
        return value
    if is_canonical_execution_account(value):
        raise MT5AccountRoleError(
            f"account {value!r} is DEMO EXECUTION ONLY; market data must be "
            f"read from {MARKET_DATA_ACCOUNT_LOGIN!r}"
        )
    raise MT5AccountRoleError(
        f"account {value!r} is not the canonical market data account "
        f"{MARKET_DATA_ACCOUNT_LOGIN!r}; refusing to proceed"
    )


__all__ = [
    "ACCOUNT_ROLES",
    "CANONICAL_ACCOUNT_LOGINS",
    "DEMO_EXECUTION_ACCOUNT_LOGIN",
    "MARKET_DATA_ACCOUNT_LOGIN",
    "MT5AccountRoleError",
    "is_canonical_execution_account",
    "is_canonical_market_data_account",
    "verify_execution_account",
    "verify_market_data_account",
]
