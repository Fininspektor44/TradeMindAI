"""Tests for additive frozen-prospective-symbol coverage in the live MT5 ECN
observation runtime (trademind.fx_research / trademind.live_signal_runtime).

Covers: the existing FX symbol universe is fully preserved; the three frozen
prospective symbols (.USTECHCASH, .US30CASH, XAGUSD) are additively
collected; duplicates/overlap are structurally impossible; an MT5-side
naming/availability gap is surfaced explicitly in runtime status rather than
silently dropped; the observations.csv schema is unchanged; no order/trading
path is enabled; and symbol selection is deterministic.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trademind.fx_research import (
    _OBSERVATION_FIELDS,
    FROZEN_PROSPECTIVE_SYMBOLS,
    FX_MAJORS,
    LIVE_OBSERVATION_SYMBOLS,
    build_fx_observations,
    load_volume_rows,
    observed_symbols,
)
from trademind.live_signal_runtime import closed_volume_rows, run_live_runtime
from trademind.ser8_core8_market_only_policy import CORE_8_SYMBOLS_ORDERED
from trademind.signal_passport_factory import FactoryRun
from trademind.signal_to_risk_bridge import BridgeRun
from trademind.volume import VolumeCollectSummary
from test_smc_ote import _synthetic_rows

NOW = datetime(2026, 8, 5, 12, 6, tzinfo=timezone.utc)

_CANONICAL_VOLUME_FIELDS = (
    "schema_version",
    "time",
    "symbol",
    "timeframe",
    "bar_seconds",
    "point",
    "open",
    "high",
    "low",
    "close",
    "bar_tick_volume",
    "tick_count",
    "tick_rate_per_sec",
    "bid_up",
    "bid_down",
    "ask_up",
    "ask_down",
    "mid_up",
    "mid_down",
    "buy_ticks",
    "sell_ticks",
    "trade_volume",
    "trade_volume_real",
    "spread_mean_points",
    "spread_min_points",
    "spread_max_points",
    "spread_last_points",
    "spread_expansion_points",
    "realized_abs_move_points",
    "direction_imbalance",
    "delta_proxy",
    "rvol_20",
    "volume_percentile_100",
    "range_per_tick_points",
    "body_per_tick_points",
    "tick_copy_status",
    "tick_copy_error",
)


def _volume_row(index: int, *, symbol: str = "EURUSD") -> dict[str, str]:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    opened = 1.10000 + index * 0.00010
    closed = opened + 0.00005
    return {
        "schema_version": "1.4",
        "time": str(int((start + timedelta(minutes=5 * index)).timestamp())),
        "symbol": symbol,
        "timeframe": "M5",
        "bar_seconds": "300",
        "point": "0.00001",
        "open": str(opened),
        "high": str(closed + 0.00002),
        "low": str(opened - 0.00002),
        "close": str(closed),
        "bar_tick_volume": str(300 + index),
        "tick_count": str(300 + index),
        "tick_rate_per_sec": str((300 + index) / 300),
        "bid_up": "150",
        "bid_down": "80",
        "ask_up": "150",
        "ask_down": "80",
        "mid_up": "150",
        "mid_down": "80",
        "buy_ticks": "0",
        "sell_ticks": "0",
        "trade_volume": "0",
        "trade_volume_real": "0",
        "spread_mean_points": "2",
        "spread_min_points": "1",
        "spread_max_points": "3",
        "spread_last_points": "2",
        "spread_expansion_points": "0",
        "realized_abs_move_points": "20",
        "direction_imbalance": "0.30",
        "delta_proxy": "70",
        "rvol_20": "1.5",
        "volume_percentile_100": "90",
        "range_per_tick_points": "0.003",
        "body_per_tick_points": "0.002",
        "tick_copy_status": "OK",
        "tick_copy_error": "0",
    }


def _ote_rows(symbol: str) -> list[dict[str, str]]:
    return [
        {
            **row,
            "schema_version": "1.4",
            "symbol": symbol,
            "bar_seconds": "300",
        }
        for row in _synthetic_rows()
    ]


def _write_canonical_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CANONICAL_VOLUME_FIELDS))
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Existing FX symbol universe preserved.
# ---------------------------------------------------------------------------


def test_fx_majors_constant_is_untouched() -> None:
    assert tuple(FX_MAJORS) == (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "USDCAD",
        "AUDUSD",
        "NZDUSD",
    )


def test_all_existing_fx_symbols_remain_in_the_live_observation_universe() -> None:
    for symbol in FX_MAJORS:
        assert symbol in LIVE_OBSERVATION_SYMBOLS


def test_existing_fx_symbol_still_flows_through_load_volume_rows(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    _write_canonical_csv(path, [_volume_row(0, symbol="EURUSD")])
    rows, _ = load_volume_rows(path)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "EURUSD"


# ---------------------------------------------------------------------------
# Frozen prospective symbols additively covered.
# ---------------------------------------------------------------------------


def test_frozen_prospective_symbols_are_exactly_the_three_candidates() -> None:
    assert FROZEN_PROSPECTIVE_SYMBOLS == (".USTECHCASH", ".US30CASH", "XAGUSD")


def test_ustechcash_is_covered_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    rows_in = _ote_rows(".USTECHCASH")
    _write_canonical_csv(path, rows_in)
    loaded, _ = load_volume_rows(path)
    assert len(loaded) == len(rows_in)
    observations = build_fx_observations(loaded)
    assert observations
    assert all(obs["symbol"] == ".USTECHCASH" for obs in observations)


def test_us30cash_is_covered_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    rows_in = _ote_rows(".US30CASH")
    _write_canonical_csv(path, rows_in)
    loaded, _ = load_volume_rows(path)
    assert len(loaded) == len(rows_in)
    observations = build_fx_observations(loaded)
    assert observations
    assert all(obs["symbol"] == ".US30CASH" for obs in observations)


def test_xagusd_is_covered_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    rows_in = _ote_rows("XAGUSD")
    _write_canonical_csv(path, rows_in)
    loaded, _ = load_volume_rows(path)
    assert len(loaded) == len(rows_in)
    observations = build_fx_observations(loaded)
    assert observations
    assert all(obs["symbol"] == "XAGUSD" for obs in observations)


def test_frozen_symbols_also_flow_through_closed_volume_rows() -> None:
    server_epoch = int(datetime(2026, 8, 5, 11, 55, tzinfo=timezone.utc).timestamp())
    rows = [_volume_row(0, symbol=symbol) for symbol in FROZEN_PROSPECTIVE_SYMBOLS]
    for row in rows:
        row["time"] = str(server_epoch)
    closed = closed_volume_rows(
        rows, now=NOW, server_utc_offset_hours=0, close_grace_seconds=5
    )
    assert {row["symbol"] for row in closed} == set(FROZEN_PROSPECTIVE_SYMBOLS)


# ---------------------------------------------------------------------------
# Duplicate prevention.
# ---------------------------------------------------------------------------


def test_live_observation_symbols_has_no_duplicates() -> None:
    assert len(LIVE_OBSERVATION_SYMBOLS) == len(set(LIVE_OBSERVATION_SYMBOLS))


def test_frozen_prospective_symbols_are_disjoint_from_fx_majors() -> None:
    assert not set(FX_MAJORS) & set(FROZEN_PROSPECTIVE_SYMBOLS)


def test_live_observation_symbols_is_exactly_the_additive_union() -> None:
    assert set(LIVE_OBSERVATION_SYMBOLS) == (
        set(FX_MAJORS) | set(FROZEN_PROSPECTIVE_SYMBOLS) | set(CORE_8_SYMBOLS_ORDERED)
    )


# ---------------------------------------------------------------------------
# Unavailable frozen symbol reported explicitly, not silently dropped.
# ---------------------------------------------------------------------------


def test_observed_symbols_reports_the_raw_symbol_set(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    _write_canonical_csv(
        path,
        [_volume_row(0, symbol="EURUSD"), _volume_row(0, symbol=".USTECHCASH")],
    )
    assert observed_symbols(path) == {"EURUSD", ".USTECHCASH"}


def test_observed_symbols_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    assert observed_symbols(tmp_path / "does-not-exist.csv") == set()


def test_run_live_runtime_reports_missing_frozen_symbols_explicitly(
    monkeypatch, tmp_path: Path
) -> None:
    # MT5 only exposes EURUSD this run -- none of the three frozen symbols.
    canonical = tmp_path / "volume.csv"
    _write_canonical_csv(canonical, [_volume_row(0, symbol="EURUSD")])
    historical = tmp_path / "historical.jsonl"
    historical.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "trademind.live_signal_runtime.collect_volume_files",
        lambda *_args, **_kwargs: VolumeCollectSummary(
            source_files=1, rows_read=1, canonical_rows=1, duplicate_keys=0,
            invalid_rows=0, output_path=canonical,
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_factory",
        lambda **_kwargs: FactoryRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT", "publishable": 0},
            evaluations=(), passport_paths=(),
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_bridge",
        lambda **_kwargs: BridgeRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT"}, package=None
        ),
    )

    run = run_live_runtime(
        login="77053345",
        volume_source_dir=tmp_path / "source",
        canonical_volume_path=canonical,
        historical_outcomes_path=historical,
        runtime_root=tmp_path / "runtime",
        account_csv=tmp_path / "account.csv",
        positions_csv=tmp_path / "positions.csv",
        symbols_csv=tmp_path / "symbols.csv",
        risk_profile_path=tmp_path / "profile.json",
        server_utc_offset_hours=0,
        now=NOW,
    )
    coverage = run.status["frozen_prospective_symbol_coverage"]
    assert coverage["requested"] == list(FROZEN_PROSPECTIVE_SYMBOLS)
    assert set(coverage["missing"]) == set(FROZEN_PROSPECTIVE_SYMBOLS)


def test_run_live_runtime_reports_no_missing_symbols_when_all_present(
    monkeypatch, tmp_path: Path
) -> None:
    canonical = tmp_path / "volume.csv"
    rows = [_volume_row(0, symbol="EURUSD")] + [
        _volume_row(0, symbol=symbol) for symbol in FROZEN_PROSPECTIVE_SYMBOLS
    ]
    _write_canonical_csv(canonical, rows)
    historical = tmp_path / "historical.jsonl"
    historical.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "trademind.live_signal_runtime.collect_volume_files",
        lambda *_args, **_kwargs: VolumeCollectSummary(
            source_files=1, rows_read=4, canonical_rows=4, duplicate_keys=0,
            invalid_rows=0, output_path=canonical,
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_factory",
        lambda **_kwargs: FactoryRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT", "publishable": 0},
            evaluations=(), passport_paths=(),
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_bridge",
        lambda **_kwargs: BridgeRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT"}, package=None
        ),
    )

    run = run_live_runtime(
        login="77053345",
        volume_source_dir=tmp_path / "source",
        canonical_volume_path=canonical,
        historical_outcomes_path=historical,
        runtime_root=tmp_path / "runtime",
        account_csv=tmp_path / "account.csv",
        positions_csv=tmp_path / "positions.csv",
        symbols_csv=tmp_path / "symbols.csv",
        risk_profile_path=tmp_path / "profile.json",
        server_utc_offset_hours=0,
        now=NOW,
    )
    assert run.status["frozen_prospective_symbol_coverage"]["missing"] == []


# ---------------------------------------------------------------------------
# observations.csv schema unchanged.
# ---------------------------------------------------------------------------


def test_observation_field_schema_is_unchanged_for_a_frozen_symbol() -> None:
    rows_in = _ote_rows(".US30CASH")
    observations = build_fx_observations(rows_in)
    assert observations
    assert set(observations[0].keys()) == set(_OBSERVATION_FIELDS)


def test_observation_field_schema_identical_for_fx_and_frozen_symbol() -> None:
    fx_observations = build_fx_observations(_ote_rows("EURUSD"))
    frozen_observations = build_fx_observations(
        _ote_rows("XAGUSD")
    )
    assert set(fx_observations[0].keys()) == set(frozen_observations[0].keys())


# ---------------------------------------------------------------------------
# No order/trading path enabled.
# ---------------------------------------------------------------------------


def test_no_order_or_trading_tokens_in_changed_modules() -> None:
    forbidden = ("CTrade", "OrderSend(", ".Buy(", ".Sell(", "PositionClose(", "TRADE_ACTION_DEAL")
    for relative_path in ("src/trademind/fx_research.py", "src/trademind/live_signal_runtime.py"):
        text = Path(relative_path).read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), relative_path


def test_run_live_runtime_safety_flags_remain_all_off(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "volume.csv"
    _write_canonical_csv(canonical, [_volume_row(0, symbol="EURUSD")])
    historical = tmp_path / "historical.jsonl"
    historical.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "trademind.live_signal_runtime.collect_volume_files",
        lambda *_args, **_kwargs: VolumeCollectSummary(
            source_files=1, rows_read=1, canonical_rows=1, duplicate_keys=0,
            invalid_rows=0, output_path=canonical,
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_factory",
        lambda **_kwargs: FactoryRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT", "publishable": 0},
            evaluations=(), passport_paths=(),
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_bridge",
        lambda **_kwargs: BridgeRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT"}, package=None
        ),
    )

    run = run_live_runtime(
        login="77053345",
        volume_source_dir=tmp_path / "source",
        canonical_volume_path=canonical,
        historical_outcomes_path=historical,
        runtime_root=tmp_path / "runtime",
        account_csv=tmp_path / "account.csv",
        positions_csv=tmp_path / "positions.csv",
        symbols_csv=tmp_path / "symbols.csv",
        risk_profile_path=tmp_path / "profile.json",
        server_utc_offset_hours=0,
        now=NOW,
    )
    safety = run.status["safety"]
    assert safety == {
        "read_only": True,
        "orders_enabled": False,
        "signal_publication_enabled": False,
        "broker_api_called": False,
        "historical_archive_mutated": False,
        "grid_robots_used_as_signal_source": False,
    }


# ---------------------------------------------------------------------------
# Deterministic symbol selection.
# ---------------------------------------------------------------------------


def test_live_observation_symbols_order_is_stable() -> None:
    assert LIVE_OBSERVATION_SYMBOLS == (
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
        ".USTECHCASH", ".US30CASH", "XAGUSD",
        "CHFJPY", "EURJPY", "EURNZD", "GBPAUD", "GBPNZD", "NZDCAD", "NZDCHF",
    )


def test_build_fx_observations_is_deterministic_across_repeated_calls() -> None:
    rows = [_volume_row(index, symbol=".USTECHCASH") for index in range(50)]
    first = build_fx_observations(list(rows))
    second = build_fx_observations(list(rows))
    assert first == second


def test_build_fx_observations_symbol_output_order_matches_universe_order() -> None:
    rows = _ote_rows("XAGUSD") + _ote_rows("EURUSD")
    observations = build_fx_observations(rows)
    symbols_in_output_order = list(dict.fromkeys(obs["symbol"] for obs in observations))
    # EURUSD precedes XAGUSD in LIVE_OBSERVATION_SYMBOLS, regardless of input order.
    assert symbols_in_output_order == ["EURUSD", "XAGUSD"]
