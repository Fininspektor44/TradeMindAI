# TradeMind SMC Observation Specification

## Purpose

Smart Money Concepts are treated as **observation-only market features**. They do not
change BUY/SELL/WAIT decisions until their incremental value is demonstrated on forward
outcomes.

This specification was derived from the user's Smart Market Structure Concepts MT5 19.8
configuration and chart examples. It is a transparent TradeMind interpretation, not a clone
of the proprietary indicator.

## Reference settings

- Maximum bars: 500
- Internal structure enabled
- Internal lookback reference: 4 bars
- Swing structure enabled
- Swing lookback reference: 30 bars
- Fractal display reference: 25 bars
- Liquidity structure lookback: 20 bars
- BSL/SSL threshold reference: 0.8
- FVG minimum filter reference: 0.1 ATR
- Delete filled FVGs: enabled in the reference indicator
- FVG touch threshold reference: 0.2
- OTE reference level: 70.5%
- Fibonacci context levels: 61.8%, 70.5%, 79.0%
- Asian range reference: 02:00 to 06:00 terminal time
- SMT pivot lookback reference: 3, disabled in phase 1
- CISD look-ahead reference: 5, disabled in phase 1

Numeric enum values in the `.set` file are not treated as documented semantics. TradeMind
uses its own named, tested definitions instead of guessing proprietary enum mappings.

## Phase 1: structure observations

The first implementation records the following on every newly closed M5 candle:

1. Internal structure context using a 4-bar reference window.
2. Swing structure context using a 30-bar reference window.
3. Bullish or bearish BOS.
4. Bullish or bearish CHoCH.
5. Buy-side or sell-side liquidity sweep using a 20-bar reference window.
6. Sweep depth in price and ATR units.
7. Three-candle bullish or bearish FVG.
8. FVG size in price and ATR units, filtered at 0.1 ATR.

All fields are experimental observations. They receive no score weight.

## Deterministic definitions

### Structure reference

For each configured lookback, the prior candles define a reference high and low. The latest
closed candle is evaluated without using future data.

### BOS and CHoCH

A close beyond the reference high or low is a structure break. A break in the direction of
the prior window bias is recorded as BOS. A break against it is recorded as CHoCH.

### Liquidity sweep

- BSL taken: the latest high exceeds the prior liquidity high and closes back at or below it.
- SSL taken: the latest low exceeds the prior liquidity low and closes back at or above it.

### Fair value gap

- Bullish FVG: candle 3 low is above candle 1 high.
- Bearish FVG: candle 3 high is below candle 1 low.
- The gap must be at least 0.1 ATR in phase 1.

## Deferred features

The following are deliberately excluded until phase 1 data quality is verified:

- Order blocks and breaker blocks
- Internal order blocks
- SMT divergence
- CISD
- Equal highs and lows
- Premium/discount and OTE scoring
- Asian, London and New York session scoring
- Previous day/week/month levels
- Higher-timeframe SMC context

## Multi-timeframe limitation

The current ECN pipeline exports M5 data. H1/H4 structure must not be claimed until the
exporter either exports those timeframes explicitly or TradeMind implements verified candle
aggregation. Higher-timeframe context is a separate milestone.

## Non-negotiable rule

No SMC feature may affect setup quality or trading decisions until forward statistics show
that it improves results after spread and does not merely fit historical noise.
