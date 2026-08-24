# SER8 ATR Position Management Experiments V1

Status: PLANNED RESEARCH ONLY. No live/demo behavior is changed by this document.

## Purpose

Test whether position-management geometry can improve SER8 expectancy without
changing the signal population. These experiments must reuse the same historical
candidates, bars, direction, cost model, conservative intrabar ordering, and
research dataset boundaries used by the existing historical replay/screening
pipeline.

The current four-variant execution-geometry experiment remains the active run and
must finish first. The experiments below are follow-up research hypotheses and
must not be mixed into the current run.

## Common trigger

For all ATR50 variants, define the trigger as price moving in the trade's favor by
+0.5 ATR from the MARKET entry price, using the ATR value fixed at entry time.

- BUY: trigger_price = market_entry + 0.5 * entry_ATR
- SELL: trigger_price = market_entry - 0.5 * entry_ATR

No future ATR values may be used to redefine the trigger retroactively.

## Variant 1 — ATR50_PARTIAL_BE

At the first valid trigger of +0.5 ATR:

1. Close 50% of the current position volume.
2. Move the stop on the remaining 50% to breakeven at the MARKET entry price.
3. Keep the original primary take-profit unchanged.
4. Do not trail the stop after breakeven.

Research question: does early partial realization plus breakeven protection improve
payoff/expectancy by reducing full-stop losses without destroying too much winner
size?

## Variant 2 — ATR50_TRAILING

At the first valid trigger of +0.5 ATR:

1. Do not realize a partial profit.
2. Activate a trailing stop for the full remaining position.
3. Keep the original primary take-profit unchanged as the upper exit boundary.
4. The trailing stop may only move in the favorable direction; it may never loosen.

The trailing distance and update step are research parameters and must be estimated
per symbol, not hard-coded in points across all pairs.

Research question: can a per-symbol ATR-normalized trailing rule improve winner
capture while controlling losses better than the static stop/target geometry?

## Variant 3 — ATR50_PARTIAL_TRAIL

At the first valid trigger of +0.5 ATR:

1. Close 50% of the current position volume.
2. Activate a trailing stop on the remaining 50%.
3. Keep the original primary take-profit unchanged.
4. The trailing stop may only move in the favorable direction; it may never loosen.

Research question: does combining partial profit capture with a per-symbol trailing
remainder produce a better expectancy/drawdown trade-off than either PARTIAL_BE or
TRAILING alone?

## Per-symbol trailing parameters

Do not use one fixed point distance for every pair. Estimate ATR-normalized
parameters independently per symbol on research/train data only.

### D_pair — trailing distance

Distance from the favorable post-trigger extreme to the trailing stop, expressed in
entry ATR units.

Initial fixed grid for research:

- 0.25 ATR
- 0.35 ATR
- 0.50 ATR
- 0.65 ATR
- 0.80 ATR
- 1.00 ATR

BUY example:

`trail_stop = favorable_high - D_pair * entry_ATR`

SELL is mirrored from the favorable low.

The first experiment should vary D_pair only and update the trailing stop on each
completed replay bar. This isolates trail width before adding a separate step
parameter.

### S_pair — trailing update step

After a robust D_pair region is identified, test a separate minimum favorable move
required before advancing the stop.

Initial grid:

- 0.10 ATR
- 0.15 ATR
- 0.20 ATR
- 0.25 ATR
- 0.33 ATR

Example: with D_pair = 0.65 ATR and S_pair = 0.20 ATR, the stop advances only when
the favorable extreme improves by at least another 0.20 entry ATR since the last
stop update.

D_pair and S_pair must not be optimized jointly in the first pass; first isolate
D_pair, then test S_pair.

## Quantile-per-symbol trail alternative

After the fixed D_pair grid, test a data-derived trailing distance based on the
post-trigger pullback distribution for trades that later continue favorably.

For each symbol, using train/research data only:

1. Select trades that reached +0.5 ATR.
2. Measure post-trigger favorable continuation.
3. Measure drawdown from the running favorable extreme before subsequent
   continuation/target.
4. Normalize each pullback by entry ATR.
5. Estimate candidate trailing distances from the pullback distribution.

Candidate quantiles:

- Q60 — aggressive
- Q70
- Q80
- Q90 — loose

This yields a symbol-specific D_pair without imposing the same noise tolerance on
EURUSD, GBPJPY, NZDCHF, etc.

## Required metrics

For every symbol and every variant, report at minimum:

- trade count
- BUY/SELL count
- ATR50 trigger activation count and activation rate
- win rate
- average winner R
- average loser R
- payoff ratio
- profit factor
- expectancy R/trade
- pre-cost expectancy and post-cost expectancy where available
- max drawdown in R
- first-50%-realization contribution (for partial variants)
- trailing-half contribution (for ATR50_PARTIAL_TRAIL)
- number/share of original TP winners converted to trailing exits
- number/share of eventual stop losses improved by partial realization or trailing
- stability across chronological research blocks

For partial variants, the report must explicitly separate realized PnL from the
first 50% and from the remaining 50% so that an apparent improvement cannot hide a
worse remainder distribution.

## Research integrity / anti-overfit rules

- Do not mutate the accepted immutable EURUSD hypothesis.
- Do not consume or tune on the protected holdout.
- Do not regenerate signals for these experiments.
- Do not reacquire historical data.
- Use the same candidate population and published bars as the authoritative replay.
- Parameter selection must occur on train/research data only.
- Freeze selected per-symbol parameters before evaluating the next chronological
  validation block.
- Do not select a parameter from full-history performance.
- Do not auto-accept a symbol or deploy a variant because it ranks first.
- Any live/demo change requires the normal hypothesis/validation lifecycle after
  research evidence is established.

## Planned experiment order

1. Finish the current execution-geometry A/B experiment.
2. ATR50_PARTIAL_BE.
3. ATR50_TRAILING using the fixed D_pair grid first, without S_pair.
4. ATR50_PARTIAL_TRAIL using the same fixed D_pair grid first, without S_pair.
5. For promising symbols/regions only, test S_pair separately.
6. Compare fixed-grid D_pair against quantile-per-symbol D_pair.
7. Validate frozen parameters on chronological out-of-sample research blocks.

The three ATR50 variants are separate experiments. Their effects must not be mixed
in one change set or interpreted as one combined hypothesis.
