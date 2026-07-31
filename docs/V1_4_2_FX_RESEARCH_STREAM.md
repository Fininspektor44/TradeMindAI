# TradeMind AI v1.4.2 FX Research Stream

## Purpose

v1.4.2 turns the seven-major FX volume feed into a separate read-only research stream. It does not change the frozen v1.3 Paper Gate, does not publish client signals and never opens, modifies or closes MetaTrader orders.

The exact FX universe is:

```text
EURUSD
GBPUSD
USDJPY
USDCHF
USDCAD
AUDUSD
NZDUSD
```

## Research key

Every validation state remains separate by:

```text
symbol + pattern + UTC session + BUY/SELL action + H3/H6/H12 horizon
```

Results are never pooled across currencies merely to inflate the sample. BUY and SELL are never mixed.

## Pipeline

```text
MT5 FX volume exporter
    -> Terminal Common Files volume CSVs
    -> v1.4 canonical volume collector
    -> data/volume_v1_4/volume_bars.csv
    -> trademind-fx-research
    -> data/fx_research_v1_4_2/observations.csv
    -> data/fx_research_v1_4_2/latest.csv
```

The runner refreshes the canonical volume archive first, then rebuilds FX observations and validation states atomically.

## Reused tested engines

v1.4.2 does not invent a second signal language. It reuses:

- the deterministic TradeMind EMA9/EMA21, RSI14 and ATR14 signal engine;
- the existing observation-only SMC structure engine;
- the action-aware chronological validation, drawdown, loss-streak and FDR controls from v1.3.

The structure layer records internal and swing BOS/CHoCH, liquidity sweeps and FVG observations. It remains observational and cannot execute a trade.

## Volume and microstructure context

Each observation includes:

- MT5 tick volume, raw tick count and tick rate;
- RVOL20 and the 100-bar volume percentile;
- Bid, Ask and midpoint up/down changes;
- directional quote imbalance and delta proxy;
- mean, minimum, maximum and last spread;
- spread expansion and spread cost in ATR;
- range-per-tick and body-per-tick efficiency;
- rolling tick-rate, spread and efficiency ratios.

For spot-FX CFDs these fields are quote-flow proxies, not centralized exchange volume or true global order-flow delta.

## Research labels

Alongside SMC labels, the stream can mark:

```text
HIGH_RVOL
EXTREME_RVOL
HIGH_TICK_ACTIVITY
TICK_ACCELERATION
POSITIVE_QUOTE_IMBALANCE
NEGATIVE_QUOTE_IMBALANCE
QUOTE_PRESSURE_ALIGNED
QUOTE_PRESSURE_CONFLICT
SPREAD_EXPANDING
VOLUME_ABSORPTION
BULLISH_VOLUME_IMPULSE
BEARISH_VOLUME_IMPULSE
```

These labels are research hypotheses. They do not improve or block the frozen v1.3 signals until a later walk-forward and out-of-sample study proves value.

## UTC sessions

The default research sessions are:

```text
ASIA                 00:00-06:59 UTC
LONDON               07:00-11:59 UTC
LONDON_NY_OVERLAP    12:00-16:59 UTC
NEW_YORK             17:00-20:59 UTC
OFF_HOURS            21:00-23:59 UTC
```

The MT5 bar timestamp is converted with `ServerUtcOffsetHours`. Keep this value fixed for a research run. Changing it later changes session labels and invalidates comparisons.

## First run

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\scripts\run_v142_fx_research.ps1" `
  -ServerUtcOffsetHours 0
```

Expected output contains healthy FX rows, research observations, completed H3/H6/H12 counts and validation states. Early states will normally be `INSUFFICIENT_SAMPLE`; that is correct until enough distinct trading days accumulate.

## Scheduled research

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\scripts\install_v142_fx_research_task.ps1" `
  -EveryMinutes 5 `
  -ServerUtcOffsetHours 0 `
  -RunNow
```

Check the task with:

```powershell
Get-ScheduledTaskInfo -TaskName "TradeMindAI-v1.4.2-FXResearch" |
  Format-List LastRunTime,LastTaskResult,NextRunTime
```

`LastTaskResult : 0` means the research cycle completed successfully.

## Promotion discipline

No FX combination can become a client product from an in-sample card. Promotion requires a frozen rule, a later OOS boundary, enough non-overlapping observations, positive expectancy after spread, stable chronological halves, acceptable drawdown and multiple-testing control.
