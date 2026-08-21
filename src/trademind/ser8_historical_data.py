"""Read-only, content-addressed SER8 broker-history datasets.

The only live source adapter in this module is MetaTrader 5's Python
``copy_rates_range`` API.  It verifies the already-authenticated terminal and
account before reading rates and deliberately exposes no login, order, deal,
position, or symbol-selection operation.  Collection never repairs gaps or
mutates bar content: validation failures remain visible in the detailed
inventory and are excluded from the compatibility ``symbol,rows`` CSV.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from trademind.ser8_symbol_universe import (
    ASSET_CLASS_FX,
    SYMBOL_REQUIRED_FIELDS,
    classify_asset_class,
    risk_model_support_for_symbol_row,
)
from trademind.signal_statistics_provenance import canonical_json_bytes, sha256_bytes

DATASET_SCHEMA_VERSION = "ser8-historical-market-data-v1"
INVENTORY_SCHEMA_VERSION = "ser8-historical-data-inventory-v1"
SOURCE_PROOF_SCHEMA_VERSION = "ser8-mt5-history-source-proof-v1"
COLLECTOR_VERSION = "1.0.0"
SOURCE_TYPE = "MT5_PYTHON_COPY_RATES_RANGE"
BAR_FIELDS = (
    "time_utc",
    "symbol",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)
_DATASET_HASH_DOMAIN = b"trademind:ser8:historical-market-data:v1"
_INVENTORY_HASH_DOMAIN = b"trademind:ser8:historical-data-inventory:v1"
_LIVE_ARTIFACT_PATH_PARTS = frozenset({
    "live_signal_runtime_v1",
    "signal_intelligence_v1_16",
    "paper_signals",
})


class HistoricalDataError(RuntimeError):
    """Fail-closed acquisition, integrity, or publication error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrokerSymbolV1:
    symbol: str
    trade_mode: str
    source_row: Mapping[str, str]
    asset_class: str
    risk_model_supported: bool
    risk_model_reason: str


@dataclass(frozen=True, slots=True)
class HistoricalBarV1:
    time_utc: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int

    def __post_init__(self) -> None:
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise HistoricalDataError("BAR_TIME_NOT_UTC_AWARE", "bar time must be timezone-aware")
        object.__setattr__(self, "time_utc", self.time_utc.astimezone(timezone.utc))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "timeframe", self.timeframe.strip().upper())
        if not self.symbol or not self.timeframe:
            raise HistoricalDataError("BAR_IDENTITY_MISSING", "bar symbol/timeframe is required")

    def csv_row(self) -> dict[str, str]:
        return {
            "time_utc": _utc_text(self.time_utc),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open": _number_text(self.open),
            "high": _number_text(self.high),
            "low": _number_text(self.low),
            "close": _number_text(self.close),
            "tick_volume": str(self.tick_volume),
            "spread": str(self.spread),
            "real_volume": str(self.real_volume),
        }


