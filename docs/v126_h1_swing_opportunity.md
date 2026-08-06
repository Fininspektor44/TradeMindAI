# TradeMind v1.26 H1 Swing Opportunity Filter

## Decision chain

1. H1 bias must match the proposed BUY or SELL direction.
2. M15 is a veto only. A clearly opposite bias or structure break rejects the setup.
3. The latest closed M5 candle must close beyond the last confirmed local M5 pivot.
4. M5 volume must be at least 1.20 times the median of the previous 20 closed M5 candles.
5. M5 delta turnover must point in the breakout direction.
6. The last confirmed opposite M5 pivot defines the structural stop.
7. The last confirmed H1 swing extremum defines the target.
8. Space to the H1 target must be at least 1.80R and 0.70 ATR H1.

## Deliberately excluded from the decision

FVG, OTE, funding, open interest, order-book imbalance, retests, staged entries and account sizing do not decide eligibility in v1.26. They may remain visible as research context only.

## Evidence boundary

The v1.26 candidate archive contains only opportunities that pass this exact chain. Historical v1.25 outcomes are not reused because the stop and target geometry changed. `outcomes.jsonl` remains empty until a dedicated forward-only v1.26 journal is introduced.

## Safety

Read-only. Orders OFF. Publication OFF. Exchange API not called. Source archives unchanged. Crypto position sizing not calculated.
