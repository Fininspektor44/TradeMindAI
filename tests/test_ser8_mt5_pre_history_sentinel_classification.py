"""SER8 MT5 PRE-HISTORY SENTINEL CLASSIFICATION V1 — proofs.

Real Windows evidence (authenticated read-only market-data account 77053345,
RoboForex/MT5) disproved the prior speculative "inclusive right endpoint"
hypothesis (5472531, reverted): the actually observed shape for BTCUSD,
ETHUSD, and EURAUD requesting a chunk wholly before the broker's retained
history is:

- copy_rates_range returns successfully (mt5.last_error() == (1, "Success"));
- the response contains EXACTLY ONE bar;
- zero bars are inside the requested logical interval;
- zero bars are before requested_from_utc;
- the single returned bar is STRICTLY AFTER requested_to_utc (by hours, in
  BTCUSD/ETHUSD's case, and by over two weeks in EURAUD's case — proving the
  classification is not a small fixed offset).

This is a narrow, positively-observed signal distinct from RES_E_NOT_FOUND
(the previously-implemented genuine-unavailable signal). Both feed the SAME
existing GENUINE_HISTORICAL_UNAVAILABLE classification/coverage-boundary
mechanism; neither weakens _validate_chunk_bars, and neither generalizes to
"any future/out-of-range bar means no history".

SOURCE-EVIDENCE AMENDMENT: the classification is authorized ONLY inside
MetaTrader5HistorySource.copy_rates, gated on a positively-verified
mt5.last_error() success result captured immediately after the SAME
copy_rates_range call. The generic acquisition boundary
(_attempt_chunk_acquisition) never inspects returned-bar shape for this
purpose, so a fake/non-MT5 HistoricalRateSource cannot trigger this
classification merely by returning a single future bar as ordinary data —
only by explicitly raising BROKER_HISTORY_NOT_RETAINED_FOR_RANGE itself.

No live MT5 calls, no network data acquisition, no broker mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

import pytest

from trademind.ser8_historical_data import (
    DATASET_SCHEMA_VERSION,
    GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES,
    HISTORICAL_UNAVAILABILITY_EVIDENCE_RES_E_NOT_FOUND,
    HISTORICAL_UNAVAILABILITY_EVIDENCE_SUCCESS_SINGLE_BAR_STRICTLY_AFTER_REQUEST,
    HistoricalBarV1,
    HistoricalDataError,
    MetaTrader5HistorySource,
    build_canonical_execution_universe,
    build_dataset_manifest,
    discover_available_coverage,
    plan_calendar_month_chunks,
    publish_dataset,
    verify_dataset,
    BrokerSymbolV1,
)
from trademind.signal_statistics_provenance import sha256_bytes

UTC = timezone.utc
COLLECTOR_SHA = "sha256:" + "c" * 64
ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"


def _proof() -> dict[str, object]:
    return {
        "schema_version": "ser8-mt5-history-source-proof-v1",
        "source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": "RoboForex-ECN",
        "market_data_account_company": "RoboForex",
        "authenticated_market_data_account_verified": True,
        "read_only_operations": (
            "initialize", "terminal_info", "account_info", "version",
            "symbol_info", "copy_rates_range",
        ),
    }


def _bar(at: datetime, *, symbol: str = "BTCUSD", close: float = 42_001.5) -> HistoricalBarV1:
    return HistoricalBarV1(
        time_utc=at,
        symbol=symbol,
        timeframe="M5",
        open=42_000.0,
        high=max(42_000.0, close) + 2.0,
        low=min(42_000.0, close) - 2.0,
        close=close,
        tick_volume=50,
        spread=20,
        real_volume=0,
    )


class WindowSource:
    """Minimal fake HistoricalRateSource keyed by exact requested chunk id."""

    def __init__(self, responses: Mapping[str, object], *, symbol: str = "BTCUSD") -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []
        self.symbol = symbol

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


def _discover(tmp_path: Path, source: WindowSource, *, start: datetime, end: datetime, symbol: str = "BTCUSD"):
    return discover_available_coverage(
        source=source,
        source_proof=_proof(),
        symbol=symbol,
        timeframe="M5",
        requested_from_utc=start,
        requested_to_utc=end,
        staging_root=tmp_path / "staging",
        collector_code_sha256=COLLECTOR_SHA,
    )


class FakeMT5:
    """Fake MetaTrader5 module: models real copy_rates_range + last_error()
    so the SOURCE-EVIDENCE amendment (mandatory, positively-verified success
    captured from the SAME call) can be exercised through the actual
    MetaTrader5HistorySource adapter — never through the generic fake."""

    TIMEFRAME_M5 = 5
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self, rates: object, *, last_error: tuple[int, str] = (1, "Success")) -> None:
        self.rates = rates
        self._last_error_value = last_error

    def initialize(self, *_: object) -> bool:
        return True

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=True, company="RoboForex")

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            login=int(MARKET_DATA_ACCOUNT), server="RoboForex-ECN", company="RoboForex",
        )

    def version(self) -> tuple[int, int, str]:
        return (5, 5000, "test")

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=symbol, visible=True, select=True, trade_mode=4,
            digits=2, point=0.01, trade_tick_size=0.01,
        )

    def copy_rates_range(self, *_: object) -> object:
        return self.rates

    def last_error(self) -> tuple[int, str]:
        return self._last_error_value

    def shutdown(self) -> None:
        pass


def _mt5_source(rates: object, *, last_error: tuple[int, str] = (1, "Success")) -> MetaTrader5HistorySource:
    return MetaTrader5HistorySource(
        market_data_account_login=MARKET_DATA_ACCOUNT,
        module=FakeMT5(rates, last_error=last_error),
    )


def _rate_row(at: datetime, *, close: float = 42_001.5) -> dict[str, object]:
    return {
        "time": int(at.timestamp()),
        "open": 42_000.0,
        "high": max(42_000.0, close) + 2.0,
        "low": min(42_000.0, close) - 2.0,
        "close": close,
        "tick_volume": 50,
        "spread": 20,
        "real_volume": 0,
    }


# ---------------------------------------------------------------------------
# A. Real sentinel shape — BTCUSD style (through the REAL MT5 adapter)
# ---------------------------------------------------------------------------


def test_A_btcusd_style_sentinel_is_positively_classified_as_genuine_unavailable() -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    sentinel_time = datetime(2026, 3, 1, 9, 5, tzinfo=UTC)  # real evidence: rows=1, GT_END=1
    source = _mt5_source([_rate_row(sentinel_time)], last_error=(1, "Success"))
    with pytest.raises(HistoricalDataError) as caught:
        source.copy_rates("BTCUSD", "M5", start, end)
    assert caught.value.code == "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE"
    assert caught.value.code in GENUINE_HISTORICAL_UNAVAILABLE_ERROR_CODES
    assert caught.value.details["historical_unavailability_evidence_type"] == (
        HISTORICAL_UNAVAILABILITY_EVIDENCE_SUCCESS_SINGLE_BAR_STRICTLY_AFTER_REQUEST
    )
    assert caught.value.details["sentinel_bar_time_utc"] == "2026-03-01T09:05:00Z"
    assert caught.value.details["requested_from_utc"] == "2026-02-01T00:00:00Z"
    assert caught.value.details["requested_to_utc"] == "2026-03-01T00:00:00Z"


# ---------------------------------------------------------------------------
# B. Real sentinel shape — EURAUD style (proves it is not a small fixed offset)
# ---------------------------------------------------------------------------


def test_B_euraud_style_sentinel_over_two_weeks_later_is_also_classified() -> None:
    start = datetime(2025, 11, 1, tzinfo=UTC)
    end = datetime(2025, 12, 1, tzinfo=UTC)
    sentinel_time = datetime(2025, 12, 17, 9, 25, tzinfo=UTC)  # 16+ days later
    source = _mt5_source([_rate_row(sentinel_time)], last_error=(1, "Success"))
    with pytest.raises(HistoricalDataError) as caught:
        source.copy_rates("EURAUD", "M5", start, end)
    assert caught.value.code == "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE"
    assert caught.value.details["sentinel_bar_time_utc"] == "2025-12-17T09:25:00Z"


# ---------------------------------------------------------------------------
# Source-evidence amendment: MT5-success is mandatory and cannot be inferred
# generically by the acquisition boundary.
# ---------------------------------------------------------------------------


def test_ethusd_style_sentinel_is_also_classified_through_the_real_adapter() -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    sentinel_time = datetime(2026, 3, 1, 11, 45, tzinfo=UTC)  # real ETHUSD evidence
    source = _mt5_source([_rate_row(sentinel_time)], last_error=(1, "Success"))
    with pytest.raises(HistoricalDataError) as caught:
        source.copy_rates("ETHUSD", "M5", start, end)
    assert caught.value.code == "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE"
    assert caught.value.details["sentinel_bar_time_utc"] == "2026-03-01T11:45:00Z"


def test_mt5_non_success_last_error_with_single_future_bar_does_not_classify_as_boundary() -> None:
    """The exact same single-future-bar shape, but last_error() does NOT
    report success: mandatory verification fails, so the bar is returned as
    ordinary data (letting the generic validator fail it closed downstream),
    never silently promoted to a genuine-unavailable boundary."""
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    sentinel_time = datetime(2026, 3, 1, 9, 5, tzinfo=UTC)
    source = _mt5_source([_rate_row(sentinel_time)], last_error=(-1, "not actually success"))
    bars = source.copy_rates("BTCUSD", "M5", start, end)
    assert len(bars) == 1
    assert bars[0].time_utc == sentinel_time


def test_generic_fake_source_cannot_infer_mt5_sentinel_from_bar_shape(tmp_path: Path) -> None:
    """A non-MT5/generic HistoricalRateSource that returns exactly one
    strictly-future bar as plain DATA (never raising the code itself) must
    NOT be promoted to BROKER_HISTORY_NOT_RETAINED_FOR_RANGE by the generic
    acquisition boundary: it fails closed as an ordinary data-integrity
    violation instead, exactly like any other out-of-range bar."""
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    shape_matching_bar = _bar(datetime(2026, 3, 1, 9, 5, tzinfo=UTC))
    source = WindowSource({"20260201T000000Z__20260301T000000Z": (shape_matching_bar,)})
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "DATA_INTEGRITY_FAILED"
    assert result.integrity_error_code == "CHUNK_BAR_OUTSIDE_REQUEST"
    assert result.coverage_truncated_at_requested_start is False


def test_generic_fake_source_can_still_supply_the_explicit_evidence_contract(
    tmp_path: Path,
) -> None:
    """A source (real MT5 adapter or otherwise) that has ALREADY produced the
    positive classification and explicitly raises
    BROKER_HISTORY_NOT_RETAINED_FOR_RANGE itself is trusted at face value by
    the generic acquisition/coverage-discovery layer — the amendment governs
    who may INFER the signal from bar shape, not who may report it."""
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    source = WindowSource(
        {
            "20260201T000000Z__20260301T000000Z": HistoricalDataError(
                "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE",
                "source-declared genuine unavailability",
                details={
                    "historical_unavailability_evidence_type": (
                        HISTORICAL_UNAVAILABILITY_EVIDENCE_SUCCESS_SINGLE_BAR_STRICTLY_AFTER_REQUEST
                    ),
                },
            )
        }
    )
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "TRUNCATED_GENUINE_BOUNDARY"


# ---------------------------------------------------------------------------
# C. In-range bar plus a future bar => data-integrity failure, never a boundary
# ---------------------------------------------------------------------------


def test_C_in_range_plus_future_bar_fails_closed_as_integrity_not_boundary(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    in_range = _bar(datetime(2026, 2, 15, tzinfo=UTC))
    future = _bar(datetime(2026, 3, 1, 9, 5, tzinfo=UTC))
    source = WindowSource({"20260201T000000Z__20260301T000000Z": (in_range, future)})
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "DATA_INTEGRITY_FAILED"
    assert result.integrity_error_code == "CHUNK_BAR_OUTSIDE_REQUEST"
    assert result.coverage_truncated_at_requested_start is False


# ---------------------------------------------------------------------------
# D. Multiple future bars, zero in-range => fail closed, never auto-genuine
# ---------------------------------------------------------------------------


def test_D_multiple_future_bars_do_not_auto_classify_as_boundary(tmp_path: Path) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    future_1 = _bar(datetime(2026, 3, 1, 9, 5, tzinfo=UTC))
    future_2 = _bar(datetime(2026, 3, 1, 9, 10, tzinfo=UTC))
    source = WindowSource({"20260201T000000Z__20260301T000000Z": (future_1, future_2)})
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution != "TRUNCATED_GENUINE_BOUNDARY"
    assert result.coverage_truncated_at_requested_start is False
    # Fails closed (as data-integrity, since both bars are strictly out of
    # range) rather than being silently treated as an unavailable prefix.
    assert result.resolution == "DATA_INTEGRITY_FAILED"
    assert result.integrity_error_code == "CHUNK_BAR_OUTSIDE_REQUEST"


# ---------------------------------------------------------------------------
# E. Left-outside bar => integrity failure
# ---------------------------------------------------------------------------


def test_E_single_bar_before_requested_from_fails_closed_as_integrity(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    before = _bar(datetime(2026, 1, 15, tzinfo=UTC))
    source = WindowSource({"20260201T000000Z__20260301T000000Z": (before,)})
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "DATA_INTEGRITY_FAILED"
    assert result.integrity_error_code == "CHUNK_BAR_OUTSIDE_REQUEST"
    assert result.coverage_truncated_at_requested_start is False


def test_E2_wrong_symbol_single_future_bar_does_not_masquerade_as_sentinel(
    tmp_path: Path,
) -> None:
    """A misaligned identity is beyond the narrow sentinel shape and must
    still fail closed, not be silently accepted as genuine-unavailable."""
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    wrong_symbol = _bar(datetime(2026, 3, 1, 9, 5, tzinfo=UTC), symbol="ETHUSD")
    source = WindowSource({"20260201T000000Z__20260301T000000Z": (wrong_symbol,)}, symbol="BTCUSD")
    result = _discover(tmp_path, source, start=start, end=end, symbol="BTCUSD")
    assert result.resolution == "DATA_INTEGRITY_FAILED"
    assert result.integrity_error_code == "BAR_IDENTITY_MISMATCH"


# ---------------------------------------------------------------------------
# F. Transient MT5 error — unchanged bounded retry/unresolved behavior
# ---------------------------------------------------------------------------


def test_F_transient_failure_behavior_unchanged(tmp_path: Path) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    source = WindowSource(
        {
            "20260201T000000Z__20260301T000000Z": HistoricalDataError(
                "MT5_COPY_RATES_FAILED", "temporary read failure"
            )
        }
    )
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "UNRESOLVED_TRANSIENT_FAILURE"
    assert result.coverage_truncated_at_requested_start is False
    # Bounded retry: the same chunk is attempted twice before giving up.
    assert source.calls == ["20260201T000000Z__20260301T000000Z"] * 2


# ---------------------------------------------------------------------------
# G. RES_E_NOT_FOUND — unchanged genuine-unavailable behavior
# ---------------------------------------------------------------------------


def test_G_res_e_not_found_behavior_unchanged(tmp_path: Path) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    source = WindowSource(
        {
            "20260201T000000Z__20260301T000000Z": HistoricalDataError(
                "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE",
                "MT5 positively reports no retained history for this window",
                details={
                    "historical_unavailability_evidence_type": (
                        HISTORICAL_UNAVAILABILITY_EVIDENCE_RES_E_NOT_FOUND
                    ),
                    "requested_from_utc": "2026-02-01T00:00:00Z",
                    "requested_to_utc": "2026-03-01T00:00:00Z",
                },
            )
        }
    )
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "TRUNCATED_GENUINE_BOUNDARY"
    boundary = result.unavailable_prefix_chunk_audit[-1]
    assert boundary["historical_unavailability_evidence_type"] == (
        HISTORICAL_UNAVAILABILITY_EVIDENCE_RES_E_NOT_FOUND
    )


# ---------------------------------------------------------------------------
# H. Backward coverage discovery across the sentinel boundary
# ---------------------------------------------------------------------------


def test_H_backward_discovery_accepts_recent_suffix_and_skips_older_chunks(
    tmp_path: Path,
) -> None:
    start = datetime(2025, 12, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]
    # ids[0]=Dec25, ids[1]=Jan26, ids[2]=Feb26, ids[3]=Mar26
    source = WindowSource(
        {
            ids[0]: AssertionError("older-than-boundary chunk must never be probed"),
            # Stands in for a real MetaTrader5HistorySource that has ALREADY
            # produced this classification internally (see test_A/test_B for
            # that mechanism proven end-to-end through the real adapter);
            # discover_available_coverage only ever classifies whatever code
            # a source explicitly raises, never bar shape itself.
            ids[1]: HistoricalDataError(
                "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE",
                "source-declared genuine unavailability",
                details={
                    "historical_unavailability_evidence_type": (
                        HISTORICAL_UNAVAILABILITY_EVIDENCE_SUCCESS_SINGLE_BAR_STRICTLY_AFTER_REQUEST
                    ),
                    "sentinel_bar_time_utc": "2026-02-03T00:00:00Z",
                },
            ),
            ids[2]: (_bar(datetime(2026, 2, 15, tzinfo=UTC)),),
            ids[3]: (_bar(datetime(2026, 3, 15, tzinfo=UTC)),),
        }
    )
    result = _discover(tmp_path, source, start=start, end=end)
    assert source.calls == [ids[3], ids[2], ids[1]]
    assert result.resolution == "TRUNCATED_GENUINE_BOUNDARY"
    assert result.accepted_chunk_count == 2
    assert result.effective_from_utc == datetime(2026, 2, 1, tzinfo=UTC)
    boundary = result.unavailable_prefix_chunk_audit[-1]
    assert boundary["chunk_id"] == ids[1]
    assert boundary["error_code"] == "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE"
    assert result.unavailable_prefix_chunk_audit[0]["status"] == "SKIPPED_UNAVAILABLE_PREFIX"


# ---------------------------------------------------------------------------
# I. Internal hole (transient/integrity between valid chunks) still fails closed
# ---------------------------------------------------------------------------


def test_I_internal_hole_still_fails_closed_no_truncation(tmp_path: Path) -> None:
    start = datetime(2025, 12, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]
    source = WindowSource(
        {
            ids[0]: AssertionError("chunk beyond an unresolved failure must never be probed"),
            ids[1]: HistoricalDataError("MT5_COPY_RATES_FAILED", "transient read failure"),
            ids[2]: (_bar(datetime(2026, 2, 5, tzinfo=UTC)),),
            ids[3]: (_bar(datetime(2026, 3, 5, tzinfo=UTC)),),
        }
    )
    result = _discover(tmp_path, source, start=start, end=end)
    assert result.resolution == "UNRESOLVED_TRANSIENT_FAILURE"
    assert result.coverage_truncated_at_requested_start is False
    assert result.accepted_chunk_count == 0
    assert len(result.discarded_chunk_audit) == 2


# ---------------------------------------------------------------------------
# J. AAPL full-history behavior unchanged
# ---------------------------------------------------------------------------


def test_J_aapl_style_full_history_unchanged(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    source = WindowSource(
        {
            "20240101T000000Z__20240201T000000Z": (
                _bar(datetime(2024, 1, 2, 14, 30, tzinfo=UTC), symbol="AAPL"),
            ),
            "20240201T000000Z__20240301T000000Z": (
                _bar(datetime(2024, 2, 1, 14, 30, tzinfo=UTC), symbol="AAPL"),
            ),
        },
        symbol="AAPL",
    )
    result = _discover(tmp_path, source, start=start, end=end, symbol="AAPL")
    assert result.resolution == "COMPLETE"
    assert result.coverage_truncated_at_requested_start is False
    assert result.accepted_chunk_count == 2


# ---------------------------------------------------------------------------
# K. Existing cache remains reusable
# ---------------------------------------------------------------------------


def test_K_accepted_suffix_cache_is_reused_boundary_chunk_reattempted(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]

    def _make_source(*, allow_recent: bool) -> WindowSource:
        return WindowSource(
            {
                ids[0]: HistoricalDataError(
                    "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE", "no history that far back"
                ),
                ids[1]: (_bar(datetime(2026, 2, 5, tzinfo=UTC)),)
                if allow_recent
                else AssertionError("accepted chunk must be served from cache on rerun"),
                ids[2]: (_bar(datetime(2026, 3, 5, tzinfo=UTC)),)
                if allow_recent
                else AssertionError("accepted chunk must be served from cache on rerun"),
            }
        )

    first = _discover(tmp_path, _make_source(allow_recent=True), start=start, end=end)
    assert first.accepted_chunk_count == 2

    second_source = _make_source(allow_recent=False)
    second = _discover(tmp_path, second_source, start=start, end=end)
    # Accepted suffix served entirely from cache; only the never-cached
    # (failed) boundary chunk is re-attempted.
    assert second_source.calls == [ids[0]]
    assert second.accepted_chunk_count == 2
    assert second.bars == first.bars


# ---------------------------------------------------------------------------
# M. Dataset identity: this fix does not change accepted dataset semantics
# ---------------------------------------------------------------------------


def _execution_row() -> dict[str, str]:
    return {
        "account_login": ACCOUNT,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "symbol": "BTCUSD",
        "digits": "2",
        "trade_mode": "FULL",
        "tick_size": "0.01",
        "tick_value": "1",
        "tick_value_profit": "1",
        "tick_value_loss": "1",
        "volume_min": "0.01",
        "volume_max": "100",
        "volume_step": "0.01",
        "contract_size": "1",
        "margin_initial": "0",
        "margin_maintenance": "0",
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1",
        "leverage": "100",
        "expiration_mode_flags": "15",
    }


def test_M_dataset_schema_and_identity_unchanged_from_ca70eaa(tmp_path: Path) -> None:
    """This fix reuses the existing GENUINE_HISTORICAL_UNAVAILABLE / coverage-
    truncation mechanism verbatim; the new evidence-type distinction is
    audit-only (per chunk, in unavailable_prefix_chunk_audit) and was never
    part of dataset identity. No dataset-identity field changed, so the
    schema version stays exactly as it was at ca70eaa."""
    assert DATASET_SCHEMA_VERSION == "ser8-historical-market-data-v3"

    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    ids = [item.chunk_id for item in plan_calendar_month_chunks(start, end)]
    source = WindowSource(
        {
            ids[0]: HistoricalDataError(
                "BROKER_HISTORY_NOT_RETAINED_FOR_RANGE", "no history that far back"
            ),
            ids[1]: (_bar(datetime(2026, 3, 5, tzinfo=UTC)),),
        }
    )
    coverage = _discover(tmp_path, source, start=start, end=end)
    assert coverage.resolution == "TRUNCATED_GENUINE_BOUNDARY"

    universe = build_canonical_execution_universe(
        [_execution_row()],
        account_login=ACCOUNT,
        raw_sha256=sha256_bytes(b"universe raw bytes"),
    )
    broker = BrokerSymbolV1(
        symbol="BTCUSD",
        trade_mode="FULL",
        source_row=_execution_row(),
        asset_class="UNKNOWN",
        risk_model_supported=True,
        risk_model_reason="",
    )
    manifest, _ = build_dataset_manifest(
        bars=coverage.bars,
        source_proof=_proof(),
        symbol_metadata={
            "name": "BTCUSD", "point": 0.01, "digits": 2, "visible": True, "trade_tick_size": 0.01,
        },
        broker_symbol=broker,
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe=universe,
        timeframe="M5",
        requested_from_utc=start,
        requested_to_utc=end,
        expected_interval_seconds=300,
        source_capture_utc=end,
        collector_code_sha256=COLLECTOR_SHA,
        coverage_discovery=coverage.manifest_summary(),
    )
    assert manifest["schema_version"] == "ser8-historical-market-data-v3"
    assert manifest["coverage_truncated_at_requested_start"] is True
    assert manifest["accepted_historical_data"] is True
    # No new identity field introduced by this fix: identity is exactly the
    # same key set verify_dataset already reconstructs and re-hashes.
    dataset_dir, persisted, created = publish_dataset(tmp_path / "dataset", manifest, _bars_bytes(coverage.bars))
    assert created is True
    verified = verify_dataset(dataset_dir)
    assert verified["dataset_sha256"] == manifest["dataset_sha256"]


def _bars_bytes(bars: Sequence[HistoricalBarV1]) -> bytes:
    from trademind.ser8_historical_data import canonical_bars_csv

    return canonical_bars_csv(bars)


def test_chunk_cache_identity_and_inventory_capacity_untouched() -> None:
    from trademind.ser8_historical_data import (
        CHUNK_ACQUISITION_CODE_SHA256,
        CHUNK_CACHE_SCHEMA_VERSION,
        CHUNK_COLLECTOR_VERSION,
        HISTORICAL_INVENTORY_JSON_BUDGET,
    )

    assert CHUNK_CACHE_SCHEMA_VERSION == "ser8-mt5-history-chunk-v1"
    assert CHUNK_COLLECTOR_VERSION == "1.1.0"
    assert CHUNK_ACQUISITION_CODE_SHA256 == (
        "sha256:34a3d2633b744942eee35ab72d291bb5205275abfc4c5a38bd122f83e02607da"
    )
    assert HISTORICAL_INVENTORY_JSON_BUDGET.max_total_string_bytes > 196_608
