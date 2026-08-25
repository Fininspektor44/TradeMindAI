"""Canonical MT5 account identity/role tests.

Exactly two real MT5 account identities may exist in this project:
77053345 (MARKET DATA ONLY) and 67206924 (DEMO EXECUTION ONLY). Every other
login fails closed, and the market-data account may never execute -- not
even when an operator explicitly allowlists it.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
from pathlib import Path

import pytest

from trademind.mt5_canonical_accounts import (
    ACCOUNT_ROLES,
    CANONICAL_ACCOUNT_LOGINS,
    DEMO_EXECUTION_ACCOUNT_LOGIN,
    MARKET_DATA_ACCOUNT_LOGIN,
    MT5AccountRoleError,
    is_canonical_execution_account,
    is_canonical_market_data_account,
    verify_execution_account,
    verify_market_data_account,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Deliberately synthetic, clearly non-real placeholders. Never runtime
#: defaults -- used only to prove non-canonical logins are refused.
NON_CANONICAL_LOGINS = ["99999999", "12345678", "00000001", "1", ""]


# --- canonical identities ------------------------------------------------


def test_exactly_two_canonical_accounts_exist():
    assert CANONICAL_ACCOUNT_LOGINS == {"77053345", "67206924"}
    assert len(CANONICAL_ACCOUNT_LOGINS) == 2


def test_canonical_logins_have_the_mandated_values_and_roles():
    assert MARKET_DATA_ACCOUNT_LOGIN == "77053345"
    assert DEMO_EXECUTION_ACCOUNT_LOGIN == "67206924"
    assert ACCOUNT_ROLES[MARKET_DATA_ACCOUNT_LOGIN] == "MARKET_DATA_ONLY"
    assert ACCOUNT_ROLES[DEMO_EXECUTION_ACCOUNT_LOGIN] == "DEMO_EXECUTION_ONLY"


def test_canonical_set_is_immutable():
    assert isinstance(CANONICAL_ACCOUNT_LOGINS, frozenset)
    with pytest.raises(AttributeError):
        CANONICAL_ACCOUNT_LOGINS.add("12345678")  # type: ignore[attr-defined]


# --- execution role: only 67206924 ---------------------------------------


def test_demo_execution_account_may_execute():
    assert verify_execution_account(DEMO_EXECUTION_ACCOUNT_LOGIN) == DEMO_EXECUTION_ACCOUNT_LOGIN
    assert is_canonical_execution_account(DEMO_EXECUTION_ACCOUNT_LOGIN) is True


def test_market_data_account_can_never_execute():
    with pytest.raises(MT5AccountRoleError, match="MARKET DATA ONLY"):
        verify_execution_account(MARKET_DATA_ACCOUNT_LOGIN)
    assert is_canonical_execution_account(MARKET_DATA_ACCOUNT_LOGIN) is False


@pytest.mark.parametrize("login", NON_CANONICAL_LOGINS)
def test_non_canonical_login_cannot_execute(login):
    with pytest.raises(MT5AccountRoleError):
        verify_execution_account(login)
    assert is_canonical_execution_account(login) is False


@pytest.mark.parametrize("login", [None, 67206924, b"67206924", ["67206924"], "  ", "67206924 "])
def test_malformed_or_padded_login_cannot_execute(login):
    with pytest.raises(MT5AccountRoleError):
        verify_execution_account(login)


# --- market-data role: only 77053345 -------------------------------------


def test_market_data_account_verifies():
    assert verify_market_data_account(MARKET_DATA_ACCOUNT_LOGIN) == MARKET_DATA_ACCOUNT_LOGIN
    assert is_canonical_market_data_account(MARKET_DATA_ACCOUNT_LOGIN) is True


def test_execution_account_is_not_a_market_data_account():
    with pytest.raises(MT5AccountRoleError, match="DEMO EXECUTION ONLY"):
        verify_market_data_account(DEMO_EXECUTION_ACCOUNT_LOGIN)


@pytest.mark.parametrize("login", NON_CANONICAL_LOGINS)
def test_non_canonical_login_is_not_market_data(login):
    with pytest.raises(MT5AccountRoleError):
        verify_market_data_account(login)


# --- no override / no widening -------------------------------------------


def test_no_override_or_bypass_parameter_exists():
    for function in (
        verify_execution_account,
        verify_market_data_account,
        is_canonical_execution_account,
        is_canonical_market_data_account,
    ):
        params = set(inspect.signature(function).parameters)
        assert not (params & {"force", "override", "bypass", "allow_all", "skip", "allowlist"})


def test_module_cannot_be_widened_by_environment_or_config():
    source = (REPO_ROOT / "src" / "trademind" / "mt5_canonical_accounts.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    # No os/env, no config file reading, no network -- the set is source-only.
    # (Checked against real code via AST, never raw text: the module's own
    # prose legitimately contains the word "environment".)
    assert roots <= {"__future__"}
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    assert not (referenced & {"getenv", "environ", "open", "load", "loads", "read_text"})


# --- repository-wide purge of obsolete real accounts ---------------------


def _tracked_text_files():
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for rel in out:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


#: Login-shaped literals that are legitimately NOT MT5 account logins.
#: Order/deal/position tickets and clearly synthetic placeholders.
_ALLOWED_NON_ACCOUNT_LOGIN_SHAPED = {
    "73312452", "73312453", "55555555",  # broker order tickets
    "99999999",                          # synthetic fail-closed placeholder
    "00024714", "00000000", "09000000",  # float/timestamp fragments
    "12345678", "00000001",              # synthetic placeholders in tests
}


def test_no_obsolete_real_mt5_account_appears_anywhere():
    """Every login-shaped literal in the repository is either a canonical
    account, a broker ticket, or a clearly synthetic placeholder. This is
    the repository-wide guard that an obsolete real account can never be
    reintroduced -- including as a comment, doc example, or fixture."""
    offenders = {}
    for rel, text in _tracked_text_files():
        if rel == "tests/test_mt5_canonical_accounts.py":
            continue  # this file names the allowed placeholders on purpose
        found = {
            token
            for token in re.findall(r"\b\d{8}\b", text)
            if token not in CANONICAL_ACCOUNT_LOGINS
            and token not in _ALLOWED_NON_ACCOUNT_LOGIN_SHAPED
        }
        if found:
            offenders[rel] = sorted(found)
    assert not offenders, f"unexpected login-shaped literals: {offenders}"


def test_no_runtime_default_selects_a_non_canonical_account():
    """No shipped script or source file may default a login/account
    argument to anything but a canonical account."""
    pattern = re.compile(
        r"""(?:--login|--account|\$Login|\$Account)[^\n]{0,80}?["']?(\d{8})["']?""",
        re.IGNORECASE,
    )
    offenders = {}
    for rel, text in _tracked_text_files():
        if rel.startswith("tests/"):
            continue
        bad = {m for m in pattern.findall(text) if m not in CANONICAL_ACCOUNT_LOGINS}
        if bad:
            offenders[rel] = sorted(bad)
    assert not offenders, f"non-canonical runtime account defaults: {offenders}"
