# TradeMind v1.15.1: online grid drawdown measurement

## Purpose

The MT5 deal history shows entries, exits, profit, commission, and swap. It does
not show how deeply an averaging basket moved against the account before it
closed. The v1.15.1 snapshot layer joins the read-only MT5 position and account
exports to reconstructed grid baskets.

The source exports are never changed. The pipeline writes a separate enriched
leg file and snapshot diagnostics.

## Inputs

Per account, the MT5 exporter writes:

- `grid_deals_<login>.csv`
- `grid_positions_<login>.csv`
- `grid_account_<login>.csv`

The position file contains one row per currently open position at each polling
time. The account file contains balance and equity at the same cadence.

## Basket matching

A snapshot position is assigned to a reconstructed basket by:

1. Magic number
2. symbol
3. direction
4. basket lifetime

When more than one historical basket has the same identity, the basket whose
open/close interval contains the snapshot time is selected.

All positions belonging to the same basket at one snapshot time are aggregated.

## Measured fields

For every observed basket the pipeline records:

- worst floating drawdown in account currency
- worst floating drawdown as a percentage of the contemporaneous balance
- latest floating profit or loss
- latest volume and number of open positions
- first, latest, and worst snapshot times
- basket age at the latest snapshot
- minutes actually observed by the collector

The account status also records:

- latest balance and equity
- latest account floating profit or loss
- worst balance-to-equity floating drawdown
- worst peak-to-trough equity drawdown

Account metrics cover the whole trading account. Basket metrics are filtered by
the baskets included in the selected report.

## Coverage rule

A basket has measured drawdown coverage only after at least one live position
snapshot is matched to it. A measured zero is valid coverage. A blank value is
not interpreted as zero.

Historical floating drawdown from before the snapshot collector started cannot
be reconstructed from closed deals. Coverage therefore grows forward in time.

## Outputs

Each report receives a `snapshots` folder containing:

- `basket_snapshot_drawdown.csv`
- `status.json`

The enriched leg CSV carries the measured `max_drawdown_money` and
`max_drawdown_pct` fields. The existing Grid Basket Analytics dashboard consumes
that enriched file, so its drawdown coverage and worst-DD cards become real
measurements rather than placeholders.

## Safety

The snapshot module:

- imports no broker or MetaTrader trading API
- sends no orders
- modifies no positions
- changes no source CSV
- changes no strategy logic
