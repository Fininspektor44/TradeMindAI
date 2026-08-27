"""Tests for the SER8 AUTONOMOUS CONTINUOUS DEMO EXECUTION V1 v1.5 change
to mt5/TradeMind_Demo_Order_Executor_v1.mq5: ONE new read-only column
(``profit``, sourced from ``DEAL_PROFIT``) on the SAME
``ExportDealHistorySnapshot`` deal-history export.

No MQL5 compiler is available in this environment (a standing constraint
throughout this session for every MT5-related task), so every proof here
is a static source-scan of the .mq5 file, matching the SAME technique
already established in tests/test_ser8_mt5_order_deal_history_export.py.

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


def _mql5_header_fields(body: str) -> list[str]:
    match = re.search(r'FileWrite\(\s*\n\s*handle,\s*\n(.*?)\n\s*\);', body, re.S)
    assert match, "expected the header FileWrite call"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_version_includes_or_supersedes_profit_export_v1_5() -> None:
    source = _source()
    assert '#property version   "1.7"' in source
    assert "v1.5 adds ONE new read-only column" in source


def test_deal_header_gained_profit_as_the_last_column() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    header = _mql5_header_fields(body)
    assert header[-1] == "profit"
    assert header[:-1] == [
        "time_msc", "account_login", "deal_ticket", "order_ticket", "position_id", "symbol", "magic",
        "side", "volume", "price", "entry", "time_deal_msc",
    ]


def test_deal_row_writes_deal_profit_as_the_last_value() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    calls = re.findall(r'FileWrite\(\s*\n\s*handle,\s*\n(.*?)\n\s*\);', body, re.S)
    assert len(calls) == 2, "expected exactly two FileWrite calls: header, then one row"
    row_call = calls[1]
    assert "DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2)" in row_call
    # The row's own argument list -- the profit expression must be the
    # LAST value written, matching the header's own last column.
    lines = [line.strip().rstrip(",") for line in row_call.splitlines() if line.strip()]
    assert lines[-1] == "DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2)"


def test_order_export_untouched_by_this_change() -> None:
    """Requirement: no other column, row, or export changed."""
    body = _function_body(_source(), "ExportOrderHistorySnapshot")
    header = _mql5_header_fields(body)
    assert "profit" not in header
    assert "DEAL_PROFIT" not in body


def test_no_order_send_or_ctrade_call_in_the_deal_export() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    for forbidden in ("OrderSend(", "OrderSendAsync(", "trade.Buy(", "trade.Sell(", "trade.PositionClose("):
        assert forbidden not in body, forbidden


def test_deal_export_still_filters_by_magic_and_still_atomic() -> None:
    body = _function_body(_source(), "ExportDealHistorySnapshot")
    assert body.count("InpMagicNumber") >= 1
    assert "FileMove(" in body  # atomic temp-file-then-rename write, unchanged.


def test_executor_file_braces_and_parens_still_balanced() -> None:
    source = _source()
    assert source.count("{") == source.count("}")
    assert source.count("(") == source.count(")")


def test_docs_updated_for_v1_5() -> None:
    doc_path = REPO_ROOT / "docs" / "SER8_MT5_DEMO_ORDER_EXECUTOR_V1.md"
    text = doc_path.read_text(encoding="utf-8")
    assert "v1.5" in text
    assert "profit" in text
