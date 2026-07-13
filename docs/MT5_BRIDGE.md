# MetaTrader 5 CSV bridge

This bridge exports **closed candles only** from MetaTrader 5. It does not open, modify, or close trades.

## Safe deployment rule

Do not attach the exporter to a chart that already runs a trading EA. Open a separate chart, for example `XAUUSD M5`, and attach `TradeMind_CSV_Exporter` there.

## MT5 exporter

Source file:

```text
mt5/TradeMind_CSV_Exporter.mq5
```

Default settings:

- symbols: `XAUUSD,EURUSD,GBPUSD`
- timeframe: `M5`
- exported closed candles: `300`
- refresh interval: `10` seconds
- destination: MetaTrader common files folder, subfolder `TradeMindAI`

The exporter creates files such as:

```text
XAUUSD_M5.csv
EURUSD_M5.csv
GBPUSD_M5.csv
```

CSV columns:

```text
time,symbol,timeframe,open,high,low,close,tick_volume,spread
```

## TradeMind AI CSV mode

Place the exported CSV files in a directory available to TradeMind AI. The default local directory is:

```text
data/mt5
```

Run the scanner with environment variables:

```bash
TRADEMIND_PROVIDER=csv \
TRADEMIND_DATA_DIR=data/mt5 \
TRADEMIND_SYMBOLS=XAUUSD,EURUSD,GBPUSD \
TRADEMIND_TIMEFRAME=M5 \
trademind
```

Optional stale-data guard:

```bash
TRADEMIND_MAX_DATA_AGE_SECONDS=900
```

Set it to `0` to disable the freshness check during manual file-copy tests.

## First test workflow

1. Compile the exporter in MetaEditor on the Windows SER8.
2. Open a separate chart and attach the exporter.
3. Confirm the MT5 Experts log reports exported files.
4. Copy the generated CSV files into `TradeMindAI/data/mt5` on the Mac.
5. Run TradeMind AI in CSV mode.
6. After the manual test works, automate the transfer or run TradeMind AI directly on the SER8.
