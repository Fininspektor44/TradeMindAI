"""Tests for the unified mt5/TradeMind_Demo_Order_Executor_v1.mq5 -- ONE EA
performing both SER8 one-shot order execution and read-only risk snapshot
export, and for the corresponding Python-side auto-discovery fix in
scripts/run_ser8_real_demo_pipeline.py (the exporter's real filenames use a
``_utc_`` segment the script's discovery previously omitted).

None of this can be executed (no MT5 terminal in this environment); every
check here is a static source scan, the same technique
tests/test_ser8_mt5_demo_order_send.py's own MQL5 tests already use.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_PATH = REPO_ROOT / "mt5" / "TradeMind_Demo_Order_Executor_v1.mq5"
STANDALONE_EXPORTER_PATH = REPO_ROOT / "mt5" / "TradeMind_MT5_Risk_Snapshot_Exporter.mq5"


def _executor_source() -> str:
    assert EXECUTOR_PATH.is_file(), "expected mt5/TradeMind_Demo_Order_Executor_v1.mq5 to exist"
    return EXECUTOR_PATH.read_text(encoding="utf-8")


def _exporter_source() -> str:
    assert STANDALONE_EXPORTER_PATH.is_file(), "expected the standalone risk exporter to still exist"
    return STANDALONE_EXPORTER_PATH.read_text(encoding="utf-8")


def _code_lines(source: str) -> str:
    """Executable code only -- strips #property description lines and
    Print()/PrintFormat() log/documentation lines, the same
    "negative assertion, not the thing itself" filter
    tests/test_ser8_mt5_demo_order_send.py already established."""
    lines = [
        line for line in source.splitlines()
        if not line.strip().startswith("#property")
        and "Print(" not in line
        and "PrintFormat(" not in line
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. ONE EA is sufficient; ONE EventSetTimer only.
# ---------------------------------------------------------------------------


def test_exactly_one_event_set_timer_call() -> None:
    source = _executor_source()
    assert source.count("EventSetTimer(") == 1


def test_both_cadences_configurable_independently() -> None:
    source = _executor_source()
    assert "input int    InpPollSeconds       = 5;" in source
    assert "input int    InpRiskRefreshSeconds = 30;" in source
    # The two cadences are tracked with separate elapsed-time state, not a
    # shared counter -- proves they can genuinely differ from one another.
    assert "g_last_order_poll_at" in source
    assert "g_last_risk_snapshot_at" in source
    assert "now-g_last_order_poll_at>=InpPollSeconds" in source
    assert "now-g_last_risk_snapshot_at>=InpRiskRefreshSeconds" in source


def test_risk_refresh_minimum_still_enforced() -> None:
    source = _executor_source()
    assert "InpRiskRefreshSeconds<10" in source
    assert "INIT_PARAMETERS_INCORRECT" in source


# ---------------------------------------------------------------------------
# 2. Risk snapshot is built into the executor with the SAME schema/filenames.
# ---------------------------------------------------------------------------


def _header_row_after(source: str, function_name: str) -> str:
    start = source.index(f"bool {function_name}(")
    write_index = source.index('FileWrite(\n      handle,\n      "', start)
    end = source.index(");", write_index)
    return source[write_index:end]


def test_risk_snapshot_filenames_match_the_real_utc_convention() -> None:
    source = _executor_source()
    assert 'InpOutputFolder+"\\\\mt5_risk_account_utc_"+LoginText()+".csv"' in source
    assert 'InpOutputFolder+"\\\\mt5_risk_positions_utc_"+LoginText()+".csv"' in source
    assert 'InpOutputFolder+"\\\\mt5_risk_symbols_utc_"+LoginText()+".csv"' in source


def test_risk_snapshot_header_columns_match_standalone_exporter_exactly() -> None:
    executor_source = _executor_source()
    exporter_source = _exporter_source()
    for function_name in ("AppendAccountSnapshot", "ExportPositionSnapshot", "ExportSymbolSnapshot"):
        executor_header = _header_row_after(executor_source, function_name)
        exporter_header = _header_row_after(exporter_source, function_name)
        assert executor_header == exporter_header, function_name


def test_risk_snapshot_functions_present() -> None:
    source = _executor_source()
    for name in (
        "AppendAccountSnapshot", "ExportPositionSnapshot", "ExportSymbolSnapshot", "CollectRiskSnapshot",
    ):
        assert f"{name}(" in source


# ---------------------------------------------------------------------------
# 3. HARD ARCHITECTURE RULE: no lot sizing / risk decisions in MQL5.
# ---------------------------------------------------------------------------


def test_no_position_sizing_or_risk_math_anywhere_in_file() -> None:
    source = _code_lines(_executor_source())
    # No arithmetic combining account balance/equity/margin with a
    # percentage/risk figure to derive a lot size -- volume must only ever
    # be read from the request file.
    forbidden = (
        "ACCOUNT_BALANCE*", "ACCOUNT_EQUITY*", "ACCOUNT_BALANCE *", "ACCOUNT_EQUITY *",
        "RiskPercent", "risk_pct", "LotSize(", "CalculateVolume", "CalculateLot",
    )
    for term in forbidden:
        assert term not in source, term
    # `volume` is assigned exactly once, from the request file.
    assignments = re.findall(r"^\s*volume\s*=", source, flags=re.MULTILINE)
    assert len(assignments) == 1
    assert "volume           = StringToDouble(FileReadString(handle));" in source


def test_risk_snapshot_functions_never_send_or_modify_an_order() -> None:
    source = _code_lines(_executor_source())
    start = source.index("string TrimCopy(string value)")
    end = source.index("int OnInit()")
    risk_section = source[start:end]
    forbidden = ("trade.Buy(", "trade.Sell(", "trade.BuyLimit(", "trade.SellLimit(",
                 "trade.BuyStop(", "trade.SellStop(", "OrderSend(", "PositionClose(", "PositionModify(")
    for term in forbidden:
        assert term not in risk_section, term


# ---------------------------------------------------------------------------
# 4. Independence: a snapshot failure never authorizes an order, and an
# order failure never blocks a future snapshot.
# ---------------------------------------------------------------------------


def test_ontimer_calls_both_jobs_unconditionally_and_independently() -> None:
    source = _executor_source()
    timer_start = source.index("void OnTimer()")
    timer_end = source.index("void OnDeinit(")
    body = source[timer_start:timer_end]
    assert "ProcessPendingRequest();" in body
    assert "CollectRiskSnapshot();" in body
    # Neither call is nested inside the other's if-block -- each has its
    # own independent cadence-gate, proven by two separate `if(now-` guards.
    assert body.count("if(now-") == 2


def test_process_pending_request_never_calls_collect_risk_snapshot() -> None:
    source = _executor_source()
    start = source.index("void ProcessPendingRequest()")
    end = source.index("//===========")
    body = source[start:end]
    assert "CollectRiskSnapshot" not in body


def test_collect_risk_snapshot_never_calls_process_pending_request() -> None:
    source = _executor_source()
    start = source.index("void CollectRiskSnapshot()")
    end = source.index("int OnInit()")
    body = source[start:end]
    assert "ProcessPendingRequest" not in body


# ---------------------------------------------------------------------------
# 5. Existing order-safety protections preserved verbatim.
# ---------------------------------------------------------------------------


def test_one_shot_consume_before_send_preserved() -> None:
    source = _executor_source()
    consumed_index = source.index("RequestConsumedFilename")
    first_send_index = min(
        i for i in (source.find("trade.Buy("), source.find("trade.Sell(")) if i != -1
    )
    assert consumed_index < first_send_index


def test_demo_login_pin_preserved() -> None:
    source = _executor_source()
    assert "InpApprovedDemoLogin!=0 && AccountInfoInteger(ACCOUNT_LOGIN)!=InpApprovedDemoLogin" in source


def test_magic_number_verification_preserved() -> None:
    source = _executor_source()
    assert "magic!=InpMagicNumber" in source
    assert "input int    InpMagicNumber       = 990244;" in source


def test_volume_sl_tp_validation_preserved() -> None:
    source = _executor_source()
    assert "volume<=0 || sl<=0 || tp<=0" in source


def test_no_grid_averaging_martingale_or_autonomous_signal_logic() -> None:
    source = _code_lines(_executor_source())
    forbidden_terms = (
        "GridStep", "MaxOrdersInGrid", "UseGrid", "grid_step", "Martingale", "martingale",
        "AveragePrice", "averaging", "OnTick",
    )
    for term in forbidden_terms:
        assert term not in source, term


# ---------------------------------------------------------------------------
# 6. Standalone exporter remains available, unmodified in spirit, only no
# longer required for the SER8 production demo path.
# ---------------------------------------------------------------------------


def test_standalone_exporter_still_exists_and_is_read_only() -> None:
    source = _exporter_source()
    forbidden = ("OrderSend(", "OrderSendAsync(", "PositionModify(", "PositionClose(", "CTrade ", ".Buy(", ".Sell(")
    assert not any(token in source for token in forbidden)


def test_installer_script_for_unified_executor_exists() -> None:
    installer = REPO_ROOT / "scripts" / "install_ser8_demo_order_executor.ps1"
    assert installer.is_file()
    text = installer.read_text(encoding="utf-8")
    assert "TradeMind_Demo_Order_Executor_v1.mq5" in text
    assert "ONLY EA required" in text
