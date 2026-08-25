from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trademind.live_signal_runtime import (
    RUN_COMPLETE,
    WAITING_NO_NEW_BARS,
    append_observations,
    closed_volume_rows,
    merge_evidence_outcomes,
    run_live_runtime,
)
from trademind.signal_passport_factory import FactoryRun
from trademind.signal_to_risk_bridge import BridgeRun
from trademind.volume import VolumeCollectSummary


NOW = datetime(2026, 8, 5, 12, 6, tzinfo=timezone.utc)


def _volume_row(*, server_epoch: int, symbol: str = "EURUSD") -> dict[str, str]:
    return {
        "schema_version": "1.4",
        "time": str(server_epoch),
        "symbol": symbol,
        "timeframe": "M5",
        "bar_seconds": "300",
        "point": "0.00001",
        "open": "1.1000",
        "high": "1.1020",
        "low": "1.0990",
        "close": "1.1010",
        "bar_tick_volume": "100",
        "tick_count": "100",
        "tick_rate_per_sec": "0.33",
        "bid_up": "50",
        "bid_down": "40",
        "ask_up": "52",
        "ask_down": "38",
        "mid_up": "51",
        "mid_down": "39",
        "buy_ticks": "0",
        "sell_ticks": "0",
        "trade_volume": "0",
        "trade_volume_real": "0",
        "spread_mean_points": "10",
        "spread_min_points": "8",
        "spread_max_points": "12",
        "spread_last_points": "10",
        "spread_expansion_points": "0",
        "realized_abs_move_points": "100",
        "direction_imbalance": "0.1",
        "delta_proxy": "10",
        "rvol_20": "1.4",
        "volume_percentile_100": "80",
        "range_per_tick_points": "3",
        "body_per_tick_points": "1",
        "tick_copy_status": "OK",
        "tick_copy_error": "0",
    }


def _observation(observation_id: str, signal_time: str) -> dict[str, str]:
    return {
        "schema_version": "1.4.2",
        "observation_id": observation_id,
        "signal_time": signal_time,
        "source_bar_time": "1785934800",
        "server_utc_offset_hours": "3",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "session": "LONDON_NY_OVERLAP",
        "signal_source": "trademind.ote_engine.build_ote_signals",
        "ote_signal_id": observation_id,
        "action": "BUY",
        "variant": "TOUCH_705",
        "score": "80",
        "entry_price": "1.1010",
        "bar_open": "1.1000",
        "bar_high": "1.1020",
        "bar_low": "1.0990",
        "anchor_price": "1.0990",
        "impulse_extreme": "1.1040",
        "impulse_atr": "1.6",
        "stop_price": "1.0988",
        "target_price": "1.1040",
        "atr": "0.001",
        "signal_reasons": "test",
        "tick_copy_status": "OK",
    }


def test_closed_volume_rows_converts_server_clock_to_utc() -> None:
    # 15:00 encoded in the broker's UTC+3 clock is 12:00 UTC; close is 12:05 UTC.
    server_epoch = int(datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc).timestamp())
    closed = closed_volume_rows(
        [_volume_row(server_epoch=server_epoch)],
        now=NOW,
        server_utc_offset_hours=3,
        close_grace_seconds=5,
    )
    assert len(closed) == 1

    still_open_epoch = int(datetime(2026, 8, 5, 15, 5, tzinfo=timezone.utc).timestamp())
    not_closed = closed_volume_rows(
        [_volume_row(server_epoch=still_open_epoch)],
        now=NOW,
        server_utc_offset_hours=3,
        close_grace_seconds=5,
    )
    assert not not_closed


def test_append_observations_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "observations.csv"
    row = _observation("EURUSD:M5:1", "2026-08-05T12:05:00+00:00")
    assert append_observations(path, [row]) == 1
    assert append_observations(path, [row]) == 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["observation_id"] == "EURUSD:M5:1"


def test_merge_evidence_outcomes_deduplicates_without_mutating_sources(tmp_path: Path) -> None:
    historical = tmp_path / "historical.jsonl"
    live = tmp_path / "live.jsonl"
    output = tmp_path / "merged.jsonl"
    first = {
        "signal_id": "HIST-1",
        "setup_key": "A",
        "completed_at": "2026-08-01T00:00:00+00:00",
        "outcome": "WIN",
        "net_r": 1.2,
    }
    second = {
        "signal_id": "LIVE-1",
        "setup_key": "A",
        "completed_at": "2026-08-05T12:00:00+00:00",
        "outcome": "LOSS",
        "net_r": -1.0,
    }
    historical.write_text(json.dumps(first) + "\n", encoding="utf-8")
    live.write_text(json.dumps(second) + "\n", encoding="utf-8")
    historical_before = historical.read_bytes()

    assert merge_evidence_outcomes(historical, live, output) == 2
    assert historical.read_bytes() == historical_before
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2

    live.write_text(json.dumps({**first, "net_r": 9.0}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting outcome"):
        merge_evidence_outcomes(historical, live, output)


def test_runtime_processes_watermark_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server_epoch = int(datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc).timestamp())
    volume_row = _volume_row(server_epoch=server_epoch)
    runtime_root = tmp_path / "runtime"
    canonical = tmp_path / "volume.csv"
    historical = tmp_path / "historical.jsonl"
    historical.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "trademind.live_signal_runtime.collect_volume_files",
        lambda *_args, **_kwargs: VolumeCollectSummary(
            source_files=1,
            rows_read=1,
            canonical_rows=1,
            duplicate_keys=0,
            invalid_rows=0,
            output_path=canonical,
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.load_volume_rows",
        lambda _path: ([volume_row], 1),
    )
    raw = _observation(f"EURUSD:M5:{server_epoch}", "2026-08-05T15:00:00+00:00")
    raw["source_bar_time"] = str(server_epoch)
    monkeypatch.setattr(
        "trademind.live_signal_runtime.build_fx_observations",
        lambda *_args, **_kwargs: [raw],
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.append_candidates",
        lambda *_args, **_kwargs: (1, ("SIGNAL-1",), 0),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.evaluate_live_outcomes",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.merge_evidence_outcomes",
        lambda *_args, **_kwargs: 43,
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_factory",
        lambda **_kwargs: FactoryRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT", "publishable": 0},
            evaluations=(),
            passport_paths=(),
        ),
    )
    monkeypatch.setattr(
        "trademind.live_signal_runtime.run_bridge",
        lambda **_kwargs: BridgeRun(
            status={"state": "WAITING_NO_PUBLISHABLE_PASSPORT"},
            package=None,
        ),
    )

    common = {
        "login": "77053345",
        "volume_source_dir": tmp_path / "source",
        "canonical_volume_path": canonical,
        "historical_outcomes_path": historical,
        "runtime_root": runtime_root,
        "account_csv": tmp_path / "account.csv",
        "positions_csv": tmp_path / "positions.csv",
        "symbols_csv": tmp_path / "symbols.csv",
        "risk_profile_path": tmp_path / "profile.json",
        "server_utc_offset_hours": 3,
        "now": NOW,
    }
    first = run_live_runtime(**common)
    assert first.state == RUN_COMPLETE
    assert first.status["new_observations"] == 1
    assert first.status["new_candidates"] == 1
    assert first.status["latest_closed_bar_at"] == "2026-08-05T12:05:00+00:00"

    second = run_live_runtime(**common)
    assert second.state == WAITING_NO_NEW_BARS
    assert second.status["new_observations"] == 0
    assert second.status["new_candidates"] == 0
