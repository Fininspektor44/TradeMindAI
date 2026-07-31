# TradeMind AI v1.4 FX Majors Volume Stream

## Scope

This stream adds seven liquid Forex majors to the read-only v1.4 tick and volume research pipeline:

```text
EURUSD
GBPUSD
USDJPY
USDCHF
USDCAD
AUDUSD
NZDUSD
```

It is deliberately deployed as a second exporter instance. The existing metals, indices and energy exporter remains unchanged and keeps its uninterrupted history.

## Why a separate exporter

- no restart or configuration change is required for the existing seven-market stream;
- FX files have unique symbol-based names and cannot overwrite market files;
- the Python collector already reads every `volume_*_M5.csv` source file and deduplicates by symbol, timeframe and bar time;
- the v1.3 Paper Gate remains frozen and unchanged;
- the FX stream is observational until its own walk-forward and OOS validation is complete.

## Deployment

From the TradeMindAI project directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\deploy_v14_fx_exporter.ps1" -Open
```

The script:

1. reads the already tested canonical v1.4 exporter;
2. creates a separate FX version with the seven majors;
3. finds the MetaTrader `MQL5\Experts` directory containing the current TradeMind exporter;
4. writes `TradeMindAI_VolumeExporter_FX_v1_4.mq5` there;
5. opens the generated source when `-Open` is supplied.

Compile the generated file in MetaEditor and attach exactly one FX exporter instance to any chart. Keep the original market exporter attached as well.

## Expected output

The same common folder is used:

```text
%APPDATA%\MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4
```

Expected additional files:

```text
volume_EURUSD_M5.csv
volume_GBPUSD_M5.csv
volume_USDJPY_M5.csv
volume_USDCHF_M5.csv
volume_USDCAD_M5.csv
volume_AUDUSD_M5.csv
volume_NZDUSD_M5.csv
```

After the first backfill, the existing scheduled Python collector automatically absorbs these files into:

```text
data\volume_v1_4\volume_bars.csv
```

No second collector task is required.

## Forex volume interpretation

Forex CFD data normally does not contain centralized exchange volume. The useful fields are tick count, tick rate, RVOL, Bid/Ask changes, spread dynamics, directional quote imbalance and price efficiency. `buy_ticks`, `sell_ticks` and `trade_volume` may remain zero and must not be presented as real global Forex order flow.

## Research separation

FX results must be evaluated separately from metals and indices. Do not pool raw observations across symbols merely to inflate sample size. Validation should retain at least:

```text
symbol + action + pattern + session + horizon
```

Correlated USD exposures must later be controlled by a dedicated currency-theme gate.
