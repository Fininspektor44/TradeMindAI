# TradeMind SMC Research Report

## Purpose

`trademind-smc-stats` summarizes observation-only market-structure features against forward
outcomes. It is a research report, not a signal generator and not a validation certificate.

## Event groups

The report measures:

- `INTERNAL_BOS` and `INTERNAL_CHOCH` from the 4-bar structure window;
- `SWING_BOS` and `SWING_CHOCH` from the 30-bar structure window;
- `BSL_SWEEP` and `SSL_SWEEP`;
- `BULLISH_FVG` and `BEARISH_FVG`;
- `ANY_SMC_EVENT` for observations containing at least one event.

A single candle may belong to more than one group. Group totals therefore must not be added
together as if they were mutually exclusive.

## Context cuts

- `HIGH_VOLUME`: `volume_ratio_20` is at or above the configured threshold, default `1.2`.
- `NORMAL_VOLUME`: positive relative volume below the threshold.
- `HIGH_SPREAD`: `spread_cost_atr` is at or above the configured threshold, default `0.10`.
- `LOW_SPREAD`: positive spread cost below the threshold.
- `STRUCTURE_ALIGNED`: internal and swing bias are both directional and equal.
- `STRUCTURE_CONFLICT`: internal and swing bias are directional and opposite.

## Metrics

For each forward horizon the report prints:

- observation count;
- evaluated BUY/SELL trade count;
- sample status;
- win rate after spread;
- profit factor after spread;
- average net result normalized by entry ATR.

`WAIT` observations remain in the dataset but are excluded from trade-performance metrics.

## Sample guard

The default minimum is 300 evaluated trades per reported group. Groups below that threshold
are marked `INSUFFICIENT_SAMPLE`. Passing the threshold changes the label to `RESEARCH_SAMPLE`,
but does not by itself prove a durable trading edge.

Recommended interpretation:

- below 300: data-quality monitoring only;
- 300 to 999: early exploratory research;
- 1000 or more: candidate for deeper stability and out-of-sample testing.

## Usage

```bash
trademind-smc-stats
trademind-smc-stats --symbol XAUUSD --non-overlap
trademind-smc-stats --horizon 12 --min-sample 300
trademind-smc-stats --volume-threshold 1.5 --spread-atr-threshold 0.08
```

Use `--non-overlap` for the more conservative view because raw M5 observations can represent
overlapping fixed-horizon trades.
