"""Tests for SER8 MT5 HISTORY SERVER-TIME FIX V1.

Real Windows failure this task fixes: v1.3 successfully created
mt5_risk_orders_utc_67206924.csv / mt5_risk_deals_utc_67206924.csv, but
both contained a header row only -- zero data rows -- even though real
broker tickets 733124339 and 733124518 definitely existed and were
filled. Automatic reconciliation therefore correctly reported those legs
AMBIGUOUS (their tickets were simply absent from the exported history).

Confirmed root cause: MQL5's HistorySelect(from, to) requires its
interval in BROKER/SERVER time. The v1.3 code built both bounds from
TimeGMT() (GMT, not server time). On a broker server ahead of GMT (the
common case), a recent order's own server-time timestamp can fall AFTER
a TimeGMT()-based upper bound, silently excluding it from the selected
range -- a genuine clock-domain bug, not a filtering, magic-number, or
reconciliation-logic bug.

No MQL5 compiler is available in this environment (a standing constraint
throughout this session for every MT5-related task), so every proof here
is a static source-scan of the .mq5 file, reusing the exact technique
already established in tests/test_ser8_mt5_order_deal_history_export.py
and tests/test_ser8_mt5_positions_snapshot_fix.py.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_PATH = REPO_ROOT / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"


def _source() -> str:
    return EXECUTOR_PATH.read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    marker = re.search(rf"\b{re.escape(function_name)}\s*\([^)]*\)\s*\n\{{", source)
    assert marker, f"could not locate function {function_name}"
    start = marker.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced braces scanning {function_name}")


# ---------------------------------------------------------------------------
# 1-3: both HistorySelect boundaries use server time (TimeCurrent), never
# TimeGMT, and both from/to share the SAME basis.
# ---------------------------------------------------------------------------


def test_order_history_uses_timecurrent_for_both_bounds() -> None:
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    assert "datetime from=TimeCurrent()-InpHistoryLookbackDays*86400;" in body
    assert "datetime to=TimeCurrent();" in body
    assert "HistorySelect(from,to)" in body


def test_deal_history_uses_timecurrent_for_both_bounds() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    assert "datetime from=TimeCurrent()-InpHistoryLookbackDays*86400;" in body
    assert "datetime to=TimeCurrent();" in body
    assert "HistorySelect(from,to)" in body


def test_neither_history_exporter_uses_timegmt_for_historyselect() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        # Only actual code lines count -- explanatory comments legitimately
        # NAME TimeGMT (to explain what was wrong / what changed) without
        # calling it.
        code_lines = [
            line for line in body.splitlines()
            if not line.strip().startswith("//")
        ]
        for line in code_lines:
            assert "TimeGMT(" not in line, f"{function_name}: unexpected TimeGMT() call in {line!r}"


def test_historyselect_call_itself_never_invokes_timegmt_inline() -> None:
    """Defends specifically against a regression shaped like the ORIGINAL
    bug -- TimeGMT() called directly inside the HistorySelect(...) call
    expression itself, e.g. HistorySelect(from,TimeGMT())."""
    source = _source()
    assert "HistorySelect(from,TimeGMT())" not in source
    assert "HistorySelect(TimeGMT()" not in source
    assert re.search(r"HistorySelect\([^)]*TimeGMT\(\)", source) is None


# ---------------------------------------------------------------------------
# 4: UtcNowMsc() and the exported capture timestamp semantics (and timer
# cadence) are unchanged -- this fix touches ONLY the HistorySelect query
# interval.
# ---------------------------------------------------------------------------


def test_utc_now_msc_still_uses_timegmt_unchanged() -> None:
    source = _source()
    assert "return (long)TimeGMT()*1000;" in source


def test_captured_msc_still_derived_from_utc_now_msc_in_both_exports() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        assert "long captured_msc=UtcNowMsc();" in body


def test_timer_cadence_clock_untouched() -> None:
    source = _source()
    assert "g_last_risk_snapshot_at=TimeGMT();" in source
    body = _function_body(source, "OnTimer")
    assert "datetime now=TimeGMT();" in body


# ---------------------------------------------------------------------------
# 5: magic filtering is unweakened.
# ---------------------------------------------------------------------------


def test_magic_filtering_unchanged_in_both_exports() -> None:
    order_body = _function_body(_source(), "ExportOrderHistorySnapshot")
    assert order_body.count("!=InpMagicNumber") == 2  # active-orders loop + history loop.
    deal_body = _function_body(_source(), "ExportDealHistorySnapshot")
    assert "DEAL_MAGIC)!=InpMagicNumber" in deal_body


# ---------------------------------------------------------------------------
# 6: order execution, ProcessPendingRequest, sizing, SL/TP, and everything
# else outside the two history exports are untouched.
# ---------------------------------------------------------------------------


def test_process_pending_request_unchanged() -> None:
    source = _source()
    body = _function_body(source, "ProcessPendingRequest")
    assert "trade.Buy(" in body and "trade.Sell(" in body
    assert "trade.BuyLimit(" in body and "trade.SellLimit(" in body
    assert "trade.BuyStop(" in body and "trade.SellStop(" in body
    assert "TimeCurrent()" not in body  # this fix never touches order execution's own clock use (it has none).


def test_no_trading_call_added_to_either_history_export() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        assert "trade." not in body
        assert "OrderSend(" not in body
        assert "OrderSendAsync(" not in body


def test_volume_still_assigned_exactly_once_file_wide() -> None:
    source = _source()
    assignments = [
        line for line in source.splitlines()
        if line.strip().startswith("volume") and "=" in line and "==" not in line
    ]
    assert len(assignments) == 1, assignments


# ---------------------------------------------------------------------------
# 7: explicit diagnostics on a HistorySelect failure -- function/export
# name + GetLastError(); a successful zero-row history is still valid.
# ---------------------------------------------------------------------------


def test_order_history_logs_function_name_and_last_error_on_historyselect_failure() -> None:
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    else_match = re.search(r"if\(HistorySelect\(from,to\)\)\s*\{.*?\}\s*else\s*\{(.*?)\}", body, re.S)
    assert else_match, "expected an else branch on the HistorySelect(...) call"
    else_body = else_match.group(1)
    assert "ExportOrderHistorySnapshot" in else_body
    assert "GetLastError()" in else_body
    assert "PrintFormat(" in else_body or "Print(" in else_body


def test_deal_history_logs_function_name_and_last_error_on_historyselect_failure() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    else_match = re.search(r"if\(HistorySelect\(from,to\)\)\s*\{.*?\}\s*else\s*\{(.*?)\}", body, re.S)
    assert else_match, "expected an else branch on the HistorySelect(...) call"
    else_body = else_match.group(1)
    assert "ExportDealHistorySnapshot" in else_body
    assert "GetLastError()" in else_body
    assert "PrintFormat(" in else_body or "Print(" in else_body


def test_zero_row_history_is_not_treated_as_a_failure() -> None:
    """The else branch only fires when HistorySelect ITSELF returns false
    -- a successful call that simply finds nothing to report (the common,
    valid case) takes the `if` branch and logs nothing extra beyond the
    normal 'orders=0'/'deals=0' summary line."""
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        if_index = body.index("if(HistorySelect(from,to))")
        else_index = body.index("else", if_index)
        # The success branch (between `if` and `else`) contains no
        # GetLastError() call of its own -- only the failure branch does.
        success_branch = body[if_index:else_index]
        assert "GetLastError()" not in success_branch


# ---------------------------------------------------------------------------
# Sanity: manual brace/paren balance (no MQL5 compiler available).
# ---------------------------------------------------------------------------


def test_executor_file_braces_and_parens_balanced() -> None:
    source = _source()
    assert source.count("{") == source.count("}")
    assert source.count("(") == source.count(")")
