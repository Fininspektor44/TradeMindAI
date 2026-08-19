"""Tests for the new read-only order/deal history exports added to
mt5/TradeMind_Demo_Order_Executor_v1.mq5 by SER8 AUTOMATIC MT5
RECONCILIATION V1 -- mt5_risk_orders_utc_<login>.csv and
mt5_risk_deals_utc_<login>.csv.

No MQL5 compiler is available in this environment (a standing constraint
throughout this session for every MT5-related task), so every proof here
is a static source-scan of the .mq5 file, matching the SAME technique
already established in tests/test_ser8_mt5_positions_snapshot_fix.py and
tests/test_mt5_unified_executor.py.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.ser8_mt5_execution_reconciliation import (  # noqa: E402
    DEAL_HISTORY_REQUIRED_FIELDS,
    ORDER_HISTORY_REQUIRED_FIELDS,
)

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


def _mql5_header_fields(body: str) -> list[str]:
    match = re.search(r'FileWrite\(\s*\n\s*handle,\s*\n(.*?)\n\s*\);', body, re.S)
    assert match, "expected the header FileWrite call"
    return re.findall(r'"([^"]+)"', match.group(1))


# ---------------------------------------------------------------------------
# 1: both new exports exist, are wired into CollectRiskSnapshot on the
# SAME timer, and never introduce a new EventSetTimer.
# ---------------------------------------------------------------------------


def test_both_new_export_functions_exist() -> None:
    source = _source()
    assert "bool ExportOrderHistorySnapshot()" in source
    assert "bool ExportDealHistorySnapshot()" in source


def test_collect_risk_snapshot_calls_both_new_exports() -> None:
    body = _function_body(_source(), "CollectRiskSnapshot")
    assert "ExportOrderHistorySnapshot()" in body
    assert "ExportDealHistorySnapshot()" in body
    # Unconditional -- one export failing never skips the others.
    assert "orders_ok=ExportOrderHistorySnapshot()" in body
    assert "deals_ok=ExportDealHistorySnapshot()" in body


def test_still_exactly_one_event_set_timer() -> None:
    source = _source()
    assert source.count("EventSetTimer(") == 1


def test_history_lookback_input_declared_and_validated() -> None:
    source = _source()
    assert "input int    InpHistoryLookbackDays = 30;" in source
    body = _function_body(source, "OnInit")
    assert "InpHistoryLookbackDays<1" in body


# ---------------------------------------------------------------------------
# 2: filenames match trademind.ser8_mt5_execution_reconciliation's own
# expected filenames exactly.
# ---------------------------------------------------------------------------


def test_order_and_deal_filenames_match_python_side_expectations() -> None:
    source = _source()
    assert '"\\\\mt5_risk_orders_utc_"' in source
    assert '"\\\\mt5_risk_deals_utc_"' in source


# ---------------------------------------------------------------------------
# 3: header schema is a superset of the Python reader's required fields.
# ---------------------------------------------------------------------------


def test_orders_header_is_a_superset_of_python_required_fields() -> None:
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    header_fields = _mql5_header_fields(body)
    assert set(ORDER_HISTORY_REQUIRED_FIELDS) <= set(header_fields), (
        set(ORDER_HISTORY_REQUIRED_FIELDS) - set(header_fields)
    )


def test_deals_header_is_a_superset_of_python_required_fields() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    header_fields = _mql5_header_fields(body)
    assert set(DEAL_HISTORY_REQUIRED_FIELDS) <= set(header_fields), (
        set(DEAL_HISTORY_REQUIRED_FIELDS) - set(header_fields)
    )


# ---------------------------------------------------------------------------
# 4: both new exports are filtered to InpMagicNumber only, and both are
# written atomically (temp file + FileMove), exactly like
# ExportPositionSnapshot.
# ---------------------------------------------------------------------------


def test_order_export_filters_by_magic_in_both_active_and_history_loops() -> None:
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    assert body.count("!=InpMagicNumber") == 2  # one guard in the active loop, one in the history loop.


def test_deal_export_filters_by_magic() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    assert "DEAL_MAGIC)!=InpMagicNumber" in body


def test_order_export_writes_atomically() -> None:
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    assert "RiskOrdersTempFilename()" in body
    assert "FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE)" in body
    flush_index = body.index("FileFlush(handle)")
    move_index = body.index("FileMove(temp_filename")
    assert flush_index < move_index
    assert "return true" not in body[:move_index]


def test_deal_export_writes_atomically() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    assert "RiskDealsTempFilename()" in body
    assert "FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE)" in body
    flush_index = body.index("FileFlush(handle)")
    move_index = body.index("FileMove(temp_filename")
    assert flush_index < move_index
    assert "return true" not in body[:move_index]


# ---------------------------------------------------------------------------
# 5: strictly read-only -- no OrderSend/CTrade call anywhere in either new
# function, and no sizing/grid/martingale language introduced.
# ---------------------------------------------------------------------------


def test_no_order_send_or_ctrade_call_in_either_new_export() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        assert "trade." not in body
        assert "OrderSend(" not in body
        assert "OrderSendAsync(" not in body


def test_no_position_sizing_or_grid_language_in_either_new_export() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        for forbidden in ("RiskDecision", "SizedOrder", "lot_size", "GridStep", "Martingale", "martingale"):
            assert forbidden not in body


def test_history_lookback_bounds_the_scan_never_unbounded() -> None:
    for function_name in ("ExportOrderHistorySnapshot", "ExportDealHistorySnapshot"):
        body = _function_body(_source(), function_name)
        assert "InpHistoryLookbackDays" in body
        assert "HistorySelect(from,TimeGMT())" in body


# ---------------------------------------------------------------------------
# 6: sanity -- balance and untouched order-execution semantics.
# ---------------------------------------------------------------------------


def test_executor_file_braces_and_parens_still_balanced() -> None:
    source = _source()
    assert source.count("{") == source.count("}")
    assert source.count("(") == source.count(")")


def test_order_execution_untouched_by_this_task() -> None:
    source = _source()
    body = _function_body(source, "ProcessPendingRequest")
    assert "trade.Buy(" in body and "trade.Sell(" in body
    assert "trade.BuyLimit(" in body and "trade.SellLimit(" in body
    assert "trade.BuyStop(" in body and "trade.SellStop(" in body
    assignments = [
        line for line in source.splitlines()
        if line.strip().startswith("volume") and "=" in line and "==" not in line
    ]
    assert len(assignments) == 1, assignments
