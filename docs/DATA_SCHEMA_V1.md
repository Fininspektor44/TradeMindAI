# TradeMind Data Schema v1.1

TradeMind records market observations for research. These fields are descriptive inputs and
forward outcomes. They do not directly change BUY, SELL or WAIT decisions in v0.7.0.

## Identity

- `schema_version`: journal schema version.
- `signal_id`: unique `symbol:timeframe:timestamp` key.
- `signal_time`, `symbol`, `timeframe`: closed-candle identity.
- `action`, `score`, `confidence`, `reasons`: output of the transparent signal engine.

## Spread

- `spread_points`: MT5 spread reported on the signal candle.
- `point_size`: configured price value of one spread point.
- `spread_cost`: spread converted into price units.
- `spread_cost_atr`: spread cost divided by entry ATR.
- `spread_price_pct`: spread cost as a percentage of entry price.

## Volume

MT5 supplies tick volume, not centralized exchange volume.

- `tick_volume`: tick volume on the signal candle.
- `volume_mean_20`: mean tick volume of the previous 20 closed candles.
- `volume_ratio_20`: signal-candle volume divided by the previous-20 mean.
- `volume_change_pct`: percentage change from the immediately preceding candle.

## Market structure

The structure module is observation-only and has zero signal-score weight.

- `structure_version`: version of the deterministic structure definitions.
- `internal_bias`: `BULLISH`, `BEARISH` or `NEUTRAL` from the prior 4-bar window.
- `internal_reference_high`, `internal_reference_low`: internal reference range.
- `internal_break`: internal `BOS`, `CHOCH`, neutral-direction break or `NONE`.
- `swing_bias`: bias from the prior 30-bar window.
- `swing_reference_high`, `swing_reference_low`: swing reference range.
- `swing_break`: swing `BOS`, `CHOCH`, neutral-direction break or `NONE`.
- `liquidity_reference_high`, `liquidity_reference_low`: prior 20-bar liquidity range.
- `bsl_sweep`, `ssl_sweep`: one when price takes the reference extreme and closes back inside.
- `bsl_sweep_depth`, `ssl_sweep_depth`: sweep depth in price units.
- `bsl_sweep_depth_atr`, `ssl_sweep_depth_atr`: sweep depth normalized by entry ATR.
- `fvg_direction`: `BULLISH`, `BEARISH` or `NONE` for the latest three-candle pattern.
- `fvg_size`, `fvg_size_atr`: FVG size in price and ATR units.
- `structure_event_count`: number of structure events recorded on the observation candle.

Exact definitions are in [`SMC_OBSERVATION_SPEC.md`](SMC_OBSERVATION_SPEC.md).

## Progress

For every configured horizon, currently 3, 6 and 12 closed candles:

- `exit_time_N`, `exit_price_N`: fixed-horizon observation point.
- `market_move_N`: raw exit price minus entry price.
- `directional_move_N`: move aligned to BUY or SELL direction.
- `net_move_N`: directional move after spread cost.
- `net_return_pct_N`: net move as a percentage of entry price.
- `progress_atr_N`: net move divided by entry ATR.
- `mfe_N`, `mae_N`: maximum favorable and adverse excursion.
- `mfe_atr_N`, `mae_atr_N`: excursions normalized by entry ATR.
- `bars_to_mfe_N`, `bars_to_mae_N`: bars needed to reach the measured extremes.

## Result

- `outcome_N`: `WIN`, `LOSS`, `FLAT` or `NO_TRADE` after spread cost.

## Compatibility

Existing journal rows are preserved. When the journal is next rewritten, legacy rows receive
blank values for fields that were not collected at the time. New rows use schema version 1.1.
