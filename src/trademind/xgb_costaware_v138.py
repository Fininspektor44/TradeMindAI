"""TradeMind v1.38 XGBoost cost-aware BTC walk-forward research.

This is a read-only reproduction of the core economic idea in:
Bysik & Slepaczuk (2026), "Machine Learning-Based Bitcoin Trading Under
Transaction Costs: Evidence From Walk-Forward Forecasting".

Key design choices reproduced:
* hourly BTC/USDT data;
* 12-month train, 3-month validation, 3-month test rolling walk-forward;
* XGBoost one-step-ahead return regression;
* OHLCV plus technical-analysis features;
* validation-loss model selection;
* long-only cost-aware execution;
* lambda=2 threshold and explicit transaction costs.

The paper's complete private research code is not public, so this is deliberately
labelled a transparent reproduction rather than an exact numerical replica.
All feature selection is training-only. No orders. No API keys. No publication.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import statistics
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

VERSION = "1.38.0"
HOURS_PER_YEAR = 365.25 * 24.0
WINDOWS = (3, 6, 12, 24, 48, 72, 168, 336)
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"


@dataclass(frozen=True, slots=True)
class Metrics:
    observations: int
    changes: int
    entries: int
    exits: int
    exposure_pct: float
    total_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    sharpe: float
    max_drawdown_pct: float
    buy_hold_return_pct: float


MODEL_GRID = (
    {
        "max_depth": 2,
        "learning_rate": 0.010,
        "n_estimators": 1200,
        "min_child_weight": 20.0,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "reg_alpha": 0.001,
        "reg_lambda": 10.0,
    },
    {
        "max_depth": 3,
        "learning_rate": 0.010,
        "n_estimators": 1500,
        "min_child_weight": 20.0,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "reg_alpha": 0.001,
        "reg_lambda": 10.0,
    },
    {
        "max_depth": 3,
        "learning_rate": 0.020,
        "n_estimators": 1000,
        "min_child_weight": 10.0,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "reg_alpha": 0.010,
        "reg_lambda": 5.0,
    },
    {
        "max_depth": 4,
        "learning_rate": 0.010,
        "n_estimators": 1400,
        "min_child_weight": 30.0,
        "subsample": 0.70,
        "colsample_bytree": 0.75,
        "reg_alpha": 0.005,
        "reg_lambda": 20.0,
    },
)


def _imports():
    import numpy as np
    import pandas as pd
    import xgboost as xgb

    return np, pd, xgb


def _month_starts(start: datetime, end: datetime) -> Iterable[datetime]:
    cur = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    stop = datetime(end.year, end.month, 1, tzinfo=timezone.utc)
    while cur < stop:
        yield cur
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)


def _normalize_epoch(value: int) -> int:
    # Binance public archives switched selected datasets to microsecond timestamps.
    if value > 10**15:
        return value // 1000
    return value


def _download(url: str, target: Path, refresh: bool) -> bool:
    if target.is_file() and target.stat().st_size > 0 and not refresh:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "TradeMindAI/1.38"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            tmp.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  missing archive: {url}")
            if tmp.exists():
                tmp.unlink()
            return False
        raise
    tmp.replace(target)
    return True


def _read_month(path: Path) -> list[tuple[int, float, float, float, float, float]]:
    rows: list[tuple[int, float, float, float, float, float]] = []
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            return rows
        with archive.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8")
            for item in csv.reader(text):
                if len(item) < 6:
                    continue
                try:
                    ts = _normalize_epoch(int(item[0]))
                    values = tuple(float(item[i]) for i in range(1, 6))
                except ValueError:
                    continue
                if ts <= 0 or min(values[:4]) <= 0:
                    continue
                rows.append((ts, values[0], values[1], values[2], values[3], max(0.0, values[4])))
    return rows


def ensure_hourly_history(
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    refresh: bool,
):
    _, pd, _ = _imports()
    all_rows: dict[int, tuple[int, float, float, float, float, float]] = {}
    monthly = cache_dir / "monthly"
    for month in _month_starts(start, end):
        stamp = month.strftime("%Y-%m")
        name = f"{symbol}-1h-{stamp}.zip"
        url = f"{BASE_URL}/{symbol}/1h/{name}"
        target = monthly / name
        print(f"archive {symbol} {stamp}")
        if not _download(url, target, refresh):
            continue
        try:
            for row in _read_month(target):
                all_rows[row[0]] = row
        except zipfile.BadZipFile:
            target.unlink(missing_ok=True)
            if _download(url, target, True):
                for row in _read_month(target):
                    all_rows[row[0]] = row

    if not all_rows:
        raise RuntimeError("No Binance H1 history was loaded")
    ordered = [all_rows[key] for key in sorted(all_rows)]
    frame = pd.DataFrame(ordered, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    frame["time"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True)
    frame = frame.set_index("time").sort_index()
    frame = frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    if len(frame) < 10_000:
        raise RuntimeError(f"Insufficient H1 history: {len(frame)} rows")
    return frame


def _rsi(close, window: int):
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(window).mean()
    loss = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, float("nan"))
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(series, span: int):
    return series.ewm(span=max(2, span), adjust=False, min_periods=max(2, span)).mean()


def _mfi(frame, window: int):
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    flow = typical * frame["volume"]
    direction = typical.diff()
    positive = flow.where(direction > 0.0, 0.0).rolling(window).sum()
    negative = flow.where(direction < 0.0, 0.0).rolling(window).sum()
    ratio = positive / negative.replace(0.0, float("nan"))
    return 100.0 - 100.0 / (1.0 + ratio)


def build_features(raw):
    np, pd, _ = _imports()
    df = raw.copy()
    eps = 1e-12
    log_close = np.log(df["close"])
    log_volume = np.log1p(df["volume"])

    # 15 stationary OHLCV-derived predictors.
    for lag in (1, 2, 3, 6, 12, 24):
        df[f"base_ret_{lag}"] = log_close.diff(lag)
    df["base_hl_range"] = (df["high"] - df["low"]) / df["close"]
    df["base_body"] = (df["close"] - df["open"]) / df["open"]
    df["base_upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"]
    df["base_lower_wick"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["close"]
    df["base_close_location"] = (df["close"] - df["low"]) / (df["high"] - df["low"] + eps)
    df["base_log_volume"] = log_volume
    df["base_vol_chg_1"] = log_volume.diff(1)
    df["base_vol_chg_6"] = log_volume.diff(6)
    roll_mean = log_volume.rolling(24).mean()
    roll_std = log_volume.rolling(24).std()
    df["base_vol_z24"] = (log_volume - roll_mean) / roll_std.replace(0.0, np.nan)

    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    signed_volume = np.sign(df["close"].diff().fillna(0.0)) * df["volume"]
    obv = signed_volume.cumsum()

    groups: dict[str, list[str]] = {
        "momentum": [],
        "dist_sma": [],
        "macd": [],
        "macd_hist": [],
        "atr_ratio": [],
        "roll_std": [],
        "bb_pos": [],
        "vwap_dev": [],
        "obv_slope": [],
        "mfi": [],
    }

    for window in WINDOWS:
        rsi_name = f"ta_rsi_{window}"
        roc_name = f"ta_roc_{window}"
        df[rsi_name] = (_rsi(df["close"], window) - 50.0) / 50.0
        df[roc_name] = df["close"].pct_change(window)
        groups["momentum"].extend((rsi_name, roc_name))

        sma = df["close"].rolling(window).mean()
        name = f"ta_dist_sma_{window}"
        df[name] = df["close"] / sma - 1.0
        groups["dist_sma"].append(name)

        fast = max(2, window // 2)
        slow = max(fast + 1, window)
        signal_span = max(2, window // 3)
        macd = _ema(df["close"], fast) - _ema(df["close"], slow)
        signal = _ema(macd, signal_span)
        name = f"ta_macd_{window}"
        hist_name = f"ta_macd_hist_{window}"
        df[name] = macd / df["close"]
        df[hist_name] = (macd - signal) / df["close"]
        groups["macd"].append(name)
        groups["macd_hist"].append(hist_name)

        atr = true_range.rolling(window).mean()
        name = f"ta_atr_ratio_{window}"
        df[name] = atr / df["close"]
        groups["atr_ratio"].append(name)

        name = f"ta_roll_std_{window}"
        df[name] = log_close.diff().rolling(window).std()
        groups["roll_std"].append(name)

        std = df["close"].rolling(window).std()
        name = f"ta_bb_pos_{window}"
        df[name] = (df["close"] - sma) / (2.0 * std.replace(0.0, np.nan))
        groups["bb_pos"].append(name)

        pv = (typical * df["volume"]).rolling(window).sum()
        vv = df["volume"].rolling(window).sum()
        vwap = pv / vv.replace(0.0, np.nan)
        name = f"ta_vwap_dev_{window}"
        df[name] = df["close"] / vwap - 1.0
        groups["vwap_dev"].append(name)

        name = f"ta_obv_slope_{window}"
        denom = df["volume"].rolling(window).sum().replace(0.0, np.nan)
        df[name] = (obv - obv.shift(window)) / denom
        groups["obv_slope"].append(name)

        name = f"ta_mfi_{window}"
        df[name] = (_mfi(df, window) - 50.0) / 50.0
        groups["mfi"].append(name)

    df["target_log_return"] = log_close.shift(-1) - log_close
    df["realized_simple_return"] = np.exp(df["target_log_return"]) - 1.0
    df = df.replace([np.inf, -np.inf], np.nan)
    return df, groups


def _select_features(train, groups: dict[str, list[str]]) -> list[str]:
    base = [column for column in train.columns if column.startswith("base_")]
    selected: list[str] = []
    target = train["target_log_return"]
    for group, candidates in groups.items():
        best_name = None
        best_score = -1.0
        for name in candidates:
            if name not in train:
                continue
            pair = train[[name, "target_log_return"]].dropna()
            if len(pair) < 500:
                continue
            corr = pair[name].corr(pair["target_log_return"])
            score = abs(float(corr)) if corr is not None and math.isfinite(float(corr)) else 0.0
            if score > best_score:
                best_name = name
                best_score = score
        if best_name:
            selected.append(best_name)
    return base + selected


def _month_offset(timestamp, months: int):
    _, pd, _ = _imports()
    return timestamp + pd.DateOffset(months=months)


def walk_forward(feature_frame, groups, output_dir: Path):
    np, pd, xgb = _imports()
    first_test = pd.Timestamp("2019-04-01", tz="UTC")
    final_end = feature_frame.index.max().floor("h")
    cursor = first_test
    predictions = []
    fold_records: list[dict] = []
    fold = 0

    while cursor < final_end:
        train_start = _month_offset(cursor, -15)
        val_start = _month_offset(cursor, -3)
        test_end = _month_offset(cursor, 3)
        if test_end > final_end + pd.Timedelta(hours=1):
            test_end = final_end + pd.Timedelta(hours=1)
        train = feature_frame[(feature_frame.index >= train_start) & (feature_frame.index < val_start)]
        val = feature_frame[(feature_frame.index >= val_start) & (feature_frame.index < cursor)]
        test = feature_frame[(feature_frame.index >= cursor) & (feature_frame.index < test_end)]
        if len(train) < 5000 or len(val) < 1000 or len(test) < 200:
            cursor = _month_offset(cursor, 3)
            continue

        features = _select_features(train, groups)
        required = features + ["target_log_return"]
        train_clean = train[required].dropna()
        val_clean = val[required].dropna()
        test_clean = test[features + ["target_log_return", "realized_simple_return"]].dropna()
        if len(train_clean) < 5000 or len(val_clean) < 1000 or len(test_clean) < 100:
            cursor = _month_offset(cursor, 3)
            continue

        x_train = train_clean[features].to_numpy(dtype=float)
        y_train = train_clean["target_log_return"].to_numpy(dtype=float)
        x_val = val_clean[features].to_numpy(dtype=float)
        y_val = val_clean["target_log_return"].to_numpy(dtype=float)

        best_params = None
        best_mse = math.inf
        for params in MODEL_GRID:
            model = xgb.XGBRegressor(
                objective="reg:squarederror",
                tree_method="hist",
                random_state=20260808,
                n_jobs=max(1, (os.cpu_count() or 4) - 1),
                **params,
            )
            model.fit(x_train, y_train, verbose=False)
            forecast = model.predict(x_val)
            mse = float(np.mean((forecast - y_val) ** 2))
            if mse < best_mse:
                best_mse = mse
                best_params = dict(params)

        assert best_params is not None
        combined = pd.concat([train_clean, val_clean], axis=0)
        final_model = xgb.XGBRegressor(
            objective="reg:squarederror",
            tree_method="hist",
            random_state=20260808,
            n_jobs=max(1, (os.cpu_count() or 4) - 1),
            **best_params,
        )
        final_model.fit(
            combined[features].to_numpy(dtype=float),
            combined["target_log_return"].to_numpy(dtype=float),
            verbose=False,
        )
        forecast = final_model.predict(test_clean[features].to_numpy(dtype=float))
        out = test_clean[["target_log_return", "realized_simple_return"]].copy()
        out["prediction"] = forecast
        out["fold"] = fold + 1
        predictions.append(out)
        fold += 1
        fold_records.append(
            {
                "fold": fold,
                "train_start": str(train_start),
                "validation_start": str(val_start),
                "test_start": str(cursor),
                "test_end": str(test_end),
                "validation_mse": best_mse,
                "features": features,
                "params": best_params,
                "test_rows": len(out),
            }
        )
        print(
            f"fold={fold:02d} test={cursor.date()}..{test_end.date()} "
            f"rows={len(out)} val_mse={best_mse:.8g} features={len(features)}"
        )
        cursor = _month_offset(cursor, 3)

    if not predictions:
        raise RuntimeError("No walk-forward folds completed")
    joined = pd.concat(predictions).sort_index()
    joined = joined[~joined.index.duplicated(keep="first")]
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "folds.json").write_text(json.dumps(fold_records, indent=2), encoding="utf-8")
    joined.to_csv(output_dir / "predictions.csv", index_label="time")
    return joined, fold_records


def execute_cost_aware(predictions, cost: float, lam: float):
    _, pd, _ = _imports()
    position = 0
    rows: list[dict] = []
    for timestamp, row in predictions.iterrows():
        forecast = float(row["prediction"])
        desired = 1 if forecast > 0.0 else 0
        old = position
        turnover_if_changed = abs(desired - old)
        changed = False
        if turnover_if_changed > 0:
            hurdle = lam * cost * turnover_if_changed
            if abs(forecast) > hurdle:
                position = desired
                changed = position != old
        turnover = abs(position - old)
        gross = position * float(row["realized_simple_return"])
        net = gross - cost * turnover
        rows.append(
            {
                "time": timestamp,
                "prediction": forecast,
                "position": position,
                "turnover": turnover,
                "gross_return": gross,
                "cost": cost * turnover,
                "net_return": net,
                "market_return": float(row["realized_simple_return"]),
                "changed": int(changed),
            }
        )
    return pd.DataFrame(rows).set_index("time")


def _metrics(frame) -> Metrics:
    np, _, _ = _imports()
    if frame.empty:
        return Metrics(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    returns = frame["net_return"].to_numpy(dtype=float)
    equity = np.cumprod(1.0 + returns)
    total = float(equity[-1] - 1.0)
    years = len(frame) / HOURS_PER_YEAR
    annual = (float(equity[-1]) ** (1.0 / years) - 1.0) if years > 0 and equity[-1] > 0 else -1.0
    sigma = float(np.std(returns, ddof=0))
    sharpe = float(np.mean(returns) / sigma * math.sqrt(HOURS_PER_YEAR)) if sigma > 1e-12 else 0.0
    annual_vol = sigma * math.sqrt(HOURS_PER_YEAR)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    changes = int(frame["turnover"].sum())
    entries = int(((frame["position"] == 1) & (frame["position"].shift(1, fill_value=0) == 0)).sum())
    exits = int(((frame["position"] == 0) & (frame["position"].shift(1, fill_value=0) == 1)).sum())
    buy_hold = float(np.prod(1.0 + frame["market_return"].to_numpy(dtype=float)) - 1.0)
    return Metrics(
        observations=len(frame),
        changes=changes,
        entries=entries,
        exits=exits,
        exposure_pct=float(frame["position"].mean() * 100.0),
        total_return_pct=total * 100.0,
        annualized_return_pct=annual * 100.0,
        annualized_vol_pct=annual_vol * 100.0,
        sharpe=sharpe,
        max_drawdown_pct=float(drawdown.min() * 100.0),
        buy_hold_return_pct=buy_hold * 100.0,
    )


def _slice(frame, start: str | None, end: str | None):
    _, pd, _ = _imports()
    result = frame
    if start:
        result = result[result.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        result = result[result.index < pd.Timestamp(end, tz="UTC")]
    return result


def run(args: argparse.Namespace) -> int:
    _, pd, _ = _imports()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).astimezone(timezone.utc)
    output_dir = args.output_dir.resolve()
    history_dir = output_dir / "history"

    print("TradeMind v1.38 XGBOOST COST-AWARE WALK-FORWARD")
    print("Paper core: 12m train -> 3m validation -> 3m test, shift 3m")
    print("Long-only. Cost-aware lambda=2.0. READ-ONLY. No orders.")
    print("Feature tier: OHLCV + training-only selected TA. No EGARCH.")
    print(f"History: {start.date()} .. {end.date()}")

    raw = ensure_hourly_history("BTCUSDT", start, end, history_dir, args.refresh)
    print(f"H1 rows: {len(raw)}  {raw.index.min()} .. {raw.index.max()}")
    features, groups = build_features(raw)
    predictions, folds = walk_forward(features, groups, output_dir)
    print(f"Completed folds: {len(folds)}; OOS predictions: {len(predictions)}")

    scenarios = (
        ("PAPER_10BPS", 0.0010),
        ("USER_6_5BPS", 0.00065),
    )
    segments = (
        ("ALL_OOS", None, None),
        ("PAPER_ERA", None, "2026-01-01"),
        ("POST_PAPER_2026", "2026-01-01", None),
    )
    status: dict[str, object] = {
        "version": VERSION,
        "source": "Binance public spot H1 archive",
        "lambda": args.lambda_value,
        "folds": len(folds),
        "oos_rows": len(predictions),
        "results": {},
    }

    print("\n===== V1.38 SUMMARY =====")
    for label, cost in scenarios:
        executed = execute_cost_aware(predictions, cost=cost, lam=args.lambda_value)
        executed.to_csv(output_dir / f"execution_{label.lower()}.csv", index_label="time")
        scenario_result: dict[str, object] = {}
        for segment, seg_start, seg_end in segments:
            subset = _slice(executed, seg_start, seg_end)
            metrics = _metrics(subset)
            scenario_result[segment] = asdict(metrics)
            print(
                f"{label:12s} {segment:15s} | obs={metrics.observations:5d} "
                f"entries={metrics.entries:3d} exposure={metrics.exposure_pct:5.1f}% "
                f"return={metrics.total_return_pct:8.2f}% ann={metrics.annualized_return_pct:7.2f}% "
                f"Sharpe={metrics.sharpe:6.3f} DD={metrics.max_drawdown_pct:7.2f}% "
                f"B&H={metrics.buy_hold_return_pct:8.2f}%"
            )
        status["results"][label] = scenario_result

    (output_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(f"Output: {output_dir}")
    print("READ-ONLY. No orders, no publication, no private API.")
    print("Important: this is an independent reproduction, not the authors' private code.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradeMind v1.38 XGBoost cost-aware research")
    parser.add_argument("--output-dir", type=Path, default=Path("data/xgb_costaware_v138"))
    parser.add_argument("--start", default="2017-12-01T00:00:00+00:00")
    parser.add_argument("--end", default="2026-08-01T00:00:00+00:00")
    parser.add_argument("--lambda-value", type=float, default=2.0)
    parser.add_argument("--refresh", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
