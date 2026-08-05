# TradeMind v1.15 Grid Basket Analytics

This module is a read-only risk audit for averaging and grid robots. It does not create trading signals, modify the robot or send orders.

## Input contract

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

## Run on Windows

```powershell
.\scripts\check_v115_grid_basket_analytics.ps1 -RunNow -OpenDashboard
```

To use a different export:

```powershell
.\scripts\check_v115_grid_basket_analytics.ps1 -RunNow -LegsPath "D:\Reports\basket_legs.csv" -OpenDashboard
```

## Safety contract

The status must report:

```text
orders_enabled: false
logic_changed: false
source_modified: false
signal_generation_enabled: false
```
