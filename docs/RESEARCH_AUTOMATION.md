# TradeMind Research Automation

TradeMind v0.9 adds read-only operational control around the ECN research pipeline. It does not
change BUY, SELL or WAIT decisions and does not send orders.

## Health checks

`trademind-health` verifies:

- every configured MT5 CSV file exists and contains the required candle columns;
- the latest candle is recent enough;
- the latest spread and tick volume are positive;
- recent unexplained candle gaps are counted;
- the research journal exists and contains schema `1.1` observations for every symbol;
- signal IDs are unique;
- the journal is receiving fresh observations.

Stale weekday data, missing files, invalid CSV data and duplicate signal IDs are `ERROR` items.
Stale weekend data and non-positive spread or volume are `WARN` items.

Example:

```powershell
.\.venv\Scripts\trademind-health.exe `
  --data-dir "$env:APPDATA\MetaQuotes\Terminal\Common\Files\TradeMindAI_ECN" `
  --journal .\data\journal_ecn\signals.csv `
  --timeframe M5 `
  --max-age-minutes 30
```

## Daily report

`scripts/generate_daily_research_report.ps1` creates a dated UTF-8 text report containing:

1. market-data and journal health;
2. non-overlapping SMC research for horizons 3, 6 and 12 candles;
3. a portfolio-normalized overview;
4. complete per-symbol sections.

Reports are stored under:

```text
data\research_reports\YYYY-MM-DD\research_YYYY-MM-DD_HHMMSS.txt
```

The newest report is also copied to:

```text
data\research_reports\latest.txt
```

Run one report manually:

```powershell
.\scripts\generate_daily_research_report.ps1
```

## Scheduled task

Install a hidden daily task at 23:55 local Windows time:

```powershell
.\scripts\install_daily_research_task.ps1 -DailyTime "23:55" -RunNow
```

The time can be changed later by rerunning the installer with another `-DailyTime` value. The task
uses the current Windows user and runs only when that user is logged on. `-RunNow` is intended for
immediate validation after installation.

## Interpretation rule

A generated report is operational evidence, not permission to trade. Any line marked
`INSUFFICIENT_SAMPLE` remains exploratory regardless of win rate or profit factor. Health `ERROR`
items must be resolved before statistics are trusted.
