from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "trademind" / "bybit_shadow.py"
RUNNER = ROOT / "scripts" / "run_v110_bybit_shadow.ps1"
INSTALLER = ROOT / "scripts" / "install_v110_bybit_shadow_task.ps1"
CHECKER = ROOT / "scripts" / "check_v110_bybit_shadow.ps1"


def test_shadow_module_is_read_only_and_keeps_all_market_inputs() -> None:
    text = MODULE.read_text(encoding="utf-8")

    assert 'SCHEMA_VERSION = "1.10.0"' in text
    assert 'SCENARIO = "MTF_FLOW_ALIGNMENT"' in text
    assert '"context_timeframe": "H1"' in text
    assert '"decision_timeframe": "M15"' in text
    assert '"trigger_timeframe": "M5"' in text
    for feature in (
        "volume",
        "turnover",
        "trade_count",
        "delta_turnover",
        "cvd_turnover",
        "largest_trade_turnover",
        "book_imbalance_10",
        "open_interest",
        "funding_rate",
        "basis_bps",
        "spread_bps",
    ):
        assert feature in text
    for forbidden in (
        "OrderSend",
        "CTrade",
        "place_order",
        "create_order",
        "positionIdx",
        "api_key",
        "api_secret",
    ):
        assert forbidden not in text
    assert '"orders_enabled": False' in text
    assert '"forward_only": True' in text


def test_shadow_validation_keeps_strict_forward_evidence_thresholds() -> None:
    text = MODULE.read_text(encoding="utf-8")

    assert "completed >= 300" in text
    assert "days >= 30" in text
    assert "ci_low > 0" in text
    assert "no_late_collapse" in text
    assert "drawdown <= 20" in text
    assert "streak <= 8" in text
    assert "STOP_FIRST_CONSERVATIVE" in text


def test_shadow_task_is_hidden_direct_python_and_runs_every_five_minutes() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    checker = CHECKER.read_text(encoding="utf-8")

    assert 'Join-Path $projectRoot ".venv\\Scripts\\pythonw.exe"' in installer
    assert "-m trademind.bybit_shadow" in installer
    assert "-RepetitionInterval" in installer
    assert "IntervalMinutes = 5" in installer
    assert "-Hidden" in installer
    assert "MultipleInstances IgnoreNew" in installer
    assert "No orders" in installer
    assert "data\\bybit_v1_9\\bybit_bars.csv" in runner
    assert "data\\bybit_shadow_v1_10" in runner
    assert "OrdersEnabled" in checker
    assert "ForwardOnly" in checker
