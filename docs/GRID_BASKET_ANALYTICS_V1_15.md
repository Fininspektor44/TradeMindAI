# TradeMind v1.15 Grid Basket Analytics

This module is a read-only risk audit for averaging and grid robots. It does not create trading signals, modify the robot or send orders.

## Data flow

```text
MT5 history deals
  -> TradeMind_Grid_Deal_Exporter.mq5
  -> grid_deals.csv
  -> grid deal reconstruction
  -> basket_legs.csv
  -> basket, leg-risk, symbol and portfolio reports
```

The MT5 exporter only reads account history and rewrites its own CSV in the MetaTrader common files folder. It contains no trade functions.

## MT5 exporter

Source file:

```text
mt5/TradeMind_Grid_Deal_Exporter.mq5
```

Attach it to a separate chart. Default output:

```text
%APPDATA%\MetaQuotes\Terminal\Common\Files\TradeMindAI\grid_deals.csv
```

Useful inputs:

- `InpHistoryDays`: history window;
- `InpMagicFilter`: comma-separated magic numbers; blank means all non-zero magics;
- `InpSymbols`: comma-separated symbols; blank means all;
- `InpMagicLabels`: optional labels such as `445501=AOExtremum;992211=GridSafe`.

## Reconstructed basket-leg contract

`basket_legs.csv` contains one immutable row for every opened leg. Required fields:

- `basket_id`: stable basket identifier;
- `robot`, `magic`, `symbol`, `side`;
- `leg_no`: 1-based leg number;
- `opened_at`: ISO-8601 timestamp;
- `price`, `volume`.

Recommended fields repeated on every row or at least on the final row:

- `closed_at`, `gross_profit`, `commission`, `swap`, `net_profit`;
- `exit_reason` (`TP`, `SL`, `MARKET`, `TIME`, `ROBOT`);
- `max_drawdown_money`, `max_drawdown_pct`, `max_adverse_points`;
- optional market context such as `atr_points`, `spread_points`, `session`, `trend_h1`, `trend_h4`, `news_window`.

A basket must keep the same robot, magic, symbol and side. A `(basket_id, leg_no)` pair must be unique.

## Outputs

- `basket_history.csv`: one normalized row per basket;
- `risk_by_leg.csv`: probability of the next leg, stop frequency, drawdown and result by reached leg;
- `symbol_report.csv`: robot, symbol and direction statistics;
- `portfolio_overlap.csv`: overlapping baskets and joint losses;
- `status.json`: safety contract and aggregate diagnostics;
- `dashboard/index.html`: local dashboard.

## Run the complete Windows pipeline

```powershell
.\scripts\run_v115_grid_pipeline.ps1 -OpenDashboard
```

To use a different MT5 deal export:

```powershell
.\scripts\run_v115_grid_pipeline.ps1 -DealsPath "D:\Reports\grid_deals.csv" -OpenDashboard
```

To analyze an already prepared basket-leg file:

```powershell
.\scripts\check_v115_grid_basket_analytics.ps1 -RunNow -LegsPath "D:\Reports\basket_legs.csv" -OpenDashboard
```

## Drawdown coverage

Deal history reconstructs entries, exits and net results. It does not reconstruct the true intrabasket floating drawdown. Until a snapshot collector is added, `drawdown_coverage` may be zero. The dashboard shows this coverage explicitly so missing drawdown is never presented as measured zero drawdown.

## Safety contract

The status must report:

```text
orders_enabled: false
logic_changed: false
source_modified: false
signal_generation_enabled: false
```
