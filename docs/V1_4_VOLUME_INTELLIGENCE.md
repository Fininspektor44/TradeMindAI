# TradeMind AI v1.4 Volume Intelligence

## Purpose

v1.4 adds a separate, read-only volume and tick-microstructure research stream. It does not change the frozen v1.3 OOS Paper Gate, does not change signal rules, and never opens or closes MetaTrader orders.

## Architecture

```text
MT5 TradeMindAI_VolumeExporter_v1_4.mq5
    -> Terminal Common/Files/TradeMindAI_Volume_v1_4/volume_<symbol>_M5.csv
    -> trademind-volume-collect
    -> data/volume_v1_4/volume_bars.csv
```

The MT5 exporter may repeat the same closed bar after a terminal restart. The Python collector deliberately deduplicates by:

```text
symbol + timeframe + bar open time
```

When duplicate rows differ, the collector prefers the better tick-copy status in this order:

```text
OK > NO_TICKS > PARTIAL > ERROR
```

## Exported features

Each row represents one closed bar and includes:

- candle OHLC and native MT5 tick volume;
- raw tick count and tick arrival rate;
- Bid, Ask and midpoint up/down changes;
- exchange buy/sell tick flags when the broker provides them;
- trade volume and real volume when the symbol provides them;
- mean, minimum, maximum and last spread;
- spread expansion within the bar;
- realized absolute midpoint movement;
- directional tick imbalance and delta proxy;
- relative volume versus the preceding 20 bars;
- volume percentile versus up to 100 preceding bars;
- range-per-tick and body-per-tick efficiency;
- tick-history copy status and error code.

For CFD symbols, `buy_ticks`, `sell_ticks`, `trade_volume`, and `trade_volume_real` may be zero because many brokers do not publish exchange trade flags. Tick count, Bid/Ask changes, spread dynamics, RVOL and price efficiency remain useful research fields.

## MT5 installation

1. Open MetaTrader 5.
2. Choose **File -> Open Data Folder**.
3. Copy `mt5/TradeMindAI_VolumeExporter_v1_4.mq5` into `MQL5/Experts`.
4. Compile it in MetaEditor.
5. Attach exactly one instance to any open chart.
6. Keep the default symbols or adjust broker suffixes in `InpSymbols`.
7. Confirm the Experts log contains:

```text
TradeMind AI v1.4 Volume Intelligence started. Read-only.
```

The output folder is:

```text
%APPDATA%\MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4
```

## Python installation and first collection

From the TradeMindAI project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v14_volume_collect.ps1"
```

Expected output includes a non-zero source-file count and the canonical path:

```text
data\volume_v1_4\volume_bars.csv
```

## Scheduled collection

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install_v14_volume_task.ps1" -EveryMinutes 5 -RunNow
```

Check it with:

```powershell
Get-ScheduledTaskInfo -TaskName "TradeMindAI-v1.4-VolumeCollector" |
  Format-List LastRunTime,LastTaskResult,NextRunTime
```

`LastTaskResult : 0` means the collector completed successfully.

## Research discipline

The v1.3 Paper Gate continues unchanged. v1.4 data starts a new schema and must not be backfilled into the frozen v1.3 training sample. Volume features are observational until a separate walk-forward and OOS study proves that they improve net expectancy, drawdown, calibration, or signal rejection quality.
