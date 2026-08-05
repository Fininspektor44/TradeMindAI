# TradeMind Control Center v1.15.2

## Purpose

The control center compares the two live read-only grid audits:

- AOExtremum, account `37365712`
- MultiRSI, account `37353316`

It rebuilds both reports from the current MT5 exporter files and creates one
side-by-side dashboard. It does not change robot inputs, send orders, modify
positions, or rewrite the original MT5 CSV exports.

## One-command run

```powershell
Set-Location "C:\Users\meff4\Documents\TradeMindAI"
git pull --ff-only origin feature/v1.15-grid-basket-analytics
.\.venv\Scripts\python.exe -m pytest -q .\tests\test_robot_control_center.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_v115_robot_control_center.ps1" -OpenDashboard
```

## Data scope

AOExtremum uses the forward export currently beginning on 4 August 2026. The
control-center pipeline accepts every non-zero Magic already selected by that
account's MT5 monitor. No guessed Magic list is applied.

MultiRSI combines Magic `8035` and `8`, which are the two observed versions of
the same robot family on account `37353316`.

Account floating drawdown is account-wide. Basket floating drawdown is matched
only to baskets included in each report.

## Dashboard sections

The dashboard contains:

- net result and closed/open basket count
- current and worst account floating drawdown
- worst measured basket drawdown in money and percent
- drawdown sample size and coverage
- maximum legs and concurrent baskets
- longest closed and longest observed basket
- current open baskets with age, floating result, volume, and measured drawdown
- side-by-side risk by leg

## Comparison gate

The dashboard refuses to call the risk comparison ready until both robots have
at least 30 measured basket-drawdown samples and no unmatched position snapshot
rows. A stronger sample begins at 100 measured baskets. This is a data-quality
gate, not a profitability guarantee.

## Outputs

```text
data\control_center_v1_15\dashboard\index.html
data\control_center_v1_15\status.json
data\control_center_v1_15\robot_summary.csv
```

Individual rebuilt reports are stored under:

```text
data\control_center_v1_15\reports\
```