class HistoricalRateSource(Protocol):
    def source_proof(self) -> Mapping[str, object]: ...

    def symbol_metadata(self, symbol: str) -> Mapping[str, object]: ...

    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        requested_from_utc: datetime,
        requested_to_utc: datetime,
    ) -> Sequence[HistoricalBarV1]: ...


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalDataError("TIME_NOT_AWARE", "timestamp must include timezone information")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalDataError("INVALID_UTC_TIMESTAMP", f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalDataError("INVALID_UTC_TIMESTAMP", "timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _number_text(value: float) -> str:
    return format(float(value), ".17g")


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_not_live_artifact_path(path: Path, *, field_name: str) -> Path:
    resolved = path.expanduser().resolve()
    lowered = {part.lower() for part in resolved.parts}
    forbidden = lowered & _LIVE_ARTIFACT_PATH_PARTS
    if forbidden or any(part.startswith("live_signal_runtime") for part in lowered):
        raise HistoricalDataError(
            "LIVE_ARTIFACT_PATH_FORBIDDEN",
            f"{field_name} cannot target a live runtime/journal path: {resolved}",
        )
    return resolved


def assert_historical_artifact_isolation(
    *,
    dataset_root: Path,
    inventory_path: Path,
    compatibility_path: Path,
) -> tuple[Path, Path, Path]:
    root = _assert_not_live_artifact_path(dataset_root, field_name="dataset_root")
    inventory = _assert_not_live_artifact_path(inventory_path, field_name="inventory")
    compatibility = _assert_not_live_artifact_path(
        compatibility_path, field_name="compatibility_csv"
    )
    for path, expected_name in (
        (inventory, "historical_inventory.json"),
        (compatibility, "historical_rows.csv"),
    ):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HistoricalDataError(
                "HISTORICAL_ARTIFACT_PATH_OUTSIDE_ROOT",
                f"{path.name} must be inside dataset_root",
            ) from exc
        if path.name != expected_name:
            raise HistoricalDataError(
                "HISTORICAL_ARTIFACT_NAME_INVALID",
                f"historical artifact must be named {expected_name}",
            )
    return root, inventory, compatibility


def load_broker_universe(symbols_csv: Path, *, account_login: str) -> tuple[BrokerSymbolV1, ...]:
    """Load every unique symbol from the real MT5 risk-symbol export."""
    if not symbols_csv.is_file():
        raise HistoricalDataError("BROKER_UNIVERSE_MISSING", f"broker universe not found: {symbols_csv}")
    with symbols_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [field for field in SYMBOL_REQUIRED_FIELDS if field not in fieldnames]
        if missing:
            raise HistoricalDataError(
                "BROKER_UNIVERSE_COLUMNS_MISSING",
                f"broker universe is missing required columns: {missing}",
            )
        raw_rows = [{key: str(value or "").strip() for key, value in row.items()} for row in reader]
    if not raw_rows:
        raise HistoricalDataError("BROKER_UNIVERSE_EMPTY", "broker universe contains no rows")

    expected_login = str(account_login).strip()
    by_symbol: dict[str, BrokerSymbolV1] = {}
    row_fingerprints: dict[str, bytes] = {}
    for row in raw_rows:
        if row.get("account_login") != expected_login:
            raise HistoricalDataError(
                "BROKER_UNIVERSE_ACCOUNT_MISMATCH",
                f"symbol export account {row.get('account_login')!r} does not match {expected_login!r}",
            )
        symbol = row.get("symbol", "").upper()
        if not symbol:
            raise HistoricalDataError("BROKER_SYMBOL_MISSING", "broker universe contains an empty symbol")
        encoded = canonical_json_bytes(row)
        if symbol in by_symbol:
            if row_fingerprints[symbol] != encoded:
                raise HistoricalDataError(
                    "BROKER_SYMBOL_CONFLICT",
                    f"broker universe contains conflicting rows for {symbol}",
                )
            continue
        supported, reason = risk_model_support_for_symbol_row(row)
        by_symbol[symbol] = BrokerSymbolV1(
            symbol=symbol,
            trade_mode=row.get("trade_mode", "").upper(),
            source_row=row,
            asset_class=classify_asset_class(symbol),
            risk_model_supported=supported,
            risk_model_reason=reason,
        )
        row_fingerprints[symbol] = encoded
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


class MetaTrader5HistorySource:
    """Narrow read-only adapter around the official MetaTrader5 package."""

    def __init__(
        self,
        *,
        account_login: str,
        terminal_path: Path | None = None,
        module: Any | None = None,
    ) -> None:
        self.account_login = str(account_login).strip()
        self.terminal_path = terminal_path
        self._mt5 = module
        self._proof: dict[str, object] | None = None

    def initialize(self) -> Mapping[str, object]:
        if self._mt5 is None:
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise HistoricalDataError(
                    "MT5_PYTHON_UNAVAILABLE",
                    "MetaTrader5 Python package is not installed in this environment",
                ) from exc
        arguments = (str(self.terminal_path),) if self.terminal_path is not None else ()
        if not self._mt5.initialize(*arguments):
            raise HistoricalDataError("MT5_INITIALIZE_FAILED", f"MT5 initialize failed: {self._last_error()}")
        terminal = self._mt5.terminal_info()
        account = self._mt5.account_info()
        if terminal is None or account is None:
            self.close()
            raise HistoricalDataError("MT5_IDENTITY_UNAVAILABLE", "terminal/account identity is unavailable")
        if not bool(getattr(terminal, "connected", False)):
            self.close()
            raise HistoricalDataError("MT5_NOT_CONNECTED", "MT5 terminal is not connected")
        actual_login = str(getattr(account, "login", ""))
        if actual_login != self.account_login:
            self.close()
            raise HistoricalDataError(
                "MT5_ACCOUNT_MISMATCH",
                f"active MT5 account {actual_login!r} does not match {self.account_login!r}",
            )
        server = str(getattr(account, "server", "") or "").strip()
        company = str(getattr(account, "company", "") or "").strip()
        terminal_company = str(getattr(terminal, "company", "") or "").strip()
        if not server or not (company or terminal_company):
            self.close()
            raise HistoricalDataError("MT5_BROKER_IDENTITY_MISSING", "MT5 broker/server identity is incomplete")
        version = self._mt5.version()
        self._proof = {
            "schema_version": SOURCE_PROOF_SCHEMA_VERSION,
            "source_type": SOURCE_TYPE,
            "account_login": actual_login,
            "account_server": server,
            "account_company": company or None,
            "account_currency": str(getattr(account, "currency", "") or "") or None,
            "terminal_company": terminal_company or None,
            "terminal_name": str(getattr(terminal, "name", "") or "") or None,
            "terminal_path": str(getattr(terminal, "path", "") or "") or None,
            "terminal_connected": True,
            "terminal_trade_allowed_observed": bool(getattr(terminal, "trade_allowed", False)),
            "mt5_version": list(version) if version is not None else None,
            "authenticated_account_verified": True,
            "utc_contract": "copy_rates_range inputs and returned epoch seconds interpreted as UTC",
            "read_only_operations": ["initialize", "terminal_info", "account_info", "version", "symbol_info", "copy_rates_range"],
        }
        return dict(self._proof)

    def _last_error(self) -> object:
        return self._mt5.last_error() if self._mt5 is not None else None

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()

    def source_proof(self) -> Mapping[str, object]:
        if self._proof is None:
            return self.initialize()
        return dict(self._proof)

    def symbol_metadata(self, symbol: str) -> Mapping[str, object]:
        self.source_proof()
        requested = symbol.strip().upper()
        info = self._mt5.symbol_info(requested)
        if info is None:
            raise HistoricalDataError("BROKER_SYMBOL_UNAVAILABLE", f"MT5 symbol_info unavailable for {requested}")
        actual = str(getattr(info, "name", "") or "").strip().upper()
        if actual != requested:
            raise HistoricalDataError(
                "BROKER_SYMBOL_IDENTITY_MISMATCH",
                f"MT5 returned symbol identity {actual!r} for {requested!r}",
            )
        point = float(getattr(info, "point", 0.0) or 0.0)
        if not math.isfinite(point) or point <= 0:
            raise HistoricalDataError("BROKER_SYMBOL_POINT_INVALID", f"MT5 point is invalid for {requested}")
        trade_mode_code = int(getattr(info, "trade_mode", -1))
        trade_mode_name = next(
            (
                name
                for name in ("DISABLED", "CLOSEONLY", "LONGONLY", "SHORTONLY", "FULL")
                if trade_mode_code == getattr(self._mt5, f"SYMBOL_TRADE_MODE_{name}", object())
            ),
            "UNKNOWN",
        )
        return {
            "name": actual,
            "visible": bool(getattr(info, "visible", False)),
            "select": bool(getattr(info, "select", False)),
            "trade_mode_code": trade_mode_code,
            "trade_mode_name": trade_mode_name,
            "digits": int(getattr(info, "digits", 0)),
            "point": point,
        }

    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        requested_from_utc: datetime,
        requested_to_utc: datetime,
    ) -> tuple[HistoricalBarV1, ...]:
        self.source_proof()
        normalized_timeframe = timeframe.strip().upper()
        mt5_timeframe = getattr(self._mt5, f"TIMEFRAME_{normalized_timeframe}", None)
        if mt5_timeframe is None:
            raise HistoricalDataError("MT5_TIMEFRAME_UNSUPPORTED", f"unsupported MT5 timeframe: {timeframe}")
        if requested_from_utc.tzinfo is None or requested_to_utc.tzinfo is None:
            raise HistoricalDataError("TIME_NOT_AWARE", "rate request timestamps must be timezone-aware")
        if requested_to_utc <= requested_from_utc:
            raise HistoricalDataError("INVALID_TIME_RANGE", "requested_to_utc must follow requested_from_utc")
        requested = symbol.strip().upper()
        metadata = self.symbol_metadata(requested)
        if not metadata["visible"]:
            raise HistoricalDataError(
                "BROKER_SYMBOL_DISABLED",
                f"{requested} is not enabled/visible; collector will not mutate Market Watch selection",
            )
        rates = self._mt5.copy_rates_range(
            requested,
            mt5_timeframe,
            requested_from_utc.astimezone(timezone.utc),
            requested_to_utc.astimezone(timezone.utc),
        )
        if rates is None:
            raise HistoricalDataError("BROKER_HISTORY_UNAVAILABLE", f"copy_rates_range failed: {self._last_error()}")
        names = tuple(getattr(getattr(rates, "dtype", None), "names", ()) or ())
        bars: list[HistoricalBarV1] = []
        for rate in rates:
            row = {name: rate[name] for name in names} if names else dict(rate)
            bars.append(
                HistoricalBarV1(
                    time_utc=datetime.fromtimestamp(int(row["time"]), tz=timezone.utc),
                    symbol=requested,
                    timeframe=normalized_timeframe,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread=int(row["spread"]),
                    real_volume=int(row["real_volume"]),
                )
            )
        return tuple(bars)


def canonical_bars_csv(bars: Sequence[HistoricalBarV1]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=BAR_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(bar.csv_row() for bar in bars)
    return output.getvalue().encode("utf-8")


def load_canonical_bars(path: Path) -> tuple[HistoricalBarV1, ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != BAR_FIELDS:
            raise HistoricalDataError("BARS_SCHEMA_INVALID", f"canonical bar columns are invalid: {path}")
        bars = []
        for row in reader:
            bars.append(
                HistoricalBarV1(
                    time_utc=parse_utc(row["time_utc"]),
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread=int(row["spread"]),
                    real_volume=int(row["real_volume"]),
                )
            )
    return tuple(bars)


def validate_historical_bars(
    bars: Sequence[HistoricalBarV1],
    *,
    symbol: str,
    timeframe: str,
    expected_interval_seconds: int,
) -> dict[str, object]:
    if expected_interval_seconds <= 0:
        raise HistoricalDataError("EXPECTED_INTERVAL_INVALID", "expected interval must be positive")
    requested_symbol = symbol.strip().upper()
    requested_timeframe = timeframe.strip().upper()
    timestamps = [bar.time_utc for bar in bars]
    unique_timestamps = set(timestamps)
    duplicate_count = len(timestamps) - len(unique_timestamps)
    monotonic = all(left < right for left, right in zip(timestamps, timestamps[1:], strict=False))
    identity_pass = all(
        bar.symbol == requested_symbol and bar.timeframe == requested_timeframe for bar in bars
    )
    finite_rows = [
        all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close))
        and all(type(value) is int for value in (bar.tick_volume, bar.spread, bar.real_volume))
        for bar in bars
    ]
    negative_volume_count = sum(
        bar.tick_volume < 0 or bar.spread < 0 or bar.real_volume < 0 for bar in bars
    )
    numeric_pass = all(finite_rows) and negative_volume_count == 0
    zero_or_negative = sum(
        any(value <= 0 for value in (bar.open, bar.high, bar.low, bar.close)) for bar in bars
    )
    high_low = sum(bar.high < bar.low for bar in bars)
    open_outside = sum(not bar.low <= bar.open <= bar.high for bar in bars)
    close_outside = sum(not bar.low <= bar.close <= bar.high for bar in bars)
    ohlc_pass = zero_or_negative == high_low == open_outside == close_outside == 0

    sorted_unique = sorted(unique_timestamps)
    gaps: list[int] = []
    weekend_overlap = 0
    for left, right in zip(sorted_unique, sorted_unique[1:], strict=False):
        delta = int((right - left).total_seconds())
        if delta > expected_interval_seconds:
            gaps.append(delta)
            cursor = left
            touches_weekend = False
            while cursor < right and not touches_weekend:
                cursor += timedelta(seconds=min(expected_interval_seconds, 86_400))
                touches_weekend = cursor.weekday() >= 5
            weekend_overlap += int(touches_weekend)
    quality_pass = bool(bars) and identity_pass and monotonic and duplicate_count == 0 and numeric_pass and ohlc_pass
    return {
        "row_count": len(bars),
        "unique_timestamp_count": len(unique_timestamps),
        "duplicate_timestamp_count": duplicate_count,
        "monotonic_timestamp_pass": monotonic,
        "symbol_timeframe_identity_pass": identity_pass,
        "ohlc_integrity_pass": ohlc_pass,
        "numeric_integrity_pass": numeric_pass,
        "gap_count": len(gaps),
        "largest_gap_seconds": max(gaps, default=0),
        "expected_interval_seconds": expected_interval_seconds,
        "weekend_session_gap_classification": "UTC_WEEKEND_OVERLAP_OBSERVED_ONLY_BROKER_SESSION_NOT_ASSUMED",
        "weekend_overlap_gap_count": weekend_overlap,
        "unexplained_gap_count": len(gaps),
        "zero_or_negative_price_count": zero_or_negative,
        "high_low_violation_count": high_low,
        "open_outside_high_low_count": open_outside,
        "close_outside_high_low_count": close_outside,
        "negative_volume_or_spread_count": negative_volume_count,
        "data_integrity_pass": quality_pass,
    }


def build_dataset_manifest(
    *,
    bars: Sequence[HistoricalBarV1],
    source_proof: Mapping[str, object],
    symbol_metadata: Mapping[str, object],
    broker_symbol: BrokerSymbolV1,
    timeframe: str,
    requested_from_utc: datetime,
    requested_to_utc: datetime,
    expected_interval_seconds: int,
    source_capture_utc: datetime,
    collector_code_sha256: str,
) -> tuple[dict[str, object], bytes]:
    quality = validate_historical_bars(
        bars,
        symbol=broker_symbol.symbol,
        timeframe=timeframe,
        expected_interval_seconds=expected_interval_seconds,
    )
    bars_bytes = canonical_bars_csv(bars)
    bars_sha256 = sha256_bytes(bars_bytes)
    dataset_identity = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source_type": SOURCE_TYPE,
        "broker_server": source_proof.get("account_server"),
        "broker_company": source_proof.get("account_company") or source_proof.get("terminal_company"),
        "account_login": source_proof.get("account_login"),
        "symbol": broker_symbol.symbol,
        "timeframe": timeframe.strip().upper(),
        "requested_from_utc": _utc_text(requested_from_utc),
        "requested_to_utc": _utc_text(requested_to_utc),
        "actual_first_bar_utc": _utc_text(min(bar.time_utc for bar in bars)) if bars else None,
        "actual_last_bar_utc": _utc_text(max(bar.time_utc for bar in bars)) if bars else None,
        "bars_sha256": bars_sha256,
        "symbol_point": symbol_metadata.get("point"),
        "symbol_digits": symbol_metadata.get("digits"),
        "expected_interval_seconds": expected_interval_seconds,
    }
    digest = hashlib.sha256()
    digest.update(_DATASET_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(dataset_identity))
    dataset_sha256 = digest.hexdigest()
    manifest: dict[str, object] = {
        **dataset_identity,
        "dataset_id": f"ser8-historical:sha256:{dataset_sha256}",
        "dataset_sha256": dataset_sha256,
        "source_capture_utc": _utc_text(source_capture_utc),
        "source_proof": dict(source_proof),
        "source_symbol_metadata": dict(symbol_metadata),
        "broker_trade_mode": broker_symbol.trade_mode,
        "asset_class": broker_symbol.asset_class,
        "risk_model_supported": broker_symbol.risk_model_supported,
        "risk_model_reason": broker_symbol.risk_model_reason or None,
        "collector_version": COLLECTOR_VERSION,
        "collector_code_sha256": collector_code_sha256,
        **quality,
        "quality": quality,
        "accepted_historical_data": bool(quality["data_integrity_pass"]),
        "gap_repair_performed": False,
        "synthetic_bars_added": 0,
    }
    manifest["manifest_sha256"] = _sha256_hex(canonical_json_bytes(manifest))
    return manifest, bars_bytes


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Directory fsync is not available on every Windows filesystem;
            # file fsync + same-volume atomic rename remains the V1 contract.
            pass
    finally:
        os.close(descriptor)


