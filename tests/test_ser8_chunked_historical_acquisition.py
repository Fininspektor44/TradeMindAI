"""Deterministic proofs for the SER8 chunked/resumable MT5 amendment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

import pytest

from trademind.ser8_historical_data import (
    CHUNK_POLICY_VERSION,
    CHUNK_ACQUISITION_CODE_SHA256,
    READ_ONLY_MT5_OPERATIONS,
    BrokerSymbolV1,
    HistoricalBarV1,
    HistoricalDataError,
    MetaTrader5HistorySource,
    acquire_chunked_history,
    build_canonical_execution_universe,
    build_dataset_manifest,
    canonical_bars_csv,
    collector_code_sha256,
    merge_historical_bars,
    plan_calendar_month_chunks,
)
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
COLLECTOR_SHA = "sha256:" + "c" * 64
UTC = timezone.utc


def _proof(*, server: str = "RoboForex-ECN") -> dict[str, object]:
    return {
        "schema_version": "ser8-mt5-history-source-proof-v1",
        "source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": server,
        "market_data_account_company": "RoboForex",
        "authenticated_market_data_account_verified": True,
        "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
    }


def _bar(at: datetime, *, close: float = 1.1002) -> HistoricalBarV1:
    return HistoricalBarV1(
        time_utc=at,
        symbol="EURUSD",
        timeframe="M5",
        open=1.1000,
        high=max(1.1000, close) + 0.0002,
        low=min(1.1000, close) - 0.0002,
        close=close,
        tick_volume=100,
        spread=10,
        real_volume=0,
    )


class WindowSource:
    def __init__(self, responses: Mapping[str, object]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def source_proof(self) -> Mapping[str, object]:
        return _proof()

    def symbol_metadata(self, symbol: str) -> Mapping[str, object]:
        return {"name": symbol, "visible": True}

    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        requested_from_utc: datetime,
        requested_to_utc: datetime,
    ) -> Sequence[HistoricalBarV1]:
        chunk_id = f"{requested_from_utc:%Y%m%dT%H%M%SZ}__{requested_to_utc:%Y%m%dT%H%M%SZ}"
        self.calls.append(chunk_id)
        response = self.responses.get(chunk_id, ())
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def _acquire(
    tmp_path: Path,
    source: WindowSource,
    *,
    start: datetime,
    end: datetime,
    proof: Mapping[str, object] | None = None,
    collector_sha256: str = COLLECTOR_SHA,
):
    return acquire_chunked_history(
        source=source,
        source_proof=proof or _proof(),
        symbol="EURUSD",
        timeframe="M5",
        requested_from_utc=start,
        requested_to_utc=end,
        staging_root=tmp_path / "staging",
        collector_code_sha256=collector_sha256,
    )


def _broker() -> BrokerSymbolV1:
    row = _execution_row()
    return BrokerSymbolV1(
        symbol="EURUSD",
        trade_mode="FULL",
        source_row=row,
        asset_class="FX",
        risk_model_supported=True,
        risk_model_reason="",
    )


def _execution_row() -> dict[str, str]:
    return {
        "account_login": ACCOUNT,
        "currency": "USD",
        "symbol": "EURUSD",
        "trade_mode": "FULL",
        "tick_size": "0.00001",
        "tick_value": "1",
        "tick_value_profit": "1",
        "tick_value_loss": "1",
        "volume_min": "0.01",
        "volume_max": "100",
        "volume_step": "0.01",
        "contract_size": "100000",
        "margin_initial": "0",
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1",
        "leverage": "100",
    }


def _manifest(
    bars: Sequence[HistoricalBarV1],
    *,
    start: datetime,
    end: datetime,
    acquisition: Mapping[str, object] | None = None,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    universe = build_canonical_execution_universe(
        [_execution_row()],
        account_login=ACCOUNT,
        raw_sha256=sha256_bytes(b"universe raw"),
    )
    manifest, _ = build_dataset_manifest(
        bars=bars,
        source_proof=_proof(),
        symbol_metadata={
            "name": "EURUSD",
            "point": 0.00001,
            "digits": 5,
            "visible": True,
            "trade_tick_size": 0.00001,
        },
        broker_symbol=_broker(),
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe=universe,
        timeframe="M5",
        requested_from_utc=start,
        requested_to_utc=end,
        expected_interval_seconds=300,
        source_capture_utc=captured_at or end,
        collector_code_sha256=COLLECTOR_SHA,
        chunk_acquisition=acquisition,
    )
    return manifest


def _rewrite_cache_manifest_field(staging: Path, key: str, value: object) -> None:
    manifest_path = next(staging.rglob("manifest.json"))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[key] = value
    payload.pop("manifest_sha256")
    payload["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(payload) + b"\n")


def test_multi_year_chunk_plan_is_deterministic_utc_and_reaches_final_partial_chunk() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2026, 8, 21, tzinfo=UTC)
    first = plan_calendar_month_chunks(start, end)
    second = plan_calendar_month_chunks(start, end)
    assert first == second
    assert len(first) == 32
    assert first[0].chunk_from_utc == start
    assert first[0].chunk_to_utc == datetime(2024, 2, 1, tzinfo=UTC)
    assert first[-1].chunk_from_utc == datetime(2026, 8, 1, tzinfo=UTC)
    assert first[-1].chunk_to_utc == end
    assert all(left.chunk_to_utc == right.chunk_from_utc for left, right in zip(first, first[1:]))
    assert all(chunk.chunk_from_utc.utcoffset() == timedelta(0) for chunk in first)


def test_chunk_planner_normalizes_offset_input_to_utc_month_boundaries() -> None:
    plus_three = timezone(timedelta(hours=3))
    chunks = plan_calendar_month_chunks(
        datetime(2024, 1, 15, 3, tzinfo=plus_three),
        datetime(2024, 3, 10, 3, tzinfo=plus_three),
    )
    assert [(item.chunk_from_utc, item.chunk_to_utc) for item in chunks] == [
        (datetime(2024, 1, 15, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)),
        (datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
        (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 3, 10, tzinfo=UTC)),
    ]


def test_empty_chunk_does_not_hide_later_data_or_fabricate_bars(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    later = _bar(datetime(2024, 2, 5, 12, tzinfo=UTC))
    source = WindowSource(
        {
            "20240101T000000Z__20240201T000000Z": (),
            "20240201T000000Z__20240301T000000Z": (later,),
        }
    )
    result = _acquire(tmp_path, source, start=start, end=end)
    assert result.capture_complete is True
    assert result.empty_chunk_count == 1
    assert result.failed_chunk_count == 0
    assert result.bars == (later,)
    assert result.requested_chunk_count == result.completed_chunk_count == 2


def test_mt5_api_failure_is_distinct_from_successful_empty_response() -> None:
    class FakeMT5:
        TIMEFRAME_M5 = 5
        SYMBOL_TRADE_MODE_FULL = 4

        def __init__(self, rates: object) -> None:
            self.rates = rates

        def initialize(self, *_: object) -> bool:
            return True

        def terminal_info(self) -> SimpleNamespace:
            return SimpleNamespace(connected=True, company="RoboForex")

        def account_info(self) -> SimpleNamespace:
            return SimpleNamespace(
                login=int(MARKET_DATA_ACCOUNT),
                server="RoboForex-ECN",
                company="RoboForex",
            )

        def version(self) -> tuple[int, int, str]:
            return (5, 5000, "test")

        def symbol_info(self, symbol: str) -> SimpleNamespace:
            return SimpleNamespace(
                name=symbol,
                visible=True,
                select=True,
                trade_mode=4,
                digits=5,
                point=0.00001,
                trade_tick_size=0.00001,
            )

        def copy_rates_range(self, *_: object) -> object:
            return self.rates

        def last_error(self) -> tuple[int, str]:
            return (-1, "proven API failure")

        def shutdown(self) -> None:
            pass

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    empty_source = MetaTrader5HistorySource(
        market_data_account_login=MARKET_DATA_ACCOUNT,
        module=FakeMT5([]),
    )
    assert empty_source.copy_rates("EURUSD", "M5", start, end) == ()
    failed_source = MetaTrader5HistorySource(
        market_data_account_login=MARKET_DATA_ACCOUNT,
        module=FakeMT5(None),
    )
    with pytest.raises(HistoricalDataError) as caught:
        failed_source.copy_rates("EURUSD", "M5", start, end)
    assert caught.value.code == "MT5_COPY_RATES_FAILED"
    assert "proven API failure" in str(caught.value)


def test_identical_boundary_duplicate_is_deduplicated_and_sorted(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    before = _bar(datetime(2024, 1, 31, 23, 55, tzinfo=UTC))
    boundary = _bar(datetime(2024, 2, 1, tzinfo=UTC))
    after = _bar(datetime(2024, 2, 1, 0, 5, tzinfo=UTC))
    source = WindowSource(
        {
            "20240101T000000Z__20240201T000000Z": (boundary, before),
            "20240201T000000Z__20240301T000000Z": (after, boundary),
        }
    )
    result = _acquire(tmp_path, source, start=start, end=end)
    assert result.capture_complete is True
    assert result.bars == (before, boundary, after)


def test_conflicting_boundary_duplicate_fails_closed(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    boundary = _bar(datetime(2024, 2, 1, tzinfo=UTC))
    source = WindowSource(
        {
            "20240101T000000Z__20240201T000000Z": (boundary,),
            "20240201T000000Z__20240301T000000Z": (replace(boundary, close=1.1003),),
        }
    )
    result = _acquire(tmp_path, source, start=start, end=end)
    assert result.capture_complete is False
    assert result.bars == ()
    assert result.merge_integrity_error_code == "CONFLICTING_DUPLICATE_BAR"


def test_valid_chunk_cache_is_reused_without_source_call(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    first_source = WindowSource({"20240101T000000Z__20240201T000000Z": (_bar(start),)})
    first = _acquire(tmp_path, first_source, start=start, end=end)
    assert first.acquired_chunk_count == 1
    second_source = WindowSource(
        {"20240101T000000Z__20240201T000000Z": AssertionError("source must not run")}
    )
    second = _acquire(tmp_path, second_source, start=start, end=end)
    assert second.capture_complete is True
    assert second.cached_chunk_count == 1
    assert second.acquired_chunk_count == 0
    assert second_source.calls == []


def test_deployed_b7_chunk_cache_survives_final_layer_only_amendment(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    chunk_id = "20240101T000000Z__20240201T000000Z"
    bar = _bar(start)
    assert collector_code_sha256() != CHUNK_ACQUISITION_CODE_SHA256
    first = _acquire(
        tmp_path,
        WindowSource({chunk_id: (bar,)}),
        start=start,
        end=end,
        collector_sha256=CHUNK_ACQUISITION_CODE_SHA256,
    )
    assert first.acquired_chunk_count == 1
    source = WindowSource({chunk_id: AssertionError("deployed cache must be reused")})
    second = _acquire(
        tmp_path,
        source,
        start=start,
        end=end,
        collector_sha256=CHUNK_ACQUISITION_CODE_SHA256,
    )
    assert second.cached_chunk_count == 1
    assert second.acquired_chunk_count == 0
    assert source.calls == []


def test_tampered_chunk_bars_are_rejected_and_reacquired(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    chunk_id = "20240101T000000Z__20240201T000000Z"
    bar = _bar(start)
    _acquire(tmp_path, WindowSource({chunk_id: (bar,)}), start=start, end=end)
    bars_path = next((tmp_path / "staging").rglob("bars.csv"))
    bars_path.write_bytes(bars_path.read_bytes().replace(b"1.1002", b"1.1003", 1))
    source = WindowSource({chunk_id: (bar,)})
    result = _acquire(tmp_path, source, start=start, end=end)
    assert source.calls == [chunk_id]
    assert result.acquired_chunk_count == 1
    assert result.chunk_audit[0]["cache_validation"] == "REJECTED"
    assert result.chunk_audit[0]["cache_error_code"] == "CHUNK_CACHE_BARS_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("symbol", "GBPUSD"),
        ("market_data_account_login", "99999999"),
        ("market_data_account_server", "Wrong-Server"),
    ],
)
def test_wrong_identity_chunk_cache_is_not_reused(
    tmp_path: Path,
    field: str,
    wrong_value: str,
) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    chunk_id = "20240101T000000Z__20240201T000000Z"
    bar = _bar(start)
    _acquire(tmp_path, WindowSource({chunk_id: (bar,)}), start=start, end=end)
    _rewrite_cache_manifest_field(tmp_path / "staging", field, wrong_value)
    source = WindowSource({chunk_id: (bar,)})
    result = _acquire(tmp_path, source, start=start, end=end)
    assert source.calls == [chunk_id]
    assert result.chunk_audit[0]["cache_validation"] == "REJECTED"
    assert result.chunk_audit[0]["cache_error_code"] == "CHUNK_CACHE_IDENTITY_MISMATCH"


def test_interrupted_collection_resumes_from_completed_chunk(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]
    interrupted = WindowSource(
        {
            ids[0]: (_bar(datetime(2024, 1, 5, tzinfo=UTC)),),
            ids[1]: KeyboardInterrupt("simulated interruption"),
        }
    )
    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        _acquire(tmp_path, interrupted, start=start, end=end)
    resumed = WindowSource(
        {
            ids[1]: (_bar(datetime(2024, 2, 5, tzinfo=UTC)),),
            ids[2]: (_bar(datetime(2024, 3, 5, tzinfo=UTC)),),
        }
    )
    result = _acquire(tmp_path, resumed, start=start, end=end)
    assert resumed.calls == ids[1:]
    assert result.cached_chunk_count == 1
    assert result.acquired_chunk_count == 2
    assert result.capture_complete is True
    assert len(result.bars) == 3


def test_failed_chunk_continues_later_chunks_but_never_exposes_partial_bars(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]
    source = WindowSource(
        {
            ids[0]: (_bar(datetime(2024, 1, 5, tzinfo=UTC)),),
            ids[1]: HistoricalDataError("MT5_API_FAILED", "simulated MT5 failure"),
            ids[2]: (_bar(datetime(2024, 3, 5, tzinfo=UTC)),),
        }
    )
    result = _acquire(tmp_path, source, start=start, end=end)
    assert source.calls == ids
    assert result.completed_chunk_count == 2
    assert result.failed_chunk_count == 1
    assert result.capture_complete is False
    assert result.bars == ()
    assert result.chunk_audit[2]["status"] == "COMPLETED"


def test_partial_acquisition_manifest_cannot_be_accepted() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    bar = _bar(datetime(2024, 1, 5, tzinfo=UTC))
    chunks = plan_calendar_month_chunks(start, end)
    summary = {
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "requested_chunk_count": 2,
        "completed_chunk_count": 1,
        "empty_chunk_count": 0,
        "failed_chunk_count": 1,
        "cached_chunk_count": 0,
        "acquired_chunk_count": 1,
        "historical_capture_complete": False,
        "chunk_audit": [
            {
                "chunk_id": chunks[0].chunk_id,
                "chunk_from_utc": "2024-01-01T00:00:00Z",
                "chunk_to_utc": "2024-02-01T00:00:00Z",
                "status": "COMPLETED",
                "row_count": 1,
                "bars_sha256": sha256_bytes(canonical_bars_csv((bar,))),
            },
            {
                "chunk_id": chunks[1].chunk_id,
                "chunk_from_utc": "2024-02-01T00:00:00Z",
                "chunk_to_utc": "2024-03-01T00:00:00Z",
                "status": "FAILED",
                "row_count": 0,
                "bars_sha256": None,
            },
        ],
    }
    manifest = _manifest((bar,), start=start, end=end, acquisition=summary)
    assert manifest["accepted_historical_data"] is False
    assert manifest["historical_capture_complete"] is False
    assert manifest["failed_chunk_count"] == 1


def test_download_order_cache_method_retry_and_capture_time_do_not_change_dataset_hash() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    bars = (
        _bar(datetime(2024, 1, 1, 0, 5, tzinfo=UTC)),
        _bar(datetime(2024, 1, 1, tzinfo=UTC)),
    )
    canonical = merge_historical_bars(bars, symbol="EURUSD", timeframe="M5")
    first = _manifest(canonical, start=start, end=end, captured_at=end)
    changed_audit = json.loads(json.dumps(first["chunk_audit"]))
    changed_audit[0]["acquisition_method"] = "CACHE"
    changed_audit[0]["retry_count"] = 9
    summary = {
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "requested_chunk_count": 1,
        "completed_chunk_count": 1,
        "empty_chunk_count": 0,
        "failed_chunk_count": 0,
        "cached_chunk_count": 1,
        "acquired_chunk_count": 0,
        "chunk_audit": changed_audit,
        "historical_capture_complete": True,
    }
    reversed_canonical = merge_historical_bars(
        tuple(reversed(canonical)), symbol="EURUSD", timeframe="M5"
    )
    second = _manifest(
        reversed_canonical,
        start=start,
        end=end,
        acquisition=summary,
        captured_at=end + timedelta(days=1),
    )
    assert canonical_bars_csv(canonical) == canonical_bars_csv(reversed_canonical)
    assert first["dataset_sha256"] == second["dataset_sha256"]


def test_manifest_records_chunk_audit_and_observable_coverage(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    bar = _bar(datetime(2024, 2, 5, tzinfo=UTC))
    result = _acquire(
        tmp_path,
        WindowSource(
            {
                "20240101T000000Z__20240201T000000Z": (),
                "20240201T000000Z__20240301T000000Z": (bar,),
            }
        ),
        start=start,
        end=end,
    )
    manifest = _manifest(result.bars, start=start, end=end, acquisition=result.manifest_summary())
    assert manifest["requested_chunk_count"] == 2
    assert manifest["completed_chunk_count"] == 2
    assert manifest["empty_chunk_count"] == 1
    assert manifest["failed_chunk_count"] == 0
    assert len(manifest["chunk_audit"]) == 2
    assert manifest["requested_duration_seconds"] == 60 * 24 * 60 * 60
    assert manifest["observed_span_seconds"] == 0
    assert manifest["first_bar_offset_seconds"] == 35 * 24 * 60 * 60
    assert manifest["last_bar_offset_seconds"] == 25 * 24 * 60 * 60
    assert manifest["gap_repair_performed"] is False
    assert manifest["synthetic_bars_added"] == 0
