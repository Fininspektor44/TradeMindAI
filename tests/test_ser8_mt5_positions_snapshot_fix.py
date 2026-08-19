"""Tests for SER8 UNIFIED MT5 POSITIONS SNAPSHOT WRITER FIX V1.

Real Windows failure this task fixes:

    MT5 account/instrument snapshot could not be verified:
    mt5_risk_positions_utc_67206924.csv missing required fields.

Direct Windows inspection proved the file was NOT malformed CSV text but
162 bytes of literal 0x00 (NUL) bytes -- exactly the length of the header
row alone. Root cause (confirmed by direct source comparison, not
assumed): ExportPositionSnapshot in
mt5/TradeMind_Demo_Order_Executor_v1.mq5 rewrote the REAL, well-known
filename in place on every call (open with FILE_WRITE, write, close) --
unlike AppendAccountSnapshot, which only ever APPENDS to an already-
complete file. A concurrent reader (trademind.mt5_risk_adapter, polling
the same path) could observe a truncated/zero-filled file during that
non-atomic in-place rewrite window. The fix makes the write atomic: write
the complete new snapshot to a temp filename, flush, close, then
FileMove(...,FILE_REWRITE) it over the real filename in one step -- the
same rename-based mechanism ReadAndConsumeRequest already uses in this
same file for one-shot request consumption.

No MQL5 compiler is available in this environment (a standing constraint
throughout this session for every MT5-related task), so every proof here
is either a static source-scan of the .mq5 file or a proof that the
Python reader's own required-field contract is unchanged and unweakened.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention for new SER8 test
modules).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trademind.mt5_risk_adapter import (  # noqa: E402
    ACCOUNT_REQUIRED_FIELDS,
    POSITION_REQUIRED_FIELDS,
    SYMBOL_REQUIRED_FIELDS,
)

EXECUTOR_PATH = REPO_ROOT / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
STANDALONE_EXPORTER_PATH = REPO_ROOT / "mt5" / "TradeMind_MT5_Risk_Snapshot_Exporter.mq5"


def _source() -> str:
    return EXECUTOR_PATH.read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    """Extracts one function's body via simple brace-balance scanning --
    the same source-scan technique this session already established for
    non-Python .mq5 artifacts."""
    marker = re.search(rf"\b{re.escape(function_name)}\s*\([^)]*\)\s*\n\{{", source)
    assert marker, f"could not locate function {function_name}"
    start = marker.end() - 1  # index of the opening brace
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
# 1: root cause -- ExportPositionSnapshot no longer writes the real
# filename directly; it writes a temp file and atomically renames it.
# ---------------------------------------------------------------------------


def test_export_position_snapshot_writes_temp_file_first() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    assert "string temp_filename=RiskPositionsTempFilename();" in body
    # FileOpen's first argument (the path actually opened for writing) is
    # the temp_filename local variable, never `filename` (the real path)
    # directly.
    open_call = re.search(r"FileOpen\(\s*\n?\s*([A-Za-z_]+),", body)
    assert open_call, "expected a FileOpen(...) call in ExportPositionSnapshot"
    assert open_call.group(1) == "temp_filename"


def test_export_position_snapshot_atomically_replaces_the_real_file() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    assert "FileMove(temp_filename,FILE_COMMON,filename,FILE_COMMON|FILE_REWRITE)" in body
    # The rename happens AFTER FileFlush/FileClose on the temp handle --
    # never before the new content is fully written and durable.
    flush_index = body.index("FileFlush(handle)")
    close_index = body.index("FileClose(handle)")
    move_index = body.index("FileMove(temp_filename")
    assert flush_index < close_index < move_index


def test_no_direct_write_to_the_real_positions_filename() -> None:
    """No FileOpen call anywhere in ExportPositionSnapshot targets the real
    (non-temp) positions filename -- the only writable handle is the temp
    file; the real file is only ever touched by the atomic FileMove."""
    body = _function_body(_source(), "ExportPositionSnapshot")
    for line in body.splitlines():
        code = line.split("//", 1)[0]
        if "FileOpen(" in code:
            assert "RiskPositionsFilename()" not in code


# ---------------------------------------------------------------------------
# 2: with ZERO open positions, a valid header-only file is always written
# -- never zero-length, never skipped.
# ---------------------------------------------------------------------------


def test_header_write_is_unconditional_before_the_positions_loop() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    header_index = body.index('"time_msc","account_login","server","currency","position_ticket"')
    loop_index = body.index("PositionsTotal()")
    # The header FileWrite happens BEFORE the position loop even starts
    # counting positions -- it does not depend on total > 0.
    assert header_index < loop_index
    # And it is not inside any conditional (if/for) block of its own --
    # the header write is at the function's top level, so the nearest
    # unclosed brace before it must be the function's own opening brace
    # (exactly one more "{" than "}" seen so far), never an `if(...)`
    # block that could skip it for the zero-positions case.
    preceding = body[:header_index]
    assert preceding.count("{") - preceding.count("}") == 1


def test_temp_file_cleared_before_each_write_so_stale_content_cannot_leak() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    assert "FileIsExist(temp_filename,FILE_COMMON)" in body
    assert "FileDelete(temp_filename,FILE_COMMON)" in body
    delete_index = body.index("FileDelete(temp_filename,FILE_COMMON)")
    open_index = body.index("FileOpen(")
    assert delete_index < open_index


# ---------------------------------------------------------------------------
# 3: no NUL-filled (or truncated) output path is reachable -- every
# FileOpen failure path returns false WITHOUT ever calling FileMove, and
# every successful path always reaches FileMove before returning true.
# ---------------------------------------------------------------------------


def test_failed_temp_open_never_touches_the_real_file() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    open_failure_block = re.search(r"if\(handle==INVALID_HANDLE\)\s*\{([^}]*)\}", body, re.S)
    assert open_failure_block, "expected an INVALID_HANDLE guard"
    assert "FileMove" not in open_failure_block.group(1)
    assert "return false" in open_failure_block.group(1)


def test_failed_atomic_replace_cleans_up_the_temp_file_and_fails_closed() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    move_failure_block = re.search(r"if\(!FileMove\([^)]*\)\)\s*\{([^}]*)\}", body, re.S)
    assert move_failure_block, "expected a FileMove(...) failure guard"
    assert "return false" in move_failure_block.group(1)
    assert "FileDelete(temp_filename,FILE_COMMON)" in move_failure_block.group(1)


def test_no_early_return_true_before_the_atomic_replace() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    move_index = body.index("FileMove(temp_filename")
    before_move = body[:move_index]
    # "return true" must never appear before the atomic replace has been
    # attempted -- the only true-returning statement is the function's
    # final one, after FileMove succeeded.
    assert "return true" not in before_move


# ---------------------------------------------------------------------------
# 4: the header schema matches trademind.mt5_risk_adapter's own
# POSITION_REQUIRED_FIELDS exactly (a superset, matched by name via
# csv.DictReader -- not by column position).
# ---------------------------------------------------------------------------


def _mql5_header_fields(body: str, *, marker: str) -> list[str]:
    match = re.search(rf'{re.escape(marker)}\s*\(\s*\n\s*handle,\s*\n(.*?)\n\s*\);', body, re.S)
    assert match, f"could not find the {marker} header FileWrite call"
    literal_fields = re.findall(r'"([^"]+)"', match.group(1))
    assert literal_fields, "expected at least one quoted header field"
    return literal_fields


def test_positions_header_is_a_superset_of_the_python_required_fields() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    header_fields = _mql5_header_fields(body, marker="FileWrite")
    assert set(POSITION_REQUIRED_FIELDS) <= set(header_fields), (
        set(POSITION_REQUIRED_FIELDS) - set(header_fields)
    )


def test_positions_required_fields_constant_was_not_weakened() -> None:
    # Requirement 1: the Python reader's contract itself must be
    # completely unchanged by this task -- re-asserted here against the
    # exact literal set this task's own spec and mt5_risk_adapter.py both
    # already agree on.
    assert set(POSITION_REQUIRED_FIELDS) == {
        "time_msc", "account_login", "currency", "position_ticket", "position_id",
        "symbol", "side", "volume", "open_price", "current_price", "sl",
    }


# ---------------------------------------------------------------------------
# 5: positions rows preserve every required field, in the FileWrite call
# that emits each data row.
# ---------------------------------------------------------------------------


def test_position_data_row_writes_every_required_field() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    # The SECOND FileWrite(handle, ...) call (the header is the first) is
    # the per-position data row.
    row_write = list(re.finditer(r"FileWrite\(\s*\n\s*handle,\s*\n(.*?)\n\s*\);", body, re.S))
    assert len(row_write) == 2, "expected exactly one header FileWrite and one data-row FileWrite"
    row_args = row_write[1].group(1)
    required_symbol_map = {
        "time_msc": "captured_msc",
        "account_login": "ACCOUNT_LOGIN",
        "currency": "ACCOUNT_CURRENCY",
        "position_ticket": "ticket",
        "position_id": "POSITION_IDENTIFIER",
        "symbol": "symbol",
        "side": "side",
        "volume": "POSITION_VOLUME",
        "open_price": "POSITION_PRICE_OPEN",
        "current_price": "POSITION_PRICE_CURRENT",
        "sl": "POSITION_SL",
    }
    for field_name, source_token in required_symbol_map.items():
        assert source_token in row_args, f"expected {field_name} (via {source_token}) in the data row"


def test_position_volume_is_never_computed_only_read_from_the_real_position() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    assert "PositionGetDouble(POSITION_VOLUME)" in body
    # No arithmetic operators applied to a "volume"-named value anywhere
    # in this function -- it is read and formatted, never sized.
    for line in body.splitlines():
        if "volume" in line.lower() and any(op in line for op in ("+", "-", "*", "/")):
            # DoubleToString(...,8) has no arithmetic on volume itself --
            # only the digits-precision comma, not a sizing operator.
            assert "DoubleToString" in line, line


# ---------------------------------------------------------------------------
# 6: account/symbol snapshot writers are structurally unchanged.
# ---------------------------------------------------------------------------


def test_account_snapshot_still_appends_in_place_unchanged() -> None:
    source = _source()
    body = _function_body(source, "AppendAccountSnapshot")
    assert "FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE" in body
    assert "FileSeek(handle,0,SEEK_END)" in body
    assert "RiskAccountFilename()" in body
    # Never touched to use the new temp/atomic-replace mechanism -- that
    # pattern is scoped to positions only, per this task's own GOAL.
    assert "RiskPositionsTempFilename" not in body
    header_fields = _mql5_header_fields(body, marker="FileWrite")
    assert set(ACCOUNT_REQUIRED_FIELDS) <= set(header_fields)


def test_symbol_snapshot_writer_untouched_by_this_task() -> None:
    source = _source()
    body = _function_body(source, "ExportSymbolSnapshot")
    assert "RiskSymbolsFilename()" in body
    assert "RiskPositionsTempFilename" not in body
    header_fields = _mql5_header_fields(body, marker="FileWrite")
    assert set(SYMBOL_REQUIRED_FIELDS) <= set(header_fields)


# ---------------------------------------------------------------------------
# 7: no sizing, no grid/averaging/martingale added anywhere.
# ---------------------------------------------------------------------------


def test_no_position_sizing_language_introduced_in_the_fix() -> None:
    body = _function_body(_source(), "ExportPositionSnapshot")
    for forbidden in ("RiskDecision", "SizedOrder", "lot_size", "CalculateVolume", "risk_pct"):
        assert forbidden not in body


def test_no_grid_averaging_martingale_anywhere_in_the_executor() -> None:
    source = _source()
    functional_lines = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#property")
    )
    for forbidden in ("GridStep", "Martingale", "martingale", "AveragePrice", "averaging", "OnTick"):
        assert forbidden not in functional_lines


def test_order_execution_function_body_unchanged_by_this_fix() -> None:
    source = _source()
    body = _function_body(source, "ProcessPendingRequest")
    # Same trade dispatch this repository has relied on since the unified
    # executor was first built -- untouched by this task.
    assert "trade.Buy(" in body and "trade.Sell(" in body
    assert "trade.BuyLimit(" in body and "trade.SellLimit(" in body
    assert "trade.BuyStop(" in body and "trade.SellStop(" in body
    assert body.count("volume") >= 1
    # Volume assigned exactly once across the whole file (unchanged
    # invariant from tests/test_mt5_unified_executor.py).
    assignments = [
        line for line in source.splitlines()
        if line.strip().startswith("volume") and "=" in line and "==" not in line
    ]
    assert len(assignments) == 1, assignments


# ---------------------------------------------------------------------------
# Sanity: manual brace/paren balance (no MQL5 compiler available in this
# environment -- same standing limitation as every other MT5 task this
# session).
# ---------------------------------------------------------------------------


def test_executor_file_braces_and_parens_are_balanced() -> None:
    source = _source()
    assert source.count("{") == source.count("}")
    assert source.count("(") == source.count(")")


def test_standalone_exporter_left_untouched() -> None:
    # The separate, non-SER8 standalone exporter is a DIFFERENT product
    # line (see the TRADEMIND LEGACY RUNTIME PURGE audit); this task does
    # not touch it, so it may still have the theoretical same-shaped race
    # this task fixes for the unified executor -- an explicit, honest gap,
    # not silently claimed as fixed.
    assert STANDALONE_EXPORTER_PATH.is_file()
    standalone_source = STANDALONE_EXPORTER_PATH.read_text(encoding="utf-8")
    assert "RiskPositionsTempFilename" not in standalone_source
