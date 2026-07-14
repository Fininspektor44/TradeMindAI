# TradeMind Data Schema v1.0

TradeMind records market observations for research. These fields are descriptive inputs and
forward outcomes. They do not directly change BUY, SELL or WAIT decisions in v0.6.0.

## Identity

- `schema_version`: journal schema version.
- `signal_id`: unique `symbol:timeframe:timestamp` key.
- `signal_time`, `symbol`, `timeframe`: closed-candle identity.
- `action`, `score`, `confidence`, `reasons`: output of the current transparent signal engine.

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
blank values for fields that were not collected at the time. New rows use schema version 1.0.
