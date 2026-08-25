from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from trademind.fx_research import build_fx_observations
from trademind.fx_signal_adapter import build_candidates
from trademind.ote_engine import build_ote_signals
from trademind.volatility import average_true_range

from test_smc_ote import _synthetic_rows


def test_ote_directions_ignore_removed_legacy_columns() -> None:
    rows = _synthetic_rows()
    baseline = [(row["signal_id"], row["action"]) for row in build_ote_signals(rows)]
    assert {action for _, action in baseline} == {"BUY", "SELL"}

    first_key = "e" + "ma_fast"
    second_key = "e" + "ma_slow"
    oscillator_key = "r" + "si"
    contaminated = [
        {**row, first_key: "999", second_key: "-999", oscillator_key: "0"}
        for row in rows
    ]

    assert [(row["signal_id"], row["action"]) for row in build_ote_signals(contaminated)] == baseline


def test_no_ote_signal_means_no_candidate() -> None:
    rows = _synthetic_rows()[:35]
    observations = build_fx_observations(rows, symbols=("EURUSD",))
    candidates, errors = build_candidates(observations)

    assert observations == []
    assert candidates == []
    assert errors == []


def test_historical_and_live_paths_cannot_import_retired_direction_container() -> None:
    module_name = "trademind.signals." + "engine"
    assert importlib.util.find_spec(module_name) is None
    for path in (
        Path("src/trademind/ser8_historical_replay.py"),
        Path("src/trademind/live_signal_runtime.py"),
        Path("src/trademind/live_signal_runtime_v122.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert module_name not in source
        assert ("Signal" + "Engine") not in source


def test_atr_is_deterministic_and_has_no_direction_output() -> None:
    from trademind.ote_models import candle_from_row

    candles = [candle_from_row(row, 0) for row in _synthetic_rows()[:20]]
    first = average_true_range(candles)
    second = average_true_range(tuple(candles))

    assert first == pytest.approx(second)
    assert first > 0
    assert isinstance(first, float)