def publish_dataset(
    root: Path,
    manifest: Mapping[str, object],
    bars_bytes: bytes,
) -> tuple[Path, dict[str, object], bool]:
    """Atomically publish or verify an idempotently pre-existing dataset."""
    root = _assert_not_live_artifact_path(root, field_name="dataset_root")
    dataset_sha256 = str(manifest["dataset_sha256"])
    if not len(dataset_sha256) == 64 or any(char not in "0123456789abcdef" for char in dataset_sha256):
        raise HistoricalDataError("DATASET_HASH_INVALID", "dataset_sha256 is malformed")
    root.mkdir(parents=True, exist_ok=True)
    if manifest.get("bars_sha256") != sha256_bytes(bars_bytes):
        raise HistoricalDataError("DATASET_BARS_HASH_MISMATCH", "manifest does not match supplied bars")
    semantic_manifest = dict(manifest)
    supplied_manifest_hash = semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != _sha256_hex(canonical_json_bytes(semantic_manifest)):
        raise HistoricalDataError("DATASET_MANIFEST_HASH_MISMATCH", "manifest hash is invalid before publication")
    destination = root / dataset_sha256
    manifest_bytes = canonical_json_bytes(dict(manifest)) + b"\n"
    if destination.exists():
        existing_manifest_path = destination / "manifest.json"
        existing_bars_path = destination / "bars.csv"
        if not existing_manifest_path.is_file() or not existing_bars_path.is_file():
            raise HistoricalDataError("DATASET_COLLISION", f"incomplete dataset already exists: {destination}")
        existing_manifest = verify_dataset(destination)
        if (
            existing_manifest.get("dataset_sha256") != dataset_sha256
            or existing_bars_path.read_bytes() != bars_bytes
            or existing_manifest.get("bars_sha256") != sha256_bytes(bars_bytes)
        ):
            raise HistoricalDataError("DATASET_COLLISION", "different content exists under the same dataset identity")
        return destination, existing_manifest, False

    temporary = Path(tempfile.mkdtemp(prefix=".ser8-history-", dir=root))
    try:
        _write_synced(temporary / "bars.csv", bars_bytes)
        _write_synced(temporary / "manifest.json", manifest_bytes)
        _fsync_directory(temporary)
        temporary.replace(destination)
        _fsync_directory(root)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination, dict(manifest), True


def verify_dataset(dataset_dir: Path) -> dict[str, object]:
    manifest_path = dataset_dir / "manifest.json"
    bars_path = dataset_dir / "bars.csv"
    if not manifest_path.is_file() or not bars_path.is_file():
        raise HistoricalDataError("DATASET_FILES_MISSING", f"dataset files missing: {dataset_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HistoricalDataError("DATASET_MANIFEST_INVALID", f"invalid dataset manifest: {manifest_path}") from exc
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise HistoricalDataError("DATASET_SCHEMA_INVALID", f"unsupported dataset schema: {dataset_dir}")
    supplied_manifest_hash = manifest.get("manifest_sha256")
    semantic_manifest = dict(manifest)
    semantic_manifest.pop("manifest_sha256", None)
    if supplied_manifest_hash != _sha256_hex(canonical_json_bytes(semantic_manifest)):
        raise HistoricalDataError("DATASET_MANIFEST_HASH_MISMATCH", f"manifest hash mismatch: {dataset_dir}")
    bars_bytes = bars_path.read_bytes()
    if manifest.get("bars_sha256") != sha256_bytes(bars_bytes):
        raise HistoricalDataError("DATASET_BARS_HASH_MISMATCH", f"bars checksum mismatch: {dataset_dir}")
    bars = load_canonical_bars(bars_path)
    quality = validate_historical_bars(
        bars,
        symbol=str(manifest["symbol"]),
        timeframe=str(manifest["timeframe"]),
        expected_interval_seconds=int(manifest["expected_interval_seconds"]),
    )
    if quality != manifest.get("quality"):
        raise HistoricalDataError("DATASET_QUALITY_MISMATCH", f"quality manifest mismatch: {dataset_dir}")
    if manifest.get("accepted_historical_data") is not bool(quality["data_integrity_pass"]):
        raise HistoricalDataError("DATASET_ACCEPTANCE_MISMATCH", f"acceptance mismatch: {dataset_dir}")
    identity_keys = (
        "schema_version", "source_type", "broker_server", "broker_company", "account_login",
        "symbol", "timeframe", "requested_from_utc", "requested_to_utc", "actual_first_bar_utc",
        "actual_last_bar_utc", "bars_sha256", "symbol_point", "symbol_digits",
        "expected_interval_seconds",
    )
    identity = {key: manifest.get(key) for key in identity_keys}
    digest = hashlib.sha256()
    digest.update(_DATASET_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(identity))
    expected = digest.hexdigest()
    if manifest.get("dataset_sha256") != expected or dataset_dir.name != expected:
        raise HistoricalDataError("DATASET_IDENTITY_MISMATCH", f"dataset identity mismatch: {dataset_dir}")
    return manifest


def inventory_hash(payload: Mapping[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("inventory_sha256", None)
    digest = hashlib.sha256()
    digest.update(_INVENTORY_HASH_DOMAIN)
    digest.update(b"\x00")
    digest.update(canonical_json_bytes(semantic))
    return digest.hexdigest()


def verify_inventory(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise HistoricalDataError("INVENTORY_SCHEMA_INVALID", "unsupported historical inventory schema")
    supplied = payload.get("inventory_sha256")
    if not isinstance(supplied, str) or supplied != inventory_hash(payload):
        raise HistoricalDataError("INVENTORY_HASH_MISMATCH", "historical inventory hash mismatch")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise HistoricalDataError("INVENTORY_ENTRIES_INVALID", "historical inventory entries must be a list")
    symbols: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str):
            raise HistoricalDataError("INVENTORY_ENTRY_INVALID", "historical inventory entry is malformed")
        symbol = entry["symbol"]
        if symbol in symbols:
            raise HistoricalDataError("INVENTORY_SYMBOL_DUPLICATE", f"duplicate inventory symbol: {symbol}")
        symbols.add(symbol)


def load_inventory(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalDataError("INVENTORY_READ_FAILED", f"cannot read inventory: {path}") from exc
    if not isinstance(payload, dict):
        raise HistoricalDataError("INVENTORY_ROOT_INVALID", "historical inventory root must be an object")
    verify_inventory(payload)
    return payload


def write_inventory_artifacts(
    *,
    inventory_path: Path,
    compatibility_path: Path,
    payload: Mapping[str, object],
) -> None:
    inventory_payload = dict(payload)
    inventory_payload["inventory_sha256"] = inventory_hash(inventory_payload)
    entries = inventory_payload.get("entries", [])
    accepted = sorted(
        (
            (str(entry["symbol"]), int(entry["row_count"]))
            for entry in entries
            if isinstance(entry, dict) and entry.get("accepted_historical_data") is True
        ),
        key=lambda item: item[0],
    )
    compatibility = io.StringIO(newline="")
    writer = csv.writer(compatibility, lineterminator="\n")
    writer.writerow(("symbol", "rows"))
    writer.writerows(accepted)
    for path, content in (
        (inventory_path, canonical_json_bytes(inventory_payload) + b"\n"),
        (compatibility_path, compatibility.getvalue().encode("utf-8")),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        _write_synced(temporary, content)
        temporary.replace(path)
        _fsync_directory(path.parent)


def collector_code_sha256() -> str:
    return sha256_bytes(Path(__file__).read_bytes())


def source_proof_result(proof: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "SOURCE_VERIFIED",
        "source_proof": dict(proof),
        "orders_sent": 0,
        "orders_canceled": 0,
        "positions_modified": 0,
    }


__all__ = [
    "ASSET_CLASS_FX",
    "BAR_FIELDS",
    "COLLECTOR_VERSION",
    "DATASET_SCHEMA_VERSION",
    "INVENTORY_SCHEMA_VERSION",
    "BrokerSymbolV1",
    "HistoricalBarV1",
    "HistoricalDataError",
    "HistoricalRateSource",
    "MetaTrader5HistorySource",
    "assert_historical_artifact_isolation",
    "build_dataset_manifest",
    "canonical_bars_csv",
    "collector_code_sha256",
    "inventory_hash",
    "load_broker_universe",
    "load_canonical_bars",
    "load_inventory",
    "parse_utc",
    "publish_dataset",
    "source_proof_result",
    "validate_historical_bars",
    "verify_dataset",
    "verify_inventory",
    "write_inventory_artifacts",
]
